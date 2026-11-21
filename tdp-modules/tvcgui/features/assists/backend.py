from __future__ import annotations

import csv
import os
import json
import struct
import time
import threading
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox

from tvcgui.core.tk_host import tk_call
from tvcgui.core.paths import resource_path

try:
    from tvcgui.platform.dolphin import rbytes, wbytes
except Exception:
    rbytes = None
    wbytes = None

try:
    import tvcgui.platform.patch_manager as _rpm
except Exception:
    _rpm = None

try:
    from tvcgui.features.combat.move_id_map import lookup_move_name as _lookup_move_name
except Exception:
    def _lookup_move_name(aid: int) -> str | None:
        return None

SCAN_START = 0x90000000
SCAN_END = 0x94000000
SCAN_BLOCK = 0x40000
OVERLAP = 0x400

_CHR_TBL_BASES = [
    0x90896640,
    0x908F1920,
    0x909478E0,
    0x9099D9C0,
]

# Confirmed Chun assist selector graft.
#
# Validated live patch:
#   Graft this 0x2C-byte Ryu-style selector setup over the active Chun assist
#   block that begins 0x48 bytes before the live Tensho state wrapper.
#
# Original live Chun block:
#   0F060027 3732203F 00000003 3733203F 00000009
#   013C0000 00001610 013C0000 000016C8 013C0000 00001744
#
# Grafted selector block:
#   0F060027 01500060 000044B4 00000003
#   0003BCC8 0003C4AC 0003CC90
#   3732203F 00000002 3733203F 00000004
#
# After install, the selector words are:
#   graft_addr + 0x10
#   graft_addr + 0x14
#   graft_addr + 0x18
CHUN_SELECTOR_ORIGINAL_BLOCK = bytes.fromhex(
    "0F 06 00 27 "
    "37 32 20 3F 00 00 00 03 "
    "37 33 20 3F 00 00 00 09 "
    "01 3C 00 00 00 00 16 10 "
    "01 3C 00 00 00 00 16 C8 "
    "01 3C 00 00 00 00 17 44"
)

CHUN_SELECTOR_GRAFT_BLOCK = bytes.fromhex(
    "0F 06 00 27 "
    "01 50 00 60 00 00 44 B4 00 00 00 03 "
    "00 03 BC C8 00 03 C4 AC 00 03 CC 90 "
    # Ryu-style selector args. The original Chun graft used 3/9; that proved
    # the graft mechanism, but direct chr_tbl move presets behave more like
    # Ryu's native assist selector and need 2/4.
    "37 32 20 3F 00 00 00 02 "
    "37 33 20 3F 00 00 00 04"
)

CHUN_SELECTOR_GRAFT_DELTA_TO_WRAPPER = 0x48
CHUN_SELECTOR_WORD_OFFSETS = (0x10, 0x14, 0x18)

CHUN_SELECTOR_PAYLOADS = [
    ("force all lanes -> 0003D620", bytes.fromhex("00 03 D6 20")),
    ("force all lanes -> 0003BCC8", bytes.fromhex("00 03 BC C8")),
    ("force all lanes -> 0003C4AC", bytes.fromhex("00 03 C4 AC")),
    ("force all lanes -> 0003CC90", bytes.fromhex("00 03 CC 90")),
]

CHUN_SELECTOR_RESET_WORDS = {
    0x10: bytes.fromhex("00 03 BC C8"),
    0x14: bytes.fromhex("00 03 C4 AC"),
    0x18: bytes.fromhex("00 03 CC 90"),
}

CHUN_SELECTOR_WORD_PRESETS = [
    ("Chun payload 0003D620", 0x0003D620),
    ("Chun payload 0003BCC8", 0x0003BCC8),
    ("Chun payload 0003C4AC", 0x0003C4AC),
    ("Chun payload 0003CC90", 0x0003CC90),
]

_FIGHTER_BASES = [
    0x9246B9C0,
    0x927EB9E0,
    0x92B6BA00,
    0x92EEBA20,
]
_CHAR_ID_OFF = 0x14

FIGHTER_SLOT_LABELS = ("P1-C1", "P2-C1", "P1-C2", "P2-C2")


# Main/HUD context piggyback.
# main.py owns the reliable live slot snapshots. The scanner keeps assist logic
# here, but uses the HUD snapshots to avoid full MEM2 scans and to support
# quick-assist buttons without making main.py understand assist internals.
_MAIN_SNAPS_CACHE: dict[str, dict] = {}
_MAIN_SNAPS_LOCK = threading.RLock()

QUICK_ASSISTS_FILE = "quick_assists.json"
QUICK_ASSISTS_FILE_CANDIDATES = (
    os.path.join("data", "assists", QUICK_ASSISTS_FILE),
    QUICK_ASSISTS_FILE,
    os.path.join("assets", QUICK_ASSISTS_FILE),
    os.path.join("data", QUICK_ASSISTS_FILE),
)
_QUICK_ASSISTS_CACHE: tuple[float, dict] | None = None

DEFAULT_QUICK_ASSISTS = {
    "default": [
        {"label": "304", "table": 304},
        {"label": "305", "table": 305},
        {"label": "306", "table": 306},
        {"label": "Default", "default": True},
    ]
}

# Headless profiles are used when quick assists are clicked from the HUD while
# the Tk scanner window is not open. If the window is open, it mirrors the same
# profiles in its existing per-fighter table.
_HEADLESS_ASSIST_PROFILES: dict[int, dict[str, object]] = {}
# Slot-specific quick-assist profiles. This is the important mirror-match layer:
# when the same character is loaded on both sides, their selector/graft can be
# shared at rest, so the desired assist must be remembered per visible HUD slot
# and applied only when that slot actually calls assist.
_HEADLESS_SLOT_ASSIST_PROFILES: dict[str, dict[str, object]] = {}
_HEADLESS_LAST_ASSIST_ATTACK_BY_BASE: dict[int, bool] = {}
_HEADLESS_LAST_PATCH_KEY: tuple[int, int] | None = None
_HEADLESS_LAST_RUNTIME_WRITE_BY_SLOT: dict[str, tuple[tuple, float]] = {}
_ASSIST_RUNTIME_WRITE_THROTTLE_SECONDS = 0.012
_ASSIST_LOG_LAST_TS: dict[str, float] = {}


def _assist_log_once(key: str, message: str, *, interval: float = 1.0) -> None:
    try:
        now = time.monotonic()
        last = float(_ASSIST_LOG_LAST_TS.get(str(key), 0.0) or 0.0)
        if now - last < float(interval):
            return
        _ASSIST_LOG_LAST_TS[str(key)] = now
        print(message)
    except Exception:
        pass

# Quick route cache. Quick assist buttons should not rescan/re-resolve the
# active graft route on every click. main.py feeds live snaps each frame; this
# cache is warmed in the background when a loaded character is seen, then quick
# clicks only resolve table -> word and write the already-known route bytes.
_QUICK_ROUTE_CACHE: dict[tuple[int, int], dict] = {}
_QUICK_ROUTE_INFLIGHT: set[tuple[int, int]] = set()
_QUICK_ROUTE_LOCK = threading.RLock()
_QUICK_ROUTE_FAIL_UNTIL: dict[tuple[int, int], float] = {}
_QUICK_ROUTE_FAIL_TTL_SECONDS = 0.75
_QUICK_ROUTE_MAX_INFLIGHT = 4

# Dedicated quick-assist click lane.
#
# HUD button clicks must not run route resolution, chr_tbl reads, or memory
# writes on the pygame/main UI lane.  The public apply_quick_assist_from_main()
# now only validates/queues the selected route and returns immediately. This
# worker serializes the real resolver/write work off the UI lane and coalesces
# repeated clicks per slot so a fast click sequence does not build a
# backlog of stale assist writes.
_QUICK_ASSIST_LANE_LOCK = threading.RLock()
_QUICK_ASSIST_LANE_EVENT = threading.Event()
_QUICK_ASSIST_LANE_PENDING: dict[str, dict[str, object]] = {}
_QUICK_ASSIST_LANE_ORDER: list[str] = []
_QUICK_ASSIST_LANE_STARTED = False
_QUICK_ASSIST_LANE_SEQ = 0
_QUICK_ASSIST_LANE_LAST_RESULT: dict[str, dict[str, object]] = {}

# UI scan result cache. This never changes assist write behavior. It only lets
# the scanner window reuse rows that the proven scanner already found, instead
# of rescanning MEM2 just to redraw the same table.
_ASSIST_SCAN_CACHE_LOCK = threading.RLock()
_ASSIST_SCAN_CACHE_SIGNATURE: tuple | None = None
_ASSIST_SCAN_CACHE_HITS: list[dict] = []

# Runtime cache invalidation.
#
# Route rows are absolute MEM2 addresses. If a match ends and the same character
# is picked again, owner_base + char_id can be identical while the live chr_tbl /
# graft address has been rebuilt. Keep a cheap epoch and bump it whenever main.py
# shows a slot unload, invalid/transitional char id, or character/base change.
# Cached route rows from older epochs are ignored and re-resolved on the next
# quick-assist click. This does not change any write/graft logic.
_ASSIST_CACHE_EPOCH = 0
_ASSIST_LAST_SLOT_SIGNATURES: dict[str, tuple[int, int]] = {}

# The selector graft changes the attack body, but several moves also depend on
# the assist wrapper's animation state. Keep the original state so Default can
# restore it, and sync the selected move state for the affected characters.
_ASSIST_STATE_SYNC_CACHE: dict[tuple[int, int, int], dict[str, int]] = {}


# Shared-character selector tables mean duplicate characters cannot keep
# separate table bytes at rest. Per-fighter profiles are stored separately,
# then Auto Slot Switch patches the shared character table when that fighter
# enters an assist-ish state.
ASSIST_RUNTIME_ATTACK_STATES = {420, 426, 427, 428}
ASSIST_RUNTIME_PREFETCH_STATES = {430, 424, 425, 432, 433}
ASSIST_RUNTIME_STATE_PRIORITY = {426: 100, 430: 45, 424: 35, 425: 30, 427: 30}
FIGHTER_MOVE_SCAN_SIZE = 0x6000

# Generic characters may have many selector-looking tables. For assist
# hijacking, prefer candidates physically near the assist route entries.
GENERIC_ASSIST_ROUTE_TABLE_INDICES = (420, 424, 425, 426, 427, 428, 430, 431, 432, 433)
GENERIC_ASSIST_ROUTE_BEFORE = 0x40
GENERIC_ASSIST_ROUTE_AFTER = 0x260
GENERIC_ASSIST_ROUTE_SCORE_BONUS = 600

# Generic fallback for characters whose assist route does not expose a usable
# Ryu/Chun-style selector table. This keeps the assist attack chr_tbl entry
# pointed at the original wrapper, then patches internal wrapper call operands.
# That preserves hop-in/positioning while redirecting the attack body.
ANCHOR_ASSIST_FALLBACK_TABLE_INDEX = 426
ANCHOR_ASSIST_WRAPPER_SCAN_SIZE = 0x900
ANCHOR_ASSIST_OPERAND_MIN = 0x00003600
ANCHOR_ASSIST_OPERAND_MAX = 0x00090000
ANCHOR_ASSIST_MAX_OPERANDS = 8
ANCHOR_ASSIST_CALL_OPS = (
    b"\x01\x32\x00\x00",
    b"\x01\x33\x00\x00",
    b"\x01\x36\x00\x00",
)

# Viewtiful Joe special case.
#
# Viewtiful Joe special case.
#
# VJoe is handled as a static/direct chr_tbl[426] route patch for testing.
# Selecting a VJoe preset writes chr_tbl[426] immediately and leaves it there.
# The main auto-trigger deliberately does not prearm/rewrite VJoe during 430,
# because the 430-prearm route tested worse than the static/direct write.
VJOE_CHAR_ID = 17
VJOE_TRAMPOLINE_SCAN_SIZE = 0x2400
VJOE_TRAMPOLINE_STATE_SEARCH_START = 0x80
VJOE_TRAMPOLINE_STATE_SEARCH_END = 0x600
VJOE_TRAMPOLINE_CONT_OFF_FROM_STATE_WRAPPER = 0x18
VJOE_TRAMPOLINE_STATE_FIELD_OFF = 0x12
VJOE_TRAMPOLINE_STATE_BY_TABLE_INDEX = {
    304: 0x0112,  # Voomerang A
    305: 0x0113,  # Voomerang B
    306: 0x0114,  # Voomerang C
}

# Volnutt special case.
#
# Volnutt's assist is not just a normal move selector. His 6B-style assist body
# is augmented by the active weapon arm. Quick assists can therefore include a
# Volnutt weapon value and still point the assist route at the normal 6B table.
# The scanner searches only inside Volnutt's resolved chr_tbl[426] wrapper and
# patches the small weapon selector bytes observed in the assist package.
VOLNUTT_CHAR_ID = 18
VOLNUTT_6B_TABLE_INDEX = 270
VOLNUTT_WEAPON_NAMES = {
    0: "Arm",
    1: "Drill",
    2: "Gun",
    3: "Shield",
}
VOLNUTT_WEAPON_ALIASES = {
    "arm": 0,
    "normal": 0,
    "default_arm": 0,
    "default arm": 0,
    "drill": 1,
    "gun": 2,
    "shield": 3,
}
VOLNUTT_WEAPON_WRAPPER_SCAN_SIZE = 0x3000
VOLNUTT_WEAPON_ASSIST_START_HEAD = bytes.fromhex(
    "04 01 60 00 00 00 00 58 3F 00 00 00 00 40 02 00 "
    "04 02 60 00 00 00 45 FC 3F 00 00 00 00 00 00"
)
VOLNUTT_WEAPON_ASSIST_START_TAIL = bytes.fromhex(
    "01 33 00 00 00 04 42 5C"
)
VOLNUTT_WEAPON_SETUP_HEAD = bytes.fromhex(
    "04 15 60 00 00 00 46 04 3F 00 00 00 00 00 00 02 "
    "04 01 60 00 00 00 46 00 3F 00 00 00 00 00 00"
)
VOLNUTT_WEAPON_SETUP_TAIL = bytes.fromhex(
    "04 02 60 00 00 00 45 FC 3F 00 00 00 00 00 00 00"
)

# Tatsunoko fallback class. These characters do not always expose a clean
# Ryu/Chun-style selector block. For them, the assist hop-in is handled by
# standby/430, while chr_tbl[426] is the attack route. A wrapper-preserving row patches the active chr_tbl[426]
# wrapper operands instead of trusting the broad slot base. This avoids fake
# direct rows when the real chr_tbl sits shortly before the nominal slot.
TATSUNOKO_DIRECT_426_CHAR_IDS = {
    1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 26, 27, 28,
}
DIRECT_426_TABLE_INDEX = 426
# Tatsunoko assists use the same 430 -> 426 -> taunt -> leave flow, but the
# first visible attack body starts inside the 426 wrapper around +0x154.
# Patching the later anchored call makes Casshan do default first, then the
# selected move. Graft at the attack-body start instead.
TATSUNOKO_WRAPPER_GRAFT_OFF = 0x154
TATSUNOKO_WRAPPER_GRAFT_SCAN_END = 0x380

# Confirmed state-coupled custom assists:
# Ken Bird Run needs its projectile and return callback, Condor Battering Ram
# and Polimar need hitboxes timed and attached to the selected animation, and
# Zero otherwise keeps the native Ryuenjin assist animation for every attack.
ASSIST_STATE_SYNC_CHAR_IDS = frozenset((1, 4, 27, 29))
ASSIST_STATE_SYNC_SCAN_SIZE = 0x900
ASSIST_STATE_SYNC_EXCLUDED_STATES = frozenset((0x01A1, 0x01A8, 0x01AE))


CHAR_ID_TO_KEY = {
    1: "Ken the Eagle", 2: "Casshan", 3: "Tekkaman", 4: "Polimar",
    5: "Yatterman-1", 6: "Doronjo", 7: "Ippatsuman", 8: "Jun the Swan",
    10: "Karas", 11: "Gold Lightan", 12: "Ryu", 13: "Chun-Li",
    14: "Batsu", 15: "Morrigan", 16: "Alex", 17: "Viewtiful Joe",
    18: "Volnutt", 19: "Roll", 20: "Saki", 21: "Soki", 22: "PTX-40A",
    23: "Yami", 24: "Yami", 25: "Yami", 26: "Tekkaman Blade",
    27: "Joe the Condor", 28: "Yatterman-2", 29: "Zero", 30: "Frank West",
}

# Confirmed live Ryu selector chain:
#   0x908C7680: 00 03 11 0C 00 03 14 6C 00 03 1C DC 37 32 20 3F
# Validated effects:
#   0003110C = Hadouken
#   0003146C = Shoryu
#   00031CDC = Tatsu
RYU_KNOWN_TARGETS = {
    0x0003110C: "Ryu Hadouken",
    0x0003146C: "Ryu Shoryu",
    0x00031CDC: "Ryu Tatsu",
}
RYU_PRESETS = [
    ("Hadouken", 0x0003110C),
    ("Shoryu", 0x0003146C),
    ("Tatsu", 0x00031CDC),
]


# Confirmed Ryu assist selector graft/table.
#
# Ryu already has the full Ryu-style selector setup in the live assist route.
# This exposes it the same way as the Chun graft table, so a lane can be
# double-clicked and written with a preset/manual selector word.
RYU_SELECTOR_GRAFT_BLOCK = bytes.fromhex(
    "0F 06 00 27 "
    "01 50 00 60 00 00 44 B4 00 00 00 03 "
    "00 03 11 0C 00 03 14 6C 00 03 1C DC "
    "37 32 20 3F 00 00 00 02 "
    "37 33 20 3F 00 00 00 04"
)

RYU_SELECTOR_WORD_OFFSETS = (0x10, 0x14, 0x18)

RYU_SELECTOR_RESET_WORDS = {
    0x10: bytes.fromhex("00 03 11 0C"),
    0x14: bytes.fromhex("00 03 14 6C"),
    0x18: bytes.fromhex("00 03 1C DC"),
}

RYU_SELECTOR_WORD_PRESETS = [
    ("Ryu Hadouken 0003110C", 0x0003110C),
    ("Ryu Shoryu 0003146C", 0x0003146C),
    ("Ryu Tatsu 00031CDC", 0x00031CDC),
]


# Generic assist selector graft probing.
#
# This is the experimental path for every other character. It looks for two
# shapes:
#   1) Native Ryu-style selector tables:
#      0F060027 01500060 000044B4 00000003 [3 words] 3732203F ... 3733203F ...
#   2) Chun-style graftable assist blocks:
#      0F060027 3732203F [arg] 3733203F [arg] 013C0000 ...
#
# Double-clicking a generic lane writes a full Ryu-style selector setup first.
# For graft candidates that do not have selector words yet, the chosen manual
# word is applied to all three lanes by default so the install has valid words.
GENERIC_SELECTOR_SETUP_PREFIX = bytes.fromhex(
    "0F 06 00 27 "
    "01 50 00 60 00 00 44 B4 00 00 00 03"
)
GENERIC_SELECTOR_WORD_OFFSETS = (0x10, 0x14, 0x18)
GENERIC_SELECTOR_TABLE_LEN = 0x2C
GENERIC_SELECTOR_DEFAULT_ARG_32 = bytes.fromhex("00 00 00 03")
GENERIC_SELECTOR_DEFAULT_ARG_33 = bytes.fromhex("00 00 00 09")
GENERIC_SELECTOR_MANUAL_PRESETS = [
    ("Chun 0003D620", 0x0003D620),
    ("Chun 0003BCC8", 0x0003BCC8),
    ("Chun 0003C4AC", 0x0003C4AC),
    ("Chun 0003CC90", 0x0003CC90),
    ("Ryu Hadouken 0003110C", 0x0003110C),
    ("Ryu Shoryu 0003146C", 0x0003146C),
    ("Ryu Tatsu 00031CDC", 0x00031CDC),
]

# Move preset harvesting for assist grafts.
#
# These rows use the same basic logic as scan normals all:
#   selector_word = move_abs - chr_tbl_abs
# Double-clicking one writes that selector word into the active graft lanes.
MOVE_PRESET_CHR_TBL_NUM_ENTRIES = 705
MOVE_PRESET_DATA_START_OFF = 0x3600
MOVE_PRESET_SLOT_SIZE = 0x90000
# Some Tatsunoko/Capcom runtime chr_tbls are laid down shortly before
# the nominal broad slot base. Scan back far enough to catch the active
# table instead of falling back to random script bytes at the broad base.
# Some characters, especially late Tatsunoko slots such as Tekkaman Blade
# on P2-C2, can place the active chr_tbl before the nominal broad owner
# base. A 0x12000 back-scan misses Blade in observed dumps
# (owner 0x9099D9C0, live chr_tbl 0x90986000, delta 0x179C0).
# Keep this below the inter-slot gap so do not walk into the previous
# loaded character window.
CHR_TBL_PRE_START_BACK = 0x24000
MOVE_PRESET_LOOKAHEAD = 0x80
MOVE_PRESET_ENTRY_OFFSETS = (0x00, 0x10, 0x20, 0x40, 0x70, 0x90)
MOVE_PRESET_ANIM_HDR = bytes.fromhex("04 01 60 00 00 00 01 E8 3F 00 00 00")
MOVE_PRESET_NORMAL_LABELS = {
    0x00: "5A", 0x01: "5B", 0x02: "5C",
    0x03: "2A", 0x04: "2B", 0x05: "2C",
    0x06: "6C", 0x08: "3C",
    0x09: "j.A", 0x0A: "j.B", 0x0B: "j.C",
    0x0E: "6B",
}
MOVE_PRESET_NORMAL_IDS = set(MOVE_PRESET_NORMAL_LABELS.keys())

# Optional assist preset name map.
#
# Do not embed/manual-define the preset names here. The assist picker reads
# move_id_map_charagnostic.csv at runtime and uses its table/index number as
# the preset key. Put the CSV beside this scanner/module, in the current
# working directory, or in ./assets or ./data.
ASSIST_PRESET_NAME_FILE = "move_id_map_charagnostic.csv"
ASSIST_PRESET_NAME_FILE_CANDIDATES = (
    os.path.join("data", "combat", ASSIST_PRESET_NAME_FILE),
    ASSIST_PRESET_NAME_FILE,
    os.path.join("assets", ASSIST_PRESET_NAME_FILE),
    os.path.join("data", ASSIST_PRESET_NAME_FILE),
)

_ASSIST_PRESET_NAME_CACHE: dict[tuple[int, int], tuple[int, str]] | None = None


def _parse_int_loose(text: str) -> int | None:
    s = (text or "").strip().lower()
    if not s:
        return None
    try:
        return int(s, 0)
    except Exception:
        try:
            return int(s, 16)
        except Exception:
            return None


def _clean_preset_name(name: str | None) -> str:
    s = str(name or "").strip()
    while "  " in s:
        s = s.replace("  ", " ")
    return s


def _is_filtered_assist_preset_name(name: str | None) -> bool:
    """Hide noisy non-attack presets from the assist preset picker."""
    s = _clean_preset_name(name).lower()
    if not s:
        return False
    normalized = "".join(ch if ch.isalnum() else " " for ch in s)
    tokens = set(normalized.split())

    # Movement/system labels are real frame-data names, but they are noise for
    # the assist move picker. Tekkaman exposed a table where many low entries
    # resolved as "backward", which pushed every useful 304+ move off-screen.
    movement_noise = {
        "backward", "forward", "walk", "run", "dash", "jump",
        "turn", "stand", "crouch", "guard", "block", "wait",
        "neutral", "landing", "idle", "filler",
    }
    if s in movement_noise or tokens.intersection({"idle", "landing", "filler"}):
        return True

    if "air dash" in s or "airdash" in s:
        return True

    return False


def _is_named_preset_name(name: str | None) -> bool:
    s = _clean_preset_name(name)
    if not s:
        return False
    if _is_filtered_assist_preset_name(s):
        return False
    low = s.lower()
    if low in ("?", "unknown", "none", "move_????", "anim_--"):
        return False
    if low.startswith("anim_"):
        return False
    if low.startswith("move 0x"):
        return False
    if " / anim 0x" in low:
        return False
    return True


def _norm_csv_key(key: str | None) -> str:
    return "".join(ch for ch in str(key or "").strip().lower() if ch.isalnum())


def _dict_pick(row: dict[str, str], names: tuple[str, ...]) -> str | None:
    wanted = {_norm_csv_key(n) for n in names}
    for key, value in row.items():
        if _norm_csv_key(key) in wanted:
            return value
    return None


def _candidate_assist_csv_paths() -> list[str]:
    search_dirs: list[str] = []
    try:
        search_dirs.append(resource_path())
    except Exception:
        pass
    try:
        search_dirs.append(os.getcwd())
    except Exception:
        pass

    out: list[str] = []
    seen: set[str] = set()
    for folder in search_dirs:
        if not folder:
            continue
        for filename in ASSIST_PRESET_NAME_FILE_CANDIDATES:
            path = filename if os.path.isabs(filename) else os.path.join(folder, filename)
            norm = os.path.normcase(os.path.abspath(path))
            if norm in seen:
                continue
            seen.add(norm)
            out.append(path)
    return out


def _parse_assist_csv_dict_row(row: dict[str, str]) -> tuple[int, int, int, str] | None:
    table_text = _dict_pick(row, (
        "table_index", "table", "entry", "index", "number", "num", "action", "action_id",
        "input", "input_id", "input_number", "move_index", "move_number",
    ))
    anim_text = _dict_pick(row, (
        "anim_id", "animation_id", "anim", "animation", "state_id", "state", "aid",
    ))
    name_text = _dict_pick(row, (
        "name", "move", "move_name", "label", "display", "display_name", "description",
    ))
    char_text = _dict_pick(row, (
        "char_id", "character_id", "cid", "character", "character_num", "character_number",
    ))

    table_index = _parse_int_loose(table_text or "")
    anim_id = _parse_int_loose(anim_text or "")
    char_id = _parse_int_loose(char_text or "")
    name = _clean_preset_name(name_text)
    if table_index is None or anim_id is None or char_id is None or not name:
        return None
    return int(char_id), int(table_index), int(anim_id), name


def _parse_assist_csv_list_row(parts: list[str]) -> tuple[int, int, int, str] | None:
    # Current move_id_map_charagnostic.csv layout used by the project:
    #   table/index number, anim/state id, name, ..., ..., ..., char_id
    # Keep a couple of fallbacks so older local copies still work.
    layouts = (
        (0, 1, 2, 6),  # table, anim, name, char_id
        (0, 1, 2, 3),  # table, anim, name, char_id
        (1, 2, 3, 0),  # char_id, table, anim, name
    )
    for table_i, anim_i, name_i, char_i in layouts:
        if max(table_i, anim_i, name_i, char_i) >= len(parts):
            continue
        table_index = _parse_int_loose(parts[table_i])
        anim_id = _parse_int_loose(parts[anim_i])
        char_id = _parse_int_loose(parts[char_i])
        name = _clean_preset_name(parts[name_i])
        if table_index is None or anim_id is None or char_id is None or not name:
            continue
        return int(char_id), int(table_index), int(anim_id), name
    return None


def _load_assist_preset_name_map() -> dict[tuple[int, int], tuple[int, str]]:
    global _ASSIST_PRESET_NAME_CACHE
    if _ASSIST_PRESET_NAME_CACHE is not None:
        return _ASSIST_PRESET_NAME_CACHE

    out: dict[tuple[int, int], tuple[int, str]] = {}

    for path in _candidate_assist_csv_paths():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
                sample = f.read(4096)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample) if sample else csv.excel
                except Exception:
                    dialect = csv.excel

                reader = csv.reader(f, dialect)
                rows = [row for row in reader if row and any(str(cell).strip() for cell in row)]
        except Exception:
            continue

        if not rows:
            continue

        header = rows[0]
        header_norm = {_norm_csv_key(c) for c in header}
        has_header = bool(header_norm & {
            "tableindex", "table", "entry", "number", "animid", "animationid",
            "name", "movename", "charid", "characterid", "cid",
        })

        if has_header:
            for parts in rows[1:]:
                row = {header[i]: parts[i] if i < len(parts) else "" for i in range(len(header))}
                parsed = _parse_assist_csv_dict_row(row)
                if parsed is None:
                    parsed = _parse_assist_csv_list_row(parts)
                if parsed is None:
                    continue
                char_id, table_index, anim_id, name = parsed
                out[(char_id, table_index)] = (anim_id, name)
        else:
            for parts in rows:
                parsed = _parse_assist_csv_list_row(parts)
                if parsed is None:
                    continue
                char_id, table_index, anim_id, name = parsed
                out[(char_id, table_index)] = (anim_id, name)

    _ASSIST_PRESET_NAME_CACHE = out
    return out


def _assist_csv_name(char_id: int | None, table_index: int, aid: int | None = None) -> str | None:
    names = _load_assist_preset_name_map()
    keys: list[tuple[int, int]] = []
    if char_id is not None:
        keys.append((int(char_id), int(table_index)))
    # Universal normals/fallback rows use char id 100 in the CSV.
    keys.append((100, int(table_index)))
    if aid is not None:
        if char_id is not None:
            keys.append((int(char_id), int(aid)))
        keys.append((100, int(aid)))

    for key in keys:
        row = names.get(key)
        if not row:
            continue
        _anim, name = row
        name = _clean_preset_name(name)
        if name:
            return name
    return None


def _lookup_move_name_safe(char_id: int | None, char_name: str | None, value: int | None) -> str | None:
    if value is None:
        return None
    tries: list[tuple] = []
    if char_name:
        tries.append((char_name, int(value)))
    if char_id is not None:
        tries.append((int(value), int(char_id)))
        tries.append((int(char_id), int(value)))
    tries.append((int(value),))

    for args in tries:
        try:
            s = _lookup_move_name(*args)
        except TypeError:
            continue
        except Exception:
            continue
        s = _clean_preset_name(s)
        if s:
            return s
    return None

SELECTOR_TAIL = b"\x37\x32\x20\x3F"
SELECTOR_COMPANION_TAIL = b"\x37\x33\x20\x3F"
STATE_WRAPPER_PREFIX = (
    b"\x34\x04\x00\x20"
    b"\x00\x00\x00\x03"
    b"\x00\x00\x00\x00"
    b"\x04\x01\x02\x3F"
)
STATE_WRAPPER_LEN = 0x16
STATE_ID_OFF = 0x12
STATE_MARKER_OFF = 0x14

# Targeted Tensho descriptor scan. These are the live 0xD0-sized records
# found in MEM2 during Chun assist/Tensho. The animation wrapper at
# 0x90984D52 changes the visual state, but these descriptor records are the
# current best target for X/Y/attach behavior.
TENSHO_DESC_IDS = (0x0112, 0x0113, 0x0114)
TENSHO_DESC_GROUP = 0x011F
TENSHO_DESC_STRIDE = 0xD0
TENSHO_FLOAT_OFFS = (0x34, 0x38, 0x3C, 0x40, 0x44, 0x48)
TENSHO_FLOAT_NAMES = {
    0x34: "f0 maybe X/pos",
    0x38: "f1 maybe Y/pos",
    0x3C: "f2 maybe Z/pos",
    0x40: "f3 maybe rot/vel X",
    0x44: "f4 maybe rot/vel Y",
    0x48: "f5 maybe rot/vel Z",
}

PHRASES: list[tuple[str, bytes]] = [
    ("Cmd 33 03 20 3F", bytes.fromhex("33 03 20 3F")),
    ("Cmd 33 35 20 3F", bytes.fromhex("33 35 20 3F")),
    ("Cmd 33 38 00 20", bytes.fromhex("33 38 00 20")),
    ("Cmd 34 04 00 20", bytes.fromhex("34 04 00 20")),
    ("Cmd 34 3D 00 20", bytes.fromhex("34 3D 00 20")),
    ("Cmd 34 41 00 20", bytes.fromhex("34 41 00 20")),
    ("Cmd 35 0A 00 20", bytes.fromhex("35 0A 00 20")),
    ("Cmd 35 01 20 3F", bytes.fromhex("35 01 20 3F")),
    ("Cmd 35 03 20 3F", bytes.fromhex("35 03 20 3F")),
    ("Cmd 37 32 20 3F", bytes.fromhex("37 32 20 3F")),
    ("Cmd 37 33 20 3F", bytes.fromhex("37 33 20 3F")),
    ("Cmd 04 01 60", bytes.fromhex("04 01 60")),
    ("Cmd 04 17 60", bytes.fromhex("04 17 60")),
]

COLS = [
    ("owner", "Character"),
    ("slot", "CID"),
    ("entry", "Current Assist"),
    ("address", "Selector Address"),
    ("raw", "Raw"),
    ("target", "Target"),
    ("guess", "Action"),
]
COL_IDS = [c[0] for c in COLS]


def _owning_chr_tbl(addr: int) -> int | None:
    best_base = None
    best_dist = None
    for base in _CHR_TBL_BASES:
        if addr < base:
            continue
        dist = addr - base
        if dist > 0x90000:
            continue
        if best_dist is None or dist < best_dist:
            best_base = base
            best_dist = dist
    return best_base


def _slot_index_for_base(base: int | None) -> int | None:
    if base is None:
        return None
    try:
        return _CHR_TBL_BASES.index(base)
    except ValueError:
        return None


def _read_slot_char_ids() -> dict[int, int]:
    result: dict[int, int] = {}
    if rbytes is None:
        return result
    for idx, chr_base in enumerate(_CHR_TBL_BASES):
        fighter_base = _FIGHTER_BASES[idx]
        try:
            b = rbytes(fighter_base + _CHAR_ID_OFF, 4)
            if b and len(b) == 4:
                result[chr_base] = struct.unpack(">I", b)[0]
        except Exception:
            pass
    return result


def _read_fighter_slots() -> list[dict[str, int | str]]:
    slots: list[dict[str, int | str]] = []
    if rbytes is None:
        return slots
    for idx, fighter_base in enumerate(_FIGHTER_BASES):
        try:
            b = rbytes(fighter_base + _CHAR_ID_OFF, 4)
            if not b or len(b) != 4:
                continue
            cid = struct.unpack(">I", b)[0]
        except Exception:
            continue
        if cid <= 0 or cid == 0xFFFFFFFF:
            continue
        label = FIGHTER_SLOT_LABELS[idx] if idx < len(FIGHTER_SLOT_LABELS) else f"F{idx}"
        name = CHAR_ID_TO_KEY.get(cid, f"CID 0x{cid:02X}")
        slots.append({
            "index": idx,
            "fighter_base": fighter_base,
            "char_id": cid,
            "label": label,
            "name": name,
        })
    return slots


def _expand_selector_hits_to_fighter_rows(hits: list[dict]) -> list[dict]:
    """Duplicate each shared character selector row for each loaded fighter using it.

    The selector table is per loaded character, but the UI needs per-fighter
    profiles when both teams load the same character. Rows produced here can
    share the same selector table address while storing different desired words.
    """
    slot_char_ids = _read_slot_char_ids()
    fighters = _read_fighter_slots()
    out: list[dict] = []
    selector_types = {
        "u32-chun-selector",
        "u32-ryu-selector-graft",
        "u32-generic-selector",
        "u32-anchored-assist-fallback",
        "u32-vjoe-trampoline",
        "u32-direct-426-fallback",
    }

    for h in hits:
        if h.get("typ") not in selector_types:
            out.append(h)
            continue

        owner_base = int(h.get("owner_base") or 0) or _owning_chr_tbl(int(h.get("addr", h.get("block", 0))))
        if owner_base is None:
            out.append(h)
            continue
        cid = slot_char_ids.get(owner_base)
        if cid is None:
            out.append(h)
            continue

        matches = [f for f in fighters if int(f.get("char_id", -1)) == int(cid)]
        if not matches:
            nh = dict(h)
            nh["owner_base"] = owner_base
            nh["char_id"] = cid
            out.append(nh)
            continue

        for f in matches:
            nh = dict(h)
            nh["owner_base"] = owner_base
            nh["char_id"] = cid
            nh["fighter_index"] = int(f["index"])
            nh["fighter_base"] = int(f["fighter_base"])
            nh["fighter_label"] = str(f["label"])
            nh["owner"] = f"{f['label']} {f['name']} @0x{int(f['fighter_base']):08X}"
            nh["guess"] = (
                f"double-click to set {f['label']} profile; shared table 0x{int(h.get('block', 0)):08X}"
            )
            out.append(nh)

    def key(row: dict) -> tuple:
        return (int(row.get("fighter_index", 99)), int(row.get("block", 0)), str(row.get("kind", "")))

    out.sort(key=key)
    return out


def _owner_name(addr: int, slot_char_ids: dict[int, int]) -> str:
    base = _owning_chr_tbl(addr)
    if base is None:
        return "?"
    cid = slot_char_ids.get(base)
    name = CHAR_ID_TO_KEY.get(cid, "?") if cid is not None else "?"
    idx = _slot_index_for_base(base)
    slot = f"S{idx}" if idx is not None else "S?"
    return f"{slot} {name} @0x{base:08X}"


def _slot_cid(addr: int, slot_char_ids: dict[int, int]) -> str:
    base = _owning_chr_tbl(addr)
    if base is None:
        return "?"
    cid = slot_char_ids.get(base)
    if cid is None:
        return "?"
    return f"0x{cid:02X}"


def _fmt_context(data: bytes, local: int, radius: int = 20, mark_len: int = 4) -> str:
    start = max(0, local - radius)
    end = min(len(data), local + radius + 24)
    parts: list[str] = []
    for i in range(start, end):
        b = data[i]
        if i == local:
            parts.append(f"[{b:02X}")
        elif i == local + mark_len - 1:
            parts.append(f"{b:02X}]")
        else:
            parts.append(f"{b:02X}")
    return " ".join(parts)


def _u16be(data: bytes, off: int) -> int | None:
    if off < 0 or off + 2 > len(data):
        return None
    return struct.unpack_from(">H", data, off)[0]


def _u32be(data: bytes, off: int) -> int | None:
    if off < 0 or off + 4 > len(data):
        return None
    return struct.unpack_from(">I", data, off)[0]


def _f32be(data: bytes, off: int) -> float | None:
    if off < 0 or off + 4 > len(data):
        return None
    return struct.unpack_from(">f", data, off)[0]


def _fmt_f32(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v:.6g}"


def _raw4(data: bytes, off: int) -> str:
    if off < 0 or off + 4 > len(data):
        return "?"
    return f"0x{_u32be(data, off):08X}"


def _selector_count(data: bytes, idx: int) -> int:
    if idx < 0 or idx + 16 > len(data):
        return 0
    if data[idx + 12:idx + 16] != SELECTOR_TAIL:
        return 0
    return sum(1 for off in (0, 4, 8) if data[idx + off] == 0x00 and data[idx + off + 1] == 0x03)


def _selector_score(data: bytes, idx: int, strict_count: int) -> int:
    score = 30 + strict_count * 15
    if strict_count == 3:
        score += 10
    if idx + 24 <= len(data) and data[idx + 20:idx + 24] == SELECTOR_COMPANION_TAIL:
        score += 15
    window = data[idx:min(len(data), idx + 0x120)]
    for phrase in (b"\x04\x01\x60\x00", b"\x04\x17\x60\x00", b"\x33\x03\x20\x3F", b"\x34\x3D\x00\x20"):
        if phrase in window:
            score += 5
    return min(score, 100)


def _append_nearby_phrases(data: bytes, base_addr: int, block_idx: int, block_addr: int,
                           slot_char_ids: dict[int, int], hits: list[dict], limit: int = 18) -> None:
    owner = _owner_name(block_addr, slot_char_ids)
    slot = _slot_cid(block_addr, slot_char_ids)
    window_start = max(0, block_idx - 0x80)
    window_end = min(len(data), block_idx + 0x180)
    added = 0
    seen: set[tuple[int, str]] = set()
    window = data[window_start:window_end]
    for name, phrase in PHRASES:
        pos = 0
        while added < limit:
            j = window.find(phrase, pos)
            if j < 0:
                break
            pos = j + 1
            local = window_start + j
            addr = base_addr + local
            key = (addr, name)
            if key in seen:
                continue
            seen.add(key)
            hits.append({
                "kind": "phrase",
                "block": block_addr,
                "addr": addr,
                "owner": owner,
                "slot": slot,
                "entry": name,
                "raw": phrase.hex(" ").upper(),
                "target": f"{addr - block_addr:+#x}",
                "guess": "nearby command phrase",
                "score": 40,
                "ctx": _fmt_context(data, local, mark_len=len(phrase)),
                "editable": True,
                "typ": f"raw{len(phrase)}",
            })
            added += 1


def _append_selector_block(data: bytes, base_addr: int, idx: int, slot_char_ids: dict[int, int],
                           hits: list[dict]) -> None:
    block_addr = base_addr + idx
    owner_base = _owning_chr_tbl(block_addr)
    if owner_base is None:
        return
    cnt = _selector_count(data, idx)
    if cnt < 2:
        return
    owner = _owner_name(block_addr, slot_char_ids)
    slot = _slot_cid(block_addr, slot_char_ids)
    score = _selector_score(data, idx, cnt)
    kind = "selector-chain" if cnt == 3 else "selector-loose"
    guess = "Confirmed Ryu selector shape" if block_addr == 0x908C7680 else "Ryu-like selector candidate"

    hits.append({
        "kind": kind,
        "block": block_addr,
        "addr": block_addr,
        "owner": owner,
        "slot": slot,
        "entry": "block",
        "raw": data[idx:idx + 16].hex(" ").upper(),
        "target": "",
        "guess": guess,
        "score": score,
        "ctx": _fmt_context(data, idx, mark_len=16),
        "editable": False,
        "typ": "raw16",
    })

    for n, off in enumerate((0, 4, 8), start=1):
        raw = _u32be(data, idx + off)
        if raw is None:
            continue
        addr = block_addr + off
        as_local = owner_base + raw
        entry_guess = RYU_KNOWN_TARGETS.get(raw, "")
        if not entry_guess and data[idx + off] == 0x00 and data[idx + off + 1] == 0x03:
            entry_guess = "selector word"
        hits.append({
            "kind": "selector",
            "block": block_addr,
            "addr": addr,
            "owner": owner,
            "slot": slot,
            "entry": f"word {n}",
            "raw": f"0x{raw:08X}",
            "target": f"0x{as_local:08X}" if 0 <= raw <= 0x100000 else "",
            "guess": entry_guess,
            "score": score,
            "ctx": _fmt_context(data, idx + off),
            "editable": True,
            "typ": "u32-selector",
        })




def _flat_selector_raw(words: list[int]) -> str:
    if not words:
        return "?"
    if all(w == words[0] for w in words):
        return f"0x{words[0]:08X}"
    return "default"


def _flat_selector_target(owner_base: int | None, words: list[int]) -> str:
    if owner_base is None or not words:
        return ""
    if not all(w == words[0] for w in words):
        return "default"
    raw = words[0]
    selector_base = _runtime_chr_tbl_base_for_owner_base(owner_base) or owner_base
    if 0 <= raw <= 0x100000:
        return f"0x{selector_base + raw:08X}"
    return ""


def _selector_word_to_move_name(owner_base: int | None, raw_word: int, slot_char_ids: dict[int, int]) -> str | None:
    if owner_base is None:
        return None

    cid = slot_char_ids.get(owner_base)
    char_name = CHAR_ID_TO_KEY.get(cid, "") if cid is not None else ""

    # First handle known Ryu native selector words without needing a chr_tbl pass.
    if cid == 12 and raw_word in RYU_KNOWN_TARGETS:
        return RYU_KNOWN_TARGETS.get(raw_word)

    chr_tbl_base = _runtime_chr_tbl_base_for_owner_base(owner_base)
    if chr_tbl_base is None:
        return None

    data = _read_mem_region_raw(chr_tbl_base, MOVE_PRESET_SLOT_SIZE)
    if not data:
        return None

    best: tuple[int, str] | None = None
    for table_index in range(MOVE_PRESET_CHR_TBL_NUM_ENTRIES - 1):
        off = table_index * 4
        entry = _u32be(data, off)
        if entry is None:
            continue
        if entry in (0, 0xFFFFFFFF):
            continue
        if entry % 4 != 0 or entry < MOVE_PRESET_DATA_START_OFF:
            continue
        if entry >= len(data):
            continue

        for delta in MOVE_PRESET_ENTRY_OFFSETS:
            if (entry + delta) != raw_word:
                continue
            aid = _move_preset_anim_id_after_hdr(data, entry)
            name, _source, is_named = _resolve_move_preset_entry_name(table_index, aid, cid, char_name)
            if not name:
                continue
            suffix = "" if delta == 0 else f" +0x{delta:02X}"
            # Prefer named exact matches, then named offset matches, then fallback exact.
            rank = 0
            if is_named and delta == 0:
                rank = 0
            elif is_named:
                rank = 1
            elif delta == 0:
                rank = 2
            else:
                rank = 3
            candidate = (rank, f"{name}{suffix}")
            if best is None or candidate[0] < best[0]:
                best = candidate

    return best[1] if best else None


def _flat_selector_entry_label(owner_base: int | None, words: list[int], slot_char_ids: dict[int, int], fallback: str) -> str:
    if not words:
        return fallback
    names = []
    for w in words:
        nm = _selector_word_to_move_name(owner_base, w, slot_char_ids)
        names.append(nm or f"0x{w:08X}")
    if all(n == names[0] for n in names):
        return names[0]
    return "default"

def _append_chun_selector_graft_table(data: bytes, base_addr: int, idx: int,
                                      slot_char_ids: dict[int, int], hits: list[dict],
                                      source: str) -> None:
    block_addr = base_addr + idx
    owner_base = _owning_chr_tbl(block_addr)
    if owner_base is None:
        return
    if idx < 0 or idx + len(CHUN_SELECTOR_GRAFT_BLOCK) > len(data):
        return

    owner = _owner_name(block_addr, slot_char_ids)
    slot = _slot_cid(block_addr, slot_char_ids)
    current_block = data[idx:idx + len(CHUN_SELECTOR_GRAFT_BLOCK)]

    words: list[int] = []
    for off in CHUN_SELECTOR_WORD_OFFSETS:
        raw = _u32be(data, idx + off)
        default_raw = _u32be(CHUN_SELECTOR_GRAFT_BLOCK, off)
        if raw is None or default_raw is None:
            continue
        # If this is the untouched original Chun block, show the selector words
        # that will be installed by the graft. Installed/fallback rows show live bytes.
        words.append(raw if source != "original" else default_raw)

    if len(words) != len(CHUN_SELECTOR_WORD_OFFSETS):
        return

    first_lane_addr = block_addr + CHUN_SELECTOR_WORD_OFFSETS[0]
    entry = _flat_selector_entry_label(owner_base, words, slot_char_ids, "Chun assist")
    raw_txt = _flat_selector_raw(words)
    target_txt = _flat_selector_target(owner_base, words)

    hits.append({
        "kind": "chun-selector-word",
        "block": block_addr,
        "addr": first_lane_addr,
        "owner": owner,
        "slot": slot,
        "entry": entry,
        "raw": raw_txt,
        "target": target_txt,
        "guess": f"double-click to set Chun fighter profile; table 0x{block_addr:08X}; source {source}",
        "score": 100,
        "ctx": _fmt_context(data, idx, mark_len=len(CHUN_SELECTOR_GRAFT_BLOCK)),
        "editable": True,
        "typ": "u32-chun-selector",
        "source": source,
        "selector_words": words,
        "table_raw": current_block.hex(" ").upper(),
    })

def _scan_chun_selector_graft_tables(data: bytes, base_addr: int,
                                     slot_char_ids: dict[int, int], hits: list[dict]) -> None:
    patterns = [
        ("graft", CHUN_SELECTOR_GRAFT_BLOCK),
        ("original", CHUN_SELECTOR_ORIGINAL_BLOCK),
    ]
    wrapper_sig = STATE_WRAPPER_PREFIX + b"\x00\x00\x01\x12\x01\x3C"
    seen: set[int] = set()

    for source, pattern in patterns:
        pos = 0
        while True:
            idx = data.find(pattern, pos)
            if idx < 0:
                break
            pos = idx + 1
            if idx in seen:
                continue
            if _slot_cid(base_addr + idx, slot_char_ids) != "0x0D":
                continue
            seen.add(idx)
            _append_chun_selector_graft_table(data, base_addr, idx, slot_char_ids, hits, source)

    # Fallback for a graft whose selector words have already been edited: find the
    # live 0112 wrapper and step back to the selector table start.
    pos = 0
    while True:
        wrapper_idx = data.find(wrapper_sig, pos)
        if wrapper_idx < 0:
            break
        pos = wrapper_idx + 1
        idx = wrapper_idx - CHUN_SELECTOR_GRAFT_DELTA_TO_WRAPPER
        if idx < 0 or idx + len(CHUN_SELECTOR_GRAFT_BLOCK) > len(data):
            continue
        if data[idx:idx + 4] != b"\x0F\x06\x00\x27":
            continue
        if idx in seen:
            continue
        if _slot_cid(base_addr + idx, slot_char_ids) != "0x0D":
            continue
        seen.add(idx)
        _append_chun_selector_graft_table(data, base_addr, idx, slot_char_ids, hits, "wrapper-fallback")


def _looks_like_ryu_selector_graft_shape(data: bytes, idx: int) -> bool:
    if idx < 0 or idx + len(RYU_SELECTOR_GRAFT_BLOCK) > len(data):
        return False
    checks = [
        (0x00, bytes.fromhex("0F 06 00 27")),
        (0x04, bytes.fromhex("01 50 00 60")),
        (0x08, bytes.fromhex("00 00 44 B4")),
        (0x0C, bytes.fromhex("00 00 00 03")),
        (0x1C, bytes.fromhex("37 32 20 3F")),
        (0x20, bytes.fromhex("00 00 00 02")),
        (0x24, bytes.fromhex("37 33 20 3F")),
        (0x28, bytes.fromhex("00 00 00 04")),
    ]
    return all(data[idx + off:idx + off + len(sig)] == sig for off, sig in checks)


def _append_ryu_selector_graft_table(data: bytes, base_addr: int, idx: int,
                                     slot_char_ids: dict[int, int], hits: list[dict],
                                     source: str) -> None:
    block_addr = base_addr + idx
    owner_base = _owning_chr_tbl(block_addr)
    if owner_base is None:
        return
    if idx < 0 or idx + len(RYU_SELECTOR_GRAFT_BLOCK) > len(data):
        return

    owner = _owner_name(block_addr, slot_char_ids)
    slot = _slot_cid(block_addr, slot_char_ids)
    current_block = data[idx:idx + len(RYU_SELECTOR_GRAFT_BLOCK)]

    words: list[int] = []
    for off in RYU_SELECTOR_WORD_OFFSETS:
        raw = _u32be(data, idx + off)
        if raw is None:
            continue
        words.append(raw)

    if len(words) != len(RYU_SELECTOR_WORD_OFFSETS):
        return

    first_lane_addr = block_addr + RYU_SELECTOR_WORD_OFFSETS[0]
    entry = _flat_selector_entry_label(owner_base, words, slot_char_ids, "Ryu assist")
    raw_txt = _flat_selector_raw(words)
    target_txt = _flat_selector_target(owner_base, words)

    hits.append({
        "kind": "ryu-selector-word",
        "block": block_addr,
        "addr": first_lane_addr,
        "owner": owner,
        "slot": slot,
        "entry": entry,
        "raw": raw_txt,
        "target": target_txt,
        "guess": f"double-click to set Ryu fighter profile; table 0x{block_addr:08X}; source {source}",
        "score": 100,
        "ctx": _fmt_context(data, idx, mark_len=len(RYU_SELECTOR_GRAFT_BLOCK)),
        "editable": True,
        "typ": "u32-ryu-selector-graft",
        "source": source,
        "selector_words": words,
        "table_raw": current_block.hex(" ").upper(),
    })

def _scan_ryu_selector_graft_tables(data: bytes, base_addr: int,
                                    slot_char_ids: dict[int, int], hits: list[dict]) -> None:
    seen: set[int] = set()

    pos = 0
    while True:
        idx = data.find(RYU_SELECTOR_GRAFT_BLOCK, pos)
        if idx < 0:
            break
        pos = idx + 1
        if idx in seen:
            continue
        if _slot_cid(base_addr + idx, slot_char_ids) != "0x0C":
            continue
        seen.add(idx)
        _append_ryu_selector_graft_table(data, base_addr, idx, slot_char_ids, hits, "graft")

    # Fallback for already-mutated selector words: match the fixed setup/tail
    # bytes and ignore the three lane dwords.
    pos = 0
    while True:
        idx = data.find(b"\x0F\x06\x00\x27", pos)
        if idx < 0:
            break
        pos = idx + 1
        if idx in seen:
            continue
        if _slot_cid(base_addr + idx, slot_char_ids) != "0x0C":
            continue
        if not _looks_like_ryu_selector_graft_shape(data, idx):
            continue
        seen.add(idx)
        _append_ryu_selector_graft_table(data, base_addr, idx, slot_char_ids, hits, "shape-fallback")



def _is_known_chun_or_ryu_slot(addr: int, slot_char_ids: dict[int, int]) -> bool:
    base = _owning_chr_tbl(addr)
    if base is None:
        return False
    return slot_char_ids.get(base) in (12, 13)


def _looks_like_generic_native_selector_shape(data: bytes, idx: int) -> bool:
    if idx < 0 or idx + GENERIC_SELECTOR_TABLE_LEN > len(data):
        return False
    if data[idx:idx + 0x10] != GENERIC_SELECTOR_SETUP_PREFIX:
        return False
    if data[idx + 0x1C:idx + 0x20] != SELECTOR_TAIL:
        return False
    if data[idx + 0x24:idx + 0x28] != SELECTOR_COMPANION_TAIL:
        return False

    # Require three plausible local selector words. Most known words are slot-local
    # offsets and sit below the 0x90000 character window.
    for off in GENERIC_SELECTOR_WORD_OFFSETS:
        raw = _u32be(data, idx + off)
        if raw is None or raw <= 0 or raw > 0x90000:
            return False
    return True


def _looks_like_generic_graft_candidate_shape(data: bytes, idx: int) -> bool:
    if idx < 0 or idx + GENERIC_SELECTOR_TABLE_LEN > len(data):
        return False
    if data[idx:idx + 4] != b"\x0F\x06\x00\x27":
        return False
    if data[idx + 4:idx + 8] != SELECTOR_TAIL:
        return False
    if data[idx + 0x0C:idx + 0x10] != SELECTOR_COMPANION_TAIL:
        return False

    # Chun's confirmed graft site had three 01 3C rows after 37 33. Keep the
    # requirement soft enough for other characters: at least two 01 3C rows in
    # the next 0x28 bytes.
    rows = 0
    for off in (0x14, 0x1C, 0x24):
        if data[idx + off:idx + off + 4] == b"\x01\x3C\x00\x00":
            rows += 1
    return rows >= 2


def _build_generic_selector_graft_block(words: tuple[int, int, int], arg32: bytes | None = None,
                                        arg33: bytes | None = None) -> bytes:
    if arg32 is None or len(arg32) != 4:
        arg32 = GENERIC_SELECTOR_DEFAULT_ARG_32
    if arg33 is None or len(arg33) != 4:
        arg33 = GENERIC_SELECTOR_DEFAULT_ARG_33
    out = bytearray()
    out.extend(GENERIC_SELECTOR_SETUP_PREFIX)
    for word in words:
        out.extend(struct.pack(">I", word & 0xFFFFFFFF))
    out.extend(SELECTOR_TAIL)
    out.extend(arg32)
    out.extend(SELECTOR_COMPANION_TAIL)
    out.extend(arg33)
    return bytes(out)


def _generic_candidate_score(data: bytes, idx: int, source: str) -> int:
    score = 0
    base_source = (
        str(source or "")
        .replace("+assist-route", "")
        .replace("-assist-route", "")
    )

    if base_source in ("native", "installed"):
        score += 120
    elif base_source == "graft-candidate":
        score += 95
    elif base_source == "tail-pair":
        score += 70
    else:
        score += 40

    window = data[idx:min(len(data), idx + 0xC0)]
    score += min(window.count(b"\x01\x3C\x00\x00"), 4) * 6
    for sig in (
        b"\x04\x17\x60\x00",
        b"\x04\x01\x60\x00",
        b"\x04\x01\x02\x3F",
        b"\x33\x35\x20\x3F",
    ):
        if sig in window:
            score += 4
    return score


def _generic_assist_route_ranges_for_owner_base(owner_base: int | None) -> list[tuple[int, int, int]]:
    """Return broad memory windows around this character's assist route entries."""
    if owner_base is None:
        return []

    chr_tbl_base = _runtime_chr_tbl_base_for_owner_base(owner_base) or owner_base
    table_size = (max(GENERIC_ASSIST_ROUTE_TABLE_INDICES) + 1) * 4
    table_data = _read_mem_region_raw(chr_tbl_base, table_size)
    if not table_data or len(table_data) < table_size:
        return []

    ranges: list[tuple[int, int, int]] = []
    for table_index in GENERIC_ASSIST_ROUTE_TABLE_INDICES:
        off = table_index * 4
        entry = _u32be(table_data, off)
        if entry is None:
            continue
        if entry in (0, 0xFFFFFFFF):
            continue
        if entry <= 0 or entry >= MOVE_PRESET_SLOT_SIZE:
            continue

        entry_addr = chr_tbl_base + entry
        ranges.append((
            entry_addr - GENERIC_ASSIST_ROUTE_BEFORE,
            entry_addr + GENERIC_ASSIST_ROUTE_AFTER,
            table_index,
        ))

    return ranges


def _generic_assist_route_bonus(owner_base: int | None, block_addr: int) -> int:
    """Score boost for generic selector candidates near assist route entries."""
    best = 0
    for start, end, table_index in _generic_assist_route_ranges_for_owner_base(owner_base):
        if start <= block_addr <= end:
            entry_addr = start + GENERIC_ASSIST_ROUTE_BEFORE
            dist = abs(block_addr - entry_addr)
            bonus = GENERIC_ASSIST_ROUTE_SCORE_BONUS - min(dist // 0x20, 80)
            if bonus > best:
                best = bonus
    return max(0, best)



def _append_generic_selector_table(data: bytes, base_addr: int, idx: int,
                                   slot_char_ids: dict[int, int], hits: list[dict],
                                   source: str) -> None:
    """Append one flattened generic assist selector row."""
    block_addr = base_addr + idx
    owner_base = _owning_chr_tbl(block_addr)
    if owner_base is None:
        return
    if idx < 0 or idx + GENERIC_SELECTOR_TABLE_LEN > len(data):
        return

    owner = _owner_name(block_addr, slot_char_ids)
    slot = _slot_cid(block_addr, slot_char_ids)
    current_block = data[idx:idx + GENERIC_SELECTOR_TABLE_LEN]
    is_native = source in ("native", "installed") and current_block.startswith(GENERIC_SELECTOR_SETUP_PREFIX)

    if is_native:
        words: list[int] = []
        for off in GENERIC_SELECTOR_WORD_OFFSETS:
            raw = _u32be(data, idx + off)
            if raw is None:
                return
            words.append(raw)
    else:
        # Deliberately mixed so the flat UI says default until a preset/custom
        # assist word is written.
        words = [0, 1, 2]

    first_lane_addr = block_addr + GENERIC_SELECTOR_WORD_OFFSETS[0]
    entry = _flat_selector_entry_label(owner_base, words, slot_char_ids, "Generic assist")
    raw_txt = _flat_selector_raw(words)
    target_txt = _flat_selector_target(owner_base, words)

    assist_bonus = _generic_assist_route_bonus(owner_base, block_addr)
    source_label = str(source or "unknown")
    if assist_bonus and "assist-route" not in source_label:
        source_label = f"{source_label}+assist-route"

    score = _generic_candidate_score(data, idx, source_label) + assist_bonus

    # If this was only found by the loose assist-route tail-pair fallback and
    # is not already a real selector setup, do not pretend it is a selector
    # table. Treat it as a generic anchored assist-wrapper fallback.
    base_source_label = (
        source_label
        .replace("+assist-route", "")
        .replace("-assist-route", "")
    )

    # Important: only the loose tail-pair assist-route fallback should become
    # anchored. Real generic selector/graft candidates may also sit near the
    # assist route, and those were the working path for older characters.
    # Misclassifying graft-candidate+assist-route as anchored makes those
    # characters stop responding to selector lane writes.
    anchored_fallback = (
        base_source_label == "tail-pair"
        and "assist-route" in source_label
        and not current_block.startswith(GENERIC_SELECTOR_SETUP_PREFIX)
    )

    typ = "u32-anchored-assist-fallback" if anchored_fallback else "u32-generic-selector"
    action = (
        f"double-click to change anchored assist-wrapper fallback; keeps chr_tbl[{ANCHOR_ASSIST_FALLBACK_TABLE_INDEX}] on wrapper"
        if anchored_fallback
        else f"double-click to change generic assist; table 0x{block_addr:08X}; source {source_label}"
    )

    anchor_default_word = _chr_tbl_word_for_owner_base(owner_base, ANCHOR_ASSIST_FALLBACK_TABLE_INDEX)
    anchor_operands = (
        _anchored_assist_operands_for_owner_base(
            owner_base,
            ANCHOR_ASSIST_FALLBACK_TABLE_INDEX,
            anchor_default_word,
        )
        if anchored_fallback
        else []
    )

    row_addr = first_lane_addr
    if anchored_fallback and anchor_default_word is not None and anchor_operands:
        anchor_base = _chr_tbl_base_for_owner_base(owner_base)
        if anchor_base is not None:
            row_addr = anchor_base + int(anchor_default_word) + int(anchor_operands[0][0])

    hits.append({
        "kind": "generic-selector-word",
        "block": block_addr,
        "addr": row_addr,
        "owner": owner,
        "slot": slot,
        "entry": entry,
        "raw": raw_txt,
        "target": target_txt,
        "guess": action,
        "score": score,
        "ctx": _fmt_context(data, idx, mark_len=GENERIC_SELECTOR_TABLE_LEN),
        "editable": True,
        "typ": typ,
        "anchor_table_index": ANCHOR_ASSIST_FALLBACK_TABLE_INDEX,
        "anchor_default_word": anchor_default_word if anchor_default_word is not None else 0,
        "anchor_operand_offsets": [off for off, _word in anchor_operands],
        "anchor_operand_words": [word for _off, word in anchor_operands],
        "source": source_label,
        "selector_words": words,
        "table_raw": current_block.hex(" ").upper(),
    })



def _looks_like_generic_tail_pair_candidate(data: bytes, tail_idx: int, allow_single_row: bool = False) -> bool:
    """Loose fallback: 37 32 / 37 33 pair with nearby route rows.

    Normal generic scanning still wants two 01 3C rows. Assist-route scanning
    allows one row because some assist routes have a thinner tail shape.
    """
    if tail_idx < 0 or tail_idx + 0x30 > len(data):
        return False
    if data[tail_idx:tail_idx + 4] != SELECTOR_TAIL:
        return False
    if data[tail_idx + 0x08:tail_idx + 0x0C] != SELECTOR_COMPANION_TAIL:
        return False

    rows = 0
    for off in range(0x10, 0x58, 4):
        if data[tail_idx + off:tail_idx + off + 4] == b"\x01\x3C\x00\x00":
            rows += 1

    min_rows = 1 if allow_single_row else 2
    if rows < min_rows:
        return False

    window = data[max(0, tail_idx - 0x70):min(len(data), tail_idx + 0x90)]
    return any(sig in window for sig in (
        b"\x0F\x06\x00\x27",
        b"\x04\x17\x60\x00",
        b"\x04\x01\x60\x00",
        b"\x04\x01\x02\x3F",
        b"\x33\x35\x20\x3F",
        b"\x34\x04\x00\x20",
    ))


def _scan_generic_selector_candidates(data: bytes, base_addr: int,
                                      slot_char_ids: dict[int, int], hits: list[dict]) -> None:
    seen: set[int] = set()

    pos = 0
    while True:
        idx = data.find(b"\x0F\x06\x00\x27", pos)
        if idx < 0:
            break
        pos = idx + 1
        if idx in seen:
            continue
        block_addr = base_addr + idx
        if _is_known_chun_or_ryu_slot(block_addr, slot_char_ids):
            continue
        if _looks_like_generic_native_selector_shape(data, idx):
            seen.add(idx)
            _append_generic_selector_table(data, base_addr, idx, slot_char_ids, hits, "native")
            continue
        if _looks_like_generic_graft_candidate_shape(data, idx):
            seen.add(idx)
            _append_generic_selector_table(data, base_addr, idx, slot_char_ids, hits, "graft-candidate")

    # Experimental fallback: align a synthetic graft 0x1C bytes before a
    # 37 32 / 37 33 tail pair. If the candidate sits inside an assist route,
    # allow thinner one-row shapes and classify them as anchored fallback later.
    pos = 0
    while True:
        tail_idx = data.find(SELECTOR_TAIL, pos)
        if tail_idx < 0:
            break
        pos = tail_idx + 1
        idx = tail_idx - 0x1C
        if idx < 0 or idx in seen:
            continue

        block_addr = base_addr + idx
        if _is_known_chun_or_ryu_slot(block_addr, slot_char_ids):
            continue

        owner_base = _owning_chr_tbl(block_addr)
        assist_route = bool(_generic_assist_route_bonus(owner_base, block_addr))
        if not _looks_like_generic_tail_pair_candidate(data, tail_idx, allow_single_row=assist_route):
            continue

        seen.add(idx)
        source = "tail-pair+assist-route" if assist_route else "tail-pair"
        _append_generic_selector_table(data, base_addr, idx, slot_char_ids, hits, source)



def _read_mem_region_raw(start_addr: int, size: int) -> bytes:
    if rbytes is None:
        return b""
    out = bytearray()
    off = 0
    while off < size:
        n = min(SCAN_BLOCK, size - off)
        try:
            chunk = rbytes(start_addr + off, n) or b""
        except Exception:
            return b""
        if len(chunk) != n:
            return b""
        out.extend(chunk)
        off += n
    return bytes(out)


def _looks_like_chr_tbl_at(data: bytes, idx: int) -> bool:
    """Validate a chr_tbl start inside a local memory blob.

    Most tables start with 0x3600, but Volnutt-style loaded tables can start
    at nearby aligned headers such as 0x3610.  Require the normal chr_act
    marker so this loose header band does not grab random script data.
    """
    if idx < 0 or idx + 0xB0C > len(data):
        return False
    try:
        first = _u32be(data, idx)
        if first is None or not (0x3400 <= int(first) <= 0x3800 and (int(first) & 0xF) == 0):
            return False
        if _u32be(data, idx + 0xB00) != 0xFFFFFFFF:
            return False
    except Exception:
        return False
    return data[idx + 0xB04:idx + 0xB0C] == b"chr_act\n"


def _find_runtime_chr_tbl_base_for_region(region_base: int) -> int | None:
    """Find the actual live chr_tbl base inside one broad character slot region.

    _CHR_TBL_BASES are stable ownership windows, but the actual chr_tbl
    can sit deeper in the region after character load. Selector words are
    relative to this actual chr_tbl base, not always the ownership window.
    """
    if rbytes is None:
        return None

    # Include bytes before region_base because a chr_tbl label lives at base-0x18
    # when the table starts exactly at the region base.
    next_bases = [b for b in _CHR_TBL_BASES if b > region_base]
    slot_end = min(region_base + MOVE_PRESET_SLOT_SIZE, min(next_bases) if next_bases else region_base + MOVE_PRESET_SLOT_SIZE)

    scan_start = max(SCAN_START, region_base - CHR_TBL_PRE_START_BACK)
    scan_size = max(0, slot_end - scan_start)
    data = _read_mem_region_raw(scan_start, scan_size)
    if not data:
        return None

    label = b"chr_tbl\n"
    pos = 0
    candidates: list[int] = []
    while True:
        j = data.find(label, pos)
        if j < 0:
            break
        pos = j + 1
        tbl_idx = j + 0x18
        if not _looks_like_chr_tbl_at(data, tbl_idx):
            continue
        cand = scan_start + tbl_idx
        if region_base - CHR_TBL_PRE_START_BACK <= cand < slot_end:
            candidates.append(cand)

    if not candidates:
        # Very old/static layouts can still have the table exactly at the
        # ownership base with no readable label in this pass.
        base_idx = region_base - scan_start
        if _looks_like_chr_tbl_at(data, base_idx):
            return region_base
        return None

    # Prefer the first validated table in this ownership window.
    candidates.sort()
    return candidates[0]


def _find_runtime_chr_tbl_bases_for_region(region_base: int) -> list[int]:
    """Find every plausible live chr_tbl base inside one broad character slot.

    Some loaded slots expose more than one valid chr_tbl-shaped table, and some
    slots do not satisfy the strict chr_tbl label path during assist state. The
    preset picker only needs valid chr_tbl entry offsets, so collect every
    validated candidate and let the picker harvest from all of them.
    """
    if rbytes is None:
        return []

    next_bases = [b for b in _CHR_TBL_BASES if b > region_base]
    slot_end = min(region_base + MOVE_PRESET_SLOT_SIZE, min(next_bases) if next_bases else region_base + MOVE_PRESET_SLOT_SIZE)

    scan_start = max(SCAN_START, region_base - CHR_TBL_PRE_START_BACK)
    scan_size = max(0, slot_end - scan_start)
    data = _read_mem_region_raw(scan_start, scan_size)
    if not data:
        return []

    candidates: list[int] = []

    def add_candidate(tbl_idx: int) -> None:
        if not _looks_like_chr_tbl_at(data, tbl_idx):
            return
        cand = scan_start + tbl_idx
        if region_base - CHR_TBL_PRE_START_BACK <= cand < slot_end:
            candidates.append(cand)

    # Preferred path: the normal chr_tbl label sits 0x18 bytes before the table.
    label = b"chr_tbl\n"
    pos = 0
    while True:
        j = data.find(label, pos)
        if j < 0:
            break
        pos = j + 1
        add_candidate(j + 0x18)

    # Backup path: scan the slot for the actual table shape, not just the label.
    # This fixes cases where the selector lane is live but the label path misses
    # the loaded chr_tbl for that slot.
    scan_limit = len(data) - 0xB0C
    for tbl_idx in range(0, max(0, scan_limit), 4):
        try:
            if _u32be(data, tbl_idx) != MOVE_PRESET_DATA_START_OFF:
                continue
            if _u32be(data, tbl_idx + 0xB00) != 0xFFFFFFFF:
                continue
            if data[tbl_idx + 0xB04:tbl_idx + 0xB0C] != b"chr_act\n":
                continue
        except Exception:
            continue
        add_candidate(tbl_idx)

    # Very old/static layouts can still have the table exactly at the ownership
    # base with no readable label in this pass.
    add_candidate(region_base - scan_start)

    def candidate_rank(cand: int) -> tuple[int, int, int]:
        # The backward scan can see the previous character's spillover table.
        # Prefer tables at/after this owner base; only use pre-base tables when
        # there is no table after the owner. This fixes Tekkaman being resolved
        # to the prior slot's chr_tbl while still allowing true pre-base tables.
        if int(cand) == int(region_base):
            band = 0
        elif int(cand) > int(region_base):
            band = 1
        else:
            band = 2
        return (band, abs(int(cand) - int(region_base)), int(cand))

    out: list[int] = []
    seen: set[int] = set()
    for cand in sorted(candidates, key=candidate_rank):
        if cand in seen:
            continue
        seen.add(cand)
        out.append(cand)
    return out


def _runtime_chr_tbl_base_for_owner_base(owner_base: int | None) -> int | None:
    if owner_base is None:
        return None
    bases = _find_runtime_chr_tbl_bases_for_region(owner_base)
    return bases[0] if bases else None


def _selector_base_for_address(addr: int) -> int | None:
    owner_base = _owning_chr_tbl(addr)
    if owner_base is None:
        return None
    return _runtime_chr_tbl_base_for_owner_base(owner_base)


def _chr_tbl_word_for_owner_base(owner_base: int | None, table_index: int) -> int | None:
    if owner_base is None:
        return None

    chr_tbl_base = _runtime_chr_tbl_base_for_owner_base(owner_base)
    if chr_tbl_base is None:
        chr_tbl_base = int(owner_base)

    data = _read_mem_region_raw(chr_tbl_base + (int(table_index) * 4), 4)
    if not data or len(data) != 4:
        return None

    return struct.unpack(">I", data)[0]


def _chr_tbl_base_for_owner_base(owner_base: int | None) -> int | None:
    if owner_base is None:
        return None
    chr_tbl_base = _runtime_chr_tbl_base_for_owner_base(owner_base)
    if chr_tbl_base is None:
        chr_tbl_base = int(owner_base)
    return chr_tbl_base


def _anchored_assist_operands_for_owner_base(
    owner_base: int | None,
    table_index: int = ANCHOR_ASSIST_FALLBACK_TABLE_INDEX,
    default_word: int | None = None,
) -> list[tuple[int, int]]:
    """Find internal wrapper call operands for the generic anchored fallback.

    The returned tuples are (operand_offset_from_wrapper, original_word). Only
    operands immediately following known local call opcodes are included. This
    avoids replacing chr_tbl[426] and preserves the wrapper's positioning work.
    """
    chr_tbl_base = _chr_tbl_base_for_owner_base(owner_base)
    if chr_tbl_base is None:
        return []

    if default_word is None:
        default_word = _chr_tbl_word_for_owner_base(owner_base, table_index)
    if default_word is None:
        return []

    wrapper_addr = chr_tbl_base + (int(default_word) & 0xFFFFFFFF)
    data = _read_mem_region_raw(wrapper_addr, ANCHOR_ASSIST_WRAPPER_SCAN_SIZE)
    if not data:
        return []

    out: list[tuple[int, int]] = []
    seen: set[int] = set()
    limit = max(0, len(data) - 8)
    for off in range(0, limit + 1, 4):
        cmd = data[off:off + 4]
        if cmd not in ANCHOR_ASSIST_CALL_OPS:
            continue

        operand_off = off + 4
        val = _u32be(data, operand_off)
        if val is None:
            continue
        if val % 4 != 0:
            continue
        if not (ANCHOR_ASSIST_OPERAND_MIN <= val < ANCHOR_ASSIST_OPERAND_MAX):
            continue
        if default_word is not None and val == int(default_word):
            continue
        if operand_off in seen:
            continue
        seen.add(operand_off)
        out.append((operand_off, val))

    def rank(item: tuple[int, int]) -> tuple[int, int]:
        off, val = item
        # Prefer calls that stay in the same broad wrapper/body neighborhood,
        # then earlier calls in the wrapper. This is generic; it does not rely
        # on a character id.
        if default_word is None:
            band = 1
        else:
            band = 0 if abs(int(val) - int(default_word)) <= 0x10000 else 1
        return (band, off)

    out.sort(key=rank)
    return out[:ANCHOR_ASSIST_MAX_OPERANDS]


def _anchored_assist_fallback_writes(
    owner_base: int | None,
    raw_val: int | None,
    table_index: int = ANCHOR_ASSIST_FALLBACK_TABLE_INDEX,
    default_word: int | None = None,
    operand_offsets: list[int] | tuple[int, ...] | None = None,
    operand_words: list[int] | tuple[int, ...] | None = None,
) -> dict[int, bytes]:
    """Generic wrapper-preserving fallback writes.

    raw_val=None restores the original wrapper entry and original internal
    operands. Non-default values keep chr_tbl[table_index] on the wrapper and
    redirect internal wrapper call operands to the selected move word.
    """
    chr_tbl_base = _chr_tbl_base_for_owner_base(owner_base)
    if chr_tbl_base is None:
        return {}

    if default_word is None:
        default_word = _chr_tbl_word_for_owner_base(owner_base, table_index)
    if default_word is None:
        return {}

    default_word = int(default_word) & 0xFFFFFFFF
    table_entry_addr = chr_tbl_base + (int(table_index) * 4)
    wrapper_addr = chr_tbl_base + default_word

    writes: dict[int, bytes] = {
        table_entry_addr: struct.pack(">I", default_word),
    }

    if operand_offsets is None or not list(operand_offsets):
        pairs = _anchored_assist_operands_for_owner_base(owner_base, table_index, default_word)
    else:
        words = list(operand_words or [])
        pairs = []
        for i, off in enumerate(list(operand_offsets)):
            old_word = int(words[i]) if i < len(words) else 0
            pairs.append((int(off), old_word))

    if raw_val is None:
        for off, old_word in pairs:
            if old_word:
                writes[wrapper_addr + int(off)] = struct.pack(">I", int(old_word) & 0xFFFFFFFF)
        return writes

    if not pairs:
        return {}

    word = int(raw_val) & 0xFFFFFFFF
    for off, _old_word in pairs:
        writes[wrapper_addr + int(off)] = struct.pack(">I", word)
    return writes



def _is_vjoe_owner_base(owner_base: int | None, slot_char_ids: dict[int, int] | None = None) -> bool:
    if owner_base is None:
        return False
    if slot_char_ids is None:
        slot_char_ids = _read_slot_char_ids()
    return int(slot_char_ids.get(int(owner_base), -1)) == VJOE_CHAR_ID


def _vjoe_find_attack_state_wrapper(wrapper_data: bytes) -> int | None:
    """Find VJoe's confirmed live attack animation wrapper inside chr_tbl[426]."""
    start = max(0, VJOE_TRAMPOLINE_STATE_SEARCH_START)
    end = min(len(wrapper_data) - STATE_WRAPPER_LEN, VJOE_TRAMPOLINE_STATE_SEARCH_END)
    if end <= start:
        return None

    candidates: list[tuple[int, int]] = []
    for idx in range(start, end + 1):
        if wrapper_data[idx:idx + len(STATE_WRAPPER_PREFIX)] != STATE_WRAPPER_PREFIX:
            continue
        if wrapper_data[idx + STATE_MARKER_OFF:idx + STATE_MARKER_OFF + 2] != b"\x01\x3C":
            continue
        state_id = _u16be(wrapper_data, idx + STATE_ID_OFF)
        cont = _u32be(wrapper_data, idx + VJOE_TRAMPOLINE_CONT_OFF_FROM_STATE_WRAPPER)
        if state_id is None or not (0x0001 <= state_id <= 0x0500):
            continue
        if cont is None or cont <= 0 or cont >= MOVE_PRESET_SLOT_SIZE:
            continue
        # Prefer the early attack wrapper, not assist taunt/leave wrappers.
        if state_id in (0x01A1, 0x01A8, 0x01AE):
            continue
        candidates.append((idx, state_id))

    if not candidates:
        return None

    # The confirmed VJoe attack state is the first state wrapper after the
    # selector/setup region. In current dumps it is around wrapper+0x19C.
    candidates.sort(key=lambda item: item[0])
    return candidates[0][0]


def _vjoe_find_assist_taunt_payload_off(wrapper_data: bytes) -> int | None:
    """Find the payload offset for VJoe's assist-taunt block, if present.

    This is diagnostic/restore metadata. The trampoline does not jump here by
    default; it redirects to the selected move. But exposing this lets us know
    where the original cleanup family lives.
    """
    # VJoe's assist taunt block in dumps begins:
    #   0F060027 04016000 000001E8 ... 000001A8 013C0000 ...
    sig = bytes.fromhex(
        "0F 06 00 27 "
        "04 01 60 00 00 00 01 E8 "
        "3F 00 00 00 00 00 01 A8 "
        "01 3C"
    )
    pos = wrapper_data.find(sig)
    if pos >= 0:
        return pos + 4

    # Softer fallback: find a state wrapper/command area that contains 0x01A8.
    needle = b"\x00\x00\x01\xA8\x01\x3C"
    pos = wrapper_data.find(needle)
    if pos >= 0:
        # Usually the taunt block has a 0F060027 four bytes before its payload.
        # Return the payload start if that prefix is present, otherwise the
        # state field itself is still useful for diagnostics.
        start = max(0, pos - 0x10)
        rel = wrapper_data[start:pos].rfind(b"\x0F\x06\x00\x27")
        if rel >= 0:
            return start + rel + 4
        return pos

    return None


def _vjoe_table_index_for_selector_word(owner_base: int | None, raw_val: int | None) -> int | None:
    if owner_base is None or raw_val is None:
        return None

    active = _vjoe_active_selector_info_for_owner_base(owner_base) or {}
    chr_tbl_base = int(active.get("chr_tbl_base") or 0)
    if not chr_tbl_base:
        chr_tbl_base = _chr_tbl_base_for_owner_base(owner_base) or 0
    if not chr_tbl_base:
        return None

    table_data = _read_mem_region_raw(chr_tbl_base, MOVE_PRESET_CHR_TBL_NUM_ENTRIES * 4)
    if not table_data:
        return None

    raw = int(raw_val) & 0xFFFFFFFF
    for table_index in range(MOVE_PRESET_CHR_TBL_NUM_ENTRIES):
        entry = _u32be(table_data, table_index * 4)
        if entry is None:
            continue
        for delta in MOVE_PRESET_ENTRY_OFFSETS:
            if ((int(entry) + int(delta)) & 0xFFFFFFFF) == raw:
                return table_index
    return None


def _vjoe_state_id_for_selector_word(owner_base: int | None, raw_val: int | None) -> int | None:
    table_index = _vjoe_table_index_for_selector_word(owner_base, raw_val)
    if table_index in VJOE_TRAMPOLINE_STATE_BY_TABLE_INDEX:
        return VJOE_TRAMPOLINE_STATE_BY_TABLE_INDEX[int(table_index)]

    # Generic fallback: if the selected target itself contains a normal state
    # wrapper early, borrow that state id. Many VJoe projectile scripts do not,
    # so this intentionally returns None for those.
    chr_tbl_base = _chr_tbl_base_for_owner_base(owner_base)
    if chr_tbl_base is None or raw_val is None:
        return None
    move_addr = chr_tbl_base + (int(raw_val) & 0xFFFFFFFF)
    data = _read_mem_region_raw(move_addr, 0x400)
    if not data:
        return None
    idx = data.find(STATE_WRAPPER_PREFIX)
    if idx >= 0 and idx + STATE_ID_OFF + 2 <= len(data):
        sid = _u16be(data, idx + STATE_ID_OFF)
        if sid is not None and 0x0001 <= sid <= 0x0500:
            return sid
    return None


def _vjoe_trampoline_info_for_owner_base(owner_base: int | None) -> dict[str, int] | None:
    """Resolve VJoe's live assist wrapper and confirmed continuation field."""
    if owner_base is None:
        return None
    if not _is_vjoe_owner_base(owner_base):
        return None

    chr_tbl_base = _chr_tbl_base_for_owner_base(owner_base)
    if chr_tbl_base is None:
        return None

    default_word = _chr_tbl_word_for_owner_base(owner_base, ANCHOR_ASSIST_FALLBACK_TABLE_INDEX)
    if default_word is None or default_word <= 0 or default_word >= MOVE_PRESET_SLOT_SIZE:
        return None

    wrapper_addr = int(chr_tbl_base) + (int(default_word) & 0xFFFFFFFF)
    wrapper_data = _read_mem_region_raw(wrapper_addr, VJOE_TRAMPOLINE_SCAN_SIZE)
    if not wrapper_data:
        return None

    state_off = _vjoe_find_attack_state_wrapper(wrapper_data)
    if state_off is None:
        return None

    state_addr = wrapper_addr + state_off + VJOE_TRAMPOLINE_STATE_FIELD_OFF
    cont_addr = wrapper_addr + state_off + VJOE_TRAMPOLINE_CONT_OFF_FROM_STATE_WRAPPER
    orig_state = _u16be(wrapper_data, state_off + VJOE_TRAMPOLINE_STATE_FIELD_OFF)
    orig_cont = _u32be(wrapper_data, state_off + VJOE_TRAMPOLINE_CONT_OFF_FROM_STATE_WRAPPER)
    if orig_state is None or orig_cont is None:
        return None

    taunt_payload_off = _vjoe_find_assist_taunt_payload_off(wrapper_data)

    return {
        "owner_base": int(owner_base),
        "chr_tbl_base": int(chr_tbl_base),
        "table_index": ANCHOR_ASSIST_FALLBACK_TABLE_INDEX,
        "default_word": int(default_word) & 0xFFFFFFFF,
        "wrapper_addr": int(wrapper_addr),
        "state_wrapper_off": int(state_off),
        "state_addr": int(state_addr),
        "orig_state": int(orig_state) & 0xFFFF,
        "cont_addr": int(cont_addr),
        "orig_cont": int(orig_cont) & 0xFFFFFFFF,
        "taunt_payload_off": int(taunt_payload_off) if taunt_payload_off is not None else 0,
    }




def _vjoe_candidate_chr_tbl_bases_global() -> list[int]:
    """Return every validated chr_tbl base in the broad character windows.

    VJoe is the character that exposed the flaw in using one static ownership
    window.  His active table can sit in spillover space while stale/secondary
    VJoe-shaped tables remain readable elsewhere.  This pass deliberately
    de-duplicates all valid chr_tbl shapes across all broad windows.
    """
    if rbytes is None:
        return []

    out: list[int] = []
    seen: set[int] = set()
    for region_base in _CHR_TBL_BASES:
        data = _read_mem_region_raw(region_base, MOVE_PRESET_SLOT_SIZE)
        if not data:
            continue

        def add_idx(idx: int) -> None:
            if not _looks_like_chr_tbl_at(data, idx):
                return
            cand = int(region_base) + int(idx)
            if cand in seen:
                return
            seen.add(cand)
            out.append(cand)

        pos = 0
        while True:
            j = data.find(b"chr_tbl\n", pos)
            if j < 0:
                break
            pos = j + 1
            add_idx(j + 0x18)

        scan_limit = max(0, len(data) - 0xB0C)
        for idx in range(0, scan_limit, 4):
            # Cheap prefilter before the full marker validation.
            first = _u32be(data, idx)
            if first is None or not (0x3400 <= int(first) <= 0x3800 and (int(first) & 0xF) == 0):
                continue
            add_idx(idx)

    out.sort()
    return out


def _vjoe_chr_tbl_shape_score(chr_tbl_base: int) -> tuple[int, dict[str, int]] | None:
    """Score one chr_tbl candidate as a live VJoe assist table."""
    data = _read_mem_region_raw(chr_tbl_base, MOVE_PRESET_SLOT_SIZE)
    if not data:
        return None

    def entry(table_index: int) -> int | None:
        return _u32be(data, table_index * 4)

    move_entries = [entry(i) for i in range(304, 310)]
    if any(v is None for v in move_entries):
        return None
    vals = [int(v or 0) for v in move_entries]
    if any(v <= 0 or v >= MOVE_PRESET_SLOT_SIZE or (v % 4) for v in vals):
        return None
    if vals != sorted(vals):
        return None

    # VJoe's 304-309 region is a compact Voomerang/Shocking Pink cluster.  Do
    # not require exact offsets across loads; require a tight, monotonic cluster.
    span = vals[-1] - vals[0]
    if not (0x1000 <= span <= 0x5000):
        return None

    default_word = entry(ANCHOR_ASSIST_FALLBACK_TABLE_INDEX)
    if default_word is None or int(default_word) <= 0 or int(default_word) >= MOVE_PRESET_SLOT_SIZE:
        return None

    wrapper_addr = int(chr_tbl_base) + int(default_word)
    wrapper_data = _read_mem_region_raw(wrapper_addr, VJOE_TRAMPOLINE_SCAN_SIZE)
    if not wrapper_data:
        return None

    graft_off = _vjoe_find_active_selector_graft_off(wrapper_data)
    state_off = _vjoe_find_attack_state_wrapper(wrapper_data)
    taunt_off = _vjoe_find_assist_taunt_payload_off(wrapper_data)

    score = 100
    # The move cluster is very VJoe-like.
    if 0x1000 <= span <= 0x3000:
        score += 120
    if vals[3] - vals[2] <= 0x1000:
        score += 20

    # The active assist table must point chr_tbl[426] at an assist wrapper that
    # contains the selector/graft block.  This is the key distinction from stale
    # tables and from tables where previous tests made chr_tbl[426] point to a
    # raw move body.
    if graft_off is not None:
        score += 700
    else:
        score -= 500

    if state_off is not None:
        score += 90
    if taunt_off is not None:
        score += 50
    if wrapper_data.startswith(b"\xBB\x83\x12\x6F"):
        score += 30

    return score, {
        "chr_tbl_base": int(chr_tbl_base),
        "table_index": ANCHOR_ASSIST_FALLBACK_TABLE_INDEX,
        "default_word": int(default_word) & 0xFFFFFFFF,
        "wrapper_addr": int(wrapper_addr),
        "graft_off": int(graft_off) if graft_off is not None else 0,
        "graft_addr": int(wrapper_addr) + int(graft_off or 0),
        "state_wrapper_off": int(state_off) if state_off is not None else 0,
        "state_addr": int(wrapper_addr) + int(state_off or 0) + VJOE_TRAMPOLINE_STATE_FIELD_OFF if state_off is not None else 0,
        "orig_state": int(_u16be(wrapper_data, int(state_off) + VJOE_TRAMPOLINE_STATE_FIELD_OFF) or 0) if state_off is not None else 0,
        "taunt_payload_off": int(taunt_off) if taunt_off is not None else 0,
        "move_304": vals[0],
        "move_305": vals[1],
        "move_306": vals[2],
        "move_307": vals[3],
        "move_308": vals[4],
        "move_309": vals[5],
    }


def _vjoe_find_active_selector_graft_off(wrapper_data: bytes) -> int | None:
    """Find the real VJoe assist graft block inside chr_tbl[426].

    Confirmed live examples put this around wrapper+0x154.  It can be either
    an already-installed Ryu-style selector table or the original 37 32 / 37 33
    graftable block.  Return the block start, not the 37 32 tail address.
    """
    if not wrapper_data:
        return None

    candidates: list[tuple[int, int, str]] = []
    limit = max(0, min(len(wrapper_data) - GENERIC_SELECTOR_TABLE_LEN, 0x700))
    for idx in range(0, limit + 1):
        if wrapper_data[idx:idx + 4] != b"\x0F\x06\x00\x27":
            continue

        source = ""
        score = 0
        if _looks_like_generic_native_selector_shape(wrapper_data, idx):
            source = "installed/native"
            score += 420
        elif _looks_like_generic_graft_candidate_shape(wrapper_data, idx):
            source = "graft-candidate"
            score += 360
        elif idx + 0x2C <= len(wrapper_data) and wrapper_data[idx + 0x1C:idx + 0x20] == SELECTOR_TAIL and wrapper_data[idx + 0x24:idx + 0x28] == SELECTOR_COMPANION_TAIL:
            source = "aligned-tail-pair"
            score += 260
        else:
            continue

        local = wrapper_data[idx:min(len(wrapper_data), idx + 0x120)]
        if STATE_WRAPPER_PREFIX in local:
            score += 80
        if b"\x00\x00\x01\xA8" in wrapper_data[idx:min(len(wrapper_data), idx + 0x300)]:
            score += 35
        # Confirmed VJoe grafts are close to +0x154 / +0x170 depending on
        # whether the module is measuring block start or tail address.
        score -= abs(idx - 0x154) // 4
        candidates.append((score, idx, source))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return int(candidates[0][1])


def _vjoe_active_selector_info_for_owner_base(
    owner_base: int | None,
    slot_char_ids: dict[int, int] | None = None,
) -> dict[str, int] | None:
    """Resolve VJoe by the active 426 wrapper, not by static address ownership."""
    if owner_base is None:
        return None
    if slot_char_ids is None:
        slot_char_ids = _read_slot_char_ids()
    if not _is_vjoe_owner_base(owner_base, slot_char_ids):
        return None

    scored: list[tuple[int, int, dict[str, int]]] = []
    for chr_tbl_base in _vjoe_candidate_chr_tbl_bases_global():
        row = _vjoe_chr_tbl_shape_score(chr_tbl_base)
        if row is None:
            continue
        score, info = row
        # Soft tie-breaker: prefer tables near this owner, but do not require
        # physical ownership.  That was the old bug.
        score -= min(abs(int(chr_tbl_base) - int(owner_base)) // 0x4000, 80)
        scored.append((score, int(chr_tbl_base), info))

    if not scored:
        return None

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    info = dict(scored[0][2])
    info["owner_base"] = int(owner_base)
    info["score"] = int(scored[0][0])
    return info


def _collect_vjoe_move_preset_rows_from_chr_tbl(
    owner_base: int,
    chr_tbl_base: int,
    slot_char_ids: dict[int, int],
) -> list[dict]:
    """Harvest VJoe presets from the resolved active chr_tbl, even if it spills."""
    data = _read_mem_region_raw(chr_tbl_base, MOVE_PRESET_SLOT_SIZE)
    if not data:
        return []

    cid = VJOE_CHAR_ID
    char_name = CHAR_ID_TO_KEY.get(cid, "Viewtiful Joe")
    rows: list[dict] = []
    seen: set[int] = set()
    for table_index in range(MOVE_PRESET_CHR_TBL_NUM_ENTRIES - 1):
        off = table_index * 4
        if off + 4 > len(data):
            break
        entry = _u32be(data, off)
        if entry is None:
            continue
        entry = int(entry)
        if entry in (0, 0xFFFFFFFF):
            continue
        if entry % 4 != 0 or entry < MOVE_PRESET_DATA_START_OFF or entry >= len(data):
            continue
        aid = _move_preset_anim_id_after_hdr(data, entry)
        if not _is_preset_worthy_table_index(table_index, aid, cid):
            continue
        if table_index in seen:
            continue
        seen.add(table_index)

        name, name_source, is_named = _resolve_move_preset_entry_name(table_index, aid, cid, char_name)
        if _is_filtered_assist_preset_name(name):
            continue
        move_abs = int(chr_tbl_base) + entry
        low = table_index & 0xFF
        is_normal = 0x100 <= table_index <= 0x10F and low in MOVE_PRESET_NORMAL_IDS
        score = 95 if is_normal else 80
        if is_named:
            score += 10

        rows.append({
            "kind": "assist-move-preset",
            "block": move_abs,
            "addr": move_abs,
            "owner": f"VJoe active chr_tbl @0x{int(chr_tbl_base):08X}",
            "slot": "0x11",
            "entry": name,
            "raw": f"0x{entry:08X}",
            "target": f"0x{move_abs:08X}",
            "guess": f"point VJoe active wrapper lanes to {name}; chr_tbl[{table_index}] @ 0x{int(chr_tbl_base):08X}",
            "score": score,
            "ctx": _fmt_context(data, entry, mark_len=4) if 0 <= entry < len(data) else "",
            "editable": True,
            "typ": "assist-move-preset",
            "selector_word": entry,
            "move_abs": move_abs,
            "move_id": aid if aid is not None else table_index,
            "table_index": table_index,
            "move_name": name,
            "move_named": is_named,
            "name_source": name_source,
            "owner_base": int(owner_base),
            "chr_tbl_base": int(chr_tbl_base),
            "char_id": cid,
            "source": f"vjoe-active+{name_source}",
        })

    return rows


def _collect_move_preset_rows_from_chr_tbl_base(
    owner_base: int,
    chr_tbl_base: int,
    char_id: int | None,
    slot_char_ids: dict[int, int],
    source_prefix: str = "forced-chr_tbl",
) -> list[dict]:
    """Harvest presets from an explicitly resolved chr_tbl base.

    This is the row-specific escape hatch for spillover characters. Tekkaman's
    active wrapper row can resolve from a chr_tbl that sits before/away from the
    nominal owner window, while the generic preset picker only knew the owner
    base. Passing the row's resolved chr_tbl here avoids the empty preset list
    and avoids harvesting stale movement/system tables.
    """
    data = _read_mem_region_raw(int(chr_tbl_base), MOVE_PRESET_SLOT_SIZE)
    if not data:
        return []

    cid = int(char_id) if char_id is not None else int(slot_char_ids.get(int(owner_base), 0) or 0)
    char_name = CHAR_ID_TO_KEY.get(cid, "")
    rows: list[dict] = []
    seen: set[int] = set()

    for table_index in range(MOVE_PRESET_CHR_TBL_NUM_ENTRIES - 1):
        off = table_index * 4
        if off + 4 > len(data):
            break
        entry = _u32be(data, off)
        if entry is None:
            continue
        entry = int(entry)
        if entry in (0, 0xFFFFFFFF):
            continue
        if entry % 4 != 0 or entry < MOVE_PRESET_DATA_START_OFF or entry >= len(data):
            continue

        aid = _move_preset_anim_id_after_hdr(data, entry)
        if not _is_preset_worthy_table_index(table_index, aid, cid):
            continue
        if table_index in seen:
            continue
        seen.add(table_index)

        name, name_source, is_named = _resolve_move_preset_entry_name(table_index, aid, cid, char_name)
        if _is_filtered_assist_preset_name(name):
            # Do not throw away priority-band moves just because the external
            # name map mislabeled them as movement. Fall back to a stable table
            # label so 304+ entries still appear and can be tested.
            if 304 <= table_index <= 368 or 510 <= table_index <= 520 or 256 <= table_index <= 270:
                name = f"move 0x{table_index:03X}"
                name_source = "fallback"
                is_named = False
            else:
                continue

        move_abs = int(chr_tbl_base) + entry
        low = table_index & 0xFF
        is_normal = 0x100 <= table_index <= 0x10F and low in MOVE_PRESET_NORMAL_IDS
        score = 95 if is_normal else 80
        if is_named:
            score += 10

        rows.append({
            "kind": "assist-move-preset",
            "block": move_abs,
            "addr": move_abs,
            "owner": f"{CHAR_ID_TO_KEY.get(cid, 'char')} chr_tbl @0x{int(chr_tbl_base):08X}",
            "slot": f"0x{cid:02X}" if cid else "?",
            "entry": name,
            "raw": f"0x{entry:08X}",
            "target": f"0x{move_abs:08X}",
            "guess": f"point row graft lanes to {name}; chr_tbl[{table_index}] @ 0x{int(chr_tbl_base):08X}",
            "score": score,
            "ctx": _fmt_context(data, entry, mark_len=4) if 0 <= entry < len(data) else "",
            "editable": True,
            "typ": "assist-move-preset",
            "selector_word": entry,
            "move_abs": move_abs,
            "move_id": aid if aid is not None else table_index,
            "table_index": table_index,
            "move_name": name,
            "move_named": is_named,
            "name_source": name_source,
            "owner_base": int(owner_base),
            "chr_tbl_base": int(chr_tbl_base),
            "char_id": cid,
            "source": f"{source_prefix}+{name_source}",
        })

    return rows

def _vjoe_animation_state_writes(
    owner_base: int | None,
    raw_val: int | None,
    info: dict[str, int] | None = None,
) -> dict[int, bytes]:
    "Patch VJoe's confirmed visible attack animation state field.\n\n    The semi-working direct/generic paths can affect the object/spawn side,\n    but they leave Joe's baked Shocking Pink pose alone. This companion write\n    updates the state wrapper the module already resolved inside the original 426\n    assist route.\n    "
    if owner_base is None:
        return {}

    if info is None or not isinstance(info, dict) or not info:
        info = _vjoe_active_selector_info_for_owner_base(owner_base) or _vjoe_trampoline_info_for_owner_base(owner_base) or {}

    state_addr = int(info.get("state_addr") or 0)
    if not state_addr:
        return {}

    if raw_val is None:
        sid = int(info.get("orig_state") or 0)
    else:
        sid = _vjoe_state_id_for_selector_word(owner_base, int(raw_val) & 0xFFFFFFFF) or 0

    if not (0x0001 <= int(sid) <= 0x0500):
        return {}

    return {state_addr: struct.pack(">H", int(sid) & 0xFFFF)}


def _vjoe_trampoline_writes(
    owner_base: int | None,
    raw_val: int | None,
    info: dict[str, int] | None = None,
) -> dict[int, bytes]:
    """VJoe-specific static/direct chr_tbl[426] write.

    This intentionally does not touch selector lanes, wrapper operands, or the
    in-wrapper Shocking Pink package. Selecting a move writes chr_tbl[426]
    immediately and leaves it there; runtime auto-trigger skips VJoe when
    VJoe Static Direct is ON.
    """
    if owner_base is None:
        return {}

    if info is None or not isinstance(info, dict) or not info:
        info = _vjoe_trampoline_info_for_owner_base(owner_base) or {}

    chr_tbl_base = int(info.get("chr_tbl_base") or 0)
    if not chr_tbl_base:
        chr_tbl_base = _chr_tbl_base_for_owner_base(owner_base) or 0
    if not chr_tbl_base:
        return {}

    table_index = int(info.get("table_index") or ANCHOR_ASSIST_FALLBACK_TABLE_INDEX)
    table_entry_addr = chr_tbl_base + (table_index * 4)

    default_word = int(info.get("default_word") or 0)
    if not default_word:
        default_word = _chr_tbl_word_for_owner_base(owner_base, table_index) or 0
    if not default_word:
        return {}

    word = default_word if raw_val is None else (int(raw_val) & 0xFFFFFFFF)
    writes = {table_entry_addr: struct.pack(">I", word & 0xFFFFFFFF)}
    writes.update(_vjoe_animation_state_writes(owner_base, raw_val, info))
    return writes


def _append_vjoe_trampoline_profile(slot_char_ids: dict[int, int], hits: list[dict]) -> None:
    """Expose VJoe's active in-wrapper selector/graft site.

    Older VJoe experiments exposed chr_tbl[426] directly.  That produced only
    partial reactions because 426 is an assist wrapper/package.  The useful
    target is the selector/graft block inside the active 426 wrapper.
    """
    for owner_base, cid in slot_char_ids.items():
        if int(cid) != VJOE_CHAR_ID:
            continue

        info = _vjoe_active_selector_info_for_owner_base(int(owner_base), slot_char_ids)
        if not info:
            # Last-ditch fallback keeps the old diagnostic row visible, but it is
            # deliberately scored lower and labelled as fallback.
            info = _vjoe_trampoline_info_for_owner_base(owner_base)
            if not info:
                continue
            info = dict(info)
            info["graft_addr"] = int(info.get("wrapper_addr", 0))
            info["fallback_static"] = 1

        idx = _slot_index_for_base(int(owner_base))
        cid_name = CHAR_ID_TO_KEY.get(int(cid), "?")
        owner = f"S{idx if idx is not None else '?'} {cid_name} @0x{int(owner_base):08X}"
        slot = f"0x{int(cid):02X}"

        graft_addr = int(info.get("graft_addr") or 0)
        if not graft_addr:
            continue
        current_block = _read_mem_region_raw(graft_addr, GENERIC_SELECTOR_TABLE_LEN)
        if not current_block or len(current_block) != GENERIC_SELECTOR_TABLE_LEN:
            continue

        words: list[int] = []
        if current_block.startswith(GENERIC_SELECTOR_SETUP_PREFIX):
            for off in GENERIC_SELECTOR_WORD_OFFSETS:
                raw = _u32be(current_block, off)
                if raw is not None:
                    words.append(int(raw))
        if len(words) != len(GENERIC_SELECTOR_WORD_OFFSETS):
            words = [0, 1, 2]

        chr_tbl_base = int(info.get("chr_tbl_base") or 0)
        raw_txt = _flat_selector_raw(words)
        if words and all(w == words[0] for w in words) and chr_tbl_base and words[0] <= MOVE_PRESET_SLOT_SIZE:
            target_txt = f"0x{chr_tbl_base + int(words[0]):08X}"
        else:
            target_txt = "default"
        entry = _flat_selector_entry_label(int(owner_base), words, slot_char_ids, "VJoe active assist")

        hits.append({
            "kind": "vjoe-active-selector-word",
            "block": graft_addr,
            "addr": graft_addr + GENERIC_SELECTOR_WORD_OFFSETS[0],
            "owner": owner,
            "slot": slot,
            "entry": entry,
            "raw": raw_txt,
            "target": target_txt,
            "guess": (
                "VJoe active wrapper graft: preserves chr_tbl[426]/hop/taunt; writes the in-wrapper selector lanes"
            ),
            "score": 1500 if not int(info.get("fallback_static", 0)) else 500,
            "ctx": (
                f"chr_tbl=0x{chr_tbl_base:08X}; "
                f"wrapper=0x{int(info.get('wrapper_addr', 0)):08X}; "
                f"graft=0x{graft_addr:08X}; "
                f"orig426=0x{int(info.get('default_word', 0)):08X}; "
                f"score={int(info.get('score', 0))}"
            ),
            "editable": True,
            "typ": "u32-generic-selector",
            "owner_base": int(owner_base),
            "char_id": VJOE_CHAR_ID,
            "vjoe_info": dict(info),
            "selector_words": words,
            "table_raw": current_block.hex(" ").upper(),
            "source": "vjoe-active-wrapper" if not int(info.get("fallback_static", 0)) else "vjoe-fallback-static",
        })



def _is_tatsunoko_direct_426_char_id(cid: int | None) -> bool:
    try:
        return int(cid) in TATSUNOKO_DIRECT_426_CHAR_IDS
    except Exception:
        return False


def _direct_426_info_for_owner_base(owner_base: int | None, slot_char_ids: dict[int, int] | None = None) -> dict[str, int] | None:
    """Resolve a direct chr_tbl[426] fallback row for Tatsunoko characters."""
    if owner_base is None:
        return None
    owner_base = int(owner_base)
    if slot_char_ids is None:
        slot_char_ids = _read_slot_char_ids()
    cid = int(slot_char_ids.get(owner_base, -1))
    if not _is_tatsunoko_direct_426_char_id(cid):
        return None

    chr_tbl_base = _chr_tbl_base_for_owner_base(owner_base)
    if chr_tbl_base is None:
        return None

    default_word = _chr_tbl_word_for_owner_base(owner_base, DIRECT_426_TABLE_INDEX)
    if default_word is None:
        return None
    default_word = int(default_word) & 0xFFFFFFFF
    if default_word <= 0 or default_word >= MOVE_PRESET_SLOT_SIZE:
        return None

    return {
        "owner_base": owner_base,
        "char_id": cid,
        "chr_tbl_base": int(chr_tbl_base),
        "table_index": DIRECT_426_TABLE_INDEX,
        "table_entry_addr": int(chr_tbl_base) + DIRECT_426_TABLE_INDEX * 4,
        "default_word": default_word,
        "default_target": int(chr_tbl_base) + default_word,
    }


def _direct_426_writes(owner_base: int | None, raw_val: int | None, info: dict | None = None) -> dict[int, bytes]:
    """Tatsunoko wrapper-preserving writes.

    The older direct-426 experiment wrote chr_tbl[426] itself. That is wrong
    for characters whose active chr_tbl lives before the nominal slot base or
    whose assist 426 wrapper owns the state/cleanup flow. Use the same safe
    idea as anchored fallback instead: keep chr_tbl[426] on the original assist
    wrapper, but patch the wrapper's internal local call operands to the chosen
    move. This restores Casshan's previous semi-working behavior and gives
    Tekkaman a real target once the pre-slot chr_tbl is found.
    """
    if info is None or not isinstance(info, dict) or not info:
        info = _direct_426_info_for_owner_base(owner_base) or {}
    if not info:
        return {}

    owner_base = int(info.get("owner_base") or owner_base or 0)
    if not owner_base:
        return {}

    table_index = int(info.get("table_index") or DIRECT_426_TABLE_INDEX)
    default_word = int(info.get("default_word") or 0) or None
    if default_word is None:
        default_word = _chr_tbl_word_for_owner_base(owner_base, table_index)
    if default_word is None:
        return {}

    pairs = _anchored_assist_operands_for_owner_base(owner_base, table_index, default_word)

    # Preferred path: preserve the real assist wrapper and patch all discovered
    # internal body-call operands. This prevents table-base mistakes and keeps
    # hop/taunt/leave intact.
    if pairs:
        return _anchored_assist_fallback_writes(
            owner_base,
            None if raw_val is None else int(raw_val),
            table_index,
            int(default_word),
            [off for off, _word in pairs],
            [word for _off, word in pairs],
        )

    # Last-resort fallback only when the wrapper exposes no call operands.
    table_entry_addr = int(info.get("table_entry_addr") or 0)
    if not table_entry_addr:
        chr_tbl_base = int(info.get("chr_tbl_base") or 0)
        if not chr_tbl_base:
            chr_tbl_base = _chr_tbl_base_for_owner_base(owner_base) or 0
        if not chr_tbl_base:
            return {}
        table_entry_addr = chr_tbl_base + table_index * 4

    word = int(default_word) if raw_val is None else (int(raw_val) & 0xFFFFFFFF)
    if not word:
        return {}
    return {table_entry_addr: struct.pack(">I", word & 0xFFFFFFFF)}

def _tatsunoko_find_wrapper_graft_off(wrapper_data: bytes) -> int | None:
    """Find the early attack-body block inside a Tatsunoko chr_tbl[426] wrapper.

    Casshan and Tekkaman both have the useful block at wrapper+0x154. That is
    before the assist-taunt block and before the later internal call operands.
    Grafting here replaces the default attack instead of appending after it.
    """
    if not wrapper_data:
        return None

    preferred = TATSUNOKO_WRAPPER_GRAFT_OFF
    if preferred + 4 <= len(wrapper_data) and wrapper_data[preferred:preferred + 4] == b"\x0F\x06\x00\x27":
        return preferred

    candidates: list[tuple[int, int]] = []
    limit = max(0, min(len(wrapper_data) - GENERIC_SELECTOR_TABLE_LEN, TATSUNOKO_WRAPPER_GRAFT_SCAN_END))
    for idx in range(0, limit + 1):
        if wrapper_data[idx:idx + 4] != b"\x0F\x06\x00\x27":
            continue
        # Avoid taunt/leave blocks. The attack-body block should not contain
        # assist taunt 0x01A8 immediately after the header.
        local = wrapper_data[idx:min(len(wrapper_data), idx + 0x90)]
        if b"\x00\x00\x01\xA8" in local:
            continue
        score = 100 - abs(idx - preferred)
        if STATE_WRAPPER_PREFIX in local or b"\x04\x01\x02\x3F" in local:
            score += 80
        if b"\x01\x3C\x00\x00" in local:
            score += 20
        candidates.append((score, idx))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return int(candidates[0][1])


def _tatsunoko_wrapper_graft_info_for_owner_base(owner_base: int | None, slot_char_ids: dict[int, int] | None = None) -> dict[str, int] | None:
    """Resolve the active early attack-body graft site for a Tatsunoko slot."""
    info = _direct_426_info_for_owner_base(owner_base, slot_char_ids)
    if not info:
        return None

    wrapper_addr = int(info["default_target"])
    wrapper_data = _read_mem_region_raw(wrapper_addr, ANCHOR_ASSIST_WRAPPER_SCAN_SIZE)
    if not wrapper_data:
        return None

    graft_off = _tatsunoko_find_wrapper_graft_off(wrapper_data)
    if graft_off is None:
        return None

    current_block = wrapper_data[graft_off:graft_off + GENERIC_SELECTOR_TABLE_LEN]
    if len(current_block) != GENERIC_SELECTOR_TABLE_LEN:
        return None

    out = dict(info)
    out["wrapper_addr"] = wrapper_addr
    out["graft_off"] = int(graft_off)
    out["graft_addr"] = wrapper_addr + int(graft_off)
    return out



def _assist_state_sync_cache_key(owner_base: int, wrapper_addr: int) -> tuple[int, int, int]:
    return (int(_ASSIST_CACHE_EPOCH), int(owner_base), int(wrapper_addr))


def _assist_state_sync_char_id(row: dict | None, owner_base: int | None = None) -> int:
    if isinstance(row, dict):
        try:
            cid = int(row.get("char_id") or 0)
        except Exception:
            cid = 0
        if cid:
            return cid
    if owner_base is None:
        return 0
    try:
        return int(_read_slot_char_ids().get(int(owner_base), 0) or 0)
    except Exception:
        return 0


def _assist_state_sync_enabled(row: dict | None, owner_base: int | None = None) -> bool:
    return _assist_state_sync_char_id(row, owner_base) in ASSIST_STATE_SYNC_CHAR_IDS


def _assist_state_sync_wrapper_info(row: dict) -> dict[str, int]:
    if not isinstance(row, dict):
        return {}

    owner_base = int(row.get("owner_base") or (_owning_chr_tbl(int(row.get("block") or 0)) or 0))
    if not owner_base or not _assist_state_sync_enabled(row, owner_base):
        return {}

    embedded = row.get("assist_state_info")
    if isinstance(embedded, dict):
        state_addr = int(embedded.get("state_addr") or 0)
        wrapper_addr = int(embedded.get("wrapper_addr") or 0)
        orig_state = int(embedded.get("orig_state") or 0)
        if state_addr and wrapper_addr and 1 <= orig_state <= 0x0500:
            return {
                "owner_base": owner_base,
                "wrapper_addr": wrapper_addr,
                "state_addr": state_addr,
                "orig_state": orig_state,
                "state_wrapper_off": int(embedded.get("state_wrapper_off") or (state_addr - wrapper_addr - STATE_ID_OFF)),
            }

    wrapper_addr = 0
    direct_info = row.get("direct_426_info")
    if isinstance(direct_info, dict):
        wrapper_addr = int(direct_info.get("wrapper_addr") or direct_info.get("default_target") or 0)

    if not wrapper_addr:
        chr_tbl_base = int(row.get("chr_tbl_base") or 0)
        if not chr_tbl_base:
            chr_tbl_base = _chr_tbl_base_for_owner_base(owner_base) or 0
        if chr_tbl_base:
            default_word = _chr_tbl_word_for_owner_base(owner_base, DIRECT_426_TABLE_INDEX)
            if default_word is not None and 0 < int(default_word) < MOVE_PRESET_SLOT_SIZE:
                wrapper_addr = int(chr_tbl_base) + int(default_word)

    if not wrapper_addr:
        return {}

    key = _assist_state_sync_cache_key(owner_base, wrapper_addr)
    cached = _ASSIST_STATE_SYNC_CACHE.get(key)
    if isinstance(cached, dict) and int(cached.get("state_addr") or 0):
        return dict(cached)

    wrapper_data = _read_mem_region_raw(wrapper_addr, ASSIST_STATE_SYNC_SCAN_SIZE)
    if not wrapper_data:
        return {}

    graft_addr = int(row.get("block") or 0)
    graft_off = graft_addr - wrapper_addr if wrapper_addr <= graft_addr < wrapper_addr + len(wrapper_data) else -1
    search_floor = max(0x40, graft_off + GENERIC_SELECTOR_TABLE_LEN if graft_off >= 0 else 0x80)

    candidates: list[tuple[int, int, int]] = []
    limit = len(wrapper_data) - STATE_WRAPPER_LEN
    for off in range(max(0, search_floor), max(0, limit) + 1):
        if wrapper_data[off:off + len(STATE_WRAPPER_PREFIX)] != STATE_WRAPPER_PREFIX:
            continue
        if wrapper_data[off + STATE_MARKER_OFF:off + STATE_MARKER_OFF + 2] != b"\x01\x3C":
            continue
        sid = _u16be(wrapper_data, off + STATE_ID_OFF)
        if sid is None or not (0x0001 <= int(sid) <= 0x0500):
            continue
        if int(sid) in ASSIST_STATE_SYNC_EXCLUDED_STATES:
            continue

        score = 1000 - min(abs(off - search_floor), 900)
        if graft_off >= 0 and off >= graft_off + GENERIC_SELECTOR_TABLE_LEN:
            score += 500
        # The attack animation wrapper is normally the first non-assist state
        # after the selector block. Later wrappers are usually taunt or cleanup.
        candidates.append((score, -off, int(sid)))

    if not candidates:
        return {}

    candidates.sort(reverse=True)
    _score, neg_off, orig_state = candidates[0]
    state_wrapper_off = -int(neg_off)
    info = {
        "owner_base": owner_base,
        "wrapper_addr": int(wrapper_addr),
        "state_wrapper_off": int(state_wrapper_off),
        "state_addr": int(wrapper_addr) + int(state_wrapper_off) + STATE_ID_OFF,
        "orig_state": int(orig_state),
    }
    _ASSIST_STATE_SYNC_CACHE[key] = dict(info)
    return info


def _assist_state_id_for_selector_word(owner_base: int, raw_val: int, row: dict | None = None) -> int | None:
    chr_tbl_base = 0
    if isinstance(row, dict):
        chr_tbl_base = int(row.get("chr_tbl_base") or 0)
        direct_info = row.get("direct_426_info")
        if not chr_tbl_base and isinstance(direct_info, dict):
            chr_tbl_base = int(direct_info.get("chr_tbl_base") or 0)
    if not chr_tbl_base:
        chr_tbl_base = _chr_tbl_base_for_owner_base(owner_base) or 0
    if not chr_tbl_base:
        return None

    raw = int(raw_val) & 0xFFFFFFFF
    candidate_words: list[int] = [raw]

    table_data = _read_mem_region_raw(chr_tbl_base, MOVE_PRESET_CHR_TBL_NUM_ENTRIES * 4)
    if table_data:
        for table_index in range(MOVE_PRESET_CHR_TBL_NUM_ENTRIES):
            entry = _u32be(table_data, table_index * 4)
            if entry is None or int(entry) in (0, 0xFFFFFFFF):
                continue
            for delta in MOVE_PRESET_ENTRY_OFFSETS:
                if ((int(entry) + int(delta)) & 0xFFFFFFFF) == raw:
                    candidate_words.insert(0, int(entry) & 0xFFFFFFFF)
                    break

    seen: set[int] = set()
    for word in candidate_words:
        if word in seen or word <= 0 or word >= MOVE_PRESET_SLOT_SIZE:
            continue
        seen.add(word)
        move_data = _read_mem_region_raw(int(chr_tbl_base) + int(word), 0x500)
        if not move_data:
            continue
        sid = _move_preset_anim_id_after_hdr(move_data, 0)
        if sid is not None and 0x0001 <= int(sid) <= 0x0500:
            return int(sid)
    return None


def _assist_state_sync_writes(row: dict, raw_val: int | None) -> dict[int, bytes]:
    if not isinstance(row, dict):
        return {}
    owner_base = int(row.get("owner_base") or (_owning_chr_tbl(int(row.get("block") or 0)) or 0))
    if not owner_base or not _assist_state_sync_enabled(row, owner_base):
        return {}

    info = _assist_state_sync_wrapper_info(row)
    state_addr = int(info.get("state_addr") or 0)
    if not state_addr:
        return {}

    if raw_val is None:
        sid = int(info.get("orig_state") or 0)
    else:
        sid = _assist_state_id_for_selector_word(owner_base, int(raw_val), row) or 0

    if not (0x0001 <= int(sid) <= 0x0500):
        return {}
    return {state_addr: struct.pack(">H", int(sid) & 0xFFFF)}


def _assist_state_sync_attach(row: dict) -> dict:
    out = dict(row or {})
    info = _assist_state_sync_wrapper_info(out)
    if info:
        out["assist_state_info"] = dict(info)
    return out


def _append_direct_426_profiles(slot_char_ids: dict[int, int], hits: list[dict]) -> None:
    """Append one early-wrapper graft row for Tatsunoko characters.

    Kept under this historical function name so the rest of the scanner call
    site stays stable. The row is now a real generic selector graft at the
    start of the 426 attack body, not the older direct/anchored 426 fallback.
    """
    for owner_base, cid in slot_char_ids.items():
        if not _is_tatsunoko_direct_426_char_id(cid):
            continue
        info = _tatsunoko_wrapper_graft_info_for_owner_base(int(owner_base), slot_char_ids)
        if not info:
            continue

        idx = _slot_index_for_base(int(owner_base))
        name = CHAR_ID_TO_KEY.get(int(cid), f"CID 0x{int(cid):02X}")
        owner = f"S{idx if idx is not None else '?'} {name} @0x{int(owner_base):08X}"
        slot = f"0x{int(cid):02X}"
        graft_addr = int(info["graft_addr"])
        chr_tbl_base = int(info["chr_tbl_base"])
        wrapper_addr = int(info["wrapper_addr"])
        default_word = int(info["default_word"])

        current_block = _read_mem_region_raw(graft_addr, GENERIC_SELECTOR_TABLE_LEN)
        if not current_block or len(current_block) != GENERIC_SELECTOR_TABLE_LEN:
            continue

        is_installed = current_block.startswith(GENERIC_SELECTOR_SETUP_PREFIX)
        if is_installed:
            words = []
            for off in GENERIC_SELECTOR_WORD_OFFSETS:
                raw = _u32be(current_block, off)
                if raw is None:
                    words = []
                    break
                words.append(int(raw))
        else:
            words = [0, 1, 2]

        entry = _flat_selector_entry_label(int(owner_base), words, slot_char_ids, "default")
        raw_txt = _flat_selector_raw(words)
        target_txt = _flat_selector_target(int(owner_base), words)

        hits.append({
            "kind": "generic-selector-word",
            "block": graft_addr,
            "addr": graft_addr + GENERIC_SELECTOR_WORD_OFFSETS[0],
            "owner": owner,
            "slot": slot,
            "entry": entry,
            "raw": raw_txt,
            "target": target_txt,
            "guess": "Tatsunoko wrapper graft; replaces early 426 attack body instead of appending after default",
            "score": 1600,
            "ctx": (
                f"chr_tbl=0x{chr_tbl_base:08X}; wrapper=0x{wrapper_addr:08X}; "
                f"graft=0x{graft_addr:08X}; chr_tbl[426]=0x{default_word:08X}"
            ),
            "editable": True,
            "typ": "u32-generic-selector",
            "owner_base": int(owner_base),
            "char_id": int(cid),
            "direct_426_info": dict(info),
            "assist_state_info": _assist_state_sync_wrapper_info({
                "block": graft_addr,
                "owner_base": int(owner_base),
                "char_id": int(cid),
                "chr_tbl_base": chr_tbl_base,
                "direct_426_info": dict(info),
            }),
            "selector_words": words,
            "table_raw": current_block.hex(" ").upper(),
            "source": "tatsunoko-wrapper-graft",
        })


def _move_preset_anim_id_after_hdr(data: bytes, off: int) -> int | None:
    if off < 0 or off >= len(data):
        return None
    end = min(off + MOVE_PRESET_LOOKAHEAD, len(data) - 4 + 1)
    start = off
    for p in range(start, end):
        if p + len(MOVE_PRESET_ANIM_HDR) <= len(data) and data[p:p + len(MOVE_PRESET_ANIM_HDR)] == MOVE_PRESET_ANIM_HDR:
            q0 = p + len(MOVE_PRESET_ANIM_HDR)
            q1 = min(q0 + MOVE_PRESET_LOOKAHEAD, len(data) - 4 + 1)
            for q in range(q0, q1):
                op = data[q + 2]
                fps = data[q + 3]
                if fps == 0x3C and op in (0x01, 0x04):
                    aid = (data[q] << 8) | data[q + 1]
                    if 1 <= aid <= 0x0500:
                        return aid
        op = data[p + 2]
        fps = data[p + 3]
        if fps == 0x3C and op in (0x01, 0x04):
            aid = (data[p] << 8) | data[p + 1]
            if 1 <= aid <= 0x0500:
                return aid
    return None


def _move_preset_name(aid: int | None, char_id: int | None = None, char_name: str | None = None) -> str:
    if aid is None:
        return "move_????"
    name = _lookup_move_name_safe(char_id, char_name, aid)
    if name:
        return name
    low = aid & 0xFF
    if low in MOVE_PRESET_NORMAL_LABELS:
        return MOVE_PRESET_NORMAL_LABELS[low]
    return f"anim_{aid:04X}"


def _append_assist_move_preset_row(base: int, move_abs: int, aid: int | None,
                                   source: str, slot_char_ids: dict[int, int],
                                   data: bytes, hits: list[dict]) -> None:
    cid = slot_char_ids.get(base)
    char_name = CHAR_ID_TO_KEY.get(cid, "") if cid is not None else ""
    owner = _owner_name(move_abs, slot_char_ids)
    slot = _slot_cid(move_abs, slot_char_ids)
    word = move_abs - base
    if word <= 0 or word > 0x100000:
        return
    name, name_source, is_named = _resolve_move_preset_entry_name(aid or 0, aid, cid, char_name)
    if _is_filtered_assist_preset_name(name):
        return
    low = (aid & 0xFF) if aid is not None else None
    is_normal = aid is not None and aid >= 0x0100 and low in MOVE_PRESET_NORMAL_IDS
    kind_label = "normal" if is_normal else "move"
    local = move_abs - base
    score = 88 if kind_label == "normal" else 72
    if is_named:
        score += 10
    if cid in (12, 13):
        score += 8
    raw = f"0x{word:08X}"
    hits.append({
        "kind": "assist-move-preset",
        "block": move_abs,
        "addr": move_abs,
        "owner": owner,
        "slot": slot,
        "entry": name,
        "raw": raw,
        "target": f"0x{move_abs:08X}",
        "guess": f"double-click: point graft lanes to {name}; base word from {source}",
        "score": score,
        "ctx": _fmt_context(data, local, mark_len=4) if 0 <= local < len(data) else "",
        "editable": True,
        "typ": "assist-move-preset",
        "selector_word": word,
        "move_abs": move_abs,
        "move_id": aid if aid is not None else 0,
        "table_index": aid if aid is not None else 0,
        "move_name": name,
        "move_named": is_named,
        "name_source": name_source,
        "owner_base": base,
        "char_id": cid if cid is not None else 0,
        "source": source,
    })


def _resolve_move_preset_entry_name(table_index: int, aid: int | None,
                                    char_id: int | None = None,
                                    char_name: str | None = None) -> tuple[str, str, bool]:
    """Return display name, source, and whether it is a real named preset."""
    csv_name = _assist_csv_name(char_id, table_index, aid)
    if csv_name:
        return csv_name, "csv", True

    # Global universal normals are table-indexed as 0x100 + normal id.
    if 0x100 <= table_index <= 0x10F:
        low = table_index & 0xFF
        if low in MOVE_PRESET_NORMAL_LABELS:
            return MOVE_PRESET_NORMAL_LABELS[low], "normal", True

    # Try the project move map with character context. This matches how the
    # frame-data window resolves pretty names through move_id_map/fd_utils.
    mapped = _lookup_move_name_safe(char_id, char_name, table_index)
    if mapped and _is_named_preset_name(mapped):
        return mapped, "move_id_map", True

    if aid is not None:
        mapped = _lookup_move_name_safe(char_id, char_name, aid)
        if mapped and _is_named_preset_name(mapped):
            return mapped, "move_id_map", True

        aid_name = _move_preset_name(aid, char_id, char_name)
        if _is_named_preset_name(aid_name):
            return aid_name, "anim", True
        return f"move 0x{table_index:03X} / anim 0x{aid:04X}", "fallback", False

    return f"move 0x{table_index:03X}", "fallback", False


def _move_preset_entry_name(table_index: int, aid: int | None,
                            char_id: int | None = None,
                            char_name: str | None = None) -> str:
    return _resolve_move_preset_entry_name(table_index, aid, char_id, char_name)[0]


def _is_preset_worthy_table_index(table_index: int, aid: int | None, char_id: int | None = None) -> bool:
    """Keep attack/move rows, not idle/block/KO/system action spam."""
    # Filter out the noisy 369-400 band from assist presets entirely.
    if 369 <= table_index <= 400:
        return False

    char_name = CHAR_ID_TO_KEY.get(char_id, "") if char_id is not None else ""
    name, name_source, is_named = _resolve_move_preset_entry_name(table_index, aid, char_id, char_name)
    if _is_filtered_assist_preset_name(name):
        return False

    # Display assist preset bands. Keep these even if the pretty-name map
    # is missing or wrong, because these are the actual special/assist ranges.
    if 304 <= table_index <= 368:
        return True
    if 510 <= table_index <= 520:
        return True
    if 256 <= table_index <= 270:
        return True

    # Universal normals.
    if 0x100 <= table_index <= 0x10F:
        return True

    # Broader move-ish ranges, but do not let low table entries like 0/1/4
    # through just because the frame-data map calls them "backward".
    if 0x130 <= table_index <= 0x18F:
        return True
    if 0x200 <= table_index <= 0x230:
        return True
    if 0x1C0 <= table_index <= 0x1FF:
        return aid is not None and aid >= 0x0100

    # CSV names are curated by table index, so allow named leftovers from CSV.
    # Generic move_id_map/anim names for low system rows are not enough.
    if is_named and name_source == "csv":
        return True

    if aid is not None and aid >= 0x0100:
        low = aid & 0xFF
        if low in MOVE_PRESET_NORMAL_IDS:
            return True
        fallback_name = _move_preset_name(aid, char_id, char_name)
        if _is_named_preset_name(fallback_name) and not _is_filtered_assist_preset_name(fallback_name):
            return True

    return False

def _scan_assist_move_presets(slot_char_ids: dict[int, int], hits: list[dict]) -> None:
    # Dedicated full-slot pass. Do this outside the 0x40000 MEM2 chunk scan so
    # chr_tbl entries can resolve into the whole character script region.
    #
    # Critical correction: _CHR_TBL_BASES are broad ownership windows, not
    # always the actual live chr_tbl base. The selector word must be relative
    # to the actual live chr_tbl base found by the chr_tbl/chr_act table shape.
    # Harvest all validated table candidates in the slot so one missed label
    # does not make the preset picker empty.
    seen: set[tuple[int, int]] = set()
    for owner_base in _CHR_TBL_BASES:
        cid = slot_char_ids.get(owner_base)
        if cid is None:
            continue

        chr_tbl_bases = _find_runtime_chr_tbl_bases_for_region(owner_base)

        # Some Tatsunoko slots expose a useful action-offset table at the broad
        # ownership base even though it does not pass the strict chr_tbl/chr_act
        # header test. Tekkaman is the current example: the direct-426 row is
        # valid enough to target chr_tbl[426]-style offsets, but the preset
        # picker was empty because no strict runtime chr_tbl was harvested.
        # Use this loose fallback only for the direct-426 Tatsunoko class and
        # only when the strict path found nothing, so Capcom/VJoe/Ryu/Chun stay
        # on their confirmed table logic.
        if not chr_tbl_bases and _is_tatsunoko_direct_426_char_id(cid):
            chr_tbl_bases = [int(owner_base)]

        if not chr_tbl_bases:
            continue

        for chr_tbl_base in chr_tbl_bases:
            data = _read_mem_region_raw(chr_tbl_base, MOVE_PRESET_SLOT_SIZE)
            if not data:
                continue

            for table_index in range(MOVE_PRESET_CHR_TBL_NUM_ENTRIES - 1):
                off = table_index * 4
                if off + 4 > len(data):
                    break
                entry = _u32be(data, off)
                if entry is None:
                    continue
                if entry in (0, 0xFFFFFFFF):
                    continue
                if entry % 4 != 0 or entry < MOVE_PRESET_DATA_START_OFF:
                    continue
                if entry >= len(data):
                    continue

                aid = _move_preset_anim_id_after_hdr(data, entry)
                if not _is_preset_worthy_table_index(table_index, aid, cid):
                    continue

                move_abs = chr_tbl_base + entry
                key = (chr_tbl_base, table_index)
                if key in seen:
                    continue
                seen.add(key)

                char_name = CHAR_ID_TO_KEY.get(cid, "") if cid is not None else ""
                name, name_source, is_named = _resolve_move_preset_entry_name(table_index, aid, cid, char_name)
                if _is_filtered_assist_preset_name(name):
                    continue
                low = table_index & 0xFF
                is_normal = 0x100 <= table_index <= 0x10F and low in MOVE_PRESET_NORMAL_IDS
                kind_label = "normal" if is_normal else "move"
                owner = _owner_name(move_abs, slot_char_ids)
                slot = _slot_cid(move_abs, slot_char_ids)
                score = 95 if is_normal else 80
                if is_named:
                    score += 10
                if cid in (12, 13):
                    score += 5

                hits.append({
                    "kind": "assist-move-preset",
                    "block": move_abs,
                    "addr": move_abs,
                    "owner": owner,
                    "slot": slot,
                    "entry": name,
                    "raw": f"0x{entry:08X}",
                    "target": f"0x{move_abs:08X}",
                    "guess": f"point graft lanes to {name}; chr_tbl[{table_index}] @ 0x{chr_tbl_base:08X}",
                    "score": score,
                    "ctx": _fmt_context(data, entry, mark_len=4) if 0 <= entry < len(data) else "",
                    "editable": True,
                    "typ": "assist-move-preset",
                    "selector_word": entry,
                    "move_abs": move_abs,
                    "move_id": aid if aid is not None else table_index,
                    "table_index": table_index,
                    "move_name": name,
                    "move_named": is_named,
                    "name_source": name_source,
                    # owner_base is the broad slot window used by graft lanes.
                    # chr_tbl_base is the real selector base used by the VM.
                    "owner_base": owner_base,
                    "chr_tbl_base": chr_tbl_base,
                    "char_id": cid if cid is not None else 0,
                    "source": (
                        f"loose_chr_tbl+{name_source}"
                        if _is_tatsunoko_direct_426_char_id(cid) and int(chr_tbl_base) == int(owner_base)
                        else f"chr_tbl+{name_source}"
                    ),
                })

def _append_state_wrapper(data: bytes, base_addr: int, idx: int, slot_char_ids: dict[int, int],
                          hits: list[dict]) -> None:
    block_addr = base_addr + idx
    owner_base = _owning_chr_tbl(block_addr)
    if owner_base is None:
        return
    if idx + STATE_WRAPPER_LEN > len(data):
        return
    if data[idx:idx + len(STATE_WRAPPER_PREFIX)] != STATE_WRAPPER_PREFIX:
        return
    if data[idx + STATE_MARKER_OFF:idx + STATE_MARKER_OFF + 2] != b"\x01\x3C":
        return

    state_id = _u16be(data, idx + STATE_ID_OFF)
    if state_id is None:
        return
    owner = _owner_name(block_addr, slot_char_ids)
    slot = _slot_cid(block_addr, slot_char_ids)
    score = 85
    if state_id in (0x01A1, 0x01A8, 0x01AE, 0x0112):
        score = 95

    hits.append({
        "kind": "state-wrapper",
        "block": block_addr,
        "addr": block_addr,
        "owner": owner,
        "slot": slot,
        "entry": "block",
        "raw": data[idx:idx + STATE_WRAPPER_LEN].hex(" ").upper(),
        "target": "",
        "guess": "assist/state-call wrapper candidate",
        "score": score,
        "ctx": _fmt_context(data, idx, mark_len=STATE_WRAPPER_LEN),
        "editable": False,
        "typ": f"raw{STATE_WRAPPER_LEN}",
    })
    hits.append({
        "kind": "state-id",
        "block": block_addr,
        "addr": block_addr + STATE_ID_OFF,
        "owner": owner,
        "slot": slot,
        "entry": "StateID",
        "raw": f"0x{state_id:04X}",
        "target": "",
        "guess": "editable state call",
        "score": score,
        "ctx": _fmt_context(data, idx + STATE_ID_OFF, mark_len=2),
        "editable": True,
        "typ": "u16-state",
    })



def _append_confirmed_wrapper_neighborhood(data: bytes, base_addr: int, block_addr: int, state_addr: int,
                                           state_id: int, label: str, slot_char_ids: dict[int, int],
                                           hits: list[dict]) -> None:
    """Focused editable poke zone around a known live state wrapper."""
    window_before = 0x30
    window_after = 0x70

    start_addr = block_addr - window_before
    end_addr = block_addr + window_after
    data_start = base_addr
    data_end = base_addr + len(data)
    if start_addr < data_start or end_addr > data_end:
        return

    block_idx = block_addr - base_addr
    state_idx = state_addr - base_addr
    owner = _owner_name(block_addr, slot_char_ids)
    slot = _slot_cid(block_addr, slot_char_ids)
    blob = data[start_addr - base_addr:end_addr - base_addr]

    hits.append({
        "kind": "confirmed-wrapper",
        "block": block_addr,
        "addr": block_addr,
        "owner": owner,
        "slot": slot,
        "entry": label,
        "raw": blob.hex(" ").upper(),
        "target": "",
        "guess": f"CONFIRMED live wrapper neighborhood, StateID 0x{state_id:04X}",
        "score": 100,
        "ctx": _fmt_context(data, block_idx, mark_len=STATE_WRAPPER_LEN),
        "editable": False,
        "typ": "raw-window",
    })

    hits.append({
        "kind": "confirmed-state-id",
        "block": block_addr,
        "addr": state_addr,
        "owner": owner,
        "slot": slot,
        "entry": "StateID",
        "raw": f"0x{state_id:04X}",
        "target": "",
        "guess": "known live animation/state call; changes Chun anim, not X/Y translation",
        "score": 100,
        "ctx": _fmt_context(data, state_idx, mark_len=2),
        "editable": True,
        "typ": "u16-state",
    })

    poke_points = [
        (block_addr - 0x10, 4, "pre -0x10 dword"),
        (block_addr - 0x0C, 4, "pre -0x0C dword"),
        (block_addr - 0x08, 4, "pre -0x08 dword"),
        (block_addr - 0x04, 4, "pre -0x04 dword"),
        (block_addr - 0x0A, 2, "pre -0x0A u16"),
        (block_addr - 0x06, 2, "pre -0x06 u16"),
        (block_addr - 0x04, 2, "pre -0x04 u16"),
        (block_addr - 0x02, 2, "pre -0x02 u16"),
        (block_addr + 0x34, 4, "post +0x34 cmd"),
        (block_addr + 0x3C, 4, "post +0x3C cmd"),
        (block_addr + 0x40, 4, "post +0x40 cmd"),
        (block_addr + 0x4C, 4, "post +0x4C cmd"),
    ]

    for addr, size, name in poke_points:
        if addr < data_start or addr + size > data_end:
            continue
        idx = addr - base_addr
        raw = data[idx:idx + size].hex(" ").upper()
        hits.append({
            "kind": "wrapper-poke",
            "block": block_addr,
            "addr": addr,
            "owner": owner,
            "slot": slot,
            "entry": name,
            "raw": raw,
            "target": f"{addr - block_addr:+#x}",
            "guess": "focused poke near confirmed Chun live wrapper",
            "score": 100,
            "ctx": _fmt_context(data, idx, mark_len=size),
            "editable": True,
            "typ": f"raw{size}",
        })

def _looks_like_tensho_descriptor(data: bytes, idx: int) -> bool:
    if idx < 0 or idx + TENSHO_DESC_STRIDE > len(data):
        return False
    desc_id = _u32be(data, idx)
    group = _u32be(data, idx + 0x04)
    if desc_id not in TENSHO_DESC_IDS or group != TENSHO_DESC_GROUP:
        return False

    # Strong shape checks from the observed records, but keep them soft enough
    # that an active match does not get rejected if one metadata field mutates.
    score = 0
    for off in (0x10, 0x14, 0x18):
        if data[idx + off:idx + off + 4] == b"\x3F\x80\x00\x00":
            score += 1
    if _u32be(data, idx + 0x54) == TENSHO_DESC_STRIDE:
        score += 1
    if _u32be(data, idx + 0xC0) == TENSHO_DESC_STRIDE:
        score += 1
    if _u32be(data, idx + 0xCC) is not None:
        score += 1
    return score >= 4


def _append_tensho_descriptor(data: bytes, base_addr: int, idx: int, slot_char_ids: dict[int, int],
                              hits: list[dict]) -> None:
    block_addr = base_addr + idx
    desc_id = _u32be(data, idx)
    group = _u32be(data, idx + 0x04)
    if desc_id is None or group is None:
        return

    local_index = _u32be(data, idx + 0xCC)
    size_a = _u32be(data, idx + 0x54)
    size_b = _u32be(data, idx + 0xC0)
    prev_delta = _u32be(data, idx + 0x58)
    owner = _owner_name(block_addr, slot_char_ids)
    slot = _slot_cid(block_addr, slot_char_ids)

    score = 100 if desc_id in TENSHO_DESC_IDS and group == TENSHO_DESC_GROUP else 80
    hits.append({
        "kind": "tensho-desc",
        "block": block_addr,
        "addr": block_addr,
        "owner": owner,
        "slot": slot,
        "entry": f"desc 0x{desc_id:04X}",
        "raw": data[idx:idx + 0x60].hex(" ").upper(),
        "target": f"idx {local_index}" if local_index is not None else "",
        "guess": f"Tensho 0xD0 descriptor, group 0x{group:04X}, sizes {size_a}/{size_b}, prev 0x{prev_delta or 0:08X}",
        "score": score,
        "ctx": _fmt_context(data, idx, mark_len=16),
        "editable": False,
        "typ": "raw-window",
    })

    for off in TENSHO_FLOAT_OFFS:
        v = _f32be(data, idx + off)
        raw = data[idx + off:idx + off + 4].hex(" ").upper()
        hits.append({
            "kind": "tensho-float",
            "block": block_addr,
            "addr": block_addr + off,
            "owner": owner,
            "slot": slot,
            "entry": f"+0x{off:02X} {TENSHO_FLOAT_NAMES.get(off, 'float')}",
            "raw": _fmt_f32(v),
            "target": raw,
            "guess": "editable big-endian f32; likely attach/position candidate",
            "score": score,
            "ctx": _fmt_context(data, idx + off, mark_len=4),
            "editable": True,
            "typ": "f32",
        })

    # Also expose the ID/group fields as raw u32 in case the route binds by descriptor ID.
    for off, name in ((0x00, "DescID"), (0x04, "GroupID"), (0x54, "SizeA"), (0xC0, "SizeB"), (0xCC, "LocalIndex")):
        raw_val = _u32be(data, idx + off)
        if raw_val is None:
            continue
        hits.append({
            "kind": "tensho-meta",
            "block": block_addr,
            "addr": block_addr + off,
            "owner": owner,
            "slot": slot,
            "entry": f"+0x{off:02X} {name}",
            "raw": f"0x{raw_val:08X}",
            "target": f"{block_addr + off - block_addr:+#x}",
            "guess": "descriptor metadata, edit only after float tests",
            "score": score - 5,
            "ctx": _fmt_context(data, idx + off, mark_len=4),
            "editable": True if off in (0x00, 0x04) else False,
            "typ": "raw4",
        })


def _scan_tensho_descriptors(data: bytes, base_addr: int, slot_char_ids: dict[int, int], hits: list[dict]) -> None:
    # Scan for 0x0112/0x0113/0x0114 records with shared group 0x011F.
    seen_starts: set[int] = set()
    for desc_id in TENSHO_DESC_IDS:
        sig = struct.pack(">II", desc_id, TENSHO_DESC_GROUP)
        pos = 0
        while True:
            idx = data.find(sig, pos)
            if idx < 0:
                break
            pos = idx + 1
            if idx in seen_starts:
                continue
            if not _looks_like_tensho_descriptor(data, idx):
                continue
            seen_starts.add(idx)
            _append_tensho_descriptor(data, base_addr, idx, slot_char_ids, hits)


def _scan_block(data: bytes, base_addr: int, slot_char_ids: dict[int, int], hits: list[dict]) -> None:
    # Keep the main table focused: one active selector row per loaded assist table.
    # Move/normal presets are harvested on demand from the row chooser.
    _scan_chun_selector_graft_tables(data, base_addr, slot_char_ids, hits)
    _scan_ryu_selector_graft_tables(data, base_addr, slot_char_ids, hits)
    _scan_generic_selector_candidates(data, base_addr, slot_char_ids, hits)



def _append_route_candidates_for_character(
    owner_base: int,
    char_id: int | None,
    slot_char_ids: dict[int, int],
    hits: list[dict],
    *,
    allow_fallback: bool = True,
) -> None:
    """Append route candidates for exactly one loaded character owner window.

    This is a read-side optimization only. It reuses the existing proven row
    builders and does not change any selector/graft write payloads.

    The old scanner fed every character window through the Chun, Ryu, generic,
    VJoe, and Tatsunoko paths. That was safe but expensive. This dispatcher
    only calls the path that matches the loaded character. If that path does not
    find anything and allow_fallback=True, it falls back to the previous broad
    per-owner scan so functionality is preserved.
    """
    try:
        owner_base = int(owner_base)
    except Exception:
        return

    cid = 0
    try:
        cid = int(char_id or slot_char_ids.get(owner_base) or 0)
    except Exception:
        cid = 0
    if cid:
        slot_char_ids[owner_base] = cid

    before = len(hits)

    def _scan_owner_slice_with(kind: str) -> None:
        data = _read_mem_region_raw(owner_base, MOVE_PRESET_SLOT_SIZE)
        if not data:
            return
        if kind == "ryu":
            _scan_ryu_selector_graft_tables(data, owner_base, slot_char_ids, hits)
        elif kind == "chun":
            _scan_chun_selector_graft_tables(data, owner_base, slot_char_ids, hits)
        elif kind == "generic":
            _scan_generic_selector_candidates(data, owner_base, slot_char_ids, hits)
        else:
            _scan_block(data, owner_base, slot_char_ids, hits)

    try:
        if cid == VJOE_CHAR_ID:
            # VJoe's active-wrapper resolver is expensive because it may verify
            # chr_tbl candidates globally. Only run it when VJoe is actually
            # loaded for this owner slot.
            _append_vjoe_trampoline_profile(slot_char_ids, hits)
        elif _is_tatsunoko_direct_426_char_id(cid):
            # Tatsunoko pass characters use the early 426 wrapper graft row.
            _append_direct_426_profiles(slot_char_ids, hits)
        elif cid == 12:
            _scan_owner_slice_with("ryu")
        elif cid == 13:
            _scan_owner_slice_with("chun")
        elif cid:
            _scan_owner_slice_with("generic")
        else:
            # Unknown/missing ID: keep the old safe behavior for this one owner
            # window only.
            _scan_owner_slice_with("all")
            _append_vjoe_trampoline_profile(slot_char_ids, hits)
            _append_direct_426_profiles(slot_char_ids, hits)
    except Exception:
        pass

    if allow_fallback and len(hits) == before:
        # Preserve behavior if the character-gated path missed a weird route.
        # This is still only one owner slice, not the entire MEM2 sweep.
        try:
            _scan_owner_slice_with("all")
        except Exception:
            pass
        try:
            if cid == VJOE_CHAR_ID:
                _append_vjoe_trampoline_profile(slot_char_ids, hits)
        except Exception:
            pass
        try:
            if _is_tatsunoko_direct_426_char_id(cid):
                _append_direct_426_profiles(slot_char_ids, hits)
        except Exception:
            pass

def _run_scan(progress_cb, done_cb):
    if rbytes is None:
        done_cb([])
        return
    slot_char_ids = _read_slot_char_ids()
    hits: list[dict] = []

    # Fast path: scan only loaded character-table windows. main.py already
    # provides the live slot list through tick_assist_profiles_from_main(). If
    # the HUD has not fed snapshots yet, scan the four known slot windows. This
    # replaces the old default 0x90000000-0x94000000 sweep.
    owner_bases = _loaded_owner_bases_from_main() or list(_CHR_TBL_BASES)
    total = max(1, len(owner_bases))
    for n, owner_base in enumerate(owner_bases, start=1):
        try:
            cid = int(slot_char_ids.get(int(owner_base), 0) or 0)
        except Exception:
            cid = 0
        _append_route_candidates_for_character(
            int(owner_base),
            cid if cid else None,
            slot_char_ids,
            hits,
            allow_fallback=True,
        )
        progress_cb(n / total * 100.0)

    # Move presets are harvested on demand from the selector-lane chooser.
    # Character-specific route rows above already append VJoe/Tatsunoko rows
    # only when those characters are loaded.

    seen = set()
    uniq = []
    for h in hits:
        k = (h["kind"], h["addr"], h["block"], h["entry"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(h)

    # Keep the friendly flat view to one generic candidate per non-Ryu/non-Chun
    # character slot. Native/installed wins, then clean graft-candidate, then
    # loose tail-pair. This prevents the experimental fallback from spamming.
    # VJoe now has its own active-wrapper row; hide the older generic/static
    # VJoe row so the UI shows one VJoe profile only.
    vjoe_active_owner_bases = {
        int(h.get("owner_base") or 0)
        for h in uniq
        if h.get("kind") == "vjoe-active-selector-word"
        and int(h.get("owner_base") or 0)
    }
    direct_426_owner_bases = {
        int(h.get("owner_base") or 0)
        for h in uniq
        if h.get("kind") == "direct-426-word"
        and int(h.get("owner_base") or 0)
    }
    best_generic: dict[int, dict] = {}
    filtered = []
    for h in uniq:
        if h.get("kind") != "generic-selector-word":
            filtered.append(h)
            continue
        owner_base = _owning_chr_tbl(int(h.get("block", 0)))
        if owner_base is None:
            continue
        # Do not also show stale/old generic VJoe rows when the resolved
        # active-wrapper VJoe row is present for that slot.
        if int(owner_base) in vjoe_active_owner_bases:
            continue
        # Tatsunoko direct-426 rows are cleaner than anchored fallback for
        # Casshan/Tekkaman style routes, so hide the older generic/anchor row.
        if int(owner_base) in direct_426_owner_bases:
            continue
        old = best_generic.get(owner_base)
        if old is None:
            best_generic[owner_base] = h
            continue
        def pick_key(row: dict) -> tuple[int, int, int]:
            typ = str(row.get("typ", ""))
            source = str(row.get("source", ""))
            base_source = (
                source
                .replace("+assist-route", "")
                .replace("-assist-route", "")
            )

            # Preserve the working old-character selector path. A real
            # graft-candidate should beat the anchored tail-pair fallback even
            # if the tail-pair has the assist-route score bonus. For Alex-style
            # slots, the only useful route is the tail-pair assist wrapper, so
            # that still beats unrelated native selector-looking tables.
            if typ == "u32-generic-selector" and str(source or "").startswith("tatsunoko-wrapper-graft"):
                priority = 60
            elif typ == "u32-generic-selector" and base_source in ("graft-candidate", "installed"):
                priority = 50
            elif typ == "u32-generic-selector" and "assist-route" in source and base_source == "native":
                priority = 45
            elif typ == "u32-anchored-assist-fallback":
                priority = 40
            elif typ == "u32-generic-selector" and base_source == "native":
                priority = 30
            else:
                priority = 10

            return (priority, int(row.get("score", 0)), -int(row.get("block", 0)))

        if pick_key(h) > pick_key(old):
            best_generic[owner_base] = h
    filtered.extend(best_generic.values())
    uniq = filtered

    def sort_key(h: dict):
        priority = 0 if h["block"] == 0x908C7680 else 1
        kind_order = {
            "ryu-selector-word": 0,
            "chun-selector-word": 1,
            "chun-graft-table": 2,
            "ryu-graft-table": 3,
            "generic-native-table": 4,
            "generic-graft-table": 5,
            "generic-selector-word": 6,
            "vjoe-trampoline-word": 6,
            "vjoe-active-selector-word": 6,
            "direct-426-word": 6,
            "assist-move-preset": 7,
            "selector-chain": 8,
            "selector-loose": 5,
            "selector": 6,
            "confirmed-wrapper": 7,
            "confirmed-state-id": 8,
            "wrapper-poke": 9,
            "state-wrapper": 10,
            "state-id": 11,
            "phrase": 12,
        }.get(h["kind"], 13)
        return (priority, h["block"], kind_order, h["addr"], h["entry"])

    uniq.sort(key=sort_key)
    _assist_scan_cache_put(_assist_team_signature_from_main(), uniq)
    done_cb(uniq)


def update_assist_context_from_main(snaps: dict | None) -> None:
    """Receive the current HUD snapshots from main.py.

    Per-frame main.py calls must stay cheap. This function ONLY copies the
    live slot context into the assist module cache. It deliberately does not
    schedule route scans, VJoe discovery, generic selector scans, or chr_tbl
    walks from the frame loop. Route discovery now happens only on explicit
    scanner actions or on the first quick-assist click for that character.
    """
    global _MAIN_SNAPS_CACHE
    if not isinstance(snaps, dict):
        return
    clean: dict[str, dict] = {}
    for key, snap in snaps.items():
        if isinstance(snap, dict):
            try:
                clean[str(key)] = dict(snap)
            except Exception:
                pass
    _assist_note_main_snap_transitions(clean)
    with _MAIN_SNAPS_LOCK:
        _MAIN_SNAPS_CACHE = clean


def _main_snaps_snapshot() -> dict[str, dict]:
    with _MAIN_SNAPS_LOCK:
        return {k: dict(v) for k, v in _MAIN_SNAPS_CACHE.items()}


def _loaded_owner_bases_from_main() -> list[int]:
    snaps = _main_snaps_snapshot()
    out: list[int] = []
    for idx, label in enumerate(FIGHTER_SLOT_LABELS):
        snap = snaps.get(label)
        if not snap:
            continue
        if not snap.get("base"):
            continue
        if idx < len(_CHR_TBL_BASES):
            out.append(int(_CHR_TBL_BASES[idx]))
    seen: set[int] = set()
    uniq: list[int] = []
    for base in out:
        if base in seen:
            continue
        seen.add(base)
        uniq.append(base)
    return uniq



def _assist_team_signature_from_main() -> tuple:
    """Passive cache key for UI scan results.

    This is only an invalidation key. It does not participate in route
    selection, graft writes, VJoe logic, Tatsunoko logic, or duplicate-character
    runtime patching.
    """
    snaps = _main_snaps_snapshot()
    parts: list[tuple[str, int, int, int]] = []
    for idx, slot_label in enumerate(FIGHTER_SLOT_LABELS):
        snap = snaps.get(slot_label) if isinstance(snaps, dict) else None
        owner_base = int(_CHR_TBL_BASES[idx]) if idx < len(_CHR_TBL_BASES) else 0
        fighter_base = 0
        char_id = 0
        if isinstance(snap, dict):
            try:
                fighter_base = int(snap.get("base") or 0)
            except Exception:
                fighter_base = 0
            for field in ("id", "csv_char_id", "char_id"):
                try:
                    char_id = int(snap.get(field) or 0)
                except Exception:
                    char_id = 0
                if char_id:
                    break
        parts.append((str(slot_label), owner_base, fighter_base, char_id))
    return tuple(parts)


def _assist_scan_cache_get(signature: tuple | None = None) -> list[dict] | None:
    sig = signature if signature is not None else _assist_team_signature_from_main()
    with _ASSIST_SCAN_CACHE_LOCK:
        if _ASSIST_SCAN_CACHE_SIGNATURE != sig or not _ASSIST_SCAN_CACHE_HITS:
            return None
        return [dict(h) for h in _ASSIST_SCAN_CACHE_HITS]


def _assist_scan_cache_put(signature: tuple | None, hits: list[dict]) -> None:
    if signature is None:
        signature = _assist_team_signature_from_main()
    with _ASSIST_SCAN_CACHE_LOCK:
        global _ASSIST_SCAN_CACHE_SIGNATURE, _ASSIST_SCAN_CACHE_HITS
        _ASSIST_SCAN_CACHE_SIGNATURE = signature
        _ASSIST_SCAN_CACHE_HITS = [dict(h) for h in (hits or [])]


def _assist_snap_char_id(snap: dict | None) -> int:
    if not isinstance(snap, dict):
        return 0
    for field in ("id", "csv_char_id", "char_id"):
        try:
            cid = int(snap.get(field) or 0)
        except Exception:
            cid = 0
        if cid:
            return cid
    return 0


def _assist_snap_fighter_base(snap: dict | None) -> int:
    if not isinstance(snap, dict):
        return 0
    try:
        return int(snap.get("base") or 0)
    except Exception:
        return 0


def _assist_clear_slot_caches(slot_label: str, owner_base: int | None = None,
                              fighter_base: int | None = None, char_id: int | None = None) -> None:
    """Drop only stale read/profile caches for one HUD slot.

    This is intentionally outside the proven graft/write code. It prevents a
    route row from a previous match being reused after Dolphin rebuilds the same
    character's live assist scripts at a different address.
    """
    global _HEADLESS_LAST_PATCH_KEY

    if owner_base is None:
        try:
            owner_base = _slot_owner_base_from_label(slot_label)
        except Exception:
            owner_base = None

    owner = int(owner_base or 0)
    fighter = int(fighter_base or 0)

    with _QUICK_ROUTE_LOCK:
        if owner:
            for key in list(_QUICK_ROUTE_CACHE.keys()):
                if int(key[0]) == owner:
                    _QUICK_ROUTE_CACHE.pop(key, None)
            for key in list(_QUICK_ROUTE_INFLIGHT):
                if int(key[0]) == owner:
                    _QUICK_ROUTE_INFLIGHT.discard(key)
            for key in list(_QUICK_ROUTE_FAIL_UNTIL.keys()):
                if int(key[0]) == owner:
                    _QUICK_ROUTE_FAIL_UNTIL.pop(key, None)

    if owner:
        _HEADLESS_ASSIST_PROFILES.pop(owner, None)
    if fighter:
        _HEADLESS_ASSIST_PROFILES.pop(fighter, None)
        _HEADLESS_LAST_ASSIST_ATTACK_BY_BASE.pop(fighter, None)

    _HEADLESS_LAST_PATCH_KEY = None

    inst = _inst
    if inst is not None:
        try:
            if owner:
                inst._slot_assist_profiles.pop(owner, None)
            if fighter:
                inst._slot_assist_profiles.pop(fighter, None)
                inst._main_last_assist_attack_by_base.pop(fighter, None)
                inst._main_patch_latch_by_base.pop(fighter, None)
            inst._main_last_patch_key = None
        except Exception:
            pass


def _assist_note_main_snap_transitions(clean_snaps: dict[str, dict]) -> None:
    """Invalidate route/profile caches when main.py sees a team transition.

    main.py is already the reliable slot source. Use it only as a stale-cache
    detector: valid slot stayed same -> keep caches; slot invalid/changed/missing
    -> bump epoch and clear that slot's cached route/profile. This fixes the
    same-character-next-match case without scanning every frame.
    """
    global _ASSIST_CACHE_EPOCH

    changed = False
    for slot_label in FIGHTER_SLOT_LABELS:
        snap = clean_snaps.get(slot_label)
        owner_base = _slot_owner_base_from_label(slot_label)
        fighter_base = _assist_snap_fighter_base(snap)
        char_id = _assist_snap_char_id(snap)
        valid = bool(fighter_base and char_id in CHAR_ID_TO_KEY)
        prev = _ASSIST_LAST_SLOT_SIGNATURES.get(slot_label)

        if not valid:
            if prev is not None:
                _assist_clear_slot_caches(slot_label, owner_base, prev[0], prev[1])
                _ASSIST_LAST_SLOT_SIGNATURES.pop(slot_label, None)
                changed = True
            elif owner_base is not None and char_id and char_id not in CHAR_ID_TO_KEY:
                # Transitional garbage IDs are a strong signal that the match /
                # character memory is being rebuilt. Clear this owner even if the module
                # did not have a previous valid signature in this process.
                _assist_clear_slot_caches(slot_label, owner_base, fighter_base, char_id)
                changed = True
            continue

        cur = (int(fighter_base), int(char_id))
        if prev != cur:
            if prev is not None:
                _assist_clear_slot_caches(slot_label, owner_base, prev[0], prev[1])
            _assist_clear_slot_caches(slot_label, owner_base, fighter_base, char_id)
            _ASSIST_LAST_SLOT_SIGNATURES[slot_label] = cur
            changed = True
            # If this slot already has a selected quick assist, prewarm the new
            # route in the background.  Do not re-apply from the HUD loop; just
            # make the cached route ready for the next assist entry.
            try:
                prof = _HEADLESS_SLOT_ASSIST_PROFILES.get(str(slot_label))
                if isinstance(prof, dict) and _profile_matches_char(prof, int(char_id)) and owner_base is not None:
                    _schedule_quick_route_prewarm(int(owner_base), int(char_id))
            except Exception:
                pass

    if changed:
        _ASSIST_CACHE_EPOCH += 1
        _ASSIST_STATE_SYNC_CACHE.clear()
        with _ASSIST_SCAN_CACHE_LOCK:
            global _ASSIST_SCAN_CACHE_SIGNATURE, _ASSIST_SCAN_CACHE_HITS
            _ASSIST_SCAN_CACHE_SIGNATURE = None
            _ASSIST_SCAN_CACHE_HITS = []


def _slot_owner_base_from_label(slot_label: str | None) -> int | None:
    if not slot_label:
        return None
    try:
        idx = FIGHTER_SLOT_LABELS.index(str(slot_label))
    except ValueError:
        return None
    if idx < 0 or idx >= len(_CHR_TBL_BASES):
        return None
    return int(_CHR_TBL_BASES[idx])



def _quick_route_key(owner_base: int | None, char_id: int | None = None) -> tuple[int, int] | None:
    if owner_base is None:
        return None
    try:
        return (int(owner_base), int(char_id or 0))
    except Exception:
        return None


def _quick_route_cache_get(owner_base: int | None, char_id: int | None = None) -> dict | None:
    key = _quick_route_key(owner_base, char_id)
    if key is None:
        return None
    with _QUICK_ROUTE_LOCK:
        row = _QUICK_ROUTE_CACHE.get(key)
        if not row:
            return None
        try:
            if int(row.get("_assist_epoch", -1)) != int(_ASSIST_CACHE_EPOCH):
                _QUICK_ROUTE_CACHE.pop(key, None)
                return None
        except Exception:
            _QUICK_ROUTE_CACHE.pop(key, None)
            return None
        return dict(row)


def _quick_route_cache_put(owner_base: int | None, char_id: int | None, row: dict | None) -> None:
    key = _quick_route_key(owner_base, char_id)
    if key is None or not row:
        return
    cached = dict(row)
    cached["_assist_epoch"] = int(_ASSIST_CACHE_EPOCH)
    with _QUICK_ROUTE_LOCK:
        _QUICK_ROUTE_CACHE[key] = cached


def _schedule_quick_route_prewarm(owner_base: int | None, char_id: int | None = None) -> None:
    key = _quick_route_key(owner_base, char_id)
    if key is None:
        return

    now = time.monotonic()
    with _QUICK_ROUTE_LOCK:
        if key in _QUICK_ROUTE_CACHE or key in _QUICK_ROUTE_INFLIGHT:
            return
        # Do not let failed background warms hammer MEM2 every HUD frame. A
        # manual quick-assist click still resolves immediately; this only
        # throttles background prewarm.
        if float(_QUICK_ROUTE_FAIL_UNTIL.get(key, 0.0)) > now:
            return
        # Serialize heavy background reads. main.py calls this every frame, so
        # queued slots naturally get their turn on following frames.
        if len(_QUICK_ROUTE_INFLIGHT) >= _QUICK_ROUTE_MAX_INFLIGHT:
            return
        _QUICK_ROUTE_INFLIGHT.add(key)

    def _worker() -> None:
        row = None
        failed = False
        try:
            row = _resolve_quick_route_row_uncached(key[0], key[1] if key[1] else None)
        except Exception as e:
            failed = True
            _assist_log_once(
                f"prewarm-failed:{key[0]}:{key[1]}",
                f"[assist quick] route prewarm failed for 0x{key[0]:08X}/cid {key[1]}: {e!r}",
                interval=2.0,
            )
        with _QUICK_ROUTE_LOCK:
            if row:
                cached = dict(row)
                cached["_assist_epoch"] = int(_ASSIST_CACHE_EPOCH)
                _QUICK_ROUTE_CACHE[key] = cached
                _QUICK_ROUTE_FAIL_UNTIL.pop(key, None)
            else:
                _QUICK_ROUTE_FAIL_UNTIL[key] = time.monotonic() + _QUICK_ROUTE_FAIL_TTL_SECONDS
            _QUICK_ROUTE_INFLIGHT.discard(key)

    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception:
        with _QUICK_ROUTE_LOCK:
            _QUICK_ROUTE_INFLIGHT.discard(key)
            _QUICK_ROUTE_FAIL_UNTIL[key] = time.monotonic() + _QUICK_ROUTE_FAIL_TTL_SECONDS

def _prewarm_quick_routes_from_snaps(snaps: dict[str, dict]) -> None:
    if not isinstance(snaps, dict):
        return
    for slot_label, snap in snaps.items():
        if not isinstance(snap, dict) or not snap.get("base"):
            continue
        owner_base = _slot_owner_base_from_label(str(slot_label))
        if owner_base is None:
            continue
        char_id = 0
        for field in ("id", "csv_char_id", "char_id"):
            try:
                char_id = int(snap.get(field) or 0)
            except Exception:
                char_id = 0
            if char_id:
                break
        _schedule_quick_route_prewarm(owner_base, char_id if char_id else None)


def _quick_assist_paths() -> list[str]:
    folders: list[str] = []
    try:
        folders.append(resource_path())
    except Exception:
        pass
    try:
        folders.append(os.getcwd())
    except Exception:
        pass
    paths: list[str] = []
    seen: set[str] = set()
    for folder in folders:
        if not folder:
            continue
        for filename in QUICK_ASSISTS_FILE_CANDIDATES:
            path = filename if os.path.isabs(filename) else os.path.join(folder, filename)
            norm = os.path.normcase(os.path.abspath(path))
            if norm in seen:
                continue
            seen.add(norm)
            paths.append(path)
    return paths


def _load_quick_assists() -> dict:
    global _QUICK_ASSISTS_CACHE
    paths = _quick_assist_paths()
    existing = next((p for p in paths if os.path.isfile(p)), None)
    if not existing:
        # Do not make the HUD buttons disappear just because the JSON file was
        # not copied yet. Main stays dumb; the assist module supplies a safe
        # four-button fallback.
        return dict(DEFAULT_QUICK_ASSISTS)
    try:
        mtime = os.path.getmtime(existing)
    except Exception:
        mtime = 0.0
    if _QUICK_ASSISTS_CACHE is not None and _QUICK_ASSISTS_CACHE[0] == mtime:
        return dict(_QUICK_ASSISTS_CACHE[1])
    try:
        with open(existing, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        try:
            print(f"[assist quick] could not load {existing}: {e!r}")
        except Exception:
            pass
        data = {}
    if not isinstance(data, dict):
        data = {}
    if "default" not in data:
        data["default"] = list(DEFAULT_QUICK_ASSISTS["default"])
    _QUICK_ASSISTS_CACHE = (mtime, data)
    return dict(data)


def _quick_char_keys_from_snap(snap: dict | None) -> list[str]:
    if not isinstance(snap, dict):
        return []
    keys: list[str] = []
    for field in ("name", "char_name"):
        val = snap.get(field)
        if val:
            keys.append(str(val))
    for field in ("id", "csv_char_id", "char_id"):
        try:
            cid = int(snap.get(field))
        except Exception:
            continue
        if cid in CHAR_ID_TO_KEY:
            keys.append(str(CHAR_ID_TO_KEY[cid]))
        keys.append(str(cid))
        keys.append(f"0x{cid:02X}")
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if not key:
            continue
        norm = key.strip().lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(key.strip())
    return out


def get_quick_assists_for_slot(slot_label: str, snap: dict | None = None) -> list[dict]:
    'Return up to four configured quick assists for a HUD slot.\n\n    main.py can call this to draw buttons. It returns display data only; assist\n    route resolution stays inside this module when apply_quick_assist_from_main\n    is called.\n    '
    data = _load_quick_assists()
    if not data:
        return []
    section = None
    for key in _quick_char_keys_from_snap(snap):
        if key in data:
            section = data.get(key)
            break
        for actual_key in data.keys():
            if str(actual_key).strip().lower() == key.strip().lower():
                section = data.get(actual_key)
                break
        if section is not None:
            break
    if section is None and isinstance(data.get("default"), list):
        section = data.get("default")
    if not isinstance(section, list):
        return []
    out: list[dict] = []
    for i, item in enumerate(section[:4]):
        if isinstance(item, str):
            item = {"label": item, "name": item}
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or item.get("move") or f"Assist {i + 1}")
        row = dict(item)
        row["label"] = label
        out.append(row)
    return out[:4]


def _generic_block_args_and_words_module(graft_addr: int, source: str,
                                         raw_val: int, apply_all: bool,
                                         lane_addr: int) -> bytes:
    try:
        current = _read_mem_region_raw(graft_addr, GENERIC_SELECTOR_TABLE_LEN)
    except Exception:
        current = b""

    words = [raw_val, raw_val, raw_val]
    arg32 = GENERIC_SELECTOR_DEFAULT_ARG_32
    arg33 = GENERIC_SELECTOR_DEFAULT_ARG_33

    if len(current) == GENERIC_SELECTOR_TABLE_LEN and current.startswith(GENERIC_SELECTOR_SETUP_PREFIX):
        for i, off in enumerate(GENERIC_SELECTOR_WORD_OFFSETS):
            old = struct.unpack(">I", current[off:off + 4])[0]
            words[i] = raw_val if (apply_all or graft_addr + off == lane_addr) else old
        arg32 = current[0x20:0x24]
        arg33 = current[0x28:0x2C]
    elif len(current) == GENERIC_SELECTOR_TABLE_LEN and current[0:4] == b"\x0F\x06\x00\x27" and current[4:8] == SELECTOR_TAIL:
        words = [raw_val, raw_val, raw_val]
        arg32 = current[0x08:0x0C]
        arg33 = current[0x10:0x14]
    elif len(current) == GENERIC_SELECTOR_TABLE_LEN and current[0x1C:0x20] == SELECTOR_TAIL and current[0x24:0x28] == SELECTOR_COMPANION_TAIL:
        words = [raw_val, raw_val, raw_val]
        arg32 = current[0x20:0x24]
        arg33 = current[0x28:0x2C]

    return _build_generic_selector_graft_block(tuple(words), arg32, arg33)


def _selector_writes_for_row_module(row: dict, raw_val: int | None) -> dict[int, bytes]:
    if not row:
        return {}
    graft_addr = int(row.get("block", 0))
    lane_addr = int(row.get("addr", graft_addr))
    typ = str(row.get("typ", ""))

    if raw_val is None:
        if typ == "u32-chun-selector":
            return {graft_addr: CHUN_SELECTOR_ORIGINAL_BLOCK}
        if typ == "u32-ryu-selector-graft":
            return {graft_addr: RYU_SELECTOR_GRAFT_BLOCK}
        if typ == "u32-anchored-assist-fallback":
            owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
            table_index = int(row.get("anchor_table_index") or ANCHOR_ASSIST_FALLBACK_TABLE_INDEX)
            default_word = int(row.get("anchor_default_word") or 0) or None
            operand_offsets = list(row.get("anchor_operand_offsets") or [])
            operand_words = list(row.get("anchor_operand_words") or [])
            writes = _anchored_assist_fallback_writes(owner_base, None, table_index, default_word, operand_offsets, operand_words)
            writes.update(_assist_state_sync_writes(row, None))
            return writes
        if typ == "u32-generic-selector":
            raw_hex = str(row.get("table_raw", "")).replace(" ", "")
            try:
                block = bytes.fromhex(raw_hex)
            except Exception:
                block = b""
            if len(block) == GENERIC_SELECTOR_TABLE_LEN:
                writes = {graft_addr: block}
                owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
                if int(row.get("char_id") or 0) == VJOE_CHAR_ID or _is_vjoe_owner_base(owner_base):
                    writes.update(_vjoe_animation_state_writes(owner_base, None, dict(row.get("vjoe_info") or {})))
                else:
                    writes.update(_assist_state_sync_writes(row, None))
                return writes
            return {}
        if typ == "u32-vjoe-trampoline":
            owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
            return _vjoe_trampoline_writes(owner_base, None, dict(row.get("vjoe_info") or {}))
        if typ == "u32-direct-426-fallback":
            owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
            writes = _direct_426_writes(owner_base, None, dict(row.get("direct_426_info") or {}))
            writes.update(_assist_state_sync_writes(row, None))
            return writes
        return {}

    payload = struct.pack(">I", int(raw_val) & 0xFFFFFFFF)

    if typ == "u32-chun-selector":
        writes: dict[int, bytes] = {graft_addr: CHUN_SELECTOR_GRAFT_BLOCK}
        for off in CHUN_SELECTOR_WORD_OFFSETS:
            writes[graft_addr + off] = payload
        return writes

    if typ == "u32-ryu-selector-graft":
        writes = {graft_addr: RYU_SELECTOR_GRAFT_BLOCK}
        for off in RYU_SELECTOR_WORD_OFFSETS:
            writes[graft_addr + off] = payload
        return writes

    if typ == "u32-anchored-assist-fallback":
        owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
        table_index = int(row.get("anchor_table_index") or ANCHOR_ASSIST_FALLBACK_TABLE_INDEX)
        default_word = int(row.get("anchor_default_word") or 0) or None
        operand_offsets = list(row.get("anchor_operand_offsets") or [])
        operand_words = list(row.get("anchor_operand_words") or [])
        writes = _anchored_assist_fallback_writes(owner_base, int(raw_val), table_index, default_word, operand_offsets, operand_words)
        writes.update(_assist_state_sync_writes(row, int(raw_val)))
        return writes

    if typ == "u32-generic-selector":
        source = str(row.get("source", "unknown"))
        block = _generic_block_args_and_words_module(graft_addr, source, int(raw_val), True, lane_addr)
        writes = {graft_addr: block}
        owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
        if int(row.get("char_id") or 0) == VJOE_CHAR_ID or _is_vjoe_owner_base(owner_base):
            writes.update(_vjoe_animation_state_writes(owner_base, int(raw_val), dict(row.get("vjoe_info") or {})))
        else:
            writes.update(_assist_state_sync_writes(row, int(raw_val)))
        return writes

    if typ == "u32-vjoe-trampoline":
        owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
        return _vjoe_trampoline_writes(owner_base, int(raw_val), dict(row.get("vjoe_info") or {}))

    if typ == "u32-direct-426-fallback":
        owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
        writes = _direct_426_writes(owner_base, int(raw_val), dict(row.get("direct_426_info") or {}))
        writes.update(_assist_state_sync_writes(row, int(raw_val)))
        return writes

    return {}


def _write_many_module(
    writes: dict[int, bytes],
    *,
    urgent: bool = False,
    key: str = "assist:module",
) -> bool:
    if not writes:
        return True
    if _rpm is not None:
        try:
            return bool(_rpm.write_many(
                writes,
                key=key,
                priority=98 if urgent else 70,
                # Urgent assist graft writes are time-critical.  Do not spend
                # the assist-entry frame reading the current bytes first; write
                # the requested graft now and let RuntimePatchManager record it.
                dirty=False if urgent else True,
                force=True if urgent else False,
                cache_ttl_sec=0.0,
            ))
        except Exception:
            pass
    if wbytes is None:
        return False
    for addr, payload in writes.items():
        try:
            if not bool(wbytes(int(addr), payload)):
                return False
        except Exception:
            return False
    return True


def _volnutt_weapon_value_from_preset(preset: dict | None) -> int | None:
    if not isinstance(preset, dict):
        return None

    for key in ("volnutt_weapon", "weapon", "weapon_id", "arm"):
        if key not in preset:
            continue
        value = preset.get(key)
        if isinstance(value, int):
            if 0 <= int(value) <= 3:
                return int(value)
            return None
        text = str(value or "").strip()
        parsed = _parse_int_loose(text)
        if parsed is not None:
            if 0 <= int(parsed) <= 3:
                return int(parsed)
            return None
        norm = text.lower().replace("_", " ").strip()
        if norm in VOLNUTT_WEAPON_ALIASES:
            return int(VOLNUTT_WEAPON_ALIASES[norm])

    # Allow labels like "Gun 6B" or "Shield Assist" without requiring a
    # separate JSON field. This is Volnutt-only and only used by the Volnutt path.
    label = str(preset.get("label") or preset.get("name") or "").lower()
    for name, val in VOLNUTT_WEAPON_ALIASES.items():
        if name and name in label:
            return int(val)
    return None


def _volnutt_find_wildcard_offsets(data: bytes, head: bytes, tail: bytes) -> list[int]:
    out: list[int] = []
    if not data or not head or not tail:
        return out
    pos = 0
    while True:
        j = data.find(head, pos)
        if j < 0:
            break
        wildcard_off = j + len(head)
        tail_start = wildcard_off + 1
        if tail_start + len(tail) <= len(data) and data[tail_start:tail_start + len(tail)] == tail:
            current = data[wildcard_off]
            # This byte is the observed weapon selector: 00 Arm, 01 Drill,
            # 02 Gun, 03 Shield. Reject anything else so do not patch random
            # matching script bytes.
            if current in (0, 1, 2, 3):
                out.append(wildcard_off)
        pos = j + 1
    return out


def _volnutt_weapon_writes(owner_base: int | None, weapon_value: int | None, row: dict | None = None) -> dict[int, bytes]:
    """Return Volnutt weapon-byte writes for the resolved assist route.

    Volnutt's 6B assist body is controlled by weapon selector bytes:
        00 Arm, 01 Drill, 02 Gun, 03 Shield

    The active latch sits immediately before the chr_tbl[426] wrapper in the
    current dumps. Do not only scan from wrapper_addr forward; that misses the
    tag-in/weapon-in block and leaves Drill stuck. This stays Volnutt-only and
    only patches the exact observed Volnutt weapon-selector byte shapes.
    """
    try:
        owner = int(owner_base or 0)
        weapon = int(weapon_value)
    except Exception:
        return {}
    if owner <= 0 or weapon not in (0, 1, 2, 3):
        return {}

    chr_tbl_base = 0
    if isinstance(row, dict):
        try:
            chr_tbl_base = int(row.get("chr_tbl_base") or 0)
        except Exception:
            chr_tbl_base = 0
    if not chr_tbl_base:
        chr_tbl_base = _chr_tbl_base_for_owner_base(owner) or 0
    if not chr_tbl_base:
        return {}

    def _find_setup_offsets(blob: bytes) -> set[int]:
        out: set[int] = set()
        if not blob:
            return out
        # 04 15 ... 46 04 ... 02 / 04 01 ... 46 00 ... XX / 04 02 ... 45 FC ...
        out.update(_volnutt_find_wildcard_offsets(
            blob,
            VOLNUTT_WEAPON_SETUP_HEAD,
            VOLNUTT_WEAPON_SETUP_TAIL,
        ))
        return out

    def _find_assist_start_loose_offsets(blob: bytes) -> set[int]:
        out: set[int] = set()
        if not blob:
            return out

        head = VOLNUTT_WEAPON_ASSIST_START_HEAD
        # The original strict tail included the continuation target. The graft
        # changes that target, so only require the stable call opcode after the
        # weapon byte: 01 33 00 00.
        loose_tail = b"\x01\x33\x00\x00"

        pos = 0
        while True:
            j = blob.find(head, pos)
            if j < 0:
                break
            wildcard_off = j + len(head)
            tail_start = wildcard_off + 1
            if tail_start + len(loose_tail) <= len(blob) and blob[tail_start:tail_start + len(loose_tail)] == loose_tail:
                current = blob[wildcard_off]
                if current in (0, 1, 2, 3):
                    out.add(wildcard_off)
            pos = j + 1
        return out

    def _find_all_volnutt_weapon_offsets(blob: bytes) -> set[int]:
        out: set[int] = set()
        out.update(_find_assist_start_loose_offsets(blob))
        out.update(_find_setup_offsets(blob))
        return out

    def _add_region(writes: dict[int, bytes], start_addr: int, size: int, label: str) -> None:
        try:
            start = int(start_addr)
            n = int(size)
        except Exception:
            return
        if start <= 0 or n <= 0:
            return
        data = _read_mem_region_raw(start, n)
        if not data:
            return

        payload = bytes([weapon & 0xFF])
        for off in sorted(_find_all_volnutt_weapon_offsets(data)):
            writes[start + int(off)] = payload

    writes: dict[int, bytes] = {}

    default_word = None
    if isinstance(row, dict):
        for info_key in ("direct_426_info", "vjoe_info"):
            info = row.get(info_key)
            if isinstance(info, dict) and info.get("default_word") is not None:
                try:
                    default_word = int(info.get("default_word"))
                    break
                except Exception:
                    default_word = None
    if default_word is None:
        default_word = _chr_tbl_word_for_owner_base(owner, ANCHOR_ASSIST_FALLBACK_TABLE_INDEX)

    if default_word is not None:
        default_word = int(default_word) & 0xFFFFFFFF
        if 0 < default_word < MOVE_PRESET_SLOT_SIZE:
            wrapper_addr = int(chr_tbl_base) + default_word

            # Critical target from the before/after Volnutt dump:
            # the weapon-in assist block is just BEFORE the table[426] wrapper.
            # Scan a small pre-wrapper window plus the wrapper start. This catches
            # both active bytes around wrapper-0xA1 and wrapper-0x79 without
            # touching the static move-library copies elsewhere in the slot.
            _add_region(writes, max(0, wrapper_addr - 0x140), 0x340, "426-prewrapper")

            # Keep the old forward scan too, but it is no longer the only target.
            _add_region(writes, wrapper_addr, VOLNUTT_WEAPON_WRAPPER_SCAN_SIZE, "426-wrapper")

    # Row/graft-local backup. This remains route-local, not a broad full-slot
    # scan, and catches weird cached rows where block is the pre-wrapper region.
    if isinstance(row, dict):
        try:
            block_addr = int(row.get("block") or 0)
        except Exception:
            block_addr = 0
        if block_addr:
            _add_region(writes, max(0, block_addr - 0x800), 0x4800, "row-neighborhood")

    # Selected 6B body backup. Since all Volnutt quick assists use table 270,
    # the selected body may also carry a copy of the weapon byte.
    try:
        table270 = _read_mem_region_raw(int(chr_tbl_base) + (VOLNUTT_6B_TABLE_INDEX * 4), 4)
        if table270 and len(table270) == 4:
            move_word = struct.unpack(">I", table270)[0]
            if 0 < int(move_word) < MOVE_PRESET_SLOT_SIZE:
                _add_region(writes, int(chr_tbl_base) + int(move_word), VOLNUTT_WEAPON_WRAPPER_SCAN_SIZE, "6B-body")
    except Exception:
        pass

    if writes:
        try:
            addr_list = ", ".join(f"0x{int(a):08X}" for a in sorted(writes.keys())[:8])
            more = "" if len(writes) <= 8 else f" +{len(writes) - 8} more"
            print(f"[assist quick] Volnutt weapon {weapon} writes={len(writes)} {addr_list}{more}")
        except Exception:
            pass
        return writes

    # Last-resort diagnostic fallback only. If this prints zero, the known state is the
    # latch is not stored in the observed script-literal shapes.
    try:
        print(f"[assist quick] Volnutt weapon {weapon} writes=0")
    except Exception:
        pass
    return {}


def _resolve_quick_route_row_uncached(owner_base: int, char_id: int | None = None) -> dict | None:
    slot_char_ids = _read_slot_char_ids()
    try:
        owner_base = int(owner_base)
    except Exception:
        return None
    if char_id is not None and int(char_id) > 0:
        slot_char_ids[owner_base] = int(char_id)

    hits: list[dict] = []
    _append_route_candidates_for_character(
        owner_base,
        int(char_id) if char_id is not None and int(char_id) > 0 else slot_char_ids.get(owner_base),
        slot_char_ids,
        hits,
        allow_fallback=True,
    )

    route_types = {
        "u32-chun-selector",
        "u32-ryu-selector-graft",
        "u32-generic-selector",
        "u32-anchored-assist-fallback",
        "u32-vjoe-trampoline",
        "u32-direct-426-fallback",
    }
    candidates: list[dict] = []
    for h in hits:
        if str(h.get("typ", "")) not in route_types:
            continue
        h_owner = int(h.get("owner_base") or 0)
        if not h_owner:
            h_owner = _owning_chr_tbl(int(h.get("block", h.get("addr", 0)))) or 0
        if int(h_owner) != int(owner_base):
            continue
        h = dict(h)
        h["owner_base"] = int(owner_base)
        if char_id is not None:
            h["char_id"] = int(char_id)
        candidates.append(h)

    if not candidates:
        return None

    def rank(row: dict) -> tuple[int, int, int]:
        typ = str(row.get("typ", ""))
        source = str(row.get("source", ""))
        cid = int(row.get("char_id") or char_id or 0)
        pri = 0
        if cid == VJOE_CHAR_ID or source.startswith("vjoe-active"):
            pri = 100
        elif source.startswith("tatsunoko-wrapper-graft") or row.get("direct_426_info"):
            pri = 90
        elif typ == "u32-chun-selector":
            pri = 80
        elif typ == "u32-ryu-selector-graft":
            pri = 80
        elif typ == "u32-generic-selector" and "graft" in source:
            pri = 70
        elif typ == "u32-anchored-assist-fallback":
            pri = 60
        else:
            pri = 50
        return (pri, int(row.get("score", 0)), -int(row.get("block", 0)))

    candidates.sort(key=rank, reverse=True)
    return _assist_state_sync_attach(candidates[0])


def _resolve_quick_route_row(owner_base: int, char_id: int | None = None) -> dict | None:
    cached = _quick_route_cache_get(owner_base, char_id)
    if cached:
        return cached

    row = _resolve_quick_route_row_uncached(owner_base, char_id)
    if row:
        _quick_route_cache_put(owner_base, char_id, row)
    return row


def _selector_word_for_quick_preset(owner_base: int, row: dict, preset: dict) -> int | None | str:
    if bool(preset.get("default")):
        return None
    for key in ("word", "selector_word", "raw"):
        if key in preset:
            val = _parse_int_loose(str(preset.get(key)))
            if val is not None:
                return int(val) & 0xFFFFFFFF
    table = preset.get("table", preset.get("table_index", preset.get("entry")))
    if table is None and _volnutt_weapon_value_from_preset(preset) is not None:
        table = VOLNUTT_6B_TABLE_INDEX
    table_index = _parse_int_loose(str(table)) if table is not None else None
    if table_index is None:
        name = str(preset.get("name") or preset.get("move") or preset.get("label") or "")
        if name.strip().lower() == "default":
            return None
        return "NO_TABLE"

    chr_tbl_base = int(row.get("chr_tbl_base") or 0)
    if not chr_tbl_base and row.get("vjoe_info"):
        chr_tbl_base = int((row.get("vjoe_info") or {}).get("chr_tbl_base") or 0)
    if not chr_tbl_base and row.get("direct_426_info"):
        chr_tbl_base = int((row.get("direct_426_info") or {}).get("chr_tbl_base") or 0)
    if not chr_tbl_base:
        chr_tbl_base = _chr_tbl_base_for_owner_base(owner_base) or 0
    if not chr_tbl_base:
        return "NO_CHR_TBL"
    data = _read_mem_region_raw(chr_tbl_base + (int(table_index) * 4), 4)
    if not data or len(data) != 4:
        return "NO_ENTRY"
    entry = struct.unpack(">I", data)[0]
    if entry in (0, 0xFFFFFFFF):
        return "NO_ENTRY"
    delta = _parse_int_loose(str(preset.get("offset", preset.get("delta", 0)))) or 0
    return (int(entry) + int(delta)) & 0xFFFFFFFF



def _quick_assist_lane_start() -> None:
    global _QUICK_ASSIST_LANE_STARTED
    with _QUICK_ASSIST_LANE_LOCK:
        if _QUICK_ASSIST_LANE_STARTED:
            return
        _QUICK_ASSIST_LANE_STARTED = True

    def _worker() -> None:
        while True:
            _QUICK_ASSIST_LANE_EVENT.wait()
            while True:
                with _QUICK_ASSIST_LANE_LOCK:
                    if not _QUICK_ASSIST_LANE_ORDER:
                        _QUICK_ASSIST_LANE_EVENT.clear()
                        break
                    slot = _QUICK_ASSIST_LANE_ORDER.pop(0)
                    job = _QUICK_ASSIST_LANE_PENDING.pop(slot, None)
                if not job:
                    continue

                slot_label = str(job.get("slot_label") or slot)
                try:
                    quick_index = int(job.get("quick_index") or 0)
                except Exception:
                    quick_index = 0
                snap = job.get("snap") if isinstance(job.get("snap"), dict) else None
                label = str(job.get("label") or f"quick {quick_index}")
                seq = int(job.get("seq") or 0)
                t0 = time.perf_counter()
                ok = False
                err = ""
                try:
                    ok = bool(_apply_quick_assist_sync(slot_label, quick_index, snap, quiet=True))
                except Exception as e:
                    err = repr(e)
                    ok = False
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                with _QUICK_ASSIST_LANE_LOCK:
                    _QUICK_ASSIST_LANE_LAST_RESULT[slot_label] = {
                        "ok": bool(ok),
                        "label": label,
                        "quick_index": int(quick_index),
                        "seq": int(seq),
                        "elapsed_ms": float(elapsed_ms),
                        "error": err,
                        "time": time.time(),
                    }
                try:
                    if ok:
                        print(f"[assist quick lane] applied {slot_label} -> {label} in {elapsed_ms:.1f}ms")
                    else:
                        suffix = f" error={err}" if err else ""
                        print(f"[assist quick lane] failed {slot_label} -> {label} in {elapsed_ms:.1f}ms{suffix}")
                except Exception:
                    pass

    try:
        threading.Thread(target=_worker, name="quick-assist-lane", daemon=True).start()
    except Exception:
        with _QUICK_ASSIST_LANE_LOCK:
            _QUICK_ASSIST_LANE_STARTED = False


def _queue_quick_assist_job(slot_label: str, quick_index: int, snap: dict | None, preset: dict, quiet: bool = False) -> None:
    global _QUICK_ASSIST_LANE_SEQ
    label = str(preset.get("label") or preset.get("name") or preset.get("move") or f"quick {quick_index}")
    clean_snap = dict(snap) if isinstance(snap, dict) else None

    # During or immediately before an assist-entry window, do not
    # let the old edge latch suppress the first real graft after the worker
    # resolves the route.  This was the source of the "loaded, but grafts on the
    # next assist 8 seconds later" feel.
    try:
        fighter_base = int((clean_snap or {}).get("base") or 0)
        if fighter_base:
            _HEADLESS_LAST_ASSIST_ATTACK_BY_BASE.pop(fighter_base, None)
            _HEADLESS_LAST_RUNTIME_WRITE_BY_SLOT.pop(str(slot_label), None)
    except Exception:
        pass

    with _QUICK_ASSIST_LANE_LOCK:
        _QUICK_ASSIST_LANE_SEQ += 1
        job = {
            "slot_label": str(slot_label),
            "quick_index": int(quick_index),
            "snap": clean_snap,
            "label": label,
            "seq": int(_QUICK_ASSIST_LANE_SEQ),
            "queued_at": time.time(),
            "priority": 100,
        }
        if str(slot_label) in _QUICK_ASSIST_LANE_ORDER:
            try:
                _QUICK_ASSIST_LANE_ORDER.remove(str(slot_label))
            except Exception:
                pass
        _QUICK_ASSIST_LANE_ORDER.insert(0, str(slot_label))
        _QUICK_ASSIST_LANE_PENDING[str(slot_label)] = job
        _QUICK_ASSIST_LANE_EVENT.set()
    _quick_assist_lane_start()
    if not quiet:
        try:
            print(f"[assist quick lane] queued urgent {slot_label} -> {label}")
        except Exception:
            pass


def apply_quick_assist_from_main(slot_label: str, quick_index: int, snap: dict | None = None, quiet: bool = False) -> bool:
    """Queue one JSON quick-assist entry without blocking the HUD/main UI lane.

    The old path resolved routes and wrote memory synchronously in the mouse
    click handler.  That made cold/stale route clicks visibly pause the main
    GUI.  This wrapper validates the request, stores it on the dedicated quick
    assist lane, and returns True immediately so the UI can animate/flash.
    """
    owner_base = _slot_owner_base_from_label(slot_label)
    if owner_base is None:
        return False
    quicks = get_quick_assists_for_slot(slot_label, snap)
    if quick_index < 0 or quick_index >= len(quicks):
        return False
    preset = dict(quicks[quick_index])

    char_id = 0
    if isinstance(snap, dict):
        for field in ("id", "csv_char_id", "char_id"):
            try:
                char_id = int(snap.get(field) or 0)
            except Exception:
                char_id = 0
            if char_id:
                break

    # Do not launch a competing background prewarm from the click handler.  The
    # dedicated quick-assist lane owns this urgent resolve/write now, so the
    # route work happens once and at the front of the assist queue.
    _queue_quick_assist_job(str(slot_label), int(quick_index), snap, preset, quiet=quiet)
    return True


def _apply_quick_assist_sync(slot_label: str, quick_index: int, snap: dict | None = None, quiet: bool = False) -> bool:
    global _HEADLESS_LAST_PATCH_KEY
    """Apply one JSON quick-assist entry for a HUD slot.

    This is the only function main.py needs for quick buttons. It resolves the
    active scanner route here, writes the profile, and stores a runtime profile
    for duplicate/shared-character assist calls.
    """
    owner_base = _slot_owner_base_from_label(slot_label)
    if owner_base is None:
        return False
    quicks = get_quick_assists_for_slot(slot_label, snap)
    if quick_index < 0 or quick_index >= len(quicks):
        return False
    preset = quicks[quick_index]
    _HEADLESS_LAST_PATCH_KEY = None
    char_id = 0
    if isinstance(snap, dict):
        for field in ("id", "csv_char_id", "char_id"):
            try:
                char_id = int(snap.get(field) or 0)
            except Exception:
                char_id = 0
            if char_id:
                break

    # Volnutt is special: every quick button points at the same 6B table, and
    # the visible result is controlled by the requested weapon byte.  Do not
    # gate this on snap["id"] only; some main.py snapshots use char_id instead,
    # and if this stays 0 the selector works while the weapon remains whatever
    # was already active, usually Drill.
    requested_volnutt_weapon = _volnutt_weapon_value_from_preset(preset)
    volnutt_weapon = requested_volnutt_weapon
    if requested_volnutt_weapon is not None and char_id not in (0, VOLNUTT_CHAR_ID):
        volnutt_weapon = None

    row = _resolve_quick_route_row(owner_base, char_id if char_id else None)
    if not row:
        if not quiet:
            try:
                print(f"[assist quick] no assist route for {slot_label} base 0x{owner_base:08X}")
            except Exception:
                pass
        return False

    if char_id == 0:
        try:
            char_id = int(row.get("char_id") or 0)
        except Exception:
            char_id = 0
    if requested_volnutt_weapon is not None:
        row_char_id = 0
        try:
            row_char_id = int(row.get("char_id") or 0)
        except Exception:
            row_char_id = 0
        if char_id not in (0, VOLNUTT_CHAR_ID) and row_char_id not in (0, VOLNUTT_CHAR_ID):
            volnutt_weapon = None

    raw = _selector_word_for_quick_preset(owner_base, row, preset)
    if isinstance(raw, str):
        if not quiet:
            try:
                print(f"[assist quick] {slot_label} {preset.get('label', quick_index)} failed: {raw}")
            except Exception:
                pass
        return False
    writes = _selector_writes_for_row_module(row, raw)
    if volnutt_weapon is not None:
        weapon_writes = _volnutt_weapon_writes(owner_base, int(volnutt_weapon), row)
        if not weapon_writes:
            try:
                print(f"[assist quick] {slot_label} Volnutt weapon {volnutt_weapon} not found in active 426 wrapper")
            except Exception:
                pass
            return False
        writes.update(weapon_writes)
    if not writes or not _write_many_module(writes, urgent=True, key=f"assist:quick:{slot_label}"):
        if not quiet:
            try:
                print(f"[assist quick] write failed for {slot_label} {preset.get('label', quick_index)}")
            except Exception:
                pass
        return False

    fighter_base = 0
    try:
        fighter_base = int((snap or {}).get("base") or 0)
    except Exception:
        fighter_base = 0
    label = str(preset.get("label") or preset.get("name") or preset.get("move") or "quick assist")
    profile = {
        "word": None if raw is None else (int(raw) & 0xFFFFFFFF),
        "label": label,
        "row": {**dict(row), "_assist_epoch": int(_ASSIST_CACHE_EPOCH)},
        "fighter_base": fighter_base or owner_base,
        "owner_base": int(owner_base),
        "slot_label": str(slot_label),
        "quick_index": int(quick_index),
        "preset": dict(preset),
        "char_id": int(char_id or row.get("char_id") or 0),
        "is_default": raw is None,
    }
    if volnutt_weapon is not None:
        profile["volnutt_weapon"] = int(volnutt_weapon)
    keys = {int(owner_base)}
    if fighter_base:
        keys.add(int(fighter_base))
    for key in keys:
        _HEADLESS_ASSIST_PROFILES[key] = profile
    _HEADLESS_SLOT_ASSIST_PROFILES[str(slot_label)] = dict(profile)

    # New/changed quick profile must be eligible immediately even if this slot
    # already entered an assist-ish window while the worker was resolving the
    # route.  Clearing these latches makes the very next HUD tick write the
    # graft instead of waiting for the next assist cooldown cycle.
    try:
        if fighter_base:
            _HEADLESS_LAST_ASSIST_ATTACK_BY_BASE.pop(int(fighter_base), None)
        _HEADLESS_LAST_RUNTIME_WRITE_BY_SLOT.pop(str(slot_label), None)
    except Exception:
        pass

    inst = _inst
    if inst is not None:
        try:
            inst._record_slot_profile(row, None if raw is None else int(raw), label)
            if volnutt_weapon is not None:
                for key in keys:
                    try:
                        if key in inst._slot_assist_profiles:
                            inst._slot_assist_profiles[key]["volnutt_weapon"] = int(volnutt_weapon)
                    except Exception:
                        pass
        except Exception:
            pass
        if not quiet:
            try:
                inst._status.set(f"Quick assist {slot_label}: {label}")
            except Exception:
                pass
    if not quiet:
        try:
            word_txt = "default" if raw is None else f"0x{int(raw):08X}"
            print(f"[assist quick] {slot_label} -> {label} {word_txt}")
        except Exception:
            pass
    return True


def _snap_state_id_for_runtime(snap: dict) -> int | None:
    if not snap:
        return None
    mv = snap.get("mv_id_display")
    if mv is None:
        mv = snap.get("attA") or snap.get("attB")
    try:
        return int(mv)
    except Exception:
        return None


def _snap_is_assist_attack_for_runtime(snap: dict) -> bool:
    mv = _snap_state_id_for_runtime(snap)
    if mv in ASSIST_RUNTIME_ATTACK_STATES:
        return True
    label = str((snap or {}).get("mv_label") or "").strip().lower()
    return label == "assist attack" or "assist attack" in label


def _snap_is_assist_runtime_window(snap: dict) -> bool:
    """True during the frames where a shared assist graft may be consumed.

    Mirror/duplicate characters cannot keep a custom graft installed at idle,
    because the other same-character slot would inherit it.  Instead, patch the
    shared graft while the specific slot is in assist standby/jump-in/attack.
    """
    mv = _snap_state_id_for_runtime(snap)
    if mv in ASSIST_RUNTIME_ATTACK_STATES or mv in ASSIST_RUNTIME_PREFETCH_STATES:
        return True
    label = str((snap or {}).get("mv_label") or "").strip().lower()
    if not label:
        return False
    return (
        "assist" in label
        or "tag in taunt" in label
        or "tag out" in label
    )


def _runtime_snap_char_id(snap: dict | None) -> int:
    if not isinstance(snap, dict):
        return 0
    for field in ("id", "csv_char_id", "char_id"):
        try:
            cid = int(snap.get(field) or 0)
        except Exception:
            cid = 0
        if cid:
            return cid
    return 0


def _runtime_char_counts(snaps: dict) -> dict[int, int]:
    out: dict[int, int] = {}
    if not isinstance(snaps, dict):
        return out
    for snap in snaps.values():
        cid = _runtime_snap_char_id(snap if isinstance(snap, dict) else None)
        if cid:
            out[cid] = int(out.get(cid, 0)) + 1
    return out


def _profile_matches_char(profile: dict | None, char_id: int) -> bool:
    if not isinstance(profile, dict):
        return False
    if not char_id:
        return True
    try:
        prof_cid = int(profile.get("char_id") or 0)
    except Exception:
        prof_cid = 0
    return bool((not prof_cid) or prof_cid == int(char_id))


def _refresh_runtime_profile_route(profile: dict, slot_label: str, char_id: int, force_default: bool = False) -> dict:
    """Refresh stale route rows after round reloads without losing selection.

    Runtime assist ticks must never do a synchronous MEM2 route scan.  If the
    cached row is stale, use a fresh cached row only when it is already warm;
    otherwise schedule a background prewarm and keep the old row for this frame.
    This prevents mirror-assist persistence from stuttering the game loop.
    """
    if not isinstance(profile, dict):
        return {}

    row = dict(profile.get("row", {}) or {})
    try:
        row_epoch = int(row.get("_assist_epoch", -1))
    except Exception:
        row_epoch = -1
    if row and row_epoch == int(_ASSIST_CACHE_EPOCH):
        return profile

    owner_base = _slot_owner_base_from_label(slot_label)
    if owner_base is None:
        try:
            owner_base = int(profile.get("owner_base") or row.get("owner_base") or 0)
        except Exception:
            owner_base = 0
    if not owner_base:
        return profile

    route_char_id = int(char_id or profile.get("char_id") or 0) or None
    new_row = _quick_route_cache_get(int(owner_base), route_char_id)
    if not new_row:
        _schedule_quick_route_prewarm(int(owner_base), route_char_id)
        return profile

    profile = dict(profile)
    profile["row"] = {**dict(new_row), "_assist_epoch": int(_ASSIST_CACHE_EPOCH)}
    profile["owner_base"] = int(owner_base)
    if char_id:
        profile["char_id"] = int(char_id)

    if force_default or bool(profile.get("is_default", False)):
        profile["word"] = None
        profile["is_default"] = True
        profile["label"] = "Default"
        return profile

    preset = profile.get("preset")
    if isinstance(preset, dict):
        raw = _selector_word_for_quick_preset(int(owner_base), dict(new_row), preset)
        if not isinstance(raw, str):
            profile["word"] = None if raw is None else (int(raw) & 0xFFFFFFFF)
    return profile


def _slot_runtime_profile(slot_label: str, snap: dict, duplicate_char: bool) -> dict | None:
    """Choose the profile that belongs to this visible slot.

    Non-duplicate characters keep the old fighter/owner fallback behavior.
    Duplicate characters are stricter: use this slot's profile if it has one;
    otherwise, if another same-character slot has a profile, actively write the
    default restore for this slot so Player B does not inherit Player A's graft.
    """
    char_id = _runtime_snap_char_id(snap)

    slot_profile = _HEADLESS_SLOT_ASSIST_PROFILES.get(str(slot_label))
    if slot_profile and _profile_matches_char(slot_profile, char_id):
        return _refresh_runtime_profile_route(dict(slot_profile), slot_label, char_id, force_default=False)

    if duplicate_char and char_id:
        # Same character is present elsewhere. If this slot has no explicit
        # choice, but a sibling does, restore default when this slot calls assist.
        for other_slot, prof in list(_HEADLESS_SLOT_ASSIST_PROFILES.items()):
            if other_slot == str(slot_label):
                continue
            if not _profile_matches_char(prof, char_id):
                continue
            default_prof = dict(prof)
            default_prof["slot_label"] = str(slot_label)
            default_prof["word"] = None
            default_prof["label"] = "Default"
            default_prof["is_default"] = True
            return _refresh_runtime_profile_route(default_prof, slot_label, char_id, force_default=True)
        return None

    try:
        fighter_base = int(snap.get("base") or 0)
    except Exception:
        fighter_base = 0

    profile = _HEADLESS_ASSIST_PROFILES.get(fighter_base)
    if profile is None:
        owner_base = _slot_owner_base_from_label(slot_label)
        if owner_base is not None:
            profile = _HEADLESS_ASSIST_PROFILES.get(int(owner_base))
    if profile and _profile_matches_char(profile, char_id):
        return _refresh_runtime_profile_route(dict(profile), slot_label, char_id, force_default=False)
    return None


def _write_runtime_profile_for_slot(slot_label: str, snap: dict, profile: dict) -> bool:
    if not isinstance(profile, dict):
        return False
    raw_obj = profile.get("word", None)
    raw_val = None if raw_obj is None else int(raw_obj) & 0xFFFFFFFF
    row = dict(profile.get("row", {}) or {})
    writes = _selector_writes_for_row_module(row, raw_val)
    volnutt_weapon = profile.get("volnutt_weapon")
    if volnutt_weapon is not None:
        owner_base_for_weapon = int(profile.get("owner_base") or row.get("owner_base") or 0)
        writes.update(_volnutt_weapon_writes(owner_base_for_weapon, int(volnutt_weapon), row))
    return bool(writes and _write_many_module(writes, urgent=True, key=f"assist:runtime:{slot_label}"))


def _headless_runtime_patch_from_main(snaps: dict) -> None:
    global _HEADLESS_LAST_PATCH_KEY
    if not isinstance(snaps, dict):
        return
    if not _HEADLESS_ASSIST_PROFILES and not _HEADLESS_SLOT_ASSIST_PROFILES:
        return

    char_counts = _runtime_char_counts(snaps)
    ordered = ["P1-C1", "P1-C2", "P2-C1", "P2-C2"]
    ordered.extend(k for k in snaps.keys() if k not in ordered)

    for slot_label in ordered:
        snap = snaps.get(slot_label)
        if not isinstance(snap, dict):
            continue
        try:
            fighter_base = int(snap.get("base") or 0)
        except Exception:
            fighter_base = 0
        if not fighter_base:
            continue

        char_id = _runtime_snap_char_id(snap)
        duplicate_char = bool(char_id and char_counts.get(char_id, 0) > 1)
        # Patch on the earlier assist-ish window for every selected slot, not
        # only mirrors.  This lets main.py stop doing idle re-apply writes while
        # still catching round-rebuilt/defaulted grafts before the assist body
        # consumes them.
        in_runtime_window = _snap_is_assist_runtime_window(snap)
        was_window = bool(_HEADLESS_LAST_ASSIST_ATTACK_BY_BASE.get(fighter_base, False))
        _HEADLESS_LAST_ASSIST_ATTACK_BY_BASE[fighter_base] = bool(in_runtime_window)

        if not in_runtime_window:
            continue

        profile = _slot_runtime_profile(str(slot_label), snap, duplicate_char)
        if not profile:
            continue

        raw_obj = profile.get("word", None)
        raw_val = None if raw_obj is None else int(raw_obj) & 0xFFFFFFFF
        weapon = profile.get("volnutt_weapon")
        weapon_key = -1 if weapon is None else int(weapon)
        state_key = int(_snap_state_id_for_runtime(snap) or -1)
        patch_key = (fighter_base, -1 if raw_val is None else raw_val, weapon_key, state_key)

        # Duplicate-character mirrors may remain in the assist window for many
        # frames.  Patch early and often enough to beat the game's graft read,
        # but not every single frame.  Non-duplicates usually write once per
        # assist-entry edge, but if the profile became ready after the edge was
        # already latched, still allow the first write for this patch key.
        now_mono = time.monotonic()
        slot_last = _HEADLESS_LAST_RUNTIME_WRITE_BY_SLOT.get(str(slot_label))
        if slot_last is not None:
            last_key, last_ts = slot_last
            if last_key == patch_key:
                if not duplicate_char and was_window:
                    continue
                if (now_mono - float(last_ts)) < _ASSIST_RUNTIME_WRITE_THROTTLE_SECONDS:
                    continue

        if _write_runtime_profile_for_slot(str(slot_label), snap, profile):
            _HEADLESS_LAST_RUNTIME_WRITE_BY_SLOT[str(slot_label)] = (patch_key, now_mono)
            if _HEADLESS_LAST_PATCH_KEY != patch_key:
                _HEADLESS_LAST_PATCH_KEY = patch_key
                _assist_log_once(
                    f"runtime:{slot_label}:{patch_key}",
                    f"[assist quick] runtime {slot_label} -> {profile.get('label', 'assist')}",
                    interval=0.75,
                )


def get_assist_runtime_debug_state(snaps: dict | None = None) -> dict:
    """Small, read-only snapshot for the Overseer panel.

    This must stay cheap. It reports cached/runtime state only; it does not
    resolve routes, scan MEM2, or write anything.
    """
    out = {
        "cache_epoch": int(_ASSIST_CACHE_EPOCH),
        "slot_profiles": {},
        "route_cache_count": 0,
        "route_inflight_count": 0,
        "last_runtime_write_by_slot": {},
        "quick_lane_pending_count": 0,
        "quick_lane_order": [],
        "quick_lane_last_result": {},
    }
    try:
        with _QUICK_ROUTE_LOCK:
            out["route_cache_count"] = len(_QUICK_ROUTE_CACHE)
            out["route_inflight_count"] = len(_QUICK_ROUTE_INFLIGHT)
    except Exception:
        pass

    try:
        with _QUICK_ASSIST_LANE_LOCK:
            out["quick_lane_pending_count"] = len(_QUICK_ASSIST_LANE_PENDING)
            out["quick_lane_order"] = list(_QUICK_ASSIST_LANE_ORDER)
            out["quick_lane_last_result"] = {k: dict(v) for k, v in _QUICK_ASSIST_LANE_LAST_RESULT.items()}
    except Exception:
        pass

    try:
        for slot_label in FIGHTER_SLOT_LABELS:
            prof = _HEADLESS_SLOT_ASSIST_PROFILES.get(str(slot_label))
            snap = snaps.get(slot_label) if isinstance(snaps, dict) else None
            row = dict((prof or {}).get("row", {}) or {}) if isinstance(prof, dict) else {}
            last = _HEADLESS_LAST_RUNTIME_WRITE_BY_SLOT.get(str(slot_label))
            state_info = row.get("assist_state_info") or {}
            direct_info = row.get("direct_426_info") or {}
            state_id = _snap_state_id_for_runtime(snap if isinstance(snap, dict) else {})
            out["slot_profiles"][str(slot_label)] = {
                "selected": bool(prof),
                "label": str((prof or {}).get("label") or "") if isinstance(prof, dict) else "",
                "is_default": bool((prof or {}).get("is_default", False)) if isinstance(prof, dict) else False,
                "word": None if not isinstance(prof, dict) or prof.get("word") is None else int(prof.get("word")) & 0xFFFFFFFF,
                "char_id": int((prof or {}).get("char_id") or 0) if isinstance(prof, dict) else 0,
                "owner_base": int((prof or {}).get("owner_base") or 0) if isinstance(prof, dict) else 0,
                "fighter_base": int((prof or {}).get("fighter_base") or 0) if isinstance(prof, dict) else 0,
                "wrapper_address": direct_info.get("wrapper_addr") or direct_info.get("default_target") or state_info.get("wrapper_addr"),
                "state_wrapper_address": (int(state_info.get("wrapper_addr") or 0) + int(state_info.get("state_wrapper_off") or 0)) if state_info.get("wrapper_addr") and state_info.get("state_wrapper_off") else None,
                "patch_status": "write reported; read-back unverified" if last is not None else "unavailable: no cached runtime write",
                "route_type": str(row.get("typ") or ""),
                "route_block": int(row.get("block") or 0),
                "route_epoch": int(row.get("_assist_epoch", -1)) if row else -1,
                "in_assist_window": bool(_snap_is_assist_runtime_window(snap if isinstance(snap, dict) else {})),
                "state_id": state_id,
                "last_write_age_sec": None if last is None else round(max(0.0, time.monotonic() - float(last[1])), 3),
            }
    except Exception as e:
        out["error"] = repr(e)
    return out


def restore_assist_runtime_defaults_from_main(snaps: dict | None = None) -> dict:
    """Best-effort restore of selected assist grafts, then clear runtime selection.

    This is intentionally conservative: it uses the already-stored route rows,
    writes default for each selected slot if possible, then clears persistent
    quick-assist state. It does not scan MEM2.
    """
    restored: list[str] = []
    failed: list[str] = []
    for slot_label, prof in list(_HEADLESS_SLOT_ASSIST_PROFILES.items()):
        if not isinstance(prof, dict):
            continue
        snap = snaps.get(slot_label) if isinstance(snaps, dict) else {}
        default_prof = dict(prof)
        default_prof["word"] = None
        default_prof["label"] = "Default"
        default_prof["is_default"] = True
        try:
            if _write_runtime_profile_for_slot(str(slot_label), snap if isinstance(snap, dict) else {}, default_prof):
                restored.append(str(slot_label))
            else:
                failed.append(str(slot_label))
        except Exception:
            failed.append(str(slot_label))
    clear_assist_runtime_state(clear_route_cache=False)
    return {"restored": restored, "failed": failed}


def clear_assist_runtime_state(*, clear_route_cache: bool = False) -> None:
    """Clear quick-assist runtime selection and latches without touching UI files."""
    global _HEADLESS_LAST_PATCH_KEY
    _HEADLESS_ASSIST_PROFILES.clear()
    _HEADLESS_SLOT_ASSIST_PROFILES.clear()
    _HEADLESS_LAST_ASSIST_ATTACK_BY_BASE.clear()
    _HEADLESS_LAST_RUNTIME_WRITE_BY_SLOT.clear()
    with _QUICK_ASSIST_LANE_LOCK:
        _QUICK_ASSIST_LANE_PENDING.clear()
        _QUICK_ASSIST_LANE_ORDER.clear()
        _QUICK_ASSIST_LANE_LAST_RESULT.clear()
        _QUICK_ASSIST_LANE_EVENT.clear()
    _HEADLESS_LAST_PATCH_KEY = None
    inst = _inst
    if inst is not None:
        try:
            inst._slot_assist_profiles.clear()
            inst._main_last_assist_attack_by_base.clear()
            inst._main_patch_latch_by_base.clear()
            inst._main_last_patch_key = None
        except Exception:
            pass
    if clear_route_cache:
        with _QUICK_ROUTE_LOCK:
            _QUICK_ROUTE_CACHE.clear()
            _QUICK_ROUTE_INFLIGHT.clear()
            _QUICK_ROUTE_FAIL_UNTIL.clear()
        with _ASSIST_SCAN_CACHE_LOCK:
            global _ASSIST_SCAN_CACHE_SIGNATURE, _ASSIST_SCAN_CACHE_HITS
            _ASSIST_SCAN_CACHE_SIGNATURE = None
            _ASSIST_SCAN_CACHE_HITS = []


class AssistScannerWindow:
    def __init__(self, master):
        self.root = tk.Toplevel(master)
        self.root.title("Assist Scanner - Shared Character Assist Picker")
        self.root.geometry("1420x700")
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self._scanning = False
        self._hit_by_iid: dict[str, dict] = {}
        self._sort_col = None
        self._sort_asc = True
        self._chun_selector_test_index = 0
        self._last_chun_selector_addr = None
        self._last_chun_selector_test = None
        self._slot_assist_profiles: dict[int, dict[str, object]] = {}
        self._auto_reapply_enabled = True
        self._auto_reapply_after_id = None
        self._auto_btn = None
        self._vjoe_static_direct_enabled = True
        self._vjoe_static_btn = None
        self._runtime_move_offset: tuple[int, int] | None = None  # (offset, width)
        self._runtime_last_profile_key: int | None = None
        # Main-HUD driven runtime switch. main.py already knows each fighter's
        # current move label/id, so this avoids guessing a fighter struct offset.
        self._main_last_assist_attack_by_base: dict[int, bool] = {}
        self._main_patch_latch_by_base: dict[int, int] = {}
        self._main_last_patch_key: tuple[int, int] | None = None
        self._build()
        # Do not auto-scan on open. The scanner window is now a debug/control UI;
        # main.py feeds live slot context every frame, and quick assists resolve
        # routes on demand. Press Rescan only when a targeted operation requires a targeted
        # diagnostic refresh.
        self._status.set("Ready - using main.py live slot context. Press Rescan for a targeted assist scan.")
        # Show already-resolved routes from main's live context instead of
        # opening to a blank table. This is cache-first and non-blocking. It
        # does not scan on open; press Refresh Cache or Rescan Loaded manually.
        try:
            self.root.after(120, lambda: self._populate_from_main_context_cache(False))
            self.root.after(900, lambda: self._populate_from_main_context_cache(False))
        except Exception:
            pass

    def _build(self):
        top = ttk.Frame(self.root)
        top.pack(side="top", fill="x", padx=8, pady=6)
        ttk.Label(
            top,
            text=(
                "Shows one profile row per loaded fighter using each shared character selector table. "
                "Double-click a row to choose that fighter's assist. Use Auto Assist Trigger for duplicate characters."
            ),
        ).pack(side="left")
        self._scan_btn = ttk.Button(top, text="Rescan Loaded", command=self._start)
        self._scan_btn.pack(side="right")

        self._refresh_main_btn = ttk.Button(top, text="Refresh Cache", command=lambda: self._populate_from_main_context_cache(True))
        self._refresh_main_btn.pack(side="right", padx=(0, 8))

        self._dump_slots_btn = ttk.Button(top, text="Dump Char Slots", command=self._dump_char_slots)
        self._dump_slots_btn.pack(side="right", padx=(0, 8))

        self._auto_btn = ttk.Button(top, text="Auto Assist Trigger: ON", command=self._toggle_auto_reapply)
        self._auto_btn.pack(side="right", padx=(0, 8))


        self._prog = tk.DoubleVar()
        ttk.Progressbar(self.root, variable=self._prog, maximum=100).pack(fill="x", padx=8, pady=(0, 4))
        self._status = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self._status, anchor="w").pack(fill="x", padx=8)

        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=8, pady=6)
        self._tree = ttk.Treeview(frame, columns=COL_IDS, show="headings", height=28)
        widths = {
            "owner": 230, "slot": 60, "entry": 300,
            "address": 130, "raw": 240, "target": 130, "guess": 420,
        }
        for col_id, header in COLS:
            self._tree.heading(col_id, text=header, command=lambda c=col_id: self._sort_by(c))
            self._tree.column(col_id, width=widths.get(col_id, 80), anchor="center")
        for c in ("owner", "entry", "guess"):
            if c in COL_IDS:
                self._tree.column(c, anchor="w")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        self._tree.bind("<Double-Button-1>", self._on_double_click)
        self._tree.bind("<Button-3>", self._on_right_click)

    def _start(self):
        if self._scanning:
            return
        self._scanning = True
        self._scan_btn.config(state="disabled")
        self._prog.set(0)
        self._hit_by_iid.clear()
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        self._status.set("Targeted scan of loaded character slot windows...")
        threading.Thread(target=_run_scan, args=(self._on_prog, self._on_done), daemon=True).start()

    def _on_prog(self, pct: float):
        try:
            self.root.after(0, lambda: self._prog.set(pct))
        except Exception:
            pass

    def _insert_hits_into_tree(self, hits: list[dict], status_prefix: str = "Cached") -> None:
        """Replace the visible tree with already-known route rows.

        This is intentionally UI-only. It does not scan MEM2. It lets the
        scanner window show the route rows that main.py/quick assists already
        warmed in _QUICK_ROUTE_CACHE, so opening the scanner does not look empty.
        """
        self._hit_by_iid.clear()
        for iid in self._tree.get_children():
            self._tree.delete(iid)

        hits_expanded = _expand_selector_hits_to_fighter_rows(hits)
        for h in hits_expanded:
            def _val(col_id: str) -> str:
                if col_id == "block":
                    return f"0x{h['block']:08X}"
                if col_id == "address":
                    return f"0x{h['addr']:08X}"
                return str(h.get(col_id, ""))
            iid = self._tree.insert("", "end", values=tuple(_val(c) for c in COL_IDS))
            self._hit_by_iid[iid] = h
            if h.get("typ") in (
                "u32-chun-selector",
                "u32-ryu-selector-graft",
                "u32-generic-selector",
                "u32-anchored-assist-fallback",
                "u32-vjoe-trampoline",
                "u32-direct-426-fallback",
            ):
                self._record_default_slot_profile(h)

        self._scanning = False
        try:
            self._scan_btn.config(state="normal")
        except Exception:
            pass
        self._prog.set(100 if hits_expanded else 0)
        chun_rows = len([h for h in hits_expanded if h["kind"] == "chun-selector-word"])
        ryu_rows = len([h for h in hits_expanded if h["kind"] == "ryu-selector-word"])
        generic_rows = len([h for h in hits_expanded if h["kind"] == "generic-selector-word"])
        self._status.set(
            f"{status_prefix} - {ryu_rows} Ryu, {chun_rows} Chun, {generic_rows} generic fighter profile row(s)."
        )

    def _populate_from_main_context_cache(self, schedule_missing: bool = False) -> None:
        """Show main.py-fed route results without pressing Rescan.

        Main already calls tick_assist_profiles_from_main(snaps), but that
        frame path only caches slot context. The scanner window displays rows
        already known from quick assists or manual scans. If a row is not warm
        yet, the Refresh Cache button schedules character-gated route discovery
        and refreshes again without blocking the UI.
        """
        snaps = _main_snaps_snapshot()
        if not snaps:
            cached = _assist_scan_cache_get(_assist_team_signature_from_main())
            if cached:
                self._insert_hits_into_tree(cached, "Scan cache")
            else:
                self._status.set("No main.py slot context yet. Start/load a match, then press Rescan Loaded.")
            return

        cached = _assist_scan_cache_get(_assist_team_signature_from_main())
        if cached and not schedule_missing:
            self._insert_hits_into_tree(cached, "Scan cache")
            return

        hits: list[dict] = []
        missing: list[tuple[int, int | None]] = []
        for slot_label in FIGHTER_SLOT_LABELS:
            snap = snaps.get(slot_label)
            if not isinstance(snap, dict) or not snap.get("base"):
                continue
            owner_base = _slot_owner_base_from_label(slot_label)
            if owner_base is None:
                continue
            char_id = 0
            for field in ("id", "csv_char_id", "char_id"):
                try:
                    char_id = int(snap.get(field) or 0)
                except Exception:
                    char_id = 0
                if char_id:
                    break

            row = _quick_route_cache_get(owner_base, char_id if char_id else None)
            if row is None:
                # A previously chosen quick assist stores the same row even if
                # the generic warm cache has not finished yet.
                prof = _HEADLESS_ASSIST_PROFILES.get(int(snap.get("base") or 0)) or _HEADLESS_ASSIST_PROFILES.get(int(owner_base))
                if isinstance(prof, dict) and isinstance(prof.get("row"), dict):
                    row = dict(prof.get("row") or {})

            if row is None:
                missing.append((int(owner_base), int(char_id) if char_id else None))
                if schedule_missing:
                    _schedule_quick_route_prewarm(owner_base, char_id if char_id else None)
                continue

            row = dict(row)
            row["owner_base"] = int(owner_base)
            if char_id:
                row["char_id"] = int(char_id)
            try:
                row["fighter_base"] = int(snap.get("base") or 0)
            except Exception:
                pass
            row["fighter_label"] = str(slot_label)
            char_name = str(snap.get("name") or CHAR_ID_TO_KEY.get(int(char_id or row.get("char_id") or 0), "?") or "?")
            if row.get("fighter_base"):
                row["owner"] = f"{slot_label} {char_name} @0x{int(row['fighter_base']):08X}"
            hits.append(row)

        if hits:
            self._insert_hits_into_tree(hits, "Main cache")
        else:
            if schedule_missing:
                self._status.set("No cached assist routes yet. Warming loaded slots from main context...")
            else:
                self._status.set("No cached assist routes yet. Press Refresh Cache to warm, or Rescan Loaded to scan.")

        if missing and schedule_missing:
            # Do not block the Tk thread. The prewarm worker fills the cache;
            # refresh the visible table a moment later.
            try:
                self.root.after(900, lambda: self._populate_from_main_context_cache(False))
            except Exception:
                pass
        elif missing and hits:
            self._status.set(f"Main cache - {len(hits)} cached row(s), {len(missing)} still missing. Press Rescan Loaded if needed.")

    def _on_done(self, hits: list[dict]):
        def _f():
            hits_expanded = _expand_selector_hits_to_fighter_rows(hits)
            for h in hits_expanded:
                def _val(col_id: str) -> str:
                    if col_id == "block":
                        return f"0x{h['block']:08X}"
                    if col_id == "address":
                        return f"0x{h['addr']:08X}"
                    return str(h.get(col_id, ""))
                iid = self._tree.insert("", "end", values=tuple(_val(c) for c in COL_IDS))
                self._hit_by_iid[iid] = h
                if h.get("typ") in (
                    "u32-chun-selector",
                    "u32-ryu-selector-graft",
                    "u32-generic-selector",
                    "u32-anchored-assist-fallback",
                    "u32-vjoe-trampoline",
                    "u32-direct-426-fallback",
                ):
                    self._record_default_slot_profile(h)
            self._scanning = False
            self._scan_btn.config(state="normal")
            self._prog.set(100)
            chun_rows = len([h for h in hits_expanded if h["kind"] == "chun-selector-word"])
            ryu_rows = len([h for h in hits_expanded if h["kind"] == "ryu-selector-word"])
            generic_rows = len([h for h in hits_expanded if h["kind"] == "generic-selector-word"])
            self._status.set(
                f"Done - {ryu_rows} Ryu, {chun_rows} Chun, {generic_rows} generic fighter profile row(s). Tables are shared per character; Auto Assist Trigger patches on assist attack."
            )
        try:
            self.root.after(0, _f)
        except Exception:
            pass

    def _sort_by(self, col_id: str):
        if self._sort_col == col_id:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col_id
            self._sort_asc = True
        headers = dict(COLS)
        for cid, header in COLS:
            arrow = (" up" if self._sort_asc else " down") if cid == col_id else ""
            self._tree.heading(cid, text=header + arrow)
        items = [(self._tree.set(iid, col_id), iid) for iid in self._tree.get_children("")]

        def key(v):
            s = str(v)
            try:
                if s.startswith("0x"):
                    return (0, int(s, 16))
                if s.startswith("+") or s.startswith("-"):
                    return (0, int(s, 0))
                return (0, float(s))
            except Exception:
                return (1, s.lower())

        items.sort(key=lambda x: key(x[0]), reverse=not self._sort_asc)
        for n, (_, iid) in enumerate(items):
            self._tree.move(iid, "", n)

    def _col_index(self, event) -> int:
        col = self._tree.identify_column(event.x)
        return int(col[1:]) - 1 if col else -1

    def _choose_ryu_preset_or_manual(self, addr: int, current: str) -> int | None:
        result: dict[str, int | None] = {"value": None}
        dlg = tk.Toplevel(self.root)
        dlg.title("Choose Ryu Selector")
        dlg.geometry("380x260")
        dlg.transient(self.root)
        dlg.grab_set()
        ttk.Label(
            dlg,
            text=(
                f"Address: 0x{addr:08X}\n"
                f"Current: {current}\n\n"
                "Choose a confirmed Ryu preset, or Manual for a raw U32."
            ),
            justify="left",
        ).pack(anchor="w", padx=12, pady=(12, 8))
        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill="x", padx=12, pady=4)

        def set_value(v: int):
            result["value"] = v
            dlg.destroy()

        for label, value in RYU_PRESETS:
            ttk.Button(btn_frame, text=f"{label}  0x{value:08X}", command=lambda v=value: set_value(v)).pack(fill="x", pady=3)

        def manual():
            text = simpledialog.askstring(
                "Manual selector",
                "Enter raw U32 value. Examples:\n"
                "0x0003110C = Hadouken\n"
                "0x0003146C = Shoryu\n"
                "0x00031CDC = Tatsu",
                parent=dlg,
                initialvalue=current,
            )
            if text is None:
                return
            try:
                cleaned = text.strip().replace(" ", "")
                value = int(cleaned, 16) if cleaned.lower().startswith("0x") else int(cleaned, 16)
            except ValueError:
                messagebox.showerror("Invalid", f"{text!r} is not a u32 value.", parent=dlg)
                return
            if not (0 <= value <= 0xFFFFFFFF):
                messagebox.showerror("Out of range", "Value must be 0-0xFFFFFFFF.", parent=dlg)
                return
            set_value(value)

        ttk.Button(btn_frame, text="Manual raw U32", command=manual).pack(fill="x", pady=(10, 3))
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(fill="x", pady=3)
        self.root.wait_window(dlg)
        return result["value"]

    def _manual_value(self, h: dict) -> bytes | None:
        typ = str(h.get("typ", ""))
        addr = int(h["addr"])
        current = str(h["raw"])
        if typ == "u16-state":
            text = simpledialog.askstring(
                "Edit StateID",
                f"Address: 0x{addr:08X}\nCurrent: {current}\n\nNew state ID u16:",
                parent=self.root,
                initialvalue=current,
            )
            if text is None:
                return None
            try:
                val = int(text.strip(), 16) if text.strip().lower().startswith("0x") else int(text.strip(), 16)
            except ValueError:
                messagebox.showerror("Invalid", f"{text!r} is not a u16 value.", parent=self.root)
                return None
            if not (0 <= val <= 0xFFFF):
                messagebox.showerror("Out of range", "Value must be 0-0xFFFF.", parent=self.root)
                return None
            return struct.pack(">H", val)
        if typ == "f32":
            text = simpledialog.askstring(
                "Edit float",
                f"Address: 0x{addr:08X}\nCurrent: {current}\n\n"
                "New big-endian f32 value. Use decimal like 1.25 or hex bytes like 0x3FA00000:",
                parent=self.root,
                initialvalue=current,
            )
            if text is None:
                return None
            cleaned = text.strip().replace(" ", "")
            try:
                if cleaned.lower().startswith("0x") and len(cleaned) == 10:
                    return bytes.fromhex(cleaned[2:])
                val = float(cleaned)
            except ValueError:
                messagebox.showerror("Invalid", f"{text!r} is not a float or raw f32 hex.", parent=self.root)
                return None
            return struct.pack(">f", val)
        if typ.startswith("raw"):
            n = int(typ[3:] or "4")
            text = simpledialog.askstring(
                "Edit raw bytes",
                f"Address: 0x{addr:08X}\nCurrent: {current}\nType: {typ}\n\nNew exact bytes:",
                parent=self.root,
                initialvalue=current,
            )
            if text is None:
                return None
            try:
                cleaned = text.replace(" ", "").replace("_", "")
                if cleaned.lower().startswith("0x"):
                    cleaned = cleaned[2:]
                payload = bytes.fromhex(cleaned)
            except ValueError:
                messagebox.showerror("Invalid", f"{text!r} is not valid hex bytes.", parent=self.root)
                return None
            if len(payload) != n:
                messagebox.showerror("Wrong length", f"Expected exactly {n} byte(s).", parent=self.root)
                return None
            return payload
        return None

    def _write_many(self, writes: dict[int, bytes], label: str) -> bool:
        if wbytes is None:
            messagebox.showerror("Write failed", "dolphin_io.wbytes unavailable.", parent=self.root)
            return False
        failures = []
        for addr, payload in writes.items():
            try:
                ok = bool(wbytes(addr, payload))
            except Exception as e:
                failures.append(f"0x{addr:08X}: {e}")
                continue
            if not ok:
                failures.append(f"0x{addr:08X}: write returned false")
        if failures:
            messagebox.showerror("Write failed", "\n".join(failures), parent=self.root)
            return False
        self._status.set(label)
        return True

    def _dump_char_slots(self):
        if rbytes is None:
            messagebox.showerror("Dump failed", "dolphin_io.rbytes unavailable.", parent=self.root)
            return

        import os
        import time

        def read_region(start_addr: int, size: int, label: str) -> bytes:
            out = bytearray()
            off = 0
            while off < size:
                n = min(SCAN_BLOCK, size - off)
                addr = start_addr + off
                try:
                    chunk = rbytes(addr, n)
                except Exception as e:
                    raise RuntimeError(f"{label}: read failed at 0x{addr:08X}: {e}") from e

                if not chunk:
                    raise RuntimeError(f"{label}: empty read at 0x{addr:08X}")

                if len(chunk) != n:
                    raise RuntimeError(
                        f"{label}: short read at 0x{addr:08X}; got 0x{len(chunk):X}, expected 0x{n:X}"
                    )

                out.extend(chunk)
                off += n

                try:
                    self._status.set(f"Dumping {label}: 0x{off:06X}/0x{size:06X}")
                    self.root.update_idletasks()
                except Exception:
                    pass

            return bytes(out)

        try:
            bases = list(_CHR_TBL_BASES)
            if not bases:
                messagebox.showerror("Dump failed", "_CHR_TBL_BASES is empty.", parent=self.root)
                return

            # Fixed raw window per character-table slot. This intentionally captures
            # more than the currently known selector area so the configuration can search it elsewhere.
            slot_size = 0x90000

            stamp = time.strftime("%Y%m%d_%H%M%S")
            out_dir = os.path.join(os.getcwd(), f"assist_char_slot_dump_{stamp}")
            os.makedirs(out_dir, exist_ok=True)

            index_lines = [
                "Assist Scanner character slot raw dump",
                f"Created: {stamp}",
                f"Slot dump size: 0x{slot_size:08X} bytes",
                "",
                "Individual slot dumps:",
            ]

            for i, base in enumerate(bases):
                label = f"slot {i} @ 0x{base:08X}"
                data = read_region(base, slot_size, label)
                filename = f"char_slot_{i}_base_0x{base:08X}_size_0x{slot_size:08X}.bin"
                path = os.path.join(out_dir, filename)
                with open(path, "wb") as f:
                    f.write(data)
                index_lines.append(
                    f"slot {i}: base=0x{base:08X}, size=0x{slot_size:08X}, file={filename}"
                )

            combined_start = min(bases)
            combined_end = max(bases) + slot_size
            combined_size = combined_end - combined_start
            combined_label = f"combined 0x{combined_start:08X}-0x{combined_end - 1:08X}"
            combined_data = read_region(combined_start, combined_size, combined_label)
            combined_filename = (
                f"char_slots_combined_0x{combined_start:08X}_"
                f"to_0x{combined_end - 1:08X}_size_0x{combined_size:08X}.bin"
            )
            combined_path = os.path.join(out_dir, combined_filename)
            with open(combined_path, "wb") as f:
                f.write(combined_data)

            index_lines.extend([
                "",
                "Combined contiguous dump:",
                f"start=0x{combined_start:08X}",
                f"end=0x{combined_end - 1:08X}",
                f"size=0x{combined_size:08X}",
                f"file={combined_filename}",
                "",
                "Known slot bases:",
                *[f"0x{base:08X}" for base in bases],
                "",
            ])

            index_path = os.path.join(out_dir, "README_dump_index.txt")
            with open(index_path, "w", encoding="utf-8") as f:
                f.write("\n".join(index_lines))

            self._status.set(f"Dump complete: {out_dir}")
            messagebox.showinfo(
                "Dump complete",
                (
                    f"Dumped {len(bases)} character slot window(s) plus one combined raw dump.\n\n"
                    f"Saved to:\n{out_dir}"
                ),
                parent=self.root,
            )

        except Exception as e:
            self._status.set("Dump failed")
            messagebox.showerror("Dump failed", str(e), parent=self.root)

    def _read_memory_region(self, start_addr: int, size: int, label: str = "region") -> bytes:
        if rbytes is None:
            raise RuntimeError("dolphin_io.rbytes unavailable.")

        out = bytearray()
        off = 0
        while off < size:
            n = min(SCAN_BLOCK, size - off)
            addr = start_addr + off
            try:
                chunk = rbytes(addr, n)
            except Exception as e:
                raise RuntimeError(f"{label}: read failed at 0x{addr:08X}: {e}") from e

            if not chunk:
                raise RuntimeError(f"{label}: empty read at 0x{addr:08X}")

            if len(chunk) != n:
                raise RuntimeError(
                    f"{label}: short read at 0x{addr:08X}; got 0x{len(chunk):X}, expected 0x{n:X}"
                )

            out.extend(chunk)
            off += n

        return bytes(out)

    def _chun_selector_candidate_score(self, base: int, idx: int, data: bytes,
                                       slot_char_ids: dict[int, int], source: str) -> int:
        addr = base + idx
        score = 0

        cid = slot_char_ids.get(base)
        if cid == 13:
            score += 100
        elif cid is not None:
            score -= 100

        if source == "graft":
            score += 40
        elif source == "original":
            score += 30
        else:
            score += 10

        wrapper_idx = idx + CHUN_SELECTOR_GRAFT_DELTA_TO_WRAPPER
        wrapper_sig = STATE_WRAPPER_PREFIX + b"\x00\x00\x01\x12\x01\x3C"
        if 0 <= wrapper_idx and wrapper_idx + len(wrapper_sig) <= len(data):
            if data[wrapper_idx:wrapper_idx + len(wrapper_sig)] == wrapper_sig:
                score += 80

        # Prefer candidates that have the confirmed pre-wrapper setup nearby.
        if idx >= 0 and idx + 0x50 <= len(data):
            window = data[idx:idx + 0x50]
            if b"\x04\x17\x60\x00" in window:
                score += 10
            if b"\x04\x01\x60\x00" in window:
                score += 10

        # Small deterministic tie-breaker: lower slot/base first, then lower address.
        score -= (_slot_index_for_base(base) or 0)
        score -= (addr & 0xFF) // 0x10
        return score

    def _find_chun_selector_graft_addr(self) -> tuple[int, str]:
        if rbytes is None:
            raise RuntimeError("dolphin_io.rbytes unavailable.")

        slot_char_ids = _read_slot_char_ids()
        patterns = [
            ("graft", CHUN_SELECTOR_GRAFT_BLOCK),
            ("original", CHUN_SELECTOR_ORIGINAL_BLOCK),
        ]
        wrapper_sig = STATE_WRAPPER_PREFIX + b"\x00\x00\x01\x12\x01\x3C"

        candidates: list[tuple[int, int, str]] = []

        for base in _CHR_TBL_BASES:
            try:
                data = self._read_memory_region(base, 0x90000, f"slot @ 0x{base:08X}")
            except Exception:
                continue

            for source, pattern in patterns:
                pos = 0
                while True:
                    idx = data.find(pattern, pos)
                    if idx < 0:
                        break
                    pos = idx + 1
                    score = self._chun_selector_candidate_score(base, idx, data, slot_char_ids, source)
                    candidates.append((score, base + idx, source))

            # Fallback for already-mutated grafts: find the live 0112 wrapper and
            # step back 0x48 to the selector slot.
            pos = 0
            while True:
                wrapper_idx = data.find(wrapper_sig, pos)
                if wrapper_idx < 0:
                    break
                pos = wrapper_idx + 1
                idx = wrapper_idx - CHUN_SELECTOR_GRAFT_DELTA_TO_WRAPPER
                if idx < 0 or idx + len(CHUN_SELECTOR_GRAFT_BLOCK) > len(data):
                    continue
                if data[idx:idx + 4] != b"\x0F\x06\x00\x27":
                    continue
                score = self._chun_selector_candidate_score(base, idx, data, slot_char_ids, "wrapper-fallback")
                candidates.append((score, base + idx, "wrapper-fallback"))

        if not candidates:
            raise RuntimeError(
                "Could not find the Chun assist selector slot. Load Chun, call the assist once, then try again."
            )

        candidates.sort(key=lambda x: (x[0], -x[1]), reverse=True)
        _, addr, source = candidates[0]
        return addr, source

    def _install_chun_selector_graft(self):
        try:
            addr, source = self._find_chun_selector_graft_addr()
        except Exception as e:
            self._status.set("Chun selector install failed")
            messagebox.showerror("Install Chun Selector failed", str(e), parent=self.root)
            return

        ok = self._write_many(
            {addr: CHUN_SELECTOR_GRAFT_BLOCK},
            f"Installed Chun selector graft at 0x{addr:08X}."
        )
        if not ok:
            return

        self._last_chun_selector_addr = addr
        selector_addrs = [addr + off for off in CHUN_SELECTOR_WORD_OFFSETS]
        messagebox.showinfo(
            "Chun selector installed",
            (
                f"Installed graft at 0x{addr:08X} using {source} match.\n\n"
                f"Selector lanes:\n"
                f"0x{selector_addrs[0]:08X}\n"
                f"0x{selector_addrs[1]:08X}\n"
                f"0x{selector_addrs[2]:08X}\n\n"
                "Use Next Chun Payload to force all three lanes."
            ),
            parent=self.root,
        )

    def _restore_chun_selector_block(self):
        try:
            addr, source = self._find_chun_selector_graft_addr()
        except Exception as e:
            self._status.set("Chun selector restore failed")
            messagebox.showerror("Restore Chun Selector failed", str(e), parent=self.root)
            return

        ok = self._write_many(
            {addr: CHUN_SELECTOR_ORIGINAL_BLOCK},
            f"Restored original Chun assist block at 0x{addr:08X}."
        )
        if not ok:
            return

        self._last_chun_selector_addr = addr
        self._last_chun_selector_test = None
        messagebox.showinfo(
            "Chun selector restored",
            f"Restored original block at 0x{addr:08X} using {source} match.",
            parent=self.root,
        )

    def _apply_next_chun_selector_payload(self):
        if not CHUN_SELECTOR_PAYLOADS:
            return

        try:
            addr, source = self._find_chun_selector_graft_addr()
        except Exception as e:
            self._status.set("Chun payload apply failed")
            messagebox.showerror("Next Chun Payload failed", str(e), parent=self.root)
            return

        # Ensure the confirmed graft is installed first. This is harmless if it is
        # already installed, and it avoids applying lane words to the original block.
        writes: dict[int, bytes] = {addr: CHUN_SELECTOR_GRAFT_BLOCK}

        name, payload = CHUN_SELECTOR_PAYLOADS[self._chun_selector_test_index]
        for off in CHUN_SELECTOR_WORD_OFFSETS:
            writes[addr + off] = payload

        ok = self._write_many(
            writes,
            f"Applied Chun selector payload {payload.hex(' ').upper()} at 0x{addr:08X}."
        )
        if not ok:
            return

        self._last_chun_selector_addr = addr
        self._last_chun_selector_test = name
        self._chun_selector_test_index = (self._chun_selector_test_index + 1) % len(CHUN_SELECTOR_PAYLOADS)

        details = "\n".join(
            f"0x{addr + off:08X} <- {payload.hex(' ').upper()}"
            for off in CHUN_SELECTOR_WORD_OFFSETS
        )
        messagebox.showinfo(
            "Chun selector payload applied",
            (
                f"{name}\n\n"
                f"Graft address: 0x{addr:08X} ({source})\n\n"
                f"{details}\n\n"
                "Test Chun assist now. Press Next Chun Payload for the next forced route."
            ),
            parent=self.root,
        )

    def _collect_move_preset_rows_for_base(self, owner_base: int, chr_tbl_base_override: int | None = None) -> list[dict]:
        """Harvest move/normal presets for one loaded character slot without showing rows."""
        if owner_base is None:
            return []
        slot_char_ids = _read_slot_char_ids()

        rows: list[dict] = []
        if chr_tbl_base_override:
            cid = slot_char_ids.get(int(owner_base))
            rows = _collect_move_preset_rows_from_chr_tbl_base(
                int(owner_base),
                int(chr_tbl_base_override),
                cid,
                slot_char_ids,
                "row-chr_tbl",
            )

        temp_hits: list[dict] = []
        try:
            _scan_assist_move_presets(slot_char_ids, temp_hits)
        except Exception:
            temp_hits = []

        rows.extend([
            h for h in temp_hits
            if int(h.get("owner_base", 0)) == int(owner_base)
            and not _is_filtered_assist_preset_name(str(h.get("move_name", "")))
        ])

        # VJoe's active chr_tbl can live outside the static owner window.  Add
        # rows from the resolved active table first, then let the normal
        # de-duplication/ranking collapse stale copies.
        active_vjoe = _vjoe_active_selector_info_for_owner_base(owner_base, slot_char_ids)
        if active_vjoe and int(active_vjoe.get("chr_tbl_base") or 0):
            active_rows = _collect_vjoe_move_preset_rows_from_chr_tbl(
                int(owner_base),
                int(active_vjoe["chr_tbl_base"]),
                slot_char_ids,
            )
            if active_rows:
                rows = active_rows + [
                    row for row in rows
                    if int(row.get("chr_tbl_base") or 0) != int(active_vjoe["chr_tbl_base"])
                ]

        # Multiple valid-looking chr_tbl copies can exist inside one character
        # slot. Only one is the selector base the assist VM actually uses.
        # Duplicate visible rows usually mean the same table_index was harvested
        # from a stale/secondary chr_tbl copy. Prefer the active runtime base.
        active_vjoe_for_preferred = _vjoe_active_selector_info_for_owner_base(owner_base, slot_char_ids)
        preferred_base = (
            int(chr_tbl_base_override)
            if chr_tbl_base_override
            else (
                int(active_vjoe_for_preferred["chr_tbl_base"])
                if active_vjoe_for_preferred and int(active_vjoe_for_preferred.get("chr_tbl_base") or 0)
                else _runtime_chr_tbl_base_for_owner_base(owner_base)
            )
        )

        if preferred_base is not None:
            preferred_rows = [
                row for row in rows
                if int(row.get("chr_tbl_base") or 0) == int(preferred_base)
            ]
            if preferred_rows:
                rows = preferred_rows

        def preset_row_rank(row: dict) -> tuple:
            chr_tbl_base = int(row.get("chr_tbl_base") or 0)
            preferred_miss = (
                0
                if preferred_base is not None and chr_tbl_base == int(preferred_base)
                else 1
            )
            owner_dist = abs(chr_tbl_base - int(owner_base)) if chr_tbl_base else 0x7FFFFFFF
            word = int(row.get("selector_word", 0))
            return (preferred_miss, owner_dist, word)

        best_by_table: dict[int, dict] = {}
        for row in rows:
            table_index = int(row.get("table_index", -1))
            old = best_by_table.get(table_index)
            if old is None or preset_row_rank(row) < preset_row_rank(old):
                best_by_table[table_index] = row

        rows = list(best_by_table.values())

        def band_priority(table_index: int, named: bool) -> int:
            # Keep the known assist/special bands at the top even when the
            # local name map is missing or wrong. This is needed for Tekkaman:
            # otherwise movement/system labels like "backward" can bury the
            # real 304+ move entries.
            if 304 <= table_index <= 368:
                return 0
            if 510 <= table_index <= 520:
                return 1
            if 256 <= table_index <= 270:
                return 2
            if not named:
                return 4
            return 3

        def row_key(row: dict) -> tuple:
            table_index = int(row.get("table_index", 0))
            name = str(row.get("move_name", ""))
            word = int(row.get("selector_word", 0))
            named = bool(row.get("move_named")) and _is_named_preset_name(name)
            return (band_priority(table_index, named), table_index, name.lower(), word)

        return sorted(rows, key=row_key)


    def _choose_loaded_move_preset_word(self, owner_base: int, parent: tk.Toplevel, chr_tbl_base_override: int | None = None) -> tuple[int, str] | None:
        rows = self._collect_move_preset_rows_for_base(owner_base, chr_tbl_base_override)
        if not rows:
            messagebox.showerror(
                "Move presets unavailable",
                f"No move/normal presets were harvested for slot base 0x{owner_base:08X}.",
                parent=parent,
            )
            return None

        result: dict[str, object] = {"value": None, "label": ""}
        dlg = tk.Toplevel(parent)
        dlg.title("Choose loaded move preset")
        dlg.geometry("760x520")
        dlg.transient(parent)
        dlg.grab_set()

        top = ttk.Frame(dlg)
        top.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(
            top,
            text=(
                f"Slot base: 0x{owner_base:08X}\n"
                + (f"Forced chr_tbl: 0x{int(chr_tbl_base_override):08X}\n" if chr_tbl_base_override else "")
                + "Pick a loaded move/normal. Named presets are ordered: 304-368, then 510-520, then 256-270, then named leftovers. Entries 369-400, idle, landing, filler, backward/forward movement, and air dash are filtered out. Unnamed entries are at the bottom."
            ),
            justify="left",
        ).pack(anchor="w")

        controls = ttk.Frame(dlg)
        controls.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(controls, text="Filter:").pack(side="left")
        filter_var = tk.StringVar()
        filter_entry = ttk.Entry(controls, textvariable=filter_var, width=28)
        filter_entry.pack(side="left", padx=(4, 12))
        ttk.Label(controls, text="Entry offset:").pack(side="left")
        offset_var = tk.StringVar(value="+0x00")
        offset_box = ttk.Combobox(
            controls,
            textvariable=offset_var,
            values=[f"+0x{delta:02X}" for delta in MOVE_PRESET_ENTRY_OFFSETS],
            width=8,
            state="readonly",
        )
        offset_box.pack(side="left")

        frame = ttk.Frame(dlg)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        cols = ("name", "table", "id", "word", "addr", "source")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=16)
        tree.heading("name", text="Move")
        tree.heading("table", text="Table")
        tree.heading("id", text="AnimID")
        tree.heading("word", text="Base Word")
        tree.heading("addr", text="Move Address")
        tree.heading("source", text="Source")
        tree.column("name", width=250, anchor="w")
        tree.column("table", width=70, anchor="center")
        tree.column("id", width=80, anchor="center")
        tree.column("word", width=105, anchor="center")
        tree.column("addr", width=115, anchor="center")
        tree.column("source", width=90, anchor="center")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        row_by_iid: dict[str, dict] = {}

        def populate() -> None:
            needle = filter_var.get().strip().lower()
            for iid in tree.get_children():
                tree.delete(iid)
            row_by_iid.clear()
            for row in rows:
                name = str(row.get("move_name", "move"))
                aid = int(row.get("move_id", 0))
                word = int(row.get("selector_word", 0))
                move_abs = int(row.get("move_abs", row.get("addr", 0)))
                source = str(row.get("source", ""))
                table_index = int(row.get("table_index", 0))
                named = bool(row.get("move_named")) and _is_named_preset_name(name)
                display_name = name if named else f"{name} (unnamed)"
                hay = f"{name} {aid:04X} {table_index:03X} {word:08X} {source}".lower()
                if needle and needle not in hay:
                    continue
                iid = tree.insert(
                    "",
                    "end",
                    values=(
                        display_name,
                        f"{table_index}",
                        f"0x{aid:04X}" if aid else "?",
                        f"0x{word:08X}",
                        f"0x{move_abs:08X}",
                        source,
                    ),
                )
                row_by_iid[iid] = row

        def parse_offset() -> int:
            text = offset_var.get().strip().replace("+", "")
            try:
                return int(text, 16)
            except Exception:
                return 0

        def choose_selected() -> None:
            sel = tree.selection()
            if not sel:
                messagebox.showerror("No move selected", "Select a move preset first.", parent=dlg)
                return
            row = row_by_iid.get(sel[0])
            if not row:
                return
            delta = parse_offset()
            base_word = int(row.get("selector_word", 0))
            value = (base_word + delta) & 0xFFFFFFFF
            name = str(row.get("move_name", "move"))
            result["value"] = value
            result["label"] = f"{name} +0x{delta:02X}"
            dlg.destroy()

        def on_filter(*_args) -> None:
            populate()

        filter_var.trace_add("write", on_filter)
        tree.bind("<Double-Button-1>", lambda _event: choose_selected())
        populate()
        first = tree.get_children()
        if first:
            tree.selection_set(first[0])
            tree.focus(first[0])

        bottom = ttk.Frame(dlg)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bottom, text="Use selected move", command=choose_selected).pack(side="right", padx=(8, 0))
        ttk.Button(bottom, text="Cancel", command=dlg.destroy).pack(side="right")
        filter_entry.focus_set()

        parent.wait_window(dlg)
        if result["value"] is None:
            return None
        return int(result["value"]), str(result["label"])


    def _choose_chun_selector_value(self, addr: int, current: str) -> tuple[int, bool] | None:
        result: dict[str, object] = {"value": None, "apply_all": True}
        dlg = tk.Toplevel(self.root)
        dlg.title("Choose Chun selector word")
        dlg.geometry("430x260")
        dlg.transient(self.root)
        dlg.grab_set()

        apply_all_var = tk.BooleanVar(value=True)

        ttk.Label(
            dlg,
            text=(
                f"Address: 0x{addr:08X}\n"
                f"Current: {current}\n\n"
                "Choose a loaded preset or enter a custom selector word. "
                "The confirmed Chun graft is installed before writing."
            ),
            justify="left",
        ).pack(anchor="w", padx=12, pady=(12, 8))

        ttk.Label(
            dlg,
            text="This writes all three Chun selector lanes together.",
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill="x", padx=12, pady=4)

        def set_value(v: int):
            result["value"] = v
            result["apply_all"] = True
            dlg.destroy()

        def set_default():
            result["value"] = "DEFAULT"
            result["apply_all"] = True
            dlg.destroy()

        def choose_loaded_move():
            owner_base = _owning_chr_tbl(addr)
            if owner_base is None:
                messagebox.showerror("No owner", "Could not resolve the character slot for this selector lane.", parent=dlg)
                return
            picked = self._choose_loaded_move_preset_word(owner_base, dlg)
            if picked is None:
                return
            value, _label = picked
            apply_all_var.set(True)
            set_value(value)

        def manual():
            text = simpledialog.askstring(
                "Custom Chun selector word",
                "Enter raw U32 selector word. Examples:\n"
                "0x000378AC\n"
                "00057480\n"
                "00 03 C4 AC",
                parent=dlg,
                initialvalue=current,
            )
            if text is None:
                return
            cleaned = text.strip().replace(" ", "").replace("_", "")
            if cleaned.lower().startswith("0x"):
                cleaned = cleaned[2:]
            try:
                value = int(cleaned, 16)
            except ValueError:
                messagebox.showerror("Invalid", f"{text!r} is not a u32 value.", parent=dlg)
                return
            if not (0 <= value <= 0xFFFFFFFF):
                messagebox.showerror("Out of range", "Value must be 0-0xFFFFFFFF.", parent=dlg)
                return
            set_value(value)

        ttk.Button(btn_frame, text="Preset assist / loaded move", command=choose_loaded_move).pack(fill="x", pady=3)
        ttk.Button(btn_frame, text="Custom raw U32", command=manual).pack(fill="x", pady=3)
        ttk.Button(btn_frame, text="Default / original assist", command=lambda: set_default()).pack(fill="x", pady=3)
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(fill="x", pady=3)
        self.root.wait_window(dlg)

        if result["value"] is None:
            return None
        if result["value"] == "DEFAULT":
            return None, True
        return int(result["value"]), bool(result["apply_all"])

    def _selector_target_for_display(self, addr: int, raw_val: int) -> str:
        selector_base = _selector_base_for_address(addr)
        if selector_base is None or raw_val > 0x100000:
            return ""
        return f"0x{selector_base + raw_val:08X}"

    def _apply_chun_selector_word(self, h: dict) -> None:
        graft_addr = int(h["block"])
        lane_addr = int(h["addr"])
        current = str(h.get("raw", "0x00000000"))

        choice = self._choose_chun_selector_value(lane_addr, current)
        if choice is None:
            return
        raw_val, apply_all = choice
        if raw_val is None:
            self._record_slot_profile(h, None, "default")
            self._update_flat_row_display(h, 0)
            h["entry"] = "default"
            h["raw"] = "default"
            h["target"] = "default"
            self._status.set(f"Stored default Chun assist profile for this fighter; shared table will restore on assist attack.")
            return
        payload = struct.pack(">I", raw_val)

        writes: dict[int, bytes] = {graft_addr: CHUN_SELECTOR_GRAFT_BLOCK}
        if apply_all:
            for off in CHUN_SELECTOR_WORD_OFFSETS:
                writes[graft_addr + off] = payload
        else:
            writes[lane_addr] = payload

        ok = self._write_many(
            writes,
            f"Installed Chun graft at 0x{graft_addr:08X} and wrote selector 0x{raw_val:08X}."
        )
        if not ok:
            return

        self._record_slot_profile(h, raw_val, "Chun assist")

        block_bytes = bytearray(CHUN_SELECTOR_GRAFT_BLOCK)
        for off in CHUN_SELECTOR_WORD_OFFSETS:
            if apply_all or graft_addr + off == lane_addr:
                block_bytes[off:off + 4] = payload

        for iid, row in self._hit_by_iid.items():
            if int(row.get("block", -1)) != graft_addr:
                continue
            if row.get("kind") == "chun-graft-table":
                hx = bytes(block_bytes).hex(" ").upper()
                self._tree.set(iid, "raw", hx)
                row["raw"] = hx
                row["guess"] = "confirmed Chun assist selector graft table; installed/edited"
                self._tree.set(iid, "guess", row["guess"])
                continue
            if row.get("kind") != "chun-selector-word":
                continue
            if h.get("fighter_base") is not None and row.get("fighter_base") != h.get("fighter_base"):
                continue
            row_addr = int(row["addr"])
            off = row_addr - graft_addr
            if off not in CHUN_SELECTOR_WORD_OFFSETS:
                continue
            if apply_all or row_addr == lane_addr:
                display_val = raw_val
            else:
                default_payload = CHUN_SELECTOR_RESET_WORDS.get(off)
                display_val = struct.unpack(">I", default_payload)[0] if default_payload else 0
            row["raw"] = f"0x{display_val:08X}"
            row["target"] = self._selector_target_for_display(row_addr, display_val)
            row["entry"] = _flat_selector_entry_label(_owning_chr_tbl(row_addr), [display_val], _read_slot_char_ids(), "Chun assist")
            row["guess"] = f"double-click to set Chun fighter profile; table 0x{graft_addr:08X}"
            self._tree.set(iid, "entry", row["entry"])
            self._tree.set(iid, "raw", row["raw"])
            self._tree.set(iid, "target", row["target"])
            self._tree.set(iid, "guess", row["guess"])

        self._last_chun_selector_addr = graft_addr
        self._last_chun_selector_test = f"0x{raw_val:08X}" + (" all lanes" if apply_all else f" at 0x{lane_addr:08X}")
        self._status.set(
            f"Chun graft installed at 0x{graft_addr:08X}; wrote 0x{raw_val:08X}"
            + (" to shared table lanes." if apply_all else f" to 0x{lane_addr:08X}.")
        )


    def _ryu_selector_candidate_score(self, base: int, idx: int, data: bytes,
                                      slot_char_ids: dict[int, int], source: str) -> int:
        addr = base + idx
        score = 0

        cid = slot_char_ids.get(base)
        if cid == 12:
            score += 100
        elif cid is not None:
            score -= 100

        if source == "graft":
            score += 40
        else:
            score += 20

        if _looks_like_ryu_selector_graft_shape(data, idx):
            score += 80

        score -= (_slot_index_for_base(base) or 0)
        score -= (addr & 0xFF) // 0x10
        return score

    def _find_ryu_selector_graft_addr(self) -> tuple[int, str]:
        if rbytes is None:
            raise RuntimeError("dolphin_io.rbytes unavailable.")

        slot_char_ids = _read_slot_char_ids()
        candidates: list[tuple[int, int, str]] = []

        for base in _CHR_TBL_BASES:
            try:
                data = self._read_memory_region(base, 0x90000, f"slot @ 0x{base:08X}")
            except Exception:
                continue

            pos = 0
            while True:
                idx = data.find(RYU_SELECTOR_GRAFT_BLOCK, pos)
                if idx < 0:
                    break
                pos = idx + 1
                score = self._ryu_selector_candidate_score(base, idx, data, slot_char_ids, "graft")
                candidates.append((score, base + idx, "graft"))

            pos = 0
            while True:
                idx = data.find(b"\x0F\x06\x00\x27", pos)
                if idx < 0:
                    break
                pos = idx + 1
                if not _looks_like_ryu_selector_graft_shape(data, idx):
                    continue
                score = self._ryu_selector_candidate_score(base, idx, data, slot_char_ids, "shape-fallback")
                candidates.append((score, base + idx, "shape-fallback"))

        if not candidates:
            raise RuntimeError(
                "Could not find the Ryu assist selector table. Load Ryu, call the assist once, then try again."
            )

        candidates.sort(key=lambda x: (x[0], -x[1]), reverse=True)
        _, addr, source = candidates[0]
        return addr, source

    def _restore_ryu_selector_block(self):
        try:
            addr, source = self._find_ryu_selector_graft_addr()
        except Exception as e:
            self._status.set("Ryu selector restore failed")
            messagebox.showerror("Restore Ryu Selector failed", str(e), parent=self.root)
            return

        ok = self._write_many(
            {addr: RYU_SELECTOR_GRAFT_BLOCK},
            f"Restored Ryu selector table at 0x{addr:08X}."
        )
        if not ok:
            return

        self._status.set(f"Restored Ryu selector table at 0x{addr:08X} using {source} match.")
        messagebox.showinfo(
            "Ryu selector restored",
            f"Restored Ryu selector table at 0x{addr:08X} using {source} match.",
            parent=self.root,
        )


    def _choose_ryu_graft_selector_value(self, addr: int, current: str) -> tuple[int, bool] | None:
        result: dict[str, object] = {"value": None, "apply_all": True}
        dlg = tk.Toplevel(self.root)
        dlg.title("Choose Ryu selector word")
        dlg.geometry("430x260")
        dlg.transient(self.root)
        dlg.grab_set()

        apply_all_var = tk.BooleanVar(value=True)

        ttk.Label(
            dlg,
            text=(
                f"Address: 0x{addr:08X}\n"
                f"Current: {current}\n\n"
                "Choose a loaded preset or enter a custom selector word. "
                "The Ryu selector setup is restored before writing."
            ),
            justify="left",
        ).pack(anchor="w", padx=12, pady=(12, 8))

        ttk.Label(
            dlg,
            text="This writes all three Ryu selector lanes together.",
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill="x", padx=12, pady=4)

        def set_value(v: int):
            result["value"] = v
            result["apply_all"] = True
            dlg.destroy()

        def set_default():
            result["value"] = "DEFAULT"
            result["apply_all"] = True
            dlg.destroy()

        def choose_loaded_move():
            owner_base = _owning_chr_tbl(addr)
            if owner_base is None:
                messagebox.showerror("No owner", "Could not resolve the character slot for this selector lane.", parent=dlg)
                return
            picked = self._choose_loaded_move_preset_word(owner_base, dlg)
            if picked is None:
                return
            value, _label = picked
            apply_all_var.set(True)
            set_value(value)

        def manual():
            text = simpledialog.askstring(
                "Custom Ryu selector word",
                "Enter raw U32 selector word. Examples:\n"
                "0x0003110C\n"
                "0003146C\n"
                "00 03 1C DC",
                parent=dlg,
                initialvalue=current,
            )
            if text is None:
                return
            cleaned = text.strip().replace(" ", "").replace("_", "")
            if cleaned.lower().startswith("0x"):
                cleaned = cleaned[2:]
            try:
                value = int(cleaned, 16)
            except ValueError:
                messagebox.showerror("Invalid", f"{text!r} is not a u32 value.", parent=dlg)
                return
            if not (0 <= value <= 0xFFFFFFFF):
                messagebox.showerror("Out of range", "Value must be 0-0xFFFFFFFF.", parent=dlg)
                return
            set_value(value)

        ttk.Button(btn_frame, text="Preset assist / loaded move", command=choose_loaded_move).pack(fill="x", pady=3)
        ttk.Button(btn_frame, text="Custom raw U32", command=manual).pack(fill="x", pady=3)
        ttk.Button(btn_frame, text="Default / original assist", command=lambda: set_default()).pack(fill="x", pady=3)
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(fill="x", pady=3)
        self.root.wait_window(dlg)

        if result["value"] is None:
            return None
        if result["value"] == "DEFAULT":
            return None, True
        return int(result["value"]), bool(result["apply_all"])

    def _apply_ryu_selector_word(self, h: dict) -> None:
        graft_addr = int(h["block"])
        lane_addr = int(h["addr"])
        current = str(h.get("raw", "0x00000000"))

        choice = self._choose_ryu_graft_selector_value(lane_addr, current)
        if choice is None:
            return
        raw_val, apply_all = choice
        if raw_val is None:
            # VJoe generic/direct rows may have also patched the visible
            # animation state. Restore that state when returning this profile
            # to default.
            if int(h.get("char_id") or 0) == VJOE_CHAR_ID or _is_vjoe_owner_base(
                int(h.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
            ):
                owner_base = int(h.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
                state_writes = _vjoe_animation_state_writes(owner_base, None)
                if state_writes:
                    self._write_many(state_writes, "Restored VJoe visible attack animation state.")

            self._record_slot_profile(h, None, "default")
            h["entry"] = "default"
            h["raw"] = "default"
            h["target"] = "default"
            self._status.set(f"Stored default Ryu assist profile for this fighter; shared table will restore on assist attack.")
            return
        payload = struct.pack(">I", raw_val)

        writes: dict[int, bytes] = {graft_addr: RYU_SELECTOR_GRAFT_BLOCK}
        if apply_all:
            for off in RYU_SELECTOR_WORD_OFFSETS:
                writes[graft_addr + off] = payload
        else:
            writes[lane_addr] = payload

        ok = self._write_many(
            writes,
            f"Restored Ryu selector table at 0x{graft_addr:08X} and wrote selector 0x{raw_val:08X}."
        )
        if not ok:
            return

        self._record_slot_profile(h, raw_val, "Ryu assist")

        block_bytes = bytearray(RYU_SELECTOR_GRAFT_BLOCK)
        for off in RYU_SELECTOR_WORD_OFFSETS:
            if apply_all or graft_addr + off == lane_addr:
                block_bytes[off:off + 4] = payload

        for iid, row in self._hit_by_iid.items():
            if int(row.get("block", -1)) != graft_addr:
                continue
            if row.get("kind") == "ryu-graft-table":
                hx = bytes(block_bytes).hex(" ").upper()
                self._tree.set(iid, "raw", hx)
                row["raw"] = hx
                row["guess"] = "confirmed Ryu assist selector table; installed/edited"
                self._tree.set(iid, "guess", row["guess"])
                continue
            if row.get("kind") != "ryu-selector-word":
                continue
            if h.get("fighter_base") is not None and row.get("fighter_base") != h.get("fighter_base"):
                continue
            row_addr = int(row["addr"])
            off = row_addr - graft_addr
            if off not in RYU_SELECTOR_WORD_OFFSETS:
                continue
            if apply_all or row_addr == lane_addr:
                display_val = raw_val
            else:
                default_payload = RYU_SELECTOR_RESET_WORDS.get(off)
                display_val = struct.unpack(">I", default_payload)[0] if default_payload else 0
            row["raw"] = f"0x{display_val:08X}"
            row["target"] = self._selector_target_for_display(row_addr, display_val)
            row["entry"] = _flat_selector_entry_label(_owning_chr_tbl(row_addr), [display_val], _read_slot_char_ids(), "Ryu assist")
            row["guess"] = f"double-click to set Ryu fighter profile; table 0x{graft_addr:08X}"
            self._tree.set(iid, "entry", row["entry"])
            self._tree.set(iid, "raw", row["raw"])
            self._tree.set(iid, "target", row["target"])
            self._tree.set(iid, "guess", row["guess"])

        self._status.set(
            f"Ryu selector table restored at 0x{graft_addr:08X}; wrote 0x{raw_val:08X}"
            + (" to shared table lanes." if apply_all else f" to 0x{lane_addr:08X}.")
        )


    def _choose_generic_selector_value(self, addr: int, current: str, source: str, owner_base_override: int | None = None, chr_tbl_base_override: int | None = None) -> tuple[int, bool] | None:
        result: dict[str, object] = {"value": None, "apply_all": True}
        dlg = tk.Toplevel(self.root)
        dlg.title("Choose generic selector word")
        dlg.geometry("450x280")
        dlg.transient(self.root)
        dlg.grab_set()

        # For a graft candidate, there are no valid selector words in the block yet.
        # Applying the chosen value to all lanes gives the graft safe/consistent words.
        apply_all_var = tk.BooleanVar(value=True)

        ttk.Label(
            dlg,
            text=(
                f"Address: 0x{addr:08X}\n"
                f"Current: {current}\n"
                f"Source: {source}\n"
                f"Preset owner: 0x{(int(owner_base_override) if owner_base_override else (_owning_chr_tbl(addr) or 0)):08X}\n"
                + (f"Preset chr_tbl: 0x{int(chr_tbl_base_override):08X}\n" if chr_tbl_base_override else "")
                + "\nChoose a loaded preset or enter a custom selector word. "
                + ("This Tatsunoko row preserves chr_tbl[426] and grafts the early 426 attack body."
                   if "tatsunoko-wrapper-graft" in str(source) else
                   ("This Tatsunoko row preserves chr_tbl[426] and patches internal wrapper calls."
                    if "direct-426" in str(source) else
                    "The generic selector setup is installed before writing."))
            ),
            justify="left",
        ).pack(anchor="w", padx=12, pady=(12, 8))

        ttk.Checkbutton(
            dlg,
            text="Apply this word to all three generic selector lanes",
            variable=apply_all_var,
        ).pack(anchor="w", padx=12, pady=(0, 8))

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill="x", padx=12, pady=4)

        def set_value(v: int):
            result["value"] = v
            result["apply_all"] = bool(apply_all_var.get())
            dlg.destroy()

        def set_default():
            result["value"] = "DEFAULT"
            result["apply_all"] = True
            dlg.destroy()

        def choose_loaded_move():
            owner_base = int(owner_base_override) if owner_base_override else _owning_chr_tbl(addr)
            if owner_base is None:
                messagebox.showerror("No owner", "Could not resolve the character slot for this selector lane.", parent=dlg)
                return
            picked = self._choose_loaded_move_preset_word(owner_base, dlg, chr_tbl_base_override)
            if picked is None:
                return
            value, _label = picked
            apply_all_var.set(True)
            set_value(value)

        def manual():
            text = simpledialog.askstring(
                "Custom generic selector word",
                "Enter raw U32 selector word. Examples:\n"
                "0x0003D620\n"
                "0003110C\n"
                "00 03 C4 AC",
                parent=dlg,
                initialvalue=current,
            )
            if text is None:
                return
            cleaned = text.strip().replace(" ", "").replace("_", "")
            if cleaned.lower().startswith("0x"):
                cleaned = cleaned[2:]
            try:
                value = int(cleaned, 16)
            except ValueError:
                messagebox.showerror("Invalid", f"{text!r} is not a u32 value.", parent=dlg)
                return
            if not (0 <= value <= 0xFFFFFFFF):
                messagebox.showerror("Out of range", "Value must be 0-0xFFFFFFFF.", parent=dlg)
                return
            set_value(value)

        ttk.Button(btn_frame, text="Preset assist / loaded move", command=choose_loaded_move).pack(fill="x", pady=3)
        ttk.Button(btn_frame, text="Custom raw U32", command=manual).pack(fill="x", pady=3)
        ttk.Button(btn_frame, text="Default / original assist", command=lambda: set_default()).pack(fill="x", pady=3)
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(fill="x", pady=3)
        self.root.wait_window(dlg)

        if result["value"] is None:
            return None
        if result["value"] == "DEFAULT":
            return None, True
        return int(result["value"]), bool(result["apply_all"])

    def _read_generic_block_for_graft(self, graft_addr: int) -> bytes:
        if rbytes is None:
            raise RuntimeError("dolphin_io.rbytes unavailable.")
        data = rbytes(graft_addr, GENERIC_SELECTOR_TABLE_LEN) or b""
        if len(data) != GENERIC_SELECTOR_TABLE_LEN:
            raise RuntimeError(
                f"Could not read generic graft block at 0x{graft_addr:08X}; got 0x{len(data):X} byte(s)."
            )
        return data

    def _generic_block_args_and_words(self, graft_addr: int, source: str,
                                      raw_val: int, apply_all: bool,
                                      lane_addr: int) -> bytes:
        try:
            current = self._read_generic_block_for_graft(graft_addr)
        except Exception:
            current = b""

        words = [raw_val, raw_val, raw_val]
        arg32 = GENERIC_SELECTOR_DEFAULT_ARG_32
        arg33 = GENERIC_SELECTOR_DEFAULT_ARG_33

        if len(current) == GENERIC_SELECTOR_TABLE_LEN and current.startswith(GENERIC_SELECTOR_SETUP_PREFIX):
            # Already native/installed. Preserve existing words unless applying all.
            for i, off in enumerate(GENERIC_SELECTOR_WORD_OFFSETS):
                old = struct.unpack(">I", current[off:off + 4])[0]
                words[i] = raw_val if (apply_all or graft_addr + off == lane_addr) else old
            arg32 = current[0x20:0x24]
            arg33 = current[0x28:0x2C]
        elif len(current) == GENERIC_SELECTOR_TABLE_LEN and current[0:4] == b"\x0F\x06\x00\x27" and current[4:8] == SELECTOR_TAIL:
            # Chun/Morrigan-style graft candidate. Preserve the original
            # 37 32 / 37 33 args.
            words = [raw_val, raw_val, raw_val]
            arg32 = current[0x08:0x0C]
            arg33 = current[0x10:0x14]
        elif len(current) == GENERIC_SELECTOR_TABLE_LEN and current[0x1C:0x20] == SELECTOR_TAIL and current[0x24:0x28] == SELECTOR_COMPANION_TAIL:
            # Loose Batsu-style tail-pair candidate. The graft is aligned so the
            # existing 37 32 / 37 33 pair lands at +0x1C/+0x24.
            words = [raw_val, raw_val, raw_val]
            arg32 = current[0x20:0x24]
            arg33 = current[0x28:0x2C]

        return _build_generic_selector_graft_block(tuple(words), arg32, arg33)

    def _apply_direct_426_word(self, h: dict) -> None:
        lane_addr = int(h["addr"])
        source = str(h.get("source", "tatsunoko-direct-426"))
        current = str(h.get("raw", "default"))
        owner_base = int(h.get("owner_base") or (_owning_chr_tbl(lane_addr) or 0))

        choice = self._choose_generic_selector_value(lane_addr, current, source, owner_base)
        if choice is None:
            return
        raw_val, _apply_all = choice

        info = dict(h.get("direct_426_info") or {})
        writes = _direct_426_writes(owner_base, raw_val, info)
        if not writes:
            messagebox.showerror(
                "Direct 426 failed",
                "Could not resolve this character's chr_tbl[426] entry.",
                parent=self.root,
            )
            return

        if raw_val is None:
            ok = self._write_many(writes, "Restored Tatsunoko active wrapper operands to the original assist route.")
            if not ok:
                return
            self._record_slot_profile(h, None, "default")
            h["entry"] = "default"
            h["raw"] = "default"
            h["target"] = f"orig 0x{int(info.get('default_word', 0)):08X}"
            h["guess"] = "Tatsunoko wrapper default; original assist attack restored"
        else:
            raw_val = int(raw_val) & 0xFFFFFFFF
            ok = self._write_many(writes, f"Tatsunoko active wrapper wrote internal attack calls -> 0x{raw_val:08X}.")
            if not ok:
                return
            self._record_slot_profile(h, raw_val, "Tatsunoko active wrapper")
            slot_char_ids = _read_slot_char_ids()
            name = _flat_selector_entry_label(owner_base, [raw_val], slot_char_ids, "selected move")
            chr_tbl_base = int(info.get("chr_tbl_base") or (_chr_tbl_base_for_owner_base(owner_base) or 0))
            h["entry"] = name
            h["raw"] = f"0x{raw_val:08X}"
            h["target"] = f"0x{chr_tbl_base + raw_val:08X}" if chr_tbl_base and raw_val <= MOVE_PRESET_SLOT_SIZE else ""
            h["guess"] = "Tatsunoko active wrapper profile; chr_tbl[426] preserved and internal attack calls patched"

        for iid, row in self._hit_by_iid.items():
            if row is h or (
                str(row.get("typ", "")) == "u32-direct-426-fallback"
                and int(row.get("owner_base") or 0) == owner_base
            ):
                row["entry"] = h["entry"]
                row["raw"] = h["raw"]
                row["target"] = h["target"]
                row["guess"] = h["guess"]
                self._tree.set(iid, "entry", row["entry"])
                self._tree.set(iid, "raw", row["raw"])
                self._tree.set(iid, "target", row["target"])
                self._tree.set(iid, "guess", row["guess"])

        self._status.set("Tatsunoko active-wrapper profile stored and written immediately.")


    def _apply_vjoe_trampoline_word(self, h: dict) -> None:
        lane_addr = int(h["addr"])
        source = str(h.get("source", "vjoe-static-426"))
        current = str(h.get("raw", "default"))

        owner_base = int(h.get("owner_base") or (_owning_chr_tbl(lane_addr) or 0))
        choice = self._choose_generic_selector_value(lane_addr, current, source, owner_base)
        if choice is None:
            return
        raw_val, _apply_all = choice

        info = dict(h.get("vjoe_info") or {})
        writes = _vjoe_trampoline_writes(owner_base, raw_val, info)
        if not writes:
            messagebox.showerror(
                "VJoe static direct failed",
                "Could not resolve VJoe chr_tbl[426] for static/direct patch.",
                parent=self.root,
            )
            return

        if raw_val is None:
            ok = self._write_many(writes, "Restored VJoe chr_tbl[426] to the original assist attack route.")
            if not ok:
                return
            self._record_slot_profile(h, None, "default")
            h["entry"] = "VJoe default"
            h["raw"] = "default"
            h["target"] = f"orig 0x{int(info.get('default_word', 0)):08X}"
            h["guess"] = "VJoe static direct default; chr_tbl[426] restores original route"
            self._status.set("Stored VJoe default profile; chr_tbl[426] restored immediately.")
        else:
            ok = self._write_many(
                writes,
                f"VJoe static direct wrote chr_tbl[426] -> 0x{int(raw_val) & 0xFFFFFFFF:08X}."
            )
            if not ok:
                return
            self._record_slot_profile(h, int(raw_val), "VJoe static 426")
            slot_char_ids = _read_slot_char_ids()
            name = _flat_selector_entry_label(owner_base, [int(raw_val)], slot_char_ids, "VJoe selected move")
            h["entry"] = name
            h["raw"] = f"0x{int(raw_val) & 0xFFFFFFFF:08X}"
            sid = _vjoe_state_id_for_selector_word(owner_base, int(raw_val))
            state_txt = f"; state 0x{int(sid):04X}" if sid is not None else "; state unchanged"
            h["target"] = f"chr_tbl[426] <- 0x{int(raw_val) & 0xFFFFFFFF:08X}" + state_txt
            h["guess"] = "VJoe static/direct active; route/spawn path plus visible attack state are patched"

        for iid, row in self._hit_by_iid.items():
            if row is h or (
                str(row.get("typ", "")) == "u32-vjoe-trampoline"
                and int(row.get("owner_base") or 0) == owner_base
            ):
                row["entry"] = h["entry"]
                row["raw"] = h["raw"]
                row["target"] = h["target"]
                row["guess"] = h["guess"]
                self._tree.set(iid, "entry", row["entry"])
                self._tree.set(iid, "raw", row["raw"])
                self._tree.set(iid, "target", row["target"])
                self._tree.set(iid, "guess", row["guess"])

        self._status.set(
            "VJoe static-direct profile stored and written immediately. Auto Assist Trigger will skip VJoe while VJoe Static Direct is ON."
        )



    def _apply_generic_selector_word(self, h: dict) -> None:
        graft_addr = int(h["block"])
        lane_addr = int(h["addr"])
        source = str(h.get("source", "unknown"))
        current = str(h.get("raw", "0x00000000"))

        owner_override = None
        chr_tbl_override = None
        row_source = str(h.get("source", ""))
        if int(h.get("char_id") or 0) == VJOE_CHAR_ID or row_source.startswith("vjoe-"):
            owner_override = int(h.get("owner_base") or 0) or None

        # Tatsunoko wrapper rows keep direct_426_info even after the first
        # successful install changes source to "installed" for display. Do not
        # re-resolve presets from the clicked edit address on later edits; that
        # falls back to the broad 0x909478E0 slot and can return an empty picker.
        if row_source.startswith("tatsunoko-wrapper-graft") or h.get("direct_426_info"):
            owner_override = int(h.get("owner_base") or 0) or None
            info = h.get("direct_426_info") or {}
            try:
                chr_tbl_override = int(info.get("chr_tbl_base") or 0) or None
            except Exception:
                chr_tbl_override = None
        choice = self._choose_generic_selector_value(lane_addr, current, source, owner_override, chr_tbl_override)
        if choice is None:
            return
        raw_val, apply_all = choice

        if raw_val is None:
            if str(h.get("typ", "")) == "u32-anchored-assist-fallback":
                owner_base = int(h.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
                table_index = int(h.get("anchor_table_index") or ANCHOR_ASSIST_FALLBACK_TABLE_INDEX)
                default_word = int(h.get("anchor_default_word") or 0) or None
                operand_offsets = list(h.get("anchor_operand_offsets") or [])
                operand_words = list(h.get("anchor_operand_words") or [])
                writes = _anchored_assist_fallback_writes(
                    owner_base, None, table_index, default_word, operand_offsets, operand_words
                )
                if writes:
                    self._write_many(
                        writes,
                        f"Restored anchored fallback chr_tbl[{table_index}] wrapper and internal operands."
                    )

            self._record_slot_profile(h, None, "default")
            h["entry"] = "default"
            h["raw"] = "default"
            h["target"] = "default"
            self._status.set("Stored default generic assist profile for this fighter; shared table will restore on assist attack if possible.")
            return

        if str(h.get("typ", "")) == "u32-anchored-assist-fallback":
            owner_base = int(h.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
            table_index = int(h.get("anchor_table_index") or ANCHOR_ASSIST_FALLBACK_TABLE_INDEX)
            default_word = int(h.get("anchor_default_word") or 0) or None
            operand_offsets = list(h.get("anchor_operand_offsets") or [])
            operand_words = list(h.get("anchor_operand_words") or [])

            writes = _anchored_assist_fallback_writes(
                owner_base, raw_val, table_index, default_word, operand_offsets, operand_words
            )
            if not writes:
                messagebox.showerror(
                    "Anchored assist fallback failed",
                    f"Could not resolve internal wrapper operands for chr_tbl[{table_index}].",
                    parent=self.root,
                )
                return

            ok = self._write_many(
                writes,
                f"Patched anchored fallback internal operands to 0x{raw_val:08X}; chr_tbl[{table_index}] kept on wrapper."
            )
            if not ok:
                return

            self._record_slot_profile(h, raw_val, "anchored assist fallback")
            self._update_flat_row_display(h, raw_val)

            for iid, row in self._hit_by_iid.items():
                if row is h or (
                    int(row.get("fighter_base") or 0) == int(h.get("fighter_base") or -1)
                    and int(row.get("char_id") or 0) == int(h.get("char_id") or -2)
                    and str(row.get("typ", "")) == "u32-anchored-assist-fallback"
                ):
                    row["entry"] = h["entry"]
                    row["raw"] = h["raw"]
                    row["target"] = h["target"]
                    row["guess"] = f"anchored wrapper profile; chr_tbl[{table_index}] preserved"
                    self._tree.set(iid, "entry", row["entry"])
                    self._tree.set(iid, "raw", row["raw"])
                    self._tree.set(iid, "target", row["target"])
                    self._tree.set(iid, "guess", row["guess"])

            self._status.set(
                f"Anchored assist fallback profile stored as 0x{raw_val:08X}; chr_tbl[{table_index}] stays on wrapper."
            )
            return

        try:
            block = self._generic_block_args_and_words(graft_addr, source, raw_val, apply_all, lane_addr)
        except Exception as e:
            messagebox.showerror("Generic graft failed", str(e), parent=self.root)
            return

        writes = {graft_addr: block}
        owner_base_for_state = int(h.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
        if int(h.get("char_id") or 0) == VJOE_CHAR_ID or _is_vjoe_owner_base(owner_base_for_state):
            writes.update(_vjoe_animation_state_writes(owner_base_for_state, int(raw_val)))

        ok = self._write_many(
            writes,
            f"Installed generic selector table at 0x{graft_addr:08X} and wrote selector 0x{raw_val:08X}."
        )
        if not ok:
            return

        self._record_slot_profile(h, raw_val, "generic assist")

        for iid, row in self._hit_by_iid.items():
            if int(row.get("block", -1)) != graft_addr:
                continue
            if row.get("kind") in ("generic-native-table", "generic-graft-table"):
                hx = block.hex(" ").upper()
                self._tree.set(iid, "raw", hx)
                row["raw"] = hx
                row["guess"] = "experimental generic selector table; installed/edited"
                self._tree.set(iid, "guess", row["guess"])
                if row.get("direct_426_info"):
                    row["source"] = "tatsunoko-wrapper-graft-installed"
                else:
                    row["source"] = "installed"
                continue
            if row.get("kind") != "generic-selector-word":
                continue
            if h.get("fighter_base") is not None and row.get("fighter_base") != h.get("fighter_base"):
                continue
            row_addr = int(row["addr"])
            off = row_addr - graft_addr
            if off not in GENERIC_SELECTOR_WORD_OFFSETS:
                continue
            display_val = struct.unpack(">I", block[off:off + 4])[0]
            owner_base = _owning_chr_tbl(row_addr)
            row["raw"] = f"0x{display_val:08X}"
            row["target"] = self._selector_target_for_display(row_addr, display_val)
            row["entry"] = _flat_selector_entry_label(
                owner_base,
                [display_val],
                _read_slot_char_ids(),
                "generic assist",
            )
            row["guess"] = "double-click to set generic fighter profile"
            if row.get("direct_426_info"):
                row["source"] = "tatsunoko-wrapper-graft-installed"
            else:
                row["source"] = "installed"
            self._tree.set(iid, "entry", row["entry"])
            self._tree.set(iid, "raw", row["raw"])
            self._tree.set(iid, "target", row["target"])
            self._tree.set(iid, "guess", row["guess"])

        self._status.set(
            f"Generic selector table installed at 0x{graft_addr:08X}; wrote 0x{raw_val:08X}"
            + (" to shared table lanes." if apply_all else f" using lane 0x{lane_addr:08X}.")
        )



    def _find_generic_selector_graft_addr_for_base(self, owner_base: int) -> tuple[int, str]:
        data = self._read_memory_region(owner_base, 0x90000, f"slot @ 0x{owner_base:08X}")
        candidates: list[tuple[int, int, str]] = []

        def add_candidate(idx: int, source: str) -> None:
            if idx < 0 or idx + GENERIC_SELECTOR_TABLE_LEN > len(data):
                return
            block_addr = owner_base + idx
            bonus = _generic_assist_route_bonus(owner_base, block_addr)
            source_label = source
            if bonus and "assist-route" not in source_label:
                source_label = f"{source_label}+assist-route"
            score = _generic_candidate_score(data, idx, source_label) + bonus
            candidates.append((score, block_addr, source_label))

        pos = 0
        while True:
            idx = data.find(b"\x0F\x06\x00\x27", pos)
            if idx < 0:
                break
            pos = idx + 1
            if _looks_like_generic_native_selector_shape(data, idx):
                add_candidate(idx, "native")
            elif _looks_like_generic_graft_candidate_shape(data, idx):
                add_candidate(idx, "graft-candidate")

        # Experimental fallback: align a synthetic graft 0x1C bytes before a
        # 37 32 / 37 33 tail pair. Assist-route candidates can be thinner.
        pos = 0
        while True:
            tail_idx = data.find(SELECTOR_TAIL, pos)
            if tail_idx < 0:
                break
            pos = tail_idx + 1
            idx = tail_idx - 0x1C
            if idx < 0 or idx + GENERIC_SELECTOR_TABLE_LEN > len(data):
                continue

            block_addr = owner_base + idx
            assist_route = bool(_generic_assist_route_bonus(owner_base, block_addr))
            if not _looks_like_generic_tail_pair_candidate(data, tail_idx, allow_single_row=assist_route):
                continue

            add_candidate(idx, "tail-pair")

        if not candidates:
            raise RuntimeError(
                f"No generic selector/graft candidate found in slot base 0x{owner_base:08X}. "
                "For this character, find the assist block first or use a confirmed Chun/Ryu table."
            )

        def candidate_key(item: tuple[int, int, str]) -> tuple[int, int, int]:
            score, addr, source = item
            base_source = (
                str(source or "")
                .replace("+assist-route", "")
                .replace("-assist-route", "")
            )

            # Keep legacy generic grafts ahead of loose tail-pair fallback, but
            # let the anchored tail-pair route beat unrelated native-looking
            # selector tables. This is what keeps Alex fixed without stealing
            # Jun/Ken-style generic grafts.
            if base_source in ("graft-candidate", "installed"):
                priority = 50
            elif "assist-route" in str(source or "") and base_source == "native":
                priority = 45
            elif base_source == "tail-pair" and "assist-route" in str(source or ""):
                priority = 40
            elif base_source == "native":
                priority = 30
            else:
                priority = 10

            return (priority, int(score), -int(addr))

        candidates.sort(key=candidate_key, reverse=True)
        _score, addr, source = candidates[0]
        return addr, source


    def _choose_assist_move_preset_word(self, h: dict) -> tuple[int, bool, str] | None:
        move_name = str(h.get("move_name", "move"))
        move_abs = int(h.get("move_abs", h.get("addr", 0)))
        base_word = int(h.get("selector_word", 0))
        current = f"0x{base_word:08X}"

        result: dict[str, object] = {"value": None, "apply_all": True, "label": ""}
        dlg = tk.Toplevel(self.root)
        dlg.title("Choose assist move entry")
        dlg.geometry("500x430")
        dlg.transient(self.root)
        dlg.grab_set()

        apply_all_var = tk.BooleanVar(value=True)

        ttk.Label(
            dlg,
            text=(
                f"Move: {move_name}\n"
                f"Move address: 0x{move_abs:08X}\n"
                f"Base selector word: {current}\n\n"
                "Pick which entry point to write into the graft lanes. "
                "Base is fastest; +0x70/+0x90 are useful Ryu-style continuation probes."
            ),
            justify="left",
        ).pack(anchor="w", padx=12, pady=(12, 8))

        ttk.Checkbutton(
            dlg,
            text="Apply this word to all three selector lanes",
            variable=apply_all_var,
        ).pack(anchor="w", padx=12, pady=(0, 8))

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill="x", padx=12, pady=4)

        def set_value(v: int, label: str):
            result["value"] = v
            result["label"] = label
            result["apply_all"] = bool(apply_all_var.get())
            dlg.destroy()

        for delta in MOVE_PRESET_ENTRY_OFFSETS:
            v = (base_word + delta) & 0xFFFFFFFF
            ttk.Button(
                btn_frame,
                text=f"{move_name} +0x{delta:02X}  0x{v:08X}",
                command=lambda vv=v, dd=delta: set_value(vv, f"{move_name} +0x{dd:02X}"),
            ).pack(fill="x", pady=2)

        def manual():
            text = simpledialog.askstring(
                "Manual selector word",
                "Enter raw U32 selector word. Examples:\n"
                "0x000378AC\n"
                "00057480\n"
                "00 03 C4 AC",
                parent=dlg,
                initialvalue=current,
            )
            if text is None:
                return
            cleaned = text.strip().replace(" ", "").replace("_", "")
            if cleaned.lower().startswith("0x"):
                cleaned = cleaned[2:]
            try:
                value = int(cleaned, 16)
            except ValueError:
                messagebox.showerror("Invalid", f"{text!r} is not a u32 value.", parent=dlg)
                return
            if not (0 <= value <= 0xFFFFFFFF):
                messagebox.showerror("Out of range", "Value must be 0-0xFFFFFFFF.", parent=dlg)
                return
            set_value(value, "manual")

        ttk.Button(btn_frame, text="Manual raw U32", command=manual).pack(fill="x", pady=(10, 3))
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(fill="x", pady=3)
        self.root.wait_window(dlg)

        if result["value"] is None:
            return None
        if result["value"] == "DEFAULT":
            return None, True
        return int(result["value"]), bool(result["apply_all"]), str(result["label"])

    def _apply_assist_move_preset(self, h: dict) -> None:
        choice = self._choose_assist_move_preset_word(h)
        if choice is None:
            return

        raw_val, apply_all, label = choice
        owner_base = int(h.get("owner_base", 0))
        char_id = int(h.get("char_id", 0))
        move_name = str(h.get("move_name", "move"))
        payload = struct.pack(">I", raw_val)

        try:
            if char_id == 13:
                graft_addr, source = self._find_chun_selector_graft_addr()
                writes: dict[int, bytes] = {graft_addr: CHUN_SELECTOR_GRAFT_BLOCK}
                lane_offsets = CHUN_SELECTOR_WORD_OFFSETS
                table_name = "Chun"
            elif char_id == 12:
                graft_addr, source = self._find_ryu_selector_graft_addr()
                writes = {graft_addr: RYU_SELECTOR_GRAFT_BLOCK}
                lane_offsets = RYU_SELECTOR_WORD_OFFSETS
                table_name = "Ryu"
            else:
                graft_addr, source = self._find_generic_selector_graft_addr_for_base(owner_base)
                source_base = (
                    str(source or "")
                    .replace("+assist-route", "")
                    .replace("-assist-route", "")
                )

                if source_base == "tail-pair" and "assist-route" in str(source or ""):
                    table_index = ANCHOR_ASSIST_FALLBACK_TABLE_INDEX
                    default_word = _chr_tbl_word_for_owner_base(owner_base, table_index)
                    anchor_operands = _anchored_assist_operands_for_owner_base(
                        owner_base,
                        table_index,
                        default_word,
                    )
                    writes = _anchored_assist_fallback_writes(
                        owner_base,
                        raw_val,
                        table_index,
                        default_word,
                        [off for off, _word in anchor_operands],
                        [word for _off, word in anchor_operands],
                    )
                    if not writes:
                        raise RuntimeError(
                            "Found only an anchored assist fallback, but no internal wrapper operands were writable."
                        )
                    lane_offsets = ()
                    table_name = "anchored generic"
                else:
                    block = self._generic_block_args_and_words(
                        graft_addr,
                        source,
                        raw_val,
                        True,
                        graft_addr + GENERIC_SELECTOR_WORD_OFFSETS[0],
                    )
                    writes = {graft_addr: block}
                    lane_offsets = GENERIC_SELECTOR_WORD_OFFSETS
                    table_name = "generic"
        except Exception as e:
            self._status.set("Assist move preset failed")
            messagebox.showerror("Assist move preset failed", str(e), parent=self.root)
            return

        if char_id in (12, 13):
            if apply_all:
                for off in lane_offsets:
                    writes[graft_addr + off] = payload
            else:
                writes[graft_addr + lane_offsets[0]] = payload

        ok = self._write_many(
            writes,
            f"Applied {move_name} selector 0x{raw_val:08X} to {table_name} graft at 0x{graft_addr:08X}."
        )
        if not ok:
            return

        lane_lines = []
        if char_id in (12, 13):
            target_offsets = lane_offsets if apply_all else (lane_offsets[0],)
            lane_lines = [f"0x{graft_addr + off:08X} <- {payload.hex(' ').upper()}" for off in target_offsets]
        else:
            if lane_offsets:
                lane_lines = [f"0x{graft_addr + off:08X} <- {payload.hex(' ').upper()}" for off in lane_offsets]
            else:
                lane_lines = [
                    f"0x{addr:08X} <- {data.hex(' ').upper()}"
                    for addr, data in sorted(writes.items())
                ]

        messagebox.showinfo(
            "Assist move preset applied",
            (
                f"{label}\n"
                f"Move: {move_name}\n"
                f"Selector word: 0x{raw_val:08X}\n"
                f"Table: {table_name} at 0x{graft_addr:08X} ({source})\n\n"
                + "\n".join(lane_lines)
                + "\n\nTest assist now. If it stalls, retry the same move with +0x10, +0x20, +0x70, or +0x90."
            ),
            parent=self.root,
        )

        self._status.set(
            f"Applied assist move preset {move_name} ({label}) as 0x{raw_val:08X}."
        )

    def _profile_key_for_row(self, h: dict) -> int | None:
        fighter_base = int(h.get("fighter_base") or 0)
        if fighter_base:
            return fighter_base
        owner_base = _owning_chr_tbl(int(h.get("addr", h.get("block", 0))))
        if owner_base is None:
            owner_base = _owning_chr_tbl(int(h.get("block", 0)))
        return int(owner_base) if owner_base is not None else None

    def _default_profile_label_for_row(self, h: dict) -> str:
        return "default"

    def _record_default_slot_profile(self, h: dict) -> None:
            fighter_base = int(h.get("fighter_base") or 0)
            owner_base = int(h.get("owner_base") or (_owning_chr_tbl(int(h.get("addr", h.get("block", 0)))) or 0))
            char_id = int(h.get("char_id") or 0)
            profile = {
                "word": None,
                "label": self._default_profile_label_for_row(h),
                "row": dict(h),
                "fighter_base": fighter_base or owner_base,
                "owner_base": owner_base,
                "char_id": char_id,
                "is_default": True,
            }
            # Key by fighter_base when available (duplicate-char path), always also
            # key by owner_base so the runtime snap lookup finds it by either key.
            keys = set()
            if fighter_base:
                keys.add(fighter_base)
            if owner_base:
                keys.add(owner_base)
            for key in keys:
                if key not in self._slot_assist_profiles:
                    self._slot_assist_profiles[key] = profile

    def _record_slot_profile(self, h: dict, raw_val: int | None, label: str = "assist") -> None:
            fighter_base = int(h.get("fighter_base") or 0)
            owner_base = int(h.get("owner_base") or (_owning_chr_tbl(int(h.get("addr", h.get("block", 0)))) or 0))
            char_id = int(h.get("char_id") or 0)
            profile = {
                "word": None if raw_val is None else (int(raw_val) & 0xFFFFFFFF),
                "label": str(label or "assist"),
                "row": dict(h),
                "fighter_base": fighter_base or owner_base,
                "owner_base": owner_base,
                "char_id": char_id,
                "is_default": raw_val is None,
            }
            # Key by fighter_base (duplicate-char) and owner_base. An explicit set
            # always overwrites both keys so the runtime snap lookup is always fresh.
            keys = set()
            if fighter_base:
                keys.add(fighter_base)
            if owner_base:
                keys.add(owner_base)
            for key in keys:
                self._slot_assist_profiles[key] = profile

    def _write_many_silent(self, writes: dict[int, bytes], *, urgent: bool = False) -> bool:
        if not writes:
            return True
        if _rpm is not None:
            try:
                return bool(_rpm.write_many(
                    writes,
                    key="assist:window:urgent" if urgent else "assist:window",
                    priority=98 if urgent else 70,
                    dirty=False if urgent else True,
                    force=True if urgent else False,
                    cache_ttl_sec=0.0,
                ))
            except Exception:
                pass
        if wbytes is None:
            return False
        for addr, payload in writes.items():
            try:
                if not bool(wbytes(addr, payload)):
                    return False
            except Exception:
                return False
        return True

    def _selector_writes_for_row(self, row: dict, raw_val: int | None) -> dict[int, bytes]:
        graft_addr = int(row["block"])
        lane_addr = int(row.get("addr", graft_addr))
        typ = str(row.get("typ", ""))

        # Default is an explicit profile. This is what lets duplicate characters
        # switch back from the other duplicate's custom route.
        if raw_val is None:
            if typ == "u32-chun-selector":
                # Chun default should be the real/original assist block, not the
                # last forced all-lanes graft. Custom profiles reinstall graft.
                return {graft_addr: CHUN_SELECTOR_ORIGINAL_BLOCK}
            if typ == "u32-ryu-selector-graft":
                return {graft_addr: RYU_SELECTOR_GRAFT_BLOCK}
            if typ == "u32-anchored-assist-fallback":
                owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
                table_index = int(row.get("anchor_table_index") or ANCHOR_ASSIST_FALLBACK_TABLE_INDEX)
                default_word = int(row.get("anchor_default_word") or 0) or None
                operand_offsets = list(row.get("anchor_operand_offsets") or [])
                operand_words = list(row.get("anchor_operand_words") or [])
                writes = _anchored_assist_fallback_writes(
                    owner_base, None, table_index, default_word, operand_offsets, operand_words
                )
                writes.update(_assist_state_sync_writes(row, None))
                return writes
            if typ == "u32-generic-selector":
                raw_hex = str(row.get("table_raw", "")).replace(" ", "")
                try:
                    block = bytes.fromhex(raw_hex)
                except Exception:
                    block = b""
                if len(block) == GENERIC_SELECTOR_TABLE_LEN:
                    writes = {graft_addr: block}
                    owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
                    if int(row.get("char_id") or 0) == VJOE_CHAR_ID or _is_vjoe_owner_base(owner_base):
                        writes.update(_vjoe_animation_state_writes(owner_base, None, dict(row.get("vjoe_info") or {})))
                    else:
                        writes.update(_assist_state_sync_writes(row, None))
                    return writes
                return {}
            if typ == "u32-vjoe-trampoline":
                owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
                return _vjoe_trampoline_writes(owner_base, None, dict(row.get("vjoe_info") or {}))
            if typ == "u32-direct-426-fallback":
                owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
                writes = _direct_426_writes(owner_base, None, dict(row.get("direct_426_info") or {}))
                writes.update(_assist_state_sync_writes(row, None))
                return writes
            return {}

        payload = struct.pack(">I", int(raw_val) & 0xFFFFFFFF)

        if typ == "u32-chun-selector":
            writes: dict[int, bytes] = {graft_addr: CHUN_SELECTOR_GRAFT_BLOCK}
            for off in CHUN_SELECTOR_WORD_OFFSETS:
                writes[graft_addr + off] = payload
            return writes

        if typ == "u32-ryu-selector-graft":
            writes = {graft_addr: RYU_SELECTOR_GRAFT_BLOCK}
            for off in RYU_SELECTOR_WORD_OFFSETS:
                writes[graft_addr + off] = payload
            return writes

        if typ == "u32-anchored-assist-fallback":
            owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
            table_index = int(row.get("anchor_table_index") or ANCHOR_ASSIST_FALLBACK_TABLE_INDEX)
            default_word = int(row.get("anchor_default_word") or 0) or None
            operand_offsets = list(row.get("anchor_operand_offsets") or [])
            operand_words = list(row.get("anchor_operand_words") or [])
            writes = _anchored_assist_fallback_writes(
                owner_base, int(raw_val), table_index, default_word, operand_offsets, operand_words
            )
            writes.update(_assist_state_sync_writes(row, int(raw_val)))
            return writes

        if typ == "u32-generic-selector":
            source = str(row.get("source", "unknown"))
            block = self._generic_block_args_and_words(graft_addr, source, int(raw_val), True, lane_addr)
            writes = {graft_addr: block}
            owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
            if int(row.get("char_id") or 0) == VJOE_CHAR_ID or _is_vjoe_owner_base(owner_base):
                writes.update(_vjoe_animation_state_writes(owner_base, int(raw_val), dict(row.get("vjoe_info") or {})))
            else:
                writes.update(_assist_state_sync_writes(row, int(raw_val)))
            return writes

        if typ == "u32-vjoe-trampoline":
            owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
            return _vjoe_trampoline_writes(owner_base, int(raw_val), dict(row.get("vjoe_info") or {}))

        if typ == "u32-direct-426-fallback":
            owner_base = int(row.get("owner_base") or (_owning_chr_tbl(graft_addr) or 0))
            writes = _direct_426_writes(owner_base, int(raw_val), dict(row.get("direct_426_info") or {}))
            writes.update(_assist_state_sync_writes(row, int(raw_val)))
            return writes

        return {}


    def _update_flat_row_display(self, row: dict, raw_val: int) -> None:
        owner_base = int(row.get("owner_base") or 0) or _owning_chr_tbl(int(row.get("addr", row.get("block", 0))))
        slot_char_ids = _read_slot_char_ids()
        raw_val = int(raw_val) & 0xFFFFFFFF
        row["raw"] = f"0x{raw_val:08X}"
        row["target"] = self._selector_target_for_display(int(row.get("addr", 0)), raw_val)
        row["entry"] = _flat_selector_entry_label(owner_base, [raw_val], slot_char_ids, "assist")
        row["guess"] = f"double-click to change this fighter profile; table 0x{int(row.get('block', 0)):08X}"

    def _refresh_display_for_block(self, graft_addr: int, raw_val: int) -> None:
        for iid, row in self._hit_by_iid.items():
            if int(row.get("block", -1)) != int(graft_addr):
                continue
            if row.get("typ") not in ("u32-chun-selector", "u32-ryu-selector-graft", "u32-generic-selector", "u32-anchored-assist-fallback", "u32-vjoe-trampoline", "u32-direct-426-fallback"):
                continue
            self._update_flat_row_display(row, raw_val)
            self._tree.set(iid, "entry", row["entry"])
            self._tree.set(iid, "raw", row["raw"])
            self._tree.set(iid, "target", row["target"])
            self._tree.set(iid, "guess", row["guess"])

    def _read_fighter_move_values_at(self, offset: int, width: int) -> dict[int, int]:
        vals: dict[int, int] = {}
        if rbytes is None:
            return vals
        for fighter_base in _FIGHTER_BASES:
            try:
                b = rbytes(fighter_base + offset, width)
                if not b or len(b) != width:
                    continue
                vals[fighter_base] = int.from_bytes(b, "big", signed=False)
            except Exception:
                continue
        return vals

    def _discover_runtime_move_offset(self) -> tuple[int, int] | None:
        """Best-effort discovery for the fighter current-action field.

        This intentionally learns from live assist-ish values. If no assist is
        happening yet, it may return None and the next tick will retry.
        """
        if rbytes is None:
            return None
        blobs: dict[int, bytes] = {}
        for fighter_base in _FIGHTER_BASES:
            try:
                b = rbytes(fighter_base, FIGHTER_MOVE_SCAN_SIZE)
                if b and len(b) == FIGHTER_MOVE_SCAN_SIZE:
                    blobs[fighter_base] = b
            except Exception:
                pass
        if len(blobs) < 2:
            return None

        best: tuple[int, int, int] | None = None  # score, offset, width
        for width in (2, 4):
            step = 2 if width == 2 else 4
            limit = FIGHTER_MOVE_SCAN_SIZE - width
            for off in range(0, limit, step):
                vals = []
                ok = True
                for fighter_base, blob in blobs.items():
                    v = int.from_bytes(blob[off:off + width], "big", signed=False)
                    if v > 0x700 and v != 0xFFFFFFFF:
                        ok = False
                        break
                    vals.append(v)
                if not ok:
                    continue
                score = 0
                for v in vals:
                    score += ASSIST_RUNTIME_STATE_PRIORITY.get(v, 0)
                    if v == 1:
                        score += 1
                if score <= 0:
                    continue
                # Prefer real assist attack, then standby/taunt, then lower offsets.
                if any(v in ASSIST_RUNTIME_ATTACK_STATES for v in vals):
                    score += 500
                if any(v in ASSIST_RUNTIME_PREFETCH_STATES for v in vals):
                    score += 100
                score -= off // 0x100
                cand = (score, off, width)
                if best is None or cand[0] > best[0]:
                    best = cand
        if best is None:
            return None
        _score, off, width = best
        return (off, width)

    def _active_profile_from_runtime_state(self) -> tuple[int, dict[str, object], int] | None:
        if not self._slot_assist_profiles:
            return None

        if self._runtime_move_offset is None:
            self._runtime_move_offset = self._discover_runtime_move_offset()
        if self._runtime_move_offset is None:
            return None

        off, width = self._runtime_move_offset
        vals = self._read_fighter_move_values_at(off, width)
        candidates: list[tuple[int, int, dict[str, object], int]] = []
        for fighter_base, profile in self._slot_assist_profiles.items():
            state = vals.get(int(fighter_base))
            if state is None:
                continue
            pri = ASSIST_RUNTIME_STATE_PRIORITY.get(int(state), 0)
            if pri <= 0:
                continue
            candidates.append((pri, int(fighter_base), profile, int(state)))

        if not candidates:
            # Offset may have been guessed during a transient false positive. Retry discovery.
            self._runtime_move_offset = None
            return None

        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        pri, fighter_base, profile, state = candidates[0]

        # Do not switch on ambiguous standby between duplicate same-character profiles.
        if state not in ASSIST_RUNTIME_ATTACK_STATES:
            same_pri = [c for c in candidates if c[0] == pri and int(c[2].get("char_id", 0)) == int(profile.get("char_id", 0))]
            if len(same_pri) > 1:
                return None

        return fighter_base, profile, state

    def _apply_profile_silent(self, profile: dict[str, object]) -> bool:
        raw_obj = profile.get("word", None)
        raw_val = None if raw_obj is None else (int(raw_obj) & 0xFFFFFFFF)
        row = dict(profile.get("row", {}) or {})
        if not row:
            return False
        writes = self._selector_writes_for_row(row, raw_val)
        volnutt_weapon = profile.get("volnutt_weapon")
        if volnutt_weapon is not None:
            owner_base_for_weapon = int(profile.get("owner_base") or row.get("owner_base") or 0)
            writes.update(_volnutt_weapon_writes(owner_base_for_weapon, int(volnutt_weapon), row))
        return bool(writes and self._write_many_silent(writes, urgent=True))

    def _snap_assist_state_id(self, snap: dict) -> int | None:
        """Return the current HUD-resolved move/state id for one fighter."""
        if not snap:
            return None
        mv = snap.get("mv_id_display")
        if mv is None:
            mv = snap.get("attA") or snap.get("attB")
        try:
            return int(mv)
        except Exception:
            return None

    def _snap_is_assist_attack(self, snap: dict) -> bool:
        """Return True when main.py says this fighter is being called as assist.

        Main already resolves attA/attB into mv_id_display and mv_label. Treat
        the whole assist-attack family as a trigger, not just literal 426.
        """
        if not snap:
            return False
        mv = self._snap_assist_state_id(snap)
        if mv in ASSIST_RUNTIME_ATTACK_STATES:
            return True
        label = str(snap.get("mv_label") or "").strip().lower()
        return label == "assist attack" or "assist attack" in label

    def _set_runtime_status_from_main(self, text: str) -> None:
        try:
            if self.root and self.root.winfo_exists():
                self.root.after(0, lambda msg=text: self._status.set(msg))
        except Exception:
            pass

    def _runtime_patch_from_main(self, snaps: dict) -> None:
        if not self._auto_reapply_enabled:
            return
        if not self._slot_assist_profiles:
            return
        if not isinstance(snaps, dict):
            return

        ordered_keys = ["P1-C1", "P1-C2", "P2-C1", "P2-C2"]
        ordered_keys.extend(k for k in snaps.keys() if k not in ordered_keys)

        for slot_label in ordered_keys:
            snap = snaps.get(slot_label)
            if not snap:
                continue
            try:
                fighter_base = int(snap.get("base") or 0)
            except Exception:
                fighter_base = 0
            if not fighter_base:
                continue

            state = self._snap_assist_state_id(snap)

            profile = self._slot_assist_profiles.get(fighter_base)
            if profile is None:
                # Non-duplicate path: profile keyed on owner_base (CHR_TBL slot index).
                fi = next((i for i, b in enumerate(_FIGHTER_BASES) if b == fighter_base), None)
                if fi is not None and fi < len(_CHR_TBL_BASES):
                    profile = self._slot_assist_profiles.get(_CHR_TBL_BASES[fi])

            is_attack = self._snap_is_assist_attack(snap)
            was_attack = bool(self._main_last_assist_attack_by_base.get(fighter_base, False))
            self._main_last_assist_attack_by_base[fighter_base] = bool(is_attack)

            if not profile:
                if is_attack and not was_attack:
                    msg_key = (fighter_base, int(state or -1))
                    if self._main_last_patch_key != msg_key:
                        self._main_last_patch_key = msg_key
                        self._set_runtime_status_from_main(
                            f"Auto Assist Trigger ON - {slot_label} assist attack edge, no profile for 0x{fighter_base:08X}."
                        )
                continue

            row = dict(profile.get("row", {}) or {})
            typ = str(row.get("typ", ""))
            char_id = int(profile.get("char_id") or row.get("char_id") or 0)
            is_vjoe_static = (char_id == VJOE_CHAR_ID or typ == "u32-vjoe-trampoline")

            if is_vjoe_static and bool(getattr(self, "_vjoe_static_direct_enabled", True)):
                # VJoe static-direct mode is intentionally not runtime-driven.
                # The selected chr_tbl[426] value is written immediately when chosen
                # and left at rest. The 430 standby/prearm path tested worse.
                continue

            # Normal selector/graft characters can still patch on the 426
            # assist-attack edge because their selector is read inside 426.
            # If VJoe Static Direct is OFF, VJoe also falls through to this older
            # attack-edge behavior as a comparison/debug path.
            if not is_attack or was_attack:
                continue
            event_text = "assist attack edge"

            raw_obj = profile.get("word", None)
            raw_val = None if raw_obj is None else (int(raw_obj) & 0xFFFFFFFF)

            if self._apply_profile_silent(profile):
                patch_key = (fighter_base, -1 if raw_val is None else raw_val, int(state or -1), 1 if is_vjoe_static else 0)
                if self._main_last_patch_key != patch_key:
                    self._main_last_patch_key = patch_key
                    fighter_label = row.get("fighter_label") or slot_label
                    prof_label = str(profile.get("label", "assist"))
                    word_txt = "default" if raw_val is None else f"0x{raw_val:08X}"
                    self._set_runtime_status_from_main(
                        f"Auto Assist Trigger ON - {fighter_label} {event_text}; wrote {prof_label} {word_txt}."
                    )
            else:
                self._set_runtime_status_from_main(
                    f"Auto Assist Trigger ON - {slot_label} {event_text}, write failed."
                )

    def _toggle_vjoe_static_direct(self) -> None:
        self._vjoe_static_direct_enabled = not bool(getattr(self, "_vjoe_static_direct_enabled", True))
        if self._vjoe_static_btn is not None:
            self._vjoe_static_btn.config(
                text="VJoe Static Direct: ON" if self._vjoe_static_direct_enabled else "VJoe Static Direct: OFF"
            )
        if self._vjoe_static_direct_enabled:
            self._status.set("VJoe Static Direct ON - selected VJoe chr_tbl[426] is written immediately and not prearmed on 430.")
        else:
            self._status.set("VJoe Static Direct OFF - VJoe falls back to normal attack-edge auto patching for comparison.")

    def _toggle_auto_reapply(self) -> None:
        self._auto_reapply_enabled = not bool(self._auto_reapply_enabled)
        if self._auto_btn is not None:
            self._auto_btn.config(text="Auto Assist Trigger: ON" if self._auto_reapply_enabled else "Auto Assist Trigger: OFF")
        if self._auto_reapply_enabled:
            self._runtime_move_offset = None
            self._runtime_last_profile_key = None
            self._main_last_patch_key = None
            self._main_last_assist_attack_by_base.clear()
            self._main_patch_latch_by_base.clear()
            self._status.set("Auto Assist Trigger ON - main.py only; patches VJoe on standby 430 and others on assist attack.")
        else:
            self._status.set("Auto Assist Trigger OFF")

    def _auto_reapply_tick(self) -> None:
        """Deprecated old background guesser.

        Kept as a no-op so older button/timer paths cannot overwrite the shared
        table with the last selected profile. Runtime switching now comes only
        from tick_assist_profiles_from_main(snaps).
        """
        return

    def _on_double_click(self, event):
        iid = self._tree.identify_row(event.y)
        col_idx = self._col_index(event)
        if not iid or col_idx < 0:
            return
        col_id = COL_IDS[col_idx]
        h = self._hit_by_iid.get(iid)
        if not h or not h.get("editable"):
            return
        addr = int(h["addr"])
        typ = str(h.get("typ", ""))

        if typ == "u32-chun-selector":
            self._apply_chun_selector_word(h)
            return

        if typ == "u32-ryu-selector-graft":
            self._apply_ryu_selector_word(h)
            return

        if typ == "u32-vjoe-trampoline":
            self._apply_vjoe_trampoline_word(h)
            return

        if typ == "u32-direct-426-fallback":
            self._apply_direct_426_word(h)
            return

        if typ in ("u32-generic-selector", "u32-anchored-assist-fallback"):
            self._apply_generic_selector_word(h)
            return

        if typ == "assist-move-preset":
            self._apply_assist_move_preset(h)
            return

        if wbytes is None:
            messagebox.showerror("Write failed", "dolphin_io.wbytes unavailable.", parent=self.root)
            return

        if typ == "u32-selector":
            val = self._choose_ryu_preset_or_manual(addr, str(h["raw"]))
            if val is None:
                return
            payload = struct.pack(">I", val)
        else:
            payload = self._manual_value(h)
            if payload is None:
                return
            val = None

        try:
            ok = bool(wbytes(addr, payload))
        except Exception as e:
            messagebox.showerror("Write failed", str(e), parent=self.root)
            return
        if not ok:
            messagebox.showerror("Write failed", "Could not write to Dolphin.", parent=self.root)
            return

        if typ == "u32-selector":
            raw_val = struct.unpack(">I", payload)[0]
            owner_base = _owning_chr_tbl(addr)
            as_local = owner_base + raw_val if owner_base is not None and raw_val <= 0x100000 else 0
            guess = RYU_KNOWN_TARGETS.get(raw_val, "manual selector")
            self._tree.set(iid, "raw", f"0x{raw_val:08X}")
            self._tree.set(iid, "target", f"0x{as_local:08X}" if as_local else "")
            self._tree.set(iid, "guess", guess)
            h["raw"] = f"0x{raw_val:08X}"
            h["target"] = f"0x{as_local:08X}" if as_local else ""
            h["guess"] = guess
            self._status.set(f"Wrote selector 0x{raw_val:08X} to 0x{addr:08X}")
        elif typ == "u16-state":
            state = struct.unpack(">H", payload)[0]
            self._tree.set(iid, "raw", f"0x{state:04X}")
            h["raw"] = f"0x{state:04X}"
            self._status.set(f"Wrote StateID 0x{state:04X} to 0x{addr:08X}")
        elif typ == "f32":
            fval = struct.unpack(">f", payload)[0]
            hx = payload.hex(" ").upper()
            self._tree.set(iid, "raw", _fmt_f32(fval))
            self._tree.set(iid, "target", hx)
            h["raw"] = _fmt_f32(fval)
            h["target"] = hx
            self._status.set(f"Wrote f32 {_fmt_f32(fval)} ({hx}) to 0x{addr:08X}")
        else:
            hx = payload.hex(" ").upper()
            self._tree.set(iid, "raw", hx)
            h["raw"] = hx
            self._status.set(f"Wrote {len(payload)} byte(s) to 0x{addr:08X}")

    def _on_right_click(self, event):
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        h = self._hit_by_iid.get(iid)
        if not h:
            return
        addr = int(h["addr"])
        block = int(h["block"])
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=f"Copy address 0x{addr:08X}", command=lambda: self._copy(f"0x{addr:08X}"))
        menu.add_command(label=f"Copy block 0x{block:08X}", command=lambda: self._copy(f"0x{block:08X}"))
        menu.add_command(label=f"Go to address 0x{addr:08X}", command=lambda: self._show_address_info(addr))
        menu.add_command(label=f"Go to block 0x{block:08X}", command=lambda: self._show_address_info(block))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy(self, text: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._status.set(f"Copied {text}")

    def _show_address_info(self, addr: int):
        if rbytes is None:
            messagebox.showerror("Address", "dolphin_io.rbytes unavailable", parent=self.root)
            return
        line_size = 16
        line_base = addr & ~(line_size - 1)
        start = max(SCAN_START, line_base - 8 * line_size)
        size = 17 * line_size
        try:
            data = rbytes(start, size) or b""
        except Exception as e:
            messagebox.showerror("Address", f"Read failed: {e}", parent=self.root)
            return
        dlg = tk.Toplevel(self.root)
        dlg.title(f"Assist bytes @ 0x{addr:08X}")
        dlg.geometry("840x460")
        txt = tk.Text(dlg, wrap="none", font=("Consolas", 10), bg="#101214", fg="#E8E8E8")
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        current_line = (line_base - start) // line_size
        for i in range(17):
            off = i * line_size
            chunk = data[off:off + line_size]
            a = start + off
            hx = " ".join(f"{b:02X}" for b in chunk)
            asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            prefix = ">>" if i == current_line else "  "
            txt.insert("end", f"{prefix} 0x{a:08X}: {hx:<47} {asc}\n")
        txt.config(state="disabled")


_inst = None


def tick_assist_profiles_from_main(snaps: dict) -> None:
    """Called once per HUD frame from main.py after snapshots are built."""
    update_assist_context_from_main(snaps)
    inst = _inst
    try:
        # HUD quick-assist profiles must keep working even when the Tk scanner is
        # open.  The headless path owns slot-specific mirror-match profiles;
        # the Tk path still handles profiles chosen inside the scanner window.
        _headless_runtime_patch_from_main(snaps)
        if inst is not None:
            inst._runtime_patch_from_main(snaps)
    except Exception as e:
        try:
            _assist_log_once("runtime-patch-failed", f"[assist scanner] runtime patch failed: {e!r}", interval=2.0)
        except Exception:
            pass


def open_assist_scanner_window():
    def _c(master):
        global _inst
        if _inst:
            try:
                _inst.root.lift()
                return
            except Exception:
                pass
        _inst = AssistScannerWindow(master)
    tk_call(_c)