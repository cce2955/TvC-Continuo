from __future__ import annotations

from copy import deepcopy

from tvcgui.features.stage_select import runtime


def test_yami_button_uses_the_id_2_route(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(runtime, "request_stage_selection", lambda code, **kw: calls.append((code, kw)) or {})

    runtime.request_arm_yami_stage()

    assert calls == [(runtime.YAMI_REPLACEMENT_SLOT, {"label": "Yami handoff test", "arm_yami": True})]


def test_yami_route_prearms_result_and_cancel_restores_before_confirm(monkeypatch) -> None:
    carousel = 0x92000000
    original_result = 7
    memory = {
        runtime.STAGE_GLOBAL_SELECTION: 1,
        runtime.STAGE_RUNTIME_PTR: carousel,
        carousel + runtime.RUNTIME_ENTRY_COUNT_OFF: 15,
        carousel + runtime.RUNTIME_SELF_PTR_OFF: carousel,
        carousel + runtime.RUNTIME_SELECTION_OFF: 1,
        carousel + runtime.CHARACTER_UI_STATE_OFF: 0,
        runtime.SELECT_SCENE_GLOBAL: 0,
        runtime.STAGE_RESULT_ID: original_result,
    }
    monkeypatch.setattr(runtime, "rd32", lambda address: memory.get(address))
    monkeypatch.setattr(runtime, "wd32", lambda address, value: memory.__setitem__(address, value) or True)
    monkeypatch.setattr(runtime, "_STATE", deepcopy(runtime._STATE))
    monkeypatch.setattr(runtime, "_SLOT2_YAMI_CANCEL", None)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    class UnstartedThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(runtime.threading, "Thread", UnstartedThread)

    state = runtime.select_yami_stage_slot()
    assert state["last_write"]["verified"] is True
    assert state["slot2_yami"]["active"] is True
    assert memory[runtime.STAGE_GLOBAL_SELECTION] == runtime.YAMI_REPLACEMENT_SLOT
    assert memory[carousel + runtime.RUNTIME_SELECTION_OFF] == runtime.YAMI_REPLACEMENT_SLOT
    assert memory[runtime.STAGE_RESULT_ID] == runtime.YAMI_STAGE_ASSET_ID

    runtime.cancel_stage_result_override()
    assert memory[runtime.STAGE_RESULT_ID] == original_result
    assert runtime.get_stage_probe_state()["slot2_yami"]["active"] is False

    runtime.set_stage_selection(runtime.YAMI_REPLACEMENT_SLOT)
    assert runtime.get_stage_probe_state()["slot2_yami"]["active"] is False
    assert memory[runtime.STAGE_RESULT_ID] == original_result


def test_cancel_after_character_select_restores_pending_route(monkeypatch) -> None:
    memory = {runtime.STAGE_RESULT_ID: runtime.YAMI_STAGE_ASSET_ID,
              runtime.STAGE_GLOBAL_SELECTION: runtime.YAMI_STAGE_ASSET_ID}
    monkeypatch.setattr(runtime, "rd32", lambda address: memory.get(address))
    monkeypatch.setattr(runtime, "wd32", lambda address, value: memory.__setitem__(address, value) or True)
    monkeypatch.setattr(runtime, "_STATE", deepcopy(runtime._STATE))
    event = runtime.threading.Event()
    monkeypatch.setattr(runtime, "_SLOT2_YAMI_CANCEL", event)
    runtime._set_slot2_yami_state(active=True, phase="awaiting_match", baseline=7,
                                  global_overridden=True)

    runtime.cancel_stage_result_override()

    assert event.is_set()
    assert memory[runtime.STAGE_RESULT_ID] == 7
    assert memory[runtime.STAGE_GLOBAL_SELECTION] == runtime.YAMI_REPLACEMENT_SLOT


def test_route_survives_character_select_and_holds_yami_until_live_match(monkeypatch) -> None:
    carousel = 0x92000000
    battle = 0x92001000
    memory = {
        runtime.STAGE_GLOBAL_SELECTION: 2,
        runtime.STAGE_RUNTIME_PTR: carousel,
        runtime.SELECT_SCENE_GLOBAL: 0,
        carousel + runtime.RUNTIME_ENTRY_COUNT_OFF: 15,
        carousel + runtime.RUNTIME_SELF_PTR_OFF: carousel,
        carousel + runtime.CHARACTER_UI_STATE_OFF: 0,
        runtime.STAGE_RESULT_ID: 15,
        runtime.NATIVE_BATTLE_PTR: battle,
        battle + 12: 0,
    }
    monkeypatch.setattr(runtime, "rd32", lambda address: memory.get(address))
    monkeypatch.setattr(runtime, "wd32", lambda address, value: memory.__setitem__(address, value) or True)
    monkeypatch.setattr(runtime, "_STATE", deepcopy(runtime._STATE))
    monkeypatch.setattr(runtime, "YAMI_MATCH_SETTLE_SEC", 0.0)
    route_events = []
    monkeypatch.setattr(runtime.session_recorder, "capture_event",
                        lambda when, kind, values: route_events.append((kind, values)))
    polls = 0

    def advance(_seconds):
        nonlocal polls
        polls += 1
        if polls == 1:
            # Stage is confirmed; character select replaces both values.
            memory[runtime.SELECT_SCENE_GLOBAL] = 1
            memory[carousel + runtime.CHARACTER_UI_STATE_OFF] = 1
            memory[runtime.STAGE_RESULT_ID] = 2
        elif polls == 2:
            assert memory[runtime.STAGE_GLOBAL_SELECTION] == 15
            assert memory[runtime.STAGE_RESULT_ID] == 15
            # The loader copies the raw cursor again before battle begins.
            memory[runtime.STAGE_RESULT_ID] = 2
            memory[battle + 12] = 4

    monkeypatch.setattr(runtime.time, "sleep", advance)
    runtime._run_slot2_yami_override(
        starting_runtime=carousel,
        baseline_value=7,
        cancel_event=runtime.threading.Event(),
    )

    assert polls == 2
    assert memory[runtime.STAGE_GLOBAL_SELECTION] == 15
    assert memory[runtime.STAGE_RESULT_ID] == 15
    assert "Match became live" in runtime.get_stage_probe_state()["slot2_yami"]["detail"]
    assert all(kind == "stage_route" for kind, _values in route_events)
    assert any(values["scene_state"] == 0 and values["after_global"] == 2
               for _kind, values in route_events)
    assert any(values["scene_state"] == 1 and values["observed_result"] == 2
               and values["after_result"] == 15 for _kind, values in route_events)


def test_route_retries_stage_writes_during_loader_transition(monkeypatch) -> None:
    carousel = 0x92000000
    battle = 0x92001000
    memory = {
        runtime.STAGE_GLOBAL_SELECTION: 2,
        runtime.STAGE_RUNTIME_PTR: carousel,
        runtime.SELECT_SCENE_GLOBAL: 0,
        carousel + runtime.RUNTIME_ENTRY_COUNT_OFF: 15,
        carousel + runtime.RUNTIME_SELF_PTR_OFF: carousel,
        carousel + runtime.CHARACTER_UI_STATE_OFF: 0,
        runtime.STAGE_RESULT_ID: 15,
        runtime.NATIVE_BATTLE_PTR: battle,
        battle + 12: 0,
    }
    failures_left = {runtime.STAGE_GLOBAL_SELECTION: 1, runtime.STAGE_RESULT_ID: 1}

    def write(address, value):
        if memory[runtime.SELECT_SCENE_GLOBAL] == 1 and failures_left.get(address, 0):
            failures_left[address] -= 1
            return False
        memory[address] = value
        return True

    monkeypatch.setattr(runtime, "rd32", lambda address: memory.get(address))
    monkeypatch.setattr(runtime, "wd32", write)
    monkeypatch.setattr(runtime, "_STATE", deepcopy(runtime._STATE))
    monkeypatch.setattr(runtime, "YAMI_MATCH_SETTLE_SEC", 0.0)
    polls = 0

    def advance(_seconds):
        nonlocal polls
        polls += 1
        if polls == 1:
            memory[runtime.SELECT_SCENE_GLOBAL] = 1
            memory[carousel + runtime.CHARACTER_UI_STATE_OFF] = 1
            memory[runtime.STAGE_GLOBAL_SELECTION] = 0
            memory[runtime.STAGE_RESULT_ID] = 2
        elif polls == 3:
            memory[battle + 12] = 4

    monkeypatch.setattr(runtime.time, "sleep", advance)
    runtime._run_slot2_yami_override(
        starting_runtime=carousel, baseline_value=7,
        cancel_event=runtime.threading.Event(),
    )

    state = runtime.get_stage_probe_state()["slot2_yami"]
    assert state["write_failures"] == 2
    assert memory[runtime.STAGE_GLOBAL_SELECTION] == 15
    assert memory[runtime.STAGE_RESULT_ID] == 15
    assert "Match became live" in state["detail"]
