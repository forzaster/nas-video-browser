"""UPnP library adapter; DIDL metadata stays XML, never regex or HTML."""
import asyncio
import os
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from defusedxml.ElementTree import fromstring
from async_upnp_client.aiohttp import AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.search import async_search

DIDL = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
DC = "http://purl.org/dc/elements/1.1/"
UPNP = "urn:schemas-upnp-org:metadata-1-0/upnp/"
for prefix, uri in (("", DIDL), ("dc", DC), ("upnp", UPNP)):
    ET.register_namespace(prefix, uri)


def parse_didl(xml, parent):
    result = []
    root = fromstring(xml)
    for node in root:
        kind = node.tag.rsplit("}", 1)[-1]
        title = node.findtext(f"{{{DC}}}title") or "名称なし"
        clazz = node.findtext(f"{{{UPNP}}}class") or ""
        if kind == "container":
            count = node.get("childCount", "")
            result.append({"container": True, "object_id": node.attrib["id"], "title": title, "class": clazz,
                           "child_count": int(count) if count.isdigit() else None})
            continue
        resources = []
        for res in node.findall(f"{{{DIDL}}}res"):
            url = (res.text or "").strip()
            protocol = res.get("protocolInfo", "")
            if urlparse(url).scheme not in ("http", "https"):
                continue
            mime = protocol.split(":", 3)[2] if protocol.count(":") >= 3 else ""
            if "videoItem" not in clazz and not mime.startswith("video/"):
                continue
            resources.append({"url": url, "protocol": protocol, "mime": mime,
                              "duration": res.get("duration", ""), "size": res.get("size", ""),
                              "resolution": res.get("resolution", "")})
        if not resources:
            continue
        wrapper = ET.Element(f"{{{DIDL}}}DIDL-Lite")
        wrapper.append(node)
        result.append({"container": False, "object_id": node.get("refID") or node.attrib["id"], "title": title,
                       "album": node.findtext(f"{{{UPNP}}}album") or parent,
                       "date": node.findtext(f"{{{DC}}}date") or "",
                       "resources": resources, "metadata": ET.tostring(wrapper, encoding="unicode")})
    return result


# Crawl-root detection: only the video branch is walked, not every music/photo view.
VIDEO_BRANCH = ("ビデオ", "動画", "video", "videos", "movie", "movies")
ALL_VIDEOS = ("全てのビデオ", "すべてのビデオ", "全ビデオ", "all videos", "all video")
FOLDERS = ("フォルダー", "フォルダ", "folders", "browse folders")
ROOT = {"id": "0", "title": "ルート（すべて）"}


def named(entries, names):
    return next((e for e in entries if e["container"] and e["title"].strip().casefold() in names), None)


# Spellings of the same container that renderers and servers use interchangeably.
MIME_ALIASES = [{"video/vnd.dlna.mpeg-tts", "video/mp2t", "video/m2ts", "video/mpeg2-ts"},
                {"video/x-matroska", "video/x-mkv", "video/mkv"}, {"video/x-msvideo", "video/avi", "video/msvideo"},
                {"video/x-ms-wmv", "video/wmv"}, {"video/quicktime", "video/mov"}]


def profile(protocol):
    """DLNA.ORG_PN of a protocolInfo 4th field (None when absent or `*`)."""
    fields = protocol.split(":", 3)[3] if protocol.count(":") >= 3 else ""
    return next((f.split("=", 1)[1].strip() or None for f in fields.split(";")
                 if f.strip().upper().startswith("DLNA.ORG_PN=")), None)


def parse_protocols(value):
    """ConnectionManager Sink/Source CSV -> [{"mime", "pn"}] for http-get (or `*`) entries."""
    result = []
    for entry in (value or "").split(","):
        entry = entry.strip()
        parts = entry.split(":", 3)
        if len(parts) == 4 and parts[0].strip().lower() in ("http-get", "*") and parts[2].strip():
            result.append({"mime": parts[2].strip().lower(), "pn": profile(entry)})
    return result


def mime_matches(sink, mime):
    if sink in ("*", "*/*", mime):
        return True
    if sink.endswith("/*"):
        return mime.startswith(sink[:-1])
    return any(sink in group and mime in group for group in MIME_ALIASES)


def compatible(resource, sinks):
    """sinks=None means unknown: assume playable and rely on runtime failure detection."""
    if not sinks:
        return True
    mime, pn = (resource.get("mime") or "").strip().lower(), profile(resource.get("protocol", ""))
    return any(mime_matches(s["mime"], mime) and not (s["pn"] and pn and s["pn"] != pn) for s in sinks)


def preferred(item, browser=False, sinks=None):
    resources = item["resources"]
    if browser:
        return next((r for r in resources if r["mime"] == "video/mp4" or
                     urlparse(r["url"]).path.lower().endswith(".mp4")), None)
    # Among what the renderer accepts, keep the preference for transport-stream originals over MP4.
    resources = [r for r in resources if compatible(r, sinks)]
    return next((r for r in resources if "mpeg-tts" in r["mime"] or
                 urlparse(r["url"]).path.lower().endswith((".m2ts", ".mts"))), resources[0] if resources else None)


