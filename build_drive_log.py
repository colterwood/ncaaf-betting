"""
Build the drive-level feature table (long grain: one row per drive).

Reads data/pbp_{season}_regular.csv + data/game_context.json, writes
data/drive_log_{season}.csv, and (when SUPABASE_DB_URL is set) upserts to Postgres.

Grain: one row per real drive (game_poss_num >= 1). Team = the offense, Opponent
= the defense. Reuses game_context for date / point-in-time team & coach records.

Run:
    python build_drive_log.py            # all seasons in checkpoint
    python build_drive_log.py --season 2024
    python build_drive_log.py --season 2024 --no-db
"""

import argparse
import json
import os
import re
from collections import Counter, defaultdict

import pandas as pd
from dotenv import load_dotenv

from build_coaches import seasons_from_checkpoint, DATA_DIR
from build_game_context import GAME_CONTEXT_JSON
from build_quarter_log import qbucket, game_seconds

load_dotenv()

PASS, RUN = "pass", "run"
EXPLOSIVE_YDS = 20
REDZONE_YDS = 20
_FG_MISS_RE = re.compile(r"missed|blocked|no good", re.I)
_NO_QB = ("", "N/A")


def _is_qb(q) -> bool:
    """True if `q` is a real quarterback name (not blank / not the 'N/A' marker)."""
    return isinstance(q, str) and q.strip() not in _NO_QB


def _drive_qb(d: pd.DataFrame) -> str | None:
    """The QB on the most plays in this drive; ties broken by earliest appearance.
    Returns None when no play in the drive names a QB (e.g. all runs/kneels)."""
    qbs = [q for q in d["quarterback"].tolist() if _is_qb(q)]
    if not qbs:
        return None
    counts = Counter(qbs)
    top = max(counts.values())
    for q in qbs:  # play order -> earliest among the most-frequent
        if counts[q] == top:
            return q
    return qbs[0]


def _half(start_gs, quarter: int):
    if int(quarter) > 4:
        return "OT"
    return 1 if (start_gs is not None and start_gs < 1800) else 2


def _drive_result(d: pd.DataFrame, offense: str) -> str:
    td_off = (d["is_touchdown"] & (d["offensive_team"] == offense)
              & d["play_type"].isin([PASS, RUN])).any()
    if td_off:
        return "TD"
    fg = d[d["is_field_goal"]]
    if len(fg):
        missed = fg["play_desc"].str.contains(_FG_MISS_RE, na=False).any()
        return "MISSED_FG" if missed else "FG"
    if d["play_desc"].str.contains(r"\bsafety\b", case=False, na=False).any():
        return "SAFETY"
    if d["play_desc"].str.contains(r"\bintercepted\b", case=False, na=False).any():
        return "INT"
    if (d["is_turnover"] & d["play_desc"].str.contains(r"fumble", case=False, na=False)).any():
        return "FUMBLE"
    if d["is_punt"].any():
        return "PUNT"
    # Turnover on downs: drive ends on a failed 4th-down scrimmage play (a
    # successful 4th-down conversion would continue the drive, not end it).
    if d["play_desc"].str.contains(r"turnover on downs|on downs", case=False, na=False).any():
        return "DOWNS"
    scrim = d[d["play_type"].isin([PASS, RUN])]
    if len(scrim) and scrim.iloc[-1]["down"] == 4:
        return "DOWNS"
    last_type = d.iloc[-1]["play_type"]
    if last_type in ("end of half", "end period"):
        return "END_HALF"
    if last_type == "end of game":
        return "END_GAME"
    return "OTHER"


