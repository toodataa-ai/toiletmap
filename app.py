"""
トイレマップ バックエンド (FastAPI + SQLite)
起動: uvicorn app:app --reload --port 8501
"""

import os
import threading
import subprocess
from contextlib import contextmanager
import sqlite3

from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

app = FastAPI(title="トイレマップ API")

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "toilets.db"))

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# 東京都の大まかなバウンディングボックス
TOKYO_BBOX = (35.50, 139.40, 35.90, 139.95)


# ── データベース ──────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def migrate_db():
    """既存 DB に列を追加（冪等）"""
    with get_db() as conn:
        for col, defn in [('source', "TEXT DEFAULT 'osm'"), ('facility_type', 'TEXT')]:
            try:
                conn.execute(f"ALTER TABLE toilets ADD COLUMN {col} {defn}")
            except Exception:
                pass


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS toilets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                osm_id     TEXT UNIQUE,
                lat        REAL NOT NULL,
                lon        REAL NOT NULL,
                name       TEXT,
                operator   TEXT,
                wheelchair INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS ratings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                toilet_id   INTEGER NOT NULL,
                cleanliness INTEGER NOT NULL CHECK(cleanliness BETWEEN 1 AND 5),
                crowdedness INTEGER NOT NULL CHECK(crowdedness IN (1, 2, 3)),
                comment     TEXT,
                created_at  TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (toilet_id) REFERENCES toilets(id)
            );
            CREATE INDEX IF NOT EXISTS idx_ratings_toilet ON ratings(toilet_id);
        """)


def fetch_osm_toilets():
    """Overpass API から東京のトイレデータを取得して DB に保存"""
    s, w, n, e = TOKYO_BBOX[0], TOKYO_BBOX[1], TOKYO_BBOX[2], TOKYO_BBOX[3]
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
        import json as _json
        elements = _json.loads(result.stdout.decode('utf-8')).get("elements", [])

        inserted = 0
        with get_db() as conn:
            for el in elements:
                lat = el.get("lat") or (el.get("center") or {}).get("lat")
                lon = el.get("lon") or (el.get("center") or {}).get("lon")
                if lat is None or lon is None:
                    continue
                tags = el.get("tags", {})
                cur = conn.execute(
                    """INSERT OR IGNORE INTO toilets
                       (osm_id, lat, lon, name, operator, wheelchair)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        str(el["id"]),
                        lat, lon,
                        tags.get("name"),
                        tags.get("operator"),
                        1 if tags.get("wheelchair") == "yes" else 0,
                    ),
                )
                inserted += cur.rowcount
        print(f"[OSM] {len(elements)} 件取得 / {inserted} 件新規登録")
    except Exception as exc:
        print(f"[OSM] データ取得失敗: {exc}")


# ── 起動時処理 ────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()
    migrate_db()
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM toilets").fetchone()[0]
    if count == 0:
        print("[OSM] トイレデータを初回取得中（バックグラウンド）...")
        threading.Thread(target=fetch_osm_toilets, daemon=True).start()
    else:
        print(f"[DB] トイレ {count} 件が登録済みです")


# ── リクエスト／レスポンスモデル ──────────────────────────────────────────────

class RatingIn(BaseModel):
    cleanliness: int = Field(..., ge=1, le=5, description="清潔さ 1-5")
    crowdedness: int = Field(..., ge=1, le=3, description="混雑 1=空/2=普/3=混")
    comment: str | None = Field(None, max_length=300)


class ToiletIn(BaseModel):
    lat: float = Field(..., ge=35.0, le=36.0)
    lon: float = Field(..., ge=138.0, le=141.0)
    name: str | None = Field(None, max_length=50)
    facility_type: str | None = Field(None)


# ── API エンドポイント ─────────────────────────────────────────────────────────

@app.get("/api/toilets")
def list_toilets(
    min_lat: Optional[float] = Query(None),
    max_lat: Optional[float] = Query(None),
    min_lon: Optional[float] = Query(None),
    max_lon: Optional[float] = Query(None),
    limit: int = Query(600, le=1000),
):
    """ビューポート内のトイレ一覧（位置情報＋評価集計）"""
    with get_db() as conn:
        if all(v is not None for v in [min_lat, max_lat, min_lon, max_lon]):
            where  = "WHERE t.lat BETWEEN ? AND ? AND t.lon BETWEEN ? AND ?"
            params = (min_lat, max_lat, min_lon, max_lon)
        else:
            where  = ""
            params = ()
        rows = conn.execute(f"""
            SELECT
                t.id, t.lat, t.lon,
                COALESCE(t.name, '公衆トイレ') AS name,
                t.wheelchair, t.facility_type,
                COUNT(r.id)                  AS rating_count,
                ROUND(AVG(r.cleanliness), 1) AS avg_clean,
                ROUND(AVG(r.crowdedness), 1) AS avg_crowd
            FROM toilets t
            LEFT JOIN ratings r ON r.toilet_id = t.id
            {where}
            GROUP BY t.id
            LIMIT ?
        """, (*params, limit)).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/toilets", status_code=201)
def add_toilet(body: ToiletIn):
    """ユーザーがトイレを新規追加"""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO toilets (lat, lon, name, source, facility_type) VALUES (?, ?, ?, 'user', ?)",
            (body.lat, body.lon, body.name, body.facility_type),
        )
    return {"id": cur.lastrowid}


@app.get("/api/toilets/{toilet_id}")
def get_toilet(toilet_id: int):
    """トイレ詳細＋最近の投稿5件"""
    with get_db() as conn:
        t = conn.execute("""
            SELECT
                t.id, t.osm_id, t.lat, t.lon,
                COALESCE(t.name, '公衆トイレ') AS name,
                t.operator, t.wheelchair,
                COUNT(r.id)                  AS rating_count,
                ROUND(AVG(r.cleanliness), 1) AS avg_clean,
                ROUND(AVG(r.crowdedness), 1) AS avg_crowd
            FROM toilets t
            LEFT JOIN ratings r ON r.toilet_id = t.id
            WHERE t.id = ?
            GROUP BY t.id
        """, (toilet_id,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Not found")

        recent = conn.execute("""
            SELECT cleanliness, crowdedness, comment, created_at
            FROM ratings
            WHERE toilet_id = ?
            ORDER BY created_at DESC
            LIMIT 5
        """, (toilet_id,)).fetchall()

    data = dict(t)
    data["recent_ratings"] = [dict(r) for r in recent]
    return data


@app.post("/api/toilets/{toilet_id}/ratings", status_code=201)
def post_rating(toilet_id: int, body: RatingIn):
    """評価を投稿"""
    with get_db() as conn:
        if not conn.execute("SELECT id FROM toilets WHERE id=?", (toilet_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Toilet not found")
        conn.execute(
            """INSERT INTO ratings (toilet_id, cleanliness, crowdedness, comment)
               VALUES (?, ?, ?, ?)""",
            (toilet_id, body.cleanliness, body.crowdedness, body.comment),
        )
    return {"status": "ok"}


@app.post("/api/sync")
def sync_osm():
    """OSM データを再取得（管理者用）"""
    threading.Thread(target=fetch_osm_toilets, daemon=True).start()
    return {"status": "syncing"}


@app.get("/api/stats")
def stats():
    """簡易統計"""
    with get_db() as conn:
        t_count = conn.execute("SELECT COUNT(*) FROM toilets").fetchone()[0]
        r_count = conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0]
    return {"toilets": t_count, "ratings": r_count}


# ── 静的ファイル（最後に配置） ────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
