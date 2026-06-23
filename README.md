# NCAAF Betting Data Pipeline

ESPN College Football play-by-play (PBP) scraper and a derived **Supabase feature
store** for live betting. It scrapes every FBS regular-season game (plus
conference championships) from ESPN's JSON API — no Selenium — backfills gaps
from the [CollegeFootballData](https://collegefootballdata.com) (CFBD) API, and
rolls the play data up into quarter- and drive-level feature tables in Postgres.

Covers the **2023, 2024, and 2025** seasons — 464,692 plays across ~2,630 games.

## Architecture

```
ESPN JSON API ──┐
                ├─► per-game CSVs ──► merged season CSV ──► feature tables ──► Supabase
CFBD API ───────┘   (data/games/)     (data/pbp_YYYY...)    (quarter_log,       (Postgres)
  (fallback +                                                 drive_log)
   coaches / records)
```

1. **Discover** FBS game IDs per season (ESPN core API; conferences are looked up
   dynamically, so realignment is handled automatically).
2. **Scrape** play-by-play per game → `data/games/pbp_{game_id}.csv` (checkpointed).
3. **Backfill** ESPN no-data games from CFBD.
4. **Merge** per-game files → `data/pbp_{season}_regular.csv` (adds coaches, game URL).
5. **Build features** → `quarter_log` and `drive_log` tables in Supabase.

## Components

| Script | Purpose |
|--------|---------|
| `espn_cfb_pbp_scraper.py` | Single-game scraper + universal player-name parser (handles ESPN's many play-description formats). Retries transient 5xx with exponential backoff. |
| `espn_cfb_batch_scraper.py` | Full-season discovery, scrape, and merge. Checkpoint-driven and resumable. |
| `cfbd_fallback.py` | Backfills games ESPN has no PBP for, from the CFBD API. |
| `reparse_players.py` | Re-derives player/scoring columns from stored play text without re-scraping. |
| `build_coaches.py` | Per-game home/away coach map from CFBD (resolves mid-season changes by date). |
| `build_game_context.py` | Per-game date, final score, and point-in-time (season-to-date) team & coach W/L records. |
| `build_quarter_log.py` | Quarter-level feature table (one row per team-game-quarter). |
| `build_drive_log.py` | Drive-level feature table (one row per drive). |
| `setup_db.py` | Idempotent Supabase schema creation (`quarter_log`, `drive_log`). |
| `update_season.py` | Daily in-season orchestrator (discover → scrape → backfill → merge → features). |
| `run_daily_update.bat` | Windows Task Scheduler wrapper. |

## Feature tables (Supabase Postgres)

- **`quarter_log`** — one row per (season, game_id, team, quarter). Symmetric
  "For" (offense) / "Against" (defense) blocks: drives, points, yards, time of
  possession, red-zone trips, explosive plays, turnovers, and more.
- **`drive_log`** — one row per drive: result, start/end field position, yards,
  play counts, pass/run split, 3rd-down conversions, time of possession, points,
  red-zone, explosive plays, sacks, penalties.

Both carry point-in-time team & coach records and the score/margin entering each
segment. Scoring is attributed to the wall-clock quarter (matching sportsbook
settlement); drive structure is attributed to the quarter the drive started.

## Setup

Requires Python 3.10+.

```bash
pip install pandas requests python-dotenv sqlalchemy psycopg2-binary
cp .env.example .env   # then fill in your keys
```

`.env` variables:
- `CFBD_API_KEY` — free key from <https://collegefootballdata.com/key>
- `SUPABASE_DB_URL` — Supabase Postgres connection string
  (Dashboard → Project Settings → Database → Connection string → URI)

Create the tables once:

```bash
python setup_db.py
```

## Usage

```bash
# Initial full scrape of all seasons (checkpointed; safe to interrupt and resume)
python espn_cfb_batch_scraper.py

# Merge per-game CSVs into season files
python espn_cfb_batch_scraper.py --merge

# Build feature tables -> Supabase
python build_game_context.py
python build_quarter_log.py
python build_drive_log.py

# Daily in-season update (auto-detects the current season; idempotent)
python update_season.py
```

### Daily automation

`run_daily_update.bat` is wired to a Windows Task Scheduler job that runs
`update_season.py` each morning. It auto-detects the current season, re-discovers
newly scheduled games, scrapes completed games, backfills, and rebuilds the
feature tables — all idempotent and checkpoint-driven, so a missed run self-heals
(it captures every completed-but-uncaptured game, not just yesterday's).

## Notes

- **Scope:** FBS regular season + conference championships (ESPN `seasontype=2`).
  Bowls and playoffs (`seasontype=3`) are excluded.
- **Data is not committed.** `data/` (per-game and merged CSVs, ~160 MB) is
  regenerable and also lives in Supabase; `.env` holds secrets. Both are
  gitignored.
