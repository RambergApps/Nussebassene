#!/usr/bin/env python3
"""Beregner poeng fra tippingfiler og skriver resultater.json + data.js.

Viktig prinsipp:
poengregning.py henter ikke API-data selv. status.json er eneste kilde.
Det sikrer at FIFA Calendar forblir primærkilden via sync-kamper.py.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
TIPPING_DIR = ROOT / "tippinger"
STATUS_PATH = DATA_DIR / "status.json"
RESULTATER_PATH = DATA_DIR / "resultater.json"
INNLEVERING_PATH = DATA_DIR / "innleveringsstatus.json"
DATA_JS_PATH = DATA_DIR / "data.js"

ROUNDS = ["r32", "r16", "qf", "sf", "final"]
POINTS_EXACT = int(os.getenv("POINTS_EXACT", "3"))
POINTS_OUTCOME = int(os.getenv("POINTS_OUTCOME", "1"))
POINTS_ROUND_BONUS = int(os.getenv("POINTS_ROUND_BONUS", "5"))
POINTS_OVERALL_BONUS = int(os.getenv("POINTS_OVERALL_BONUS", "10"))

BONUS_SPORSMAL: dict[str, dict[str, Any]] = {
    "r32": {"id": "antall_uavgjort", "tekst": "Hvor mange kamper ender uavgjort i løpet av de første 90 minuttene, inkludert tilleggstid?", "type": "number"},
    "r16": {"id": "antall_nullen", "tekst": "Hvor mange lag holder nullen i løpet av de første 90 minuttene, inkludert tilleggstid?", "type": "number"},
    "qf": {"id": "antall_ettmaalsseier", "tekst": "Hvor mange kamper avgjøres med ett mål i løpet av de første 90 minuttene, inkludert tilleggstid?", "type": "number"},
    "sf": {"id": "totale_maal", "tekst": "Hvor mange mål scores totalt i semifinalene i løpet av de første 90 minuttene, inkludert tilleggstid?", "type": "number"},
    "final": {"id": "begge_lag_scorer", "tekst": "Scorer begge lag i finalen i løpet av de første 90 minuttene, inkludert tilleggstid?", "type": "ja_nei"},
}

OVERALL_BONUS: dict[str, str] = {
    "flest_maal_lag": "Hvilket lag scorer flest mål fra 32-delsfinalene til og med finalen i løpet av de første 90 minuttene, inkludert tilleggstid?",
    "totale_maal_utslag": "Hvor mange mål scores totalt fra 32-delsfinalene til og med finalen i løpet av de første 90 minuttene, inkludert tilleggstid?",
    "golden_boot": "Hvem vinner FIFA Golden Boot?",
}

TOTAL_GOALS_INTERVALS: dict[str, tuple[int, int | None]] = {
    "0_46": (0, 46),
    "47_77": (47, 77),
    "78_pluss": (78, None),
}


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


def write_data_js(status: dict[str, Any], resultater: dict[str, Any], innlevering: dict[str, Any]) -> None:
    payload = {
        "status": status,
        "resultater": resultater,
        "innleveringsstatus": innlevering,
    }
    DATA_JS_PATH.write_text(
        "window.NUSSEBASSENE_DATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )


def safe_participant_from_file(path: Path) -> str:
    name = path.stem.replace("-", " ").replace("_", " ").strip()
    return re.sub(r"\s+", " ", name).title() or "Ukjent"


def normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("æ", "ae").replace("ø", "o").replace("å", "a")
    return re.sub(r"\s+", " ", text)


def canonical_team_name(name: Any) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip())


def normalize_bonus_payload(bonus: Any, round_name: str) -> dict[str, Any] | None:
    if not isinstance(bonus, dict):
        return None
    cfg = BONUS_SPORSMAL.get(round_name)
    if not cfg:
        return None
    bonus_id = str(bonus.get("id") or cfg["id"])
    svar = bonus.get("svar")
    if svar is None or svar == "":
        return None
    if bonus_id != cfg["id"]:
        return None
    try:
        if cfg.get("type") == "ja_nei":
            cleaned = str(svar).strip().lower()
            if cleaned not in {"ja", "nei"}:
                return None
            svar = cleaned
        else:
            svar = int(svar)
    except Exception:
        return None
    return {"id": bonus_id, "svar": svar}


def normalize_helhetsbonus(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for key in OVERALL_BONUS:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[key] = text
    return out


def read_submission_files() -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for round_name in ROUNDS:
        folder = TIPPING_DIR / round_name
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            data = read_json(path, {})
            participant = str(data.get("deltaker") or data.get("participant") or safe_participant_from_file(path)).strip()
            submitted_at = data.get("submitted_at") or data.get("innlevert")
            raw_tips = data.get("tips", []) if isinstance(data.get("tips"), list) else []

            # Bakoverkompatibilitet hvis noen filer er fra gammel VM-app-struktur.
            if not raw_tips and isinstance(data.get("tippinger"), list):
                raw_tips = data.get("tippinger")

            files.append({
                "deltaker": participant,
                "runde": data.get("runde") or round_name,
                "submitted_at": submitted_at,
                "source_file": str(path.relative_to(ROOT)),
                "tips": raw_tips,
                "bonus": normalize_bonus_payload(data.get("bonus"), round_name),
                "helhetsbonus": normalize_helhetsbonus(data.get("helhetsbonus")),
            })
    return files


def load_tips(submissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for data in submissions:
        for tip in data.get("tips") or []:
            if not isinstance(tip, dict):
                continue
            try:
                match_id = str(tip.get("match_id") or tip.get("kamp_id") or tip.get("id") or "")
                home_value = tip.get("home_score", tip.get("hjemme"))
                away_value = tip.get("away_score", tip.get("borte"))
                rows.append({
                    "deltaker": data["deltaker"],
                    "runde": data.get("runde"),
                    "submitted_at": data.get("submitted_at"),
                    "source_file": data.get("source_file"),
                    "match_id": match_id,
                    "match_no": tip.get("match_no"),
                    "fifa_event_id": tip.get("fifa_event_id"),
                    "hjemme_snapshot": tip.get("hjemme_snapshot") or tip.get("hjemmelag"),
                    "borte_snapshot": tip.get("borte_snapshot") or tip.get("bortelag"),
                    "home_score": int(home_value),
                    "away_score": int(away_value),
                })
            except Exception:
                print(f"Ignorerer ugyldig tipsrad i {data.get('source_file')}")
    return rows


def outcome(home: int, away: int) -> str:
    if home > away:
        return "H"
    if home < away:
        return "B"
    return "U"


def score_tip(tip: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {"poeng": 0, "exact": False, "outcome": False, "beregnet": False}
    th, ta = int(tip["home_score"]), int(tip["away_score"])
    rh, ra = int(result["home_score"]), int(result["away_score"])
    exact = th == rh and ta == ra
    same_outcome = outcome(th, ta) == outcome(rh, ra)
    points = POINTS_EXACT if exact else POINTS_OUTCOME if same_outcome else 0
    return {"poeng": points, "exact": exact, "outcome": same_outcome and not exact, "beregnet": True}


def finished_matches_for_round(matches: list[dict[str, Any]], round_name: str) -> list[dict[str, Any]]:
    return [m for m in matches if m.get("runde") == round_name]


def all_finished_with_results(matches: list[dict[str, Any]]) -> bool:
    return bool(matches) and all(m.get("ferdig") and isinstance(m.get("resultat"), dict) for m in matches)


def round_answer(round_name: str, matches: list[dict[str, Any]]) -> Any | None:
    round_matches = finished_matches_for_round(matches, round_name)
    if not all_finished_with_results(round_matches):
        return None

    results = [m["resultat"] for m in round_matches]
    if round_name == "r32":
        return sum(1 for r in results if int(r["home_score"]) == int(r["away_score"]))
    if round_name == "r16":
        return sum((1 if int(r["home_score"]) == 0 else 0) + (1 if int(r["away_score"]) == 0 else 0) for r in results)
    if round_name == "qf":
        return sum(1 for r in results if abs(int(r["home_score"]) - int(r["away_score"])) == 1)
    if round_name == "sf":
        return sum(int(r["home_score"]) + int(r["away_score"]) for r in results)
    if round_name == "final":
        final = results[0] if results else None
        if not final:
            return None
        return "ja" if int(final["home_score"]) > 0 and int(final["away_score"]) > 0 else "nei"
    return None


def total_goals_interval(total: int) -> str:
    for key, (low, high) in TOTAL_GOALS_INTERVALS.items():
        if total >= low and (high is None or total <= high):
            return key
    return "ukjent"


def overall_answers(matches: list[dict[str, Any]], top_scorers: list[dict[str, Any]]) -> dict[str, Any] | None:
    knockout_matches = [m for m in matches if m.get("runde") in ROUNDS]
    if not all_finished_with_results(knockout_matches):
        return None

    goals_by_team: dict[str, int] = {}
    total_goals = 0

    for match in knockout_matches:
        home = canonical_team_name(match.get("hjemme"))
        away = canonical_team_name(match.get("borte"))
        result = match.get("resultat") or {}
        hs = int(result["home_score"])
        as_ = int(result["away_score"])
        total_goals += hs + as_
        goals_by_team[home] = goals_by_team.get(home, 0) + hs
        goals_by_team[away] = goals_by_team.get(away, 0) + as_

    max_goals = max(goals_by_team.values()) if goals_by_team else 0

    scorer_rows = [s for s in top_scorers if isinstance(s, dict) and s.get("player") and parse_int(s.get("goals")) is not None]
    if not scorer_rows:
        # Ikke gi 0 poeng på Golden Boot hvis vi mangler toppscorerdata.
        return None
    max_player_goals = max(int(parse_int(s.get("goals")) or 0) for s in scorer_rows)
    golden_boot_winners = [str(s.get("player")) for s in scorer_rows if int(parse_int(s.get("goals")) or 0) == max_player_goals]

    return {
        "flest_maal_lag": [team for team, goals in goals_by_team.items() if goals == max_goals],
        "totale_maal_utslag": total_goals_interval(total_goals),
        "totale_maal_utslag_verdi": total_goals,
        "golden_boot": golden_boot_winners,
        "golden_boot_goals": max_player_goals,
    }


def ensure_player(players: dict[str, dict[str, Any]], participant: str) -> dict[str, Any]:
    return players.setdefault(participant, {
        "deltaker": participant,
        "poeng": 0,
        "kamp_poeng": 0,
        "bonus_poeng": 0,
        "rundebonus_poeng": 0,
        "helhetsbonus_poeng": 0,
        "exact": 0,
        "outcome": 0,
        "tips_levert": 0,
        "bonus_riktig": 0,
        "per_round": {r: {"poeng": 0, "kamp_poeng": 0, "bonus_poeng": 0, "tips": 0} for r in ROUNDS},
        "bonus": {"rounds": {}, "helhet": {}},
    })


def add_points(player: dict[str, Any], points: int, round_name: str | None = None, bucket: str = "kamp") -> None:
    player["poeng"] += points
    if bucket == "kamp":
        player["kamp_poeng"] += points
    else:
        player["bonus_poeng"] += points
        if bucket == "rundebonus":
            player["rundebonus_poeng"] += points
        elif bucket == "helhetsbonus":
            player["helhetsbonus_poeng"] += points
    if round_name:
        player["per_round"].setdefault(round_name, {"poeng": 0, "kamp_poeng": 0, "bonus_poeng": 0, "tips": 0})
        player["per_round"][round_name]["poeng"] += points
        if bucket == "kamp":
            player["per_round"][round_name]["kamp_poeng"] += points
        else:
            player["per_round"][round_name]["bonus_poeng"] += points


def score_round_bonus(submissions: list[dict[str, Any]], matches: list[dict[str, Any]], players: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fasit: dict[str, Any] = {r: round_answer(r, matches) for r in ROUNDS}
    output: dict[str, Any] = {}

    for submission in submissions:
        bonus = submission.get("bonus")
        if not bonus:
            continue
        participant = submission["deltaker"]
        round_name = submission.get("runde")
        cfg = BONUS_SPORSMAL.get(round_name)
        if not cfg:
            continue
        answer = fasit.get(round_name)
        calculated = answer is not None
        correct = bool(calculated and str(bonus.get("svar")).lower() == str(answer).lower())
        points = POINTS_ROUND_BONUS if correct else 0
        player = ensure_player(players, participant)
        if calculated:
            add_points(player, points, round_name, "rundebonus")
            if correct:
                player["bonus_riktig"] += 1
        player["bonus"]["rounds"][round_name] = {
            "id": bonus.get("id"),
            "svar": bonus.get("svar"),
            "fasit": answer,
            "beregnet": calculated,
            "riktig": correct if calculated else None,
            "poeng": points if calculated else None,
            "submitted_at": submission.get("submitted_at"),
        }

    for r in ROUNDS:
        output[r] = {
            "id": BONUS_SPORSMAL[r]["id"],
            "tekst": BONUS_SPORSMAL[r]["tekst"],
            "poeng": POINTS_ROUND_BONUS,
            "fasit": fasit[r],
            "beregnet": fasit[r] is not None,
        }
    return output


def score_overall_bonus(submissions: list[dict[str, Any]], matches: list[dict[str, Any]], top_scorers: list[dict[str, Any]], players: dict[str, dict[str, Any]]) -> dict[str, Any]:
    answers = overall_answers(matches, top_scorers)
    calculated = answers is not None
    output = {
        "sporsmal": OVERALL_BONUS,
        "poeng": POINTS_OVERALL_BONUS,
        "fasit": answers,
        "beregnet": calculated,
    }
    if not calculated:
        return output

    assert answers is not None
    correct_team_goals = {normalize_text(team) for team in answers.get("flest_maal_lag", [])}
    correct_golden_boot = {normalize_text(player) for player in answers.get("golden_boot", [])}
    correct_interval = answers.get("totale_maal_utslag")

    for submission in submissions:
        helhet = submission.get("helhetsbonus") or {}
        if not helhet:
            continue
        participant = submission["deltaker"]
        player = ensure_player(players, participant)
        player["bonus"].setdefault("helhet", {})

        checks = {
            "flest_maal_lag": normalize_text(helhet.get("flest_maal_lag")) in correct_team_goals,
            "totale_maal_utslag": str(helhet.get("totale_maal_utslag") or "") == str(correct_interval),
            "golden_boot": normalize_text(helhet.get("golden_boot")) in correct_golden_boot,
        }
        for key, correct in checks.items():
            if key not in helhet:
                continue
            points = POINTS_OVERALL_BONUS if correct else 0
            add_points(player, points, "helhet", "helhetsbonus")
            if correct:
                player["bonus_riktig"] += 1
            player["bonus"]["helhet"][key] = {
                "svar": helhet.get(key),
                "fasit": answers.get(key),
                "beregnet": True,
                "riktig": correct,
                "poeng": points,
                "submitted_at": submission.get("submitted_at"),
            }
    return output


def main() -> int:
    status = read_json(STATUS_PATH, {"generated_at": None, "matches": []})
    matches = status.get("matches", []) if isinstance(status.get("matches"), list) else []
    top_scorers = status.get("top_scorers", []) if isinstance(status.get("top_scorers"), list) else []
    submissions = read_submission_files()
    tips = load_tips(submissions)

    players: dict[str, dict[str, Any]] = {}
    match_output: list[dict[str, Any]] = []
    tips_by_match: dict[str, list[dict[str, Any]]] = {}

    for tip in tips:
        tips_by_match.setdefault(tip["match_id"], []).append(tip)

    for match in matches:
        match_id = match.get("id")
        result = match.get("resultat") if match.get("ferdig") else None
        show_tips = bool(match.get("vis_tips"))
        out_tips: list[dict[str, Any]] = []

        for tip in tips_by_match.get(match_id, []):
            participant = tip["deltaker"]
            round_name = tip.get("runde") or match.get("runde") or "ukjent"
            player = ensure_player(players, participant)
            player["tips_levert"] += 1
            player["per_round"].setdefault(round_name, {"poeng": 0, "kamp_poeng": 0, "bonus_poeng": 0, "tips": 0})["tips"] += 1

            calc = score_tip(tip, result)
            if calc["beregnet"]:
                add_points(player, calc["poeng"], round_name, "kamp")
                if calc["exact"]:
                    player["exact"] += 1
                elif calc["outcome"]:
                    player["outcome"] += 1

            if show_tips:
                out_tips.append({
                    "deltaker": participant,
                    "home_score": tip["home_score"],
                    "away_score": tip["away_score"],
                    "poeng": calc["poeng"] if calc["beregnet"] else None,
                    "exact": calc["exact"] if calc["beregnet"] else None,
                    "outcome": calc["outcome"] if calc["beregnet"] else None,
                    "submitted_at": tip.get("submitted_at"),
                })

        match_output.append({
            "id": match_id,
            "match_no": match.get("match_no"),
            "fifa_event_id": match.get("fifa_event_id"),
            "fd_match_id": match.get("fd_match_id"),
            "runde": match.get("runde"),
            "hjemme": match.get("hjemme"),
            "borte": match.get("borte"),
            "utcDate": match.get("utcDate"),
            "fifa_status": match.get("fifa_status"),
            "fd_status": match.get("fd_status"),
            "ferdig": bool(match.get("ferdig")),
            "ferdig_kilde": match.get("ferdig_kilde"),
            "resultat": result,
            "tips": out_tips,
        })

    round_bonus_status = score_round_bonus(submissions, matches, players)
    overall_bonus_status = score_overall_bonus(submissions, matches, top_scorers, players)

    leaderboard = sorted(players.values(), key=lambda p: (-p["poeng"], -p["exact"], -p["bonus_poeng"], -p["outcome"], p["deltaker"].lower()))
    for i, player in enumerate(leaderboard, start=1):
        player["plass"] = i

    resultater = {
        "generated_at": now_iso(),
        "source": {
            "status_source": "data/status.json",
            "api_fetching": "none_in_poengregning",
        },
        "regler": {
            "regel": "90 minutter",
            "poeng_exact": POINTS_EXACT,
            "poeng_outcome": POINTS_OUTCOME,
            "poeng_rundebonus": POINTS_ROUND_BONUS,
            "poeng_helhetsbonus": POINTS_OVERALL_BONUS,
            "bonus_sporsmal": BONUS_SPORSMAL,
            "helhetsbonus_sporsmal": OVERALL_BONUS,
            "totale_maal_intervaller": TOTAL_GOALS_INTERVALS,
        },
        "matches": match_output,
        "bonus": {
            "runder": round_bonus_status,
            "helhet": overall_bonus_status,
        },
        "deltakere": players,
        "leaderboard": leaderboard,
        "summary": {
            "tips_total": len(tips),
            "bonus_total": sum(1 for s in submissions if s.get("bonus")),
            "helhetsbonus_total": sum(len(s.get("helhetsbonus") or {}) for s in submissions),
            "kamper_total": len(matches),
            "kamper_ferdig": len([m for m in matches if m.get("ferdig")]),
        },
    }

    innlevering = read_json(INNLEVERING_PATH, {"generated_at": None, "rounds": {}, "deltakere": []})
    write_json(RESULTATER_PATH, resultater)
    write_data_js(status, resultater, innlevering)
    print(f"Skrev {RESULTATER_PATH.relative_to(ROOT)} og {DATA_JS_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
