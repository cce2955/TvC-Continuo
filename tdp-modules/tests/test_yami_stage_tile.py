from __future__ import annotations

from copy import deepcopy

from tvcgui.features.stage_select import runtime, yami_tile


def _fake_menu(monkeypatch):
    base = 0x91000000
    root = 0x92000000
    words = {
        yami_tile._SEQ_PTR: base,
        runtime.STAGE_RUNTIME_PTR: root,
        root + runtime.RUNTIME_ENTRY_COUNT_OFF: 15,
        root + runtime.RUNTIME_SELF_PTR_OFF: root,
        root + runtime.RUNTIME_SELECTION_OFF: 17,
        root + runtime.CHARACTER_UI_STATE_OFF: 0,
        runtime.SELECT_SCENE_GLOBAL: 0,
        runtime.STAGE_GLOBAL_SELECTION: 17,
        runtime.STAGE_RESULT_ID: 7,
    }
    bytes_ = {base + offset: old for offset, (old, _) in yami_tile._PATCH.items()}
    writes = []
    monkeypatch.setattr(yami_tile.mem, "rd32", lambda addr: words.get(addr))
    monkeypatch.setattr(yami_tile.mem, "wd32", lambda addr, val: writes.append((addr, val)) or words.__setitem__(addr, val) or True)
    monkeypatch.setattr(yami_tile.mem, "rd8", lambda addr: bytes_.get(addr))
    monkeypatch.setattr(yami_tile.mem, "wd8", lambda addr, val: bytes_.__setitem__(addr, val) or True)
    monkeypatch.setattr(yami_tile.mem, "rbytes", lambda addr, size: yami_tile._SIGNATURE if addr == base + 0x40 else b"")
    return base, root, words, bytes_, writes


def test_guarded_tile_patch_restores_exact_original_bytes(monkeypatch):
    base, root, words, bytes_, _writes = _fake_menu(monkeypatch)
    assert yami_tile._read_live_menu() == (base, root)
    changed = yami_tile._apply(base)
    assert changed == list(yami_tile._PATCH)
    assert all(bytes_[base + off] == new for off, (_old, new) in yami_tile._PATCH.items())
    yami_tile._restore(base, changed)
    assert all(bytes_[base + off] == old for off, (old, _new) in yami_tile._PATCH.items())


def test_foreign_menu_byte_refuses_all_writes(monkeypatch):
    base, _root, _words, bytes_, _writes = _fake_menu(monkeypatch)
    bytes_[base + 0x25BAB] = 0xEE
    try:
        yami_tile._apply(base)
    except RuntimeError as exc:
        assert "differs" in str(exc)
    else:
        raise AssertionError("Foreign menu byte was accepted")
    assert bytes_[base + 0x25B7F] == 15


def test_tile_writes_use_guarded_quarantine_exception(monkeypatch):
    base, _root, _words, bytes_, _writes = _fake_menu(monkeypatch)
    bypasses = []

    def write_byte(address, value, *, bypass_quarantine=False):
        bypasses.append(bypass_quarantine)
        bytes_[address] = value
        return True

    monkeypatch.setattr(yami_tile.mem, "wd8", write_byte)
    changed = yami_tile._apply(base)
    yami_tile._restore(base, changed)
    assert bypasses == [True] * (2 * len(yami_tile._PATCH))


def test_partial_menu_patch_failure_restores_prior_bytes(monkeypatch):
    base, _root, _words, bytes_, _writes = _fake_menu(monkeypatch)
    failed_addr = base + 0x25BAB

    def write_byte(address, value, *, bypass_quarantine=False):
        if address == failed_addr and value == 16:
            return False
        bytes_[address] = value
        return True

    monkeypatch.setattr(yami_tile.mem, "wd8", write_byte)
    try:
        yami_tile._apply(base)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Partial patch unexpectedly succeeded")
    assert all(bytes_[base + off] == old for off, (old, _new) in yami_tile._PATCH.items())


