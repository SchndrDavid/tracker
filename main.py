import os
import re
import json
import sqlite3
from datetime import datetime, date, timedelta
from typing import Optional, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import httpx

# -------------------------------------------------------------------------
# Configuration & Constants
# -------------------------------------------------------------------------

TAGS = ["Work", "Gaming", "Chillin", "Study", "Gym", "Running"]

TAG_COLORS = {
    "Work": "#38bdf8",     # Sky blue
    "Gaming": "#a855f7",   # Purple
    "Chillin": "#34d399",  # Mint green
    "Study": "#fbbf24",    # Amber gold
    "Gym": "#f43f5e",      # Rose red
    "Running": "#06b6d4",  # Cyan
}

DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def get_db_path() -> str:
    path = os.getenv("DAYLOG_DB", "/data/daylog.db")
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except (OSError, PermissionError):
            # Fallback for local development environments without /data permissions
            fallback_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
            os.makedirs(fallback_dir, exist_ok=True)
            return os.path.join(fallback_dir, "daylog.db")
    return path

def get_db() -> sqlite3.Connection:
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn

def init_db():
    conn = get_db()
    try:
        with conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS days (
                date       TEXT PRIMARY KEY,
                wake_time  INTEGER,
                sleep_time INTEGER,
                note       TEXT
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                start_min   INTEGER NOT NULL,
                end_min     INTEGER NOT NULL,
                label       TEXT NOT NULL DEFAULT '',
                tag         TEXT NOT NULL,
                source      TEXT NOT NULL DEFAULT 'manual',
                external_id TEXT,
                meta        TEXT,
                UNIQUE(source, external_id)
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_blocks_date ON blocks(date);")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS watched (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT NOT NULL,
                jellyfin_id  TEXT,
                title        TEXT NOT NULL,
                series_title TEXT,
                kind         TEXT NOT NULL,
                meta         TEXT,
                UNIQUE(date, jellyfin_id)
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_watched_date ON watched(date);")

            conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """)

            conn.execute("""
            CREATE TABLE IF NOT EXISTS steam_snapshots (
                appid            INTEGER PRIMARY KEY,
                name             TEXT NOT NULL,
                playtime_forever INTEGER NOT NULL,
                updated_at       TEXT NOT NULL
            );
            """)

            conn.execute("""
            CREATE TABLE IF NOT EXISTS steam_plays (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                date             TEXT NOT NULL,
                appid            INTEGER NOT NULL,
                name             TEXT NOT NULL,
                minutes          INTEGER NOT NULL,
                playtime_forever INTEGER NOT NULL,
                icon_url         TEXT,
                header_url       TEXT,
                meta             TEXT,
                UNIQUE(date, appid)
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_steam_plays_date ON steam_plays(date);")
    finally:
        conn.close()

# -------------------------------------------------------------------------
# Settings Helpers
# -------------------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    val = os.getenv(key)
    if val:
        return val.strip()
    try:
        conn = get_db()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            if row and row["value"]:
                return row["value"].strip()
        finally:
            conn.close()
    except Exception:
        pass
    return default

def set_setting(key: str, value: str):
    conn = get_db()
    try:
        with conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value.strip())
            )
    finally:
        conn.close()

# -------------------------------------------------------------------------
# Helper Functions
# -------------------------------------------------------------------------

def validate_date(date_str: str) -> str:
    if not date_str or not DATE_REGEX.match(date_str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid date format. Expected YYYY-MM-DD."
        )
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid calendar date."
        )
    return date_str

def validate_times(start_min: int, end_min: int):
    if not (0 <= start_min <= 1800 and 0 <= end_min <= 1800):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_min and end_min must be between 0 and 1800."
        )
    if end_min <= start_min:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end_min must be strictly greater than start_min."
        )

def validate_tag(tag: str):
    if tag not in TAGS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown tag '{tag}'. Allowed tags are: {', '.join(TAGS)}"
        )

def compute_awake_minutes(wake_time: Optional[int], sleep_time: Optional[int]) -> int:
    """
    Computes awake minutes for a day.
    - If both wake_time and sleep_time are set:
        - If sleep_time >= wake_time: sleep_time - wake_time
        - If sleep_time < wake_time (slept past midnight): sleep_time + 1440 - wake_time
    - If only wake_time is set: 1440 - wake_time (morning sleep 0..wake_time is excluded)
    - If only sleep_time is set: sleep_time (evening sleep sleep_time..1440 is excluded)
    - If neither is set: full 24h = 1440 min.
    """
    if wake_time is not None and sleep_time is not None:
        if sleep_time >= wake_time:
            return sleep_time - wake_time
        else:
            return sleep_time + 1440 - wake_time
    elif wake_time is not None:
        return 1440 - wake_time
    elif sleep_time is not None:
        return sleep_time
    else:
        return 1440

