from __future__ import annotations

from tvcgui.ui import hud_settings
from tvcgui.ui.components import _command_dock_layout
from tvcgui.features.overlay import customization
from tvcgui.features.overlay import master_renderer


def test_named_hud_preset_round_trips_switches_and_appearance(tmp_path):
    path = tmp_path / "hud_presets.json"
    hud = {key: key in ("show_damage_badge", "show_tag_card") for key in hud_settings.HUD_KEYS}
    appearance = {"p1_color": "#123456", "opacity": 0.6, "positions": {"P1": [0.2, 0.3]}}
    hud_settings.save_preset("Tournament", hud, appearance, path=path)
    saved = hud_settings.load_presets(path)["Tournament"]
    assert {key: saved["hud"][key] for key in hud_settings.HUD_KEYS} == hud
    assert all(saved["hud"][key] for key in hud_settings.VISIBLE_KEYS)
    assert saved["overlay"]["p1_color"] == "#123456"
    assert saved["overlay"]["opacity"] == 0.6
    assert saved["overlay"]["positions"]["P1"] == [0.2, 0.3]
    hud_settings.delete_preset("Tournament", path=path)
    assert hud_settings.load_presets(path) == {}


def test_bad_preset_name_cannot_become_a_path(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        hud_settings.save_preset("../outside", {}, path=tmp_path / "hud_presets.json")


def test_top_hud_row_has_only_the_four_requested_actions():
    sections, _height = _command_dock_layout(1000, False)
    assert list(sections["hud"][1]) == [
        "hud_btn", "info_set_btn", "settings_btn", "custom_music",
    ]
    assert "tools_btn" in sections["visuals"][1]


def test_more_button_stays_right_of_visual_controls():
    for width in (320, 600, 1000, 1920):
        sections, _height = _command_dock_layout(width, False)
        section, controls = sections["visuals"]
        more = controls["tools_btn"]
        assert more.right <= section.right
        assert all(not more.colliderect(rect) for key, rect in controls.items() if key != "tools_btn")
        assert more.right >= section.right - 10


def test_hud_switches_update_visible_rows_and_override(monkeypatch):
    saved = []
    monkeypatch.setattr(customization, "load", lambda: customization.normalize({}))
    monkeypatch.setattr(customization, "save", lambda data: saved.append(customization.normalize(data)) or saved[-1])
    hud_settings.save_visible_settings({
        "show_health": False, "show_inputs": False, "show_meter_rail": False,
        "show_move_timing": False, "show_untech_panel": False,
        "show_damage_badge": False,
    })
    result = saved[-1]
    assert all(result[key] is False for key in (*hud_settings.VISIBLE_KEYS, "show_stun", "show_damage_scaling"))


def test_overlay_retries_control_file_after_incomplete_read(tmp_path, monkeypatch):
    path = tmp_path / "master_overlay_control.json"
    monkeypatch.setattr(master_renderer, "MASTER_CONTROL_FILE", str(path))
    overlay = object.__new__(master_renderer.MasterOverlay)
    overlay.control = master_renderer.MasterControl()
    overlay._last_control_mtime = 0
    path.write_text('{"show_meter_panel":', encoding="utf-8")
    overlay._read_control_file()
    assert overlay._last_control_mtime == 0
    path.write_text('{"hud_info_set":"CUSTOM","show_meter_panel":false}', encoding="utf-8")
    overlay._read_control_file()
    assert overlay.control.show_meter_panel is False
