"""
CFBD (College Football Data) Fallback Scraper

For ESPN no-data games, fetches play-by-play from the cfbd API.
cfbd game IDs match ESPN game IDs for FBS games.

Outputs the same column schema as espn_cfb_pbp_scraper.py, with an added
'data_source' column set to "cfbd". Successfully scraped games are moved out
of the no_data bucket in checkpoint.json and into scraped.

Setup:
    CFBD_API_KEY must be set in .env

Usage:
    python cfbd_fallback.py                   # all seasons, all no_data games
    python cfbd_fallback.py --season 2024     # single season
    python cfbd_fallback.py --game 401628504  # single game probe (prints output)
"""

import argparse
import json
import os
import re
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from espn_cfb_pbp_scraper import (
    abbreviate_name,
    fetch_game_data,
    secs_remaining_regulation,
    detect_turnover,
)

load_dotenv()

CFBD_BASE = "https://api.collegefootballdata.com"
DATA_DIR  = Path("data")
GAMES_DIR = DATA_DIR / "games"
CKPT_FILE = DATA_DIR / "checkpoint.json"
SEASONS   = [2025, 2024, 2023]
DELAY     = 0.5   # seconds between cfbd API calls
MIN_PLAYS = 80    # games below this are flagged as partial_data, not scraped


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    key = os.getenv("CFBD_API_KEY")
    if not key:
        raise ValueError("CFBD_API_KEY not set in .env")
    return {"Authorization": f"Bearer {key}"}


