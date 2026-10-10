"""
mission_manager.py
------------------
Owns all mission-mode state and logic previously scattered as nested
functions inside legacy_main().

Public surface used by the render loop:
    mgr = MissionManager(move_map, global_map, debug_flag_addrs, read_debug_flags_fn)
    mgr.update(snaps, render_snap_by_slot, frame_idx, now)
    mgr.write_overlay_data()
    mgr.write_mode_state()
    mgr.consume_select_command()
    mgr.consume_celebrate_ack()
    mgr.update_selector_from_inputs(snaps, now)
    mgr.active_slot          -> str | None
    mgr.selector_open        -> bool
    mgr.select_mission_delta(delta)
"""

from __future__ import annotations

import json
import os
import re
import time
import threading
from pathlib import Path
from typing import Any

from tvcgui.platform.dolphin import wd8
from tvcgui.runtime import input_monitor
from tvcgui.runtime.mission_reaction_policy import (
    MISSION_CROUCH_REACTION_STATES,
    MISSION_REACTION_STATES,
    MISSION_MEGACRASH_STATES,
    MISSION_COMBO_KEEPALIVE_STATES,
    MISSION_BLOCKSTUN_STATES,
)
from tvcgui.runtime.mission_events import (
    EVENT_ACTION,
    EVENT_COMBO_BEGIN,
    EVENT_COMBO_CHANGE,
    EVENT_COMBO_END,
    EVENT_DAMAGE,
    EVENT_HITSTUN_BEGIN,
    EVENT_HITSTUN_END,
    EVENT_INPUT,
    EVENT_MEGACRASH_BEGIN,
    EVENT_MEGACRASH_END,
    MissionEvent,
)
from tvcgui.runtime.mission_menu_input import (
    MISSION_MENU_CLOSE,
    MISSION_MENU_NEXT,
    MISSION_MENU_OPEN,
    MISSION_MENU_PREVIOUS,
    MISSION_MENU_SELECT,
    MissionMenuCommand,
)
from tvcgui.core.paths import user_data_path
from tvcgui.features.training.mission_mode import (
    build_overlay_payload,
    load_progress,
    save_progress,
    mark_mission_complete,
    set_selected_mission_id,
)
from tvcgui.features.training.mission_runtime_v2 import MissionRuntimeV2, RuntimeResult

MISSION_MODE_FILE          = user_data_path("training", "mission_mode_state.json")
MISSION_OVERLAY_FILE       = user_data_path("training", "mission_overlay_data.json")
MISSION_SELECT_FILE        = user_data_path("training", "mission_select_command.json")
MISSION_CELEBRATE_ACK_FILE = user_data_path("training", "mission_celebrate_ack.json")

MISSION_COMMAND_POLL_INTERVAL = 0.10

# ---------------------------------------------------------------------------
# Sets / constants (same values as in the old main)
# ---------------------------------------------------------------------------

# Mission reaction/keep-alive IDs live in mission_reaction_policy.py.  The
# manager, realtime sampler, and V2 evaluator must all share that one table.

# Verified global in-game combo counter. It remains telemetry/evidence only;
# native mission reaction state owns reset continuity.
MISSION_GLOBAL_COMBO_COUNTER_ADDR = 0x809BDDB3

MISSION_IGNORE_LABELS = {"", "idle", "crouched", "crouching"}
MISSION_ASSIST_OFF_STATES = {420, 424, 425, 426, 427, 428, 430, 431, 432, 433, 0x01A1, 0x01A8, 0x01AE}
MISSION_AIRBORNE_LABEL_TOKENS = (
    "jump", "air ", "j.", "air dash", "air weapon switch",
    "air random flight",
)
MISSION_VAR_LABEL_TOKENS = ("weapon switch", "variable air raid", "var")

MISSION_REQUIRE_DAMAGE_CONFIRM = True

MISSION_NON_DAMAGE_CONFIRM_LABELS = {"baroque cancel"}

MISSION_WHIFF_CONFIRM_LABELS = {
    s.strip().lower() for s in {
        "Air Dash A", "Air Dash B", "Air Dash C",
        "Weapon Switch Neutral A", "Weapon Switch Neutral B", "Weapon Switch Neutral C",
        "Weapon Switch Forwards A", "Weapon Switch Forwards B", "Weapon Switch Forwards C",
        "Weapon Switch Backwards A", "Weapon Switch Backwards B", "Weapon Switch Backwards C",
        "Air Weapon Switch Forward A", "Air Weapon Switch Forward B", "Air Weapon Switch Forward C",
        "Air Weapon Switch Backwards A", "Air Weapon Switch Backwards B", "Air Weapon Switch Backwards C",
        "Air Weapon Switch Neutral A", "Air Weapon Switch Neutral B", "Air Weapon Switch Neutral C",
        "Roll A", "Roll B", "Roll C", "Random Flight A", "Random Flight B", "Random Flight C",
        "Air Random Flight A", "Air Random Flight B", "Air Random Flight C",
        "Zombie Spree A", "Zombie Spree B", "Zombie Spree C", "Megacrash", "voltekka air",
         "yatter run",
        "Clutch A", "Clutch B", "Clutch C",
        "Tree A", "Tree B", "Tree C",
        "Rock A", "Rock B", "Rock C",
        "Comfy", "LOAD SUPER ARMOR PIERCING SHELL",
        "Pummel A", "Pummel B", "Pummel C",
        "Cactus Bunker A", "Cactus Bunker B", "Cactus Bunker C",
        "Air Rock A", "Air Rock B", "Air Rock C",
        "Quick Upper B", "yatter step", "Omochama",
    }
}

MISSION_GENERIC_VAR_LABELS = {"VAR", "var"}

MISSION_SELF_VAR_MISSIONS = {"alex_011"}

# Legacy fallback for existing mission packs. New missions should use
# setup_meter_refill=true in their JSON instead of adding another ID here.
MISSION_METER_REFILL_MISSIONS = {"ryu_008", "saki_009", "alex_017"}

MISSION_INPUT_DIRECTION_MASK = 0x0F
MISSION_SELECTOR_DOWN_MASK = 0x08
MISSION_SELECTOR_DOWN_DIRECTIONS = frozenset((0x08, 0x09, 0x0A))
MISSION_SELECTOR_REPEAT_WINDOW = 1.35
MISSION_INPUT_A = 0x80
MISSION_INPUT_B = 0x40
MISSION_INPUT_C = 0x20
MISSION_INPUT_P = 0x10
MISSION_INPUT_TAUNT = 0x0C00
MISSION_INPUT_ATTACK_MASK = MISSION_INPUT_A | MISSION_INPUT_B | MISSION_INPUT_C | MISSION_INPUT_P | MISSION_INPUT_TAUNT
MISSION_INPUT_ABC_MASK = MISSION_INPUT_A | MISSION_INPUT_B | MISSION_INPUT_C
MISSION_INPUT_BUTTON_MASKS = {
    "A": MISSION_INPUT_A,
    "B": MISSION_INPUT_B,
    "C": MISSION_INPUT_C,
    "L": MISSION_INPUT_A,
    "M": MISSION_INPUT_B,
    "H": MISSION_INPUT_C,
    "P": MISSION_INPUT_P,
    "T": MISSION_INPUT_TAUNT,
}
def _mission_selector_down_rising(direction: int, previous_direction: int, pressed: int) -> bool:
    """Accept clean down taps and down diagonals for the selector shortcut."""
    current = int(direction) & MISSION_INPUT_DIRECTION_MASK
    previous = int(previous_direction) & MISSION_INPUT_DIRECTION_MASK
    pressed_word = int(pressed) & 0xFFFF
    if pressed_word & MISSION_SELECTOR_DOWN_MASK:
        return True
    return (
        current in MISSION_SELECTOR_DOWN_DIRECTIONS
        and previous not in MISSION_SELECTOR_DOWN_DIRECTIONS
    )


def _mission_route_combo_live(
    global_combo_active: bool,
    opponent_in_hitstun: bool,
    dedicated_megacrash_match: bool = False,
) -> bool:
    """Return whether the defender is in a real mission reaction state.

    The global combo counter remains useful as hit-count evidence, but it can
    stay nonzero after recovery and must not keep an ordered mission route alive.
    Normal routes live only while the defender's primary action is a verified
    reaction state. Megacrash gets its dedicated state-edge frame.
    """
    _ = global_combo_active
    return bool(opponent_in_hitstun or dedicated_megacrash_match)


def _mission_combo_reset_tick(
    progress_index: int,
    combo_live: bool,
    grace_left: int,
    grace_step_index,
) -> tuple[bool, int]:
    """Return ``(reset_now, remaining_grace)`` for one mission frame.

    Grace is consumed only after both combo-counter and hitstun liveness end.
    A step with N grace frames receives exactly N dropped-combo frames.
    Without matching explicit grace, any dropped route resets immediately.
    """
    progress = max(0, int(progress_index or 0))
    remaining = max(0, int(grace_left or 0))
    if progress <= 0 or bool(combo_live):
        return False, remaining
    if remaining > 0 and grace_step_index == progress:
        return False, remaining - 1
    return True, 0


MISSION_INPUT_BAROQUE_PAIRS = (
    ("AP", MISSION_INPUT_A | MISSION_INPUT_P),
    ("BP", MISSION_INPUT_B | MISSION_INPUT_P),
    ("CP", MISSION_INPUT_C | MISSION_INPUT_P),
)
MISSION_INPUT_EVENT_WINDOW = 60
MISSION_INPUT_NORMAL_LATCH = 14
MISSION_INPUT_BAROQUE_LATCH = 14
MISSION_INPUT_LEGS_WINDOW = 42
MISSION_INPUT_COMMAND_WINDOW = 36
MISSION_INPUT_MASH_COUNT = 4
MISSION_HIT_CONFIRM_WINDOW = 16
MISSION_HIT_CORRELATION_WINDOW = 4
MISSION_HIT_PRELABEL_WINDOW = 8
MISSION_BUFFERED_STEP_LIMIT = 16
MISSION_BUFFERED_NORMAL_HIT_WINDOW = 20

# Prediction is display-only until a real HP or combo event confirms the hit.
# Native action IDs let the HUD move with the player immediately, while frame
# data supplies a bounded confirmation window for each attack.
MISSION_PREDICTION_MAX_STEPS = 16
MISSION_PREDICTION_FALLBACK_STARTUP = 12
MISSION_PREDICTION_FALLBACK_ACTIVE = 4
MISSION_PREDICTION_CONFIRM_PADDING = 12
MISSION_PREDICTION_MAX_AGE_SECONDS = 1.50
MISSION_ACTION_EVENT_LIMIT = 32
MISSION_HIT_EVENT_LIMIT = 32

_MISSION_DIRECTION_MIRROR = {"1": "3", "3": "1", "4": "6", "6": "4", "7": "9", "9": "7"}

DORONJO_DAMAGE_PASS: dict[str, set] = {
    "clutch a": {2}, "clutch b": {2}, "clutch c": {2},
    "pummel a": {880}, "pummel b": {880}, "pummel c": {880},
    "tree a": {1360}, "tree b": {1360}, "tree c": {1360},
    "rock a": {4480}, "rock b": {4480}, "rock c": {4480},
}

