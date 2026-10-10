from __future__ import annotations

"""Resolve native attack-property phases from TvC action scripts.

The live collision record is too short-lived for an external Python poll, but
its persistent source is the fighter's current native action script:

* fighter + 0x1E0: character action table
* field 0x240: Property A / hit-result word
* field 0x244: Property B contact-routing word

The script does more than SET those fields. It later applies OR and CLEAR
operations to build result behavior. This parser replays those operations in
script order, preserves every mutation, and separates the initial guard/tier
byte from the later hit-result word instead of collapsing them into one value.
"""

import struct
import threading
import time
from typing import Callable, Optional

try:
    from tvcgui.platform.dolphin import rd32 as _live_rd32, rbytes as _live_rbytes
except Exception:
    def _live_rd32(_address: int):
        return None

    def _live_rbytes(_address: int, _size: int):
        return b""

FIGHTER_CHR_TABLE_OFFSET = 0x01E0

# Persistent spawned attack actors. Unlike fighter collision packets, these
# actors remain registered while the spawned attack object exists, so an external poll can
# harvest their linked native Property A/B record without a code hook.
LIVE_PROJECTILE_ACTOR_TABLE = 0x80476E50
LIVE_PROJECTILE_ACTOR_TABLE_COUNT = 64
LIVE_PROJECTILE_ACTOR_SIZE = 0x1A4
LIVE_PROJECTILE_OWNER_OFFSET = 0x0130
LIVE_PROJECTILE_ID_OFFSET = 0x0134
LIVE_PROJECTILE_LINKED_OFFSET = 0x013C
LIVE_PROJECTILE_LINKED_READ_SIZE = 0x0364
LIVE_PROJECTILE_PROPERTY_A_OFFSET = 0x0080
LIVE_PROJECTILE_PROPERTY_B_OFFSET = 0x0084
LIVE_PROJECTILE_LINKED_OWNER_OFFSET = 0x0030
LIVE_PROJECTILE_TARGET_OFFSET = 0x0034
LIVE_PROJECTILE_RUNTIME_STATUS_OFFSET = 0x0020
LIVE_PROJECTILE_PHASE_A_OFFSET = 0x035C
LIVE_PROJECTILE_PHASE_B_OFFSET = 0x0360
FIGHTER_ACTION_ID_OFFSET = 0x01E8
MEM2_LO = 0x90000000
MEM2_HI = 0x94000000
TABLE_READ_SIZE = 0x1200
TABLE_MAX_ENTRIES = TABLE_READ_SIZE // 4
MOVE_REL_MIN = 0x3600
MOVE_REL_MAX = 0x90000
MAX_ACTION_SCAN = 0x4000
MIN_ACTION_SCAN = 0x40

SCRIPT_VALUE_MARKER = 0x3F000000
SCRIPT_FIELD_PROPERTY_A = 0x00000240
SCRIPT_FIELD_PROPERTY_B = 0x00000244
SCRIPT_OP_SET = 0x01
SCRIPT_OP_OR = 0x15
SCRIPT_OP_CLEAR = 0x17
SCRIPT_OPERATION_NAMES = {
    SCRIPT_OP_SET: "SET",
    SCRIPT_OP_OR: "OR",
    SCRIPT_OP_CLEAR: "CLEAR",
}

# Native scripts use this exact clear-then-OR pair to replace the hit-result
# portion of Property A. The OR value is stored byte-shifted; value >> 8 is the
# reaction/result code used by TvCGUI's existing native reaction dictionary.
HIT_RESULT_CLEAR_MASK = 0x80042F00
PROPERTY_A_CLASS_MASK = 0x0000003F

_LOCK = threading.RLock()
_TABLE_CACHE: dict[int, tuple[dict[int, int], tuple[int, ...], int]] = {}
_TABLE_GENERATION: dict[int, int] = {}
_DEFINITION_CACHE: dict[tuple[int, int, int], dict] = {}
_FAILED_UNTIL: dict[tuple[int, int, int], float] = {}


def _is_mem2(value: int) -> bool:
    return MEM2_LO <= int(value or 0) < MEM2_HI


def _read_word(reader: Callable[[int], Optional[int]], address: int) -> Optional[int]:
    try:
        value = reader(int(address))
    except Exception:
        return None
    if value is None:
        return None
    return int(value) & 0xFFFFFFFF


