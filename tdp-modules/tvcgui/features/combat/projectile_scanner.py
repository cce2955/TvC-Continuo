from __future__ import annotations
import json, os, sys, struct, threading, queue, time, tkinter as tk, re
from tkinter import ttk, simpledialog, messagebox
from tvcgui.core.tk_host import tk_call
from tvcgui.core.paths import data_path, user_data_path

try:
    from tvcgui.platform.dolphin import rbytes, wbytes
except Exception:
    rbytes = None
    wbytes = None
SUPER_STRUCT_SIG = b"\x00\x00\x0C\x00\x00\x00\x23\x00"
SUPER_VERIFY_A   = b"\x00\x00\x04\x00\x00\x00\xFF\xFF\xFF\xFF"
SUPER_VERIFY_B   = b"\x3F\x80\x00\x00"
SUPER_VERIFY_LOOK = 0x120
_SUPER_STRUCT_DMG_OFF = 0x09

# Shinkuu / super-card experimental offsets from local base
_SUPER_EX_OFFSETS = {
    "ex03c": 0x03C,
    "ex060": 0x060,
    "ex090": 0x090,
    "ex094": 0x094,
    "ex09c": 0x09C,
    "ex0d4": 0x0D4,
    "ex0e4": 0x0E4,
}

# Temporary named super fields discovered from the probe pass.
# f32/u16 decides how the edit dialog writes the value.
_SUPER_FIELD_OFFSETS = {
    "super_hit_react":    (0x00A, "u16"),  # CONFIRMED: hit reaction
    "super_life":         (0x00E, "u16"),  # likely timer/lifetime
    "super_air_kb_y":     (0x038, "f32"),  # validated: super scale behaves like air KB Y
    "super_speed":        (0x090, "f32"),  # validated: speed on several supers
    "super_accel":        (0x094, "f32"),  # validated: secondary accel / release behavior
    "super_speed_2":      (0x09C, "f32"),  # validated: additional speed value
    "super_accel_b":      (0x0B0, "f32"),  # Ryu has 32 here; secondary motion/offset candidate
    "super_accel_c":      (0x0D4, "f32"),  # Ryu has 60 here; secondary motion/decel candidate
    # Legacy experimental fields from the first probe pass. Keep these for old rows,
    # but the display workbench uses the friendlier super_beam_* names below.
    "super_multihit_cap": (0x0D8, "u32"),  # old probe slot, not the Shinkuu hit-count field
    "super_radius":       (0x0E4, "f32"),  # CONFIRMED: super radius / hit radius

    # Shinkuu/Kikosho-style super beam card fields. Offsets are relative to the
    # real super-card base, e.g. Ryu Shinkuu at 0x908D0BD0.
    "super_hit_count":    (0x024, "u32"),  # 0x908D0BF4: number of hit emissions allowed
    "super_hit_interval": (0x028, "u32"),  # 0x908D0BF8: time spacing for each emitted hit
    "super_particle_fx":  (0x040, "u32"),  # 0x908D0C10: on-screen particle/effect id
    "super_beam_width":   (0x03C, "f32"),  # 0x908D0C0C: collision/beam width scale
    "super_hit_source":   (0x060, "u32"),  # 0x908D0C30: linked hit source/anchor, dangerous
    "super_spawn_bone":   (0x068, "u32"),  # 0x908D0C38: spawn bone/origin selector
    "super_lifetime":     (0x084, "u32"),  # 0x908D0C54: beam lifetime in frames/units
    "super_beam_visual":  (0x0E8, "u32"),  # 0x908D0CB8: render extent / visual shape

    # Final-hit card embedded after the main beam card. These are exposed on the
    # main super row for quick editing; they are not separate projectile rows.
    "super_final_damage":      (0x110, "u32"),
    "super_final_lifetime":    (0x114, "u32"),
    "super_final_particle_fx": (0x134, "u32"),
    "super_final_spawn_bone":  (0x154, "u32"),
}



# Compact 00/23 and 01/23 projectile-super cards.
# These are not Shinkuu/Kikosho beam cards; they are smaller hit/projectile
# records used by Volnutt Machine Gun Sweep, Casshan Brutal Ax, Tekkaman
# Voltekka chunks, and Morrigan Finishing Shower bullet tables.
PROJECTILE_SUPER_FMTS = {"projectile_super_card", "projectile_super_card_0123"}
_PROJECTILE_SUPER_FIELD_OFFSETS = {
    "ps_lifetime":      (0x008, "u16"),  # active/life window for this card
    "ps_hit_count":     (0x00C, "u16"),  # hit/emission count for this card
    "ps_mode":          (0x010, "u16"),  # card mode/style; still being farmed
    "ps_emit_count":    (0x018, "u16"),  # secondary emit/count field
    "ps_interval":      (0x01C, "u16"),  # interval/spacing when present
    "ps_offset_x":      (0x026, "f32"),  # spawn/velocity X-ish value
    "ps_offset_y":      (0x02A, "f32"),  # spawn/velocity Y-ish value
    "ps_scale":         (0x02E, "f32"),  # scale/radius-ish constant, often 1.0
    "ps_particle_fx":   (0x034, "u16"),  # particle/effect id
    "ps_projectile_id": (0x052, "u16"),  # projectile/object id/type slot
    "ps_spawn_bone":    (0x05C, "u16"),  # spawn bone/source selector when present
}

# ---------------------------------------------------------------------------
# Scan parameters
# ---------------------------------------------------------------------------
# The suffix the module search for in the template/template2 path.
# Full 12-byte signature:  00 00 XX YY  00 00 00 0C  <discriminator>
# Find _SUFFIX, then look 4 bytes back for the damage word.
_SUFFIX    = b"\x00\x00\x00\x0C"
SCAN_START = 0x90000000
SCAN_END   = 0x94000000
SCAN_BLOCK = 0x40000
PROJ_MAP_FILE = "projectilemap.json"
PROJ_IDS_FILE = "projectile_ids.json"

# ---------------------------------------------------------------------------
# Static projectile template layout
# ---------------------------------------------------------------------------
# Standard template records are 0xB0 bytes. Physics-family records extend to
# 0xD8 bytes, meaning +0xD4 is the final possible f32 in that larger record.
# +0xD8 and +0xDC are outside both records and must never be read as fields.
#
# These names reflect the current recomp and live-dump mapping. Fields still
# under research are labeled as raw rather than given gameplay names.
FIELD_OFFSETS = {
    "radius":          0x02C,  # f32: base collision scale
    "kb_x":            0x024,  # f32: knockback X
    "kb_y":            0x028,  # f32: knockback Y
    "c042":            0x042,  # u16: validation constant, normally 10
    "motion_family":   0x050,  # u16: motion family
    "type":            0x051,  # u8 low byte alias, 3=linear, 4=physics
    "id":              0x052,  # u16: projectile definition ID
    "lifetime":        0x05A,  # u16: active/lifetime value
    "fixed_scale":     0x06E,  # u16 fixed-point scale, 1024 = 1.0
    "hb_size":         0x06E,  # compatibility alias
    "speed":           0x080,  # f32: primary travel speed
    "speed_mult":      0x084,  # f32: speed/time multiplier
    "accel":           0x084,  # compatibility alias, not proven acceleration
    "percent_scale":   0x08C,  # f32: percentage-style scale, normally 100.0
    "hitbox":          0x08C,  # compatibility alias, not direct radius
    "arc":             0x090,  # f32: curve/gravity parameter A
    "arc2":            0x094,  # f32: curve/gravity parameter B
    "physics_tail_d4": 0x0D4,  # f32: physics-record tail, exact use unresolved
    "mode_a":          0x014,  # u32: mode/count A
    "mode_b":          0x018,  # u32: mode/count B or flags
    "linked_resource": 0x048,  # u32: linked resource/script ID
    "flags_72":        0x072,  # u32: flags/sentinel candidate
}

_STATIC_FIELD_TYPES = {
    "radius": "f32", "kb_x": "f32", "kb_y": "f32",
    "motion_family": "u16", "type": "u8", "id": "u16",
    "lifetime": "u16", "fixed_scale": "u16", "hb_size": "u16",
    "speed": "f32", "speed_mult": "f32", "accel": "f32",
    "percent_scale": "f32", "hitbox": "f32", "arc": "f32",
    "arc2": "f32", "physics_tail_d4": "f32", "mode_a": "u32",
    "mode_b": "u32", "linked_resource": "u32", "flags_72": "u32",
    "c042": "u16",
}

# Validation-gate field addresses relative to the record base.
_VALID_C042    = 0x042
_VALID_ACCEL   = 0x084
_VALID_HITBOX  = 0x08C
_VALID_HBSIZE  = 0x06E
_DISCRIMINATOR = 0x008

# ---------------------------------------------------------------------------
# Character / signature tables
# ---------------------------------------------------------------------------
_NAME_TO_KEY = {
    "Ryu": "RYU", "Chun-Li": "CHUN", "Jun the Swan": "JUN",
    "Ken the Eagle": "KEN", "Alex": "ALEX", "Batsu": "BATSU",
    "Frank West": "FRANK", "Volnutt": "VOLNUTT", "Morrigan": "MORRIGAN",
    "Roll": "ROLL", "Saki": "SAKI", "Viewtiful Joe": "VJOE",
    "Zero": "ZERO", "Casshan": "CASSHAN", "Doronjo": "DORONJO",
    "Ippatsuman": "IPPATSMAN", "Joe the Condor": "JOE",
    "Tekkaman": "TEKKAMAN", "Tekkaman Blade": "BLADE",
    "Yatterman-1": "YATTER1", "Yatterman-2": "YATTER2", "Karas": "KARAS",
    "Polimar": "POLIMAR", "Soki": "SOKI", "Yami": "YAMI",
    "Gold Lightan": "LIGHTAN", "PTX-40A": "PTX",
}

# Fallback names for canonical Shinkuu/Kikosho-style beam cards when the
# owning slot is known but the per-hit damage value does not match that
# character's projectile map.  This is needed for supers like Morrigan's
# Finishing Shower, where the visible super is a multi-hit beam/barrage card
# with a small per-hit damage value that can collide with unrelated moves.
_SUPER_BEAM_DEFAULT_MOVE_BY_KEY = {
    "RYU": "Shinkuu Hadouken",
    "CHUN": "Kikosho",
    "MORRIGAN": "Finishing Shower",
    "VOLNUTT": "Machine Gun Sweep",
    "TEKKAMAN": "Voltekka",
    "CASSHAN": "Super Destruction Beam",
}

CHAR_SIGS = {
    "KEN": [b"\x00\x00\x00\x09"],
    "RYU": [b"\x00\x04\x01\x02"],
    
}

CHAR_SIG_OFFSETS = {
    "KEN": "pre",
    "RYU": "c",
    
}

_SIG_C_TO_KEYS:   dict[bytes, list[str]] = {}
_SIG_PRE_TO_KEYS: dict[bytes, list[str]] = {}

for _k, _sigs in CHAR_SIGS.items():
    _target = _SIG_PRE_TO_KEYS if CHAR_SIG_OFFSETS.get(_k) == "pre" else _SIG_C_TO_KEYS
    for _s in _sigs:
        _target.setdefault(_s, []).append(_k)

# ---------------------------------------------------------------------------
# Script opcode table
# ---------------------------------------------------------------------------
SCRIPT_OPCODES: dict[bytes, dict] = {
    b"\x05\x2B": {
        "fmt_name":   "script(0x052B)",
        "dmg_offset": 4,
    },
}

def _dmg_write_offset(fmt: str) -> int:
    if str(fmt or "") in PROJECTILE_SUPER_FMTS:
        return 4
    for info in SCRIPT_OPCODES.values():
        if info["fmt_name"] == fmt:
            return info["dmg_offset"]
    return 2  # default for template / template2

# ---------------------------------------------------------------------------
# chr_tbl param table  -  authoritative damage addresses for script-mode hits
#
# Each slot has a 16-byte parameter-entry table at a fixed offset from its
# chr_tbl_base.  The damage is a u32 big-endian at entry+0x00 (NOT u16 at +2).
#
#   chr_tbl_base + 0x25E0  →  u32 damage for the 2400-class entry (Spree/Attack)
#   chr_tbl_base + 0x2640  →  u32 damage for the 3200-class entry (Fall)
#
# Per-slot exact addresses (precomputed for fast lookup):
#   slot 0  chr_tbl 0x90896640  →  spree 0x90898C20  / fall 0x90898C80
#   slot 1  chr_tbl 0x908F1920  →  spree 0x908F3F00  / fall 0x908F3F60
#   slot 2  chr_tbl 0x909478E0  →  spree 0x90949EC0  / fall 0x90949F20
#   slot 3  chr_tbl 0x9099D9C0  →  spree 0x9099FFA0  / fall 0x909A0000
#
# Ownership: a hit address belongs to a slot when
#   chr_tbl_base <= hit_addr < chr_tbl_base + _CHR_TBL_SLOT_SIZE
# ---------------------------------------------------------------------------
_SCRIPT_DMG_OFFSETS: dict[int, int] = {
    2400: 0x25E0,   # u32 at entry start (Spree/Attack)
    3200: 0x2640,   # u32 at entry start (Fall)
}

_CHR_TBL_BASES = [
    0x90896640,   # slot 0
    0x908F1920,   # slot 1  (owns Frank West / Zombie addresses)
    0x909478E0,   # slot 2
    0x9099D9C0,   # slot 3
]
_CHR_TBL_SLOT_SIZE = 0x80000

# Frank West's character ID in the game's roster.
FRANK_CHAR_ID = 30   # 0x1E

# Fighter base addresses for each slot  -  char_id is read from base + 0x14.
# These are the same bases established in the slot/scanner work.
_FIGHTER_BASES = [
    0x9246B9C0,   # slot 0
    0x927EB9E0,   # slot 1
    0x92B6BA00,   # slot 2
    0x92EEBA20,   # slot 3
]
_CHAR_ID_OFF = 0x14   # offset within fighter base where char_id (u32 BE) lives

# chr_tbl_base → slot index, for mapping ownership back to fighter bases
_CHR_TBL_TO_SLOT = {
    0x90896640: 0,
    0x908F1920: 1,
    0x909478E0: 2,
    0x9099D9C0: 3,
}

_DYNAMIC_CHR_TBL_CACHE: list[int] | None = None

def _discover_chr_tbl_bases() -> list[int]:
    """Discover live chr_tbl bases from MEM2 instead of trusting old slot ranges.

    The projectile scanner originally used hard-coded chr_tbl bases from one
    session. That made same-damage projectile records from other slots show up
    under the currently selected character.  The frame-data scanner already
    proves the table by the literal chr_tbl/chr_act labels, so mirror that here.
    """
    global _DYNAMIC_CHR_TBL_CACHE
    if _DYNAMIC_CHR_TBL_CACHE:
        return list(_DYNAMIC_CHR_TBL_CACHE)
    if rbytes is None:
        return list(_CHR_TBL_BASES)

    found: list[int] = []
    label = b"chr_tbl\n"
    addr = SCAN_START
    while addr < SCAN_END:
        size = min(SCAN_BLOCK, SCAN_END - addr)
        try:
            data = rbytes(addr, size) or b""
        except Exception:
            data = b""
        pos = 0
        while data:
            idx = data.find(label, pos)
            if idx < 0:
                break
            base = addr + idx + 0x18
            try:
                chk = rbytes(base - 0x18, 0x2000) or b""
            except Exception:
                chk = b""
            if b"chr_act\n" in chk and base not in found:
                found.append(base)
            pos = idx + 1
        addr += size

    found.sort()
    if len(found) >= 4:
        _DYNAMIC_CHR_TBL_CACHE = found[:4]
    else:
        _DYNAMIC_CHR_TBL_CACHE = list(_CHR_TBL_BASES)
    return list(_DYNAMIC_CHR_TBL_CACHE)

def _current_chr_tbl_bases() -> list[int]:
    try:
        bases = _discover_chr_tbl_bases()
        return bases if bases else list(_CHR_TBL_BASES)
    except Exception:
        return list(_CHR_TBL_BASES)

def _projectile_key_from_char_id(cid: int | None) -> str | None:
    if cid is None:
        return None
    name_or_key = CHAR_ID_TO_KEY.get(cid)
    if not name_or_key:
        return None
    return _NAME_TO_KEY.get(str(name_or_key), str(name_or_key))

def _active_keys_from_lookup(lookup: dict) -> set[str]:
    keys: set[str] = set()
    try:
        for matches in lookup.values():
            for key, _mv in matches:
                keys.add(str(key))
    except Exception:
        pass
    return keys


def _read_slot_char_ids() -> dict[int, int]:
    """
    Read the live char_id for each slot from its fighter base + 0x14.
    Returns a dict of {chr_tbl_base: char_id}.
    Returns an empty dict if rbytes is unavailable.
    """
    result: dict[int, int] = {}
    if rbytes is None:
        return result
    bases = _current_chr_tbl_bases()
    for slot_idx, fighter_base in enumerate(_FIGHTER_BASES):
        chr_tbl_base = bases[slot_idx] if slot_idx < len(bases) else (_CHR_TBL_BASES[slot_idx] if slot_idx < len(_CHR_TBL_BASES) else None)
        if chr_tbl_base is None:
            continue
        try:
            b = rbytes(fighter_base + _CHAR_ID_OFF, 4)
            if b and len(b) == 4:
                cid = struct.unpack(">I", b)[0]
                result[int(chr_tbl_base)] = cid
        except Exception:
            pass
    return result


def _resolve_script_dmg_addr(hit_addr: int, dmg: int) -> int | None:
    """
    For a script-cluster hit, return the authoritative u32 param-table address
    for this damage value within the owning slot's chr_tbl region.
    Returns None if no mapping exists or no slot owns hit_addr.
    """
    off = _SCRIPT_DMG_OFFSETS.get(dmg)
    if off is None:
        return None
    base = _owning_chr_tbl(hit_addr)
    if base is None:
        return None
    return base + off


def _write_u32(addr: int, val: int) -> bool:
    """Write a big-endian u32  -  used for the zombie param-table entries."""
    if wbytes is None:
        return False
    try:
        return bool(wbytes(addr, struct.pack(">I", val)))
    except Exception as e:
        print(f"[proj_scanner] write u32 failed: {e}")
        return False

# ---------------------------------------------------------------------------
# Template format classification
#
# Uses the u32 discriminator at base+0x08, exactly as the notes specify:
#   0xFFFFFFFF          → "template"
#   0x00000000 / 0x01   → "template2"
#   anything else       → "script(0xNN)"
# ---------------------------------------------------------------------------
def _classify_discriminator(after4: bytes) -> str:
    """after4 = 4 bytes starting at base+0x08 (the 4 bytes after _SUFFIX)."""
    if len(after4) < 4:
        return "script(?)"
    disc = struct.unpack_from(">I", after4)[0]
    if disc == 0xFFFFFFFF:
        return "template"
    if disc in (0x00000000, 0x00000001):
        return "template2"
    return f"script(0x{(after4[0]):02X})"


# ---------------------------------------------------------------------------
# Validation gate for template / template2 records
#
# All five conditions from the notes must hold before any field is trusted.
# Returns True only if the record passes every check.
# ---------------------------------------------------------------------------
def _validate_template(data: bytes, base_off: int, relaxed: bool = False) -> bool:
    end = len(data)

    def u16(off: int) -> int | None:
        o = base_off + off
        if o + 2 > end:
            return None
        return struct.unpack_from(">H", data, o)[0]

    def u32(off: int) -> int | None:
        o = base_off + off
        if o + 4 > end:
            return None
        return struct.unpack_from(">I", data, o)[0]

    def f32(off: int) -> float | None:
        o = base_off + off
        if o + 4 > end:
            return None
        return struct.unpack_from(">f", data, o)[0]

    if not relaxed and u16(_VALID_C042) != 10:
        return False

    accel = f32(_VALID_ACCEL)
    if accel is None:
        return False
    if not relaxed:
        if not (abs(accel - 1.0) <= 1e-4 or abs(accel - 0.75) <= 1e-4):
            return False
    else:
        if not (-100.0 <= accel <= 100.0):
            return False

    hb = f32(_VALID_HITBOX)
    if hb is None:
        return False
    if not relaxed and abs(hb - 100.0) > 0.1:
        return False

    if not relaxed and u16(_VALID_HBSIZE) != 1024:
        return False

    disc = u32(_DISCRIMINATOR)
    if disc not in (0xFFFFFFFF, 0x00000000, 0x00000001):
        return False

    return True    
# ---------------------------------------------------------------------------
# Clustering helpers
#
# Groups validated template records by (proj_id, type_family) first, then by
# (damage, speed, kb_x, kb_y) to identify tiers/variants  -  exactly the
# workflow described in the notes.
# ---------------------------------------------------------------------------
def _cluster_key(h: dict) -> tuple:
    """Primary cluster key: (proj_id, type_family)."""
    try:
        pid = int(h.get("id", 0))
    except (ValueError, TypeError):
        pid = 0
    try:
        tf = int(h.get("type", 0))
    except (ValueError, TypeError):
        tf = 0
    return (pid, tf)


def _tier_key(h: dict) -> tuple:
    """Secondary cluster key within a family: (damage, speed_rounded, kb_x_rounded, kb_y_rounded)."""
    def _f(v, decimals=1):
        try:
            return round(float(v), decimals)
        except (ValueError, TypeError):
            return 0.0
    return (h.get("dmg", 0), _f(h.get("speed")), _f(h.get("kb_x")), _f(h.get("kb_y")))


def _annotate_clusters(hits: list[dict]) -> None:
    """
    Tag each template/template2 hit with a 'cluster' string of the form
    'ID:0xNNNN TF:N tier:M/T' so the UI can show grouping without a
    separate column explosion.

    Script/opcode hits are tagged 'script'  -  no cluster analysis.
    """
    from collections import defaultdict

    # Separate template hits from opcode hits
    tmpl_hits  = [h for h in hits if h.get("fmt") in ("template", "template2")]
    other_hits = [h for h in hits if h not in tmpl_hits]

    # Group by primary key
    families: dict[tuple, list[dict]] = defaultdict(list)
    for h in tmpl_hits:
        families[_cluster_key(h)].append(h)

    for ck, members in families.items():
        pid, tf = ck
        # Group by tier within the family
        tiers: dict[tuple, list[dict]] = defaultdict(list)
        for h in members:
            tiers[_tier_key(h)].append(h)
        total_tiers = len(tiers)
        # Stable level order. Charge/projectile levels overwhelmingly increase
        # damage first; speed and knockback break ties. Scan order is not a
        # semantic level and used to make tier labels change between runs.
        ordered_tiers = sorted(
            tiers.items(),
            key=lambda item: (
                int(item[0][0] or 0),
                abs(float(item[0][1] or 0.0)),
                abs(float(item[0][2] or 0.0)) + abs(float(item[0][3] or 0.0)),
            ),
        )
        for tier_num, (tk_, tier_members) in enumerate(ordered_tiers, start=1):
            label = f"ID:0x{pid:04X} TF:{tf} tier:{tier_num}/{total_tiers}"
            for h in tier_members:
                h["cluster"] = label
                h["tier"] = tier_num
                h["tier_total"] = total_tiers
                h["tier_signature"] = list(tk_)

    for h in other_hits:
        h["cluster"] = "script"


# ---------------------------------------------------------------------------
# Helper lookup builders
# ---------------------------------------------------------------------------
def _keys_for_block(c_word: bytes, pre_word: bytes) -> list[str]:
    keys = set()
    keys.update(_SIG_C_TO_KEYS.get(bytes(c_word), []))
    keys.update(_SIG_PRE_TO_KEYS.get(bytes(pre_word), []))
    return list(keys)


def _resource_path(name: str) -> str:
    """Find combat reference JSON in organized source and bundled data roots."""
    candidates = [
        data_path("combat", name),
        user_data_path("combat", name),
        os.path.join(os.getcwd(), "data", "combat", name),
        # Legacy fallbacks keep an older portable install readable.
        os.path.join(os.getcwd(), name),
        name,
    ]
    seen = set()
    for path in candidates:
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            continue
        seen.add(norm)
        if os.path.exists(path):
            return path
    return data_path("combat", name)

