"""Self-contained click-release bridge to the existing pygame LAB dock."""
from __future__ import annotations

from tvcgui.features.audio.music_player import controller

class MusicDock:
    def __init__(self):
        self._down = False
        self._inside = False
        self._mode_down = None

    def frame_modes(self, *, rects: dict, mouse_pos, left_pressed: bool):
        """Three independent mode segments in the top HUD row."""
        pressed = bool(left_pressed)
        hit = next((name for name, rect in rects.items()
                    if rect.collidepoint(*mouse_pos)), None)
        if pressed and self._mode_down is None:
            # Only capture a click when it began inside this control.
            self._mode_down = hit or '_outside'
        elif not pressed and self._mode_down is not None:
            chosen = self._mode_down
            self._mode_down = None
            if chosen == hit and chosen in ('native', 'custom', 'no_bgm'):
                try:
                    controller.set_output_mode(chosen)
                except Exception as ex:
                    controller._message = f'Music selection failed: {ex!s}'

    def frame_mouse(self, *, visible: bool, inside: bool, left_pressed: bool):
        # mixer operations happen on the existing pygame GUI thread.
        controller.tick()
        p = bool(left_pressed)
        if not visible:
            self._inside = False
        elif p and not self._down:
            self._inside = bool(inside)
        elif self._down and not p:
            if self._inside and inside:
                from tvcgui.features.audio.music_window import open_window
                open_window()
            self._inside = False
        self._down = p

music_dock = MusicDock()
