from __future__ import annotations

import importlib
import json
import sys
import threading
import time
import types


if "dolphin_memory_engine" not in sys.modules:
    dme = types.ModuleType("dolphin_memory_engine")
    dme.is_hooked = lambda: False
    dme.hook = lambda: None
    dme.un_hook = lambda: None
    dme.read_byte = lambda *_args, **_kwargs: 0
    dme.read_bytes = lambda *_args, **_kwargs: b""
    dme.write_byte = lambda *_args, **_kwargs: None
    dme.write_bytes = lambda *_args, **_kwargs: None
    sys.modules["dolphin_memory_engine"] = dme

from tvcgui.runtime.mission_events import (
    EVENT_ACTION,
    EVENT_COMBO_END,
    EVENT_DAMAGE,
    EVENT_HITSTUN_BEGIN,
    EVENT_HITSTUN_END,
    EVENT_RECOVERY_IDLE,
    MissionEvent,
    MissionEventStream,
)


module = importlib.import_module("tvcgui.features.training.mission_manager")
MissionManager = module.MissionManager


def test_overlay_queue_keeps_newer_combat_progress_over_late_gui_frame() -> None:
    manager = object.__new__(MissionManager)
    manager._overlay_write_condition = threading.Condition()
    manager._pending_overlay_payload = None

    manager._queue_overlay_payload({
        "active_mission_id": "route_a",
        "mission_event_cursor": 42,
        "completed_step_count": 2,
        "__publish_seq": 10,
    })
    manager._queue_overlay_payload({
        "active_mission_id": "route_a",
        "mission_event_cursor": 41,
        "completed_step_count": 1,
        "__publish_seq": 11,
    })

    assert manager._pending_overlay_payload["mission_event_cursor"] == 42
    assert manager._pending_overlay_payload["completed_step_count"] == 2

    manager._queue_overlay_payload({
        "active_mission_id": "route_a",
        "mission_event_cursor": 42,
        "completed_step_count": 13,
        "__mission_result_revision": 8,
        "__publish_seq": 12,
    })
    manager._queue_overlay_payload({
        "active_mission_id": "route_a",
        "mission_event_cursor": 42,
        "completed_step_count": 0,
        "__mission_result_revision": 7,
        "__publish_seq": 13,
    })
    assert manager._pending_overlay_payload["completed_step_count"] == 13


def test_overlay_file_rejects_zero_cursor_and_older_revision(tmp_path, monkeypatch) -> None:
    manager = object.__new__(MissionManager)
    manager._overlay_file_lock = threading.RLock()
    manager._last_overlay_file_seq = 0
    manager._last_overlay_mission_id = ""
    manager._last_overlay_mission_cursor = 0
    manager._last_overlay_mission_revision = 0
    manager._last_overlay_serialized = ""
    overlay_file = tmp_path / "mission.json"
    monkeypatch.setattr(module, "MISSION_OVERLAY_FILE", str(overlay_file))

    manager._write_overlay_payload_file({
        "active": True,
        "active_mission_id": "route_a",
        "mission_event_cursor": 42,
        "completed_step_count": 13,
        "__mission_result_revision": 8,
        "__publish_seq": 10,
    })
    manager._write_overlay_payload_file({
        "active": True,
        "active_mission_id": "route_a",
        "mission_event_cursor": 0,
        "completed_step_count": 0,
        "__mission_result_revision": 7,
        "__publish_seq": 11,
    })

    saved = json.loads(overlay_file.read_text(encoding="utf-8"))
    assert saved["completed_step_count"] == 13
    assert "__mission_result_revision" not in saved


def test_ryu_air_chain_final_jc_phase_can_confirm_mission_step() -> None:
    from tvcgui.features.training.mission_runtime_v2 import MissionRuntimeV2

    manager = MissionManager({}, {}, {}, lambda: [], lambda *_args: "")
    runtime = MissionRuntimeV2()
    runtime.configure(
        slot="P1-C1",
        character="Ryu",
        mission_id="ryu_air_chain",
        raw_steps=[{"label": "j.B"}, {"label": "j.C"}],
        goal={},
        event_floor=0,
    )
    try:
        assert "j.C" in manager._action_label_candidates(0x10D, 12)
        result = runtime.consume(
            [
                MissionEvent(1, 1, 100_000_000, EVENT_ACTION, "P1-C1", char_id=12, action_id=0x10A),
                MissionEvent(2, 2, 150_000_000, EVENT_ACTION, "P1-C1", char_id=12, action_id=0x10B, action_frame=1),
                MissionEvent(3, 3, 160_000_000, EVENT_DAMAGE, "P2-C1", damage=100),
                MissionEvent(4, 4, 180_000_000, EVENT_ACTION, "P1-C1", char_id=12, action_id=0x10D),
                MissionEvent(5, 5, 300_000_000, EVENT_DAMAGE, "P2-C1", damage=100),
            ],
            owner_slot="P1-C1",
            label_matcher=manager._mission_label_matches,
            action_label_resolver=lambda event: manager._action_label_candidates(event.action_id, 12),
            newest_sequence=5,
        )
        assert result.cleared
        assert result.progress_index == 2
    finally:
        manager.close()


