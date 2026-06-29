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


def load_tips() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for round_name in ROUNDS:
        folder = TIPPING_DIR / round_name
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            data = read_json(path, {})
            participant = str(data.get("deltaker") or safe_participant_from_file(path)).strip()
            submitted_at = data.get("submitted_at")
            raw_tips = data.get("tips", []) if isinstance(data.get("tips"), list) else []
            for tip in raw_tips:
                try:
                    rows.append({
                        "deltaker": participant,
                        "runde": data.get("runde") or round_name,
                        "submitted_at": submitted_at,
                        "source_file": str(path.relative_to(ROOT)),
                        "match_id": str(tip.get("match_id") or tip.get("id") or ""),
                        "match_no": tip.get("match_no"),
                        "fifa_event_id": tip.get("fifa_event_id"),
                        "hjemme_snapshot": tip.get("hjemme_snapshot"),
                        "borte_snapshot": tip.get("borte_snapshot"),
                        "home_score": int(tip.get("home_score")),
                        "away_score": int(tip.get("away_score")),
                    })
                except Exception:
                    print(f"Ignorerer ugyldig tipsrad i {path}")
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


def main() -> int:
    status = read_json(STATUS_PATH, {"generated_at": None, "matches": []})
    matches = status.get("matches", []) if isinstance(status.get("matches"), list) else []
    tips = load_tips()

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
            player = players.setdefault(participant, {
                "deltaker": participant,
                "poeng": 0,
                "exact": 0,
                "outcome": 0,
                "tips_levert": 0,
                "per_round": {r: {"poeng": 0, "tips": 0} for r in ROUNDS},
            })
            player["tips_levert"] += 1
            player["per_round"].setdefault(round_name, {"poeng": 0, "tips": 0})["tips"] += 1

            calc = score_tip(tip, result)
            if calc["beregnet"]:
                player["poeng"] += calc["poeng"]
                player["per_round"][round_name]["poeng"] += calc["poeng"]
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

    leaderboard = sorted(players.values(), key=lambda p: (-p["poeng"], -p["exact"], -p["outcome"], p["deltaker"].lower()))
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
        },
        "matches": match_output,
        "deltakere": players,
        "leaderboard": leaderboard,
        "summary": {
            "tips_total": len(tips),
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
