"""
Build a game_id -> (home_coach, away_coach) mapping for all scraped games and
cache it to data/coaches.json. The merge step reads that cache to fill the
home_coach / away_coach columns.

Source: CollegeFootballData /coaches (season head-coach records) + /games
(dates). CFBD game IDs match ESPN game IDs, so coaches are keyed by game_id and
no team-name matching against our dataset is needed.

Mid-season coaching changes are resolved per game by date:
  - A team's season-opening coach = the one with a prior-season record at that
    school (falls back to the coach with the most games if none).
  - The replacement coached the LAST k games of the team's in-dataset schedule,
    where k = (replacement's CFBD game count) - (bowl games, which we exclude).
  - Games are ordered by date; the last k go to the replacement, the rest to the
    opener.

Run:
    python build_coaches.py            # build cache + print verification report
    python build_coaches.py --report   # just re-print the report from cache
"""

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

CFBD = "https://api.collegefootballdata.com"
DATA_DIR = Path("data")
COACHES_JSON = DATA_DIR / "coaches.json"
SEASONS = [2025, 2024, 2023]


def _headers() -> dict:
    key = os.getenv("CFBD_API_KEY")
    if not key:
        raise ValueError("CFBD_API_KEY not set in .env")
    return {"Authorization": f"Bearer {key}"}


def fetch_coaches(year: int) -> dict[str, list[tuple[str, int]]]:
    """school -> [(coach_name, games_coached), ...] for one season."""
    r = requests.get(f"{CFBD}/coaches", params={"year": year}, headers=_headers(), timeout=30)
    r.raise_for_status()
    out: dict[str, list] = defaultdict(list)
    for c in r.json():
        name = f"{c['firstName']} {c['lastName']}"
        for s in c["seasons"]:
            if s["year"] == year:
                out[s["school"]].append((name, s["games"]))
    return out


def fetch_games(year: int) -> dict[str, dict]:
    """game_id -> {date, home, away} for one season's regular schedule."""
    r = requests.get(
        f"{CFBD}/games",
        params={"year": year, "seasonType": "regular"},
        headers=_headers(), timeout=30,
    )
    r.raise_for_status()
    out = {}
    for g in r.json():
        out[str(g["id"])] = {
            "date": g["startDate"],
            "home": g["homeTeam"],
            "away": g["awayTeam"],
        }
    return out


def our_game_ids(year: int) -> set[str]:
    """Game IDs we have data for this season. Prefer the checkpoint's scraped
    list (works mid-season before any merge); fall back to the merged CSV."""
    ckpt = DATA_DIR / "checkpoint.json"
    if ckpt.exists():
        cp = json.loads(ckpt.read_text())
        ids = set(cp.get(str(year), {}).get("scraped", []))
        if ids:
            return ids
    path = DATA_DIR / f"pbp_{year}_regular.csv"
    if path.exists():
        df = pd.read_csv(path, dtype={"game_id": str}, usecols=["game_id"], keep_default_na=False)
        return set(df["game_id"].unique())
    return set()


