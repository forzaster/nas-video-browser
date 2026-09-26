"""Synthetic local-only fixture. Run with DLNA_DEMO_DATA and uvicorn tests.demo:app.

Fake renderers exercise automatic routing: one accepts only MPEG-TS (rejects MP4), one has no ConnectionManager
(unknown) and never starts MKV, one is offline (every command fails)."""
import os
import time
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from app.main import create_app
from app.upnp import Network, parse_didl, parse_protocols

ROOT = Path(os.environ["DLNA_DEMO_DATA"])
DEVICE = {"id": "uuid:demo", "kind": "server", "name": "動作確認用サーバー（合成動画）",
          "location": "http://127.0.0.1:8766/fixture", "model": "Test fixture"}

# A tiny fake ContentDirectory tree for the source-folder picker.
TREE = {"0": [("1", "ミュージック", 3), ("3", "ビデオ", 2)], "3": [("3$28", "全てのビデオ", 2), ("3$35", "フォルダー", 1)],
        "3$35": [("3$35$1", "2024-01 テストアルバム", 2)]}

RENDERERS = [
    {"id": "uuid:demo-tv-ts", "name": "デモTV（TSのみ）", "sink": "http-get:*:video/vnd.dlna.mpeg-tts:*,http-get:*:video/mp2t:*"},
    {"id": "uuid:demo-tv-any", "name": "デモTV（形式不明・MKV不可）", "sink": None},
    {"id": "uuid:demo-tv-off", "name": "デモTV（オフライン）", "sink": None, "offline": True},
]
# (title, kind) -> resource mime/extension; the second MP4 needs no ffmpeg thumbnail beyond the sample.
MEDIA = [("20240102123450.mp4", "mp4"), ("20240102123451.mp4", "mp4"), ("運動会（MP4なし）.m2ts", "m2ts"),
         ("発表会（MKVのみ）.mkv", "mkv")]
MIMES = {"mp4": "video/mp4", "m2ts": "video/vnd.dlna.mpeg-tts", "mkv": "video/x-matroska"}


class DemoNetwork(Network):
    playing = {}  # renderer id -> (uri, started)

    async def sink_protocols(self, device):
        spec = next(r for r in RENDERERS if r["id"] == device["id"])
        if spec["sink"] is None:
            raise ValueError("ConnectionManagerサービスがありません")
        return parse_protocols(spec["sink"])

    async def command(self, device, action, **kwargs):
        spec = next(r for r in RENDERERS if r["id"] == device["id"])
        if spec.get("offline"):
            raise OSError("demo renderer is offline")
        uri, started = self.playing.get(device["id"], ("", 0))
        if action == "SetAVTransportURI":
            self.playing[device["id"]] = (kwargs["CurrentURI"], time.monotonic())
        elif action == "Stop":
            self.playing.pop(device["id"], None)
        elif action == "GetTransportInfo":
            if not uri:
                return {"CurrentTransportState": "STOPPED"}
            if uri.endswith(".mkv"):  # accepted, but never starts: runtime failure
                return {"CurrentTransportState": "TRANSITIONING"}
            return {"CurrentTransportState": "PLAYING" if time.monotonic() - started < 8 else "STOPPED"}
        elif action == "GetPositionInfo":
            elapsed = min(8, int(time.monotonic() - started)) if uri else 0
            return {"TrackURI": uri, "RelTime": f"00:00:{elapsed:02d}", "TrackDuration": "00:00:08"}
        return {}


    async def detect_root(self, device):
        return {"id": "3$28", "title": "全てのビデオ"}

    async def containers(self, device, parent="0"):
        return [{"id": i, "title": t, "child_count": n} for i, t, n in TREE.get(parent, [])]

    async def items(self, device, root="0", parent=""):
        for n, (title, kind) in enumerate(MEDIA):
            xml = f'''<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
            <item id="demo-{n}" parentID="0" restricted="1"><dc:title>{title}</dc:title><upnp:album>2024-01 テストアルバム</upnp:album><upnp:class>object.item.videoItem</upnp:class><res protocolInfo="http-get:*:{MIMES[kind]}:*" duration="00:00:04" resolution="640x360">http://127.0.0.1:8766/fixture/sample.{kind}</res></item></DIDL-Lite>'''
            yield parse_didl(xml, "")[0]

app = create_app(ROOT / "catalog", DemoNetwork())
app.state.catalog.device(DEVICE)
for spec in RENDERERS:
    app.state.catalog.device({"id": spec["id"], "kind": "renderer", "name": spec["name"],
                              "location": "http://127.0.0.1:8766/fixture", "model": "Fake renderer"})
app.state.player.start_timeout = 8  # quicker runtime-failure demo than the real 20s
app.mount("/fixture", StaticFiles(directory=ROOT), name="fixture")
