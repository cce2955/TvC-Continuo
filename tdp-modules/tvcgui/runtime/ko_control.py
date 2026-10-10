"""Extracted runtime module from :mod:`main`.

This module deliberately preserves the original function names and behavior so
`main.py` can remain a compatibility-oriented entry point while the subsystem
has a focused home.
"""
from __future__ import annotations

import time

from tvcgui.platform.dolphin import rd32, wd32
import tvcgui.platform.patch_manager as runtime_pm

def _ko_wd32(addr: int, value: int) -> bool:
    """Write a KO control word even while the select-write barrier is active."""
    try:
        return bool(wd32(addr, value, bypass_quarantine=True))
    except TypeError:
        return bool(wd32(addr, value))


# KO lab: base-relative packet that forces a fighter object back to the
# idle-ish action cluster observed before KO.  This is intentionally a
# live poke/test helper, not a permanent patch: use the per-panel
# "Idle Restore" button while the fighter object is still loaded.
IDLE_RESTORE_PACKET_U32 = (
    (0x1E8, 0x00000001),
    (0x1EC, 0x00000001),
    (0x1F0, 0xFFFFFFFF),
    (0x1F4, 0x000001BF),
    (0x1F8, 0x0000001E),
    (0x1FC, 0x00000000),
    (0x200, 0x00000000),
    (0x204, 0x00000002),
    (0x208, 0x00000000),
    (0x20C, 0x00000000),
    (0x210, 0x00000000),
    (0x214, 0x00000102),
    (0x218, 0x00000001),
    (0x21C, 0x00000036),
    (0x220, 0x00000036),
    (0x070, 0x00000000),
)


def apply_idle_restore_packet(base: int | None, *, slot_label: str = "", verify: bool = False) -> dict:
    """Write the KO idle-restore test packet to a live fighter base.

    verify=True immediately reads the written fields back.  That gives the GUI
    a visible yes/no status instead of relying on console prints, and it helps
    separate "button did not fire" from "the game overwrote the value again".
    """
    try:
        base_i = int(base or 0)
    except Exception:
        base_i = 0
    if not base_i:
        return {"ok": False, "error": "no live fighter base", "slot": slot_label, "wrote": 0, "total": len(IDLE_RESTORE_PACKET_U32), "verified": 0}

    wrote = 0
    verified = 0
    failed = []
    verify_failed = []
    for off, value in IDLE_RESTORE_PACKET_U32:
        addr = base_i + int(off)
        value_i = int(value) & 0xFFFFFFFF
        try:
            ok = _ko_wd32(addr, value_i)
        except Exception:
            ok = False
        if ok:
            wrote += 1
            if verify:
                try:
                    rb = rd32(addr)
                except Exception:
                    rb = None
                if rb is not None and (int(rb) & 0xFFFFFFFF) == value_i:
                    verified += 1
                else:
                    verify_failed.append((addr, rb, value_i))
        else:
            failed.append(addr)

    result = {
        "ok": wrote == len(IDLE_RESTORE_PACKET_U32),
        "slot": slot_label,
        "base": base_i,
        "wrote": wrote,
        "total": len(IDLE_RESTORE_PACKET_U32),
        "verified": verified,
        "verify_total": len(IDLE_RESTORE_PACKET_U32) if verify else 0,
        "failed": failed,
        "verify_failed": verify_failed,
    }
    if verify:
        result["ok"] = result["ok"] and verified == len(IDLE_RESTORE_PACKET_U32)
    return result


# Per-character neutral restore. The legacy IDLE_RESTORE_PACKET_U32 was
# captured from one fighter and is not safe as a universal post-KO neutral
# state. Learn the actual point fighter's natural idle/control words during
# normal play, then use those same values when escaping Win Pose.
# Control words that distinguish a naturally controllable neutral fighter from
# a survivor that has merely had Win Pose cancelled. Keep these separate from
# the action cluster so we can restore native neutral control state while the
# fighter is action 1 without overwriting a real walk, jump, dash, or attack.
KO_NEUTRAL_CONTROL_OFFSETS_U32 = (
    0x058, 0x05C, 0x060, 0x064, 0x068, 0x06C, 0x070, 0x088,
)

KO_NEUTRAL_RESTORE_OFFSETS_U32 = tuple(dict.fromkeys(
    list(KO_NEUTRAL_CONTROL_OFFSETS_U32)
    + [int(off) for off, _value in IDLE_RESTORE_PACKET_U32]
))


IDLE_RESTORE_HOLD_SECONDS = 0.90


# KO Rescue is the next test after the plain idle packet.  The idle packet
# proved the GUI write path is working, but KO/result state can still own the
# scene.  This packet revives the opposite side, restores their action clusters,
# and rewinds the most promising small round/result globals observed in the
# pre-KO -> KO -> post-KO dumps.
KO_ROUND_REWIND_PACKET_U32 = (
    (0x8091C280, 0x00000001),  # candidate fight/result phase: active-ish value
    (0x8091C284, 0x00000004),  # companion phase/substate seen right before KO
    (0x80561874, 0x8049B892),  # candidate result callback/state ptr pre/post-active
    (0x80561878, 0x00000000),  # candidate KO/result latch
    (0x804A232C, 0x00000003),  # small state group, returns to 3 after result settles
    (0x804A3AEC, 0x00000003),
    (0x804ABEBC, 0x00000003),
    (0x804AD974, 0x00000003),
)

KO_RESCUE_HP_FLOOR = 0x00001000
KO_RESCUE_HOLD_SECONDS = 12.00

# v4: the idle packet proved writes work, but it did not restore the
# control/flag layer.  Keep a rolling pre-KO snapshot of the wider
# fighter state and use it for a real frame rewind.
KO_REWIND_OFFSETS_U32 = (
    0x028, 0x02C, 0x030, 0x034,
    0x040, 0x044, 0x04C,
    0x058, 0x05C, 0x060, 0x064, 0x068, 0x06C, 0x070, 0x074, 0x078, 0x07C, 0x080, 0x084, 0x088,
    0x1E8, 0x1EC, 0x1F0, 0x1F4, 0x1F8, 0x1FC, 0x200, 0x204, 0x208, 0x20C, 0x210, 0x214,
    0x218, 0x21C, 0x220, 0x224, 0x228, 0x22C, 0x230, 0x234, 0x238, 0x23C, 0x240, 0x244,
    0x248, 0x24C, 0x250, 0x254, 0x258, 0x25C, 0x260, 0x264, 0x268, 0x26C, 0x270, 0x274,
    0x278, 0x27C, 0x280, 0x284, 0x288, 0x28C, 0x290, 0x294, 0x298, 0x29C, 0x2A0,
)
KO_REWIND_BAD_ACTIONS = {0x0000002A, 0x0000009A}



def _ko_code_wd32(addr: int, value: int) -> bool:
    """Write one PPC instruction through the JIT-safe code lane."""
    try:
        addr_i = int(addr) & 0xFFFFFFFF
        value_i = int(value) & 0xFFFFFFFF
        return bool(runtime_pm.write_code_u32(
            addr_i,
            value_i,
            key=f"ko:code:0x{addr_i:08X}",
            force=True,
        ))
    except Exception:
        return False


def _write_code_u32_count(
    addr: int,
    value: int,
    *,
    verify: bool = False,
) -> tuple[int, int, list]:
    """JIT-safe counterpart to _write_u32_count for PPC code."""
    addr_i = int(addr) & 0xFFFFFFFF
    value_i = int(value) & 0xFFFFFFFF

    try:
        ok = _ko_code_wd32(addr_i, value_i)
    except Exception:
        ok = False

    if not ok:
        return 0, 0, [(addr_i, None, value_i)]

    if not verify:
        return 1, 0, []

    try:
        rb = rd32(addr_i)
    except Exception:
        rb = None

    if rb is not None and (int(rb) & 0xFFFFFFFF) == value_i:
        return 1, 1, []

    return 1, 0, [(addr_i, rb, value_i)]




def _write_code_packet_count(
    pairs,
    *,
    verify: bool = False,
    key: str = "ko:code:batch",
) -> tuple[int, int, list, int]:
    """Apply a complete PPC instruction packet with one JIT reset."""

    final = {}

    try:
        for addr, value in pairs:
            addr_i = int(addr) & 0xFFFFFFFF
            value_i = int(value) & 0xFFFFFFFF
            final[addr_i] = value_i.to_bytes(4, "big")
    except Exception:
        return 0, 0, [], 0

    total = len(final)

    if not total:
        return 0, 0, [], 0

    try:
        ok = bool(
            runtime_pm.write_code_many(
                final,
                key=key,
                priority=98,
                force=True,
            )
        )
    except Exception:
        ok = False

    if not ok:
        failed = [
            (
                addr,
                None,
                int.from_bytes(payload, "big"),
            )
            for addr, payload in final.items()
        ]
        return 0, 0, failed, total

    wrote = total

    if not verify:
        return wrote, 0, [], total

    verified = 0
    failed = []

    for addr, payload in final.items():
        expected = int.from_bytes(payload, "big")

        try:
            rb = rd32(addr)
        except Exception:
            rb = None

        if rb is not None and (int(rb) & 0xFFFFFFFF) == expected:
            verified += 1
        else:
            failed.append((addr, rb, expected))

    return wrote, verified, failed, total


