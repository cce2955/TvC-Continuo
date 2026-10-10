# char_dumper.py
#
# Read-only character dump helper for the Frame Data Workbench.
# This gives us an assist-scanner-style one-button dump for whatever
# character/slot is currently open: move heads, command signatures,
# projectile/template rows, super beam-card candidates, and raw chunks.

from __future__ import annotations

import json
import os
import re
import struct
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

MEM2_START = 0x90000000
MEM2_END = 0x94000000
MOVE_HEAD_LEN = 0x400
MOVE_SCAN_LEN = 0x500
REGION_PAD_BEFORE = 0x4000
REGION_PAD_AFTER = 0x300000
REGION_MAX_SIZE = 0x700000

SUPER_BEAM_SIG = b"\x00\x00\x00\x0C\x00\x00\x00\x23"
PROJECTILE_ANCHOR = b"\x00\x04\x01\x02\x00\x01\x00"

# Known command pairs that are useful when staring at a raw move stream.
COMMAND_NAMES: dict[tuple[int, int], str] = {
    (0x01, 0x33): "post-animation/script link",
    (0x01, 0x34): "script/link reference",
    (0x01, 0x3C): "phase/anim record link",
    (0x04, 0x01): "direct field write",
    (0x04, 0x03): "field op",
    (0x04, 0x15): "field OR / hit-result mutation",
    (0x04, 0x17): "field CLEAR / hit-result mutation",
    (0x07, 0x01): "field write/control op",
    (0x11, 0x16): "control marker",
    (0x11, 0x1E): "control packet",
    (0x11, 0x22): "control packet",
    (0x32, 0x56): "action/control marker",
    (0x33, 0x03): "phase/action marker",
    (0x33, 0x32): "hitstop",
    (0x33, 0x35): "speed modifier",
    (0x33, 0x38): "paired action parameter",
    (0x34, 0x04): "effect/control packet",
    (0x34, 0x3D): "effect/control packet",
    (0x34, 0x40): "reach/stretch/effect scale packet",
    (0x34, 0x41): "effect/control marker",
    (0x35, 0x01): "active/window",
    (0x35, 0x05): "hit spark / hitdata behavior packet",
    (0x35, 0x07): "knockback packet (alternate form)",
    (0x35, 0x09): "knockback packet",
    (0x35, 0x0C): "hit push/pull packet",
    (0x35, 0x0A): "hitdata phase/reset marker",
    (0x35, 0x0D): "hitbox geometry packet",
    (0x35, 0x10): "damage/power",
    (0x36, 0x43): "meter/resource packet",
}

SUPER_FIELDS: tuple[tuple[str, int, str], ...] = (
    ("damage", 0x010, "u32"),
    ("hit_count", 0x024, "u32"),
    ("hit_interval", 0x028, "u32"),
    ("beam_scale", 0x038, "f32"),
    ("beam_width", 0x03C, "f32"),
    ("particle_fx", 0x040, "u32"),
    ("hit_source", 0x060, "u32"),
    ("spawn_bone", 0x068, "u32"),
    ("lifetime", 0x084, "u32"),
    ("beam_speed", 0x090, "f32"),
    ("beam_force", 0x094, "f32"),
    ("hit_radius", 0x0E4, "f32"),
    ("beam_visual", 0x0E8, "u32"),
    ("final_damage", 0x110, "u32"),
    ("final_lifetime", 0x114, "u32"),
    ("final_particle_fx", 0x134, "u32"),
    ("final_spawn_bone", 0x154, "u32"),
)

