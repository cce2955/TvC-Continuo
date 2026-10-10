#!/usr/bin/env python3
"""
master_overlay.py
-----------------
Single Dolphin-parented transparent overlay window that will eventually host:
- HUD drawing
- hitbox drawing
- projectile drawing
- debug overlays

For now, this is the master shell only:
- one pygame window
- one Dolphin sync loop
- one event loop
- one flip per frame
- simple toggles for HUD / hitboxes
- optional control file so another process can toggle behavior later

This file is intentionally the first step only.
"""

from __future__ import annotations

import ctypes
import json
import math
import re
import os
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Optional, Protocol

import pygame
import win32con
import win32gui

from tvcgui.core.paths import user_data_path


TARGET_FPS = 60
COLORKEY = (0, 0, 0)

BASE_W = 1280
BASE_H = 720

MASTER_CONTROL_FILE = user_data_path("overlay", "master_overlay_control.json")
MISSION_MODE_FILE = user_data_path("training", "mission_mode_state.json")
MISSION_OVERLAY_FILE = user_data_path("training", "mission_overlay_data.json")
MISSION_SELECT_FILE = user_data_path("training", "mission_select_command.json")
MISSION_CELEBRATE_ACK_FILE = user_data_path("training", "mission_celebrate_ack.json")
CRASH_LOG_FILE = user_data_path("runtime", "master_overlay_crash.log")
YAMI_NAME_OVERLAY_FILE = user_data_path("character_select", "yami_name_overlay.json")

def _ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _mission_completion_ease(value: float) -> float:
    value = max(0.0, min(1.0, float(value)))
    return 1.0 - pow(1.0 - value, 3.0)


def _mission_scroll_ease(value: float) -> float:
    """Smooth a mission step viewport move without overshooting the target row."""
    value = max(0.0, min(1.0, float(value)))
    return value * value * value * (value * (value * 6.0 - 15.0) + 10.0)


def _mission_scroll_target(current_index: int, total_steps: int, visible_count: int = 6) -> int:
    """Keep the current mission step near the center of the collapsed viewport."""
    total_steps = max(0, int(total_steps))
    visible_count = max(1, int(visible_count))
    max_start = max(0, total_steps - visible_count)
    return min(max(0, int(current_index) - 2), max_start)


def _mission_intro_ease(value: float) -> float:
    """Fast, controlled mission-panel entrance easing with no permanent motion."""
    value = max(0.0, min(1.0, float(value)))
    return 1.0 - pow(1.0 - value, 3.0)


def _mission_panel_intro_progress(phase: float, duration: float = 0.30) -> float:
    if duration <= 0.0:
        return 1.0
    return _mission_intro_ease(float(phase) / float(duration))


def _mission_pip_intensity(index: int, total: int, fill_ratio: float = 1.0) -> float:
    """Return a deliberately back-loaded progress-pip brightness.

    Early pips remain only slightly brighter than the empty rail. The curve
    accelerates near the end so the final few pips become progressively more
    intense, with the last pip reaching full brightness.
    """
    total = max(1, int(total))
    index = max(0, min(total - 1, int(index)))
    fill_ratio = max(0.0, min(1.0, float(fill_ratio)))
    position = 1.0 if total <= 1 else float(index) / float(total - 1)
    return fill_ratio * (0.14 + 0.86 * pow(position, 2.4))


def _mission_pip_stage_intensity(completed: int, total: int) -> float:
    """Return the shared brightness for every completed pip at one route stage."""
    total = max(1, int(total))
    completed = max(0, min(total, int(completed)))
    if completed <= 0:
        return 0.0
    position = 1.0 if total <= 1 else float(completed - 1) / float(total - 1)
    return 0.08 + 0.92 * pow(position, 2.4)


def _mission_pip_wave_progress(
    elapsed: float,
    order_index: int,
    stagger: float = 0.055,
    duration: float = 0.18,
) -> float:
    """Stagger one cumulative pip catch-up step."""
    local = float(elapsed) - max(0, int(order_index)) * max(0.0, float(stagger))
    if duration <= 0.0:
        return 1.0 if local >= 0.0 else 0.0
    return _mission_intro_ease(local / float(duration))


def _mission_pip_sheen_progress(phase: float, duration: float = 0.38) -> float:
    """Move one short polish pass across a pip after it fully locks."""
    if duration <= 0.0:
        return 1.0
    return _mission_intro_ease(max(0.0, min(1.0, float(phase) / float(duration))))


def _mission_row_intro_progress(
    phase: float,
    row_order: int,
    stagger: float = 0.075,
    duration: float = 0.28,
) -> float:
    """Return the staggered slide-and-fade progress for one visible mission row."""
    local = float(phase) - max(0, int(row_order)) * max(0.0, float(stagger))
    if duration <= 0.0:
        return 1.0 if local >= 0.0 else 0.0
    return _mission_intro_ease(local / float(duration))


def _mission_element_intro_progress(
    phase: float,
    delay: float = 0.0,
    duration: float = 0.18,
) -> float:
    """Return a quick delayed fade-and-slide progress for one HUD element."""
    local = float(phase) - max(0.0, float(delay))
    if duration <= 0.0:
        return 1.0 if local >= 0.0 else 0.0
    return _mission_intro_ease(local / float(duration))


def _mission_element_exit_progress(
    progress: float,
    order: int = 0,
    stagger: float = 0.055,
) -> float:
    """Return staggered exit progress from a normalized mission transition."""
    progress = max(0.0, min(1.0, float(progress)))
    delay = max(0, int(order)) * max(0.0, float(stagger))
    available = max(0.12, 1.0 - delay)
    return _mission_intro_ease((progress - delay) / available)


def _mission_complete_plate_progress(phase: float) -> tuple[float, float, float]:
    """Return plate alpha, bracket lock, and sheen progress for the completion graphic."""
    phase = max(0.0, float(phase))
    enter = _mission_intro_ease(phase / 0.30)
    exit_t = max(0.0, min(1.0, (phase - 2.35) / 0.42))
    alpha = max(0.0, min(1.0, enter * (1.0 - exit_t * exit_t)))
    lock = _mission_intro_ease((phase - 0.08) / 0.30)
    sheen = max(0.0, min(1.0, (phase - 0.38) / 0.52))
    return alpha, lock, sheen


MISSION_COMPLETE_HOLD_FRAMES = 90
MISSION_COMPLETE_MIN_BODY_HEIGHT = 96


def _mission_complete_badge_progress(
    elapsed_frames: float,
    total_frames: int = 90,
) -> tuple[float, float, float, float]:
    """Return alpha, slide, title lock, and sheen progress for the in-panel badge."""
    total_frames = max(1, int(total_frames))
    elapsed = max(0.0, min(float(total_frames), float(elapsed_frames)))
    enter = _mission_intro_ease(elapsed / 9.0)
    slide = _mission_intro_ease(elapsed / 13.0)
    title_lock = _mission_intro_ease(max(0.0, elapsed - 4.0) / 11.0)
    exit_start = max(0.0, float(total_frames) - 16.0)
    exit_t = _mission_intro_ease((elapsed - exit_start) / 16.0)
    alpha = max(0.0, min(1.0, enter * (1.0 - exit_t)))
    sheen = max(0.0, min(1.0, (elapsed - 16.0) / 28.0))
    return alpha, slide, title_lock, sheen


