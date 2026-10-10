from __future__ import annotations

"""Deterministic Punish / Sequence Trainer runtime.

This module intentionally keeps the repeating drill scheduler separate from the
mailbox sequence executor.  A round is always:

    WAIT_TARGET_IDLE -> COUNTDOWN -> OFFER/ACTIVE sequence -> TARGET_IDLE -> ...

Mailbox offers are persistent only up to the configured stuck-step timeout.
If a step cannot be accepted in time, the trainer clears only its own request,
aborts the desynced repetition, and waits read-only for native idle (0x001).
Recovery never forces Idle through +0x200; hard knockdown / All Guard can leave
a forced recovery request resident and poison later rounds.  Outside an active
sequence, the only automatic scheduling signal is the selected mailbox target's
native idle action (0x001).
Leaving idle cancels any pending countdown and the next idle starts a fresh one.
"""

import json
import os
import random
import re
import struct
import time
from typing import Any

try:
    from tvcgui.core.paths import user_data_path
except Exception:
    def user_data_path(*parts: str) -> str:
        return os.path.join(os.path.expanduser("~"), "TvCGUI", *parts)

try:
    from tvcgui.platform.dolphin import addr_in_ram, rd32, wd32
except Exception:
    addr_in_ram = None
    rd32 = None
    wd32 = None

PUNISH_TRAINER_CONFIG_FILE = user_data_path("training", "punish_trainer.json")

PUNISH_TARGET_SLOT_PTRS = {
    "P1-C1": 0x803C9FCC,
    "P2-C1": 0x803C9FD4,
    "P1-C2": 0x803C9FDC,
    "P2-C2": 0x803C9FE4,
}

OFF_ACTION_FRAME = 0x1D8
OFF_ACTION_ID = 0x1E8
OFF_ACTION_REQUEST = 0x200
OFF_BLOCKSTUN_REMAINING = 0x1204
OFF_HITSTUN_REMAINING = 0x1210
OFF_UNTECH_REMAINING = 0x1220

PUNISH_DEFAULT_SLOT = "P2-C1"
PUNISH_DEFAULT_COOLDOWN_SEC = 3.0
PUNISH_DEFAULT_RANDOM_MIN_SEC = 5.0
PUNISH_DEFAULT_RANDOM_MAX_SEC = 10.0
PUNISH_DEFAULT_MOVE_LABEL = "5A [0x100]"
PUNISH_DEFAULT_ACTION_ID = 0x100
PUNISH_DEFAULT_STEP_TIMEOUT_FRAMES = 60

PUNISH_JUMP_ACTION_ID = 0x013
PUNISH_JUMP_STATE_IDS = frozenset({0x013, 0x014, 0x015, 0x016})
PUNISH_JUMP_LABEL = "Jump"
PUNISH_SUPER_JUMP_ACTION_ID = 0x01F
PUNISH_SUPER_JUMP_STATE_IDS = frozenset({0x01C, 0x01D, 0x01F})
PUNISH_SUPER_JUMP_LABEL = "Super Jump"
PUNISH_IDLE_ACTION_ID = 0x001

TRANSITION_CANCEL = "cancel"
TRANSITION_NEUTRAL = "neutral"
TRANSITION_DELAY = "delay"
TRANSITION_RANDOM_DELAY = "random_delay"
TRANSITIONS = {TRANSITION_CANCEL, TRANSITION_NEUTRAL, TRANSITION_DELAY, TRANSITION_RANDOM_DELAY}

PHASE_OFF = "off"
PHASE_WAIT_TARGET = "wait_target"
PHASE_WAIT_READY = "wait_ready"
PHASE_COUNTDOWN = "countdown"
PHASE_OFFER = "offer"
PHASE_ACTIVE = "active"
PHASE_RECOVER_IDLE = "recover_idle"

PUNISH_LABEL_ACTIONS = {
    "JUMP": PUNISH_JUMP_ACTION_ID,
    "SUPERJUMP": PUNISH_SUPER_JUMP_ACTION_ID,
    "5A": 0x100,
    "5B": 0x101,
    "5C": 0x102,
    "2A": 0x103,
    "2B": 0x104,
    "2C": 0x105,
    "6C": 0x106,
    "3C": 0x108,
    "j.A": 0x109,
    "j.B": 0x10A,
    "j.C": 0x10B,
    "6B": 0x10E,
}

_RUNTIME_KEYS = {
    "phase", "target_base", "next_fire_at", "scheduled_cooldown_sec",
    "run_mode", "runtime_sequence", "runtime_sequence_name", "step_index",
    "pending_step_index", "pending_allow_busy", "pending_origin_action",
    "pending_origin_action_frame", "pending_offer_marker", "pending_started_at",
    "step_start_marker", "step_start_action_frame", "step_delay_frames", "last_request_addr",
    "last_mailbox_value", "last_seen_action", "last_seen_action_frame",
    "last_status", "last_status_at", "last_trigger", "trigger_count",
    "countdown_flash_until", "manual_only", "manual_test_requested",
    "runtime_reset_requested", "tick_serial", "cycle_serial", "punisher_engaged",
    "recovery_reason",
}


def _now() -> float:
    return time.monotonic()


def _runtime_now(value: float | None = None) -> float:
    """Normalize host timestamps to monotonic seconds.

    Some host paths pass Unix epoch seconds and others pass monotonic seconds.
    The trainer stores all deadlines in the monotonic domain.
    """
    mono = _now()
    if value is None:
        return mono
    try:
        candidate = float(value)
    except Exception:
        return mono
    if candidate != candidate or candidate in (float("inf"), float("-inf")):
        return mono
    if candidate >= 1_000_000_000.0:
        try:
            return mono + (candidate - time.time())
        except Exception:
            return mono
    return candidate


def _read_u32(addr: int, default: int = 0) -> int:
    if rd32 is None:
        return int(default) & 0xFFFFFFFF
    try:
        value = rd32(int(addr))
    except Exception:
        value = None
    if value is None:
        return int(default) & 0xFFFFFFFF
    return int(value) & 0xFFFFFFFF


def _write_u32(addr: int, value: int) -> bool:
    if wd32 is None:
        return False
    try:
        result = wd32(int(addr), int(value) & 0xFFFFFFFF)
        return result is not False
    except Exception:
        return False


def _valid_base(base: int) -> bool:
    base = int(base or 0)
    if not base:
        return False
    if addr_in_ram is not None:
        try:
            return bool(addr_in_ram(base))
        except Exception:
            pass
    return 0x90000000 <= base < 0x94000000


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        value = float(value)
    except Exception:
        value = default
    return round(max(low, min(high, value)), 2)


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        value = int(value)
    except Exception:
        value = default
    return max(low, min(high, value))


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())[:128]