class Network:
    async def load(self, url):
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("機器URLはhttp(s)で指定してください")
        return await UpnpFactory(AiohttpRequester(timeout=10), non_strict=True).async_create_device(url)

    def descriptions(self, root):
        result = []
        for device in root.all_devices:
            kinds = []
            services = [s.service_type for s in device.services.values()]
            if any(":ContentDirectory:" in s for s in services):
                kinds.append("server")
            if any(":AVTransport:" in s for s in services):
                kinds.append("renderer")
            if kinds:
                result.append({"id": device.udn, "name": device.friendly_name,
                               "model": device.model_name, "location": root.device_url,
                               "kind": ",".join(kinds)})
        return result

    async def discover(self):
        locations = set()
        async def found(headers):
            if headers.get("location"):
                locations.add(headers["location"])
        source = (os.environ.get("DLNA_INTERFACE", "0.0.0.0"), 0)
        await async_search(found, timeout=4, source=source)
        results, errors = [], []
        semaphore = asyncio.Semaphore(6)
        async def describe(url):
            async with semaphore:
                return self.descriptions(await self.load(url))
        for url, response in zip(sorted(locations), await asyncio.gather(
                *(describe(u) for u in sorted(locations)), return_exceptions=True)):
            if isinstance(response, Exception):
                errors.append(f"{url}: {response}")
            else:
                results.extend(response)
        return list({r["id"]: r for r in results}.values()), errors

    async def service(self, device, name):
        root = await self.load(device["location"])
        for node in root.all_devices:
            if node.udn == device["id"]:
                for svc in node.services.values():
                    if f":{name}:" in svc.service_type:
                        return svc
        raise ValueError(f"{name}サービスがありません")

    async def browse(self, service, object_id, start=0, count=200, parent=None):
        """One BrowseDirectChildren page: (entries, returned, total). Read-only."""
        response = await service.action("Browse").async_call(
            ObjectID=object_id, BrowseFlag="BrowseDirectChildren", Filter="*",
            StartingIndex=start, RequestedCount=count, SortCriteria="")
        return (parse_didl(response["Result"], parent),
                int(response["NumberReturned"]), int(response["TotalMatches"]))

    async def containers(self, device, parent="0", limit=500):
        """Direct child containers of `parent` (first `limit` children), for the source-folder picker."""
        service = await self.service(device, "ContentDirectory")
        result, start = [], 0
        while start < limit:
            entries, count, total = await self.browse(service, parent, start, min(200, limit - start))
            result += [{"id": e["object_id"], "title": e["title"], "child_count": e["child_count"]}
                       for e in entries if e["container"]]
            start += count
            if count == 0 or start >= total:
                break
        return result

    async def detect_root(self, device, service=None):
        """Video branch -> flat all-videos (only if items carry upnp:album) -> folder view -> branch -> "0"."""
        service = service or await self.service(device, "ContentDirectory")
        top = (await self.browse(service, "0"))[0]
        branch = named(top, VIDEO_BRANCH) or next(
            (e for e in top if e["container"] and "video" in e["class"].lower()), None)
        if not branch:
            return dict(ROOT)
        children = (await self.browse(service, branch["object_id"]))[0]
        flat = named(children, ALL_VIDEOS)
        if flat:
            videos = [e for e in (await self.browse(service, flat["object_id"]))[0] if not e["container"]]
            # parent=None leaves album None when the item has no upnp:album of its own.
            if videos and sum(e["album"] is not None for e in videos) >= 0.9 * len(videos):
                return {"id": flat["object_id"], "title": flat["title"]}
        folder = named(children, FOLDERS)
        chosen = folder or branch
        return {"id": chosen["object_id"], "title": chosen["title"]}

    async def items(self, device, root="0", parent="未分類"):
        """Recursive walk from `root`; items directly in it get `parent` as their album fallback."""
        service = await self.service(device, "ContentDirectory")
        pending, visited = [(root, parent)], set()
        while pending:
            object_id, parent = pending.pop()
            if object_id in visited:
                continue
            visited.add(object_id)
            start = 0
            while True:
                entries, count, total = await self.browse(service, object_id, start, 200, parent)
                for entry in entries:
                    if entry["container"]:
                        pending.append((entry["object_id"], entry["title"]))
                    else:
                        yield entry
                if count == 0:
                    if start < total:
                        raise ValueError("Browseのページングが途中で停止しました")
                    break
                start += count
                if start >= total:
                    break

    async def sink_protocols(self, device):
        """What a renderer accepts, from ConnectionManager GetProtocolInfo. Read-only; raises when unavailable."""
        service = await self.service(device, "ConnectionManager")
        return parse_protocols((await service.action("GetProtocolInfo").async_call()).get("Sink", ""))

    async def command(self, device, action, **kwargs):
        service = await self.service(device, "AVTransport")
        return await service.action(action).async_call(InstanceID=0, **kwargs)
