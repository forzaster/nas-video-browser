import asyncio
import ipaddress
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .catalog import Catalog
from .jobs import Crawler, Player, Unplayable, sync_days
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


class Group(BaseModel):
    year: str = Field(min_length=1, max_length=300)
    album: str = Field("", max_length=1000)  # "" = the whole year


class CrawlRoot(BaseModel):
    id: str | None = Field(None, min_length=1, max_length=1024)  # None = auto-detect
    title: str | None = Field(None, max_length=300)


class Plan(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=10000)


class Queue(Plan):
    repeat: bool = True


def tokens():
    """(admin, guest). DLNA_TOKEN is a legacy alias for DLNA_ADMIN_TOKEN."""
    admin, legacy = os.getenv("DLNA_ADMIN_TOKEN", ""), os.getenv("DLNA_TOKEN", "")
    guest = os.getenv("DLNA_GUEST_TOKEN", "")
    if admin and legacy and admin != legacy:
        raise ValueError("DLNA_ADMIN_TOKEN と DLNA_TOKEN が異なります（DLNA_TOKENは管理者トークンの旧名です）")
    admin = admin or legacy
    if guest and not admin:
        raise ValueError("DLNA_GUEST_TOKEN を使うには DLNA_ADMIN_TOKEN も設定してください")
    if guest and guest == admin:
        raise ValueError("DLNA_ADMIN_TOKEN と DLNA_GUEST_TOKEN には別の値を設定してください")
    return admin, guest


def role_for(supplied, admin, guest):
    supplied, role = (supplied or "").encode(), None
    for name, token in (("guest", guest), ("admin", admin)):  # compare against every token
        if token and secrets.compare_digest(supplied, token.encode()):
            role = name
    return role


