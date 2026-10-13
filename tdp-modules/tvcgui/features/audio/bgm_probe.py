"""Opt-in MEM1 BGM volume write/readback experiment.

This is deliberately NOT a persistent mute and does not touch the Options
menu's temporary MEM2 copy. It is driven by the existing pygame dock's draw
loop, so old main.py button tuples/event routing remain untouched.

Target 0x803EBA60 was observed tracking native BGM volume as a big-endian f32.
A write is not established as effective until observed via readback, and even
a successful readback does NOT establish that audible BGM has changed.
"""

from __future__ import annotations

import math
import struct
import time

BGM_VOLUME_ADDR = 0x803EBA60
_ZERO = b"\x00\x00\x00\x00"
_CHECK_DELAYS = (0.10, 0.50, 1.50)


class BgmProbe:
    def __init__(self, io=None, clock=None):
        # Dependency injected so we can test without launching Dolphin.
        self._io = io
        self._clock = clock or time.monotonic
        self._original: bytes | None = None
        self._armed = False
        self._pressed_on_button = False
        self._previous_pressed = False
        self._checks: tuple[float, ...] = ()
        self._next_check = 0
        self._issued = 0.0
        self._message = ""
        self._message_until = 0.0
        self._result = "idle"

    def _backend(self):
        if self._io is None:
            from tvcgui.platform import dolphin
            self._io = dolphin
        return self._io

    def _read(self) -> bytes | None:
        try:
            data = self._backend().rbytes(BGM_VOLUME_ADDR, 4)
            return bytes(data) if data is not None and len(data) == 4 else None
        except Exception as exc:
            self._report(f"read error: {exc!r}", result="error")
            return None

    def _write(self, data: bytes) -> bool:
        try:
            return bool(self._backend().wbytes(BGM_VOLUME_ADDR, data))
        except Exception as exc:
            self._report(f"write error: {exc!r}", result="error")
            return False

    @staticmethod
    def _float(data: bytes) -> float | None:
        try:
            value = struct.unpack(">f", data)[0]
            return value if math.isfinite(value) else None
        except (ValueError, struct.error, TypeError):
            return None

    def _report(self, message: str, *, result: str) -> None:
        self._result = result
        self._message = str(message)
        self._message_until = self._clock() + 5.0
        print(f"[Continuo BGM probe] {message}", flush=True)

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def label(self) -> str:
        if self._armed:
            return {
                "set": "BGM: 0 HELD",
                "overwritten": "BGM: REVERTED",
                "failed": "BGM: WRITE FAIL",
                "error": "BGM: ERROR",
            }.get(self._result, "BGM: RESTORE")
        if self._clock() < self._message_until:
            return {
                "restored": "BGM: RESTORED",
                "failed": "BGM: WRITE FAIL",
                "error": "BGM: ERROR",
                "invalid": "BGM: BAD VALUE",
                "no_read": "BGM: NO READ",
                "zero": "BGM: ALREADY 0",
            }.get(self._result, "BGM: ZERO TEST")
        return "BGM: ZERO TEST"

    @property
    def last_message(self) -> str:
        return self._message

    def toggle(self) -> None:
        if self._armed:
            self.restore()
        else:
            self.zero_once()

    def zero_once(self) -> None:
        original = self._read()
        if original is None:
            self._report("MEM1 BGM volume could not be read. Is Dolphin hooked?", result="no_read")
            return
        value = self._float(original)
        if value is None or not 0.0 <= value <= 1.01:
            self._report(f"Refusing write: unexpected BGM value {original.hex(' ')} ({value})", result="invalid")
            return
        if original == _ZERO:
            self._report("Volume already 0. Restore it through Sound Options before testing.", result="zero")
            return
        if not self._write(_ZERO):
            self._report("Write failed (possibly select-scene write quarantine).", result="failed")
            return

        self._original = original
        self._armed = True  # Permit explicit restore even if the game overwrites the zero.
        self._issued = self._clock()
        self._checks = tuple(self._issued + delay for delay in _CHECK_DELAYS)
        self._next_check = 0
        immediate = self._read()
        if immediate != _ZERO:
            self._report(
                f"Write attempted; immediate readback is {immediate.hex(' ') if immediate else 'UNREADABLE'}"
                f" (original {value:.3f}). Check game-side overwrite/write mapping.",
                result="failed",
            )
        else:
            self._report(
                f"Readback ZERO at 0x{BGM_VOLUME_ADDR:08X} (original {value:.3f})."
                " Listen for music; second click restores the original float.",
                result="set",
            )

    def restore(self) -> None:
        self._checks = ()
        self._next_check = 0
        self._armed = False
        original, self._original = self._original, None
        if original is None:
            self._report("No saved BGM value available for restore.", result="error")
            return
        if not self._write(original):
            self._report("Restore write failed. Restore BGM using TvC Sound Options.", result="failed")
            return
        readback = self._read()
        if readback == original:
            self._report(f"Restored original BGM float {self._float(original):.3f}.", result="restored")
        else:
            self._report("Restore did not stick. Restore BGM using TvC Sound Options.", result="failed")

    def update(self) -> None:
        if not self._armed or self._next_check >= len(self._checks):
            return
        if self._clock() < self._checks[self._next_check]:
            return
        # At most one read each frame, never a spin/blocking loop.
        since = self._clock() - self._issued
        current = self._read()
        self._next_check += 1
        if current is None:
            self._report(f"Readback unavailable at +{since:.2f}s.", result="error")
        elif current == _ZERO:
            self._report(f"Zero still stored at +{since:.2f}s. Audible mute remains unverified.", result="set")
        else:
            self._report(
                f"Zero overwritten at +{since:.2f}s. Current {current.hex(' ')}"
                f" (float {self._float(current)}).",
                result="overwritten",
            )
            # Don't keep reading once game ownership is established.
            self._next_check = len(self._checks)

    def frame_mouse(self, *, visible: bool, inside: bool, left_pressed: bool) -> None:
        """Handle a click-release without consuming main.py's pygame events."""
        self.update()
        pressed = bool(left_pressed)
        if not visible:
            self._pressed_on_button = False
        elif pressed and not self._previous_pressed:
            self._pressed_on_button = bool(inside)
        elif not pressed and self._previous_pressed:
            if self._pressed_on_button and inside:
                self.toggle()
            self._pressed_on_button = False
        self._previous_pressed = pressed


probe = BgmProbe()
