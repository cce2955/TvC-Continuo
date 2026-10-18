from __future__ import annotations

import struct
from typing import Any

from tvcgui.core.constants import (
    CHAR_NAMES, OFF_CHAR_ID, RUNTIME_BLOCKSTUN_REMAINING_OFF, RUNTIME_HITSTUN_REMAINING_OFF,
    RUNTIME_IMPACT_FREEZE_OFF, RUNTIME_REACTION_TIMER_OFF, SLOTS,
)
from tvcgui.features.combat.move_id_map import lookup_move_name
from tvcgui.platform.dolphin import addr_in_ram, rbytes, rd8, rd32

ACTION_FRAME_OFF = 0x01D8
ACTION_OFF = 0x01E8
STATE_FLAGS_A_OFF = 0x0058
STATE_FLAGS_B_OFF = 0x0060
STATE_FLAGS_6C_OFF = 0x006C
FIGHTER_COMBO_COUNT_OFF = 0x11C4
HITSTUN_DECAY_COUNTER_OFF = 0x11CC
UNTECH_TIMER_OFF = 0x1220

INPUT_PREVIOUS_OFF = 0x13C8
INPUT_HELD_OFF = 0x13CC
INPUT_PRESSED_OFF = 0x13D0
INPUT_RELEASED_OFF = 0x13D4
INPUT_REPEAT_A_OFF = 0x13D8
INPUT_REPEAT_B_OFF = 0x13DC
INPUT_SOFTWARE_FLAGS_OFF = 0x13E0
INPUT_RULE_TABLE_PTR_OFF = 0x13E8

ACCEPTED_COMMAND_OFF = 0x1994
PENDING_COMMAND_FLAGS_OFF = 0x2108
PENDING_COMMAND_INDEX_OFF = 0x210C
PENDING_COMMAND_META_OFF = 0x2110
PENDING_COMMAND_PARAM_OFF = 0x2114

P1_SOURCE_STATUS_ADDR = 0x803F404C
P1_DECODED_SOURCE_ADDR = 0x803F4050
P1_RAW_SOURCE_ADDR = 0x803F4054
GLOBAL_COMBO_COUNTER_ADDR = 0x809BDDB3

DIRECTION_MASK = 0x0F
BUTTON_A = 0x80
BUTTON_B = 0x40
BUTTON_C = 0x20
BUTTON_PARTNER = 0x10
BUTTON_TAUNT = 0x0C00
KNOWN_INPUT_MASK = DIRECTION_MASK | BUTTON_A | BUTTON_B | BUTTON_C | BUTTON_PARTNER | BUTTON_TAUNT
DISPLAY_IGNORED_INPUT_MASK = 0x8000

_DIRECTION_TO_NUMPAD = {
    0x0: "5",
    0x1: "6",
    0x2: "4",
    0x4: "8",
    0x5: "9",
    0x6: "7",
    0x8: "2",
    0x9: "3",
    0xA: "1",
}

_GENERIC_ACTION_NAMES = {
    0x100: "5A",
    0x101: "5B",
    0x102: "5C",
    0x103: "2A",
    0x104: "2B",
    0x105: "2C",
    0x106: "6C",
    0x107: "Back+C",
    0x108: "3C",
    0x109: "j.A",
    0x10A: "j.B",
    0x10B: "j.C",
    0x10C: "j.Forward+C",
    0x10D: "j.Back+C",
    0x10E: "6B",
}

_RYU_ACTION_NAMES = {
    0x130: "Hadouken A",
    0x131: "Hadouken B",
    0x132: "Hadouken C",
}

_SLOT_POINTERS = {label: int(ptr) for label, ptr, _team in SLOTS}


def available_slots() -> tuple[str, ...]:
    return tuple(_SLOT_POINTERS)


def _read_u32(addr: int, default: int = 0) -> int:
    try:
        value = rd32(int(addr))
        if value is None:
            return int(default) & 0xFFFFFFFF
        return int(value) & 0xFFFFFFFF
    except Exception:
        return int(default) & 0xFFFFFFFF


def _fighter_base(slot_label: str) -> tuple[int, int]:
    ptr_addr = int(_SLOT_POINTERS.get(str(slot_label), 0))
    if not ptr_addr:
        return 0, 0
    base = _read_u32(ptr_addr, 0)
    if not addr_in_ram(base):
        return ptr_addr, 0
    return ptr_addr, base


def read_global_combo_count() -> int:
    """Read the game-wide combo counter for the realtime sampler."""
    try:
        value = rd8(GLOBAL_COMBO_COUNTER_ADDR)
        return int(value) if value is not None else 0
    except Exception:
        return 0


