#!/usr/bin/env python3
"""Synkroniserer utslagskamper med FIFA Calendar som primærkilde.

Prinsipp:
- FIFA Calendar er primærkilde for kampoppsett, match_no, fifa_event_id, runde,
  lag, avspark, kampstatus og resultat når FIFA leverer dette.
- football-data.org brukes som kontrollkilde og som resultatfallback når FIFA ikke
  har ferdig/resultat ennå.
- status.json er den eneste sannheten de andre scriptfilene skal lese.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
STATUS_PATH = DATA_DIR / "status.json"
API_KONTROLL_PATH = DATA_DIR / "api-kontroll.json"

VALID_ROUNDS = {"r32", "r16", "qf", "sf", "final"}
ROUND_ORDER = {"r32": 1, "r16": 2, "qf": 3, "sf": 4, "final": 5}
FINISHED_WORDS = {"finished", "full-time", "full time", "ft", "completed", "result", "played"}
LIVE_WORDS = {"live", "in progress", "first half", "second half", "half-time", "halftime", "extra time", "penalties"}

# FIFA Calendar-URL kan overstyres i repo variables/secrets med FIFA_CALENDAR_URL.
# Flere URL-er kan skilles med ||.
DEFAULT_FIFA_URLS = [
    "https://api.fifa.com/api/v3/calendar/matches?" + urlencode({
        "from": "2026-06-28T00:00:00Z",
        "to": "2026-07-20T23:59:59Z",
        "language": "en",
        "count": "500",
        "idCompetition": "17",
    }),
    "https://api.fifa.com/api/v3/calendar/matches?" + urlencode({
        "from": "2026-06-28T00:00:00Z",
        "to": "2026-07-20T23:59:59Z",
        "language": "en",
        "count": "500",
    }),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    request_headers = {
        "User-Agent": "Nussebassene/1.0 (+https://rambergapps.github.io/Nussebassene/)",
        "Accept": "application/json,text/plain,*/*",
    }
    if headers:
        request_headers.update(headers)
    req = Request(url, headers=request_headers)
    with urlopen(req, timeout=timeout) as response:  # nosec - controlled URL/config
        return json.loads(response.read().decode("utf-8"))


def first_value(obj: dict[str, Any], names: list[str]) -> Any:
    lowered = {k.lower(): k for k in obj.keys()}
    for name in names:
        key = lowered.get(name.lower())
        if key is not None:
            return obj.get(key)
    return None


def text_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                locale = str(item.get("Locale") or item.get("locale") or "").lower()
                if locale.startswith("en"):
                    t = text_value(item.get("Description") or item.get("description") or item.get("Name") or item.get("name"))
                    if t:
                        return t
        for item in value:
            t = text_value(item)
            if t:
                return t
    if isinstance(value, dict):
        for key in [
            "Description", "description", "Name", "name", "TeamName", "teamName",
            "ShortClubName", "shortName", "DisplayName", "displayName", "Abbreviation",
        ]:
            t = text_value(value.get(key))
            if t:
                return t
    return None


def parse_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value)
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def looks_like_match(item: dict[str, Any]) -> bool:
    keys = {k.lower() for k in item.keys()}
    has_id = any(k in keys for k in ["idevent", "eventid", "id", "matchid", "matchnumber", "matchno"])
    has_team = any("home" in k for k in keys) and any("away" in k for k in keys)
    has_date = any(k in keys for k in ["date", "utcdate", "datetime", "matchdate", "localdate"])
    return has_id and (has_team or has_date)


def deep_find_match_lists(obj: Any) -> list[list[dict[str, Any]]]:
    lists: list[list[dict[str, Any]]] = []
    if isinstance(obj, list):
        dict_items = [x for x in obj if isinstance(x, dict)]
        if dict_items:
            score = sum(1 for x in dict_items if looks_like_match(x))
            if score >= max(1, len(dict_items) // 4):
                lists.append(dict_items)
        for item in obj:
            lists.extend(deep_find_match_lists(item))
    elif isinstance(obj, dict):
        for value in obj.values():
            lists.extend(deep_find_match_lists(value))
    return lists


def pick_match_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ["Results", "results", "Matches", "matches", "Items", "items", "data"]:
            value = payload.get(key)
            if isinstance(value, list) and any(isinstance(x, dict) for x in value):
                return [x for x in value if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    candidates = deep_find_match_lists(payload)
    return max(candidates, key=len) if candidates else []


def team_name(match: dict[str, Any], side: str) -> str | None:
    if side == "home":
        candidates = ["Home", "home", "HomeTeam", "homeTeam", "home_team", "HomeTeamName", "homeTeamName"]
    else:
        candidates = ["Away", "away", "AwayTeam", "awayTeam", "away_team", "AwayTeamName", "awayTeamName"]
    for key in candidates:
        if key in match:
            t = text_value(match[key])
            if t:
                return t
    return None


def parse_match_no(match: dict[str, Any]) -> int | None:
    value = first_value(match, ["MatchNumber", "matchNumber", "MatchNo", "matchNo", "match_no", "match_no_display"])
    if value is not None:
        return parse_int(value)
    for key in ["Properties", "properties", "MatchInfo", "matchInfo"]:
        nested = match.get(key)
        if isinstance(nested, dict):
            value = first_value(nested, ["MatchNumber", "matchNumber", "MatchNo", "matchNo"])
            if value is not None:
                return parse_int(value)
    return None


def parse_event_id(match: dict[str, Any]) -> str | None:
    value = first_value(match, ["IdEvent", "idEvent", "EventId", "eventId", "fifa_event_id", "FifaEventId", "id"])
    return str(value) if value is not None else None


def parse_date(match: dict[str, Any]) -> str | None:
    value = first_value(match, ["Date", "date", "UTCDate", "utcDate", "UtcDate", "DateUTC", "matchDate", "datetime", "LocalDate"])
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    if re.match(r"^\d{4}-\d{2}-\d{2}T", text) and not text.endswith("Z") and "+" not in text[-6:]:
        text += "Z"
    return text


def stage_text(match: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ["StageName", "stageName", "Stage", "stage", "Round", "round", "CompetitionStage", "competitionStage", "PhaseName"]:
        t = text_value(match.get(key))
        if t:
            parts.append(t)
    return " ".join(parts).lower()


def round_from_match(match_no: int | None, match: dict[str, Any]) -> str | None:
    stage = stage_text(match)
    if "round of 32" in stage or "last 32" in stage:
        return "r32"
    if "round of 16" in stage or "last 16" in stage:
        return "r16"
    if "quarter" in stage:
        return "qf"
    if "semi" in stage:
        return "sf"
    if "final" in stage and "third" not in stage and "3rd" not in stage:
        return "final"
    if match_no is None:
        return None
    if 73 <= match_no <= 88:
        return "r32"
    if 89 <= match_no <= 96:
        return "r16"
    if 97 <= match_no <= 100:
        return "qf"
    if 101 <= match_no <= 102:
        return "sf"
    if match_no == 104:
        return "final"
    return None


def known_team(name: str | None) -> bool:
    if not name:
        return False
    t = name.strip().lower()
    if t in {"tbd", "to be determined", "unknown", "winner", "runner-up"}:
        return False
    placeholder_words = ["winner group", "runner-up group", "best third", "winners of", "winner of", "loser of", "3rd"]
    return not any(word in t for word in placeholder_words)


def parse_fifa_status(match: dict[str, Any]) -> str | None:
    value = first_value(match, ["MatchStatus", "matchStatus", "Status", "status", "MatchStatusName", "statusName"])
    text = text_value(value)
    if text:
        return text
    return str(value) if value is not None else None


def fifa_status_is_finished(status: str | None) -> bool:
    if not status:
        return False
    s = status.strip().lower()
    return any(word == s or word in s for word in FINISHED_WORDS)


def fifa_status_is_live(status: str | None) -> bool:
    if not status:
        return False
    s = status.strip().lower()
    return any(word == s or word in s for word in LIVE_WORDS)


def parse_fifa_score(match: dict[str, Any], side: str) -> int | None:
    names = [
        f"{side}Score", f"{side.title()}Score", f"{side}_score",
        f"{side}TeamScore", f"{side.title()}TeamScore",
        f"{side}Goals", f"{side.title()}Goals",
    ]
    value = first_value(match, names)
    if value is not None:
        return parse_int(value)
    for key in ["Score", "score", "Result", "result", "MatchResult", "matchResult"]:
        node = match.get(key)
        if isinstance(node, dict):
            value = first_value(node, names + ["home" if side == "home" else "away", "homeTeam" if side == "home" else "awayTeam"])
            if value is not None:
                return parse_int(value)
    return None


def calc_tip_status(utc_date: str | None, home: str | None, away: str | None, finished: bool = False, live: bool = False) -> tuple[bool, str, bool]:
    if finished:
        return False, "ferdig", True
    if not known_team(home) or not known_team(away):
        return False, "mangler_lag", False
    if live:
        return False, "startet", True
    if not utc_date:
        return False, "mangler_tid", False
    try:
        kickoff = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
    except ValueError:
        return False, "mangler_tid", False
    started = datetime.now(timezone.utc) >= kickoff
    if started:
        return False, "startet", True
    return True, "åpen", False


def normalize_team(name: str | None) -> str:
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", "and")
    text = re.sub(r"\b(fc|cf|team|national|football|club)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def team_similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize_team(a), normalize_team(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.92
    return SequenceMatcher(None, na, nb).ratio()


def fetch_fifa() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    urls_env = os.getenv("FIFA_CALENDAR_URL", "").strip()
    urls = [u.strip() for u in urls_env.split("||") if u.strip()] if urls_env else DEFAULT_FIFA_URLS
    errors: list[str] = []
    for url in urls:
        try:
            payload = fetch_json(url)
            matches = pick_match_list(payload)
            if matches:
                return matches, {"ok": True, "url": url, "count": len(matches), "error": None}
            errors.append(f"{url}: fant ingen kampliste")
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            errors.append(f"{url}: {exc}")
    return [], {"ok": False, "url": urls[0] if urls else None, "count": 0, "error": " | ".join(errors)[:2000]}


def football_data_url() -> str:
    base = os.getenv("FOOTBALL_DATA_BASE_URL", "https://api.football-data.org/v4").rstrip("/")
    competition = os.getenv("FOOTBALL_DATA_COMPETITION", "WC")
    params = urlencode({"dateFrom": "2026-06-28", "dateTo": "2026-07-20"})
    return f"{base}/competitions/{competition}/matches?{params}"


def fetch_football_data() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    token = os.getenv("FOOTBALL_DATA_TOKEN", "").strip()
    if not token:
        return [], {"ok": False, "count": 0, "error": "FOOTBALL_DATA_TOKEN mangler"}
    url = football_data_url()
    try:
        payload = fetch_json(url, headers={"X-Auth-Token": token})
        matches = payload.get("matches", []) if isinstance(payload, dict) else []
        return matches, {"ok": True, "count": len(matches), "error": None, "url": url}
    except Exception as exc:  # noqa: BLE001 - logges i kontrollfil
        return [], {"ok": False, "count": 0, "error": str(exc), "url": url}



def football_data_scorers_url() -> str:
    base = os.getenv("FOOTBALL_DATA_BASE_URL", "https://api.football-data.org/v4").rstrip("/")
    competition = os.getenv("FOOTBALL_DATA_COMPETITION", "WC")
    # Toppscorerlisten er nødvendig for helhetsbonusen «FIFA Golden Boot».
    # Limit kan overstyres hvis API-planen trenger lavere/høyere verdi.
    limit = os.getenv("FOOTBALL_DATA_SCORERS_LIMIT", "100")
    params = urlencode({"season": "2026", "limit": limit})
    return f"{base}/competitions/{competition}/scorers?{params}"


def parse_fd_scorer(row: dict[str, Any]) -> dict[str, Any] | None:
    player = row.get("player") if isinstance(row.get("player"), dict) else {}
    team = row.get("team") if isinstance(row.get("team"), dict) else {}
    name = player.get("name") or player.get("shortName") or player.get("lastName")
    if not name:
        return None
    goals = parse_int(row.get("goals"))
    return {
        "player": str(name),
        "player_id": player.get("id"),
        "team": team.get("name") or team.get("shortName") or team.get("tla"),
        "team_id": team.get("id"),
        "goals": int(goals or 0),
        "assists": parse_int(row.get("assists")),
        "penalties": parse_int(row.get("penalties")),
        "source": "football-data.org",
        "source_role": "golden_boot_control_source",
    }


def fetch_football_data_scorers() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    token = os.getenv("FOOTBALL_DATA_TOKEN", "").strip()
    if not token:
        return [], {"ok": False, "count": 0, "error": "FOOTBALL_DATA_TOKEN mangler"}
    url = football_data_scorers_url()
    try:
        payload = fetch_json(url, headers={"X-Auth-Token": token})
        raw = payload.get("scorers", []) if isinstance(payload, dict) else []
        scorers = [x for x in (parse_fd_scorer(row) for row in raw if isinstance(row, dict)) if x]
        scorers.sort(key=lambda x: (-int(x.get("goals") or 0), str(x.get("player") or "").lower()))
        return scorers, {"ok": True, "count": len(scorers), "error": None, "url": url}
    except Exception as exc:  # noqa: BLE001 - logges i kontrollfil
        return [], {"ok": False, "count": 0, "error": str(exc), "url": url}

def fd_team(fd_match: dict[str, Any], side: str) -> str | None:
    obj = fd_match.get("homeTeam" if side == "home" else "awayTeam") or {}
    if isinstance(obj, dict):
        return obj.get("name") or obj.get("shortName") or obj.get("tla")
    return None


def score_pair(node: dict[str, Any]) -> tuple[int | None, int | None]:
    if not isinstance(node, dict):
        return None, None
    home = node.get("home", node.get("homeTeam"))
    away = node.get("away", node.get("awayTeam"))
    if home is None or away is None:
        return None, None
    return int(home), int(away)


def fd_score(fd_match: dict[str, Any]) -> dict[str, Any] | None:
    score = fd_match.get("score") or {}
    regular_home, regular_away = score_pair(score.get("regularTime") or {})
    full_home, full_away = score_pair(score.get("fullTime") or {})
    home = regular_home if regular_home is not None else full_home
    away = regular_away if regular_away is not None else full_away
    if home is None or away is None:
        return None
    return {
        "home_score": int(home),
        "away_score": int(away),
        "duration": score.get("duration"),
        "winner": score.get("winner"),
    }


def date_distance_seconds(a_iso: str | None, b_iso: str | None) -> float:
    if not a_iso or not b_iso:
        return 999999999.0
    try:
        a = datetime.fromisoformat(a_iso.replace("Z", "+00:00"))
        b = datetime.fromisoformat(b_iso.replace("Z", "+00:00"))
        return abs((a - b).total_seconds())
    except Exception:
        return 999999999.0


def match_fd(fifa_match: dict[str, Any], fd_matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    home = fifa_match.get("hjemme")
    away = fifa_match.get("borte")
    utc = fifa_match.get("utcDate")
    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    for fd in fd_matches:
        fd_home = fd_team(fd, "home")
        fd_away = fd_team(fd, "away")
        same_order = (team_similarity(home, fd_home) + team_similarity(away, fd_away)) / 2
        reverse_order = (team_similarity(home, fd_away) + team_similarity(away, fd_home)) / 2
        sim = max(same_order, reverse_order)
        seconds = date_distance_seconds(utc, fd.get("utcDate"))
        date_bonus = 1.0 if seconds <= 4 * 3600 else 0.85 if seconds <= 24 * 3600 else 0.45
        score = sim * date_bonus
        if score > best[0]:
            best = (score, fd)
    return best[1] if best[0] >= 0.72 else None


def convert_fifa_match(raw: dict[str, Any]) -> dict[str, Any] | None:
    match_no = parse_match_no(raw)
    runde = round_from_match(match_no, raw)
    if runde not in VALID_ROUNDS:
        return None

    home = team_name(raw, "home")
    away = team_name(raw, "away")
    utc_date = parse_date(raw)
    event_id = parse_event_id(raw)
    fifa_status = parse_fifa_status(raw)
    finished = fifa_status_is_finished(fifa_status)
    live = fifa_status_is_live(fifa_status)

    hs = parse_fifa_score(raw, "home")
    as_ = parse_fifa_score(raw, "away")
    result = None
    if hs is not None and as_ is not None:
        result = {
            "home_score": int(hs),
            "away_score": int(as_),
            "source": "fifa_calendar",
            "source_role": "primary",
        }

    # Hvis FIFA leverer score, behandler vi kampen som ferdig selv om statusfeltet er annerledes/mangelfullt.
    if result and not live:
        finished = True

    tippebar, tippe_status, vis_tips = calc_tip_status(utc_date, home, away, finished, live)
    match_id = f"M{match_no}" if match_no is not None else f"fifa_{event_id}" if event_id else None
    if not match_id:
        return None

    return {
        "id": match_id,
        "match_no": match_no,
        "fifa_event_id": event_id,
        "fd_match_id": None,
        "runde": runde,
        "hjemme": home or "TBD",
        "borte": away or "TBD",
        "utcDate": utc_date,
        "fifa_status": fifa_status,
        "fd_status": None,
        "tippebar": tippebar,
        "tippe_status": tippe_status,
        "vis_tips": vis_tips,
        "ferdig": finished,
        "ferdig_kilde": "fifa_calendar" if finished else None,
        "resultat": result,
    }


def sort_key(match: dict[str, Any]) -> tuple[int, int, str]:
    return (ROUND_ORDER.get(match.get("runde"), 99), int(match.get("match_no") or 999), str(match.get("utcDate") or ""))


def same_score(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if not a or not b:
        return False
    return a.get("home_score") == b.get("home_score") and a.get("away_score") == b.get("away_score")


def apply_football_data_control(matches: list[dict[str, Any]], fd_matches: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    discrepancies: list[dict[str, Any]] = []
    allow_fd_fallback = os.getenv("FD_RESULT_FALLBACK", "true").strip().lower() not in {"0", "false", "no"}

    for match in matches:
        fd = None
        if match.get("fd_match_id"):
            fd = next((x for x in fd_matches if x.get("id") == match.get("fd_match_id")), None)
        if not fd:
            fd = match_fd(match, fd_matches)
        if not fd:
            continue

        match["fd_match_id"] = fd.get("id")
        match["fd_status"] = fd.get("status")
        fd_result = fd_score(fd) if fd.get("status") == "FINISHED" else None
        fifa_result = match.get("resultat")

        if fifa_result and fd_result and not same_score(fifa_result, fd_result):
            discrepancies.append({
                "match_id": match.get("id"),
                "match_no": match.get("match_no"),
                "fifa_event_id": match.get("fifa_event_id"),
                "fd_match_id": fd.get("id"),
                "type": "score_mismatch",
                "fifa_result": fifa_result,
                "football_data_result": fd_result,
            })
            # FIFA vinner alltid når begge har resultat.
            continue

        if match.get("fifa_status") and fd.get("status") and match.get("ferdig") is False and fd.get("status") == "FINISHED":
            discrepancies.append({
                "match_id": match.get("id"),
                "match_no": match.get("match_no"),
                "fifa_event_id": match.get("fifa_event_id"),
                "fd_match_id": fd.get("id"),
                "type": "status_mismatch",
                "fifa_status": match.get("fifa_status"),
                "football_data_status": fd.get("status"),
            })

        # Fallback: bare når FIFA ikke har resultat/ferdigstatus nok til å beregne poeng.
        if allow_fd_fallback and not match.get("resultat") and fd_result:
            fd_result["source"] = "football-data.org"
            fd_result["source_role"] = "fallback_when_fifa_missing_result"
            match["resultat"] = fd_result
            match["ferdig"] = True
            match["ferdig_kilde"] = "football-data.org_fallback"
            match["tippebar"] = False
            match["tippe_status"] = "ferdig"
            match["vis_tips"] = True

    return matches, discrepancies


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    previous_status = read_json(STATUS_PATH, {"matches": []})
    previous_by_id = {m.get("id"): m for m in previous_status.get("matches", []) if isinstance(m, dict)}
    warnings: list[str] = []

    fifa_raw, fifa_info = fetch_fifa()
    matches: list[dict[str, Any]] = []
    for raw in fifa_raw:
        converted = convert_fifa_match(raw)
        if not converted:
            continue
        old = previous_by_id.get(converted["id"], {})
        if old.get("fd_match_id") and not converted.get("fd_match_id"):
            converted["fd_match_id"] = old.get("fd_match_id")
        # Behold tidligere resultat bare hvis FIFA ikke leverer resultat nå.
        if old.get("resultat") and not converted.get("resultat"):
            converted["resultat"] = old.get("resultat")
            converted["ferdig"] = bool(old.get("ferdig"))
            converted["ferdig_kilde"] = old.get("ferdig_kilde")
            converted["tippebar"] = False if converted["ferdig"] else converted.get("tippebar")
            converted["tippe_status"] = "ferdig" if converted["ferdig"] else converted.get("tippe_status")
            converted["vis_tips"] = True if converted["ferdig"] else converted.get("vis_tips")
        matches.append(converted)

    if not matches and previous_status.get("matches"):
        warnings.append("FIFA-henting feilet eller ga 0 utslagskamper. Beholder forrige status.json.")
        matches = previous_status.get("matches", [])

    fd_matches, fd_info = fetch_football_data()
    top_scorers, fd_scorers_info = fetch_football_data_scorers()
    if not top_scorers and isinstance(previous_status.get("top_scorers"), list):
        top_scorers = previous_status.get("top_scorers", [])
    discrepancies: list[dict[str, Any]] = []
    if fd_matches and matches:
        matches, discrepancies = apply_football_data_control(matches, fd_matches)

    dedup: dict[str, dict[str, Any]] = {}
    for match in matches:
        mid = match.get("id")
        if not mid:
            continue
        if mid not in dedup:
            dedup[mid] = match
        else:
            old_score = len([v for v in dedup[mid].values() if v not in (None, "", [], {})])
            new_score = len([v for v in match.values() if v not in (None, "", [], {})])
            if new_score > old_score:
                dedup[mid] = match

    final_matches = sorted(dedup.values(), key=sort_key)
    status_payload = {
        "generated_at": now_iso(),
        "source": {
            "primary": "fifa_calendar",
            "control": "football-data.org",
            "result_priority": ["fifa_calendar", "football-data.org_fallback"],
        },
        "matches": final_matches,
        "top_scorers": top_scorers,
    }

    kontroll = {
        "generated_at": now_iso(),
        "fifa": fifa_info,
        "football_data": fd_info,
        "football_data_scorers": fd_scorers_info,
        "warnings": warnings,
        "discrepancies": discrepancies,
        "match_count": len(final_matches),
        "rounds": {r: len([m for m in final_matches if m.get("runde") == r]) for r in ["r32", "r16", "qf", "sf", "final"]},
        "unmapped_fd": [m.get("id") for m in final_matches if not m.get("fd_match_id")],
        "fd_result_fallback_enabled": os.getenv("FD_RESULT_FALLBACK", "true").strip().lower() not in {"0", "false", "no"},
    }

    write_json(STATUS_PATH, status_payload)
    write_json(API_KONTROLL_PATH, kontroll)
    print(f"Skrev {STATUS_PATH.relative_to(ROOT)} med {len(final_matches)} kamp(er)")
    if warnings:
        for warning in warnings:
            print(f"ADVARSEL: {warning}")
    if discrepancies:
        print(f"Kontrollavvik funnet: {len(discrepancies)}")
    time.sleep(0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
