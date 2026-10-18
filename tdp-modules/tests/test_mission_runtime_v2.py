from tvcgui.features.training.mission_runtime_v2 import MissionRuntimeV2
from tvcgui.runtime.mission_events import (
    EVENT_ACTION,
    EVENT_BLOCKSTUN_BEGIN,
    EVENT_COMBO_BEGIN,
    EVENT_COMBO_CHANGE,
    EVENT_COMBO_END,
    EVENT_DAMAGE,
    EVENT_INPUT,
    EVENT_JUMP,
    MissionEvent,
)


def ev(seq, kind, slot="P1-C1", ns=None, **kwargs):
    return MissionEvent(
        sequence=seq,
        sample_sequence=seq,
        timestamp_ns=seq * 10_000_000 if ns is None else ns,
        kind=kind,
        slot=slot,
        **kwargs,
    )


def resolver(event):
    return {
        0x102: ["5C"],
        0x120: ["Shoryu M"],
        0x130: ["Tatsu A"],
        0x131: ["Tatsu B"],
    }.get(event.action_id, [])


def matcher(candidate, expected):
    c = candidate.lower().replace(" ", "")
    return any(c == str(w).lower().replace(" ", "") for w in expected)


def configure(runtime, steps, goal=None, floor=0):
    runtime.configure(
        slot="P1-C1",
        character="Ryu",
        mission_id="ryu_test",
        raw_steps=steps,
        goal=goal or {},
        event_floor=floor,
        whiff_policy=lambda labels, step: bool(isinstance(step, dict) and step.get("whiff")),
    )


def consume(runtime, events, jump_cancel_checker=None):
    return runtime.consume(
        events,
        owner_slot="P1-C1",
        label_matcher=matcher,
        action_label_resolver=resolver,
        jump_cancel_checker=jump_cancel_checker,
        newest_sequence=max([e.sequence for e in events] or [0]),
    )


def test_damage_step_advances_only_after_damage_edge():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["5C"]}])
    result = consume(runtime, [ev(1, EVENT_ACTION, action_id=0x102)])
    assert result.progress_index == 0
    result = consume(runtime, [ev(2, EVENT_DAMAGE, slot="P2-C1", damage=1000)])
    assert result.cleared
    assert result.progress_index == 1


def test_native_chain_hit_keeps_source_move_ownership_until_damage():
    runtime = MissionRuntimeV2()
    runtime.configure(
        slot="P1-C1",
        character="Karas",
        mission_id="karas_004",
        raw_steps=[{"label": "5C"}, {"label": "Tobimizuchi"}, {"label": "Ukifune"}],
        goal={},
        event_floor=0,
    )
    labels = {258: ["5C"], 319: ["Tobimizuchi"], 512: ["chain hit"], 520: ["Ukifune"]}

    def sample(events):
        return runtime.consume(
            events,
            owner_slot="P1-C1",
            label_matcher=matcher,
            action_label_resolver=lambda event: labels.get(event.action_id, []),
            action_startup_resolver=lambda action_id: 24 if action_id == 319 else 2,
            newest_sequence=max(event.sequence for event in events),
        )

    assert sample([
        ev(1, EVENT_ACTION, action_id=258, ns=1_000_000_000),
        ev(2, EVENT_DAMAGE, slot="P2-C1", damage=883, ns=1_100_000_000),
        ev(3, EVENT_ACTION, action_id=319, ns=1_130_000_000),
    ]).progress_index == 1
    # Recorded 9:08 AM trace: action 319 -> chain hit 512 -> 639 HP damage.
    assert sample([
        ev(4, EVENT_ACTION, action_id=512, previous_action_id=319, ns=1_630_000_000),
        ev(5, EVENT_ACTION, action_id=512, previous_action_id=512, ns=1_630_500_000),
        ev(6, EVENT_DAMAGE, slot="P2-C1", damage=639, ns=1_631_000_000),
    ]).progress_index == 2
    assert sample([
        ev(7, EVENT_ACTION, action_id=520, ns=1_850_000_000),
        ev(8, EVENT_DAMAGE, slot="P2-C1", damage=1180, ns=2_100_000_000),
    ]).cleared