def read_overlay_input_packet(
    slot_label: str = "P1-C1",
    fighter_base: int = 0,
    *,
    combo_count: int | None = None,
) -> dict[str, Any]:
    """Read only the small input packet needed by the in-game overlay."""
    label = str(slot_label or "P1-C1")
    base = int(fighter_base or 0) & 0xFFFFFFFF
    if not addr_in_ram(base):
        _ptr_addr, base = _fighter_base(label)
    if not base:
        return {
            "connected": False,
            "slot": label,
            "base": 0,
            "previous": 0,
            "held": 0,
            "pressed": 0,
            "released": 0,
            "current_hp": 0,
            "aux_hp": 0,
            "current_meter": 0,
            "action_id": 0,
            "action_frame": 0,
            "blockstun_remaining": 0,
            "hitstun_remaining": 0,
            "untech_remaining": 0,
            "reaction_timer_remaining": 0,
            "impact_freeze_remaining": 0,
            "fighter_combo_count": 0,
            "decay_counter": 0,
            "state_flags_6c": 0,
            "combo_count": 0,
            "point_active": False,
            "held_text": "5",
            "pressed_text": "none",
            "released_text": "none",
        }

    # Realtime sampling is latency-sensitive. All fields used by this packet
    # live inside one contiguous fighter-struct span, so take one process-memory
    # snapshot instead of several separate RPM/DME calls. At 240 Hz across four
    # fighters, removing those extra round trips matters far more than copying
    # ~17 KB of local memory. The scalar path remains as a safety fallback.
    realtime_span_end = 0x44A4  # includes the native point flag at +0x44A0
    realtime_span_size = realtime_span_end - OFF_CHAR_ID
    try:
        realtime_blob = rbytes(base + OFF_CHAR_ID, realtime_span_size)
    except Exception:
        realtime_blob = None

    if realtime_blob and len(realtime_blob) >= realtime_span_size:
        def blob_u32(offset: int) -> int:
            return struct.unpack_from(">I", realtime_blob, int(offset) - OFF_CHAR_ID)[0]

        char_id = blob_u32(OFF_CHAR_ID)
        current_hp = blob_u32(0x28)
        aux_hp = blob_u32(0x2C)
        # Team meter is owned by the fixed C1 team bank at +0x4C. It is
        # already inside this same 17 KB fighter snapshot, so exposing it on
        # the realtime lane costs no additional Dolphin/process-memory read.
        current_meter = blob_u32(0x4C) if label.endswith("-C1") else 0
        action_frame_raw = blob_u32(ACTION_FRAME_OFF)
        action_id = blob_u32(ACTION_OFF) & 0x7FFF
        blockstun_remaining = blob_u32(RUNTIME_BLOCKSTUN_REMAINING_OFF)
        hitstun_remaining = blob_u32(RUNTIME_HITSTUN_REMAINING_OFF)
        untech_remaining = blob_u32(UNTECH_TIMER_OFF)
        reaction_timer_remaining = blob_u32(RUNTIME_REACTION_TIMER_OFF)
        impact_freeze_remaining = blob_u32(RUNTIME_IMPACT_FREEZE_OFF)
        fighter_combo_count = blob_u32(FIGHTER_COMBO_COUNT_OFF)
        decay_counter = blob_u32(HITSTUN_DECAY_COUNTER_OFF)
        state_flags_6c = blob_u32(STATE_FLAGS_6C_OFF)
        previous = blob_u32(INPUT_PREVIOUS_OFF)
        held = blob_u32(INPUT_HELD_OFF)
        pressed = blob_u32(INPUT_PRESSED_OFF)
        released = blob_u32(INPUT_RELEASED_OFF)
        point_active = bool(blob_u32(0x44A0))
    else:
        # Fallback keeps the older segmented reads for unusual builds where a
        # large contiguous read is unavailable.
        try:
            packet_blob = rbytes(base + INPUT_PREVIOUS_OFF, 16)
        except Exception:
            packet_blob = None
        if packet_blob and len(packet_blob) >= 16:
            previous, held, pressed, released = struct.unpack(">IIII", packet_blob[:16])
        else:
            previous = _read_u32(base + INPUT_PREVIOUS_OFF)
            held = _read_u32(base + INPUT_HELD_OFF)
            pressed = _read_u32(base + INPUT_PRESSED_OFF)
            released = _read_u32(base + INPUT_RELEASED_OFF)

        try:
            fighter_blob = rbytes(base + OFF_CHAR_ID, (ACTION_OFF - OFF_CHAR_ID) + 4)
        except Exception:
            fighter_blob = None
        if fighter_blob and len(fighter_blob) >= (ACTION_OFF - OFF_CHAR_ID) + 4:
            char_id = struct.unpack_from(">I", fighter_blob, 0)[0]
            current_hp = struct.unpack_from(">I", fighter_blob, 0x28 - OFF_CHAR_ID)[0]
            aux_hp = struct.unpack_from(">I", fighter_blob, 0x2C - OFF_CHAR_ID)[0]
            current_meter = struct.unpack_from(">I", fighter_blob, 0x4C - OFF_CHAR_ID)[0] if label.endswith("-C1") else 0
            action_frame_raw = struct.unpack_from(">I", fighter_blob, ACTION_FRAME_OFF - OFF_CHAR_ID)[0]
            action_id = struct.unpack_from(">I", fighter_blob, ACTION_OFF - OFF_CHAR_ID)[0] & 0x7FFF
        else:
            char_id = _read_u32(base + OFF_CHAR_ID)
            current_hp = _read_u32(base + 0x28)
            aux_hp = _read_u32(base + 0x2C)
            current_meter = _read_u32(base + 0x4C) if label.endswith("-C1") else 0
            action_frame_raw = _read_u32(base + ACTION_FRAME_OFF)
            action_id = _read_u32(base + ACTION_OFF) & 0x7FFF

        blockstun_remaining = _read_u32(base + RUNTIME_BLOCKSTUN_REMAINING_OFF)
        hitstun_remaining = _read_u32(base + RUNTIME_HITSTUN_REMAINING_OFF)
        untech_remaining = _read_u32(base + UNTECH_TIMER_OFF)
        reaction_timer_remaining = _read_u32(base + RUNTIME_REACTION_TIMER_OFF)
        impact_freeze_remaining = _read_u32(base + RUNTIME_IMPACT_FREEZE_OFF)
        fighter_combo_count = _read_u32(base + FIGHTER_COMBO_COUNT_OFF)
        decay_counter = _read_u32(base + HITSTUN_DECAY_COUNTER_OFF)
        state_flags_6c = _read_u32(base + STATE_FLAGS_6C_OFF)
        point_active = bool(_read_u32(base + 0x44A0))

    try:
        action_frame_float = struct.unpack(">f", struct.pack(">I", action_frame_raw & 0xFFFFFFFF))[0]
        action_frame = int(action_frame_float) if 0.0 <= action_frame_float < 100000.0 else 0
    except Exception:
        action_frame = 0

    # Mission events are produced on this realtime lane. The fighter snapshot
    # above already supplied hitstun and point ownership; global combo count is
    # sampled once per scheduler tick and shared across all slot packets.
    if combo_count is None:
        combo_count = read_global_combo_count()
    else:
        combo_count = max(0, int(combo_count or 0))

    return {
        "connected": True,
        "slot": label,
        "base": base,
        "char_id": char_id,
        "action_id": action_id,
        "action_frame": action_frame,
        "current_hp": current_hp,
        "aux_hp": aux_hp,
        "current_meter": current_meter,
        "blockstun_remaining": blockstun_remaining,
        "hitstun_remaining": hitstun_remaining,
        "untech_remaining": untech_remaining,
        "reaction_timer_remaining": reaction_timer_remaining,
        "impact_freeze_remaining": impact_freeze_remaining,
        "fighter_combo_count": fighter_combo_count,
        "decay_counter": decay_counter,
        "state_flags_6c": state_flags_6c,
        "combo_count": combo_count,
        "point_active": point_active,
        "previous": previous,
        "held": held,
        "pressed": pressed,
        "released": released,
        "held_text": format_input_word(held),
        "pressed_text": format_button_edges(pressed),
        "released_text": format_button_edges(released),
    }



