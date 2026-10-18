"""Persistent visual preferences for the in-game overlay."""
from __future__ import annotations

import json
from pathlib import Path

from tvcgui.core.paths import user_data_path


FILE = Path(user_data_path("overlay", "overlay_customization.json"))
DEFAULTS = {
    "show_health": True,
    "show_stun": True,
    "show_inputs": True,
    "show_move_timing": True,
    "show_meter_rail": True,
    "show_damage_scaling": None,
    "opacity": 1.0,
    "p1_color": "#ff6464",
    "p2_color": "#5aa0ff",
    "positions": {},
    "edit_mode": False,
}


def _color(value, fallback):
    value = str(value)
    if len(value) == 7 and value[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in value[1:]):
        return value.lower()
    return fallback


def normalize(data):
    data = data if isinstance(data, dict) else {}
    result = dict(DEFAULTS)
    for key in ("show_health", "show_stun", "show_inputs", "show_move_timing", "show_meter_rail", "edit_mode"):
        result[key] = bool(data.get(key, DEFAULTS[key]))
    override = data.get("show_damage_scaling")
    result["show_damage_scaling"] = None if override is None else bool(override)
    try:
        result["opacity"] = max(0.25, min(1.0, float(data.get("opacity", 1.0))))
    except (TypeError, ValueError):
        result["opacity"] = 1.0
    for key in ("p1_color", "p2_color"):
        result[key] = _color(data.get(key, DEFAULTS[key]), DEFAULTS[key])
    positions = data.get("positions")
    if isinstance(positions, dict):
        result["positions"] = {}
        for team in ("P1", "P2"):
            pair = positions.get(team)
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                try:
                    result["positions"][team] = [max(0.0, min(1.0, float(v))) for v in pair]
                except (TypeError, ValueError):
                    pass
    return result


def load(path=FILE):
    try:
        return normalize(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return normalize({})


def save(data, path=FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = normalize(data)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    temporary.replace(path)
    return clean
