from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pygame

from tvcgui.features.overlay import customization
from tvcgui.features.overlay import hud_renderer as hud
from tvcgui.features.overlay import master_renderer as master


def test_overlay_customization_saves_independent_visibility_colors_and_positions(tmp_path):
    path = tmp_path / "overlay.json"
    data = customization.load(path)
    assert data["show_health"] and data["show_stun"] and data["show_inputs"]
    data.update(show_health=True, show_stun=False, show_inputs=False,
                show_move_timing=False, show_damage_scaling=True,
                p1_color="#abcdef", opacity=0.65, edit_mode=True)
    data["positions"] = {"P1": [0.4, 0.7], "P2": [1, 0]}
    customization.save(data, path)
    saved = customization.load(path)
    assert saved["show_health"] and not saved["show_stun"]
    assert not saved["show_inputs"] and not saved["show_move_timing"]
    assert saved["show_damage_scaling"] is True
    assert saved["p1_color"] == "#abcdef"
    assert saved["opacity"] == 0.65
    assert saved["positions"] == {"P1": [0.4, 0.7], "P2": [1.0, 0.0]}
    assert saved["edit_mode"]


def test_overlay_customization_rejects_bad_colors_and_out_of_bounds_positions():
    clean = customization.normalize({
        "p1_color": "not a color", "p2_color": "#102030",
        "opacity": 9,
        "positions": {"P1": [-9, 7], "P2": ["bad", 0]},
    })
    assert clean["p1_color"] == customization.DEFAULTS["p1_color"]
    assert clean["p2_color"] == "#102030"
    assert clean["positions"] == {"P1": [0.0, 1.0]}
    assert clean["opacity"] == 1.0
    assert clean["show_damage_scaling"] is None


def test_compact_panel_respects_independent_visibility_and_position():
    pygame.font.init()
    prior = hud._anim_state
    hud._anim_state = {"overlay_alpha": 1.0, "slots": {}, "teams": {}}
    slots = {
        "P1-C1": {"name": "Ryu", "cur": 50000, "max": 50000, "meter": 10000},
        "P1-C2": {"name": "Chun-Li", "cur": 45000, "max": 50000, "meter": 10000},
    }
    control = SimpleNamespace(
        show_health=True, show_stun=False, show_inputs=False,
        show_move_timing=False, show_meter_rail=False,
        show_damage_scaling=True, show_damage_badge=False,
        show_untech_panel=True, show_meter_panel=False,
        show_red_health_panel=False, show_attack_property_panel=False,
        show_tag_card=False, show_combo_card=False, hud_info_set="CUSTOM",
        p1_color="#123456", positions={"P1": [0.5, 0.4]},
    )
    try:
        team = hud._get_team_anim("P1")
        team.update(present=True, alpha=1.0, entrance_active=False)
        for label in slots:
            hud._get_slot_anim(label)["present"] = True
        surface = pygame.Surface((1280, 720), pygame.SRCALPHA)
        font, small = pygame.font.Font(None, 24), pygame.font.Font(None, 17)
        with (
            mock.patch.object(hud, "_draw_compact_health") as health,
            mock.patch.object(hud, "_draw_compact_untech_scaling_row") as stun,
            mock.patch.object(hud, "_draw_compact_input_history") as inputs,
            mock.patch.object(hud, "_draw_compact_damage_scaling_rows") as damage,
        ):
            hud._draw_compact_team_panel(surface, font, small, "P1", slots, 1.0, 1.0, 1/60, control)
        assert health.call_count == 2
        stun.assert_not_called()
        inputs.assert_not_called()
        damage.assert_called_once()
        assert hud.EDITABLE_PANEL_RECTS["P1"].x > 0
        control.show_health = False
        with mock.patch.object(hud, "_draw_compact_health") as health:
            hud._draw_compact_team_panel(surface, font, small, "P1", slots, 1.0, 1.0, 1/60, control)
        health.assert_not_called()
    finally:
        hud._anim_state = prior


def test_edit_mode_drag_saves_normalized_panel_position():
    overlay = master.MasterOverlay()
    overlay.w, overlay.h = 1280, 720
    overlay.control.edit_mode = True
    overlay.control.positions = {}
    prior = dict(hud.EDITABLE_PANEL_RECTS)
    hud.EDITABLE_PANEL_RECTS.clear()
    hud.EDITABLE_PANEL_RECTS["P1"] = pygame.Rect(0, 0, 480, 250)
    events = [
        pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(10, 10)),
        pygame.event.Event(pygame.MOUSEMOTION, pos=(210, 160)),
        pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=(210, 160)),
    ]
    try:
        with (
            mock.patch.object(pygame.event, "get", return_value=events),
            mock.patch.object(customization, "load", return_value=customization.normalize({})),
            mock.patch.object(customization, "save") as save,
        ):
            overlay.handle_events()
        position = save.call_args.args[0]["positions"]["P1"]
        assert position == [200 / 800, 150 / 470]
        assert overlay._drag_team is None
    finally:
        hud.EDITABLE_PANEL_RECTS.clear()
        hud.EDITABLE_PANEL_RECTS.update(prior)