def test_id_15_route_prearms_without_changing_training_cursor(monkeypatch):
    _base, root, words, _bytes, writes = _fake_menu(monkeypatch)
    words[root + runtime.RUNTIME_SELECTION_OFF] = 15
    words[runtime.STAGE_GLOBAL_SELECTION] = 15
    monkeypatch.setattr(runtime, "rd32", lambda addr: words.get(addr))
    bypasses = []

    def write_stage(address, value, *, bypass_quarantine=False):
        writes.append((address, value))
        bypasses.append(bypass_quarantine)
        words[address] = value
        return True

    monkeypatch.setattr(runtime, "wd32", write_stage)
    monkeypatch.setattr(runtime, "_STATE", deepcopy(runtime._STATE))
    monkeypatch.setattr(runtime, "_SLOT2_YAMI_CANCEL", None)

    class UnstartedThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(runtime.threading, "Thread", UnstartedThread)
    armed, _detail = runtime._arm_slot2_yami_override(root, source_code=15)
    assert armed
    assert words[runtime.STAGE_RESULT_ID] == 15
    assert bypasses == [True]
    assert words[root + runtime.RUNTIME_SELECTION_OFF] == 15
    assert runtime._slot2_yami_snapshot()["source_code"] == 15
    runtime._cancel_slot2_yami_if_active(restore=True)
    assert words[runtime.STAGE_RESULT_ID] == 7
    assert bypasses == [True, True]


def test_leaving_ark_does_not_overwrite_a_new_training_result(monkeypatch):
    _base, root, words, _bytes, _writes = _fake_menu(monkeypatch)
    words[root + runtime.RUNTIME_SELECTION_OFF] = 15
    words[runtime.STAGE_GLOBAL_SELECTION] = 15
    monkeypatch.setattr(runtime, "rd32", lambda addr: words.get(addr))
    monkeypatch.setattr(runtime, "wd32", lambda addr, val: words.__setitem__(addr, val) or True)
    monkeypatch.setattr(runtime, "_STATE", deepcopy(runtime._STATE))
    monkeypatch.setattr(runtime, "_SLOT2_YAMI_CANCEL", None)

    class UnstartedThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(runtime.threading, "Thread", UnstartedThread)
    armed, _detail = runtime._arm_slot2_yami_override(root, source_code=15)
    assert armed
    words[runtime.STAGE_RESULT_ID] = 17
    runtime._cancel_slot2_yami_if_active(restore=True)
    assert words[runtime.STAGE_RESULT_ID] == 17


def test_id_15_route_holds_ark_through_character_select(monkeypatch):
    _base, root, words, _bytes, _writes = _fake_menu(monkeypatch)
    battle = 0x92001000
    words[root + runtime.RUNTIME_SELECTION_OFF] = 15
    words[runtime.STAGE_GLOBAL_SELECTION] = 15
    words[runtime.STAGE_RESULT_ID] = 15
    words[runtime.NATIVE_BATTLE_PTR] = battle
    words[battle + 12] = 0
    monkeypatch.setattr(runtime, "rd32", lambda addr: words.get(addr))
    monkeypatch.setattr(runtime, "wd32", lambda addr, val: words.__setitem__(addr, val) or True)
    monkeypatch.setattr(runtime, "_STATE", deepcopy(runtime._STATE))
    monkeypatch.setattr(runtime, "YAMI_MATCH_SETTLE_SEC", 0.0)
    monkeypatch.setattr(runtime.session_recorder, "capture_event", lambda *_args: None)
    polls = 0

    def advance(_seconds):
        nonlocal polls
        polls += 1
        if polls == 1:
            words[runtime.SELECT_SCENE_GLOBAL] = 1
            words[root + runtime.CHARACTER_UI_STATE_OFF] = 1
            words[runtime.STAGE_RESULT_ID] = 17
        elif polls == 2:
            assert words[runtime.STAGE_RESULT_ID] == 15
            words[battle + 12] = 4

    monkeypatch.setattr(runtime.time, "sleep", advance)
    runtime._run_slot2_yami_override(
        starting_runtime=root, baseline_value=7,
        cancel_event=runtime.threading.Event(), source_code=15,
    )
    assert polls == 2
    assert words[runtime.STAGE_GLOBAL_SELECTION] == 15
    assert words[runtime.STAGE_RESULT_ID] == 15
