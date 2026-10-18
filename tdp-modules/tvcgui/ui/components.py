"""Extracted runtime module from :mod:`main`.

This module deliberately preserves the original function names and behavior so
`main.py` can remain a compatibility-oriented entry point while the subsystem
has a focused home.
"""
from __future__ import annotations

import math
from collections import OrderedDict

import pygame

from tvcgui.runtime.megacrash import (
    MEGACRASH_TRAINER_DEFAULT_CHANCE,
    MEGACRASH_TRAINER_DEFAULT_COOLDOWN_SEC,
    MEGACRASH_TRAINER_DEFAULT_DELAY_FRAMES,
    MEGACRASH_TRAINER_DEFAULT_MODE,
)

TOP_UI_RESERVED = 94

# Calm broadcast palette shared by the main window, Mission Mode, and master HUD.
# Strong color is reserved for identity, active state, and important feedback.
GUI_BG_DARK = (6, 10, 17)
GUI_PANEL = (10, 17, 28)
GUI_PANEL_2 = (14, 24, 38)
GUI_PANEL_3 = (18, 31, 49)

GUI_BORDER = (43, 59, 77)
GUI_BORDER_HOT = (104, 178, 218)

GUI_TEXT = (232, 240, 248)
GUI_TEXT_MUTED = (157, 174, 194)
GUI_TEXT_DIM = (101, 119, 141)

GUI_APP_ACCENT = (53, 170, 220)
GUI_CONFIRM = (67, 201, 156)
GUI_WARNING = (232, 183, 86)
GUI_DANGER = (223, 82, 102)

GUI_ACCENT_BLUE = GUI_APP_ACCENT
GUI_ACCENT_PURPLE = (158, 109, 216)
GUI_ACCENT_GOLD = GUI_WARNING
GUI_ACCENT_GREEN = GUI_CONFIRM
GUI_ACCENT_RED = GUI_DANGER

GUI_P1 = (230, 72, 94)
GUI_P2 = (62, 157, 222)
GUI_P3 = (176, 92, 214)
GUI_P4 = (69, 201, 149)

GUI_SLOT_MUTED = {
    "P1": (190, 76, 94),
    "P2": (67, 134, 190),
    "P3": (148, 83, 183),
    "P4": (67, 166, 127),
}


_GRADIENT_CACHE: "OrderedDict[tuple, tuple[pygame.Surface, int]]" = OrderedDict()
_GRADIENT_CACHE_PIXELS = 0
# A 2560x1440 main canvas is over three million pixels; keep its reusable
# gradients resident while bounding this RGBA cache to about 64 MiB.
_GRADIENT_CACHE_PIXEL_LIMIT = 16_000_000
_GRADIENT_CACHE_ENTRY_LIMIT = 64
_TEXT_CACHE: "OrderedDict[tuple, pygame.Surface]" = OrderedDict()
_TEXT_CACHE_ENTRY_LIMIT = 2048
_OUTLINED_TEXT_CACHE: "OrderedDict[tuple, pygame.Surface]" = OrderedDict()
_OUTLINED_TEXT_CACHE_ENTRY_LIMIT = 512
_DOCK_FONT_CACHE: dict[int, pygame.font.Font] = {}


def ko_control_button_label(enabled: bool, live_active: bool, unavailable: bool) -> str:
    if unavailable:
        return "KO: PATCH BLOCKED"
    if live_active:
        return "KO: ACTIVE"
    if enabled:
        return "KO: ARMED"
    return "KO Control: OFF"


def _cache_text(key: tuple, surface: pygame.Surface) -> pygame.Surface:
    _TEXT_CACHE[key] = surface
    _TEXT_CACHE.move_to_end(key)
    while len(_TEXT_CACHE) > _TEXT_CACHE_ENTRY_LIMIT:
        _TEXT_CACHE.popitem(last=False)
    return surface


def _cached_gradient_surface(
    width: int,
    height: int,
    top_col: tuple[int, int, int],
    bot_col: tuple[int, int, int],
    alpha: int,
) -> pygame.Surface:
    global _GRADIENT_CACHE_PIXELS

    key = (
        int(width),
        int(height),
        tuple(int(v) for v in top_col),
        tuple(int(v) for v in bot_col),
        int(alpha),
    )
    cached = _GRADIENT_CACHE.get(key)
    if cached is not None:
        _GRADIENT_CACHE.move_to_end(key)
        return cached[0]

    grad = pygame.Surface((int(width), int(height)), pygame.SRCALPHA)
    for y in range(int(height)):
        t = y / max(1, int(height) - 1)
        r = int(top_col[0] * (1.0 - t) + bot_col[0] * t)
        g = int(top_col[1] * (1.0 - t) + bot_col[1] * t)
        b = int(top_col[2] * (1.0 - t) + bot_col[2] * t)
        pygame.draw.line(grad, (r, g, b, int(alpha)), (0, y), (int(width), y))

    area = int(width) * int(height)
    if area <= _GRADIENT_CACHE_PIXEL_LIMIT:
        _GRADIENT_CACHE[key] = (grad, area)
        _GRADIENT_CACHE_PIXELS += area
        while (
            len(_GRADIENT_CACHE) > _GRADIENT_CACHE_ENTRY_LIMIT
            or _GRADIENT_CACHE_PIXELS > _GRADIENT_CACHE_PIXEL_LIMIT
        ):
            _old_key, (_old_surface, old_area) = _GRADIENT_CACHE.popitem(last=False)
            _GRADIENT_CACHE_PIXELS = max(0, _GRADIENT_CACHE_PIXELS - int(old_area))
    return grad



def _clamp_u8(v: int) -> int:
    return max(0, min(255, int(v)))


def _brighten(col: tuple[int, int, int], amt: int) -> tuple[int, int, int]:
    return (
        _clamp_u8(col[0] + amt),
        _clamp_u8(col[1] + amt),
        _clamp_u8(col[2] + amt),
    )


def _darken(col: tuple[int, int, int], amt: int) -> tuple[int, int, int]:
    return (
        _clamp_u8(col[0] - amt),
        _clamp_u8(col[1] - amt),
        _clamp_u8(col[2] - amt),
    )


def _mix_col(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, float(t)))
    return (
        _clamp_u8(a[0] * (1.0 - t) + b[0] * t),
        _clamp_u8(a[1] * (1.0 - t) + b[1] * t),
        _clamp_u8(a[2] * (1.0 - t) + b[2] * t),
    )


def _slot_accent_for_label(slot_label: str, *, muted: bool = False) -> tuple[int, int, int]:
    label = str(slot_label or "")
    if label.startswith("P1"):
        base = GUI_P1 if label.endswith("C1") else GUI_P3
    elif label.startswith("P2"):
        base = GUI_P2 if label.endswith("C1") else GUI_P4
    else:
        base = GUI_APP_ACCENT
    if muted:
        return _mix_col(base, GUI_APP_ACCENT, 0.45)
    return base


