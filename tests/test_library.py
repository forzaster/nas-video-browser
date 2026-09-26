import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from fastapi.testclient import TestClient

from app.catalog import Catalog, display_title, year_for
from app.jobs import Crawler, Player, make_thumbnail
from app.main import create_app
from app.upnp import Network, parse_didl

SERVER = {"id": "uuid:server", "name": "MiniDLNA", "kind": "server", "location": "http://localhost/description.xml", "model": "test"}
RENDERER = {**SERVER, "id": "uuid:tv", "kind": "renderer", "name": "TV"}
HEADERS = {"X-Requested-With": "dlna-library"}
LOCAL = {"base_url": "http://localhost", "client": ("127.0.0.1", 50000)}
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
    with TestClient(app, **LOCAL) as client:
        assert client.patch(f"/api/media/{id}/title", json={"title": "x"}).status_code == 403
        assert client.patch(f"/api/media/{id}/title", headers={**HEADERS, "Origin": "http://evil.example"}, json={"title": "x"}).status_code == 403
        assert client.patch(f"/api/media/{id}/title", headers={**HEADERS, "Origin": "http://localhost:8000"}, json={"title": "x"}).status_code == 403
        assert client.patch(f"/api/media/{id}/title", headers={**HEADERS, "Origin": "null"}, json={"title": "x"}).status_code == 403
        assert client.patch(f"/api/media/{id}/title", headers={**HEADERS, "Origin": "http://localhost"}, json={"title": "ok"}).status_code == 200
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
    monkeypatch.setenv("DLNA_ALLOWED_HOSTS", "192.168.1.10, nas-mac.local")
    with TestClient(create_app(tmp_path), base_url="http://192.168.1.10:8000", client=("192.168.1.20", 50000)) as client:
        assert client.get("/api/devices").status_code == 401
        assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"token": "secret"}).status_code == 200
        assert client.get("/api/devices").status_code == 200
        assert client.get("/api/devices", headers={"Host": "nas-mac.local:8000"}).status_code == 200


