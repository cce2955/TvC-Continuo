# tk_host.py
#
# Dedicated Tk host running on its own thread, with a hidden root.
# All Tk operations must be executed on the Tk thread via tk_call().
#
# Guarantees:
# - Exactly one tk.Tk() instance (the hidden host root)
# - Application windows are tk.Toplevel(root)
# - tk_call() is safe to use immediately after ensure_tk_host(); it waits for root readiness
#
# Optional:
# - tk_call_sync() to get a return value or raise exceptions on the caller thread

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Optional


_TK_THREAD: Optional[threading.Thread] = None
_TK_ROOT: Optional[tk.Tk] = None

# The module signal when _TK_ROOT is created and the pump loop is active.
_TK_READY = threading.Event()

# Task queue to run on Tk thread.
_TK_QUEUE: "queue.Queue[_Task]" = queue.Queue()

# How often an idle host checks for new background->Tk work.
#
# The queue used to be drained "until empty" in one Tk callback.  Frame-data
# background scans can enqueue hundreds of progress updates, which kept that
# callback alive long enough to starve ButtonPress/TreeviewSelect handling.
# Native scrolling may still appear to work in that state, making it look like
# only clicks are broken.  Bound each pass and yield to Tk between slices.
_PUMP_MS = 16
_PUMP_MAX_TASKS = 24
_PUMP_BUDGET_SECONDS = 0.004


@dataclass
class _Task:
    fn: Callable[[tk.Tk], Any]
    done: Optional[threading.Event] = None
    out: Any = None
    err: Optional[BaseException] = None


def _tk_thread_main() -> None:
    global _TK_ROOT

    root = tk.Tk()
    root.withdraw()  # hidden host root
    _TK_ROOT = root

    # Root is now usable.
    _TK_READY.set()

    def pump() -> None:
        # Never drain an unbounded producer queue in one Tk callback.  The
        # host must return to mainloop regularly so Windows can dispatch real
        # pointer/button events to the Toplevel.
        processed = 0
        deadline = time.perf_counter() + _PUMP_BUDGET_SECONDS
        while processed < _PUMP_MAX_TASKS and time.perf_counter() < deadline:
            try:
                task = _TK_QUEUE.get_nowait()
            except queue.Empty:
                break

            try:
                task.out = task.fn(root)
            except BaseException as e:
                task.err = e
                try:
                    traceback.print_exception(type(e), e, e.__traceback__)
                except Exception:
                    pass
            finally:
                if task.done is not None:
                    task.done.set()
            processed += 1

        # If work remains, continue promptly but still as a *new* callback so
        # input, redraw, and selection events run between slices.
        try:
            has_backlog = not _TK_QUEUE.empty()
        except Exception:
            has_backlog = False
        root.after(1 if has_backlog else _PUMP_MS, pump)

    pump()
    root.mainloop()


def ensure_tk_host() -> None:
    global _TK_THREAD

    if _TK_THREAD and _TK_THREAD.is_alive():
        return

    _TK_READY.clear()
    _TK_THREAD = threading.Thread(target=_tk_thread_main, daemon=True, name="TkHostThread")
    _TK_THREAD.start()

    # Wait until Tk root is created so tk_call() can safely enqueue work.
    _TK_READY.wait(timeout=5.0)


def tk_call_widget(widget: Any, fn: Callable[[], Any]) -> None:
    """Compatibility helper for background workers targeting an existing widget.

    Every TvCGUI Toplevel is owned by the dedicated Tk host thread, so the
    safest way to marshal a no-argument widget callback is through the same
    host queue rather than calling ``widget.after`` from the worker thread.
    ``widget`` is accepted for the older call-site API and intentionally not
    dereferenced off-thread.
    """
    _ = widget
    tk_call(lambda _root: fn())


def tk_call(fn: Callable[[tk.Tk], Any]) -> None:
    '\n    Fire-and-forget: schedule fn(root) on Tk thread.\n    Exceptions are swallowed (but printed to stderr) unless the operator use tk_call_sync().\n    '
    ensure_tk_host()
    task = _Task(fn=fn, done=None)
    _TK_QUEUE.put(task)


def tk_call_sync(fn: Callable[[tk.Tk], Any], timeout: float = 10.0) -> Any:
    """
    Schedule fn(root) on Tk thread and wait for result.
    If fn raises, re-raise on caller thread.
    """
    ensure_tk_host()
    done = threading.Event()
    task = _Task(fn=fn, done=done)
    _TK_QUEUE.put(task)

    if not done.wait(timeout=timeout):
        raise TimeoutError("tk_call_sync timed out waiting for Tk task to complete")

    if task.err is not None:
        raise task.err

    return task.out


def get_tk_root() -> Optional[tk.Tk]:
    """
    Returns the hidden Tk root if created; otherwise None.
    """
    return _TK_ROOT
