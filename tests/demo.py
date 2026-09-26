"""Synthetic local-only fixture. Run with DLNA_DEMO_DATA and uvicorn tests.demo:app."""
import os
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from app.main import create_app
from app.upnp import Network, parse_didl

ROOT = Path(os.environ["DLNA_DEMO_DATA"])
DEVICE = {"id": "uuid:demo", "kind": "server", "name": "動作確認用サーバー（合成動画）",
          "location": "http://127.0.0.1:8766/fixture", "model": "Test fixture"}

class DemoNetwork(Network):
    async def items(self, device):
        for n in range(2):
            xml = f'''<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
            <item id="demo-{n}" parentID="0" restricted="1"><dc:title>2024010212345{n}.mp4</dc:title><upnp:album>2024-01 テストアルバム</upnp:album><upnp:class>object.item.videoItem</upnp:class><res protocolInfo="http-get:*:video/mp4:*" duration="00:00:04" resolution="640x360">http://127.0.0.1:8766/fixture/sample.mp4</res></item></DIDL-Lite>'''
            yield parse_didl(xml, "")[0]

app = create_app(ROOT / "catalog", DemoNetwork())
app.state.catalog.device(DEVICE)
app.mount("/fixture", StaticFiles(directory=ROOT), name="fixture")
