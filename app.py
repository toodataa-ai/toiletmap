"""
公園マップ バックエンド (FastAPI + PostgreSQL / SQLite fallback)
DATABASE_URL 未設定時は parks.db (SQLite) を使用
起動: uvicorn app:app --reload --port 8502
"""

import os
import re
import sqlite3
import threading
import subprocess
import time
import json as _json
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Optional
from xml.etree import ElementTree

import requests as _requests
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
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

KOENTANBO_BASE     = "https://www.koentanbo.com"
KOENTANBO_UA       = "parkmap-bot/1.0"
SHINJUKU_BASE      = "https://www.city.shinjuku.lg.jp"
SHINJUKU_URL       = f"{SHINJUKU_BASE}/seikatsu/file15_03_00020.html"
SUGINAMI_URL       = "https://www.city.suginami.tokyo.jp/s100/1621.html"
NERIMA_URL         = "https://www.city.nerima.tokyo.jp/kankomoyoshi/annai/fukei/nerima_park/kunai/mizusisetu.html"
TORITSU_URL        = "https://www.kensetsu.metro.tokyo.lg.jp/park/kouenannai/mizu"
MINATO_URL         = "https://www.city.minato.tokyo.jp/shiba-koudobokutan/tosyouike.html"
MINATO_BASE        = "https://www.city.minato.tokyo.jp"
OTA_URL        = "https://www.city.ota.tokyo.jp/shisetsu/park/mizu-asobi.html"
SETAGAYA_URL   = "https://www.city.setagaya.lg.jp/02075/9197.html"
TAITO_URL      = "https://www.city.taito.lg.jp/kenchiku/hanamidori/koen/shokai/mizuasobi.html"
BUNKYO_URL     = "https://www.city.bunkyo.lg.jp/b036/p004823.html"
KITA_URL       = "https://www.city.kita.lg.jp/parks/list/1009530.html"
ARAKAWA_URL    = "https://www.city.arakawa.tokyo.jp/a043/koen/koen/mizuasobikouen.html"
ITABASHI_URL   = "https://www.city.itabashi.tokyo.jp/bousai/kouen/kouen/1006629.html"
ADACHI_URL     = "https://www.city.adachi.tokyo.jp/k-iji/2025jabu-jabu-ike.html"
KATSUSHIKA_URL = "https://www.city.katsushika.lg.jp/planning/1003408/1003556.html"
CHIYODA_URL  = "https://www.city.chiyoda.lg.jp/koho/machizukuri/koen/kodomonoike.html"
SUMIDA_URL   = "https://www.city.sumida.lg.jp/sisetu_info/kouen/riyou/mizushisetsu.html"
KOTO_URL     = "https://www.city.koto.lg.jp/470705/machizukuri/kasenkoen/annai/7483.html"
SHINAGAWA_URL = "https://www.city.shinagawa.tokyo.jp/PC/shisetsu/shisetsu-bunka/shisetsu-bunka-kouen/hpg000000346.html"
MEGURO_URL   = "https://www.city.meguro.tokyo.jp/douro/shisetsu/sports/nagare-unten.html"
SHIBUYA_URL  = "https://www.city.shibuya.tokyo.jp/shisetsu/koen/kuritsu-koen/shinsuishisetsu.html"
NAKANO_URL   = "https://www.city.tokyo-nakano.lg.jp/machizukuri/kouen/kouensiyou/jyabujyabuike.html"
EDOGAWA_URL  = "https://www.city.edogawa.tokyo.jp/e066/kuseijoho/gaiyo/shisetsuguide/bunya/koendobutsuen/jabjab.html"

# 都立公園22件の正確な座標（ジオコード失敗時のフォールバック）
# 新規公園がサイトに追加された場合はここにない→自動ジオコードへフォールバック
TORITSU_COORDS: dict[str, tuple[float, float]] = {
    "赤塚公園":         (35.78477, 139.65644),
    "秋留台公園":       (35.72889, 139.29417),
    "浮間公園":         (35.79486, 139.69286),
    "大泉中央公園":     (35.77554, 139.59701),
    "尾久の原公園":     (35.75150, 139.77690),
    "亀戸中央公園":     (35.70075, 139.83612),
    "木場公園":         (35.67056, 139.80944),
    "駒沢オリンピック公園": (35.64595, 139.65318),
    "猿江恩賜公園":     (35.69050, 139.81920),
    "汐入公園":         (35.73776, 139.81261),
    "舎人公園":         (35.77500, 139.80473),
    "戸山公園":         (35.69389, 139.70361),
    "野川公園":         (35.65056, 139.54083),
    "東村山中央公園":   (35.75465, 139.46858),
    "東大和南公園":     (35.74540, 139.42686),
    "光が丘公園":       (35.76243, 139.62890),
    "府中の森公園":     (35.66940, 139.47758),
    "水元公園":         (35.74333, 139.84723),
    "武蔵野公園":       (35.69944, 139.50305),
    "陵南公園":         (35.66667, 139.31584),
    "林試の森公園":     (35.64148, 139.69820),
    "井の頭自然文化園": (35.71778, 139.56612),
}
KOENTANBO_SITEMAPS = [
    f"{KOENTANBO_BASE}/post-sitemap.xml",
    f"{KOENTANBO_BASE}/post-sitemap2.xml",
    f"{KOENTANBO_BASE}/post-sitemap3.xml",
    f"{KOENTANBO_BASE}/post-sitemap4.xml",
]
# 関東広域バウンディングボックス
KANTO_BBOX = (34.5, 138.5, 37.0, 141.5)

_kb_status: dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "skipped": 0}
_sj_status: dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_sg_status: dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_nm_status: dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_tt_status: dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_mn_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_ot_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_sw_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_ti_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_bk_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_kt_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_ar_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_ib_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_ad_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_ks_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_cd_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_sm_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_ko_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_sn_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_mg_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_sb_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_nk_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
_eg_status:  dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "deleted": 0}

# 水遊び公園のソース一覧（/api/parks/water で使用）
WATER_SOURCES = (
    'shinjuku', 'suginami', 'nerima', 'minato', 'toritsu',
    'ota', 'setagaya', 'taito', 'bunkyo', 'kita', 'arakawa',
    'itabashi', 'adachi', 'katsushika',
    'chiyoda', 'sumida', 'koto', 'shinagawa', 'meguro', 'shibuya', 'nakano', 'edogawa',
)


# ── DB ヘルパー ───────────────────────────────────────────────────────────────

def _q(sql: str) -> str:
    if USE_SQLITE:
        return sql.replace('%s', '?').replace('::numeric', '')
    return sql


@contextmanager
def get_db():
    if USE_SQLITE:
        conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, timeout=30)
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
        # マイグレーション: カラムが未存在なら追加
        # PostgreSQL は ALTER TABLE 失敗でトランザクションがエラー状態になるため SAVEPOINT を使う
        for col_sql in [
            "ALTER TABLE parks ADD COLUMN last_fetched TIMESTAMP",
            "ALTER TABLE parks ADD COLUMN created_at TIMESTAMP",
            "ALTER TABLE park_photos ADD COLUMN photo_source TEXT",
            "ALTER TABLE parks ADD COLUMN description TEXT",
            "ALTER TABLE parks ADD COLUMN source_url TEXT",
            "ALTER TABLE parks ADD COLUMN address TEXT",
        ]:
            if USE_SQLITE:
                try:
                    cur.execute(col_sql)
                except Exception:
                    pass
            else:
                cur.execute("SAVEPOINT _mig")
                try:
                    cur.execute(col_sql)
                    cur.execute("RELEASE SAVEPOINT _mig")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT _mig")


# ── OSM データ取得 ────────────────────────────────────────────────────────────

def fetch_osm_parks():
    s, w, n, e = TOKYO_BBOX
    query = f"""
    [out:json][timeout:90];
    (
      node["leisure"="playground"]({s},{w},{n},{e});
      way["leisure"="playground"]({s},{w},{n},{e});
      node["leisure"="park"]({s},{w},{n},{e});
      way["leisure"="park"]({s},{w},{n},{e});
    );
    out center tags;
    """
    try:
        resp = _requests.post(
            OVERPASS_URL,
            data={"data": query},
            timeout=120,
            headers={"User-Agent": KOENTANBO_UA},
        )
        resp.raise_for_status()
        elements = resp.json().get("elements", [])

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

_scheduler = BackgroundScheduler(timezone="Asia/Tokyo")

def _scheduled_sync():
    if not _kb_status["running"]:
        print("[scheduler] 定期同期開始")
        threading.Thread(target=fetch_koentanbo_parks, daemon=True).start()

@app.on_event("startup")
def startup():
    db_type = "SQLite" if USE_SQLITE else "PostgreSQL"
    print(f"[DB] 使用DB: {db_type}")
    init_db()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM parks")
        count = cur.fetchone()[0]
    print(f"[DB] 公園 {count} 件が登録済みです")
    _scheduler.add_job(_scheduled_sync, 'cron', hour=2, minute=0, id='koentanbo_daily')
    _scheduler.start()
    print("[scheduler] 起動完了 — 毎日 02:00 JST に自動同期")


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
                       p.source,
                       COUNT(ph.id) AS photo_count,
                       p.created_at
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
                       p.source,
                       COUNT(ph.id) AS photo_count,
                       p.created_at
                FROM parks p
                LEFT JOIN park_photos ph ON ph.park_id = p.id
                GROUP BY p.id LIMIT %s
            """), (limit,))
        return _fetchall(cur)


@app.get("/api/parks/{park_id:int}")
def get_park(park_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_q("""
            SELECT p.id, p.osm_id, p.lat, p.lon,
                   COALESCE(p.name,'公園') AS name,
                   p.operator, p.park_type, p.source, p.description, p.source_url,
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