def parse_meta(meta_val: Any) -> dict:
    if not meta_val:
        return {}
    if isinstance(meta_val, dict):
        return meta_val
    try:
        return json.loads(meta_val)
    except Exception:
        return {}

def format_row_block(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "date": row["date"],
        "start_min": row["start_min"],
        "end_min": row["end_min"],
        "label": row["label"],
        "tag": row["tag"],
        "source": row["source"],
        "external_id": row["external_id"],
        "meta": parse_meta(row["meta"])
    }

def format_row_watched(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "date": row["date"],
        "jellyfin_id": row["jellyfin_id"],
        "title": row["title"],
        "series_title": row["series_title"],
        "kind": row["kind"],
        "meta": parse_meta(row["meta"])
    }

# -------------------------------------------------------------------------
# Pydantic Request Models
# -------------------------------------------------------------------------

class DayUpdateRequest(BaseModel):
    wake_time: Optional[int] = Field(None, ge=0, le=1439)
    sleep_time: Optional[int] = Field(None, ge=0, le=1439)
    note: Optional[str] = ""

class BlockCreateRequest(BaseModel):
    date: str
    start_min: int
    end_min: int
    label: Optional[str] = ""
    tag: str
    source: Optional[str] = "manual"
    external_id: Optional[str] = None
    meta: Optional[dict] = None

class BlockUpdateRequest(BaseModel):
    date: Optional[str] = None
    start_min: Optional[int] = None
    end_min: Optional[int] = None
    label: Optional[str] = None
    tag: Optional[str] = None
    meta: Optional[dict] = None

class WatchedCreateRequest(BaseModel):
    date: str
    title: str
    jellyfin_id: Optional[str] = None
    series_title: Optional[str] = None
    kind: str  # movie | episode | other
    meta: Optional[dict] = None

class SteamPlayCreateRequest(BaseModel):
    date: str
    name: str
    minutes: int
    appid: Optional[int] = 0
    icon_url: Optional[str] = None
    header_url: Optional[str] = None

class SettingsUpdateRequest(BaseModel):
    steam_api_key: Optional[str] = None
    steam_id: Optional[str] = None
    jellyfin_api_key: Optional[str] = None
    jellyfin_url: Optional[str] = None
    gymtrack_url: Optional[str] = None

# -------------------------------------------------------------------------
# Application Lifespan & Initialization
# -------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="DayLog", lifespan=lifespan)

# -------------------------------------------------------------------------
# Core API Endpoints
# -------------------------------------------------------------------------

@app.get("/health")
@app.get("/api/health")
def health_check():
    return {"status": "ok", "service": "daylog"}

@app.get("/api/tags")
def get_tags():
    return {
        "tags": TAGS,
        "colors": TAG_COLORS
    }

@app.get("/api/day/{date}")
def get_day(date: str):
    vdate = validate_date(date)
    conn = get_db()
    try:
        day_row = conn.execute(
            "SELECT date, wake_time, sleep_time, note FROM days WHERE date = ?",
            (vdate,)
        ).fetchone()

        wake_time = day_row["wake_time"] if day_row else None
        sleep_time = day_row["sleep_time"] if day_row else None
        note = day_row["note"] if day_row and day_row["note"] is not None else ""

        awake_min = compute_awake_minutes(wake_time, sleep_time)

        block_rows = conn.execute(
            "SELECT * FROM blocks WHERE date = ? ORDER BY start_min ASC, end_min ASC, id ASC",
            (vdate,)
        ).fetchall()

        blocks = [format_row_block(r) for r in block_rows]
        logged_min = sum(b["end_min"] - b["start_min"] for b in blocks)
        unlogged_min = max(0, awake_min - logged_min)

        steam_rows = conn.execute(
            "SELECT * FROM steam_plays WHERE date = ? ORDER BY minutes DESC, id ASC",
            (vdate,)
        ).fetchall()
        steam_games = [
            {
                "id": r["id"],
                "date": r["date"],
                "appid": r["appid"],
                "name": r["name"],
                "minutes": r["minutes"],
                "playtime_forever": r["playtime_forever"],
                "icon_url": r["icon_url"],
                "header_url": r["header_url"],
                "meta": parse_meta(r["meta"])
            }
            for r in steam_rows
        ]

        return {
            "date": vdate,
            "wake_time": wake_time,
            "sleep_time": sleep_time,
            "note": note,
            "awake_time": awake_min,
            "logged_time": logged_min,
            "unlogged_time": unlogged_min,
            "blocks": blocks,
            "steam_games": steam_games
        }
    finally:
        conn.close()

