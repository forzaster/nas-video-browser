import asyncio
import json
import logging
import os
from pathlib import Path

from .upnp import preferred

NOTHING_FOUND = "動画が見つかりませんでした。取得元フォルダを確認してください"

log = logging.getLogger(__name__)


def seconds(value):
    try:
        parts = [float(p) for p in value.split(":")]
        return sum(n * 60 ** i for i, n in enumerate(reversed(parts)))
    except (ValueError, AttributeError):
        return 0


def env_int(name, default):
    try:
        return max(1, int(os.getenv(name, "")))
    except ValueError:
        return default


# Defaults for DLNA_FFMPEG_JOBS / DLNA_THUMB_WORKERS, measured against a LinkStation: 4 workers x 4 frames
# was ~2.9x faster than sequential; 16 slots let one crawl run uncapped while a second crawl shares them.
FFMPEG_JOBS, THUMB_WORKERS = 16, 4
_slots = (None, None)


def process_slots():
    """Process-wide cap on concurrent ffmpeg/ffprobe, shared by every crawl (one per running event loop)."""
    global _slots
    loop = asyncio.get_running_loop()
    if _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(env_int("DLNA_FFMPEG_JOBS", FFMPEG_JOBS)))
    return _slots[1]


async def run_process(*args, timeout=90):
    async with process_slots():  # the timeout starts only once a slot is free
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
    if proc.returncode:
        raise RuntimeError(err.decode(errors="replace")[-800:])
    return out