@app.get("/api/parks/water")
def list_water_parks():
    """水遊びができる公園の一覧を返す。"""
    placeholders = ",".join(["%s"] * len(WATER_SOURCES))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_q(f"""
            SELECT p.id, p.lat, p.lon,
                   COALESCE(p.name,'公園') AS name,
                   p.source, p.description, p.source_url
            FROM parks p
            WHERE p.source IN ({placeholders})
            ORDER BY p.source, p.name
        """), WATER_SOURCES)
        return _fetchall(cur)


@app.get("/api/parks/search")
def search_parks(q: str = Query("", min_length=1), limit: int = Query(20, le=50)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(_q("""
            SELECT p.id, p.lat, p.lon,
                   COALESCE(p.name,'公園') AS name,
                   p.park_type,
                   p.source,
                   COUNT(ph.id) AS photo_count,
                   p.created_at
            FROM parks p
            LEFT JOIN park_photos ph ON ph.park_id = p.id
            WHERE p.name LIKE %s
            GROUP BY p.id
            ORDER BY p.name LIMIT %s
        """), (f"%{q}%", limit))
        return _fetchall(cur)


@app.get("/api/parks/list")
def parks_list(
    source: str = Query("water"),
    limit: int = Query(500, le=2000),
):
    """一覧ページ用: 水遊び公園またはkoentanbo公園（写真あり）を返す。"""
    with get_db() as conn:
        cur = conn.cursor()
        if source == "water":
            placeholders = ",".join(["%s"] * len(WATER_SOURCES))
            cur.execute(_q(f"""
                SELECT p.id, p.lat, p.lon,
                       COALESCE(p.name,'公園') AS name,
                       p.source, p.description, p.source_url, p.address,
                       (SELECT ph.photo_url FROM park_photos ph
                        WHERE ph.park_id = p.id ORDER BY ph.id LIMIT 1) AS photo_url
                FROM parks p
                WHERE p.source IN ({placeholders})
                ORDER BY p.source, p.name
                LIMIT %s
            """), (*WATER_SOURCES, limit))
        elif source == "koentanbo":
            cur.execute(_q("""
                SELECT p.id, p.lat, p.lon,
                       COALESCE(p.name,'公園') AS name,
                       p.source, p.description, p.source_url, p.address,
                       (SELECT ph.photo_url FROM park_photos ph
                        WHERE ph.park_id = p.id ORDER BY ph.id LIMIT 1) AS photo_url
                FROM parks p
                WHERE p.source = 'koentanbo'
                  AND EXISTS (SELECT 1 FROM park_photos ph WHERE ph.park_id = p.id)
                ORDER BY p.name
                LIMIT %s
            """), (limit,))
        else:
            raise HTTPException(status_code=400, detail="source は 'water' または 'koentanbo' を指定してください")
        return _fetchall(cur)


@app.post("/api/sync")
def sync_osm():
    threading.Thread(target=fetch_osm_parks, daemon=True).start()
    return {"status": "syncing"}


# ── 公園探訪郊外 スクレイピング ───────────────────────────────────────────────

def _parse_koentanbo_page(url: str) -> dict | None:
    try:
        r = _requests.get(url, timeout=15, headers={"User-Agent": KOENTANBO_UA})
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")

        # 公園名: <h2> タグから
        h2 = soup.find("h2")
        name = h2.get_text(strip=True) if h2 else None

        # 緯度経度: Google マップリンクの daddr= パラメータから
        lat = lon = None
        for a in soup.find_all("a", href=True):
            m = re.search(r"daddr=([\d.]+),\s*([\d.]+)", a["href"])
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
                break
        if lat is None:
            # フォールバック: iframe の ll= パラメータ
            iframe = soup.find("iframe", src=True)
            if iframe:
                m = re.search(r"ll=([\d.]+),\s*([\d.]+)", iframe["src"])
                if m:
                    lat, lon = float(m.group(1)), float(m.group(2))

        if lat is None or lon is None:
            return None

        # 写真: wp-image クラスを持つ img タグ
        photos = []
        seen = set()
        for img in soup.find_all("img"):
            cls = " ".join(img.get("class") or [])
            src = img.get("src", "")
            if "wp-image" in cls and "/wp-content/uploads/" in src:
                if src.startswith("/"):
                    src = KOENTANBO_BASE + src
                if src not in seen:
                    seen.add(src)
                    photos.append(src)

        return {"name": name, "lat": lat, "lon": lon, "photos": photos[:5]}
    except Exception as exc:
        print(f"[koentanbo] parse error {url}: {exc}")
        return None


def _process_koentanbo_url(url: str, is_update: bool = False) -> bool:
    """1ページをスクレイプしてDBに保存。
    is_update=True の場合は既存レコードを上書き（ユーザー投稿写真は保持）。
    新規登録 or 更新できたら True を返す。
    """
    data = _parse_koentanbo_page(url)
    if not data:
        return False
    lat, lon = data["lat"], data["lon"]
    S, W, N, E = KANTO_BBOX
    if not (S <= lat <= N and W <= lon <= E):
        return False

    slug   = url.rstrip("/").split("/")[-1]
    osm_id = f"koentanbo_{slug}"
    now    = "CURRENT_TIMESTAMP" if USE_SQLITE else "NOW()"
    try:
        with get_db() as conn:
            cur = conn.cursor()
            if is_update:
                # 基本情報を更新
                cur.execute(
                    _q(f"UPDATE parks SET lat=%s, lon=%s, name=%s, last_fetched={now}"
                       " WHERE osm_id=%s"),
                    (lat, lon, data["name"] or "公園", osm_id),
                )
                # koentanbo 由来の写真だけ差し替え（ユーザー投稿は残す）
                cur.execute(_q("SELECT id FROM parks WHERE osm_id=%s"), (osm_id,))
                row = cur.fetchone()
                if row:
                    park_id = row[0]
                    cur.execute(
                        _q("DELETE FROM park_photos WHERE park_id=%s AND photo_source='koentanbo'"),
                        (park_id,),
                    )
                    for photo_url in data["photos"][:3]:
                        cur.execute(
                            _q("INSERT INTO park_photos (park_id, photo_url, caption, photo_source)"
                               " VALUES (%s,%s,%s,'koentanbo')"),
                            (park_id, photo_url, None),
                        )
            else:
                cur.execute(
                    _q(f"""INSERT INTO parks (osm_id, lat, lon, name, park_type, source, last_fetched, created_at)
                          VALUES (%s,%s,%s,%s,'park','koentanbo',{now},{now})
                          ON CONFLICT (osm_id) DO NOTHING"""),
                    (osm_id, lat, lon, data["name"] or "公園"),
                )
                if cur.rowcount:
                    cur.execute(_q("SELECT id FROM parks WHERE osm_id=%s"), (osm_id,))
                    row = cur.fetchone()
                    if row:
                        park_id = row[0]
                        for photo_url in data["photos"][:3]:
                            cur.execute(
                                _q("INSERT INTO park_photos (park_id, photo_url, caption, photo_source)"
                                   " VALUES (%s,%s,%s,'koentanbo')"),
                                (park_id, photo_url, None),
                            )
                    return True
                return False
    except Exception as exc:
        print(f"[koentanbo] db error {url}: {exc}")
        return False
    return is_update  # UPDATE の場合は常に True


def fetch_koentanbo_parks():
    global _kb_status
    _kb_status = {"running": True, "total": 0, "done": 0, "inserted": 0, "skipped": 0}
    headers = {"User-Agent": KOENTANBO_UA}

    # サイトマップから (url, lastmod) を収集
    url_lastmod: dict[str, str] = {}
    for sm_url in KOENTANBO_SITEMAPS:
        try:
            r = _requests.get(sm_url, timeout=15, headers=headers)
            if r.status_code != 200:
                continue
            root = ElementTree.fromstring(r.content)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            for url_el in root.findall("sm:url", ns):
                loc = url_el.findtext("sm:loc", namespaces=ns) or ""
                loc = loc.strip()
                if (loc.startswith(KOENTANBO_BASE + "/")
                        and loc != KOENTANBO_BASE + "/"
                        and not re.search(r"/(list|ranking|about|tag|category|page)/", loc)):
                    slug = loc.rstrip("/").split("/")[-1]
                    if slug and len(slug) > 2:
                        lastmod = (url_el.findtext("sm:lastmod", namespaces=ns) or "").strip()
                        url_lastmod[loc] = lastmod
        except Exception as exc:
            print(f"[koentanbo] sitemap error {sm_url}: {exc}")

    print(f"[koentanbo] サイトマップから {len(url_lastmod)} 件取得")

    # DB の既存レコード (osm_id, last_fetched) を一括取得
    db_fetched: dict[str, str] = {}
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT osm_id, last_fetched FROM parks WHERE osm_id LIKE 'koentanbo_%'")
            for row in cur.fetchall():
                db_fetched[row[0]] = str(row[1] or "")
    except Exception as exc:
        print(f"[koentanbo] existing check error: {exc}")

    # 処理対象を分類：新規 / 更新あり / スキップ
    new_urls:    list[str] = []
    update_urls: list[str] = []
    for url, lastmod in url_lastmod.items():
        osm_id = f"koentanbo_{url.rstrip('/').split('/')[-1]}"
        if osm_id not in db_fetched:
            new_urls.append(url)
        elif lastmod and db_fetched[osm_id] and lastmod > db_fetched[osm_id][:10]:
            # サイトマップの lastmod がDB保存日より新しい
            update_urls.append(url)

    skipped = len(url_lastmod) - len(new_urls) - len(update_urls)
    _kb_status["skipped"] = skipped
    _kb_status["total"]   = len(new_urls) + len(update_urls)
    print(f"[koentanbo] 新規:{len(new_urls)} 更新:{len(update_urls)} スキップ:{skipped} — 8並列で処理開始")

    def run(url: str, is_update: bool) -> bool:
        return _process_koentanbo_url(url, is_update=is_update)

    tasks = [(u, False) for u in new_urls] + [(u, True) for u in update_urls]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(run, u, upd): u for u, upd in tasks}
        for fut in as_completed(futures):
            _kb_status["done"] += 1
            if fut.result():
                _kb_status["inserted"] += 1
            if _kb_status["done"] % 200 == 0:
                print(f"[koentanbo] {_kb_status['done']}/{_kb_status['total']} 件処理 "
                      f"({_kb_status['inserted']} 件登録/更新)")

    _kb_status["running"] = False
    print(f"[koentanbo] 完了: {_kb_status['inserted']} 件登録/更新")