def _mailbox_value_for_action(action_id: int) -> int:
    return (int(action_id) - 0x4000) & 0xFFFFFFFF


def _read_action_frame(base: int) -> int:
    raw = _read_u32(int(base) + OFF_ACTION_FRAME, 0)
    try:
        value = struct.unpack(">f", struct.pack(">I", raw & 0xFFFFFFFF))[0]
        if 0.0 <= value < 100000.0:
            return max(0, int(value))
    except Exception:
        pass
    return 0


def parse_action_id(value: Any, fallback: int | None = None) -> int | None:
    if value is None:
        return fallback
    if isinstance(value, int):
        return int(value) & 0xFFFF
    text = _clean_text(value).upper()
    if not text:
        return fallback
    bracket = re.search(r"\[\s*0X([0-9A-F]+)\s*\]", text)
    if bracket:
        return int(bracket.group(1), 16) & 0xFFFF
    direct = re.search(r"0X([0-9A-F]+)", text)
    if direct:
        return int(direct.group(1), 16) & 0xFFFF
    compact = text.replace(" ", "")
    if compact in PUNISH_LABEL_ACTIONS:
        return PUNISH_LABEL_ACTIONS[compact]
    try:
        return int(compact, 10) & 0xFFFF
    except Exception:
        return fallback


def _normalize_slot(value: Any) -> str:
    text = str(value or PUNISH_DEFAULT_SLOT).strip().upper()
    aliases = {
        "P1": "P1-C1", "P2": "P2-C1", "1": "P1-C1", "2": "P2-C1",
        "P1C1": "P1-C1", "P1C2": "P1-C2", "P2C1": "P2-C1", "P2C2": "P2-C2",
    }
    text = aliases.get(text, text)
    return text if text in PUNISH_TARGET_SLOT_PTRS else PUNISH_DEFAULT_SLOT


def _normalize_transition(value: Any) -> str:
    text = str(value or TRANSITION_CANCEL).strip().lower().replace(" ", "_")
    aliases = {
        "cancel_asap": TRANSITION_CANCEL,
        "cancel": TRANSITION_CANCEL,
        "neutral": TRANSITION_NEUTRAL,
        "wait_for_neutral": TRANSITION_NEUTRAL,
        "delay": TRANSITION_DELAY,
        "random": TRANSITION_RANDOM_DELAY,
        "random_delay": TRANSITION_RANDOM_DELAY,
    }
    text = aliases.get(text, text)
    return text if text in TRANSITIONS else TRANSITION_CANCEL


def _normalize_step(raw: Any, fallback_label: str = "", fallback_action: int = 0) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    label = _clean_text(raw.get("move_label") or raw.get("label") or raw.get("display") or fallback_label)
    action_id = parse_action_id(raw.get("action_id"), None)
    if action_id is None:
        action_id = parse_action_id(label, fallback_action or None)
    if not action_id:
        return None
    delay = _clamp_int(raw.get("delay_frames", raw.get("delay", 0)), 0, 0, 180)
    random_min = _clamp_int(raw.get("random_min_frames", raw.get("delay_min_frames", 1)), 1, 0, 180)
    random_max = _clamp_int(raw.get("random_max_frames", raw.get("delay_max_frames", max(1, random_min))), max(1, random_min), 0, 180)
    if random_min > random_max:
        random_min, random_max = random_max, random_min
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in {"jump", "super_jump", "move"}:
        folded = label.casefold()
        if int(action_id) == PUNISH_SUPER_JUMP_ACTION_ID and folded.startswith("super jump"):
            kind = "super_jump"
        elif int(action_id) == PUNISH_JUMP_ACTION_ID and folded.startswith("jump"):
            kind = "jump"
        else:
            kind = "move"
    return {
        "move_label": label or f"0x{int(action_id):03X}",
        "action_id": int(action_id) & 0xFFFF,
        "kind": kind,
        "transition": _normalize_transition(raw.get("transition")),
        "delay_frames": delay,
        "random_min_frames": random_min,
        "random_max_frames": random_max,
    }