async def all_or_nothing(*coroutines):
    """gather() that cancels and awaits the rest as soon as one fails (or on cancellation)."""
    tasks = [asyncio.ensure_future(c) for c in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def thumb_name(id, fingerprint):
    return f"{id}-{fingerprint[:16]}.jpg"


async def make_thumbnail(catalog, item):
    """Returns True when FFmpeg ran, False when the cached file was reused."""
    filename = thumb_name(item["id"], item["fingerprint"])
    final = catalog.thumbs / filename
    if final.exists():
        catalog.thumbnail(item["id"], item["fingerprint"], filename)
        return False
    resource = preferred(item)
    url = resource["url"]
    duration = seconds(resource.get("duration"))
    parts = [catalog.thumbs / f"{filename}.{index}.jpg" for index in range(4)]
    try:
        if not duration:
            raw = await run_process(os.getenv("FFPROBE", "ffprobe"), "-v", "error",
                "-rw_timeout", "15000000", "-protocol_whitelist", "http,https,tcp,tls,crypto",
                "-show_entries", "format=duration", "-of", "json", url, timeout=30)
            duration = float(json.loads(raw).get("format", {}).get("duration", 0))
        await all_or_nothing(*(run_process(os.getenv("FFMPEG", "ffmpeg"), "-nostdin", "-y", "-v", "error",
            "-rw_timeout", "15000000", "-protocol_whitelist", "http,https,tcp,tls,crypto",
            "-ss", str(max(0, duration * fraction)), "-i", url, "-frames:v", "1",
            "-vf", "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2",
            str(part)) for fraction, part in zip((0.1, 0.35, 0.65, 0.9), parts)))
        temporary = catalog.thumbs / f"{filename}.tmp.jpg"
        args = []
        for part in parts:
            args.extend(("-i", str(part)))
        await run_process(os.getenv("FFMPEG", "ffmpeg"), "-nostdin", "-y", "-v", "error", *args,
            "-filter_complex", "[0:v][1:v][2:v][3:v]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[v]",
            "-map", "[v]", "-frames:v", "1", str(temporary))
        temporary.replace(final)
        catalog.thumbnail(item["id"], item["fingerprint"], filename)
        return True
    except Exception as error:
        catalog.thumbnail(item["id"], item["fingerprint"], error=str(error))
        raise
    finally:
        for part in parts:
            part.unlink(missing_ok=True)
        (catalog.thumbs / f"{filename}.tmp.jpg").unlink(missing_ok=True)


def crawl_status(state="idle"):
    # count=found, new/updated/skipped=DB rows, thumbnails=generated of thumbs_total queued, thumbs_skipped=cached
    # root={"id","title","auto"}: the container this crawl walked (None until chosen)
    return {"state": state, "root": None, "count": 0, "new": 0, "updated": 0, "skipped": 0,
            "thumbnails": 0, "thumbs_total": 0, "thumbs_skipped": 0, "errors": []}


class Crawler:
    """One crawl per server at a time; thumbnails are generated while the tree is still being scanned."""
    def __init__(self, catalog, network):
        self.catalog, self.network = catalog, network
        self.workers = env_int("DLNA_THUMB_WORKERS", THUMB_WORKERS)
        self.tasks, self.status = {}, {}

    def state(self, id):
        return self.status.get(id) or crawl_status()

    def start(self, device):
        id = device["id"]
        if id in self.tasks and not self.tasks[id].done():
            return self.status[id]
        self.status[id] = crawl_status("scanning")
        self.tasks[id] = asyncio.create_task(self.crawl(device))
        return self.status[id]

    async def thumbnailer(self, queue, status):
        while True:
            id, item = await queue.get(), None
            try:
                item = self.catalog.get(id)
                if await make_thumbnail(self.catalog, item):
                    status["thumbnails"] += 1
                else:
                    status["thumbs_skipped"] += 1
                    status["thumbs_total"] -= 1
            except Exception as error:
                status["errors"].append(f"{item['source_title'] if item else id}: {error}")
            finally:
                queue.task_done()

    async def crawl(self, device):
        status = self.status[device["id"]]
        queue, ids = asyncio.Queue(), set()
        workers = [asyncio.create_task(self.thumbnailer(queue, status)) for _ in range(self.workers)]
        try:
            if device.get("crawl_root"):  # admin-chosen container, used as-is
                root = {"id": device["crawl_root"], "title": device.get("crawl_root_title") or device["crawl_root"], "auto": False}
                parent = root["title"]
            else:  # auto: re-detected each crawl (a few Browse calls)
                root = {**await self.network.detect_root(device), "auto": True}
                parent = "未分類"
            status["root"] = root
            async for item in self.network.items(device, root["id"], parent):
                row = self.catalog.sync(device["id"], item)
                if row["id"] in ids:
                    continue
                ids.add(row["id"])
                status["count"] = len(ids)
                status[{"new": "new", "updated": "updated", "unchanged": "skipped"}[row["state"]]] += 1
                filename = thumb_name(row["id"], row["fingerprint"])
                if not (self.catalog.thumbs / filename).exists():
                    # Missing or previously failed: (re)generate in the background.
                    status["thumbs_total"] += 1
                    queue.put_nowait(row["id"])
                    continue
                if row["thumbnail"] != filename:
                    self.catalog.thumbnail(row["id"], row["fingerprint"], filename)
                status["thumbs_skipped"] += 1
            if not ids:  # a wrong/empty root must not mark the whole library unavailable
                raise ValueError(NOTHING_FOUND)
            # Only after the entire tree succeeded. Failed crawls never hide existing media.
            self.catalog.finish_scan(device["id"], ids)
            status["state"] = "thumbnails"
            await queue.join()
            status["state"] = "complete"
        except asyncio.CancelledError:
            status["state"] = "interrupted"
            raise
        except Exception as error:
            status["state"] = "failed"
            status["errors"].append(str(error))
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


class Player:
    """A queue per renderer survives closing the browser, not a server restart."""
    def __init__(self, catalog, network):
        self.catalog, self.network = catalog, network
        self.states, self.tasks, self.locks = {}, {}, {}

    def lock(self, id):
        return self.locks.setdefault(id, asyncio.Lock())

    async def start(self, device, ids, repeat):
        async with self.lock(device["id"]):
            old = self.tasks.pop(device["id"], None)
            if old:
                old.cancel()
                await asyncio.gather(old, return_exceptions=True)
            state = {"queue": ids, "index": 0, "repeat": repeat, "state": "BUFFERING",
                     "position": "", "duration": "", "error": None}
            self.states[device["id"]] = state
            try:
                await self.load_track(device, state)
            except Exception as error:
                state.update(state="ERROR", error=str(error))
                raise
            self.tasks[device["id"]] = asyncio.create_task(self.monitor(device, state))
            return state

    async def load_track(self, device, state):
        item = self.catalog.get(state["queue"][state["index"]])
        if not item or not item["available"]:
            raise ValueError("動画が見つかりません。再クロールしてください")
        resource = preferred(item)
        state.update(media_id=item["id"], title=item["title"], uri=resource["url"],
                     state="BUFFERING", position="", duration=resource["duration"], seen_playing=False)
        await self.network.command(device, "SetAVTransportURI", CurrentURI=resource["url"],
                                   CurrentURIMetaData=item["metadata"])
        await self.network.command(device, "Play", Speed="1")

    async def monitor(self, device, state):
        try:
            while True:
                await asyncio.sleep(2)
                transport = await self.network.command(device, "GetTransportInfo")
                position = await self.network.command(device, "GetPositionInfo")
                current = transport.get("CurrentTransportState", "UNKNOWN")
                duration = position.get("TrackDuration", "")
                state.update(state=current, position=position.get("RelTime", ""))
                if seconds(duration):
                    state["duration"] = duration
                uri = position.get("TrackURI", "")
                if uri and uri != state["uri"]:
                    state["state"] = "EXTERNAL_CONTROL"
                    return
                if current == "PLAYING":
                    state["seen_playing"] = True
                if current == "STOPPED" and state["seen_playing"]:
                    # A pause/stall is NOT completion. Confirm proximity to the end.
                    length = seconds(state["duration"])
                    progress = max(seconds(state["position"]), state.get("last_position", 0))
                    if not length or progress < max(0, length - 4):
                        return
                    state["index"] += 1
                    if state["index"] >= len(state["queue"]):
                        if not state["repeat"]:
                            state["index"] = len(state["queue"]) - 1
                            return
                        state["index"] = 0
                    state["last_position"] = 0
                    await self.load_track(device, state)
                else:
                    state["last_position"] = seconds(state["position"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state.update(state="ERROR", error=str(error))

    async def control(self, device, action):
        async with self.lock(device["id"]):
            task = self.tasks.pop(device["id"], None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            state = self.states.get(device["id"])
            try:
                result = await self.network.command(device, action, **({"Speed": "1"} if action == "Play" else {}))
            except Exception as error:
                if state:
                    state.update(state="ERROR", error=str(error))
                raise
            if state:
                state["state"] = {"Play": "PLAYING", "Pause": "PAUSED_PLAYBACK", "Stop": "STOPPED"}[action]
                if action != "Stop":
                    self.tasks[device["id"]] = asyncio.create_task(self.monitor(device, state))
            return result
