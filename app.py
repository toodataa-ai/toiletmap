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
KOENTANBO_SITEMAPS = [
    f"{KOENTANBO_BASE}/post-sitemap.xml",
    f"{KOENTANBO_BASE}/post-sitemap2.xml",
    f"{KOENTANBO_BASE}/post-sitemap3.xml",
    f"{KOENTANBO_BASE}/post-sitemap4.xml",
]
# 関東広域バウンディングボックス
KANTO_BBOX = (34.5, 138.5, 37.0, 141.5)

_kb_status: dict = {"running": False, "total": 0, "done": 0, "inserted": 0, "skipped": 0}
_sj_status: dict = {"running": False, "total": 0, "done": 0, "inserted": 0}
_sg_status: dict = {"running": False, "total": 0, "done": 0, "inserted": 0}


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

def _geocode_park(name: str, address: str = "") -> tuple | None:
    """Nominatim で公園→(lat, lon) 変換。公園名優先、失敗時は住所でリトライ。"""
    queries = [name, address] if address else [name]
    for q in queries:
        try:
            resp = _requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "json", "limit": 1, "countrycodes": "jp"},
                headers={"User-Agent": KOENTANBO_UA},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception as exc:
            print(f"[shinjuku] geocode error '{q}': {exc}")
        time.sleep(1.1)
    return None


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
    _sj_status = {"running": True, "total": 0, "done": 0, "inserted": 0}
    headers = {"User-Agent": KOENTANBO_UA}
    try:
        r = _requests.get(SHINJUKU_URL, timeout=15, headers=headers)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")

        parks_data = []
        seen_links = set()
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["th", "td"])  # 公園名は <th scope="row">
            if not cells:
                continue
            a = cells[0].find("a", href=True)
            if not a:
                continue
            href = a["href"]
            if href in seen_links:
                continue
            name    = a.get_text(strip=True)
            address = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            seen_links.add(href)
            parks_data.append({"name": name, "href": href, "address": address})

        _sj_status["total"] = len(parks_data)
        print(f"[shinjuku] {len(parks_data)} 件発見")

        now_expr = "CURRENT_TIMESTAMP" if USE_SQLITE else "NOW()"
        for park in parks_data:
            name    = park["name"]
            href    = park["href"]
            address = park["address"]
            slug    = re.sub(r'[^a-z0-9_]', '_', href.rstrip("/").split("/")[-1].lower())
            osm_id  = f"shinjuku_{slug}"

            # 座標取得（既存レコードがあればDBの値を再利用）
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(_q("SELECT id, lat, lon FROM parks WHERE osm_id=%s"), (osm_id,))
                existing = cur.fetchone()

            if existing:
                park_id, lat, lon = existing[0], existing[1], existing[2]
            else:
                coords = _geocode_park(name, f"東京都新宿区{address}" if address else "")
                if not coords:
                    print(f"[shinjuku] geocode 失敗: {name} ({address})")
                    _sj_status["done"] += 1
                    continue
                lat, lon = coords
                park_id = None

            detail_url = (SHINJUKU_BASE + href) if href.startswith("/") else href
            photos = _parse_shinjuku_park_detail(detail_url)

            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    if park_id is None:
                        cur.execute(
                            _q(f"""INSERT INTO parks
                                   (osm_id, lat, lon, name, park_type, source, last_fetched, created_at)
                                   VALUES (%s,%s,%s,%s,'park','shinjuku',{now_expr},{now_expr})
                                   ON CONFLICT (osm_id) DO NOTHING"""),
                            (osm_id, lat, lon, name),
                        )
                        if USE_SQLITE:
                            park_id = cur.lastrowid
                        else:
                            cur.execute(_q("SELECT id FROM parks WHERE osm_id=%s"), (osm_id,))
                            park_id = cur.fetchone()[0]
                        _sj_status["inserted"] += 1
                        print(f"[shinjuku] 登録: {name} ({lat:.5f},{lon:.5f}) 写真{len(photos)}枚")
                    else:
                        print(f"[shinjuku] 写真更新: {name} 写真{len(photos)}枚")

                    # 写真を差し替え（ロゴ混入修正のため毎回更新）
                    cur.execute(
                        _q("DELETE FROM park_photos WHERE park_id=%s AND photo_source='shinjuku'"),
                        (park_id,),
                    )
                    for photo_url in photos:
                        cur.execute(
                            _q("INSERT INTO park_photos"
                               " (park_id, photo_url, caption, photo_source)"
                               " VALUES (%s,%s,%s,'shinjuku')"),
                            (park_id, photo_url, None),
                        )
            except Exception as exc:
                print(f"[shinjuku] db error {name}: {exc}")

            _sj_status["done"] += 1

    except Exception as exc:
        print(f"[shinjuku] fetch error: {exc}")
    finally:
        _sj_status["running"] = False
        print(f"[shinjuku] 完了: {_sj_status['inserted']} 件登録")


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
    _sg_status = {"running": True, "total": 0, "done": 0, "inserted": 0}
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

        _sg_status["total"] = len(parks_data)
        print(f"[suginami] {len(parks_data)} 件発見（流れあり）")

        now_expr = "CURRENT_TIMESTAMP" if USE_SQLITE else "NOW()"
        for park in parks_data:
            name    = park["name"]
            address = park["address"]
            slug    = re.sub(r"[\s/\\]", "_", name)
            osm_id  = f"suginami_{slug}"

            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(_q("SELECT id FROM parks WHERE osm_id=%s"), (osm_id,))
                if cur.fetchone():
                    _sg_status["done"] += 1
                    continue

            coords = _geocode_park(name, f"東京都{address}" if address else "")
            if not coords:
                print(f"[suginami] geocode 失敗: {name} ({address})")
                _sg_status["done"] += 1
                continue
            lat, lon = coords

            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        _q(f"""INSERT INTO parks
                               (osm_id, lat, lon, name, park_type, source, last_fetched, created_at)
                               VALUES (%s,%s,%s,%s,'park','suginami',{now_expr},{now_expr})
                               ON CONFLICT (osm_id) DO NOTHING"""),
                        (osm_id, lat, lon, name),
                    )
                    if cur.rowcount:
                        _sg_status["inserted"] += 1
                        print(f"[suginami] 登録: {name} ({lat:.5f},{lon:.5f})")
            except Exception as exc:
                print(f"[suginami] db error {name}: {exc}")

            _sg_status["done"] += 1

    except Exception as exc:
        print(f"[suginami] fetch error: {exc}")
    finally:
        _sg_status["running"] = False
        print(f"[suginami] 完了: {_sg_status['inserted']} 件登録")


@app.post("/api/sync/suginami")
def sync_suginami():
    if _sg_status["running"]:
        return {"status": "already_running", **_sg_status}
    threading.Thread(target=fetch_suginami_parks, daemon=True).start()
    return {"status": "started"}


@app.get("/api/sync/suginami/status")
def suginami_status():
    return _sg_status


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
