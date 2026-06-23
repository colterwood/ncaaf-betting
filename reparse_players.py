"""
Re-derive parsed columns (player names + scoring flags) in existing per-game CSVs
without re-scraping the ESPN/cfbd APIs.

Player columns: parse_players is a universal, format-detecting parser; rewrites
offensive_player/quarterback/kicker from play_desc + play_type.

Scoring flags: is_touchdown / is_extra_point / is_two_point_conversion are
re-derived from play_desc (fixes box-score TD formats like "12 Yd Run" that the
original detector missed). scoring_play is redefined as a robust flag union
(TD | made-FG | XP | 2pt | safety) instead of the original score-column delta,
which was unreliable because ESPN interleaves box-score summary rows with
non-monotonic scores. is_field_goal / is_turnover / is_punt / is_sack are left
as scraped (already correct).

Usage:
    python reparse_players.py            # re-derive all per-game CSVs in data/games/
    python reparse_players.py --dry-run  # report what would change, write nothing
"""

import argparse
import re
from pathlib import Path

import pandas as pd

from espn_cfb_pbp_scraper import (
    parse_players, abbreviate_name,
    detect_touchdown, detect_extra_point, detect_two_point_conversion,
)

GAMES_DIR = Path("data") / "games"

_FG_MISS_RE = re.compile(r"missed|blocked|no good", re.I)
_SAFETY_RE = re.compile(r"\bsafety\b", re.I)


def _as_bool(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1"])


def reparse_file(path: Path, dry_run: bool = False) -> int:
    """Re-derive player + scoring-flag columns for one per-game CSV.
    Returns the number of rows whose tracked columns changed."""
    df = pd.read_csv(path, dtype={"game_id": str}, keep_default_na=False)

    new_off, new_qb, new_kick, new_td, new_xp, new_2pt = [], [], [], [], [], []
    for play_type, desc in zip(df["play_type"], df["play_desc"]):
        pt, d = str(play_type), str(desc)
        off, qb, kick = parse_players(pt, d)
        new_off.append(abbreviate_name(off))
        new_qb.append(abbreviate_name(qb))
        new_kick.append(abbreviate_name(kick))
        td = detect_touchdown(d)
        new_td.append(td)
        new_xp.append(detect_extra_point(pt, d, td))
        new_2pt.append(detect_two_point_conversion(pt, d))

    # scoring_play = robust flag union (TD | made FG | XP | 2pt | safety)
    fg_existing = _as_bool(df["is_field_goal"])
    made_fg = fg_existing & ~df["play_desc"].str.contains(_FG_MISS_RE, na=False)
    safety = df["play_desc"].str.contains(_SAFETY_RE, na=False)
    new_scoring = [
        bool(td or mf or xp or tp or sf)
        for td, mf, xp, tp, sf in zip(new_td, made_fg, new_xp, new_2pt, safety)
    ]

    # Count changes across all tracked columns
    old_off = df["offensive_player"].tolist()
    old_qb = df["quarterback"].tolist()
    old_kick = df["kicker"].tolist()
    old_td = _as_bool(df["is_touchdown"]).tolist()
    old_scoring = _as_bool(df["scoring_play"]).tolist()
    changed = sum(
        1 for i in range(len(df))
        if (new_off[i] != old_off[i] or new_qb[i] != old_qb[i]
            or new_kick[i] != old_kick[i] or new_td[i] != old_td[i]
            or new_scoring[i] != old_scoring[i])
    )

    if not dry_run:
        df["offensive_player"] = new_off
        df["quarterback"] = new_qb
        df["kicker"] = new_kick
        df["is_touchdown"] = new_td
        df["is_extra_point"] = new_xp
        df["is_two_point_conversion"] = new_2pt
        df["scoring_play"] = new_scoring
        df.to_csv(path, index=False)

    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-parse player columns in per-game CSVs")
    ap.add_argument("--dry-run", action="store_true", help="Report only; write nothing")
    args = ap.parse_args()

    files = sorted(GAMES_DIR.glob("pbp_*.csv"))
    print(f"Found {len(files)} per-game CSVs.")

    total_changed = 0
    files_touched = 0
    for i, path in enumerate(files, 1):
        try:
            changed = reparse_file(path, dry_run=args.dry_run)
        except Exception as e:
            print(f"  {path.name}: ERROR {e}")
            continue
        if changed:
            files_touched += 1
            total_changed += changed
        if i % 200 == 0:
            print(f"  ...{i}/{len(files)} processed")

    verb = "would change" if args.dry_run else "changed"
    print(f"\nDone. {verb} {total_changed:,} rows across {files_touched} files.")


if __name__ == "__main__":
    main()
