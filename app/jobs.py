import asyncio
import json
import logging
import os
from pathlib import Path

from .upnp import preferred

log = logging.getLogger(__name__)


def seconds(value):
    try:
        parts = [float(p) for p in value.split(":")]
        return sum(n * 60 ** i for i, n in enumerate(reversed(parts)))
    except (ValueError, AttributeError):
        return 0


async def run_process(*args, timeout=90):
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


async def make_thumbnail(catalog, item):
    filename = f"{item['id']}-{item['fingerprint'][:16]}.jpg"
    final = catalog.thumbs / filename
    if final.exists():
        catalog.thumbnail(item["id"], item["fingerprint"], filename)
        return
    resource = preferred(item)
    url = resource["url"]
    duration = seconds(resource.get("duration"))
    parts = []
    try:
        if not duration:
            raw = await run_process(os.getenv("FFPROBE", "ffprobe"), "-v", "error",
                "-rw_timeout", "15000000", "-protocol_whitelist", "http,https,tcp,tls,crypto",
                "-show_entries", "format=duration", "-of", "json", url, timeout=30)
            duration = float(json.loads(raw).get("format", {}).get("duration", 0))
        for index, fraction in enumerate((0.1, 0.35, 0.65, 0.9)):
            part = catalog.thumbs / f"{filename}.{index}.jpg"
            parts.append(part)
            await run_process(os.getenv("FFMPEG", "ffmpeg"), "-nostdin", "-y", "-v", "error",
                "-rw_timeout", "15000000", "-protocol_whitelist", "http,https,tcp,tls,crypto",
                "-ss", str(max(0, duration * fraction)), "-i", url, "-frames:v", "1",
                "-vf", "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2",
                str(part))
        temporary = catalog.thumbs / f"{filename}.tmp.jpg"
        args = []
        for part in parts:
            args.extend(("-i", str(part)))
        await run_process(os.getenv("FFMPEG", "ffmpeg"), "-nostdin", "-y", "-v", "error", *args,
            "-filter_complex", "[0:v][1:v][2:v][3:v]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[v]",
            "-map", "[v]", "-frames:v", "1", str(temporary))
        temporary.replace(final)
        catalog.thumbnail(item["id"], item["fingerprint"], filename)
    except Exception as error:
        catalog.thumbnail(item["id"], item["fingerprint"], error=str(error))
        raise
    finally:
        for part in parts:
            part.unlink(missing_ok=True)
        (catalog.thumbs / f"{filename}.tmp.jpg").unlink(missing_ok=True)


class Crawler:
    def __init__(self, catalog, network):
        self.catalog, self.network = catalog, network
        self.tasks, self.status = {}, {}

    def start(self, device):
        id = device["id"]
        if id in self.tasks and not self.tasks[id].done():
            return self.status[id]
        self.status[id] = {"state": "scanning", "count": 0, "thumbnails": 0, "errors": []}
        self.tasks[id] = asyncio.create_task(self.crawl(device))
        return self.status[id]

    async def crawl(self, device):
        status = self.status[device["id"]]
        ids = set()
        try:
            async for item in self.network.items(device):
                ids.add(self.catalog.upsert(device["id"], item))
                status["count"] = len(ids)
            self.catalog.finish_scan(device["id"], ids)
            status["state"] = "thumbnails"
            for id in sorted(ids):
                try:
                    await make_thumbnail(self.catalog, self.catalog.get(id))
                    status["thumbnails"] += 1
                except Exception as error:
                    status["errors"].append(f"{self.catalog.get(id)['source_title']}: {error}")
            status["state"] = "complete"
        except asyncio.CancelledError:
            status["state"] = "interrupted"
            raise
        except Exception as error:
            status["state"] = "failed"
            status["errors"].append(str(error))


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
