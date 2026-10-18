from __future__ import annotations

import os
import sys
import types
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.modules.setdefault("win32con", types.SimpleNamespace())
sys.modules.setdefault("win32gui", types.SimpleNamespace())

import pygame

from tests.v19_contract_helpers import function_source, read
from tvcgui.features.overlay import master_renderer as mission_hud

MASTER = "tvcgui/features/overlay/master_renderer.py"
DUPLICATE = "tdp-modules/tvcgui/features/overlay/master_renderer.py"


class V19MissionRedesignContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()
        pygame.font.init()

    @staticmethod
    def _steps() -> list[dict]:
        return [
            {"display": "5A", "input": "A", "color": "blue"},
            {"display": "5B", "input": "B", "color": "yellow"},
            {"display": "2B", "input": "2 B", "color": "yellow"},
            {"display": "5C", "input": "C", "color": "green"},
            {"display": "6C", "input": "6 C", "color": "green"},
            {"display": "Hado L", "input": "2 3 6 A", "color": "blue"},
        ]

    @classmethod
    def _overlay(cls, intro: float = 1.0) -> mission_hud.MasterOverlay:
        overlay = mission_hud.MasterOverlay()
        overlay.w = 1280
        overlay.h = 720
        overlay.screen = pygame.Surface((1280, 720), pygame.SRCALPHA)
        overlay.font = pygame.font.SysFont("consolas", 22, bold=True)
        overlay.smallfont = pygame.font.SysFont("consolas", 16)
        overlay.mission_active = True
        overlay.mission_slot = "P1-C1"
        overlay._mission_intro_phase = intro
        return overlay

    @classmethod
    def _draw_panel(
        cls,
        intro: float = 1.0,
        completed: int = 1,
        current: int = 1,
        pulse: bool = False,
        reduce_motion: bool = False,
        theme_color: tuple[int, int, int] = (76, 139, 228),
    ) -> mission_hud.MasterOverlay:
        overlay = cls._overlay(intro)
        overlay.control.reduce_mission_motion = reduce_motion
        if pulse:
            overlay._row_bump[0] = 1.0
        steps = cls._steps()
        data = {
            "active_mission_id": "ryu-c1",
            "active_mission_name": "Challenge",
            "active_mission_notes": "Complete the sequence cleanly.",
            "selector_hint": "Down, Down, Taunt: Open Mission",
            "active_mission_steps": steps,
            "completed_step_count": completed,
            "current_step_index": current,
        }
        for index in range(len(steps)):
            overlay.step_anim[index] = 0.0 if index < completed else (1.0 if index == current else 0.0)
        overlay._draw_active_mission_panel(
            data=data,
            character="Ryu",
            theme_color=theme_color,
            mission_name="Challenge",
            mission_notes="Complete the sequence cleanly.",
            steps=steps,
            selector_hint="Down, Down, Taunt: Open Mission",
            goal_progress_type=None,
            goal_target_state=None,
            goal_current_frames=0,
            goal_needed_frames=0,
            goal_timer_active=False,
        )
        return overlay

    def test_intro_ease_is_clamped_and_monotonic(self):
        self.assertEqual(mission_hud._mission_intro_ease(-1.0), 0.0)
        self.assertEqual(mission_hud._mission_intro_ease(2.0), 1.0)
        self.assertLess(mission_hud._mission_intro_ease(0.2), mission_hud._mission_intro_ease(0.8))

    def test_panel_intro_reaches_full_opacity(self):
        self.assertEqual(mission_hud._mission_panel_intro_progress(0.0), 0.0)
        self.assertEqual(mission_hud._mission_panel_intro_progress(1.0), 1.0)

    def test_rows_enter_in_staggered_order(self):
        first = mission_hud._mission_row_intro_progress(0.20, 0)
        second = mission_hud._mission_row_intro_progress(0.20, 1)
        third = mission_hud._mission_row_intro_progress(0.20, 2)
        self.assertGreater(first, second)
        self.assertGreater(second, third)

    def test_all_rows_settle_after_intro(self):
        for row in range(6):
            self.assertEqual(mission_hud._mission_row_intro_progress(2.0, row), 1.0)

    def test_complete_plate_progress_locks_and_sheens(self):
        alpha, lock, sheen = mission_hud._mission_complete_plate_progress(0.65)
        self.assertGreater(alpha, 0.9)
        self.assertGreater(lock, 0.9)
        self.assertGreater(sheen, 0.0)
        self.assertLess(sheen, 1.0)

    def test_complete_plate_exits_cleanly(self):
        alpha, _lock, _sheen = mission_hud._mission_complete_plate_progress(2.80)
        self.assertEqual(alpha, 0.0)

    def test_panel_uses_wider_responsive_layout(self):
        source = function_source(MASTER, "_draw_active_mission_panel")
        self.assertIn("panel_w = max(520", source)
        self.assertIn("int(self.w * 0.43)", source)
        self.assertIn(", 760)", source)

    def test_panel_uses_flat_angular_body(self):
        source = function_source(MASTER, "_draw_active_mission_panel")
        self.assertIn("panel_points = [", source)
        self.assertIn("pygame.draw.polygon(panel", source)
        self.assertNotIn("_draw_vertical_gradient", source)
        self.assertNotIn("scan_period", source)

    def test_progress_pip_curve_starts_genuinely_dim(self):
        self.assertLess(mission_hud._mission_pip_intensity(0, 12), 0.20)

    def test_progress_pip_curve_reaches_full_intensity_at_the_end(self):
        self.assertAlmostEqual(mission_hud._mission_pip_intensity(11, 12), 1.0, places=5)

    def test_progress_pip_curve_accelerates_toward_last_pips(self):
        first_gap = (
            mission_hud._mission_pip_intensity(2, 12)
            - mission_hud._mission_pip_intensity(1, 12)
        )
        last_gap = (
            mission_hud._mission_pip_intensity(11, 12)
            - mission_hud._mission_pip_intensity(10, 12)
        )
        self.assertGreater(last_gap, first_gap * 4.0)

    def test_progress_pip_fill_ratio_controls_partial_light_up(self):
        full = mission_hud._mission_pip_intensity(8, 12, 1.0)
        half = mission_hud._mission_pip_intensity(8, 12, 0.5)
        self.assertAlmostEqual(half, full * 0.5, places=5)

    def test_progress_stage_brightness_is_shared_by_completed_pips(self):
        second = mission_hud._mission_pip_stage_intensity(2, 10)
        third = mission_hud._mission_pip_stage_intensity(3, 10)
        fourth = mission_hud._mission_pip_stage_intensity(4, 10)
        self.assertGreater(third, second)
        self.assertGreater(fourth, third)

    def test_progress_wave_is_staggered(self):
        first = mission_hud._mission_pip_wave_progress(0.10, 0)
        second = mission_hud._mission_pip_wave_progress(0.10, 1)
        third = mission_hud._mission_pip_wave_progress(0.10, 2)
        self.assertGreater(first, second)
        self.assertGreater(second, third)

    def test_newly_filled_pip_waits_for_lock_then_starts_sheen(self):
        overlay = self._overlay()
        overlay.mission_overlay_data = {
            "active_mission_id": "ryu-c1",
            "active_mission_steps": self._steps(),
            "completed_step_count": 0,
            "current_step_index": 0,
        }
        overlay.update_mission_animations(0.016)
        overlay.mission_overlay_data["completed_step_count"] = 1
        overlay.update_mission_animations(0.05)
        self.assertEqual(overlay._mission_pip_sheen_pending_index, 0)
        for _ in range(4):
            overlay.update_mission_animations(0.05)
        self.assertEqual(overlay._mission_pip_sheen_pending_index, -1)
        self.assertEqual(overlay._mission_pip_sheen_index, 0)
        self.assertLess(overlay._mission_pip_sheen_phase, 1.0)

    def test_final_completion_arms_full_strip_sheen(self):
        overlay = self._overlay()
        overlay.mission_overlay_data = {
            "active_mission_id": "ryu-c1",
            "active_mission_steps": self._steps(),
            "completed_step_count": len(self._steps()) - 1,
            "current_step_index": len(self._steps()) - 1,
        }
        overlay.update_mission_animations(0.016)
        overlay.mission_overlay_data["completed_step_count"] = len(self._steps())
        for _ in range(20):
            overlay.update_mission_animations(0.05)
        self.assertLess(overlay._mission_strip_complete_sheen_phase, 1.0)

    def test_newest_pip_enters_at_previous_stage_then_all_brighten_left_to_right(self):
        overlay = self._overlay()
        overlay.mission_overlay_data = {
            "active_mission_id": "ryu-c1",
            "active_mission_steps": self._steps(),
            "completed_step_count": 2,
            "current_step_index": 2,
        }
        overlay.update_mission_animations(0.016)
        previous_stage = mission_hud._mission_pip_stage_intensity(2, len(self._steps()))
        target_stage = mission_hud._mission_pip_stage_intensity(3, len(self._steps()))
        overlay.mission_overlay_data["completed_step_count"] = 3
        overlay.update_mission_animations(0.001)
        self.assertAlmostEqual(overlay._mission_pip_levels[2], previous_stage, places=4)
        self.assertGreaterEqual(overlay._mission_pip_levels[0], previous_stage)
        overlay.update_mission_animations(0.06)
        self.assertGreater(overlay._mission_pip_levels[0], previous_stage)
        self.assertLess(overlay._mission_pip_levels[0], target_stage)
        self.assertLessEqual(overlay._mission_pip_levels[1], overlay._mission_pip_levels[0])
        self.assertLessEqual(overlay._mission_pip_levels[2], overlay._mission_pip_levels[1])
        overlay.update_mission_animations(0.06)
        self.assertGreaterEqual(overlay._mission_pip_levels[1], previous_stage)
        self.assertLessEqual(overlay._mission_pip_levels[2], overlay._mission_pip_levels[1])
        overlay.update_mission_animations(0.08)
        self.assertGreater(overlay._mission_pip_levels[2], previous_stage)
        for _ in range(20):
            overlay.update_mission_animations(0.05)
        for index in range(3):
            self.assertAlmostEqual(overlay._mission_pip_levels[index], target_stage, places=4)

    def test_failure_clears_pending_sheen_and_powers_pips_down(self):
        overlay = self._overlay()
        overlay.mission_overlay_data = {
            "active_mission_id": "ryu-c1",
            "active_mission_steps": self._steps(),
            "completed_step_count": 3,
            "current_step_index": 3,
        }
        overlay.update_mission_animations(0.016)
        overlay.mission_overlay_data["completed_step_count"] = 0
        overlay.update_mission_animations(0.05)
        self.assertEqual(overlay._mission_pip_sheen_pending_index, -1)
        self.assertEqual(overlay._mission_pip_sheen_index, -1)
        self.assertLess(overlay._mission_progress_display, 3.0)

    def test_header_has_segmented_progress_strip(self):
        source = function_source(MASTER, "_draw_active_mission_panel")
        self.assertIn("segment_count = max(1, total_steps)", source)
        self.assertIn("for seg in range(segment_count)", source)
        self.assertIn('progress_label = self.smallfont.render("COMPLETED"', source)
        self.assertIn("progress_display = max(0.0", source)
        self.assertIn("self._mission_progress_display", source)
        self.assertIn("_mission_pip_stage_intensity", source)
        self.assertIn("_mission_pip_sheen_progress", source)
        self.assertIn("pygame.BLEND_RGBA_ADD", source)

    def test_completed_challenge_moves_badge_to_progress_strip(self):
        source = function_source(MASTER, "_draw_active_mission_panel")
        self.assertIn("mission_done = total_steps > 0", source)
        self.assertIn('badge_text = badge_font.render("MISSION COMPLETE"', source)
        self.assertIn("strip_rect = pygame.Rect", source)
        self.assertIn("complete_level = pip_levels[-1]", source)
        self.assertIn("compact_complete = bool(mission_done and (self._mission_hold_frames > 0 or self._toast_phase > 0.0))", source)
        self.assertNotIn('done_text = self.smallfont.render("DONE"', source)

    def test_completion_hold_duration_is_90_frames(self):
        source = read(MASTER)
        self.assertIn("self._mission_hold_duration_frames: int = 90", source)

    def test_completion_can_fall_back_without_token(self):
        source = function_source(MASTER, "update_mission_animations")
        self.assertIn("live_completed_count >= live_step_total", source)
        self.assertIn("self._prev_live_completed_count < live_step_total", source)

    def test_completion_hides_rows_within_panel(self):
        source = function_source(MASTER, "_draw_active_mission_panel")
        self.assertIn("if compact_complete:", source)
        self.assertIn('title_font.render("MISSION COMPLETE"', source)
        self.assertIn("compact_complete = bool(mission_done and (self._mission_hold_frames > 0 or self._toast_phase > 0.0))", source)
        self.assertIn("for display_order, (idx, step) in enumerate(visible_steps):", source)

    def test_completion_overlay_uses_slide_fade_lock_and_sheen_phases(self):
        source = function_source(MASTER, "_draw_active_mission_panel")
        self.assertIn("_mission_complete_badge_progress", source)
        self.assertIn("badge_alpha, slide_progress, title_lock, sheen_progress", source)
        self.assertIn("pygame.transform.smoothscale", source)
        self.assertIn("pygame.BLEND_RGBA_ADD", source)

    def test_completion_badge_phase_enters_holds_and_exits(self):
        start = mission_hud._mission_complete_badge_progress(0, 90)
        entered = mission_hud._mission_complete_badge_progress(14, 90)
        held = mission_hud._mission_complete_badge_progress(45, 90)
        exiting = mission_hud._mission_complete_badge_progress(86, 90)
        self.assertEqual(start[0], 0.0)
        self.assertGreater(entered[0], 0.9)
        self.assertGreater(held[0], 0.9)
        self.assertLess(exiting[0], held[0])
        self.assertGreater(held[3], 0.0)

    def test_completion_badge_draws_gradient_wings_and_glints(self):
        source = function_source(MASTER, "_draw_active_mission_panel")
        self.assertIn("for row_y in range(height)", source)
        self.assertIn("wing_offset = int(round((1.0 - slide_progress) * 34.0))", source)
        self.assertIn("Small deterministic glints", source)

    def test_hint_fold_animates_from_bottom_of_main_panel(self):
        source = function_source(MASTER, "_draw_active_mission_panel")
        update_source = function_source(MASTER, "update_mission_animations")
        self.assertIn("self._mission_hint_fold", update_source)
        self.assertIn("fold_y = panel_y + panel_h - 1", source)
        self.assertIn('hint_label = self.smallfont.render("HINT"', source)
        self.assertNotIn("panel_h = pad + header_h + 7 + challenge_h + 8 + timer_h + note_h", source)

    def test_empty_authored_hint_still_folds_out_with_fallback_text(self):
        overlay = self._overlay()
        overlay.mission_show_hint = True
        overlay.mission_overlay_data = {
            "active_mission_id": "ryu-c1",
            "active_mission_steps": self._steps(),
            "active_mission_notes": "",
            "completed_step_count": 0,
            "current_step_index": 0,
        }
        overlay.update_mission_animations(0.10)
        self.assertGreater(overlay._mission_hint_fold, 0.0)
        source = function_source(MASTER, "_draw_active_mission_panel")
        self.assertIn("No hint text is available for this challenge.", source)

    def test_open_hint_extends_panel_rect_below_main_box(self):
        steps = self._steps()
        base = self._overlay()
        base._mission_hint_fold = 0.0
        base._draw_active_mission_panel(
            data={"completed_step_count": 0, "current_step_index": 0},
            character="Ryu",
            theme_color=(76, 139, 228),
            mission_name="Challenge",
            mission_notes="A real hint that should fold below the panel.",
            steps=steps,
            selector_hint="Down, Down, Taunt: Open Mission",
            goal_progress_type=None,
            goal_target_state=None,
            goal_current_frames=0,
            goal_needed_frames=0,
            goal_timer_active=False,
        )
        closed_height = base.mission_panel_rect.height

        opened = self._overlay()
        opened.mission_show_hint = True
        opened._mission_hint_fold = 1.0
        opened._draw_active_mission_panel(
            data={"completed_step_count": 0, "current_step_index": 0},
            character="Ryu",
            theme_color=(76, 139, 228),
            mission_name="Challenge",
            mission_notes="A real hint that should fold below the panel.",
            steps=steps,
            selector_hint="Down, Down, Taunt: Open Mission",
            goal_progress_type=None,
            goal_target_state=None,
            goal_current_frames=0,
            goal_needed_frames=0,
            goal_timer_active=False,
        )
        self.assertGreater(opened.mission_panel_rect.height, closed_height)

    def test_large_centered_completion_plate_is_not_invoked(self):
        update_source = function_source(MASTER, "update_mission_animations")
        run_source = function_source(MASTER, "run")
        self.assertNotIn("self._trigger_celebration()", update_source)
        self.assertNotIn("self.draw_celebration()", run_source)

    def test_completed_steps_keep_visible_done_chip(self):
        source = function_source(MASTER, "_draw_active_mission_panel")
        self.assertIn('done = self.smallfont.render("DONE"', source)
        self.assertIn("readable afterward so completed work is still obvious", source)

    def test_current_step_gets_current_chip(self):
        source = function_source(MASTER, "_draw_active_mission_panel")
        self.assertIn('current = self.smallfont.render("CURRENT"', source)
        self.assertIn("is_current = idx == current_index and not mission_done", source)

    def test_input_notation_is_preserved_on_redesigned_rows(self):
        source = function_source(MASTER, "_draw_active_mission_panel")
        self.assertIn("self._render_mission_input_notation(input_text, accent)", source)

    def test_rows_use_slide_and_fade_together(self):
        source = function_source(MASTER, "_draw_active_mission_panel")
        self.assertIn("row_shift = int(round((1.0 - row_intro) * 32.0))", source)
        self.assertIn("row_layer.set_alpha(int(255 * row_intro))", source)

    def test_progress_strip_animation_tracks_completion_changes(self):
        overlay = self._overlay()
        overlay.mission_overlay_data = {
            "active_mission_id": "ryu-c1",
            "active_mission_steps": self._steps(),
            "completed_step_count": 0,
            "current_step_index": 0,
        }
        overlay.update_mission_animations(0.016)
        overlay.mission_overlay_data["completed_step_count"] = 3
        overlay.update_mission_animations(0.05)
        self.assertGreater(overlay._mission_progress_display, 0.0)
        self.assertLess(overlay._mission_progress_display, 3.1)
        overlay.mission_overlay_data["completed_step_count"] = 1
        overlay.update_mission_animations(0.05)
        self.assertGreater(overlay._mission_progress_display, 0.0)

    def test_step_pulse_waits_for_confirmation(self):
        overlay = self._overlay()
        overlay.mission_overlay_data = {
            "active_mission_id": "ryu-c1",
            "active_mission_steps": self._steps(),
            "completed_step_count": 0,
            "confirmed_step_count": 0,
            "current_step_index": 0,
        }
        overlay.update_mission_animations(0.016)
        overlay.mission_overlay_data.update(completed_step_count=1, current_step_index=1)
        overlay.update_mission_animations(0.016)
        self.assertNotIn(0, overlay._row_bump)
        overlay.mission_overlay_data["confirmed_step_count"] = 1
        overlay.update_mission_animations(0.016)
        self.assertGreater(overlay._row_bump[0], 0.0)

    def test_reduced_motion_snaps_mission_visuals(self):
        overlay = self._overlay()
        overlay.control.reduce_mission_motion = True
        overlay.mission_overlay_data = {
            "active_mission_id": "ryu-c1",
            "active_mission_steps": self._steps(),
            "completed_step_count": 1,
            "confirmed_step_count": 1,
            "current_step_index": 1,
            "selector_open": True,
            "selector_index": 1,
        }
        overlay.update_mission_animations(0.016)
        self.assertEqual(overlay._mission_intro_phase, 2.0)
        self.assertEqual(overlay._mission_selector_open_phase, 1.0)
        self.assertEqual(overlay._mission_selector_move_age, 1.0)
        self.assertEqual(overlay.step_anim[1], 1.0)
        self.assertFalse(overlay._row_bump)

    def test_confirmation_outline_renders_and_reduced_motion_hides_it(self):
        plain = pygame.surfarray.array3d(self._draw_panel().screen)
        pulsed = pygame.surfarray.array3d(self._draw_panel(pulse=True).screen)
        reduced = pygame.surfarray.array3d(self._draw_panel(pulse=True, reduce_motion=True).screen)
        self.assertGreater(int((pulsed != plain).sum()), 0)
        self.assertEqual(int((reduced != plain).sum()), 0)

    def test_trial_panel_tint_changes_with_fighter(self):
        ryu = pygame.surfarray.array3d(self._draw_panel(theme_color=(170, 30, 30)).screen)
        alex = pygame.surfarray.array3d(self._draw_panel(theme_color=(40, 120, 255)).screen)
        self.assertGreater(int((ryu != alex).sum()), 100)

    def test_trial_top_strip_shows_fighter_color(self):
        ryu = self._draw_panel(intro=2.0, theme_color=(170, 30, 30))
        alex = self._draw_panel(intro=2.0, theme_color=(40, 120, 255))
        for overlay, dominant in ((ryu, 0), (alex, 2)):
            rect = overlay.mission_panel_rect
            pixel = overlay.screen.get_at((rect.centerx, rect.y + 4))
            self.assertGreater(pixel[dominant], max(pixel[(dominant + 1) % 3], pixel[(dominant + 2) % 3]))

    def test_mission_portrait_is_cached_and_feathered(self):
        overlay = self._overlay()
        portrait = overlay._mission_portrait_watermark("Ryu", 68)
        self.assertIsNotNone(portrait)
        self.assertIs(portrait, overlay._mission_portrait_watermark("Ryu", 68))
        self.assertEqual(portrait.get_at((0, 0)).a, 0)
        self.assertGreater(portrait.get_at((34, 34)).a, 20)
        self.assertIsNone(overlay._mission_portrait_watermark("Unknown Fighter", 68))

    def test_mission_select_tint_changes_with_fighter(self):
        def render(character: str):
            overlay = self._overlay(intro=2.0)
            overlay._mission_selector_open_phase = 1.0
            overlay.mission_overlay_data = {
                "character": character,
                "slot": "P1-C1",
                "selector_open": True,
                "selector_index": 0,
                "missions": [{"mission_id": "one", "name": "Challenge", "completed": False}],
            }
            overlay.draw_mission_overlay()
            return overlay

        ryu = render("Ryu")
        alex = render("Alex")
        self.assertGreater(int((pygame.surfarray.array3d(ryu.screen) != pygame.surfarray.array3d(alex.screen)).sum()), 100)
        for overlay, dominant in ((ryu, 0), (alex, 2)):
            rect = overlay.mission_panel_rect
            pixel = overlay.screen.get_at((rect.centerx, rect.y + 4))
            self.assertGreater(pixel[dominant], max(pixel[(dominant + 1) % 3], pixel[(dominant + 2) % 3]))

    def test_mission_hides_when_live_fighter_unloads(self):
        overlay = self._overlay(intro=2.0)
        overlay.mission_overlay_data = {
            "active": True,
            "character": "Alex",
            "fighter_loaded": False,
            "active_mission_steps": self._steps(),
            "active_mission_id": "alex-1",
        }
        overlay.draw_mission_overlay()
        self.assertIsNone(overlay.mission_panel_rect)
        overlay.mission_overlay_data["fighter_loaded"] = True
        overlay.draw_mission_overlay()
        self.assertIsNotNone(overlay.mission_panel_rect)

    def test_mission_draws_with_regular_overlay_off(self):
        source = function_source(MASTER, "run")
        hidden_branch = source.split("if not self.control.show_hud:", 1)[1].split("continue", 1)[0]
        self.assertIn("self.draw_mission_overlay()", hidden_branch)
        self.assertIn("self.present()", hidden_branch)

    def test_activation_restarts_intro_sequence(self):
        overlay = mission_hud.MasterOverlay()
        overlay.mission_active = True
        overlay.mission_slot = "P1-C1"
        overlay.mission_overlay_data = {
            "active_mission_id": "mission-a",
            "active_mission_steps": self._steps(),
            "completed_step_count": 0,
            "current_step_index": 0,
        }
        overlay.update_mission_animations(0.016)
        self.assertEqual(overlay._mission_intro_generation, 1)
        self.assertEqual(overlay._mission_intro_phase, 0.0)
        overlay.update_mission_animations(0.10)
        self.assertGreater(overlay._mission_intro_phase, 0.0)

    def test_changing_mission_restarts_intro_sequence(self):
        overlay = mission_hud.MasterOverlay()
        overlay.mission_active = True
        overlay.mission_slot = "P1-C1"
        overlay.mission_overlay_data = {
            "active_mission_id": "mission-a",
            "active_mission_steps": self._steps(),
            "completed_step_count": 0,
            "current_step_index": 0,
        }
        overlay.update_mission_animations(0.016)
        overlay.update_mission_animations(0.10)
        overlay.mission_overlay_data["active_mission_id"] = "mission-b"
        overlay.update_mission_animations(0.016)
        self.assertEqual(overlay._mission_intro_generation, 2)
        self.assertEqual(overlay._mission_intro_phase, 0.0)

    def test_disabling_mission_resets_intro_state(self):
        overlay = mission_hud.MasterOverlay()
        overlay.mission_active = False
        overlay._mission_intro_phase = 1.0
        overlay._mission_intro_was_active = True
        overlay.update_mission_animations(0.016)
        self.assertFalse(overlay._mission_intro_was_active)
        self.assertEqual(overlay._mission_intro_phase, 0.0)

    def test_rendered_panel_is_wider_and_buttons_stay_inside(self):
        overlay = self._draw_panel()
        self.assertIsNotNone(overlay.mission_panel_rect)
        self.assertGreaterEqual(overlay.mission_panel_rect.width, 520)
        self.assertLessEqual(overlay.mission_panel_rect.width, 760)
        self.assertTrue(overlay.mission_panel_rect.contains(overlay.mission_toggle_rect))
        self.assertTrue(overlay.mission_panel_rect.contains(overlay.mission_hint_rect))

    def test_intro_render_has_less_visible_content_than_settled_render(self):
        early = self._draw_panel(intro=0.18)
        settled = self._draw_panel(intro=1.0)
        early_alpha = pygame.surfarray.array_alpha(early.screen)
        settled_alpha = pygame.surfarray.array_alpha(settled.screen)
        self.assertLess(int(early_alpha.sum()), int(settled_alpha.sum()))

    def test_completed_row_render_retains_green_confirmation_pixels(self):
        overlay = self._draw_panel(completed=1, current=1)
        pixels = pygame.surfarray.array3d(overlay.screen)
        green_pixels = (pixels[:, :, 1] > pixels[:, :, 0] + 25) & (pixels[:, :, 1] > pixels[:, :, 2] + 5)
        self.assertGreater(int(green_pixels.sum()), 20)

    def test_completion_graphic_is_centered_and_angular(self):
        source = function_source(MASTER, "draw_celebration")
        self.assertIn('title_font.render("MISSION COMPLETE"', source)
        self.assertIn("plate_x = (self.w - plate_w) // 2", source)
        self.assertIn("body = [", source)
        self.assertIn("Side brackets slide inward", source)

    def test_completion_graphic_uses_flat_low_gloss_palette(self):
        source = function_source(MASTER, "draw_celebration")
        self.assertIn("(9, 18, 34", source)
        self.assertIn("(48, 67, 98", source)
        self.assertNotIn("ring_radius", source)
        self.assertNotIn("draw_lightning", source)

    def test_completion_graphic_has_one_additive_sheen_pass(self):
        source = function_source(MASTER, "draw_celebration")
        self.assertIn("One explicit sheen pass", source)
        self.assertIn("pygame.BLEND_RGBA_ADD", source)

    def test_completion_particles_do_not_use_old_gold_palette(self):
        source = function_source(MASTER, "_trigger_celebration")
        self.assertIn("palette = (", source)
        self.assertNotIn("(255, 225, 90)", source)
        self.assertNotIn("(255, 220, 60)", source)

    def test_mission_selector_still_marks_completed_challenges(self):
        source = function_source(MASTER, "draw_mission_overlay")
        self.assertIn('suffix = " [done]" if completed else ""', source)

    def test_primary_and_duplicate_master_renderers_match(self):
        self.assertEqual(read(MASTER), read(DUPLICATE))


if __name__ == "__main__":
    unittest.main()
