from __future__ import annotations

import copy
import json
import os
import sys
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tvcgui.core.paths import resource_path, user_data_path
from tvcgui.features.training.wiki_input_catalog import infer_wiki_input_notation


def _bundle_base_dir() -> str:
    """Return the project/bundle root, not this relocated module's package directory."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return str(sys._MEIPASS)
    return resource_path()


def _user_data_dir() -> str:
    """Persist source-run state at the project root and frozen-run state beside the EXE."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return resource_path()


MISSIONS_DIR = os.path.join(_bundle_base_dir(), "missions")
MISSION_PROGRESS_FILE = user_data_path("training", "mission_progress.json")


@dataclass
class MissionStep:
    labels: List[str]
    grace: int = 0
    pass_step: bool = False
    grace_keeps_alive_only: bool = False
    display: str = ""
    input_notation: str = ""


@dataclass
class MissionDef:
    mission_id: str
    name: str
    character: str
    steps: List[MissionStep] = field(default_factory=list)
    notes: str = ""
    setup_debug_flags: Dict[str, int] = field(default_factory=dict)
    setup_meter_refill: bool = False
    setup_megacrash_trainer: Dict[str, Any] = field(default_factory=dict)
    goal: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MissionPack:
    character: str
    missions: List[MissionDef] = field(default_factory=list)


_MISSION_PACK_CACHE: Dict[str, tuple[int, int, MissionPack]] = {}
_MISSION_PACK_NEXT_CHECK: Dict[str, float] = {}
_MISSION_PROGRESS_CACHE: Dict[str, Any] = {"signature": None, "data": {}, "next_check": 0.0}
MISSION_PACK_POLL_INTERVAL = 0.50
MISSION_PROGRESS_POLL_INTERVAL = 0.25


def _safe_slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")


def _missions_path_for_character(character_name: str) -> str:
    slug = _safe_slug(character_name)
    return os.path.join(MISSIONS_DIR, f"{slug}.json")


