"""
Daily in-season updater for NCAAF play-by-play.

Designed to run unattended (e.g. Windows Task Scheduler) once a day during the
season. It is idempotent and checkpoint-driven — safe to run any number of times.

Each run:
  1. Auto-detects the current season (or takes one as an argument).
  2. Refreshes game discovery (picks up newly-scheduled games).
  3. Scrapes games CFBD reports as completed and not yet captured. Scheduled /
     in-progress games are left pending and retried on the next run.
  4. Backfills any ESPN no-data games from the CFBD fallback.
  5. Rebuilds the coach map (captures mid-season coaching changes).
  6. Re-merges the season CSV (adds home_coach/away_coach, game_url, etc.).

Output is appended to data/update_log.txt.

Usage:
    python update_season.py            # current season (auto-detected)
    python update_season.py 2026       # explicit season
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

from espn_cfb_batch_scraper import (
    load_checkpoint, scrape_season, merge_season,
)
from cfbd_fallback import run_fallback
import build_coaches
import build_game_context
import build_quarter_log
import build_drive_log

load_dotenv()

DATA_DIR = Path("data")
LOG_FILE = DATA_DIR / "update_log.txt"
CFBD_GAMES = "https://api.collegefootballdata.com/games"


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    DATA_DIR.mkdir(exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def current_season(now: datetime) -> int:
    """CFB season year. Aug-Dec -> that year; Jan-Jul -> previous year."""
    return now.year if now.month >= 8 else now.year - 1


def completed_game_ids(season: int) -> set[str] | None:
    """Set of CFBD game IDs marked completed for the season's regular schedule.
    Returns None if the CFBD lookup fails (caller then relies on the
    GameNotFinalError safety net instead of a pre-filter)."""
    key = os.getenv("CFBD_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(
            CFBD_GAMES,
            params={"year": season, "seasonType": "regular"},
            headers={"Authorization": f"Bearer {key}"}, timeout=30,
        )
        r.raise_for_status()
        return {str(g["id"]) for g in r.json() if g.get("completed")}
    except Exception as e:
        log(f"  WARN: CFBD completed-games lookup failed ({e}); attempting all pending")
        return None


def main() -> None:
    season = int(sys.argv[1]) if len(sys.argv) > 1 else current_season(datetime.now())
    log(f"===== Daily update: season {season} =====")

    cp = load_checkpoint()

    # 1-3. Discover + scrape completed games (idempotent; checkpoints each game)
    completed = completed_game_ids(season)
    if completed is not None:
        log(f"CFBD reports {len(completed)} completed games for {season}")
    try:
        scrape_season(season, cp, refresh_discovery=True, only_game_ids=completed)
    except Exception as e:
        log(f"  ERROR during scrape: {e}")

    # 4. Backfill ESPN no-data games from CFBD
    try:
        run_fallback([season])
    except Exception as e:
        log(f"  WARN: cfbd fallback failed: {e}")

    # 5. Refresh coach map (mid-season changes) for all seasons in the checkpoint
    try:
        build_coaches.build()
    except Exception as e:
        log(f"  WARN: coach rebuild failed: {e}")

    # 6. Re-merge the season CSV (fills coaches/url, preserves data_source)
    cp = load_checkpoint()
    try:
        merge_season(season, cp)
    except Exception as e:
        log(f"  ERROR during merge: {e}")

    # 7. Build derived feature artifacts (records + quarter log -> Supabase).
    # Rebuild context for ALL checkpoint seasons (not just the current one):
    # game_context.json is a single file keyed by game_id across seasons and is
    # consumed by both feature builders, so a single-season build would clobber
    # the other seasons' entries. Past seasons are static, so this is idempotent.
    try:
        build_game_context.build()
    except Exception as e:
        log(f"  WARN: game-context build failed: {e}")
    try:
        build_quarter_log.build([season])
    except Exception as e:
        log(f"  WARN: quarter-log build failed: {e}")
    try:
        build_drive_log.build([season])
    except Exception as e:
        log(f"  WARN: drive-log build failed: {e}")

    info = cp.get(str(season), {})
    log(f"Done. discovered={len(info.get('games', []))} "
        f"scraped={len(info.get('scraped', []))} "
        f"no_data={len(info.get('no_data', []))} "
        f"canceled={len(info.get('canceled', []))} "
        f"failed={len(info.get('failed', []))}")
    log("")


if __name__ == "__main__":
    main()