def resolve_season(year: int, report: list) -> dict[str, str]:
    """Return game_id -> {home_coach, away_coach} for one season."""
    coaches = fetch_coaches(year)
    prior = fetch_coaches(year - 1)
    games = fetch_games(year)
    ids = our_game_ids(year)

    # Restrict to games present in our dataset
    games = {gid: g for gid, g in games.items() if gid in ids}

    # Per-school: ordered list of that school's in-dataset games (by date)
    school_games: dict[str, list] = defaultdict(list)
    for gid, g in games.items():
        school_games[g["home"]].append(gid)
        school_games[g["away"]].append(gid)
    for s in school_games:
        school_games[s].sort(key=lambda gid: games[gid]["date"])

    # school -> {game_id: coach}
    school_coach_by_game: dict[str, dict] = {}

    for school, clist in coaches.items():
        sgames = school_games.get(school, [])
        if not sgames:
            continue

        if len(clist) == 1:
            school_coach_by_game[school] = {gid: clist[0][0] for gid in sgames}
            continue

        # Multiple coaches -> resolve the in-season split
        cfbd_total = sum(g for _, g in clist)
        bowl_extra = cfbd_total - len(sgames)  # games outside our data (bowls)

        prior_hc = {n for n, _ in clist if n in {nm for nm, _ in prior.get(school, [])}}
        if len(prior_hc) == 1:
            opener = next(iter(prior_hc))
        else:  # no/ambiguous prior record -> opener = most games
            opener = max(clist, key=lambda x: x[1])[0]

        enders = [(n, g) for n, g in clist if n != opener]
        # Pick the ender that actually coached games (ignore 0-game offseason hires)
        enders = [(n, g) for n, g in enders if g > 0]

        assign = {gid: opener for gid in sgames}
        ender_rows = []
        if enders:
            ender, ender_games = max(enders, key=lambda x: x[1]) if len(enders) > 1 else enders[0]
            k = max(ender_games - bowl_extra, 0)  # ender's in-dataset games
            for gid in sgames[len(sgames) - k:]:
                assign[gid] = ender
            ender_rows = sgames[len(sgames) - k:]

        school_coach_by_game[school] = assign

        # Record for verification report (only true splits)
        if enders and k > 0:
            report.append({
                "year": year, "school": school,
                "opener": opener, "ender": ender,
                "opener_games": len(sgames) - k, "ender_games": k,
                "bowl_excluded": bowl_extra,
                "ender_first_date": games[ender_rows[0]]["date"][:10] if ender_rows else "-",
            })

    # Build game_id -> coaches. Schools with no FBS coach record are non-FBS
    # (FCS) opponents — CFBD does not track their coaches.
    result = {}
    for gid, g in games.items():
        hc = school_coach_by_game.get(g["home"], {}).get(gid, "Non-FBS")
        ac = school_coach_by_game.get(g["away"], {}).get(gid, "Non-FBS")
        result[gid] = {"home_coach": hc, "away_coach": ac}
    return result


def seasons_from_checkpoint() -> list[int]:
    """Seasons that have any scraped games, per the checkpoint. Falls back to
    the SEASONS constant if no checkpoint exists yet."""
    ckpt = DATA_DIR / "checkpoint.json"
    if ckpt.exists():
        cp = json.loads(ckpt.read_text())
        yrs = sorted(int(k) for k, v in cp.items()
                     if k.isdigit() and v.get("scraped"))
        if yrs:
            return yrs
    return SEASONS


def build(seasons: list[int] | None = None) -> list:
    """Build/refresh data/coaches.json for the given seasons (default: all
    seasons present in the checkpoint). Returns the mid-season-change report."""
    seasons = seasons or seasons_from_checkpoint()
    all_map: dict = {}
    report: list = []
    for year in seasons:
        print(f"[{year}] resolving coaches...")
        season_map = resolve_season(year, report)
        all_map.update(season_map)
        nonfbs = sum(1 for v in season_map.values()
                     if v["home_coach"] == "Non-FBS" or v["away_coach"] == "Non-FBS")
        print(f"[{year}] {len(season_map)} games mapped ({nonfbs} with a non-FBS opponent)")

    all_map["_report"] = report
    COACHES_JSON.write_text(json.dumps(all_map, indent=2))
    print(f"Saved {COACHES_JSON}")
    return report


def print_report(report: list) -> None:
    print("\n=== Mid-season coaching-change attribution (verify these) ===")
    for r in sorted(report, key=lambda x: (x["year"], x["school"])):
        print(f"  {r['year']} {r['school']:18s}: {r['opener']} (first {r['opener_games']}) "
              f"-> {r['ender']} (last {r['ender_games']}, from {r['ender_first_date']})"
              + (f"  [+{r['bowl_excluded']} bowl excl]" if r['bowl_excluded'] else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="Re-print report from cache")
    ap.add_argument("--season", type=int, help="Build a single season only")
    args = ap.parse_args()

    if args.report and COACHES_JSON.exists():
        cache = json.loads(COACHES_JSON.read_text())
        print_report(cache.get("_report", []))
        return

    report = build([args.season] if args.season else None)
    print_report(report)


if __name__ == "__main__":
    main()
