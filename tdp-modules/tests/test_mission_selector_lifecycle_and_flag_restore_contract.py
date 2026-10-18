from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mission_packs_have_no_var_challenges() -> None:
    for path in (ROOT / "missions").glob("*.json"):
        pack = json.loads(path.read_text(encoding="utf-8"))
        for mission in pack.get("missions", []):
            assert "VAR" not in str(mission.get("name", "")).upper(), (path.name, mission.get("mission_id"))
            assert all(
                "VAR" not in str(step.get("label", "")).upper()
                for step in mission.get("steps", [])
            ), (path.name, mission.get("mission_id"))


def test_karas_kasha_extensions_use_2a_after_kasha() -> None:
    pack = json.loads((ROOT / "missions" / "karas.json").read_text(encoding="utf-8"))
    for mission in pack["missions"]:
        steps = mission.get("steps", [])
        for index, step in enumerate(steps[:-1]):
            if "kasha" in str(step.get("label", "")).lower():
                assert steps[index + 1].get("label") != "5A", mission["mission_id"]


def _module():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
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
    return importlib.import_module("tvcgui.features.training.mission_manager")


def _manager():
    module = _module()
    return module.MissionManager({}, {}, {}, lambda: [], lambda *_args: "")


def _gesture(start_seq: int) -> list[dict]:
    return [
        {"seq": start_seq, "held": 0x08, "pressed": 0x08, "released": 0},
        {"seq": start_seq + 1, "held": 0, "pressed": 0, "released": 0x08},
        {"seq": start_seq + 2, "held": 0x08, "pressed": 0x08, "released": 0},
        {"seq": start_seq + 3, "held": 0, "pressed": 0, "released": 0x08},
        {"seq": start_seq + 4, "held": 0x0C00, "pressed": 0x0C00, "released": 0},
    ]


def test_mission_payload_tracks_live_fighter_visibility(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "build_overlay_payload", lambda name: {
        "character": name,
        "active_mission_id": "alex_001",
        "active_mission_steps": [],
        "missions": [],
    })
    manager = _manager()
    try:
        snap = {"base": 0x90000000, "name": "Alex", "teamtag": "P1"}
        manager.active_slot = "P1-C1"
        manager._render_snap_by_slot = {"P1-C1": snap}
        manager._live_snaps_by_slot = {"P1-C1": snap}
        manager._capture_mission_owner("P1-C1", manager._render_snap_by_slot)
        monkeypatch.setattr(manager, "_augment_payload_with_runtime", lambda payload, _snaps: payload)
        monkeypatch.setattr(manager, "_sync_debug_overrides", lambda _payload: None)
        monkeypatch.setattr(manager, "_queue_overlay_payload", lambda _payload: None)

        manager.write_overlay_data(manager._render_snap_by_slot)
        assert manager.last_overlay_payload["fighter_loaded"] is True

        # The render snapshot lingers for the HUD exit animation; the live
        # snapshot has already gone away.
        manager._live_snaps_by_slot = {}
        manager.write_overlay_data(manager._render_snap_by_slot)
        assert manager.last_overlay_payload["fighter_loaded"] is False
    finally:
        manager.close()