def _load_map():
    try:
        with open(_resource_path(PROJ_MAP_FILE), encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[proj_scanner] {e}")
        return {}

def _load_ids():
    try:
        with open(_resource_path(PROJ_IDS_FILE), encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[proj_scanner] {e}")
        return {}
def _build_char_damage_map(proj_map):
    out = {}
    for key, moves in proj_map.items():
        dmg_map = {}
        for entry in moves:
            dmg = int(entry.get("dmg", 0))
            if dmg:
                dmg_map.setdefault(dmg, []).append(entry.get("move", "?"))
        out[key] = dmg_map
    return out


# Fill this with the live roster IDs.
# Example: FRANK is already known to be 30.
# Live roster char_id -> projectile-map key
CHAR_ID_TO_KEY = {
   1:  "Ken the Eagle",
    2:  "Casshan",
    3:  "Tekkaman",
    4:  "Polimar",
    5:  "Yatterman-1",
    6:  "Doronjo",
    7:  "Ippatsuman",
    8:  "Jun the Swan",
    10: "Karas",
    11: "Gold Lightan",
    12: "Ryu",
    13: "Chun-Li",
    14: "Batsu",
    15: "Morrigan",
    16: "Alex",
    17: "Viewtiful Joe",
    18: "Volnutt",
    19: "Roll",
    20: "Saki",
    21: "Soki",
    22: "PTX-40A",
    23: "Yami",
    26: "Tekkaman Blade",
    27: "Joe the Condor",
    28: "Yatterman-2",
    29: "Zero",
    30: "Frank West",
}
def _build_lookup(proj_map, active_keys):
    lookup = {}
    for key, moves in proj_map.items():
        if key not in active_keys:
            continue
        for entry in moves:
            dmg = int(entry.get("dmg", 0))
            if dmg:
                lookup.setdefault(dmg, []).append((key, entry.get("move", "?")))
    return lookup




# ---------------------------------------------------------------------------
# Memory helpers (live Dolphin reads)
# ---------------------------------------------------------------------------
def _read_u8(addr: int) -> str:
    if rbytes is None: return "?"
    try:
        b = rbytes(addr, 1)
        return str(b[0]) if b and len(b) == 1 else "?"
    except Exception:
        return "?"

def _read_u16(addr: int) -> str:
    if rbytes is None: return "?"
    try:
        b = rbytes(addr, 2)
        return str((b[0] << 8) | b[1]) if b and len(b) == 2 else "?"
    except Exception:
        return "?"

def _read_u32(addr: int) -> str:
    if rbytes is None: return "?"
    try:
        b = rbytes(addr, 4)
        return str(struct.unpack(">I", b)[0]) if b and len(b) == 4 else "?"
    except Exception:
        return "?"

def _read_u32_int(addr: int):
    """Internal u32 reader for scanner validation.

    The UI reader above returns text.  Validation needs an int; using the text
    reader made the canonical super-beam pass reject valid cards and fall back
    to shifted shadow rows.
    """
    if rbytes is None:
        return None
    try:
        b = rbytes(addr, 4)
        return struct.unpack(">I", b)[0] if b and len(b) == 4 else None
    except Exception:
        return None

def _read_u16_hex(addr: int) -> str:
    if rbytes is None: return "?"
    try:
        b = rbytes(addr, 2)
        if b and len(b) == 2:
            return f"0x{(b[0] << 8) | b[1]:04X}"
    except Exception:
        pass
    return "?"

def _read_f32(addr: int) -> str:
    if rbytes is None: return "?"
    try:
        b = rbytes(addr, 4)
        if b and len(b) == 4:
            return f"{struct.unpack('>f', b)[0]:.4f}"
    except Exception:
        pass
    return "?"

def _write_u16(addr: int, val: int) -> bool:
    if wbytes is None: return False
    try:
        return bool(wbytes(addr, bytes([(val >> 8) & 0xFF, val & 0xFF])))
    except Exception as e:
        print(f"[proj_scanner] write u16 failed: {e}")
        return False

def _write_u8(addr: int, val: int) -> bool:
    if wbytes is None: return False
    try:
        return bool(wbytes(addr, bytes([int(val) & 0xFF])))
    except Exception as e:
        print(f"[proj_scanner] write u8 failed: {e}")
        return False

def _write_f32(addr: int, val: float) -> bool:
    if wbytes is None: return False
    try:
        return bool(wbytes(addr, struct.pack(">f", val)))
    except Exception as e:
        print(f"[proj_scanner] write f32 failed: {e}")
        return False

def _write_dmg(addr: int, new_dmg: int, fmt: str) -> bool:
    return _write_u16(addr + _dmg_write_offset(fmt), new_dmg)


# ---------------------------------------------------------------------------
# Live projectile actor and linked collision inspector
# ---------------------------------------------------------------------------
# These offsets come from the live actor and recomp-backed linked-collision
# mapping. Reads are grouped so the inspector can update without scanning all
# of MEM2 again.
_LIVE_ACTOR_TABLE = 0x80476E50
# Read beyond the original 16 entries. Validation below rejects unrelated
# pointers, while the wider table catches actors registered by teammates and P2.
_LIVE_ACTOR_TABLE_COUNT = 64
_LIVE_ACTOR_POOL_BASES = (0x91B159B4,)
_LIVE_ACTOR_STRIDE = 0x1A4
_LIVE_ACTOR_COUNT = 48

# Stable MEM1 globals that point at the four live fighter structs. Do not use
# one-session MEM2 fighter addresses for ownership. Those were the reason the
# monitor only worked reliably for P1-C1.
_LIVE_SLOT_PTR_ADDRS = {
    "P1-C1": 0x803C9FCC,
    "P1-C2": 0x803C9FDC,
    "P2-C1": 0x803C9FD4,
    "P2-C2": 0x803C9FE4,
}
_LIVE_SLOT_FALLBACK_BASES = {
    "P1-C1": 0x9246B9C0,
    "P1-C2": 0x927EB9E0,
    "P2-C1": 0x92B6BA00,
    "P2-C2": 0x92EEBA20,
}
_LIVE_FIGHTER_ACTION_OFF = 0x01E8

_LIVE_ACTOR_OFF = {
    "x": 0x05C, "y": 0x06C, "z": 0x07C,
    "prev_x": 0x0BC, "prev_y": 0x0CC, "prev_z": 0x0DC,
    "emitter_x": 0x0E0, "emitter_y": 0x0E4, "emitter_z": 0x0E8,
    "scale_candidate": 0x0F8,
    "dir_z": 0x104, "dir_x": 0x108, "dir_y": 0x10C,
    "fixed_scale_raw": 0x110,
    "impact_x": 0x118, "impact_y": 0x11C, "impact_z": 0x120,
    "owner": 0x130, "projectile_id": 0x134,
    "owner_mirror": 0x138, "linked": 0x13C,
}

_LIVE_LINKED_OFF = {
    "actor_type": 0x028,
    "owner": 0x030,
    "target": 0x034,
    "clash_priority": 0x04C,
    "quota_max": 0x054,
    "quota_used": 0x058,
    "mutual_clash_lockout": 0x078,
    "hit_flags_a": 0x080,
    "hit_flags_b": 0x084,
    "damage": 0x08C,
    "damage_scale": 0x090,
    "hitstun_override": 0x094,
    "blockstun_override": 0x098,
    "secondary_reaction_raw": 0x09C,
    "giant_stagger": 0x0A0,
    "hitstop_override": 0x0A4,
    "hit_dir_x": 0x0A8,
    "hit_dir_y": 0x0AC,
    "hit_dir_z": 0x0B0,
    "paired_state_a": 0x0B4,
    "paired_state_b": 0x0B8,
    "effect_1_id": 0x0BC,
    "effect_2_id": 0x0C0,
    "effect_3_id_raw": 0x0C4,
    "world_effect_id": 0x0C8,
    "effect_1_arg": 0x0CC,
    "effect_2_arg": 0x0D0,
    "effect_3_arg_raw": 0x0D4,
    "world_effect_arg": 0x0D8,
    "shape_type": 0x308,
    "shape_ptr": 0x30C,
    "shape_count": 0x314,
    "contact_mode_a_raw": 0x318,
    "contact_mode_b_raw": 0x31C,
    "contact_offset_y": 0x320,
    "contact_offset_x": 0x324,
    "contact_world_x": 0x660,
    "contact_world_y": 0x670,
    "contact_world_z": 0x680,
}

_LIVE_SHAPE_LABELS = {1: "Sphere", 2: "Line", 3: "Box"}


def _buf_u8(data: bytes, off: int, default=0):
    return data[off] if data and 0 <= off < len(data) else default


def _buf_u16(data: bytes, off: int, default=0):
    if not data or off < 0 or off + 2 > len(data):
        return default
    return struct.unpack_from(">H", data, off)[0]


def _buf_u32(data: bytes, off: int, default=0):
    if not data or off < 0 or off + 4 > len(data):
        return default
    return struct.unpack_from(">I", data, off)[0]


def _buf_s32(data: bytes, off: int, default=0):
    if not data or off < 0 or off + 4 > len(data):
        return default
    return struct.unpack_from(">i", data, off)[0]


def _buf_f32(data: bytes, off: int, default=0.0):
    if not data or off < 0 or off + 4 > len(data):
        return default
    try:
        value = struct.unpack_from(">f", data, off)[0]
    except Exception:
        return default
    return value if value == value and abs(value) != float("inf") else default


def _fmt_hex(value: int) -> str:
    return f"0x{int(value or 0) & 0xFFFFFFFF:08X}"


def _fmt_float(value: float) -> str:
    try:
        return f"{float(value):.4f}"
    except Exception:
        return "?"


def _fmt_vec(*values: float) -> str:
    return "(" + ", ".join(_fmt_float(v) for v in values) + ")"


def _sane_live_point(x: float, y: float, z: float) -> bool:
    return all(v == v and abs(v) < 30.0 for v in (x, y, z)) and (abs(x) + abs(y) + abs(z) > 0.001)


def _read_live_block(addr: int, size: int) -> bytes:
    if rbytes is None:
        return b""
    try:
        return rbytes(addr, size) or b""
    except Exception:
        return b""


def _read_live_u32(addr: int, default: int = 0) -> int:
    data = _read_live_block(addr, 4)
    if len(data) != 4:
        return int(default)
    try:
        return struct.unpack_from(">I", data, 0)[0]
    except Exception:
        return int(default)


def _current_live_slot_bases() -> dict[str, int]:
    """Resolve all four fighter bases from their stable MEM1 pointer globals."""
    out: dict[str, int] = {}
    for slot_label, ptr_addr in _LIVE_SLOT_PTR_ADDRS.items():
        base = _read_live_u32(ptr_addr)
        if not (0x90000000 <= base < 0x94000000):
            base = int(_LIVE_SLOT_FALLBACK_BASES.get(slot_label, 0))
        if 0x90000000 <= base < 0x94000000:
            out[slot_label] = base
    return out


def _live_detail(group: str, label: str, offset: str, value, confidence: str,
                 address: int | None = None) -> dict:
    return {
        "group": group,
        "label": label,
        "offset": offset,
        "value": str(value),
        "confidence": confidence,
        "address": address,
    }


def _decode_live_actor(actor: int, actor_data: bytes, table_seen: bool,
                       owner_names: dict[int, str]) -> dict | None:
    owner = _buf_u32(actor_data, _LIVE_ACTOR_OFF["owner"])
    owner_slot = owner_names.get(owner)
    if owner_slot is None:
        return None

    projectile_id = _buf_u32(actor_data, _LIVE_ACTOR_OFF["projectile_id"])
    if not (1 <= projectile_id <= 0xFFFF):
        return None

    x = _buf_f32(actor_data, _LIVE_ACTOR_OFF["x"])
    y = _buf_f32(actor_data, _LIVE_ACTOR_OFF["y"])
    z = _buf_f32(actor_data, _LIVE_ACTOR_OFF["z"])
    if not _sane_live_point(x, y, z):
        return None

    linked = _buf_u32(actor_data, _LIVE_ACTOR_OFF["linked"])
    if not table_seen and not (0x90000000 <= linked < 0x94000000):
        return None

    prev_x = _buf_f32(actor_data, _LIVE_ACTOR_OFF["prev_x"], x)
    prev_y = _buf_f32(actor_data, _LIVE_ACTOR_OFF["prev_y"], y)
    prev_z = _buf_f32(actor_data, _LIVE_ACTOR_OFF["prev_z"], z)
    if not _sane_live_point(prev_x, prev_y, prev_z):
        prev_x, prev_y, prev_z = x, y, z
    vel_x, vel_y, vel_z = x - prev_x, y - prev_y, z - prev_z

    emitter = (
        _buf_f32(actor_data, _LIVE_ACTOR_OFF["emitter_x"]),
        _buf_f32(actor_data, _LIVE_ACTOR_OFF["emitter_y"]),
        _buf_f32(actor_data, _LIVE_ACTOR_OFF["emitter_z"]),
    )
    direction = (
        _buf_f32(actor_data, _LIVE_ACTOR_OFF["dir_x"]),
        _buf_f32(actor_data, _LIVE_ACTOR_OFF["dir_y"]),
        _buf_f32(actor_data, _LIVE_ACTOR_OFF["dir_z"]),
    )
    impact = (
        _buf_f32(actor_data, _LIVE_ACTOR_OFF["impact_x"]),
        _buf_f32(actor_data, _LIVE_ACTOR_OFF["impact_y"]),
        _buf_f32(actor_data, _LIVE_ACTOR_OFF["impact_z"]),
    )
    owner_mirror = _buf_u32(actor_data, _LIVE_ACTOR_OFF["owner_mirror"])
    owner_action_id = _read_live_u32(owner + _LIVE_FIGHTER_ACTION_OFF)
    scale_candidate = _buf_f32(actor_data, _LIVE_ACTOR_OFF["scale_candidate"])
    fixed_scale_raw = _buf_u32(actor_data, _LIVE_ACTOR_OFF["fixed_scale_raw"])

    linked_data = b""
    if 0x90000000 <= linked < 0x94000000:
        linked_data = _read_live_block(linked, 0x684)

    def lu32(name): return _buf_u32(linked_data, _LIVE_LINKED_OFF[name])
    def ls32(name): return _buf_s32(linked_data, _LIVE_LINKED_OFF[name])
    def lf32(name): return _buf_f32(linked_data, _LIVE_LINKED_OFF[name])
    def la(name): return linked + _LIVE_LINKED_OFF[name] if linked_data else None

    actor_type = ls32("actor_type") if linked_data else 0
    clash_priority = ls32("clash_priority") if linked_data else 0
    quota_max = ls32("quota_max") if linked_data else 0
    quota_used = ls32("quota_used") if linked_data else 0
    quota_remaining = max(0, quota_max - quota_used) if quota_max >= 0 else quota_max - quota_used
    hit_flags_a = lu32("hit_flags_a") if linked_data else 0
    hit_flags_b = lu32("hit_flags_b") if linked_data else 0
    damage = ls32("damage") if linked_data else 0
    shape_type = lu32("shape_type") if linked_data else 0
    shape_count = lu32("shape_count") if linked_data else 0
    shape_ptr = lu32("shape_ptr") if linked_data else 0

    details = [
        _live_detail("Actor", "Actor address", "base", _fmt_hex(actor), "Confirmed", actor),
        _live_detail("Actor", "Owner slot", "+130", owner_slot, "Confirmed", actor + 0x130),
        _live_detail("Actor", "Owner pointer", "+130", _fmt_hex(owner), "Confirmed", actor + 0x130),
        _live_detail("Actor", "Owner action ID", "owner +1E8", f"{owner_action_id} ({_fmt_hex(owner_action_id)})", "Confirmed", owner + _LIVE_FIGHTER_ACTION_OFF),
        _live_detail("Actor", "Projectile ID", "+134", f"{projectile_id} ({_fmt_hex(projectile_id)})", "Confirmed", actor + 0x134),
        _live_detail("Actor", "Owner/root mirror", "+138", _fmt_hex(owner_mirror), "High", actor + 0x138),
        _live_detail("Actor", "Linked collision record", "+13C", _fmt_hex(linked), "Confirmed", actor + 0x13C),
        _live_detail("Motion", "Current position", "+5C/+6C/+7C", _fmt_vec(x, y, z), "Confirmed", actor + 0x5C),
        _live_detail("Motion", "Previous position", "+BC/+CC/+DC", _fmt_vec(prev_x, prev_y, prev_z), "Confirmed", actor + 0xBC),
        _live_detail("Motion", "Per-frame movement", "current - previous", _fmt_vec(vel_x, vel_y, vel_z), "Confirmed"),
        _live_detail("Motion", "Emitter/origin", "+E0/+E4/+E8", _fmt_vec(*emitter), "High", actor + 0xE0),
        _live_detail("Motion", "Direction vector", "+108/+10C/+104", _fmt_vec(*direction), "High", actor + 0x108),
        _live_detail("Motion", "Impact/contact position", "+118/+11C/+120", _fmt_vec(*impact), "High", actor + 0x118),
    ]

    if linked_data:
        linked_owner = lu32("owner")
        target = lu32("target")
        damage_scale = lf32("damage_scale")
        hitstun = ls32("hitstun_override")
        blockstun = ls32("blockstun_override")
        giant_stagger = ls32("giant_stagger")
        hitstop = ls32("hitstop_override")
        mutual_lockout = lf32("mutual_clash_lockout")
        hit_dir = (lf32("hit_dir_x"), lf32("hit_dir_y"), lf32("hit_dir_z"))
        clash_bypass = bool(hit_flags_b & (1 << 4))
        priority_override = bool(hit_flags_b & (1 << 5))
        response_class = (hit_flags_b >> 6) & 0x7

        details.extend([
            _live_detail("Collision", "Linked owner pointer", "+30", _fmt_hex(linked_owner), "High", la("owner")),
            _live_detail("Collision", "Target/contact fighter", "+34", _fmt_hex(target), "Confirmed", la("target")),
            _live_detail("Collision", "Collision actor type", "+28", actor_type, "Medium-high", la("actor_type")),
            _live_detail("Collision", "Projectile clash priority", "+4C", clash_priority, "Confirmed", la("clash_priority")),
            _live_detail("Collision", "Collision quota maximum", "+54", quota_max, "Confirmed", la("quota_max")),
            _live_detail("Collision", "Collision quota consumed", "+58", quota_used, "Confirmed", la("quota_used")),
            _live_detail("Collision", "Collision quota remaining", "+54 - +58", quota_remaining, "Computed"),
            _live_detail("Collision", "Mutual-clash lockout", "+78", _fmt_float(mutual_lockout), "High", la("mutual_clash_lockout")),
            _live_detail("Collision", "Hit flags A", "+80", _fmt_hex(hit_flags_a), "High", la("hit_flags_a")),
            _live_detail("Collision", "Hit flags B", "+84", _fmt_hex(hit_flags_b), "Confirmed", la("hit_flags_b")),
            _live_detail("Collision", "Bypass projectile clashes", "+84 bit 4", str(clash_bypass), "High", la("hit_flags_b")),
            _live_detail("Collision", "Clash-priority override", "+84 bit 5", str(priority_override), "Confirmed", la("hit_flags_b")),
            _live_detail("Collision", "Equal-priority response class", "+84 bits 6-8", response_class, "High", la("hit_flags_b")),
            _live_detail("Hit", "Damage", "+8C", damage, "Confirmed", la("damage")),
            _live_detail("Hit", "Damage/hit scaling multiplier", "+90", _fmt_float(damage_scale), "High", la("damage_scale")),
            _live_detail("Hit", "Hitstun override", "+94", hitstun, "Confirmed", la("hitstun_override")),
            _live_detail("Hit", "Blockstun override", "+98", blockstun, "Confirmed", la("blockstun_override")),
            _live_detail("Hit", "Giant stagger/armor impact", "+A0", giant_stagger, "High", la("giant_stagger")),
            _live_detail("Hit", "Hitstop override", "+A4", hitstop, "High", la("hitstop_override")),
            _live_detail("Hit", "Hit direction", "+A8/+AC/+B0", _fmt_vec(*hit_dir), "Confirmed", la("hit_dir_x")),
            _live_detail("Effects", "Contact effect 1", "+BC / +CC", f"ID {_fmt_hex(lu32('effect_1_id'))}, arg {_fmt_hex(lu32('effect_1_arg'))}", "Confirmed", la("effect_1_id")),
            _live_detail("Effects", "Contact effect 2", "+C0 / +D0", f"ID {_fmt_hex(lu32('effect_2_id'))}, arg {_fmt_hex(lu32('effect_2_arg'))}", "Confirmed", la("effect_2_id")),
            _live_detail("Effects", "World collision effect", "+C8 / +D8", f"ID {_fmt_hex(lu32('world_effect_id'))}, arg {_fmt_hex(lu32('world_effect_arg'))}", "High", la("world_effect_id")),
            _live_detail("Geometry", "Shape type", "+308", f"{shape_type}: {_LIVE_SHAPE_LABELS.get(shape_type, 'Unknown')}", "Confirmed", la("shape_type")),
            _live_detail("Geometry", "Shape array", "+30C", _fmt_hex(shape_ptr), "Confirmed", la("shape_ptr")),
            _live_detail("Geometry", "Shape count", "+314", shape_count, "Confirmed", la("shape_count")),
            _live_detail("Geometry", "Contact-point Y offset", "+320", _fmt_float(lf32("contact_offset_y")), "High", la("contact_offset_y")),
            _live_detail("Geometry", "Contact-point X offset", "+324", _fmt_float(lf32("contact_offset_x")), "High", la("contact_offset_x")),
            _live_detail("Geometry", "Computed world contact", "+660/+670/+680", _fmt_vec(lf32("contact_world_x"), lf32("contact_world_y"), lf32("contact_world_z")), "High", linked + 0x660),
            _live_detail("Research", "Secondary stun/reaction raw", "+9C", ls32("secondary_reaction_raw"), "Unresolved", la("secondary_reaction_raw")),
            _live_detail("Research", "Paired-contact state A", "+B4", ls32("paired_state_a"), "Behavior known", la("paired_state_a")),
            _live_detail("Research", "Paired-contact state B", "+B8", ls32("paired_state_b"), "Behavior known", la("paired_state_b")),
            _live_detail("Research", "Effect slot 3 raw", "+C4 / +D4", f"ID {_fmt_hex(lu32('effect_3_id_raw'))}, arg {_fmt_hex(lu32('effect_3_arg_raw'))}", "Unresolved", la("effect_3_id_raw")),
            _live_detail("Research", "Contact offset mode A raw", "+318", _fmt_hex(lu32("contact_mode_a_raw")), "Unresolved", la("contact_mode_a_raw")),
            _live_detail("Research", "Contact offset mode B raw", "+31C", _fmt_hex(lu32("contact_mode_b_raw")), "Unresolved", la("contact_mode_b_raw")),
        ])

    details.extend([
        _live_detail("Research", "Actor scale candidate", "+F8", _fmt_float(scale_candidate), "Candidate", actor + 0xF8),
        _live_detail("Research", "Actor fixed scale raw", "+110", f"{fixed_scale_raw} ({fixed_scale_raw / 1024.0:.4f}x)", "High", actor + 0x110),
    ])

    return {
        "actor": actor,
        "owner": owner_slot,
        "owner_pointer": owner,
        "owner_action_id": owner_action_id,
        "projectile_id": projectile_id,
        "linked": linked,
        "damage": damage,
        "position": (x, y, z),
        "velocity": (vel_x, vel_y, vel_z),
        "clash_priority": clash_priority,
        "quota": (quota_used, quota_max),
        "shape_type": shape_type,
        "shape_count": shape_count,
        "details": details,
    }


def _collect_live_projectiles() -> list[dict]:
    if rbytes is None:
        return []

    slot_bases = _current_live_slot_bases()
    owner_names = {base: slot_label for slot_label, base in slot_bases.items()}
    if not owner_names:
        return []

    table_data = _read_live_block(_LIVE_ACTOR_TABLE, _LIVE_ACTOR_TABLE_COUNT * 4)
    table_ptrs = []
    for i in range(_LIVE_ACTOR_TABLE_COUNT):
        ptr = _buf_u32(table_data, i * 4)
        if 0x91000000 <= ptr < 0x94000000:
            table_ptrs.append(ptr)
    table_seen = set(table_ptrs)

    actor_blocks: dict[int, bytes] = {}
    for pool_base in _LIVE_ACTOR_POOL_BASES:
        pool_data = _read_live_block(pool_base, _LIVE_ACTOR_STRIDE * _LIVE_ACTOR_COUNT)
        if len(pool_data) < _LIVE_ACTOR_STRIDE:
            continue
        for i in range(_LIVE_ACTOR_COUNT):
            off = i * _LIVE_ACTOR_STRIDE
            chunk = pool_data[off:off + _LIVE_ACTOR_STRIDE]
            if len(chunk) == _LIVE_ACTOR_STRIDE:
                actor_blocks[pool_base + off] = chunk

    for ptr in table_ptrs:
        if ptr not in actor_blocks:
            chunk = _read_live_block(ptr, _LIVE_ACTOR_STRIDE)
            if len(chunk) == _LIVE_ACTOR_STRIDE:
                actor_blocks[ptr] = chunk

    out = []
    for actor, actor_data in actor_blocks.items():
        decoded = _decode_live_actor(actor, actor_data, actor in table_seen, owner_names)
        if decoded is not None:
            out.append(decoded)
    out.sort(key=lambda r: (str(r.get("owner")), int(r.get("projectile_id") or 0), int(r.get("actor") or 0)))
    return out



def collect_live_projectiles() -> list[dict]:
    """Public read-only wrapper for the validated live projectile pool."""
    return _collect_live_projectiles()


def scan_slot_projectile_definitions(
    slot_label: str,
    character_key: str,
    fighter_base: int | None,
    char_id: int | None,
) -> list[dict]:
    """Synchronously return validated projectile/super records for one slot.

    Callers that run this from a GUI should place it on a worker thread.
    """
    result: list[dict] = []
    _run_monitor_slot_scan(
        str(slot_label),
        str(character_key),
        fighter_base,
        char_id,
        lambda _pct: None,
        lambda hits: result.extend(list(hits or [])),
    )
    return result

# ---------------------------------------------------------------------------
# Blank fields for opcode-scan hits
# ---------------------------------------------------------------------------
_OPCODE_HIT_FIELDS = {
    "radius": "?", "speed": "?", "speed_mult": "?", "accel": "?",
    "kb_x": "?", "kb_y": "?", "arc": "?", "arc2": "?",
    "percent_scale": "?", "hitbox": "?", "motion_family": "?",
    "type": "?", "id": "?", "spawn_x": "?", "spawn_y": "?",
    "lifetime": "?", "fixed_scale": "?", "hb_size": "?",
    "physics_tail_d4": "?", "mode_a": "?", "mode_b": "?",
    "linked_resource": "?", "flags_72": "?", "c042": "?",
    "cluster": "script",
    "ex03c": "?", "ex060": "?", "ex090": "?", "ex094": "?",
    "ex09c": "?", "ex0d4": "?", "ex0e4": "?",
    "super_speed": "?", "super_accel": "?",
    "super_speed_2": "?", "super_accel_b": "?", "super_accel_c": "?",
    "super_air_kb_y": "?", "super_multihit_cap": "?",
    "super_radius": "?", "super_hit_react": "?", "super_life": "?",
}


# ---------------------------------------------------------------------------
# Opcode-scan pass  (05 2B and any future SCRIPT_OPCODES entries)
# ---------------------------------------------------------------------------
def _scan_opcode_blocks(data: bytes, base_addr: int, hits: list, lookup: dict,
                        slot_char_ids: dict | None = None,
                        requested_keys: set[str] | None = None) -> None:
    frank_bases = {
        base for base, cid in (slot_char_ids or {}).items() if cid == FRANK_CHAR_ID
    }
    requested_keys = {str(k) for k in (requested_keys or set())}
    for sig, info in SCRIPT_OPCODES.items():
        fmt_name   = info["fmt_name"]
        dmg_offset = info["dmg_offset"]

        pos = 0
        while True:
            idx = data.find(sig, pos)
            if idx < 0:
                break
            pos = idx + 1

            if idx + dmg_offset + 2 > len(data):
                continue

            dmg = (data[idx + dmg_offset] << 8) | data[idx + dmg_offset + 1]
            if dmg < 2 or dmg > 20000:
                continue

            addr = base_addr + idx
            extra = {
                "preA":   _read_u8(addr - 2),
                "preB":   _read_u8(addr - 1),
                "opcode": _read_u16_hex(addr),
                "param1": _read_u16_hex(addr + 2),
                "param2": _read_u16_hex(addr + 4),
                "param3": _read_u16_hex(addr + 6),
                "f32_1":  _read_f32(addr + 8),
                "f32_2":  _read_f32(addr + 12),
                "f32_3":  _read_f32(addr + 16),
            }
            c_word = b""
            if addr >= base_addr + 4:
                c_word = data[idx - 4:idx]
            pre_word = data[idx - 8:idx - 4] if idx >= 8 else b""
            sig_keys = _keys_for_block(c_word, pre_word)

            base_hit = {
                "addr": addr,
                "dmg":  dmg,
                "fmt":  fmt_name,
                "dmg_write_addr": _resolve_script_dmg_addr(addr, dmg) or (addr + dmg_offset),
                **_OPCODE_HIT_FIELDS,
                **extra,
            }

            slot_key = _key_for_hit_addr(addr, slot_char_ids)

            if slot_key is not None:
                slot_matches = [(k, mv) for k, mv in lookup.get(dmg, []) if k == slot_key]
                if slot_matches:
                    for key, mv in slot_matches:
                        if (
                            key == "FRANK"
                            and "zombie" in str(mv).lower()
                            and _owning_chr_tbl(addr) in frank_bases
                            and not _is_frank_zombie_fall_label(mv)
                        ):
                            continue
                        hits.append({**base_hit, "key": key, "move": mv})
                    continue
                # If ownership is known, never fall back to another character's
                # damage-only name.  For a selected slot with no saved projectile
                # map, retain the record under a stable generic name so the profile
                # monitor can still discover and edit it automatically.
                if requested_keys and slot_key not in requested_keys:
                    continue
                if not requested_keys or slot_key in requested_keys:
                    hits.append({
                        **base_hit,
                        "key": slot_key,
                        "move": f"Script projectile @ 0x{addr:08X}",
                    })
                    continue

            if dmg in lookup:
                matches = lookup[dmg]

                if sig_keys:
                    sig_matches = [(k, mv) for k, mv in matches if k in sig_keys]
                    if sig_matches:
                        matches = sig_matches

                for key, mv in matches:
                    if (
                        key == "FRANK"
                        and "zombie" in str(mv).lower()
                        and _owning_chr_tbl(addr) in frank_bases
                        and not _is_frank_zombie_fall_label(mv)
                    ):
                        continue
                    hits.append({**base_hit, "key": key, "move": mv})
            elif sig_keys and dmg >= 1:
                for key in sig_keys:
                    hits.append({**base_hit, "key": key, "move": "Signature Match"})
            else:
                hits.append({**base_hit, "key": "?", "move": "Unknown"})

# ---------------------------------------------------------------------------
# Suffix-scan pass  (template / template2 / script(0xNN))
# ---------------------------------------------------------------------------
def _is_super_like_block(data: bytes, base_off: int) -> bool:
    """
    Permissive secondary filter for super/script-like blocks.
    This does NOT try to enforce the normal projectile template rules.
    """
    end = len(data)

    def u16(off: int) -> int | None:
        o = base_off + off
        if o + 2 > end:
            return None
        return struct.unpack_from(">H", data, o)[0]

    def u32(off: int) -> int | None:
        o = base_off + off
        if o + 4 > end:
            return None
        return struct.unpack_from(">I", data, o)[0]

    def f32(off: int) -> float | None:
        o = base_off + off
        if o + 4 > end:
            return None
        return struct.unpack_from(">f", data, o)[0]

    disc = u32(_DISCRIMINATOR)
    if disc is None:
        return False

    # Let the regular template/template2 path own those.
    if disc in (0xFFFFFFFF, 0x00000000, 0x00000001):
        return False

    dmg = u16(0x02)
    if dmg is None or not (500 <= dmg <= 20000):
        return False

    plausible = 0

    hb = f32(_VALID_HITBOX)
    if hb is not None and 0.0 <= hb <= 300.0:
        plausible += 1

    accel = f32(_VALID_ACCEL)
    if accel is not None and -10.0 <= accel <= 10.0:
        plausible += 1

    hb_size = u16(_VALID_HBSIZE)
    if hb_size is not None and 0 <= hb_size <= 4096:
        plausible += 1

    c042 = u16(_VALID_C042)
    if c042 is not None and 0 <= c042 <= 64:
        plausible += 1

    return plausible >= 2

def _scan_suffix_blocks(data: bytes, base_addr: int, hits: list,
                         lookup: dict, id_map: dict,
                         slot_char_ids: dict | None = None,
                         requested_keys: set[str] | None = None) -> None:
    frank_bases = {
        base for base, cid in (slot_char_ids or {}).items() if cid == FRANK_CHAR_ID
    }
    requested_keys = {str(k) for k in (requested_keys or set())}
    pos = 0
    while True:
        idx = data.find(_SUFFIX, pos)
        if idx < 0:
            break
        pos = idx + 1

        if idx < 4:
            continue

        c = data[idx - 4:idx]
        pre8 = data[idx - 8:idx - 4] if idx >= 8 else b""
        sig_keys = _keys_for_block(c, pre8)

        dmg = (c[2] << 8) | c[3]
        if not dmg:
            continue

        base_off = idx - 4
        a = base_addr + base_off

        after4 = data[idx + 4:idx + 8] if idx + 8 <= len(data) else b""
        fmt = _classify_discriminator(after4)

        is_template_ok = False
        is_super_ok = False

        if fmt in ("template", "template2"):
            slot_owned = _owning_chr_tbl(a) is not None
            if not slot_owned:
                ok = _validate_template(data, base_off)
                if not ok:
                    continue
            is_template_ok = True
        else:
            # New parallel permissive path for supers / script-like blocks
            if _owning_chr_tbl(a) is not None and _is_super_like_block(data, base_off):
                is_super_ok = True
                fmt = "super_like"
            else:
                continue


        if is_template_ok:
            motion_family = _read_u16(a + FIELD_OFFSETS["motion_family"])
            speed_mult = _read_f32(a + FIELD_OFFSETS["speed_mult"])
            percent_scale = _read_f32(a + FIELD_OFFSETS["percent_scale"])
            fixed_scale = _read_u16(a + FIELD_OFFSETS["fixed_scale"])
            physics_tail = _read_f32(a + FIELD_OFFSETS["physics_tail_d4"]) if str(motion_family) == "4" else "?"
            fields = {
                "radius":          _read_f32(a + FIELD_OFFSETS["radius"]),
                "kb_x":            _read_f32(a + FIELD_OFFSETS["kb_x"]),
                "kb_y":            _read_f32(a + FIELD_OFFSETS["kb_y"]),
                "motion_family":   motion_family,
                "type":            _read_u8(a + FIELD_OFFSETS["type"]),
                "id":              _read_u16(a + FIELD_OFFSETS["id"]),
                "lifetime":        _read_u16(a + FIELD_OFFSETS["lifetime"]),
                "fixed_scale":     fixed_scale,
                "hb_size":         fixed_scale,
                "speed":           _read_f32(a + FIELD_OFFSETS["speed"]),
                "speed_mult":      speed_mult,
                "accel":           speed_mult,
                "percent_scale":   percent_scale,
                "hitbox":          percent_scale,
                "arc":             _read_f32(a + FIELD_OFFSETS["arc"]),
                "arc2":            _read_f32(a + FIELD_OFFSETS["arc2"]),
                "physics_tail_d4": physics_tail,
                "mode_a":          _read_u32(a + FIELD_OFFSETS["mode_a"]),
                "mode_b":          _read_u32(a + FIELD_OFFSETS["mode_b"]),
                "linked_resource": _read_u32(a + FIELD_OFFSETS["linked_resource"]),
                "flags_72":        _read_u32(a + FIELD_OFFSETS["flags_72"]),
                "c042":            _read_u16(a + FIELD_OFFSETS["c042"]),
                "preA": "?", "preB": "?",
                "opcode": "?", "param1": "?", "param2": "?", "param3": "?",
                "f32_1": "?", "f32_2": "?", "f32_3": "?",
                "cluster": "",
            }
        else:
            # super_like path: do not force all projectile fields to mean anything
            fields = {
                **_OPCODE_HIT_FIELDS,
                "speed":  _read_f32(a + FIELD_OFFSETS["speed"]),
                "accel":  _read_f32(a + FIELD_OFFSETS["accel"]),
                "hitbox": _read_f32(a + FIELD_OFFSETS["hitbox"]),
                "type":   _read_u8(a + FIELD_OFFSETS["type"]),
                "id":     _read_u16(a + FIELD_OFFSETS["id"]),
                "cluster": "super_like",
            }

        slot_key = _key_for_hit_addr(a, slot_char_ids)

        if slot_key is not None:
            slot_matches = [(k, mv) for k, mv in lookup.get(dmg, []) if k == slot_key]
            if slot_matches:
                matches = slot_matches

                if len({k for k, _ in matches}) > 1:
                    proj_id = fields.get("id")
                    try:
                        pid_int = int(proj_id)
                    except (TypeError, ValueError):
                        pid_int = None
                    if pid_int is not None:
                        id_matches = [
                            (k, mv) for k, mv in matches
                            if id_map.get(k, {}).get(mv) == pid_int
                        ]
                        if id_matches:
                            matches = id_matches

                for key, mv in matches:
                    if (
                        key == "FRANK"
                        and "zombie" in str(mv).lower()
                        and _owning_chr_tbl(a) in frank_bases
                        and not _is_frank_zombie_fall_label(mv)
                    ):
                        continue
                    hits.append({
                        "addr": a,
                        "key": key,
                        "move": mv,
                        "dmg": dmg,
                        "fmt": fmt,
                        "dmg_write_addr": _resolve_script_dmg_addr(a, dmg) or (a + 2),
                        **fields
                    })
                continue
            if requested_keys and slot_key not in requested_keys:
                continue
            if not requested_keys or slot_key in requested_keys:
                try:
                    pid_value = int(fields.get("id"))
                except Exception:
                    pid_value = None
                if pid_value is not None and pid_value >= 0:
                    generic_move = f"Projectile 0x{pid_value:04X}"
                else:
                    generic_move = f"Projectile @ 0x{a:08X}"
                hits.append({
                    "addr": a,
                    "key": slot_key,
                    "move": generic_move,
                    "dmg": dmg,
                    "fmt": fmt,
                    "dmg_write_addr": _resolve_script_dmg_addr(a, dmg) or (a + 2),
                    **fields,
                })
                continue

        is_slot_owned = _owning_chr_tbl(a) is not None
        ok = _validate_template(data, base_off, relaxed=is_slot_owned)
        if dmg in lookup:
            matches = lookup[dmg]

            if sig_keys:
                sig_matches = [(k, mv) for k, mv in matches if k in sig_keys]
                if sig_matches:
                    matches = sig_matches

            if len({k for k, _ in matches}) > 1:
                proj_id = fields.get("id")
                try:
                    pid_int = int(proj_id)
                except (TypeError, ValueError):
                    pid_int = None
                if pid_int is not None:
                    id_matches = [
                        (k, mv) for k, mv in matches
                        if id_map.get(k, {}).get(mv) == pid_int
                    ]
                    if id_matches:
                        matches = id_matches

            for key, mv in matches:
                if (
                    key == "FRANK"
                    and "zombie" in str(mv).lower()
                    and _owning_chr_tbl(a) in frank_bases
                    and not _is_frank_zombie_fall_label(mv)
                ):
                    continue
                hits.append({
                    "addr": a,
                    "key": key,
                    "move": mv,
                    "dmg": dmg,
                    "fmt": fmt,
                    "dmg_write_addr": _resolve_script_dmg_addr(a, dmg) or (a + 2),
                    **fields
                })

        elif sig_keys and dmg >= 1:
            for key in sig_keys:
                hits.append({
                    "addr": a,
                    "key": key,
                    "move": "Signature Match",
                    "dmg": dmg,
                    "fmt": fmt,
                    "dmg_write_addr": _resolve_script_dmg_addr(a, dmg) or (a + 2),
                    **fields
                })

        elif dmg >= 500:
            hits.append({
                "addr": a,
                "key": "?",
                "move": "Unknown",
                "dmg": dmg,
                "fmt": fmt,
                "dmg_write_addr": _resolve_script_dmg_addr(a, dmg) or (a + 2),
                **fields
            })
# ---------------------------------------------------------------------------
# Zombie canonical block scanner
#
# The authoritative spawn-like signature for Zombie Spree/Attack/Fall is:
#   2C 11 02 3F  (block start marker)
#   followed within 0x40 bytes by:
#   04 01 02 3F 00 00 00 XX  (where XX is the variant byte)
#
# Only variants 0x36–0x3B are valid Zombie spawn variants per the notes.
# Anything outside this range is noise from other characters' scripts.
#
# Deduplication: one row per (chr_tbl_base, variant) across the full scan.
# ---------------------------------------------------------------------------
_ZOMBIE_BLOCK_SIG  = b"\x2C\x11\x02\x3F"
_ZOMBIE_INNER_SIG  = b"\x04\x01\x02\x3F\x00\x00\x00"
_ZOMBIE_INNER_LOOK = 0x40
_ZOMBIE_VARIANT_MIN = 0x36
_ZOMBIE_VARIANT_MAX = 0x3B

_ZOMBIE_VARIANT_NAMES: dict[int, str] = {
    0x36: "Zombie Spree (v0x36)",
    0x37: "Zombie Spree (v0x37)",
    0x38: "Zombie Spree (v0x38)",
    0x39: "Zombie Spree (v0x39)",
    0x3A: "Zombie Spree (v0x3A)",
    0x3B: "Zombie Spree (v0x3B)",
}

_FRANK_ZOMBIE_FALL_NAMES = {"Zombie Fall", "Zombie fall"}
_FRANK_ZOMBIE_FALL_OFF = 0x19BE
_FRANK_ZOMBIE_ATTACK_OFF = 0x0B14
_FRANK_ZOMBIE_SPREE_OFF  = 0x7C4C

_FRANK_ZOMBIE_SPREE_KBY_L = 0x12E
_FRANK_ZOMBIE_SPREE_KBY_M = 0x17E
_FRANK_ZOMBIE_SPREE_KBY_H = 0x1CE

_FRANK_ZOMBIE_SPREE_ARC_L   = 0x122
_FRANK_ZOMBIE_SPREE_ACCEL_L = 0x142

_FRANK_ZOMBIE_SPREE_ARC_M   = 0x172
_FRANK_ZOMBIE_SPREE_ACCEL_M = 0x192

_FRANK_ZOMBIE_SPREE_ARC_H   = 0x1C2
_FRANK_ZOMBIE_SPREE_ACCEL_H = 0x1E2

_FRANK_ZOMBIE_ATTACK_SPEED_A = 0x7E
_FRANK_ZOMBIE_ATTACK_SPEED_A = 0x7E
_FRANK_ZOMBIE_ATTACK_ACCEL_A = 0x92
_FRANK_ZOMBIE_ATTACK_SPAWN_X = 0x159
_FRANK_ZOMBIE_FALL_DMG_OFF = 0x04
_FRANK_ZOMBIE_FALL_SPAWN_Y_OFF = 0xA6
# Exact per-slot ownership ranges derived from chr_tbl analysis notes.
# Each tuple is (chr_tbl_base, move_data_start, max_referenced_addr + slack).
# Using tight bounds prevents cross-slot false positives.
_CHR_TBL_RANGES = [
    (0x90896640, 0x90896640, 0x908D2000),            # slot 0
    (0x908F1920, 0x908F1920, 0x9092B634 + 0x2000),   # slot 1
    (0x909478E0, 0x909478E0, 0x909BE310 + 0x2000),   # slot 2
    (0x9099D9C0, 0x9099D9C0, 0x909DECAC + 0x2000),   # slot 3
]

def _owning_chr_tbl(addr: int) -> int | None:
    """
    Dynamic ownership:
    assign the hit to the closest chr_tbl base within a sane forward window.
    This avoids hardcoding slot-specific end ranges that can miss valid data.
    """
    best_base = None
    best_dist = None

    for base in _current_chr_tbl_bases():
        if addr < base:
            continue
        dist = addr - base
        if dist > 0x90000:   # generous window; adjust if needed
            continue
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_base = base

    return best_base

def _key_for_hit_addr(addr: int, slot_char_ids: dict[int, int] | None) -> str | None:
    if not slot_char_ids:
        return None
    base = _owning_chr_tbl(addr)
    if base is None:
        return None
    cid = slot_char_ids.get(base)
    if cid is None:
        return None
    return _projectile_key_from_char_id(cid)
def _is_frank_zombie_move_label(move: str) -> bool:
    s = str(move or "").lower()
    return "zombie" in s

def _is_frank_zombie_fall_label(move: str) -> bool:
    return str(move or "") in _FRANK_ZOMBIE_FALL_NAMES

def _apply_frank_zombie_anchor(hits: list[dict]) -> list[dict]:
    """
    Frank-specific override:
      - if a real Zombie Fall row exists, use it as anchor
      - otherwise synthesize Zombie Fall from the owning chr_tbl base
      - derive Attack/Spree from Fall
    """
    anchored_rows: list[dict] = []
    anchor_bases: set[int] = set()
    fall_by_base: dict[int, dict] = {}
    zombie_base_seed: dict[int, dict] = {}

    for h in hits:
        if h.get("key") != "FRANK":
            continue

        base = _owning_chr_tbl(h.get("addr", 0))
        if base is None:
            continue

        if _is_frank_zombie_move_label(h.get("move", "")):
            zombie_base_seed.setdefault(base, h)

        if _is_frank_zombie_fall_label(h.get("move", "")):
            fall_by_base.setdefault(base, h)

    for base, seed in zombie_base_seed.items():
        fall_hit = fall_by_base.get(base)
        if fall_hit is None:
            fall_addr = base + _FRANK_ZOMBIE_FALL_OFF
            fall_hit = {
                **seed,
                "addr": fall_addr,
                "move": "Zombie Fall",
                "dmg": 3200,
                "fmt": "frank_zoml",
                "cluster": f"frank zombie anchor @ 0x{fall_addr:08X}",
                "dmg_write_addr": fall_addr + _FRANK_ZOMBIE_FALL_DMG_OFF,
            }

        anchor_bases.add(base)
        fall_addr   = int(fall_hit["addr"])
        attack_addr = fall_addr + _FRANK_ZOMBIE_ATTACK_OFF
        spree_addr  = fall_addr + _FRANK_ZOMBIE_SPREE_OFF - 4

        anchored_rows.append({
            **fall_hit,
            "addr": fall_addr,
            "move": "Zombie Fall",
            "dmg": 3200,
            "cluster": f"frank zombie anchor @ 0x{fall_addr:08X}",
            "dmg_write_addr": fall_addr + _FRANK_ZOMBIE_FALL_DMG_OFF,
            "spawn_y": _read_f32(fall_addr + _FRANK_ZOMBIE_FALL_SPAWN_Y_OFF),
        })

        anchored_rows.append({
            **fall_hit,
            "addr": attack_addr,
            "move": "Zombie Attack",
            "dmg": 2400,
            "cluster": f"frank zombie anchor @ 0x{fall_addr:08X}",
            "dmg_write_addr": base + _SCRIPT_DMG_OFFSETS[2400],
            "speed":   _read_f32(attack_addr + _FRANK_ZOMBIE_ATTACK_SPEED_A),
            "accel":   _read_f32(attack_addr + _FRANK_ZOMBIE_ATTACK_ACCEL_A),
            "spawn_x": _read_u8(attack_addr + _FRANK_ZOMBIE_ATTACK_SPAWN_X),
        })
        
        anchored_rows.append({
            **fall_hit,
            "addr": spree_addr,
            "move": "Zombie Spree L",
            "dmg": 2400,
            "cluster": f"frank zombie anchor @ 0x{fall_addr:08X}",
            "dmg_write_addr": base + _SCRIPT_DMG_OFFSETS[2400],
            "arc":   _read_f32(spree_addr + _FRANK_ZOMBIE_SPREE_ARC_L),
            "kb_y":  _read_f32(spree_addr + _FRANK_ZOMBIE_SPREE_KBY_L),
            "speed": _read_f32(spree_addr + _FRANK_ZOMBIE_SPREE_ACCEL_L),
        })

        anchored_rows.append({
            **fall_hit,
            "addr": spree_addr,
            "move": "Zombie Spree M",
            "dmg": 2400,
            "cluster": f"frank zombie anchor @ 0x{fall_addr:08X}",
            "dmg_write_addr": base + _SCRIPT_DMG_OFFSETS[2400],
            "arc":   _read_f32(spree_addr + _FRANK_ZOMBIE_SPREE_ARC_M),
            "kb_y":  _read_f32(spree_addr + _FRANK_ZOMBIE_SPREE_KBY_M),
            "speed": _read_f32(spree_addr + _FRANK_ZOMBIE_SPREE_ACCEL_M),
        })

        anchored_rows.append({
            **fall_hit,
            "addr": spree_addr,
            "move": "Zombie Spree H",
            "dmg": 2400,
            "cluster": f"frank zombie anchor @ 0x{fall_addr:08X}",
            "dmg_write_addr": base + _SCRIPT_DMG_OFFSETS[2400],
            "arc":   _read_f32(spree_addr + _FRANK_ZOMBIE_SPREE_ARC_H),
            "kb_y":  _read_f32(spree_addr + _FRANK_ZOMBIE_SPREE_KBY_H),
            "speed": _read_f32(spree_addr + _FRANK_ZOMBIE_SPREE_ACCEL_H),
        })

    if not anchor_bases:
        return hits

    kept: list[dict] = []
    for h in hits:
        if (
            h.get("key") == "FRANK"
            and _is_frank_zombie_move_label(h.get("move", ""))
            and _owning_chr_tbl(h.get("addr", 0)) in anchor_bases
        ):
            continue
        kept.append(h)

    kept.extend(anchored_rows)
    kept.sort(key=lambda x: (int(x.get("addr", 0)), str(x.get("move", ""))))
    return kept
# ---------------------------------------------------------------------------
# Main scan thread
# ---------------------------------------------------------------------------
def _scan_zombie_blocks(data: bytes, base_addr: int, hits: list,
                         lookup: dict, seen_variants: set,
                         slot_char_ids: dict | None = None) -> None:
    """
    Disabled for now.
    Raw zombie spree variants stay suppressed.
    """
    return
def _looks_like_super_dispatch_0023(data: bytes, idx: int) -> bool:
    'Return True when a 00/23 match is a super/action dispatch row.\n\n    Compact projectile-super cards and super dispatch rows both begin with\n    00 23 00 00.  Dispatch rows are not projectile payloads: at +0x02 they\n    hold the action selector u32, at +0x06 variant, at +0x0A phase length, and\n    at +0x16 a child script link.  If the module let the projectile scanner own these,\n    rows like Morrigan selector 0x60 become fake "damage 96" bullets.\n    '
    try:
        if idx < 0 or idx + 0x1A > len(data):
            return False
        if data[idx:idx + 2] != b"\x00\x23":
            return False
        selector = struct.unpack_from(">I", data, idx + 0x02)[0]
        variant = struct.unpack_from(">I", data, idx + 0x06)[0]
        phase = struct.unpack_from(">I", data, idx + 0x0A)[0]
        param_a = struct.unpack_from(">I", data, idx + 0x0E)[0]
        param_b = struct.unpack_from(">I", data, idx + 0x12)[0]
        child_link = struct.unpack_from(">I", data, idx + 0x16)[0]
    except Exception:
        return False

    # Confirmed Ryu/Shinkuu and Morrigan rows use small selectors and sane
    # phase values, then jump through a 00/04xxxx-style script link.
    if not (0 <= selector <= 0x200):
        return False
    if not (0 <= variant <= 0x200):
        return False
    if not (0 <= phase <= 0x400):
        return False
    if not (0 <= param_a <= 0x400 and 0 <= param_b <= 0x400):
        return False
    if not (0x00010000 <= child_link <= 0x00090000):
        return False
    return True


def _read_projectile_super_field(addr: int, name: str):
    off, typ = _PROJECTILE_SUPER_FIELD_OFFSETS.get(name, (None, None))
    if off is None:
        return "?"
    a = int(addr) + int(off)
    if typ == "f32":
        return _read_f32(a)
    if typ == "u16":
        return _read_u16(a)
    if typ == "u32":
        return _read_u32(a)
    if typ == "u8":
        return _read_u8(a)
    return "?"

def _projectile_super_case_label(slot_key: str | None, owner_base: int | None, addr: int, dmg: int, card_type: int) -> str | None:
    """Best-effort case names for compact projectile-super cards farmed from dumps."""
    rel = int(addr) - int(owner_base or 0) if owner_base else None
    if slot_key == "MORRIGAN":
        # Finishing Shower is a dense table of small 00/23 bullet cards.
        if 0x4B000 <= int(rel or 0) <= 0x4E800 and 20 <= int(dmg) <= 140:
            return "Finishing Shower Bullet"
        return "Morrigan Projectile Super Card"
    if slot_key == "VOLNUTT":
        if int(dmg) == 480:
            return "Machine Gun Sweep"
        if int(dmg) == 2000:
            return "Machine Gun Sweep Super Card"
        return None
    if slot_key == "TEKKAMAN":
        if int(dmg) == 1280:
            return "Voltekka (Ground)"
        if int(dmg) == 720:
            return "Voltekka Projectile Card"
        if int(dmg) == 1760:
            return "Disco Ball Card"
        if int(dmg) == 1920:
            return "Voltekka Projectile Card"
        return None
    if slot_key == "CASSHAN":
        if int(dmg) == 1440:
            return "Brutal Ax"
        if int(dmg) in (960, 1040):
            return "Casshan Projectile Super Card"
        return None
    return None

def _append_projectile_super_card(hits: list, lookup: dict, char_damage_map: dict,
                                  slot_char_ids: dict[int, int] | None,
                                  addr: int, dmg: int, fmt: str, card_type: int) -> None:
    owner_base = _owning_chr_tbl(addr)
    if owner_base is None:
        return
    slot_key = _key_for_hit_addr(addr, slot_char_ids)
    active_keys = _active_keys_from_lookup(lookup)
    if active_keys and slot_key not in active_keys:
        return

    move = _projectile_super_case_label(slot_key, owner_base, addr, int(dmg), int(card_type))
    if move is None and slot_key is not None:
        mapped = char_damage_map.get(slot_key, {}).get(int(dmg), [])
        if mapped:
            move = mapped[0]
        elif int(dmg) < 500:
            return
        else:
            move = "Projectile Super Card"
    if move is None:
        if int(dmg) < 500:
            # Low-damage cards are usually tables; without ownership/context they
            # are too noisy to show.
            return
        move = "Projectile Super Card"

    hit = {
        "addr": int(addr),
        "dmg": int(dmg),
        "fmt": fmt,
        "dmg_write_addr": int(addr) + 0x04,
        **_OPCODE_HIT_FIELDS,
        "cluster": f"projectile super {card_type:04X} @ 0x{int(addr):08X}",
        "key": slot_key or "?",
        "move": move,
        "ps_card_type": int(card_type),
    }
    for name in _PROJECTILE_SUPER_FIELD_OFFSETS:
        hit[name] = _read_projectile_super_field(int(addr), name)
    # Make common template columns useful for these rows too.
    hit["lifetime"] = hit.get("ps_lifetime")
    hit["hitbox"] = hit.get("ps_scale")
    hit["speed"] = hit.get("ps_offset_x")
    hit["accel"] = hit.get("ps_offset_y")
    hits.append(hit)

def _append_super_hit(hits: list, lookup: dict, char_damage_map: dict,
                      slot_char_ids: dict[int, int] | None,
                      addr: int, dmg, fmt: str, dmg_write_addr: int,
                      cluster: str, extra: dict | None = None):
    hit_base = {
        "addr": addr,
        "dmg": dmg,
        "fmt": fmt,
        "dmg_write_addr": dmg_write_addr,
        **_OPCODE_HIT_FIELDS,
        "cluster": cluster,
    }
    if extra:
        hit_base.update(extra)
    if fmt in ("super_struct", "super_struct_card", "super_struct_card2", "super_beam_card"):
        ex_base = _super_ex_base(addr, fmt)
        ex03c = _read_f32(ex_base + _SUPER_EX_OFFSETS["ex03c"])
        def _read_named_super_field(name: str):
            off, typ = _SUPER_FIELD_OFFSETS.get(name, (None, None))
            if off is None:
                return "?"
            a = ex_base + int(off)
            if typ == "f32":
                return _read_f32(a)
            if typ == "u16":
                return _read_u16(a)
            if typ == "u32":
                return _read_u32(a)
            if typ == "u8":
                return _read_u8(a)
            return "?"

        for _name in _SUPER_FIELD_OFFSETS.keys():
            hit_base[_name] = _read_named_super_field(_name)

        hit_base.update({
            "ex03c": ex03c,
            "ex060": _read_f32(ex_base + _SUPER_EX_OFFSETS["ex060"]),
            "ex090": _read_f32(ex_base + _SUPER_EX_OFFSETS["ex090"]),
            "ex094": _read_f32(ex_base + _SUPER_EX_OFFSETS["ex094"]),
            "ex09c": _read_f32(ex_base + _SUPER_EX_OFFSETS["ex09c"]),
            "ex0d4": _read_f32(ex_base + _SUPER_EX_OFFSETS["ex0d4"]),
            "ex0e4": _read_f32(ex_base + _SUPER_EX_OFFSETS["ex0e4"]),
        })
        hit_base["hitbox"] = ex03c

    # Prefer slot-owned character resolution first.
    if isinstance(dmg, int):
        slot_key = _key_for_hit_addr(addr, slot_char_ids)
        active_keys = _active_keys_from_lookup(lookup)

        # Canonical super-beam rows need an owning-slot fallback before global
        # damage lookup.  If the slot is known and selected, use the character's
        # known beam-super name when its per-hit damage is not in the map.
        # Do not use selected-character fallback without slot ownership; that
        # would mislabel another slot's beam card when scanning all MEM2.
        if (
            fmt == "super_beam_card"
            and slot_key in _SUPER_BEAM_DEFAULT_MOVE_BY_KEY
            and (not active_keys or slot_key in active_keys)
        ):
            slot_moves = char_damage_map.get(slot_key, {}).get(dmg, [])
            if not slot_moves:
                hits.append({
                    **hit_base,
                    "key": slot_key,
                    "move": _SUPER_BEAM_DEFAULT_MOVE_BY_KEY[slot_key],
                })
                return
        if slot_key is not None:
            moves = char_damage_map.get(slot_key, {}).get(dmg, [])
            if moves and (not active_keys or slot_key in active_keys):
                for mv in moves:
                    hits.append({**hit_base, "key": slot_key, "move": mv})
                return
            if active_keys and slot_key not in active_keys:
                return
            # Same selected character, but damage is not in the name map. Keep it
            # as a candidate instead of misnaming it from another character.
            hits.append({**hit_base, "key": slot_key, "move": "Super Struct Candidate"})
            return

        # Fallback to the old global lookup only when ownership is unknown.
        if dmg in lookup:
            for key, mv in lookup[dmg]:
                hits.append({**hit_base, "key": key, "move": mv})
            return

    hits.append({**hit_base, "key": "?", "move": "Super Struct Candidate"})


def _super_ex_base(addr: int, fmt: str) -> int:
    if fmt == "super_struct":
        return addr - 0x09
    if fmt == "super_struct_card":
        return addr - 0x0E
    if fmt == "super_struct_card2":
        return addr - 0x0C
    if fmt == "super_beam_card":
        return addr
    return addr


def _super_probe_string(ex_base: int, max_items: int = 28) -> str:
    """
    Compact experimental dump for super structs.

    Reads 0x120 bytes from the inferred local base and reports plausible
    aligned f32/u16 values. This is intentionally noisy: the point is to
    expose candidate lifetime/radius/speed/scale fields quickly.
    """
    if rbytes is None:
        return "?"
    try:
        data = rbytes(ex_base, 0x120)
    except Exception:
        data = b""
    if not data or len(data) < 0x20:
        return "?"

    out = []

    def _add(label: str, val: str):
        if len(out) < max_items:
            out.append(f"{label}={val}")

    # Floats: good for radius/speed/scale/gravity/arc.
    for off in range(0, min(len(data) - 4, 0x120), 4):
        try:
            f = struct.unpack_from(">f", data, off)[0]
        except Exception:
            continue
        if not (-5000.0 <= f <= 5000.0):
            continue
        af = abs(f)
        # Skip pure zeros and denormal-looking trash; keep common constants.
        if af == 0.0 or (0.0 < af < 0.0001):
            continue
        if af <= 5000.0:
            _add(f"F{off:03X}", f"{f:.3g}")

    # U16s: good for damage/lifetime/type/id/flags.
    for off in range(0, min(len(data) - 2, 0x120), 2):
        try:
            u = struct.unpack_from(">H", data, off)[0]
        except Exception:
            continue
        if u in (0, 1, 4, 10, 12, 35, 255, 256, 512, 1024, 1200, 1600, 2000, 2400, 3200, 4800, 5200, 6000, 10000):
            _add(f"U{off:03X}", str(u))
        elif 2 <= u <= 240 and off % 2 == 0:
            # possible timers / small flags / type bytes packed into u16
            _add(f"U{off:03X}", str(u))

    return " ".join(out) if out else "?"

def _scan_super_struct_blocks(data: bytes, base_addr: int, hits: list,
                              lookup: dict, char_damage_map: dict,
                              slot_char_ids: dict[int, int] | None) -> None:
    beam_ranges: list[tuple[int, int]] = []

    def _inside_beam_range(addr: int) -> bool:
        try:
            a = int(addr)
        except Exception:
            return False
        for lo, hi in beam_ranges:
            if lo <= a < hi:
                return True
        return False

    # ── Pass 0: real super beam card ──────────────────────────────────────
    # Shape seen on Ryu Shinkuu / Chun Kikosho:
    #   base+0x08 = 0000000C
    #   base+0x0C = 00000023
    #   base+0x10 = damage u32
    # This keeps the row anchored to the real card base, not the shifted 0x23
    # signature used by the older exploratory scanner.
    pos = 0
    beam_sig = b"\x00\x00\x00\x0C\x00\x00\x00\x23"
    while True:
        sig_i = data.find(beam_sig, pos)
        if sig_i < 0:
            break
        pos = sig_i + 1
        idx = sig_i - 8
        if idx < 0:
            continue
        if idx + 0x158 > len(data):
            continue

        block_addr = base_addr + idx
        if _owning_chr_tbl(block_addr) is None:
            continue

        dmg = _read_u32_int(block_addr + 0x10)
        if not isinstance(dmg, int) or not (2 <= dmg <= 30000):
            continue

        # Soft validation: lifetime/count/particle fields should be sane, but
        # do not overfit because other supers may use different ids/counts.
        # Some valid cards use 0xFFFFFFFF / 0xFFFFFFFE as sentinel values.
        lifetime = _read_u32_int(block_addr + 0x84)
        hit_count = _read_u32_int(block_addr + 0x24)
        if isinstance(lifetime, int) and lifetime not in (0xFFFFFFFF, 0xFFFFFFFE) and lifetime > 0x10000:
            continue
        if isinstance(hit_count, int) and hit_count > 0x10000:
            continue

        beam_ranges.append((block_addr, block_addr + 0x160))

        _append_super_hit(
            hits, lookup, char_damage_map, slot_char_ids,
            block_addr, dmg, "super_beam_card", block_addr + 0x10,
            f"super beam @ 0x{block_addr:08X}",
            {
                "opcode": _read_u16_hex(block_addr),
                "param1": _read_u16_hex(block_addr + 2),
                "param2": _read_u16_hex(block_addr + 4),
                "param3": _read_u16_hex(block_addr + 6),
            }
        )

    # ── Pass 1: original sig  00 00 0C 00 00 00 23 00 ─────────────────────
    pos = 0
    while True:
        idx = data.find(SUPER_STRUCT_SIG, pos)
        if idx < 0:
            break
        pos = idx + 1

        block_addr = base_addr + idx
        if _owning_chr_tbl(block_addr) is None:
            continue
        if _inside_beam_range(block_addr):
            continue

        window_end = min(idx + SUPER_VERIFY_LOOK, len(data))
        window = data[idx:window_end]
        if not (SUPER_VERIFY_A in window or SUPER_VERIFY_B in window):
            continue

        dmg_addr = block_addr + _SUPER_STRUCT_DMG_OFF
        dmg = "?"
        if rbytes is not None:
            try:
                b = rbytes(dmg_addr, 2)
                if b and len(b) == 2:
                    dmg = (b[0] << 8) | b[1]
            except Exception:
                pass

        _append_super_hit(
            hits, lookup, char_damage_map, slot_char_ids,
            block_addr, dmg, "super_struct", dmg_addr,
            f"super struct @ 0x{block_addr:08X}"
        )

    # ── Pass 2: wildcard sig  ?? 23 00 00 00 [dmg hi] [dmg lo] 00 00 00 00
    pos = 0
    while True:
        idx = data.find(b"\x23\x00\x00\x00", pos)
        if idx < 0:
            break
        pos = idx + 1

        block_addr = base_addr + idx
        if _owning_chr_tbl(block_addr) is None:
            continue
        if _inside_beam_range(block_addr):
            continue

        dmg_off = idx + 4
        if dmg_off + 6 > len(data):
            continue

        dmg = (data[dmg_off] << 8) | data[dmg_off + 1]
        if not (2 <= dmg <= 20000):
            continue
        
        if data[dmg_off + 2 : dmg_off + 6] != b"\x00\x00\x00\x00":
            continue

        dmg_addr = base_addr + dmg_off

        _append_super_hit(
            hits, lookup, char_damage_map, slot_char_ids,
            block_addr, dmg, "super_struct", dmg_addr,
            f"super struct2 @ 0x{block_addr:08X}"
        )


    # ── Pass 3: compact projectile-super card  01 23 00 00 [dmg hi] [dmg lo]
    pos = 0
    while True:
        idx = data.find(b"\x01\x23\x00\x00", pos)
        if idx < 0:
            break
        pos = idx + 1

        block_addr = base_addr + idx
        if _owning_chr_tbl(block_addr) is None:
            continue
        if _inside_beam_range(block_addr):
            continue
        if idx + 0x60 > len(data):
            continue

        dmg = (data[idx + 4] << 8) | data[idx + 5]
        life = (data[idx + 8] << 8) | data[idx + 9]
        if not (2 <= dmg <= 30000 and 0 <= life <= 0x400):
            continue

        _append_projectile_super_card(
            hits, lookup, char_damage_map, slot_char_ids,
            block_addr, dmg, "projectile_super_card_0123", 0x0123
        )

    # ── Pass 4: compact projectile-super card  00 23 00 00 [dmg hi] [dmg lo] ...
    pos = 0
    while True:
        idx = data.find(b"\x00\x23\x00\x00", pos)
        if idx < 0:
            break
        pos = idx + 1

        if _looks_like_super_dispatch_0023(data, idx):
            # Super/action caller rows are handled by fd_super_integration.
            # They are not projectile payload cards.
            continue

        block_addr = base_addr + idx
        if _owning_chr_tbl(block_addr) is None:
            continue
        if _inside_beam_range(block_addr):
            continue
        if idx + 0x60 > len(data):
            continue

        dmg = (data[idx + 4] << 8) | data[idx + 5]
        life = (data[idx + 8] << 8) | data[idx + 9]
        count = (data[idx + 0x0C] << 8) | data[idx + 0x0D]
        # Low damage is important for Morrigan-style projectile-super tables,
        # but require sane card values so ordinary data does not flood the UI.
        if not (2 <= dmg <= 30000 and 0 <= life <= 0x400 and 0 <= count <= 0x400):
            continue

        _append_projectile_super_card(
            hits, lookup, char_damage_map, slot_char_ids,
            block_addr, dmg, "projectile_super_card", 0x0023
        )
 


def _scan_morrigan_finishing_shower_missile(data: bytes,
                                            base_addr: int,
                                            hits: list,
                                            active_keys,
                                            slot_char_ids: dict[int, int] | None,
                                            seen_fs_missiles: set[int]) -> None:
    "Find Morrigan Finishing Shower's live missile template.\n\n    This block is not the normal 00 00 dmg / 00 00 00 0C projectile-template\n    layout, and it is not the later 00/23 card-list table.  Operator pokes\n    confirmed this live record shape:\n\n      base + 0x06 = u16 damage, 0x0320 / 800\n      base + 0x30 = f32 radius\n      base + 0x34 = u32 FX / hit effect ID\n      base + 0x5F = u8 spawn origin / bone-ish selector\n      base + 0x90 = f32 travel speed\n      base + 0xD8 = f32 secondary radius / hitbox radius\n\n    Canonical example: base 0x908E2900, damage 0x908E2906, speed 0x908E2990, secondary radius 0x908E29D8.\n    "
    try:
        if "MORRIGAN" not in {str(k).upper() for k in (active_keys or [])}:
            return
    except Exception:
        return
    if not data:
        return

    # Damage lives inside this record, so do not include 0x0320 in the
    # signature.  Otherwise the row disappears after the operator edits damage.
    sig = b"\x00\x00\x01\x03"
    start = 0
    while True:
        off = data.find(sig, start)
        if off < 0:
            break
        if off + 0x94 > len(data):
            start = off + 1
            continue
        # Confirm the local invariant bytes from the known 0x908E2900 block.
        if data[off + 0x08:off + 0x10] != b"\x00\x00\x00\x10\x00\x00\x00\x01":
            start = off + 1
            continue
        a = base_addr + off
        start = off + 1
        if a in seen_fs_missiles:
            continue
        if slot_char_ids:
            try:
                if _key_for_hit_addr(a, slot_char_ids) != "MORRIGAN":
                    continue
            except Exception:
                continue
        # Soft validators from the current confirmed memory page.  Keep them
        # permissive so altered values do not make the row disappear mid-edit.
        try:
            dmg = _read_u16(a + 0x06)
            kb_x = _read_f32(a + 0x28)
            kb_y = _read_f32(a + 0x2C)
            radius = _read_f32(a + 0x30)
            fx = _read_u32(a + 0x34)
            spawn_origin = _read_u8(a + 0x5F)
            speed = _read_f32(a + 0x90)
            hitbox = _read_f32(a + 0xD8)
            pid = _read_u16(a + 0x52)
            ptype = _read_u8(a + 0x51)
        except Exception:
            dmg = speed = radius = fx = spawn_origin = pid = ptype = kb_x = kb_y = hitbox = "?"
        hits.append({
            "addr": a,
            "key": "MORRIGAN",
            "move": "Finishing Shower Missile",
            "dmg": dmg,
            "dmg_write_addr": a + 0x06,
            "fmt": "morrigan_fs_missile",
            "cluster": "confirmed live missile template",
            "proj_role": "confirmed",
            "type": ptype,
            "id": pid,
            "radius": radius,
            "fx": fx,
            "spawn_origin": spawn_origin,
            "speed": speed,
            "accel": "?",
            "kb_x": kb_x,
            "kb_y": kb_y,
            "hitbox": hitbox,
            "arc": "?",
            "arc2": "?",
        })
        seen_fs_missiles.add(a)

def _run_scan(active_keys, progress_cb, done_cb, show_unknowns: bool = True):
    if rbytes is None:
        done_cb([]); return

    proj_map = _load_map()
    id_map   = _load_ids()
    lookup   = _build_lookup(proj_map, active_keys)
    char_damage_map = _build_char_damage_map(proj_map)

    # Read live char_id per slot so zombie block scanner can gate on Frank.
    slot_char_ids = _read_slot_char_ids()

    total = SCAN_END - SCAN_START
    hits  = []
    addr  = SCAN_START
    seen_zombie_variants: set = set()
    seen_fs_missiles: set[int] = set()

    while addr < SCAN_END:
        sz = min(SCAN_BLOCK, SCAN_END - addr)
        try:
            data = rbytes(addr, sz)
        except Exception:
            data = b""

        if data:
            _scan_opcode_blocks(data, addr, hits, lookup, slot_char_ids, set(active_keys or set()))
            _scan_suffix_blocks(data, addr, hits, lookup, id_map, slot_char_ids, set(active_keys or set()))
            _scan_zombie_blocks(data, addr, hits, lookup, seen_zombie_variants, slot_char_ids)
            _scan_morrigan_finishing_shower_missile(data, addr, hits, active_keys, slot_char_ids, seen_fs_missiles)
            _scan_super_struct_blocks(data, addr, hits, lookup, char_damage_map, slot_char_ids)
        progress_cb((addr - SCAN_START + sz) / total * 100.0)
        addr += sz
    _annotate_clusters(hits)

    # Frank-specific zombie handling:
    # ignore Frank move-ID association except Zombie Fall,
    # then derive Attack/Spree from the discovered Fall row.
    hits = _apply_frank_zombie_anchor(hits)

    if not show_unknowns:
        hits = [h for h in hits if not (h.get("key") == "?" and h.get("move") == "Unknown")]

    _dump_hits(hits)
        

    
    done_cb(hits)


def _dump_hits(hits: list, context: int = 0x100):
    if rbytes is None or not hits:
        return
    super_hits = [
        h for h in hits
        if h.get("fmt") in ("super_struct", "super_struct_card", "super_struct_card2", "super_beam_card")
    ]
    if not super_hits:
        return
    try:
        dump_path = user_data_path("runtime", "proj_dump.bin")
        os.makedirs(os.path.dirname(dump_path), exist_ok=True)
        with open(dump_path, "wb") as f:
            for h in super_hits:
                base = max(h["addr"] - context, SCAN_START)
                size = min(context * 2, SCAN_END - base)
                try:
                    data = rbytes(base, size)
                except Exception:
                    data = b""
                f.write(base.to_bytes(4, "big"))
                f.write(h["addr"].to_bytes(4, "big"))
                f.write(len(data).to_bytes(4, "big"))
                f.write(data)
        print(f"[proj_scanner] dumped {len(super_hits)} super_struct hit(s) to {dump_path}")
    except Exception as e:
        print(f"[proj_scanner] dump failed: {e}")
# ---------------------------------------------------------------------------
# Column definitions
# kb_x / kb_y replace aerial_kb_x / aerial_kb_y
# cluster column added
# ---------------------------------------------------------------------------
_COLS = [
    ("address",  "Address",   None,       False),
    ("char",     "Char",      None,       False),
    ("move",     "Move",      None,       False),
    ("dmg",      "Damage",    "dmg",      False),
    ("cluster",  "Cluster",   None,       False),

    # Named super fields. These are editable for super rows.
    ("super_lifetime",     "Lifetime",     "super_lifetime", False),
    ("super_hit_count",    "Hit Count",    "super_hit_count", False),
    ("super_hit_interval", "Hit Interval", "super_hit_interval", False),
    ("super_particle_fx",  "Particle FX",  "super_particle_fx", False),
    ("super_spawn_bone",   "Spawn Bone",   "super_spawn_bone", False),
    ("super_hit_source",   "Hit Source",   "super_hit_source", False),
    ("super_air_kb_y",     "Beam Scale",   "super_air_kb_y", True),
    ("super_beam_width",   "Beam Width",   "super_beam_width", True),
    ("super_speed",        "Beam Speed",   "super_speed", True),
    ("super_accel",        "Beam Force",   "super_accel", True),
    ("super_radius",       "Hit Radius",   "super_radius", True),
    ("super_beam_visual",  "Beam Visual",  "super_beam_visual", False),
    ("super_final_damage",      "Final Damage",   "super_final_damage", False),
    ("super_final_lifetime",    "Final Life",     "super_final_lifetime", False),
    ("super_final_particle_fx", "Final FX",       "super_final_particle_fx", False),
    ("super_final_spawn_bone",  "Final Bone",     "super_final_spawn_bone", False),
    # Older exploratory slots kept for farming other supers.
    ("super_hit_react",    "HitReact",     "super_hit_react", False),
    ("super_life",         "OldLife?",     "super_life", False),
    ("super_speed_2",      "SuperSpeed2?", "super_speed_2", True),
    ("super_accel_b",      "AccelB?",      "super_accel_b", True),
    ("super_accel_c",      "AccelC?",      "super_accel_c", True),
    ("super_multihit_cap", "UnknownD8",    "super_multihit_cap", False),

    # Standard projectile/template fields, corrected from recomp mapping.
    ("motion_family",   "Motion +50",     "motion_family",   False),
    ("id",              "ID +52",         "id",              False),
    ("lifetime",        "Life +5A",       "lifetime",        False),
    ("radius",          "Base Scale +2C", "radius",          True),
    ("speed",           "Speed +80",      "speed",           True),
    ("speed_mult",      "Speed Mult +84", "speed_mult",      True),
    ("percent_scale",   "Percent +8C",    "percent_scale",   True),
    ("fixed_scale",     "Fixed +6E",      "fixed_scale",     False),
    ("kb_x",            "KB X +24",       "kb_x",            True),
    ("kb_y",            "KB Y +28",       "kb_y",            True),
    ("arc",             "Curve A +90",    "arc",             True),
    ("arc2",            "Curve B +94",    "arc2",            True),
    ("mode_a",          "Mode A +14",     "mode_a",          False),
    ("mode_b",          "Mode B +18",     "mode_b",          False),
    ("linked_resource", "Resource +48",   "linked_resource", False),
    ("flags_72",        "Flags +72",      "flags_72",        False),
    ("physics_tail_d4", "Physics +D4",    "physics_tail_d4", True),
    ("spawn_x",         "Spawn X",        "spawn_x",         False),
    ("spawn_y",         "Spawn Y",        "spawn_y",         True),

    # Compact projectile-super card fields.
    ("ps_lifetime", "PS Life +08", "ps_lifetime", False),
    ("ps_hit_count", "PS Hits +0C", "ps_hit_count", False),
    ("ps_mode", "PS Mode +10", "ps_mode", False),
    ("ps_emit_count", "PS Emit +18", "ps_emit_count", False),
    ("ps_interval", "PS Interval +1C", "ps_interval", False),
    ("ps_offset_x", "PS X +26", "ps_offset_x", True),
    ("ps_offset_y", "PS Y +2A", "ps_offset_y", True),
    ("ps_scale", "PS Scale +2E", "ps_scale", True),
    ("ps_particle_fx", "PS FX +34", "ps_particle_fx", False),
    ("ps_projectile_id", "PS ID +52", "ps_projectile_id", False),
    ("ps_spawn_bone", "PS Bone +5C", "ps_spawn_bone", False),
    ("fmt",      "Fmt",       None,       False),
]
_COL_IDS    = [c[0] for c in _COLS]
_FMT_COL_IDX = _COL_IDS.index("fmt")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
class ProjScannerWindow:
    def __init__(self, master, get_active_fn):
        self._get_active = get_active_fn
        self._scanning   = False
        self._keys: set  = set()
        self._addr_by_iid: dict[str, int] = {}
        self._dmg_write_by_iid: dict[str, int] = {}
        self._definition_by_iid: dict[str, dict] = {}
        self._show_unknowns = tk.BooleanVar(value=True)
        self._show_research = tk.BooleanVar(value=True)
        self._live_auto = tk.BooleanVar(value=True)
        self._live_refreshing = False
        self._live_queue: queue.Queue = queue.Queue()
        self._live_records: dict[int, dict] = {}
        self._live_iid_to_actor: dict[str, int] = {}
        self._live_detail_addr: dict[str, int] = {}
        self._closed = False

        self.root = tk.Toplevel(master)
        self.root.title("Projectile Definition and Runtime Inspector")
        self.root.geometry("1660x980")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._auto_scan()
        self.root.after(250, self._live_tick)

    def _build(self):
        top = ttk.Frame(self.root)
        top.pack(side="top", fill="x", padx=8, pady=6)
        self._active_var = tk.StringVar(value="Active: --")
        ttk.Label(top, textvariable=self._active_var).pack(side="left")
        ttk.Checkbutton(
            top, text="Show unknowns",
            variable=self._show_unknowns,
            command=self._start,
        ).pack(side="right", padx=8)
        self._scan_btn = ttk.Button(top, text="Rescan", command=self._start)
        self._scan_btn.pack(side="right", padx=4)

        self._prog = tk.DoubleVar()
        ttk.Progressbar(self.root, variable=self._prog, maximum=100).pack(
            fill="x", padx=8, pady=(0, 4))
        self._status = tk.StringVar(value="Scanning...")
        ttk.Label(self.root, textvariable=self._status, anchor="w").pack(fill="x", padx=8)

        # Keep definitions and live instances in one visible projectile workspace.
        # The vertical splitter lets the user give either half more room.
        body = ttk.Panedwindow(self.root, orient="vertical")
        body.pack(fill="both", expand=True, padx=8, pady=6)

        definitions_section = ttk.LabelFrame(body, text="Projectile definitions")
        live_section = ttk.LabelFrame(body, text="Live projectile instances and runtime fields")
        body.add(definitions_section, weight=3)
        body.add(live_section, weight=2)

        frame = ttk.Frame(definitions_section)
        frame.pack(fill="both", expand=True, padx=4, pady=4)

        self._tree = ttk.Treeview(frame, columns=_COL_IDS, show="headings", height=16)
        widths = {"address": 110, "char": 80, "move": 160, "dmg": 65,
                  "cluster": 220, "super_hit_react": 90, "super_life": 90,
                  "super_air_kb_y": 100, "super_speed": 110, "super_accel": 110,
                  "super_speed_2": 90, "super_accel_b": 90, "super_accel_c": 90,
                  "super_multihit_cap": 90, "super_radius": 105,
                  "motion_family": 90, "speed": 85, "speed_mult": 105,
                  "percent_scale": 95, "fixed_scale": 90, "arc": 85, "arc2": 85,
                  "mode_a": 95, "mode_b": 95, "linked_resource": 110,
                  "flags_72": 95, "physics_tail_d4": 100,
                  "ps_lifetime": 90, "ps_hit_count": 90, "ps_mode": 90,
                  "ps_emit_count": 90, "ps_interval": 100, "ps_offset_x": 90,
                  "ps_offset_y": 90, "ps_scale": 90, "ps_particle_fx": 90,
                  "ps_projectile_id": 90, "ps_spawn_bone": 90}
        self._sort_col = None
        self._sort_asc = True

        for col_id, header, _, _ in _COLS:
            self._tree.heading(col_id, text=header,
                command=lambda c=col_id: self._sort_by(c))
            self._tree.column(col_id, width=widths.get(col_id, 65), anchor="center")
        self._tree.column("move", anchor="w")
        self._tree.column("cluster", anchor="w")

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
        self._tree.bind("<<TreeviewSelect>>", self._on_definition_select)

        ttk.Label(
            definitions_section,
            text="Static 0xB0/0xD8 definitions are above. Their currently spawned runtime actors and collision data stay visible directly below.",
            foreground="gray",
        ).pack(anchor="w", padx=8, pady=(0, 4))

        self._build_live_tab(live_section)
        self._request_live_refresh()

    def _build_live_tab(self, parent):
        controls = ttk.Frame(parent)
        controls.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Checkbutton(controls, text="Auto refresh", variable=self._live_auto).pack(side="left")
        ttk.Checkbutton(
            controls, text="Show research fields", variable=self._show_research,
            command=self._refresh_selected_live_details,
        ).pack(side="left", padx=12)
        ttk.Button(controls, text="Refresh now", command=self._request_live_refresh).pack(side="right")
        self._live_status = tk.StringVar(value="Waiting for live projectile actors...")
        ttk.Label(controls, textvariable=self._live_status, anchor="w").pack(side="left", padx=12)
        self._live_group_var = tk.StringVar(
            value="Select a projectile definition above to jump to its matching live instance."
        )
        ttk.Label(
            parent, textvariable=self._live_group_var, anchor="w", foreground="gray"
        ).pack(fill="x", padx=12, pady=(0, 3))

        pane = ttk.Panedwindow(parent, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=3)
        pane.add(right, weight=4)

        live_cols = ("actor", "owner", "projectile_id", "damage", "position",
                     "movement", "priority", "quota", "shapes", "linked")
        self._live_tree = ttk.Treeview(left, columns=live_cols, show="headings", height=24)
        live_headers = {
            "actor": "Actor", "owner": "Owner", "projectile_id": "Proj ID",
            "damage": "Damage", "position": "Current Position",
            "movement": "Per-frame Movement", "priority": "Clash",
            "quota": "Quota", "shapes": "Shapes", "linked": "Linked Record",
        }
        live_widths = {
            "actor": 105, "owner": 65, "projectile_id": 75, "damage": 70,
            "position": 185, "movement": 185, "priority": 65, "quota": 70,
            "shapes": 100, "linked": 110,
        }
        for col in live_cols:
            self._live_tree.heading(col, text=live_headers[col])
            self._live_tree.column(col, width=live_widths[col], anchor="center")
        live_vsb = ttk.Scrollbar(left, orient="vertical", command=self._live_tree.yview)
        live_hsb = ttk.Scrollbar(left, orient="horizontal", command=self._live_tree.xview)
        self._live_tree.configure(yscrollcommand=live_vsb.set, xscrollcommand=live_hsb.set)
        self._live_tree.grid(row=0, column=0, sticky="nsew")
        live_vsb.grid(row=0, column=1, sticky="ns")
        live_hsb.grid(row=1, column=0, sticky="ew")
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)
        self._live_tree.bind("<<TreeviewSelect>>", self._on_live_select)
        self._live_tree.bind("<Button-3>", self._on_live_row_right_click)

        ttk.Label(right, text="Confirmed runtime fields", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", padx=4, pady=(0, 4))
        self._live_detail_tree = ttk.Treeview(
            right, columns=("offset", "value", "confidence"),
            show="tree headings", height=24,
        )
        self._live_detail_tree.heading("#0", text="Field")
        self._live_detail_tree.heading("offset", text="Offset")
        self._live_detail_tree.heading("value", text="Value")
        self._live_detail_tree.heading("confidence", text="Confidence")
        self._live_detail_tree.column("#0", width=230, anchor="w")
        self._live_detail_tree.column("offset", width=125, anchor="center")
        self._live_detail_tree.column("value", width=270, anchor="w")
        self._live_detail_tree.column("confidence", width=110, anchor="center")
        detail_vsb = ttk.Scrollbar(right, orient="vertical", command=self._live_detail_tree.yview)
        detail_hsb = ttk.Scrollbar(right, orient="horizontal", command=self._live_detail_tree.xview)
        detail_frame = ttk.Frame(right)
        detail_frame.pack(fill="both", expand=True)
        self._live_detail_tree.configure(yscrollcommand=detail_vsb.set, xscrollcommand=detail_hsb.set)
        self._live_detail_tree.grid(in_=detail_frame, row=0, column=0, sticky="nsew")
        detail_vsb.grid(in_=detail_frame, row=0, column=1, sticky="ns")
        detail_hsb.grid(in_=detail_frame, row=1, column=0, sticky="ew")
        detail_frame.grid_rowconfigure(0, weight=1)
        detail_frame.grid_columnconfigure(0, weight=1)
        self._live_detail_tree.bind("<Button-3>", self._on_live_detail_right_click)
        ttk.Label(
            right,
            text="Live data updates from the 0x1A4 actor pool and its linked collision record. Research fields remain raw when the exact designer-facing meaning is not proven.",
            foreground="gray", wraplength=650, justify="left",
        ).pack(anchor="w", padx=4, pady=(4, 0))

    def _on_close(self):
        self._closed = True
        snap = self._snapshot()
        try:
            owner_base = int(snap.get("base") or 0)
        except Exception:
            owner_base = 0
        _clear_live_profile_label(self.slot_label, owner_base)
        try:
            self.root.destroy()
        except Exception:
            pass

    def _on_projectile_tab_changed(self, _event=None):
        # Kept for compatibility with older callers. Live data is always visible now.
        self._request_live_refresh()

    def _live_tick(self):
        if self._closed:
            return
        while True:
            try:
                records, error = self._live_queue.get_nowait()
            except queue.Empty:
                break
            self._on_live_done(records, error)
        if self._live_auto.get():
            self._request_live_refresh()
        try:
            self.root.after(500, self._live_tick)
        except Exception:
            pass

    def _request_live_refresh(self):
        if self._closed or self._live_refreshing:
            return
        self._live_refreshing = True
        if not self._live_records:
            self._live_status.set("Reading live projectile actors...")
        threading.Thread(target=self._live_worker, daemon=True).start()

    def _live_worker(self):
        try:
            records = _collect_live_projectiles()
            error = None
        except Exception as exc:
            records = []
            error = str(exc)
        self._live_queue.put((records, error))

    def _on_live_done(self, records: list[dict], error: str | None):
        self._live_refreshing = False
        if self._closed:
            return
        if error:
            self._live_status.set(f"Live read failed: {error}")
            return

        selected_actor = None
        selection = self._live_tree.selection()
        if selection:
            selected_actor = self._live_iid_to_actor.get(selection[0])

        self._live_records = {int(r["actor"]): r for r in records}
        self._live_iid_to_actor.clear()
        for iid in self._live_tree.get_children(""):
            self._live_tree.delete(iid)

        selected_iid = None
        for record in records:
            actor = int(record["actor"])
            used, maximum = record.get("quota", (0, 0))
            shape_type = int(record.get("shape_type") or 0)
            shape_count = int(record.get("shape_count") or 0)
            values = (
                _fmt_hex(actor),
                record.get("owner", "?"),
                record.get("projectile_id", "?"),
                record.get("damage", "?"),
                _fmt_vec(*record.get("position", (0, 0, 0))),
                _fmt_vec(*record.get("velocity", (0, 0, 0))),
                record.get("clash_priority", "?"),
                f"{used}/{maximum}",
                f"{shape_count} {_LIVE_SHAPE_LABELS.get(shape_type, '?')}",
                _fmt_hex(record.get("linked", 0)),
            )
            iid = self._live_tree.insert("", "end", values=values)
            self._live_iid_to_actor[iid] = actor
            if actor == selected_actor:
                selected_iid = iid

        if selected_iid is None and records:
            selected_iid = self._live_tree.get_children("")[0]
        if selected_iid:
            self._live_tree.selection_set(selected_iid)
            self._live_tree.focus(selected_iid)
            self._live_tree.see(selected_iid)
            self._populate_live_details(self._live_iid_to_actor.get(selected_iid))
        else:
            self._populate_live_details(None)

        self._live_status.set(f"{len(records)} live projectile actor(s)")
        self._on_definition_select()

    @staticmethod
    def _coerce_projectile_id(value):
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        text = str(value).strip()
        if not text or text in {"?", "-", "None"}:
            return None
        try:
            return int(text, 0)
        except ValueError:
            try:
                return int(float(text))
            except (TypeError, ValueError):
                return None

    def _on_definition_select(self, _event=None):
        selection = self._tree.selection()
        if not selection:
            self._live_group_var.set(
                "Select a projectile definition above to jump to its matching live instance."
            )
            return
        definition = self._definition_by_iid.get(selection[0], {})
        projectile_id = self._coerce_projectile_id(definition.get("id"))
        if projectile_id is None:
            projectile_id = self._coerce_projectile_id(definition.get("ps_projectile_id"))
        move = str(definition.get("move") or "Unknown projectile")
        char = str(definition.get("key") or definition.get("char") or "?")
        if projectile_id is None:
            self._live_group_var.set(
                f"{char} | {move}: no decoded projectile ID is available for live matching."
            )
            return

        matches = []
        for iid, actor in self._live_iid_to_actor.items():
            record = self._live_records.get(actor, {})
            live_id = self._coerce_projectile_id(record.get("projectile_id"))
            if live_id == projectile_id:
                matches.append(iid)

        self._live_group_var.set(
            f"{char} | {move} | projectile ID {projectile_id}: {len(matches)} live instance(s)."
        )
        if not matches:
            return
        iid = matches[0]
        self._live_tree.selection_set(iid)
        self._live_tree.focus(iid)
        self._live_tree.see(iid)
        self._populate_live_details(self._live_iid_to_actor.get(iid))

    def _on_live_select(self, _event=None):
        selection = self._live_tree.selection()
        actor = self._live_iid_to_actor.get(selection[0]) if selection else None
        self._populate_live_details(actor)

    def _refresh_selected_live_details(self):
        self._on_live_select()

    def _populate_live_details(self, actor: int | None):
        for iid in self._live_detail_tree.get_children(""):
            self._live_detail_tree.delete(iid)
        self._live_detail_addr.clear()
        record = self._live_records.get(int(actor or 0))
        if not record:
            return

        group_nodes = {}
        for item in record.get("details", []):
            group = str(item.get("group") or "Other")
            if group == "Research" and not self._show_research.get():
                continue
            parent = group_nodes.get(group)
            if parent is None:
                parent = self._live_detail_tree.insert("", "end", text=group, open=True, values=("", "", ""))
                group_nodes[group] = parent
            iid = self._live_detail_tree.insert(
                parent, "end", text=str(item.get("label") or "?"),
                values=(item.get("offset", ""), item.get("value", "?"), item.get("confidence", "")),
            )
            address = item.get("address")
            if isinstance(address, int):
                self._live_detail_addr[iid] = address

    def _on_live_row_right_click(self, event):
        iid = self._live_tree.identify_row(event.y)
        actor = self._live_iid_to_actor.get(iid)
        if actor is None:
            return
        record = self._live_records.get(actor, {})
        linked = int(record.get("linked") or 0)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=f"Copy actor address ({_fmt_hex(actor)})", command=lambda: self._copy(_fmt_hex(actor)))
        if linked:
            menu.add_command(label=f"Copy linked record ({_fmt_hex(linked)})", command=lambda: self._copy(_fmt_hex(linked)))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_live_detail_right_click(self, event):
        iid = self._live_detail_tree.identify_row(event.y)
        if not iid or not self._live_detail_tree.parent(iid):
            return
        values = self._live_detail_tree.item(iid, "values")
        value = str(values[1]) if len(values) > 1 else ""
        address = self._live_detail_addr.get(iid)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Copy value", command=lambda: self._copy(value))
        if address is not None:
            menu.add_command(label=f"Copy address ({_fmt_hex(address)})", command=lambda: self._copy(_fmt_hex(address)))
            menu.add_command(label=f"View address ({_fmt_hex(address)})", command=lambda: self._show_address_info(address, f"Live field @ {_fmt_hex(address)}"))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _auto_scan(self):
        names = self._get_active()
        self._keys = {_NAME_TO_KEY[n] for n in names if n in _NAME_TO_KEY}
        self._active_var.set(f"Active: {', '.join(sorted(names)) or 'none'}")
        self._start()

    def _start(self):
        if self._scanning:
            return
        names = self._get_active()
        self._keys = {_NAME_TO_KEY[n] for n in names if n in _NAME_TO_KEY}
        self._active_var.set(f"Active: {', '.join(n for n in names if n) or 'none'}")
        if not self._keys:
            self._status.set("No active characters with known projectiles.")
            return
        self._scanning = True
        self._scan_btn.config(state="disabled")
        self._prog.set(0)
        self._addr_by_iid.clear()
        self._dmg_write_by_iid.clear()
        self._definition_by_iid.clear()
        for i in self._tree.get_children():
            self._tree.delete(i)
        self._status.set("Scanning MEM2...")
        threading.Thread(
            target=_run_scan,
            args=(set(self._keys), self._on_prog, self._on_done),
            kwargs={"show_unknowns": self._show_unknowns.get()},
            daemon=True,
        ).start()

    def _sort_by(self, col_id: str):
        if self._sort_col == col_id:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col_id
            self._sort_asc = True

        col_map = {c[0]: c[1] for c in _COLS}
        for cid, header, _, _ in _COLS:
            arrow = (" ▲" if self._sort_asc else " ▼") if cid == col_id else ""
            self._tree.heading(cid, text=col_map[cid] + arrow)

        items = [(self._tree.set(iid, col_id), iid)
                 for iid in self._tree.get_children("")]

        def sort_key(val):
            try:
                return (0, float(val))
            except Exception:
                return (1, str(val).lower())

        items.sort(key=lambda x: sort_key(x[0]), reverse=not self._sort_asc)
        for idx, (_, iid) in enumerate(items):
            self._tree.move(iid, "", idx)

    def _on_prog(self, pct: float):
        try:
            self.root.after(0, lambda: self._prog.set(pct))
        except Exception:
            pass

    def _on_done(self, hits: list):
        def _f():
            _TYPE_LABELS = {"3": "3:Linear", "4": "4:Physics",
                            3:   "3:Linear",   4: "4:Physics"}
            for h in hits:
                raw_type = h.get("type", "?")
                type_str = _TYPE_LABELS.get(raw_type, str(raw_type))
                raw_motion = h.get("motion_family", raw_type)
                motion_str = _TYPE_LABELS.get(raw_motion, str(raw_motion))
                display = dict(h)
                display["address"] = f"0x{h['addr']:08X}"
                display["char"] = h.get("key", "?")
                display["type"] = type_str
                display["motion_family"] = motion_str
                values = []
                for col_id, _header, _fkey, _is_float in _COLS:
                    value = display.get(col_id, "?")
                    if value is None:
                        value = "?"
                    values.append(value)
                iid = self._tree.insert("", "end", values=tuple(values))
                self._addr_by_iid[iid] = h["addr"]
                self._dmg_write_by_iid[iid] = h.get("dmg_write_addr", h["addr"] + 2)
                self._definition_by_iid[iid] = dict(h)
            self._scanning = False
            self._scan_btn.config(state="normal")
            self._prog.set(100)
            n_known = sum(1 for h in hits if h.get("key") != "?")
            n_unk   = sum(1 for h in hits if h.get("key") == "?" and h.get("move") == "Unknown")
            self._status.set(
                f"Done  -  {len(hits)} match(es): {n_known} attributed, {n_unk} unknown."
            )
        try:
            self.root.after(0, _f)
        except Exception:
            pass

    def _col_index(self, event) -> int:
        col = self._tree.identify_column(event.x)
        return int(col[1:]) - 1 if col else -1

    def _fmt_for_iid(self, iid: str) -> str:
        vals = self._tree.item(iid, "values")
        return str(vals[_FMT_COL_IDX]) if len(vals) > _FMT_COL_IDX else ""

    def _on_right_click(self, event):
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        addr = self._addr_by_iid.get(iid)
        if addr is None:
            return

        col_idx     = self._col_index(event)
        field_addr  = addr
        field_label = "base"
        if 0 <= col_idx < len(_COLS):
            col_id, header, fkey, _ = _COLS[col_idx]
            vals = self._tree.item(iid, "values")
            move_name = str(vals[2]) if len(vals) > 2 else ""

            if fkey == "dmg":
                field_addr = self._dmg_write_by_iid.get(iid, addr + _dmg_write_offset(self._fmt_for_iid(iid)))
                field_label = "dmg"
            elif move_name == "Zombie Fall" and fkey == "spawn_y":
                field_addr = addr + _FRANK_ZOMBIE_FALL_SPAWN_Y_OFF
                field_label = header

            elif move_name == "Zombie Attack" and fkey == "speed":
                field_addr = addr + _FRANK_ZOMBIE_ATTACK_SPEED_A
                field_label = header
            elif move_name == "Zombie Attack" and fkey == "accel":
                field_addr = addr + _FRANK_ZOMBIE_ATTACK_ACCEL_A
                field_label = header

            elif move_name == "Zombie Attack" and fkey == "spawn_x":
                field_addr = addr + _FRANK_ZOMBIE_ATTACK_SPAWN_X
                field_label = header
            elif fkey in _PROJECTILE_SUPER_FIELD_OFFSETS and self._fmt_for_iid(iid) in PROJECTILE_SUPER_FMTS:
                field_addr = addr + _PROJECTILE_SUPER_FIELD_OFFSETS[fkey][0]
                field_label = header
            elif fkey in _SUPER_FIELD_OFFSETS and self._fmt_for_iid(iid) in ("super_struct", "super_struct_card", "super_struct_card2", "super_beam_card"):
                ex_base = _super_ex_base(addr, self._fmt_for_iid(iid))
                field_addr = ex_base + _SUPER_FIELD_OFFSETS[fkey][0]
                field_label = header
            elif fkey == "hitbox" and self._fmt_for_iid(iid) in ("super_struct", "super_struct_card", "super_struct_card2", "super_beam_card"):
                ex_base = _super_ex_base(addr, self._fmt_for_iid(iid))
                field_addr = ex_base + _SUPER_EX_OFFSETS["ex03c"]
                field_label = header
            elif fkey in _SUPER_EX_OFFSETS and self._fmt_for_iid(iid) in ("super_struct", "super_struct_card", "super_struct_card2", "super_beam_card"):
                ex_base = _super_ex_base(addr, self._fmt_for_iid(iid))
                field_addr = ex_base + _SUPER_EX_OFFSETS[fkey]
            elif move_name.startswith("Zombie Spree "):
                if fkey == "kb_y":
                    if move_name == "Zombie Spree L":
                        field_addr = addr + _FRANK_ZOMBIE_SPREE_KBY_L
                    elif move_name == "Zombie Spree M":
                        field_addr = addr + _FRANK_ZOMBIE_SPREE_KBY_M
                    else:
                        field_addr = addr + _FRANK_ZOMBIE_SPREE_KBY_H
                    field_label = header

                elif fkey == "speed":
                    if move_name == "Zombie Spree L":
                        field_addr = addr + _FRANK_ZOMBIE_SPREE_ACCEL_L
                    elif move_name == "Zombie Spree M":
                        field_addr = addr + _FRANK_ZOMBIE_SPREE_ACCEL_M
                    else:
                        field_addr = addr + _FRANK_ZOMBIE_SPREE_ACCEL_H
                    field_label = header

                elif fkey == "arc":
                    if move_name == "Zombie Spree L":
                        field_addr = addr + _FRANK_ZOMBIE_SPREE_ARC_L
                    elif move_name == "Zombie Spree M":
                        field_addr = addr + _FRANK_ZOMBIE_SPREE_ARC_M
                    else:
                        field_addr = addr + _FRANK_ZOMBIE_SPREE_ARC_H
                    field_label = header

                elif fkey and fkey in FIELD_OFFSETS:
                    field_addr  = addr + FIELD_OFFSETS[fkey]
                    field_label = header

            elif fkey and fkey in FIELD_OFFSETS:
                field_addr  = addr + FIELD_OFFSETS[fkey]
                field_label = header

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=f"Copy base address (0x{addr:08X})",
                         command=lambda: self._copy(f"0x{addr:08X}"))
        menu.add_command(label=f"Go to base address (0x{addr:08X})",
                         command=lambda: self._show_address_info(addr, f"Base @ 0x{addr:08X}"))
        if field_addr != addr:
            menu.add_separator()
            menu.add_command(label=f"Copy {field_label} address (0x{field_addr:08X})",
                             command=lambda: self._copy(f"0x{field_addr:08X}"))
            menu.add_command(label=f"Go to {field_label} address (0x{field_addr:08X})",
                             command=lambda: self._show_address_info(field_addr, f"{field_label} @ 0x{field_addr:08X}"))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy(self, text: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._status.set(f"Copied {text}")

    def _show_address_info(self, addr: int, title: str):
        if rbytes is None:
            messagebox.showerror("Address", "dolphin_io.rbytes unavailable", parent=self.root)
            return

        line_size = 16
        context_lines = 8
        line_base = addr & ~(line_size - 1)
        start = max(SCAN_START, line_base - context_lines * line_size)
        total_lines = context_lines * 2 + 1
        size = total_lines * line_size
        try:
            data = rbytes(start, size) or b""
        except Exception as e:
            messagebox.showerror("Address", f"Read failed: {e}", parent=self.root)
            return

        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.geometry("780x440")

        txt = tk.Text(dlg, wrap="none", font=("Consolas", 10), bg="#101214", fg="#E8E8E8")
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert("end", "Right-click menu address view. '>>' marks the selected line.\n\n")

        current_line = (line_base - start) // line_size
        for i in range(total_lines):
            off = i * line_size
            chunk = data[off:off + line_size]
            a = start + off
            hx = " ".join(f"{b:02X}" for b in chunk)
            asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            prefix = ">>" if i == current_line else "  "
            txt.insert("end", f"{prefix} 0x{a:08X}: {hx:<47} {asc}\n")
        txt.config(state="disabled")

    def _on_double_click(self, event):
        col_idx = self._col_index(event)
        iid     = self._tree.identify_row(event.y)
        if not iid or col_idx < 0:
            return

        col_id, header, fkey, is_float = _COLS[col_idx]
        if col_id in ("address", "char", "move", "cluster", "fmt"):
            return

        addr = self._addr_by_iid.get(iid)
        if addr is None:
            return

        fmt = self._fmt_for_iid(iid)
        vals = self._tree.item(iid, "values")
        move_name = str(vals[2]) if len(vals) > 2 else ""

        if fkey == "dmg":
            write_addr = self._dmg_write_by_iid.get(iid, addr + _dmg_write_offset(fmt))
        elif move_name == "Zombie Fall" and fkey == "spawn_y":
            write_addr = addr + _FRANK_ZOMBIE_FALL_SPAWN_Y_OFF
        elif move_name == "Zombie Attack" and fkey == "speed":
            write_addr = addr + _FRANK_ZOMBIE_ATTACK_SPEED_A
        elif fkey == "hitbox" and fmt in ("super_struct", "super_struct_card", "super_struct_card2", "super_beam_card"):
            ex_base = _super_ex_base(addr, fmt)
            write_addr = ex_base + _SUPER_EX_OFFSETS["ex03c"]
        elif fkey in _SUPER_EX_OFFSETS and fmt in ("super_struct", "super_struct_card", "super_struct_card2", "super_beam_card"):
            ex_base = _super_ex_base(addr, fmt)
            write_addr = ex_base + _SUPER_EX_OFFSETS[fkey]
        elif move_name == "Zombie Attack" and fkey == "accel":
            write_addr = addr + _FRANK_ZOMBIE_ATTACK_ACCEL_A

        elif move_name == "Zombie Attack" and fkey == "spawn_x":
            write_addr = addr + _FRANK_ZOMBIE_ATTACK_SPAWN_X
        elif fkey in _SUPER_FIELD_OFFSETS and fmt in ("super_struct", "super_struct_card", "super_struct_card2", "super_beam_card"):
            ex_base = _super_ex_base(addr, fmt)
            write_addr = ex_base + _SUPER_FIELD_OFFSETS[fkey][0]
        elif move_name.startswith("Zombie Spree "):
            if fkey == "kb_y":
                if move_name == "Zombie Spree L":
                    write_addr = addr + _FRANK_ZOMBIE_SPREE_KBY_L
                elif move_name == "Zombie Spree M":
                    write_addr = addr + _FRANK_ZOMBIE_SPREE_KBY_M
                else:
                    write_addr = addr + _FRANK_ZOMBIE_SPREE_KBY_H

            elif fkey == "speed":
                if move_name == "Zombie Spree L":
                    write_addr = addr + _FRANK_ZOMBIE_SPREE_ACCEL_L
                elif move_name == "Zombie Spree M":
                    write_addr = addr + _FRANK_ZOMBIE_SPREE_ACCEL_M
                else:
                    write_addr = addr + _FRANK_ZOMBIE_SPREE_ACCEL_H

            elif fkey == "arc":
                if move_name == "Zombie Spree L":
                    write_addr = addr + _FRANK_ZOMBIE_SPREE_ARC_L
                elif move_name == "Zombie Spree M":
                    write_addr = addr + _FRANK_ZOMBIE_SPREE_ARC_M
                else:
                    write_addr = addr + _FRANK_ZOMBIE_SPREE_ARC_H

            elif fkey in FIELD_OFFSETS:
                write_addr = addr + FIELD_OFFSETS[fkey]
            else:
                return
            
        elif fkey in FIELD_OFFSETS:
            write_addr = addr + FIELD_OFFSETS[fkey]
        else:
            return

        vals    = self._tree.item(iid, "values")
        cur_val = vals[col_idx]

        new_val = simpledialog.askstring(
            f"Edit {header}",
            f"Move: {vals[2]}\nAddress: 0x{write_addr:08X}\nCurrent: {cur_val}\n\nNew value:",
            parent=self.root, initialvalue=str(cur_val),
        )
        if new_val is None:
            return
        new_val = new_val.strip()

        super_field_type = None
        if fkey in _SUPER_FIELD_OFFSETS:
            super_field_type = _SUPER_FIELD_OFFSETS[fkey][1]
        static_field_type = _STATIC_FIELD_TYPES.get(fkey)

        if is_float:
            try:
                fval = float(new_val)
            except ValueError:
                messagebox.showerror("Invalid", f"'{new_val}' is not a valid float.",
                                     parent=self.root)
                return
            if _write_f32(write_addr, fval):
                self._tree.set(iid, col_id, f"{fval:.4f}")
                self._status.set(f"Wrote {fval} to 0x{write_addr:08X}")
            else:
                messagebox.showerror("Write failed", "Could not write to Dolphin.",
                                     parent=self.root)
        else:
            try:
                ival = int(new_val, 16) if new_val.startswith("0x") else int(new_val)
            except ValueError:
                messagebox.showerror("Invalid", f"'{new_val}' is not a valid number.",
                                     parent=self.root)
                return
            if fkey == "dmg":
                if not (0 <= ival <= 0xFFFFFFFF):
                    messagebox.showerror("Out of range", "Damage must be 0–4294967295.",
                                         parent=self.root)
                    return
            else:
                if fkey == "spawn_x":
                    if not (0 <= ival <= 0xFF):
                        messagebox.showerror("Out of range", "Spawn X must be 0–255.",
                                             parent=self.root)
                        return
                elif super_field_type == "u32" or static_field_type == "u32":
                    if not (0 <= ival <= 0xFFFFFFFF):
                        messagebox.showerror("Out of range", "Value must be 0–4294967295.",
                                             parent=self.root)
                        return
                elif static_field_type == "u8":
                    if not (0 <= ival <= 0xFF):
                        messagebox.showerror("Out of range", "Value must be 0–255.",
                                             parent=self.root)
                        return
                elif not (0 <= ival <= 0xFFFF):
                    messagebox.showerror("Out of range", "Value must be 0–65535.",
                                         parent=self.root)
                    return

            if fkey == "dmg":
                resolved = self._dmg_write_by_iid.get(iid)
                fallback = addr + _dmg_write_offset(fmt)

                if fmt == "super_beam_card" and resolved is not None:
                    ok = _write_u32(resolved, ival)
                elif fmt in ("super_struct", "super_struct_card", "super_struct_card2") and resolved is not None:
                    if not (0 <= ival <= 0xFFFF):
                        messagebox.showerror("Out of range", "Damage must be 0–65535.",
                                            parent=self.root)
                        return
                    ok = _write_u16(resolved, ival)

                elif resolved and resolved != fallback:
                    ok = _write_u32(resolved, ival)

                else:
                    ok = _write_dmg(addr, ival, fmt)
            elif fkey == "spawn_x":
                ok = bool(wbytes(write_addr, bytes([ival]))) if wbytes is not None else False
            elif super_field_type == "u32" or static_field_type == "u32":
                ok = _write_u32(write_addr, ival)
            elif static_field_type == "u8":
                ok = bool(wbytes(write_addr, bytes([ival & 0xFF]))) if wbytes is not None else False
            else:
                ok = _write_u16(write_addr, ival)
            if ok:
                self._tree.set(iid, col_id, ival)
                self._status.set(f"Wrote {ival} to 0x{write_addr:08X}")
            else:
                messagebox.showerror("Write failed", "Could not write to Dolphin.",
                                     parent=self.root)



# ---------------------------------------------------------------------------
# Persistent live correlation evidence
# ---------------------------------------------------------------------------
_PROJECTILE_CORRELATION_FILE = "projectile_live_correlations.json"
_PROJECTILE_CORRELATION_LOCK = threading.Lock()

# The Profile Monitor already performs the authoritative live projectile-to-row
# match. Publish that exact row label so the main GUI and in-game overlay can
# consume the same strength-specific name instead of rebuilding a generic name
# from the fighter action ID.
_LIVE_PROFILE_LABEL_LOCK = threading.RLock()
_LIVE_PROFILE_LABELS_BY_SLOT: dict[str, dict] = {}
_LIVE_PROFILE_LABELS_BY_BASE: dict[int, dict] = {}
# A profile row is authoritative only while the Profile Monitor is actively
# refreshing that projectile. The monitor polls every 55 ms, so 0.20 seconds
# gives ample scheduling tolerance without allowing a suffix to leak onto the
# next move.
_LIVE_PROFILE_LABEL_LATCH_SECONDS = 0.20
_LIVE_PROFILE_ACTIVE_SECONDS = 0.20

# The profiler window and the Pygame loop normally share one interpreter, but
# the visible HUD is fed through a separate overlay process. Keep a tiny
# on-disk mailbox as the final source of truth so the exact LIVE profiler row
# survives module duplication, thread timing, and the overlay process boundary.
_LIVE_PROFILE_MAILBOX_FILE = user_data_path("combat", "live_profile_labels.json")
_LIVE_PROFILE_MAILBOX_WRITE_AT = 0.0
_LIVE_PROFILE_MAILBOX_READ_MTIME = -1.0
_LIVE_PROFILE_MAILBOX_READ_CACHE: dict[str, dict] = {}


def _write_live_profile_mailbox(force: bool = False) -> None:
    global _LIVE_PROFILE_MAILBOX_WRITE_AT
    global _LIVE_PROFILE_MAILBOX_READ_MTIME, _LIVE_PROFILE_MAILBOX_READ_CACHE
    now = time.monotonic()
    if not force and now - _LIVE_PROFILE_MAILBOX_WRITE_AT < 0.045:
        return
    _LIVE_PROFILE_MAILBOX_WRITE_AT = now
    try:
        with _LIVE_PROFILE_LABEL_LOCK:
            slots = {str(k): dict(v) for k, v in _LIVE_PROFILE_LABELS_BY_SLOT.items()}
        payload = {"version": 2, "slots": slots, "written_wall_time": time.time()}
        os.makedirs(os.path.dirname(_LIVE_PROFILE_MAILBOX_FILE), exist_ok=True)
        tmp = _LIVE_PROFILE_MAILBOX_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, _LIVE_PROFILE_MAILBOX_FILE)
        # Keep this process's reader cache synchronized even when two writes
        # land within the filesystem timestamp resolution window.
        _LIVE_PROFILE_MAILBOX_READ_CACHE = {str(k): dict(v) for k, v in slots.items()}
        try:
            _LIVE_PROFILE_MAILBOX_READ_MTIME = os.path.getmtime(_LIVE_PROFILE_MAILBOX_FILE)
        except OSError:
            _LIVE_PROFILE_MAILBOX_READ_MTIME = -1.0
    except Exception:
        pass