def read_realtime_action_edge(fighter_base: int) -> dict[str, int]:
    """Read the native action ID + action frame in one process-memory read.

    Move history must not miss a one-frame cancel state because three separate
    ReadProcessMemory calls and a heavy listener happened to straddle it.  The
    two fields we need are only 0x10 bytes apart, so read the 0x14-byte window
    once and decode both values from that same snapshot.  Character identity is
    supplied by the regular realtime packet cache when an edge is emitted.
    """
    base = int(fighter_base or 0) & 0xFFFFFFFF
    if not addr_in_ram(base):
        return {"base": 0, "char_id": 0, "action_id": 0, "action_frame": 0}
    span = (ACTION_OFF - ACTION_FRAME_OFF) + 4
    blob = rbytes(base + ACTION_FRAME_OFF, span)
    if not blob or len(blob) < span:
        return {"base": base, "char_id": 0, "action_id": 0, "action_frame": 0}
    action_frame_raw = struct.unpack_from(">I", blob, 0)[0]
    action_id = struct.unpack_from(">I", blob, ACTION_OFF - ACTION_FRAME_OFF)[0] & 0x7FFF
    try:
        action_frame_float = struct.unpack(">f", struct.pack(">I", action_frame_raw & 0xFFFFFFFF))[0]
        action_frame = int(action_frame_float) if 0.0 <= action_frame_float < 100000.0 else 0
    except Exception:
        action_frame = 0
    return {
        "base": base,
        "char_id": 0,
        "action_id": action_id,
        "action_frame": action_frame,
    }

