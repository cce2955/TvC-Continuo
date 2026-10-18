"""Bounded, read-only recordings of the shared HUD fighter snapshots."""
from __future__ import annotations

from collections import deque
import json
import math
import os
from pathlib import Path
import tempfile
import threading


FORMAT = "tvc-continuo-session"
VERSION = 1
MAX_FRAMES = 3600  # the latest minute at 60 FPS
MAX_EVENTS = 4000
MAX_SESSION_BYTES = 30_000_000
FIGHTER_FIELDS = ("id", "name", "cur", "max", "aux", "meter", "attA", "attB", "x", "y")


def _scalar(value):
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


class SessionRecorder:
    def __init__(self, capacity: int = MAX_FRAMES):
        self._frames = deque(maxlen=max(1, min(int(capacity), MAX_FRAMES)))
        self._events = deque(maxlen=MAX_EVENTS)
        self._lock = threading.Lock()
        self._recording = False

    def start(self) -> None:
        with self._lock:
            self._frames.clear()
            self._events.clear()
            self._recording = True

    def stop(self) -> None:
        with self._lock:
            self._recording = False

    def status(self) -> tuple[bool, int]:
        with self._lock:
            return self._recording, len(self._frames)

    def capture(self, frame_index: int, timestamp: float, fighters: dict,
                *, context: dict | None = None) -> None:
        with self._lock:
            if not self._recording:
                return
            selected = {}
            for slot, snap in (fighters or {}).items():
                if not isinstance(snap, dict):
                    continue
                selected[str(slot)] = {key: _scalar(snap.get(key)) for key in FIGHTER_FIELDS}
            frame = {
                "frame": int(frame_index),
                "time": float(timestamp),
                "fighters": selected,
                "context": {str(k): _scalar(v) for k, v in (context or {}).items()},
            }
            self._frames.append(frame)

    def frames(self) -> list[dict]:
        with self._lock:
            return list(self._frames)

    def capture_event(self, timestamp: float, kind: str, values: dict) -> None:
        """Keep scene and music changes alongside the recorded HUD frames."""
        with self._lock:
            if not self._recording:
                return
            data = {str(key): _scalar(value) for key, value in values.items()}
            if self._events and self._events[-1]["kind"] == kind and self._events[-1]["values"] == data:
                return
            self._events.append({"time": float(timestamp), "kind": str(kind), "values": data})

    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    def save(self, destination: Path) -> int:
        frames = self.frames()
        if not frames:
            raise ValueError("No session frames have been recorded")
        payload = json.dumps({"format": FORMAT, "version": VERSION,
                              "frames": frames, "events": self.events()},
                             separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_SESSION_BYTES:
            raise ValueError("Recording is too large to save")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".session-", delete=False) as file:
            temporary = Path(file.name)
            file.write(payload)
        try:
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return len(frames)


def load_session(source: Path) -> list[dict]:
    """Load a recording for offline, read-only timeline playback."""
    source = Path(source)
    if source.stat().st_size > MAX_SESSION_BYTES:
        raise ValueError("Recording exceeds the size limit")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("format") != FORMAT or payload.get("version") != VERSION:
        raise ValueError("Unsupported session recording")
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames or len(frames) > MAX_FRAMES:
        raise ValueError("Recording has no valid frame timeline")
    for frame in frames:
        if (not isinstance(frame, dict) or
                not isinstance(frame.get("fighters"), dict) or
                not isinstance(frame.get("context"), dict) or
                any(not isinstance(fighter, dict)
                    for fighter in frame["fighters"].values())):
            raise ValueError("Recording contains an invalid frame")
    return frames


recorder = SessionRecorder()