def get_game_meta(game_id: str, season: int) -> dict | None:
    """Return {week, homeTeam, awayTeam} for an ESPN game ID via cfbd /games endpoint."""
    r = requests.get(
        f"{CFBD_BASE}/games",
        params={"year": season, "id": game_id},
        headers=_headers(), timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    g = data[0]
    return {
        "week":     g.get("week"),
        "homeTeam": g.get("homeTeam", ""),
        "awayTeam": g.get("awayTeam", ""),
    }


def fetch_cfbd_plays(game_id: str, season: int, week: int, team: str) -> list[dict]:
    """Fetch plays for a specific game. cfbd ignores gameId as a filter, so we
    use the team name (which scopes to one game per week) then filter client-side."""
    r = requests.get(
        f"{CFBD_BASE}/plays",
        params={"year": season, "week": week, "team": team},
        headers=_headers(), timeout=30,
    )
    r.raise_for_status()
    gid_int = int(game_id)
    return [p for p in r.json() if p.get("gameId") == gid_int]


def get_espn_team_names(game_id: str) -> tuple[str, str]:
    """Return (home_full_name, away_full_name) from ESPN header data.
    Works even for games with no drive/PBP data."""
    data = fetch_game_data(game_id)
    home = away = ""
    for c in data["header"]["competitions"][0]["competitors"]:
        t = c["team"]
        name = f"{t['location']} {t['name']}"
        if c["homeAway"] == "home":
            home = name
        else:
            away = name
    return home, away


# ---------------------------------------------------------------------------
# Play-level helpers
# ---------------------------------------------------------------------------

def _clock_secs(clock: dict | None) -> int:
    if not clock:
        return 0
    return (clock.get("minutes") or 0) * 60 + (clock.get("seconds") or 0)


def _normalize_desc(raw: str) -> str:
    """cfbd uses 'F.Last' — add a space to match our 'F. Last' convention."""
    return re.sub(r'(?<=[A-Z])\.(?=[A-Za-z])', '. ', raw.strip())


def map_cfbd_play_type(cfbd_type: str, desc: str = "") -> str:
    t = cfbd_type.lower()
    d = desc.lower()
    if "kickoff" in t:                          return "kickoff"
    if "rushing touchdown" in t:                return "run"
    if "rush" in t:                             return "run"
    if "passing touchdown" in t:                return "pass"
    if any(x in t for x in ["pass", "reception", "completion", "incompletion"]):
        return "pass"
    if "sack" in t:                             return "pass"
    if "interception" in t:                     return "pass"
    if "fumble" in t:
        if re.search(r"\bsacked\b", d):         return "pass"
        if re.search(r"\bpass\b|\breception\b", d): return "pass"
        if re.search(r"\brun\b|\brush\b", d):   return "run"
        if re.search(r"\bpunt\b", d):           return "punt"
        if re.search(r"\bkickoff\b", d):        return "kickoff"
        return "fumble"
    if "punt" in t:                             return "punt"
    if "field goal" in t:                       return "field goal"
    if "extra point" in t or "pat" in t:        return "extra point"
    if "two point" in t or "two-point" in t:    return "two-point conversion"
    if "safety" in t:                           return "safety"
    if "timeout" in t:                          return "timeout"
    if "end of half" in t:                      return "end of half"
    if "end of game" in t:                      return "end of game"
    if "end period" in t:                       return "end period"
    if "penalty" in t:                          return "penalty"
    return t or "unknown"


def detect_cfbd_scoring_flags(cfbd_type: str) -> tuple[bool, bool, bool, bool]:
    """Returns (is_td, is_xp, is_2pt, is_fg)."""
    t = cfbd_type.lower()
    return (
        "touchdown" in t,
        "extra point" in t and ("good" in t or "successful" in t),
        "two point" in t or "two-point" in t,
        "field goal" in t,
    )


def parse_cfbd_players(cfbd_type: str, desc: str) -> tuple[str, str, str]:
    """Extract (offensive_player, quarterback, kicker) from cfbd play text.
    Desc must already be normalized (F.Last → F. Last)."""
    t   = cfbd_type.lower()
    NO_OFF, NA = "No Offensive Player", "N/A"

    if "kickoff" in t:
        m = re.match(r"^(.+?)\s+kicks?\b", desc, re.I)
        return NO_OFF, NA, (m.group(1) if m else NA)

    if "punt" in t:
        m = re.match(r"^(.+?)\s+punts?\b", desc, re.I)
        return NO_OFF, NA, (m.group(1) if m else NA)

    if "field goal" in t:
        m = re.match(r"^(.+?)\s+\d+\s+(?:yd|yard)", desc, re.I) \
            or re.match(r"^(.+?)\s+(?:field goal|fg)\b", desc, re.I)
        return NO_OFF, NA, (m.group(1) if m else NA)

    if "extra point" in t:
        m = re.match(r"^(.+?)\s+extra point", desc, re.I)
        return NO_OFF, NA, (m.group(1) if m else NA)

    is_sack = "sack" in t or bool(re.search(r"\bsacked\b", desc, re.I))
    if is_sack:
        m = re.match(r"^(.+?)\s+sacked\b", desc, re.I)
        qb = m.group(1) if m else NA
        return qb, qb, NA  # offensive_player = quarterback = QB

    if "interception" in t:
        m = re.match(r"^(.+?)\s+pass\b", desc, re.I)
        return NO_OFF, (m.group(1) if m else NA), NA

    if any(x in t for x in ["rush", "rushing"]):
        # cfbd uses "rushed" and also "scrambles" for QB scrambles
        m = re.match(r"^(.+?)\s+(?:rushed?|scrambles?)\b", desc, re.I)
        return (m.group(1) if m else NO_OFF), NA, NA

    if any(x in t for x in ["pass", "completion", "incompletion", "reception", "passing"]):
        # QB can precede "pass", "steps back to pass", "throws", etc.
        m_qb = re.match(r"^(.+?)\s+(?:pass\b|steps back|throws?\b|rolls? out)", desc, re.I)
        qb   = m_qb.group(1) if m_qb else NA
        # Receiver may be terminated by "for", "at", a period, comma, or end —
        # cfbd uses "Catch made by X for 5 yards" AND "Catch made by X at PSU 19".
        m_rec = re.search(
            r"(?:catch made by|caught by)\s+(.+?)(?:\s+for\b|\s+at\b|\.|,|\s*$)",
            desc, re.I,
        )
        if not m_rec:
            m_rec = re.search(r"incomplete\s+intended for\s+(.+?)(?:\.|,|\s*$)", desc, re.I)
        if not m_rec:
            # ESPN-style text that occasionally appears in cfbd: "X pass to RECEIVER for"
            m_rec = re.search(
                r"pass\s+(?:complete\s+)?to\s+(.+?)(?:\s+for\b|,|\s*$)", desc, re.I
            )
        return (m_rec.group(1).strip() if m_rec else NO_OFF), qb, NA

    if "fumble" in t:
        if re.search(r"\bsacked\b", desc, re.I):
            m = re.match(r"^(.+?)\s+sacked\b", desc, re.I)
            qb = m.group(1) if m else NA
            return qb, qb, NA
        m = re.match(r"^(.+?)\s+rushed?\b", desc, re.I)
        if m: return m.group(1), NA, NA
        m = re.match(r"^(.+?)\s+pass\b", desc, re.I)
        if m: return NO_OFF, m.group(1), NA
        return NO_OFF, NA, NA

    return NO_OFF, NA, NA


# ---------------------------------------------------------------------------
# Core game processor
# ---------------------------------------------------------------------------

def process_cfbd_game(game_id: str, season: int) -> pd.DataFrame:
    # Step 1: week + team name lookup
    meta = get_game_meta(game_id, season)
    if not meta or not meta["week"]:
        return pd.DataFrame()
    time.sleep(DELAY)

    # Step 2: plays filtered by homeTeam (cfbd ignores gameId; team scopes to one game/week)
    plays_raw = fetch_cfbd_plays(game_id, season, meta["week"], meta["homeTeam"])
    if not plays_raw:
        return pd.DataFrame()
    time.sleep(DELAY)

    # Step 3: full team names from ESPN header
    espn_home, espn_away = get_espn_team_names(game_id)

    # Build cfbd_short → ESPN_full name map
    cfbd_home = plays_raw[0].get("home", "")
    cfbd_away = plays_raw[0].get("away", "")
    name_map  = {cfbd_home: espn_home, cfbd_away: espn_away}

    # Step 4: sort chronologically (period ASC, clock DESC = 15:00 first)
    plays_raw.sort(key=lambda p: (
        p.get("period", 1),
        -_clock_secs(p.get("clock") or {}),
    ))

    rows = []
    game_poss_num  = 0
    home_poss_count = 0
    away_poss_count = 0
    prev_drive_id  = None
    prev_home_score = 0
    prev_away_score = 0
    play_seq        = 0
    poss_yards_cum  = 0
    drive_started_kickoff = False
    poss_incremented = False

    for play in plays_raw:
        cfbd_type = play.get("playType", "")
        desc      = _normalize_desc(play.get("playText") or "")

        period  = play.get("period", 1) or 1
        secs_q  = _clock_secs(play.get("clock"))
        secs_reg = secs_remaining_regulation(period, secs_q)

        cfbd_offense = play.get("offense", "")
        offense      = name_map.get(cfbd_offense, cfbd_offense)
        home_name    = name_map.get(cfbd_home, cfbd_home)
        away_name    = name_map.get(cfbd_away, cfbd_away)

        # Scores: offense_score / defense_score → home / away
        off_score = play.get("offenseScore") or 0
        def_score = play.get("defenseScore") or 0
        if cfbd_offense == cfbd_home:
            home_score, away_score = off_score, def_score
        else:
            home_score, away_score = def_score, off_score

        play_type     = map_cfbd_play_type(cfbd_type, desc)
        is_kickoff    = play_type == "kickoff"
        play_yards    = play.get("yardsGained") or 0
        yards_to_ez   = play.get("yardsToGoal")
        down          = play.get("down") or None      # CFBD: 0 on non-scrimmage -> null
        distance      = play.get("distance")

        # ── Possession tracking (driveId-based) ──────────────────────────────
        drive_id   = play.get("driveId")
        is_new_drive = drive_id != prev_drive_id

        if is_new_drive:
            prev_drive_id         = drive_id
            drive_started_kickoff = is_kickoff
            poss_incremented      = False
            play_seq              = 0
            poss_yards_cum        = 0

            if not drive_started_kickoff and cfbd_offense:
                game_poss_num += 1
                if cfbd_offense == cfbd_home:
                    home_poss_count += 1
                else:
                    away_poss_count += 1
                poss_incremented = True

        # ── Flags ─────────────────────────────────────────────────────────────
        td, xp, two_pt, fg = detect_cfbd_scoring_flags(cfbd_type)
        turnover  = detect_turnover(cfbd_type, desc, False)
        sack      = "sack" in cfbd_type.lower() or bool(re.search(r"\bsacked\b", desc, re.I))
        punt      = "punt" in cfbd_type.lower()
        # scoring_play = flag union (matches the ESPN scraper), not a score delta
        made_fg   = fg and not re.search(r"missed|blocked|no good", desc, re.I)
        safety    = bool(re.search(r"\bsafety\b", desc, re.I))
        scoring   = bool(td or made_fg or xp or two_pt or safety)

        # ── Players ───────────────────────────────────────────────────────────
        off_p, qb, kicker = parse_cfbd_players(cfbd_type, desc)
        off_p  = abbreviate_name(off_p.strip())
        qb     = abbreviate_name(qb.strip())
        kicker = abbreviate_name(kicker.strip())

        # ── Possession counters ───────────────────────────────────────────────
        if is_kickoff:
            poss_play_num = 0
            g_poss = h_poss = a_poss = 0
            poss_yards = 0
        else:
            # First non-kickoff in a kickoff-starting drive: start possession now
            if drive_started_kickoff and not poss_incremented and cfbd_offense:
                game_poss_num += 1
                if cfbd_offense == cfbd_home:
                    home_poss_count += 1
                else:
                    away_poss_count += 1
                poss_incremented = True
                play_seq         = 0
                poss_yards_cum   = 0

            play_seq       += 1
            poss_play_num   = play_seq
            g_poss          = game_poss_num
            h_poss          = home_poss_count
            a_poss          = away_poss_count
            poss_yards_cum += play_yards
            poss_yards      = poss_yards_cum

        rows.append({
            "game_id":               game_id,
            "home_team":             home_name,
            "away_team":             away_name,
            "play_desc":             desc,
            "home_score":            home_score,
            "away_score":            away_score,
            "quarter":               period,
            "secs_left_quarter":     secs_q,
            "secs_left_reg":         secs_reg,
            "offensive_team":        offense,
            "play_type":             play_type,
            "scoring_play":          scoring,
            "is_touchdown":          td,
            "is_field_goal":         fg,
            "is_turnover":           turnover,
            "is_punt":               punt,
            "is_sack":               sack,
            "is_extra_point":        xp,
            "is_two_point_conversion": two_pt,
            "offensive_player":      off_p,
            "quarterback":           qb,
            "kicker":                kicker,
            "poss_play_num":         poss_play_num,
            "game_poss_num":         g_poss,
            "home_team_poss_num":    h_poss,
            "away_team_poss_num":    a_poss,
            "yards_to_end_zone":     yards_to_ez,
            "down":                  down,
            "distance":              distance,
            "play_yards":            play_yards,
            "poss_yards":            poss_yards,
            "data_source":           "cfbd",
            "game_url":              f"https://www.espn.com/college-football/playbyplay/_/gameId/{game_id}",
        })

        prev_home_score = home_score
        prev_away_score = away_score

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def load_checkpoint() -> dict:
    if CKPT_FILE.exists():
        return json.loads(CKPT_FILE.read_text())
    return {}


def save_checkpoint(cp: dict) -> None:
    CKPT_FILE.write_text(json.dumps(cp, indent=2))


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def run_fallback(seasons: list[int]) -> None:
    cp = load_checkpoint()

    for season in seasons:
        key     = str(season)
        no_data = list(cp.get(key, {}).get("no_data", []))
        scraped = set(cp.get(key, {}).get("scraped", []))
        pending = [g for g in no_data if g not in scraped]

        if not pending:
            print(f"[{season}] No no-data games to retry.")
            continue

        print(f"\n[{season}] cfbd fallback: {len(pending)} games")

        partial = set(cp[key].get("partial_data", []))

        for i, game_id in enumerate(pending, 1):
            print(f"  [{i:3d}/{len(pending)}]  {game_id}...", end="", flush=True)
            try:
                df = process_cfbd_game(game_id, season)
                if df.empty:
                    print(" not in cfbd")
                elif len(df) < MIN_PLAYS:
                    # Save for inspection but don't promote to scraped
                    out = GAMES_DIR / f"pbp_{game_id}.csv"
                    df.to_csv(out, index=False)
                    partial.add(game_id)
                    no_data_set = set(cp[key]["no_data"])
                    no_data_set.discard(game_id)
                    cp[key]["partial_data"] = sorted(partial)
                    cp[key]["no_data"]      = sorted(no_data_set)
                    save_checkpoint(cp)
                    print(f" {len(df)} plays — PARTIAL (below {MIN_PLAYS}-play threshold, excluded from merge)")
                else:
                    out = GAMES_DIR / f"pbp_{game_id}.csv"
                    df.to_csv(out, index=False)
                    scraped.add(game_id)
                    no_data_set = set(cp[key]["no_data"])
                    no_data_set.discard(game_id)
                    cp[key]["scraped"]  = sorted(scraped)
                    cp[key]["no_data"]  = sorted(no_data_set)
                    save_checkpoint(cp)
                    print(f" {len(df)} plays (cfbd)")
            except Exception as e:
                print(f" FAILED: {e}")
            time.sleep(DELAY)


def refresh_cfbd_games() -> None:
    """Re-process every cfbd-sourced per-game file already on disk, using the
    current parser. Used after parser fixes — no checkpoint changes."""
    cp = load_checkpoint()

    # Map each game_id -> season by scanning the checkpoint buckets
    game_season: dict[str, int] = {}
    for skey, info in cp.items():
        for bucket in ("scraped", "partial_data", "no_data"):
            for gid in info.get(bucket, []):
                game_season.setdefault(str(gid), int(skey))

    files = sorted(GAMES_DIR.glob("pbp_*.csv"))
    targets = []
    for p in files:
        head = pd.read_csv(p, dtype={"game_id": str}, keep_default_na=False, nrows=1)
        if "data_source" in head.columns and (head["data_source"] == "cfbd").any():
            targets.append(head.iloc[0]["game_id"])

    print(f"Refreshing {len(targets)} cfbd games with current parser...")
    for i, gid in enumerate(targets, 1):
        season = game_season.get(gid)
        if not season:
            print(f"  [{i:3d}/{len(targets)}]  {gid}... no season in checkpoint, skip")
            continue
        print(f"  [{i:3d}/{len(targets)}]  {gid} ({season})...", end="", flush=True)
        try:
            df = process_cfbd_game(gid, season)
            if df.empty:
                print(" empty, skipped")
            else:
                df.to_csv(GAMES_DIR / f"pbp_{gid}.csv", index=False)
                print(f" {len(df)} plays")
        except Exception as e:
            print(f" FAILED: {e}")
        time.sleep(DELAY)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="cfbd fallback PBP scraper")
    parser.add_argument("--season", type=int, help="Single season year")
    parser.add_argument("--game",   type=str, help="Probe a single ESPN game ID")
    parser.add_argument("--refresh-cfbd", action="store_true",
                        help="Re-process all cfbd games on disk with current parser")
    args = parser.parse_args()

    if args.refresh_cfbd:
        refresh_cfbd_games()
        return

    if args.game:
        season = args.season or 2024
        print(f"Probing cfbd for game {args.game} (season {season})...")
        df = process_cfbd_game(args.game, season)
        if df.empty:
            print("No data found in cfbd.")
        else:
            print(f"{len(df)} plays\n")
            cols = ["quarter", "offensive_team", "play_type", "offensive_player",
                    "quarterback", "play_desc", "home_score", "away_score"]
            print(df[cols].head(20).to_string(index=False))
    else:
        seasons = [args.season] if args.season else SEASONS
        run_fallback(seasons)


if __name__ == "__main__":
    main()
