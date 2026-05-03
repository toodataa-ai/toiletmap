"""
トイレマップ バックエンド (FastAPI + PostgreSQL / SQLite fallback)
DATABASE_URL 未設定時は toilets.db (SQLite) を使用
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

app = FastAPI(title="トイレマップ API")

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_SQLITE   = DATABASE_URL is None
SQLITE_PATH  = os.environ.get("DB_PATH",
               os.path.join(os.path.dirname(os.path.abspath(__file__)), "toilets.db"))

if not USE_SQLITE:
    import psycopg2
    import psycopg2.extras

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
TOKYO_BBOX   = (35.50, 139.40, 35.90, 139.95)


# ── DB ヘルパー ───────────────────────────────────────────────────────────────

def _q(sql: str) -> str:
    """SQLite 用にプレースホルダー(%s→?)と型キャスト(::numeric)を変換"""
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
                CREATE TABLE IF NOT EXISTS toilets (
                    id            INTEGER PRIMARY KEY,
                    osm_id        TEXT UNIQUE,
                    lat           REAL NOT NULL,
                    lon           REAL NOT NULL,
                    name          TEXT,
                    operator      TEXT,
                    wheelchair    INTEGER DEFAULT 0,
                    source        TEXT DEFAULT 'osm',
                    facility_type TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ratings (
                    id          INTEGER PRIMARY KEY,
                    toilet_id   INTEGER NOT NULL REFERENCES toilets(id),
                    cleanliness INTEGER NOT NULL CHECK(cleanliness BETWEEN 1 AND 5),
                    crowdedness INTEGER NOT NULL CHECK(crowdedness IN (1, 2, 3)),
                    comment     TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # 既存 DB に facility_type がない場合のマイグレーション
            try:
                cur.execute("ALTER TABLE toilets ADD COLUMN facility_type TEXT")
            except Exception:
                pass
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS toilets (
                    id            SERIAL PRIMARY KEY,
                    osm_id        TEXT UNIQUE,
                    lat           REAL NOT NULL,
                    lon           REAL NOT NULL,
                    name          TEXT,
                    operator      TEXT,
                    wheelchair    INTEGER DEFAULT 0,
                    source        TEXT DEFAULT 'osm',
                    facility_type TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ratings (
                    id          SERIAL PRIMARY KEY,
                    toilet_id   INTEGER NOT NULL REFERENCES toilets(id),
                    cleanliness INTEGER NOT NULL CHECK(cleanliness BETWEEN 1 AND 5),
                    crowdedness INTEGER NOT NULL CHECK(crowdedness IN (1, 2, 3)),
                    comment     TEXT,
                    created_at  TIMESTAMP DEFAULT NOW()
                )
            """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ratings_toilet ON ratings(toilet_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_toilets_latlon  ON toilets(lat, lon)")


# ── OSM データ取得 ────────────────────────────────────────────────────────────

def fetch_osm_toilets():
    s, w, n, e = TOKYO_BBOX
    query = f"""
    [out:json][timeout:40][maxsize:30000000];
    (
      node["amenity"="toilets"]({s},{w},{n},{e});
      way["amenity"="toilets"]({s},{w},{n},{e});
    );
    out center tags;
    """
    try:
        result = subprocess.run(
            ['curl', '-s', '-m', '60', '-X', 'POST', OVERPASS_URL,
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
                cur.execute(
                    _q("""INSERT INTO toilets (osm_id, lat, lon, name, operator, wheelchair)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (osm_id) DO NOTHING"""),
                    (str(el["id"]), lat, lon,
                     tags.get("name"), tags.get("operator"),
                     1 if tags.get("wheelchair") == "yes" else 0),
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
        cur.execute("SELECT COUNT(*) FROM toilets")
        count = cur.fetchone()[0]
    if count == 0:
        print("[OSM] トイレデータを初回取得中（バックグラウンド）...")
        threading.Thread(target=fetch_osm_toilets, daemon=True).start()
    else:
        print(f"[DB] トイレ {count} 件が登録済みです")


# ── モデル ────────────────────────────────────────────────────────────────────

class RatingIn(BaseModel):
    cleanliness: int      = Field(..., ge=1, le=5)
    crowdedness: int      = Field(..., ge=1, le=3)
    comment:     str|None = Field(None, max_length=300)

class ToiletIn(BaseModel):
    lat:           float    = Field(..., ge=35.0, le=36.0)
    lon:           float    = Field(..., ge=138.0, le=141.0)
    name:          str|None = Field(None, max_length=50)
    facility_type: str|None = Field(None)


# ── API エンドポイント ─────────────────────────────────────────────────────────

@app.get("/api/toilets")
def list_toilets(
    min_lat: Optional[float] = Query(None),
    max_lat: Optional[float] = Query(None),
    min_lon: Optional[float] = Query(None),
    max_lon: Optional[float] = Query(None),
    limit:   int             = Query(600, le=1000),
):
    with get_db() as conn:
        cur = conn.cursor()
        if all(v is not None for v in [min_lat, max_lat, min_lon, max_lon]):
            cur.execute(_q("""
                SELECT t.id, t.lat, t.lon,
                    COALESCE(t.name,'公衆トイレ') AS name,
                    t.wheelchair, t.facility_type,
                    COUNT(r.id)                            AS rating_count,
                    ROUND(AVG(r.cleanliness)::numeric, 1)  AS avg_clean,
                    ROUND(AVG(r.crowdedness)::numeric, 1)  AS avg_crowd
                FROM toilets t
                LEFT JOIN ratings r ON r.toilet_id = t.id
                WHERE t.lat BETWEEN %s AND %s AND t.lon BETWEEN %s AND %s
                GROUP BY t.id LIMIT %s
            """), (min_lat, max_lat, min_lon, max_lon, limit))
        else:
            cur.execute(_q("""
                SELECT t.id, t.lat, t.lon,
                    COALESCE(t.name,'公衆トイレ') AS name,
                    t.wheelchair, t.facility_type,
                    COUNT(r.id)                            AS rating_count,
                    ROUND(AVG(r.cleanliness)::numeric, 1)  AS avg_clean,
                    ROUND(AVG(r.crowdedness)::numeric, 1)  AS avg_crowd
                FROM toilets t
                LEFT JOIN ratings r ON r.toilet_id = t.id
                GROUP BY t.id LIMIT %s
            """), (limit,))
        return _fetchall(cur)


@app.get("/api/toilets/{toilet_id}")
def get_toilet(toilet_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_q("""
            SELECT t.id, t.osm_id, t.lat, t.lon,
                COALESCE(t.name,'公衆トイレ') AS name,
                t.operator, t.wheelchair, t.facility_type,
                COUNT(r.id)                            AS rating_count,
                ROUND(AVG(r.cleanliness)::numeric, 1)  AS avg_clean,
                ROUND(AVG(r.crowdedness)::numeric, 1)  AS avg_crowd
            FROM toilets t
            LEFT JOIN ratings r ON r.toilet_id = t.id
            WHERE t.id = %s GROUP BY t.id
        """), (toilet_id,))
        t = _fetchone(cur)
        if not t:
            raise HTTPException(status_code=404, detail="Not found")
        cur.execute(_q("""
            SELECT cleanliness, crowdedness, comment, created_at
            FROM ratings WHERE toilet_id = %s
            ORDER BY created_at DESC LIMIT 5
        """), (toilet_id,))
        t["recent_ratings"] = _fetchall(cur)
    return t


@app.post("/api/toilets/{toilet_id}/ratings", status_code=201)
def post_rating(toilet_id: int, body: RatingIn):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_q("SELECT id FROM toilets WHERE id=%s"), (toilet_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Toilet not found")
        cur.execute(
            _q("INSERT INTO ratings (toilet_id, cleanliness, crowdedness, comment) VALUES (%s,%s,%s,%s)"),
            (toilet_id, body.cleanliness, body.crowdedness, body.comment),
        )
    return {"status": "ok"}


@app.post("/api/toilets", status_code=201)
def add_toilet(body: ToiletIn):
    with get_db() as conn:
        cur = conn.cursor()
        if USE_SQLITE:
            cur.execute(
                _q("""INSERT INTO toilets (lat, lon, name, source, facility_type)
                   VALUES (%s, %s, %s, 'user', %s)"""),
                (body.lat, body.lon, body.name, body.facility_type),
            )
            new_id = cur.lastrowid
        else:
            cur.execute(
                """INSERT INTO toilets (lat, lon, name, source, facility_type)
                   VALUES (%s, %s, %s, 'user', %s) RETURNING id""",
                (body.lat, body.lon, body.name, body.facility_type),
            )
            new_id = cur.fetchone()[0]
    return {"id": new_id}


@app.post("/api/sync")
def sync_osm():
    threading.Thread(target=fetch_osm_toilets, daemon=True).start()
    return {"status": "syncing"}


@app.get("/api/stats")
def stats():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM toilets")
        t = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ratings")
        r = cur.fetchone()[0]
    return {"toilets": t, "ratings": r}


# ── 静的ファイル ──────────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