def process_game_drives(gid: str, g: pd.DataFrame, ctx: dict, season: int) -> list[dict]:
    c = ctx.get(gid, {})
    home, away = g.iloc[0]["home_team"], g.iloc[0]["away_team"]

    real = g[g["game_poss_num"] >= 1]
    if real.empty:
        return []

    # First pass: drive start clock for TOP linking + per-quarter/half ordinals
    starts = {}  # poss -> game_seconds at first play
    start_q = {}
    raw_qb, offense_of = {}, {}
    for poss, d in real.groupby("game_poss_num", sort=True):
        f = d.iloc[0]
        starts[poss] = game_seconds(f["quarter"], f["secs_left_quarter"])
        start_q[poss] = qbucket(f["quarter"])
        raw_qb[poss] = _drive_qb(d)
        offense_of[poss] = f["offensive_team"]

    # Rank QBs by the order they first LEAD a drive (become a drive's primary QB),
    # chronologically, per team: 1 = the team's first QB to run a drive, 2 = the
    # next new one, etc. -> the `personnel` field. A one-off trick-play passer who
    # never leads a drive isn't counted, which keeps ranks clean and 1-based.
    qb_rank: dict = defaultdict(dict)
    for poss in sorted(raw_qb):
        qb, team = raw_qb[poss], offense_of[poss]
        if qb is not None and qb not in qb_rank[team]:
            qb_rank[team][qb] = len(qb_rank[team]) + 1

    # Each drive's QB = most-frequent passer; QB-less drives (runs/kneels) inherit
    # the team's nearest identifiable drive in this game (prev, else next).
    team_poss = defaultdict(list)
    for poss in sorted(raw_qb):
        team_poss[offense_of[poss]].append(poss)
    drive_qb: dict = {}
    for team, plist in team_poss.items():
        last = None
        fwd = {}
        for p in plist:                # forward-fill: carry the prior known QB
            if raw_qb[p] is not None:
                last = raw_qb[p]
            fwd[p] = last
        nxt = None
        for p in reversed(plist):      # back-fill leading gaps: carry the next QB
            if fwd[p] is not None:
                nxt = fwd[p]
            drive_qb[p] = fwd[p] if fwd[p] is not None else nxt

    ordered = sorted(starts, key=lambda p: (starts[p] is None, starts[p] if starts[p] is not None else 0, p))
    top = {}
    for i, poss in enumerate(ordered):
        gs = starts[poss]
        if gs is None:
            top[poss] = None
            continue
        half_end = 1800 if gs < 1800 else 3600
        nxt = next((starts[ordered[j]] for j in range(i + 1, len(ordered))
                    if starts[ordered[j]] is not None), None)
        end = nxt if (nxt is not None and nxt <= half_end) else half_end
        top[poss] = max(end - gs, 0)
    # per-quarter / per-half ordinals (by start order, both teams)
    q_counter, h_counter = defaultdict(int), defaultdict(int)
    drive_num_q, drive_num_h = {}, {}
    for poss in ordered:
        f = real[real["game_poss_num"] == poss].iloc[0]
        q = qbucket(f["quarter"])
        h = _half(starts[poss], f["quarter"])
        q_counter[q] += 1
        h_counter[h] += 1
        drive_num_q[poss] = q_counter[q]
        drive_num_h[poss] = h_counter[h]

    rows = []
    for poss, d in real.groupby("game_poss_num", sort=True):
        offense = d.iloc[0]["offensive_team"]
        side = "home" if offense == home else "away"
        opp_side = "away" if side == "home" else "home"
        opponent = away if side == "home" else home
        f, last = d.iloc[0], d.iloc[-1]

        scrim = d[d["play_type"].isin([PASS, RUN])]
        n_pass = int((d["play_type"] == PASS).sum())
        n_run = int((d["play_type"] == RUN).sum())
        pass_yds = pd.to_numeric(d.loc[d["play_type"] == PASS, "play_yards"], errors="coerce").sum()
        run_yds = pd.to_numeric(d.loc[d["play_type"] == RUN, "play_yards"], errors="coerce").sum()

        start_pos = pd.to_numeric(f["yards_to_end_zone"], errors="coerce")
        total_yards = pd.to_numeric(last["poss_yards"], errors="coerce")
        end_pos = None
        if pd.notna(start_pos) and pd.notna(total_yards):
            end_pos = max(int(start_pos) - int(total_yards), 0)

        # 3rd downs (scrimmage) + conversions (gained the line to gain, or TD)
        third = scrim[scrim["down"] == 3]
        third_downs = len(third)
        conv = 0
        for _, p in third.iterrows():
            dist = pd.to_numeric(p["distance"], errors="coerce")
            py = pd.to_numeric(p["play_yards"], errors="coerce")
            if bool(p["is_touchdown"]) or (pd.notna(dist) and dist > 0 and pd.notna(py) and py >= dist):
                conv += 1

        # points scored by the offense on this drive
        result = _drive_result(d, offense)
        pts = 0
        if result == "TD":
            pts = 6 + (1 if d["is_extra_point"].any() else 0) + (2 if d["is_two_point_conversion"].any() else 0)
        elif result == "FG":
            pts = 3

        ytz = pd.to_numeric(d["yards_to_end_zone"], errors="coerce")
        py_all = pd.to_numeric(d["play_yards"], errors="coerce")

        # score entering the drive (first play's cumulative score)
        hs, as_ = int(f["home_score"]), int(f["away_score"])
        team_score, opp_score = (hs, as_) if side == "home" else (as_, hs)

        rows.append({
            # season is authoritative from the per-season file we're iterating;
            # don't rely on game_context having an entry for this game_id.
            "season": season,
            "game_id": gid,
            "date": (c.get("date") or "")[:10],
            "team": offense,
            "opponent": opponent,
            "is_home": side == "home",
            "home_away": "Home" if side == "home" else "Away",
            "quarterback": drive_qb.get(poss),
            "personnel": qb_rank.get(offense, {}).get(drive_qb.get(poss)),
            "team_coach": c.get(f"{side}_coach"),
            "team_wins_entering": c.get(f"{side}_team_wins"),
            "team_losses_entering": c.get(f"{side}_team_losses"),
            "opp_coach": c.get(f"{opp_side}_coach"),
            "opp_wins_entering": c.get(f"{opp_side}_team_wins"),
            "opp_losses_entering": c.get(f"{opp_side}_team_losses"),
            "drive_num": int(poss),
            "drive_num_quarter": drive_num_q[poss],
            "drive_num_half": drive_num_h[poss],
            "quarter": start_q[poss],
            "half": str(_half(starts[poss], f["quarter"])),
            "margin_entering": team_score - opp_score,
            "start_position": int(start_pos) if pd.notna(start_pos) else None,
            "end_position": end_pos,
            "drive_result": result,
            "total_yards": int(total_yards) if pd.notna(total_yards) else None,
            "num_plays": n_pass + n_run,
            "num_pass_plays": n_pass,
            "num_run_plays": n_run,
            "third_downs": third_downs,
            "third_down_conversions": conv,
            "yards_per_pass": round(pass_yds / n_pass, 3) if n_pass else None,
            "yards_per_run": round(run_yds / n_run, 3) if n_run else None,
            "yards_per_play": round((pass_yds + run_yds) / (n_pass + n_run), 3) if (n_pass + n_run) else None,
            "total_secs": top[poss],
            "secs_per_play": round(top[poss] / (n_pass + n_run), 3)
            if (top[poss] is not None and (n_pass + n_run)) else None,
            "points_scored": pts,
            "is_scoring_drive": pts > 0,
            "reached_redzone": bool((ytz <= REDZONE_YDS).any()),
            "explosive_plays": int((py_all >= EXPLOSIVE_YDS).sum()),
            "sacks": int(d["is_sack"].sum()),
            "penalties": int((d["play_type"] == "penalty").sum()),
        })
    return rows