def _write_u32_count(addr: int, value: int, *, verify: bool = False) -> tuple[int, int, list]:
    """Small local write helper: returns wrote, verified, failures."""
    addr_i = int(addr) & 0xFFFFFFFF
    value_i = int(value) & 0xFFFFFFFF
    try:
        ok = _ko_wd32(addr_i, value_i)
    except Exception:
        ok = False
    if not ok:
        return 0, 0, [(addr_i, None, value_i)]
    if not verify:
        return 1, 0, []
    try:
        rb = rd32(addr_i)
    except Exception:
        rb = None
    if rb is not None and (int(rb) & 0xFFFFFFFF) == value_i:
        return 1, 1, []
    return 1, 0, [(addr_i, rb, value_i)]


def _slot_side(slot_label: str) -> str:
    s = str(slot_label or "").upper()
    if s.startswith("P1"):
        return "P1"
    if s.startswith("P2"):
        return "P2"
    return ""


def capture_ko_rewind_baselines(snaps: dict, render_snap_by_slot: dict, existing: dict) -> dict:
    """Keep last non-KO frame values and per-character natural neutral state.

    The broad rewind snapshot remains available for the older KO lab tools.
    Separately, while both teams are still alive, capture the point fighter's
    own action-1 neutral/control words. That neutral snapshot is what KO Control
    should use after Win Pose instead of a single hardcoded fighter packet.
    """
    now_ts = time.time()
    out = dict(existing or {})
    merged = {}
    for src in (render_snap_by_slot or {}, snaps or {}):
        for slot, snap in src.items():
            if isinstance(snap, dict):
                merged[slot] = snap

    team_alive = {"P1": False, "P2": False}
    for slot, snap in merged.items():
        try:
            base = int(snap.get("base") or 0)
        except Exception:
            base = 0
        if not base:
            continue
        try:
            hp_now = int(snap.get("cur") or snap.get("baroque_local_hp32") or rd32(base + 0x28) or 0) & 0xFFFFFFFF
        except Exception:
            hp_now = 0
        side = _slot_side(str(slot))
        if side and hp_now > 0:
            team_alive[side] = True
    round_live = bool(team_alive["P1"] and team_alive["P2"])

    for slot, snap in merged.items():
        try:
            base = int(snap.get("base") or 0)
        except Exception:
            base = 0
        if not base:
            continue
        try:
            hp_now = int(snap.get("cur") or snap.get("baroque_local_hp32") or rd32(base + 0x28) or 0) & 0xFFFFFFFF
        except Exception:
            hp_now = 0
        try:
            act_now = int(snap.get("attA") or rd32(base + 0x1E8) or 0) & 0xFFFFFFFF
        except Exception:
            act_now = 0
        if hp_now == 0 or act_now in KO_REWIND_BAD_ACTIONS:
            continue

        previous = dict(out.get(str(slot)) or {})
        values = {}
        ok = 0
        for off in KO_REWIND_OFFSETS_U32:
            try:
                v = rd32(base + int(off))
            except Exception:
                v = None
            if v is not None:
                values[int(off)] = int(v) & 0xFFFFFFFF
                ok += 1
        if ok >= 8:
            previous.update({"base": base, "values": values, "ts": now_ts, "act": act_now, "hp": hp_now})

        # Learn a clean neutral only before either team is fully KO'd. Require
        # the native point flag so a benched teammate's passive action state is
        # never mistaken for the post-KO movement baseline.
        point_active = bool(
            snap.get("damage_point_active",
                     snap.get("damage_is_point",
                              snap.get("point_active", False)))
        )
        try:
            point_active = bool(point_active or int(rd32(base + 0x44A0) or 0))
        except Exception:
            pass
        if round_live and point_active and act_now == 1:
            neutral_values = {}
            for off in KO_NEUTRAL_RESTORE_OFFSETS_U32:
                try:
                    v = rd32(base + int(off))
                except Exception:
                    v = None
                if v is not None:
                    neutral_values[int(off)] = int(v) & 0xFFFFFFFF
            try:
                raw_char_id = snap.get("char_id")
                if raw_char_id is None:
                    raw_char_id = rd32(base + 0x14)
                char_id = int(raw_char_id or 0) & 0xFFFFFFFF
            except Exception:
                char_id = 0
            if len(neutral_values) >= 10:
                previous["neutral_values"] = neutral_values
                previous["neutral_char_id"] = char_id
                previous["neutral_ts"] = now_ts
                previous["neutral_base"] = base

        if previous:
            out[str(slot)] = previous
    return out


def _ko_ctrl_apply_neutral_baseline(slot: str, base: int, snap: dict, baseline: dict | None) -> dict:
    """Restore this exact character's learned natural neutral state."""
    base_i = int(base or 0)
    entry = baseline if isinstance(baseline, dict) else {}
    values = dict(entry.get("neutral_values") or {})
    if not base_i or not values:
        return {"ok": False, "wrote": 0, "mode": "fallback"}
    try:
        raw_char_id = (snap or {}).get("char_id")
        if raw_char_id is None:
            raw_char_id = rd32(base_i + 0x14)
        live_char = int(raw_char_id or 0) & 0xFFFFFFFF
    except Exception:
        live_char = 0
    stored_char = int(entry.get("neutral_char_id") or 0) & 0xFFFFFFFF
    if stored_char and live_char and stored_char != live_char:
        return {"ok": False, "wrote": 0, "mode": "char-mismatch"}
    wrote = 0
    for off in KO_NEUTRAL_RESTORE_OFFSETS_U32:
        if int(off) not in values:
            continue
        try:
            if _ko_wd32(base_i + int(off), int(values[int(off)]) & 0xFFFFFFFF):
                wrote += 1
        except Exception:
            pass
    return {"ok": wrote > 0, "wrote": wrote, "mode": "learned-neutral", "slot": str(slot or "?")}

def _ko_ctrl_apply_neutral_controls(slot: str, base: int, snap: dict, baseline: dict | None) -> dict:
    """Restore only this character's learned neutral control words.

    This is intentionally narrower than _ko_ctrl_apply_neutral_baseline(). It
    is safe to repeat while the survivor is sitting in native action 1, but it
    must stop as soon as the game accepts a real movement or attack action.
    """
    base_i = int(base or 0)
    entry = baseline if isinstance(baseline, dict) else {}
    values = dict(entry.get("neutral_values") or {})
    if not base_i or not values:
        return {"ok": False, "wrote": 0, "mode": "no-neutral-controls"}
    try:
        raw_char_id = (snap or {}).get("char_id")
        if raw_char_id is None:
            raw_char_id = rd32(base_i + 0x14)
        live_char = int(raw_char_id or 0) & 0xFFFFFFFF
    except Exception:
        live_char = 0
    stored_char = int(entry.get("neutral_char_id") or 0) & 0xFFFFFFFF
    if stored_char and live_char and stored_char != live_char:
        return {"ok": False, "wrote": 0, "mode": "char-mismatch"}

    wrote = 0
    matched = 0
    missing = []
    for off in KO_NEUTRAL_CONTROL_OFFSETS_U32:
        off_i = int(off)
        if off_i not in values:
            missing.append(off_i)
            continue
        target = int(values[off_i]) & 0xFFFFFFFF
        try:
            current = rd32(base_i + off_i)
        except Exception:
            current = None
        if current is not None and (int(current) & 0xFFFFFFFF) == target:
            matched += 1
            continue
        try:
            if _ko_wd32(base_i + off_i, target):
                wrote += 1
        except Exception:
            pass
    return {
        "ok": (wrote + matched) > 0 and not missing,
        "wrote": wrote,
        "matched": matched,
        "mode": "learned-neutral-controls",
        "slot": str(slot or "?"),
        "missing": missing,
    }


def apply_slot_rewind_baseline(slot_label: str, base: int, baseline: dict | None, *, verify: bool = False) -> dict:
    """Write a captured pre-KO baseline to a fighter slot."""
    slot = str(slot_label or "?")
    try:
        base_i = int(base or 0)
    except Exception:
        base_i = 0
    values = {}
    if isinstance(baseline, dict):
        values = dict(baseline.get("values") or {})
    if not base_i or not values:
        return {"ok": False, "slot": slot, "base": base_i, "wrote": 0, "total": 0, "verified": 0, "verify_total": 0, "failed": [], "mode": "KO rewind baseline"}
    wrote = verified = total = 0
    failed = []
    for off, value in values.items():
        total += 1
        w, v, f = _write_u32_count(base_i + int(off), int(value), verify=verify)
        wrote += w
        verified += v
        failed.extend(f)
    return {"ok": wrote == total and (not verify or verified == total), "slot": slot, "base": base_i, "wrote": wrote, "total": total, "verified": verified, "verify_total": total if verify else 0, "failed": failed, "mode": "KO rewind baseline"}