def _packet(
    *,
    seq: int,
    ns: int,
    base: int,
    char_id: int,
    action: int,
    frame: int,
    hp: int,
    combo: int,
    held: int = 0,
    pressed: int = 0,
    hitstun: int = 0,
) -> dict:
    return {
        "seq": seq,
        "sample_ns": ns,
        "base": base,
        "char_id": char_id,
        "action_id": action,
        "action_frame": frame,
        "current_hp": hp,
        "combo_count": combo,
        "hitstun_remaining": hitstun,
        "held": held,
        "pressed": pressed,
        "released": 0,
    }


def test_events_are_immutable_and_keep_total_order() -> None:
    stream = MissionEventStream(capacity=256)
    stream.on_sample("P1-C1", _packet(
        seq=1, ns=1, base=0x90000000, char_id=12,
        action=0, frame=0, hp=10000, combo=0,
    ))
    stream.on_sample("P1-C1", _packet(
        seq=2, ns=2, base=0x90000000, char_id=12,
        action=0x100, frame=1, hp=10000, combo=0,
        held=0x80, pressed=0x80,
    ))

    newest, events = stream.events_since(0)

    assert newest == len(events)
    assert [event.sequence for event in events] == list(range(1, newest + 1))
    assert any(event.kind == EVENT_ACTION for event in events)
    action = next(event for event in events if event.kind == EVENT_ACTION)
    assert isinstance(action, MissionEvent)
    try:
        action.action_id = 0
    except Exception:
        pass
    else:
        raise AssertionError("MissionEvent must be immutable")



def test_repeated_native_action_frame_reset_is_a_new_event() -> None:
    stream = MissionEventStream(capacity=256)
    base = 0x90000000
    stream.on_sample("P1-C1", _packet(
        seq=1, ns=1, base=base, char_id=12,
        action=0, frame=0, hp=10000, combo=0,
    ))
    stream.on_sample("P1-C1", _packet(
        seq=2, ns=2, base=base, char_id=12,
        action=0x100, frame=1, hp=10000, combo=0,
        held=0x80, pressed=0x80,
    ))
    stream.on_sample("P1-C1", _packet(
        seq=3, ns=3, base=base, char_id=12,
        action=0x100, frame=8, hp=10000, combo=0, held=0x80,
    ))
    stream.on_sample("P1-C1", _packet(
        seq=4, ns=4, base=base, char_id=12,
        action=0x100, frame=1, hp=10000, combo=0,
        held=0x80, pressed=0x80,
    ))

    actions = [event for event in stream.snapshot() if event.kind == EVENT_ACTION]
    assert [event.action_id for event in actions] == [0x100, 0x100]


def test_finishing_hit_emits_damage_edge() -> None:
    stream = MissionEventStream(capacity=256)
    base = 0x91000000
    stream.on_sample("P2-C1", _packet(
        seq=1, ns=1, base=base, char_id=13,
        action=0, frame=0, hp=500, combo=0,
    ))
    stream.on_sample("P2-C1", _packet(
        seq=2, ns=2, base=base, char_id=13,
        action=48, frame=1, hp=0, combo=1, hitstun=8,
    ))

    damage = [event for event in stream.snapshot() if event.kind == EVENT_DAMAGE]
    assert len(damage) == 1
    assert damage[0].damage == 500
    assert damage[0].current_hp == 0


def test_damage_while_idle_emits_delayed_recovery_when_idle_persists() -> None:
    stream = MissionEventStream(capacity=256)
    base = 0x91000000
    for seq, ns, hp in (
        (1, 10_000_000, 10000),
        (2, 20_000_000, 9500),
        (3, 40_000_000, 9500),
        (4, 60_000_000, 9500),
        (5, 90_000_000, 9500),
    ):
        stream.on_sample("P2-C1", _packet(
            seq=seq, ns=ns, base=base, char_id=13,
            action=1, frame=seq, hp=hp, combo=0,
        ))
    events = stream.snapshot()
    assert sum(event.kind == EVENT_DAMAGE for event in events) == 1
    assert sum(event.kind == EVENT_RECOVERY_IDLE for event in events) == 1
    recovery = next(event for event in events if event.kind == EVENT_RECOVERY_IDLE)
    assert recovery.timestamp_ns > next(event for event in events if event.kind == EVENT_DAMAGE).timestamp_ns