@app.post("/api/sync/koentanbo")
def sync_koentanbo():
    if _kb_status["running"]:
        return {"status": "already_running", **_kb_status}
    threading.Thread(target=fetch_koentanbo_parks, daemon=True).start()
    return {"status": "started"}


@app.get("/api/sync/koentanbo/status")
def koentanbo_status():
    return _kb_status


# ── 新宿区公式 水遊び場 スクレイピング ──────────────────────────────────────────

def _delete_removed_parks(source: str, current_osm_ids: set, status: dict):
    """sourceのDBレコードのうちcurrent_osm_idsにないものを削除する。"""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(_q("SELECT id, osm_id, name FROM parks WHERE source=%s"), (source,))
            db_parks = cur.fetchall()
        to_delete = [(r[0], r[1], r[2]) for r in db_parks if r[1] not in current_osm_ids]
        if not to_delete:
            return
        with get_db() as conn:
            cur = conn.cursor()
            for pid, oid, pname in to_delete:
                cur.execute(_q("DELETE FROM park_photos WHERE park_id=%s"), (pid,))
                cur.execute(_q("DELETE FROM parks WHERE id=%s"), (pid,))
                print(f"[{source}] 削除: {pname} ({oid})")
                status["deleted"] += 1
    except Exception as exc:
        print(f"[{source}] delete error: {exc}")


def _extract_nominatim_addr(addr_dict: dict) -> str:
    """Nominatim addressdetails から日本語住所文字列を構築する。"""
    city         = addr_dict.get("city") or addr_dict.get("town") or addr_dict.get("county") or ""
    district     = addr_dict.get("city_district") or ""
    suburb       = addr_dict.get("suburb") or ""
    neighbourhood = addr_dict.get("neighbourhood") or addr_dict.get("quarter") or ""
    parts = [p for p in [city, district, suburb, neighbourhood] if p]
    if not parts:
        return ""
    addr = "".join(parts)
    return addr if addr.startswith("東京都") else ("東京都" + addr if "東京" in addr or "都" in city else addr)


def _best_addr(source_addr: str, geocoded_addr: str) -> str:
    """ソース住所とジオコード住所のうち、より具体的な方を返す。"""
    if not source_addr:
        return geocoded_addr or ""
    if not geocoded_addr:
        return source_addr
    return geocoded_addr if len(geocoded_addr) > len(source_addr) else source_addr


def _is_ward_only_addr(addr: str) -> bool:
    """住所が都・区・市レベルのみで町丁目がない場合 True（例: 東京都荒川区）。"""
    return bool(re.match(r'^東京都[\S]+[都区市]$', addr or ""))


def _geocode_nominatim(q: str) -> tuple | None:
    """Nominatim で検索。(lat, lon, addr_str) または None を返す。"""
    result = None
    try:
        resp = _requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 1, "countrycodes": "jp", "addressdetails": 1},
            headers={"User-Agent": KOENTANBO_UA},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
            addr_str = _extract_nominatim_addr(data[0].get("address", {}))
            result = (lat, lon, addr_str)
    except Exception as exc:
        print(f"[geocode/nominatim] error '{q}': {exc}")
    time.sleep(1.1)
    return result