def apply_ko_rescue_packet(slot_label: str, bases_by_slot: dict, *, verify: bool = False, baseline_by_slot: dict | None = None) -> dict:
    """Aggressive KO lab poke: full pre-KO frame rewind, revive opposite side, rewind candidate globals."""
    slot = str(slot_label or "?")
    side = _slot_side(slot)
    opp_slots = ("P2-C1", "P2-C2") if side == "P1" else (("P1-C1", "P1-C2") if side == "P2" else ())

    wrote = 0
    verified = 0
    total = 0
    failed = []
    touched = []

    def _add_result(res: dict) -> None:
        nonlocal wrote, verified, total, failed
        wrote += int(res.get("wrote") or 0)
        total += int(res.get("total") or 0)
        verified += int(res.get("verified") or 0)
        failed.extend(res.get("failed") or [])
        failed.extend([x[0] if isinstance(x, tuple) and x else x for x in (res.get("verify_failed") or [])])

    # 1) clicked/winner slot: prefer a full pre-KO frame rewind baseline.
    base = int((bases_by_slot or {}).get(slot) or 0)
    if base:
        touched.append(slot)
        bline = (baseline_by_slot or {}).get(slot) if isinstance(baseline_by_slot, dict) else None
        res = apply_slot_rewind_baseline(slot, base, bline, verify=verify)
        if int(res.get("total") or 0) > 0:
            _add_result(res)
        else:
            _add_result(apply_idle_restore_packet(base, slot_label=slot, verify=verify))

    # 2) opposite side: revive and prefer their last pre-KO baseline too.
    for oslot in opp_slots:
        obase = int((bases_by_slot or {}).get(oslot) or 0)
        if not obase:
            continue
        touched.append(oslot)
        bline = (baseline_by_slot or {}).get(oslot) if isinstance(baseline_by_slot, dict) else None
        res = apply_slot_rewind_baseline(oslot, obase, bline, verify=verify)
        if int(res.get("total") or 0) > 0:
            _add_result(res)
        else:
            _add_result(apply_idle_restore_packet(obase, slot_label=oslot, verify=verify))
        try:
            max_hp = int(rd32(obase + 0x24) or 0) & 0xFFFFFFFF
        except Exception:
            max_hp = 0
        hp = KO_RESCUE_HP_FLOOR
        if isinstance(bline, dict) and int(bline.get("hp") or 0) > 0:
            hp = int(bline.get("hp") or KO_RESCUE_HP_FLOOR) & 0xFFFFFFFF
        elif max_hp:
            hp = max(1, min(KO_RESCUE_HP_FLOOR, max_hp))
        for off in (0x28, 0x2C):
            total += 1
            w, v, f = _write_u32_count(obase + off, hp, verify=verify)
            wrote += w
            verified += v
            failed.extend(f)

    # 3) candidate global/result rewind pack.
    for addr, value in KO_ROUND_REWIND_PACKET_U32:
        total += 1
        w, v, f = _write_u32_count(addr, value, verify=verify)
        wrote += w
        verified += v
        failed.extend(f)

    return {
        "ok": wrote == total and (not verify or verified == total),
        "slot": slot,
        "base": base,
        "wrote": wrote,
        "total": total,
        "verified": verified,
        "verify_total": total if verify else 0,
        "failed": failed,
        "touched": touched,
        "mode": "KO rewind",
    }



KO_SLOT_FLAG_HOLD_OFFSETS_U32 = (
    0x058, 0x05C, 0x060, 0x064, 0x068, 0x06C, 0x070, 0x074, 0x078, 0x07C, 0x080, 0x084, 0x088,
)
KO_SLOT_ACTION_HOLD_OFFSETS_U32 = (
    0x1E8, 0x1EC, 0x1F0, 0x1F4, 0x1F8, 0x1FC, 0x200, 0x204, 0x208, 0x20C, 0x210, 0x214,
    0x218, 0x21C, 0x220,
)


def apply_slot_only_ko_hold(slot_label: str, bases_by_slot: dict, baseline_by_slot: dict | None, *, hold_kind: str = "slot_flags", verify: bool = False) -> dict:
    """Selected-slot-only KO lab hold.

    Unlike apply_ko_rescue_packet(), this intentionally does NOT revive the
    opposite side and does NOT write the candidate global/result rewind pack.
    It is for separating the winner input gate from the loser/round-end state.
    """
    slot = str(slot_label or "?")
    base = int((bases_by_slot or {}).get(slot) or 0)
    bline = (baseline_by_slot or {}).get(slot) if isinstance(baseline_by_slot, dict) else None
    values = dict((bline or {}).get("values") or {}) if isinstance(bline, dict) else {}
    wrote = verified = total = 0
    failed = []

    def _write(addr: int, value: int) -> None:
        nonlocal wrote, verified, total, failed
        total += 1
        w, v, f = _write_u32_count(addr, value, verify=verify)
        wrote += w
        verified += v
        failed.extend(f)

    if not base:
        return {"ok": False, "slot": slot, "base": base, "wrote": 0, "total": 0, "verified": 0, "verify_total": 0, "failed": [], "mode": "slot-only KO hold"}

    kind = str(hold_kind or "slot_flags")
    if kind == "slot_clear_result":
        # Clear the bits that distinguish the post-KO winner frame from the
        # last active frame in the 161103 -> 161112 dumps.  This is deliberately
        # less invasive than a full snapshot rewrite.
        try:
            v58 = int(rd32(base + 0x58) or 0) & 0xFFFFFFFF
        except Exception:
            v58 = 0
        try:
            v60 = int(rd32(base + 0x60) or 0) & 0xFFFFFFFF
        except Exception:
            v60 = 0
        try:
            v64 = int(rd32(base + 0x64) or 0) & 0xFFFFFFFF
        except Exception:
            v64 = 0
        _write(base + 0x58, v58 & ~0x00040000)
        _write(base + 0x60, v60 & ~0x04000000)
        _write(base + 0x64, v64 & ~0x00010240)
        # Keep +0x70/+0x74 at the last-good values if available; those are
        # per-frame control/transition bits and were different on the KO frame.
        for off in (0x70, 0x74):
            if off in values:
                _write(base + off, int(values[off]) & 0xFFFFFFFF)
    else:
        offsets = list(KO_SLOT_FLAG_HOLD_OFFSETS_U32)
        if kind == "slot_flags_action":
            offsets.extend(KO_SLOT_ACTION_HOLD_OFFSETS_U32)
        for off in offsets:
            if off not in values:
                continue
            _write(base + int(off), int(values[off]) & 0xFFFFFFFF)

    return {
        "ok": wrote == total and (not verify or verified == total),
        "slot": slot,
        "base": base,
        "wrote": wrote,
        "total": total,
        "verified": verified,
        "verify_total": total if verify else 0,
        "failed": failed,
        "mode": f"slot-only {kind}",
    }

def idle_restore_status_text(result: dict, *, held: bool = False) -> str:
    slot = str(result.get("slot") or "?")
    base = int(result.get("base") or 0)
    wrote = int(result.get("wrote") or 0)
    total = int(result.get("total") or len(IDLE_RESTORE_PACKET_U32))
    verified = int(result.get("verified") or 0)
    verify_total = int(result.get("verify_total") or 0)
    mode = str(result.get("mode") or "Idle restore")
    if mode.lower().startswith("ko"):
        prefix = "KO rescue hold" if held else "KO rescue"
    else:
        prefix = "Idle hold" if held else "Idle restore"
    if base <= 0:
        return f"{prefix} {slot}: no live base"
    if verify_total:
        return f"{prefix} {slot}: wrote {wrote}/{total}, verified {verified}/{verify_total} @ 0x{base:08X}"
    return f"{prefix} {slot}: wrote {wrote}/{total} @ 0x{base:08X}"



# v17 KO/input-buffer tests.
# Current finding: post-KO still shows compact input tokens around +0x1380,
# but the real interpreter package at +0x13C8/+0x13CC/+0x13D0/+0x13D4/+0x13D8/+0x13DC
# stays neutral/zero.  That points at the input reset/update layer, not the
# winner-pose/event layer.  These tests combine the known no-win-pose patch
# (0x80048D9C -> li r3,1) with NOPs for the two input-buffer clear helpers and
# optional selected-slot live injection from +0x1380 history into the +0x13xx
# interpreter buffer.
KO_GLOBAL_HOLD_GROUPS = {}


def capture_ko_global_baseline(group_name: str) -> dict:
    return {"group": str(group_name or ""), "values": {}, "ts": time.time()}


def apply_ko_global_hold(baseline: dict | None, *, verify: bool = False) -> dict:
    group = str((baseline or {}).get("group") or "global") if isinstance(baseline, dict) else "global"
    return {"ok": True, "slot": group, "base": 1, "wrote": 0, "total": 0, "verified": 0, "verify_total": 0, "failed": [], "mode": f"global-hold {group}"}


INPUT_BUFFER_FIELDS_U32 = (
    0x13C8, 0x13CC, 0x13D0, 0x13D4, 0x13D8, 0x13DC, 0x13E0,
)


def _compact_token_to_current_mask(tok: int) -> int:
    """Best-effort conversion from observed compact +0x1380 token to raw +13CC mask.

    Observed live values:
      neutral +13CC = 0x00000800
      down    token 0x08 -> +13CC 0x00200808
      back    token 0x02 -> +13CC 0x00400802
      jab     token 0x80 -> +13CC usually remains neutral and appears in buffer/edge masks
    """
    t = int(tok or 0) & 0xFFFFFFFF
    cur = 0x00000800
    if t & 0x08:
        cur |= 0x00200008
    if t & 0x02:
        cur |= 0x00400002
    # These are guesses for the other directions so the injector is not useless
    # outside the exact down/back cases the module captured.  They are intentionally low
    # risk: they only set extra bits while the held compact token contains them.
    if t & 0x04:
        cur |= 0x00100004
    if t & 0x01:
        cur |= 0x00800001
    return cur & 0xFFFFFFFF


