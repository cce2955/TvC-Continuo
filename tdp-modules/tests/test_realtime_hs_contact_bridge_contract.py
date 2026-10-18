from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "tvcgui" / "features" / "overlay" / "manager.py"
HUD = ROOT / "tvcgui" / "features" / "overlay" / "hud_renderer.py"


def _extract_function(path: Path, name: str):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.Module(body=[fn], type_ignores=[])
    ns = {}
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), ns)
    return ns[name]


def test_native_contact_prefers_final_untech_clock_when_present():
    choose = _extract_function(MANAGER, "_native_hs_contact_values")
    result = choose({"hitstun_remaining": 21, "untech_remaining": 17}, 4)
    assert result == {
        "clock_source": "untech",
        "target": 17,
        "native_hitstun": 21,
        "native_untech": 17,
        "decay_frames": 4,
        "raw_estimate": 21,
    }


def test_native_contact_uses_resolved_hitstun_when_no_untech_clock_exists():
    choose = _extract_function(MANAGER, "_native_hs_contact_values")
    result = choose({"hitstun_remaining": 17, "untech_remaining": 0}, 99)
    assert result["clock_source"] == "hitstun"
    assert result["target"] == 17
    assert result["decay_frames"] == 0
    assert result["raw_estimate"] == 17


def test_contact_without_native_clock_is_not_invented_from_profile_data():
    choose = _extract_function(MANAGER, "_native_hs_contact_values")
    assert choose({"hitstun_remaining": 0, "untech_remaining": 0}, 4) is None


def test_victim_hp_edge_mints_event_from_same_sample_native_values():
    source = MANAGER.read_text(encoding="utf-8")
    start = source.index("def _emit_realtime_hs_contact(")
    end = source.index("\n    def _on_realtime_input_sample", start)
    body = source[start:end]
    assert 'current_hp = int(sample.get("current_hp", 0) or 0)' in body
    assert "if previous_hp <= 0 or current_hp >= previous_hp:" in body
    assert 'native = _native_hs_contact_values(sample, decay_counter // 4)' in body
    assert 'armed = self._realtime_hs_arm_for_team(attacker_team, now_ns) or {}' in body
    assert '"native_hitstun": int(native["native_hitstun"])' in body
    assert '"native_untech": int(native["native_untech"])' in body
    assert '"clock_source": str(native["clock_source"])' in body
    assert '_hs_contact_target' not in body
    assert 'armed.get("hitstun"' not in body


def test_attacker_move_cache_is_metadata_only_and_does_not_require_hitstun():
    source = MANAGER.read_text(encoding="utf-8")
    start = source.index("def _arm_realtime_hs_move(")
    end = source.index("\n    def _realtime_hs_arm_for_team", start)
    body = source[start:end]
    assert 'if action_id <= 0:' in body
    assert 'if hitstun <= 0 or action_id <= 0:' not in body
    emit_start = source.index("def _emit_realtime_hs_contact(")
    emit_end = source.index("\n    def _on_realtime_input_sample", emit_start)
    emit = source[emit_start:emit_end]
    assert 'if not armed:' not in emit


def test_realtime_sidecar_carries_both_native_counters_without_new_read():
    source = MANAGER.read_text(encoding="utf-8")
    assert 'int(sample.get("hitstun_remaining", 0) or 0)' in source
    assert '"hitstun_remaining": max(0, int(sample.get("hitstun_remaining", 0) or 0))' in source
    assert '"untech_remaining": max(0, int(sample.get("untech_remaining", 0) or 0))' in source
    monitor = (ROOT / "tvcgui/runtime/input_monitor.py").read_text(encoding="utf-8")
    assert "realtime_span_end = 0x44A4" in monitor
    assert "hitstun_remaining = blob_u32(RUNTIME_HITSTUN_REMAINING_OFF)" in monitor
    assert "untech_remaining = blob_u32(UNTECH_TIMER_OFF)" in monitor


def test_renderer_enriches_contact_with_victim_current_native_counter():
    source = HUD.read_text(encoding="utf-8")
    start = source.index("def _merge_realtime_inputs(")
    end = source.index("\n# ---------------------------------------------------------------------------\n# Drawing helpers", start)
    body = source[start:end]
    assert 'victim_slot = str(enriched_hs.get("victim_slot") or "")' in body
    assert 'enriched_hs["native_hitstun_current"]' in body
    assert 'enriched_hs["native_untech_current"]' in body
    assert 'snap["realtime_hitstun_remaining"]' in body
    assert 'snap["realtime_untech_remaining"]' in body


