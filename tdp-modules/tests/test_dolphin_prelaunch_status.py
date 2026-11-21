from __future__ import annotations

import threading
from pathlib import Path

from tvcgui.platform import dolphin
from tvcgui.ui.overseer import _format_state_text


def test_attach_retry_can_stop_before_dolphin_exists(monkeypatch):
    called = []
    monkeypatch.setattr(dolphin.dme, "hook", lambda: called.append(True))
    stop = threading.Event()
    stop.set()

    assert dolphin.hook(stop_event=stop) is False
    assert called == []


def test_tool_status_identifies_instance_and_recent_events():
    report = _format_state_text({
        "hooked": True,
        "dolphin": {
            "mem2_latched": True,
            "mem2_pid": 12292,
            "mem2_path": r"C:\Brawl Online\netplay\Dolphin.exe",
            "candidates": [
                {"pid": 3172, "path": r"C:\Dolphin\Dolphin.exe"},
                {"pid": 12292, "path": r"C:\Brawl Online\netplay\Dolphin.exe"},
            ],
        },
        "overlay_enabled": False,
        "ko_control": "ARMED",
        "recent_events": [{"seq": 7, "kind": "DAMAGE", "slot": "P2-C1",
                           "action": "0x0100", "damage": 720, "combo": 1}],
    })
    assert "MEM2 process: PID 12292" in report
    assert "DME's MEM1 process cannot be identified" in report
    assert "Overlay: OFF" in report
    assert "KO Control: ARMED" in report
    assert "#7 DAMAGE P2-C1" in report


def test_main_opens_gui_before_background_attach():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    startup = source.split("def legacy_main():", 1)[1].split("    # Saved frame-data patches", 1)[0]
    assert startup.index("init_pygame()") < startup.index("attach_thread.start()")
    assert "hook()\n" not in startup