def test_selector_reopens_after_selecting_a_mission(monkeypatch, tmp_path) -> None:
    module = _module()
    menu_module = importlib.import_module("tvcgui.runtime.mission_menu_input")
    monkeypatch.setattr(menu_module, "MISSION_MENU_VISUAL_FILE", str(tmp_path / "visual.json"))
    monkeypatch.setattr(
        module,
        "build_overlay_payload",
        lambda name: {
            "character": name,
            "missions": [{"mission_id": "alex_002"}],
            "active_mission_id": "alex_002",
        },
    )
    monkeypatch.setattr(module, "load_progress", lambda: {})
    monkeypatch.setattr(module, "save_progress", lambda _progress: None)
    monkeypatch.setattr(module, "set_selected_mission_id", lambda progress, *_args: progress)

    manager = _manager()
    manager.active_slot = "P1-C1"
    snap = {
        "base": 0x90000000,
        "name": "Alex",
        "teamtag": "P1",
        "damage_point_active": True,
    }
    manager._render_snap_by_slot = {"P1-C1": snap}
    samples = _gesture(1)
    manager.set_input_sample_provider(lambda *_args: ({}, list(samples)))

    manager._update_selector_from_inputs({"P1-C1": snap}, 10.0)
    assert manager.selector_open is True

    samples.extend([
        {"seq": 6, "held": 0, "pressed": 0, "released": 0x0C00},
        {"seq": 7, "held": 0x0C00, "pressed": 0x0C00, "released": 0},
    ])
    manager._update_selector_from_inputs({"P1-C1": snap}, 10.1)
    assert manager.selector_open is False

    samples.extend([
        {"seq": 8, "held": 0, "pressed": 0, "released": 0x0C00},
        *_gesture(9),
    ])
    manager._update_selector_from_inputs({"P1-C1": snap}, 10.2)
    assert manager.selector_open is True


def test_selector_after_tag_opens_new_point_characters_missions(monkeypatch, tmp_path) -> None:
    module = _module()
    menu_module = importlib.import_module("tvcgui.runtime.mission_menu_input")
    monkeypatch.setattr(menu_module, "MISSION_MENU_VISUAL_FILE", str(tmp_path / "visual.json"))
    monkeypatch.setattr(
        module,
        "build_overlay_payload",
        lambda name: {
            "character": name,
            "missions": [{"mission_id": "casshan_001" if name == "Casshan" else "alex_001"}],
            "active_mission_id": "casshan_001" if name == "Casshan" else "alex_001",
        },
    )

    manager = _manager()
    manager.active_slot = "P1-C1"
    alex = {
        "base": 0x90000000,
        "name": "Alex",
        "teamtag": "P1",
        "csv_char_id": 4,
        "damage_point_active": False,
    }
    casshan = {
        "base": 0x90010000,
        "name": "Casshan",
        "teamtag": "P1",
        "csv_char_id": 2,
        "damage_point_active": True,
    }
    snaps = {"P1-C1": alex, "P1-C2": casshan}
    manager._render_snap_by_slot = snaps
    requested: list[str] = []

    def provider(slot: str, _base: int):
        requested.append(slot)
        return {}, _gesture(20) if slot == "P1-C2" else []

    manager.set_input_sample_provider(provider)
    manager._update_selector_from_inputs(snaps, 20.0)

    assert requested == ["P1-C2"]
    assert manager.selector_open is True
    assert manager.active_slot == "P1-C2"
    assert manager._mission_owner["name"] == "Casshan"


def test_completed_setup_flag_restores_and_forces_cpu_idle() -> None:
    manager = _manager()
    values = {"CpuAction": 7}
    writes: list[tuple[str, int]] = []
    manager._read_debug_flag = lambda name: values.get(name, 0)

    def write(name: str, value: int) -> bool:
        values[name] = value
        writes.append((name, value))
        return True

    manager._write_debug_flag = write
    payload = {
        "active": True,
        "slot": "P1-C1",
        "character": "Alex",
        "active_mission_id": "alex_002",
        "active_mission_setup_debug_flags": {"CpuAction": 2},
    }

    manager._sync_debug_overrides(payload)
    assert values["CpuAction"] == 2

    manager._sync_debug_overrides({**payload, "just_cleared": True})
    assert values["CpuAction"] == 0
    assert manager._setup_state["completed_mission_key"] == (
        "P1-C1", "Alex", "alex_002"
    )

    writes_before = list(writes)
    manager._sync_debug_overrides(payload)
    assert writes == writes_before
    assert values["CpuAction"] == 0

    manager.active_slot = "P1-C1"
    manager._prepare_selected_mission("Alex", "alex_002")
    manager._sync_debug_overrides(payload)
    assert values["CpuAction"] == 2


