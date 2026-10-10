from __future__ import annotations

import struct
import threading
import time
from typing import Any

try:
    from tvcgui.platform.dolphin import rbytes, wbytes
except Exception:  # pragma: no cover
    rbytes = None
    wbytes = None

_LOCK = threading.RLock()
_LAST_WRITES: dict[int, tuple[bytes, float, str]] = {}
_STATS: dict[str, Any] = {
    "write_calls": 0,
    "write_ok": 0,
    "write_fail": 0,
    "skip_dirty": 0,
    "skip_cache": 0,
    "bytes_written": 0,
    "slow_writes": 0,
    "last_error": "",
    "last_write_age_sec": -1.0,
    "last_write_key": "",
    "last_write_addr": 0,
    "last_write_size": 0,
    "last_write_ms": 0.0,
}
_SLOW_LOG_UNTIL: dict[str, float] = {}
SLOW_WRITE_MS = 20.0
SLOW_LOG_TTL_SEC = 1.0
DEFAULT_CACHE_TTL_SEC = 0.0


def _now() -> float:
    try:
        return time.monotonic()
    except Exception:
        return 0.0


def _set_stat(key: str, value: Any) -> None:
    with _LOCK:
        _STATS[key] = value


def _inc_stat(key: str, amount: int = 1) -> None:
    with _LOCK:
        try:
            _STATS[key] = int(_STATS.get(key, 0) or 0) + int(amount)
        except Exception:
            _STATS[key] = int(amount)


def _read_current(addr: int, size: int) -> bytes | None:
    if rbytes is None:
        return None
    try:
        data = rbytes(int(addr), int(size))
    except Exception:
        return None
    if not data or len(data) != int(size):
        return None
    return bytes(data)


def _rate_limited_slow_log(key: str, text: str) -> None:
    now = _now()
    with _LOCK:
        until = float(_SLOW_LOG_UNTIL.get(key, 0.0) or 0.0)
        if until > now:
            return
        _SLOW_LOG_UNTIL[key] = now + SLOW_LOG_TTL_SEC
    try:
        print(text)
    except Exception:
        pass


def write_bytes(
    addr: int,
    payload: bytes | bytearray,
    *,
    key: str = "runtime",
    priority: int = 50,
    dirty: bool = True,
    force: bool = False,
    cache_ttl_sec: float = DEFAULT_CACHE_TTL_SEC,
    log_slow: bool = True,
) -> bool:
    """Central memory-write gate for runtime patch features.

    This function intentionally stays synchronous for the first pass. It gives
    all runtime systems one place to skip duplicate writes, do dirty reads, keep
    lightweight stats, and clear stale write state during Safe Restore/Hard
    Reset. Worker-thread lanes, such as quick assists, can call it from their
    own thread without blocking the main UI lane.
    """
    if wbytes is None:
        _inc_stat("write_fail")
        _set_stat("last_error", "dolphin_io.wbytes unavailable")
        return False

    try:
        addr_i = int(addr)
        data = bytes(payload)
    except Exception as e:
        _inc_stat("write_fail")
        _set_stat("last_error", f"bad write args: {e!r}")
        return False

    if addr_i <= 0 or not data:
        _inc_stat("write_fail")
        _set_stat("last_error", f"bad write addr/size: 0x{addr_i:08X}/{len(data)}")
        return False

    now = _now()
    key_s = str(key or "runtime")

    if not force and cache_ttl_sec and cache_ttl_sec > 0:
        with _LOCK:
            cached = _LAST_WRITES.get(addr_i)
        if cached and cached[0] == data and (now - cached[1]) <= float(cache_ttl_sec):
            _inc_stat("skip_cache")
            return True

    if not force and dirty:
        cur = _read_current(addr_i, len(data))
        if cur == data:
            with _LOCK:
                _LAST_WRITES[addr_i] = (data, now, key_s)
            _inc_stat("skip_dirty")
            return True

    _inc_stat("write_calls")
    t0 = _now()
    try:
        ok = bool(wbytes(addr_i, data))
    except Exception as e:
        ok = False
        _set_stat("last_error", repr(e))
    elapsed_ms = max(0.0, (_now() - t0) * 1000.0)

    if ok:
        with _LOCK:
            _LAST_WRITES[addr_i] = (data, _now(), key_s)
            _STATS["write_ok"] = int(_STATS.get("write_ok", 0) or 0) + 1
            _STATS["bytes_written"] = int(_STATS.get("bytes_written", 0) or 0) + len(data)
            _STATS["last_write_key"] = key_s
            _STATS["last_write_addr"] = addr_i
            _STATS["last_write_size"] = len(data)
            _STATS["last_write_ms"] = elapsed_ms
            _STATS["last_error"] = ""
    else:
        _inc_stat("write_fail")
        if not _STATS.get("last_error"):
            _set_stat("last_error", f"wbytes returned false at 0x{addr_i:08X}")
        return False

    if log_slow and elapsed_ms >= SLOW_WRITE_MS:
        _inc_stat("slow_writes")
        _rate_limited_slow_log(
            key_s,
            f"[runtime patch] slow write {elapsed_ms:.1f}ms key={key_s} addr=0x{addr_i:08X} size={len(data)}",
        )
    return True


def write_u8(addr: int, value: int, **kwargs: Any) -> bool:
    return write_bytes(int(addr), bytes([int(value) & 0xFF]), **kwargs)


def write_u16(addr: int, value: int, **kwargs: Any) -> bool:
    return write_bytes(int(addr), struct.pack(">H", int(value) & 0xFFFF), **kwargs)


def write_u32(addr: int, value: int, **kwargs: Any) -> bool:
    return write_bytes(int(addr), struct.pack(">I", int(value) & 0xFFFFFFFF), **kwargs)


def write_many(
    writes: dict[int, bytes | bytearray],
    *,
    key: str = "runtime:many",
    priority: int = 50,
    dirty: bool = True,
    force: bool = False,
    cache_ttl_sec: float = DEFAULT_CACHE_TTL_SEC,
) -> bool:
    if not isinstance(writes, dict) or not writes:
        return True
    ok_all = True
    for addr, payload in list(writes.items()):
        ok = write_bytes(
            int(addr),
            bytes(payload),
            key=f"{key}:0x{int(addr):08X}",
            priority=priority,
            dirty=dirty,
            force=force,
            cache_ttl_sec=cache_ttl_sec,
        )
        if not ok:
            ok_all = False
    return ok_all


def clear_runtime_patch_state(*, clear_cache: bool = True, clear_slow_log: bool = True) -> dict[str, Any]:
    with _LOCK:
        cache_count = len(_LAST_WRITES)
        if clear_cache:
            _LAST_WRITES.clear()
        if clear_slow_log:
            _SLOW_LOG_UNTIL.clear()
        _STATS["last_error"] = ""
    return {"cleared_cache_entries": cache_count if clear_cache else 0}


def get_runtime_patch_state() -> dict[str, Any]:
    now = _now()
    with _LOCK:
        stats = dict(_STATS)
        stats["cache_entries"] = len(_LAST_WRITES)
        last_times = [row[1] for row in _LAST_WRITES.values() if row]
        if last_times:
            stats["last_write_age_sec"] = max(0.0, now - max(last_times))
        else:
            stats["last_write_age_sec"] = -1.0
    return stats