def _geocode_gsi(address: str) -> tuple | None:
    """国土地理院 API で住所→(lat, lon, "") 変換。日本語住所に強く制限なし。"""
    try:
        resp = _requests.get(
            "https://msearch.gsi.go.jp/address-search/AddressSearch",
            params={"q": address},
            headers={"User-Agent": KOENTANBO_UA},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            lon, lat = data[0]["geometry"]["coordinates"]
            return (float(lat), float(lon), "")
    except Exception as exc:
        print(f"[geocode/gsi] error '{address}': {exc}")
    return None


def _geocode_park(name: str, address: str = "") -> tuple | None:
    """公園名優先で Nominatim → 公園名+住所Nominatim → 国土地理院 → 住所Nominatim の順で試みる。
    (lat, lon, geocoded_addr_str) を返す。geocoded_addr_str は Nominatim 由来の住所。"""
    # 1. 公園名のみで Nominatim
    result = _geocode_nominatim(name)
    if result:
        return result
    if not address:
        return None
    # 2. 公園名＋住所で Nominatim（名前単独で失敗した場合の補完）
    result = _geocode_nominatim(f"{name} {address}")
    if result:
        return result
    # 3. 住所で国土地理院（制限なし・日本語住所に強い）
    result = _geocode_gsi(address)
    if result:
        return result
    # 4. 住所で Nominatim（最終手段）
    return _geocode_nominatim(address)


def _parse_shinjuku_park_detail(url: str) -> list:
    """詳細ページから公園写真URLリストを返す（.jpgのみ、最大5件）。"""
    photos = []
    try:
        r = _requests.get(url, timeout=15, headers={"User-Agent": KOENTANBO_UA})
        if r.status_code != 200:
            return []
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        seen = set()
        for img in soup.find_all("img", src=True):
            src = img["src"]
            # .jpgのみ取得（サイトアイコン・ロゴは.pngなので除外できる）
            if "/content/" in src and re.search(r'\.jpe?g$', src, re.I):
                if src.startswith("/"):
                    src = SHINJUKU_BASE + src
                if src not in seen:
                    seen.add(src)
                    photos.append(src)
    except Exception as exc:
        print(f"[shinjuku] detail error {url}: {exc}")
    return photos[:5]


def fetch_shinjuku_parks():
    global _sj_status
    _sj_status = {"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
    headers = {"User-Agent": KOENTANBO_UA}
    try:
        r = _requests.get(SHINJUKU_URL, timeout=15, headers=headers)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")

        # 親水施設テーブルのみ取得（景観施設は除外）
        tables = soup.find_all("table", class_="bmsupport_table") or soup.find_all("table")
        if not tables:
            print("[shinjuku] テーブルが見つかりません")
            return
        shinsuii_table = tables[0]

        # rowspan対応パーサー
        parks_data = []
        current_park = None
        span_rem = {}  # col_idx -> remaining rows after current row

        tbody = shinsuii_table.find("tbody") or shinsuii_table
        for tr in tbody.find_all("tr", recursive=False):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue

            occupied = {c for c, rem in span_rem.items() if rem > 0}
            col_map = {}
            new_spans = {}
            col = 0
            for cell in cells:
                while col in occupied:
                    col += 1
                rs = int(cell.get("rowspan", 1))
                col_map[col] = cell
                if rs > 1:
                    new_spans[col] = rs - 1
                col += 1

            span_rem = {c: rem - 1 for c, rem in span_rem.items() if rem > 1}
            span_rem.update(new_spans)

            def txt(c, _cm=col_map):
                return _cm[c].get_text(" ", strip=True).strip() if c in _cm else ""

            col0 = col_map.get(0)
            if col0 and col0.find("a", href=True):
                if current_park:
                    parks_data.append(current_park)
                a = col0.find("a", href=True)
                name = a.get_text(strip=True)
                href = a["href"]
                desc_parts = []
                if txt(2): desc_parts.append(f"施設: {txt(2)}")
                if txt(3): desc_parts.append(f"時期: {txt(3)}")
                if txt(4): desc_parts.append(f"時間: {txt(4)}")
                if txt(5): desc_parts.append(f"補給水: {txt(5)}")
                if txt(6): desc_parts.append(f"消毒: {txt(6)}")
                current_park = {
                    "name": name, "href": href,
                    "address": txt(1),
                    "desc_parts": desc_parts,
                }
            elif current_park and col0 is None:
                # rowspan継続行（同じ公園の追加施設情報）
                extra = []
                if txt(2): extra.append(f"施設: {txt(2)}")
                if txt(3): extra.append(f"時期: {txt(3)}")
                if txt(4): extra.append(f"時間: {txt(4)}")
                if extra:
                    current_park["desc_parts"].append("")  # 空行
                    for item in extra:
                        current_park["desc_parts"].append(f"（追加）{item}")

        if current_park:
            parks_data.append(current_park)

        current_osm_ids = {
            f"shinjuku_{re.sub(r'[^a-z0-9_]', '_', p['href'].rstrip('/').split('/')[-1].lower())}"
            for p in parks_data
        }

        _sj_status["total"] = len(parks_data)
        print(f"[shinjuku] {len(parks_data)} 件発見（親水施設のみ）")

        now_expr = "CURRENT_TIMESTAMP" if USE_SQLITE else "NOW()"
        for park in parks_data:
            name        = park["name"]
            href        = park["href"]
            address     = park["address"]
            description = "\n".join(park["desc_parts"])
            source_url  = (SHINJUKU_BASE + href) if href.startswith("/") else href
            slug        = re.sub(r'[^a-z0-9_]', '_', href.rstrip("/").split("/")[-1].lower())
            osm_id      = f"shinjuku_{slug}"

            full_addr_sj = f"東京都新宿区{address}" if address else "東京都新宿区"
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(_q("SELECT id, lat, lon, address FROM parks WHERE osm_id=%s"), (osm_id,))
                existing = cur.fetchone()

            if existing:
                park_id, lat, lon, existing_addr = existing[0], existing[1], existing[2], existing[3]
                if not existing_addr:
                    result = _geocode_park(name, full_addr_sj)
                    if result:
                        lat, lon, geocoded_addr = result
                        better = _best_addr(full_addr_sj, geocoded_addr)
                        with get_db() as conn:
                            cur = conn.cursor()
                            cur.execute(
                                _q("UPDATE parks SET lat=%s, lon=%s, address=%s WHERE id=%s"),
                                (lat, lon, better, park_id),
                            )
                        print(f"[shinjuku] 住所更新: {name} → {better}")
            else:
                result = _geocode_park(name, full_addr_sj)
                if not result:
                    print(f"[shinjuku] geocode 失敗: {name} ({full_addr_sj})")
                    _sj_status["done"] += 1
                    continue
                lat, lon, geocoded_addr = result
                better = _best_addr(full_addr_sj, geocoded_addr)
                park_id = None

            detail_url = source_url
            photos = _parse_shinjuku_park_detail(detail_url)

            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    if park_id is None:
                        cur.execute(
                            _q(f"""INSERT INTO parks
                                   (osm_id, lat, lon, name, park_type, source, description, source_url, address, last_fetched, created_at)
                                   VALUES (%s,%s,%s,%s,'park','shinjuku',%s,%s,%s,{now_expr},{now_expr})
                                   ON CONFLICT (osm_id) DO NOTHING"""),
                            (osm_id, lat, lon, name, description, source_url, better),
                        )
                        if USE_SQLITE:
                            park_id = cur.lastrowid
                        else:
                            cur.execute(_q("SELECT id FROM parks WHERE osm_id=%s"), (osm_id,))
                            park_id = cur.fetchone()[0]
                        _sj_status["inserted"] += 1
                        print(f"[shinjuku] 登録: {name} ({lat:.5f},{lon:.5f}) 写真{len(photos)}枚")
                    else:
                        cur.execute(
                            _q("UPDATE parks SET description=%s, source_url=%s WHERE id=%s"),
                            (description, source_url, park_id),
                        )
                        print(f"[shinjuku] 更新: {name} 写真{len(photos)}枚")

                    cur.execute(
                        _q("DELETE FROM park_photos WHERE park_id=%s AND photo_source='shinjuku'"),
                        (park_id,),
                    )
                    for photo_url in photos:
                        cur.execute(
                            _q("INSERT INTO park_photos (park_id, photo_url, caption, photo_source)"
                               " VALUES (%s,%s,%s,'shinjuku')"),
                            (park_id, photo_url, None),
                        )
            except Exception as exc:
                print(f"[shinjuku] db error {name}: {exc}")

            _sj_status["done"] += 1

        _delete_removed_parks("shinjuku", current_osm_ids, _sj_status)

    except Exception as exc:
        print(f"[shinjuku] fetch error: {exc}")
    finally:
        _sj_status["running"] = False
        print(f"[shinjuku] 完了: {_sj_status['inserted']} 件登録 / {_sj_status['deleted']} 件削除")


@app.post("/api/sync/shinjuku")
def sync_shinjuku():
    if _sj_status["running"]:
        return {"status": "already_running", **_sj_status}
    threading.Thread(target=fetch_shinjuku_parks, daemon=True).start()
    return {"status": "started"}


@app.get("/api/sync/shinjuku/status")
def shinjuku_status():
    return _sj_status


# ── 杉並区公式 水遊び場 スクレイピング ──────────────────────────────────────────

def fetch_suginami_parks():
    global _sg_status
    _sg_status = {"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
    headers = {"User-Agent": KOENTANBO_UA}
    try:
        r = _requests.get(SUGINAMI_URL, timeout=15, headers=headers)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")

        parks_data = []
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if len(cells) < 3:
                continue
            facilities = cells[2].get_text(strip=True)
            if "流れ" not in facilities:
                continue
            name    = cells[0].get_text(strip=True)
            address = cells[1].get_text(strip=True)
            if not name:
                continue
            parks_data.append({"name": name, "address": address})

        # 今回取得した osm_id 一覧（削除判定用）
        current_osm_ids = {
            f"suginami_{re.sub(r'[\s/\\]', '_', p['name'])}"
            for p in parks_data
        }

        _sg_status["total"] = len(parks_data)
        print(f"[suginami] {len(parks_data)} 件発見（流れあり）")

        now_expr = "CURRENT_TIMESTAMP" if USE_SQLITE else "NOW()"
        for park in parks_data:
            name    = park["name"]
            address = park["address"]
            slug    = re.sub(r"[\s/\\]", "_", name)
            osm_id  = f"suginami_{slug}"

            full_addr = f"東京都{address}" if address else ""
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(_q("SELECT id, address FROM parks WHERE osm_id=%s"), (osm_id,))
                row = cur.fetchone()

            if row:
                existing_id, existing_addr = row
                if (not existing_addr or _is_ward_only_addr(existing_addr)) and full_addr:
                    result = _geocode_park(name, full_addr)
                    if result:
                        lat_r, lon_r, geocoded_addr = result
                        better = _best_addr(full_addr, geocoded_addr)
                        if better != existing_addr:
                            with get_db() as conn:
                                cur = conn.cursor()
                                cur.execute(
                                    _q("UPDATE parks SET lat=%s, lon=%s, address=%s WHERE id=%s"),
                                    (lat_r, lon_r, better, existing_id),
                                )
                            print(f"[suginami] 住所更新: {name} → {better}")
                _sg_status["done"] += 1
                continue

            result = _geocode_park(name, full_addr)
            if not result:
                print(f"[suginami] geocode 失敗: {name} ({full_addr})")
                _sg_status["done"] += 1
                continue
            lat, lon, geocoded_addr = result
            better = _best_addr(full_addr, geocoded_addr)

            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        _q(f"""INSERT INTO parks
                               (osm_id, lat, lon, name, park_type, source, address, last_fetched, created_at)
                               VALUES (%s,%s,%s,%s,'park','suginami',%s,{now_expr},{now_expr})
                               ON CONFLICT (osm_id) DO NOTHING"""),
                        (osm_id, lat, lon, name, better),
                    )
                    if cur.rowcount:
                        _sg_status["inserted"] += 1
                        print(f"[suginami] 登録: {name} ({lat:.5f},{lon:.5f})")
            except Exception as exc:
                print(f"[suginami] db error {name}: {exc}")

            _sg_status["done"] += 1

        # 元データから消えた公園を削除
        _delete_removed_parks("suginami", current_osm_ids, _sg_status)

    except Exception as exc:
        print(f"[suginami] fetch error: {exc}")
    finally:
        _sg_status["running"] = False
        print(f"[suginami] 完了: {_sg_status['inserted']} 件登録 / {_sg_status['deleted']} 件削除")


@app.post("/api/sync/suginami")
def sync_suginami():
    if _sg_status["running"]:
        return {"status": "already_running", **_sg_status}
    threading.Thread(target=fetch_suginami_parks, daemon=True).start()
    return {"status": "started"}


@app.get("/api/sync/suginami/status")
def suginami_status():
    return _sg_status


# ── 練馬区公式 水施設 スクレイピング ──────────────────────────────────────────

def fetch_nerima_parks():
    global _nm_status
    _nm_status = {"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
    headers = {"User-Agent": KOENTANBO_UA}
    try:
        r = _requests.get(NERIMA_URL, timeout=15, headers=headers)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")

        def _cell_facility(cell, col_name):
            t = cell.get_text(strip=True)
            if "○" in t or "〇" in t:
                return col_name
            if "注釈" in t:
                return "ミスト" if col_name == "噴水" else col_name
            return None

        parks_data = []
        seen = set()
        for table in soup.find_all("table"):
            tbody = table.find("tbody") or table
            for tr in tbody.find_all("tr"):
                cells = tr.find_all(["th", "td"])
                for offset in [0, 5]:
                    if len(cells) < offset + 5:
                        continue
                    name = cells[offset].get_text(strip=True)
                    # 注記付きセル（例: （注釈2）三原台公園…）はスキップ
                    if re.match(r'^（[注※]', name):
                        continue
                    if not name or name in {"公園名", "池", "噴水", "流れ"}:
                        continue
                    if name in seen:
                        continue
                    seen.add(name)
                    address = cells[offset + 1].get_text(strip=True)
                    facilities = list(filter(None, [
                        _cell_facility(cells[offset + 2], "池"),
                        _cell_facility(cells[offset + 3], "噴水"),
                        _cell_facility(cells[offset + 4], "流れ"),
                    ]))
                    if not facilities:
                        continue
                    parks_data.append({
                        "name": name, "address": address,
                        "description": f"施設: {'・'.join(facilities)}",
                    })

        current_osm_ids = {
            f"nerima_{re.sub(r'[\s/\\]', '_', p['name'])}"
            for p in parks_data
        }

        _nm_status["total"] = len(parks_data)
        print(f"[nerima] {len(parks_data)} 件発見")

        now_expr = "CURRENT_TIMESTAMP" if USE_SQLITE else "NOW()"
        for park in parks_data:
            name        = park["name"]
            address     = park["address"]
            description = park["description"]
            slug        = re.sub(r"[\s/\\]", "_", name)
            osm_id      = f"nerima_{slug}"

            full_addr = f"東京都練馬区{address}" if address else "東京都練馬区"
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(_q("SELECT id, address FROM parks WHERE osm_id=%s"), (osm_id,))
                row = cur.fetchone()

            if row:
                existing_id, existing_addr = row
                if (not existing_addr or _is_ward_only_addr(existing_addr)) and full_addr:
                    result = _geocode_park(name, full_addr)
                    if result:
                        lat_r, lon_r, geocoded_addr = result
                        better = _best_addr(full_addr, geocoded_addr)
                        if better != existing_addr:
                            with get_db() as conn:
                                cur = conn.cursor()
                                cur.execute(
                                    _q("UPDATE parks SET lat=%s, lon=%s, address=%s WHERE id=%s"),
                                    (lat_r, lon_r, better, existing_id),
                                )
                            print(f"[nerima] 住所更新: {name} → {better}")
                _nm_status["done"] += 1
                continue

            result = _geocode_park(name, full_addr)
            if not result:
                print(f"[nerima] geocode 失敗: {name} ({full_addr})")
                _nm_status["done"] += 1
                continue
            lat, lon, geocoded_addr = result
            better = _best_addr(full_addr, geocoded_addr)

            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        _q(f"""INSERT INTO parks
                               (osm_id, lat, lon, name, park_type, source, description, source_url, address, last_fetched, created_at)
                               VALUES (%s,%s,%s,%s,'park','nerima',%s,%s,%s,{now_expr},{now_expr})
                               ON CONFLICT (osm_id) DO NOTHING"""),
                        (osm_id, lat, lon, name, description, NERIMA_URL, better),
                    )
                    if cur.rowcount:
                        _nm_status["inserted"] += 1
                        print(f"[nerima] 登録: {name} ({lat:.5f},{lon:.5f})")
            except Exception as exc:
                print(f"[nerima] db error {name}: {exc}")

            _nm_status["done"] += 1

        _delete_removed_parks("nerima", current_osm_ids, _nm_status)

    except Exception as exc:
        print(f"[nerima] fetch error: {exc}")
    finally:
        _nm_status["running"] = False
        print(f"[nerima] 完了: {_nm_status['inserted']} 件登録 / {_nm_status['deleted']} 件削除")


@app.post("/api/sync/nerima")
def sync_nerima():
    if _nm_status["running"]:
        return {"status": "already_running", **_nm_status}
    threading.Thread(target=fetch_nerima_parks, daemon=True).start()
    return {"status": "started"}


@app.get("/api/sync/nerima/status")
def nerima_status():
    return _nm_status


def fetch_toritsu_parks(force: bool = False):
    """force=True のとき既存エントリの座標を再ジオコードして更新する。"""
    global _tt_status
    _tt_status = {"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
    headers = {"User-Agent": KOENTANBO_UA}
    try:
        r = _requests.get(TORITSU_URL, timeout=15, headers=headers)
        r.raise_for_status()
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")

        parks_data = []
        seen = set()
        table = soup.find("table")
        if not table:
            raise ValueError("テーブルが見つかりません")

        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            name = cells[0].get_text(strip=True)
            if not name or name == "公園名":
                continue
            if name in seen:
                continue
            seen.add(name)
            address = cells[1].get_text(strip=True)
            a = cells[0].find("a", href=True)
            park_url = a["href"] if a else ""
            parks_data.append({"name": name, "address": address, "source_url": park_url})

        current_osm_ids = {
            f"toritsu_{re.sub(r'[\s/\\]', '_', p['name'])}"
            for p in parks_data
        }

        _tt_status["total"] = len(parks_data)
        print(f"[toritsu] {len(parks_data)} 件発見 (force={force})")

        now_expr = "CURRENT_TIMESTAMP" if USE_SQLITE else "NOW()"
        for park in parks_data:
            name       = park["name"]
            address    = park["address"]
            source_url = park["source_url"]
            slug       = re.sub(r"[\s/\\]", "_", name)
            osm_id     = f"toritsu_{slug}"

            # 「・」区切りの住所は最初の市区町村のみ使用（ジオコード精度向上）
            address_primary = re.split(r"[・/]", address)[0].strip()
            geocode_addr = f"東京都{address_primary}" if address_primary else ""

            existing_id = None
            existing_addr = None
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(_q("SELECT id, address FROM parks WHERE osm_id=%s"), (osm_id,))
                row = cur.fetchone()
                if row:
                    existing_id, existing_addr = row
                    if not force and existing_addr and not _is_ward_only_addr(existing_addr):
                        _tt_status["done"] += 1
                        continue
                # osm_id なし → 同名の他ソースエントリを探して toritsu に更新
                if existing_id is None:
                    cur.execute(_q("SELECT id FROM parks WHERE name=%s AND source != 'toritsu' LIMIT 1"), (name,))
                    other = cur.fetchone()
                    if other:
                        existing_id = other[0]

            # 既知座標辞書を優先、なければジオコード
            hardcoded = TORITSU_COORDS.get(name)
            if hardcoded:
                lat, lon = hardcoded
                geocoded_addr_tt = ""
            else:
                result = _geocode_park(name, geocode_addr)
                if not result:
                    print(f"[toritsu] geocode 失敗: {name} ({geocode_addr})")
                    _tt_status["done"] += 1
                    continue
                lat, lon, geocoded_addr_tt = result
            full_addr = _best_addr(geocode_addr, geocoded_addr_tt)

            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    if existing_id:
                        cur.execute(
                            _q("UPDATE parks SET lat=%s, lon=%s, source='toritsu', source_url=%s, osm_id=%s, address=COALESCE(NULLIF(address,''),%s) WHERE id=%s"),
                            (lat, lon, source_url, osm_id, full_addr, existing_id),
                        )
                        print(f"[toritsu] 座標更新: {name} ({lat:.5f},{lon:.5f})")
                    else:
                        cur.execute(
                            _q(f"""INSERT INTO parks
                                   (osm_id, lat, lon, name, park_type, source, description, source_url, address, last_fetched, created_at)
                                   VALUES (%s,%s,%s,%s,'park','toritsu',%s,%s,%s,{now_expr},{now_expr})
                                   ON CONFLICT (osm_id) DO NOTHING"""),
                            (osm_id, lat, lon, name, None, source_url, full_addr),
                        )
                    _tt_status["inserted"] += 1
                    print(f"[toritsu] 登録/更新: {name} ({lat:.5f},{lon:.5f})")
            except Exception as exc:
                print(f"[toritsu] db error {name}: {exc}")

            _tt_status["done"] += 1

        _delete_removed_parks("toritsu", current_osm_ids, _tt_status)

    except Exception as exc:
        print(f"[toritsu] fetch error: {exc}")
    finally:
        _tt_status["running"] = False
        print(f"[toritsu] 完了: {_tt_status['inserted']} 件登録/更新 / {_tt_status['deleted']} 件削除")


@app.post("/api/sync/toritsu")
def sync_toritsu(force: bool = False):
    if _tt_status["running"]:
        return {"status": "already_running", **_tt_status}
    threading.Thread(target=fetch_toritsu_parks, args=(force,), daemon=True).start()
    return {"status": "started"}


@app.get("/api/sync/toritsu/status")
def toritsu_status():
    return _tt_status


def fetch_minato_parks():
    """港区公式サイトから水遊び場（じゃぶじゃぶ池など）を取得する。"""
    global _mn_status
    _mn_status = {"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0}
    headers = {"User-Agent": KOENTANBO_UA}
    try:
        r = _requests.get(MINATO_URL, timeout=15, headers=headers)
        r.raise_for_status()
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")

        parks_data = []
        seen = set()
        skip_names = {"公園名", "施設名", "名称"}

        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                cells = tr.find_all(["th", "td"])
                if len(cells) < 2:
                    continue
                a = cells[1].find("a", href=True)
                name = (a.get_text(strip=True) if a else cells[1].get_text(strip=True))
                if not name or name in skip_names or name in seen or _is_junk_name(name):
                    continue
                seen.add(name)
                href = a["href"] if a else ""
                park_url = (MINATO_BASE + href) if href.startswith("/") else href
                desc = cells[5].get_text(strip=True) if len(cells) > 5 else ""
                parks_data.append({"name": name, "source_url": park_url, "description": desc})

        current_osm_ids = {f"minato_{re.sub(r'[\s/\\]', '_', p['name'])}" for p in parks_data}
        _mn_status["total"] = len(parks_data)
        print(f"[minato] {len(parks_data)} 件発見")

        now_expr = "CURRENT_TIMESTAMP" if USE_SQLITE else "NOW()"
        for park in parks_data:
            name       = park["name"]
            source_url = park["source_url"]
            desc       = park["description"] or None
            osm_id     = f"minato_{re.sub(r'[\s/\\]', '_', name)}"

            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(_q("SELECT id, address FROM parks WHERE osm_id=%s"), (osm_id,))
                row = cur.fetchone()

            if row:
                existing_id, existing_addr = row
                if not existing_addr or _is_ward_only_addr(existing_addr):
                    result = _geocode_park(name, "東京都港区")
                    if result:
                        lat_r, lon_r, geocoded_addr = result
                        better = _best_addr("東京都港区", geocoded_addr)
                        if better != existing_addr:
                            with get_db() as conn:
                                cur = conn.cursor()
                                cur.execute(
                                    _q("UPDATE parks SET lat=%s, lon=%s, address=%s WHERE id=%s"),
                                    (lat_r, lon_r, better, existing_id),
                                )
                            print(f"[minato] 住所更新: {name} → {better}")
                _mn_status["done"] += 1
                continue

            result = _geocode_park(name, "東京都港区")
            if not result:
                print(f"[minato] geocode 失敗: {name}")
                _mn_status["done"] += 1
                continue
            lat, lon, geocoded_addr = result
            better = _best_addr("東京都港区", geocoded_addr)

            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        _q(f"""INSERT INTO parks
                               (osm_id, lat, lon, name, park_type, source, description, source_url, address, last_fetched, created_at)
                               VALUES (%s,%s,%s,%s,'park','minato',%s,%s,%s,{now_expr},{now_expr})
                               ON CONFLICT (osm_id) DO NOTHING"""),
                        (osm_id, lat, lon, name, desc, source_url, better),
                    )
                    _mn_status["inserted"] += 1
            except Exception as exc:
                print(f"[minato] db error {name}: {exc}")
            _mn_status["done"] += 1

        _delete_removed_parks("minato", current_osm_ids, _mn_status)

    except Exception as exc:
        print(f"[minato] fetch error: {exc}")
    finally:
        _mn_status["running"] = False
        print(f"[minato] 完了: {_mn_status['inserted']} 件登録 / {_mn_status['deleted']} 件削除")


@app.post("/api/sync/minato")
def sync_minato():
    if _mn_status["running"]:
        return {"status": "already_running", **_mn_status}
    threading.Thread(target=fetch_minato_parks, daemon=True).start()
    return {"status": "started"}


@app.get("/api/sync/minato/status")
def minato_status():
    return _mn_status


# ── 追加区 水遊び公園 汎用スクレイパー ───────────────────────────────────────────

def _build_full_address(addr_raw: str, ward_prefix: str) -> str:
    """住所文字列に東京都・区名プレフィックスを補完する。"""
    if not addr_raw:
        return ward_prefix
    if addr_raw.startswith("東京都"):
        return addr_raw
    ward_part = ward_prefix.replace("東京都", "")
    if ward_part and addr_raw.startswith(ward_part):
        return "東京都" + addr_raw
    return ward_prefix + addr_raw


_JUNK_PAT = re.compile(
    r'^\d+月'                       # 日付（6月4日、8月10日等）
    r'|です|ます|ください'           # 敬語文
    r'|について|に関する'             # 説明文
    r'|ガーデナー|カレンダー|お知らせ|トイレ|整備|落書き'  # ナビリンク
)

def _is_junk_name(name: str) -> bool:
    """公園名でない誤取得テキスト（ナビ・注記・日付等）を判定する。"""
    return bool(_JUNK_PAT.search(name))


def _fetch_generic_ward_parks(
    source: str, url: str, status: dict,
    ward_prefix: str,
    name_col: int, addr_col: int = -1, desc_col: int = -1,
    extra_skip: set | None = None,
):
    """汎用区水遊び公園スクレイパー（テーブル型ページ対応）。"""
    status.update({"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0})
    skip_names = {"公園名", "園名", "実施場所", "施設名", "名称", "公園・児童遊園名",
                  "No.", "番号", "住所", "所在地", "施設の種類", "特徴", "週休日",
                  "みどり・公園"}
    if extra_skip:
        skip_names |= extra_skip
    headers = {"User-Agent": KOENTANBO_UA}
    try:
        r = _requests.get(url, timeout=15, headers=headers)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")

        parks_data = []
        seen: set[str] = set()

        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                cells = tr.find_all(["th", "td"])
                needed = name_col + 1
                if addr_col >= 0:
                    needed = max(needed, addr_col + 1)
                if desc_col >= 0:
                    needed = max(needed, desc_col + 1)
                if len(cells) < needed:
                    continue

                name = cells[name_col].get_text(strip=True)
                if not name or name in skip_names or re.match(r'^\d+$', name):
                    continue
                if _is_junk_name(name):
                    continue
                if name in seen:
                    continue
                seen.add(name)

                addr_raw = cells[addr_col].get_text(strip=True) if 0 <= addr_col < len(cells) else ""
                desc_raw = cells[desc_col].get_text(strip=True) if 0 <= desc_col < len(cells) else ""
                if desc_raw in skip_names:
                    desc_raw = ""

                addr_full = _build_full_address(addr_raw, ward_prefix)
                description = f"施設: {desc_raw}" if desc_raw else None
                parks_data.append({"name": name, "address": addr_full, "description": description})

        # テーブルがない場合: <li> タグからパーク名を抽出
        if not parks_data:
            main_area = soup.find(["main", "article"])
            search_area = main_area or soup
            for li in search_area.find_all("li"):
                text = li.get_text(strip=True)
                if ("公園" in text or "遊園" in text) and len(text) <= 40:
                    name = re.sub(r'\s+', '', text)
                    if name and name not in seen and name not in skip_names and not _is_junk_name(name):
                        seen.add(name)
                        parks_data.append({"name": name, "address": ward_prefix, "description": None})

        current_osm_ids = {
            f"{source}_{re.sub(r'[\s/\\]', '_', p['name'])}"
            for p in parks_data
        }
        status["total"] = len(parks_data)
        print(f"[{source}] {len(parks_data)} 件発見")

        now_expr = "CURRENT_TIMESTAMP" if USE_SQLITE else "NOW()"
        for park in parks_data:
            name = park["name"]
            address = park["address"]
            description = park["description"]
            slug = re.sub(r"[\s/\\]", "_", name)
            osm_id = f"{source}_{slug}"

            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(_q("SELECT id, address FROM parks WHERE osm_id=%s"), (osm_id,))
                row = cur.fetchone()

            if row:
                existing_id, existing_addr = row
                if (not existing_addr or _is_ward_only_addr(existing_addr)) and address:
                    result = _geocode_park(name, address)
                    if result:
                        lat_r, lon_r, geocoded_addr = result
                        final_addr = _best_addr(address, geocoded_addr)
                        if final_addr != existing_addr:
                            with get_db() as conn:
                                cur = conn.cursor()
                                cur.execute(
                                    _q("UPDATE parks SET lat=%s, lon=%s, address=%s WHERE id=%s"),
                                    (lat_r, lon_r, final_addr, existing_id),
                                )
                            print(f"[{source}] 住所更新: {name} → {final_addr}")
                status["done"] += 1
                continue

            result = _geocode_park(name, address)
            if not result:
                print(f"[{source}] geocode 失敗: {name} ({address})")
                status["done"] += 1
                continue
            lat, lon, geocoded_addr = result
            final_addr = _best_addr(address, geocoded_addr)

            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        _q(f"""INSERT INTO parks
                               (osm_id, lat, lon, name, park_type, source, description, source_url,
                                address, last_fetched, created_at)
                               VALUES (%s,%s,%s,%s,'park',%s,%s,%s,%s,{now_expr},{now_expr})
                               ON CONFLICT (osm_id) DO NOTHING"""),
                        (osm_id, lat, lon, name, source, description, url, final_addr),
                    )
                    if cur.rowcount:
                        status["inserted"] += 1
                        print(f"[{source}] 登録: {name} ({lat:.5f},{lon:.5f})")
            except Exception as exc:
                print(f"[{source}] db error {name}: {exc}")
            status["done"] += 1

        _delete_removed_parks(source, current_osm_ids, status)

    except Exception as exc:
        print(f"[{source}] fetch error: {exc}")
    finally:
        status["running"] = False
        print(f"[{source}] 完了: {status['inserted']} 件登録 / {status.get('deleted', 0)} 件削除")


def _sync_parks_data(source: str, url: str, status: dict, parks_data: list):
    """parks_data のジオコーディング・DB登録・不要レコード削除を一括実行するヘルパー。"""
    current_osm_ids = {
        f"{source}_{re.sub(r'[\s/\\]', '_', p['name'])}"
        for p in parks_data
    }
    status["total"] = len(parks_data)
    print(f"[{source}] {len(parks_data)} 件発見")

    now_expr = "CURRENT_TIMESTAMP" if USE_SQLITE else "NOW()"
    for park in parks_data:
        name = park["name"]
        address = park.get("address", "")
        description = park.get("description")
        slug = re.sub(r"[\s/\\]", "_", name)
        osm_id = f"{source}_{slug}"

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(_q("SELECT id, address FROM parks WHERE osm_id=%s"), (osm_id,))
            row = cur.fetchone()

        if row:
            existing_id, existing_addr = row
            # 住所が未設定または区名のみのレコードは再ジオコードして更新
            if (not existing_addr or _is_ward_only_addr(existing_addr)) and address:
                result = _geocode_park(name, address)
                if result:
                    lat_r, lon_r, geocoded_addr = result
                    final_addr = _best_addr(address, geocoded_addr)
                    if final_addr != existing_addr:
                        with get_db() as conn:
                            cur = conn.cursor()
                            cur.execute(
                                _q("UPDATE parks SET lat=%s, lon=%s, address=%s WHERE id=%s"),
                                (lat_r, lon_r, final_addr, existing_id),
                            )
                        status["inserted"] += 1
                        print(f"[{source}] 住所更新: {name} → {final_addr}")
            status["done"] += 1
            continue

        result = _geocode_park(name, address)
        if not result:
            print(f"[{source}] geocode 失敗: {name} ({address})")
            status["done"] += 1
            continue
        lat, lon, geocoded_addr = result
        final_addr = _best_addr(address, geocoded_addr)

        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    _q(f"""INSERT INTO parks
                           (osm_id, lat, lon, name, park_type, source, description, source_url,
                            address, last_fetched, created_at)
                           VALUES (%s,%s,%s,%s,'park',%s,%s,%s,%s,{now_expr},{now_expr})
                           ON CONFLICT (osm_id) DO NOTHING"""),
                    (osm_id, lat, lon, name, source, description, url, final_addr),
                )
                if cur.rowcount:
                    status["inserted"] += 1
                    print(f"[{source}] 登録: {name} ({lat:.5f},{lon:.5f}) addr={final_addr}")
        except Exception as exc:
            print(f"[{source}] db error {name}: {exc}")
        status["done"] += 1

    _delete_removed_parks(source, current_osm_ids, status)
    status["running"] = False
    print(f"[{source}] 完了: {status['inserted']} 件登録 / {status.get('deleted', 0)} 件削除")


# ── 大田区 ────────────────────────────────────────────────────────────────────

def fetch_ota_parks():
    _fetch_generic_ward_parks(
        'ota', OTA_URL, _ot_status,
        ward_prefix="東京都大田区", name_col=0, addr_col=1, desc_col=2,
    )

@app.post("/api/sync/ota")
def sync_ota():
    if _ot_status["running"]:
        return {"status": "already_running", **_ot_status}
    threading.Thread(target=fetch_ota_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/ota/status")
def ota_status():
    return _ot_status


# ── 世田谷区 ──────────────────────────────────────────────────────────────────

def fetch_setagaya_parks():
    """世田谷区 - 公園名が <h2> 見出しに記載されているため専用パーサー。"""
    source, status = 'setagaya', _sw_status
    status.update({"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0})
    try:
        r = _requests.get(SETAGAYA_URL, timeout=15, headers={"User-Agent": KOENTANBO_UA})
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        parks_data: list[dict] = []
        seen: set[str] = set()
        for h2 in soup.find_all("h2"):
            name = h2.get_text(strip=True)
            if not ("公園" in name or "遊園" in name or "緑地" in name):
                continue
            # "水辺のある公園", "特徴のある公園" 等のカテゴリ見出しをスキップ
            if "ある" in name or _is_junk_name(name):
                continue
            if name not in seen:
                seen.add(name)
                parks_data.append({"name": name, "address": "東京都世田谷区", "description": None})
        _sync_parks_data(source, SETAGAYA_URL, status, parks_data)
    except Exception as exc:
        print(f"[{source}] fetch error: {exc}")
        status["running"] = False

@app.post("/api/sync/setagaya")
def sync_setagaya():
    if _sw_status["running"]:
        return {"status": "already_running", **_sw_status}
    threading.Thread(target=fetch_setagaya_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/setagaya/status")
def setagaya_status():
    return _sw_status


# ── 台東区 ────────────────────────────────────────────────────────────────────

def fetch_taito_parks():
    """台東区 - li要素に「公園名（住所）」形式で記載されているため専用パーサー。"""
    source, status = 'taito', _ti_status
    status.update({"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0})
    try:
        r = _requests.get(TAITO_URL, timeout=15, headers={"User-Agent": KOENTANBO_UA})
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        parks_data: list[dict] = []
        seen: set[str] = set()
        main = soup.find(["main", "article"]) or soup
        for li in main.find_all("li"):
            raw = li.get_text(strip=True)
            m = re.match(r'^(.+?)[（(]([^）)]+)[）)]', raw)
            if not m:
                continue
            name = m.group(1).strip()
            if not ("公園" in name or "遊園" in name or "広場" in name):
                continue
            if _is_junk_name(name):
                continue
            addr = "東京都台東区" + m.group(2).strip()
            if name not in seen:
                seen.add(name)
                parks_data.append({"name": name, "address": addr, "description": None})
        _sync_parks_data(source, TAITO_URL, status, parks_data)
    except Exception as exc:
        print(f"[{source}] fetch error: {exc}")
        status["running"] = False

@app.post("/api/sync/taito")
def sync_taito():
    if _ti_status["running"]:
        return {"status": "already_running", **_ti_status}
    threading.Thread(target=fetch_taito_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/taito/status")
def taito_status():
    return _ti_status


# ── 文京区 ────────────────────────────────────────────────────────────────────

def fetch_bunkyo_parks():
    """文京区 - li要素に「公園名（住所）」形式で記載されているため専用パーサー。"""
    source, status = 'bunkyo', _bk_status
    status.update({"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0})
    try:
        r = _requests.get(BUNKYO_URL, timeout=15, headers={"User-Agent": KOENTANBO_UA})
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        parks_data: list[dict] = []
        seen: set[str] = set()
        main = soup.find(["main", "article"]) or soup
        for li in main.find_all("li"):
            raw = li.get_text(strip=True)
            m = re.match(r'^(.+?)[（(]([^）)]+)[）)]', raw)
            if not m:
                continue
            name = m.group(1).strip()
            if not ("公園" in name or "遊園" in name or "緑地" in name):
                continue
            if _is_junk_name(name):
                continue
            addr = "東京都文京区" + m.group(2).strip()
            if name not in seen:
                seen.add(name)
                parks_data.append({"name": name, "address": addr, "description": None})
        _sync_parks_data(source, BUNKYO_URL, status, parks_data)
    except Exception as exc:
        print(f"[{source}] fetch error: {exc}")
        status["running"] = False

@app.post("/api/sync/bunkyo")
def sync_bunkyo():
    if _bk_status["running"]:
        return {"status": "already_running", **_bk_status}
    threading.Thread(target=fetch_bunkyo_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/bunkyo/status")
def bunkyo_status():
    return _bk_status


# ── 北区 ─────────────────────────────────────────────────────────────────────

def fetch_kita_parks():
    """北区 - 公園名が p 要素内に '、' 区切りで記載されているため専用パーサー。"""
    source, status = 'kita', _kt_status
    status.update({"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0})
    try:
        r = _requests.get(KITA_URL, timeout=15, headers={"User-Agent": KOENTANBO_UA})
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        parks_data: list[dict] = []
        seen: set[str] = set()
        main = soup.find(["main", "article"]) or soup
        for elem in main.find_all(["p", "dd", "li"]):
            text = elem.get_text(strip=True)
            if "、" not in text:
                continue
            for name in text.split("、"):
                name = re.sub(r"\s+", "", name)
                if ("公園" in name or "遊園" in name) and 2 < len(name) <= 25 and name not in seen:
                    seen.add(name)
                    parks_data.append({"name": name, "address": "東京都北区", "description": None})
        _sync_parks_data(source, KITA_URL, status, parks_data)
    except Exception as exc:
        print(f"[{source}] fetch error: {exc}")
        status["running"] = False

@app.post("/api/sync/kita")
def sync_kita():
    if _kt_status["running"]:
        return {"status": "already_running", **_kt_status}
    threading.Thread(target=fetch_kita_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/kita/status")
def kita_status():
    return _kt_status


# ── 荒川区 ────────────────────────────────────────────────────────────────────

def fetch_arakawa_parks():
    """荒川区 - 公園名が h3 見出しに '・' 区切りで記載されているため専用パーサー。"""
    source, status = 'arakawa', _ar_status
    status.update({"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0})
    try:
        r = _requests.get(ARAKAWA_URL, timeout=15, headers={"User-Agent": KOENTANBO_UA})
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        parks_data: list[dict] = []
        seen: set[str] = set()
        for h3 in soup.find_all("h3"):
            text = re.sub(r"[（(].*?[）)]", "", h3.get_text(strip=True)).strip()
            if "公園" not in text and "遊園" not in text:
                continue
            for name in re.split(r"[・]", text):
                name = name.strip()
                if name and name not in seen:
                    seen.add(name)
                    parks_data.append({"name": name, "address": "東京都荒川区", "description": None})
        _sync_parks_data(source, ARAKAWA_URL, status, parks_data)
    except Exception as exc:
        print(f"[{source}] fetch error: {exc}")
        status["running"] = False

@app.post("/api/sync/arakawa")
def sync_arakawa():
    if _ar_status["running"]:
        return {"status": "already_running", **_ar_status}
    threading.Thread(target=fetch_arakawa_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/arakawa/status")
def arakawa_status():
    return _ar_status


# ── 板橋区 ────────────────────────────────────────────────────────────────────

def fetch_itabashi_parks():
    _fetch_generic_ward_parks(
        'itabashi', ITABASHI_URL, _ib_status,
        ward_prefix="東京都板橋区", name_col=1, addr_col=2,
    )

@app.post("/api/sync/itabashi")
def sync_itabashi():
    if _ib_status["running"]:
        return {"status": "already_running", **_ib_status}
    threading.Thread(target=fetch_itabashi_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/itabashi/status")
def itabashi_status():
    return _ib_status


# ── 足立区 ────────────────────────────────────────────────────────────────────

def fetch_adachi_parks():
    """足立区 - 「公園名（所在地）」が1列に合体しているため専用パーサー。"""
    source, status = 'adachi', _ad_status
    status.update({"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0})
    try:
        r = _requests.get(ADACHI_URL, timeout=15, headers={"User-Agent": KOENTANBO_UA})
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        parks_data: list[dict] = []
        seen: set[str] = set()
        skip = {"公園名", "公園名（所在地）", "施設名"}
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                cells = tr.find_all(["th", "td"])
                if len(cells) < 1:
                    continue
                raw = cells[0].get_text(strip=True)
                if not raw or raw in skip or _is_junk_name(raw):
                    continue
                # "北鹿浜公園（鹿浜5-22-1）" → name="北鹿浜公園", addr="東京都足立区鹿浜5-22-1"
                m = re.match(r'^(.+?)[（(]([^）)]+)[）)]', raw)
                if m:
                    name = m.group(1).strip()
                    addr = "東京都足立区" + m.group(2).strip()
                else:
                    name = raw
                    addr = "東京都足立区"
                desc = cells[2].get_text(strip=True) if len(cells) > 2 else None
                if desc and _is_junk_name(desc):
                    desc = None
                if name and name not in seen:
                    seen.add(name)
                    parks_data.append({"name": name, "address": addr,
                                       "description": f"施設: {desc}" if desc else None})
        _sync_parks_data(source, ADACHI_URL, status, parks_data)
    except Exception as exc:
        print(f"[{source}] fetch error: {exc}")
        status["running"] = False

@app.post("/api/sync/adachi")
def sync_adachi():
    if _ad_status["running"]:
        return {"status": "already_running", **_ad_status}
    threading.Thread(target=fetch_adachi_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/adachi/status")
def adachi_status():
    return _ad_status


# ── 葛飾区 ────────────────────────────────────────────────────────────────────

def fetch_katsushika_parks():
    _fetch_generic_ward_parks(
        'katsushika', KATSUSHIKA_URL, _ks_status,
        ward_prefix="東京都葛飾区", name_col=0, addr_col=1,
    )

@app.post("/api/sync/katsushika")
def sync_katsushika():
    if _ks_status["running"]:
        return {"status": "already_running", **_ks_status}
    threading.Thread(target=fetch_katsushika_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/katsushika/status")
def katsushika_status():
    return _ks_status


# ── 千代田区 ──────────────────────────────────────────────────────────────────

def fetch_chiyoda_parks():
    """千代田区 - li要素に「公園名（住所）」形式で記載されているため専用パーサー。"""
    source = 'chiyoda'
    status = _cd_status
    status.update({"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0})
    headers = {"User-Agent": KOENTANBO_UA}
    try:
        r = _requests.get(CHIYODA_URL, timeout=15, headers=headers)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        parks_data: list[dict] = []
        seen: set[str] = set()
        main_area = soup.find(["main", "article"]) or soup
        for li in main_area.find_all("li"):
            raw = li.get_text(strip=True)
            m = re.match(r'^(.+?)[（(]([^）)]+)[）)]', raw)
            if not m:
                continue
            name = m.group(1).strip()
            if not any(kw in name for kw in ("公園", "遊園", "池", "広場")):
                continue
            if _is_junk_name(name) or name in seen:
                continue
            addr = "東京都千代田区" + m.group(2).strip()
            seen.add(name)
            parks_data.append({"name": name, "address": addr, "description": None})
    except Exception as exc:
        print(f"[{source}] fetch error: {exc}")
        status["running"] = False
        return
    _sync_parks_data(source, CHIYODA_URL, status, parks_data)

@app.post("/api/sync/chiyoda")
def sync_chiyoda():
    if _cd_status["running"]:
        return {"status": "already_running", **_cd_status}
    threading.Thread(target=fetch_chiyoda_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/chiyoda/status")
def chiyoda_status():
    return _cd_status


# ── 墨田区 ────────────────────────────────────────────────────────────────────

def fetch_sumida_parks():
    _fetch_generic_ward_parks(
        'sumida', SUMIDA_URL, _sm_status,
        ward_prefix="東京都墨田区", name_col=0, addr_col=1,
        extra_skip={"所在地", "面積", "深さ", "備考"},
    )

@app.post("/api/sync/sumida")
def sync_sumida():
    if _sm_status["running"]:
        return {"status": "already_running", **_sm_status}
    threading.Thread(target=fetch_sumida_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/sumida/status")
def sumida_status():
    return _sm_status


# ── 江東区 ────────────────────────────────────────────────────────────────────

def fetch_koto_parks():
    _fetch_generic_ward_parks(
        'koto', KOTO_URL, _ko_status,
        ward_prefix="東京都江東区", name_col=0, addr_col=1,
        extra_skip={"住所", "面積", "深さ", "備考"},
    )

@app.post("/api/sync/koto")
def sync_koto():
    if _ko_status["running"]:
        return {"status": "already_running", **_ko_status}
    threading.Thread(target=fetch_koto_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/koto/status")
def koto_status():
    return _ko_status


# ── 品川区 ────────────────────────────────────────────────────────────────────

def fetch_shinagawa_parks():
    _fetch_generic_ward_parks(
        'shinagawa', SHINAGAWA_URL, _sn_status,
        ward_prefix="東京都品川区", name_col=0,
    )

@app.post("/api/sync/shinagawa")
def sync_shinagawa():
    if _sn_status["running"]:
        return {"status": "already_running", **_sn_status}
    threading.Thread(target=fetch_shinagawa_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/shinagawa/status")
def shinagawa_status():
    return _sn_status


# ── 目黒区 ────────────────────────────────────────────────────────────────────

def fetch_meguro_parks():
    """目黒区 - li要素内の<a>テキストが公園名、括弧内が住所。"""
    source = 'meguro'
    status = _mg_status
    status.update({"running": True, "total": 0, "done": 0, "inserted": 0, "deleted": 0})
    headers = {"User-Agent": KOENTANBO_UA}
    try:
        r = _requests.get(MEGURO_URL, timeout=15, headers=headers)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        parks_data: list[dict] = []
        seen: set[str] = set()
        main_area = soup.find(["main", "article"]) or soup
        for li in main_area.find_all("li"):
            raw = li.get_text(strip=True)
            m = re.match(r'^(.+?)[（(]([^）)]+)[）)]', raw)
            if not m:
                continue
            name = m.group(1).strip()
            if not any(kw in name for kw in ("公園", "遊園", "広場", "池")):
                continue
            if _is_junk_name(name) or name in seen:
                continue
            addr_part = m.group(2).strip()
            addr = addr_part if addr_part.startswith("東京都") else "東京都目黒区" + addr_part
            seen.add(name)
            parks_data.append({"name": name, "address": addr, "description": None})
    except Exception as exc:
        print(f"[{source}] fetch error: {exc}")
        status["running"] = False
        return
    _sync_parks_data(source, MEGURO_URL, status, parks_data)

@app.post("/api/sync/meguro")
def sync_meguro():
    if _mg_status["running"]:
        return {"status": "already_running", **_mg_status}
    threading.Thread(target=fetch_meguro_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/meguro/status")
def meguro_status():
    return _mg_status


# ── 渋谷区 ────────────────────────────────────────────────────────────────────

def fetch_shibuya_parks():
    _fetch_generic_ward_parks(
        'shibuya', SHIBUYA_URL, _sb_status,
        ward_prefix="東京都渋谷区", name_col=0,
    )

@app.post("/api/sync/shibuya")
def sync_shibuya():
    if _sb_status["running"]:
        return {"status": "already_running", **_sb_status}
    threading.Thread(target=fetch_shibuya_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/shibuya/status")
def shibuya_status():
    return _sb_status


# ── 中野区 ────────────────────────────────────────────────────────────────────

def fetch_nakano_parks():
    _fetch_generic_ward_parks(
        'nakano', NAKANO_URL, _nk_status,
        ward_prefix="東京都中野区", name_col=0, addr_col=1,
        extra_skip={"所在地", "池の面積", "池の深さ", "備考"},
    )

@app.post("/api/sync/nakano")
def sync_nakano():
    if _nk_status["running"]:
        return {"status": "already_running", **_nk_status}
    threading.Thread(target=fetch_nakano_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/nakano/status")
def nakano_status():
    return _nk_status


# ── 江戸川区 ──────────────────────────────────────────────────────────────────

def fetch_edogawa_parks():
    _fetch_generic_ward_parks(
        'edogawa', EDOGAWA_URL, _eg_status,
        ward_prefix="東京都江戸川区", name_col=0, addr_col=1,
        extra_skip={"園名", "所在地", "面積", "深さ", "備考"},
    )

@app.post("/api/sync/edogawa")
def sync_edogawa():
    if _eg_status["running"]:
        return {"status": "already_running", **_eg_status}
    threading.Thread(target=fetch_edogawa_parks, daemon=True).start()
    return {"status": "started"}

@app.get("/api/sync/edogawa/status")
def edogawa_status():
    return _eg_status


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
        # ソース別内訳
        cur.execute("SELECT COALESCE(source,'osm'), COUNT(*) FROM parks GROUP BY source ORDER BY COUNT(*) DESC")
        breakdown = dict(cur.fetchall())
        # 今日の訪問数（JST）
        if USE_SQLITE:
            cur.execute("SELECT COUNT(*) FROM page_views WHERE date(created_at,'+9 hours')=date('now','+9 hours')")
        else:
            cur.execute("SELECT COUNT(*) FROM page_views WHERE (created_at AT TIME ZONE 'Asia/Tokyo')::date=(NOW() AT TIME ZONE 'Asia/Tokyo')::date")
        today_v = cur.fetchone()[0]
    return {"parks": p, "photos": ph, "visits": v, "visits_today": today_v, "parks_breakdown": breakdown}


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