def test_native_chain_hit_requires_link_to_pending_move():
    for successor_label, previous_action, expected_progress in (
        ("chain hit", 400, 1),
        ("chain hit", 999, 0),
        ("different move", 400, 0),
    ):
        runtime = MissionRuntimeV2()
        configure(runtime, [{"label": "special"}, {"label": "followup"}])
        labels = {400: ["special"], 512: [successor_label]}
        result = runtime.consume(
            [
                ev(1, EVENT_ACTION, action_id=400, ns=1_000_000_000),
                ev(2, EVENT_ACTION, action_id=512, previous_action_id=previous_action, ns=1_100_000_000),
                ev(3, EVENT_DAMAGE, slot="P2-C1", damage=500, ns=1_101_000_000),
            ],
            owner_slot="P1-C1",
            label_matcher=matcher,
            action_label_resolver=lambda event: labels.get(event.action_id, []),
            newest_sequence=3,
        )
        assert result.progress_index == expected_progress


def test_native_chain_hit_without_damage_expires_at_move_deadline():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"label": "special"}, {"label": "followup"}])
    runtime.consume(
        [
            ev(1, EVENT_ACTION, action_id=400, ns=1_000_000_000),
            ev(2, EVENT_ACTION, action_id=512, previous_action_id=400, ns=1_100_000_000),
        ],
        owner_slot="P1-C1",
        label_matcher=matcher,
        action_label_resolver=lambda event: {400: ["special"], 512: ["chain hit"]}.get(event.action_id, []),
        newest_sequence=2,
    )
    assert runtime.tick(now_ns=1_200_000_000, newest_sequence=2).ready_progress_index == 1
    assert runtime.tick(now_ns=2_000_000_000, newest_sequence=2).ready_progress_index == 0


def test_alex_2c_confirms_from_combo_increment_at_one_hp():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"label": "2B"}, {"label": "2C"}, {"label": "Slash Elbow M"}])
    labels = {260: ["2B"], 261: ["2C"]}
    result = runtime.consume(
        [
            ev(1, EVENT_ACTION, action_id=260, ns=1_000_000_000),
            ev(2, EVENT_DAMAGE, slot="P2-C1", damage=542,
               current_hp=1, combo_count=3, hitstun_remaining=17, ns=1_100_000_000),
            ev(3, EVENT_ACTION, action_id=261, ns=1_130_000_000),
            # The recorded 9:15 AM 2C hit raised combo 3 -> 4 at the HP floor.
            ev(4, EVENT_COMBO_CHANGE, combo_count=4, ns=1_410_000_000),
        ],
        owner_slot="P1-C1",
        label_matcher=matcher,
        action_label_resolver=lambda event: labels.get(event.action_id, []),
        newest_sequence=4,
    )
    assert result.progress_index == 2
    assert result.current_step_label == "Slash Elbow M"


def test_combo_increment_does_not_replace_normal_hp_confirmation():
    for hp, combo_after, expected_progress in ((2, 4, 1), (1, 3, 1)):
        runtime = MissionRuntimeV2()
        configure(runtime, [{"label": "2B"}, {"label": "2C"}])
        labels = {260: ["2B"], 261: ["2C"]}
        result = runtime.consume(
            [
                ev(1, EVENT_ACTION, action_id=260, ns=1_000_000_000),
                ev(2, EVENT_DAMAGE, slot="P2-C1", damage=542,
                   current_hp=hp, combo_count=3, hitstun_remaining=17, ns=1_100_000_000),
                ev(3, EVENT_ACTION, action_id=261, ns=1_130_000_000),
                ev(4, EVENT_COMBO_CHANGE, combo_count=combo_after, ns=1_410_000_000),
            ],
            owner_slot="P1-C1",
            label_matcher=matcher,
            action_label_resolver=lambda event: labels.get(event.action_id, []),
            newest_sequence=4,
        )
        assert result.progress_index == expected_progress


def test_clear_evidence_survives_recovery_in_same_event_batch():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["5C"]}])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, action_id=0x102),
        ev(2, EVENT_DAMAGE, slot="P2-C1", damage=100, hitstun_remaining=10),
        ev(3, "HITSTUN_END", slot="P2-C1", action_id=1),
    ])
    assert not result.cleared
    assert result.attempt_id == 2
    assert result.last_cleared_attempt == 1
    assert result.clear_count == 1


