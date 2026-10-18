"""Build a short audio preview of a custom song's loop seam."""
from __future__ import annotations

from pathlib import Path
import wave


PREVIEW_SIDE_SECONDS = 3.0


def song_duration(mixer, song: Path) -> float:
    """Read the decoded song length without changing streamed music playback."""
    if song.suffix.lower() == '.wav':
        try:
            with wave.open(str(song), 'rb') as audio:
                return audio.getnframes() / audio.getframerate()
        except (wave.Error, EOFError, ZeroDivisionError):
            pass  # pygame can handle WAV variants that wave cannot parse.
    if not mixer.get_init():
        mixer.init(frequency=44100, size=-16, channels=2, buffer=1024)
    decoded = mixer.Sound(str(song))
    return decoded.get_length()


def build_loop_preview(mixer, song: Path, *, start: float, end: float | None):
    """Return (Sound, before_start, boundary, loop_start, after_end).

    This uses a short decoded slice on a mixer channel so the main streamed
    playlist and its position remain untouched. The original Sound is released
    after the preview is assembled.
    """
    import numpy as np
    import pygame

    if not mixer.get_init():
        mixer.init(frequency=44100, size=-16, channels=2, buffer=1024)
    original = mixer.Sound(str(song))
    samples = pygame.sndarray.samples(original)
    rate = mixer.get_init()[0]
    duration = len(samples) / rate
    boundary = duration if end is None else end
    if not 0 <= start < boundary <= duration + 0.02:
        raise ValueError(f'Loop points must be inside the song ({duration:.2f} seconds).')
    boundary = min(boundary, duration)
    before_start = max(0.0, boundary - PREVIEW_SIDE_SECONDS)
    after_end = min(duration, start + PREVIEW_SIDE_SECONDS)
    first = samples[round(before_start * rate):round(boundary * rate)]
    second = samples[round(start * rate):round(after_end * rate)]
    if len(first) == 0 or len(second) == 0:
        raise ValueError('The loop needs audio on both sides of the jump.')
    clip = np.concatenate((first, second), axis=0)
    preview = pygame.sndarray.make_sound(clip)
    return preview, before_start, boundary, start, after_end
