"""Click-release bridge from the pygame command dock to Settings."""
from __future__ import annotations


class SettingsDock:
    def __init__(self):
        self._down = False
        self._started_inside = False

    def frame_mouse(self, *, inside: bool, left_pressed: bool):
        pressed = bool(left_pressed)
        if pressed and not self._down:
            self._started_inside = bool(inside)
        elif self._down and not pressed:
            if self._started_inside and inside:
                from tvcgui.ui.settings_window import open_window
                open_window()
            self._started_inside = False
        self._down = pressed


settings_dock = SettingsDock()
