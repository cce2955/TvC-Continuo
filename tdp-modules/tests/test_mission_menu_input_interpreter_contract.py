from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_dolphin_stub() -> None:
    if "dolphin_memory_engine" in sys.modules:
        return
    dme = types.ModuleType("dolphin_memory_engine")
    dme.is_hooked = lambda: False
    dme.hook = lambda: None
    dme.un_hook = lambda: None
    dme.read_byte = lambda *_args, **_kwargs: 0
    dme.read_bytes = lambda *_args, **_kwargs: b""
    dme.write_byte = lambda *_args, **_kwargs: None
    dme.write_bytes = lambda *_args, **_kwargs: None
    sys.modules["dolphin_memory_engine"] = dme


def _sample(
    held: int,
    *,
    pressed: int = 0,
    point: bool = True,
    ns: int,
    char_id: int = 4,
    base: int = 0x90000000,
) -> dict:
    return {
        "held": held,
        "pressed": pressed,
        "released": 0,
        "point_active": point,
        "sample_ns": ns,
        "char_id": char_id,
        "base": base,
    }


def _gesture(interpreter, slot: str, start_ns: int, *, char_id: int = 4, base: int = 0x90000000):
    step = 100_000_000
    interpreter.on_sample(slot, _sample(0x08, pressed=0x08, ns=start_ns, char_id=char_id, base=base))
    interpreter.on_sample(slot, _sample(0, ns=start_ns + step, char_id=char_id, base=base))
    interpreter.on_sample(slot, _sample(0x08, pressed=0x08, ns=start_ns + 2 * step, char_id=char_id, base=base))
    interpreter.on_sample(slot, _sample(0, ns=start_ns + 3 * step, char_id=char_id, base=base))
    interpreter.on_sample(slot, _sample(0x0C00, pressed=0x0C00, ns=start_ns + 4 * step, char_id=char_id, base=base))


def test_shortcut_reopens_after_menu_select(tmp_path, monkeypatch) -> None:
    menu = importlib.import_module("tvcgui.runtime.mission_menu_input")
    monkeypatch.setattr(menu, "MISSION_MENU_VISUAL_FILE", str(tmp_path / "visual.json"))
    from tvcgui.runtime.mission_menu_input import (
        MISSION_MENU_OPEN,
        MISSION_MENU_SELECT,
        MissionMenuInputInterpreter,
    )

    interpreter = MissionMenuInputInterpreter()
    interpreter.set_enabled(True)
    _gesture(interpreter, "P1-C1", 1_000_000_000)
    commands = interpreter.drain_commands()
    assert [command.kind for command in commands] == [MISSION_MENU_OPEN]

    # Release Taunt, then press it once to select the current mission.
    interpreter.on_sample("P1-C1", _sample(0, ns=1_500_000_000))
    interpreter.on_sample(
        "P1-C1", _sample(0x0C00, pressed=0x0C00, ns=1_600_000_000)
    )
    commands = interpreter.drain_commands()
    assert [command.kind for command in commands] == [MISSION_MENU_SELECT]

    # A fresh release and gesture must open again. No MissionManager reset is
    # involved because this state belongs entirely to the interpreter.
    interpreter.on_sample("P1-C1", _sample(0, ns=1_700_000_000))
    _gesture(interpreter, "P1-C1", 2_000_000_000)
    commands = interpreter.drain_commands()
    assert [command.kind for command in commands] == [MISSION_MENU_OPEN]


def test_navigation_publishes_cursor_before_manager_drains_commands(tmp_path, monkeypatch) -> None:
    menu = importlib.import_module("tvcgui.runtime.mission_menu_input")
    visual_file = tmp_path / "menu_visual.json"
    monkeypatch.setattr(menu, "MISSION_MENU_VISUAL_FILE", str(visual_file))
    interpreter = menu.MissionMenuInputInterpreter()
    interpreter.set_enabled(True)
    interpreter.on_sample("P1-C1", _sample(0, ns=1_000_000_000))
    interpreter.set_menu_open("P1", "P1-C1")
    interpreter.configure_menu("P1-C1", 5, 2)
    interpreter.on_sample("P1-C1", _sample(0x08, pressed=0x08, ns=1_020_000_000))
    assert json.loads(visual_file.read_text(encoding="utf-8"))["index"] == 3
    assert [command.kind for command in interpreter.drain_commands()] == [menu.MISSION_MENU_NEXT]


