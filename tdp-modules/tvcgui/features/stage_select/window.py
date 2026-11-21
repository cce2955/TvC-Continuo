"""Small opt-in control for the experimental Ark stage tile."""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog
import json
import queue
import threading
from pathlib import Path

from . import yami_tile
from .navigation_experiment import NavigationExperiment

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
    experiment = NavigationExperiment()
    export_messages = queue.Queue()
    experiment_status = tk.StringVar(value="Experiment stopped.")

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
    ).pack(fill="x", padx=20, pady=(0, 8))

    controls = tk.Frame(win, bg="#151821")
    controls.pack(fill="x", padx=20)
    button = tk.Button(controls, text="Start navigation experiment")
    button.pack(side="left")

    def _toggle_experiment() -> None:
        if experiment.state()["running"]:
            experiment.stop()
        else:
            experiment.start()

    button.configure(command=_toggle_experiment)

    def _export() -> None:
        path = filedialog.asksaveasfilename(parent=win, title="Export stage navigation experiment",
                                            defaultextension=".jsonl", filetypes=[("JSON Lines", "*.jsonl")])
        if not path:
            return
        events, dropped = experiment.events()
        running = experiment.state()["running"]

        def write_export() -> None:
            try:
                with Path(path).open("w", encoding="utf-8", newline="\n") as handle:
                    for event in events:
                        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                    handle.write(json.dumps({"type": "export_status", "dropped_events": dropped,
                                             "running_at_export": running}) + "\n")
                export_messages.put(f"Exported {len(events)} events to {path}")
            except OSError as exc:
                export_messages.put(f"Export failed: {exc}")

        threading.Thread(target=write_export, name="ArkExperimentExport", daemon=True).start()

    tk.Button(controls, text="Export JSONL", command=_export).pack(side="left", padx=6)
    status_label = tk.Label(win, textvariable=experiment_status, bg="#151821", fg="#f2f5ff",
                            justify="left", anchor="w", wraplength=520)
    status_label.pack(fill="x", padx=20, pady=(6, 14))
    closed = False
    was_running = False

    def _refresh() -> None:
        nonlocal was_running
        if closed:
            return
        state = experiment.state()
        button.configure(text="Stop navigation experiment" if state["running"] else "Start navigation experiment")
        if state["running"]:
            experiment_status.set(f"Recording read-only at 25 ms: {state['events']} events; dropped {state['dropped']}.")
        elif state["error"]:
            experiment_status.set(f"Experiment stopped: {state['error']}")
        elif was_running:
            experiment_status.set(f"Stopped: {state['events']} events; dropped {state['dropped']}.")
        was_running = state["running"]
        try:
            experiment_status.set(export_messages.get_nowait())
        except queue.Empty:
            pass
        win.after(250, _refresh)

    _refresh()

    def _close() -> None:
        nonlocal closed
        global _WIN
        closed = True
        experiment.stop()
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
