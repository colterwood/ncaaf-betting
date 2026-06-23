"""
Build data/game_context.json: per-game date, final score, winner, and
POINT-IN-TIME (season-to-date, entering the game) win/loss records for both
teams and both coaches.

These are game-level facts that every quarter-log row of a team-game shares, so
they're computed once here and joined by (game_id, home/away side) in
build_quarter_log.py. Reused later by the drives feature too.

Method: order each season's games by date and accumulate completed-game outcomes
BEFORE each game. Coach records use the per-game coach attribution from
coaches.json (so mid-season changes and interims are handled correctly). FCS
(non-FBS) opponents have no reliable schedule/coach in CFBD -> their team and
coach records are null.

Run:
    python build_game_context.py            # all seasons in checkpoint
    python build_game_context.py --season 2024
"""

import argparse
import json
from collections import defaultdict

import requests
from dotenv import load_dotenv

from build_coaches import (
    _headers, fetch_coaches, seasons_from_checkpoint, CFBD, DATA_DIR, COACHES_JSON,
)

load_dotenv()

GAME_CONTEXT_JSON = DATA_DIR / "game_context.json"
_NOT_A_COACH = {"Non-FBS", "Unknown", None, ""}


def fetch_games_full(year: int) -> dict[str, dict]:
    """game_id -> {date, home, away, home_points, away_points, completed}."""
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
            "home_points": g.get("homePoints"),
            "away_points": g.get("awayPoints"),
            "home_line": g.get("homeLineScores"),   # authoritative per-quarter points
            "away_line": g.get("awayLineScores"),
            "completed": bool(g.get("completed")),
        }
    return out


def _winner_side(g: dict) -> str | None:
    hp, ap = g["home_points"], g["away_points"]
    if hp is None or ap is None:
        return None
    if hp > ap:
        return "home"
    if ap > hp:
        return "away"
    return "tie"


def build(seasons: list[int] | None = None) -> dict:
    seasons = seasons or seasons_from_checkpoint()
    coaches_map = json.loads(COACHES_JSON.read_text()) if COACHES_JSON.exists() else {}

    context: dict = {}
    for year in seasons:
        print(f"[{year}] building game context...")
        games = fetch_games_full(year)
        fbs_teams = set(fetch_coaches(year).keys())

        order = sorted(games, key=lambda gid: (games[gid]["date"], gid))
        team_rec: dict = defaultdict(lambda: [0, 0])   # team -> [w, l] season-to-date
        coach_rec: dict = defaultdict(lambda: [0, 0])  # coach -> [w, l] season-to-date

        for gid in order:
            g = games[gid]
            ht, at = g["home"], g["away"]
            cm = coaches_map.get(gid, {})
            hc = cm.get("home_coach")
            ac = cm.get("away_coach")

            # Snapshot records ENTERING this game (before applying its result)
            def rec(key, store, ok):
                return list(store[key]) if ok else None
            ht_rec = rec(ht, team_rec, ht in fbs_teams)
            at_rec = rec(at, team_rec, at in fbs_teams)
            hc_rec = rec(hc, coach_rec, hc not in _NOT_A_COACH)
            ac_rec = rec(ac, coach_rec, ac not in _NOT_A_COACH)

            context[gid] = {
                "season": year,
                "date": g["date"],
                "home_team": ht, "away_team": at,
                "home_points": g["home_points"], "away_points": g["away_points"],
                "home_line_scores": g["home_line"], "away_line_scores": g["away_line"],
                "winner_side": _winner_side(g),
                "home_team_wins":   ht_rec[0] if ht_rec else None,
                "home_team_losses": ht_rec[1] if ht_rec else None,
                "away_team_wins":   at_rec[0] if at_rec else None,
                "away_team_losses": at_rec[1] if at_rec else None,
                "home_coach": hc if hc not in _NOT_A_COACH else None,
                "away_coach": ac if ac not in _NOT_A_COACH else None,
                "home_coach_wins":   hc_rec[0] if hc_rec else None,
                "home_coach_losses": hc_rec[1] if hc_rec else None,
                "away_coach_wins":   ac_rec[0] if ac_rec else None,
                "away_coach_losses": ac_rec[1] if ac_rec else None,
            }

            # Apply this game's outcome to the accumulators (completed games only)
            if not g["completed"]:
                continue
            side = _winner_side(g)
            if side == "home":
                team_rec[ht][0] += 1; team_rec[at][1] += 1
                if hc not in _NOT_A_COACH: coach_rec[hc][0] += 1
                if ac not in _NOT_A_COACH: coach_rec[ac][1] += 1
            elif side == "away":
                team_rec[at][0] += 1; team_rec[ht][1] += 1
                if ac not in _NOT_A_COACH: coach_rec[ac][0] += 1
                if hc not in _NOT_A_COACH: coach_rec[hc][1] += 1
            # tie -> no W/L change

        print(f"[{year}] {len(order)} games contextualized")

    GAME_CONTEXT_JSON.write_text(json.dumps(context, indent=2))
    print(f"Saved {GAME_CONTEXT_JSON} ({len(context)} games)")
    return context


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, help="Build a single season only")
    args = ap.parse_args()
    build([args.season] if args.season else None)


if __name__ == "__main__":
    main()
