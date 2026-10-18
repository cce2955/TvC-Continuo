"""Opt-in, guarded runtime Ark tile for the native stage carousel.

The stock carousel omits cursor 15 on its forward edge. Its loaded SEQ can
expose the native Ark art and Down navigation without changing a game file.
Up from Training still skips Ark while the reverse path is investigated.
"""
from __future__ import annotations

import atexit
import threading
import time
from typing import Any

from tvcgui.platform import dolphin as mem

from . import runtime

_PATCH = {0x25B7F: (15, 17), 0x25B87: (15, 17), 0x25BAB: (15, 16), 0x26103: (15, 16)}
_SEQ_PTR = 0x809BCE38
_SIGNATURE = b"fpack/menu/001/0000.fpk\x00"
_POLL_SEC = 0.006
_LOCK = threading.RLock()
_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_STATE: dict[str, Any] = {
    "enabled": False,
    "phase": "off",
    "detail": "Ark tile is off.",
    "last_transition": "",
}


def _read_live_menu() -> tuple[int, int] | None:
    try:
        base = mem.rd32(_SEQ_PTR)
        root = mem.rd32(runtime.STAGE_RUNTIME_PTR)
    except Exception:
        return None
    if not base or not runtime._in_mem2(base) or not root or not runtime._in_mem2(root):
        return None
    try:
        if mem.rbytes(base + 0x40, len(_SIGNATURE)) != _SIGNATURE:
            return None
        if mem.rd32(root + runtime.RUNTIME_ENTRY_COUNT_OFF) != 15:
            return None
        if mem.rd32(root + runtime.RUNTIME_SELF_PTR_OFF) != root:
            return None
        if mem.rd32(runtime.SELECT_SCENE_GLOBAL) != 0:
            return None
        if mem.rd32(root + runtime.CHARACTER_UI_STATE_OFF) != 0:
            return None
    except Exception:
        return None
    return int(base), int(root)


def _write8(address: int, value: int) -> bool:
    # The character-select write quarantine is broad enough to cover this
    # stage scene. These four verified SEQ bytes are an explicit opt-in route.
    try:
        return bool(mem.wd8(address, value, bypass_quarantine=True))
    except TypeError:  # lightweight memory fakes in tests
        return bool(mem.wd8(address, value))


def _restore(base: int, changed: list[int]) -> None:
    try:
        signature = mem.rbytes(base + 0x40, len(_SIGNATURE))
    except Exception:
        return
    if signature != _SIGNATURE:
        return
    for offset in reversed(changed):
        old, new = _PATCH[offset]
        try:
            if mem.rd8(base + offset) == new:
                _write8(base + offset, old)
        except Exception:
            pass


def _apply(base: int) -> list[int]:
    for offset, (old, _new) in _PATCH.items():
        if mem.rd8(base + offset) != old:
            raise RuntimeError(f"Stage script differs at +0x{offset:X}; Ark tile not applied.")
    changed: list[int] = []
    try:
        for offset, (_old, new) in _PATCH.items():
            if not _write8(base + offset, new) or mem.rd8(base + offset) != new:
                raise RuntimeError(f"Stage script write failed at +0x{offset:X}.")
            changed.append(offset)
    except Exception:
        _restore(base, changed)
        raise
    return changed


def _run(stop: threading.Event) -> None:
    base = 0
    changed: list[int] = []
    previous: int | None = None
    armed_for_hover = False
    last_arm_attempt = 0.0
    try:
        while not stop.is_set():
            menu = _read_live_menu()
            if menu is None or (base and menu[0] != base):
                if changed:
                    _restore(base, changed)
                    changed = []
                base = 0
                previous = None
                armed_for_hover = False
                with _LOCK:
                    _STATE["phase"] = "waiting"
                    _STATE["detail"] = "Waiting for the native stage carousel."
                stop.wait(0.10)
                continue
            current_base, root = menu
            if not changed:
                try:
                    changed = _apply(current_base)
                except RuntimeError as exc:
                    with _LOCK:
                        _STATE["phase"] = "refused"
                        _STATE["detail"] = str(exc)
                    stop.wait(0.25)
                    continue
                base = current_base
                previous = mem.rd32(root + runtime.RUNTIME_SELECTION_OFF)
                with _LOCK:
                    _STATE["phase"] = "active"
                    _STATE["detail"] = "Ark and Training are separate tiles; use Down to reach Ark."

            now = time.monotonic()
            current = mem.rd32(root + runtime.RUNTIME_SELECTION_OFF)
            if current is not None and previous is not None and current != previous:
                with _LOCK:
                    _STATE["last_transition"] = f"{previous} -> {current}"
            previous = current
            if current == 15 and mem.rd32(runtime.STAGE_GLOBAL_SELECTION) == 15:
                if not armed_for_hover and now - last_arm_attempt >= 1.0:
                    last_arm_attempt = now
                    armed_for_hover, detail = runtime._arm_slot2_yami_override(root, source_code=15)
                    if not armed_for_hover:
                        with _LOCK:
                            _STATE["detail"] = detail
            elif current != 15:
                armed_for_hover = False
            stop.wait(_POLL_SEC)
    except Exception as exc:
        with _LOCK:
            _STATE["phase"] = "error"
            _STATE["detail"] = f"Ark tile worker stopped: {exc!r}"
    finally:
        if changed:
            _restore(base, changed)
        with _LOCK:
            if not _STATE["enabled"]:
                _STATE["phase"] = "off"
                _STATE["detail"] = "Ark tile is off."
            elif _STATE["phase"] != "error":
                _STATE["phase"] = "waiting"
                _STATE["detail"] = "Waiting for the native stage carousel."


def ensure_running() -> None:
    global _THREAD, _STOP
    with _LOCK:
        if (not _STATE["enabled"] or _STATE["phase"] == "error"
                or (_THREAD is not None and _THREAD.is_alive())):
            return
        _STOP = threading.Event()
        _THREAD = threading.Thread(target=_run, args=(_STOP,), name="YamiStageTile", daemon=True)
        _THREAD.start()


def set_enabled(enabled: bool) -> dict[str, Any]:
    global _THREAD
    enabled = bool(enabled)
    with _LOCK:
        _STATE["enabled"] = enabled
        if enabled:
            _STATE["phase"] = "waiting"
            _STATE["detail"] = "Waiting for the native stage carousel."
        thread = _THREAD
        stop = _STOP
    if not enabled:
        stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        route = runtime._slot2_yami_snapshot()
        if route.get("active") and int(route.get("source_code") or 2) == 15:
            runtime._cancel_slot2_yami_if_active(restore=True)
    if enabled:
        ensure_running()
    return get_state()


def get_state() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATE)


def _shutdown() -> None:
    _STOP.set()
    thread = _THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=0.5)


atexit.register(_shutdown)
