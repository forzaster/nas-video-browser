import asyncio
import ipaddress
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .catalog import Catalog
from .jobs import Crawler, Player
from .upnp import Network

log = logging.getLogger(__name__)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def bare(host):
    return (host or "").strip().lower().removeprefix("[").removesuffix("]").rstrip(".")


def allowed_hosts():
    return LOCAL_HOSTS | {bare(h) for h in os.getenv("DLNA_ALLOWED_HOSTS", "").split(",") if h.strip()}


def loopback(host):
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)


def client_rules(value):
    """Parse DLNA_ALLOWED_CLIENTS: IPs/CIDRs, `!` prefix excludes. None when unset."""
    entries = [e.strip() for e in (value or "").split(",") if e.strip()]
    if not entries:
        return None
    rules = ([], [])
    for entry in entries:
        excluded = entry.startswith("!")
        try:
            rules[excluded].append(ipaddress.ip_network(entry.removeprefix("!").strip()))
        except ValueError as error:
            raise ValueError(f"DLNA_ALLOWED_CLIENTS: 不正なエントリ {entry!r} ({error})") from None
    if not rules[0]:
        raise ValueError("DLNA_ALLOWED_CLIENTS: 許可するIP/CIDRがありません")
    return rules


def client_allowed(host, rules):
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    address = getattr(address, "ipv4_mapped", None) or address
    included, excluded = rules
    return any(address in n for n in included) and not any(address in n for n in excluded)


class Location(BaseModel):
    url: str = Field(max_length=2048)


class Title(BaseModel):
    title: str = Field(max_length=300)


class Queue(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=10000)
    repeat: bool = True


def port(url):
    return url.port or {"http": 80, "https": 443}.get(url.scheme)


def same_site(origin, url):
    try:
        parsed = urlparse(origin)
        return (bare(parsed.hostname) in allowed_hosts() and parsed.scheme == url.scheme
                and port(parsed) == port(url))
    except ValueError:
        return False


def create_app(directory=None, network=None):
    clients = client_rules(os.getenv("DLNA_ALLOWED_CLIENTS"))
    catalog = Catalog(directory or os.getenv("DLNA_DATA", "data"))
    network = network or Network()
    crawler, player = Crawler(catalog, network), Player(catalog, network)

    @asynccontextmanager
    async def lifespan(app):
        yield
        tasks = list(crawler.tasks.values()) + list(player.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Family Videos", lifespan=lifespan)
    app.state.catalog, app.state.crawler, app.state.player = catalog, crawler, player
    assets = Path(__file__).parent / "static"

    @app.middleware("http")
    async def access(request: Request, call_next):
        # Host allowlist blocks DNS rebinding; without a token only loopback clients are served.
        if request.url.hostname is None or bare(request.url.hostname) not in allowed_hosts():
            return JSONResponse({"detail": "許可されていないホスト名です"}, status_code=400)
        host = request.client and request.client.host
        if clients and not loopback(host) and not client_allowed(host, clients):
            return JSONResponse({"detail": "許可されていない接続元です"}, status_code=403)
        token = os.getenv("DLNA_TOKEN")
        if not token and not loopback(host):
            return JSONResponse({"detail": "LANから使うにはDLNA_TOKENを設定してください"}, status_code=403)
        if token and not secrets.compare_digest(request.cookies.get("dlna_token", ""), token):
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if request.url.path not in ("/", "/static/app.js", "/static/style.css", "/api/login"):
                if not secrets.compare_digest(supplied, token):
                    return JSONResponse({"detail": "ログインしてください"}, status_code=401)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and not same_site(origin, request.url):
                return JSONResponse({"detail": "別のサイトからの操作は受け付けません"}, status_code=403)
            if request.url.path != "/api/login" and request.headers.get("x-requested-with") != "dlna-library":
                return JSONResponse({"detail": "操作ヘッダーが必要です"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(Exception)
    async def error(request, exception):
        log.exception("unhandled error: %s %s", request.method, request.url.path, exc_info=exception)
        return JSONResponse({"detail": "機器との通信でエラーが発生しました"}, status_code=502)

    def device(id, kind=None):
        value = catalog.get_device(id)
        if not value or (kind and kind not in value["kind"].split(",")):
            raise HTTPException(404, "機器が見つかりません")
        return value

    @app.get("/")
    def index():
        return FileResponse(assets / "index.html")

    @app.post("/api/login")
    async def login(request: Request):
        data = await request.json()
        supplied = data.get("token", "")
        if not isinstance(supplied, str) or not secrets.compare_digest(supplied, os.getenv("DLNA_TOKEN", "")):
            raise HTTPException(401, "トークンが違います")
        response = JSONResponse({"ok": True})
        response.set_cookie("dlna_token", supplied, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https")
        return response

    @app.get("/api/devices")
    def devices():
        return catalog.devices()

    @app.post("/api/discover")
    async def discover():
        results, errors = await network.discover()
        for item in results:
            catalog.device(item)
        return {"devices": results, "errors": errors}

    @app.post("/api/devices")
    async def add(location: Location):
        if urlparse(location.url).scheme not in ("http", "https"):
            raise HTTPException(422, "機器URLはhttp(s)で指定してください")
        try:
            results = network.descriptions(await network.load(location.url))
        except Exception:
            log.exception("device description failed: %s", location.url)
            raise HTTPException(502, "機器説明を取得できませんでした")
        if not results:
            raise HTTPException(422, "ContentDirectory/AVTransportが見つかりません")
        for item in results:
            catalog.device(item)
        return results

    @app.post("/api/servers/{id}/crawl")
    async def crawl(id: str):
        return crawler.start(device(id, "server"))

    @app.get("/api/servers/{id}/crawl")
    def crawl_state(id: str):
        device(id, "server")
        return crawler.state(id)

    @app.get("/api/media")
    def media(server_id: str, year: str | None = None, album: str | None = None):
        return [{k: v for k, v in row.items() if k != "metadata"}
                for row in catalog.media(server_id, year, album)]

    @app.patch("/api/media/{id}/title")
    def title(id: str, body: Title):
        if not catalog.get(id):
            raise HTTPException(404, "動画が見つかりません")
        catalog.title(id, body.title)
        return {"title": catalog.get(id)["title"]}

    @app.post("/api/renderers/{id}/queue")
    async def queue(id: str, body: Queue):
        for media_id in body.ids:
            item = catalog.get(media_id)
            if not item or not item["available"]:
                raise HTTPException(404, "動画が見つかりません")
        try:
            return await player.start(device(id, "renderer"), body.ids, body.repeat)
        except ValueError as error:  # app-authored Japanese messages (missing media/service)
            raise HTTPException(422, str(error))

    @app.post("/api/renderers/{id}/{action}")
    async def control(id: str, action: str):
        actions = {"play": "Play", "pause": "Pause", "stop": "Stop"}
        if action not in actions:
            raise HTTPException(404, "未対応の操作です")
        try:
            await player.control(device(id, "renderer"), actions[action])
        except ValueError as error:
            raise HTTPException(422, str(error))
        return {"ok": True}

    @app.get("/api/renderers/{id}/state")
    def state(id: str):
        device(id, "renderer")
        return player.states.get(id, {"state": "IDLE"})

    app.mount("/static", StaticFiles(directory=assets), name="static")
    app.mount("/thumbs", StaticFiles(directory=catalog.thumbs), name="thumbs")
    return app