def _draw_mission_completion_wipe(
    row_surf: pygame.Surface,
    progress: float,
    theme_color: tuple[int, int, int],
) -> None:
    """Darken a completed mission row under a bright sheen and particle trail."""
    progress = max(0.0, min(1.0, float(progress)))
    if progress <= 0.0:
        return

    width, height = row_surf.get_size()
    if width <= 0 or height <= 0:
        return

    eased = _mission_completion_ease(progress)
    wipe_x = max(0, min(width, int(round(width * eased))))

    if wipe_x > 0:
        dark_layer = pygame.Surface((wipe_x, height), pygame.SRCALPHA)
        for strip_y in range(height):
            frac = strip_y / max(1, height - 1)
            center = max(0.0, 1.0 - abs(frac - 0.46) * 2.35)
            r = int(13 + center * 8)
            g = int(25 + center * 13)
            b = int(18 + center * 8)
            pygame.draw.line(dark_layer, (r, g, b, 232), (0, strip_y), (wipe_x, strip_y))
        row_surf.blit(dark_layer, (0, 0))

    if progress >= 0.995:
        return

    polish = pygame.Surface((width, height), pygame.SRCALPHA)
    band_w = max(48, min(88, width // 3))
    slant = max(8, height // 3)
    tail_x = wipe_x - band_w
    head_x = wipe_x + max(12, band_w // 4)

    # Broad colored glow makes the completion pass unmistakable.
    pygame.draw.polygon(
        polish,
        (
            min(105, 42 + int(theme_color[0]) // 4),
            min(135, 52 + int(theme_color[1]) // 3),
            min(180, 70 + int(theme_color[2]) // 2),
            82,
        ),
        [
            (tail_x - slant, height),
            (tail_x, 0),
            (head_x, 0),
            (head_x - slant, height),
        ],
    )

    core_w = max(12, band_w // 5)
    pygame.draw.polygon(
        polish,
        (145, 185, 235, 118),
        [
            (wipe_x - core_w - slant, height),
            (wipe_x - core_w, 0),
            (wipe_x + core_w, 0),
            (wipe_x + core_w - slant, height),
        ],
    )
    pygame.draw.line(
        polish,
        (210, 235, 255, 145),
        (wipe_x + 2, 1),
        (wipe_x + 2 - slant, height - 2),
        2,
    )

    # A short deterministic particle trail follows the ridge. It is tied to
    # wipe progress, so it remains stable and does not shimmer after settling.
    particle_count = 11
    trail_span = max(48, min(104, width // 3))
    for particle_index in range(particle_count):
        ratio = (particle_index + 1) / float(particle_count + 1)
        px = wipe_x - int(ratio * trail_span)
        if px < -4 or px >= width + 4:
            continue
        wave = 0.5 + 0.5 * math.sin(progress * 18.0 + particle_index * 1.73)
        py = int(round(3 + wave * max(1, height - 7)))
        life = pow(1.0 - ratio, 1.25)
        radius = 2 if particle_index < 4 else 1
        particle_color = (
            min(170, 70 + int(theme_color[0]) // 2),
            min(205, 80 + int(theme_color[1]) // 2),
            min(245, 105 + int(theme_color[2]) // 2),
            int(150 * life),
        )
        pygame.draw.circle(polish, particle_color, (px, py), radius)
        streak = max(2, int(8 * life))
        pygame.draw.line(
            polish,
            particle_color,
            (px - streak, py),
            (px, py),
            1,
        )

    row_surf.blit(polish, (0, 0), special_flags=pygame.BLEND_RGBA_ADD)

def pause_on_error(context: str, exc: BaseException) -> None:
    print(f"\n[{context}]")
    print(f"error={exc!r}")
    traceback.print_exc()
    try:
        _ensure_parent(CRASH_LOG_FILE)
        with open(CRASH_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n[{context}]\n")
            f.write(f"error={exc!r}\n")
            traceback.print_exc(file=f)
            f.write("\n")
    except Exception:
        pass

    try:
        input("\nCrash detected. Press Enter to close...")
    except EOFError:
        pass


def set_dpi_aware() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def find_dolphin_hwnd() -> Optional[int]:
    candidates: list[tuple[int, int, str]] = []

    def score_title(title: str) -> int:
        tl = title.lower()
        if "dolphin" not in tl:
            return -10_000

        score = 0

        if "|" in title:
            score += 50
            score += min(30, title.count("|") * 5)

        for token in ("jit", "jit64", "opengl", "vulkan", "d3d", "direct3d", "hle"):
            if token in tl:
                score += 20

        if "(" in title and ")" in title:
            score += 30

        for bad in (
            "memory",
            "watch",
            "log",
            "breakpoint",
            "register",
            "disassembly",
            "config",
            "settings",
        ):
            if bad in tl:
                score -= 25

        if title.count("|") >= 3:
            score += 20

        return score

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        if not title:
            return
        if "dolphin" not in title.lower():
            return
        candidates.append((score_title(title), hwnd, title))

    win32gui.EnumWindows(cb, None)

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def is_valid_hwnd(hwnd: Optional[int]) -> bool:
    try:
        return bool(hwnd and win32gui.IsWindow(int(hwnd)))
    except Exception:
        return False


def get_client_screen_rect(hwnd: Optional[int]) -> Optional[tuple[int, int, int, int]]:
    if not is_valid_hwnd(hwnd):
        return None

    try:
        left, top, right, bottom = win32gui.GetClientRect(int(hwnd))
        tl = win32gui.ClientToScreen(int(hwnd), (left, top))
        br = win32gui.ClientToScreen(int(hwnd), (right, bottom))
    except Exception:
        return None

    w = br[0] - tl[0]
    h = br[1] - tl[1]
    if w <= 0 or h <= 0:
        return None

    return tl[0], tl[1], w, h


def apply_overlay_style(hwnd: int) -> None:
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    style &= ~(
        win32con.WS_CAPTION
        | win32con.WS_THICKFRAME
        | win32con.WS_MINIMIZE
        | win32con.WS_MAXIMIZE
        | win32con.WS_SYSMENU
    )
    style |= win32con.WS_POPUP
    win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)

    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    ex |= win32con.WS_EX_LAYERED
    ex &= ~win32con.WS_EX_TOPMOST
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)

    win32gui.SetLayeredWindowAttributes(hwnd, 0x000000, 0, win32con.LWA_COLORKEY)
    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_NOTOPMOST,
        0,
        0,
        0,
        0,
        win32con.SWP_FRAMECHANGED
        | win32con.SWP_NOMOVE
        | win32con.SWP_NOSIZE
        | win32con.SWP_NOACTIVATE,
    )


def sync_overlay_to_dolphin(dolphin_hwnd: Optional[int], overlay_hwnd: Optional[int]) -> Optional[tuple[int, int]]:
    if not is_valid_hwnd(overlay_hwnd):
        return None

    rect = get_client_screen_rect(dolphin_hwnd)
    if rect is None:
        return None

    x, y, w, h = rect
    try:
        win32gui.SetWindowPos(
            int(overlay_hwnd),
            win32con.HWND_NOTOPMOST,
            x,
            y,
            w,
            h,
            win32con.SWP_NOACTIVATE,
        )
    except Exception:
        return None

    return w, h


@dataclass
class MasterControl:
    show_hud: bool = True
    show_hitboxes: bool = True
    show_hurtboxes: bool = True
    show_debug: bool = False
    show_interaction_card: bool = False
    show_combo_card: bool = False
    # Core telemetry: damage scaling and native stun clocks are first-class HUD components.
    show_damage_badge: bool = True
    show_untech_panel: bool = True
    show_damage_inactive: bool = True
    show_meter_panel: bool = True
    show_red_health_panel: bool = True
    show_attack_property_panel: bool = False
    show_tag_card: bool = False
    hud_info_set: str = "CORE"


class Renderer(Protocol):
    def on_resize(self, w: int, h: int) -> None:
        ...

    def update(self, dt: float, control: MasterControl) -> None:
        ...

    def draw(self, screen: pygame.Surface, control: MasterControl) -> None:
        ...


class NullHudRenderer:
    """
    Placeholder HUD renderer.
    This will get replaced by the converted HUD module later.
    """

    def __init__(self) -> None:
        self.font: Optional[pygame.font.Font] = None
        self.smallfont: Optional[pygame.font.Font] = None
        self.w = BASE_W
        self.h = BASE_H

    def on_resize(self, w: int, h: int) -> None:
        self.w = w
        self.h = h
        scale = min(w / BASE_W, h / BASE_H)
        font_size = max(10, int(16 * scale))
        small_size = max(8, int(13 * scale))

        try:
            self.font = pygame.font.SysFont("consolas", font_size, bold=True)
        except Exception:
            self.font = pygame.font.Font(None, font_size)

        try:
            self.smallfont = pygame.font.SysFont("consolas", small_size)
        except Exception:
            self.smallfont = pygame.font.Font(None, small_size)

    def update(self, dt: float, control: MasterControl) -> None:
        return

    def draw(self, screen: pygame.Surface, control: MasterControl) -> None:
        if not control.show_hud:
            return
        if self.font is None or self.smallfont is None:
            return

        label = self.font.render("MASTER HUD SLOT", True, (220, 220, 220))
        sub = self.smallfont.render("HUD renderer not wired yet", True, (150, 150, 150))

        x = 24
        y = max(50, int(screen.get_height() * 0.22))

        bg_w = max(label.get_width(), sub.get_width()) + 16
        bg_h = label.get_height() + sub.get_height() + 14

        bg = pygame.Surface((bg_w, bg_h), pygame.SRCALPHA)
        bg.fill((18, 18, 18, 180))
        screen.blit(bg, (x - 8, y - 6))

        screen.blit(label, (x, y))
        screen.blit(sub, (x, y + label.get_height() + 4))


class NullHitboxRenderer:
    """
    Placeholder hitbox renderer.
    This will get replaced by the converted hitbox module later.
    """

    def __init__(self) -> None:
        self.w = BASE_W
        self.h = BASE_H
        self.phase = 0.0

    def on_resize(self, w: int, h: int) -> None:
        self.w = w
        self.h = h

    def update(self, dt: float, control: MasterControl) -> None:
        self.phase += dt * 2.0

    def draw(self, screen: pygame.Surface, control: MasterControl) -> None:
        if not control.show_hitboxes:
            return

        cx = screen.get_width() // 2
        cy = screen.get_height() // 2
        pulse = 40 + int(10 * (0.5 + 0.5 * __import__("math").sin(self.phase)))
        r = max(20, pulse)

        pygame.draw.circle(screen, (255, 120, 120, 100), (cx, cy), r, 2)
        pygame.draw.circle(screen, (120, 180, 255, 100), (cx + 80, cy - 20), max(12, r - 18), 2)
        pygame.draw.line(screen, (200, 200, 200, 120), (cx - 10, cy), (cx + 10, cy), 1)
        pygame.draw.line(screen, (200, 200, 200, 120), (cx, cy - 10), (cx, cy + 10), 1)


class MasterOverlay:
    def __init__(self) -> None:
        self.dolphin_hwnd: Optional[int] = None
        self.overlay_hwnd: Optional[int] = None
        self.screen: Optional[pygame.Surface] = None
        self.clock: Optional[pygame.time.Clock] = None

        self.w = BASE_W
        self.h = BASE_H

        self.running = True
        self.control = MasterControl()

        self.font: Optional[pygame.font.Font] = None
        self.smallfont: Optional[pygame.font.Font] = None

        self._last_control_mtime = 0.0
        
        self.mission_active = False
        self.mission_slot: Optional[str] = None
        self._last_mission_mtime = 0.0
        self._last_mission_overlay_mtime = 0
        self.mission_overlay_data: dict = {}
        self._last_yami_name_mtime_ns: int = 0
        self.yami_name_overlay_data: dict = {}
        self._yami_name_font_cache: dict[int, pygame.font.Font] = {}
        self.mission_click_rects: list[tuple[pygame.Rect, Optional[str]]] = []
        self.mission_panel_rect: Optional[pygame.Rect] = None
        self.mission_toggle_rect: Optional[pygame.Rect] = None
        self.mission_hint_rect: Optional[pygame.Rect] = None
        self.mission_show_all: bool = False
        self.mission_show_hint: bool = False
        self._mission_hint_fold: float = 0.0

        self.hud_renderer: Renderer = NullHudRenderer()
        self.hitbox_renderer: Renderer = NullHitboxRenderer()

        # Mission step animation state
        # step_anim[idx] = t in [0.0, 1.0]  (1.0 = fully active/metallic, 0.0 = dark/done)
        self.step_anim: dict[int, float] = {}
        self._prev_current_step: int = -1
        self._prev_completed_count: int = 0
        self._prev_live_completed_count: int = 0
        self._prev_live_step_total: int = 0
        self._prev_live_mission_id: str = ""
        self._last_clear_seq_seen: int = 0

        # Celebration state
        self._celebrate_active: bool = False
        self._celebrate_phase: float = 0.0      # total elapsed since trigger
        self._celebrate_particles: list = []
        self._prev_mission_complete: bool = False
        self._last_mission_id_seen: str = ""
        self._mission_hold_frames: int = 0
        self._mission_hold_data: dict = {}
        self._mission_hold_duration_frames: int = MISSION_COMPLETE_HOLD_FRAMES

        # Lightweight mission polish state
        self._toast_phase: float = 0.0
        self._row_bump: dict[int, float] = {}
        self._last_completed_for_bump: int = 0

        # Collapsed Mission Mode step viewport animation.
        self._mission_scroll_pos: float = 0.0
        self._mission_scroll_from: float = 0.0
        self._mission_scroll_target: float = 0.0
        self._mission_scroll_phase: float = 1.0
        self._mission_scroll_mission_id: str = ""
        self._mission_scroll_initialized: bool = False

        # Mission panel activation choreography. The panel enters first, then
        # visible rows slide and fade in one at a time. The sequence restarts
        # only when Mission Mode opens or the selected mission changes.
        self._mission_intro_phase: float = 0.0
        self._mission_intro_mission_id: str = ""
        self._mission_intro_was_active: bool = False
        self._mission_intro_generation: int = 0

        # Mission-to-mission transition state. The live payload can change
        # instantly when a point character tags or the selected trial changes,
        # while the renderer briefly keeps the previous payload on screen so
        # its rows and chips can slide and fade away before the new set enters.
        self._mission_visible_data: dict = {}
        self._mission_visible_key: tuple[str, str, str, str] | None = None
        self._mission_last_active_data: dict = {}
        self._mission_transition_old_data: dict = {}
        self._mission_transition_new_data: dict = {}
        self._mission_transition_new_key: tuple[str, str, str, str] | None = None
        self._mission_transition_state: str = "idle"
        self._mission_transition_phase: float = 1.0
        self._mission_transition_out_duration: float = 0.22
        self._mission_transition_in_duration: float = 0.34

        # Header progress strip animation. This is a float so the pips can
        # light up and power down one by one instead of snapping.
        self._mission_progress_display: float = 0.0
        self._mission_progress_mission_id: str = ""
        self._mission_progress_last_completed: int = 0
        self._mission_pip_sheen_pending_index: int = -1
        self._mission_pip_sheen_index: int = -1
        self._mission_pip_sheen_phase: float = 1.0
        self._mission_strip_complete_sheen_phase: float = 1.0
        self._mission_pip_levels: list[float] = []
        self._mission_pip_wave_order: list[int] = []
        self._mission_pip_wave_starts: dict[int, float] = {}
        self._mission_pip_wave_target: float = 0.0
        self._mission_pip_wave_elapsed: float = 0.0
        self._mission_pip_wave_completed_count: int = 0
        

    def init(self) -> None:
        set_dpi_aware()

        # Hook Dolphin memory before anything tries to read it
        from tvcgui.platform.dolphin import hook
        hook()

        self.dolphin_hwnd = find_dolphin_hwnd()

        self.dolphin_hwnd = find_dolphin_hwnd()
        if not self.dolphin_hwnd:
            raise RuntimeError("Dolphin window not found.")

        pygame.init()

        try:
            from tvcgui.features.overlay import hud_renderer as hud_renderer_module
            self.hud_renderer = hud_renderer_module.HudRenderer()
            print(f"[master] master_renderer.__file__={os.path.abspath(__file__)}")
            print(f"[master] hud_renderer.__file__={os.path.abspath(hud_renderer_module.__file__)}")
            print("[master] hud renderer loaded")
        except Exception:
            print("[master] failed to import hud renderer")
            traceback.print_exc()
            self.hud_renderer = NullHudRenderer()

        try:
            from tvcgui.features.hitboxes.renderer import HitboxRenderer
            self.hitbox_renderer = HitboxRenderer()
            print("[master] hitbox renderer loaded")
        except Exception:
            print("[master] failed to import hitbox renderer")
            traceback.print_exc()
            self.hitbox_renderer = NullHitboxRenderer()

        self.screen = pygame.display.set_mode((self.w, self.h), pygame.SRCALPHA)
        pygame.display.set_caption("TvC Master Overlay")

        icon_path = os.path.join("assets", "portraits", "Placeholder.png")
        if not os.path.exists(icon_path):
            icon_path = os.path.join("assets", "icon.png")
        if os.path.exists(icon_path):
            try:
                icon = pygame.image.load(icon_path).convert_alpha()
                pygame.display.set_icon(icon)
            except Exception:
                pass

        self.overlay_hwnd = pygame.display.get_wm_info()["window"]
        apply_overlay_style(self.overlay_hwnd)
        win32gui.SetWindowLong(self.overlay_hwnd, win32con.GWL_HWNDPARENT, self.dolphin_hwnd)

        self.clock = pygame.time.Clock()

        self._refresh_fonts()
        self.hud_renderer.on_resize(self.w, self.h)
        self.hitbox_renderer.on_resize(self.w, self.h)

        self._write_default_control_file()

    def _refresh_fonts(self) -> None:
        scale = min(self.w / BASE_W, self.h / BASE_H)
        font_size = max(10, int(14 * scale))
        small_size = max(8, int(12 * scale))

        try:
            self.font = pygame.font.SysFont("consolas", font_size, bold=True)
        except Exception:
            self.font = pygame.font.Font(None, font_size)

        try:
            self.smallfont = pygame.font.SysFont("consolas", small_size)
        except Exception:
            self.smallfont = pygame.font.Font(None, small_size)
    def _char_theme_color(self, character: str) -> tuple[int, int, int]:
        name = (character or "").strip().lower()

        if "alex" in name:
            return (40, 120, 255)      # Capcom blue
        if "ryu" in name:
            return (170, 30, 30)       # deep red
        if "chun" in name:
            return (80, 150, 255)      # Chun blue
        if "ken" in name:
            return (245, 245, 245)     # white
        if "jun" in name:
            return (255, 70, 170)      # hot pink
        if "viewtiful joe" in name or "v joe" in name or "joe" in name:
            return (220, 40, 40)       # hero red
        if "casshan" in name:
            return (180, 230, 255)     # steel/cyan
        if "doronjo" in name:
            return (210, 120, 255)     # villain purple
        if "saki" in name:
            return (255, 220, 80)      # gold
        if "batsu" in name:
            return (255, 140, 40)      # orange
        if "morrigan" in name:
            return (120, 40, 170)      # succubus purple
        if "roll" in name:
            return (255, 170, 210)     # soft pink
        if "polimar" in name:
            return (220, 35, 35)       # red hero
        if "karas" in name:
            return (90, 90, 110)       # dark steel
        if "zero" in name:
            return (220, 40, 40)       # crimson
        if "gold_lightan" in name or "gold lightan" in name:
            return (255, 215, 40)      # gold
        if "tekkaman_blade" in name or "tekkaman blade" in name:
            return (120, 180, 255)     # blade blue
        if "tekkaman" in name:
            return (235, 235, 235)     # white armor
        if "volnutt" in name:
            return (70, 170, 255)      # mega blue
        if "ptx_40a" in name or "ptx" in name:
            return (255, 150, 60)
        if "yatterman_1" in name:
            return (255, 60, 60)       # red
        if "yatterman_2" in name:
            return (255, 120, 190)     # pink

        return (170, 120, 255)
    def _wrap_text_lines(self, text: str, font: pygame.font.Font, max_width: int) -> list[str]:
        """Wrap text to a pixel width, including unusually long single tokens."""
        if not text:
            return []

        out: list[str] = []
        max_width = max(32, int(max_width))

        def split_oversized_word(word: str) -> list[str]:
            if font.size(word)[0] <= max_width:
                return [word]

            chunks: list[str] = []
            chunk = ""
            for char in word:
                trial = chunk + char
                if chunk and font.size(trial)[0] > max_width:
                    chunks.append(chunk)
                    chunk = char
                else:
                    chunk = trial
            if chunk:
                chunks.append(chunk)
            return chunks or [word]

        for paragraph in str(text).splitlines():
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            words: list[str] = []
            for word in paragraph.split():
                words.extend(split_oversized_word(word))
            if not words:
                continue

            line = words[0]
            for word in words[1:]:
                trial = f"{line} {word}"
                if font.size(trial)[0] <= max_width:
                    line = trial
                else:
                    out.append(line)
                    line = word
            out.append(line)

        return out

    def _write_default_control_file(self) -> None:
        if os.path.exists(MASTER_CONTROL_FILE):
            return
        self._write_control_file()

    def _write_control_file(self) -> None:
        payload = {
            "show_hud": self.control.show_hud,
            "show_hitboxes": self.control.show_hitboxes,
            "show_hurtboxes": self.control.show_hurtboxes,
            "show_debug": self.control.show_debug,
            "show_interaction_card": self.control.show_interaction_card,
            "show_combo_card": self.control.show_combo_card,
            "show_damage_badge": self.control.show_damage_badge,
            "show_untech_panel": self.control.show_untech_panel,
            "show_damage_inactive": self.control.show_damage_inactive,
            "show_meter_panel": self.control.show_meter_panel,
            "show_red_health_panel": self.control.show_red_health_panel,
            "show_attack_property_panel": self.control.show_attack_property_panel,
            "show_tag_card": self.control.show_tag_card,
            "hud_info_set": self.control.hud_info_set,
            "native_hud_defaults_v": 4,
        }
        try:
            _ensure_parent(MASTER_CONTROL_FILE)
            with open(MASTER_CONTROL_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            pass

    def _read_control_file(self) -> None:
        try:
            mt = os.path.getmtime(MASTER_CONTROL_FILE)
            if mt == self._last_control_mtime:
                return
            self._last_control_mtime = mt

            with open(MASTER_CONTROL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.control.show_hud = bool(data.get("show_hud", True))
            self.control.show_hitboxes = bool(data.get("show_hitboxes", True))
            self.control.show_hurtboxes = bool(data.get("show_hurtboxes", True))
            self.control.show_debug = bool(data.get("show_debug", False))
            self.control.show_interaction_card = bool(data.get("show_interaction_card", False))
            self.control.show_combo_card = bool(data.get("show_combo_card", False))
            self.control.show_damage_badge = bool(data.get("show_damage_badge", True))
            self.control.show_untech_panel = bool(data.get("show_untech_panel", True))
            self.control.show_damage_inactive = bool(data.get("show_damage_inactive", True))
            self.control.show_meter_panel = bool(data.get("show_meter_panel", True))
            self.control.show_red_health_panel = bool(data.get("show_red_health_panel", True))
            self.control.show_attack_property_panel = bool(data.get("show_attack_property_panel", False))
            self.control.show_tag_card = bool(data.get("show_tag_card", False))
            self.control.hud_info_set = str(data.get("hud_info_set", "CORE") or "CORE").strip().upper()
            # Damage scaling and native stun clocks are part of CORE. Old control
            # files may have persisted both as false before they were promoted.
            # CORE now guarantees them; an individual user toggle should switch
            # the GUI to CUSTOM and is then respected normally.
            if self.control.hud_info_set == "CORE":
                self.control.show_damage_badge = True
                self.control.show_untech_panel = True
        except Exception:
            pass
    def _read_mission_mode_file(self) -> None:
        try:
            mt = os.path.getmtime(MISSION_MODE_FILE)
            if mt == self._last_mission_mtime:
                return
            self._last_mission_mtime = mt

            with open(MISSION_MODE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.mission_active = bool(data.get("active", False))
            self.mission_slot = data.get("slot")
        except Exception:
            self.mission_active = False
            self.mission_slot = None

    def _mission_payload_key(self, data: dict) -> tuple[str, str, str, str]:
        data = data if isinstance(data, dict) else {}
        return (
            str(data.get("character") or ""),
            str(data.get("active_mission_id") or ""),
            str(data.get("active_mission_name") or ""),
            str(self.mission_slot or data.get("slot") or ""),
        )

    def _reset_mission_entry_animation(self) -> None:
        """Reset only visual state when a newly selected mission takes over."""
        self.step_anim = {}
        self._row_bump = {}
        self._last_completed_for_bump = 0
        self._mission_scroll_initialized = False
        self._mission_scroll_mission_id = ""
        self._mission_intro_phase = 0.0
        self._mission_intro_mission_id = ""
        self._mission_intro_was_active = False
        self._mission_progress_mission_id = ""
        self._mission_progress_display = 0.0
        self._mission_progress_last_completed = 0
        self._mission_pip_levels = []
        self._mission_pip_wave_order = []
        self._mission_pip_wave_starts = {}
        self._mission_pip_sheen_pending_index = -1
        self._mission_pip_sheen_index = -1
        self._mission_pip_sheen_phase = 1.0
        self._mission_strip_complete_sheen_phase = 1.0

    def _stage_mission_overlay_payload(self, data: dict) -> None:
        """Publish mission UI state immediately with no transition latency."""
        data = dict(data or {})
        previous_key = self._mission_visible_key
        new_key = self._mission_payload_key(data)
        selector_open = bool(data.get("selector_open", False))

        self.mission_overlay_data = data
        self._mission_visible_data = dict(data)
        self._mission_visible_key = new_key
        if not selector_open:
            self._mission_last_active_data = dict(data)

        self._mission_transition_state = "idle"
        self._mission_transition_phase = 1.0
        self._mission_transition_old_data = {}
        self._mission_transition_new_data = {}
        self._mission_transition_new_key = None

        # A completion hold owns the mission panel until its rendered-frame
        # countdown finishes. Live mission payloads continue updating behind
        # it, but they must not replace the completion badge on the next file
        # refresh.
        if previous_key != new_key and self._mission_hold_frames <= 0:
            self._reset_mission_entry_animation()

    def _update_mission_transition(self, dt: float) -> None:
        dt = max(0.0, float(dt))
        if self._mission_transition_state == "out":
            duration = max(0.01, self._mission_transition_out_duration)
            self._mission_transition_phase = min(
                1.0,
                self._mission_transition_phase + dt / duration,
            )
            if self._mission_transition_phase >= 1.0:
                incoming = self._mission_transition_new_data or self.mission_overlay_data
                self._mission_visible_data = dict(incoming or {})
                if not bool(self._mission_visible_data.get("selector_open", False)):
                    self._mission_last_active_data = dict(self._mission_visible_data)
                self._mission_visible_key = (
                    self._mission_transition_new_key
                    or self._mission_payload_key(self._mission_visible_data)
                )
                self._mission_transition_old_data = {}
                self._mission_transition_new_data = {}
                self._mission_transition_new_key = None
                self._mission_transition_state = "in"
                self._mission_transition_phase = 0.0
                self._reset_mission_entry_animation()

        elif self._mission_transition_state == "in":
            duration = max(0.01, self._mission_transition_in_duration)
            self._mission_transition_phase = min(
                1.0,
                self._mission_transition_phase + dt / duration,
            )
            if self._mission_transition_phase >= 1.0:
                self._mission_transition_state = "idle"
                self._mission_transition_phase = 1.0

    def _mission_display_payload(self) -> dict:
        if self._mission_transition_state == "out" and self._mission_transition_old_data:
            return self._mission_transition_old_data
        if self._mission_visible_data:
            return self._mission_visible_data
        return self.mission_overlay_data or {}

    def _read_mission_overlay_file(self) -> None:
        try:
            mt = os.stat(MISSION_OVERLAY_FILE).st_mtime_ns
            if mt == self._last_mission_overlay_mtime:
                return
            self._last_mission_overlay_mtime = mt

            with open(MISSION_OVERLAY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                return

            self._stage_mission_overlay_payload(data)

        except Exception:
            pass

    def on_resize(self, w: int, h: int) -> None:
        if w <= 0 or h <= 0:
            return
        if w == self.w and h == self.h:
            return

        self.w = w
        self.h = h

        self.screen = pygame.display.set_mode((w, h), pygame.SRCALPHA)
        self._refresh_fonts()

        self.hud_renderer.on_resize(w, h)
        self.hitbox_renderer.on_resize(w, h)

    def _write_mission_select_command(self, payload: dict) -> None:
        try:
            _ensure_parent(MISSION_SELECT_FILE)
            with open(MISSION_SELECT_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            pass
    def _write_mission_celebrate_ack(self, celebrate_token: int) -> None:
        try:
            _ensure_parent(MISSION_CELEBRATE_ACK_FILE)
            with open(MISSION_CELEBRATE_ACK_FILE, "w", encoding="utf-8") as f:
                json.dump({"celebrate_token": int(celebrate_token)}, f, indent=2)
        except Exception:
            pass
    def handle_events(self) -> None:
        now = time.monotonic()
        if now < float(getattr(self, "_event_poll_resume_time", 0.0) or 0.0):
            return

        try:
            events = pygame.event.get()
        except Exception as exc:
            error_count = int(getattr(self, "_event_poll_error_count", 0) or 0) + 1
            self._event_poll_error_count = error_count
            self._event_poll_resume_time = now + 0.25

            last_log = float(getattr(self, "_event_poll_last_log", 0.0) or 0.0)
            if now - last_log >= 2.0:
                self._event_poll_last_log = now
                print(
                    "[master] pygame event queue read failed; "
                    f"overlay kept alive, retrying shortly ({type(exc).__name__}: {exc})"
                )

            # A malformed SDL event can leave Pygame's converter with an
            # exception set. Clearing is best effort and must never terminate
            # the compositor.
            try:
                pygame.event.clear()
            except Exception:
                try:
                    pygame.event.pump()
                except Exception:
                    pass
            return

        self._event_poll_error_count = 0
        self._event_poll_resume_time = 0.0

        for event in events:
            if event.type == pygame.QUIT:
                self.running = False

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if self.mission_toggle_rect and self.mission_toggle_rect.collidepoint(event.pos):
                    self.mission_show_all = not self.mission_show_all
                    return

                if self.mission_hint_rect and self.mission_hint_rect.collidepoint(event.pos):
                    self.mission_show_hint = not self.mission_show_hint
                    return

                if self.mission_panel_rect and self.mission_panel_rect.collidepoint(event.pos):
                    for rect, mission_id in self.mission_click_rects:
                        if rect.collidepoint(event.pos):
                            if mission_id:
                                self._write_mission_select_command({
                                    "action": "select",
                                    "slot": self.mission_slot,
                                    "mission_id": mission_id,
                                })
                            return
                elif self.mission_overlay_data.get("selector_open"):
                    self._write_mission_select_command({
                        "action": "close",
                    })
                    return

            # Keyboard hotkeys intentionally disabled; layer controls live in the main GUI.

    def clear(self) -> None:
        assert self.screen is not None
        self.screen.fill(COLORKEY)

    def _read_yami_name_overlay_file(self) -> None:
        try:
            st = os.stat(YAMI_NAME_OVERLAY_FILE)
            mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
            if mtime_ns == self._last_yami_name_mtime_ns:
                return
            with open(YAMI_NAME_OVERLAY_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self.yami_name_overlay_data = data
                self._last_yami_name_mtime_ns = mtime_ns
        except FileNotFoundError:
            self.yami_name_overlay_data = {}
            self._last_yami_name_mtime_ns = 0
        except Exception:
            # Character-select labels are cosmetic. Never let a partial state
            # file disturb the gameplay overlay.
            return

    def _yami_name_font(self, px: int) -> pygame.font.Font:
        size = max(8, int(px))
        cached = self._yami_name_font_cache.get(size)
        if cached is not None:
            return cached
        try:
            font = pygame.font.SysFont("Arial Black", size, bold=True)
        except Exception:
            try:
                font = pygame.font.SysFont("impact", size, bold=True)
            except Exception:
                font = pygame.font.Font(None, size)
        self._yami_name_font_cache[size] = font
        return font

    def _draw_yami_name_plate(self, label: str, base_rect: tuple[float, float, float, float]) -> None:
        if not label or self.screen is None:
            return
        scale = min(self.w / float(BASE_W), self.h / float(BASE_H))
        if scale <= 0.0:
            return
        ox = (self.w - BASE_W * scale) * 0.5
        oy = (self.h - BASE_H * scale) * 0.5
        bx, by, bw, bh = base_rect
        x = int(round(ox + bx * scale))
        y = int(round(oy + by * scale))
        w = max(34, int(round(bw * scale)))
        h = max(12, int(round(bh * scale)))

        # Cover only the stock character-name strip, not the portrait or the
        # Ryu placeholder icon. A slightly skewed plate matches the native card
        # well enough to read as part of the menu rather than HUD text.
        cut = max(2, int(round(8 * scale)))
        pts = [
            (x + cut, y),
            (x + w, y + max(1, cut // 2)),
            (x + w - cut, y + h),
            (x, y + h - max(1, cut // 2)),
        ]
        pygame.draw.polygon(self.screen, (8, 22, 40), pts)
        pygame.draw.polygon(self.screen, (180, 205, 225), pts, max(1, int(round(2 * scale))))
        top_y = y + max(1, int(round(3 * scale)))
        pygame.draw.line(
            self.screen,
            (55, 145, 230),
            (x + cut + 2, top_y),
            (x + w - 4, top_y),
            max(1, int(round(2 * scale))),
        )

        font = self._yami_name_font(max(10, int(round(27 * scale))))
        text = font.render(str(label).upper(), True, (245, 248, 252))
        # Small dark outline/shadow to survive the animated character-select BG.
        shadow = font.render(str(label).upper(), True, (0, 0, 0))
        tx = x + (w - text.get_width()) // 2
        ty = y + (h - text.get_height()) // 2 - max(0, int(round(1 * scale)))
        self.screen.blit(shadow, (tx + max(1, int(round(scale))), ty + max(1, int(round(scale)))))
        self.screen.blit(text, (tx, ty))

    def draw_yami_name_overlay(self) -> None:
        data = self.yami_name_overlay_data
        if not isinstance(data, dict) or not bool(data.get("active")):
            return
        p1 = str(data.get("p1") or "").strip()
        p2 = str(data.get("p2") or "").strip()
        # Calibrated against the native 1280x720 Character Select layout. The
        # master overlay applies the same centered aspect-fit transform used for
        # the rest of TvCGUI, so windowed/letterboxed Dolphin scales cleanly.
        if p1:
            self._draw_yami_name_plate(p1, (198.0, 365.0, 230.0, 48.0))
        if p2:
            self._draw_yami_name_plate(p2, (852.0, 365.0, 230.0, 48.0))

    def draw_master_debug(self, dt: float) -> None:
        if not self.control.show_debug:
            return
        if self.screen is None or self.font is None or self.smallfont is None:
            return

        lines = [
            "MASTER OVERLAY",
            f"{self.w}x{self.h}",
            f"HUD: {'ON' if self.control.show_hud else 'OFF'}",
            f"HITBOXES: {'ON' if self.control.show_hitboxes else 'OFF'}",
            f"HURTBOXES: {'ON' if self.control.show_hurtboxes else 'OFF'}",
            f"dt={dt:.4f}",
        ]

        rendered = [self.smallfont.render(line, True, (180, 180, 180)) for line in lines]
        box_w = max(s.get_width() for s in rendered) + 12
        box_h = sum(s.get_height() for s in rendered) + 12

        bg = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
        bg.fill((16, 16, 16, 190))
        self.screen.blit(bg, (10, 10))

        y = 16
        for surf in rendered:
            self.screen.blit(surf, (16, y))
            y += surf.get_height()


        y = 16
        for surf in rendered:
            self.screen.blit(surf, (16, y))
            y += surf.get_height()
    def _spawn_lightning_bolt(self) -> None:
        import math, random

        # Origin: random edge or top region
        side = random.choice(["top", "left", "right"])
        if side == "top":
            ox = random.randint(int(self.w * 0.1), int(self.w * 0.9))
            oy = random.randint(0, int(self.h * 0.15))
        elif side == "left":
            ox = random.randint(0, int(self.w * 0.15))
            oy = random.randint(0, int(self.h * 0.6))
        else:
            ox = random.randint(int(self.w * 0.85), self.w)
            oy = random.randint(0, int(self.h * 0.6))

        # Target: toward center-ish with some spread
        tx = self.w // 2 + random.randint(-int(self.w * 0.25), int(self.w * 0.25))
        ty = self.h // 2 + random.randint(-int(self.h * 0.2), int(self.h * 0.2))

        # Build jagged segments
        num_segments = random.randint(6, 14)
        dx = (tx - ox) / num_segments
        dy = (ty - oy) / num_segments
        perp_x = -dy
        perp_y = dx
        length = math.hypot(perp_x, perp_y)
        if length > 0:
            perp_x /= length
            perp_y /= length

        points = [(ox, oy)]
        for i in range(1, num_segments):
            jag = random.uniform(-0.45, 0.45) * math.hypot(dx, dy) * 2.2
            px = ox + dx * i + perp_x * jag
            py = oy + dy * i + perp_y * jag
            points.append((px, py))
        points.append((tx, ty))

        # Random branches
        branches = []
        for i in range(1, len(points) - 1):
            if random.random() < 0.35:
                bx, by = points[i]
                branch_len = random.randint(3, 6)
                branch_angle = math.atan2(dy, dx) + random.uniform(-1.1, 1.1)
                bpoints = [(bx, by)]
                for _ in range(branch_len):
                    bx += math.cos(branch_angle) * random.uniform(8, 22)
                    by += math.sin(branch_angle) * random.uniform(8, 22)
                    branch_angle += random.uniform(-0.4, 0.4)
                    bpoints.append((bx, by))
                branches.append(bpoints)

        color_choice = random.choice([
            (200, 230, 255),   # cool white-blue
            (255, 255, 160),   # yellow-white
            (220, 180, 255),   # purple
            (160, 240, 255),   # cyan
        ])

        lifetime = random.uniform(0.06, 0.18)
        width = random.choice([1, 1, 2, 2, 3])

        self._lightning_bolts.append({
            "points": points,
            "branches": branches,
            "color": color_choice,
            "life": lifetime,
            "max_life": lifetime,
            "width": width,
            "flicker_phase": random.uniform(0, math.tau),
        })

    def draw_lightning(self) -> None:
        import math, random
        if not self._celebrate_active or self.screen is None:
            return

        CELEBRATE_DURATION = 3.0
        # Only fire lightning in first ~2.4s
        if self._celebrate_phase > 0.8:
            return

        dt_approx = 1.0 / TARGET_FPS

        # Spawn new bolts on timer
        self._lightning_timer -= dt_approx
        if self._lightning_timer <= 0:
            count = random.randint(1, 3)
            for _ in range(count):
                self._spawn_lightning_bolt()
            self._lightning_timer = random.uniform(0.14, 0.35)

        # Update and draw existing bolts
        alive = []
        for bolt in self._lightning_bolts:
            bolt["life"] -= dt_approx
            if bolt["life"] <= 0:
                continue
            alive.append(bolt)

            fade = bolt["life"] / bolt["max_life"]
            flicker = 0.7 + 0.3 * math.sin(bolt["flicker_phase"] + self._celebrate_phase * 40)
            alpha = int(255 * fade * flicker)
            alpha = max(0, min(255, alpha))

            r, g, b = bolt["color"]

            # Glow pass (wider, transparent)
            if len(bolt["points"]) >= 2:
                glow_surf = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
                glow_color = (r, g, b, max(0, alpha // 4))
                glow_w = bolt["width"] + 3
                for i in range(len(bolt["points"]) - 1):
                    p1 = (int(bolt["points"][i][0]), int(bolt["points"][i][1]))
                    p2 = (int(bolt["points"][i+1][0]), int(bolt["points"][i+1][1]))
                    pygame.draw.line(glow_surf, glow_color, p1, p2, glow_w)
                self.screen.blit(glow_surf, (0, 0))

                # Core bolt
                core_color = (
                    min(255, r + 60),
                    min(255, g + 60),
                    min(255, b + 60),
                    alpha,
                )
                core_surf = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
                for i in range(len(bolt["points"]) - 1):
                    p1 = (int(bolt["points"][i][0]), int(bolt["points"][i][1]))
                    p2 = (int(bolt["points"][i+1][0]), int(bolt["points"][i+1][1]))
                    pygame.draw.line(core_surf, core_color, p1, p2, bolt["width"])
                self.screen.blit(core_surf, (0, 0))

                # Branches (thinner, dimmer)
                for branch in bolt["branches"]:
                    if len(branch) < 2:
                        continue
                    branch_color = (r, g, b, max(0, alpha // 2))
                    branch_surf = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
                    for i in range(len(branch) - 1):
                        p1 = (int(branch[i][0]), int(branch[i][1]))
                        p2 = (int(branch[i+1][0]), int(branch[i+1][1]))
                        pygame.draw.line(branch_surf, branch_color, p1, p2, max(1, bolt["width"] - 1))
                    self.screen.blit(branch_surf, (0, 0))

        self._lightning_bolts = alive
    def _trigger_celebration(self) -> None:
        """Start the flat esports mission-complete plate and restrained trail particles."""
        import math, random
        self._celebrate_active = True
        self._celebrate_phase = 0.0
        self._toast_phase = 0.0

        cx, cy = self.w // 2, self.h // 2 - int(self.h * 0.03)
        self._celebrate_particles = []

        palette = (
            (82, 150, 245),
            (117, 92, 205),
            (176, 205, 245),
            (235, 242, 252),
        )
        for _ in range(30):
            angle = random.uniform(-0.42, 0.42) + random.choice((0.0, math.pi))
            speed = random.uniform(90, 320)
            size = random.randint(1, 4)
            lifetime = random.uniform(0.55, 1.25)
            self._celebrate_particles.append({
                "x": float(cx + random.uniform(-65, 65)),
                "y": float(cy + random.uniform(-30, 30)),
                "vx": math.cos(angle) * speed,
                "vy": math.sin(angle) * speed + random.uniform(-40, 15),
                "size": size,
                "life": lifetime,
                "max_life": lifetime,
                "color": random.choice(palette),
            })

        self._lightning_bolts = []
        self._lightning_timer = 0.0
        self._lightning_spawn_interval = 999.0

    def update_celebration(self, dt: float) -> None:
        if not self._celebrate_active:
            return

        CELEBRATE_DURATION = 3.0
        self._celebrate_phase += dt
        if self._celebrate_phase >= CELEBRATE_DURATION:
            self._celebrate_active = False
            self._celebrate_particles = []
            return

        gravity = 300.0
        for p in self._celebrate_particles:
            p["x"] += p["vx"] * dt
            p["y"] += p["vy"] * dt
            p["vy"] += gravity * dt
            p["life"] -= dt

        self._celebrate_particles = [p for p in self._celebrate_particles if p["life"] > 0]

    def draw_celebration(self) -> None:
        """Draw a centered, flat-color esports mission-complete confirmation plate."""
        if not self._celebrate_active or self.screen is None or self.font is None:
            return

        phase = self._celebrate_phase
        plate_alpha_f, bracket_lock, sheen_progress = _mission_complete_plate_progress(phase)
        if plate_alpha_f <= 0.0:
            return
        plate_alpha = int(255 * plate_alpha_f)

        scale = min(self.w / 1280.0, self.h / 720.0)
        plate_w = max(430, min(int(self.w * 0.56), int(720 * max(0.85, scale))))
        plate_h = max(118, int(142 * max(0.82, scale)))
        plate_x = (self.w - plate_w) // 2
        plate_y = self.h // 2 - plate_h // 2 - int(self.h * 0.03)

        # Dim only the center band, preserving visibility of the mission list.
        band = pygame.Surface((self.w, plate_h + 76), pygame.SRCALPHA)
        band.fill((5, 10, 20, int(70 * plate_alpha_f)))
        self.screen.blit(band, (0, plate_y - 38))

        plate = pygame.Surface((plate_w, plate_h), pygame.SRCALPHA)
        cut = max(18, plate_h // 6)
        body = [
            (cut, 0), (plate_w - cut, 0), (plate_w, cut),
            (plate_w, plate_h - cut), (plate_w - cut, plate_h),
            (cut, plate_h), (0, plate_h - cut), (0, cut),
        ]
        pygame.draw.polygon(plate, (9, 18, 34, int(244 * plate_alpha_f)), body)
        pygame.draw.polygon(plate, (48, 67, 98, plate_alpha), body, 2)
        pygame.draw.line(plate, (80, 147, 235, plate_alpha), (cut + 18, 5), (plate_w - cut - 18, 5), 2)
        pygame.draw.line(plate, (108, 79, 181, int(205 * plate_alpha_f)), (cut + 34, plate_h - 6), (plate_w - cut - 34, plate_h - 6), 2)

        # Side brackets slide inward and lock around the plate.
        bracket_gap = int((1.0 - bracket_lock) * 72)
        bracket_color = (83, 147, 235, plate_alpha)
        violet = (112, 82, 188, int(225 * plate_alpha_f))
        left_x = 6 - bracket_gap
        right_x = plate_w - 6 + bracket_gap
        pygame.draw.lines(plate, bracket_color, False, [
            (left_x + cut + 10, 14), (left_x, plate_h // 2), (left_x + cut + 10, plate_h - 14)
        ], 4)
        pygame.draw.lines(plate, violet, False, [
            (right_x - cut - 10, 14), (right_x, plate_h // 2), (right_x - cut - 10, plate_h - 14)
        ], 4)

        # Small completion lock mark.
        lock_cx = plate_w // 2
        lock_cy = 19
        lock_r = max(10, int(13 * max(0.85, scale)))
        pygame.draw.circle(plate, (12, 27, 50, plate_alpha), (lock_cx, lock_cy), lock_r)
        pygame.draw.circle(plate, bracket_color, (lock_cx, lock_cy), lock_r, 2)
        pygame.draw.lines(plate, (205, 224, 250, plate_alpha), False, [
            (lock_cx - 5, lock_cy), (lock_cx - 1, lock_cy + 4), (lock_cx + 7, lock_cy - 5)
        ], 2)

        try:
            title_font = pygame.font.SysFont(
                "bahnschrift",
                max(26, int(42 * max(0.78, scale))),
                bold=True,
            )
            sub_font = pygame.font.SysFont(
                "bahnschrift",
                max(13, int(17 * max(0.78, scale))),
                bold=True,
            )
        except Exception:
            title_font = self.font
            sub_font = self.smallfont or self.font

        title_surf = title_font.render("MISSION COMPLETE", True, (232, 238, 247))
        title_surf.set_alpha(plate_alpha)
        title_y = plate_h // 2 - title_surf.get_height() // 2 - 4
        plate.blit(title_surf, ((plate_w - title_surf.get_width()) // 2, title_y))

        held = self._mission_hold_data or self.mission_overlay_data or {}
        mission_name = str(held.get("active_mission_name") or "OBJECTIVE SEQUENCE")
        if mission_name.strip().lower() in {"challenge", "no mission loaded"}:
            mission_name = "ALL OBJECTIVES CLEARED"
        else:
            mission_name = f"{mission_name.upper()} CLEARED"
        sub_surf = sub_font.render(mission_name, True, (94, 157, 242))
        sub_surf.set_alpha(plate_alpha)
        sub_y = min(plate_h - sub_surf.get_height() - 14, title_y + title_surf.get_height() + 6)
        plate.blit(sub_surf, ((plate_w - sub_surf.get_width()) // 2, sub_y))

        # One explicit sheen pass after the frame locks.
        if 0.0 < sheen_progress < 1.0:
            sheen = pygame.Surface((plate_w, plate_h), pygame.SRCALPHA)
            head_x = int(-100 + sheen_progress * (plate_w + 200))
            pygame.draw.polygon(sheen, (116, 177, 250, int(52 * plate_alpha_f)), [
                (head_x - 72, 0), (head_x - 18, 0), (head_x + 38, plate_h), (head_x - 20, plate_h)
            ])
            pygame.draw.polygon(sheen, (236, 245, 255, int(180 * plate_alpha_f)), [
                (head_x - 16, 0), (head_x - 8, 0), (head_x + 48, plate_h), (head_x + 38, plate_h)
            ])
            plate.blit(sheen, (0, 0), special_flags=pygame.BLEND_RGBA_ADD)

        self.screen.blit(plate, (plate_x, plate_y))

        # Restrained streak particles around the plate, not a full-screen explosion.
        for particle in self._celebrate_particles:
            fade = max(0.0, min(1.0, particle["life"] / max(0.001, particle["max_life"])))
            if fade <= 0.0:
                continue
            r, g, b = particle["color"]
            alpha = int(150 * fade * plate_alpha_f)
            px, py = int(particle["x"]), int(particle["y"])
            length = max(3, int(abs(particle["vx"]) * 0.025))
            direction = -1 if particle["vx"] < 0 else 1
            pygame.draw.line(
                self.screen,
                (r, g, b, alpha),
                (px - direction * length, py),
                (px, py),
                max(1, int(particle["size"])),
            )

    def _update_mission_scroll_state(
        self,
        dt: float,
        current_index: int,
        total_steps: int,
        mission_id: str,
    ) -> None:
        """Move the collapsed viewport immediately with live mission progress."""
        target = 0.0
        if not self.mission_show_all and total_steps > 6:
            target = float(_mission_scroll_target(current_index, total_steps, 6))

        mission_id = str(mission_id or "")
        if not self._mission_scroll_initialized or mission_id != self._mission_scroll_mission_id:
            self._mission_scroll_pos = target
            self._mission_scroll_from = target
            self._mission_scroll_target = target
            self._mission_scroll_phase = 1.0
            self._mission_scroll_mission_id = mission_id
            self._mission_scroll_initialized = True
            return

        self._mission_scroll_from = target
        self._mission_scroll_target = target
        self._mission_scroll_pos = target
        self._mission_scroll_phase = 1.0

    def update_mission_animations(self, dt: float) -> None:
        """Drive per-step metallic gradient animations each frame and fire celebration on final-step completion."""
        self._update_mission_transition(dt)
        live_data = self.mission_overlay_data or {}
        display_data = self._mission_display_payload()
        holding = self._mission_hold_frames > 0
        data = self._mission_hold_data if holding else display_data

        steps = data.get("active_mission_steps") or []
        completed_count = int(data.get("completed_step_count", 0))
        current_idx = int(data.get("current_step_index", 0))
        mission_id = str(data.get("active_mission_id") or "")
        self._update_mission_scroll_state(dt, current_idx, len(steps), mission_id)

        intro_key = mission_id or str(self.mission_slot or "")
        if self.mission_active:
            if (
                not self._mission_intro_was_active
                or intro_key != self._mission_intro_mission_id
            ):
                self._mission_intro_phase = 0.0
                self._mission_intro_mission_id = intro_key
                self._mission_intro_generation += 1
            else:
                self._mission_intro_phase = min(2.0, self._mission_intro_phase + max(0.0, dt))
            self._mission_intro_was_active = True
        else:
            self._mission_intro_was_active = False
            self._mission_intro_phase = 0.0

        hint_target = 1.0 if self.mission_show_hint and self.mission_active else 0.0
        hint_speed = 7.5
        if self._mission_hint_fold < hint_target:
            self._mission_hint_fold = min(
                hint_target,
                self._mission_hint_fold + max(0.0, dt) * hint_speed,
            )
        else:
            self._mission_hint_fold = max(
                hint_target,
                self._mission_hint_fold - max(0.0, dt) * hint_speed,
            )

        progress_target = float(max(0, min(len(steps), completed_count)))
        step_total = len(steps)
        if len(self._mission_pip_levels) != step_total:
            self._mission_pip_levels = [0.0] * step_total

        # Mission state is already sampled and matched asynchronously. Do not
        # replay a catch-up wave after the data arrives. The panel must show the
        # newest predicted or confirmed step on this render frame.
        previous_completed = int(self._mission_progress_last_completed)
        self._mission_progress_display = progress_target
        stage_strength = _mission_pip_stage_intensity(completed_count, step_total)
        self._mission_pip_levels = [
            stage_strength if index < completed_count else 0.0
            for index in range(step_total)
        ]
        self._mission_pip_wave_order = []
        self._mission_pip_wave_starts = {}
        self._mission_pip_wave_target = stage_strength
        self._mission_pip_wave_elapsed = 0.0
        self._mission_pip_wave_completed_count = completed_count
        self._mission_progress_mission_id = mission_id
        self._mission_progress_last_completed = completed_count

        if completed_count > previous_completed:
            self._mission_pip_sheen_index = completed_count - 1
            self._mission_pip_sheen_phase = 0.0
        elif completed_count < previous_completed:
            self._mission_pip_sheen_index = -1
            self._mission_pip_sheen_phase = 1.0
            self._mission_strip_complete_sheen_phase = 1.0

        if self._mission_pip_wave_order:
            self._mission_pip_wave_elapsed += max(0.0, dt)
            wave_completed_count = self._mission_pip_wave_completed_count
            for order_index, pip_index in enumerate(self._mission_pip_wave_order):
                if not 0 <= pip_index < len(self._mission_pip_levels):
                    continue
                target = (
                    self._mission_pip_wave_target
                    if pip_index < wave_completed_count
                    else 0.0
                )
                start_level = float(self._mission_pip_wave_starts.get(pip_index, 0.0))
                wave_progress = _mission_pip_wave_progress(
                    self._mission_pip_wave_elapsed,
                    order_index,
                    stagger=0.045,
                    duration=0.15,
                )
                self._mission_pip_levels[pip_index] = (
                    start_level + (target - start_level) * wave_progress
                )

            wave_duration = 0.18 + 0.055 * max(0, len(self._mission_pip_wave_order) - 1)
            if self._mission_pip_wave_elapsed >= wave_duration:
                for pip_index in range(len(self._mission_pip_levels)):
                    self._mission_pip_levels[pip_index] = (
                        self._mission_pip_wave_target
                        if pip_index < wave_completed_count
                        else 0.0
                    )
                self._mission_pip_wave_order = []
                self._mission_pip_wave_starts = {}
                if self._mission_pip_sheen_pending_index >= 0:
                    self._mission_pip_sheen_index = self._mission_pip_sheen_pending_index
                    self._mission_pip_sheen_phase = 0.0
                    self._mission_pip_sheen_pending_index = -1
                if step_total > 0 and wave_completed_count >= step_total:
                    self._mission_strip_complete_sheen_phase = 0.0

        if self._mission_pip_sheen_phase < 1.0:
            self._mission_pip_sheen_phase = min(
                1.0,
                self._mission_pip_sheen_phase + max(0.0, dt),
            )
            if self._mission_pip_sheen_phase >= 1.0:
                self._mission_pip_sheen_index = -1

        if self._mission_strip_complete_sheen_phase < 1.0:
            self._mission_strip_complete_sheen_phase = min(
                1.0,
                self._mission_strip_complete_sheen_phase + max(0.0, dt) * 0.9,
            )

        ANIM_SPEED = 4.0

        for idx in range(len(steps)):
            t = self.step_anim.get(idx, None)
            if t is None:
                t = 1.0 if idx == current_idx else 0.0

            if idx < completed_count:
                t = max(0.0, t - dt * ANIM_SPEED)
            elif idx == current_idx:
                t = min(1.0, t + dt * ANIM_SPEED)
            else:
                t = max(0.0, t - dt * ANIM_SPEED)

            self.step_anim[idx] = t

        if holding:
            self._mission_hold_frames -= 1
            if self._mission_hold_frames <= 0:
                self._mission_hold_data = {}

        if completed_count > self._last_completed_for_bump:
            for done_idx in range(self._last_completed_for_bump, completed_count):
                self._row_bump[done_idx] = 1.0
        self._last_completed_for_bump = completed_count

        for bump_idx in list(self._row_bump):
            self._row_bump[bump_idx] = max(0.0, self._row_bump[bump_idx] - dt * 5.5)
            if self._row_bump[bump_idx] <= 0.0:
                del self._row_bump[bump_idx]

        if holding or self._mission_hold_frames > 0:
            self._toast_phase += max(0.0, dt)
        else:
            self._toast_phase = 0.0

        live_steps = live_data.get("active_mission_steps") or []
        live_step_total = len(live_steps)
        live_completed_count = int(live_data.get("completed_step_count", 0))
        live_confirmed_count = int(
            live_data.get("confirmed_step_count", live_completed_count) or 0
        )
        live_mission_id = str(live_data.get("active_mission_id") or "")
        celebrate_pending = bool(live_data.get("celebrate_pending", False))
        celebrate_token = int(live_data.get("celebrate_token", 0) or 0)

        should_celebrate = (
            celebrate_pending
            and celebrate_token > 0
            and celebrate_token != self._last_clear_seq_seen
        )
        should_celebrate = should_celebrate or (
            live_step_total > 0
            and live_confirmed_count >= live_step_total
            and (
                live_mission_id != self._prev_live_mission_id
                or self._prev_live_completed_count < live_step_total
            )
        )

        if should_celebrate:
            held = dict(live_data)
            held["completed_step_count"] = max(live_completed_count, live_step_total)
            held["confirmed_step_count"] = max(live_confirmed_count, live_step_total)
            held["current_step_index"] = max(0, live_step_total - 1)

            self._mission_hold_data = held
            self._mission_hold_frames = self._mission_hold_duration_frames

            print(
                f"[celebrate fire] mission_id={live_mission_id} "
                f"celebrate_token={celebrate_token}"
            )

            self._toast_phase = 0.0
            self._celebrate_active = False
            self._celebrate_particles = []
            if celebrate_token > 0:
                self._last_clear_seq_seen = celebrate_token
                self._write_mission_celebrate_ack(celebrate_token)

        self._prev_live_mission_id = live_mission_id
        self._prev_live_completed_count = live_confirmed_count
        self._prev_live_step_total = live_step_total



    def _mission_direction_icon(self, digit: str, color: tuple[int, int, int], size: int = 16, charged: bool = False) -> pygame.Surface:
        vectors = {
            "1": (-1.0, 1.0), "2": (0.0, 1.0), "3": (1.0, 1.0),
            "4": (-1.0, 0.0), "5": (0.0, 0.0), "6": (1.0, 0.0),
            "7": (-1.0, -1.0), "8": (0.0, -1.0), "9": (1.0, -1.0),
        }
        size = max(12, int(size))
        surf = pygame.Surface((size, size), pygame.SRCALPHA)
        if charged:
            pygame.draw.rect(surf, (*color, 105), surf.get_rect(), 1, border_radius=3)
        vx, vy = vectors.get(str(digit), (0.0, 0.0))
        cx = size / 2.0
        cy = size / 2.0
        if vx == 0.0 and vy == 0.0:
            pygame.draw.circle(surf, color, (round(cx), round(cy)), max(2, size // 6))
            return surf
        mag = math.hypot(vx, vy) or 1.0
        ux, uy = vx / mag, vy / mag
        pygame.draw.line(
            surf,
            color,
            (cx - ux * size * 0.22, cy - uy * size * 0.22),
            (cx + ux * size * 0.22, cy + uy * size * 0.22),
            max(2, size // 8),
        )
        px, py = -uy, ux
        tip = (cx + ux * size * 0.40, cy + uy * size * 0.40)
        back = (cx + ux * size * 0.10, cy + uy * size * 0.10)
        half = size * 0.16
        pygame.draw.polygon(surf, color, [
            (round(tip[0]), round(tip[1])),
            (round(back[0] + px * half), round(back[1] + py * half)),
            (round(back[0] - px * half), round(back[1] - py * half)),
        ])
        return surf

    def _mission_charge_direction_chip(
        self,
        digit: str,
        color: tuple[int, int, int],
    ) -> pygame.Surface:
        """Render a charge as an explicit HOLD capsule plus the held direction."""
        label_font = self.smallfont or self.font
        hold = label_font.render("HOLD", True, (218, 228, 242))
        arrow = self._mission_direction_icon(digit, color, 17, False)
        width = hold.get_width() + arrow.get_width() + 15
        height = max(22, hold.get_height() + 6, arrow.get_height() + 4)
        surf = pygame.Surface((width, height), pygame.SRCALPHA)
        pygame.draw.rect(surf, (10, 24, 42, 232), surf.get_rect(), border_radius=5)
        pygame.draw.rect(surf, (*color, 225), surf.get_rect(), 1, border_radius=5)
        pygame.draw.rect(surf, (*color, 110), (2, 2, 4, height - 4), border_radius=2)
        surf.blit(hold, (8, (height - hold.get_height()) // 2))
        surf.blit(arrow, (width - arrow.get_width() - 4, (height - arrow.get_height()) // 2))
        return surf

    def _mission_rotation_chip(
        self,
        color: tuple[int, int, int],
    ) -> pygame.Surface:
        label_font = self.smallfont or self.font
        label = label_font.render("360", True, (242, 246, 252))
        size = max(24, label.get_width() + 14)
        surf = pygame.Surface((size, 22), pygame.SRCALPHA)
        pygame.draw.rect(surf, (10, 24, 42, 232), surf.get_rect(), border_radius=11)
        pygame.draw.rect(surf, (*color, 225), surf.get_rect(), 1, border_radius=11)
        pygame.draw.arc(surf, (*color, 210), (3, 3, 16, 16), 0.35, 5.65, 2)
        pygame.draw.polygon(surf, color, [(16, 2), (20, 5), (15, 7)])
        surf.blit(label, (size - label.get_width() - 5, (22 - label.get_height()) // 2))
        return surf

    def _render_mission_input_notation(self, notation: str, color: tuple[int, int, int]) -> pygame.Surface | None:
        notation = str(notation or "").strip().upper()
        if not notation:
            return None

        jump_match = re.fullmatch(r"J\.?([ABC])", notation)
        if jump_match:
            jump_text = f"j.{jump_match.group(1)}"
            label = self.smallfont.render(jump_text, True, (245, 248, 255))
            box = pygame.Surface(
                (label.get_width() + 10, max(16, label.get_height() + 4)),
                pygame.SRCALPHA,
            )
            pygame.draw.rect(box, (*color, 170), box.get_rect(), border_radius=4)
            pygame.draw.rect(box, color, box.get_rect(), 1, border_radius=4)
            box.blit(
                label,
                (
                    (box.get_width() - label.get_width()) // 2,
                    (box.get_height() - label.get_height()) // 2,
                ),
            )
            return box

        if re.fullmatch(r"J\.?2C", notation):
            jump = self.smallfont.render("j.", True, (202, 212, 228))
            down = self._mission_direction_icon("2", color, 17, False)
            button = self.smallfont.render("C", True, (245, 248, 255))
            chip = pygame.Surface((button.get_width() + 8, max(17, button.get_height() + 4)), pygame.SRCALPHA)
            pygame.draw.rect(chip, (*color, 170), chip.get_rect(), border_radius=4)
            pygame.draw.rect(chip, color, chip.get_rect(), 1, border_radius=4)
            chip.blit(button, ((chip.get_width() - button.get_width()) // 2, (chip.get_height() - button.get_height()) // 2))
            parts = [jump, down, chip]
            gap = 3
            width = sum(part.get_width() for part in parts) + gap * 2
            height = max(part.get_height() for part in parts)
            surface = pygame.Surface((width, height), pygame.SRCALPHA)
            dx = 0
            for part in parts:
                surface.blit(part, (dx, (height - part.get_height()) // 2))
                dx += part.get_width() + gap
            return surface

        if notation in {"TAUNT", "TAUNT(T)", "T"}:
            word = self.smallfont.render("TAUNT", True, (202, 212, 228))
            key = self.smallfont.render("T", True, (245, 248, 255))
            chip = pygame.Surface((key.get_width() + 8, max(17, key.get_height() + 4)), pygame.SRCALPHA)
            pygame.draw.rect(chip, (*color, 170), chip.get_rect(), border_radius=4)
            pygame.draw.rect(chip, color, chip.get_rect(), 1, border_radius=4)
            chip.blit(key, ((chip.get_width() - key.get_width()) // 2, (chip.get_height() - key.get_height()) // 2))
            surface = pygame.Surface((word.get_width() + chip.get_width() + 4, max(word.get_height(), chip.get_height())), pygame.SRCALPHA)
            surface.blit(word, (0, (surface.get_height() - word.get_height()) // 2))
            surface.blit(chip, (word.get_width() + 4, (surface.get_height() - chip.get_height()) // 2))
            return surface

        tokens = re.findall(
            r"360|\[[1-9]\]|ATK|XX|HOLD|RELEASE|MASH|CHARGE|AIR|THEN|[1-9]|[ABCLMHPTX]|\+|/|>|X\d+",
            notation.replace("×", "X"),
        )
        tokens = [token for token in tokens if token.strip()]
        if not tokens:
            return None
        parts = []
        gap = 3
        for token in tokens:
            charged = token.startswith("[") and token.endswith("]")
            bare = token[1:-1] if charged else token
            if token == "360":
                parts.append(self._mission_rotation_chip(color))
            elif bare in "123456789" and charged:
                parts.append(self._mission_charge_direction_chip(bare, color))
            elif bare in "123456789":
                parts.append(self._mission_direction_icon(bare, color, 16, False))
            elif bare in {"A", "B", "C", "L", "M", "H", "P", "T", "X", "XX", "ATK"}:
                label = self.smallfont.render(bare, True, (245, 248, 255))
                box = pygame.Surface((label.get_width() + 8, max(16, label.get_height() + 4)), pygame.SRCALPHA)
                pygame.draw.rect(box, (*color, 170), box.get_rect(), border_radius=4)
                pygame.draw.rect(box, color, box.get_rect(), 1, border_radius=4)
                box.blit(label, ((box.get_width() - label.get_width()) // 2, (box.get_height() - label.get_height()) // 2))
                parts.append(box)
            else:
                parts.append(self.smallfont.render(bare, True, (188, 196, 214)))
        width = sum(part.get_width() for part in parts) + gap * max(0, len(parts) - 1)
        height = max(part.get_height() for part in parts)
        surface = pygame.Surface((max(1, width), max(1, height)), pygame.SRCALPHA)
        dx = 0
        for part in parts:
            surface.blit(part, (dx, (height - part.get_height()) // 2))
            dx += part.get_width() + gap
        return surface

    def _draw_active_mission_panel(
        self,
        data: dict,
        character: str,
        theme_color: tuple[int, int, int],
        mission_name: str,
        mission_notes: str,
        steps: list,
        selector_hint: str,
        goal_progress_type: object,
        goal_target_state: object,
        goal_current_frames: int,
        goal_needed_frames: int,
        goal_timer_active: bool,
        transition_mode: str = "idle",
        transition_progress: float = 1.0,
    ) -> None:
        """Draw the flatter, spaced V19 Mission Mode panel with staged row entry."""
        if self.screen is None or self.font is None or self.smallfont is None:
            return

        completed_count = int(data.get("completed_step_count", 0) or 0)
        confirmed_count = int(
            data.get("confirmed_step_count", completed_count) or 0
        )
        confirmed_count = max(0, min(completed_count, confirmed_count))
        predicted_count = max(
            0,
            int(
                data.get(
                    "predicted_step_count",
                    max(0, completed_count - confirmed_count),
                )
                or 0
            ),
        )
        current_index = int(data.get("current_step_index", 0) or 0)
        total_steps = len(steps)
        mission_done = total_steps > 0 and confirmed_count >= total_steps
        completion_hold_active = bool(
            mission_done and self._mission_hold_frames > 0
        )

        transition_mode = str(transition_mode or "idle").lower()
        transition_progress = max(0.0, min(1.0, float(transition_progress)))
        is_exiting = transition_mode == "out" and not completion_hold_active
        exit_progress = _mission_intro_ease(transition_progress) if is_exiting else 0.0

        def entry_progress(delay: float, duration: float = 0.18) -> float:
            if is_exiting:
                return 1.0
            return _mission_element_intro_progress(
                self._mission_intro_phase,
                delay,
                duration,
            )

        def exit_visibility(order: int = 0) -> float:
            if not is_exiting:
                return 1.0
            return 1.0 - _mission_element_exit_progress(
                transition_progress,
                order,
                0.045,
            )

        def blit_faded(
            target: pygame.Surface,
            source: pygame.Surface,
            position: tuple[int, int],
            visibility: float,
            offset: tuple[int, int] = (0, 0),
        ) -> None:
            visibility = max(0.0, min(1.0, float(visibility)))
            if visibility <= 0.0:
                return
            layer = source.copy()
            layer.set_alpha(int(round(255 * visibility)))
            px = int(position[0] + (1.0 - visibility) * offset[0])
            py = int(position[1] + (1.0 - visibility) * offset[1])
            target.blit(layer, (px, py))

        pad = 9
        row_gap = 3

        # Keep the mission panel compact while still leaving enough room for
        # move labels and input chips. Long guidance now wraps vertically.
        horizontal_margin = max(10, min(24, self.w // 36))
        panel_w = min(
            700,
            max(360, int(self.w * 0.40)),
            max(260, self.w - horizontal_margin * 2),
        )
        inner_w = max(140, panel_w - pad * 2)

        header_chip_h = max(27, self.font.get_height() + 8)
        header_h = header_chip_h + 6
        row_h = max(27, self.smallfont.get_height() + 9)
        timer_h = 44 if (
            goal_progress_type == "state_duration"
            and goal_target_state == "blockstun"
            and goal_needed_frames > 0
        ) else 0

        hint_button_text = "Hide Hint" if self.mission_show_hint else "Show Hint"
        all_button_text = "Show Less" if self.mission_show_all else "Show All"
        challenge_button_specs = [
            (all_button_text, "all"),
            (hint_button_text, "hint"),
        ]
        challenge_button_h = 28
        challenge_button_widths = [
            max(82, self.smallfont.size(text)[0] + 20)
            for text, _kind in challenge_button_specs
        ]
        challenge_buttons_w = sum(challenge_button_widths) + 7

        mission_chip_text = str(mission_name or "No mission loaded").strip()
        mission_chip_max_w = max(92, inner_w - challenge_buttons_w - 18)
        mission_chip_display = mission_chip_text
        while (
            self.font.size(mission_chip_display)[0] + 22 > mission_chip_max_w
            and len(mission_chip_display) > 4
        ):
            mission_chip_display = mission_chip_display[:-4].rstrip() + "..."
        mission_chip_surf = self.font.render(
            mission_chip_display,
            True,
            (232, 239, 249),
        )
        mission_chip_h = max(28, mission_chip_surf.get_height() + 8)

        instruction_text = str(
            selector_hint or "Down, Down, Taunt: Open Mission Select"
        ).strip()
        instruction_lines = self._wrap_text_lines(
            instruction_text,
            self.smallfont,
            max(96, inner_w - 20),
        )
        instruction_surfs = [
            self.smallfont.render(line, True, (202, 213, 230))
            for line in instruction_lines
        ]
        instruction_gap = 2
        challenge_top_h = max(mission_chip_h, challenge_button_h)
        instruction_block_h = (
            6
            + sum(surf.get_height() for surf in instruction_surfs)
            + instruction_gap * max(0, len(instruction_surfs) - 1)
            if instruction_surfs
            else 0
        )
        challenge_h = 8 + challenge_top_h + instruction_block_h + 8

        hint_text = str(mission_notes or "").strip()
        note_lines = self._wrap_text_lines(
            hint_text or "No hint text is available for this challenge.",
            self.smallfont,
            max(80, inner_w - 34),
        )

        # Measure the exact rendered surfaces instead of estimating with
        # Font.get_height(). Consolas can return a rendered surface one or two
        # pixels taller than get_height(), which previously made the final
        # wrapped line fail the note box boundary check and disappear.
        note_line_gap = 2
        hint_label_surf = self.smallfont.render("HINT", True, (112, 166, 238))
        note_line_surfs = [
            self.smallfont.render(line, True, (215, 224, 239))
            for line in note_lines
        ]
        note_h_full = (
            8
            + hint_label_surf.get_height()
            + 4
            + sum(surf.get_height() for surf in note_line_surfs)
            + note_line_gap * max(0, len(note_line_surfs) - 1)
            + 8
        )
        note_fold = max(0.0, min(1.0, self._mission_hint_fold))
        note_h = int(round(note_h_full * note_fold))
        note_gap = 8 if note_h > 0 else 0

        # Reserve room for the full expanded hint before choosing how many
        # route rows can be shown. At short resolutions, route rows collapse
        # first instead of allowing the hint to fall below the screen.
        outer_margin = max(8, min(18, self.h // 40))
        available_total_h = max(120, self.h - outer_margin * 2)
        base_panel_h = pad + header_h + 7 + challenge_h + 8 + timer_h + pad
        requested_limit = total_steps if self.mission_show_all else min(6, total_steps)

        # A footer is needed whenever physical space forces a shortened view.
        provisional_footer_h = 18 if total_steps > requested_limit else 6
        row_budget = (
            available_total_h
            - note_h
            - note_gap
            - base_panel_h
            - provisional_footer_h
        )
        max_rows_fit = max(
            0 if note_fold > 0.01 else 1,
            int((row_budget + row_gap) // max(1, row_h + row_gap)),
        )
        visible_limit = min(requested_limit, max_rows_fit)
        if total_steps > 0 and visible_limit <= 0 and note_fold <= 0.01:
            visible_limit = 1

        collapsed_scroll = total_steps > visible_limit
        max_start = max(0, total_steps - max(1, visible_limit))
        if collapsed_scroll:
            if not self.mission_show_all and visible_limit == 6:
                scroll_pos = max(
                    0.0,
                    min(float(max_start), float(self._mission_scroll_pos)),
                )
            else:
                scroll_pos = float(
                    _mission_scroll_target(
                        current_index,
                        total_steps,
                        max(1, visible_limit),
                    )
                )
            visible_start = max(0, int(math.floor(scroll_pos)))
            visible_end = min(
                total_steps,
                visible_start + max(1, visible_limit) + 1,
            )
            visible_rows = visible_limit
            footer_start = min(max_start, max(0, int(round(scroll_pos))))
            footer_end = min(total_steps, footer_start + visible_limit)
        else:
            scroll_pos = 0.0
            visible_start = 0
            visible_end = total_steps
            visible_rows = total_steps
            footer_start = 0
            footer_end = total_steps

        visible_steps = list(
            enumerate(steps[visible_start:visible_end], start=visible_start)
        )

        list_h = max(
            0,
            visible_rows * row_h + max(0, visible_rows - 1) * row_gap,
        )
        footer_h = 18 if collapsed_scroll else 6
        completion_visible = bool(
            mission_done
            and (self._mission_hold_frames > 0 or self._toast_phase > 0.0)
        )
        route_body_h = list_h + footer_h
        if completion_visible:
            route_body_h = max(route_body_h, MISSION_COMPLETE_MIN_BODY_HEIGHT)
        panel_h = base_panel_h + note_h + note_gap + route_body_h

        panel_intro = _mission_panel_intro_progress(self._mission_intro_phase)
        if is_exiting or completion_hold_active:
            panel_intro = 1.0
        total_draw_h = panel_h
        max_panel_y = max(outer_margin, self.h - total_draw_h - outer_margin)
        preferred_y = max(outer_margin, int(self.h * 0.06))
        panel_y_final = min(preferred_y, max_panel_y)
        panel_x = max(horizontal_margin, (self.w - panel_w) // 2)
        panel_x -= int(round(exit_progress * 24.0))
        panel_y = min(
            max_panel_y,
            panel_y_final + int(round((1.0 - panel_intro) * 18.0)),
        )
        self.mission_panel_rect = pygame.Rect(panel_x, panel_y, panel_w, panel_h)

        panel = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
        cut = 10
        panel_points = [
            (cut, 0), (panel_w - cut, 0), (panel_w, cut),
            (panel_w, panel_h - cut), (panel_w - cut, panel_h),
            (cut, panel_h), (0, panel_h - cut), (0, cut),
        ]
        pygame.draw.polygon(panel, (7, 14, 27, 238), panel_points)
        pygame.draw.polygon(panel, (58, 72, 101, 235), panel_points, 1)
        pygame.draw.line(panel, (75, 138, 225, 220), (cut + 16, 3), (panel_w // 2 - 8, 3), 2)
        pygame.draw.line(panel, (104, 77, 171, 205), (panel_w // 2 + 8, 3), (panel_w - cut - 16, 3), 2)

        # Styled character identity and compact completion status.
        progress_value = self.font.render(
            f"{completed_count}/{total_steps}",
            True,
            (115, 187, 255),
        )
        progress_status = (
            "TRACKING"
            if predicted_count > 0
            else ("COMPLETE" if mission_done else "CONFIRMED")
        )
        progress_label = self.smallfont.render(
            progress_status,
            True,
            (171, 209, 244) if predicted_count > 0 else (198, 211, 229),
        )
        progress_chip_w = (
            progress_value.get_width()
            + progress_label.get_width()
            + 25
        )
        progress_chip_rect = pygame.Rect(
            panel_w - pad - progress_chip_w,
            pad,
            progress_chip_w,
            header_chip_h,
        )
        progress_chip_layer = pygame.Surface(
            progress_chip_rect.size,
            pygame.SRCALPHA,
        )
        pygame.draw.rect(
            progress_chip_layer,
            (9, 24, 42, 235),
            progress_chip_layer.get_rect(),
            border_radius=6,
        )
        pygame.draw.rect(
            progress_chip_layer,
            (69, 125, 194),
            progress_chip_layer.get_rect(),
            1,
            border_radius=6,
        )
        pygame.draw.rect(
            progress_chip_layer,
            (91, 172, 246),
            (0, 3, 3, progress_chip_rect.height - 6),
            border_radius=2,
        )
        progress_text_x = 10
        progress_chip_layer.blit(
            progress_value,
            (
                progress_text_x,
                progress_chip_rect.height // 2 - progress_value.get_height() // 2,
            ),
        )
        progress_chip_layer.blit(
            progress_label,
            (
                progress_text_x + progress_value.get_width() + 7,
                progress_chip_rect.height // 2 - progress_label.get_height() // 2,
            ),
        )
        progress_chip_vis = min(
            entry_progress(0.035, 0.17),
            exit_visibility(4),
        )
        blit_faded(
            panel,
            progress_chip_layer,
            progress_chip_rect.topleft,
            progress_chip_vis,
            (18, 0),
        )

        mode_surf = self.smallfont.render(
            "MISSION MODE",
            True,
            (174, 190, 215),
        )
        slot_surf = self.smallfont.render(
            str(data.get("slot") or self.mission_slot or ""),
            True,
            (221, 229, 241),
        )
        slot_chip_w = slot_surf.get_width() + 14
        reserved_left_w = (
            progress_chip_rect.x
            - pad
            - mode_surf.get_width()
            - slot_chip_w
            - 22
        )
        character_display = str(character or "Unknown")
        while (
            self.font.size(character_display)[0] + 20 > max(70, reserved_left_w)
            and len(character_display) > 4
        ):
            character_display = character_display[:-4].rstrip() + "..."
        character_surf = self.font.render(
            character_display.upper(),
            True,
            theme_color,
        )
        character_chip_w = min(
            max(74, character_surf.get_width() + 20),
            max(74, reserved_left_w),
        )
        character_chip_rect = pygame.Rect(
            pad,
            pad,
            character_chip_w,
            header_chip_h,
        )
        character_chip_layer = pygame.Surface(
            character_chip_rect.size,
            pygame.SRCALPHA,
        )
        pygame.draw.rect(
            character_chip_layer,
            (10, 22, 38, 236),
            character_chip_layer.get_rect(),
            border_radius=6,
        )
        pygame.draw.rect(
            character_chip_layer,
            theme_color,
            character_chip_layer.get_rect(),
            1,
            border_radius=6,
        )
        pygame.draw.rect(
            character_chip_layer,
            theme_color,
            (0, 3, 4, character_chip_rect.height - 6),
            border_radius=2,
        )
        character_chip_layer.blit(
            character_surf,
            (
                11,
                character_chip_rect.height // 2 - character_surf.get_height() // 2,
            ),
        )
        character_chip_vis = min(
            entry_progress(0.0, 0.17),
            exit_visibility(5),
        )
        blit_faded(
            panel,
            character_chip_layer,
            character_chip_rect.topleft,
            character_chip_vis,
            (-18, 0),
        )

        mode_x = character_chip_rect.right + 9
        available_mode_right = progress_chip_rect.x - 8
        meta_vis = min(entry_progress(0.055, 0.16), exit_visibility(4))
        if mode_x + mode_surf.get_width() + slot_chip_w + 7 <= available_mode_right:
            blit_faded(
                panel,
                mode_surf,
                (
                    mode_x,
                    pad + header_chip_h // 2 - mode_surf.get_height() // 2,
                ),
                meta_vis,
                (0, -6),
            )
            slot_x = mode_x + mode_surf.get_width() + 7
        else:
            slot_x = mode_x

        slot_chip_rect = pygame.Rect(
            slot_x,
            pad + (header_chip_h - max(20, slot_surf.get_height() + 6)) // 2,
            slot_chip_w,
            max(20, slot_surf.get_height() + 6),
        )
        if slot_chip_rect.right <= available_mode_right:
            slot_chip_layer = pygame.Surface(slot_chip_rect.size, pygame.SRCALPHA)
            pygame.draw.rect(
                slot_chip_layer,
                (23, 34, 52),
                slot_chip_layer.get_rect(),
                border_radius=5,
            )
            pygame.draw.rect(
                slot_chip_layer,
                (65, 81, 111),
                slot_chip_layer.get_rect(),
                1,
                border_radius=5,
            )
            slot_chip_layer.blit(
                slot_surf,
                (
                    slot_chip_rect.width // 2 - slot_surf.get_width() // 2,
                    slot_chip_rect.height // 2 - slot_surf.get_height() // 2,
                ),
            )
            blit_faded(
                panel,
                slot_chip_layer,
                slot_chip_rect.topleft,
                meta_vis,
                (0, -6),
            )

        segment_y = pad + header_h
        segment_gap = 2
        segment_count = max(1, total_steps)
        segment_w = max(4, (inner_w - segment_gap * (segment_count - 1)) // segment_count)
        progress_display = max(0.0, min(float(segment_count), float(self._mission_progress_display)))
        strip_h = 10
        strip_rect = pygame.Rect(pad, segment_y - 2, inner_w, strip_h)
        pygame.draw.rect(panel, (21, 28, 40), strip_rect, border_radius=3)
        pygame.draw.rect(panel, (49, 62, 87), strip_rect, 1, border_radius=3)

        pip_levels = list(self._mission_pip_levels[:segment_count])
        if len(pip_levels) < segment_count:
            fallback = _mission_pip_stage_intensity(completed_count, segment_count)
            pip_levels.extend(
                fallback if index < completed_count else 0.0
                for index in range(len(pip_levels), segment_count)
            )

        complete_level = pip_levels[-1] if mission_done and pip_levels else 0.0
        bright_base = (29, 37, 52)
        bright_peak = (98, 181, 255)
        if complete_level > 0.0:
            strip_fill = tuple(
                int(bright_base[i] + (bright_peak[i] - bright_base[i]) * complete_level)
                for i in range(3)
            )
            pygame.draw.rect(panel, strip_fill, strip_rect.inflate(-2, -2), border_radius=3)
            strip_glow = pygame.Surface((strip_rect.width, strip_rect.height + 8), pygame.SRCALPHA)
            pygame.draw.rect(
                strip_glow,
                (130, 205, 255, int(90 * complete_level)),
                strip_glow.get_rect(),
                border_radius=4,
            )
            panel.blit(strip_glow, (strip_rect.x, strip_rect.y - 4), special_flags=pygame.BLEND_RGBA_ADD)

        sx = pad
        for seg in range(segment_count):
            seg_rect = pygame.Rect(sx, segment_y, segment_w, 5)
            strength = max(0.0, min(1.0, float(pip_levels[seg])))
            is_predicted_segment = confirmed_count <= seg < completed_count
            if strength > 0.001:
                base = (29, 37, 52)
                bright = (
                    (145, 121, 236)
                    if is_predicted_segment
                    else (98, 181, 255)
                )
                fill = tuple(int(base[i] + (bright[i] - base[i]) * strength) for i in range(3))
                pygame.draw.rect(panel, fill, seg_rect, border_radius=2)

                glow_strength = max(0.0, (strength - 0.42) / 0.58)
                if glow_strength > 0.0:
                    glow = pygame.Surface((seg_rect.width, seg_rect.height + 6), pygame.SRCALPHA)
                    pygame.draw.rect(
                        glow,
                        (
                            166,
                            139,
                            247,
                            int(90 * glow_strength),
                        ) if is_predicted_segment else (
                            115,
                            190,
                            255,
                            int(115 * glow_strength),
                        ),
                        glow.get_rect(),
                        border_radius=2,
                    )
                    panel.blit(glow, (seg_rect.x, seg_rect.y - 3), special_flags=pygame.BLEND_RGBA_ADD)

                if seg == self._mission_pip_sheen_index and strength >= 0.001:
                    sheen_t = _mission_pip_sheen_progress(self._mission_pip_sheen_phase)
                    if sheen_t < 1.0:
                        sheen = pygame.Surface((seg_rect.width + 14, seg_rect.height + 8), pygame.SRCALPHA)
                        ridge_x = int(round(-8 + (seg_rect.width + 18) * sheen_t))
                        ridge_w = max(4, seg_rect.width // 5)
                        alpha = int(110 + 120 * strength)
                        pygame.draw.polygon(sheen, (215, 240, 255, alpha // 3), [
                            (ridge_x - ridge_w, 0),
                            (ridge_x + 1, 0),
                            (ridge_x + ridge_w, sheen.get_height()),
                            (ridge_x - 1, sheen.get_height()),
                        ])
                        pygame.draw.line(
                            sheen,
                            (245, 252, 255, alpha),
                            (ridge_x, 0),
                            (ridge_x + ridge_w, sheen.get_height()),
                            2,
                        )
                        panel.blit(
                            sheen,
                            (seg_rect.x - 7, seg_rect.y - 4),
                            special_flags=pygame.BLEND_RGBA_ADD,
                        )
            else:
                pygame.draw.rect(panel, (28, 34, 47), seg_rect, border_radius=2)
            sx += segment_w + segment_gap

        if mission_done and complete_level >= 0.999:
            strip_sheen_t = _mission_pip_sheen_progress(self._mission_strip_complete_sheen_phase)
            if strip_sheen_t < 1.0:
                sheen = pygame.Surface((strip_rect.width + 24, strip_rect.height + 10), pygame.SRCALPHA)
                ridge_x = int(round(-12 + (strip_rect.width + 24) * strip_sheen_t))
                ridge_w = max(10, strip_rect.width // 10)
                pygame.draw.polygon(sheen, (220, 241, 255, 70), [
                    (ridge_x - ridge_w, 0),
                    (ridge_x + 2, 0),
                    (ridge_x + ridge_w, sheen.get_height()),
                    (ridge_x - 2, sheen.get_height()),
                ])
                pygame.draw.line(
                    sheen,
                    (250, 253, 255, 190),
                    (ridge_x, 0),
                    (ridge_x + ridge_w, sheen.get_height()),
                    3,
                )
                panel.blit(sheen, (strip_rect.x - 12, strip_rect.y - 5), special_flags=pygame.BLEND_RGBA_ADD)

            badge_progress = _mission_intro_ease(
                (self._mission_strip_complete_sheen_phase - 0.18) / 0.22
            )
            if badge_progress > 0.0:
                badge_font = pygame.font.SysFont("consolas", 11, bold=True)
                badge_text = badge_font.render("MISSION COMPLETE", True, (233, 243, 252))
                badge_w = min(inner_w - 12, badge_text.get_width() + 18)
                badge_h = 12
                badge_rect = pygame.Rect(0, 0, badge_w, badge_h)
                badge_rect.center = strip_rect.center
                badge_layer = pygame.Surface((badge_rect.width, badge_rect.height), pygame.SRCALPHA)
                pygame.draw.rect(badge_layer, (8, 35, 49, int(215 * badge_progress)), badge_layer.get_rect(), border_radius=5)
                pygame.draw.rect(badge_layer, (93, 193, 224, int(210 * badge_progress)), badge_layer.get_rect(), 1, border_radius=5)
                badge_layer.blit(
                    badge_text,
                    (
                        badge_layer.get_width() // 2 - badge_text.get_width() // 2,
                        badge_layer.get_height() // 2 - badge_text.get_height() // 2,
                    ),
                )
                badge_layer.set_alpha(int(255 * badge_progress))
                panel.blit(badge_layer, badge_rect.topleft)

        strip_vis = min(entry_progress(0.075, 0.17), exit_visibility(3))
        if strip_vis < 0.999:
            strip_cover = pygame.Surface((strip_rect.width, strip_rect.height + 8), pygame.SRCALPHA)
            strip_cover.fill((7, 14, 27, int(round(255 * (1.0 - strip_vis)))))
            panel.blit(strip_cover, (strip_rect.x, strip_rect.y - 4))

        # Mission identity chip, controls, and a full-width wrapped command hint.
        challenge_y = segment_y + 14
        challenge_rect = pygame.Rect(pad, challenge_y, inner_w, challenge_h)
        pygame.draw.rect(panel, (11, 22, 39, 230), challenge_rect, border_radius=5)
        pygame.draw.rect(panel, (50, 65, 93, 220), challenge_rect, 1, border_radius=5)
        pygame.draw.rect(
            panel,
            (74, 135, 219),
            (challenge_rect.x, challenge_rect.y, 3, challenge_rect.height),
            border_radius=2,
        )

        button_y = challenge_rect.y + 8
        button_x = (
            challenge_rect.right
            - 8
            - sum(challenge_button_widths)
            - 7
        )
        mission_chip_rect = pygame.Rect(
            challenge_rect.x + 8,
            challenge_rect.y + 8,
            max(80, button_x - challenge_rect.x - 16),
            mission_chip_h,
        )
        mission_chip_layer = pygame.Surface(mission_chip_rect.size, pygame.SRCALPHA)
        pygame.draw.rect(
            mission_chip_layer,
            (16, 31, 52),
            mission_chip_layer.get_rect(),
            border_radius=5,
        )
        pygame.draw.rect(
            mission_chip_layer,
            theme_color,
            mission_chip_layer.get_rect(),
            1,
            border_radius=5,
        )
        pygame.draw.rect(
            mission_chip_layer,
            theme_color,
            (0, 3, 4, mission_chip_rect.height - 6),
            border_radius=2,
        )
        mission_chip_layer.blit(
            mission_chip_surf,
            (
                11,
                mission_chip_rect.height // 2 - mission_chip_surf.get_height() // 2,
            ),
        )
        mission_chip_vis = min(
            entry_progress(0.095, 0.17),
            exit_visibility(2),
        )
        blit_faded(
            panel,
            mission_chip_layer,
            mission_chip_rect.topleft,
            mission_chip_vis,
            (-16, 0),
        )

        for (text, kind), button_w in zip(
            challenge_button_specs,
            challenge_button_widths,
        ):
            rect = pygame.Rect(
                button_x,
                button_y,
                button_w,
                challenge_button_h,
            )
            hovered = rect.move(panel_x, panel_y).collidepoint(
                pygame.mouse.get_pos()
            )
            active = self.mission_show_all if kind == "all" else self.mission_show_hint
            fill = (23, 39, 63) if not active else (37, 45, 78)
            if hovered:
                fill = tuple(min(255, c + 12) for c in fill)
            button_layer = pygame.Surface(rect.size, pygame.SRCALPHA)
            pygame.draw.rect(button_layer, fill, button_layer.get_rect(), border_radius=4)
            pygame.draw.rect(
                button_layer,
                (91, 111, 148),
                button_layer.get_rect(),
                1,
                border_radius=4,
            )
            text_surf = self.smallfont.render(text, True, (228, 233, 242))
            button_layer.blit(
                text_surf,
                (
                    rect.width // 2 - text_surf.get_width() // 2,
                    rect.height // 2 - text_surf.get_height() // 2,
                ),
            )
            button_order = 0 if kind == "hint" else 1
            button_vis = min(
                entry_progress(0.115 + button_order * 0.035, 0.16),
                exit_visibility(1 + button_order),
            )
            animated_button_x = rect.x + int(round((1.0 - button_vis) * 14.0))
            blit_faded(
                panel,
                button_layer,
                (rect.x, rect.y),
                button_vis,
                (14, 0),
            )
            global_rect = pygame.Rect(
                panel_x + animated_button_x,
                panel_y + rect.y,
                rect.width,
                rect.height,
            )
            if not is_exiting and button_vis >= 0.90:
                if kind == "all":
                    self.mission_toggle_rect = global_rect
                else:
                    self.mission_hint_rect = global_rect
            button_x += button_w + 7

        instruction_y = challenge_rect.y + 8 + challenge_top_h + 6
        instruction_x = challenge_rect.x + 10
        instruction_vis = min(
            entry_progress(0.15, 0.18),
            exit_visibility(1),
        )
        for instruction_index, instruction_surf in enumerate(instruction_surfs):
            if is_exiting:
                line_vis = instruction_vis
            else:
                line_vis = instruction_vis * _mission_element_intro_progress(
                    self._mission_intro_phase,
                    0.15 + instruction_index * 0.025,
                    0.16,
                )
            line_vis = max(0.0, min(1.0, line_vis))
            blit_faded(
                panel,
                instruction_surf,
                (instruction_x, instruction_y),
                line_vis,
                (0, 6),
            )
            instruction_y += instruction_surf.get_height() + instruction_gap

        cursor_y = challenge_rect.bottom + 8

        if timer_h:
            timer_rect = pygame.Rect(pad, cursor_y, inner_w, timer_h - 8)
            pygame.draw.rect(panel, (10, 22, 38), timer_rect, border_radius=4)
            pygame.draw.rect(panel, (52, 71, 102), timer_rect, 1, border_radius=4)
            label = self.smallfont.render(
                f"Blockstun Timer  {goal_current_frames}/{goal_needed_frames}f",
                True,
                (226, 231, 240),
            )
            panel.blit(label, (timer_rect.x + 8, timer_rect.y + 4))
            bar = pygame.Rect(timer_rect.x + 8, timer_rect.bottom - 12, timer_rect.width - 16, 6)
            pygame.draw.rect(panel, (34, 44, 62), bar, border_radius=2)
            frac = max(0.0, min(1.0, goal_current_frames / float(max(1, goal_needed_frames))))
            if frac > 0.0:
                color = (76, 151, 230) if goal_timer_active else (76, 92, 120)
                pygame.draw.rect(panel, color, (bar.x, bar.y, int(bar.width * frac), bar.height), border_radius=2)
            cursor_y += timer_h

        if note_h > 0:
            note_rect = pygame.Rect(pad, cursor_y, inner_w, note_h)
            note_layer = pygame.Surface(note_rect.size, pygame.SRCALPHA)
            pygame.draw.rect(note_layer, (8, 17, 31, 242), note_layer.get_rect(), border_radius=6)
            pygame.draw.rect(note_layer, (58, 78, 111, 225), note_layer.get_rect(), 1, border_radius=6)
            pygame.draw.line(
                note_layer,
                (78, 142, 225, 220),
                (12, 1),
                (note_rect.width - 12, 1),
                2,
            )
            note_layer.blit(hint_label_surf, (12, 8))
            note_y = 8 + hint_label_surf.get_height() + 4
            for surf in note_line_surfs:
                note_layer.blit(surf, (12, note_y))
                note_y += surf.get_height() + note_line_gap
            note_vis = min(
                entry_progress(0.175, 0.18),
                exit_visibility(0),
            )
            blit_faded(
                panel,
                note_layer,
                note_rect.topleft,
                note_vis,
                (0, 8),
            )
            cursor_y += note_h + note_gap

        list_y = cursor_y
        list_h = max(0, visible_rows * row_h + max(0, visible_rows - 1) * row_gap)
        old_clip = panel.get_clip()
        compact_complete = bool(mission_done and (self._mission_hold_frames > 0 or self._toast_phase > 0.0))
        if collapsed_scroll and not compact_complete:
            panel.set_clip(pygame.Rect(pad, list_y, inner_w, list_h))

        if compact_complete:
            hold_elapsed_frames = max(
                0,
                self._mission_hold_duration_frames - self._mission_hold_frames,
            )
            badge_alpha, slide_progress, title_lock, sheen_progress = _mission_complete_badge_progress(
                hold_elapsed_frames,
                self._mission_hold_duration_frames,
            )
            complete_rect = pygame.Rect(
                pad,
                list_y,
                inner_w,
                max(MISSION_COMPLETE_MIN_BODY_HEIGHT - 2, list_h + footer_h - 2),
            )
            complete_layer = pygame.Surface((complete_rect.width, complete_rect.height), pygame.SRCALPHA)

            # A restrained navy-to-blue gradient gives the badge depth without
            # taking over the whole HUD.
            height = max(1, complete_layer.get_height())
            for row_y in range(height):
                frac = row_y / max(1, height - 1)
                center = max(0.0, 1.0 - abs(frac - 0.48) * 2.25)
                r = int(7 + 8 * frac + 4 * center)
                g = int(15 + 18 * frac + 8 * center)
                b = int(30 + 25 * frac + 14 * center)
                pygame.draw.line(
                    complete_layer,
                    (r, g, b, int(242 * badge_alpha)),
                    (0, row_y),
                    (complete_layer.get_width(), row_y),
                )

            pygame.draw.rect(
                complete_layer,
                (65, 88, 124, int(220 * badge_alpha)),
                complete_layer.get_rect(),
                1,
                border_radius=7,
            )

            # Side wings slide inward and lock around the title.
            wing_offset = int(round((1.0 - slide_progress) * 34.0))
            wing_y = complete_layer.get_height() // 2
            left_end = complete_layer.get_width() // 2 - 118
            right_start = complete_layer.get_width() // 2 + 118
            pygame.draw.line(
                complete_layer,
                (87, 162, 238, int(230 * badge_alpha)),
                (18 - wing_offset, wing_y),
                (left_end - wing_offset, wing_y),
                3,
            )
            pygame.draw.line(
                complete_layer,
                (125, 92, 205, int(220 * badge_alpha)),
                (right_start + wing_offset, wing_y),
                (complete_layer.get_width() - 18 + wing_offset, wing_y),
                3,
            )

            accent_y = complete_layer.get_height() - 8
            accent_half = int((complete_layer.get_width() // 2 - 28) * slide_progress)
            pygame.draw.line(
                complete_layer,
                (75, 138, 225, int(220 * badge_alpha)),
                (complete_layer.get_width() // 2 - accent_half, accent_y),
                (complete_layer.get_width() // 2 - 10, accent_y),
                2,
            )
            pygame.draw.line(
                complete_layer,
                (104, 77, 171, int(205 * badge_alpha)),
                (complete_layer.get_width() // 2 + 10, accent_y),
                (complete_layer.get_width() // 2 + accent_half, accent_y),
                2,
            )

            title_font = pygame.font.SysFont("consolas", 24, bold=True)
            subtitle_font = pygame.font.SysFont("consolas", 15, bold=True)
            title = title_font.render("MISSION COMPLETE", True, (236, 243, 252))
            title_scale = 0.90 + 0.10 * title_lock
            pulse = 1.0 + 0.025 * max(0.0, 1.0 - abs(float(hold_elapsed_frames) - 18.0) / 8.0)
            title_scale *= pulse
            scaled_title = pygame.transform.smoothscale(
                title,
                (
                    max(1, int(round(title.get_width() * title_scale))),
                    max(1, int(round(title.get_height() * title_scale))),
                ),
            )
            title_x = complete_layer.get_width() // 2 - scaled_title.get_width() // 2
            title_y = max(10, complete_layer.get_height() // 2 - scaled_title.get_height() - 4)
            scaled_title.set_alpha(int(255 * badge_alpha * title_lock))
            complete_layer.blit(scaled_title, (title_x, title_y))

            subline = mission_name.upper() + " CLEARED" if mission_name else "CHALLENGE CLEARED"
            subtitle = subtitle_font.render(subline, True, (110, 178, 245))
            subtitle_alpha = badge_alpha * _mission_intro_ease(max(0.0, hold_elapsed_frames - 8.0) / 10.0)
            subtitle.set_alpha(int(255 * subtitle_alpha))
            subtitle_x = complete_layer.get_width() // 2 - subtitle.get_width() // 2
            subtitle_y = complete_layer.get_height() // 2 + 5 + int(round((1.0 - slide_progress) * 8.0))
            complete_layer.blit(subtitle, (subtitle_x, subtitle_y))

            # One diagonal polish pass crosses the whole badge after it locks.
            if 0.0 < sheen_progress < 1.0:
                sheen = pygame.Surface(
                    (complete_layer.get_width() + 40, complete_layer.get_height() + 16),
                    pygame.SRCALPHA,
                )
                ridge_x = int(round(-24 + (complete_layer.get_width() + 56) * sheen_progress))
                ridge_w = max(18, complete_layer.get_width() // 14)
                pygame.draw.polygon(
                    sheen,
                    (215, 239, 255, int(55 * badge_alpha)),
                    [
                        (ridge_x - ridge_w, 0),
                        (ridge_x + 2, 0),
                        (ridge_x + ridge_w, sheen.get_height()),
                        (ridge_x - 2, sheen.get_height()),
                    ],
                )
                pygame.draw.line(
                    sheen,
                    (248, 253, 255, int(170 * badge_alpha)),
                    (ridge_x, 0),
                    (ridge_x + ridge_w, sheen.get_height()),
                    2,
                )
                complete_layer.blit(sheen, (-20, -8), special_flags=pygame.BLEND_RGBA_ADD)

            # Small deterministic glints add impact without turning into confetti.
            glint_phase = min(1.0, max(0.0, (hold_elapsed_frames - 10.0) / 16.0))
            glint_alpha = int(150 * badge_alpha * glint_phase)
            for index, x_frac in enumerate((0.18, 0.32, 0.68, 0.82)):
                gx = int(complete_layer.get_width() * x_frac)
                gy = 15 + (index % 2) * max(6, complete_layer.get_height() - 34)
                pygame.draw.circle(complete_layer, (175, 220, 255, glint_alpha), (gx, gy), 2)

            slide_x = int(round((1.0 - slide_progress) * 18.0))
            panel.blit(complete_layer, (complete_rect.x + slide_x, complete_rect.y))
        else:
            for display_order, (idx, step) in enumerate(visible_steps):
                if collapsed_scroll:
                    row_y = list_y + int(round((idx - scroll_pos) * (row_h + row_gap)))
                else:
                    row_y = list_y + display_order * (row_h + row_gap)

                if is_exiting:
                    exit_order = max(0, visible_rows - 1 - display_order)
                    row_exit = _mission_element_exit_progress(
                        transition_progress,
                        exit_order,
                        0.055,
                    )
                    row_intro = 1.0 - row_exit
                    row_shift = -int(round(row_exit * 38.0))
                else:
                    row_intro = _mission_row_intro_progress(
                        self._mission_intro_phase,
                        display_order,
                    )
                    row_shift = int(round((1.0 - row_intro) * 32.0))
                row_layer = pygame.Surface((inner_w, row_h), pygame.SRCALPHA)
                is_done = idx < confirmed_count
                is_predicted = confirmed_count <= idx < completed_count
                is_current = (
                    idx == current_index
                    and not mission_done
                    and not is_predicted
                )

                if isinstance(step, dict):
                    move_text = str(step.get("display") or "").strip() or " / ".join(step.get("labels", []))
                    input_text = str(step.get("input") or "").strip()
                    step_color_name = str(step.get("color") or "").lower()
                elif isinstance(step, list):
                    move_text = " / ".join(str(v) for v in step)
                    input_text = ""
                    step_color_name = ""
                else:
                    move_text = str(step)
                    input_text = ""
                    step_color_name = ""

                accent = {
                    "yellow": (201, 174, 88),
                    "green": (85, 190, 137),
                    "blue": (78, 139, 225),
                }.get(step_color_name, (90, 119, 170))

                if is_done:
                    row_fill = (9, 20, 31, 236)
                    border = (49, 77, 87)
                elif is_predicted:
                    row_fill = (20, 20, 47, 238)
                    border = (102, 87, 174)
                elif is_current:
                    row_fill = (13, 29, 52, 242)
                    border = (91, 84, 159)
                else:
                    row_fill = (10, 19, 33, 224)
                    border = (43, 56, 79)

                pygame.draw.rect(row_layer, row_fill, row_layer.get_rect(), border_radius=4)
                pygame.draw.rect(row_layer, border, row_layer.get_rect(), 1, border_radius=4)
                pygame.draw.rect(row_layer, accent, (0, 3, 4, row_h - 6), border_radius=2)

                # The wipe darkens the row body, while status and labels remain
                # readable afterward so completed work is still obvious.
                if is_done:
                    t = self.step_anim.get(idx, 0.0)
                    _draw_mission_completion_wipe(
                        row_layer,
                        1.0 - max(0.0, min(1.0, t)),
                        theme_color,
                    )

                status_cx = 20
                status_cy = row_h // 2
                if is_done:
                    pygame.draw.circle(row_layer, (22, 66, 55), (status_cx, status_cy), 9)
                    pygame.draw.circle(row_layer, (83, 189, 137), (status_cx, status_cy), 9, 1)
                    pygame.draw.lines(row_layer, (113, 222, 164), False, [
                        (status_cx - 4, status_cy), (status_cx - 1, status_cy + 3), (status_cx + 5, status_cy - 4)
                    ], 2)
                elif is_predicted:
                    pygame.draw.circle(row_layer, (42, 36, 83), (status_cx, status_cy), 9)
                    pygame.draw.circle(row_layer, (151, 126, 235), (status_cx, status_cy), 9, 1)
                    pygame.draw.circle(row_layer, (194, 179, 250), (status_cx, status_cy), 3)
                elif is_current:
                    pygame.draw.polygon(row_layer, (103, 157, 235), [
                        (status_cx - 4, status_cy - 6),
                        (status_cx + 6, status_cy),
                        (status_cx - 4, status_cy + 6),
                    ])
                else:
                    pygame.draw.circle(row_layer, (62, 79, 106), (status_cx, status_cy), 8, 1)

                number_surf = self.font.render(str(idx + 1), True, accent if not is_done else (125, 146, 158))
                row_layer.blit(number_surf, (39, status_cy - number_surf.get_height() // 2))
                pygame.draw.line(row_layer, (44, 57, 79), (72, 5), (72, row_h - 5), 1)

                move_color = (224, 231, 241) if not is_done else (145, 158, 171)
                max_move_w = max(80, inner_w - 270)
                shown_move = move_text
                while self.font.size(shown_move)[0] > max_move_w and len(shown_move) > 4:
                    shown_move = shown_move[:-4].rstrip() + "..."
                move_surf = self.font.render(shown_move, True, move_color)
                row_layer.blit(move_surf, (84, status_cy - move_surf.get_height() // 2))

                input_surf = self._render_mission_input_notation(input_text, accent)
                input_right = inner_w - 10
                if input_surf is not None:
                    row_layer.blit(input_surf, (input_right - input_surf.get_width(), status_cy - input_surf.get_height() // 2))
                    input_right -= input_surf.get_width() + 10

                if is_done:
                    done = self.smallfont.render("DONE", True, (102, 205, 151))
                    chip = pygame.Rect(input_right - done.get_width() - 20, status_cy - 10, done.get_width() + 18, 20)
                    pygame.draw.rect(row_layer, (12, 42, 35), chip, border_radius=4)
                    pygame.draw.rect(row_layer, (50, 116, 90), chip, 1, border_radius=4)
                    row_layer.blit(done, (chip.x + 9, chip.centery - done.get_height() // 2))
                elif is_predicted:
                    ready = self.smallfont.render("READY", True, (191, 175, 247))
                    chip = pygame.Rect(input_right - ready.get_width() - 20, status_cy - 10, ready.get_width() + 18, 20)
                    pygame.draw.rect(row_layer, (37, 30, 72), chip, border_radius=4)
                    pygame.draw.rect(row_layer, (103, 87, 169), chip, 1, border_radius=4)
                    row_layer.blit(ready, (chip.x + 9, chip.centery - ready.get_height() // 2))
                elif is_current:
                    current = self.smallfont.render("CURRENT", True, (151, 139, 226))
                    chip = pygame.Rect(input_right - current.get_width() - 20, status_cy - 10, current.get_width() + 18, 20)
                    pygame.draw.rect(row_layer, (30, 26, 56), chip, border_radius=4)
                    pygame.draw.rect(row_layer, (84, 72, 140), chip, 1, border_radius=4)
                    row_layer.blit(current, (chip.x + 9, chip.centery - current.get_height() // 2))

                row_layer.set_alpha(int(255 * row_intro))
                panel.blit(row_layer, (pad + row_shift, row_y))

        if collapsed_scroll and not compact_complete:
            panel.set_clip(old_clip)

        if collapsed_scroll and not compact_complete:
            footer = self.smallfont.render(
                f"Showing {footer_start + 1}-{footer_end} of {total_steps}",
                True,
                (126, 139, 159),
            )
            footer_vis = min(entry_progress(0.20, 0.15), exit_visibility(0))
            blit_faded(
                panel,
                footer,
                (pad, panel_h - pad - footer.get_height()),
                footer_vis,
                (0, 5),
            )

        panel_exit_alpha = 1.0
        if is_exiting:
            panel_exit_alpha = 1.0 - _mission_intro_ease(
                (transition_progress - 0.52) / 0.48
            )
        panel.set_alpha(
            int(255 * panel_intro * max(0.0, min(1.0, panel_exit_alpha)))
        )
        self.screen.blit(panel, (panel_x, panel_y))

    def draw_mission_overlay(self) -> None:
        self.mission_click_rects = []
        self.mission_panel_rect = None
        self.mission_toggle_rect = None
        self.mission_hint_rect = None

        holding_completion = bool(
            self._mission_hold_frames > 0 and self._mission_hold_data
        )
        if not holding_completion and (not self.mission_active or not self.mission_slot):
            return
        if self.screen is None or self.font is None or self.smallfont is None:
            return

        display_payload = self._mission_display_payload()
        data = self._mission_hold_data if holding_completion else display_payload
        display_slot = str(data.get("slot") or self.mission_slot or "")
        character = data.get("character") or "Unknown"
        theme_color = self._char_theme_color(character)
        mission_name = data.get("active_mission_name") or "No mission loaded"
        mission_notes = data.get("active_mission_notes") or ""
        steps = data.get("active_mission_steps") or []
        missions = data.get("missions") or []
        goal_progress_type = data.get("goal_progress_type")
        goal_target_state = data.get("goal_target_state")
        goal_current_frames = int(data.get("goal_current_frames", 0) or 0)
        goal_needed_frames = int(data.get("goal_needed_frames", 0) or 0)
        goal_timer_active = bool(data.get("goal_timer_active", False))        

        selector_open = bool(data.get("selector_open", False))
        selector_index = int(data.get("selector_index", 0))
        selector_hint = data.get("selector_hint") or ""
        selector_controls = data.get("selector_controls") or ""

        title = self.font.render(
            f"{character} Mission Mode - {display_slot}",
            True,
            (235, 235, 235),
        )

        completed_step_count = int(data.get("completed_step_count", 0))
        current_step_index = int(data.get("current_step_index", 0))
        progress_surf = self.smallfont.render(
            f"{completed_step_count}/{len(steps)}",
            True,
            (190, 200, 220),
        )

        pad = 10

        if selector_open:
            # Mission Select uses the same neutral broadcast language as the
            # compact HUD instead of the old terminal/debug presentation.
            mission_count = len(missions)
            completed_missions = sum(1 for mission in missions if bool(mission.get("completed", False)))

            panel_pad = max(11, int(12 * min(1.25, self.w / 1600.0)))
            header_h = max(48, self.font.get_height() + self.smallfont.get_height() + 18)
            controls_h = max(26, self.smallfont.get_height() + 10)
            row_h = max(31, self.smallfont.get_height() + 13)
            row_gap = 4
            footer_h = max(22, self.smallfont.get_height() + 7)

            # Give mission names breathing room, but keep the selector compact
            # enough to stay out of the match itself.
            longest_name_w = 0
            for idx, mission in enumerate(missions):
                name = str(mission.get("name") or mission.get("mission_id") or f"Mission {idx + 1}")
                longest_name_w = max(longest_name_w, self.smallfont.size(name)[0])
            desired_w = max(410, longest_name_w + 190)
            box_w = min(max(410, int(self.w * 0.30)), max(410, desired_w), max(410, self.w - 32))
            box_w = min(box_w, 570)
            rows_h = mission_count * row_h + max(0, mission_count - 1) * row_gap
            box_h = panel_pad * 2 + header_h + 7 + controls_h + 8 + rows_h + 8 + footer_h
            box_h = min(box_h, max(180, self.h - 36))

            x = (self.w - box_w) // 2
            y = max(20, int(self.h * 0.055))
            if y + box_h > self.h - 12:
                y = max(12, self.h - box_h - 12)
            self.mission_panel_rect = pygame.Rect(x, y, box_w, box_h)

            panel = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
            panel_rect = panel.get_rect()
            radius = 8

            # Neutral shell with a quiet top-light and character accent rail.
            pygame.draw.rect(panel, (10, 14, 20, 238), panel_rect, border_radius=radius)
            for yy in range(max(1, box_h)):
                t = yy / max(1, box_h - 1)
                shade = int(24 - 10 * t)
                pygame.draw.line(panel, (shade, shade + 4, shade + 10, 72), (1, yy), (box_w - 2, yy), 1)
            pygame.draw.rect(panel, (70, 82, 102, 210), panel_rect, 1, border_radius=radius)
            pygame.draw.line(panel, (238, 243, 250, 48), (radius + 2, 1), (box_w - radius - 3, 1), 1)
            accent_w = max(3, int(4 * min(1.25, self.w / 1600.0)))
            pygame.draw.rect(panel, (*theme_color, 235), (0, radius, accent_w, max(1, box_h - radius * 2)))

            # Header.
            content_x = panel_pad
            header_y = panel_pad - 1
            kicker = self.smallfont.render("MISSION SELECT", True, tuple(min(255, int(c * 0.92 + 30)) for c in theme_color))
            panel.blit(kicker, (content_x, header_y))

            char_label = f"{character}  ·  {display_slot}"
            char_surf = self.font.render(char_label, True, (239, 243, 250))
            panel.blit(char_surf, (content_x, header_y + kicker.get_height() + 2))

            progress_text = f"{completed_missions}/{mission_count} CLEAR"
            progress_s = self.smallfont.render(progress_text, True, (176, 221, 195) if completed_missions else (177, 190, 210))
            progress_pad_x = 9
            progress_h = max(20, progress_s.get_height() + 6)
            progress_w = progress_s.get_width() + progress_pad_x * 2
            progress_r = pygame.Rect(box_w - panel_pad - progress_w, header_y + 2, progress_w, progress_h)
            pygame.draw.rect(panel, (18, 40, 34, 218) if completed_missions else (22, 29, 40, 218), progress_r, border_radius=progress_h // 2)
            pygame.draw.rect(panel, (60, 125, 94, 185) if completed_missions else (72, 88, 112, 170), progress_r, 1, border_radius=progress_h // 2)
            panel.blit(progress_s, (progress_r.centerx - progress_s.get_width() // 2, progress_r.centery - progress_s.get_height() // 2))

            divider_y = panel_pad + header_h
            pygame.draw.line(panel, (72, 84, 104, 115), (panel_pad, divider_y), (box_w - panel_pad, divider_y), 1)

            # Controls are grouped into a quiet strip instead of one long debug string.
            controls_y = divider_y + 7
            controls_rect = pygame.Rect(panel_pad, controls_y, box_w - panel_pad * 2, controls_h)
            pygame.draw.rect(panel, (14, 19, 27, 220), controls_rect, border_radius=5)
            pygame.draw.rect(panel, (52, 64, 82, 150), controls_rect, 1, border_radius=5)
            control_parts = [
                ("↑/↓", "MOVE"),
                ("TAUNT", "SELECT"),
                ("MOUSE", "CLICK"),
            ]
            cx = controls_rect.x + 10
            for ci, (key_text, action_text) in enumerate(control_parts):
                key_s = self.smallfont.render(key_text, True, (228, 234, 244))
                action_s = self.smallfont.render(action_text, True, (126, 142, 166))
                panel.blit(key_s, (cx, controls_rect.centery - key_s.get_height() // 2))
                cx += key_s.get_width() + 5
                panel.blit(action_s, (cx, controls_rect.centery - action_s.get_height() // 2))
                cx += action_s.get_width() + 12
                if ci < len(control_parts) - 1:
                    pygame.draw.line(panel, (63, 75, 94, 130), (cx - 5, controls_rect.y + 6), (cx - 5, controls_rect.bottom - 6), 1)

            rows_top = controls_rect.bottom + 8
            mouse_pos = pygame.mouse.get_pos()
            local_mouse = (mouse_pos[0] - x, mouse_pos[1] - y)
            available_rows_bottom = box_h - panel_pad - footer_h - 8
            visible_row_capacity = max(1, (available_rows_bottom - rows_top + row_gap) // (row_h + row_gap))
            if mission_count <= visible_row_capacity:
                first_visible = 0
            else:
                half_window = visible_row_capacity // 2
                first_visible = max(0, min(selector_index - half_window, mission_count - visible_row_capacity))
            last_visible = min(mission_count, first_visible + visible_row_capacity)

            for visible_pos, idx in enumerate(range(first_visible, last_visible)):
                mission = missions[idx]
                row_y = rows_top + visible_pos * (row_h + row_gap)

                selected = idx == selector_index
                completed = bool(mission.get("completed", False))
                mission_id = mission.get("mission_id")
                name = str(mission.get("name") or mission_id or f"Mission {idx + 1}")
                row = pygame.Rect(panel_pad, row_y, box_w - panel_pad * 2, row_h)
                global_row = row.move(x, y)
                self.mission_click_rects.append((global_row, mission_id))
                hovered = row.collidepoint(local_mouse)

                # Selection is strong but not loud. Hover is intentionally weaker.
                if selected:
                    fill = tuple(int(c * 0.16) for c in theme_color)
                    pygame.draw.rect(panel, (*fill, 245), row, border_radius=5)
                    pygame.draw.rect(panel, (*theme_color, 235), row, 1, border_radius=5)
                    pygame.draw.rect(panel, (*theme_color, 245), (row.x, row.y + 4, 3, row.height - 8), border_radius=2)
                elif hovered:
                    pygame.draw.rect(panel, (31, 39, 51, 225), row, border_radius=5)
                    pygame.draw.rect(panel, (81, 96, 119, 175), row, 1, border_radius=5)
                else:
                    pygame.draw.rect(panel, (20, 26, 35, 205), row, border_radius=5)
                    pygame.draw.rect(panel, (49, 60, 77, 125), row, 1, border_radius=5)

                # Number chip.
                num_text = f"{idx + 1:02d}"
                num_s = self.smallfont.render(num_text, True, (245, 248, 252) if selected else (151, 165, 185))
                num_w = max(30, num_s.get_width() + 10)
                num_r = pygame.Rect(row.x + 8, row.centery - max(18, num_s.get_height() + 6) // 2, num_w, max(18, num_s.get_height() + 6))
                if selected:
                    pygame.draw.rect(panel, (*theme_color, 190), num_r, border_radius=4)
                else:
                    pygame.draw.rect(panel, (32, 41, 54, 220), num_r, border_radius=4)
                    pygame.draw.rect(panel, (67, 80, 101, 150), num_r, 1, border_radius=4)
                panel.blit(num_s, (num_r.centerx - num_s.get_width() // 2, num_r.centery - num_s.get_height() // 2))

                # Completion chip reserves width only when needed.
                clear_s = None
                clear_w = 0
                if completed:
                    clear_s = self.smallfont.render("CLEAR", True, (118, 224, 161))
                    clear_w = clear_s.get_width() + 18

                name_x = num_r.right + 10
                name_right = row.right - 10 - clear_w
                shown_name = name
                max_name_w = max(40, name_right - name_x)
                while self.smallfont.size(shown_name)[0] > max_name_w and len(shown_name) > 4:
                    shown_name = shown_name[:-4].rstrip() + "…"
                name_color = (239, 243, 250) if selected else ((167, 216, 185) if completed else (207, 216, 230))
                name_s = self.smallfont.render(shown_name, True, name_color)
                panel.blit(name_s, (name_x, row.centery - name_s.get_height() // 2))

                if clear_s is not None:
                    clear_r = pygame.Rect(row.right - clear_w - 7, row.centery - max(19, clear_s.get_height() + 6) // 2, clear_w, max(19, clear_s.get_height() + 6))
                    pygame.draw.rect(panel, (12, 43, 34, 220), clear_r, border_radius=4)
                    pygame.draw.rect(panel, (48, 112, 83, 175), clear_r, 1, border_radius=4)
                    panel.blit(clear_s, (clear_r.centerx - clear_s.get_width() // 2, clear_r.centery - clear_s.get_height() // 2))

                if selected:
                    # Tiny one-shot-looking highlight line gives selection some
                    # life without constantly pulsing the entire row.
                    pygame.draw.line(panel, (245, 249, 255, 85), (row.x + 6, row.y + 1), (row.right - 7, row.y + 1), 1)

            # Footer tells the player where they are without duplicating controls.
            footer_y = box_h - panel_pad - footer_h
            pygame.draw.line(panel, (57, 68, 86, 110), (panel_pad, footer_y - 5), (box_w - panel_pad, footer_y - 5), 1)
            selected_name = ""
            if missions and 0 <= selector_index < len(missions):
                selected_mission = missions[selector_index]
                selected_name = str(selected_mission.get("name") or selected_mission.get("mission_id") or "")
            footer_position = f"{selector_index + 1 if mission_count else 0} OF {mission_count}"
            if mission_count > visible_row_capacity:
                footer_position += f"  ·  SHOWING {first_visible + 1}-{last_visible}"
            footer_left = self.smallfont.render(
                footer_position,
                True,
                (124, 140, 162),
            )
            footer_right = self.smallfont.render("TAUNT TO START", True, tuple(min(255, int(c * 0.78 + 46)) for c in theme_color))
            panel.blit(footer_left, (panel_pad, footer_y + (footer_h - footer_left.get_height()) // 2))
            panel.blit(footer_right, (box_w - panel_pad - footer_right.get_width(), footer_y + (footer_h - footer_right.get_height()) // 2))

            self.screen.blit(panel, (x, y))

        else:
            self._draw_active_mission_panel(
                data=data,
                character=character,
                theme_color=theme_color,
                mission_name=mission_name,
                mission_notes=mission_notes,
                steps=steps,
                selector_hint=selector_hint,
                goal_progress_type=goal_progress_type,
                goal_target_state=goal_target_state,
                goal_current_frames=goal_current_frames,
                goal_needed_frames=goal_needed_frames,
                goal_timer_active=goal_timer_active,
                transition_mode=("idle" if holding_completion else self._mission_transition_state),
                transition_progress=(1.0 if holding_completion else self._mission_transition_phase),
            )


    def present(self) -> None:
        pygame.display.flip()

    def run(self) -> None:
        self.init()
        assert self.clock is not None
        assert self.screen is not None
        assert self.dolphin_hwnd is not None
        assert self.overlay_hwnd is not None

        print("[master] started")
        print("[master] overlay controls are managed by the main GUI")

        while self.running:
            try:
                sync_size = sync_overlay_to_dolphin(self.dolphin_hwnd, self.overlay_hwnd)
                if sync_size is None:
                    self.dolphin_hwnd = find_dolphin_hwnd()
                    sync_size = sync_overlay_to_dolphin(self.dolphin_hwnd, self.overlay_hwnd)

                if sync_size is None:
                    self.handle_events()
                    self.clock.tick(TARGET_FPS)
                    continue

                w, h = sync_size
                self.on_resize(w, h)

                self._read_control_file()
                self._read_mission_mode_file()
                self._read_mission_overlay_file()
                self._read_yami_name_overlay_file()
                self.handle_events()

                dt = self.clock.tick(TARGET_FPS) / 1000.0

                self.clear()

                try:
                    self.hitbox_renderer.update(dt, self.control)
                except Exception as exc:
                    print("[master] hitbox update failed")
                    traceback.print_exc()
                    self.hitbox_renderer = NullHitboxRenderer()
                    self.hitbox_renderer.on_resize(self.w, self.h)

                try:
                    self.hud_renderer.update(dt, self.control)
                except Exception as exc:
                    print("[master] hud update failed")
                    traceback.print_exc()
                    self.hud_renderer = NullHudRenderer()
                    self.hud_renderer.on_resize(self.w, self.h)

                self.update_mission_animations(dt)
                self.update_celebration(dt)

                # Overlay OFF is the global visual master switch. Keep the
                # compositor process and live model state running so all user
                # layer selections return unchanged on Overlay ON, but present
                # a fully transparent frame without drawing ANY HUD component.
                if not self.control.show_hud:
                    self.present()
                    continue

                try:
                    self.hitbox_renderer.draw(self.screen, self.control)
                except Exception as exc:
                    print("[master] hitbox draw failed")
                    traceback.print_exc()
                    self.hitbox_renderer = NullHitboxRenderer()
                    self.hitbox_renderer.on_resize(self.w, self.h)

                try:
                    self.hud_renderer.draw(self.screen, self.control)
                except Exception as exc:
                    print("[master] hud draw failed")
                    traceback.print_exc()
                    self.hud_renderer = NullHudRenderer()
                    self.hud_renderer.on_resize(self.w, self.h)

                self.draw_yami_name_overlay()
                self.draw_mission_overlay()
                self.draw_master_debug(dt)

                self.present()

            except Exception as exc:
                pause_on_error("MasterLoopCrash", exc)
                self.running = False

        pygame.quit()


def main() -> None:
    overlay = MasterOverlay()
    overlay.run()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        pause_on_error("FatalCrash", exc)
        raise