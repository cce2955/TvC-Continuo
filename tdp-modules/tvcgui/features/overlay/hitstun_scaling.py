"""Live air-recovery deterioration state for the transparent HUD.

TvC keeps ordinary resolved hitstun at fighter +0x1210, while airborne recovery
uses a separate lockout timer at fighter +0x1220. The native deterioration path
reduces that lockout by floor(fighter[+0x11CC] / 4) and clamps the initialized
result to a minimum of four frames.

The HUD treats each observed hit as its own recovery clock. Native airborne
recovery lockout at +0x1220 is authoritative when present; ordinary resolved
hitstun at +0x1210 is the fallback so the clock exists before deterioration and
on hits that never enter the air-recovery lane. A later hit mints a new
generation and immediately discards the previous clock, even when the new
window is shorter.

This module is read-only. Realtime values normally come from the existing 240 Hz
fighter snapshot, so the HS display does not add another Dolphin memory read.
Direct reads remain as a fallback for source/debug paths without the sampler.
"""
from __future__ import annotations

from typing import Any

RULE_CONTEXT_PTR_ADDR = 0x803EFB4C
RULE_ARRAY_OFF = 0x5C
HITSTUN_DECAY_RULE_INDEX = 11

OFF_STATE_FLAGS_6C = 0x006C
OFF_COMBO_COUNT = 0x11C4
OFF_DECAY_COUNTER = 0x11CC
OFF_HITSTUN_TIMER = 0x1210
OFF_UNTECH_TIMER = 0x1220

CANTUKEMI_MASK = 0x02000000
MIN_UNTECH_FRAMES = 4
DECAY_NUMERATOR = 1
DECAY_DENOMINATOR = 4

SLOT_LABELS = ("P1-C1", "P1-C2", "P2-C1", "P2-C2")
_STATE_CACHE: dict[str, dict] = {}
_LAST_BASES: dict[str, int] = {}
_LATCHES: dict[str, dict] = {}
_POLL_COUNTER = 0
_RULE_CACHE: bool | None = None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _read_u32(address: int) -> int:
    if not address:
        return 0
    try:
        from tvcgui.platform.dolphin import rd32

        return _safe_int(rd32(int(address))) & 0xFFFFFFFF
    except Exception:
        return 0


def _valid_runtime_ptr(value: int) -> bool:
    value = int(value) & 0xFFFFFFFF
    return 0x80000000 <= value <= 0x817FFFFF or 0x90000000 <= value <= 0x93FFFFFF


def _rule_enabled() -> bool | None:
    """Read the native rule gate used by the deterioration branch."""
    context = _read_u32(RULE_CONTEXT_PTR_ADDR)
    if not _valid_runtime_ptr(context):
        return None
    value = _read_u32(context + RULE_ARRAY_OFF + HITSTUN_DECAY_RULE_INDEX * 4)
    return bool(value)


def _point_slot(payload: dict, team: str) -> tuple[str, dict] | None:
    rows: list[tuple[str, dict]] = []
    for suffix in ("C1", "C2"):
        label = f"{team}-{suffix}"
        row = payload.get(label)
        if isinstance(row, dict):
            rows.append((label, row))
    if not rows:
        return None

    point_rows = [
        item for item in rows
        if bool(
            item[1].get(
                "damage_point_active",
                item[1].get("damage_is_point", item[1].get("realtime_point_active", False)),
            )
        )
    ]
    if len(point_rows) == 1:
        return point_rows[0]
    return rows[0]


def _owner_slot(payload: dict, team: str) -> tuple[str, dict] | None:
    # Preserve the discovered fixed-C1 owner lane first. Point is the fallback
    # for partial payloads where C1 is unavailable.
    c1 = f"{team}-C1"
    row = payload.get(c1)
    if isinstance(row, dict) and _safe_int(row.get("base")):
        return c1, row
    return _point_slot(payload, team)


def _sanitize_counter(value: int) -> int:
    return max(0, min(999, _safe_int(value)))


def _sanitize_timer(value: int) -> int:
    value = _safe_int(value) & 0xFFFFFFFF
    if value & 0x80000000:
        value -= 0x100000000
    if value < 0 or value > 999:
        return 0
    return int(value)


def _row_u32(row: dict, realtime_key: str, base: int, offset: int) -> int:
    """Prefer the cached 240 Hz packet and fall back to a direct read."""
    if isinstance(row, dict) and realtime_key in row:
        return _safe_int(row.get(realtime_key)) & 0xFFFFFFFF
    return _read_u32(base + offset) if base else 0


