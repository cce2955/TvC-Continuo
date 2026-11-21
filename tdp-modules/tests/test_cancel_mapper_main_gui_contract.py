from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_main_gui_has_cancel_mapper_tool_button_and_click_route():
    main_text = (ROOT / "main.py").read_text(encoding="utf-8")
    components_text = (ROOT / "tvcgui" / "ui" / "components.py").read_text(encoding="utf-8")

    assert "cancel_mapper_btn_rect" in main_text
    assert "cancel_lab_btn_rect" in main_text
    assert "elif dock.cancel_mapper_btn_rect.collidepoint(mx, my):" in main_text
    assert "elif dock.cancel_lab_btn_rect.collidepoint(mx, my):" in main_text
    assert "open_cancel_mapper_window" in main_text
    assert "open_cancel_lab_window" in main_text
    assert '"Cancel Mapper"' in components_text
    assert '"Cancel Lab"' in components_text
    assert '"TRAINING"' in components_text
    assert '"LAB / SETUP"' in components_text
    assert "TOP_UI_RESERVED = 94" in components_text


def test_standalone_mapper_and_loading_shell_are_available():
    mapper_text = (ROOT / "tvcgui" / "features" / "frame_data" / "cancel_mapper.py").read_text(encoding="utf-8")
    window_text = (ROOT / "tvcgui" / "features" / "frame_data" / "window.py").read_text(encoding="utf-8")

    assert "def open_standalone_cancel_mapper(" in mapper_text
    assert "Fighter" in mapper_text
    assert "Open Live Cancel Lab" not in mapper_text
    assert "Test this route in Live Cancel Lab" not in mapper_text
    assert "def open_cancel_mapper_window(" in window_text
    assert "def open_cancel_mapper_loading_window(" in window_text
    assert "def close_cancel_mapper_loading_window(" in window_text
    assert "def open_cancel_lab_window(" in window_text
    assert "def open_cancel_lab_loading_window(" in window_text
    assert "def close_cancel_lab_loading_window(" in window_text