def test_overlay_consumes_cache_without_reading_dolphin() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    overlay_source = (
        root / "tvcgui" / "features" / "overlay" / "manager.py"
    ).read_text(encoding="utf-8")
    sampler_source = (
        root / "tvcgui" / "runtime" / "realtime_sampler.py"
    ).read_text(encoding="utf-8")

    assert "read_overlay_input_packet" not in overlay_source
    assert "def _input_sampler_loop" not in overlay_source
    assert "snapshot_for_slot" in overlay_source
    assert "class RealtimeCombatSampler" in sampler_source
    assert "def _run" in sampler_source


def test_realtime_sampler_cache_and_event_consumers_are_independent() -> None:
    from tvcgui.runtime.realtime_sampler import RealtimeCombatSampler

    reads = []
    sampler = RealtimeCombatSampler(
        autostart=False,
        read_packet_fn=lambda *_args, **_kwargs: reads.append(1),
    )
    stream = MissionEventStream(capacity=256)
    sampler.add_listener(stream.on_sample)

    base = 0x90000000
    sampler.publish_packet("P1-C1", _packet(
        seq=1, ns=10, base=base, char_id=12,
        action=0, frame=0, hp=10000, combo=0,
    ))
    sampler.publish_packet("P1-C1", _packet(
        seq=2, ns=20, base=base, char_id=12,
        action=0x100, frame=1, hp=10000, combo=0,
        held=0x80, pressed=0x80,
    ))

    latest, cached = sampler.snapshot_for_slot("P1-C1", base)
    assert reads == []
    assert latest["action_id"] == 0x100
    assert cached

    cursor_a, events_a = stream.events_since(0, "P1-C1")
    cursor_b, events_b = stream.events_since(0, "P1-C1")
    assert cursor_a == cursor_b
    assert events_a == events_b
    assert any(event.kind == EVENT_ACTION for event in events_a)
    sampler.close()

def test_realtime_worker_drains_buffered_combo_without_new_samples(monkeypatch) -> None:
    stream = MissionEventStream(capacity=512)
    p1 = 0x90000000
    p2 = 0x91000000
    frame_ns = 1_000_000_000 // 60
    t = 1_000_000_000

    # Baselines.
    stream.on_sample("P1-C1", _packet(
        seq=1, ns=t, base=p1, char_id=12,
        action=0, frame=0, hp=10000, combo=0,
    ))
    stream.on_sample("P2-C1", _packet(
        seq=2, ns=t, base=p2, char_id=13,
        action=0, frame=0, hp=10000, combo=0,
    ))

    # Three actions and three confirmed hits happen before MissionManager runs.
    action_times = [t + 2 * frame_ns, t + 20 * frame_ns, t + 38 * frame_ns]
    hit_times = [t + 14 * frame_ns, t + 32 * frame_ns, t + 50 * frame_ns]
    actions = [(0x100, 0x80), (0x101, 0x40), (0x102, 0x20)]
    hp = 10000
    sample_seq = 3
    for hit_number, ((action_id, button), action_ns, hit_ns) in enumerate(
        zip(actions, action_times, hit_times), start=1
    ):
        stream.on_sample("P1-C1", _packet(
            seq=sample_seq, ns=action_ns, base=p1, char_id=12,
            action=action_id, frame=1, hp=10000, combo=hit_number - 1,
            held=button, pressed=button,
        ))
        sample_seq += 1
        hp -= 100
        stream.on_sample("P2-C1", _packet(
            seq=sample_seq, ns=hit_ns, base=p2, char_id=13,
            action=48, frame=1, hp=hp, combo=hit_number,
            hitstun=12,
        ))
        sample_seq += 1

    # The combo and hitstun end before the next GUI frame.
    stream.on_sample("P2-C1", _packet(
        seq=sample_seq, ns=t + 60 * frame_ns, base=p2, char_id=13,
        action=0, frame=0, hp=hp, combo=0, hitstun=0,
    ))

    kinds = [event.kind for event in stream.snapshot()]
    assert kinds.count(EVENT_DAMAGE) == 3
    assert EVENT_HITSTUN_BEGIN in kinds
    assert EVENT_HITSTUN_END in kinds
    assert EVENT_COMBO_END in kinds

    manager = MissionManager({}, {}, {}, lambda: [], lambda *_args: "")
    manager.set_event_provider(stream.events_since)
    manager.active_slot = "P1-C1"
    # The mission was already active when the GUI became busy. Its parser
    # cursor therefore starts before the buffered combo, not at the stream head.
    manager._runtime = module._new_mission_runtime(
        slot="P1-C1", mission_id="event_combo"
    )
    manager._runtime["mission_event_floor_sequence"] = 0
    manager._frame_idx = 120
    manager._now = 2.0
    snaps = {
        "P1-C1": {
            "base": p1, "teamtag": "P1", "name": "Ryu",
            "char_id": 12, "csv_char_id": 12, "cur": 10000,
            "mv_label": "Idle", "mv_id_display": 0,
        },
        "P2-C1": {
            "base": p2, "teamtag": "P2", "name": "Chun-Li",
            "char_id": 13, "csv_char_id": 13, "cur": hp,
            "mv_label": "Idle", "mv_id_display": 0,
        },
    }
    manager._render_snap_by_slot = snaps
    manager._capture_mission_owner("P1-C1", snaps)
    manager._drain_event_state()

    monkeypatch.setattr(module, "load_progress", lambda: {})
    monkeypatch.setattr(module, "save_progress", lambda _progress: None)
    monkeypatch.setattr(module, "mark_mission_complete", lambda progress, *_args: progress)
    monkeypatch.setattr(module, "build_overlay_payload", lambda _name: {
        "active": True,
        "slot": "P1-C1",
        "point_slot": "P1-C1",
        "character": "Ryu",
        "active_mission_id": "event_combo",
        "active_mission_steps": [
            {"label": "5A", "input": "5A"},
            {"label": "5B", "input": "5B"},
            {"label": "5C", "input": "5C"},
        ],
        "active_mission_goal": {},
    })

    payload = {
        "active": True,
        "slot": "P1-C1",
        "point_slot": "P1-C1",
        "character": "Ryu",
        "active_mission_id": "event_combo",
        "active_mission_steps": [
            {"label": "5A", "input": "5A"},
            {"label": "5B", "input": "5B"},
            {"label": "5C", "input": "5C"},
        ],
        "active_mission_goal": {},
    }

    deadline = time.monotonic() + 2.0
    result = manager._augment_payload_with_runtime(payload, snaps)
    while not result.get("just_cleared") and time.monotonic() < deadline:
        time.sleep(0.01)
        result = manager._augment_payload_with_runtime(payload, snaps)

    assert result["just_cleared"] is True, (
        result.get("completed_step_count"), result.get("current_step_label"),
        manager._mission_realtime_result,
    )
    assert result["completed_step_count"] == 3
    assert manager._runtime["progress_index"] == 0
    manager.close()


