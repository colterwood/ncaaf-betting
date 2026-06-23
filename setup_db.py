"""
One-time (idempotent) database setup for the betting feature store.

Creates the quarter_log table + indexes in the Supabase Postgres project pointed
to by SUPABASE_DB_URL (.env). Run this once after creating a new project; safe to
re-run (CREATE TABLE / INDEX IF NOT EXISTS). Uses a direct Postgres connection so
it does not depend on the Supabase MCP (which is interactive-session only).

Run:
    python setup_db.py
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DDL = """
create table if not exists quarter_log (
  season integer not null,
  game_id text not null,
  date text,
  team text not null,
  opponent text,
  is_home boolean,
  quarter text not null,
  team_coach text,
  team_wins_entering integer,
  team_losses_entering integer,
  opp_coach text,
  opp_wins_entering integer,
  opp_losses_entering integer,
  team_coach_wins_entering integer,
  team_coach_losses_entering integer,
  opp_coach_wins_entering integer,
  opp_coach_losses_entering integer,
  margin_entering integer,
  drives_for integer,
  td_drives_for integer,
  fg_drives_for integer,
  turnovers_for integer,
  points_for integer,
  avg_start_pos_for real,
  avg_yards_for real,
  avg_secs_per_drive_for real,
  plays_per_drive_for real,
  pass_plays_per_drive_for real,
  rush_plays_per_drive_for real,
  plays_for integer,
  three_and_outs_for integer,
  redzone_trips_for integer,
  redzone_tds_for integer,
  explosive_plays_for integer,
  sacks_allowed_for integer,
  punts_for integer,
  total_top_for integer,
  drives_against integer,
  td_drives_against integer,
  fg_drives_against integer,
  takeaways integer,
  points_against integer,
  avg_start_pos_against real,
  avg_yards_against real,
  avg_secs_per_drive_against real,
  plays_per_drive_against real,
  pass_plays_per_drive_against real,
  rush_plays_per_drive_against real,
  plays_against integer,
  three_and_outs_against integer,
  redzone_trips_against integer,
  redzone_tds_against integer,
  explosive_plays_against integer,
  sacks_made integer,
  punts_against integer,
  total_top_against integer,
  primary key (season, game_id, team, quarter)
);
create index if not exists idx_quarter_log_team_season on quarter_log (team, season);
create index if not exists idx_quarter_log_quarter on quarter_log (quarter);
create index if not exists idx_quarter_log_date on quarter_log (date);
alter table quarter_log add column if not exists home_away text;

create table if not exists drive_log (
  season integer not null,
  game_id text not null,
  date text,
  team text not null,
  opponent text,
  is_home boolean,
  home_away text,
  quarterback text,
  personnel integer,
  team_coach text,
  team_wins_entering integer,
  team_losses_entering integer,
  opp_coach text,
  opp_wins_entering integer,
  opp_losses_entering integer,
  drive_num integer not null,
  drive_num_quarter integer,
  drive_num_half integer,
  quarter text,
  half text,
  margin_entering integer,
  start_position integer,
  end_position integer,
  drive_result text,
  total_yards integer,
  num_plays integer,
  num_pass_plays integer,
  num_run_plays integer,
  third_downs integer,
  third_down_conversions integer,
  yards_per_pass real,
  yards_per_run real,
  total_secs integer,
  points_scored integer,
  is_scoring_drive boolean,
  reached_redzone boolean,
  explosive_plays integer,
  sacks integer,
  penalties integer,
  primary key (season, game_id, drive_num)
);
create index if not exists idx_drive_log_team_season on drive_log (team, season);
create index if not exists idx_drive_log_result on drive_log (drive_result);
create index if not exists idx_drive_log_date on drive_log (date);
alter table drive_log add column if not exists quarterback text;
alter table drive_log add column if not exists personnel integer;
"""


def engine_from_env():
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        raise SystemExit("SUPABASE_DB_URL not set in .env")
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix):]
            break
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 20})


def main() -> None:
    eng = engine_from_env()
    with eng.begin() as conn:
        for stmt in filter(str.strip, DDL.split(";")):
            conn.execute(text(stmt))
        n = conn.execute(text(
            "select count(*) from information_schema.columns "
            "where table_name = 'quarter_log'"
        )).scalar()
    eng.dispose()
    print(f"quarter_log ready ({n} columns).")


if __name__ == "__main__":
    main()
