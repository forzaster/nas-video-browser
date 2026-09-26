import asyncio
import json
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.catalog import Catalog, display_title, year_for
from app.jobs import Crawler, Player, make_thumbnail
from app.main import create_app
from app.upnp import Network, parse_didl

SERVER = {"id": "uuid:server", "name": "MiniDLNA", "kind": "server", "location": "http://localhost/description.xml", "model": "test"}
RENDERER = {**SERVER, "id": "uuid:tv", "kind": "renderer", "name": "TV"}
HEADERS = {"X-Requested-With": "dlna-library"}
XML = '''<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
<item id="a" parentID="0" restricted="1"><dc:title>20240102123456.mp4</dc:title><dc:date>2024-01-02</dc:date><upnp:album>2024-01 家族旅行</upnp:album><upnp:class>object.item.videoItem</upnp:class><res protocolInfo="http-get:*:video/mp4:*" duration="00:00:20" size="100" resolution="1920x1080">http://localhost/video.mp4?a=1&amp;b=2</res><res protocolInfo="http-get:*:video/vnd.dlna.mpeg-tts:*">http://localhost/video.m2ts</res></item></DIDL-Lite>'''


def item():
    return parse_didl(XML, "fallback")[0]


def test_xml_and_original_title_rules():
    parsed = item()
    assert parsed["album"] == "2024-01 家族旅行"
    assert len(parsed["resources"]) == 2
    assert "&b=2" in parsed["resources"][0]["url"]
    assert display_title(parsed["title"]) == "12:34:56"
    assert year_for("2023-12", "2024-01-01", "") == "2023"
    assert year_for("旅行", "2024-01-01", "") == "2024"
    assert year_for("旅行", "", "") == "年不明"
    parsed = parse_didl(XML.replace("20240102123456.mp4", "家族の 夏休み &amp; 海.mp4"), "")[0]
    assert parsed["title"] == "家族の 夏休み & 海.mp4"


def test_user_title_survives_recrawl_and_cache_invalidates(tmp_path):
    db = Catalog(tmp_path)
    source = item()
    id = db.upsert(SERVER["id"], source)
    db.title(id, "初めての海")
    fingerprint = db.get(id)["fingerprint"]
    db.thumbnail(id, fingerprint, "cached.jpg")
    db.upsert(SERVER["id"], source)
    assert db.get(id)["thumbnail"] == "cached.jpg"
    source["resources"][0]["size"] = "200"
    db.upsert(SERVER["id"], source)
    assert db.get(id)["title"] == "初めての海"
    assert db.get(id)["thumbnail"] is None
    db.finish_scan(SERVER["id"], [])
    assert not db.media(SERVER["id"])
    db.upsert(SERVER["id"], source)
    assert Catalog(tmp_path).get(id)["title"] == "初めての海"
    db.title(id, " ")
    assert db.get(id)["title"] == "12:34:56"


def test_crawl_failure_keeps_existing_media(tmp_path):
    db = Catalog(tmp_path)
    id = db.upsert(SERVER["id"], item())
    class Broken:
        async def items(self, device):
            if False:
                yield
            raise RuntimeError("server offline")
    async def run():
        crawler = Crawler(db, Broken())
        crawler.start(SERVER)
        await crawler.tasks[SERVER["id"]]
        assert crawler.status[SERVER["id"]]["state"] == "failed"
    asyncio.run(run())
    assert db.get(id)["available"] == 1