def direction_name(direction_bits: int, *, unrestricted_zero: bool = False) -> str:
    direction = int(direction_bits) & DIRECTION_MASK
    if direction == 0 and unrestricted_zero:
        return "any"
    return _DIRECTION_TO_NUMPAD.get(direction, f"dir 0x{direction:X}")


def button_names(value: int) -> tuple[str, ...]:
    word = int(value) & 0xFFFFFFFF
    out: list[str] = []
    if word & BUTTON_A:
        out.append("A")
    if word & BUTTON_B:
        out.append("B")
    if word & BUTTON_C:
        out.append("C")
    if word & BUTTON_PARTNER:
        out.append("P")
    if (word & BUTTON_TAUNT) == BUTTON_TAUNT:
        out.append("T")
    return tuple(out)


def format_input_word(value: int, *, neutral_label: bool = True) -> str:
    word = int(value) & 0xFFFFFFFF
    direction = direction_name(word)
    buttons = "".join(button_names(word))
    text = f"{direction}{buttons}" if buttons else direction
    unknown_bits = (word & 0xFFFF) & ~KNOWN_INPUT_MASK & ~DISPLAY_IGNORED_INPUT_MASK
    if unknown_bits:
        text += f" +0x{unknown_bits:04X}"
    if not neutral_label and text == "5":
        return ""
    return text


def format_button_edges(value: int) -> str:
    word = int(value) & 0xFFFFFFFF
    labels = list(button_names(word))
    direction = word & DIRECTION_MASK
    if direction:
        labels.insert(0, direction_name(direction))
    unknown_bits = (word & 0xFFFF) & ~KNOWN_INPUT_MASK & ~DISPLAY_IGNORED_INPUT_MASK
    if unknown_bits:
        labels.append(f"0x{unknown_bits:04X}")
    return " + ".join(labels) if labels else "none"


def _action_name(action_id: int, char_id: int) -> str:
    action = int(action_id) & 0x7FFF
    try:
        mapped = lookup_move_name(action, char_id=char_id)
    except Exception:
        mapped = None
    if mapped:
        return str(mapped)
    if int(char_id) == 12 and action in _RYU_ACTION_NAMES:
        return _RYU_ACTION_NAMES[action]
    return _GENERIC_ACTION_NAMES.get(action, "")


def action_name(action_id: int, char_id: int) -> str:
    """Return the best known display name for one native action ID."""
    return _action_name(action_id, char_id)


