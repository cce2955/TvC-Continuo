from tvcgui.features.stage_select import navigation_experiment as exp
import threading


def test_unavailable_menu_does_not_read_memory(monkeypatch):
    monkeypatch.setattr(exp.yami_tile, "_read_live_menu", lambda: None)
    assert exp.observe() == {"menu": "unavailable"}


def test_observation_uses_raw_values_and_optional_regions(monkeypatch):
    base, root = 0x91000000, 0x92000000
    reads = []
    monkeypatch.setattr(exp.yami_tile, "_read_live_menu", lambda: (base, root))
    monkeypatch.setattr(exp.yami_tile, "get_state", lambda: {"enabled": True})
    monkeypatch.setattr(exp.yami_tile.mem, "rd32", lambda addr: 15 if addr == root + exp.runtime.RUNTIME_SELECTION_OFF else None)
    monkeypatch.setattr(exp.yami_tile.mem, "rd8", lambda addr: None)

    def read(addr, size):
        reads.append((addr, size))
        return bytes(size) if addr == root else None

    monkeypatch.setattr(exp.yami_tile.mem, "rbytes", read)
    sample = exp.observe()
    assert sample["cursor_object"] == 15
    assert sample["cursor_global"] is None
    assert sample["ark_enabled"] is True
    assert "regions" not in sample
    assert reads == [(root, 0x100)]
    detailed = exp.observe(include_regions=True)
    assert detailed["regions"] == {"0x25b40": None, "0x260c0": None}
    assert detailed["region_offsets_are"] == "loaded container relative"


def test_start_stop_records_unavailable_and_refuses_duplicate_start(monkeypatch):
    called = threading.Event()

    def observe(**_kwargs):
        called.set()
        return {"menu": "unavailable"}

    monkeypatch.setattr(exp, "observe", observe)
    monkeypatch.setattr(exp.yami_tile, "get_state", lambda: {"enabled": False})
    capture = exp.NavigationExperiment()
    assert capture.start()
    assert called.wait(2)
    assert not capture.start()
    capture.stop()
    capture._thread.join(timeout=2)
    assert not capture.state()["running"]
    events, dropped = capture.events()
    assert events[0]["type"] == "metadata"
    assert events[0]["ark_enabled_at_start"] is False
    assert events[1]["menu"] == "unavailable"
    assert dropped == 0