def _draw_vertical_gradient(
    surf: pygame.Surface,
    rect: pygame.Rect,
    top_col: tuple[int, int, int],
    bot_col: tuple[int, int, int],
    alpha: int = 255,
) -> None:
    if rect.width <= 0 or rect.height <= 0:
        return

    grad = _cached_gradient_surface(
        rect.width,
        rect.height,
        top_col,
        bot_col,
        alpha,
    )
    surf.blit(grad, rect.topleft)


def _draw_horizontal_gradient(
    surf: pygame.Surface,
    rect: pygame.Rect,
    left_col: tuple[int, int, int],
    right_col: tuple[int, int, int],
    alpha: int = 255,
) -> None:
    """Draw a broad horizontal color sweep used by the calmer broadcast skin."""
    global _GRADIENT_CACHE_PIXELS
    if rect.width <= 0 or rect.height <= 0:
        return
    key = (
        "horizontal", int(rect.width), int(rect.height),
        tuple(int(v) for v in left_col), tuple(int(v) for v in right_col), int(alpha),
    )
    cached = _GRADIENT_CACHE.get(key)
    if cached is not None:
        _GRADIENT_CACHE.move_to_end(key)
        grad = cached[0]
    else:
        grad = pygame.Surface((int(rect.width), int(rect.height)), pygame.SRCALPHA)
        for x in range(int(rect.width)):
            t = x / max(1, int(rect.width) - 1)
            # Smoothstep produces a longer, softer sweep than a linear split.
            t = t * t * (3.0 - 2.0 * t)
            r = int(left_col[0] * (1.0 - t) + right_col[0] * t)
            g = int(left_col[1] * (1.0 - t) + right_col[1] * t)
            b = int(left_col[2] * (1.0 - t) + right_col[2] * t)
            pygame.draw.line(grad, (r, g, b, int(alpha)), (x, 0), (x, int(rect.height)))
        area = int(rect.width) * int(rect.height)
        if area <= _GRADIENT_CACHE_PIXEL_LIMIT:
            _GRADIENT_CACHE[key] = (grad, area)
            _GRADIENT_CACHE_PIXELS += area
            while (
                len(_GRADIENT_CACHE) > _GRADIENT_CACHE_ENTRY_LIMIT
                or _GRADIENT_CACHE_PIXELS > _GRADIENT_CACHE_PIXEL_LIMIT
            ):
                _old_key, (_old_surface, old_area) = _GRADIENT_CACHE.popitem(last=False)
                _GRADIENT_CACHE_PIXELS = max(0, _GRADIENT_CACHE_PIXELS - int(old_area))
    surf.blit(grad, rect.topleft)


def _fit_text(
    font: pygame.font.Font,
    text: str,
    color: tuple[int, int, int],
    max_width: int,
) -> pygame.Surface:
    text = str(text or "")
    key = ("fit", font, text, tuple(color), int(max_width))
    cached = _TEXT_CACHE.get(key)
    if cached is not None:
        _TEXT_CACHE.move_to_end(key)
        return cached
    if max_width <= 8:
        return _cache_text(key, font.render("", True, color))

    surf = font.render(text, True, color)
    if surf.get_width() <= max_width:
        return _cache_text(key, surf)

    if len(text) <= 1:
        return _cache_text(key, font.render("", True, color))

    low = 0
    high = len(text)
    best = ""

    while low <= high:
        mid = (low + high) // 2
        trial = text[:mid].rstrip() + "."
        trial_surf = font.render(trial, True, color)
        if trial_surf.get_width() <= max_width:
            best = trial
            low = mid + 1
        else:
            high = mid - 1

    return _cache_text(key, font.render(best, True, color))

def _render_outlined_text(
    font: pygame.font.Font,
    text: str,
    text_color: tuple[int, int, int],
    outline_color: tuple[int, int, int],
    max_width: int,
    outline_px: int = 1,
) -> pygame.Surface:
    key = (font, str(text), tuple(text_color), tuple(outline_color),
           int(max_width), int(outline_px))
    cached = _OUTLINED_TEXT_CACHE.get(key)
    if cached is not None:
        _OUTLINED_TEXT_CACHE.move_to_end(key)
        return cached
    base = _fit_text(font, text, text_color, max_width)

    w = base.get_width()
    h = base.get_height()

    if w <= 0 or h <= 0:
        return base

    pad = max(1, int(outline_px))
    out = pygame.Surface((w + pad * 2, h + pad * 2), pygame.SRCALPHA)

    outline = _fit_text(font, text, outline_color, max_width)
    for ox, oy in (
        (-pad, -pad), (0, -pad), (pad, -pad),
        (-pad, 0),                (pad, 0),
        (-pad, pad),  (0, pad),   (pad, pad),
    ):
        out.blit(outline, (pad + ox, pad + oy))

    out.blit(base, (pad, pad))
    _OUTLINED_TEXT_CACHE[key] = out
    while len(_OUTLINED_TEXT_CACHE) > _OUTLINED_TEXT_CACHE_ENTRY_LIMIT:
        _OUTLINED_TEXT_CACHE.popitem(last=False)
    return out


def _render_rainbow_outlined_text(
    font: pygame.font.Font,
    text: str,
    max_width: int,
    t_ms: int,
    outline_color: tuple[int, int, int] = (0, 0, 0),
    outline_px: int = 1,
) -> pygame.Surface:
    """Render fitted text with a soft animated rainbow fill and dark outline.

    Used for the Baroque line only. It is display-only and keeps the same
    truncation behavior as _fit_text.
    """
    base = _fit_text(font, text, (255, 255, 255), max_width)
    w = base.get_width()
    h = base.get_height()
    if w <= 0 or h <= 0:
        return base

    pad = max(1, int(outline_px))
    out = pygame.Surface((w + pad * 2, h + pad * 2), pygame.SRCALPHA)

    outline = _fit_text(font, text, outline_color, max_width)
    for ox, oy in (
        (-pad, -pad), (0, -pad), (pad, -pad),
        (-pad, 0),                (pad, 0),
        (-pad, pad),  (0, pad),   (pad, pad),
    ):
        out.blit(outline, (pad + ox, pad + oy))

    rainbow = pygame.Surface((w, h), pygame.SRCALPHA)
    phase = (float(t_ms) / 1000.0) * 0.35
    for x in range(w):
        t = (x / max(1, w - 1)) + phase
        r = int(190 + 55 * math.sin(2.0 * math.pi * (t + 0.00)))
        g = int(185 + 55 * math.sin(2.0 * math.pi * (t + 0.33)))
        b = int(220 + 35 * math.sin(2.0 * math.pi * (t + 0.66)))
        pygame.draw.line(
            rainbow,
            (_clamp_u8(r), _clamp_u8(g), _clamp_u8(b), 255),
            (x, 0),
            (x, h),
        )

    colored = base.copy()
    colored.blit(rainbow, (0, 0), special_flags=pygame.BLEND_MULT)
    out.blit(colored, (pad, pad))
    return out


