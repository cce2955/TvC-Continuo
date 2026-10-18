"""HUD settings commands and named presets shared by Tk and the Pygame loop."""
from __future__ import annotations

import json
from pathlib import Path
from queue import Empty, SimpleQueue

from tvcgui.core.paths import user_data_path
from tvcgui.features.overlay import customization


HUD_KEYS = (
    "show_interaction_card", "show_combo_card", "show_damage_badge",
    "show_untech_panel", "show_meter_panel", "show_red_health_panel",
    "show_tag_card",
)
VISIBLE_KEYS = ("show_health", "show_inputs", "show_move_timing", "show_meter_rail")
PRESETS_FILE = Path(user_data_path("overlay", "hud_presets.json"))
_commands = SimpleQueue()


def submit(command: str, value) -> None:
    _commands.put((command, value))


def drain():
    while True:
        try:
            yield _commands.get_nowait()
        except Empty:
            return


def save_visible_settings(values: dict):
    """Apply the switches that control the main overlay rows."""
    data = customization.load()
    for key in VISIBLE_KEYS:
        if key in values:
            data[key] = bool(values[key])
    if "show_untech_panel" in values:
        data["show_stun"] = bool(values["show_untech_panel"])
    if "show_damage_badge" in values:
        # An old Appearance override must not mask the HUD Settings switch.
        data["show_damage_scaling"] = bool(values["show_damage_badge"])
    return customization.save(data)


def _clean_name(name: str) -> str:
    name = " ".join(str(name).split())
    if not name or len(name) > 48 or any(c in name for c in "\\/<>:\"|?*"):
        raise ValueError("Enter a preset name of up to 48 characters without path symbols.")
    return name


def load_presets(path=PRESETS_FILE) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result = {}
    for name, preset in raw.items():
        try:
            clean = _clean_name(name)
        except ValueError:
            continue
        if isinstance(preset, dict) and isinstance(preset.get("hud"), dict):
            result[clean] = {
                "hud": {key: bool(preset["hud"].get(key, customization.DEFAULTS.get(key, False)))
                        for key in (*HUD_KEYS, *VISIBLE_KEYS)},
                "overlay": customization.normalize(preset.get("overlay")),
            }
    return result


def save_preset(name, hud, overlay=None, path=PRESETS_FILE):
    clean = _clean_name(name)
    presets = load_presets(path)
    appearance = customization.normalize(overlay if overlay is not None else customization.load())
    appearance["edit_mode"] = False
    presets[clean] = {
        "hud": {key: bool(hud.get(key, customization.DEFAULTS.get(key, False)))
                for key in (*HUD_KEYS, *VISIBLE_KEYS)},
        "overlay": appearance,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(presets, indent=2), encoding="utf-8")
    temporary.replace(target)
    return clean


def delete_preset(name, path=PRESETS_FILE):
    presets = load_presets(path)
    presets.pop(name, None)
    target = Path(path)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(presets, indent=2), encoding="utf-8")
    temporary.replace(target)