def _empty_state(rule_enabled: bool | None) -> dict:
    return {
        "hitstun_decay_live": False,
        "hitstun_decay_rule_enabled": rule_enabled,
        "hitstun_decay_combo_count": 0,
        "hitstun_decay_counter": 0,
        "hitstun_decay_frames": 0,
        "hitstun_decay_min_frames": MIN_UNTECH_FRAMES,
        "hitstun_untech_remaining": 0,
        "hitstun_hitstun_remaining": 0,
        "hitstun_clock_remaining": 0,
        "hitstun_clock_source": "",
        "hitstun_untech_effective_start": 0,
        "hitstun_untech_expiry_target": 0,
        "hitstun_untech_elapsed": 0,
        "hitstun_untech_generation": 0,
        "hitstun_untech_active": False,
        "hitstun_untech_expired": False,
        "hitstun_untech_base_estimate": 0,
        "hitstun_untech_latched_loss": 0,
        "hitstun_untech_approximate": False,
        "hitstun_cantukemi": False,
        "hitstun_decay_owner_slot": "",
        "hitstun_decay_victim_slot": "",
        "_hitstun_victim_base": 0,
        "_hitstun_victim_hp": 0,
        "_hitstun_sample_ns": 0,
        "_hitstun_authored_raw": 0,
        "_hitstun_victim_action_id": 0,
        "_hitstun_victim_action_frame": 0,
        "_hitstun_victim_freeze": 0,
    }


def _snapshot_team(payload: dict, team: str, rule_enabled: bool | None) -> dict:
    owner_info = _owner_slot(payload, team)
    opponent = "P2" if team == "P1" else "P1"
    victim_info = _point_slot(payload, opponent)
    if owner_info is None:
        return _empty_state(rule_enabled)

    owner_label, owner = owner_info
    owner_base = _safe_int(owner.get("base"))
    combo_count = _sanitize_counter(
        _row_u32(owner, "realtime_fighter_combo_count", owner_base, OFF_COMBO_COUNT)
    )
    authored_raw = max(0, _safe_int(owner.get("move_hitstun")))
    counter = _sanitize_counter(
        _row_u32(owner, "realtime_decay_counter", owner_base, OFF_DECAY_COUNTER)
    )

    decay_frames = counter // DECAY_DENOMINATOR
    if rule_enabled is False:
        decay_frames = 0

    victim_label = ""
    victim_base = 0
    victim_hp = 0
    sample_ns = 0
    hitstun_remaining = 0
    untech_remaining = 0
    cantukemi = False
    victim_action_id = 0
    victim_action_frame = 0
    victim_freeze = 0
    if victim_info is not None:
        victim_label, victim = victim_info
        victim_base = _safe_int(victim.get("base"))
        hitstun_remaining = _sanitize_timer(
            _row_u32(victim, "realtime_hitstun_remaining", victim_base, OFF_HITSTUN_TIMER)
        )
        untech_remaining = _sanitize_timer(
            _row_u32(victim, "realtime_untech_remaining", victim_base, OFF_UNTECH_TIMER)
        )
        state_flags = _row_u32(victim, "realtime_state_flags_6c", victim_base, OFF_STATE_FLAGS_6C)
        cantukemi = bool(state_flags & CANTUKEMI_MASK)
        victim_hp = max(0, _safe_int(victim.get("realtime_current_hp", victim.get("cur"))))
        sample_ns = max(0, _safe_int(victim.get("realtime_sample_ns")))
        victim_action_id = max(0, _safe_int(victim.get("realtime_action_id")))
        victim_action_frame = max(0, _safe_int(victim.get("realtime_action_frame")))
        victim_freeze = max(0, _safe_int(victim.get("realtime_impact_freeze_remaining")))

    return {
        "hitstun_decay_live": bool(owner_base),
        "hitstun_decay_rule_enabled": rule_enabled,
        "hitstun_decay_combo_count": combo_count,
        "hitstun_decay_counter": counter,
        "hitstun_decay_frames": max(0, decay_frames),
        "hitstun_decay_min_frames": MIN_UNTECH_FRAMES,
        "hitstun_untech_remaining": untech_remaining,
        "hitstun_hitstun_remaining": hitstun_remaining,
        "hitstun_clock_remaining": untech_remaining if untech_remaining > 0 else hitstun_remaining,
        "hitstun_clock_source": "untech" if untech_remaining > 0 else ("hitstun" if hitstun_remaining > 0 else ""),
        "hitstun_untech_effective_start": 0,
        "hitstun_untech_expiry_target": 0,
        "hitstun_untech_elapsed": 0,
        "hitstun_untech_generation": 0,
        "hitstun_untech_active": False,
        "hitstun_untech_expired": False,
        "hitstun_untech_base_estimate": 0,
        "hitstun_untech_latched_loss": 0,
        "hitstun_untech_approximate": False,
        "hitstun_cantukemi": cantukemi,
        "hitstun_decay_owner_slot": owner_label,
        "hitstun_decay_victim_slot": victim_label,
        "_hitstun_victim_base": victim_base,
        "_hitstun_victim_hp": victim_hp,
        "_hitstun_sample_ns": sample_ns,
        "_hitstun_authored_raw": authored_raw,
        "_hitstun_victim_action_id": victim_action_id,
        "_hitstun_victim_action_frame": victim_action_frame,
        "_hitstun_victim_freeze": victim_freeze,
    }