def test_mission_without_cpu_action_forces_idle_once() -> None:
    manager = _manager()
    values = {"CpuAction": 5}
    writes: list[tuple[str, int]] = []
    manager._read_debug_flag = lambda name: values.get(name, 0)

    def write(name: str, value: int) -> bool:
        values[name] = value
        writes.append((name, value))
        return True

    manager._write_debug_flag = write
    payload = {
        "active": True,
        "slot": "P1-C1",
        "character": "Alex",
        "active_mission_id": "alex_001",
        "active_mission_setup_debug_flags": {},
    }

    manager._sync_debug_overrides(payload)
    manager._sync_debug_overrides(payload)

    assert values["CpuAction"] == 0
    assert writes.count(("CpuAction", 0)) == 1


def test_regular_mission_clears_prior_guard_and_pushblock_setup() -> None:
    manager = _manager()
    manager.active_slot = "P1-C1"
    values = {"CpuAction": 0, "CpuGuard": 1, "CpuPushblock": 1}
    writes: list[tuple[str, int]] = []
    manager._read_debug_flag = lambda name: values.get(name, 0)

    def write(name: str, value: int) -> bool:
        values[name] = value
        writes.append((name, value))
        return True

    manager._write_debug_flag = write
    special = {
        "active": True, "slot": "P1-C1", "character": "Chun-Li",
        "active_mission_id": "chun_008",
        "active_mission_setup_debug_flags": {"CpuGuard": 2, "CpuPushblock": 1},
    }
    manager._sync_debug_overrides(special)
    assert values["CpuGuard"] == 2

    manager._prepare_selected_mission("Chun-Li", "chun_001")
    ordinary = {**special, "active_mission_id": "chun_001", "active_mission_setup_debug_flags": {}}
    manager._sync_debug_overrides(ordinary)
    assert (values["CpuAction"], values["CpuGuard"], values["CpuPushblock"]) == (0, 0, 0)

    before = list(writes)
    manager._sync_debug_overrides(ordinary)
    assert writes == before


def test_five_bar_refill_pauses_for_hitstun_and_stops_at_full_meter() -> None:
    manager = _manager()
    values = {"P1Meter": 0, "BaroquePct": 0}
    writes: list[tuple[str, int]] = []
    manager._read_debug_flag = lambda name: values.get(name, 0)

    def write(name: str, value: int) -> bool:
        values[name] = value
        writes.append((name, value))
        return True

    manager._write_debug_flag = write
    hitstun = False
    manager._opponent_in_hitstun = lambda *_args: hitstun
    payload = {
        "active": True, "slot": "P1-C1", "active_mission_id": "ryu_007",
        "active_mission_setup_meter_refill": True,
    }
    snap = {"P1-C1": {"meter": 10000}}
    manager._update_meter_refill(payload, "P1-C1", snap)
    assert values == {"P1Meter": 1, "BaroquePct": 1}

    hitstun = True
    manager._update_meter_refill(payload, "P1-C1", snap)
    assert values["P1Meter"] == 0

    hitstun = False
    snap["P1-C1"]["meter"] = 50000
    manager._update_meter_refill(payload, "P1-C1", snap)
    assert values["P1Meter"] == 0
    before = list(writes)
    manager._update_meter_refill(payload, "P1-C1", snap)
    assert writes == before