def _read_bytes(reader: Callable[[int, int], bytes], address: int, size: int) -> bytes:
    try:
        return bytes(reader(int(address), int(size)) or b"")
    except Exception:
        return b""




def _blob_u32(blob: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(blob):
        return 0
    return struct.unpack_from(">I", blob, offset)[0]


def _runtime_property_layout(raw_80: int, raw_84: int) -> tuple[int, int, str]:
    """Return the confirmed native spawned-actor Property A/B orientation.

    Across Hadouken, Kikoken, Shinkuu, and Kikosho, +0x80 consistently carries
    a valid packed guard/strength class while +0x84 carries contact-routing
    flags. The deferred words at +0x35C/+0x360 follow the same A/B ordering,
    although they are queued definitions and are not guaranteed to be copied
    into the live +0x80/+0x84 pair before the actor is released.
    """
    raw_80 &= 0xFFFFFFFF
    raw_84 &= 0xFFFFFFFF
    if _is_attack_class(raw_80):
        return raw_80, raw_84, "A80_B84_CONFIRMED"
    return 0, 0, "UNRESOLVED"


def collect_live_projectile_properties(
    owner_bases: dict[str, int],
    *,
    read_u32: Callable[[int], Optional[int]] | None = None,
    read_block: Callable[[int, int], bytes] | None = None,
) -> list[dict]:
    """Return native Property A/B from active spawned attack actors.

    Fighter action scripts stop being authoritative once they spawn an
    independent attack object. Active spawned actors are registered through
    the persistent pointer table at 0x80476E50 and link to their own collision
    record at actor +0x13C. Only registered actors are accepted here. Freed-pool
    residue is deliberately excluded from the player-facing badge.

    This is a direct native-memory harvest. It does not read TvCGUI profile
    data, inferred damage tables, or observed frame data.
    """
    word_reader = read_u32 or _live_rd32
    block_reader = read_block or _live_rbytes
    reverse_owners = {
        int(base): str(slot)
        for slot, base in (owner_bases or {}).items()
        if _is_mem2(int(base or 0))
    }
    if not reverse_owners:
        return []

    table = _read_bytes(
        block_reader,
        LIVE_PROJECTILE_ACTOR_TABLE,
        LIVE_PROJECTILE_ACTOR_TABLE_COUNT * 4,
    )
    table_ptrs: set[int] = set()
    for index in range(min(LIVE_PROJECTILE_ACTOR_TABLE_COUNT, len(table) // 4)):
        actor = _blob_u32(table, index * 4)
        if _is_mem2(actor):
            table_ptrs.add(actor)

    actor_blobs: dict[int, tuple[bytes, str]] = {}
    for actor in table_ptrs:
        blob = _read_bytes(block_reader, actor, LIVE_PROJECTILE_ACTOR_SIZE)
        if len(blob) == LIVE_PROJECTILE_ACTOR_SIZE:
            actor_blobs[actor] = (blob, "actor_table")

    rows: list[dict] = []
    for actor, (actor_blob, registry_source) in actor_blobs.items():
        owner = _blob_u32(actor_blob, LIVE_PROJECTILE_OWNER_OFFSET)
        owner_slot = reverse_owners.get(owner)
        if owner_slot is None:
            continue
        projectile_id = _blob_u32(actor_blob, LIVE_PROJECTILE_ID_OFFSET)
        if not (1 <= projectile_id <= 0xFFFF):
            continue
        linked = _blob_u32(actor_blob, LIVE_PROJECTILE_LINKED_OFFSET)
        if not _is_mem2(linked):
            continue
        linked_blob = _read_bytes(block_reader, linked, LIVE_PROJECTILE_LINKED_READ_SIZE)
        if len(linked_blob) < LIVE_PROJECTILE_PROPERTY_B_OFFSET + 4:
            continue
        linked_owner = _blob_u32(linked_blob, LIVE_PROJECTILE_LINKED_OWNER_OFFSET)
        raw_80 = _blob_u32(linked_blob, LIVE_PROJECTILE_PROPERTY_A_OFFSET)
        raw_84 = _blob_u32(linked_blob, LIVE_PROJECTILE_PROPERTY_B_OFFSET)
        property_a, property_b, property_layout = _runtime_property_layout(raw_80, raw_84)
        if not _is_attack_class(property_a):
            continue

        rows.append({
            "source": "live_attack_actor",
            "registry_source": registry_source,
            "owner_slot": owner_slot,
            "owner_base": owner,
            "owner_action_id": int(_read_word(word_reader, owner + FIGHTER_ACTION_ID_OFFSET) or 0),
            "projectile_id": projectile_id,
            "actor": actor,
            "linked": linked,
            "linked_owner": linked_owner,
            "runtime_status_20": _blob_u32(linked_blob, LIVE_PROJECTILE_RUNTIME_STATUS_OFFSET),
            "target": _blob_u32(linked_blob, LIVE_PROJECTILE_TARGET_OFFSET),
            "property_layout": property_layout,
            "raw_property_80": raw_80,
            "raw_property_84": raw_84,
            "property_a": property_a & 0xFFFFFFFF,
            "property_b": property_b & 0xFFFFFFFF,
            "phase_property_a": _blob_u32(linked_blob, LIVE_PROJECTILE_PHASE_A_OFFSET),
            "phase_property_b": _blob_u32(linked_blob, LIVE_PROJECTILE_PHASE_B_OFFSET),
        })

    rows.sort(key=lambda row: (
        str(row.get("owner_slot") or ""),
        int(row.get("projectile_id") or 0),
        int(row.get("actor") or 0),
    ))
    return rows


def _parse_table_blob(root: int, blob: bytes) -> tuple[dict[int, int], tuple[int, ...]]:
    entries: dict[int, int] = {}
    roots: set[int] = set()
    limit = min(len(blob) // 4, TABLE_MAX_ENTRIES)
    for action_id in range(limit):
        rel = struct.unpack_from(">I", blob, action_id * 4)[0]
        if rel == 0xFFFFFFFF:
            break
        if rel == 0 or rel & 3 or rel < MOVE_REL_MIN or rel > MOVE_REL_MAX:
            continue
        move_root = int(root) + int(rel)
        if not _is_mem2(move_root):
            continue
        entries[action_id] = move_root
        roots.add(move_root)
    return entries, tuple(sorted(roots))


def _table_entry_root(
    root: int,
    action_id: int,
    read_block: Callable[[int, int], bytes],
) -> int:
    """Read one live action-table entry without consulting the cache."""
    action = int(action_id)
    if action < 0 or action >= TABLE_MAX_ENTRIES:
        return 0
    blob = _read_bytes(read_block, int(root) + action * 4, 4)
    if len(blob) != 4:
        return 0
    rel = struct.unpack_from(">I", blob, 0)[0]
    if rel in (0, 0xFFFFFFFF) or rel & 3 or rel < MOVE_REL_MIN or rel > MOVE_REL_MAX:
        return 0
    move_root = int(root) + int(rel)
    return move_root if _is_mem2(move_root) else 0


def _invalidate_table_root_locked(root: int) -> None:
    root = int(root)
    _TABLE_CACHE.pop(root, None)
    for key in list(_DEFINITION_CACHE):
        if key[0] == root:
            _DEFINITION_CACHE.pop(key, None)
    for key in list(_FAILED_UNTIL):
        if key[0] == root:
            _FAILED_UNTIL.pop(key, None)


def _table_entries(
    root: int,
    read_block: Callable[[int, int], bytes],
    *,
    validation_actions: tuple[int, ...] = (),
) -> tuple[dict[int, int], tuple[int, ...], int]:
    """Return the current table and invalidate reused-root cache entries.

    TvC reuses the same MEM2 action-table address when training-mode character
    selection changes. Caching only by the table pointer therefore lets the old
    character's move roots survive under the new character name. Validate a few
    live entries on every lookup and advance a generation whenever the table
    contents change.
    """
    root = int(root)
    anchors = tuple(dict.fromkeys(
        int(action) for action in (*validation_actions, 0x0100, 0x0101, 0x0102, 0x0103)
        if 0 <= int(action) < TABLE_MAX_ENTRIES
    ))
    with _LOCK:
        cached = _TABLE_CACHE.get(root)
    if cached is not None:
        entries, sorted_roots, generation = cached
        matches = True
        for action in anchors:
            if _table_entry_root(root, action, read_block) != int(entries.get(action, 0) or 0):
                matches = False
                break
        if matches:
            return entries, sorted_roots, generation
        with _LOCK:
            _invalidate_table_root_locked(root)

    blob = _read_bytes(read_block, root, TABLE_READ_SIZE)
    parsed_entries, parsed_roots = _parse_table_blob(root, blob) if len(blob) >= 4 else ({}, ())
    with _LOCK:
        generation = int(_TABLE_GENERATION.get(root, 0))
        if parsed_entries:
            generation += 1
            _TABLE_GENERATION[root] = generation
            _TABLE_CACHE[root] = (parsed_entries, parsed_roots, generation)
    return parsed_entries, parsed_roots, generation


def _action_span(move_root: int, sorted_roots: tuple[int, ...]) -> int:
    next_root = next((candidate for candidate in sorted_roots if candidate > move_root), 0)
    if next_root:
        return max(MIN_ACTION_SCAN, min(MAX_ACTION_SCAN, next_root - move_root))
    return MAX_ACTION_SCAN


def _is_attack_class(value: int) -> bool:
    packed = int(value) & 0xFF
    guard = packed & 0x38
    strength = packed & 0x07
    return guard in (0x00, 0x08, 0x10, 0x20) and strength in (0x01, 0x02, 0x04)


def _script_property_commands(blob: bytes, move_root: int) -> list[dict]:
    """Decode every supported Property A/B mutation in script order."""
    commands: list[dict] = []
    for offset in range(max(0, len(blob) - 15)):
        if blob[offset] != 0x04 or blob[offset + 2:offset + 4] != b"\x60\x00":
            continue
        marker = struct.unpack_from(">I", blob, offset + 8)[0]
        if marker != SCRIPT_VALUE_MARKER:
            continue
        operation = int(blob[offset + 1])
        if operation not in SCRIPT_OPERATION_NAMES:
            continue
        field_id = struct.unpack_from(">I", blob, offset + 4)[0]
        if field_id not in {SCRIPT_FIELD_PROPERTY_A, SCRIPT_FIELD_PROPERTY_B}:
            continue
        value = struct.unpack_from(">I", blob, offset + 12)[0]
        commands.append({
            "offset": int(offset),
            "address": int(move_root + offset + 12),
            "packet_address": int(move_root + offset),
            "operation": operation,
            "operation_name": SCRIPT_OPERATION_NAMES[operation],
            "field_id": int(field_id),
            "field_name": "A" if field_id == SCRIPT_FIELD_PROPERTY_A else "B",
            "value": int(value) & 0xFFFFFFFF,
        })
    return commands


def _apply_operation(current: int, operation: int, value: int) -> int:
    current = int(current) & 0xFFFFFFFF
    value = int(value) & 0xFFFFFFFF
    if operation == SCRIPT_OP_SET:
        return value
    if operation == SCRIPT_OP_OR:
        return (current | value) & 0xFFFFFFFF
    if operation == SCRIPT_OP_CLEAR:
        return (current & ~value) & 0xFFFFFFFF
    return current


def _build_property_phases(commands: list[dict]) -> list[dict]:
    """Replay Property A/B commands and preserve each native attack phase.

    A phase begins at a valid SET of field 0x240. All following A/B mutations
    belong to that phase until the next valid class SET. The exact
    clear(0x80042F00) + OR pair is extracted separately as the native hit-result
    word; later A mutations remain visible as raw modifiers rather than being
    misrepresented as part of guard height.
    """
    phases: list[dict] = []
    current: dict | None = None
    running_a = 0
    running_b = 0

    def finish() -> None:
        nonlocal current
        if current is None:
            return
        final_a = int(running_a) & 0xFFFFFFFF
        final_b = int(running_b) & 0xFFFFFFFF
        result_addr = int(current.get("hit_result_addr") or 0)
        all_a_or_mask = 0
        all_a_clear_mask = 0
        post_result_a_or_mask = 0
        for operation_row in current["property_a_modifiers"]:
            operation = int(operation_row.get("operation") or 0)
            value = int(operation_row.get("value") or 0) & 0xFFFFFFFF
            if operation == SCRIPT_OP_OR:
                all_a_or_mask |= value
                if result_addr and int(operation_row.get("address") or 0) > result_addr:
                    post_result_a_or_mask |= value
            elif operation == SCRIPT_OP_CLEAR:
                all_a_clear_mask |= value

        current["property_a_final"] = final_a
        current["property_a_final_unknown_mask"] = final_a & ~PROPERTY_A_CLASS_MASK
        current["property_a_all_or_mask"] = all_a_or_mask & 0xFFFFFFFF
        current["property_a_all_clear_mask"] = all_a_clear_mask & 0xFFFFFFFF
        current["property_a_post_result_or_mask"] = post_result_a_or_mask & 0xFFFFFFFF
        current["property_b"] = final_b
        current["property_b_final"] = final_b
        current["operation_count"] = len(current["operations"])
        current["property_a_modifier_count"] = len(current["property_a_modifiers"])
        current["property_b_modifier_count"] = len(current["property_b_modifiers"])
        phases.append(current)
        current = None

    index = 0
    while index < len(commands):
        row = commands[index]
        operation = int(row["operation"])
        field_id = int(row["field_id"])
        value = int(row["value"]) & 0xFFFFFFFF

        starts_phase = (
            field_id == SCRIPT_FIELD_PROPERTY_A
            and operation == SCRIPT_OP_SET
            and _is_attack_class(value)
        )
        if starts_phase:
            finish()
            running_a = value
            current = {
                "phase_index": len(phases) + 1,
                "script_offset": int(row["offset"]),
                "property_a": value & 0xFF,
                "property_a_initial": value,
                "property_a_initial_unknown_mask": value & ~PROPERTY_A_CLASS_MASK,
                "property_a_addr": int(row["address"]),
                "property_b": int(running_b) & 0xFFFFFFFF,
                "property_b_initial": int(running_b) & 0xFFFFFFFF,
                "property_b_addr": 0,
                "result_clear_mask": 0,
                "hit_result_raw": None,
                "a_result_flags_raw": None,
                "a_result_code": None,
                # Compatibility alias retained for older overlay bridges.
                "hit_reaction": None,
                "hit_result_addr": 0,
                "operations": [dict(row)],
                "property_a_modifiers": [],
                "property_b_modifiers": [],
            }
            index += 1
            continue

        if current is None:
            # Track B state before the first phase in case a script initializes
            # it early, but do not report unrelated preamble operations.
            if field_id == SCRIPT_FIELD_PROPERTY_A:
                running_a = _apply_operation(running_a, operation, value)
            else:
                running_b = _apply_operation(running_b, operation, value)
            index += 1
            continue

        current["operations"].append(dict(row))
        if field_id == SCRIPT_FIELD_PROPERTY_A:
            running_a = _apply_operation(running_a, operation, value)
            current["property_a_modifiers"].append(dict(row))

            # The result mutation is a precise two-command native idiom.
            if operation == SCRIPT_OP_CLEAR and value == HIT_RESULT_CLEAR_MASK:
                current["result_clear_mask"] = value
                if index + 1 < len(commands):
                    next_row = commands[index + 1]
                    if (
                        int(next_row["offset"]) == int(row["offset"]) + 0x10
                        and int(next_row["field_id"]) == SCRIPT_FIELD_PROPERTY_A
                        and int(next_row["operation"]) == SCRIPT_OP_OR
                    ):
                        result_raw = int(next_row["value"]) & 0xFFFFFFFF
                        result_code = (result_raw >> 8) & 0x00FFFFFF
                        current["hit_result_raw"] = result_raw
                        current["a_result_flags_raw"] = result_raw
                        current["a_result_code"] = result_code
                        current["hit_reaction"] = result_code
                        current["hit_result_addr"] = int(next_row["address"])
        else:
            previous_b = running_b
            running_b = _apply_operation(running_b, operation, value)
            current["property_b_modifiers"].append(dict(row))
            if operation == SCRIPT_OP_SET and not current.get("property_b_addr"):
                current["property_b_initial"] = value
                current["property_b_addr"] = int(row["address"])
            elif not current.get("property_b_addr") and running_b != previous_b:
                current["property_b_addr"] = int(row["address"])

        index += 1

    finish()
    return phases



def parse_attack_definition_blob(blob: bytes, move_root: int, *, scan_size: int | None = None) -> dict:
    """Decode Property A/B phases from an already-read action-script blob.

    This is the zero-I/O form used by the static frame-data scanner. It keeps
    profile construction from rereading Dolphin memory solely to expose the
    native Property A/B mutation chain.
    """
    try:
        root = int(move_root or 0)
    except Exception:
        root = 0
    data = bytes(blob or b"")
    size = int(scan_size if scan_size is not None else len(data))
    commands = _script_property_commands(data, root)
    phases = _build_property_phases(commands)
    if not phases:
        return {
            "status": "NO_PROPERTY_COMMAND",
            "move_root": root,
            "scan_size": size,
            "command_count": len(commands),
            "phase_count": 0,
            "phases": [],
            "operations": commands,
        }
    first = phases[0]
    return {
        "status": "OK",
        "move_root": root,
        "scan_size": size,
        "command_count": len(commands),
        "property_a": int(first["property_a"]) & 0xFF,
        "property_b": int(first["property_b"]) & 0xFFFFFFFF,
        "property_a_addr": int(first["property_a_addr"]),
        "property_b_addr": int(first["property_b_addr"]),
        "phase_count": len(phases),
        "phases": phases,
        "operations": commands,
    }

def resolve_live_attack_definition(
    fighter_base_abs: int,
    action_id: int,
    *,
    chr_tbl_abs: int | None = None,
    read_u32: Callable[[int], Optional[int]] | None = None,
    read_block: Callable[[int, int], bytes] | None = None,
) -> dict:
    """Return the current action's native Property A/B script definition."""
    word_reader = read_u32 or _live_rd32
    block_reader = read_block or _live_rbytes
    try:
        base = int(fighter_base_abs or 0)
        action = int(action_id)
    except Exception:
        return {"status": "BAD_ARGUMENT"}
    if not base:
        return {"status": "NO_FIGHTER", "action_id": action}
    if action < 0:
        return {"status": "BAD_ACTION", "action_id": action}

    root = int(chr_tbl_abs or 0)
    if not root:
        root = int(_read_word(word_reader, base + FIGHTER_CHR_TABLE_OFFSET) or 0)
    if not _is_mem2(root):
        return {
            "status": "NO_CHARACTER_TABLE",
            "fighter_base": base,
            "action_id": action,
            "chr_tbl": root,
        }

    entries, sorted_roots, table_generation = _table_entries(
        root,
        block_reader,
        validation_actions=(action,),
    )
    key = (root, action, table_generation)
    with _LOCK:
        cached = _DEFINITION_CACHE.get(key)
        if cached is not None:
            return dict(cached)
        if time.monotonic() < float(_FAILED_UNTIL.get(key, 0.0) or 0.0):
            return {
                "status": "RETRY_PENDING",
                "fighter_base": base,
                "action_id": action,
                "chr_tbl": root,
                "table_generation": table_generation,
            }

    if not entries:
        result = {
            "status": "TABLE_READ_FAILED",
            "fighter_base": base,
            "action_id": action,
            "chr_tbl": root,
            "table_generation": table_generation,
        }
    else:
        move_root = int(entries.get(action, 0))
        if not move_root:
            result = {
                "status": "NO_ACTION_ENTRY",
                "fighter_base": base,
                "action_id": action,
                "chr_tbl": root,
                "table_generation": table_generation,
            }
        else:
            scan_size = _action_span(move_root, sorted_roots)
            blob = _read_bytes(block_reader, move_root, scan_size)
            parsed = parse_attack_definition_blob(blob, move_root, scan_size=scan_size)
            result = dict(parsed)
            result.update({
                "fighter_base": base,
                "action_id": action,
                "chr_tbl": root,
                "table_generation": table_generation,
            })

    with _LOCK:
        if result.get("status") == "OK":
            _DEFINITION_CACHE[key] = dict(result)
            _FAILED_UNTIL.pop(key, None)
        else:
            _FAILED_UNTIL[key] = time.monotonic() + 0.20
    return result


def resolve_live_attack_property(
    fighter_base_abs: int,
    action_id: int,
    *,
    chr_tbl_abs: int | None = None,
    read_u32: Callable[[int], Optional[int]] | None = None,
    read_block: Callable[[int, int], bytes] | None = None,
) -> Optional[int]:
    definition = resolve_live_attack_definition(
        fighter_base_abs,
        action_id,
        chr_tbl_abs=chr_tbl_abs,
        read_u32=read_u32,
        read_block=read_block,
    )
    value = definition.get("property_a")
    return None if value is None else int(value) & 0xFF


def clear_attack_property_runtime_cache() -> None:
    with _LOCK:
        _TABLE_CACHE.clear()
        _TABLE_GENERATION.clear()
        _DEFINITION_CACHE.clear()
        _FAILED_UNTIL.clear()


__all__ = [
    "HIT_RESULT_CLEAR_MASK",
    "SCRIPT_FIELD_PROPERTY_A",
    "SCRIPT_FIELD_PROPERTY_B",
    "SCRIPT_OP_SET",
    "SCRIPT_OP_OR",
    "SCRIPT_OP_CLEAR",
    "collect_live_projectile_properties",
    "parse_attack_definition_blob",
    "resolve_live_attack_definition",
    "resolve_live_attack_property",
    "clear_attack_property_runtime_cache",
]
