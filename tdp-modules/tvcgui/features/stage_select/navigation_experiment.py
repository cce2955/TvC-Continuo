"""Bounded, read-only observations of the stage carousel."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import threading
import time

from . import runtime, yami_tile

MAX_EVENTS = 5000
INTERVAL = 0.025
MAX_SECONDS = 120.0


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def observe(*, include_regions=False):
    """Read one sequential sample; no input mapping or frame clock is assumed."""
    menu = yami_tile._read_live_menu()
    if not menu:
        return {"menu": "unavailable"}
    base, root = menu
    mem = yami_tile.mem
    blob = mem.rbytes(root, 0x100)
    words = ({f"+0x{off:02x}": int.from_bytes(blob[off:off + 4], "big")
              for off in range(0, 0x100, 4)} if blob is not None and len(blob) == 0x100 else None)
    sample = {
        "menu": "validated", "base": base, "root": root,
        "ark_enabled": bool(yami_tile.get_state()["enabled"]),
        "cursor_global": mem.rd32(runtime.STAGE_GLOBAL_SELECTION),
        "cursor_object": mem.rd32(root + runtime.RUNTIME_SELECTION_OFF),
        "stage_result": mem.rd32(runtime.STAGE_RESULT_ID),
        "input_word_candidate": mem.rd32(0x80473878),
        "root_words": words,
        "patch_bytes": {hex(off): mem.rd8(base + off) for off in yami_tile._PATCH},
    }
    if include_regions:
        regions = {}
        for offset in (0x25B40, 0x260C0):
            data = mem.rbytes(base + offset, 0xC0)
            regions[hex(offset)] = data.hex() if data is not None and len(data) == 0xC0 else None
        sample.update(region_offsets_are="loaded container relative", regions=regions)
    return sample


class NavigationExperiment:
    def __init__(self):
        self._lock = threading.Lock()
        self._events = deque(maxlen=MAX_EVENTS)
        self._stop = threading.Event()
        self._thread = None
        self._dropped = 0
        self._error = None

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._events.clear()
            self._dropped = 0
            self._error = None
            self._stop = threading.Event()
            self._events.append({"type": "metadata", "timestamp_utc": _timestamp(),
                                 "method": "read-only sequential polling", "interval_seconds": INTERVAL,
                                 "duration_limit_seconds": MAX_SECONDS,
                                 "ark_enabled_at_start": bool(yami_tile.get_state()["enabled"]),
                                 "note": "Input mapping and game-frame timing unverified."})
            self._thread = threading.Thread(target=self._run, daemon=True, name="ArkNavigationExperiment")
            self._thread.start()
        return True

    def _run(self):
        last = None
        menu_key = None
        deadline = time.monotonic() + MAX_SECONDS
        try:
            while not self._stop.is_set() and time.monotonic() < deadline:
                sample = observe()
                key = (sample.get("base"), sample.get("root")) if sample.get("menu") == "validated" else None
                if key is not None and key != menu_key:
                    detailed = observe(include_regions=True)
                    if (detailed.get("base"), detailed.get("root")) == key:
                        with self._lock:
                            if len(self._events) == MAX_EVENTS:
                                self._dropped += 1
                            self._events.append({"type": "menu_observed", "timestamp_utc": _timestamp(), **detailed})
                menu_key = key
                if sample != last:
                    with self._lock:
                        if len(self._events) == MAX_EVENTS:
                            self._dropped += 1
                        self._events.append({"type": "observation", "timestamp_utc": _timestamp(), **sample})
                    last = sample
                self._stop.wait(INTERVAL)
        except Exception as exc:
            with self._lock:
                self._error = str(exc)

    def stop(self):
        self._stop.set()

    def state(self):
        with self._lock:
            return {"running": self._thread is not None and self._thread.is_alive(),
                    "events": len(self._events), "dropped": self._dropped, "error": self._error}

    def events(self):
        with self._lock:
            return list(self._events), self._dropped
