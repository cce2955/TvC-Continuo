from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "tvcgui" / "features" / "overlay" / "master_renderer.py"


def test_completion_badge_has_at_least_sixty_rendered_frames() -> None:
    source = RENDERER.read_text(encoding="utf-8")
    assert "MISSION_COMPLETE_HOLD_FRAMES = 60" in source
    assert "self._mission_hold_duration_frames: int = MISSION_COMPLETE_HOLD_FRAMES" in source


def test_short_missions_reserve_full_completion_badge_height() -> None:
    source = RENDERER.read_text(encoding="utf-8")
    assert "MISSION_COMPLETE_MIN_BODY_HEIGHT = 96" in source
    assert "route_body_h = max(route_body_h, MISSION_COMPLETE_MIN_BODY_HEIGHT)" in source
    assert "max(MISSION_COMPLETE_MIN_BODY_HEIGHT - 2, list_h + footer_h - 2)" in source


def test_completion_hold_survives_live_mission_clear() -> None:
    source = RENDERER.read_text(encoding="utf-8")
    assert "holding_completion = bool(" in source
    assert "if not holding_completion and (not self.mission_active or not self.mission_slot):" in source
    assert "data = self._mission_hold_data if holding_completion else display_payload" in source
    assert "if is_exiting or completion_hold_active:" in source
    assert 'transition_mode=("idle" if holding_completion else self._mission_transition_state)' in source


def test_live_payload_refresh_cannot_cancel_completion_hold() -> None:
    source = RENDERER.read_text(encoding="utf-8")
    stage_start = source.index("    def _stage_mission_overlay_payload")
    stage_end = source.index("    def _update_mission_transition", stage_start)
    stage_source = source[stage_start:stage_end]
    assert "self._mission_hold_frames = 0" not in stage_source
    assert "self._mission_hold_data = {}" not in stage_source
    assert "if previous_key != new_key and self._mission_hold_frames <= 0:" in stage_source
