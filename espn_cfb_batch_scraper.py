"""
ESPN College Football Play-by-Play — Batch Scraper

Discovery:  For each season, fetches FBS team IDs via ESPN's core API conference
            groups (FBS conferences only — no FCS bleed-through).  Then loops all
            team schedules to collect regular-season game IDs (seasontype=2).
            Each game appears in two team schedules; deduplication is applied.

Scraping:   Fetches PBP data per game, saves per-game CSVs, checkpoints progress.

Merging:    Combines per-game CSVs into one file per season.

Bowl games and playoffs (seasontype=3) are excluded automatically.
Conference championship games (seasontype=2) are included.

Usage:
    python espn_cfb_batch_scraper.py                  # all three seasons
    python espn_cfb_batch_scraper.py --season 2025    # single season
    python espn_cfb_batch_scraper.py --discover       # discovery only (no scraping)
    python espn_cfb_batch_scraper.py --merge          # merge existing CSVs only
    python espn_cfb_batch_scraper.py --retry-failed   # re-attempt previously failed games

Seasons are scraped in priority order: 2025 → 2024 → 2023.
Checkpointing: progress is saved after every game; safe to interrupt and resume.

Output layout:
    data/
        checkpoint.json          — tracks discovered game IDs and scrape status
        games/
            pbp_{game_id}.csv    — one file per game
        pbp_2025_regular.csv     — merged season files (written after each season)
        pbp_2024_regular.csv
        pbp_2023_regular.csv
"""

import argparse
import json
import re
import time
from pathlib import Path

import pandas as pd
import requests

from espn_cfb_pbp_scraper import (
    process_game, HEADERS, GameNotPlayedError, GameNotFinalError,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# FBS conferences are discovered dynamically each season as the children of the
# FBS parent group (group 80), so conference realignment (new/dropped leagues) is
# handled automatically. This hardcoded list is only a fallback if that lookup
# fails — verified 2024: 151=AAC 1=ACC 4=Big12 5=Big10 12=CUSA 18=FBS-Ind 15=MAC
# 17=MWC 9=Pac12 8=SEC 37=SunBelt.
FBS_CONF_IDS_FALLBACK = ["151", "1", "4", "5", "12", "18", "15", "17", "9", "8", "37"]
FBS_PARENT_GROUP = "80"

FBS_CHILDREN_URL = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/college-football"
    "/seasons/{season}/types/2/groups/{parent}/children?limit=50"
)
CORE_TEAMS_URL = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/college-football"
    "/seasons/{season}/types/2/groups/{conf}/teams?limit=50"
)
SCHED_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
    "/teams/{team_id}/schedule"
)

DATA_DIR  = Path("data")
GAMES_DIR = DATA_DIR / "games"
CKPT_FILE = DATA_DIR / "checkpoint.json"
COACHES_JSON = DATA_DIR / "coaches.json"

SEASONS = [2025, 2024, 2023]

DELAY_DISCOVER = 0.3   # seconds between team-schedule requests
DELAY_SCRAPE   = 1.0   # seconds between game PBP requests


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def get_fbs_conf_ids(season: int) -> list[str]:
    """FBS conference group IDs for a season (children of the FBS parent group).
    Falls back to the known-good static list if the lookup fails or is empty."""
    try:
        url = FBS_CHILDREN_URL.format(season=season, parent=FBS_PARENT_GROUP)
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        ids = []
        for item in resp.json().get("items", []):
            m = re.search(r"/groups/(\d+)", item.get("$ref", ""))
            if m:
                ids.append(m.group(1))
        if ids:
            return ids
    except Exception as e:
        print(f"  Conf-list lookup failed ({e}); using fallback list")
    return FBS_CONF_IDS_FALLBACK


def get_fbs_team_ids(season: int) -> list[str]:
    """
    Return ESPN team IDs for all FBS teams in a given season.
    Uses the core API's conference-group roster, which gives exactly the teams
    competing in each FBS conference that year (~134 teams).
    """
    all_ids: set[str] = set()
    for conf_id in get_fbs_conf_ids(season):
        url = CORE_TEAMS_URL.format(season=season, conf=conf_id)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            for item in resp.json().get("items", []):
                ref = item.get("$ref", "")
                m = re.search(r"/teams/(\d+)", ref)
                if m:
                    all_ids.add(m.group(1))
        except Exception as e:
            print(f"\n  Conf {conf_id} team-fetch error: {e}")
    return sorted(all_ids)


