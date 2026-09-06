# DayLog — Retrospective Diary Calendar

DayLog is a retrospective daily calendar application. Rather than planning future tasks, DayLog records what you **actually did**, allowing you to look back after a week or a month and see where your time truly went.

## Tech Stack & Architecture

- **Backend**: Python 3.12 + FastAPI with standard library `sqlite3` (no ORM).
- **Frontend**: A single standalone file `static/index.html` with inline CSS and Vanilla JS.
  - Zero build steps, zero npm dependencies, no external JavaScript libraries.
  - Typography: **Archivo** (headings) + **IBM Plex Sans** (body text).
  - Modern glassmorphism dark theme with blur panels, subtle borders, and smooth animations.
  - Mobile-first touch-friendly design.
  - Dynamic base path resolution supporting reverse proxies and subpaths.
- **Port**: 8107 (container port `8107:8000`).
- **Database**: Default `/data/daylog.db` (configured via `DAYLOG_DB`).
- **Container User**: Runs securely as `1000:1000`.

---

## Core Concepts

1. **Retrospective Logging**: You log what actually happened. Unlogged gaps on the timeline are completely normal and displayed as a primary metric (`Unlogged: Xh Ym`).
2. **Day Boundaries & Sleep**:
   - `wake_time` and `sleep_time` are not blocks on the timeline; they establish the boundaries of your waking day.
   - Time before waking and after going to bed is visually dimmed on the 24-hour timeline.
   - Total awake time = `sleep_time - wake_time` (or `sleep_time + 1440 - wake_time` when sleeping past midnight).
   - Awake time is the denominator for all statistics and percentages.
3. **Tags**:
   - Fixed categories: `Work`, `Gaming`, `Chillin`, `Study`, `Gym`, `Running`.
   - Distinct colors assigned to each tag.
4. **Watched (Media Log)**:
   - Standalone daily log of movies and episodes.
   - **Never** converted into timeline blocks, keeping watch time from double-counting with `Chillin`.
   - Proxies Jellyfin search, seasons, episodes, and poster images without ever leaking API tokens.
5. **GymTrack Synchronization**:
   - Idempotent sync via `GET /api/log` on `GYMTRACK_URL`.
   - Auto-categorizes workouts (`Running` if name contains "run"/"běh", otherwise `Gym`).
   - Workouts without `started_at` are flagged with `needs_time: true` for manual placement.
   - User-edited blocks (`user_edited: true`) are never overwritten by subsequent syncs.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DAYLOG_PORT` | `8107` | Host port the container is published on |
| `DAYLOG_UID` | `1000` | User ID that owns `./data` |
| `DAYLOG_GID` | `1000` | Group ID that owns `./data` |
| `DAYLOG_DB` | `/data/daylog.db` | Path to SQLite database file |
| `JELLYFIN_URL` | *(empty)* | Base URL to Jellyfin instance (e.g. `http://127.0.0.1:8096`) |
| `JELLYFIN_API_KEY` | *(empty)* | Secret Jellyfin API token |
| `GYMTRACK_URL` | *(empty)* | Base URL to GymTrack instance (e.g. `http://127.0.0.1:8101`) |

Copy `.env.example` to `.env` to override configuration:
```bash
cp .env.example .env
```

## Data Storage

DayLog persists all database records (days, blocks, watched titles) into `./data/daylog.db`, mounted to `/data/daylog.db` inside the container.

---

## Deployment

```bash
docker compose up -d --build
```

Then open `http://<host>:8107/` in your browser.

---

## Running Locally

1. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Or on Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. Run the application:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8107
   ```

---

## REST API Summary

- `GET /` — Serves the frontend single-page application (`static/index.html`)
- `GET /health` — Service health check
- `GET /api/tags` — Available tags and assigned color codes
- `GET /api/day/{date}` — Retrieve day details, wake/sleep boundaries, unlogged time, and blocks
- `POST /api/day/{date}` — Update wake time, sleep time, or day reflection note
- `GET /api/range?from=...&to=...` — Retrieve multiple days and blocks for calendar ranges
- `POST /api/block` — Create a new activity block
- `PATCH /api/block/{id}` — Update block label, times, or tag (sets `meta.user_edited = true`)
- `DELETE /api/block/{id}` — Remove an activity block
- `GET /api/watched/{date}` — Get watched movies/episodes for a date
- `POST /api/watched` — Add watched item (Jellyfin or manual entry)
- `DELETE /api/watched/{id}` — Remove watched item
- `GET /api/stats?from=...&to=...` — Aggregated tag statistics, sleep averages, unlogged time, and watched summary
- `POST /api/sync/gymtrack?from=...&to=...` — Synchronize workouts from GymTrack
- `GET /api/jellyfin/search?q=...` — Proxied Jellyfin search
- `GET /api/jellyfin/seasons?series_id=...` — Proxied seasons list
- `GET /api/jellyfin/episodes?season_id=...` — Proxied episodes list
- `GET /api/jellyfin/image/{item_id}` — Proxied poster image stream

---

## Automated Smoke Tests

Run the comprehensive test suite verifying schema, block lifecycle, validation rules, midnight sleep calculations, unlogged math, GymTrack idempotency, and Jellyfin fallback:

```bash
python tests/smoke.py
```

---

## License

Released under the MIT License — see [LICENSE](LICENSE).