def _inclusive_base_estimate(effective_start: int, loss: int) -> int:
    """Convert the live countdown endpoint back to the player-facing raw span.

    The HUD labels hitstun as inclusive frame endpoints. Under that convention,
    an observed effective endpoint of 21 with a native -4F deterioration step
    corresponds to a displayed raw span of approximately 24F, not 25F.
    """
    effective = max(0, _safe_int(effective_start))
    removed = max(0, _safe_int(loss))
    if effective <= 0:
        return 0
    return max(effective, effective + max(0, removed - 1))


def _apply_untech_latch(team: str, state: dict) -> dict:
    """Turn contact + native timers into a replace-on-hit expiry clock.

    Contact is the start signal, but only native game counters may advance the
    clock. A provisional contact generation may exist before +0x1210/+0x1220
    appears, but it remains frozen until the game publishes a native remaining
    value. This keeps Dolphin pause/frame-step perfectly deterministic.
    """
    latch = _LATCHES.setdefault(team, {
        "victim_base": 0,
        "victim_hp": 0,
        "prev_remaining": 0,
        "prev_counter": 0,
        "prev_combo_count": 0,
        "effective_start": 0,
        "base_estimate": 0,
        "loss": 0,
        "approximate": False,
        "generation": 0,
        "elapsed": 0,
        "sample_ns": 0,
        "source": "",
        "contact_ns": 0,
        "last_authored_hitstun": 0,
        "age_frames": 0,
        "last_action_id": 0,
        "last_action_frame": 0,
        "retired": False,
    })

    victim_base = _safe_int(state.get("_hitstun_victim_base"))
    victim_hp = max(0, _safe_int(state.get("_hitstun_victim_hp")))
    untech_remaining = max(0, _safe_int(state.get("hitstun_untech_remaining")))
    hitstun_remaining = max(0, _safe_int(state.get("hitstun_hitstun_remaining")))
    candidate_source = "untech" if untech_remaining > 0 else ("hitstun" if hitstun_remaining > 0 else "")
    candidate_remaining = untech_remaining if candidate_source == "untech" else hitstun_remaining
    counter = max(0, _safe_int(state.get("hitstun_decay_counter")))
    combo_count = max(0, _safe_int(state.get("hitstun_decay_combo_count")))
    native_loss = max(0, _safe_int(state.get("hitstun_decay_frames")))
    sample_ns = max(0, _safe_int(state.get("_hitstun_sample_ns")))
    authored_raw = max(0, _safe_int(state.get("_hitstun_authored_raw")))
    action_id = max(0, _safe_int(state.get("_hitstun_victim_action_id")))
    action_frame = max(0, _safe_int(state.get("_hitstun_victim_action_frame")))
    freeze = max(0, _safe_int(state.get("_hitstun_victim_freeze")))
    if authored_raw > 0:
        latch["last_authored_hitstun"] = authored_raw

    previous_base = _safe_int(latch.get("victim_base"))
    previous_hp = max(0, _safe_int(latch.get("victim_hp")))
    previous = max(0, _safe_int(latch.get("prev_remaining")))
    previous_counter = max(0, _safe_int(latch.get("prev_counter")))
    previous_combo = max(0, _safe_int(latch.get("prev_combo_count")))
    previous_source = str(latch.get("source") or "")

    # Once a native lane is known, stay on it until replacement. A provisional
    # contact clock is allowed to adopt the first native lane that appears.
    if previous_source == "untech":
        remaining = untech_remaining
    elif previous_source == "hitstun":
        remaining = hitstun_remaining
    else:
        remaining = candidate_remaining

    victim_changed = victim_base != previous_base
    combo_advanced = combo_count > previous_combo
    hp_dropped = (
        not victim_changed
        and previous_hp > 0
        and victim_hp >= 0
        and victim_hp < previous_hp
    )
    counter_changed = counter != previous_counter
    # A larger same-lane timer is not by itself proof of a new hit. Sampler
    # ordering can expose an older/larger native value after a smaller one.
    # Only real contact evidence (HP/combo/counter transition) may mint a new
    # generation.
    contact_event = bool(combo_advanced or hp_dropped)
    sample_advanced = sample_ns <= 0 or sample_ns >= max(0, _safe_int(latch.get("sample_ns")))
    timer_rearmed = bool(sample_advanced and candidate_remaining > 0 and candidate_remaining > previous)
    explicit_new_hit = bool(
        contact_event
        or timer_rearmed
        or (counter_changed and candidate_remaining > 0 and candidate_remaining >= previous)
    )

    # Convert authored raw stun to the same inclusive endpoint convention used
    # by the HUD. Example: RAW 24 with -4F deterioration displays 21F.
    raw_hint = authored_raw or max(0, _safe_int(latch.get("last_authored_hitstun")))
    authored_target = max(0, raw_hint - max(0, native_loss - 1)) if raw_hint > 0 else 0
    provisional_target = max(candidate_remaining, authored_target)

    # Contact itself can mint the clock even if the native countdown is still
    # zero. That is the latency fix. Timer-only starts remain as fallback for
    # projectiles/unknown moves that lack an authored hitstun hint.
    retired = bool(latch.get("retired", False)) and not victim_changed
    first_timer = candidate_remaining > 0 and _safe_int(latch.get("effective_start")) <= 0 and not retired
    new_lock = bool(
        (contact_event and provisional_target > 0)
        or (candidate_remaining > 0 and not retired and (victim_changed or first_timer or explicit_new_hit))
    )

    if new_lock:
        source = candidate_source or "contact"
        target = provisional_target if contact_event and provisional_target > 0 else candidate_remaining
        # Prefer the native observed endpoint if it is already present and the
        # authored hint is missing. Otherwise the authored endpoint lets the
        # bar exist on the contact frame.
        target = max(1, int(target or candidate_remaining or authored_target or 1))
        loss = native_loss if native_loss > 0 else 0
        latch["generation"] = max(0, _safe_int(latch.get("generation"))) + 1
        latch["retired"] = False
        latch["source"] = source
        latch["effective_start"] = target
        latch["loss"] = loss
        latch["base_estimate"] = raw_hint if raw_hint > 0 else _inclusive_base_estimate(target, loss)
        latch["approximate"] = bool(source == "contact" or authored_raw > 0)
        latch["elapsed"] = 0
        latch["age_frames"] = 0
        latch["last_action_id"] = action_id
        latch["last_action_frame"] = action_frame
        latch["contact_ns"] = 0
        remaining = candidate_remaining
    else:
        target = max(0, _safe_int(latch.get("effective_start")))
        if target > 0:
            if sample_ns > 0 and sample_ns != _safe_int(latch.get("sample_ns")):
                prior_action_id = _safe_int(latch.get("last_action_id"))
                prior_action_frame = max(0, _safe_int(latch.get("last_action_frame")))
                if freeze <= 0:
                    if action_id == prior_action_id and action_frame > prior_action_frame:
                        advance = min(4, action_frame - prior_action_frame)
                    elif action_id != prior_action_id:
                        advance = 1
                    else:
                        advance = 0
                    latch["age_frames"] = _safe_int(latch.get("age_frames")) + advance
                latch["last_action_id"] = action_id
                latch["last_action_frame"] = action_frame
            native_elapsed = 0
            if previous_source == "untech":
                # Never switch to +0x1210 when the authoritative air-recovery
                # lane reaches zero. Zero means this hit's air clock expired.
                native_elapsed = target if untech_remaining <= 0 else max(0, target - min(target, untech_remaining))
            elif previous_source == "hitstun":
                native_elapsed = target if hitstun_remaining <= 0 else max(0, target - min(target, hitstun_remaining))
            elif candidate_remaining > 0:
                # A provisional contact clock adopts the first native lane that
                # appears without minting a new generation.
                if previous_source == "contact" and candidate_source:
                    latch["source"] = candidate_source
                native_elapsed = max(0, target - min(target, candidate_remaining))

            # Never let a late/stale native sample refill the bar. Native game
            # counter changes are the only thing allowed to advance it.
            latch["elapsed"] = max(
                max(0, _safe_int(latch.get("elapsed"))),
                min(target, native_elapsed),
            )
            if _safe_int(latch.get("age_frames")) >= target * 2:
                latch["elapsed"] = target
                latch["retired"] = True

    target = max(0, _safe_int(latch.get("effective_start")))
    elapsed = max(0, min(target, _safe_int(latch.get("elapsed")))) if target > 0 else 0
    clock_remaining = max(0, target - elapsed) if target > 0 else 0
    if target > 0 and clock_remaining <= 0:
        latch["retired"] = True

    # True neutral clears old geometry. During a combo, an expired clock remains
    # visible until the next hit replaces it.
    if target > 0 and clock_remaining <= 0 and combo_count <= 0 and counter <= 0:
        latch["effective_start"] = 0
        latch["base_estimate"] = 0
        latch["loss"] = 0
        latch["approximate"] = False
        latch["elapsed"] = 0
        latch["age_frames"] = 0
        latch["source"] = ""
        latch["contact_ns"] = 0
        target = 0
        elapsed = 0
        clock_remaining = 0

    latch["victim_base"] = victim_base
    latch["victim_hp"] = victim_hp
    latch["prev_remaining"] = candidate_remaining
    latch["prev_counter"] = counter
    latch["prev_combo_count"] = combo_count
    latch["sample_ns"] = sample_ns

    state["hitstun_clock_remaining"] = clock_remaining
    state["hitstun_clock_source"] = str(latch.get("source") or "")
    state["hitstun_untech_effective_start"] = target
    state["hitstun_untech_expiry_target"] = target
    state["hitstun_untech_elapsed"] = elapsed
    state["hitstun_untech_generation"] = max(0, _safe_int(latch.get("generation")))
    state["hitstun_untech_active"] = bool(target > 0 and elapsed < target)
    state["hitstun_untech_expired"] = bool(target > 0 and elapsed >= target)
    state["hitstun_untech_base_estimate"] = max(target, _safe_int(latch.get("base_estimate")))
    state["hitstun_untech_latched_loss"] = max(0, _safe_int(latch.get("loss")))
    state["hitstun_untech_approximate"] = bool(latch.get("approximate", False))
    return state


