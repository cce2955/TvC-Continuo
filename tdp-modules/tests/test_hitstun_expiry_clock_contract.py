from pathlib import Path

from tvcgui.features.overlay import hitstun_scaling as hs

ROOT = Path(__file__).resolve().parents[1]


def _state(rem, combo, *, loss=4, counter=16, hp=900, base=0x90001000, hitstun=0):
    state = hs._empty_state(True)
    state.update({
        "hitstun_decay_live": True,
        "hitstun_decay_combo_count": combo,
        "hitstun_decay_counter": counter,
        "hitstun_decay_frames": loss,
        "hitstun_untech_remaining": rem,
        "hitstun_hitstun_remaining": hitstun,
        "_hitstun_victim_base": base,
        "_hitstun_victim_hp": hp,
    })
    return state


def test_inclusive_display_matches_24_raw_minus_4_to_21_endpoint():
    assert hs._inclusive_base_estimate(21, 4) == 24


def test_per_hit_clock_advances_from_native_timer_and_stops_at_target():
    hs._LATCHES.clear()
    first = hs._apply_untech_latch("P1", _state(21, 1))
    assert first["hitstun_untech_generation"] == 1
    assert first["hitstun_untech_expiry_target"] == 21
    assert first["hitstun_untech_elapsed"] == 0

    next_frame = hs._apply_untech_latch("P1", _state(20, 1))
    assert next_frame["hitstun_untech_elapsed"] == 1

    expired = hs._apply_untech_latch("P1", _state(0, 1))
    assert expired["hitstun_untech_elapsed"] == 21
    assert expired["hitstun_untech_expired"] is True


def test_stalled_native_clock_retires_after_twice_its_span_of_game_frames():
    hs._LATCHES.clear()
    def sample(frame, stamp):
        state = _state(3, 1, loss=0, counter=0)
        state.update(_hitstun_sample_ns=stamp, _hitstun_victim_action_id=0x55,
                     _hitstun_victim_action_frame=frame)
        return state
    assert hs._apply_untech_latch("P1", sample(10, 100))["hitstun_clock_remaining"] == 3
    for frame in range(11, 16):
        assert hs._apply_untech_latch("P1", sample(frame, frame + 100))["hitstun_clock_remaining"] == 3
    assert hs._apply_untech_latch("P1", sample(15, 200))["hitstun_clock_remaining"] == 3
    assert hs._apply_untech_latch("P1", sample(16, 201))["hitstun_clock_remaining"] == 0


def test_retired_clock_ignores_same_native_value_until_new_contact():
    hs._LATCHES.clear()
    def sample(frame, stamp, *, hp=900, combo=1, counter=0):
        state = _state(3, combo, hp=hp, loss=0, counter=counter)
        state.update(_hitstun_sample_ns=stamp, _hitstun_victim_action_id=0x55,
                     _hitstun_victim_action_frame=frame)
        return state
    hs._apply_untech_latch("P1", sample(10, 100))
    for frame in range(11, 17):
        expired = hs._apply_untech_latch("P1", sample(frame, frame + 100))
    assert expired["hitstun_clock_remaining"] == 0
    # Even after neutral clears the geometry, the old nonzero native word
    # cannot look like a fresh first timer.
    assert hs._apply_untech_latch("P1", sample(17, 117, combo=0))["hitstun_clock_remaining"] == 0
    assert hs._apply_untech_latch("P1", sample(18, 118, combo=0))["hitstun_clock_remaining"] == 0
    # A genuine HP/combo edge can rearm an identical 3F value.
    new_hit = hs._apply_untech_latch("P1", sample(19, 119, hp=800, combo=2))
    assert new_hit["hitstun_clock_remaining"] == 3


def test_new_shorter_hit_scraps_old_clock_immediately():
    hs._LATCHES.clear()
    hs._apply_untech_latch("P1", _state(21, 1, hp=900))
    old = hs._apply_untech_latch("P1", _state(17, 1, hp=900))
    assert old["hitstun_untech_elapsed"] == 4

    replacement = hs._apply_untech_latch(
        "P1", _state(12, 2, loss=8, counter=32, hp=820)
    )
    assert replacement["hitstun_untech_generation"] == 2
    assert replacement["hitstun_untech_expiry_target"] == 12
    assert replacement["hitstun_untech_elapsed"] == 0


