#!/usr/bin/env python3
"""Bygger data/innleveringsstatus.json fra tippingfiler."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
TIPPING_DIR = ROOT / "tippinger"
STATUS_PATH = DATA_DIR / "status.json"
INNLEVERING_PATH = DATA_DIR / "innleveringsstatus.json"
DATA_JS_PATH = DATA_DIR / "data.js"
RESULTATER_PATH = DATA_DIR / "resultater.json"

ROUNDS = ["r32", "r16", "qf", "sf", "final"]


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


def write_data_js() -> None:
    status = read_json(STATUS_PATH, {"generated_at": None, "matches": []})
    resultater = read_json(RESULTATER_PATH, {
        "generated_at": None,
        "regler": {"regel": "90 minutter", "poeng_exact": 3, "poeng_outcome": 1},
        "matches": [],
        "deltakere": {},
        "leaderboard": [],
    })
    innlevering = read_json(INNLEVERING_PATH, {"generated_at": None, "rounds": {}, "deltakere": []})
    payload = {"status": status, "resultater": resultater, "innleveringsstatus": innlevering}
    DATA_JS_PATH.write_text(
        "window.NUSSEBASSENE_DATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )


def safe_participant_from_file(path: Path) -> str:
    name = path.stem.replace("-", " ").replace("_", " ").strip()
    return re.sub(r"\s+", " ", name).title() or "Ukjent"


def read_tip_files() -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for round_name in ROUNDS:
        folder = TIPPING_DIR / round_name
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            data = read_json(path, {})
            participant = str(data.get("deltaker") or safe_participant_from_file(path)).strip()
            tips = data.get("tips") if isinstance(data.get("tips"), list) else []
            files.append({
                "deltaker": participant,
                "runde": data.get("runde") or round_name,
                "submitted_at": data.get("submitted_at"),
                "path": str(path.relative_to(ROOT)),
                "tips": tips,
            })
    return files


def main() -> int:
    status = read_json(STATUS_PATH, {"matches": []})
    matches = status.get("matches", []) if isinstance(status.get("matches"), list) else []

    possible_by_round = {
        r: [m for m in matches if m.get("runde") == r and m.get("hjemme") not in (None, "", "TBD") and m.get("borte") not in (None, "", "TBD")]
        for r in ROUNDS
    }
    possible_ids_by_round = {r: {m.get("id") for m in ms} for r, ms in possible_by_round.items()}

    participants: dict[str, dict[str, Any]] = {}
    for file in read_tip_files():
        participant = file["deltaker"]
        round_name = file["runde"]
        p = participants.setdefault(participant, {
            "deltaker": participant,
            "total_levert": 0,
            "rounds": {
                r: {"levert": 0, "mulige": len(possible_by_round[r]), "komplett": False, "submitted_at": None, "path": None}
                for r in ROUNDS
            },
        })
        delivered_ids = {str(t.get("match_id") or t.get("id")) for t in file["tips"] if isinstance(t, dict)}
        valid_delivered = delivered_ids.intersection(possible_ids_by_round.get(round_name, set()))
        p["rounds"].setdefault(round_name, {"levert": 0, "mulige": len(possible_by_round.get(round_name, [])), "komplett": False, "submitted_at": None, "path": None})
        p["rounds"][round_name].update({
            "levert": len(valid_delivered),
            "mulige": len(possible_by_round.get(round_name, [])),
            "komplett": len(possible_by_round.get(round_name, [])) > 0 and len(valid_delivered) >= len(possible_by_round.get(round_name, [])),
            "submitted_at": file.get("submitted_at"),
            "path": file.get("path"),
        })

    for p in participants.values():
        p["total_levert"] = sum(r.get("levert", 0) for r in p["rounds"].values())

    payload = {
        "generated_at": now_iso(),
        "rounds": {
            r: {
                "mulige": len(possible_by_round[r]),
                "match_ids": [m.get("id") for m in possible_by_round[r]],
            }
            for r in ROUNDS
        },
        "deltakere": sorted(participants.values(), key=lambda p: p["deltaker"].lower()),
    }

    write_json(INNLEVERING_PATH, payload)
    write_data_js()
    print(f"Skrev {INNLEVERING_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
