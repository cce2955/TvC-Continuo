"""Coalesced background work for features that must not block the HUD."""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional


class DeferredWorkLoop:
    """Run callbacks periodically or on request, with a minimum interval.

    Multiple requests can be coalesced into one callback. Worker exceptions are
    isolated so a background feature cannot stop the HUD loop.
    """

    def __init__(
        self,
        callback: Callable[[], None],
        *,
        interval: float = 0.25,
        name: str = "TvCDeferredWork",
    ) -> None:
        self._callback = callback
        self._interval = max(0.01, float(interval))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def request(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        next_allowed = 0.0
        while not self._stop.is_set():
            timeout = max(0.01, next_allowed - time.monotonic()) if next_allowed else self._interval
            self._wake.wait(timeout)
            self._wake.clear()
            if self._stop.is_set():
                break
            now = time.monotonic()
            if now < next_allowed:
                self._wake.wait(next_allowed - now)
                self._wake.clear()
                if self._stop.is_set():
                    break
            try:
                self._callback()
            except Exception:
                pass
            next_allowed = time.monotonic() + self._interval

    def close(self, *, final_callback: Optional[Callable[[], None]] = None, timeout: float = 1.5) -> None:
        """Request shutdown, wait up to ``timeout``, then run final cleanup.

        A callback that exceeds the timeout can still be running when the final
        callback is invoked, so callers must coordinate any shared resources.
        """
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(0.0, float(timeout)))
        if final_callback is not None:
            try:
                final_callback()
            except Exception:
                pass