def test_realtime_sampler_snapshot_contains_hs_fields_without_expanding_read_span():
    source = (ROOT / "tvcgui/runtime/input_monitor.py").read_text(encoding="utf-8")
    assert "realtime_span_end = 0x44A4" in source
    assert "untech_remaining = blob_u32(UNTECH_TIMER_OFF)" in source
    assert "fighter_combo_count = blob_u32(FIGHTER_COMBO_COUNT_OFF)" in source
    assert "decay_counter = blob_u32(HITSTUN_DECAY_COUNTER_OFF)" in source
    assert "state_flags_6c = blob_u32(STATE_FLAGS_6C_OFF)" in source


def test_renderer_counts_down_from_native_remaining_without_wall_clock_smoothing():
    source = (ROOT / "tvcgui/features/overlay/hud_renderer.py").read_text(encoding="utf-8")
    start = source.index("def _draw_compact_untech_scaling_row(")
    end = source.index("\ndef _research_dock_active_panel", start)
    compact = source[start:end]
    assert "remaining = max(0, target - elapsed) if target > 0 else 0" in compact
    assert 'hit_value = f"{remaining}/{target}"' in compact
    assert 'draw_clock_cell(hit_cell, hit_label, hit_value, hit_color, remaining, target' in compact
    assert "_hs_visual_elapsed(" not in compact
    assert "hitstun_untech_generation" in compact

def test_hs_visual_geometry_is_exactly_game_frame_locked():
    source = (ROOT / "tvcgui/features/overlay/hud_renderer.py").read_text(encoding="utf-8")
    start = source.index("def _hs_visual_elapsed(")
    end = source.index("\n\ndef _draw_compact_untech_scaling_row", start)
    body = source[start:end]
    assert "del dt" in body
    assert 'slot_anim["hs_visual_elapsed"] = exact' in body
    assert "return exact" in body
    assert "HS_VISUAL_SWEEP_FPS" not in source
    assert "_approach(" not in body


def test_no_decay_hit_uses_ordinary_hitstun_fallback_from_first_hit():
    hs._LATCHES.clear()
    first = hs._apply_untech_latch("P1", _state(0, 1, loss=0, counter=0, hitstun=18))
    assert first["hitstun_untech_generation"] == 1
    assert first["hitstun_clock_source"] == "hitstun"
    assert first["hitstun_untech_expiry_target"] == 18
    assert first["hitstun_clock_remaining"] == 18
    next_frame = hs._apply_untech_latch("P1", _state(0, 1, loss=0, counter=0, hitstun=17))
    assert next_frame["hitstun_untech_elapsed"] == 1
    assert next_frame["hitstun_clock_remaining"] == 17


def test_untech_lane_does_not_switch_to_hitstun_when_air_timer_expires():
    hs._LATCHES.clear()
    hs._apply_untech_latch("P1", _state(6, 1, loss=0, counter=0, hitstun=10))
    expired = hs._apply_untech_latch("P1", _state(0, 1, loss=0, counter=0, hitstun=5))
    assert expired["hitstun_clock_source"] == "untech"
    assert expired["hitstun_untech_elapsed"] == 6
    assert expired["hitstun_clock_remaining"] == 0


def test_manager_forwards_existing_realtime_hitstun_without_new_dolphin_read():
    source = (ROOT / "tvcgui/features/overlay/manager.py").read_text(encoding="utf-8")
    assert '"realtime_hitstun_remaining": max(0, int(input_packet.get("hitstun_remaining", 0) or 0))' in source
    monitor = (ROOT / "tvcgui/runtime/input_monitor.py").read_text(encoding="utf-8")
    assert "hitstun_remaining = blob_u32(RUNTIME_HITSTUN_REMAINING_OFF)" in monitor
    assert "realtime_span_end = 0x44A4" in monitor