def test_saki_shell_refill_rearms_for_each_load_step_and_restores_meter() -> None:
    manager = _manager()
    try:
        values = {"P1Meter": 3, "BaroquePct": 7}
        manager._read_debug_flag = lambda name: values.get(name, 0)
        manager._write_debug_flag = lambda name, value: values.__setitem__(name, value) or True
        hitstun = False
        manager._opponent_in_hitstun = lambda *_args: hitstun
        shell = {"labels": ["LOAD SUPER ARMOR PIERCING SHELL"]}
        payload = {
            "active": True,
            "slot": "P1-C1",
            "character": "Saki",
            "active_mission_id": "saki_test_route",
            "active_mission_steps": [shell, {"labels": ["5C"]}, shell],
            "current_step_index": 0,
            "mission_attempt_id": 1,
            "mission_event_floor": 10,
        }
        snaps = {"P1-C1": {"meter": 0}}

        manager._update_meter_refill(payload, "P1-C1", snaps)
        assert values == {"P1Meter": 1, "BaroquePct": 7}
        hitstun = True
        manager._update_meter_refill(payload, "P1-C1", snaps)
        assert values["P1Meter"] == 0
        hitstun = False
        snaps["P1-C1"]["meter"] = 10000
        manager._update_meter_refill(payload, "P1-C1", snaps)
        assert values["P1Meter"] == 0
        snaps["P1-C1"]["meter"] = 0
        manager._update_meter_refill(payload, "P1-C1", snaps)
        assert values["P1Meter"] == 0  # This load step was already supplied.

        payload["current_step_index"] = 1
        manager._update_meter_refill(payload, "P1-C1", snaps)
        assert values["P1Meter"] == 0  # Other steps use normal meter gain.

        payload["current_step_index"] = 2
        manager._update_meter_refill(payload, "P1-C1", snaps)
        assert values["P1Meter"] == 1  # The next load step gets its own bar.
        payload.update(current_step_index=0, mission_attempt_id=2)
        manager._update_meter_refill(payload, "P1-C1", snaps)
        assert values["P1Meter"] == 1  # A retry also gets a fresh bar.

        manager._update_meter_refill({"active": False}, "P1-C1", snaps)
        assert values == {"P1Meter": 3, "BaroquePct": 7}
    finally:
        manager.close()


def test_clear_character_completions_keeps_selection_and_other_characters() -> None:
    module = importlib.import_module("tvcgui.features.training.mission_mode")
    progress = {
        "chun_li": {"completed": {"chun_001": True}, "selected_mission_id": "chun_003"},
        "ryu": {"completed": {"ryu_001": True}, "selected_mission_id": "ryu_001"},
    }
    cleared = module.clear_character_completions(progress, "Chun-Li")
    assert module.is_mission_complete(cleared, "Chun-Li", "chun_001") is False
    assert module.get_selected_mission_id(cleared, "Chun-Li") == "chun_003"
    assert module.is_mission_complete(cleared, "Ryu", "ryu_001") is True


def test_mission_lock_survives_completion_and_can_be_cleared() -> None:
    module = importlib.import_module("tvcgui.features.training.mission_mode")
    progress = module.set_selected_mission_id({}, "Chun-Li", "chun_003")
    progress = module.set_locked_mission_id(progress, "Chun-Li", "chun_003")
    progress = module.mark_mission_complete(progress, "Chun-Li", "chun_003")
    progress = module.clear_character_completions(progress, "Chun-Li")
    assert module.get_locked_mission_id(progress, "Chun-Li") == "chun_003"
    assert module.get_selected_mission_id(progress, "Chun-Li") == "chun_003"
    assert not module.is_mission_complete(progress, "Chun-Li", "chun_003")
    progress = module.set_locked_mission_id(progress, "Chun-Li", None)
    assert module.get_locked_mission_id(progress, "Chun-Li") is None


def test_clear_advances_after_recovery_and_celebration_ack(monkeypatch) -> None:
    module = _module()
    mode = importlib.import_module("tvcgui.features.training.mission_mode")
    from tvcgui.features.training.mission_runtime_v2 import RuntimeResult

    saved = {"progress": mode.set_selected_mission_id({}, "Alex", "alex_001")}
    monkeypatch.setattr(module, "load_progress", lambda: saved["progress"])
    monkeypatch.setattr(module, "save_progress", lambda value: saved.__setitem__("progress", value))
    monkeypatch.setattr(module, "build_overlay_payload", lambda _name: {
        "character": "Alex",
        "active_mission_id": mode.get_selected_mission_id(saved["progress"], "Alex"),
        "missions": [{"mission_id": "alex_001"}, {"mission_id": "alex_002"}],
    })
    manager = _manager()
    manager.set_event_provider(lambda *_args: (0, ()))
    manager._update_meter_refill = lambda *_args: None
    manager._set_mission_realtime_context = lambda **_kwargs: ("test",)
    manager._realtime_result_with_revision_for_key = lambda *_args: (
        RuntimeResult(
            progress_index=0, current_step_label="5A", cleared=False,
            phase="ARMED", attempt_id=2, event_floor=0, event_cursor=0,
            buffered_advances=0, restart_count=1, last_restart_reason="wakeup",
        ), 1,
    )
    prepared = []
    manager._prepare_selected_mission = lambda _character, mission_id: prepared.append(mission_id)
    manager._runtime.update({
        "v2_clear_latched_attempt": 1,
        "celebrate_token": 3,
        "celebrate_acked_token": 3,
        "mission_event_floor_sequence": 0,
    })
    payload = {
        "active": True, "slot": "P1-C1", "point_slot": "P1-C1",
        "character": "Alex", "active_mission_id": "alex_001",
        "active_mission_steps": [{"labels": ["5A"]}], "active_mission_goal": {},
        "missions": [{"mission_id": "alex_001"}, {"mission_id": "alex_002"}],
    }
    result = manager._augment_payload_with_runtime(payload, {"P1-C1": {"csv_char_id": 1}})
    assert result["active_mission_id"] == "alex_002"
    assert mode.get_selected_mission_id(saved["progress"], "Alex") == "alex_002"
    assert prepared == ["alex_002"]
    assert manager._runtime["celebrate_token"] == 3
    manager.close()


