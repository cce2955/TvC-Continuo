"""Persistent observed timing values collected during normal play."""
from __future__ import annotations

import json
import os
import threading
from collections import Counter
from copy import deepcopy
from typing import Any

from tvcgui.core.paths import user_data_path

OBSERVATION_VERSION = 1
OBSERVATION_FILE = user_data_path("frame_data", "timing_observations.json")
AUTO_FILL_MATCHES = 2
CONFIRMED_MATCHES = 3
MAX_SAMPLES_PER_FIELD = 16
BLOCK_ADVANTAGE_OBSERVATION_ENABLED = False

_LOCK = threading.RLock()
_DOC: dict[str, Any] | None = None


def _empty_doc() -> dict[str, Any]:
    return {"version": OBSERVATION_VERSION, "characters": {}}


def _strip_untrusted_block_advantage(doc: dict[str, Any]) -> bool:
    """Remove block-advantage samples produced by the retired readiness guess."""
    changed = False
    for char_row in (doc.get("characters") or {}).values():
        if not isinstance(char_row, dict):
            continue
        for move in (char_row.get("moves") or {}).values():
            if not isinstance(move, dict):
                continue
            fields = move.get("fields")
            if isinstance(fields, dict) and fields.pop("adv_block_observed", None) is not None:
                changed = True
    return changed


def _write_doc_file(doc: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(OBSERVATION_FILE)), exist_ok=True)
    tmp = f"{OBSERVATION_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, OBSERVATION_FILE)