def _read_rule_entries(table_ptr: int, max_entries: int = 128) -> list[dict[str, int]]:
    if not addr_in_ram(table_ptr):
        return []
    size = max(1, min(512, int(max_entries))) * 0x18
    try:
        blob = rbytes(int(table_ptr), size)
    except Exception:
        blob = None
    if not blob:
        return []

    entries: list[dict[str, int]] = []
    for index in range(min(int(max_entries), len(blob) // 0x18)):
        off = index * 0x18
        chunk = blob[off:off + 0x18]
        if len(chunk) < 0x18:
            break
        selector, subtype, direction, buttons, gate_a, gate_b, action = struct.unpack(">hHIIIII", chunk)
        if selector == -1:
            break
        entries.append({
            "index": index,
            "address": (int(table_ptr) + off) & 0xFFFFFFFF,
            "selector": int(selector),
            "subtype": int(subtype),
            "direction": int(direction) & 0xFFFFFFFF,
            "buttons": int(buttons) & 0xFFFFFFFF,
            "gate_a": int(gate_a) & 0xFFFFFFFF,
            "gate_b": int(gate_b) & 0xFFFFFFFF,
            "action": int(action) & 0xFFFFFFFF,
        })
    return entries


def _input_only_rule_matches(entries: list[dict[str, int]], held: int) -> list[dict[str, int]]:
    direction = int(held) & DIRECTION_MASK
    matches: list[dict[str, int]] = []
    for entry in entries:
        required_direction = int(entry.get("direction", 0)) & DIRECTION_MASK
        required_buttons = int(entry.get("buttons", 0)) & 0xFFFFFFFF
        direction_ok = required_direction == 0 or direction == required_direction
        buttons_ok = required_buttons == 0 or (int(held) & required_buttons) == required_buttons
        if direction_ok and buttons_ok:
            matches.append(entry)
    return matches


def read_input_snapshot(slot_label: str = "P1-C1") -> dict[str, Any]:
    label = str(slot_label or "P1-C1")
    ptr_addr, base = _fighter_base(label)
    if not base:
        return {
            "connected": False,
            "slot": label,
            "pointer_address": ptr_addr,
            "base": 0,
            "error": "Waiting for a live fighter pointer.",
        }

    char_id = _read_u32(base + OFF_CHAR_ID)
    action_id = _read_u32(base + ACTION_OFF) & 0x7FFF
    previous = _read_u32(base + INPUT_PREVIOUS_OFF)
    held = _read_u32(base + INPUT_HELD_OFF)
    pressed = _read_u32(base + INPUT_PRESSED_OFF)
    released = _read_u32(base + INPUT_RELEASED_OFF)
    repeat_a = _read_u32(base + INPUT_REPEAT_A_OFF)
    repeat_b = _read_u32(base + INPUT_REPEAT_B_OFF)
    software_flags = _read_u32(base + INPUT_SOFTWARE_FLAGS_OFF)
    state_a = _read_u32(base + STATE_FLAGS_A_OFF)
    state_b = _read_u32(base + STATE_FLAGS_B_OFF)

    table_ptr = _read_u32(base + INPUT_RULE_TABLE_PTR_OFF)
    entries = _read_rule_entries(table_ptr)
    current_action_rules = [entry for entry in entries if (entry["action"] & 0x7FFF) == action_id]
    input_matches = _input_only_rule_matches(entries, held)

    accepted_command = _read_u32(base + ACCEPTED_COMMAND_OFF)
    pending_command_flags = _read_u32(base + PENDING_COMMAND_FLAGS_OFF)
    pending_command_index = _read_u32(base + PENDING_COMMAND_INDEX_OFF)
    pending_command_meta = _read_u32(base + PENDING_COMMAND_META_OFF)
    pending_command_param = _read_u32(base + PENDING_COMMAND_PARAM_OFF)

    source_status = source_decoded = source_raw = 0
    if label == "P1-C1":
        source_status = _read_u32(P1_SOURCE_STATUS_ADDR)
        source_decoded = _read_u32(P1_DECODED_SOURCE_ADDR)
        source_raw = _read_u32(P1_RAW_SOURCE_ADDR)

    return {
        "connected": True,
        "slot": label,
        "pointer_address": ptr_addr,
        "base": base,
        "char_id": char_id,
        "char_name": CHAR_NAMES.get(char_id, f"ID_{char_id}"),
        "action_id": action_id,
        "action_name": _action_name(action_id, char_id),
        "previous": previous,
        "held": held,
        "pressed": pressed,
        "released": released,
        "repeat_a": repeat_a,
        "repeat_b": repeat_b,
        "software_flags": software_flags,
        "state_a": state_a,
        "state_b": state_b,
        "held_text": format_input_word(held),
        "pressed_text": format_button_edges(pressed),
        "released_text": format_button_edges(released),
        "rule_table": table_ptr,
        "rule_count": len(entries),
        "current_action_rules": current_action_rules,
        "input_only_matches": input_matches,
        "accepted_command": accepted_command,
        "pending_command_flags": pending_command_flags,
        "pending_command_index": pending_command_index,
        "pending_command_meta": pending_command_meta,
        "pending_command_param": pending_command_param,
        "source_status": source_status,
        "source_decoded": source_decoded,
        "source_raw": source_raw,
    }


def format_rule(entry: dict[str, int]) -> str:
    address = int(entry.get("address", 0))
    selector = int(entry.get("selector", 0))
    subtype = int(entry.get("subtype", 0))
    direction = int(entry.get("direction", 0))
    buttons = int(entry.get("buttons", 0))
    gate_a = int(entry.get("gate_a", 0))
    gate_b = int(entry.get("gate_b", 0))
    action = int(entry.get("action", 0)) & 0x7FFF
    button_text = "".join(button_names(buttons)) or "none"
    return (
        f"0x{address:08X}  src={selector}/{subtype}  "
        f"dir={direction_name(direction, unrestricted_zero=True)}  buttons={button_text}  "
        f"gateA=0x{gate_a:08X}  gateB=0x{gate_b:08X}  -> 0x{action:03X}"
    )
