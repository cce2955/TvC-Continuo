#!/usr/bin/env python3

from __future__ import annotations

import ctypes
import math
import struct
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Optional
import os
import re
import shutil
import tempfile
import threading
import time
import pygame
import win32con
import win32gui
import traceback
import sys

def pause_on_error(context: str, exc: BaseException) -> None:
    print(f"\n[{context}]")
    print(f"error={exc!r}")
    traceback.print_exc()

    try:
        input("\nCrash detected. Press Enter to close...")
    except EOFError:
        pass
from tvcgui.core.paths import data_path, user_data_path
from tvcgui.platform.dolphin import hook, rd32, rbytes
from tvcgui.core.constants import CHAR_NAMES, RUNTIME_IMPACT_FREEZE_OFF, ATT_ID_OFF_PRIMARY
from tvcgui.features.combat.move_id_map import lookup_move_name

import json as _json

WORLD_Y_OFFSET = -0.7
GIANT_FALLBACK_REF_CAM_Z = 7.260000228881836
GIANT_CAMERA_Z_THRESHOLD = 10.0
GIANT_X_SCALE = 0.42
GIANT_Y_SCALE = 0.76
GIANT_Y_SCREEN_OFFSET = 38.0
HURTBOX_BUILD_TAG = "TAKEWHEEL_V24_STABLE_HITS"
_last_hurt_debug_text = "hurt=not-run"
_legend_window_started = False
_legend_visible_requested = False
_legend_manual_hidden = False
PROJECTILE_Y_OFFSET: float = 0
PROJECTILE_RADIUS_SCALE: float = 0.5
LINKED_EFFECT_RADIUS_SCALE: float = 1.0
PROJECTILE_DESPAWN_FRAMES: int = 6
PERSPECTIVE_Z_OVERRIDE: Optional[float] = None
HITBOX_FILTER_FILE = user_data_path("hitboxes", "hitbox_filter.json")
_last_filter_mtime = 0.0
_slot_filter = {"P1": True, "P2": True, "P3": True, "P4": True}
_hurtbox_filter = {"P1": True, "P2": True, "P3": True, "P4": True}
# Ruler sources are intentionally independent from attack-box visibility.
# A player can hide live hitboxes while still keeping any combination of the
# four saved-profile rulers available.
_ruler_slot_filter = {"P1": True, "P2": True, "P3": True, "P4": True}
_show_hurtboxes = True
_hurtbox_view_mode = "clean"
_hitbox_view_mode = "live"
_show_range_ruler = False
# Active-frame samples are retained internally to build the two clean ruler
# axes. They are no longer a separate on-screen Dynamic/ghost layer.
_show_range_dynamic = False
_range_ruler_axes = {"horizontal": True, "vertical": False}
# When both axes are enabled, the 2D sampled coverage envelope is optional.
# Default True preserves the existing combined-axis behavior.
_show_range_envelope = True
# Optional per-slot profile keys selected by the GUI Ruler Lock window. Empty
# means automatic move/posture selection, which remains the default behavior.
_range_ruler_locks: Dict[str, str] = {}
RANGE_PROFILE_BASENAME = "hitbox_range_profiles.json"
# Version 2 invalidates sweeps captured by the old frame gate, which treated
# raw active2_* fields as real hit windows and could include startup circles.
# Existing static ruler geometry remains valid; only the supplemental dynamic
# coverage payload is re-recorded the next time its move is used with Dynamic on.
RANGE_DYNAMIC_SWEEP_VERSION = 2
RANGE_DYNAMIC_MAX_FRAMES = 48
RANGE_DYNAMIC_MAX_SHAPES_PER_FRAME = 8
# Each axis is an active-frame envelope, never the final allocated circle.
# Horizontal remembers the furthest forward edge; Vertical keeps the highest
# and lowest edges across the same confirmed active frames.
RANGE_HORIZONTAL_ENVELOPE_VERSION = 1
RANGE_VERTICAL_ENVELOPE_VERSION = 1
RANGE_VERTICAL_ENVELOPE_MAX_SPAN = 8.0
# When both ruler axes are enabled, reconstruct a compact 2D threat envelope
# from the same confirmed active-frame samples. Each horizontal slice keeps
# its own forward reach instead of combining unrelated X/Y maxima into a box.
RANGE_COVERAGE_SLICE_COUNT = 13
RANGE_COVERAGE_TOUCH_EPSILON = 0.045
# Keep the time heatmap readable even for long multi-hit active windows.
# Short moves keep one layer per recorded active frame. Longer moves are
# compressed into chronological buckets while preserving first/last frame.
RANGE_COVERAGE_TEMPORAL_MAX_LAYERS = 8
# Air/special sweeps remain visible long enough to read after the active state
# ends, but never leave an old airborne arc permanently on the floor.
RANGE_DYNAMIC_LINGER_MS = 520
# Specials often move through several internal state IDs during one command.
# Keep a short action-family session so startup/spin/end or repeated multi-hit
# states contribute to one strength-specific effective-range profile.
RANGE_SPECIAL_SESSION_GRACE_MS = 220
RANGE_SPECIAL_SESSION_MAX_MS = 2400
# Lightweight contact-audit layer.  We deliberately do not revive the old
# broad resolver-pool scan: it was expensive enough to stall the overlay during
# impact.  Instead, a damage drop or the already-observed impact-freeze counter
# opens one short audit window.  The audit samples only the active body boxes of
# the currently profiled move and the already-cached defender hurt rig.
RANGE_CONTACT_AUDIT_VERSION = 2
RANGE_CONTACT_SNAPSHOT_MAX_AGE_MS = 220
RANGE_CONTACT_WITNESS_MAX_GAP = 0.55
RANGE_CONTACT_MARKER_MS = 760
RANGE_CONTACT_MAX_WITNESSES = 10
# A later occurrence needs long enough to be readable at 60 Hz, but it still
# expires with the active snapshot instead of becoming persistent clutter.
RANGE_CONTACT_EXPECTED_MARKER_MS = 260
# Two matching engine-signalled contacts promote a pair from a one-off witness
# to a stable truth reference. It remains diagnostic; it does not rewrite max
# horizontal/vertical envelopes from a single matchup.
RANGE_CONTACT_TRUTH_MIN_SAMPLES = 2
# Ground/air presentation is a live physical state, not a move-name category.
# Each on-stage slot learns its floor-level root Y from idle/walk/crouch/block
# states.  During an action, a meaningful vertical departure from that baseline
# switches the display from the floor ruler to the air sweep.  Explicit jump/
# air-dash states cover the earliest airborne frames before the root has risen
# far enough to cross the geometry threshold.
RANGE_AIRBORNE_ROOT_DELTA = 0.30
# When the fighter's root returns this close to its learned floor reference,
# ground wins even if TvC leaves a jump/air action state ID live for a few
# recovery frames.  This is deliberately lower than the airborne threshold so
# takeoff/landing has a tiny hysteresis band instead of posture chatter.
RANGE_GROUND_SNAP_ROOT_DELTA = 0.16
RANGE_GROUND_REFERENCE_ALPHA = 0.18
RANGE_EXPLICIT_AIR_STATE_IDS = frozenset({8, 9, 20, 21, 22, 28, 29, 35, 36, 50})
RANGE_EXPLICIT_GROUND_STATE_IDS = frozenset({1, 2, 3, 6, 7, 10, 11, 13, 19, 25, 30, 31, 48, 49, 52, 53})
# Presentation posture is independent from the action's current state ID.
# Crouching normals can use dedicated attack states, so their input label is
# also considered when the action is stored; these IDs handle idle/block pose.
RANGE_CROUCH_STATE_IDS = frozenset({10, 11, 49, 53})
# Action states frequently transition through a generic standing/recovery ID
# even while a crouching normal is still visually in progress.  Keep the
# action's posture bucket briefly through that ambiguous hand-off so a 2A/2B/
# 2C ruler does not flicker to the standing ruler mid-animation.  A real new
# action, a confirmed air state, or a settled passive posture immediately wins.
RANGE_POSTURE_ACTION_HOLD_MS = 240
RANGE_POSTURE_PASSIVE_SETTLE_MS = 120
# Schema 5 switches the ruler anchor from the live hurtbox-envelope center to
# the fighter's actual model/root translation.  Old schema-4 attack profiles
# mixed pose changes and stale action origins into their stored reach, so they
# are deliberately re-learned instead of being numerically converted.
RANGE_PROFILE_SCHEMA = 5
RANGE_PROFILE_LEGACY_SCHEMA = 4
RANGE_PROFILE_MAX_LOCAL_REACH = 6.0
RANGE_PROFILE_MAX_ROOT_ADVANCE = 2.25
RANGE_PROFILE_FLUSH_MS = 750
# Range profiles are persistent training data. A known move stays scan-free
# unless it is missing one compact active-frame axis envelope. The one capture
# supplies both Horz and Vert data; no ghost/sweep display is involved.
RANGE_PROFILE_WRITE_ENABLED = True
RANGE_PROFILE_AUTO_LEARN_MISSING_ONLY = True

def _range_profile_resource_path() -> str:
    """Return the packaged read-only seed path for the profile JSON.

    PyInstaller one-file builds unpack data into ``sys._MEIPASS``.  That
    directory is temporary, so it must never be the destination for learned
    range records.  Source runs use this module's directory as both seed and
    writable destination.
    """
    return data_path("hitboxes", RANGE_PROFILE_BASENAME)


def _range_profile_writable_path() -> str:
    """Return the persistent profile path used by the live auto-learner.

    An explicit environment override is useful for portable/test installs.
    Frozen builds otherwise write beside ``TvCGUI.exe`` rather than into the
    one-file extraction directory.
    """
    override = str(os.environ.get("TVC_RANGE_PROFILE_FILE", "") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return user_data_path("hitboxes", RANGE_PROFILE_BASENAME)


def _write_range_profile_payload_atomic(path: str, payload: Dict[str, Any]) -> None:
    """Write and validate one profile JSON payload via same-directory replace."""
    target = os.path.abspath(path)
    parent = os.path.dirname(target) or os.getcwd()
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{RANGE_PROFILE_BASENAME}.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # A few filesystems do not expose fsync; JSON validation below
                # still prevents replacing the good file with malformed output.
                pass
        with open(tmp_path, "r", encoding="utf-8") as f:
            verify = _json.load(f)
        if (
            not isinstance(verify, dict)
            or int(verify.get("schema") or 0) != RANGE_PROFILE_SCHEMA
            or not isinstance(verify.get("attacks"), dict)
            or not isinstance(verify.get("bodies"), dict)
        ):
            raise ValueError("profile export verification failed")
        last_error = None
        for attempt in range(3):
            try:
                os.replace(tmp_path, target)
                tmp_path = ""
                return
            except OSError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.05)
        raise last_error or OSError("could not replace profile JSON")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def _ensure_writable_range_profile_file() -> str:
    """Seed a persistent JSON beside the app on the first frozen launch.

    Existing user data wins.  The packaged profile file is copied only when no
    writable file exists yet, so upgrades never replace learned records.
    """
    target = _range_profile_writable_path()
    if os.path.exists(target):
        return target
    seed = _range_profile_resource_path()
    try:
        if os.path.exists(seed) and os.path.abspath(seed) != os.path.abspath(target):
            with open(seed, "r", encoding="utf-8") as f:
                payload = _json.load(f)
        else:
            payload = {"schema": RANGE_PROFILE_SCHEMA, "attacks": {}, "bodies": {}}
        if not isinstance(payload, dict):
            raise ValueError("range profile seed is not an object")
        payload.setdefault("schema", RANGE_PROFILE_SCHEMA)
        payload.setdefault("attacks", {})
        payload.setdefault("bodies", {})
        _write_range_profile_payload_atomic(target, payload)
        print(f"[range profile] seeded writable JSON: {target}")
    except Exception as exc:
        # Keep the selected target path. The normal exporter will report/retry
        # if the directory is unavailable instead of silently writing to a
        # transient PyInstaller extraction folder.
        print(f"[range profile] could not seed writable JSON {target!r}: {exc!r}")
    return target


# Live profile writes always target a persistent file.  In one-file mode this
# is next to TvCGUI.exe; ``_MEIPASS`` is used only as the first-run seed source.
RANGE_PROFILE_FILE = _ensure_writable_range_profile_file()
# A calibration pip is a small horizontal reach correction applied only after
# an observed hit/miss result disagrees with the retained ruler prediction.
RANGE_CALIBRATION_PIP = 0.055
RANGE_CALIBRATION_MAX_PIPS = 12
RANGE_CALIBRATION_FALSE_POSITIVE_LIMIT = 3
RANGE_CALIBRATION_GRACE_MS = 260
RANGE_PROFILE_GENERIC_BODY = {
    "min_x": -0.72,
    "max_x": 0.72,
    "min_y": -1.70,
    "max_y": 1.70,
    "min_z": -0.65,
    "max_z": 0.65,
}

MOTION_THRESHOLD: float = 0.003
STILL_FRAME_LIMIT: int = 4
MOTION_FRAME_REQUIRED: int = 2
ACTOR_TABLE = 0x80476E50
ACTOR_MAX   = 16  # confirmed live projectile table slots; multiple actors appear inside this table

# Some supers (Morrigan Finishing Shower / similar missile showers) allocate
# many projectile actors in the projectile actor pool, but only a couple of
# those actors are exposed through ACTOR_TABLE on a given frame.  Scan this
# small stride-based pool too so multi-missile moves do not collapse to one
# or two misleading probes.  Normal player hitboxes are untouched by this.
PROJECTILE_ACTOR_POOL_BASES = (
    0x91B159B4,
)
PROJECTILE_ACTOR_POOL_STRIDE = 0x1A4
PROJECTILE_ACTOR_POOL_COUNT = 48


ACTOR_OFF_X = 0x5C
ACTOR_OFF_Y = 0x6C
ACTOR_OFF_Z = 0x7C

# Live projectile actor layout, confirmed from Volnutt dump sequences.
# The actor table can contain duplicate pointers, so projectile reads de-dupe
# by actor address and then validate owner/id/position before drawing.
ACTOR_OFF_PREV_X = 0xBC
ACTOR_OFF_PREV_Y = 0xCC
ACTOR_OFF_PREV_Z = 0xDC

ACTOR_OFF_SWEEP_X = 0xE0
ACTOR_OFF_SWEEP_Y = 0xE4
ACTOR_OFF_SWEEP_Z = 0xE8

ACTOR_OFF_OWNER = 0x130
ACTOR_OFF_PROJ_ID = 0x134
ACTOR_OFF_OWNER_MIRROR = 0x138
ACTOR_OFF_LINKED_RECORD = 0x13C

# Secondary projectile contact/result anchor. Casshan/FLAG_309 tests showed
# +0x118/+0x11C becomes populated on hit and snaps near the defender. That is
# useful as an impact/result point, but it is NOT the live projectile hitbox,
# so do not use it as the projectile anchor by default.
ACTOR_OFF_IMPACT_X = 0x118
ACTOR_OFF_IMPACT_Y = 0x11C
ACTOR_OFF_IMPACT_Z = 0x120
PROJECTILE_USE_IMPACT_ANCHOR = False
PROJECTILE_IMPACT_ANCHOR_MAX_DIST = 1.75

# Projectile actor positions are useful, but the actor-table radii/sweep fields
# are NOT confirmed collision boxes. Keep this conservative: draw a small probe
# at the live actor point only, and do not draw actor +0xE0/+0xE4 as a sweep.
PROJECTILE_FALLBACK_RADIUS = 0.35
PROJECTILE_SWEEP_MIN_WORLD = 0.03
PROJECTILE_SWEEP_MAX_WORLD = 12.0

# Candidate collision visualizer.  This intentionally draws a SHORT local
# capsule in front of the projectile root, using the projectile direction vector
# and the actor's scale-ish field.  It does not use actor +0xE0/+0xE4, because
# live Hadouken tests proved that field is an emitter/origin anchor, not a
# collision sweep.
ACTOR_OFF_SCALE_CANDIDATE = 0xF8
ACTOR_OFF_DIR_X = 0x108
ACTOR_OFF_DIR_Y = 0x10C
ACTOR_OFF_DIR_Z = 0x104
LINKED_OFF_TARGET = 0x34

# Additive linked collision lane for explosions, super radii, attached lines,
# and adjacent effect volumes. This does not replace the original fighter or
# standard projectile readers.
LINKED_COLLIDER_OFF_TYPE = 0x308
LINKED_COLLIDER_OFF_SHAPES = 0x30C
LINKED_COLLIDER_OFF_COUNT = 0x314
LINKED_COLLIDER_TYPE_SPHERE = 1
LINKED_COLLIDER_TYPE_LINE = 2
LINKED_COLLIDER_TYPE_BOX = 3
LINKED_COLLIDER_MAX_SHAPES = 32
LINKED_COLLIDER_POOL_BASES = (0x91A6B774,)
LINKED_COLLIDER_POOL_STRIDE = 0x1544
LINKED_COLLIDER_POOL_COUNT = 48
LINKED_COLLIDER_TO_ACTOR_DELTA = 0x000AA240
LINKED_SPHERE_STRIDE = 0x18
LINKED_LINE_STRIDE = 0x40
LINKED_BOX_STRIDE = 0x20
LINKED_OFF_DIR_X = 0xAC
LINKED_OFF_DIR_Y = 0xB0
PROJECTILE_SCALE_RADIUS_MIN = 0.08
PROJECTILE_SCALE_RADIUS_MAX = 0.85
PROJECTILE_DEFAULT_EXTENT = 0.80
PROJECTILE_EXTENT_MIN = 0.35
PROJECTILE_EXTENT_MAX = 1.25
PROJECTILE_EXTENT_BY_ID = {
    0x135: 0.80,  # Casshan FLAG_309 probe: impact lands ~0.75-0.85u ahead of root.
    0x160: 0.70,  # Volnutt/Morrigan-style multi actors observed as separate 0x160 projectiles.
    0x163: 0.80,  # Morrigan Finishing Shower missile actors.
}

# Doronjo/Odronjo-style giant objects can be actor-table projectiles with a
# very small actor id (0x1), so they pass through the actor system but do not
# carry the usual actor +0xF8 radius.  The linked hit-state card below was
# observed on the big object:
#   linked +0x80 == 0x0000030C
#   linked +0x84 == 0x00000123
# Use this as a narrow signature instead of treating every id=1 actor as huge.
PROJECTILE_LARGE_FIELD_RADIUS = 1.10
PROJECTILE_LARGE_FIELD_EXTENT = 1.45
PROJECTILE_LARGE_FIELD_CARDS = {
    (0x0000030C, 0x00000123),
}

# For some missile showers, actor +0x108/+0x10C is the local/up vector,
# not forward.  These ids draw their candidate capsule from per-frame motion
# first, then fall back to the actor/link direction fields if motion is zero.
PROJECTILE_MOTION_DIR_FIRST_IDS = {
    0x163,
}

# Leave empty until a projectile ID has a collision-confirmed radius.
# Earlier 0x130/0x134/0x160 = 1.20 guesses were visual/template scale and
# produced bogus half-screen circles on Hadouken-style projectiles.
PROJECTILE_RADIUS_BY_ID = {
}

# ----------------------------
# Projectile signature scanner (kept but commented out from active use)
# ----------------------------
# PROJ_SIG         = b"\x04\x01\x02\x00\x00"
# PROJ_RADIUS_OFF  = 0x2F
# PROJ_SCAN_START  = 0x90000000
# PROJ_SCAN_END    = 0x94000000
# PROJ_SCAN_BLOCK  = 0x40000

# Physical hitboxes with radius above this world-unit value are skipped entirely.
HITBOX_MAX_RENDER_RADIUS: float = 4.0

PROJECTILE_POOLS = [
    0x91B15900,
    0x91B15A10,
    0x91B15B50,
    0x91B15C90,
    0x91B15DD0,
    0x91B15F10,
]
PROJECTILE_NODE_STRIDE = 0x30
PROJECTILE_NODE_COUNT = 16

# Node layout (confirmed):
#   Row 0: +0x00 = X,  +0x08 = dim_0
#   Row 1: +0x10 = Y,  +0x18 = dim_1
#   Row 2: +0x20 = Z,  +0x28 = dim_2
PROJ_OFF_X: int = 0x00
PROJ_OFF_Y: int = 0x10
PROJ_OFF_Z: int = 0x20
PROJ_OFF_DIM_0: int = 0x08
PROJ_OFF_DIM_1: int = 0x18
PROJ_OFF_DIM_2: int = 0x28

# Candidate offsets inside the node that may contain an owning actor pointer.
PROJ_PTR_CANDIDATES = (
    0x04,
    0x0C,
    0x14,
    0x1C,
    0x24,
    0x2C,
)

OFF_CHAR_ID = 0x14
# Per-slot activity marker: active/on-stage fighter is 0; standby teammate is 1
# in all supplied team, jump, and giant dumps.  The body descriptors remain
# allocated while standby, so use this before rendering them.
OFF_SLOT_ACTIVITY = 0x04
OFF_STATE_ID = 0x1EA  # current action/state id
OFF_CHR_TBL = 0x1E0   # live character action-table pointer

# Slot-local action-frame counter.
# Captured slot-1 base 0x9246B9C0 and counter address 0x9246BB98.
# 0x9246BB98 - 0x9246B9C0 = 0x1D8.
# Values are big-endian floats: 0x40000000=2.0, 0x40400000=3.0,
# 0x40800000=4.0, etc. The -1 bias maps 2.0 to action frame 1.
OFF_ACTION_COUNTER = 0x1D8
OFF_STATE_FLAGS = 0x0058
OFF_GUARD_MASK = 0x0244
OFF_COLLISION_EXCLUSION = 0x1218
GUARD_ENABLE_BIT = 0x00400000
GUARD_MID_BIT = 0x02000000
GUARD_HIGH_BIT = 0x04000000
GUARD_LOW_BIT = 0x08000000
GUARD_HEIGHT_MASK = GUARD_MID_BIT | GUARD_HIGH_BIT | GUARD_LOW_BIT
STUN_BYPASS_BIT = 0x00040000
NO_DAMAGE_GUARD_BIT = 0x00100000
GENERIC_PROTECTION_MARKER = 0x00000001
CONFIRMED_COUNTER_ACTIONS = {
    (27, 0x0139),
    (27, 0x013A),
    (27, 0x013B),
}
CONFIRMED_GUARD_ACTIONS = {
    (2, 0x0136),
    (2, 0x0137),
    (2, 0x0138),
}
CONFIRMED_ARMOR_ACTIONS = {
    (27, 0x0136),
    (27, 0x0137),
    (27, 0x0138),
}
ACTION_COUNTER_FRAME_BIAS = -1.0
ACTION_COUNTER_MIN = 0.0
ACTION_COUNTER_MAX = 600.0
FRAME_DATA_SCAN_ENABLED = True


PASSIVE_STATE_IDS = {
    1,   # idle
    2,   # forward
    3,   # backward
    6,   # forward dash
    7,   # back dash
    8,   # air dash
    9,   # rising
    10,  # crouching
    11,  # crouched
    13,  # landing
    19,  # pre jump
    20,  # jump
    21,  # jump forward
    22,  # jump back
    25,  # push block post anim
    28,  # super jump
    29,  # super jump
    30,  # landing
    31,  # pre super jump
    35,  # air dash forward
    36,  # air dash back
    48,  # block
    49,  # crouching block
    50,  # air block
    52,  # air pushblock
    53,  # crouch pushblock
}

PASSIVE_STATE_IDS = {
    1, 2, 3,
    6, 7, 8, 9, 10, 11, 13,
    19, 20, 21, 22,
    25, 28, 29, 30, 31, 35, 36,
    48, 49, 50, 52, 53,
}

# Megacrash has large dormant volumes in memory before/after the actual burst.
# Keep those hidden unless frame-data gating says the burst is currently active.
MEGACRASH_STATE_IDS = {448}

def read_state_raw(slot_base: int) -> int:
    raw = rd32(slot_base + OFF_STATE_ID)
    return 0 if raw is None else raw

def decode_state_id(raw: int) -> int:
    hi16 = (raw >> 16) & 0xFFFF
    low16 = raw & 0xFFFF
    low8 = raw & 0xFF

    if hi16 != 0:
        return hi16
    if low16 != 0:
        return low16
    return low8

def read_state_id(slot_base: int) -> int:
    raw = read_state_raw(slot_base)
    state_id = decode_state_id(raw)
    return state_id

def is_passive_state(state_id: int) -> bool:
    return state_id in PASSIVE_STATE_IDS
_last_state_ids: Dict[str, int] = {}
_last_state_raws: Dict[str, int] = {}
def _slots_from_payload(value, fallback):
    out = dict(fallback)
    if isinstance(value, dict):
        for slot in ("P1", "P2", "P3", "P4"):
            if slot in value:
                out[slot] = bool(value.get(slot))
    return out


def _normalize_hurtbox_view_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"clean", "simple", "minimal"}:
        return "clean"
    if raw in {"detailed", "detail", "full"}:
        return "detailed"
    if raw in {"debug", "raw", "labels"}:
        return "debug"
    return "clean"


def _normalize_hitbox_view_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"ghost", "trail", "history"}:
        return "ghost"
    return "live"


def set_hitbox_view_mode(value: Any) -> str:
    """Set and persist the attack-hitbox presentation mode."""
    global _hitbox_view_mode, _last_filter_mtime
    mode = _normalize_hitbox_view_mode(value)
    _hitbox_view_mode = mode
    try:
        payload = {}
        if os.path.exists(HITBOX_FILTER_FILE):
            with open(HITBOX_FILTER_FILE, "r", encoding="utf-8") as f:
                existing = _json.load(f)
            if isinstance(existing, dict):
                payload = existing
        payload["hitbox_view_mode"] = mode
        os.makedirs(os.path.dirname(HITBOX_FILTER_FILE), exist_ok=True)
        temp_path = HITBOX_FILTER_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            _json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(temp_path, HITBOX_FILTER_FILE)
        _last_filter_mtime = 0.0
    except Exception as exc:
        print(f"[HitboxView] could not save mode {mode!r}: {exc!r}")
    return mode


def _read_hitbox_view_mode() -> str:
    _read_filter_payload()
    return _hitbox_view_mode


def _range_ruler_enabled() -> bool:
    """Return whether the retained single-tip range ruler is enabled."""
    _read_filter_payload()
    return bool(_show_range_ruler)


def _range_dynamic_enabled() -> bool:
    """Compatibility shim: Dynamic ghost coverage has been retired."""
    _read_filter_payload()
    return False


def set_range_dynamic_enabled(value: Any) -> bool:
    """Compatibility shim that permanently disables the retired ghost layer."""
    global _show_range_dynamic, _last_filter_mtime
    _show_range_dynamic = False
    try:
        payload = {}
        if os.path.exists(HITBOX_FILTER_FILE):
            with open(HITBOX_FILTER_FILE, "r", encoding="utf-8") as f:
                existing = _json.load(f)
            if isinstance(existing, dict):
                payload = existing
        payload["show_range_dynamic"] = False
        os.makedirs(os.path.dirname(HITBOX_FILTER_FILE), exist_ok=True)
        temp_path = HITBOX_FILTER_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            _json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(temp_path, HITBOX_FILTER_FILE)
        _last_filter_mtime = 0.0
    except Exception:
        pass
    return False


def _range_ruler_axes_enabled() -> Dict[str, bool]:
    """Return the independent saved-profile ruler layer toggles.

    Horz and Vert are intentionally not mutually exclusive. Horz answers
    forward reach; Vert answers the full active height envelope.
    """
    _read_filter_payload()
    return {
        "horizontal": bool(_range_ruler_axes.get("horizontal", True)),
        "vertical": bool(_range_ruler_axes.get("vertical", False)),
    }


def _range_envelope_enabled() -> bool:
    _read_filter_payload()
    return bool(_show_range_envelope)


def _range_ruler_locks_enabled() -> Dict[str, str]:
    _read_filter_payload()
    return dict(_range_ruler_locks)


def set_range_ruler_enabled(value: Any) -> bool:
    """Set and persist the optional observed-gap ruler."""
    global _show_range_ruler, _last_filter_mtime
    _show_range_ruler = bool(value)
    try:
        payload = {}
        if os.path.exists(HITBOX_FILTER_FILE):
            with open(HITBOX_FILTER_FILE, "r", encoding="utf-8") as f:
                existing = _json.load(f)
            if isinstance(existing, dict):
                payload = existing
        payload["show_range_ruler"] = bool(_show_range_ruler)
        os.makedirs(os.path.dirname(HITBOX_FILTER_FILE), exist_ok=True)
        temp_path = HITBOX_FILTER_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            _json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(temp_path, HITBOX_FILTER_FILE)
        _last_filter_mtime = 0.0
    except Exception as exc:
        print(f"[RangeRuler] could not save state: {exc!r}")
    return bool(_show_range_ruler)


def set_hurtbox_view_mode(value: Any) -> str:
    """Set and persist the legend-selected hurtbox presentation mode.

    The main overlay polls hitbox_filter.json, so persisting here keeps the
    selection through a relaunch without touching any existing hitbox filters.
    """
    global _hurtbox_view_mode, _last_filter_mtime
    mode = _normalize_hurtbox_view_mode(value)
    _hurtbox_view_mode = mode
    try:
        payload = {}
        if os.path.exists(HITBOX_FILTER_FILE):
            with open(HITBOX_FILTER_FILE, "r", encoding="utf-8") as f:
                existing = _json.load(f)
            if isinstance(existing, dict):
                payload = existing
        payload["hurtbox_view_mode"] = mode
        os.makedirs(os.path.dirname(HITBOX_FILTER_FILE), exist_ok=True)
        temp_path = HITBOX_FILTER_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            _json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(temp_path, HITBOX_FILTER_FILE)
        _last_filter_mtime = 0.0
    except Exception as exc:
        print(f"[HitboxView] could not save mode {mode!r}: {exc!r}")
    return mode


def _read_filter_payload() -> None:
    global _last_filter_mtime, _slot_filter, _hurtbox_filter, _ruler_slot_filter, _show_hurtboxes, _hurtbox_view_mode, _hitbox_view_mode, _show_range_ruler, _show_range_dynamic, _range_ruler_axes, _show_range_envelope, _range_ruler_locks
    try:
        mt = os.path.getmtime(HITBOX_FILTER_FILE)
        if mt == _last_filter_mtime:
            return
        _last_filter_mtime = mt
        with open(HITBOX_FILTER_FILE) as f:
            data = _json.load(f)

        if not isinstance(data, dict):
            return

        _slot_filter = _slots_from_payload(data.get("hitbox_slots", data), _slot_filter)
        _hurtbox_filter = _slots_from_payload(data.get("hurtbox_slots", data.get("_hurtbox_slots", _hurtbox_filter)), _hurtbox_filter)
        # Old configs had no ruler_slots key.  Their safe/default behavior is
        # all four raw fighter slots enabled, rather than inheriting whichever
        # attack boxes happen to be visible.
        _ruler_slot_filter = _slots_from_payload(data.get("ruler_slots", _ruler_slot_filter), _ruler_slot_filter)
        _show_hurtboxes = bool(data.get("show_hurtboxes", any(_hurtbox_filter.values())))
        _hurtbox_view_mode = _normalize_hurtbox_view_mode(data.get("hurtbox_view_mode", _hurtbox_view_mode))
        _hitbox_view_mode = _normalize_hitbox_view_mode(data.get("hitbox_view_mode", _hitbox_view_mode))
        _show_range_ruler = bool(data.get("show_range_ruler", _show_range_ruler))
        # Legacy configs may still contain this key. It is deliberately ignored
        # so a stale Dynamic ON setting can never redraw ghost coverage.
        _show_range_dynamic = False
        _axes = data.get("range_ruler_axes")
        if isinstance(_axes, dict):
            _range_ruler_axes = {
                "horizontal": bool(_axes.get("horizontal", _range_ruler_axes.get("horizontal", True))),
                "vertical": bool(_axes.get("vertical", _range_ruler_axes.get("vertical", False))),
            }
        else:
            # Legacy exclusive selector migration. New UI persists both keys.
            _axis = str(data.get("range_ruler_axis", "horizontal") or "horizontal").strip().lower()
            _range_ruler_axes = {"horizontal": _axis != "vertical", "vertical": _axis == "vertical"}
        _show_range_envelope = bool(data.get("range_ruler_envelope", True))
        _locks = data.get("range_ruler_locks")
        if isinstance(_locks, dict):
            _range_ruler_locks = {
                str(_slot): str(_profile)
                for _slot, _profile in _locks.items()
                if str(_slot) in SLOT_BASES and str(_profile).strip()
            }
        else:
            _range_ruler_locks = {}
    except Exception:
        pass


def _read_slot_filter() -> dict:
    _read_filter_payload()
    return _slot_filter


def _read_hurtbox_filter() -> dict:
    _read_filter_payload()
    return _hurtbox_filter


def _read_ruler_slot_filter() -> dict:
    """Return the four independent source-slot toggles for saved rulers."""
    _read_filter_payload()
    return _ruler_slot_filter


def _hurtbox_layer_requested() -> bool:
    _read_filter_payload()
    return bool(_show_hurtboxes and any(_hurtbox_filter.values()))


def _read_hurtbox_view_mode(control=None) -> str:
    _read_filter_payload()
    if control is not None and bool(getattr(control, "show_debug", False)):
        return "debug"
    return _hurtbox_view_mode


def dump_state18(slot_name: str, slot_base: int) -> None:
    raw = rd32(slot_base + OFF_STATE_ID)
    raw = 0 if raw is None else raw

    hi16 = (raw >> 16) & 0xFFFF
    low16 = raw & 0xFFFF
    low8 = raw & 0xFF

    print(
        f"[State18] {slot_name} "
        f"addr=0x{slot_base + OFF_STATE_ID:08X} "
        f"raw=0x{raw:08X} ({raw}) "
        f"hi16=0x{hi16:04X} ({hi16}) "
        f"low16=0x{low16:04X} ({low16}) "
        f"low8=0x{low8:02X} ({low8})"
    )

def set_dpi_aware() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


set_dpi_aware()


@dataclass(frozen=True)
class HitboxLayout:
    struct_shift: int
    blocks: Tuple[int, ...]
    off_x: int
    off_y: int
    off_r: int
    off_flag: int


@dataclass(frozen=True)
class CameraLayout:
    base: int
    off_x: int
    off_y: int
    off_z: int
    off_w: int


@dataclass(frozen=True)
class DisplayConfig:
    baseline_w: int
    baseline_h: int
    baseline_ppu: float
    zoom: float
    center_y_offset_px: int
    max_radius_units: float
    fps: int
    show_debug_axes: bool


SLOT_BASES: Dict[str, int] = {
    "P1": 0x9246B9C0,
    "P2": 0x92B6BA00,
    "P3": 0x927EB9E0,
    "P4": 0x92EEBA20,
}
SLOT_ORDER: Tuple[str, ...] = tuple(SLOT_BASES.keys())


@dataclass(frozen=True)
class MoveFrameData:
    move_id: int
    move_name: str
    active_windows: Tuple[Tuple[int, int], ...]
    startup_frames: int = 0
    recovery_frames: int = 0
    total_frames: int = 0
    invuln_frames: int = 0

    def active_text(self) -> str:
        if not self.active_windows:
            return "?"
        return ",".join(f"{s}-{e}" if s != e else str(s) for s, e in self.active_windows)

    def invuln_text(self) -> str:
        try:
            frames = int(self.invuln_frames or 0)
        except Exception:
            frames = 0
        return f"{frames}f" if frames > 0 else ""


def _valid_active_window(start: Any, end: Any) -> Optional[Tuple[int, int]]:
    try:
        s = int(start)
        e = int(end)
    except Exception:
        return None
    if s <= 0 or e <= 0:
        return None
    if e < s:
        e = s
    return (s, e)


def _state_lookup_keys(state_id: int) -> Tuple[int, ...]:
    keys: List[int] = []
    try:
        raw = int(state_id) & 0xFFFF
    except Exception:
        return tuple(keys)

    for candidate in (raw, raw & 0xFF, 0x0100 | (raw & 0xFF)):
        if candidate not in keys:
            keys.append(candidate)
    return tuple(keys)


def _parse_invuln_frames(raw: Any) -> int:
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        try:
            return max(0, int(raw))
        except Exception:
            return 0
    txt = str(raw or "").strip().lower()
    if not txt:
        return 0
    vals = []
    for m in re.finditer(r"(\d+)\s*f", txt):
        try:
            vals.append(int(m.group(1)))
        except Exception:
            pass
    if vals:
        return max(vals)
    return 0


def is_frame_data_invuln(fd: Optional[MoveFrameData], action_frame: Optional[int]) -> bool:
    if fd is None or action_frame is None:
        return False
    try:
        frames = int(fd.invuln_frames or 0)
    except Exception:
        return False
    return frames > 0 and 1 <= int(action_frame) <= frames


def _dim_hitbox_color(color: Tuple[int, int, int]) -> Tuple[int, int, int]:
    r, g, b = color[:3]
    return (
        max(35, int(r * 0.45)),
        max(35, int(g * 0.45)),
        max(35, int(b * 0.45)),
    )


def _soften_hurt_color(color: Tuple[int, int, int], factor: float = 0.58, floor: int = 36) -> Tuple[int, int, int]:
    r, g, b = color[:3]
    return (
        max(floor, int(r * factor)),
        max(floor, int(g * factor)),
        max(floor, int(b * factor)),
    )


def _renderer_slot_from_scan_label(label: str, fallback_idx: int) -> Optional[str]:
    s = str(label or "").strip().lower().replace("_", "-").replace(" ", "")
    mapping = {
        "p1-c1": "P1", "p1c1": "P1", "p1-char1": "P1", "p1char1": "P1",
        "p2-c1": "P2", "p2c1": "P2", "p2-char1": "P2", "p2char1": "P2",
        "p1-c2": "P3", "p1c2": "P3", "p1-char2": "P3", "p1char2": "P3",
        "p2-c2": "P4", "p2c2": "P4", "p2-char2": "P4", "p2char2": "P4",
        "p1": "P1", "p2": "P2", "p3": "P3", "p4": "P4",
    }
    if s in mapping:
        return mapping[s]
    if 0 <= fallback_idx < len(SLOT_ORDER):
        return SLOT_ORDER[fallback_idx]
    return None


def read_action_counter_float(slot_base: int) -> Optional[float]:
    raw = rd32(slot_base + OFF_ACTION_COUNTER)
    if raw is None:
        return None
    try:
        value = struct.unpack(">f", struct.pack(">I", raw & 0xFFFFFFFF))[0]
    except Exception:
        return None
    if not math.isfinite(value):
        return None
    if value < ACTION_COUNTER_MIN or value > ACTION_COUNTER_MAX:
        return None
    return value


def read_action_frame(slot_base: int) -> Optional[int]:
    value = read_action_counter_float(slot_base)
    if value is None:
        return None
    return max(0, int(round(value + ACTION_COUNTER_FRAME_BIAS)))


def is_frame_data_active(fd: Optional[MoveFrameData], action_frame: Optional[int]) -> bool:
    if fd is None or action_frame is None:
        return False
    for start, end in fd.active_windows:
        if start <= action_frame <= end:
            return True
    return False


def is_ground_normal_frame_data(fd: Optional[MoveFrameData]) -> bool:
    """Return True for standard grounded normal labels such as 5A, 2B, 6C.

    Missing-profile capture intentionally stays limited to ordinary grounded
    normals.  Existing saved profiles can still be rendered for their known
    move keys, but a special/projectile/super is never auto-created merely
    because its frame-data row happened to have an active window.
    """
    if fd is None or is_air_normal_frame_data(fd):
        return False
    label = str(getattr(fd, "move_name", "") or "").strip().upper()
    if not label:
        return False
    compact = re.sub(r"[\s._-]+", "", label)
    return bool(re.fullmatch(r"[1-9]?[ABC]", compact))


def is_air_normal_frame_data(fd: Optional[MoveFrameData]) -> bool:
    """Return True only for explicitly labelled jumping/air normal rows.

    The persistent range guide is ground-only for now.  This intentionally uses
    the saved frame-data label rather than root height or hitbox placement:
    airborne specials/supers can have overlapping root positions, whereas the
    normal preview consistently labels jumping normals as ``j.A``/``j.B``/
    ``j.C`` (or an ``air`` equivalent).
    """
    if fd is None:
        return False
    label = str(getattr(fd, "move_name", "") or "").strip().lower()
    if not label:
        return False
    label = label.replace("jump.", "j.").replace("jump ", "j.")
    label = label.replace("air.", "j.").replace("air ", "j.")
    compact = re.sub(r"[\s._-]+", "", label)
    return label.startswith("j.") or compact in {"ja", "jb", "jc", "j2a", "j2b", "j2c"}


def _range_label_is_air(label: Any) -> bool:
    """Return whether a move label belongs to an airborne action family."""
    text = str(label or "").strip().lower()
    if not text:
        return False
    compact = re.sub(r"[\s._-]+", "", text)
    return (
        text.startswith("j.")
        or text.startswith("jump")
        or text.startswith("air ")
        or text.startswith("air_")
        or compact.startswith("air")
        or compact in {"ja", "jb", "jc", "j2a", "j2b", "j2c"}
    )


def _range_label_is_throw_or_grab(label: Any) -> bool:
    """Keep throws/grabs out of body-hitbox range profiles for now."""
    text = str(label or "").strip().lower()
    return any(token in text for token in ("throw", "grab", "command grab"))


def _range_label_is_normal(label: Any) -> bool:
    """Recognize the normal labels used by the compact frame-data cache."""
    text = str(label or "").strip().lower()
    if not text:
        return False
    canonical = text.replace("jump.", "j.").replace("jump ", "j.")
    canonical = canonical.replace("air.", "j.").replace("air ", "j.")
    compact = re.sub(r"[\s._-]+", "", canonical)
    return bool(re.fullmatch(r"[1-9]?[abc]", compact) or compact in {"ja", "jb", "jc", "j2a", "j2b", "j2c"})


def _range_special_family(label: Any) -> str:
    """Return a stable family key for multi-state body specials.

    TvC can expose one command as several mapped states, for example
    ``Tatsu L`` -> ``Tatsu Start`` -> ``Tatsu Spin`` -> ``Tatsu End``.
    The ruler needs those states to describe one physical attack instead of
    four unrelated profiles.  Air/super/shin modifiers are intentionally kept
    so distinct move families do not collapse together.
    """
    text = str(label or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"\([^)]*\)", " ", text)
    tokens = [tok for tok in re.split(r"[^a-z0-9]+", text) if tok]
    if not tokens:
        return ""
    # Strength suffixes identify variants but not the underlying family.
    while tokens and tokens[-1] in {"a", "b", "c", "l", "m", "h", "light", "medium", "heavy"}:
        tokens.pop()
    # Internal phase labels should remain attached to the command that began
    # the session.  Preserve semantic modifiers such as air/super/shin.
    phase_words = {
        "start", "startup", "windup", "spin", "end", "ending",
        "phase", "second", "third", "loop", "repeat", "recovery",
    }
    while len(tokens) > 1 and tokens[-1] in phase_words:
        tokens.pop()
    # ``second phase`` leaves ``second`` after removing phase.
    while len(tokens) > 1 and tokens[-1] in {"second", "third"}:
        tokens.pop()
    return " ".join(tokens)


def _range_special_has_explicit_strength(label: Any) -> bool:
    """Return whether a special label names a concrete button strength."""
    text = str(label or "").strip().lower()
    tokens = [tok for tok in re.split(r"[^a-z0-9]+", text) if tok]
    return bool(tokens and tokens[-1] in {"a", "b", "c", "l", "m", "h", "light", "medium", "heavy"})


def range_action_descriptor(
    fd: Optional[MoveFrameData],
    char_id: int,
    state_id: int,
) -> Optional[Dict[str, Any]]:
    """Describe one action for the saved ruler/sweep system.

    Move labels classify the action only for bookkeeping.  The actual display
    is selected later from the fighter's live standing/crouching/air state:
    each posture recalls only its own most-recent saved action.  The same
    single-tip ruler is drawn at the fighter's current ground or air height;
    Dynamic is the optional coverage layer.

    Frame-data actions are gated by their validated active windows. Mapped
    special states do not have full frame data yet, so they use the existing
    live 0x53 attack-flag path and only profile a real body hitbox episode.
    Projectile ownership remains separate; a projectile-only startup will not
    produce player-body hitbox samples and therefore cannot create a profile.
    """
    try:
        cid = int(char_id)
        sid = int(state_id)
    except Exception:
        return None

    # The caller supplies fighter+0x1E8 when available. Keep that action ID
    # authoritative even if a compact frame-data alias resolves through the
    # same low byte. This is what keeps strength variants in separate profiles.
    move_id = sid
    try:
        mapped_name = str(lookup_move_name(move_id, cid) or "").strip()
    except Exception:
        mapped_name = ""
    if mapped_name:
        move_name = mapped_name
    elif fd is not None:
        move_name = str(getattr(fd, "move_name", "") or "").strip()
    else:
        move_name = ""
    has_frame_gate = bool(fd is not None and getattr(fd, "active_windows", ()))

    # Range capture is geometry-driven. A missing move-map label must never
    # make a real active body hitbox ineligible for profiling. Keep a stable
    # action-ID fallback name so unknown attacks still get their own profile.
    if not move_name:
        move_name = f"Action 0x{move_id:04X}"

    is_air = _range_label_is_air(move_name)
    is_normal = _range_label_is_normal(move_name)
    if is_normal:
        kind = "air_normal" if is_air else "ground_normal"
    else:
        kind = "air_special" if is_air else "ground_special"

    return {
        "move_id": move_id,
        "move_name": move_name,
        "kind": kind,
        # This is only a default/fallback.  The renderer overwrites it with
        # physical grounded/airborne state for each action instance.
        "display_mode": "ground_ruler",
        "label_air_hint": bool(is_air),
        # Normals can use validated frame-data timing. Everything else is
        # geometry-driven: if the live descriptor says 0x53, it is active and
        # must be tracked regardless of whether its frame-data row is complete.
        "frame_gated": bool(has_frame_gate and is_normal),
    }


# These are locomotion/passive labels that can appear briefly between attack
# states.  They are valid state names, but never valid posture-lock sources.
_RANGE_PASSIVE_ACTION_LABELS = frozenset({
    "idle", "ready", "forward", "backward", "walk", "walk forward",
    "walk backward", "crouch", "crouching", "stand", "standing",
    "block", "guard", "landing", "turn", "taunt", "assist standby",
})


def _range_descriptor_is_combat_action(
    descriptor: Optional[Dict[str, Any]],
    fd: Optional[MoveFrameData],
    state_id: int,
) -> bool:
    """Return whether a descriptor is safe to use as a posture-lock source.

    The move map includes passive state labels such as ``Forward`` and
    ``idle``.  Treating those as a new action overwrites a crouching lock in
    the middle of a 2A recovery.  Validated frame-data actions always win;
    unprofiled specials are allowed only when their label is not a passive
    locomotion/state label.
    """
    if not isinstance(descriptor, dict):
        return False
    try:
        if fd is not None and bool(getattr(fd, "active_windows", ())):
            return True
    except Exception:
        pass
    try:
        sid = int(state_id)
    except Exception:
        sid = 0
    label = str(descriptor.get("move_name") or "").strip().lower()
    compact = re.sub(r"[\s._-]+", " ", label).strip()
    if not compact or compact in _RANGE_PASSIVE_ACTION_LABELS:
        return False
    if sid in RANGE_EXPLICIT_GROUND_STATE_IDS:
        return False
    # A mapped non-passive special can lack frame data; it is still a real
    # action and will be guarded by the live 0x53 hitbox path during learning.
    return True


def lookup_frame_data(
    fd_by_slot: Dict[str, Dict[int, MoveFrameData]],
    slot_name: str,
    state_id: int,
) -> Optional[MoveFrameData]:
    slot_fd = fd_by_slot.get(slot_name) or {}
    if not slot_fd:
        return None
    for key in _state_lookup_keys(state_id):
        fd = slot_fd.get(key)
        if fd is not None:
            return fd
    return None


def build_frame_data_cache() -> Dict[str, Dict[int, MoveFrameData]]:
    """Build renderer frame gates from the compact read-only preview cache.

    This function runs inside the Dolphin-parented overlay and is called on
    character changes. It must never invoke the full dynamic normal scanner:
    doing so blocks this process for tens of seconds and makes fresh character
    data appear to "flash in" only after the scan finishes.
    """
    if not FRAME_DATA_SCAN_ENABLED:
        return {}

    try:
        import tvcgui.tools.scanners.normal_scanner as fdscan
    except Exception as exc:
        print(f"[FrameGate] scan_normals_all import failed: {exc!r}")
        return {}

    try:
        # Preview-only is a small saved normal snapshot. It may return a cache
        # miss for an unprofiled character, but it never performs a dynamic
        # MEM2 scan and never writes either profile JSON during a match.
        scanned = fdscan.scan_once(cache_only=True, preview_only=True)
    except Exception as exc:
        print(f"[FrameGate] preview cache read failed: {exc!r}")
        return {}

    fd_by_slot: Dict[str, Dict[int, MoveFrameData]] = {}
    for idx, entry in enumerate(scanned or []):
        if not isinstance(entry, dict):
            continue
        slot_name = _renderer_slot_from_scan_label(str(entry.get("slot_label") or ""), idx)
        if slot_name is None:
            continue
        slot_map: Dict[int, MoveFrameData] = {}

        for mv in entry.get("moves", []):
            move_id = mv.get("id")
            if move_id is None:
                continue

            # ``active2_*`` is not a reliable second hit window in the raw
            # scanner output.  For ordinary normals it frequently contains a
            # neighboring setup/timing field (for example Ryu 2A reports 1-4
            # there while its real strike begins at 6).  Treating it as active
            # made the Dynamic sweep record startup pose circles as attacks.
            #
            # Canonical timing comes from the move's primary active window plus
            # explicitly resolved hit-segment windows.  Real multi-hits carry
            # their additional phases in ``hit_segments`` (e.g. Ryu 6B), so
            # this keeps legitimate later hits without promoting raw active2
            # noise into an attack window.
            windows: List[Tuple[int, int]] = []
            primary = _valid_active_window(mv.get("active_start"), mv.get("active_end"))
            if primary is not None:
                windows.append(primary)

            raw_segments = mv.get("hit_segments")
            if isinstance(raw_segments, list):
                for segment in raw_segments:
                    if not isinstance(segment, dict):
                        continue
                    segment_window = _valid_active_window(
                        segment.get("active_start"), segment.get("active_end")
                    )
                    if segment_window is not None and segment_window not in windows:
                        windows.append(segment_window)

            windows.sort(key=lambda item: (int(item[0]), int(item[1])))

            invuln_frames = _parse_invuln_frames(mv.get("invuln_frames") or mv.get("invuln"))
            if not windows and invuln_frames <= 0:
                continue

            try:
                mid = int(move_id) & 0xFFFF
            except Exception:
                continue

            try:
                startup_frames = int(mv.get("startup"))
            except Exception:
                startup_frames = min((int(start) for start, _end in windows), default=0)
            startup_frames = max(0, int(startup_frames))

            recovery_raw = mv.get("runtime_observed_recovery")
            if recovery_raw is None:
                recovery_raw = mv.get("recovery")
            try:
                recovery_frames = max(0, int(recovery_raw))
            except Exception:
                recovery_frames = 0

            try:
                total_frames = max(0, int(mv.get("animation_total_frames") or 0))
            except Exception:
                total_frames = 0

            fd = MoveFrameData(
                move_id=mid,
                move_name=str(mv.get("move_name") or f"anim_{mid:04X}"),
                active_windows=tuple(windows),
                startup_frames=startup_frames,
                recovery_frames=recovery_frames,
                total_frames=total_frames,
                invuln_frames=invuln_frames,
            )

            for key in _state_lookup_keys(mid):
                previous = slot_map.get(key)
                # Prefer the canonical full action ID over a low-byte/internal
                # script alias.  This matters for Shoryu 0x0136/7/8, whose
                # live state must not be shadowed by an unrelated 0x0036 row.
                if previous is None or ((mid & 0xFF00) and not (int(previous.move_id) & 0xFF00)):
                    slot_map[key] = fd

        fd_by_slot[slot_name] = slot_map

    loaded = sum(len(v) for v in fd_by_slot.values())
    print(f"[FrameGate] loaded {loaded} preview frame-gate entries")
    return fd_by_slot


PASSIVE_HOLD_FRAMES = 3
_slot_passive_hold: Dict[str, int] = {k: 0 for k in SLOT_BASES}

HITBOX = HitboxLayout(
    struct_shift=0x4C0,
    blocks=(0x64, 0xA4, 0xE4),
    off_x=0x00,
    off_y=0x04,
    off_r=0x18,
    off_flag=0xC3,
)

# Skeletal hurtbox descriptors.  This is separate from the existing normal
# slot hitbox reader above.  Each descriptor points at a live 3x4 bone matrix
# in MEM1, has a local offset, and a radius.  Draw this only for active hit
# targets for now so normal/projectile UX does not get buried.
HURTBOX_DESC_BASE = 0xC20
HURTBOX_DESC_STRIDE = 0x18
HURTBOX_DESC_COUNT = 24
HURTBOX_MIN_RADIUS = 0.025
HURTBOX_MAX_RADIUS = 0.85
HURTBOX_CONTACT_PAD = 0.03
# Standby teammates keep descriptor pointers, but many of those matrices are
# zero/degenerate/stale.  Reject bad transforms before drawing and require a
# coherent body rig before a whole slot is considered renderable.
HURTBOX_MATRIX_MIN_ROW_NORM = 0.18
HURTBOX_MATRIX_MAX_ROW_NORM = 4.00
HURTBOX_MATRIX_MAX_TRANSLATION = 45.0
HURTBOX_MIN_COHERENT_DESCRIPTORS = 5
HURTBOX_COHERENT_XY_RANGE = 6.0
HURTBOX_COHERENT_Z_RANGE = 5.0
# Reaction/hitstun states from move_id_map_charagnostic.csv.  The first
# hurtbox probe only drew when a transient hit-contact record was found,
# which often disappears by the time the overlay frame paints.  Keep the
# contact highlight path, but also draw the victim skeletal hurtboxes while
# they are visibly in hitstun/knockdown/recovery states.
HURTBOX_REACTION_STATE_IDS = {
    60, 61, 62, 64, 65,
    70, 73, 74, 75, 76, 77, 79, 80,
    89, 90, 91, 92, 93, 94, 95, 96, 98,
    101, 102, 104, 108, 109,
    113, 115, 116, 119, 124, 126, 128, 129, 130, 132, 133, 142,
    161, 166,
}
HIT_EVENT_SCAN_START = 0x91970000
HIT_EVENT_SCAN_END = 0x91978000

CAMERA = CameraLayout(
    base=0x8053CB20,
    off_x=0x00,
    off_y=0x04,
    off_z=0x08,
    off_w=0x0C,
)

DISPLAY = DisplayConfig(
    baseline_w=1280,
    baseline_h=720,
    baseline_ppu=160.0,
    zoom=1.0,
    center_y_offset_px=0,
    max_radius_units=8.0,
    fps=60,
    show_debug_axes=False,
)

USE_LIVE_CAMERA = True
# TvC camera's Y/Z view affine is kept beside the camera position.  The
# overlay used to ignore this tilt transform, which is why bone hurtboxes
# gradually drifted during camera follow / super-jump pans.
CAMERA_VIEW_COS_OFF = 0x20
CAMERA_VIEW_SIN_OFF = 0x24
CAMERA_VIEW_Y_TRANSLATE_OFF = 0x28
CAMERA_DEPTH_Y_COEFF_OFF = 0x30
CAMERA_DEPTH_Z_COEFF_OFF = 0x34
CAMERA_DEPTH_TRANSLATE_OFF = 0x38

COLORS: Dict[str, List[Tuple[int, int, int]]] = {
    "P1": [(255, 72, 92), (255, 104, 124), (255, 142, 156)],
    "P2": [(180, 60, 255), (60, 200, 255), (60, 255, 180)],
    "P3": [(255, 80, 180), (255, 0, 120), (255, 120, 200)],
    "P4": [(80, 255, 120), (0, 255, 80), (120, 255, 200)],
}

PROJ_COLORS: Dict[str, Tuple[int, int, int]] = {
    "P1": (255, 180, 120),
    "P2": (180, 140, 255),
    "P3": (255, 160, 210),
    "P4": (160, 255, 200),
}

HURT_REGION_COLORS: Dict[str, Tuple[int, int, int]] = {
    "head": (165, 245, 255),
    "torso": (88, 210, 255),
    "pelvis": (88, 160, 255),
    "arm_l": (90, 250, 205),
    "arm_r": (90, 250, 205),
    "leg_l": (135, 210, 255),
    "leg_r": (135, 210, 255),
    "other": (95, 235, 255),
}

COL_CROSS = (255, 255, 255)
COL_DIM = (120, 120, 120)
COL_BG = (0, 0, 0)
COL_DEBUG = (0, 255, 0)
COL_PROJ = (255, 255, 255)

COL_CROSS = (255, 255, 255)
COL_DIM = (120, 120, 120)
COL_BG = (0, 0, 0)
COL_DEBUG = (0, 255, 0)
COL_PROJ = (255, 255, 255)

HITBOX_SPAWN_FRAMES = 6
HITBOX_CROSS_DELAY_FRAMES = 1
HITBOX_ACTIVE_PULSE_SPEED = 0.22

# --- surface cache ---
_surface_cache: Dict[Tuple[int, Tuple[int,int,int], bool], pygame.Surface] = {}

# Cached hurtbox sprites.  Always-on hurtboxes need to be readable, but creating
# a translucent Surface per box per frame tanks FPS.  Cache by screen radius,
# color, and highlight state so the draw path is a cheap blit.
_hurt_surface_cache: Dict[Tuple[int, Tuple[int,int,int], bool, bool, bool], pygame.Surface] = {}

def _get_cached_hurtbox_surface(rpx: int, color: Tuple[int,int,int], highlight: bool, detail: bool = True, invuln: bool = False) -> pygame.Surface:
    """Cached bright-outline hurtbox sprite.

    Cyan = normal hurtbox, yellow = contacted/highlighted, green = startup
    invulnerability currently active for the owning move. Geometry is unchanged;
    this is only paint style.
    """
    rpx = max(2, int(rpx))
    rpx = max(2, int(round(rpx / 2.0) * 2))

    if highlight:
        rim = (255, 232, 100)
        halo = (255, 247, 180)
        inner = (255, 255, 255)
        key = (rpx, rim, True, bool(detail), False)
    elif invuln:
        rim = (88, 255, 130)
        halo = (170, 255, 190)
        inner = (240, 255, 244)
        key = (rpx, rim, False, bool(detail), True)
    else:
        rim = color[:3] if color else (95, 235, 255)
        halo = tuple(min(255, c + 25) for c in rim)
        inner = tuple(min(255, c + 120) for c in rim)
        key = (rpx, rim, False, bool(detail), False)

    surf = _hurt_surface_cache.get(key)
    if surf is not None:
        return surf

    scale = 2
    pad = 8
    size = (rpx * 2 + pad * 2 + 8) * scale
    hi = pygame.Surface((size, size), pygame.SRCALPHA)
    cx = cy = size // 2
    rr = rpx * scale

    if highlight:
        pygame.draw.circle(hi, (*halo, 50), (cx, cy), rr + 5 * scale, 2 * scale)
        pygame.draw.circle(hi, (*rim, 255), (cx, cy), rr, 3 * scale)
        pygame.draw.circle(hi, (*inner, 210), (cx, cy), max(2, rr - 4 * scale), 1 * scale)
    else:
        outline_w = 2 * scale if detail else 1 * scale
        halo_alpha = 30 if detail else 12
        rim_alpha = 220 if detail else 108
        inner_alpha = 92 if detail else 44
        pygame.draw.circle(hi, (*halo, halo_alpha), (cx, cy), rr + 3 * scale, outline_w)
        pygame.draw.circle(hi, (*rim, rim_alpha), (cx, cy), rr, outline_w)
        pygame.draw.circle(hi, (*inner, inner_alpha), (cx, cy), max(2, rr - 4 * scale), 1 * scale)

    # Minimal center reticle.
    c = (4 if highlight else (3 if detail else 2)) * scale
    a = 225 if highlight else (125 if detail else 68)
    pygame.draw.line(hi, (255, 255, 255, a), (cx - c, cy), (cx + c, cy), 1 * scale)
    pygame.draw.line(hi, (255, 255, 255, a), (cx, cy - c), (cx, cy + c), 1 * scale)

    try:
        surf = pygame.transform.smoothscale(hi, (size // scale, size // scale))
    except Exception:
        surf = pygame.transform.scale(hi, (size // scale, size // scale))
    _hurt_surface_cache[key] = surf
    return surf

def slot_passive_override(name: str, state_id: int) -> bool:
    return is_passive_state(state_id)
def _get_cached_hitbox_surface(rpx: int, color: Tuple[int,int,int], active: bool):
    key = (rpx, color, active)
    if key in _surface_cache:
        return _surface_cache[key]

    pad = 6
    size = rpx * 2 + pad * 2
    surf = pygame.Surface((size, size), pygame.SRCALPHA)

    cx = cy = rpx + pad
    r_c, g_c, b_c = color

    if active:
        pygame.draw.circle(surf, (r_c, g_c, b_c, 110), (cx, cy), rpx)
        pygame.draw.circle(surf, (r_c, g_c, b_c, 220), (cx, cy), rpx, 2)
    else:
        pygame.draw.circle(surf, (r_c, g_c, b_c, 55), (cx, cy), rpx)

    _surface_cache[key] = surf
    return surf

# ----------------------------
# Projectile scanner (kept, just not wired into main loop)
# ----------------------------

class ProjectileScanner:
    def __init__(self):
        self._radius_addrs: List[int] = []
        self._scan_count: int = 0

    # def scan(self) -> int:
    #     found: List[int] = []
    #     for base_addr in range(PROJ_SCAN_START, PROJ_SCAN_END, PROJ_SCAN_BLOCK):
    #         data = rbytes(base_addr, PROJ_SCAN_BLOCK)
    #         if not data:
    #             continue
    #         idx = data.find(PROJ_SIG)
    #         while idx != -1:
    #             sig_addr = base_addr + idx
    #             radius_addr = sig_addr + PROJ_RADIUS_OFF
    #             r = _rf(radius_addr)
    #             if 0.0 < r < 20.0:
    #                 found.append(radius_addr)
    #             idx = data.find(PROJ_SIG, idx + 1)
    #     self._radius_addrs = found
    #     self._scan_count += 1
    #     print(f"[ProjectileScanner] scan #{self._scan_count}: {len(found)} radius address(es) found")
    #     for a in found:
    #         print(f"  radius_addr=0x{a:08X}  r={_rf(a):.4f}")
    #     return len(found)

    def scan(self) -> int:
        print("[ProjectileScanner] signature scan disabled (using node watcher instead)")
        return 0

    def dump(self, max_hits: int = 3) -> None:
        print("[ProjectileScanner.dump] signature scan disabled  -  use NodeWatcher.dump() (F3) instead")

    @property
    def radius_addrs(self) -> List[int]:
        return self._radius_addrs

    @property
    def scan_count(self) -> int:
        return self._scan_count


# ----------------------------
# Multi-offset node watcher
# ----------------------------

@dataclass
class ProjectileNodeState:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    dim_0: float = 0.0
    dim_1: float = 0.0
    dim_2: float = 0.0
    actor_ptr: int = 0
    inactive_frames: int = 0
    active: bool = False


@dataclass
class ProjectileActorState:
    actor: int
    owner: int
    owner_slot: str
    proj_id: int
    x: float
    y: float
    z: float
    prev_x: float
    prev_y: float
    prev_z: float
    sweep_x: float
    sweep_y: float
    sweep_z: float
    radius: float
    linked_record: int = 0
    anchor_source: str = "root"
    root_x: float = 0.0
    root_y: float = 0.0
    root_z: float = 0.0
    impact_x: float = 0.0
    impact_y: float = 0.0
    impact_z: float = 0.0
    contact_valid: bool = False
    target_ptr: int = 0
    dir_x: float = 0.0
    dir_y: float = 0.0
    dir_z: float = 0.0
    hit_start_x: float = 0.0
    hit_start_y: float = 0.0
    hit_start_z: float = 0.0
    hit_end_x: float = 0.0
    hit_end_y: float = 0.0
    hit_end_z: float = 0.0
    extent: float = 0.0

    @property
    def has_sweep(self) -> bool:
        # Disabled: actor +0xE0/+0xE4 is not collision sweep in live tests.
        return False

    def label(self, debug: bool = False) -> str:
        if debug:
            return (
                f"PRJ {self.owner_slot}:0x{self.proj_id:X} "
                f"{self.anchor_source} @0x{self.actor:08X}"
            )
        return f"PRJ {self.owner_slot}:0x{self.proj_id:X}@{self.actor & 0xFFFF:04X}"


@dataclass
class HurtboxState:
    slot_name: str
    slot_base: int
    index: int
    desc_addr: int
    matrix_ptr: int
    local_x: float
    local_y: float
    local_z: float
    x: float
    y: float
    z: float
    radius: float
    raw_type: int = 0

    def label(self) -> str:
        return f"HURT {self.slot_name}[{self.index}]"


@dataclass
class HitContactState:
    event_addr: int
    source: int
    source_slot: str
    target: int
    target_slot: str
    x: float
    y: float
    z: float
    dir_x: float = 0.0
    dir_y: float = 0.0

    def label(self) -> str:
        return f"HIT {self.source_slot}>{self.target_slot}"


class ProjectileNodeTracker:
    def __init__(self, pool_count: int):
        self._nodes: Dict[int, ProjectileNodeState] = {
            i: ProjectileNodeState() for i in range(pool_count)
        }

    def update_from_node(self, node_idx: int, node_addr: int) -> None:
        state = self._nodes[node_idx]

        x = _rf(node_addr + PROJ_OFF_X)
        y = _rf(node_addr + PROJ_OFF_Y)
        z = _rf(node_addr + PROJ_OFF_Z)

        dim_0 = _clean_dim(_rf(node_addr + PROJ_OFF_DIM_0))
        dim_1 = _clean_dim(_rf(node_addr + PROJ_OFF_DIM_1))
        dim_2 = _clean_dim(_rf(node_addr + PROJ_OFF_DIM_2))

        actor_ptr = 0
        for off in PROJ_PTR_CANDIDATES:
            p = rd32(node_addr + off)
            if _looks_like_ptr(p):
                actor_ptr = p
                break

        has_any_dim = (dim_0 > 0.0) or (dim_1 > 0.0) or (dim_2 > 0.0)
        sane_pos = abs(x) < 30 and abs(y) < 30 and abs(z) < 30

        if sane_pos and has_any_dim:

            state.x = x
            state.y = y
            state.z = z

            state.dim_0 = dim_0
            state.dim_1 = dim_1
            state.dim_2 = dim_2

            state.actor_ptr = actor_ptr
            state.inactive_frames = 0
            state.active = True

        else:

            state.inactive_frames += 1

            if state.inactive_frames >= PROJECTILE_DESPAWN_FRAMES:
                state.active = False
                state.dim_0 = 0.0
                state.dim_1 = 0.0
                state.dim_2 = 0.0
                state.actor_ptr = 0

    def visible_nodes(self) -> List[ProjectileNodeState]:
        return [s for s in self._nodes.values() if s.active]

    def actor_clusters(self):

        clusters = []
        threshold = 0.6

        for s in self._nodes.values():

            if not s.active:
                continue

            placed = False

            for cluster in clusters:

                ref = cluster[0]

                dx = s.x - ref.x
                dy = s.y - ref.y
                dz = s.z - ref.z

                dist = abs(dx) + abs(dy) + abs(dz)

                if dist < threshold:
                    cluster.append(s)
                    placed = True
                    break

            if not placed:
                clusters.append([s])

        return clusters
    def dump_active(self, max_nodes: int = 8) -> None:
        active = [(idx, s) for idx, s in self._nodes.items() if s.active]
        if not active:
            print("[NodeWatcher.dump] no active nodes")
            return

        print()
        print("[NodeWatcher.dump] active projectile nodes:")
        print(" idx        actor_ptr        x        y        z      d0      d1      d2")
        for idx, s in active[:max_nodes]:
            print(
                f"{idx:4d}   0x{s.actor_ptr:08X}   "
                f"{s.x:7.3f} {s.y:7.3f} {s.z:7.3f}   "
                f"{s.dim_0:5.3f} {s.dim_1:5.3f} {s.dim_2:5.3f}"
            )
        print()


# ----------------------------
# Translation tracker
# ----------------------------

@dataclass
class HitboxMotionState:
    still_frames: int = 0
    motion_frames: int = 0
    suppressed: bool = False
    prev_x: float = 0.0
    prev_y: float = 0.0
    initialized: bool = False


class MotionFilter:
    def __init__(self):
        self._states: Dict[Tuple[str, int], HitboxMotionState] = {}

    def _key(self, slot: str, idx: int) -> Tuple[str, int]:
        return (slot, idx)
    def cleanup(self):
        self._states = {
            k: v for k, v in self._states.items()
            if not v.suppressed or v.motion_frames > 0
        }
    def update(self, slot: str, idx: int, x: float, y: float, r: float) -> bool:
        key = self._key(slot, idx)
        if key not in self._states:
            self._states[key] = HitboxMotionState()
        state = self._states[key]

        if r <= 0.001:
            state.still_frames = 0
            state.motion_frames = 0
            state.suppressed = False
            state.initialized = False
            return False

        if not state.initialized:
            state.prev_x = x
            state.prev_y = y
            state.initialized = True
            state.still_frames = 0
            state.motion_frames = MOTION_FRAME_REQUIRED
            state.suppressed = False
            return True

        dx = x - state.prev_x
        dy = y - state.prev_y
        delta = math.sqrt(dx * dx + dy * dy)
        state.prev_x = x
        state.prev_y = y

        if delta >= MOTION_THRESHOLD:
            state.still_frames = 0
            state.motion_frames = min(state.motion_frames + 1, MOTION_FRAME_REQUIRED + 1)
            if state.suppressed and state.motion_frames >= MOTION_FRAME_REQUIRED:
                state.suppressed = False
        else:
            state.motion_frames = 0
            state.still_frames = min(state.still_frames + 1, STILL_FRAME_LIMIT + 1)
            if not state.suppressed and state.still_frames >= STILL_FRAME_LIMIT:
                state.suppressed = True

        return not state.suppressed

    def reset_slot(self, slot: str, count: int):
        for i in range(count):
            key = self._key(slot, i)
            if key in self._states:
                del self._states[key]


# ----------------------------
# Memory helpers
# ----------------------------

def rb(addr: int) -> int:
    v = rd32(addr & ~3)
    if v is None:
        return 0
    shift = (3 - (addr & 3)) * 8
    return (v >> shift) & 0xFF


def _rf(addr: int) -> float:
    v = rd32(addr)
    if v is None:
        return 0.0
    try:
        f = struct.unpack(">f", struct.pack(">I", v))[0]
        return f if math.isfinite(f) else 0.0
    except Exception:
        return 0.0


rf = _rf

_SENTINEL_BITS: frozenset = frozenset({0x3F7FFFFE, 0x3F800000})
_DIM_MAX: float = 2.0


def _clean_dim(v: float) -> float:

    if not math.isfinite(v):
        return 0.0

    if v < 0.01:
        return 0.0

    if v > 3.0:
        return 0.0

    return v


def _looks_like_ptr(v: int) -> bool:
    if v is None:
        return False
    if v < 0x90000000:
        return False
    if v > 0x94000000:
        return False
    if (v & 3) != 0:
        return False
    return True


def read_hitboxes(slot_base: int, layout: HitboxLayout):
    base = slot_base + layout.struct_shift
    out = []
    flag = rb(base + layout.off_flag)
    for b in layout.blocks:
        x = _rf(base + b + layout.off_x)
        y = _rf(base + b + layout.off_y)
        r = _rf(base + b + layout.off_r)
        out.append((x, y, r, flag))
    return out


def _valid_matrix_ptr(ptr: int) -> bool:
    ptr = int(ptr or 0) & 0xFFFFFFFF
    return 0x80000000 <= ptr < 0x81800000 and (ptr & 3) == 0


def _read_matrix_3x4(matrix_ptr: int) -> Optional[Tuple[float, ...]]:
    vals: List[float] = []
    for off in range(0, 0x30, 4):
        raw = rd32(matrix_ptr + off)
        if raw is None:
            return None
        try:
            value = struct.unpack(">f", struct.pack(">I", int(raw) & 0xFFFFFFFF))[0]
        except Exception:
            return None
        if not math.isfinite(value):
            return None
        vals.append(value)
    return tuple(vals)


def _valid_bone_matrix(matrix_ptr: int) -> bool:
    """Reject uninitialized/stale bone matrices without rejecting large fighters.

    PTX's live giant-body matrices still have normal-sized orthonormal rows.
    A parked teammate in the supplied dump instead has zero rows, a 59.0 row,
    and an absurd translation.  Those are descriptor leftovers, not renderable
    body geometry.
    """
    values = _read_matrix_3x4(matrix_ptr)
    if values is None:
        return False
    rows = ((values[0], values[1], values[2]),
            (values[4], values[5], values[6]),
            (values[8], values[9], values[10]))
    for row in rows:
        norm = math.sqrt(sum(component * component for component in row))
        if not (HURTBOX_MATRIX_MIN_ROW_NORM <= norm <= HURTBOX_MATRIX_MAX_ROW_NORM):
            return False
    if any(abs(values[idx]) > HURTBOX_MATRIX_MAX_TRANSLATION for idx in (3, 7, 11)):
        return False
    # A body matrix must span 3D space.  This rejects all-zero / collapsed rows
    # while allowing normal mirrored bones.
    det = (
        values[0] * ((values[5] * values[10]) - (values[6] * values[9]))
        - values[1] * ((values[4] * values[10]) - (values[6] * values[8]))
        + values[2] * ((values[4] * values[9]) - (values[5] * values[8]))
    )
    return 0.015 <= abs(det) <= 16.0


def _matrix_transform_point(matrix_ptr: int, lx: float, ly: float, lz: float) -> Tuple[float, float, float]:
    # Live bone matrices are row-major 3x4:
    #   [r00 r01 r02 tx]
    #   [r10 r11 r12 ty]
    #   [r20 r21 r22 tz]
    x = (_rf(matrix_ptr + 0x00) * lx) + (_rf(matrix_ptr + 0x04) * ly) + (_rf(matrix_ptr + 0x08) * lz) + _rf(matrix_ptr + 0x0C)
    y = (_rf(matrix_ptr + 0x10) * lx) + (_rf(matrix_ptr + 0x14) * ly) + (_rf(matrix_ptr + 0x18) * lz) + _rf(matrix_ptr + 0x1C)
    z = (_rf(matrix_ptr + 0x20) * lx) + (_rf(matrix_ptr + 0x24) * ly) + (_rf(matrix_ptr + 0x28) * lz) + _rf(matrix_ptr + 0x2C)
    return x, y, z


def _sane_world_box(x: float, y: float, z: float, r: float) -> bool:
    return (
        math.isfinite(x) and math.isfinite(y) and math.isfinite(z) and math.isfinite(r)
        and abs(x) < 40.0 and abs(y) < 40.0 and abs(z) < 40.0
        and HURTBOX_MIN_RADIUS <= r <= HURTBOX_MAX_RADIUS
    )


def read_hurtboxes(slot_name: str, slot_base: int) -> List[HurtboxState]:
    out: List[HurtboxState] = []
    for i in range(HURTBOX_DESC_COUNT):
        desc = slot_base + HURTBOX_DESC_BASE + i * HURTBOX_DESC_STRIDE
        raw_type = rd32(desc) or 0
        matrix_ptr = rd32(desc + 0x04) or 0

        # A blank descriptor means the current list is done.
        if raw_type == 0 and matrix_ptr == 0:
            break
        if not _valid_matrix_ptr(matrix_ptr):
            continue
        if not _valid_bone_matrix(matrix_ptr):
            continue

        lx = _rf(desc + 0x08)
        ly = _rf(desc + 0x0C)
        lz = _rf(desc + 0x10)
        r = _rf(desc + 0x14)
        x, y, z = _matrix_transform_point(matrix_ptr, lx, ly, lz)
        if not _sane_world_box(x, y, z, r):
            continue

        out.append(HurtboxState(
            slot_name=slot_name,
            slot_base=slot_base,
            index=i,
            desc_addr=desc,
            matrix_ptr=matrix_ptr,
            local_x=lx,
            local_y=ly,
            local_z=lz,
            x=x,
            y=y,
            z=z,
            radius=r,
            raw_type=raw_type,
        ))
    return out


def _slot_has_coherent_hurt_rig(slot_base: int, hlist: List[HurtboxState]) -> bool:
    """Return whether a slot currently owns a renderable on-stage body rig.

    This is intentionally geometry-based rather than move-ID based: assist
    standby can use the same idle action ID as the active fighter, but its bone
    list is partial/stale.  A real body has several sane descriptors clustered
    around the fighter root.
    """
    min_required = max(4, min(HURTBOX_MIN_COHERENT_DESCRIPTORS, len(hlist)))
    if len(hlist) < min_required:
        return False
    try:
        char_id = int(rd32(slot_base + OFF_CHAR_ID) or 0)
        if char_id <= 0 or char_id == 0xFFFFFFFF:
            return False
        # Do not hard-reject by +0x04 activity here.
        # Giant/solo layouts can place the visible opponent in a non-zero slot,
        # while true standby/parked partners are already rejected by the root
        # sentinel/offscreen tests below.
        root_x, root_y, root_z = read_fighter_root(slot_base)
    except Exception:
        return False
    if not all(math.isfinite(v) for v in (root_x, root_y, root_z)):
        return False
    # The common parked/off-stage sentinel is 90,90.
    if abs(root_y) >= 70.0 or abs(root_z) >= 70.0:
        return False

    coherent = 0
    for hb in hlist:
        if (abs(hb.x - root_x) <= HURTBOX_COHERENT_XY_RANGE
                and abs(hb.y - root_y) <= HURTBOX_COHERENT_XY_RANGE
                and abs(hb.z - root_z) <= HURTBOX_COHERENT_Z_RANGE):
            coherent += 1
    return coherent >= min_required


def _slot_name_from_base(ptr: int) -> Optional[str]:
    ptr = int(ptr or 0) & 0xFFFFFFFF
    for name, base in SLOT_BASES.items():
        if ptr == int(base):
            return name
    return None


def read_hit_contacts() -> List[HitContactState]:
    # Normal hit resolution records show source/target at +0x30/+0x34 and
    # resolved contact-ish point at +0x5C/+0x60.  +0x48 is 0 while the contact
    # is live, then returns to FFFFFFFF when the record is stale.
    out: List[HitContactState] = []
    seen = set()
    for addr in range(HIT_EVENT_SCAN_START, HIT_EVENT_SCAN_END, 4):
        source = rd32(addr + 0x30) or 0
        target = rd32(addr + 0x34) or 0
        source_slot = _slot_name_from_base(source)
        target_slot = _slot_name_from_base(target)
        if source_slot is None or target_slot is None or source == target:
            continue
        if (rd32(addr + 0x48) or 0) != 0:
            continue
        x = _rf(addr + 0x5C)
        y = _rf(addr + 0x60)
        z = _rf(addr + 0x64)
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            continue
        if abs(x) > 40.0 or abs(y) > 40.0 or abs(z) > 40.0:
            continue
        key = (source, target, round(x, 3), round(y, 3))
        if key in seen:
            continue
        seen.add(key)
        out.append(HitContactState(
            event_addr=addr,
            source=source,
            source_slot=source_slot,
            target=target,
            target_slot=target_slot,
            x=x,
            y=y,
            z=z,
            dir_x=_rf(addr + 0x6C),
            dir_y=_rf(addr + 0x70),
        ))
    return out


def hurtbox_contains_contact(hurtbox: HurtboxState, contact: HitContactState) -> bool:
    return math.hypot(hurtbox.x - contact.x, hurtbox.y - contact.y) <= (hurtbox.radius + HURTBOX_CONTACT_PAD)


def classify_hurtbox_regions(hlist: List[HurtboxState]) -> Dict[str, Any]:
    """Pick a compact body-region subset from the live skeletal circles.

    The descriptor order is not a stable semantic order, so this works from
    screen/world position: highest = head, central large zones = torso/pelvis,
    outer upper zones = arms, outer lower zones = legs. It is a display grouping
    only; it does not alter the actual collision geometry.
    """
    if not hlist:
        return {"region_by_index": {}, "major_indices": set(), "label_by_index": {}}

    weight = sum(max(0.05, float(hb.radius)) for hb in hlist)
    center_x = (
        sum(float(hb.x) * max(0.05, float(hb.radius)) for hb in hlist) / weight
        if weight > 0.0 else sum(float(hb.x) for hb in hlist) / float(len(hlist))
    )
    ys = [float(hb.y) for hb in hlist]
    top_y, bottom_y = max(ys), min(ys)
    span = max(0.25, top_y - bottom_y)
    arm_threshold = max(0.14, max(abs(float(hb.x) - center_x) for hb in hlist) * 0.50)

    used: set[int] = set()

    def choose(candidates: List[HurtboxState], score):
        best = None
        best_value = None
        for hb in candidates:
            if hb.index in used:
                continue
            value = score(hb)
            if best is None or value > best_value:
                best, best_value = hb, value
        if best is not None:
            used.add(best.index)
        return best

    head = choose(list(hlist), lambda hb: float(hb.y) + float(hb.radius) * 0.25)
    torso_band = [hb for hb in hlist if bottom_y + span * 0.48 <= float(hb.y) <= bottom_y + span * 0.84]
    torso = choose(torso_band or list(hlist), lambda hb: float(hb.radius) * 2.0 - abs(float(hb.x) - center_x))
    pelvis_band = [hb for hb in hlist if bottom_y + span * 0.25 <= float(hb.y) <= bottom_y + span * 0.60]
    pelvis = choose(pelvis_band or list(hlist), lambda hb: float(hb.radius) * 2.0 - abs(float(hb.x) - center_x))

    left_arm_pool = [hb for hb in hlist if float(hb.x) < center_x - arm_threshold and float(hb.y) > bottom_y + span * 0.36]
    right_arm_pool = [hb for hb in hlist if float(hb.x) > center_x + arm_threshold and float(hb.y) > bottom_y + span * 0.36]
    left_arm = choose(left_arm_pool or [hb for hb in hlist if float(hb.x) < center_x], lambda hb: abs(float(hb.x) - center_x) + float(hb.radius) * 0.15)
    right_arm = choose(right_arm_pool or [hb for hb in hlist if float(hb.x) > center_x], lambda hb: abs(float(hb.x) - center_x) + float(hb.radius) * 0.15)

    left_leg_pool = [hb for hb in hlist if float(hb.x) < center_x - arm_threshold * 0.35 and float(hb.y) < bottom_y + span * 0.48]
    right_leg_pool = [hb for hb in hlist if float(hb.x) > center_x + arm_threshold * 0.35 and float(hb.y) < bottom_y + span * 0.48]
    left_leg = choose(left_leg_pool or [hb for hb in hlist if float(hb.x) < center_x], lambda hb: (bottom_y + span * 0.52 - float(hb.y)) + float(hb.radius) * 0.10)
    right_leg = choose(right_leg_pool or [hb for hb in hlist if float(hb.x) > center_x], lambda hb: (bottom_y + span * 0.52 - float(hb.y)) + float(hb.radius) * 0.10)

    chosen = {
        "head": head,
        "torso": torso,
        "pelvis": pelvis,
        "arm_l": left_arm,
        "arm_r": right_arm,
        "leg_l": left_leg,
        "leg_r": right_leg,
    }
    pretty = {
        "head": "HEAD", "torso": "TORSO", "pelvis": "PELVIS",
        "arm_l": "ARM", "arm_r": "ARM", "leg_l": "LEG", "leg_r": "LEG",
    }
    region_by_index: Dict[int, str] = {}
    label_by_index: Dict[int, str] = {}
    for region, hb in chosen.items():
        if hb is None:
            continue
        region_by_index[hb.index] = region
        label_by_index[hb.index] = pretty[region]

    # Give the detailed/debug modes a section tint for every remaining circle.
    for hb in hlist:
        if hb.index in region_by_index:
            continue
        y_norm = (float(hb.y) - bottom_y) / span
        dx = float(hb.x) - center_x
        if y_norm >= 0.84:
            region = "head"
        elif y_norm <= 0.34:
            region = "leg_l" if dx < 0.0 else "leg_r"
        elif abs(dx) >= arm_threshold and y_norm >= 0.35:
            region = "arm_l" if dx < 0.0 else "arm_r"
        elif y_norm <= 0.54:
            region = "pelvis"
        else:
            region = "torso"
        region_by_index[hb.index] = region

    return {
        "region_by_index": region_by_index,
        "major_indices": set(label_by_index),
        "label_by_index": label_by_index,
    }


def should_draw_reaction_hurtboxes(slot_base: int) -> bool:
    state_id = read_state_id(slot_base)
    return state_id in HURTBOX_REACTION_STATE_IDS


def read_and_draw_hurtboxes_for_slot(overlay, slot_name: str, slot_base: int, contacts=None) -> int:
    contacts = contacts or []
    hurt_palette = COLORS.get(slot_name, [(120, 220, 255)])
    drawn = 0
    for hurt in read_hurtboxes(slot_name, slot_base):
        highlight = any(hurtbox_contains_contact(hurt, contact) for contact in contacts)
        color = hurt_palette[hurt.index % len(hurt_palette)]
        if not highlight:
            color = _dim_hitbox_color(color)
        overlay.draw_hurtbox(
            hurt.x, hurt.y, hurt.z, hurt.radius, color, hurt.label(), highlight=highlight
        )
        drawn += 1
    return drawn


def read_fighter_root(slot_base: int):
    return _rf(slot_base + 0xB0), _rf(slot_base + 0xB4), _rf(slot_base + 0xB8)


def read_camera_pos(layout: CameraLayout):
    return (
        _rf(layout.base + layout.off_x),
        _rf(layout.base + layout.off_y),
        _rf(layout.base + layout.off_z),
        _rf(layout.base + layout.off_w),
    )


def read_camera_view_affine(layout: CameraLayout) -> Tuple[float, float, float, float, float, float]:
    """Read the live Y/Z camera rows.

    The first row maps world Y/Z into screen Y. The second row maps world Y/Z
    into camera-space depth. Giant bodies have much larger Z spread, so their
    hurtboxes drift unless the per-point depth row is applied too.
    """
    return (
        _rf(layout.base + CAMERA_VIEW_COS_OFF),
        _rf(layout.base + CAMERA_VIEW_SIN_OFF),
        _rf(layout.base + CAMERA_VIEW_Y_TRANSLATE_OFF),
        _rf(layout.base + CAMERA_DEPTH_Y_COEFF_OFF),
        _rf(layout.base + CAMERA_DEPTH_Z_COEFF_OFF),
        _rf(layout.base + CAMERA_DEPTH_TRANSLATE_OFF),
    )


def update_projectile_nodes(tracker):

    node_idx = 0
    pools = resolve_projectile_pools()

    for pool in pools:

        for i in range(PROJECTILE_NODE_COUNT):

            node_addr = pool + i * PROJECTILE_NODE_STRIDE
            tracker.update_from_node(node_idx, node_addr)

            node_idx += 1

# ----------------------------
# Win32 helpers
# ----------------------------

def find_dolphin_hwnd() -> Optional[int]:
    candidates: List[Tuple[int, int, str]] = []

    def score_title(t: str) -> int:
        tl = t.lower()
        if "dolphin" not in tl:
            return -10_000
        s = 0
        if "|" in t:
            s += 50
            s += min(30, t.count("|") * 5)
        for tok in ("jit", "jit64", "opengl", "vulkan", "d3d", "direct3d", "hle"):
            if tok in tl:
                s += 20
        if "(" in t and ")" in t:
            s += 30
        for bad in ("memory", "watch", "log", "breakpoint", "register", "disassembly", "config", "settings"):
            if bad in tl:
                s -= 25
        if t.count("|") >= 3:
            s += 20
        return s

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        if not title:
            return
        if "dolphin" not in title.lower():
            return
        candidates.append((score_title(title), hwnd, title))

    win32gui.EnumWindows(cb, None)
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def get_client_screen_rect(hwnd: int):
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    tl = win32gui.ClientToScreen(hwnd, (left, top))
    br = win32gui.ClientToScreen(hwnd, (right, bottom))
    return tl[0], tl[1], br[0] - tl[0], br[1] - tl[1]


def apply_overlay_style(hwnd: int) -> None:
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    style &= ~(
        win32con.WS_CAPTION
        | win32con.WS_THICKFRAME
        | win32con.WS_MINIMIZE
        | win32con.WS_MAXIMIZE
        | win32con.WS_SYSMENU
    )
    style |= win32con.WS_POPUP
    win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)

    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    # Overlay must never intercept clicks intended for Dolphin or the main GUI.
    # It is a visual layer only; all control stays in the existing application.
    ex |= (
        win32con.WS_EX_LAYERED
        | win32con.WS_EX_TRANSPARENT
        | win32con.WS_EX_NOACTIVATE
        | win32con.WS_EX_TOOLWINDOW
    )
    ex &= ~win32con.WS_EX_TOPMOST
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)

    win32gui.SetLayeredWindowAttributes(hwnd, 0x000000, 0, win32con.LWA_COLORKEY)
    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_NOTOPMOST,
        0,
        0,
        0,
        0,
        win32con.SWP_FRAMECHANGED
        | win32con.SWP_NOMOVE
        | win32con.SWP_NOSIZE
        | win32con.SWP_NOACTIVATE,
    )


def sync_overlay_to_dolphin(dolphin_hwnd: int, overlay_hwnd: int):
    x, y, w, h = get_client_screen_rect(dolphin_hwnd)
    win32gui.SetWindowPos(
        overlay_hwnd,
        win32con.HWND_NOTOPMOST,
        x,
        y,
        w,
        h,
        win32con.SWP_NOACTIVATE,
    )
    return w, h


def _legend_window_worker() -> None:
    try:
        import tkinter as tk
    except Exception:
        return

    root = tk.Tk()
    root.title("TvC Hitbox Legend")
    root.configure(bg="#101820")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", False)
    except Exception:
        pass

    items = [
        ("#ff7a38", "HIT", "player attack hitbox"),
        ("#79efff", "HURT", "normal hurtbox (body-section colors in clean mode)"),
        ("#6dff8c", "INVUL", "hurtbox while startup invulnerability is active"),
        ("#ffffff", "CONTACT", "resolver contact marker / impact focus"),
        ("#ffb478", "PROJECTILE", "projectile hitbox or sweep"),
    ]

    tk.Label(root, text="Overlay legend", fg="#e8f4ff", bg="#101820", font=("Consolas", 12, "bold")).pack(anchor="w", padx=10, pady=(10, 6))
    tk.Label(root, text="Hitbox view", fg="#b8d8f0", bg="#101820", font=("Consolas", 10, "bold")).pack(anchor="w", padx=10)

    hitbox_mode_row = tk.Frame(root, bg="#101820")
    hitbox_mode_row.pack(anchor="w", padx=10, pady=(4, 8))
    hitbox_mode_buttons = {}

    def _refresh_hitbox_mode_buttons() -> None:
        current = _read_hitbox_view_mode()
        for mode, button in hitbox_mode_buttons.items():
            selected = (mode == current)
            button.configure(
                bg="#2f6f8c" if selected else "#1a2a38",
                fg="#ffffff" if selected else "#b8d8f0",
                activebackground="#3a83a3" if selected else "#253b4d",
                activeforeground="#ffffff",
                relief="sunken" if selected else "raised",
            )

    def _choose_hitbox_mode(mode: str) -> None:
        set_hitbox_view_mode(mode)
        _refresh_hitbox_mode_buttons()

    for _mode, _label in (("live", "Live"), ("ghost", "Ghost")):
        btn = tk.Button(
            hitbox_mode_row,
            text=_label,
            command=lambda m=_mode: _choose_hitbox_mode(m),
            font=("Consolas", 10, "bold"),
            bd=1,
            padx=10,
            pady=3,
            cursor="hand2",
        )
        btn.pack(side="left", padx=(0, 5))
        hitbox_mode_buttons[_mode] = btn
    _refresh_hitbox_mode_buttons()

    tk.Label(root, text="Ghost = prior hitbox positions only", fg="#b8d8f0", bg="#101820", font=("Consolas", 9)).pack(anchor="w", padx=10, pady=(0, 6))

    # Ruler visibility and source-slot filters live in the main command dock.
    # The legend stays read-only for range state so two separate buttons cannot
    # appear to compete for the same persisted toggle.
    tk.Label(
        root,
        text="Range ruler: controlled from the main dock",
        fg="#b8d8f0",
        bg="#101820",
        font=("Consolas", 9),
    ).pack(anchor="w", padx=10, pady=(0, 8))

    tk.Label(root, text="Hurtbox view", fg="#b8d8f0", bg="#101820", font=("Consolas", 10, "bold")).pack(anchor="w", padx=10)

    mode_row = tk.Frame(root, bg="#101820")
    mode_row.pack(anchor="w", padx=10, pady=(4, 8))
    mode_buttons = {}

    def _refresh_mode_buttons() -> None:
        current = _read_hurtbox_view_mode()
        for mode, button in mode_buttons.items():
            selected = (mode == current)
            button.configure(
                bg="#2f6f8c" if selected else "#1a2a38",
                fg="#ffffff" if selected else "#b8d8f0",
                activebackground="#3a83a3" if selected else "#253b4d",
                activeforeground="#ffffff",
                relief="sunken" if selected else "raised",
            )

    def _choose_mode(mode: str) -> None:
        set_hurtbox_view_mode(mode)
        _refresh_mode_buttons()

    for _mode, _label in (("clean", "Clean"), ("detailed", "Detailed"), ("debug", "Debug")):
        btn = tk.Button(
            mode_row,
            text=_label,
            command=lambda m=_mode: _choose_mode(m),
            font=("Consolas", 10, "bold"),
            bd=1,
            padx=10,
            pady=3,
            cursor="hand2",
        )
        btn.pack(side="left", padx=(0, 5))
        mode_buttons[_mode] = btn
    _refresh_mode_buttons()

    tk.Label(root, text="Clean = core sections  •  Detailed = all boxes, helpers dim  •  Debug = all labels", fg="#b8d8f0", bg="#101820", font=("Consolas", 9)).pack(anchor="w", padx=10, pady=(0, 8))

    body = tk.Frame(root, bg="#101820")
    body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    for color, name, desc in items:
        row = tk.Frame(body, bg="#101820")
        row.pack(anchor="w", fill="x", pady=2)
        sw = tk.Canvas(row, width=18, height=18, bg="#101820", highlightthickness=0, bd=0)
        sw.create_oval(2, 2, 16, 16, outline=color, width=3)
        sw.pack(side="left")
        tk.Label(row, text=name, fg="#f4fbff", bg="#101820", font=("Consolas", 10, "bold"), width=10, anchor="w").pack(side="left", padx=(8, 0))
        tk.Label(row, text=desc, fg="#d6e5ef", bg="#101820", font=("Consolas", 10), anchor="w").pack(side="left", padx=(6, 0))

    shown = False

    def _manual_hide() -> None:
        global _legend_manual_hidden
        _legend_manual_hidden = True
        root.withdraw()

    def _sync_visibility() -> None:
        nonlocal shown
        effective = bool(_legend_visible_requested and not _legend_manual_hidden)
        # Only change visibility when the overlay toggle changes.  Never call
        # lift(), never force-focus, and never resurrect a manually hidden or
        # minimized legend every frame.
        if effective != shown:
            try:
                if effective:
                    root.deiconify()
                else:
                    root.withdraw()
            except Exception:
                pass
            shown = effective
        try:
            _refresh_mode_buttons()
        except Exception:
            pass
        root.after(200, _sync_visibility)

    root.withdraw()
    root.protocol("WM_DELETE_WINDOW", _manual_hide)
    root.after(0, _sync_visibility)
    root.mainloop()


def set_legend_window_visible(visible: bool) -> None:
    global _legend_window_started, _legend_visible_requested, _legend_manual_hidden
    was_visible = bool(_legend_visible_requested)
    _legend_visible_requested = bool(visible)
    # Turning the overlay off resets a manual hide.  The next explicit overlay
    # enable can show the legend again, but it never reappears while still on.
    if not _legend_visible_requested:
        _legend_manual_hidden = False
        return
    if not was_visible:
        _legend_manual_hidden = False
    if _legend_window_started:
        return
    _legend_window_started = True
    try:
        threading.Thread(target=_legend_window_worker, name="hitbox_legend_window", daemon=True).start()
    except Exception:
        _legend_window_started = False


def _read_live_protection_state(slot_base: int) -> tuple[int, int, int]:
    """Return exact live (exclusion frames, state flags, protection flags).

    +0x244 contains both height-resolver masks and the 0x00040000 hitstun
    bypass bit. Returning the full words lets the caller distinguish counters,
    no-damage guard points, and damage-taking armor instead of collapsing all
    three into one generic guard mask.
    """
    try:
        raw_timer = int(rd32(int(slot_base) + OFF_COLLISION_EXCLUSION) or 0) & 0xFFFFFFFF
        state_flags = int(rd32(int(slot_base) + OFF_STATE_FLAGS) or 0) & 0xFFFFFFFF
        protection_flags = int(rd32(int(slot_base) + OFF_GUARD_MASK) or 0) & 0xFFFFFFFF
        remaining = (raw_timer + 0xFF) // 0x100 if raw_timer else 0
        if remaining > 180:
            remaining = 0
        return int(remaining), int(state_flags), int(protection_flags)
    except Exception:
        return 0, 0, 0

def _guard_mask_short(mask: int) -> str:
    parts = []
    if int(mask) & GUARD_HIGH_BIT:
        parts.append("H")
    if int(mask) & GUARD_MID_BIT:
        parts.append("M")
    if int(mask) & GUARD_LOW_BIT:
        parts.append("L")
    return "+".join(parts)


def _read_live_invuln_frames(slot_base: int) -> int:
    """Read the active action's +0x1218 invul signature without profile cache.

    This is intentionally a small direct read so the hitbox layer stays correct
    even while Frame Data's optional profile work is still running.
    """
    try:
        chr_tbl = rd32(slot_base + OFF_CHR_TBL) or 0
        action_id = decode_state_id(read_state_raw(slot_base))
        if not (0x90000000 <= chr_tbl <= 0x94000000) or action_id <= 0 or action_id > 0x1FFF:
            return 0
        rel = rd32(chr_tbl + (int(action_id) * 4)) or 0
        if rel <= 0 or rel > 0x00400000:
            return 0
        root = chr_tbl + rel
        # Shoryu stores the header 8 bytes before its action root; Jun 6B has
        # it inside the action section.  Scan both layouts in one compact read.
        block = rbytes(max(0, root - 8), 0x900)
        if not block:
            return 0
        sig = b"\x04\x01\x60\x00\x00\x00\x12\x18\x3F\x00\x00\x00"
        best = 0
        pos = 0
        while True:
            idx = block.find(sig, pos)
            if idx < 0:
                break
            pos = idx + 1
            if idx + 16 > len(block):
                continue
            raw = int.from_bytes(block[idx + 12:idx + 16], "big", signed=False)
            frames = (raw >> 8) & 0xFFFF if (raw & 0xFF) == 0 else (raw & 0xFFFF)
            if 3 <= frames <= 120:
                best = max(best, frames)
        return best
    except Exception:
        return 0





# ----------------------------
# Overlay
# ----------------------------

class Overlay:
    def __init__(self, cfg: DisplayConfig):
        self.cfg = cfg
        self.cam_x = 0.0
        self.cam_y = 0.0
        self.cam_z = 0.0
        self.ref_cam_z = None
        self.cam_view_cos = 1.0
        self.cam_view_sin = 0.0
        self.cam_view_y_translate = 0.0
        self.cam_depth_y_coeff = 0.0
        self.cam_depth_z_coeff = 1.0
        self.cam_depth_translate = 0.0
        self.cam_view_affine_valid = False
        self.cam_depth_affine_valid = False
        self.giant_x_anchor: Optional[float] = None
        self.ppu = cfg.baseline_ppu
        self.zoom = cfg.zoom
        self.w = cfg.baseline_w
        self.h = cfg.baseline_h
        self.cx = self.w // 2
        self.cy = self.h // 2 + cfg.center_y_offset_px
        self.screen: Optional[pygame.Surface] = None
        self.font_small: Optional[pygame.font.Font] = None
        self.font_hud: Optional[pygame.font.Font] = None
        self.debug_axes = cfg.show_debug_axes
        self.viewport_scale = self.h / 720.0
        self.aspect_ratio = self.w / float(self.h)
        self.base_aspect = 4.0 / 3.0
        self.x_aspect_scale = self.base_aspect / self.aspect_ratio
        self.window_aspect = self.w / float(self.h)
        self.stretch_factor = self.window_aspect / self.base_aspect

        self.frame_index = 0
        self.hitbox_spawn_start: Dict[str, int] = {}
        self.hitbox_last_seen: Dict[str, int] = {}

    def init_pygame(self) -> int:
        pygame.init()
        self.font_small = pygame.font.SysFont("consolas", 11)
        self.font_hud = pygame.font.SysFont("consolas", 13, bold=True)
        self.screen = pygame.display.set_mode((self.w, self.h), pygame.SRCALPHA)
        pygame.display.set_caption("TvC Hitbox Overlay v3")

        icon_path = os.path.join("assets", "portraits", "Placeholder.png")
        if not os.path.exists(icon_path):
            icon_path = os.path.join("assets", "icon.png")
        if os.path.exists(icon_path):
            icon = pygame.image.load(icon_path).convert_alpha()
            pygame.display.set_icon(icon)

        hwnd = pygame.display.get_wm_info()["window"]
        apply_overlay_style(hwnd)
        return hwnd

    def on_resize(self, w: int, h: int):
        if w <= 0 or h <= 0:
            return
        if w == self.w and h == self.h:
            return
        self.w, self.h = w, h
        self.screen = pygame.display.set_mode((w, h), pygame.SRCALPHA)
        self.viewport_scale = h / 720.0
        self.aspect_ratio = w / float(h)
        self.base_aspect = 4.0 / 3.0
        self.x_aspect_scale = self.base_aspect / self.aspect_ratio
        self.cx = int(w * 0.5)
        self.cy = int(h * 0.5)
        self.window_aspect = w / float(h)
        self.stretch_factor = self.window_aspect / self.base_aspect

    def set_camera_view_affine(
        self,
        cos_yz: float,
        sin_yz: float,
        translate_y: float,
        depth_y_coeff: float = 0.0,
        depth_z_coeff: float = 1.0,
        depth_translate: float = 0.0,
    ) -> None:
        try:
            valid = (
                math.isfinite(cos_yz) and math.isfinite(sin_yz) and math.isfinite(translate_y)
                and 0.75 <= abs(cos_yz) <= 1.25
                and abs(sin_yz) <= 0.60
            )
        except Exception:
            valid = False
        if valid:
            self.cam_view_cos = float(cos_yz)
            self.cam_view_sin = float(sin_yz)
            self.cam_view_y_translate = float(translate_y)
            self.cam_view_affine_valid = True
        else:
            self.cam_view_cos = 1.0
            self.cam_view_sin = 0.0
            self.cam_view_y_translate = 0.0
            self.cam_view_affine_valid = False

        try:
            depth_valid = (
                math.isfinite(depth_y_coeff) and math.isfinite(depth_z_coeff) and math.isfinite(depth_translate)
                and abs(depth_z_coeff) >= 0.75 and abs(depth_z_coeff) <= 1.25
                and abs(depth_y_coeff) <= 0.60
            )
        except Exception:
            depth_valid = False
        if depth_valid:
            self.cam_depth_y_coeff = float(depth_y_coeff)
            self.cam_depth_z_coeff = float(depth_z_coeff)
            self.cam_depth_translate = float(depth_translate)
            self.cam_depth_affine_valid = True
        else:
            self.cam_depth_y_coeff = 0.0
            self.cam_depth_z_coeff = 1.0
            self.cam_depth_translate = 0.0
            self.cam_depth_affine_valid = False

    def set_giant_x_anchor(self, anchor_x: Optional[float]) -> None:
        # Compatibility only.  Giant mode now uses the real camera X just like
        # the normal projection path; no synthetic midpoint anchor is applied.
        self.giant_x_anchor = None

    def world_to_screen(self, world_x: float, world_y: float, world_z: float):
        """Single camera projection for normal and giant fighters.

        Giant mode is not given a fabricated midpoint, X/Y multiplier, or screen
        offset.  It uses the same camera X/Y transform as ordinary fighters;
        the only difference is the live camera distance (Z), exactly as the
        game camera reports it.
        """
        if self.ref_cam_z is None and abs(self.cam_z) > 0.0001:
            self.ref_cam_z = self.cam_z

        effective_ref_cam_z = self.ref_cam_z
        # When launched directly in giant mode, seed from the ordinary camera
        # distance rather than from the already-zoomed-out giant distance.
        if abs(self.cam_z) > 0.0001 and self.cam_z >= GIANT_CAMERA_Z_THRESHOLD:
            if effective_ref_cam_z is None or effective_ref_cam_z >= GIANT_CAMERA_Z_THRESHOLD:
                effective_ref_cam_z = GIANT_FALLBACK_REF_CAM_Z

        camera_scale = (
            effective_ref_cam_z / self.cam_z
            if (effective_ref_cam_z is not None and abs(self.cam_z) > 0.0001)
            else 1.0
        )
        zoom_scale = self.cfg.baseline_ppu * self.viewport_scale * camera_scale
        sx = self.cx + (world_x - self.cam_x) * zoom_scale * self.stretch_factor

        if self.cam_view_affine_valid:
            center_world_y = self.cam_y - WORLD_Y_OFFSET
            view_y = (self.cam_view_cos * world_y) + (self.cam_view_sin * world_z) + self.cam_view_y_translate
            center_view_y = (self.cam_view_cos * center_world_y) + self.cam_view_y_translate
            projected_y = view_y - center_view_y
        else:
            projected_y = (world_y - self.cam_y) + WORLD_Y_OFFSET

        sy = self.cy - (projected_y * zoom_scale)
        if abs(self.cam_z) >= GIANT_CAMERA_Z_THRESHOLD:
            sy += GIANT_Y_SCREEN_OFFSET
        return int(sx), int(sy), 1.0, zoom_scale

    def clear(self):
        self.frame_index += 1

        stale = [
            label
            for label, last_seen in self.hitbox_last_seen.items()
            if self.frame_index - last_seen > 2
        ]
        for label in stale:
            self.hitbox_last_seen.pop(label, None)
            self.hitbox_spawn_start.pop(label, None)

        self.screen.fill(COL_BG)

    def draw_debug_axes(self):
        if not self.debug_axes:
            return
        pygame.draw.line(self.screen, COL_DEBUG, (0, self.h // 2), (self.w, self.h // 2), 1)
        pygame.draw.line(self.screen, COL_DEBUG, (self.w // 2, 0), (self.w // 2, self.h), 1)

    def _project_hitbox(self, x, y, z, r):
        if r <= 0.001 or not math.isfinite(r):
            return None
        if r > HITBOX_MAX_RENDER_RADIUS:
            return None
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        r = min(r, self.cfg.max_radius_units)
        sx, sy, depth, focal = self.world_to_screen(x, y, z)
        rpx = max(2, int((r / depth) * focal))
        if rpx <= 0 or rpx > 5000:
            return None
        # Do not build/blit a surface for off-stage pools or stale descriptors.
        # This also removes partial circles that were being clipped at screen edges
        # for fighters parked at ±30 in assist standby.
        margin = max(72, rpx + 28)
        if (sx + rpx) < -margin or (sx - rpx) > (self.w + margin) or (sy + rpx) < -margin or (sy - rpx) > (self.h + margin):
            return None
        return sx, sy, rpx

    def draw_hitbox(
        self,
        x,
        y,
        z,
        r,
        color,
        label,
        is_active=False,
        invuln=False,
        show_label=False,
    ):
        """Draw the primary physical lane as a clean, team-colored glass ring."""
        result = self._project_hitbox(x, y, z, r)
        if result is None:
            return
        sx, sy, rpx = result

        r_c, g_c, b_c = color[:3]
        self.hitbox_last_seen[label] = self.frame_index
        if label not in self.hitbox_spawn_start:
            self.hitbox_spawn_start[label] = self.frame_index

        age = self.frame_index - self.hitbox_spawn_start[label]
        spawn_t = min(1.0, age / float(max(1, HITBOX_SPAWN_FRAMES - 1)))
        draw_rpx = max(2, int(rpx * (0.82 + 0.18 * spawn_t)))
        pulse_t = 0.5 + 0.5 * math.sin(self.frame_index * HITBOX_ACTIVE_PULSE_SPEED) if is_active else 0.0

        pad = 16
        size = draw_rpx * 2 + pad * 2 + 10
        surf = pygame.Surface((size, size), pygame.SRCALPHA)
        cx = cy = size // 2

        # Low-opacity glass fill keeps the model and hurtboxes readable.
        fill_alpha = 42 if is_active else 22
        pygame.draw.circle(surf, (r_c, g_c, b_c, fill_alpha), (cx, cy), draw_rpx)

        # Soft outer bloom, then a dark separator and crisp team-color rim.
        bloom_alpha = int((34 if is_active else 18) + 24 * pulse_t)
        pygame.draw.circle(surf, (r_c, g_c, b_c, bloom_alpha), (cx, cy), draw_rpx + 7)
        pygame.draw.circle(surf, (4, 8, 14, 185), (cx, cy), draw_rpx + 3, 4)
        pygame.draw.circle(surf, (r_c, g_c, b_c, 235 if is_active else 165), (cx, cy), draw_rpx + 1, 2)
        pygame.draw.circle(surf, (225, 242, 255, 155 if is_active else 90), (cx, cy), draw_rpx - 1, 1)

        # Active boxes get one restrained breathing ring, no yellow/orange blob.
        if is_active:
            pulse_px = int(2 + 3 * pulse_t)
            pygame.draw.circle(
                surf,
                (r_c, g_c, b_c, int(55 + 65 * pulse_t)),
                (cx, cy),
                draw_rpx + 7 + pulse_px,
                1,
            )

        # Invulnerability remains a separate mint ring rather than recoloring the hitbox.
        if invuln:
            pygame.draw.circle(surf, (105, 255, 170, 220), (cx, cy), draw_rpx + 11, 2)
            pygame.draw.circle(surf, (205, 255, 226, 90), (cx, cy), draw_rpx + 14, 1)

        self.screen.blit(surf, (sx - cx, sy - cy))

        # Small center reticle, deliberately lighter than the old full crosshair.
        reticle = max(3, min(7, draw_rpx // 4))
        reticle_col = (225, 242, 255)
        pygame.draw.line(self.screen, reticle_col, (sx - reticle, sy), (sx + reticle, sy), 1)
        pygame.draw.line(self.screen, reticle_col, (sx, sy - reticle), (sx, sy + reticle), 1)

        if show_label and rpx >= 12 and self.font_small is not None:
            txt = self.font_small.render(str(label), True, (232, 244, 255))
            tw, th = txt.get_size()
            bx = sx + rpx + 6
            by = sy - 9
            badge = pygame.Surface((tw + 8, th + 4), pygame.SRCALPHA)
            pygame.draw.rect(badge, (5, 10, 18, 205), (0, 0, tw + 8, th + 4), border_radius=4)
            pygame.draw.rect(badge, (r_c, g_c, b_c, 190), (0, 0, tw + 8, th + 4), 1, border_radius=4)
            badge.blit(txt, (4, 2))
            self.screen.blit(badge, (bx, by))

    def draw_move_phase_chip(
        self,
        projected_bounds: Tuple[int, int, int, int],
        text: str,
        color: Tuple[int, int, int],
        active: bool,
    ) -> None:
        if self.font_small is None or not text:
            return
        left, top, right, _bottom = projected_bounds
        center_x = int((left + right) * 0.5)
        txt = self.font_small.render(str(text), True, (240, 248, 255))
        tw, th = txt.get_size()
        padx = 8
        chip_w = tw + padx * 2
        chip_h = th + 6
        chip = pygame.Surface((chip_w, chip_h), pygame.SRCALPHA)
        cr, cg, cb = color[:3]
        pygame.draw.rect(chip, (4, 10, 18, 226 if active else 198), (0, 0, chip_w, chip_h), border_radius=7)
        pygame.draw.rect(chip, (cr, cg, cb, 240 if active else 170), (0, 0, chip_w, chip_h), 1, border_radius=7)
        if active:
            pygame.draw.rect(chip, (225, 242, 255, 78), (1, 1, chip_w - 2, chip_h - 2), 1, border_radius=6)
        chip.blit(txt, ((chip_w - tw) // 2, (chip_h - th) // 2 - 1))
        self.screen.blit(chip, (center_x - chip_w // 2, int(top) - chip_h - 7))

    def draw_hitbox_ghost(self, x, y, z, r, color, alpha: int = 110):
        """Draw one faded historical hitbox position without labels or fill."""
        result = self._project_hitbox(x, y, z, r)
        if result is None:
            return
        sx, sy, rpx = result
        alpha = max(0, min(255, int(alpha)))
        if alpha <= 0:
            return
        pad = 10
        size = rpx * 2 + pad * 2
        surf = pygame.Surface((size, size), pygame.SRCALPHA)
        cx = cy = size // 2
        cr, cg, cb = color[:3]
        pygame.draw.circle(surf, (cr, cg, cb, max(12, alpha // 5)), (cx, cy), rpx)
        pygame.draw.circle(surf, (cr, cg, cb, alpha), (cx, cy), rpx, max(1, min(3, rpx // 6 + 1)))
        pygame.draw.circle(surf, (240, 248, 255, max(8, alpha // 2)), (cx, cy), rpx + 2, 1)
        self.screen.blit(surf, (sx - cx, sy - cy))

    def draw_range_ruler(
        self,
        source_slot: str,
        target_slot: str,
        source_x: float,
        source_y: float,
        source_z: float,
        source_r: float,
        target_x: float,
        target_y: float,
        target_z: float,
        target_r: float,
        gap_units: float,
    ) -> None:
        """Draw a screen-space guide for one observed active attack/body gap.

        The value is horizontal world-coordinate edge separation from the
        currently drawn circles.  It is a spacing guide, not a collision claim.
        """
        try:
            if source_x <= target_x:
                src_edge_x = source_x + source_r
                tgt_edge_x = target_x - target_r
            else:
                src_edge_x = source_x - source_r
                tgt_edge_x = target_x + target_r
            ruler_y = min(float(source_y), float(target_y)) - 0.42
            sx1, sy1, _d1, _f1 = self.world_to_screen(src_edge_x, ruler_y, source_z)
            sx2, sy2, _d2, _f2 = self.world_to_screen(tgt_edge_x, ruler_y, target_z)
            if not (math.isfinite(sx1) and math.isfinite(sx2) and math.isfinite(sy1) and math.isfinite(sy2)):
                return

            y = int(min(sy1, sy2))
            x1, x2 = int(sx1), int(sx2)
            if abs(x2 - x1) < 4:
                x2 = x1 + (4 if x2 >= x1 else -4)
            left, right = sorted((x1, x2))
            y = max(12, min(self.h - 26, y))
            color = (112, 224, 255) if gap_units > 0.001 else (255, 202, 100)
            surf = pygame.Surface((max(1, right - left + 24), 30), pygame.SRCALPHA)
            ly = 15
            lx1 = 12
            lx2 = surf.get_width() - 12
            pygame.draw.line(surf, (*color, 210), (lx1, ly), (lx2, ly), 2)
            pygame.draw.line(surf, (*color, 235), (lx1, ly - 5), (lx1, ly + 5), 2)
            pygame.draw.line(surf, (*color, 235), (lx2, ly - 5), (lx2, ly + 5), 2)
            for tick_x in range(lx1 + 10, lx2, 10):
                pygame.draw.line(surf, (*color, 130), (tick_x, ly - 2), (tick_x, ly + 2), 1)
            if self.font_small is not None:
                label = f"{source_slot}→{target_slot}  GAP {max(0.0, gap_units):.2f}u"
                txt = self.font_small.render(label, True, (238, 248, 255))
                tw, th = txt.get_size()
                bx = max(0, (surf.get_width() - tw) // 2)
                badge = pygame.Surface((tw + 8, th + 4), pygame.SRCALPHA)
                pygame.draw.rect(badge, (8, 16, 26, 220), badge.get_rect(), border_radius=3)
                pygame.draw.rect(badge, (*color, 200), badge.get_rect(), 1, border_radius=3)
                badge.blit(txt, (4, 2))
                surf.blit(badge, (max(0, bx - 4), 0))
            self.screen.blit(surf, (left - 12, y - 15))
        except Exception:
            pass

    def draw_saved_range_zone(
        self,
        source_slot: str,
        target_slot: str,
        center_x: float,
        center_y: float,
        center_z: float,
        direction: float,
        far_units: float,
        tip_forward: float,
        tip_y: float,
        tip_z: float,
        tip_radius: float,
        target_gap: float | None,
        touching: bool,
        move_name: str = "RANGE",
        target_source: str = "profile",
        calibration_pips: int = 0,
        advance_forward: float = 0.0,
        source_label: Optional[str] = None,
        airborne: bool = False,
        posture: str = "stand",
        state_override: Optional[str] = None,
        detail_override: Optional[str] = None,
    ) -> None:
        """Draw a retained center-to-tip ruler at the fighter's current height."""
        try:
            direction = 1.0 if float(direction) >= 0.0 else -1.0
            far_units = max(0.0, float(far_units))
            tip_forward = max(0.0, float(tip_forward))
            tip_radius = max(0.0, float(tip_radius))
            if far_units <= 0.002:
                return

            sx0, sy0, _d0, _f0 = self.world_to_screen(center_x, center_y, center_z)
            sx1, sy1, _d1, _f1 = self.world_to_screen(center_x + direction * far_units, center_y, center_z)
            if not all(math.isfinite(v) for v in (sx0, sy0, sx1, sy1)):
                return

            x0, x1 = int(sx0), int(sx1)
            left, right = sorted((x0, x1))
            if right - left < 12:
                right = left + 12
            floor_y = int(max(sy0, sy1)) - 9
            floor_y = max(34, min(self.h - 30, floor_y))
            band_h = 16
            has_target = bool(target_slot)
            if airborne:
                accent = (182, 124, 255) if not touching else (92, 236, 145)
            else:
                accent = (92, 236, 145) if touching else ((104, 210, 255) if has_target else (150, 162, 184))
            fill_alpha = 74 if touching else 50

            width = max(1, right - left)
            surf = pygame.Surface((width + 4, band_h + 34), pygame.SRCALPHA)
            band = pygame.Rect(2, 18, width, band_h)
            pygame.draw.rect(surf, (*accent, fill_alpha), band, border_radius=3)
            pygame.draw.rect(surf, (*accent, 228), band, 2, border_radius=3)
            pygame.draw.line(surf, (*accent, 235), (2, 12), (2, 18 + band_h + 4), 2)
            pygame.draw.line(surf, (*accent, 235), (2 + width, 12), (2 + width, 18 + band_h + 4), 2)
            for tick in range(12, width, 12):
                pygame.draw.line(surf, (*accent, 130), (2 + tick, 20), (2 + tick, 18 + band_h - 2), 1)

            # Mark the actual stored hitbox circle at its profiled tip; the band
            # is only a distance ruler, while this circle is what checks contact.
            tip_x = center_x + direction * tip_forward
            tip_world_y = center_y + float(tip_y)
            tip_world_z = center_z + float(tip_z)
            projected_tip = self._project_hitbox(tip_x, tip_world_y, tip_world_z, max(0.02, tip_radius))
            if projected_tip is not None:
                tsx, tsy, tr = projected_tip
                marker = pygame.Surface((tr * 2 + 14, tr * 2 + 14), pygame.SRCALPHA)
                mc = marker.get_width() // 2
                pygame.draw.circle(marker, (*accent, 44), (mc, mc), tr)
                pygame.draw.circle(marker, (*accent, 245), (mc, mc), tr, 2)
                pygame.draw.circle(marker, (250, 252, 255, 205), (mc, mc), max(2, min(4, tr // 5)))
                self.screen.blit(marker, (int(tsx) - mc, int(tsy) - mc))

            if self.font_small is not None:
                state = str(state_override) if state_override else ("TOUCHING" if touching else ("OUT OF RANGE" if has_target else "BODY LEARNING"))
                if detail_override is not None:
                    detail = str(detail_override)
                elif not has_target:
                    detail = "target profile pending"
                elif touching:
                    detail = f"{target_slot} body"
                else:
                    detail = f"+{max(0.0, float(target_gap or 0.0)):.2f}u"
                move_tag = str(move_name or "RANGE")[:12]
                cal_text = f"  CAL {int(calibration_pips):+d}" if int(calibration_pips) else ""
                adv_text = f"  MOVE {float(advance_forward):+.2f}u" if abs(float(advance_forward)) >= 0.03 else ""
                source_tag = str(source_label or source_slot)
                pose = str(posture or "stand").strip().lower()
                range_tag = "AIR RANGE" if airborne else ("CROUCH RANGE" if pose == "crouch" else "RANGE")
                label = f"{source_tag} {move_tag}  {range_tag} {far_units:.2f}u{adv_text}{cal_text}  {state}  {detail}"
                txt = self.font_small.render(label, True, (240, 250, 255))
                tw, th = txt.get_size()
                badge = pygame.Surface((tw + 8, th + 4), pygame.SRCALPHA)
                pygame.draw.rect(badge, (7, 15, 25, 224), badge.get_rect(), border_radius=3)
                pygame.draw.rect(badge, (*accent, 205), badge.get_rect(), 1, border_radius=3)
                badge.blit(txt, (4, 2))
                bx = max(0, min(surf.get_width() - badge.get_width(), (surf.get_width() - badge.get_width()) // 2))
                surf.blit(badge, (bx, 0))
            self.screen.blit(surf, (left - 2, floor_y - 18))
        except Exception:
            pass

    def draw_saved_vertical_range_zone(
        self,
        source_slot: str,
        center_x: float,
        center_y: float,
        center_z: float,
        direction: float,
        lower_y: float,
        upper_y: float,
        upper_forward: float,
        lower_forward: float,
        move_name: str = "RANGE",
        source_label: Optional[str] = None,
        airborne: bool = False,
        posture: str = "stand",
    ) -> None:
        """Draw one clean top-to-bottom active-frame envelope ruler."""
        try:
            direction = 1.0 if float(direction) >= 0.0 else -1.0
            lower_y = float(lower_y)
            upper_y = float(upper_y)
            if not all(math.isfinite(v) for v in (lower_y, upper_y)) or upper_y <= lower_y + 0.01:
                return
            # Keep the rail just ahead of the fighter instead of through the
            # torso. The measurement remains root-relative; this is display-only
            # placement that avoids covering the character model.
            forward_hint = max(abs(float(upper_forward or 0.0)), abs(float(lower_forward or 0.0)))
            rail_forward = 0.30 + min(0.42, max(0.0, forward_hint) * 0.18)
            rail_x = float(center_x) + direction * rail_forward
            sx_top, sy_top, _d1, _f1 = self.world_to_screen(rail_x, float(center_y) + upper_y, float(center_z))
            sx_bottom, sy_bottom, _d2, _f2 = self.world_to_screen(rail_x, float(center_y) + lower_y, float(center_z))
            if not all(math.isfinite(v) for v in (sx_top, sy_top, sx_bottom, sy_bottom)):
                return
            x = int((sx_top + sx_bottom) / 2)
            top = int(min(sy_top, sy_bottom))
            bottom = int(max(sy_top, sy_bottom))
            height = max(12, bottom - top)
            top = max(26, min(self.h - 38, top))
            bottom = max(top + 12, min(self.h - 18, bottom))
            height = max(12, bottom - top)
            accent = (182, 124, 255) if airborne else (104, 210, 255)
            rail_w = 14
            pad = 7
            surf = pygame.Surface((rail_w + pad * 2 + 6, height + 38), pygame.SRCALPHA)
            rail_x_px = pad + 3
            rail = pygame.Rect(rail_x_px, 20, rail_w, height)
            pygame.draw.rect(surf, (*accent, 46), rail, border_radius=3)
            pygame.draw.rect(surf, (*accent, 228), rail, 2, border_radius=3)
            pygame.draw.line(surf, (*accent, 235), (rail_x_px - 5, 20), (rail_x_px + rail_w + 5, 20), 2)
            pygame.draw.line(surf, (*accent, 235), (rail_x_px - 5, 20 + height), (rail_x_px + rail_w + 5, 20 + height), 2)
            for tick_y in range(30, 20 + height, 12):
                pygame.draw.line(surf, (*accent, 130), (rail_x_px + 3, tick_y), (rail_x_px + rail_w - 3, tick_y), 1)

            if self.font_small is not None:
                up = max(0.0, upper_y)
                down = max(0.0, -lower_y)
                span = upper_y - lower_y
                source_tag = str(source_label or source_slot)
                pose = str(posture or "stand").strip().lower()
                axis_tag = "AIR VERT" if airborne else ("CROUCH VERT" if pose == "crouch" else "VERT")
                label = f"{source_tag} {str(move_name or 'RANGE')[:12]}  {axis_tag} {span:.2f}u  UP {up:.2f}  DOWN {down:.2f}"
                txt = self.font_small.render(label, True, (240, 250, 255))
                tw, th = txt.get_size()
                badge = pygame.Surface((tw + 8, th + 4), pygame.SRCALPHA)
                pygame.draw.rect(badge, (7, 15, 25, 224), badge.get_rect(), border_radius=3)
                pygame.draw.rect(badge, (*accent, 205), badge.get_rect(), 1, border_radius=3)
                badge.blit(txt, (4, 2))
                bx = max(0, min(self.w - badge.get_width(), x - badge.get_width() // 2))
                by = max(2, top - badge.get_height() - 6)
                self.screen.blit(badge, (bx, by))
            self.screen.blit(surf, (x - rail_x_px, top - 20))
        except Exception:
            pass

    def draw_saved_coverage_envelope(
        self,
        source_slot: str,
        center_x: float,
        center_y: float,
        center_z: float,
        direction: float,
        slices: List[Dict[str, float]],
        touching: bool,
        target_slot: str = "",
        move_name: str = "RANGE",
        source_label: Optional[str] = None,
        airborne: bool = False,
        temporal_layers: Optional[List[Dict[str, Any]]] = None,
        hit_frame: Optional[int] = None,
    ) -> None:
        """Draw a compact 2D active-coverage envelope from sampled Y slices.

        Each row represents the real forward reach available at that height
        across the move's confirmed active frames. The rows are connected only
        as a visual envelope; this is not a max-X by max-Y rectangle. When
        temporal layers are supplied, early active coverage is cold and later
        active coverage heats toward red.
        """
        try:
            if not isinstance(slices, list) or len(slices) < 2:
                return
            direction = 1.0 if float(direction) >= 0.0 else -1.0
            projected: List[Tuple[float, float, float, float]] = []
            for item in slices:
                if not isinstance(item, dict):
                    continue
                try:
                    y_rel = float(item.get("y_from_start"))
                    near = max(0.0, float(item.get("near_from_start") or 0.0))
                    far = max(near, float(item.get("far_from_start") or 0.0))
                except Exception:
                    continue
                if not all(math.isfinite(v) for v in (y_rel, near, far)) or far <= 0.02:
                    continue
                x_near = float(center_x) + direction * near
                x_far = float(center_x) + direction * far
                world_y = float(center_y) + y_rel
                npx, npy, _nd, _nf = self.world_to_screen(x_near, world_y, float(center_z))
                fpx, fpy, _fd, _ff = self.world_to_screen(x_far, world_y, float(center_z))
                if all(math.isfinite(v) for v in (npx, npy, fpx, fpy)):
                    projected.append((float(npx), float(npy), float(fpx), float(fpy)))
            if len(projected) < 2:
                return

            accent = (92, 236, 145) if touching else ((182, 124, 255) if airborne else (104, 210, 255))
            xs = [v for row in projected for v in (row[0], row[2])]
            ys = [v for row in projected for v in (row[1], row[3])]

            temporal_projected: List[Dict[str, Any]] = []
            for layer in list(temporal_layers or []):
                if not isinstance(layer, dict):
                    continue
                layer_rows: List[Tuple[float, float, float, float]] = []
                for item in list(layer.get("slices") or []):
                    if not isinstance(item, dict):
                        continue
                    try:
                        y_rel = float(item.get("y_from_start"))
                        near = max(0.0, float(item.get("near_from_start") or 0.0))
                        far = max(near, float(item.get("far_from_start") or 0.0))
                    except Exception:
                        continue
                    if not all(math.isfinite(v) for v in (y_rel, near, far)) or far <= 0.02:
                        continue
                    world_y = float(center_y) + y_rel
                    x_near = float(center_x) + direction * near
                    x_far = float(center_x) + direction * far
                    npx, npy, _nd, _nf = self.world_to_screen(x_near, world_y, float(center_z))
                    fpx, fpy, _fd, _ff = self.world_to_screen(x_far, world_y, float(center_z))
                    if all(math.isfinite(v) for v in (npx, npy, fpx, fpy)):
                        row = (float(npx), float(npy), float(fpx), float(fpy))
                        layer_rows.append(row)
                        xs.extend((row[0], row[2]))
                        ys.extend((row[1], row[3]))
                if len(layer_rows) >= 2:
                    temporal_projected.append({**layer, "projected": layer_rows})

            pad = 12
            left = int(math.floor(min(xs))) - pad
            top = int(math.floor(min(ys))) - pad
            right = int(math.ceil(max(xs))) + pad
            bottom = int(math.ceil(max(ys))) + pad
            width = max(4, right - left)
            height = max(4, bottom - top)
            surf = pygame.Surface((width, height), pygame.SRCALPHA)

            def heat_color(progress: float) -> Tuple[int, int, int]:
                """Cold blue -> cyan -> amber -> hot red."""
                p = max(0.0, min(1.0, float(progress)))
                stops = (
                    (0.00, (64, 126, 255)),
                    (0.35, (66, 224, 255)),
                    (0.68, (255, 205, 74)),
                    (1.00, (255, 74, 66)),
                )
                for index in range(1, len(stops)):
                    p0, c0 = stops[index - 1]
                    p1, c1 = stops[index]
                    if p <= p1:
                        q = 0.0 if p1 <= p0 else (p - p0) / (p1 - p0)
                        return tuple(int(round(c0[i] + (c1[i] - c0[i]) * q)) for i in range(3))
                return stops[-1][1]

            # Time heatmap. Draw chronological layers first so the retained
            # overall envelope can sit cleanly on top. Late frames use slightly
            # stronger alpha, which makes advancing arcs visibly heat forward.
            for layer in temporal_projected:
                rows = list(layer.get("projected") or [])
                if len(rows) < 2:
                    continue
                try:
                    progress = float(layer.get("progress") or 0.0)
                except Exception:
                    progress = 0.0
                color = heat_color(progress)
                layer_near = [(int(row[0] - left), int(row[1] - top)) for row in rows]
                layer_far = [(int(row[2] - left), int(row[3] - top)) for row in rows]
                layer_poly = layer_near + list(reversed(layer_far))
                fill_alpha = int(round(14 + 16 * max(0.0, min(1.0, progress))))
                line_alpha = int(round(78 + 92 * max(0.0, min(1.0, progress))))
                if len(layer_poly) >= 3:
                    pygame.draw.polygon(surf, (*color, fill_alpha), layer_poly)
                if len(layer_far) >= 2:
                    pygame.draw.lines(surf, (*color, line_alpha), False, layer_far, 2)
                for npt, fpt in zip(layer_near, layer_far):
                    pygame.draw.line(surf, (*color, max(44, line_alpha // 2)), npt, fpt, 1)

                # The predicted first-hit layer gets a compact bright marker.
                try:
                    f0 = int(layer.get("frame_start"))
                    f1 = int(layer.get("frame_end"))
                except Exception:
                    f0 = f1 = -1
                if hit_frame is not None and f0 <= int(hit_frame) <= f1 and layer_far:
                    marker = layer_far[len(layer_far) // 2]
                    pygame.draw.circle(surf, (245, 255, 245, 235), marker, 4, 1)

            near_points = [(int(row[0] - left), int(row[1] - top)) for row in projected]
            far_points = [(int(row[2] - left), int(row[3] - top)) for row in projected]
            polygon = near_points + list(reversed(far_points))
            if len(polygon) >= 3:
                pygame.draw.polygon(surf, (*accent, 10 if temporal_projected else (24 if not touching else 34)), polygon)

            # Individual rows make the varying X-at-Y relationship readable.
            for idx, (npx, npy, fpx, fpy) in enumerate(projected):
                a = 34 if temporal_projected else (62 if idx not in (0, len(projected) - 1) else 92)
                pygame.draw.line(
                    surf, (*accent, a),
                    (int(npx - left), int(npy - top)),
                    (int(fpx - left), int(fpy - top)),
                    1,
                )

            if len(far_points) >= 2:
                pygame.draw.lines(surf, (*accent, 220 if touching else 170), False, far_points, 2)
            if len(near_points) >= 2:
                pygame.draw.lines(surf, (*accent, 76), False, near_points, 1)
            for px, py in far_points:
                pygame.draw.circle(surf, (*accent, 205), (px, py), 2)

            if self.font_small is not None:
                source_tag = str(source_label or source_slot)
                target_tag = f"  vs {target_slot}" if target_slot else ""
                frame_tag = ""
                if temporal_projected:
                    try:
                        first_active = min(int(layer.get("frame_start")) for layer in temporal_projected)
                        last_active = max(int(layer.get("frame_end")) for layer in temporal_projected)
                        frame_tag = f"  F{first_active}>F{last_active}"
                    except Exception:
                        frame_tag = ""
                hit_tag = f"  HIT ~F{int(hit_frame)}" if hit_frame is not None else ""
                label = f"{source_tag} {str(move_name or 'RANGE')[:12]}  2D COVERAGE{frame_tag}{hit_tag}{target_tag}"
                txt = self.font_small.render(label, True, (238, 248, 255))
                tw, th = txt.get_size()
                badge = pygame.Surface((tw + 8, th + 4), pygame.SRCALPHA)
                pygame.draw.rect(badge, (7, 15, 25, 216), badge.get_rect(), border_radius=3)
                pygame.draw.rect(badge, (*accent, 185), badge.get_rect(), 1, border_radius=3)
                badge.blit(txt, (4, 2))
                bx = max(0, min(width - badge.get_width(), (width - badge.get_width()) // 2))
                surf.blit(badge, (bx, 0))

            self.screen.blit(surf, (left, top))
        except Exception:
            pass

    def draw_dynamic_range_sweep(
        self,
        source_label: str,
        move_name: str,
        shapes: List[Tuple[float, float, float, float, int]],
        target_gap: float | None,
        touching: bool,
        target_slot: str = "",
    ) -> None:
        """Draw the recorded active-frame coverage of one move.

        The regular ruler stays the single farthest tip. This is deliberately a
        separate ghosted layer: it shows the actual sampled trajectory and all
        simultaneous/multi-hit circles, including vertical travel.
        """
        try:
            if not shapes:
                return
            accent = (180, 124, 255) if not touching else (92, 236, 145)
            projected = []
            for x, y, z, radius, frame_no in shapes[:160]:
                result = self._project_hitbox(float(x), float(y), float(z), max(0.02, float(radius)))
                if result is None:
                    continue
                sx, sy, rpx = result
                projected.append((int(sx), int(sy), max(2, int(rpx)), int(frame_no)))
            if not projected:
                return
            frame_values = sorted({item[3] for item in projected})
            frame_rank = {value: index for index, value in enumerate(frame_values)}
            count = max(1, len(frame_values))
            for sx, sy, rpx, frame_no in projected:
                rank = frame_rank.get(frame_no, 0)
                alpha = max(44, min(165, 64 + int(96 * (rank + 1) / count)))
                pad = 8
                size = rpx * 2 + pad * 2
                surf = pygame.Surface((size, size), pygame.SRCALPHA)
                mid = size // 2
                pygame.draw.circle(surf, (*accent, max(10, alpha // 5)), (mid, mid), rpx)
                pygame.draw.circle(surf, (*accent, alpha), (mid, mid), rpx, max(1, min(2, rpx // 8 + 1)))
                self.screen.blit(surf, (sx - mid, sy - mid))

            min_x = min(sx - rpx for sx, _sy, rpx, _f in projected)
            max_x = max(sx + rpx for sx, _sy, rpx, _f in projected)
            min_y = min(sy - rpx for _sx, sy, rpx, _f in projected)
            label_y = max(34, min(self.h - 26, min_y - 20))
            if self.font_small is not None:
                if touching:
                    detail = f"{target_slot} COVERAGE TOUCH" if target_slot else "COVERAGE TOUCH"
                elif target_gap is None:
                    detail = "TARGET PENDING"
                else:
                    detail = f"COVERAGE GAP +{max(0.0, float(target_gap)):.2f}u"
                label = f"{str(source_label)} {str(move_name)[:12]}  DYNAMIC {len(frame_values)}f/{len(projected)} boxes  {detail}"
                txt = self.font_small.render(label, True, (245, 239, 255))
                tw, th = txt.get_size()
                badge = pygame.Surface((tw + 8, th + 4), pygame.SRCALPHA)
                pygame.draw.rect(badge, (21, 10, 38, 225), badge.get_rect(), border_radius=3)
                pygame.draw.rect(badge, (*accent, 215), badge.get_rect(), 1, border_radius=3)
                badge.blit(txt, (4, 2))
                bx = max(2, min(self.w - badge.get_width() - 2, (min_x + max_x - badge.get_width()) // 2))
                self.screen.blit(badge, (bx, label_y))
        except Exception:
            pass

    def draw_hurtbox(self, x, y, z, r, color, label, highlight=False, detail=True, invuln=False, show_label=False):
        """Polished cached hurtbox draw.

        Same data and same geometry as V13.  The difference is visual language:
        hitboxes are solid/warm/labeled; hurtboxes are blue glass outlines.
        """
        result = self._project_hitbox(x, y, z, r)
        if result is None:
            return
        sx, sy, rpx = result
        rpx = max(2, int(rpx))

        surf = _get_cached_hurtbox_surface(rpx, color[:3], bool(highlight), bool(detail), bool(invuln))
        cx = surf.get_width() // 2
        cy = surf.get_height() // 2
        self.screen.blit(surf, (sx - cx, sy - cy))

        if (highlight or show_label) and self.font_small is not None:
            if highlight:
                fg = (255, 245, 130)
            elif invuln:
                fg = (115, 255, 155)
            else:
                fg = (228, 246, 255)
            shadow = self.font_small.render(label, True, (0, 0, 0))
            txt = self.font_small.render(label, True, fg)
            self.screen.blit(shadow, (sx + rpx + 7, sy - 7))
            self.screen.blit(txt, (sx + rpx + 6, sy - 8))

    def draw_hit_contact(self, contact: HitContactState, color=(255, 255, 255)):
        try:
            sx, sy, _d, _f = self.world_to_screen(contact.x, contact.y, contact.z)
            d = 8
            pygame.draw.line(self.screen, color, (sx - d, sy - d), (sx + d, sy + d), 2)
            pygame.draw.line(self.screen, color, (sx - d, sy + d), (sx + d, sy - d), 2)
            if self.font_small is not None:
                txt = self.font_small.render(contact.label(), True, color)
                self.screen.blit(txt, (sx + d + 4, sy - 8))
        except Exception:
            pass

    def draw_range_contact_audit_marker(self, marker: Dict[str, Any], witnessed: bool) -> None:
        """Draw an honest contact-audit marker.

        ``witnessed`` means the game produced a damage/impact signal and the
        displayed descriptor pair is the closest live geometry.  ``expected``
        means the stored pair is being checked during a later occurrence.
        """
        try:
            sx, sy, _d, _f = self.world_to_screen(
                float(marker.get("x") or 0.0),
                float(marker.get("y") or 0.0),
                float(marker.get("z") or 0.0),
            )
            gap = max(0.0, float(marker.get("surface_gap") or 0.0))
            touching = gap <= 0.045
            color = (255, 228, 112) if witnessed else ((100, 246, 150) if touching else (255, 156, 94))
            radius = 8 if witnessed else 6
            pygame.draw.circle(self.screen, color, (int(sx), int(sy)), radius, 2)
            pygame.draw.line(self.screen, color, (int(sx) - radius - 3, int(sy)), (int(sx) + radius + 3, int(sy)), 1)
            pygame.draw.line(self.screen, color, (int(sx), int(sy) - radius - 3), (int(sx), int(sy) + radius + 3), 1)
            if self.font_small is not None:
                source = str(marker.get("source_slot") or "?")
                target = str(marker.get("target_slot") or "?")
                hit_index = int(marker.get("hitbox_index") or 0)
                hurt_index = int(marker.get("hurtbox_index") or 0)
                if witnessed:
                    signal = str(marker.get("signal") or "impact").upper()
                    mismatch = not bool(marker.get("predicted_touch"))
                    status = "RULER MISS" if mismatch else "RULER ALIGN"
                    label = f"{signal} WIT {source}[{hit_index}]>{target}[{hurt_index}]  {status}"
                else:
                    move_name = str(marker.get("move_name") or "RANGE")
                    samples = max(0, int(marker.get("samples") or 0))
                    truth = bool(marker.get("truth"))
                    prefix = "TRUTH" if truth else "CHECK"
                    label = f"{prefix} {move_name} {source}[{hit_index}]>{target}[{hurt_index}]  GAP +{gap:.2f}  x{samples}"
                shadow = self.font_small.render(label, True, (0, 0, 0))
                txt = self.font_small.render(label, True, color)
                self.screen.blit(shadow, (int(sx) + radius + 5, int(sy) - 9))
                self.screen.blit(txt, (int(sx) + radius + 4, int(sy) - 10))
        except Exception:
            return

    def draw_projectile_hitbox(
        self,
        x,
        y,
        z,
        r,
        color,
        label,
        sweep_x=None,
        sweep_y=None,
        sweep_z=None,
        contact_x=None,
        contact_y=None,
        contact_z=None,
        contact_valid=False,
        root_x=None,
        root_y=None,
        root_z=None,
        show_label=False,
    ):
        """Draw the projectile/effect lane at the radius supplied by its own lane."""
        display_r = max(0.0, float(r))
        result = self._project_hitbox(x, y, z, display_r)
        if result is None:
            return
        sx, sy, rpx = result
        r_c, g_c, b_c = color[:3]
        dark = (4, 9, 16)
        glass = (224, 244, 255)

        if sweep_x is not None and sweep_y is not None:
            try:
                ex, ey, _depth_e, _focal_e = self.world_to_screen(x, y, z)
                bx, by, _depth_b, _focal_b = self.world_to_screen(sweep_x, sweep_y, sweep_z or z)
                width = max(2, min(180, int(rpx * 2)))
                pad = max(12, rpx + 10)
                min_x = min(bx, ex) - pad
                min_y = min(by, ey) - pad
                max_x = max(bx, ex) + pad
                max_y = max(by, ey) + pad
                surf = pygame.Surface((max(1, max_x - min_x), max(1, max_y - min_y)), pygame.SRCALPHA)
                p0 = (bx - min_x, by - min_y)
                p1 = (ex - min_x, ey - min_y)
                cap = max(1, width // 2)

                # Transparent glass body with a crisp, team-colored outline.
                pygame.draw.line(surf, (*dark, 175), p0, p1, width + 6)
                pygame.draw.circle(surf, (*dark, 175), p0, cap + 3)
                pygame.draw.circle(surf, (*dark, 175), p1, cap + 3)
                pygame.draw.line(surf, (r_c, g_c, b_c, 42), p0, p1, width)
                pygame.draw.circle(surf, (r_c, g_c, b_c, 42), p0, cap)
                pygame.draw.circle(surf, (r_c, g_c, b_c, 42), p1, cap)
                pygame.draw.line(surf, (r_c, g_c, b_c, 230), p0, p1, 2)
                pygame.draw.line(surf, (*glass, 150), p0, p1, 1)
                pygame.draw.circle(surf, (r_c, g_c, b_c, 235), p0, cap, 2)
                pygame.draw.circle(surf, (r_c, g_c, b_c, 235), p1, cap, 2)
                pygame.draw.circle(surf, (*glass, 130), p0, max(1, cap - 2), 1)
                pygame.draw.circle(surf, (*glass, 130), p1, max(1, cap - 2), 1)
                self.screen.blit(surf, (min_x, min_y))
            except Exception:
                pass
        else:
            pad = 12
            size = rpx * 2 + pad * 2
            surf = pygame.Surface((size, size), pygame.SRCALPHA)
            cx = cy = rpx + pad
            pygame.draw.circle(surf, (*dark, 180), (cx, cy), rpx + 4, 4)
            pygame.draw.circle(surf, (r_c, g_c, b_c, 40), (cx, cy), rpx)
            pygame.draw.circle(surf, (r_c, g_c, b_c, 230), (cx, cy), rpx + 1, 2)
            pygame.draw.circle(surf, (*glass, 145), (cx, cy), max(1, rpx - 2), 1)
            pygame.draw.circle(surf, (r_c, g_c, b_c, 25), (cx, cy), rpx + 7, 1)
            self.screen.blit(surf, (sx - rpx - pad, sy - rpx - pad))

        # Root marker remains subtle and lane-colored.
        if root_x is not None and root_y is not None:
            try:
                rx, ry, _d, _f = self.world_to_screen(root_x, root_y, root_z or z)
                d = 4
                pygame.draw.line(self.screen, (r_c, g_c, b_c), (rx - d, ry), (rx + d, ry), 1)
                pygame.draw.line(self.screen, (r_c, g_c, b_c), (rx, ry - d), (rx, ry + d), 1)
            except Exception:
                pass

        if contact_valid and contact_x is not None and contact_y is not None:
            try:
                cx, cy, _d, _f = self.world_to_screen(contact_x, contact_y, contact_z or z)
                d = max(5, min(10, rpx // 2))
                contact_col = (230, 248, 255)
                pygame.draw.polygon(
                    self.screen,
                    contact_col,
                    [(cx, cy - d), (cx + d, cy), (cx, cy + d), (cx - d, cy)],
                    2,
                )
                if show_label and self.font_small is not None:
                    txt = self.font_small.render("CONTACT", True, contact_col)
                    self.screen.blit(txt, (cx + d + 3, cy - 8))
            except Exception:
                pass

        if show_label and rpx >= 8 and self.font_small is not None:
            label_text = f"{label} r={display_r:.2f}"
            shadow = self.font_small.render(label_text, True, (0, 0, 0))
            txt = self.font_small.render(label_text, True, color[:3])
            self.screen.blit(shadow, (sx + rpx + 6, sy - 7))
            self.screen.blit(txt, (sx + rpx + 5, sy - 8))

    def draw_hud(self, counts, motion_filter: MotionFilter, node_tracker: ProjectileNodeTracker, hurt_counts=None):
        # The old multi-field debug header was useful during bring-up, but it
        # obscures the playfield in normal use.  The legend now lives in a
        # separate helper window instead of the top-left overlay area.
        return

    def present(self):
        pygame.display.flip()


def _derive_giant_x_anchor() -> Optional[float]:
    """Return the live on-stage fighter midpoint for giant camera framing.

    In giant matches the camera object keeps X at a neutral scene origin even
    though the visible fight is framed around the giant/opponent pair.  Using
    that stale zero makes the error grow as either fighter walks away from
    center.  Root X midpoint is live, cheap, and follows the real framing.
    """
    xs: List[float] = []
    for slot_base in SLOT_BASES.values():
        try:
            char_id = int(rd32(slot_base + OFF_CHAR_ID) or 0)
            if char_id <= 0 or char_id == 0xFFFFFFFF:
                continue
            x, y, z = read_fighter_root(slot_base)
            if not all(math.isfinite(v) for v in (x, y, z)):
                continue
            if abs(y) >= 70.0 or abs(z) >= 70.0:
                continue
            xs.append(float(x))
        except Exception:
            continue
    if len(xs) >= 2:
        return (min(xs) + max(xs)) * 0.5
    if len(xs) == 1:
        return xs[0]
    return None


def _slot_root_is_on_screen(overlay: Overlay, slot_base: int) -> bool:
    """Cull an entire parked/off-stage fighter before stale box descriptors draw."""
    try:
        x, y, z = read_fighter_root(slot_base)
        if not all(math.isfinite(v) for v in (x, y, z)):
            return False
        sx, sy, _d, _f = overlay.world_to_screen(x, y, z)
        # Giant framing can place a valid opponent near/just beyond the edge
        # before their own circles re-enter view. Keep this broad; individual
        # geometry still clips naturally at the pygame surface.
        margin = 700
        return (-margin <= sx <= overlay.w + margin) and (-margin <= sy <= overlay.h + margin)
    except Exception:
        return False


class HitboxRenderer:
    def __init__(self) -> None:
        self.motion_filter = MotionFilter()
        self.scanner = ProjectileScanner()

        total_nodes = len(PROJECTILE_POOLS) * PROJECTILE_NODE_COUNT
        self.node_tracker = ProjectileNodeTracker(total_nodes)

        self.cached_projectiles: List[ProjectileActorState] = []
        self.cached_hurtboxes: Dict[str, List[HurtboxState]] = {}
        self.cached_hit_contacts: List[HitContactState] = []
        self.hurt_counts: Dict[str, int] = {}
        self.slot_renderable: Dict[str, bool] = {slot: False for slot in SLOT_BASES}
        self._last_hurt_update_ms: int = 0
        self._last_contact_update_ms: int = 0
        self.last_char_ids: Dict[str, int] = {}
        self.last_counts: Dict[str, int] = {}
        self.fd_by_slot: Dict[str, Dict[int, MoveFrameData]] = {}
        self._fd_scan_done = False
        self._last_camera_mode_key: Optional[str] = None
        self._hitbox_ghost_frames: List[List[Tuple[float, float, float, float, Tuple[int, int, int]]]] = []
        # Keep the most recently observed ruler per team briefly after a
        # startup/active frame so short normals remain readable at 60 Hz.
        self._last_range_rulers: Dict[str, Tuple[int, float, str, HurtboxState, Tuple[str, float, float, float, float]]] = {}
        # Latest measured maximum forward reach per raw fighter slot. A new
        # active move replaces the prior measurement; walking/idle does not.
        self._saved_ranges: Dict[str, Dict[str, Any]] = {}
        # Last usable action for each live posture.  The actual profile JSON
        # remains move-keyed; this is transient presentation state only.
        self._saved_ranges_by_posture: Dict[str, Dict[str, Dict[str, Any]]] = {
            slot: {} for slot in SLOT_BASES
        }
        # Presentation-only action posture locks.  These never modify the
        # learned JSON; they just prevent crouching/air actions from briefly
        # borrowing a standing ruler when TvC switches through a generic state
        # during startup/recovery.
        self._range_posture_locks: Dict[str, Dict[str, Any]] = {}
        self._range_profiles: Dict[str, Any] = self._load_range_profiles()
        self._range_profiles_dirty = False
        self._last_range_profile_flush_ms = 0
        # One active calibration attempt per raw fighter slot.  HP loss is used
        # as a lightweight resolved-hit signal; the broad resolver scan remains
        # disabled during ordinary overlay play.
        self._range_calibration_attempts: Dict[str, Dict[str, Any]] = {}
        self._range_last_hp: Dict[str, int] = {}
        # Body-center anchor captured when each action state begins.  A forward-
        # moving normal is measured from this center, not from its later active
        # position, so its retained ruler includes the move's own advance.
        self._range_move_origins: Dict[str, Dict[str, Any]] = {}
        # One temporary collector per raw fighter slot.  A missing profile is
        # sampled across its complete active window, then committed once at
        # recovery/state exit.  Existing valid entries never enter this map.
        self._range_pending_learns: Dict[str, Dict[str, Any]] = {}
        # Sticky body-special ownership. Specials can change internal state IDs
        # several times while remaining one attack, so exact-ID profiling alone
        # truncates Shoryu/Tatsu/Legs-style effective coverage.
        self._range_special_sessions: Dict[str, Dict[str, Any]] = {}
        # Contact audit is intentionally tiny and action-scoped.  Each active
        # profiled body move retains a few current hitbox snapshots.  When the
        # game signals a real impact, those snapshots identify the closest
        # attack/hurtbox pair without scanning the global resolver heap.
        self._range_contact_snapshots: Dict[str, List[Dict[str, Any]]] = {}
        self._range_contact_markers: List[Dict[str, Any]] = []
        self._range_contact_expectations: Dict[str, Dict[str, Any]] = {}
        self._range_last_impact_freeze: Dict[str, int] = {}
        # Per raw slot floor-reference root height used for the live ground/air
        # presentation decision.  This is transient; it is never stored in a
        # move profile and resets cleanly on a roster/camera switch.
        self._range_ground_reference_y: Dict[str, float] = {}
        # One presentation timer per fighter move. This is intentionally
        # independent of the three physical radii so the phase chip never
        # jumps between individual circles.
        self._move_phase_state: Dict[str, Dict[str, Any]] = {}
        self._move_phase_tick: int = 0

        self.w = DISPLAY.baseline_w
        self.h = DISPLAY.baseline_h

        self.overlay = Overlay(DISPLAY)
        self.overlay.font_small = pygame.font.SysFont("consolas", 11)
        self.overlay.font_hud = pygame.font.SysFont("consolas", 13, bold=True)

    @staticmethod
    def _profile_bounds_default() -> Dict[str, float]:
        return {
            "min_x": float("inf"), "max_x": float("-inf"),
            "min_y": float("inf"), "max_y": float("-inf"),
            "min_z": float("inf"), "max_z": float("-inf"),
        }

    @staticmethod
    def _profile_bounds_valid(bounds: Any) -> bool:
        if not isinstance(bounds, dict):
            return False
        try:
            return all(math.isfinite(float(bounds.get(k))) for k in ("min_x", "max_x", "min_y", "max_y", "min_z", "max_z")) and float(bounds["min_x"]) <= float(bounds["max_x"]) and float(bounds["min_y"]) <= float(bounds["max_y"])
        except Exception:
            return False

    def _load_range_profiles(self) -> Dict[str, Any]:
        """Load retained attack/body envelopes without trusting malformed data.

        Schema 4 measured attack reach from a pose-dependent hurtbox-envelope
        center and could retain a stale move-start center while rendering.  The
        body envelopes remain useful, but the attack entries use incompatible
        coordinates and are intentionally learned again under schema 5.
        """
        fallback = {"schema": RANGE_PROFILE_SCHEMA, "attacks": {}, "bodies": {}}
        try:
            with open(RANGE_PROFILE_FILE, "r", encoding="utf-8") as f:
                doc = _json.load(f)
            if not isinstance(doc, dict):
                return fallback
            schema = int(doc.get("schema") or 0)
            bodies = doc.get("bodies") if isinstance(doc.get("bodies"), dict) else {}
            if schema == RANGE_PROFILE_SCHEMA:
                attacks = doc.get("attacks") if isinstance(doc.get("attacks"), dict) else {}
                return {"schema": RANGE_PROFILE_SCHEMA, "attacks": attacks, "bodies": bodies}
            if schema == RANGE_PROFILE_LEGACY_SCHEMA:
                # Do not allow the old stale-anchor data to draw one more wrong
                # ruler.  Preserve the learned body envelopes only.
                print("[range profile] schema-4 attacks reset for root-anchor profiling")
                return {"schema": RANGE_PROFILE_SCHEMA, "attacks": {}, "bodies": bodies}
            return fallback
        except Exception:
            return fallback

    def _flush_range_profiles(self, force: bool = False) -> bool:
        """Persist learned range data and report whether this call exported it.

        The JSON is first written and re-parsed in a temporary sibling file,
        then atomically replaced.  A failed write leaves the previous good file
        intact and keeps ``_range_profiles_dirty`` true for a later retry.
        """
        if not RANGE_PROFILE_WRITE_ENABLED:
            self._range_profiles_dirty = False
            return False
        if not self._range_profiles_dirty:
            return False
        now_ms = pygame.time.get_ticks()
        if not force and now_ms - int(self._last_range_profile_flush_ms) < RANGE_PROFILE_FLUSH_MS:
            return False
        payload = {
            "schema": RANGE_PROFILE_SCHEMA,
            "attacks": self._range_profiles.get("attacks", {}),
            "bodies": self._range_profiles.get("bodies", {}),
        }
        try:
            _write_range_profile_payload_atomic(RANGE_PROFILE_FILE, payload)
            self._range_profiles_dirty = False
            self._last_range_profile_flush_ms = now_ms
            print(
                f"[range profile] export OK: {len(payload['attacks'])} attacks / "
                f"{len(payload['bodies'])} bodies -> {RANGE_PROFILE_FILE}"
            )
            return True
        except Exception as exc:
            print(f"[range profile] export FAILED; keeping data in memory for retry: {exc!r}")
            return False

    @staticmethod
    def _range_calibration_entry(profile: Dict[str, Any]) -> Dict[str, Any]:
        """Return a normalized per-move calibration block."""
        calibration = profile.get("calibration")
        if not isinstance(calibration, dict):
            calibration = {}
            profile["calibration"] = calibration
        try:
            pips = int(calibration.get("pips") or 0)
        except Exception:
            pips = 0
        calibration["pips"] = max(-RANGE_CALIBRATION_MAX_PIPS, min(RANGE_CALIBRATION_MAX_PIPS, pips))
        calibration.setdefault("false_negative_hits", 0)
        calibration.setdefault("false_positive_streak", 0)
        calibration.setdefault("false_positive_adjustments", 0)
        return calibration

    def _range_adjustment(self, profile: Dict[str, Any]) -> tuple[float, int]:
        calibration = self._range_calibration_entry(profile)
        pips = int(calibration.get("pips") or 0)
        return float(pips) * RANGE_CALIBRATION_PIP, pips

    @staticmethod
    def _range_profile_core_geometry(profile: Dict[str, Any]) -> Optional[Dict[str, float]]:
        """Return the original root-anchor geometry stored for one attack.

        This is deliberately separate from the horizontal-envelope override so
        callers can still tell whether the move has a real core profile rather
        than treating a malformed supplement as a complete move record.
        """
        try:
            if str(profile.get("anchor") or "") != "fighter_root":
                return None
            values = {
                "reach_from_start": float(profile.get("reach_from_start")),
                "tip_center_from_start": float(profile.get("tip_center_from_start")),
                "tip_y_from_start": float(profile.get("tip_y_from_start")),
                "tip_z_from_start": float(profile.get("tip_z_from_start")),
                "tip_radius": float(profile.get("tip_radius")),
                "advance_at_tip": float(profile.get("advance_at_tip") or 0.0),
                "tip_center_from_active_root": float(profile.get("tip_center_from_active_root") or 0.0),
            }
            if not all(math.isfinite(v) for v in values.values()):
                return None
            if values["reach_from_start"] <= 0.03 or values["tip_radius"] <= 0.01:
                return None
            return values
        except Exception:
            return None

    @staticmethod
    def _horizontal_envelope_from_dynamic_frames(frames: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, float]]:
        """Extract the single furthest horizontal active hit edge.

        Every Dynamic frame is already normalized for facing:

            action-start root -> root travel -> local hitbox circle

        The ruler should therefore choose the largest ``root_fwd + local_fwd +
        radius`` over *all confirmed active frames*, not the final active
        circle.  The chosen circle's Y/Z/radius are kept so the ruler tip still
        marks where that horizontal maximum actually occurred.
        """
        if not isinstance(frames, list) or not frames:
            return None
        best: Optional[Dict[str, float]] = None
        for frame_index, item in enumerate(frames):
            if not isinstance(item, dict):
                continue
            root = item.get("root")
            shapes = item.get("shapes")
            if not isinstance(root, (tuple, list)) or len(root) < 3 or not isinstance(shapes, list):
                continue
            try:
                root_fwd, root_y, root_z = (float(root[0]), float(root[1]), float(root[2]))
            except Exception:
                continue
            if not all(math.isfinite(v) for v in (root_fwd, root_y, root_z)):
                continue
            if abs(root_fwd) > RANGE_PROFILE_MAX_ROOT_ADVANCE:
                continue
            for shape_index, shape in enumerate(shapes):
                if not isinstance(shape, (tuple, list)) or len(shape) < 4:
                    continue
                try:
                    local_fwd, local_y, local_z, radius = (
                        float(shape[0]), float(shape[1]), float(shape[2]), float(shape[3])
                    )
                except Exception:
                    continue
                if not all(math.isfinite(v) for v in (local_fwd, local_y, local_z, radius)):
                    continue
                if radius <= 0.01 or radius > 1.5:
                    continue
                tip_center_from_start = root_fwd + local_fwd
                reach_from_start = tip_center_from_start + radius
                if reach_from_start <= 0.03 or reach_from_start > RANGE_PROFILE_MAX_LOCAL_REACH:
                    continue
                candidate = {
                    "version": float(RANGE_HORIZONTAL_ENVELOPE_VERSION),
                    "reach_from_start": float(reach_from_start),
                    "tip_center_from_start": float(tip_center_from_start),
                    "tip_y_from_start": float(root_y + local_y),
                    "tip_z_from_start": float(root_z + local_z),
                    "tip_radius": float(radius),
                    "advance_at_tip": float(root_fwd),
                    "tip_center_from_active_root": float(local_fwd),
                    "active_frame": float(int(item.get("frame") or frame_index)),
                    "shape_index": float(shape_index),
                }
                if best is None or candidate["reach_from_start"] > best["reach_from_start"]:
                    best = candidate
        return best

    @staticmethod
    def _validated_horizontal_envelope(raw: Any) -> Optional[Dict[str, float]]:
        """Validate one persisted horizontal-envelope payload."""
        if not isinstance(raw, dict):
            return None
        try:
            if int(raw.get("version") or 0) != RANGE_HORIZONTAL_ENVELOPE_VERSION:
                return None
            values = {
                "reach_from_start": float(raw.get("reach_from_start")),
                "tip_center_from_start": float(raw.get("tip_center_from_start")),
                "tip_y_from_start": float(raw.get("tip_y_from_start")),
                "tip_z_from_start": float(raw.get("tip_z_from_start")),
                "tip_radius": float(raw.get("tip_radius")),
                "advance_at_tip": float(raw.get("advance_at_tip") or 0.0),
                "tip_center_from_active_root": float(raw.get("tip_center_from_active_root") or 0.0),
            }
            if not all(math.isfinite(v) for v in values.values()):
                return None
            if values["reach_from_start"] <= 0.03 or values["reach_from_start"] > RANGE_PROFILE_MAX_LOCAL_REACH:
                return None
            if values["tip_radius"] <= 0.01 or values["tip_radius"] > 1.5:
                return None
            if abs(values["advance_at_tip"]) > RANGE_PROFILE_MAX_ROOT_ADVANCE:
                return None
            return values
        except Exception:
            return None

    def _range_profile_horizontal_envelope(self, profile: Dict[str, Any]) -> Optional[Dict[str, float]]:
        """Return saved/derivable max-horizontal geometry for a profile."""
        persisted = self._validated_horizontal_envelope(profile.get("horizontal_envelope"))
        if persisted is not None:
            return persisted
        # Older profiles may already have a valid recorded sweep.  Use it for
        # rendering immediately even before the next save backfills the compact
        # envelope field.
        return self._horizontal_envelope_from_dynamic_frames(self._range_dynamic_sweep(profile))

    def _range_profile_geometry(self, profile: Dict[str, Any]) -> Optional[Dict[str, float]]:
        """Return display geometry, preferring true max-horizontal coverage."""
        core = self._range_profile_core_geometry(profile)
        if core is None:
            return None
        envelope = self._range_profile_horizontal_envelope(profile)
        return envelope if envelope is not None else core

    def _apply_horizontal_envelope(self, profile: Dict[str, Any], frames: List[Dict[str, Any]], replace: bool = False) -> bool:
        """Persist a one-time max-horizontal active-frame correction.

        This is intentionally an upgrade to a move's ruler geometry, not a
        calibration pip.  It does not alter contact witnesses or calibration.
        The profile's canonical tip fields are updated too so any older display
        call path reads the same truthful max-horizontal point.
        """
        if not RANGE_PROFILE_WRITE_ENABLED or not isinstance(profile, dict):
            return False
        envelope = self._horizontal_envelope_from_dynamic_frames(frames)
        if envelope is None:
            return False
        current = self._validated_horizontal_envelope(profile.get("horizontal_envelope"))
        # Once a valid envelope exists, it is authoritative. Do not allow later
        # play to drift it backward/forward without an explicit reset.
        if current is not None and not bool(replace):
            return False
        persisted = {
            "version": RANGE_HORIZONTAL_ENVELOPE_VERSION,
            "reach_from_start": float(envelope["reach_from_start"]),
            "tip_center_from_start": float(envelope["tip_center_from_start"]),
            "tip_y_from_start": float(envelope["tip_y_from_start"]),
            "tip_z_from_start": float(envelope["tip_z_from_start"]),
            "tip_radius": float(envelope["tip_radius"]),
            "advance_at_tip": float(envelope["advance_at_tip"]),
            "tip_center_from_active_root": float(envelope["tip_center_from_active_root"]),
            "active_frame": int(envelope.get("active_frame") or 0),
            "shape_index": int(envelope.get("shape_index") or 0),
        }
        profile["horizontal_envelope"] = persisted
        profile["reach_from_start"] = persisted["reach_from_start"]
        profile["tip_center_from_start"] = persisted["tip_center_from_start"]
        profile["tip_y_from_start"] = persisted["tip_y_from_start"]
        profile["tip_z_from_start"] = persisted["tip_z_from_start"]
        profile["tip_radius"] = persisted["tip_radius"]
        profile["advance_at_tip"] = persisted["advance_at_tip"]
        profile["tip_center_from_active_root"] = persisted["tip_center_from_active_root"]
        profile["ruler_geometry_source"] = "max_horizontal_active_envelope"
        profile["updated_ms"] = int(pygame.time.get_ticks())
        self._range_profiles_dirty = True
        return True

    @staticmethod
    def _vertical_envelope_from_dynamic_frames(frames: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, float]]:
        """Extract the top and bottom active hitbox edges across one move.

        Samples are action-root relative and contain only confirmed active
        frames.  Keeping both extrema makes a vertical ruler useful for arcs,
        low sweeps, launchers, and moves whose hitbox travels from floor to sky.
        """
        if not isinstance(frames, list) or not frames:
            return None
        top: Optional[Dict[str, float]] = None
        bottom: Optional[Dict[str, float]] = None
        for frame_index, item in enumerate(frames):
            if not isinstance(item, dict):
                continue
            root = item.get("root")
            shapes = item.get("shapes")
            if not isinstance(root, (tuple, list)) or len(root) < 3 or not isinstance(shapes, list):
                continue
            try:
                root_fwd, root_y, root_z = (float(root[0]), float(root[1]), float(root[2]))
            except Exception:
                continue
            if not all(math.isfinite(v) for v in (root_fwd, root_y, root_z)):
                continue
            if abs(root_fwd) > RANGE_PROFILE_MAX_ROOT_ADVANCE:
                continue
            for shape_index, shape in enumerate(shapes):
                if not isinstance(shape, (tuple, list)) or len(shape) < 4:
                    continue
                try:
                    local_fwd, local_y, local_z, radius = (
                        float(shape[0]), float(shape[1]), float(shape[2]), float(shape[3])
                    )
                except Exception:
                    continue
                if not all(math.isfinite(v) for v in (local_fwd, local_y, local_z, radius)):
                    continue
                if radius <= 0.01 or radius > 1.5:
                    continue
                center_y = root_y + local_y
                candidate_common = {
                    "forward_from_start": float(root_fwd + local_fwd),
                    "z_from_start": float(root_z + local_z),
                    "radius": float(radius),
                    "active_frame": float(int(item.get("frame") or frame_index)),
                    "shape_index": float(shape_index),
                }
                top_candidate = dict(candidate_common)
                top_candidate["edge_y_from_start"] = float(center_y + radius)
                bottom_candidate = dict(candidate_common)
                bottom_candidate["edge_y_from_start"] = float(center_y - radius)
                if top is None or top_candidate["edge_y_from_start"] > top["edge_y_from_start"]:
                    top = top_candidate
                if bottom is None or bottom_candidate["edge_y_from_start"] < bottom["edge_y_from_start"]:
                    bottom = bottom_candidate
        if top is None or bottom is None:
            return None
        high = float(top["edge_y_from_start"])
        low = float(bottom["edge_y_from_start"])
        if not math.isfinite(high) or not math.isfinite(low) or high <= low + 0.01:
            return None
        if (high - low) > RANGE_VERTICAL_ENVELOPE_MAX_SPAN:
            return None
        return {
            "version": float(RANGE_VERTICAL_ENVELOPE_VERSION),
            "upper_y_from_start": high,
            "lower_y_from_start": low,
            "upper_forward_from_start": float(top["forward_from_start"]),
            "lower_forward_from_start": float(bottom["forward_from_start"]),
            "upper_z_from_start": float(top["z_from_start"]),
            "lower_z_from_start": float(bottom["z_from_start"]),
            "upper_radius": float(top["radius"]),
            "lower_radius": float(bottom["radius"]),
            "upper_active_frame": float(top["active_frame"]),
            "lower_active_frame": float(bottom["active_frame"]),
            "upper_shape_index": float(top["shape_index"]),
            "lower_shape_index": float(bottom["shape_index"]),
        }

    @staticmethod
    def _validated_vertical_envelope(raw: Any) -> Optional[Dict[str, float]]:
        """Validate one persisted top/bottom active-envelope payload."""
        if not isinstance(raw, dict):
            return None
        try:
            if int(raw.get("version") or 0) != RANGE_VERTICAL_ENVELOPE_VERSION:
                return None
            values = {
                "upper_y_from_start": float(raw.get("upper_y_from_start")),
                "lower_y_from_start": float(raw.get("lower_y_from_start")),
                "upper_forward_from_start": float(raw.get("upper_forward_from_start") or 0.0),
                "lower_forward_from_start": float(raw.get("lower_forward_from_start") or 0.0),
                "upper_z_from_start": float(raw.get("upper_z_from_start") or 0.0),
                "lower_z_from_start": float(raw.get("lower_z_from_start") or 0.0),
                "upper_radius": float(raw.get("upper_radius") or 0.0),
                "lower_radius": float(raw.get("lower_radius") or 0.0),
            }
            if not all(math.isfinite(v) for v in values.values()):
                return None
            if values["upper_y_from_start"] <= values["lower_y_from_start"] + 0.01:
                return None
            if (values["upper_y_from_start"] - values["lower_y_from_start"]) > RANGE_VERTICAL_ENVELOPE_MAX_SPAN:
                return None
            if not (0.01 < values["upper_radius"] <= 1.5 and 0.01 < values["lower_radius"] <= 1.5):
                return None
            return values
        except Exception:
            return None

    def _range_profile_vertical_envelope(self, profile: Dict[str, Any]) -> Optional[Dict[str, float]]:
        """Return saved or derivable top/bottom active geometry for a profile."""
        persisted = self._validated_vertical_envelope(profile.get("vertical_envelope"))
        if persisted is not None:
            return persisted
        return self._vertical_envelope_from_dynamic_frames(self._range_dynamic_sweep(profile))

    def _range_profile_vertical_geometry(self, profile: Dict[str, Any]) -> Optional[Dict[str, float]]:
        """Return vertical envelope, with a one-tip fallback while it is learned."""
        envelope = self._range_profile_vertical_envelope(profile)
        if envelope is not None:
            return envelope
        core = self._range_profile_core_geometry(profile)
        if core is None:
            return None
        try:
            center_y = float(core["tip_y_from_start"])
            radius = float(core["tip_radius"])
            if radius <= 0.01:
                return None
            return {
                "upper_y_from_start": center_y + radius,
                "lower_y_from_start": center_y - radius,
                "upper_forward_from_start": float(core["tip_center_from_start"]),
                "lower_forward_from_start": float(core["tip_center_from_start"]),
                "upper_z_from_start": float(core["tip_z_from_start"]),
                "lower_z_from_start": float(core["tip_z_from_start"]),
                "upper_radius": radius,
                "lower_radius": radius,
                "fallback": 1.0,
            }
        except Exception:
            return None

    def _apply_vertical_envelope(self, profile: Dict[str, Any], frames: List[Dict[str, Any]], replace: bool = False) -> bool:
        """Persist one complete top/bottom active-frame envelope without overwriting it."""
        if not RANGE_PROFILE_WRITE_ENABLED or not isinstance(profile, dict):
            return False
        envelope = self._vertical_envelope_from_dynamic_frames(frames)
        if envelope is None:
            return False
        if self._validated_vertical_envelope(profile.get("vertical_envelope")) is not None and not bool(replace):
            return False
        persisted = {
            "version": RANGE_VERTICAL_ENVELOPE_VERSION,
            "upper_y_from_start": float(envelope["upper_y_from_start"]),
            "lower_y_from_start": float(envelope["lower_y_from_start"]),
            "upper_forward_from_start": float(envelope["upper_forward_from_start"]),
            "lower_forward_from_start": float(envelope["lower_forward_from_start"]),
            "upper_z_from_start": float(envelope["upper_z_from_start"]),
            "lower_z_from_start": float(envelope["lower_z_from_start"]),
            "upper_radius": float(envelope["upper_radius"]),
            "lower_radius": float(envelope["lower_radius"]),
            "upper_active_frame": int(envelope.get("upper_active_frame") or 0),
            "lower_active_frame": int(envelope.get("lower_active_frame") or 0),
            "upper_shape_index": int(envelope.get("upper_shape_index") or 0),
            "lower_shape_index": int(envelope.get("lower_shape_index") or 0),
        }
        profile["vertical_envelope"] = persisted
        profile["vertical_geometry_source"] = "full_active_envelope"
        profile["updated_ms"] = int(pygame.time.get_ticks())
        self._range_profiles_dirty = True
        return True

    @staticmethod
    def _range_dynamic_sweep(profile: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Return validated root-path + local-hitbox samples for a move.

        Each sample preserves both sides of the motion equation instead of
        folding them into one world-space tip:

          action-start root -> per-frame root travel -> hitbox local offset

        That lets a vertical/advancing/multi-hit normal be reconstructed from a
        fresh standing position without trying to attach the ruler to a live
        skeleton descriptor.
        """
        raw = profile.get("dynamic_sweep") if isinstance(profile, dict) else None
        if not isinstance(raw, dict) or int(raw.get("version") or 0) != RANGE_DYNAMIC_SWEEP_VERSION:
            return None
        raw_frames = raw.get("frames")
        if not isinstance(raw_frames, list) or not raw_frames:
            return None
        out: List[Dict[str, Any]] = []
        for item in raw_frames[:RANGE_DYNAMIC_MAX_FRAMES]:
            if not isinstance(item, dict):
                continue
            root = item.get("root")
            shapes = item.get("shapes")
            if not isinstance(root, (tuple, list)) or len(root) < 3 or not isinstance(shapes, list):
                continue
            try:
                root_vals = [float(root[0]), float(root[1]), float(root[2])]
            except Exception:
                continue
            if not all(math.isfinite(v) for v in root_vals):
                continue
            clean_shapes: List[List[float]] = []
            for shape in shapes[:RANGE_DYNAMIC_MAX_SHAPES_PER_FRAME]:
                if not isinstance(shape, (tuple, list)) or len(shape) < 4:
                    continue
                try:
                    forward, local_y, local_z, radius = (float(shape[0]), float(shape[1]), float(shape[2]), float(shape[3]))
                except Exception:
                    continue
                if not all(math.isfinite(v) for v in (forward, local_y, local_z, radius)):
                    continue
                if radius <= 0.01 or radius > 1.5:
                    continue
                if abs(forward) > RANGE_PROFILE_MAX_LOCAL_REACH or abs(local_y) > RANGE_PROFILE_MAX_LOCAL_REACH:
                    continue
                clean_shapes.append([forward, local_y, local_z, radius])
            if clean_shapes:
                out.append({
                    "frame": int(item.get("frame") or len(out)),
                    "root": root_vals,
                    "shapes": clean_shapes,
                })
        return out or None

    @staticmethod
    def _dynamic_sweep_payload(frames: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Sanitize collected active frames into the persisted coverage payload."""
        if not isinstance(frames, list):
            return None
        clean: List[Dict[str, Any]] = []
        seen = set()
        for item in frames[:RANGE_DYNAMIC_MAX_FRAMES]:
            if not isinstance(item, dict):
                continue
            try:
                frame_no = int(item.get("frame") or len(clean))
                root = item.get("root") or ()
                root_vals = [float(root[0]), float(root[1]), float(root[2])]
            except Exception:
                continue
            if not all(math.isfinite(v) for v in root_vals):
                continue
            # Dolphin/overlay can tick more than once on one action frame.
            # Keep only the first complete snapshot for that game frame.
            if frame_no in seen:
                continue
            seen.add(frame_no)
            shapes_out: List[List[float]] = []
            for shape in list(item.get("shapes") or [])[:RANGE_DYNAMIC_MAX_SHAPES_PER_FRAME]:
                try:
                    vals = [float(shape[0]), float(shape[1]), float(shape[2]), float(shape[3])]
                except Exception:
                    continue
                if not all(math.isfinite(v) for v in vals) or vals[3] <= 0.01 or vals[3] > 1.5:
                    continue
                if abs(vals[0]) > RANGE_PROFILE_MAX_LOCAL_REACH or abs(vals[1]) > RANGE_PROFILE_MAX_LOCAL_REACH:
                    continue
                shapes_out.append(vals)
            if shapes_out:
                clean.append({"frame": frame_no, "root": root_vals, "shapes": shapes_out})
        if not clean:
            return None
        shape_count = sum(len(item["shapes"]) for item in clean)
        return {
            "version": RANGE_DYNAMIC_SWEEP_VERSION,
            "frames": clean,
            "frame_count": len(clean),
            "shape_count": shape_count,
        }

    def _merge_dynamic_sweep(self, profile: Dict[str, Any], frames: List[Dict[str, Any]], replace: bool = False) -> bool:
        """Attach a one-time multi-hit/vertical coverage supplement.

        It never changes the established single-tip ruler fields. Existing
        static profiles only receive this supplement when Dynamic is enabled
        and the field is genuinely absent.
        """
        if not RANGE_PROFILE_WRITE_ENABLED or not isinstance(profile, dict):
            return False
        if self._range_dynamic_sweep(profile) is not None and not bool(replace):
            return False
        payload = self._dynamic_sweep_payload(frames)
        if payload is None:
            return False
        profile["dynamic_sweep"] = payload
        profile["updated_ms"] = int(pygame.time.get_ticks())
        self._range_profiles_dirty = True
        return True

    @staticmethod
    def _dynamic_sweep_world_shapes(
        center: Tuple[float, float, float], direction: float, frames: List[Dict[str, Any]]
    ) -> List[Tuple[float, float, float, float, int]]:
        """Rebuild saved active hitbox positions from a fresh action-root."""
        cx, cy, cz = (float(center[0]), float(center[1]), float(center[2]))
        direction = 1.0 if float(direction) >= 0.0 else -1.0
        out: List[Tuple[float, float, float, float, int]] = []
        for item in frames:
            root = item.get("root") or (0.0, 0.0, 0.0)
            frame_no = int(item.get("frame") or 0)
            try:
                root_fwd, root_y, root_z = (float(root[0]), float(root[1]), float(root[2]))
            except Exception:
                continue
            for shape in list(item.get("shapes") or []):
                try:
                    local_fwd, local_y, local_z, radius = (float(shape[0]), float(shape[1]), float(shape[2]), float(shape[3]))
                except Exception:
                    continue
                x = cx + direction * (root_fwd + local_fwd)
                y = cy + root_y + local_y
                z = cz + root_z + local_z
                if all(math.isfinite(v) for v in (x, y, z, radius)) and radius > 0.01:
                    out.append((x, y, z, radius, frame_no))
        return out

    @staticmethod
    def _profile_display_mode(profile: Dict[str, Any], saved: Dict[str, Any]) -> str:
        """Return the persisted display policy for a saved body action."""
        mode = str(saved.get("display_mode") or profile.get("display_mode") or "ground_ruler").strip().lower()
        return mode if mode in {"ground_ruler", "dynamic_only"} else "ground_ruler"

    @staticmethod
    def _coverage_slices_from_dynamic_frames(
        frames: Optional[List[Dict[str, Any]]],
        slice_count: int = RANGE_COVERAGE_SLICE_COUNT,
    ) -> Optional[List[Dict[str, float]]]:
        """Approximate full X/Y threat coverage with horizontal slices.

        A slice is calculated from every active hitbox circle that crosses the
        sampled Y height. Root travel and local hitbox travel stay paired per
        frame, so a move cannot borrow max X from one frame and max Y from
        another to create a fake rectangular corner.
        """
        if not isinstance(frames, list) or not frames:
            return None
        circles: List[Tuple[float, float, float]] = []
        lower = None
        upper = None
        for item in frames:
            if not isinstance(item, dict):
                continue
            root = item.get("root")
            shapes = item.get("shapes")
            if not isinstance(root, (tuple, list)) or len(root) < 3 or not isinstance(shapes, list):
                continue
            try:
                root_fwd = float(root[0])
                root_y = float(root[1])
            except Exception:
                continue
            if not all(math.isfinite(v) for v in (root_fwd, root_y)) or abs(root_fwd) > RANGE_PROFILE_MAX_ROOT_ADVANCE:
                continue
            for shape in shapes:
                if not isinstance(shape, (tuple, list)) or len(shape) < 4:
                    continue
                try:
                    local_fwd = float(shape[0])
                    local_y = float(shape[1])
                    radius = float(shape[3])
                except Exception:
                    continue
                if not all(math.isfinite(v) for v in (local_fwd, local_y, radius)):
                    continue
                if radius <= 0.01 or radius > 1.5:
                    continue
                fwd = root_fwd + local_fwd
                y = root_y + local_y
                if abs(fwd) > RANGE_PROFILE_MAX_LOCAL_REACH:
                    continue
                circles.append((fwd, y, radius))
                lo = y - radius
                hi = y + radius
                lower = lo if lower is None else min(lower, lo)
                upper = hi if upper is None else max(upper, hi)
        if not circles or lower is None or upper is None or upper <= lower + 0.01:
            return None
        if float(upper) - float(lower) > RANGE_VERTICAL_ENVELOPE_MAX_SPAN:
            return None

        count = max(5, min(25, int(slice_count or RANGE_COVERAGE_SLICE_COUNT)))
        out: List[Dict[str, float]] = []
        for idx in range(count):
            t = 0.0 if count <= 1 else float(idx) / float(count - 1)
            sample_y = float(lower) + (float(upper) - float(lower)) * t
            near = None
            far = None
            contributors = 0
            for center_fwd, center_y, radius in circles:
                dy = sample_y - center_y
                if abs(dy) > radius + 1e-6:
                    continue
                half = math.sqrt(max(0.0, radius * radius - dy * dy))
                lo = center_fwd - half
                hi = center_fwd + half
                near = lo if near is None else min(near, lo)
                far = hi if far is None else max(far, hi)
                contributors += 1
            if far is None or near is None or contributors <= 0:
                continue
            # The range ruler is a forward-threat guide. Coverage behind the
            # action root is intentionally excluded from this presentation.
            clipped_near = max(0.0, float(near))
            clipped_far = max(clipped_near, float(far))
            if clipped_far <= 0.02 or clipped_far > RANGE_PROFILE_MAX_LOCAL_REACH:
                continue
            out.append({
                "y_from_start": float(sample_y),
                "near_from_start": float(clipped_near),
                "far_from_start": float(clipped_far),
                "contributors": float(contributors),
            })
        return out or None

    @staticmethod
    def _coverage_temporal_layers_from_dynamic_frames(
        frames: Optional[List[Dict[str, Any]]],
        slice_count: int = RANGE_COVERAGE_SLICE_COUNT,
        max_layers: int = RANGE_COVERAGE_TEMPORAL_MAX_LAYERS,
    ) -> Optional[List[Dict[str, Any]]]:
        """Build chronological 2D coverage layers for the heatmap.

        One short active window keeps one layer per sampled action frame. Long
        windows are grouped into chronological buckets so the display stays
        readable. Each layer is still built only from the frames inside that
        bucket, so early geometry can never borrow reach from a later frame.
        """
        if not isinstance(frames, list) or not frames:
            return None
        clean: List[Dict[str, Any]] = []
        for item in frames:
            if not isinstance(item, dict):
                continue
            try:
                frame_no = int(item.get("frame"))
            except Exception:
                continue
            clean.append({**item, "frame": frame_no})
        if not clean:
            return None
        clean.sort(key=lambda item: int(item.get("frame") or 0))

        layer_cap = max(2, min(12, int(max_layers or RANGE_COVERAGE_TEMPORAL_MAX_LAYERS)))
        group_count = min(len(clean), layer_cap)
        groups: List[List[Dict[str, Any]]] = []
        if len(clean) <= layer_cap:
            groups = [[item] for item in clean]
        else:
            for group_index in range(group_count):
                start = int(math.floor(group_index * len(clean) / group_count))
                end = int(math.floor((group_index + 1) * len(clean) / group_count))
                chunk = clean[start:max(start + 1, end)]
                if chunk:
                    groups.append(chunk)

        first_frame = int(clean[0]["frame"])
        last_frame = int(clean[-1]["frame"])
        span = max(1, last_frame - first_frame)
        out: List[Dict[str, Any]] = []
        for chunk in groups:
            slices = HitboxRenderer._coverage_slices_from_dynamic_frames(chunk, slice_count)
            if not slices:
                continue
            frame_start = int(chunk[0]["frame"])
            frame_end = int(chunk[-1]["frame"])
            midpoint = (frame_start + frame_end) * 0.5
            progress = max(0.0, min(1.0, (midpoint - first_frame) / span))
            out.append({
                "frame_start": frame_start,
                "frame_end": frame_end,
                "progress": float(progress),
                "slices": slices,
            })
        return out or None

    def _coverage_first_hit_frame(
        self,
        source_x: float,
        source_y: float,
        direction: float,
        frames: Optional[List[Dict[str, Any]]],
        target_root: Tuple[float, float, float],
        target_local: Dict[str, float],
        hurtboxes: List[HurtboxState],
        adjustment: float = 0.0,
    ) -> Optional[int]:
        """Estimate the first saved action frame whose 2D coverage can hit.

        This intentionally reuses the same body-edge and vertical-overlap math
        as the combined ruler. The result is therefore an approximation of the
        earliest frame at the current spacing, not a collision-engine oracle.
        """
        if not isinstance(frames, list) or not frames:
            return None
        ordered = sorted(
            (item for item in frames if isinstance(item, dict)),
            key=lambda item: int(item.get("frame") or 0),
        )
        for item in ordered:
            slices = self._coverage_slices_from_dynamic_frames([item])
            if not slices:
                continue
            if abs(float(adjustment)) > 1e-6:
                adjusted: List[Dict[str, float]] = []
                for row in slices:
                    near = max(0.0, float(row.get("near_from_start") or 0.0))
                    far = max(near, float(row.get("far_from_start") or 0.0) + float(adjustment))
                    adjusted.append({**row, "near_from_start": near, "far_from_start": far})
                slices = adjusted
            gap, vertical_overlap = self._coverage_gap_to_target(
                float(source_x), float(source_y), direction, slices,
                target_root, target_local, hurtboxes,
            )
            if vertical_overlap and gap is not None and float(gap) <= RANGE_COVERAGE_TOUCH_EPSILON:
                try:
                    return int(item.get("frame"))
                except Exception:
                    return None
        return None

    def _coverage_gap_to_target(
        self,
        source_x: float,
        source_y: float,
        direction: float,
        slices: List[Dict[str, float]],
        target_root: Tuple[float, float, float],
        target_local: Dict[str, float],
        hurtboxes: List[HurtboxState],
    ) -> Tuple[Optional[float], bool]:
        """Return minimum horizontal gap across Y slices that cross the body.

        The horizontal body edge uses the same core-body calculation as the
        working single-axis ruler. Live hurt circles decide only whether the
        target actually occupies each sampled height.
        """
        try:
            sy = float(source_y)
            tx, ty, tz = (float(target_root[0]), float(target_root[1]), float(target_root[2]))
            if not all(math.isfinite(v) for v in (sy, tx, ty, tz)):
                return None, False

            core_hurts: List[Tuple[float, float, float, float]] = []
            for hurt in list(hurtboxes or []):
                try:
                    hx, hy, hz, hr = float(hurt.x), float(hurt.y), float(hurt.z), max(0.0, float(hurt.radius))
                except Exception:
                    continue
                if not all(math.isfinite(v) for v in (hx, hy, hz, hr)) or hr <= 0.01:
                    continue
                if abs(hx - tx) > 1.10 or abs(hy - ty) > 2.80 or abs(hz - tz) > 2.20:
                    continue
                core_hurts.append((hx, hy, hz, hr))

            try:
                fallback_lo = ty + max(-2.40, min(2.40, float(target_local.get("min_y", -1.70))))
                fallback_hi = ty + max(-2.40, min(2.40, float(target_local.get("max_y", 1.70))))
            except Exception:
                fallback_lo, fallback_hi = ty - 1.70, ty + 1.70
            if fallback_hi < fallback_lo:
                fallback_lo, fallback_hi = fallback_hi, fallback_lo

            best_gap: Optional[float] = None
            vertical_overlap = False
            for item in list(slices or []):
                if not isinstance(item, dict):
                    continue
                try:
                    y_world = sy + float(item.get("y_from_start"))
                    far = float(item.get("far_from_start"))
                except Exception:
                    continue
                if not all(math.isfinite(v) for v in (y_world, far)) or far <= 0.02:
                    continue

                if core_hurts:
                    at_height = any(abs(y_world - hy) <= hr + 0.02 for _hx, hy, _hz, hr in core_hurts)
                else:
                    at_height = fallback_lo - 0.02 <= y_world <= fallback_hi + 0.02
                if not at_height:
                    continue
                vertical_overlap = True
                gap = self._horizontal_ruler_gap_to_target(
                    float(source_x), direction, far, target_root, target_local, hurtboxes,
                )
                if gap is None:
                    continue
                best_gap = float(gap) if best_gap is None else min(best_gap, float(gap))
            return best_gap, vertical_overlap
        except Exception:
            return None, False

    @staticmethod
    def _horizontal_ruler_gap_to_target(
        source_x: float,
        direction: float,
        far_units: float,
        target_root: Tuple[float, float, float],
        target_local: Dict[str, float],
        hurtboxes: List[HurtboxState],
    ) -> Optional[float]:
        """Return the visible horizontal gap from the ruler to the target body.

        A skeletal rig contains many circles. The outermost circle is not a
        stable body edge: hands, animation helpers, and occasional descriptor
        outliers can extend well beyond the visible core body. Using min/max
        across the whole rig made the ruler turn green while a clear gap was
        still visible on screen.

        Use a body-weighted live edge instead. The near-side edge is taken from
        the inner 35th percentile of the live hurtbox edges. This keeps the
        current pose responsive while preventing one or two extreme descriptors
        from deciding the result. If live geometry is unavailable, use the
        stored body envelope but cap its horizontal extent to a conservative
        half-width so old accumulated profiles cannot recreate the same bug.
        """
        try:
            sx = float(source_x)
            direction = 1.0 if float(direction) >= 0.0 else -1.0
            far = max(0.0, float(far_units))
            tx = float(target_root[0])
            ty = float(target_root[1])
            tz = float(target_root[2])
            if not all(math.isfinite(v) for v in (sx, far, tx, ty, tz)):
                return None

            if direction > 0.0 and tx < sx - 0.05:
                return None
            if direction < 0.0 and tx > sx + 0.05:
                return None

            ruler_edge = sx + direction * far
            live_edges: List[float] = []
            for hurt in list(hurtboxes or []):
                try:
                    hx = float(hurt.x)
                    hy = float(hurt.y)
                    hz = float(hurt.z)
                    hr = max(0.0, float(hurt.radius))
                except Exception:
                    continue
                if not all(math.isfinite(v) for v in (hx, hy, hz, hr)):
                    continue
                # Reject detached bone/helper descriptors before calculating the
                # body edge. Normal fighters keep their vulnerable core close to
                # the fighter root even when limbs animate farther outward.
                if abs(hx - tx) > 1.10 or abs(hy - ty) > 2.80 or abs(hz - tz) > 2.20:
                    continue
                live_edges.append(hx - hr if direction > 0.0 else hx + hr)

            if len(live_edges) >= 4:
                live_edges.sort()
                n = len(live_edges)
                if direction > 0.0:
                    idx = min(n - 1, max(0, int(math.floor((n - 1) * 0.35))))
                else:
                    idx = min(n - 1, max(0, int(math.ceil((n - 1) * 0.65))))
                target_edge = float(live_edges[idx])
            elif live_edges:
                # Small rigs do not provide enough samples for a percentile.
                # Use their median rather than an extreme edge.
                live_edges.sort()
                target_edge = float(live_edges[len(live_edges) // 2])
            else:
                try:
                    raw_extent = abs(float(target_local["min_x"] if direction > 0.0 else target_local["max_x"]))
                except Exception:
                    raw_extent = 0.38
                body_extent = max(0.18, min(0.48, raw_extent))
                target_edge = tx - body_extent if direction > 0.0 else tx + body_extent

            if not math.isfinite(target_edge):
                return None
            if direction > 0.0:
                return max(0.0, target_edge - ruler_edge)
            return max(0.0, ruler_edge - target_edge)
        except Exception:
            return None

    def _range_target_candidates(
        self,
        source_slot: str,
        display_center: Tuple[float, float, float],
    ) -> List[Tuple[float, str, Tuple[float, float, float], Dict[str, float], str]]:
        """Collect opposing on-stage hurt/body targets for one saved profile."""
        source_team = self._team_for_slot(source_slot)
        if not source_team:
            return []
        candidates = []
        for target_slot in SLOT_BASES:
            if self._team_for_slot(target_slot) == source_team:
                continue
            if not self.slot_renderable.get(target_slot, False):
                continue
            target_root, target_local, target_source, _target_cid = self._body_bounds_for_slot(target_slot)
            if target_root is None or target_local is None:
                continue
            dist = abs(float(target_root[0]) - float(display_center[0]))
            if dist >= 0.05:
                candidates.append((dist, target_slot, target_root, target_local, target_source))
        return candidates

    def _draw_profile_dynamic_sweep(
        self,
        ov: Any,
        source_slot: str,
        saved: Dict[str, Any],
        profile: Dict[str, Any],
        anchor: Tuple[float, float, float],
        direction: float,
        candidates: List[Tuple[float, str, Tuple[float, float, float], Dict[str, float], str]],
    ) -> bool:
        """Draw one persisted multi-hit/vertical sweep and calculate its gap."""
        dynamic_frames = self._range_dynamic_sweep(profile)
        if not dynamic_frames:
            return False
        sweep_shapes = self._dynamic_sweep_world_shapes(anchor, direction, dynamic_frames)
        if not sweep_shapes:
            return False
        dynamic_best = None
        dynamic_slot = ""
        for _dist2, candidate_slot, candidate_root, candidate_local, _candidate_source in candidates:
            hlist = list(self.cached_hurtboxes.get(candidate_slot) or [])
            for sx, sy, sz, sr, _frame_no in sweep_shapes:
                if hlist:
                    for hurt in hlist:
                        dx = float(sx) - float(hurt.x)
                        dy = float(sy) - float(hurt.y)
                        dz = float(sz) - float(hurt.z)
                        gap = max(0.0, math.sqrt(dx * dx + dy * dy + dz * dz) - float(sr) - float(hurt.radius))
                        if dynamic_best is None or gap < dynamic_best[0]:
                            dynamic_best = (gap, candidate_slot)
                else:
                    body_world = self._world_bounds(candidate_root, candidate_local)
                    _touch, gap = self._sphere_aabb_touch_or_gap((sx, sy, sz), sr, body_world)
                    if dynamic_best is None or gap < dynamic_best[0]:
                        dynamic_best = (gap, candidate_slot)
        dynamic_gap = None if dynamic_best is None else float(dynamic_best[0])
        if dynamic_best is not None:
            dynamic_slot = str(dynamic_best[1])
        ov.draw_dynamic_range_sweep(
            self._ruler_source_label(source_slot, saved),
            str(saved.get("move_name") or "RANGE"),
            sweep_shapes,
            dynamic_gap,
            bool(dynamic_gap is not None and dynamic_gap <= 0.045),
            dynamic_slot,
        )
        return True


    def _ruler_source_label(self, source_slot: str, saved: Dict[str, Any]) -> str:
        """Return a human-readable source label without hiding the raw slot.

        Raw slots are P1/P2/P3/P4 in memory order; their on-screen C1/C2
        labels are a separate HUD concern.  Showing the character name here
        prevents a valid Ryu guide from looking like an unexplained ``P3``
        guide.
        """
        try:
            char_id = int(saved.get("char_id") or 0)
        except Exception:
            char_id = 0
        char_name = CHAR_NAMES.get(char_id)
        return f"{char_name} ({source_slot})" if char_name else str(source_slot)

    @staticmethod
    def _range_contact_audit_entry(profile: Dict[str, Any]) -> Dict[str, Any]:
        """Return the append/merge contact-audit block for one attack profile.

        A game impact is real; the hitbox/hurtbox pair below is the closest
        matching live geometry at that moment.  It is intentionally stored as a
        witness rather than claimed to be a raw resolver-owned descriptor pair.
        """
        audit = profile.get("contact_audit")
        if not isinstance(audit, dict):
            audit = {}
            profile["contact_audit"] = audit
        audit["version"] = RANGE_CONTACT_AUDIT_VERSION
        audit.setdefault("confirmed_events", 0)
        audit.setdefault("ruler_mismatches", 0)
        audit.setdefault("unresolved_events", 0)
        audit.setdefault("truth_events", 0)
        audit.setdefault("last_signal", "")
        audit.setdefault("last_mismatch", False)
        audit.setdefault("last_update_ms", 0)
        witnesses = audit.get("witnesses")
        if not isinstance(witnesses, list):
            witnesses = []
            audit["witnesses"] = witnesses
        return audit

    @staticmethod
    def _range_contact_pair_metrics(hit: Dict[str, Any], hurt: HurtboxState) -> Dict[str, float]:
        """Return 2D circle-pair metrics in the same x/y space as hitboxes.

        Hurtbox z is intentionally not folded in here: the player hitbox table
        is a 2D x/y circle list, and the existing overlay/contact helpers use
        that same plane.  Treating z as a fake third hitbox coordinate made
        valid pairs disappear on characters with animated bone depth.
        """
        hx = float(hit["x"])
        hy = float(hit["y"])
        hr = max(0.0, float(hit["radius"]))
        dx = hx - float(hurt.x)
        dy = hy - float(hurt.y)
        distance = math.hypot(dx, dy)
        combined = hr + max(0.0, float(hurt.radius))
        surface_gap = max(0.0, distance - combined)
        overlap = max(0.0, combined - distance)
        if distance > 1e-6:
            ux, uy = dx / distance, dy / distance
            px = hx - ux * hr
            py = hy - uy * hr
        else:
            px = (hx + float(hurt.x)) * 0.5
            py = (hy + float(hurt.y)) * 0.5
        return {
            "surface_gap": float(surface_gap),
            "overlap": float(overlap),
            "point_x": float(px),
            "point_y": float(py),
            "point_z": float(hurt.z),
        }

    def _capture_range_active_snapshot(self, source_slot: str, meta: Dict[str, Any], now_ms: int) -> None:
        """Keep a few exact active-frame body-hitbox snapshots for one move.

        This reads at most the three normal player descriptors for a source that
        is already running a saved range profile.  It is not a normal scan and
        remains active even when the visual Hitboxes layer is hidden.
        """
        if source_slot not in SLOT_BASES or not isinstance(meta, dict):
            return
        try:
            base = SLOT_BASES[source_slot]
            state_id = decode_state_id(read_state_raw(base))
            expected_state = int(meta.get("state_id") or state_id)
            if int(state_id) != expected_state:
                return
            profile_key = str(meta.get("profile_key") or "")
            if not profile_key:
                char_id = int(meta.get("char_id") or 0)
                move_key = int(meta.get("move_key") or 0)
                profile_key = f"{char_id}:{move_key}"
            root = tuple(float(v) for v in read_fighter_root(base))
            if not all(math.isfinite(v) for v in root):
                return
            boxes = read_hitboxes(base, HITBOX)
            active_boxes: List[Dict[str, Any]] = []
            action_frame = None
            for index, (x, y, radius, flag) in enumerate(boxes):
                if float(radius) <= 0.001:
                    continue
                _draw_now, is_active, action_frame, _fd = self._frame_gate_for_slot(
                    source_slot, base, int(state_id), int(flag)
                )
                if not is_active:
                    continue
                if not all(math.isfinite(float(v)) for v in (x, y, radius)):
                    continue
                active_boxes.append({
                    "index": int(index),
                    "x": float(x),
                    "y": float(y),
                    "z": 0.0,
                    "radius": max(0.0, float(radius)),
                })
            if not active_boxes:
                return
            raw_origin = meta.get("action_origin")
            if isinstance(raw_origin, (tuple, list)) and len(raw_origin) >= 3:
                origin = tuple(float(raw_origin[i]) for i in range(3))
            else:
                origin = root
            snapshot = {
                "ms": int(now_ms),
                "profile_key": profile_key,
                "move_name": str(meta.get("move_name") or "RANGE"),
                "state_id": int(state_id),
                "action_frame": int(action_frame) if isinstance(action_frame, int) else None,
                "root": root,
                "action_origin": origin,
                "boxes": active_boxes,
            }
            history = self._range_contact_snapshots.setdefault(source_slot, [])
            if history and str(history[-1].get("profile_key") or "") == profile_key and history[-1].get("action_frame") == snapshot.get("action_frame"):
                history[-1] = snapshot
            else:
                history.append(snapshot)
            self._range_contact_snapshots[source_slot] = history[-8:]
        except Exception:
            return

    def _recent_range_active_snapshot(self, source_slot: str, profile_key: str, now_ms: int) -> Optional[Dict[str, Any]]:
        history = self._range_contact_snapshots.get(source_slot) or []
        for snapshot in reversed(history):
            try:
                if str(snapshot.get("profile_key") or "") != str(profile_key):
                    continue
                if int(now_ms) - int(snapshot.get("ms") or 0) > RANGE_CONTACT_SNAPSHOT_MAX_AGE_MS:
                    continue
                if isinstance(snapshot.get("boxes"), list) and snapshot["boxes"]:
                    return snapshot
            except Exception:
                continue
        return None

    def _range_profile_prediction_for_snapshot(
        self,
        profile: Dict[str, Any],
        snapshot: Dict[str, Any],
        target_hurtboxes: List[HurtboxState],
    ) -> Tuple[bool, Optional[float]]:
        """Evaluate the retained single-tip estimate at one observed hit frame."""
        geometry = self._range_profile_geometry(profile)
        if geometry is None or not target_hurtboxes:
            return False, None
        try:
            root = snapshot.get("root")
            if not isinstance(root, (tuple, list)) or len(root) < 3:
                return False, None
            target_x = sum(float(h.x) for h in target_hurtboxes) / float(len(target_hurtboxes))
            direction = 1.0 if target_x >= float(root[0]) else -1.0
            _adjustment, _pips = self._range_adjustment(profile)
            tip_x = float(root[0]) + direction * (float(geometry["tip_center_from_start"]) + _adjustment)
            tip_y = float(root[1]) + float(geometry["tip_y_from_start"])
            tip_z = float(root[2]) + float(geometry["tip_z_from_start"])
            radius = max(0.0, float(geometry["tip_radius"]))
            best = None
            for hurt in target_hurtboxes:
                gap = max(0.0, math.sqrt((tip_x - float(hurt.x)) ** 2 + (tip_y - float(hurt.y)) ** 2 + (tip_z - float(hurt.z)) ** 2) - radius - float(hurt.radius))
                best = gap if best is None else min(best, gap)
            return bool(best is not None and float(best) <= 0.045), best
        except Exception:
            return False, None

    def _record_range_contact_witness(
        self,
        source_slot: str,
        target_slot: str,
        profile_key: str,
        snapshot: Dict[str, Any],
        hit: Dict[str, Any],
        hurt: HurtboxState,
        metrics: Dict[str, float],
        signal: str,
        predicted_touch: bool,
        now_ms: int,
    ) -> None:
        """Merge one engine-signalled impact into a move's audit witnesses."""
        if not RANGE_PROFILE_WRITE_ENABLED:
            return
        profile = (self._range_profiles.get("attacks") or {}).get(str(profile_key))
        if not isinstance(profile, dict):
            return
        try:
            target_char_id = int(rd32(SLOT_BASES[target_slot] + OFF_CHAR_ID) or 0)
        except Exception:
            target_char_id = 0
        try:
            origin = snapshot.get("action_origin")
            if not isinstance(origin, (tuple, list)) or len(origin) < 3:
                origin = snapshot.get("root")
            if not isinstance(origin, (tuple, list)) or len(origin) < 3:
                return
            direction = 1.0 if float(hurt.x) >= float(origin[0]) else -1.0
            local_x = (float(hit["x"]) - float(origin[0])) * direction
            local_y = float(hit["y"]) - float(origin[1])
            local_z = float(hit.get("z") or 0.0) - float(origin[2])
        except Exception:
            return
        audit = self._range_contact_audit_entry(profile)
        witnesses = audit["witnesses"]
        hit_index = int(hit.get("index") or 0)
        hurt_index = int(hurt.index)
        action_frame = snapshot.get("action_frame")
        match = None
        for witness in witnesses:
            if not isinstance(witness, dict):
                continue
            if (
                int(witness.get("target_char_id") or 0) == int(target_char_id)
                and int(witness.get("hitbox_index") or -1) == hit_index
                and int(witness.get("hurtbox_index") or -1) == hurt_index
            ):
                match = witness
                break
        if match is None:
            match = {
                "target_char_id": int(target_char_id),
                "hitbox_index": hit_index,
                "hurtbox_index": hurt_index,
                "samples": 0,
            }
            witnesses.append(match)
        prior_samples = max(0, int(match.get("samples") or 0))
        # Merge position/radius gently so a single freeze-frame timing offset
        # cannot replace an older good witness.
        alpha = 1.0 if prior_samples <= 0 else 0.35
        def blend(key: str, value: float) -> None:
            old = match.get(key)
            try:
                oldf = float(old)
            except Exception:
                oldf = value
            match[key] = (oldf * (1.0 - alpha)) + (float(value) * alpha)
        blend("local_x", local_x)
        blend("local_y", local_y)
        blend("local_z", local_z)
        blend("radius", max(0.0, float(hit.get("radius") or 0.0)))
        blend("surface_gap", max(0.0, float(metrics.get("surface_gap") or 0.0)))
        blend("overlap", max(0.0, float(metrics.get("overlap") or 0.0)))
        sample_total = prior_samples + 1
        match["action_frame"] = int(action_frame) if isinstance(action_frame, int) else None
        match["signal"] = str(signal)
        match["confidence"] = "geometry_overlap" if float(metrics.get("surface_gap") or 0.0) <= 0.045 else "nearest_geometry"
        match["last_seen_ms"] = int(now_ms)
        match["samples"] = sample_total
        match["truth"] = bool(sample_total >= RANGE_CONTACT_TRUTH_MIN_SAMPLES)
        match["predicted_touch_last"] = bool(predicted_touch)
        match["mismatch_count"] = int(match.get("mismatch_count") or 0) + (0 if predicted_touch else 1)
        audit["confirmed_events"] = int(audit.get("confirmed_events") or 0) + 1
        audit["truth_events"] = int(audit.get("truth_events") or 0) + 1
        audit["last_signal"] = str(signal)
        audit["last_mismatch"] = not bool(predicted_touch)
        audit["last_update_ms"] = int(now_ms)
        if not predicted_touch:
            audit["ruler_mismatches"] = int(audit.get("ruler_mismatches") or 0) + 1
        # Keep a compact latest summary for exports and external audit tools;
        # callers do not have to infer the current truth state from a list.
        audit["latest"] = {
            "target_char_id": int(target_char_id),
            "hitbox_index": hit_index,
            "hurtbox_index": hurt_index,
            "action_frame": int(action_frame) if isinstance(action_frame, int) else None,
            "samples": int(sample_total),
            "truth": bool(sample_total >= RANGE_CONTACT_TRUTH_MIN_SAMPLES),
            "predicted_touch": bool(predicted_touch),
            "surface_gap": float(metrics.get("surface_gap") or 0.0),
            "signal": str(signal),
            "updated_ms": int(now_ms),
        }
        # Keep the most supported / newest witness set compact and auditable.
        witnesses[:] = sorted(
            [item for item in witnesses if isinstance(item, dict)],
            key=lambda item: (int(item.get("samples") or 0), int(item.get("last_seen_ms") or 0)),
            reverse=True,
        )[:RANGE_CONTACT_MAX_WITNESSES]
        profile["updated_ms"] = int(now_ms)
        self._range_profiles_dirty = True

    def _process_range_contact_audit(
        self,
        now_ms: int,
        hp_drops: Dict[str, int],
        impact_events: Dict[str, int],
    ) -> None:
        """Record the best live geometry witness for one engine-signalled hit/block.

        Damage loss validates a hit.  The impact-freeze edge additionally covers
        blocks and zero-damage interactions.  Pair ownership is labelled
        *inferred* because TvC's available compact event record exposes actor
        source/target but not raw descriptor indices.
        """
        signalled_targets = set(str(slot) for slot, delta in hp_drops.items() if int(delta or 0) > 0)
        signalled_targets.update(str(slot) for slot, value in impact_events.items() if int(value or 0) > 0)
        if not signalled_targets:
            return
        for target_slot in signalled_targets:
            target_hurtboxes = list(self.cached_hurtboxes.get(target_slot) or [])
            if not target_hurtboxes or target_slot not in SLOT_BASES:
                continue
            target_team = self._team_for_slot(target_slot)
            best = None
            for source_slot, history in list(self._range_contact_snapshots.items()):
                if self._team_for_slot(source_slot) == target_team:
                    continue
                if not history:
                    continue
                for snapshot in reversed(history):
                    try:
                        age = int(now_ms) - int(snapshot.get("ms") or 0)
                        if age < 0 or age > RANGE_CONTACT_SNAPSHOT_MAX_AGE_MS:
                            continue
                        profile_key = str(snapshot.get("profile_key") or "")
                        profile = (self._range_profiles.get("attacks") or {}).get(profile_key)
                        if not isinstance(profile, dict):
                            continue
                        for hit in snapshot.get("boxes") or []:
                            if not isinstance(hit, dict):
                                continue
                            for hurt in target_hurtboxes:
                                metrics = self._range_contact_pair_metrics(hit, hurt)
                                candidate = (
                                    float(metrics["surface_gap"]),
                                    -float(metrics["overlap"]),
                                    age,
                                    source_slot,
                                    profile_key,
                                    snapshot,
                                    hit,
                                    hurt,
                                    metrics,
                                    profile,
                                )
                                if best is None or candidate[:3] < best[:3]:
                                    best = candidate
                    except Exception:
                        continue
            if best is None:
                continue
            gap, _neg_overlap, _age, source_slot, profile_key, snapshot, hit, hurt, metrics, profile = best
            audit = self._range_contact_audit_entry(profile)
            if float(gap) > RANGE_CONTACT_WITNESS_MAX_GAP:
                audit["unresolved_events"] = int(audit.get("unresolved_events") or 0) + 1
                self._range_profiles_dirty = True
                continue
            predicted_touch, predicted_gap = self._range_profile_prediction_for_snapshot(profile, snapshot, target_hurtboxes)
            signal = "damage" if int(hp_drops.get(target_slot) or 0) > 0 else "impact"
            self._record_range_contact_witness(
                source_slot, target_slot, profile_key, snapshot, hit, hurt,
                metrics, signal, predicted_touch, now_ms,
            )
            marker = {
                "until_ms": int(now_ms) + RANGE_CONTACT_MARKER_MS,
                "source_slot": str(source_slot),
                "target_slot": str(target_slot),
                "hitbox_index": int(hit.get("index") or 0),
                "hurtbox_index": int(hurt.index),
                "x": float(metrics["point_x"]),
                "y": float(metrics["point_y"]),
                "z": float(metrics["point_z"]),
                "surface_gap": float(metrics["surface_gap"]),
                "predicted_touch": bool(predicted_touch),
                "predicted_gap": predicted_gap,
                "signal": signal,
                "kind": "witness",
            }
            self._range_contact_markers.append(marker)
            self._range_contact_markers = self._range_contact_markers[-8:]
            mismatch = "mismatch" if not predicted_touch else "aligned"
            print(
                f"[contact truth] {signal} {profile_key}: {source_slot}[{marker['hitbox_index']}] -> "
                f"{target_slot}[{marker['hurtbox_index']}] inferred {mismatch}; "
                f"pair_gap={marker['surface_gap']:.3f}; saved=1"
            )

    def _refresh_range_contact_expectations(self, now_ms: int) -> None:
        """Check stored witnesses against the same live descriptors next time.

        This is a diagnostic overlay, not a synthetic collision.  It tells us
        whether a previously witnessed descriptor pair is lining up now before
        the game resolves another hit.
        """
        self._range_contact_expectations.clear()
        for source_slot, history in list(self._range_contact_snapshots.items()):
            if not history:
                continue
            snapshot = history[-1]
            try:
                if int(now_ms) - int(snapshot.get("ms") or 0) > RANGE_CONTACT_EXPECTED_MARKER_MS:
                    continue
                # Exact-pair checks are meaningful only on a confirmed active
                # snapshot. Do not redraw a stale truth marker in recovery.
                if snapshot.get("action_frame") is None:
                    continue
                profile_key = str(snapshot.get("profile_key") or "")
                profile = (self._range_profiles.get("attacks") or {}).get(profile_key)
                if not isinstance(profile, dict):
                    continue
                audit = profile.get("contact_audit")
                witnesses = audit.get("witnesses") if isinstance(audit, dict) else None
                if not isinstance(witnesses, list) or not witnesses:
                    continue
                source_team = self._team_for_slot(source_slot)
                for target_slot, target_base in SLOT_BASES.items():
                    if self._team_for_slot(target_slot) == source_team:
                        continue
                    hlist = list(self.cached_hurtboxes.get(target_slot) or [])
                    if not hlist:
                        continue
                    target_char_id = int(rd32(target_base + OFF_CHAR_ID) or 0)
                    boxes_by_index = {int(box.get("index") or 0): box for box in snapshot.get("boxes") or [] if isinstance(box, dict)}
                    hurts_by_index = {int(hurt.index): hurt for hurt in hlist}
                    for witness in witnesses:
                        if not isinstance(witness, dict) or int(witness.get("target_char_id") or 0) != target_char_id:
                            continue
                        hit = boxes_by_index.get(int(witness.get("hitbox_index") or -1))
                        hurt = hurts_by_index.get(int(witness.get("hurtbox_index") or -1))
                        if hit is None or hurt is None:
                            continue
                        metrics = self._range_contact_pair_metrics(hit, hurt)
                        current = self._range_contact_expectations.get(source_slot)
                        witness_samples = max(0, int(witness.get("samples") or 0))
                        candidate = {
                            "until_ms": int(now_ms) + RANGE_CONTACT_EXPECTED_MARKER_MS,
                            "source_slot": source_slot,
                            "target_slot": target_slot,
                            "profile_key": profile_key,
                            "move_name": str(snapshot.get("move_name") or "RANGE"),
                            "hitbox_index": int(hit.get("index") or 0),
                            "hurtbox_index": int(hurt.index),
                            "x": float(metrics["point_x"]),
                            "y": float(metrics["point_y"]),
                            "z": float(metrics["point_z"]),
                            "surface_gap": float(metrics["surface_gap"]),
                            "samples": witness_samples,
                            "truth": bool(witness.get("truth") or witness_samples >= RANGE_CONTACT_TRUTH_MIN_SAMPLES),
                            "kind": "expected",
                        }
                        if current is None or float(candidate["surface_gap"]) < float(current.get("surface_gap") or 999.0):
                            self._range_contact_expectations[source_slot] = candidate
            except Exception:
                continue

    def _apply_range_calibration(self, profile_key: str, step: int, reason: str) -> None:
        """Adjust one retained ruler by a single bounded calibration pip."""
        # Calibration is also profile mutation.  In the default locked build it
        # remains observational only and never changes the stored JSON.
        if not RANGE_PROFILE_WRITE_ENABLED:
            return
        profile = (self._range_profiles.get("attacks") or {}).get(str(profile_key))
        if not isinstance(profile, dict):
            return
        calibration = self._range_calibration_entry(profile)
        before = int(calibration.get("pips") or 0)
        after = max(-RANGE_CALIBRATION_MAX_PIPS, min(RANGE_CALIBRATION_MAX_PIPS, before + int(step)))
        if after == before:
            return
        calibration["pips"] = after
        calibration["updated_ms"] = int(pygame.time.get_ticks())
        if step > 0:
            calibration["false_negative_hits"] = int(calibration.get("false_negative_hits") or 0) + 1
            calibration["false_positive_streak"] = 0
        else:
            calibration["false_positive_adjustments"] = int(calibration.get("false_positive_adjustments") or 0) + 1
            calibration["false_positive_streak"] = 0
        self._range_profiles_dirty = True
        print(f"[range calibration] {profile_key} {reason}: pip {before:+d} -> {after:+d}")

    def _observe_range_calibration(
        self,
        source_slot: str,
        profile_key: str,
        target_slot: str,
        predicted_touch: bool,
        now_ms: int,
    ) -> None:
        """Start or update one active move observation for adaptive reach."""
        previous = self._range_calibration_attempts.get(source_slot)
        if isinstance(previous, dict) and str(previous.get("profile_key") or "") != str(profile_key):
            # A new move replaced the old action before its grace window ended.
            # Drop the old partial sample rather than treating it as a whiff.
            self._range_calibration_attempts.pop(source_slot, None)
            previous = None
        if not isinstance(previous, dict):
            previous = {
                "profile_key": str(profile_key),
                "target_slots": set(),
                "started_ms": int(now_ms),
                "last_active_ms": int(now_ms),
                "predicted_touch": False,
                "resolved_hit": False,
            }
            self._range_calibration_attempts[source_slot] = previous
        previous["last_active_ms"] = int(now_ms)
        previous["predicted_touch"] = bool(previous.get("predicted_touch", False) or predicted_touch)
        if target_slot:
            targets = previous.get("target_slots")
            if not isinstance(targets, set):
                targets = set(targets or ())
                previous["target_slots"] = targets
            targets.add(str(target_slot))

    def _tick_range_calibration(self, now_ms: int, active_sources: set[str], hp_drops: Dict[str, int]) -> None:
        """Resolve hit/miss observations once the active window has finished."""
        for source_slot, attempt in list(self._range_calibration_attempts.items()):
            if not isinstance(attempt, dict):
                self._range_calibration_attempts.pop(source_slot, None)
                continue
            targets = attempt.get("target_slots")
            if not isinstance(targets, set):
                targets = set(targets or ())
                attempt["target_slots"] = targets
            if any(int(hp_drops.get(target) or 0) > 0 for target in targets):
                attempt["resolved_hit"] = True
            if source_slot in active_sources:
                continue
            if int(now_ms) - int(attempt.get("last_active_ms") or now_ms) < RANGE_CALIBRATION_GRACE_MS:
                continue

            profile_key = str(attempt.get("profile_key") or "")
            predicted_touch = bool(attempt.get("predicted_touch", False))
            resolved_hit = bool(attempt.get("resolved_hit", False))
            profile = (self._range_profiles.get("attacks") or {}).get(profile_key)
            if isinstance(profile, dict):
                calibration = self._range_calibration_entry(profile)
                if resolved_hit:
                    # The move dealt damage while its ruler never reached the
                    # target: extend by one visible pip.
                    if not predicted_touch:
                        self._apply_range_calibration(profile_key, +1, "hit beyond ruler")
                    else:
                        calibration["false_positive_streak"] = 0
                elif predicted_touch:
                    # Do not overreact to one spacing sample. Three predicted
                    # contacts without damage remove one pip.
                    streak = int(calibration.get("false_positive_streak") or 0) + 1
                    calibration["false_positive_streak"] = streak
                    if streak >= RANGE_CALIBRATION_FALSE_POSITIVE_LIMIT:
                        self._apply_range_calibration(profile_key, -1, "repeated touching whiff")
                    self._range_profiles_dirty = True
            self._range_calibration_attempts.pop(source_slot, None)

    def _range_move_origin(
        self,
        source_slot: str,
        char_id: int,
        state_id: int,
        move_id: int,
        action_frame: Optional[int],
        fighter_root: Tuple[float, float, float],
    ) -> Tuple[float, float, float]:
        """Return the fighter-root position captured at this action's start.

        The root is stable across crouch/standing hurtbox poses.  A state ID is
        not enough on its own because repeated normals can reuse the same state,
        so an action-counter rewind also begins a fresh capture.
        """
        try:
            root = (float(fighter_root[0]), float(fighter_root[1]), float(fighter_root[2]))
            if not all(math.isfinite(v) for v in root):
                return root
            previous = self._range_move_origins.get(source_slot)
            starts_new = (
                not isinstance(previous, dict)
                or int(previous.get("char_id") or -1) != int(char_id)
                or int(previous.get("state_id") or -1) != int(state_id)
                or int(previous.get("move_id") or -1) != int(move_id)
            )
            if not starts_new and action_frame is not None:
                prior_frame = previous.get("last_action_frame")
                try:
                    # A normal replay can retain the same decoded state.  The
                    # action counter restarting is the reliable new-instance
                    # boundary; tolerate one frame of rounding jitter.
                    starts_new = prior_frame is not None and int(action_frame) + 1 < int(prior_frame)
                except Exception:
                    starts_new = False
            if starts_new:
                self._range_move_origins[source_slot] = {
                    "char_id": int(char_id),
                    "state_id": int(state_id),
                    "move_id": int(move_id),
                    "root": root,
                    "started_ms": int(pygame.time.get_ticks()),
                    "last_action_frame": action_frame,
                }
                return root
            if isinstance(previous, dict):
                previous["last_action_frame"] = action_frame
                saved = previous.get("root")
                if isinstance(saved, (tuple, list)) and len(saved) >= 3:
                    origin = (float(saved[0]), float(saved[1]), float(saved[2]))
                    if all(math.isfinite(v) for v in origin):
                        return origin
                previous["root"] = root
            return root
        except Exception:
            return tuple(float(v) for v in fighter_root)

    def _observe_range_ground_reference(
        self,
        source_slot: str,
        state_id: int,
        fighter_root: Tuple[float, float, float],
    ) -> None:
        """Refresh one slot's floor-level root reference from true ground states."""
        if source_slot not in SLOT_BASES or int(state_id) not in RANGE_EXPLICIT_GROUND_STATE_IDS:
            return
        try:
            root_y = float(fighter_root[1])
        except Exception:
            return
        if not math.isfinite(root_y):
            return
        previous = self._range_ground_reference_y.get(source_slot)
        if previous is None or not math.isfinite(float(previous)):
            self._range_ground_reference_y[source_slot] = root_y
            return
        # A stage/camera transition can legitimately move the whole reference.
        # Snap large changes; otherwise smooth minor float jitter.
        if abs(root_y - float(previous)) > 1.25:
            self._range_ground_reference_y[source_slot] = root_y
        else:
            alpha = float(RANGE_GROUND_REFERENCE_ALPHA)
            self._range_ground_reference_y[source_slot] = (float(previous) * (1.0 - alpha)) + (root_y * alpha)

    def _range_slot_is_airborne(
        self,
        source_slot: str,
        state_id: int,
        fighter_root: Tuple[float, float, float],
    ) -> bool:
        """Return the live physical ground/air mode for ruler presentation.

        State IDs catch the first jump/air-dash frames; root height catches
        attack-state IDs that do not themselves identify as air attacks.
        Labels are intentionally not part of this decision.
        """
        try:
            sid = int(state_id)
            root_y = float(fighter_root[1])
        except Exception:
            return False
        baseline = self._range_ground_reference_y.get(source_slot)
        if baseline is not None and math.isfinite(root_y) and math.isfinite(float(baseline)):
            root_delta = abs(root_y - float(baseline))
            # Landing truth must beat a stale explicit-air state.  TvC can
            # retain the jump/air action ID for recovery frames after the
            # model/root has already returned to the floor; without this
            # root snap, an old j.A/j.B/j.C ruler remains selected on landing.
            if root_delta <= float(RANGE_GROUND_SNAP_ROOT_DELTA):
                return False
            if root_delta >= float(RANGE_AIRBORNE_ROOT_DELTA):
                return True
            # Between the two thresholds, retain the engine's early-jump
            # signal so the display changes promptly once the root departs the
            # floor, but never lets that state override a confirmed landing.
            return sid in RANGE_EXPLICIT_AIR_STATE_IDS
        # Before a floor reference has been learned, state IDs remain the only
        # reliable immediate hint.  Once the reference exists, root position
        # above has priority for landing truth.
        return sid in RANGE_EXPLICIT_AIR_STATE_IDS

    @staticmethod
    def _range_label_is_crouching(label: Any) -> bool:
        """Return True for standard down-input normals, e.g. 2A/2B/2C."""
        text = str(label or "").strip().lower()
        if not text:
            return False
        canonical = text.replace("jump.", "j.").replace("jump ", "j.")
        canonical = canonical.replace("air.", "j.").replace("air ", "j.")
        compact = re.sub(r"[\s._-]+", "", canonical)
        return bool(re.fullmatch(r"2[abc]", compact))

    def _range_action_posture(
        self,
        source_slot: str,
        state_id: int,
        fighter_root: Tuple[float, float, float],
        move_name: Any = None,
        airborne_hint: bool = False,
    ) -> str:
        """Classify one action as standing, crouching, or airborne.

        Live air state/root height wins.  A normal's canonical 2A/2B/2C label
        catches crouching attack states whose raw state ID is not the passive
        crouch state.  This bucket is purely a display selector; it does not
        alter profile geometry or the saved JSON schema.
        """
        if bool(airborne_hint) or self._range_slot_is_airborne(source_slot, int(state_id), fighter_root):
            return "air"
        if int(state_id) in RANGE_CROUCH_STATE_IDS or self._range_label_is_crouching(move_name):
            return "crouch"
        return "stand"

    def _range_live_posture(
        self,
        source_slot: str,
        state_id: int,
        fighter_root: Tuple[float, float, float],
    ) -> str:
        """Return the current physical presentation bucket for a fighter."""
        if self._range_slot_is_airborne(source_slot, int(state_id), fighter_root):
            return "air"
        if int(state_id) in RANGE_CROUCH_STATE_IDS:
            return "crouch"
        return "stand"

    def _note_range_action_posture(
        self,
        source_slot: str,
        posture: Any,
        profile_key: Any,
        state_id: int,
        action_frame: Optional[int],
        now_ms: int,
    ) -> None:
        """Latch an action's intended presentation posture for its lifetime.

        TvC can replace a 2A/2B/2C action state with a generic standing or
        recovery state before the animation visually finishes.  The geometry
        is still the crouching action, so the ruler must keep that action's
        bucket rather than re-selecting the last standing normal for a frame.
        This is transient UI state only; no profile data is changed.
        """
        if source_slot not in SLOT_BASES:
            return
        bucket = str(posture or "stand").strip().lower()
        if bucket not in {"stand", "crouch", "air"}:
            bucket = "stand"
        self._range_posture_locks[source_slot] = {
            "posture": bucket,
            "profile_key": str(profile_key or ""),
            "state_id": int(state_id),
            "action_frame": action_frame if isinstance(action_frame, int) else None,
            "last_seen_ms": int(now_ms),
        }

    def _range_presentation_posture(
        self,
        source_slot: str,
        state_id: int,
        fighter_root: Tuple[float, float, float],
        now_ms: int,
        action_posture: Any = None,
        profile_key: Any = None,
        action_frame: Optional[int] = None,
    ) -> str:
        """Choose the stable ruler bucket for the current frame.

        A current action label wins immediately.  Once the engine drops that
        label into an ambiguous standing/recovery state, hold its bucket just
        long enough for the passive crouch/stand state to settle.  Air is
        always immediate so a jump never carries a floor ruler upward.
        """
        raw = self._range_live_posture(source_slot, int(state_id), fighter_root)
        if action_posture is not None:
            bucket = str(action_posture).strip().lower()
            if bucket in {"stand", "crouch", "air"}:
                # A stale j.* descriptor can survive the physical landing for
                # a frame or two.  Once the root says ground, do not let that
                # stale action descriptor re-arm the air lock; immediately
                # select the current grounded posture/ruler instead.
                if bucket == "air" and raw != "air":
                    self._range_posture_locks.pop(source_slot, None)
                    return raw
                self._note_range_action_posture(
                    source_slot, bucket, profile_key, int(state_id), action_frame, int(now_ms),
                )
                return bucket

        # A confirmed physical air state takes priority over a stale ground
        # lock.  The opposite direction is held briefly only to suppress the
        # generic-state wobble seen on crouching normals.
        if raw == "air":
            self._range_posture_locks.pop(source_slot, None)
            return "air"

        lock = self._range_posture_locks.get(source_slot)
        if not isinstance(lock, dict):
            return raw
        bucket = str(lock.get("posture") or "stand").strip().lower()
        if bucket not in {"stand", "crouch", "air"}:
            self._range_posture_locks.pop(source_slot, None)
            return raw
        try:
            age_ms = max(0, int(now_ms) - int(lock.get("last_seen_ms") or now_ms))
        except Exception:
            age_ms = RANGE_POSTURE_ACTION_HOLD_MS + 1

        # Same bucket is already stable; discard the temporary lock.
        if raw == bucket:
            self._range_posture_locks.pop(source_slot, None)
            return raw

        # The only common bad hand-off is crouch -> generic stand while the
        # attack still animates.  Keep the crouch guide through a short grace
        # period, then let a genuinely settled standing pose win.
        if bucket == "crouch" and raw == "stand" and age_ms <= RANGE_POSTURE_ACTION_HOLD_MS:
            return "crouch"
        if bucket == "stand" and raw == "crouch" and age_ms <= RANGE_POSTURE_PASSIVE_SETTLE_MS:
            return "stand"

        self._range_posture_locks.pop(source_slot, None)
        return raw

    def _remember_range_for_posture(
        self,
        source_slot: str,
        saved: Dict[str, Any],
        posture: Any,
    ) -> None:
        """Store the last action under its own posture without profile writes."""
        if source_slot not in SLOT_BASES or not isinstance(saved, dict):
            return
        bucket = str(posture or "stand").strip().lower()
        if bucket not in {"stand", "crouch", "air"}:
            bucket = "stand"
        entry = dict(saved)
        entry["posture"] = bucket
        self._saved_ranges[source_slot] = entry
        self._saved_ranges_by_posture.setdefault(source_slot, {})[bucket] = entry

    @staticmethod
    def _pending_max_active_frame(fd: MoveFrameData) -> int:
        try:
            return max(int(end) for _start, end in (fd.active_windows or ()))
        except Exception:
            return 0

    def _nearest_opponent_direction(self, source_slot: str, source_root: Tuple[float, float, float]) -> float:
        """Use nearest opposing live root as the action-facing sign."""
        source_team = self._team_for_slot(source_slot)
        best: Optional[Tuple[float, float]] = None
        for target_slot, target_base in SLOT_BASES.items():
            if self._team_for_slot(target_slot) == source_team:
                continue
            if not self.slot_renderable.get(target_slot, False):
                continue
            try:
                tx, _ty, _tz = read_fighter_root(target_base)
                tx = float(tx)
                if not math.isfinite(tx):
                    continue
                dist = abs(tx - float(source_root[0]))
                if best is None or dist < best[0]:
                    best = (dist, tx)
            except Exception:
                continue
        if best is None:
            return 1.0
        return 1.0 if best[1] >= float(source_root[0]) else -1.0

    def _capture_dynamic_sweep_frame(
        self,
        pending: Dict[str, Any],
        start_root: Tuple[float, float, float],
        active_root: Tuple[float, float, float],
        shapes: List[Tuple[float, float, float, float]],
        direction: float,
        action_frame: Optional[int],
    ) -> None:
        """Append one active-frame root-path/local-shape snapshot to a collector."""
        try:
            if not isinstance(pending, dict) or len(list(pending.get("dynamic_frames") or [])) >= RANGE_DYNAMIC_MAX_FRAMES:
                return
            sx, sy, sz = (float(start_root[0]), float(start_root[1]), float(start_root[2]))
            ax, ay, az = (float(active_root[0]), float(active_root[1]), float(active_root[2]))
            direction = 1.0 if float(direction) >= 0.0 else -1.0
            frame_no = int(action_frame) if isinstance(action_frame, int) else len(list(pending.get("dynamic_frames") or []))
            frames = pending.setdefault("dynamic_frames", [])
            if any(int(item.get("frame") or -999999) == frame_no for item in frames if isinstance(item, dict)):
                return
            local_shapes: List[List[float]] = []
            for x, y, z, radius in shapes[:RANGE_DYNAMIC_MAX_SHAPES_PER_FRAME]:
                x, y, z, radius = (float(x), float(y), float(z), float(radius))
                values = ((x - ax) * direction, y - ay, z - az, radius)
                if all(math.isfinite(v) for v in values) and radius > 0.01:
                    local_shapes.append([float(v) for v in values])
            if not local_shapes:
                return
            frames.append({
                "frame": frame_no,
                "root": [(ax - sx) * direction, ay - sy, az - sz],
                "shapes": local_shapes,
            })
        except Exception:
            return

    def _pending_range_preview_profile(self, pending: Any) -> Optional[Dict[str, Any]]:
        """Build an in-memory ruler profile from a special that is still active.

        Special effective range used to become visible only after the collector
        finalized at the end of the move. This preview uses the exact same
        confirmed active samples immediately, without writing a partial profile
        to disk. The finalized profile remains the persistent source afterward.
        """
        if not isinstance(pending, dict) or not bool(pending.get("special_session")):
            return None
        frames = list(pending.get("dynamic_frames") or [])
        if not frames:
            return None
        profile_key = str(pending.get("profile_key") or "")
        existing = (self._range_profiles.get("attacks") or {}).get(profile_key)
        preview: Dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}

        if self._range_profile_core_geometry(preview) is None:
            shapes = list(pending.get("best_shapes") or [])
            start_root = pending.get("start_root")
            active_root = pending.get("best_active_root")
            if not shapes or not isinstance(start_root, (tuple, list)) or not isinstance(active_root, (tuple, list)):
                return None
            try:
                ox, oy, oz = (float(start_root[0]), float(start_root[1]), float(start_root[2]))
                ax, ay, az = (float(active_root[0]), float(active_root[1]), float(active_root[2]))
                direction = 1.0 if float(pending.get("facing") or 1.0) >= 0.0 else -1.0
            except Exception:
                return None
            best = None
            for shape in shapes:
                try:
                    x, y, z, radius = (float(shape[0]), float(shape[1]), float(shape[2]), float(shape[3]))
                except Exception:
                    continue
                if not all(math.isfinite(v) for v in (x, y, z, radius)) or radius <= 0.01 or radius > 1.5:
                    continue
                center_from_start = (x - ox) * direction
                reach = center_from_start + radius
                advance = (ax - ox) * direction
                local_forward = (x - ax) * direction
                if reach <= 0.03 or reach > RANGE_PROFILE_MAX_LOCAL_REACH or abs(advance) > RANGE_PROFILE_MAX_ROOT_ADVANCE:
                    continue
                candidate = (reach, center_from_start, y - oy, z - oz, radius, advance, local_forward)
                if best is None or candidate[0] > best[0]:
                    best = candidate
            if best is None:
                return None
            reach, center_from_start, local_y, local_z, radius, advance, local_forward = best
            preview.update({
                "anchor": "fighter_root",
                "profile_schema": RANGE_PROFILE_SCHEMA,
                "char_id": int(pending.get("char_id") or 0),
                "move_id": int(pending.get("move_id") or 0),
                "move_name": str(pending.get("move_name") or "RANGE"),
                "samples": 0,
                "reach_from_start": float(reach),
                "tip_center_from_start": float(center_from_start),
                "tip_y_from_start": float(local_y),
                "tip_z_from_start": float(local_z),
                "tip_radius": float(radius),
                "advance_at_tip": float(advance),
                "tip_center_from_active_root": float(local_forward),
                "calibration": {"pips": 0, "false_negative_hits": 0, "false_positive_streak": 0, "false_positive_adjustments": 0},
            })

        payload = self._dynamic_sweep_payload(frames)
        if payload is not None:
            preview["dynamic_sweep"] = payload
        horizontal = self._horizontal_envelope_from_dynamic_frames(frames)
        if horizontal is not None:
            preview["horizontal_envelope"] = {
                "version": RANGE_HORIZONTAL_ENVELOPE_VERSION,
                "reach_from_start": float(horizontal["reach_from_start"]),
                "tip_center_from_start": float(horizontal["tip_center_from_start"]),
                "tip_y_from_start": float(horizontal["tip_y_from_start"]),
                "tip_z_from_start": float(horizontal["tip_z_from_start"]),
                "tip_radius": float(horizontal["tip_radius"]),
                "advance_at_tip": float(horizontal["advance_at_tip"]),
                "tip_center_from_active_root": float(horizontal["tip_center_from_active_root"]),
                "active_frame": int(horizontal.get("active_frame") or 0),
                "shape_index": int(horizontal.get("shape_index") or 0),
            }
        vertical = self._vertical_envelope_from_dynamic_frames(frames)
        if vertical is not None:
            preview["vertical_envelope"] = {
                "version": RANGE_VERTICAL_ENVELOPE_VERSION,
                "upper_y_from_start": float(vertical["upper_y_from_start"]),
                "lower_y_from_start": float(vertical["lower_y_from_start"]),
                "upper_forward_from_start": float(vertical["upper_forward_from_start"]),
                "lower_forward_from_start": float(vertical["lower_forward_from_start"]),
                "upper_z_from_start": float(vertical["upper_z_from_start"]),
                "lower_z_from_start": float(vertical["lower_z_from_start"]),
                "upper_radius": float(vertical["upper_radius"]),
                "lower_radius": float(vertical["lower_radius"]),
                "upper_active_frame": int(vertical.get("upper_active_frame") or 0),
                "lower_active_frame": int(vertical.get("lower_active_frame") or 0),
                "upper_shape_index": int(vertical.get("upper_shape_index") or 0),
                "lower_shape_index": int(vertical.get("lower_shape_index") or 0),
            }
        preview["move_name"] = str(pending.get("move_name") or preview.get("move_name") or "RANGE")
        preview["move_kind"] = str(pending.get("move_kind") or preview.get("move_kind") or "ground_special")
        preview["display_mode"] = "ground_ruler"
        preview["runtime_preview"] = True
        return preview


    def _range_special_session_descriptor(
        self,
        source_slot: str,
        char_id: int,
        state_id: int,
        descriptor: Dict[str, Any],
        fighter_root: Tuple[float, float, float],
        now_ms: int,
    ) -> Dict[str, Any]:
        """Bind related internal special states to one owning move profile.

        The first useful special state opens a session. If a concrete strength
        label appears after a generic startup state, ownership is promoted to
        that strength while preserving the original action-start root and any
        samples already collected.
        """
        out = dict(descriptor or {})
        kind = str(out.get("kind") or "")
        if not kind.endswith("special"):
            self._range_special_sessions.pop(source_slot, None)
            return out
        name = str(out.get("move_name") or "").strip()
        family = _range_special_family(name)
        if not family:
            return out
        try:
            root = tuple(float(v) for v in fighter_root[:3])
        except Exception:
            root = tuple(float(v) for v in fighter_root)
        session = self._range_special_sessions.get(source_slot)
        valid = False
        if isinstance(session, dict):
            try:
                age = int(now_ms) - int(session.get("last_seen_ms") or now_ms)
                total = int(now_ms) - int(session.get("started_ms") or now_ms)
                same_char = int(session.get("char_id") or -1) == int(char_id)
                within_window = age <= RANGE_SPECIAL_SESSION_GRACE_MS and total <= RANGE_SPECIAL_SESSION_MAX_MS
                current_specific = _range_special_has_explicit_strength(name)
                owner_specific = bool(session.get("owner_specific"))
                owner_move_id = int(session.get("owner_move_id") or -1)
                current_move_id = int(out.get("move_id") or state_id)

                # Once a concrete command owns a live-hitbox session, internal
                # phase IDs are allowed to change family/name without breaking
                # the effective-range capture. A new concrete action ID starts
                # a new session, which keeps L/M/H and other strengths distinct.
                explicit_new_command = bool(
                    current_specific
                    and owner_specific
                    and current_move_id != owner_move_id
                )
                valid = bool(same_char and within_window and not explicit_new_command)
            except Exception:
                valid = False
        if not valid:
            session = {
                "char_id": int(char_id),
                "family": family,
                "owner_move_id": int(out.get("move_id") or state_id),
                "owner_move_name": name,
                "owner_state_id": int(state_id),
                "owner_specific": _range_special_has_explicit_strength(name),
                "start_root": root,
                "started_ms": int(now_ms),
                "last_seen_ms": int(now_ms),
            }
            self._range_special_sessions[source_slot] = session
        else:
            session["last_seen_ms"] = int(now_ms)
            # A generic phase can be observed before the strength-specific ID.
            # Promote the owner in place so the earlier startup/motion samples
            # remain part of the final strength profile.
            current_specific = _range_special_has_explicit_strength(name)
            if current_specific and not bool(session.get("owner_specific")):
                old_key = f"{int(char_id)}:{int(session.get('owner_move_id') or state_id)}"
                new_id = int(out.get("move_id") or state_id)
                new_key = f"{int(char_id)}:{new_id}"
                session["owner_move_id"] = new_id
                session["owner_move_name"] = name
                session["owner_state_id"] = int(state_id)
                session["owner_specific"] = True
                pending = self._range_pending_learns.get(source_slot)
                if isinstance(pending, dict) and str(pending.get("profile_key") or "") == old_key:
                    pending["profile_key"] = new_key
                    pending["move_id"] = new_id
                    pending["move_name"] = name
                    pending["state_id"] = int(state_id)
        out["special_family"] = family
        out["special_session"] = True
        out["move_id"] = int(session.get("owner_move_id") or out.get("move_id") or state_id)
        out["move_name"] = str(session.get("owner_move_name") or name)
        out["session_state_id"] = int(session.get("owner_state_id") or state_id)
        out["session_start_root"] = session.get("start_root") or root
        return out


    def _queue_missing_range_profile(
        self,
        source_slot: str,
        char_id: int,
        state_id: int,
        fd: Optional[MoveFrameData],
        fighter_root: Tuple[float, float, float],
        action_frame: Optional[int],
        now_ms: int,
        descriptor: Dict[str, Any],
        capture_samples_for_existing: bool = False,
    ) -> None:
        """Capture one missing body-attack profile or missing axis envelope.

        Ground/air normals with preview frame data sample only their confirmed
        active windows. Mapped specials do not have a full frame profile yet,
        so they use the existing exact live ``0x53`` attack-flag gate. That is
        deliberately narrower than treating any allocated descriptor during a
        special as an active hitbox.
        """
        if not RANGE_PROFILE_WRITE_ENABLED or not RANGE_PROFILE_AUTO_LEARN_MISSING_ONLY:
            return
        if not isinstance(descriptor, dict):
            return
        try:
            move_id = int(descriptor.get("move_id") or state_id)
            move_name = str(descriptor.get("move_name") or f"0x{move_id:04X}")
            move_kind = str(descriptor.get("kind") or "ground_special")
            airborne_now = bool(descriptor.get("runtime_airborne"))
            display_mode = "ground_ruler"
            posture = str(descriptor.get("posture") or self._range_action_posture(
                source_slot, int(state_id), tuple(float(v) for v in fighter_root), move_name, airborne_now,
            ))
            frame_gated = bool(descriptor.get("frame_gated"))
        except Exception:
            return

        profile_key = f"{int(char_id)}:{move_id}"
        existing = (self._range_profiles.get("attacks") or {}).get(profile_key)
        core_exists = isinstance(existing, dict) and self._range_profile_core_geometry(existing) is not None
        # A known profile is sampled once more only when it lacks either axis
        # envelope and has no retained active-frame samples to derive it from.
        # The same compact capture provides both Horz and Vert.
        refresh_existing = bool(core_exists and capture_samples_for_existing and move_kind.endswith("special"))
        needs_envelope = bool(
            core_exists and (
                refresh_existing
                or self._range_profile_horizontal_envelope(existing) is None
                or self._range_profile_vertical_envelope(existing) is None
            )
        )
        needs_samples = bool(
            core_exists
            and (refresh_existing or needs_envelope)
            and (refresh_existing or self._range_dynamic_sweep(existing) is None)
        )
        if core_exists and not needs_samples:
            return

        try:
            root = tuple(float(v) for v in fighter_root)
            if not all(math.isfinite(v) for v in root):
                return
        except Exception:
            return

        # Specials can span several internal state IDs. A sticky session owns
        # one start root across those phases; normals retain exact action origin.
        special_session = bool(descriptor.get("special_session"))
        session_state_id = int(descriptor.get("session_state_id") or state_id)
        raw_session_root = descriptor.get("session_start_root")
        if special_session and isinstance(raw_session_root, (tuple, list)) and len(raw_session_root) >= 3:
            try:
                origin = tuple(float(raw_session_root[i]) for i in range(3))
            except Exception:
                origin = root
        else:
            origin = self._range_move_origin(source_slot, int(char_id), session_state_id, move_id, action_frame, root)
        pending = self._range_pending_learns.get(source_slot)
        pending_key = str(pending.get("profile_key") or "") if isinstance(pending, dict) else ""
        if pending_key != profile_key:
            if isinstance(pending, dict):
                self._finalize_missing_range_profile(source_slot, now_ms, "new move")
            pending = {
                "profile_key": profile_key,
                "needs_core": not core_exists,
                "needs_samples": bool(needs_samples or not core_exists),
                "needs_envelope": bool(needs_envelope or not core_exists),
                "refresh_existing": bool(refresh_existing),
                "char_id": int(char_id),
                "state_id": int(state_id),
                "move_id": move_id,
                "move_name": move_name,
                "move_kind": move_kind,
                "display_mode": display_mode,
                "airborne_action": bool(airborne_now),
                "posture": posture,
                "frame_gated": frame_gated,
                "special_session": bool(special_session),
                "special_family": str(descriptor.get("special_family") or ""),
                "active_sample_index": 0,
                "start_root": origin,
                "facing": self._nearest_opponent_direction(source_slot, origin),
                "max_active_frame": self._pending_max_active_frame(fd) if fd is not None else 0,
                "last_action_frame": action_frame,
                "last_seen_ms": int(now_ms),
                "best_score": float("-inf"),
                "best_active_root": None,
                "best_shapes": [],
                "dynamic_frames": [],
            }
            self._range_pending_learns[source_slot] = pending
        else:
            pending["last_seen_ms"] = int(now_ms)
            pending["last_action_frame"] = action_frame
            pending["airborne_action"] = bool(pending.get("airborne_action")) or bool(airborne_now)
            pending["display_mode"] = "ground_ruler"
            if bool(pending.get("airborne_action")):
                pending["posture"] = "air"
            if fd is not None:
                pending["max_active_frame"] = max(
                    int(pending.get("max_active_frame") or 0),
                    self._pending_max_active_frame(fd),
                )

        boxes: Optional[List[Tuple[float, float, float, int]]] = None
        active_now = False
        if frame_gated:
            active_now = is_frame_data_active(fd, action_frame)
        else:
            # No preview frame data for specials yet. Read only this fighter's
            # three descriptors while the one missing/supplemental action is in
            # progress, then require the engine's exact active marker.
            try:
                boxes = read_hitboxes(SLOT_BASES[source_slot], HITBOX)
            except Exception:
                boxes = None
            active_now = any(
                int(flag) == 0x53 and math.isfinite(float(radius)) and float(radius) > 0.01
                for _x, _y, radius, flag in (boxes or [])
            )
        if not active_now:
            return

        if boxes is None:
            try:
                boxes = read_hitboxes(SLOT_BASES[source_slot], HITBOX)
            except Exception:
                return
        shapes: List[Tuple[float, float, float, float]] = []
        for x, y, radius, flag in boxes:
            try:
                x, y, radius = float(x), float(y), max(0.0, float(radius))
            except Exception:
                continue
            if not all(math.isfinite(v) for v in (x, y, radius)) or radius <= 0.01:
                continue
            # For an unprofiled special, only use actual active player attack
            # descriptors. Frame-gated normals can retain their complete live
            # active shape set because their timing has already been validated.
            if not frame_gated and int(flag) != 0x53:
                continue
            shapes.append((x, y, 0.0, radius))
        if not shapes:
            return

        direction = 1.0 if float(pending.get("facing") or 1.0) >= 0.0 else -1.0
        start_root = pending.get("start_root") or origin
        if bool(pending.get("needs_samples", True)) or bool(pending.get("needs_envelope", False)):
            capture_frame = action_frame
            if bool(pending.get("special_session")):
                # Special counters can be phase-local or use subframe units.
                # Use the raw state/counter only to suppress duplicate renderer
                # ticks, then persist a compact chronological active-sample ID.
                raw_marker = None
                if isinstance(action_frame, int):
                    raw_marker = (int(state_id), int(action_frame))
                if raw_marker is not None and pending.get("last_special_capture_marker") == raw_marker:
                    capture_frame = None
                else:
                    if raw_marker is not None:
                        pending["last_special_capture_marker"] = raw_marker
                    capture_frame = int(pending.get("active_sample_index") or 0) + 1
                    pending["active_sample_index"] = int(capture_frame)
            if capture_frame is not None:
                self._capture_dynamic_sweep_frame(pending, start_root, root, shapes, direction, capture_frame)
        try:
            sx = float(start_root[0])
        except Exception:
            return
        best_score = max(((float(x) - sx) * direction + float(radius)) for x, _y, _z, radius in shapes)
        if math.isfinite(best_score) and best_score > float(pending.get("best_score") or float("-inf")):
            pending["best_score"] = float(best_score)
            pending["best_active_root"] = root
            pending["best_shapes"] = shapes


    def _finalize_missing_range_profile(self, source_slot: str, now_ms: int, reason: str) -> None:
        """Commit one missing body-action profile from its confirmed active set."""
        pending = self._range_pending_learns.pop(source_slot, None)
        self._range_move_origins.pop(source_slot, None)
        if not isinstance(pending, dict):
            return
        profile_key = str(pending.get("profile_key") or "")
        attacks = self._range_profiles.get("attacks") or {}
        existing = attacks.get(profile_key)
        core_exists = isinstance(existing, dict) and self._range_profile_core_geometry(existing) is not None
        shapes = pending.get("best_shapes")
        active_root = pending.get("best_active_root")
        start_root = pending.get("start_root")
        dynamic_frames = list(pending.get("dynamic_frames") or [])
        airborne_action = bool(pending.get("airborne_action"))
        display_mode = "ground_ruler"
        move_kind = str(pending.get("move_kind") or "ground_special")

        horizontal_saved = False
        vertical_saved = False
        samples_saved = False
        if core_exists:
            entry = existing
            entry.setdefault("display_mode", display_mode)
            entry.setdefault("move_kind", move_kind)
            refresh_existing = bool(pending.get("refresh_existing"))
            if bool(pending.get("needs_envelope")):
                horizontal_saved = self._apply_horizontal_envelope(entry, dynamic_frames, replace=refresh_existing)
                vertical_saved = self._apply_vertical_envelope(entry, dynamic_frames, replace=refresh_existing)
            if bool(pending.get("needs_samples")):
                samples_saved = self._merge_dynamic_sweep(entry, dynamic_frames, replace=refresh_existing)
            if not horizontal_saved and not vertical_saved and not samples_saved:
                return
        else:
            if not isinstance(shapes, list) or not shapes or not isinstance(active_root, (tuple, list)) or not isinstance(start_root, (tuple, list)):
                return
            entry = self._learn_attack_profile(
                int(pending.get("char_id") or 0),
                int(pending.get("move_id") or 0),
                str(pending.get("move_name") or "RANGE"),
                tuple(float(v) for v in start_root[:3]),
                tuple(float(v) for v in active_root[:3]),
                shapes,
                float(pending.get("facing") or 1.0),
            )
            if not isinstance(entry, dict) or self._range_profile_core_geometry(entry) is None:
                return
            entry["display_mode"] = display_mode
            entry["move_kind"] = move_kind
            horizontal_saved = self._apply_horizontal_envelope(entry, dynamic_frames)
            vertical_saved = self._apply_vertical_envelope(entry, dynamic_frames)
            samples_saved = self._merge_dynamic_sweep(entry, dynamic_frames)

        saved_entry = {
            "profile_key": profile_key,
            "move_key": int(pending.get("move_id") or 0),
            "move_name": str(pending.get("move_name") or "RANGE"),
            "last_direction": float(pending.get("facing") or 1.0),
            "captured_state_id": int(pending.get("state_id") or 0),
            "char_id": int(pending.get("char_id") or 0),
            "display_mode": display_mode,
            "airborne_action": bool(airborne_action),
            "posture": str(pending.get("posture") or ("air" if airborne_action else "stand")),
            "move_kind": move_kind,
            "action_origin": tuple(float(v) for v in start_root[:3]) if isinstance(start_root, (tuple, list)) else None,
            "dynamic_live_until_ms": int(now_ms) + RANGE_DYNAMIC_LINGER_MS,
            "profile_locked": False,
            "profile_source": "auto-learned",
            "default_profile": False,
        }
        self._remember_range_for_posture(source_slot, saved_entry, saved_entry.get("posture"))
        exported = self._flush_range_profiles(force=True)
        export_note = "saved" if exported else "queued for retry"
        if horizontal_saved and vertical_saved:
            mode_note = "Horz + Vert envelopes"
        elif horizontal_saved:
            mode_note = "Horz envelope"
        elif vertical_saved:
            mode_note = "Vert envelope"
        else:
            mode_note = "active-frame samples"
        print(
            f"[range profile] saved {mode_note} for {profile_key} ({pending.get('move_name')}) "
            f"from one complete active window; {export_note}; reason={reason}"
        )


    def _finalize_missing_range_profiles(self, seen_slots: set[str], now_ms: int) -> None:
        """Commit collectors at recovery/state exit; never reopen known moves."""
        for source_slot, pending in list(self._range_pending_learns.items()):
            if not isinstance(pending, dict):
                self._range_pending_learns.pop(source_slot, None)
                continue
            action_frame = pending.get("last_action_frame")
            max_active = int(pending.get("max_active_frame") or 0)
            elapsed = int(now_ms) - int(pending.get("last_seen_ms") or now_ms)
            past_active = isinstance(action_frame, int) and max_active > 0 and int(action_frame) > max_active
            if bool(pending.get("special_session")):
                # Internal phase changes can briefly pass through an unmapped or
                # passive state. Do not truncate the special on that hand-off.
                if elapsed >= RANGE_SPECIAL_SESSION_GRACE_MS:
                    self._finalize_missing_range_profile(source_slot, now_ms, "special end")
                    self._range_special_sessions.pop(source_slot, None)
                continue
            if source_slot not in seen_slots or past_active or elapsed >= 420:
                self._finalize_missing_range_profile(source_slot, now_ms, "recovery" if past_active else "state exit")

    def _learn_attack_profile(
        self,
        char_id: int,
        move_id: int,
        move_name: str,
        start_root: Tuple[float, float, float],
        active_root: Tuple[float, float, float],
        shapes: List[Tuple[float, float, float, float]],
        facing: float,
    ) -> Optional[Dict[str, Any]]:
        """Learn the farthest active tip from the action-start fighter root.

        Stored fields deliberately separate the move's root travel from the
        hitbox extension.  Rendering later reuses ``reach_from_start`` from the
        *current* root, so a retained ruler never points back to an old location.
        """
        # Discovery-only guard: a valid entry is authoritative.  Even if a
        # future caller accidentally reaches this learner, it cannot replace or
        # add samples to an already learned move profile.
        key = f"{int(char_id)}:{int(move_id)}"
        existing = (self._range_profiles.get("attacks") or {}).get(key)
        if isinstance(existing, dict) and self._range_profile_core_geometry(existing) is not None:
            return existing
        if not RANGE_PROFILE_WRITE_ENABLED:
            return None
        if not shapes:
            return None
        try:
            ox, oy, oz = (float(start_root[0]), float(start_root[1]), float(start_root[2]))
            ax, ay, az = (float(active_root[0]), float(active_root[1]), float(active_root[2]))
            direction = 1.0 if float(facing) >= 0.0 else -1.0
            if not all(math.isfinite(v) for v in (ox, oy, oz, ax, ay, az)):
                return None

            best: Optional[Tuple[float, float, float, float, float, float, float]] = None
            for x, y, z, r in shapes:
                x, y, z = float(x), float(y), float(z)
                r = max(0.0, float(r))
                if not all(math.isfinite(v) for v in (x, y, z, r)):
                    continue
                tip_center_from_start = (x - ox) * direction
                reach_from_start = tip_center_from_start + r
                advance_at_tip = (ax - ox) * direction
                tip_center_from_active_root = (x - ax) * direction
                if (
                    reach_from_start <= 0.03
                    or reach_from_start > RANGE_PROFILE_MAX_LOCAL_REACH
                    or abs(advance_at_tip) > RANGE_PROFILE_MAX_ROOT_ADVANCE
                    or r > 1.5
                ):
                    continue
                candidate = (
                    reach_from_start,
                    tip_center_from_start,
                    y - oy,
                    z - oz,
                    r,
                    advance_at_tip,
                    tip_center_from_active_root,
                )
                if best is None or candidate[0] > best[0]:
                    best = candidate
            if best is None:
                return None

            (
                reach_from_start,
                tip_center_from_start,
                tip_y_from_start,
                tip_z_from_start,
                tip_radius,
                advance_at_tip,
                tip_center_from_active_root,
            ) = best
            key = f"{int(char_id)}:{int(move_id)}"
            attacks = self._range_profiles.setdefault("attacks", {})
            entry = attacks.get(key)
            if not isinstance(entry, dict) or str(entry.get("anchor") or "") != "fighter_root":
                entry = {
                    "anchor": "fighter_root",
                    "profile_schema": RANGE_PROFILE_SCHEMA,
                    "char_id": int(char_id),
                    "move_id": int(move_id),
                    "move_name": str(move_name or f"0x{int(move_id):04X}"),
                    "samples": 0,
                    "reach_from_start": 0.0,
                    "tip_center_from_start": 0.0,
                    "tip_y_from_start": 0.0,
                    "tip_z_from_start": 0.0,
                    "tip_radius": 0.0,
                    "advance_at_tip": 0.0,
                    "tip_center_from_active_root": 0.0,
                    "calibration": {"pips": 0, "false_negative_hits": 0, "false_positive_streak": 0, "false_positive_adjustments": 0},
                }
                attacks[key] = entry
            self._range_calibration_entry(entry)

            # Keep the complete active-frame sample with the farthest valid
            # world-space edge from the same action-start root.
            if reach_from_start >= float(entry.get("reach_from_start") or 0.0):
                entry["reach_from_start"] = max(0.0, min(RANGE_PROFILE_MAX_LOCAL_REACH, reach_from_start))
                entry["tip_center_from_start"] = max(0.0, min(RANGE_PROFILE_MAX_LOCAL_REACH, tip_center_from_start))
                entry["tip_y_from_start"] = tip_y_from_start
                entry["tip_z_from_start"] = tip_z_from_start
                entry["tip_radius"] = max(0.0, min(1.5, tip_radius))
                entry["advance_at_tip"] = advance_at_tip
                entry["tip_center_from_active_root"] = tip_center_from_active_root
            entry["move_name"] = str(move_name or entry.get("move_name") or f"0x{int(move_id):04X}")
            entry["samples"] = int(entry.get("samples") or 0) + 1
            entry["last_sample"] = {
                "start_root": [ox, oy, oz],
                "active_root": [ax, ay, az],
                "reach_from_start": reach_from_start,
                "advance_at_tip": advance_at_tip,
                "tip_center_from_active_root": tip_center_from_active_root,
            }
            entry["updated_ms"] = int(pygame.time.get_ticks())
            self._range_profiles_dirty = True
            return entry
        except Exception:
            return None

    def _learn_body_profile(
        self,
        char_id: int,
        root: Tuple[float, float, float],
        hurtboxes: List[HurtboxState],
    ) -> Optional[Dict[str, Any]]:
        """Merge skeletal hurt circles into a persisted root-relative body AABB."""
        key = str(int(char_id))
        if not RANGE_PROFILE_WRITE_ENABLED:
            existing = (self._range_profiles.get("bodies") or {}).get(key)
            return existing if isinstance(existing, dict) else None
        if not hurtboxes:
            return None
        try:
            rx, ry, rz = (float(root[0]), float(root[1]), float(root[2]))
            if not all(math.isfinite(v) for v in (rx, ry, rz)):
                return None
            key = str(int(char_id))
            bodies = self._range_profiles.setdefault("bodies", {})
            entry = bodies.get(key)
            if not isinstance(entry, dict):
                entry = {"char_id": int(char_id), "samples": 0, "bounds": self._profile_bounds_default()}
                bodies[key] = entry
            bounds = entry.get("bounds")
            if not self._profile_bounds_valid(bounds):
                bounds = self._profile_bounds_default()
                entry["bounds"] = bounds
            for hurt in hurtboxes:
                x, y, z, r = float(hurt.x), float(hurt.y), float(hurt.z), max(0.0, float(hurt.radius))
                if not all(math.isfinite(v) for v in (x, y, z, r)):
                    continue
                bounds["min_x"] = min(float(bounds["min_x"]), x - r - rx)
                bounds["max_x"] = max(float(bounds["max_x"]), x + r - rx)
                bounds["min_y"] = min(float(bounds["min_y"]), y - r - ry)
                bounds["max_y"] = max(float(bounds["max_y"]), y + r - ry)
                bounds["min_z"] = min(float(bounds["min_z"]), z - r - rz)
                bounds["max_z"] = max(float(bounds["max_z"]), z + r - rz)
            entry["samples"] = int(entry.get("samples") or 0) + 1
            entry["updated_ms"] = int(pygame.time.get_ticks())
            self._range_profiles_dirty = True
            return entry
        except Exception:
            return None

    @staticmethod
    def _bounds_from_hurtboxes(root: Tuple[float, float, float], hurtboxes: List[HurtboxState]) -> Optional[Dict[str, float]]:
        try:
            rx, ry, rz = (float(root[0]), float(root[1]), float(root[2]))
            if not hurtboxes:
                return None
            vals = {"min_x": float("inf"), "max_x": float("-inf"), "min_y": float("inf"), "max_y": float("-inf"), "min_z": float("inf"), "max_z": float("-inf")}
            for hurt in hurtboxes:
                x, y, z, r = float(hurt.x), float(hurt.y), float(hurt.z), max(0.0, float(hurt.radius))
                vals["min_x"] = min(vals["min_x"], x - r - rx)
                vals["max_x"] = max(vals["max_x"], x + r - rx)
                vals["min_y"] = min(vals["min_y"], y - r - ry)
                vals["max_y"] = max(vals["max_y"], y + r - ry)
                vals["min_z"] = min(vals["min_z"], z - r - rz)
                vals["max_z"] = max(vals["max_z"], z + r - rz)
            return vals if all(math.isfinite(float(v)) for v in vals.values()) else None
        except Exception:
            return None

    def _body_bounds_for_slot(self, slot_name: str) -> Tuple[Optional[Tuple[float, float, float]], Optional[Dict[str, float]], str, int]:
        """Resolve a live body AABB, falling back to that character's learned envelope."""
        base = SLOT_BASES.get(slot_name)
        if base is None:
            return None, None, "none", 0
        try:
            root = tuple(float(v) for v in read_fighter_root(base))
            char_id = int(rd32(base + OFF_CHAR_ID) or 0)
        except Exception:
            return None, None, "none", 0
        if char_id <= 0 or char_id == 0xFFFFFFFF or not all(math.isfinite(v) for v in root):
            return None, None, "none", 0
        if abs(root[1]) >= 70.0 or abs(root[2]) >= 70.0:
            return None, None, "none", 0
        hlist = list(self.cached_hurtboxes.get(slot_name) or [])
        live_bounds = self._bounds_from_hurtboxes(root, hlist)
        if live_bounds is not None:
            # Live hurt geometry is used only for the current range check.
            # It is not folded back into the saved body envelope.
            return root, live_bounds, "live", char_id
        profile = (self._range_profiles.get("bodies") or {}).get(str(char_id))
        if isinstance(profile, dict) and self._profile_bounds_valid(profile.get("bounds")):
            return root, dict(profile["bounds"]), "profile", char_id
        return root, dict(RANGE_PROFILE_GENERIC_BODY), "learning", char_id

    @staticmethod
    def _aabb_touch_or_gap(a: Dict[str, float], b: Dict[str, float]) -> Tuple[bool, float]:
        """Return 3D AABB touch state and positive horizontal separation."""
        x_gap = max(float(b["min_x"]) - float(a["max_x"]), float(a["min_x"]) - float(b["max_x"]), 0.0)
        y_gap = max(float(b["min_y"]) - float(a["max_y"]), float(a["min_y"]) - float(b["max_y"]), 0.0)
        z_gap = max(float(b["min_z"]) - float(a["max_z"]), float(a["min_z"]) - float(b["max_z"]), 0.0)
        return (x_gap <= 0.045 and y_gap <= 0.10 and z_gap <= 0.16), x_gap

    @staticmethod
    def _team_for_slot(slot_name: str) -> str:
        # Raw fighter slots are ordered by team: P1/P2 are the left team and
        # P3/P4 are the right team.  The visible C1/C2 labels are applied later.
        return "P1" if str(slot_name) in {"P1", "P2"} else ("P2" if str(slot_name) in {"P3", "P4"} else "")

    @staticmethod
    def _world_bounds(root: Tuple[float, float, float], local: Dict[str, float]) -> Dict[str, float]:
        return {
            "min_x": float(root[0]) + float(local["min_x"]),
            "max_x": float(root[0]) + float(local["max_x"]),
            "min_y": float(root[1]) + float(local["min_y"]),
            "max_y": float(root[1]) + float(local["max_y"]),
            "min_z": float(root[2]) + float(local["min_z"]),
            "max_z": float(root[2]) + float(local["max_z"]),
        }

    @staticmethod
    def _body_center_world(root: Tuple[float, float, float], local: Dict[str, float]) -> Tuple[float, float, float]:
        """Return the center of a live/profiled fighter body envelope."""
        return (
            float(root[0]) + (float(local["min_x"]) + float(local["max_x"])) * 0.5,
            float(root[1]) + (float(local["min_y"]) + float(local["max_y"])) * 0.5,
            float(root[2]) + (float(local["min_z"]) + float(local["max_z"])) * 0.5,
        )

    @staticmethod
    def _sphere_aabb_touch_or_gap(center: Tuple[float, float, float], radius: float, bounds: Dict[str, float]) -> Tuple[bool, float]:
        """Return sphere-to-body-box touch state and the positive surface gap."""
        cx, cy, cz = (float(center[0]), float(center[1]), float(center[2]))
        nearest_x = min(max(cx, float(bounds["min_x"])), float(bounds["max_x"]))
        nearest_y = min(max(cy, float(bounds["min_y"])), float(bounds["max_y"]))
        nearest_z = min(max(cz, float(bounds["min_z"])), float(bounds["max_z"]))
        distance = math.sqrt((cx - nearest_x) ** 2 + (cy - nearest_y) ** 2 + (cz - nearest_z) ** 2)
        gap = max(0.0, distance - max(0.0, float(radius)))
        return gap <= 0.045, gap

    @staticmethod
    def _hitboxes_enabled(control=None) -> bool:
        """Attack/projectile hitbox layer toggle."""
        return bool(getattr(control, "show_hitboxes", True))

    @staticmethod
    def _hurtboxes_enabled(control=None) -> bool:
        """Defender/body hurtbox layer toggle."""
        return bool(getattr(control, "show_hurtboxes", True)) and _hurtbox_layer_requested()

    def _any_collision_layer_enabled(self, control=None) -> bool:
        return (
            self._hitboxes_enabled(control)
            or self._hurtboxes_enabled(control)
            or _range_ruler_enabled()
        )

    def _clear_runtime_hitbox_state(self) -> None:
        self.cached_projectiles.clear()
        self.cached_hurtboxes.clear()
        self.cached_hit_contacts.clear()
        self._last_range_rulers.clear()
        self._saved_ranges.clear()
        self._saved_ranges_by_posture = {slot: {} for slot in SLOT_BASES}
        self._range_calibration_attempts.clear()
        self._range_last_hp.clear()
        self._range_move_origins.clear()
        self._range_pending_learns.clear()
        self._range_special_sessions.clear()
        self._range_contact_snapshots.clear()
        self._range_contact_markers.clear()
        self._range_contact_expectations.clear()
        self._range_last_impact_freeze.clear()
        self._range_ground_reference_y.clear()
        self._move_phase_state.clear()
        self.hurt_counts = {}
        self.slot_renderable = {slot: False for slot in SLOT_BASES}
        self.last_counts = {}
        self.motion_filter._states.clear()
        self._hitbox_ghost_frames.clear()
        # Keep the camera zoom anchor across character/giant switches.
        # Giant mode uses a farther live cam_z, and the normal ref_cam_z anchor
        # is what shrinks projection back into the correct screen scale.
        for node in getattr(self.node_tracker, "_nodes", {}).values():
            node.active = False
            node.inactive_frames = PROJECTILE_DESPAWN_FRAMES
            node.dim_0 = 0.0
            node.dim_1 = 0.0
            node.dim_2 = 0.0
            node.actor_ptr = 0

    def on_resize(self, w: int, h: int) -> None:
        if w <= 0 or h <= 0:
            return
        self.w = w
        self.h = h
        self.overlay.on_resize(w, h)
        _surface_cache.clear()

    def _refresh_frame_data_cache(self) -> None:
        self.fd_by_slot = build_frame_data_cache()
        self._fd_scan_done = True

    def _frame_gate_for_slot(
        self,
        slot_name: str,
        slot_base: int,
        state_id: int,
        hitbox_flag: int,
    ) -> Tuple[bool, bool, Optional[int], Optional[MoveFrameData]]:
        """Return (draw_now, is_active, action_frame, frame_data).

        Frame-data lookup is also the source of the on-screen move label.  For
        a labelled action, keep its currently allocated attack geometry visible
        for the *entire lifetime of that state*: startup and recovery are dim;
        only the resolved active window gets the bright/pulsing treatment.
        The geometry disappears as soon as the labelled action/state ends.

        This deliberately does not use ``active_end`` as a visibility cutoff.
        In TvC, the live hitbox descriptors remain useful through recovery,
        even after their collision flag has turned off.  Restricting rendering
        to startup + active was what made them blink for only a frame.

        Unknown/unlabelled states retain the exact live-flag fallback so helper
        states, assists, and idle do not fabricate persistent hitboxes.
        """
        fd = lookup_frame_data(self.fd_by_slot, slot_name, state_id)
        action_frame = read_action_frame(slot_base)

        # A profiled action is the same condition the move-label layer uses.
        # Keep the box visible for as long as that action remains current,
        # regardless of whether action-counter data is temporarily unavailable.
        if fd is not None and fd.active_windows:
            is_active = is_frame_data_active(fd, action_frame)
            return True, is_active, action_frame, fd

        # No labelled action: retain the old exact live-box signal.
        is_active = (hitbox_flag == 0x53)
        return is_active, is_active, action_frame, fd

    def _move_phase_chip(
        self,
        slot_name: str,
        fd: Optional[MoveFrameData],
        action_frame: Optional[int],
        group_visible: bool,
    ) -> Tuple[Optional[str], bool]:
        """Return one stable move-level countdown for startup, active, and recovery.

        TvC's action counter is exposed as a zero-based frame after the local
        bias is applied.  Preserve frame zero.  Dropping it made an 8-frame
        startup begin at 7, and clamping active time to one made the last active
        number display twice.

        The active range shown by the frame-data table is treated as a boundary
        pair for this countdown: 8-17 displays 9..1.  A single-frame range such
        as 8-8 still receives one active frame.  Recovery then begins at the
        end boundary and uses the saved frame-data recovery value.
        """
        tick = int(self._move_phase_tick)
        if fd is None or not getattr(fd, "active_windows", None):
            self._move_phase_state.pop(slot_name, None)
            return None, False

        move_key = (int(fd.move_id), str(fd.move_name))
        state = self._move_phase_state.get(slot_name)
        if state is None or state.get("move_key") != move_key or tick - int(state.get("last_tick", tick)) > 2:
            state = {
                "move_key": move_key,
                "start_tick": tick,
                "last_tick": tick,
                "last_raw_frame": None,
                "effective_frame": 0,
            }
            self._move_phase_state[slot_name] = state

        # A missing circle for a draw or two must not restart the move clock.
        # There is nowhere to anchor the chip while the physical group is absent,
        # so keep the state but skip drawing until the group is visible again.
        if not group_visible:
            return None, False

        state["last_tick"] = tick
        raw_frame = int(action_frame) if action_frame is not None else None
        prev_raw = state.get("last_raw_frame")
        stored_effective = state.get("effective_frame")
        effective = int(stored_effective) if stored_effective is not None else 0

        if raw_frame is not None and raw_frame >= 0:
            if prev_raw is None:
                effective = raw_frame
            elif raw_frame > int(prev_raw):
                effective = raw_frame
            elif raw_frame == int(prev_raw):
                # The overlay may draw more often than the game's counter moves.
                effective = max(effective, raw_frame)
            elif raw_frame <= 1 and int(prev_raw) > 2:
                # A true reset means a new use of the same move.
                effective = raw_frame
                state["start_tick"] = tick
            else:
                # Brief substate/counter regressions retain the move-level clock.
                effective += 1
            state["last_raw_frame"] = raw_frame
        else:
            # Local fallback is zero-based too, matching read_action_frame().
            effective = max(0, tick - int(state.get("start_tick", tick)))

        state["effective_frame"] = effective

        windows = sorted((int(start), int(end)) for start, end in fd.active_windows)
        phase_windows: List[Tuple[int, int]] = []
        for start_frame, end_frame in windows:
            # Most table ranges are boundary pairs, so 8-17 is nine active
            # countdown ticks.  Preserve a true one-frame range as one tick.
            end_exclusive = end_frame if end_frame > start_frame else start_frame + 1
            phase_windows.append((start_frame, end_exclusive))

        first_start = min(start for start, _end in phase_windows)

        # The zero-based action clock makes this naturally display S..1.
        # Example: first active boundary 8 gives startup 8,7,...,1 at f=0..7.
        if effective < first_start:
            remaining = first_start - effective
            return (str(remaining), False) if remaining > 0 else (None, False)

        for start_frame, end_exclusive in phase_windows:
            if start_frame <= effective < end_exclusive:
                remaining = end_exclusive - effective
                return str(max(1, remaining)), True
            if effective < start_frame:
                # Multi-hit gap. Keep one move-level chip alive and count toward
                # the next active window instead of blinking off between hits.
                remaining = start_frame - effective
                return (str(remaining), False) if remaining > 0 else (None, False)

        final_boundary = max(end_exclusive for _start, end_exclusive in phase_windows)
        recovery_frames = max(0, int(getattr(fd, "recovery_frames", 0) or 0))

        # If a legacy preview lacks an explicit Recovery value, the MOT total is
        # still enough to supply a deterministic animation-tail countdown.
        if recovery_frames <= 0:
            total_frames = max(0, int(getattr(fd, "total_frames", 0) or 0))
            if total_frames > final_boundary:
                recovery_frames = total_frames - final_boundary

        if recovery_frames > 0 and effective >= final_boundary:
            recovery_elapsed = effective - final_boundary
            remaining = recovery_frames - recovery_elapsed
            if remaining > 0:
                return str(remaining), False

        return None, False

    def update(self, dt: float, control=None) -> None:
        hitboxes_on = self._hitboxes_enabled(control)
        hurtboxes_on = self._hurtboxes_enabled(control)
        ruler_on = _range_ruler_enabled()
        set_legend_window_visible(hitboxes_on or hurtboxes_on or ruler_on)
        if not (hitboxes_on or hurtboxes_on or ruler_on):
            self._clear_runtime_hitbox_state()
            return

        self._move_phase_tick += 1

        char_changed = False
        for name, base in SLOT_BASES.items():
            cid = rd32(base + OFF_CHAR_ID) or 0
            if self.last_char_ids.get(name) != cid:
                print(f"[CharChange] {name} char_id {self.last_char_ids.get(name)} -> {cid}")
                self.last_char_ids[name] = cid
                char_changed = True

        try:
            _camx, _camy, _camz, _camw = read_camera_pos(CAMERA)
            camera_mode_key = "giant" if float(_camz or 0.0) >= 10.0 else "normal"
        except Exception:
            camera_mode_key = self._last_camera_mode_key or "normal"

        if self._last_camera_mode_key != camera_mode_key:
            print(f"[CameraMode] {self._last_camera_mode_key} -> {camera_mode_key}")
            self._last_camera_mode_key = camera_mode_key
            char_changed = True

        if char_changed:
            self._clear_runtime_hitbox_state()

        if char_changed or not self._fd_scan_done:
            self._refresh_frame_data_cache()

        # Projectiles move fast; do not throttle this on pygame tick parity.
        # The old parity gate made projectile overlays appear to update at a
        # lower frame rate than the actual projectile.
        pools = resolve_projectile_pools() or PROJECTILE_POOLS
        node_idx = 0
        for pool in pools:
            for i in range(PROJECTILE_NODE_COUNT):
                node_addr = pool + i * PROJECTILE_NODE_STRIDE
                self.node_tracker.update_from_node(node_idx, node_addr)
                node_idx += 1

        slot_filter = _read_slot_filter()
        counts: Dict[str, int] = {}

        if hitboxes_on:
            for name, base in SLOT_BASES.items():
                try:
                    if not slot_filter.get(name, True):
                        counts[name] = 0
                        continue

                    raw_state = read_state_raw(base)
                    state_id = decode_state_id(raw_state)
                    if slot_passive_override(name, state_id):
                        continue

                    boxes = read_hitboxes(base, HITBOX)
                    active = 0
                    for i, (x, y, r, flag) in enumerate(boxes):
                        visible = self.motion_filter.update(name, i, x, y, r)
                        if visible and r > 0.001:
                            active += 1
                    counts[name] = active

                except Exception as slot_exc:
                    print(f"[SlotError] {name}: {slot_exc!r}")

            # Read projectile actors every overlay update so the drawn volume
            # tracks fast projectiles frame-for-frame.
            self.cached_projectiles = read_projectile_hitboxes()
        else:
            self.cached_projectiles = []
            self.motion_filter.cleanup()

        now_ms = pygame.time.get_ticks()

        # Cache skeletal hurtboxes in update(), not draw().  The same tiny
        # geometry pass also tells us whether a partner slot is actually on-stage
        # so stale standby hitboxes can be culled even when hurtbox display is off.
        if (hitboxes_on or hurtboxes_on or ruler_on) and now_ms - self._last_hurt_update_ms >= 16:
            hurt_filter = _read_hurtbox_filter()
            hurt_counts: Dict[str, int] = {}
            sample_bits = []
            cached: Dict[str, List[HurtboxState]] = {}
            renderable: Dict[str, bool] = {}
            for hurt_slot, hurt_base in SLOT_BASES.items():
                raw_hlist = read_hurtboxes(hurt_slot, hurt_base)
                live_rig = _slot_has_coherent_hurt_rig(hurt_base, raw_hlist)
                renderable[hurt_slot] = live_rig
                if not hurt_filter.get(hurt_slot, True) or not live_rig:
                    cached[hurt_slot] = []
                    hurt_counts[hurt_slot] = 0
                    sample_bits.append(f"{hurt_slot}[0]={'STANDBY' if raw_hlist and not live_rig else 'OFF'}")
                    continue
                # The ruler needs body geometry for touch/gap calculations,
                # but keeping that geometry cached does not make hurtboxes visible.
                cached[hurt_slot] = raw_hlist if (hurtboxes_on or ruler_on) else []
                hurt_counts[hurt_slot] = len(raw_hlist) if hurtboxes_on else 0
                # Saved body profiles are deliberately not updated from this
                # live hurtbox cache.  The cache is display/target geometry only.
                if raw_hlist:
                    h0 = raw_hlist[0]
                    sample_bits.append(f"{hurt_slot}[0]={h0.x:.2f},{h0.y:.2f} r={h0.radius:.2f}")
                else:
                    sample_bits.append(f"{hurt_slot}[0]=NONE")
            self.slot_renderable = renderable
            self.cached_hurtboxes = cached
            self.hurt_counts = hurt_counts
            globals()["_last_hurt_debug_text"] = " | ".join(sample_bits)
            self._last_hurt_update_ms = now_ms
        elif not hurtboxes_on:
            self.cached_hurtboxes = {}
            self.hurt_counts = {}
            self.cached_hit_contacts = []
            globals()["_last_hurt_debug_text"] = "hurt=off"

        self._flush_range_profiles()

        # Never scan the broad resolver/contact pool during normal overlay play.
        #
        # That old path scanned 0x91970000..0x91978000 at 4-byte stride every
        # 50 ms once a fighter entered hitstun.  It performs thousands of live
        # Dolphin reads exactly on hit, which is why the overlay looked frozen
        # until both players returned to idle.  Hitboxes and skeletal hurtboxes
        # remain live without this speculative contact marker; only the optional
        # yellow CONTACT marker/focus mode is disabled.
        self.cached_hit_contacts = []

        if pygame.time.get_ticks() % 300 == 0:
            self.motion_filter.cleanup()

        self.last_counts = counts

    def draw(self, screen: pygame.Surface, control=None) -> None:
        hitboxes_on = self._hitboxes_enabled(control)
        hurtboxes_on = self._hurtboxes_enabled(control)
        ruler_on = _range_ruler_enabled()
        if not (hitboxes_on or hurtboxes_on or ruler_on):
            return

        camx, camy, camz, camw = read_camera_pos(CAMERA)
        cam_cos, cam_sin, cam_translate_y, cam_depth_y, cam_depth_z, cam_depth_translate = read_camera_view_affine(CAMERA)

        ov = self.overlay
        ov.screen = screen
        ov.cam_x = camx
        ov.cam_y = camy
        ov.cam_z = camz
        ov.set_camera_view_affine(cam_cos, cam_sin, cam_translate_y, cam_depth_y, cam_depth_z, cam_depth_translate)
        ov.set_giant_x_anchor(_derive_giant_x_anchor() if abs(float(camz or 0.0)) >= GIANT_CAMERA_Z_THRESHOLD else None)
        ov.on_resize(screen.get_width(), screen.get_height())

        slot_filter = _read_slot_filter()
        hurt_filter = _read_hurtbox_filter()
        ruler_filter = _read_ruler_slot_filter()
        debug_labels = bool(getattr(control, "show_debug", False))
        hitbox_view_mode = _read_hitbox_view_mode()
        # The saved-profile ruler needs target/body geometry, not live attack
        # geometry.  It can run with the hitbox drawing layer disabled.
        # Ruler visibility is independent from the hurtbox display layer.
        # Hidden body geometry is still sampled above for touch/gap math.
        range_ruler_on = bool(ruler_on)
        range_ruler_axes = _range_ruler_axes_enabled()
        range_ruler_envelope_on = _range_envelope_enabled()
        range_ruler_locks = _range_ruler_locks_enabled()
        now_ms = pygame.time.get_ticks()
        hp_drops: Dict[str, int] = {}
        impact_events: Dict[str, int] = {}
        if range_ruler_on and RANGE_PROFILE_WRITE_ENABLED:
            for _hp_slot, _hp_base in SLOT_BASES.items():
                try:
                    _hp_now = int(rd32(_hp_base + 0x28) or 0)
                except Exception:
                    _hp_now = 0
                _hp_before = self._range_last_hp.get(_hp_slot)
                if isinstance(_hp_before, int) and 0 < _hp_now < _hp_before:
                    hp_drops[_hp_slot] = int(_hp_before - _hp_now)
                self._range_last_hp[_hp_slot] = _hp_now
                try:
                    _impact_now = int(rd32(_hp_base + RUNTIME_IMPACT_FREEZE_OFF) or 0)
                except Exception:
                    _impact_now = 0
                # Treat only small positive countdowns as a contact edge; stale
                # sentinels/unknown counters cannot manufacture audit events.
                if not (0 < _impact_now < 300):
                    _impact_now = 0
                _impact_before = int(self._range_last_impact_freeze.get(_hp_slot) or 0)
                if _impact_now > 0 and _impact_before <= 0:
                    impact_events[_hp_slot] = _impact_now
                self._range_last_impact_freeze[_hp_slot] = _impact_now
        else:
            self._range_calibration_attempts.clear()
            self._range_last_hp.clear()
            self._range_move_origins.clear()
            self._range_pending_learns.clear()
            self._range_special_sessions.clear()
            self._range_posture_locks.clear()
            self._range_contact_snapshots.clear()
            self._range_contact_markers.clear()
            self._range_contact_expectations.clear()
            self._range_last_impact_freeze.clear()
        # Existing moves resolve directly from their saved profile. Missing
        # normals learn from their validated active window. Body specials use a
        # short sticky family session so multi-state attacks can learn their
        # complete effective sweep without a full memory scan.
        ruler_attack_shapes: List[Tuple[str, float, float, float, float]] = []
        profile_trigger_meta: Dict[str, Dict[str, Any]] = {}
        # Action posture context is separate from raw live state.  It lets the
        # draw pass hold 2A/2B/2C in the crouch bucket while the engine briefly
        # reports a generic standing/recovery state mid-animation.
        action_posture_context: Dict[str, Dict[str, Any]] = {}
        learning_slots: set[str] = set()
        current_ghost_frame: List[Tuple[float, float, float, float, Tuple[int, int, int]]] = []

        if range_ruler_on:
            # A profile is armed only by the fighter actually performing that
            # action. Each action is filed under its standing/crouching/air
            # posture. Missing actions sample only themselves.
            for name, base in SLOT_BASES.items():
                if not ruler_filter.get(name, True):
                    continue
                if not self.slot_renderable.get(name, False):
                    continue
                try:
                    state_id = decode_state_id(read_state_raw(base))
                    char_id = int(rd32(base + OFF_CHAR_ID) or 0)
                    action_id = int(rd32(base + ATT_ID_OFF_PRIMARY) or 0) & 0x7FFF
                    root = tuple(float(v) for v in read_fighter_root(base))
                    if not all(math.isfinite(v) for v in root):
                        continue
                    self._observe_range_ground_reference(name, int(state_id), root)
                    if slot_passive_override(name, state_id):
                        continue

                    # +0x1E8 is the authoritative player action ID and is what
                    # distinguishes strength variants such as Tatsu L/M/H, Legs
                    # A/B/C, Denko, Battering Ram, and other specials. The old
                    # state-ID path collapses many of those into generic phases.
                    range_action_id = int(action_id) if int(action_id) > 0 else int(state_id)
                    fd = (
                        lookup_frame_data(self.fd_by_slot, name, range_action_id)
                        or lookup_frame_data(self.fd_by_slot, name, state_id)
                    )
                    descriptor = range_action_descriptor(fd, char_id, range_action_id)
                    if descriptor is None:
                        continue
                    airborne_now = self._range_slot_is_airborne(name, int(state_id), root)
                    descriptor = dict(descriptor)
                    descriptor["runtime_airborne"] = bool(airborne_now)
                    descriptor["runtime_state_id"] = int(state_id)
                    descriptor["runtime_action_id"] = int(range_action_id)
                    descriptor["display_mode"] = "ground_ruler"
                    descriptor["posture"] = self._range_action_posture(
                        name, int(state_id), root, descriptor.get("move_name"), airborne_now,
                    )
                    if str(descriptor.get("kind") or "").endswith("special"):
                        descriptor = self._range_special_session_descriptor(
                            name, char_id, int(state_id), descriptor, root, now_ms,
                        )
                    else:
                        self._range_special_sessions.pop(name, None)
                    move_key = int(descriptor["move_id"])
                    profile_key = f"{char_id}:{move_key}"
                    action_frame = read_action_frame(base)
                    # Only bona-fide attack/special descriptors can establish
                    # an action posture.  Generic Forward/idle/recovery map
                    # labels must not overwrite a crouching action lock.
                    if _range_descriptor_is_combat_action(descriptor, fd, int(state_id)):
                        action_posture_context[name] = {
                            "posture": str(descriptor.get("posture") or ("air" if airborne_now else "stand")),
                            "profile_key": profile_key,
                            "state_id": int(state_id),
                            "action_frame": action_frame if isinstance(action_frame, int) else None,
                        }
                    if bool(descriptor.get("special_session")) and isinstance(descriptor.get("session_start_root"), (tuple, list)):
                        origin = tuple(float(v) for v in descriptor.get("session_start_root")[:3])
                    else:
                        origin = self._range_move_origin(
                            name, char_id, int(state_id), move_key, action_frame, root,
                        )

                    # Never leave a prior move's guide visible while a new
                    # action is being profiled.  Ground/air is determined from
                    # this fighter's live root state, not the move label.
                    prior = self._saved_ranges.get(name)
                    if isinstance(prior, dict) and str(prior.get("profile_key") or "") != profile_key:
                        self._saved_ranges.pop(name, None)
                        self._range_calibration_attempts.pop(name, None)

                    profile = (self._range_profiles.get("attacks") or {}).get(profile_key)
                    core_exists = isinstance(profile, dict) and self._range_profile_core_geometry(profile) is not None
                    if core_exists:
                        profile_trigger_meta[name] = {
                            "profile_key": profile_key,
                            "char_id": char_id,
                            "move_key": move_key,
                            "move_name": str(descriptor["move_name"] or profile.get("move_name") or f"0x{move_key:04X}"),
                            "state_id": int(state_id),
                            "display_mode": "ground_ruler",
                            "airborne_action": bool(airborne_now),
                            "move_kind": str(profile.get("move_kind") or descriptor.get("kind") or "ground_special"),
                            "posture": str(descriptor.get("posture") or ("air" if airborne_now else "stand")),
                            "action_origin": origin,
                        }
                        # Known moves stay fast. One compact active-window
                        # capture is allowed only while a Horz/Vert envelope is
                        # genuinely missing and no retained samples can derive it.
                        needs_envelope = bool(
                            self._range_profile_horizontal_envelope(profile) is None
                            or self._range_profile_vertical_envelope(profile) is None
                        )
                        is_live_body_action = str(descriptor.get("kind") or "").endswith("special")
                        needs_samples = bool(self._range_dynamic_sweep(profile) is None and needs_envelope)
                        # Specials and unknown attacks are always sampled from
                        # their real live hitboxes. This keeps existing profiles
                        # honest too, so a previously partial Legs/Denko/Ram
                        # capture can be refreshed by the next complete use.
                        if needs_samples or is_live_body_action:
                            self._queue_missing_range_profile(
                                name, char_id, int(state_id), fd, root, action_frame, now_ms,
                                descriptor, capture_samples_for_existing=is_live_body_action or needs_samples,
                            )
                            learning_slots.add(name)
                    else:
                        # Unknown normals, air normals, and mapped specials all
                        # learn once. Frame-data rows use validated active
                        # frames; mapped specials require live 0x53 body boxes.
                        self._queue_missing_range_profile(
                            name, char_id, int(state_id), fd, root, action_frame, now_ms, descriptor,
                        )
                        learning_slots.add(name)

                    # Specials must be visible while they are being learned, not
                    # only on the next attempt. Build a transient profile from
                    # the active samples collected so far and bind it to the same
                    # normal ruler draw path. Nothing partial is persisted here.
                    if str(descriptor.get("kind") or "").endswith("special"):
                        pending_preview = self._pending_range_preview_profile(self._range_pending_learns.get(name))
                        if isinstance(pending_preview, dict) and self._range_profile_geometry(pending_preview) is not None:
                            profile_trigger_meta[name] = {
                                "profile_key": profile_key,
                                "char_id": char_id,
                                "move_key": move_key,
                                "move_name": str(descriptor.get("move_name") or pending_preview.get("move_name") or f"0x{move_key:04X}"),
                                "state_id": int(state_id),
                                "display_mode": "ground_ruler",
                                "airborne_action": bool(airborne_now),
                                "move_kind": str(descriptor.get("kind") or pending_preview.get("move_kind") or "ground_special"),
                                "posture": str(descriptor.get("posture") or ("air" if airborne_now else "stand")),
                                "action_origin": origin,
                                "live_profile": pending_preview,
                            }
                except Exception:
                    continue

        if hitbox_view_mode == "ghost":
            for age, ghost_frame in enumerate(reversed(self._hitbox_ghost_frames), start=1):
                alpha = max(24, 142 - age * 32)
                for gx, gy, gz, gr, gcolor in ghost_frame:
                    ov.draw_hitbox_ghost(gx, gy, gz, gr, gcolor, alpha)

        contacts_by_target: Dict[str, List[HitContactState]] = {}
        focus_mode = False
        focus_sources = set()
        focus_targets = set()
        focus_points = []
        if self.cached_hit_contacts:
            focus_mode = True
            for contact in self.cached_hit_contacts:
                if hurt_filter.get(contact.target_slot, True):
                    contacts_by_target.setdefault(contact.target_slot, []).append(contact)
                    focus_sources.add(contact.source_slot)
                    focus_targets.add(contact.target_slot)
                    focus_points.append((contact.x, contact.y, contact.z, contact))

        # In collision focus mode, collapse the scene to only the relevant data.
        # This reduces both clutter and draw cost exactly when impact frames get dense.
        HURT_FOCUS_MARGIN = 1.05
        HIT_FOCUS_PAD = 2.4
        PROJ_FOCUS_PAD = 2.8

        slot_invuln: Dict[str, bool] = {}
        slot_invuln_text: Dict[str, str] = {}
        slot_counter_mask: Dict[str, int] = {}
        slot_counter_text: Dict[str, str] = {}
        slot_guard_mask: Dict[str, int] = {}
        slot_guard_text: Dict[str, str] = {}
        slot_armor_mask: Dict[str, int] = {}
        slot_armor_text: Dict[str, str] = {}
        for _slot_name, _slot_base in SLOT_BASES.items():
            try:
                _raw_state = read_state_raw(_slot_base)
                _state_id = decode_state_id(_raw_state)
                _char_id = int(rd32(_slot_base + OFF_CHAR_ID) or 0)
                _fd = lookup_frame_data(self.fd_by_slot, _slot_name, _state_id)
                _action_frame = read_action_frame(_slot_base)
                # Prefer the direct current-action signature.  It avoids any
                # cache/listing mismatch for special wrappers and guarantees the
                # visual changes on the exact live move that owns the timer.
                _direct_remaining, _state_flags, _protection_flags = _read_live_protection_state(_slot_base)
                _live_frames = 0 if _direct_remaining > 0 else _read_live_invuln_frames(_slot_base)
                _fd_frames = int(_fd.invuln_frames or 0) if _fd is not None else 0
                _invuln_frames = _live_frames or _fd_frames
                # A nonzero direct timer is authoritative for the current frame.
                # Static/profile timing is retained only as a fallback when the
                # direct field cannot be observed.
                slot_invuln[_slot_name] = bool(
                    _direct_remaining > 0
                    or (_invuln_frames and _action_frame is not None and 1 <= int(_action_frame) <= _invuln_frames)
                )
                slot_invuln_text[_slot_name] = (
                    f"{_direct_remaining}f left" if _direct_remaining > 0
                    else (f"{_invuln_frames}f" if slot_invuln[_slot_name] else "")
                )

                _action_key = (_char_id, int(_state_id))
                _resolver_enabled = bool(_state_flags & GUARD_ENABLE_BIT)
                _height_mask = int(_protection_flags) & GUARD_HEIGHT_MASK
                _stun_bypass = bool(int(_protection_flags) & STUN_BYPASS_BIT)
                _no_damage = bool(int(_state_flags) & NO_DAMAGE_GUARD_BIT)

                _counter_active = bool(
                    _resolver_enabled and _height_mask and _action_key in CONFIRMED_COUNTER_ACTIONS
                )
                _guard_active = bool(
                    _resolver_enabled
                    and _stun_bypass
                    and not _counter_active
                    and (_no_damage or _action_key in CONFIRMED_GUARD_ACTIONS)
                )
                _armor_active = bool(
                    _resolver_enabled
                    and _stun_bypass
                    and not _counter_active
                    and not _guard_active
                    and (_action_key in CONFIRMED_ARMOR_ACTIONS or not _no_damage)
                )

                slot_counter_mask[_slot_name] = int(_height_mask) if _counter_active else 0
                slot_counter_text[_slot_name] = _guard_mask_short(_height_mask) if _counter_active else ""
                slot_guard_mask[_slot_name] = GENERIC_PROTECTION_MARKER if _guard_active else 0
                slot_guard_text[_slot_name] = "NO DMG" if _guard_active else ""
                slot_armor_mask[_slot_name] = GENERIC_PROTECTION_MARKER if _armor_active else 0
                slot_armor_text[_slot_name] = "TAKES DMG" if _armor_active else ""
            except Exception:
                slot_invuln[_slot_name] = False
                slot_invuln_text[_slot_name] = ""
                slot_counter_mask[_slot_name] = 0
                slot_counter_text[_slot_name] = ""
                slot_guard_mask[_slot_name] = 0
                slot_guard_text[_slot_name] = ""
                slot_armor_mask[_slot_name] = 0
                slot_armor_text[_slot_name] = ""

        if hurtboxes_on:
            hurt_view_mode = _read_hurtbox_view_mode(control)
            for hurt_slot, hlist in self.cached_hurtboxes.items():
                if not hurt_filter.get(hurt_slot, True):
                    continue
                if not self.slot_renderable.get(hurt_slot, False):
                    continue
                if not _slot_root_is_on_screen(ov, SLOT_BASES[hurt_slot]):
                    continue

                if focus_mode and hurt_slot not in focus_targets:
                    continue

                contacts = contacts_by_target.get(hurt_slot, [])
                invuln_active = bool(slot_invuln.get(hurt_slot, False))
                counter_mask_active = int(slot_counter_mask.get(hurt_slot, 0) or 0)
                guard_mask_active = int(slot_guard_mask.get(hurt_slot, 0) or 0)
                armor_mask_active = int(slot_armor_mask.get(hurt_slot, 0) or 0)
                layout = classify_hurtbox_regions(hlist)
                region_by_index = layout.get("region_by_index", {})
                major_indices = set(layout.get("major_indices", set()))
                label_by_index = dict(layout.get("label_by_index", {}))

                extra_indices = set()
                for hurt in hlist:
                    if any(hurtbox_contains_contact(hurt, contact) for contact in contacts):
                        extra_indices.add(hurt.index)
                if focus_mode:
                    for hurt in hlist:
                        for fx, fy, fz, _c in focus_points:
                            if math.hypot(hurt.x - fx, hurt.y - fy) <= (hurt.radius + HURT_FOCUS_MARGIN):
                                extra_indices.add(hurt.index)
                                break

                visible_indices = set(h.index for h in hlist)
                if hurt_view_mode == "clean":
                    visible_indices = set(major_indices) | set(extra_indices)

                first_label_drawn = False
                for hurt in hlist:
                    if hurt.index not in visible_indices:
                        continue
                    highlight = any(hurtbox_contains_contact(hurt, contact) for contact in contacts)
                    if focus_mode and not (highlight or (hurt.index in extra_indices)):
                        continue

                    region = region_by_index.get(hurt.index, "other")
                    base_color = HURT_REGION_COLORS.get(region, HURT_REGION_COLORS["other"])

                    if hurt_view_mode == "clean":
                        detail = True
                        show_label = (hurt.index in label_by_index)
                        label = label_by_index.get(hurt.index, hurt.label())
                        color = base_color
                    elif hurt_view_mode == "detailed":
                        detail = highlight or (hurt.index in major_indices) or (hurt.index in extra_indices)
                        show_label = (hurt.index in label_by_index and detail)
                        label = label_by_index.get(hurt.index, hurt.label())
                        color = base_color if detail else _soften_hurt_color(base_color, 0.52, 34)
                    else:
                        detail = True
                        show_label = True
                        short_region = region.split("_")[0].upper()
                        label = f"{short_region}[{hurt.index}]"
                        color = base_color

                    ov.draw_hurtbox(
                        hurt.x,
                        hurt.y,
                        hurt.z,
                        hurt.radius,
                        color,
                        label,
                        highlight=highlight,
                        detail=detail,
                        invuln=invuln_active and not highlight,
                        show_label=show_label,
                    )
                    if (invuln_active or counter_mask_active or guard_mask_active or armor_mask_active) and not first_label_drawn and ov.font_hud is not None:
                        try:
                            _sx, _sy, _d, _f = ov.world_to_screen(hurt.x, hurt.y, hurt.z)
                            if invuln_active:
                                _label = f"INVUL {slot_invuln_text.get(hurt_slot, '')}"
                                _color = (105, 255, 145)
                            elif counter_mask_active:
                                _label = f"COUNTER {slot_counter_text.get(hurt_slot, '')}"
                                _color = (255, 145, 135)
                            elif armor_mask_active:
                                _label = f"ARMOR {slot_armor_text.get(hurt_slot, '')}"
                                _color = (205, 155, 255)
                            else:
                                _label = f"GUARD POINT {slot_guard_text.get(hurt_slot, '')}"
                                _color = (255, 205, 105)
                            _txt = ov.font_hud.render(_label, True, _color)
                            ov.screen.blit(_txt, (_sx + 12, _sy - 24))
                            first_label_drawn = True
                        except Exception:
                            pass

            for contact in self.cached_hit_contacts:
                if hurt_filter.get(contact.target_slot, True):
                    ov.draw_hit_contact(contact)

        if hitboxes_on:
            # Primary physical lane: preserve the old standalone renderer's
            # behavior exactly.  This lane is intentionally independent from
            # projectile/effect discovery, hurt-rig coherence, on-screen root
            # tests, and contact-focus filtering.
            for name, base in SLOT_BASES.items():
                if not slot_filter.get(name, True):
                    continue

                raw_state = read_state_raw(base)
                state_id = decode_state_id(raw_state)
                if slot_passive_override(name, state_id):
                    continue

                # Anchor range profiling to the fighter root, not the live
                # hurtbox-envelope center.  Crouch/pose changes move that AABB
                # even when the model has not travelled.
                try:
                    _action_root = tuple(float(v) for v in read_fighter_root(base))
                    _action_char_id = int(rd32(base + OFF_CHAR_ID) or 0)
                    # Locked profile mode does not capture move starts or live
                    # geometry.  Existing profile data alone defines the ruler.
                    _action_origin = _action_root
                except Exception:
                    _action_root = tuple(float(v) for v in read_fighter_root(base))
                    _action_char_id = int(rd32(base + OFF_CHAR_ID) or 0)
                    _action_origin = _action_root

                boxes = read_hitboxes(base, HITBOX)
                palette = COLORS.get(name, [(255, 255, 255)])
                fd = lookup_frame_data(self.fd_by_slot, name, state_id)
                action_frame = read_action_frame(base)
                drawable_boxes: List[Tuple[int, float, float, float, int]] = []

                for i, (x, y, r, flag) in enumerate(boxes):
                    if r <= 0.001:
                        continue
                    visible = self.motion_filter.update(name, i, x, y, r)
                    if not visible and fd is None:
                        continue
                    drawable_boxes.append((i, x, y, r, flag))

                chip_text, chip_active = self._move_phase_chip(
                    name,
                    fd,
                    action_frame,
                    bool(drawable_boxes),
                )
                group_bounds: Optional[List[int]] = None

                for i, x, y, r, flag in drawable_boxes:
                    base_color = palette[i % len(palette)]
                    # All radii in one move share one move-level phase.
                    is_active = bool(chip_active) if fd is not None else (flag == 0x53)
                    draw_color = base_color if is_active else _dim_hitbox_color(base_color)
                    label = f"{name}[{i}]"
                    if debug_labels and fd is not None and action_frame is not None:
                        label = f"{name}[{i}] f={action_frame}/{fd.active_text()}"
                    current_ghost_frame.append((float(x), float(y), 0.0, float(r), tuple(draw_color[:3])))
                    projected = ov._project_hitbox(x, y, 0, r)
                    if projected is not None:
                        psx, psy, pr = projected
                        if group_bounds is None:
                            group_bounds = [psx - pr, psy - pr, psx + pr, psy + pr]
                        else:
                            group_bounds[0] = min(group_bounds[0], psx - pr)
                            group_bounds[1] = min(group_bounds[1], psy - pr)
                            group_bounds[2] = max(group_bounds[2], psx + pr)
                            group_bounds[3] = max(group_bounds[3], psy + pr)
                    if hitbox_view_mode != "ghost":
                        ov.draw_hitbox(
                            x, y, 0, r, draw_color, label,
                            is_active=is_active,
                            invuln=bool(slot_invuln.get(name, False)),
                            show_label=debug_labels,
                        )

                if hitbox_view_mode != "ghost" and group_bounds is not None and chip_text:
                    ov.draw_move_phase_chip(
                        tuple(group_bounds),
                        chip_text,
                        palette[0],
                        chip_active,
                    )

            # Secondary lanes: original moving projectiles and additive
            # linked explosion/super collision volumes.  These never replace or
            # gate the primary physical lane above.
            for proj in self.cached_projectiles:
                if not slot_filter.get(proj.owner_slot, True):
                    continue
                if focus_mode and proj.owner_slot not in focus_sources:
                    continue

                if focus_mode:
                    near_focus = False
                    for fx, fy, fz, _c in focus_points:
                        if math.hypot(proj.x - fx, (proj.y + PROJECTILE_Y_OFFSET) - fy) <= (proj.radius + PROJ_FOCUS_PAD):
                            near_focus = True
                            break
                    if not near_focus and not proj.contact_valid:
                        continue

                color = PROJ_COLORS.get(proj.owner_slot, COL_PROJ)
                if range_ruler_on:
                    ruler_attack_shapes.append((proj.owner_slot, float(proj.x), float(proj.y + PROJECTILE_Y_OFFSET), float(proj.z), float(proj.radius)))
                current_ghost_frame.append((float(proj.x), float(proj.y + PROJECTILE_Y_OFFSET), float(proj.z), float(proj.radius), tuple(color[:3])))
                if hitbox_view_mode != "ghost":
                    ov.draw_projectile_hitbox(
                        proj.x,
                        proj.y + PROJECTILE_Y_OFFSET,
                        proj.z,
                        proj.radius,
                        color,
                        proj.label(debug_labels and not focus_mode),
                        sweep_x=(proj.hit_start_x if proj.extent > 0.0001 else None),
                        sweep_y=((proj.hit_start_y + PROJECTILE_Y_OFFSET) if proj.extent > 0.0001 else None),
                        sweep_z=(proj.hit_start_z if proj.extent > 0.0001 else None),
                        contact_x=proj.impact_x,
                        contact_y=proj.impact_y + PROJECTILE_Y_OFFSET,
                        contact_z=proj.impact_z,
                        contact_valid=proj.contact_valid,
                        root_x=proj.root_x,
                        root_y=proj.root_y + PROJECTILE_Y_OFFSET,
                        root_z=proj.root_z,
                        show_label=debug_labels,
                    )

        if range_ruler_on:
            # A collector commits at the first recovery frame (or state exit),
            # so a newly learned normal immediately becomes a normal saved
            # ruler without waiting for a second full-screen scan.
            self._finalize_missing_range_profiles(learning_slots, now_ms)

        if range_ruler_on and profile_trigger_meta:
            # Bind known profiles without scanning their live attack geometry.
            # ``action_origin`` is retained only for the short live Dynamic
            # replay; the persistent ground ruler still anchors to the current
            # standing root after the action ends.
            for source_slot, meta in profile_trigger_meta.items():
                if not isinstance(meta, dict):
                    continue
                try:
                    _char_id = int(meta.get("char_id") or 0)
                    move_key = int(meta.get("move_key") or 0)
                    move_name = str(meta.get("move_name") or "RANGE")
                    action_state_id = int(meta.get("state_id") or 0)
                    airborne_action = bool(meta.get("airborne_action"))
                    display_mode = "ground_ruler"
                    move_kind = str(meta.get("move_kind") or "ground_special")
                    action_posture = str(meta.get("posture") or ("air" if airborne_action else "stand"))
                except Exception:
                    continue
                source_team = self._team_for_slot(source_slot)
                if not source_team:
                    continue
                try:
                    source_center = tuple(float(v) for v in read_fighter_root(SLOT_BASES[source_slot]))
                    if not all(math.isfinite(v) for v in source_center):
                        continue
                except Exception:
                    continue
                profile_key = f"{_char_id}:{move_key}"
                live_profile = meta.get("live_profile") if isinstance(meta, dict) else None
                profile = live_profile if isinstance(live_profile, dict) else (self._range_profiles.get("attacks") or {}).get(profile_key)
                if not isinstance(profile, dict) or self._range_profile_geometry(profile) is None:
                    continue

                nearest = None
                for target_slot, target_base in SLOT_BASES.items():
                    if self._team_for_slot(target_slot) == source_team:
                        continue
                    if not self.slot_renderable.get(target_slot, False):
                        continue
                    try:
                        tx, _ty, _tz = read_fighter_root(target_base)
                        if not math.isfinite(float(tx)):
                            continue
                        dist = abs(float(tx) - float(source_center[0]))
                        if nearest is None or dist < nearest[0]:
                            nearest = (dist, float(tx))
                    except Exception:
                        continue
                previous = self._saved_ranges.get(source_slot) or {}
                if nearest is None:
                    direction = 1.0 if float(previous.get("last_direction") or 1.0) >= 0.0 else -1.0
                else:
                    direction = 1.0 if nearest[1] >= float(source_center[0]) else -1.0
                raw_origin = meta.get("action_origin")
                if isinstance(raw_origin, (tuple, list)) and len(raw_origin) >= 3:
                    try:
                        action_origin = tuple(float(raw_origin[i]) for i in range(3))
                    except Exception:
                        action_origin = source_center
                else:
                    action_origin = source_center
                saved_entry = {
                    "profile_key": profile_key,
                    "move_key": move_key,
                    "move_name": move_name,
                    "last_direction": float(direction),
                    "captured_state_id": action_state_id,
                    "char_id": _char_id,
                    "display_mode": display_mode,
                    "airborne_action": bool(airborne_action),
                    "posture": action_posture,
                    "move_kind": move_kind,
                    "action_origin": action_origin,
                    "dynamic_live_until_ms": int(now_ms) + RANGE_DYNAMIC_LINGER_MS,
                    "profile_locked": True,
                    "default_profile": False,
                    "live_profile": profile if isinstance(live_profile, dict) else None,
                }
                self._remember_range_for_posture(source_slot, saved_entry, action_posture)

        if range_ruler_on and profile_trigger_meta:
            # Capture only current active body boxes for actions that already
            # have a profile.  This is the narrow evidence window used when a
            # later HP/impact signal says the saved ruler guessed wrong.
            for _source_slot, _meta in profile_trigger_meta.items():
                self._capture_range_active_snapshot(_source_slot, _meta, now_ms)
            self._process_range_contact_audit(now_ms, hp_drops, impact_events)
            self._refresh_range_contact_expectations(now_ms)
        else:
            self._range_contact_expectations.clear()

        if range_ruler_on and RANGE_PROFILE_WRITE_ENABLED:
            self._tick_range_calibration(now_ms, set(profile_trigger_meta.keys()), hp_drops)

        if hitboxes_on:
            if current_ghost_frame:
                self._hitbox_ghost_frames.append(current_ghost_frame)
                self._hitbox_ghost_frames = self._hitbox_ghost_frames[-4:]
            elif hitbox_view_mode == "ghost":
                self._hitbox_ghost_frames = self._hitbox_ghost_frames[-3:]

        if False and range_ruler_on:
            # Retained center-to-tip profile below is the ruler display.
            # Keep this former live-circle guide disabled to avoid duplicate guides.
            # Raw slots are contiguous by team: P1/P2 versus P3/P4.
            team_for_slot = {"P1": "P1", "P2": "P1", "P3": "P2", "P4": "P2"}
            best_by_team: Dict[str, Tuple[float, float, str, HurtboxState, Tuple[str, float, float, float, float]]] = {}
            for attack in ruler_attack_shapes:
                source_slot, ax, ay, az, ar = attack
                source_team = team_for_slot.get(source_slot)
                if source_team is None:
                    continue
                for target_slot, hlist in self.cached_hurtboxes.items():
                    if team_for_slot.get(target_slot) == source_team:
                        continue
                    if not self.slot_renderable.get(target_slot, False):
                        continue
                    for hurt in hlist:
                        # Prefer the body lane nearest to the attack. A wider
                        # threshold keeps grounded jabs and short normals visible.
                        vertical_gap = abs(float(ay) - float(hurt.y))
                        if vertical_gap > float(ar) + float(hurt.radius) + 2.75:
                            continue
                        gap = max(0.0, abs(float(ax) - float(hurt.x)) - float(ar) - float(hurt.radius))
                        score = gap + vertical_gap * 0.16
                        current = best_by_team.get(source_team)
                        if current is None or score < current[0]:
                            best_by_team[source_team] = (score, gap, target_slot, hurt, attack)

            now_ms = pygame.time.get_ticks()
            for source_team, (_score, gap, target_slot, hurt, attack) in best_by_team.items():
                self._last_range_rulers[source_team] = (now_ms + 700, gap, target_slot, hurt, attack)

            # Draw fresh results first; if a fast normal has already ended,
            # retain the last valid guide long enough to read it.
            for source_team in ("P1", "P2"):
                entry = self._last_range_rulers.get(source_team)
                if entry is None:
                    continue
                expire_ms, gap, target_slot, hurt, attack = entry
                if now_ms > int(expire_ms):
                    self._last_range_rulers.pop(source_team, None)
                    continue
                source_slot, ax, ay, az, ar = attack
                ov.draw_range_ruler(
                    source_slot, target_slot,
                    ax, ay, az, ar,
                    float(hurt.x), float(hurt.y), float(hurt.z), float(hurt.radius),
                    gap,
                )

        if range_ruler_on and (self._saved_ranges or self._saved_ranges_by_posture):
            # Each live posture owns its own last action. Standing never leaks a
            # crouching ruler, and a jump never inherits a floor ruler.
            for source_slot in SLOT_BASES:
                if not ruler_filter.get(source_slot, True) or not self.slot_renderable.get(source_slot, False):
                    continue
                try:
                    display_center = tuple(float(v) for v in read_fighter_root(SLOT_BASES[source_slot]))
                    if not all(math.isfinite(v) for v in display_center):
                        continue
                    try:
                        live_state_id = decode_state_id(read_state_raw(SLOT_BASES[source_slot]))
                    except Exception:
                        live_state_id = 0
                    self._observe_range_ground_reference(source_slot, int(live_state_id), display_center)
                    _action_ctx = action_posture_context.get(source_slot) or {}
                    live_posture = self._range_presentation_posture(
                        source_slot,
                        int(live_state_id),
                        display_center,
                        int(now_ms),
                        _action_ctx.get("posture"),
                        _action_ctx.get("profile_key"),
                        _action_ctx.get("action_frame"),
                    )
                    locked_profile_key = str(range_ruler_locks.get(source_slot) or "").strip()
                    locked_profile = None
                    locked_posture = None
                    if locked_profile_key:
                        _candidate = (self._range_profiles.get("attacks") or {}).get(locked_profile_key)
                        try:
                            _live_char_id = int(rd32(SLOT_BASES[source_slot] + OFF_CHAR_ID) or 0)
                        except Exception:
                            _live_char_id = 0
                        try:
                            _profile_char_id = int((_candidate or {}).get("char_id") or locked_profile_key.split(":", 1)[0])
                        except Exception:
                            _profile_char_id = -1
                        if (
                            isinstance(_candidate, dict)
                            and _live_char_id > 0
                            and _profile_char_id == _live_char_id
                            and self._range_profile_geometry(_candidate) is not None
                        ):
                            locked_profile = _candidate
                            _locked_name = str(_candidate.get("move_name") or locked_profile_key)
                            _locked_kind = str(_candidate.get("move_kind") or "").lower()
                            _locked_name_lower = _locked_name.lower()
                            locked_posture = "air" if (
                                "air" in _locked_kind
                                or _locked_name_lower.startswith("j.")
                                or _locked_name_lower.startswith("air ")
                            ) else "stand"
                            try:
                                _locked_move_key = int(_candidate.get("move_id") or locked_profile_key.split(":", 1)[1])
                            except Exception:
                                _locked_move_key = 0
                            saved = {
                                "profile_key": locked_profile_key,
                                "move_key": _locked_move_key,
                                "move_name": _locked_name,
                                "char_id": _profile_char_id,
                                "last_direction": float((self._saved_ranges.get(source_slot) or {}).get("last_direction") or 1.0),
                                "display_mode": str(_candidate.get("display_mode") or "ground_ruler"),
                                "airborne_action": locked_posture == "air",
                                "posture": locked_posture,
                                "move_kind": str(_candidate.get("move_kind") or "locked"),
                                "profile_locked": True,
                                "default_profile": False,
                            }
                        else:
                            saved = (self._saved_ranges_by_posture.get(source_slot) or {}).get(live_posture)
                    else:
                        saved = (self._saved_ranges_by_posture.get(source_slot) or {}).get(live_posture)
                    if not isinstance(saved, dict):
                        continue
                    source_team = self._team_for_slot(source_slot)
                    if not source_team:
                        continue
                    profile_key = str(saved.get("profile_key") or "")
                    live_profile = saved.get("live_profile")
                    profile = locked_profile if isinstance(locked_profile, dict) else (live_profile if isinstance(live_profile, dict) else (self._range_profiles.get("attacks") or {}).get(profile_key))
                    if not isinstance(profile, dict):
                        continue
                    try:
                        live_until = int(saved.get("dynamic_live_until_ms") or 0)
                    except Exception:
                        live_until = 0
                    use_air_ruler = (locked_posture == "air") if locked_posture is not None else (live_posture == "air")
                    candidates = self._range_target_candidates(source_slot, display_center)
                    if candidates:
                        _dist, nearest_slot, nearest_root, _nearest_local, _nearest_source = min(candidates, key=lambda item: item[0])
                        direction = 1.0 if float(nearest_root[0]) >= float(display_center[0]) else -1.0
                    else:
                        nearest_slot = ""
                        direction = 1.0 if float(saved.get("last_direction") or 1.0) >= 0.0 else -1.0

                    horizontal_axis_on = bool(range_ruler_axes.get("horizontal", False))
                    vertical_axis_on = bool(range_ruler_axes.get("vertical", False))
                    coverage_slices = None
                    coverage_frames = None
                    coverage_temporal_layers = None
                    if horizontal_axis_on and vertical_axis_on and range_ruler_envelope_on:
                        coverage_frames = self._range_dynamic_sweep(profile)
                        coverage_slices = self._coverage_slices_from_dynamic_frames(coverage_frames)
                        coverage_temporal_layers = self._coverage_temporal_layers_from_dynamic_frames(coverage_frames)

                    if vertical_axis_on:
                        vertical = self._range_profile_vertical_geometry(profile)
                        if vertical is not None:
                            ov.draw_saved_vertical_range_zone(
                                source_slot,
                                display_center[0], display_center[1], display_center[2],
                                direction,
                                float(vertical["lower_y_from_start"]),
                                float(vertical["upper_y_from_start"]),
                                float(vertical.get("upper_forward_from_start") or 0.0),
                                float(vertical.get("lower_forward_from_start") or 0.0),
                                str(saved.get("move_name") or "RANGE"),
                                source_label=self._ruler_source_label(source_slot, saved),
                                airborne=bool(use_air_ruler),
                                posture=live_posture,
                            )

                    if not horizontal_axis_on:
                        continue

                    geometry = self._range_profile_geometry(profile)
                    if geometry is None:
                        continue
                    adjustment, calibration_pips = self._range_adjustment(profile)
                    far = max(0.0, geometry["reach_from_start"] + adjustment)
                    tip_forward = max(0.0, geometry["tip_center_from_start"] + adjustment)
                    if coverage_slices and abs(float(adjustment)) > 1e-6:
                        adjusted_slices: List[Dict[str, float]] = []
                        for _slice in coverage_slices:
                            _near = max(0.0, float(_slice.get("near_from_start") or 0.0))
                            _far = max(_near, float(_slice.get("far_from_start") or 0.0) + float(adjustment))
                            adjusted_slices.append({**_slice, "near_from_start": _near, "far_from_start": _far})
                        coverage_slices = adjusted_slices
                    if coverage_temporal_layers and abs(float(adjustment)) > 1e-6:
                        adjusted_layers: List[Dict[str, Any]] = []
                        for _layer in coverage_temporal_layers:
                            adjusted_rows: List[Dict[str, float]] = []
                            for _slice in list(_layer.get("slices") or []):
                                _near = max(0.0, float(_slice.get("near_from_start") or 0.0))
                                _far = max(_near, float(_slice.get("far_from_start") or 0.0) + float(adjustment))
                                adjusted_rows.append({**_slice, "near_from_start": _near, "far_from_start": _far})
                            adjusted_layers.append({**_layer, "slices": adjusted_rows})
                        coverage_temporal_layers = adjusted_layers
                    tip_y = geometry["tip_y_from_start"]
                    tip_z = geometry["tip_z_from_start"]
                    tip_radius = max(0.0, geometry["tip_radius"])
                    if far <= 0.03 or tip_radius <= 0.01:
                        continue

                    if not candidates:
                        if coverage_slices:
                            ov.draw_saved_coverage_envelope(
                                source_slot,
                                display_center[0], display_center[1], display_center[2],
                                direction,
                                coverage_slices,
                                False,
                                "",
                                str(saved.get("move_name") or "RANGE"),
                                source_label=self._ruler_source_label(source_slot, saved),
                                airborne=bool(use_air_ruler),
                                temporal_layers=coverage_temporal_layers,
                            )
                        ov.draw_saved_range_zone(
                            source_slot, "", display_center[0], display_center[1], display_center[2],
                            direction, far, tip_forward, tip_y, tip_z, tip_radius,
                            None, False, str(saved.get("move_name") or "RANGE"), "learning", calibration_pips,
                            source_label=self._ruler_source_label(source_slot, saved),
                            airborne=bool(use_air_ruler),
                            posture=live_posture,
                        )
                        continue

                    best_gap = None
                    best_slot = nearest_slot
                    best_source = "profile"
                    best_vertical_overlap = True
                    best_hit_frame: Optional[int] = None
                    coverage_mode = bool(coverage_slices)
                    for _dist2, candidate_slot, candidate_root, candidate_local, candidate_source in candidates:
                        hlist = list(self.cached_hurtboxes.get(candidate_slot) or [])
                        if coverage_mode:
                            candidate_gap, vertical_overlap = self._coverage_gap_to_target(
                                float(display_center[0]),
                                float(display_center[1]),
                                direction,
                                coverage_slices or [],
                                candidate_root,
                                candidate_local,
                                hlist,
                            )
                            # A target at the wrong height is not a 2D coverage
                            # contact even if it is horizontally inside max X.
                            if not vertical_overlap:
                                item = (float("inf"), candidate_slot, "live" if hlist else candidate_source, False, None)
                            elif candidate_gap is None:
                                continue
                            else:
                                candidate_hit_frame = None
                                if float(candidate_gap) <= RANGE_COVERAGE_TOUCH_EPSILON:
                                    candidate_hit_frame = self._coverage_first_hit_frame(
                                        float(display_center[0]),
                                        float(display_center[1]),
                                        direction,
                                        coverage_frames,
                                        candidate_root,
                                        candidate_local,
                                        hlist,
                                        float(adjustment),
                                    )
                                item = (
                                    float(candidate_gap), candidate_slot,
                                    "live" if hlist else candidate_source, True,
                                    candidate_hit_frame,
                                )
                        else:
                            horizontal_gap = self._horizontal_ruler_gap_to_target(
                                float(display_center[0]),
                                direction,
                                far,
                                candidate_root,
                                candidate_local,
                                hlist,
                            )
                            if horizontal_gap is None:
                                continue
                            item = (float(horizontal_gap), candidate_slot, "live" if hlist else candidate_source, True, None)
                        if best_gap is None or item[0] < best_gap[0]:
                            best_gap = item
                    if best_gap is None:
                        target_gap = None
                        touching = False
                        best_vertical_overlap = False if coverage_mode else True
                        best_hit_frame = None
                    else:
                        target_gap, best_slot, best_source, best_vertical_overlap, best_hit_frame = best_gap
                        if not math.isfinite(float(target_gap)):
                            target_gap = None
                        touching = bool(best_vertical_overlap and target_gap is not None and float(target_gap) <= RANGE_COVERAGE_TOUCH_EPSILON)

                    state_override = None
                    detail_override = None
                    if coverage_mode:
                        state_override = "TOUCHING" if touching else "OUT OF COVERAGE"
                        if touching:
                            frame_tag = f"  ~F{int(best_hit_frame)}" if best_hit_frame is not None else ""
                            detail_override = f"{best_slot} body{frame_tag}"
                        elif not best_vertical_overlap:
                            detail_override = "HEIGHT MISS"
                        elif target_gap is not None:
                            detail_override = f"+{max(0.0, float(target_gap)):.2f}u"
                        else:
                            detail_override = "NO OVERLAP"
                        ov.draw_saved_coverage_envelope(
                            source_slot,
                            display_center[0], display_center[1], display_center[2],
                            direction,
                            coverage_slices or [],
                            touching,
                            best_slot,
                            str(saved.get("move_name") or "RANGE"),
                            source_label=self._ruler_source_label(source_slot, saved),
                            airborne=bool(use_air_ruler),
                            temporal_layers=coverage_temporal_layers,
                            hit_frame=best_hit_frame,
                        )

                    ov.draw_saved_range_zone(
                        source_slot, best_slot,
                        display_center[0], display_center[1], display_center[2],
                        direction, far, tip_forward, tip_y, tip_z, tip_radius,
                        target_gap, touching,
                        str(saved.get("move_name") or "RANGE"), str(best_source), calibration_pips,
                        float(geometry.get("advance_at_tip") or 0.0),
                        source_label=self._ruler_source_label(source_slot, saved),
                        airborne=bool(use_air_ruler),
                        posture=live_posture,
                        state_override=state_override,
                        detail_override=detail_override,
                    )
                except Exception:
                    continue

        # Contact audit markers are intentionally small and transient.  A
        # witness is engine-signalled but descriptor pairing is geometric; an
        # expected marker is the stored pair being checked on a later action.
        for marker in list(self._range_contact_markers):
            try:
                if int(marker.get("until_ms") or 0) < int(now_ms):
                    self._range_contact_markers.remove(marker)
                    continue
                ov.draw_range_contact_audit_marker(marker, witnessed=True)
            except Exception:
                continue
        for marker in list(self._range_contact_expectations.values()):
            try:
                if int(marker.get("until_ms") or 0) >= int(now_ms):
                    ov.draw_range_contact_audit_marker(marker, witnessed=False)
            except Exception:
                continue

        ov.draw_hud(self.last_counts, self.motion_filter, self.node_tracker, self.hurt_counts if hurtboxes_on else {})

# ----------------------------
# Main
# ----------------------------
def _valid_projectile_actor_candidate(actor: int, require_link_or_table: bool = True, table_seen: Optional[set] = None) -> bool:
    actor = int(actor or 0) & 0xFFFFFFFF
    if not (0x91000000 <= actor <= 0x94000000):
        return False

    owner = rd32(actor + ACTOR_OFF_OWNER) or 0
    if _owner_slot_name(owner) is None:
        return False

    proj_id = (rd32(actor + ACTOR_OFF_PROJ_ID) or 0) & 0xFFFFFFFF
    if proj_id <= 0 or proj_id > 0xFFFF:
        return False

    root_x = _rf(actor + ACTOR_OFF_X)
    root_y = _rf(actor + ACTOR_OFF_Y)
    root_z = _rf(actor + ACTOR_OFF_Z)
    if not _sane_projectile_world_point(root_x, root_y, root_z):
        return False
    if not _nonzero_projectile_world_point(root_x, root_y, root_z):
        return False

    if not require_link_or_table:
        return True

    if table_seen is not None and actor in table_seen:
        return True

    linked_record = rd32(actor + ACTOR_OFF_LINKED_RECORD) or 0
    return 0x90000000 <= linked_record <= 0x94000000


def get_projectile_actors():
    'Return unique raw projectile actor pointers.\n\n    ACTOR_TABLE is reliable for simple projectiles, but Morrigan-style missile\n    showers can have many actor structs in the projectile actor pool while only\n    exposing a small subset through ACTOR_TABLE.  The module seed from ACTOR_TABLE, then\n    scan the small stride-based actor pool and include candidates with valid\n    owner/id/root and either a valid linked hit record or table presence.\n    '
    actors: List[int] = []
    seen = set()
    table_seen = set()

    for i in range(ACTOR_MAX):
        ptr = rd32(ACTOR_TABLE + i * 4)
        if ptr is None:
            continue
        if not (0x91000000 <= ptr <= 0x94000000):
            continue
        if ptr in seen:
            continue
        seen.add(ptr)
        table_seen.add(ptr)
        actors.append(ptr)

    for pool_base in PROJECTILE_ACTOR_POOL_BASES:
        for i in range(PROJECTILE_ACTOR_POOL_COUNT):
            ptr = pool_base + i * PROJECTILE_ACTOR_POOL_STRIDE
            if ptr in seen:
                continue
            if not _valid_projectile_actor_candidate(ptr, require_link_or_table=True, table_seen=table_seen):
                continue
            seen.add(ptr)
            actors.append(ptr)

    return actors


def _owner_slot_name(owner: int) -> Optional[str]:
    owner = int(owner or 0) & 0xFFFFFFFF
    for slot_name, slot_base in SLOT_BASES.items():
        if owner == int(slot_base):
            return slot_name
    return None


def _sane_projectile_world_point(x: float, y: float, z: float) -> bool:
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
        return False
    return abs(x) < 30.0 and abs(y) < 30.0 and abs(z) < 30.0


def _nonzero_projectile_world_point(x: float, y: float, z: float) -> bool:
    return abs(x) + abs(y) + abs(z) > 0.001


def _projectile_anchor_distance(a_x: float, a_y: float, b_x: float, b_y: float) -> float:
    dx = float(a_x) - float(b_x)
    dy = float(a_y) - float(b_y)
    return math.sqrt(dx * dx + dy * dy)


def _valid_slot_ptr(ptr: int) -> bool:
    ptr = int(ptr or 0) & 0xFFFFFFFF
    return ptr in set(int(v) for v in SLOT_BASES.values())


def _safe_radius_from_scale(scale_value: float, fallback: float = PROJECTILE_FALLBACK_RADIUS) -> float:
    if math.isfinite(scale_value) and PROJECTILE_SCALE_RADIUS_MIN <= scale_value <= PROJECTILE_SCALE_RADIUS_MAX:
        return float(scale_value)
    return float(fallback)


def _normalize_2d(dx: float, dy: float) -> Tuple[float, float]:
    if not (math.isfinite(dx) and math.isfinite(dy)):
        return 0.0, 0.0
    mag = math.sqrt(dx * dx + dy * dy)
    if mag < 0.05 or mag > 4.0:
        return 0.0, 0.0
    return dx / mag, dy / mag


def _is_large_projectile_field(proj_id: int, linked_record: int) -> bool:
    if proj_id != 0x1:
        return False
    if not (0x90000000 <= int(linked_record or 0) <= 0x94000000):
        return False
    card0 = rd32(linked_record + 0x80) or 0
    card1 = rd32(linked_record + 0x84) or 0
    return (card0, card1) in PROJECTILE_LARGE_FIELD_CARDS


def _projectile_direction(actor: int, linked_record: int, proj_id: int, root_x: float, root_y: float, prev_x: float, prev_y: float) -> Tuple[float, float, float]:
    # Morrigan Finishing Shower / id 0x163 showed actor +0x108/+0x10C as
    # local/up, not forward. Doronjo/Odronjo's large field id 0x1 does the
    # same, so use per-frame root motion first for those cases.
    if proj_id in PROJECTILE_MOTION_DIR_FIRST_IDS or _is_large_projectile_field(proj_id, linked_record):
        ndx, ndy = _normalize_2d(root_x - prev_x, root_y - prev_y)
        if ndx or ndy:
            return ndx, ndy, 0.0

    # Casshan FLAG_309: actor +0x108/+0x10C and linked +0xAC/+0xB0 both
    # hold the useful forward vector.  actor +0x104 is Z-ish/unused for 2D.
    dx = _rf(actor + ACTOR_OFF_DIR_X)
    dy = _rf(actor + ACTOR_OFF_DIR_Y)
    ndx, ndy = _normalize_2d(dx, dy)
    if ndx or ndy:
        return ndx, ndy, _rf(actor + ACTOR_OFF_DIR_Z)

    if linked_record and (0x90000000 <= linked_record <= 0x94000000):
        dx = _rf(linked_record + LINKED_OFF_DIR_X)
        dy = _rf(linked_record + LINKED_OFF_DIR_Y)
        ndx, ndy = _normalize_2d(dx, dy)
        if ndx or ndy:
            return ndx, ndy, 0.0

    # Last fallback: motion vector from previous point to current root.
    ndx, ndy = _normalize_2d(root_x - prev_x, root_y - prev_y)
    return ndx, ndy, 0.0


def _projectile_extent(proj_id: int, radius: float) -> float:
    if proj_id in PROJECTILE_EXTENT_BY_ID:
        return float(PROJECTILE_EXTENT_BY_ID[proj_id])
    # Conservative generic guess: one diameter ahead of the root, clamped.
    return max(PROJECTILE_EXTENT_MIN, min(PROJECTILE_EXTENT_MAX, float(radius) * 2.0))


def _matrix_radius_scale(matrix_ptr: int) -> float:
    """Return a conservative radius scale from a live 3x4 attachment matrix."""
    if not (0x90000000 <= int(matrix_ptr or 0) < 0x94000000):
        return 1.0
    try:
        rows = [
            (_rf(matrix_ptr + 0x00), _rf(matrix_ptr + 0x04), _rf(matrix_ptr + 0x08)),
            (_rf(matrix_ptr + 0x10), _rf(matrix_ptr + 0x14), _rf(matrix_ptr + 0x18)),
            (_rf(matrix_ptr + 0x20), _rf(matrix_ptr + 0x24), _rf(matrix_ptr + 0x28)),
        ]
        scales = [math.sqrt(sum(v * v for v in row)) for row in rows]
        sane = [v for v in scales if math.isfinite(v) and 0.05 <= v <= 20.0]
        return max(sane) if sane else 1.0
    except Exception:
        return 1.0


def _linked_shape_point(
    record: int,
    coord_off: int,
    matrix_off: int = 0x04,
) -> Optional[Tuple[float, float, float]]:
    matrix_ptr = int(rd32(record + matrix_off) or 0) & 0xFFFFFFFF
    if not (0x90000000 <= matrix_ptr < 0x94000000):
        return None
    lx = _rf(record + coord_off)
    ly = _rf(record + coord_off + 4)
    lz = _rf(record + coord_off + 8)
    if not all(math.isfinite(v) and abs(v) < 100.0 for v in (lx, ly, lz)):
        return None
    point = _matrix_transform_point(matrix_ptr, lx, ly, lz)
    if not all(math.isfinite(v) and abs(v) < 40.0 for v in point):
        return None
    return point


def _read_linked_collider_object(
    linked: int,
    actor: int = 0,
) -> List[ProjectileActorState]:
    """Decode confirmed sphere, line, and box records from one linked object."""
    linked = int(linked or 0) & 0xFFFFFFFF
    actor = int(actor or 0) & 0xFFFFFFFF
    if not (0x90000000 <= linked < 0x94000000):
        return []

    collider_type = int(rd32(linked + LINKED_COLLIDER_OFF_TYPE) or 0)
    shapes = int(rd32(linked + LINKED_COLLIDER_OFF_SHAPES) or 0) & 0xFFFFFFFF
    count = int(rd32(linked + LINKED_COLLIDER_OFF_COUNT) or 0)
    stride = {
        LINKED_COLLIDER_TYPE_SPHERE: LINKED_SPHERE_STRIDE,
        LINKED_COLLIDER_TYPE_LINE: LINKED_LINE_STRIDE,
        LINKED_COLLIDER_TYPE_BOX: LINKED_BOX_STRIDE,
    }.get(collider_type)
    if stride is None or not (0x90000000 <= shapes < 0x94000000):
        return []
    if not (1 <= count <= LINKED_COLLIDER_MAX_SHAPES):
        return []

    owner = int(rd32(linked + 0x30) or 0) & 0xFFFFFFFF
    target_ptr = int(rd32(linked + 0x34) or 0) & 0xFFFFFFFF
    owner_slot = _owner_slot_name(owner)
    proj_id = 0
    if owner_slot is None and 0x90000000 <= actor < 0x94000000:
        owner = int(rd32(actor + ACTOR_OFF_OWNER) or 0) & 0xFFFFFFFF
        owner_slot = _owner_slot_name(owner)
        if owner_slot is None:
            owner = int(rd32(actor + ACTOR_OFF_OWNER_MIRROR) or 0) & 0xFFFFFFFF
            owner_slot = _owner_slot_name(owner)
        proj_id = int(rd32(actor + ACTOR_OFF_PROJ_ID) or 0) & 0xFFFF

    out: List[ProjectileActorState] = []
    for index in range(count):
        record = shapes + index * stride
        flags = int(rd32(record) or 0) & 0xFFFFFFFF
        if (flags & 1) == 0:
            continue

        matrix_ptr = int(rd32(record + 0x04) or 0) & 0xFFFFFFFF
        shape_actor = actor
        if not (0x90000000 <= shape_actor < 0x94000000) and 0x90000000 <= matrix_ptr < 0x94000000:
            candidate = matrix_ptr - 0x80
            if 0x90000000 <= candidate < 0x94000000:
                shape_actor = candidate

        shape_owner = owner
        shape_owner_slot = owner_slot
        shape_proj_id = proj_id
        if shape_owner_slot is None and 0x90000000 <= shape_actor < 0x94000000:
            shape_owner = int(rd32(shape_actor + ACTOR_OFF_OWNER) or 0) & 0xFFFFFFFF
            shape_owner_slot = _owner_slot_name(shape_owner)
            if shape_owner_slot is None:
                shape_owner = int(rd32(shape_actor + ACTOR_OFF_OWNER_MIRROR) or 0) & 0xFFFFFFFF
                shape_owner_slot = _owner_slot_name(shape_owner)
            shape_proj_id = int(rd32(shape_actor + ACTOR_OFF_PROJ_ID) or 0) & 0xFFFF
        if shape_owner_slot is None:
            shape_owner_slot = "P1"

        if collider_type == LINKED_COLLIDER_TYPE_SPHERE:
            point = _linked_shape_point(record, 0x08, 0x04)
            radius = abs(_rf(record + 0x14)) * _matrix_radius_scale(matrix_ptr)
            if point is None or not (0.01 <= radius <= 20.0):
                continue
            start = end = point
            shape_name = "sphere"
        elif collider_type == LINKED_COLLIDER_TYPE_LINE:
            matrix_a = int(rd32(record + 0x04) or 0) & 0xFFFFFFFF
            matrix_b = int(rd32(record + 0x08) or 0) & 0xFFFFFFFF
            a = _linked_shape_point(record, 0x0C, 0x04)
            b = _linked_shape_point(record, 0x18, 0x08)
            radius = abs(_rf(record + 0x3C)) * max(
                _matrix_radius_scale(matrix_a),
                _matrix_radius_scale(matrix_b),
            )
            if a is None or b is None or not (0.01 <= radius <= 20.0):
                continue
            start, end = a, b
            shape_name = "line"
        else:
            point = _linked_shape_point(record, 0x08, 0x04)
            hx = abs(_rf(record + 0x14))
            hy = abs(_rf(record + 0x18))
            hz = abs(_rf(record + 0x1C))
            if point is None or not all(math.isfinite(v) and 0.0 <= v <= 20.0 for v in (hx, hy, hz)):
                continue
            start = (point[0] - hx, point[1], point[2])
            end = (point[0] + hx, point[1], point[2])
            radius = max(0.01, hy, hz)
            shape_name = "box"

        x, y, z = end
        out.append(ProjectileActorState(
            actor=shape_actor,
            owner=shape_owner,
            owner_slot=shape_owner_slot,
            proj_id=shape_proj_id,
            x=x, y=y, z=z,
            prev_x=x, prev_y=y, prev_z=z,
            sweep_x=start[0], sweep_y=start[1], sweep_z=start[2],
            radius=radius,
            linked_record=linked,
            target_ptr=target_ptr,
            anchor_source=f"LINK-{shape_name}[{index}]",
            root_x=x, root_y=y, root_z=z,
            hit_start_x=start[0], hit_start_y=start[1], hit_start_z=start[2],
            hit_end_x=end[0], hit_end_y=end[1], hit_end_z=end[2],
            extent=math.dist(start, end),
        ))
    return out


def _read_all_linked_collision_objects() -> List[ProjectileActorState]:
    """Read the additive explosion and super-collider pool."""
    out: List[ProjectileActorState] = []
    seen = set()
    for pool_base in LINKED_COLLIDER_POOL_BASES:
        for index in range(LINKED_COLLIDER_POOL_COUNT):
            linked = pool_base + index * LINKED_COLLIDER_POOL_STRIDE
            actor = linked + LINKED_COLLIDER_TO_ACTOR_DELTA
            for shape in _read_linked_collider_object(linked, actor):
                key = (
                    shape.linked_record,
                    shape.anchor_source,
                    round(shape.x, 5),
                    round(shape.y, 5),
                    round(shape.z, 5),
                    round(shape.radius, 5),
                )
                if key in seen:
                    continue
                seen.add(key)
                out.append(shape)
    return out


def _read_projectile_actor(actor: int) -> Optional[ProjectileActorState]:
    owner = rd32(actor + ACTOR_OFF_OWNER) or 0
    owner_slot = _owner_slot_name(owner)
    if owner_slot is None:
        return None

    proj_id = rd32(actor + ACTOR_OFF_PROJ_ID) or 0
    proj_id &= 0xFFFFFFFF
    if proj_id <= 0 or proj_id > 0xFFFF:
        return None

    root_x = _rf(actor + ACTOR_OFF_X)
    root_y = _rf(actor + ACTOR_OFF_Y)
    root_z = _rf(actor + ACTOR_OFF_Z)
    if not _sane_projectile_world_point(root_x, root_y, root_z):
        return None

    prev_x = _rf(actor + ACTOR_OFF_PREV_X)
    prev_y = _rf(actor + ACTOR_OFF_PREV_Y)
    prev_z = _rf(actor + ACTOR_OFF_PREV_Z)
    if not _sane_projectile_world_point(prev_x, prev_y, prev_z):
        prev_x, prev_y, prev_z = root_x, root_y, root_z

    linked_record = rd32(actor + ACTOR_OFF_LINKED_RECORD) or 0
    target_ptr = 0
    if linked_record and (0x90000000 <= linked_record <= 0x94000000):
        target_ptr = rd32(linked_record + LINKED_OFF_TARGET) or 0

    impact_x = _rf(actor + ACTOR_OFF_IMPACT_X)
    impact_y = _rf(actor + ACTOR_OFF_IMPACT_Y)
    impact_z = _rf(actor + ACTOR_OFF_IMPACT_Z)
    contact_valid = (
        _valid_slot_ptr(target_ptr)
        and _sane_projectile_world_point(impact_x, impact_y, impact_z)
        and _nonzero_projectile_world_point(impact_x, impact_y, impact_z)
    )

    # The candidate collision shape is a short local capsule projected forward
    # from the actor root.  This is a probe for the actual damaging volume, not
    # a proven final answer yet.  Contact/result is drawn separately and never
    # used as the live projectile anchor.
    large_field = _is_large_projectile_field(proj_id, linked_record)
    if large_field:
        radius = PROJECTILE_LARGE_FIELD_RADIUS
        extent = PROJECTILE_LARGE_FIELD_EXTENT
    else:
        scale_radius = _safe_radius_from_scale(_rf(actor + ACTOR_OFF_SCALE_CANDIDATE))
        radius = float(PROJECTILE_RADIUS_BY_ID.get(proj_id, scale_radius))
        extent = _projectile_extent(proj_id, radius)

    dir_x, dir_y, dir_z = _projectile_direction(actor, linked_record, proj_id, root_x, root_y, prev_x, prev_y)
    if not (dir_x or dir_y):
        # If direction is unknown, keep the probe centered on the root rather
        # than drawing misleading geometry.
        hit_start_x, hit_start_y, hit_start_z = root_x, root_y, root_z
        hit_end_x, hit_end_y, hit_end_z = root_x, root_y, root_z
        x, y, z = root_x, root_y, root_z
        anchor_source = "root"
    else:
        hit_start_x, hit_start_y, hit_start_z = root_x, root_y, root_z
        hit_end_x = root_x + dir_x * extent
        hit_end_y = root_y + dir_y * extent
        hit_end_z = root_z
        x = root_x + dir_x * (extent * 0.5)
        y = root_y + dir_y * (extent * 0.5)
        z = root_z
        anchor_source = "field" if large_field else "cand"

    # Keep these fields populated for debug/backward compatibility, but do not
    # draw actor +0xE0/+0xE4 as a sweep. That field caused bogus half-screen
    # bars in live tests.
    sweep_x = hit_start_x
    sweep_y = hit_start_y
    sweep_z = hit_start_z

    return ProjectileActorState(
        actor=actor,
        owner=owner,
        owner_slot=owner_slot,
        proj_id=proj_id,
        x=x,
        y=y,
        z=z,
        prev_x=prev_x,
        prev_y=prev_y,
        prev_z=prev_z,
        sweep_x=sweep_x,
        sweep_y=sweep_y,
        sweep_z=sweep_z,
        radius=radius,
        linked_record=linked_record,
        anchor_source=anchor_source,
        root_x=root_x,
        root_y=root_y,
        root_z=root_z,
        impact_x=impact_x,
        impact_y=impact_y,
        impact_z=impact_z,
        contact_valid=contact_valid,
        target_ptr=target_ptr,
        dir_x=dir_x,
        dir_y=dir_y,
        dir_z=dir_z,
        hit_start_x=hit_start_x,
        hit_start_y=hit_start_y,
        hit_start_z=hit_start_z,
        hit_end_x=hit_end_x,
        hit_end_y=hit_end_y,
        hit_end_z=hit_end_z,
        extent=extent,
    )

def read_projectile_hitboxes(slot_filter: Optional[Dict[str, bool]] = None) -> List[ProjectileActorState]:
    """Return original projectiles plus additive explosion and super colliders.

    Fighter physical hitboxes remain entirely inside ``read_hitboxes()``. The
    original actor projectile reader is preserved, and confirmed linked shapes
    are appended as a separate third lane.
    """
    result: List[ProjectileActorState] = []

    # Original projectile lane, unchanged.
    for actor in get_projectile_actors():
        proj = _read_projectile_actor(actor)
        if proj is None:
            continue
        if slot_filter is not None and not slot_filter.get(proj.owner_slot, True):
            continue
        result.append(proj)

    # Additive explosion/super lane.
    for shape in _read_all_linked_collision_objects():
        if slot_filter is not None and not slot_filter.get(shape.owner_slot, True):
            continue
        result.append(shape)

    return result


def read_projectile_positions():
    """Compatibility shim for old callers: return current points only."""
    return [(p.x, p.y, p.z) for p in read_projectile_hitboxes()]

def debug_dump_pools():

    print("\n--- projectile pool dump ---")

    for pool in PROJECTILE_POOLS:

        print(f"\nPOOL 0x{pool:08X}")

        for i in range(PROJECTILE_NODE_COUNT):

            addr = pool + i * PROJECTILE_NODE_STRIDE

            x = _rf(addr + PROJ_OFF_X)
            y = _rf(addr + PROJ_OFF_Y)
            z = _rf(addr + PROJ_OFF_Z)

            d0 = _clean_dim(_rf(addr + PROJ_OFF_DIM_0))
            d1 = _clean_dim(_rf(addr + PROJ_OFF_DIM_1))
            d2 = _clean_dim(_rf(addr + PROJ_OFF_DIM_2))

            if abs(x) > 0.001 or abs(y) > 0.001 or abs(z) > 0.001:
                print(
                    f"node {i:02d} "
                    f"x={x:7.3f} y={y:7.3f} z={z:7.3f} "
                    f"d0={d0:.3f} d1={d1:.3f} d2={d2:.3f}"
                )

def resolve_projectile_pools():

    manager = 0x80476E50
    pools = []

    for i in range(16):

        ptr = rd32(manager + i * 4)
        if ptr is None:
            continue

        if 0x91000000 <= ptr <= 0x94000000:

            node_base = ptr - 0x6C
            pools.append(node_base)

    return pools
def has_valid_state_id(raw_state: int, state_id: int) -> bool:
    if raw_state == 0:
        return False
    if state_id <= 0:
        return False
    return True
            
def main():
    hook()
    dolphin_hwnd = find_dolphin_hwnd()
    if not dolphin_hwnd:
        print("Dolphin not found.")
        return

    overlay = Overlay(DISPLAY)
    overlay_hwnd = overlay.init_pygame()
    win32gui.SetWindowLong(overlay_hwnd, win32con.GWL_HWNDPARENT, dolphin_hwnd)
    clock = pygame.time.Clock()

    motion_filter = MotionFilter()

    scanner = ProjectileScanner()
    print("Projectile node watcher active (signature scan disabled).")
    print("  F3 = dump active node offset table to console")
    print("  F2 = (no-op, scan disabled)")

    total_nodes = len(PROJECTILE_POOLS) * PROJECTILE_NODE_COUNT
    node_tracker = ProjectileNodeTracker(total_nodes)

    _last_char_ids: Dict[str, int] = {}
    fd_by_slot: Dict[str, Dict[int, MoveFrameData]] = build_frame_data_cache()

    running = True
    while running:
        try:
            w, h = sync_overlay_to_dolphin(dolphin_hwnd, overlay_hwnd)
            overlay.on_resize(w, h)

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                # Keyboard hotkeys intentionally disabled; use the main GUI controls.

            char_changed = False
            for name, base in SLOT_BASES.items():
                cid = rd32(base + OFF_CHAR_ID) or 0
                if _last_char_ids.get(name) != cid:
                    print(f"[CharChange] {name} char_id {_last_char_ids.get(name)} -> {cid}")
                    _last_char_ids[name] = cid
                    char_changed = True

            if char_changed:
                fd_by_slot = build_frame_data_cache()

            camx, camy, camz, camw = read_camera_pos(CAMERA)
            cam_cos, cam_sin, cam_translate_y, cam_depth_y, cam_depth_z, cam_depth_translate = read_camera_view_affine(CAMERA)
            if USE_LIVE_CAMERA:
                overlay.cam_x = camx
                overlay.cam_y = camy
                overlay.cam_z = camz
                overlay.set_camera_view_affine(cam_cos, cam_sin, cam_translate_y, cam_depth_y, cam_depth_z, cam_depth_translate)
                overlay.set_giant_x_anchor(_derive_giant_x_anchor() if abs(float(camz or 0.0)) >= GIANT_CAMERA_Z_THRESHOLD else None)

            slot_filter = _read_slot_filter()

            if pygame.time.get_ticks() % 2 == 0:
                update_projectile_nodes(node_tracker)

            overlay.clear()
            overlay.draw_debug_axes()

            counts = {}

            for name, base in SLOT_BASES.items():
                try:
                    if not slot_filter.get(name, True):
                        counts[name] = 0
                        continue

                
                    raw_state = read_state_raw(base)
                    state_id = decode_state_id(raw_state)
                    char_id = rd32(base + OFF_CHAR_ID) or 0

                    if _last_state_raws.get(name) != raw_state:
                        dump_state18(name, base)
                        print(
                            f"[StateChange] {name} char_id={char_id} "
                            f"raw=0x{raw_state:08X} "
                            f"hi16=0x{(raw_state >> 16) & 0xFFFF:04X} ({(raw_state >> 16) & 0xFFFF}) "
                            f"low16=0x{raw_state & 0xFFFF:04X} ({raw_state & 0xFFFF}) "
                            f"low8=0x{raw_state & 0xFF:02X} ({raw_state & 0xFF}) "
                            f"decoded={state_id} passive={is_passive_state(state_id)}"
                        )

                        _last_state_raws[name] = raw_state
                        _last_state_ids[name] = state_id
                    boxes = read_hitboxes(base, HITBOX)
                    active = 0
                    palette = COLORS.get(name, [(255, 255, 255)])

                    if slot_passive_override(name, state_id):
                        counts[name] = 0
                        continue

                    for i, (x, y, r, flag) in enumerate(boxes):
                        visible = motion_filter.update(name, i, x, y, r)

                        fd = lookup_frame_data(fd_by_slot, name, state_id)
                        # When frame data is available, keep drawing the dim startup
                        # hitboxes instead of letting the stillness filter hide them.
                        if not visible and fd is None:
                            continue

                        if r > 0.001:
                            active += 1
                            base_color = palette[i % len(palette)]
                            action_frame = read_action_frame(base)
                            is_active = is_frame_data_active(fd, action_frame) if fd is not None else (flag == 0x53)
                            draw_color = base_color if is_active else _dim_hitbox_color(base_color)
                            overlay.draw_hitbox(
                                x,
                                y,
                                0,
                                r,
                                draw_color,
                                f"{name}[{i}]",
                                is_active=is_active,
                                show_label=False,
                            )

                    counts[name] = active

                except Exception as slot_exc:
                    print(
                        f"[SlotError] slot={name} base=0x{base:08X} "
                        f"char_id={rd32(base + OFF_CHAR_ID) or 0} "
                        f"state_id={read_state_id(base)} "
                        f"err={slot_exc!r}"
                    )
                    pause_on_error(f"SlotError:{name}", slot_exc)
                    running = False
                    break
                
            # Keep standalone rendering responsive during hits as well.  The
            # broad resolver-pool contact scan is intentionally disabled here;
            # it caused thousands of Dolphin reads per hit frame.
            contacts_by_target: Dict[str, List[HitContactState]] = {}

            hurt_counts: Dict[str, int] = {}
            for hurt_slot, hurt_base in SLOT_BASES.items():
                hurt_counts[hurt_slot] = read_and_draw_hurtboxes_for_slot(
                    overlay,
                    hurt_slot,
                    hurt_base,
                    contacts_by_target.get(hurt_slot, []),
                )

            if any(slot_filter.values()):
                projectiles = read_projectile_hitboxes(slot_filter)
                for proj in projectiles:
                    color = PROJ_COLORS.get(proj.owner_slot, COL_PROJ)
                    overlay.draw_projectile_hitbox(
                        proj.x,
                        proj.y + PROJECTILE_Y_OFFSET,
                        proj.z,
                        proj.radius,
                        color,
                        proj.label(False),
                        sweep_x=(proj.hit_start_x if proj.extent > 0.0001 else None),
                        sweep_y=((proj.hit_start_y + PROJECTILE_Y_OFFSET) if proj.extent > 0.0001 else None),
                        sweep_z=(proj.hit_start_z if proj.extent > 0.0001 else None),
                        contact_x=proj.impact_x,
                        contact_y=proj.impact_y + PROJECTILE_Y_OFFSET,
                        contact_z=proj.impact_z,
                        contact_valid=proj.contact_valid,
                        root_x=proj.root_x,
                        root_y=proj.root_y + PROJECTILE_Y_OFFSET,
                        root_z=proj.root_z,
                        show_label=False,
                    )

            # Late hurtbox pass: draw this layer after normal boxes/projectiles so
            # it cannot be hidden underneath the existing overlay primitives.
            # Recompute counts here so the HUD reflects the late pass too.
            hurt_counts = {}
            for hurt_slot, hurt_base in SLOT_BASES.items():
                hurt_counts[hurt_slot] = read_and_draw_hurtboxes_for_slot(
                    overlay,
                    hurt_slot,
                    hurt_base,
                    contacts_by_target.get(hurt_slot, []),
                )

            try:
                sample_bits = []
                for _hs, _hb in SLOT_BASES.items():
                    _hlist = read_hurtboxes(_hs, _hb)
                    if _hlist:
                        _h0 = _hlist[0]
                        sample_bits.append(f"{_hs}[0]={_h0.x:.2f},{_h0.y:.2f} r={_h0.radius:.2f}")
                    else:
                        sample_bits.append(f"{_hs}[0]=NONE")
                globals()["_last_hurt_debug_text"] = " | ".join(sample_bits)
            except Exception as _e:
                globals()["_last_hurt_debug_text"] = f"hurt debug err={_e!r}"

            if pygame.time.get_ticks() % 300 == 0:
                motion_filter.cleanup()

            overlay.draw_hud(counts, motion_filter, node_tracker, hurt_counts)
            overlay.present()
            clock.tick(DISPLAY.fps)

        except Exception as exc:
            pause_on_error("MainLoopCrash", exc)
            running = False

    pygame.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        pause_on_error("FatalCrash", exc)
        raise