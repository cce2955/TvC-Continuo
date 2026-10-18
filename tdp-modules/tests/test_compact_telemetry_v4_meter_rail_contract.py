from pathlib import Path

HUD = Path(__file__).resolve().parents[1] / "tvcgui" / "features" / "overlay" / "hud_renderer.py"


def _body(name: str, next_name: str) -> str:
    source = HUD.read_text(encoding="utf-8")
    start = source.index(f"def {name}(")
    end = source.index(f"\ndef {next_name}", start)
    return source[start:end]


def test_meter_is_a_full_width_team_rail_below_character_rows():
    team = _body("_draw_compact_team_panel", "draw_overlay")
    assert "meter_rail_y = partner_hp_y" in team
    assert "damage_scale_y = meter_rail_y + meter_rail_layout_extra" in team
    assert "_draw_compact_meter_rail(" in team
    assert "_draw_compact_meter_inline(" not in team


def test_meter_rail_preserves_identity_value_stock_and_profile_delta():
    meter = _body("_draw_compact_meter_rail", "_draw_compact_guard_chip")
    assert 'font_sm.render("MTR"' in meter
    assert "_draw_compact_meter(" in meter
    assert "_compact_meter_text(meter_i)" in meter
    assert "stock_x = cursor_right - stock.get_width()" in meter
    assert 'snap.get("meter_profile_last_delta")' in meter


def test_character_name_gets_full_header_runway_after_meter_moves_out():
    team = _body("_draw_compact_team_panel", "draw_overlay")
    assert "name_available = max(36, right - name_x" in team
    assert "meter_left" not in team


def test_bbq_remains_character_owned_on_health_rows():
    team = _body("_draw_compact_team_panel", "draw_overlay")
    assert "point_bq_rect = pygame.Rect(point_bbq_x, top_hp_y" in team
    assert "partner_bq_rect = pygame.Rect(partner_bbq_x, bottom_hp_y" in team


def test_meter_rail_cost_is_included_in_panel_height():
    team = _body("_draw_compact_team_panel", "draw_overlay")
    assert "meter_rail_layout_extra = meter_rail_height + meter_rail_gap" in team
    assert "if not show_meter_rail:\n        meter_rail_layout_extra = 0" in team
    assert "+ meter_rail_layout_extra + frame_data_layout_extra" in team
