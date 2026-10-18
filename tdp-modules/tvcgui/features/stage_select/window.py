"""Small opt-in control for the experimental Ark stage tile."""
from __future__ import annotations

import tkinter as tk

from . import yami_tile

try:
    from tvcgui.core.tk_host import tk_call
except Exception:  # pragma: no cover
    tk_call = None

_WIN: tk.Toplevel | None = None


def _show(master: tk.Misc | None) -> None:
    global _WIN
    if _WIN is not None and _WIN.winfo_exists():
        _WIN.lift()
        _WIN.focus_force()
        return

    win = tk.Toplevel(master)
    _WIN = win
    win.title("Extra Stages")
    win.configure(bg="#151821")
    win.resizable(False, False)

    enabled = tk.BooleanVar(value=bool(yami_tile.get_state()["enabled"]))

    def _toggle() -> None:
        yami_tile.set_enabled(enabled.get())

    tk.Checkbutton(
        win, text="Enable Ark of Yamato stage", variable=enabled,
        command=_toggle, bg="#151821", fg="#f2f5ff",
        activebackground="#151821", activeforeground="#f2f5ff",
        selectcolor="#1f2430", font=("Segoe UI", 11, "bold"),
        anchor="w",
    ).pack(fill="x", padx=20, pady=(20, 12))
    tk.Label(
        win,
        text="Still investigating: Up from Training skips Ark.\nUse Down to reach Ark of Yamato.",
        bg="#151821", fg="#f4be6a", font=("Segoe UI", 10),
        justify="left", anchor="w",
    ).pack(fill="x", padx=20, pady=(0, 20))

    def _close() -> None:
        global _WIN
        _WIN = None
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", _close)


def open_stage_select_window(master: tk.Misc | None = None) -> None:
    if tk_call is not None:
        tk_call(lambda root: _show(master or root))
    else:
        _show(master)


def open_stage_test_window(master: tk.Misc | None = None) -> None:
    open_stage_select_window(master)
