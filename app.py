"""
公園マップ バックエンド (FastAPI + PostgreSQL / SQLite fallback)
DATABASE_URL 未設定時は parks.db (SQLite) を使用
起動: uvicorn app:app --reload --port 8502
"""

import os
import sqlite3
import threading
import subprocess
import json as _json
from contextlib import contextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

app = FastAPI(title="公園マップ API")

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_SQLITE   = DATABASE_URL is None
SQLITE_PATH  = os.environ.get("DB_PATH",
               os.path.join(os.path.dirname(os.path.abspath(__file__)), "parks.db"))

if not USE_SQLITE:
    import psycopg2
    import psycopg2.extras

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
TOKYO_BBOX   = (35.50, 139.40, 35.90, 139.95)


# ── DB ヘルパー ───────────────────────────────────────────────────────────────

def _q(sql: str) -> str:
    if USE_SQLITE:
        return sql.replace('%s', '?').replace('::numeric', '')
    return sql


@contextmanager
def get_db():
    if USE_SQLITE:
        conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _fetchall(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetchone(cur):
    cols = [d[0] for d in cur.description]
    row  = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        if USE_SQLITE:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS parks (
                    id         INTEGER PRIMARY KEY,
                    osm_id     TEXT UNIQUE,
                    lat        REAL NOT NULL,
                    lon        REAL NOT NULL,
                    name       TEXT,
                    operator   TEXT,
                    park_type  TEXT DEFAULT 'playground',
                    source     TEXT DEFAULT 'osm'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS park_photos (
                    id         INTEGER PRIMARY KEY,
                    park_id    INTEGER NOT NULL REFERENCES parks(id),
                    photo_url  TEXT NOT NULL,
                    caption    TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS page_views (
                    id         INTEGER PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS parks (
                    id         SERIAL PRIMARY KEY,
                    osm_id     TEXT UNIQUE,
                    lat        REAL NOT NULL,
                    lon        REAL NOT NULL,
                    name       TEXT,
                    operator   TEXT,
                    park_type  TEXT DEFAULT 'playground',
                    source     TEXT DEFAULT 'osm'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS park_photos (
                    id         SERIAL PRIMARY KEY,
                    park_id    INTEGER NOT NULL REFERENCES parks(id),
                    photo_url  TEXT NOT NULL,
                    caption    TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS page_views (
                    id         SERIAL PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_parks_latlon ON parks(lat, lon)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_photos_park  ON park_photos(park_id)")


# ── OSM データ取得 ────────────────────────────────────────────────────────────

def fetch_osm_parks():
    s, w, n, e = TOKYO_BBOX
    query = f"""
    [out:json][timeout:60][maxsize:50000000];
    (
      node["leisure"="playground"]({s},{w},{n},{e});
      way["leisure"="playground"]({s},{w},{n},{e});
      node["leisure"="park"]({s},{w},{n},{e});
      way["leisure"="park"]({s},{w},{n},{e});
    );
    out center tags;
    """
    try:
        result = subprocess.run(
            ['curl', '-s', '-m', '90', '-X', 'POST', OVERPASS_URL,
             '--data-urlencode', f'data={query}'],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode('utf-8', errors='replace'))
        elements = _json.loads(result.stdout.decode('utf-8')).get("elements", [])

        inserted = 0
        with get_db() as conn:
            cur = conn.cursor()
            for el in elements:
                lat = el.get("lat") or (el.get("center") or {}).get("lat")
                lon = el.get("lon") or (el.get("center") or {}).get("lon")
                if lat is None or lon is None:
                    continue
                tags = el.get("tags", {})
                ptype = tags.get("leisure", "playground")
                name  = tags.get("name") or (
                    "遊び場" if ptype == "playground" else "公園"
                )
                cur.execute(
                    _q("""INSERT INTO parks (osm_id, lat, lon, name, operator, park_type)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (osm_id) DO NOTHING"""),
                    (str(el["id"]), lat, lon, name,
                     tags.get("operator"), ptype),
                )
                inserted += cur.rowcount
        print(f"[OSM] {len(elements)} 件取得 / {inserted} 件新規登録")
    except Exception as exc:
        print(f"[OSM] データ取得失敗: {exc}")


# ── 起動時処理 ────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    db_type = "SQLite" if USE_SQLITE else "PostgreSQL"
    print(f"[DB] 使用DB: {db_type}")
    init_db()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM parks")
        count = cur.fetchone()[0]
    if count == 0:
        print("[OSM] 公園データを初回取得中（バックグラウンド）...")
        threading.Thread(target=fetch_osm_parks, daemon=True).start()
    else:
        print(f"[DB] 公園 {count} 件が登録済みです")


# ── モデル ────────────────────────────────────────────────────────────────────

class PhotoIn(BaseModel):
    photo_url: str  = Field(..., max_length=2048)
    caption:   str | None = Field(None, max_length=100)

class ParkIn(BaseModel):
    lat:       float    = Field(..., ge=35.0, le=36.0)
    lon:       float    = Field(..., ge=138.0, le=141.0)
    name:      str | None = Field(None, max_length=60)
    park_type: str | None = Field(None)


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/parks")
def list_parks(
    min_lat: Optional[float] = Query(None),
    max_lat: Optional[float] = Query(None),
    min_lon: Optional[float] = Query(None),
    max_lon: Optional[float] = Query(None),
    limit:   int             = Query(800, le=1500),
):
    with get_db() as conn:
        cur = conn.cursor()
        if all(v is not None for v in [min_lat, max_lat, min_lon, max_lon]):
            cur.execute(_q("""
                SELECT p.id, p.lat, p.lon,
                       COALESCE(p.name,'公園') AS name,
                       p.park_type,
                       COUNT(ph.id) AS photo_count
                FROM parks p
                LEFT JOIN park_photos ph ON ph.park_id = p.id
                WHERE p.lat BETWEEN %s AND %s AND p.lon BETWEEN %s AND %s
                GROUP BY p.id LIMIT %s
            """), (min_lat, max_lat, min_lon, max_lon, limit))
        else:
            cur.execute(_q("""
                SELECT p.id, p.lat, p.lon,
                       COALESCE(p.name,'公園') AS name,
                       p.park_type,
                       COUNT(ph.id) AS photo_count
                FROM parks p
                LEFT JOIN park_photos ph ON ph.park_id = p.id
                GROUP BY p.id LIMIT %s
            """), (limit,))
        return _fetchall(cur)


@app.get("/api/parks/{park_id}")
def get_park(park_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_q("""
            SELECT p.id, p.osm_id, p.lat, p.lon,
                   COALESCE(p.name,'公園') AS name,
                   p.operator, p.park_type, p.source,
                   COUNT(ph.id) AS photo_count
            FROM parks p
            LEFT JOIN park_photos ph ON ph.park_id = p.id
            WHERE p.id = %s GROUP BY p.id
        """), (park_id,))
        park = _fetchone(cur)
        if not park:
            raise HTTPException(status_code=404, detail="Not found")
        cur.execute(_q("""
            SELECT id, photo_url, caption, created_at
            FROM park_photos WHERE park_id = %s
            ORDER BY created_at DESC
        """), (park_id,))
        park["photos"] = _fetchall(cur)
    return park


@app.post("/api/parks/{park_id}/photos", status_code=201)
def add_photo(park_id: int, body: PhotoIn):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_q("SELECT id FROM parks WHERE id=%s"), (park_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Park not found")
        cur.execute(
            _q("INSERT INTO park_photos (park_id, photo_url, caption) VALUES (%s,%s,%s)"),
            (park_id, body.photo_url, body.caption),
        )
    return {"status": "ok"}


@app.post("/api/parks", status_code=201)
def add_park(body: ParkIn):
    with get_db() as conn:
        cur = conn.cursor()
        if USE_SQLITE:
            cur.execute(
                _q("INSERT INTO parks (lat, lon, name, park_type, source) VALUES (%s,%s,%s,%s,'user')"),
                (body.lat, body.lon, body.name, body.park_type or 'playground'),
            )
            new_id = cur.lastrowid
        else:
            cur.execute(
                "INSERT INTO parks (lat, lon, name, park_type, source) VALUES (%s,%s,%s,%s,'user') RETURNING id",
                (body.lat, body.lon, body.name, body.park_type or 'playground'),
            )
            new_id = cur.fetchone()[0]
    return {"id": new_id}


@app.post("/api/sync")
def sync_osm():
    threading.Thread(target=fetch_osm_parks, daemon=True).start()
    return {"status": "syncing"}


@app.post("/api/visit", status_code=201)
def record_visit():
    with get_db() as conn:
        cur = conn.cursor()
        if USE_SQLITE:
            cur.execute("INSERT INTO page_views (created_at) VALUES (CURRENT_TIMESTAMP)")
        else:
            cur.execute("INSERT INTO page_views (created_at) VALUES (NOW())")
    return {"status": "ok"}


@app.get("/api/stats")
def stats():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM parks")
        p = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM park_photos")
        ph = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM page_views")
        v = cur.fetchone()[0]
    return {"parks": p, "photos": ph, "visits": v}


@app.get("/api/backup")
def backup():
    from fastapi.responses import JSONResponse
    import json as _json_mod
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM parks")
        parks = _fetchall(cur)
        cur.execute("SELECT * FROM park_photos")
        photos = _fetchall(cur)
    content = _json_mod.dumps(
        {"parks": parks, "photos": photos},
        ensure_ascii=False, default=str, indent=2
    )
    return JSONResponse(
        content=_json_mod.loads(content),
        headers={"Content-Disposition": "attachment; filename=parkmap_backup.json"}
    )


# ── 静的ファイル ──────────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