MOVE_SUMMARY_KEYS = (
    "abs", "id", "move_name", "pretty_name", "name", "kind", "family_label",
    "damage", "damage_addr", "meter", "meter_addr",
    "startup", "active", "active2",
    "hitstun", "hitstun_addr", "blockstun", "blockstun_addr", "hitstop", "hitstop_addr",
    "attack_property", "attack_property_addr", "hit_reaction", "hit_reaction_addr",
    "kb_type", "kb_type_addr", "launch_profile", "launch_profile_addr", "kb_unknown", "kb_unknown_addr",
    "ground_kb", "ground_kb_addr", "ground_kb_y", "ground_kb_y_addr", "ground_kb_packet_addr", "ground_kb_mode", "ground_kb_aux", "push_pull_packets",
    "kb_x", "kb_x_addr", "air_kb", "air_kb_addr", "knockback_addr",
    "speed_mod", "speed_addr", "superbg_val", "superbg_addr",
    "hit_spark", "hit_spark_addr",
    "stretch_part", "stretch_part_addr", "stretch_len", "stretch_len_addr",
    "stretch_width", "stretch_width_addr", "stretch_height", "stretch_height_addr",
    "stretch_time", "stretch_time_addr", "post_link", "post_link_addr",
)


def _app_base_dir() -> str:
    try:
        if getattr(sys, "frozen", False):
            return os.path.dirname(os.path.abspath(sys.executable))
    except Exception:
        pass
    return str(Path(__file__).resolve().parents[3])


def _safe_name(text: Any) -> str:
    s = str(text or "unknown").strip().lower()
    s = re.sub(r"[^a-z0-9_.-]+", "_", s)
    s = s.strip("_")
    return s or "unknown"


def _get_rbytes():
    try:
        from tvcgui.platform.dolphin import rbytes as dio_rbytes  # type: ignore
        if dio_rbytes is not None:
            return dio_rbytes
    except Exception:
        pass
    try:
        import tvcgui.features.combat.projectile_scanner as P  # type: ignore
        if getattr(P, "rbytes", None) is not None:
            return P.rbytes
    except Exception:
        pass
    return None


def _read_bytes(addr: int, size: int, rbytes_func=None) -> bytes:
    if not addr or size <= 0:
        return b""
    if rbytes_func is None:
        rbytes_func = _get_rbytes()
    if rbytes_func is None:
        return b""
    try:
        data = rbytes_func(int(addr), int(size))
        return bytes(data or b"")
    except Exception:
        return b""


def _u8(data: bytes, off: int):
    try:
        return data[off]
    except Exception:
        return None


def _u16(data: bytes, off: int):
    try:
        return struct.unpack_from(">H", data, off)[0]
    except Exception:
        return None


def _u32(data: bytes, off: int):
    try:
        return struct.unpack_from(">I", data, off)[0]
    except Exception:
        return None


def _f32(data: bytes, off: int):
    try:
        val = struct.unpack_from(">f", data, off)[0]
        if val != val:
            return None
        return float(val)
    except Exception:
        return None


def _read_typed(data: bytes, off: int, typ: str):
    if typ == "u8":
        return _u8(data, off)
    if typ == "u16":
        return _u16(data, off)
    if typ == "u32":
        return _u32(data, off)
    if typ == "f32":
        return _f32(data, off)
    return None


def _hex(data: bytes, max_len: int = 64) -> str:
    return bytes(data[:max_len]).hex(" ").upper()


def _jsonable(value: Any, depth: int = 0):
    if depth > 4:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return _hex(value, 128)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, depth + 1) for v in list(value)[:200]]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "moves":
                continue
            if callable(v):
                continue
            try:
                out[str(k)] = _jsonable(v, depth + 1)
            except Exception:
                out[str(k)] = str(v)
        return out
    return str(value)


def _move_addr(mv: dict) -> int:
    try:
        return int(mv.get("abs") or 0)
    except Exception:
        return 0


def _valid_mem_addr(addr: int) -> bool:
    return MEM2_START <= int(addr) < MEM2_END


def _interesting_addrs(moves: list[dict], projectile_hits: list[dict]) -> list[int]:
    out = []
    for mv in moves:
        a = _move_addr(mv)
        if _valid_mem_addr(a):
            out.append(a)
        for key, value in mv.items():
            if not str(key).endswith("_addr"):
                continue
            try:
                ai = int(value or 0)
            except Exception:
                continue
            if _valid_mem_addr(ai):
                out.append(ai)
    for h in projectile_hits:
        for key in ("addr", "dmg_write_addr"):
            try:
                ai = int(h.get(key) or 0)
            except Exception:
                continue
            if _valid_mem_addr(ai):
                out.append(ai)
    return sorted(set(out))