def _load() -> dict[str, Any]:
    global _DOC
    with _LOCK:
        if _DOC is not None:
            return _DOC
        try:
            with open(OBSERVATION_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or int(data.get("version") or 0) != OBSERVATION_VERSION:
                data = _empty_doc()
        except Exception:
            data = _empty_doc()
        data.setdefault("characters", {})
        if _strip_untrusted_block_advantage(data):
            try:
                _write_doc_file(data)
            except Exception:
                pass
        _DOC = data
        return _DOC


def _save(doc: dict[str, Any]) -> None:
    global _DOC
    with _LOCK:
        _write_doc_file(doc)
        _DOC = doc


def _consensus(samples: list[int]) -> dict[str, Any]:
    values = [int(value) for value in samples]
    if not values:
        return {"value": None, "matches": 0, "sample_count": 0, "confidence": "none"}
    counts = Counter(values)
    value, matches = counts.most_common(1)[0]
    confidence = "confirmed" if matches >= CONFIRMED_MATCHES else "matched" if matches >= AUTO_FILL_MATCHES else "single"
    return {"value": int(value), "matches": int(matches), "sample_count": len(values), "confidence": confidence}


def record_timing_result(result: Any) -> dict[str, Any]:
    """Record stable fields from one completed TimingResult."""
    try:
        payload = result.to_dict()
    except Exception:
        payload = dict(result or {})
    char_id = int(payload.get("char_id") or 0)
    action_id = int(payload.get("action_id") or 0)
    kind = str(payload.get("kind") or "").lower()
    if char_id <= 0 or action_id < 0x0100 or kind not in {"hit", "block"}:
        return {}

    fields: dict[str, int] = {}
    single_contact = int(payload.get("contact_count") or 1) == 1
    if kind == "block":
        if single_contact and int(payload.get("blockstun") or 0) > 0:
            fields["blockstun"] = int(payload["blockstun"])
        if (
            BLOCK_ADVANTAGE_OBSERVATION_ENABLED
            and payload.get("block_advantage") is not None
            and bool(payload.get("clean"))
        ):
            fields["adv_block_observed"] = int(payload["block_advantage"])
    else:
        if single_contact and int(payload.get("hitstun") or 0) > 0:
            fields["hitstun"] = int(payload["hitstun"])
        if single_contact and int(payload.get("damage") or 0) > 0:
            fields["damage"] = int(payload["damage"])
    if int(payload.get("hitstop") or 0) > 0 and single_contact:
        fields["hitstop"] = int(payload["hitstop"])
    if not fields:
        return {}

    with _LOCK:
        doc = deepcopy(_load())
        chars = doc.setdefault("characters", {})
        char_row = chars.setdefault(str(char_id), {"moves": {}})
        moves = char_row.setdefault("moves", {})
        move = moves.setdefault(str(action_id), {"action_name": str(payload.get("action_name") or ""), "fields": {}})
        if payload.get("action_name"):
            move["action_name"] = str(payload.get("action_name"))
        field_rows = move.setdefault("fields", {})
        for field_name, value in fields.items():
            row = field_rows.setdefault(field_name, {"samples": []})
            samples = [int(item) for item in list(row.get("samples") or [])]
            samples.append(int(value))
            row["samples"] = samples[-MAX_SAMPLES_PER_FIELD:]
            row.update(_consensus(row["samples"]))
        _save(doc)
        return deepcopy(move)


def get_move_observations(char_id: int, action_id: int) -> dict[str, Any]:
    with _LOCK:
        doc = _load()
        move = (((doc.get("characters") or {}).get(str(int(char_id))) or {}).get("moves") or {}).get(str(int(action_id)))
        return deepcopy(move) if isinstance(move, dict) else {}


def _empty_value(value: Any) -> bool:
    return value is None or str(value).strip() in {"", "-", "?", "n/a", "N/A"}


def apply_observations_to_scan_data(
    scan_data: Any,
    min_matches: int = AUTO_FILL_MATCHES,
    *,
    change_log: list[dict[str, Any]] | None = None,
) -> int:
    """Fill empty frame-data fields when repeated live observations agree.

    ``change_log`` is optional and receives one record for each field that was
    newly populated.  Live Frame Data windows use it to update only the rows
    that changed instead of rebuilding the entire tree.
    """
    if not isinstance(scan_data, list):
        return 0
    changed = 0
    for slot in scan_data:
        if not isinstance(slot, dict):
            continue
        char_id = int(slot.get("char_id") or slot.get("csv_char_id") or slot.get("id") or 0)
        if char_id <= 0:
            continue
        for move in list(slot.get("moves") or []):
            if not isinstance(move, dict):
                continue
            if move.get("adv_block_observed_source") == "live_timing_scan":
                move["adv_block_observed"] = None
                move.pop("adv_block_observed_source", None)
                move.pop("adv_block_observed_samples", None)
            action_id = int(move.get("id") or 0)
            observed = get_move_observations(char_id, action_id)
            fields = observed.get("fields") if isinstance(observed, dict) else None
            if not isinstance(fields, dict):
                continue
            provenance = move.setdefault("timing_observations", {})
            for field_name, row in fields.items():
                if field_name == "adv_block_observed" and not BLOCK_ADVANTAGE_OBSERVATION_ENABLED:
                    continue
                if not isinstance(row, dict):
                    continue
                provenance[field_name] = deepcopy(row)
                value = row.get("value")
                matches = int(row.get("matches") or 0)
                if value is None or matches < int(min_matches):
                    continue
                target_field = "adv_block_observed" if field_name == "adv_block_observed" else field_name
                if _empty_value(move.get(target_field)):
                    move[target_field] = int(value)
                    move[f"{target_field}_source"] = "live_timing_scan"
                    move[f"{target_field}_samples"] = int(row.get("sample_count") or matches)
                    changed += 1
                    if change_log is not None:
                        change_log.append(
                            {
                                "slot_label": str(slot.get("slot_label") or ""),
                                "char_id": int(char_id),
                                "action_id": int(action_id),
                                "field": str(target_field),
                                "value": int(value),
                                "matches": int(matches),
                                "sample_count": int(row.get("sample_count") or matches),
                                "confidence": str(row.get("confidence") or "matched"),
                            }
                        )
    return changed


def summarize_move_observations(char_id: int, action_id: int) -> str:
    move = get_move_observations(char_id, action_id)
    fields = move.get("fields") if isinstance(move, dict) else None
    if not isinstance(fields, dict) or not fields:
        return "No live timing observations yet."
    lines = [f"Live timing scans for {move.get('action_name') or f'0x{int(action_id):04X}'}"]
    for field_name, row in sorted(fields.items()):
        lines.append(
            f"{field_name}: {row.get('value')}  ({row.get('matches', 0)}/{row.get('sample_count', 0)} matching, {row.get('confidence', 'none')})"
        )
    return "\n".join(lines)


__all__ = [
    "OBSERVATION_FILE",
    "AUTO_FILL_MATCHES",
    "CONFIRMED_MATCHES",
    "BLOCK_ADVANTAGE_OBSERVATION_ENABLED",
    "record_timing_result",
    "get_move_observations",
    "apply_observations_to_scan_data",
    "summarize_move_observations",
]