def require_admin(request: Request):
    if getattr(request.state, "role", None) != "admin":
        raise HTTPException(403, "管理者のみ操作できます")


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
    admin_token, guest_token = tokens()
    catalog = Catalog(directory or os.getenv("DLNA_DATA", "data"))
    auto_days = sync_days(os.getenv("DLNA_AUTO_SYNC_DAYS"))
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
        if not admin_token:
            if not loopback(host):
                return JSONResponse({"detail": "LANから使うにはDLNA_ADMIN_TOKENを設定してください"}, status_code=403)
            request.state.role = "admin"
        else:
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
            request.state.role = (role_for(supplied, admin_token, guest_token)  # explicit header wins
                                  or role_for(request.cookies.get("dlna_token"), admin_token, guest_token))
            exempt = ("/", "/static/app.js", "/static/style.css", "/api/login", "/api/logout")
            if not request.state.role and request.url.path not in exempt:
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
        supplied = data.get("token", "") if isinstance(data, dict) else ""
        role = isinstance(supplied, str) and role_for(supplied, admin_token, guest_token)
        if not role:
            raise HTTPException(401, "トークンが違います")
        response = JSONResponse({"role": role})
        response.set_cookie("dlna_token", supplied, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https")
        return response

    @app.post("/api/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie("dlna_token", httponly=True, samesite="strict")
        return response

    @app.get("/api/me")
    def me(request: Request):
        return {"role": request.state.role, "auth": bool(admin_token)}

    @app.get("/api/devices")
    def devices():
        return catalog.devices()

    @app.post("/api/discover")
    async def discover():
        results, errors = await network.discover()
        for item in results:
            catalog.device(item)
        return {"devices": results, "errors": errors}

    @app.post("/api/devices", dependencies=[Depends(require_admin)])
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

    def crawl_status(server):
        return {**crawler.state(server["id"]), **crawler.sync_info(server, auto_days)}

    @app.post("/api/servers/{id}/crawl")
    async def crawl(id: str, auto: bool = False):
        """Manual (default) always starts or joins; auto=true only when the sync is due (page load)."""
        server = device(id, "server")
        if not auto or crawler.running(id) or crawler.auto_due(server, auto_days):
            crawler.start(server)
        return crawl_status(server)

    @app.get("/api/servers/{id}/crawl")
    def crawl_state(id: str):
        return crawl_status(device(id, "server"))

    @app.get("/api/servers/{id}/containers", dependencies=[Depends(require_admin)])
    async def containers(id: str, parent: str = Query("0", min_length=1, max_length=1024)):
        try:
            return await network.containers(device(id, "server"), parent)
        except ValueError as error:  # app-authored (missing service)
            raise HTTPException(422, str(error))

    @app.get("/api/servers/{id}/crawl-root", dependencies=[Depends(require_admin)])
    def crawl_root(id: str):
        value = device(id, "server")
        return {"id": value["crawl_root"], "title": value["crawl_root_title"], "last": crawler.state(id)["root"]}

    @app.put("/api/servers/{id}/crawl-root", dependencies=[Depends(require_admin)])
    def set_crawl_root(id: str, body: CrawlRoot):
        device(id, "server")
        catalog.crawl_root(id, body.id, body.title and body.title.strip())
        return crawl_root(id)

    @app.get("/api/media")
    def media(request: Request, server_id: str, year: str | None = None, album: str | None = None,
              include_hidden: bool = False):
        # Guests never receive hidden rows; admins only when they ask for them.
        show = include_hidden and request.state.role == "admin"
        return [{k: v for k, v in row.items() if k != "metadata"}
                for row in catalog.media(server_id, year, album, include_hidden=show)]

    @app.get("/api/hidden", dependencies=[Depends(require_admin)])
    def hidden(server_id: str):
        return {"groups": catalog.hidden_groups(server_id)}

    @app.put("/api/servers/{id}/hidden", dependencies=[Depends(require_admin)])
    def hide_group(id: str, body: Group):
        device(id, "server")
        catalog.hide_group(id, body.year, body.album, True)
        return {"hidden": True}

    @app.delete("/api/servers/{id}/hidden", dependencies=[Depends(require_admin)])
    def show_group(id: str, body: Group):
        device(id, "server")
        catalog.hide_group(id, body.year, body.album, False)
        return {"hidden": False}

    def hide_item(id, value):
        if not catalog.get(id):
            raise HTTPException(404, "動画が見つかりません")
        catalog.hide(id, value)
        return {"hidden": value}

    @app.put("/api/media/{id}/hidden", dependencies=[Depends(require_admin)])
    def hide(id: str):
        return hide_item(id, True)

    @app.delete("/api/media/{id}/hidden", dependencies=[Depends(require_admin)])
    def show(id: str):
        return hide_item(id, False)

    @app.patch("/api/media/{id}/title", dependencies=[Depends(require_admin)])
    def title(id: str, body: Title):
        if not catalog.get(id):
            raise HTTPException(404, "動画が見つかりません")
        catalog.title(id, body.title)
        return {"title": catalog.get(id)["title"]}

    def playable_ids(request, ids):
        for media_id in ids:
            item = catalog.get(media_id)
            if not item or not item["available"] or (item["hidden_effective"] and request.state.role != "admin"):
                raise HTTPException(404, "動画が見つかりません")

    @app.post("/api/renderers/{id}/plan")
    async def plan(request: Request, id: str, body: Plan):
        """Per item: playable true / false (known incompatible) / null (renderer did not say). Read-only."""
        playable_ids(request, body.ids)
        return await player.plan(device(id, "renderer"), body.ids)

    @app.post("/api/renderers/{id}/queue")
    async def queue(request: Request, id: str, body: Queue):
        playable_ids(request, body.ids)
        renderer = device(id, "renderer")
        try:
            return await player.start(renderer, body.ids, body.repeat)
        except Unplayable as error:
            raise HTTPException(422, {"message": str(error), "code": error.code})
        except ValueError as error:  # app-authored Japanese messages (missing media/service)
            raise HTTPException(422, {"message": str(error), "code": "push_failed"})
        except Exception:  # device unreachable or refused: the client may fall back to the browser
            log.exception("queue push failed: %s", renderer["name"])
            code = player.states.get(id, {}).get("failure") or "push_failed"
            text = "対応していない形式のため再生できませんでした" if code == "unplayable" else "機器に接続できませんでした"
            raise HTTPException(502, {"message": f"{renderer['name']}: {text}", "code": code})

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
