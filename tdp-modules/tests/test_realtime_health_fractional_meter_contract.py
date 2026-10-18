from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUD = ROOT / "tvcgui" / "features" / "overlay" / "hud_renderer.py"
MANAGER = ROOT / "tvcgui" / "features" / "overlay" / "manager.py"
INPUT_MONITOR = ROOT / "tvcgui" / "runtime" / "input_monitor.py"
REALTIME_SAMPLER = ROOT / "tvcgui" / "runtime" / "realtime_sampler.py"


def _body(path: Path, name: str, next_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    start = source.index(f"def {name}(")
    end = source.index(f"\ndef {next_name}", start)
    return source[start:end]


def test_realtime_snapshot_exposes_team_meter_without_an_extra_primary_read():
    source = INPUT_MONITOR.read_text(encoding="utf-8")
    body = _body(INPUT_MONITOR, "read_overlay_input_packet", "direction_name")
    assert 'current_meter = blob_u32(0x4C) if label.endswith("-C1") else 0' in body
    assert 'aux_hp = blob_u32(0x2C)' in body
    assert '"current_meter": current_meter' in body
    # +0x4C lives inside the existing contiguous realtime_blob.
    primary = body.split("else:\n        # Fallback", 1)[0]
    assert "_read_u32(base + 0x4C)" not in primary



def test_realtime_sampler_preserves_meter_and_treats_meter_delta_as_meaningful():
    source = REALTIME_SAMPLER.read_text(encoding="utf-8")
    assert 'current_meter = max(0, int(packet.get("current_meter", 0) or 0))' in source
    assert 'meter_changed = previous is None or current_meter != int(previous_meter)' in source
    assert 'or meter_changed' in source
    assert '"current_meter": current_meter' in source

def test_realtime_combat_transport_updates_when_meter_or_hp_changes():
    source = MANAGER.read_text(encoding="utf-8")
    assert 'int(sample.get("current_hp", 0) or 0),' in source
    assert 'int(sample.get("current_meter", 0) or 0),' in source
    assert '"current_meter": int(sample.get("current_meter", 0) or 0),' in source
    assert '"seq", "sample_ns", "base", "char_id", "current_hp", "aux_hp", "current_meter", "action_id"' in source


def test_hud_merges_realtime_hp_and_team_meter_as_authoritative_resources():
    body = _body(HUD, "_merge_realtime_inputs", "make_font")
    assert 'snap["cur"] = live_hp' in body
    assert 'realtime_team_meter[team] = live_meter' in body
    assert 'snap["meter"] = live_meter' in body
    assert 'for suffix in ("C1", "C2")' in body
    assert "Do not age these values out on wall time" in body


def test_primary_health_fill_snaps_while_damage_trail_remains_animated():
    team = _body(HUD, "_draw_compact_team_panel", "draw_overlay")
    assert 'point_anim["hp_display_frac"] = point_hp_target' in team
    assert 'partner_anim["hp_display_frac"] = partner_hp_target' in team
    assert '_approach(point_anim["hp_display_frac"], point_hp_target' not in team
    assert '_approach(partner_anim["hp_display_frac"], partner_hp_target' not in team
    assert 'point_anim["hp_trail_frac"] = _approach' in team
    assert 'partner_anim["hp_trail_frac"] = _approach' in team


def test_primary_meter_value_snaps_to_realtime_value():
    team = _body(HUD, "_draw_compact_team_panel", "draw_overlay")
    assert 'point_anim["meter_display_value"] = point_meter_target' in team
    assert '_approach(point_anim["meter_display_value"], point_meter_target' not in team


def test_meter_cells_show_continuous_sub_stock_progress():
    meter = _body(HUD, "_draw_compact_meter", "_draw_compact_health")
    assert 'full_cells = min(5, int(meter_value // 10000.0))' in meter
    assert 'partial = 0.0 if full_cells >= 5 else (meter_value - full_cells * 10000.0) / 10000.0' in meter
    assert 'fill_fraction = 1.0 if index < full_cells else (partial if index == full_cells else 0.0)' in meter
    assert 'fill_w = max(1, min(cell.width, int(round(cell.width * fill_fraction))))' in meter
    assert '_compact_meter_gradient_cell' in meter
    assert 'source_rect = pygame.Rect(0, 0, min(fill_w_inner, gradient_cell.get_width()), fill_h_inner)' in meter
    assert 'if fill_fraction < 0.999 and fill_right < cell.right:' in meter
    # Fractional progress is static/game-state driven, not a wall-clock pulse.
    assert 'time.time()' not in meter


def test_meter_gradient_softly_crossfades_stock_colors():
    source = HUD.read_text(encoding="utf-8")
    assert 'def _compact_meter_gradient_color(position: float)' in source
    for stop in ('(0.18, (96, 170, 255))', '(0.22, (92, 214, 132))', '(0.42, (255, 226, 92))', '(0.62, (255, 162, 78))', '(0.82, (255, 92, 92))'):
        assert stop in source
