#!/usr/bin/env python3
"""
hud_overlay.py
--------------
Transparent Dolphin-parented overlay that displays live per-slot HUD data:
    HP | Meter | MoveID | Baroque | Frame Advantage

Frame advantage is tracked locally in this process for all four slots.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import tempfile
import time
from typing import Optional
import math
import random
import pygame
import win32con
import win32gui

from tvcgui.core.paths import user_data_path
from tvcgui.features.overlay.damage_scaling import build_damage_breakdown_lines, build_live_damage_modifier
from tvcgui.runtime.input_monitor import action_name as realtime_action_name

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_FILE = user_data_path("overlay", "hud_overlay_data.json")
REALTIME_INPUT_FILE = user_data_path("overlay", "hud_input_realtime.json")
REALTIME_STUN_FILE = os.path.join(tempfile.gettempdir(), "tvcgui_hud_stun_realtime.json")
REALTIME_MOVES_FILE = os.path.join(tempfile.gettempdir(), "tvcgui_hud_moves_realtime.json")
TARGET_FPS = 60
COLORKEY = (0, 0, 0)

BASE_W          = 1280
BASE_H          = 720
BASE_FONT_SIZE  = 14
BASE_ROW_H      = 22
BG_ALPHA        = 180
OOMPH_SCALE     = 0.40

SLOT_LAYOUT = {
    "P1-C1": ("left",  28, 178),
    "P1-C2": ("left",  28, 207),
    "P2-C1": ("right", 28, 178),
    "P2-C2": ("right", 28, 207),
}
SLOT_COLORS = {
    "P1-C1": (255, 100, 100),
    "P1-C2": (255, 150, 120),
    "P2-C1": ( 90, 160, 255),
    "P2-C2": (120, 190, 255),
}

COL_TEXT         = (220, 220, 220)
COL_TEXT_DIM     = (120, 120, 120)
COL_DEAD         = ( 90,  90,  90)
COL_HP_HIGH      = ( 60, 200,  90)
COL_HP_LOW       = (220,  60,  60)
COL_HP_DEAD      = ( 70,  70,  70)
COL_HP_BG        = ( 40,  40,  40)
COL_METER_FULL   = ( 70, 140, 255)
COL_METER_EMPTY  = ( 35,  35,  50)
COL_BAROQUE_ON   = (255, 200,  60)
COL_BAROQUE_BG   = (100,  60,   8)

BG_ALPHA         = 200

# Overlay layout: compact is the default match presentation; detail preserves the legacy per-slot rows.
HUD_LAYOUT_MODE = "compact"

ASSIST_STANDBY_IDS = {430, 432, 433}
ASSIST_ATTACK_IDS  = {420, 426, 427, 428}
ASSIST_OFF_IDS     = ASSIST_STANDBY_IDS 

PASSIVE_LABELS = {
    "idle", "crouch", "crouched", "crouching", "couching", "stand", "standing",
    "jump", "jump forward", "jump back", "jump cancel", "landing", "rising",
    "assist standby", "assist leave",
    "assist attack", "assist taunt", "tag out", "tag in",
}

_PROFILE_HISTORY_SUFFIX_RE = re.compile(
    r"^(.*?)(?:\s+|\s*\()((?:level|lvl|lv|l)\s*[-_:]?\s*[1-9][0-9]*|[1-9][0-9]*|[abc]|[lmh])\)?\s*$",
    re.IGNORECASE,
)


def _profile_history_norm(value) -> str:
    text = str(value or "").strip().lower().replace("_", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _split_profile_history_variant(label: str) -> tuple[str, str]:
    text = str(label or "").strip()
    match = _PROFILE_HISTORY_SUFFIX_RE.match(text)
    if not match:
        return text, ""
    core = str(match.group(1) or "").strip(" -_/()[]")
    suffix = re.sub(r"\s+", "", str(match.group(2) or "").strip())
    return core, suffix


def _resolve_profile_history_label(current_label: str, profile_label: str) -> str:
    """Merge a profiler variant into the move history's full move name."""
    current = str(current_label or "").strip()
    profile = str(profile_label or "").strip()
    if not profile:
        return current
    if not current:
        return profile

    current_norm = _profile_history_norm(current)
    profile_norm = _profile_history_norm(profile)
    if not current_norm or current_norm == profile_norm:
        return profile
    if current_norm in profile_norm:
        return profile

    profile_core, profile_suffix = _split_profile_history_variant(profile)
    current_core, _current_suffix = _split_profile_history_variant(current)
    current_tokens = set(_profile_history_norm(current_core).split())
    profile_tokens = set(_profile_history_norm(profile_core).split())
    same_family = bool(current_tokens and profile_tokens and current_tokens.intersection(profile_tokens))

    if profile_suffix and same_family:
        return f"{current_core or current} {profile_suffix}".strip()
    if same_family:
        return profile
    return current


def _apply_profile_history_correction(slot_anim: dict, snap: dict,
                                      current_move_id: int | None,
                                      current_label: str) -> str:
    """Rewrite the existing projectile chip from the explicit profiler payload."""
    if not bool(snap.get("profile_live_active", False)):
        return current_label
    profile_label = str(snap.get("profile_live_label") or snap.get("profile_history_label") or "").strip()
    resolved_full = str(snap.get("profile_resolved_label") or "").strip()
    if not profile_label:
        return current_label

    try:
        profile_action_id = int(snap.get("profile_history_action_id") or 0)
    except Exception:
        profile_action_id = 0
    move_events = slot_anim.get("move_events") or []

    def candidate_text(event: dict) -> str:
        base_text = str(event.get("base_text") or event.get("text") or "").strip()
        resolved = resolved_full or _resolve_profile_history_label(base_text, profile_label)
        if not resolved:
            return ""
        # Require the event's actual family to match the profiler family. This
        # is what prevents Idle 2, 5A 2, and 2A 2.
        event_core, _ = _split_profile_history_variant(base_text)
        profile_core, _ = _split_profile_history_variant(profile_label)
        event_tokens = set(_profile_history_norm(event_core).split())
        profile_tokens = set(_profile_history_norm(profile_core).split())
        if not event_tokens.intersection(profile_tokens):
            return ""
        return resolved

    target = None
    replacement = ""
    if profile_action_id:
        for event in move_events:
            try:
                event_action = int(event.get("action_id") or 0)
            except Exception:
                event_action = 0
            if event_action != profile_action_id:
                continue
            replacement = candidate_text(event)
            if replacement:
                target = event
                break
    if target is None:
        for event in move_events:
            replacement = candidate_text(event)
            if replacement:
                target = event
                break

    if target is None or not replacement:
        return current_label

    target["text"] = replacement
    target["profile_label"] = profile_label
    target["profile_corrected"] = True
    target["life"] = max(float(target.get("life") or 0.0), 1.0)

    if move_events and target is move_events[0]:
        final_label = str(snap.get("final_move_label") or "").strip()
        if final_label:
            current_label = final_label
            slot_anim["prev_move_label"] = final_label
    return current_label


# Move IDs that mean the character is in hitstun / blockstun
REACTION_IDS = {48, 49, 50, 51, 52, 64, 65, 66, 73,75, 79, 80, 81, 
                82, 83, 89, 90, 92, 95, 96, 98,
                102,105,106,113, 114,115,116, 117, 118 ,119, 160}
BLOCK_REACTION_IDS = {48, 49, 50, 51, 52, 53}
BAROQUE_CANCEL_IDS = {162, 163, 164}
INPUT_DIRECTION_MASK = 0x0F
INPUT_FACE_BUTTON_MASK = 0xF0
INPUT_TAUNT_MASK = 0x0C00
INPUT_BUTTON_MASK = INPUT_FACE_BUTTON_MASK | INPUT_TAUNT_MASK
INPUT_TRACK_MASK = INPUT_DIRECTION_MASK | INPUT_BUTTON_MASK

_INPUT_DIRECTION_TEXT = {
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


def _format_overlay_input_token(direction_bits: int, button_bits: int = 0) -> str:
    direction = _INPUT_DIRECTION_TEXT.get(int(direction_bits) & INPUT_DIRECTION_MASK, "5")
    buttons: list[str] = []
    word = int(button_bits) & INPUT_BUTTON_MASK
    if word & 0x80:
        buttons.append("A")
    if word & 0x40:
        buttons.append("B")
    if word & 0x20:
        buttons.append("C")
    if word & 0x10:
        buttons.append("P")
    if (word & INPUT_TAUNT_MASK) == INPUT_TAUNT_MASK:
        buttons.append("T")
    return direction + "".join(buttons)


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_display_slots: dict = {}
_frame: int = 0

# Per cross-team pair state machine: (atk_slot, vic_slot) -> state dict
_adv_pairs: dict = {}

# Short-lived match widgets sourced from the same live HUD snapshots.
_interaction_ribbon = {
    "title": "",
    "detail": "",
    "color": (130, 175, 255),
    "stamp": "",
    "life": 0.0,
    "age": 0.0,
}
_combo_ledgers: dict[str, dict] = {"P1": {}, "P2": {}}
_punish_overlay: dict = {}
_timing_engine_payload: dict = {}
_last_timing_sequence: int = 0
_COUNTDOWN_FONT_CACHE: dict[tuple[int, bool], pygame.font.Font] = {}
_PUNISH_BADGE_ANIM = {
    "alpha": 0.0,
    "slide_px": 0.0,
    "pop": 0.0,
    "shine": 0.0,
    "last_token": "",
    "last_slot": "P2-C1",
    "last_payload": {},
}

_HISTORY_HEADER_CHIP_CACHE: dict[tuple, pygame.Surface] = {}
_COMPACT_METER_GRADIENT_CACHE: dict[tuple, pygame.Surface] = {}
_COMPACT_PANEL_SHELL_CACHE: dict[tuple, tuple[pygame.Surface, pygame.Surface]] = {}

# ---------------------------------------------------------------------------
# ADV helpers
# ---------------------------------------------------------------------------

def _mv(slot_label: str):
    v = _display_slots.get(slot_label, {}).get("mv_id_display")
    return int(v) if v is not None else None

def _is_attacking(mv):
    return mv is not None and 256 <= mv <= 512

def _is_stuck(mv):
    return mv is not None and mv in REACTION_IDS

def _is_actionable(mv):
    return mv is not None and not _is_attacking(mv) and not _is_stuck(mv)

def _get_active_slot(team: str) -> str | None:
    c1, c2 = f"{team}-C1", f"{team}-C2"
    s1, s2 = _display_slots.get(c1), _display_slots.get(c2)
    if not s1 and not s2:
        return None
    if s1 and not s2:
        return c1
    if s2 and not s1:
        return c2

    # Native fighter+0x44A0 is the stable point-character flag. +0x44A4 is a
    # separate runtime permission/lane field and must not select C1 versus C2.
    c1_is_point = bool(s1.get("damage_point_active", s1.get("damage_is_point")))
    c2_is_point = bool(s2.get("damage_point_active", s2.get("damage_is_point")))
    if c1_is_point != c2_is_point:
        return c1 if c1_is_point else c2

    # During short tag transitions the native flags can momentarily agree. Keep
    # the old animation-state fallback for those frames.
    mv1 = int(s1.get("mv_id_display") or 0)
    mv2 = int(s2.get("mv_id_display") or 0)
    if mv2 in ASSIST_OFF_IDS and mv1 not in ASSIST_OFF_IDS:
        return c1
    if mv1 in ASSIST_OFF_IDS and mv2 not in ASSIST_OFF_IDS:
        return c2
    return c1

def _push_adv(slot_label: str, value: int) -> None:
    sa = _get_slot_anim(slot_label)
    events = sa["adv_events"]
    events.insert(0, {"value": value, "life": 1.0, "age": 0.0, "x_offset": 20})
    if len(events) > 3:
        events.pop()


def _consume_timing_engine_result() -> bool:
    """Publish one authoritative timing-engine result to the block counter."""
    global _last_timing_sequence
    latest = (_timing_engine_payload or {}).get("latest")
    if not isinstance(latest, dict):
        return False
    try:
        sequence = int(latest.get("sequence") or 0)
    except Exception:
        sequence = 0
    if sequence <= _last_timing_sequence:
        return False
    _last_timing_sequence = sequence
    if str(latest.get("kind") or "").lower() != "block":
        return False
    if latest.get("block_advantage") is None:
        return False

    attacker_slot = str(latest.get("attacker_slot") or "")
    defender_slot = str(latest.get("defender_slot") or "")
    try:
        advantage = int(latest.get("block_advantage"))
    except Exception:
        return False
    _push_adv(attacker_slot, advantage)
    _push_adv(defender_slot, -advantage)

    attacker = str(latest.get("attacker_name") or _snap_name(attacker_slot))
    defender = str(latest.get("defender_name") or _snap_name(defender_slot))
    move = str(latest.get("action_name") or _snap_move(attacker_slot))
    blockstun = int(latest.get("blockstun") or 0)
    hitstop = int(latest.get("hitstop") or 0)
    attacker_ready = latest.get("attacker_ready_frames")
    defender_ready = latest.get("defender_ready_frames")
    detail_parts = [f"{defender}", f"{advantage:+d}", f"BS {blockstun}", f"STOP {hitstop}"]
    if attacker_ready is not None and defender_ready is not None:
        detail_parts.append(f"READY {int(attacker_ready)}/{int(defender_ready)}")
    _interaction_ribbon.update({
        "title": f"BLOCK  |  {attacker}  {move}",
        "detail": "  •  ".join(detail_parts),
        "color": (128, 180, 255),
        "stamp": "OBSERVED",
        "life": 1.0,
        "age": 0.0,
    })
    pair = _adv_pairs.get((attacker_slot, defender_slot))
    if isinstance(pair, dict):
        pair["state"] = 0
        pair["first_end"] = None
        pair["first_slot"] = None
    return True


def _snap_int(slot_label: str, key: str, default: int = 0) -> int:
    try:
        return int((_display_slots.get(slot_label) or {}).get(key) or default)
    except (TypeError, ValueError):
        return default


def _snap_float(slot_label: str, key: str, default: float = 0.0) -> float:
    try:
        return float((_display_slots.get(slot_label) or {}).get(key) or default)
    except (TypeError, ValueError):
        return default


def _snap_name(slot_label: str) -> str:
    name = str((_display_slots.get(slot_label) or {}).get("name") or slot_label or "---").strip()
    return name or slot_label


def _snap_move(slot_label: str) -> str:
    snap = _display_slots.get(slot_label) or {}
    label = str(snap.get("final_move_label") or snap.get("mv_label_display") or snap.get("mv_label") or "").strip()
    if label and label.lower() not in PASSIVE_LABELS:
        return label
    try:
        move_id = int(snap.get("mv_id_display"))
        return f"0x{move_id:04X}"
    except (TypeError, ValueError):
        return "Action"


def _snap_attack_property_label(slot_label: str) -> str:
    snap = _display_slots.get(slot_label) or {}
    label = str(snap.get("attack_property_label") or "").strip().upper()
    if label:
        return label
    try:
        value = int(snap.get("attack_property")) & 0xFF
    except (TypeError, ValueError):
        return "UNKNOWN"
    return {
        0x04: "UNBLOCKABLE",
        0x09: "MID", 0x0A: "MID", 0x0C: "MID",
        0x11: "OVERHEAD", 0x12: "OVERHEAD", 0x14: "OVERHEAD",
        0x21: "LOW", 0x22: "LOW", 0x24: "LOW",
    }.get(value, "UNKNOWN")


def _contact_is_block(victim_slot: str) -> bool:
    victim_move = _mv(victim_slot)
    if victim_move in BLOCK_REACTION_IDS:
        return True
    victim_label = str((_display_slots.get(victim_slot) or {}).get("mv_label") or "").strip().lower()
    return "blockstun" in victim_label or victim_label.startswith("block ")


def _emit_first_contact_badge(st: dict, attacker_slot: str, victim_slot: str) -> bool:
    label = str(st.get("attack_guard_label") or "").strip().upper()
    if not label or label == "UNKNOWN":
        label = _snap_attack_property_label(attacker_slot)
        if label and label != "UNKNOWN":
            st["attack_guard_label"] = label
    if not label or label == "UNKNOWN":
        st["guard_indicator_pending"] = True
        return False
    hit = not bool(st.get("first_contact_blocked", False))
    _set_guard_indicator(victim_slot, label, hit)
    st["guard_indicator_pending"] = False
    st["guard_indicator_emitted"] = True
    return True


def _begin_adv_contact(st: dict, attacker_slot: str, victim_slot: str) -> None:
    current_contact_blocked = _contact_is_block(victim_slot)
    victim_move = _mv(victim_slot)
    victim_label = str((_display_slots.get(victim_slot) or {}).get("mv_label") or "").strip().lower()
    attacker_label = str((_display_slots.get(attacker_slot) or {}).get("mv_label") or "").strip().lower()
    st["victim_was_attacking"] = bool(_is_attacking(victim_move))
    st["victim_was_committed"] = bool(
        victim_move is not None
        and not _is_actionable(victim_move)
        and not _is_stuck(victim_move)
        and not current_contact_blocked
    )
    st["attacker_called_reversal"] = bool(
        "reversal" in attacker_label
        or (_display_slots.get(attacker_slot) or {}).get("is_reversal")
        or (_display_slots.get(attacker_slot) or {}).get("reversal")
    )
    st["victim_contact_label"] = victim_label

    # Hits lock to the first contacting move for the whole combo. Blocks should
    # retrigger for each newly blocked move in a blockstring.
    if bool(st.get("combo_property_locked", False)):
        if current_contact_blocked:
            st["victim_hp_start"] = _snap_int(victim_slot, "cur")
            st["attack_move"] = _snap_move(attacker_slot)
            st["attack_guard_label"] = _snap_attack_property_label(attacker_slot)
            st["attacker_name"] = _snap_name(attacker_slot)
            st["victim_name"] = _snap_name(victim_slot)
            st["first_contact_blocked"] = True
            st["guard_indicator_pending"] = False
            st["guard_indicator_emitted"] = False
            _emit_first_contact_badge(st, attacker_slot, victim_slot)
            return
        if bool(st.get("guard_indicator_pending", False)) and not bool(st.get("guard_indicator_emitted", False)):
            _emit_first_contact_badge(st, attacker_slot, victim_slot)
        return
    st["victim_hp_start"] = _snap_int(victim_slot, "cur")
    st["attack_move"] = _snap_move(attacker_slot)
    st["attack_guard_label"] = _snap_attack_property_label(attacker_slot)
    st["attacker_name"] = _snap_name(attacker_slot)
    st["victim_name"] = _snap_name(victim_slot)
    st["first_contact_blocked"] = current_contact_blocked
    st["guard_indicator_pending"] = False
    st["guard_indicator_emitted"] = False
    st["combo_property_locked"] = True
    _emit_first_contact_badge(st, attacker_slot, victim_slot)


def _set_guard_indicator(slot_label: str, label: str, hit: bool) -> None:
    slot_anim = _get_slot_anim(slot_label)
    guard_label = str(label or "").strip().upper() or "UNKNOWN"
    slot_anim["guard_indicator_label"] = guard_label
    slot_anim["guard_indicator_result"] = "HIT" if hit else "BLOCK"
    slot_anim["guard_indicator_life"] = 1.0
    slot_anim["guard_indicator_flash"] = 1.0
    _trigger_team_panel_fx(_team_from_slot(slot_label), (255, 110, 110) if hit else (92, 232, 146), 1.05 if hit else 0.85, 8 if hit else 6)


def _interaction_stamp(st: dict, hit: bool) -> str:
    if not hit:
        return ""
    if bool(st.get("attacker_called_reversal", False)):
        return "REVERSAL"
    if bool(st.get("victim_was_attacking", False)):
        return "COUNTER"
    if bool(st.get("victim_was_committed", False)):
        return "PUNISH"
    return ""


def _publish_interaction(attacker_slot: str, victim_slot: str, st: dict, advantage: int) -> None:
    start_hp = int(st.get("victim_hp_start") or _snap_int(victim_slot, "cur"))
    current_hp = _snap_int(victim_slot, "cur")
    damage = max(0, start_hp - current_hp)
    hit = damage > 0
    kind = "HIT" if hit else "BLOCK"
    move = str(st.get("attack_move") or _snap_move(attacker_slot))
    attacker = str(st.get("attacker_name") or _snap_name(attacker_slot))
    victim = str(st.get("victim_name") or _snap_name(victim_slot))
    detail = f"{victim}  •  {advantage:+d}"
    if hit:
        detail += f"  •  {damage:,} DMG"
    _interaction_ribbon.update({
        "title": f"{kind}  |  {attacker}  {move}",
        "detail": detail,
        "color": (255, 126, 126) if hit else (128, 180, 255),
        "stamp": _interaction_stamp(st, hit),
        "life": 1.0,
        "age": 0.0,
    })
    st["combo_property_locked"] = False
    st["attack_guard_label"] = ""
    st["attack_move"] = ""
    st["first_contact_blocked"] = False
    st["guard_indicator_pending"] = False
    st["guard_indicator_emitted"] = False


def _combo_register_damage(victim_slot: str, damage: int) -> None:
    """Accumulate a compact combo ledger from discrete HP-loss events."""
    if damage <= 0:
        return
    attacker_team = "P2" if str(victim_slot).startswith("P1") else "P1"
    attacker_slot = _get_active_slot(attacker_team)
    if not attacker_slot:
        return
    ledger = _combo_ledgers.setdefault(attacker_team, {})
    same_chain = (
        ledger.get("attacker_slot") == attacker_slot
        and ledger.get("victim_slot") == victim_slot
        and (_frame - int(ledger.get("last_hit_frame") or -9999)) <= 75
    )
    if not same_chain:
        ledger.clear()
        ledger.update({
            "attacker_slot": attacker_slot,
            "victim_slot": victim_slot,
            "attacker_name": _snap_name(attacker_slot),
            "victim_name": _snap_name(victim_slot),
            "hits": 0,
            "damage": 0,
            "meter_start": _snap_int(attacker_slot, "meter"),
            "baroque_start": _snap_float(attacker_slot, "baroque_red_pct_max"),
            "last_hit_frame": _frame,
            "life": 1.0,
            "hit_sheen": 0.0,
            "final_sheen": 0.0,
            "final_confirmed": False,
            "milestone_sheen": 0.0,
            "milestone_scale": 0.0,
            "milestone_hit": 0,
        })
    ledger["hits"] = int(ledger.get("hits") or 0) + 1
    ledger["damage"] = int(ledger.get("damage") or 0) + int(damage)
    ledger["last_hit_frame"] = _frame
    ledger["life"] = 1.0
    victim_snapshot = (_display_slots.get(str(victim_slot)) or {})
    victim_is_point = bool(
        victim_snapshot.get(
            "damage_combo_lane_active",
            victim_snapshot.get("damage_point_active", False),
        )
    )
    ledger["last_hit_damage"] = int(damage)
    ledger["damage_breakdown_lines"] = build_damage_breakdown_lines(
        _display_slots,
        attacker_slot,
        str(victim_slot),
        int(damage),
        victim_is_point=victim_is_point,
        owner_slot=f"{attacker_team}-C1",
    )
    # Every registered hit restarts a restrained polish sweep. A late hit also
    # cancels any pending final confirmation and keeps the chain visually live.
    ledger["hit_sheen"] = 1.0
    ledger["final_sheen"] = 0.0
    ledger["final_confirmed"] = False
    hit_count = int(ledger.get("hits") or 0)
    if hit_count in (10, 20, 30):
        ledger["milestone_sheen"] = 1.0
        ledger["milestone_scale"] = 1.0
        ledger["milestone_hit"] = hit_count


def _tick_combo_ledgers(dt: float) -> None:
    for team, ledger in _combo_ledgers.items():
        if not ledger:
            continue
        ledger["hit_sheen"] = max(0.0, float(ledger.get("hit_sheen") or 0.0) - dt * 5.0)
        ledger["milestone_sheen"] = max(0.0, float(ledger.get("milestone_sheen") or 0.0) - dt * 2.65)
        ledger["milestone_scale"] = max(0.0, float(ledger.get("milestone_scale") or 0.0) - dt * 3.8)
        age = _frame - int(ledger.get("last_hit_frame") or _frame)
        if age > 75 and not bool(ledger.get("final_confirmed", False)):
            # The combo is settled. Hold the card at full opacity long enough
            # for one deliberate confirmation sheen before beginning its fade.
            ledger["final_confirmed"] = True
            ledger["final_sheen"] = 1.0
        if bool(ledger.get("final_confirmed", False)):
            final_sheen = max(0.0, float(ledger.get("final_sheen") or 0.0) - dt * 2.05)
            ledger["final_sheen"] = final_sheen
            if final_sheen <= 0.01:
                ledger["life"] = max(0.0, float(ledger.get("life") or 0.0) - dt * 1.45)
        if float(ledger.get("life") or 0.0) <= 0.01:
            ledger.clear()

def _update_adv() -> None:
    """Called once per frame. Tracks frame advantage for the active pair."""
    p1_slot = _get_active_slot("P1")
    p2_slot = _get_active_slot("P2")
    if not p1_slot or not p2_slot:
        return

    # Check both directions (P1 attacks P2, and P2 attacks P1)
    for a_slot, v_slot in ((p1_slot, p2_slot), (p2_slot, p1_slot)):
        key = (a_slot, v_slot)
        st = _adv_pairs.setdefault(key, {
            "state": 0,
            "first_end": None,
            "first_slot": None,
            "prev_a": None,
            "prev_v": None,
            "victim_hp_start": None,
            "attack_move": "",
            "attack_guard_label": "",
            "combo_property_locked": False,
            "first_contact_blocked": False,
            "guard_indicator_pending": False,
            "guard_indicator_emitted": False,
            "attacker_name": "",
            "victim_name": "",
        })

        a_mv   = _mv(a_slot)
        v_mv   = _mv(v_slot)
        prev_a = st["prev_a"]
        prev_v = st["prev_v"]
        st["prev_a"] = a_mv
        st["prev_v"] = v_mv

        if st["state"] == 0:
            if _is_actionable(a_mv) and _is_actionable(v_mv):
                st["combo_property_locked"] = False
                st["attack_guard_label"] = ""
                st["attack_move"] = ""
                st["first_contact_blocked"] = False
                st["guard_indicator_pending"] = False
                st["guard_indicator_emitted"] = False
            if _is_attacking(a_mv) and _is_stuck(v_mv):
                st["state"]      = 1
                st["first_end"]  = None
                st["first_slot"] = None
                _begin_adv_contact(st, a_slot, v_slot)

        elif st["state"] == 1:
            if bool(st.get("guard_indicator_pending", False)) and not bool(st.get("guard_indicator_emitted", False)):
                _emit_first_contact_badge(st, a_slot, v_slot)

            # New hit or move-id change  -  reset timer, stay tracking
            if _is_attacking(a_mv) and _is_stuck(v_mv) and (
                not _is_attacking(prev_a) or a_mv != prev_a
            ):
                st["first_end"]  = None
                st["first_slot"] = None
                _begin_adv_contact(st, a_slot, v_slot)
                if not _is_attacking(prev_a):
                    st["state"] = 0
                continue

            a_act = _is_actionable(a_mv)
            v_act = _is_actionable(v_mv)

            if a_act and v_act:
                _push_adv(a_slot,  0)
                _push_adv(v_slot,  0)
                _publish_interaction(a_slot, v_slot, st, 0)
                st["state"] = 0
            elif a_act:
                st["state"]      = 2
                st["first_end"]  = _frame
                st["first_slot"] = "A"
            elif v_act:
                st["state"]      = 2
                st["first_end"]  = _frame
                st["first_slot"] = "V"

        elif st["state"] == 2:
            # Attacker hit again before adv resolved  -  discard stale timer, restart
            if _is_attacking(a_mv) and _is_stuck(v_mv):
                st["first_end"]  = None
                st["first_slot"] = None
                st["state"]      = 1 if _is_attacking(prev_a) else 0
                continue

            # Attacker went idle for a frame but victim still stuck  -  they're in a
            # blockstring gap. Don't resolve yet; if they start attacking again,
            # state 1 above will catch it. If victim recovers first the module resolve below.
            if st["first_slot"] == "A" and _is_stuck(v_mv) and not _is_attacking(a_mv):
                # attacker is in gap  -  wait, don't commit yet
                continue

            if st["first_slot"] == "A" and _is_actionable(v_mv):
                diff = _frame - st["first_end"]
                _push_adv(a_slot,  diff)
                _push_adv(v_slot, -diff)
                _publish_interaction(a_slot, v_slot, st, diff)
                st["state"] = 0

            elif st["first_slot"] == "V" and _is_actionable(a_mv):
                diff = _frame - st["first_end"]
                _push_adv(a_slot, -diff)
                _push_adv(v_slot,  diff)
                _publish_interaction(a_slot, v_slot, st, -diff)
                st["state"] = 0

# ---------------------------------------------------------------------------
# Animation system
# ---------------------------------------------------------------------------

ANIM_SPEED = 10.0
FADE_SPEED = 6.0
PIP_SPEED  = 12.0
# HS Scale keeps its numeric/native countdown exact, but lets the blue body
# trail slightly while draining so short 15-25F windows remain readable.
_anim_state = {
    "overlay_alpha": 0.0,
    "slots": {},
    "teams": {},
    "assembly_age": 0.0,
    "assembly_active": False,
    "match_reset_armed": False,
    "last_match_reset_frame": -9999,
}

def _approach(current: float, target: float, speed: float, dt: float) -> float:
    if current < target:
        return min(current + speed * dt, target)
    else:
        return max(current - speed * dt, target)

def _ease_visual(current: float | None, target: float, responsiveness: float, dt: float) -> float:
    """Frame-rate independent easing for display-only gauges."""
    target = float(target)
    if current is None or not math.isfinite(float(current)):
        return target
    current = float(current)
    dt = max(0.0, min(0.10, float(dt or 0.0)))
    if dt <= 0.0:
        return target
    blend = 1.0 - math.exp(-max(0.1, float(responsiveness)) * dt)
    value = current + (target - current) * blend
    if abs(value - target) < 0.001:
        return target
    return value

def _get_slot_anim(slot_label: str):
    return _anim_state["slots"].setdefault(slot_label, {
        "alpha": 0.0,
        "meter_display": 0.0,
        "pip_values": [0.0] * 5,
        "present": False,
        "baroque_last_pct": 0.0,
        "baroque_display_pct": 0.0,
        "baroque_freeze_timer": 0,
        "baroque_prev_ready": False,
        "prev_hp": None,
        "last_hit_damage": 0,
        "damage_timer": 0,
        "damage_events": [],
        "adv_events": [],
        "prev_meter": None,
        "meter_events": [],
        "prev_move_label": "",
        "prev_move_id": None,
        "move_events": [],
        "move_scroll_px": 0.0,
        "prev_baroque_pct": None,
        "baroque_events": [],
        "input_history": [],
        "input_chips": [],
        "pending_input_chip_tokens": [],
        "pending_input_chip_start_frame": None,
        "pending_input_last_frame": None,
        "input_chip_break": False,
        "button_hold_active": {},
        "button_hold_events": [],
        "button_hold_seq": 0,
        "qualified_hold_mask": 0,
        "prev_input_state": None,
        "prev_visible_input_state": None,
        "prev_input_key": None,
        "last_input_frame": -9999,
        "last_input_sample_seq": 0,
        "last_input_timestamp_frame": -999999,
        "last_action_sample_seq": 0,
        "last_move_log_seq": 0,
        "damage_scale_visual_pct": None,
        "damage_scale_target_pct": None,
        "damage_scale_pulse": 0.0,
        "hs_visual_remaining": None,
        "hs_visual_effective": None,
        "hs_visual_elapsed": 0.0,
        "hs_visual_generation": -1,
        "hs_visual_target": 0,
        "hs_visual_last_frame": -1,
        "hs_visual_signature": None,
        "hs_visual_pulse": 0.0,
        "hs_contact_generation": 0,
        "hs_contact_start_ns": 0,
        "hs_contact_target": 0,
        "hs_contact_raw": 0,
        "hs_contact_loss": 0,
        "hs_contact_move": "",
        "bs_contact_generation": 0,
        "bs_contact_target": 0,
        "bs_contact_remaining": 0,
        "bs_contact_move": "",
        "hp_display_frac": None,
        "hp_trail_frac": None,
        "hp_trail_delay": 0.0,
        "partner_hp_display_frac": None,
        "meter_display_value": None,
        "meter_spend_sweep": 0.0,
        "meter_spend_amount": 0,
        "baroque_alpha": 0.0,
        "baroque_fade_direction": 0,
        "event_history": [],
        "prev_compact_move_key": "",
        "guard_indicator_label": "",
        "guard_indicator_result": "",
        "guard_indicator_life": 0.0,
        "guard_indicator_flash": 0.0,
        "ko_alpha": 0.0,
        "ko_scale": 0.90,
        "ko_punch": 0.0,
        "prev_dead": False,
        "hp_value_flash": 0.0,
        "baroque_change_flash": 0.0,
        "move_change_flash": 0.0,
        "stun_generation_flash": 0.0,
        "stun_expire_flash": 0.0,
        "bs_generation_flash": 0.0,
        "bs_expire_flash": 0.0,
        "prev_realtime_action_id": None,
    })


def _get_team_anim(team: str):
    return _anim_state["teams"].setdefault(team, {
        "alpha": 0.0,
        "slide_x": 0.0,
        "slide_y": -34.0,
        "present": False,
        "current_point_label": None,
        "swap_progress": 0.0,
        "move_history_signature": (),
        "move_history_prev": [],
        "move_history_slide": 0.0,
        "input_history_signature": (),
        "input_history_prev": [],
        "input_history_current": [],
        "input_history_slide": 0.0,
        "hold_history_signature": (),
        "hold_history_prev": [],
        "hold_history_current": [],
        "hold_history_slide": 0.0,
        "hold_expand": 0.0,
        "log_history_signature": (),
        "log_history_prev": [],
        "log_history_slide": 0.0,
        "pulse_life": 0.0,
        "pulse_color": (107, 154, 232),
        "sweep_pos": -0.25,
        "shake": 0.0,
        "sparks": [],
        "tag_card": None,
        "tag_lock_pending": False,
        "tag_lock_flash": 0.0,
        "impact_recoil_age": 1.0,
        "impact_recoil_power": 0.0,
        "entrance_age": 0.0,
        "entrance_active": False,
        "meter_gain_flash": 0.0,
        "meter_gain_start": 0.0,
        "meter_gain_end": 0.0,
        "meter_stock_pop": 0.0,
        "meter_stock_pop_index": -1,
        "meter_max_flash": 0.0,
        "meter_value_flash": 0.0,
        "prev_meter_target": None,
    })


def _push_event_history(
    slot_anim: dict,
    label: str,
    value: str,
    color: tuple[int, int, int],
    rainbow: bool = False,
) -> None:
    items = slot_anim["event_history"]
    items.insert(0, {
        "label": label,
        "value": value,
        "color": color,
        "life": 1.0,
        "rainbow": bool(rainbow),
    })
    del items[6:]


def _team_from_slot(slot_label: str) -> str:
    return "P1" if str(slot_label).startswith("P1") else "P2"


def _trigger_team_panel_fx(team: str, color: tuple[int, int, int], strength: float = 1.0, spark_count: int = 6) -> None:
    team_anim = _get_team_anim(team)
    strength = max(0.25, min(1.6, float(strength or 1.0)))
    team_anim["pulse_life"] = max(float(team_anim.get("pulse_life", 0.0)), min(0.72, (0.28 + 0.14 * strength) * OOMPH_SCALE / 0.40))
    team_anim["pulse_color"] = tuple(max(0, min(255, int(c))) for c in (color or (107, 154, 232)))
    team_anim["sweep_pos"] = -0.20
    team_anim["shake"] = max(float(team_anim.get("shake", 0.0)), min(0.42, (0.14 + 0.18 * strength) * OOMPH_SCALE / 0.40))
    sparks = team_anim.setdefault("sparks", [])
    for _ in range(max(1, int(round(spark_count * OOMPH_SCALE)))):
        sparks.append({
            "column": random.choice(("left", "right")),
            "x": random.uniform(0.06, 0.94),
            "y": random.uniform(0.12, 0.82),
            "vx": random.uniform(-0.22, 0.22),
            "vy": random.uniform(-0.22, 0.22),
            "life": random.uniform(0.45, 0.9),
            "size": random.uniform(1.2, 3.4),
            "color": team_anim["pulse_color"],
        })
    if len(sparks) > 24:
        del sparks[:-24]


def _trigger_impact_recoil(team: str, damage: int) -> None:
    """Nudge only the struck HUD side outward, then settle it immediately."""
    team_anim = _get_team_anim(team)
    damage_i = max(0, int(damage or 0))
    power = 2.0 + min(2.5, damage_i / 3200.0)
    team_anim["impact_recoil_age"] = 0.0
    team_anim["impact_recoil_power"] = max(float(team_anim.get("impact_recoil_power", 0.0)), power)


def _tick_team_panel_fx(team_anim: dict, dt: float) -> None:
    team_anim["pulse_life"] = max(0.0, float(team_anim.get("pulse_life", 0.0)) - dt * 2.25)
    team_anim["impact_recoil_age"] = min(1.0, float(team_anim.get("impact_recoil_age", 1.0)) + dt / 0.16)
    if float(team_anim.get("impact_recoil_age", 1.0)) >= 1.0:
        team_anim["impact_recoil_power"] = 0.0
    team_anim["tag_lock_flash"] = max(0.0, float(team_anim.get("tag_lock_flash", 0.0)) - dt * 5.5)
    team_anim["shake"] = max(0.0, float(team_anim.get("shake", 0.0)) - dt * 5.4)
    team_anim["sweep_pos"] = min(1.20, float(team_anim.get("sweep_pos", -0.25)) + dt * 1.30)
    updated = []
    for spark in list(team_anim.get("sparks", [])):
        life = max(0.0, float(spark.get("life", 0.0)) - dt * 1.6)
        if life <= 0.01:
            continue
        spark["life"] = life
        spark["x"] = float(spark.get("x", 0.0)) + float(spark.get("vx", 0.0)) * dt
        spark["y"] = float(spark.get("y", 0.0)) + float(spark.get("vy", 0.0)) * dt
        spark["vy"] = float(spark.get("vy", 0.0)) * 0.985
        if -0.2 <= float(spark.get("x", 0.0)) <= 1.2 and -0.2 <= float(spark.get("y", 0.0)) <= 1.2:
            updated.append(spark)
    team_anim["sparks"] = updated[-24:]


def _team_panel_fx_columns(x: int, y: int, width: int, height: int, scale: float) -> tuple[pygame.Rect, pygame.Rect]:
    inset_x = max(4, int(5 * scale))
    inset_y = max(6, int(8 * scale))
    col_w = max(max(18, int(22 * scale)), min(int(width * 0.12), int(56 * scale)))
    inner_h = max(8, height - inset_y * 2)
    left_rect = pygame.Rect(x + inset_x, y + inset_y, col_w, inner_h)
    right_rect = pygame.Rect(x + width - inset_x - col_w, y + inset_y, col_w, inner_h)
    return left_rect, right_rect


def _draw_team_panel_fx(screen, team_anim: dict, x: int, y: int, width: int, height: int, accent: tuple[int, int, int], scale: float, alpha: float) -> None:
    pulse = max(0.0, min(1.0, float(team_anim.get("pulse_life", 0.0))))
    if pulse <= 0.01 and not team_anim.get("sparks"):
        return
    fx_color = tuple(team_anim.get("pulse_color") or accent or (107, 154, 232))
    radius = max(6, int(7 * scale))
    left_col, right_col = _team_panel_fx_columns(x, y, width, height, scale)
    columns = (left_col, right_col)
    if pulse > 0.01:
        glow = pygame.Surface((width + int(18 * scale), height + int(18 * scale)), pygame.SRCALPHA)
        glow_rect = pygame.Rect(int(9 * scale), int(9 * scale), width, height)
        pygame.draw.rect(glow, (*fx_color, int(14 * pulse * alpha)), glow_rect, border_radius=radius + 3)
        pygame.draw.rect(glow, (*fx_color, int(31 * pulse * alpha)), glow_rect, 2, border_radius=radius + 3)
        screen.blit(glow, (x - int(9 * scale), y - int(9 * scale)))

        sweep = float(team_anim.get("sweep_pos", -0.25))
        if -0.05 <= sweep <= 1.20:
            for col_rect in columns:
                sheen = pygame.Surface((col_rect.width, col_rect.height), pygame.SRCALPHA)
                center_x = int((sweep * col_rect.width))
                band_w = max(10, int(col_rect.width * 0.90))
                poly = [
                    (center_x - band_w, 0),
                    (center_x + int(band_w * 0.40), 0),
                    (center_x - int(band_w * 0.40), col_rect.height),
                    (center_x - int(band_w * 1.40), col_rect.height),
                ]
                pygame.draw.polygon(sheen, (*fx_color, int(10 * pulse * alpha)), poly)
                pygame.draw.line(sheen, (255, 255, 255, int(7 * pulse * alpha)), (max(0, center_x - band_w), 2), (min(col_rect.width - 1, center_x + int(band_w * 0.25)), 2), 1)
                screen.blit(sheen, col_rect.topleft)

    for spark in list(team_anim.get("sparks", [])):
        life = max(0.0, min(1.0, float(spark.get("life", 0.0))))
        if life <= 0.01:
            continue
        col_rect = left_col if str(spark.get("column", "left")) == "left" else right_col
        sx = col_rect.x + int(float(spark.get("x", 0.0)) * col_rect.width)
        sy = col_rect.y + int(float(spark.get("y", 0.0)) * col_rect.height)
        size = max(1, int(float(spark.get("size", 1.0)) * scale * (0.70 + life * 0.55)))
        spark_color = tuple(spark.get("color") or fx_color)
        pygame.draw.circle(screen, (*spark_color, int(70 * life * alpha)), (sx, sy), size)
        pygame.draw.line(screen, (*spark_color, int(48 * life * alpha)), (sx - size, sy), (sx + size, sy), 1)



# ---------------------------------------------------------------------------
# DPI / Win32
# ---------------------------------------------------------------------------

def set_dpi_aware() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

set_dpi_aware()

def find_dolphin_hwnd() -> Optional[int]:
    candidates = []
    def score_title(t: str) -> int:
        tl = t.lower()
        if "dolphin" not in tl:
            return -10_000
        s = 0
        if "|" in t:
            s += 50 + min(30, t.count("|") * 5)
        for tok in ("jit", "jit64", "opengl", "vulkan", "d3d", "direct3d", "hle"):
            if tok in tl: s += 20
        if "(" in t and ")" in t: s += 30
        for bad in ("memory", "watch", "log", "breakpoint", "register", "disassembly", "config", "settings"):
            if bad in tl: s -= 25
        if t.count("|") >= 3: s += 20
        return s
    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd): return
        title = win32gui.GetWindowText(hwnd) or ""
        if not title or "dolphin" not in title.lower(): return
        candidates.append((score_title(title), hwnd, title))
    win32gui.EnumWindows(cb, None)
    if not candidates: return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]

def get_client_screen_rect(hwnd: int):
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    tl = win32gui.ClientToScreen(hwnd, (left, top))
    br = win32gui.ClientToScreen(hwnd, (right, bottom))
    return tl[0], tl[1], br[0] - tl[0], br[1] - tl[1]

def apply_overlay_style(hwnd: int) -> None:
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    style &= ~(win32con.WS_CAPTION | win32con.WS_THICKFRAME |
               win32con.WS_MINIMIZE | win32con.WS_MAXIMIZE | win32con.WS_SYSMENU)
    style |= win32con.WS_POPUP
    win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    ex |= win32con.WS_EX_LAYERED
    ex &= ~win32con.WS_EX_TOPMOST
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
    win32gui.SetLayeredWindowAttributes(hwnd, 0x000000, 0, win32con.LWA_COLORKEY)
    win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
        win32con.SWP_FRAMECHANGED | win32con.SWP_NOMOVE |
        win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)

def sync_overlay_to_dolphin(dolphin_hwnd: int, overlay_hwnd: int):
    x, y, w, h = get_client_screen_rect(dolphin_hwnd)
    win32gui.SetWindowPos(overlay_hwnd, win32con.HWND_NOTOPMOST, x, y, w, h, win32con.SWP_NOACTIVATE)
    return w, h

# ---------------------------------------------------------------------------
# Data reader
# ---------------------------------------------------------------------------

_last_data_signature: tuple[int, int, int, int] | None = None
_cached_slots: dict = {}
_last_realtime_input_signature: tuple[int, int, int, int] | None = None
_cached_realtime_inputs: dict = {}
_last_realtime_stun_signature: tuple[int, int, int, int] | None = None
_cached_realtime_stun: dict = {}
_last_realtime_moves_signature: tuple[int, int, int, int] | None = None
_cached_realtime_moves: dict = {}
# Per-slot frame-locked stun edge consumers. The producer preserves every
# native +0x1204/+0x1210/+0x1220 transition; the renderer consumes at most one
# new native edge per 60 Hz draw so short counters visibly advance 1F at a time.
_realtime_stun_edge_cursor: dict[str, int] = {}
_realtime_stun_edge_base: dict[str, int] = {}
_realtime_stun_edge_pending: dict[str, list[dict]] = {}
_realtime_stun_edge_display: dict[str, dict] = {}

def read_slot_data() -> dict:
    global _last_data_signature, _cached_slots
    try:
        stat = os.stat(DATA_FILE)
        signature = (
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            int(getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000))),
            int(stat.st_size),
            int(getattr(stat, "st_ino", 0)),
        )
        if signature != _last_data_signature:
            with open(DATA_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _cached_slots = loaded
                _last_data_signature = signature
    except Exception:
        pass
    return _cached_slots

def read_realtime_input_data() -> dict:
    """Read the low-latency input sidecar independently of the full HUD payload."""
    global _last_realtime_input_signature, _cached_realtime_inputs
    try:
        stat = os.stat(REALTIME_INPUT_FILE)
        signature = (
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            int(getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000))),
            int(stat.st_size),
            int(getattr(stat, "st_ino", 0)),
        )
        if signature != _last_realtime_input_signature:
            with open(REALTIME_INPUT_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _cached_realtime_inputs = loaded
                _last_realtime_input_signature = signature
    except Exception:
        pass
    return _cached_realtime_inputs

def read_realtime_stun_data() -> dict:
    """Read the tiny native-stun IPC independently of input history."""
    global _last_realtime_stun_signature, _cached_realtime_stun
    try:
        stat = os.stat(REALTIME_STUN_FILE)
        signature = (
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            int(getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000))),
            int(stat.st_size),
            int(getattr(stat, "st_ino", 0)),
        )
        if signature != _last_realtime_stun_signature:
            with open(REALTIME_STUN_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _cached_realtime_stun = loaded
                _last_realtime_stun_signature = signature
    except Exception:
        pass
    return _cached_realtime_stun


def read_realtime_move_data() -> dict:
    """Read the attack-only edge IPC.  This is intentionally separate from
    the per-frame combat/stun file so move chips can appear on the first render
    after the native edge is sampled.
    """
    global _last_realtime_moves_signature, _cached_realtime_moves
    try:
        stat = os.stat(REALTIME_MOVES_FILE)
        signature = (
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            int(getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000))),
            int(stat.st_size),
            int(getattr(stat, "st_ino", 0)),
        )
        if signature != _last_realtime_moves_signature:
            with open(REALTIME_MOVES_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _cached_realtime_moves = loaded
                _last_realtime_moves_signature = signature
    except Exception:
        pass
    return _cached_realtime_moves


def _consume_realtime_stun_edges(combat_slot_map: dict) -> dict[str, dict]:
    """Return one exact native stun sample per slot for this render frame.

    The IPC writer and this renderer both orbit the game's 60 Hz cadence. A
    latest-only file can race that boundary and make two native decrements look
    like one 2F jump. The producer now retains a short edge ring; this consumer
    drains one unseen edge per draw. No wall-clock interpolation is used, so
    Dolphin pause and frame-step remain authoritative.
    """
    out: dict[str, dict] = {}
    if not isinstance(combat_slot_map, dict):
        return out
    for raw_slot, state in combat_slot_map.items():
        slot = str(raw_slot or "")
        if not slot or not isinstance(state, dict):
            continue
        latest = state.get("latest") if isinstance(state.get("latest"), dict) else {}
        samples = state.get("stun_samples") if isinstance(state.get("stun_samples"), list) else []
        clean_samples = []
        for item in samples:
            if not isinstance(item, dict):
                continue
            try:
                sample_ns = int(item.get("sample_ns", 0) or 0)
            except Exception:
                sample_ns = 0
            if sample_ns > 0:
                clean_samples.append((sample_ns, dict(item)))
        clean_samples.sort(key=lambda pair: pair[0])
        try:
            latest_base = int(latest.get("base", 0) or 0)
        except Exception:
            latest_base = 0
        previous_base = int(_realtime_stun_edge_base.get(slot, 0) or 0)
        if latest_base and previous_base and latest_base != previous_base:
            _realtime_stun_edge_cursor.pop(slot, None)
            _realtime_stun_edge_pending.pop(slot, None)
            _realtime_stun_edge_display.pop(slot, None)
        if latest_base:
            _realtime_stun_edge_base[slot] = latest_base

        cursor = _realtime_stun_edge_cursor.get(slot)
        if cursor is None:
            # First sight of a slot starts at current native state rather than
            # replaying stale history from before the overlay opened.
            if clean_samples:
                sample_ns, item = clean_samples[-1]
                _realtime_stun_edge_cursor[slot] = sample_ns
                _realtime_stun_edge_display[slot] = dict(item)
            elif latest:
                _realtime_stun_edge_display[slot] = dict(latest)
            out[slot] = dict(_realtime_stun_edge_display.get(slot) or latest)
            continue

        pending = _realtime_stun_edge_pending.setdefault(slot, [])
        queued_ns = {int(item.get("sample_ns", 0) or 0) for item in pending if isinstance(item, dict)}
        for sample_ns, item in clean_samples:
            if sample_ns > int(cursor) and sample_ns not in queued_ns:
                pending.append(dict(item))
                queued_ns.add(sample_ns)
        pending.sort(key=lambda item: int(item.get("sample_ns", 0) or 0))
        if pending:
            item = pending.pop(0)
            sample_ns = int(item.get("sample_ns", 0) or 0)
            _realtime_stun_edge_cursor[slot] = sample_ns
            _realtime_stun_edge_display[slot] = dict(item)
        elif slot not in _realtime_stun_edge_display and latest:
            _realtime_stun_edge_display[slot] = dict(latest)
        out[slot] = dict(_realtime_stun_edge_display.get(slot) or latest)
    return out


def _merge_realtime_inputs(slots: dict, realtime_payload: dict) -> None:
    """Overlay realtime inputs plus the independent native-stun transport."""
    if not isinstance(slots, dict) or not isinstance(realtime_payload, dict):
        return
    slot_map = realtime_payload.get("slots")
    if not isinstance(slot_map, dict):
        slot_map = {}
    stun_payload = read_realtime_stun_data()
    combat_payload = stun_payload if isinstance(stun_payload, dict) and stun_payload else realtime_payload
    combat_slot_map = combat_payload.get("slots") if isinstance(combat_payload, dict) else None
    if not isinstance(combat_slot_map, dict):
        combat_slot_map = slot_map

    # MOVES bypasses the busy combat/stun transport.  Overlay the tiny
    # attack-only ring onto a shallow copy of the combat map.  Guard by fighter
    # base so a stale file from a previous match/tag can never repaint history.
    combat_slot_map = {
        key: dict(value) if isinstance(value, dict) else {}
        for key, value in (combat_slot_map or {}).items()
    }
    move_payload = read_realtime_move_data()
    move_slots = move_payload.get("slots") if isinstance(move_payload, dict) else None
    if isinstance(move_slots, dict):
        for move_slot, move_state in move_slots.items():
            if not isinstance(move_state, dict):
                continue
            snap = slots.get(move_slot) if isinstance(slots, dict) else None
            try:
                move_base = int(move_state.get("base", 0) or 0)
                snap_base = int((snap or {}).get("base", 0) or 0)
            except Exception:
                move_base = snap_base = 0
            if move_base and snap_base and move_base != snap_base:
                continue
            state = combat_slot_map.setdefault(move_slot, {})
            raw_log = move_state.get("move_log")
            if isinstance(raw_log, list):
                state["move_log"] = [dict(item) for item in raw_log if isinstance(item, dict)]

    frame_stun_samples = _consume_realtime_stun_edges(combat_slot_map)

    now_ns = time.monotonic_ns()
    max_age_ns = 2_000_000_000
    for slot_label, realtime_state in slot_map.items():
        snap = slots.get(slot_label)
        if not isinstance(snap, dict) or not isinstance(realtime_state, dict):
            continue
        latest = realtime_state.get("latest")
        samples = realtime_state.get("samples")
        if not isinstance(latest, dict):
            latest = {}
        if not isinstance(samples, list):
            samples = []

        fresh_samples = []
        for item in samples:
            if not isinstance(item, dict):
                continue
            try:
                sample_ns = int(item.get("sample_ns", 0) or 0)
            except Exception:
                sample_ns = 0
            if sample_ns <= 0 or now_ns - sample_ns <= max_age_ns:
                fresh_samples.append(item)

        try:
            latest_ns = int(latest.get("sample_ns", 0) or 0)
        except Exception:
            latest_ns = 0
        latest_fresh = latest_ns <= 0 or now_ns - latest_ns <= max_age_ns
        if latest_fresh and latest:
            snap["input_held"] = int(latest.get("held", snap.get("input_held", 0)) or 0) & 0xFFFF
            snap["input_pressed"] = int(latest.get("pressed", 0) or 0) & 0xFFFF
            snap["input_released"] = int(latest.get("released", 0) or 0) & 0xFFFF
            # Inputs/actions use newest 240 Hz state. Stun counters use the
            # frame-locked edge consumer so the visible number changes by one
            # native frame per 60 Hz draw instead of occasionally jumping 2F.
            stun_latest = frame_stun_samples.get(str(slot_label)) or latest
            snap["realtime_blockstun_remaining"] = max(0, int(stun_latest.get("blockstun_remaining", 0) or 0))
            # HS/AIR HS are reset-sensitive. Never replay an older queued
            # counter after a new cancel hit has already replaced the native
            # timer; use the newest 240 Hz packet directly.
            snap["realtime_hitstun_remaining"] = max(0, int(latest.get("hitstun_remaining", 0) or 0))
            snap["realtime_untech_remaining"] = max(0, int(latest.get("untech_remaining", 0) or 0))
            snap["realtime_impact_freeze_remaining"] = max(0, int(latest.get("impact_freeze_remaining", 0) or 0))
            snap["realtime_combat_sample_ns"] = int(latest.get("sample_ns", latest_ns) or latest_ns)
            try:
                realtime_action_id = int(latest.get("action_id", snap.get("mv_id_display", 0)) or 0) & 0x7FFF
                realtime_action_frame = max(0, int(latest.get("action_frame", 0) or 0))
                realtime_char_id = int(latest.get("char_id", snap.get("id", 0)) or 0)
                realtime_label = realtime_action_name(realtime_action_id, realtime_char_id) if realtime_action_id else ""
                snap["realtime_action_id"] = realtime_action_id
                snap["realtime_action_frame"] = realtime_action_frame
                snap["realtime_char_id"] = realtime_char_id
                snap["realtime_action_label"] = realtime_label
                if realtime_action_id:
                    # History identity follows the native 240 Hz action edge.
                    # Richer profiler labels may still rewrite the chip later.
                    snap["mv_id_display"] = realtime_action_id
                    if realtime_label:
                        snap["mv_label"] = realtime_label
            except Exception:
                pass
        if fresh_samples:
            snap["input_samples"] = fresh_samples

    # Health and team meter are authoritative resources, just like the native
    # stun clocks. Pull them from the independent 240 Hz combat transport so
    # their primary HUD geometry does not wait for the slower full payload.
    # Do not age these values out on wall time: if Dolphin is paused, the last
    # native resource sample is still the correct game state. A fighter-base
    # match prevents stale IPC from a previous match/process from overriding
    # a newly resolved slot.
    realtime_team_meter = {}
    for slot_label, combat_state in combat_slot_map.items():
        snap = slots.get(slot_label)
        latest = combat_state.get("latest") if isinstance(combat_state, dict) else None
        if not isinstance(snap, dict) or not isinstance(latest, dict) or not latest:
            continue
        try:
            live_base = int(latest.get("base", 0) or 0)
            snap_base = int(snap.get("base", 0) or 0)
        except Exception:
            live_base = snap_base = 0
        if live_base and snap_base and live_base != snap_base:
            continue
        if "current_hp" in latest:
            try:
                live_hp = max(0, int(latest.get("current_hp", 0) or 0))
                snap["realtime_current_hp"] = live_hp
                snap["cur"] = live_hp
            except Exception:
                pass
        # The tiny combat sidecar updates on native action/action-frame edges,
        # so move history does not wait for the slower full HUD payload.
        try:
            live_action = int(latest.get("action_id", 0) or 0) & 0x7FFF
            live_action_frame = max(0, int(latest.get("action_frame", 0) or 0))
            live_char_id = int(latest.get("char_id", snap.get("id", 0)) or 0)
            live_label = realtime_action_name(live_action, live_char_id) if live_action else ""
            snap["realtime_action_id"] = live_action
            snap["realtime_action_frame"] = live_action_frame
            snap["realtime_char_id"] = live_char_id
            snap["realtime_action_label"] = live_label

            # FD timing now travels with the native action sample.  This lets
            # the rail arm on action frame 0/1 instead of waiting for the
            # slower full scanner/profile payload to identify the move again.
            if "fd_available" in latest and "fd_action_id" in latest:
                try:
                    fd_action_id = int(latest.get("fd_action_id", 0) or 0) & 0x7FFF
                except Exception:
                    fd_action_id = 0
                if fd_action_id == live_action:
                    fd_available = bool(latest.get("fd_available", False))
                    snap["realtime_fd_action_id"] = fd_action_id
                    snap["realtime_fd_available"] = fd_available
                    if fd_available:
                        windows = []
                        for pair in (latest.get("fd_active_windows") or []):
                            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                                continue
                            try:
                                start_i = int(pair[0])
                                end_i = int(pair[1])
                            except (TypeError, ValueError):
                                continue
                            if start_i >= 0 and end_i >= start_i:
                                windows.append([start_i, end_i])
                        snap["startup"] = max(0, int(latest.get("fd_startup", 0) or 0))
                        snap["active_start"] = latest.get("fd_active_start")
                        snap["active_end"] = latest.get("fd_active_end")
                        snap["active_windows"] = windows
                        snap["recovery"] = max(0, int(latest.get("fd_recovery", 0) or 0))
                    else:
                        # Clear only stale timing from a different action. If
                        # the full payload has already caught up to this same
                        # action, it remains a valid fallback.
                        try:
                            full_timing_action = int(snap.get("move_metadata_action_id", 0) or 0) & 0x7FFF
                        except Exception:
                            full_timing_action = 0
                        if full_timing_action != live_action:
                            snap["startup"] = None
                            snap["active_start"] = None
                            snap["active_end"] = None
                            snap["active_windows"] = []
                            snap["recovery"] = None

            raw_action_samples = combat_state.get("actions") if isinstance(combat_state, dict) else None
            if isinstance(raw_action_samples, list):
                fresh_action_samples = []
                for item in raw_action_samples:
                    if not isinstance(item, dict):
                        continue
                    try:
                        item_ns = int(item.get("sample_ns", 0) or 0)
                    except Exception:
                        item_ns = 0
                    if item_ns <= 0 or now_ns - item_ns <= max_age_ns:
                        fresh_action_samples.append(dict(item))
                snap["realtime_action_samples"] = fresh_action_samples[-24:]

            # Unlike the transient combat/action samples above, this is an
            # authoritative committed history ring.  Never age entries out
            # before the renderer consumes them; a short A>B>C>special chain
            # must survive even if the HUD misses several draw frames.
            raw_move_log = combat_state.get("move_log") if isinstance(combat_state, dict) else None
            if isinstance(raw_move_log, list):
                snap["realtime_move_log"] = [
                    dict(item) for item in raw_move_log[-96:] if isinstance(item, dict)
                ]
            if live_action:
                snap["mv_id_display"] = live_action
                # Never pair a new native action ID with a stale old label. If
                # the lookup has no name yet, use the exact action ID for attack
                # actions and let the profiler rewrite it later.
                if live_label:
                    snap["mv_label"] = live_label
                elif live_action >= 0x100:
                    snap["mv_label"] = f"0x{live_action:04X}"
                else:
                    snap["mv_label"] = ""
        except Exception:
            pass
        if str(slot_label).endswith("-C1") and "current_meter" in latest:
            try:
                live_meter = max(0, min(200000, int(latest.get("current_meter", 0) or 0)))
                main_meter = max(0, min(200000, int(snap.get("meter", 0) or 0)))
                team = str(slot_label).split("-", 1)[0]
                # Positive realtime values are safe to promote immediately. A
                # transient/stale realtime zero must not erase a known nonzero
                # main snapshot. When the authoritative main payload also
                # reaches zero, zero is accepted normally on the next merge.
                if live_meter > 0 or main_meter <= 0:
                    realtime_team_meter[team] = live_meter
            except Exception:
                pass

    # Meter is team-owned at the C1 bank, but the compact panel may currently
    # be drawing C2 as the point character. Mirror the realtime team value onto
    # both visible fighter snapshots so point swaps cannot reintroduce latency.
    for team, live_meter in realtime_team_meter.items():
        for suffix in ("C1", "C2"):
            snap = slots.get(f"{team}-{suffix}")
            if isinstance(snap, dict):
                snap["realtime_meter"] = live_meter
                snap["meter"] = live_meter

    # Contact is still identified by the 240 Hz HP edge, but duration comes
    # from the victim's native +0x1210/+0x1220 values already present on that
    # same sample. Each render enriches the immutable contact event with the
    # victim's newest native remaining counter so hitstop and other holds are
    # reflected exactly instead of approximated from wall-clock time.
    hs_teams = combat_payload.get("hs_teams") if isinstance(combat_payload, dict) else None
    if isinstance(hs_teams, dict):
        for team, hs_state in hs_teams.items():
            if team not in {"P1", "P2"} or not isinstance(hs_state, dict):
                continue
            latest_hs = hs_state.get("latest")
            if not isinstance(latest_hs, dict):
                latest_hs = {}
            try:
                hs_ns = int(latest_hs.get("sample_ns", 0) or 0)
            except Exception:
                hs_ns = 0
            hs_fresh = bool(latest_hs) and (hs_ns <= 0 or now_ns - hs_ns <= max_age_ns)

            enriched_hs = dict(latest_hs) if hs_fresh else {}
            if enriched_hs:
                victim_slot = str(enriched_hs.get("victim_slot") or "")
                victim_state = combat_slot_map.get(victim_slot)
                # HS/AIR HS must follow the newest native victim state.  The
                # old frame-edge playback queue could lag behind a cancel and
                # feed the new contact generation values from the previous hit.
                # Keep that queue for presentation-sensitive BS, but never use
                # stale replay data to drive hitstun.
                victim_latest = victim_state.get("latest") if isinstance(victim_state, dict) else None
                if not isinstance(victim_latest, dict) or not victim_latest:
                    victim_latest = frame_stun_samples.get(victim_slot)
                if isinstance(victim_latest, dict):
                    try:
                        victim_ns = int(victim_latest.get("sample_ns", 0) or 0)
                    except Exception:
                        victim_ns = 0
                    victim_fresh = victim_ns <= 0 or now_ns - victim_ns <= max_age_ns
                    if victim_fresh:
                        enriched_hs["native_hitstun_current"] = max(0, int(victim_latest.get("hitstun_remaining", 0) or 0))
                        enriched_hs["native_untech_current"] = max(0, int(victim_latest.get("untech_remaining", 0) or 0))
                        enriched_hs["native_current_sample_ns"] = victim_ns
                        # Some launch routes publish +0x1210 on the contact
                        # packet and arm +0x1220 one native frame later.  Let
                        # the same hit generation promote itself to AIR HS so
                        # it cannot get stuck displaying the ground clock.
                        if (
                            str(enriched_hs.get("clock_source") or "") == "hitstun"
                            and int(enriched_hs.get("native_untech_current", 0) or 0) > 0
                        ):
                            promoted = int(enriched_hs.get("native_untech_current", 0) or 0)
                            loss = max(0, int(enriched_hs.get("decay_frames", 0) or 0))
                            enriched_hs["clock_source"] = "untech"
                            enriched_hs["native_untech"] = promoted
                            enriched_hs["target"] = promoted
                            enriched_hs["raw_estimate"] = promoted + loss
                            enriched_hs["source_promoted"] = True

            for suffix in ("C1", "C2"):
                team_snap = slots.get(f"{team}-{suffix}")
                if not isinstance(team_snap, dict):
                    continue
                if enriched_hs:
                    team_snap["realtime_hs_contact"] = dict(enriched_hs)
                else:
                    team_snap.pop("realtime_hs_contact", None)

    # Blockstun uses its own native +0x1204 contact generation. Unlike hitstun,
    # blocked contacts do not require HP loss, so the manager mints them from
    # +0x1204 plus the impact-freeze re-arm edge. The current counter is merged
    # here every render so Dolphin pause/frame-step controls the entire gauge.
    bs_teams = combat_payload.get("bs_teams") if isinstance(combat_payload, dict) else None
    # The normal BS token is attacker-owned and drives blockstring/gap timing.
    # Rebuild a separate victim-owned mirror every render for punish training.
    for _slot_snap in slots.values():
        if isinstance(_slot_snap, dict):
            _slot_snap.pop("realtime_punish_blockstun_contact", None)
    if isinstance(bs_teams, dict):
        for team, bs_state in bs_teams.items():
            if team not in {"P1", "P2"} or not isinstance(bs_state, dict):
                continue
            latest_bs = bs_state.get("latest")
            if not isinstance(latest_bs, dict):
                latest_bs = {}
            try:
                bs_ns = int(latest_bs.get("sample_ns", 0) or 0)
            except Exception:
                bs_ns = 0
            # Do not wall-clock-expire a native blockstun token. During Dolphin
            # pause/frame-step the sampler correctly stops producing new game
            # state, so an old contact timestamp is expected while +0x1204 is
            # frozen. The manager clears the token only on native +0x1204 == 0
            # or replaces it on the next blocked contact.
            enriched_bs = dict(latest_bs) if latest_bs else {}
            if enriched_bs:
                victim_slot = str(enriched_bs.get("victim_slot") or "")
                victim_state = combat_slot_map.get(victim_slot)
                victim_latest = frame_stun_samples.get(victim_slot)
                if not isinstance(victim_latest, dict) or not victim_latest:
                    victim_latest = victim_state.get("latest") if isinstance(victim_state, dict) else None
                if isinstance(victim_latest, dict):
                    try:
                        victim_ns = int(victim_latest.get("sample_ns", 0) or 0)
                    except Exception:
                        victim_ns = 0
                    # Same rule for the current native value: a paused game
                    # intentionally leaves this sample old in wall time. Treat
                    # it as authoritative until a new game-state sample arrives.
                    enriched_bs["native_blockstun_current"] = max(0, int(victim_latest.get("blockstun_remaining", 0) or 0))
                    enriched_bs["native_current_sample_ns"] = victim_ns

            for suffix in ("C1", "C2"):
                team_snap = slots.get(f"{team}-{suffix}")
                if not isinstance(team_snap, dict):
                    continue
                if enriched_bs:
                    team_snap["realtime_blockstun_contact"] = dict(enriched_bs)
                else:
                    team_snap.pop("realtime_blockstun_contact", None)

            if enriched_bs:
                victim_slot = str(enriched_bs.get("victim_slot") or "")
                victim_snap = slots.get(victim_slot)
                if isinstance(victim_snap, dict):
                    victim_snap["realtime_punish_blockstun_contact"] = dict(enriched_bs)

# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def make_font(size: int, bold: bool = True) -> pygame.font.Font:
    try:
        return pygame.font.SysFont("consolas", max(8, size), bold=bold)
    except Exception:
        return pygame.font.Font(None, max(8, size))

def _countdown_font(size: int, bold: bool = True) -> pygame.font.Font:
    key = (max(8, int(size)), bool(bold))
    font = _COUNTDOWN_FONT_CACHE.get(key)
    if font is None:
        font = make_font(key[0], bold=key[1])
        _COUNTDOWN_FONT_CACHE[key] = font
    return font


def _punish_badge_anchor(
    screen: pygame.Surface,
    scale: float,
    slot: str,
    badge_w: int,
    badge_h: int,
) -> tuple[int, int, int]:
    team = "P1" if str(slot).upper().startswith("P1") else "P2"
    is_left = team == "P1"
    margin_x = max(8, int(12 * scale))
    panel_y = int(144 * scale)
    gap = max(5, int(7 * scale))
    x = margin_x if is_left else screen.get_width() - margin_x - badge_w
    y = max(8, panel_y - badge_h - gap)
    direction = -1 if is_left else 1
    return x, y, direction


def _draw_punish_countdown(screen: pygame.Surface, scale: float) -> None:
    data = _punish_overlay if isinstance(_punish_overlay, dict) else {}
    anim = _PUNISH_BADGE_ANIM
    dt = 1.0 / 60.0

    requested_visible = bool(data.get("visible", False))
    if requested_visible:
        anim["last_payload"] = dict(data)
        anim["last_slot"] = str(data.get("slot") or anim.get("last_slot") or "P2-C1")
    payload = data if requested_visible else anim.get("last_payload", {})
    if not isinstance(payload, dict):
        payload = {}

    target_alpha = 1.0 if requested_visible else 0.0
    anim["alpha"] = _approach(float(anim.get("alpha", 0.0)), target_alpha, 8.5 if requested_visible else 6.8, dt)
    if float(anim["alpha"]) <= 0.01 and not requested_visible:
        anim["last_token"] = ""
        anim["pop"] = 0.0
        anim["shine"] = 0.0
        return

    phase = str(payload.get("phase") or "off")
    try:
        remaining = max(0.0, float(payload.get("remaining") or 0.0))
    except Exception:
        remaining = 0.0
    try:
        scheduled = max(0.0, float(payload.get("scheduled_interval") or 0.0))
    except Exception:
        scheduled = 0.0

    attacking = phase != "cooldown" or remaining <= 0.02
    count_text = "" if attacking else str(max(1, int(math.ceil(remaining))))
    slot = str(payload.get("slot") or anim.get("last_slot") or "P2-C1").strip()
    move = str(payload.get("move_label") or "Move").strip()
    random_mode = bool(payload.get("random_interval", False))
    token = f"{slot}|{move}|{phase}|{count_text}|{int(attacking)}"

    if requested_visible and token != str(anim.get("last_token") or ""):
        anim["last_token"] = token
        anim["pop"] = 1.0
        anim["shine"] = 1.0
        _x, _y, direction = _punish_badge_anchor(screen, scale, slot, 1, 1)
        anim["slide_px"] = float(direction * max(12, int(18 * scale)))

    anim["slide_px"] = _approach(float(anim.get("slide_px", 0.0)), 0.0 if requested_visible else (8.0 if slot.startswith("P2") else -8.0), 125.0, dt)
    anim["pop"] = _approach(float(anim.get("pop", 0.0)), 0.0, 4.8, dt)
    anim["shine"] = _approach(float(anim.get("shine", 0.0)), 0.0, 2.7, dt)

    badge_w = max(184, int(218 * scale))
    badge_h = max(42, int(50 * scale))
    accent = (255, 111, 82) if attacking else SLOT_COLORS.get(slot, (91, 181, 255))
    bg_top = (14, 20, 31, 242)
    bg_bottom = (7, 12, 21, 238)

    badge = pygame.Surface((badge_w, badge_h), pygame.SRCALPHA)
    radius = max(7, int(9 * scale))
    pygame.draw.rect(badge, bg_bottom, badge.get_rect(), border_radius=radius)
    pygame.draw.rect(badge, bg_top, (1, 1, badge_w - 2, max(16, badge_h // 2)), border_radius=radius)
    pygame.draw.rect(badge, (*accent, 235), badge.get_rect(), width=max(1, int(1.5 * scale)), border_radius=radius)

    strip_w = max(4, int(5 * scale))
    pygame.draw.rect(badge, (*accent, 245), (0, radius, strip_w, badge_h - radius * 2), border_radius=max(2, strip_w // 2))

    title_font = _countdown_font(max(9, int(10 * scale)), True)
    move_font = _countdown_font(max(8, int(9 * scale)), False)
    count_font = _countdown_font(max(18, int(24 * scale)), True)
    chip_font = _countdown_font(max(7, int(8 * scale)), True)

    pad_x = max(10, int(12 * scale))
    number_area_w = max(42, int(48 * scale))
    text_right = badge_w - number_area_w - max(7, int(8 * scale))

    chip_text = slot.replace("-", " ")
    chip_surf = chip_font.render(chip_text, True, (205, 218, 235))
    chip_pad_x = max(5, int(6 * scale))
    chip_pad_y = max(2, int(2 * scale))
    chip_rect = pygame.Rect(pad_x, max(5, int(6 * scale)), chip_surf.get_width() + chip_pad_x * 2, chip_surf.get_height() + chip_pad_y * 2)
    pygame.draw.rect(badge, (255, 255, 255, 18), chip_rect, border_radius=max(4, int(5 * scale)))
    badge.blit(chip_surf, (chip_rect.x + chip_pad_x, chip_rect.y + chip_pad_y))

    title_text = "ATTACKING" if attacking else "PUNISH IN"
    title_surf = title_font.render(title_text, True, accent)
    title_x = chip_rect.right + max(6, int(7 * scale))
    title_y = max(6, int(7 * scale))
    if title_x + title_surf.get_width() > text_right:
        title_x = pad_x
        title_y = chip_rect.bottom + 1
    badge.blit(title_surf, (title_x, title_y))

    move_max_w = max(60, text_right - pad_x)
    move_text = _compact_fit_text(move_font, move, move_max_w)
    move_surf = move_font.render(move_text, True, (194, 207, 225))
    move_y = badge_h - move_surf.get_height() - max(6, int(7 * scale))
    badge.blit(move_surf, (pad_x, move_y))

    orb_size = max(32, int(38 * scale))
    orb = pygame.Rect(badge_w - orb_size - max(7, int(8 * scale)), (badge_h - orb_size) // 2, orb_size, orb_size)
    pygame.draw.ellipse(badge, (*accent, 32), orb)
    pygame.draw.ellipse(badge, (*accent, 210), orb, width=max(1, int(1.5 * scale)))

    if attacking:
        attack_font = _countdown_font(max(13, int(15 * scale)), True)
        attack_surf = attack_font.render("!", True, (250, 252, 255))
        badge.blit(attack_surf, attack_surf.get_rect(center=orb.center))
        sweep = (float(_frame) * 2.7) % (orb.width + max(8, int(10 * scale)))
        for offset in (-9, 0, 9):
            sx = int(orb.x + sweep + offset)
            pygame.draw.line(badge, (*accent, 100), (sx, orb.bottom - 5), (sx + 7, orb.y + 5), max(1, int(2 * scale)))
    else:
        count_surf = count_font.render(count_text, True, (248, 251, 255))
        badge.blit(count_surf, count_surf.get_rect(center=(orb.centerx, orb.centery - 1)))

    if scheduled > 0.0 and not attacking:
        progress = max(0.0, min(1.0, 1.0 - remaining / scheduled))
        track_x = pad_x
        track_w = max(20, text_right - pad_x)
        track_h = max(2, int(3 * scale))
        track_y = badge_h - track_h
        pygame.draw.rect(badge, (39, 52, 71, 220), (track_x, track_y, track_w, track_h), border_radius=track_h // 2)
        fill_w = max(1, int(track_w * progress))
        pygame.draw.rect(badge, (*accent, 235), (track_x, track_y, fill_w, track_h), border_radius=track_h // 2)

    if random_mode and not attacking:
        dot_r = max(2, int(2 * scale))
        dot_x = max(pad_x, text_right - max(4, int(5 * scale)))
        pygame.draw.circle(badge, (124, 231, 184), (dot_x, max(7, int(8 * scale))), dot_r)

    shine_strength = max(0.0, min(1.0, float(anim.get("shine", 0.0))))
    if shine_strength > 0.01:
        shine_w = max(18, int(30 * scale))
        shine_x = int((1.0 - shine_strength) * (badge_w + shine_w)) - shine_w
        shine = pygame.Surface((shine_w, badge_h), pygame.SRCALPHA)
        for px in range(shine_w):
            dist = abs(px - shine_w / 2) / max(1.0, shine_w / 2)
            alpha = int(45 * (1.0 - dist) * shine_strength)
            pygame.draw.line(shine, (255, 255, 255, alpha), (px, 0), (px, badge_h))
        badge.blit(shine, (shine_x, 0), special_flags=pygame.BLEND_RGBA_ADD)

    pop = max(0.0, min(1.0, float(anim.get("pop", 0.0))))
    pulse = 1.0 + (0.055 if attacking else 0.035) * math.sin(float(_frame) * 0.42) * min(1.0, pop + (0.45 if attacking else 0.0))
    pulse += 0.045 * pop
    draw_w = max(1, int(badge_w * pulse))
    draw_h = max(1, int(badge_h * pulse))
    if draw_w != badge_w or draw_h != badge_h:
        badge = pygame.transform.smoothscale(badge, (draw_w, draw_h))

    alpha = max(0, min(255, int(255 * float(anim.get("alpha", 0.0)))))
    badge.set_alpha(alpha)

    anchor_x, anchor_y, _direction = _punish_badge_anchor(screen, scale, slot, badge_w, badge_h)
    x = anchor_x + int(float(anim.get("slide_px", 0.0))) - (draw_w - badge_w) // 2
    y = anchor_y - (draw_h - badge_h) // 2

    shadow = pygame.Surface((draw_w + 10, draw_h + 10), pygame.SRCALPHA)
    shadow_alpha = int(88 * float(anim.get("alpha", 0.0)))
    pygame.draw.rect(shadow, (0, 0, 0, shadow_alpha), (5, 5, draw_w, draw_h), border_radius=radius + 2)
    screen.blit(shadow, (x - 5, y - 5))
    screen.blit(badge, (x, y))


def _draw_hp_bar(screen, x, y, bar_w, bar_h, hp_cur, hp_max, is_dead):
    pygame.draw.rect(screen, COL_HP_BG, (x, y, bar_w, bar_h), border_radius=2)
    if hp_max and hp_max > 0:
        frac = max(0.0, min(1.0, hp_cur / hp_max))
        fill_w = max(1, int(bar_w * frac))
        bar_col = COL_HP_DEAD if is_dead else (COL_HP_LOW if frac <= 0.30 else COL_HP_HIGH)
        pygame.draw.rect(screen, bar_col, (x, y, fill_w, bar_h), border_radius=2)
        if not is_dead:
            flash = pygame.Surface((fill_w, bar_h), pygame.SRCALPHA)
            flash.fill((255, 255, 255, 18))
            screen.blit(flash, (x, y))

def _draw_meter_pips_animated(screen, x, y, pip_w, pip_h, pip_gap, slot_anim, is_dead):
    meter_val = slot_anim["meter_display"]
    for i in range(5):
        px = x + i * (pip_w + pip_gap)
        target = 1.0 if i < int(meter_val) else 0.0
        slot_anim["pip_values"][i] = _approach(slot_anim["pip_values"][i], target, PIP_SPEED, 1/60.0)
        v = slot_anim["pip_values"][i]
        scale = 0.75 + 0.25 * v
        w = int(pip_w * scale); h = int(pip_h * scale)
        ox = (pip_w - w) // 2; oy = (pip_h - h) // 2
        col = COL_METER_EMPTY if is_dead else tuple(
            int(COL_METER_EMPTY[c] + (COL_METER_FULL[c] - COL_METER_EMPTY[c]) * v) for c in range(3))
        pygame.draw.rect(screen, col, (px + ox, y + oy, w, h), border_radius=1)

def _draw_divider(screen, x, y, row_h, scale, alpha=220):
    w = max(2, int(2 * scale)); h = int(row_h * 0.7); dy = (row_h - h) // 2
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    surf.fill((220, 220, 220, alpha))
    screen.blit(surf, (x, y + dy))
    glow = pygame.Surface((w + 2, h + 4), pygame.SRCALPHA)
    glow.fill((180, 180, 180, 80))
    screen.blit(glow, (x - 1, y + dy - 2))

def _draw_gradient_frame(screen, rect, top_color, pulse=1.0):
    x, y, w, h = rect
    if w <= 2 or h <= 2:
        return

    border = max(1, int(round(pulse)))
    frame = pygame.Surface((w, h), pygame.SRCALPHA)

    for yy in range(h):
        t = yy / max(1, h - 1)
        r = int(top_color[0] * (1.0 - t))
        g = int(top_color[1] * (1.0 - t))
        b = int(top_color[2] * (1.0 - t))
        a = int(170 + 55 * pulse)
        col = (r, g, b, a)

        if yy < border:
            pygame.draw.line(frame, col, (0, yy), (w - 1, yy))
        elif yy >= h - border:
            pygame.draw.line(frame, col, (0, yy), (w - 1, yy))
        else:
            pygame.draw.line(frame, col, (0, yy), (border - 1, yy))
            pygame.draw.line(frame, col, (w - border, yy), (w - 1, yy))

    glow = pygame.Surface((w + 6, h + 6), pygame.SRCALPHA)
    glow_col = (*top_color, int(45 + 45 * pulse))
    pygame.draw.rect(glow, glow_col, (3, 3, w, h), width=max(1, border), border_radius=3)

    screen.blit(glow, (x - 3, y - 3))
    screen.blit(frame, (x, y))


def _hud_brighten(color, amount: int = 0):
    """Overlay-local RGB brighten helper used by compact HUD styling."""
    try:
        amt = int(amount)
        values = tuple(int(v) for v in color[:3])
    except Exception:
        return (255, 255, 255)
    return tuple(max(0, min(255, value + amt)) for value in values)


def _hud_darken(color, amount: int = 0):
    """Overlay-local RGB darken helper used by compact HUD styling."""
    try:
        amt = int(amount)
        values = tuple(int(v) for v in color[:3])
    except Exception:
        return (0, 0, 0)
    return tuple(max(0, min(255, value - amt)) for value in values)


def _draw_vertical_gradient(screen, rect, top_color, bottom_color, alpha=255):
    """Draw a small vertical RGBA gradient directly into the overlay.

    Keep this helper local to ``hud_renderer``. The compact HUD runs in its
    own overlay process and cannot rely on similarly named helpers from the
    main GUI renderer. V6 bars use this for health, meter, damage scale, and
    stun tracks.
    """
    try:
        rect = pygame.Rect(rect)
    except Exception:
        return
    if rect.width <= 0 or rect.height <= 0:
        return

    try:
        a = max(0, min(255, int(alpha)))
        top = tuple(max(0, min(255, int(v))) for v in top_color[:3])
        bottom = tuple(max(0, min(255, int(v))) for v in bottom_color[:3])
    except Exception:
        return

    # Draw opaque gradients directly. For translucent gradients, use one local
    # SRCALPHA surface so alpha blends with the HUD instead of replacing it.
    target = screen
    ox = rect.x
    oy = rect.y
    local_rect = rect
    if a < 255:
        target = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
        ox = 0
        oy = 0
        local_rect = pygame.Rect(0, 0, rect.width, rect.height)

    for row in range(local_rect.height):
        t = row / max(1, local_rect.height - 1)
        color = (
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
        )
        draw_color = (*color, a) if a < 255 else color
        pygame.draw.line(
            target,
            draw_color,
            (local_rect.x, local_rect.y + row),
            (local_rect.right - 1, local_rect.y + row),
            1,
        )

    if a < 255:
        screen.blit(target, rect.topleft)

# ---------------------------------------------------------------------------
# Main row renderer
# ---------------------------------------------------------------------------

def _draw_slot_row(screen, font, font_sm, slot_label, snap,
                   anchor_x, anchor_y, row_h, scale,
                   is_active_char, slot_anim, overlay_alpha,
                   dt,
                   measure_only=False):

    slot_col = SLOT_COLORS.get(slot_label, (200, 200, 200))
    hp_cur   = snap.get("cur") or 0
    hp_max   = snap.get("max") or 1
    is_dead  = (hp_cur <= 0)

    # Damage tracking
    prev_hp = slot_anim["prev_hp"]
    if prev_hp is not None and hp_cur < prev_hp:
        dmg = prev_hp - hp_cur
        if dmg > 1:
            slot_anim["last_hit_damage"] = dmg
            slot_anim["damage_timer"] = 45
            events = slot_anim["damage_events"]
            events.insert(0, {"value": int(dmg), "life": 1.0, "x_offset": -20, "type": "self"})
            if len(events) > 5:
                events.pop()

            opponent = None
            if slot_label.startswith("P1"):
                opponent = _get_active_slot("P2")
            else:
                opponent = _get_active_slot("P1")

            if opponent:
                opp_anim = _get_slot_anim(opponent)
                opp_events = opp_anim["damage_events"]
                opp_events.insert(0, {
                    "value": int(dmg),
                    "life": 1.0,
                    "x_offset": 20,
                    "type": "opponent"
                })
                if len(opp_events) > 5:
                    opp_events.pop()
    slot_anim["prev_hp"] = hp_cur

    show_damage = (
        slot_anim["damage_timer"] > 0
        or any(ev["life"] > 0 for ev in slot_anim["damage_events"])
    )

    if slot_anim["damage_timer"] > 0:
        slot_anim["damage_timer"] -= 1

    # Colors
    if is_dead:
        name_col = text_col = COL_DEAD
    elif not is_active_char:
        name_col = text_col = COL_TEXT_DIM
    else:
        name_col = text_col = COL_TEXT

    # Layout
    pad      = int(6  * scale)
    acc_w    = int(3  * scale)
    badge_w  = int(26 * scale)
    name_gap = int(6  * scale)
    bar_w    = int(80 * scale)
    bar_h    = max(4, int(6 * scale))
    pip_w    = max(4, int(8 * scale))
    pip_h    = max(4, int(8 * scale))
    pip_gap  = max(1, int(2 * scale))
    meter_w  = 5 * pip_w + 4 * pip_gap
    sep      = int(10 * scale)

    char_name = snap.get("name") or "???"
    name_surf = font.render(char_name, True, name_col)
    name_w    = name_surf.get_width()

    hp_str   = f"{int(hp_cur)}/{int(hp_max)}"
    hp_num_s = font_sm.render(hp_str, True, text_col)

    meter_val = snap.get("meter")
    try:
        raw_meter = float(meter_val) if meter_val is not None else 0.0
        if   raw_meter < 10000: meter_f = 0
        elif raw_meter < 20000: meter_f = 1
        elif raw_meter < 30000: meter_f = 2
        elif raw_meter < 40000: meter_f = 3
        elif raw_meter < 50000: meter_f = 4
        else:                   meter_f = 5
        meter_str = f"{raw_meter:.0f}"
    except (TypeError, ValueError):
        meter_f = 0; meter_str = "---"

    # Meter gain tracking
    prev_meter = slot_anim["prev_meter"]
    cur_meter  = raw_meter if meter_val is not None else 0
    if prev_meter is not None and cur_meter > prev_meter:
        gain = int(cur_meter - prev_meter)
        if gain > 0:
            events = slot_anim["meter_events"]
            events.insert(0, {"value": gain, "life": 1.0, "x_offset": 20})
            if len(events) > 5:
                events.pop()
    slot_anim["prev_meter"] = cur_meter

    # Baroque gain/loss tracking
    prev_baroque_pct = slot_anim.get("prev_baroque_pct")
    cur_baroque_pct  = float(snap.get("baroque_red_pct_max") or 0.0)
    if prev_baroque_pct is not None and abs(cur_baroque_pct - prev_baroque_pct) >= 0.01:
        delta = cur_baroque_pct - prev_baroque_pct
        events = slot_anim["baroque_events"]
        events.insert(0, {"value": delta, "life": 1.0, "x_offset": 20})
        _push_event_history(slot_anim, "BBQ", f"{delta:+.0f}%", (255, 180, 92) if delta < 0 else (172, 112, 255), rainbow=True)
        if len(events) > 5:
            events.pop()
    slot_anim["prev_baroque_pct"] = cur_baroque_pct

    meter_num_s = font_sm.render(meter_str, True, text_col)

    move_id  = snap.get("mv_id_display")
    try:
        move_id_int = int(move_id) if move_id is not None else None
    except Exception:
        move_id_int = None
    mv_label = (snap.get("final_move_label") or snap.get("mv_label_display") or snap.get("mv_label") or "").strip()
    if not mv_label and move_id is not None:
        mv_label = f"0x{int(move_id):04X}"

    # A projectile profile can resolve after the generic move was already
    # added to history. Correct that existing event before drawing or deciding
    # whether this frame needs a new history chip.
    mv_label = _apply_profile_history_correction(
        slot_anim,
        snap,
        move_id_int,
        mv_label,
    )

    mission_suffix = ""
    if snap.get("mission_wrong_ready"):
        mission_suffix = " | WRONG READY"
    elif snap.get("mission_varing"):
        mission_suffix = " | VAR"
    elif snap.get("mission_var_ready"):
        mission_suffix = " | AIR OK"
    elif snap.get("mission_target"):
        mission_suffix = " | TARGET"

    mv_label_display = f"{mv_label}{mission_suffix}" if mv_label else mission_suffix.strip(" |")
    is_passive = mv_label.lower() in PASSIVE_LABELS
    is_baroque = (move_id is not None and int(move_id) in BAROQUE_CANCEL_IDS)
    move_col   = COL_TEXT_DIM if ((is_passive and not is_baroque) or not is_active_char or is_dead) else COL_TEXT
    move_surf  = font_sm.render(mv_label_display or "---", True, move_col)

    # Move history tracking. Passive/native state labels are intentional
    # evidence too; do not discard Idle/Crouch/Jump/etc.
    prev_move_label = slot_anim.get("prev_move_label", "")
    prev_move_id = slot_anim.get("prev_move_id")
    if (
        mv_label
        and mv_label != "---"
        and not is_dead
    ):
        move_events = slot_anim["move_events"]
        same_action = move_id_int is not None and prev_move_id == move_id_int
        newest = move_events[0] if move_events else None

        # A same-action generic to specific change is a correction, not a new
        # move. Update the newest chip in place so the sequence remains clean.
        if same_action and newest is not None:
            newest_action = newest.get("action_id")
            try:
                newest_action = int(newest_action) if newest_action is not None else None
            except Exception:
                newest_action = None
            if newest_action == move_id_int and mv_label != str(newest.get("text") or ""):
                base_text = str(newest.get("base_text") or newest.get("text") or "").strip()
                corrected = _resolve_profile_history_label(base_text, mv_label)
                if corrected != base_text or _profile_history_norm(mv_label) == _profile_history_norm(corrected):
                    newest["text"] = mv_label
                    newest["life"] = 1.0
        elif mv_label != prev_move_label:
            base_text = str(snap.get("mv_label_base") or snap.get("mv_label") or mv_label).strip()
            move_events.insert(0, {
                "text": mv_label,
                "base_text": base_text,
                "action_id": move_id_int,
                "created_at": time.time(),
                "life": 1.0,
                "frame": _frame,
            })
            if len(move_events) > 6:
                move_events.pop()
            slot_anim["move_scroll_px"] = max(int(28 * scale), 14)
    slot_anim["prev_move_label"] = mv_label
    slot_anim["prev_move_id"] = move_id_int

    # Baroque
    baroque_ready = snap.get("baroque_ready_local", False) and not is_dead
    baroque_pct   = snap.get("baroque_red_pct_max", 0.0) 
    if baroque_ready:
        slot_anim["baroque_last_pct"] = baroque_pct
        display_pct = baroque_pct
        show_baroque_badge = True
        slot_anim["baroque_freeze_timer"] = 0
    else:
        if slot_anim["baroque_prev_ready"]:
            slot_anim["baroque_display_pct"] = slot_anim["baroque_last_pct"]
            slot_anim["baroque_freeze_timer"] = 120
        if slot_anim["baroque_freeze_timer"] > 0:
            display_pct = slot_anim["baroque_display_pct"]
            show_baroque_badge = True
            slot_anim["baroque_freeze_timer"] -= 1
        else:
            display_pct = 0.0
            show_baroque_badge = False
    slot_anim["baroque_prev_ready"] = baroque_ready

    baroque_badge_w = 0
    if show_baroque_badge:
        bq_surf_tmp = font_sm.render(f"BBQ {display_pct:.1f}%", True, (255, 255, 255))
        baroque_badge_w = bq_surf_tmp.get_width() + int(10 * scale)

    # Determine live popup events for width calculation
    adv_events_live    = [e for e in slot_anim["adv_events"]    if e["life"] > 0]
    damage_events_live = [e for e in slot_anim["damage_events"] if e["life"] > 0]
    meter_events_live  = [e for e in slot_anim["meter_events"]  if e["life"] > 0]
    has_popups = bool(adv_events_live or damage_events_live or meter_events_live or show_baroque_badge)
    popup_max_w = (font.size("-9999")[0] + int(12 * scale)) if has_popups else 0

    total_w = (
        acc_w + pad
        + badge_w + name_gap
        + name_w + sep
        + font_sm.size("HP")[0] + int(4*scale) + bar_w + int(4*scale) + hp_num_s.get_width() + sep
        + font_sm.size("M")[0]  + int(4*scale) + meter_w + int(4*scale) + meter_num_s.get_width() + sep
        + move_surf.get_width() + sep
        + (baroque_badge_w + sep if show_baroque_badge else 0)
        + popup_max_w
        + pad
    )

    MAX_ROW_W = int(600 * scale)
    total_w = min(total_w, MAX_ROW_W)

    if measure_only:
        return total_w

    # Background pill (neo-futurist metallic)
    pill = pygame.Surface((total_w, row_h), pygame.SRCALPHA)
    base_alpha = int(BG_ALPHA * slot_anim["alpha"] * overlay_alpha)

    for y in range(row_h):
        shade = int(18 + 18 * (1 - y / row_h))
        pygame.draw.line(pill, (shade, shade + 2, shade + 4, base_alpha), (0, y), (total_w, y))

    for y in range(0, row_h, 2):
        pygame.draw.line(pill, (255, 255, 255, 8), (0, y), (total_w, y))

    pygame.draw.line(pill, (200, 220, 255, int(30 * overlay_alpha)), (0, 0), (total_w, 0))
    pygame.draw.line(pill, (0, 0, 0, int(80 * overlay_alpha)), (0, row_h - 1), (total_w, row_h - 1))

    if is_active_char and not is_dead:
        pygame.draw.rect(pill, (*slot_col, 120), (0, 0, total_w, row_h), 1)

        speed  = 120 + (slot_anim["meter_display"] * 10)
        t      = (time.time() * speed) % total_w
        scan_x = int(t) if slot_label.startswith("P1") else int(total_w - t)

        scan = pygame.Surface((6, row_h), pygame.SRCALPHA)
        scan.fill((*slot_col, 40))
        pill.blit(scan, (scan_x, 0))

    screen.blit(pill, (anchor_x, anchor_y))

    if is_active_char and not is_dead:
        pygame.draw.rect(screen, (*slot_col, 160), (anchor_x, anchor_y, total_w, row_h), 1, border_radius=2)

    pygame.draw.rect(screen, slot_col, (anchor_x, anchor_y, acc_w, row_h), border_radius=1)

    badge_x   = anchor_x + acc_w + pad
    badge_col = slot_col if is_active_char and not is_dead else COL_DEAD
    pygame.draw.rect(screen, (*badge_col, 200),
                     (badge_x, anchor_y + int(3*scale), badge_w, row_h - int(6*scale)), border_radius=2)
    badge_label = "C1" if slot_label.endswith("C1") else "C2"
    bs = font_sm.render(badge_label, True, (240, 240, 240))
    screen.blit(bs, (badge_x + (badge_w - bs.get_width()) // 2,
                     anchor_y + (row_h - bs.get_height()) // 2))

    cx     = badge_x + badge_w + name_gap
    mid_y  = anchor_y + row_h // 2
    popup_y = anchor_y + row_h + int(4 * scale)

    damage_y = popup_y
    meter_y  = popup_y
    adv_y    = popup_y
    sm_top   = anchor_y + int(2 * scale)
    sm_bot   = anchor_y + row_h - int(2 * scale) - font_sm.get_height()

    screen.blit(name_surf, (cx, mid_y - font.get_height() // 2))
    cx += name_w + sep
    _draw_divider(screen, cx - sep // 2, anchor_y, row_h, scale)

    lbl = font_sm.render("HP", True, COL_TEXT_DIM)
    screen.blit(lbl, (cx, sm_top))
    hp_bar_x = cx
    _draw_hp_bar(screen, hp_bar_x, mid_y - bar_h // 2, bar_w, bar_h, hp_cur, hp_max, is_dead)
    cx += bar_w + int(4 * scale)
    screen.blit(hp_num_s, (cx, sm_bot))
    cx += hp_num_s.get_width() + sep
    hp_anchor_x = cx - int(100 * scale)

    _draw_divider(screen, cx - sep // 2, anchor_y, row_h, scale)

    lbl = font_sm.render("M", True, COL_TEXT_DIM)
    screen.blit(lbl, (cx, sm_top))
    slot_anim["meter_display"] = _approach(slot_anim["meter_display"], meter_f, PIP_SPEED, 1/60.0)
    _draw_meter_pips_animated(screen, cx, mid_y - pip_h // 2, pip_w, pip_h, pip_gap, slot_anim, is_dead)
    cx += meter_w + int(4 * scale)
    screen.blit(meter_num_s, (cx, sm_bot))
    cx += meter_num_s.get_width() + sep
    meter_anchor_x = cx - int(60 * scale)

    _draw_divider(screen, cx - sep // 2, anchor_y, row_h, scale)

    move_anchor_x = cx
    screen.blit(move_surf, (move_anchor_x, mid_y - move_surf.get_height() // 2))
    cx += move_surf.get_width() + sep
    _draw_divider(screen, cx - sep // 2, anchor_y, row_h, scale)

    # Damage popup
    if show_damage:
        dx  = hp_anchor_x
        gap = int(6 * scale)
        for i, ev in enumerate(slot_anim["damage_events"]):
            ev["life"] -= 0.010
            speed = 240 if abs(ev["x_offset"]) > 5 else 80
            ev["x_offset"] = _approach(ev["x_offset"], 0, speed, 1/60.0)
            if ev["life"] <= 0:
                continue
            base_col = (80, 255, 120) if ev.get("type") == "opponent" else (255, 80, 80)
            if i > 0:
                base_col = tuple(int(c * 0.75) for c in base_col)
            alpha    = int(255 * ev["life"])
            dmg_font = font if i == 0 else font_sm
            dmg_surf = dmg_font.render(f"-{ev['value']}", True, base_col)
            dmg_surf.set_alpha(alpha)
            w = dmg_surf.get_width(); h = dmg_surf.get_height()
            pad_x = int(4 * scale); pad_y = int(2 * scale)
            bg = pygame.Surface((w + pad_x * 2, h + pad_y * 2), pygame.SRCALPHA)
            bg.fill((20, 0, 0, int(180 * ev["life"])))
            draw_x = dx + int(ev["x_offset"]) + (int(30 * scale) if ev.get("type") == "opponent" else 0)
            screen.blit(bg, (draw_x - pad_x, damage_y - pad_y))
            screen.blit(dmg_surf, (draw_x, damage_y))
            dx += w + gap

    # Meter gain popup
    meter_events = slot_anim["meter_events"]
    if meter_events:
        dx  = meter_anchor_x
        gap = int(6 * scale)
        for i, ev in enumerate(meter_events):
            ev["life"] -= 0.010
            ev["x_offset"] = _approach(ev["x_offset"], 0, 120, 1/60.0)
            if ev["life"] <= 0:
                continue
            base_col = (80, 160, 255) if i == 0 else (60, 120, 200)
            alpha    = int(255 * ev["life"])
            m_font   = font if i == 0 else font_sm
            surf     = m_font.render(f"+{ev['value']}", True, base_col)
            surf.set_alpha(alpha)
            w = surf.get_width(); h = surf.get_height()
            pad_x = int(4 * scale); pad_y = int(2 * scale)
            bg = pygame.Surface((w + pad_x * 2, h + pad_y * 2), pygame.SRCALPHA)
            bg.fill((0, 20, 50, int(180 * ev["life"])))
            draw_x = dx + int(ev["x_offset"])
            screen.blit(bg, (draw_x - pad_x, meter_y - pad_y))
            screen.blit(surf, (draw_x, meter_y))
            dx += w + gap
        slot_anim["meter_events"] = [e for e in meter_events if e["life"] > 0]

    # Baroque gain/loss popup
    baroque_events = slot_anim["baroque_events"]
    if baroque_events:
        dx  = meter_anchor_x + int(90 * scale)
        gap = int(6 * scale)
        for i, ev in enumerate(baroque_events):
            ev["life"] -= 0.010
            ev["x_offset"] = _approach(ev["x_offset"], 0, 120, 1/60.0)
            if ev["life"] <= 0:
                continue
            val = ev["value"]
            txt = f"{val:+.1f}%"
            alpha = int(255 * ev["life"])
            b_font = font if i == 0 else font_sm

            base = b_font.render(txt, True, (255, 255, 255))
            rainbow = pygame.Surface(base.get_size(), pygame.SRCALPHA)
            t = time.time() * 0.4
            for x in range(base.get_width()):
                phase = (x / max(1, base.get_width()) + t) % 1.0
                r = int(200 + 55 * math.sin(2 * math.pi * phase))
                g = int(160 + 55 * math.sin(2 * math.pi * (phase + 0.33)))
                pygame.draw.line(rainbow, (r, g, 255, 255), (x, 0), (x, base.get_height()))
            base.blit(rainbow, (0, 0), special_flags=pygame.BLEND_MULT)
            surf = base
            surf.set_alpha(alpha)

            w = surf.get_width(); h = surf.get_height()
            pad_x = int(4 * scale); pad_y = int(2 * scale)
            bg = pygame.Surface((w + pad_x * 2, h + pad_y * 2), pygame.SRCALPHA)
            bg.fill((45, 25, 0, int(180 * ev["life"])))
            draw_x = dx + int(ev["x_offset"])
            screen.blit(bg, (draw_x - pad_x, meter_y - pad_y))
            screen.blit(surf, (draw_x, meter_y))
            dx += w + gap
        slot_anim["baroque_events"] = [e for e in baroque_events if e["life"] > 0]

    # Baroque badge
    if show_baroque_badge:
        bq_text  = f"BBQ {display_pct:.1f}%"
        bq_base  = font_sm.render(bq_text, True, (255, 255, 255))
        rainbow  = pygame.Surface(bq_base.get_size(), pygame.SRCALPHA)
        t = time.time() * 0.4
        for x in range(bq_base.get_width()):
            phase = (x / bq_base.get_width() + t) % 1.0
            r = int(200 + 55 * math.sin(2 * math.pi * phase))
            g = int(160 + 55 * math.sin(2 * math.pi * (phase + 0.33)))
            pygame.draw.line(rainbow, (r, g, 255, 255), (x, 0), (x, bq_base.get_height()))
        bq_base.blit(rainbow, (0, 0), special_flags=pygame.BLEND_MULT)
        bq_surf = bq_base

        bq_w    = bq_surf.get_width() + int(8 * scale)
        bq_h    = row_h - int(6 * scale)
        bq_pill = pygame.Surface((bq_w, bq_h), pygame.SRCALPHA)
        bq_pill.fill((35, 30, 20, 220))
        screen.blit(bq_pill, (cx, anchor_y + int(3 * scale)))
        screen.blit(bq_surf, (cx + int(4 * scale), anchor_y + (row_h - bq_surf.get_height()) // 2))
        cx += bq_w + sep

    # Frame advantage popup
    adv_anchor_x = anchor_x + total_w - popup_max_w - int(6 * scale)
    adv_events   = slot_anim["adv_events"]
    if adv_events:
        dx  = adv_anchor_x
        gap = int(6 * scale)
        for i, ev in enumerate(adv_events):
            ev["life"] -= 0.010
            ev["x_offset"] = _approach(ev["x_offset"], 0, 120, 1/60.0)
            if ev["life"] <= 0:
                continue
            val      = ev["value"]
            base_col = (80, 255, 120) if val > 0 else ((255, 80, 80) if val < 0 else (200, 200, 200))
            txt      = f"+{val}" if val > 0 else str(val)
            alpha    = int(255 * ev["life"])
            adv_font = font if i == 0 else font_sm
            adv_surf = adv_font.render(txt, True, base_col)
            adv_surf.set_alpha(alpha)
            w = adv_surf.get_width(); h = adv_surf.get_height()
            pad_x = int(4 * scale); pad_y = int(2 * scale)
            bg_col = (0, 30, 0) if val > 0 else ((40, 0, 0) if val < 0 else (20, 20, 20))
            bg = pygame.Surface((w + pad_x * 2, h + pad_y * 2), pygame.SRCALPHA)
            bg.fill((*bg_col, int(180 * ev["life"])))
            draw_x = dx + int(ev["x_offset"])
            screen.blit(bg, (draw_x - pad_x, adv_y - pad_y))
            screen.blit(adv_surf, (draw_x, adv_y))
            dx += w + gap
        slot_anim["adv_events"] = [e for e in adv_events if e["life"] > 0]

    # One-line move history under this slot's move field
    move_events = slot_anim["move_events"]
    if move_events:
        move_list_y = anchor_y + row_h + int(18 * scale)

        # newest stays pinned while it is still the current live move
        newest_is_live = (
            not is_passive
            and mv_label
            and mv_label != "---"
            and len(move_events) > 0
            and move_events[0]["text"] == mv_label
        )

        # scroll animation for new inserts
        slot_anim["move_scroll_px"] = _approach(
            float(slot_anim.get("move_scroll_px", 0.0)),
            0.0,
            180.0,
            dt
        )
        scroll_px = int(slot_anim["move_scroll_px"])

        # display oldest -> newest
        display_items = list(reversed(move_events))

        parts = []
        newest_rect_index = None

        for idx, ev in enumerate(display_items):
            recency_from_newest = len(display_items) - 1 - idx
            ev_is_baroque = ev["text"].lower() == "baroque cancel"

            if ev_is_baroque:
                phase = (time.time() * 0.8) % 1.0
                color = (
                    int(200 + 55 * math.sin(2 * math.pi * phase)),
                    int(160 + 55 * math.sin(2 * math.pi * (phase + 0.33))),
                    255,
                )
            elif recency_from_newest == 0:
                color = (80, 255, 120)    # newest = green
            elif recency_from_newest == 1:
                color = (80, 160, 255)    # mid = blue
            elif recency_from_newest == 2:
                color = (255, 220, 90)    # old = yellow
            else:
                color = COL_TEXT          # 4th/5th neutral

            parts.append({
                "text": ev["text"],
                "color": color,
                "life": ev["life"],
                "is_newest": (recency_from_newest == 0),
            })
            if recency_from_newest == 0:
                newest_rect_index = len(parts) - 1

            if idx < len(display_items) - 1:
                parts.append({
                    "text": " > ",
                    "color": COL_TEXT,
                    "life": ev["life"],
                    "is_newest": False,
                })

        line_life = min(ev["life"] for ev in move_events)
        rendered_parts = []
        total_line_w = 0
        max_line_h = 0

        for part in parts:
            surf = font_sm.render(part["text"], True, part["color"])
            rendered_parts.append((surf, part["is_newest"]))
            total_line_w += surf.get_width()
            max_line_h = max(max_line_h, surf.get_height())

        move_list_x = badge_x - int(2 * scale) - scroll_px

        pad_x = int(5 * scale)
        pad_y = int(2 * scale)
        bg = pygame.Surface((total_line_w + pad_x * 2, max_line_h + pad_y * 2), pygame.SRCALPHA)
        bg.fill((12, 12, 12, 185))
        screen.blit(bg, (move_list_x - pad_x, move_list_y - pad_y))
        dx = move_list_x
        newest_rect = None
        for surf, is_newest_part in rendered_parts:
            screen.blit(surf, (dx, move_list_y))
            if is_newest_part:
                newest_rect = (
                    dx - int(3 * scale),
                    move_list_y - int(2 * scale),
                    surf.get_width() + int(6 * scale),
                    surf.get_height() + int(4 * scale),
                )
            dx += surf.get_width()

        # pulsing gradient frame around newest move only
        if newest_rect is not None:
            newest_text = move_events[0]["text"].lower() if move_events else ""
            if newest_text == "baroque cancel":
                phase = (time.time() * 0.8) % 1.0
                frame_col = (
                    int(200 + 55 * math.sin(2 * math.pi * phase)),
                    int(160 + 55 * math.sin(2 * math.pi * (phase + 0.33))),
                    255,
                )
            else:
                frame_col = (80, 255, 120)
            pulse = 0.85 + 0.35 * (0.5 + 0.5 * math.sin(time.time() * 6.0))
            _draw_gradient_frame(screen, newest_rect, frame_col, pulse=pulse)

        

        # fade only older entries; keep current active move pinned
        for i, ev in enumerate(move_events):
            if i == 0 and newest_is_live:
                ev["life"] = 1.0
            else:
                ev["life"] -= 0.006

        slot_anim["move_events"] = [e for e in move_events if e["life"] > 0]

    return total_w


def _compute_active_slots(slots: dict) -> set[str]:
    active = set()
    for team, (c1, c2) in (("P1", ("P1-C1", "P1-C2")), ("P2", ("P2-C1", "P2-C2"))):
        s1, s2 = slots.get(c1), slots.get(c2)
        if s1 and not s2:
            active.add(c1)
        elif s2 and not s1:
            active.add(c2)
        elif s1 and s2:
            c1_off = int(s1.get("mv_id_display") or 0) in ASSIST_OFF_IDS
            c2_off = int(s2.get("mv_id_display") or 0) in ASSIST_OFF_IDS
            if c2_off and not c1_off:
                active.add(c1)
            elif c1_off and not c2_off:
                active.add(c2)
            else:
                active.add(c1); active.add(c2)
    return active


def _draw_overlay_detail(screen, font, font_sm, slots, scale, dt) -> None:
    _anim_state["overlay_alpha"] = _approach(_anim_state["overlay_alpha"], 1.0, FADE_SPEED, dt)
    overlay_alpha = _anim_state["overlay_alpha"]
    row_h   = max(14, int(BASE_ROW_H * scale))
    row_gap = int(25 * scale)
    active  = _compute_active_slots(slots)
    active  = _compute_active_slots(slots)

    for slot_label, (side, base_x, base_y) in SLOT_LAYOUT.items():
        snap      = slots.get(slot_label)
        slot_anim = _get_slot_anim(slot_label)

        target_alpha = 1.0 if slot_anim["present"] else 0.0
        slot_anim["alpha"] = _approach(slot_anim["alpha"], target_alpha, FADE_SPEED, dt)

        if slot_anim["alpha"] <= 0.01 and not slot_anim["present"]:
            _display_slots.pop(slot_label, None)
            continue
        if slot_anim["alpha"] <= 0.01:
            continue
        if not snap:
            continue

        scaled_y = int(base_y * scale) + (row_gap if slot_label.endswith("C2") else 0)

        # First pass: measure width
        row_w = _draw_slot_row(screen, font, font_sm, slot_label, snap,
                               0, scaled_y, row_h, scale, slot_label in active,
                               slot_anim, overlay_alpha, dt,
                               measure_only=True)

        if side == "right":
            anchor_x = screen.get_width() - int(base_x * scale) - row_w
        else:
            anchor_x = int(base_x * scale)

        # Second pass: draw
        _draw_slot_row(screen, font, font_sm, slot_label, snap,
                       anchor_x, scaled_y, row_h, scale, slot_label in active,
                       slot_anim, overlay_alpha, dt)


# ---------------------------------------------------------------------------
# Compact team overlay
# ---------------------------------------------------------------------------

def _compact_meter_level(meter_value) -> int:
    try:
        meter = float(meter_value or 0.0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, int(meter // 10000.0)))


def _compact_meter_text(meter_value) -> str:
    try:
        meter_i = max(0, int(meter_value or 0))
    except (TypeError, ValueError):
        meter_i = 0
    return f"{meter_i}/50000"


def _lerp_color(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, float(t)))
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def _compact_meter_color(meter_value) -> tuple[int, int, int]:
    try:
        meter = max(0.0, min(50000.0, float(meter_value or 0.0)))
    except (TypeError, ValueError):
        meter = 0.0
    stops = [
        (0.0, (82, 156, 255)),
        (10000.0, (82, 156, 255)),
        (20000.0, (72, 194, 255)),
        (30000.0, (72, 224, 164)),
        (40000.0, (242, 198, 88)),
        (50000.0, (255, 116, 116)),
    ]
    for (m1, c1), (m2, c2) in zip(stops, stops[1:]):
        if meter <= m2:
            span = max(1.0, m2 - m1)
            return _lerp_color(c1, c2, (meter - m1) / span)
    return stops[-1][1]


def _compact_health_gradient_color(position: float) -> tuple[int, int, int]:
    """Map HP position across the full bar to the requested danger/healthy ramp.

    0-10%: red
    10-25%: red -> yellow
    25-90%: yellow -> green
    90-100%: green -> white ender
    """
    pos = max(0.0, min(1.0, float(position)))
    red = (255, 92, 92)
    yellow = (255, 214, 88)
    green = (88, 232, 120)
    white_end = (142, 244, 164)
    stops = [
        (0.00, red),
        (0.10, red),
        (0.25, yellow),
        (0.90, green),
        (1.00, white_end),
    ]
    for (p1, c1), (p2, c2) in zip(stops, stops[1:]):
        if pos <= p2:
            span = max(1e-6, p2 - p1)
            return _lerp_color(c1, c2, (pos - p1) / span)
    return white_end


def _compact_meter_gradient_color(position: float) -> tuple[int, int, int]:
    """Continuous stock-color ramp with soft crossfades at 10K boundaries."""
    p = max(0.0, min(1.0, float(position or 0.0)))
    stops = [
        (0.00, (82, 156, 255)),
        (0.18, (96, 170, 255)),
        (0.22, (92, 214, 132)),
        (0.38, (92, 214, 132)),
        (0.42, (255, 226, 92)),
        (0.58, (255, 226, 92)),
        (0.62, (255, 162, 78)),
        (0.78, (255, 162, 78)),
        (0.82, (255, 92, 92)),
        (1.00, (255, 92, 92)),
    ]
    for (p1, c1), (p2, c2) in zip(stops, stops[1:]):
        if p <= p2:
            return _lerp_color(c1, c2, (p - p1) / max(0.0001, p2 - p1))
    return stops[-1][1]


def _compact_meter_gradient_cell(width: int, height: int, index: int, is_dead: bool) -> pygame.Surface:
    key = (max(1, int(width)), max(1, int(height)), max(0, min(4, int(index))), bool(is_dead))
    cached = _COMPACT_METER_GRADIENT_CACHE.get(key)
    if cached is not None:
        return cached
    w, h, idx, dead = key
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    for px in range(w):
        local = px / max(1, w - 1)
        global_pos = (idx + local) / 5.0
        base = COL_DEAD if dead else _compact_meter_gradient_color(global_pos)
        top = _hud_brighten(base, 30)
        mid = base
        bottom = _hud_darken(base, 24)
        if h <= 2:
            pygame.draw.line(surf, mid, (px, 0), (px, h - 1))
        else:
            pygame.draw.line(surf, top, (px, 0), (px, 0))
            pygame.draw.line(surf, mid, (px, 1), (px, h - 2))
            pygame.draw.line(surf, bottom, (px, h - 1), (px, h - 1))
    if len(_COMPACT_METER_GRADIENT_CACHE) >= 40:
        _COMPACT_METER_GRADIENT_CACHE.clear()
    _COMPACT_METER_GRADIENT_CACHE[key] = surf
    return surf


def _compact_hp_text(cur, maximum) -> str:
    try:
        cur_i = max(0, int(cur or 0))
        max_i = max(1, int(maximum or 1))
    except (TypeError, ValueError):
        return "--"
    return f"{cur_i}/{max_i}"


def _compact_trim(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max(1, max_chars - 1)].rstrip() + "…"


def _compact_move_label(snap: dict) -> str:
    move_id = snap.get("mv_id_display")
    label = (snap.get("final_move_label") or snap.get("mv_label_display") or snap.get("mv_label") or "").strip()
    if not label and move_id is not None:
        label = f"0x{int(move_id):04X}"
    if not label:
        return ""

    lowered = label.lower()
    is_baroque = move_id is not None and int(move_id) in BAROQUE_CANCEL_IDS
    if lowered in PASSIVE_LABELS and not is_baroque:
        return ""
    return label


def _compact_event_move_label(snap: dict) -> str:
    """Return the real fighter-action label used to create history events.

    mv_label_display may be a delayed projectile-profiler override. History
    insertion must still follow the actual current action, then the profiler
    rewrites the already-created projectile chip in place.
    """
    move_id = snap.get("mv_id_display")
    label = str(snap.get("mv_label") or snap.get("mv_label_display") or "").strip()
    if not label and move_id is not None:
        try:
            label = f"0x{int(move_id):04X}"
        except Exception:
            label = str(move_id)
    if not label:
        return ""
    lowered = label.lower()
    try:
        move_id_int = int(move_id) if move_id is not None else -1
        is_baroque = move_id_int in BAROQUE_CANCEL_IDS
    except Exception:
        move_id_int = -1
        is_baroque = False
    # Resolved low-ID states are useful evidence: Idle, Crouch, Jump,
    # Jump Cancel, Landing, etc. Unknown low IDs never reach this point because
    # realtime_action_name() returns an empty label for them.
    return label


def _compact_partner_state(snap: dict) -> str:
    current = int(snap.get("mv_id_display") or 0)
    label = (snap.get("mv_label") or "").lower()
    hp = int(snap.get("cur") or 0)
    if hp <= 0:
        return "KO"
    if current in ASSIST_ATTACK_IDS or "assist attack" in label:
        return "ACTIVE"
    if "assist leave" in label or "tag out" in label:
        return "RETURN"
    if current in ASSIST_STANDBY_IDS or "standby" in label:
        return "READY"
    return "READY"


def _track_compact_input_packet(
    slot_anim: dict,
    input_held: int,
    input_pressed: int,
    input_released: int,
    frame_number: int,
) -> None:
    """Record one raw input sample without dropping repeated direction taps."""
    input_held = int(input_held) & 0xFFFF
    input_pressed = int(input_pressed) & 0xFFFF
    input_released = int(input_released) & 0xFFFF
    frame_number = int(frame_number)

    _update_button_hold_log(slot_anim, input_held, frame_number)

    current_input_state = input_held & INPUT_TRACK_MASK
    direction_key = current_input_state & INPUT_DIRECTION_MASK
    held_buttons = current_input_state & INPUT_BUTTON_MASK
    previous_input_state = slot_anim.get("prev_input_state")
    previous_visible_state = slot_anim.get("prev_visible_input_state")
    input_history = slot_anim["input_history"]

    if previous_input_state is None:
        previous_input_state_int = current_input_state
    else:
        previous_input_state_int = int(previous_input_state) & INPUT_TRACK_MASK

    previous_qualified_mask = int(slot_anim.get("qualified_hold_mask", 0)) & INPUT_BUTTON_MASK
    qualified_hold_mask = _qualified_button_hold_mask(slot_anim, frame_number)
    newly_qualified_mask = qualified_hold_mask & ~previous_qualified_mask
    slot_anim["qualified_hold_mask"] = qualified_hold_mask

    # The game's pressed/released words can remain asserted for a complete game
    # frame. The overlay may sample that frame more than once, so only accept the
    # newly observed edge bits. A real second tap is still preserved because the
    # edge word clears, or the held state returns to neutral, between taps.
    previous_raw_pressed = int(slot_anim.get("prev_raw_pressed", 0) or 0) & INPUT_BUTTON_MASK
    previous_raw_released = int(slot_anim.get("prev_raw_released", 0) or 0) & INPUT_BUTTON_MASK
    raw_pressed = input_pressed & INPUT_BUTTON_MASK
    raw_released = input_released & INPUT_BUTTON_MASK
    fresh_raw_pressed = raw_pressed & ~previous_raw_pressed
    fresh_raw_released = raw_released & ~previous_raw_released

    derived_pressed = current_input_state & ~previous_input_state_int & INPUT_BUTTON_MASK
    derived_released = previous_input_state_int & ~current_input_state & INPUT_BUTTON_MASK
    pressed_buttons = (fresh_raw_pressed | derived_pressed) & INPUT_BUTTON_MASK
    released_buttons = (fresh_raw_released | derived_released) & INPUT_BUTTON_MASK

    visible_held_buttons = held_buttons & (~qualified_hold_mask & INPUT_BUTTON_MASK)
    visible_input_state = direction_key | visible_held_buttons

    if previous_visible_state is None:
        previous_visible_state_int = visible_input_state
    else:
        previous_visible_state_int = int(previous_visible_state) & INPUT_TRACK_MASK

    visible_pressed_buttons = pressed_buttons & (~qualified_hold_mask & INPUT_BUTTON_MASK)
    hidden_hold_edges = previous_qualified_mask | qualified_hold_mask
    visible_released_buttons = released_buttons & (~hidden_hold_edges & INPUT_BUTTON_MASK)

    input_events: list[tuple[str, str]] = []
    previous_direction = previous_visible_state_int & INPUT_DIRECTION_MASK
    direction_changed = direction_key != previous_direction

    if visible_pressed_buttons:
        press_state = direction_key | visible_held_buttons | visible_pressed_buttons
        input_events.append((
            "press",
            _format_overlay_input_token(
                press_state & INPUT_DIRECTION_MASK,
                press_state & INPUT_BUTTON_MASK,
            ),
        ))

    if direction_changed:
        if direction_key == 0 and not visible_held_buttons:
            # Neutral is the separator between taps, not an input-history chip.
            _freeze_active_input_chip(slot_anim, frame_number)
        else:
            direction_text = _format_overlay_input_token(
                visible_input_state & INPUT_DIRECTION_MASK,
                visible_input_state & INPUT_BUTTON_MASK,
            )
            if not input_events or input_events[-1][1] != direction_text:
                input_events.append(("direction", direction_text))

    if newly_qualified_mask or visible_released_buttons:
        _freeze_active_input_chip(slot_anim, frame_number)

    if previous_input_state is None and visible_input_state and not input_events:
        input_events.append((
            "initial",
            _format_overlay_input_token(
                visible_input_state & INPUT_DIRECTION_MASK,
                visible_input_state & INPUT_BUTTON_MASK,
            ),
        ))

    for event_kind, input_text in input_events:
        if not input_text:
            continue
        # Keep every real state transition. A direction pressed on one frame and
        # a button pressed later are two distinct inputs. Same-frame direction +
        # button packets already arrive as one combined token above.
        input_history.append(input_text)
        _append_input_chip_token(slot_anim, input_text, frame_number)
        slot_anim["last_input_frame"] = frame_number

    slot_anim["prev_input_state"] = current_input_state
    slot_anim["prev_visible_input_state"] = visible_input_state
    slot_anim["prev_input_key"] = direction_key
    slot_anim["prev_raw_pressed"] = raw_pressed
    slot_anim["prev_raw_released"] = raw_released
    del input_history[:-12]


def _compact_track_slot(slot_label: str, snap: dict) -> None:
    """Update compact-overlay event state from the latest slot snapshot."""
    slot_anim = _get_slot_anim(slot_label)
    try:
        hp_cur = int(snap.get("cur") or 0)
    except (TypeError, ValueError):
        hp_cur = 0

    prev_hp = slot_anim.get("prev_hp")
    if prev_hp is not None and hp_cur != prev_hp:
        hp_delta = hp_cur - prev_hp
        slot_anim["hp_value_flash"] = 1.0
        if abs(hp_delta) > 1:
            events = slot_anim["damage_events"]
            if hp_delta < 0:
                damage = -hp_delta
                events.insert(0, {"value": damage, "life": 1.0, "age": 0.0, "x_offset": 0, "type": "self"})
                _push_event_history(slot_anim, "DMG IN", _compact_short_number(damage), (255, 110, 110))
                _trigger_team_panel_fx(_team_from_slot(slot_label), (255, 110, 110), min(1.35, 0.55 + damage / 2400.0), 10)
                _trigger_impact_recoil(_team_from_slot(slot_label), damage)
                slot_anim["hp_trail_delay"] = 0.16
                _combo_register_damage(slot_label, damage)
                opponent = _get_active_slot("P2" if slot_label.startswith("P1") else "P1")
                if opponent:
                    opp_anim = _get_slot_anim(opponent)
                    other = opp_anim["damage_events"]
                    other.insert(0, {"value": damage, "life": 1.0, "age": 0.0, "x_offset": 0, "type": "opponent"})
                    _push_event_history(opp_anim, "DMG OUT", _compact_short_number(damage), (255, 110, 110))
                    _trigger_team_panel_fx(_team_from_slot(opponent), (255, 132, 92), min(1.05, 0.45 + damage / 3200.0), 7)
                    del other[3:]
            else:
                events.insert(0, {"value": hp_delta, "life": 1.0, "age": 0.0, "x_offset": 0, "type": "heal"})
                _push_event_history(slot_anim, "HP +", _compact_short_number(hp_delta), (92, 232, 146))
                _trigger_team_panel_fx(_team_from_slot(slot_label), (92, 232, 146), 0.70, 5)
            del events[3:]
    slot_anim["prev_hp"] = hp_cur

    try:
        meter_cur = int(snap.get("meter") or 0)
    except (TypeError, ValueError):
        meter_cur = 0
    prev_meter = slot_anim.get("prev_meter")
    if prev_meter is not None and meter_cur != prev_meter:
        meter_delta = meter_cur - prev_meter
        if abs(meter_delta) > 10:
            meter_events = slot_anim["meter_events"]
            meter_events.insert(0, {
                "value": abs(meter_delta),
                "direction": "gain" if meter_delta > 0 else "loss",
                "life": 1.0,
                "age": 0.0,
                "x_offset": 0,
            })
            _push_event_history(slot_anim, "MTR", f"{'+' if meter_delta > 0 else '-'}{_compact_short_number(abs(meter_delta))}", (96, 182, 255) if meter_delta > 0 else (255, 164, 92))
            _trigger_team_panel_fx(_team_from_slot(slot_label), (96, 182, 255) if meter_delta > 0 else (255, 164, 92), 0.55, 4)
            if meter_delta < 0:
                slot_anim["meter_spend_sweep"] = 1.0
                slot_anim["meter_spend_amount"] = abs(int(meter_delta))
            del meter_events[3:]
    slot_anim["prev_meter"] = meter_cur

    try:
        baroque_cur = float(snap.get("baroque_red_pct_max") or 0.0)
    except (TypeError, ValueError):
        baroque_cur = 0.0
    prev_baroque = slot_anim.get("prev_baroque_pct")
    if prev_baroque is not None:
        baroque_delta = baroque_cur - float(prev_baroque)
        if abs(baroque_delta) >= 0.05:
            slot_anim["baroque_change_flash"] = 1.0
            baroque_events = slot_anim["baroque_events"]
            baroque_events.insert(0, {
                "value": baroque_delta,
                "direction": "gain" if baroque_delta > 0 else "loss",
                "life": 1.0,
                "age": 0.0,
                "x_offset": 0,
            })
            baroque_text = f"{'+' if baroque_delta > 0 else '-'}{abs(baroque_delta):.1f}%"
            baroque_color = (255, 211, 92) if baroque_delta > 0 else (255, 132, 104)
            _push_event_history(slot_anim, "BBQ", baroque_text, baroque_color, rainbow=True)
            _trigger_team_panel_fx(_team_from_slot(slot_label), (172, 112, 255) if baroque_delta > 0 else (255, 180, 92), 0.85, 7)
            del baroque_events[3:]
    slot_anim["prev_baroque_pct"] = baroque_cur

    try:
        input_held = int(snap.get("input_held") or 0) & 0xFFFF
        input_pressed = int(snap.get("input_pressed") or 0) & 0xFFFF
        input_released = int(snap.get("input_released") or 0) & 0xFFFF
    except (TypeError, ValueError):
        input_held = 0
        input_pressed = 0
        input_released = 0

    raw_samples = snap.get("input_samples")
    samples = [item for item in raw_samples if isinstance(item, dict)] if isinstance(raw_samples, list) else []
    last_sample_seq = int(slot_anim.get("last_input_sample_seq", 0) or 0)
    max_available_seq = max((int(item.get("seq", 0) or 0) for item in samples), default=0)
    if max_available_seq and max_available_seq < last_sample_seq:
        # Producer restarted, so its sequence counter began again.
        last_sample_seq = 0

    pending_samples = [
        item for item in samples
        if int(item.get("seq", 0) or 0) > last_sample_seq
    ]
    pending_samples.sort(key=lambda item: int(item.get("seq", 0) or 0))

    if pending_samples:
        # Timestamp edges against the renderer's actual 60 Hz clock. Using the
        # newest packet in a batch as "frame zero" made a captured input look
        # younger than it really was whenever transport took a frame or two.
        # The sidecar now gets the edge on screen quickly, and this keeps its
        # frame counter honest from the instant it appears.
        now_ns = time.monotonic_ns()
        game_frame_ns = 1_000_000_000.0 / 60.0
        last_timestamp_frame = int(slot_anim.get("last_input_timestamp_frame", -999999) or -999999)
        for sample in pending_samples:
            try:
                sample_ns = int(sample.get("sample_ns", 0) or 0)
            except Exception:
                sample_ns = 0
            if sample_ns > 0 and now_ns >= sample_ns:
                age_frames = int(round((now_ns - sample_ns) / game_frame_ns))
                sample_frame = int(_frame) - max(0, age_frames)
            else:
                sample_frame = int(_frame)
            # Ordered samples must never travel backwards on the display clock.
            sample_frame = max(last_timestamp_frame, sample_frame)
            last_timestamp_frame = sample_frame
            _track_compact_input_packet(
                slot_anim,
                int(sample.get("held", 0) or 0) & 0xFFFF,
                int(sample.get("pressed", 0) or 0) & 0xFFFF,
                int(sample.get("released", 0) or 0) & 0xFFFF,
                sample_frame,
            )
        slot_anim["last_input_sample_seq"] = int(pending_samples[-1].get("seq", 0) or 0)
        slot_anim["last_input_timestamp_frame"] = last_timestamp_frame
    else:
        # Continue active frame counters and hold qualification while unchanged.
        _track_compact_input_packet(
            slot_anim, input_held, input_pressed, input_released, int(_frame)
        )

    # Consume the authoritative committed native move log.  The producer
    # records every action transition before a later snapshot can overwrite it,
    # which makes fast cancel chains real history rather than best-effort HUD
    # observations.  Fall back to the older transient action ring only when a
    # move-log producer is not present (e.g. older sidecar files during upgrade).
    committed_raw = snap.get("realtime_move_log")
    committed = [item for item in committed_raw if isinstance(item, dict)] if isinstance(committed_raw, list) else []
    # Presence of the ring, even when empty, means the authoritative producer is
    # active.  Falling back merely because the ring is empty reintroduced old
    # IDLE/FORWARD debugger states before the first attack of a round.
    use_committed_log = isinstance(committed_raw, list)
    if use_committed_log:
        # Authoritative mode is intentionally dumb: the visible per-slot MOVES
        # row is a mirror of the producer's last five committed attack-instance
        # events.  Do not consume through a cursor and do not reconstruct from
        # the current snapshot.  If the renderer misses several frames, the next
        # draw still receives the exact last five moves in the ring.
        events = slot_anim["move_events"]
        try:
            committed_base = int(snap.get("base", 0) or 0)
        except Exception:
            committed_base = 0
        previous_base = int(slot_anim.get("move_log_base", 0) or 0)
        if (
            not bool(slot_anim.get("committed_move_log_active", False))
            or (committed_base and previous_base and committed_base != previous_base)
        ):
            events.clear()
        slot_anim["committed_move_log_active"] = True
        if committed_base:
            slot_anim["move_log_base"] = committed_base

        ordered = sorted(
            committed,
            key=lambda item: (
                int(item.get("log_seq", 0) or 0),
                int(item.get("sample_ns", 0) or 0),
            ),
        )
        now_ns = time.monotonic_ns()
        game_frame_ns = 1_000_000_000.0 / 60.0
        displayable = []
        for action_sample in ordered:
            try:
                action_id = int(action_sample.get("action_id", 0) or 0) & 0x7FFF
                char_id = int(action_sample.get("char_id", snap.get("id", 0)) or 0)
                sample_ns = int(action_sample.get("sample_ns", 0) or 0)
                log_seq = int(action_sample.get("log_seq", 0) or 0)
            except Exception:
                continue
            if action_id <= 0:
                continue
            action_label = realtime_action_name(action_id, char_id) or (f"0x{action_id:04X}" if action_id >= 0x100 else "")
            base_action_label = _compact_event_move_label({"mv_id_display": action_id, "mv_label": action_label})
            if not base_action_label:
                continue
            if sample_ns > 0 and now_ns >= sample_ns:
                age_frames = int(round((now_ns - sample_ns) / game_frame_ns))
                action_display_frame = int(_frame) - max(0, age_frames)
            else:
                action_display_frame = int(_frame)
            displayable.append({
                "text": base_action_label,
                "base_text": base_action_label,
                "action_id": action_id,
                "created_at": time.time(),
                "sample_ns": sample_ns,
                "log_seq": log_seq,
                "life": 1.0,
                "frame": action_display_frame,
            })
        rebuilt = list(reversed(displayable[-5:]))

        new_signature = tuple(int(item.get("log_seq", 0) or 0) for item in rebuilt)
        if new_signature != tuple(slot_anim.get("move_log_signature", ())):
            slot_anim["move_change_flash"] = 1.0
            slot_anim["move_log_signature"] = new_signature
        events.clear()
        events.extend(rebuilt[:5])

    if not use_committed_log:
        action_samples_raw = snap.get("realtime_action_samples")
        action_samples = [item for item in action_samples_raw if isinstance(item, dict)] if isinstance(action_samples_raw, list) else []
        last_action_seq = int(slot_anim.get("last_action_sample_seq", 0) or 0)
        max_action_seq = max((int(item.get("seq", 0) or 0) for item in action_samples), default=0)
        if max_action_seq and max_action_seq < last_action_seq:
            last_action_seq = 0
        pending_actions = [item for item in action_samples if int(item.get("seq", 0) or 0) > last_action_seq]
        pending_actions.sort(key=lambda item: (int(item.get("seq", 0) or 0), int(item.get("sample_ns", 0) or 0)))
        if pending_actions:
            now_ns = time.monotonic_ns()
            game_frame_ns = 1_000_000_000.0 / 60.0
            events = slot_anim["move_events"]
            for action_sample in pending_actions:
                try:
                    action_id = int(action_sample.get("action_id", 0) or 0) & 0x7FFF
                    char_id = int(action_sample.get("char_id", snap.get("id", 0)) or 0)
                    sample_ns = int(action_sample.get("sample_ns", 0) or 0)
                except Exception:
                    continue
                if action_id <= 0:
                    slot_anim["prev_compact_move_key"] = ""
                    continue
                action_label = realtime_action_name(action_id, char_id) or (f"0x{action_id:04X}" if action_id >= 0x100 else "")
                action_snap = {"mv_id_display": action_id, "mv_label": action_label}
                base_action_label = _compact_event_move_label(action_snap)
                if not base_action_label:
                    slot_anim["prev_compact_move_key"] = ""
                    continue
                action_key = f"id:{action_id}"
                if action_key == str(slot_anim.get("prev_compact_move_key") or ""):
                    continue
                if sample_ns > 0 and now_ns >= sample_ns:
                    age_frames = int(round((now_ns - sample_ns) / game_frame_ns))
                    action_display_frame = int(_frame) - max(0, age_frames)
                else:
                    action_display_frame = int(_frame)
                slot_anim["move_change_flash"] = 1.0
                events.insert(0, {
                    "text": base_action_label,
                    "base_text": base_action_label,
                    "action_id": action_id,
                    "native_state_flags": int(action_sample.get("state_flags_6c", 0) or 0) & 0xFFFFFFFF,
                    "created_at": time.time(),
                    "sample_ns": sample_ns,
                    "life": 1.0,
                    "frame": action_display_frame,
                })
                del events[5:]
                slot_anim["prev_compact_move_key"] = action_key
            slot_anim["last_action_sample_seq"] = int(pending_actions[-1].get("seq", 0) or 0)

    move_id = snap.get("mv_id_display")
    base_move_label = _compact_event_move_label(snap)
    display_move_label = str(
        snap.get("final_move_label")
        or snap.get("mv_label_display")
        or base_move_label
    ).strip()
    try:
        move_id_key = int(move_id) if move_id is not None else -1
    except (TypeError, ValueError):
        move_id_key = -1

    # History identity follows the real action and generic family. A profiler
    # suffix changes the existing chip text, never the event identity.
    move_key = (
        f"id:{move_id_key}"
        if move_id_key >= 0 and base_move_label
        else (f"label:{base_move_label.lower()}" if base_move_label else "")
    )
    previous_key = str(slot_anim.get("prev_compact_move_key") or "")
    events = slot_anim["move_events"]
    if not use_committed_log:
        if move_key and move_key != previous_key:
            slot_anim["move_change_flash"] = 1.0
            events.insert(0, {
                "text": display_move_label or base_move_label,
                "base_text": base_move_label,
                "action_id": move_id_key if move_id_key >= 0 else None,
                "created_at": time.time(),
                "life": 1.0,
                "frame": _frame,
            })
            del events[5:]
        elif move_key and events:
            newest = events[0]
            try:
                newest_action = int(newest.get("action_id") or -1)
            except Exception:
                newest_action = -1
            if newest_action == move_id_key and display_move_label:
                newest["text"] = display_move_label
                newest["life"] = 1.0
    elif events:
        # The committed FIFO owns event identity.  The live profiler may only
        # enrich the newest chip's text; it may not insert or evict history.
        newest = events[0]
        try:
            newest_action = int(newest.get("action_id") or -1)
        except Exception:
            newest_action = -1
        if newest_action == move_id_key and display_move_label:
            newest["text"] = display_move_label
            newest["life"] = 1.0

    # The chip now exists. Apply the live profiler correction after insertion,
    # which guarantees Hyper Zero Blaster can become Hyper Zero Blaster 2/3.
    _apply_profile_history_correction(
        slot_anim,
        snap,
        move_id_key if move_id_key >= 0 else None,
        display_move_label or base_move_label,
    )
    slot_anim["prev_compact_move_key"] = move_key

    if slot_anim.get("damage_timer", 0) > 0:
        slot_anim["damage_timer"] -= 1

    slot_anim["guard_indicator_life"] = max(0.0, float(slot_anim.get("guard_indicator_life", 0.0)) - 0.008)
    slot_anim["guard_indicator_flash"] = max(0.0, float(slot_anim.get("guard_indicator_flash", 0.0)) - 0.030)


def _draw_compact_meter(screen, x: int, y: int, width: int, meter_value_visual, scale: float, is_dead: bool, spend_sweep: float = 0.0, spend_amount: int = 0, gain_flash: float = 0.0, gain_start: float = 0.0, gain_end: float = 0.0, stock_pop: float = 0.0, stock_pop_index: int = -1, max_flash: float = 0.0) -> int:
    try:
        meter_value = max(0.0, min(50000.0, float(meter_value_visual or 0.0)))
    except (TypeError, ValueError):
        meter_value = 0.0
    full_cells = min(5, int(meter_value // 10000.0))
    partial = 0.0 if full_cells >= 5 else (meter_value - full_cells * 10000.0) / 10000.0
    height = max(8, int(10 * scale))
    rect = pygame.Rect(x, y, max(30, width), height)
    radius = max(1, int(2 * scale))
    pygame.draw.rect(screen, (22, 28, 36), rect, border_radius=radius)
    inner = rect.inflate(-1, -1)
    _draw_vertical_gradient(screen, inner, (40, 48, 60), (28, 35, 46), 255)
    pygame.draw.rect(screen, (132, 146, 168), rect, 1, border_radius=radius)
    pygame.draw.line(screen, (244, 248, 252), (rect.x + 2, rect.y + 1), (rect.right - 3, rect.y + 1), 1)

    inner = rect.inflate(-3, -3)
    gap = max(1, int(2 * scale))
    cell_w = max(3, (inner.width - gap * 4) // 5)
    for index in range(5):
        cell_x = inner.x + index * (cell_w + gap)
        cell = pygame.Rect(cell_x, inner.y, cell_w, inner.height)
        border_radius = max(1, inner.height // 3)
        pygame.draw.rect(screen, (38, 45, 58), cell, border_radius=border_radius)
        pygame.draw.rect(screen, (102, 116, 138), cell, 1, border_radius=border_radius)
        pygame.draw.line(screen, (200, 210, 228), (cell.x + 1, cell.y + 1), (cell.right - 2, cell.y + 1), 1)

        fill_fraction = 1.0 if index < full_cells else (partial if index == full_cells else 0.0)
        if fill_fraction > 0.0:
            fill_w = max(1, min(cell.width, int(round(cell.width * fill_fraction))))
            fill_w_inner = max(1, fill_w - 2)
            fill_h_inner = max(1, cell.height - 2)
            gradient_cell = _compact_meter_gradient_cell(max(1, cell.width - 2), fill_h_inner, index, is_dead)
            source_rect = pygame.Rect(0, 0, min(fill_w_inner, gradient_cell.get_width()), fill_h_inner)
            screen.blit(gradient_cell, (cell.x + 1, cell.y + 1), source_rect)
            fill_right = cell.x + 1 + source_rect.width
            pygame.draw.line(screen, (255, 252, 245), (cell.x + 1, cell.y + 1), (max(cell.x + 1, fill_right - 1), cell.y + 1), 1)
            if fill_fraction < 0.999 and fill_right < cell.right:
                # keep the continuity gate for regression coverage, but do not
                # draw the old bright end-cap slant/emphasis.
                pass

    # Gain is immediate, but the newly acquired region gets a brief broadcast
    # flash so the eye can see what changed without delaying the resource.
    gain_flash = max(0.0, min(1.0, float(gain_flash or 0.0)))
    if gain_flash > 0.01 and inner.width > 0:
        start_ratio = max(0.0, min(1.0, float(gain_start or 0.0) / 50000.0))
        end_ratio = max(start_ratio, min(1.0, float(gain_end or 0.0) / 50000.0))
        gx1 = inner.x + int(inner.width * start_ratio)
        gx2 = inner.x + int(inner.width * end_ratio)
        if gx2 > gx1:
            flash = pygame.Surface((gx2 - gx1, inner.height), pygame.SRCALPHA)
            flash.fill((255, 255, 255, int(72 * gain_flash)))
            screen.blit(flash, (gx1, inner.y), special_flags=pygame.BLEND_RGBA_ADD)
    stock_pop = max(0.0, min(1.0, float(stock_pop or 0.0)))
    if stock_pop > 0.01 and 0 <= int(stock_pop_index) < 5:
        idx = int(stock_pop_index)
        cell_x = inner.x + idx * (cell_w + gap)
        pop_cell = pygame.Rect(cell_x, inner.y, cell_w, inner.height).inflate(int(3 * scale * stock_pop), int(2 * scale * stock_pop))
        pygame.draw.rect(screen, (248, 252, 255, int(210 * stock_pop)), pop_cell, 1, border_radius=max(2, inner.height // 3))
    max_flash = max(0.0, min(1.0, float(max_flash or 0.0)))
    if max_flash > 0.01:
        max_rect = rect.inflate(int(4 * scale * max_flash), int(2 * scale * max_flash))
        pygame.draw.rect(screen, (255, 236, 170, int(210 * max_flash)), max_rect, 1, border_radius=radius + 1)
    # No transient slanted spend sweep. Spend uses the smooth drain itself.
    return rect.width


def _draw_compact_health(
    screen,
    x: int,
    y: int,
    width: int,
    height: int,
    cur,
    maximum,
    is_dead: bool,
    display_fraction=None,
    trail_fraction=None,
    recoverable_fraction=None,
) -> None:
    rect = pygame.Rect(x, y, width, height)
    radius = max(2, min(max(3, int(height * 0.42)), max(2, height // 2)))
    pygame.draw.rect(screen, (22, 28, 36), rect, border_radius=radius)
    inner = rect.inflate(-1, -1)
    _draw_vertical_gradient(screen, inner, (40, 48, 60), (28, 35, 46), 255)
    pygame.draw.rect(screen, (132, 146, 168), rect, 1, border_radius=radius)
    pygame.draw.line(screen, (244, 248, 252), (rect.x + 2, rect.y + 1), (rect.right - 3, rect.y + 1), 1)
    inner = rect.inflate(-3, -3)
    try:
        target_fraction = max(0.0, min(1.0, float(cur or 0) / max(1.0, float(maximum or 1))))
    except (TypeError, ValueError):
        target_fraction = 0.0
    fraction = target_fraction if display_fraction is None else max(0.0, min(1.0, float(display_fraction)))
    trail = fraction if trail_fraction is None else max(fraction, min(1.0, float(trail_fraction)))
    if inner.width > 0 and inner.height > 0 and trail > fraction + 0.001:
        trail_fill = max(1, int(inner.width * trail))
        trail_rect = pygame.Rect(inner.x, inner.y, trail_fill, inner.height)
        _draw_vertical_gradient(screen, trail_rect, (177, 120, 97) if not is_dead else (95, 74, 77), (141, 85, 72) if not is_dead else (71, 54, 58), 255)
        edge_x = trail_rect.right - 1
        pygame.draw.line(screen, (255, 221, 182), (edge_x, inner.y), (edge_x, inner.bottom - 1), 1)
    if recoverable_fraction is not None and inner.width > 0 and inner.height > 0:
        try:
            recoverable = max(fraction, min(1.0, float(recoverable_fraction)))
        except (TypeError, ValueError):
            recoverable = fraction
        if recoverable > fraction + 0.001:
            recoverable_fill = max(1, int(inner.width * recoverable))
            recoverable_rect = pygame.Rect(inner.x, inner.y, recoverable_fill, inner.height)
            _draw_vertical_gradient(screen, recoverable_rect, (196, 92, 125) if not is_dead else (92, 55, 67), (155, 61, 86) if not is_dead else (74, 42, 52), 230)
            pygame.draw.line(screen, (255, 173, 198), (recoverable_rect.x, recoverable_rect.y), (recoverable_rect.right - 1, recoverable_rect.y), 1)
    if inner.width > 0 and inner.height > 0 and fraction > 0.0:
        fill = max(1, int(inner.width * fraction))
        fill_rect = pygame.Rect(inner.x, inner.y, fill, inner.height)
        if is_dead:
            _draw_vertical_gradient(screen, fill_rect, _hud_brighten(COL_HP_DEAD, 42), _hud_darken(COL_HP_DEAD, 24), 255)
            edge_color = (190, 186, 186)
        else:
            fill_surf = pygame.Surface((fill_rect.width, fill_rect.height), pygame.SRCALPHA)
            denom = max(1, inner.width - 1)
            for px in range(fill_rect.width):
                pos = px / denom
                base = _compact_health_gradient_color(pos)
                top = _hud_brighten(base, 28)
                mid = base
                bottom = _hud_darken(base, 22)
                if fill_rect.height <= 2:
                    pygame.draw.line(fill_surf, mid, (px, 0), (px, fill_rect.height - 1))
                else:
                    pygame.draw.line(fill_surf, top, (px, 0), (px, 0))
                    pygame.draw.line(fill_surf, mid, (px, 1), (px, fill_rect.height - 2))
                    pygame.draw.line(fill_surf, bottom, (px, fill_rect.height - 1), (px, fill_rect.height - 1))
            screen.blit(fill_surf, fill_rect.topleft)
            edge_color = _hud_brighten(_compact_health_gradient_color(min(1.0, (fill_rect.width - 1) / denom)), 18)
        highlight_y = fill_rect.y + max(1, fill_rect.height // 3)
        pygame.draw.line(screen, (252, 255, 248), (fill_rect.x, fill_rect.y), (fill_rect.right - 1, fill_rect.y), 1)
        pygame.draw.line(screen, _hud_brighten((236, 244, 236) if not is_dead else (210, 210, 210), 8), (fill_rect.x + 1, highlight_y), (fill_rect.right - 2, highlight_y), 1)
        pygame.draw.line(screen, _hud_darken((160, 170, 180) if is_dead else _compact_health_gradient_color(min(1.0, (fill_rect.width - 1) / max(1, inner.width - 1))), 40), (fill_rect.x, fill_rect.bottom - 1), (fill_rect.right - 1, fill_rect.bottom - 1), 1)
        edge_x = fill_rect.right - 1
        pygame.draw.line(screen, edge_color, (edge_x, inner.y), (edge_x, inner.bottom - 1), 1)
    for ratio in (0.25, 0.50, 0.75):
        tick_x = inner.x + int(inner.width * ratio)
        pygame.draw.line(screen, (15, 18, 24), (tick_x, inner.y + 1), (tick_x, inner.bottom - 2), 1)


def _compact_short_number(value) -> str:
    try:
        value_i = abs(int(value))
    except (TypeError, ValueError):
        return "0"
    if value_i >= 10000:
        scaled = value_i / 1000.0
        return f"{scaled:.1f}K" if value_i % 1000 else f"{int(scaled)}K"
    return str(value_i)


def _compact_action_chip(label: str) -> tuple[str, tuple[int, int, int], str]:
    raw = (label or "").strip()
    low = raw.lower()
    if not raw:
        return "", COL_TEXT_DIM, ""
    if "blockstun" in low:
        return "BLOCK", (255, 190, 88), "STATE"
    if "hitstun" in low:
        return "HITSTUN", (255, 110, 110), "STATE"
    if "knockdown" in low or "down" in low:
        return "DOWN", (255, 110, 110), "STATE"
    return raw.upper(), COL_TEXT, "MOVE"


def _compact_fit_text(font, text: str, max_width: int) -> str:
    text = (text or "").strip()
    if max_width <= 0 or not text:
        return ""
    if font.size(text)[0] <= max_width:
        return text
    suffix = "…"
    suffix_w = font.size(suffix)[0]
    low, high = 0, len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip() + suffix
        if font.size(candidate)[0] <= max_width:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best or suffix


def _compact_rainbow_color(position: float, speed: float = 1.0) -> tuple[int, int, int]:
    phase = time.time() * (speed * 0.58) + position
    return (
        int(166 + 84 * math.sin(phase * 2.05)),
        int(166 + 84 * math.sin(phase * 2.05 + 2.10)),
        int(166 + 84 * math.sin(phase * 2.05 + 4.20)),
    )


def _render_compact_rainbow_text(font, text: str, phase_offset: float = 0.0) -> pygame.Surface:
    base = font.render(text, True, (255, 255, 255))
    gradient = pygame.Surface(base.get_size(), pygame.SRCALPHA)
    width = max(1, base.get_width())
    for px in range(width):
        color = _compact_rainbow_color((px / width) * 3.2 + phase_offset, 1.1)
        pygame.draw.line(gradient, (*color, 255), (px, 0), (px, max(0, base.get_height() - 1)))
    base.blit(gradient, (0, 0), special_flags=pygame.BLEND_MULT)
    return base


def _update_compact_baroque_anim(slot_anim: dict, snap: dict, is_dead: bool, dt: float) -> bool:
    ready = bool((snap or {}).get("baroque_ready_local", False)) and not bool(is_dead)
    previous_alpha = float(slot_anim.get("baroque_alpha", 0.0))
    if ready:
        slot_anim["baroque_last_pct"] = float((snap or {}).get("baroque_red_pct_max") or 0.0)
        slot_anim["baroque_display_pct"] = _approach(
            float(slot_anim.get("baroque_display_pct", 0.0)),
            float(slot_anim.get("baroque_last_pct", 0.0)),
            80.0,
            dt,
        )
        slot_anim["baroque_alpha"] = _approach(previous_alpha, 1.0, 5.5, dt)
    else:
        slot_anim["baroque_alpha"] = _approach(previous_alpha, 0.0, 2.1, dt)
    current_alpha = float(slot_anim.get("baroque_alpha", 0.0))
    if current_alpha > previous_alpha + 0.0001:
        slot_anim["baroque_fade_direction"] = 1
    elif current_alpha < previous_alpha - 0.0001:
        slot_anim["baroque_fade_direction"] = -1
    else:
        slot_anim["baroque_fade_direction"] = 0
    return current_alpha > 0.03


def _draw_compact_baroque_badge(screen, font_sm, rect: pygame.Rect, percent: float, scale: float, is_left: bool, alpha: float = 1.0, fade_direction: int = 0, owner_label: str = "", change_flash: float = 0.0) -> None:
    pct = max(0.0, float(percent or 0.0))
    fade = max(0.0, min(1.0, float(alpha or 0.0)))
    if fade <= 0.01 or rect.width <= 2 or rect.height <= 2:
        return

    entering = int(fade_direction or 0) > 0
    leaving = int(fade_direction or 0) < 0
    # Entry is revealed by the wipe itself, so avoid multiplying the content
    # down to near-invisible alpha a second time. Exit keeps the approved fade.
    visual_fade = min(1.0, 0.34 + 0.66 * math.sqrt(fade)) if entering else fade

    radius = max(4, int(5 * scale))
    pulse = 0.70 + 0.30 * ((math.sin(time.time() * 3.8) + 1.0) * 0.5)
    border = _compact_rainbow_color(0.16 + pct * 0.004, 1.0)
    border_glow = _compact_rainbow_color(0.48 + pct * 0.003, 1.15)

    glow = pygame.Surface((rect.width + max(6, int(8 * scale)), rect.height + max(6, int(8 * scale))), pygame.SRCALPHA)
    glow_rect = pygame.Rect(max(3, int(4 * scale)), max(3, int(4 * scale)), rect.width, rect.height)
    pygame.draw.rect(glow, (*border_glow, int(22 * visual_fade * pulse)), glow_rect, border_radius=radius + 2)
    screen.blit(glow, (rect.x - glow_rect.x, rect.y - glow_rect.y))

    badge = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
    pygame.draw.rect(badge, (14, 18, 28, int(212 * visual_fade)), badge.get_rect(), border_radius=radius)
    pygame.draw.rect(badge, (22, 27, 39, int(188 * visual_fade)), badge.get_rect(), 1, border_radius=radius)
    pygame.draw.rect(badge, (*border, int(210 * visual_fade)), pygame.Rect(1, 1, max(1, rect.width - 2), max(2, int(2 * scale))), border_top_left_radius=radius, border_top_right_radius=radius)

    label_pad_x = max(5, int(6 * scale))
    label_gap = max(4, int(5 * scale))
    owner = str(owner_label or "").strip().upper()
    badge_label = f"{owner} BBQ" if owner else "BBQ"
    label_text = font_sm.render(badge_label, True, (225, 232, 246))
    value_text = _render_compact_rainbow_text(font_sm, f"{pct:.2f}%", 0.18)

    chip_h = max(int(13 * scale), label_text.get_height() + max(2, int(3 * scale)))
    chip_w = max(int(24 * scale), label_text.get_width() + label_pad_x * 2)
    chip_y = badge.get_height() // 2 - chip_h // 2
    chip_x = max(4, int(5 * scale))
    chip_rect = pygame.Rect(chip_x, chip_y, chip_w, chip_h)
    pygame.draw.rect(badge, (34, 42, 60, int(216 * visual_fade)), chip_rect, border_radius=max(3, int(4 * scale)))
    pygame.draw.rect(badge, (*border, int(148 * visual_fade)), chip_rect, 1, border_radius=max(3, int(4 * scale)))

    label_text.set_alpha(int(245 * visual_fade))
    value_text.set_alpha(int(255 * visual_fade))
    badge.blit(label_text, (chip_rect.centerx - label_text.get_width() // 2, chip_rect.centery - label_text.get_height() // 2))

    value_x = chip_rect.right + label_gap
    available_w = rect.width - value_x - max(5, int(6 * scale))
    if value_text.get_width() > available_w and available_w > 8:
        scale_mul = available_w / max(1, value_text.get_width())
        value_text = pygame.transform.smoothscale(value_text, (max(1, int(value_text.get_width() * scale_mul)), max(1, int(value_text.get_height() * scale_mul))))
        value_text.set_alpha(int(255 * visual_fade))
    badge.blit(value_text, (value_x, badge.get_height() // 2 - value_text.get_height() // 2))

    if pct >= 1.0:
        marker_r = max(2, int(2 * scale))
        marker_x = rect.width - max(7, int(8 * scale))
        marker_y = badge.get_height() // 2
        pygame.draw.circle(badge, (*border_glow, int(210 * visual_fade * pulse)), (marker_x, marker_y), marker_r)

    if fade < 0.995:
        soft_w = max(10, int(16 * scale))
        if is_left:
            solid_w = max(0, min(rect.width, int(rect.width * fade)))
            edge_start = solid_w
            edge_end = min(rect.width, solid_w + soft_w)
            wipe_center = edge_start + soft_w * 0.45
        else:
            solid_start = max(0, rect.width - int(rect.width * fade))
            edge_start = max(0, solid_start - soft_w)
            edge_end = solid_start
            wipe_center = edge_end - soft_w * 0.45

        mask = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
        for col in range(rect.width):
            col_alpha = 0
            if is_left:
                if col < edge_start:
                    col_alpha = 255
                elif col < edge_end:
                    t = 1.0 - ((col - edge_start) / max(1, edge_end - edge_start))
                    col_alpha = int(255 * t)
            else:
                if col >= edge_end:
                    col_alpha = 255
                elif col >= edge_start:
                    t = (col - edge_start) / max(1, edge_end - edge_start)
                    col_alpha = int(255 * t)
            if col_alpha > 0:
                pygame.draw.line(mask, (255, 255, 255, col_alpha), (col, 0), (col, rect.height - 1))
        badge.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)

        wipe = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
        if entering:
            # Softest at the endpoints, clearest while the reveal edge crosses.
            wipe_alpha = max(0.0, min(1.0, math.sin(math.pi * fade)))
        elif leaving:
            wipe_alpha = max(0.0, min(1.0, 1.0 - fade))
        else:
            wipe_alpha = max(0.0, min(1.0, 1.0 - fade))
        band_w = max(10, int(18 * scale))
        for idx in range(4):
            color = _compact_rainbow_color(0.12 + idx * 0.12 + time.time() * 0.08, 1.0)
            offset = (idx - 1.5) * max(2, int(3 * scale))
            cx = wipe_center + offset
            poly = [
                (int(cx - band_w), 0),
                (int(cx + band_w * 0.35), 0),
                (int(cx - band_w * 0.25), rect.height),
                (int(cx - band_w * 1.60), rect.height),
            ]
            pygame.draw.polygon(wipe, (*color, int((18 + idx * 6) * wipe_alpha)), poly)
        badge.blit(wipe, (0, 0))

    screen.blit(badge, rect.topleft)
    change_flash = max(0.0, min(1.0, float(change_flash or 0.0)))
    if change_flash > 0.01:
        flash_rect = rect.inflate(max(2, int(4 * scale * change_flash)), max(2, int(2 * scale * change_flash)))
        pygame.draw.rect(screen, (250, 248, 255, int(180 * change_flash)), flash_rect, 1, border_radius=radius + 2)


def _compact_chip_color(color: tuple[int, int, int], life: float) -> tuple[int, int, int]:
    weight = max(0.30, min(1.0, float(life)))
    return tuple(int(28 + (component - 28) * weight) for component in color)


def _compact_smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, float(value)))
    return value * value * (3.0 - 2.0 * value)


def _compact_lock_ease(value: float) -> float:
    """Ease into place with a restrained overshoot, then settle at one."""
    value = max(0.0, min(1.0, float(value)))
    overshoot = 0.56
    shifted = value - 1.0
    return 1.0 + (overshoot + 1.0) * shifted * shifted * shifted + overshoot * shifted * shifted


def _hud_stage_progress(age: float, start: float, duration: float = 0.18) -> float:
    if duration <= 0.0:
        return 1.0 if age >= start else 0.0
    return _compact_lock_ease((float(age) - float(start)) / float(duration))


def _blit_hud_stage(
    destination: pygame.Surface,
    layer: pygame.Surface,
    source_rect: pygame.Rect,
    destination_x: int,
    destination_y: int,
    progress: float,
    is_left: bool,
    scale: float,
    clip_rect: pygame.Rect | None = None,
    vertical_lift: float = 2.0,
) -> None:
    if progress <= 0.001:
        return
    source_rect = pygame.Rect(source_rect).clip(layer.get_rect())
    if source_rect.width <= 0 or source_rect.height <= 0:
        return
    alpha_progress = max(0.0, min(1.0, float(progress)))
    staged = layer.subsurface(source_rect).copy()
    staged.set_alpha(max(0, min(255, int(255 * alpha_progress))))
    travel = max(14, int(24 * scale))
    direction = -1 if is_left else 1
    offset_x = int((1.0 - float(progress)) * travel * direction)
    offset_y = int((1.0 - alpha_progress) * float(vertical_lift) * scale)
    previous_clip = destination.get_clip()
    if clip_rect is not None:
        destination.set_clip(clip_rect)
    destination.blit(
        staged,
        (destination_x + source_rect.x + offset_x, destination_y + source_rect.y + offset_y),
    )
    destination.set_clip(previous_clip)


def _draw_match_assembly_spine(screen, scale: float, dt: float) -> None:
    if not bool(_anim_state.get("assembly_active", False)):
        return
    age = max(0.0, float(_anim_state.get("assembly_age", 0.0)) + dt)
    _anim_state["assembly_age"] = age
    if age >= 0.72:
        _anim_state["assembly_active"] = False
        return
    build = _compact_lock_ease(age / 0.24)
    release = max(0.0, min(1.0, (age - 0.30) / 0.42))
    fade = 1.0 - release
    center_x = screen.get_width() // 2
    y = int(138 * scale)
    half_span = int((18 + 166 * build) * scale)
    gap = max(5, int(7 * scale))
    rail_alpha = int(112 * fade)
    core_alpha = int(190 * fade * min(1.0, build * 1.4))
    pygame.draw.line(screen, (154, 196, 255, rail_alpha), (center_x - half_span, y), (center_x - gap, y), max(1, int(2 * scale)))
    pygame.draw.line(screen, (255, 135, 135, rail_alpha), (center_x + gap, y), (center_x + half_span, y), max(1, int(2 * scale)))
    lock_h = max(8, int(17 * scale * build))
    pygame.draw.line(screen, (242, 247, 255, core_alpha), (center_x, y - lock_h), (center_x, y + lock_h), max(1, int(2 * scale)))
    if 0.18 <= age <= 0.44:
        flash = math.sin(math.pi * ((age - 0.18) / 0.26))
        pygame.draw.circle(screen, (255, 255, 255, int(86 * flash * fade)), (center_x, y), max(2, int(5 * scale)), 1)


def _maybe_restart_match_assembly(slots: dict) -> None:
    snapshots = [snap for snap in slots.values() if isinstance(snap, dict) and snap.get("max")]
    if not snapshots:
        return
    ratios = []
    for snap in snapshots:
        try:
            ratios.append(max(0.0, min(1.0, float(snap.get("cur") or 0.0) / max(1.0, float(snap.get("max") or 1.0)))))
        except (TypeError, ValueError):
            pass
    if not ratios:
        return
    if min(ratios) <= 0.45:
        _anim_state["match_reset_armed"] = True
    reset_ready = bool(_anim_state.get("match_reset_armed", False)) and min(ratios) >= 0.95
    enough_frames = (_frame - int(_anim_state.get("last_match_reset_frame", -9999))) > 120
    if reset_ready and enough_frames:
        _anim_state["last_match_reset_frame"] = _frame
        _anim_state["match_reset_armed"] = False
        _restart_hud_entrance()


def _restart_hud_entrance() -> None:
    _anim_state["overlay_alpha"] = 0.0
    _anim_state["assembly_age"] = 0.0
    _anim_state["assembly_active"] = True
    for team_anim in _anim_state.get("teams", {}).values():
        team_anim["present"] = False
        team_anim["entrance_age"] = 0.0
        team_anim["entrance_active"] = True
        team_anim["alpha"] = 0.0
        team_anim["tag_lock_pending"] = False
        team_anim["tag_lock_flash"] = 0.0
        team_anim["impact_recoil_age"] = 1.0
        team_anim["impact_recoil_power"] = 0.0


def _compact_event_fade(event: dict | None) -> float:
    if not event:
        return 1.0
    age = float(event.get("age", 1.0))
    life = float(event.get("life", 1.0))
    fade_in = _compact_smoothstep(age / 0.15)
    fade_out = _compact_smoothstep(life / 0.28)
    return min(fade_in, fade_out)


def _draw_compact_stat_chip(
    screen,
    font_sm,
    x: int,
    y: int,
    label: str,
    value: str,
    color: tuple[int, int, int],
    scale: float,
    life: float = 1.0,
    event: dict | None = None,
    rainbow: bool = False,
) -> int:
    if rainbow:
        label_surface = _render_compact_rainbow_text(font_sm, label, 0.10)
        value_surface = _render_compact_rainbow_text(font_sm, value, 0.48)
    else:
        label_surface = font_sm.render(label, True, (142, 151, 169))
        value_surface = font_sm.render(value, True, _compact_chip_color(color, life))
    pad_x = max(4, int(5 * scale))
    seg_gap = max(2, int(2 * scale))
    label_w = label_surface.get_width() + pad_x * 2
    value_w = value_surface.get_width() + pad_x * 2
    height = max(int(15 * scale), label_surface.get_height() + int(4 * scale))
    width = label_w + seg_gap + value_w
    fade = _compact_event_fade(event)
    if fade <= 0.01:
        return width

    rise = int((1.0 - fade) * max(1, int(3 * scale)))
    chip = pygame.Surface((width, height), pygame.SRCALPHA)
    radius = max(2, int(2 * scale))
    border = _compact_rainbow_color(0.28, 1.1) if rainbow else _compact_chip_color(color, life)
    label_rect = pygame.Rect(0, 0, label_w, height)
    value_rect = pygame.Rect(label_w + seg_gap, 0, value_w, height)
    pygame.draw.rect(chip, (16, 21, 31, int(222 * fade)), chip.get_rect(), border_radius=radius)
    pygame.draw.rect(chip, (26, 31, 43, int(232 * fade)), label_rect, border_radius=radius)
    pygame.draw.rect(chip, (*border, int(66 * fade)), value_rect, border_radius=radius)
    pygame.draw.rect(chip, (*border, int(235 * fade)), chip.get_rect(), 1, border_radius=radius)
    if seg_gap > 0:
        pygame.draw.line(chip, (*border, int(160 * fade)), (label_rect.right + seg_gap // 2, 2), (label_rect.right + seg_gap // 2, height - 3), 1)
    label_surface.set_alpha(int(235 * fade))
    value_surface.set_alpha(int(255 * fade))
    chip.blit(label_surface, (label_rect.x + pad_x, chip.get_height() // 2 - label_surface.get_height() // 2))
    chip.blit(value_surface, (value_rect.x + pad_x, chip.get_height() // 2 - value_surface.get_height() // 2))
    screen.blit(chip, (x, y - rise))
    return width


def _compact_tick_event_queue(events: list[dict]) -> dict | None:
    if not events:
        return None
    event = events[0]
    event["age"] = float(event.get("age", 0.0)) + 0.014
    event["life"] = float(event.get("life", 0.0)) - 0.014
    active = event if event.get("life", 0.0) > 0.0 else None
    events[:] = [entry for entry in events if entry.get("life", 0.0) > 0.0]
    return active


def _compact_consume_panel_events(slot_anim: dict) -> tuple[dict | None, dict | None, dict | None, dict | None]:
    damage_event = _compact_tick_event_queue(slot_anim.get("damage_events", []))
    meter_event = _compact_tick_event_queue(slot_anim.get("meter_events", []))
    advantage_event = _compact_tick_event_queue(slot_anim.get("adv_events", []))
    baroque_event = _compact_tick_event_queue(slot_anim.get("baroque_events", []))
    return damage_event, meter_event, advantage_event, baroque_event


def _draw_compact_info_strip(
    screen,
    font_sm,
    slot_anim: dict,
    x: int,
    y: int,
    right: int,
    action_label: str,
    scale: float,
) -> None:
    """Draw current action/state first, then spend spare width on live events.

    STATE is no longer a separate labeled box. Reaction states become compact
    badges in the same ribbon as MOVE, while transient damage/meter/frame/BBQ
    chips pack from the right edge inward. History below remains untouched.
    """
    damage_event, meter_event, advantage_event, baroque_event = _compact_consume_panel_events(slot_anim)
    chips: list[tuple[str, str, tuple[int, int, int], float, dict | None, bool]] = []

    if damage_event is not None:
        event_type = str(damage_event.get("type") or "")
        if event_type == "opponent":
            label, color = "DMG OUT", (255, 110, 110)
        elif event_type == "heal":
            label, color = "HP +", (92, 232, 146)
        else:
            label, color = "DMG IN", (255, 110, 110)
        chips.append((label, _compact_short_number(damage_event.get("value", 0)), color, float(damage_event.get("life", 1.0)), damage_event, False))

    if meter_event is not None:
        direction = str(meter_event.get("direction") or "gain")
        gain = direction != "loss"
        chips.append(("MTR", f"{'+' if gain else '-'}{_compact_short_number(meter_event.get('value', 0))}", (96, 182, 255) if gain else (255, 164, 92), float(meter_event.get("life", 1.0)), meter_event, False))

    if advantage_event is not None:
        value = int(advantage_event.get("value", 0))
        value_text = f"{value:+d}" if value else "0"
        color = (92, 232, 146) if value > 0 else ((255, 112, 112) if value < 0 else (196, 205, 220))
        chips.append(("FRAME", value_text, color, float(advantage_event.get("life", 1.0)), advantage_event, False))

    if baroque_event is not None:
        value = float(baroque_event.get("value", 0.0))
        value_text = f"{value:+.0f}%"
        color = (172, 112, 255) if value > 0 else (255, 180, 92)
        chips.append(("BBQ", value_text, color, float(baroque_event.get("life", 1.0)), baroque_event, True))

    action_text, action_color, action_kind = _compact_action_chip(action_label)
    gap = max(4, int(5 * scale))
    action_right = x
    strip_width = max(0, right - x)
    action_cap = max(72, int(strip_width * 0.52))

    if action_text:
        if action_kind == "STATE":
            # State is a badge inside the action ribbon, not another labeled
            # telemetry box. This keeps DOWN/BLOCK/HITSTUN obvious but cheap.
            value = _compact_fit_text(font_sm, action_text, action_cap - max(10, int(12 * scale)))
            if value:
                state_chip = _render_compact_text_chip(font_sm, value, action_color, scale, alpha=0.95)
                screen.blit(state_chip, (x, y))
                action_right = x + state_chip.get_width()
        else:
            label_surface = font_sm.render("MOVE", True, (142, 151, 169))
            pad_x = max(4, int(5 * scale))
            text_gap = max(3, int(4 * scale))
            available = action_cap - label_surface.get_width() - pad_x * 2 - text_gap
            value = _compact_fit_text(font_sm, action_text, available)
            if value:
                action_right = x + _draw_compact_stat_chip(screen, font_sm, x, y, "MOVE", value, action_color, scale, 1.0)

    # Transient telemetry packs from the right edge inward. The current action
    # therefore never disappears just because several meter/damage events fired.
    event_right = right
    min_left = action_right + (gap if action_right > x else 0)
    for label, value, color, life, event, rainbow in chips:
        if rainbow:
            label_surface = _render_compact_rainbow_text(font_sm, label, 0.10)
            value_surface = _render_compact_rainbow_text(font_sm, value, 0.48)
        else:
            label_surface = font_sm.render(label, True, (142, 151, 169))
            value_surface = font_sm.render(value, True, color)
        pad_x = max(4, int(5 * scale))
        seg_gap = max(2, int(2 * scale))
        width = label_surface.get_width() + value_surface.get_width() + pad_x * 4 + seg_gap
        chip_x = event_right - width
        if chip_x < min_left:
            continue
        _draw_compact_stat_chip(screen, font_sm, chip_x, y, label, value, color, scale, life, event, rainbow=rainbow)
        event_right = chip_x - gap

def _render_compact_text_chip(font_sm, primary: str, color: tuple[int, int, int], scale: float = 1.0, alpha: float = 1.0, secondary: str = "", rainbow: bool = False, emphasis: float = 1.0) -> pygame.Surface:
    pad_x = max(5, int(6 * scale))
    pad_y = max(2, int(3 * scale))
    inner_gap = max(3, int(4 * scale))

    prim = str(primary or "").strip()
    sec = str(secondary or "").strip()
    if rainbow and prim:
        prim_surf = _render_compact_rainbow_text(font_sm, prim, 0.24)
    else:
        prim_surf = font_sm.render(prim, True, color)
    if emphasis != 1.0:
        prim_surf = pygame.transform.smoothscale(prim_surf, (max(1, int(prim_surf.get_width() * emphasis)), max(1, int(prim_surf.get_height() * emphasis))))
    sec_surf = font_sm.render(sec, True, (174, 184, 200)) if sec else None

    width = prim_surf.get_width() + pad_x * 2
    height = prim_surf.get_height() + pad_y * 2
    if sec_surf is not None:
        width += inner_gap + sec_surf.get_width()
        height = max(height, sec_surf.get_height() + pad_y * 2)

    surf = pygame.Surface((max(1, width), max(1, height)), pygame.SRCALPHA)
    bg_alpha = max(0, min(255, int(188 * alpha)))
    border_alpha = max(0, min(255, int(162 * alpha)))
    rect = pygame.Rect(0, 0, width, height)
    radius = max(2, int(2 * scale))
    pygame.draw.rect(surf, (32, 39, 50, bg_alpha), rect, border_radius=radius)
    top_band = pygame.Rect(0, 0, width, max(1, height // 2))
    _draw_vertical_gradient(surf, top_band, (46, 56, 72), (32, 39, 50), max(0, min(255, int(76 * alpha))))
    pygame.draw.rect(surf, (104, 120, 144, border_alpha), rect, 1, border_radius=radius)
    accent_w = max(3, int(4 * scale))
    pygame.draw.rect(surf, (*color, max(0, min(255, int(214 * alpha)))), (0, 0, accent_w, height), border_radius=radius)
    pygame.draw.line(surf, (246, 248, 252, max(0, min(255, int(70 * alpha)))), (accent_w + 2, 1), (width - 3, 1), 1)
    pygame.draw.line(surf, (10, 12, 16, max(0, min(255, int(64 * alpha)))), (accent_w + 2, height - 2), (width - 3, height - 2), 1)

    if alpha < 0.999:
        prim_surf.set_alpha(max(0, min(255, int(255 * alpha))))
        if sec_surf is not None:
            sec_surf.set_alpha(max(0, min(255, int(255 * alpha))))
    dx = pad_x + accent_w - 1
    surf.blit(prim_surf, (dx, (height - prim_surf.get_height()) // 2))
    dx += prim_surf.get_width()
    if sec_surf is not None:
        dx += inner_gap
        surf.blit(sec_surf, (dx, (height - sec_surf.get_height()) // 2))
    return surf


def _history_label_width(font_sm, scale: float) -> int:
    labels = ("LOG", "INPUTS", "FRAMES", "HOLD", "MOVES", "DMG MOD", "METER+", "RED HP", "ATK PROP")
    return max(font_sm.size(label)[0] for label in labels) + max(16, int(20 * scale))


def _draw_history_header_chip(screen, font_sm, title: str, x: int, y: int, scale: float) -> int:
    label_surface = font_sm.render(title, True, (194, 208, 228))
    accent_w = max(3, int(4 * scale))
    pad_x = max(6, int(7 * scale))
    label_h = max(font_sm.get_height() + max(4, int(5 * scale)), int(15 * scale))
    label_w = label_surface.get_width() + accent_w + pad_x * 2
    key = (id(font_sm), str(title), label_w, label_h, round(float(scale), 3))
    chip = _HISTORY_HEADER_CHIP_CACHE.get(key)
    if chip is None:
        chip = pygame.Surface((label_w, label_h), pygame.SRCALPHA)
        radius = max(2, int(2 * scale))
        pygame.draw.rect(chip, (26, 33, 43, 204), (0, 0, label_w, label_h), border_radius=radius)
        _draw_vertical_gradient(chip, pygame.Rect(0, 0, label_w, max(1, label_h // 2)), (42, 51, 66), (26, 33, 43), 88)
        pygame.draw.rect(chip, (86, 102, 126, 182), (0, 0, label_w, label_h), 1, border_radius=radius)
        pygame.draw.rect(chip, (86, 142, 228, 220), (0, 0, accent_w, label_h), border_radius=radius)
        pygame.draw.line(chip, (246, 248, 252, 60), (accent_w + 2, 1), (label_w - 3, 1), 1)
        chip.blit(label_surface, (accent_w + pad_x, (label_h - label_surface.get_height()) // 2))
        if len(_HISTORY_HEADER_CHIP_CACHE) >= 32:
            _HISTORY_HEADER_CHIP_CACHE.clear()
        _HISTORY_HEADER_CHIP_CACHE[key] = chip
    screen.blit(chip, (x, y))
    return label_w


def _compact_red_health_values(snap: dict) -> tuple[int, int, int, float, int, int, str]:
    current = _panel_int(snap.get("red_health_current", snap.get("cur")), 0)
    maximum = max(1, _panel_int(snap.get("max"), 1))
    auxiliary = _panel_int(
        snap.get("red_health_aux", snap.get("recoverable_ceiling")),
        current,
    )
    auxiliary = max(current, min(maximum, auxiliary))
    recoverable = max(
        0,
        _panel_int(
            snap.get("red_health_recoverable", snap.get("recoverable_hp")),
            auxiliary - current,
        ),
    )
    recoverable_pct = float(
        snap.get(
            "red_health_pct_max",
            snap.get("recoverable_pct_max", recoverable * 100.0 / maximum),
        )
        or 0.0
    )
    pending_current = _panel_int(snap.get("red_health_pending_current"), 0)
    pending_aux = _panel_int(snap.get("red_health_pending_aux"), 0)
    event = str(snap.get("red_health_last_event") or "").replace("_", " ").upper()
    return current, auxiliary, recoverable, recoverable_pct, pending_current, pending_aux, event




def _attack_property_a_ui(raw_value: int) -> tuple[str, str]:
    """Return plain-language guard and strength labels for property A."""
    raw = int(raw_value) & 0xFFFFFFFF
    guard_mask = raw & 0x38
    strength_mask = raw & 0x07

    guards = []
    for bit, label in ((0x08, "MID"), (0x10, "HIGH"), (0x20, "LOW")):
        if guard_mask & bit:
            guards.append(label)
    if guards:
        guard = "/".join(guards)
    elif strength_mask:
        guard = "UNBLOCKABLE"
    else:
        guard = "NO GUARD DATA"

    strengths = []
    for bit, label in ((0x01, "LIGHT"), (0x02, "MEDIUM"), (0x04, "HEAVY")):
        if strength_mask & bit:
            strengths.append(label)
    strength = "/".join(strengths) if strengths else "NO HIT TIER"
    return guard, strength


def _attack_property_b_ui(
    raw_value: int,
    *,
    source_kind: str = "script",
) -> list[tuple[str, tuple[int, int, int]]]:
    """Translate native Property B without promoting correlations to facts."""
    raw = int(raw_value) & 0xFFFFFFFF
    projectile = str(source_kind or "script").strip().lower() in {"projectile", "actor", "spawned_actor"}
    out: list[tuple[str, tuple[int, int, int]]] = []

    if projectile:
        # The native registry holds any spawned attack object, not only travel
        # projectiles. Keep actor-family labels broad until individual low bits
        # are proven across bullets, weapons, summons, and capture objects.
        if raw & 0x00000002:
            out.append(("SPAWNED-ATTACK CORE FLAG (CORRELATED)", (109, 209, 205)))
        if raw & 0x00000008:
            out.append(("ALTERNATE NORMAL/CONTACT ROUTE 0x00000008 (UNRESOLVED)", (255, 174, 115)))
        if raw & 0x00000080:
            out.append(("ACTOR CONTACT FAMILY 0x80 (CORRELATED)", (185, 139, 238)))
        if raw & 0x00000100:
            out.append(("INITIAL ACTOR PHASE", (255, 174, 115)))
        if raw & 0x00000020:
            out.append(("SUSTAINED-CONTACT ACTOR ROUTE", (185, 139, 238)))
        if raw & 0x00000010:
            out.append(("TARGET-ACQUIRED / CONTACT-LOCK (CORRELATED)", (255, 174, 115)))
        if raw & 0x00000001:
            out.append(("NATIVE RESULT MODIFIER +0004", (236, 188, 92)))
        if raw & 0x00000040:
            out.append(("STANDARD STRIKE BASELINE", (127, 205, 255)))
        if raw & 0x00040000:
            out.append(("SPECIAL CHIP ROUTE B · 1/8", (236, 188, 92)))
        if raw & 0x00400000:
            out.append(("GROUND CAPTURE ROUTE CORE", (255, 151, 105)))
        if raw & 0x00080000:
            out.append(("CAPTURE/CINEMATIC MOD 0x00080000 (CORRELATED)", (255, 174, 115)))
        if raw & 0x40000000:
            out.append(("RESULT PROPAGATION MODIFIER +1", (255, 151, 105)))

        known = (
            0x00000001 | 0x00000002 | 0x00000008 | 0x00000010 | 0x00000020 | 0x00000040 | 0x00000080 |
            0x00000100 | 0x00040000 | 0x00080000 | 0x00400000 | 0x40000000
        )
        unknown = raw & ~known
        if unknown:
            out.append((f"ACTOR B UNRESOLVED 0x{unknown:08X}", (255, 112, 120)))
        if not out:
            out.append(("NO SPAWNED-ACTOR B FLAGS", (126, 139, 158)))
        return out

    # Exact native combinations are clearer than pretending each low bit has a
    # universal meaning. These names are tied to repeated raw captures.
    exact: dict[int, list[tuple[str, tuple[int, int, int]]]] = {
        0x00000015: [
            ("CAPTURE TRIGGER PACKET", (255, 151, 105)),
        ],
        0x00400014: [
            ("GROUND CAPTURE PACKET", (255, 151, 105)),
        ],
        0x400C0055: [
            ("LEVEL 3 CINEMATIC CAPTURE PACKET", (255, 151, 105)),
        ],
        0x40080055: [
            ("CINEMATIC CAPTURE / LAUNCH PACKET (CORRELATED)", (255, 151, 105)),
        ],
        0x40000041: [
            ("RESULT PROPAGATION MODIFIER +1", (255, 151, 105)),
            ("STANDARD STRIKE BASELINE", (127, 205, 255)),
            ("NATIVE RESULT MODIFIER +0004", (236, 188, 92)),
        ],
        0x01000001: [
            ("REPEAT-CONTACT HANDLING", (255, 174, 115)),
            ("NATIVE RESULT MODIFIER +0004", (236, 188, 92)),
        ],
        0x01000041: [
            ("REPEAT-CONTACT HANDLING", (255, 174, 115)),
            ("STANDARD STRIKE BASELINE", (127, 205, 255)),
            ("NATIVE RESULT MODIFIER +0004", (236, 188, 92)),
        ],
        0x00000041: [
            ("STANDARD STRIKE BASELINE", (127, 205, 255)),
            ("NATIVE RESULT MODIFIER +0004", (236, 188, 92)),
        ],
    }
    if raw in exact:
        return exact[raw]

    if raw & 0x00000040:
        out.append(("STANDARD STRIKE BASELINE", (127, 205, 255)))
    if raw & 0x00000008:
        out.append(("ALTERNATE NORMAL/CONTACT ROUTE 0x00000008 (UNRESOLVED)", (255, 174, 115)))
    if raw & 0x00040000:
        out.append(("SPECIAL CHIP ROUTE B · 1/8", (236, 188, 92)))
    if raw & 0x00000001:
        out.append(("NATIVE RESULT MODIFIER +0004", (236, 188, 92)))
    if raw & 0x01000000:
        out.append(("REPEAT-CONTACT HANDLING", (255, 174, 115)))
    if raw & 0x40000000:
        out.append(("RESULT PROPAGATION MODIFIER +1", (255, 151, 105)))
    if raw & 0x00400000:
        out.append(("GROUND CAPTURE ROUTE CORE", (255, 151, 105)))
    if raw & 0x00080000:
        out.append(("CAPTURE/CINEMATIC MOD 0x00080000 (CORRELATED)", (255, 174, 115)))

    known = (
        0x00000001 | 0x00000008 | 0x00000040 | 0x00040000 | 0x00400000 |
        0x00080000 | 0x01000000 | 0x40000000
    )
    unknown = raw & ~known
    if unknown:
        out.append((f"B UNRESOLVED 0x{unknown:08X}", (255, 112, 120)))
    if not out:
        out.append(("NO B FLAGS", (126, 139, 158)))
    return out

def _attack_scaling_ui(attack: dict) -> str:
    loss = attack.get("attack_property_live_scaling_loss_per_hit")
    floor = attack.get("attack_property_live_scaling_floor")
    try:
        loss_pct = int(round(float(loss) * 100.0))
        floor_pct = int(round(float(floor) * 100.0))
    except Exception:
        return "PRORATION UNKNOWN"
    return f"PRORATE {loss_pct}% / MIN {floor_pct}%"


def _attack_source_ui(attack_slot: str, attack: dict) -> str:
    source_badge = "C1" if str(attack_slot or "").endswith("C1") else "C2"
    packet_state = str(attack.get("attack_property_packet_state") or "CONTACT").upper()
    try:
        sequence = int(attack.get("attack_property_event_sequence") or 0)
    except Exception:
        sequence = 0
    action_name = str(attack.get("attack_property_packet_action_name") or "").strip().upper()
    event_label = f"{source_badge} {packet_state}"
    if sequence:
        event_label += f" #{sequence}"
    if action_name and action_name not in {"IDLE", "UNKNOWN"}:
        return f"{event_label}: {action_name}"
    return event_label


def _attack_optional_int(value):
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _attack_optional_float(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _attack_property_phase_rows(attack: dict) -> list[dict]:
    """Return native action-script phases only, with no profile merge."""
    attack = attack if isinstance(attack, dict) else {}
    native = [dict(row) for row in (attack.get("attack_property_phases") or []) if isinstance(row, dict)]
    count = max(len(native), _panel_int(attack.get("attack_property_phase_count"), 0))
    if (
        count <= 0
        and bool(attack.get("attack_property_display_active"))
        and not attack.get("attack_property_projectiles")
    ):
        count = 1

    rows: list[dict] = []
    for index in range(count):
        row = dict(native[index]) if index < len(native) else {}
        row["phase_index"] = _panel_int(row.get("phase_index"), index + 1)
        row["source"] = "native_action_script"
        if index == 0:
            row.setdefault("property_a", attack.get("attack_property_live_a"))
            row.setdefault("property_b", attack.get("attack_property_live_b"))
        elif index == 1:
            row.setdefault("property_a", attack.get("attack_property_live_phase_a"))
            row.setdefault("property_b", attack.get("attack_property_live_phase_b"))
        if row.get("a_result_flags_raw") is None and row.get("hit_result_raw") is not None:
            row["a_result_flags_raw"] = row.get("hit_result_raw")
        if row.get("a_result_code") is None and row.get("a_result_flags_raw") is not None:
            row["a_result_code"] = (_panel_int(row.get("a_result_flags_raw"), 0) >> 8) & 0x00FFFFFF
        if row.get("hit_reaction") is None and row.get("a_result_code") is not None:
            row["hit_reaction"] = row.get("a_result_code")
        if "native_operations" not in row:
            row["native_operations"] = [
                dict(operation)
                for operation in (row.get("operations") or [])
                if isinstance(operation, dict)
            ]
        rows.append(row)
    return rows

def _attack_property_phase_groups(rows_or_attack) -> list[dict]:
    """Collapse identical native script blocks without discarding raw rows."""
    if isinstance(rows_or_attack, dict):
        rows = _attack_property_phase_rows(rows_or_attack)
    else:
        rows = [dict(row) for row in (rows_or_attack or []) if isinstance(row, dict)]
    groups: list[dict] = []
    by_signature: dict[tuple, dict] = {}
    for row in rows:
        signature = (
            _panel_int(row.get("property_a_initial", row.get("property_a")), 0),
            _panel_int(row.get("property_a_final", row.get("property_a")), 0),
            _panel_int(row.get("property_a_post_result_or_mask"), 0),
            _panel_int(row.get("property_b_initial", row.get("property_b")), 0),
            _panel_int(row.get("property_b"), 0),
            _panel_int(row.get("a_result_flags_raw", row.get("hit_result_raw")), -1),
            _panel_int(row.get("property_a_initial_unknown_mask"), 0),
        )
        group = by_signature.get(signature)
        if group is None:
            group = dict(row)
            group["group_index"] = len(groups) + 1
            group["repeat_count"] = 0
            group["phase_indices"] = []
            groups.append(group)
            by_signature[signature] = group
        group["repeat_count"] += 1
        group["phase_indices"].append(_panel_int(row.get("phase_index"), len(group["phase_indices"]) + 1))
    return groups


def _attack_property_phase_count(attack: dict) -> int:
    return len(_attack_property_phase_rows(attack))


def _attack_projectile_rows(attack: dict) -> list[dict]:
    """Compatibility accessor for spawned attack-actor rows."""
    attack = attack if isinstance(attack, dict) else {}
    source_rows = attack.get("attack_property_actors") or attack.get("attack_property_projectiles") or []
    return [dict(row) for row in source_rows if isinstance(row, dict)]


def _attack_actor_groups(attack: dict) -> list[dict]:
    """Group simultaneous/recycled actors by native definition for readability."""
    groups: list[dict] = []
    by_signature: dict[tuple, dict] = {}
    for row in _attack_projectile_rows(attack):
        signature = (
            str(row.get("attack_actor_name") or row.get("projectile_action_name") or ""),
            _panel_int(row.get("property_a"), 0),
            _panel_int(row.get("property_b"), 0),
            _panel_int(row.get("phase_property_a"), 0),
            _panel_int(row.get("phase_property_b"), 0),
            bool(row.get("attack_actor_live", row.get("projectile_live"))),
            bool(row.get("cleanup_observed")),
            bool(row.get("inactive_generic_actor")),
        )
        group = by_signature.get(signature)
        if group is None:
            group = dict(row)
            group["actor_count"] = 0
            group["actor_indices"] = []
            group["allocation_epochs"] = []
            groups.append(group)
            by_signature[signature] = group
        group["actor_count"] += 1
        group["actor_indices"].append(_panel_int(row.get("attack_actor_index", row.get("projectile_index")), 0))
        epoch = _panel_int(row.get("allocation_epoch"), 0)
        if epoch and epoch not in group["allocation_epochs"]:
            group["allocation_epochs"].append(epoch)
    return groups


def _attack_projectile_primary_tokens(row: dict) -> list[tuple[str, tuple[int, int, int]]]:
    index = max(1, _panel_int(row.get("attack_actor_index", row.get("projectile_index")), 1))
    projectile_id = _panel_int(row.get("attack_actor_id", row.get("projectile_id")), 0)
    actor_count = max(1, _panel_int(row.get("actor_count"), 1))
    actor_label = f"ACTORS ×{actor_count}" if actor_count > 1 else f"ACTOR {index}"
    tokens = [(actor_label, (127, 205, 255))]
    if projectile_id:
        tokens.append((f"ID {projectile_id:04X}", (166, 181, 204)))
    prop_a = _panel_int(row.get("property_a"), 0)
    guard, strength = _attack_property_a_ui(prop_a)
    actor_name = str(row.get("attack_actor_name") or row.get("projectile_action_name") or "").upper()
    prop_b = _panel_int(row.get("property_b"), 0)
    capture_actor = bool(
        (prop_a & 0x00300000)
        or (prop_b & 0x00400000)
        or any(word in actor_name for word in ("GRAB", "THROW", "CLUTCH", "CAPTURE"))
    )
    if guard == "UNBLOCKABLE":
        label = "UNBLOCKABLE CAPTURE" if capture_actor else "GUARD DECODE UNVERIFIED"
        tokens.append((label, (255, 174, 115)))
    else:
        tokens.append((guard, (109, 209, 205)))
    tokens.append((strength, (185, 139, 238)))
    high_flags = prop_a & ~0x3F
    if high_flags == 0x00200000:
        tokens.append(("CAPTURE TRIGGER / OPPONENT LOCK", (255, 151, 105)))
    elif high_flags == 0x00100000:
        tokens.append(("VICTIM STABILIZER / CARRIED-REACTION MODIFIER", (255, 174, 115)))
    elif high_flags:
        result_label = _attack_reaction_label(None, high_flags)
        if not result_label.startswith("A RESULT FLAGS"):
            tokens.append((result_label, (255, 174, 115)))
        else:
            tokens.append((f"A FLAGS 0x{high_flags:08X}", (255, 174, 115)))
    epoch = _panel_int(row.get("allocation_epoch"), 0)
    if epoch:
        tokens.append((f"LIFETIME E{epoch}", (144, 155, 174)))
    if bool(row.get("inactive_generic_actor")):
        tokens.append(("UNRESOLVED INACTIVE ACTOR", (255, 112, 120)))
    if bool(row.get("attack_actor_live", row.get("projectile_live"))):
        tokens.append(("LIVE SPAWNED ACTOR", (102, 224, 164)))
    else:
        age = max(0, _panel_int(row.get("age_frames"), 0))
        label = f"LAST SPAWNED ACTOR {age}F" if age else "LAST SPAWNED ACTOR"
        tokens.append((label, (236, 188, 92)))
    return tokens


def _attack_projectile_secondary_tokens(
    row: dict,
    *,
    include_raw: bool = False,
) -> list[tuple[str, tuple[int, int, int]]]:
    prop_b = _panel_int(row.get("property_b"), 0)
    tokens = list(_attack_property_b_ui(prop_b, source_kind="actor"))
    phase_a = _panel_int(row.get("phase_property_a"), 0)
    phase_b = _panel_int(row.get("phase_property_b"), 0)
    if phase_a or phase_b:
        tokens.append(("QUEUED / DEFERRED DEFINITION", (109, 209, 205)))
        if phase_a:
            guard, strength = _attack_property_a_ui(phase_a)
            if guard == "UNBLOCKABLE":
                tokens.append(("QUEUED GUARD DECODE UNVERIFIED", (255, 174, 115)))
            else:
                tokens.append((f"QUEUED {guard}", (109, 209, 205)))
            tokens.append((f"QUEUED {strength}", (185, 139, 238)))
        if phase_b:
            for text, color in _attack_property_b_ui(phase_b, source_kind="actor"):
                tokens.append((f"QUEUED {text}", color))
    if include_raw:
        actor = _panel_int(row.get("actor"), 0)
        linked = _panel_int(row.get("linked"), 0)
        tokens.append((f"A 0x{_panel_int(row.get('property_a'), 0) & 0xFFFFFFFF:08X}", (166, 181, 204)))
        tokens.append((f"B 0x{prop_b & 0xFFFFFFFF:08X}", (166, 181, 204)))
        if actor:
            tokens.append((f"ACTOR 0x{actor:08X}", (144, 155, 174)))
        if linked:
            tokens.append((f"LINK 0x{linked:08X}", (144, 155, 174)))
        layout = str(row.get("property_layout") or "").strip()
        registry = str(row.get("registry_source") or "").strip()
        if layout:
            tokens.append((f"LAYOUT {layout}", (144, 155, 174)))
        if registry:
            tokens.append((f"SOURCE {registry.upper()}", (144, 155, 174)))
    return tokens


def _attack_phase_primary_tokens(row: dict) -> list[tuple[str, tuple[int, int, int]]]:
    tokens: list[tuple[str, tuple[int, int, int]]] = []
    phase_index = max(1, _panel_int(row.get("phase_index"), 1))
    group_index = max(0, _panel_int(row.get("group_index"), 0))
    repeat_count = max(1, _panel_int(row.get("repeat_count"), 1))
    label = f"TYPE {group_index}" if group_index else f"P{phase_index}"
    if repeat_count > 1:
        label += f" ×{repeat_count}"
    tokens.append((label, (127, 205, 255)))

    prop_a = _panel_int(row.get("property_a"), 0)
    guard, strength = _attack_property_a_ui(prop_a)
    tokens.append((guard, (109, 209, 205) if guard != "UNBLOCKABLE" else (255, 112, 120)))
    tokens.append((strength, (185, 139, 238)))

    initial_unknown = _attack_optional_int(row.get("property_a_initial_unknown_mask"))
    if initial_unknown not in (None, 0):
        initial_unknown &= 0xFFFFFFFF
        if initial_unknown == 0x00200000:
            tokens.append(("CAPTURE TRIGGER / OPPONENT LOCK 0x00200000", (255, 174, 115)))
        else:
            tokens.append((f"A INIT FLAGS 0x{initial_unknown:08X}", (255, 174, 115)))
    post_flags = _panel_int(row.get("property_a_post_result_or_mask"), 0) & 0xFFFFFFFF
    if post_flags:
        if post_flags == 0x08000000:
            tokens.append(("A POST 0x08000000 · COMMON ATTACK FLAG", (166, 181, 204)))
        elif post_flags == 0x18000000:
            tokens.append(("A POST 0x18000000 · HIGH/AIR FAMILY (CORRELATED)", (255, 174, 115)))
        else:
            tokens.append((f"A POST FLAGS 0x{post_flags:08X}", (255, 174, 115)))

    operation_count = _attack_optional_int(row.get("operation_count"))
    if operation_count:
        tokens.append((f"{operation_count} NATIVE OPS", (166, 181, 204)))
    return tokens

_NATIVE_A_RESULT_RAW_LABELS = {
    0x00000000: "NORMAL HIT RESULT",
    0x00000100: "LAUNCH / SOFT-KD ROUTE",
    0x00000200: "HARD KNOCKDOWN",
    0x00000300: "SPIRAL KNOCKDOWN",
    0x00000400: "SWEEP",
    0x00000800: "STAGGER",
    0x00001000: "CAPTURE / THROW CONNECTION",
    0x00001800: "CAPTURE + STAGGER",
    0x00004000: "OTG ENABLED",
    0x00004100: "OTG + LAUNCH / SOFT-KD",
    0x00004200: "OTG + HARD KNOCKDOWN",
    0x00008000: "WALL BOUNCE (POWERED ROLL SWING OBSERVED)",
    0x00008200: "HARD KNOCKDOWN (ROLL SWING FAMILY)",
    0x00008300: "EXACT REACTION 0x00008300 UNRESOLVED",
    0x00008800: "EXACT REACTION 0x00008800 UNRESOLVED",
    0x00010000: "AIRBORNE SOFT-KNOCKDOWN OVERRIDE (PROVISIONAL)",
    0x00010100: "CONDITIONAL REACTION COMPOSITE (UNRESOLVED)",
    0x00020000: "SPECIAL REACTION COMPONENT (UNRESOLVED)",
    0x00024000: "SPECIAL REACTION COMPONENT + OTG",
    0x00040000: "LAUNCHER",
    0x00080000: "AIR KNOCKDOWN",
    0x00100100: "STABILIZED SOFT KNOCKDOWN",
    0x00110000: "REPEATED-JUGGLE ROUTE (CORRELATED)",
    0x00201000: "CINEMATIC IMPACT / TRANSITION",
    0x00420000: "MEGACRASH BLOWBACK",
    0x00300100: "CINEMATIC + LAUNCH",
    0x00301000: "CINEMATIC + CAPTURE",
    0x00301100: "CINEMATIC + CAPTURE + LAUNCH",
    0x80000080: "CRUMPLE",
    0x80000200: "WALL-INTERACTION HARD KNOCKDOWN",
    0x80008200: "POWERED/CHARGED WALL-BOUNCE REACTION",
    0x80000800: "STAGGER + FORCED TURNAROUND",
    0x80004200: "OTG + HARD KNOCKDOWN + HIGH MODIFIER UNRESOLVED",
    0x80080000: "SPECIAL AIR-KNOCKDOWN COMPOSITE",
}

# Shifted code fallback for captures whose exact raw word is not in the ledger.
_NATIVE_ATTACK_REACTION_LABELS = {
    0x00000000: "NORMAL HIT RESULT",
    0x00000001: "LAUNCH / SOFT-KD ROUTE",
    0x00000002: "HARD KNOCKDOWN",
    0x00000003: "SPIRAL KNOCKDOWN",
    0x00000004: "SWEEP",
    0x00000008: "STAGGER",
    0x00000010: "CAPTURE / THROW CONNECTION",
    0x00000018: "CAPTURE + STAGGER",
    0x00000040: "OTG ENABLED",
    0x00000041: "OTG + LAUNCH / SOFT-KD",
    0x00000042: "OTG + HARD KNOCKDOWN",
    0x00000080: "WALL BOUNCE (POWERED ROLL SWING OBSERVED)",
    0x00000082: "HARD KNOCKDOWN (ROLL SWING FAMILY)",
    0x00000083: "EXACT REACTION 0x00008300 UNRESOLVED",
    0x00000088: "EXACT REACTION 0x00008800 UNRESOLVED",
    0x00000100: "AIRBORNE SOFT-KNOCKDOWN OVERRIDE (PROVISIONAL)",
    0x00000200: "SPECIAL REACTION COMPONENT (UNRESOLVED)",
    0x00000240: "SPECIAL REACTION COMPONENT + OTG",
    0x00000400: "LAUNCHER",
    0x00000800: "AIR KNOCKDOWN",
    0x00001001: "STABILIZED SOFT KNOCKDOWN",
    0x00002010: "CINEMATIC IMPACT / TRANSITION",
    0x00004200: "MEGACRASH BLOWBACK",
    0x00800002: "WALL-INTERACTION HARD KNOCKDOWN",
    0x00800082: "POWERED/CHARGED WALL-BOUNCE REACTION",
    0x00800008: "STAGGER + FORCED TURNAROUND",
    0x00800042: "OTG + HARD KNOCKDOWN + HIGH MODIFIER UNRESOLVED",
    0x00800800: "SPECIAL AIR-KNOCKDOWN COMPOSITE",
    0x00001100: "REPEATED-JUGGLE ROUTE (CORRELATED)",
    0x00003001: "CINEMATIC + LAUNCH",
    0x00003010: "CINEMATIC + CAPTURE",
    0x00003011: "CINEMATIC + CAPTURE + LAUNCH",
    0x00000101: "CONDITIONAL REACTION COMPOSITE (UNRESOLVED)",
}


def _attack_reaction_label(value: int | None, raw_value: int | None = None) -> str:
    """Prefer exact Property A result words over their shifted code.

    Exact raw matching preserves low-byte behavior such as 0x80000080 Crumple,
    which cannot be reconstructed from the historical value>>8 code alone.
    """
    raw = _attack_optional_int(raw_value)
    if raw is not None:
        raw &= 0xFFFFFFFF
        known_raw = _NATIVE_A_RESULT_RAW_LABELS.get(raw)
        if known_raw:
            return known_raw
    reaction = _attack_optional_int(value)
    if reaction is None:
        return ""
    known = _NATIVE_ATTACK_REACTION_LABELS.get(reaction)
    if known:
        return known
    if raw is not None:
        return f"A RESULT FLAGS 0x{raw:08X}"
    return f"A RESULT CODE 0x{reaction & 0xFFFFFFFF:08X}"


def _attack_phase_secondary_tokens(
    row: dict,
    *,
    include_raw: bool = False,
) -> list[tuple[str, tuple[int, int, int]]]:
    """Display only values harvested from the native Property A/B script."""
    tokens = list(_attack_property_b_ui(_panel_int(row.get("property_b"), 0)))

    reaction = _attack_optional_int(row.get("a_result_code", row.get("hit_reaction")))
    result_raw = _attack_optional_int(row.get("a_result_flags_raw", row.get("hit_result_raw")))
    if reaction is not None:
        reaction_text = _attack_reaction_label(reaction, result_raw)
        if include_raw:
            reaction_text += f" [A FLAGS 0x{(result_raw or 0) & 0xFFFFFFFF:08X}]"
        tokens.append((reaction_text, (224, 151, 169)))
    elif _attack_optional_int(row.get("result_clear_mask")):
        tokens.append(("A RESULT FLAGS CLEARED, NO OR VALUE", (255, 112, 120)))

    if include_raw:
        initial_a = _attack_optional_int(row.get("property_a_initial"))
        initial_b = _attack_optional_int(row.get("property_b_initial"))
        final_b = _attack_optional_int(row.get("property_b"))
        if initial_a is not None:
            tokens.append((f"A SET 0x{initial_a & 0xFFFFFFFF:08X}", (166, 181, 204)))
        final_a = _attack_optional_int(row.get("property_a_final"))
        post_a = _attack_optional_int(row.get("property_a_post_result_or_mask"))
        if final_a is not None:
            tokens.append((f"A FINAL 0x{final_a & 0xFFFFFFFF:08X}", (166, 181, 204)))
        if post_a:
            tokens.append((f"A POST OR 0x{post_a & 0xFFFFFFFF:08X}", (190, 164, 236)))
        if initial_b is not None:
            tokens.append((f"B SET 0x{initial_b & 0xFFFFFFFF:08X}", (166, 181, 204)))
        if final_b is not None and initial_b is not None and final_b != initial_b:
            tokens.append((f"B FINAL 0x{final_b & 0xFFFFFFFF:08X}", (190, 164, 236)))
    return tokens

def _attack_move_context_tokens(attack: dict) -> list[tuple[str, tuple[int, int, int]]]:
    """Attack Property intentionally excludes frame-data/profile context."""
    del attack
    return []

def _attack_native_operation_text(row: dict) -> str:
    operations = [
        operation for operation in (row.get("native_operations") or row.get("operations") or [])
        if isinstance(operation, dict)
    ]
    parts = []
    for operation in operations:
        name = str(operation.get("operation_name") or "").upper()
        if not name:
            op = _panel_int(operation.get("operation"), -1)
            name = {0x01: "SET", 0x15: "OR", 0x17: "CLEAR"}.get(op, f"OP{op:02X}")
        field = str(operation.get("field_name") or "").upper()
        if not field:
            field = "A" if _panel_int(operation.get("field_id"), 0) == 0x240 else "B"
        value = _panel_int(operation.get("value"), 0) & 0xFFFFFFFF
        parts.append(f"{name} {field} {value:08X}")
    return "  >  ".join(parts)


def _compact_research_tokens(
    mode: str,
    team: str,
    point_label: str,
    point: dict,
    partner_label: str,
    partner: dict,
) -> tuple[str, list[tuple[str, tuple[int, int, int]]]]:
    accent = (236, 92, 108) if team == "P1" else (82, 164, 236)
    tokens: list[tuple[str, tuple[int, int, int]]] = []

    if mode == "damage":
        opponent_team = "P2" if team == "P1" else "P1"
        victim = _damage_point_slot(opponent_team)
        if victim is None:
            return "DMG MOD", [("NO LIVE OPPONENT", (126, 139, 158))]
        victim_slot, _victim_snapshot = victim
        owner_slot = _damage_team_owner_slot(team, point_label)
        data = build_live_damage_modifier(
            _display_slots,
            point_label,
            victim_slot,
            owner_slot=owner_slot,
        )
        percent = float(data.get("percent") or 100.0)
        suffix = "*" if bool(data.get("approximate", False)) else ""
        tokens.append((f"MOD {percent:.1f}%{suffix}", accent))
        factors = [str(item or "").strip() for item in (data.get("factors") or ["BASE"])]
        for factor in factors:
            if factor:
                tokens.append((factor.upper(), (174, 196, 224)))
        return "DMG MOD", tokens

    if mode == "meter":
        current = max(0, min(50000, _panel_int(point.get("meter_profile_current", point.get("meter")), 0)))
        delta = _panel_int(point.get("meter_profile_last_delta"), 0)
        predicted_raw = point.get("meter_profile_last_predicted")
        predicted = None if predicted_raw is None else _panel_int(predicted_raw)
        role = str(point.get("meter_profile_last_role") or "").upper()
        match = point.get("meter_profile_last_match")
        source = str(point.get("meter_profile_last_source") or "").strip()
        move = str(point.get("meter_profile_last_move") or "").strip()
        tokens.append((f"CUR {current / 10000.0:.2f} BAR", accent))
        if delta:
            delta_color = (92, 218, 154) if delta > 0 else (255, 166, 92)
            tokens.append((f"LAST {delta:+d} {role or 'UNKNOWN'}", delta_color))
        else:
            tokens.append(("NO TRANSITION", (126, 139, 158)))
        if predicted is not None:
            state = "MATCH" if match is True else ("MISS" if match is False else "PRED")
            state_color = (92, 218, 154) if match is True else ((255, 112, 120) if match is False else (164, 182, 210))
            tokens.append((f"PRED {predicted:+d} {state}", state_color))
        source_line = " ".join(part for part in (source, move) if part)
        if source_line:
            tokens.append((source_line.upper(), (166, 181, 204)))
        return "METER+", tokens

    if mode == "red":
        for slot_label, snap in ((point_label, point), (partner_label, partner)):
            _current, auxiliary, red, red_pct, pending_current, pending_aux, event = _compact_red_health_values(snap)
            badge = "C1" if slot_label.endswith("C1") else "C2"
            slot_accent = SLOT_COLORS.get(slot_label, accent)
            tokens.append((f"{badge} AUX {_compact_short_number(auxiliary)} RED {_compact_short_number(red)} {red_pct:.1f}%", slot_accent))
            tokens.append((f"{badge} Q {pending_current:+d}/{pending_aux:+d}", (180, 194, 214)))
            if event:
                tokens.append((f"{badge} {event}", (224, 151, 169)))
        return "RED HP", tokens

    if mode == "attack":
        attack_slot, attack = _active_attack_snapshot(team)
        attack = attack if isinstance(attack, dict) else {}
        actor = _panel_int(attack.get("attack_property_live_actor"), 0)
        display_active = bool(attack.get("attack_property_display_active")) or bool(actor)
        if not display_active:
            status = str(attack.get("attack_property_definition_status") or "WAITING").upper()
            error = str(attack.get("attack_property_definition_error") or "").strip()
            action_id = _panel_int(
                attack.get("attack_property_definition_action_id")
                or attack.get("mv_id_display")
                or attack.get("attA"),
                0,
            )
            tokens = [("NO PROPERTY FOR CURRENT ACTION", (126, 139, 158))]
            detail = status.replace("_", " ")
            if action_id:
                detail += f"  ACTION {action_id:04X}"
            tokens.append((detail, (255, 112, 120) if error else (166, 181, 204)))
            if error:
                tokens.append((_compact_trim(error.upper(), 34), (255, 112, 120)))
            return "ATK PROP", tokens

        damage = _panel_int(attack.get("attack_property_live_damage"), 0)
        victim = str(attack.get("attack_property_live_victim_slot") or "").strip().upper()
        prop_a = _panel_int(attack.get("attack_property_live_a"), 0)
        prop_b = _panel_int(attack.get("attack_property_live_b"), 0)
        source = str(attack.get("attack_property_display_source") or attack.get("attack_property_packet_source") or "")
        guard, strength = _attack_property_a_ui(prop_a)

        tokens.append((_attack_source_ui(attack_slot, attack), accent))
        tokens.append((guard, (109, 209, 205) if guard != "UNBLOCKABLE" else (255, 112, 120)))
        tokens.append((strength, (185, 139, 238)))
        if source in {"move_definition", "move_definition_latched"}:
            state_text = "CURRENT NATIVE SCRIPT" if source == "move_definition" else "LAST NATIVE SCRIPT"
            tokens.append((state_text, (127, 205, 255)))
            action_id = _panel_int(attack.get("attack_property_packet_action_id"), 0)
            if action_id:
                tokens.append((f"ACTION {action_id:04X}", (166, 181, 204)))
            phases = _attack_property_phase_rows(attack)
            phase_groups = _attack_property_phase_groups(phases)
            block_text = f"{len(phases)} SCRIPT BLOCK{'S' if len(phases) != 1 else ''}"
            if len(phase_groups) != len(phases):
                block_text += f" · {len(phase_groups)} UNIQUE"
            tokens.append((block_text, (166, 181, 204)))
            for phase in phase_groups[:2]:
                tokens.extend(_attack_phase_primary_tokens(phase))
                tokens.extend(_attack_phase_secondary_tokens(phase))
            if len(phase_groups) > 2:
                tokens.append((f"+{len(phase_groups) - 2} MORE TYPES", (166, 181, 204)))
            return "ATK PROP", tokens

        damage_text = f"BASE DMG {damage}"
        if victim and victim != "-":
            damage_text += f" TO {victim}"
        tokens.append((damage_text, (255, 124, 132)))
        tokens.append((_attack_scaling_ui(attack), (236, 188, 92)))
        tokens.extend(_attack_property_b_ui(prop_b))

        phase_a = _panel_int(attack.get("attack_property_live_phase_a"), 0)
        phase_b = _panel_int(attack.get("attack_property_live_phase_b"), 0)
        if phase_a or phase_b:
            next_guard, next_strength = _attack_property_a_ui(phase_a)
            tokens.append(("NEXT PHASE", (109, 209, 205)))
            if phase_a:
                tokens.append((f"NEXT {next_guard}", (109, 209, 205)))
                tokens.append((f"NEXT {next_strength}", (185, 139, 238)))
            for label, color in _attack_property_b_ui(phase_b):
                tokens.append((f"NEXT {label}", color))
        return "ATK PROP", tokens

    return "DATA", []


def _draw_compact_research_row(
    screen,
    font_sm,
    mode: str,
    team: str,
    point_label: str,
    point: dict,
    partner_label: str,
    partner: dict,
    x: int,
    y: int,
    right: int,
    row_height: int,
    scale: float,
) -> None:
    title, tokens = _compact_research_tokens(
        mode,
        team,
        point_label,
        point,
        partner_label,
        partner,
    )
    label_w = _draw_history_header_chip(screen, font_sm, title, x, y, scale)
    gap_x = max(4, int(5 * scale))
    gap_y = max(2, int(2 * scale))
    draw_left = x + label_w + max(6, int(7 * scale))
    line_height = max(font_sm.get_height() + max(4, int(5 * scale)), int(15 * scale))
    line_y = y
    line_index = 0
    draw_x = draw_left
    clip = pygame.Rect(draw_left, y - 1, max(1, right - draw_left), max(1, row_height + 2))
    old_clip = screen.get_clip()
    screen.set_clip(clip)

    for raw_text, color in tokens:
        available = max(1, right - draw_left - max(10, int(12 * scale)))
        chip_text = _compact_fit_text(font_sm, str(raw_text or ""), available)
        chip = _render_compact_text_chip(font_sm, chip_text, color, scale, 1.0)
        if draw_x + chip.get_width() > right and line_index == 0:
            line_index = 1
            line_y = y + line_height + gap_y
            draw_x = draw_left
        if draw_x + chip.get_width() > right:
            remaining = right - draw_x - max(2, int(3 * scale))
            if remaining <= max(28, int(32 * scale)):
                continue
            fitted = _compact_fit_text(font_sm, str(raw_text or ""), remaining - max(10, int(12 * scale)))
            chip = _render_compact_text_chip(font_sm, fitted, color, scale, 1.0)
        screen.blit(chip, (draw_x, line_y))
        draw_x += chip.get_width() + gap_x

    screen.set_clip(old_clip)


def _compact_attack_badge_height(team: str, font_sm, scale: float) -> int:
    _slot, attack = _active_attack_snapshot(team)
    attack = attack if isinstance(attack, dict) else {}
    phase_rows = _attack_property_phase_rows(attack)
    phase_groups = _attack_property_phase_groups(phase_rows)
    phase_count = len(phase_rows)
    projectile_count = len(_attack_actor_groups(attack))
    visible_phases = min(3, len(phase_groups))
    visible_projectiles = min(2, projectile_count)
    header_h = max(font_sm.get_height() + 8, int(21 * scale))
    line_h = max(font_sm.get_height() + 5, int(18 * scale))
    body_h = visible_phases * line_h * 2
    if phase_count > visible_phases:
        body_h += line_h
    if projectile_count:
        body_h += line_h  # SPAWNED ATTACK ACTORS section label
        body_h += visible_projectiles * line_h * 2
        if projectile_count > visible_projectiles:
            body_h += line_h
    if not phase_count and not projectile_count:
        body_h = line_h * 2
    return header_h + body_h + max(7, int(8 * scale))


def _draw_attack_property_badge(
    screen,
    font_sm,
    team: str,
    x: int,
    y: int,
    right: int,
    height: int,
    scale: float,
) -> None:
    """Draw fighter script blocks and persistent spawned attack actors."""
    slot, attack = _active_attack_snapshot(team)
    attack = attack if isinstance(attack, dict) else {}
    accent = (236, 92, 108) if team == "P1" else (82, 164, 236)
    width = max(1, right - x)
    rect = pygame.Rect(x, y, width, max(1, height))

    shell = pygame.Surface(rect.size, pygame.SRCALPHA)
    pygame.draw.rect(shell, (11, 18, 29, 218), shell.get_rect(), border_radius=max(5, int(6 * scale)))
    pygame.draw.rect(shell, (*accent, 118), shell.get_rect(), 1, border_radius=max(5, int(6 * scale)))
    title_bar = pygame.Rect(0, 0, shell.get_width(), max(font_sm.get_height() + 8, int(21 * scale)))
    pygame.draw.rect(shell, (*accent, 32), title_bar, border_radius=max(5, int(6 * scale)))
    pygame.draw.line(shell, (*accent, 88), (0, title_bar.bottom), (shell.get_width(), title_bar.bottom), 1)
    screen.blit(shell, rect.topleft)

    pad = max(6, int(7 * scale))
    title = font_sm.render("ATK PROPERTY", True, accent)
    screen.blit(title, (rect.x + pad, rect.y + max(2, int(3 * scale))))

    move = str(
        attack.get("attack_property_packet_action_name")
        or attack.get("final_move_label")
        or attack.get("mv_label_display")
        or attack.get("mv_label")
        or "---"
    ).strip().upper()
    phases = _attack_property_phase_rows(attack)
    phase_groups = _attack_property_phase_groups(phases)
    projectiles = _attack_actor_groups(attack)
    action_id = _panel_int(
        attack.get("attack_property_packet_action_id")
        or attack.get("attack_property_definition_action_id")
        or attack.get("mv_id_display")
        or attack.get("attA"),
        0,
    )
    summary_parts = [move]
    if phases:
        summary_parts.append("NATIVE SCRIPT")
    if projectiles:
        live_count = sum(
            max(1, _panel_int(row.get("actor_count"), 1))
            for row in projectiles
            if bool(row.get("attack_actor_live", row.get("projectile_live")))
        )
        total_actor_count = sum(max(1, _panel_int(row.get("actor_count"), 1)) for row in projectiles)
        if live_count:
            summary_parts.append(f"{live_count} LIVE ATTACK ACTOR{'S' if live_count != 1 else ''}")
        else:
            summary_parts.append(f"{total_actor_count} LAST ATTACK ACTOR{'S' if total_actor_count != 1 else ''}")
    if action_id:
        summary_parts.append(f"ACT {action_id:04X}")
    if phases:
        block_text = f"{len(phases)} SCRIPT BLOCK{'S' if len(phases) != 1 else ''}"
        if len(phase_groups) != len(phases):
            block_text += f" · {len(phase_groups)} UNIQUE"
        summary_parts.append(block_text)
    summary = _panel_fit(font_sm, "  |  ".join(summary_parts), max(1, rect.width - title.get_width() - pad * 3))
    summary.set_alpha(220)
    screen.blit(summary, (rect.right - pad - summary.get_width(), rect.y + max(2, int(3 * scale))))

    if not phases and not projectiles:
        status = str(attack.get("attack_property_definition_status") or "WAITING").upper().replace("_", " ")
        message = _panel_fit(font_sm, f"NO NATIVE PROPERTY FOR CURRENT ACTION  {status}", rect.width - pad * 2)
        message.set_alpha(180)
        screen.blit(message, (rect.x + pad, rect.y + title_bar.height + max(7, int(8 * scale))))
        return

    line_y = rect.y + title_bar.height + max(4, int(5 * scale))
    line_h = max(font_sm.get_height() + 5, int(18 * scale))

    def draw_token_line(tokens, draw_y: int) -> None:
        draw_x = rect.x + pad
        max_x = rect.right - pad
        for text, color in tokens:
            remaining = max_x - draw_x
            if remaining <= max(22, int(26 * scale)):
                break
            fitted = _compact_fit_text(font_sm, str(text or ""), max(1, remaining - max(8, int(10 * scale))))
            chip = _render_compact_text_chip(font_sm, fitted, color, scale, 1.0)
            if draw_x + chip.get_width() > max_x:
                break
            screen.blit(chip, (draw_x, draw_y))
            draw_x += chip.get_width() + max(3, int(4 * scale))

    visible_phases = phase_groups[:3]
    for row in visible_phases:
        draw_token_line(_attack_phase_primary_tokens(row), line_y)
        line_y += line_h
        secondary = _attack_phase_secondary_tokens(row)
        if secondary:
            draw_token_line(secondary, line_y)
        else:
            quiet = font_sm.render("NO EXTRA NATIVE FLAGS", True, (126, 139, 158))
            quiet.set_alpha(175)
            screen.blit(quiet, (rect.x + pad, line_y + 1))
        line_y += line_h
    if len(phase_groups) > len(visible_phases):
        more = font_sm.render(f"+{len(phase_groups) - len(visible_phases)} MORE UNIQUE BLOCK TYPES IN RESEARCH PANEL", True, (166, 181, 204))
        more.set_alpha(190)
        screen.blit(more, (rect.x + pad, line_y))
        line_y += line_h

    if projectiles:
        any_live_projectile = any(bool(row.get("attack_actor_live", row.get("projectile_live"))) for row in projectiles)
        section_text = "SPAWNED ATTACK ACTORS" if any_live_projectile else "LAST SPAWNED ATTACK ACTORS"
        section_color = (102, 224, 164) if any_live_projectile else (236, 188, 92)
        section = font_sm.render(section_text, True, section_color)
        section.set_alpha(225)
        screen.blit(section, (rect.x + pad, line_y + 1))
        line_y += line_h
        visible_projectiles = projectiles[:2]
        for row in visible_projectiles:
            draw_token_line(_attack_projectile_primary_tokens(row), line_y)
            line_y += line_h
            draw_token_line(_attack_projectile_secondary_tokens(row), line_y)
            line_y += line_h
        if len(projectiles) > len(visible_projectiles):
            more = font_sm.render(f"+{len(projectiles) - len(visible_projectiles)} MORE ACTOR DEFINITIONS IN RESEARCH PANEL", True, (166, 181, 204))
            more.set_alpha(190)
            screen.blit(more, (rect.x + pad, min(rect.bottom - more.get_height() - 2, line_y)))


def _draw_compact_history_line(screen, font_sm, title: str, items: list[dict], x: int, y: int, right: int, scale: float, prev_items: list[dict] | None = None, slide_progress: float = 0.0) -> None:
    label_w = _draw_history_header_chip(screen, font_sm, title, x, y, scale)
    draw_x = x + label_w + max(6, int(7 * scale))
    gap = max(6, int(7 * scale))
    clip_rect = pygame.Rect(draw_x, y - 1, max(1, right - draw_x), max(font_sm.get_height() + int(8 * scale), int(18 * scale)))

    def _norm(source) -> list[dict]:
        out = []
        for item in source or []:
            if not item:
                continue
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            if not label and not value:
                continue
            out.append({
                "label": label,
                "value": value,
                "color": item.get("color") or (196, 205, 220),
                "life": float(item.get("life", 1.0)),
                "rainbow": bool(item.get("rainbow", False) or label.upper() == "BBQ"),
            })
            if len(out) >= 4:
                break
        return out

    def _render_parts(source_items, alpha_override=None):
        rendered = []
        for idx, item in enumerate(source_items):
            alpha = max(0.35, min(1.0, alpha_override if alpha_override is not None else item.get("life", 1.0)))
            primary = f"{item.get('label','')} {item.get('value','')}".strip()
            color = item.get("color") or (196, 205, 220)
            surf = _render_compact_text_chip(font_sm, primary, color, scale, alpha, rainbow=item.get("rainbow", False), emphasis=1.04 if idx == 0 else 1.0)
            rendered.append(surf)
        return rendered

    current_items = _norm(items)
    previous_items = _norm(prev_items)
    if not current_items and not previous_items:
        empty = font_sm.render(" - ", True, (86, 96, 114))
        screen.blit(empty, (draw_x, y))
        return

    current_parts = _render_parts(current_items, 1.0)
    previous_parts = _render_parts(previous_items, max(0.0, min(1.0, slide_progress))) if previous_items else []
    inserted_shift = max(int(30 * scale), current_parts[0].get_width() + gap if current_parts else int(30 * scale))

    def _draw_parts(parts, base_x):
        dx = base_x
        for idx, surf in enumerate(parts):
            if dx > right:
                break
            screen.blit(surf, (dx, y))
            dx += surf.get_width()
            if idx < len(parts) - 1:
                dx += gap

    old_clip = screen.get_clip()
    screen.set_clip(clip_rect)
    if slide_progress > 0.001 and previous_parts:
        _draw_parts(previous_parts, draw_x + int(inserted_shift * (1.0 - slide_progress)))
        _draw_parts(current_parts, draw_x - int(inserted_shift * slide_progress))
    else:
        _draw_parts(current_parts, draw_x)
    screen.set_clip(old_clip)




HOLD_LOG_LIFETIME_FRAMES = 80
HOLD_LOG_MIN_FRAMES = 26

_BUTTON_HOLD_SPECS = (
    ("A", 0x0080),
    ("B", 0x0040),
    ("C", 0x0020),
    ("P", 0x0010),
    ("T", 0x0C00),
)

_DIRECTION_HOLD_SPECS = {
    0x1: "6",
    0x2: "4",
    0x4: "8",
    0x5: "9",
    0x6: "7",
    0x8: "2",
    0x9: "3",
    0xA: "1",
}

_BUTTON_HOLD_COLORS = {
    "A": (92, 232, 146),
    "B": (132, 204, 255),
    "C": (255, 148, 112),
    "P": (190, 132, 255),
    "T": (242, 205, 92),
    "1": (134, 190, 255),
    "2": (112, 176, 255),
    "3": (134, 190, 255),
    "4": (112, 176, 255),
    "6": (112, 176, 255),
    "7": (134, 190, 255),
    "8": (112, 176, 255),
    "9": (134, 190, 255),
}


def _button_mask_is_held(input_word: int, mask: int) -> bool:
    word = int(input_word) & 0xFFFF
    if mask == INPUT_TAUNT_MASK:
        return (word & mask) == mask
    return bool(word & mask)


def _qualified_button_hold_mask(slot_anim: dict, current_frame: int) -> int:
    active = slot_anim.get("button_hold_active", {})
    qualified = 0
    for label, mask in _BUTTON_HOLD_SPECS:
        hold = active.get(label)
        if not isinstance(hold, dict):
            continue
        start_frame = int(hold.get("start_frame", current_frame))
        held_frames = max(0, int(current_frame) - start_frame)
        if held_frames >= HOLD_LOG_MIN_FRAMES:
            qualified |= int(mask)
    return qualified & INPUT_BUTTON_MASK


def _update_button_hold_log(slot_anim: dict, input_held: int, frame_number: int) -> None:
    active = slot_anim.setdefault("button_hold_active", {})
    events = slot_anim.setdefault("button_hold_events", [])

    def _begin_hold(label: str, kind: str) -> None:
        if label in active:
            return
        slot_anim["button_hold_seq"] = int(slot_anim.get("button_hold_seq", 0)) + 1
        active[label] = {
            "start_frame": int(frame_number),
            "seq": int(slot_anim["button_hold_seq"]),
            "kind": kind,
        }

    def _end_hold(label: str) -> None:
        if label not in active:
            return
        hold = active.pop(label)
        start_frame = int(hold.get("start_frame", frame_number))
        held_frames = max(0, int(frame_number) - start_frame)
        if held_frames >= HOLD_LOG_MIN_FRAMES:
            events.insert(0, {
                "label": label,
                "start_frame": start_frame,
                "end_frame": int(frame_number),
                "frames": held_frames,
                "seq": int(hold.get("seq", 0)),
                "kind": str(hold.get("kind") or "button"),
            })

    for label, mask in _BUTTON_HOLD_SPECS:
        held_now = _button_mask_is_held(input_held, mask)
        if held_now:
            _begin_hold(label, "button")
        else:
            _end_hold(label)

    direction_bits = int(input_held) & INPUT_DIRECTION_MASK
    direction_label = _DIRECTION_HOLD_SPECS.get(direction_bits, "")
    for old_label in tuple(_DIRECTION_HOLD_SPECS.values()):
        if old_label != direction_label:
            _end_hold(old_label)
    if direction_label:
        _begin_hold(direction_label, "direction")

    events[:] = [
        event
        for event in events
        if int(frame_number) - int(event.get("end_frame", frame_number)) < HOLD_LOG_LIFETIME_FRAMES
    ]
    del events[12:]


def _display_button_holds(slot_anim: dict, current_frame: int, limit: int = 8) -> list[dict]:
    out: list[dict] = []
    active = slot_anim.get("button_hold_active", {})

    active_rows = []
    for label, hold in active.items():
        start_frame = int(hold.get("start_frame", current_frame))
        held_frames = max(0, int(current_frame) - start_frame)
        if held_frames < HOLD_LOG_MIN_FRAMES:
            continue
        active_rows.append({
            "id": f"active:{label}:{int(hold.get('seq', 0))}",
            "label": label,
            "frames": held_frames,
            "active": True,
            "alpha": 1.0,
            "seq": int(hold.get("seq", 0)),
        })
    active_rows.sort(key=lambda item: item["seq"], reverse=True)
    out.extend(active_rows)

    for event in slot_anim.get("button_hold_events", []):
        age = max(0, int(current_frame) - int(event.get("end_frame", current_frame)))
        if age >= HOLD_LOG_LIFETIME_FRAMES:
            continue
        out.append({
            "id": f"done:{event.get('label')}:{int(event.get('seq', 0))}",
            "label": str(event.get("label") or "?"),
            "frames": max(0, int(event.get("frames", 0))),
            "active": False,
            "alpha": max(0.0, 1.0 - age / HOLD_LOG_LIFETIME_FRAMES),
            "seq": int(event.get("seq", 0)),
        })
        if len(out) >= limit:
            break

    return out[:limit]


def _draw_compact_hold_history(
    screen,
    font_sm,
    items: list[dict],
    x: int,
    y: int,
    right: int,
    scale: float,
    prev_items: list[dict] | None = None,
    slide_progress: float = 0.0,
) -> None:
    label_w = _draw_history_header_chip(screen, font_sm, "HOLD", x, y, scale)
    draw_x = x + label_w + max(6, int(7 * scale))
    gap = max(5, int(6 * scale))
    clip_rect = pygame.Rect(
        draw_x,
        y - 1,
        max(1, right - draw_x),
        max(font_sm.get_height() + int(8 * scale), int(18 * scale)),
    )

    def _build_parts(source, alpha_override=None):
        rendered = []
        for index, item in enumerate(list(source or [])[:8]):
            label = str(item.get("label") or "?")
            frames = max(0, int(item.get("frames") or 0))
            item_alpha = float(item.get("alpha", 1.0))
            alpha = item_alpha if alpha_override is None else min(item_alpha, float(alpha_override))
            color = _BUTTON_HOLD_COLORS.get(label, (196, 205, 220))
            primary = f"{label} {frames}f"
            secondary = "HELD" if item.get("active") else ""
            rendered.append(
                _render_compact_text_chip(
                    font_sm,
                    primary,
                    color,
                    scale,
                    max(0.0, min(1.0, alpha)),
                    secondary=secondary,
                    emphasis=1.04 if index == 0 else 1.0,
                )
            )
        return rendered

    def _draw_parts(parts, base_x):
        dx = base_x
        for index, surf in enumerate(parts):
            if dx > right:
                break
            screen.blit(surf, (dx, y))
            dx += surf.get_width()
            if index < len(parts) - 1:
                dx += gap

    current_parts = _build_parts(items)
    previous_parts = _build_parts(prev_items, max(0.0, min(1.0, slide_progress))) if prev_items else []

    if not current_parts and not previous_parts:
        empty = font_sm.render(" - ", True, (86, 96, 114))
        screen.blit(empty, (draw_x, y))
        return

    inserted_shift = max(
        int(30 * scale),
        current_parts[0].get_width() + gap if current_parts else int(30 * scale),
    )

    old_clip = screen.get_clip()
    screen.set_clip(clip_rect)
    if slide_progress > 0.001 and previous_parts:
        _draw_parts(previous_parts, draw_x + int(inserted_shift * (1.0 - slide_progress)))
        _draw_parts(current_parts, draw_x - int(inserted_shift * slide_progress))
    else:
        _draw_parts(current_parts, draw_x)
    screen.set_clip(old_clip)


def _freeze_active_input_chip(slot_anim: dict, frame_number: int) -> None:
    """Stop the active frame counter without creating a new history entry."""
    chips = slot_anim.setdefault("input_chips", [])
    if chips and chips[-1].get("end_frame") is None:
        chips[-1]["end_frame"] = int(frame_number)


def _coalesce_recent_direction_chip(
    slot_anim: dict,
    input_history: list,
    token: str,
    frame_number: int,
    *,
    max_age_frames: int = 3,
) -> bool:
    """Fold a brief setup direction into the button input that immediately follows."""
    token = str(token or "").strip()
    if not token or not _input_token_has_buttons(token):
        return False

    chips = slot_anim.setdefault("input_chips", [])
    if not chips:
        return False

    last = chips[-1]
    if last.get("end_frame") is not None:
        return False

    previous_tokens = [
        str(item or "").strip()
        for item in last.get("tokens", [])
        if str(item or "").strip()
    ]
    if len(previous_tokens) != 1:
        return False

    previous_token = previous_tokens[0]
    if _input_token_has_buttons(previous_token):
        return False

    previous_direction, _previous_buttons, _previous_extra = _split_input_token(
        previous_token
    )
    current_direction, _current_buttons, _current_extra = _split_input_token(token)
    if previous_direction != current_direction:
        return False

    start_frame = int(last.get("start_frame") or frame_number)
    age = max(0, int(frame_number) - start_frame)
    if age > max(0, int(max_age_frames)):
        return False

    last["tokens"] = [token]
    last["start_frame"] = int(frame_number)
    last["end_frame"] = None

    if input_history and str(input_history[-1] or "").strip() == previous_token:
        input_history[-1] = token
    else:
        input_history.append(token)

    return True


def _append_input_chip_token(slot_anim: dict, token: str, frame_number: int) -> None:
    """Create one timed chip per raw input change, including direction-only inputs."""
    token = str(token or "").strip()
    if not token or token == "·":
        return

    chips = slot_anim.setdefault("input_chips", [])

    # Any new raw input freezes the previous chip's counter.
    if chips and chips[-1].get("end_frame") is None:
        chips[-1]["end_frame"] = frame_number

    # Avoid double-appending the same token on the same frame from held+pressed paths.
    if chips:
        last = chips[-1]
        if last.get("tokens") == [token] and int(last.get("start_frame") or -1) == int(frame_number):
            return

    chips.append({
        "tokens": [token],
        "start_frame": frame_number,
        "end_frame": None,
    })
    del chips[:-12]

    slot_anim["pending_input_chip_tokens"] = []
    slot_anim["pending_input_chip_start_frame"] = None
    slot_anim["pending_input_last_frame"] = None
    slot_anim["input_chip_break"] = False


def _display_input_chips(slot_anim: dict, current_frame: int, limit: int = 8) -> list[dict]:
    visible: list[dict] = []

    for chip in list(slot_anim.get("input_chips", [])):
        tokens = [
            str(tok).strip()
            for tok in chip.get("tokens", [])
            if str(tok).strip() and str(tok).strip() != "·"
        ]
        if not tokens:
            continue

        start_frame = chip.get("start_frame")
        end_frame = chip.get("end_frame")
        if start_frame is None:
            frames = 0
        else:
            stop = current_frame if end_frame is None else int(end_frame)
            frames = max(0, int(stop) - int(start_frame))

        item = {
            "tokens": tokens,
            "frames": frames,
            "active": end_frame is None,
        }

        # Same-frame duplicates are rejected when chips are created. Keep every
        # later chip here so a real repeated press remains a separate entry.
        visible.append(item)

    chips = visible[-max(1, int(limit)):]
    chips.reverse()
    return chips


_INPUT_DIRECTION_VECTORS = {
    "1": (-1.0, 1.0),
    "2": (0.0, 1.0),
    "3": (1.0, 1.0),
    "4": (-1.0, 0.0),
    "5": (0.0, 0.0),
    "6": (1.0, 0.0),
    "7": (-1.0, -1.0),
    "8": (0.0, -1.0),
    "9": (1.0, -1.0),
}


def _split_input_token(token: str) -> tuple[str, str, str]:
    text = str(token or "").strip().upper()
    if not text or text == "·":
        return text, "", ""
    direction = text[0] if text and text[0] in _INPUT_DIRECTION_VECTORS else "5"
    rest = text[1:] if text[:1] == direction else text
    extra = ""
    if " +" in rest:
        rest, extra = rest.split(" +", 1)
        extra = f"+{extra}"
    return direction, "".join(ch for ch in rest if ch.isalpha()), extra


def _input_token_has_buttons(token: str) -> bool:
    _direction, buttons, _extra = _split_input_token(token)
    return bool(buttons)


def _group_input_history_tokens(source, limit: int = 5) -> list[list[str]]:
    tokens = [str(item or "").strip() for item in (source or []) if str(item or "").strip()]
    groups: list[list[str]] = []
    current: list[str] = []

    for token in tokens:
        if token == "·":
            if current:
                groups.append(current)
                current = []
            continue
        direction, buttons, extra = _split_input_token(token)
        if direction == "5" and not buttons and not extra:
            continue
        if current and current[-1] == token:
            continue
        if not buttons and not extra and current:
            prev_dir, prev_buttons, prev_extra = _split_input_token(current[-1])
            if not prev_buttons and not prev_extra and prev_dir == direction:
                continue
        current.append(token)
        if buttons:
            groups.append(current)
            current = []

    if current:
        groups.append(current)

    if not groups:
        return []
    trimmed = groups[-limit:]
    trimmed.reverse()
    return trimmed


def _render_input_direction_icon(direction: str, color: tuple[int, int, int], scale: float, alpha: float = 1.0) -> pygame.Surface:
    size = max(11, int(14 * scale))
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    rgba = (*color, max(0, min(255, int(255 * alpha))))
    cx = size / 2.0
    cy = size / 2.0
    vec = _INPUT_DIRECTION_VECTORS.get(str(direction), (0.0, 0.0))
    if vec == (0.0, 0.0):
        pygame.draw.circle(surf, rgba, (round(cx), round(cy)), max(2, int(size * 0.18)), 0)
        pygame.draw.circle(surf, (255, 255, 255, max(0, min(255, int(110 * alpha)))), (round(cx), round(cy)), max(3, int(size * 0.30)), 1)
        return surf

    vx, vy = vec
    mag = math.hypot(vx, vy) or 1.0
    ux = vx / mag
    uy = vy / mag
    start = (cx - ux * (size * 0.24), cy - uy * (size * 0.24))
    end = (cx + ux * (size * 0.24), cy + uy * (size * 0.24))
    shaft_w = max(2, int(size * 0.12))
    pygame.draw.line(surf, rgba, start, end, shaft_w)
    px = -uy
    py = ux
    tip = (cx + ux * (size * 0.40), cy + uy * (size * 0.40))
    back = (cx + ux * (size * 0.12), cy + uy * (size * 0.12))
    head_half = size * 0.16
    points = [
        (round(tip[0]), round(tip[1])),
        (round(back[0] + px * head_half), round(back[1] + py * head_half)),
        (round(back[0] - px * head_half), round(back[1] - py * head_half)),
    ]
    pygame.draw.polygon(surf, rgba, points)
    return surf


def _render_input_token_surface(font_sm, token: str, color: tuple[int, int, int], scale: float, alpha: float = 1.0) -> pygame.Surface:
    text = str(token or "").strip()
    if not text:
        return pygame.Surface((1, max(1, font_sm.get_height())), pygame.SRCALPHA)
    if text == "·":
        dot = font_sm.render("•", True, color)
        if alpha < 0.999:
            dot.set_alpha(max(0, min(255, int(255 * alpha))))
        return dot

    direction, buttons, extra = _split_input_token(text)
    icon = _render_input_direction_icon(direction, color, scale, alpha)
    parts = [icon]
    gap = max(3, int(4 * scale))
    width = icon.get_width()
    height = max(icon.get_height(), font_sm.get_height())

    if buttons:
        btn = font_sm.render(buttons, True, color)
        if alpha < 0.999:
            btn.set_alpha(max(0, min(255, int(255 * alpha))))
        parts.append(btn)
        width += gap + btn.get_width()
        height = max(height, btn.get_height())
    if extra:
        extra_surf = font_sm.render(extra, True, (120, 128, 144))
        if alpha < 0.999:
            extra_surf.set_alpha(max(0, min(255, int(255 * alpha))))
        parts.append(extra_surf)
        width += gap + extra_surf.get_width()
        height = max(height, extra_surf.get_height())

    surf = pygame.Surface((max(1, width), max(1, height)), pygame.SRCALPHA)
    dx = 0
    for idx, part in enumerate(parts):
        surf.blit(part, (dx, (height - part.get_height()) // 2))
        dx += part.get_width()
        if idx < len(parts) - 1:
            dx += gap
    return surf


def _render_input_group_surface(font_sm, chip: dict, color: tuple[int, int, int], scale: float, alpha: float = 1.0, emphasis: float = 1.0, show_counter: bool = True, dense: bool = False) -> pygame.Surface:
    """Render an input chip, optionally with its frozen frame counter."""
    tokens = list(chip.get("tokens") or [])
    frames = max(0, int(chip.get("frames") or 0))

    if dense:
        inner_gap = max(2, int(3 * scale))
        pad_x = max(3, int(4 * scale))
        pad_y = max(1, int(2 * scale))
        unit_gap = max(2, int(3 * scale))
        border_radius = max(4, int(5 * scale))
    else:
        inner_gap = max(4, int(5 * scale))
        pad_x = max(5, int(6 * scale))
        pad_y = max(2, int(3 * scale))
        unit_gap = max(3, int(4 * scale))
        border_radius = max(5, int(6 * scale))

    rendered_parts: list[pygame.Surface] = []
    visible_tokens = [str(tok).strip() for tok in tokens if str(tok).strip()]
    for idx, token in enumerate(visible_tokens):
        rendered_parts.append(_render_input_token_surface(font_sm, token, color, scale * emphasis, alpha))
        if idx < len(visible_tokens) - 1:
            sep = font_sm.render(">", True, (90, 98, 114))
            if alpha < 0.999:
                sep.set_alpha(max(0, min(255, int(255 * alpha))))
            rendered_parts.append(sep)

    if not rendered_parts:
        rendered_parts.append(font_sm.render(" - ", True, color))

    content_w = sum(part.get_width() for part in rendered_parts) + inner_gap * max(0, len(rendered_parts) - 1)
    content_h = max(part.get_height() for part in rendered_parts)
    chip_w = content_w + pad_x * 2
    chip_h = content_h + pad_y * 2

    chip_surface = pygame.Surface((max(1, chip_w), max(1, chip_h)), pygame.SRCALPHA)
    bg_alpha = max(0, min(255, int(168 * alpha)))
    border_alpha = max(0, min(255, int(116 * alpha)))
    pygame.draw.rect(chip_surface, (26, 31, 40, bg_alpha), (0, 0, chip_w, chip_h), border_radius=border_radius)
    pygame.draw.rect(chip_surface, (70, 84, 104, border_alpha), (0, 0, chip_w, chip_h), 1, border_radius=border_radius)
    pygame.draw.rect(chip_surface, (*color, max(0, min(255, int(90 * alpha)))), (1, 1, max(1, chip_w - 2), max(2, int(2 * scale))), border_top_left_radius=border_radius, border_top_right_radius=border_radius)

    dx = pad_x
    for idx, part in enumerate(rendered_parts):
        chip_surface.blit(part, (dx, (chip_h - part.get_height()) // 2))
        dx += part.get_width()
        if idx < len(rendered_parts) - 1:
            dx += inner_gap

    if not show_counter:
        return chip_surface

    counter_text = f"{frames}f"
    counter_color = (232, 240, 252) if bool(chip.get("active")) else (154, 165, 184)
    counter_surface = font_sm.render(counter_text, True, counter_color)
    if alpha < 0.999:
        counter_surface.set_alpha(max(0, min(255, int(255 * alpha))))
    counter_pad_x = max(5, int(6 * scale))
    counter_w = counter_surface.get_width() + counter_pad_x * 2
    counter_h = chip_h
    counter_box = pygame.Surface((counter_w, counter_h), pygame.SRCALPHA)
    active = bool(chip.get("active"))
    counter_fill = (34, 44, 58, max(0, min(255, int((190 if active else 148) * alpha))))
    counter_border = (*color, max(0, min(255, int((138 if active else 72) * alpha))))
    pygame.draw.rect(counter_box, counter_fill, (0, 0, counter_w, counter_h), border_radius=border_radius)
    pygame.draw.rect(counter_box, counter_border, (0, 0, counter_w, counter_h), 1, border_radius=border_radius)
    counter_box.blit(counter_surface, ((counter_w - counter_surface.get_width()) // 2, (counter_h - counter_surface.get_height()) // 2))

    unit_w = chip_w + unit_gap + counter_w
    unit_h = max(chip_h, counter_h)
    unit = pygame.Surface((unit_w, unit_h), pygame.SRCALPHA)
    unit.blit(chip_surface, (0, (unit_h - chip_h) // 2))
    unit.blit(counter_box, (chip_w + unit_gap, (unit_h - counter_h) // 2))
    return unit



def _compact_frame_data_state(snap: dict) -> dict:
    """Build the live startup/active/recovery state for the compact HUD.

    TvC's realtime action counter is zero-based.  The frame-data table's active
    range is treated the same way as the hitbox timer: 8-17 displays nine
    active ticks (9..1), so the end value is the recovery boundary rather than
    an inclusive tenth active frame.
    """
    # Never draw timing that belongs to the previous action. The realtime FD
    # sidecar supplies its own action identity; the full-payload metadata is a
    # fallback for builds where that sidecar has not populated yet.
    try:
        live_action_id = int(snap.get("realtime_action_id", 0) or 0) & 0x7FFF
    except Exception:
        live_action_id = 0
    try:
        timing_action_id = int(
            snap.get("realtime_fd_action_id", snap.get("move_metadata_action_id", 0)) or 0
        ) & 0x7FFF
    except Exception:
        timing_action_id = 0
    if live_action_id and timing_action_id and live_action_id != timing_action_id:
        return {"available": False}

    windows: list[tuple[int, int]] = []
    for pair in (snap.get("active_windows") or []):
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        try:
            start = int(pair[0])
            end = int(pair[1])
        except (TypeError, ValueError):
            continue
        if start < 0 or end < start:
            continue
        end_exclusive = end if end > start else start + 1
        item = (start, end_exclusive)
        if item not in windows:
            windows.append(item)

    if not windows:
        try:
            start = int(snap.get("active_start"))
            end = int(snap.get("active_end"))
        except (TypeError, ValueError):
            start = end = -1
        if start >= 0 and end >= start:
            windows.append((start, end if end > start else start + 1))

    windows.sort(key=lambda pair: (pair[0], pair[1]))
    if not windows:
        return {"available": False}

    first_start = min(start for start, _end in windows)
    final_boundary = max(end for _start, end in windows)
    try:
        recovery = max(0, int(snap.get("recovery") or 0))
    except (TypeError, ValueError):
        recovery = 0
    try:
        action_frame = max(0, int(snap.get("realtime_action_frame") or 0))
    except (TypeError, ValueError):
        action_frame = 0

    phase = ""
    remaining = 0
    phase_key = "idle"
    if action_frame < first_start:
        phase = "START"
        phase_key = "startup"
        remaining = first_start - action_frame
    else:
        for start, end in windows:
            if start <= action_frame < end:
                phase = "ACTIVE"
                phase_key = "active"
                remaining = end - action_frame
                break
            if action_frame < start:
                # Keep one move-level clock alive through a multi-hit gap.
                phase = "NEXT"
                phase_key = "gap"
                remaining = start - action_frame
                break
        else:
            if recovery > 0 and action_frame < final_boundary + recovery:
                phase = "REC"
                phase_key = "recovery"
                remaining = final_boundary + recovery - action_frame

    total = max(1, final_boundary + recovery)
    startup_total = max(0, first_start)
    active_total = sum(max(0, int(end) - int(start)) for start, end in windows)
    done = not phase or remaining <= 0
    return {
        "available": True,
        "windows": windows,
        "first_start": first_start,
        "final_boundary": final_boundary,
        "startup_total": startup_total,
        "active_total": active_total,
        "recovery_total": recovery,
        "recovery": recovery,
        "action_frame": action_frame,
        "total": total,
        "phase": phase,
        "phase_key": phase_key,
        "remaining": max(0, remaining),
        "done": done,
    }


def _update_compact_move_timing_race(
    state: dict,
    snap: dict,
    race_clock: dict | None,
    race_anim: dict | None,
) -> dict | None:
    """Latch the opponent's stun expiry into the current move's frame space.

    This is deliberately action-instance based.  A hit that occurs *during*
    the current move must not suddenly create a race against that same move.
    The comparison arms only when a new action (or a same-ID frame reset)
    begins while the opponent is already in hit/block stun.  That is the case
    we care about for links, chains, and cancels.

    Once armed, ``expiry_frame`` lives in the attacker's action-frame space:
        attacker_action_frame + victim_stun_remaining
    Because both native counters pause and advance with the game, the expiry
    marker stays fixed while the move playhead approaches it.
    """
    if not isinstance(race_anim, dict):
        return None
    try:
        action_id = int(snap.get("realtime_action_id", snap.get("mv_id_display", 0)) or 0) & 0x7FFF
    except Exception:
        action_id = 0
    try:
        action_frame = max(0, int(snap.get("realtime_action_frame", 0) or 0))
    except Exception:
        action_frame = 0

    previous_id = int(race_anim.get("move_timing_last_action_id", -1) or -1)
    previous_frame = int(race_anim.get("move_timing_last_action_frame", -1) or -1)
    new_instance = bool(action_id != previous_id or (action_id == previous_id and previous_frame >= 0 and action_frame < previous_frame))

    if new_instance:
        # Clear any comparison owned by the previous move first.  This also
        # means idle/non-FD actions cleanly separate two action instances.
        for key in (
            "move_timing_race_action_id",
            "move_timing_race_expiry_frame",
            "move_timing_race_arm_frame",
            "move_timing_race_start_remaining",
            "move_timing_race_kind",
            "move_timing_race_generation",
        ):
            race_anim.pop(key, None)

    # Do NOT require the stun clock to be present on the single render where
    # the action id flips.  The realtime action lane and the contact sidecar
    # can arrive one render apart, which made the prediction appear once and
    # then seemingly never again.  While this action is still in authored
    # startup, any live stun clock is safe to latch: the current move has not
    # reached its own first active frame yet, so the clock cannot have been
    # created by this move's hit.  This gives the two realtime lanes a whole
    # startup window to handshake instead of a one-frame race.
    try:
        first_start_for_arm = max(0, int(state.get("first_start", 0) or 0))
    except Exception:
        first_start_for_arm = 0
    try:
        existing_race_action = int(race_anim.get("move_timing_race_action_id", -1) or -1)
    except Exception:
        existing_race_action = -1

    can_late_arm = bool(
        state.get("available")
        and isinstance(race_clock, dict)
        and existing_race_action != action_id
        and first_start_for_arm > 0
        and action_frame < first_start_for_arm
    )
    if can_late_arm:
        try:
            stun_remaining = max(0, int(race_clock.get("remaining", 0) or 0))
        except Exception:
            stun_remaining = 0
        if bool(race_clock.get("active", False)) and stun_remaining > 0:
            kind = str(race_clock.get("kind") or "HS").upper()
            race_anim["move_timing_race_action_id"] = action_id
            race_anim["move_timing_race_expiry_frame"] = action_frame + stun_remaining
            race_anim["move_timing_race_arm_frame"] = action_frame
            race_anim["move_timing_race_start_remaining"] = stun_remaining
            race_anim["move_timing_race_kind"] = kind
            race_anim["move_timing_race_generation"] = int(race_clock.get("generation", 0) or 0)

    race_anim["move_timing_last_action_id"] = action_id
    race_anim["move_timing_last_action_frame"] = action_frame

    try:
        race_action_id = int(race_anim.get("move_timing_race_action_id", -1) or -1)
    except Exception:
        race_action_id = -1
    if not bool(state.get("available")) or race_action_id != action_id:
        return None

    try:
        expiry_frame = max(0, int(race_anim.get("move_timing_race_expiry_frame", 0) or 0))
        first_start = max(0, int(state.get("first_start", 0) or 0))
        start_remaining = max(0, int(race_anim.get("move_timing_race_start_remaining", 0) or 0))
    except Exception:
        return None
    if expiry_frame <= 0 or start_remaining <= 0:
        return None

    remaining_now = max(0, expiry_frame - action_frame)
    margin = int(expiry_frame - first_start)
    kind = str(race_anim.get("move_timing_race_kind") or "HS").upper()
    return {
        "kind": kind,
        "expiry_frame": expiry_frame,
        "remaining": remaining_now,
        "start_remaining": start_remaining,
        "margin": margin,
        "generation": int(race_anim.get("move_timing_race_generation", 0) or 0),
        "arm_frame": max(0, int(race_anim.get("move_timing_race_arm_frame", 0) or 0)),
        "first_start": first_start,
        "windows": list(state.get("windows") or []),
    }




def _realtime_punish_blockstun_contact_clock(slot_anim: dict, snap: dict) -> dict | None:
    """Return the blocker-owned copy of the native +0x1204 countdown.

    The regular ``realtime_blockstun_contact`` token intentionally lives on the
    attacker and powers blockstring timing.  This mirror lives on the fighter
    who actually blocked so their STUN rail can deplete during blockstun and so
    the punish session can survive the transition into the defender's response.
    """
    event = snap.get("realtime_punish_blockstun_contact") if isinstance(snap, dict) else None
    if not isinstance(event, dict):
        return None
    try:
        generation = max(0, int(event.get("generation", 0) or 0))
        target = max(0, int(event.get("target", 0) or 0))
    except Exception:
        return None
    if generation <= 0 or target <= 0:
        return None

    if generation != int(slot_anim.get("punish_bs_contact_generation", 0) or 0):
        slot_anim["punish_bs_generation_flash"] = 1.0
        slot_anim["punish_bs_contact_generation"] = generation
        slot_anim["punish_bs_contact_target"] = target
        slot_anim["punish_bs_contact_remaining"] = target
        slot_anim["punish_bs_contact_move"] = str(event.get("move_label") or "")

    target = max(0, int(slot_anim.get("punish_bs_contact_target", target) or 0))
    try:
        current = max(0, min(target, int(event.get("native_blockstun_current", event.get("native_blockstun", target)) or 0)))
    except Exception:
        current = max(0, int(slot_anim.get("punish_bs_contact_remaining", target) or target))
    previous_remaining = max(0, min(target, int(slot_anim.get("punish_bs_contact_remaining", target) or target)))
    remaining = min(previous_remaining, current)
    if previous_remaining > 0 and remaining <= 0:
        slot_anim["punish_bs_expire_flash"] = 1.0
    slot_anim["punish_bs_contact_remaining"] = remaining
    return {
        "generation": 3_000_000 + generation,
        "native_generation": generation,
        "target": target,
        "remaining": remaining,
        "elapsed": max(0, target - remaining),
        "active": remaining > 0,
        "expired": remaining <= 0,
        "move_label": str(slot_anim.get("punish_bs_contact_move") or ""),
        "attacker_slot": str(event.get("attacker_slot") or ""),
        "victim_slot": str(event.get("victim_slot") or ""),
        "action_id": int(event.get("action_id", 0) or 0),
        "attacker_action_frame": max(0, int(event.get("attacker_action_frame", 0) or 0)),
        "attacker_move_end_frame": max(0, int(event.get("attacker_move_end_frame", 0) or 0)),
    }


def _update_compact_punish_race(state: dict, snap: dict, race_anim: dict | None) -> dict | None:
    """Track one blocked move and grade the defender's attempted punish.

    A block contact records the attacker's authored recovery boundary.  When the
    blocker starts a move with frame data, we compare that move's first active
    frame with the opponent's remaining recovery.  Positive margin means the
    response becomes active while the opponent is still recovering; negative
    margin means the opponent was already free.
    """
    if not isinstance(race_anim, dict):
        return None

    event = snap.get("realtime_punish_blockstun_contact") if isinstance(snap, dict) else None
    if isinstance(event, dict):
        try:
            generation = max(0, int(event.get("generation", 0) or 0))
        except Exception:
            generation = 0
        if generation > 0 and generation != int(race_anim.get("punish_generation", 0) or 0):
            try:
                victim_action_id = int(snap.get("realtime_action_id", snap.get("mv_id_display", 0)) or 0) & 0x7FFF
            except Exception:
                victim_action_id = 0
            try:
                victim_action_frame = max(0, int(snap.get("realtime_action_frame", 0) or 0))
            except Exception:
                victim_action_frame = 0
            race_anim["punish_generation"] = generation
            race_anim["punish_attacker_slot"] = str(event.get("attacker_slot") or "")
            race_anim["punish_attacker_action_id"] = int(event.get("action_id", 0) or 0) & 0x7FFF
            race_anim["punish_attacker_contact_frame"] = max(0, int(event.get("attacker_action_frame", 0) or 0))
            race_anim["punish_attacker_move_end_frame"] = max(0, int(event.get("attacker_move_end_frame", 0) or 0))
            race_anim["punish_block_target"] = max(0, int(event.get("target", 0) or 0))
            race_anim["punish_block_remaining"] = max(0, int(event.get("native_blockstun_current", event.get("native_blockstun", 0)) or 0))
            race_anim["punish_contact_victim_action_id"] = victim_action_id
            race_anim["punish_contact_victim_action_frame"] = victim_action_frame
            race_anim["punish_attempt_action_id"] = -1
            race_anim["punish_attempt_expiry_frame"] = -1
            race_anim["punish_attempt_margin"] = 0
            race_anim["punish_attempt_cancelled"] = False
            race_anim["punish_attempt_attacker_remaining"] = 0
            race_anim["punish_attempt_first_start"] = 0
            race_anim["punish_attempt_started"] = False
        if generation > 0 and generation == int(race_anim.get("punish_generation", 0) or 0):
            try:
                race_anim["punish_block_remaining"] = max(0, int(event.get("native_blockstun_current", event.get("native_blockstun", 0)) or 0))
            except Exception:
                pass

    generation = int(race_anim.get("punish_generation", 0) or 0)
    if generation <= 0:
        return None

    try:
        action_id = int(snap.get("realtime_action_id", snap.get("mv_id_display", 0)) or 0) & 0x7FFF
    except Exception:
        action_id = 0
    try:
        action_frame = max(0, int(snap.get("realtime_action_frame", 0) or 0))
    except Exception:
        action_frame = 0

    attempt_id = int(race_anim.get("punish_attempt_action_id", -1) or -1)
    if attempt_id >= 0:
        if action_id != attempt_id:
            return None
        expiry_frame = max(0, int(race_anim.get("punish_attempt_expiry_frame", 0) or 0))
        first_start = max(0, int(race_anim.get("punish_attempt_first_start", 0) or 0))
        return {
            "kind": "PUNISH",
            "expiry_frame": expiry_frame,
            "remaining": max(0, expiry_frame - action_frame),
            "start_remaining": max(0, int(race_anim.get("punish_attempt_attacker_remaining", 0) or 0)),
            "margin": int(race_anim.get("punish_attempt_margin", 0) or 0),
            "generation": 4_000_000 + generation,
            "arm_frame": max(0, int(race_anim.get("punish_attempt_arm_frame", 0) or 0)),
            "first_start": first_start,
            "windows": list(state.get("windows") or []),
            "cancelled": bool(race_anim.get("punish_attempt_cancelled", False)),
            "attacker_slot": str(race_anim.get("punish_attacker_slot") or ""),
            "theoretical_window": max(0, int(race_anim.get("punish_theoretical_window", 0) or 0)),
        }

    if not bool(state.get("available")):
        return None
    first_start = max(0, int(state.get("first_start", 0) or 0))
    contact_action_id = int(race_anim.get("punish_contact_victim_action_id", -1) or -1)
    contact_action_frame = max(0, int(race_anim.get("punish_contact_victim_action_frame", 0) or 0))
    is_new_response = bool(action_id != contact_action_id or action_frame < contact_action_frame)
    if not is_new_response or action_id < 0x0100:
        return None

    attacker_slot = str(race_anim.get("punish_attacker_slot") or "")
    attacker_original_action = int(race_anim.get("punish_attacker_action_id", 0) or 0) & 0x7FFF
    attacker_move_end = max(0, int(race_anim.get("punish_attacker_move_end_frame", 0) or 0))
    attacker_contact_frame = max(0, int(race_anim.get("punish_attacker_contact_frame", 0) or 0))
    block_target = max(0, int(race_anim.get("punish_block_target", 0) or 0))
    if attacker_move_end <= attacker_contact_frame:
        return None

    attacker_snap = _display_slots.get(attacker_slot) or {}
    try:
        attacker_now_action = int(attacker_snap.get("realtime_action_id", attacker_snap.get("mv_id_display", 0)) or 0) & 0x7FFF
    except Exception:
        attacker_now_action = 0
    try:
        attacker_now_frame = max(0, int(attacker_snap.get("realtime_action_frame", 0) or 0))
    except Exception:
        attacker_now_frame = 0

    cancelled = False
    if attacker_now_action == attacker_original_action:
        attacker_remaining = max(0, attacker_move_end - attacker_now_frame)
    else:
        # A new offensive action means the attacker chained/cancelled instead of
        # simply recovering.  That is blockstring territory, not a guaranteed
        # punish against the original move.
        cancelled = attacker_now_action >= 0x0100
        attacker_remaining = 0

    expiry_frame = action_frame + attacker_remaining
    margin = int(expiry_frame - first_start)
    theoretical_window = max(0, (attacker_move_end - attacker_contact_frame) - block_target)
    race_anim["punish_attempt_action_id"] = action_id
    race_anim["punish_attempt_expiry_frame"] = expiry_frame
    race_anim["punish_attempt_margin"] = margin
    race_anim["punish_attempt_cancelled"] = cancelled
    race_anim["punish_attempt_attacker_remaining"] = attacker_remaining
    race_anim["punish_attempt_first_start"] = first_start
    race_anim["punish_attempt_arm_frame"] = action_frame
    race_anim["punish_attempt_started"] = True
    race_anim["punish_theoretical_window"] = theoretical_window

    return {
        "kind": "PUNISH",
        "expiry_frame": expiry_frame,
        "remaining": max(0, expiry_frame - action_frame),
        "start_remaining": attacker_remaining,
        "margin": margin,
        "generation": 4_000_000 + generation,
        "arm_frame": action_frame,
        "first_start": first_start,
        "windows": list(state.get("windows") or []),
        "cancelled": cancelled,
        "attacker_slot": attacker_slot,
        "theoretical_window": theoretical_window,
    }

def _draw_compact_frame_data_bar(
    screen,
    font_sm,
    snap: dict,
    x: int,
    y: int,
    right: int,
    scale: float,
    *,
    race_clock: dict | None = None,
    race_anim: dict | None = None,
    race_override: dict | None = None,
) -> None:
    """Draw a dedicated MOVE TIMING rail above the PHASE badges.

    The timeline gets the first row directly beneath STUN so the two gauges
    read as one visual race. PHASE remains a separate numeric badge row below
    it and never compresses the MOVE TIMING runway. MOVE TIMING uses the same
    left-to-right elapsed-frame axis as the STUN head, while the STUN fill
    itself depletes from full to empty.
    """
    state = _compact_frame_data_state(snap or {})
    row_h = max(font_sm.get_height() + max(5, int(6 * scale)), int(17 * scale))
    row_gap = max(2, int(3 * scale))
    timeline_y = y
    phase_y = y + row_h + row_gap

    # ---- Row 2: numeric phase / race badges only -------------------------
    phase_label_w = _draw_history_header_chip(screen, font_sm, "PHASE", x, phase_y, scale)
    phase_content_x = x + phase_label_w + max(5, int(6 * scale))

    # Update the action-instance tracker even for actions without authored
    # timing so an idle state cleanly resets the previous race.
    race = race_override if isinstance(race_override, dict) else _update_compact_move_timing_race(state, snap or {}, race_clock, race_anim)

    phase_colors = {
        "startup": (242, 194, 92),
        "active": (255, 104, 112),
        "gap": (205, 173, 104),
        "recovery": (112, 170, 245),
        "idle": (126, 139, 160),
    }
    phase_fills = {
        "startup": (126, 96, 42),
        "active": (164, 53, 64),
        "gap": (86, 73, 48),
        "recovery": (49, 88, 146),
        "idle": (55, 66, 84),
    }

    def draw_badge(cursor_x: int, text: str, fg, fill_rgb, *, min_text: str | None = None) -> pygame.Rect:
        surf = font_sm.render(text, True, fg)
        pad_x = max(4, int(5 * scale))
        badge_h = max(surf.get_height() + max(3, int(4 * scale)), int(15 * scale))
        min_w = font_sm.size(min_text)[0] + pad_x * 2 if min_text else 0
        badge_w = max(min_w, surf.get_width() + pad_x * 2)
        badge_y = phase_y + max(0, (row_h - badge_h) // 2)
        rect = pygame.Rect(cursor_x, badge_y, badge_w, badge_h)
        box = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
        box.fill((*fill_rgb, 116))
        pygame.draw.rect(box, (*fg, 176), box.get_rect(), 1, border_radius=max(2, int(3 * scale)))
        box.blit(surf, ((rect.width - surf.get_width()) // 2, (rect.height - surf.get_height()) // 2))
        screen.blit(box, rect.topleft)
        return rect

    if state.get("available"):
        phase_key = str(state.get("phase_key") or "idle")
        remaining = int(state.get("remaining") or 0)
        phase = str(state.get("phase") or "")
        phase_color = phase_colors.get(phase_key, phase_colors["idle"])
        status_text = f"{phase} {remaining}f" if phase and remaining > 0 else "DONE"
        status_rect = draw_badge(
            phase_content_x,
            status_text,
            phase_color if phase else phase_colors["idle"],
            phase_fills.get(phase_key, phase_fills["idle"]),
            min_text="ACTIVE 99f",
        )
        cursor_x = status_rect.right + max(5, int(6 * scale))

        # Keep the live phase countdown, but also print the authored maximum
        # for each phase so the player does not have to remember the first tick.
        # For multi-hit moves ACTIVE is the total authored active frames across
        # all windows; gaps remain represented on the MOVE TIMING rail.
        startup_total = max(0, int(state.get("startup_total", state.get("first_start", 0)) or 0))
        active_total = max(0, int(state.get("active_total", 0) or 0))
        recovery_total = max(0, int(state.get("recovery_total", state.get("recovery", 0)) or 0))
        summary_text = f"STARTUP {startup_total}f  ACTIVE {active_total}f  REC {recovery_total}f"
        summary_surf = font_sm.render(summary_text, True, (164, 176, 196))
        summary_gap = max(5, int(6 * scale))
        # The summary is informational text rather than another status badge.
        # Draw it whenever it fits; on very narrow panels retain the live timer
        # and race verdict rather than clipping the row.
        if cursor_x + summary_surf.get_width() + summary_gap <= right:
            screen.blit(
                summary_surf,
                (cursor_x, phase_y + max(0, (row_h - summary_surf.get_height()) // 2)),
            )
            cursor_x += summary_surf.get_width() + summary_gap

        if race:
            kind = str(race.get("kind") or "HS").upper()
            if kind == "AIR HS":
                race_color = STUN_CLOCK_UNTECH_COLOR
                race_fill = (77, 64, 112)
            elif kind == "BS":
                race_color = STUN_CLOCK_BLOCK_COLOR
                race_fill = (116, 68, 42)
            elif kind == "PUNISH":
                race_color = (111, 225, 157)
                race_fill = (37, 102, 69)
            else:
                race_color = STUN_CLOCK_HIT_COLOR
                race_fill = (42, 84, 96)

            if kind == "PUNISH":
                stun_text = f"THEY READY {int(race.get('remaining', 0) or 0)}f"
                min_stun_text = "THEY READY 99f"
            else:
                stun_text = f"{kind} END {int(race.get('remaining', 0) or 0)}f"
                min_stun_text = "AIR HS END 99f" if kind == "AIR HS" else ("BS END 99f" if kind == "BS" else "HS END 99f")
            stun_rect = draw_badge(cursor_x, stun_text, race_color, race_fill, min_text=min_stun_text)
            cursor_x = stun_rect.right + max(4, int(5 * scale))

            margin = int(race.get("margin", 0) or 0)
            if kind == "PUNISH" and bool(race.get("cancelled", False)):
                verdict_text = "CHAINED"
                verdict_color = (242, 194, 92)
                verdict_fill = (126, 96, 42)
            elif kind == "PUNISH" and margin > 0:
                verdict_text = f"{margin}F PUNISH"
                verdict_color = (111, 225, 157)
                verdict_fill = (37, 102, 69)
            elif kind == "PUNISH" and margin == 0:
                verdict_text = "LAST FRAME"
                verdict_color = (242, 194, 92)
                verdict_fill = (126, 96, 42)
            elif margin > 0:
                verdict_text = f"{margin}F EARLY"
                verdict_color = (242, 194, 92)
                verdict_fill = (126, 96, 42)
            elif margin == 0:
                verdict_text = "ON TIME"
                verdict_color = (111, 225, 157)
                verdict_fill = (37, 102, 69)
            else:
                verdict_text = f"{abs(margin)}F LATE"
                verdict_color = (255, 113, 124)
                verdict_fill = (126, 46, 56)

            # Compact EARLY <-> LATE cue.  Zero is the center line, positive
            # race margin lands to the left (early), and negative margin lands
            # to the right (late).  Clamp large misses to the rail edge so the
            # relationship stays readable during fast play.
            verdict_s = font_sm.render(verdict_text, True, verdict_color)
            verdict_pad = max(4, int(5 * scale))
            verdict_w = verdict_s.get_width() + verdict_pad * 2
            gap_w = max(4, int(5 * scale))
            min_gauge_w = max(46, int(56 * scale))
            max_gauge_w = max(min_gauge_w, int(82 * scale))
            room_for_gauge = right - cursor_x - verdict_w - gap_w

            if room_for_gauge >= min_gauge_w:
                gauge_w = min(max_gauge_w, room_for_gauge)
                gauge_h = max(6, int(7 * scale))
                gauge_y = phase_y + max(0, (row_h - gauge_h) // 2)
                gauge = pygame.Rect(cursor_x, gauge_y, gauge_w, gauge_h)
                gauge_radius = max(2, gauge_h // 2)

                pygame.draw.rect(screen, (24, 30, 40), gauge, border_radius=gauge_radius)
                half_w = max(1, gauge.width // 2)
                early_zone = pygame.Surface((half_w, gauge.height), pygame.SRCALPHA)
                early_zone.fill((242, 194, 92, 42))
                screen.blit(early_zone, gauge.topleft)
                late_w = max(1, gauge.width - half_w)
                late_zone = pygame.Surface((late_w, gauge.height), pygame.SRCALPHA)
                late_zone.fill((255, 113, 124, 38))
                screen.blit(late_zone, (gauge.x + half_w, gauge.y))
                pygame.draw.rect(screen, (78, 91, 112), gauge, 1, border_radius=gauge_radius)

                center_x = gauge.centerx
                pygame.draw.line(
                    screen,
                    (111, 225, 157),
                    (center_x, gauge.y - 2),
                    (center_x, gauge.bottom + 1),
                    max(1, int(1 * scale)),
                )

                visual_range = 12.0
                signed_visual = max(-visual_range, min(visual_range, -float(margin)))
                marker_frac = (signed_visual + visual_range) / (visual_range * 2.0)
                marker_x = gauge.x + int(round(marker_frac * max(1, gauge.width - 1)))
                marker_x = max(gauge.left + 1, min(gauge.right - 2, marker_x))
                marker_color = _hud_brighten(verdict_color, 45)
                pygame.draw.line(
                    screen,
                    marker_color,
                    (marker_x, gauge.y - 3),
                    (marker_x, gauge.bottom + 2),
                    max(1, int(2 * scale)),
                )
                cap = max(2, int(2 * scale))
                pygame.draw.polygon(
                    screen,
                    marker_color,
                    [
                        (marker_x - cap, gauge.y - 4),
                        (marker_x + cap, gauge.y - 4),
                        (marker_x, gauge.y - 1),
                    ],
                )
                cursor_x = gauge.right + gap_w

            if right - cursor_x >= max(48, verdict_w):
                draw_badge(cursor_x, verdict_text, verdict_color, verdict_fill)
    else:
        muted = font_sm.render("-", True, (86, 96, 114))
        screen.blit(muted, (phase_content_x, phase_y + max(0, (row_h - muted.get_height()) // 2)))

    # ---- Row 1: dedicated MOVE TIMING rail -------------------------------
    timing_label_w = _draw_history_header_chip(screen, font_sm, "MOVE TIMING", x, timeline_y, scale)
    track_x = x + timing_label_w + max(5, int(6 * scale))
    track_w = max(32, right - track_x)
    track_h = max(9, int(10 * scale))
    track_y = timeline_y + max(0, (row_h - track_h) // 2)
    track_rect = pygame.Rect(track_x, track_y, track_w, track_h)
    radius = max(2, track_h // 2)
    pygame.draw.rect(screen, (20, 25, 34), track_rect, border_radius=radius)
    pygame.draw.rect(screen, (72, 84, 106), track_rect, 1, border_radius=radius)

    if not state.get("available"):
        return

    phase_key = str(state.get("phase_key") or "idle")
    phase_color = phase_colors.get(phase_key, phase_colors["idle"])
    move_total = max(1, int(state.get("total") or 1))
    race_expiry = max(0, int(race.get("expiry_frame", 0) or 0)) if race else 0
    # MOVE and stun expiry share one frame scale.  This rail does not borrow
    # width from the badges above, so their physical relationship stays legible.
    display_total = max(move_total, race_expiry, 1)
    first_start = max(0, int(state.get("first_start") or 0))
    final_boundary = max(first_start, int(state.get("final_boundary") or first_start))
    recovery = max(0, int(state.get("recovery") or 0))
    windows = list(state.get("windows") or [])

    def px(frame_value: float) -> int:
        frac = max(0.0, min(1.0, float(frame_value) / float(display_total)))
        return track_rect.x + int(round(frac * track_rect.width))

    inner_y = track_rect.y + 1
    inner_h = max(1, track_rect.height - 2)

    if first_start > 0:
        sx0, sx1 = px(0), px(first_start)
        if sx1 > sx0:
            pygame.draw.rect(screen, (128, 98, 43), pygame.Rect(sx0, inner_y, sx1 - sx0, inner_h))

    for start, end in windows:
        ax0, ax1 = px(int(start)), px(int(end))
        if ax1 > ax0:
            pygame.draw.rect(screen, (168, 53, 64), pygame.Rect(ax0, inner_y, ax1 - ax0, inner_h))

    if recovery > 0:
        rx0, rx1 = px(final_boundary), px(final_boundary + recovery)
        if rx1 > rx0:
            pygame.draw.rect(screen, (48, 88, 148), pygame.Rect(rx0, inner_y, rx1 - rx0, inner_h))

    if display_total > move_total:
        move_end_x = px(move_total)
        pygame.draw.line(screen, (102, 115, 136), (move_end_x, track_rect.y + 1), (move_end_x, track_rect.bottom - 2), 1)

    current = max(0, int(state.get("action_frame") or 0))
    elapsed_right = max(track_rect.left, min(track_rect.right, px(min(current + 1, display_total))))
    if elapsed_right > track_rect.left:
        elapsed = pygame.Surface((elapsed_right - track_rect.left, inner_h), pygame.SRCALPHA)
        elapsed.fill((255, 255, 255, 26))
        screen.blit(elapsed, (track_rect.left, inner_y))

    for boundary in (first_start, final_boundary):
        if boundary <= 0 or boundary >= display_total:
            continue
        bx = px(boundary)
        pygame.draw.line(screen, (168, 181, 203), (bx, track_rect.y + 1), (bx, track_rect.bottom - 2), 1)

    # Comparison marker on the same authored move axis. HS/BS use a live stun
    # sweep. PUNISH instead marks when the opponent becomes actionable, because
    # the defender's own playhead already shows where the punish attempt is now.
    if race and race_expiry > 0:
        kind = str(race.get("kind") or "HS").upper()
        if kind == "AIR HS":
            race_color = STUN_CLOCK_UNTECH_COLOR
        elif kind == "BS":
            race_color = STUN_CLOCK_BLOCK_COLOR
        elif kind == "PUNISH":
            race_color = (111, 225, 157)
        else:
            race_color = STUN_CLOCK_HIT_COLOR
        race_head = _hud_brighten(race_color, 64)
        deadline_x = max(track_rect.left + 1, min(track_rect.right - 1, px(race_expiry)))

        if kind == "PUNISH":
            # The green runway is the time the defender has before the blocked
            # attacker becomes actionable. The red active segment crossing this
            # marker is therefore a guaranteed punish at a glance.
            if deadline_x > track_rect.left + 1:
                ready_window = pygame.Surface((deadline_x - track_rect.left, inner_h), pygame.SRCALPHA)
                ready_window.fill((*race_color, 24))
                screen.blit(ready_window, (track_rect.left, inner_y))
            line_y = track_rect.bottom - 2
            if deadline_x > track_rect.left + 1:
                pygame.draw.line(screen, race_color, (track_rect.left + 2, line_y), (deadline_x, line_y), max(1, int(2 * scale)))
            pygame.draw.line(screen, race_head, (deadline_x, track_rect.y - 2), (deadline_x, track_rect.bottom + 2), max(1, int(2 * scale)))
            cap = max(2, int(3 * scale))
            pygame.draw.polygon(
                screen,
                race_head,
                [(deadline_x - cap, track_rect.y - 3), (deadline_x + cap, track_rect.y - 3), (deadline_x, track_rect.y + 1)],
            )
        else:
            hs_remaining = max(0, int(race.get("remaining", 0) or 0))
            hs_elapsed = max(0, min(race_expiry, race_expiry - hs_remaining))
            hs_now_x = max(track_rect.left + 1, min(track_rect.right - 1, px(hs_elapsed)))

            if deadline_x > track_rect.left + 1:
                stun_window = pygame.Surface((deadline_x - track_rect.left, inner_h), pygame.SRCALPHA)
                stun_window.fill((*race_color, 28))
                screen.blit(stun_window, (track_rect.left, inner_y))

            strip_h = max(2, int(3 * scale))
            strip_y = track_rect.bottom - strip_h - 1
            if deadline_x > track_rect.left + 1:
                strip_bg = pygame.Surface((deadline_x - track_rect.left, strip_h), pygame.SRCALPHA)
                strip_bg.fill((*_hud_darken(race_color, 18), 122))
                screen.blit(strip_bg, (track_rect.left, strip_y))
            if hs_now_x > track_rect.left + 1:
                strip_fill = pygame.Surface((hs_now_x - track_rect.left, strip_h), pygame.SRCALPHA)
                strip_fill.fill((*race_color, 182))
                screen.blit(strip_fill, (track_rect.left, strip_y))

            pygame.draw.line(screen, race_color, (hs_now_x, track_rect.centery), (hs_now_x, track_rect.bottom + 1), 1)
            now_cap = max(2, int(2 * scale))
            pygame.draw.polygon(
                screen,
                race_color,
                [(hs_now_x - now_cap, track_rect.bottom + 2), (hs_now_x + now_cap, track_rect.bottom + 2), (hs_now_x, track_rect.bottom - 1)],
            )

            line_y = track_rect.bottom - 2
            if deadline_x > track_rect.left + 1:
                pygame.draw.line(screen, race_color, (track_rect.left + 2, line_y), (deadline_x, line_y), max(1, int(2 * scale)))
            pygame.draw.line(screen, race_head, (deadline_x, track_rect.y - 2), (deadline_x, track_rect.bottom + 2), max(1, int(2 * scale)))
            cap = max(2, int(3 * scale))
            pygame.draw.polygon(
                screen,
                race_head,
                [(deadline_x - cap, track_rect.y - 3), (deadline_x + cap, track_rect.y - 3), (deadline_x, track_rect.y + 1)],
            )

    playhead_x = px(min(float(current) + 0.5, float(display_total)))
    playhead_x = max(track_rect.left + 1, min(track_rect.right - 2, playhead_x))
    playhead_w = max(2, int(2 * scale))
    glow = pygame.Surface((playhead_w + 4, track_rect.height + 6), pygame.SRCALPHA)
    pygame.draw.rect(glow, (238, 246, 255, 52), glow.get_rect(), border_radius=2)
    screen.blit(glow, (playhead_x - playhead_w // 2 - 2, track_rect.y - 3))
    pygame.draw.rect(
        screen,
        (246, 250, 255),
        pygame.Rect(playhead_x - playhead_w // 2, track_rect.y - 2, playhead_w, track_rect.height + 4),
        border_radius=1,
    )
    cap_r = max(2, int(2 * scale))
    pygame.draw.circle(screen, phase_color, (playhead_x, track_rect.y - 2), cap_r)

def _draw_compact_input_history(
    screen,
    font_sm,
    chips: list[dict],
    x: int,
    y: int,
    right: int,
    scale: float,
    prev_chips: list[dict] | None = None,
    slide_progress: float = 0.0,
) -> None:
    row_gap = max(2, int(2 * scale))
    label_w = _history_label_width(font_sm, scale)
    label_h = max(font_sm.get_height() + max(4, int(5 * scale)), int(15 * scale))
    frame_y = y + label_h + row_gap
    draw_x = x + label_w + max(5, int(6 * scale))
    gap = max(2, int(3 * scale))

    _draw_history_header_chip(screen, font_sm, "INPUTS", x, y, scale)
    _draw_history_header_chip(screen, font_sm, "FRAMES", x, frame_y, scale)

    recency_colors = [
        (92, 232, 146),
        (132, 204, 255),
        (232, 236, 244),
        (198, 207, 220),
        (170, 181, 199),
        (146, 158, 178),
        (124, 137, 158),
        (106, 119, 140),
    ]

    def _build_units(source, alpha: float):
        rendered = []
        for idx, chip in enumerate(list(source or [])[:8]):
            color = recency_colors[min(idx, len(recency_colors) - 1)]
            emphasis = 1.03 if idx == 0 else 1.0
            input_surface = _render_input_group_surface(
                font_sm,
                chip,
                color,
                scale,
                alpha,
                emphasis,
                show_counter=False,
                dense=True,
            )

            frames = max(0, int(chip.get("frames") or 0))
            frame_color = (232, 240, 252) if bool(chip.get("active")) else (154, 165, 184)
            frame_surface = font_sm.render(f"{frames}f", True, frame_color)
            if alpha < 0.999:
                frame_surface.set_alpha(max(0, min(255, int(255 * alpha))))

            counter_pad = max(2, int(3 * scale))
            unit_width = max(
                input_surface.get_width(),
                frame_surface.get_width() + counter_pad * 2,
            )
            unit_height = (
                input_surface.get_height()
                + row_gap
                + frame_surface.get_height()
            )
            unit = pygame.Surface(
                (max(1, unit_width), max(1, unit_height)),
                pygame.SRCALPHA,
            )
            unit.blit(
                input_surface,
                ((unit_width - input_surface.get_width()) // 2, 0),
            )
            unit.blit(
                frame_surface,
                (
                    (unit_width - frame_surface.get_width()) // 2,
                    input_surface.get_height() + row_gap,
                ),
            )
            rendered.append(unit)
        return rendered

    def _draw_units(units, base_x: int):
        dx = base_x
        for idx, unit in enumerate(units):
            if dx >= right:
                break
            screen.blit(unit, (dx, y))
            dx += unit.get_width()
            if idx < len(units) - 1:
                dx += gap

    if not chips and not prev_chips:
        empty = font_sm.render(" - ", True, (86, 96, 114))
        screen.blit(empty, (draw_x, y))
        screen.blit(empty, (draw_x, frame_y))
        return

    current_units = _build_units(chips, 1.0)
    previous_units = (
        _build_units(prev_chips, max(0.0, min(1.0, slide_progress)))
        if prev_chips
        else []
    )

    if not current_units and not previous_units:
        empty = font_sm.render(" - ", True, (86, 96, 114))
        screen.blit(empty, (draw_x, y))
        screen.blit(empty, (draw_x, frame_y))
        return

    input_height = current_units[0].get_height() if current_units else (
        previous_units[0].get_height() if previous_units else font_sm.get_height() * 2
    )
    clip_rect = pygame.Rect(
        draw_x,
        y - 2,
        max(1, right - draw_x),
        max(input_height + int(4 * scale), int(32 * scale)),
    )

    inserted_shift = int(28 * scale)
    if current_units:
        inserted_shift = max(
            inserted_shift,
            current_units[0].get_width() + gap,
        )

    old_clip = screen.get_clip()
    screen.set_clip(clip_rect)
    if current_units:
        screen.blit(current_units[0], (draw_x, y))
        tail_x = draw_x + current_units[0].get_width() + gap
        current_tail = current_units[1:]
        previous_tail = previous_units
        if slide_progress > 0.001 and previous_tail:
            _draw_units(
                previous_tail,
                tail_x + int(inserted_shift * (1.0 - slide_progress)),
            )
            if current_tail:
                _draw_units(
                    current_tail,
                    tail_x - int(inserted_shift * slide_progress),
                )
        elif current_tail:
            _draw_units(current_tail, tail_x)
    elif previous_units:
        _draw_units(previous_units, draw_x)
    screen.set_clip(old_clip)


def _merge_move_history(*lists: list[dict]) -> list[dict]:
    """Return the literal five most recent committed move instances.

    Repeated text is meaningful (2A -> 2A is two moves), so never deduplicate
    this list by label.  Native sample time / log sequence outrank renderer
    frame numbers, which may tie when several cancels are consumed in one draw.
    """
    merged: list[dict] = []
    for seq in lists:
        for item in seq or []:
            if item and item.get("text"):
                merged.append(item)
    merged.sort(
        key=lambda item: (
            int(item.get("sample_ns", 0) or 0),
            int(item.get("log_seq", 0) or 0),
            int(item.get("frame", 0) or 0),
        ),
        reverse=True,
    )
    return merged[:5]


def _draw_compact_move_history(screen, font_sm, items: list[dict], x: int, y: int, right: int, scale: float, prev_texts: list[str] | None = None, slide_progress: float = 0.0) -> None:
    label_w = _draw_history_header_chip(screen, font_sm, "MOVES", x, y, scale)
    draw_x = x + label_w + max(6, int(7 * scale))
    gap = max(5, int(6 * scale))
    clip_rect = pygame.Rect(draw_x, y - 1, max(1, right - draw_x), max(font_sm.get_height() + int(8 * scale), int(18 * scale)))

    recency_colors = [
        (92, 232, 146),
        (132, 204, 255),
        (232, 236, 244),
        (178, 188, 204),
        (122, 134, 153),
    ]

    def _normalize_texts(source) -> list[dict]:
        out = []
        for item in source or []:
            txt = str(item.get("text") if isinstance(item, dict) else item or "").strip()
            if txt:
                out.append({"text": txt})
            if len(out) >= 5:
                break
        return out

    def _build_parts(items_norm: list[dict], alpha: float):
        rendered = []
        for idx, item in enumerate(items_norm[:5]):
            txt = str(item.get("text") or "")
            color = recency_colors[min(idx, len(recency_colors) - 1)]
            surf = _render_compact_text_chip(font_sm, txt.upper(), color, scale, alpha, emphasis=1.10 if idx == 0 else 1.0)
            rendered.append(surf)
        return rendered

    def _draw_parts(parts, base_x: int):
        dx = base_x
        for idx, surf in enumerate(parts):
            if dx > right:
                break
            screen.blit(surf, (dx, y))
            dx += surf.get_width()
            if idx < len(parts) - 1:
                dx += gap

    current_texts = _normalize_texts(items)
    previous_texts = _normalize_texts(prev_texts)

    if not current_texts and not previous_texts:
        empty = font_sm.render(" - ", True, (86, 96, 114))
        screen.blit(empty, (draw_x, y))
        return

    current_parts = _build_parts(current_texts, 1.0)
    previous_parts = _build_parts(previous_texts, max(0.0, min(1.0, slide_progress))) if previous_texts else []

    inserted_shift = 0
    if current_parts:
        inserted_shift = max(current_parts[0].get_width() + gap, int(24 * scale))

    old_clip = screen.get_clip()
    screen.set_clip(clip_rect)
    if slide_progress > 0.001 and previous_parts:
        _draw_parts(previous_parts, draw_x + int(inserted_shift * (1.0 - slide_progress)))
        _draw_parts(current_parts, draw_x - int(inserted_shift * slide_progress))
    else:
        _draw_parts(current_parts, draw_x)
    screen.set_clip(old_clip)


def _draw_compact_ko_badge(screen, font_sm, rect: pygame.Rect, scale: float, alpha: float = 1.0, scale_mul: float = 1.0) -> None:
    alpha = max(0.0, min(1.0, float(alpha)))
    scale_mul = max(0.65, float(scale_mul))
    if alpha <= 0.01:
        return
    w = max(1, int(rect.width * scale_mul))
    h = max(1, int(rect.height * scale_mul))
    draw_rect = pygame.Rect(0, 0, w, h)
    draw_rect.center = rect.center
    radius = max(2, int(3 * scale * scale_mul))
    badge = pygame.Surface((draw_rect.width, draw_rect.height), pygame.SRCALPHA)
    pygame.draw.rect(badge, (54, 16, 22, int(214 * alpha)), badge.get_rect(), border_radius=radius)
    pygame.draw.rect(badge, (255, 92, 104, int(245 * alpha)), badge.get_rect(), 1, border_radius=radius)
    label = font_sm.render("KO", True, (255, 224, 228))
    if scale_mul != 1.0:
        label = pygame.transform.smoothscale(label, (max(1, int(label.get_width() * scale_mul)), max(1, int(label.get_height() * scale_mul))))
    label.set_alpha(int(255 * alpha))
    badge.blit(label, (badge.get_width() // 2 - label.get_width() // 2, badge.get_height() // 2 - label.get_height() // 2))
    screen.blit(badge, draw_rect.topleft)


def _draw_compact_guard_indicator(screen, font_sm, rect: pygame.Rect, label: str, result: str, scale: float, life: float = 1.0, flash: float = 0.0) -> None:
    life = max(0.0, min(1.0, float(life or 0.0)))
    if life <= 0.01 or rect.width <= 2 or rect.height <= 2:
        return

    property_label = str(label or "UNKNOWN").strip().upper() or "UNKNOWN"
    if property_label == "HIGH":
        property_label = "OVERHEAD"
    if property_label in {"UNBLK", "UNBLOCK"}:
        property_label = "UNBLOCKABLE"

    result = str(result or "").strip().upper()
    blocked = result == "BLOCK"
    result_text = "BLOCK" if blocked else "ATK HIT"

    pulse = 0.62 + 0.38 * ((math.sin(time.time() * 16.0) + 1.0) * 0.5) if flash > 0.0 else 1.0
    if blocked:
        base_fill = (22, 48, 34)
        base_border = (
            min(255, int(92 + 36 * pulse)),
            min(255, int(232 + 18 * pulse)),
            min(255, int(146 + 28 * pulse)),
        )
        result_color = (220, 255, 232)
        property_color = (148, 232, 176)
        glow_alpha = int(34 * pulse)
    else:
        base_fill = (72, 20, 26)
        base_border = (
            min(255, int(220 + 35 * pulse)),
            min(255, int(70 + 60 * pulse)),
            min(255, int(82 + 60 * pulse)),
        )
        result_color = (255, 228, 232)
        property_color = (255, 164, 174)
        glow_alpha = int(42 * pulse)

    alpha = max(0, min(255, int(255 * life)))
    badge = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
    radius = max(3, int(4 * scale))
    pygame.draw.rect(badge, (*base_fill, min(230, int(210 * life))), badge.get_rect(), border_radius=radius)
    pygame.draw.rect(badge, (*base_border, alpha), badge.get_rect(), 1, border_radius=radius)
    if glow_alpha > 0:
        pygame.draw.rect(badge, (*base_border, min(255, int(glow_alpha * life))), pygame.Rect(1, 1, max(1, rect.width - 2), max(2, int(2 * scale))), border_top_left_radius=radius, border_top_right_radius=radius)
    else:
        pygame.draw.rect(badge, (92, 232, 146, min(255, int(90 * life))), pygame.Rect(1, 1, max(1, rect.width - 2), max(2, int(2 * scale))), border_top_left_radius=radius, border_top_right_radius=radius)

    max_text_width = rect.width - max(8, int(10 * scale))
    result_surface = font_sm.render(_compact_fit_text(font_sm, result_text, max_text_width), True, result_color)
    property_surface = font_sm.render(_compact_fit_text(font_sm, property_label, max_text_width), True, property_color)
    result_surface.set_alpha(alpha)
    property_surface.set_alpha(alpha)

    total_h = result_surface.get_height() + property_surface.get_height() - max(1, int(2 * scale))
    top_y = max(1, (rect.height - total_h) // 2)
    badge.blit(result_surface, ((rect.width - result_surface.get_width()) // 2, top_y))
    badge.blit(property_surface, ((rect.width - property_surface.get_width()) // 2, top_y + result_surface.get_height() - max(1, int(2 * scale))))
    screen.blit(badge, rect.topleft)


def _wrap_damage_factor_lines(font_sm, factors, max_width: int) -> list[str]:
    """Wrap complete modifier labels without clipping individual factors."""
    tokens = [str(item or "").strip() for item in (factors or [])]
    tokens = [item for item in tokens if item]
    if not tokens:
        tokens = ["BASE"]

    lines: list[str] = []
    current = ""
    separator = "  ·  "
    for token in tokens:
        candidate = token if not current else f"{current}{separator}{token}"
        if font_sm.size(candidate)[0] <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        if font_sm.size(token)[0] <= max_width:
            current = token
        else:
            current = _compact_fit_text(font_sm, token, max_width)
    if current:
        lines.append(current)
    return lines


def _draw_damage_modifier_meter(
    card,
    rect: pygame.Rect,
    percent: float,
    accent: tuple[int, int, int],
    scale: float,
) -> None:
    """Draw a 0 to 200 percent rail with a visible 100 percent baseline."""
    pygame.draw.rect(
        card,
        (24, 31, 43, 235),
        rect,
        border_radius=max(2, rect.height // 2),
    )
    pygame.draw.rect(
        card,
        (67, 82, 104, 210),
        rect,
        1,
        border_radius=max(2, rect.height // 2),
    )

    meter_max_percent = 200.0
    ratio = max(0.0, min(1.0, float(percent) / meter_max_percent))
    fill_w = max(0, min(rect.width, int(round(rect.width * ratio))))
    if fill_w > 0:
        fill_rect = pygame.Rect(rect.x, rect.y, fill_w, rect.height)
        pygame.draw.rect(
            card,
            (*accent, 218),
            fill_rect,
            border_radius=max(2, rect.height // 2),
        )
        highlight_h = max(1, int(2 * scale))
        pygame.draw.rect(
            card,
            (245, 250, 255, 72),
            (fill_rect.x + 1, fill_rect.y + 1, max(1, fill_rect.width - 2), highlight_h),
            border_radius=max(1, highlight_h // 2),
        )

    baseline_x = rect.x + int(round(rect.width * 0.5))
    pygame.draw.line(
        card,
        (232, 239, 248, 205),
        (baseline_x, rect.y - max(1, int(scale))),
        (baseline_x, rect.bottom + max(1, int(scale))),
        max(1, int(scale)),
    )

    marker_x = rect.x + max(0, min(rect.width - 1, fill_w - 1))
    marker_radius = max(2, int(2.5 * scale))
    pygame.draw.circle(
        card,
        (244, 248, 255, 235),
        (marker_x, rect.centery),
        marker_radius,
    )
    pygame.draw.circle(
        card,
        (*accent, 255),
        (marker_x, rect.centery),
        max(1, marker_radius - 1),
    )


def _damage_point_slot(team: str) -> tuple[str, dict] | None:
    """Return the row carrying the stable native point fighter for one team."""
    c1 = f"{team}-C1"
    c2 = f"{team}-C2"
    rows = [
        (c1, _display_slots.get(c1)),
        (c2, _display_slots.get(c2)),
    ]
    rows = [(label, snap) for label, snap in rows if isinstance(snap, dict)]
    if not rows:
        return None

    native = [
        (label, snap)
        for label, snap in rows
        if bool(snap.get("damage_point_active", snap.get("damage_is_point")))
    ]
    if len(native) == 1:
        return native[0]

    # The point flag can briefly overlap during a tag transition. Use the same
    # action-state fallback as the main HUD, but never use +0x44A4 here.
    selected = _get_active_slot(team)
    if selected:
        snap = _display_slots.get(selected)
        if isinstance(snap, dict):
            return selected, snap
    return rows[0]


def _damage_team_owner_slot(team: str, point_slot: str) -> str:
    """Return the team owner row used by the native damage scaler.

    0x80052758 reduces both team members to the same side index. The two dumps
    confirm that +0x11C4/+0x11D8 remain on C1 when C2 tags in, so C1 is the
    owner source while the point row supplies character-specific modifiers.
    """
    owner_slot = f"{team}-C1"
    owner = _display_slots.get(owner_slot)
    return owner_slot if isinstance(owner, dict) else point_slot


def _damage_modifier_badge_rows() -> list[tuple[str, dict, tuple[int, int, int]]]:
    """Build one hybrid native damage row for P1 and P2."""
    p1 = _damage_point_slot("P1")
    p2 = _damage_point_slot("P2")
    if p1 is None or p2 is None:
        return []

    p1_slot, p1_snapshot = p1
    p2_slot, p2_snapshot = p2
    rows: list[tuple[str, dict, tuple[int, int, int]]] = []
    row_specs = (
        ("P1", p1_slot, p1_snapshot, "P2", p2_slot, p2_snapshot, (236, 92, 108)),
        ("P2", p2_slot, p2_snapshot, "P1", p1_slot, p1_snapshot, (82, 164, 236)),
    )
    for team, attacker_slot, attacker, victim_team, victim_slot, victim, accent in row_specs:
        attacker_name = _compact_trim(str(attacker.get("name") or "---"), 16)
        victim_name = _compact_trim(str(victim.get("name") or "---"), 13)
        label = f"{team}  {attacker_name}  >  {victim_team} {victim_name}"
        owner_slot = _damage_team_owner_slot(team, attacker_slot)
        data = build_live_damage_modifier(
            _display_slots,
            attacker_slot,
            victim_slot,
            owner_slot=owner_slot,
        )
        data["source_slot"] = attacker_slot
        data["owner_slot"] = owner_slot
        data["source_base"] = int(attacker.get("base") or 0)
        data["source_id"] = attacker.get("id")
        rows.append((label, data, accent))
    return rows


def _draw_damage_modifier_badge(
    screen,
    font,
    font_sm,
    scale: float,
) -> None:
    """Draw one live point-fighter damage rail for P1 and P2."""
    rows = _damage_modifier_badge_rows()
    if not rows:
        return

    width = max(330, int(382 * scale))
    pad = max(7, int(8 * scale))
    rail_w = max(3, int(4 * scale))
    meter_h = max(7, int(8 * scale))
    row_gap = max(4, int(5 * scale))
    line_gap = max(1, int(2 * scale))
    radius = max(6, int(7 * scale))

    title = font_sm.render("CURRENT DAMAGE MODIFIER  ·  LIVE FIGHTERS", True, (232, 240, 248))
    factor_max_width = width - pad * 2 - rail_w
    prepared_rows = []
    any_approximate = False
    for label, data, accent in rows:
        percent = float(data.get("percent") or 100.0)
        approximate = bool(data.get("approximate", False))
        live = bool(data.get("live", False))
        any_approximate = any_approximate or approximate
        raw_factors = list(data.get("factors") or ["BASE"])
        if not live:
            raw_factors = ["NO LIVE SCALING DATA"]
        factor_lines = _wrap_damage_factor_lines(
            font_sm,
            raw_factors,
            factor_max_width,
        )
        label_s = font_sm.render(label, True, (238, 244, 250))
        value_text = f"{percent:.1f}%" + ("*" if approximate else "")
        value_color = accent if live else (132, 142, 158)
        value_s = font.render(value_text, True, value_color)
        factor_surfaces = [
            font_sm.render(line, True, (164, 184, 207))
            for line in factor_lines
        ]
        header_h = max(label_s.get_height(), value_s.get_height())
        factors_h = sum(surface.get_height() for surface in factor_surfaces)
        factors_h += line_gap * max(0, len(factor_surfaces) - 1)
        row_h = header_h + row_gap + meter_h + row_gap + factors_h + row_gap
        prepared_rows.append(
            {
                "label_s": label_s,
                "value_s": value_s,
                "factor_surfaces": factor_surfaces,
                "percent": percent,
                "accent": accent,
                "row_h": row_h,
                "header_h": header_h,
                "live": live,
            }
        )

    title_top = max(3, int(4 * scale))
    title_bottom = title_top + title.get_height()
    divider_y = title_bottom + max(4, int(5 * scale))
    row_y_start = divider_y + max(4, int(5 * scale))
    footer_h = font_sm.get_height() + max(2, int(3 * scale)) if any_approximate else 0
    height = row_y_start + sum(item["row_h"] for item in prepared_rows) + footer_h + pad
    x = screen.get_width() // 2 - width // 2
    y = int(142 * scale)

    shadow = pygame.Surface((width + 10, height + 10), pygame.SRCALPHA)
    pygame.draw.rect(
        shadow,
        (0, 0, 0, 92),
        (5, 5, width, height),
        border_radius=radius + 2,
    )
    screen.blit(shadow, (x - 5, y + 2))

    card = pygame.Surface((width, height), pygame.SRCALPHA)
    pygame.draw.rect(
        card,
        (10, 14, 21, 232),
        card.get_rect(),
        border_radius=radius,
    )
    pygame.draw.rect(
        card,
        (57, 72, 94, 210),
        card.get_rect(),
        1,
        border_radius=radius,
    )

    card.blit(title, ((width - title.get_width()) // 2, title_top))
    pygame.draw.line(
        card,
        (55, 73, 96, 210),
        (pad, divider_y),
        (width - pad, divider_y),
        1,
    )

    row_y = row_y_start
    for index, item in enumerate(prepared_rows):
        accent = item["accent"]
        row_h = int(item["row_h"])
        header_h = int(item["header_h"])
        label_s = item["label_s"]
        value_s = item["value_s"]
        factor_surfaces = item["factor_surfaces"]
        live = bool(item["live"])

        pygame.draw.rect(
            card,
            (*accent, 28),
            (rail_w, row_y, width - rail_w, row_h),
            border_radius=max(3, int(4 * scale)),
        )
        pygame.draw.rect(
            card,
            (*accent, 235),
            (0, row_y + 2, rail_w, max(1, row_h - 4)),
            border_radius=max(1, rail_w // 2),
        )
        left_x = pad + rail_w
        card.blit(label_s, (left_x, row_y + max(0, (header_h - label_s.get_height()) // 2)))
        card.blit(
            value_s,
            (
                width - pad - value_s.get_width(),
                row_y + max(0, (header_h - value_s.get_height()) // 2),
            ),
        )

        meter_y = row_y + header_h + row_gap
        meter_rect = pygame.Rect(
            left_x,
            meter_y,
            max(1, width - left_x - pad),
            meter_h,
        )
        _draw_damage_modifier_meter(
            card,
            meter_rect,
            float(item["percent"] if live else 0.0),
            accent if live else (104, 112, 126),
            scale,
        )

        factor_y = meter_rect.bottom + row_gap
        for factor_s in factor_surfaces:
            card.blit(factor_s, (left_x, factor_y))
            factor_y += factor_s.get_height() + line_gap

        row_y += row_h
        if index < len(prepared_rows) - 1:
            pygame.draw.line(
                card,
                (42, 54, 72, 170),
                (pad, row_y - max(2, row_gap // 2)),
                (width - pad, row_y - max(2, row_gap // 2)),
                1,
            )

    if any_approximate:
        footer = font_sm.render(
            "* move properties can bypass some factors",
            True,
            (112, 130, 153),
        )
        card.blit(
            footer,
            ((width - footer.get_width()) // 2, height - footer.get_height() - max(3, int(4 * scale))),
        )

    screen.blit(card, (x, y))



def _panel_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        if isinstance(value, str):
            return int(value, 0)
        return int(value)
    except Exception:
        return int(default)


def _panel_hex(value) -> str:
    if value is None or value == "":
        return "--------"
    return f"{_panel_int(value) & 0xFFFFFFFF:08X}"


def _panel_fit(font, text: str, max_width: int) -> pygame.Surface:
    text = str(text or "")
    color = (174, 190, 211)
    rendered = font.render(text, True, color)
    if rendered.get_width() <= max_width:
        return rendered
    suffix = "..."
    while text and font.size(text + suffix)[0] > max_width:
        text = text[:-1]
    return font.render((text.rstrip() + suffix) if text else "", True, color)


def _draw_research_shell(
    screen,
    rect: pygame.Rect,
    title: str,
    accent: tuple[int, int, int],
    font_sm,
) -> pygame.Surface:
    radius = max(6, int(7 * min(rect.width / 360.0, 1.0)))
    shadow = pygame.Surface((rect.width + 10, rect.height + 10), pygame.SRCALPHA)
    pygame.draw.rect(shadow, (0, 0, 0, 92), (5, 5, rect.width, rect.height), border_radius=radius + 2)
    screen.blit(shadow, (rect.x - 5, rect.y + 2))
    card = pygame.Surface(rect.size, pygame.SRCALPHA)
    pygame.draw.rect(card, (9, 14, 22, 235), card.get_rect(), border_radius=radius)
    pygame.draw.rect(card, (*accent, 205), card.get_rect(), 1, border_radius=radius)
    pygame.draw.rect(card, (*accent, 220), (0, 0, max(3, int(4)), rect.height), border_radius=2)
    title_s = font_sm.render(title, True, (232, 240, 248))
    card.blit(title_s, (10, 7))
    pygame.draw.line(card, (48, 63, 82, 205), (9, 7 + title_s.get_height() + 5), (rect.width - 9, 7 + title_s.get_height() + 5), 1)
    return card


def _team_point_snapshot(team: str) -> tuple[str, dict] | tuple[None, None]:
    selected = _damage_point_slot(team)
    if selected is not None:
        return selected
    for slot in (f"{team}-C1", f"{team}-C2"):
        snap = _display_slots.get(slot)
        if isinstance(snap, dict):
            return slot, snap
    return None, None


def _draw_meter_generation_panel(screen, font, font_sm, scale: float) -> None:
    width = max(350, int(390 * scale))
    row_h = max(66, int(70 * scale))
    title_h = font_sm.get_height() + 21
    height = title_h + row_h * 2 + max(9, int(10 * scale))
    x = screen.get_width() // 2 - width // 2
    y = screen.get_height() - height - max(14, int(18 * scale))
    rect = pygame.Rect(x, max(int(410 * scale), y), width, height)
    card = _draw_research_shell(screen, rect, "METER GENERATION", (67, 201, 156), font_sm)
    content_y = title_h

    for index, team in enumerate(("P1", "P2")):
        slot, snap = _team_point_snapshot(team)
        snap = snap if isinstance(snap, dict) else {}
        accent = (236, 92, 108) if team == "P1" else (82, 164, 236)
        row_y = content_y + index * row_h
        if index:
            pygame.draw.line(card, (42, 56, 73, 170), (10, row_y), (width - 10, row_y), 1)

        current = _panel_int(snap.get("meter_profile_current", snap.get("meter")), 0)
        current = max(0, min(50000, current))
        delta = _panel_int(snap.get("meter_profile_last_delta"), 0)
        predicted_raw = snap.get("meter_profile_last_predicted")
        predicted = None if predicted_raw is None else _panel_int(predicted_raw)
        match = snap.get("meter_profile_last_match")
        role = str(snap.get("meter_profile_last_role") or "").upper()
        source = str(snap.get("meter_profile_last_source") or "")
        move = str(snap.get("meter_profile_last_move") or "")
        name = _compact_trim(str(snap.get("name") or slot or "---"), 14)

        header = font_sm.render(f"{team}  {name}", True, accent)
        value = font.render(f"{current / 10000.0:.2f} BAR", True, (232, 240, 248))
        card.blit(header, (12, row_y + 5))
        card.blit(value, (width - 12 - value.get_width(), row_y + 3))

        bar_x = 12
        bar_y = row_y + 25
        bar_w = width - 24
        bar_h = max(7, int(8 * scale))
        pygame.draw.rect(card, (24, 34, 47), (bar_x, bar_y, bar_w, bar_h), border_radius=3)
        fill_w = int(bar_w * (current / 50000.0))
        if fill_w > 0:
            pygame.draw.rect(card, accent, (bar_x, bar_y, fill_w, bar_h), border_radius=3)
        for pip in range(1, 5):
            px = bar_x + int(bar_w * pip / 5.0)
            pygame.draw.line(card, (9, 14, 22), (px, bar_y), (px, bar_y + bar_h), 1)

        if delta:
            prediction_text = ""
            if predicted is not None:
                state = "MATCH" if match is True else ("MISS" if match is False else "PRED")
                prediction_text = f"  PRED {predicted:+d} {state}"
            detail = f"LAST {delta:+d}  {role or 'UNKNOWN'}{prediction_text}"
        else:
            detail = "NO METER TRANSITION CAPTURED"
        detail_s = _panel_fit(font_sm, detail, width - 24)
        card.blit(detail_s, (12, bar_y + bar_h + 4))
        if source or move:
            source_line = "  ".join(part for part in (source, move) if part)
            source_s = _panel_fit(font_sm, source_line, width - 24)
            source_s.set_alpha(190)
            card.blit(source_s, (12, bar_y + bar_h + 4 + font_sm.get_height()))

    screen.blit(card, rect.topleft)


def _draw_red_health_panel(screen, font, font_sm, scale: float) -> None:
    width = max(370, int(410 * scale))
    row_h = max(47, int(51 * scale))
    title_h = font_sm.get_height() + 21
    height = title_h + row_h * 4 + max(8, int(9 * scale))
    rect = pygame.Rect(max(12, int(16 * scale)), screen.get_height() - height - max(14, int(18 * scale)), width, height)
    card = _draw_research_shell(screen, rect, "RECOVERABLE HEALTH", (223, 82, 102), font_sm)
    content_y = title_h

    for index, slot in enumerate(("P1-C1", "P1-C2", "P2-C1", "P2-C2")):
        snap = _display_slots.get(slot)
        snap = snap if isinstance(snap, dict) else {}
        row_y = content_y + index * row_h
        if index:
            pygame.draw.line(card, (42, 56, 73, 160), (10, row_y), (width - 10, row_y), 1)
        accent = SLOT_COLORS.get(slot, (180, 180, 190))
        name = _compact_trim(str(snap.get("name") or "---"), 12)
        point = bool(snap.get("red_health_point", snap.get("damage_point_active", False)))
        current = _panel_int(snap.get("red_health_current", snap.get("cur")), 0)
        maximum = max(1, _panel_int(snap.get("max"), 1))
        auxiliary = _panel_int(snap.get("red_health_aux", snap.get("recoverable_ceiling")), current)
        red = max(0, _panel_int(snap.get("red_health_recoverable", snap.get("recoverable_hp")), auxiliary - current))
        red_pct = float(snap.get("red_health_pct_max", snap.get("recoverable_pct_max", 0.0)) or 0.0)
        status = "POINT" if point else "RESERVE"
        if _panel_int(snap.get("cur"), 0) <= 0:
            status = "KO"
        header = font_sm.render(f"{slot}  {name}  {status}", True, accent if status != "KO" else (132, 140, 151))
        value = font_sm.render(f"HP {current}  AUX {auxiliary}  RED {red} ({red_pct:.1f}%)", True, (232, 240, 248))
        card.blit(header, (12, row_y + 4))
        card.blit(value, (width - 12 - value.get_width(), row_y + 4))

        bar_x = 12
        bar_y = row_y + 23
        bar_w = width - 24
        bar_h = max(7, int(8 * scale))
        pygame.draw.rect(card, (24, 34, 47), (bar_x, bar_y, bar_w, bar_h), border_radius=3)
        aux_w = int(bar_w * max(0.0, min(1.0, auxiliary / float(maximum))))
        cur_w = int(bar_w * max(0.0, min(1.0, current / float(maximum))))
        if aux_w > 0:
            pygame.draw.rect(card, (150, 55, 74), (bar_x, bar_y, aux_w, bar_h), border_radius=3)
        if cur_w > 0:
            pygame.draw.rect(card, accent, (bar_x, bar_y, cur_w, bar_h), border_radius=3)

        event = str(snap.get("red_health_last_event") or "")
        pending_cur = _panel_int(snap.get("red_health_pending_current"), 0)
        pending_aux = _panel_int(snap.get("red_health_pending_aux"), 0)
        if event:
            red_delta = _panel_int(snap.get("red_health_last_red_delta"), 0)
            detail = f"{event.replace('_', ' ').upper()}  RED {red_delta:+d}  Q {pending_cur:+d}/{pending_aux:+d}"
        else:
            detail = f"QUEUE {pending_cur:+d}/{pending_aux:+d}  SYNC {_panel_int(snap.get('red_health_heal_sync'), 0)}"
        detail_s = _panel_fit(font_sm, detail, width - 24)
        detail_s.set_alpha(195)
        card.blit(detail_s, (12, bar_y + bar_h + 3))

    screen.blit(card, rect.topleft)


def _active_attack_snapshot(team: str) -> tuple[str, dict] | tuple[None, None]:
    candidates = []
    for slot in (f"{team}-C1", f"{team}-C2"):
        snap = _display_slots.get(slot)
        if not isinstance(snap, dict):
            continue
        count = _panel_int(snap.get("attack_property_packet_count"), 0)
        actor = _panel_int(snap.get("attack_property_live_actor"), 0)
        display_active = bool(snap.get("attack_property_display_active")) or bool(actor)
        source = str(snap.get("attack_property_display_source") or snap.get("attack_property_packet_source") or "")
        source_rank = 3 if actor else (2 if source == "move_definition" else (1 if display_active else 0))
        capture_frame = _panel_int(snap.get("attack_property_packet_capture_frame"), -1)
        sequence = _panel_int(snap.get("attack_property_event_sequence"), 0)
        candidates.append((source_rank, capture_frame, sequence, count, slot, snap))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
        _rank, _capture, _sequence, _count, slot, snap = candidates[0]
        return slot, snap
    return None, None


def _draw_attack_property_panel(screen, font, font_sm, scale: float) -> None:
    width = max(380, int(420 * scale))
    row_h = max(91, int(96 * scale))
    title_h = font_sm.get_height() + 21
    height = title_h + row_h * 2 + max(8, int(9 * scale))
    rect = pygame.Rect(screen.get_width() - width - max(12, int(16 * scale)), screen.get_height() - height - max(14, int(18 * scale)), width, height)
    card = _draw_research_shell(screen, rect, "ATTACK PROPERTIES", (158, 109, 216), font_sm)
    content_y = title_h

    for index, team in enumerate(("P1", "P2")):
        slot, snap = _active_attack_snapshot(team)
        snap = snap if isinstance(snap, dict) else {}
        row_y = content_y + index * row_h
        if index:
            pygame.draw.line(card, (42, 56, 73, 170), (10, row_y), (width - 10, row_y), 1)
        accent = (236, 92, 108) if team == "P1" else (82, 164, 236)
        actor = _panel_int(snap.get("attack_property_live_actor"), 0)
        display_active = bool(snap.get("attack_property_display_active")) or bool(actor)
        name = _compact_trim(str(snap.get("name") or slot or "---"), 13)
        packet_move = str(snap.get("attack_property_packet_action_name") or "")
        move = _compact_trim(packet_move or str(snap.get("final_move_label") or snap.get("mv_label_display") or snap.get("mv_label") or "---"), 22)
        packet_state = str(snap.get("attack_property_packet_state") or "NONE").upper()
        header = font_sm.render(f"{team}  {packet_state}  {name}  {move}", True, accent)
        card.blit(header, (12, row_y + 5))

        if not display_active:
            status = str(snap.get("attack_property_definition_status") or "WAITING").upper().replace("_", " ")
            error = str(snap.get("attack_property_definition_error") or "").strip()
            action_id = _panel_int(snap.get("attack_property_definition_action_id") or snap.get("mv_id_display") or snap.get("attA"), 0)
            empty = font.render("NO PROPERTY FOR CURRENT ACTION", True, (123, 137, 157))
            card.blit(empty, (12, row_y + 28))
            detail_text = f"{status}  ACTION {action_id:04X}" if action_id else status
            if error:
                detail_text += f"  {error}"
            detail = _panel_fit(font_sm, detail_text, width - 24)
            card.blit(detail, (12, row_y + 54))
            continue

        source = str(snap.get("attack_property_display_source") or snap.get("attack_property_packet_source") or "")
        if source in {"move_definition", "move_definition_latched"}:
            phases = _attack_property_phase_rows(snap)
            phase_groups = _attack_property_phase_groups(phases)
            first = phase_groups[0] if phase_groups else {}
            state_text = "CURRENT NATIVE SCRIPT" if source == "move_definition" else "LAST NATIVE SCRIPT"
            action_id = _panel_int(snap.get("attack_property_packet_action_id"), 0)
            primary = "  ".join(text for text, _color in _attack_phase_primary_tokens(first))
            secondary = "  ".join(text for text, _color in _attack_phase_secondary_tokens(first))
            count_text = f"{len(phases)} BLOCKS"
            if len(phase_groups) != len(phases):
                count_text += f" / {len(phase_groups)} UNIQUE"
            line1 = _panel_fit(font_sm, f"{state_text}  ACTION {action_id:04X}  {count_text}", width - 24)
            line2 = _panel_fit(font_sm, primary, width - 24)
            line3 = _panel_fit(font_sm, secondary, width - 24)
            card.blit(line1, (12, row_y + 27))
            line2.set_alpha(220)
            card.blit(line2, (12, row_y + 47))
            line3.set_alpha(195)
            card.blit(line3, (12, row_y + 67))
            continue


        damage = _panel_int(snap.get("attack_property_live_damage"), 0)
        status20 = _panel_hex(snap.get("attack_property_live_status20"))
        prop_a = _panel_hex(snap.get("attack_property_live_a"))
        prop_b = _panel_hex(snap.get("attack_property_live_b"))
        text_a = str(snap.get("attack_property_live_a_text") or "UNRESOLVED")
        text_b = str(snap.get("attack_property_live_b_text") or "UNRESOLVED")
        scaling = str(snap.get("attack_property_live_scaling_track") or "Combat lane unknown")
        victim = str(snap.get("attack_property_live_victim_slot") or "-")
        phase_a = _panel_int(snap.get("attack_property_live_phase_a"), 0)
        phase_b = _panel_int(snap.get("attack_property_live_phase_b"), 0)
        line1 = f"ACTOR {actor:08X}  DMG {damage}  VICTIM {victim}  STATUS {status20}"
        line2 = f"A {prop_a}  {text_a}"
        line3 = f"B {prop_b}  {text_b}  |  {scaling}"
        if phase_a or phase_b:
            line3 += f"  |  NEXT A {phase_a:08X} B {phase_b:08X}"
        for offset, line in enumerate((line1, line2, line3)):
            surf = _panel_fit(font_sm, line, width - 24)
            if offset:
                surf.set_alpha(205)
            card.blit(surf, (12, row_y + 27 + offset * (font_sm.get_height() + 2)))

    screen.blit(card, rect.topleft)



def _draw_compact_damage_scaling_rows(
    screen,
    font_sm,
    team: str,
    point_label: str,
    point: dict,
    partner_label: str,
    partner: dict,
    x: int,
    y: int,
    right: int,
    scale: float,
    dt: float,
) -> None:
    """Draw adaptive team damage scaling on one compact telemetry rail.

    Neutral 100% slots collapse to text-only chips. If only one character has
    meaningful scaling, that slot gets the bar width while the neutral partner
    stays compact. This spends horizontal space instead of stacking more rows.
    """
    opponent_team = "P2" if team == "P1" else "P1"
    victim = _damage_point_slot(opponent_team)
    rail_h = max(font_sm.get_height() + 4, int(17 * scale))
    rail = pygame.Rect(x, y, max(80, right - x), rail_h)
    radius = max(3, int(4 * scale))
    pygame.draw.rect(screen, (10, 14, 21, 220), rail, border_radius=radius)
    pygame.draw.rect(screen, (61, 76, 98, 105), rail, 1, border_radius=radius)

    label_color = (151, 164, 184)
    label = font_sm.render("DMG SCALE", True, label_color)
    pad = max(5, int(6 * scale))
    screen.blit(label, (rail.x + pad, rail.centery - label.get_height() // 2))
    content_x = rail.x + pad + label.get_width() + max(7, int(9 * scale))
    content_right = rail.right - pad
    gap = max(5, int(6 * scale))

    rows = sorted(
        ((point_label, point), (partner_label, partner)),
        key=lambda item: 0 if item[0].endswith("C1") else 1,
    )
    cells = []
    for slot_label, snap in rows:
        badge = "C1" if slot_label.endswith("C1") else "C2"
        is_point = bool(snap.get("damage_point_active", snap.get("damage_is_point")))
        slot_accent = SLOT_COLORS.get(slot_label, (190, 195, 208))

        if victim is None:
            percent = 100.0
            value_text = "--"
            live = False
            gauge_color = (91, 102, 119)
        else:
            victim_slot, _victim_snapshot = victim
            owner_slot = _damage_team_owner_slot(team, slot_label)
            data = build_live_damage_modifier(_display_slots, slot_label, victim_slot, owner_slot=owner_slot)
            percent = float(data.get("percent") or 100.0)
            approximate = bool(data.get("approximate", False))
            live = bool(data.get("live", False))
            value_text = f"{percent:.1f}%" + ("*" if approximate else "")
            if not live:
                gauge_color = (91, 102, 119)
            elif percent < 99.95:
                gauge_color = (235, 91, 108)
            elif percent > 100.05:
                gauge_color = (232, 188, 83)
            else:
                gauge_color = (122, 183, 198)

        slot_anim = _get_slot_anim(slot_label)
        previous_target = slot_anim.get("damage_scale_target_pct")
        if previous_target is None or abs(float(previous_target) - float(percent)) > 0.01:
            slot_anim["damage_scale_target_pct"] = float(percent)
            if live:
                slot_anim["damage_scale_pulse"] = 1.0
        visual_percent = _ease_visual(slot_anim.get("damage_scale_visual_pct"), float(percent), 30.0, dt)
        slot_anim["damage_scale_visual_pct"] = visual_percent
        slot_anim["damage_scale_pulse"] = max(0.0, float(slot_anim.get("damage_scale_pulse", 0.0)) - max(0.0, dt) * 4.8)

        deviated = bool(live and abs(percent - 100.0) > 0.05)
        cells.append({
            "slot_label": slot_label,
            "badge": badge,
            "is_point": is_point,
            "slot_accent": slot_accent,
            "percent": percent,
            "visual_percent": visual_percent,
            "value_text": value_text,
            "live": live,
            "gauge_color": gauge_color,
            "deviated": deviated,
            "pulse": float(slot_anim.get("damage_scale_pulse", 0.0)),
        })

    # When scaling is neutral there is no reason to spend pixels drawing two
    # empty 100%-centered gauges. Keep both exact values, but collapse them to
    # quiet text chips until one side actually deviates.
    active_indices = [i for i, item in enumerate(cells) if item["deviated"]]
    if not active_indices:
        idle_tint = pygame.Surface((rail.width, rail.height), pygame.SRCALPHA)
        idle_tint.fill((5, 8, 13, 34))
        screen.blit(idle_tint, rail.topleft)
        total_w = max(20, content_right - content_x)
        half = total_w // 2
        for i, item in enumerate(cells):
            cx = content_x + i * half
            cw = half if i == 0 else total_w - half
            badge_color = item["slot_accent"] if item["is_point"] else (125, 134, 150)
            value_color = (126, 140, 160) if item["value_text"] != "--" else (82, 94, 112)
            badge_s = font_sm.render(item["badge"], True, badge_color)
            value_s = font_sm.render(item["value_text"], True, value_color)
            screen.blit(badge_s, (cx + 2, rail.centery - badge_s.get_height() // 2))
            screen.blit(value_s, (cx + cw - value_s.get_width() - 2, rail.centery - value_s.get_height() // 2))
            if i == 0:
                sep_x = content_x + half - gap // 2
                pygame.draw.line(screen, (42, 52, 66), (sep_x, rail.y + 4), (sep_x, rail.bottom - 4), 1)
        return

    avail = max(40, content_right - content_x)
    compact_w = max(font_sm.size("C2 100.0%")[0] + max(7, int(9 * scale)), int(58 * scale))
    if len(active_indices) == 1 and avail > compact_w * 2 + gap:
        active_index = active_indices[0]
        wide_w = max(24, avail - gap - compact_w)
        widths = [compact_w, compact_w]
        widths[active_index] = wide_w
    else:
        first = max(20, (avail - gap) // 2)
        widths = [first, max(20, avail - gap - first)]

    cursor = content_x
    for index, item in enumerate(cells):
        cell = pygame.Rect(cursor, rail.y + 1, widths[index], max(1, rail.height - 2))
        cursor += widths[index] + gap
        if index:
            sep_x = cell.x - gap // 2
            pygame.draw.line(screen, (47, 59, 75), (sep_x, rail.y + 4), (sep_x, rail.bottom - 4), 1)

        badge_color = item["slot_accent"] if item["is_point"] else (125, 134, 150)
        badge_s = font_sm.render(item["badge"], True, badge_color)
        pulse = max(0.0, min(1.0, float(item.get("pulse", 0.0))))
        value_color = _lerp_color(item["gauge_color"], (255, 255, 255), pulse * 0.78)
        value_s = font_sm.render(item["value_text"], True, value_color)
        screen.blit(badge_s, (cell.x + 2, cell.centery - badge_s.get_height() // 2))
        screen.blit(value_s, (cell.right - value_s.get_width() - 2, cell.centery - value_s.get_height() // 2))

        # A neutral partner collapses to its identity + value while the active
        # scaler consumes the remaining horizontal runway.
        if not item["deviated"] and len(active_indices) == 1:
            continue

        gx = cell.x + badge_s.get_width() + max(5, int(6 * scale))
        gr = cell.right - value_s.get_width() - max(5, int(6 * scale))
        gw = max(12, gr - gx)
        gh = max(4, int(5 * scale))
        gy = cell.centery - gh // 2
        gauge = pygame.Rect(gx, gy, gw, gh)
        pygame.draw.rect(screen, (37, 44, 56), gauge, border_radius=max(2, gh // 2))
        pygame.draw.rect(screen, (100, 114, 136), gauge, 1, border_radius=max(2, gh // 2))
        pygame.draw.line(screen, (235, 240, 247), (gauge.x + 1, gauge.y + 1), (gauge.right - 2, gauge.y + 1), 1)
        base_x = gx + gw // 2
        pygame.draw.line(screen, (152, 164, 184), (base_x, gy - 2), (base_x, gy + gh + 1), 1)
        if item["live"]:
            ratio = max(0.0, min(1.0, item["visual_percent"] / 200.0))
            marker_x = gx + int(round(gw * ratio))
            fill = pygame.Rect(min(base_x, marker_x), gy + 1, max(1, abs(marker_x - base_x)), max(1, gh - 2))
            _draw_vertical_gradient(screen, fill, _hud_brighten(item["gauge_color"], 14), _hud_darken(item["gauge_color"], 8), 255)
            pygame.draw.line(screen, (250, 250, 250), (fill.x, fill.y), (fill.right - 1, fill.y), 1)
            pulse = max(0.0, min(1.0, float(item.get("pulse", 0.0))))
            if pulse > 0.01:
                halo_r = max(2, int((2.0 + 2.5 * pulse) * scale))
                pygame.draw.circle(screen, _hud_brighten(item["gauge_color"], 42), (marker_x, cell.centery), halo_r, 1)
            pygame.draw.line(screen, _hud_brighten(item["gauge_color"], int(28 * pulse)), (marker_x, gy - 2), (marker_x, gy + gh + 1), max(1, int(1 + pulse)))

def _realtime_hs_contact_clock(slot_anim: dict, snap: dict) -> dict | None:
    """Return the current per-hit clock from native victim counters.

    The HP edge creates a generation, but the countdown itself follows the
    victim's +0x1210 hitstun or +0x1220 untech timer. This means hitstop pauses
    the HUD exactly when the game pauses the underlying counter.
    """
    event = snap.get("realtime_hs_contact") if isinstance(snap, dict) else None
    if not isinstance(event, dict):
        return None
    try:
        generation = max(0, int(event.get("generation", 0) or 0))
        target = max(0, int(event.get("target", 0) or 0))
        source = str(event.get("clock_source") or "")
        raw = max(target, int(event.get("raw_estimate", target) or target))
        loss = max(0, int(event.get("decay_frames", 0) or 0))
    except Exception:
        return None
    if generation <= 0 or target <= 0 or source not in {"hitstun", "untech"}:
        return None

    previous_generation = int(slot_anim.get("hs_contact_generation", 0) or 0)
    source_promoted = bool(event.get("source_promoted", False))
    if generation != previous_generation:
        # A new hit is a hard reset, even when it lands during the previous
        # hit's still-nonzero countdown.
        slot_anim["stun_generation_flash"] = 1.0
        slot_anim["hs_contact_generation"] = generation
        slot_anim["hs_contact_target"] = target
        slot_anim["hs_contact_raw"] = raw
        slot_anim["hs_contact_loss"] = loss
        slot_anim["hs_contact_source"] = source
        slot_anim["hs_contact_move"] = str(event.get("move_label") or "")
        slot_anim["hs_contact_remaining"] = target
    elif source_promoted and source == "untech" and str(slot_anim.get("hs_contact_source") or "") != "untech":
        # Same physical hit, later authoritative air-recovery lane.  Switch to
        # +0x1220 immediately rather than waiting for another contact.
        slot_anim["hs_contact_target"] = target
        slot_anim["hs_contact_raw"] = raw
        slot_anim["hs_contact_loss"] = loss
        slot_anim["hs_contact_source"] = source
        slot_anim["hs_contact_remaining"] = target

    target = max(0, int(slot_anim.get("hs_contact_target", target) or 0))
    raw = max(target, int(slot_anim.get("hs_contact_raw", raw) or raw))
    loss = max(0, int(slot_anim.get("hs_contact_loss", loss) or loss))
    source = str(slot_anim.get("hs_contact_source") or source)

    if source == "untech":
        current_raw = event.get("native_untech_current", event.get("native_untech", target))
    else:
        current_raw = event.get("native_hitstun_current", event.get("native_hitstun", target))
    try:
        current = max(0, min(target, int(current_raw or 0)))
    except Exception:
        current = max(0, int(slot_anim.get("hs_contact_remaining", target) or target))

    # A same-generation clock may only drain. New hits use a new generation,
    # while the victim state above is always the newest monotonic native sample.
    previous_remaining = max(0, min(target, int(slot_anim.get("hs_contact_remaining", target) or target)))
    remaining = min(previous_remaining, current)
    if previous_remaining > 0 and remaining <= 0:
        slot_anim["stun_expire_flash"] = 1.0
    slot_anim["hs_contact_remaining"] = remaining
    elapsed = max(0, target - remaining)
    return {
        # Separate namespace from the slower manager-side latch generation so
        # a realtime hit always hard-resets the visual sweep immediately.
        "generation": 1_000_000 + generation,
        "target": target,
        "elapsed": elapsed,
        "remaining": remaining,
        "raw": raw,
        "loss": loss,
        "clock_source": source,
        "active": remaining > 0,
        "expired": remaining <= 0,
        "move_label": str(slot_anim.get("hs_contact_move") or ""),
    }

def _realtime_blockstun_contact_clock(slot_anim: dict, snap: dict) -> dict | None:
    """Return the frame-step-locked native +0x1204 blockstun clock."""
    event = snap.get("realtime_blockstun_contact") if isinstance(snap, dict) else None
    if not isinstance(event, dict):
        return None
    try:
        generation = max(0, int(event.get("generation", 0) or 0))
        target = max(0, int(event.get("target", 0) or 0))
    except Exception:
        return None
    if generation <= 0 or target <= 0:
        return None

    if generation != int(slot_anim.get("bs_contact_generation", 0) or 0):
        slot_anim["bs_generation_flash"] = 1.0
        slot_anim["bs_contact_generation"] = generation
        slot_anim["bs_contact_target"] = target
        slot_anim["bs_contact_remaining"] = target
        slot_anim["bs_contact_move"] = str(event.get("move_label") or "")

    target = max(0, int(slot_anim.get("bs_contact_target", target) or 0))
    try:
        current = max(0, min(target, int(event.get("native_blockstun_current", event.get("native_blockstun", target)) or 0)))
    except Exception:
        current = max(0, int(slot_anim.get("bs_contact_remaining", target) or target))

    # Same-generation blockstun can only drain. A new blocked move gets a new
    # generation from the manager and therefore hard-resets even if its total is
    # shorter than the old move's remaining blockstun.
    previous_remaining = max(0, min(target, int(slot_anim.get("bs_contact_remaining", target) or target)))
    remaining = min(previous_remaining, current)
    if previous_remaining > 0 and remaining <= 0:
        slot_anim["bs_expire_flash"] = 1.0
    slot_anim["bs_contact_remaining"] = remaining
    return {
        "generation": 2_000_000 + generation,
        "target": target,
        "remaining": remaining,
        "elapsed": max(0, target - remaining),
        "active": remaining > 0,
        "expired": remaining <= 0,
        "move_label": str(slot_anim.get("bs_contact_move") or ""),
    }


def _hs_visual_elapsed(slot_anim: dict, generation: int, elapsed: int, target: int, dt: float) -> float:
    """Return the exact native elapsed frame count for HS geometry.

    This bar is intentionally game-frame locked. Dolphin pause/frame-step must
    freeze the entire visual, so wall-clock ``dt`` is never allowed to advance
    the fill between native +0x1210/+0x1220 counter changes.
    """
    del dt
    global _frame
    generation = max(0, int(generation or 0))
    target = max(0, int(target or 0))
    exact = float(max(0, min(target, int(elapsed or 0)))) if target > 0 else 0.0

    slot_anim["hs_visual_generation"] = generation
    slot_anim["hs_visual_target"] = target
    slot_anim["hs_visual_elapsed"] = exact
    slot_anim["hs_visual_last_frame"] = _frame
    return exact


# Stun-clock identity colors. Keep these distinct so the rows can be read at
# a glance without relying on the text label:
#   hitstun = blue/cyan, untech = purple, blockstun = orange.
STUN_CLOCK_HIT_COLOR = (122, 183, 198)
STUN_CLOCK_HIT_HEAD = (218, 246, 252)
STUN_CLOCK_UNTECH_COLOR = (167, 151, 220)
STUN_CLOCK_UNTECH_HEAD = (236, 230, 252)
STUN_CLOCK_BLOCK_COLOR = (235, 136, 91)
STUN_CLOCK_BLOCK_HEAD = (252, 224, 202)


def _draw_compact_untech_scaling_row(
    screen,
    font_sm,
    team: str,
    snap: dict,
    x: int,
    y: int,
    right: int,
    scale: float,
    dt: float,
    *,
    move_prediction: dict | None = None,
) -> None:
    """Draw adaptive native hit/untech and blockstun clocks on one rail.

    Active clocks consume the runway. An inactive sibling collapses to a tiny
    text chip, and a fully neutral rail becomes a quiet READY state. Native
    frame-lock semantics are unchanged.
    """
    slot_label = _get_active_slot(team) or f"{team}-C1"
    slot_anim = _get_slot_anim(slot_label)
    rail_h = max(font_sm.get_height() + 4, int(17 * scale))
    rail = pygame.Rect(x, y, max(80, right - x), rail_h)
    radius = max(3, int(4 * scale))
    pygame.draw.rect(screen, (10, 14, 21, 220), rail, border_radius=radius)
    pygame.draw.rect(screen, (61, 76, 98, 105), rail, 1, border_radius=radius)

    pad = max(5, int(6 * scale))
    brand = font_sm.render("STUN", True, (151, 164, 184))
    screen.blit(brand, (rail.x + pad, rail.centery - brand.get_height() // 2))
    content_x = rail.x + pad + brand.get_width() + max(7, int(9 * scale))
    content_right = rail.right - pad
    gap = max(5, int(6 * scale))

    # Hitstun / untech native clock. Direct realtime contact wins, with the
    # slower manager latch retained only as a compatibility fallback.
    live = bool(snap.get("hitstun_decay_live", False))
    rule = snap.get("hitstun_decay_rule_enabled")
    target = max(0, int(snap.get("hitstun_untech_expiry_target", snap.get("hitstun_untech_effective_start")) or 0))
    loss = max(0, int(snap.get("hitstun_untech_latched_loss", snap.get("hitstun_decay_frames")) or 0)) if target > 0 else max(0, int(snap.get("hitstun_decay_frames") or 0))
    elapsed = max(0, min(target, int(snap.get("hitstun_untech_elapsed") or 0))) if target > 0 else 0
    base_est = max(target, int(snap.get("hitstun_untech_base_estimate") or 0))
    generation = max(0, int(snap.get("hitstun_untech_generation") or 0))
    active = bool(snap.get("hitstun_untech_active", False))
    cantukemi = bool(snap.get("hitstun_cantukemi", False))
    clock_source = str(snap.get("hitstun_clock_source") or "untech")
    contact_clock = _realtime_hs_contact_clock(slot_anim, snap)
    if contact_clock is not None:
        live = True
        target = int(contact_clock["target"])
        loss = int(contact_clock["loss"])
        elapsed = int(contact_clock["elapsed"])
        base_est = max(target, int(contact_clock["raw"]))
        generation = int(contact_clock["generation"])
        active = bool(contact_clock["active"])
        clock_source = str(contact_clock.get("clock_source") or "hitstun")
        cantukemi = False

    slot_anim["hs_visual_signature"] = (generation, base_est, target, loss, bool(cantukemi), bool(live))
    remaining = max(0, target - elapsed) if target > 0 else 0
    if cantukemi:
        hit_label = "AIR HS"
        hit_value = "NO TECH"
        hit_color = (235, 91, 108)
    elif live and target > 0:
        if clock_source == "hitstun":
            hit_label = "HS"
            hit_color = STUN_CLOCK_HIT_COLOR
        else:
            hit_label = f"AIR HS -{loss}" if loss > 0 else "AIR HS"
            hit_color = STUN_CLOCK_UNTECH_COLOR
        hit_value = f"{remaining}/{target}"
    elif rule is False:
        hit_label, hit_value, hit_color = "HS", "--", (91, 102, 119)
    else:
        hit_label, hit_value, hit_color = "AIR HS", "--", (91, 102, 119)

    punish_block_clock = _realtime_punish_blockstun_contact_clock(slot_anim, snap)
    block_clock = punish_block_clock if punish_block_clock is not None else _realtime_blockstun_contact_clock(slot_anim, snap)
    if block_clock is None:
        block_target = 0
        block_remaining = 0
        block_value = "--"
        block_color = (91, 102, 119)
    else:
        block_target = max(0, int(block_clock.get("target", 0) or 0))
        block_remaining = max(0, min(block_target, int(block_clock.get("remaining", 0) or 0)))
        block_value = f"{block_remaining}/{block_target}"
        block_color = STUN_CLOCK_BLOCK_COLOR if block_remaining > 0 else (151, 164, 184)

    hit_active = bool(cantukemi or (live and target > 0 and remaining > 0))
    block_active = block_remaining > 0

    if not hit_active and not block_active:
        idle_tint = pygame.Surface((rail.width, rail.height), pygame.SRCALPHA)
        idle_tint.fill((5, 8, 13, 38))
        screen.blit(idle_tint, rail.topleft)
        hit_expire = max(0.0, min(1.0, float(slot_anim.get("stun_expire_flash", 0.0))))
        block_expire = max(0.0, min(1.0, float(slot_anim.get("bs_expire_flash", 0.0))))
        if hit_expire > 0.01 and target > 0:
            exp_label = font_sm.render(f"{hit_label} 0/{target}", True, _lerp_color((151, 164, 184), (246, 250, 255), hit_expire))
            screen.blit(exp_label, (content_x, rail.centery - exp_label.get_height() // 2))
            collapse_w = max(3, int((content_right - content_x) * 0.18 * hit_expire))
            pygame.draw.rect(screen, (238, 244, 250, int(150 * hit_expire)), (content_right - collapse_w, rail.centery - 2, collapse_w, 4), border_radius=2)
        elif block_expire > 0.01 and block_target > 0:
            exp_label = font_sm.render(f"BS 0/{block_target}", True, _lerp_color((151, 164, 184), (255, 232, 214), block_expire))
            screen.blit(exp_label, (content_x, rail.centery - exp_label.get_height() // 2))
            collapse_w = max(3, int((content_right - content_x) * 0.18 * block_expire))
            pygame.draw.rect(screen, (252, 224, 202, int(150 * block_expire)), (content_right - collapse_w, rail.centery - 2, collapse_w, 4), border_radius=2)
        else:
            ready = font_sm.render("READY", True, (82, 94, 112))
            screen.blit(ready, (content_right - ready.get_width(), rail.centery - ready.get_height() // 2))
        return

    def draw_clock_cell(cell: pygame.Rect, label_text: str, value_text: str, color, current: int, total: int, *, raw_total: int | None = None, lost: int = 0, head_color=None, compact: bool = False, generation_flash: float = 0.0, expire_flash: float = 0.0, prediction: dict | None = None):
        generation_flash = max(0.0, min(1.0, float(generation_flash or 0.0)))
        expire_flash = max(0.0, min(1.0, float(expire_flash or 0.0)))
        value_flash = max(generation_flash, expire_flash)
        label_s = font_sm.render(label_text, True, color)
        value_s = font_sm.render(value_text, True, _lerp_color(color, (255, 255, 255), value_flash * 0.82))
        screen.blit(label_s, (cell.x + 2, cell.centery - label_s.get_height() // 2))
        screen.blit(value_s, (cell.right - value_s.get_width() - 2, cell.centery - value_s.get_height() // 2))
        if generation_flash > 0.01:
            pygame.draw.rect(screen, (*_hud_brighten(color, 48), int(180 * generation_flash)), cell.inflate(0, -2), 1, border_radius=max(2, int(3 * scale)))
        if expire_flash > 0.01:
            collapse_w = max(2, int(cell.width * 0.16 * expire_flash))
            end_rect = pygame.Rect(cell.right - collapse_w - 2, cell.y + 2, collapse_w, max(1, cell.height - 4))
            pygame.draw.rect(screen, (245, 248, 252, int(150 * expire_flash)), end_rect, border_radius=2)
        if compact or total <= 0:
            return
        gx = cell.x + label_s.get_width() + max(4, int(5 * scale))
        gr = cell.right - value_s.get_width() - max(4, int(5 * scale))
        gw = max(10, gr - gx)
        gh = max(4, int(5 * scale))
        gy = cell.centery - gh // 2
        gauge = pygame.Rect(gx, gy, gw, gh)
        pygame.draw.rect(screen, (37, 44, 56), gauge, border_radius=max(2, gh // 2))
        pygame.draw.rect(screen, (100, 114, 136), gauge, 1, border_radius=max(2, gh // 2))
        pygame.draw.line(screen, (235, 240, 247), (gauge.x + 1, gauge.y + 1), (gauge.right - 2, gauge.y + 1), 1)
        denom = max(1, int(raw_total or total), total)
        usable_w = max(1, min(gw, int(round(gw * total / float(denom)))))
        active_zone = pygame.Rect(gx, gy, usable_w, gh)
        pygame.draw.rect(screen, (66, 76, 92), active_zone, border_radius=max(2, gh // 2))
        if lost > 0 and usable_w < gw:
            lost_rect = pygame.Rect(gx + usable_w, gy, gw - usable_w, gh)
            _draw_vertical_gradient(screen, lost_rect, (242, 122, 136), (196, 76, 90), 245)

        # Project the newly-started move directly into the SAME native stun
        # generation.  This turns STUN + MOVE TIMING into one race gauge:
        # yellow = startup that still has to elapse, red = authored active
        # window(s).  The geometry is expressed in stun-elapsed frames, so a
        # red block before the right edge means the move becomes active while
        # the victim is still stunned.  A red chevron at the right edge means
        # first active occurs only after stun has expired.
        if isinstance(prediction, dict) and total > 0:
            try:
                _pred_generation_raw = prediction.get("generation", -1)
                pred_generation = int(-1 if _pred_generation_raw is None else _pred_generation_raw)
                _cell_generation_raw = prediction.get("cell_generation", pred_generation)
                cell_generation = int(pred_generation if _cell_generation_raw is None else _cell_generation_raw)
                arm_remaining = max(0, int(prediction.get("start_remaining", 0) or 0))
                arm_frame = max(0, int(prediction.get("arm_frame", 0) or 0))
                first_start = max(0, int(prediction.get("first_start", 0) or 0))
                pred_windows = list(prediction.get("windows") or [])
            except Exception:
                pred_generation = -1
                cell_generation = -2
                arm_remaining = 0
                arm_frame = 0
                first_start = 0
                pred_windows = []

            # Caller only attaches a prediction to the matching live clock,
            # but keep generation validation here as a stale-hit guard.
            if pred_generation >= 0 and pred_generation == cell_generation and arm_remaining > 0:
                stun_elapsed_at_arm = max(0, min(int(total), int(total) - arm_remaining))

                def pred_x(stun_elapsed_value: float) -> int:
                    frac = max(0.0, min(1.0, float(stun_elapsed_value) / float(max(1, int(total)))))
                    return gx + int(round(usable_w * frac))

                startup_end_elapsed = stun_elapsed_at_arm + max(0, first_start - arm_frame)
                startup_x0 = pred_x(stun_elapsed_at_arm)
                startup_x1 = pred_x(min(int(total), startup_end_elapsed))
                if startup_end_elapsed > stun_elapsed_at_arm and startup_x1 > startup_x0:
                    startup_overlay = pygame.Surface((startup_x1 - startup_x0, gh), pygame.SRCALPHA)
                    startup_overlay.fill((242, 194, 92, 146))
                    screen.blit(startup_overlay, (startup_x0, gy))
                    pygame.draw.line(screen, (250, 217, 129), (startup_x0, gy), (startup_x1 - 1, gy), 1)

                first_active_elapsed = None
                for win_start, win_end in pred_windows:
                    try:
                        win_start = int(win_start)
                        win_end = int(win_end)
                    except Exception:
                        continue
                    abs_start = stun_elapsed_at_arm + (win_start - arm_frame)
                    abs_end = stun_elapsed_at_arm + (win_end - arm_frame)
                    if first_active_elapsed is None or abs_start < first_active_elapsed:
                        first_active_elapsed = abs_start
                    clip_start = max(0, min(int(total), abs_start))
                    clip_end = max(0, min(int(total), abs_end))
                    if clip_end > clip_start:
                        ax0, ax1 = pred_x(clip_start), pred_x(clip_end)
                        if ax1 <= ax0:
                            ax1 = min(gx + usable_w, ax0 + 1)
                        if ax1 > ax0:
                            active_overlay = pygame.Surface((ax1 - ax0, gh + 2), pygame.SRCALPHA)
                            active_overlay.fill((255, 86, 101, 212))
                            screen.blit(active_overlay, (ax0, gy - 1))
                            pygame.draw.line(screen, (255, 176, 184), (ax0, gy - 2), (ax1 - 1, gy - 2), 1)

                # First-active cue.  If it falls beyond stun, the cue lives on
                # the expiration edge and points outward instead of pretending
                # the active window existed inside the stun bar.
                if first_active_elapsed is not None:
                    if first_active_elapsed <= int(total):
                        fx = max(gx, min(gx + usable_w - 1, pred_x(first_active_elapsed)))
                        pygame.draw.line(screen, (255, 218, 223), (fx, gy - 3), (fx, gy + gh + 2), 1)
                    else:
                        edge_x = gx + usable_w
                        cap = max(2, int(3 * scale))
                        pygame.draw.polygon(
                            screen,
                            (255, 104, 118),
                            [(edge_x - cap * 2, gy - cap), (edge_x - cap * 2, gy + gh + cap), (edge_x - 1, gy + gh // 2)],
                        )

        # Countdown is numeric and the bar now reads as REMAINING stun:
        # full at generation, then depleted behind the forward-moving head.
        # The head still travels left -> right on the same elapsed-frame axis
        # used by MOVE TIMING, so the stacked rails remain directly comparable.
        active_total = max(1, int(total))
        current_clamped = max(0, min(active_total, int(current)))
        elapsed_frames = active_total - current_clamped
        progress_w = max(0, min(usable_w, int(round(usable_w * elapsed_frames / float(active_total)))))
        remaining_w = max(0, usable_w - progress_w)
        if remaining_w > 0:
            fill_x = gx + progress_w
            fill_rect = pygame.Rect(fill_x, gy, remaining_w, gh)
            _draw_vertical_gradient(screen, fill_rect, _hud_brighten(color, 14), _hud_darken(color, 8), 255)
            if fill_rect.width > 2:
                pygame.draw.line(screen, (250, 250, 250), (fill_rect.x + 1, fill_rect.y + 1), (fill_rect.right - 1, fill_rect.y + 1), 1)
        if current > 0:
            hx = gx + max(0, min(usable_w - 1, progress_w))
            pygame.draw.line(screen, head_color or color, (hx, gy - 2), (hx, gy + gh + 1), 1)

    # Only paint a projected move on the exact stun generation it was armed
    # against.  A fresh hit caused by the current move creates a new generation
    # and must not be mistaken for the old link/cancel prediction.
    hit_prediction = None
    block_prediction = None
    if isinstance(move_prediction, dict):
        pred_kind = str(move_prediction.get("kind") or "HS").upper()
        _pred_generation_raw = move_prediction.get("generation", -1)
        pred_generation = int(-1 if _pred_generation_raw is None else _pred_generation_raw)
        if pred_kind == "BS":
            block_generation = int(block_clock.get("generation", -2)) if block_clock is not None and block_clock.get("generation", -2) is not None else -2
            if block_clock is not None and pred_generation == block_generation:
                block_prediction = dict(move_prediction)
                block_prediction["cell_generation"] = block_generation
        elif contact_clock is not None:
            contact_generation = int(contact_clock.get("generation", -2)) if contact_clock.get("generation", -2) is not None else -2
            if pred_generation == contact_generation:
                hit_prediction = dict(move_prediction)
                hit_prediction["cell_generation"] = contact_generation

    avail = max(40, content_right - content_x)
    hit_head = STUN_CLOCK_UNTECH_HEAD if clock_source == "untech" else STUN_CLOCK_HIT_HEAD
    hit_generation_flash = float(slot_anim.get("stun_generation_flash", 0.0))
    hit_expire_flash = float(slot_anim.get("stun_expire_flash", 0.0))
    block_generation_flash = float(slot_anim.get("bs_generation_flash", 0.0))
    block_expire_flash = float(slot_anim.get("bs_expire_flash", 0.0))

    if hit_active and block_active:
        first = max(20, (avail - gap) // 2)
        hit_cell = pygame.Rect(content_x, rail.y + 1, first, max(1, rail.height - 2))
        block_cell = pygame.Rect(content_x + first + gap, rail.y + 1, max(20, avail - gap - first), max(1, rail.height - 2))
        sep_x = block_cell.x - gap // 2
        pygame.draw.line(screen, (47, 59, 75), (sep_x, rail.y + 4), (sep_x, rail.bottom - 4), 1)
        draw_clock_cell(hit_cell, hit_label, hit_value, hit_color, remaining, target, raw_total=base_est, lost=(loss if clock_source == "untech" else 0), head_color=hit_head, generation_flash=hit_generation_flash, expire_flash=hit_expire_flash, prediction=hit_prediction)
        draw_clock_cell(block_cell, "BS", block_value, block_color, block_remaining, block_target, head_color=STUN_CLOCK_BLOCK_HEAD, generation_flash=block_generation_flash, expire_flash=block_expire_flash, prediction=block_prediction)
        return

    # One live clock gets almost the whole row. Its inactive sibling remains a
    # compact identity chip, so the user can still see what the other lane is.
    if hit_active:
        inactive_text = font_sm.render("BS --", True, (82, 94, 112))
        compact_w = inactive_text.get_width() + max(6, int(8 * scale))
        active_w = max(24, avail - gap - compact_w)
        hit_cell = pygame.Rect(content_x, rail.y + 1, active_w, max(1, rail.height - 2))
        block_cell = pygame.Rect(content_x + active_w + gap, rail.y + 1, compact_w, max(1, rail.height - 2))
        draw_clock_cell(hit_cell, hit_label, hit_value, hit_color, remaining, target, raw_total=base_est, lost=(loss if clock_source == "untech" else 0), head_color=hit_head, generation_flash=hit_generation_flash, expire_flash=hit_expire_flash, prediction=hit_prediction)
        draw_clock_cell(block_cell, "BS", "--", (82, 94, 112), 0, 0, compact=True)
    else:
        inactive_text = font_sm.render(f"{hit_label} --", True, (82, 94, 112))
        compact_w = inactive_text.get_width() + max(6, int(8 * scale))
        block_w = max(24, avail - gap - compact_w)
        hit_cell = pygame.Rect(content_x, rail.y + 1, compact_w, max(1, rail.height - 2))
        block_cell = pygame.Rect(content_x + compact_w + gap, rail.y + 1, block_w, max(1, rail.height - 2))
        draw_clock_cell(hit_cell, hit_label, "--", (82, 94, 112), 0, 0, compact=True)
        draw_clock_cell(block_cell, "BS", block_value, block_color, block_remaining, block_target, head_color=STUN_CLOCK_BLOCK_HEAD, generation_flash=block_generation_flash, expire_flash=block_expire_flash, prediction=block_prediction)

def _research_dock_active_panel(control=None) -> str | None:
    """Return one fallback research panel when the core HUD is hidden."""
    if control is None:
        return None
    for key, attr in (
        ("attack", "show_attack_property_panel"),
        ("damage", "show_damage_badge"),
        ("untech", "show_untech_panel"),
        ("meter", "show_meter_panel"),
        ("red", "show_red_health_panel"),
    ):
        if bool(getattr(control, attr, False)):
            return key
    return None


def _research_dock_geometry(screen, scale: float, panel: str) -> pygame.Rect:
    margin = max(10, int(14 * scale))
    max_width = max(520, int(760 * scale))
    width = min(screen.get_width() - margin * 2, max_width)
    content_heights = {
        "damage": max(112, int(118 * scale)),
        "untech": max(72, int(78 * scale)),
        "meter": max(98, int(104 * scale)),
        "red": max(170, int(178 * scale)),
        "attack": max(220, int(240 * scale)),
    }
    title_h = max(27, int(29 * scale))
    height = title_h + content_heights.get(panel, content_heights["damage"])
    x = (screen.get_width() - width) // 2
    y = screen.get_height() - height - margin
    return pygame.Rect(x, max(margin, y), width, height)


def _research_dock_title(panel: str) -> tuple[str, tuple[int, int, int]]:
    return {
        "damage": ("RESEARCH DOCK  |  DAMAGE MODIFIER", (222, 170, 74)),
        "untech": ("RESEARCH DOCK  |  AIR HS", (167, 151, 220)),
        "meter": ("RESEARCH DOCK  |  METER GENERATION", (67, 201, 156)),
        "red": ("RESEARCH DOCK  |  RECOVERABLE HEALTH", (223, 82, 102)),
        "attack": ("RESEARCH DOCK  |  ATTACK PROPERTIES", (158, 109, 216)),
    }.get(panel, ("RESEARCH DOCK", (104, 169, 224)))


def _draw_research_damage_content(card, area: pygame.Rect, font, font_sm, scale: float, dt: float) -> None:
    rows = _damage_modifier_badge_rows()
    if not rows:
        empty = font.render("NO LIVE DAMAGE DATA", True, (125, 139, 158))
        card.blit(empty, (area.centerx - empty.get_width() // 2, area.centery - empty.get_height() // 2))
        return

    gap = max(8, int(10 * scale))
    cell_w = (area.width - gap) // 2
    meter_h = max(7, int(8 * scale))
    for index, (label, data, accent) in enumerate(rows[:2]):
        cell = pygame.Rect(area.x + index * (cell_w + gap), area.y, cell_w, area.height)
        pygame.draw.rect(card, (*accent, 22), cell, border_radius=max(4, int(5 * scale)))
        pygame.draw.rect(card, (*accent, 120), cell, 1, border_radius=max(4, int(5 * scale)))
        pad = max(8, int(9 * scale))
        percent = float(data.get("percent") or 100.0)
        approximate = bool(data.get("approximate", False))
        live = bool(data.get("live", False))
        source_slot = str(data.get("source_slot") or "")
        slot_anim = _get_slot_anim(source_slot) if source_slot else None
        if slot_anim is not None:
            previous_target = slot_anim.get("damage_scale_target_pct")
            if previous_target is None or abs(float(previous_target) - percent) > 0.01:
                slot_anim["damage_scale_target_pct"] = percent
                if live:
                    slot_anim["damage_scale_pulse"] = 1.0
            visual_percent = _ease_visual(slot_anim.get("damage_scale_visual_pct"), percent, 30.0, dt)
            slot_anim["damage_scale_visual_pct"] = visual_percent
            slot_anim["damage_scale_pulse"] = max(0.0, float(slot_anim.get("damage_scale_pulse", 0.0)) - max(0.0, dt) * 4.8)
        else:
            visual_percent = percent
        label_s = _panel_fit(font_sm, label, cell.width - pad * 2 - max(76, int(86 * scale)))
        value_s = font.render(
            f"{percent:.1f}%" + ("*" if approximate else ""),
            True,
            accent if live else (132, 142, 158),
        )
        card.blit(label_s, (cell.x + pad, cell.y + pad - 1))
        card.blit(value_s, (cell.right - pad - value_s.get_width(), cell.y + max(3, pad - 4)))

        meter = pygame.Rect(cell.x + pad, cell.y + pad + max(label_s.get_height(), value_s.get_height()) + 4, cell.width - pad * 2, meter_h)
        _draw_damage_modifier_meter(card, meter, visual_percent if live else 0.0, accent if live else (104, 112, 126), scale)

        factors = list(data.get("factors") or ["BASE"])
        if not live:
            factors = ["NO LIVE SCALING DATA"]
        factor_lines = _wrap_damage_factor_lines(font_sm, factors, cell.width - pad * 2)
        factor_y = meter.bottom + max(5, int(6 * scale))
        max_lines = max(1, (cell.bottom - factor_y - pad) // max(1, font_sm.get_height() + 2))
        for line in factor_lines[:max_lines]:
            surface = font_sm.render(line, True, (164, 184, 207))
            card.blit(surface, (cell.x + pad, factor_y))
            factor_y += font_sm.get_height() + 2


def _draw_research_untech_content(card, area: pygame.Rect, font, font_sm, scale: float, dt: float) -> None:
    gap = max(8, int(10 * scale))
    cell_w = (area.width - gap) // 2
    for index, team in enumerate(("P1", "P2")):
        slot, snap = _team_point_snapshot(team)
        snap = snap if isinstance(snap, dict) else {}
        accent = (236, 92, 108) if team == "P1" else (82, 164, 236)
        cell = pygame.Rect(area.x + index * (cell_w + gap), area.y, cell_w, area.height)
        pygame.draw.rect(card, (*accent, 20), cell, border_radius=max(4, int(5 * scale)))
        pygame.draw.rect(card, (*accent, 110), cell, 1, border_radius=max(4, int(5 * scale)))
        pad = max(8, int(9 * scale))
        counter = max(0, _panel_int(snap.get("hitstun_decay_counter"), 0))
        target = max(0, _panel_int(snap.get("hitstun_untech_expiry_target", snap.get("hitstun_untech_effective_start")), 0))
        loss = max(0, _panel_int(snap.get("hitstun_untech_latched_loss", snap.get("hitstun_decay_frames")), 0)) if target > 0 else max(0, _panel_int(snap.get("hitstun_decay_frames"), 0))
        elapsed = max(0, min(target, _panel_int(snap.get("hitstun_untech_elapsed"), 0))) if target > 0 else 0
        base = max(target, _panel_int(snap.get("hitstun_untech_base_estimate"), 0))
        generation = max(0, _panel_int(snap.get("hitstun_untech_generation"), 0))
        active = bool(snap.get("hitstun_untech_active", False))
        expired = bool(snap.get("hitstun_untech_expired", False))
        cantukemi = bool(snap.get("hitstun_cantukemi", False))
        approximate = bool(snap.get("hitstun_untech_approximate", False))

        slot_anim = _get_slot_anim(slot or f"{team}-C1")
        signature = (generation, base, target, loss, cantukemi)
        slot_anim["hs_visual_signature"] = signature
        slot_anim["hs_visual_pulse"] = 0.0

        name = _compact_trim(str(snap.get("name") or slot or "---"), 15)
        header = font_sm.render(f"{team}  {name}", True, accent)
        remaining_frames = max(0, target - elapsed) if target > 0 else 0
        if cantukemi:
            value_text = "NO TECH"
            value_color = (235, 91, 108)
        elif target > 0:
            value_text = f"{remaining_frames}/{target}F"
            value_color = (151, 164, 184) if expired else (232, 240, 248)
        else:
            value_text = f"DECAY -{loss}F"
            value_color = (232, 240, 248)
        value = font.render(value_text, True, value_color)
        card.blit(header, (cell.x + pad, cell.y + pad))
        card.blit(value, (cell.right - pad - value.get_width(), cell.y + max(3, pad - 4)))

        bar = pygame.Rect(cell.x + pad, cell.y + pad + max(header.get_height(), value.get_height()) + 5, cell.width - pad * 2, max(8, int(9 * scale)))
        pygame.draw.rect(card, (24, 34, 47), bar, border_radius=3)
        if target > 0 and not cantukemi:
            denominator = max(1, base, target)
            target_w = max(1, min(bar.width, int(round(bar.width * target / float(denominator)))))
            visual_elapsed = _hs_visual_elapsed(slot_anim, generation, elapsed, target, dt)
            visual_remaining = max(0.0, float(target) - visual_elapsed)
            exact_remaining = max(0, target - elapsed)
            fill_w = max(0, min(target_w, int(round(bar.width * visual_remaining / float(denominator)))))
            exact_w = max(0, min(target_w, int(round(bar.width * exact_remaining / float(denominator)))))
            pygame.draw.rect(card, (72, 82, 96), (bar.x, bar.y, target_w, bar.height), border_radius=3)
            if target_w < bar.width:
                pygame.draw.rect(card, (235, 91, 108), (bar.x + target_w, bar.y, bar.width - target_w, bar.height), border_radius=3)
            if fill_w > 0:
                pygame.draw.rect(card, (122, 183, 198), (bar.x, bar.y, fill_w, bar.height), border_radius=3)
            pygame.draw.line(card, (238, 218, 224), (bar.x + target_w, bar.y - 1), (bar.x + target_w, bar.bottom), 1)
            if active and elapsed < target:
                lead_x = bar.x + max(0, min(target_w - 1, exact_w))
                pygame.draw.line(card, (218, 246, 252), (lead_x, bar.y - 2), (lead_x, bar.bottom + 1), 1)

        raw_prefix = "~" if approximate and base > 0 else ""
        raw_text = f"RAW {raw_prefix}{base}F" if base > 0 else "RAW --"
        detail = _panel_fit(
            font_sm,
            f"{raw_text}   DECAY -{loss}F   COUNT {counter}   FLOOR 4F",
            cell.width - pad * 2,
        )
        card.blit(detail, (cell.x + pad, bar.bottom + 5))

def _draw_research_meter_content(card, area: pygame.Rect, font, font_sm, scale: float) -> None:
    gap = max(8, int(10 * scale))
    cell_w = (area.width - gap) // 2
    for index, team in enumerate(("P1", "P2")):
        slot, snap = _team_point_snapshot(team)
        snap = snap if isinstance(snap, dict) else {}
        accent = (236, 92, 108) if team == "P1" else (82, 164, 236)
        cell = pygame.Rect(area.x + index * (cell_w + gap), area.y, cell_w, area.height)
        pygame.draw.rect(card, (*accent, 20), cell, border_radius=max(4, int(5 * scale)))
        pygame.draw.rect(card, (*accent, 110), cell, 1, border_radius=max(4, int(5 * scale)))
        pad = max(8, int(9 * scale))
        current = max(0, min(50000, _panel_int(snap.get("meter_profile_current", snap.get("meter")), 0)))
        delta = _panel_int(snap.get("meter_profile_last_delta"), 0)
        predicted_raw = snap.get("meter_profile_last_predicted")
        predicted = None if predicted_raw is None else _panel_int(predicted_raw)
        match = snap.get("meter_profile_last_match")
        role = str(snap.get("meter_profile_last_role") or "").upper()
        source = str(snap.get("meter_profile_last_source") or "")
        move = str(snap.get("meter_profile_last_move") or "")
        name = _compact_trim(str(snap.get("name") or slot or "---"), 15)
        header = font_sm.render(f"{team}  {name}", True, accent)
        value = font.render(f"{current / 10000.0:.2f} BAR", True, (232, 240, 248))
        card.blit(header, (cell.x + pad, cell.y + pad))
        card.blit(value, (cell.right - pad - value.get_width(), cell.y + max(3, pad - 4)))

        bar = pygame.Rect(cell.x + pad, cell.y + pad + max(header.get_height(), value.get_height()) + 5, cell.width - pad * 2, max(8, int(9 * scale)))
        pygame.draw.rect(card, (24, 34, 47), bar, border_radius=3)
        fill_w = int(bar.width * current / 50000.0)
        if fill_w > 0:
            pygame.draw.rect(card, accent, (bar.x, bar.y, fill_w, bar.height), border_radius=3)
        for pip in range(1, 5):
            px = bar.x + int(bar.width * pip / 5.0)
            pygame.draw.line(card, (9, 14, 22), (px, bar.y), (px, bar.bottom), 1)

        if delta:
            prediction_text = ""
            if predicted is not None:
                state = "MATCH" if match is True else ("MISS" if match is False else "PRED")
                prediction_text = f"  PRED {predicted:+d} {state}"
            detail = f"LAST {delta:+d}  {role or 'UNKNOWN'}{prediction_text}"
        else:
            detail = "NO METER TRANSITION CAPTURED"
        detail_s = _panel_fit(font_sm, detail, cell.width - pad * 2)
        card.blit(detail_s, (cell.x + pad, bar.bottom + 5))
        source_line = "  ".join(part for part in (source, move) if part)
        if source_line:
            source_s = _panel_fit(font_sm, source_line, cell.width - pad * 2)
            source_s.set_alpha(190)
            card.blit(source_s, (cell.x + pad, bar.bottom + 7 + font_sm.get_height()))


def _draw_research_red_content(card, area: pygame.Rect, font, font_sm, scale: float) -> None:
    gap_x = max(8, int(10 * scale))
    gap_y = max(6, int(7 * scale))
    cell_w = (area.width - gap_x) // 2
    cell_h = (area.height - gap_y) // 2
    slots = ("P1-C1", "P1-C2", "P2-C1", "P2-C2")
    for index, slot in enumerate(slots):
        col = index % 2
        row = index // 2
        cell = pygame.Rect(area.x + col * (cell_w + gap_x), area.y + row * (cell_h + gap_y), cell_w, cell_h)
        snap = _display_slots.get(slot)
        snap = snap if isinstance(snap, dict) else {}
        accent = SLOT_COLORS.get(slot, (180, 180, 190))
        point = bool(snap.get("red_health_point", snap.get("damage_point_active", False)))
        current = _panel_int(snap.get("red_health_current", snap.get("cur")), 0)
        maximum = max(1, _panel_int(snap.get("max"), 1))
        auxiliary = _panel_int(snap.get("red_health_aux", snap.get("recoverable_ceiling")), current)
        red = max(0, _panel_int(snap.get("red_health_recoverable", snap.get("recoverable_hp")), auxiliary - current))
        red_pct = float(snap.get("red_health_pct_max", snap.get("recoverable_pct_max", 0.0)) or 0.0)
        status = "POINT" if point else "RESERVE"
        if _panel_int(snap.get("cur"), 0) <= 0:
            status = "KO"
        pygame.draw.rect(card, (*accent, 20), cell, border_radius=max(4, int(5 * scale)))
        pygame.draw.rect(card, (*accent, 105), cell, 1, border_radius=max(4, int(5 * scale)))
        pad = max(7, int(8 * scale))
        name = _compact_trim(str(snap.get("name") or "---"), 15)
        header = _panel_fit(font_sm, f"{slot}  {name}  {status}", cell.width - pad * 2)
        value = _panel_fit(font_sm, f"HP {current}  AUX {auxiliary}  RED {red} ({red_pct:.1f}%)", cell.width - pad * 2)
        card.blit(header, (cell.x + pad, cell.y + pad - 1))
        card.blit(value, (cell.x + pad, cell.y + pad + font_sm.get_height() + 1))

        bar = pygame.Rect(cell.x + pad, cell.y + pad + font_sm.get_height() * 2 + 5, cell.width - pad * 2, max(7, int(8 * scale)))
        pygame.draw.rect(card, (24, 34, 47), bar, border_radius=3)
        aux_w = int(bar.width * max(0.0, min(1.0, auxiliary / float(maximum))))
        cur_w = int(bar.width * max(0.0, min(1.0, current / float(maximum))))
        if aux_w > 0:
            pygame.draw.rect(card, (150, 55, 74), (bar.x, bar.y, aux_w, bar.height), border_radius=3)
        if cur_w > 0:
            pygame.draw.rect(card, accent, (bar.x, bar.y, cur_w, bar.height), border_radius=3)

        event = str(snap.get("red_health_last_event") or "")
        pending_cur = _panel_int(snap.get("red_health_pending_current"), 0)
        pending_aux = _panel_int(snap.get("red_health_pending_aux"), 0)
        if event:
            red_delta = _panel_int(snap.get("red_health_last_red_delta"), 0)
            detail = f"{event.replace('_', ' ').upper()}  RED {red_delta:+d}  Q {pending_cur:+d}/{pending_aux:+d}"
        else:
            detail = f"QUEUE {pending_cur:+d}/{pending_aux:+d}  SYNC {_panel_int(snap.get('red_health_heal_sync'), 0)}"
        detail_s = _panel_fit(font_sm, detail, cell.width - pad * 2)
        detail_s.set_alpha(195)
        card.blit(detail_s, (cell.x + pad, bar.bottom + 4))


def _draw_research_attack_content(card, area: pygame.Rect, font, font_sm, scale: float) -> None:
    gap = max(8, int(10 * scale))
    cell_w = (area.width - gap) // 2
    for index, team in enumerate(("P1", "P2")):
        slot, snap = _active_attack_snapshot(team)
        snap = snap if isinstance(snap, dict) else {}
        accent = (236, 92, 108) if team == "P1" else (82, 164, 236)
        cell = pygame.Rect(area.x + index * (cell_w + gap), area.y, cell_w, area.height)
        pygame.draw.rect(card, (*accent, 20), cell, border_radius=max(4, int(5 * scale)))
        pygame.draw.rect(card, (*accent, 105), cell, 1, border_radius=max(4, int(5 * scale)))
        pad = max(8, int(9 * scale))
        actor = _panel_int(snap.get("attack_property_live_actor"), 0)
        display_active = bool(snap.get("attack_property_display_active")) or bool(actor)
        name = _compact_trim(str(snap.get("name") or slot or "---"), 14)
        packet_move = str(snap.get("attack_property_packet_action_name") or "")
        move = _compact_trim(packet_move or str(snap.get("final_move_label") or snap.get("mv_label_display") or snap.get("mv_label") or "---"), 25)
        packet_state = str(snap.get("attack_property_packet_state") or "NONE").upper()
        header = _panel_fit(font_sm, f"{team}  {packet_state}  {name}  {move}", cell.width - pad * 2)
        card.blit(header, (cell.x + pad, cell.y + pad))
        if not display_active:
            status = str(snap.get("attack_property_definition_status") or "WAITING").upper().replace("_", " ")
            error = str(snap.get("attack_property_definition_error") or "").strip()
            action_id = _panel_int(snap.get("attack_property_definition_action_id") or snap.get("mv_id_display") or snap.get("attA"), 0)
            empty = font.render("NO PROPERTY FOR CURRENT ACTION", True, (123, 137, 157))
            card.blit(empty, (cell.x + pad, cell.y + pad + header.get_height() + 9))
            detail_text = f"{status}  ACTION {action_id:04X}" if action_id else status
            if error:
                detail_text += f"  {error}"
            detail = _panel_fit(font_sm, detail_text, cell.width - pad * 2)
            card.blit(detail, (cell.x + pad, cell.y + pad + header.get_height() + 34))
            continue

        source = str(snap.get("attack_property_display_source") or snap.get("attack_property_packet_source") or "")
        projectiles = _attack_actor_groups(snap)
        if source in {
            "move_definition", "move_definition_latched",
            "native_script_and_live_attack_actor", "native_script_and_last_attack_actor",
            "live_attack_actor", "live_attack_actor_latched",
            "native_script_and_live_projectile", "native_script_and_last_projectile",
            "live_projectile_actor", "live_projectile_latched",
        }:
            if source == "move_definition":
                state_text = "CURRENT NATIVE SCRIPT"
            elif source == "move_definition_latched":
                state_text = "LAST NATIVE SCRIPT"
            elif source in {"native_script_and_live_attack_actor", "native_script_and_live_projectile"}:
                state_text = "CURRENT NATIVE SCRIPT + LIVE ATTACK ACTOR"
            elif source in {"native_script_and_last_attack_actor", "native_script_and_last_projectile"}:
                state_text = "CURRENT NATIVE SCRIPT + LAST ATTACK ACTOR"
            elif source in {"live_attack_actor_latched", "live_projectile_latched"}:
                state_text = "LAST SPAWNED ATTACK ACTOR"
            else:
                state_text = "LIVE SPAWNED ATTACK ACTOR"
            action_id = _panel_int(snap.get("attack_property_packet_action_id"), 0)
            line_y = cell.y + pad + header.get_height() + 10
            phases = _attack_property_phase_rows(snap)
            phase_groups = _attack_property_phase_groups(phases)
            summary = f"{state_text}  ACTION {action_id:04X}"
            if phases:
                summary += f"  {len(phases)} SCRIPT BLOCK{'S' if len(phases) != 1 else ''}"
                if len(phase_groups) != len(phases):
                    summary += f"  {len(phase_groups)} UNIQUE"
            if projectiles:
                live_count = sum(
                    max(1, _panel_int(row.get("actor_count"), 1))
                    for row in projectiles
                    if bool(row.get("attack_actor_live", row.get("projectile_live")))
                )
                total_actor_count = sum(max(1, _panel_int(row.get("actor_count"), 1)) for row in projectiles)
                projectile_word = "LIVE ATTACK ACTOR" if live_count else "LAST ATTACK ACTOR"
                projectile_count = live_count if live_count else total_actor_count
                summary += f"  {projectile_count} {projectile_word}{'S' if projectile_count != 1 else ''}"
            surf = _panel_fit(font_sm, summary, cell.width - pad * 2)
            card.blit(surf, (cell.x + pad, line_y))
            line_y += font_sm.get_height() + 5

            phase_line_h = font_sm.get_height() + 3
            remaining_lines = max(0, (cell.bottom - pad - line_y) // max(1, phase_line_h))
            visible_phase_count = max(1, remaining_lines // 3) if phase_groups else 0
            visible_phases = phase_groups[:visible_phase_count]
            for phase in visible_phases:
                primary = "  ".join(text for text, _color in _attack_phase_primary_tokens(phase))
                secondary = "  ".join(text for text, _color in _attack_phase_secondary_tokens(phase, include_raw=True))
                operations = _attack_native_operation_text(phase)
                for text, color in (
                    (primary, (214, 224, 239)),
                    (secondary, (166, 181, 204)),
                    (operations, (144, 155, 174)),
                ):
                    if not text or line_y + font_sm.get_height() > cell.bottom - pad:
                        continue
                    row_s = font_sm.render(_compact_fit_text(font_sm, text, cell.width - pad * 2), True, color)
                    card.blit(row_s, (cell.x + pad, line_y))
                    line_y += font_sm.get_height() + 3
            if len(phase_groups) > len(visible_phases) and line_y + font_sm.get_height() <= cell.bottom - pad:
                more = font_sm.render(f"+{len(phase_groups) - len(visible_phases)} MORE UNIQUE BLOCK TYPES", True, (166, 181, 204))
                card.blit(more, (cell.x + pad, line_y))
                line_y += font_sm.get_height() + 3

            if projectiles and line_y + font_sm.get_height() <= cell.bottom - pad:
                any_live_projectile = any(bool(row.get("projectile_live")) for row in projectiles)
                proj_header_text = "LIVE SPAWNED ATTACK ACTORS" if any_live_projectile else "LAST SPAWNED ATTACK ACTORS"
                proj_header_color = (102, 224, 164) if any_live_projectile else (236, 188, 92)
                proj_header = font_sm.render(proj_header_text, True, proj_header_color)
                card.blit(proj_header, (cell.x + pad, line_y))
                line_y += font_sm.get_height() + 3
                for projectile in projectiles:
                    primary = "  ".join(text for text, _color in _attack_projectile_primary_tokens(projectile))
                    secondary = "  ".join(text for text, _color in _attack_projectile_secondary_tokens(projectile, include_raw=True))
                    for text, color in ((primary, (214, 224, 239)), (secondary, (144, 155, 174))):
                        if not text or line_y + font_sm.get_height() > cell.bottom - pad:
                            break
                        row_s = font_sm.render(_compact_fit_text(font_sm, text, cell.width - pad * 2), True, color)
                        card.blit(row_s, (cell.x + pad, line_y))
                        line_y += font_sm.get_height() + 3
                    if line_y + font_sm.get_height() > cell.bottom - pad:
                        break
            continue

        damage = _panel_int(snap.get("attack_property_live_damage"), 0)
        status20 = _panel_hex(snap.get("attack_property_live_status20"))
        prop_a = _panel_hex(snap.get("attack_property_live_a"))
        prop_b = _panel_hex(snap.get("attack_property_live_b"))
        text_a = str(snap.get("attack_property_live_a_text") or "UNRESOLVED")
        text_b = str(snap.get("attack_property_live_b_text") or "UNRESOLVED")
        scaling = str(snap.get("attack_property_live_scaling_track") or "Combat lane unknown")
        victim = str(snap.get("attack_property_live_victim_slot") or "-")
        phase_a = _panel_int(snap.get("attack_property_live_phase_a"), 0)
        phase_b = _panel_int(snap.get("attack_property_live_phase_b"), 0)
        phase_text = f"  |  NEXT A {phase_a:08X} B {phase_b:08X}" if (phase_a or phase_b) else ""
        lines = (
            f"ACTOR {actor:08X}  DMG {damage}  VICTIM {victim}  STATUS {status20}",
            f"A {prop_a}  {text_a}",
            f"B {prop_b}  {text_b}  |  {scaling}{phase_text}",
        )
        line_y = cell.y + pad + header.get_height() + 7
        for line in lines:
            surf = _panel_fit(font_sm, line, cell.width - pad * 2)
            card.blit(surf, (cell.x + pad, line_y))
            line_y += font_sm.get_height() + 3


def _draw_research_dock(screen, font, font_sm, scale: float, panel: str, dt: float) -> None:
    rect = _research_dock_geometry(screen, scale, panel)
    title, accent = _research_dock_title(panel)
    card = _draw_research_shell(screen, rect, title, accent, font_sm)
    title_h = max(27, int(29 * scale))
    pad = max(9, int(10 * scale))
    area = pygame.Rect(pad, title_h + 2, rect.width - pad * 2, rect.height - title_h - pad)
    if panel == "damage":
        _draw_research_damage_content(card, area, font, font_sm, scale, dt)
    elif panel == "untech":
        _draw_research_untech_content(card, area, font, font_sm, scale, dt)
    elif panel == "meter":
        _draw_research_meter_content(card, area, font, font_sm, scale)
    elif panel == "red":
        _draw_research_red_content(card, area, font, font_sm, scale)
    elif panel == "attack":
        _draw_research_attack_content(card, area, font, font_sm, scale)
    screen.blit(card, rect.topleft)


def _draw_research_panels(screen, font, font_sm, scale: float, control=None, dt: float = 1.0 / 60.0) -> None:
    panel = _research_dock_active_panel(control)
    if panel is not None:
        _draw_research_dock(screen, font, font_sm, scale, panel, dt)

def _draw_live_interaction_ribbon(screen, font, font_sm, scale: float, dt: float) -> None:
    life = max(0.0, float(_interaction_ribbon.get("life") or 0.0) - dt * 0.72)
    age = max(0.0, float(_interaction_ribbon.get("age") or 0.0) + dt)
    _interaction_ribbon["life"] = life
    _interaction_ribbon["age"] = age
    if life <= 0.01 or not _interaction_ribbon.get("title"):
        return

    fade_out = min(1.0, life * 2.6)
    panel_progress = _compact_lock_ease(age / 0.24)
    slice_progress = _compact_lock_ease((age - 0.12) / 0.24)
    slice_lock_pulse = math.sin(math.pi * max(0.0, min(1.0, (age - 0.34) / 0.18)))
    divider_progress = _compact_lock_ease((age - 0.31) / 0.18)
    sheen_progress = max(0.0, min(1.0, (age - 0.52) / 0.34))
    sheen_alpha = math.sin(math.pi * sheen_progress) if 0.0 < sheen_progress < 1.0 else 0.0
    fade = min(fade_out, panel_progress)

    title = str(_interaction_ribbon.get("title") or "")
    detail = str(_interaction_ribbon.get("detail") or "")
    accent = tuple(_interaction_ribbon.get("color") or (130, 175, 255))
    stamp = str(_interaction_ribbon.get("stamp") or "").strip().upper()
    title_left, title_separator, title_right = title.partition("  |  ")
    if not title_separator:
        title_left, title_right = title, ""
    title_left_s = font.render(title_left, True, (246, 248, 252))
    title_right_s = font.render(title_right, True, (246, 248, 252)) if title_right else None
    detail_s = font_sm.render(detail, True, (196, 208, 226))
    title_gap = max(7, int(8 * scale))
    separator_gap = max(5, int(6 * scale))
    title_width = title_left_s.get_width()
    if title_right_s is not None:
        title_width += title_gap + max(1, int(1 * scale)) + separator_gap + title_right_s.get_width()
    stamp_s = font_sm.render(stamp, True, (255, 255, 255)) if stamp else None
    stamp_reserve = (stamp_s.get_width() + int(34 * scale)) if stamp_s is not None else 0
    width = max(int(280 * scale), title_width + int(28 * scale) + stamp_reserve, detail_s.get_width() + int(34 * scale))
    title_height = max(title_left_s.get_height(), title_right_s.get_height() if title_right_s is not None else 0)
    height = max(int(42 * scale), title_height + detail_s.get_height() + int(12 * scale))
    base_x = screen.get_width() // 2 - width // 2
    base_y = int(100 * scale)
    x = base_x + int((1.0 - panel_progress) * -34 * scale)
    y = base_y + int((1.0 - panel_progress) * -10 * scale)
    radius = max(6, int(7 * scale))

    shadow = pygame.Surface((width + 10, height + 10), pygame.SRCALPHA)
    pygame.draw.rect(shadow, (0, 0, 0, int(88 * fade)), (5, 5, width, height), border_radius=radius + 2)
    screen.blit(shadow, (x - 5, y + 2))

    card = pygame.Surface((width, height), pygame.SRCALPHA)
    pygame.draw.rect(card, (10, 13, 19, int(232 * fade)), card.get_rect(), border_radius=radius)
    pygame.draw.rect(card, (34, 41, 55, int(216 * fade)), card.get_rect(), 1, border_radius=radius)

    rail_w = max(5, int(6 * scale))
    slice_alpha = max(0.0, min(1.0, slice_progress))
    pygame.draw.rect(
        card,
        (*accent, int(220 * fade * slice_alpha)),
        (0, 0, rail_w, height),
        border_top_left_radius=radius,
        border_bottom_left_radius=radius,
    )

    slice_target_w = max(2, int(width * 0.40))
    slice_bottom_w = max(1, int(width * 0.28))
    slice_layer = pygame.Surface((slice_target_w, height), pygame.SRCALPHA)
    highlight = tuple(min(255, int(channel + (255 - channel) * 0.30)) for channel in accent)
    for gradient_x in range(slice_target_w):
        ratio = gradient_x / max(1, slice_target_w - 1)
        gradient_color = _lerp_color(highlight, accent, min(1.0, ratio * 0.88))
        gradient_alpha = int((94.0 - 62.0 * ratio) * fade * slice_alpha)
        if gradient_x <= slice_bottom_w:
            bottom_y = height - 1
        else:
            tail = (slice_target_w - gradient_x) / max(1, slice_target_w - slice_bottom_w)
            bottom_y = max(0, int((height - 1) * tail))
        pygame.draw.line(
            slice_layer,
            (*gradient_color, max(0, gradient_alpha)),
            (gradient_x, 0),
            (gradient_x, bottom_y),
            1,
        )
    slice_offset_x = int((1.0 - slice_progress) * -slice_target_w * 0.92)
    old_clip = card.get_clip()
    card.set_clip(pygame.Rect(0, 0, slice_target_w, height))
    card.blit(slice_layer, (slice_offset_x, 0))
    card.set_clip(old_clip)

    if slice_lock_pulse > 0.001:
        lock_alpha = int(116 * fade * slice_lock_pulse)
        pygame.draw.line(
            card,
            (232, 242, 255, lock_alpha),
            (slice_target_w, 1),
            (slice_bottom_w, height - 2),
            max(1, int(2 * scale)),
        )
        pygame.draw.line(
            card,
            (*accent, int(72 * fade * slice_lock_pulse)),
            (slice_target_w - max(2, int(3 * scale)), 1),
            (slice_bottom_w - max(2, int(3 * scale)), height - 2),
            1,
        )

    top_line_start = max(8, int(9 * scale))
    top_line_end = top_line_start + int((width - top_line_start - int(10 * scale)) * slice_alpha)
    if top_line_end > top_line_start:
        pygame.draw.line(card, (248, 250, 254, int(58 * fade * slice_alpha)), (top_line_start, 1), (top_line_end, 1), 1)

    title_x = int(14 * scale)
    title_y = int(5 * scale)
    title_left_s.set_alpha(int(255 * fade))
    card.blit(title_left_s, (title_x, title_y))

    if title_right_s is not None:
        separator_x = title_x + title_left_s.get_width() + title_gap
        separator_target_h = max(int(12 * scale), title_height - int(2 * scale))
        separator_h = max(0, int(separator_target_h * divider_progress))
        separator_mid_y = title_y + title_height // 2
        if separator_h > 0:
            pygame.draw.line(
                card,
                (*accent, int(210 * fade * divider_progress)),
                (separator_x, separator_mid_y - separator_h // 2),
                (separator_x, separator_mid_y + separator_h // 2),
                max(1, int(1 * scale)),
            )
            pygame.draw.line(
                card,
                (248, 250, 254, int(88 * fade * divider_progress)),
                (separator_x + 1, separator_mid_y - separator_h // 3),
                (separator_x + 1, separator_mid_y + separator_h // 3),
                1,
            )
        title_right_s.set_alpha(int(255 * fade * divider_progress))
        right_x = separator_x + separator_gap + int((1.0 - divider_progress) * 8 * scale)
        card.blit(title_right_s, (right_x, title_y))

    if sheen_alpha > 0.0:
        sheen = pygame.Surface((width, height), pygame.SRCALPHA)
        band_w = max(int(30 * scale), width // 7)
        center_x = int(-band_w + sheen_progress * (width + band_w * 2))
        pygame.draw.polygon(
            sheen,
            (255, 255, 255, int(46 * fade * sheen_alpha)),
            [
                (center_x - band_w, 0),
                (center_x + max(2, band_w // 3), 0),
                (center_x - max(2, band_w // 3), height - 1),
                (center_x - band_w * 2, height - 1),
            ],
        )
        pygame.draw.line(
            sheen,
            (255, 255, 255, int(82 * fade * sheen_alpha)),
            (center_x, 1),
            (center_x - band_w, height - 2),
            1,
        )
        card.blit(sheen, (0, 0), special_flags=pygame.BLEND_RGBA_ADD)

    detail_s.set_alpha(int(240 * fade * divider_progress))
    detail_x = int(14 * scale) + int((1.0 - divider_progress) * 6 * scale)
    card.blit(detail_s, (detail_x, height - detail_s.get_height() - int(5 * scale)))

    if stamp_s is not None:
        stamp_in = _compact_lock_ease((age - 0.40) / 0.17)
        stamp_out = max(0.0, min(1.0, (0.30 - life) / 0.30))
        stamp_alpha = max(0.0, min(1.0, stamp_in * (1.0 - stamp_out) * fade))
        if stamp_alpha > 0.001:
            stamp_colors = {
                "COUNTER": ((255, 119, 88), (72, 25, 18)),
                "PUNISH": ((255, 203, 86), (62, 46, 15)),
                "REVERSAL": ((190, 126, 255), (42, 24, 68)),
            }
            stamp_accent, stamp_fill = stamp_colors.get(stamp, (accent, (26, 32, 44)))
            pad_x = max(7, int(8 * scale))
            plate_w = stamp_s.get_width() + pad_x * 2
            plate_h = max(stamp_s.get_height() + int(5 * scale), int(18 * scale))
            plate = pygame.Surface((plate_w + int(8 * scale), plate_h), pygame.SRCALPHA)
            slant = max(4, int(6 * scale))
            pygame.draw.polygon(
                plate,
                (*stamp_fill, int(232 * stamp_alpha)),
                [(slant, 0), (plate_w + slant, 0), (plate_w, plate_h - 1), (0, plate_h - 1)],
            )
            pygame.draw.polygon(
                plate,
                (*stamp_accent, int(230 * stamp_alpha)),
                [(slant, 0), (plate_w + slant, 0), (plate_w, plate_h - 1), (0, plate_h - 1)],
                1,
            )
            stamp_s.set_alpha(int(255 * stamp_alpha))
            plate.blit(stamp_s, (pad_x + slant // 2, (plate_h - stamp_s.get_height()) // 2))
            slam = int((1.0 - stamp_in) * 22 * scale) - int(stamp_out * 18 * scale)
            plate_x = width - plate.get_width() - int(8 * scale) + slam
            plate_y = max(2, int(4 * scale))
            card.blit(plate, (plate_x, plate_y))
    screen.blit(card, (x, y))


def _blit_combo_sheen(card, progress: float, fade: float, heavy: bool = False) -> None:
    """Sweep a clipped additive polish band across the combo card."""
    progress = max(0.0, min(1.0, float(progress)))
    envelope = math.sin(math.pi * progress)
    if envelope <= 0.001:
        return
    width, height = card.get_size()
    band_fraction = 0.24 if heavy else 0.105
    band_w = max(8, int(width * band_fraction))
    travel = width + band_w * 2
    center_x = int(-band_w + progress * travel)
    sheen = pygame.Surface((width, height), pygame.SRCALPHA)
    alpha = int((92 if heavy else 24) * fade * envelope)
    core_alpha = int((148 if heavy else 38) * fade * envelope)
    sheen_rgb = 118 if heavy else 28
    core_rgb = 220 if heavy else 72
    pygame.draw.polygon(
        sheen,
        (sheen_rgb, sheen_rgb, sheen_rgb, alpha),
        [
            (center_x - band_w, 0),
            (center_x + max(2, band_w // 3), 0),
            (center_x - max(2, band_w // 3), height - 1),
            (center_x - band_w * 2, height - 1),
        ],
    )
    pygame.draw.line(
        sheen,
        (core_rgb, core_rgb, core_rgb, core_alpha),
        (center_x, 1),
        (center_x - band_w, height - 2),
        max(1, int(2 if heavy else 1)),
    )
    card.blit(sheen, (0, 0), special_flags=pygame.BLEND_RGBA_ADD)
    if heavy:
        border_alpha = int(68 * fade * envelope)
        pygame.draw.rect(
            card,
            (255, 255, 255, border_alpha),
            card.get_rect(),
            1,
            border_radius=max(3, int(height * 0.16)),
        )


def _blit_combo_milestone(card: pygame.Surface, progress: float, fade: float) -> None:
    progress = max(0.0, min(1.0, float(progress)))
    envelope = math.sin(math.pi * progress)
    if envelope <= 0.001:
        return
    width, height = card.get_size()
    polish = pygame.Surface((width, height), pygame.SRCALPHA)
    band_w = max(22, width // 6)
    center_x = int(-band_w + progress * (width + band_w * 2))
    pygame.draw.polygon(
        polish,
        (255, 230, 142, int(112 * envelope * fade)),
        [(center_x - band_w, 0), (center_x + band_w // 3, 0), (center_x - band_w // 3, height - 1), (center_x - band_w * 2, height - 1)],
    )
    pygame.draw.line(polish, (255, 255, 255, int(190 * envelope * fade)), (center_x, 1), (center_x - band_w, height - 2), 2)
    pygame.draw.rect(polish, (255, 211, 92, int(90 * envelope * fade)), polish.get_rect(), 2, border_radius=max(3, height // 4))
    card.blit(polish, (0, 0), special_flags=pygame.BLEND_RGBA_ADD)


def _draw_combo_ledger(screen, font_sm, team: str, x: int, y: int, width: int, scale: float, is_left: bool) -> None:
    ledger = _combo_ledgers.get(team) or {}
    life = float(ledger.get("life") or 0.0)
    if life <= 0.01:
        return
    attacker_slot = str(ledger.get("attacker_slot") or "")
    if not attacker_slot:
        return

    fade = min(1.0, life * 2.4)
    meter_delta = _snap_int(attacker_slot, "meter") - int(ledger.get("meter_start") or 0)
    baroque_delta = _snap_float(attacker_slot, "baroque_red_pct_max") - float(ledger.get("baroque_start") or 0.0)
    hits = int(ledger.get("hits") or 0)
    damage = int(ledger.get("damage") or 0)
    last_hit = int(ledger.get("last_hit_damage") or 0)

    title = f"COMBO  {hits} HIT{'S' if hits != 1 else ''}  |  {damage:,} DMG"
    if last_hit > 0:
        title += f"  |  LAST {last_hit:,}"

    resource = f"MTR {meter_delta:+,}"
    if abs(baroque_delta) >= 0.5:
        resource += f"  •  BBQ {baroque_delta:+.0f}%"

    detail_lines = [
        str(line)
        for line in (ledger.get("damage_breakdown_lines") or [])
        if str(line).strip()
    ][:4]

    title_s = font_sm.render(title, True, (236, 241, 248))
    resource_s = font_sm.render(
        resource,
        True,
        (124, 188, 255) if meter_delta >= 0 else (255, 174, 104),
    )
    detail_surfaces = [
        font_sm.render(line, True, (185, 202, 224))
        for line in detail_lines
    ]

    pad_x = max(8, int(8 * scale))
    pad_y = max(3, int(3 * scale))
    gap = max(1, int(2 * scale))
    content_surfaces = [title_s, *detail_surfaces, resource_s]
    content_height = sum(surface.get_height() for surface in content_surfaces)
    content_height += gap * max(0, len(content_surfaces) - 1)
    h = max(int(29 * scale), content_height + pad_y * 2)
    w = min(
        width,
        max(
            int(194 * scale),
            max(
                (surface.get_width() for surface in content_surfaces),
                default=0,
            ) + pad_x * 2,
        ),
    )

    draw_x = x if is_left else x + width - w
    slide = int((1.0 - fade) * 8 * scale)
    draw_y = y + slide
    shadow = pygame.Surface((w + 8, h + 8), pygame.SRCALPHA)
    card = pygame.Surface((w, h), pygame.SRCALPHA)
    pygame.draw.rect(
        shadow,
        (0, 0, 0, int(72 * fade)),
        (4, 4, w, h),
        border_radius=max(4, int(5 * scale)),
    )
    pygame.draw.rect(
        card,
        (15, 19, 28, int(224 * fade)),
        card.get_rect(),
        border_radius=max(4, int(5 * scale)),
    )
    pygame.draw.rect(
        card,
        (107, 154, 232, int(190 * fade)),
        card.get_rect(),
        1,
        border_radius=max(3, int(4 * scale)),
    )
    rail_w = max(2, int(3 * scale))
    rail_x = 0 if is_left else w - rail_w
    pygame.draw.rect(
        card,
        (107, 154, 232, int(230 * fade)),
        (rail_x, 0, rail_w, h),
    )

    cursor_y = pad_y
    title_s.set_alpha(int(255 * fade))
    card.blit(title_s, (pad_x, cursor_y))
    cursor_y += title_s.get_height() + gap

    for detail_s in detail_surfaces:
        detail_s.set_alpha(int(242 * fade))
        card.blit(detail_s, (pad_x, cursor_y))
        cursor_y += detail_s.get_height() + gap

    resource_s.set_alpha(int(238 * fade))
    resource_y = min(h - resource_s.get_height() - pad_y, cursor_y)
    card.blit(resource_s, (pad_x, resource_y))

    # A restrained sweep acknowledges each hit. A brighter pass confirms the
    # final combo total before the card fades.
    hit_sheen = float(ledger.get("hit_sheen") or 0.0)
    if hit_sheen > 0.001:
        _blit_combo_sheen(card, 1.0 - hit_sheen, fade, heavy=False)
    final_sheen = float(ledger.get("final_sheen") or 0.0)
    if final_sheen > 0.001:
        _blit_combo_sheen(card, 1.0 - final_sheen, fade, heavy=True)
    milestone_sheen = float(ledger.get("milestone_sheen") or 0.0)
    if milestone_sheen > 0.001:
        _blit_combo_milestone(card, 1.0 - milestone_sheen, fade)

    milestone_scale = max(
        0.0,
        min(1.0, float(ledger.get("milestone_scale") or 0.0)),
    )
    milestone_progress = 1.0 - milestone_scale
    scale_envelope = (
        math.sin(math.pi * milestone_progress)
        if milestone_scale > 0.001
        else 0.0
    )
    scale_factor = 1.0 + 0.075 * scale_envelope
    if scale_factor > 1.001:
        scaled_size = (
            max(1, int(card.get_width() * scale_factor)),
            max(1, int(card.get_height() * scale_factor)),
        )
        card = pygame.transform.smoothscale(card, scaled_size)
        shadow = pygame.transform.smoothscale(
            shadow,
            (scaled_size[0] + 8, scaled_size[1] + 8),
        )
        draw_x -= (scaled_size[0] - w) // 2
        draw_y -= (scaled_size[1] - h) // 2

    screen.blit(shadow, (draw_x - 4, draw_y - 1))
    screen.blit(shadow, (draw_x - 4, draw_y + 1))
    screen.blit(card, (draw_x, draw_y))

def _draw_tag_card(screen, font_sm, team_anim: dict, x: int, y: int, width: int, scale: float, is_left: bool, dt: float) -> None:
    card_data = team_anim.get("tag_card")
    if not isinstance(card_data, dict):
        return
    card_data["life"] = max(0.0, float(card_data.get("life") or 0.0) - dt * 0.62)
    life = float(card_data.get("life") or 0.0)
    if life <= 0.01:
        team_anim["tag_card"] = None
        return
    fade = min(1.0, life * 2.5)
    slide = int((1.0 - fade) * 10 * scale)
    name = _compact_trim(str(card_data.get("name") or "---"), 16)
    hp = _compact_hp_text(card_data.get("cur"), card_data.get("max"))
    meter = _compact_meter_text(card_data.get("meter"))
    bbq = float(card_data.get("bbq") or 0.0)
    try:
        cur_hp = max(0.0, float(card_data.get("cur") or 0.0))
        max_hp = max(1.0, float(card_data.get("max") or 1.0))
        hp_pct = min(1.0, cur_hp / max_hp)
    except Exception:
        hp_pct = 1.0

    if hp_pct <= 0.25:
        hp_state = "CRITICAL"
        accent = (235, 94, 100)
        fill = (52, 20, 27)
        detail_color = (255, 177, 181)
    elif hp_pct <= 0.55:
        hp_state = "CAUTION"
        accent = (239, 192, 83)
        fill = (51, 42, 20)
        detail_color = (255, 225, 144)
    else:
        hp_state = "READY"
        accent = (91, 210, 137)
        fill = (18, 48, 34)
        detail_color = (166, 241, 194)

    title_s = font_sm.render(f"TAG IN  •  {name}", True, (236, 241, 248))
    detail = f"{hp_state}  •  HP {hp}  |  MTR {meter}"
    if bbq > 0.0:
        detail += f"  |  BBQ {bbq:.2f}%"
    detail_s = font_sm.render(detail, True, detail_color)
    h = max(int(28 * scale), title_s.get_height() + detail_s.get_height() + int(7 * scale))
    w = min(width, max(int(185 * scale), title_s.get_width() + int(18 * scale), detail_s.get_width() + int(18 * scale)))
    draw_x = x if is_left else x + width - w
    draw_y = y + slide
    shadow = pygame.Surface((w + 8, h + 8), pygame.SRCALPHA)
    pygame.draw.rect(shadow, (0, 0, 0, int(70 * fade)), (4, 4, w, h), border_radius=max(4, int(5 * scale)))
    card = pygame.Surface((w, h), pygame.SRCALPHA)
    pygame.draw.rect(card, (*fill, int(224 * fade)), card.get_rect(), border_radius=max(4, int(5 * scale)))
    pygame.draw.rect(card, (*accent, int(200 * fade)), card.get_rect(), 1, border_radius=max(3, int(4 * scale)))
    rail_w = max(2, int(3 * scale))
    rail_x = 0 if is_left else w - rail_w
    pygame.draw.rect(card, (*accent, int(238 * fade)), (rail_x, 0, rail_w, h), border_radius=max(2, int(3 * scale)))
    title_s.set_alpha(int(255 * fade)); detail_s.set_alpha(int(245 * fade))
    card.blit(title_s, (int(8 * scale), int(3 * scale)))
    card.blit(detail_s, (int(8 * scale), h - detail_s.get_height() - int(3 * scale)))
    screen.blit(shadow, (draw_x - 4, draw_y + 1))
    screen.blit(card, (draw_x, draw_y))


def _draw_compact_broadcast_splash(panel, width: int, height: int, accent: tuple[int, int, int], is_left: bool, scale: float, alpha: float) -> None:
    """Neutralized: panel background stays black/charcoal, not team-colored."""
    return


def _cached_compact_panel_shell(
    width: int,
    height: int,
    accent: tuple[int, int, int],
    is_left: bool,
    scale: float,
    panel_alpha: float,
) -> tuple[pygame.Surface, pygame.Surface]:
    alpha_bucket = max(1, min(20, int(round(float(panel_alpha) * 20.0))))
    key = (int(width), int(height), tuple(accent), bool(is_left), round(float(scale), 3), alpha_bucket)
    cached = _COMPACT_PANEL_SHELL_CACHE.get(key)
    if cached is not None:
        return cached

    alpha = alpha_bucket / 20.0
    shadow = pygame.Surface((width + int(16 * scale), height + int(16 * scale)), pygame.SRCALPHA)
    panel = pygame.Surface((width, height), pygame.SRCALPHA)
    base_alpha = int(220 * alpha)
    notch = max(8, int(10 * scale))
    if is_left:
        points = [(notch, 0), (width - 1, 0), (width - 1, height - 1), (0, height - 1), (0, notch)]
    else:
        points = [(0, 0), (width - notch - 1, 0), (width - 1, notch), (width - 1, height - 1), (0, height - 1)]

    pygame.draw.polygon(shadow, (0, 0, 0, int(76 * alpha)), [(p[0] + int(8 * scale), p[1] + int(8 * scale)) for p in points])
    pygame.draw.polygon(panel, (16, 21, 28, base_alpha), points)
    for py in range(height):
        blend = py / max(1, height - 1)
        line_color = _lerp_color((60, 68, 80), (12, 16, 22), blend)
        pygame.draw.line(panel, (*line_color, int(172 * alpha)), (1, py), (width - 2, py))

    core_h = max(56, int(60 * scale))
    analysis_y = max(core_h + int(16 * scale), int(78 * scale))
    pygame.draw.rect(panel, (22, 28, 38, int(126 * alpha)), (int(8 * scale), int(8 * scale), width - int(16 * scale), core_h), border_radius=max(6, int(7 * scale)))
    pygame.draw.rect(panel, (8, 12, 18, int(116 * alpha)), (int(8 * scale), analysis_y, width - int(16 * scale), height - analysis_y - int(8 * scale)), border_radius=max(6, int(7 * scale)))
    _draw_compact_broadcast_splash(panel, width, height, accent, is_left, scale, alpha)

    sheen_surface = pygame.Surface((width, height), pygame.SRCALPHA)
    pygame.draw.polygon(sheen_surface, (255, 255, 255, int(18 * alpha)), [
        (int(width * 0.08), 0),
        (int(width * 0.42), 0),
        (int(width * 0.28), height - 1),
        (0, height - 1),
    ])
    pygame.draw.line(sheen_surface, (250, 252, 255, int(26 * alpha)), (int(width * 0.08), 1), (int(width * 0.42), 1), 1)
    panel.blit(sheen_surface, (0, 0))
    pygame.draw.polygon(panel, (*accent, int(170 * alpha)), points, 1)
    pygame.draw.line(panel, (250, 252, 255, int(42 * alpha)), (notch if is_left else 0, 0), (width - 1 if is_left else width - notch - 1, 0))
    pygame.draw.line(panel, (8, 10, 14, int(178 * alpha)), (0, height - 1), (width - 1, height - 1))
    pygame.draw.line(panel, (*accent, int(60 * alpha)), (int(8 * scale), int(74 * scale)), (width - int(8 * scale), int(74 * scale)))
    rail_w = max(4, int(5 * scale))
    rail_x = 0 if is_left else width - rail_w
    pygame.draw.rect(panel, (*accent, int(238 * alpha)), (rail_x, 0, rail_w, height))

    if len(_COMPACT_PANEL_SHELL_CACHE) >= 96:
        _COMPACT_PANEL_SHELL_CACHE.clear()
    cached = (shadow, panel)
    _COMPACT_PANEL_SHELL_CACHE[key] = cached
    return cached



def _compact_baroque_inline_width(font_sm, scale: float, owner_label: str = "") -> int:
    """Width for a character-owned BBQ badge embedded in a health row."""
    owner = str(owner_label or "").strip().upper()
    sample = f"{owner} BBQ 100.00%" if owner else "BBQ 100.00%"
    return max(int(92 * scale), font_sm.size(sample)[0] + max(18, int(22 * scale)))


def _compact_team_meter_value(*snaps: dict | None) -> int:
    """Return the strongest readable team meter sample from available snapshots."""
    best = 0
    for snap in snaps:
        if not isinstance(snap, dict):
            continue
        for key in ("meter", "meter_profile_current"):
            try:
                candidate = int(float(snap.get(key) or 0))
            except Exception:
                candidate = 0
            if candidate > best:
                best = candidate
    return max(0, min(50000, best))


def _draw_compact_meter_rail(
    screen,
    font_sm,
    slot_anim: dict,
    snap: dict,
    left: int,
    y: int,
    right: int,
    scale: float,
    is_dead: bool,
    show_profile_delta: bool = False,
    meter_value: int | float | None = None,
) -> None:
    """Draw the shared team meter as a thin full-width resource rail.

    Meter is team-owned, so it sits between the character rows and combat
    telemetry instead of being visually tucked into C1's header corner. The
    rail preserves the five-stock identity, exact value, stock count, and
    optional generation delta while spending horizontal rather than vertical
    space.
    """
    rail_h = max(6, int(7 * scale))
    rail_side_inset = max(14, int(16 * scale))
    rail = pygame.Rect(left + rail_side_inset, y, max(80, right - left - rail_side_inset * 2), rail_h)
    radius = max(3, int(4 * scale))

    try:
        meter_i = max(0, int(float(meter_value if meter_value is not None else _compact_team_meter_value(snap))))
    except (TypeError, ValueError):
        meter_i = 0
    level = _compact_meter_level(meter_i)
    meter_color = _compact_meter_color(meter_i) if not is_dead else COL_DEAD
    active = meter_i > 0 or level > 0
    idle_tint = 1.0 if active else 0.62

    bg = (10, 14, 21, int(220 * idle_tint))
    border = (61, 76, 98, int(112 * idle_tint))
    pygame.draw.rect(screen, bg, rail, border_radius=radius)
    pygame.draw.rect(screen, border, rail, 1, border_radius=radius)
    pygame.draw.line(screen, (*meter_color[:3], int(74 * idle_tint)), (rail.x + 2, rail.y + 1), (rail.right - 3, rail.y + 1), 1)

    pad = max(5, int(6 * scale))
    gap = max(4, int(5 * scale))
    label_color = COL_DEAD if is_dead else tuple(int(c * idle_tint) for c in (132, 151, 181))
    label = font_sm.render("MTR", True, label_color)
    label_y = rail.centery - label.get_height() // 2
    screen.blit(label, (rail.x + pad, label_y))
    cursor_left = rail.x + pad + label.get_width() + gap

    delta = None
    if show_profile_delta:
        # Reserve a fixed slot for the meter delta so the bar never wiggles as
        # the delta appears, disappears, or changes digit count.
        delta_slot_w = max(
            font_sm.size("+99.9K")[0],
            font_sm.size("-99.9K")[0],
            font_sm.size("+99999")[0],
            font_sm.size("-99999")[0],
        )
        delta_value = _panel_int(snap.get("meter_profile_last_delta"), 0)
        if delta_value:
            meter_match = snap.get("meter_profile_last_match")
            if delta_value < 0:
                delta_color = (255, 171, 92)
            elif meter_match is True:
                delta_color = (92, 218, 154)
            elif meter_match is False:
                delta_color = (255, 112, 120)
            else:
                delta_color = (92, 174, 242)
            delta = font_sm.render(f"{delta_value:+d}", True, delta_color)
            delta_x = cursor_left + max(0, delta_slot_w - delta.get_width())
            screen.blit(delta, (delta_x, rail.centery - delta.get_height() // 2))
        cursor_left += delta_slot_w + gap

    stock = font_sm.render(str(level), True, meter_color)
    exact_text = _compact_meter_text(meter_i)
    meter_value_flash = max(0.0, min(1.0, float(slot_anim.get("meter_value_flash", 0.0))))
    exact_color = COL_DEAD if is_dead else _lerp_color((177, 189, 208), (255, 255, 255), meter_value_flash * 0.85)
    exact = font_sm.render(exact_text, True, exact_color)
    cursor_right = rail.right - pad
    stock_x = cursor_right - stock.get_width()
    screen.blit(stock, (stock_x, rail.centery - stock.get_height() // 2))
    cursor_right = stock_x - gap
    exact_x = cursor_right - exact.get_width()
    if exact_x > cursor_left + max(48, int(64 * scale)):
        screen.blit(exact, (exact_x, rail.centery - exact.get_height() // 2))
        cursor_right = exact_x - gap
    else:
        short = font_sm.render(f"{_compact_short_number(meter_i)}/50K", True, COL_DEAD if is_dead else (177, 189, 208))
        short_x = cursor_right - short.get_width()
        if short_x > cursor_left + max(42, int(54 * scale)):
            screen.blit(short, (short_x, rail.centery - short.get_height() // 2))
            cursor_right = short_x - gap

    bar_x = cursor_left
    bar_right = max(bar_x + max(54, int(72 * scale)), cursor_right)
    bar_w = max(48, bar_right - bar_x - max(6, int(8 * scale)))
    bar_h = max(6, int(7 * scale))
    _draw_compact_meter(
        screen,
        bar_x,
        rail.centery - bar_h // 2,
        bar_w,
        slot_anim.get("meter_display_value", meter_i if meter_i > 0 else _compact_team_meter_value(snap)),
        scale,
        is_dead,
        float(slot_anim.get("meter_spend_sweep", 0.0)),
        int(slot_anim.get("meter_spend_amount", 0) or 0),
        float(slot_anim.get("meter_gain_flash", 0.0)),
        float(slot_anim.get("meter_gain_start", 0.0)),
        float(slot_anim.get("meter_gain_end", 0.0)),
        float(slot_anim.get("meter_stock_pop", 0.0)),
        int(slot_anim.get("meter_stock_pop_index", -1) or -1),
        float(slot_anim.get("meter_max_flash", 0.0)),
    )


def _draw_compact_guard_chip(screen, font_sm, point_anim: dict, right: int, y: int, scale: float) -> int:
    """Move the transient guard result into spare horizontal HUD space."""
    label = str(point_anim.get("guard_indicator_label") or "").strip().upper()
    result = str(point_anim.get("guard_indicator_result") or "").strip().upper()
    life = float(point_anim.get("guard_indicator_life", 0.0) or 0.0)
    if not label or life <= 0.01:
        return right
    if label == "HIGH":
        label = "OVERHEAD"
    elif label in {"UNBLK", "UNBLOCK"}:
        label = "UNBLOCKABLE"
    blocked = result == "BLOCK"
    primary = "BLOCK" if blocked else "ATK HIT"
    color = (92, 232, 146) if blocked else (255, 112, 120)
    chip = _render_compact_text_chip(font_sm, primary, color, scale, alpha=max(0.35, min(1.0, life)), secondary=label)
    x = right - chip.get_width()
    screen.blit(chip, (x, y))
    return x - max(4, int(5 * scale))

def _draw_compact_team_panel(screen, font, font_sm, team: str, slots: dict, scale: float, overlay_alpha: float, dt: float, control=None) -> None:
    first_label, second_label = f"{team}-C1", f"{team}-C2"
    point_label = _get_active_slot(team) or first_label
    partner_label = second_label if point_label == first_label else first_label
    point = slots.get(point_label) or slots.get(first_label) or slots.get(second_label)
    partner = slots.get(partner_label) or {}
    if not point:
        return

    point_anim = _get_slot_anim(point_label)
    partner_anim = _get_slot_anim(partner_label)
    # Primary resource displays are truth indicators. Snap them to the newest
    # realtime snapshot; secondary spend/trail effects remain animated.
    point_anim["meter_display"] = float(_compact_meter_level(point.get("meter")))
    partner_anim["meter_display"] = float(_compact_meter_level(partner.get("meter")))

    try:
        point_cur = max(0, int(point.get("cur") or 0))
        point_max = max(1, int(point.get("max") or 1))
    except (TypeError, ValueError):
        point_cur, point_max = 0, 1
    point_hp_target = max(0.0, min(1.0, point_cur / point_max))
    if point_anim.get("hp_display_frac") is None:
        point_anim["hp_display_frac"] = point_hp_target
    if point_anim.get("hp_trail_frac") is None:
        point_anim["hp_trail_frac"] = point_hp_target
    point_anim["hp_display_frac"] = point_hp_target
    if point_hp_target >= float(point_anim.get("hp_trail_frac") or 0.0):
        point_anim["hp_trail_frac"] = point_hp_target
        point_anim["hp_trail_delay"] = 0.0
    else:
        point_anim["hp_trail_delay"] = max(0.0, float(point_anim.get("hp_trail_delay", 0.0)) - dt)
        if float(point_anim.get("hp_trail_delay", 0.0)) <= 0.0:
            point_anim["hp_trail_frac"] = _approach(float(point_anim.get("hp_trail_frac") or point_hp_target), point_hp_target, 1.25, dt)

    try:
        partner_cur = max(0, int(partner.get("cur") or 0))
        partner_max = max(1, int(partner.get("max") or 1))
    except (TypeError, ValueError):
        partner_cur, partner_max = 0, 1
    partner_hp_target = max(0.0, min(1.0, partner_cur / partner_max))
    if partner_anim.get("hp_display_frac") is None:
        partner_anim["hp_display_frac"] = partner_hp_target
    if partner_anim.get("hp_trail_frac") is None:
        partner_anim["hp_trail_frac"] = partner_hp_target
    partner_anim["hp_display_frac"] = partner_hp_target
    if partner_hp_target >= float(partner_anim.get("hp_trail_frac") or 0.0):
        partner_anim["hp_trail_frac"] = partner_hp_target
        partner_anim["hp_trail_delay"] = 0.0
    else:
        partner_anim["hp_trail_delay"] = max(0.0, float(partner_anim.get("hp_trail_delay", 0.0)) - dt)
        if float(partner_anim.get("hp_trail_delay", 0.0)) <= 0.0:
            partner_anim["hp_trail_frac"] = _approach(float(partner_anim.get("hp_trail_frac") or partner_hp_target), partner_hp_target, 1.15, dt)

    try:
        point_meter_target = max(0.0, float(point.get("meter") or 0.0))
    except (TypeError, ValueError):
        point_meter_target = 0.0
    if point_anim.get("meter_display_value") is None:
        point_anim["meter_display_value"] = point_meter_target
    point_anim["meter_display_value"] = point_meter_target
    point_anim["meter_spend_sweep"] = max(0.0, float(point_anim.get("meter_spend_sweep", 0.0)) - dt * 2.75)

    team_anim = _get_team_anim(team)
    team_meter_target = _compact_team_meter_value(point, partner, slots.get(first_label), slots.get(second_label))
    meter_display = team_anim.get("meter_display_value")
    previous_meter_target = team_anim.get("prev_meter_target")
    if previous_meter_target is not None and abs(float(previous_meter_target) - float(team_meter_target)) > 0.5:
        team_anim["meter_value_flash"] = 1.0
        if float(team_meter_target) > float(previous_meter_target):
            team_anim["meter_gain_flash"] = 1.0
            team_anim["meter_gain_start"] = float(previous_meter_target)
            team_anim["meter_gain_end"] = float(team_meter_target)
            old_level = int(max(0.0, float(previous_meter_target)) // 10000.0)
            new_level = int(max(0.0, float(team_meter_target)) // 10000.0)
            if new_level > old_level:
                team_anim["meter_stock_pop"] = 1.0
                team_anim["meter_stock_pop_index"] = min(4, max(0, new_level - 1))
            if float(team_meter_target) >= 50000.0 and float(previous_meter_target) < 50000.0:
                team_anim["meter_max_flash"] = 1.0
    team_anim["prev_meter_target"] = float(team_meter_target)
    team_anim["meter_gain_flash"] = max(0.0, float(team_anim.get("meter_gain_flash", 0.0)) - dt * 7.0)
    team_anim["meter_stock_pop"] = max(0.0, float(team_anim.get("meter_stock_pop", 0.0)) - dt * 6.0)
    team_anim["meter_max_flash"] = max(0.0, float(team_anim.get("meter_max_flash", 0.0)) - dt * 3.8)
    team_anim["meter_value_flash"] = max(0.0, float(team_anim.get("meter_value_flash", 0.0)) - dt * 7.5)
    if meter_display is None:
        meter_display = float(team_meter_target)
        team_anim["meter_display_value"] = meter_display
        team_anim["meter_drain_target"] = float(team_meter_target)
        team_anim["meter_drain_speed"] = 0.0
    else:
        meter_display = float(meter_display)
        meter_target_f = float(team_meter_target)
        if meter_target_f >= meter_display:
            # Gains remain realtime. The bar fills immediately from the newest
            # native meter sample instead of easing behind the game state.
            team_anim["meter_display_value"] = meter_target_f
            team_anim["meter_drain_target"] = meter_target_f
            team_anim["meter_drain_speed"] = 0.0
        else:
            prior_target = float(team_anim.get("meter_drain_target", meter_display) or meter_display)
            if abs(prior_target - meter_target_f) > 0.5:
                # A spend is an instantaneous game-state change, but the HUD
                # drains it visually over ~0.18s. Large spends cross multiple
                # stocks smoothly instead of popping whole pips away.
                spend_amount = max(0.0, meter_display - meter_target_f)
                team_anim["meter_drain_target"] = meter_target_f
                team_anim["meter_drain_speed"] = max(60000.0, spend_amount / 0.18)
            drain_speed = max(60000.0, float(team_anim.get("meter_drain_speed", 60000.0) or 60000.0))
            team_anim["meter_display_value"] = _approach(meter_display, meter_target_f, drain_speed, dt)
            if float(team_anim["meter_display_value"]) <= meter_target_f + 0.5:
                team_anim["meter_display_value"] = meter_target_f
                team_anim["meter_drain_speed"] = 0.0
    team_anim["meter_spend_sweep"] = max(
        max(0.0, float(team_anim.get("meter_spend_sweep", 0.0))),
        max(0.0, float(point_anim.get("meter_spend_sweep", 0.0))),
        max(0.0, float(partner_anim.get("meter_spend_sweep", 0.0))),
    )
    team_anim["meter_spend_amount"] = max(
        int(team_anim.get("meter_spend_amount", 0) or 0),
        int(point_anim.get("meter_spend_amount", 0) or 0),
        int(partner_anim.get("meter_spend_amount", 0) or 0),
    )
    team_anim["meter_spend_sweep"] = max(0.0, float(team_anim.get("meter_spend_sweep", 0.0)) - dt * 2.75)
    _tick_team_panel_fx(team_anim, dt)
    hold_items = _display_button_holds(point_anim, _frame)
    hold_target = 1.0 if hold_items else 0.0
    team_anim["hold_expand"] = _approach(
        float(team_anim.get("hold_expand", 0.0)),
        hold_target,
        7.5 if hold_target else 4.5,
        dt,
    )
    hold_expand = max(0.0, min(1.0, float(team_anim.get("hold_expand", 0.0))))

    hud_info_set = str(getattr(control, "hud_info_set", "CUSTOM") or "CUSTOM").strip().upper() if control is not None else "CUSTOM"
    core_telemetry = hud_info_set == "CORE"
    show_damage_inline = bool(core_telemetry or (control is not None and getattr(control, "show_damage_badge", False)))
    show_untech_inline = bool(core_telemetry or (control is not None and getattr(control, "show_untech_panel", False)))
    show_meter_inline = bool(control is not None and getattr(control, "show_meter_panel", False))
    show_red_inline = bool(control is not None and getattr(control, "show_red_health_panel", False))
    show_attack_inline = bool(control is not None and getattr(control, "show_attack_property_panel", False))
    research_row_height = _compact_attack_badge_height(team, font_sm, scale) if show_attack_inline else 0
    research_gap = max(1, int(2 * scale)) if show_attack_inline else 0
    research_layout_extra = research_row_height + research_gap
    # Move timing is a core two-row HUD block:
    #   MOVE TIMING -> a dedicated full-runway timeline
    #   PHASE       -> compact numeric badges only
    # Reserve both rows at all times so the panel never jumps when a move starts.
    frame_data_row_h = max(font_sm.get_height() + max(5, int(6 * scale)), int(17 * scale))
    frame_data_inner_gap = max(2, int(3 * scale))
    frame_data_gap = max(2, int(3 * scale))
    frame_data_layout_extra = frame_data_row_h * 2 + frame_data_inner_gap + frame_data_gap
    compact_metric_rail_h = max(font_sm.get_height() + 4, int(17 * scale))
    meter_rail_height = max(6, int(7 * scale))
    meter_rail_gap = max(3, int(4 * scale))
    meter_rail_layout_extra = meter_rail_height + meter_rail_gap
    damage_scale_height = compact_metric_rail_h
    untech_scale_height = compact_metric_rail_h
    damage_scale_layout_extra = damage_scale_height + max(3, int(4 * scale)) if show_damage_inline else 0
    untech_scale_layout_extra = untech_scale_height + max(3, int(4 * scale)) if show_untech_inline else 0
    scaling_layout_extra = damage_scale_layout_extra + untech_scale_layout_extra

    base_width = max(442, int(486 * scale))
    # Spend spare screen width on MOVE/history before compressing diagnostics.
    responsive_cap = min(max(base_width, int(620 * scale)), max(base_width, int(screen.get_width() * 0.36)))
    width = max(base_width, responsive_cap)
    collapsed_height = max(154, int(166 * scale)) + meter_rail_layout_extra + frame_data_layout_extra + research_layout_extra + scaling_layout_extra
    hold_extra_height = max(18, int(20 * scale))
    height = collapsed_height + int(hold_extra_height * hold_expand)
    # Keep both compact team panels flush with the game viewport corners.
    # Their entrance animation still begins outside the corresponding edge.
    margin_x = 0
    base_y = 0
    is_left = team == "P1"
    base_x = 0 if is_left else screen.get_width() - width
    accent = SLOT_COLORS.get(point_label, SLOT_COLORS[f"{team}-C1"])
    point_dead = int(point.get("cur") or 0) <= 0
    partner_dead = int(partner.get("cur") or 0) <= 0
    for anim in (point_anim, partner_anim):
        anim["hp_value_flash"] = max(0.0, float(anim.get("hp_value_flash", 0.0)) - dt * 7.5)
        anim["baroque_change_flash"] = max(0.0, float(anim.get("baroque_change_flash", 0.0)) - dt * 6.0)
        anim["move_change_flash"] = max(0.0, float(anim.get("move_change_flash", 0.0)) - dt * 7.0)
        anim["stun_generation_flash"] = max(0.0, float(anim.get("stun_generation_flash", 0.0)) - dt * 8.0)
        anim["stun_expire_flash"] = max(0.0, float(anim.get("stun_expire_flash", 0.0)) - dt * 9.0)
        anim["bs_generation_flash"] = max(0.0, float(anim.get("bs_generation_flash", 0.0)) - dt * 8.0)
        anim["bs_expire_flash"] = max(0.0, float(anim.get("bs_expire_flash", 0.0)) - dt * 9.0)
    if point_dead and not bool(point_anim.get("prev_dead", False)):
        point_anim["ko_punch"] = 1.0
    if partner_dead and not bool(partner_anim.get("prev_dead", False)):
        partner_anim["ko_punch"] = 1.0
    point_anim["prev_dead"] = point_dead
    partner_anim["prev_dead"] = partner_dead
    point_anim["ko_punch"] = max(0.0, float(point_anim.get("ko_punch", 0.0)) - dt * 5.5)
    partner_anim["ko_punch"] = max(0.0, float(partner_anim.get("ko_punch", 0.0)) - dt * 5.5)
    point_anim["ko_alpha"] = _approach(float(point_anim.get("ko_alpha", 0.0)), 1.0 if point_dead else 0.0, 6.5 if point_dead else 3.2, dt)
    point_anim["ko_scale"] = 1.0 + 0.14 * float(point_anim.get("ko_punch", 0.0)) if point_dead else _approach(float(point_anim.get("ko_scale", 0.90)), 0.92, 4.0, dt)

    team_present = bool(_get_slot_anim(first_label).get("present") or _get_slot_anim(second_label).get("present"))
    if team_present and not team_anim.get("present"):
        team_anim["entrance_age"] = -0.07 if not is_left else 0.0
        team_anim["entrance_active"] = True
        team_anim["slide_y"] = 10.0
        team_anim["slide_x"] = -float(width + margin_x + 28) if is_left else float(width + margin_x + 28)
        team_anim["alpha"] = 0.0
    if team_present:
        prior_point = team_anim.get("current_point_label")
        if prior_point is None:
            team_anim["current_point_label"] = point_label
        elif prior_point != point_label:
            team_anim["current_point_label"] = point_label
            team_anim["swap_progress"] = 1.0
            team_anim["tag_lock_pending"] = True
            team_anim["tag_lock_flash"] = 0.0
            team_anim["tag_card"] = {
                "name": str(point.get("name") or "---"),
                "cur": point.get("cur"),
                "max": point.get("max"),
                "meter": point.get("meter"),
                "bbq": point.get("baroque_red_pct_max"),
                "life": 1.0,
            }
    team_anim["present"] = team_present
    if team_present:
        entrance_active = bool(team_anim.get("entrance_active", False))
        if entrance_active:
            entrance_age = float(team_anim.get("entrance_age", 0.0)) + dt
            team_anim["entrance_age"] = entrance_age
            shell_age = max(0.0, entrance_age - 0.12)
            shell_progress = _compact_lock_ease(shell_age / 0.48)
            shell_alpha = max(0.0, min(1.0, shell_progress))
            shell_travel = float(width + margin_x + 28)
            start_x = -shell_travel if is_left else shell_travel
            team_anim["slide_x"] = start_x * (1.0 - shell_progress)
            team_anim["slide_y"] = 10.0 * (1.0 - shell_alpha)
            team_anim["alpha"] = shell_alpha
            if entrance_age >= 1.32:
                team_anim["entrance_active"] = False
                team_anim["slide_x"] = 0.0
                team_anim["slide_y"] = 0.0
                team_anim["alpha"] = 1.0
        else:
            team_anim["slide_y"] = _approach(float(team_anim.get("slide_y", 0.0)), 0.0, 176.0, dt)
            team_anim["slide_x"] = _approach(float(team_anim.get("slide_x", 0.0)), 0.0, 1800.0, dt)
            team_anim["alpha"] = _approach(float(team_anim.get("alpha", 0.0)), 1.0, 4.8, dt)
        team_anim["swap_progress"] = _approach(float(team_anim.get("swap_progress", 0.0)), 0.0, 4.8, dt)
        if bool(team_anim.get("tag_lock_pending", False)) and float(team_anim.get("swap_progress", 0.0)) <= 0.08:
            team_anim["tag_lock_pending"] = False
            team_anim["tag_lock_flash"] = 1.0
    else:
        off_target = -float(width + margin_x + 18) if is_left else float(width + margin_x + 18)
        team_anim["slide_x"] = _approach(float(team_anim.get("slide_x", 0.0)), off_target, 1700.0, dt)
        team_anim["alpha"] = _approach(float(team_anim.get("alpha", 0.0)), 0.0, 3.2, dt)

    panel_alpha = overlay_alpha * float(team_anim.get("alpha", 0.0))
    if panel_alpha <= 0.01:
        return
    shake = float(team_anim.get("shake", 0.0))
    shake_dir = -1.0 if is_left else 1.0
    shake_x = math.sin(time.time() * 42.0) * (1.0 * shake) * shake_dir
    shake_y = math.sin(time.time() * 28.0 + (0.8 if is_left else 1.4)) * (0.5 * shake)
    impact_age = max(0.0, min(1.0, float(team_anim.get("impact_recoil_age", 1.0))))
    impact_curve = math.sin(math.pi * impact_age) * (1.0 - 0.28 * impact_age) if impact_age < 1.0 else 0.0
    impact_x = shake_dir * float(team_anim.get("impact_recoil_power", 0.0)) * impact_curve
    x = int(base_x + float(team_anim.get("slide_x", 0.0)) + shake_x + impact_x)
    y = int(base_y + float(team_anim.get("slide_y", 0.0)) + shake_y)

    shadow, panel = _cached_compact_panel_shell(width, height, accent, is_left, scale, panel_alpha)
    screen.blit(shadow, (x - int(4 * scale), y + int(4 * scale)))
    screen.blit(panel, (x, y))
    _draw_team_panel_fx(screen, team_anim, x, y, width, height, accent, scale, panel_alpha)

    root_screen = screen
    panel_x, panel_y = x, y
    content_extra_h = max(86, int(92 * scale))
    screen = pygame.Surface((width, height + content_extra_h), pygame.SRCALPHA)
    x, y = 0, 0

    outer_pad = max(7, int(8 * scale))
    left = x + outer_pad
    right = x + width - outer_pad
    # Two complete character rows sit together at the top of the panel:
    # badge + name + health + state.  LOG/MOVES begin only after both rows,
    # so a C2 marker never becomes visually detached from its character.
    primary_y = y + max(5, int(6 * scale))
    hp_y = primary_y + max(15, int(17 * scale)) + max(2, int(3 * scale))
    secondary_y = hp_y + max(7, int(8 * scale)) + max(4, int(5 * scale))
    partner_hp_y = secondary_y + font_sm.get_height() + max(1, int(1 * scale))
    meter_rail_y = partner_hp_y + max(5, int(6 * scale)) + max(2, int(3 * scale))
    damage_scale_y = meter_rail_y + meter_rail_layout_extra
    untech_scale_y = damage_scale_y + (damage_scale_layout_extra if show_damage_inline else 0)
    # Frame data belongs with the live combat clocks. Place it immediately
    # beneath the STUN rail (or beneath the last enabled scaling rail) instead
    # of burying it under INPUTS / FRAMES. Its reserved height is constant.
    frame_data_y = meter_rail_y + meter_rail_layout_extra + scaling_layout_extra + int(1 * scale)
    strip_y = frame_data_y + frame_data_layout_extra
    history_y = strip_y + max(17, int(18 * scale)) + int(3 * scale)
    input_y = history_y + max(12, int(13 * scale)) + int(2 * scale)
    input_row_h = max(15, int(17 * scale))
    frames_y = input_y + input_row_h + int(2 * scale)
    frames_row_h = max(13, int(15 * scale))
    research_y = frames_y + frames_row_h + int(2 * scale)
    hold_y = research_y + research_layout_extra
    hold_row_h = max(15, int(17 * scale))
    move_history_y = hold_y + int((hold_row_h + int(2 * scale)) * hold_expand)
    # Preserve the old tag-swap motion: when the active point changes, the
    # incoming fighter rises from the reserve row while the outgoing fighter
    # drops into it.  The C1/C2 badges travel with their full character rows.
    swap_progress = float(team_anim.get("swap_progress", 0.0))
    row_distance = secondary_y - primary_y
    hp_distance = partner_hp_y - hp_y
    top_row_y = int(primary_y + row_distance * swap_progress)
    top_hp_y = int(hp_y + hp_distance * swap_progress)
    bottom_row_y = int(secondary_y - row_distance * swap_progress)
    bottom_hp_y = int(partner_hp_y - hp_distance * swap_progress)
    top_row_alpha = max(0.55, 1.0 - 0.18 * swap_progress)
    bottom_row_alpha = max(0.55, 1.0 - 0.18 * swap_progress)
    forward_sign = 1 if is_left else -1
    incoming_row_dx = int(forward_sign * 18 * scale * swap_progress)
    outgoing_row_dx = int(-forward_sign * 14 * scale * swap_progress)

    # Character identity stays attached to each character row.  C1 and C2 are
    # still directly one under the other, but each now carries its own name,
    # health, and status instead of leaving C2 stranded above the LOG/MOVES area.
    point_badge = "C1" if point_label.endswith("C1") else "C2"
    partner_badge = "C1" if partner_label.endswith("C1") else "C2"
    partner_color = SLOT_COLORS.get(partner_label, accent)
    badge_w = max(22, int(25 * scale))
    badge_h = max(16, int(18 * scale))
    badge_radius = max(2, int(2 * scale))

    badge_rect = pygame.Rect(left + incoming_row_dx, top_row_y, badge_w, badge_h)
    pygame.draw.rect(screen, accent, badge_rect, border_radius=badge_radius)
    badge = font_sm.render(point_badge, True, (250, 250, 252))
    screen.blit(badge, (badge_rect.centerx - badge.get_width() // 2, badge_rect.centery - badge.get_height() // 2))
    tag_lock_flash = max(0.0, min(1.0, float(team_anim.get("tag_lock_flash", 0.0))))
    if tag_lock_flash > 0.001:
        lock_rect = badge_rect.inflate(max(4, int(6 * scale)), max(4, int(5 * scale)))
        pygame.draw.rect(screen, (255, 255, 255, int(130 * tag_lock_flash)), lock_rect, 1, border_radius=badge_radius + 2)
        pygame.draw.line(screen, (*accent, int(190 * tag_lock_flash)), (lock_rect.left, lock_rect.top), (lock_rect.right, lock_rect.top), max(1, int(2 * scale)))

    partner_badge_rect = pygame.Rect(left + outgoing_row_dx, bottom_row_y, badge_w, badge_h)
    partner_fill = tuple(max(24, int(channel * 0.24)) for channel in partner_color)
    pygame.draw.rect(screen, partner_fill, partner_badge_rect, border_radius=badge_radius)
    pygame.draw.rect(screen, partner_color, partner_badge_rect, 1, border_radius=badge_radius)
    partner_badge_surface = font_sm.render(partner_badge, True, (183, 193, 210))
    screen.blit(
        partner_badge_surface,
        (
            partner_badge_rect.centerx - partner_badge_surface.get_width() // 2,
            partner_badge_rect.centery - partner_badge_surface.get_height() // 2,
        ),
    )

    name_x = badge_rect.right + max(5, int(6 * scale))
    show_point_baroque = _update_compact_baroque_anim(point_anim, point, point_dead, dt)
    show_partner_baroque = _update_compact_baroque_anim(partner_anim, partner, partner_dead, dt)

    # Character identity gets the full header runway. Team meter lives on its
    # own thin shared rail beneath both health rows.
    name_available = max(36, right - name_x - max(5, int(7 * scale)))
    name = _compact_fit_text(font, str(point.get("name") or "???"), name_available)
    name_surface = font.render(name, True, COL_DEAD if point_dead else (235, 238, 245))
    name_surface.set_alpha(int(255 * top_row_alpha))
    screen.blit(name_surface, (name_x, top_row_y - max(0, int(1 * scale))))

    # Fixed numeric HP column makes point/reserve values visually scan as one
    # column even when the health bars themselves resize around BBQ.
    hp_h = max(6, int(7 * scale))
    point_red_values = _compact_red_health_values(point)
    point_auxiliary = point_red_values[1]
    point_recoverable = point_red_values[2]
    point_hp_label = _compact_hp_text(point.get("cur"), point.get("max"))
    hp_flash = max(0.0, min(1.0, float(point_anim.get("hp_value_flash", 0.0))))
    hp_color = COL_DEAD if point_dead else _lerp_color((182, 192, 208), (255, 255, 255), hp_flash * 0.90)
    hp_text = font_sm.render(point_hp_label, True, hp_color)
    point_red_text = None
    if show_red_inline and point_recoverable > 0:
        point_red_text = font_sm.render(f" +{_compact_short_number(point_recoverable)}", True, (246, 94, 128))
    red_text_w = point_red_text.get_width() if point_red_text is not None else 0
    hp_number_col_w = max(font_sm.size("99999/99999")[0] + (font_sm.size(" +9999")[0] if show_red_inline else 0), hp_text.get_width() + red_text_w)

    point_bbq_w = _compact_baroque_inline_width(font_sm, scale, point_badge) if show_point_baroque else 0
    bbq_gap = max(4, int(5 * scale))
    point_bbq_x = right - point_bbq_w if point_bbq_w else right
    point_value_right = point_bbq_x - (bbq_gap if point_bbq_w else 0)
    point_value_x = point_value_right - hp_number_col_w
    hp_gap = max(4, int(5 * scale))
    hp_w = max(72, point_value_x - hp_gap - name_x)
    _draw_compact_health(
        screen,
        name_x,
        top_hp_y,
        hp_w,
        hp_h,
        point.get("cur"),
        point.get("max"),
        point_dead,
        point_anim.get("hp_display_frac"),
        point_anim.get("hp_trail_frac"),
        (point_auxiliary / max(1.0, float(point.get("max") or 1))) if show_red_inline else None,
    )
    hp_text_x = point_value_right - hp_text.get_width() - red_text_w
    hp_text.set_alpha(int(255 * top_row_alpha))
    screen.blit(hp_text, (hp_text_x, top_hp_y - max(1, int(2 * scale))))
    if point_red_text is not None:
        point_red_text.set_alpha(int(255 * top_row_alpha))
        screen.blit(point_red_text, (hp_text_x + hp_text.get_width(), top_hp_y - max(1, int(2 * scale))))
    if show_point_baroque:
        point_bq_rect = pygame.Rect(point_bbq_x, top_hp_y - max(4, int(5 * scale)), point_bbq_w, max(14, int(16 * scale)))
        _draw_compact_baroque_badge(
            screen,
            font_sm,
            point_bq_rect,
            float(point_anim.get("baroque_display_pct", point.get("baroque_red_pct_max") or 0.0)),
            scale,
            is_left,
            float(point_anim.get("baroque_alpha", 0.0)),
            int(point_anim.get("baroque_fade_direction", 0) or 0),
            point_badge,
            float(point_anim.get("baroque_change_flash", 0.0)),
        )
    if point_dead:
        ko_w = max(22, font_sm.size("KO")[0] + int(10 * scale))
        ko_h = max(14, int(16 * scale))
        ko_x = max(name_x, point_value_x - ko_w - max(4, int(5 * scale)))
        _draw_compact_ko_badge(screen, font_sm, pygame.Rect(ko_x, top_hp_y - int(4 * scale), ko_w, ko_h), scale, float(point_anim.get("ko_alpha", 1.0)), float(point_anim.get("ko_scale", 1.0)))

    for entry in point_anim.get("event_history", []):
        entry["life"] = max(0.30, float(entry.get("life", 1.0)) - 0.003)
    point_anim["event_history"] = point_anim.get("event_history", [])[:6]

    action_label = _compact_move_label(point)
    info_right = _draw_compact_guard_chip(screen, font_sm, point_anim, right, strip_y, scale)
    _draw_compact_info_strip(screen, font_sm, point_anim, left, strip_y, info_right, action_label, scale)
    log_items = point_anim.get("event_history", [])[:4]
    log_signature = tuple(f"{str(item.get('label') or '').strip()} {str(item.get('value') or '').strip()}".strip() for item in log_items if item)
    previous_log_signature = tuple(team_anim.get("log_history_signature", ()))
    if log_signature != previous_log_signature:
        if previous_log_signature:
            team_anim["log_history_prev"] = [
                {"label": (txt.split(' ', 1)[0] if ' ' in txt else txt), "value": (txt.split(' ', 1)[1] if ' ' in txt else ''), "color": (196, 205, 220), "life": 1.0}
                for txt in previous_log_signature
            ]
            team_anim["log_history_slide"] = 0.52
        team_anim["log_history_signature"] = log_signature
    team_anim["log_history_slide"] = _approach(float(team_anim.get("log_history_slide", 0.0)), 0.0, 6.6, dt)
    _draw_compact_history_line(screen, font_sm, "LOG", log_items, left, history_y, right, scale, team_anim.get("log_history_prev", []), float(team_anim.get("log_history_slide", 0.0)))
    # Keep the raw input tail in chronological order. The grouped renderer
    # builds commands from oldest to newest, then places the newest group first.
    input_chips = _display_input_chips(point_anim, _frame)
    input_signature = tuple("|".join(chip.get("tokens", [])) for chip in input_chips)
    previous_input_signature = tuple(team_anim.get("input_history_signature", ()))
    if input_signature != previous_input_signature:
        if previous_input_signature:
            team_anim["input_history_prev"] = [dict(chip) for chip in input_chips[1:]]
            team_anim["input_history_slide"] = 0.28
        team_anim["input_history_signature"] = input_signature
    team_anim["input_history_current"] = [dict(chip) for chip in input_chips]
    team_anim["input_history_slide"] = _approach(float(team_anim.get("input_history_slide", 0.0)), 0.0, 22.0, dt)
    _draw_compact_input_history(
        screen,
        font_sm,
        input_chips,
        left,
        input_y,
        right,
        scale,
        team_anim.get("input_history_prev", []),
        float(team_anim.get("input_history_slide", 0.0)),
    )

    if show_attack_inline:
        _draw_attack_property_badge(
            screen,
            font_sm,
            team,
            left,
            research_y,
            right,
            research_row_height,
            scale,
        )

    hold_signature = tuple(str(item.get("id") or "") for item in hold_items)
    previous_hold_signature = tuple(team_anim.get("hold_history_signature", ()))
    if hold_signature != previous_hold_signature:
        if previous_hold_signature:
            team_anim["hold_history_prev"] = [
                dict(item) for item in team_anim.get("hold_history_current", [])
            ]
            team_anim["hold_history_slide"] = 0.50
        team_anim["hold_history_signature"] = hold_signature
    team_anim["hold_history_current"] = [dict(item) for item in hold_items]
    team_anim["hold_history_slide"] = _approach(
        float(team_anim.get("hold_history_slide", 0.0)),
        0.0,
        6.6,
        dt,
    )
    if hold_expand > 0.03:
        faded_hold_items = []
        for item in hold_items:
            entry = dict(item)
            entry["alpha"] = float(entry.get("alpha", 1.0)) * hold_expand
            faded_hold_items.append(entry)
        _draw_compact_hold_history(
            screen,
            font_sm,
            faded_hold_items,
            left,
            hold_y,
            right,
            scale,
            team_anim.get("hold_history_prev", []),
            float(team_anim.get("hold_history_slide", 0.0)),
        )

    merged_moves = _merge_move_history(point_anim.get("move_events", []), partner_anim.get("move_events", []))
    move_signature = tuple(str(item.get("text") or "").strip() for item in merged_moves[:5] if str(item.get("text") or "").strip())
    if move_signature != tuple(team_anim.get("move_history_signature", ())):
        team_anim["move_history_signature"] = move_signature
    # No positional history tween here. Native action edges appear at their
    # final location on the first render that sees them; change emphasis is
    # handled by the separate flash/pulse path.
    team_anim["move_history_prev"] = []
    team_anim["move_history_slide"] = 0.0
    _draw_compact_move_history(
        screen,
        font_sm,
        merged_moves,
        left,
        move_history_y,
        right,
        scale,
        None,
        0.0,
    )

    partner_anim["ko_alpha"] = _approach(float(partner_anim.get("ko_alpha", 0.0)), 1.0 if partner_dead else 0.0, 6.5 if partner_dead else 3.2, dt)
    partner_anim["ko_scale"] = 1.0 + 0.14 * float(partner_anim.get("ko_punch", 0.0)) if partner_dead else _approach(float(partner_anim.get("ko_scale", 0.90)), 0.92, 4.0, dt)

    # Reserve row: identity, state, health, and character-owned BBQ stay together.
    partner_state = _compact_partner_state(partner)
    state_color = (255, 112, 120) if partner_dead else (partner_color if partner_state == "ACTIVE" else (112, 124, 144))
    state_surface = font_sm.render(partner_state, True, state_color)
    state_x = right - state_surface.get_width()
    state_surface.set_alpha(int(255 * bottom_row_alpha))
    screen.blit(state_surface, (state_x, bottom_row_y))

    partner_name_x = partner_badge_rect.right + max(5, int(6 * scale))
    partner_name_available = max(36, state_x - partner_name_x - max(6, int(8 * scale)))
    partner_name = _compact_fit_text(font_sm, str(partner.get("name") or "---"), partner_name_available)
    partner_name_surface = font_sm.render(partner_name, True, COL_DEAD if partner_dead else (168, 177, 194))
    partner_name_surface.set_alpha(int(255 * bottom_row_alpha))
    screen.blit(partner_name_surface, (partner_name_x, bottom_row_y))

    partner_red_values = _compact_red_health_values(partner)
    partner_auxiliary = partner_red_values[1]
    partner_recoverable = partner_red_values[2]
    partner_hp_label = _compact_hp_text(partner.get("cur"), partner.get("max"))
    partner_hp_flash = max(0.0, min(1.0, float(partner_anim.get("hp_value_flash", 0.0))))
    partner_hp_color = COL_DEAD if partner_dead else _lerp_color((150, 161, 180), (255, 255, 255), partner_hp_flash * 0.90)
    partner_hp_text = font_sm.render(partner_hp_label, True, partner_hp_color)
    partner_red_text = None
    if show_red_inline and partner_recoverable > 0:
        partner_red_text = font_sm.render(f" +{_compact_short_number(partner_recoverable)}", True, (246, 94, 128))
    partner_red_text_w = partner_red_text.get_width() if partner_red_text is not None else 0
    partner_number_col_w = max(font_sm.size("99999/99999")[0] + (font_sm.size(" +9999")[0] if show_red_inline else 0), partner_hp_text.get_width() + partner_red_text_w)
    partner_bbq_w = _compact_baroque_inline_width(font_sm, scale, partner_badge) if show_partner_baroque else 0
    partner_bbq_x = right - partner_bbq_w if partner_bbq_w else right
    partner_value_right = partner_bbq_x - (max(4, int(5 * scale)) if partner_bbq_w else 0)
    partner_value_x = partner_value_right - partner_number_col_w
    partner_hp_w = max(54, partner_value_x - max(4, int(5 * scale)) - partner_name_x)
    _draw_compact_health(
        screen, partner_name_x, bottom_hp_y, partner_hp_w, max(4, int(5 * scale)),
        partner.get("cur"), partner.get("max"), partner_dead, partner_anim.get("hp_display_frac"),
        partner_anim.get("hp_trail_frac"),
        (partner_auxiliary / max(1.0, float(partner.get("max") or 1))) if show_red_inline else None,
    )
    partner_hp_text_x = partner_value_right - partner_hp_text.get_width() - partner_red_text_w
    partner_hp_text.set_alpha(int(255 * bottom_row_alpha))
    screen.blit(partner_hp_text, (partner_hp_text_x, bottom_hp_y - max(1, int(2 * scale))))
    if partner_red_text is not None:
        partner_red_text.set_alpha(int(255 * bottom_row_alpha))
        screen.blit(partner_red_text, (partner_hp_text_x + partner_hp_text.get_width(), bottom_hp_y - max(1, int(2 * scale))))
    if show_partner_baroque:
        partner_bq_rect = pygame.Rect(partner_bbq_x, bottom_hp_y - max(4, int(5 * scale)), partner_bbq_w, max(14, int(16 * scale)))
        _draw_compact_baroque_badge(
            screen,
            font_sm,
            partner_bq_rect,
            float(partner_anim.get("baroque_display_pct", partner.get("baroque_red_pct_max") or 0.0)),
            scale,
            is_left,
            float(partner_anim.get("baroque_alpha", 0.0)),
            int(partner_anim.get("baroque_fade_direction", 0) or 0),
            partner_badge,
            float(partner_anim.get("baroque_change_flash", 0.0)),
        )
    if partner_dead:
        partner_ko_w = max(22, font_sm.size("KO")[0] + int(10 * scale))
        ko_x = max(partner_name_x, partner_value_x - partner_ko_w - max(4, int(5 * scale)))
        _draw_compact_ko_badge(
            screen, font_sm, pygame.Rect(ko_x, bottom_hp_y - int(4 * scale), partner_ko_w, max(13, int(15 * scale))),
            scale, float(partner_anim.get("ko_alpha", 1.0)), float(partner_anim.get("ko_scale", 1.0))
        )
    _draw_compact_meter_rail(
        screen,
        font_sm,
        team_anim,
        point,
        left,
        meter_rail_y,
        right,
        scale,
        bool(point_dead and partner_dead),
        show_profile_delta=show_meter_inline,
        meter_value=team_meter_target,
    )
    if show_damage_inline:
        _draw_compact_damage_scaling_rows(
            screen,
            font_sm,
            team,
            point_label,
            point,
            partner_label,
            partner,
            left,
            damage_scale_y,
            right,
            scale,
            dt,
        )
    # Build one shared native race before drawing either rail.  STUN and MOVE
    # TIMING now consume the same action instance, stun generation, and authored
    # active windows.  That lets the STUN gauge project startup/active directly
    # into its own clock instead of asking the user to compare two unrelated
    # normalized meters.
    race_clock = _realtime_hs_contact_clock(point_anim, point)
    if isinstance(race_clock, dict):
        race_clock = dict(race_clock)
        race_clock["kind"] = "AIR HS" if str(race_clock.get("clock_source") or "") == "untech" else "HS"
    if not isinstance(race_clock, dict) or not bool(race_clock.get("active", False)):
        block_race = _realtime_blockstun_contact_clock(point_anim, point)
        if isinstance(block_race, dict) and bool(block_race.get("active", False)):
            race_clock = dict(block_race)
            race_clock["kind"] = "BS"

    move_timing_state = _compact_frame_data_state(point or {})
    pressure_race = _update_compact_move_timing_race(
        move_timing_state,
        point or {},
        race_clock if isinstance(race_clock, dict) else None,
        point_anim,
    )
    punish_race = _update_compact_punish_race(
        move_timing_state,
        point or {},
        point_anim,
    )
    # A defender-owned punish attempt is more specific than the generic
    # pressure/link race, so let it own the MOVE TIMING verdict for that action.
    shared_race = punish_race if isinstance(punish_race, dict) else pressure_race

    if show_untech_inline:
        _draw_compact_untech_scaling_row(
            screen,
            font_sm,
            team,
            point,
            left,
            untech_scale_y,
            right,
            scale,
            dt,
            move_prediction=shared_race if isinstance(shared_race, dict) else None,
        )

    # Render the move clock directly after the live stun/scaling rails. The
    # action frame is refreshed through the realtime combat sidecar at native
    # game-frame cadence, independently of the slower full HUD payload.
    _draw_compact_frame_data_bar(
        screen,
        font_sm,
        point,
        left,
        frame_data_y,
        right,
        scale,
        race_clock=race_clock if isinstance(race_clock, dict) else None,
        race_anim=point_anim,
        race_override=shared_race if isinstance(shared_race, dict) else None,
    )

    if control is None or getattr(control, "show_tag_card", True):
        _draw_tag_card(screen, font_sm, team_anim, x, y + height + int(5 * scale), width, scale, is_left, dt)
    if control is None or getattr(control, "show_combo_card", True):
        _draw_combo_ledger(screen, font_sm, team, x, y + height + int(38 * scale), width, scale, is_left)

    entrance_age = max(0.0, float(team_anim.get("entrance_age", 1.32)))
    panel_clip = pygame.Rect(panel_x, panel_y, width, height)
    stage_bands = (
        (pygame.Rect(0, 0, width, max(1, strip_y)), 0.30, 0.20),
        (pygame.Rect(0, strip_y, width, max(1, history_y - strip_y)), 0.45, 0.18),
        (pygame.Rect(0, history_y, width, max(1, input_y - history_y)), 0.58, 0.18),
        (pygame.Rect(0, input_y, width, max(1, move_history_y - input_y)), 0.71, 0.19),
        (pygame.Rect(0, move_history_y, width, max(1, height - move_history_y)), 0.86, 0.19),
    )
    for source_rect, start, duration in stage_bands:
        _blit_hud_stage(
            root_screen,
            screen,
            source_rect,
            panel_x,
            panel_y,
            _hud_stage_progress(entrance_age, start, duration),
            is_left,
            scale,
            panel_clip,
        )

    _blit_hud_stage(
        root_screen,
        screen,
        pygame.Rect(0, height, width, min(screen.get_height() - height, max(34, int(37 * scale)))),
        panel_x,
        panel_y,
        _hud_stage_progress(entrance_age, 1.00, 0.20),
        is_left,
        scale,
        None,
        6.0,
    )
    combo_start_y = height + max(32, int(35 * scale))
    _blit_hud_stage(
        root_screen,
        screen,
        pygame.Rect(0, combo_start_y, width, max(1, screen.get_height() - combo_start_y)),
        panel_x,
        panel_y,
        _hud_stage_progress(entrance_age, 1.10, 0.22),
        is_left,
        scale,
        None,
        8.0,
    )


def draw_overlay(screen, font, font_sm, slots, scale, dt, control=None) -> None:
    core_visible = control is None or getattr(control, "show_hud", True)

    for slot_label, snap in slots.items():
        if isinstance(snap, dict):
            _compact_track_slot(slot_label, snap)

    if HUD_LAYOUT_MODE != "compact":
        if core_visible:
            _draw_overlay_detail(screen, font, font_sm, slots, scale, dt)
        _draw_research_panels(screen, font, font_sm, scale, control, dt)
        return

    if core_visible:
        _anim_state["overlay_alpha"] = _approach(_anim_state["overlay_alpha"], 1.0, FADE_SPEED, dt)
        overlay_alpha = _anim_state["overlay_alpha"]
        _maybe_restart_match_assembly(slots)
        _draw_match_assembly_spine(screen, scale, dt)
        _draw_compact_team_panel(screen, font, font_sm, "P1", slots, scale, overlay_alpha, dt, control)
        _draw_compact_team_panel(screen, font, font_sm, "P2", slots, scale, overlay_alpha, dt, control)
        if control is None or getattr(control, "show_interaction_card", True):
            _draw_live_interaction_ribbon(screen, font, font_sm, scale, dt)
        _tick_combo_ledgers(dt)
    else:
        _draw_research_panels(screen, font, font_sm, scale, control, dt)


# ---------------------------------------------------------------------------
# Renderer class for master_overlay.py
# ---------------------------------------------------------------------------

class HudRenderer:
    def __init__(self) -> None:
        self.w = BASE_W
        self.h = BASE_H
        self.scale = 1.0
        self.font = make_font(BASE_FONT_SIZE, bold=True)
        self.font_sm = make_font(int(BASE_FONT_SIZE * 0.78), bold=False)
        self._hud_was_visible = False
        self._last_dt = 1.0 / 60.0

    def on_resize(self, w: int, h: int) -> None:
        if w <= 0 or h <= 0:
            return
        self.w = w
        self.h = h
        self.scale = min(w / BASE_W, h / BASE_H)
        self.font = make_font(int(BASE_FONT_SIZE * self.scale), bold=True)
        self.font_sm = make_font(int(BASE_FONT_SIZE * self.scale * 0.78), bold=False)

    def update(self, dt: float, control=None) -> None:
        global _frame, _punish_overlay, _timing_engine_payload
        _frame += 1
        self._last_dt = max(1.0 / 240.0, min(0.10, float(dt or (1.0 / 60.0))))

        new_slots = read_slot_data()
        _merge_realtime_inputs(new_slots, read_realtime_input_data())
        punish_data = new_slots.get("_punish_trainer") if isinstance(new_slots, dict) else None
        timing_data = new_slots.get("_timing_engine") if isinstance(new_slots, dict) else None
        _punish_overlay = dict(punish_data) if isinstance(punish_data, dict) else {}
        _timing_engine_payload = dict(timing_data) if isinstance(timing_data, dict) else {}

        for slot_label in SLOT_LAYOUT.keys():
            _get_slot_anim(slot_label)["present"] = slot_label in new_slots

        for k, v in new_slots.items():
            if str(k).startswith("_"):
                continue
            if isinstance(v, dict):
                _display_slots[k] = v

        timing_published = _consume_timing_engine_result()
        if not timing_published:
            _update_adv()

    def draw(self, screen: pygame.Surface, control=None) -> None:
        hud_visible = control is None or getattr(control, "show_hud", True)
        research_visible = bool(
            control is not None
            and (
                getattr(control, "show_damage_badge", False)
                or getattr(control, "show_untech_panel", False)
                or getattr(control, "show_meter_panel", False)
                or getattr(control, "show_red_health_panel", False)
                or getattr(control, "show_attack_property_panel", False)
            )
        )
        if not hud_visible and not research_visible:
            self._hud_was_visible = False
            return
        if hud_visible and not self._hud_was_visible:
            _restart_hud_entrance()
            self._hud_was_visible = True
        elif not hud_visible:
            self._hud_was_visible = False

        draw_overlay(screen, self.font, self.font_sm, _display_slots, self.scale, self._last_dt, control)
        if hud_visible:
            _draw_punish_countdown(screen, self.scale)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global _frame, _punish_overlay

    dolphin_hwnd = find_dolphin_hwnd()
    if not dolphin_hwnd:
        print("[hud_overlay] Dolphin window not found  -  exiting.")
        return

    pygame.init()
    screen = pygame.display.set_mode((BASE_W, BASE_H), pygame.SRCALPHA)
    pygame.display.set_caption("TvC HUD Overlay")

    overlay_hwnd = pygame.display.get_wm_info()["window"]
    apply_overlay_style(overlay_hwnd)
    win32gui.SetWindowLong(overlay_hwnd, win32con.GWL_HWNDPARENT, dolphin_hwnd)

    cur_w, cur_h = BASE_W, BASE_H
    scale   = 1.0
    font    = make_font(BASE_FONT_SIZE, bold=True)
    font_sm = make_font(int(BASE_FONT_SIZE * 0.78), bold=False)
    clock   = pygame.time.Clock()
    running = True

    while running:
        w, h = sync_overlay_to_dolphin(dolphin_hwnd, overlay_hwnd)
        if w > 0 and h > 0 and (w, h) != (cur_w, cur_h):
            cur_w, cur_h = w, h
            screen  = pygame.display.set_mode((w, h), pygame.SRCALPHA)
            scale   = min(w / BASE_W, h / BASE_H)
            font    = make_font(int(BASE_FONT_SIZE * scale), bold=True)
            font_sm = make_font(int(BASE_FONT_SIZE * scale * 0.78), bold=False)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False

        dt = clock.tick(TARGET_FPS) / 1000.0
        _frame += 1

        new_slots = read_slot_data()
        _merge_realtime_inputs(new_slots, read_realtime_input_data())
        punish_data = new_slots.get("_punish_trainer") if isinstance(new_slots, dict) else None
        _punish_overlay = dict(punish_data) if isinstance(punish_data, dict) else {}

        for slot_label in SLOT_LAYOUT.keys():
            _get_slot_anim(slot_label)["present"] = slot_label in new_slots

        for k, v in new_slots.items():
            if str(k).startswith("_"):
                continue
            if isinstance(v, dict):
                _display_slots[k] = v

        _update_adv()

        draw_overlay(screen, font, font_sm, _display_slots, scale, dt)
        _draw_punish_countdown(screen, scale)
        pygame.display.flip()

    pygame.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        input("\n[CRASHED] Press Enter to close...")