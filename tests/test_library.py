import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from async_upnp_client.exceptions import UpnpActionError
from fastapi.testclient import TestClient

from app.catalog import Catalog, display_title, year_for
from app.jobs import THUMB_WORKERS, Crawler, Player, make_thumbnail, process_slots, run_process
from app.main import create_app
from app.upnp import Network, parse_didl, parse_protocols, preferred

SERVER = {"id": "uuid:server", "name": "MiniDLNA", "kind": "server", "location": "http://localhost/description.xml", "model": "test"}
RENDERER = {**SERVER, "id": "uuid:tv", "kind": "renderer", "name": "TV"}
HEADERS = {"X-Requested-With": "dlna-library"}
LOCAL = {"base_url": "http://localhost", "client": ("127.0.0.1", 50000)}
XML = '''<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
<item id="a" parentID="0" restricted="1"><dc:title>20240102123456.mp4</dc:title><dc:date>2024-01-02</dc:date><upnp:album>2024-01 家族旅行</upnp:album><upnp:class>object.item.videoItem</upnp:class><res protocolInfo="http-get:*:video/mp4:*" duration="00:00:20" size="100" resolution="1920x1080">http://localhost/video.mp4?a=1&amp;b=2</res><res protocolInfo="http-get:*:video/vnd.dlna.mpeg-tts:*">http://localhost/video.m2ts</res></item></DIDL-Lite>'''


@pytest.fixture(autouse=True)
def no_tokens(monkeypatch):
    for name in ("DLNA_TOKEN", "DLNA_ADMIN_TOKEN", "DLNA_GUEST_TOKEN"):
        monkeypatch.delenv(name, raising=False)


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
        async def detect_root(self, device):
            return {"id": "0", "title": "root"}
        async def items(self, device, root, parent):
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



def fake_ffmpeg(monkeypatch, frame):
    """run_process stand-in: ffprobe reports 100s, frames go to `frame(part)`, xstack writes the output."""
    async def process(*args, timeout=90):
        if args[0] == "ffprobe":
            return b'{"format": {"duration": "100"}}'
        if "-filter_complex" in args:
            Path(args[-1]).write_bytes(b"jpg")
            return b""
        return await frame(Path(args[-1]))
    monkeypatch.setattr("app.jobs.run_process", process)


def test_frames_are_extracted_concurrently(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    row = db.get(db.upsert(SERVER["id"], item()))
    active, peak = [0], [0]
    async def frame(part):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.01)
        part.write_bytes(b"part")
        active[0] -= 1
    fake_ffmpeg(monkeypatch, frame)
    assert asyncio.run(make_thumbnail(db, row)) is True
    assert peak[0] == 4
    assert [f.name for f in db.thumbs.iterdir()] == [db.get(row["id"])["thumbnail"]]


