import os
import sys
import json
import sqlite3
import tempfile
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# Set up path so main can be imported from root
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

# Set up temporary database environment BEFORE importing main
temp_dir = tempfile.TemporaryDirectory()
temp_db_path = os.path.join(temp_dir.name, "daylog_test.db")
os.environ["DAYLOG_DB"] = temp_db_path
os.environ["JELLYFIN_API_KEY"] = ""  # Explicitly empty for Jellyfin test
os.environ["JELLYFIN_URL"] = ""

# Start Fake GymTrack server on ephemeral port
class FakeGymTrackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/log"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = {
                "workouts": [
                    {
                        "id": 42,
                        "date": "2026-09-07",
                        "name": "Morning Outdoor Run",
                        "seconds": 3600,
                        "started_at": "2026-09-07T07:30:00"
                    },
                    {
                        "id": 43,
                        "date": "2026-09-07",
                        "name": "Full-body Heavy Circuit",
                        "seconds": 5400,
                        "started_at": None
                    }
                ]
            }
            self.wfile.write(json.dumps(data).encode("utf-8"))
        elif self.path == "/Users":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = [
                {"Id": "u-admin", "Name": "mordor67", "Policy": {"IsAdministrator": True}},
                {"Id": "u-bubu", "Name": "bubu", "Policy": {"IsAdministrator": False}}
            ]
            self.wfile.write(json.dumps(data).encode("utf-8"))
        elif self.path.startswith("/Users/") and "/Items" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = {
                "Items": [
                    {
                        "Id": "jf-item-101",
                        "Name": "OutKast",
                        "Type": "Episode",
                        "SeriesName": "Lanterns",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 3,
                        "UserData": {
                            "LastPlayedDate": "2026-09-06T12:56:41.0000000Z"
                        }
                    },
                    {
                        "Id": "jf-item-102",
                        "Name": "Supergirl",
                        "Type": "Movie",
                        "ProductionYear": 2026,
                        "UserData": {
                            "LastPlayedDate": "2026-09-06T09:00:34.0000000Z"
                        }
                    }
                ]
            }
            self.wfile.write(json.dumps(data).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

fake_server = HTTPServer(("127.0.0.1", 0), FakeGymTrackHandler)
server_port = fake_server.server_port
os.environ["GYMTRACK_URL"] = f"http://127.0.0.1:{server_port}"

server_thread = threading.Thread(target=fake_server.serve_forever, daemon=True)
server_thread.start()

# Now import FastAPI test client & app
from starlette.testclient import TestClient
import main
from main import app, init_db

init_db()
client = TestClient(app)

def print_section(title: str):
    print("\n" + "=" * 70)
    print(f"=== {title}")
    print("=" * 70)

def main_test():
    print("Starting DayLog Comprehensive Smoke Test Suite...")

    # ---------------------------------------------------------------------
    # 1. Repository Schema & Table Info
    # ---------------------------------------------------------------------
    print_section("1. SQLite Schema Verification (PRAGMA table_info)")
    conn = sqlite3.connect(temp_db_path)
    conn.row_factory = sqlite3.Row
    for table_name in ["days", "blocks", "watched", "steam_plays", "steam_snapshots", "settings"]:
        cols = conn.execute(f"PRAGMA table_info({table_name});").fetchall()
        print(f"\nTable: {table_name}")
        for c in cols:
            print(f"  - col #{c['cid']}: {c['name']} ({c['type']}) {'NOT NULL' if c['notnull'] else 'NULL'} PK={c['pk']}")
    conn.close()

    # ---------------------------------------------------------------------
    # 2. Block Lifecycle (POST -> GET -> PATCH -> GET -> DELETE -> GET)
    # ---------------------------------------------------------------------
    print_section("2. Block Lifecycle (POST -> GET -> PATCH -> GET -> DELETE -> GET)")
    test_date = "2026-09-05"

    # POST block
    post_data = {
        "date": test_date,
        "start_min": 540,  # 09:00
        "end_min": 660,    # 11:00
        "label": "Deep work session",
        "tag": "Work",
        "source": "manual"
    }
    r_post = client.post("/api/block", json=post_data)
    print(f"POST /api/block -> HTTP {r_post.status_code}")
    print(f"Response: {r_post.json()}")
    assert r_post.status_code == 201
    block_id = r_post.json()["id"]

    # GET day (verify block is present)
    r_get1 = client.get(f"/api/day/{test_date}")
    print(f"\nGET /api/day/{test_date} -> HTTP {r_get1.status_code}")
    day_blocks = r_get1.json()["blocks"]
    assert len(day_blocks) == 1
    assert day_blocks[0]["id"] == block_id
    assert day_blocks[0]["start_min"] == 540
    print(f"Day contains block: {day_blocks[0]}")

    # PATCH block (update times and label)
    patch_data = {
        "start_min": 600,  # 10:00
        "end_min": 720,    # 12:00
        "label": "Updated deep work session"
    }
    r_patch = client.patch(f"/api/block/{block_id}", json=patch_data)
    print(f"\nPATCH /api/block/{block_id} -> HTTP {r_patch.status_code}")
    patch_res = r_patch.json()
    print(f"Response: {patch_res}")
    assert r_patch.status_code == 200
    assert patch_res["start_min"] == 600
    assert patch_res["meta"].get("user_edited") is True

    # GET day (verify patch reflected and meta.user_edited is True)
    r_get2 = client.get(f"/api/day/{test_date}")
    print(f"\nGET /api/day/{test_date} -> HTTP {r_get2.status_code}")
    assert r_get2.json()["blocks"][0]["start_min"] == 600
    assert r_get2.json()["blocks"][0]["meta"]["user_edited"] is True
    print("Verified meta.user_edited is True")

    # DELETE block
    r_del = client.delete(f"/api/block/{block_id}")
    print(f"\nDELETE /api/block/{block_id} -> HTTP {r_del.status_code}")
    assert r_del.status_code == 200

    # GET day (verify block is gone)
    r_get3 = client.get(f"/api/day/{test_date}")
    print(f"\nGET /api/day/{test_date} -> HTTP {r_get3.status_code}")
    assert len(r_get3.json()["blocks"]) == 0
    print("Verified block successfully deleted from day")

    # ---------------------------------------------------------------------
    # 3. Validation Tests
    # ---------------------------------------------------------------------
    print_section("3. Validation (Unknown tag, end_min < start_min, invalid date format)")
    
    # 3a. Unknown tag
    r_bad_tag = client.post("/api/block", json={
        "date": "2026-09-05",
        "start_min": 600,
        "end_min": 660,
        "label": "Invalid activity",
        "tag": "UnknownCategory"
    })
    print(f"POST unknown tag -> HTTP {r_bad_tag.status_code} {r_bad_tag.json()}")
    assert r_bad_tag.status_code == 400

    # 3b. end_min <= start_min
    r_bad_time = client.post("/api/block", json={
        "date": "2026-09-05",
        "start_min": 700,
        "end_min": 600,
        "label": "Time reversal",
        "tag": "Work"
    })
    print(f"POST end_min < start_min -> HTTP {r_bad_time.status_code} {r_bad_time.json()}")
    assert r_bad_time.status_code == 400

    # 3c. GET /api/stats?from=blabla
    r_bad_date = client.get("/api/stats?from=blabla&to=2026-09-05")
    print(f"GET /api/stats?from=blabla -> HTTP {r_bad_date.status_code} {r_bad_date.json()}")
    assert r_bad_date.status_code == 400

    # ---------------------------------------------------------------------
    # 4. Sleep Past Midnight Calculation
    # ---------------------------------------------------------------------
    print_section("4. Sleep Past Midnight (wake_time=480, sleep_time=90 -> awake_time=1050)")
    midnight_date = "2026-09-06"
    r_midnight_day = client.post(f"/api/day/{midnight_date}", json={
        "wake_time": 480,   # 08:00
        "sleep_time": 90,   # 01:30 (past midnight)
        "note": "Stayed up late coding"
    })
    print(f"POST /api/day/{midnight_date} -> HTTP {r_midnight_day.status_code}")
    print(f"Day data: {r_midnight_day.json()}")
    assert r_midnight_day.status_code == 200
    assert r_midnight_day.json()["awake_time"] == 1050

    r_midnight_stats = client.get(f"/api/stats?from={midnight_date}&to={midnight_date}")
    print(f"GET /api/stats -> HTTP {r_midnight_stats.status_code}")
    stats_data = r_midnight_stats.json()
    print(f"Total awake minutes in stats: {stats_data['total_awake_minutes']}")
    assert stats_data["total_awake_minutes"] == 1050
    assert stats_data["total_awake_minutes"] > 0, "Awake time must never be negative!"

    # ---------------------------------------------------------------------
    # 5. Unlogged Time Verification
    # ---------------------------------------------------------------------
    print_section("5. Unlogged Time Calculation (16h awake = 960m, 2h block = 120m -> 840m unlogged)")
    unlogged_date = "2026-09-08"
    # Set 16h awake: wake 07:00 (420), sleep 23:00 (1380) -> 1380 - 420 = 960 min (16h)
    client.post(f"/api/day/{unlogged_date}", json={
        "wake_time": 420,
        "sleep_time": 1380,
        "note": "Unlogged test day"
    })
    # Add a single 2-hour (120 min) block
    client.post("/api/block", json={
        "date": unlogged_date,
        "start_min": 600,  # 10:00
        "end_min": 720,    # 12:00
        "label": "2 hour study session",
        "tag": "Study"
    })

    r_unlogged_stats = client.get(f"/api/stats?from={unlogged_date}&to={unlogged_date}")
    st = r_unlogged_stats.json()
    print(f"GET /api/stats -> HTTP {r_unlogged_stats.status_code}")
    print(f"Total awake: {st['total_awake_minutes']}m, Logged: {st['total_logged_minutes']}m, Unlogged: {st['unlogged_minutes']}m")
    assert st["total_awake_minutes"] == 960
    assert st["total_logged_minutes"] == 120
    assert st["unlogged_minutes"] == 840
    print(f"Study tag pct: {st['tags'][0]['percentage']}%, Unlogged pct: {st['unlogged_percentage']}%")
    # Percentage check
    total_pct = sum(t["percentage"] for t in st["tags"]) + st["unlogged_percentage"]
    print(f"Sum of percentages: {total_pct}%")
    assert abs(total_pct - 100.0) <= 0.2

    # ---------------------------------------------------------------------
    # 6. GymTrack Sync Idempotence & user_edited Protection
    # ---------------------------------------------------------------------
    print_section("6. GymTrack Sync Idempotence & user_edited Protection")
    sync_date = "2026-09-07"

    # First sync
    r_sync1 = client.post(f"/api/sync/gymtrack?from={sync_date}&to={sync_date}")
    print(f"First POST /api/sync/gymtrack -> HTTP {r_sync1.status_code} {r_sync1.json()}")
    assert r_sync1.status_code == 200
    assert r_sync1.json()["created"] == 2
    assert r_sync1.json()["needs_time"] == 1

    # Repeat sync (must create 0 new blocks, idempotent)
    r_sync2 = client.post(f"/api/sync/gymtrack?from={sync_date}&to={sync_date}")
    print(f"\nRepeat POST /api/sync/gymtrack -> HTTP {r_sync2.status_code} {r_sync2.json()}")
    assert r_sync2.status_code == 200
    assert r_sync2.json()["created"] == 0, "Repeat sync must NOT duplicate workouts!"

    # GET day: verify exactly 2 blocks exist, not 4
    r_gym_day = client.get(f"/api/day/{sync_date}")
    gym_blocks = r_gym_day.json()["blocks"]
    print(f"\nGET /api/day/{sync_date} -> Found {len(gym_blocks)} blocks (expected 2)")
    assert len(gym_blocks) == 2

    # Verify workout 43 has needs_time: true
    b_needs_time = next(b for b in gym_blocks if b["external_id"] == "43")
    print(f"Workout 43 without started_at meta: {b_needs_time['meta']}")
    assert b_needs_time["meta"]["needs_time"] is True
    assert b_needs_time["tag"] == "Gym"

    # Verify workout 42 with run in name has tag Running
    b_run = next(b for b in gym_blocks if b["external_id"] == "42")
    print(f"Workout 42 ('run') tag: {b_run['tag']}")
    assert b_run["tag"] == "Running"

    # Manually adjust workout 43's time via PATCH
    r_adjust = client.patch(f"/api/block/{b_needs_time['id']}", json={
        "start_min": 1020,  # 17:00
        "end_min": 1110     # 18:30
    })
    print(f"\nPATCH /api/block/{b_needs_time['id']} -> HTTP {r_adjust.status_code}")
    adjusted_block = r_adjust.json()
    assert adjusted_block["start_min"] == 1020
    assert adjusted_block["meta"]["user_edited"] is True
    print(f"Adjusted block: {adjusted_block}")

    # Run sync a third time
    r_sync3 = client.post(f"/api/sync/gymtrack?from={sync_date}&to={sync_date}")
    print(f"\nThird POST /api/sync/gymtrack -> HTTP {r_sync3.status_code} {r_sync3.json()}")
    assert r_sync3.json()["skipped"] == 1

    # Verify user-edited times were NOT overwritten
    r_gym_day3 = client.get(f"/api/day/{sync_date}")
    b_check = next(b for b in r_gym_day3.json()["blocks"] if b["external_id"] == "43")
    print(f"Workout 43 times after 3rd sync: start={b_check['start_min']}, end={b_check['end_min']}")
    assert b_check["start_min"] == 1020
    assert b_check["end_min"] == 1110

    # ---------------------------------------------------------------------
    # 7. Watched Separation from Timeline & Series Grouping
    # ---------------------------------------------------------------------
    print_section("7. Watched Separation from Timeline & Series Grouping")
    watched_date = "2026-09-09"

    # Set day boundaries: wake 480, sleep 1440 -> 960m awake
    client.post(f"/api/day/{watched_date}", json={"wake_time": 480, "sleep_time": 1440})
    # Add a 1h Work block
    client.post("/api/block", json={
        "date": watched_date,
        "start_min": 540,
        "end_min": 600,
        "label": "Morning work",
        "tag": "Work"
    })

    # Stats before adding watched
    stats_before = client.get(f"/api/stats?from={watched_date}&to={watched_date}").json()
    logged_before = stats_before["total_logged_minutes"]
    unlogged_before = stats_before["unlogged_minutes"]

    # Add 1 movie, 2 episodes of a series, and 1 manual item
    client.post("/api/watched", json={
        "date": watched_date,
        "jellyfin_id": "jf-m-1",
        "title": "Interstellar",
        "series_title": None,
        "kind": "movie",
        "meta": {"year": 2014}
    })
    client.post("/api/watched", json={
        "date": watched_date,
        "jellyfin_id": "jf-ep-1",
        "title": "Severance S01E01 - Good News About Hell",
        "series_title": "Severance",
        "kind": "episode",
        "meta": {"season": 1, "episode": 1}
    })
    client.post("/api/watched", json={
        "date": watched_date,
        "jellyfin_id": "jf-ep-2",
        "title": "Severance S01E02 - Half Loop",
        "series_title": "Severance",
        "kind": "episode",
        "meta": {"season": 1, "episode": 2}
    })
    client.post("/api/watched", json={
        "date": watched_date,
        "jellyfin_id": None,
        "title": "Veritasium YouTube Documentary",
        "series_title": None,
        "kind": "other",
        "meta": {}
    })

    # Stats after adding watched
    stats_after = client.get(f"/api/stats?from={watched_date}&to={watched_date}").json()
    logged_after = stats_after["total_logged_minutes"]
    unlogged_after = stats_after["unlogged_minutes"]

    print(f"Logged time before: {logged_before}m | after: {logged_after}m")
    print(f"Unlogged time before: {unlogged_before}m | after: {unlogged_after}m")
    assert logged_before == logged_after, "Watched items MUST NOT alter timeline logged time!"
    assert unlogged_before == unlogged_after, "Watched items MUST NOT alter unlogged time!"

    watched_stats = stats_after["watched"]
    print(f"\nWatched stats: {json.dumps(watched_stats, indent=2)}")
    assert watched_stats["total_movies"] == 1
    assert watched_stats["total_episodes"] == 2
    assert watched_stats["total_other"] == 1
    assert len(watched_stats["series"]) == 1
    assert watched_stats["series"][0]["series_title"] == "Severance"
    assert watched_stats["series"][0]["episode_count"] == 2
    print("Verified series episodes correctly grouped under 'Severance' with count 2")

    # ---------------------------------------------------------------------
    # 8. Jellyfin Unconfigured / Missing Key Fallback (503 & Graceful Operation)
    # ---------------------------------------------------------------------
    print_section("8. Jellyfin Without Key (503 Graceful Fallback)")
    # Request Jellyfin search without JELLYFIN_API_KEY
    r_jf = client.get("/api/jellyfin/search?q=batman")
    print(f"GET /api/jellyfin/search?q=batman -> HTTP {r_jf.status_code} {r_jf.json()}")
    assert r_jf.status_code == 503

    # Normal day endpoint works completely fine
    r_day_check = client.get(f"/api/day/{watched_date}")
    print(f"GET /api/day/{watched_date} -> HTTP {r_day_check.status_code}")
    assert r_day_check.status_code == 200

    # Manual watched POST works completely fine
    r_manual = client.post("/api/watched", json={
        "date": watched_date,
        "title": "Local Cinema Movie",
        "kind": "movie"
    })
    print(f"Manual POST /api/watched -> HTTP {r_manual.status_code} {r_manual.json()}")
    assert r_manual.status_code == 201

    # ---------------------------------------------------------------------
    # 9. Independent Wake/Sleep & Past-Midnight Blocks
    # ---------------------------------------------------------------------
    print_section("9. Independent Wake/Sleep & Past-Midnight Blocks")
    indep_date1 = "2026-09-10"
    indep_date2 = "2026-09-11"

    # 9a. Only wake_time set (morning sleep excluded immediately)
    r_wake_only = client.post(f"/api/day/{indep_date1}", json={
        "wake_time": 480,  # 08:00
        "sleep_time": None
    })
    print(f"Wake only -> awake_time: {r_wake_only.json()['awake_time']}m (expected 960m)")
    assert r_wake_only.json()["awake_time"] == 960

    # 9b. Only sleep_time set (night sleep excluded immediately)
    r_sleep_only = client.post(f"/api/day/{indep_date2}", json={
        "wake_time": None,
        "sleep_time": 1380  # 23:00
    })
    print(f"Sleep only -> awake_time: {r_sleep_only.json()['awake_time']}m (expected 1380m)")
    assert r_sleep_only.json()["awake_time"] == 1380

    # 9c. Past-midnight block (e.g. 23:30 to 01:30 next day -> 1410 to 1530)
    r_late_block = client.post("/api/block", json={
        "date": indep_date1,
        "start_min": 1410,
        "end_min": 1530,
        "label": "Late night gaming session",
        "tag": "Gaming"
    })
    print(f"Late night block -> HTTP {r_late_block.status_code}, duration: {r_late_block.json()['end_min'] - r_late_block.json()['start_min']}m")
    assert r_late_block.status_code == 201
    assert r_late_block.json()["end_min"] == 1530

    # ---------------------------------------------------------------------
    # 10. Steam Gaming Integration & Settings
    # ---------------------------------------------------------------------
    print_section("10. Steam Gaming Integration & Settings")
    steam_date = "2026-09-12"

    # 10a. Sync without key -> 503
    r_sync_nokey = client.post("/api/sync/steam")
    print(f"POST /api/sync/steam (no key) -> HTTP {r_sync_nokey.status_code}")
    assert r_sync_nokey.status_code == 503

    # 10b. Settings endpoint
    r_settings = client.post("/api/settings", json={
        "steam_id": "76561198144801984"
    })
    assert r_settings.status_code == 200
    assert r_settings.json()["steam_id"] == "76561198144801984"

    # 10c. Manual Steam play entry
    r_play = client.post("/api/steam/play", json={
        "date": steam_date,
        "name": "Counter-Strike 2",
        "minutes": 90,
        "appid": 730
    })
    print(f"POST /api/steam/play -> HTTP {r_play.status_code} {r_play.json()['name']} ({r_play.json()['minutes']}m)")
    assert r_play.status_code == 201
    play_id = r_play.json()["id"]

    # 10d. Verify in GET /api/day/{date}
    r_day_steam = client.get(f"/api/day/{steam_date}")
    assert r_day_steam.status_code == 200
    games = r_day_steam.json().get("steam_games", [])
    print(f"GET /api/day/{steam_date} -> found {len(games)} games")
    assert len(games) == 1
    assert games[0]["name"] == "Counter-Strike 2"
    assert games[0]["minutes"] == 90

    # 10e. Verify in GET /api/stats
    r_stats_steam = client.get(f"/api/stats?from={steam_date}&to={steam_date}")
    assert r_stats_steam.status_code == 200
    steam_stats = r_stats_steam.json().get("steam", {})
    print(f"GET /api/stats -> Steam total minutes: {steam_stats.get('total_minutes')}")
    assert steam_stats["total_minutes"] == 90
    assert len(steam_stats["games"]) == 1

    # 10f. Delete play entry
    r_del_play = client.delete(f"/api/steam/play/{play_id}")
    assert r_del_play.status_code == 200

    r_day_steam2 = client.get(f"/api/day/{steam_date}")
    assert len(r_day_steam2.json().get("steam_games", [])) == 0

    # ---------------------------------------------------------------------
    # 11. Jellyfin Users & Automated Playback History Sync
    # ---------------------------------------------------------------------
    print_section("11. Jellyfin Users & Automated Playback History Sync")
    
    # Configure fake server as Jellyfin endpoint
    client.post("/api/settings", json={
        "jellyfin_url": f"http://127.0.0.1:{server_port}",
        "jellyfin_api_key": "test-key"
    })

    # 11a. Fetch Jellyfin users
    r_users = client.get("/api/jellyfin/users")
    print(f"GET /api/jellyfin/users -> HTTP {r_users.status_code}")
    assert r_users.status_code == 200
    users_list = r_users.json().get("users", [])
    print(f"Users found: {[u['name'] for u in users_list]}")
    assert len(users_list) == 2
    assert users_list[0]["name"] == "mordor67"
    assert users_list[0]["is_admin"] is True

    # 11b. First Sync -> 2 items created (Lanterns episode & Supergirl movie)
    r_jf_sync1 = client.post("/api/sync/jellyfin")
    print(f"First POST /api/sync/jellyfin -> HTTP {r_jf_sync1.status_code} {r_jf_sync1.json()}")
    assert r_jf_sync1.status_code == 200
    assert r_jf_sync1.json()["created"] == 2
    assert r_jf_sync1.json()["user"] == "mordor67"

    # 11c. Idempotence / Repeat Sync -> 0 items created
    r_jf_sync2 = client.post("/api/sync/jellyfin")
    print(f"Repeat POST /api/sync/jellyfin -> HTTP {r_jf_sync2.status_code} {r_jf_sync2.json()}")
    assert r_jf_sync2.status_code == 200
    assert r_jf_sync2.json()["created"] == 0

    # 11d. Verify in GET /api/watched/2026-09-06
    r_watched_sync = client.get("/api/watched/2026-09-06")
    assert r_watched_sync.status_code == 200
    watched_items = r_watched_sync.json()
    print(f"GET /api/watched/2026-09-06 -> {len(watched_items)} items")
    titles = [w["title"] for w in watched_items]
    print(f"Watched titles: {titles}")
    assert any("Lanterns" in t for t in titles)
    assert any("Supergirl" in t for t in titles)

    print("\n" + "=" * 70)
    print("ALL VERIFICATION REQUIREMENTS SUCCESSFULLY PASSED!")
    print("=" * 70)

if __name__ == "__main__":
    main_test()

