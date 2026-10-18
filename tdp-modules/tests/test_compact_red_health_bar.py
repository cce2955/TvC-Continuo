from __future__ import annotations

import pygame

from tvcgui.features.overlay import hud_renderer
from tvcgui.features.overlay.hud_renderer import _compact_red_health_values, _draw_compact_health


def test_recoverable_amount_restores_ceiling_even_if_auxiliary_is_stale():
    current, ceiling, red, *_ = _compact_red_health_values({
        "cur": 6000, "max": 10000, "red_health_aux": 6000,
        "red_health_recoverable": 1500,
    })
    assert (current, ceiling, red) == (6000, 7500, 1500)


def test_baroque_pool_can_supply_missing_red_amount():
    current, ceiling, red, *_ = _compact_red_health_values({
        "cur": 6000, "max": 10000, "red_health_aux": 6000,
        "red_health_recoverable": 0,
        "baroque_local_hp32": 6000, "baroque_local_pool32": 7200,
    })
    assert (current, ceiling, red) == (6000, 7200, 1200)


def test_health_bar_draws_red_segment_past_current_hp():
    surface = pygame.Surface((110, 16), pygame.SRCALPHA)
    _draw_compact_health(surface, 0, 0, 100, 10, 50, 100, False,
                         recoverable_fraction=0.75)
    red = surface.get_at((65, 5))
    empty = surface.get_at((90, 5))
    assert red.r > red.g
    assert red.r > empty.r


def test_realtime_combat_sample_updates_red_bar_before_full_payload(monkeypatch):
    monkeypatch.setattr(hud_renderer, "read_realtime_stun_data", lambda: {
        "slots": {"P1-C1": {"latest": {
            "base": 0x9246B9C0, "current_hp": 6000, "aux_hp": 7200,
            "action_id": 0, "action_frame": 0,
        }}},
    })
    monkeypatch.setattr(hud_renderer, "read_realtime_move_data", lambda: {})
    slots = {"P1-C1": {"base": 0x9246B9C0, "cur": 8000, "max": 10000}}
    hud_renderer._merge_realtime_inputs(slots, {"slots": {}})
    assert slots["P1-C1"]["cur"] == 6000
    assert _compact_red_health_values(slots["P1-C1"])[1:3] == (7200, 1200)