def test_cache_reuse_does_not_invoke_ffmpeg(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    id = db.upsert(SERVER["id"], item())
    row = db.get(id)
    filename = f"{id}-{row['fingerprint'][:16]}.jpg"
    (db.thumbs / filename).write_bytes(b"cached")
    process = AsyncMock(side_effect=AssertionError("should not regenerate"))
    monkeypatch.setattr("app.jobs.run_process", process)
    asyncio.run(make_thumbnail(db, row))
    assert db.get(id)["thumbnail"] == filename
    process.assert_not_called()


def test_api_title_and_device_control(tmp_path):
    net = Network()
    net.command = AsyncMock(return_value={})
    app = create_app(tmp_path, net)
    db = app.state.catalog
    db.device(SERVER)
    db.device(RENDERER)
    id = db.upsert(SERVER["id"], item())
    with TestClient(app) as client:
        assert client.patch(f"/api/media/{id}/title", json={"title": "x"}).status_code == 403
        assert client.patch(f"/api/media/{id}/title", headers={**HEADERS, "Origin": "http://evil.example"}, json={"title": "x"}).status_code == 403
        response = client.patch(f"/api/media/{id}/title", headers=HEADERS, json={"title": "<img src=x onerror=alert(1)>"})
        assert response.status_code == 200
        rows = client.get("/api/media", params={"server_id": SERVER["id"]}).json()
        assert len(rows) == 1 and "metadata" not in rows[0]
        assert rows[0]["custom_title"].startswith("<img")
        assert client.post("/api/renderers/uuid:tv/queue", headers=HEADERS, json={"ids": [id]}).status_code == 200
        assert net.command.call_args_list[0].args[1] == "SetAVTransportURI"
        assert net.command.call_args_list[1].args[1] == "Play"
        assert client.post("/api/renderers/uuid:tv/stop", headers=HEADERS).status_code == 200
        assert app.state.player.states[RENDERER["id"]]["state"] == "STOPPED"
        assert client.post("/api/renderers/uuid:tv/queue", headers=HEADERS, json={"ids": ["missing"]}).status_code == 404


def test_lan_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DLNA_TOKEN", "secret")
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/devices").status_code == 401
        assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"token": "secret"}).status_code == 200
        assert client.get("/api/devices").status_code == 200


def test_browse_pagination_and_containers():
    responses = [
        {"Result": '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><container id="folder" /></DIDL-Lite>', "NumberReturned": 1, "TotalMatches": 1},
        {"Result": XML, "NumberReturned": 1, "TotalMatches": 2},
        {"Result": XML.replace('id="a"', 'id="b"'), "NumberReturned": 1, "TotalMatches": 2},
    ]
    call = AsyncMock(side_effect=responses)
    class Service:
        def action(self, name):
            assert name == "Browse"
            return type("Action", (), {"async_call": call})()
    net = Network()
    net.service = AsyncMock(return_value=Service())
    async def run():
        return [i async for i in net.items(SERVER)]
    assert [i["object_id"] for i in asyncio.run(run())] == ["a", "b"]
    assert call.call_args_list[-1].kwargs["StartingIndex"] == 1


def test_pause_does_not_advance_queue(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    id = db.upsert(SERVER["id"], item())
    net = Network()
    responses = [{"CurrentTransportState": "PAUSED_PLAYBACK"}, {"RelTime": "00:00:10", "TrackDuration": "00:00:20"},
                 {"CurrentTransportState": "STOPPED"}, {"RelTime": "00:00:02", "TrackDuration": "00:00:20"}]
    net.command = AsyncMock(side_effect=responses)
    monkeypatch.setattr("app.jobs.asyncio.sleep", AsyncMock())
    player = Player(db, net)
    state = {"queue": [id], "index": 0, "repeat": True, "seen_playing": True,
             "uri": "http://localhost/video.mp4", "duration": "00:00:20"}
    asyncio.run(player.monitor(RENDERER, state))
    assert len(net.command.call_args_list) == 4
    assert all(c.args[1] in ("GetTransportInfo", "GetPositionInfo") for c in net.command.call_args_list)
    assert state["index"] == 0


def test_completed_track_advances(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    id = db.upsert(SERVER["id"], item())
    second = item()
    second["object_id"] = "second"
    next_id = db.upsert(SERVER["id"], second)
    net = Network()
    net.command = AsyncMock(side_effect=[
        {"CurrentTransportState": "PLAYING"}, {"RelTime": "00:00:19", "TrackDuration": "00:00:20"},
        {"CurrentTransportState": "STOPPED"}, {"RelTime": "00:00:00", "TrackDuration": "00:00:20"},
        {}, {}, RuntimeError("end test")])
    monkeypatch.setattr("app.jobs.asyncio.sleep", AsyncMock())
    player = Player(db, net)
    state = {"queue": [id,next_id], "index": 0, "repeat": False, "seen_playing": False,
             "uri": "http://localhost/video.mp4", "duration": "00:00:20"}
    asyncio.run(player.monitor(RENDERER, state))
    assert state["index"] == 1
    assert state["media_id"] == next_id
    assert [c.args[1] for c in net.command.call_args_list][4:6] == ["SetAVTransportURI", "Play"]