@app.post("/api/day/{date}")
def update_day(date: str, payload: DayUpdateRequest):
    vdate = validate_date(date)
    conn = get_db()
    try:
        with conn:
            conn.execute("""
            INSERT INTO days (date, wake_time, sleep_time, note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                wake_time = excluded.wake_time,
                sleep_time = excluded.sleep_time,
                note = excluded.note
            """, (vdate, payload.wake_time, payload.sleep_time, payload.note or ""))
        return get_day(vdate)
    finally:
        conn.close()

@app.get("/api/range")
def get_range(
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    vfrom = validate_date(from_date)
    vto = validate_date(to_date)
    if vfrom > vto:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from' date cannot be after 'to' date."
        )

    conn = get_db()
    try:
        days_map = {}
        for row in conn.execute("SELECT * FROM days WHERE date >= ? AND date <= ?", (vfrom, vto)):
            days_map[row["date"]] = {
                "date": row["date"],
                "wake_time": row["wake_time"],
                "sleep_time": row["sleep_time"],
                "note": row["note"] or ""
            }

        block_rows = conn.execute(
            "SELECT * FROM blocks WHERE date >= ? AND date <= ? ORDER BY date ASC, start_min ASC, id ASC",
            (vfrom, vto)
        ).fetchall()

        blocks_by_date = {}
        for r in block_rows:
            b = format_row_block(r)
            blocks_by_date.setdefault(b["date"], []).append(b)

        result = []
        cur = datetime.strptime(vfrom, "%Y-%m-%d").date()
        end_d = datetime.strptime(vto, "%Y-%m-%d").date()
        while cur <= end_d:
            d_str = cur.strftime("%Y-%m-%d")
            d_info = days_map.get(d_str, {"date": d_str, "wake_time": None, "sleep_time": None, "note": ""})
            awake = compute_awake_minutes(d_info["wake_time"], d_info["sleep_time"])
            d_blocks = blocks_by_date.get(d_str, [])
            logged = sum(b["end_min"] - b["start_min"] for b in d_blocks)
            result.append({
                **d_info,
                "awake_time": awake,
                "logged_time": logged,
                "unlogged_time": max(0, awake - logged),
                "blocks": d_blocks
            })
            cur += timedelta(days=1)

        return result
    finally:
        conn.close()