def test_jump_cancel_is_input_evidence_not_free_pass():
    runtime = MissionRuntimeV2()
    configure(runtime, [
        {"labels": ["5C"], "pass": True},
        {"labels": ["Jump Cancel"], "input": "7 / 8 / 9"},
        {"labels": ["Tatsu A"], "pass": True},
    ])
    result = consume(runtime, [ev(1, EVENT_ACTION, action_id=0x102)])
    assert result.progress_index == 1
    # Raw TvC direction 0x08 is down, not numpad 8. It must not arm JC.
    result = consume(runtime, [ev(2, EVENT_INPUT, held=0x08)])
    assert result.progress_index == 1
    # Raw 0x05 is up-forward (numpad 9). Input arms the JC but does not
    # complete it until TvC actually transitions into a native jump state.
    result = consume(runtime, [ev(3, EVENT_INPUT, held=0x05)])
    assert result.progress_index == 1
    result = consume(runtime, [ev(4, EVENT_JUMP, action_id=21)])
    assert result.progress_index == 2
    result = consume(runtime, [ev(5, EVENT_ACTION, action_id=0x130)])
    assert result.cleared


def test_jump_cancel_respects_known_direct_move_flag():
    runtime = MissionRuntimeV2()
    configure(runtime, [
        {"labels": ["5C"], "pass": True},
        {"labels": ["Jump Cancel"], "input": "7 / 8 / 9"},
    ])
    result = consume(runtime, [ev(1, EVENT_ACTION, action_id=0x102)])
    assert result.progress_index == 1
    result = consume(runtime, [ev(2, EVENT_INPUT, held=0x04)])
    assert result.progress_index == 1
    result = consume(
        runtime,
        [ev(3, EVENT_JUMP, action_id=20)],
        jump_cancel_checker=lambda action_id: False,
    )
    assert result.progress_index == 1


def test_rapid_buffered_actions_and_hits_all_advance_in_one_consume():
    runtime = MissionRuntimeV2()
    configure(runtime, [
        {"labels": ["5C"]},
        {"labels": ["Shoryu M"]},
        {"labels": ["Tatsu A"]},
    ])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, action_id=0x102),
        ev(2, EVENT_DAMAGE, slot="P2-C1", damage=500),
        ev(3, EVENT_ACTION, action_id=0x120),
        ev(4, EVENT_DAMAGE, slot="P2-C1", damage=700),
        ev(5, EVENT_ACTION, action_id=0x130),
        ev(6, EVENT_DAMAGE, slot="P2-C1", damage=900),
    ])
    assert result.cleared
    assert result.progress_index == 3
    assert result.buffered_advances == 3


def test_damage_edge_can_precede_action_edge_in_same_sampler_instant():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["5C"]}])
    result = consume(runtime, [
        ev(1, EVENT_DAMAGE, slot="P2-C1", ns=100_000_000, damage=500),
        ev(2, EVENT_ACTION, ns=120_000_000, action_id=0x102),
    ])
    assert result.cleared


def test_delayed_hit_survives_trigger_action_within_authored_window():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["5C"], "delayed_confirm_frames": 120}])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x102),
        ev(2, EVENT_ACTION, ns=300_000_000, action_id=0x130),
        ev(3, EVENT_DAMAGE, slot="P2-C1", ns=400_000_000, damage=500),
    ])
    assert result.cleared


def test_delayed_hit_cannot_claim_damage_after_authored_window():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["5C"], "delayed_confirm_frames": 6}])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x102),
        ev(2, EVENT_ACTION, ns=300_000_000, action_id=0x130),
        ev(3, EVENT_DAMAGE, slot="P2-C1", ns=310_000_000, damage=500),
    ])
    assert not result.cleared
    assert result.progress_index == 0


def test_delayed_ready_state_expires_without_another_game_event():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["5C"], "delayed_confirm_frames": 6}])
    armed = consume(runtime, [
        ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x102),
    ])
    assert armed.prediction_active

    expired = runtime.tick(now_ns=201_000_000, newest_sequence=1)
    assert not expired.prediction_active
    assert expired.progress_index == 0


def test_ordinary_whiff_does_not_claim_later_move_damage():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["5C"]}])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x102),
        ev(2, EVENT_ACTION, ns=150_000_000, action_id=0x130),
        ev(3, EVENT_DAMAGE, slot="P2-C1", ns=200_000_000, damage=500),
    ])
    assert not result.cleared
    assert result.progress_index == 0


