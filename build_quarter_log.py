"""
Build the quarter-level game log (long grain: one row per team-game-quarter).

Reads data/pbp_{season}_regular.csv + data/game_context.json, aggregates into the
quarter_log, writes data/quarter_log_{season}.csv, and (when SUPABASE_DB_URL is
set) upserts to Postgres.

Attribution (per the approved plan):
  - DRIVE-STRUCTURE metrics (drive count, starting position, yards, time of
    possession, plays/drive, pass-rush split, 3-and-outs, red-zone trips) are keyed
    to the quarter the drive STARTED.
  - SCORING / event metrics (points, TD drives, FG drives, turnovers, explosive
    plays, sacks, punts) are keyed to the quarter the PLAY happened (wall-clock),
    matching how sportsbooks settle quarter markets.

Mirror trick: a team's "Against" block == its opponent's "For" block for the same
game+quarter, so offensive aggregates are computed once per (game, offense, quarter)
and each output row pairs own-offense (For) with opponent-offense (Against).

Run:
    python build_quarter_log.py            # all seasons in checkpoint
    python build_quarter_log.py --season 2024
    python build_quarter_log.py --season 2024 --no-db   # skip Supabase upsert
"""

import argparse
import json
import os
from collections import defaultdict

import pandas as pd
from dotenv import load_dotenv

from build_coaches import seasons_from_checkpoint, DATA_DIR
from build_game_context import GAME_CONTEXT_JSON

load_dotenv()

PASS, RUN = "pass", "run"
EXPLOSIVE_YDS = 20
REDZONE_YDS = 20
QUARTERS = ["1", "2", "3", "4", "OT"]


def qbucket(q: int) -> str:
    return str(int(q)) if int(q) <= 4 else "OT"


def game_seconds(quarter: int, secs_left_quarter: int):
    """Absolute regulation seconds elapsed at a play; None in OT."""
    q = int(quarter)
    if q > 4:
        return None
    return (q - 1) * 900 + (900 - int(secs_left_quarter))


# ---------------------------------------------------------------------------
# Per-drive reconstruction
# ---------------------------------------------------------------------------

def _drive_records(g: pd.DataFrame) -> list[dict]:
    """One record per real drive (game_poss_num >= 1) for a single game."""
    drives = []
    real = g[g["game_poss_num"] >= 1]
    for poss, d in real.groupby("game_poss_num", sort=True):
        first = d.iloc[0]
        last = d.iloc[-1]
        offense = first["offensive_team"]
        scrim = d[d["play_type"].isin([PASS, RUN])]
        n_pass = int((d["play_type"] == PASS).sum())
        n_rush = int((d["play_type"] == RUN).sum())
        scrim_yds = pd.to_numeric(scrim["play_yards"], errors="coerce").sum()
        pass_yds = pd.to_numeric(d.loc[d["play_type"] == PASS, "play_yards"], errors="coerce").sum()
        run_yds = pd.to_numeric(d.loc[d["play_type"] == RUN, "play_yards"], errors="coerce").sum()

        # Outcome + the quarter the ending event happened (wall-clock)
        outcome, event_q = "other", None
        td_off = d[(d["is_touchdown"]) & (d["offensive_team"] == offense)
                   & (d["play_type"].isin([PASS, RUN]))]
        # made FG: a field-goal play whose text isn't a miss/block (independent of
        # the scoring_play flag)
        fg_made = d[(d["is_field_goal"])
                    & ~d["play_desc"].str.contains(r"missed|blocked|no good", case=False, regex=True, na=False)]
        to_play = d[d["is_turnover"]]
        punt_play = d[d["is_punt"]]
        if len(td_off):
            outcome, event_q = "td", qbucket(td_off.iloc[0]["quarter"])
        elif len(fg_made):
            outcome, event_q = "fg", qbucket(fg_made.iloc[0]["quarter"])
        elif len(to_play):
            outcome, event_q = "turnover", qbucket(to_play.iloc[-1]["quarter"])
        elif len(punt_play):
            outcome, event_q = "punt", qbucket(punt_play.iloc[-1]["quarter"])

        ytz = pd.to_numeric(d["yards_to_end_zone"], errors="coerce")
        reached_rz = bool((ytz <= REDZONE_YDS).any())

        drives.append({
            "offense": offense,
            "start_q": qbucket(first["quarter"]),
            "start_gs": game_seconds(first["quarter"], first["secs_left_quarter"]),
            "start_pos": pd.to_numeric(first["yards_to_end_zone"], errors="coerce"),
            "plays": n_pass + n_rush,
            "pass_plays": n_pass,
            "rush_plays": n_rush,
            "scrim_yards": scrim_yds,
            "pass_yds": pass_yds,
            "run_yds": run_yds,
            "yards": pd.to_numeric(last["poss_yards"], errors="coerce"),
            "outcome": outcome,
            "event_q": event_q,
            "three_and_out": (n_pass + n_rush) <= 3 and outcome == "punt",
            "rz_trip": reached_rz,
            "rz_td": reached_rz and outcome == "td",
            "top": None,  # filled below
        })

    # Time of possession: next drive's start (within the same half) minus this start
    ordered = sorted(
        [d for d in drives if d["start_gs"] is not None], key=lambda x: x["start_gs"]
    )
    for i, dr in enumerate(ordered):
        half_end = 1800 if dr["start_gs"] < 1800 else 3600
        nxt = ordered[i + 1]["start_gs"] if i + 1 < len(ordered) else None
        end = nxt if (nxt is not None and nxt <= half_end) else half_end
        dr["top"] = max(end - dr["start_gs"], 0)
    return drives