def _compact_token_to_button_mask(tok: int, hist: int = 0) -> int:
    t = (int(tok or 0) | int(hist or 0)) & 0xFFFFFFFF
    # Observed jab/light compact token is 0x80.  Super history included 0x61 in
    # the rolling compact slots, so include high nibble/button-looking bits from
    # history too.  Direction bits are handled separately through current mask.
    return t & 0x000000F0


def apply_ko_input_inject(slot_label: str, bases_by_slot: dict, *, mode: str = "input_inject", verify: bool = False) -> dict:
    """Selected-slot post-KO input-buffer injector.

    This does not revive the opponent and does not touch scene/result globals.
    It only takes the compact tokens that still update post-KO (+0x1380 etc.)
    and writes a plausible +0x13xx interpreter packet for the clicked winner.
    """
    slot = str(slot_label or "?")
    base = int((bases_by_slot or {}).get(slot) or 0)
    wrote = verified = total = 0
    failed = []

    def _write(addr: int, value: int) -> None:
        nonlocal wrote, verified, total, failed
        total += 1
        w, v, f = _write_u32_count(addr, value & 0xFFFFFFFF, verify=verify)
        wrote += w
        verified += v
        failed.extend(f)

    if not base:
        return {"ok": False, "slot": slot, "base": base, "wrote": 0, "total": 0, "verified": 0, "verify_total": 0, "failed": [], "mode": "KO input inject"}

    def _safe_rd(off: int, default: int = 0) -> int:
        try:
            return int(rd32(base + off) or 0) & 0xFFFFFFFF
        except Exception:
            return int(default) & 0xFFFFFFFF

    tok0 = _safe_rd(0x1380)
    tok1 = _safe_rd(0x1384)
    tok2 = _safe_rd(0x1388)
    tok3 = _safe_rd(0x138C)
    hist = (tok0 | tok1 | tok2 | tok3) & 0xFFFFFFFF
    old = _safe_rd(0x13CC, 0x00000800)
    current = _compact_token_to_current_mask(tok0)
    changed = (current ^ old) & 0xFFFFFFFF
    press = (current & changed) & 0xFFFFFFFF
    release = (old & changed) & 0xFFFFFFFF
    button = _compact_token_to_button_mask(tok0, hist)

    # Always feed the buffered masks; the captured jab frame had the useful
    # 0x80 bit in +13D8, even when current +13CC had already returned neutral.
    buffered = (press | button | (current & 0x0000000F)) & 0xFFFFFFFF
    repeat = (button | (current & 0x0000000F)) & 0xFFFFFFFF

    kind = str(mode or "input_inject")
    if kind == "input_inject_edge":
        # Stronger variant for games that key off either edge field.  The jab
        # capture had 0x80 in +13D4/+13D8, so intentionally duplicate button
        # edges into both +13D0 and +13D4 while held/testing.
        press = (press | button) & 0xFFFFFFFF
        release = (release | button) & 0xFFFFFFFF
        buffered = (buffered | button) & 0xFFFFFFFF
        repeat = (repeat | button) & 0xFFFFFFFF
    elif kind == "input_inject_buffer_only":
        # Least invasive: leave held/current alone, only keep the buffer alive.
        current = old if old else 0x00000800
        changed = 0
        press = button
        release = button
        buffered = button
        repeat = button

    _write(base + 0x13C8, old)
    _write(base + 0x13CC, current)
    _write(base + 0x13D0, press)
    _write(base + 0x13D4, release)
    _write(base + 0x13D8, buffered)
    _write(base + 0x13DC, repeat)

    return {
        "ok": wrote == total and (not verify or verified == total),
        "slot": slot,
        "base": base,
        "wrote": wrote,
        "total": total,
        "verified": verified,
        "verify_total": total if verify else 0,
        "failed": failed,
        "mode": f"KO {kind} tok=0x{tok0:X} hist=0x{hist:X} buf=0x{buffered:X}",
    }


KO_DOL_ORIGINALS_U32 = (
    # KO/death/result-tail/action tests; cleaned every click.
    (0x80052438, 0x3B800001),
    (0x8005243C, 0x60601000),
    (0x800525B0, 0x48000995),
    (0x800525E4, 0x4182001C),
    (0x800525F4, 0x64000020),
    (0x800525FC, 0x4800067D),
    (0x80052614, 0x40820124),
    (0x80052620, 0x41820070),
    (0x80052678, 0x48000EC9),
    (0x80052694, 0x418200A4),
    (0x80052708, 0x48000E39),
    (0x80052734, 0x4810A1A1),
    (0x80052C78, 0x9421FFC0),
    (0x80052F44, 0x9421FFC0),

    # Generic action writer / forced action mailbox tests.
    (0x80049094, 0x54600421),
    (0x80049098, 0x909F01E8),
    (0x80049140, 0x800301F4),
    (0x80049148, 0x900301E8),
    (0x8004916C, 0x800301F4),
    (0x80049178, 0x900301E8),
    (0x80049204, 0x90030064),
    (0x80049228, 0x90030064),
    (0x80049234, 0x90030064),
    (0x80049250, 0x90030064),
    (0x8004925C, 0x90030064),
    (0x8004927C, 0x90030070),
    (0x800492BC, 0x901F01EC),
    (0x800492EC, 0x90BF0060),

    # Final-KO result resolver gate.  These are restored whenever KO Ctrl
    # returns to SAFE/OFF; they are only overridden during FULL final-team KO.
    (0x800447E0, 0x64600400),  # oris r0,r3,0x400: native +0x60 action-state setup
    (0x800447E8, 0x4182000C),  # beq to normal resolver / otherwise return -2

    # 9410 / 9414 action-source reads inside 0x80048270.
    (0x80048750, 0x808D9410),
    (0x80048834, 0x808D9414),
    (0x80048AA4, 0x806D9410),
    (0x80048AE8, 0x806D9410),
    (0x80048BC8, 0x806D9414),
    (0x80048D84, 0x806D9410),
    (0x80048D94, 0x4182001C),
    (0x80048D98, 0x808D9414),
    (0x80048D9C, 0x806D9410),
    (0x80048E00, 0x808D9414),
    (0x80048E28, 0x806D9410),

    # Input-buffer clear/reset helpers.
    (0x80075E50, 0x900313C8),
    (0x80075E54, 0x900313CC),
    (0x80075E58, 0x900313D0),
    (0x80075E5C, 0x900313D4),
    (0x80075E60, 0x900313D8),
    (0x80075E64, 0x900313DC),
    (0x80075E68, 0x900313E0),
    (0x80076384, 0x900313C8),
    (0x80076388, 0x900313CC),
    (0x8007638C, 0x900313D4),
    (0x80076390, 0x900313D0),
    (0x80076394, 0x900313D8),
    (0x80076398, 0x900313DC),
    (0x8007639C, 0x900313E0),
    (0x8007646C, 0x90A313D8),
    (0x80076470, 0x900313DC),

    # Raw-pad / interpreter-feed gates and clear paths inside 0x800767B0 / 0x80076300.
    (0x80076820, 0x408201F8),  # bne result states past the controller read path
    (0x80076904, 0x4082007C),
    (0x80076938, 0x547F002E),
    (0x80076A44, 0x3BE00000),
    (0x80076A7C, 0x901C13C8),
    (0x80076A80, 0x57FF0036),  # rlwinm r31,r31,0,0,27 strips direction nibble in result state
    (0x80076A84, 0x901C13CC),
    (0x80076A88, 0x901C13D0),
    (0x80076A8C, 0x901C13D4),
    (0x80076BB8, 0x901C13D0),
    (0x80076BCC, 0x901C13D0),
    (0x8007637C, 0x40820028),
    (0x80076380, 0x38000000),

    # Data/script action bundle copy into the fighter struct.
    (0x802B1868, 0x901F01E8),
    (0x802B1870, 0x901F01EC),
    (0x802B1880, 0x901F01F4),
    (0x8006AA08, 0x3860002A),

    # Event/result dispatcher layer.
    (0x80053568, 0x481035AC),
    (0x80156B14, 0x1C030030),
    (0x80156B20, 0x4BFFFF38),
    (0x8015C8D4, 0x9421FFF0),
)

NO_CLEAR_75E_WRITES = tuple((addr, 0x60000000) for addr in (0x80075E50, 0x80075E54, 0x80075E58, 0x80075E5C, 0x80075E60, 0x80075E64, 0x80075E68))
NO_CLEAR_763_WRITES = tuple((addr, 0x60000000) for addr in (0x80076384, 0x80076388, 0x8007638C, 0x80076390, 0x80076394, 0x80076398, 0x8007639C))
NO_BUFFER_OUT_WRITES = ((0x8007646C, 0x60000000), (0x80076470, 0x60000000))
PATCH_8D9C_IDLE = ((0x80048D9C, 0x38600001),)