def _clear_live_profile_label(slot_label: str, owner_base: int = 0) -> None:
    """Clear one slot immediately when its live projectile disappears."""
    slot_key = str(slot_label or "")
    changed = False
    with _LIVE_PROFILE_LABEL_LOCK:
        if slot_key and _LIVE_PROFILE_LABELS_BY_SLOT.pop(slot_key, None) is not None:
            changed = True
        if owner_base and _LIVE_PROFILE_LABELS_BY_BASE.pop(int(owner_base), None) is not None:
            changed = True
        if slot_key:
            for base, item in list(_LIVE_PROFILE_LABELS_BY_BASE.items()):
                if str((item or {}).get("slot_label") or "") == slot_key:
                    _LIVE_PROFILE_LABELS_BY_BASE.pop(base, None)
                    changed = True
    # Force the empty mailbox write. Without this, the overlay process can keep
    # reading the final Blaster 2/3 row until the normal write throttle expires.
    if changed or slot_key:
        _write_live_profile_mailbox(force=True)


def _read_live_profile_mailbox() -> dict[str, dict]:
    global _LIVE_PROFILE_MAILBOX_READ_MTIME, _LIVE_PROFILE_MAILBOX_READ_CACHE
    try:
        mtime = os.path.getmtime(_LIVE_PROFILE_MAILBOX_FILE)
    except OSError:
        return {}
    if mtime == _LIVE_PROFILE_MAILBOX_READ_MTIME:
        return {str(k): dict(v) for k, v in _LIVE_PROFILE_MAILBOX_READ_CACHE.items()}
    try:
        with open(_LIVE_PROFILE_MAILBOX_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        slots = raw.get("slots", {}) if isinstance(raw, dict) else {}
        clean = {str(k): dict(v) for k, v in slots.items() if isinstance(v, dict)}
    except Exception:
        return {str(k): dict(v) for k, v in _LIVE_PROFILE_MAILBOX_READ_CACHE.items()}
    _LIVE_PROFILE_MAILBOX_READ_MTIME = mtime
    _LIVE_PROFILE_MAILBOX_READ_CACHE = clean
    return {str(k): dict(v) for k, v in clean.items()}


def _usable_profile_label(value) -> str:
    label = str(value or "").strip()
    low = label.lower()
    if not label or low in {"unknown", "none", "-", "--", "projectile"}:
        return ""
    if low.startswith(("unmatched projectile", "projectile 0x", "projectile @")):
        return ""
    return label


def _publish_live_profile_label(slot_label: str, snap: dict, record: dict,
                                definition: dict, label: str) -> None:
    label = _usable_profile_label(label)
    if not label:
        return
    try:
        owner_base = int(record.get("owner_pointer") or snap.get("base") or 0)
    except Exception:
        owner_base = 0
    try:
        action_id = int(record.get("owner_action_id") or snap.get("mv_id_display") or 0)
    except Exception:
        action_id = 0
    try:
        projectile_id = int(record.get("projectile_id") or 0)
    except Exception:
        projectile_id = 0
    try:
        static_addr = int((definition or {}).get("addr") or 0)
    except Exception:
        static_addr = 0
    payload = {
        "label": label,
        "slot_label": str(slot_label or ""),
        "owner_base": owner_base,
        "action_id": action_id,
        "projectile_id": projectile_id,
        "static_addr": static_addr,
        "seen_at": time.monotonic(),
        "seen_wall_time": time.time(),
        "active": True,
    }
    with _LIVE_PROFILE_LABEL_LOCK:
        if slot_label:
            _LIVE_PROFILE_LABELS_BY_SLOT[str(slot_label)] = payload
        if owner_base:
            _LIVE_PROFILE_LABELS_BY_BASE[owner_base] = payload
    _write_live_profile_mailbox()


def get_live_profile_move_record(slot_label: str | None = None, owner_base: int = 0,
                                 max_age: float | None = None) -> dict | None:
    """Return the freshest Profile Monitor record for one fighter.

    This deliberately does not require the fighter's current action ID to
    match. The projectile can be resolved a few frames after the fighter has
    already returned to idle or started another move. Consumers that update a
    current live label should check the action ID themselves, while history
    consumers can use the record to correct the earlier move event.
    """
    age_limit = _LIVE_PROFILE_LABEL_LATCH_SECONDS if max_age is None else max(0.0, float(max_age))
    now = time.monotonic()
    candidates: list[dict] = []
    with _LIVE_PROFILE_LABEL_LOCK:
        if slot_label:
            item = _LIVE_PROFILE_LABELS_BY_SLOT.get(str(slot_label))
            if item:
                candidates.append(dict(item))
        if owner_base:
            item = _LIVE_PROFILE_LABELS_BY_BASE.get(int(owner_base))
            if item and item not in candidates:
                candidates.append(dict(item))

    # Also consume the exact row published by the profiler mailbox. This is
    # intentionally checked even when an in-memory candidate exists, then the
    # freshest record wins below.
    mailbox = _read_live_profile_mailbox()
    if slot_label:
        item = mailbox.get(str(slot_label))
        if item:
            candidates.append(dict(item))
    if owner_base:
        for item in mailbox.values():
            try:
                same_owner = int(item.get("owner_base") or 0) == int(owner_base)
            except Exception:
                same_owner = False
            if same_owner:
                candidates.append(dict(item))

    valid: list[dict] = []
    wall_now = time.time()
    for item in candidates:
        try:
            seen_wall = float(item.get("seen_wall_time") or 0.0)
        except Exception:
            seen_wall = 0.0
        if seen_wall:
            age = max(0.0, wall_now - seen_wall)
        else:
            age = now - float(item.get("seen_at") or 0.0)
        if age > age_limit:
            continue
        if item.get("active") is False:
            continue
        label = _usable_profile_label(item.get("label"))
        if not label:
            continue
        item["label"] = label
        item["age"] = max(0.0, age)
        valid.append(item)

    if not valid:
        return None
    valid.sort(
        key=lambda item: (
            float(item.get("seen_wall_time") or 0.0),
            float(item.get("seen_at") or 0.0),
        ),
        reverse=True,
    )
    return dict(valid[0])


def get_live_profile_move_label(slot_label: str | None = None, owner_base: int = 0,
                                action_id: int = 0, max_age: float | None = None) -> str | None:
    """Return the exact live label selected by the Profile Monitor.

    The match is keyed by both HUD slot and fighter pointer. A brief latch keeps
    the strength name visible after the projectile actor disappears.
    """
    item = get_live_profile_move_record(
        slot_label=slot_label,
        owner_base=owner_base,
        max_age=max_age,
    )
    if not item:
        return None

    age = float(item.get("age") or 0.0)
    item_action = int(item.get("action_id") or 0)
    if action_id and item_action and int(action_id) != item_action and age > 0.35:
        # While the projectile actor is actively refreshing the registry, its
        # exact label remains valid even if the fighter has already returned to
        # idle. After the live refresh stops, require the action IDs to match so
        # an old projectile cannot rename an unrelated later move.
        return None
    return _usable_profile_label(item.get("label")) or None



_PROFILE_SUFFIX_RE = re.compile(
    r"^(.*?)(?:\s+|\s*\()((?:level|lvl|lv|l)\s*[-_:]?\s*[1-9][0-9]*|[1-9][0-9]*|[abc]|[lmh])\)?\s*$",
    re.IGNORECASE,
)


def _profile_label_norm(value) -> str:
    text = str(value or "").strip().lower().replace("_", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _split_profile_variant(label: str) -> tuple[str, str]:
    text = str(label or "").strip()
    match = _PROFILE_SUFFIX_RE.match(text)
    if not match:
        return text, ""
    core = str(match.group(1) or "").strip(" -_/()[]")
    suffix = re.sub(r"\s+", "", str(match.group(2) or "").strip())
    return core, suffix


def profile_labels_share_family(left: str, right: str) -> bool:
    """Return True when two labels describe the same projectile move family."""
    left_core, _ = _split_profile_variant(left)
    right_core, _ = _split_profile_variant(right)
    left_norm = _profile_label_norm(left_core)
    right_norm = _profile_label_norm(right_core)
    if not left_norm or not right_norm:
        return False
    if left_norm in right_norm or right_norm in left_norm:
        return True
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    shared = left_tokens.intersection(right_tokens)
    # Ignore generic words that can occur in unrelated move labels.
    shared.difference_update({"hyper", "super", "projectile", "shot", "attack"})
    return bool(shared)


def resolve_profile_precedence_label(current_label: str, profile_label: str) -> str:
    """Return the label the GUI and overlay should display.

    The Profile Monitor is authoritative whenever it has a differing live row.
    If the profiler uses a shortened family name, such as ``Blaster 2``, keep
    the GUI's full family name and append the profiler's variant, producing
    ``Hyper Zero Blaster 2``. Otherwise use the profiler label exactly.
    """
    current = str(current_label or "").strip()
    profile = _usable_profile_label(profile_label)
    if not profile:
        return current
    if not current:
        return profile

    current_norm = _profile_label_norm(current)
    profile_norm = _profile_label_norm(profile)
    if not current_norm or current_norm == profile_norm:
        return profile
    if current_norm in profile_norm:
        return profile

    profile_core, profile_suffix = _split_profile_variant(profile)
    current_core, _current_suffix = _split_profile_variant(current)
    profile_core_norm = _profile_label_norm(profile_core)
    current_core_norm = _profile_label_norm(current_core)

    current_tokens = set(current_core_norm.split())
    profile_tokens = set(profile_core_norm.split())
    shared_tokens = current_tokens.intersection(profile_tokens)
    same_family = bool(
        profile_core_norm
        and current_core_norm
        and (
            profile_core_norm in current_core_norm
            or current_core_norm in profile_core_norm
            or shared_tokens
        )
    )

    if profile_suffix and same_family:
        return f"{current_core or current} {profile_suffix}".strip()

    return profile


def apply_live_profile_label_override(slot_label: str, snap: dict) -> str | None:
    """Create one authoritative final label for both GUI and overlay.

    The normal move lookup remains the fallback. While the Profile Monitor has
    a LIVE row for the same move family, that row takes precedence. The exact
    same ``final_move_label`` is then serialized to the overlay, so neither
    renderer performs another independent name lookup.
    """
    if not isinstance(snap, dict):
        return None

    # Snapshots can be reused. Remove only the previous profile-owned fields,
    # then restore the ordinary move label before checking the current LIVE row.
    previous_source = str(snap.get("move_label_source") or "")
    normal_label = str(snap.get("mv_label_base") or snap.get("mv_label") or "").strip()
    fallback_label = str(snap.get("mv_label_display") or normal_label).strip()
    if previous_source == "profile_monitor":
        fallback_label = normal_label
        if normal_label:
            snap["mv_label_display"] = normal_label
        else:
            snap.pop("mv_label_display", None)

    for key in (
        "profile_live_label", "profile_resolved_label", "profile_live_active",
        "profile_history_label", "profile_history_action_id",
        "profile_history_projectile_id", "profile_history_static_addr",
        "profile_history_age", "profile_history_seen_wall_time",
        "profile_history_token", "mv_label_profile", "profile_label_override",
        "mv_id_label_display",
    ):
        snap.pop(key, None)
    snap.pop("move_label_source", None)
    snap["final_move_label"] = fallback_label

    try:
        owner_base = int(snap.get("base") or 0)
    except Exception:
        owner_base = 0
    try:
        current_action_id = int(snap.get("mv_id_display") or 0)
    except Exception:
        current_action_id = 0

    profile_record = get_live_profile_move_record(
        slot_label=str(slot_label or ""),
        owner_base=owner_base,
        max_age=_LIVE_PROFILE_LABEL_LATCH_SECONDS,
    )
    if not profile_record:
        return None

    profile_label = _usable_profile_label(profile_record.get("label"))
    if not profile_label:
        return None

    try:
        profile_action_id = int(profile_record.get("action_id") or 0)
    except Exception:
        profile_action_id = 0
    try:
        profile_projectile_id = int(profile_record.get("projectile_id") or 0)
    except Exception:
        profile_projectile_id = 0
    try:
        profile_static_addr = int(profile_record.get("static_addr") or 0)
    except Exception:
        profile_static_addr = 0

    # The main loop supplies the best full family label it has seen for this
    # profiler row. For Blaster 3 this is Hyper Zero Blaster, producing the
    # final authoritative text Hyper Zero Blaster 3.
    profile_family_base = str(snap.get("profile_action_base_label") or normal_label).strip()
    profile_resolved = resolve_profile_precedence_label(profile_family_base, profile_label)
    if not profile_resolved:
        profile_resolved = profile_label

    snap["profile_live_label"] = profile_label
    snap["profile_resolved_label"] = profile_resolved
    snap["profile_live_active"] = True
    snap["profile_history_label"] = profile_label
    snap["profile_history_action_id"] = profile_action_id
    snap["profile_history_projectile_id"] = profile_projectile_id
    snap["profile_history_static_addr"] = profile_static_addr
    snap["profile_history_age"] = float(profile_record.get("age") or 0.0)
    snap["profile_history_seen_wall_time"] = float(profile_record.get("seen_wall_time") or 0.0)
    snap["profile_history_token"] = (
        f"{profile_action_id}:{profile_projectile_id}:{profile_static_addr}:"
        f"{float(profile_record.get('seen_at') or 0.0):.6f}:"
        f"{_profile_label_norm(profile_label)}"
    )

    # Only replace the CURRENT GUI/MOVE label when it is still the same move.
    # History can still use profile_resolved_label to repair the earlier chip.
    same_action = bool(current_action_id and profile_action_id and current_action_id == profile_action_id)
    same_family = profile_labels_share_family(normal_label, profile_label)
    if not (same_action or same_family):
        return None

    current_resolved = resolve_profile_precedence_label(normal_label, profile_label)
    if not current_resolved:
        return None

    aliases = list(snap.get("mv_label_aliases") or [])
    aliases.extend([normal_label, fallback_label, profile_label, current_resolved])
    deduped = []
    seen = set()
    for alias in aliases:
        alias = str(alias or "").strip()
        key = _profile_label_norm(alias)
        if alias and key not in seen:
            seen.add(key)
            deduped.append(alias)

    snap["mv_label_display"] = current_resolved
    snap["final_move_label"] = current_resolved
    snap["mv_label_profile"] = profile_label
    snap["mv_label_aliases"] = deduped
    snap["move_label_source"] = "profile_monitor"
    snap["profile_label_override"] = True
    return current_resolved


def _load_projectile_correlations() -> dict:
    path = user_data_path(_PROJECTILE_CORRELATION_FILE)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_projectile_correlations(payload: dict) -> None:
    path = user_data_path(_PROJECTILE_CORRELATION_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _best_correlated_move_name(char_key: str | None, projectile_id: int | None,
                               damage: int | None = None) -> str | None:
    if not char_key or projectile_id is None:
        return None
    payload = _load_projectile_correlations()
    char = (payload.get("characters") or {}).get(str(char_key)) or {}
    observations = char.get("observations") or {}
    candidates = []
    for obs in observations.values():
        try:
            if int(obs.get("projectile_id")) != int(projectile_id):
                continue
        except Exception:
            continue
        if damage not in (None, 0):
            try:
                if int(obs.get("damage") or 0) not in (0, int(damage)):
                    continue
            except Exception:
                pass
        for move in (obs.get("moves") or {}).values():
            label = str(move.get("move_label") or "").strip()
            if not label:
                continue
            try:
                count = int(move.get("count") or 0)
            except Exception:
                count = 0
            candidates.append((count, label))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1].lower()))
    return candidates[0][1]


