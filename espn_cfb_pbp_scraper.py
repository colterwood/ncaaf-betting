"""
ESPN College Football Play-by-Play Scraper
Uses ESPN's JSON API — no Selenium required.

Usage:
    python espn_cfb_pbp_scraper.py                  # sample game 401756846
    python espn_cfb_pbp_scraper.py 401756846        # explicit game ID
"""

import re
import sys
import time

import pandas as pd
import requests

ESPN_PBP_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CFB-PBP-Scraper/1.0)"}

# Raised when a game was canceled, postponed, or otherwise never played
class GameNotPlayedError(Exception):
    pass


# Raised when a game has not finished yet (scheduled or in progress). Distinct
# from "no data" so a live-season updater retries it instead of skipping it.
class GameNotFinalError(Exception):
    pass


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

# ESPN intermittently returns these under sustained load; backed-off retries
# clear nearly all of them. Other statuses (e.g. 404) are not transient.
_RETRY_STATUS = {500, 502, 503, 504}


def fetch_game_data(game_id: str, *, retries: int = 4, backoff: float = 2.0) -> dict:
    """GET the ESPN summary JSON, retrying transient 5xx / network errors with
    exponential backoff (2, 4, 8, 16s). Non-transient errors fail fast."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(ESPN_PBP_URL, params={"event": game_id},
                                headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None and status not in _RETRY_STATUS:
                raise  # non-transient HTTP error (e.g. 404) — fail fast
            last_exc = e
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
    raise last_exc  # exhausted retries


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def clock_to_seconds(clock_str: str) -> int:
    try:
        m, s = clock_str.split(":")
        return int(m) * 60 + int(s)
    except Exception:
        return 0


def secs_remaining_regulation(period: int, secs_left_quarter: int) -> int:
    if period <= 4:
        return (4 - period) * 900 + secs_left_quarter
    return 0  # OT


# ---------------------------------------------------------------------------
# Play type mapping
# ---------------------------------------------------------------------------

def map_play_type(espn_type: str, desc: str = "") -> str:
    t = espn_type.lower()
    d = desc.lower()
    if "kickoff" in t:
        return "kickoff"
    if "rush" in t or "run" in t:
        return "run"
    if any(x in t for x in ["reception", "incompletion", "interception"]):
        return "pass"
    if "pass" in t:
        return "pass"
    if "sack" in t:
        return "pass"
    if "punt" in t:
        return "punt"
    if "field goal" in t:
        return "field goal"
    if "extra point" in t or "pat" in t:
        return "extra point"
    if "two point" in t or "two-point" in t:
        return "two-point conversion"
    if "penalty" in t:
        return "penalty"
    if "fumble" in t:
        # Derive underlying play type from description
        if re.search(r"\bkickoff\b", d):
            return "kickoff"
        if re.search(r"\bpunt\b", d):
            return "punt"
        if re.search(r"\bsacked\b", d):
            return "pass"  # sack that resulted in a fumble
        if re.search(r"\bpass\b|\breception\b", d):
            return "pass"
        if re.search(r"\brun\b|\brush\b", d):
            return "run"
        return "fumble"  # can't determine
    return t or "unknown"


# ---------------------------------------------------------------------------
# Player name extraction
# ---------------------------------------------------------------------------

def abbreviate_name(name: str) -> str:
    """Normalize any name form to 'F. Last'.

      'Rocco Becht'   → 'R. Becht'   (First Last)
      'T.Green'       → 'T. Green'   (gamebook F.Last, no space)
      'R. Becht'      → 'R. Becht'   (already abbreviated)
      'O.Mabson II'   → 'O. Mabson II'
    Passes through 'N/A' and 'No Offensive Player'.
    """
    if not name or name in ("No Offensive Player", "N/A"):
        return name
    name = name.strip()
    # Gamebook "Last,First" → "First Last"  ('Green,Taylen' → 'Taylen Green')
    if "," in name:
        last, _, first = name.partition(",")
        if first.strip() and last.strip():
            name = f"{first.strip()} {last.strip()}"
    # Already in initial form: 'F.Last', 'F. Last', 'A.J. Henning'
    m = re.match(r"^([A-Za-z])\.\s*(.+)$", name)
    if m:
        return f"{m.group(1)}. {m.group(2).strip()}"
    parts = name.split()
    if len(parts) == 1:
        return name  # only one token, nothing to abbreviate
    first = parts[0]
    if len(first) == 1:
        return name
    return f"{first[0]}. {' '.join(parts[1:])}"


NO_OFF, NA = "No Offensive Player", "N/A"

# Receiver pattern shared by completions and two-point pass conversions.
# Handles ESPN formats:
#   "QB pass complete to RECEIVER for N yds ..."   (regular completion)
#   "QB pass to RECEIVER for N yds for a TD ..."   (scoring summary)
#   "QB pass complete to RECEIVER"                 (no trailing yardage)
_PASS_COMPLETE_RE = re.compile(
    r"^(.+?)\s+pass\s+(?:complete\s+)?to\s+(.+?)(?:\s+for\b|,|\s*$)", re.I
)

# Gamebook format detection:
#   - jersey-numbered token "#11 J.Royer"
#   - a leading formation (optionally after a "(MM:SS)" timestamp)
#   - gamebook-only phrases "caught at" / "thrown to" / "field goal attempt from"
_FORMATIONS = (
    r"No Huddle-Shotgun|No Huddle|Shotgun|Pistol|Wildcat|Empty|Jumbo|"
    r"Goal Line|Singleback|I-Formation|I-Form|Ace"
)
# A leading "(MM:SS)" timestamp is itself a reliable gamebook signal — standard
# ESPN and cfbd prose never start with one.
_FORMATION_RE = re.compile(rf"^\(\d+:\d+\)|^(?:{_FORMATIONS})\b", re.I)
_GAMEBOOK_MARKER_RE = re.compile(
    r"#\d+\s+[A-Za-z]|\bcaught at\b|\bthrown to\b|field goal attempt from", re.I
)

# Trailing clauses that concatenate a second play/commentary onto the first;
# everything from these markers on is ignored for player extraction.
_NOISE_MARKERS = (
    "The previous play is under",
    "CALL OVERTURNED",
    "(Original Play:",
    " PENALTY ",
    ", PENALTY",
)

# Jersey-optional player token: "#11 J.Royer", "J.Royer", or "Green,Taylen"
_PLAYER = r"(?:#\d+\s+)?(.+?)"


def _is_gamebook(d: str) -> bool:
    return bool(_FORMATION_RE.search(d) or _GAMEBOOK_MARKER_RE.search(d))


def _clean_gamebook(d: str) -> str:
    """Strip leading timestamp + formation and truncate trailing review/penalty
    noise, leaving the core '[#NN ]NAME action ...' text."""
    d = re.sub(r"^\(\d+:\d+\)\s*", "", d)                       # timestamp
    d = re.sub(rf"^(?:{_FORMATIONS})\s+", "", d, flags=re.I)    # formation
    for marker in _NOISE_MARKERS:
        idx = d.find(marker)
        if idx > 0:
            d = d[:idx]
    return d.strip()


def _g(pattern: str, d: str) -> str | None:
    """Helper: return first capture group of pattern in d, or None."""
    m = re.search(pattern, d, re.I)
    return m.group(1).strip() if m else None


def _parse_gamebook(pt: str, raw: str) -> tuple[str, str, str]:
    """Extract players from the NFL-gamebook format (jersey numbers optional):
       'Shotgun #2 B.Sorsby pass complete short left to #11 J.Royer caught at ...'
       'No Huddle-Shotgun Green,Taylen pass complete deep middle to Blake,Xavier caught ...'
    The primary player precedes the action verb; the receiver follows 'to' and
    precedes 'caught'/'thrown'/'for'.
    """
    d = _clean_gamebook(raw)

    # Sack — QB is both passer and ball carrier
    if re.search(r"\bsacked\b", d, re.I):
        qb = _g(rf"^{_PLAYER}\s+sacked\b", d) or NA
        return qb, qb, NA

    if pt == "kickoff":
        return NO_OFF, NA, (_g(rf"^{_PLAYER}\s+(?:onside\s+)?kickoff\b", d) or NA)
    if pt == "punt":
        return NO_OFF, NA, (_g(rf"^{_PLAYER}\s+punt\b", d) or NA)
    if pt == "field goal":
        return NO_OFF, NA, (_g(rf"^{_PLAYER}\s+field goal\b", d) or NA)

    if pt == "run":
        return (_g(rf"^{_PLAYER}\s+(?:rush|run)\b", d) or NO_OFF), NA, NA

    if pt in ("pass", "two-point conversion"):
        qb = _g(rf"^{_PLAYER}\s+pass\b", d) or NA
        if re.search(r"\bintercepted\b", d, re.I):
            return NO_OFF, qb, NA
        # Receiver: "... to [#NN ]RECEIVER (caught|thrown|for) ..."
        rec = _g(rf"\bto\s+{_PLAYER}\s+(?:caught|thrown|for)\b", d)
        if rec:
            return rec, qb, NA
        # Gamebook run inside a 2pt conversion (no "pass")
        if pt == "two-point conversion" and not re.search(r"\bpass\b", d, re.I):
            return (_g(rf"^{_PLAYER}\s+(?:rush|run)\b", d) or NO_OFF), NA, NA
        return NO_OFF, qb, NA

    return NO_OFF, NA, NA


# A valid player name never contains digits, jersey marks, parens, or fragments
# of an adjacent play. Captures hitting these are malformed (usually concatenated
# multi-play descriptions) and are rejected to a clean fallback.
_BAD_NAME_RE = re.compile(
    r"\d|#|\(|\)|\bfumbled\b|recovered by|Shotgun|Huddle|steps back|"
    r"caught at|thrown to|under review|intercepted by|\bpenalty\b|blocked by",
    re.I,
)


def _sanitize(name: str, fallback: str) -> str:
    if not name or name in (NO_OFF, NA):
        return name
    n = name.strip()
    if len(n) > 30 or _BAD_NAME_RE.search(n):
        return fallback
    return n


def parse_players(play_type: str, desc: str) -> tuple[str, str, str]:
    """Extract (offensive_player, quarterback, kicker) from a play description.

    Description-driven (works at scrape time and when re-parsing CSVs). Dispatches
    by detected format: gamebook → box-score scoring → standard/cfbd-verbose.
    A final sanitizer rejects malformed captures from corrupted source rows.
    """
    off, qb, kicker = _extract_players((play_type or "").lower(), desc or "")
    return _sanitize(off, NO_OFF), _sanitize(qb, NA), _sanitize(kicker, NA)


def _extract_players(pt: str, d: str) -> tuple[str, str, str]:

    # --- Format 1: NFL gamebook (jersey numbers and/or formation prefix) ---
    if _is_gamebook(d):
        return _parse_gamebook(pt, d)

    # --- Format 2: box-score scoring "RECEIVER N Yd pass from QB (Kicker Kick)" ---
    if pt == "pass":
        m = re.match(r"^(.+?)\s+\d+\s+Yd\s+pass\s+from\s+(.+?)(?:\s+\(.*\))?\s*$", d, re.I)
        if m:
            return m.group(1).strip(), m.group(2).strip(), NA

    # --- Format 3: standard / cfbd-verbose prose ---
    # Sacks (mapped to play_type 'pass') — QB is both passer and "ball carrier".
    # Checked first so sack+fumble plays are handled before the pass branches.
    # ESPN: "QB sacked by ..."   cfbd: "QB steps back to pass. Sacked at ..."
    if re.search(r"\bsacked\b", d, re.I):
        m = re.match(r"^(.+?)\s+(?:sacked|steps back)\b", d, re.I)
        qb = m.group(1).strip() if m else NA
        return qb, qb, NA

    if pt == "kickoff":
        # ESPN "X kickoff" / cfbd "X kicks N yards" / "X onside kickoff"
        m = re.match(r"^(.+?)\s+(?:onside\s+)?(?:kickoff|kicks)\b", d, re.I)
        return NO_OFF, NA, (m.group(1).strip() if m else NA)

    if pt == "punt":
        # ESPN "X punt" / cfbd "X punts"
        m = re.match(r"^(.+?)\s+punts?\b", d, re.I)
        return NO_OFF, NA, (m.group(1).strip() if m else NA)

    if pt == "field goal":
        m = re.match(r"^(.+?)\s+\d+\s*(?:yd|yard)", d, re.I) \
            or re.match(r"^(.+?)\s+field goal\b", d, re.I)
        return NO_OFF, NA, (m.group(1).strip() if m else NA)

    if pt == "extra point":
        m = re.match(r"^(.+?)\s+extra point", d, re.I)
        return NO_OFF, NA, (m.group(1).strip() if m else NA)

    if pt == "pass":
        # QB precedes "pass" or the cfbd phrasing "steps back to pass"
        m_qb = re.match(r"^(.+?)\s+(?:pass\b|steps back)", d, re.I)
        qb = m_qb.group(1).strip() if m_qb else NA

        # Verbose "Catch made by RECEIVER" format (some games use this style,
        # where "complete to" is followed by a yard line, not the receiver)
        m_catch = re.search(
            r"(?:catch made by|caught by)\s+(.+?)\s+(?:at\b|for\b|\.|,|$)", d, re.I
        )
        if m_catch:
            return m_catch.group(1).strip(), qb, NA

        # Interception — only the passer (QB) is recorded
        if re.search(r"\bintercepted\b", d, re.I):
            return NO_OFF, qb, NA

        # Incompletion — capture intended target if present
        if re.search(r"\bincomplete\b", d, re.I):
            m = re.search(
                r"incomplete\s+to\s+(.+?)(?:,|\s+broken up|\s+at\b|\s*$)", d, re.I
            )
            if not m:  # cfbd "Pass incomplete intended for RECEIVER" (F.Last form)
                # allow internal '.' only when followed by a letter (keeps "K.Wetjen",
                # drops the sentence-ending period)
                m = re.search(
                    r"intended for\s+([A-Za-z](?:[\w'\-]|\.(?=[A-Za-z]))*(?:\s+(?:Jr\.|Sr\.|II|III|IV))?)",
                    d, re.I,
                )
            if m:
                return m.group(1).strip(), qb, NA
            return NO_OFF, qb, NA

        # Standard "complete to RECEIVER" / scoring-summary "pass to RECEIVER"
        m = _PASS_COMPLETE_RE.match(d)
        if m:
            return m.group(2).strip(), m.group(1).strip(), NA

        # Receiver-only fallback for source rows missing the passer name
        # (e.g. "pass complete to Jace Henry for 8 yds"). Reject empty/numeric
        # captures like "pass complete to for 39 yds".
        m = re.search(r"pass\s+(?:complete\s+)?to\s+(.+?)(?:\s+for\b|,|\s*$)", d, re.I)
        if m:
            rec = m.group(1).strip()
            if rec and not rec[0].isdigit():
                return rec, qb, NA

        # Fallback — at least keep the passer
        return NO_OFF, qb, NA

    if pt == "run":
        # ESPN "X run/rush" / cfbd "X rushed" / cfbd "X scrambles"
        m = re.match(r"^(.+?)\s+(?:runs?|rush(?:ed|es)?|scrambles?)\b", d, re.I)
        if m:
            return m.group(1).strip(), NA, NA
        # QB kneel: "J. Arnold takes a knee" (but not "Kneel down by TEAM")
        m = re.match(r"^(.+?)\s+takes a knee", d, re.I)
        return (m.group(1).strip() if m else NO_OFF), NA, NA

    if pt == "two-point conversion":
        if re.search(r"\bpass\b", d, re.I):
            m = _PASS_COMPLETE_RE.match(d)
            if m:
                return m.group(2).strip(), m.group(1).strip(), NA
        m = re.match(r"^(.+?)\s+(?:run|rush)\b", d, re.I)
        return (m.group(1).strip() if m else NO_OFF), NA, NA

    return NO_OFF, NA, NA


# ---------------------------------------------------------------------------
# Boolean flag detection
# ---------------------------------------------------------------------------

# Box-score scoring-summary touchdown formats, e.g. "12 Yd Run",
# "22 Yd pass from", "23 Yd Interception Return", "34 Yd Return of Blocked Punt".
# These lack the words "TD"/"touchdown" so the text checks below miss them.
# (Deliberately excludes "N Yd Field Goal", which is not a touchdown.)
_BOX_TD_RE = re.compile(
    r"\d+\s+yd\s+(?:"
    r"run|pass(?:\s+from)?|reception|"
    r"(?:interception|fumble|punt|kickoff|blocked\s+\w+)\s+return|"
    r"return(?:\s+of)?"
    r")\b",
    re.I,
)


def detect_touchdown(desc: str) -> bool:
    d = desc.lower()
    if "for a td" in d or "touchdown" in d:
        return True
    return bool(_BOX_TD_RE.search(desc))


def detect_extra_point(espn_type: str, desc: str, is_td: bool) -> bool:
    """
    TRUE only if the extra point kick was successful.
    XP may appear as a standalone play or embedded in the TD play text as "(Name KICK)".
    """
    t, d = espn_type.lower(), desc.lower()
    failed = re.search(r"\([^)]*(?:failed|no good)[^)]*\)", d) is not None

    if "extra point" in t or "pat" in t:
        return not failed

    # Embedded in TD description: "(K. Konrardy KICK)" — upper/lower case KICK
    if is_td and re.search(r"\([^)]*\bkick\b[^)]*\)", d):
        return not failed

    return False


def detect_field_goal(espn_type: str, desc: str) -> bool:
    return "field goal" in espn_type.lower() or "field goal" in desc.lower()


def detect_two_point_conversion(espn_type: str, desc: str) -> bool:
    """TRUE only if the two-point conversion was successful."""
    t, d = espn_type.lower(), desc.lower()
    is_two_pt = (
        "two point" in t or "two-point" in t
        or "two point" in d or "two-point" in d
        or bool(re.search(r"\btwopt\b|\b2pt\b", d))
        # Embedded in TD: "(Name 2pt rush)" or similar
        or bool(re.search(r"\([^)]*\b2pt\b[^)]*\)", d))
    )
    if not is_two_pt:
        return False
    failed = "failed" in d or "no good" in d
    return not failed


def detect_turnover(espn_type: str, desc: str, api_flag: bool) -> bool:
    if api_flag:
        return True
    d = desc.lower()
    return (
        bool(re.search(r"\bintercepted\b", d))
        # "fumbled...recovered by" covers both plain fumbles and sack+fumble
        or bool(re.search(r"\bfumbled?\b.*\brecovered by\b", d))
        or "turnover on downs" in d
    )


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_game(game_id: str) -> pd.DataFrame:
    data = fetch_game_data(game_id)

    # Check game status before parsing
    comp0 = data.get("header", {}).get("competitions", [{}])[0]
    status = (comp0.get("status") or {}).get("type", {}).get("name", "")
    if status in ("STATUS_CANCELED", "STATUS_POSTPONED"):
        raise GameNotPlayedError(f"game {game_id} status: {status}")
    # Scheduled or in-progress games aren't ready yet — retry on a later run
    # rather than recording them as having no play-by-play data.
    if status and status != "STATUS_FINAL":
        raise GameNotFinalError(f"game {game_id} status: {status}")

    # --- Teams ---
    competitions = data["header"]["competitions"][0]
    home_id = away_id = home_name = away_name = None
    for comp in competitions["competitors"]:
        team = comp["team"]
        full_name = f"{team['location']} {team['name']}"
        if comp["homeAway"] == "home":
            home_id, home_name = str(team["id"]), full_name
        else:
            away_id, away_name = str(team["id"]), full_name

    drives = (data.get("drives") or {}).get("previous", [])

    rows = []
    game_poss_num = 0
    home_poss_count = 0
    away_poss_count = 0
    prev_home_score = 0
    prev_away_score = 0

    for drive in drives:
        plays = drive.get("plays", [])
        if not plays:
            continue

        drive_team_id = str((drive.get("team") or {}).get("id", ""))
        if drive_team_id == home_id:
            drive_team = home_name
        elif drive_team_id == away_id:
            drive_team = away_name
        else:
            drive_team = None

        # ESPN assigns kickoff drives to the *receiving* team, so drive_team is
        # already the correct offensive team for both kickoffs and regular plays.
        # ESPN may bundle a kickoff + the receiving team's possession into one
        # drive, so defer the possession counter increment until the first
        # non-kickoff play appears in such drives.
        first_type = (plays[0].get("type") or {}).get("text", "").lower()
        starts_with_kickoff = "kickoff" in first_type
        poss_incremented = False

        if not starts_with_kickoff and drive_team:
            game_poss_num += 1
            if drive_team == home_name:
                home_poss_count += 1
            else:
                away_poss_count += 1
            poss_incremented = True

        play_seq = 0        # resets each possession
        poss_yards_cum = 0  # cumulative yards within current possession

        for play in plays:
            espn_type = (play.get("type") or {}).get("text", "")
            desc = play.get("text", "")
            period = (play.get("period") or {}).get("number", 1)
            clock_str = (play.get("clock") or {}).get("displayValue", "0:00")
            home_score = play.get("homeScore", prev_home_score)
            away_score = play.get("awayScore", prev_away_score)
            api_turnover = play.get("isTurnover", False)
            play_yards = play.get("statYardage") or 0
            start_obj = play.get("start") or {}
            yards_to_end_zone = start_obj.get("yardsToEndzone")
            down = start_obj.get("down")
            distance = start_obj.get("distance")

            secs_q = clock_to_seconds(clock_str)
            secs_reg = secs_remaining_regulation(period, secs_q)

            is_kickoff_play = "kickoff" in espn_type.lower()
            off_team = drive_team  # ESPN assigns drives to the offensive/receiving team

            play_type = map_play_type(espn_type, desc)
            td = detect_touchdown(desc)
            fg = detect_field_goal(espn_type, desc)
            xp = detect_extra_point(espn_type, desc, td)
            two_pt = detect_two_point_conversion(espn_type, desc)
            turnover = detect_turnover(espn_type, desc, api_turnover)
            sack = "sack" in espn_type.lower() or bool(re.search(r"\bsacked\b", desc, re.I))
            punt = "punt" in espn_type.lower()
            # scoring_play = robust flag union (TD | made FG | XP | 2pt | safety).
            # NOT a score-column delta — ESPN interleaves box-score summary rows
            # with non-monotonic scores, which makes deltas unreliable.
            made_fg = fg and not re.search(r"missed|blocked|no good", desc, re.I)
            safety = bool(re.search(r"\bsafety\b", desc, re.I))
            scoring = bool(td or made_fg or xp or two_pt or safety)

            off_player, qb, kicker = parse_players(play_type, desc)
            off_player = abbreviate_name(off_player)
            qb = abbreviate_name(qb)
            kicker = abbreviate_name(kicker)

            if is_kickoff_play:
                poss_play_num = 0
                g_poss = h_poss = a_poss = 0
                poss_yards = 0
            else:
                # First non-kickoff play in a kickoff-starting drive: increment now
                if starts_with_kickoff and not poss_incremented and drive_team:
                    game_poss_num += 1
                    if drive_team == home_name:
                        home_poss_count += 1
                    else:
                        away_poss_count += 1
                    poss_incremented = True
                    play_seq = 0        # possession starts here
                    poss_yards_cum = 0  # reset cumulative yards

                play_seq += 1
                poss_play_num = play_seq
                g_poss = game_poss_num
                h_poss = home_poss_count
                a_poss = away_poss_count
                poss_yards_cum += play_yards
                poss_yards = poss_yards_cum

            rows.append({
                "game_id": game_id,
                "home_team": home_name,
                "away_team": away_name,
                "play_desc": desc,
                "home_score": home_score,
                "away_score": away_score,
                "quarter": period,
                "secs_left_quarter": secs_q,
                "secs_left_reg": secs_reg,
                "offensive_team": off_team,
                "play_type": play_type,
                "scoring_play": scoring,
                "is_touchdown": td,
                "is_field_goal": fg,
                "is_turnover": turnover,
                "is_punt": punt,
                "is_sack": sack,
                "is_extra_point": xp,
                "is_two_point_conversion": two_pt,
                "offensive_player": off_player,
                "quarterback": qb,
                "kicker": kicker,
                "poss_play_num": poss_play_num,
                "game_poss_num": g_poss,
                "home_team_poss_num": h_poss,
                "away_team_poss_num": a_poss,
                "yards_to_end_zone": yards_to_end_zone,
                "down": down,
                "distance": distance,
                "play_yards": play_yards,
                "poss_yards": poss_yards,
                "data_source": "espn",
                "game_url": f"https://www.espn.com/college-football/playbyplay/_/gameId/{game_id}",
            })

            prev_home_score = home_score
            prev_away_score = away_score

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    game_id = sys.argv[1] if len(sys.argv) > 1 else "401756846"
    print(f"Fetching play-by-play for game {game_id}...")
    df = process_game(game_id)

    out = f"pbp_{game_id}.csv"
    df.to_csv(out, index=False)
    print(f"Saved {len(df)} plays to {out}\n")

    preview_cols = [
        "quarter", "secs_left_quarter", "offensive_team", "play_type",
        "play_desc", "home_score", "away_score",
    ]
    print("--- First 15 plays ---")
    print(df[preview_cols].head(15).to_string(index=False))

    print("\n--- Scoring plays ---")
    scoring = df[df["scoring_play"]].copy()
    print(scoring[["quarter", "offensive_team", "play_type", "play_desc",
                    "home_score", "away_score", "is_touchdown",
                    "is_field_goal", "is_extra_point"]].to_string(index=False))

    print("\n--- Possession summary (first 20 non-kickoff plays) ---")
    non_ko = df[df["play_type"] != "kickoff"].head(20)
    print(non_ko[["game_poss_num", "home_team_poss_num", "away_team_poss_num",
                   "poss_play_num", "offensive_team", "play_type", "play_desc"]].to_string(index=False))
