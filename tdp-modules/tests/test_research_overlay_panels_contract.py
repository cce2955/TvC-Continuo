from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_master_control_persists_all_research_panel_flags() -> None:
    text = _read("tvcgui/features/overlay/master_renderer.py")
    for token in (
        "show_damage_badge",
        "show_meter_panel",
        "show_red_health_panel",
        "show_attack_property_panel",
    ):
        assert f"{token}: bool" in text
        assert f'"{token}": self.control.{token}' in text
        assert f'data.get("{token}"' in text


def test_main_dock_has_independent_native_research_buttons_and_clear_behavior() -> None:
    text = _read("main.py")
    for token in (
        "meter_panel_btn_rect",
        "red_health_panel_btn_rect",
        "attack_property_panel_btn_rect",
    ):
        assert token in text
    assert "show_damage_badge = not show_damage_badge" in text
    assert "show_meter_panel = not show_meter_panel" in text
    assert "show_red_health_panel = not show_red_health_panel" in text
    assert "open_attack_resolver_window()" in text
    assert "show_damage_badge = False" in text
    assert "show_meter_panel = False" in text
    assert "show_red_health_panel = False" in text
    assert "show_attack_property_panel = False" in text


def test_lab_recorder_region_follows_drawn_layout(monkeypatch):
    import pygame
    from tvcgui.ui.components import _command_dock_layout, draw_top_command_dock

    monkeypatch.setattr(pygame.mouse, "get_pressed", lambda count: (False,) * count)
    pygame.font.init()
    surface = pygame.Surface((1400, 500))
    font = pygame.font.Font(None, 14)
    options = dict(hitbox_slots={}, hurtbox_slots={}, ruler_slots={}, ruler_axes={},
                   ruler_enabled=False, ruler_envelope_enabled=False, ruler_lock_active=False,
                   overlay_enabled=False, show_interaction_card=False, show_combo_card=False,
                   hud_info_set="CORE", show_damage_badge=False, show_untech_panel=False,
                   show_damage_inactive=False, show_meter_panel=False, show_red_health_panel=False,
                   show_attack_property_panel=False, show_tag_card=False, mouse_pos=(0, 0))
    layout, _ = _command_dock_layout(surface.get_width(), True)
    position = layout["lab"][1]["research_recorder"].center
    opened = draw_top_command_dock(surface, font, tools_open=True, **options)
    assert opened.research_recorder.collidepoint(position)
    assert not opened.action_recorder_btn_rect.collidepoint(position)
    closed = draw_top_command_dock(surface, font, tools_open=False, **options)
    assert not closed.research_recorder.collidepoint(position)


def test_overlay_payload_exports_profiler_fields() -> None:
    text = _read("tvcgui/features/overlay/manager.py")
    for token in (
        '"attack_property_live_a_text"',
        '"attack_property_live_b_text"',
        '"meter_profile_last_delta"',
        '"meter_profile_last_predicted"',
        '"red_health_recoverable"',
        '"red_health_pending_current"',
        '"red_health_last_event"',
    ):
        assert token in text


def test_hud_renderer_has_four_independent_research_panels() -> None:
    text = _read("tvcgui/features/overlay/hud_renderer.py")
    for function_name in (
        "_draw_damage_modifier_badge",
        "_draw_meter_generation_panel",
        "_draw_red_health_panel",
        "_draw_attack_property_panel",
        "_draw_research_panels",
    ):
        assert f"def {function_name}" in text
    assert 'getattr(control, "show_meter_panel", False)' in text
    assert 'getattr(control, "show_red_health_panel", False)' in text
    assert 'getattr(control, "show_attack_property_panel", False)' in text
    assert "if not hud_visible and not research_visible" in text


def test_profilers_publish_live_overlay_annotations() -> None:
    meter = _read("tvcgui/features/training/meter_generation_profiler.py")
    red = _read("tvcgui/features/training/red_health_profiler.py")
    attack = _read("tvcgui/features/training/attack_property_profiler.py")
    assert 'snap["meter_profile_last_delta"]' in meter
    assert 'snap["meter_profile_last_predicted"]' in meter
    assert 'snap["red_health_recoverable"]' in red
    assert 'snap["red_health_last_event"]' in red
    assert 'snap["attack_property_live_a_text"]' in attack
    assert 'snap["attack_property_live_b_text"]' in attack