# ---------------------------------------------------------------------------
# Per-character-slot projectile profile monitor
# ---------------------------------------------------------------------------
_MONITOR_OWNER_BY_SLOT = {
    "P1-C1": "P1-C1",
    "P1-C2": "P1-C2",
    "P2-C1": "P2-C1",
    "P2-C2": "P2-C2",
}
_MONITOR_SLOT_INDEX = {
    "P1-C1": 0,
    "P1-C2": 1,
    "P2-C1": 2,
    "P2-C2": 3,
}
_MONITOR_STATIC_FIELDS = (
    ("Damage", "dmg", None, "damage"),
    ("Motion family", "motion_family", 0x050, "u16"),
    ("Projectile definition ID", "id", 0x052, "u16"),
    ("Lifetime / active value", "lifetime", 0x05A, "u16"),
    ("Base collision scale", "radius", 0x02C, "f32"),
    ("Travel speed", "speed", 0x080, "f32"),
    ("Speed / time multiplier", "speed_mult", 0x084, "f32"),
    ("Percentage scale", "percent_scale", 0x08C, "f32"),
    ("Fixed scale", "fixed_scale", 0x06E, "u16"),
    ("Knockback X", "kb_x", 0x024, "f32"),
    ("Knockback Y", "kb_y", 0x028, "f32"),
    ("Curve / gravity A", "arc", 0x090, "f32"),
    ("Curve / gravity B", "arc2", 0x094, "f32"),
    ("Mode / count A", "mode_a", 0x014, "u32"),
    ("Mode / flags B", "mode_b", 0x018, "u32"),
    ("Linked resource / script", "linked_resource", 0x048, "u32"),
    ("Flags / sentinel", "flags_72", 0x072, "u32"),
    ("Physics tail", "physics_tail_d4", 0x0D4, "f32"),
)