def _normalize_sequence(raw: Any, *, fallback_label: str = "", fallback_action: int = 0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw[:64]:
            step = _normalize_step(item)
            if step is not None:
                out.append(step)
    if not out and fallback_action:
        step = _normalize_step({"move_label": fallback_label, "action_id": fallback_action, "transition": TRANSITION_CANCEL})
        if step is not None:
            out.append(step)
    return out


def _normalize_presets(raw: Any) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(raw, dict):
        return out
    for raw_name, raw_sequence in list(raw.items())[:64]:
        name = _clean_text(raw_name)[:48]
        if not name:
            continue
        sequence = _normalize_sequence(raw_sequence)
        if sequence:
            out[name] = sequence
    return out


def _migrate_legacy_jump_delay(sequence: list[dict[str, Any]]) -> None:
    """Remove the retired auto-added 8F jump delay from older configs.

    This migration runs once per saved config.  After that, users remain free to
    assign an 8F delay manually if they actually want one.
    """
    for step in sequence:
        if not isinstance(step, dict):
            continue
        kind = str(step.get("kind") or "").strip().lower()
        if kind not in {"jump", "super_jump"}:
            continue
        if _normalize_transition(step.get("transition")) != TRANSITION_DELAY:
            continue
        try:
            delay = int(step.get("delay_frames") or 0)
        except Exception:
            delay = 0
        if delay == 8:
            step["transition"] = TRANSITION_CANCEL
            step["delay_frames"] = 0


def normalize_punish_trainer_state(state: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(state or {})
    slot = _normalize_slot(raw.get("target_slot") or raw.get("opponent_team") or raw.get("target_side"))
    explicit_sequence = "sequence" in raw
    move_label = _clean_text(raw.get("move_label") or raw.get("response_label") or ("" if explicit_sequence else PUNISH_DEFAULT_MOVE_LABEL))
    action_id = parse_action_id(raw.get("action_id"), None)
    if action_id is None:
        action_id = parse_action_id(move_label, None if explicit_sequence else PUNISH_DEFAULT_ACTION_ID)

    sequence = _normalize_sequence(
        raw.get("sequence"),
        fallback_label=move_label if not explicit_sequence else "",
        fallback_action=int(action_id or 0) if not explicit_sequence else 0,
    )
    presets = _normalize_presets(raw.get("presets"))
    legacy_jump_delay_migrated = bool(raw.get("legacy_jump_delay_migrated", False))
    if not legacy_jump_delay_migrated:
        _migrate_legacy_jump_delay(sequence)
        for preset_sequence in presets.values():
            _migrate_legacy_jump_delay(preset_sequence)
        legacy_jump_delay_migrated = True

    if sequence:
        move_label = sequence[0]["move_label"]
        action_id = sequence[0]["action_id"]
    elif explicit_sequence:
        move_label = ""
        action_id = 0

    cooldown_sec = _clamp_float(raw.get("cooldown_sec", raw.get("interval_sec")), PUNISH_DEFAULT_COOLDOWN_SEC, 0.25, 30.0)
    random_min_sec = _clamp_float(raw.get("random_min_sec", raw.get("interval_min_sec")), PUNISH_DEFAULT_RANDOM_MIN_SEC, 0.25, 30.0)
    random_max_sec = _clamp_float(raw.get("random_max_sec", raw.get("interval_max_sec")), PUNISH_DEFAULT_RANDOM_MAX_SEC, 0.25, 30.0)
    if random_min_sec > random_max_sec:
        random_min_sec, random_max_sec = random_max_sec, random_min_sec

    out: dict[str, Any] = {
        "enabled": bool(raw.get("enabled", False)),
        "target_slot": slot,
        "move_label": move_label,
        "action_id": int(action_id or 0) & 0xFFFF,
        "sequence": sequence,
        "presets": presets,
        "legacy_jump_delay_migrated": legacy_jump_delay_migrated,
        "randomize_presets": bool(raw.get("randomize_presets", False)),
        # Retained for config compatibility only. Automatic repeats now always
        # use one hard rule: the selected mailbox target must be in native
        # idle (action 0x001) for a countdown to exist.
        "require_neutral": True,
        "cooldown_sec": cooldown_sec,
        "random_interval": bool(raw.get("random_interval", False)),
        "random_min_sec": random_min_sec,
        "random_max_sec": random_max_sec,
        "show_countdown": bool(raw.get("show_countdown", False)),
        "auto_add_jump": bool(raw.get("auto_add_jump", True)),
        "step_timeout_frames": _clamp_int(
            raw.get("step_timeout_frames", PUNISH_DEFAULT_STEP_TIMEOUT_FRAMES),
            PUNISH_DEFAULT_STEP_TIMEOUT_FRAMES, 15, 300,
        ),
        "last_transition": _normalize_transition(raw.get("last_transition", TRANSITION_CANCEL)),
        "last_delay_frames": _clamp_int(raw.get("last_delay_frames", 0), 0, 0, 180),
        "last_random_min_frames": _clamp_int(raw.get("last_random_min_frames", 1), 1, 0, 180),
        "last_random_max_frames": _clamp_int(raw.get("last_random_max_frames", 4), 4, 0, 180),
        "manual_test_requested": bool(raw.get("manual_test_requested", False)),
        "runtime_reset_requested": bool(raw.get("runtime_reset_requested", False)),
    }
    if out["last_random_min_frames"] > out["last_random_max_frames"]:
        out["last_random_min_frames"], out["last_random_max_frames"] = out["last_random_max_frames"], out["last_random_min_frames"]

    # Preserve only the new runtime fields.  Old state-machine fields from
    # previous builds are intentionally ignored on normalization.
    for key in _RUNTIME_KEYS:
        if key in raw:
            out[key] = raw[key]
    out.setdefault("phase", PHASE_OFF)
    out.setdefault("target_base", 0)
    out.setdefault("next_fire_at", 0.0)
    out.setdefault("scheduled_cooldown_sec", 0.0)
    out.setdefault("run_mode", "")
    out.setdefault("runtime_sequence", [])
    out.setdefault("runtime_sequence_name", "")
    out.setdefault("step_index", -1)
    out.setdefault("pending_step_index", -1)
    out.setdefault("pending_allow_busy", False)
    out.setdefault("pending_origin_action", -1)
    out.setdefault("pending_origin_action_frame", 0)
    out.setdefault("pending_offer_marker", 0)
    out.setdefault("pending_started_at", 0.0)
    out.setdefault("step_start_marker", 0)
    out.setdefault("step_start_action_frame", 0)
    out.setdefault("step_delay_frames", 0)
    out.setdefault("last_request_addr", 0)
    out.setdefault("last_mailbox_value", 0)
    out.setdefault("last_seen_action", 0)
    out.setdefault("last_seen_action_frame", 0)
    out.setdefault("last_status", "Punish Trainer off.")
    out.setdefault("last_status_at", 0.0)
    out.setdefault("last_trigger", None)
    out.setdefault("trigger_count", 0)
    out.setdefault("countdown_flash_until", 0.0)
    out.setdefault("manual_only", False)
    out.setdefault("tick_serial", 0)
    out.setdefault("cycle_serial", 0)
    out.setdefault("punisher_engaged", False)
    out.setdefault("recovery_reason", "")
    return out


def load_punish_trainer_config() -> dict[str, Any]:
    loaded: dict[str, Any] = {}
    try:
        with open(PUNISH_TRAINER_CONFIG_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            loaded = data
    except Exception:
        pass
    state = normalize_punish_trainer_state(loaded)
    state["enabled"] = False
    _reset_runtime_fields(state, keep_status=False)
    return state


def save_punish_trainer_config(state: dict[str, Any]) -> None:
    clean = normalize_punish_trainer_state(state)
    for key in _RUNTIME_KEYS:
        clean.pop(key, None)
    clean.pop("manual_test_requested", None)
    clean.pop("runtime_reset_requested", None)
    clean["enabled"] = False
    try:
        os.makedirs(os.path.dirname(PUNISH_TRAINER_CONFIG_FILE), exist_ok=True)
        with open(PUNISH_TRAINER_CONFIG_FILE, "w", encoding="utf-8") as handle:
            json.dump(clean, handle, indent=2, sort_keys=True)
    except Exception as exc:
        print(f"[punish trainer] config save failed: {exc!r}", flush=True)


def _set_status(state: dict[str, Any], text: str, now: float | None = None) -> None:
    state["last_status"] = _clean_text(text)[:180]
    state["last_status_at"] = float(_now() if now is None else now)


def _snap_base(snap: Any) -> int:
    if not isinstance(snap, dict):
        return 0
    for key in ("base", "fighter_base", "ea"):
        try:
            base = int(snap.get(key) or 0)
        except Exception:
            base = 0
        if _valid_base(base):
            return base
    return 0


def punish_target_base(target_slot: str, snaps: dict | None = None) -> int:
    slot = _normalize_slot(target_slot)
    base = _snap_base((snaps or {}).get(slot))
    if base:
        return base
    ptr = PUNISH_TARGET_SLOT_PTRS[slot]
    base = _read_u32(ptr, 0)
    return base if _valid_base(base) else 0


def _base_ready(base: int) -> bool:
    """True when this fighter base is in a clean controllable state."""
    if not _valid_base(base):
        return False
    action = _read_u32(base + OFF_ACTION_ID, 0) & 0x7FFF
    # Attacks/specials live at 0x100+.  Passive/control states are below that
    # boundary.  Exclude the known normal/super-jump families so the optional
    # neutral gate still waits for landing, while avoiding the old brittle
    # assumption that every controllable state must be below 0x13.
    if action >= 0x100:
        return False
    if action in PUNISH_JUMP_STATE_IDS or action in PUNISH_SUPER_JUMP_STATE_IDS:
        return False
    if _read_u32(base + OFF_BLOCKSTUN_REMAINING, 0) > 0:
        return False
    if _read_u32(base + OFF_HITSTUN_REMAINING, 0) > 0:
        return False
    if _read_u32(base + OFF_UNTECH_REMAINING, 0) > 0:
        return False
    return True


def _slot_ready(slot: str, snaps: dict | None = None) -> bool:
    """True when the selected dummy can cleanly accept a scripted action."""
    return _base_ready(punish_target_base(slot, snaps))


def _target_idle(slot: str, snaps: dict | None = None) -> bool:
    """True only when the selected mailbox target is in native idle 0x001.

    This is the sole automatic round/re-arm gate.  Do not consult the other
    fighter, hitstun, blockstun, untech, prior punish outcome, or action
    history here.
    """
    base = punish_target_base(_normalize_slot(slot), snaps)
    if not _valid_base(base):
        return False
    return (_read_u32(base + OFF_ACTION_ID, 0) & 0x7FFF) == PUNISH_IDLE_ACTION_ID


def _punisher_slot_for_target(target_slot: str) -> str:
    """Return the active opposing fighter that performs the punish.

    The trainer may script either point/partner slot, but the human-controlled
    punisher is the opposing active C1 fighter.  Re-arm intentionally depends
    only on that fighter's native idle action, never on the dummy's reaction
    timers or hit/block history.
    """
    slot = _normalize_slot(target_slot)
    return "P1-C1" if slot.startswith("P2-") else "P2-C1"


def punish_punisher_base(target_slot: str, snaps: dict | None = None) -> int:
    return punish_target_base(_punisher_slot_for_target(target_slot), snaps)


def _punisher_idle(target_slot: str, snaps: dict | None = None) -> bool:
    """True only when the punisher is in the game's native idle action.

    Do not consult hitstun, blockstun, untech, or the dummy at all here.
    Those values were the source of repeat drills getting stranded after the
    player successfully punished or blocked the scripted move.
    """
    base = punish_punisher_base(target_slot, snaps)
    if not _valid_base(base):
        return False
    return (_read_u32(base + OFF_ACTION_ID, 0) & 0x7FFF) == PUNISH_IDLE_ACTION_ID


def _clear_our_request(state: dict[str, Any]) -> None:
    addr = int(state.get("last_request_addr") or 0)
    value = int(state.get("last_mailbox_value") or 0) & 0xFFFFFFFF
    if addr and value and _read_u32(addr, 0) == value:
        _write_u32(addr, 0)
    state["last_request_addr"] = 0
    state["last_mailbox_value"] = 0


def _reset_runtime_fields(state: dict[str, Any], *, keep_status: bool = True) -> None:
    _clear_our_request(state)
    state.update({
        "phase": PHASE_OFF,
        "target_base": 0,
        "next_fire_at": 0.0,
        "scheduled_cooldown_sec": 0.0,
        "run_mode": "",
        "runtime_sequence": [],
        "runtime_sequence_name": "",
        "step_index": -1,
        "pending_step_index": -1,
        "pending_allow_busy": False,
        "pending_origin_action": -1,
        "pending_origin_action_frame": 0,
        "pending_offer_marker": 0,
        "pending_started_at": 0.0,
        "step_start_marker": 0,
        "step_start_action_frame": 0,
        "step_delay_frames": 0,
        "last_request_addr": 0,
        "last_mailbox_value": 0,
        "last_seen_action": 0,
        "last_seen_action_frame": 0,
        "countdown_flash_until": 0.0,
        "manual_only": False,
        "tick_serial": 0,
        "punisher_engaged": False,
        "recovery_reason": "",
    })
    if not keep_status:
        state["last_status"] = "Punish Trainer off."
        state["last_status_at"] = 0.0


def clear_punish_trainer_runtime(state: dict[str, Any] | None = None) -> dict[str, Any]:
    target = normalize_punish_trainer_state(state)
    _reset_runtime_fields(target, keep_status=False)
    target["manual_test_requested"] = False
    target["runtime_reset_requested"] = False
    if isinstance(state, dict):
        state.clear()
        state.update(target)
        return state
    return target


def _selected_interval(state: dict[str, Any]) -> float:
    if bool(state.get("random_interval", False)):
        low = float(state.get("random_min_sec") or PUNISH_DEFAULT_RANDOM_MIN_SEC)
        high = float(state.get("random_max_sec") or PUNISH_DEFAULT_RANDOM_MAX_SEC)
        if low > high:
            low, high = high, low
        return round(random.uniform(low, high), 2)
    return float(state.get("cooldown_sec") or PUNISH_DEFAULT_COOLDOWN_SEC)


def _sequence_label(state: dict[str, Any]) -> str:
    name = _clean_text(state.get("runtime_sequence_name"))
    if name:
        return name
    seq = state.get("runtime_sequence") or state.get("sequence") or []
    if isinstance(seq, list) and seq:
        labels = [_clean_text(step.get("move_label")) for step in seq[:4] if isinstance(step, dict)]
        return " > ".join(labels) + (" ..." if len(seq) > 4 else "")
    return _clean_text(state.get("move_label"))


def _choose_runtime_sequence(state: dict[str, Any]) -> list[dict[str, Any]]:
    presets = state.get("presets") if isinstance(state.get("presets"), dict) else {}
    if bool(state.get("randomize_presets", False)) and presets:
        names = [name for name, seq in presets.items() if isinstance(seq, list) and seq]
        if names:
            name = random.choice(names)
            seq = _normalize_sequence(presets.get(name))
            state["runtime_sequence_name"] = name
            state["runtime_sequence"] = seq
            return seq
    seq = _normalize_sequence(state.get("sequence"), fallback_label=state.get("move_label", ""), fallback_action=int(state.get("action_id") or 0))
    state["runtime_sequence_name"] = ""
    state["runtime_sequence"] = seq
    return seq


def _jump_state_ids_for_step(step: dict[str, Any] | None) -> frozenset[int]:
    if not isinstance(step, dict):
        return frozenset()
    kind = str(step.get("kind") or "").strip().lower()
    try:
        action_id = int(step.get("action_id") or 0) & 0xFFFF
    except Exception:
        action_id = 0
    label = _clean_text(step.get("move_label")).casefold()
    if kind == "super_jump" or action_id == PUNISH_SUPER_JUMP_ACTION_ID or label.startswith("super jump"):
        return PUNISH_SUPER_JUMP_STATE_IDS
    if kind == "jump" or action_id == PUNISH_JUMP_ACTION_ID or label.startswith("jump"):
        return PUNISH_JUMP_STATE_IDS
    return frozenset()


def _step_matches_action(step: dict[str, Any], current_action: int) -> bool:
    current = int(current_action) & 0x7FFF
    jump_states = _jump_state_ids_for_step(step)
    if jump_states:
        return current in jump_states
    try:
        return current == (int(step.get("action_id") or 0) & 0xFFFF)
    except Exception:
        return False


def _frame_marker(state: dict[str, Any], frame_idx: int | None, now_f: float) -> int:
    if frame_idx is not None:
        try:
            return int(frame_idx)
        except Exception:
            pass
    # Fallback is a real-time 60 fps marker, not GUI tick count.  That keeps
    # frame delays stable even if the host polls faster/slower than 60 Hz.
    return int(now_f * 60.0)


def _arm_step_delay(state: dict[str, Any], step: dict[str, Any]) -> int:
    transition = _normalize_transition(step.get("transition"))
    if transition == TRANSITION_RANDOM_DELAY:
        low = _clamp_int(step.get("random_min_frames"), 1, 0, 180)
        high = _clamp_int(step.get("random_max_frames"), max(1, low), 0, 180)
        if low > high:
            low, high = high, low
        value = random.randint(low, high)
    else:
        value = _clamp_int(step.get("delay_frames"), 0, 0, 180)
    state["step_delay_frames"] = value
    return value


def _begin_wait_ready(state: dict[str, Any], now_f: float) -> None:
    """Park automatic mode until the selected mailbox target is idle again."""
    _clear_our_request(state)
    state["phase"] = PHASE_WAIT_READY
    state["next_fire_at"] = 0.0
    state["scheduled_cooldown_sec"] = 0.0
    state["run_mode"] = ""
    state["runtime_sequence"] = []
    state["runtime_sequence_name"] = ""
    state["step_index"] = -1
    state["pending_step_index"] = -1
    state["pending_started_at"] = 0.0
    state["recovery_reason"] = ""
    target = _normalize_slot(state.get("target_slot", PUNISH_DEFAULT_SLOT))
    _set_status(state, f"Waiting for target {target} native idle (0x001).", now_f)


def _begin_countdown(state: dict[str, Any], now_f: float) -> None:
    duration = max(0.05, float(_selected_interval(state)))
    state["phase"] = PHASE_COUNTDOWN
    state["scheduled_cooldown_sec"] = duration
    state["next_fire_at"] = now_f + duration
    state["cycle_serial"] = int(state.get("cycle_serial") or 0) + 1
    state["countdown_flash_until"] = 0.0
    label = _sequence_label(state) or "sequence"
    _set_status(state, f"Round {state['cycle_serial']}: {label} in {duration:g}s.", now_f)


def _begin_run(state: dict[str, Any], now_f: float, mode: str) -> bool:
    _clear_our_request(state)
    seq = _choose_runtime_sequence(state)
    if not seq:
        state["run_mode"] = ""
        state["phase"] = PHASE_OFF if mode == "manual" and not state.get("enabled") else PHASE_WAIT_READY
        _set_status(state, "Add at least one move to the sequence.", now_f)
        return False
    state["run_mode"] = mode
    state["manual_only"] = mode == "manual"
    state["step_index"] = -1
    state["pending_step_index"] = 0
    state["pending_allow_busy"] = False
    state["pending_origin_action"] = -1
    state["pending_origin_action_frame"] = 0
    state["pending_offer_marker"] = 0
    state["pending_started_at"] = now_f
    state["recovery_reason"] = ""
    state["phase"] = PHASE_OFFER
    state["next_fire_at"] = 0.0
    state["scheduled_cooldown_sec"] = 0.0
    state["punisher_engaged"] = False
    _set_status(state, f"Queued {_sequence_label(state)}. Waiting for native acceptance.", now_f)
    return True


def _complete_run(state: dict[str, Any], now_f: float, snaps: dict | None) -> None:
    label = _sequence_label(state) or "sequence"
    mode = str(state.get("run_mode") or "auto")
    _clear_our_request(state)
    state["runtime_sequence"] = []
    state["runtime_sequence_name"] = ""
    state["step_index"] = -1
    state["pending_step_index"] = -1
    state["pending_allow_busy"] = False
    state["pending_started_at"] = 0.0
    state["recovery_reason"] = ""
    state["run_mode"] = ""
    state["manual_only"] = False

    if bool(state.get("enabled", False)):
        # A finished automatic sequence has one and only one re-arm gate: the
        # selected mailbox target must be back in native idle (0x001).
        target_slot = state.get("target_slot", PUNISH_DEFAULT_SLOT)
        if _target_idle(target_slot, snaps):
            _begin_countdown(state, now_f)
        else:
            _begin_wait_ready(state, now_f)
        return

    state["phase"] = PHASE_OFF
    state["next_fire_at"] = 0.0
    state["scheduled_cooldown_sec"] = 0.0
    if mode == "manual":
        _set_status(state, f"Perform Once complete: {label}.", now_f)
    else:
        _set_status(state, "Punish Trainer off.", now_f)


def _queue_next_step(state: dict[str, Any], next_index: int, allow_busy: bool, now_f: float) -> None:
    _clear_our_request(state)
    state["pending_step_index"] = int(next_index)
    state["pending_allow_busy"] = bool(allow_busy)
    state["pending_origin_action"] = -1
    state["pending_origin_action_frame"] = 0
    state["pending_offer_marker"] = 0
    state["pending_started_at"] = now_f
    state["phase"] = PHASE_OFFER
    seq = state.get("runtime_sequence") if isinstance(state.get("runtime_sequence"), list) else []
    if 0 <= next_index < len(seq):
        _set_status(state, f"Offering step {next_index + 1}/{len(seq)}: {seq[next_index].get('move_label', '')}.", now_f)


def _begin_recover_idle(state: dict[str, Any], base: int, now_f: float, reason: str) -> None:
    """Abort a desynced repetition and wait for *natural* target idle.

    Recovery is deliberately read-only after clearing the request that belongs
    to this trainer.  Do NOT request Idle through +0x200 here.  During hard
    knockdown / wake-up / All Guard, an Idle mailbox request can remain resident
    and prevent subsequent scripted requests until another hit happens to shake
    the native state machine loose.
    """
    _clear_our_request(state)
    state["phase"] = PHASE_RECOVER_IDLE
    state["runtime_sequence"] = []
    state["runtime_sequence_name"] = ""
    state["step_index"] = -1
    state["pending_step_index"] = -1
    state["pending_allow_busy"] = False
    state["pending_origin_action"] = -1
    state["pending_origin_action_frame"] = 0
    state["pending_offer_marker"] = 0
    state["pending_started_at"] = 0.0
    state["next_fire_at"] = 0.0
    state["scheduled_cooldown_sec"] = 0.0
    state["recovery_reason"] = _clean_text(reason)
    state["run_mode"] = "recover"
    _set_status(state, f"{reason} Rep aborted; waiting for target natural idle.", now_f)


def _service_recover_idle(state: dict[str, Any], base: int, current_action: int, now_f: float) -> None:
    """Read-only recovery gate after a timed-out/desynced sequence."""
    if (int(current_action) & 0x7FFF) != PUNISH_IDLE_ACTION_ID:
        return

    # The target recovered under the game's own state machine.  No hit/block
    # event or mailbox acknowledgement is required to re-arm the trainer.
    _clear_our_request(state)
    state["run_mode"] = ""
    state["recovery_reason"] = ""
    if bool(state.get("enabled", False)):
        _begin_countdown(state, now_f)
    else:
        state["phase"] = PHASE_OFF
        _set_status(state, "Recovery complete; Punish Trainer off.", now_f)


def _service_offer(state: dict[str, Any], base: int, current_action: int, current_action_frame: int, now_f: float, marker: int) -> None:
    seq = state.get("runtime_sequence") if isinstance(state.get("runtime_sequence"), list) else []
    idx = int(state.get("pending_step_index", -1))
    if idx < 0 or idx >= len(seq):
        # Corrupt/transient run state: restart the round rather than dying.
        if state.get("enabled"):
            _begin_wait_ready(state, now_f)
        else:
            state["phase"] = PHASE_OFF
        return

    step = seq[idx]

    timeout_frames = _clamp_int(
        state.get("step_timeout_frames", PUNISH_DEFAULT_STEP_TIMEOUT_FRAMES),
        PUNISH_DEFAULT_STEP_TIMEOUT_FRAMES, 15, 300,
    )
    pending_started = float(state.get("pending_started_at") or 0.0)
    if pending_started <= 0.0:
        pending_started = now_f
        state["pending_started_at"] = pending_started
    elapsed_offer_frames = max(0.0, (now_f - pending_started) * 60.0)
    if elapsed_offer_frames >= float(timeout_frames):
        label = _clean_text(step.get("move_label")) or f"step {idx + 1}"
        _begin_recover_idle(
            state, base, now_f,
            f"Step {idx + 1}/{len(seq)} {label} timed out after {timeout_frames}F.",
        )
        return

    matches_current = _step_matches_action(step, current_action)
    origin_action = int(state.get("pending_origin_action", -1))
    origin_frame = int(state.get("pending_origin_action_frame", 0) or 0)
    offered_marker = int(state.get("pending_offer_marker", 0) or 0)

    # A different native action matching the requested step is unambiguous
    # acceptance.  For back-to-back identical actions, do not mistake the old
    # action for the new one: wait for a frame reset or native consumption of
    # our mailbox request.
    # Do not call a step "accepted" until this trainer has actually offered
    # it.  This also prevents an immediate false-accept when two adjacent
    # sequence rows use the same action ID.
    accepted = bool(matches_current and origin_action >= 0)
    if accepted and (int(current_action) & 0x7FFF) == (origin_action & 0x7FFF):
        request_addr = base + OFF_ACTION_REQUEST
        try:
            desired_probe = _mailbox_value_for_action(int(step.get("action_id") or 0))
        except Exception:
            desired_probe = 0
        mailbox_probe = _read_u32(request_addr, 0)
        frame_restarted = marker > offered_marker and int(current_action_frame) < origin_frame
        request_consumed = marker > offered_marker and mailbox_probe != desired_probe
        accepted = bool(frame_restarted or request_consumed)

    if accepted:
        # Native action entry is the acknowledgement.  Once it happens, our
        # old request has done its job and must be removed immediately.
        # Leaving an accepted value in +0x200 serialized the sequence because
        # the next step could not be offered until the previous move ended.
        _clear_our_request(state)

        state["phase"] = PHASE_ACTIVE
        state["step_index"] = idx
        state["pending_step_index"] = -1
        state["pending_started_at"] = 0.0
        state["step_start_marker"] = marker
        state["step_start_action_frame"] = current_action_frame
        _arm_step_delay(state, step)
        if idx == 0:
            state["trigger_count"] = int(state.get("trigger_count") or 0) + 1
            state["countdown_flash_until"] = now_f + 0.45
            state["last_trigger"] = {
                "time": now_f,
                "slot": state.get("target_slot"),
                "move": step.get("move_label", ""),
                "action_id": int(step.get("action_id") or 0),
                "step": idx,
            }

        # If this row says Cancel ASAP (or a literal 0F delay), get the next
        # mailbox offer resident on the same observed acceptance frame.  The
        # game then decides the first legal cancel frame.
        if idx < len(seq) - 1:
            transition = _normalize_transition(step.get("transition"))
            immediate = transition == TRANSITION_CANCEL
            if transition == TRANSITION_DELAY and int(state.get("step_delay_frames") or 0) <= 0:
                immediate = True
            if immediate:
                next_idx = idx + 1
                _queue_next_step(state, next_idx, True, now_f)
                _service_offer(state, base, current_action, current_action_frame, now_f, marker)
                return

        _set_status(state, f"Step {idx + 1}/{len(seq)} accepted: {step.get('move_label', '')}.", now_f)
        return

    allow_busy = bool(state.get("pending_allow_busy", False))
    if not allow_busy and not _base_ready(base):
        _set_status(state, f"Waiting for {state.get('target_slot')} to regain control.", now_f)
        return

    request_addr = base + OFF_ACTION_REQUEST
    mailbox = _read_u32(request_addr, 0)
    desired = _mailbox_value_for_action(int(step.get("action_id") or 0))

    # Never overwrite another system/game request.  If our own offer is still
    # resident, leave it alone indefinitely until native logic consumes it.
    if mailbox != 0:
        if mailbox == desired:
            state["last_request_addr"] = request_addr
            state["last_mailbox_value"] = desired
            return
        _set_status(state, f"Waiting for {state.get('target_slot')} action mailbox to clear.", now_f)
        return

    if _write_u32(request_addr, desired):
        state["last_request_addr"] = request_addr
        state["last_mailbox_value"] = desired
        if int(state.get("pending_origin_action", -1)) < 0:
            state["pending_origin_action"] = int(current_action) & 0x7FFF
            state["pending_origin_action_frame"] = int(current_action_frame)
            state["pending_offer_marker"] = int(marker)
        _set_status(state, f"Offered step {idx + 1}/{len(seq)}: {step.get('move_label', '')}.", now_f)
    else:
        _set_status(state, f"Could not write action mailbox at 0x{request_addr:08X}; retrying.", now_f)


def _service_active(state: dict[str, Any], base: int, current_action: int, current_action_frame: int, now_f: float, marker: int, snaps: dict | None) -> None:
    seq = state.get("runtime_sequence") if isinstance(state.get("runtime_sequence"), list) else []
    idx = int(state.get("step_index", -1))
    if idx < 0 or idx >= len(seq):
        if state.get("enabled"):
            _begin_wait_ready(state, now_f)
        else:
            state["phase"] = PHASE_OFF
        return

    step = seq[idx]
    if idx >= len(seq) - 1:
        # The sequence remains IN PROGRESS after the final mailbox action is
        # accepted.  It ends only when the selected mailbox target itself
        # returns to native idle 0x001.  We intentionally ignore the other
        # fighter and every hit/block/reaction timer.
        if (int(current_action) & 0x7FFF) == PUNISH_IDLE_ACTION_ID:
            _complete_run(state, now_f, snaps)
        return

    transition = _normalize_transition(step.get("transition"))
    next_idx = idx + 1

    if transition == TRANSITION_NEUTRAL:
        if _slot_ready(state.get("target_slot", PUNISH_DEFAULT_SLOT), snaps):
            _queue_next_step(state, next_idx, False, now_f)
        return

    if transition == TRANSITION_CANCEL:
        # Offer immediately and keep it resident.  Native cancel logic decides
        # the first legal frame on which the next action is accepted.
        _queue_next_step(state, next_idx, True, now_f)
        return

    delay_frames = max(0, int(state.get("step_delay_frames") or 0))
    elapsed = max(0, marker - int(state.get("step_start_marker") or marker))
    if elapsed >= delay_frames:
        _queue_next_step(state, next_idx, True, now_f)
    else:
        _set_status(state, f"Step {idx + 1}/{len(seq)}: {step.get('move_label', '')} ({elapsed}/{delay_frames}F).", now_f)


def punish_trainer_overlay_payload(state: dict[str, Any] | None, now: float | None = None) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    now_f = _runtime_now(now)
    phase = str(state.get("phase") or PHASE_OFF)
    remaining = 0.0
    if phase == PHASE_COUNTDOWN:
        remaining = max(0.0, float(state.get("next_fire_at") or 0.0) - now_f)
    flash_until = float(state.get("countdown_flash_until") or 0.0)
    enabled = bool(state.get("enabled", False))
    show = bool(state.get("show_countdown", False))
    visible = show and ((enabled and phase == PHASE_COUNTDOWN) or now_f < flash_until)
    display_phase = "cooldown" if phase == PHASE_COUNTDOWN else ("attack" if now_f < flash_until else phase)
    return {
        "enabled": enabled,
        "show": show,
        "visible": visible,
        "phase": display_phase,
        "remaining": remaining,
        "slot": str(state.get("target_slot") or PUNISH_DEFAULT_SLOT),
        "move_label": _sequence_label(state),
        "sequence_index": int(state.get("step_index", -1)),
        "sequence_length": len(state.get("runtime_sequence") or state.get("sequence") or []),
        "random_interval": bool(state.get("random_interval", False)),
        "scheduled_interval": float(state.get("scheduled_cooldown_sec") or 0.0),
        "flash_until": flash_until,
        "now": now_f,
    }


def tick_punish_trainer(
    state: dict[str, Any] | None,
    snaps: dict | None = None,
    now: float | None = None,
    frame_idx: int | None = None,
) -> dict[str, Any]:
    now_f = _runtime_now(now)
    clean = normalize_punish_trainer_state(state)
    if isinstance(state, dict):
        state.clear()
        state.update(clean)
        clean = state

    clean["tick_serial"] = int(clean.get("tick_serial") or 0) + 1
    marker = _frame_marker(clean, frame_idx, now_f)

    manual_requested = bool(clean.pop("manual_test_requested", False))
    reset_requested = bool(clean.pop("runtime_reset_requested", False))
    if reset_requested:
        enabled = bool(clean.get("enabled", False))
        _reset_runtime_fields(clean, keep_status=True)
        clean["enabled"] = enabled

    slot = _normalize_slot(clean.get("target_slot"))
    clean["target_slot"] = slot
    base = punish_target_base(slot, snaps)
    previous_base = int(clean.get("target_base") or 0)
    if previous_base and base and previous_base != base:
        _clear_our_request(clean)
        clean["phase"] = PHASE_OFF
        clean["runtime_sequence"] = []
        clean["run_mode"] = ""
    clean["target_base"] = base

    if not base:
        _clear_our_request(clean)
        clean["phase"] = PHASE_WAIT_TARGET if (clean.get("enabled") or manual_requested) else PHASE_OFF
        _set_status(clean, f"Waiting for {slot} to appear in a match.", now_f)
        if manual_requested:
            clean["manual_test_requested"] = True  # keep it queued until target exists
        return clean

    current_action = _read_u32(base + OFF_ACTION_ID, 0) & 0x7FFF
    current_action_frame = _read_action_frame(base)
    clean["last_seen_action"] = current_action
    clean["last_seen_action_frame"] = current_action_frame

    # Perform Once always uses the same sequencer as automatic rounds.  It may
    # interrupt a countdown, but never interrupts a sequence already executing.
    if manual_requested and str(clean.get("phase")) not in {PHASE_OFFER, PHASE_ACTIVE}:
        _begin_run(clean, now_f, "manual")

    enabled = bool(clean.get("enabled", False))
    phase = str(clean.get("phase") or PHASE_OFF)

    if not enabled and str(clean.get("run_mode") or "") != "manual":
        _clear_our_request(clean)
        clean["phase"] = PHASE_OFF
        clean["next_fire_at"] = 0.0
        clean["scheduled_cooldown_sec"] = 0.0
        clean["runtime_sequence"] = []
        clean["step_index"] = -1
        clean["pending_step_index"] = -1
        return clean

    phase = str(clean.get("phase") or PHASE_OFF)

    if phase in {PHASE_OFF, PHASE_WAIT_TARGET}:
        if str(clean.get("run_mode") or "") == "manual":
            clean["phase"] = PHASE_OFFER
        elif enabled:
            if current_action == PUNISH_IDLE_ACTION_ID:
                _begin_countdown(clean, now_f)
            else:
                _begin_wait_ready(clean, now_f)
        return clean

    if phase == PHASE_WAIT_READY:
        # There is no history-based re-arm.  The instant the selected mailbox
        # target reaches native idle, create a completely fresh countdown.
        if current_action == PUNISH_IDLE_ACTION_ID:
            _begin_countdown(clean, now_f)
        return clean

    if phase == PHASE_COUNTDOWN:
        # A countdown is valid ONLY while the selected mailbox target remains
        # idle.  If it leaves idle for any reason, discard this countdown.
        # Do not resume it.  Wait for the next idle edge and start from the
        # full configured duration again.
        if current_action != PUNISH_IDLE_ACTION_ID:
            _begin_wait_ready(clean, now_f)
            return clean
        if now_f >= float(clean.get("next_fire_at") or 0.0):
            _begin_run(clean, now_f, "auto")
            _service_offer(clean, base, current_action, current_action_frame, now_f, marker)
        return clean

    if phase == PHASE_RECOVER_IDLE:
        _service_recover_idle(clean, base, current_action, now_f)
        return clean

    if phase == PHASE_OFFER:
        _service_offer(clean, base, current_action, current_action_frame, now_f, marker)
        return clean

    if phase == PHASE_ACTIVE:
        _service_active(clean, base, current_action, current_action_frame, now_f, marker, snaps)
        return clean

    # Unknown state from a hot reload/old build: recover using the same single
    # target-idle rule as every normal automatic round.
    _clear_our_request(clean)
    if enabled:
        if current_action == PUNISH_IDLE_ACTION_ID:
            _begin_countdown(clean, now_f)
        else:
            _begin_wait_ready(clean, now_f)
    else:
        clean["phase"] = PHASE_OFF
    return clean


def punish_trainer_status(state: dict[str, Any] | None, now: float | None = None) -> str:
    if not isinstance(state, dict):
        return ""
    now_f = _runtime_now(now)
    if bool(state.get("enabled", False)) and str(state.get("phase")) == PHASE_COUNTDOWN:
        remaining = max(0.0, float(state.get("next_fire_at") or 0.0) - now_f)
        return f"Punish {state.get('target_slot', PUNISH_DEFAULT_SLOT)} {_sequence_label(state)} in {remaining:.1f}s"
    return _clean_text(state.get("last_status"))


def _move_sort_key(item: tuple[int, str]) -> tuple[int, int, str]:
    action_id, label = item
    if 0x100 <= action_id <= 0x10F:
        group = 0
    elif action_id < 0x180:
        group = 1
    elif action_id < 0x300:
        group = 2
    else:
        group = 3
    return group, action_id, label.casefold()


def punish_roster_context(snaps: dict | None, move_map: dict | None = None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    mapping = move_map if isinstance(move_map, dict) else {}
    for slot in ("P1-C1", "P1-C2", "P2-C1", "P2-C2"):
        snap = (snaps or {}).get(slot) or {}
        base = _snap_base(snap) or punish_target_base(slot, snaps)
        name = _clean_text(snap.get("name") if isinstance(snap, dict) else "")
        try:
            char_id = int((snap or {}).get("csv_char_id"))
        except Exception:
            char_id = -1
        bucket = mapping.get(char_id, {}) if char_id >= 0 else {}
        moves: list[dict[str, Any]] = []
        seen_action_ids: set[int] = set()

        moves.append({
            "label": PUNISH_JUMP_LABEL,
            "action_id": PUNISH_JUMP_ACTION_ID,
            "kind": "jump",
            "display": f"{PUNISH_JUMP_LABEL} [0x{PUNISH_JUMP_ACTION_ID:03X}]",
        })
        seen_action_ids.add(PUNISH_JUMP_ACTION_ID)
        moves.append({
            "label": PUNISH_SUPER_JUMP_LABEL,
            "action_id": PUNISH_SUPER_JUMP_ACTION_ID,
            "kind": "super_jump",
            "display": f"{PUNISH_SUPER_JUMP_LABEL} [0x{PUNISH_SUPER_JUMP_ACTION_ID:03X}]",
        })
        seen_action_ids.add(PUNISH_SUPER_JUMP_ACTION_ID)

        for label, action_id in PUNISH_LABEL_ACTIONS.items():
            if str(label).upper() in {"JUMP", "SUPERJUMP"}:
                continue
            action_id = int(action_id) & 0xFFFF
            seen_action_ids.add(action_id)
            moves.append({"label": label, "action_id": action_id, "kind": "move", "display": f"{label} [0x{action_id:03X}]"})

        if isinstance(bucket, dict):
            pairs = []
            for raw_id, raw_label in bucket.items():
                try:
                    action_id = int(raw_id) & 0xFFFF
                except Exception:
                    continue
                label = _clean_text(raw_label)
                if not label or label.upper().startswith("FLAG_"):
                    continue
                if action_id < 0x100 or action_id >= 0x4000:
                    continue
                pairs.append((action_id, label))
            for action_id, label in sorted(pairs, key=_move_sort_key):
                if action_id in seen_action_ids:
                    continue
                seen_action_ids.add(action_id)
                moves.append({"label": label, "action_id": action_id, "kind": "move", "display": f"{label} [0x{action_id:03X}]"})

        out[slot] = {"slot": slot, "base": base, "name": name or "Waiting", "char_id": char_id, "moves": moves}
    return out


# Compatibility aliases used by the host package.
_load_punish_training_config = load_punish_trainer_config
_save_punish_training_config = save_punish_trainer_config
_tick_punish_training = tick_punish_trainer
_punish_training_roster_context = punish_roster_context
_clear_punish_training_runtime = clear_punish_trainer_runtime
_load_punish_trainer_config = load_punish_trainer_config
_save_punish_trainer_config = save_punish_trainer_config
_tick_punish_trainer = tick_punish_trainer
_punish_roster_context = punish_roster_context
_punish_trainer_overlay_payload = punish_trainer_overlay_payload