def draw_glass_button(
    surf: pygame.Surface,
    rect: pygame.Rect,
    label: str,
    font: pygame.font.Font,
    *,
    active: bool = False,
    hover: bool = False,
    accent: tuple[int, int, int] = GUI_ACCENT_BLUE,
    fill: tuple[int, int, int] | None = None,
    align: str = "center",
) -> None:
    """Draw a restrained broadcast control with one clear state accent."""
    if rect.width <= 0 or rect.height <= 0:
        return

    base = fill if fill is not None else GUI_PANEL_2
    tint = 0.16 if active else (0.07 if hover else 0.025)
    left = _mix_col(base, accent, tint)
    right = _darken(base, 4)
    if hover:
        left = _brighten(left, 7)
        right = _brighten(right, 4)

    shell = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
    _draw_horizontal_gradient(shell, shell.get_rect(), left, right, 246)

    border = _mix_col(GUI_BORDER, accent, 0.55 if active else (0.28 if hover else 0.08))
    pygame.draw.rect(shell, border, shell.get_rect(), 1, border_radius=4)

    # One slim rail is enough to communicate state without turning every control
    # into a separate neon card.
    rail_alpha = 230 if active else (120 if hover else 55)
    pygame.draw.rect(
        shell,
        (*accent, rail_alpha),
        pygame.Rect(1, 3, 3 if active else 2, max(1, shell.get_height() - 6)),
        border_radius=2,
    )
    if active:
        pygame.draw.line(shell, (*_brighten(accent, 20), 105), (7, shell.get_height() - 2), (shell.get_width() - 7, shell.get_height() - 2))

    surf.blit(shell, rect.topleft)

    text_col = GUI_TEXT if active or hover else GUI_TEXT_MUTED
    label_surf = _fit_text(font, label, text_col, rect.width - 14)
    if align == "left":
        tx = rect.x + 9
    elif align == "right":
        tx = rect.right - label_surf.get_width() - 9
    else:
        tx = rect.x + (rect.width - label_surf.get_width()) // 2
    ty = rect.y + (rect.height - label_surf.get_height()) // 2
    surf.blit(label_surf, (tx, ty))

def draw_slot_chip(
    surf: pygame.Surface,
    rect: pygame.Rect,
    label: str,
    font: pygame.font.Font,
    *,
    enabled: bool,
    accent: tuple[int, int, int],
    hover: bool,
    compact: bool = False,
) -> None:
    base = (16, 27, 42) if enabled else (13, 21, 33)
    left = _mix_col(base, accent, 0.13 if enabled else 0.025)
    right = _darken(base, 3)
    if hover:
        left = _brighten(left, 6)
        right = _brighten(right, 4)

    _draw_horizontal_gradient(surf, rect, left, right, 240)
    pygame.draw.rect(
        surf,
        _mix_col(GUI_BORDER, accent, 0.48 if enabled else (0.22 if hover else 0.05)),
        rect,
        1,
        border_radius=4,
    )
    if enabled:
        pygame.draw.rect(surf, (*accent, 205), pygame.Rect(rect.x + 2, rect.y + 4, 2, max(1, rect.height - 8)), border_radius=1)

    state = "ON" if enabled else "OFF"
    text = str(label) if compact else f"{label} {state}"
    label_surf = _fit_text(font, text, GUI_TEXT if enabled else GUI_TEXT_DIM, rect.width - 10)
    surf.blit(
        label_surf,
        (
            rect.x + (rect.width - label_surf.get_width()) // 2,
            rect.y + (rect.height - label_surf.get_height()) // 2,
        ),
    )


def _layout_command_section(
    width: int,
    y: int,
    label: str,
    specs: list[tuple[str, int]],
    *,
    button_h: int = 22,
    row_gap: int = 5,
    item_gap: int = 5,
) -> tuple[pygame.Rect, dict[str, pygame.Rect]]:
    """Lay out one responsive command section.

    Each section owns a fixed label rail and lets its buttons wrap inside the
    remaining width. The same helper is used by the height probe and renderer,
    so resizing never makes the panel layout and click targets disagree.
    """
    pad_x = 8
    label_w = 84 if width >= 760 else 68
    section_w = max(220, int(width) - pad_x * 2)
    content_x = pad_x + label_w
    content_right = pad_x + section_w - 7
    x = content_x
    row_y = int(y) + 5
    rows = 1
    rects: dict[str, pygame.Rect] = {}

    for key, raw_w in specs:
        item_w = max(20, int(raw_w))
        if x + item_w > content_right and x > content_x:
            rows += 1
            x = content_x
            row_y += button_h + row_gap
        rects[key] = pygame.Rect(x, row_y, item_w, button_h)
        x += item_w + item_gap

    height = 10 + rows * button_h + (rows - 1) * row_gap
    return pygame.Rect(pad_x, int(y), section_w, height), rects


# Visuals has its own LAB slot. The hit-test bridge keeps the existing
# character-select window dispatch without adding another dock return value.
_CHAR_SELECT_VISUALS_RECT = pygame.Rect(-10000, -10000, 0, 0)


def character_select_visuals_hit_test(pos: tuple[int, int] | None = None) -> bool:
    """Return True when the pointer is over the Visuals button."""
    try:
        point = pygame.mouse.get_pos() if pos is None else pos
        return bool(_CHAR_SELECT_VISUALS_RECT.collidepoint(int(point[0]), int(point[1])))
    except Exception:
        return False