def test_main_wires_sampler_to_event_stream() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    main_source = (root / "main.py").read_text(encoding="utf-8")
    manager_source = (
        root / "tvcgui" / "features" / "training" / "mission_manager.py"
    ).read_text(encoding="utf-8")

    assert "realtime_sampler = RealtimeCombatSampler()" in main_source
    assert "mission_event_stream = MissionEventStream()" in main_source
    assert "realtime_sampler.add_listener(mission_event_stream.on_sample)" in main_source
    assert "mission_mgr.set_event_provider(mission_event_stream.events_since)" in main_source
    assert "mission_mgr.set_input_sample_provider(realtime_sampler.snapshot_for_slot)" in main_source
    assert "self._drain_event_state()" in manager_source
    assert "self._record_mission_input_stream(" in manager_source
    assert "self._drain_buffered_action_steps(" in manager_source
    assert "rd8(MISSION_GLOBAL_COMBO_COUNTER_ADDR)" not in manager_source


def test_selector_prefers_raw_sampler_edges_when_event_stream_is_attached(monkeypatch) -> None:
    """The mission parser and selector intentionally use different consumers."""
    monkeypatch.setattr(module, "build_overlay_payload", lambda _name: {
        "missions": [{"mission_id": "ryu_001"}],
        "active_mission_id": "ryu_001",
    })
    manager = MissionManager({}, {}, {}, lambda: [], lambda *_args: "")
    manager.active_slot = "P1-C1"
    snap = {
        "base": 0x90000000,
        "name": "Ryu",
        "teamtag": "P1",
    }
    manager._render_snap_by_slot = {"P1-C1": snap}
    samples = [
        {"seq": 1, "held": 0x08, "pressed": 0x08, "released": 0},
        {"seq": 2, "held": 0x00, "pressed": 0x00, "released": 0x08},
        {"seq": 3, "held": 0x08, "pressed": 0x08, "released": 0},
        {"seq": 4, "held": 0x00, "pressed": 0x00, "released": 0x08},
        {"seq": 5, "held": 0x0C00, "pressed": 0x0C00, "released": 0},
    ]

    # The generic event stream is attached for mission matching, but the menu
    # gesture must still use the sampler's dedicated input-edge queue.
    manager.set_event_provider(lambda _cursor, _slot=None: (99, ()))
    manager.set_input_sample_provider(lambda *_args: ({}, samples))

    manager._update_selector_from_inputs({"P1-C1": snap}, 10.0)

    assert manager.selector_open is True
    manager.close()
