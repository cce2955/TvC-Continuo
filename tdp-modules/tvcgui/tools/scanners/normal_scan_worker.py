# Background normal-scan coordinator.
#
# Compact HUD/cache reads stay in a lightweight thread. Expensive dynamic
# discovery runs in a separate process so Python parsing and profile writes
# cannot monopolize the HUD process through the GIL. Workbench opens use the
# in-process saved profile cache and do not pay Windows/PyInstaller spawn cost.

from __future__ import annotations

import multiprocessing
import os
import pickle
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional



def _lower_child_priority() -> None:
    try:
        if os.name == "nt":
            import ctypes
            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
        else:
            os.nice(5)
    except Exception:
        pass


def _scan_process_entry(mode: str, kwargs: dict, compact_path: str, rich_path: str, error_path: str, retain_rich: bool) -> None:
    """Child-process entry point. Must remain top-level for Windows spawn."""
    try:
        _lower_child_priority()
        from tvcgui.tools.scanners import normal_scanner

        if mode == "full":
            rich = normal_scanner.scan_once(
                force_dynamic=True,
                cache_only=False,
                preview_only=False,
                **dict(kwargs or {}),
            )
        else:
            rich = normal_scanner.scan_once(
                force_dynamic=False,
                cache_only=True,
                preview_only=False,
            )

        # Dynamic scans persist their profile before returning. Read the tiny
        # immutable preview afterward so the HUD never needs the rich result.
        compact = normal_scanner.scan_once(
            force_dynamic=False,
            cache_only=True,
            preview_only=True,
        )
        with open(compact_path, "wb") as handle:
            pickle.dump(compact, handle, protocol=pickle.HIGHEST_PROTOCOL)
        if retain_rich:
            with open(rich_path, "wb") as handle:
                pickle.dump(rich, handle, protocol=pickle.HIGHEST_PROTOCOL)
    except BaseException:
        try:
            Path(error_path).write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise


class ScanNormalsWorker(threading.Thread):
    """Coalescing scan coordinator with process-isolated heavy work."""

    def __init__(
        self,
        scan_func,
        full_scan_func=None,
        workbench_scan_func=None,
        *,
        isolate_heavy_scans: bool = True,
    ):
        super().__init__(daemon=True)
        self._scan_func = scan_func
        self._full_scan_func = full_scan_func
        self._workbench_scan_func = workbench_scan_func
        self._isolate_heavy_scans = bool(isolate_heavy_scans)
        self._want = threading.Event()
        self._lock = threading.Lock()
        self._last = None
        self._last_ts = 0.0
        self._busy = False
        self._request_count = 0
        self._want_full = False
        self._want_workbench = False
        self._full_kwargs = {}
        self._retain_rich = True
        self._last_mode = "none"
        self._rich_results = []
        self._rich_generation = 0
        self._active_process: Optional[multiprocessing.Process] = None
        self._cached_compact_after_heavy = None

    @staticmethod
    def _load_pickle(path: str):
        with open(path, "rb") as handle:
            return pickle.load(handle)

    def _run_isolated(self, mode: str, kwargs: dict, *, retain_rich: bool):
        temp_dir = tempfile.mkdtemp(prefix="tvc_fd_scan_")
        compact_path = os.path.join(temp_dir, "compact.pkl")
        rich_path = os.path.join(temp_dir, "rich.pkl")
        error_path = os.path.join(temp_dir, "error.txt")
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_scan_process_entry,
            args=(mode, dict(kwargs or {}), compact_path, rich_path, error_path, bool(retain_rich)),
            name=f"TvCFrameData-{mode}",
            daemon=True,
        )
        with self._lock:
            self._active_process = process
        process.start()
        while process.is_alive():
            process.join(timeout=0.05)
            # This thread sleeps while the child parses, leaving the HUD's GIL
            # and frame cadence untouched.
            time.sleep(0.001)
        process.join(timeout=0.1)
        with self._lock:
            self._active_process = None

        if process.exitcode != 0 or not os.path.exists(compact_path):
            message = "isolated scan failed"
            try:
                message = Path(error_path).read_text(encoding="utf-8").strip() or message
            except Exception:
                pass
            raise RuntimeError(message)

        compact = self._load_pickle(compact_path)
        rich = self._load_pickle(rich_path) if retain_rich and os.path.exists(rich_path) else None
        for path in (compact_path, rich_path, error_path):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass
        return compact, rich

    def run(self):
        while True:
            self._want.wait()
            self._want.clear()
            if self._scan_func is None:
                continue

            try:
                with self._lock:
                    self._busy = True
                    want_full = bool(self._want_full)
                    want_workbench = bool(self._want_workbench)
                    full_kwargs = dict(self._full_kwargs or {})
                    retain_rich = bool(self._retain_rich)
                    self._retain_rich = True
                    self._want_full = False
                    self._want_workbench = False
                    self._full_kwargs = {}

                mode = "cache"
                rich = None
                if want_full and self._full_scan_func is not None:
                    mode = "full"
                    if self._isolate_heavy_scans:
                        try:
                            compact, rich = self._run_isolated(mode, full_kwargs, retain_rich=retain_rich)
                            if retain_rich and rich is not None:
                                res = rich
                                self._cached_compact_after_heavy = compact
                            else:
                                res = compact
                                mode = "full_compact"
                        except Exception as isolated_error:
                            print(f"[fd profile] isolated scan unavailable, using compatibility fallback: {isolated_error}", flush=True)
                            res = self._full_scan_func(**full_kwargs) if full_kwargs else self._full_scan_func()
                            rich = res
                    else:
                        res = self._full_scan_func(**full_kwargs) if full_kwargs else self._full_scan_func()
                        rich = res
                elif want_workbench and self._workbench_scan_func is not None:
                    # Workbench requests are cache-only profile loads. Spawning
                    # a second Python/PyInstaller process on Windows is very
                    # expensive and also throws away the in-process profile
                    # cache. Dynamic/full discovery remains process-isolated.
                    mode = "workbench"
                    res = None
                    # The compact scanner already resolved slot identity/table
                    # signature. If that snapshot is fresh, load the rich saved
                    # profile directly from it with zero additional Dolphin
                    # table reads. Fall back when stale/invalid so correctness
                    # always wins over startup speed.
                    try:
                        age = time.time() - float(self._last_ts or 0.0)
                        snapshot = self._last if 0.0 <= age <= 2.0 else None
                        if snapshot is not None:
                            from tvcgui.tools.scanners import normal_scanner
                            res = normal_scanner.load_workbench_from_snapshot(snapshot)
                    except Exception:
                        res = None
                    if res is None:
                        res = self._workbench_scan_func()
                    rich = res
                else:
                    if self._cached_compact_after_heavy is not None:
                        res = self._cached_compact_after_heavy
                        self._cached_compact_after_heavy = None
                    else:
                        res = self._scan_func()

                now = time.time()
                with self._lock:
                    self._last = res
                    self._last_ts = now
                    self._last_mode = mode
                    if mode in {"workbench", "full"} and rich is not None:
                        self._rich_generation += 1
                        self._rich_results.append((
                            int(self._rich_generation), rich, now, mode
                        ))
                        if len(self._rich_results) > 4:
                            del self._rich_results[:-4]
            except Exception as error:
                print(f"scan worker failed: {error}", flush=True)
            finally:
                with self._lock:
                    self._busy = False

    def request(self, *, force_dynamic: bool = False, workbench: bool = False, retain_rich: bool = True, **full_scan_kwargs):
        with self._lock:
            self._request_count += 1
            if force_dynamic:
                self._want_full = True
                self._retain_rich = bool(self._retain_rich and retain_rich)
            elif workbench:
                self._want_workbench = True
                self._retain_rich = bool(self._retain_rich and retain_rich)
            if force_dynamic:
                if full_scan_kwargs:
                    if self._full_kwargs:
                        merged = dict(self._full_kwargs)
                        for key, value in full_scan_kwargs.items():
                            if key == "dynamic_char_ids":
                                old = set(merged.get(key) or ())
                                old.update(value or ())
                                merged[key] = tuple(sorted(old))
                            else:
                                merged[key] = value
                        self._full_kwargs = merged
                    else:
                        normalized = dict(full_scan_kwargs)
                        if "dynamic_char_ids" in normalized:
                            normalized["dynamic_char_ids"] = tuple(sorted(set(normalized.get("dynamic_char_ids") or ())))
                        self._full_kwargs = normalized
                else:
                    self._full_kwargs = {}
        self._want.set()

    def is_busy(self):
        with self._lock:
            return bool(self._busy)

    def request_count(self):
        with self._lock:
            return int(self._request_count)

    def last_mode(self):
        with self._lock:
            return str(self._last_mode)

    def drain_completed_rich_results(self):
        with self._lock:
            completed = list(self._rich_results)
            self._rich_results.clear()
            return completed

    def get_latest(self):
        with self._lock:
            return self._last, self._last_ts