def _command_dock_layout(width: int, tools_open: bool) -> tuple[dict[str, tuple[pygame.Rect, dict[str, pygame.Rect]]], int]:
    width_i = max(320, int(width))
    sections: dict[str, tuple[pygame.Rect, dict[str, pygame.Rect]]] = {}
    y = 6
    section_gap = 5

    hud_specs = [
        ("hud_btn", 100),
        ("info_set_btn", 104),
        ("settings_btn", 74),
        ("custom_music", 106),  # A single setup button; output modes live in the music window
    ]
    section, rects = _layout_command_section(width_i, y, "HUD", hud_specs)
    sections["hud"] = (section, rects)
    y = section.bottom + section_gap

    visual_specs = [("hb_btn", 104)]
    visual_specs.extend((f"hb_{slot}", 22) for slot in ("P1", "P2", "P3", "P4"))
    visual_specs.append(("hurt_btn", 112))
    visual_specs.extend((f"hurt_{slot}", 22) for slot in ("P1", "P2", "P3", "P4"))
    visual_specs.extend([
        ("ruler_btn", 84),
        ("ruler_h", 82),
        ("ruler_v", 70),
        ("ruler_envelope", 82),
        ("ruler_lock", 92),
    ])
    visual_specs.extend((f"ruler_{slot}", 22) for slot in ("P1", "P2", "P3", "P4"))
    # Reserve the right edge for the expandable tools menu on every row width.
    # It has its own click target, so it cannot collide with a wrapped chip.
    more_w = 82
    section, rects = _layout_command_section(width_i - more_w - 8, y, "VISUALS", visual_specs)
    rects["tools_btn"] = pygame.Rect(width_i - more_w - 15, section.y + 5, more_w, 22)
    section.width = max(section.width, width_i - 16)
    sections["visuals"] = (section, rects)
    y = section.bottom + section_gap

    if tools_open:
        training_specs = [
            ("cancel_mapper", 114),
            ("cancel_lab", 106),
            ("action_recorder", 132),
            ("timing_probe", 124),
            ("punish", 132),
            ("megacrash", 176),
        ]
        section, rects = _layout_command_section(width_i, y, "TRAINING", training_specs)
        sections["training"] = (section, rects)
        y = section.bottom + section_gap

        lab_specs = [
            ("assist", 106),
            ("win", 124),
            ("ko", 122),
            ("extra", 166),
            ("stage", 116),
            ("visuals", 68),
            ("dump", 120),
            ("status", 112),
        ]
        section, rects = _layout_command_section(width_i, y, "LAB / SETUP", lab_specs)
        sections["lab"] = (section, rects)
        y = section.bottom + 6
    else:
        help_rect = pygame.Rect(8, y, max(220, width_i - 16), 28)
        sections["help"] = (help_rect, {})
        y = help_rect.bottom + 6

    return sections, max(72, int(y))


def _dock_smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, float(value)))
    return value * value * (3.0 - 2.0 * value)


def get_top_dock_height(
    width: int,
    tools_open: bool = False,
    *,
    tools_progress: float | None = None,
) -> int:
    """Return the animated vertical space reserved by the command dock."""
    _closed_sections, closed_height = _command_dock_layout(width, False)
    _open_sections, open_height = _command_dock_layout(width, True)
    if tools_progress is None:
        tools_progress = 1.0 if tools_open else 0.0
    eased = _dock_smoothstep(tools_progress)
    return int(round(closed_height + (open_height - closed_height) * eased))


