"""Local metadata; source fields and user titles have separate ownership."""
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def display_title(title):
    stem = Path(title).stem
    if re.fullmatch(r"\d{14}", stem):
        return f"{stem[8:10]}:{stem[10:12]}:{stem[12:14]}"
    return stem


# Own flag, or the item's year (album '') or year+album is hidden.
SELECT = """SELECT *, (hidden OR EXISTS(SELECT 1 FROM hidden_groups g WHERE g.server_id=media.server_id
    AND g.year=media.year AND g.album IN ('', media.album))) AS hidden_effective FROM media"""


def year_for(album, date, title):
    for value in (album, date, title):
        match = re.match(r"((?:19|20)\d{2})", value or "")
        if match:
            return match[1]
    return "年不明"


class Catalog:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.thumbs = self.directory / "thumbs"
        self.thumbs.mkdir(exist_ok=True)
        self.path = self.directory / "catalog.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL,
                    kind TEXT NOT NULL, model TEXT, seen TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS media (
                    id TEXT PRIMARY KEY, server_id TEXT NOT NULL, object_id TEXT NOT NULL,
                    source_title TEXT NOT NULL, custom_title TEXT,
                    album TEXT NOT NULL, year TEXT NOT NULL, date TEXT,
                    resources TEXT NOT NULL, metadata TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, thumbnail TEXT, thumb_error TEXT,
                    available INTEGER NOT NULL DEFAULT 1, hidden INTEGER NOT NULL DEFAULT 0,
                    updated TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(server_id, object_id)
                );
                CREATE INDEX IF NOT EXISTS media_group ON media(server_id, available, year, album);
                CREATE TABLE IF NOT EXISTS hidden_groups (  -- album '' hides the whole year
                    server_id TEXT NOT NULL, year TEXT NOT NULL, album TEXT NOT NULL,
                    PRIMARY KEY(server_id, year, album)
                );
            """)
            if "hidden" not in {r["name"] for r in db.execute("PRAGMA table_info(media)")}:
                db.execute("ALTER TABLE media ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def device(self, device):
        with self.connect() as db:
            db.execute("""INSERT INTO devices(id,name,location,kind,model) VALUES(:id,:name,:location,:kind,:model)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,location=excluded.location,
                kind=excluded.kind,model=excluded.model,seen=CURRENT_TIMESTAMP""", device)

    def devices(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM devices ORDER BY name")]

    def get_device(self, id):
        return next((d for d in self.devices() if d["id"] == id), None)

    def upsert(self, server_id, item):
        return self.sync(server_id, item)["id"]

    def sync(self, server_id, item):
        """Store an item; unchanged, visible rows are not rewritten. state: new/updated/unchanged."""
        id = digest(server_id + "\0" + item["object_id"])
        resources = json.dumps(item["resources"], ensure_ascii=False, sort_keys=True)
        fingerprint = digest(resources + (item.get("date") or ""))
        values = (item["title"], item["album"], year_for(item["album"], item.get("date"), item["title"]),
                  item.get("date"), resources, item["metadata"], fingerprint)
        with self.connect() as db:
            old = db.execute("""SELECT source_title,album,year,date,resources,metadata,fingerprint,thumbnail,available
                FROM media WHERE id=?""", (id,)).fetchone()
            if old and old["available"] and tuple(old)[:7] == values:
                return {"id": id, "state": "unchanged", "fingerprint": fingerprint, "thumbnail": old["thumbnail"]}
            db.execute("""INSERT INTO media(id,server_id,object_id,source_title,album,year,date,resources,metadata,fingerprint)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                source_title=excluded.source_title,album=excluded.album,year=excluded.year,date=excluded.date,
                resources=excluded.resources,metadata=excluded.metadata,
                thumbnail=CASE WHEN media.fingerprint=excluded.fingerprint THEN media.thumbnail ELSE NULL END,
                thumb_error=NULL, fingerprint=excluded.fingerprint,available=1,updated=CURRENT_TIMESTAMP""",
                (id, server_id, item["object_id"], *values))
        thumbnail = old["thumbnail"] if old and old["fingerprint"] == fingerprint else None
        return {"id": id, "state": "updated" if old else "new", "fingerprint": fingerprint, "thumbnail": thumbnail}

    def finish_scan(self, server_id, ids):
        # Only called after the entire tree succeeds. Failed crawls never hide existing media.
        with self.connect() as db:
            db.execute("UPDATE media SET available=0 WHERE server_id=?", (server_id,))
            db.executemany("UPDATE media SET available=1 WHERE id=?", [(id,) for id in ids])

    def media(self, server_id=None, year=None, album=None, include_hidden=True):
        clauses, values = ["available=1"], []
        for key, value in (("server_id", server_id), ("year", year), ("album", album)):
            if value is not None:
                clauses.append(key + "=?")
                values.append(value)
        having = "" if include_hidden else " AND NOT hidden_effective"
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM ({SELECT}) WHERE " + " AND ".join(clauses) + having +
                              " ORDER BY year,album,source_title,id", values).fetchall()
        return [self.decode(r) for r in rows]

    def decode(self, row):
        item = dict(row)
        item["resources"] = json.loads(item["resources"])
        item["title"] = item["custom_title"] or display_title(item["source_title"])
        item["hidden_effective"] = bool(item["hidden_effective"])
        return item

    def get(self, id):
        with self.connect() as db:
            row = db.execute(SELECT + " WHERE id=?", (id,)).fetchone()
        return self.decode(row) if row else None

    def hide(self, id, value):
        with self.connect() as db:
            db.execute("UPDATE media SET hidden=? WHERE id=?", (int(value), id))

    def hide_group(self, server_id, year, album, value):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO hidden_groups VALUES(?,?,?)" if value else
                       "DELETE FROM hidden_groups WHERE server_id=? AND year=? AND album=?", (server_id, year, album))

    def hidden_groups(self, server_id):
        with self.connect() as db:
            return [{"year": r["year"], "album": r["album"]} for r in db.execute(
                "SELECT year,album FROM hidden_groups WHERE server_id=? ORDER BY year,album", (server_id,))]

    def title(self, id, value):
        with self.connect() as db:
            db.execute("UPDATE media SET custom_title=? WHERE id=?", (value.strip() or None, id))

    def thumbnail(self, id, fingerprint, path=None, error=None):
        with self.connect() as db:
            db.execute("UPDATE media SET thumbnail=?,thumb_error=? WHERE id=? AND fingerprint=?",
                       (path, error, id, fingerprint))