def test_buffered_next_move_does_not_steal_previous_moves_hit():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["5C"]}, {"labels": ["Shoryu M"]}])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x102),
        ev(2, EVENT_ACTION, ns=150_000_000, action_id=0x120, action_frame=1),
        ev(3, EVENT_DAMAGE, slot="P2-C1", ns=160_000_000, damage=100),
    ])
    assert result.progress_index == 1
    assert result.prediction_active
    result = consume(runtime, [
        ev(4, EVENT_DAMAGE, slot="P2-C1", ns=200_000_000, damage=100),
    ])
    assert result.cleared


def test_air_normal_handoff_works_with_unknown_startup():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["5C"]}, {"labels": ["Shoryu M"]}])
    result = runtime.consume(
        [
            ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x102),
            ev(2, EVENT_ACTION, ns=150_000_000, action_id=0x120, action_frame=1),
            ev(3, EVENT_DAMAGE, slot="P2-C1", ns=158_000_000, damage=100),
            ev(4, EVENT_DAMAGE, slot="P2-C1", ns=180_000_000, damage=100),
        ],
        owner_slot="P1-C1",
        label_matcher=matcher,
        action_label_resolver=resolver,
        action_startup_resolver=lambda _action: 0,
        newest_sequence=4,
    )
    assert result.cleared


def test_ryu_second_jb_hit_cannot_confirm_jc_before_jc_is_active():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["j.B"]}, {"labels": ["j.C"]}])
    names = {0x10A: ["j.B"], 0x10C: ["j.B (Second)"], 0x10B: ["j.C"]}
    first = runtime.consume(
        [
            ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x10A),
            ev(2, EVENT_DAMAGE, slot="P2-C1", ns=220_000_000, damage=100),
            ev(3, EVENT_ACTION, ns=225_000_000, action_id=0x10C),
            ev(4, EVENT_DAMAGE, slot="P2-C1", ns=248_000_000, damage=100),
            ev(5, EVENT_ACTION, ns=250_000_000, action_id=0x10B, action_frame=1),
        ],
        owner_slot="P1-C1", label_matcher=matcher,
        action_label_resolver=lambda event: names.get(event.action_id, []),
        action_startup_resolver=lambda action_id: 11 if action_id == 0x10B else 8,
        newest_sequence=5,
    )
    assert first.progress_index == 1
    assert first.prediction_active
    final = runtime.consume(
        [ev(6, EVENT_DAMAGE, slot="P2-C1", ns=390_000_000, damage=100)],
        owner_slot="P1-C1", label_matcher=matcher,
        action_label_resolver=lambda event: names.get(event.action_id, []),
        action_startup_resolver=lambda action_id: 11 if action_id == 0x10B else 8,
        newest_sequence=6,
    )
    assert final.cleared


def test_chun_legs_followup_waits_for_jb_hit_not_late_legs_hit():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["Legs C"]}, {"labels": ["j.B"]}])
    names = {0x135: ["Legs C"], 0x10A: ["j.B"]}
    first = runtime.consume(
        [
            ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x135),
            ev(2, EVENT_DAMAGE, slot="P2-C1", ns=150_000_000, damage=100),
            ev(3, EVENT_ACTION, ns=160_000_000, action_id=0x10A, action_frame=1),
            ev(4, EVENT_DAMAGE, slot="P2-C1", ns=175_000_000, damage=100),
        ],
        owner_slot="P1-C1", label_matcher=matcher,
        action_label_resolver=lambda event: names.get(event.action_id, []),
        action_startup_resolver=lambda action_id: 8 if action_id == 0x10A else 4,
        newest_sequence=4,
    )
    assert first.progress_index == 1
    assert first.prediction_active
    final = runtime.consume(
        [ev(5, EVENT_DAMAGE, slot="P2-C1", ns=300_000_000, damage=100)],
        owner_slot="P1-C1", label_matcher=matcher,
        action_label_resolver=lambda event: names.get(event.action_id, []),
        action_startup_resolver=lambda action_id: 8 if action_id == 0x10A else 4,
        newest_sequence=5,
    )
    assert final.cleared