def build_season(season: int, ctx: dict) -> pd.DataFrame:
    path = DATA_DIR / f"pbp_{season}_regular.csv"
    if not path.exists():
        print(f"[{season}] {path} not found, skipping")
        return pd.DataFrame()
    df = pd.read_csv(path, dtype={"game_id": str}, keep_default_na=False, low_memory=False)
    for col in ["quarter", "secs_left_quarter", "game_poss_num", "home_score",
                "away_score", "down", "distance", "play_yards", "poss_yards",
                "yards_to_end_zone"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["is_touchdown", "is_field_goal", "is_turnover", "is_punt", "is_sack",
                "is_extra_point", "is_two_point_conversion"]:
        df[col] = df[col].astype(str).str.lower().isin(["true", "1"])

    rows = []
    for gid, g in df.groupby("game_id", sort=False):
        rows.extend(process_game_drives(gid, g, ctx, season))
    out = pd.DataFrame(rows)
    out_path = DATA_DIR / f"drive_log_{season}.csv"
    out.to_csv(out_path, index=False)
    print(f"[{season}] {len(out):,} drive-rows -> {out_path}")
    return out


def load_to_postgres(df: pd.DataFrame, season: int) -> bool:
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
        dff = df.astype(object).where(pd.notnull(df), None)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM drive_log WHERE season = :s"), {"s": int(season)})
            dff.to_sql("drive_log", conn, if_exists="append", index=False,
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