# v19: post-KO control/input gate tests.
# 0x80076938 strips the low 8 gameplay input bits when the battle scene state
# is past the normal-fight range.  0x80048D94 is the +0x64 result/action
# override branch; forcing it to branch skips the result override and falls back
# toward normal input/action selection.  0x80048D9C idle suppresses the visible
# win/result pose source so the control gate can be tested cleanly.
KEEP_LOW_INPUT_BYTE = (
    (0x80076938, 0x60000000),  # NOP rlwinm r31,r3,0,0,23 low-byte strip
)
SKIP_64_RESULT_OVERRIDE = (
    (0x80048D94, 0x4800001C),  # force branch past 9414/9410 result override
)
POST_KO_CONTROL_PACKET = KEEP_LOW_INPUT_BYTE + SKIP_64_RESULT_OVERRIDE + PATCH_8D9C_IDLE

# Final-result resolver exception.
#
# 0x800447E0 is part of the normal action setup. It sets
# fighter+0x60 |= 0x04000000, and controllable point fighters in the known-good
# baseline carry that bit. Keep that instruction native. The result-only block
# is the following conditional branch at 0x800447E8, which returns -2 instead
# of entering ordinary action resolution when fighter+0x64 bit 0x40 is set.
KO_RESULT_RESOLVER_UNLOCK_PACKET = (
    (0x800447E8, 0x4800000C),  # continue into native action resolution
)

# These are the newer raw-pad / interpreter-feed tests.  v17 only NOPed the
# +13xx clear stores, but 0x80076300 can return before the buffer builder ever
# runs, and 0x800767B0 can skip the real controller conversion before +13CC is
# written.  These patches test those gates directly.
# 0x800767B0 has an earlier battle-state gate before FORCE_PAD_READ.
# State 6 and 11 reach the controller read naturally, but result states such as
# 9 and 12 branch at 0x80076820 directly to 0x80076A18 with r31 still zero.
# This is why the direction fix could work in one KO phase and fail in another.
# FULL mode is only used for KO Control, so let every final-result phase fall
# through to the native team controller reader at 0x8007690C.
FORCE_RESULT_PAD_ROUTE = (
    (0x80076820, 0x60000000),
)

FORCE_PAD_READ = (
    (0x80076904, 0x60000000),  # NOP bne-to-skip: always fall through into controller read/conversion
)
NO_A7C_CLEAR = tuple((addr, 0x60000000) for addr in (
    0x80076A7C, 0x80076A84, 0x80076A88, 0x80076A8C,
))
# Result-state input filtering also clears r31's low direction nibble at
# 0x80076A80. Keeping the stores alive was not enough: the packet builder
# would still receive no 1/2/4/8 direction bits, so the survivor could be
# released to idle but never walk, crouch, or jump.
KEEP_RESULT_DIRECTIONS = (
    (0x80076A80, 0x60000000),
)
NO_LATE_13D0_CLEAR = tuple((addr, 0x60000000) for addr in (
    0x80076BB8, 0x80076BCC,
))
FORCE_BUFFER_BUILD = (
    (0x8007637C, 0x48000028),  # always branch to 0x800763A4 buffer-builder, do not enter clear/return block
)
NO_ZERO_R31 = (
    (0x80076A44, 0x60000000),  # do not zero normalized pad mask at 0x80076A38/44 scene gate
)

# Top-dock KO Ctrl payloads.
#
# SAFE packet: keep this armed during normal gameplay. It only affects the
# post-KO/result input sanitizer and the +0x64 result override; it does NOT
# force the raw pad-feed path, so it should not leak P1 input into the CPU on
# the next arcade match.
KO_CONTROL_SAFE_PACKET = POST_KO_CONTROL_PACKET + KO_RESULT_RESOLVER_UNLOCK_PACKET

# FULL packet: this is the heavier Control+Full lab payload. It includes the
# raw-pad / input-feed safety patches that helped post-KO control, including
# preserving the decoded direction nibble after the result-state filter. These
# can contaminate normal CPU input if left on before a match. Auto mode only
# escalates to this after a full-team KO is detected, then drops back to SAFE.
# Restored v22 reference packet.
#
# This is the last known working KO Ctrl model from the earlier project thread:
# SAFE is armed during live play; FULL only comes on after a whole team is KO'd.
# Deliberately DO NOT add later resolver/phase/live-fighter experiments here.
KO_CONTROL_FULL_PACKET = (
    POST_KO_CONTROL_PACKET
    + FORCE_RESULT_PAD_ROUTE
    + FORCE_PAD_READ
    + NO_A7C_CLEAR
    + KEEP_RESULT_DIRECTIONS
    + FORCE_BUFFER_BUILD
    + NO_LATE_13D0_CLEAR
    + NO_ZERO_R31
    + KO_RESULT_RESOLVER_UNLOCK_PACKET
)

KO_DOL_PATCH_TESTS = (
    {
        "name": "post-KO control packet",
        "short": "KO Control",
        "writes": POST_KO_CONTROL_PACKET,
        "note": "NOP low-byte input strip, skip +0x64 result override, and suppress win-pose source",
    },
    {
        "name": "keep low input byte only",
        "short": "LowByteOnly",
        "writes": KEEP_LOW_INPUT_BYTE,
        "note": "NOP 0x80076938 so result scene cannot strip the low 8 gameplay input bits",
    },
    {
        "name": "low input plus 8D9C idle",
        "short": "Low+8D9C",
        "writes": KEEP_LOW_INPUT_BYTE + PATCH_8D9C_IDLE,
        "note": "keep low-byte input and suppress winner/result pose source, but do not skip +0x64 override",
    },
    {
        "name": "skip +0x64 result override only",
        "short": "Skip64Only",
        "writes": SKIP_64_RESULT_OVERRIDE,
        "note": "force 0x80048D94 branch past the +0x64 9414/9410 override path",
    },
    {
        "name": "skip +0x64 plus 8D9C idle",
        "short": "Skip64+8D",
        "writes": SKIP_64_RESULT_OVERRIDE + PATCH_8D9C_IDLE,
        "note": "skip result override and suppress win-pose source, but leave input sanitizer untouched",
    },
    {
        "name": "post-KO control plus input full",
        "short": "Control+Full",
        "writes": POST_KO_CONTROL_PACKET + FORCE_PAD_READ + NO_A7C_CLEAR + KEEP_RESULT_DIRECTIONS + FORCE_BUFFER_BUILD + NO_LATE_13D0_CLEAR + NO_ZERO_R31,
        "note": "control packet plus v18 full input-feed safety patches",
    },
    {
        "name": "post-KO control plus edge inject",
        "short": "Control+Inject",
        "writes": POST_KO_CONTROL_PACKET + FORCE_PAD_READ + NO_A7C_CLEAR + KEEP_RESULT_DIRECTIONS + FORCE_BUFFER_BUILD + NO_LATE_13D0_CLEAR + NO_ZERO_R31,
        "hold_seconds": 12.0,
        "hold_kind": "input_inject_edge",
        "note": "control packet plus full input-feed patches and clicked-slot +1380 token edge injection",
    },
    {
        "name": "8D9C idle only",
        "short": "8D9COnly",
        "writes": PATCH_8D9C_IDLE,
        "note": "baseline: suppress winner/result pose source only",
    },
    {
        "name": "restore originals",
        "short": "DOL Reset",
        "writes": (),
        "note": "restore all KO DOL/input/control test addresses and stop holds",
    },
)

def apply_ko_dol_result_patch(test_index: int, *, verify: bool = True) -> dict:
    """Restore the KO/input test code addresses, then apply one selected DOL patch."""
    try:
        idx = int(test_index) % len(KO_DOL_PATCH_TESTS)
    except Exception:
        idx = 0
    test = KO_DOL_PATCH_TESTS[idx]
    wrote = verified = total = 0
    failed = []

    packet = (
        tuple(KO_DOL_ORIGINALS_U32)
        + tuple(test.get("writes", ()))
    )

    wrote, verified, failed, total = _write_code_packet_count(
        packet,
        verify=verify,
        key=f"ko:test:{idx}",
    )

    return {
        "ok": wrote == total and (not verify or verified == total),
        "idx": idx,
        "name": str(test.get("name") or f"test {idx + 1}"),
        "short": str(test.get("short") or "KO DOL"),
        "note": str(test.get("note") or ""),
        "wrote": wrote,
        "total": total,
        "verified": verified,
        "verify_total": total if verify else 0,
        "failed": failed,
        "mode": "KO DOL",
    }