def test_right_locks_and_left_unlocks_highlighted_mission(tmp_path, monkeypatch) -> None:
    menu = importlib.import_module("tvcgui.runtime.mission_menu_input")
    monkeypatch.setattr(menu, "MISSION_MENU_VISUAL_FILE", str(tmp_path / "visual.json"))
    interpreter = menu.MissionMenuInputInterpreter()
    interpreter.set_enabled(True)
    interpreter.on_sample("P1-C1", _sample(0, ns=1_000_000_000))
    interpreter.set_menu_open("P1", "P1-C1")
    interpreter.configure_menu("P1-C1", 4, 1)
    interpreter.on_sample("P1-C1", _sample(0x01, pressed=0x01, ns=1_020_000_000))
    interpreter.on_sample("P1-C1", _sample(0x02, pressed=0x02, ns=1_040_000_000))
    assert [command.kind for command in interpreter.drain_commands()] == [
        menu.MISSION_MENU_LOCK, menu.MISSION_MENU_UNLOCK,
    ]
    assert json.loads((tmp_path / "visual.json").read_text(encoding="utf-8"))["index"] == 1


def test_tag_moves_shortcut_to_new_native_point_slot(tmp_path, monkeypatch) -> None:
    menu = importlib.import_module("tvcgui.runtime.mission_menu_input")
    monkeypatch.setattr(menu, "MISSION_MENU_VISUAL_FILE", str(tmp_path / "visual.json"))
    from tvcgui.runtime.mission_menu_input import (
        MISSION_MENU_OPEN,
        MissionMenuInputInterpreter,
    )

    interpreter = MissionMenuInputInterpreter()
    interpreter.set_enabled(True)
    interpreter.on_sample("P1-C1", _sample(0, point=True, ns=1_000_000_000))
    interpreter.on_sample("P1-C1", _sample(0, point=False, ns=1_100_000_000))
    interpreter.on_sample(
        "P1-C2",
        _sample(0, point=True, ns=1_200_000_000, char_id=2, base=0x90010000),
    )
    _gesture(
        interpreter,
        "P1-C2",
        2_000_000_000,
        char_id=2,
        base=0x90010000,
    )
    commands = interpreter.drain_commands()
    assert len(commands) == 1
    assert commands[0].kind == MISSION_MENU_OPEN
    assert commands[0].slot == "P1-C2"
    assert commands[0].char_id == 2


def test_mission_manager_only_consumes_menu_commands(monkeypatch) -> None:
    _install_dolphin_stub()
    module = importlib.import_module("tvcgui.features.training.mission_manager")
    menu_module = importlib.import_module("tvcgui.runtime.mission_menu_input")

    monkeypatch.setattr(
        module,
        "build_overlay_payload",
        lambda name: {
            "character": name,
            "missions": [{"mission_id": "casshan_001"}],
            "active_mission_id": "casshan_001",
        },
    )
    manager = module.MissionManager({}, {}, {}, lambda: [], lambda *_args: "")

    class Queue:
        def __init__(self):
            self.commands = [
                menu_module.MissionMenuCommand(
                    sequence=1,
                    kind=menu_module.MISSION_MENU_OPEN,
                    teamtag="P1",
                    slot="P1-C2",
                    base=0x90010000,
                    char_id=2,
                    sample_ns=1,
                )
            ]

        def drain_commands(self):
            out, self.commands = self.commands, []
            return out

        def set_menu_open(self, *_args):
            pass

        def configure_menu(self, *_args):
            pass

        def close_menu(self, *_args):
            pass

    queue = Queue()
    manager.set_menu_input_interpreter(queue)
    manager.active_slot = "P1-C1"
    snaps = {
        "P1-C1": {
            "base": 0x90000000,
            "name": "Alex",
            "damage_point_active": False,
        },
        "P1-C2": {
            "base": 0x90010000,
            "name": "Casshan",
            "teamtag": "P1",
            "csv_char_id": 2,
            "damage_point_active": True,
        },
    }
    manager._render_snap_by_slot = snaps
    manager._consume_menu_input_commands(snaps, 10.0)

    assert manager.selector_open is True
    assert manager.active_slot == "P1-C2"
    assert manager._mission_owner["name"] == "Casshan"


def test_completed_mission_reasserts_idle_after_external_flag_change() -> None:
    _install_dolphin_stub()
    module = importlib.import_module("tvcgui.features.training.mission_manager")
    manager = module.MissionManager({}, {}, {}, lambda: [], lambda *_args: "")
    values = {"CpuAction": 7}
    writes = []
    manager._read_debug_flag = lambda name: values.get(name, 0)

    def write(name, value):
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
    manager._sync_debug_overrides({**payload, "just_cleared": True})
    assert values["CpuAction"] == 0

    # Simulate another subsystem or stale writer changing the flag afterward.
    values["CpuAction"] = 2
    manager._sync_debug_overrides(payload)
    assert values["CpuAction"] == 0
    assert writes[-1] == ("CpuAction", 0)