@app.post("/api/block", status_code=status.HTTP_201_CREATED)
def create_block(payload: BlockCreateRequest):
    vdate = validate_date(payload.date)
    validate_times(payload.start_min, payload.end_min)
    validate_tag(payload.tag)

    meta_dict = payload.meta or {}
    meta_json = json.dumps(meta_dict)
    source = payload.source or "manual"
    external_id = payload.external_id if source != "manual" else None

    conn = get_db()
    try:
        with conn:
            cursor = conn.execute("""
            INSERT INTO blocks (date, start_min, end_min, label, tag, source, external_id, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                vdate,
                payload.start_min,
                payload.end_min,
                payload.label or "",
                payload.tag,
                source,
                external_id,
                meta_json
            ))
            block_id = cursor.lastrowid
        
        row = conn.execute("SELECT * FROM blocks WHERE id = ?", (block_id,)).fetchone()
        return format_row_block(row)
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A block with this source and external_id already exists."
        )
    finally:
        conn.close()

@app.patch("/api/block/{block_id}")
def update_block(block_id: int, payload: BlockUpdateRequest):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM blocks WHERE id = ?", (block_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")

        current_block = format_row_block(row)
        new_date = validate_date(payload.date) if payload.date is not None else current_block["date"]
        new_start = payload.start_min if payload.start_min is not None else current_block["start_min"]
        new_end = payload.end_min if payload.end_min is not None else current_block["end_min"]
        validate_times(new_start, new_end)

        new_tag = payload.tag if payload.tag is not None else current_block["tag"]
        validate_tag(new_tag)

        new_label = payload.label if payload.label is not None else current_block["label"]

        # Merge meta: mark user_edited = True as required by specification
        current_meta = current_block["meta"]
        if payload.meta:
            current_meta.update(payload.meta)
        current_meta["user_edited"] = True
        if "needs_time" in current_meta and (payload.start_min is not None or payload.end_min is not None):
            current_meta["needs_time"] = False

        meta_json = json.dumps(current_meta)

        with conn:
            conn.execute("""
            UPDATE blocks
            SET date = ?, start_min = ?, end_min = ?, label = ?, tag = ?, meta = ?
            WHERE id = ?
            """, (new_date, new_start, new_end, new_label, new_tag, meta_json, block_id))

        updated_row = conn.execute("SELECT * FROM blocks WHERE id = ?", (block_id,)).fetchone()
        return format_row_block(updated_row)
    finally:
        conn.close()

@app.delete("/api/block/{block_id}")
def delete_block(block_id: int):
    conn = get_db()
    try:
        with conn:
            cursor = conn.execute("DELETE FROM blocks WHERE id = ?", (block_id,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")
        return {"ok": True, "deleted_id": block_id}
    finally:
        conn.close()

# -------------------------------------------------------------------------
# Watched API Endpoints (Strictly independent from timeline blocks)
# -------------------------------------------------------------------------

@app.get("/api/watched/{date}")
def get_watched_for_day(date: str):
    vdate = validate_date(date)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM watched WHERE date = ? ORDER BY id ASC",
            (vdate,)
        ).fetchall()
        return [format_row_watched(r) for r in rows]
    finally:
        conn.close()

@app.post("/api/watched", status_code=status.HTTP_201_CREATED)
def add_watched(payload: WatchedCreateRequest):
    vdate = validate_date(payload.date)
    if not payload.title or not payload.title.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Title cannot be empty")
    if payload.kind not in ["movie", "episode", "other"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="kind must be one of: movie, episode, other"
        )

    meta_json = json.dumps(payload.meta or {})
    jellyfin_id = payload.jellyfin_id if payload.jellyfin_id else None

    conn = get_db()
    try:
        with conn:
            cursor = conn.execute("""
            INSERT INTO watched (date, jellyfin_id, title, series_title, kind, meta)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, jellyfin_id) DO UPDATE SET
                title = excluded.title,
                series_title = excluded.series_title,
                kind = excluded.kind,
                meta = excluded.meta
            """, (
                vdate,
                jellyfin_id,
                payload.title.strip(),
                payload.series_title.strip() if payload.series_title else None,
                payload.kind,
                meta_json
            ))
            row_id = cursor.lastrowid

        row = conn.execute("SELECT * FROM watched WHERE id = ?", (row_id,)).fetchone()
        if not row:
            # In case of ON CONFLICT update where lastrowid is not updated
            row = conn.execute(
                "SELECT * FROM watched WHERE date = ? AND jellyfin_id = ?",
                (vdate, jellyfin_id)
            ).fetchone()
        return format_row_watched(row)
    finally:
        conn.close()

@app.delete("/api/watched/{watched_id}")
def delete_watched(watched_id: int):
    conn = get_db()
    try:
        with conn:
            cursor = conn.execute("DELETE FROM watched WHERE id = ?", (watched_id,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watched entry not found")
        return {"ok": True, "deleted_id": watched_id}
    finally:
        conn.close()

# -------------------------------------------------------------------------
# Jellyfin Proxy Endpoints (Zero API token leaks, graceful 503)
# -------------------------------------------------------------------------

def get_jellyfin_config():
    url = get_setting("JELLYFIN_URL", "").rstrip("/")
    key = get_setting("JELLYFIN_API_KEY", "").strip()
    if not url or not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Jellyfin integration is not configured (missing JELLYFIN_URL or JELLYFIN_API_KEY)."
        )
    return url, key

@app.get("/api/jellyfin/search")
async def jellyfin_search(q: str = Query(..., min_length=1)):
    url, key = get_jellyfin_config()
    target_url = f"{url}/Items"
    params = {
        "searchTerm": q,
        "IncludeItemTypes": "Movie,Series",
        "Recursive": "true",
        "Limit": "20"
    }
    headers = {"X-Emby-Token": key}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(target_url, params=params, headers=headers)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Jellyfin search failed with status {resp.status_code}."
                )
            data = resp.json()
            items = []
            for item in data.get("Items", []):
                items.append({
                    "id": item.get("Id"),
                    "title": item.get("Name"),
                    "year": item.get("ProductionYear"),
                    "type": item.get("Type", "").lower(),  # 'movie' or 'series'
                    "overview": item.get("Overview", "")
                })
            return {"items": items}
    except httpx.RequestError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to reach Jellyfin server."
        )

@app.get("/api/jellyfin/seasons")
async def jellyfin_seasons(series_id: str = Query(...)):
    url, key = get_jellyfin_config()
    target_url = f"{url}/Shows/{series_id}/Seasons"
    headers = {"X-Emby-Token": key}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(target_url, headers=headers)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Jellyfin seasons query failed with status {resp.status_code}."
                )
            data = resp.json()
            seasons = []
            for s in data.get("Items", []):
                seasons.append({
                    "id": s.get("Id"),
                    "name": s.get("Name"),
                    "index": s.get("IndexNumber", 0)
                })
            return {"seasons": seasons}
    except httpx.RequestError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to reach Jellyfin server."
        )

@app.get("/api/jellyfin/episodes")
async def jellyfin_episodes(season_id: str = Query(...), series_id: Optional[str] = None):
    url, key = get_jellyfin_config()
    target_url = f"{url}/Shows/{series_id}/Episodes" if series_id else f"{url}/Items"
    params = {"seasonId": season_id} if series_id else {"parentId": season_id, "IncludeItemTypes": "Episode"}
    headers = {"X-Emby-Token": key}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(target_url, params=params, headers=headers)
            if resp.status_code != 200:
                # Fallback to Items query if Shows/.../Episodes route differed
                fallback_url = f"{url}/Items"
                fallback_params = {"parentId": season_id, "IncludeItemTypes": "Episode"}
                resp = await client.get(fallback_url, params=fallback_params, headers=headers)

            if resp.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Jellyfin episodes query failed with status {resp.status_code}."
                )

            data = resp.json()
            episodes = []
            for ep in data.get("Items", []):
                episodes.append({
                    "id": ep.get("Id"),
                    "name": ep.get("Name"),
                    "index": ep.get("IndexNumber", 0),
                    "season_index": ep.get("ParentIndexNumber", 0)
                })
            return {"episodes": episodes}
    except httpx.RequestError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to reach Jellyfin server."
        )

@app.get("/api/jellyfin/image/{item_id}")
async def jellyfin_image_proxy(item_id: str):
    url, key = get_jellyfin_config()
    target_url = f"{url}/Items/{item_id}/Images/Primary"
    headers = {"X-Emby-Token": key}

    try:
        client = httpx.AsyncClient(timeout=10.0)
        req = client.build_request("GET", target_url, headers=headers)
        resp = await client.send(req, stream=True)
        if resp.status_code != 200:
            await resp.aclose()
            await client.aclose()
            raise HTTPException(status_code=resp.status_code, detail="Image not found on Jellyfin")

        content_type = resp.headers.get("Content-Type", "image/jpeg")

        async def stream_image():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(stream_image(), media_type=content_type)
    except httpx.RequestError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to fetch image from Jellyfin."
        )

# -------------------------------------------------------------------------
# GymTrack Sync Endpoint (Idempotent upsert & safe user_edited protection)
# -------------------------------------------------------------------------

@app.post("/api/sync/gymtrack")
async def sync_gymtrack(
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    vfrom = validate_date(from_date)
    vto = validate_date(to_date)
    if vfrom > vto:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from' date cannot be after 'to' date."
        )

    gymtrack_url = get_setting("GYMTRACK_URL", "").rstrip("/")
    if not gymtrack_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GYMTRACK_URL is not configured."
        )

    target_url = f"{gymtrack_url}/api/log"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(target_url, params={"from": vfrom, "to": vto})
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"GymTrack returned status {resp.status_code}."
                )
            data = resp.json()
    except httpx.RequestError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GymTrack service is unreachable."
        )

    workouts = data.get("workouts", [])

    created_count = 0
    updated_count = 0
    skipped_count = 0
    needs_time_count = 0

    conn = get_db()
    try:
        with conn:
            for w in workouts:
                workout_id = str(w.get("id"))
                w_date = w.get("date")
                name = w.get("name", "Workout")
                seconds = int(w.get("seconds", 0))
                duration_min = max(1, round(seconds / 60))
                started_at = w.get("started_at")

                # Tag determination: 'run' or 'běh' -> Running, else Gym
                if re.search(r"run|běh", name, re.IGNORECASE):
                    tag = "Running"
                else:
                    tag = "Gym"

                # Check if block exists
                existing = conn.execute(
                    "SELECT * FROM blocks WHERE source = 'gymtrack' AND external_id = ?",
                    (workout_id,)
                ).fetchone()

                if existing:
                    existing_meta = parse_meta(existing["meta"])
                    if existing_meta.get("user_edited", False):
                        # User manually adjusted this block; NEVER overwrite start/end times
                        skipped_count += 1
                        continue
                    else:
                        # Sync updates fields
                        if started_at:
                            try:
                                dt = datetime.fromisoformat(started_at)
                                start_min = dt.hour * 60 + dt.minute
                                end_min = min(1440, start_min + duration_min)
                                needs_time = False
                            except ValueError:
                                start_min = existing["start_min"]
                                end_min = existing["end_min"]
                                needs_time = existing_meta.get("needs_time", False)
                        else:
                            start_min = 0
                            end_min = min(1440, duration_min)
                            needs_time = True

                        new_meta = {
                            **existing_meta,
                            "needs_time": needs_time,
                            "seconds": seconds,
                            "workout_name": name,
                            "user_edited": False
                        }
                        conn.execute("""
                        UPDATE blocks
                        SET date = ?, start_min = ?, end_min = ?, label = ?, tag = ?, meta = ?
                        WHERE id = ?
                        """, (
                            w_date,
                            start_min,
                            end_min,
                            name,
                            tag,
                            json.dumps(new_meta),
                            existing["id"]
                        ))
                        updated_count += 1
                        if needs_time:
                            needs_time_count += 1
                else:
                    # New workout
                    if started_at:
                        try:
                            dt = datetime.fromisoformat(started_at)
                            start_min = dt.hour * 60 + dt.minute
                            end_min = min(1440, start_min + duration_min)
                            needs_time = False
                        except ValueError:
                            start_min = 0
                            end_min = min(1440, duration_min)
                            needs_time = True
                    else:
                        start_min = 0
                        end_min = min(1440, duration_min)
                        needs_time = True

                    if needs_time:
                        needs_time_count += 1

                    meta = {
                        "needs_time": needs_time,
                        "seconds": seconds,
                        "workout_name": name,
                        "user_edited": False
                    }

                    conn.execute("""
                    INSERT INTO blocks (date, start_min, end_min, label, tag, source, external_id, meta)
                    VALUES (?, ?, ?, ?, ?, 'gymtrack', ?, ?)
                    """, (
                        w_date,
                        start_min,
                        end_min,
                        name,
                        tag,
                        workout_id,
                        json.dumps(meta)
                    ))
                    created_count += 1

        return {
            "created": created_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "needs_time": needs_time_count
        }
    finally:
        conn.close()

# -------------------------------------------------------------------------
# Steam API & Gaming Endpoints
# -------------------------------------------------------------------------

def get_steam_config():
    key = get_setting("STEAM_API_KEY", "").strip()
    steam_id = get_setting("STEAM_ID", "").strip()
    if not key or not steam_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Steam integration is not configured (missing STEAM_API_KEY or STEAM_ID)."
        )
    return key, steam_id

@app.post("/api/sync/steam")
async def sync_steam():
    key, steam_id = get_steam_config()
    url = "https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/"
    params = {
        "key": key,
        "steamid": steam_id,
        "format": "json"
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Steam API returned HTTP {resp.status_code}"
                )
            data = resp.json()
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Network error connecting to Steam: {exc}"
        )

    games = data.get("response", {}).get("games", [])
    today_str = date.today().isoformat()
    now_iso = datetime.utcnow().isoformat()

    conn = get_db()
    updated_games = []
    try:
        with conn:
            for g in games:
                appid = g.get("appid", 0)
                name = g.get("name", f"App {appid}")
                playtime_forever = g.get("playtime_forever", 0)
                img_icon = g.get("img_icon_url", "")
                icon_url = f"https://media.steampowered.com/steamcommunity/public/images/apps/{appid}/{img_icon}.jpg" if img_icon else None
                header_url = f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg"

                snap = conn.execute("SELECT playtime_forever FROM steam_snapshots WHERE appid = ?", (appid,)).fetchone()
                if snap is not None:
                    delta = playtime_forever - snap["playtime_forever"]
                    if delta > 0:
                        conn.execute("""
                        INSERT INTO steam_plays (date, appid, name, minutes, playtime_forever, icon_url, header_url, meta)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(date, appid) DO UPDATE SET
                            minutes = minutes + excluded.minutes,
                            playtime_forever = excluded.playtime_forever,
                            name = excluded.name,
                            icon_url = excluded.icon_url,
                            header_url = excluded.header_url
                        """, (today_str, appid, name, delta, playtime_forever, icon_url, header_url, json.dumps({})))
                        updated_games.append({"appid": appid, "name": name, "delta_minutes": delta})

                # Always update snapshot
                conn.execute("""
                INSERT INTO steam_snapshots (appid, name, playtime_forever, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(appid) DO UPDATE SET
                    playtime_forever = excluded.playtime_forever,
                    name = excluded.name,
                    updated_at = excluded.updated_at
                """, (appid, name, playtime_forever, now_iso))

        return {
            "synced_games": len(games),
            "updated_today": len(updated_games),
            "games": updated_games
        }
    finally:
        conn.close()

@app.post("/api/steam/play", status_code=status.HTTP_201_CREATED)
def add_steam_play(payload: SteamPlayCreateRequest):
    vdate = validate_date(payload.date)
    if not payload.name or not payload.name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Game name cannot be empty")
    if payload.minutes <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Minutes must be greater than 0")

    appid = payload.appid or 0
    header_url = payload.header_url or (f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg" if appid else None)

    conn = get_db()
    try:
        with conn:
            cursor = conn.execute("""
            INSERT INTO steam_plays (date, appid, name, minutes, playtime_forever, icon_url, header_url, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, appid) DO UPDATE SET
                minutes = minutes + excluded.minutes,
                name = excluded.name,
                icon_url = excluded.icon_url,
                header_url = excluded.header_url
            """, (vdate, appid, payload.name.strip(), payload.minutes, payload.minutes, payload.icon_url, header_url, json.dumps({})))
            row_id = cursor.lastrowid

        row = conn.execute("SELECT * FROM steam_plays WHERE id = ?", (row_id,)).fetchone()
        if not row:
            row = conn.execute("SELECT * FROM steam_plays WHERE date = ? AND appid = ?", (vdate, appid)).fetchone()
        return {
            "id": row["id"],
            "date": row["date"],
            "appid": row["appid"],
            "name": row["name"],
            "minutes": row["minutes"],
            "playtime_forever": row["playtime_forever"],
            "icon_url": row["icon_url"],
            "header_url": row["header_url"],
            "meta": parse_meta(row["meta"])
        }
    finally:
        conn.close()

@app.delete("/api/steam/play/{play_id}")
def delete_steam_play(play_id: int):
    conn = get_db()
    try:
        with conn:
            cursor = conn.execute("DELETE FROM steam_plays WHERE id = ?", (play_id,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Game play record not found")
        return {"ok": True, "deleted_id": play_id}
    finally:
        conn.close()

# -------------------------------------------------------------------------
# Settings API Endpoints
# -------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    steam_key = get_setting("STEAM_API_KEY", "")
    steam_id = get_setting("STEAM_ID", "")
    jf_key = get_setting("JELLYFIN_API_KEY", "")
    jf_url = get_setting("JELLYFIN_URL", "http://jellyfin:8096")
    gt_url = get_setting("GYMTRACK_URL", "http://gymtrack:8000")

    return {
        "steam_configured": bool(steam_key and steam_id),
        "steam_id": steam_id,
        "steam_api_key_masked": f"{steam_key[:4]}...{steam_key[-4:]}" if len(steam_key) >= 8 else ("configured" if steam_key else ""),
        "jellyfin_configured": bool(jf_key),
        "jellyfin_url": jf_url,
        "gymtrack_url": gt_url
    }

@app.post("/api/settings")
def update_settings(payload: SettingsUpdateRequest):
    if payload.steam_api_key is not None:
        set_setting("STEAM_API_KEY", payload.steam_api_key)
    if payload.steam_id is not None:
        set_setting("STEAM_ID", payload.steam_id)
    if payload.jellyfin_api_key is not None:
        set_setting("JELLYFIN_API_KEY", payload.jellyfin_api_key)
    if payload.jellyfin_url is not None:
        set_setting("JELLYFIN_URL", payload.jellyfin_url)
    if payload.gymtrack_url is not None:
        set_setting("GYMTRACK_URL", payload.gymtrack_url)
    return get_settings()

# -------------------------------------------------------------------------
# Statistics API Endpoint
# -------------------------------------------------------------------------

@app.get("/api/stats")
def get_stats(
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    vfrom = validate_date(from_date)
    vto = validate_date(to_date)
    if vfrom > vto:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from' date cannot be after 'to' date."
        )

    conn = get_db()
    try:
        # 1. Fetch days
        days_map = {}
        for row in conn.execute("SELECT * FROM days WHERE date >= ? AND date <= ?", (vfrom, vto)):
            days_map[row["date"]] = {
                "wake_time": row["wake_time"],
                "sleep_time": row["sleep_time"]
            }

        # 2. Fetch blocks
        blocks = [
            format_row_block(r)
            for r in conn.execute("SELECT * FROM blocks WHERE date >= ? AND date <= ?", (vfrom, vto))
        ]

        blocks_by_date = {}
        tag_totals = {tag: 0 for tag in TAGS}
        for b in blocks:
            duration = b["end_min"] - b["start_min"]
            blocks_by_date.setdefault(b["date"], []).append(b)
            if b["tag"] in tag_totals:
                tag_totals[b["tag"]] += duration

        # 3. Iterate all calendar dates in range
        cur = datetime.strptime(vfrom, "%Y-%m-%d").date()
        end_d = datetime.strptime(vto, "%Y-%m-%d").date()

        total_awake_minutes = 0
        sleep_durations = []
        wake_times = []
        empty_days_count = 0
        daily_breakdown = []

        while cur <= end_d:
            d_str = cur.strftime("%Y-%m-%d")
            d_info = days_map.get(d_str, {"wake_time": None, "sleep_time": None})
            w_time = d_info["wake_time"]
            s_time = d_info["sleep_time"]

            day_awake = compute_awake_minutes(w_time, s_time)
            total_awake_minutes += day_awake

            if w_time is not None and s_time is not None:
                sleep_durations.append(1440 - day_awake)
            elif w_time is not None:
                sleep_durations.append(w_time)
            elif s_time is not None:
                sleep_durations.append(1440 - s_time)

            if w_time is not None:
                wake_times.append(w_time)

            d_blocks = blocks_by_date.get(d_str, [])
            if not d_blocks:
                empty_days_count += 1

            day_tag_mins = {t: 0 for t in TAGS}
            day_logged = 0
            for b in d_blocks:
                dur = b["end_min"] - b["start_min"]
                if b["tag"] in day_tag_mins:
                    day_tag_mins[b["tag"]] += dur
                day_logged += dur

            daily_breakdown.append({
                "date": d_str,
                "awake_time": day_awake,
                "logged_time": day_logged,
                "unlogged_time": max(0, day_awake - day_logged),
                "tags": day_tag_mins
            })

            cur += timedelta(days=1)

        # Tag breakdown with percentages of total awake time
        total_logged = sum(tag_totals.values())
        unlogged_minutes = max(0, total_awake_minutes - total_logged)

        tag_stats = []
        for tag in TAGS:
            mins = tag_totals[tag]
            pct = round((mins / total_awake_minutes * 100), 1) if total_awake_minutes > 0 else 0.0
            tag_stats.append({
                "tag": tag,
                "minutes": mins,
                "percentage": pct,
                "color": TAG_COLORS.get(tag, "#cbd5e1")
            })

        # Sort tags descending by minutes
        tag_stats.sort(key=lambda x: x["minutes"], reverse=True)

        unlogged_pct = round((unlogged_minutes / total_awake_minutes * 100), 1) if total_awake_minutes > 0 else 0.0

        # Sleep averages
        avg_sleep_min = round(sum(sleep_durations) / len(sleep_durations)) if sleep_durations else None
        avg_wake_min = round(sum(wake_times) / len(wake_times)) if wake_times else None

        # 4. Watched items aggregation (STRICTLY separate from time stats!)
        watched_rows = conn.execute(
            "SELECT * FROM watched WHERE date >= ? AND date <= ? ORDER BY date DESC, id ASC",
            (vfrom, vto)
        ).fetchall()

        movies = []
        series_map = {}
        other_items = []

        for row in watched_rows:
            w_item = format_row_watched(row)
            k = w_item["kind"]
            if k == "movie":
                movies.append(w_item)
            elif k == "episode":
                s_title = w_item["series_title"] or "Unknown Series"
                if s_title not in series_map:
                    series_map[s_title] = {
                        "series_title": s_title,
                        "episode_count": 0,
                        "episodes": []
                    }
                series_map[s_title]["episode_count"] += 1
                series_map[s_title]["episodes"].append(w_item)
            else:
                other_items.append(w_item)

        series_list = list(series_map.values())
        series_list.sort(key=lambda s: s["episode_count"], reverse=True)

        # 5. Steam plays aggregation
        steam_play_rows = conn.execute("""
            SELECT appid, name, icon_url, header_url, SUM(minutes) as total_minutes
            FROM steam_plays
            WHERE date >= ? AND date <= ?
            GROUP BY appid
            ORDER BY total_minutes DESC
        """, (vfrom, vto)).fetchall()

        steam_stats_games = [
            {
                "appid": r["appid"],
                "name": r["name"],
                "minutes": r["total_minutes"],
                "icon_url": r["icon_url"],
                "header_url": r["header_url"]
            }
            for r in steam_play_rows
        ]
        total_steam_mins = sum(g["minutes"] for g in steam_stats_games)

        return {
            "period": {"from": vfrom, "to": vto},
            "total_awake_minutes": total_awake_minutes,
            "total_logged_minutes": total_logged,
            "unlogged_minutes": unlogged_minutes,
            "unlogged_percentage": unlogged_pct,
            "tags": tag_stats,
            "sleep": {
                "avg_sleep_minutes": avg_sleep_min,
                "avg_wake_min": avg_wake_min,
                "recorded_days": len(sleep_durations)
            },
            "empty_days_count": empty_days_count,
            "daily_breakdown": daily_breakdown,
            "watched": {
                "total_movies": len(movies),
                "total_episodes": sum(s["episode_count"] for s in series_list),
                "total_other": len(other_items),
                "series": series_list,
                "movies": movies,
                "other": other_items
            },
            "steam": {
                "total_minutes": total_steam_mins,
                "games": steam_stats_games
            }
        }
    finally:
        conn.close()

# -------------------------------------------------------------------------
# Static Frontend Serving
# -------------------------------------------------------------------------

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/", response_class=HTMLResponse)
def serve_index():
    index_path = os.path.join(static_dir, "index.html")
    if not os.path.exists(index_path):
        return HTMLResponse("<h1>DayLog</h1><p>Frontend static/index.html is loading...</p>")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