def apply_ko_control_auto_mode(mode: str, *, verify: bool = True) -> dict:
    """Apply the top-dock KO Ctrl code state.

    mode="off" restores every KO/input DOL address.
    mode="safe" applies only the non-leaky post-KO control packet.
    mode="full" applies the heavier Control+Full packet during the KO/result
    window, including the final-result resolver unlock.  Auto-mode drops back
    to SAFE/originals later.
    """
    mode = str(mode or "off").lower()
    if mode not in ("off", "safe", "full"):
        mode = "off"

    wrote = verified = total = 0
    failed = []

    payload = ()
    if mode == "safe":
        payload = KO_CONTROL_SAFE_PACKET
    elif mode == "full":
        payload = KO_CONTROL_FULL_PACKET

    packet = tuple(KO_DOL_ORIGINALS_U32) + tuple(payload)

    wrote, verified, failed, total = _write_code_packet_count(
        packet,
        verify=verify,
        key=f"ko:auto:{mode}",
    )

    critical_expected = dict(payload)
    critical_reads = {}
    critical_ok = True
    for addr in (0x80076820, 0x80076938, 0x80048D94, 0x80048D9C, 0x80076904, 0x80076A80, 0x8007637C, 0x800447E0, 0x800447E8):
        if addr not in critical_expected:
            continue
        try:
            actual = rd32(addr)
        except Exception:
            actual = None
        critical_reads[f"0x{addr:08X}"] = None if actual is None else int(actual) & 0xFFFFFFFF
        if actual is None or (int(actual) & 0xFFFFFFFF) != (int(critical_expected[addr]) & 0xFFFFFFFF):
            critical_ok = False

    name = {
        "off": "KO Control OFF",
        "safe": "KO Control SAFE armed",
        "full": "KO Control FULL active",
    }.get(mode, "KO Control")
    return {
        "ok": wrote == total and (not verify or verified == total) and critical_ok,
        "name": name,
        "short": "KO Ctrl",
        "note": f"top-dock auto mode -> {mode}",
        "wrote": wrote,
        "total": total,
        "verified": verified,
        "verify_total": total if verify else 0,
        "failed": failed,
        "critical_ok": critical_ok,
        "critical_reads": critical_reads,
        "mode": "KO Control",
        "state": mode,
    }


def apply_ko_control_full_toggle(enabled: bool, *, verify: bool = True) -> dict:
    """Top-dock toggle for the working post-KO control patch.

    OFF restores every KO/input DOL test address. ON restores first, then
    applies the known-good Control+Full payload so stale lab pokes cannot
    contaminate the toggle.
    """
    wrote = verified = total = 0
    failed = []

    packet = tuple(KO_DOL_ORIGINALS_U32)

    if enabled:
        packet += tuple(KO_CONTROL_FULL_PACKET)

    wrote, verified, failed, total = _write_code_packet_count(
        packet,
        verify=verify,
        key="ko:full:on" if enabled else "ko:full:off",
    )

    name = "KO Control+Full ON" if enabled else "KO Control+Full OFF"
    return {
        "ok": wrote == total and (not verify or verified == total),
        "name": name,
        "short": "KO Ctrl",
        "note": "top-dock Control+Full toggle" if enabled else "restored KO/input DOL originals",
        "wrote": wrote,
        "total": total,
        "verified": verified,
        "verify_total": total if verify else 0,
        "failed": failed,
        "mode": "KO Control",
    }


KO_CONTROL_PATCH_WATCH_INTERVAL_SEC = 0.20

def ensure_ko_control_full_patch(enabled: bool, *, verify: bool = False) -> dict:
    """Keep the top-dock KO Control payload resident while the toggle is ON.

    Some game transitions can restore executable instructions after the toggle
    was already enabled. The GUI state would still say ON while the native
    input gates were back to stock. This watcher reads the expected FULL
    payload and rewrites only addresses that no longer match. It never restores
    the whole patch set during an ON check, so there is no temporary unpatched
    window.
    """
    if not enabled:
        return {"ok": True, "checked": 0, "reasserted": 0, "failed": []}

    expected = {}
    for addr, value in KO_CONTROL_FULL_PACKET:
        expected[int(addr)] = int(value) & 0xFFFFFFFF

    checked = 0
    reasserted = 0
    failed = []
    repaired = []
    for addr, value in expected.items():
        checked += 1
        try:
            actual = rd32(addr)
        except Exception:
            actual = None
        if actual is not None and (int(actual) & 0xFFFFFFFF) == value:
            continue

        try:
            ok = _ko_code_wd32(addr, value)
        except Exception:
            ok = False
        if not ok:
            failed.append(addr)
            continue

        if verify:
            try:
                rb = rd32(addr)
            except Exception:
                rb = None
            if rb is None or (int(rb) & 0xFFFFFFFF) != value:
                failed.append(addr)
                continue

        reasserted += 1
        repaired.append(addr)

    return {
        "ok": not failed,
        "checked": checked,
        "reasserted": reasserted,
        "repaired": repaired,
        "failed": failed,
    }


# The DOL packet restores the input path, but a survivor can already be parked
# in winner action 0x2A by the time FULL is detected. In that state the raw
# controller buffer updates normally while the fighter never asks the ordinary
# action resolver for a new move. Release only the living winner object.
KO_SURVIVOR_RESULT_ACTIONS = {0x0000002A}
KO_SURVIVOR_RELEASE_INTERVAL_SEC = 0.015
KO_SURVIVOR_FULL_LATCH_SEC = 30.0
_KO_SURVIVOR_RELEASE_LAST: dict[int, float] = {}
_KO_SURVIVOR_RELEASE_LOGGED: set[int] = set()
_KO_SURVIVOR_INPUT_PREV: dict[int, int] = {}
_KO_SURVIVOR_INPUT_LOGGED: set[int] = set()


def _ko_ctrl_bridge_survivor_input(slot: str, base: int) -> dict:
    """Bridge the live compact input shadow into the native interpreter packet.

    Some post-KO survivors keep updating fighter+0x1380 while fighter+0x13CC
    remains neutral even with the result-scene input gates patched. This is a
    survivor-only fallback. It does not synthesize inputs: it mirrors the
    compact direction/button byte the game is already updating.
    """
    try:
        base_i = int(base or 0)
    except Exception:
        base_i = 0
    if not base_i:
        return {"bridged": 0, "wrote": 0, "slot": str(slot or "?"), "token": 0}

    try:
        token = int(rd32(base_i + 0x1380) or 0) & 0xFF
    except Exception:
        token = 0
    try:
        native_held = int(rd32(base_i + 0x13CC) or 0) & 0xFFFFFFFF
    except Exception:
        native_held = 0

    previous_token = int(_KO_SURVIVOR_INPUT_PREV.get(base_i, 0) or 0) & 0xFF
    token_changed = token != previous_token
    native_low = native_held & 0xFF
    needs_bridge = native_low != token

    # If native input is already following the compact source, leave it alone.
    # Keep our edge baseline synchronized so a later fallback starts cleanly.
    if not needs_bridge and not token_changed:
        _KO_SURVIVOR_INPUT_PREV[base_i] = token
        return {
            "bridged": 0, "wrote": 0, "slot": str(slot or "?"),
            "token": token, "native_held": native_held, "held": native_held,
        }

    previous_held = _compact_token_to_current_mask(previous_token)
    current_held = _compact_token_to_current_mask(token)
    pressed = token & (~previous_token & 0xFF)
    released = previous_token & (~token & 0xFF)

    # +13D8 is the useful command-edge field in the live action recorder.
    # Feed the same fresh edge there while keeping +13D0/+13D4 coherent.
    packet = (
        (0x13C8, previous_held),
        (0x13CC, current_held),
        (0x13D0, pressed),
        (0x13D4, released),
        (0x13D8, pressed),
        (0x13DC, token),
    )
    wrote = 0
    for off, value in packet:
        try:
            if _ko_wd32(base_i + off, int(value) & 0xFFFFFFFF):
                wrote += 1
        except Exception:
            pass

    _KO_SURVIVOR_INPUT_PREV[base_i] = token
    if base_i not in _KO_SURVIVOR_INPUT_LOGGED and needs_bridge:
        _KO_SURVIVOR_INPUT_LOGGED.add(base_i)
        try:
            print(
                f"[ko control] input bridge {slot} base=0x{base_i:08X} "
                f"shadow=0x{token:02X} native=0x{native_held:08X} held=0x{current_held:08X}",
                flush=True,
            )
        except Exception:
            pass

    return {
        "bridged": 1 if wrote else 0,
        "wrote": wrote,
        "slot": str(slot or "?"),
        "token": token,
        "native_held": native_held,
        "held": current_held,
        "pressed": pressed,
        "released": released,
    }


def _ko_ctrl_snap_u32(snap: dict, key: str, base: int, off: int, default: int = 0) -> int:
    try:
        value = snap.get(key) if isinstance(snap, dict) else None
        if value is not None:
            return int(value) & 0xFFFFFFFF
    except Exception:
        pass
    if base:
        try:
            value = rd32(base + int(off))
            if value is not None:
                return int(value) & 0xFFFFFFFF
        except Exception:
            pass
    return int(default) & 0xFFFFFFFF