def _monitor_norm_name(value) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())

def _monitor_name_token(value) -> str:
    """Aggressive move-name key used only for static-definition correlation.

    This intentionally treats names such as ``Mega Buster`` and ``Megabuster``
    as the same projectile without changing the displayed label.
    """
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _monitor_snap_char(snap: dict | None) -> tuple[int | None, str, str | None]:
    snap = dict(snap or {})
    cid = None
    for field in ("id", "char_id", "csv_char_id"):
        try:
            value = int(snap.get(field) or 0)
        except Exception:
            value = 0
        if value:
            cid = value
            break
    name = str(snap.get("char_name") or snap.get("name") or CHAR_ID_TO_KEY.get(cid or -1) or "Unknown")
    key = _projectile_key_from_char_id(cid)
    if not key:
        key = _NAME_TO_KEY.get(name)
    return cid, name, key


def _run_monitor_slot_scan(slot_label: str, key: str, fighter_base: int | None,
                           char_id: int | None, progress_cb, done_cb) -> None:
    """Scan the exact character module attached to the selected fighter.

    This no longer assumes that sorted chr_tbl regions line up with HUD slot
    order. The normal scanner already knows how to resolve and validate the
    live fighter's chr_tbl pointer, so use that same concrete ownership proof.
    """
    if rbytes is None:
        done_cb([])
        return

    base = 0
    if fighter_base:
        try:
            from tvcgui.tools.scanners.normal_scanner import resolve_chr_tbl_from_live_memory
            base = int(resolve_chr_tbl_from_live_memory(int(fighter_base)) or 0)
        except Exception:
            base = 0

    if not base:
        slot_index = _MONITOR_SLOT_INDEX.get(str(slot_label))
        bases = _current_chr_tbl_bases()
        if slot_index is None or slot_index >= len(bases):
            done_cb([])
            return
        base = int(bases[slot_index])

    size = 0x90000
    try:
        data = rbytes(base, size) or b""
    except Exception:
        data = b""
    if not data:
        done_cb([])
        return

    selected = {str(key)}
    proj_map = _load_map()
    id_map = _load_ids()
    lookup = _build_lookup(proj_map, selected)
    char_damage_map = _build_char_damage_map(proj_map)
    slot_char_ids: dict[int, int] = {}
    try:
        resolved_char_id = int(char_id or 0)
    except Exception:
        resolved_char_id = 0
    if not resolved_char_id and fighter_base:
        resolved_char_id = _read_live_u32(int(fighter_base) + _CHAR_ID_OFF)
    if resolved_char_id:
        slot_char_ids[int(base)] = resolved_char_id
    hits: list[dict] = []
    seen_zombie_variants: set = set()
    seen_fs_missiles: set[int] = set()

    _scan_opcode_blocks(data, base, hits, lookup, slot_char_ids, selected)
    _scan_suffix_blocks(data, base, hits, lookup, id_map, slot_char_ids, selected)
    _scan_zombie_blocks(data, base, hits, lookup, seen_zombie_variants, slot_char_ids)
    _scan_morrigan_finishing_shower_missile(
        data, base, hits, selected, slot_char_ids, seen_fs_missiles
    )
    _scan_super_struct_blocks(data, base, hits, lookup, char_damage_map, slot_char_ids)
    _annotate_clusters(hits)
    hits = _apply_frank_zombie_anchor(hits)

    concrete_hits: list[dict] = []
    seen_records: set[tuple[int, str]] = set()
    for raw_hit in hits:
        hit = dict(raw_hit or {})
        try:
            addr = int(hit.get("addr") or 0)
        except Exception:
            addr = 0
        if not (base <= addr < base + size):
            continue
        fmt = str(hit.get("fmt") or "")
        marker = (addr, fmt)
        if marker in seen_records:
            continue
        seen_records.add(marker)
        hit["key"] = str(key)
        hit["ownership_proof"] = f"{slot_label} chr_tbl range"
        hit["discovery_proof"] = (
            "validated 0xB0/0xD8 projectile record"
            if fmt in ("template", "template2")
            else "slot-owned validated projectile/super payload"
        )
        move = str(hit.get("move") or "")
        if not move or move in {"Unknown", "Signature Match"}:
            try:
                pid = int(hit.get("id"))
            except Exception:
                pid = -1
            hit["move"] = f"Projectile 0x{pid:04X}" if pid >= 0 else f"Projectile @ 0x{addr:08X}"
        concrete_hits.append(hit)

    progress_cb(100.0)
    done_cb(concrete_hits)