def _scan_region_bounds(moves: list[dict], projectile_hits: list[dict]) -> tuple[int | None, int | None]:
    addrs = _interesting_addrs(moves, projectile_hits)
    if not addrs:
        return None, None
    lo = max(MEM2_START, (min(addrs) - REGION_PAD_BEFORE) & ~0xFFF)
    hi = min(MEM2_END, ((max(addrs) + REGION_PAD_AFTER + 0xFFF) & ~0xFFF))
    if hi <= lo:
        return lo, min(MEM2_END, lo + REGION_MAX_SIZE)
    if hi - lo > REGION_MAX_SIZE:
        # Keep the front of the character's table region and use per-candidate
        # chunks for anything later. This prevents one button from pulling huge
        # MEM2 slices.
        hi = lo + REGION_MAX_SIZE
    return lo, hi


def _command_hits_for_move(addr: int, data: bytes) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for i in range(0, max(0, min(len(data), MOVE_SCAN_LEN) - 1)):
        key = (data[i], data[i + 1])
        label = COMMAND_NAMES.get(key)
        if not label:
            continue
        hits.append({
            "offset": i,
            "addr": addr + i,
            "cmd": f"{key[0]:02X}/{key[1]:02X}",
            "label": label,
            "bytes": _hex(data[i:i + 24], 24),
        })
    return hits


def _move_summary(mv: dict, rbytes_func=None) -> dict[str, Any]:
    out = {k: _jsonable(mv.get(k)) for k in MOVE_SUMMARY_KEYS if k in mv}
    addr = _move_addr(mv)
    out["abs_hex"] = f"0x{addr:08X}" if addr else ""
    raw = _read_bytes(addr, MOVE_SCAN_LEN, rbytes_func) if addr else b""
    out["raw_head_hex"] = _hex(raw, 96) if raw else ""
    out["command_hits"] = _command_hits_for_move(addr, raw) if raw else []
    return out


def _command_histogram(move_summaries: list[dict[str, Any]]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for mv in move_summaries:
        for hit in mv.get("command_hits") or []:
            cmd = str(hit.get("cmd") or "")
            if cmd:
                hist[cmd] = hist.get(cmd, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])))