def test_chun_second_jc_hit_is_accepted_as_jc() -> None:
    from tvcgui.features.training.mission_runtime_v2 import MissionRuntimeV2
    from tvcgui.runtime.mission_events import EVENT_ACTION, EVENT_DAMAGE, MissionEvent

    manager = _manager()
    labels = manager._action_label_candidates(0x10C, 13)
    assert "j.C" in labels
    runtime = MissionRuntimeV2()
    runtime.configure(
        slot="P1-C1", character="Chun-Li", mission_id="chun_005",
        raw_steps=[{"labels": ["j.C"]}, {"labels": ["j.C"]}],
        goal={}, event_floor=0,
    )
    def event(sequence: int, kind: str, slot: str, **kwargs):
        return MissionEvent(sequence, sequence, sequence * 20_000_000, kind, slot, **kwargs)
    result = runtime.consume(
        [
            event(1, EVENT_ACTION, "P1-C1", action_id=0x10B),
            event(2, EVENT_DAMAGE, "P2-C1", damage=100),
            event(3, EVENT_ACTION, "P1-C1", action_id=0x10C),
            event(4, EVENT_DAMAGE, "P2-C1", damage=100),
        ],
        owner_slot="P1-C1",
        label_matcher=manager._mission_label_matches,
        action_label_resolver=lambda item: manager._action_label_candidates(item.action_id, 13),
        newest_sequence=4,
    )
    assert result.cleared


def test_controller_clear_entry_clears_only_current_character(monkeypatch) -> None:
    module = _module()
    manager = _manager()
    manager.active_slot = "P1-C1"
    manager._selector.update({"open": True, "selected_index": 1, "source_slot": "P1-C1"})
    manager._mission_owner_name = lambda _snaps: "Chun-Li"
    monkeypatch.setattr(module, "build_overlay_payload", lambda _name: {
        "missions": [{"mission_id": "chun_001"}],
    })
    saved = []
    monkeypatch.setattr(module, "load_progress", lambda: {
        "chun_li": {"completed": {"chun_001": True}, "selected_mission_id": "chun_001"},
        "ryu": {"completed": {"ryu_001": True}, "selected_mission_id": "ryu_001"},
    })
    monkeypatch.setattr(module, "save_progress", lambda progress: saved.append(progress))

    class Queue:
        def drain_commands(self):
            return [module.MissionMenuCommand(1, module.MISSION_MENU_SELECT, "P1", "P1-C1", 0, 13, 1)]

        def set_menu_open(self, *_args):
            pass

        def configure_menu(self, *_args):
            pass

    manager._menu_input_interpreter = Queue()
    manager._consume_menu_input_commands({}, 10.0)
    assert len(saved) == 1
    assert saved[0]["chun_li"]["completed"] == {}
    assert saved[0]["ryu"]["completed"] == {"ryu_001": True}
    assert manager.selector_open is True