def _ko_ctrl_release_survivors(snaps: dict, state: dict, now: float, baseline_by_slot: dict | None = None) -> dict:
    """Clear the live winner result lock and escape action 0x2A.

    This is deliberately survivor-only. It does not revive the losing team,
    rewind the round manager, alter HP, or touch camera/result globals.
    """
    dead_team = ""
    if bool((state or {}).get("p1_dead")) and not bool((state or {}).get("p2_dead")):
        dead_team = "P1"
    elif bool((state or {}).get("p2_dead")) and not bool((state or {}).get("p1_dead")):
        dead_team = "P2"
    if not dead_team:
        return {"released": 0, "wrote": 0, "slots": []}

    winner_team = "P2" if dead_team == "P1" else "P1"
    wrote = 0
    released = 0
    bridged = 0
    touched = []
    input_slots = []
    seen_bases = set()

    candidates = []
    for slot in (f"{winner_team}-C1", f"{winner_team}-C2"):
        snap = (snaps or {}).get(slot)
        if not isinstance(snap, dict):
            continue
        try:
            base = int(snap.get("base") or 0)
        except Exception:
            base = 0
        if not base or base in seen_bases:
            continue
        seen_bases.add(base)
        hp = _ko_ctrl_snap_u32(snap, "cur", base, 0x28, 0)
        if hp <= 0:
            continue
        point_active = bool(
            snap.get("damage_point_active",
                     snap.get("damage_is_point",
                              snap.get("point_active", False)))
        )
        # Native +0x44A0 is the point-fighter flag used by the live damage
        # path. Read it as a fallback/confirmation so C2 winners do not depend
        # on profiler metadata being refreshed during the result transition.
        try:
            point_active = bool(point_active or int(rd32(base + 0x44A0) or 0))
        except Exception:
            pass
        candidates.append((slot, snap, base, point_active))

    # Prefer the actual point fighter when the runtime point flag is available.
    # If the result transition has already cleared that metadata, fall back to
    # the living winner object that is visibly parked in Win Pose.
    point_candidates = [row for row in candidates if row[3]]
    if point_candidates:
        candidates = point_candidates
    else:
        result_candidates = [
            row for row in candidates
            if _ko_ctrl_snap_u32(row[1], "attA", row[2], 0x1E8, 0) in KO_SURVIVOR_RESULT_ACTIONS
        ]
        if result_candidates:
            candidates = result_candidates

    for slot, snap, base, _point_active in candidates:
        action = _ko_ctrl_snap_u32(snap, "attA", base, 0x1E8, 0)

        last = float(_KO_SURVIVOR_RELEASE_LAST.get(base, 0.0) or 0.0)
        if (float(now) - last) < KO_SURVIVOR_RELEASE_INTERVAL_SEC:
            continue
        _KO_SURVIVOR_RELEASE_LAST[base] = float(now)

        baseline_entry = (baseline_by_slot or {}).get(slot) if isinstance(baseline_by_slot, dict) else None

        # Once Win Pose has been released, do not keep rewriting the fighter's
        # control words while it is action 1. The recomp shows the missing input
        # is upstream in 0x800767B0: result states 9/12 can skip the native pad
        # read entirely. With that gate patched, the game's own action resolver
        # must be allowed to mutate the fighter state naturally.

        # Once result action 0x2A has already landed, move the survivor back to
        # the generic idle cluster. Stop touching the action as soon as the game
        # accepts any ordinary action, so live player input owns it again.
        if action in KO_SURVIVOR_RESULT_ACTIONS:
            learned = _ko_ctrl_apply_neutral_baseline(
                slot, base, snap,
                baseline_entry,
            )
            if bool(learned.get("ok")):
                wrote += int(learned.get("wrote") or 0)
            else:
                # Fallback for a fighter that never produced one clean idle
                # sample before KO. Keep the legacy packet rather than leaving
                # the character trapped in Win Pose.
                for off, value in IDLE_RESTORE_PACKET_U32:
                    if _ko_wd32(base + int(off), int(value) & 0xFFFFFFFF):
                        wrote += 1
            released += 1
            touched.append(slot)
            if base not in _KO_SURVIVOR_RELEASE_LOGGED:
                _KO_SURVIVOR_RELEASE_LOGGED.add(base)
                try:
                    print(
                        f"[ko control] survivor release {slot} base=0x{base:08X} action=0x{action:02X}",
                        flush=True,
                    )
                except Exception:
                    pass

    return {
        "released": released, "bridged": bridged, "wrote": wrote,
        "slots": touched, "input_slots": input_slots,
    }


def _ko_ctrl_reset_survivor_release_state() -> None:
    _KO_SURVIVOR_RELEASE_LAST.clear()
    _KO_SURVIVOR_RELEASE_LOGGED.clear()
    _KO_SURVIVOR_INPUT_PREV.clear()
    _KO_SURVIVOR_INPUT_LOGGED.clear()


def _ko_ctrl_slot_is_dead(snap: dict) -> bool:
    """True when this loaded fighter slot is already in KO/dead state."""
    if not isinstance(snap, dict):
        return False
    try:
        base = int(snap.get("base") or 0)
    except Exception:
        base = 0

    hp_values = []
    for key in ("cur", "baroque_local_hp32"):
        try:
            if snap.get(key) is not None:
                hp_values.append(int(snap.get(key) or 0) & 0xFFFFFFFF)
        except Exception:
            pass
    if base:
        try:
            v = rd32(base + 0x28)
            if v is not None:
                hp_values.append(int(v) & 0xFFFFFFFF)
        except Exception:
            pass

    act_values = []
    for key in ("attA", "attB", "mv_id_display"):
        try:
            if snap.get(key) is not None:
                act_values.append(int(snap.get(key) or 0) & 0xFFFFFFFF)
        except Exception:
            pass
    if base:
        for off in (0x1E8, 0x1EC, 0x218):
            try:
                v = rd32(base + off)
                if v is not None:
                    act_values.append(int(v) & 0xFFFFFFFF)
            except Exception:
                pass

    hp_dead = bool(hp_values) and min(hp_values) <= 0
    act_dead = any(v in (0x9A, 0x9B) for v in act_values)
    return bool(hp_dead or act_dead)




KO_NATIVE_BATTLE_STATE_PTR = 0x803EFB4C
KO_NATIVE_RESULT_STATES = frozenset((9, 10, 11, 12))

def read_ko_native_battle_state() -> int | None:
    """Read the game's native battle/result state used by the input router.

    Recomp path 0x80076804..0x80076820 loads *(0x803EFB4C), then reads
    +0x0C from that object before deciding whether the fighter reaches the
    native controller reader. Result phases observed in post-KO dumps are
    9, 11, and 12; 10 is kept with that contiguous result-state family.
    """
    try:
        obj = rd32(KO_NATIVE_BATTLE_STATE_PTR)
        if obj is None:
            return None
        obj = int(obj) & 0xFFFFFFFF
        if not (0x80000000 <= obj < 0x94000000):
            return None
        value = rd32(obj + 0x0C)
        if value is None:
            return None
        return int(value) & 0xFFFFFFFF
    except Exception:
        return None


def ko_ctrl_team_ko_state(snaps: dict) -> dict:
    """Return whether either loaded team is fully KO'd.

    The top-dock KO Ctrl toggle is an auto arm, not a permanent DOL patch.
    The Control+Full DOL packet is active only while a whole team is KO'd.
    """
    team_slots = {"P1": [], "P2": []}
    seen_by_team = {"P1": set(), "P2": set()}
    for slot in ("P1-C1", "P1-C2", "P2-C1", "P2-C2"):
        snap = (snaps or {}).get(slot)
        if not isinstance(snap, dict):
            continue
        try:
            base = int(snap.get("base") or 0)
        except Exception:
            base = 0
        if not base:
            continue
        team = "P1" if slot.startswith("P1") else "P2"
        if base in seen_by_team[team]:
            continue
        seen_by_team[team].add(base)
        team_slots[team].append((slot, snap))

    dead = {}
    loaded = {}
    parts = []
    for team in ("P1", "P2"):
        entries = team_slots.get(team) or []
        loaded[team] = len(entries)
        if entries:
            team_dead = all(_ko_ctrl_slot_is_dead(snap) for _slot, snap in entries)
        else:
            team_dead = False
        dead[team] = bool(team_dead)
        if entries:
            parts.append(f"{team}:{'KO' if team_dead else 'live'}({len(entries)})")
        else:
            parts.append(f"{team}:none")

    return {
        "p1_dead": dead.get("P1", False),
        "p2_dead": dead.get("P2", False),
        "any_team_dead": bool(dead.get("P1", False) or dead.get("P2", False)),
        "both_loaded": bool(loaded.get("P1", 0) and loaded.get("P2", 0)),
        "loaded": loaded,
        "summary": " ".join(parts),
    }




def maintain_ko_survivor_control(enabled: bool, snaps: dict, now: float, baseline_by_slot: dict | None = None) -> dict:
    """Keep the living point fighter in the native action resolver after KO.

    The DOL packet preserves the controller and resolver paths. This live helper
    handles the one remaining object-state problem: the survivor may already be
    carrying result flags or Win Pose action 42. It touches only the living
    winning fighter object and never revives the loser or rewinds scene globals.

    A short winner latch survives losing-team pointer unload during the result
    transition. It clears as soon as both teams are live again.
    """
    now_f = float(now)
    if not enabled:
        _ko_ctrl_reset_survivor_release_state()
        maintain_ko_survivor_control.winner_team = ""
        maintain_ko_survivor_control.until = 0.0
        return {"active": False, "released": 0, "wrote": 0, "slots": []}

    state = ko_ctrl_team_ko_state(snaps)
    winner_team = str(getattr(maintain_ko_survivor_control, "winner_team", "") or "")
    until = float(getattr(maintain_ko_survivor_control, "until", 0.0) or 0.0)

    current_winner = ""
    if bool(state.get("p1_dead")) and not bool(state.get("p2_dead")):
        current_winner = "P2"
    elif bool(state.get("p2_dead")) and not bool(state.get("p1_dead")):
        current_winner = "P1"

    if current_winner:
        winner_team = current_winner
        until = max(until, now_f + KO_SURVIVOR_FULL_LATCH_SEC)
        maintain_ko_survivor_control.winner_team = winner_team
        maintain_ko_survivor_control.until = until

    both_live = bool(state.get("both_loaded") and not state.get("any_team_dead"))
    if both_live:
        _ko_ctrl_reset_survivor_release_state()
        maintain_ko_survivor_control.winner_team = ""
        maintain_ko_survivor_control.until = 0.0
        return {"active": False, "released": 0, "wrote": 0, "slots": [], "state": state}

    if not current_winner and winner_team and now_f < until:
        # Reconstruct only the dead-side identity. The live fighter objects still
        # come from the current snapshots, so no stale fighter pointer is reused.
        state = dict(state or {})
        state["p1_dead"] = winner_team == "P2"
        state["p2_dead"] = winner_team == "P1"
        state["any_team_dead"] = True
        state["latched_winner"] = winner_team

    if not bool(state.get("any_team_dead")):
        return {"active": False, "released": 0, "wrote": 0, "slots": [], "state": state}

    result = _ko_ctrl_release_survivors(snaps, state, now_f, baseline_by_slot=baseline_by_slot)
    result = dict(result or {})
    result["active"] = True
    result["state"] = state
    return result


