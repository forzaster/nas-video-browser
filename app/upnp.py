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
        if kind == "container":
            result.append({"container": True, "object_id": node.attrib["id"], "title": title})
            continue
        clazz = node.findtext(f"{{{UPNP}}}class") or ""
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


def preferred(item, browser=False):
    resources = item["resources"]
    if browser:
        return next((r for r in resources if r["mime"] == "video/mp4" or
                     urlparse(r["url"]).path.lower().endswith(".mp4")), None)
    # Preserve the original preference for transport-stream originals over MP4.
    return next((r for r in resources if "mpeg-tts" in r["mime"] or
                 urlparse(r["url"]).path.lower().endswith((".m2ts", ".mts"))), resources[0])


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

    async def items(self, device):
        service = await self.service(device, "ContentDirectory")
        pending, visited = [("0", "未分類")], set()
        while pending:
            object_id, parent = pending.pop()
            if object_id in visited:
                continue
            visited.add(object_id)
            start = 0
            while True:
                response = await service.action("Browse").async_call(
                    ObjectID=object_id, BrowseFlag="BrowseDirectChildren", Filter="*",
                    StartingIndex=start, RequestedCount=200, SortCriteria="")
                entries = parse_didl(response["Result"], parent)
                for entry in entries:
                    if entry["container"]:
                        pending.append((entry["object_id"], entry["title"]))
                    else:
                        yield entry
                count, total = int(response["NumberReturned"]), int(response["TotalMatches"])
                if count == 0:
                    if start < total:
                        raise ValueError("Browseのページングが途中で停止しました")
                    break
                start += count
                if start >= total:
                    break

    async def command(self, device, action, **kwargs):
        service = await self.service(device, "AVTransport")
        return await service.action(action).async_call(InstanceID=0, **kwargs)