def get_team_game_ids(team_id: str, season: int) -> list[str]:
    """Return regular-season game IDs for one team/season (seasontype=2)."""
    resp = requests.get(
        SCHED_URL.format(team_id=team_id),
        params={"season": season, "seasontype": 2, "limit": 50},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    ids: list[str] = []
    for event in resp.json().get("events", []):
        gid = event.get("id")
        if not gid:
            continue
        stype = int((event.get("season") or {}).get("type", 2))
        if stype == 2:
            ids.append(str(gid))
    return ids


def discover_season(season: int) -> list[str]:
    """Collect all unique regular-season FBS game IDs for a given season."""
    print(f"  Fetching {season} FBS team roster...")
    team_ids = get_fbs_team_ids(season)
    print(f"  {len(team_ids)} FBS teams found for {season}.")

    seen: set[str] = set()
    n = len(team_ids)
    errors = 0

    for i, tid in enumerate(team_ids, 1):
        print(f"\r  {season}: scanning team {i}/{n}  ({len(seen)} games found)  ", end="", flush=True)
        try:
            seen.update(get_team_game_ids(tid, season))
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"\n  Team {tid} schedule error: {e}")
        time.sleep(DELAY_DISCOVER)

    print(f"\r  {season}: {len(seen)} unique regular-season games discovered.          ")
    return sorted(seen)


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def load_checkpoint() -> dict:
    if CKPT_FILE.exists():
        return json.loads(CKPT_FILE.read_text())
    return {}


def save_checkpoint(cp: dict) -> None:
    CKPT_FILE.write_text(json.dumps(cp, indent=2))