def test_renderer_draws_ordinary_hitstun_even_when_decay_rule_is_off():
    source = (ROOT / "tvcgui/features/overlay/hud_renderer.py").read_text(encoding="utf-8")
    start = source.index("def _draw_compact_untech_scaling_row(")
    end = source.index("\ndef _research_dock_active_panel", start)
    compact = source[start:end]
    assert 'elif live and target > 0:' in compact
    assert 'if clock_source == "hitstun":' in compact
    assert 'hit_label = "HS"' in compact
    assert 'hit_label = f"AIR HS -{loss}" if loss > 0 else "AIR HS"' in compact


def test_renderer_red_tail_uses_loss_latched_to_current_hit_generation():
    source = (ROOT / "tvcgui/features/overlay/hud_renderer.py").read_text(encoding="utf-8")
    assert 'snap.get("hitstun_untech_latched_loss", snap.get("hitstun_decay_frames"))' in source


def test_contact_can_mint_clock_but_cannot_advance_before_native_timer_appears():
    hs._LATCHES.clear()

    idle = _state(0, 0, loss=4, counter=16, hp=1000, hitstun=0)
    idle["_hitstun_authored_raw"] = 24
    hs._apply_untech_latch("P1", idle)

    contact = _state(0, 1, loss=4, counter=16, hp=900, hitstun=0)
    contact["_hitstun_authored_raw"] = 24
    first = hs._apply_untech_latch("P1", contact)
    assert first["hitstun_untech_generation"] == 1
    assert first["hitstun_untech_expiry_target"] == 21
    assert first["hitstun_untech_elapsed"] == 0
    assert first["hitstun_clock_remaining"] == 21

    # Re-reading the same game state, no matter how much host time passes,
    # must not move a single visual frame.
    held = _state(0, 1, loss=4, counter=16, hp=900, hitstun=0)
    held["_hitstun_authored_raw"] = 24
    second = hs._apply_untech_latch("P1", held)
    assert second["hitstun_untech_elapsed"] == 0
    assert second["hitstun_clock_remaining"] == 21

    # The first native decrement is the first permitted visual decrement.
    native = _state(0, 1, loss=4, counter=16, hp=900, hitstun=20)
    native["_hitstun_authored_raw"] = 24
    third = hs._apply_untech_latch("P1", native)
    assert third["hitstun_untech_elapsed"] == 1
    assert third["hitstun_clock_remaining"] == 20


def test_late_native_timer_cannot_refill_contact_clock():
    hs._LATCHES.clear()

    idle = _state(0, 0, loss=4, counter=16, hp=1000, hitstun=0)
    idle["_hitstun_authored_raw"] = 24
    hs._apply_untech_latch("P1", idle)

    contact = _state(0, 1, loss=4, counter=16, hp=900, hitstun=0)
    contact["_hitstun_authored_raw"] = 24
    hs._apply_untech_latch("P1", contact)

    # No native change means no visual progress.
    still_waiting = _state(0, 1, loss=4, counter=16, hp=900, hitstun=0)
    still_waiting["_hitstun_authored_raw"] = 24
    before_native = hs._apply_untech_latch("P1", still_waiting)
    assert before_native["hitstun_untech_elapsed"] == 0

    # Native remaining 18 means exactly three frames elapsed from the 21F target.
    native_arrives = _state(18, 1, loss=4, counter=16, hp=900, hitstun=0)
    native_arrives["_hitstun_authored_raw"] = 24
    after_native = hs._apply_untech_latch("P1", native_arrives)
    assert after_native["hitstun_untech_generation"] == 1
    assert after_native["hitstun_untech_elapsed"] == 3
    assert after_native["hitstun_clock_remaining"] == 18

    # An out-of-order/stale larger native sample cannot refill the bar.
    stale = _state(21, 1, loss=4, counter=16, hp=900, hitstun=0)
    stale["_hitstun_authored_raw"] = 24
    protected = hs._apply_untech_latch("P1", stale)
    assert protected["hitstun_untech_elapsed"] == 3
    assert protected["hitstun_clock_remaining"] == 18


def test_manager_serializes_authored_move_hitstun_as_contact_hint():
    source = (ROOT / "tvcgui/features/overlay/manager.py").read_text(encoding="utf-8")
    assert '"move_hitstun":           matched_move.get("hitstun") if isinstance(matched_move, dict) else None' in source
