import pytest

from tvcgui.runtime import mission_menu_input
from tvcgui.runtime.mission_menu_input import (
    MISSION_MENU_OPEN,
    MissionMenuInputInterpreter,
)


@pytest.fixture(autouse=True)
def isolate_menu_visual_file(monkeypatch, tmp_path):
    """Keep gesture tests from writing to the application's runtime data."""
    monkeypatch.setattr(mission_menu_input, "MISSION_MENU_VISUAL_FILE", str(tmp_path / "visual.json"))


def _sample(*, held=0, pressed=0, seq=1, ns=1_000_000_000):
    return {
        "slot": "P1-C1",
        "base": 0x92000000,
        "char_id": 1,
        "point_active": True,
        "held": held,
        "pressed": pressed,
        "seq": seq,
        "sample_ns": ns,
    }


def _down_down_taunt(interpreter, start_seq=1, start_ns=1_000_000_000):
    packets = [
        _sample(held=0x08, seq=start_seq, ns=start_ns),
        _sample(held=0x00, seq=start_seq + 1, ns=start_ns + 10_000_000),
        _sample(held=0x08, seq=start_seq + 2, ns=start_ns + 20_000_000),
        _sample(held=0x00, seq=start_seq + 3, ns=start_ns + 30_000_000),
        _sample(held=0x0C00, pressed=0x0C00, seq=start_seq + 4, ns=start_ns + 40_000_000),
    ]
    for packet in packets:
        interpreter.on_sample("P1-C1", packet)


def test_down_down_taunt_is_ignored_when_mission_mode_disabled():
    interpreter = MissionMenuInputInterpreter()
    assert interpreter.enabled is False
    _down_down_taunt(interpreter)
    assert interpreter.drain_commands() == []


def test_down_down_taunt_opens_only_after_mission_mode_enabled():
    interpreter = MissionMenuInputInterpreter()
    interpreter.set_enabled(True)
    _down_down_taunt(interpreter)
    commands = interpreter.drain_commands()
    assert [command.kind for command in commands] == [MISSION_MENU_OPEN]


def test_disabled_gesture_cannot_be_completed_after_enabling():
    interpreter = MissionMenuInputInterpreter()
    # Perform Down, Down during normal play.
    for packet in (
        _sample(held=0x08, seq=1, ns=1_000_000_000),
        _sample(held=0x00, seq=2, ns=1_010_000_000),
        _sample(held=0x08, seq=3, ns=1_020_000_000),
        _sample(held=0x00, seq=4, ns=1_030_000_000),
    ):
        interpreter.on_sample("P1-C1", packet)

    interpreter.set_enabled(True)
    interpreter.on_sample(
        "P1-C1",
        _sample(held=0x0C00, pressed=0x0C00, seq=5, ns=1_040_000_000),
    )
    assert interpreter.drain_commands() == []


def test_disabling_clears_open_menu_and_pending_commands():
    interpreter = MissionMenuInputInterpreter()
    interpreter.set_enabled(True)
    _down_down_taunt(interpreter)
    interpreter.set_enabled(False)
    assert interpreter.enabled is False
    assert interpreter.drain_commands() == []