MISSION_GENERIC_ACTION_LABELS = {
    0x100: "5A",
    0x101: "5B",
    0x102: "5C",
    0x103: "2A",
    0x104: "2B",
    0x105: "2C",
    0x106: "6C",
    0x107: "4C",
    0x108: "3C",
    0x109: "j.A",
    0x10A: "j.B",
    0x10B: "j.C",
    0x10C: "j.6C",
    0x10D: "j.4C",
    0x10E: "6B",
}
MISSION_AIR_NORMAL_ACTIONS = {
    0x109: "A",
    0x10A: "B",
    0x10B: "C",
    0x10C: "C",
    0x10D: "C",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_mission_runtime(
    slot=None,
    mission_id=None,
    clear_seq=0,
    celebrate_token=0,
    celebrate_pending=False,
    celebrate_acked_token=0,
    
) -> dict:
    return {
        "slot": slot,
        "mission_id": mission_id,
        "progress_index": 0,
        "last_seen_label": "",
        "last_seen_anim": None,
        "last_seen_hitstun": False,
        "last_inputs": {},
        "mission_input_events": [],
        "mission_input_serial": 0,
        "mission_attack_command_serial": 0,
        "mission_input_consumed_serial": 0,
        "mission_action_events": [],
        "mission_action_serial": 0,
        "mission_action_consumed_serial": 0,
        "mission_last_action_id": None,
        "mission_last_action_sample_seq": -1,
        "mission_realtime_hp_by_slot": {},
        "mission_realtime_hitstun_by_slot": {},
        "mission_input_match_serial": 0,
        "mission_step_start_serial": 0,
        "pending_input_serial": 0,
        "pending_action_serial": 0,
        "pending_delayed_confirm_until_frame": -9999,
        "mission_prev_direction": None,
        "mission_prev_buttons": 0,
        "mission_baroque_latch": 0,
        "mission_last_input_token": "",
        "mission_last_input_frame": -9999,
        "mission_hit_events": [],
        "mission_hit_serial": 0,
        "mission_last_hit_evidence_frame": -9999,
        "mission_buffered_advances": 0,
        "predicted_progress_index": 0,
        "prediction_entries": [],
        "prediction_revision": 0,
        "pending_label_confirmed": False,
        "pending_arm_source": "",
        "last_global_combo_count": None,
        "last_actual_hitstun": False,
        "last_opponent_megacrash": False,
        "hitstun_grace": 0,
        "global_combo_count": 0,
        "pending_step_index": None,
        "pending_labels": [],
        "pending_anim": None,
        "pending_started_frame": -9999,
        "opponent_hp_by_base": {},
           "attacker_hp_by_base": {},
        "goal_state_frames": 0,
        "goal_combo_damage": 0,
        "goal_combo_hits": 0,
        "goal_hp_hit_count": 0,
        "goal_hit_latch": 0,
        "goal_failed": False,
        "goal_last_damage_frame": -1,
        "last_sampled_damage_frame": -1,
        "last_damage_event_sequence": 0,
        "clear_seq": int(clear_seq),
        "celebrate_token": int(celebrate_token),
        "celebrate_pending": bool(celebrate_pending),
        "celebrate_acked_token": int(celebrate_acked_token),
        "saved_p1meter_flag": None,
        "saved_baroque_flag": None,
        "saved_meter_flag_mission": None,
        "reset_grace_frames": 0,
        "reset_grace_labels": [],
        "reset_grace_step_index": None,
        "shell_install_hold": 0,
        "post_install_hold_frames": 0,
        "shell_installed": False,
        "shell_release_grace": 0,
          "reset_grace_keeps_alive_only": False,
    }


# ---------------------------------------------------------------------------
# MissionManager
# ---------------------------------------------------------------------------

class MissionManager:
    """
    Owns all mission-mode state and the logic that drives it each frame.

    Parameters
    ----------
    move_map : dict
        Per-character animation-ID -> label mapping (from load_move_map).
    global_map : dict
        Global animation-ID -> label mapping.
    debug_flag_addrs : list | dict
        The DEBUG_FLAG_ADDRS structure from config.py.
    read_debug_flags_fn : callable
        The merged_debug_values() callable from the main module (returns
        list of (name, addr, value, …) rows).
    move_label_for_fn : callable
        move_label_for(anim_id, csv_char_id, move_map, global_map)
    """

    def __init__(
        self,
        move_map: dict,
        global_map: dict,
        debug_flag_addrs: Any,
        read_debug_flags_fn,
        move_label_for_fn,
    ) -> None:
        self._move_map = move_map
        self._global_map = global_map
        self._debug_flag_addrs = debug_flag_addrs
        self._read_debug_flags = read_debug_flags_fn
        self._move_label_for = move_label_for_fn
        self._mission_timing_by_action = self._load_mission_frame_timings()

        # Core runtime state
        self._runtime: dict = _new_mission_runtime()
        self._active_slot: str | None = None
        # Mission ownership is pinned to the point fighter selected when Mission
        # Mode is enabled. Assist calls may temporarily perturb the live slot
        # snapshots, but they must never replace the mission pack or donate
        # their actions to the point character's route.
        self._mission_owner: dict[str, Any] = {
            "slot": None,
            "teamtag": "",
            "base": 0,
            "char_id": 0,
            "name": "",
        }

        # Selector state
        self._selector: dict = {
            "open": False,
            "selected_index": 0,
            "sequence": [],
            "sequence_blocked": False,
            "last_direction": 0,
            "last_taunt_held": False,
            "source_slot": None,
            "opened_at": 0.0,
            "hint_until": 0.0,
        }
        # Raw Mission Select input is owned by a standalone 240 Hz interpreter.
        # MissionManager only consumes immutable menu commands.
        self._menu_input_interpreter = None

        # Debug override save/restore state
        self._setup_state: dict = {
            "mission_key": None,
            "saved_debug_values": {},
            "applied_debug_values": {},
            "completed_mission_key": None,
            "idle_mission_key": None,
        }
        self._meter_refill_state: dict = {
            "mission_key": None,
            "saved_p1meter_flag": None,
            "saved_baroque_flag": None,
        }

        # Frame index, updated each call to update().
        self._frame_idx: int = 0

        # Health history is manager-wide rather than mission-wide. Mission
        # selection and route resets must not erase the baseline needed to
        # recognize the next frame's first hit.
        self._health_history_by_base: dict[int, dict[str, Any]] = {}
        self._health_damage_events: list[dict[str, Any]] = []
        self._health_damage_frame: int = -1

        # Last overlay payload built by write_overlay_data().  main.py uses this
        # to sync mission-scoped helpers such as Megacrash Trainer without
        # rereading the JSON file the module just wrote.
        self._last_overlay_payload: dict = self._build_empty_overlay_payload()
        self._next_command_poll: float = 0.0
        self._last_overlay_serialized: str = ""
        self._last_overlay_write_time: float = 0.0
        self._last_overlay_progress_signature: tuple = ()
        self._last_mode_serialized: str = ""
        self._overlay_write_condition = threading.Condition()
        self._overlay_file_lock = threading.RLock()
        self._overlay_publish_seq: int = 0
        self._last_overlay_file_seq: int = 0
        self._last_overlay_mission_id: str = ""
        self._last_overlay_mission_cursor: int = 0
        self._pending_overlay_payload: dict | None = None
        self._overlay_writer_stop = False
        self._overlay_writer_thread = threading.Thread(
            target=self._overlay_writer_loop,
            name="TvCMissionOverlayWriter",
            daemon=True,
        )
        self._overlay_writer_thread.start()

        # The normal GUI loop can be busy, but HudOverlayManager already owns a
        # dedicated 240 Hz Dolphin input sampler. Mission mode consumes that
        # ordered queue so short taps and complete strings are never reduced to
        # one late 60 Hz snapshot.
        self._input_sample_provider = None
        self._input_sample_cursor_by_consumer: dict[tuple[str, str], int] = {}

        # Realtime Mission Mode source. The sampler owns all Dolphin reads and
        # publishes immutable events. Mission consumers keep independent cursors
        # so selector, route matching, and state tracking never steal events.
        self._event_provider = None
        self._event_cursor_by_consumer: dict[tuple[str, str], int] = {}
        self._event_state_cursor: int = 0
        self._event_state: dict[str, Any] = {
            "latest_sequence": 0,
            "combo_count": 0,
            "hitstun_by_slot": {},
            "megacrash_by_slot": {},
            "action_by_slot": {},
            "hp_by_slot": {},
            "damage_events": [],
        }
        # Mission Runtime V2 owns route/goal evaluation. The large legacy
        # evaluator remains below only as a no-realtime compatibility fallback.
        self._mission_runtime_v2 = MissionRuntimeV2()

        # Realtime mission evaluation must not wait for the main GUI frame. The
        # sampler already produces immutable events off-thread, so this worker
        # only drains the in-memory ring and advances MissionRuntimeV2. It does
        # no Dolphin reads, scanner work, JSON serialization, or disk I/O.
        # Changed results are handed to the dedicated overlay writer thread.
        self._mission_realtime_lock = threading.RLock()
        self._mission_realtime_context: dict[str, Any] = {}
        self._mission_realtime_context_generation: int = 0
        self._mission_realtime_result: RuntimeResult | None = None
        self._mission_realtime_result_key: tuple[str, str, str] | None = None
        self._mission_realtime_result_signature: tuple = ()
        self._mission_realtime_stop = threading.Event()
        self._mission_realtime_wake = threading.Event()
        self._mission_realtime_thread = threading.Thread(
            target=self._mission_realtime_loop,
            name="TvCMissionRealtimeEvaluator",
            daemon=True,
        )
        self._mission_realtime_thread.start()

    def _load_mission_frame_timings(self) -> dict[tuple[int, int], dict[str, int]]:
        """Load compact startup, active, and hitstun data for prediction.

        Mission mode only needs three integers per native action. Keeping this
        small index avoids searching the full frame-data profile on every hit.
        """
        try:
            from tvcgui.features.frame_data.profile_store import iter_frame_data_profiles
            profiles = iter_frame_data_profiles()
        except Exception:
            return {}

        timings: dict[tuple[int, int], dict[str, int]] = {}
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            try:
                char_id = int(profile.get("char_id", 0) or 0)
            except Exception:
                char_id = 0
            if char_id <= 0:
                continue
            for move in profile.get("moves", []) or []:
                if not isinstance(move, dict):
                    continue
                try:
                    action_id = int(move.get("id", 0) or 0) & 0x7FFF
                except Exception:
                    action_id = 0
                if action_id < 0x100:
                    continue

                def _int_field(*names: str) -> int:
                    for name in names:
                        value = move.get(name)
                        try:
                            if value is not None and str(value).strip() != "":
                                return max(0, int(float(value)))
                        except Exception:
                            continue
                    return 0

                startup = _int_field("active_start", "startup")
                active_end = _int_field("active_end")
                hitstun = _int_field("hitstun")
                recovery = _int_field("runtime_observed_recovery", "recovery")
                animation_total = _int_field("animation_total_frames", "total_animation_frames")
                active = max(
                    1,
                    active_end - startup + 1
                    if startup > 0 and active_end >= startup
                    else MISSION_PREDICTION_FALLBACK_ACTIVE,
                )

                def _boolish(value) -> bool:
                    if isinstance(value, bool):
                        return value
                    if isinstance(value, (int, float)):
                        return value != 0
                    text = str(value or "").strip().lower()
                    if text in {"", "0", "false", "no", "off", "none", "null"}:
                        return False
                    if text in {"1", "true", "yes", "on"}:
                        return True
                    # Non-empty structured/window text means an authored JC
                    # window exists (for example "6-12").
                    return bool(text)

                jump_cancel_fields = (
                    "jump_cancel",
                    "jump_cancelable",
                    "jump_cancellable",
                    "jump_cancel_window",
                )
                jump_cancel_known = any(
                    field in move and move.get(field) not in (None, "")
                    for field in jump_cancel_fields
                )
                jump_cancel = any(_boolish(move.get(field)) for field in jump_cancel_fields)

                # Pending hit confirmation must have a deterministic wall-clock
                # expiry. Prefer the captured move duration, but retain enough
                # padding for hitstop and cross-slot sampler ordering. This is a
                # whiff failsafe, not the normal combo-reset clock.
                if animation_total > 0:
                    mission_timeout_frames = animation_total + 18
                elif recovery > 0:
                    mission_timeout_frames = max(active_end, startup) + recovery + 18
                else:
                    mission_timeout_frames = max(45, max(active_end, startup) + 24)
                mission_timeout_frames = max(30, min(240, mission_timeout_frames))

                candidate = {
                    "startup": startup or MISSION_PREDICTION_FALLBACK_STARTUP,
                    "active": active,
                    "hitstun": hitstun,
                    "recovery": recovery,
                    "mission_timeout_frames": mission_timeout_frames,
                    "jump_cancel_known": jump_cancel_known,
                    "jump_cancel": jump_cancel,
                }
                key = (char_id, action_id)
                existing = timings.get(key)
                if existing is None or candidate["startup"] < existing["startup"]:
                    if existing is not None:
                        candidate["jump_cancel_known"] = bool(
                            candidate["jump_cancel_known"] or existing.get("jump_cancel_known")
                        )
                        candidate["jump_cancel"] = bool(
                            candidate["jump_cancel"] or existing.get("jump_cancel")
                        )
                    timings[key] = candidate
                elif jump_cancel_known:
                    existing["jump_cancel_known"] = True
                    existing["jump_cancel"] = bool(existing.get("jump_cancel") or jump_cancel)
        return timings

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def active_slot(self) -> str | None:
        return self._active_slot

    @active_slot.setter
    def active_slot(self, value: str | None) -> None:
        self._active_slot = value
        if value is None:
            self._clear_mission_owner()

    def _snap_char_id(self, snap: dict | None) -> int:
        if not isinstance(snap, dict):
            return 0
        for field in ("csv_char_id", "char_id", "id"):
            try:
                value = int(snap.get(field, 0) or 0)
            except Exception:
                value = 0
            if value:
                return value
        return 0

    def _snap_action_id(self, snap: dict | None) -> int:
        if not isinstance(snap, dict):
            return 0
        for field in ("mv_id_display", "mv_id", "attA", "attB"):
            try:
                value = int(snap.get(field, 0) or 0) & 0x7FFF
            except Exception:
                value = 0
            if value:
                return value
        return 0

    def _snap_is_support_or_assist(self, snap: dict | None) -> bool:
        if not isinstance(snap, dict):
            return True
        label = str(snap.get("mv_label") or "").strip().lower()
        action_id = self._snap_action_id(snap)
        return bool(
            action_id in MISSION_ASSIST_OFF_STATES
            or "assist" in label
            or "tag out" in label
            or "tag in taunt" in label
            or "ko" in label
            or int(snap.get("cur", 1) or 0) <= 0
        )

    def _clear_mission_owner(self) -> None:
        self._mission_owner = {
            "slot": None,
            "teamtag": "",
            "base": 0,
            "char_id": 0,
            "name": "",
        }

    def _capture_mission_owner(self, slot_label: str, snaps_dict: dict | None = None) -> None:
        snaps = snaps_dict or getattr(self, "_render_snap_by_slot", {}) or {}
        snap = snaps.get(slot_label) if isinstance(snaps, dict) else None
        self._mission_owner = {
            "slot": slot_label,
            "teamtag": str((snap or {}).get("teamtag") or ("P1" if str(slot_label).startswith("P1") else "P2")),
            "base": int((snap or {}).get("base") or 0),
            "char_id": self._snap_char_id(snap),
            "name": str((snap or {}).get("name") or "").strip(),
        }

    def _ensure_mission_owner(self, snaps_dict: dict | None = None) -> None:
        if not self._active_slot:
            self._clear_mission_owner()
            return
        if not self._mission_owner.get("slot"):
            self._capture_mission_owner(self._active_slot, snaps_dict)

    def _mission_owner_matches_snap(self, snap: dict | None) -> bool:
        if not isinstance(snap, dict):
            return False
        owner_base = int(self._mission_owner.get("base", 0) or 0)
        snap_base = int(snap.get("base", 0) or 0)
        if owner_base and snap_base:
            return owner_base == snap_base
        owner_char_id = int(self._mission_owner.get("char_id", 0) or 0)
        snap_char_id = self._snap_char_id(snap)
        if owner_char_id and snap_char_id:
            return owner_char_id == snap_char_id
        owner_name = str(self._mission_owner.get("name") or "").strip().lower()
        snap_name = str(snap.get("name") or "").strip().lower()
        return bool(owner_name and snap_name and owner_name == snap_name)

    def _mission_owner_slot(self, snaps_dict: dict | None = None) -> str | None:
        self._ensure_mission_owner(snaps_dict)
        snaps = snaps_dict or getattr(self, "_render_snap_by_slot", {}) or {}
        owner_slot = str(self._mission_owner.get("slot") or self._active_slot or "")
        teamtag = str(self._mission_owner.get("teamtag") or "")
        candidate_slots = []
        if teamtag:
            candidate_slots.extend((f"{teamtag}-C1", f"{teamtag}-C2"))
        if owner_slot and owner_slot not in candidate_slots:
            candidate_slots.insert(0, owner_slot)
        for candidate in candidate_slots:
            snap = snaps.get(candidate) if isinstance(snaps, dict) else None
            if self._mission_owner_matches_snap(snap):
                return candidate
        # Never switch ownership to an assist merely because the point snapshot
        # was briefly unavailable. Keep the pinned logical slot and mission pack.
        return owner_slot or self._active_slot

    def _mission_owner_name(self, snaps_dict: dict | None = None) -> str:
        self._ensure_mission_owner(snaps_dict)
        name = str(self._mission_owner.get("name") or "").strip()
        if name:
            return name
        owner_slot = self._mission_owner_slot(snaps_dict)
        snaps = snaps_dict or getattr(self, "_render_snap_by_slot", {}) or {}
        snap = snaps.get(owner_slot) if owner_slot and isinstance(snaps, dict) else None
        name = str((snap or {}).get("name") or "").strip()
        if name:
            self._mission_owner["name"] = name
        return name

    @property
    def selector_open(self) -> bool:
        return self._selector["open"]

    @property
    def last_overlay_payload(self) -> dict:
        return dict(self._last_overlay_payload or {})

    def set_input_sample_provider(self, provider) -> None:
        """Compatibility source used by older tests and standalone callers."""
        self._input_sample_provider = provider

    def set_menu_input_interpreter(self, interpreter) -> None:
        """Attach Mission Select input and gate it to active Mission Mode."""
        self._menu_input_interpreter = interpreter
        if interpreter is not None:
            try:
                interpreter.set_enabled(bool(self._active_slot))
            except Exception:
                pass
        if interpreter is not None and self._selector.get("open"):
            slot = str(self._selector.get("source_slot") or self._active_slot or "")
            if slot:
                try:
                    interpreter.set_menu_open("P2" if slot.startswith("P2") else "P1", slot)
                except Exception:
                    pass

    def set_event_provider(self, provider) -> None:
        """Attach the immutable realtime event stream.

        The provider signature is ``provider(cursor, slot_label=None)`` and
        returns ``(newest_sequence, events)``. Mission Mode never reads Dolphin
        directly while this provider is attached.
        """
        live_provider = provider if callable(provider) else None
        live_floor = 0
        if live_provider is not None:
            try:
                live_floor, _events = live_provider(0, None)
            except TypeError:
                try:
                    live_floor, _events = live_provider(0)
                except Exception:
                    live_floor = 0
            except Exception:
                live_floor = 0
        live_floor = max(0, int(live_floor or 0))
        with self._mission_realtime_lock:
            self._event_provider = live_provider
            self._event_cursor_by_consumer.clear()
            self._event_state_cursor = live_floor
            self._event_state["latest_sequence"] = live_floor
            # Attaching the realtime source establishes a hard live boundary.
            # Do not let events collected before Mission Mode was wired donate
            # inputs/actions to the current attempt.
            self._runtime["mission_event_floor_sequence"] = live_floor
            self._mission_runtime_v2.reset_all(live_floor)
            self._mission_realtime_result = None
            self._mission_realtime_result_key = None
            self._mission_realtime_result_signature = ()
            self._mission_realtime_context_generation += 1
        self._mission_realtime_wake.set()

    def _set_mission_realtime_context(
        self,
        *,
        route_slot: str,
        character_name: str,
        mission_id: str,
        steps: list,
        mission_goal: dict,
        event_floor: int,
        char_id: int,
    ) -> tuple[str, str, str]:
        """Publish immutable mission configuration to the realtime evaluator."""
        key = (str(route_slot or ""), str(character_name or ""), str(mission_id or ""))
        context = {
            "key": key,
            "route_slot": str(route_slot or ""),
            "character_name": str(character_name or ""),
            "mission_id": str(mission_id or ""),
            "steps": list(steps or []),
            "goal": dict(mission_goal or {}),
            "event_floor": max(0, int(event_floor or 0)),
            "char_id": max(0, int(char_id or 0)),
        }
        with self._mission_realtime_lock:
            previous = dict(self._mission_realtime_context or {})
            previous_key = tuple(previous.get("key") or ())
            previous_floor = int(previous.get("event_floor", -1) or -1)
            if previous_key != key or previous_floor != context["event_floor"]:
                self._mission_realtime_context_generation += 1
                self._mission_realtime_result = None
                self._mission_realtime_result_key = None
                self._mission_realtime_result_signature = ()
            self._mission_realtime_context = context
        self._mission_realtime_wake.set()
        return key

    def _initial_realtime_result(self, steps: list, event_floor: int) -> RuntimeResult:
        current_label = None
        if steps:
            step = steps[0]
            if isinstance(step, dict):
                current_label = str(step.get("display") or step.get("display_label") or "").strip()
                if not current_label:
                    current_label = " / ".join(self._step_labels(step))
            else:
                current_label = " / ".join(self._step_labels(step))
        return RuntimeResult(
            progress_index=0,
            current_step_label=current_label or None,
            cleared=False,
            phase="armed",
            attempt_id=1,
            event_floor=max(0, int(event_floor or 0)),
            event_cursor=max(0, int(event_floor or 0)),
            buffered_advances=0,
            restart_count=0,
            last_restart_reason="",
        )

    def _realtime_result_for_key(
        self,
        key: tuple[str, str, str],
        steps: list,
        event_floor: int,
    ) -> RuntimeResult:
        with self._mission_realtime_lock:
            if self._mission_realtime_result_key == key and self._mission_realtime_result is not None:
                return self._mission_realtime_result
        return self._initial_realtime_result(steps, event_floor)

    def _allocate_overlay_publish_seq(self) -> int:
        with self._overlay_file_lock:
            self._overlay_publish_seq += 1
            return int(self._overlay_publish_seq)

    def _publish_mission_realtime_result(
        self,
        context: dict,
        result: RuntimeResult,
    ) -> None:
        """Push a changed V2 result to the overlay without waiting for GUI update()."""
        key = tuple(context.get("key") or ())
        steps = list(context.get("steps") or [])
        total_steps = len(steps) if steps else 1
        confirmed = total_steps if result.cleared else max(0, int(result.progress_index))
        ready_progress = confirmed
        if not result.cleared and result.goal_progress_type is None:
            ready_progress = max(confirmed, int(result.ready_progress_index or 0))
        completed = max(0, min(total_steps, ready_progress))
        predicted = max(0, completed - confirmed)
        prediction_active = bool(predicted and result.prediction_active)
        current_idx = max(0, min(total_steps - 1, int(result.progress_index))) if total_steps else 0
        signature = (
            key,
            completed,
            confirmed,
            predicted,
            prediction_active,
            current_idx,
            bool(result.cleared),
            int(result.attempt_id),
            int(result.restart_count),
            str(result.last_restart_reason),
            result.goal_progress_type,
            int(result.goal_current_frames),
            int(result.goal_damage),
            int(result.goal_hits),
        )
        with self._mission_realtime_lock:
            if signature == self._mission_realtime_result_signature:
                return
            packet = dict(self._last_overlay_payload or {})

        # The worker can outrun the first GUI payload by a millisecond. Do not
        # consume the signature until the matching mission packet exists; the
        # idle loop will retry this same result once the packet is staged.
        if not packet.get("active"):
            return
        if str(packet.get("active_mission_id") or "") != str(context.get("mission_id") or ""):
            return

        with self._mission_realtime_lock:
            if signature == self._mission_realtime_result_signature:
                return
            self._mission_realtime_result_signature = signature

        packet.update({
            "completed_step_count": completed,
            "confirmed_step_count": confirmed,
            "predicted_step_count": predicted,
            "prediction_active": prediction_active,
            "current_step_index": current_idx,
            "current_step_label": result.current_step_label,
            "mission_event_cursor": int(result.event_cursor),
            "mission_buffered_advances": int(result.buffered_advances),
            "mission_runtime_version": 2,
            "mission_attempt_id": int(result.attempt_id),
            "mission_attempt_phase": str(result.phase),
            "mission_event_floor": int(result.event_floor),
            "mission_restart_count": int(result.restart_count),
            "mission_last_restart_reason": str(result.last_restart_reason),
            "goal_progress_type": result.goal_progress_type,
            "goal_current_frames": int(result.goal_current_frames),
            "goal_needed_frames": int(result.goal_needed_frames),
            "goal_timer_active": bool(result.goal_timer_active),
            "goal_current_damage": int(result.goal_damage),
            "goal_needed_damage": int(result.goal_damage_needed),
            "goal_current_hits": int(result.goal_hits),
            "goal_max_hits": int(result.goal_max_hits),
        })
        publish_seq = self._allocate_overlay_publish_seq()
        packet["__publish_seq"] = publish_seq
        # Never perform JSON serialization or filesystem I/O on the realtime
        # evaluator. A slow antivirus scan, cloud-sync hook, or filesystem flush
        # must be incapable of delaying the next mission input. The writer is
        # latest-only, which is exactly what the 60 Hz renderer needs.
        self._queue_overlay_payload(packet)

    def _mission_realtime_loop(self) -> None:
        """Continuously consume Mission V2 events independently of GUI cadence."""
        local_generation = -1
        cursor = 0
        while not self._mission_realtime_stop.is_set():
            with self._mission_realtime_lock:
                provider = self._event_provider
                context = dict(self._mission_realtime_context or {})
                generation = int(self._mission_realtime_context_generation)

            if not callable(provider) or not context.get("key"):
                self._mission_realtime_wake.wait(0.010)
                self._mission_realtime_wake.clear()
                continue

            if generation != local_generation:
                local_generation = generation
                cursor = max(0, int(context.get("event_floor", 0) or 0))
                with self._mission_realtime_lock:
                    self._mission_runtime_v2.reset_all(cursor)
                    self._mission_runtime_v2.configure(
                        slot=str(context.get("route_slot") or ""),
                        character=str(context.get("character_name") or ""),
                        mission_id=str(context.get("mission_id") or ""),
                        raw_steps=list(context.get("steps") or []),
                        goal=dict(context.get("goal") or {}),
                        event_floor=cursor,
                        whiff_policy=lambda labels, step: self._step_allows_whiff_confirm(list(labels), step),
                    )

            try:
                newest, events = provider(cursor, None)
            except TypeError:
                try:
                    newest, events = provider(cursor)
                except Exception:
                    newest, events = cursor, ()
            except Exception:
                newest, events = cursor, ()

            newest = max(cursor, int(newest or 0))
            if not events and newest <= cursor:
                # Deadlines must continue to advance while the game is quiet.
                # Previously a scheduled combo reset only expired when another
                # input/action event arrived, so the *next* button press could
                # become the thing that finally reset the route.
                with self._mission_realtime_lock:
                    idle_result = self._mission_runtime_v2.tick(
                        now_ns=time.monotonic_ns(),
                        newest_sequence=newest,
                    )
                    self._mission_realtime_result = idle_result
                    self._mission_realtime_result_key = tuple(context.get("key") or ())
                self._publish_mission_realtime_result(context, idle_result)
                self._mission_realtime_wake.wait(0.0015)
                self._mission_realtime_wake.clear()
                continue

            char_id_hint = max(0, int(context.get("char_id", 0) or 0))

            def _resolve_action(event: MissionEvent) -> list[str]:
                event_action = int(getattr(event, "action_id", 0) or 0) & 0x7FFF
                char_id = char_id_hint or int(getattr(event, "char_id", 0) or 0)
                return list(self._action_label_candidates(event_action, char_id, snap=None) or [])

            def _jump_cancel_allowed(action_id: int) -> bool:
                info = self._mission_timing_by_action.get(
                    (int(char_id_hint or 0), int(action_id or 0) & 0x7FFF)
                )
                if not isinstance(info, dict) or not info.get("jump_cancel_known"):
                    # Native accepted jump remains authoritative when the frame
                    # profile has no JC metadata for this exact action.
                    return True
                return bool(info.get("jump_cancel"))

            def _action_timeout_ns(action_id: int) -> int:
                info = self._mission_timing_by_action.get(
                    (int(char_id_hint or 0), int(action_id or 0) & 0x7FFF)
                )
                if not isinstance(info, dict):
                    return 0
                try:
                    frames = max(0, int(info.get("mission_timeout_frames", 0) or 0))
                except Exception:
                    frames = 0
                if frames <= 0:
                    return 0
                return int(frames * (1_000_000_000 / 60.0))

            with self._mission_realtime_lock:
                result = self._mission_runtime_v2.consume(
                    events,
                    owner_slot=str(context.get("route_slot") or ""),
                    label_matcher=self._mission_label_matches,
                    action_label_resolver=_resolve_action,
                    jump_cancel_checker=_jump_cancel_allowed,
                    action_timeout_resolver=_action_timeout_ns,
                    newest_sequence=newest,
                )
                self._mission_realtime_result = result
                self._mission_realtime_result_key = tuple(context.get("key") or ())
            cursor = max(cursor, int(result.event_cursor))
            self._publish_mission_realtime_result(context, result)

    def _events_for_consumer(
        self,
        consumer: str,
        slot_label: str | None = None,
    ) -> list[MissionEvent]:
        provider = self._event_provider
        if not callable(provider):
            return []
        key = (str(consumer), str(slot_label or "*"))
        default_cursor = (
            int(self._runtime.get("mission_event_floor_sequence", 0) or 0)
            if str(consumer).startswith("mission")
            else 0
        )
        cursor = int(self._event_cursor_by_consumer.get(key, default_cursor) or 0)
        try:
            newest, events = provider(cursor, slot_label)
        except TypeError:
            newest, events = provider(cursor)
            if slot_label:
                events = [event for event in events if getattr(event, "slot", "") == slot_label]
        except Exception:
            return []
        self._event_cursor_by_consumer[key] = max(cursor, int(newest or 0))
        return [event for event in (events or ()) if isinstance(event, MissionEvent)]

    def _drain_event_state(self) -> None:
        """Update the latest combat state from immutable events only."""
        provider = self._event_provider
        if not callable(provider):
            return
        try:
            newest, events = provider(self._event_state_cursor, None)
        except TypeError:
            newest, events = provider(self._event_state_cursor)
        except Exception:
            return
        self._event_state_cursor = max(self._event_state_cursor, int(newest or 0))
        self._event_state["latest_sequence"] = self._event_state_cursor
        hitstun_by_slot = self._event_state.setdefault("hitstun_by_slot", {})
        megacrash_by_slot = self._event_state.setdefault("megacrash_by_slot", {})
        action_by_slot = self._event_state.setdefault("action_by_slot", {})
        hp_by_slot = self._event_state.setdefault("hp_by_slot", {})
        damage_events = self._event_state.setdefault("damage_events", [])

        for event in events or ():
            if not isinstance(event, MissionEvent):
                continue
            slot = str(event.slot or "")
            if event.kind == EVENT_ACTION:
                action_by_slot[slot] = int(event.action_id)
            elif event.kind == EVENT_DAMAGE:
                hp_by_slot[slot] = int(event.current_hp)
                damage_events.append(event)
            elif event.kind == EVENT_HITSTUN_BEGIN:
                hitstun_by_slot[slot] = True
            elif event.kind == EVENT_HITSTUN_END:
                hitstun_by_slot[slot] = False
            elif event.kind == EVENT_MEGACRASH_BEGIN:
                megacrash_by_slot[slot] = True
            elif event.kind == EVENT_MEGACRASH_END:
                megacrash_by_slot[slot] = False
            elif event.kind in (EVENT_COMBO_BEGIN, EVENT_COMBO_CHANGE, EVENT_COMBO_END):
                self._event_state["combo_count"] = int(event.combo_count)

        # Keep enough history for a delayed GUI frame while bounding allocations.
        if len(damage_events) > 128:
            del damage_events[:-128]

    def _fresh_runtime(self, **kwargs) -> dict:
        """Create mission state whose event cursors start at the live stream head."""
        runtime = _new_mission_runtime(**kwargs)
        floor = int(self._event_state.get("latest_sequence", 0) or 0)
        runtime["last_damage_event_sequence"] = floor
        runtime["mission_event_floor_sequence"] = floor
        for key in list(self._event_cursor_by_consumer):
            if str(key[0]).startswith("mission"):
                del self._event_cursor_by_consumer[key]
        return runtime

    def _sync_input_cursors_to_latest(self, slot_label: str, snap: dict | None = None) -> None:
        if callable(self._event_provider):
            try:
                newest, _events = self._event_provider(0, None)
            except TypeError:
                newest, _events = self._event_provider(0)
            except Exception:
                newest = 0
            for consumer in ("selector", "mission", "mission-opponent"):
                self._event_cursor_by_consumer[(consumer, str(slot_label))] = int(newest or 0)
            return

        provider = self._input_sample_provider
        if not callable(provider) or not slot_label:
            return
        try:
            _latest, samples = provider(
                slot_label,
                int((snap or {}).get("base") or 0),
            )
        except Exception:
            return
        newest = max(
            [int((sample or {}).get("seq", 0) or 0) for sample in (samples or [])]
            or [0]
        )
        for consumer in ("selector", "mission", "mission-opponent"):
            self._input_sample_cursor_by_consumer[(consumer, str(slot_label))] = newest

    # ------------------------------------------------------------------
    # Public update entry point
    # ------------------------------------------------------------------

    def update(
        self,
        snaps: dict,
        render_snap_by_slot: dict,
        frame_idx: int,
        now: float,
    ) -> None:
        """Called once per frame from the render loop."""
        self._frame_idx = frame_idx
        self._now = now
        self._render_snap_by_slot = render_snap_by_slot
        self._ensure_mission_owner(render_snap_by_slot)
        self._drain_event_state()
        if not callable(self._event_provider):
            self._sample_health_deltas(render_snap_by_slot, frame_idx)
        self._consume_menu_input_commands(snaps, now)
        if now >= self._next_command_poll:
            self._next_command_poll = now + MISSION_COMMAND_POLL_INTERVAL
            self.consume_select_command()
            self.consume_celebrate_ack()

        # Evaluate and publish mission state immediately, before the GUI's
        # profiler and rendering work. write_overlay_data is content-deduped, so
        # an unchanged frame performs no filesystem write.
        self.write_mode_state()
        self.write_overlay_data(render_snap_by_slot)

    # ------------------------------------------------------------------
    # Selector navigation
    # ------------------------------------------------------------------

    def select_mission_delta(self, delta: int) -> None:
        """Advance the selected mission by delta steps (from HUD button or keyboard)."""
        if not self._active_slot:
            return

        character_name = self._mission_owner_name(getattr(self, "_render_snap_by_slot", {}))
        if not character_name:
            return

        payload = build_overlay_payload(character_name)
        missions = payload.get("missions", [])
        if not missions:
            return

        active_id = payload.get("active_mission_id")
        idx = next(
            (i for i, m in enumerate(missions) if m.get("mission_id") == active_id),
            0,
        )
        new_idx = (idx + delta) % len(missions)
        new_id = missions[new_idx].get("mission_id")

        progress = load_progress()
        progress = set_selected_mission_id(progress, character_name, new_id)
        save_progress(progress)

        self._prepare_selected_mission(character_name, new_id)
        self._runtime = self._fresh_runtime(slot=self._active_slot)
        self.write_overlay_data()

    # ------------------------------------------------------------------
    # File consumers
    # ------------------------------------------------------------------

    def consume_select_command(self) -> None:
        if not os.path.isfile(MISSION_SELECT_FILE):
            return
        try:
            with open(MISSION_SELECT_FILE, "r", encoding="utf-8") as f:
                cmd = json.load(f)
        except Exception:
            return

        try:
            os.remove(MISSION_SELECT_FILE)
        except Exception:
            pass

        if not isinstance(cmd, dict):
            return

        action = cmd.get("action")
        if action == "close":
            self._close_selector()
            return

        if action != "select":
            return

        slot = cmd.get("slot")
        mission_id = cmd.get("mission_id")

        if slot != self._active_slot or not mission_id:
            return

        character_name = self._mission_owner_name(getattr(self, "_render_snap_by_slot", {}))
        if not character_name:
            return

        progress = load_progress()
        progress = set_selected_mission_id(progress, character_name, mission_id)
        save_progress(progress)

        self._prepare_selected_mission(character_name, mission_id)
        self._runtime = self._fresh_runtime(slot=self._active_slot)
        self._close_selector(rearm=True)

    def consume_celebrate_ack(self) -> None:
        if not os.path.isfile(MISSION_CELEBRATE_ACK_FILE):
            return
        try:
            with open(MISSION_CELEBRATE_ACK_FILE, "r", encoding="utf-8") as f:
                ack = json.load(f)
        except Exception:
            return

        try:
            os.remove(MISSION_CELEBRATE_ACK_FILE)
        except Exception:
            pass

        if not isinstance(ack, dict):
            return

        ack_token = int(ack.get("celebrate_token", 0) or 0)
        current_token = int(self._runtime.get("celebrate_token", 0) or 0)

        if ack_token > 0 and ack_token == current_token:
            self._runtime["celebrate_acked_token"] = ack_token
            self._runtime["celebrate_pending"] = False

    def _queue_overlay_payload(self, payload: dict) -> None:
        with self._overlay_write_condition:
            incoming = dict(payload or {})
            incoming_seq = int(incoming.get("__publish_seq", 0) or 0)
            pending_seq = int((self._pending_overlay_payload or {}).get("__publish_seq", 0) or 0)
            if self._pending_overlay_payload is None or incoming_seq >= pending_seq:
                self._pending_overlay_payload = incoming
            self._overlay_write_condition.notify()

    def _write_overlay_payload_file(self, payload: dict) -> None:
        """Write one queued mission payload without allowing stale state to win.

        This method runs only on the overlay writer thread. The private publish
        sequence is not serialized into the public mission payload.
        """
        packet = dict(payload or {})
        publish_seq = int(packet.pop("__publish_seq", 0) or 0)
        mission_id = str(packet.get("active_mission_id") or "")
        mission_cursor = max(0, int(packet.get("mission_event_cursor", 0) or 0))
        try:
            serialized = json.dumps(packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        except Exception:
            return
        with self._overlay_file_lock:
            if publish_seq > 0 and publish_seq < int(self._last_overlay_file_seq or 0):
                return
            # A GUI frame can begin before the realtime worker advances and
            # finish afterward. Publish sequence alone would then let the later
            # stale GUI write roll the mission pips backward. Event cursor is
            # the actual combat-state freshness authority.
            if (
                mission_id
                and mission_id == self._last_overlay_mission_id
                and mission_cursor > 0
                and mission_cursor < int(self._last_overlay_mission_cursor or 0)
            ):
                return
            if serialized == self._last_overlay_serialized and os.path.isfile(MISSION_OVERLAY_FILE):
                self._last_overlay_file_seq = max(self._last_overlay_file_seq, publish_seq)
                self._last_overlay_mission_id = mission_id or self._last_overlay_mission_id
                self._last_overlay_mission_cursor = max(self._last_overlay_mission_cursor, mission_cursor)
                return
            try:
                tmp = f"{MISSION_OVERLAY_FILE}.tmp"
                os.makedirs(os.path.dirname(MISSION_OVERLAY_FILE), exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                os.replace(tmp, MISSION_OVERLAY_FILE)
                self._last_overlay_serialized = serialized
                self._last_overlay_file_seq = max(self._last_overlay_file_seq, publish_seq)
                if mission_id:
                    if mission_id != self._last_overlay_mission_id:
                        self._last_overlay_mission_cursor = mission_cursor
                    else:
                        self._last_overlay_mission_cursor = max(self._last_overlay_mission_cursor, mission_cursor)
                    self._last_overlay_mission_id = mission_id
                elif not packet.get("active"):
                    self._last_overlay_mission_id = ""
                    self._last_overlay_mission_cursor = 0
            except Exception:
                pass

    def _overlay_writer_loop(self) -> None:
        while True:
            with self._overlay_write_condition:
                while self._pending_overlay_payload is None and not self._overlay_writer_stop:
                    self._overlay_write_condition.wait(timeout=0.25)
                if self._overlay_writer_stop and self._pending_overlay_payload is None:
                    return
                payload = self._pending_overlay_payload
                self._pending_overlay_payload = None
            self._write_overlay_payload_file(payload)

    # ------------------------------------------------------------------
    # File writers
    # ------------------------------------------------------------------

    def write_overlay_data(self, render_snap_by_slot: dict | None = None, *, force: bool = False) -> None:
        snaps = render_snap_by_slot or getattr(self, "_render_snap_by_slot", {})

        payload = self._build_empty_overlay_payload()

        if self._active_slot:
            self._ensure_mission_owner(snaps)
            point_slot = self._mission_owner_slot(snaps) or self._active_slot
            character_name = self._mission_owner_name(snaps)

            if character_name:
                payload = build_overlay_payload(character_name)
                payload["active"] = True
                payload["slot"] = self._active_slot
                payload["point_slot"] = point_slot
                payload = self._augment_payload_with_runtime(payload, snaps)
                payload["selector_open"] = bool(self._selector["open"])
                payload["selector_index"] = int(self._selector["selected_index"])
                payload["selector_hint"] = "Mission Mode: Down, Down, Taunt opens Mission Select"
                payload["selector_controls"] = "Down: Move  Taunt: Select  Mouse still works"
                payload["scanlines"] = True

        self._sync_debug_overrides(payload)
        self._last_overlay_payload = dict(payload or {})

        # Mission evaluation is lossless; rendering only needs the newest state.
        # Never block the GUI or realtime evaluator on JSON/disk I/O. The writer
        # thread coalesces superseded payloads while publish sequence + event
        # cursor guards prevent a stale GUI frame from rolling progress backward.
        self._last_overlay_write_time = float(getattr(self, "_now", 0.0) or time.time())
        publish_payload = dict(payload or {})
        publish_payload["__publish_seq"] = self._allocate_overlay_publish_seq()
        self._queue_overlay_payload(publish_payload)


    def write_mode_state(self, *, force: bool = False) -> None:
        if not self._active_slot and self._meter_refill_state.get("mission_key") is not None:
            self._restore_meter_refill_overrides()

        payload = {
            "active": bool(self._active_slot),
            "slot": self._active_slot,
        }
        try:
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            if not force and serialized == self._last_mode_serialized:
                return
            os.makedirs(os.path.dirname(MISSION_MODE_FILE), exist_ok=True)
            with open(MISSION_MODE_FILE, "w", encoding="utf-8") as f:
                f.write(serialized)
            self._last_mode_serialized = serialized
        except Exception:
            pass

    def close(self) -> None:
        self._mission_realtime_stop.set()
        self._mission_realtime_wake.set()
        if self._mission_realtime_thread.is_alive():
            self._mission_realtime_thread.join(timeout=1.0)
        with self._overlay_write_condition:
            self._overlay_writer_stop = True
            self._overlay_write_condition.notify_all()
        if self._overlay_writer_thread.is_alive():
            self._overlay_writer_thread.join(timeout=1.0)

    def restore_debug_overrides(self) -> None:
        'Call at shutdown to restore any debug flags the module overwrote.'
        self._restore_debug_overrides()
        self._restore_meter_refill_overrides()

    # ------------------------------------------------------------------
    # Toggle active slot (called from click handler in main)
    # ------------------------------------------------------------------

    def toggle_active_slot(self, slot_label: str) -> None:
        disabling = self._active_slot == slot_label
        if disabling:
            self._restore_debug_overrides(force_cpu_idle=True)
            self._restore_meter_refill_overrides()
            self._close_selector()
        self._active_slot = None if disabling else slot_label
        interpreter = self._menu_input_interpreter
        if interpreter is not None:
            try:
                interpreter.set_enabled(bool(self._active_slot))
            except Exception:
                pass
        if self._active_slot:
            self._capture_mission_owner(
                self._active_slot,
                getattr(self, "_render_snap_by_slot", {}),
            )
            owner_slot = self._mission_owner_slot(getattr(self, "_render_snap_by_slot", {}))
            snap = getattr(self, "_render_snap_by_slot", {}).get(owner_slot)
            self._sync_input_cursors_to_latest(owner_slot or self._active_slot, snap)
        else:
            self._clear_mission_owner()
        self._runtime = self._fresh_runtime(slot=self._active_slot)
        self.write_mode_state(force=True)
        self.write_overlay_data(force=True)

    # ------------------------------------------------------------------
    # Var state query (used by hud_overlay_manager)
    # ------------------------------------------------------------------

    def var_state(self, slot_label: str, snaps_dict: dict) -> dict:
        return self._mission_var_state(slot_label, snaps_dict)

    # ------------------------------------------------------------------
    # Debug flag helpers
    # ------------------------------------------------------------------

    def _resolve_debug_addr(self, name: str) -> int | None:
        try:
            for entry in self._read_debug_flags():
                if not entry or not isinstance(entry, (tuple, list)):
                    continue
                if entry[0] != name:
                    continue
                for item in entry[1:]:
                    if isinstance(item, int):
                        return item
        except Exception as e:
            print(f"[mission] merged lookup failed for {name!r}: {e!r}")

        try:
            dfa = self._debug_flag_addrs
            if isinstance(dfa, dict):
                entry = dfa.get(name)
                if isinstance(entry, int):
                    return entry
                if isinstance(entry, dict):
                    for key in ("addr", "address"):
                        if isinstance(entry.get(key), int):
                            return entry[key]
                if isinstance(entry, (tuple, list)):
                    for item in entry:
                        if isinstance(item, int):
                            return item
            elif isinstance(dfa, (list, tuple)):
                for entry in dfa:
                    if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                        if entry[0] == name and isinstance(entry[1], int):
                            return entry[1]
        except Exception as e:
            print(f"[mission] static lookup failed for {name!r}: {e!r}")

        print(f"[mission] missing debug flag addr for {name!r}")
        return None

    def _read_debug_flag(self, name: str) -> int | None:
        addr = self._resolve_debug_addr(name)
        if not isinstance(addr, int):
            return None
        try:
            return rd8(addr)
        except Exception:
            return None

    def _write_debug_flag(self, name: str, value: int) -> bool:
        addr = self._resolve_debug_addr(name)
        if not isinstance(addr, int):
            print(f"[mission] missing debug flag addr for {name!r}")
            return False
        try:
            wd8(addr, max(0, min(255, int(value))))
            return True
        except Exception as e:
            print(f"[mission] write failed for {name!r}: {e!r}")
            return False

    # ------------------------------------------------------------------
    # Debug override sync/restore
    # ------------------------------------------------------------------

    def _extract_active_overrides(self, payload: dict) -> dict[str, int]:
        if not isinstance(payload, dict):
            return {}

        raw = (
            payload.get("active_mission_setup_debug_flags")
            or payload.get("active_mission_debug_flags")
        )
        if isinstance(raw, dict):
            out = {}
            for key, value in raw.items():
                try:
                    out[str(key)] = max(0, min(255, int(value)))
                except Exception:
                    pass
            return out

        active_id = payload.get("active_mission_id")
        for mission in (payload.get("missions") or []):
            if not isinstance(mission, dict):
                continue
            if mission.get("mission_id") != active_id:
                continue
            raw = mission.get("setup_debug_flags") or mission.get("debug_flags") or {}
            if not isinstance(raw, dict):
                return {}
            out = {}
            for key, value in raw.items():
                try:
                    out[str(key)] = max(0, min(255, int(value)))
                except Exception:
                    pass
            return out

        return {}

    def _restore_debug_overrides(self, *, force_cpu_idle: bool = False) -> None:
        saved_values = dict(self._setup_state.get("saved_debug_values") or {})
        for name, original_value in saved_values.items():
            if isinstance(original_value, int):
                self._write_debug_flag(name, original_value)
                print(f"[mission restore] {name} -> {original_value}")
        if force_cpu_idle:
            self._write_debug_flag("CpuAction", 0)
            print("[mission restore] CpuAction -> 0")
        self._setup_state["mission_key"] = None
        self._setup_state["saved_debug_values"] = {}
        self._setup_state["applied_debug_values"] = {}

    def _mission_debug_key(self, payload: dict) -> tuple | None:
        if not isinstance(payload, dict) or not payload.get("active"):
            return None
        mission_id = payload.get("active_mission_id")
        if not mission_id:
            return None
        return (
            payload.get("slot"),
            payload.get("character"),
            mission_id,
        )

    def _prepare_selected_mission(self, character_name: str, mission_id: str) -> None:
        """Rearm setup flags when a mission is explicitly selected."""
        selected_key = (self._active_slot, character_name, mission_id)
        current_key = self._setup_state.get("mission_key")
        if current_key is not None and current_key != selected_key:
            self._restore_debug_overrides(force_cpu_idle=True)
        self._setup_state["completed_mission_key"] = None
        self._setup_state["idle_mission_key"] = None

    def _release_completed_mission_overrides(self, mission_key: tuple | None) -> None:
        """Release mission-owned flags immediately when the route clears."""
        if mission_key is None:
            return
        if self._setup_state.get("mission_key") is not None:
            self._restore_debug_overrides(force_cpu_idle=True)
        elif self._setup_state.get("idle_mission_key") != mission_key:
            self._write_debug_flag("CpuAction", 0)
            print("[mission restore] CpuAction -> 0")
        self._setup_state["completed_mission_key"] = mission_key
        self._setup_state["idle_mission_key"] = mission_key

    def _mission_meter_refill_enabled(self, payload: dict) -> bool:
        mission_id = str((payload or {}).get("active_mission_id") or "")
        return bool((payload or {}).get("active_mission_setup_meter_refill")) or mission_id in MISSION_METER_REFILL_MISSIONS

    def _restore_meter_refill_overrides(self) -> None:
        state = self._meter_refill_state
        saved_meter = state.get("saved_p1meter_flag")
        saved_baroque = state.get("saved_baroque_flag")
        if isinstance(saved_meter, int):
            self._write_debug_flag("P1Meter", saved_meter)
        if isinstance(saved_baroque, int):
            self._write_debug_flag("BaroquePct", saved_baroque)
        state["mission_key"] = None
        state["saved_p1meter_flag"] = None
        state["saved_baroque_flag"] = None

    def _sync_meter_refill_mission(self, payload: dict) -> bool:
        enabled = self._mission_meter_refill_enabled(payload)
        mission_key = (payload.get("slot"), payload.get("active_mission_id")) if enabled else None
        state = self._meter_refill_state
        if state.get("mission_key") == mission_key:
            return enabled

        if state.get("mission_key") is not None:
            self._restore_meter_refill_overrides()

        if mission_key is not None:
            state["mission_key"] = mission_key
            state["saved_p1meter_flag"] = int(self._read_debug_flag("P1Meter") or 0)
            state["saved_baroque_flag"] = int(self._read_debug_flag("BaroquePct") or 0)

        return enabled

    def _sync_debug_overrides(self, payload: dict) -> None:
        mission_key = self._mission_debug_key(payload)
        if mission_key is None:
            if self._setup_state.get("mission_key") is not None:
                self._restore_debug_overrides(force_cpu_idle=True)
            self._setup_state["completed_mission_key"] = None
            self._setup_state["idle_mission_key"] = None
            return

        if bool(payload.get("just_cleared")):
            self._release_completed_mission_overrides(mission_key)
            return

        if self._setup_state.get("completed_mission_key") == mission_key:
            # Completion owns CpuAction until the user explicitly selects a
            # mission again. Reassert idle if any later system changes it.
            if self._read_debug_flag("CpuAction") != 0:
                self._write_debug_flag("CpuAction", 0)
            self._setup_state["idle_mission_key"] = mission_key
            return

        overrides = self._extract_active_overrides(payload)
        current_key = self._setup_state.get("mission_key")

        if current_key != mission_key:
            if current_key is not None:
                self._restore_debug_overrides(force_cpu_idle=True)

            self._setup_state["completed_mission_key"] = None
            self._setup_state["idle_mission_key"] = None

            if "CpuAction" not in overrides:
                if self._read_debug_flag("CpuAction") != 0:
                    self._write_debug_flag("CpuAction", 0)
                self._setup_state["idle_mission_key"] = mission_key

            if not overrides:
                self._setup_state["mission_key"] = mission_key
                return

            saved = {}
            applied = {}
            for name, wanted in overrides.items():
                cur = self._read_debug_flag(name)
                if isinstance(cur, int):
                    saved[name] = cur
                if self._write_debug_flag(name, wanted):
                    applied[name] = wanted
                    print(
                        f"[mission apply] mission={payload.get('active_mission_id')} "
                        f"{name}: {cur} -> {wanted}"
                    )

            self._setup_state["mission_key"] = mission_key
            self._setup_state["saved_debug_values"] = saved
            self._setup_state["applied_debug_values"] = applied
            return

        for name, wanted in (self._setup_state.get("applied_debug_values") or {}).items():
            if self._read_debug_flag(name) != wanted:
                self._write_debug_flag(name, wanted)

        # Unless this mission explicitly owns CpuAction, idle is the permanent
        # mission default rather than a one-frame cleanup write.
        if "CpuAction" not in overrides and self._read_debug_flag("CpuAction") != 0:
            self._write_debug_flag("CpuAction", 0)
            self._setup_state["idle_mission_key"] = mission_key

    # ------------------------------------------------------------------
    # Selector helpers
    # ------------------------------------------------------------------

    def _close_selector(self, *, rearm: bool = False) -> None:
        slot = str(self._selector.get("source_slot") or self._active_slot or "")
        self._selector["open"] = False
        self._selector["sequence"] = []
        self._selector["sequence_blocked"] = False
        self._selector["opened_at"] = 0.0
        if rearm:
            self._selector["last_direction"] = 0
            self._selector["last_taunt_held"] = False
        interpreter = self._menu_input_interpreter
        if interpreter is not None:
            try:
                interpreter.close_menu("P2" if slot.startswith("P2") else "P1")
            except Exception:
                pass

    def _set_selector_source_slot(self, slot_label: str | None) -> None:
        """Reset gesture state when control moves to another point fighter."""
        normalized = str(slot_label or "") or None
        if self._selector.get("source_slot") == normalized:
            return
        self._selector["source_slot"] = normalized
        self._selector["sequence"] = []
        self._selector["sequence_blocked"] = False
        self._selector["last_direction"] = 0
        self._selector["last_taunt_held"] = False

    def _open_selector(self, character_name: str, now: float) -> None:
        payload = build_overlay_payload(character_name)
        missions = payload.get("missions", [])
        active_id = payload.get("active_mission_id")
        idx = next(
            (i for i, m in enumerate(missions) if m.get("mission_id") == active_id),
            0,
        )
        self._selector["open"] = True
        self._selector["selected_index"] = idx
        self._selector["opened_at"] = now
        self._selector["hint_until"] = now + 8.0
        self._selector["sequence"] = []
        self._selector["sequence_blocked"] = False
        slot = str(self._selector.get("source_slot") or self._active_slot or "")
        interpreter = self._menu_input_interpreter
        if interpreter is not None and slot:
            try:
                interpreter.set_menu_open("P2" if slot.startswith("P2") else "P1", slot)
            except Exception:
                pass

    def _label_is_crouch(self, label: str) -> bool:
        return (label or "").strip().lower() in {"crouched", "crouching"}

    def _label_is_taunt(self, label: str) -> bool:
        return (label or "").strip().lower() == "taunt"

    def _native_point_slot(self, slot_label: str, snaps_dict: dict) -> str | None:
        """Resolve the team's actual point fighter from the native point flag.

        Assist calls can shuffle which live snapshot looks active, but they do
        not change this flag. A real tag does, so Down, Down, Taunt can affirm
        the newly tagged point character without assist calls stealing Mission
        Mode ownership.
        """
        if not slot_label:
            return None
        prefix = "P1" if str(slot_label).startswith("P1") else "P2"
        c1 = f"{prefix}-C1"
        c2 = f"{prefix}-C2"
        s1 = snaps_dict.get(c1) if isinstance(snaps_dict, dict) else None
        s2 = snaps_dict.get(c2) if isinstance(snaps_dict, dict) else None

        if s1 and not s2:
            return c1
        if s2 and not s1:
            return c2
        if not s1 and not s2:
            return None

        c1_is_point = bool((s1 or {}).get("damage_point_active", (s1 or {}).get("damage_is_point")))
        c2_is_point = bool((s2 or {}).get("damage_point_active", (s2 or {}).get("damage_is_point")))
        if c1_is_point != c2_is_point:
            return c1 if c1_is_point else c2

        return self._team_active_slot(slot_label, snaps_dict)

    def _selector_source_slot(self, snaps_dict: dict) -> str | None:
        if not self._active_slot:
            return None

        # Once open, keep reading the affirmed owner. While closed, listen to
        # the native point fighter so a real tag can intentionally rebind it.
        if self._selector.get("open"):
            owner_slot = self._mission_owner_slot(snaps_dict)
            return owner_slot or self._active_slot
        return self._native_point_slot(self._active_slot, snaps_dict) or self._active_slot

    def _affirm_selector_point_owner(self, snaps_dict: dict) -> tuple[str | None, str]:
        """Pin Mission Mode to the point fighter performing the shortcut."""
        point_slot = self._native_point_slot(self._active_slot or "", snaps_dict)
        if not point_slot:
            return None, ""
        snap = snaps_dict.get(point_slot) if isinstance(snaps_dict, dict) else None
        if not isinstance(snap, dict):
            return None, ""

        self._active_slot = point_slot
        self._capture_mission_owner(point_slot, snaps_dict)
        return point_slot, self._mission_owner_name(snaps_dict)

    def _input_packets_for_slot(
        self,
        slot_label: str,
        snap: dict,
        *,
        consumer: str,
    ) -> list[dict]:
        """Return every unseen realtime observation for one consumer.

        Mission matching consumes the immutable combat event stream. The
        selector shortcut intentionally consumes the sampler's raw input-edge
        queue instead. Down, Down, Taunt is a gesture, not a combat event
        grammar, and converting unrelated ACTION/HITSTUN events back into input
        packets can overwrite its direction-edge state between taps.
        """
        provider = self._input_sample_provider
        prefer_input_queue = str(consumer) == "selector" and callable(provider)
        if callable(self._event_provider) and not prefer_input_queue:
            events = self._events_for_consumer(consumer, slot_label)
            return [event.as_packet() for event in events]

        latest: dict = {}
        samples: list[dict] = []
        if callable(provider):
            try:
                latest, samples = provider(
                    slot_label,
                    int((snap or {}).get("base") or 0),
                )
            except Exception:
                latest, samples = {}, []

        cursor_key = (str(consumer), str(slot_label))
        cursor = int(self._input_sample_cursor_by_consumer.get(cursor_key, 0) or 0)
        fresh = [
            dict(sample)
            for sample in (samples or [])
            if int((sample or {}).get("seq", 0) or 0) > cursor
        ]
        fresh.sort(key=lambda sample: int(sample.get("seq", 0) or 0))
        if fresh:
            self._input_sample_cursor_by_consumer[cursor_key] = max(
                int(sample.get("seq", 0) or 0) for sample in fresh
            )
            return fresh

        if latest:
            packet = dict(latest)
            packet.setdefault("seq", cursor)
            return [packet]

        try:
            packet = input_monitor.read_overlay_input_packet(
                slot_label,
                int((snap or {}).get("base") or 0),
            )
        except Exception:
            packet = {}
        return [packet] if packet else []

    def _consume_menu_input_commands(self, snaps_dict: dict, now: float) -> None:
        """Apply commands from the standalone realtime menu interpreter."""
        interpreter = self._menu_input_interpreter
        if interpreter is None:
            return
        try:
            commands = interpreter.drain_commands()
        except Exception:
            return

        all_snaps = dict(getattr(self, "_render_snap_by_slot", {}) or {})
        all_snaps.update(snaps_dict or {})

        for command in commands or ():
            if not isinstance(command, MissionMenuCommand):
                continue
            kind = str(command.kind or "")
            slot = str(command.slot or "")

            if kind == MISSION_MENU_CLOSE:
                self._close_selector()
                continue

            if kind == MISSION_MENU_OPEN:
                # Mission Select shortcut is Mission Mode-only. Never let a
                # global Down, Down, Taunt gesture activate Mission Mode.
                if not self._active_slot:
                    try:
                        interpreter.close_menu(command.teamtag)
                    except Exception:
                        pass
                    continue
                active_team = "P2" if str(self._active_slot).startswith("P2") else "P1"
                if command.teamtag != active_team:
                    try:
                        interpreter.close_menu(command.teamtag)
                    except Exception:
                        pass
                    continue
                snap = all_snaps.get(slot)
                if not isinstance(snap, dict):
                    try:
                        interpreter.close_menu(command.teamtag)
                    except Exception:
                        pass
                    continue

                # The shortcut owns activation and follows the native point slot.
                self._active_slot = slot
                self._capture_mission_owner(slot, all_snaps)
                self._set_selector_source_slot(slot)
                character_name = self._mission_owner_name(all_snaps)
                payload = build_overlay_payload(character_name) if character_name else {}
                if not (payload.get("missions") or []):
                    self._close_selector()
                    continue

                self._runtime = self._fresh_runtime(slot=slot)
                self._open_selector(character_name, float(now))
                self.write_mode_state(force=True)
                continue

            if not self._selector.get("open"):
                continue

            character_name = self._mission_owner_name(all_snaps)
            payload = build_overlay_payload(character_name) if character_name else {}
            missions = list(payload.get("missions") or [])
            if not missions:
                self._close_selector()
                continue

            if kind == MISSION_MENU_NEXT:
                self._selector["selected_index"] = (
                    int(self._selector.get("selected_index", 0)) + 1
                ) % len(missions)
                self._selector["opened_at"] = float(now)
                self._selector["hint_until"] = float(now) + 8.0
                continue

            if kind == MISSION_MENU_PREVIOUS:
                self._selector["selected_index"] = (
                    int(self._selector.get("selected_index", 0)) - 1
                ) % len(missions)
                self._selector["opened_at"] = float(now)
                self._selector["hint_until"] = float(now) + 8.0
                continue

            if kind == MISSION_MENU_SELECT:
                idx = int(self._selector.get("selected_index", 0)) % len(missions)
                mission_id = missions[idx].get("mission_id")
                if mission_id:
                    progress = load_progress()
                    progress = set_selected_mission_id(
                        progress, character_name, mission_id
                    )
                    save_progress(progress)
                    self._prepare_selected_mission(character_name, mission_id)
                    self._runtime = self._fresh_runtime(slot=self._active_slot)
                self._close_selector(rearm=True)
                self.write_mode_state(force=True)

        if self._selector.get("open") and float(now) - float(
            self._selector.get("opened_at", 0.0) or 0.0
        ) > 8.0:
            self._close_selector()

    def _update_selector_from_inputs(self, snaps_dict: dict, now: float) -> None:
        """Legacy adapter for tests and standalone callers.

        Production wiring attaches MissionMenuInputInterpreter directly to the
        240 Hz sampler. This fallback only forwards cached raw packets into the
        same standalone interpreter when an older caller invokes this method.
        Gesture state and parsing still do not live in MissionManager.
        """
        if self._menu_input_interpreter is None and callable(self._input_sample_provider):
            try:
                from tvcgui.runtime.mission_menu_input import MissionMenuInputInterpreter

                self._menu_input_interpreter = MissionMenuInputInterpreter()
                self._menu_input_interpreter.set_enabled(bool(self._active_slot))
            except Exception:
                return

        interpreter = self._menu_input_interpreter
        if interpreter is not None and callable(self._input_sample_provider):
            selector_slot = self._selector_source_slot(snaps_dict)
            snap = (snaps_dict or {}).get(selector_slot) if selector_slot else None
            if selector_slot and isinstance(snap, dict):
                packets = self._input_packets_for_slot(
                    selector_slot, snap, consumer="selector"
                )
                point_active = bool(
                    snap.get(
                        "damage_point_active",
                        snap.get("damage_is_point", True),
                    )
                )
                for packet in packets:
                    forwarded = dict(packet or {})
                    forwarded.setdefault("slot", selector_slot)
                    forwarded.setdefault("base", int(snap.get("base", 0) or 0))
                    forwarded.setdefault("char_id", self._snap_char_id(snap))
                    forwarded.setdefault("point_active", point_active)
                    try:
                        interpreter.on_sample(selector_slot, forwarded)
                    except Exception:
                        continue

        self._consume_menu_input_commands(snaps_dict, now)

    # ------------------------------------------------------------------
    # Team / slot helpers
    # ------------------------------------------------------------------

    def _team_active_slot(self, slot_label: str, snaps_dict: dict) -> str | None:
        prefix = "P1" if slot_label.startswith("P1") else "P2"
        c1 = f"{prefix}-C1"
        c2 = f"{prefix}-C2"
        s1 = snaps_dict.get(c1)
        s2 = snaps_dict.get(c2)

        if s1 and not s2:
            return c1
        if s2 and not s1:
            return c2
        if not s1 and not s2:
            return None

        # Native fighter+0x44A0 is authoritative for the point character.
        # Assist calls leave it alone; real tags change it.
        c1_is_point = bool(s1.get("damage_point_active", s1.get("damage_is_point")))
        c2_is_point = bool(s2.get("damage_point_active", s2.get("damage_is_point")))
        if c1_is_point != c2_is_point:
            return c1 if c1_is_point else c2

        mv1 = s1.get("attA") or s1.get("attB") or 0
        mv2 = s2.get("attA") or s2.get("attB") or 0
        c1_off = mv1 in MISSION_ASSIST_OFF_STATES
        c2_off = mv2 in MISSION_ASSIST_OFF_STATES
        if c2_off and not c1_off:
            return c1
        if c1_off and not c2_off:
            return c2
        return c1

    def _partner_slot(self, slot_label: str) -> str | None:
        if slot_label.endswith("C1"):
            return slot_label[:-2] + "C2"
        if slot_label.endswith("C2"):
            return slot_label[:-2] + "C1"
        return None

    def _mission_var_state(self, slot_label: str, snaps_dict: dict) -> dict:
        partner_slot = self._partner_slot(slot_label)
        partner_snap = snaps_dict.get(partner_slot) if partner_slot else None
        target_snap = snaps_dict.get(slot_label)
        active_slot = self._team_active_slot(slot_label, snaps_dict)

        partner_exists = bool(
            isinstance(partner_snap, dict)
            and isinstance(target_snap, dict)
            and partner_snap.get("base") != target_snap.get("base")
        )
        wrong_character_ready = bool(
            partner_exists
            and active_slot == partner_slot
            and active_slot != slot_label
        )
        label = (partner_snap.get("mv_label") or "").strip().lower() if partner_snap else ""
        partner_airborne = wrong_character_ready and any(
            t in label for t in MISSION_AIRBORNE_LABEL_TOKENS
        )
        partner_varing = wrong_character_ready and any(
            t in label for t in MISSION_VAR_LABEL_TOKENS
        )
        return {
            "target_slot": slot_label,
            "partner_slot": partner_slot,
            "team_active_slot": active_slot,
            "partner_exists": partner_exists,
            "wrong_character_ready": wrong_character_ready,
            "partner_airborne": partner_airborne,
            "partner_varing": partner_varing,
            "var_ready": partner_airborne,
        }

    # ------------------------------------------------------------------
    # Opponent query helpers
    # ------------------------------------------------------------------

    def _sample_health_deltas(self, snaps_dict: dict, frame_idx: int) -> None:
        """Capture health changes continuously, even while mission mode is idle."""
        events: list[dict[str, Any]] = []
        live_bases: set[int] = set()

        for snap in (snaps_dict or {}).values():
            if not isinstance(snap, dict):
                continue
            base = snap.get("base")
            cur_hp = snap.get("cur")
            teamtag = str(snap.get("teamtag") or "")
            if not isinstance(base, int) or not isinstance(cur_hp, int):
                continue

            live_bases.add(base)
            previous = self._health_history_by_base.get(base)
            if isinstance(previous, dict):
                prev_hp = previous.get("cur")
                if isinstance(prev_hp, int) and cur_hp < prev_hp:
                    events.append({
                        "base": base,
                        "teamtag": teamtag,
                        "damage": prev_hp - cur_hp,
                    })

            self._health_history_by_base[base] = {
                "cur": cur_hp,
                "teamtag": teamtag,
            }

        for base in list(self._health_history_by_base):
            if base not in live_bases:
                del self._health_history_by_base[base]

        self._health_damage_events = events
        self._health_damage_frame = int(frame_idx)

    def _opponent_in_state(self, slot_label: str, snaps_dict: dict, state_ids: set) -> bool:
        if not slot_label:
            return False

        my_team = "P1" if slot_label.startswith("P1") else "P2"

        for other_snap in snaps_dict.values():
            if not isinstance(other_snap, dict):
                continue
            if other_snap.get("teamtag") == my_team:
                continue

            # Only the primary live action is authoritative. ``attB`` can
            # retain an old reaction value after recovery and previously kept
            # missions alive long enough for late steps to count.
            att_a = other_snap.get("attA")
            if att_a in state_ids:
                return True

        return False

    def _opponent_in_hitstun(self, slot_label: str, snaps_dict: dict) -> bool:
        if not slot_label:
            return False
        my_team = "P1" if str(slot_label).startswith("P1") else "P2"
        if callable(self._event_provider):
            return any(
                bool(active) and ("P1" if str(other_slot).startswith("P1") else "P2") != my_team
                for other_slot, active in self._event_state.get("hitstun_by_slot", {}).items()
            )

        if self._opponent_in_state(slot_label, snaps_dict, MISSION_REACTION_STATES):
            return True
        for other_snap in (snaps_dict or {}).values():
            if not isinstance(other_snap, dict) or other_snap.get("teamtag") == my_team:
                continue
            try:
                if int(other_snap.get("timing_hitstun_remaining", 0) or 0) > 0:
                    return True
            except Exception:
                pass
        return False

    def _opponent_in_megacrash(self, slot_label: str, snaps_dict: dict) -> bool:
        if not slot_label:
            return False
        if callable(self._event_provider):
            my_team = "P1" if str(slot_label).startswith("P1") else "P2"
            return any(
                bool(active) and ("P1" if str(other_slot).startswith("P1") else "P2") != my_team
                for other_slot, active in self._event_state.get("megacrash_by_slot", {}).items()
            )
        return self._opponent_in_state(slot_label, snaps_dict, MISSION_MEGACRASH_STATES)

    def _global_combo_count(self) -> int | None:
        """Return the combo count already published by the realtime sampler."""
        if callable(self._event_provider):
            return int(self._event_state.get("combo_count", 0) or 0)
        value = self._runtime.get("global_combo_count")
        return int(value) if value is not None else None

    def _opponent_damage_this_frame(self, slot_label: str, snaps_dict: dict) -> list[int]:
        if not slot_label:
            return []
        my_team = "P1" if slot_label.startswith("P1") else "P2"

        if callable(self._event_provider):
            cursor = int(self._runtime.get("last_damage_event_sequence", 0) or 0)
            damage_events = list(self._event_state.get("damage_events", []) or [])
            fresh = [event for event in damage_events if int(event.sequence) > cursor]
            if fresh:
                self._runtime["last_damage_event_sequence"] = max(
                    int(event.sequence) for event in fresh
                )
            return [
                int(event.damage)
                for event in fresh
                if event.team and event.team != my_team and int(event.damage) > 0
            ]

        # Prefer the manager-wide sample. It is collected before mission
        # commands and survives mission changes, so the first hit cannot be
        # lost just because the mission runtime was reset that frame.
        sampled_frame = int(self._health_damage_frame)
        if (
            sampled_frame == int(self._frame_idx)
            and int(self._runtime.get("last_sampled_damage_frame", -1)) != sampled_frame
        ):
            self._runtime["last_sampled_damage_frame"] = sampled_frame
            return [
                int(event.get("damage", 0) or 0)
                for event in self._health_damage_events
                if str(event.get("teamtag") or "") != my_team
                and int(event.get("damage", 0) or 0) > 0
            ]

        # Fallback for direct calls that occur without update().
        hp_cache = self._runtime.setdefault("opponent_hp_by_base", {})
        live_bases: set = set()
        damage_values: list[int] = []

        for other_snap in snaps_dict.values():
            if not isinstance(other_snap, dict):
                continue
            if other_snap.get("teamtag") == my_team:
                continue
            base = other_snap.get("base")
            cur_hp = other_snap.get("cur")
            if not isinstance(base, int) or not isinstance(cur_hp, int):
                continue
            live_bases.add(base)
            prev = hp_cache.get(base)
            if isinstance(prev, int) and cur_hp < prev:
                damage_values.append(prev - cur_hp)
            hp_cache[base] = cur_hp

        for base in list(hp_cache):
            if base not in live_bases:
                del hp_cache[base]

        return damage_values


    def _action_can_own_hit(
        self,
        action: dict,
        frame_idx: int,
        *,
        sample_ns: int = 0,
    ) -> bool:
        timing = self._prediction_timing_for_action(action)
        startup = max(1, int(timing.get("startup", 0) or 0))
        active = max(1, int(timing.get("active", 0) or 0))
        action_ns = int((action or {}).get("sample_ns", 0) or 0)
        hit_ns = int(sample_ns or 0)
        if action_ns > 0 and hit_ns > 0:
            earliest_ns, deadline_ns = self._prediction_window_ns(action)
            last_sample_ns = int((action or {}).get("last_sample_ns", action_ns) or action_ns)
            deadline_ns = max(
                deadline_ns,
                last_sample_ns
                + int(MISSION_PREDICTION_CONFIRM_PADDING * (1_000_000_000 / 60.0)),
            )
            return earliest_ns <= hit_ns <= deadline_ns
        action_frame = int((action or {}).get("frame", frame_idx) or frame_idx)
        action_last_frame = int((action or {}).get("last_frame", action_frame) or action_frame)
        earliest_frame = action_frame + max(0, startup - 2)
        deadline_frame = max(
            action_frame + startup + active + MISSION_PREDICTION_CONFIRM_PADDING,
            action_last_frame + MISSION_PREDICTION_CONFIRM_PADDING,
        )
        return earliest_frame <= int(frame_idx) <= deadline_frame

    def _candidate_action_serials_for_hit(
        self,
        frame_idx: int,
        *,
        sample_ns: int = 0,
        sample_seq: int = 0,
    ) -> list[int]:
        """Return every live native action that could have produced a hit.

        Delayed attacks, projectiles, install releases, and attacks that require
        a later trigger can still connect after another native action begins.
        Treating only the newest action as the owner makes those hits impossible
        to confirm.  Keep the full eligible set; the ordered mission step later
        claims the matching action after real hit evidence arrives.
        """
        hit_seq = int(sample_seq or 0)
        live_actions = []
        directly_eligible = []
        for action in self._runtime.get("mission_action_events", []):
            if action.get("consumed"):
                continue
            action_seq = int(action.get("sample_seq", 0) or 0)
            if hit_seq and action_seq and action_seq > hit_seq:
                continue
            live_actions.append(action)
            if self._action_can_own_hit(action, frame_idx, sample_ns=sample_ns):
                directly_eligible.append(action)

        # Cinematic supers and other scripted attacks can traverse several
        # native action IDs after one command. If any phase is eligible at the
        # hit frame, every earlier phase from that same input command remains a
        # candidate. This keeps the player-facing move as the hit owner without
        # permitting an unrelated later input to donate its hit.
        eligible_chains = {
            (
                int(action.get("char_id", 0) or 0),
                int(
                    action.get("command_serial", action.get("input_serial", 0)) or 0
                ),
            )
            for action in directly_eligible
            if int(action.get("command_serial", action.get("input_serial", 0)) or 0) > 0
        }
        candidates: list[int] = []
        for action in live_actions:
            chain_key = (
                int(action.get("char_id", 0) or 0),
                int(
                    action.get("command_serial", action.get("input_serial", 0)) or 0
                ),
            )
            if action not in directly_eligible and chain_key not in eligible_chains:
                continue
            serial = int(action.get("serial", 0) or 0)
            if serial > 0 and serial not in candidates:
                candidates.append(serial)

        # Generic delayed-hit support. A mission step may declare a longer
        # confirmation window for summons, traps, stored attacks, or glitches
        # that require a later trigger action. This is data-driven and is not
        # tied to any character or move name.
        pending_serial = int(self._runtime.get("pending_action_serial", 0) or 0)
        pending_deadline = int(
            self._runtime.get("pending_delayed_confirm_until_frame", -9999) or -9999
        )
        if pending_serial > 0 and int(frame_idx) <= pending_deadline:
            pending_action = self._mission_action_event_by_serial(pending_serial)
            if pending_action is not None:
                pending_seq = int(pending_action.get("sample_seq", 0) or 0)
                if not hit_seq or not pending_seq or pending_seq <= hit_seq:
                    if pending_serial not in candidates:
                        candidates.insert(0, pending_serial)
        return candidates

    def _owner_action_serial_for_hit(
        self,
        frame_idx: int,
        *,
        sample_ns: int = 0,
        sample_seq: int = 0,
    ) -> int:
        """Compatibility view of the newest eligible action owner."""
        candidates = self._candidate_action_serials_for_hit(
            frame_idx,
            sample_ns=sample_ns,
            sample_seq=sample_seq,
        )
        return int(candidates[-1] if candidates else 0)

    def _hit_can_confirm_action(self, hit: dict, action_serial: int) -> bool:
        wanted = int(action_serial or 0)
        if wanted <= 0:
            return True
        candidates = [
            int(serial or 0)
            for serial in ((hit or {}).get("candidate_action_serials") or [])
            if int(serial or 0) > 0
        ]
        if candidates:
            return wanted in candidates
        # Backward compatibility for buffered events and older tests created
        # before candidate ownership was introduced.
        return int((hit or {}).get("owner_action_serial", 0) or 0) == wanted

    def _record_mission_hit_events(
        self,
        global_combo_count: int | None,
        damage_values: list[int],
        frame_idx: int,
    ) -> int:
        """Merge HP and combo-counter evidence into physical hit events.

        TvC does not publish the related writes in a stable order. HP can fall
        before the combo counter changes, or the counter can change first. An
        HP edge creates an event immediately for responsiveness. A nearby combo
        edge is attached to that same event instead of creating a duplicate.
        """
        events = self._runtime.setdefault("mission_hit_events", [])
        previous_count = self._runtime.get("last_global_combo_count")

        combo_hits = 0
        if global_combo_count is not None:
            current_count = max(0, int(global_combo_count))
            if previous_count is None:
                # A fresh mission/runtime can first observe the counter after
                # several rapid hits. Preserve the full count instead of
                # collapsing the entire opening string into one hit event.
                combo_hits = current_count
            else:
                previous_count = max(0, int(previous_count))
                if current_count > previous_count:
                    combo_hits = current_count - previous_count
            self._runtime["last_global_combo_count"] = current_count

        positive_damage = [int(value) for value in damage_values if int(value or 0) > 0]
        new_event_count = 0

        def new_event(
            *,
            hp_seen: bool,
            combo_seen: bool,
            damage: int = 0,
            owner_action_serial: int = 0,
        ) -> dict:
            nonlocal new_event_count
            serial = int(self._runtime.get("mission_hit_serial", 0) or 0) + 1
            self._runtime["mission_hit_serial"] = serial
            candidate_action_serials = self._candidate_action_serials_for_hit(frame_idx)
            if int(owner_action_serial or 0) > 0 and int(owner_action_serial) not in candidate_action_serials:
                candidate_action_serials.append(int(owner_action_serial))
            event = {
                "serial": serial,
                "frame": int(frame_idx),
                "last_frame": int(frame_idx),
                "hp_seen": bool(hp_seen),
                "combo_seen": bool(combo_seen),
                "damage": max(0, int(damage or 0)),
                # Keep the newest owner for compatibility/debug display, but
                # mission confirmation uses the full candidate set below.
                "owner_action_serial": int(owner_action_serial or (candidate_action_serials[-1] if candidate_action_serials else 0)),
                "candidate_action_serials": candidate_action_serials,
                "consumed": False,
            }
            events.append(event)
            new_event_count += 1
            return event

        # HP is the lowest-latency signal. Pair it with a combo-only event from
        # the previous few frames when the counter happened to publish first.
        for damage in positive_damage:
            match = next((
                event for event in reversed(events)
                if not event.get("hp_seen")
                and 0 <= int(frame_idx) - int(event.get("last_frame", event.get("frame", frame_idx))) <= MISSION_HIT_CORRELATION_WINDOW
            ), None)
            owner_action_serial = self._owner_action_serial_for_hit(frame_idx)
            if match is None:
                new_event(
                    hp_seen=True,
                    combo_seen=False,
                    damage=damage,
                    owner_action_serial=owner_action_serial,
                )
            else:
                match["hp_seen"] = True
                match["damage"] = int(match.get("damage", 0) or 0) + damage
                if owner_action_serial:
                    match["owner_action_serial"] = owner_action_serial
                merged_candidates = list(match.get("candidate_action_serials") or [])
                for serial in self._candidate_action_serials_for_hit(frame_idx):
                    if serial not in merged_candidates:
                        merged_candidates.append(serial)
                if owner_action_serial and owner_action_serial not in merged_candidates:
                    merged_candidates.append(owner_action_serial)
                match["candidate_action_serials"] = merged_candidates
                match["last_frame"] = int(frame_idx)

        # Counter increments are authoritative for hit count. Pair each one to
        # a recent HP-only event before allocating another physical hit.
        for _ in range(max(0, int(combo_hits))):
            match = next((
                event for event in reversed(events)
                if not event.get("combo_seen")
                and 0 <= int(frame_idx) - int(event.get("last_frame", event.get("frame", frame_idx))) <= MISSION_HIT_CORRELATION_WINDOW
            ), None)
            if match is None:
                new_event(hp_seen=False, combo_seen=True)
            else:
                match["combo_seen"] = True
                match["last_frame"] = int(frame_idx)

        if positive_damage or combo_hits > 0 or new_event_count > 0:
            self._runtime["mission_last_hit_evidence_frame"] = int(frame_idx)

        events[:] = [
            event for event in events
            if int(frame_idx) - int(event.get("frame", frame_idx)) <= MISSION_HIT_CONFIRM_WINDOW
            and not (
                bool(event.get("consumed"))
                and int(frame_idx) - int(event.get("last_frame", event.get("frame", frame_idx))) > MISSION_HIT_CORRELATION_WINDOW
            )
        ]
        return new_event_count

    def _peek_mission_hit_event(
        self,
        frame_idx: int,
        min_frame: int | None = None,
        owner_action_serial: int = 0,
    ) -> dict | None:
        floor = -999999 if min_frame is None else int(min_frame)
        wanted_owner = int(owner_action_serial or 0)
        for event in self._runtime.get("mission_hit_events", []):
            event_frame = int(event.get("frame", frame_idx))
            age = int(frame_idx) - event_frame
            if wanted_owner and not self._hit_can_confirm_action(event, wanted_owner):
                continue
            if (
                event_frame >= floor
                and 0 <= age <= MISSION_HIT_CONFIRM_WINDOW
                and not event.get("consumed")
            ):
                return event
        return None

    def _consume_mission_hit_event(
        self,
        frame_idx: int,
        min_frame: int | None = None,
        owner_action_serial: int = 0,
    ) -> bool:
        event = self._peek_mission_hit_event(
            frame_idx,
            min_frame=min_frame,
            owner_action_serial=owner_action_serial,
        )
        if not event:
            return False
        event["consumed"] = True
        return True

    def _mission_action_event_by_serial(self, serial: int) -> dict | None:
        wanted = int(serial or 0)
        if wanted <= 0:
            return None
        for event in self._runtime.get("mission_action_events", []):
            if int(event.get("serial", 0) or 0) == wanted:
                return event
        return None

    def _pending_command_chain_active(
        self,
        pending_action_serial: int,
        pending_input_serial: int,
    ) -> bool:
        pending_action = self._mission_action_event_by_serial(pending_action_serial)
        command_serial = 0
        if pending_action is not None:
            command_serial = int(
                pending_action.get(
                    "command_serial",
                    pending_action.get("input_serial", 0),
                )
                or 0
            )
        if command_serial <= 0:
            command_serial = int(pending_input_serial or 0)
        if command_serial <= 0:
            return False

        live_events = [
            event
            for event in self._runtime.get("mission_action_events", [])
            if not event.get("consumed")
        ]
        if not live_events:
            return False
        latest = max(live_events, key=lambda event: int(event.get("serial", 0) or 0))
        latest_command = int(
            latest.get("command_serial", latest.get("input_serial", 0)) or 0
        )
        return bool(
            latest_command == command_serial
            and int(latest.get("serial", 0) or 0) >= int(pending_action_serial or 0)
        )

    def _mission_input_event_by_serial(self, serial: int) -> dict | None:
        wanted = int(serial or 0)
        if wanted <= 0:
            return None
        for event in self._runtime.get("mission_input_events", []):
            if int(event.get("serial", 0) or 0) == wanted:
                return event
        return None

    def _action_event_matches_step(self, event: dict, step: Any, labels: list[str]) -> bool:
        candidates = list((event or {}).get("labels") or [])
        notation = self._step_input_notation(step)
        expected = list(labels or [])
        if notation:
            expected.append(notation)
        for candidate in candidates:
            if self._mission_label_matches(str(candidate), expected):
                return True
        return False

    def _latest_matching_action_event(self, step: Any, labels: list[str]) -> dict | None:
        consumed = int(self._runtime.get("mission_action_consumed_serial", 0) or 0)
        for event in reversed(self._runtime.get("mission_action_events", [])):
            if event.get("consumed"):
                continue
            if int(event.get("serial", 0) or 0) <= consumed:
                continue
            if self._action_event_matches_step(event, step, labels):
                return event
        return None

    def _prediction_timing_for_action(self, event: dict) -> dict[str, int]:
        char_id = int((event or {}).get("char_id", 0) or 0)
        action_id = int((event or {}).get("action_id", 0) or 0) & 0x7FFF
        timing = self._mission_timing_by_action.get((char_id, action_id))
        if timing:
            return dict(timing)
        return {
            "startup": MISSION_PREDICTION_FALLBACK_STARTUP,
            "active": MISSION_PREDICTION_FALLBACK_ACTIVE,
            "hitstun": 0,
        }

    def _prediction_window_ns(self, event: dict) -> tuple[int, int]:
        timing = self._prediction_timing_for_action(event)
        startup = max(1, int(timing.get("startup", 0) or 0))
        active = max(1, int(timing.get("active", 0) or 0))
        start_ns = int((event or {}).get("sample_ns", 0) or 0)
        if start_ns <= 0:
            start_ns = time.monotonic_ns()
        earliest = start_ns + int(max(0, startup - 2) * (1_000_000_000 / 60.0))
        latest_frames = startup + active + MISSION_PREDICTION_CONFIRM_PADDING
        latest = start_ns + int(latest_frames * (1_000_000_000 / 60.0))
        return earliest, latest

    def _step_supports_prediction(self, step: Any, labels: list[str]) -> bool:
        """Return whether the step may advance visually before hit confirmation."""
        if not labels or self._step_grace(step) > 0:
            return False
        if (
            self._step_is_pass(step)
            or self._step_is_baroque_cancel(labels)
            or self._step_is_megacrash(labels)
            or self._step_allows_whiff_confirm(labels, step)
        ):
            return False
        return True

    def _refresh_predicted_progress(
        self,
        steps: list,
        confirmed_progress: int,
    ) -> int:
        """Predict the ordered prefix from native actions without confirming it.

        The real progress index remains authoritative. This method only lets the
        overlay follow already-observed native action transitions while HP and
        combo evidence arrive a few frames later.
        """
        confirmed = max(0, int(confirmed_progress or 0))
        if confirmed >= len(steps):
            self._runtime["predicted_progress_index"] = confirmed
            self._runtime["prediction_entries"] = []
            return confirmed

        now_ns = time.monotonic_ns()
        max_age_ns = int(MISSION_PREDICTION_MAX_AGE_SECONDS * 1_000_000_000)
        actions = sorted(
            [
                event for event in self._runtime.get("mission_action_events", [])
                if not event.get("consumed")
                and not event.get("prediction_rejected")
                and int(event.get("serial", 0) or 0)
                > int(self._runtime.get("mission_action_consumed_serial", 0) or 0)
                and (
                    int(event.get("sample_ns", 0) or 0) <= 0
                    or now_ns - int(event.get("sample_ns", 0) or 0) <= max_age_ns
                )
            ],
            key=lambda event: (
                int(event.get("sample_ns", 0) or 0),
                int(event.get("sample_seq", 0) or 0),
                int(event.get("serial", 0) or 0),
            ),
        )

        predicted = confirmed
        entries: list[dict[str, Any]] = []
        action_cursor = 0
        while (
            predicted < len(steps)
            and len(entries) < MISSION_PREDICTION_MAX_STEPS
        ):
            step = steps[predicted]
            labels = self._step_labels(step)
            if not self._step_supports_prediction(step, labels):
                break

            match_index = None
            for index in range(action_cursor, len(actions)):
                action = actions[index]
                if self._action_event_matches_step(action, step, labels):
                    match_index = index
                    break
                # Native attack actions are ordered route evidence. Do not jump
                # over a different attack to make a later step appear valid.
                if int(action.get("action_id", 0) or 0) >= 0x100:
                    break
            if match_index is None:
                break

            action = actions[match_index]
            earliest_ns, deadline_ns = self._prediction_window_ns(action)
            if now_ns > deadline_ns:
                action["prediction_rejected"] = True
                action_cursor = match_index + 1
                continue

            timing = self._prediction_timing_for_action(action)
            entries.append({
                "step_index": predicted,
                "action_serial": int(action.get("serial", 0) or 0),
                "action_id": int(action.get("action_id", 0) or 0),
                "label": str((action.get("labels") or [""])[0]),
                "startup": int(timing.get("startup", 0) or 0),
                "active": int(timing.get("active", 0) or 0),
                "hitstun": int(timing.get("hitstun", 0) or 0),
                "earliest_confirm_ns": earliest_ns,
                "deadline_ns": deadline_ns,
            })
            predicted += 1
            action_cursor = match_index + 1

        previous = int(self._runtime.get("predicted_progress_index", confirmed) or confirmed)
        self._runtime["predicted_progress_index"] = predicted
        self._runtime["prediction_entries"] = entries
        if predicted != previous:
            self._runtime["prediction_revision"] = int(
                self._runtime.get("prediction_revision", 0) or 0
            ) + 1
        return predicted

    def _prune_mission_event_buffers(self) -> None:
        """Keep only the compact live suffix required by mission matching."""
        actions = list(self._runtime.get("mission_action_events", []) or [])
        actions = [event for event in actions if not event.get("consumed")]
        self._runtime["mission_action_events"] = actions[-MISSION_ACTION_EVENT_LIMIT:]

        # Keep recently consumed hits briefly because the global combo counter
        # may update after the HP edge and still needs to correlate with that
        # same physical hit instead of creating a duplicate.
        hits = list(self._runtime.get("mission_hit_events", []) or [])
        self._runtime["mission_hit_events"] = hits[-MISSION_HIT_EVENT_LIMIT:]

        inputs = list(self._runtime.get("mission_input_events", []) or [])
        consumed = int(self._runtime.get("mission_input_consumed_serial", 0) or 0)
        inputs = [
            event for event in inputs
            if int(event.get("serial", 0) or 0) > consumed
        ]
        self._runtime["mission_input_events"] = inputs[-64:]

    def _find_action_hit_pair_for_step(
        self,
        step: Any,
        labels: list[str],
        frame_idx: int,
    ) -> tuple[dict, dict] | None:
        actions = sorted(
            [
                event for event in self._runtime.get("mission_action_events", [])
                if not event.get("consumed")
                and int(event.get("serial", 0) or 0)
                > int(self._runtime.get("mission_action_consumed_serial", 0) or 0)
            ],
            key=lambda event: (
                int(event.get("sample_seq", 0) or 0),
                int(event.get("serial", 0) or 0),
            ),
        )
        hits = sorted(
            [
                hit for hit in self._runtime.get("mission_hit_events", [])
                if not hit.get("consumed")
                and 0 <= int(frame_idx) - int(hit.get("frame", frame_idx)) <= MISSION_HIT_CONFIRM_WINDOW
            ],
            key=lambda hit: (
                int(hit.get("sample_seq", 0) or 0),
                int(hit.get("serial", 0) or 0),
            ),
        )
        for action in actions:
            if not self._action_event_matches_step(action, step, labels):
                continue
            action_serial = int(action.get("serial", 0) or 0)
            action_seq = int(action.get("sample_seq", 0) or 0)
            for hit in hits:
                hit_seq = int(hit.get("sample_seq", 0) or 0)
                if not self._hit_can_confirm_action(hit, action_serial):
                    continue
                if hit_seq and action_seq and hit_seq < action_seq:
                    continue
                return action, hit
        return None

    def _drain_buffered_action_steps(
        self,
        steps: list,
        progress_index: int,
        frame_idx: int,
    ) -> tuple[int, int]:
        progress = max(0, int(progress_index or 0))
        advanced = 0
        while progress < len(steps) and advanced < MISSION_BUFFERED_STEP_LIMIT:
            step = steps[progress]
            labels = self._step_labels(step)
            if not labels:
                break
            if (
                self._step_is_pass(step)
                or self._step_is_baroque_cancel(labels)
                or self._step_is_megacrash(labels)
                or self._step_allows_whiff_confirm(labels, step)
            ):
                break
            pair = self._find_action_hit_pair_for_step(step, labels, frame_idx)
            if pair is None:
                break
            action_event, hit_event = pair
            action_event["consumed"] = True
            hit_event["consumed"] = True
            action_serial = int(action_event.get("serial", 0) or 0)
            input_serial = int(action_event.get("input_serial", 0) or 0)
            self._runtime["mission_action_consumed_serial"] = max(
                int(self._runtime.get("mission_action_consumed_serial", 0) or 0),
                action_serial,
            )
            if input_serial > 0:
                self._consume_mission_input_through(input_serial)
            completed_grace = self._step_grace(step)
            progress += 1
            advanced += 1
            self._runtime.update({
                "progress_index": progress,
                "pending_step_index": None,
                "pending_labels": [],
                "pending_anim": None,
                "pending_started_frame": -9999,
                "pending_input_serial": 0,
                "pending_action_serial": 0,
                "pending_delayed_confirm_until_frame": -9999,
                "pending_label_confirmed": False,
                "pending_arm_source": "",
                "reset_grace_frames": completed_grace,
                "reset_grace_labels": [],
                "reset_grace_step_index": progress if completed_grace > 0 else None,
                "reset_grace_keeps_alive_only": self._step_grace_keeps_alive_only(step),
                "mission_step_start_serial": input_serial,
                "shell_install_hold": 0,
                "mission_last_hit_evidence_frame": int(frame_idx),
            })
        return progress, advanced

    def _buffered_normal_spec(self, step: Any, labels: list[str]) -> tuple[str, str] | None:
        """Return a fast input spec for ordinary ground or air normals.

        These are safe to queue directly because the input uniquely names the
        required attack. Specials and system actions continue through the full
        label-aware checker below.
        """
        candidates: list[str] = []
        notation = self._step_input_notation(step)
        if notation:
            candidates.append(notation)
        candidates.extend(labels or [])

        for raw in candidates:
            text = str(raw or "").strip().upper().replace(" ", "")
            match = re.fullmatch(r"([1-9])([ABC])", text)
            if match:
                return match.group(1), match.group(2)
            air_match = re.fullmatch(r"(?:J\.|AIR|JUMP(?:ING)?)([ABC])", text)
            if air_match:
                return "AIR", air_match.group(1)
        return None

    def _find_buffered_normal_input(
        self,
        step: Any,
        labels: list[str],
        frame_idx: int,
    ) -> dict | None:
        spec = self._buffered_normal_spec(step, labels)
        if spec is None:
            return None
        wanted_direction, wanted_button = spec
        wanted_mask = MISSION_INPUT_BUTTON_MASKS.get(wanted_button, 0)
        if not wanted_mask:
            return None

        events = sorted(
            self._recent_mission_events(frame_idx, MISSION_INPUT_EVENT_WINDOW),
            key=lambda event: int(event.get("serial", 0) or 0),
        )
        for event in events:
            pressed = int(event.get("pressed", 0) or 0)
            if not (pressed & wanted_mask):
                continue
            direction = self._mission_event_direction_digit(event)
            if wanted_direction == "AIR":
                action_id = int(event.get("action_id", 0) or 0) & 0x7FFF
                action_button = MISSION_AIR_NORMAL_ACTIONS.get(action_id)
                if action_button == wanted_button:
                    return event
                if direction not in {"7", "8", "9"}:
                    # Air normals are commonly pressed after the stick returns
                    # to neutral. Accept a recent jump direction instead of
                    # relabeling that input as a grounded normal.
                    serial = int(event.get("serial", 0) or 0)
                    prior_jump = any(
                        int(prior.get("serial", 0) or 0) < serial
                        and serial - int(prior.get("serial", 0) or 0) <= 12
                        and self._mission_event_direction_digit(prior) in {"7", "8", "9"}
                        for prior in events
                    )
                    if not prior_jump:
                        continue
            else:
                mirrored = _MISSION_DIRECTION_MIRROR.get(wanted_direction, wanted_direction)
                if direction not in {wanted_direction, mirrored}:
                    continue
            return event
        return None

    def _find_hit_for_buffered_input(self, input_event: dict, frame_idx: int) -> dict | None:
        input_frame = int((input_event or {}).get("frame", frame_idx))
        candidates = []
        for hit in self._runtime.get("mission_hit_events", []):
            if hit.get("consumed"):
                continue
            hit_frame = int(hit.get("frame", frame_idx))
            age = int(frame_idx) - hit_frame
            if not 0 <= age <= MISSION_HIT_CONFIRM_WINDOW:
                continue
            if hit_frame < input_frame - MISSION_HIT_PRELABEL_WINDOW:
                continue
            if hit_frame > input_frame + MISSION_BUFFERED_NORMAL_HIT_WINDOW:
                continue
            candidates.append(hit)
        if not candidates:
            return None
        return min(candidates, key=lambda hit: (int(hit.get("frame", frame_idx)), int(hit.get("serial", 0))))

    def _drain_buffered_normal_steps(
        self,
        steps: list,
        progress_index: int,
        frame_idx: int,
    ) -> tuple[int, int]:
        """Advance all ready ordinary-normal steps in one update.

        Input and hit events are both persistent queues. Only the exact input
        and hit claimed by a completed step are consumed, so later buffered
        attacks remain available to the following steps.
        """
        progress = max(0, int(progress_index or 0))
        advanced = 0

        while progress < len(steps) and advanced < MISSION_BUFFERED_STEP_LIMIT:
            step = steps[progress]
            labels = self._step_labels(step)
            if not labels:
                break
            if (
                self._step_is_pass(step)
                or self._step_is_baroque_cancel(labels)
                or self._step_is_megacrash(labels)
                or self._step_allows_whiff_confirm(labels, step)
            ):
                break

            input_event = self._find_buffered_normal_input(step, labels, frame_idx)
            if input_event is None:
                break
            hit_event = self._find_hit_for_buffered_input(input_event, frame_idx)
            if hit_event is None:
                break

            hit_event["consumed"] = True
            input_serial = int(input_event.get("serial", 0) or 0)
            self._consume_mission_input_through(input_serial)

            completed_grace = self._step_grace(step)
            progress += 1
            advanced += 1
            self._runtime.update({
                "progress_index": progress,
                "pending_step_index": None,
                "pending_labels": [],
                "pending_anim": None,
                "pending_started_frame": -9999,
                "pending_input_serial": 0,
                "pending_action_serial": 0,
                "pending_delayed_confirm_until_frame": -9999,
                "pending_label_confirmed": False,
                "pending_arm_source": "",
                "reset_grace_frames": completed_grace,
                "reset_grace_labels": [],
                "reset_grace_step_index": progress if completed_grace > 0 else None,
                "reset_grace_keeps_alive_only": self._step_grace_keeps_alive_only(step),
                "mission_step_start_serial": input_serial,
                "shell_install_hold": 0,
                "mission_last_hit_evidence_frame": int(frame_idx),
            })

        self._runtime["mission_buffered_advances"] = advanced
        return progress, advanced

    def _clear_trial_detection_buffers(self) -> None:
        self._runtime.update({
            "mission_hit_events": [],
            "mission_input_events": [],
            "mission_action_events": [],
            "mission_input_serial": 0,
            "mission_attack_command_serial": 0,
            "mission_action_serial": 0,
            "mission_action_consumed_serial": 0,
            "mission_last_action_id": None,
            "mission_last_action_sample_seq": -1,
            "mission_input_consumed_serial": 0,
            "mission_input_match_serial": 0,
            "pending_input_serial": 0,
            "pending_action_serial": 0,
            "pending_delayed_confirm_until_frame": -9999,
            "pending_label_confirmed": False,
            "pending_arm_source": "",
            "mission_step_start_serial": int(self._runtime.get("mission_input_serial", 0) or 0),
            "mission_last_input_token": "",
            "mission_last_input_frame": -9999,
            "mission_last_input_sample_seq": -1,
            "mission_buffered_advances": 0,
            "predicted_progress_index": 0,
            "prediction_entries": [],
        })


    # ------------------------------------------------------------------
    # Input-aware mission safeguards
    # ------------------------------------------------------------------

    def _read_mission_input_packet(self, slot_label: str, snap: dict) -> dict:
        try:
            return input_monitor.read_overlay_input_packet(
                slot_label,
                int((snap or {}).get("base") or 0),
            )
        except Exception:
            return {}

    def _mission_button_letters(self, bits: int) -> str:
        word = int(bits) & 0xFFFF
        labels = []
        if word & MISSION_INPUT_A:
            labels.append("A")
        if word & MISSION_INPUT_B:
            labels.append("B")
        if word & MISSION_INPUT_C:
            labels.append("C")
        if word & MISSION_INPUT_P:
            labels.append("P")
        if (word & MISSION_INPUT_TAUNT) == MISSION_INPUT_TAUNT:
            labels.append("T")
        return "".join(labels)

    def _record_mission_input(
        self,
        packet: dict,
        frame_idx: int,
        sample_seq: int = 0,
    ) -> dict:
        held = int((packet or {}).get("held") or 0) & 0xFFFF
        pressed = int((packet or {}).get("pressed") or 0) & 0xFFFF
        direction = held & MISSION_INPUT_DIRECTION_MASK
        held_buttons = held & MISSION_INPUT_ATTACK_MASK
        previous_direction = self._runtime.get("mission_prev_direction")
        previous_buttons = int(self._runtime.get("mission_prev_buttons", 0) or 0)
        raw_pressed_buttons = pressed & MISSION_INPUT_ATTACK_MASK
        transition_pressed_buttons = held_buttons & ~previous_buttons
        pressed_buttons = raw_pressed_buttons | transition_pressed_buttons
        if pressed_buttons:
            self._runtime["mission_attack_command_serial"] = int(
                self._runtime.get("mission_attack_command_serial", 0) or 0
            ) + 1
        events = self._runtime.setdefault("mission_input_events", [])

        tokens = []
        if direction != previous_direction:
            tokens.append(str({0: 5, 1: 6, 2: 4, 4: 8, 5: 9, 6: 7, 8: 2, 9: 3, 10: 1}.get(direction, 5)))
        if pressed_buttons:
            digit = str({0: 5, 1: 6, 2: 4, 4: 8, 5: 9, 6: 7, 8: 2, 9: 3, 10: 1}.get(direction, 5))
            tokens.append(digit + self._mission_button_letters(pressed_buttons))

        active_baroque_pairs = [
            (label, mask)
            for label, mask in MISSION_INPUT_BAROQUE_PAIRS
            if (held_buttons & mask) == mask
        ]
        previous_baroque_pairs = {
            label
            for label, mask in MISSION_INPUT_BAROQUE_PAIRS
            if (previous_buttons & mask) == mask
        }
        new_baroque_pairs = [
            (label, mask)
            for label, mask in active_baroque_pairs
            if label not in previous_baroque_pairs
        ]
        if new_baroque_pairs:
            self._runtime["mission_baroque_latch"] = MISSION_INPUT_BAROQUE_LATCH
            tokens.extend(label for label, _mask in new_baroque_pairs)
        else:
            self._runtime["mission_baroque_latch"] = max(
                0,
                int(self._runtime.get("mission_baroque_latch", 0) or 0) - 1,
            )

        for token in tokens:
            if (
                token == self._runtime.get("mission_last_input_token")
                and int(sample_seq or 0) > 0
                and int(sample_seq or 0)
                == int(self._runtime.get("mission_last_input_sample_seq", -1) or -1)
            ):
                continue
            if token.isdigit():
                event_pressed = 0
                event_buttons = ""
            elif token in {"AP", "BP", "CP"}:
                pair_mask = dict(MISSION_INPUT_BAROQUE_PAIRS).get(token, 0)
                event_pressed = pair_mask
                event_buttons = token
            else:
                event_pressed = pressed_buttons
                event_buttons = self._mission_button_letters(pressed_buttons)
            self._runtime["mission_input_serial"] = int(
                self._runtime.get("mission_input_serial", 0) or 0
            ) + 1
            events.append({
                "serial": int(self._runtime["mission_input_serial"]),
                "frame": int(frame_idx),
                "token": token,
                "direction": direction,
                "pressed": event_pressed,
                "buttons": event_buttons,
                "held_buttons": held_buttons,
                "sample_seq": int(sample_seq or 0),
                "sample_ns": int((packet or {}).get("sample_ns", 0) or 0),
                "action_id": int((packet or {}).get("action_id", 0) or 0) & 0x7FFF,
                "char_id": int((packet or {}).get("char_id", 0) or 0),
            })
            self._runtime["mission_last_input_token"] = token
            self._runtime["mission_last_input_frame"] = int(frame_idx)
            self._runtime["mission_last_input_sample_seq"] = int(sample_seq or 0)

        events[:] = [
            event for event in events
            if int(frame_idx) - int(event.get("frame", frame_idx)) <= MISSION_INPUT_EVENT_WINDOW
        ]
        self._runtime["mission_prev_direction"] = direction
        self._runtime["mission_prev_buttons"] = held_buttons

        return {
            "held": held,
            "pressed": pressed,
            "direction": direction,
            "pressed_buttons": pressed_buttons,
            "fresh_attack": bool(pressed_buttons),
            "baroque": int(self._runtime.get("mission_baroque_latch", 0) or 0) > 0,
        }

    def _resolved_snapshot_move_labels(self, snap: dict) -> list[str]:
        """Return the same resolved labels the HUD presents to the player.

        Mission matching is label-first. Native action IDs are retained only
        for hit ownership and timing because many specials, cinematic supers,
        projectiles, and scripted attacks use internal IDs that differ from the
        final label shown by the HUD.
        """
        if not isinstance(snap, dict):
            return []

        labels: list[str] = []
        for value in (
            snap.get("final_move_label"),
            snap.get("mv_label_display"),
            snap.get("mv_label"),
            snap.get("profile_resolved_label"),
            snap.get("profile_live_label"),
            snap.get("mv_label_base"),
        ):
            text = str(value or "").strip()
            if text and text not in labels:
                labels.append(text)
        for value in snap.get("mv_label_aliases") or []:
            text = str(value or "").strip()
            if text and text not in labels:
                labels.append(text)
        return labels

    def _snapshot_action_label_candidates(
        self,
        snap: dict,
        action_id: int,
    ) -> list[str]:
        """Return HUD labels for the latest native action sample.

        The caller only supplies ``snap`` for the newest unseen 240 Hz packet
        in a render frame. Therefore the HUD label belongs to that latest
        action even when its internal ID differs from ``mv_id_display``.
        """
        _ = action_id
        return self._resolved_snapshot_move_labels(snap)

    def _action_label_candidates(
        self,
        action_id: int,
        char_id: int = 0,
        snap: dict | None = None,
    ) -> list[str]:
        action = int(action_id or 0) & 0x7FFF
        labels: list[str] = []

        # Player-facing resolved labels are authoritative for mission identity.
        # Native and generic maps are fallback aliases only.
        for label in self._snapshot_action_label_candidates(snap or {}, action):
            if label not in labels:
                labels.append(label)

        try:
            mapped = self._move_label_for(
                action,
                int(char_id or 0),
                self._move_map,
                self._global_map,
            )
        except Exception:
            mapped = None
        if mapped and str(mapped).strip() and str(mapped).strip() not in labels:
            labels.append(str(mapped).strip())

        generic = MISSION_GENERIC_ACTION_LABELS.get(action)
        if generic and generic not in labels:
            labels.append(generic)
        return labels

    def _record_mission_action_sample(
        self,
        packet: dict,
        frame_idx: int,
        sample_seq: int = 0,
        snap: dict | None = None,
    ) -> None:
        action_id = int((packet or {}).get("action_id", 0) or 0) & 0x7FFF
        packet_char_id = int((packet or {}).get("char_id", 0) or 0)
        try:
            resolved_char_id = int((snap or {}).get("csv_char_id", 0) or 0)
        except Exception:
            resolved_char_id = 0
        char_id = resolved_char_id or packet_char_id
        owner_char_id = int(self._mission_owner.get("char_id", 0) or 0)
        if owner_char_id and char_id and char_id != owner_char_id:
            return
        last_action = self._runtime.get("mission_last_action_id")
        last_seq = int(self._runtime.get("mission_last_action_sample_seq", -1) or -1)
        if action_id == int(last_action or 0) and int(sample_seq or 0) == last_seq:
            return
        self._runtime["mission_last_action_id"] = action_id
        self._runtime["mission_last_action_sample_seq"] = int(sample_seq or 0)
        if action_id < 0x100:
            return
        # Keep unlabeled native attack phases too. Cinematic level 3s and
        # scripted attacks often transition through internal action IDs that
        # are absent from the move map. Those phases still extend the original
        # command chain and can own a later hit.
        labels = self._action_label_candidates(action_id, char_id, snap=snap)
        events = self._runtime.setdefault("mission_action_events", [])
        previous = events[-1] if events else None
        if previous and int(previous.get("action_id", 0) or 0) == action_id:
            previous["last_sample_seq"] = int(sample_seq or 0)
            previous["last_sample_ns"] = int((packet or {}).get("sample_ns", 0) or 0)
            previous["last_frame"] = int(frame_idx)
            previous_labels = list(previous.get("labels") or [])
            for label in labels:
                if label not in previous_labels:
                    previous_labels.append(label)
            previous["labels"] = previous_labels
            previous["phase_only"] = not bool(previous_labels)
            return
        serial = int(self._runtime.get("mission_action_serial", 0) or 0) + 1
        self._runtime["mission_action_serial"] = serial
        events.append({
            "serial": serial,
            "frame": int(frame_idx),
            "last_frame": int(frame_idx),
            "sample_seq": int(sample_seq or 0),
            "last_sample_seq": int(sample_seq or 0),
            "sample_ns": int((packet or {}).get("sample_ns", 0) or 0),
            "last_sample_ns": int((packet or {}).get("sample_ns", 0) or 0),
            "action_id": action_id,
            "char_id": char_id,
            "labels": labels,
            "phase_only": not bool(labels),
            "input_serial": int(self._runtime.get("mission_input_serial", 0) or 0),
            "command_serial": int(
                self._runtime.get("mission_attack_command_serial", 0) or 0
            ),
            "consumed": False,
        })
        del events[:-MISSION_ACTION_EVENT_LIMIT]

    def _record_opponent_realtime_stream(
        self,
        slot_label: str,
        snaps_dict: dict,
        frame_idx: int,
    ) -> tuple[list[int], bool, bool | None]:
        my_team = "P1" if str(slot_label).startswith("P1") else "P2"
        hp_cache = self._runtime.setdefault("mission_realtime_hp_by_slot", {})
        hitstun_cache = self._runtime.setdefault("mission_realtime_hitstun_by_slot", {})
        damage_values: list[int] = []
        hitstun_exit = False
        any_sampled = False
        any_hitstun = False

        if callable(self._event_provider):
            # Discover opponents from both the current render snapshots and the
            # realtime state cache. This keeps hit confirmation alive even when
            # the GUI skipped or temporarily omitted an opponent snapshot.
            enemy_slots: set[str] = set()
            for other_slot, other_snap in (snaps_dict or {}).items():
                if not isinstance(other_snap, dict):
                    continue
                other_team = str(other_snap.get("teamtag") or (
                    "P1" if str(other_slot).startswith("P1") else "P2"
                ))
                if other_team and other_team != my_team:
                    enemy_slots.add(str(other_slot))
            for state_key in (
                "hitstun_by_slot", "megacrash_by_slot", "action_by_slot", "hp_by_slot"
            ):
                for other_slot in self._event_state.get(state_key, {}):
                    other_team = (
                        "P1" if str(other_slot).startswith("P1")
                        else "P2" if str(other_slot).startswith("P2")
                        else ""
                    )
                    if other_team and other_team != my_team:
                        enemy_slots.add(str(other_slot))

            for other_slot in sorted(enemy_slots):
                for event in self._events_for_consumer("mission-opponent", other_slot):
                    any_sampled = True
                    if event.kind == EVENT_DAMAGE and int(event.damage) > 0:
                        damage_values.append(int(event.damage))
                        serial = int(self._runtime.get("mission_hit_serial", 0) or 0) + 1
                        self._runtime["mission_hit_serial"] = serial
                        candidates = self._candidate_action_serials_for_hit(
                            frame_idx,
                            sample_ns=int(event.timestamp_ns),
                            sample_seq=int(event.sequence),
                        )
                        self._runtime.setdefault("mission_hit_events", []).append({
                            "serial": serial,
                            "frame": int(frame_idx),
                            "last_frame": int(frame_idx),
                            "sample_seq": int(event.sequence),
                            "sample_ns": int(event.timestamp_ns),
                            "hp_seen": True,
                            "combo_seen": int(self._event_state.get("combo_count", 0) or 0) > 0,
                            "damage": int(event.damage),
                            "owner_action_serial": int(candidates[-1] if candidates else 0),
                            "candidate_action_serials": candidates,
                            "consumed": False,
                        })
                        del self._runtime["mission_hit_events"][:-MISSION_HIT_EVENT_LIMIT]
                        self._runtime["mission_last_hit_evidence_frame"] = int(frame_idx)
                    elif event.kind == EVENT_HITSTUN_END:
                        hitstun_exit = True

            any_hitstun = self._opponent_in_hitstun(slot_label, snaps_dict)
            return damage_values, hitstun_exit, bool(any_hitstun)

        for other_slot, other_snap in (snaps_dict or {}).items():
            if not isinstance(other_snap, dict) or other_snap.get("teamtag") == my_team:
                continue
            packets = self._input_packets_for_slot(
                str(other_slot), other_snap, consumer="mission-opponent"
            )
            for packet in packets:
                seq = int((packet or {}).get("seq", 0) or 0)
                hp = int((packet or {}).get("current_hp", 0) or 0)
                action = int((packet or {}).get("action_id", 0) or 0) & 0x7FFF
                if hp > 0:
                    previous_hp = hp_cache.get(str(other_slot))
                    if isinstance(previous_hp, int) and hp < previous_hp:
                        damage = previous_hp - hp
                        damage_values.append(damage)
                        serial = int(self._runtime.get("mission_hit_serial", 0) or 0) + 1
                        self._runtime["mission_hit_serial"] = serial
                        hit_sample_ns = int((packet or {}).get("sample_ns", 0) or 0)
                        candidate_action_serials = self._candidate_action_serials_for_hit(
                            frame_idx,
                            sample_ns=hit_sample_ns,
                            sample_seq=seq,
                        )
                        owner_action_serial = int(
                            candidate_action_serials[-1] if candidate_action_serials else 0
                        )
                        self._runtime.setdefault("mission_hit_events", []).append({
                            "serial": serial,
                            "frame": int(frame_idx),
                            "last_frame": int(frame_idx),
                            "sample_seq": seq,
                            "sample_ns": hit_sample_ns,
                            "hp_seen": True,
                            "combo_seen": False,
                            "damage": int(damage),
                            "owner_action_serial": owner_action_serial,
                            "candidate_action_serials": candidate_action_serials,
                            "consumed": False,
                        })
                        del self._runtime["mission_hit_events"][:-MISSION_HIT_EVENT_LIMIT]
                        self._runtime["mission_last_hit_evidence_frame"] = int(frame_idx)
                    hp_cache[str(other_slot)] = hp

                in_hitstun = action in MISSION_COMBO_KEEPALIVE_STATES
                previous_hitstun = hitstun_cache.get(str(other_slot))
                if previous_hitstun is True and not in_hitstun:
                    hitstun_exit = True
                hitstun_cache[str(other_slot)] = in_hitstun
                any_sampled = True
                any_hitstun = any_hitstun or in_hitstun

        return damage_values, hitstun_exit, (any_hitstun if any_sampled else None)

    def _record_mission_input_stream(
        self,
        slot_label: str,
        snap: dict,
        frame_idx: int,
    ) -> dict:
        """Consume every unseen 240 Hz input sample before route evaluation."""
        packets = self._input_packets_for_slot(
            slot_label, snap, consumer="mission"
        )
        aggregate = {
            "held": 0,
            "pressed": 0,
            "direction": 0,
            "pressed_buttons": 0,
            "fresh_attack": False,
            "baroque": False,
        }
        packet_count = len(packets)
        for packet_index, packet in enumerate(packets):
            sample_seq = int((packet or {}).get("seq", 0) or 0)
            result = self._record_mission_input(
                packet,
                frame_idx,
                sample_seq=sample_seq,
            )
            self._record_mission_action_sample(
                packet,
                frame_idx,
                sample_seq,
                # A 60 Hz HUD snapshot represents the newest unseen 240 Hz
                # packet, not every older packet drained in this render frame.
                snap=snap if packet_index == packet_count - 1 else None,
            )
            aggregate["held"] = int(result.get("held", 0) or 0)
            aggregate["direction"] = int(result.get("direction", 0) or 0)
            aggregate["pressed"] |= int(result.get("pressed", 0) or 0)
            aggregate["pressed_buttons"] |= int(result.get("pressed_buttons", 0) or 0)
            aggregate["fresh_attack"] = bool(
                aggregate["fresh_attack"] or result.get("fresh_attack", False)
            )
            aggregate["baroque"] = bool(
                aggregate["baroque"] or result.get("baroque", False)
            )
        return aggregate

    def _step_input_notation(self, step: Any) -> str:
        if not isinstance(step, dict):
            return ""
        return str(
            step.get("input")
            or step.get("command")
            or step.get("notation")
            or ""
        ).strip()

    def _recent_mission_events(self, frame_idx: int, window: int) -> list[dict]:
        consumed_serial = max(
            int(self._runtime.get("mission_input_consumed_serial", 0) or 0),
            int(self._runtime.get("mission_step_start_serial", 0) or 0),
        )
        return [
            event for event in self._runtime.get("mission_input_events", [])
            if int(event.get("serial", 0) or 0) > consumed_serial
            and int(frame_idx) - int(event.get("frame", frame_idx)) <= int(window)
        ]

    def _mark_mission_input_match(self, event_or_serial) -> None:
        if isinstance(event_or_serial, dict):
            serial = int(event_or_serial.get("serial", 0) or 0)
        else:
            serial = int(event_or_serial or 0)
        if serial > 0:
            self._runtime["mission_input_match_serial"] = serial

    def _consume_mission_input_through(self, serial: int) -> None:
        serial = int(serial or 0)
        if serial <= 0:
            return
        current = int(
            self._runtime.get("mission_input_consumed_serial", 0) or 0
        )
        consumed = max(current, serial)
        self._runtime["mission_input_consumed_serial"] = consumed
        self._runtime["mission_input_match_serial"] = 0
        self._runtime["pending_input_serial"] = 0
        self._runtime["mission_input_events"] = [
            event
            for event in self._runtime.get("mission_input_events", [])
            if int(event.get("serial", 0) or 0) > consumed
        ]
        # Label-based and special-case confirmations also retire the native
        # action that owned the consumed input. Otherwise that stale action can
        # sit in front of the prediction queue and block every later step.
        for action in self._runtime.get("mission_action_events", []):
            action_input = int(action.get("input_serial", 0) or 0)
            if action_input > 0 and action_input <= consumed:
                action["consumed"] = True
                self._runtime["mission_action_consumed_serial"] = max(
                    int(self._runtime.get("mission_action_consumed_serial", 0) or 0),
                    int(action.get("serial", 0) or 0),
                )

    def _normal_input_matches(self, notation: str, frame_idx: int) -> bool:
        compact = str(notation or "").replace(" ", "").upper()
        match = __import__("re").fullmatch(r"([1-9])([ABC])", compact)
        if not match:
            return False
        wanted_direction, wanted_button = match.groups()
        mirrored = _MISSION_DIRECTION_MIRROR.get(wanted_direction, wanted_direction)
        for event in reversed(self._recent_mission_events(frame_idx, MISSION_INPUT_NORMAL_LATCH)):
            token = str(event.get("token") or "").upper()
            if token in {wanted_direction + wanted_button, mirrored + wanted_button}:
                self._mark_mission_input_match(event)
                return True
        return False

    def _mission_event_direction_digit(self, event: dict) -> str:
        token = str((event or {}).get("token") or "").strip().upper()
        if token and token[0] in "123456789":
            return token[0]
        direction = int((event or {}).get("direction") or 0) & MISSION_INPUT_DIRECTION_MASK
        return str({0: 5, 1: 6, 2: 4, 4: 8, 5: 9, 6: 7, 8: 2, 9: 3, 10: 1}.get(direction, 5))

    def _neutral_button_input_matches(self, button: str, frame_idx: int) -> bool:
        wanted = str(button or "").upper()
        wanted_mask = MISSION_INPUT_BUTTON_MASKS.get(wanted, 0)
        if not wanted_mask:
            return False
        for event in reversed(self._recent_mission_events(frame_idx, MISSION_INPUT_NORMAL_LATCH)):
            if self._mission_event_direction_digit(event) != "5":
                continue
            pressed = int(event.get("pressed") or 0)
            held = int(event.get("held_buttons") or pressed)
            if pressed & wanted_mask and (held & wanted_mask) == wanted_mask:
                self._mark_mission_input_match(event)
                return True
        return False

    def _button_chord_input_matches(
        self,
        buttons: str,
        frame_idx: int,
        *,
        neutral_only: bool = False,
    ) -> bool:
        required = 0
        for button in str(buttons or "").upper():
            required |= MISSION_INPUT_BUTTON_MASKS.get(button, 0)
        if not required:
            return False
        for event in reversed(self._recent_mission_events(frame_idx, MISSION_INPUT_NORMAL_LATCH)):
            if neutral_only and self._mission_event_direction_digit(event) != "5":
                continue
            pressed = int(event.get("pressed") or 0)
            held = int(event.get("held_buttons") or pressed)
            if pressed & required and (held & required) == required:
                self._mark_mission_input_match(event)
                return True
        return False

    def _double_attack_input_matches(self, frame_idx: int, *, neutral_only: bool = False) -> bool:
        for event in reversed(self._recent_mission_events(frame_idx, MISSION_INPUT_NORMAL_LATCH)):
            if neutral_only and self._mission_event_direction_digit(event) != "5":
                continue
            pressed = int(event.get("pressed") or 0) & MISSION_INPUT_ABC_MASK
            held = int(event.get("held_buttons") or pressed) & MISSION_INPUT_ABC_MASK
            if pressed and int(held).bit_count() >= 2:
                self._mark_mission_input_match(event)
                return True
        return False

    def _rotation_input_matches(
        self,
        frame_idx: int,
        *,
        button: str = "",
        double_attack: bool = False,
        any_attack: bool = False,
        motion_only: bool = False,
    ) -> bool:
        events = self._recent_mission_events(frame_idx, MISSION_INPUT_COMMAND_WINDOW)
        if not events:
            return False
        wanted_mask = MISSION_INPUT_BUTTON_MASKS.get(str(button or "").upper(), 0)
        for final_index in range(len(events) - 1, -1, -1):
            final_event = events[final_index]
            pressed = int(final_event.get("pressed") or 0)
            held = int(final_event.get("held_buttons") or pressed)
            abc_pressed = pressed & MISSION_INPUT_ABC_MASK
            abc_held = held & MISSION_INPUT_ABC_MASK
            if motion_only:
                pass
            elif double_attack:
                if not abc_pressed or int(abc_held).bit_count() < 2:
                    continue
            elif wanted_mask:
                if not (pressed & wanted_mask):
                    continue
            elif any_attack:
                if not abc_pressed:
                    continue
            else:
                continue

            directions: list[str] = []
            for event in events[: final_index + 1]:
                digit = self._mission_event_direction_digit(event)
                if digit == "5":
                    continue
                cardinal = {
                    "1": "2", "2": "2", "3": "6", "6": "6",
                    "9": "8", "8": "8", "7": "4", "4": "4",
                }.get(digit)
                if cardinal and (not directions or directions[-1] != cardinal):
                    directions.append(cardinal)
            tail = directions[-8:]
            clockwise = ["6", "2", "4", "8"]
            counter = ["6", "8", "4", "2"]
            doubled = tail + tail
            if all(cardinal in tail for cardinal in clockwise):
                for sequence in (clockwise, counter):
                    for start in range(max(1, len(doubled) - len(sequence) + 1)):
                        if doubled[start:start + len(sequence)] == sequence:
                            self._mark_mission_input_match(final_event)
                            return True
        return False

    def _motion_input_matches(
        self,
        motion: str,
        frame_idx: int,
        *,
        button: str = "",
        double_attack: bool = False,
        any_attack: bool = False,
        required_attack_mask: int = 0,
        motion_only: bool = False,
    ) -> bool:
        motion = "".join(ch for ch in str(motion or "") if ch in "123456789")
        if not motion:
            return False

        mirrored_motion = "".join(_MISSION_DIRECTION_MIRROR.get(ch, ch) for ch in motion)
        wanted_button_mask = MISSION_INPUT_BUTTON_MASKS.get(
            str(button or "").upper(),
            0,
        )

        events = self._recent_mission_events(frame_idx, MISSION_INPUT_COMMAND_WINDOW)
        for final_index in range(len(events) - 1, -1, -1):
            final_event = events[final_index]
            pressed = int(final_event.get("pressed") or 0)
            held_buttons = int(final_event.get("held_buttons") or pressed)
            abc_pressed = pressed & MISSION_INPUT_ABC_MASK
            abc_held = held_buttons & MISSION_INPUT_ABC_MASK

            if motion_only:
                pass
            elif required_attack_mask:
                if not abc_pressed or (abc_held & required_attack_mask) != required_attack_mask:
                    continue
            elif double_attack:
                if not abc_pressed or int(abc_held).bit_count() < 2:
                    continue
            elif wanted_button_mask:
                if not (abc_pressed & wanted_button_mask):
                    continue
            elif any_attack:
                if not abc_pressed:
                    continue
            else:
                continue

            directions: list[str] = []
            for event in events[: final_index + 1]:
                digit = self._mission_event_direction_digit(event)
                if digit == "5":
                    continue
                if not directions or directions[-1] != digit:
                    directions.append(digit)

            for wanted in {motion, mirrored_motion}:
                wanted_digits = list(wanted)
                if len(directions) >= len(wanted_digits) and directions[-len(wanted_digits):] == wanted_digits:
                    self._mark_mission_input_match(final_event)
                    return True

        return False

    def _mash_input_matches(self, button: str, frame_idx: int) -> bool:
        wanted = str(button or "X").upper()
        wanted_mask = {
            "A": MISSION_INPUT_A,
            "B": MISSION_INPUT_B,
            "C": MISSION_INPUT_C,
        }.get(wanted, MISSION_INPUT_ABC_MASK)

        count = 0
        for event in self._recent_mission_events(frame_idx, MISSION_INPUT_LEGS_WINDOW):
            pressed = int(event.get("pressed") or 0) & MISSION_INPUT_ABC_MASK
            if pressed & wanted_mask:
                count += 1
                if count >= MISSION_INPUT_MASH_COUNT:
                    self._mark_mission_input_match(event)
                    return True
        return False

    def _button_sequence_input_matches(self, sequence: str, frame_idx: int) -> bool:
        wanted = [letter for letter in str(sequence or "").upper() if letter in "ABC"]
        if not wanted:
            return False

        observed: list[tuple[str, int]] = []
        for event in self._recent_mission_events(frame_idx, MISSION_INPUT_COMMAND_WINDOW):
            pressed = int(event.get("pressed") or 0) & MISSION_INPUT_ABC_MASK
            serial = int(event.get("serial", 0) or 0)
            for letter, mask in (
                ("A", MISSION_INPUT_A),
                ("B", MISSION_INPUT_B),
                ("C", MISSION_INPUT_C),
            ):
                if pressed & mask:
                    observed.append((letter, serial))

        if len(observed) < len(wanted):
            return False
        tail = observed[-len(wanted):]
        if [letter for letter, _serial in tail] != wanted:
            return False
        self._mark_mission_input_match(tail[-1][1])
        return True

    def _command_input_matches(self, notation: str, frame_idx: int) -> bool:
        raw = str(notation or "").strip().upper()
        if not raw:
            return False

        alternatives = re.split(r"\s+/\s+", raw)
        if len(alternatives) > 1:
            return any(
                self._command_input_matches(alternative, frame_idx)
                for alternative in alternatives
                if alternative.strip()
            )

        if raw in {"TAUNT", "TAUNT(T)", "T"}:
            return self._button_chord_input_matches("T", frame_idx)

        compact = raw.replace("AIR", "").replace("J.", "")
        compact = compact.replace("[", "").replace("]", "")
        compact = compact.replace("CHARGE", "").replace("HOLD", "")
        compact = compact.replace(" ", "")
        compact = compact.replace("HCB", "63214").replace("HCF", "41236")
        compact = compact.replace("QCF", "236").replace("QCB", "214")
        compact = compact.replace("RDP", "421").replace("DP", "623")

        if "RELEASE" in compact:
            return False

        if compact == "ABCP":
            return self._button_chord_input_matches("ABCP", frame_idx)
        if compact == "XX":
            return self._double_attack_input_matches(frame_idx, neutral_only=True)
        if compact == "X":
            return any(
                self._neutral_button_input_matches(button, frame_idx)
                for button in "ABC"
            )
        if re.fullmatch(r"[ABCLMHP]", compact):
            return self._neutral_button_input_matches(compact, frame_idx)

        rotation_match = re.fullmatch(r"360(?:(XX)|([ABCLMH])|(X))?", compact)
        if rotation_match:
            double_token, button_token, any_token = rotation_match.groups()
            return self._rotation_input_matches(
                frame_idx,
                button=button_token or "",
                double_attack=bool(double_token),
                any_attack=bool(any_token),
                motion_only=not any((double_token, button_token, any_token)),
            )

        mash_match = re.fullmatch(r"MASH([ABCLMHX])", compact)
        if mash_match:
            button = mash_match.group(1)
            button = {"L": "A", "M": "B", "H": "C"}.get(button, button)
            return self._mash_input_matches(button, frame_idx)

        sequence_match = re.fullmatch(r"[ABC]{3,}", compact)
        if sequence_match:
            return self._button_sequence_input_matches(compact, frame_idx)

        pair_match = re.fullmatch(r"([1-9]+)([ABCLMH])\+([ABCLMH])", compact)
        if pair_match:
            motion, first_button, second_button = pair_match.groups()
            required_mask = (
                MISSION_INPUT_BUTTON_MASKS[first_button]
                | MISSION_INPUT_BUTTON_MASKS[second_button]
            )
            return self._motion_input_matches(
                motion,
                frame_idx,
                required_attack_mask=required_mask,
            )

        generic_list_match = re.fullmatch(r"([1-9]+)([ABCLMH](?:/[ABCLMH])+)", compact)
        if generic_list_match:
            motion, buttons = generic_list_match.groups()
            return any(
                self._motion_input_matches(motion, frame_idx, button=button)
                for button in buttons.split("/")
            )

        exact_match = re.fullmatch(r"([1-9]+)([ABCLMH])", compact)
        if exact_match:
            motion, button = exact_match.groups()
            return self._motion_input_matches(motion, frame_idx, button=button)

        double_match = re.fullmatch(r"([1-9]+)XX", compact)
        if double_match:
            return self._motion_input_matches(
                double_match.group(1),
                frame_idx,
                double_attack=True,
            )

        generic_match = re.fullmatch(r"([1-9]+)(?:A/B/C|L/M/H|X)", compact)
        if generic_match:
            return self._motion_input_matches(
                generic_match.group(1),
                frame_idx,
                any_attack=True,
            )

        motion_only_match = re.fullmatch(r"([1-9]+)", compact)
        if motion_only_match:
            return self._motion_input_matches(
                motion_only_match.group(1),
                frame_idx,
                motion_only=True,
            )

        return False

    def _chun_legs_input_matches(self, expected_labels: list[str], frame_idx: int) -> bool:
        labels = [str(label or "").strip() for label in expected_labels]
        labels_norm = [label.lower() for label in labels]
        if not any("legs" in label for label in labels_norm):
            return False
        wanted_button = ""
        for label in reversed(labels):
            match = __import__("re").search(r"(?:^|\s)([ABC])$", label, flags=__import__("re").IGNORECASE)
            if match:
                wanted_button = match.group(1).upper()
                break
        counts = {"A": 0, "B": 0, "C": 0}
        for event in self._recent_mission_events(frame_idx, MISSION_INPUT_LEGS_WINDOW):
            pressed = int(event.get("pressed") or 0) & MISSION_INPUT_ABC_MASK
            serial = int(event.get("serial", 0) or 0)
            if pressed & MISSION_INPUT_A:
                counts["A"] += 1
                if (wanted_button in {"", "A"}) and counts["A"] >= MISSION_INPUT_MASH_COUNT:
                    self._mark_mission_input_match(serial)
                    return True
            if pressed & MISSION_INPUT_B:
                counts["B"] += 1
                if (wanted_button in {"", "B"}) and counts["B"] >= MISSION_INPUT_MASH_COUNT:
                    self._mark_mission_input_match(serial)
                    return True
            if pressed & MISSION_INPUT_C:
                counts["C"] += 1
                if (wanted_button in {"", "C"}) and counts["C"] >= MISSION_INPUT_MASH_COUNT:
                    self._mark_mission_input_match(serial)
                    return True
        return False

    def _step_is_megacrash(self, expected_labels: list[str]) -> bool:
        return any(
            self._canonical_mission_label(label) == "megacrash"
            for label in expected_labels
        )

    def _megacrash_step_matches(
        self,
        expected_labels: list[str],
        opponent_in_megacrash: bool,
        previous_opponent_megacrash: bool,
    ) -> bool:
        """Use the opponent's dedicated Megacrash state edge for this step."""
        return bool(
            self._step_is_megacrash(expected_labels)
            and opponent_in_megacrash
            and not previous_opponent_megacrash
        )

    def _step_input_matches(
        self,
        character_name: str,
        expected_step: Any,
        expected_labels: list[str],
        frame_idx: int,
    ) -> bool:
        self._runtime["mission_input_match_serial"] = 0
        labels_norm = {str(label or "").strip().lower() for label in expected_labels}
        notation = self._step_input_notation(expected_step)
        compact_notation = notation.replace(" ", "").upper()
        if compact_notation in {"ATK+P", "ATKP"}:
            for event in reversed(
                self._recent_mission_events(
                    frame_idx,
                    MISSION_INPUT_BAROQUE_LATCH,
                )
            ):
                held_buttons = int(event.get("held_buttons") or 0)
                pressed_buttons = int(event.get("pressed") or 0)
                has_attack = bool(held_buttons & MISSION_INPUT_ABC_MASK)
                has_partner = bool(held_buttons & MISSION_INPUT_P)
                fresh_pair_edge = bool(
                    pressed_buttons
                    & (MISSION_INPUT_ABC_MASK | MISSION_INPUT_P)
                )
                if has_attack and has_partner and fresh_pair_edge:
                    self._mark_mission_input_match(event)
                    return True
            return False

        if (
            "baroque cancel" in labels_norm
            or compact_notation in {"AP", "A+P", "BP", "B+P", "CP", "C+P", "A+P/B+P/C+P"}
        ):
            for event in reversed(
                self._recent_mission_events(frame_idx, MISSION_INPUT_BAROQUE_LATCH)
            ):
                if str(event.get("token") or "").upper() in {"AP", "BP", "CP"}:
                    self._mark_mission_input_match(event)
                    return True
            return False
        if self._command_input_matches(notation, frame_idx):
            return True
        if self._normal_input_matches(notation, frame_idx):
            return True
        for label in expected_labels:
            if self._command_input_matches(label, frame_idx):
                return True
            if self._normal_input_matches(label, frame_idx):
                return True
        if str(character_name or "").strip().lower().replace("-", " ") in {"chun li", "chunli"}:
            return self._chun_legs_input_matches(expected_labels, frame_idx)
        return False

    # ------------------------------------------------------------------
    # Step predicate helpers
    # ------------------------------------------------------------------

    def _canonical_mission_label(self, label: str) -> str:
        value = str(label or "").strip().lower()
        value = value.replace("j.", "air ")
        value = value.replace("jumping ", "air ")
        value = value.replace("jump ", "air ")
        value = __import__("re").sub(r"[^a-z0-9]+", " ", value)
        return " ".join(value.split())

    def _mission_label_signature(self, label: str) -> tuple[str, str, str]:
        """Return ``(full, family, strength)`` for mission move matching.

        The live resolver may expose phases such as ``Start/Whiff`` and
        strength suffixes such as ``A``, ``B``, or ``C``.  Mission JSON can ask
        for an exact strength (``Slam B``) or a generic family
        (``Powerbomb``), which intentionally accepts every strength.
        """
        import re

        full = self._canonical_mission_label(label)
        if not full:
            return "", "", ""

        phase_suffixes = (
            " start whiff",
            " startup whiff",
            " start or whiff",
            " start",
            " whiff",
        )

        def strip_phase(value: str) -> str:
            result = value
            changed = True
            while changed:
                changed = False
                for suffix in phase_suffixes:
                    if result.endswith(suffix):
                        result = result[: -len(suffix)].strip()
                        changed = True
                        break
            return result

        family = strip_phase(full)
        strength = ""
        strength_match = re.match(r"^(.*\S)\s+([abc])$", family)
        if strength_match:
            possible_family = strip_phase(strength_match.group(1).strip())
            # ``air B`` is a normal, not the B-strength form of a move named
            # ``air``. Numeric notation such as 5B is already one token.
            if possible_family and possible_family != "air":
                family = possible_family
                strength = strength_match.group(2)

        return full, family, strength

    def _mission_label_family_equal(self, left: str, right: str) -> bool:
        # Human-authored missions commonly use both "Power Bomb" and
        # "Powerbomb". Punctuation and whitespace are not move identity.
        return bool(left and right and left.replace(" ", "") == right.replace(" ", ""))

    def _mission_label_matches(self, current_label: str, expected_labels: list[str]) -> bool:
        current, current_family, current_strength = self._mission_label_signature(
            current_label
        )
        if not current:
            return False
        for expected in expected_labels or []:
            wanted, wanted_family, wanted_strength = self._mission_label_signature(
                expected
            )
            if not wanted:
                continue
            if current == wanted:
                return True
            if current.replace(" ", "") == wanted.replace(" ", ""):
                return True
            if {current, wanted} <= {"air a", "ja"}:
                return True
            if {current, wanted} <= {"air b", "jb"}:
                return True
            if {current, wanted} <= {"air c", "jc"}:
                return True
            if not self._mission_label_family_equal(current_family, wanted_family):
                continue
            # An explicitly named strength remains strict. A generic family in
            # mission JSON intentionally accepts A, B, C, or an unsuffixed live
            # label from a shared hit/cinematic phase.
            if wanted_strength:
                if current_strength == wanted_strength:
                    return True
                continue
            return True
        return False

    def _mission_snapshot_label_matches(self, snap: dict, expected_labels: list[str]) -> bool:
        """Match the base move label plus projectile-proven level aliases.

        Charged moves often share one action ID.  The projectile level detector
        adds aliases such as ``Hyper Zero Blaster Lv2`` only after the exact
        projectile variant spawns, so missions can distinguish levels without
        breaking older steps that still expect the unqualified move name.
        """
        candidates = self._resolved_snapshot_move_labels(snap or {})
        seen = set()
        for candidate in candidates:
            canon = self._canonical_mission_label(candidate)
            if not candidate or canon in seen:
                continue
            seen.add(canon)
            if self._mission_label_matches(candidate, expected_labels):
                return True

        try:
            detected_level = int((snap or {}).get("move_level") or 0)
        except Exception:
            detected_level = 0
        if detected_level <= 0:
            return False

        import re
        level_re = re.compile(r"(?:level|lvl|lv|l)\s*[-_:]?\s*([1-9][0-9]*)", re.I)
        strip_re = re.compile(r"\b(?:level|lvl|lv|l)\s*[-_:]?\s*[1-9][0-9]*\b", re.I)
        candidate_bases = []
        for candidate in candidates:
            stripped = strip_re.sub("", candidate)
            canon = self._canonical_mission_label(stripped)
            if canon:
                candidate_bases.append(canon)
        for expected in expected_labels or []:
            match = level_re.search(str(expected or ""))
            if not match or int(match.group(1)) != detected_level:
                continue
            expected_base = self._canonical_mission_label(strip_re.sub("", str(expected or "")))
            if not expected_base:
                return True
            for candidate_base in candidate_bases:
                if candidate_base == expected_base:
                    return True
                # Permit a concise mission label such as "Hyper Blaster Lv2"
                # to match the game's longer "Hyper Zero Blaster" label.
                expected_tokens = set(expected_base.split())
                candidate_tokens = set(candidate_base.split())
                if expected_tokens and expected_tokens <= candidate_tokens:
                    return True
        return False

    def _label_is_ignorable(self, label: str) -> bool:
        return (label or "").strip().lower() in MISSION_IGNORE_LABELS

    def _is_direction_input_key(self, key: str) -> bool:
        return any(t in (key or "").strip().lower() for t in (
            "up", "down", "left", "right", "dir", "stick", "analog", "xaxis", "yaxis"
        ))

    def _has_fresh_attack_input(self, current: dict, last: dict) -> bool:
        if not isinstance(current, dict) or not current:
            return False
        if not isinstance(last, dict):
            last = {}
        for key, cur_val in current.items():
            if self._is_direction_input_key(key):
                continue
            if int(cur_val or 0) != 0 and int(last.get(key, 0) or 0) == 0:
                return True
        return False

    def _step_has_non_damage_confirm(
        self, expected_labels: list[str], snap: dict, current_label: str
    ) -> bool:
        labels_norm = {str(x).strip().lower() for x in (expected_labels or []) if str(x).strip()}
        if not labels_norm:
            return False
        current_norm = (current_label or "").strip().lower()
        if "baroque cancel" in labels_norm:
            if current_norm == "baroque cancel":
                return True
            if snap.get("baroque_cancel_latched") or snap.get("baroque_cancel_raw"):
                return True
        if labels_norm & MISSION_NON_DAMAGE_CONFIRM_LABELS:
            if current_norm in labels_norm:
                return True
        return False

    def _step_allows_whiff_confirm(self, expected_labels: list[str], step: Any = None) -> bool:
        labels_norm = {str(x).strip().lower() for x in (expected_labels or []) if str(x).strip()}
        matched = labels_norm & MISSION_WHIFF_CONFIRM_LABELS
        explicit = isinstance(step, dict) and bool(
            step.get("whiff", False)
            or step.get("whiff_confirm", False)
            or step.get("allow_whiff", False)
        )
        if matched or explicit:
            print(
                f"[mission whiff confirm] labels={sorted(matched)!r} "
                f"explicit={explicit}"
            )
        return explicit or bool(matched)

    def _step_allows_zero_damage_confirm(
        self, character_name: str, expected_labels: list[str], current_label: str
    ) -> bool:
        if character_name != "Saki":
            return False
        return (current_label or "").strip().lower() == "load super armor piercing shell"

    def _saki_shell_release_label(self, label: str) -> bool:
        return (label or "").strip().lower() in {"5c", "j.c", "j.b"}

    def _is_saki_shell_label(self, label: str) -> bool:
        return (label or "").strip().lower() == "load super armor piercing shell"

    def _step_needs_reset_grace(self, character_name: str, expected_labels: list[str]) -> bool:
        if character_name != "Saki":
            return False
        labels_norm = {str(x).strip().lower() for x in (expected_labels or []) if str(x).strip()}
        return "j.c" in labels_norm

    def _reset_grace_accepts_label(self, character_name: str, label: str) -> bool:
        if character_name != "Saki":
            return False
        return (label or "").strip().lower() == "j.c"

    def _step_is_generic_partner_var(self, mission_id: str, expected_labels: list[str]) -> bool:
        if mission_id in MISSION_SELF_VAR_MISSIONS:
            return False
        labels_norm = {str(x).strip().lower() for x in (expected_labels or []) if str(x).strip()}
        return bool(labels_norm & MISSION_GENERIC_VAR_LABELS)

    def _partner_matches_generic_var(self, slot_label: str, snaps_dict: dict) -> bool:
        partner_slot = self._partner_slot(slot_label)
        if not partner_slot:
            return False
        me = snaps_dict.get(slot_label) or {}
        partner = snaps_dict.get(partner_slot) or {}
        if not me or not partner:
            return False
        my_base = me.get("base")
        partner_base = partner.get("base")
        if isinstance(my_base, int) and isinstance(partner_base, int) and my_base == partner_base:
            return False

        partner_label = (partner.get("mv_label") or "").strip().lower()
        partner_anim = partner.get("attA") or partner.get("attB")
        partner_csv = partner.get("csv_char_id")

        if partner_label in MISSION_GENERIC_VAR_LABELS:
            print(f"[mission generic var direct] slot={slot_label} partner={partner_slot} label={partner_label!r}")
            return True

        if partner_anim is not None:
            mapped = self._move_label_for(partner_anim, partner_csv, self._move_map, self._global_map)
            if (mapped or "").strip().lower() in MISSION_GENERIC_VAR_LABELS:
                print(f"[mission generic var mapped] slot={slot_label} partner={partner_slot} mapped={mapped!r}")
                return True

        print(f"[mission generic var miss] slot={slot_label} partner={partner_slot} label={partner_label!r}")
        return False

    def _doronjo_damage_pass(
        self, character_name: str, expected_labels: list[str], damage_values: list[int]
    ) -> bool:
        if character_name != "Doronjo":
            return False
        labels_norm = {str(x).strip().lower() for x in (expected_labels or []) if str(x).strip()}
        if not labels_norm or not damage_values:
            return False
        for label in labels_norm:
            allowed = DORONJO_DAMAGE_PASS.get(label)
            if allowed and any(d in allowed for d in damage_values):
                return True
        return False
    def _doronjo_damage_pass(
        self, character_name: str, expected_labels: list[str], damage_values: list[int]
    ) -> bool:
        if character_name != "Doronjo":
            return False
        labels_norm = {str(x).strip().lower() for x in (expected_labels or []) if str(x).strip()}
        if not labels_norm or not damage_values:
            return False
        for label in labels_norm:
            allowed = DORONJO_DAMAGE_PASS.get(label)
            if allowed and any(d in allowed for d in damage_values):
                return True
        return False

    def _can_repeat_same_step_on_damage(
        self,
        steps: list,
        progress_index: int,
        pending_labels: list[str],
        current_label: str,
        current_anim,
        damage_values: list[int],
    ) -> bool:
        if not damage_values:
            return False

        if progress_index <= 0 or progress_index >= len(steps):
            return False

        current_labels = self._step_labels(steps[progress_index])
        previous_labels = self._step_labels(steps[progress_index - 1])

        if not current_labels or not previous_labels:
            return False

        current_norm = {str(x).strip().lower() for x in current_labels}
        previous_norm = {str(x).strip().lower() for x in previous_labels}
        pending_norm = {str(x).strip().lower() for x in pending_labels}

        if not current_norm or current_norm != previous_norm:
            return False

        if pending_norm and pending_norm != current_norm:
            return False

        if (current_label or "").strip().lower() not in current_norm:
            return False

        last_anim = self._runtime.get("last_seen_anim")
        if current_anim != last_anim:
            return False


        return True

    def _step_is_baroque_cancel(self, expected_labels: list[str]) -> bool:
        labels_norm = {
            str(x).strip().lower()
            for x in (expected_labels or [])
            if str(x).strip()
        }
        return "baroque cancel" in labels_norm

    # ------------------------------------------------------------------
    # Core augment (was _augment_payload_with_runtime)
    # ------------------------------------------------------------------
    def _step_labels(self, step) -> list[str]:
        if isinstance(step, dict):
            raw = step.get("labels")
            if isinstance(raw, list):
                labels = [str(x).strip() for x in raw if str(x).strip()]
                if labels:
                    return labels
            label = str(step.get("label", "")).strip()
            return [label] if label else []

        if isinstance(step, list):
            return [str(x).strip() for x in step if str(x).strip()]

        text = str(step).strip()
        return [text] if text else []

    def _step_grace(self, step) -> int:
        if not isinstance(step, dict):
            return 0
        try:
            return max(0, int(step.get("grace", 0) or 0))
        except Exception:
            return 0

    def _step_delayed_confirm_frames(self, step) -> int:
        """Return a generic extended hit-confirm window for delayed attacks.

        This supports traps, summons, stored attacks, cinematic phases, and
        trigger glitches without hardcoding character or move names.
        """
        if not isinstance(step, dict):
            return 0
        try:
            return max(0, int(step.get("delayed_confirm_frames", 0) or 0))
        except Exception:
            return 0

    def _step_is_pass(self, step) -> bool:
        return isinstance(step, dict) and bool(step.get("pass", False))

    def _step_is_jump_cancel(self, step) -> bool:
        labels = self._step_labels(step)
        extra = []
        if isinstance(step, dict):
            extra = [
                str(step.get("display") or ""),
                str(step.get("input") or step.get("command") or step.get("notation") or ""),
            ]
        step_text = " ".join([str(value or "") for value in labels] + extra).strip().lower()
        return "jump cancel" in step_text or "7 / 8 / 9" in step_text

    def _consume_jump_cancel_free_passes(self, steps: list, progress_index: int) -> tuple[int, int]:
        """Skip mission Jump Cancel rows that have no reliable runtime label.

        Jump cancels are route notation, not a dependable live action label in
        every character path. Treating them as free passes keeps the mission
        focused on the attacks before and after the jump instead of stalling.
        """
        progress = max(0, int(progress_index or 0))
        advanced = 0
        while progress < len(steps) and self._step_is_jump_cancel(steps[progress]):
            step = steps[progress]
            completed_grace = self._step_grace(step)
            print(f"[mission jump cancel pass] step={progress}")
            progress += 1
            advanced += 1
            self._runtime.update({
                "progress_index": progress,
                "pending_step_index": None,
                "pending_labels": [],
                "pending_anim": None,
                "pending_frame": None,
                "reset_grace_frames": completed_grace,
                "reset_grace_labels": [],
                "reset_grace_step_index": progress if completed_grace > 0 else None,
                "reset_grace_keeps_alive_only": self._step_grace_keeps_alive_only(step),
                "last_seen_label": "",
                "last_seen_anim": None,
                "last_seen_hitstun": False,
                "last_inputs": {},
            })
        return progress, advanced

    def _step_grace_keeps_alive_only(self, step) -> bool:
        return isinstance(step, dict) and bool(step.get("grace_keeps_alive_only", False))
    def _augment_payload_with_runtime(self, payload: dict, snaps_dict: dict) -> dict:
        """Evaluate Mission Mode from the immutable realtime event stream.

        Route progression, hit confirmation, restart boundaries, jump cancels,
        Baroque/assist input steps, and the three supported goal mission types
        are owned by MissionRuntimeV2.  Snapshot evaluation is retained only as
        a compatibility fallback when no realtime event provider is attached.
        """
        if not callable(self._event_provider):
            return self._augment_payload_with_runtime_legacy(payload, snaps_dict)

        payload = dict(payload or {})
        slot = str(payload.get("slot") or "")
        route_slot = str(
            payload.get("point_slot")
            or self._mission_owner_slot(snaps_dict)
            or slot
            or ""
        )
        mission_id = str(payload.get("active_mission_id") or "")
        character_name = str(payload.get("character") or "")
        steps = list(payload.get("active_mission_steps") or [])
        mission_goal = dict(payload.get("active_mission_goal") or {})

        # Setup helpers remain manager-owned because they intentionally write
        # debug flags. Runtime V2 is read-only with respect to game state.
        self._sync_meter_refill_mission(payload)

        if not payload.get("active") or not slot or not mission_id or (not steps and not mission_goal):
            self._mission_runtime_v2.reset_all(
                int(self._event_state.get("latest_sequence", 0) or 0)
            )
            payload.update({
                "completed_step_count": 0,
                "confirmed_step_count": 0,
                "predicted_step_count": 0,
                "prediction_active": False,
                "current_step_index": 0,
                "current_step_label": None,
                "just_cleared": False,
                "celebrate_pending": False,
                "celebrate_token": 0,
                "mission_runtime_version": 2,
            })
            return payload

        newest_stream_sequence = int(self._event_state.get("latest_sequence", 0) or 0)
        mission_arm_floor = int(
            self._runtime.get("mission_event_floor_sequence", newest_stream_sequence)
            or newest_stream_sequence
        )
        self._mission_runtime_v2.configure(
            slot=route_slot,
            character=character_name,
            mission_id=mission_id,
            raw_steps=steps,
            goal=mission_goal,
            event_floor=mission_arm_floor,
            whiff_policy=lambda labels, step: self._step_allows_whiff_confirm(list(labels), step),
        )

        snap = (snaps_dict or {}).get(route_slot) or {}
        route_char_id = self._snap_char_id(snap)
        realtime_key = self._set_mission_realtime_context(
            route_slot=route_slot,
            character_name=character_name,
            mission_id=mission_id,
            steps=steps,
            mission_goal=mission_goal,
            event_floor=mission_arm_floor,
            char_id=route_char_id,
        )
        # The realtime worker owns event consumption. The GUI frame only reads
        # its newest immutable result, so scanner/render stalls cannot batch
        # several mission steps together.
        result = self._realtime_result_for_key(realtime_key, steps, mission_arm_floor)

        def _clear_v2(final_count: int, final_idx: int, final_label: str | None) -> dict:
            if character_name and mission_id:
                progress = load_progress()
                progress = mark_mission_complete(progress, character_name, mission_id)
                save_progress(progress)

            next_seq = int(self._runtime.get("clear_seq", 0) or 0) + 1
            next_token = int(self._runtime.get("celebrate_token", 0) or 0) + 1
            self._release_completed_mission_overrides((slot, character_name, mission_id))

            cp = build_overlay_payload(character_name or "")
            cp.update({
                "active": True,
                "slot": slot,
                "point_slot": route_slot,
                "just_cleared": True,
                "clear_seq": next_seq,
                "celebrate_pending": True,
                "celebrate_token": next_token,
                "completed_step_count": final_count,
                "confirmed_step_count": final_count,
                "predicted_step_count": 0,
                "prediction_active": False,
                "current_step_index": final_idx,
                "current_step_label": final_label,
                "mission_runtime_version": 2,
                "mission_attempt_id": result.attempt_id,
                "mission_restart_count": result.restart_count,
                "mission_last_restart_reason": result.last_restart_reason,
                "mission_event_cursor": result.event_cursor,
            })
            # Keep the runtime in CLEAR until the defender actually recovers.
            # Rearming here made the trial visually reset on the final hit and
            # threw away the continuity state needed to observe knockdown ->
            # wakeup/Idle.  The realtime runtime now rearms itself only from the
            # defender recovery boundary (or grace expiry).
            self._runtime["clear_seq"] = next_seq
            self._runtime["celebrate_token"] = next_token
            self._runtime["celebrate_pending"] = True
            self._runtime["celebrate_acked_token"] = 0
            self._runtime["v2_clear_latched_attempt"] = int(result.attempt_id)
            return cp

        total_steps = len(steps) if steps else 1
        if result.cleared:
            final_idx = max(0, total_steps - 1)
            final_label = result.current_step_label
            if steps:
                final_step = steps[final_idx]
                if isinstance(final_step, dict):
                    final_label = str(final_step.get("display") or "").strip() or " / ".join(self._step_labels(final_step))
                else:
                    final_label = " / ".join(self._step_labels(final_step))

            # Celebrate/persist exactly once, but hold the completed route on
            # screen while the defender is still in hitstun/knockdown.
            latched_attempt = int(self._runtime.get("v2_clear_latched_attempt", 0) or 0)
            if latched_attempt == int(result.attempt_id):
                payload.update({
                    "just_cleared": False,
                    "clear_seq": int(self._runtime.get("clear_seq", 0) or 0),
                    "celebrate_pending": bool(self._runtime.get("celebrate_pending", False)),
                    "celebrate_token": int(self._runtime.get("celebrate_token", 0) or 0),
                    "completed_step_count": total_steps,
                    "confirmed_step_count": total_steps,
                    "predicted_step_count": 0,
                    "prediction_active": False,
                    "current_step_index": final_idx,
                    "current_step_label": final_label,
                    "mission_runtime_version": 2,
                    "mission_attempt_id": int(result.attempt_id),
                    "mission_attempt_phase": str(result.phase),
                    "mission_event_floor": int(result.event_floor),
                    "mission_restart_count": int(result.restart_count),
                    "mission_last_restart_reason": str(result.last_restart_reason),
                    "mission_event_cursor": int(result.event_cursor),
                })
                return payload
            return _clear_v2(total_steps, final_idx, final_label)

        # Recovery starts a new attempt. Drop the old clear latch only after the
        # realtime runtime has actually crossed that defender boundary.
        if int(self._runtime.get("v2_clear_latched_attempt", 0) or 0):
            if int(self._runtime.get("v2_clear_latched_attempt", 0) or 0) != int(result.attempt_id):
                self._runtime.pop("v2_clear_latched_attempt", None)

        confirmed = max(0, int(result.progress_index))
        ready_progress = confirmed
        if result.goal_progress_type is None:
            ready_progress = max(confirmed, int(result.ready_progress_index or 0))
        completed = max(0, min(total_steps, ready_progress))
        predicted = max(0, completed - confirmed)
        prediction_active = bool(predicted and result.prediction_active)

        payload.update({
            "just_cleared": False,
            "clear_seq": int(self._runtime.get("clear_seq", 0) or 0),
            "celebrate_pending": bool(self._runtime.get("celebrate_pending", False)),
            "celebrate_token": int(self._runtime.get("celebrate_token", 0) or 0),
            "completed_step_count": completed,
            "confirmed_step_count": confirmed,
            "predicted_step_count": predicted,
            "prediction_active": prediction_active,
            "current_step_index": int(result.progress_index),
            "current_step_label": result.current_step_label,
            "mission_event_cursor": int(result.event_cursor),
            "mission_buffered_advances": int(result.buffered_advances),
            "mission_runtime_version": 2,
            "mission_attempt_id": int(result.attempt_id),
            "mission_attempt_phase": str(result.phase),
            "mission_event_floor": int(result.event_floor),
            "mission_restart_count": int(result.restart_count),
            "mission_last_restart_reason": str(result.last_restart_reason),
            "goal_progress_type": result.goal_progress_type,
            "goal_current_frames": int(result.goal_current_frames),
            "goal_needed_frames": int(result.goal_needed_frames),
            "goal_timer_active": bool(result.goal_timer_active),
            "goal_current_damage": int(result.goal_damage),
            "goal_needed_damage": int(result.goal_damage_needed),
            "goal_current_hits": int(result.goal_hits),
            "goal_max_hits": int(result.goal_max_hits),
        })
        return payload

    def _augment_payload_with_runtime_legacy(self, payload: dict, snaps_dict: dict) -> dict:
        payload = dict(payload or {})
        slot = payload.get("slot")
        route_slot = payload.get("point_slot") or self._mission_owner_slot(snaps_dict) or slot
        mission_id = payload.get("active_mission_id")
        steps = list(payload.get("active_mission_steps") or [])
        mission_goal = dict(payload.get("active_mission_goal") or {})
        character_name = payload.get("character")
        frame_idx = self._frame_idx
        meter_refill_enabled = self._sync_meter_refill_mission(payload)

        def _clear_payload(final_count, final_idx, final_label):
            print(f"[mission clear] slot={slot} mission_id={mission_id} character={character_name!r}")
            if character_name and mission_id:
                progress = load_progress()
                progress = mark_mission_complete(progress, character_name, mission_id)
                save_progress(progress)

            next_seq = int(self._runtime.get("clear_seq", 0)) + 1
            next_token = int(self._runtime.get("celebrate_token", 0)) + 1

            self._release_completed_mission_overrides(
                (slot, character_name, mission_id)
            )

            cp = build_overlay_payload(character_name or "")
            cp["active"] = True
            cp["slot"] = slot
            cp["point_slot"] = route_slot
            cp["just_cleared"] = True
            cp["clear_seq"] = next_seq
            cp["celebrate_pending"] = True
            cp["celebrate_token"] = next_token
            cp["completed_step_count"] = final_count
            cp["confirmed_step_count"] = final_count
            cp["predicted_step_count"] = 0
            cp["prediction_active"] = False
            cp["current_step_index"] = final_idx
            cp["current_step_label"] = final_label

            self._runtime = self._fresh_runtime(
                slot=slot,
                mission_id=mission_id,
                clear_seq=next_seq,
                celebrate_token=next_token,
                celebrate_pending=True,
                celebrate_acked_token=0,
            )
            return cp

        if not payload.get("active") or not slot or not mission_id or (not steps and not mission_goal):
            self._runtime = self._fresh_runtime()
            payload.update({
                "completed_step_count": 0,
                "current_step_index": 0,
                "current_step_label": steps[0] if steps else None,
                "just_cleared": False,
                "celebrate_pending": False,
                "celebrate_token": 0,
            })
            return payload

        if (
            self._runtime.get("slot") != slot
            or self._runtime.get("mission_id") != mission_id
        ):
            self._runtime = self._fresh_runtime(
                slot=slot,
                mission_id=mission_id,
                clear_seq=int(self._runtime.get("clear_seq", 0) or 0),
                celebrate_token=int(self._runtime.get("celebrate_token", 0) or 0),
                celebrate_pending=bool(self._runtime.get("celebrate_pending", False)),
                celebrate_acked_token=int(self._runtime.get("celebrate_acked_token", 0) or 0),
            )

        snap = snaps_dict.get(route_slot) or self._render_snap_by_slot.get(route_slot) if hasattr(self, "_render_snap_by_slot") else snaps_dict.get(route_slot) or {}
        snap = snap or {}
        current_label = str(
            snap.get("final_move_label")
            or snap.get("mv_label_display")
            or snap.get("mv_label")
            or ""
        ).strip()
        current_anim = snap.get("mv_id_display")
        current_inputs = snap.get("inputs") or {}

        realtime_inputs: dict = {}
        realtime_damage: list[int] = []
        realtime_hitstun_exit = False
        realtime_hitstun_state: bool | None = None
        if callable(self._event_provider):
            realtime_inputs = self._record_mission_input_stream(
                str(route_slot), snap, frame_idx
            )
            realtime_damage, realtime_hitstun_exit, realtime_hitstun_state = (
                self._record_opponent_realtime_stream(
                    str(route_slot), snaps_dict, frame_idx
                )
            )
            self._prune_mission_event_buffers()
            if realtime_inputs:
                current_inputs = realtime_inputs
            action_events = list(self._runtime.get("mission_action_events") or [])
            if action_events:
                latest_action = action_events[-1]
                action_labels = list(latest_action.get("labels") or [])
                if action_labels:
                    current_label = str(action_labels[0])
                current_anim = int(latest_action.get("action_id", 0) or 0)

        opponent_in_hitstun = (
            bool(realtime_hitstun_state)
            if realtime_hitstun_state is not None
            else self._opponent_in_hitstun(route_slot, snaps_dict)
        )
        previous_actual_hitstun = bool(
            self._runtime.get("last_actual_hitstun", False)
        )
        actual_hitstun_now = bool(opponent_in_hitstun)
        hitstun_exit_edge = bool(
            realtime_hitstun_exit
            or (previous_actual_hitstun and not actual_hitstun_now)
        )
        damage_values = (
            realtime_damage
            if callable(self._event_provider)
            else self._opponent_damage_this_frame(route_slot, snaps_dict)
        )
        opponent_took_damage = bool(damage_values)
        frame_damage = sum(int(x) for x in damage_values)
        baroque_pool_adjusted = bool(
            snap.get("baroque_cancel_raw")
            or realtime_inputs.get("baroque", False)
        )

        # Declarative five-bar setup. Preserve the old reliable behavior,
        # but enable it from mission JSON for any character.
        if meter_refill_enabled:
            meter_val = int(snap.get("meter", 0) or 0)
            self._write_debug_flag("BaroquePct", 1)
            if opponent_in_hitstun:
                self._write_debug_flag("P1Meter", 0)
            else:
                self._write_debug_flag("P1Meter", 1 if meter_val < 50000 else 0)

        progress_index = int(self._runtime.get("progress_index", 0))
        starting_progress_index = progress_index
        starting_consumed_input_serial = int(
            self._runtime.get("mission_input_consumed_serial", 0) or 0
        )

        # Drain the immutable event timeline before evaluating the late GUI
        # snapshot. event_index and mission_index are independent, so one GUI
        # update can confirm every action and hit that happened while it was
        # busy. The snapshot path below remains only for legacy and unusual
        # declarative steps that are not yet represented by native events.
        buffered_advanced = 0
        if callable(self._event_provider) and steps:
            progress_index, action_advanced = self._drain_buffered_action_steps(
                steps, progress_index, frame_idx
            )
            buffered_advanced += action_advanced
            progress_index, normal_advanced = self._drain_buffered_normal_steps(
                steps, progress_index, frame_idx
            )
            buffered_advanced += normal_advanced
            self._runtime["progress_index"] = progress_index

        progress_index, jump_cancel_advanced = self._consume_jump_cancel_free_passes(
            steps, progress_index
        )
        buffered_advanced += jump_cancel_advanced
        self._runtime["progress_index"] = progress_index

        if progress_index >= len(steps):
            final_idx = max(0, len(steps) - 1)
            final_label = (
                " / ".join(self._step_labels(steps[final_idx]))
                if steps else None
            )
            return _clear_payload(len(steps), final_idx, final_label)

        # Hitstun grace
        if opponent_in_hitstun:
            self._runtime["hitstun_grace"] = 0
        else:
            self._runtime["hitstun_grace"] = 0

        # Shell release grace
        shell_release_grace = int(self._runtime.get("shell_release_grace", 0) or 0)
        if shell_release_grace > 0:
            self._runtime["shell_release_grace"] = shell_release_grace - 1

        reset_grace_active_now = int(self._runtime.get("reset_grace_frames", 0)) > 0
        global_combo_count = self._global_combo_count()
        global_combo_active = bool(
            global_combo_count is not None and int(global_combo_count) > 0
        )
        dedicated_megacrash_match = self._opponent_in_megacrash(route_slot, snaps_dict)
        opponent_real_combo_state = _mission_route_combo_live(
            global_combo_active,
            opponent_in_hitstun,
            dedicated_megacrash_match,
        ) or dedicated_megacrash_match
        opponent_in_combo_state = opponent_real_combo_state

        if (
            self._runtime.get("shell_installed")
            and self._saki_shell_release_label(current_label)
            and opponent_in_combo_state
        ):
            self._runtime["shell_release_grace"] = 20

        # Goal-type missions
        if mission_goal:
            goal_type = str(mission_goal.get("type", "")).strip().lower()

            if opponent_in_combo_state and frame_damage > 0:
                self._runtime["goal_combo_damage"] = int(self._runtime.get("goal_combo_damage", 0)) + frame_damage
                if self._runtime.get("goal_last_damage_frame") != frame_idx:
                    self._runtime["goal_combo_hits"] = int(self._runtime.get("goal_combo_hits", 0)) + 1
                    self._runtime["goal_last_damage_frame"] = frame_idx

            def _goal_base(payload):
                payload["just_cleared"] = False
                payload["clear_seq"] = int(self._runtime.get("clear_seq", 0))
                payload["celebrate_pending"] = bool(self._runtime.get("celebrate_pending", False))
                payload["celebrate_token"] = int(self._runtime.get("celebrate_token", 0) or 0)
                payload["completed_step_count"] = 0
                payload["current_step_index"] = 0

            if goal_type == "state_duration":
                target_state = str(mission_goal.get("target_state", "")).strip().lower()
                needed = int(mission_goal.get("frames", 0) or 0)
                in_target = (
                    self._opponent_in_state(route_slot, snaps_dict, MISSION_BLOCKSTUN_STATES)
                    if target_state == "blockstun" else False
                )
                if in_target:
                    self._runtime["goal_state_frames"] = int(self._runtime.get("goal_state_frames", 0)) + 1
                else:
                    self._runtime["goal_state_frames"] = 0
                current_f = int(self._runtime.get("goal_state_frames", 0))
                _goal_base(payload)
                payload.update({
                    "current_step_label": f"{current_f}/{needed} frames",
                    "goal_progress_type": "state_duration",
                    "goal_target_state": target_state,
                    "goal_current_frames": current_f,
                    "goal_needed_frames": needed,
                    "goal_timer_active": bool(in_target),
                })
                if needed > 0 and current_f >= needed:
                    return _clear_payload(1, 0, payload["current_step_label"])
                return payload

            if goal_type == "damage_under_hits":
                needed_dmg = int(mission_goal.get("damage", 0) or 0)
                max_hits = int(mission_goal.get("max_hits", 0) or 0)
                combo_dmg = int(self._runtime.get("goal_combo_damage", 0))
                combo_hits = int(self._runtime.get("goal_combo_hits", 0))
                if max_hits > 0 and combo_hits > max_hits:
                    self._runtime["goal_failed"] = True
                _goal_base(payload)
                payload["current_step_label"] = f"{combo_dmg}/{needed_dmg} damage, {combo_hits}/{max_hits} hits"
                if (
                    needed_dmg > 0 and combo_dmg >= needed_dmg
                    and combo_hits <= max_hits
                    and not self._runtime.get("goal_failed", False)
                ):
                    return _clear_payload(1, 0, payload["current_step_label"])
                if not opponent_in_combo_state:
                    self._runtime.update({
                        "goal_combo_damage": 0, "goal_combo_hits": 0,
                        "goal_failed": False, "goal_last_damage_frame": -1,
                    })
                return payload

            if goal_type == "combo_damage":
                needed_dmg = int(mission_goal.get("damage", 0) or 0)
                combo_dmg = int(self._runtime.get("goal_combo_damage", 0))
                _goal_base(payload)
                payload["current_step_label"] = f"{combo_dmg}/{needed_dmg} combo damage"
                if needed_dmg > 0 and combo_dmg >= needed_dmg:
                    return _clear_payload(1, 0, payload["current_step_label"])
                if not opponent_in_combo_state:
                    self._runtime.update({
                        "goal_combo_damage": 0, "goal_combo_hits": 0,
                        "goal_failed": False, "goal_last_damage_frame": -1,
                    })
                return payload

        # Step-list missions
        expected_step_for_reset = steps[progress_index] if progress_index < len(steps) else None
        expected_labels_for_reset = self._step_labels(expected_step_for_reset)
        expected_label_visible_for_reset = bool(
            expected_labels_for_reset
            and self._mission_label_matches(current_label, expected_labels_for_reset)
            and not self._label_is_ignorable(current_label)
        )

        grace_left = int(self._runtime.get("reset_grace_frames", 0) or 0)
        grace_step_index = self._runtime.get("reset_grace_step_index")
        explicit_reset_grace = bool(
            grace_left > 0 and grace_step_index == progress_index
        )
        hitstun_exit_requires_reset = bool(
            progress_index > 0
            and hitstun_exit_edge
            and not explicit_reset_grace
        )

        if progress_index > 0 and not opponent_real_combo_state:
            if expected_label_visible_for_reset and not (
                grace_left > 0 and grace_step_index == progress_index
            ):
                # Cinematic or delayed expected move is visibly active. Keep the
                # route armed, but do not treat this as a hit confirmation.
                reset_now = False
                remaining_grace = grace_left
            else:
                reset_now, remaining_grace = _mission_combo_reset_tick(
                    progress_index,
                    opponent_real_combo_state,
                    grace_left,
                    grace_step_index,
                )
            self._runtime["reset_grace_frames"] = remaining_grace
            if hitstun_exit_requires_reset:
                reset_now = True
            if reset_now:
                progress_index = 0
                self._runtime.update({
                    "progress_index": 0,
                    "pending_step_index": None,
                    "pending_labels": [],
                    "pending_anim": None,
                    "pending_frame": None,
                    "reset_grace_frames": 0,
                    "reset_grace_labels": [],
                    "reset_grace_step_index": None,
                    "reset_grace_keeps_alive_only": False,
                    "shell_installed": False,
                    "shell_release_grace": 0,
                    "last_seen_label": "",
                    "last_seen_anim": None,
                    "last_seen_hitstun": False,
                    "last_actual_hitstun": False,
                    "last_inputs": {},
                })

        last_seen_label = self._runtime.get("last_seen_label", "")
        last_seen_anim = self._runtime.get("last_seen_anim")
        last_seen_hitstun = bool(
            self._runtime.get(
                "last_actual_hitstun",
                self._runtime.get("last_seen_hitstun", False),
            )
        )
        hitstun_edge_confirm = bool(opponent_in_hitstun and not last_seen_hitstun)
        last_inputs = self._runtime.get("last_inputs") or {}

        has_fresh_attack_input = self._has_fresh_attack_input(current_inputs, last_inputs)

        is_fresh_instance = (
            current_anim != last_seen_anim
            or current_label != last_seen_label
            or (opponent_in_combo_state and not last_seen_hitstun)
            or has_fresh_attack_input
        )

        expected_step = steps[progress_index] if progress_index < len(steps) else None
        expected_labels = self._step_labels(expected_step)

        generic_partner_var_step = self._step_is_generic_partner_var(mission_id, expected_labels)
        partner_var_matched = generic_partner_var_step and self._partner_matches_generic_var(route_slot, snaps_dict)

        current_matches_expected = self._mission_label_matches(current_label, expected_labels)
        non_damage_confirm = self._step_has_non_damage_confirm(expected_labels, snap, current_label)
        step_allows_whiff = self._step_allows_whiff_confirm(expected_labels)
        pass_confirm = self._step_is_pass(expected_step)
        zero_damage_confirm = (
            self._step_allows_zero_damage_confirm(character_name, expected_labels, current_label)
            and not pass_confirm
        )
        doronjo_pass = self._doronjo_damage_pass(character_name, expected_labels, damage_values)
        baroque_damage_confirm = (
            self._step_is_baroque_cancel(expected_labels)
            and opponent_in_combo_state
            and baroque_pool_adjusted
        )

        if self._runtime.get("pending_step_index") != progress_index:
            self._runtime.update({
                "pending_step_index": None,
                "pending_labels": [],
                "pending_anim": None,
            })

        pending_step_index = self._runtime.get("pending_step_index")
        pending_labels = list(self._runtime.get("pending_labels") or [])

        reset_grace_active = int(self._runtime.get("reset_grace_frames", 0) or 0) > 0
        reset_grace_confirm_allowed = False
        reset_grace_match = (
            reset_grace_active
            and current_matches_expected
            and not self._label_is_ignorable(current_label)
        )
        post_install_match = (
            character_name == "Saki"
            and int(self._runtime.get("post_install_hold_frames", 0) or 0) > 0
            and current_matches_expected
            and has_fresh_attack_input
            and not self._is_saki_shell_label(current_label)
        )

        baroque_buffered_for_next_step = (
            progress_index + 1 < len(steps)
            and baroque_pool_adjusted
            and opponent_in_combo_state
            and self._step_is_baroque_cancel(
                self._step_labels(steps[progress_index + 1])
            )
        )

        matched_fresh_expected = (
            expected_labels
            and (
                partner_var_matched
                or (
                    current_matches_expected
                    and not self._label_is_ignorable(current_label)
                    and (
                        pass_confirm
                        or (
                            is_fresh_instance
                        )
                    )
                )
            )
        )

        if partner_var_matched:
            print(f"[mission generic var] slot={slot} step={progress_index} labels={expected_labels!r}")
            completed_grace = self._step_grace(expected_step)
            progress_index += 1
            self._runtime.update({
                "progress_index": progress_index,
                "pending_step_index": None,
                "pending_labels": [],
                "pending_anim": None,
                "reset_grace_frames": completed_grace,
                "reset_grace_labels": [],
                "reset_grace_step_index": progress_index if completed_grace > 0 else None,
                "shell_install_hold": 0,
            })
            if (
                progress_index < len(steps)
                and self._step_is_baroque_cancel(self._step_labels(steps[progress_index]))
                    and baroque_pool_adjusted
                    and opponent_in_combo_state
                ):
                    progress_index += 1
                    self._runtime.update({
                        "progress_index": progress_index,
                        "pending_step_index": None,
                        "pending_labels": [],
                        "pending_anim": None,
                        "reset_grace_frames": self._step_grace(steps[progress_index - 1]),
                        "reset_grace_labels": [],
                        "reset_grace_step_index": progress_index,
                        "shell_install_hold": 0,
                    })
        elif MISSION_REQUIRE_DAMAGE_CONFIRM:
            if matched_fresh_expected:
                if pass_confirm:
                    print(
                        f"[mission pass] slot={slot} step={progress_index} "
                        f"matched={expected_labels!r}"
                    )

                    pass_grace = self._step_grace(expected_step)

                    progress_index += 1
                    self._runtime.update({
                        "progress_index": progress_index,
                        "reset_grace_frames": pass_grace,
                        "reset_grace_labels": [],
                        "reset_grace_step_index": progress_index if pass_grace > 0 else None,
                        "reset_grace_keeps_alive_only": self._step_grace_keeps_alive_only(expected_step),
                    })

                elif step_allows_whiff or zero_damage_confirm:
                    print(
                        f"[mission confirm immediate] slot={slot} step={progress_index} "
                        f"matched={expected_labels!r} zero={zero_damage_confirm}"
                    )

                    progress_index += 1

                    self._runtime.update({
                        "progress_index": progress_index,
                        "pending_step_index": None,
                        "pending_labels": [],
                        "pending_anim": None,
                        "reset_grace_frames": 0,
                        "reset_grace_labels": [],
                        "shell_install_hold": 0,
                        "post_install_hold_frames": 12 if zero_damage_confirm else 0,
                        "shell_installed": zero_damage_confirm,
                        "shell_release_grace": 0,
                        "last_seen_label": "",
                        "last_seen_anim": None,
                        "last_seen_hitstun": False,
                        "last_inputs": {},
                        "hitstun_grace": 0,
                    })

                else:
                    self._runtime.update({
                        "pending_step_index": progress_index,
                        "pending_labels": expected_labels[:],
                        "pending_anim": current_anim,
                        "pending_frame": frame_idx,
                    })
                    pending_step_index = progress_index
                    pending_labels = expected_labels[:]

            repeat_same_step_damage = self._can_repeat_same_step_on_damage(
                steps,
                progress_index,
                pending_labels,
                current_label,
                current_anim,
                damage_values,
            )

            pending_frame = int(self._runtime.get("pending_frame", frame_idx) or frame_idx)
            fresh_buffered_hit = False
            for hit_event in list(self._runtime.get("mission_hit_events") or []):
                if not isinstance(hit_event, dict) or hit_event.get("consumed"):
                    continue
                hit_frame = int(hit_event.get("frame", -1) or -1)
                if hit_frame < pending_frame:
                    continue
                if not (hit_event.get("hp_seen") or int(hit_event.get("damage", 0) or 0) > 0):
                    continue
                if not opponent_in_hitstun:
                    continue
                fresh_buffered_hit = True
                hit_event["consumed"] = True
                break

            if (
                baroque_damage_confirm
                and pending_step_index != progress_index
            ):
                self._runtime.update({
                    "pending_step_index": progress_index,
                    "pending_labels": expected_labels[:],
                    "pending_anim": current_anim,
                    "pending_frame": frame_idx,
                })
                pending_step_index = progress_index
                pending_labels = expected_labels[:]

            if (
                pending_step_index == progress_index
                and pending_labels
                and (opponent_in_combo_state or reset_grace_confirm_allowed)
                and (
                    opponent_took_damage
                    or fresh_buffered_hit
                    or hitstun_edge_confirm
                    or non_damage_confirm
                    or doronjo_pass
                    or repeat_same_step_damage
                    or baroque_damage_confirm
                )
            ):
                completed_grace = self._step_grace(expected_step)

                progress_index += 1

                if (
                    baroque_buffered_for_next_step
                    and progress_index < len(steps)
                    and self._step_is_baroque_cancel(self._step_labels(steps[progress_index]))
                ):
                    progress_index += 1

                self._runtime.update({
                    "progress_index": progress_index,
                    "pending_step_index": None,
                    "pending_labels": [],
                    "pending_anim": None,
                    "reset_grace_frames": completed_grace,
                    "reset_grace_labels": [],
                    "reset_grace_step_index": progress_index if completed_grace > 0 else None,
                    "reset_grace_keeps_alive_only": self._step_grace_keeps_alive_only(expected_step),
                    "shell_install_hold": 0,
                })
        else:
            if matched_fresh_expected:
                print(f"[mission advance] slot={slot} step={progress_index} matched={current_label!r}")
                progress_index += 1
                self._runtime["progress_index"] = progress_index

        progress_index, late_jump_cancel_advanced = self._consume_jump_cancel_free_passes(
            steps, progress_index
        )
        if late_jump_cancel_advanced:
            self._runtime["progress_index"] = progress_index

        if progress_index > starting_progress_index:
            self._runtime["mission_input_consumed_serial"] = max(
                starting_consumed_input_serial,
                int(self._runtime.get("mission_input_consumed_serial", 0) or 0),
            )

        if not partner_var_matched:
            self._runtime.update({
                "last_seen_label": current_label,
                "last_seen_anim": current_anim,
                "last_seen_hitstun": opponent_in_hitstun,
                "last_actual_hitstun": actual_hitstun_now,
                "last_inputs": dict(current_inputs),
            })

        if progress_index >= len(steps):
            final_idx = max(0, len(steps) - 1)
            final_label = (
                (" / ".join(steps[final_idx]) if isinstance(steps[final_idx], list) else steps[final_idx])
                if steps else None
            )
            return _clear_payload(len(steps), final_idx, final_label)

        tracking_current_step = bool(
            self._runtime.get("pending_step_index") == progress_index
            and list(self._runtime.get("pending_labels") or [])
        )
        predicted_progress = progress_index
        if callable(self._event_provider):
            predicted_progress = self._refresh_predicted_progress(
                steps, progress_index
            )
        predicted_step_count = max(
            1 if tracking_current_step else 0,
            predicted_progress - progress_index,
        )
        payload.update({
            "just_cleared": False,
            "clear_seq": int(self._runtime.get("clear_seq", 0)),
            "celebrate_pending": bool(self._runtime.get("celebrate_pending", False)),
            "celebrate_token": int(self._runtime.get("celebrate_token", 0) or 0),
            "completed_step_count": progress_index,
            "confirmed_step_count": progress_index,
            "predicted_step_count": predicted_step_count,
            "prediction_active": bool(predicted_step_count or tracking_current_step),
            "current_step_index": progress_index,
            "current_step_label": (
                " / ".join(self._step_labels(steps[progress_index]))
                if progress_index < len(steps) else None
            ),
            "mission_event_cursor": int(self._event_state.get("latest_sequence", 0) or 0),
            "mission_buffered_advances": int(buffered_advanced),
        })
        return payload

    # ------------------------------------------------------------------
    # Empty payload builder
    # ------------------------------------------------------------------

    def _build_empty_overlay_payload(self) -> dict:
        return {
            "active": False,
            "slot": self._active_slot,
            "point_slot": self._mission_owner.get("slot") if hasattr(self, "_mission_owner") else self._active_slot,
            "character": None,
            "mission_count": 0,
            "active_mission_id": None,
            "active_mission_name": None,
            "active_mission_steps": [],
            "active_mission_setup_meter_refill": False,
            "missions": [],
            "completed_step_count": 0,
            "confirmed_step_count": 0,
            "predicted_step_count": 0,
            "prediction_active": False,
            "current_step_index": 0,
            "current_step_label": None,
            "just_cleared": False,
            "celebrate_pending": False,
            "celebrate_token": 0,
            "selector_open": False,
            "selector_index": 0,
            "selector_hint": "Mission Mode: Down, Down, Taunt opens Mission Select",
            "selector_controls": "Up/Down: Move  Taunt: Select  Mouse still works",
            "scanlines": True,
            "goal_progress_type": None,
            "goal_target_state": None,
            "goal_current_frames": 0,
            "goal_needed_frames": 0,
            "goal_timer_active": False,
        }