def _scan_super_beam_cards(region_start: int, data: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pos = 0
    seen: set[int] = set()
    while True:
        sig_i = data.find(SUPER_BEAM_SIG, pos)
        if sig_i < 0:
            break
        pos = sig_i + 1
        idx = sig_i - 8
        if idx < 0 or idx + 0x158 > len(data):
            continue
        addr = region_start + idx
        if addr in seen:
            continue
        seen.add(addr)
        card = data[idx:idx + 0x180]
        damage = _u32(card, 0x10)
        hit_count = _u32(card, 0x24)
        lifetime = _u32(card, 0x84)
        if damage is None or not (1 <= int(damage) <= 50000):
            continue
        if hit_count is not None and int(hit_count) not in (0xFFFFFFFF, 0xFFFFFFFE) and int(hit_count) > 0x20000:
            continue
        if lifetime is not None and int(lifetime) not in (0xFFFFFFFF, 0xFFFFFFFE) and int(lifetime) > 0x20000:
            continue
        fields = {}
        for name, off, typ in SUPER_FIELDS:
            fields[name] = _read_typed(card, off, typ)
        rows.append({
            "addr": addr,
            "addr_hex": f"0x{addr:08X}",
            "fmt": "super_beam_card",
            "fields": fields,
            "header_hex": _hex(card[:0x40], 0x40),
        })
    return rows


def _scan_projectile_templates(region_start: int, data: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pos = 0
    seen: set[int] = set()
    while True:
        i = data.find(PROJECTILE_ANCHOR, pos)
        if i < 0:
            break
        pos = i + 1
        if i in seen:
            continue
        seen.add(i)
        dmg_off = i + 7
        radius_off = dmg_off + 0x2C
        if radius_off + 4 > len(data):
            continue
        dmg = _u16(data, dmg_off)
        radius = _f32(data, radius_off)
        if dmg is None or radius is None:
            continue
        if not (1 <= int(dmg) <= 50000):
            continue
        if not (0.0 < float(radius) <= 2000.0):
            continue
        addr = region_start + i
        rows.append({
            "addr": addr,
            "addr_hex": f"0x{addr:08X}",
            "fmt": "projectile_template_anchor",
            "damage_addr": region_start + dmg_off,
            "damage": int(dmg),
            "radius_addr": region_start + radius_off,
            "radius": float(radius),
            "header_hex": _hex(data[i:i + 0x50], 0x50),
        })
    return rows


def _scan_23_card_candidates(region_start: int, data: bytes) -> list[dict[str, Any]]:
    """Loose exploratory 0x23 card hits for supers that do not use beam_sig."""
    rows: list[dict[str, Any]] = []
    patterns = ((b"\x01\x23\x00\x00", "0123_card"), (b"\x00\x23\x00\x00", "0023_card"))
    seen: set[int] = set()
    for sig, label in patterns:
        pos = 0
        while True:
            i = data.find(sig, pos)
            if i < 0:
                break
            pos = i + 1
            if i in seen or i + 8 > len(data):
                continue
            seen.add(i)
            dmg = _u16(data, i + 4)
            if dmg is None or not (2 <= int(dmg) <= 50000):
                continue
            addr = region_start + i
            rows.append({
                "addr": addr,
                "addr_hex": f"0x{addr:08X}",
                "fmt": label,
                "damage_addr": region_start + i + 4,
                "damage": int(dmg),
                "header_hex": _hex(data[i:i + 0x60], 0x60),
            })
    rows.sort(key=lambda row: int(row.get("addr") or 0))
    return rows[:256]


def _write_chunk_index(path: str, entries: list[tuple[str, int, bytes]]) -> list[dict[str, Any]]:
    manifest = []
    with open(path, "wb") as f:
        f.write(b"TVCDUMP2")
        f.write(len(entries).to_bytes(4, "big"))
        for label, addr, data in entries:
            raw_label = str(label).encode("utf-8", "replace")[:96]
            f.write(len(raw_label).to_bytes(2, "big"))
            f.write(raw_label)
            f.write(int(addr).to_bytes(4, "big", signed=False))
            f.write(len(data).to_bytes(4, "big", signed=False))
            f.write(data)
            manifest.append({
                "label": label,
                "addr": addr,
                "addr_hex": f"0x{addr:08X}",
                "size": len(data),
            })
    return manifest


def _projectile_hit_summary(hit: dict) -> dict[str, Any]:
    keys = (
        "addr", "fmt", "key", "move", "dmg", "dmg_write_addr", "cluster", "proj_role",
        "radius", "lifetime", "speed", "accel", "kb_x", "kb_y", "hitbox", "arc", "arc2",
        "super_lifetime", "super_hit_count", "super_hit_interval", "super_particle_fx",
        "super_spawn_bone", "super_hit_source", "super_air_kb_y", "super_beam_width",
        "super_speed", "super_accel", "super_radius", "super_beam_visual",
        "super_final_damage", "super_final_lifetime", "super_final_particle_fx", "super_final_spawn_bone",
    )
    return {k: _jsonable(hit.get(k)) for k in keys if k in hit}


def _write_text_report(path: str, report: dict[str, Any]) -> None:
    lines: list[str] = []
    meta = report.get("meta") or {}
    lines.append("TvC Character Dump")
    lines.append("==================")
    lines.append(f"Character: {meta.get('char_name', '-')}")
    lines.append(f"Slot: {meta.get('slot_label', '-')}")
    lines.append(f"Created: {meta.get('created_at', '-')}")
    lines.append(f"Moves: {report.get('counts', {}).get('moves', 0)}")
    lines.append(f"Projectile rows already loaded: {report.get('counts', {}).get('projectile_rows', 0)}")
    lines.append(f"Super beam candidates: {report.get('counts', {}).get('super_beam_candidates', 0)}")
    lines.append(f"Projectile template candidates: {report.get('counts', {}).get('projectile_template_candidates', 0)}")
    lines.append("")

    bounds = report.get("scan_region") or {}
    if bounds:
        lines.append("Scan region")
        lines.append("-----------")
        lines.append(f"start: {bounds.get('start_hex')}")
        lines.append(f"end:   {bounds.get('end_hex')}")
        lines.append(f"size:  {bounds.get('size')}")
        lines.append("")

    lines.append("Command histogram")
    lines.append("-----------------")
    hist = report.get("command_histogram") or {}
    if hist:
        for cmd, count in hist.items():
            label = COMMAND_NAMES.get(tuple(int(x, 16) for x in cmd.split("/")), "") if "/" in cmd else ""
            lines.append(f"{cmd:5} {count:5}  {label}")
    else:
        lines.append("No command hits captured.")
    lines.append("")

    lines.append("Super beam candidates")
    lines.append("---------------------")
    cards = report.get("super_beam_candidates") or []
    if cards:
        for c in cards:
            f = c.get("fields") or {}
            lines.append(
                f"{c.get('addr_hex')} dmg={f.get('damage')} hits={f.get('hit_count')} "
                f"interval={f.get('hit_interval')} lifetime={f.get('lifetime')} "
                f"fx={f.get('particle_fx')} bone={f.get('spawn_bone')} "
                f"speed={f.get('beam_speed')} radius={f.get('hit_radius')}"
            )
    else:
        lines.append("None found in the selected scan region.")
    lines.append("")

    lines.append("Projectile template candidates")
    lines.append("-----------------------------")
    templ = report.get("projectile_template_candidates") or []
    if templ:
        for p in templ[:80]:
            lines.append(f"{p.get('addr_hex')} dmg={p.get('damage')} radius={p.get('radius')} dmg_addr=0x{int(p.get('damage_addr') or 0):08X}")
        if len(templ) > 80:
            lines.append(f"... {len(templ) - 80} more in JSON")
    else:
        lines.append("None found in the selected scan region.")
    lines.append("")

    lines.append("Loaded projectile/super rows")
    lines.append("----------------------------")
    phits = report.get("projectile_rows") or []
    if phits:
        for h in phits[:100]:
            lines.append(
                f"0x{int(h.get('addr') or 0):08X} {h.get('fmt', ''):18} "
                f"{h.get('move', '')} dmg={h.get('dmg', '')}"
            )
        if len(phits) > 100:
            lines.append(f"... {len(phits) - 100} more in JSON")
    else:
        lines.append("No projectile rows were loaded in the workbench yet.")
    lines.append("")

    lines.append("Raw files")
    lines.append("---------")
    for key in ("move_head_chunks", "candidate_chunks"):
        lines.append(f"{key}: {report.get(key, {}).get('path', '')}")
    lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def dump_character(target_slot: dict, moves: list[dict], projectile_hits: list[dict] | None = None,
                   output_root: str | None = None) -> str:
    """Write a read-only dump folder and return its path."""
    projectile_hits = list(projectile_hits or [])
    moves = list(moves or [])
    rbytes_func = _get_rbytes()

    char_name = str(target_slot.get("char_name") or target_slot.get("character") or target_slot.get("name") or "unknown")
    slot_label = str(target_slot.get("slot_label") or target_slot.get("slot") or "slot")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = output_root or os.path.join(_app_base_dir(), "character_dumps")
    outdir = os.path.join(root, f"{stamp}_{_safe_name(slot_label)}_{_safe_name(char_name)}")
    raw_dir = os.path.join(outdir, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    move_summaries = [_move_summary(mv, rbytes_func) for mv in moves]
    command_hist = _command_histogram(move_summaries)

    region_start, region_end = _scan_region_bounds(moves, projectile_hits)
    region_data = b""
    super_cards: list[dict[str, Any]] = []
    projectile_templates: list[dict[str, Any]] = []
    loose_23_cards: list[dict[str, Any]] = []
    if region_start is not None and region_end is not None:
        region_data = _read_bytes(region_start, region_end - region_start, rbytes_func)
        if region_data:
            super_cards = _scan_super_beam_cards(region_start, region_data)
            projectile_templates = _scan_projectile_templates(region_start, region_data)
            loose_23_cards = _scan_23_card_candidates(region_start, region_data)

    # Binary move heads.
    move_entries: list[tuple[str, int, bytes]] = []
    for mv in moves:
        addr = _move_addr(mv)
        if not _valid_mem_addr(addr):
            continue
        data = _read_bytes(addr, MOVE_HEAD_LEN, rbytes_func)
        if not data:
            continue
        name = mv.get("move_name") or mv.get("pretty_name") or mv.get("name") or "move"
        move_entries.append((str(name), addr, data))
    move_bin = os.path.join(raw_dir, "move_heads.bin")
    move_manifest = _write_chunk_index(move_bin, move_entries) if move_entries else []

    # Binary candidate chunks.
    cand_entries: list[tuple[str, int, bytes]] = []
    for card in super_cards:
        addr = int(card.get("addr") or 0)
        if addr:
            data = _read_bytes(addr, 0x180, rbytes_func)
            if data:
                cand_entries.append(("super_beam_card", addr, data))
    for row in loose_23_cards[:64]:
        addr = int(row.get("addr") or 0)
        if addr:
            data = _read_bytes(addr, 0x100, rbytes_func)
            if data:
                cand_entries.append((str(row.get("fmt") or "23_card"), addr, data))
    for row in projectile_templates[:128]:
        addr = int(row.get("addr") or 0)
        if addr:
            data = _read_bytes(addr, 0x120, rbytes_func)
            if data:
                cand_entries.append(("projectile_template", addr, data))
    cand_bin = os.path.join(raw_dir, "candidate_chunks.bin")
    cand_manifest = _write_chunk_index(cand_bin, cand_entries) if cand_entries else []

    slot_meta = _jsonable(target_slot)
    report: dict[str, Any] = {
        "schema": "tvc_continuo.character_dump.v1",
        "meta": {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "char_name": char_name,
            "slot_label": slot_label,
            "rbytes_available": bool(rbytes_func is not None),
        },
        "slot": slot_meta,
        "counts": {
            "moves": len(moves),
            "projectile_rows": len(projectile_hits),
            "super_beam_candidates": len(super_cards),
            "projectile_template_candidates": len(projectile_templates),
            "loose_23_card_candidates": len(loose_23_cards),
            "move_head_chunks": len(move_manifest),
            "candidate_chunks": len(cand_manifest),
        },
        "scan_region": {
            "start": region_start,
            "start_hex": f"0x{region_start:08X}" if region_start is not None else "",
            "end": region_end,
            "end_hex": f"0x{region_end:08X}" if region_end is not None else "",
            "size": (region_end - region_start) if region_start is not None and region_end is not None else 0,
            "bytes_read": len(region_data),
        },
        "command_histogram": command_hist,
        "moves": move_summaries,
        "projectile_rows": [_projectile_hit_summary(h) for h in projectile_hits],
        "super_beam_candidates": super_cards,
        "projectile_template_candidates": projectile_templates,
        "loose_23_card_candidates": loose_23_cards,
        "move_head_chunks": {
            "path": os.path.relpath(move_bin, outdir) if move_manifest else "",
            "entries": move_manifest,
        },
        "candidate_chunks": {
            "path": os.path.relpath(cand_bin, outdir) if cand_manifest else "",
            "entries": cand_manifest,
        },
    }

    json_path = os.path.join(outdir, "character_dump.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)

    text_path = os.path.join(outdir, "character_dump.txt")
    _write_text_report(text_path, report)

    return outdir
