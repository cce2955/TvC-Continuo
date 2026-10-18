"""
hud_overlay_manager.py
----------------------
Owns the hud_overlay subprocess and the per-frame data file it reads.

Public surface:
    mgr = HudOverlayManager(move_map, global_map)
    mgr.write_data(render_snap_by_slot, last_scan_normals, mission_mgr)
    mgr.check_proc()   # call each frame; detects if the proc died
    # subprocess is launched/stopped by master_overlay; this manager
    # only owns the data file write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import TYPE_CHECKING

from tvcgui.core.paths import user_data_path
from tvcgui.features.frame_data.attack_property_runtime import resolve_live_attack_definition
from tvcgui.runtime.realtime_sampler import RealtimeCombatSampler
from tvcgui.runtime.mission_events import MissionEventStream
from tvcgui.features.overlay.damage_scaling import annotate_damage_scaling_payload
from tvcgui.features.overlay.hitstun_scaling import annotate_hitstun_scaling_payload

if TYPE_CHECKING:
    from tvcgui.features.training.mission_manager import MissionManager

HUD_OVERLAY_DATA_FILE = user_data_path("overlay", "hud_overlay_data.json")
HUD_REALTIME_INPUT_FILE = user_data_path("overlay", "hud_input_realtime.json")
# Stun clocks are ephemeral realtime IPC, not persistent user data. Keep this
# tiny file off the project/EXE directory so OneDrive/AV cannot stall a short
# 9-15F blockstun countdown by coalescing several JSON rewrites.
HUD_REALTIME_STUN_FILE = os.path.join(tempfile.gettempdir(), "tvcgui_hud_stun_realtime.json")
# Attack-history edges get their own tiny, synchronous IPC.  The stun bridge
# is intentionally busy (action-frame/stun updates every game frame); putting
# MOVES on that transport made a correct history feel late.
HUD_REALTIME_MOVES_FILE = os.path.join(tempfile.gettempdir(), "tvcgui_hud_moves_realtime.json")
ATTACK_PROPERTY_SHORT_LABELS = {
    0x01: "UNBLK",
    0x02: "UNBLK",
    0x04: "UNBLK",
    0x09: "MID",
    0x0A: "MID",
    0x0C: "MID",
    0x11: "OVERHEAD",
    0x12: "OVERHEAD",
    0x14: "OVERHEAD",
    0x21: "LOW",
    0x22: "LOW",
    0x24: "LOW",
}

# Ryu's player-facing Tatsu strengths enter through unique command IDs, then
# transition into internal Start/Spin/End IDs that are reused by L/M/H. Keep
# the command strength alive briefly so the realtime timing lane can choose the
# correct duplicate internal row instead of collapsing everything to L.
_RYU_TATSU_ENTRY_SESSION = {
    307: ("Ground", "L"),
    308: ("Ground", "M"),
    309: ("Ground", "H"),
    323: ("Air", "L"),
    324: ("Air", "M"),
    325: ("Air", "H"),
}
_RYU_TATSU_SECTION_IDS = {273, 274, 275}
_RYU_TATSU_SESSION_MAX_NS = 2_000_000_000



# MOVES is an observable native action/state history. The 720 Hz edge sampler
# already sees attack and passive transitions; keep them all here. The renderer
# suppresses only unresolved low-ID internals, so useful labels such as Idle,
# Crouch, Jump, Jump Cancel, Landing, etc. remain visible diagnostic evidence.


def _move_history_is_observable(action_id: int) -> bool:
    return (int(action_id) & 0x7FFF) > 0

def _is_ryu_character_name(value) -> bool:
    name = str(value or "").strip().lower()
    return name == "ryu" or name.startswith("ryu ") or name.endswith(" ryu")


def _family_variant_rank(timing: dict, session: dict) -> tuple[int, int, int, int]:
    """Rank one compact timing variant against a live family session."""
    if not isinstance(timing, dict) or not isinstance(session, dict):
        return (0, 0, 0, 0)
    wanted_family = str(session.get("family_key") or "")
    wanted_group = str(session.get("family_group_key") or "")
    wanted_strength = str(session.get("strength") or "").upper()
    wanted_context = str(session.get("context") or "").lower()
    got_family = str(timing.get("fd_family_key") or "")
    got_group = str(timing.get("fd_family_group_key") or "")
    got_strength = str(timing.get("fd_family_strength") or "").upper()
    got_context = str(timing.get("fd_family_context") or "").lower()
    return (
        1 if wanted_group and got_group == wanted_group else 0,
        1 if wanted_family and got_family == wanted_family else 0,
        1 if wanted_strength and got_strength == wanted_strength else 0,
        1 if wanted_context and got_context == wanted_context else 0,
    )


def _native_hs_contact_values(sample: dict, decay_frames: int = 0) -> dict | None:
    """Choose the native victim clock already resolved on the contact sample.

    The hit resolver writes +0x1210 (resolved hitstun) and, when applicable,
    +0x1220 (scaled air-recovery/untech lockout) before the later HP
    subtraction. A 240 Hz HP-loss sample can therefore use those final engine
    values directly instead of rebuilding stun from the attacker's profile.
    """
    if not isinstance(sample, dict):
        return None
    try:
        hitstun = max(0, int(sample.get("hitstun_remaining", 0) or 0))
        untech = max(0, int(sample.get("untech_remaining", 0) or 0))
        decay = max(0, int(decay_frames or 0))
    except Exception:
        return None

    if untech > 0:
        return {
            "clock_source": "untech",
            "target": untech,
            "native_hitstun": hitstun,
            "native_untech": untech,
            "decay_frames": decay,
            # Reconstruction is display-only. The authoritative endpoint is
            # always the final native +0x1220 value above.
            "raw_estimate": untech + decay,
        }
    if hitstun > 0:
        return {
            "clock_source": "hitstun",
            "target": hitstun,
            "native_hitstun": hitstun,
            "native_untech": 0,
            "decay_frames": 0,
            "raw_estimate": hitstun,
        }
    return None


def _known_attack_property(value) -> bool:
    try:
        packed = int(value) & 0xFF
    except Exception:
        return False
    guard = packed & 0x38
    strength = packed & 0x07
    return guard in (0x00, 0x08, 0x10, 0x20) and strength in (0x01, 0x02, 0x04)


def _overlay_attack_property_label(value) -> str:
    try:
        packed = int(value) & 0xFF
    except Exception:
        return ""
    if not _known_attack_property(packed):
        return ""
    guard = packed & 0x38
    if guard == 0x10:
        return "OVERHEAD"
    if guard == 0x20:
        return "LOW"
    if guard == 0x08:
        return "MID"
    return "UNBLK"





def _attack_property_move_quality(move: dict | None) -> tuple[int, int, int]:
    """Prefer the populated owner when one action ID has duplicate scan rows."""
    if not isinstance(move, dict):
        return (0, 0, 0)
    segments = [row for row in (move.get("hit_segments") or []) if isinstance(row, dict)]
    compact_windows = [row for row in (move.get("active_windows") or []) if isinstance(row, (list, tuple)) and len(row) >= 2]
    populated = sum(
        1 for key in (
            "attack_property", "damage", "active_start", "active_end",
            "hit_reaction", "kb_type", "hitstun", "blockstun",
        )
        if move.get(key) is not None
    )
    source_bonus = 1 if str(move.get("source") or "") == "table" else 0
    return (max(len(segments), len(compact_windows)), populated, source_bonus)


def _frame_data_timing_from_move(move: dict | None) -> dict | None:
    """Return the tiny timing record needed by the realtime FD rail.

    The normal/profile scanner already owns the authored timing data.  This
    helper strips that row down to immutable startup/active/recovery geometry
    so the 240 Hz action lane can arm the HUD without waiting for the next
    heavyweight GUI snapshot.
    """
    if not isinstance(move, dict):
        return None
    try:
        action_id = int(move.get("id") or 0) & 0x7FFF
    except Exception:
        return None
    if action_id <= 0:
        return None

    windows: list[list[int]] = []

    def append_window(start, end) -> None:
        try:
            start_i = int(start)
            end_i = int(end)
        except (TypeError, ValueError):
            return
        if start_i < 0 or end_i < start_i:
            return
        pair = [start_i, end_i]
        if pair not in windows:
            windows.append(pair)

    # The FD rail represents attack-active time, not every script timing
    # candidate. ``active2`` is discovered independently by an inline pattern
    # and can be present without a corresponding hit/damage bundle. Prefer the
    # primary active packet and confirmed hit segments so those candidates do
    # not become phantom red windows.
    append_window(move.get("active_start"), move.get("active_end"))
    segments = [seg for seg in (move.get("hit_segments") or []) if isinstance(seg, dict)]
    for segment in segments:
        append_window(segment.get("active_start"), segment.get("active_end"))

    # Compact timing rows may already contain a curated all-hit window list.
    # Use it when no richer per-hit bundle survived into this row.
    if not segments:
        for pair in (move.get("active_windows") or []):
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                append_window(pair[0], pair[1])

    # Active 2 is a fallback only. It may rescue an odd move whose scanner row
    # has no primary active packet, but it cannot invent an additional hit next
    # to otherwise valid timing.
    if not windows:
        append_window(move.get("active2_start"), move.get("active2_end"))
    windows.sort(key=lambda pair: (pair[0], pair[1]))
    if not windows:
        return None

    recovery = move.get("runtime_observed_recovery")
    if recovery is None:
        recovery = move.get("recovery")
    try:
        recovery_i = max(0, int(recovery or 0))
    except (TypeError, ValueError):
        recovery_i = 0
    try:
        startup_i = max(0, int(move.get("startup") or 0))
    except (TypeError, ValueError):
        startup_i = 0

    return {
        "fd_available": True,
        "fd_action_id": action_id,
        "fd_startup": startup_i,
        "fd_active_start": int(windows[0][0]),
        "fd_active_end": int(windows[0][1]),
        "fd_active_windows": windows,
        "fd_recovery": recovery_i,
        # Resolver-only metadata. The tiny HUD sidecar strips these fields after
        # a variant is selected, so they do not enlarge the render payload.
        "fd_family_key": str(move.get("family_key") or ""),
        "fd_family_group_key": str(move.get("family_group_key") or ""),
        "fd_family_strength": str(move.get("family_strength") or move.get("family_strength_guess") or ""),
        "fd_family_context": str(move.get("family_context") or ""),
        "fd_family_phase": str(move.get("family_phase") or ""),
    }


def _frame_data_timing_cache_from_scan(last_scan_normals) -> dict[str, dict]:
    """Build slot/character action timing tables from an existing scan.

    ``actions`` remains the one-row-per-action compatibility map. ``variants``
    preserves family-disambiguated duplicates for actions whose native ID is
    reused by multiple player-facing strengths.
    """
    result: dict[str, dict] = {}
    for slot_data in (last_scan_normals or []):
        if not isinstance(slot_data, dict):
            continue
        slot = str(slot_data.get("slot_label") or "")
        if not slot:
            continue
        try:
            char_id = int(slot_data.get("char_id") or 0)
        except Exception:
            char_id = 0
        char_name = str(slot_data.get("char_name") or "")

        best_by_action: dict[int, dict] = {}
        best_variant_rows: dict[int, dict[str, dict]] = {}
        # The visible Normals Preview intentionally remains normal-only. The
        # FD rail consumes its sibling all-action timing table when available.
        # Fall back to moves for older preview files during migration.
        timing_source = slot_data.get("timing_moves") or slot_data.get("moves") or []
        for move in timing_source:
            if not isinstance(move, dict):
                continue
            try:
                action_id = int(move.get("id") or 0) & 0x7FFF
            except Exception:
                continue
            if action_id <= 0:
                continue
            current = best_by_action.get(action_id)
            if current is None or _attack_property_move_quality(move) > _attack_property_move_quality(current):
                best_by_action[action_id] = move

            family_group = str(move.get("family_group_key") or "").strip()
            if family_group:
                family_rows = best_variant_rows.setdefault(action_id, {})
                previous = family_rows.get(family_group)
                if previous is None or _attack_property_move_quality(move) > _attack_property_move_quality(previous):
                    family_rows[family_group] = move

        timing_by_action: dict[int, dict] = {}
        for action_id, move in best_by_action.items():
            timing = _frame_data_timing_from_move(move)
            if timing:
                timing_by_action[action_id] = timing

        variants_by_action: dict[int, list[dict]] = {}
        for action_id, family_rows in best_variant_rows.items():
            variants: list[dict] = []
            for _group, move in sorted(family_rows.items()):
                timing = _frame_data_timing_from_move(move)
                if timing:
                    variants.append(timing)
            if variants:
                variants_by_action[action_id] = variants

        result[slot] = {
            "char_id": char_id,
            "char_name": char_name,
            "actions": timing_by_action,
            "variants": variants_by_action,
        }
    return result


def _bridge_attack_property_annotations(slot_payload: dict, snap: dict) -> None:
    """Forward profiler annotations into the subprocess payload.

    The telemetry scheduler annotates the main-process fighter snapshot, but
    the overlay subprocess only sees fields serialized here. Static move
    definitions have no runtime actor address, so dropping display/source
    metadata makes valid properties look inactive.
    """
    for key, value in (snap or {}).items():
        if key.startswith("attack_property_") and key not in {
            "attack_property",
            "attack_property_label",
        }:
            slot_payload[key] = value


class HudOverlayManager:
    """
    Writes hud_overlay_data.json each frame so the hud_overlay subprocess
    (parented to Dolphin) can render the transparent HUD.

    Parameters
    ----------
    move_map : dict
        Per-character anim-ID -> label mapping.
    global_map : dict
        Global anim-ID -> label mapping.
    """

    def __init__(
        self,
        move_map: dict,
        global_map: dict,
        realtime_sampler: RealtimeCombatSampler | None = None,
    ) -> None:
        self._move_map = move_map
        self._global_map = global_map

        self._proc: subprocess.Popen | None = None
        self._active: bool = False
        self._last_serialized: str = ""
        self._payload_condition = threading.Condition()
        self._pending_payload: dict | None = None
        self._payload_writer_stop = False
        self._payload_writer_thread = threading.Thread(
            target=self._payload_writer_loop,
            name="TvCOverlayPayloadWriter",
            daemon=True,
        )
        self._payload_writer_thread.start()

        # Realtime Dolphin reads belong to a permanent scheduler outside the
        # overlay. HudOverlayManager only reads cached packets from it.
        self._realtime_sampler = realtime_sampler or RealtimeCombatSampler()
        self._owns_realtime_sampler = realtime_sampler is None

        # Mission Runtime V2 must be wired at the sampler layer, not by optional
        # launcher glue. Own the immutable event stream here beside the realtime
        # sampler so every live build has the same path:
        # sampler -> MissionEventStream -> MissionManager.
        self._mission_event_stream = MissionEventStream()
        self._mission_event_manager = None
        self._realtime_sampler.add_listener(self._mission_event_stream.on_sample)

        # The normal HUD payload is produced by the main GUI frame and may be
        # delayed by heavier scanners. Inputs are latency-sensitive, so mirror
        # only real input transitions into a tiny sidecar file from a dedicated
        # writer thread. The HUD subprocess polls this file at its own 60 Hz
        # render cadence and no longer waits for the full telemetry payload.
        self._input_bridge_condition = threading.Condition()
        self._input_bridge_stop = False
        self._input_bridge_dirty = False
        self._stun_bridge_stop = False
        self._stun_bridge_dirty = False
        self._input_bridge_slots: dict[str, dict] = {}
        self._input_bridge_last_held: dict[str, int] = {}
        self._input_bridge_last_combat: dict[str, tuple] = {}
        self._realtime_latest_sample_by_slot: dict[str, dict] = {}
        # Authoritative native action-edge log for the MOVES row.  Keep this
        # independent from the latest combat snapshot: short cancels can begin
        # and end between HUD frames, but once an action edge is observed it
        # must remain committed to history.
        # (fighter base, action id, action frame, sample_ns).  Keeping the
        # native frame makes repeated same-ID moves first-class instances.
        self._move_history_last_action_by_slot: dict[str, tuple[int, int, int, int]] = {}
        self._move_history_edge_serial_by_slot: dict[str, int] = {}
        self._move_history_log_sequence: int = 0
        # Pre-indexed scanner timing rows for the latency-critical FD rail.
        # Keyed by slot then action ID, with a character ID guard so tag/team
        # changes cannot reuse another fighter's timing for the same action ID.
        self._realtime_fd_timing_by_slot: dict[str, dict] = {}
        self._realtime_fd_family_session_by_slot: dict[str, dict] = {}

        # Attacker move identity is cached only as optional contact metadata.
        # The 240 Hz victim HP edge mints the HS event from the victim's own
        # already-resolved +0x1210/+0x1220 counters. Duration never comes from
        # the profile cache or an inferred wall-clock timer.
        self._hs_armed_by_slot: dict[str, dict] = {}
        self._hs_bridge_teams: dict[str, dict] = {
            "P1": {"latest": {}, "events": []},
            "P2": {"latest": {}, "events": []},
        }
        self._hs_generation_by_team: dict[str, int] = {"P1": 0, "P2": 0}
        # HP subtraction and the native stun timer do not always land in the
        # same 240 Hz packet.  Keep a tiny per-victim pending contact so a hit
        # can mint its generation on the very next native +0x1210/+0x1220 edge
        # instead of being lost for the rest of the combo.
        self._hs_pending_contact_by_victim: dict[str, dict] = {}
        self._bs_bridge_teams: dict[str, dict] = {
            "P1": {"latest": {}, "events": []},
            "P2": {"latest": {}, "events": []},
        }
        self._bs_generation_by_team: dict[str, int] = {"P1": 0, "P2": 0}
        self._last_realtime_input_serialized = ""
        self._last_realtime_stun_serialized = ""
        for path in (HUD_REALTIME_INPUT_FILE, HUD_REALTIME_STUN_FILE, HUD_REALTIME_MOVES_FILE):
            try:
                os.remove(path)
            except OSError:
                pass
        self._input_bridge_thread = threading.Thread(
            target=self._input_bridge_writer_loop,
            name="TvCRealtimeInputBridge",
            daemon=True,
        )
        self._input_bridge_thread.start()
        self._stun_bridge_thread = threading.Thread(
            target=self._stun_bridge_writer_loop,
            name="TvCRealtimeStunBridge",
            daemon=True,
        )
        self._stun_bridge_thread.start()
        self._realtime_sampler.add_listener(self._on_realtime_input_sample)

    def _refresh_realtime_fd_timing(self, last_scan_normals) -> None:
        """Publish the current scanner timing index to the realtime lane."""
        updates = _frame_data_timing_cache_from_scan(last_scan_normals)
        if not updates:
            return
        with self._input_bridge_condition:
            for slot, entry in updates.items():
                # An explicit row with zero moves is still meaningful: it
                # clears a previous character's action map for that slot.
                self._realtime_fd_timing_by_slot[str(slot)] = dict(entry)

    def _realtime_tatsu_session_for(
        self,
        slot: str,
        action_id: int,
        sample_ns: int,
    ) -> dict | None:
        """Retain Ryu Tatsu L/M/H ownership across internal phase IDs."""
        slot = str(slot or "")
        entry = self._realtime_fd_timing_by_slot.get(slot) or {}
        char_name = str(entry.get("char_name") or "")
        if not _is_ryu_character_name(char_name):
            self._realtime_fd_family_session_by_slot.pop(slot, None)
            return None

        try:
            action_id = int(action_id or 0) & 0x7FFF
            now_ns = int(sample_ns or time.monotonic_ns())
        except Exception:
            return None

        owner = _RYU_TATSU_ENTRY_SESSION.get(action_id)
        if owner:
            context, strength = owner
            group_key = f"ryu:tatsu:{context.lower()}_{strength.lower()}_chain"
            session = {
                "family_key": "ryu:tatsu",
                "family_group_key": group_key,
                "context": context,
                "strength": strength,
                "entry_action_id": action_id,
                "started_ns": now_ns,
                "last_seen_ns": now_ns,
            }
            self._realtime_fd_family_session_by_slot[slot] = session
            return dict(session)

        session = self._realtime_fd_family_session_by_slot.get(slot)
        if not isinstance(session, dict):
            return None
        try:
            age = now_ns - int(session.get("last_seen_ns") or session.get("started_ns") or now_ns)
            total = now_ns - int(session.get("started_ns") or now_ns)
        except Exception:
            age = total = _RYU_TATSU_SESSION_MAX_NS + 1
        if age > _RYU_TATSU_SESSION_MAX_NS or total > _RYU_TATSU_SESSION_MAX_NS:
            self._realtime_fd_family_session_by_slot.pop(slot, None)
            return None

        if action_id in _RYU_TATSU_SECTION_IDS:
            session["last_seen_ns"] = now_ns
            return dict(session)

        # Do not destroy the owner on a brief helper/transition state. The
        # session is only consulted for the known shared Tatsu section IDs and
        # expires quickly on its own. A new Tatsu entry replaces it instantly.
        return dict(session)

    def _realtime_fd_timing_for(
        self,
        slot: str,
        char_id: int,
        action_id: int,
        family_session: dict | None = None,
    ) -> dict:
        """Resolve timing for one native action without any memory reads."""
        try:
            action_id = int(action_id or 0) & 0x7FFF
            char_id = int(char_id or 0)
        except Exception:
            return {"fd_available": False, "fd_action_id": 0}
        entry = self._realtime_fd_timing_by_slot.get(str(slot)) or {}
        try:
            cached_char_id = int(entry.get("char_id") or 0)
        except Exception:
            cached_char_id = 0
        if char_id and cached_char_id and char_id != cached_char_id:
            return {"fd_available": False, "fd_action_id": action_id, "fd_char_id": char_id}

        timing = None
        if isinstance(family_session, dict):
            variants = (entry.get("variants") or {}).get(action_id) or []
            if isinstance(variants, list) and variants:
                ranked = [
                    (_family_variant_rank(candidate, family_session), candidate)
                    for candidate in variants
                    if isinstance(candidate, dict)
                ]
                ranked = [item for item in ranked if item[0] > (0, 0, 0, 0)]
                if ranked:
                    ranked.sort(key=lambda item: item[0], reverse=True)
                    best_rank, best_candidate = ranked[0]
                    # Require the same family and strength (or an exact group)
                    # before overriding the compatibility action map. This
                    # prevents an unrelated family annotation from hijacking a
                    # reused action ID.
                    if best_rank[0] or (best_rank[1] and best_rank[2]):
                        timing = best_candidate

        if not isinstance(timing, dict):
            timing = (entry.get("actions") or {}).get(action_id)
        if not isinstance(timing, dict):
            return {"fd_available": False, "fd_action_id": action_id, "fd_char_id": char_id or cached_char_id}
        resolved = dict(timing)
        resolved["fd_available"] = True
        resolved["fd_action_id"] = action_id
        resolved["fd_char_id"] = char_id or cached_char_id
        return resolved

    def _arm_realtime_hs_move(
        self,
        slot_label: str,
        action_id: int,
        hitstun: int,
        move_label: str,
        *,
        point_active: bool = False,
        action_frame: int = 0,
        active_windows: list | tuple | None = None,
        recovery: int = 0,
    ) -> None:
        """Cache attacker move identity and timing for a later native contact."""
        try:
            hitstun = max(0, int(hitstun or 0))
            action_id = int(action_id or 0) & 0x7FFF
        except Exception:
            return
        if action_id <= 0:
            return
        slot = str(slot_label or "")
        if not slot:
            return
        team = "P1" if slot.startswith("P1") else ("P2" if slot.startswith("P2") else "")
        if not team:
            return

        timing_windows: list[list[int]] = []
        for pair in (active_windows or []):
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            try:
                start_i = max(0, int(pair[0]))
                end_i = max(start_i, int(pair[1]))
            except Exception:
                continue
            timing_windows.append([start_i, end_i])
        try:
            action_frame_i = max(0, int(action_frame or 0))
        except Exception:
            action_frame_i = 0
        try:
            recovery_i = max(0, int(recovery or 0))
        except Exception:
            recovery_i = 0
        move_end_frame = 0
        if timing_windows:
            move_end_frame = max(int(pair[1]) for pair in timing_windows) + recovery_i

        armed = {
            "slot": slot,
            "team": team,
            "action_id": action_id,
            "hitstun": hitstun,
            "move_label": str(move_label or ""),
            "point_active": bool(point_active),
            "action_frame": action_frame_i,
            "active_windows": timing_windows,
            "recovery": recovery_i,
            "move_end_frame": move_end_frame,
            "armed_ns": time.monotonic_ns(),
        }
        with self._input_bridge_condition:
            self._hs_armed_by_slot[slot] = armed

    def _realtime_hs_arm_for_team(self, team: str, now_ns: int) -> dict | None:
        """Pick the move that was already armed when the opponent was hit."""
        candidates: list[tuple[int, int, int, dict]] = []
        for slot, armed in self._hs_armed_by_slot.items():
            if str(armed.get("team") or "") != team:
                continue
            armed_ns = int(armed.get("armed_ns", 0) or 0)
            # Keep a short projectile/assist grace window after the owner leaves
            # the action. A newer armed move always wins.
            if armed_ns <= 0 or now_ns - armed_ns > 2_000_000_000:
                continue
            latest = self._realtime_latest_sample_by_slot.get(slot) or {}
            action_matches = int(latest.get("action_id", 0) or 0) == int(armed.get("action_id", 0) or 0)
            candidates.append((1 if action_matches else 0, 1 if bool(armed.get("point_active")) else 0, armed_ns, armed))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return dict(candidates[0][3])

    def _emit_realtime_hs_contact(self, victim_slot: str, sample: dict, previous_sample: dict | None) -> None:
        """Mint/replace one HS clock from authoritative victim-native edges.

        A contact is not allowed to depend on one lucky HP sample.  Fast cancel
        chains can resolve the new +0x1210/+0x1220 value on the packet before or
        after HP subtraction, and a new hit can overwrite a still-nonzero timer
        with an equal/shorter value.  Treat HP loss, a fresh impact-freeze edge,
        and a native stun restart/increase as contact evidence.  Every confirmed
        contact receives a new generation, so the HUD hard-resets immediately.
        """
        if not isinstance(previous_sample, dict):
            return

        try:
            previous_hp = int(previous_sample.get("current_hp", 0) or 0)
            current_hp = int(sample.get("current_hp", 0) or 0)
            previous_blockstun = max(0, int(previous_sample.get("blockstun_remaining", 0) or 0))
            current_blockstun = max(0, int(sample.get("blockstun_remaining", 0) or 0))
            previous_hitstun = max(0, int(previous_sample.get("hitstun_remaining", 0) or 0))
            current_hitstun = max(0, int(sample.get("hitstun_remaining", 0) or 0))
            previous_untech = max(0, int(previous_sample.get("untech_remaining", 0) or 0))
            current_untech = max(0, int(sample.get("untech_remaining", 0) or 0))
            previous_freeze = max(0, int(previous_sample.get("impact_freeze_remaining", 0) or 0))
            current_freeze = max(0, int(sample.get("impact_freeze_remaining", 0) or 0))
        except Exception:
            return

        victim_team = "P1" if victim_slot.startswith("P1") else ("P2" if victim_slot.startswith("P2") else "")
        if not victim_team:
            return
        attacker_team = "P2" if victim_team == "P1" else "P1"
        state = self._hs_bridge_teams.setdefault(attacker_team, {"latest": {}, "events": []})

        # Block/chip owns the BS lane.  It must also cancel a pending HP-only HS
        # edge so an all-guard string cannot later wake up as phantom hitstun.
        if current_blockstun > 0:
            self._hs_pending_contact_by_victim.pop(victim_slot, None)
            latest = state.get("latest") if isinstance(state.get("latest"), dict) else {}
            if str(latest.get("victim_slot") or "") == victim_slot:
                state["latest"] = {}
            return

        previous_native = previous_untech if previous_untech > 0 else previous_hitstun
        current_native = current_untech if current_untech > 0 else current_hitstun
        hp_dropped = bool(previous_hp > 0 and current_hp < previous_hp)
        timer_started = bool(current_native > 0 and previous_native <= 0)
        timer_rearmed = bool(current_native > 0 and current_native > previous_native)
        freeze_rearmed = bool(current_native > 0 and current_freeze > 0 and current_freeze > previous_freeze)
        pending = self._hs_pending_contact_by_victim.get(victim_slot)
        pending_contact = isinstance(pending, dict)

        # HP can change one packet before the native stun counter appears.  Do
        # not throw that contact away; complete it on the next stun edge.
        if hp_dropped and current_native <= 0:
            self._hs_pending_contact_by_victim[victim_slot] = {
                "sample_ns": int(sample.get("sample_ns", 0) or time.monotonic_ns()),
                "hp": current_hp,
            }
            return

        new_contact = bool(
            (hp_dropped and current_native > 0)
            or (pending_contact and current_native > 0)
            or timer_started
            or timer_rearmed
            or freeze_rearmed
        )
        if not new_contact:
            return
        self._hs_pending_contact_by_victim.pop(victim_slot, None)

        now_ns = int(sample.get("sample_ns", 0) or time.monotonic_ns())
        armed = self._realtime_hs_arm_for_team(attacker_team, now_ns) or {}
        attacker_slot = str(armed.get("slot") or "")
        decay_owner_slot = f"{attacker_team}-C1"
        attacker_sample = (
            self._realtime_latest_sample_by_slot.get(decay_owner_slot)
            or self._realtime_latest_sample_by_slot.get(attacker_slot)
            or {}
        )
        decay_counter = max(0, int(attacker_sample.get("decay_counter", 0) or 0))
        native = _native_hs_contact_values(sample, decay_counter // 4)
        if not native:
            return

        generation = int(self._hs_generation_by_team.get(attacker_team, 0) or 0) + 1
        self._hs_generation_by_team[attacker_team] = generation
        event = {
            "generation": generation,
            "sample_ns": now_ns,
            "attacker_team": attacker_team,
            "attacker_slot": attacker_slot,
            "victim_slot": victim_slot,
            "action_id": int(armed.get("action_id", 0) or 0),
            "move_label": str(armed.get("move_label") or ""),
            "clock_source": str(native["clock_source"]),
            "native_hitstun": int(native["native_hitstun"]),
            "native_untech": int(native["native_untech"]),
            "target": int(native["target"]),
            "decay_frames": int(native["decay_frames"]),
            "raw_estimate": int(native["raw_estimate"]),
            "native": True,
        }
        state["latest"] = dict(event)
        events = state.setdefault("events", [])
        events.append(dict(event))
        del events[:-32]


    def _emit_realtime_blockstun_contact(self, victim_slot: str, sample: dict, previous_sample: dict | None) -> None:
        """Mint one native blockstun clock when a blocked contact resolves.

        +0x1204 is already the game's resolved live blockstun countdown. A new
        blocked contact is identified by the counter starting/increasing, or by
        the native impact-freeze timer being re-armed while blockstun is active.
        The latter catches blockstrings where the new move overwrites a still-
        nonzero blockstun counter with an equal or shorter value.
        """
        if not isinstance(previous_sample, dict):
            return
        try:
            previous_blockstun = max(0, int(previous_sample.get("blockstun_remaining", 0) or 0))
            current_blockstun = max(0, int(sample.get("blockstun_remaining", 0) or 0))
            previous_freeze = max(0, int(previous_sample.get("impact_freeze_remaining", 0) or 0))
            current_freeze = max(0, int(sample.get("impact_freeze_remaining", 0) or 0))
        except Exception:
            return
        victim_team = "P1" if victim_slot.startswith("P1") else ("P2" if victim_slot.startswith("P2") else "")
        if not victim_team:
            return
        attacker_team = "P2" if victim_team == "P1" else "P1"
        state = self._bs_bridge_teams.setdefault(attacker_team, {"latest": {}, "events": []})

        # Clock lifetime is game-state driven, never wall-clock driven. When
        # Dolphin is paused on a nonzero +0x1204 value there are no new samples,
        # so the current contact must remain latched indefinitely. Clear it only
        # when the game itself advances +0x1204 to zero.
        if current_blockstun <= 0:
            latest = state.get("latest") if isinstance(state.get("latest"), dict) else {}
            if str(latest.get("victim_slot") or "") == victim_slot:
                state["latest"] = {}
            return

        new_contact = bool(
            previous_blockstun <= 0
            or current_blockstun > previous_blockstun
            or (current_freeze > 0 and current_freeze > previous_freeze)
        )
        if not new_contact:
            return

        now_ns = int(sample.get("sample_ns", 0) or time.monotonic_ns())
        armed = self._realtime_hs_arm_for_team(attacker_team, now_ns) or {}

        generation = int(self._bs_generation_by_team.get(attacker_team, 0) or 0) + 1
        self._bs_generation_by_team[attacker_team] = generation
        attacker_slot = str(armed.get("slot") or "")
        attacker_sample = self._realtime_latest_sample_by_slot.get(attacker_slot) or {}
        try:
            attacker_contact_frame = max(0, int(attacker_sample.get("action_frame", armed.get("action_frame", 0)) or 0))
        except Exception:
            attacker_contact_frame = max(0, int(armed.get("action_frame", 0) or 0))
        event = {
            "generation": generation,
            "sample_ns": now_ns,
            "attacker_team": attacker_team,
            "attacker_slot": attacker_slot,
            "victim_slot": victim_slot,
            "action_id": int(armed.get("action_id", 0) or 0),
            "move_label": str(armed.get("move_label") or ""),
            "attacker_action_frame": attacker_contact_frame,
            "attacker_move_end_frame": max(0, int(armed.get("move_end_frame", 0) or 0)),
            "attacker_recovery": max(0, int(armed.get("recovery", 0) or 0)),
            "attacker_active_windows": list(armed.get("active_windows") or []),
            "target": current_blockstun,
            "native_blockstun": current_blockstun,
            "impact_freeze": current_freeze,
            "native": True,
        }
        state["latest"] = dict(event)
        events = state.setdefault("events", [])
        events.append(dict(event))
        del events[:-32]

    def _on_realtime_input_sample(self, slot_label: str, sample: dict) -> None:
        """Queue only latency-sensitive input transitions for the HUD sidecar."""
        if not isinstance(sample, dict):
            return
        slot = str(slot_label or sample.get("slot") or "")
        if not slot:
            return
        # The high-frequency action lane is the sole producer for MOVES.
        # Handle it on a deliberately tiny path and return immediately: action
        # history must never block behind frame-data, stun, input, or profile
        # work. Each event is one native action/state transition.
        if bool(sample.get("fast_action_edge", False)):
            try:
                current_base = int(sample.get("base", 0) or 0)
                current_char_id = int(sample.get("char_id", 0) or 0)
                current_action_id = int(sample.get("action_id", 0) or 0) & 0x7FFF
                current_action_frame = max(0, int(sample.get("action_frame", 0) or 0))
                sample_ns = int(sample.get("sample_ns", 0) or time.monotonic_ns())
            except Exception:
                return
            move_bridge_slots = None
            with self._input_bridge_condition:
                state = self._input_bridge_slots.setdefault(
                    slot,
                    {"latest": {}, "samples": [], "actions": [], "move_log": [], "stun_samples": []},
                )
                changed = False
                if bool(sample.get("move_history_reset", False)):
                    state["move_log"] = []
                    changed = True
                if bool(sample.get("move_instance", False)) and _move_history_is_observable(current_action_id):
                    self._move_history_log_sequence += 1
                    move_log = state.setdefault("move_log", [])
                    move_log.append({
                        "log_seq": int(self._move_history_log_sequence),
                        "sample_ns": sample_ns,
                        "base": current_base,
                        "char_id": current_char_id,
                        "action_id": current_action_id,
                        "action_frame": current_action_frame,
                    })
                    # The HUD only renders five, but keep a little headroom so
                    # a renderer stall cannot erase a short cancel chain.
                    del move_log[:-32]
                    changed = True
                if changed:
                    move_bridge_slots = {
                        key: {
                            "base": int((value.get("latest") or {}).get("base", 0) or (value.get("move_log") or [{}])[-1].get("base", 0) or 0),
                            "move_log": [dict(item) for item in value.get("move_log", ())[-16:]],
                        }
                        for key, value in self._input_bridge_slots.items()
                    }
                self._input_bridge_condition.notify_all()
            if move_bridge_slots is not None:
                self._write_move_bridge_now(move_bridge_slots)
            return

        try:
            held = int(sample.get("held", 0) or 0) & 0xFFFF
            pressed = int(sample.get("pressed", 0) or 0) & 0xFFFF
            released = int(sample.get("released", 0) or 0) & 0xFFFF
            seq = int(sample.get("seq", 0) or 0)
            sample_ns = int(sample.get("sample_ns", 0) or time.monotonic_ns())
        except Exception:
            return

        # MOVES is owned exclusively by the fast action-edge lane above.
        # The regular combat sampler must never reconstruct or commit move
        # history, otherwise the two samplers can race and drop/duplicate edges.
        move_bridge_changed = False
        move_bridge_slots = None

        previous_sample = self._realtime_latest_sample_by_slot.get(slot)
        try:
            previous_action_id = int((previous_sample or {}).get("action_id", -1)) & 0x7FFF
        except Exception:
            previous_action_id = -1
        try:
            previous_action_frame = max(0, int((previous_sample or {}).get("action_frame", -1)))
        except Exception:
            previous_action_frame = -1
        try:
            current_action_id = int(sample.get("action_id", 0) or 0) & 0x7FFF
        except Exception:
            current_action_id = 0
        try:
            current_action_frame = max(0, int(sample.get("action_frame", 0) or 0))
        except Exception:
            current_action_frame = 0
        action_changed = previous_sample is None or current_action_id != previous_action_id
        action_frame_changed = (
            previous_sample is None
            or action_changed
            or current_action_frame != previous_action_frame
        )

        try:
            current_char_id = int(sample.get("char_id", 0) or 0)
        except Exception:
            current_char_id = 0
        # The action edge itself selects the timing row. No scanner/profile
        # work occurs here; this is an O(1) lookup into the prebuilt cache.
        # Tatsu keeps the L/M/H command owner across its shared internal
        # Start/Spin/End action IDs so repeated M/H phases do not fall back to
        # the first (usually L) duplicate timing row.
        family_session = self._realtime_tatsu_session_for(
            slot, current_action_id, sample_ns
        )
        fd_timing = self._realtime_fd_timing_for(
            slot, current_char_id, current_action_id, family_session
        )
        self._realtime_latest_sample_by_slot[slot] = dict(sample)

        previous_held = self._input_bridge_last_held.get(slot)
        input_changed = previous_held is None or held != previous_held or bool(pressed) or bool(released)
        self._input_bridge_last_held[slot] = held

        combat_key = (
            int(sample.get("blockstun_remaining", 0) or 0),
            int(sample.get("hitstun_remaining", 0) or 0),
            int(sample.get("untech_remaining", 0) or 0),
            int(sample.get("impact_freeze_remaining", 0) or 0),
            int(sample.get("fighter_combo_count", 0) or 0),
            int(sample.get("decay_counter", 0) or 0),
            int(sample.get("state_flags_6c", 0) or 0) & 0xFFFFFFFF,
            int(sample.get("current_hp", 0) or 0),
            int(sample.get("aux_hp", 0) or 0),
            int(sample.get("current_meter", 0) or 0),
            int(sample.get("action_id", 0) or 0),
            bool(sample.get("point_active", False)),
        )
        combat_changed = self._input_bridge_last_combat.get(slot) != combat_key
        self._input_bridge_last_combat[slot] = combat_key
        # Preserve every native stun edge in a tiny ring. The HUD process renders
        # at 60 Hz while this producer can land on the opposite side of the same
        # 16.7 ms boundary; a latest-value-only temp file can therefore collapse
        # two adjacent 1F decrements into one visual jump. Keeping the native
        # edge sequence lets the renderer consume exactly one game-frame value
        # per displayed frame without inventing wall-clock countdowns.
        stun_changed = bool(
            previous_sample is None
            or int(sample.get("blockstun_remaining", 0) or 0) != int((previous_sample or {}).get("blockstun_remaining", 0) or 0)
            or int(sample.get("hitstun_remaining", 0) or 0) != int((previous_sample or {}).get("hitstun_remaining", 0) or 0)
            or int(sample.get("untech_remaining", 0) or 0) != int((previous_sample or {}).get("untech_remaining", 0) or 0)
            or int(sample.get("impact_freeze_remaining", 0) or 0) != int((previous_sample or {}).get("impact_freeze_remaining", 0) or 0)
        )
        # Action-frame-only samples are critical for 60 FPS HUD animation.
        # The realtime sampler publishes them even when inputs, HP, stun, etc.
        # are unchanged; keep those samples alive on the tiny temp-file combat
        # transport so the overlay sees one exact native action frame per tick.
        if not input_changed and not combat_changed and not action_frame_changed:
            return

        event = {
            "seq": seq,
            "held": held,
            "pressed": pressed,
            "released": released,
            "sample_ns": sample_ns,
            "base": int(sample.get("base", 0) or 0),
            "char_id": int(sample.get("char_id", 0) or 0),
            "current_hp": int(sample.get("current_hp", 0) or 0),
            "aux_hp": int(sample.get("aux_hp", 0) or 0),
            "current_meter": int(sample.get("current_meter", 0) or 0),
            "action_id": int(sample.get("action_id", 0) or 0),
            "action_frame": max(0, int(sample.get("action_frame", 0) or 0)),
            "blockstun_remaining": max(0, int(sample.get("blockstun_remaining", 0) or 0)),
            "hitstun_remaining": max(0, int(sample.get("hitstun_remaining", 0) or 0)),
            "untech_remaining": max(0, int(sample.get("untech_remaining", 0) or 0)),
            "impact_freeze_remaining": max(0, int(sample.get("impact_freeze_remaining", 0) or 0)),
            "fighter_combo_count": max(0, int(sample.get("fighter_combo_count", 0) or 0)),
            "decay_counter": max(0, int(sample.get("decay_counter", 0) or 0)),
            "state_flags_6c": int(sample.get("state_flags_6c", 0) or 0) & 0xFFFFFFFF,
            "point_active": bool(sample.get("point_active", False)),
        }
        event.update(fd_timing)
        with self._input_bridge_condition:
            if combat_changed:
                self._emit_realtime_hs_contact(slot, sample, previous_sample)
                self._emit_realtime_blockstun_contact(slot, sample, previous_sample)
            state = self._input_bridge_slots.setdefault(
                slot,
                {"latest": {}, "samples": [], "actions": [], "move_log": [], "stun_samples": []},
            )
            state["latest"] = dict(event)
            if stun_changed:
                stun_samples = state.setdefault("stun_samples", [])
                stun_samples.append({
                    "sample_ns": sample_ns,
                    "base": int(sample.get("base", 0) or 0),
                    "action_id": current_action_id,
                    "action_frame": current_action_frame,
                    "blockstun_remaining": max(0, int(sample.get("blockstun_remaining", 0) or 0)),
                    "hitstun_remaining": max(0, int(sample.get("hitstun_remaining", 0) or 0)),
                    "untech_remaining": max(0, int(sample.get("untech_remaining", 0) or 0)),
                    "impact_freeze_remaining": max(0, int(sample.get("impact_freeze_remaining", 0) or 0)),
                })
                del stun_samples[:-96]
            if action_changed:
                actions = state.setdefault("actions", [])
                action_event = {
                    "seq": seq,
                    "sample_ns": sample_ns,
                    "base": int(sample.get("base", 0) or 0),
                    "char_id": int(sample.get("char_id", 0) or 0),
                    "action_id": current_action_id,
                    "action_frame": max(0, int(sample.get("action_frame", 0) or 0)),
                    "state_flags_6c": int(sample.get("state_flags_6c", 0) or 0) & 0xFFFFFFFF,
                }
                action_event.update(fd_timing)
                actions.append(action_event)
                del actions[:-24]
            if input_changed:
                samples = state.setdefault("samples", [])
                # The input-history sidecar only consumes these five fields.
                # Keeping combat/timing payloads in every ring entry made each
                # input edge rewrite a much larger JSON file than necessary.
                samples.append({key: event[key] for key in
                                ("seq", "held", "pressed", "released", "sample_ns")})
                del samples[:-96]
                self._input_bridge_dirty = True
            # The stun/combat sidecar is intentionally tiny and lives in the OS
            # temp directory. Feed it native action-frame edges as well as stun
            # changes so frame-data geometry can update at the game's 60 Hz
            # cadence without bloating or rewriting the input-history JSON.
            if combat_changed or action_frame_changed:
                self._stun_bridge_dirty = True
            if move_bridge_changed:
                # Copy only what the MOVES renderer needs.  A five-chip HUD does
                # not need to serialize HP/stun/input history before the new move
                # can appear.  Keep a few extra committed entries for renderer
                # stalls and match/tag rebasing.
                move_bridge_slots = {
                    key: {
                        "base": int((value.get("latest") or {}).get("base", 0) or 0),
                        "move_log": [dict(item) for item in value.get("move_log", ())[-24:]],
                    }
                    for key, value in self._input_bridge_slots.items()
                }
            self._input_bridge_condition.notify_all()

        if move_bridge_slots is not None:
            self._write_move_bridge_now(move_bridge_slots)

    def _write_move_bridge_now(self, slots: dict) -> None:
        """Publish committed attack edges immediately on the sampler callback.

        This file changes only on move-instance edges (or fighter swaps), so a
        synchronous temp-file replace is tiny and avoids the scheduling/JSON
        backlog of the high-frequency stun transport.  The overlay sees the
        move on its very next draw.
        """
        payload = {
            "written_mono_ns": time.monotonic_ns(),
            "slots": slots if isinstance(slots, dict) else {},
        }
        try:
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            tmp = f"{HUD_REALTIME_MOVES_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(serialized)
            os.replace(tmp, HUD_REALTIME_MOVES_FILE)
        except Exception:
            pass

    def _input_bridge_writer_loop(self) -> None:
        """Write input edges only. Combat clocks use the separate tiny stun IPC."""
        while True:
            with self._input_bridge_condition:
                while not self._input_bridge_dirty and not self._input_bridge_stop:
                    self._input_bridge_condition.wait(timeout=0.25)
                if self._input_bridge_stop and not self._input_bridge_dirty:
                    return
                slots = {
                    slot: {
                        "latest": dict(state.get("latest") or {}),
                        "samples": [dict(item) for item in state.get("samples", ())],
                    }
                    for slot, state in self._input_bridge_slots.items()
                }
                self._input_bridge_dirty = False

            payload = {"written_wall_ns": time.time_ns(), "slots": slots}
            try:
                serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                os.makedirs(os.path.dirname(HUD_REALTIME_INPUT_FILE), exist_ok=True)
                tmp = f"{HUD_REALTIME_INPUT_FILE}.tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                os.replace(tmp, HUD_REALTIME_INPUT_FILE)
                self._last_realtime_input_serialized = serialized
            except Exception:
                pass

    @staticmethod
    def _stun_slot_latest(state: dict) -> dict:
        latest = dict(state.get("latest") or {}) if isinstance(state, dict) else {}
        if not latest:
            return {}
        # Keep the realtime stun transport intentionally tiny.
        keys = (
            "seq", "sample_ns", "base", "char_id", "current_hp", "aux_hp", "current_meter", "action_id", "action_frame",
            "blockstun_remaining", "hitstun_remaining", "untech_remaining",
            "impact_freeze_remaining", "fighter_combo_count", "decay_counter",
            "state_flags_6c", "point_active",
            "fd_available", "fd_action_id", "fd_char_id", "fd_startup",
            "fd_active_start", "fd_active_end", "fd_active_windows", "fd_recovery",
        )
        return {key: latest.get(key) for key in keys if key in latest}

    def _stun_bridge_writer_loop(self) -> None:
        """Write native stun counters independently of input-history JSON.

        Blockstun is short enough that a delayed full input-history rewrite can
        collapse several 1F native decrements. This transport contains only the
        current combat counters/contact tokens and lives in the OS temp dir.
        """
        while True:
            with self._input_bridge_condition:
                while not self._stun_bridge_dirty and not self._stun_bridge_stop:
                    self._input_bridge_condition.wait(timeout=0.25)
                if self._stun_bridge_stop and not self._stun_bridge_dirty:
                    return
                slots = {
                    slot: {
                        "latest": self._stun_slot_latest(state),
                        "actions": [dict(item) for item in state.get("actions", ())[-24:]],
                        "move_log": [dict(item) for item in state.get("move_log", ())[-96:]],
                        "stun_samples": [dict(item) for item in state.get("stun_samples", ())[-64:]],
                    }
                    for slot, state in self._input_bridge_slots.items()
                }
                hs_teams = {
                    team: {
                        "latest": dict(state.get("latest") or {}),
                        "events": [dict(item) for item in state.get("events", ())[-8:]],
                    }
                    for team, state in self._hs_bridge_teams.items()
                }
                bs_teams = {
                    team: {
                        "latest": dict(state.get("latest") or {}),
                        "events": [dict(item) for item in state.get("events", ())[-8:]],
                    }
                    for team, state in self._bs_bridge_teams.items()
                }
                self._stun_bridge_dirty = False

            payload = {
                "written_wall_ns": time.time_ns(),
                "slots": slots,
                "hs_teams": hs_teams,
                "bs_teams": bs_teams,
            }
            try:
                serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                tmp = f"{HUD_REALTIME_STUN_FILE}.tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                os.replace(tmp, HUD_REALTIME_STUN_FILE)
                self._last_realtime_stun_serialized = serialized
            except Exception:
                pass

    def _set_input_sampler_targets(self, render_snap_by_slot: dict) -> None:
        self._realtime_sampler.set_targets(render_snap_by_slot)

    def _input_snapshot_for_slot(
        self,
        slot_label: str,
        base: int,
    ) -> tuple[dict, list[dict]]:
        return self._realtime_sampler.snapshot_for_slot(slot_label, base)

    def _queue_payload(self, payload: dict) -> None:
        with self._payload_condition:
            self._pending_payload = payload
            self._payload_condition.notify()

    def _payload_writer_loop(self) -> None:
        while True:
            with self._payload_condition:
                while self._pending_payload is None and not self._payload_writer_stop:
                    self._payload_condition.wait(timeout=0.25)
                if self._payload_writer_stop and self._pending_payload is None:
                    return
                payload = self._pending_payload
                self._pending_payload = None
            try:
                serialized = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
                if serialized == self._last_serialized and os.path.isfile(HUD_OVERLAY_DATA_FILE):
                    continue
                os.makedirs(os.path.dirname(HUD_OVERLAY_DATA_FILE), exist_ok=True)
                tmp = f"{HUD_OVERLAY_DATA_FILE}.tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                os.replace(tmp, HUD_OVERLAY_DATA_FILE)
                self._last_serialized = serialized
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    def prime_input_sampler_targets(self, render_snap_by_slot: dict) -> None:
        """Point the 240 Hz input sampler at the current live fighter objects."""
        self._set_input_sampler_targets(render_snap_by_slot)

    def add_input_sample_listener(self, listener) -> None:
        self._realtime_sampler.add_listener(listener)

    def remove_input_sample_listener(self, listener) -> None:
        self._realtime_sampler.remove_listener(listener)

    def mission_input_bundle(self, slot_label: str, fighter_base: int = 0) -> tuple[dict, list[dict]]:
        """Return the latest packet plus the sampler's ordered edge queue."""
        return self._input_snapshot_for_slot(str(slot_label), int(fighter_base or 0))

    def write_data(
        self,
        render_snap_by_slot: dict,
        last_scan_normals,
        mission_mgr: "MissionManager",
        punish_overlay: dict | None = None,
        timing_payload: dict | None = None,
    ) -> None:
        """
        Build and write hud_overlay_data.json from the current frame's
        fighter snapshots, scan-normals data, and mission state.
        """
        payload: dict = {}
        self._set_input_sampler_targets(render_snap_by_slot)
        # Index every already-scanned move before processing the current GUI
        # snapshot.  The realtime action listener can now arm FD on frame 0/1
        # instead of waiting for cur_anim/profile enrichment to catch up.
        self._refresh_realtime_fd_timing(last_scan_normals)

        # Guarantee Mission V2 receives the same immutable realtime stream as
        # the HUD. This removes any dependency on main.py/launcher wiring and
        # prevents a silent fallback to the old frame-driven mission evaluator.
        if self._mission_event_manager is not mission_mgr:
            try:
                mission_mgr.set_event_provider(self._mission_event_stream.events_since)
                self._mission_event_manager = mission_mgr
            except Exception:
                pass

        mission_active_slot = mission_mgr.active_slot

        # Compute VAR state once for the active mission slot
        mission_var: dict = {}
        if mission_active_slot:
            try:
                mission_var = mission_mgr.var_state(mission_active_slot, render_snap_by_slot)
            except Exception:
                mission_var = {}

        for slot_label, snap in render_snap_by_slot.items():
            # The main loop already produced the authoritative final label.
            # Do not re-read the profiler or rebuild the name in this sink.
            snap = dict(snap or {})
            cur_anim = snap.get("attA") or snap.get("attB")
            mv_label = snap.get("mv_label")
            mv_label_display = (
                snap.get("final_move_label")
                or snap.get("mv_label_display")
                or mv_label
            )
            startup = None
            active_start = None
            active_end = None
            active_windows = []
            recovery = None
            attack_property = None
            attack_property_b = None
            attack_property_label = ""
            matched_move = None
            native_definition = None
            input_packet, input_samples = self._input_snapshot_for_slot(
                slot_label,
                int(snap.get("base") or 0),
            )
            input_held = int(input_packet.get("held", 0) or 0) & 0xFFFF
            input_pressed = int(input_packet.get("pressed", 0) or 0) & 0xFFFF
            input_released = int(input_packet.get("released", 0) or 0) & 0xFFFF

            slot_tree_row = None
            if last_scan_normals and cur_anim is not None:
                for slot_data in last_scan_normals:
                    if slot_data.get("slot_label") == slot_label:
                        slot_tree_row = slot_data
                        candidates = [
                            mv for mv in slot_data.get("moves", [])
                            if isinstance(mv, dict) and mv.get("id") == cur_anim
                        ]
                        if candidates:
                            matched_move = max(candidates, key=_attack_property_move_quality)
                            startup = matched_move.get("startup")
                            active_start = matched_move.get("active_start")
                            active_end = matched_move.get("active_end")
                            recovery = matched_move.get("runtime_observed_recovery")
                            if recovery is None:
                                recovery = matched_move.get("recovery")

                            # Preserve every authored/observed active segment so the
                            # HUD bar follows one complete move instead of a single
                            # hitbox radius.  The primary window is always first.
                            def _append_window(start, end):
                                try:
                                    start_i = int(start)
                                    end_i = int(end)
                                except (TypeError, ValueError):
                                    return
                                if start_i < 0 or end_i < start_i:
                                    return
                                pair = [start_i, end_i]
                                if pair not in active_windows:
                                    active_windows.append(pair)

                            _append_window(active_start, active_end)
                            for segment in (matched_move.get("hit_segments") or []):
                                if isinstance(segment, dict):
                                    _append_window(segment.get("active_start"), segment.get("active_end"))
                            active_windows.sort(key=lambda pair: (pair[0], pair[1]))
                        break

            # Cache move identity as optional metadata for the realtime contact
            # event. The duration itself comes from the victim's native clocks.
            if isinstance(matched_move, dict):
                self._arm_realtime_hs_move(
                    slot_label,
                    int(cur_anim or input_packet.get("action_id", 0) or 0),
                    int(matched_move.get("hitstun") or 0),
                    str(mv_label_display or mv_label or matched_move.get("pretty_name") or matched_move.get("move_name") or ""),
                    point_active=bool(input_packet.get("point_active", snap.get("damage_point_active", False))),
                    action_frame=int(input_packet.get("action_frame", snap.get("realtime_action_frame", 0)) or 0),
                    active_windows=active_windows,
                    recovery=int(recovery or 0),
                )

            # Attack Property is native-only. Resolve the current action from
            # the fighter's character table and never fill this card from the
            # frame-data profile or scanner tree.
            if cur_anim is not None:
                try:
                    tree_root = int((slot_tree_row or {}).get("chr_tbl_abs") or 0)
                except Exception:
                    tree_root = 0
                native_definition = resolve_live_attack_definition(
                    int(snap.get("base") or 0),
                    int(cur_anim),
                    chr_tbl_abs=tree_root or None,
                )
                if native_definition.get("status") == "OK":
                    attack_property = native_definition.get("property_a")
                    attack_property_b = native_definition.get("property_b")
                    attack_property_label = _overlay_attack_property_label(attack_property)

            partner_slot = mission_var.get("partner_slot")

            slot_payload = {
                "base":                   int(snap.get("base") or 0),
                "name":                   snap.get("name"),
                "cur":                    snap.get("cur"),
                "max":                    snap.get("max"),
                "meter":                  snap.get("meter"),
                # Authored stun is a zero-latency contact-side hint for the HS
                # clock. The realtime native timer remains authoritative once
                # it appears, but we do not wait for it before showing/draining.
                "move_hitstun":           matched_move.get("hitstun") if isinstance(matched_move, dict) else None,
                "mv_id_display":          cur_anim,
                "mv_id_label_display":    snap.get("mv_id_label_display"),
                "mv_label":               mv_label,
                "mv_label_display":       mv_label_display,
                "final_move_label":       mv_label_display,
                "mv_label_base":          snap.get("mv_label_base") or snap.get("mv_label"),
                "mv_label_aliases":       list(snap.get("mv_label_aliases") or []),
                "move_level":             snap.get("move_level"),
                "move_level_total":       snap.get("move_level_total"),
                "move_level_label":       snap.get("move_level_label"),
                "move_level_source":      snap.get("move_level_source"),
                "move_level_confidence":  snap.get("move_level_confidence"),
                "move_level_projectile_id": snap.get("move_level_projectile_id"),
                "profile_history_label":    snap.get("profile_history_label"),
                "profile_history_action_id": snap.get("profile_history_action_id"),
                "profile_history_projectile_id": snap.get("profile_history_projectile_id"),
                "profile_history_static_addr": snap.get("profile_history_static_addr"),
                "profile_history_age":      snap.get("profile_history_age"),
                "profile_history_seen_wall_time": snap.get("profile_history_seen_wall_time"),
                "profile_history_token":    snap.get("profile_history_token"),
                "profile_live_label":       snap.get("profile_live_label"),
                "profile_resolved_label":   snap.get("profile_resolved_label"),
                "profile_live_active":      bool(snap.get("profile_live_active", False)),
                "move_label_source":        snap.get("move_label_source"),
                # Preserve scanner/profile flags for the diagnostic MOVES row.
                # The native realtime action chooses *when* the event appears;
                # these fields only enrich that already-created history chip.
                "move_metadata_action_id":  int(cur_anim or 0),
                "move_kind":                matched_move.get("kind") if isinstance(matched_move, dict) else None,
                "move_invuln":              matched_move.get("invuln") if isinstance(matched_move, dict) else None,
                "move_protection":          (matched_move.get("armor_probe") or matched_move.get("guard_point") or matched_move.get("armor")) if isinstance(matched_move, dict) else None,
                "move_cancel_probe":        matched_move.get("cancel_probe") if isinstance(matched_move, dict) else None,
                "move_custom_cancel_window": matched_move.get("custom_cancel_window") if isinstance(matched_move, dict) else None,
                "move_baroque_probe":       matched_move.get("baroque_probe") if isinstance(matched_move, dict) else None,
                "move_jump_cancel":         bool(
                    isinstance(matched_move, dict) and (
                        matched_move.get("jump_cancel")
                        or matched_move.get("jump_cancelable")
                        or matched_move.get("jump_cancellable")
                        or matched_move.get("jump_cancel_window")
                    )
                ),
                "profile_label_override":   snap.get("profile_label_override", False),
                "baroque_ready_local":    snap.get("baroque_ready_local", False),
                "baroque_red_pct_max":    snap.get("baroque_red_pct_max", 0.0),
                "baroque_local_hp32":     snap.get("baroque_local_hp32"),
                "baroque_local_pool32":   snap.get("baroque_local_pool32"),
                "baroque_cancel_raw":     snap.get("baroque_cancel_raw", False),
                "baroque_cancel_latched": snap.get("baroque_cancel_latched", False),
                "baroque_cancel_frames":  snap.get("baroque_cancel_latch_frames", 0),
                "input_previous":         input_packet.get("previous", 0),
                "input_held":             input_held,
                "input_pressed":          input_pressed,
                "input_released":         input_released,
                "input_samples":          input_samples,
                "input_text":             input_packet.get("held_text", "5"),
                "input_pressed_text":     input_packet.get("pressed_text", "none"),
                "input_released_text":    input_packet.get("released_text", "none"),
                "realtime_sample_ns":     int(input_packet.get("sample_ns", 0) or 0),
                "realtime_current_hp":    int(input_packet.get("current_hp", snap.get("cur") or 0) or 0),
                "realtime_action_id":     int(input_packet.get("action_id", cur_anim or 0) or 0),
                "realtime_action_frame":  max(0, int(input_packet.get("action_frame", 0) or 0)),
                "realtime_impact_freeze_remaining": max(0, int(input_packet.get("impact_freeze_remaining", 0) or 0)),
                "realtime_hitstun_remaining": max(0, int(input_packet.get("hitstun_remaining", 0) or 0)),
                "realtime_untech_remaining": max(0, int(input_packet.get("untech_remaining", 0) or 0)),
                "realtime_fighter_combo_count": max(0, int(input_packet.get("fighter_combo_count", 0) or 0)),
                "realtime_decay_counter": max(0, int(input_packet.get("decay_counter", 0) or 0)),
                "realtime_state_flags_6c": int(input_packet.get("state_flags_6c", 0) or 0) & 0xFFFFFFFF,
                "realtime_point_active":  bool(input_packet.get("point_active", False)),
                # Live frame-data bar payload.  Runtime-observed recovery wins
                # over the static value above; action_frame comes from the
                # realtime input sampler and is independent of hitbox rendering.
                "startup":                startup,
                "active_start":           active_start,
                "active_end":             active_end,
                "active_windows":         active_windows,
                "recovery":               recovery,
                "attack_property":        attack_property,
                "attack_property_label":  attack_property_label,
                "attack_property_packet_count": snap.get("attack_property_packet_count", 0),
                "attack_property_live_actor": snap.get("attack_property_live_actor"),
                "attack_property_live_a": snap.get("attack_property_live_a"),
                "attack_property_live_b": snap.get("attack_property_live_b"),
                "attack_property_live_a_text": snap.get("attack_property_live_a_text", ""),
                "attack_property_live_b_text": snap.get("attack_property_live_b_text", ""),
                "attack_property_live_b_unknown": snap.get("attack_property_live_b_unknown"),
                "attack_property_live_status20": snap.get("attack_property_live_status20"),
                "attack_property_live_damage": snap.get("attack_property_live_damage"),
                "attack_property_live_phase_a": snap.get("attack_property_live_phase_a"),
                "attack_property_live_phase_b": snap.get("attack_property_live_phase_b"),
                "attack_property_live_victim_slot": snap.get("attack_property_live_victim_slot", ""),
                "attack_property_live_action_frame": snap.get("attack_property_live_action_frame"),
                "recoverable_hp":          snap.get("recoverable_hp", 0),
                "recoverable_ceiling":     snap.get("recoverable_ceiling", snap.get("aux")),
                "recoverable_pct_max":     snap.get("recoverable_pct_max", 0.0),
                "red_health_current":      snap.get("red_health_current", snap.get("cur")),
                "red_health_aux":          snap.get("red_health_aux", snap.get("aux")),
                "red_health_recoverable":  snap.get("red_health_recoverable", snap.get("recoverable_hp", 0)),
                "red_health_pct_max":      snap.get("red_health_pct_max", snap.get("recoverable_pct_max", 0.0)),
                "red_health_pending_current": snap.get("red_health_pending_current", 0),
                "red_health_pending_aux":  snap.get("red_health_pending_aux", 0),
                "red_health_heal_sync":    snap.get("red_health_heal_sync", 0),
                "red_health_point":        snap.get("red_health_point", False),
                "red_health_baroque":      snap.get("red_health_baroque", False),
                "red_health_red_spent":    snap.get("red_health_red_spent", 0),
                "red_health_last_event":   snap.get("red_health_last_event", ""),
                "red_health_last_current_delta": snap.get("red_health_last_current_delta", 0),
                "red_health_last_aux_delta": snap.get("red_health_last_aux_delta", 0),
                "red_health_last_red_delta": snap.get("red_health_last_red_delta", 0),
                "red_health_last_predicted": snap.get("red_health_last_predicted"),
                "red_health_last_match":   snap.get("red_health_last_match"),
                "red_health_last_attacker": snap.get("red_health_last_attacker", ""),
                "red_health_last_move":    snap.get("red_health_last_move", ""),
                "meter_profile_current":   snap.get("meter_profile_current", snap.get("meter")),
                "meter_profile_last_delta": snap.get("meter_profile_last_delta", 0),
                "meter_profile_last_kind": snap.get("meter_profile_last_kind", ""),
                "meter_profile_last_role": snap.get("meter_profile_last_role", ""),
                "meter_profile_last_source": snap.get("meter_profile_last_source", ""),
                "meter_profile_last_move": snap.get("meter_profile_last_move", ""),
                "meter_profile_last_predicted": snap.get("meter_profile_last_predicted"),
                "meter_profile_last_difference": snap.get("meter_profile_last_difference"),
                "meter_profile_last_match": snap.get("meter_profile_last_match"),
                "meter_profile_last_base_damage": snap.get("meter_profile_last_base_damage", 0),
                "meter_profile_last_property_a": snap.get("meter_profile_last_property_a", ""),
                "meter_profile_last_property_b": snap.get("meter_profile_last_property_b", ""),
                "mission_target":         slot_label == mission_active_slot,
                "mission_var_partner":    slot_label == partner_slot,
                "mission_wrong_ready":    bool(
                    mission_var.get("wrong_character_ready", False)
                    and slot_label == partner_slot
                ),
                "mission_var_ready":      bool(
                    mission_var.get("partner_airborne", False)
                    and slot_label == partner_slot
                ),
                "mission_varing":         bool(
                    mission_var.get("partner_varing", False)
                    and slot_label == partner_slot
                ),
                "timing_action_frame":     int(snap.get("timing_action_frame") or 0),
                "timing_blockstun":        int(snap.get("timing_blockstun") or 0),
                "timing_hitstun_total":    int(snap.get("timing_hitstun_total") or 0),
                "timing_hitstun_remaining": int(snap.get("timing_hitstun_remaining") or 0),
                "timing_hitstop_active":   int(snap.get("timing_hitstop_active") or 0),
                "timing_hitstop_pending":  int(snap.get("timing_hitstop_pending") or 0),
                "timing_hitstop":          int(snap.get("timing_hitstop") or 0),
            }

            # Preserve all attack-property annotations produced by the
            # background profiler. The earlier serializer copied only raw
            # actor fields, which dropped the move-definition active/source
            # flags and made the HUD show only the detected action.
            _bridge_attack_property_annotations(slot_payload, snap)

            # Native script is the authoritative Attack Property source.
            # Always overwrite any older pool/resolver/profile annotation for
            # the current action so this badge stays a clean native harvest.
            existing_attack_source = str(slot_payload.get("attack_property_display_source") or "")
            existing_attack_phases = [
                row for row in (slot_payload.get("attack_property_phases") or [])
                if isinstance(row, dict)
            ]
            projectile_only_profiler_result = bool(
                slot_payload.get("attack_property_projectiles")
                and not existing_attack_phases
                and existing_attack_source in {
                    "live_attack_actor", "live_attack_actor_latched",
                    "live_projectile_actor", "live_projectile_latched",
                }
            )
            if (
                not projectile_only_profiler_result
                and isinstance(native_definition, dict)
                and native_definition.get("status") == "OK"
                and _known_attack_property(attack_property)
            ):
                native_phases = [
                    dict(phase)
                    for phase in (native_definition.get("phases") or [])
                    if isinstance(phase, dict)
                ]
                projectile_rows = [
                    row for row in (slot_payload.get("attack_property_projectiles") or [])
                    if isinstance(row, dict)
                ]
                has_projectiles = bool(projectile_rows)
                has_live_projectiles = any(bool(row.get("projectile_live")) for row in projectile_rows)
                slot_payload.update({
                    "attack_property_display_active": True,
                    "attack_property_display_source": (
                        "native_script_and_live_attack_actor"
                        if has_live_projectiles
                        else ("native_script_and_last_attack_actor" if has_projectiles else "move_definition")
                    ),
                    "attack_property_packet_state": (
                        "CURRENT MOVE + LIVE ATTACK ACTOR"
                        if has_live_projectiles
                        else ("CURRENT MOVE + LAST ATTACK ACTOR" if has_projectiles else "CURRENT MOVE")
                    ),
                    "attack_property_packet_source": "move_definition",
                    "attack_property_packet_action_id": int(cur_anim or 0),
                    "attack_property_packet_action_name": str(mv_label_display or mv_label or ""),
                    "attack_property_live_actor": 0,
                    "attack_property_live_a": int(attack_property) & 0xFF,
                    "attack_property_live_b": int(attack_property_b or 0) & 0xFFFFFFFF,
                    "attack_property_definition_status": "OK",
                    "attack_property_definition_action_id": int(cur_anim or 0),
                    "attack_property_definition_action_source": "native_action_script",
                    "attack_property_phase_count": len(native_phases),
                    "attack_property_phases": native_phases,
                })

            payload[slot_label] = slot_payload

        annotate_damage_scaling_payload(payload, render_snap_by_slot)
        annotate_hitstun_scaling_payload(payload, render_snap_by_slot)
        payload["_punish_trainer"] = dict(punish_overlay or {})
        payload["_timing_engine"] = dict(timing_payload or {})

        self._queue_payload(payload)

    def check_proc(self) -> None:
        """Poll the subprocess handle; update active flag if it has exited."""
        if self._proc and self._proc.poll() is not None:
            self._proc = None
            self._active = False
    def close(self) -> None:
        self._realtime_sampler.remove_listener(self._on_realtime_input_sample)
        self._realtime_sampler.remove_listener(self._mission_event_stream.on_sample)
        if self._owns_realtime_sampler:
            self._realtime_sampler.close()
        with self._input_bridge_condition:
            self._input_bridge_stop = True
            self._stun_bridge_stop = True
            self._input_bridge_condition.notify_all()
        if self._input_bridge_thread.is_alive():
            self._input_bridge_thread.join(timeout=1.0)
        if self._stun_bridge_thread.is_alive():
            self._stun_bridge_thread.join(timeout=1.0)
        with self._payload_condition:
            self._payload_writer_stop = True
            self._payload_condition.notify_all()
        if self._payload_writer_thread.is_alive():
            self._payload_writer_thread.join(timeout=1.0)