# ---------------------------------------------------------------------------
# Per-game aggregation
# ---------------------------------------------------------------------------

def _zero_block() -> dict:
    return defaultdict(float)


def process_game(gid: str, g: pd.DataFrame, ctx: dict) -> list[dict]:
    home = g.iloc[0]["home_team"]
    away = g.iloc[0]["away_team"]
    teams = {home: "home", away: "away"}

    # Which quarter buckets occur in this game (always 1-4; OT if present)
    buckets = list(QUARTERS[:4])
    if (g["quarter"] > 4).any():
        buckets.append("OT")

    drives = _drive_records(g)

    # Offensive aggregates per (offense, quarter). 'struct' keyed by drive-start
    # quarter; 'event' keyed by the event's wall-clock quarter.
    struct: dict = defaultdict(_zero_block)   # (offense, start_q) -> sums
    event: dict = defaultdict(_zero_block)    # (offense, event_q) -> counts
    for dr in drives:
        s = struct[(dr["offense"], dr["start_q"])]
        s["drives"] += 1
        s["plays"] += dr["plays"]
        s["pass_plays"] += dr["pass_plays"]
        s["rush_plays"] += dr["rush_plays"]
        if pd.notna(dr["scrim_yards"]):
            s["scrim_yards_sum"] += dr["scrim_yards"]
        if pd.notna(dr["pass_yds"]):
            s["pass_yds_sum"] += dr["pass_yds"]
        if pd.notna(dr["run_yds"]):
            s["run_yds_sum"] += dr["run_yds"]
        if pd.notna(dr["start_pos"]):
            s["start_pos_sum"] += dr["start_pos"]; s["start_pos_n"] += 1
        if pd.notna(dr["yards"]):
            s["yards_sum"] += dr["yards"]; s["yards_n"] += 1
        if dr["top"] is not None:
            s["top_sum"] += dr["top"]; s["top_n"] += 1
        s["three_and_outs"] += int(dr["three_and_out"])
        s["rz_trips"] += int(dr["rz_trip"])
        s["rz_tds"] += int(dr["rz_td"])
        if dr["event_q"]:
            e = event[(dr["offense"], dr["event_q"])]
            if dr["outcome"] == "td":
                e["td_drives"] += 1
            elif dr["outcome"] == "fg":
                e["fg_drives"] += 1
            elif dr["outcome"] == "turnover":
                e["turnovers"] += 1

    # Play-level wall-clock events: explosive plays, sacks, punts
    play_ev: dict = defaultdict(_zero_block)  # (offense, play_q) -> counts
    for _, p in g.iterrows():
        if p["game_poss_num"] < 1:
            continue
        q = qbucket(p["quarter"])
        pe = play_ev[(p["offensive_team"], q)]
        if pd.notna(pd.to_numeric(p["play_yards"], errors="coerce")) \
                and float(pd.to_numeric(p["play_yards"], errors="coerce")) >= EXPLOSIVE_YDS:
            pe["explosive"] += 1
        if p["is_sack"]:
            pe["sacks_taken"] += 1
        if p["is_punt"]:
            pe["punts"] += 1

    # Points per (side, quarter) from CFBD authoritative line scores (robust to
    # ESPN's non-monotonic per-row score columns). Fall back to cumulative score
    # deltas only if line scores are missing (e.g. a not-yet-final live game).
    c = ctx.get(gid, {})
    line = {"home": c.get("home_line_scores"), "away": c.get("away_line_scores")}
    points = {}
    if isinstance(line["home"], list) and isinstance(line["away"], list):
        for side in ("home", "away"):
            ls = line[side]
            for q in buckets:
                if q == "OT":
                    points[(side, q)] = int(sum(x for x in ls[4:] if x is not None)) if len(ls) > 4 else 0
                else:
                    i = int(q) - 1
                    points[(side, q)] = int(ls[i]) if i < len(ls) and ls[i] is not None else 0
    else:  # fallback: end-of-quarter cumulative score deltas
        for side, col in (("home", "home_score"), ("away", "away_score")):
            prev = 0
            for q in buckets:
                qp = g[g["quarter"].apply(qbucket) == q]
                end = int(qp.iloc[-1][col]) if len(qp) else prev
                points[(side, q)] = end - prev
                prev = end

    # Cumulative score entering each quarter (for margin_entering)
    start_score = {"home": {}, "away": {}}
    for side in ("home", "away"):
        prev = 0
        for q in buckets:
            start_score[side][q] = prev
            prev += points[(side, q)]

    rows = []
    for team, side in teams.items():
        opp = away if side == "home" else home
        opp_side = "away" if side == "home" else "home"
        for q in buckets:
            sf, ef, pf = struct.get((team, q), {}), event.get((team, q), {}), play_ev.get((team, q), {})
            sa, ea, pa = struct.get((opp, q), {}), event.get((opp, q), {}), play_ev.get((opp, q), {})

            def avg(d, num, den):
                n = d.get(den, 0)
                return round(d.get(num, 0) / n, 3) if n else None

            row = {
                "season": c.get("season"),
                "game_id": gid,
                "date": (c.get("date") or "")[:10],
                "team": team,
                "opponent": opp,
                "is_home": side == "home",
                "home_away": "Home" if side == "home" else "Away",
                "quarter": q,
                "team_coach": c.get(f"{side}_coach"),
                "team_wins_entering": c.get(f"{side}_team_wins"),
                "team_losses_entering": c.get(f"{side}_team_losses"),
                "opp_coach": c.get(f"{opp_side}_coach"),
                "opp_wins_entering": c.get(f"{opp_side}_team_wins"),
                "opp_losses_entering": c.get(f"{opp_side}_team_losses"),
                "team_coach_wins_entering": c.get(f"{side}_coach_wins"),
                "team_coach_losses_entering": c.get(f"{side}_coach_losses"),
                "opp_coach_wins_entering": c.get(f"{opp_side}_coach_wins"),
                "opp_coach_losses_entering": c.get(f"{opp_side}_coach_losses"),
                "margin_entering": start_score[side][q] - start_score[opp_side][q],
                # ---- For (offense) ----
                "drives_for": int(sf.get("drives", 0)),
                "td_drives_for": int(ef.get("td_drives", 0)),
                "fg_drives_for": int(ef.get("fg_drives", 0)),
                "turnovers_for": int(ef.get("turnovers", 0)),
                "points_for": points[(side, q)],
                "avg_start_pos_for": avg(sf, "start_pos_sum", "start_pos_n"),
                "avg_yards_for": avg(sf, "yards_sum", "yards_n"),
                "yards_per_play_for": avg(sf, "scrim_yards_sum", "plays"),
                "avg_secs_per_drive_for": avg(sf, "top_sum", "top_n"),
                "plays_per_drive_for": avg(sf, "plays", "drives"),
                "pass_plays_per_drive_for": avg(sf, "pass_plays", "drives"),
                "rush_plays_per_drive_for": avg(sf, "rush_plays", "drives"),
                "plays_for": int(sf.get("plays", 0)),
                "pass_plays_for": int(sf.get("pass_plays", 0)),
                "rush_plays_for": int(sf.get("rush_plays", 0)),
                "yards_per_pass_for": avg(sf, "pass_yds_sum", "pass_plays"),
                "yards_per_run_for": avg(sf, "run_yds_sum", "rush_plays"),
                "secs_per_play_for": avg(sf, "top_sum", "plays"),
                "three_and_outs_for": int(sf.get("three_and_outs", 0)),
                "redzone_trips_for": int(sf.get("rz_trips", 0)),
                "redzone_tds_for": int(sf.get("rz_tds", 0)),
                "explosive_plays_for": int(pf.get("explosive", 0)),
                "sacks_allowed_for": int(pf.get("sacks_taken", 0)),
                "punts_for": int(pf.get("punts", 0)),
                "total_top_for": int(sf.get("top_sum", 0)),
                # ---- Against (defense = opponent offense) ----
                "drives_against": int(sa.get("drives", 0)),
                "td_drives_against": int(ea.get("td_drives", 0)),
                "fg_drives_against": int(ea.get("fg_drives", 0)),
                "takeaways": int(ea.get("turnovers", 0)),
                "points_against": points[(opp_side, q)],
                "avg_start_pos_against": avg(sa, "start_pos_sum", "start_pos_n"),
                "avg_yards_against": avg(sa, "yards_sum", "yards_n"),
                "yards_per_play_against": avg(sa, "scrim_yards_sum", "plays"),
                "avg_secs_per_drive_against": avg(sa, "top_sum", "top_n"),
                "plays_per_drive_against": avg(sa, "plays", "drives"),
                "pass_plays_per_drive_against": avg(sa, "pass_plays", "drives"),
                "rush_plays_per_drive_against": avg(sa, "rush_plays", "drives"),
                "plays_against": int(sa.get("plays", 0)),
                "pass_plays_against": int(sa.get("pass_plays", 0)),
                "rush_plays_against": int(sa.get("rush_plays", 0)),
                "yards_per_pass_against": avg(sa, "pass_yds_sum", "pass_plays"),
                "yards_per_run_against": avg(sa, "run_yds_sum", "rush_plays"),
                "secs_per_play_against": avg(sa, "top_sum", "plays"),
                "three_and_outs_against": int(sa.get("three_and_outs", 0)),
                "redzone_trips_against": int(sa.get("rz_trips", 0)),
                "redzone_tds_against": int(sa.get("rz_tds", 0)),
                "explosive_plays_against": int(pa.get("explosive", 0)),
                "sacks_made": int(pa.get("sacks_taken", 0)),
                "punts_against": int(pa.get("punts", 0)),
                "total_top_against": int(sa.get("top_sum", 0)),
            }
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Season driver
# ---------------------------------------------------------------------------