def draw_top_command_dock(
    screen: pygame.Surface,
    smallfont: pygame.font.Font,
    *,
    hitbox_slots: dict,
    hurtbox_slots: dict,
    ruler_slots: dict,
    ruler_enabled: bool,
    ruler_axes: dict,
    ruler_envelope_enabled: bool,
    ruler_lock_active: bool,
    overlay_enabled: bool,
    show_interaction_card: bool,
    show_combo_card: bool,
    hud_info_set: str,
    show_damage_badge: bool,
    show_untech_panel: bool,
    show_damage_inactive: bool,
    show_meter_panel: bool,
    show_red_health_panel: bool,
    show_attack_property_panel: bool,
    show_tag_card: bool,
    megacrash_trainer_enabled: bool = False,
    megacrash_trainer_chance: int = MEGACRASH_TRAINER_DEFAULT_CHANCE,
    megacrash_trainer_mode: str = MEGACRASH_TRAINER_DEFAULT_MODE,
    megacrash_trainer_delay_frames: int = MEGACRASH_TRAINER_DEFAULT_DELAY_FRAMES,
    megacrash_trainer_cooldown_sec: float = MEGACRASH_TRAINER_DEFAULT_COOLDOWN_SEC,
    megacrash_trainer_cooldown_remaining: float = 0.0,
    mem_dump_active: bool = False,
    mem_dump_label: str = "",
    win_score_enabled: bool = False,
    char_test_active: bool = False,
    ko_control_enabled: bool = False,
    ko_control_live_active: bool = False,
    ko_control_unavailable: bool = False,
    solo_team_active: bool = False,
    action_spoof_active: bool = False,
    tools_open: bool = False,
    tools_progress: float | None = None,
    mouse_pos: tuple[int, int],
    t_ms: int = 0,
) -> tuple:
    """Draw a responsive command dock using the same glass-card language as the HUD."""
    global _CHAR_SELECT_VISUALS_RECT
    del megacrash_trainer_chance, megacrash_trainer_mode, megacrash_trainer_delay_frames
    del megacrash_trainer_cooldown_sec, megacrash_trainer_cooldown_remaining
    del solo_team_active

    mx, my = mouse_pos
    width, _height = screen.get_size()
    if tools_progress is None:
        tools_progress = 1.0 if tools_open else 0.0
    tools_progress = max(0.0, min(1.0, float(tools_progress)))
    eased_tools = _dock_smoothstep(tools_progress)
    closed_sections, closed_h = _command_dock_layout(width, False)
    open_sections, open_h = _command_dock_layout(width, True)
    dock_h = int(round(closed_h + (open_h - closed_h) * eased_tools))
    sections = open_sections if tools_progress > 0.001 else closed_sections

    if tools_progress > 0.001:
        reveal_shift = int(round((1.0 - eased_tools) * 14.0))
        for section_key in ("training", "lab"):
            if section_key not in sections:
                continue
            section_rect, button_rects = sections[section_key]
            section_rect = section_rect.move(0, -reveal_shift)
            button_rects = {key: value.move(0, -reveal_shift) for key, value in button_rects.items()}
            sections[section_key] = (section_rect, button_rects)

    dock_rect = pygame.Rect(0, 0, width, dock_h)
    _draw_horizontal_gradient(screen, dock_rect, (12, 25, 40), (5, 10, 18), 255)
    pygame.draw.line(screen, (49, 132, 174), (0, 0), (width, 0), 1)
    pygame.draw.line(screen, (31, 49, 67), (0, dock_rect.bottom - 1), (width, dock_rect.bottom - 1))

    dock_size = 11 if width >= 760 else 10
    dockfont = _DOCK_FONT_CACHE.get(dock_size)
    if dockfont is None:
        try:
            dockfont = pygame.font.SysFont("consolas", dock_size)
        except Exception:
            dockfont = smallfont
        _DOCK_FONT_CACHE[dock_size] = dockfont

    off = pygame.Rect(-10000, -10000, 0, 0)
    ps_btn_rect = off.copy()
    solo_team_btn_rect = off.copy()
    as_btn_rect = off.copy()
    megacrash_btn_rect = off.copy()
    memdump_btn_rect = off.copy()
    win_counter_btn_rect = off.copy()
    overseer_btn_rect = off.copy()
    select_probe_btn_rect = off.copy()
    yami_stage_btn_rect = off.copy()
    ko_control_btn_rect = off.copy()
    action_spoof_btn_rect = off.copy()
    cancel_mapper_btn_rect = off.copy()
    cancel_lab_btn_rect = off.copy()
    timing_probe_btn_rect = off.copy()

    section_accents = {
        "hud": GUI_APP_ACCENT,
        "visuals": GUI_CONFIRM,
        "training": GUI_ACCENT_PURPLE,
        "lab": GUI_WARNING,
        "help": GUI_APP_ACCENT,
    }
    section_labels = {
        "hud": "HUD",
        "visuals": "VISUALS",
        "training": "TRAINING",
        "lab": "LAB / SETUP",
    }

    def draw_section(key: str) -> dict[str, pygame.Rect]:
        section_rect, rects = sections[key]
        accent = section_accents.get(key, GUI_APP_ACCENT)
        left = _mix_col((13, 24, 38), accent, 0.10)
        _draw_horizontal_gradient(screen, section_rect, left, (7, 13, 22), 250)
        pygame.draw.rect(screen, (38, 55, 73), section_rect, 1, border_radius=5)
        pygame.draw.rect(screen, (*accent, 180), pygame.Rect(section_rect.x + 1, section_rect.y + 4, 3, max(1, section_rect.height - 8)), border_radius=2)

        label_w = 75 if width >= 760 else 61
        label = section_labels.get(key, key.upper())
        label_surf = _fit_text(dockfont, label, _brighten(accent, 28), label_w - 14)
        screen.blit(label_surf, (section_rect.x + 10, section_rect.centery - label_surf.get_height() // 2))
        pygame.draw.line(screen, (43, 59, 77), (section_rect.x + label_w, section_rect.y + 5), (section_rect.x + label_w, section_rect.bottom - 5))
        return rects

    hud = draw_section("hud")
    hud_btn_rect = hud["hud_btn"]
    interaction_card_btn_rect = off.copy()
    combo_card_btn_rect = off.copy()
    info_set_btn_rect = hud["info_set_btn"]
    damage_badge_btn_rect = off.copy()
    untech_panel_btn_rect = off.copy()
    meter_panel_btn_rect = off.copy()
    red_health_panel_btn_rect = off.copy()
    attack_property_panel_btn_rect = off.copy()
    tag_card_btn_rect = off.copy()
    clear_card_btn_rect = off.copy()
    tools_btn_rect = off.copy()
    settings_btn_rect = hud["settings_btn"]
    help_btn_rect = off.copy()
    # This button owns its click bridge, keeping the legacy dock return tuple
    # stable for existing HUD callers.
    from tvcgui.ui.settings_dock import settings_dock
    settings_dock.frame_mouse(
        inside=settings_btn_rect.collidepoint(mx, my),
        left_pressed=pygame.mouse.get_pressed(3)[0],
    )
    # Standalone Music Setup opens from the top-right of the main HUD. Keep
    # the main.py tuple stable, and process click release only once each frame.
    music_button_rect = hud["custom_music"]
    try:
        from tvcgui.features.audio.music_player import controller as _top_music
        from tvcgui.features.audio.music_dock import music_dock as _top_music_dock
        _top_music_dock.frame_mouse(
            visible=True,
            inside=music_button_rect.collidepoint(mx, my),
            left_pressed=pygame.mouse.get_pressed(3)[0],
        )
        music_active = _top_music.dock_active()
        draw_glass_button(
            screen, music_button_rect, "Custom Music", dockfont,
            active=bool(music_active), hover=music_button_rect.collidepoint(mx, my),
            accent=GUI_ACCENT_GOLD,
            fill=(72, 54, 34) if music_active else (45, 35, 45), align="center",
        )
    except Exception:
        pass  # Optional feature must never prevent Continuo from drawing.

    draw_glass_button(
        screen, hud_btn_rect, "Overlay: ON" if overlay_enabled else "Overlay: OFF", dockfont,
        active=bool(overlay_enabled), hover=hud_btn_rect.collidepoint(mx, my), accent=GUI_APP_ACCENT,
        fill=(43, 58, 86) if overlay_enabled else (27, 33, 45), align="center",
    )
    info_set_name = str(hud_info_set or "CUSTOM").strip().upper()
    info_set_accent = {
        "CORE": GUI_APP_ACCENT,
        "RESEARCH": GUI_WARNING,
        "FULL": GUI_CONFIRM,
        "CUSTOM": GUI_ACCENT_PURPLE,
    }.get(info_set_name, GUI_ACCENT_PURPLE)
    draw_glass_button(
        screen, info_set_btn_rect, "HUD Settings", dockfont,
        active=True, hover=info_set_btn_rect.collidepoint(mx, my),
        accent=info_set_accent, fill=_mix_col((27, 33, 45), info_set_accent, 0.18), align="center",
    )
    draw_glass_button(
        screen, settings_btn_rect, "Settings", dockfont,
        active=False, hover=settings_btn_rect.collidepoint(mx, my), accent=GUI_APP_ACCENT,
        fill=(27, 33, 45), align="center",
    )

    visual = draw_section("visuals")
    tools_btn_rect = visual["tools_btn"]
    draw_glass_button(
        screen, tools_btn_rect, "More", dockfont,
        active=bool(tools_open), hover=tools_btn_rect.collidepoint(mx, my), accent=GUI_ACCENT_GOLD,
        fill=(74, 56, 30) if tools_open else (54, 43, 30), align="left",
    )
    arrow_x = tools_btn_rect.right - 14
    arrow_y = tools_btn_rect.centery
    if tools_open:
        arrow = [(arrow_x - 4, arrow_y + 2), (arrow_x, arrow_y - 2), (arrow_x + 4, arrow_y + 2)]
    else:
        arrow = [(arrow_x - 4, arrow_y - 2), (arrow_x, arrow_y + 2), (arrow_x + 4, arrow_y - 2)]
    pygame.draw.lines(screen, GUI_ACCENT_GOLD, False, arrow, 2)
    hb_btn_rect = visual["hb_btn"]
    hurt_btn_rect = visual["hurt_btn"]
    ruler_btn_rect = visual["ruler_btn"]
    ruler_axis_h_rect = visual["ruler_h"]
    ruler_axis_v_rect = visual["ruler_v"]
    ruler_envelope_rect = visual["ruler_envelope"]
    ruler_lock_rect = visual["ruler_lock"]

    hb_on = any(bool(v) for v in hitbox_slots.values())
    hurt_on = any(bool(v) for v in hurtbox_slots.values())
    ruler_axes = ruler_axes if isinstance(ruler_axes, dict) else {}
    ruler_horz_on = bool(ruler_axes.get("horizontal", True))
    ruler_vert_on = bool(ruler_axes.get("vertical", False))

    draw_glass_button(
        screen, hb_btn_rect, "Hitboxes: ON" if hb_on else "Hitboxes: OFF", dockfont,
        active=hb_on, hover=hb_btn_rect.collidepoint(mx, my), accent=GUI_APP_ACCENT, align="center",
    )
    draw_glass_button(
        screen, hurt_btn_rect, "Hurtboxes: ON" if hurt_on else "Hurtboxes: OFF", dockfont,
        active=hurt_on, hover=hurt_btn_rect.collidepoint(mx, my), accent=GUI_ACCENT_GREEN, align="center",
    )
    draw_glass_button(
        screen, ruler_btn_rect, "Ruler: ON" if ruler_enabled else "Ruler: OFF", dockfont,
        active=bool(ruler_enabled), hover=ruler_btn_rect.collidepoint(mx, my), accent=GUI_ACCENT_PURPLE,
        fill=(48, 43, 76) if ruler_enabled else (27, 33, 45), align="center",
    )
    draw_glass_button(
        screen, ruler_axis_h_rect, "Horizontal", dockfont,
        active=ruler_horz_on, hover=ruler_axis_h_rect.collidepoint(mx, my), accent=GUI_ACCENT_PURPLE,
        fill=(48, 43, 76) if ruler_horz_on else (27, 33, 45), align="center",
    )
    draw_glass_button(
        screen, ruler_axis_v_rect, "Vertical", dockfont,
        active=ruler_vert_on, hover=ruler_axis_v_rect.collidepoint(mx, my), accent=GUI_ACCENT_PURPLE,
        fill=(48, 43, 76) if ruler_vert_on else (27, 33, 45), align="center",
    )
    draw_glass_button(
        screen, ruler_envelope_rect, "Envelope", dockfont,
        active=bool(ruler_envelope_enabled), hover=ruler_envelope_rect.collidepoint(mx, my), accent=GUI_ACCENT_PURPLE,
        fill=(48, 43, 76) if ruler_envelope_enabled else (27, 33, 45), align="center",
    )
    draw_glass_button(
        screen, ruler_lock_rect, "Ruler Lock", dockfont,
        active=bool(ruler_lock_active), hover=ruler_lock_rect.collidepoint(mx, my), accent=GUI_WARNING,
        fill=(57, 49, 30) if ruler_lock_active else (27, 33, 45), align="center",
    )

    slot_colors = dict(GUI_SLOT_MUTED)
    hb_filter_rects: dict[str, pygame.Rect] = {}
    hurt_filter_rects: dict[str, pygame.Rect] = {}
    ruler_filter_rects: dict[str, pygame.Rect] = {}
    for slot_name in ("P1", "P2", "P3", "P4"):
        hb_rect = visual[f"hb_{slot_name}"]
        hurt_rect = visual[f"hurt_{slot_name}"]
        ruler_rect = visual[f"ruler_{slot_name}"]
        draw_slot_chip(
            screen, hb_rect, slot_name[-1], smallfont,
            enabled=bool(hitbox_slots.get(slot_name, False)), accent=slot_colors.get(slot_name, GUI_ACCENT_BLUE),
            hover=hb_rect.collidepoint(mx, my), compact=True,
        )
        draw_slot_chip(
            screen, hurt_rect, slot_name[-1], smallfont,
            enabled=bool(hurtbox_slots.get(slot_name, False)), accent=slot_colors.get(slot_name, GUI_ACCENT_GREEN),
            hover=hurt_rect.collidepoint(mx, my), compact=True,
        )
        draw_slot_chip(
            screen, ruler_rect, slot_name[-1], smallfont,
            enabled=bool(ruler_slots.get(slot_name, True)), accent=slot_colors.get(slot_name, GUI_ACCENT_PURPLE),
            hover=ruler_rect.collidepoint(mx, my), compact=True,
        )
        hb_filter_rects[slot_name] = hb_rect.inflate(4, 4)
        hurt_filter_rects[slot_name] = hurt_rect.inflate(4, 4)
        ruler_filter_rects[slot_name] = ruler_rect.inflate(4, 4)

    previous_clip = screen.get_clip()
    screen.set_clip(dock_rect)

    if tools_progress > 0.001:
        training = draw_section("training")
        cancel_mapper_btn_rect = training["cancel_mapper"]
        cancel_lab_btn_rect = training["cancel_lab"]
        solo_team_btn_rect = training["action_recorder"]
        timing_probe_btn_rect = training["timing_probe"]
        action_spoof_btn_rect = training["punish"]
        megacrash_btn_rect = training["megacrash"]

        draw_glass_button(
            screen, cancel_mapper_btn_rect, "Cancel Mapper", dockfont, active=False,
            hover=cancel_mapper_btn_rect.collidepoint(mx, my), accent=GUI_ACCENT_PURPLE,
            fill=(43, 38, 68), align="center",
        )
        draw_glass_button(
            screen, cancel_lab_btn_rect, "Cancel Lab", dockfont, active=False,
            hover=cancel_lab_btn_rect.collidepoint(mx, my), accent=GUI_ACCENT_PURPLE,
            fill=(43, 38, 68), align="center",
        )
        draw_glass_button(
            screen, solo_team_btn_rect, "Action Recorder", dockfont, active=False,
            hover=solo_team_btn_rect.collidepoint(mx, my), accent=GUI_CONFIRM,
            fill=(27, 33, 45), align="center",
        )
        draw_glass_button(
            screen, timing_probe_btn_rect, "Timing Monitor", dockfont, active=False,
            hover=timing_probe_btn_rect.collidepoint(mx, my), accent=GUI_WARNING,
            fill=(47, 42, 29), align="center",
        )
        draw_glass_button(
            screen, action_spoof_btn_rect, "Punish Trainer: ON" if action_spoof_active else "Punish Trainer", dockfont,
            active=bool(action_spoof_active), hover=action_spoof_btn_rect.collidepoint(mx, my),
            accent=GUI_ACCENT_PURPLE, fill=(48, 43, 76) if action_spoof_active else (27, 33, 45), align="center",
        )
        draw_glass_button(
            screen, megacrash_btn_rect, "Megacrash Trainer: ON" if megacrash_trainer_enabled else "Megacrash Trainer", dockfont,
            active=bool(megacrash_trainer_enabled), hover=megacrash_btn_rect.collidepoint(mx, my),
            accent=GUI_APP_ACCENT, fill=(43, 58, 86) if megacrash_trainer_enabled else (27, 33, 45), align="center",
        )

        lab = draw_section("lab")
        as_btn_rect = lab["assist"]
        yami_stage_btn_rect = lab["stage"]
        select_probe_btn_rect = lab["extra"]
        _CHAR_SELECT_VISUALS_RECT = lab["visuals"].copy()
        ko_control_btn_rect = lab["ko"]
        memdump_btn_rect = lab["dump"]
        win_counter_btn_rect = lab["win"]
        overseer_btn_rect = lab["status"]

        draw_glass_button(screen, as_btn_rect, "Assist Setup", dockfont, hover=as_btn_rect.collidepoint(mx, my), accent=GUI_APP_ACCENT, align="center")
        draw_glass_button(screen, yami_stage_btn_rect, "Extra Stages", dockfont, hover=yami_stage_btn_rect.collidepoint(mx, my), accent=GUI_APP_ACCENT, align="center")
        draw_glass_button(
            screen, select_probe_btn_rect, "Extra Characters: ON" if char_test_active else "Extra Characters: OFF", dockfont,
            active=bool(char_test_active), hover=select_probe_btn_rect.collidepoint(mx, my), accent=GUI_APP_ACCENT,
            fill=(43, 58, 86) if char_test_active else (27, 33, 45), align="center",
        )
        draw_glass_button(
            screen, _CHAR_SELECT_VISUALS_RECT, "Visuals", dockfont,
            active=False, hover=_CHAR_SELECT_VISUALS_RECT.collidepoint(mx, my), accent=GUI_ACCENT_PURPLE,
            fill=(43, 38, 68), align="center",
        )
        ko_label = ko_control_button_label(
            ko_control_enabled, ko_control_live_active, ko_control_unavailable
        )
        draw_glass_button(
            screen, ko_control_btn_rect, ko_label, dockfont,
            active=bool(ko_control_enabled and not ko_control_unavailable),
            hover=ko_control_btn_rect.collidepoint(mx, my),
            accent=GUI_DANGER if ko_control_unavailable else GUI_ACCENT_GREEN,
            fill=(54, 31, 38) if ko_control_unavailable else (31, 46, 40) if ko_control_enabled else (27, 33, 45),
            align="center",
        )
        dump_label = mem_dump_label if mem_dump_active and mem_dump_label else "Dump Memory"
        draw_glass_button(
            screen, memdump_btn_rect, dump_label, dockfont, active=bool(mem_dump_active),
            hover=memdump_btn_rect.collidepoint(mx, my), accent=GUI_APP_ACCENT,
            fill=(43, 58, 86) if mem_dump_active else (27, 33, 45), align="center",
        )
        draw_glass_button(
            screen, win_counter_btn_rect, "Win Score: ON" if win_score_enabled else "Win Score: OFF", dockfont,
            active=bool(win_score_enabled), hover=win_counter_btn_rect.collidepoint(mx, my), accent=GUI_APP_ACCENT,
            fill=(43, 58, 86) if win_score_enabled else (27, 33, 45), align="center",
        )
        draw_glass_button(screen, overseer_btn_rect, "Tool Status", dockfont, hover=overseer_btn_rect.collidepoint(mx, my), accent=GUI_APP_ACCENT, align="center")
    else:
        help_rect, _ = sections["help"]
        help_tip = "Research buttons add one compact data row inside both team HUD panels. Select a view, or click the active view again to close it."
        help_accent = GUI_APP_ACCENT
        if hud_btn_rect.collidepoint(mx, my):
            help_tip = "Overlay controls only the core team HUD. Every other in-game layer can remain on or off independently."
        elif interaction_card_btn_rect.collidepoint(mx, my):
            help_tip = "Hit and Block controls only the live interaction ribbon."
        elif combo_card_btn_rect.collidepoint(mx, my):
            help_tip = "Combo controls only the live combo ledger."
        elif info_set_btn_rect.collidepoint(mx, my):
            help_tip = "HUD Settings contains the display switches, built-in sets, and saved presets."
            help_accent = info_set_accent
        elif damage_badge_btn_rect.collidepoint(mx, my):
            help_tip = "Damage Mod adds branded C1 and C2 damage-scaling gauges beneath the character health rows."
            help_accent = GUI_WARNING
        elif untech_panel_btn_rect.collidepoint(mx, my):
            help_tip = "HS Scale is a per-hit recovery clock. It starts full and drains to recovery; a new hit resets it immediately."
            help_accent = GUI_ACCENT_RED
        elif meter_panel_btn_rect.collidepoint(mx, my):
            help_tip = "Meter adds the latest gain or spend beside the existing meter display. Color shows prediction status."
            help_accent = GUI_CONFIRM
        elif red_health_panel_btn_rect.collidepoint(mx, my):
            help_tip = "Red HP paints recoverable health into the existing bars and adds the recoverable amount beside HP."
            help_accent = GUI_DANGER
        elif attack_property_panel_btn_rect.collidepoint(mx, my):
            help_tip = "Research opens the standalone read-only attack window. It never patches game code or adds another row to the match overlay."
            help_accent = GUI_ACCENT_PURPLE
        elif tag_card_btn_rect.collidepoint(mx, my):
            help_tip = "Tag controls only the incoming tag resource card."
        elif clear_card_btn_rect.collidepoint(mx, my):
            help_tip = "Clear hides optional HUD cards. Research capture is controlled from its standalone window."
            help_accent = GUI_ACCENT_RED
        elif hb_btn_rect.collidepoint(mx, my):
            help_tip = "Hitboxes control only attack and projectile box drawings."
        elif hurt_btn_rect.collidepoint(mx, my):
            help_tip = "Hurtboxes control only body and defender bubble drawings."
            help_accent = GUI_ACCENT_GREEN
        elif ruler_btn_rect.collidepoint(mx, my):
            help_tip = "Ruler controls saved active-frame reach guides and does not require visible hitboxes or hurtboxes."
            help_accent = GUI_ACCENT_PURPLE
        elif ruler_axis_h_rect.collidepoint(mx, my):
            help_tip = "Horizontal toggles the furthest forward active hitbox edge."
            help_accent = GUI_ACCENT_PURPLE
        elif ruler_axis_v_rect.collidepoint(mx, my):
            help_tip = "Vertical toggles the full active hitbox height envelope."
            help_accent = GUI_ACCENT_PURPLE
        elif ruler_envelope_rect.collidepoint(mx, my):
            help_tip = "Envelope adds the 2D sampled threat shape when Horizontal and Vertical are both on. Turn it off to keep only the two ruler axes."
            help_accent = GUI_ACCENT_PURPLE
        elif ruler_lock_rect.collidepoint(mx, my):
            help_tip = "Ruler Lock opens a move picker. Locking a saved move keeps that move's ruler visible until you unlock it."
            help_accent = GUI_WARNING
        elif tools_btn_rect.collidepoint(mx, my):
            help_tip = "Tools opens dedicated Training and Lab / Setup sections."
        elif settings_btn_rect.collidepoint(mx, my):
            help_tip = "Settings exports preferences and records read-only session replays."
        elif help_btn_rect.collidepoint(mx, my):
            help_tip = "Help opens the built-in guide for HUD controls, frame data, trainers, charge tracking, and setup tools."
            help_accent = GUI_APP_ACCENT
        elif music_button_rect.collidepoint(mx, my):
            help_tip = "Custom Music opens music sources, playlists, folder locations and native audio preferences."
            help_accent = GUI_ACCENT_GOLD

        _draw_horizontal_gradient(screen, help_rect, _mix_col((12, 23, 36), help_accent, 0.06), (7, 13, 22), 248)
        pygame.draw.rect(screen, (38, 55, 73), help_rect, 1, border_radius=5)
        pygame.draw.rect(screen, (*help_accent, 135), pygame.Rect(help_rect.x + 6, help_rect.centery - 2, 4, 4), border_radius=2)
        help_surf = _fit_text(smallfont, help_tip, GUI_TEXT_DIM, help_rect.width - 30)
        screen.blit(help_surf, (help_rect.x + 18, help_rect.centery - help_surf.get_height() // 2))

    screen.set_clip(previous_clip)
    if tools_progress < 0.88:
        _CHAR_SELECT_VISUALS_RECT = off.copy()
        as_btn_rect = off.copy()
        megacrash_btn_rect = off.copy()
        memdump_btn_rect = off.copy()
        win_counter_btn_rect = off.copy()
        overseer_btn_rect = off.copy()
        select_probe_btn_rect = off.copy()
        yami_stage_btn_rect = off.copy()
        ko_control_btn_rect = off.copy()
        action_spoof_btn_rect = off.copy()
        cancel_mapper_btn_rect = off.copy()
        cancel_lab_btn_rect = off.copy()
        solo_team_btn_rect = off.copy()
        timing_probe_btn_rect = off.copy()

    return (
        hb_btn_rect, hurt_btn_rect, ps_btn_rect, as_btn_rect, hud_btn_rect,
        megacrash_btn_rect, memdump_btn_rect, win_counter_btn_rect,
        overseer_btn_rect, select_probe_btn_rect, yami_stage_btn_rect, ko_control_btn_rect,
        action_spoof_btn_rect, cancel_mapper_btn_rect, cancel_lab_btn_rect, solo_team_btn_rect, timing_probe_btn_rect,
        interaction_card_btn_rect, combo_card_btn_rect, info_set_btn_rect, damage_badge_btn_rect, untech_panel_btn_rect,
        meter_panel_btn_rect, red_health_panel_btn_rect, attack_property_panel_btn_rect,
        tag_card_btn_rect, clear_card_btn_rect,
        tools_btn_rect, help_btn_rect, hb_filter_rects, hurt_filter_rects, ruler_btn_rect,
        ruler_axis_h_rect, ruler_axis_v_rect, ruler_envelope_rect, ruler_lock_rect, ruler_filter_rects,
    )


def draw_status_rail(
    screen: pygame.Surface,
    smallfont: pygame.font.Font,
    *,
    text: str,
) -> None:
    if not text:
        return

    w, h = screen.get_size()
    rail_h = 22
    rect = pygame.Rect(0, h - rail_h, w, rail_h)

    _draw_vertical_gradient(
        screen,
        rect,
        (18, 20, 28),
        (12, 13, 18),
        245,
    )

    pygame.draw.line(screen, (58, 64, 82), (0, rect.y), (w, rect.y))
    label = _fit_text(smallfont, text, GUI_TEXT_MUTED, w - 18)
    screen.blit(label, (8, rect.y + (rail_h - label.get_height()) // 2))




def draw_bottom_workspace_tabs(
    screen: pygame.Surface,
    rect: pygame.Rect,
    smallfont: pygame.font.Font,
    active_tab: str,
    mouse_pos: tuple[int, int],
) -> tuple[pygame.Rect, dict[str, pygame.Rect]]:
    """Draw a responsive lower workspace with one calm active accent."""
    mx, my = mouse_pos
    tab_h = 27
    pad = 5

    if rect.width <= 0 or rect.height <= tab_h + 8:
        return rect, {}

    panel = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
    _draw_horizontal_gradient(panel, panel.get_rect(), (10, 20, 32), (5, 10, 18), 250)
    pygame.draw.rect(panel, (37, 53, 70), panel.get_rect(), 1, border_radius=5)
    screen.blit(panel, rect.topleft)

    tabs = [
        ("scan", "NORMALS", GUI_APP_ACCENT),
        ("advantage", "ADVANTAGE", GUI_WARNING),
        ("events", "EVENTS", GUI_CONFIRM),
        ("debug", "DEBUG", GUI_ACCENT_PURPLE),
        ("activity", "ACTIVITY", GUI_DANGER),
    ]

    tab_rects: dict[str, pygame.Rect] = {}
    x = rect.x + pad
    y = rect.y + pad
    gap = 5
    natural_widths = [max(80, min(156, smallfont.size(label)[0] + 30)) for _key, label, _accent in tabs]
    total_natural = sum(natural_widths) + gap * (len(tabs) - 1)
    available = max(120, rect.width - pad * 2)
    if total_natural > available:
        shared = max(58, (available - gap * (len(tabs) - 1)) // len(tabs))
        tab_widths = [shared for _ in tabs]
    else:
        tab_widths = natural_widths

    for (key, label, accent), width in zip(tabs, tab_widths):
        tr = pygame.Rect(x, y, width, tab_h)
        tab_rects[key] = tr
        is_active = key == active_tab
        draw_glass_button(
            screen,
            tr,
            label,
            smallfont,
            active=is_active,
            hover=tr.collidepoint(mx, my),
            accent=accent if is_active else GUI_APP_ACCENT,
            fill=(15, 25, 39),
            align="center",
        )
        x += width + gap

    content = pygame.Rect(
        rect.x + pad,
        rect.y + tab_h + pad + 6,
        rect.width - pad * 2,
        rect.height - tab_h - pad * 2 - 6,
    )
    if content.height < 16:
        content.height = 16

    content_layer = pygame.Surface(content.size, pygame.SRCALPHA)
    _draw_horizontal_gradient(content_layer, content_layer.get_rect(), (8, 16, 27), (4, 9, 16), 250)
    pygame.draw.rect(content_layer, (34, 49, 66), content_layer.get_rect(), 1, border_radius=4)
    screen.blit(content_layer, content.topleft)
    return content, tab_rects


__all__ = [
    'TOP_UI_RESERVED',
    'GUI_BG_DARK',
    'GUI_PANEL',
    'GUI_PANEL_2',
    'GUI_PANEL_3',
    'GUI_BORDER',
    'GUI_BORDER_HOT',
    'GUI_TEXT',
    'GUI_TEXT_MUTED',
    'GUI_TEXT_DIM',
    'GUI_APP_ACCENT',
    'GUI_CONFIRM',
    'GUI_WARNING',
    'GUI_DANGER',
    'GUI_ACCENT_BLUE',
    'GUI_ACCENT_PURPLE',
    'GUI_ACCENT_GOLD',
    'GUI_ACCENT_GREEN',
    'GUI_ACCENT_RED',
    'GUI_P1',
    'GUI_P2',
    'GUI_P3',
    'GUI_P4',
    'GUI_SLOT_MUTED',
    '_clamp_u8',
    '_brighten',
    '_darken',
    '_mix_col',
    '_slot_accent_for_label',
    '_draw_vertical_gradient',
    '_draw_horizontal_gradient',
    '_fit_text',
    '_render_outlined_text',
    '_render_rainbow_outlined_text',
    'draw_glass_button',
    'draw_slot_chip',
    'get_top_dock_height',
    'draw_top_command_dock',
    'character_select_visuals_hit_test',
    'draw_status_rail',
    'draw_bottom_workspace_tabs'
]