def annotate_hitstun_scaling_payload(payload: dict, render_snap_by_slot: dict | None = None) -> None:
    """Add native air-recovery deterioration fields to each team row in-place."""
    global _POLL_COUNTER, _RULE_CACHE
    del render_snap_by_slot
    if not isinstance(payload, dict):
        return

    _POLL_COUNTER += 1
    bases = {
        label: _safe_int((payload.get(label) or {}).get("base"))
        for label in SLOT_LABELS
        if isinstance(payload.get(label), dict)
    }
    roster_changed = bases != _LAST_BASES
    missing = any(team not in _STATE_CACHE for team in ("P1", "P2"))

    # The per-hit clock itself refreshes every manager frame. Only the global
    # rule pointer is slow-changing, so avoid rereading that pointer 60 times a
    # second. All fighter values normally come from the 240 Hz cached packet.
    if roster_changed or missing or _RULE_CACHE is None or (_POLL_COUNTER % 30) == 0:
        _RULE_CACHE = _rule_enabled()

    for team in ("P1", "P2"):
        state = _snapshot_team(payload, team, _RULE_CACHE)
        _STATE_CACHE[team] = _apply_untech_latch(team, state)

    _LAST_BASES.clear()
    _LAST_BASES.update(bases)

    for team in ("P1", "P2"):
        state = dict(_STATE_CACHE.get(team) or {})
        state.pop("_hitstun_victim_base", None)
        state.pop("_hitstun_victim_hp", None)
        state.pop("_hitstun_sample_ns", None)
        state.pop("_hitstun_authored_raw", None)
        state.pop("_hitstun_victim_action_id", None)
        state.pop("_hitstun_victim_action_frame", None)
        state.pop("_hitstun_victim_freeze", None)
        for suffix in ("C1", "C2"):
            row = payload.get(f"{team}-{suffix}")
            if isinstance(row, dict):
                row.update(state)