def _load_json_file(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json_file(path: str, payload: Any) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


_MISSION_STRENGTH_BUTTONS = {
    "a": "A",
    "b": "B",
    "c": "C",
    "l": "A",
    "light": "A",
    "weak": "A",
    "m": "B",
    "mid": "B",
    "medium": "B",
    "h": "C",
    "heavy": "C",
    "strong": "C",
}


def _mission_strength_button(candidates: List[str]) -> str:
    for raw in reversed(candidates):
        tokens = re.findall(r"[A-Za-z]+", str(raw or "").lower())
        for token in reversed(tokens):
            mapped = _MISSION_STRENGTH_BUTTONS.get(token)
            if mapped:
                return mapped
    return ""


def _mission_has_air_marker(value: str) -> bool:
    text = str(value or "")
    return bool(
        re.search(r"(?:^|[^a-z0-9])air(?:$|[^a-z0-9])", text, flags=re.IGNORECASE)
        or re.search(r"(?:^|[^a-z0-9])j\.", text, flags=re.IGNORECASE)
    )


def _mission_variant_parts(labels: List[str], display: str = "") -> List[str]:
    values = [str(label or "").strip() for label in labels if str(label or "").strip()]
    display_text = str(display or "").strip()

    if display_text:
        if re.search(r"\s+/\s+", display_text):
            display_parts = [
                part.strip()
                for part in re.split(r"\s+/\s+", display_text)
                if part.strip()
            ]
            if len(display_parts) > 1:
                values = display_parts
        elif not values:
            values = [display_text]

    return values


def _mission_preferred_variants(labels: List[str], display: str = "") -> List[str]:
    values = _mission_variant_parts(labels, display)
    if not values:
        return []

    display_text = str(display or "").strip()
    display_requests_air = bool(display_text and _mission_has_air_marker(display_text))
    air_values = [value for value in values if _mission_has_air_marker(value)]
    ground_values = [value for value in values if not _mission_has_air_marker(value)]

    if display_requests_air and air_values:
        return air_values
    if ground_values:
        return ground_values
    return air_values or values


def _mission_strength_variant(value: str) -> tuple[str, str]:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return "", ""

    prefix = re.fullmatch(
        r"(light|weak|mid|medium|heavy|strong|[LMH])\s+(.+)",
        text,
        flags=re.IGNORECASE,
    )
    if prefix:
        button = _MISSION_STRENGTH_BUTTONS.get(prefix.group(1).lower(), "")
        if button:
            return prefix.group(2).strip(), button

    suffix = re.fullmatch(
        r"(.+?)\s+(light|weak|mid|medium|heavy|strong|[ABCLMH])",
        text,
        flags=re.IGNORECASE,
    )
    if suffix:
        button = _MISSION_STRENGTH_BUTTONS.get(suffix.group(2).lower(), "")
        if button:
            return suffix.group(1).strip(), button

    return text, ""


def _mission_variant_buttons(labels: List[str], display: str = "") -> List[str]:
    found = []
    for value in _mission_preferred_variants(labels, display):
        _base, button = _mission_strength_variant(value)
        if button and button not in found:
            found.append(button)
    return [button for button in ("A", "B", "C") if button in found]


def _mission_is_assist_step(labels: List[str], display: str = "") -> bool:
    values = [str(display or "").strip()] + [
        str(label or "").strip() for label in labels
    ]
    joined = " / ".join(value.lower() for value in values if value)
    if "assist" in joined:
        return True

    compact_parts = [
        re.sub(r"\s+", "", value.upper())
        for value in _mission_variant_parts(labels, display)
    ]
    return bool(compact_parts) and all(
        part in {"A+P", "B+P", "C+P", "AP", "BP", "CP"}
        for part in compact_parts
    )


def _normalize_step_display(labels: List[str], display: str = "") -> str:
    display_text = str(display or "").strip()

    if _mission_is_assist_step(labels, display_text):
        return "ATK+P"

    values = _mission_preferred_variants(labels, display_text)
    if not values:
        return display_text

    collapsed = []
    for value in values:
        base, button = _mission_strength_variant(value)
        collapsed.append((base, button))

    bases = {
        re.sub(r"[^a-z0-9]+", " ", base.lower()).strip()
        for base, _button in collapsed
        if base
    }
    buttons = [
        button for button in ("A", "B", "C")
        if any(found == button for _base, found in collapsed)
    ]

    if len(bases) == 1 and len(buttons) >= 2:
        base = next(base for base, _button in collapsed if base)
        return f"{base} {'/'.join(buttons)}"

    if display_text and not re.search(r"\s+/\s+", display_text):
        return display_text

    return " / ".join(values)


def _collapse_motion_strength_notation(notation: str) -> str:
    parts = [
        " ".join(part.strip().split())
        for part in re.split(r"\s+/\s+", str(notation or "").strip())
        if part.strip()
    ]
    if len(parts) < 2:
        return str(notation or "").strip()

    parsed = []
    for part in parts:
        match = re.fullmatch(r"(.+?)([ABC])", part, flags=re.IGNORECASE)
        if not match:
            return " / ".join(parts)
        parsed.append((match.group(1), match.group(2).upper()))

    prefixes = {prefix.upper() for prefix, _button in parsed}
    buttons = [
        button for button in ("A", "B", "C")
        if any(found == button for _prefix, found in parsed)
    ]
    if len(prefixes) == 1 and len(buttons) >= 2:
        prefix = parsed[0][0]
        return f"{prefix}{'/'.join(buttons)}"
    return " / ".join(parts)


def _normalize_step_input_notation(
    notation: str,
    labels: List[str],
    display: str = "",
    *,
    expand_strength_variants: bool = True,
) -> str:
    if _mission_is_assist_step(labels, display):
        return "ATK+P"

    raw = " ".join(str(notation or "").strip().split())
    if not raw:
        return ""

    raw = re.sub(
        r"(?:^|\s+/\s+)AIR\s+",
        lambda match: match.group(0).replace("AIR ", ""),
        raw,
        flags=re.IGNORECASE,
    )
    raw = re.sub(r"^AIR\s+", "", raw, flags=re.IGNORECASE)

    buttons = _mission_variant_buttons(labels, display) if expand_strength_variants else []
    if buttons:
        replacement = "/".join(buttons)
        raw = re.sub(r"(?<!X)X(?!X)", replacement, raw)
        if len(buttons) >= 2:
            raw = re.sub(
                r"(?P<prefix>(?:\[[1-9]\]|[1-9])+)[ABC]$",
                lambda match: f"{match.group('prefix')}{replacement}",
                raw,
                flags=re.IGNORECASE,
            )

    return _collapse_motion_strength_notation(raw)


def _infer_step_input_notation(character_name: str, labels: List[str], display: str = "") -> str:
    """Infer a readable command hint without requiring mission JSON rewrites."""
    candidates = [str(display or "").strip()] + [str(label or "").strip() for label in labels]
    candidates = [value for value in candidates if value]
    lowered = [value.lower() for value in candidates]

    if any("baroque cancel" in value for value in lowered):
        return "A+P / B+P / C+P"

    if any("jump cancel" in value for value in lowered):
        return "7 / 8 / 9"

    # Normals already contain their own notation. Jump normals stay literal.
    # A second j.A is still j.A, not a motion command inferred from the wiki.
    for value in candidates:
        compact = value.replace(" ", "")
        jump_match = re.search(
            r"(?:^|[^a-z0-9])j\.?([ABC])(?:$|[^a-z0-9])",
            value,
            flags=re.IGNORECASE,
        )
        if not jump_match:
            jump_match = re.search(
                r"(?:^|[^a-z0-9])air\s*([ABC])(?:$|[^a-z0-9])",
                value,
                flags=re.IGNORECASE,
            )
        if jump_match:
            return f"j.{jump_match.group(1).upper()}"

        match = re.fullmatch(r"([1-9])([ABC])", compact, flags=re.IGNORECASE)
        if match:
            return f"{match.group(1)}{match.group(2).upper()}"

    joined = " / ".join(lowered)
    strength = _mission_strength_button(candidates)

    if "donkey" in joined:
        return f"421{strength}" if strength else "421A/B/C"

    if "shinkuu" in joined or "shinku" in joined:
        return "236XX"

    preferred_candidates = _mission_preferred_variants(labels, display)
    wiki_candidates = (
        [str(display or "").strip()] + preferred_candidates
        if str(display or "").strip()
        else preferred_candidates
    )
    wiki_candidates = [value for value in wiki_candidates if value]

    wiki_notation = infer_wiki_input_notation(character_name, wiki_candidates)
    if wiki_notation:
        return _normalize_step_input_notation(
            wiki_notation,
            labels,
            display,
        )

    char_key = _safe_slug(character_name)
    if char_key in {"chun_li", "chunli"}:
        if "legs" in joined or "lightning legs" in joined:
            return f"{strength or 'A'} {strength or 'A'} {strength or 'A'}"
        if "kikoken" in joined:
            return f"[4]6{strength or 'A'}"
        if "spinning bird" in joined or re.search(r"\bsbk\b", joined):
            return f"[2]8{strength or 'A'}"
        if "tensho" in joined:
            return f"22{strength or 'A'}"

    return ""


def _load_mission_pack_uncached(character_name: str) -> MissionPack:
    path = _missions_path_for_character(character_name)
    raw = _load_json_file(path, default=None)

    if not isinstance(raw, dict):
        return MissionPack(character=character_name, missions=[])

    missions: List[MissionDef] = []
    raw_missions = raw.get("missions", [])

    for entry in raw_missions:
        if not isinstance(entry, dict):
            continue

        mission_id = str(entry.get("mission_id", "")).strip()
        name = str(entry.get("name", mission_id)).strip()
        notes = str(entry.get("notes", "")).strip()

        setup_debug_flags: Dict[str, int] = {}
        raw_setup_debug_flags = entry.get("setup_debug_flags", {})
        if isinstance(raw_setup_debug_flags, dict):
            for key, value in raw_setup_debug_flags.items():
                try:
                    setup_debug_flags[str(key)] = int(value)
                except Exception:
                    pass

        raw_setup_meter_refill = entry.get("setup_meter_refill", False)
        if isinstance(raw_setup_meter_refill, str):
            setup_meter_refill = raw_setup_meter_refill.strip().lower() in {"1", "true", "yes", "on"}
        else:
            setup_meter_refill = bool(raw_setup_meter_refill)

        setup_megacrash_trainer: Dict[str, Any] = {}
        raw_setup_mega = (
            entry.get("setup_megacrash_trainer")
            or entry.get("setup_megacrash")
            or entry.get("megacrash_trainer")
            or {}
        )
        if isinstance(raw_setup_mega, dict):
            # Keep values raw-ish here; main.py owns validation/clamping so
            # the mission JSON can use strings like "targeted" or "5C".
            setup_megacrash_trainer = dict(raw_setup_mega)

        goal: Dict[str, Any] = {}
        raw_goal = entry.get("goal", {})
        if isinstance(raw_goal, dict):
            goal = dict(raw_goal)

        steps: List[MissionStep] = []
        for step in entry.get("steps", []):
            if not isinstance(step, dict):
                continue

            labels: List[str] = []
            raw_labels = step.get("labels")

            if isinstance(raw_labels, list):
                for item in raw_labels:
                    text = str(item).strip()
                    if text:
                        labels.append(text)

            if not labels:
                label = str(step.get("label", "")).strip()
                if label:
                    labels.append(label)

            if not labels:
                continue

            grace = 0
            try:
                grace = max(0, int(step.get("grace", 0) or 0))
            except Exception:
                grace = 0

            pass_step = bool(step.get("pass", False))
            display = str(
                step.get("display")
                or step.get("display_label")
                or step.get("text")
                or ""
            ).strip()
            input_notation = str(
                step.get("input")
                or step.get("command")
                or step.get("notation")
                or step.get("directions")
                or ""
            ).strip()
            explicit_input_notation = bool(input_notation)

            display = _normalize_step_display(labels, display)

            if not input_notation:
                input_notation = _infer_step_input_notation(
                    character_name,
                    labels,
                    display,
                )

            input_notation = _normalize_step_input_notation(
                input_notation,
                labels,
                display,
                expand_strength_variants=not explicit_input_notation,
            )

            steps.append(
                MissionStep(
                    labels=labels,
                    grace=grace,
                    pass_step=pass_step,
                    grace_keeps_alive_only=bool(
                        step.get("grace_keeps_alive_only", False)
                    ),
                    display=display,
                    input_notation=input_notation,
                )
            )
        if not mission_id or (not steps and not goal):
            continue

        missions.append(
            MissionDef(
                mission_id=mission_id,
                name=name,
                character=character_name,
                steps=steps,
                notes=notes,
                setup_debug_flags=setup_debug_flags,
                setup_meter_refill=setup_meter_refill,
                setup_megacrash_trainer=setup_megacrash_trainer,
                goal=goal,
            )
        )

    return MissionPack(character=character_name, missions=missions)


def load_mission_pack(character_name: str) -> MissionPack:
    """Load a mission pack only when its source file changes."""
    path = _missions_path_for_character(character_name)
    now = time.monotonic()
    cached = _MISSION_PACK_CACHE.get(path)
    if cached and now < float(_MISSION_PACK_NEXT_CHECK.get(path, 0.0) or 0.0):
        return cached[2]

    try:
        stat = os.stat(path)
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        signature = (-1, -1)

    _MISSION_PACK_NEXT_CHECK[path] = now + MISSION_PACK_POLL_INTERVAL
    if cached and cached[:2] == signature:
        return cached[2]

    pack = _load_mission_pack_uncached(character_name)
    _MISSION_PACK_CACHE[path] = (signature[0], signature[1], pack)
    return pack


def _progress_signature() -> tuple[int, int]:
    try:
        stat = os.stat(MISSION_PROGRESS_FILE)
        return int(stat.st_mtime_ns), int(stat.st_size)
    except OSError:
        return -1, -1


def load_progress() -> Dict[str, Any]:
    """Load mission progress only when the file changes."""
    now = time.monotonic()
    cached_signature = _MISSION_PROGRESS_CACHE.get("signature")
    if cached_signature is not None and now < float(_MISSION_PROGRESS_CACHE.get("next_check", 0.0) or 0.0):
        return copy.deepcopy(_MISSION_PROGRESS_CACHE.get("data") or {})

    signature = _progress_signature()
    _MISSION_PROGRESS_CACHE["next_check"] = now + MISSION_PROGRESS_POLL_INTERVAL
    if cached_signature == signature:
        return copy.deepcopy(_MISSION_PROGRESS_CACHE.get("data") or {})

    data = _load_json_file(MISSION_PROGRESS_FILE, default={})
    if not isinstance(data, dict):
        data = {}
    _MISSION_PROGRESS_CACHE["signature"] = signature
    _MISSION_PROGRESS_CACHE["data"] = copy.deepcopy(data)
    return copy.deepcopy(data)


def save_progress(progress: Dict[str, Any]) -> None:
    _save_json_file(MISSION_PROGRESS_FILE, progress)
    _MISSION_PROGRESS_CACHE["signature"] = _progress_signature()
    _MISSION_PROGRESS_CACHE["data"] = copy.deepcopy(progress if isinstance(progress, dict) else {})
    _MISSION_PROGRESS_CACHE["next_check"] = time.monotonic() + MISSION_PROGRESS_POLL_INTERVAL


def _get_char_progress_node(progress: Dict[str, Any], character_name: str) -> Dict[str, Any]:
    char_key = _safe_slug(character_name)
    node = progress.get(char_key)

    if not isinstance(node, dict):
        node = {}

    completed = node.get("completed")
    if not isinstance(completed, dict):
        legacy_completed = {
            k: v for k, v in node.items()
            if isinstance(k, str) and k not in {"selected_mission_id", "locked_mission_id"}
        }
        completed = legacy_completed

    return {
        "completed": completed,
        "selected_mission_id": node.get("selected_mission_id"),
        "locked_mission_id": node.get("locked_mission_id"),
    }


def _set_char_progress_node(progress: Dict[str, Any], character_name: str, node: Dict[str, Any]) -> Dict[str, Any]:
    char_key = _safe_slug(character_name)
    progress[char_key] = {
        "completed": dict(node.get("completed", {})),
        "selected_mission_id": node.get("selected_mission_id"),
        "locked_mission_id": node.get("locked_mission_id"),
    }
    return progress


def is_mission_complete(progress: Dict[str, Any], character_name: str, mission_id: str) -> bool:
    node = _get_char_progress_node(progress, character_name)
    completed = node.get("completed", {})
    return bool(completed.get(mission_id, False))


def mark_mission_complete(progress: Dict[str, Any], character_name: str, mission_id: str) -> Dict[str, Any]:
    node = _get_char_progress_node(progress, character_name)
    completed = dict(node.get("completed", {}))
    completed[mission_id] = True
    node["completed"] = completed
    return _set_char_progress_node(progress, character_name, node)


def clear_character_completions(progress: Dict[str, Any], character_name: str) -> Dict[str, Any]:
    """Clear only this character's completion marks; keep the selected trial."""
    node = _get_char_progress_node(progress, character_name)
    node["completed"] = {}
    return _set_char_progress_node(progress, character_name, node)


def get_selected_mission_id(progress: Dict[str, Any], character_name: str) -> Optional[str]:
    node = _get_char_progress_node(progress, character_name)
    mission_id = node.get("selected_mission_id")
    return str(mission_id).strip() if mission_id else None


def set_selected_mission_id(progress: Dict[str, Any], character_name: str, mission_id: Optional[str]) -> Dict[str, Any]:
    node = _get_char_progress_node(progress, character_name)
    node["selected_mission_id"] = mission_id
    return _set_char_progress_node(progress, character_name, node)


def get_locked_mission_id(progress: Dict[str, Any], character_name: str) -> Optional[str]:
    node = _get_char_progress_node(progress, character_name)
    mission_id = node.get("locked_mission_id")
    return str(mission_id).strip() if mission_id else None


def set_locked_mission_id(progress: Dict[str, Any], character_name: str, mission_id: Optional[str]) -> Dict[str, Any]:
    node = _get_char_progress_node(progress, character_name)
    node["locked_mission_id"] = mission_id
    return _set_char_progress_node(progress, character_name, node)


def find_mission_by_id(pack: MissionPack, mission_id: Optional[str]) -> Optional[MissionDef]:
    if not mission_id:
        return None
    for mission in pack.missions:
        if mission.mission_id == mission_id:
            return mission
    return None


def pick_default_mission(pack: MissionPack, progress: Dict[str, Any]) -> Optional[MissionDef]:
    if not pack.missions:
        return None

    selected_id = get_selected_mission_id(progress, pack.character)
    selected = find_mission_by_id(pack, selected_id)
    if selected is not None:
        return selected

    for mission in pack.missions:
        if not is_mission_complete(progress, pack.character, mission.mission_id):
            return mission

    return pack.missions[0]


def _goal_to_step_labels(goal: Dict[str, Any]) -> List[List[str]]:
    if not isinstance(goal, dict):
        return []

    goal_type = str(goal.get("type", "")).strip().lower()

    if goal_type == "state_duration":
        target_state = str(goal.get("target_state", "")).strip()
        frames = int(goal.get("frames", 0) or 0)
        if target_state and frames > 0:
            return [[f"{target_state} for {frames} frames"]]

    if goal_type == "damage_under_hits":
        damage = int(goal.get("damage", 0) or 0)
        max_hits = int(goal.get("max_hits", 0) or 0)
        if damage > 0 and max_hits > 0:
            return [[f"{damage} damage in {max_hits} hits or less"]]

    if goal_type == "combo_damage":
        damage = int(goal.get("damage", 0) or 0)
        if damage > 0:
            return [[f"{damage} damage in a single combo"]]

    return [["Special challenge"]]

_STEP_BTN_RE = re.compile(
    r"(?:^[0-9]+([ABCLMH])(?:$|\s|\(|\))|^j\.?([ABCLMH])(?:$|\s|\(|\))|(?:^|\s)([ABCLMH])(?:$|\s|\(|\)))",
    re.I,
)

def _step_color(labels: List[str]) -> Optional[str]:
    text = " / ".join(str(x).strip() for x in (labels or []) if str(x).strip())
    if not text:
        return None

    m = _STEP_BTN_RE.search(text)
    if not m:
        return None

    btn = next(g for g in m.groups() if g).upper()

    if btn in {"A", "L"}:
        return "blue"
    if btn in {"B", "M"}:
        return "yellow"
    if btn in {"C", "H"}:
        return "green"

    return None
def build_overlay_payload(character_name: str) -> Dict[str, Any]:
    pack = load_mission_pack(character_name)
    progress = load_progress()
    active = pick_default_mission(pack, progress)
    selected_id = get_selected_mission_id(progress, pack.character)
    locked_id = get_locked_mission_id(progress, pack.character)

    return {
        "character": pack.character,
        "mission_count": len(pack.missions),
        "active_mission_id": active.mission_id if active else None,
        "active_mission_name": active.name if active else None,
        "active_mission_notes": active.notes if active else "",
        "active_mission_steps": (
            [
{
    "labels": step.labels,
    "display": step.display,
    "input": step.input_notation,
    "grace": step.grace,
    "pass": step.pass_step,
    "grace_keeps_alive_only": step.grace_keeps_alive_only,
    "color": _step_color(step.labels),
}
    for step in active.steps
]
            if active and active.steps
            else _goal_to_step_labels(active.goal) if active else []
        ),
        "active_mission_goal": dict(active.goal) if active else {},
        "active_mission_setup_debug_flags": dict(active.setup_debug_flags) if active else {},
        "active_mission_setup_meter_refill": bool(active.setup_meter_refill) if active else False,
        "active_mission_setup_megacrash_trainer": dict(active.setup_megacrash_trainer) if active else {},
        "selected_mission_id": selected_id,
        "locked_mission_id": locked_id,
        "missions": [
            {
                "mission_id": mission.mission_id,
                "name": mission.name,
                "notes": mission.notes,
                "completed": is_mission_complete(progress, pack.character, mission.mission_id),
                "selected": mission.mission_id == selected_id,
                "locked": mission.mission_id == locked_id,
                "steps": [
{
    "labels": step.labels,
    "display": step.display,
    "input": step.input_notation,
    "grace": step.grace,
    "pass": step.pass_step,
    "grace_keeps_alive_only": step.grace_keeps_alive_only,
    "color": _step_color(step.labels),
}
    for step in mission.steps
] if mission.steps else _goal_to_step_labels(mission.goal),
                "goal": dict(mission.goal),
                "setup_debug_flags": dict(mission.setup_debug_flags),
                "setup_meter_refill": bool(mission.setup_meter_refill),
                "setup_megacrash_trainer": dict(mission.setup_megacrash_trainer),
            }
            for mission in pack.missions
        ],
    }