class ProjectileProfileMonitorWindow:
    """Compact per-slot projectile dashboard.

    The left side lists every possible projectile for the selected character.
    The right side shows one selected projectile with a small live summary,
    grouped runtime values, editable static definition values, and a separate
    research view for addresses and unresolved fields.
    """

    POLL_MS = 55

    _STANDARD_FIELD_SPECS = {
        "dmg": ("Combat", "Damage", None, "damage"),
        "motion_family": ("Identity", "Motion family", 0x050, "u16"),
        "id": ("Identity", "Projectile definition ID", 0x052, "u16"),
        "lifetime": ("Identity", "Lifetime / active value", 0x05A, "u16"),
        "linked_resource": ("Identity", "Linked resource / script", 0x048, "u32"),
        "mode_a": ("Identity", "Mode / count A", 0x014, "u32"),
        "mode_b": ("Identity", "Mode / flags B", 0x018, "u32"),
        "flags_72": ("Identity", "Flags / sentinel", 0x072, "u32"),
        "radius": ("Movement", "Base collision scale", 0x02C, "f32"),
        "speed": ("Movement", "Travel speed", 0x080, "f32"),
        "speed_mult": ("Movement", "Speed / time multiplier", 0x084, "f32"),
        "percent_scale": ("Movement", "Percentage scale", 0x08C, "f32"),
        "fixed_scale": ("Movement", "Fixed scale", 0x06E, "u16"),
        "kb_x": ("Combat", "Knockback X", 0x024, "f32"),
        "kb_y": ("Combat", "Knockback Y", 0x028, "f32"),
        "arc": ("Movement", "Curve / gravity A", 0x090, "f32"),
        "arc2": ("Movement", "Curve / gravity B", 0x094, "f32"),
        "physics_tail_d4": ("Movement", "Physics tail", 0x0D4, "f32"),
    }

    _FIELD_LABEL_OVERRIDES = {
        "super_lifetime": "Lifetime",
        "super_hit_count": "Hit count",
        "super_hit_interval": "Hit interval",
        "super_particle_fx": "Particle effect",
        "super_spawn_bone": "Spawn bone",
        "super_hit_source": "Hit source",
        "super_air_kb_y": "Beam scale",
        "super_beam_width": "Beam width",
        "super_speed": "Beam speed",
        "super_accel": "Beam force",
        "super_radius": "Hit radius",
        "super_beam_visual": "Beam visual extent",
        "super_final_damage": "Final-hit damage",
        "super_final_lifetime": "Final-hit lifetime",
        "super_final_particle_fx": "Final-hit particle effect",
        "super_final_spawn_bone": "Final-hit spawn bone",
        "super_hit_react": "Hit reaction",
        "super_life": "Legacy lifetime",
        "super_speed_2": "Secondary speed",
        "super_accel_b": "Secondary force B",
        "super_accel_c": "Secondary force C",
        "super_multihit_cap": "Legacy multi-hit value",
        "ps_lifetime": "Lifetime",
        "ps_hit_count": "Hit count",
        "ps_mode": "Mode",
        "ps_emit_count": "Emit count",
        "ps_interval": "Emission interval",
        "ps_offset_x": "Spawn / motion X",
        "ps_offset_y": "Spawn / motion Y",
        "ps_scale": "Scale",
        "ps_particle_fx": "Particle effect",
        "ps_projectile_id": "Projectile definition ID",
        "ps_spawn_bone": "Spawn bone",
        "spawn_x": "Spawn X",
        "spawn_y": "Spawn Y",
        "hitbox": "Hitbox scale",
        "hb_size": "Hitbox size",
        "accel": "Acceleration",
        "type": "Projectile type",
        "c042": "Raw +42",
    }

    _STATIC_METADATA_KEYS = {
        "addr", "address", "char", "key", "move", "name", "cluster", "fmt",
        "dmg_write_addr", "preA", "preB", "opcode", "param1", "param2", "param3",
        "f32_1", "f32_2", "f32_3", "ex03c", "ex060", "ex090", "ex094",
        "ex09c", "ex0d4", "ex0e4",
    }

    def __init__(self, master, slot_label: str, get_snap_fn, get_all_snap_fn=None):
        self.slot_label = str(slot_label)
        self._get_snap = get_snap_fn
        self._get_all_snap = get_all_snap_fn
        self._closed = False
        self._scan_running = False
        self._live_running = False
        self._scan_generation = 0
        self._current_identity = None
        self._projectile_key = None

        self._definition_by_iid: dict[str, dict] = {}
        self._row_iids: list[str] = []
        self._row_by_signature: dict[tuple, str] = {}
        self._ephemeral_rows: set[str] = set()
        self._actor_to_row: dict[int, str] = {}
        self._live_by_row: dict[str, list[dict]] = {}
        self._static_edit_by_iid: dict[str, dict] = {}
        self._static_iid_by_key: dict[str, str] = {}
        self._live_field_iids: dict[tuple[str, str], str] = {}
        self._live_detail_by_iid: dict[str, dict] = {}
        self._research_field_iids: dict[tuple[str, str], str] = {}
        self._summary_label_vars: dict[str, tk.StringVar] = {}
        self._summary_widgets: dict[str, list[tk.Widget]] = {}
        self._summary_edit_specs: dict[str, list[dict]] = {}
        self._selected_row: str | None = None
        self._selected_actor: int | None = None
        self._last_live_actors: set[int] = set()
        self._last_render_actor: int | None = None
        self._last_correlation_tokens: set[tuple] = set()
        # All validated static records from the selected slot.  Keep these
        # separately from the display rows so any projectile, named or unnamed,
        # can be attached to the selected roster entry and populate the cards.
        self._scanned_definitions: list[dict] = []
        self._static_row_by_addr: dict[int, str] = {}

        self.root = tk.Toplevel(master)
        self.root.title(f"Projectile Profile Monitor: {self.slot_label}")
        self.root.geometry("1180x760")
        self.root.minsize(980, 620)
        self.root.configure(background="#0B1220")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_styles()
        self._build_ui()
        self.root.after(1, self._identity_tick)
        self.root.after(self.POLL_MS, self._live_tick)

    def _build_styles(self):
        style = ttk.Style(self.root)
        style.configure(
            "PM.Treeview",
            background="#111827",
            fieldbackground="#111827",
            foreground="#DCE7F7",
            borderwidth=0,
            rowheight=27,
        )
        style.map(
            "PM.Treeview",
            background=[("selected", "#245DA8")],
            foreground=[("selected", "#FFFFFF")],
        )
        style.configure(
            "PM.Treeview.Heading",
            background="#1B2A41",
            foreground="#DCE7F7",
            relief="flat",
            padding=(7, 7),
        )
        style.configure("PM.TNotebook", background="#0B1220", borderwidth=0)
        style.configure("PM.TNotebook.Tab", padding=(14, 8))
        style.configure("PM.TPanedwindow", background="#0B1220")

    def _build_ui(self):
        header = tk.Frame(self.root, bg="#0B1220", padx=14, pady=12)
        header.pack(fill="x")
        title_box = tk.Frame(header, bg="#0B1220")
        title_box.pack(side="left", fill="x", expand=True)
        self._title_var = tk.StringVar(value=f"{self.slot_label}: waiting for character...")
        tk.Label(
            title_box, textvariable=self._title_var, bg="#0B1220", fg="#F4F8FF",
            font=("Segoe UI", 15, "bold"), anchor="w",
        ).pack(anchor="w")
        self._status_var = tk.StringVar(value="Loading projectile profile...")
        tk.Label(
            title_box, textvariable=self._status_var, bg="#0B1220", fg="#8FA6C5",
            font=("Segoe UI", 9), anchor="w",
        ).pack(anchor="w", pady=(3, 0))
        tk.Button(
            header, text="Rescan definitions", command=self._rescan,
            bg="#1D4F8C", fg="white", activebackground="#2867B2",
            activeforeground="white", relief="flat", padx=14, pady=7,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side="right")

        body = ttk.Panedwindow(self.root, orient="horizontal", style="PM.TPanedwindow")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        left = tk.Frame(body, bg="#0E1728", width=310, highlightthickness=1, highlightbackground="#26364F")
        right = tk.Frame(body, bg="#0E1728", highlightthickness=1, highlightbackground="#26364F")
        body.add(left, weight=0)
        body.add(right, weight=1)

        left_head = tk.Frame(left, bg="#0E1728", padx=10, pady=10)
        left_head.pack(fill="x")
        tk.Label(
            left_head, text="POSSIBLE PROJECTILES", bg="#0E1728", fg="#87A7D5",
            font=("Segoe UI", 9, "bold"), anchor="w",
        ).pack(side="left")
        self._roster_count_var = tk.StringVar(value="0")
        tk.Label(
            left_head, textvariable=self._roster_count_var, bg="#223451", fg="#D9E8FF",
            font=("Segoe UI", 8, "bold"), padx=7, pady=2,
        ).pack(side="right")

        roster_wrap = tk.Frame(left, bg="#0E1728")
        roster_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.roster = ttk.Treeview(
            roster_wrap, columns=("state", "damage", "pid"), show="tree headings",
            selectmode="browse", style="PM.Treeview",
        )
        self.roster.heading("#0", text="Projectile")
        self.roster.heading("state", text="State")
        self.roster.heading("damage", text="Dmg")
        self.roster.heading("pid", text="ID")
        self.roster.column("#0", width=170, minwidth=130, anchor="w", stretch=True)
        self.roster.column("state", width=64, minwidth=56, anchor="center", stretch=False)
        self.roster.column("damage", width=58, minwidth=50, anchor="center", stretch=False)
        self.roster.column("pid", width=62, minwidth=52, anchor="center", stretch=False)
        rscroll = ttk.Scrollbar(roster_wrap, orient="vertical", command=self.roster.yview)
        self.roster.configure(yscrollcommand=rscroll.set)
        self.roster.pack(side="left", fill="both", expand=True)
        rscroll.pack(side="right", fill="y")
        self.roster.tag_configure("live", background="#173C4B", foreground="#E8FFFF")
        self.roster.tag_configure("ready", foreground="#C8D5E8")
        self.roster.tag_configure("unmapped", foreground="#8493A8")
        self.roster.bind("<<TreeviewSelect>>", self._on_roster_select)

        tk.Label(
            left, text="A live projectile is selected automatically when it appears.",
            bg="#0E1728", fg="#7387A3", font=("Segoe UI", 8),
            wraplength=280, justify="left", padx=10, pady=8,
        ).pack(fill="x")

        detail_header = tk.Frame(right, bg="#0E1728", padx=14, pady=12)
        detail_header.pack(fill="x")
        detail_text = tk.Frame(detail_header, bg="#0E1728")
        detail_text.pack(side="left", fill="x", expand=True)
        self._projectile_title_var = tk.StringVar(value="Select a projectile")
        tk.Label(
            detail_text, textvariable=self._projectile_title_var, bg="#0E1728", fg="#F4F8FF",
            font=("Segoe UI", 14, "bold"), anchor="w",
        ).pack(anchor="w")
        self._projectile_subtitle_var = tk.StringVar(value="Static definition and live verification")
        tk.Label(
            detail_text, textvariable=self._projectile_subtitle_var, bg="#0E1728", fg="#7890B0",
            font=("Segoe UI", 8), anchor="w",
        ).pack(anchor="w", pady=(2, 0))
        self._live_badge = tk.Label(
            detail_header, text="READY", bg="#26364F", fg="#C8D5E8",
            font=("Segoe UI", 9, "bold"), padx=12, pady=5,
        )
        self._live_badge.pack(side="right")

        self._summary_frame = tk.Frame(right, bg="#0E1728", padx=10, pady=4)
        self._summary_frame.pack(fill="x")
        self._summary_vars: dict[str, tk.StringVar] = {}
        summary_specs = (
            ("damage", "DAMAGE"),
            ("pid", "PROJECTILE ID"),
            ("position", "SPEED"),
            ("movement", "LIFETIME"),
            ("priority", "KNOCKBACK X / Y"),
            ("quota", "COLLISION SCALE"),
        )
        for index, (key, label) in enumerate(summary_specs):
            row_index, col_index = divmod(index, 3)
            card = tk.Frame(
                self._summary_frame, bg="#142035", highlightthickness=1,
                highlightbackground="#263A59", padx=10, pady=8, cursor="hand2",
            )
            card.grid(row=row_index, column=col_index, sticky="nsew", padx=4, pady=4)
            self._summary_frame.grid_columnconfigure(col_index, weight=1)
            label_var = tk.StringVar(value=label)
            self._summary_label_vars[key] = label_var
            label_widget = tk.Label(
                card, textvariable=label_var, bg="#142035", fg="#7890B0",
                font=("Segoe UI", 7, "bold"), anchor="w", cursor="hand2",
            )
            label_widget.pack(anchor="w")
            var = tk.StringVar(value="-")
            self._summary_vars[key] = var
            value_widget = tk.Label(
                card, textvariable=var, bg="#142035", fg="#F2F7FF",
                font=("Segoe UI", 10, "bold"), anchor="w", cursor="hand2",
            )
            value_widget.pack(anchor="w", pady=(4, 0))
            widgets = [card, label_widget, value_widget]
            self._summary_widgets[key] = widgets
            for widget in widgets:
                widget.bind("<Double-Button-1>", lambda _event, k=key: self._edit_summary_key(k))
                widget.bind("<Button-3>", lambda event, k=key: self._on_summary_right_click(event, k))

        instance_bar = tk.Frame(right, bg="#0E1728", padx=14, pady=8)
        instance_bar.pack(fill="x")
        tk.Label(
            instance_bar, text="Live instance", bg="#0E1728", fg="#A8B8CE",
            font=("Segoe UI", 9),
        ).pack(side="left")
        self._actor_var = tk.StringVar(value="No live instance")
        self._actor_combo = ttk.Combobox(
            instance_bar, textvariable=self._actor_var, state="readonly", width=24,
        )
        self._actor_combo.pack(side="left", padx=(8, 0))
        self._actor_combo.bind("<<ComboboxSelected>>", self._on_actor_select)
        self._instance_hint_var = tk.StringVar(value="Static values remain available while no projectile is active.")
        tk.Label(
            instance_bar, textvariable=self._instance_hint_var, bg="#0E1728", fg="#6F84A1",
            font=("Segoe UI", 8), anchor="e",
        ).pack(side="right")

        self.notebook = ttk.Notebook(right, style="PM.TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        live_tab = tk.Frame(self.notebook, bg="#0E1728")
        static_tab = tk.Frame(self.notebook, bg="#0E1728")
        research_tab = tk.Frame(self.notebook, bg="#0E1728")
        self._live_tab = live_tab
        self._static_tab = static_tab
        self._research_tab = research_tab
        self.notebook.add(live_tab, text="Live stats")
        self.notebook.add(static_tab, text="Static definition")
        self.notebook.add(research_tab, text="Research")

        self.live_tree = self._build_detail_tree(
            live_tab, ("value",),
            ("Value",), (500,),
        )
        self.live_tree.bind("<Double-Button-1>", self._on_live_double_click)
        self.live_tree.bind("<Button-3>", self._on_live_right_click)

        static_tools = tk.Frame(static_tab, bg="#0E1728", padx=8, pady=7)
        static_tools.pack(fill="x")
        tk.Label(
            static_tools, text="Double-click a value to write it directly to Dolphin.",
            bg="#0E1728", fg="#7890B0", font=("Segoe UI", 8),
        ).pack(side="left")
        tk.Button(
            static_tools, text="Edit selected", command=self._edit_selected_static,
            bg="#1D4F8C", fg="white", activebackground="#2867B2",
            activeforeground="white", relief="flat", padx=10, pady=5,
            font=("Segoe UI", 8, "bold"), cursor="hand2",
        ).pack(side="right")
        self.static_tree = self._build_detail_tree(
            static_tab, ("value", "type", "offset"),
            ("Value", "Type", "Offset"), (390, 90, 110),
        )
        self.static_tree.bind("<Double-Button-1>", self._on_static_double_click)
        self.static_tree.bind("<Button-3>", self._on_static_right_click)

        self.research_tree = self._build_detail_tree(
            research_tab, ("value", "offset", "address", "confidence"),
            ("Value", "Offset", "Address", "Confidence"), (260, 100, 150, 110),
        )
        self.research_tree.bind("<Button-3>", self._on_research_right_click)

    def _build_detail_tree(self, parent, columns, headings, widths):
        wrap = tk.Frame(parent, bg="#0E1728")
        wrap.pack(fill="both", expand=True, padx=8, pady=8)
        tree = ttk.Treeview(
            wrap, columns=columns, show="tree headings", selectmode="browse",
            style="PM.Treeview",
        )
        tree.heading("#0", text="Field")
        tree.column("#0", width=250, minwidth=180, anchor="w", stretch=True)
        for col, heading, width in zip(columns, headings, widths):
            tree.heading(col, text=heading)
            tree.column(col, width=width, minwidth=70, anchor="w", stretch=True)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        tree.tag_configure("group", background="#18263D", foreground="#9FC1F0")
        tree.tag_configure("muted", foreground="#7E90AA")
        tree.tag_configure("editable", foreground="#DDF2FF")
        return tree

    def _on_close(self):
        self._closed = True
        snap = self._snapshot()
        try:
            owner_base = int(snap.get("base") or 0)
        except Exception:
            owner_base = 0
        _clear_live_profile_label(self.slot_label, owner_base)
        try:
            self.root.destroy()
        except Exception:
            pass

    def _snapshot(self) -> dict:
        try:
            return dict(self._get_snap() or {})
        except Exception:
            return {}

    def _identity_tick(self):
        if self._closed:
            return
        snap = self._snapshot()
        cid, name, key = _monitor_snap_char(snap)
        identity = (cid, name, key)
        if identity != self._current_identity:
            try:
                _old_owner_base = int(snap.get("base") or 0)
            except Exception:
                _old_owner_base = 0
            _clear_live_profile_label(self.slot_label, _old_owner_base)
            self._scan_generation += 1
            self._scan_running = False
            self._current_identity = identity
            self._projectile_key = key
            self._title_var.set(f"{self.slot_label}: {name}")
            self._rebuild_possible_rows(name, key)
            self._rescan()
        try:
            self.root.after(250, self._identity_tick)
        except Exception:
            pass

    def _clear_tree(self, tree):
        for iid in tree.get_children(""):
            tree.delete(iid)

    def _rebuild_possible_rows(self, char_name: str, key: str | None):
        self._clear_tree(self.roster)
        self._definition_by_iid.clear()
        self._row_iids.clear()
        self._row_by_signature.clear()
        self._ephemeral_rows.clear()
        self._actor_to_row.clear()
        self._live_by_row.clear()
        self._selected_row = None
        self._selected_actor = None
        self._last_live_actors.clear()
        self._scanned_definitions.clear()
        self._static_row_by_addr.clear()

        proj_map = _load_map()
        possible = list(proj_map.get(key, []) or []) if key else []
        seen = set()
        for entry in possible:
            move = str(entry.get("move") or entry.get("name") or "Projectile")
            try:
                dmg = int(entry.get("dmg") or 0)
            except Exception:
                dmg = 0
            sig = (_monitor_norm_name(move), dmg)
            if sig in seen:
                continue
            seen.add(sig)
            self._insert_possible_row(move, dmg, dict(entry), sig)

        self._roster_count_var.set(str(len(self._row_iids)))
        if self._row_iids:
            first = self._row_iids[0]
            self.roster.selection_set(first)
            self.roster.focus(first)
            self._select_row(first)
            self._status_var.set(
                f"{char_name}: {len(self._row_iids)} possible projectile(s). Monitoring this slot."
            )
        else:
            self._projectile_title_var.set("No saved projectile map")
            self._status_var.set(
                f"{char_name}: no saved projectile-name map found. Scanning definitions now."
            )
            self._render_empty_detail()

    def _insert_possible_row(self, move: str, damage: int = 0, definition: dict | None = None,
                             signature: tuple | None = None, ephemeral: bool = False) -> str:
        definition = dict(definition or {})
        pid = self._definition_id(definition)
        iid = self.roster.insert(
            "", "end", text=move, tags=("ready",),
            values=("READY", str(damage) if damage else "", str(pid) if pid is not None else ""),
        )
        self._row_iids.append(iid)
        self._definition_by_iid[iid] = definition
        if signature is not None:
            self._row_by_signature[signature] = iid
        if ephemeral:
            self._ephemeral_rows.add(iid)
        self._roster_count_var.set(str(len(self._row_iids)))
        return iid

    def _rescan(self):
        if self._closed or self._scan_running or not self._projectile_key:
            return
        self._scan_running = True
        self._scan_generation += 1
        generation = self._scan_generation
        key = str(self._projectile_key)
        snap = self._snapshot()
        try:
            fighter_base = int(snap.get("base") or 0)
        except Exception:
            fighter_base = 0
        try:
            char_id = int(snap.get("id") or snap.get("char_id") or snap.get("csv_char_id") or 0)
        except Exception:
            char_id = 0
        self._status_var.set("Resolving this fighter's live chr_tbl and scanning validated projectile definitions...")

        def done(hits):
            def apply():
                if self._closed or generation != self._scan_generation:
                    return
                self._scan_running = False
                self._apply_static_hits([h for h in list(hits or []) if str(h.get("key") or "") == key])
            try:
                tk_call(lambda _root: apply())
            except Exception:
                pass

        threading.Thread(
            target=_run_monitor_slot_scan,
            args=(self.slot_label, key, fighter_base, char_id, lambda _pct: None, done),
            daemon=True,
        ).start()

    def _find_row_for_definition(self, hit: dict) -> str:
        """Attach a scanned definition to any projectile row, never one named move.

        Matching is shared by all characters and all projectile formats.  Exact
        static address and projectile ID win, then normalized name, then unique
        damage.  A new scanned row is created only when no existing row can own
        the record.
        """
        move = str(hit.get("move") or "Projectile")
        try:
            dmg = int(hit.get("dmg") or 0)
        except Exception:
            dmg = 0
        try:
            addr = int(hit.get("addr") or 0)
        except Exception:
            addr = 0
        if addr and addr in self._static_row_by_addr:
            row = self._static_row_by_addr[addr]
            if self.roster.exists(row):
                return row

        pid = self._definition_id(hit)
        if pid is not None:
            by_id = [iid for iid in self._row_iids
                     if self._definition_id(self._definition_by_iid.get(iid, {})) == pid]
            if len(by_id) == 1:
                return by_id[0]

        exact = self._row_by_signature.get((_monitor_norm_name(move), dmg))
        if exact:
            return exact
        move_token = _monitor_name_token(move)
        by_name = [iid for iid in self._row_iids
                   if _monitor_norm_name(self.roster.item(iid, "text")) == _monitor_norm_name(move)]
        if len(by_name) == 1:
            return by_name[0]
        by_token = [iid for iid in self._row_iids
                    if move_token and _monitor_name_token(self.roster.item(iid, "text")) == move_token]
        if len(by_token) == 1:
            return by_token[0]
        by_damage = [iid for iid in self._row_iids
                     if dmg and self._definition_damage(self._definition_by_iid.get(iid, {})) == dmg]
        if len(by_damage) == 1:
            return by_damage[0]

        # If only one existing row is still missing a concrete static address,
        # it is the only possible owner for this slot-scoped record.
        unbound = [iid for iid in self._row_iids
                   if not int((self._definition_by_iid.get(iid, {}) or {}).get("addr") or 0)]
        if len(unbound) == 1:
            return unbound[0]
        return self._insert_possible_row(move, dmg, hit, (_monitor_norm_name(move), dmg)
                                        )

    def _merge_static_into_row(self, row: str, hit: dict, evidence: str = "slot scan") -> None:
        if not row or not self.roster.exists(row):
            return
        merged = dict(self._definition_by_iid.get(row, {}) or {})
        # Scanned memory values are authoritative, while a saved display name is
        # retained unless the scan has a genuinely named move.
        saved_move = str(self.roster.item(row, "text") or merged.get("move") or "")
        scanned_move = str(hit.get("move") or "")
        merged.update(dict(hit or {}))
        if saved_move and (not scanned_move or scanned_move.startswith("Projectile 0x")
                           or scanned_move.startswith("Projectile @")
                           or scanned_move in {"Unknown", "Signature Match"}):
            merged["move"] = saved_move
        merged["static_match_evidence"] = evidence
        self._definition_by_iid[row] = merged
        try:
            addr = int(merged.get("addr") or 0)
        except Exception:
            addr = 0
        if addr:
            self._static_row_by_addr[addr] = row
        self.roster.set(row, "damage", str(merged.get("dmg") or ""))
        pid = self._definition_id(merged)
        self.roster.set(row, "pid", str(pid) if pid is not None else "")

    def _ensure_row_has_static(self, row: str, live_record: dict | None = None) -> bool:
        """Bind the best slot-scanned definition to a row on demand."""
        if not row or not self.roster.exists(row):
            return False
        current = dict(self._definition_by_iid.get(row, {}) or {})
        try:
            if int(current.get("addr") or 0):
                return True
        except Exception:
            pass
        candidates = []
        used = set(self._static_row_by_addr)
        for hit in self._scanned_definitions:
            try:
                addr = int(hit.get("addr") or 0)
            except Exception:
                addr = 0
            if addr and addr not in used:
                candidates.append(hit)
        if not candidates:
            return False

        wanted_pid = self._definition_id(current)
        wanted_damage = self._definition_damage(current)
        wanted_name = _monitor_name_token(self.roster.item(row, "text"))
        if live_record:
            try:
                wanted_pid = int(live_record.get("projectile_id") or wanted_pid or 0) or wanted_pid
            except Exception:
                pass
            try:
                wanted_damage = int(live_record.get("damage") or wanted_damage or 0)
            except Exception:
                pass

        if wanted_pid is not None:
            matches = [h for h in candidates if self._definition_id(h) == wanted_pid]
            if len(matches) == 1:
                self._merge_static_into_row(row, matches[0], "exact projectile ID")
                return True
        if wanted_name:
            matches = [h for h in candidates if _monitor_name_token(h.get("move")) == wanted_name]
            if len(matches) == 1:
                self._merge_static_into_row(row, matches[0], "normalized move name")
                return True
        if wanted_damage:
            matches = [h for h in candidates if self._definition_damage(h) == wanted_damage]
            if len(matches) == 1:
                self._merge_static_into_row(row, matches[0], "unique damage")
                return True
        if len(candidates) == 1:
            self._merge_static_into_row(row, candidates[0], "only remaining slot definition")
            return True
        return False

    @staticmethod
    def _definition_damage(definition: dict) -> int:
        try:
            return int(definition.get("dmg") or 0)
        except Exception:
            return 0

    @staticmethod
    def _definition_id(definition: dict) -> int | None:
        for field in ("id", "ps_projectile_id"):
            value = definition.get(field)
            if value in (None, "", "?", "-"):
                continue
            try:
                return int(value, 0) if isinstance(value, str) else int(value)
            except Exception:
                continue
        return None

    def _apply_static_hits(self, hits: list[dict]):
        attached = 0
        self._scanned_definitions = [dict(h or {}) for h in hits]
        for raw_hit in self._scanned_definitions:
            hit = dict(raw_hit or {})
            move = str(hit.get("move") or "")
            try:
                pid_for_name = self._definition_id(hit)
                damage_for_name = int(hit.get("dmg") or 0)
            except Exception:
                pid_for_name, damage_for_name = None, 0
            if move.startswith("Projectile 0x") or move.startswith("Projectile @") or move in {"Unknown", "Signature Match", ""}:
                learned_name = _best_correlated_move_name(self._projectile_key, pid_for_name, damage_for_name)
                if learned_name:
                    hit["move"] = learned_name
            row = self._find_row_for_definition(hit)
            self._merge_static_into_row(row, hit, "shared projectile matcher")
            attached += 1

        # Finish any one-to-one leftovers in roster order.  This is slot-scoped:
        # both sides came from the same fighter chr_tbl, so no other character's
        # records can leak into the pairing.
        unbound_rows = [iid for iid in self._row_iids
                        if not int((self._definition_by_iid.get(iid, {}) or {}).get("addr") or 0)]
        used_addrs = set(self._static_row_by_addr)
        unbound_hits = [h for h in self._scanned_definitions
                        if int(h.get("addr") or 0) not in used_addrs]
        if unbound_rows and len(unbound_rows) == len(unbound_hits):
            unbound_hits.sort(key=lambda h: int(h.get("addr") or 0))
            for row, hit in zip(unbound_rows, unbound_hits):
                self._merge_static_into_row(row, hit, "slot definition order")
                attached += 1
        self._roster_count_var.set(str(len(self._row_iids)))
        self._status_var.set(
            f"{len(self._row_iids)} possible projectile(s), {attached} static definition record(s) mapped."
        )
        if self._selected_row:
            self._render_static(self._selected_row)
            self._render_static_summary(self._selected_row)
            self._refresh_selected_panel()

    def _on_roster_select(self, _event=None):
        selection = self.roster.selection()
        if selection:
            self._select_row(selection[0])

    def _select_row(self, row: str):
        if not row or not self.roster.exists(row):
            return
        self._selected_row = row
        self._selected_actor = None
        self._last_render_actor = None
        self._projectile_title_var.set(str(self.roster.item(row, "text") or "Projectile"))
        self._ensure_row_has_static(row)
        definition = self._definition_by_iid.get(row, {})
        fmt = str(definition.get("fmt") or "unmapped definition")
        try:
            base = int(definition.get("addr") or 0)
        except Exception:
            base = 0
        base_text = _fmt_hex(base) if base else "not scanned yet"
        self._projectile_subtitle_var.set(f"{fmt}  |  static base {base_text}")
        self._render_static(row)
        self._render_static_summary(row)
        # Manual projectile selection is definition-first. A newly spawned live
        # actor may still switch to Live stats automatically below.
        try:
            self.notebook.select(self._static_tab)
        except Exception:
            pass
        self._refresh_selected_panel()

    @staticmethod
    def _first_present(definition: dict, *keys):
        for key in keys:
            value = definition.get(key)
            if value not in (None, "", "?", "-"):
                return key, value
        return None, None

    def _render_static_summary(self, row: str | None):
        definition = self._definition_by_iid.get(row or "", {})
        self._summary_edit_specs.clear()

        damage_key, damage = self._first_present(definition, "dmg")
        pid_key, pid_value = self._first_present(definition, "id", "ps_projectile_id")
        if pid_value in (None, "", "?", "-"):
            pid_value = self._definition_id(definition)
        speed_key, speed = self._first_present(definition, "speed", "super_speed", "ps_offset_x")
        life_key, lifetime = self._first_present(definition, "lifetime", "super_lifetime", "ps_lifetime", "super_life")
        kbx_key, kbx = self._first_present(definition, "kb_x")
        kby_key, kby = self._first_present(definition, "kb_y")
        scale_key, scale = self._first_present(definition, "radius", "super_radius", "ps_scale", "percent_scale", "hitbox")

        self._summary_vars["damage"].set(str(damage) if damage not in (None, "") else "-")
        self._summary_vars["pid"].set(str(pid_value) if pid_value not in (None, "") else "-")
        self._summary_vars["position"].set(str(speed) if speed not in (None, "") else "-")
        self._summary_vars["movement"].set(str(lifetime) if lifetime not in (None, "") else "-")
        self._summary_vars["priority"].set(
            f"{kbx} / {kby}" if kbx not in (None, "") or kby not in (None, "") else "-"
        )
        self._summary_vars["quota"].set(str(scale) if scale not in (None, "") else "-")

        card_fields = {
            "damage": [(damage_key, damage)],
            "pid": [(pid_key, pid_value)],
            "position": [(speed_key, speed)],
            "movement": [(life_key, lifetime)],
            "priority": [(kbx_key, kbx), (kby_key, kby)],
            "quota": [(scale_key, scale)],
        }
        for card_key, fields in card_fields.items():
            specs = []
            for field_key, value in fields:
                if not field_key:
                    continue
                edit = self._static_edit_info(definition, str(field_key))
                specs.append({
                    "key": str(field_key), "value": value, "edit": edit,
                    "definition": definition, "row": row,
                })
            self._summary_edit_specs[card_key] = specs

    def _summary_addresses(self, key: str) -> list[int]:
        addresses = []
        for spec in self._summary_edit_specs.get(key, []):
            edit = spec.get("edit")
            if edit:
                try:
                    addresses.append(int(edit[0]))
                except Exception:
                    pass
        return addresses

    def _on_summary_right_click(self, event, key: str):
        value = str(self._summary_vars.get(key).get() if key in self._summary_vars else "")
        addresses = self._summary_addresses(key)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Copy value", command=lambda: self._copy(value))
        if addresses:
            for index, address in enumerate(addresses):
                suffix = f" {index + 1}" if len(addresses) > 1 else ""
                menu.add_command(
                    label=f"Copy address{suffix} ({_fmt_hex(address)})",
                    command=lambda a=address: self._copy(_fmt_hex(a)),
                )
            menu.add_separator()
            menu.add_command(label="Edit value", command=lambda: self._edit_summary_key(key))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _edit_summary_key(self, key: str):
        specs = [spec for spec in self._summary_edit_specs.get(key, []) if spec.get("edit")]
        if not specs:
            messagebox.showinfo(
                "Address not mapped",
                "This field has not been attached to a concrete static address yet. Rescan definitions, then select it again.",
                parent=self.root,
            )
            return
        current_values = [str(spec.get("value") if spec.get("value") is not None else "") for spec in specs]
        prompt = "New value" if len(specs) == 1 else "New values, comma separated"
        address_text = ", ".join(_fmt_hex(int(spec["edit"][0])) for spec in specs)
        new_value = simpledialog.askstring(
            "Edit projectile field",
            f"{prompt}\nAddress: {address_text}",
            initialvalue=", ".join(current_values),
            parent=self.root,
        )
        if new_value is None:
            return
        parts = [part.strip() for part in str(new_value).split(",")]
        if len(specs) > 1 and len(parts) != len(specs):
            messagebox.showerror("Invalid value", f"Enter exactly {len(specs)} comma-separated values.", parent=self.root)
            return
        if len(specs) == 1:
            parts = [str(new_value).strip()]
        try:
            for spec, part in zip(specs, parts):
                self._write_static_edit_spec(spec, part)
        except Exception as exc:
            messagebox.showerror("Write failed", str(exc), parent=self.root)
            return
        if self._selected_row:
            self._render_static(self._selected_row)
            self._render_static_summary(self._selected_row)
        self._refresh_selected_panel()

    def _refresh_selected_panel(self):
        row = self._selected_row
        if not row or not self.roster.exists(row):
            self._render_empty_detail()
            return
        records = list(self._live_by_row.get(row, []) or [])
        records.sort(key=lambda r: int(r.get("actor") or 0))
        actor_values = [_fmt_hex(int(r.get("actor") or 0)) for r in records]
        self._actor_combo.configure(values=actor_values)
        if records:
            actors = {int(r.get("actor") or 0) for r in records}
            if self._selected_actor not in actors:
                self._selected_actor = int(records[0].get("actor") or 0)
            self._actor_var.set(_fmt_hex(self._selected_actor))
            record = next((r for r in records if int(r.get("actor") or 0) == self._selected_actor), records[0])
            self._render_live(record)
        else:
            self._selected_actor = None
            self._actor_combo.configure(values=[])
            self._actor_var.set("No live instance")
            self._render_no_live()

    def _on_actor_select(self, _event=None):
        text = str(self._actor_var.get() or "")
        try:
            self._selected_actor = int(text, 16)
        except Exception:
            self._selected_actor = None
        self._refresh_selected_panel()

    def _render_empty_detail(self):
        for var in self._summary_vars.values():
            var.set("-")
        self._live_badge.configure(text="READY", bg="#26364F", fg="#C8D5E8")
        self._clear_tree(self.live_tree)
        self._clear_tree(self.static_tree)
        self._clear_tree(self.research_tree)

    def _render_no_live(self):
        self._render_static_summary(self._selected_row)
        self._live_badge.configure(text="READY", bg="#26364F", fg="#C8D5E8")
        self._instance_hint_var.set("Waiting for this projectile to exist in Dolphin.")
        self._clear_tree(self.live_tree)
        self.live_tree.insert(
            "", "end", text="No live instance", tags=("muted",),
            values=("Use the projectile in-game to verify its runtime values.",),
        )
        self._render_research(None)
        self._last_render_actor = None

    def _render_live(self, record: dict):
        actor = int(record.get("actor") or 0)
        position = record.get("position", (0.0, 0.0, 0.0))
        movement = record.get("velocity", (0.0, 0.0, 0.0))
        quota_used, quota_max = record.get("quota", (0, 0))
        # The cards are definition-first and remain editable. Live motion and
        # collision values stay in the Live stats tab below.
        self._render_static_summary(self._selected_row)
        self._live_badge.configure(text="LIVE", bg="#0E6A69", fg="#E8FFFF")
        self._instance_hint_var.set(f"Refreshing every {self.POLL_MS} ms")

        details = [d for d in list(record.get("details", []) or []) if str(d.get("group") or "") != "Research"]
        keys = [(str(d.get("group") or "Other"), str(d.get("label") or "?")) for d in details]
        existing_keys = set(self._live_field_iids)
        if actor != self._last_render_actor or set(keys) != existing_keys:
            self._clear_tree(self.live_tree)
            self._live_field_iids.clear()
            self._live_detail_by_iid.clear()
            groups: dict[str, str] = {}
            for detail in details:
                group = str(detail.get("group") or "Other")
                label = str(detail.get("label") or "?")
                group_iid = groups.get(group)
                if not group_iid:
                    group_iid = self.live_tree.insert("", "end", text=group, open=True, tags=("group",), values=("",))
                    groups[group] = group_iid
                iid = self.live_tree.insert(
                    group_iid, "end", text=label,
                    values=(str(detail.get("value") or ""),),
                )
                self._live_field_iids[(group, label)] = iid
                self._live_detail_by_iid[iid] = dict(detail)
        else:
            for detail in details:
                key = (str(detail.get("group") or "Other"), str(detail.get("label") or "?"))
                iid = self._live_field_iids.get(key)
                if iid and self.live_tree.exists(iid):
                    self.live_tree.item(
                        iid,
                        values=(str(detail.get("value") or ""),),
                    )
                    self._live_detail_by_iid[iid] = dict(detail)
        self._last_render_actor = actor
        self._render_research(record)

    def _static_group_for_key(self, key: str) -> str:
        if key.startswith("super_"):
            return "Beam / super card"
        if key.startswith("ps_"):
            return "Compact projectile card"
        if key in ("spawn_x", "spawn_y"):
            return "Spawn"
        if key in ("hitbox", "hb_size", "accel", "type", "c042"):
            return "Additional mapped fields"
        return "Other scanned fields"

    def _static_label_for_key(self, key: str) -> str:
        return self._FIELD_LABEL_OVERRIDES.get(key, key.replace("_", " ").title())

    def _static_edit_info(self, definition: dict, key: str):
        try:
            base = int(definition.get("addr") or 0)
        except Exception:
            base = 0
        fmt = str(definition.get("fmt") or "")
        if not base:
            return None
        if key == "dmg":
            try:
                addr = int(definition.get("dmg_write_addr") or (base + _dmg_write_offset(fmt)))
            except Exception:
                addr = 0
            return (addr, "damage", "damage") if addr else None
        # Scanner rows may provide an explicit address for any field.
        for address_key in (f"{key}_write_addr", f"{key}_addr", f"{key}_address"):
            try:
                direct = int(definition.get(address_key) or 0)
            except Exception:
                direct = 0
            if direct:
                typ = str(_STATIC_FIELD_TYPES.get(key) or "u32")
                return direct, typ, "direct"
        address_map = definition.get("field_addresses") or definition.get("addresses")
        if isinstance(address_map, dict):
            try:
                direct = int(address_map.get(key) or 0)
            except Exception:
                direct = 0
            if direct:
                typ = str(_STATIC_FIELD_TYPES.get(key) or "u32")
                return direct, typ, "direct"
        if key in _SUPER_EX_OFFSETS:
            return _super_ex_base(base, fmt) + int(_SUPER_EX_OFFSETS[key]), "f32", f"+{int(_SUPER_EX_OFFSETS[key]):03X}"
        spec = self._STANDARD_FIELD_SPECS.get(key)
        if spec:
            _group, _label, offset, typ = spec
            return base + int(offset), typ, f"+{int(offset):03X}"
        if key in FIELD_OFFSETS:
            offset = int(FIELD_OFFSETS[key])
            typ = str(_STATIC_FIELD_TYPES.get(key) or "u32")
            return base + offset, typ, f"+{offset:03X}"
        if key in _SUPER_FIELD_OFFSETS:
            offset, typ = _SUPER_FIELD_OFFSETS[key]
            addr = _super_ex_base(base, fmt) + int(offset)
            return addr, typ, f"+{int(offset):03X}"
        if key in _PROJECTILE_SUPER_FIELD_OFFSETS:
            offset, typ = _PROJECTILE_SUPER_FIELD_OFFSETS[key]
            return base + int(offset), typ, f"+{int(offset):03X}"
        import re
        match = re.search(r"(?:0x|\+|_)([0-9a-fA-F]{2,3})$", str(key))
        if match:
            offset = int(match.group(1), 16)
            if 0 <= offset < 0x200:
                value = definition.get(key)
                typ = "f32" if isinstance(value, float) else "u32"
                return base + offset, typ, f"+{offset:03X}"
        return None

    def _iter_static_fields(self, definition: dict):
        emitted = set()
        for key, (group, label, _offset, typ) in self._STANDARD_FIELD_SPECS.items():
            value = definition.get(key)
            if value in (None, "?", ""):
                continue
            edit = self._static_edit_info(definition, key)
            offset_text = edit[2] if edit else ""
            yield group, label, key, value, typ, offset_text, edit
            emitted.add(key)

        for key, value in definition.items():
            if key in emitted or key in self._STATIC_METADATA_KEYS or value in (None, "?", ""):
                continue
            if isinstance(value, (dict, list, tuple, set)):
                continue
            edit = self._static_edit_info(definition, key)
            if edit:
                typ = edit[1]
                offset_text = edit[2]
            else:
                typ = "read-only"
                offset_text = ""
            yield self._static_group_for_key(key), self._static_label_for_key(key), key, value, typ, offset_text, edit

    def _render_static(self, row: str):
        self._clear_tree(self.static_tree)
        self._static_edit_by_iid.clear()
        self._static_iid_by_key.clear()
        definition = self._definition_by_iid.get(row, {})
        if not definition:
            self.static_tree.insert(
                "", "end", text="Definition not scanned yet", tags=("muted",),
                values=("Rescan definitions to populate this projectile.", "", ""),
            )
            return
        grouped: dict[str, list] = {}
        for item in self._iter_static_fields(definition):
            grouped.setdefault(item[0], []).append(item)
        if not grouped:
            self.static_tree.insert("", "end", text="No mapped static fields", tags=("muted",), values=("", "", ""))
            return
        preferred = ["Identity", "Movement", "Combat", "Spawn", "Beam / super card", "Compact projectile card", "Additional mapped fields", "Other scanned fields"]
        ordered_groups = [g for g in preferred if g in grouped] + [g for g in grouped if g not in preferred]
        for group in ordered_groups:
            group_iid = self.static_tree.insert("", "end", text=group, open=True, tags=("group",), values=("", "", ""))
            for _group, label, key, value, typ, offset_text, edit in grouped[group]:
                tag = "editable" if edit else "muted"
                iid = self.static_tree.insert(
                    group_iid, "end", text=label, tags=(tag,),
                    values=(str(value), typ, offset_text),
                )
                self._static_iid_by_key[str(key)] = iid
                if edit:
                    self._static_edit_by_iid[iid] = {
                        "address": int(edit[0]), "type": typ, "key": key,
                        "definition": definition, "row": row, "fmt": str(definition.get("fmt") or ""),
                    }

        # Preserve a complete editable view of the underlying record. Mapped
        # fields above remain the friendly interface, while this collapsed
        # section guarantees that no static word is hidden from research.
        try:
            raw_base = int(definition.get("addr") or 0)
        except Exception:
            raw_base = 0
        if raw_base:
            try:
                motion_family = int(definition.get("motion_family") or 0)
            except Exception:
                motion_family = 0
            raw_size = 0xD8 if motion_family == 4 or definition.get("physics_tail_d4") not in (None, "", "?") else 0xB0
            raw_data = _read_live_block(raw_base, raw_size)
            if len(raw_data) >= 4:
                raw_group = self.static_tree.insert(
                    "", "end", text=f"Raw static record ({raw_size:#x} bytes)",
                    open=False, tags=("group",), values=("", "", ""),
                )
                for offset in range(0, min(raw_size, len(raw_data)) - 3, 4):
                    word = struct.unpack_from(">I", raw_data, offset)[0]
                    fvalue = struct.unpack_from(">f", raw_data, offset)[0]
                    if not (fvalue == fvalue and abs(fvalue) != float("inf")):
                        ftext = "nan/inf"
                    else:
                        ftext = f"{fvalue:.7g}"
                    raw_key = f"__raw_{offset:03X}"
                    iid = self.static_tree.insert(
                        raw_group, "end", text=f"Raw +{offset:03X}", tags=("editable",),
                        values=(f"0x{word:08X} | u32 {word} | f32 {ftext}", "raw32", f"+{offset:03X}"),
                    )
                    self._static_iid_by_key[raw_key] = iid
                    self._static_edit_by_iid[iid] = {
                        "address": raw_base + offset, "type": "raw32", "key": raw_key,
                        "definition": definition, "row": row, "fmt": str(definition.get("fmt") or ""),
                    }

    def _render_research(self, record: dict | None):
        self._clear_tree(self.research_tree)
        self._research_field_iids.clear()
        row = self._selected_row
        definition = self._definition_by_iid.get(row or "", {})
        try:
            static_base = int(definition.get("addr") or 0)
        except Exception:
            static_base = 0
        static_group = self.research_tree.insert("", "end", text="Static record", open=True, tags=("group",), values=("", "", "", ""))
        self.research_tree.insert(static_group, "end", text="Format", values=(str(definition.get("fmt") or "-"), "", "", ""))
        self.research_tree.insert(static_group, "end", text="Static base", values=(_fmt_hex(static_base) if static_base else "-", "base", _fmt_hex(static_base) if static_base else "", "Confirmed"))
        if record is None:
            self.research_tree.insert("", "end", text="No live runtime record", tags=("muted",), values=("", "", "", ""))
            return
        actor = int(record.get("actor") or 0)
        linked = int(record.get("linked") or 0)
        runtime_group = self.research_tree.insert("", "end", text="Runtime addresses", open=True, tags=("group",), values=("", "", "", ""))
        self.research_tree.insert(runtime_group, "end", text="Actor", values=(_fmt_hex(actor), "base", _fmt_hex(actor), "Confirmed"))
        self.research_tree.insert(runtime_group, "end", text="Linked collision record", values=(_fmt_hex(linked), "+13C", _fmt_hex(actor + 0x13C), "Confirmed"))
        self.research_tree.insert(runtime_group, "end", text="Static/live correlation", values=(str(record.get("match_evidence") or "unmatched live actor"), "ID then damage", "", "Runtime proof"))
        self.research_tree.insert(runtime_group, "end", text="Owner action at spawn/read", values=(f"{int(record.get('owner_action_id') or 0)} ({_fmt_hex(int(record.get('owner_action_id') or 0))})", "owner +1E8", _fmt_hex(int(record.get('owner_pointer') or 0) + _LIVE_FIGHTER_ACTION_OFF), "Confirmed"))
        all_details = list(record.get("details", []) or [])
        mapped_details = [d for d in all_details if str(d.get("group") or "") != "Research"]
        if mapped_details:
            mapped_group = self.research_tree.insert("", "end", text="Mapped live field addresses", open=False, tags=("group",), values=("", "", "", ""))
            for detail in mapped_details:
                address = detail.get("address")
                field_name = f"{detail.get('group') or 'Other'} / {detail.get('label') or '?'}"
                self.research_tree.insert(
                    mapped_group, "end", text=field_name,
                    values=(
                        str(detail.get("value") or ""),
                        str(detail.get("offset") or ""),
                        _fmt_hex(address) if isinstance(address, int) else "",
                        str(detail.get("confidence") or ""),
                    ),
                )
        research_details = [d for d in all_details if str(d.get("group") or "") == "Research"]
        if research_details:
            group_iid = self.research_tree.insert("", "end", text="Unresolved and research fields", open=True, tags=("group",), values=("", "", "", ""))
            for detail in research_details:
                address = detail.get("address")
                self.research_tree.insert(
                    group_iid, "end", text=str(detail.get("label") or "?"),
                    values=(
                        str(detail.get("value") or ""),
                        str(detail.get("offset") or ""),
                        _fmt_hex(address) if isinstance(address, int) else "",
                        str(detail.get("confidence") or ""),
                    ),
                )

    def _live_tick(self):
        if self._closed:
            return
        if not self._live_running:
            self._live_running = True
            threading.Thread(target=self._live_worker, daemon=True).start()
        try:
            self.root.after(self.POLL_MS, self._live_tick)
        except Exception:
            pass

    def _live_worker(self):
        try:
            records = _collect_live_projectiles()
            error = None
        except Exception as exc:
            records = []
            error = str(exc)
        try:
            tk_call(lambda _root: self._apply_live(records, error))
        except Exception:
            self._live_running = False

    def _owner_code(self) -> str:
        return _MONITOR_OWNER_BY_SLOT.get(self.slot_label, "")

    def _match_row(self, record: dict, occupied: set[str]) -> str | None:
        actor = int(record.get("actor") or 0)
        bound = self._actor_to_row.get(actor)
        if bound and self.roster.exists(bound):
            return bound
        live_id = int(record.get("projectile_id") or 0)
        exact = [iid for iid in self._row_iids
                 if self._definition_id(self._definition_by_iid.get(iid, {})) == live_id]
        if len(exact) == 1:
            record["match_evidence"] = "exact static projectile ID"
            self._ensure_row_has_static(exact[0], record)
            return exact[0]
        live_damage = int(record.get("damage") or 0)
        damage_rows = [iid for iid in self._row_iids
                       if live_damage and self._definition_damage(self._definition_by_iid.get(iid, {})) == live_damage]
        free_damage_rows = [iid for iid in damage_rows if iid not in occupied]
        if len(free_damage_rows) == 1:
            record["match_evidence"] = "unique damage correlation"
            self._ensure_row_has_static(free_damage_rows[0], record)
            return free_damage_rows[0]
        if len(damage_rows) == 1:
            record["match_evidence"] = "unique damage correlation"
            self._ensure_row_has_static(damage_rows[0], record)
            return damage_rows[0]
        return None

    def _record_live_correlation(self, record: dict, row: str | None) -> None:
        if not row or not self.roster.exists(row):
            return
        snap = self._snapshot()
        char_key = str(self._projectile_key or "").strip()
        if not char_key:
            return
        try:
            projectile_id = int(record.get("projectile_id") or 0)
            actor = int(record.get("actor") or 0)
            action_id = int(record.get("owner_action_id") or snap.get("mv_id_display") or 0)
            damage = int(record.get("damage") or 0)
        except Exception:
            return
        # The roster row is the profiler's resolved projectile variant. It can
        # contain strength-specific names such as Hyper Zero Blaster L2/L3/L4
        # or Volnutt Charge C even when the fighter action label is generic.
        row_label = _usable_profile_label(self.roster.item(row, "text"))
        move_label = row_label or str(snap.get("mv_label_display") or snap.get("mv_label") or "").strip()
        if not move_label or move_label.lower() in {"unknown", "-", "none"}:
            move_label = f"Action 0x{action_id:04X}" if action_id else "Unlabeled action"
        definition = dict(self._definition_by_iid.get(row, {}) or {})
        _publish_live_profile_label(self.slot_label, snap, record, definition, move_label)
        try:
            static_addr = int(definition.get("addr") or 0)
        except Exception:
            static_addr = 0
        evidence = str(record.get("match_evidence") or "live owner/action observation")
        token = (actor, projectile_id, action_id, static_addr, _monitor_norm_name(move_label))
        if token in self._last_correlation_tokens:
            return
        self._last_correlation_tokens.add(token)

        with _PROJECTILE_CORRELATION_LOCK:
            payload = _load_projectile_correlations()
            payload.setdefault("version", 1)
            chars = payload.setdefault("characters", {})
            char = chars.setdefault(char_key, {})
            char["char_name"] = str(snap.get("name") or snap.get("char_name") or char_key)
            observations = char.setdefault("observations", {})
            obs_key = f"{projectile_id:04X}:{damage}:{static_addr:08X}"
            obs = observations.setdefault(obs_key, {})
            obs.update({
                "projectile_id": projectile_id,
                "damage": damage,
                "static_addr": static_addr,
                "static_fmt": str(definition.get("fmt") or ""),
                "match_evidence": evidence,
                "slot": self.slot_label,
                "owner_pointer": int(record.get("owner_pointer") or 0),
                "last_actor": actor,
                "last_seen_unix": int(time.time()),
            })
            moves = obs.setdefault("moves", {})
            move_key = f"{action_id:08X}:{_monitor_norm_name(move_label)}"
            move = moves.setdefault(move_key, {
                "action_id": action_id,
                "move_label": move_label,
                "count": 0,
            })
            move["count"] = int(move.get("count") or 0) + 1
            move["last_seen_unix"] = int(time.time())
            _save_projectile_correlations(payload)

        current_text = str(self.roster.item(row, "text") or "")
        if (current_text.startswith("Projectile 0x") or current_text.startswith("Projectile @") or
                current_text.startswith("Unmatched projectile")):
            self.roster.item(row, text=move_label)
            self._projectile_title_var.set(move_label)
        record["observed_move_label"] = move_label

    def _apply_live(self, records: list[dict], error: str | None):
        self._live_running = False
        if self._closed:
            return
        if error:
            self._status_var.set(f"Live projectile read failed: {error}")
            return
        snap = self._snapshot()
        try:
            owner_base = int(snap.get("base") or 0)
        except Exception:
            owner_base = 0
        if owner_base:
            active = [r for r in records if int(r.get("owner_pointer") or 0) == owner_base]
        else:
            owner = self._owner_code()
            active = [r for r in records if str(r.get("owner") or "") == owner]
        live_actors = {int(r.get("actor") or 0) for r in active}

        for actor in list(self._actor_to_row):
            if actor not in live_actors:
                self._actor_to_row.pop(actor, None)

        occupied = set(self._actor_to_row.values())
        live_by_row: dict[str, list[dict]] = {}
        for record in active:
            actor = int(record.get("actor") or 0)
            row = self._match_row(record, occupied)
            if row is None:
                move = f"Unmatched projectile ID {record.get('projectile_id', '?')}"
                row = self._insert_possible_row(move, int(record.get("damage") or 0), {}, None, ephemeral=True)
            self._actor_to_row[actor] = row
            occupied.add(row)
            live_by_row.setdefault(row, []).append(record)

        for row in list(self._row_iids):
            if not self.roster.exists(row):
                continue
            row_records = live_by_row.get(row, [])
            if row_records:
                self.roster.item(row, tags=("live",))
                self.roster.set(row, "state", f"LIVE {len(row_records)}" if len(row_records) > 1 else "LIVE")
                self.roster.set(row, "damage", str(row_records[0].get("damage") or ""))
                self.roster.set(row, "pid", str(row_records[0].get("projectile_id") or ""))
            else:
                self.roster.item(row, tags=(("unmapped",) if row in self._ephemeral_rows else ("ready",)))
                self.roster.set(row, "state", "READY")
                definition = self._definition_by_iid.get(row, {})
                self.roster.set(row, "damage", str(definition.get("dmg") or ""))
                pid = self._definition_id(definition)
                self.roster.set(row, "pid", str(pid) if pid is not None else "")

        for row in list(self._ephemeral_rows):
            if row in live_by_row or not self.roster.exists(row):
                continue
            was_selected = row == self._selected_row
            self.roster.delete(row)
            self._ephemeral_rows.discard(row)
            self._definition_by_iid.pop(row, None)
            try:
                self._row_iids.remove(row)
            except ValueError:
                pass
            if was_selected:
                self._selected_row = None
                self._selected_actor = None

        new_actors = live_actors - self._last_live_actors
        self._live_by_row = live_by_row

        # Publish only rows that are LIVE on this exact poll. When the actor
        # disappears, clear the authority immediately so the suffix cannot be
        # applied to Idle, 5A, 2A, or the next unrelated action.
        published_profile = False
        for record in active:
            actor = int(record.get("actor") or 0)
            row = self._actor_to_row.get(actor)
            if not row or not self.roster.exists(row):
                continue
            label = _usable_profile_label(self.roster.item(row, "text"))
            if not label:
                continue
            definition = dict(self._definition_by_iid.get(row, {}) or {})
            _publish_live_profile_label(self.slot_label, snap, record, definition, label)
            published_profile = True
            self._record_live_correlation(record, row)
        if not published_profile:
            _clear_live_profile_label(self.slot_label, owner_base)

        if new_actors:
            actor = next(iter(new_actors))
            row = self._actor_to_row.get(actor)
            if row and self.roster.exists(row):
                self.roster.selection_set(row)
                self.roster.focus(row)
                self.roster.see(row)
                self._selected_row = row
                self._selected_actor = actor
                self._projectile_title_var.set(str(self.roster.item(row, "text") or "Projectile"))
                self._render_static(row)
                self._render_static_summary(row)
                try:
                    self.notebook.select(self._live_tab)
                except Exception:
                    pass
        self._last_live_actors = live_actors
        if not self._selected_row and self._row_iids:
            first = self._row_iids[0]
            if self.roster.exists(first):
                self.roster.selection_set(first)
                self.roster.focus(first)
                self._select_row(first)
        self._roster_count_var.set(str(len(self._row_iids)))

        if active:
            base_note = f" from {_fmt_hex(owner_base)}" if owner_base else ""
            self._status_var.set(f"{self.slot_label}: {len(active)} live projectile" + ("" if len(active) == 1 else "s") + base_note + ". Live action correlations are being saved automatically.")
        elif not self._scan_running:
            self._status_var.set(f"{self.slot_label}: monitoring {len(self._row_iids)} possible projectile(s).")
        self._refresh_selected_panel()

    def _live_edit_descriptor(self, detail: dict | None):
        detail = dict(detail or {})
        label = str(detail.get("label") or "")
        address = detail.get("address")
        if not isinstance(address, int) or address <= 0:
            return None
        if label in {"Actor address", "Owner slot", "Per-frame movement", "Collision quota remaining"}:
            return None

        vector_offsets = {
            "Current position": (0x00, 0x10, 0x20),
            "Previous position": (0x00, 0x10, 0x20),
            "Emitter/origin": (0x00, 0x04, 0x08),
            "Direction vector": (0x00, 0x04, -0x04),
            "Impact/contact position": (0x00, 0x04, 0x08),
            "Hit direction": (0x00, 0x04, 0x08),
            "Computed world contact": (0x00, 0x10, 0x20),
        }
        if label in vector_offsets:
            return {
                "kind": "vec3_f32",
                "addresses": [address + off for off in vector_offsets[label]],
                "detail": detail,
            }

        pair_offsets = {
            "Contact effect 1": (0x00, 0x10),
            "Contact effect 2": (0x00, 0x10),
            "World collision effect": (0x00, 0x10),
            "Effect slot 3 raw": (0x00, 0x10),
        }
        if label in pair_offsets:
            return {
                "kind": "pair_u32",
                "addresses": [address + off for off in pair_offsets[label]],
                "detail": detail,
            }

        if label == "Bypass projectile clashes":
            return {"kind": "bit", "addresses": [address], "mask": 1 << 4, "detail": detail}
        if label == "Clash-priority override":
            return {"kind": "bit", "addresses": [address], "mask": 1 << 5, "detail": detail}
        if label == "Equal-priority response class":
            return {"kind": "bits", "addresses": [address], "mask": 0x7 << 6, "shift": 6, "detail": detail}

        f32_labels = {
            "Mutual-clash lockout", "Damage/hit scaling multiplier",
            "Contact-point Y offset", "Contact-point X offset",
            "Actor scale candidate",
        }
        s32_labels = {
            "Collision actor type", "Projectile clash priority",
            "Collision quota maximum", "Collision quota consumed",
            "Damage", "Hitstun override", "Blockstun override",
            "Giant stagger/armor impact", "Hitstop override",
            "Secondary stun/reaction raw", "Paired-contact state A",
            "Paired-contact state B",
        }
        u32_labels = {
            "Owner pointer", "Owner action ID", "Projectile ID",
            "Owner/root mirror", "Linked collision record",
            "Linked owner pointer", "Target/contact fighter",
            "Hit flags A", "Hit flags B", "Shape type", "Shape array",
            "Shape count", "Contact offset mode A raw",
            "Contact offset mode B raw", "Actor fixed scale raw",
        }
        if label in f32_labels:
            kind = "f32"
        elif label in s32_labels:
            kind = "s32"
        elif label in u32_labels:
            kind = "u32"
        else:
            raw_value = str(detail.get("value") or "")
            if raw_value.startswith("0x"):
                kind = "u32"
            elif any(ch in raw_value for ch in (".", "e", "E")):
                kind = "f32"
            else:
                kind = "s32"
        return {"kind": kind, "addresses": [address], "detail": detail}

    @staticmethod
    def _parse_bool(value: str) -> bool:
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled"}:
            return False
        raise ValueError("Enter true/false or 1/0")

    def _write_live_descriptor(self, descriptor: dict, new_value: str):
        kind = str(descriptor.get("kind") or "")
        addresses = [int(a) for a in descriptor.get("addresses", [])]
        if not addresses:
            raise ValueError("No writable address")
        if kind == "vec3_f32":
            parts = [p.strip() for p in str(new_value).strip("() ").split(",")]
            if len(parts) != 3:
                raise ValueError("Enter X, Y, Z")
            values = [float(p) for p in parts]
            for address, value in zip(addresses, values):
                if not _write_f32(address, value):
                    raise RuntimeError(f"Could not write {_fmt_hex(address)}")
            return
        if kind == "pair_u32":
            text = str(new_value).replace("ID", "").replace("arg", "").replace(":", " ")
            parts = [p.strip() for p in text.replace("/", ",").split(",") if p.strip()]
            if len(parts) != 2:
                raise ValueError("Enter ID, argument")
            values = [int(p, 0) for p in parts]
            for address, value in zip(addresses, values):
                if not _write_u32(address, value & 0xFFFFFFFF):
                    raise RuntimeError(f"Could not write {_fmt_hex(address)}")
            return
        if kind == "bit":
            enabled = self._parse_bool(new_value)
            current = _read_live_u32(addresses[0])
            mask = int(descriptor.get("mask") or 0)
            updated = (current | mask) if enabled else (current & ~mask)
            if not _write_u32(addresses[0], updated & 0xFFFFFFFF):
                raise RuntimeError(f"Could not write {_fmt_hex(addresses[0])}")
            return
        if kind == "bits":
            value = int(str(new_value).strip(), 0)
            shift = int(descriptor.get("shift") or 0)
            mask = int(descriptor.get("mask") or 0)
            maximum = mask >> shift
            if not 0 <= value <= maximum:
                raise ValueError(f"Enter a value from 0 to {maximum}")
            current = _read_live_u32(addresses[0])
            updated = (current & ~mask) | ((value << shift) & mask)
            if not _write_u32(addresses[0], updated & 0xFFFFFFFF):
                raise RuntimeError(f"Could not write {_fmt_hex(addresses[0])}")
            return
        if kind == "f32":
            ok = _write_f32(addresses[0], float(new_value))
        elif kind == "u8":
            ok = _write_u8(addresses[0], int(new_value, 0))
        elif kind == "u16":
            ok = _write_u16(addresses[0], int(new_value, 0))
        elif kind == "u32":
            ok = _write_u32(addresses[0], int(new_value, 0) & 0xFFFFFFFF)
        else:
            ok = _write_u32(addresses[0], int(new_value, 0) & 0xFFFFFFFF)
        if not ok:
            raise RuntimeError(f"Could not write {_fmt_hex(addresses[0])}")

    def _edit_live_iid(self, iid: str):
        detail = self._live_detail_by_iid.get(iid)
        descriptor = self._live_edit_descriptor(detail)
        if descriptor is None:
            return
        addresses = descriptor.get("addresses", [])
        current = str((detail or {}).get("value") or "")
        new_value = simpledialog.askstring(
            "Edit live projectile field",
            f"{(detail or {}).get('label') or 'Field'}\nAddress: " + ", ".join(_fmt_hex(a) for a in addresses),
            initialvalue=current,
            parent=self.root,
        )
        if new_value is None:
            return
        try:
            self._write_live_descriptor(descriptor, new_value)
        except Exception as exc:
            messagebox.showerror("Write failed", str(exc), parent=self.root)
            return
        self._status_var.set("Wrote live field at " + ", ".join(_fmt_hex(a) for a in addresses))

    def _on_live_double_click(self, event):
        iid = self.live_tree.identify_row(event.y)
        if iid:
            self._edit_live_iid(iid)

    def _on_live_right_click(self, event):
        iid = self.live_tree.identify_row(event.y)
        if not iid:
            return
        detail = self._live_detail_by_iid.get(iid)
        if not detail:
            return
        descriptor = self._live_edit_descriptor(detail)
        value = str(detail.get("value") or "")
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Copy value", command=lambda: self._copy(value))
        if descriptor:
            for index, address in enumerate(descriptor.get("addresses", [])):
                suffix = f" {index + 1}" if len(descriptor.get("addresses", [])) > 1 else ""
                menu.add_command(
                    label=f"Copy address{suffix} ({_fmt_hex(address)})",
                    command=lambda a=address: self._copy(_fmt_hex(a)),
                )
            menu.add_separator()
            menu.add_command(label="Edit value", command=lambda: self._edit_live_iid(iid))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _write_static_edit_spec(self, spec: dict, new_value: str):
        edit = spec.get("edit")
        if not edit:
            raise ValueError("No mapped address")
        address, typ, _offset = edit
        definition = spec.get("definition") or {}
        key = str(spec.get("key") or "")
        fmt = str(definition.get("fmt") or "")
        parsed = None
        display = ""
        if typ == "damage":
            parsed = int(new_value, 0)
            if not 0 <= parsed <= 0xFFFFFFFF:
                raise ValueError("damage out of range")
            base = int(definition.get("addr") or 0)
            resolved = int(definition.get("dmg_write_addr") or 0)
            fallback = base + _dmg_write_offset(fmt) if base else 0
            if fmt == "super_beam_card" and resolved:
                ok = _write_u32(resolved, parsed)
            elif fmt in ("super_struct", "super_struct_card", "super_struct_card2") and resolved:
                if parsed > 0xFFFF:
                    raise ValueError("this super damage field is u16")
                ok = _write_u16(resolved, parsed)
            elif resolved and resolved != fallback:
                ok = _write_u32(resolved, parsed)
            elif base:
                ok = _write_dmg(base, parsed, fmt)
            else:
                ok = False
            display = str(parsed)
        elif typ == "f32":
            parsed = float(new_value)
            ok = _write_f32(int(address), parsed)
            display = f"{parsed:g}"
        elif typ == "u8":
            parsed = int(new_value, 0)
            if not 0 <= parsed <= 0xFF:
                raise ValueError("u8 out of range")
            ok = _write_u8(int(address), parsed)
            display = str(parsed)
        elif typ == "raw32":
            text = str(new_value).strip()
            if text.lower().startswith("f:"):
                parsed = float(text[2:].strip())
                ok = _write_f32(int(address), parsed)
                display = f"f32 {parsed:g}"
            else:
                # Accept a bare integer/hex word or the first token from the
                # displayed composite raw value.
                token = text.split("|")[0].strip()
                parsed = int(token, 0)
                if not 0 <= parsed <= 0xFFFFFFFF:
                    raise ValueError("raw32 out of range")
                ok = _write_u32(int(address), parsed)
                display = f"0x{parsed:08X}"
        elif typ == "u16":
            parsed = int(new_value, 0)
            if not 0 <= parsed <= 0xFFFF:
                raise ValueError("u16 out of range")
            ok = _write_u16(int(address), parsed)
            display = str(parsed)
        else:
            parsed = int(new_value, 0)
            if not -(1 << 31) <= parsed <= 0xFFFFFFFF:
                raise ValueError("u32/s32 out of range")
            ok = _write_u32(int(address), parsed & 0xFFFFFFFF)
            display = str(parsed)
        if not ok:
            raise RuntimeError("Could not write the projectile field to Dolphin")
        row = spec.get("row")
        if row in self._definition_by_iid:
            self._definition_by_iid[row][key] = parsed
            if key == "dmg":
                self.roster.set(row, "damage", display)
        self._status_var.set(f"Wrote {display} to {_fmt_hex(int(address))}")
        return parsed, display

    def _on_static_double_click(self, event):
        iid = self.static_tree.identify_row(event.y)
        if iid:
            self._edit_static_iid(iid)

    def _edit_selected_static(self):
        selection = self.static_tree.selection()
        if selection:
            self._edit_static_iid(selection[0])

    def _edit_static_iid(self, iid: str):
        edit = self._static_edit_by_iid.get(iid)
        if not edit:
            return
        current = str(self.static_tree.set(iid, "value") or "")
        new_value = simpledialog.askstring(
            "Edit static projectile field",
            f"{self.static_tree.item(iid, 'text')}\nAddress: {_fmt_hex(edit['address'])}\nType: {edit['type']}",
            initialvalue=current,
            parent=self.root,
        )
        if new_value is None:
            return
        spec = {
            "edit": (int(edit["address"]), str(edit["type"]), ""),
            "definition": edit.get("definition") or {},
            "key": edit.get("key"),
            "row": edit.get("row"),
        }
        try:
            _parsed, display = self._write_static_edit_spec(spec, new_value)
        except Exception as exc:
            messagebox.showerror("Write failed", str(exc), parent=self.root)
            return
        self.static_tree.set(iid, "value", display)
        if self._selected_row:
            self._render_static_summary(self._selected_row)
        self._refresh_selected_panel()

    def _on_static_right_click(self, event):
        iid = self.static_tree.identify_row(event.y)
        if not iid:
            return
        edit = self._static_edit_by_iid.get(iid)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Copy value", command=lambda: self._copy(str(self.static_tree.set(iid, "value") or "")))
        if edit:
            menu.add_command(label=f"Copy address ({_fmt_hex(edit['address'])})", command=lambda: self._copy(_fmt_hex(edit["address"])))
            menu.add_separator()
            menu.add_command(label="Edit value", command=lambda: self._edit_static_iid(iid))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_research_right_click(self, event):
        iid = self.research_tree.identify_row(event.y)
        if not iid:
            return
        value = str(self.research_tree.set(iid, "value") or "")
        address = str(self.research_tree.set(iid, "address") or "")
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Copy value", command=lambda: self._copy(value))
        if address:
            menu.add_command(label="Copy address", command=lambda: self._copy(address))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy(self, text: str):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception:
            pass

_profile_monitor_instances: dict[str, ProjectileProfileMonitorWindow] = {}


def open_projectile_profile_monitor(slot_label: str, get_snap_fn, get_all_snap_fn=None):
    """Open or focus the projectile monitor for one HUD character slot."""
    slot_label = str(slot_label)

    def create(master):
        existing = _profile_monitor_instances.get(slot_label)
        if existing is not None:
            try:
                existing.root.lift()
                existing.root.focus_force()
                return
            except Exception:
                _profile_monitor_instances.pop(slot_label, None)
        inst = ProjectileProfileMonitorWindow(master, slot_label, get_snap_fn, get_all_snap_fn)
        _profile_monitor_instances[slot_label] = inst

        old_close = inst._on_close
        def close_and_forget():
            _profile_monitor_instances.pop(slot_label, None)
            old_close()
        inst._on_close = close_and_forget
        inst.root.protocol("WM_DELETE_WINDOW", close_and_forget)

    tk_call(create)


# ---------------------------------------------------------------------------
_inst = None

def open_proj_scanner_window(get_active_fn):
    def _c(master):
        global _inst
        if _inst:
            try:
                _inst.root.lift()
                return
            except Exception:
                pass
        _inst = ProjScannerWindow(master, get_active_fn)
    tk_call(_c)