def test_whiffed_previous_move_cannot_claim_next_moves_later_hit():
    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["5C"]}, {"labels": ["Shoryu M"]}])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x102),
        ev(2, EVENT_ACTION, ns=150_000_000, action_id=0x120, action_frame=1),
        ev(3, EVENT_DAMAGE, slot="P2-C1", ns=200_000_000, damage=100),
    ])
    assert result.progress_index == 0
    assert not result.prediction_active


def test_four_hit_route_stays_confirmed_through_reaction_clock_gap():
    runtime = MissionRuntimeV2()
    configure(runtime, [
        {"labels": ["5C"]},
        {"labels": ["Shoryu M"]},
        {"labels": ["Tatsu A"]},
        {"labels": ["Tatsu B"]},
    ])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x102),
        ev(2, EVENT_DAMAGE, slot="P2-C1", ns=110_000_000, damage=100, hitstun_remaining=12),
        ev(3, EVENT_ACTION, ns=180_000_000, action_id=0x120),
        ev(4, EVENT_DAMAGE, slot="P2-C1", ns=190_000_000, damage=100, hitstun_remaining=9),
        ev(5, "REACTION_BEGIN", slot="P2-C1", ns=200_000_000, action_id=70),
        ev(6, "HITSTUN_END", slot="P2-C1", ns=210_000_000, action_id=70),
        ev(7, EVENT_ACTION, ns=240_000_000, action_id=0x130),
        ev(8, EVENT_DAMAGE, slot="P2-C1", ns=250_000_000, damage=100, hitstun_remaining=10),
        ev(9, EVENT_ACTION, ns=300_000_000, action_id=0x131),
        ev(10, EVENT_DAMAGE, slot="P2-C1", ns=310_000_000, damage=100, hitstun_remaining=8),
    ])
    assert result.cleared
    assert result.progress_index == 4
    assert result.attempt_id == 1


def test_combo_counter_end_alone_does_not_break_native_reaction_continuity():
    runtime = MissionRuntimeV2()
    configure(runtime, [
        {"labels": ["5C"]},
        {"labels": ["Shoryu M"]},
    ])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, action_id=0x102),
        ev(2, EVENT_DAMAGE, slot="P2-C1", damage=500),
        ev(3, EVENT_COMBO_BEGIN, slot="P2-C1", combo_count=1),
        ev(4, EVENT_COMBO_END, slot="P2-C1", combo_count=0),
    ])
    assert result.progress_index == 1
    assert result.attempt_id == 1
    assert result.event_floor == 0
    # A duplicate event cannot advance the route.
    result = consume(runtime, [ev(4, EVENT_ACTION, action_id=0x120)])
    assert result.progress_index == 1


def test_grace_is_game_event_time_not_wall_time():
    runtime = MissionRuntimeV2()
    configure(runtime, [
        {"labels": ["5C"], "grace": 6},
        {"labels": ["Shoryu M"], "pass": True},
    ])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x102),
        ev(2, EVENT_DAMAGE, slot="P2-C1", ns=110_000_000, damage=500),
        ev(3, EVENT_COMBO_BEGIN, slot="P2-C1", ns=120_000_000, combo_count=1),
        ev(4, EVENT_COMBO_END, slot="P2-C1", ns=130_000_000, combo_count=0),
        # 6f grace is ~100ms, so this expected action is valid at +70ms.
        ev(5, EVENT_ACTION, ns=180_000_000, action_id=0x120),
    ])
    assert result.cleared
    assert result.attempt_id == 1


def test_combo_damage_goal_uses_damage_events():
    runtime = MissionRuntimeV2()
    configure(runtime, [], goal={"type": "combo_damage", "damage": 1500})
    result = consume(runtime, [
        ev(1, EVENT_COMBO_BEGIN, slot="P2-C1", combo_count=1),
        ev(2, EVENT_DAMAGE, slot="P2-C1", damage=800, combo_count=1),
        ev(3, EVENT_DAMAGE, slot="P2-C1", damage=750, combo_count=2),
    ])
    assert result.cleared
    assert result.goal_damage == 1550


def test_state_duration_goal_uses_native_blockstun_count():
    runtime = MissionRuntimeV2()
    configure(runtime, [], goal={"type": "state_duration", "target_state": "blockstun", "frames": 20})
    result = consume(runtime, [
        ev(1, EVENT_BLOCKSTUN_BEGIN, slot="P2-C1", blockstun_remaining=21),
    ])
    assert result.cleared
    assert result.goal_current_frames == 21