def _is_cfbd_sourced(game_id: str) -> bool:
    """True if the per-game CSV was backfilled from CFBD (no ESPN PBP)."""
    p = GAMES_DIR / f"pbp_{game_id}.csv"
    if not p.exists():
        return False
    try:
        head = pd.read_csv(p, nrows=1, dtype=str, keep_default_na=False)
        return "data_source" in head.columns and head.iloc[0]["data_source"] == "cfbd"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def scrape_season(
    season: int,
    cp: dict,
    retry_failed: bool = False,
    refresh_discovery: bool = False,
    only_game_ids: set | None = None,
    rescrape: bool = False,
) -> None:
    key = str(season)

    # --- Discovery phase ---
    if not cp.get(key, {}).get("games"):
        print(f"\n[{season}] Discovering game IDs...")
        games = discover_season(season)
        cp[key] = {"games": games, "scraped": [], "failed": [], "no_data": []}
        save_checkpoint(cp)
    elif refresh_discovery:
        # Live season: re-discover and merge any newly-scheduled game IDs.
        print(f"\n[{season}] Refreshing discovery...")
        found = set(discover_season(season))
        existing = set(cp[key]["games"])
        new_ids = found - existing
        if new_ids:
            cp[key]["games"] = sorted(existing | found)
            print(f"[{season}] Added {len(new_ids)} newly-discovered game(s).")
            save_checkpoint(cp)
        else:
            print(f"[{season}] No new games since last discovery.")
    else:
        print(f"\n[{season}] {len(cp[key]['games'])} game IDs already discovered.")

    all_games = cp[key]["games"]
    scraped   = set(cp[key].get("scraped",   []))
    failed    = set(cp[key].get("failed",    []))
    no_data   = set(cp[key].get("no_data",   []))
    canceled  = set(cp[key].get("canceled",  []))

    if rescrape:
        # Force re-processing of all previously-scraped games (e.g. to add new
        # columns). Overwrites their per-game CSVs; status buckets unchanged.
        # Skip cfbd-sourced games — ESPN has no PBP for them (refresh those via
        # `cfbd_fallback.py --refresh-cfbd`).
        pending = [g for g in sorted(scraped) if not _is_cfbd_sourced(g)]
    else:
        skip = scraped | no_data | canceled | (set() if retry_failed else failed)
        pending = [g for g in all_games if g not in skip]
        # Live season: only attempt games already known to be complete, to avoid
        # hitting hundreds of not-yet-played games every run.
        if only_game_ids is not None:
            pending = [g for g in pending if g in only_game_ids]

    print(f"[{season}] Scraping: {len(pending)} pending  "
          f"| {len(scraped)} done  | {len(failed)} failed  "
          f"| {len(no_data)} no PBP  | {len(canceled)} canceled")

    for i, game_id in enumerate(pending, 1):
        out_path = GAMES_DIR / f"pbp_{game_id}.csv"
        pct = 100 * (len(scraped) + i - 1) / max(len(all_games), 1)
        print(
            f"  [{i:4d}/{len(pending)}]  {game_id}  ({pct:.1f}% of season)...",
            end="", flush=True,
        )

        try:
            df = process_game(game_id)

            if df.empty:
                print(" no PBP data")
                no_data.add(game_id)
                failed.discard(game_id)
                cp[key]["no_data"] = sorted(no_data)
            else:
                df.to_csv(out_path, index=False)
                scraped.add(game_id)
                failed.discard(game_id)
                cp[key]["scraped"] = sorted(scraped)
                cp[key]["failed"]  = sorted(failed)
                print(f" {len(df)} plays")

        except GameNotPlayedError as e:
            print(f" canceled/postponed")
            canceled.add(game_id)
            failed.discard(game_id)
            no_data.discard(game_id)
            cp[key]["canceled"] = sorted(canceled)
            cp[key]["failed"]   = sorted(failed)
            cp[key]["no_data"]  = sorted(no_data)

        except GameNotFinalError:
            # Not played yet — leave pending so it's retried on a later run.
            print(" not final yet (pending)")
            failed.discard(game_id)
            no_data.discard(game_id)
            cp[key]["failed"]  = sorted(failed)
            cp[key]["no_data"] = sorted(no_data)

        except Exception as e:
            print(f" FAILED: {e}")
            failed.add(game_id)
            cp[key]["failed"] = sorted(failed)

        save_checkpoint(cp)
        time.sleep(DELAY_SCRAPE)

    print(f"\n[{season}] Done. {len(scraped)} scraped / {len(all_games)} total.")


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def merge_season(season: int, cp: dict) -> None:
    key = str(season)
    scraped_ids = cp.get(key, {}).get("scraped", [])

    if not scraped_ids:
        print(f"[{season}] No scraped games to merge.")
        return

    dfs, missing = [], 0
    for gid in scraped_ids:
        p = GAMES_DIR / f"pbp_{gid}.csv"
        if p.exists():
            # keep_default_na=False so literal "N/A" (kicker/QB on non-applicable
            # plays) is preserved as a string rather than converted to NaN/blank.
            dfs.append(pd.read_csv(p, dtype={"game_id": str}, keep_default_na=False))
        else:
            missing += 1

    if not dfs:
        print(f"[{season}] No CSV files found on disk.")
        return

    merged = pd.concat(dfs, ignore_index=True)

    # Backfill data_source for rows scraped before the column existed (ESPN files)
    if "data_source" not in merged.columns:
        merged["data_source"] = "espn"
    else:
        merged["data_source"] = merged["data_source"].replace("", "espn").fillna("espn")

    # Add home_coach / away_coach from the prebuilt cache (build_coaches.py).
    # "N/A" until the cache exists; "Non-FBS" for FCS opponents not tracked by CFBD.
    coaches = {}
    if COACHES_JSON.exists():
        coaches = json.loads(COACHES_JSON.read_text())
    merged["home_coach"] = merged["game_id"].map(
        lambda g: coaches.get(g, {}).get("home_coach", "N/A")
    )
    merged["away_coach"] = merged["game_id"].map(
        lambda g: coaches.get(g, {}).get("away_coach", "N/A")
    )

    # Add ESPN game URL for easy verification (Ctrl+click in Excel)
    merged["game_url"] = (
        "https://www.espn.com/college-football/playbyplay/_/gameId/"
        + merged["game_id"].astype(str)
    )

    out = DATA_DIR / f"pbp_{season}_regular.csv"
    merged.to_csv(out, index=False)
    note = f"  ({missing} CSVs missing from disk)" if missing else ""
    print(f"[{season}] Merged {len(dfs)} games -> {len(merged):,} plays -> {out}{note}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Batch ESPN CFB PBP scraper")
    parser.add_argument("--season",       type=int,            help="Single season year")
    parser.add_argument("--discover",     action="store_true", help="Discovery only (skip scraping)")
    parser.add_argument("--merge",        action="store_true", help="Merge existing CSVs only")
    parser.add_argument("--retry-failed", action="store_true", help="Retry previously failed games")
    parser.add_argument("--rescrape",     action="store_true", help="Re-process all scraped games (e.g. new columns)")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    GAMES_DIR.mkdir(parents=True, exist_ok=True)

    seasons = [args.season] if args.season else SEASONS
    cp = load_checkpoint()

    if args.merge:
        for s in seasons:
            merge_season(s, cp)
        return

    for season in seasons:
        if args.discover:
            key = str(season)
            if not cp.get(key, {}).get("games"):
                games = discover_season(season)
                cp[key] = {"games": games, "scraped": [], "failed": [], "no_data": []}
                save_checkpoint(cp)
            else:
                print(f"[{season}] Already discovered: {len(cp[key]['games'])} games.")
        else:
            scrape_season(season, cp, retry_failed=args.retry_failed, rescrape=args.rescrape)
            merge_season(season, cp)

    print("\nAll done.")


if __name__ == "__main__":
    main()