def test_failed_frame_cancels_the_others_and_cleans_up(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    row = db.get(db.upsert(SERVER["id"], item()))
    started, cancelled = [], []
    async def frame(part):
        part.write_bytes(b"part")
        started.append(part)
        if part.name.endswith(".0.jpg"):
            while len(started) < 4:
                await asyncio.sleep(0)
            raise RuntimeError("frame broken")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(part)
            raise
    fake_ffmpeg(monkeypatch, frame)
    with pytest.raises(RuntimeError, match="frame broken"):
        asyncio.run(asyncio.wait_for(make_thumbnail(db, row), 5))
    assert len(cancelled) == 3 and list(db.thumbs.iterdir()) == []
    assert db.get(row["id"])["thumb_error"] == "frame broken"


def test_process_cap_bounds_concurrent_ffmpeg(monkeypatch):
    monkeypatch.setenv("DLNA_FFMPEG_JOBS", "2")
    active, peak = [0], [0]
    class Proc:
        returncode = None
        async def communicate(self):
            await asyncio.sleep(0.01)
            active[0] -= 1
            self.returncode = 0
            return b"ok", b""
    async def spawn(*args, **kwargs):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        return Proc()
    monkeypatch.setattr("app.jobs.asyncio.create_subprocess_exec", spawn)
    async def run():  # e.g. two servers crawling at once share the same slots
        return await asyncio.gather(*(run_process("ffmpeg") for _ in range(6)))
    assert asyncio.run(run()) == [b"ok"] * 6 and peak[0] == 2


def test_worker_and_process_env_overrides(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    monkeypatch.delenv("DLNA_THUMB_WORKERS", raising=False)
    assert Crawler(db, None).workers == THUMB_WORKERS
    monkeypatch.setenv("DLNA_THUMB_WORKERS", "6")
    assert Crawler(db, None).workers == 6
    monkeypatch.setenv("DLNA_THUMB_WORKERS", "many")
    assert Crawler(db, None).workers == THUMB_WORKERS
    monkeypatch.setenv("DLNA_FFMPEG_JOBS", "3")
    async def slots():
        return process_slots()._value
    assert asyncio.run(slots()) == 3

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
        assert response.status_code == 403 and "DLNA_ADMIN_TOKEN" in response.json()["detail"]
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
        self.calls, self.roots = 0, []

    async def detect_root(self, device):
        return {"id": "v", "title": "全てのビデオ"}

    async def items(self, device, root, parent):
        self.calls += 1
        self.roots.append((root, parent))
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


LAN = {"base_url": "http://192.168.1.10:8000", "client": ("192.168.1.20", 50000)}
ADMIN, GUEST = {**HEADERS, "Authorization": "Bearer admin-secret"}, {**HEADERS, "Authorization": "Bearer guest-secret"}


def role_app(tmp_path, monkeypatch, sources=("a",)):
    monkeypatch.setenv("DLNA_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("DLNA_GUEST_TOKEN", "guest-secret")
    monkeypatch.setenv("DLNA_ALLOWED_HOSTS", "192.168.1.10")
    net = Network()
    net.command = AsyncMock(return_value={})
    app = create_app(tmp_path, net)
    app.state.catalog.device(SERVER)
    app.state.catalog.device(RENDERER)
    ids = [app.state.catalog.upsert(SERVER["id"], s) for s in items(*sources)]
    return app, ids


def test_roles_and_token_config(tmp_path, monkeypatch):
    app, _ = role_app(tmp_path, monkeypatch)
    with TestClient(app, **LAN) as client:
        assert client.get("/api/me").status_code == 401
        assert client.get("/api/me", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/me", headers=ADMIN).json() == {"role": "admin", "auth": True}
        assert client.get("/api/me", headers=GUEST).json()["role"] == "guest"
        assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"token": "guest-secret"}).json() == {"role": "guest"}
        assert client.get("/api/me").json()["role"] == "guest"
        assert client.get("/api/me", headers=ADMIN).json()["role"] == "admin"
        assert client.post("/api/login", json={"token": "admin-secret"}).json() == {"role": "admin"}
        assert client.get("/api/me").json()["role"] == "admin"
        assert client.post("/api/logout").status_code == 403  # CSRF header still required
        assert client.post("/api/logout", headers=HEADERS).status_code == 200
        assert client.get("/api/me").status_code == 401
    # DLNA_TOKEN stays a working alias for the admin token.
    monkeypatch.delenv("DLNA_ADMIN_TOKEN")
    monkeypatch.setenv("DLNA_TOKEN", "admin-secret")
    with TestClient(create_app(tmp_path), **LAN) as client:
        assert client.get("/api/me", headers=ADMIN).json()["role"] == "admin"
        assert client.get("/api/me", headers=GUEST).json()["role"] == "guest"
    monkeypatch.setenv("DLNA_ADMIN_TOKEN", "admin-secret")
    create_app(tmp_path)  # same value twice is fine
    monkeypatch.setenv("DLNA_ADMIN_TOKEN", "other")
    with pytest.raises(ValueError, match="DLNA_TOKEN"):
        create_app(tmp_path)
    monkeypatch.delenv("DLNA_TOKEN")
    monkeypatch.setenv("DLNA_GUEST_TOKEN", "other")
    with pytest.raises(ValueError, match="別の値"):
        create_app(tmp_path)
    monkeypatch.delenv("DLNA_ADMIN_TOKEN")
    with pytest.raises(ValueError, match="DLNA_ADMIN_TOKEN"):
        create_app(tmp_path)
    monkeypatch.delenv("DLNA_GUEST_TOKEN")
    with TestClient(create_app(tmp_path), **LOCAL) as client:  # no token: loopback is admin
        assert client.get("/api/me").json() == {"role": "admin", "auth": False}


def test_guest_permissions(tmp_path, monkeypatch):
    app, (id,) = role_app(tmp_path, monkeypatch)
    with TestClient(app, **LAN) as client:
        for method, path, body in [("PUT", f"/api/media/{id}/hidden", None), ("DELETE", f"/api/media/{id}/hidden", None),
                                   ("PUT", "/api/servers/uuid:server/hidden", {"year": "2024"}),
                                   ("DELETE", "/api/servers/uuid:server/hidden", {"year": "2024"}),
                                   ("PATCH", f"/api/media/{id}/title", {"title": "x"}),
                                   ("POST", "/api/devices", {"url": "http://10.0.0.5/d.xml"})]:
            response = client.request(method, path, headers=GUEST, json=body)
            assert response.status_code == 403 and response.json()["detail"] == "管理者のみ操作できます", path
        assert client.get("/api/hidden", params={"server_id": SERVER["id"]}, headers=GUEST).status_code == 403
        assert client.get("/api/devices", headers=GUEST).status_code == 200
        assert client.post("/api/renderers/uuid:tv/queue", headers=GUEST, json={"ids": [id]}).status_code == 200
        assert client.post("/api/renderers/uuid:tv/stop", headers=GUEST).status_code == 200
        assert client.patch(f"/api/media/{id}/title", headers=ADMIN, json={"title": "x"}).status_code == 200
        assert client.put("/api/servers/uuid:server/hidden", headers=ADMIN, json={"year": "y" * 301}).status_code == 422


def test_hidden_items_and_groups(tmp_path, monkeypatch):
    sources = items("a", "b", "c")
    sources[2]["album"] = "2024-02 別アルバム"
    app, _ = role_app(tmp_path, monkeypatch, ())
    db = app.state.catalog
    a, b, c = (db.upsert(SERVER["id"], s) for s in sources)
    params = {"server_id": SERVER["id"]}
    def ids(headers, include_hidden=False):
        rows = client.get("/api/media", params={**params, "include_hidden": include_hidden}, headers=headers).json()
        return {r["id"]: r["hidden_effective"] for r in rows}
    with TestClient(app, **LAN) as client:
        assert client.put(f"/api/media/{a}/hidden", headers=ADMIN).json() == {"hidden": True}
        assert client.put("/api/media/missing/hidden", headers=ADMIN).status_code == 404
        assert ids(GUEST) == ids(GUEST, True) == ids(ADMIN) == {b: False, c: False}
        assert ids(ADMIN, True) == {a: True, b: False, c: False}
        # Album hide covers its items only; year hide covers every album.
        album = {"year": "2024", "album": "2024-01 家族旅行"}
        assert client.put("/api/servers/uuid:server/hidden", headers=ADMIN, json=album).status_code == 200
        assert ids(GUEST) == {c: False} and ids(ADMIN, True) == {a: True, b: True, c: False}
        assert client.put("/api/servers/uuid:server/hidden", headers=ADMIN, json={"year": "2024"}).status_code == 200
        assert ids(GUEST) == {} and ids(GUEST, True) == {} and ids(ADMIN, True) == {a: True, b: True, c: True}
        assert client.get("/api/hidden", params=params, headers=ADMIN).json() == {
            "groups": [{"year": "2024", "album": ""}, album]}
        assert client.post("/api/renderers/uuid:tv/queue", headers=GUEST, json={"ids": [c]}).status_code == 404
        assert client.post("/api/renderers/uuid:tv/queue", headers=ADMIN, json={"ids": [c]}).status_code == 200
        assert client.put("/api/servers/uuid:other/hidden", headers=ADMIN, json={"year": "2024"}).status_code == 404
        # Unhide restores, layer by layer.
        assert client.request("DELETE", "/api/servers/uuid:server/hidden", headers=ADMIN, json={"year": "2024"}).status_code == 200
        assert ids(GUEST) == {c: False}
        assert client.request("DELETE", "/api/servers/uuid:server/hidden", headers=ADMIN, json=album).status_code == 200
        assert ids(GUEST) == {b: False, c: False}
        assert client.delete(f"/api/media/{a}/hidden", headers=ADMIN).json() == {"hidden": False}
        assert ids(GUEST) == {a: False, b: False, c: False}


def test_hidden_flag_survives_crawl_and_migration(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old"
    old.mkdir()
    with sqlite3.connect(old / "catalog.sqlite3") as conn:  # schema from before the hidden column
        conn.execute("""CREATE TABLE media (id TEXT PRIMARY KEY, server_id TEXT NOT NULL, object_id TEXT NOT NULL,
            source_title TEXT NOT NULL, custom_title TEXT, album TEXT NOT NULL, year TEXT NOT NULL, date TEXT,
            resources TEXT NOT NULL, metadata TEXT NOT NULL, fingerprint TEXT NOT NULL, thumbnail TEXT, thumb_error TEXT,
            available INTEGER NOT NULL DEFAULT 1, updated TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(server_id, object_id))""")
        conn.execute("INSERT INTO media(id,server_id,object_id,source_title,album,year,resources,metadata,fingerprint)"
                     " VALUES('x','s','o','t.mp4','al','2020','[]','','f')")
    db = Catalog(old)
    assert db.get("x")["hidden"] == 0 and db.get("x")["hidden_effective"] is False
    db.hide("x", True)
    assert Catalog(old).get("x")["hidden"] == 1
    db = Catalog(tmp_path)
    fake_thumbnails(monkeypatch, [])
    crawl(db, FakeNetwork(items("a", "b")))
    a = next(r["id"] for r in db.media(SERVER["id"]) if r["object_id"] == "a")
    db.hide(a, True)
    changed = items("a", "b")
    changed[0]["title"] = "renamed.mp4"  # forces a rewrite of the row
    crawl(db, FakeNetwork(changed))
    assert db.get(a)["hidden"] == 1 and db.get(a)["source_title"] == "renamed.mp4"
    assert [r["object_id"] for r in db.media(SERVER["id"], include_hidden=False)] == ["b"]


def didl(*children):
    ns = 'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
    return f"<DIDL-Lite {ns}>{''.join(children)}</DIDL-Lite>"


def folder(id, title, clazz="object.container.storageFolder"):
    return f'<container id="{id}" childCount="3"><dc:title>{title}</dc:title><upnp:class>{clazz}</upnp:class></container>'


def video(id, album=True):
    return (f'<item id="{id}"><dc:title>{id}.mp4</dc:title>' + ("<upnp:album>2024-01 旅行</upnp:album>" if album else "") +
            f'<upnp:class>object.item.videoItem</upnp:class><res protocolInfo="http-get:*:video/mp4:*">http://localhost/{id}.mp4</res></item>')


class TreeService:
    """Read-only fake ContentDirectory: {object_id: [child xml...]}."""
    def __init__(self, tree):
        self.tree, self.browsed = tree, []

    def action(self, name):
        assert name == "Browse"
        async def call(ObjectID, StartingIndex, RequestedCount, **kwargs):
            self.browsed.append(ObjectID)
            children = self.tree.get(ObjectID, [])
            page = children[StartingIndex:StartingIndex + RequestedCount]
            return {"Result": didl(*page), "NumberReturned": len(page), "TotalMatches": len(children)}
        return type("Action", (), {"async_call": staticmethod(call)})()


def tree_network(tree):
    net, service = Network(), TreeService(tree)
    net.service = AsyncMock(return_value=service)
    return net, service


def twonky(album=True):
    return {"0": [folder("0$1", "ミュージック"), folder("0$2", "ピクチャー"), folder("0$3", "ビデオ")],
            "0$3": [folder("0$3$37", "アルバム"), folder("0$3$35", "フォルダー"), folder("0$3$28", "全てのビデオ")],
            "0$3$28": [video("v1", album), video("v2", album)],
            "0$3$35": [folder("0$3$35$1", "2024-01 旅行")], "0$3$35$1": [video("v1", album), video("v2", album)]}


def test_detect_root_prefers_flat_container_with_albums():
    net, service = tree_network(twonky())
    assert asyncio.run(net.detect_root(SERVER)) == {"id": "0$3$28", "title": "全てのビデオ"}
    assert "0$1" not in service.browsed and "0$2" not in service.browsed  # music/photo views untouched
    async def walk():
        return [i async for i in net.items(SERVER, "0$3$28")]
    assert [(i["object_id"], i["album"]) for i in asyncio.run(walk())] == [("v1", "2024-01 旅行"), ("v2", "2024-01 旅行")]


def test_detect_root_uses_folders_when_flat_items_lack_albums():
    tree = twonky(album=False)  # MiniDLNA style: "All Video" items carry no upnp:album
    tree["0"] = [folder("64", "Browse Folders"), folder("1", "Music"), folder("2", "Video", "object.container")]
    tree["2"] = [folder("2$8", "All Video"), folder("2$15", "Folders")]
    tree["2$8"], tree["2$15"] = tree.pop("0$3$28"), tree.pop("0$3$35")
    net, _ = tree_network(tree)
    assert asyncio.run(net.detect_root(SERVER)) == {"id": "2$15", "title": "Folders"}
    async def walk():
        return [i async for i in net.items(SERVER, "2$15")]
    assert {i["album"] for i in asyncio.run(walk())} == {"2024-01 旅行"}  # parent folder, not "All Video"


def test_detect_root_by_class_and_fallbacks():
    net, _ = tree_network({"0": [folder("a", "Media", "object.container.videoContainer")], "a": [folder("b", "x")]})
    assert asyncio.run(net.detect_root(SERVER)) == {"id": "a", "title": "Media"}  # branch itself
    net, _ = tree_network({"0": [folder("m", "Music"), folder("p", "Photos")]})
    assert asyncio.run(net.detect_root(SERVER))["id"] == "0"
    net, _ = tree_network({"0": [folder("1", "ミュージック"), folder("3", "ビデオ")], "3": [folder("7", "フォルダ")]})
    assert asyncio.run(net.containers(SERVER, "0")) == [{"id": "1", "title": "ミュージック", "child_count": 3},
                                                        {"id": "3", "title": "ビデオ", "child_count": 3}]


def test_manual_root_overrides_auto_and_survives_discovery(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    fake_thumbnails(monkeypatch, [])
    db.device(SERVER)
    network = FakeNetwork(items("a"))
    async def run(device):
        crawler = Crawler(db, network)
        crawler.start(device)
        await crawler.tasks[SERVER["id"]]
        return crawler.status[SERVER["id"]]
    status = asyncio.run(run(db.get_device(SERVER["id"])))
    assert status["root"] == {"id": "v", "title": "全てのビデオ", "auto": True} and network.roots[-1] == ("v", "未分類")
    db.crawl_root(SERVER["id"], "0$3$35", "フォルダー")
    db.device({**SERVER, "name": "renamed"})  # discovery upsert keeps the admin choice
    device = db.get_device(SERVER["id"])
    assert (device["name"], device["crawl_root"], device["crawl_root_title"]) == ("renamed", "0$3$35", "フォルダー")
    status = asyncio.run(run(device))
    assert status["root"] == {"id": "0$3$35", "title": "フォルダー", "auto": False} and network.roots[-1][0] == "0$3$35"
    db.crawl_root(SERVER["id"], None, "ignored")
    assert db.get_device(SERVER["id"])["crawl_root"] is db.get_device(SERVER["id"])["crawl_root_title"] is None


def test_devices_migration_adds_crawl_root(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / "catalog.sqlite3") as conn:
        conn.execute("CREATE TABLE devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL,"
                     " kind TEXT NOT NULL, model TEXT, seen TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO devices(id,name,location,kind) VALUES('uuid:server','n','http://x','server')")
    db = Catalog(tmp_path)
    assert db.get_device(SERVER["id"])["crawl_root"] is None
    db.crawl_root(SERVER["id"], "7")
    assert Catalog(tmp_path).get_device(SERVER["id"])["crawl_root_title"] == "7"


def test_empty_crawl_fails_without_finish_scan(tmp_path, monkeypatch):
    db = Catalog(tmp_path)
    old = db.upsert(SERVER["id"], items("old")[0])
    finish = []
    monkeypatch.setattr(db, "finish_scan", lambda *args: finish.append(args))
    status = crawl(db, FakeNetwork([]))
    assert status["state"] == "failed" and not finish
    assert status["errors"] == ["動画が見つかりませんでした。取得元フォルダを確認してください"]
    assert db.get(old)["available"] == 1


def test_crawl_root_api_is_admin_only(tmp_path, monkeypatch):
    app, _ = role_app(tmp_path, monkeypatch)
    net = app.state.crawler.network
    net.containers = AsyncMock(return_value=[{"id": "0$3", "title": "ビデオ", "child_count": 7}])
    path = "/api/servers/uuid:server/"
    with TestClient(app, **LAN) as client:
        assert client.get(path + "containers", headers=GUEST).status_code == 403
        assert client.get(path + "crawl-root", headers=GUEST).status_code == 403
        assert client.put(path + "crawl-root", headers=GUEST, json={"id": "x"}).status_code == 403
        assert client.get(path + "crawl", headers=GUEST).json()["root"] is None  # guests still see status
        assert client.get(path + "containers", params={"parent": "0$3"}, headers=ADMIN).json()[0]["title"] == "ビデオ"
        assert net.containers.call_args.args[1] == "0$3"
        assert client.get("/api/servers/uuid:tv/containers", headers=ADMIN).status_code == 404
        assert client.put(path + "crawl-root", headers=ADMIN, json={"id": "0$3", "title": "ビデオ"}).json() == {
            "id": "0$3", "title": "ビデオ", "last": None}
        assert client.put(path + "crawl-root", headers=ADMIN, json={"id": "x" * 1025}).status_code == 422
        assert client.put(path + "crawl-root", headers=ADMIN, json={"id": ""}).status_code == 422
        assert client.put(path + "crawl-root", headers=ADMIN, json={"id": None}).json()["id"] is None


SINK = ("http-get:*:video/mp4:DLNA.ORG_PN=AVC_MP4_HP_HD_AAC;DLNA.ORG_OP=01, rtsp-rtp-udp:*:video/x-other:*,"
        "http-get:*:video/mp2t:DLNA.ORG_PN=AVC_TS_JP_AAC_T;DLNA.ORG_FLAGS=01,http-get:*:audio/mpeg:*")


def media(name, *kinds, pn=None):
    """An item with one resource per kind (mp4 / m2ts / mkv) at a name-specific URL."""
    source = item()
    source["object_id"] = name
    mimes = {"mp4": "video/mp4", "m2ts": "video/vnd.dlna.mpeg-tts", "mkv": "video/x-matroska"}
    source["resources"] = [{"url": f"http://localhost/{name}.{k}", "mime": mimes[k], "duration": "00:00:20",
                            "protocol": f"http-get:*:{mimes[k]}:" + (f"DLNA.ORG_PN={pn}" if pn else "*"),
                            "size": "", "resolution": ""} for k in kinds]
    return source


def test_sink_parsing_and_resource_matching():
    sinks = parse_protocols(SINK)
    assert sinks == [{"mime": "video/mp4", "pn": "AVC_MP4_HP_HD_AAC"}, {"mime": "video/mp2t", "pn": "AVC_TS_JP_AAC_T"},
                     {"mime": "audio/mpeg", "pn": None}]
    both = media("a", "mp4", "m2ts")
    assert preferred(both)["url"].endswith(".m2ts")  # unknown sink: previous preference
    assert preferred(both, sinks=sinks)["url"].endswith(".m2ts")  # mp2t is an alias of vnd.dlna.mpeg-tts
    assert preferred(both, sinks=parse_protocols("http-get:*:video/mp4:*"))["url"].endswith(".mp4")
    assert preferred(media("b", "mkv"), sinks=sinks) is None
    assert preferred(media("b", "mkv"), sinks=parse_protocols("http-get:*:video/*:*")) is not None
    # PN is compared only when both sides name one.
    assert preferred(media("c", "m2ts", pn="AVC_TS_JP_AAC_T"), sinks=sinks) is not None
    assert preferred(media("c", "m2ts", pn="AVC_TS_EU_ISO"), sinks=sinks) is None
    assert preferred(media("c", "m2ts", pn="AVC_TS_EU_ISO"), sinks=parse_protocols("http-get:*:video/mp2t:*")) is not None
    assert preferred(media("c", "m2ts"), sinks=parse_protocols("")) is not None  # empty list = unknown


class FakeRenderer:
    """AVTransport stand-in: `plays(uri)` decides whether a pushed URI ever reaches PLAYING; a playing track ends
    on its second poll. sinks=None: no ConnectionManager."""
    def __init__(self, sinks="http-get:*:video/mp4:*", plays=lambda uri: True, fail=None):
        self.sinks, self.plays, self.fail, self.uri, self.pushed, self.polls = sinks, plays, fail, None, [], 0

    async def sink_protocols(self, device):
        if self.sinks is None:
            raise ValueError("ConnectionManagerサービスがありません")
        return parse_protocols(self.sinks)

    async def command(self, device, action, **kwargs):
        if self.fail:
            raise self.fail
        if action == "SetAVTransportURI":
            self.uri, self.polls = kwargs["CurrentURI"], 0
            self.pushed.append(self.uri)
        if action == "GetTransportInfo":
            self.polls += 1
            playing = self.plays(self.uri)
            return {"CurrentTransportState": ("PLAYING" if self.polls < 2 else "STOPPED") if playing else "TRANSITIONING"}
        if action == "GetPositionInfo":
            return {"TrackURI": self.uri, "RelTime": "00:00:20" if self.polls >= 2 else "00:00:01", "TrackDuration": "00:00:20"}
        return {}


def run_queue(tmp_path, net, sources, repeat=False, timeout=0.05):
    db = Catalog(tmp_path)
    ids = [db.upsert(SERVER["id"], s) for s in sources]
    player = Player(db, net)
    player.poll, player.start_timeout = 0, timeout
    async def run():
        await player.start(RENDERER, ids, repeat)
        await asyncio.wait_for(player.tasks[RENDERER["id"]], 5)
        return player.states[RENDERER["id"]], await player.plan(RENDERER, ids)
    return ids, *asyncio.run(run())


def test_auto_advance_skips_incompatible_items(tmp_path):
    net = FakeRenderer()
    ids, state, plan = run_queue(tmp_path, net, [media("a", "mp4", "m2ts"), media("b", "m2ts"), media("c", "mp4")])
    assert net.pushed == ["http://localhost/a.mp4", "http://localhost/c.mp4"]
    assert state["skipped"] == 1 and state["index"] == 2 and state["failure"] is None
    assert [p["playable"] for p in plan] == [True, False, True]


def test_runtime_failures_are_skipped_and_all_failing_stops(tmp_path):
    net = FakeRenderer(sinks=None)  # unknown: everything is tried
    net.plays = lambda uri: uri.endswith("a.mp4") and net.pushed.count(uri) == 1
    ids, state, plan = run_queue(tmp_path, net, [media("a", "mp4"), media("b", "mp4"), media("c", "mp4")], repeat=True)
    assert net.pushed == [f"http://localhost/{n}.mp4" for n in "abca"]
    assert state["state"] == "ERROR" and state["failure"] == "all_failed" and state["skipped"] == 3
    assert [p["playable"] for p in plan] == [False, False, False]  # remembered for this renderer


def test_never_playing_first_item_reports_unplayable(tmp_path):
    net = FakeRenderer(sinks=None, plays=lambda uri: False)
    ids, state, plan = run_queue(tmp_path, net, [media("a", "mp4"), media("b", "mp4")])
    assert net.pushed == ["http://localhost/a.mp4"]  # the user's pick is not skipped: the client falls back
    assert state["state"] == "ERROR" and state["failure"] == "unplayable"
    assert [p["playable"] for p in plan] == [False, None]


def test_plan_and_queue_failures_carry_codes(tmp_path):
    net = FakeRenderer(sinks="http-get:*:video/mp4:*")
    app = create_app(tmp_path, net)
    db = app.state.catalog
    db.device(RENDERER)
    db.device({**RENDERER, "id": "uuid:old", "name": "古いTV"})
    a, b = db.upsert(SERVER["id"], media("a", "mp4")), db.upsert(SERVER["id"], media("b", "m2ts"))
    with TestClient(app, raise_server_exceptions=False, **LOCAL) as client:
        response = client.post("/api/renderers/uuid:tv/plan", headers=HEADERS, json={"ids": [a, b]})
        assert response.json() == [{"id": a, "playable": True}, {"id": b, "playable": False}]
        assert client.post("/api/renderers/uuid:tv/plan", headers=HEADERS, json={"ids": ["missing"]}).status_code == 404
        response = client.post("/api/renderers/uuid:tv/queue", headers=HEADERS, json={"ids": [b]})
        assert response.status_code == 422 and response.json()["detail"]["code"] == "unplayable" and not net.pushed
        net.sinks = None  # unknown sink: assume playable
        app.state.player.caps.clear()
        assert client.post("/api/renderers/uuid:old/plan", headers=HEADERS, json={"ids": [b]}).json()[0]["playable"] is None
        net.fail = OSError("connect to 10.0.0.9 refused")
        response = client.post("/api/renderers/uuid:old/queue", headers=HEADERS, json={"ids": [a]})
        assert response.status_code == 502 and response.json()["detail"]["code"] == "push_failed"
        assert "10.0.0.9" not in response.text and "古いTV" in response.json()["detail"]["message"]
        net.fail = UpnpActionError(error_code=714, error_desc="Illegal MIME-type")
        response = client.post("/api/renderers/uuid:old/queue", headers=HEADERS, json={"ids": [a]})
        assert response.status_code == 502 and response.json()["detail"]["code"] == "unplayable"
        assert client.post("/api/renderers/uuid:old/plan", headers=HEADERS, json={"ids": [a]}).json()[0]["playable"] is False
        assert client.get("/api/renderers/uuid:old/state").json()["failure"] == "unplayable"


def test_guest_cannot_plan_or_queue_hidden_items(tmp_path, monkeypatch):
    app, (id,) = role_app(tmp_path, monkeypatch)
    app.state.catalog.hide(id, True)
    with TestClient(app, **LAN) as client:
        for path in ("plan", "queue"):
            assert client.post(f"/api/renderers/uuid:tv/{path}", headers=GUEST, json={"ids": [id]}).status_code == 404
            assert client.post(f"/api/renderers/uuid:tv/{path}", headers=ADMIN, json={"ids": [id]}).status_code == 200