def test_legs_c_uses_direct_mash_strength_with_generic_action_label():
    runtime = MissionRuntimeV2()
    runtime.configure(
        slot="P1-C1",
        character="Chun-Li",
        mission_id="chun_legs_c",
        raw_steps=[{"labels": ["Legs C"], "input": "C C C"}],
        goal={},
        event_floor=0,
    )

    def legs_resolver(event):
        return ["Lightning Legs"] if event.action_id == 0x220 else []

    def legs_matcher(candidate, expected):
        c = candidate.lower().replace(" ", "")
        return any(c == str(w).lower().replace(" ", "") for w in expected)

    result = runtime.consume(
        [
            ev(1, EVENT_INPUT, ns=100_000_000, pressed=0x20, held=0x20),
            ev(2, EVENT_INPUT, ns=130_000_000, pressed=0x20, held=0x20),
            ev(3, EVENT_INPUT, ns=160_000_000, pressed=0x20, held=0x20),
            ev(4, EVENT_ACTION, ns=180_000_000, action_id=0x220),
            ev(5, EVENT_DAMAGE, slot="P2-C1", ns=190_000_000, damage=450),
        ],
        owner_slot="P1-C1",
        label_matcher=legs_matcher,
        action_label_resolver=legs_resolver,
        newest_sequence=5,
    )
    assert result.cleared


def test_legs_c_rejects_wrong_mash_strength():
    runtime = MissionRuntimeV2()
    runtime.configure(
        slot="P1-C1",
        character="Chun-Li",
        mission_id="chun_legs_c_wrong",
        raw_steps=[{"labels": ["Legs C"], "input": "C C C"}],
        goal={},
        event_floor=0,
    )

    result = runtime.consume(
        [
            ev(1, EVENT_INPUT, ns=100_000_000, pressed=0x40, held=0x40),
            ev(2, EVENT_INPUT, ns=130_000_000, pressed=0x40, held=0x40),
            ev(3, EVENT_INPUT, ns=160_000_000, pressed=0x40, held=0x40),
            ev(4, EVENT_ACTION, ns=180_000_000, action_id=0x220),
            ev(5, EVENT_DAMAGE, slot="P2-C1", ns=190_000_000, damage=450),
        ],
        owner_slot="P1-C1",
        label_matcher=matcher,
        action_label_resolver=lambda event: ["Lightning Legs"],
        newest_sequence=5,
    )
    assert not result.cleared
    assert result.progress_index == 0


def test_native_hitstun_end_resets_even_if_combo_counter_still_lags():
    runtime = MissionRuntimeV2()
    configure(runtime, [
        {"labels": ["5C"]},
        {"labels": ["Shoryu M"]},
    ])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, action_id=0x102),
        ev(2, EVENT_DAMAGE, slot="P2-C1", damage=500),
        ev(3, EVENT_COMBO_BEGIN, slot="P2-C1", combo_count=1),
        ev(4, "HITSTUN_BEGIN", slot="P2-C1", hitstun_remaining=10, combo_count=1),
        # Combo count is deliberately still nonzero here. Native stun zero wins.
        ev(5, "HITSTUN_END", slot="P2-C1", hitstun_remaining=0, combo_count=1),
    ])
    assert result.progress_index == 0
    assert result.attempt_id == 2
    assert result.last_restart_reason == "hitstun_end"


def test_special_reaction_keeps_route_alive_after_hitstun_bar_ends():
    from tvcgui.runtime.mission_events import (
        EVENT_HITSTUN_BEGIN,
        EVENT_HITSTUN_END,
        EVENT_REACTION_BEGIN,
        EVENT_REACTION_END,
    )

    runtime = MissionRuntimeV2()
    configure(runtime, [
        {"labels": ["5C"]},
        {"labels": ["Shoryu M"]},
    ])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, action_id=0x102),
        ev(2, EVENT_DAMAGE, slot="P2-C1", damage=500),
        ev(3, EVENT_COMBO_BEGIN, slot="P2-C1", combo_count=1),
        ev(4, EVENT_HITSTUN_BEGIN, slot="P2-C1", hitstun_remaining=8, combo_count=1),
        # Wall bounce becomes active before regular hitstun reaches zero.
        ev(5, EVENT_REACTION_BEGIN, slot="P2-C1", action_id=70, combo_count=1),
        ev(6, EVENT_HITSTUN_END, slot="P2-C1", hitstun_remaining=0, combo_count=1),
    ])
    assert result.progress_index == 1
    assert result.attempt_id == 1
    assert result.last_restart_reason == ""

    result = consume(runtime, [
        ev(7, EVENT_REACTION_END, slot="P2-C1", action_id=103, combo_count=1),
    ])
    assert result.progress_index == 0
    assert result.attempt_id == 2
    assert result.last_restart_reason == "reaction_end"