def build_season(season: int, ctx: dict) -> pd.DataFrame:
    path = DATA_DIR / f"pbp_{season}_regular.csv"
    if not path.exists():
        print(f"[{season}] {path} not found, skipping")
        return pd.DataFrame()
    df = pd.read_csv(path, dtype={"game_id": str}, keep_default_na=False, low_memory=False)
    # numeric coercions
    for col in ["quarter", "secs_left_quarter", "secs_left_reg", "home_score",
                "away_score", "game_poss_num", "poss_play_num"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["is_touchdown", "is_field_goal", "is_turnover", "is_punt", "is_sack",
                "scoring_play"]:
        df[col] = df[col].astype(str).str.lower().isin(["true", "1"])

    all_rows = []
    for gid, g in df.groupby("game_id", sort=False):
        all_rows.extend(process_game(gid, g, ctx))
    out = pd.DataFrame(all_rows)
    out_path = DATA_DIR / f"quarter_log_{season}.csv"
    out.to_csv(out_path, index=False)
    print(f"[{season}] {len(out):,} quarter-rows -> {out_path}")
    return out


def load_to_postgres(df: pd.DataFrame, season: int) -> bool:
    """Idempotent season-grained upsert: delete the season's rows, then append.
    Uses SUPABASE_DB_URL (.env); no-op with a message if it isn't set. Works
    headless (Task Scheduler) — the Supabase MCP is interactive-session only."""
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        print(f"[{season}] SUPABASE_DB_URL not set; skipped DB load (CSV written).")
        return False
    from sqlalchemy import create_engine, text
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix):]
            break
    engine = create_engine(url, pool_pre_ping=True)
    try:
        dff = df.astype(object).where(pd.notnull(df), None)  # NaN -> NULL
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM quarter_log WHERE season = :s"), {"s": int(season)})
            dff.to_sql("quarter_log", conn, if_exists="append", index=False,
                       method="multi", chunksize=500)
        print(f"[{season}] loaded {len(df):,} rows to Supabase")
        return True
    finally:
        engine.dispose()


def build(seasons: list[int] | None = None, use_db: bool = True) -> None:
    seasons = seasons or seasons_from_checkpoint()
    ctx = json.loads(GAME_CONTEXT_JSON.read_text()) if GAME_CONTEXT_JSON.exists() else {}
    for season in seasons:
        df = build_season(season, ctx)
        if use_db and len(df):
            try:
                load_to_postgres(df, season)
            except Exception as e:
                print(f"[{season}] WARN: Supabase load failed: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int)
    ap.add_argument("--no-db", action="store_true")
    args = ap.parse_args()
    build([args.season] if args.season else None, use_db=not args.no_db)


if __name__ == "__main__":
    main()