def tick_ko_control_auto(enabled: bool, live_active: bool, snaps: dict, now: float, last_apply: float, *, verify: bool = False, baseline_by_slot: dict | None = None) -> tuple[bool, float, dict | None, dict]:
    """Auto-arm KO Control without leaking into the next arcade match.

    SAFE stays armed during live play. FULL activates after a full-team KO,
    lingers through the result transition, then returns to SAFE when both teams
    are live again. Turning the button OFF restores the original instructions.
    """
    state = ko_ctrl_team_ko_state(snaps)
    native_battle_state = read_ko_native_battle_state()
    native_result_now = native_battle_state in KO_NATIVE_RESULT_STATES
    result = None
    now = float(now)
    last_apply = float(last_apply or 0.0)

    full_until = float(getattr(tick_ko_control_auto, "full_until", 0.0) or 0.0)
    safe_last = float(getattr(tick_ko_control_auto, "safe_last", 0.0) or 0.0)
    post_ko_latched = bool(getattr(tick_ko_control_auto, "post_ko_latched", False))

    if not enabled:
        tick_ko_control_auto.full_until = 0.0
        tick_ko_control_auto.safe_last = 0.0
        tick_ko_control_auto.post_ko_latched = False
        tick_ko_control_auto.last_mode = "off"
        _ko_ctrl_reset_survivor_release_state()
        if live_active or (now - safe_last) > 0.01:
            result = apply_ko_control_auto_mode("off", verify=verify)
        return False, now, result, state

    both_live = bool(state.get("both_loaded") and not state.get("any_team_dead"))
    full_ko_now = bool(state.get("both_loaded") and state.get("any_team_dead"))

    # Enter FULL as soon as either the snapshots prove a team KO or the game's
    # own battle object enters a result phase. Once entered, stay latched. A
    # post-KO action can move the native state out of 9..12 even though the
    # defeated team is still dead, so native-state family membership is not a
    # valid signal to turn FULL back off.
    if full_ko_now or native_result_now:
        post_ko_latched = True
        tick_ko_control_auto.post_ko_latched = True
        full_until = max(full_until, now + KO_SURVIVOR_FULL_LATCH_SEC)
        tick_ko_control_auto.full_until = full_until

    # A new live round is the only normal reset condition: the game is back in
    # its known live battle state and both teams are loaded with no KO fighter.
    # This lets FULL survive arbitrary result-state changes while the winner is
    # moving post-KO, but drops the aggressive controller route as soon as the
    # next real round/match is ready.
    new_live_round = bool(post_ko_latched and native_battle_state == 4 and both_live)
    if new_live_round:
        post_ko_latched = False
        tick_ko_control_auto.post_ko_latched = False
        full_until = 0.0
        tick_ko_control_auto.full_until = 0.0
        _ko_ctrl_reset_survivor_release_state()

    if post_ko_latched:
        want = "full"
        want_full = True
    elif full_ko_now or native_result_now:
        # Defensive fallback for the same tick that establishes the latch.
        want = "full"
        want_full = True
    else:
        want = "safe"
        want_full = False

    prior_mode = str(getattr(tick_ko_control_auto, "last_mode", "") or "")
    if want != prior_mode:
        try:
            print(
                f"[ko control] auto -> {want.upper()} "
                f"native_state={native_battle_state if native_battle_state is not None else '?'} "
                f"snapshot={state.get('summary', '?')}",
                flush=True,
            )
        except Exception:
            pass
        tick_ko_control_auto.last_mode = want

    interval = 0.75 if want == "full" else 1.50
    if (want_full != bool(live_active)) or ((now - last_apply) > interval):
        result = apply_ko_control_auto_mode(want, verify=verify)
        applied_ok = bool(result.get("ok", False))
        live_active = bool(want_full and applied_ok)
        last_apply = now
        if want == "safe" and applied_ok:
            tick_ko_control_auto.safe_last = now

    release_result = {"released": 0, "wrote": 0, "slots": []}
    if want_full and (bool(live_active) or bool(result and result.get("ok", False))):
        release_result = _ko_ctrl_release_survivors(snaps, state, now, baseline_by_slot=baseline_by_slot)

    state = dict(state or {})
    state["native_battle_state"] = native_battle_state
    state["native_result_now"] = bool(native_result_now)
    state["post_ko_latched"] = bool(post_ko_latched)
    state["new_live_round"] = bool(new_live_round)
    state["auto_mode"] = want
    state["full_until"] = full_until
    state["apply_ok"] = bool(result.get("ok", True)) if isinstance(result, dict) else True
    state["survivor_release"] = release_result
    return bool(live_active), float(last_apply or 0.0), result, state


def ko_dol_status_text(result: dict) -> str:
    name = str(result.get("name") or "?")
    wrote = int(result.get("wrote") or 0)
    total = int(result.get("total") or 0)
    verified = int(result.get("verified") or 0)
    verify_total = int(result.get("verify_total") or 0)
    note = str(result.get("note") or "")
    if verify_total:
        return f"KO DOL {name}: wrote {wrote}/{total}, verified {verified}/{verify_total}; {note}"
    return f"KO DOL {name}: wrote {wrote}/{total}; {note}"


def ko_dol_button_label(active_idx: int) -> str:
    try:
        idx = int(active_idx)
    except Exception:
        idx = -1
    if 0 <= idx < len(KO_DOL_PATCH_TESTS):
        return str(KO_DOL_PATCH_TESTS[idx].get("short") or "KO DOL")
    return "KO DOL"

__all__ = [
    'IDLE_RESTORE_PACKET_U32',
    'apply_idle_restore_packet',
    'IDLE_RESTORE_HOLD_SECONDS',
    'KO_ROUND_REWIND_PACKET_U32',
    'KO_RESCUE_HP_FLOOR',
    'KO_RESCUE_HOLD_SECONDS',
    'KO_REWIND_OFFSETS_U32',
    'KO_REWIND_BAD_ACTIONS',
    '_write_u32_count',
    '_slot_side',
    'capture_ko_rewind_baselines',
    'apply_slot_rewind_baseline',
    'apply_ko_rescue_packet',
    'KO_SLOT_FLAG_HOLD_OFFSETS_U32',
    'KO_SLOT_ACTION_HOLD_OFFSETS_U32',
    'apply_slot_only_ko_hold',
    'idle_restore_status_text',
    'KO_GLOBAL_HOLD_GROUPS',
    'capture_ko_global_baseline',
    'apply_ko_global_hold',
    'INPUT_BUFFER_FIELDS_U32',
    '_compact_token_to_current_mask',
    '_compact_token_to_button_mask',
    'apply_ko_input_inject',
    'KO_DOL_ORIGINALS_U32',
    'NO_CLEAR_75E_WRITES',
    'NO_CLEAR_763_WRITES',
    'NO_BUFFER_OUT_WRITES',
    'PATCH_8D9C_IDLE',
    'KEEP_LOW_INPUT_BYTE',
    'SKIP_64_RESULT_OVERRIDE',
    'POST_KO_CONTROL_PACKET',
    'KO_RESULT_RESOLVER_UNLOCK_PACKET',
    'FORCE_PAD_READ',
    'NO_A7C_CLEAR',
    'NO_LATE_13D0_CLEAR',
    'FORCE_BUFFER_BUILD',
    'NO_ZERO_R31',
    'KO_CONTROL_SAFE_PACKET',
    'KO_CONTROL_FULL_PACKET',
    'KO_DOL_PATCH_TESTS',
    'apply_ko_dol_result_patch',
    'apply_ko_control_auto_mode',
    'apply_ko_control_full_toggle',
    'ensure_ko_control_full_patch',
    'KO_CONTROL_PATCH_WATCH_INTERVAL_SEC',
    'KO_SURVIVOR_RESULT_ACTIONS',
    'KO_SURVIVOR_FULL_LATCH_SEC',
    'KO_NATIVE_BATTLE_STATE_PTR',
    'KO_NATIVE_RESULT_STATES',
    'read_ko_native_battle_state',
    '_ko_ctrl_release_survivors',
    '_ko_ctrl_slot_is_dead',
    'ko_ctrl_team_ko_state',
    'maintain_ko_survivor_control',
    'tick_ko_control_auto',
    'ko_dol_status_text',
    'ko_dol_button_label'
]