def test_renderer_clock_follows_native_remaining_not_wall_clock():
    source = HUD.read_text(encoding="utf-8")
    start = source.index("def _realtime_hs_contact_clock(")
    end = source.index("\ndef _hs_visual_elapsed", start)
    body = source[start:end]
    assert 'event.get("native_untech_current"' in body
    assert 'event.get("native_hitstun_current"' in body
    assert 'remaining = min(previous_remaining, current)' in body
    assert 'elapsed = max(0, target - remaining)' in body
    assert '(now_ns - start_ns) // 16_666_667' not in body
    assert 'time.monotonic_ns()' not in body


def test_compact_row_names_the_native_clock_and_only_untech_can_show_decay():
    source = HUD.read_text(encoding="utf-8")
    start = source.index("def _draw_compact_untech_scaling_row(")
    end = source.index("\ndef _research_dock_active_panel", start)
    body = source[start:end]
    assert 'clock_source = str(contact_clock.get("clock_source") or "hitstun")' in body
    assert 'hit_label = "HS"' in body
    assert 'hit_label = f"AIR HS -{loss}" if loss > 0 else "AIR HS"' in body
    assert 'lost=(loss if clock_source == "untech" else 0)' in body


def test_native_renderer_clock_pauses_when_game_counter_holds_and_resets_on_new_hit():
    clock = _extract_function(HUD, "_realtime_hs_contact_clock")
    anim = {}
    base_event = {
        "generation": 1,
        "target": 21,
        "clock_source": "hitstun",
        "native_hitstun": 21,
        "native_untech": 0,
        "native_hitstun_current": 21,
        "raw_estimate": 21,
        "decay_frames": 0,
    }
    first = clock(anim, {"realtime_hs_contact": dict(base_event)})
    assert first["remaining"] == 21
    assert first["elapsed"] == 0

    held = clock(anim, {"realtime_hs_contact": dict(base_event)})
    assert held["remaining"] == 21
    assert held["elapsed"] == 0

    one_tick = dict(base_event, native_hitstun_current=20)
    moved = clock(anim, {"realtime_hs_contact": one_tick})
    assert moved["remaining"] == 20
    assert moved["elapsed"] == 1

    stale_refill = dict(base_event, native_hitstun_current=21)
    protected = clock(anim, {"realtime_hs_contact": stale_refill})
    assert protected["remaining"] == 20

    replacement = {
        "generation": 2,
        "target": 12,
        "clock_source": "hitstun",
        "native_hitstun": 12,
        "native_untech": 0,
        "native_hitstun_current": 12,
        "raw_estimate": 12,
        "decay_frames": 0,
    }
    reset = clock(anim, {"realtime_hs_contact": replacement})
    assert reset["remaining"] == 12
    assert reset["elapsed"] == 0
    assert reset["generation"] != first["generation"]


def test_stuck_native_hitstun_expires_after_twice_its_span_of_fighter_frames():
    clock = _extract_function(HUD, "_realtime_hs_contact_clock")
    anim = {}
    event = {
        "generation": 1, "target": 3, "clock_source": "hitstun",
        "native_hitstun_current": 3, "native_action_id_current": 0x55,
        "native_action_frame_current": 10, "native_current_sample_ns": 100,
    }
    assert clock(anim, {"realtime_hs_contact": event})["remaining"] == 3
    for frame in range(11, 16):
        event = dict(event, native_action_frame_current=frame, native_current_sample_ns=100 + frame)
        assert clock(anim, {"realtime_hs_contact": event})["remaining"] == 3
    # Repeated HUD draws and a paused action frame cannot consume the budget.
    assert clock(anim, {"realtime_hs_contact": event})["remaining"] == 3
    paused = dict(event, native_current_sample_ns=200)
    assert clock(anim, {"realtime_hs_contact": paused})["remaining"] == 3
    expired = dict(event, native_action_frame_current=16, native_current_sample_ns=201)
    assert clock(anim, {"realtime_hs_contact": expired})["remaining"] == 0
    stale_same_hit = dict(expired, native_action_frame_current=17, native_current_sample_ns=203)
    assert clock(anim, {"realtime_hs_contact": stale_same_hit})["remaining"] == 0
    new_hit = dict(expired, generation=2, native_action_frame_current=1, native_current_sample_ns=202)
    assert clock(anim, {"realtime_hs_contact": new_hit})["remaining"] == 3