def test_host_allowlist_blocks_dns_rebinding(tmp_path, monkeypatch):
    monkeypatch.delenv("DLNA_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("DLNA_TOKEN", raising=False)
    with TestClient(create_app(tmp_path), **LOCAL) as client:
        assert client.get("/api/devices").status_code == 200
        assert client.get("/api/devices", headers={"Host": "127.0.0.1:8000"}).status_code == 200
        assert client.get("/api/devices", headers={"Host": "[::1]:8000"}).status_code == 200
        response = client.get("/api/devices", headers={"Host": "evil.example:8000"})
        assert response.status_code == 400 and "evil" not in response.text
        assert client.get("/", headers={"Host": "attacker.localhost.evil.example"}).status_code == 400
        # Even an Origin matching the (rebound) Host is refused.
        assert client.post("/api/discover", headers={**HEADERS, "Host": "evil.example", "Origin": "http://evil.example"}).status_code == 400
        assert client.post("/api/devices", headers={**HEADERS, "Origin": "http://evil.example"}, json={"url": "http://x"}).status_code == 403
    monkeypatch.setenv("DLNA_ALLOWED_HOSTS", "evil.example")
    with TestClient(create_app(tmp_path), **LOCAL) as client:
        assert client.get("/api/devices", headers={"Host": "evil.example"}).status_code == 200


def test_lan_client_requires_token(tmp_path, monkeypatch):
    monkeypatch.delenv("DLNA_TOKEN", raising=False)
    monkeypatch.setenv("DLNA_ALLOWED_HOSTS", "192.168.1.10")
    lan = {"base_url": "http://192.168.1.10:8000", "client": ("192.168.1.20", 50000)}
    with TestClient(create_app(tmp_path), **lan) as client:
        response = client.get("/api/devices")
        assert response.status_code == 403 and "DLNA_TOKEN" in response.json()["detail"]
        assert client.post("/api/devices", headers=HEADERS, json={"url": "http://169.254.169.254/"}).status_code == 403
    with TestClient(create_app(tmp_path), base_url="http://localhost", client=("::1", 50000)) as client:
        assert client.get("/api/devices").status_code == 200
    monkeypatch.setenv("DLNA_TOKEN", "secret")
    with TestClient(create_app(tmp_path), **lan) as client:
        assert client.get("/api/devices").status_code == 401
        assert client.get("/api/devices", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_client_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("DLNA_TOKEN", "secret")
    monkeypatch.setenv("DLNA_ALLOWED_HOSTS", "192.168.1.10")
    auth = {"Authorization": "Bearer secret"}
    def get(ip, base_url="http://192.168.1.10:8000"):
        with TestClient(create_app(tmp_path), base_url=base_url, client=(ip, 50000)) as client:
            return client.get("/api/devices", headers=auth)
    monkeypatch.delenv("DLNA_ALLOWED_CLIENTS", raising=False)
    assert get("10.9.9.9").status_code == 200  # unset: old behavior
    monkeypatch.setenv("DLNA_ALLOWED_CLIENTS", " 192.168.1.0/24, !192.168.1.1 ")
    assert get("192.168.1.20").status_code == 200
    assert get("::ffff:192.168.1.20").status_code == 200
    response = get("10.9.9.9")
    assert response.status_code == 403 and response.json()["detail"] == "許可されていない接続元です"
    assert get("192.168.1.1").status_code == 403
    assert get("::ffff:192.168.1.1").status_code == 403
    assert get("testclient").status_code == 403
    assert get("127.0.0.1", "http://localhost").status_code == 200
    assert get("::1", "http://localhost").status_code == 200
    assert get("::ffff:127.0.0.1", "http://localhost").status_code == 200
    with TestClient(create_app(tmp_path), base_url="http://192.168.1.10:8000", client=("192.168.1.20", 50000)) as client:
        assert client.get("/api/devices").status_code == 401  # token still required
    monkeypatch.delenv("DLNA_TOKEN")
    assert get("192.168.1.20").status_code == 403  # no token: LAN still refused
    for bad in ("192.168.1.0/33", "not-an-ip", "!", "192.168.1.5/24", "!192.168.1.1"):
        monkeypatch.setenv("DLNA_ALLOWED_CLIENTS", bad)
        with pytest.raises(ValueError, match="DLNA_ALLOWED_CLIENTS"):
            create_app(tmp_path)


def test_device_errors_are_not_leaked(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("DLNA_TOKEN", raising=False)
    net = Network()
    net.load = AsyncMock(side_effect=RuntimeError("upstream secret body 10.0.0.5"))
    with TestClient(create_app(tmp_path, net), **LOCAL) as client:
        response = client.post("/api/devices", headers=HEADERS, json={"url": "http://10.0.0.5/desc.xml"})
        assert response.status_code == 502
        assert response.json()["detail"] == "機器説明を取得できませんでした"
        assert "secret" not in response.text
        response = client.post("/api/devices", headers=HEADERS, json={"url": "file:///etc/passwd"})
        assert response.status_code == 422 and response.json()["detail"] == "機器URLはhttp(s)で指定してください"
    assert "upstream secret body" in caplog.text


def test_unhandled_exception_is_generic(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("DLNA_TOKEN", raising=False)
    net = Network()
    net.command = AsyncMock(side_effect=RuntimeError("internal detail /Users/x"))
    app = create_app(tmp_path, net)
    app.state.catalog.device(RENDERER)
    with TestClient(app, raise_server_exceptions=False, **LOCAL) as client:
        response = client.post("/api/renderers/uuid:tv/play", headers=HEADERS)
        assert response.status_code == 502
        assert "internal detail" not in response.text and response.json()["detail"]
        net.command = AsyncMock(side_effect=ValueError("AVTransportサービスがありません"))  # app-authored message stays visible
        response = client.post("/api/renderers/uuid:tv/pause", headers=HEADERS)
        assert response.status_code == 422 and response.json()["detail"] == "AVTransportサービスがありません"
    assert "internal detail" in caplog.text


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


def items(*names):
    result = []
    for name in names:
        source = item()
        source["object_id"] = name
        result.append(source)
    return result


class FakeNetwork:
    def __init__(self, sources, fail=False, gate=None):
        self.sources, self.fail, self.gate = sources, fail, gate
        self.calls = 0

    async def items(self, device):
        self.calls += 1
        for index, source in enumerate(self.sources):
            if self.gate and index:
                await asyncio.wait_for(self.gate.wait(), 5)
            yield source
        if self.fail:
            raise RuntimeError("server offline")


def fake_thumbnails(monkeypatch, calls):
    async def fake(catalog, row):
        calls.append(row["id"])
        name = f"{row['id']}-{row['fingerprint'][:16]}.jpg"
        (catalog.thumbs / name).write_bytes(b"jpg")
        catalog.thumbnail(row["id"], row["fingerprint"], name)
        return True
    monkeypatch.setattr("app.jobs.make_thumbnail", fake)


def crawl(db, network):
    async def run():
        crawler = Crawler(db, network)
        crawler.start(SERVER)
        await crawler.tasks[SERVER["id"]]
        return crawler.status[SERVER["id"]]
    return asyncio.run(run())


def test_unchanged_items_are_skipped(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    calls = []
    fake_thumbnails(monkeypatch, calls)
    status = crawl(db, FakeNetwork(items("a", "b", "c")))
    assert (status["state"], status["count"], status["new"], status["skipped"]) == ("complete", 3, 3, 0)
    assert (status["thumbnails"], status["thumbs_total"], len(calls)) == (3, 3, 3)
    with db.connect() as conn:  # a rewrite would reset this marker
        conn.execute("UPDATE media SET updated='marker'")
    changed = items("a", "b", "c", "d")
    changed[1]["title"] = "renamed.mp4"
    status = crawl(db, FakeNetwork(changed))
    assert (status["count"], status["new"], status["updated"], status["skipped"]) == (4, 1, 1, 2)
    # Only the new item needs FFmpeg; b keeps its fingerprint so its cached thumbnail is reused.
    assert len(calls) == 4 and (status["thumbnails"], status["thumbs_skipped"]) == (1, 3)
    rows = {m["object_id"]: m for m in db.media(SERVER["id"])}
    assert rows["a"]["updated"] == rows["c"]["updated"] == "marker"
    assert rows["b"]["updated"] != "marker" and rows["b"]["source_title"] == "renamed.mp4"
    assert all(r["thumbnail"] for r in rows.values())


def test_failed_thumbnail_is_retried_and_cached_one_is_not(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    ok, bad = (db.upsert(SERVER["id"], s) for s in items("ok", "bad"))
    row = db.get(ok)
    (db.thumbs / f"{ok}-{row['fingerprint'][:16]}.jpg").write_bytes(b"cached")
    db.thumbnail(bad, db.get(bad)["fingerprint"], error="ffmpeg failed")
    process = AsyncMock(side_effect=RuntimeError("still broken"))
    monkeypatch.setattr("app.jobs.run_process", process)
    status = crawl(db, FakeNetwork(items("ok", "bad")))
    assert status["state"] == "complete" and status["skipped"] == 2
    assert status["thumbs_skipped"] == 1 and status["thumbs_total"] == 1 and status["thumbnails"] == 0
    assert process.call_count == 1 and len(status["errors"]) == 1  # retried once, only for the failed one
    assert db.get(ok)["thumbnail"] and db.get(bad)["thumb_error"] == "still broken"


def test_start_is_idempotent_while_running(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    fake_thumbnails(monkeypatch, [])
    gate = asyncio.Event()
    network = FakeNetwork(items("a", "b"), gate=gate)
    async def run():
        crawler = Crawler(db, network)
        first = crawler.start(SERVER)
        task = crawler.tasks[SERVER["id"]]
        await asyncio.sleep(0.01)
        assert crawler.start(SERVER) is first and crawler.tasks[SERVER["id"]] is task
        other = crawler.start({**SERVER, "id": "uuid:other"})  # another server never cancels this one
        gate.set()
        await task
        await crawler.tasks["uuid:other"]
        assert first["state"] == other["state"] == "complete" and not task.cancelled()
        assert crawler.start(SERVER) is not first  # finished crawls can be restarted
        await crawler.tasks[SERVER["id"]]
    asyncio.run(run())
    assert network.calls == 3


def test_scan_failure_skips_finish_scan(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    fake_thumbnails(monkeypatch, [])
    old = db.upsert(SERVER["id"], items("gone")[0])
    finish = []
    monkeypatch.setattr(db, "finish_scan", lambda *args: finish.append(args))
    status = crawl(db, FakeNetwork(items("a"), fail=True))
    assert status["state"] == "failed" and status["count"] == 1 and not finish
    assert db.get(old)["available"] == 1


def test_thumbnails_are_generated_during_scan(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    gate, calls = asyncio.Event(), []
    async def fake(catalog, row):
        calls.append(row["object_id"])
        gate.set()  # the scan only continues once the first thumbnail started
        return True
    monkeypatch.setattr("app.jobs.make_thumbnail", fake)
    status = crawl(db, FakeNetwork(items("a", "b"), gate=gate))
    assert status["state"] == "complete" and calls == ["a", "b"] and status["thumbnails"] == 2


def test_cancel_stops_thumbnail_workers(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    started, workers = asyncio.Event(), []
    async def slow(catalog, row):
        workers.append(asyncio.current_task())
        started.set()
        await asyncio.sleep(60)
    monkeypatch.setattr("app.jobs.make_thumbnail", slow)
    async def run():
        crawler = Crawler(db, FakeNetwork(items("a")))
        crawler.start(SERVER)
        task = crawler.tasks[SERVER["id"]]
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert crawler.status[SERVER["id"]]["state"] == "interrupted"
        assert workers and all(w.done() for w in workers)
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []
    asyncio.run(run())