def test_recovery_idle_without_a_confirmed_hit_cannot_reset_route():
    from tvcgui.runtime.mission_events import EVENT_RECOVERY_IDLE

    runtime = MissionRuntimeV2()
    configure(runtime, [
        {"labels": ["5C"], "pass": True},
        {"labels": ["Shoryu M"], "pass": True},
    ])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, action_id=0x102),
        # Even if a native reaction edge/timer was missed entirely, the victim
        # becoming universal idle proves the route is over.
        ev(2, EVENT_RECOVERY_IDLE, slot="P2-C1", action_id=1),
    ])
    assert result.progress_index == 1
    assert result.attempt_id == 1
    assert result.last_restart_reason == ""


def test_recovery_idle_resets_confirmed_hit_even_when_stun_sample_was_missed():
    from tvcgui.runtime.mission_events import EVENT_RECOVERY_IDLE

    runtime = MissionRuntimeV2()
    configure(runtime, [{"labels": ["5C"]}, {"labels": ["Shoryu M"]}])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, action_id=0x102),
        ev(2, EVENT_DAMAGE, slot="P2-C1", damage=500),
    ])
    assert result.progress_index == 1
    assert runtime._state.reaction_seen is False

    # A delayed, older idle packet must not terminate the new hit.
    result = consume(runtime, [
        ev(3, EVENT_RECOVERY_IDLE, slot="P2-C1", ns=15_000_000, action_id=1),
    ])
    assert result.progress_index == 1

    result = consume(runtime, [
        ev(4, EVENT_RECOVERY_IDLE, slot="P2-C1", action_id=1),
    ])
    assert result.progress_index == 0
    assert result.attempt_id == 2
    assert result.last_restart_reason == "recovery_idle"


def test_confirmed_hit_grace_starts_at_recovery_and_expires_on_event_time():
    from tvcgui.runtime.mission_events import EVENT_RECOVERY_IDLE

    runtime = MissionRuntimeV2()
    configure(runtime, [
        {"labels": ["5C"], "grace": 6, "grace_keeps_alive_only": True},
        {"labels": ["Shoryu M"]},
    ])
    assert runtime._steps[0].grace_keeps_alive_only is True
    result = consume(runtime, [
        ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x102),
        ev(2, EVENT_DAMAGE, slot="P2-C1", ns=110_000_000, damage=500),
        ev(3, EVENT_RECOVERY_IDLE, slot="P2-C1", ns=120_000_000, action_id=1),
    ])
    assert result.progress_index == 1
    assert result.attempt_id == 1

    result = consume(runtime, [ev(4, EVENT_ACTION, ns=180_000_000, action_id=0x120)])
    assert result.progress_index == 1  # Grace does not turn an input into a hit.
    result = consume(runtime, [ev(5, EVENT_INPUT, ns=230_000_000, held=0)])
    assert result.progress_index == 0
    assert result.attempt_id == 2


def test_recovery_idle_respects_step_grace_before_resetting():
    from tvcgui.runtime.mission_events import EVENT_RECOVERY_IDLE

    runtime = MissionRuntimeV2()
    configure(runtime, [
        {"labels": ["5C"], "pass": True, "grace": 6},
        {"labels": ["Shoryu M"], "pass": True},
    ])
    result = consume(runtime, [
        ev(1, EVENT_ACTION, ns=100_000_000, action_id=0x102),
        ev(2, EVENT_RECOVERY_IDLE, slot="P2-C1", ns=120_000_000, action_id=1),
    ])
    assert result.progress_index == 1
    assert result.attempt_id == 1
    # A later required move inside grace cancels the pending restart by
    # advancing the route instead of throwing away the attempt.
    result = consume(runtime, [
        ev(3, EVENT_ACTION, ns=150_000_000, action_id=0x120),
    ])
    assert result.cleared
    assert result.attempt_id == 1
