"""Settings backup and read-only session recording tools."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

from tvcgui.core.paths import user_data_path
from tvcgui.core.settings_bundle import export_settings, schedule_settings_import
from tvcgui.core.tk_host import tk_call
from tvcgui.runtime.session_recording import load_session_payload, recorder


_WINDOW = None
BG = "#0a111c"
PANEL = "#122138"
TEXT = "#e8f0f8"
MUTED = "#9daec2"
ACCENT = "#35aadc"


def _button(parent, label, command):
    return tk.Button(parent, text=label, command=command, bg=PANEL, fg=TEXT,
                     activebackground="#24405a", activeforeground=TEXT,
                     relief="flat", padx=12, pady=7, font=("Segoe UI", 9))


def _show_replay(parent, frames: list[dict]) -> None:
    """Inspect captured frames without connecting to or writing to Dolphin."""
    window = tk.Toplevel(parent)
    window.title("Continuo  |  Session Replay")
    window.geometry("620x365")
    window.configure(bg=BG)
    playing = [False]
    play_generation = [0]
    index = tk.IntVar(value=0)
    header = tk.Label(window, bg=BG, fg=ACCENT, font=("Segoe UI", 11, "bold"))
    header.pack(anchor="w", padx=15, pady=(15, 6))
    body = tk.Label(window, bg=BG, fg=TEXT, font=("Consolas", 10),
                    justify="left", anchor="nw")
    body.pack(fill="both", expand=True, padx=15)

    def show(value=None):
        frame = frames[min(max(int(float(index.get() if value is None else value)), 0), len(frames) - 1)]
        context = frame.get("context") or {}
        header.configure(text=(f"Frame {frame.get('frame', '?')}   "
                               f"Scene {context.get('scene') or '?'}   "
                               f"Stage {context.get('stage') if context.get('stage') is not None else '?'}   "
                               f"Phase {context.get('phase') if context.get('phase') is not None else '?'}"))
        lines = [f"Music: {context.get('music_owner') or '-'}", ""]
        for slot in ("P1-C1", "P1-C2", "P2-C1", "P2-C2"):
            fighter = (frame.get("fighters") or {}).get(slot) or {}
            lines.append(
                f"{slot:6}  {str(fighter.get('name') or '-'):20.20} "
                f"ID {str(fighter.get('id') if fighter.get('id') is not None else '-'):>3}  "
                f"HP {str(fighter.get('cur') if fighter.get('cur') is not None else '-'):>6}/"
                f"{str(fighter.get('max') if fighter.get('max') is not None else '-'):>6}  "
                f"Meter {str(fighter.get('meter') if fighter.get('meter') is not None else '-'):>5}  "
                f"Action {str(fighter.get('attA') if fighter.get('attA') is not None else '-'):>5}"
            )
        body.configure(text="\n".join(lines))

    scale = tk.Scale(window, from_=0, to=len(frames) - 1, variable=index,
                     orient="horizontal", command=show, showvalue=False,
                     bg=BG, fg=MUTED, troughcolor=PANEL, highlightthickness=0)
    scale.pack(fill="x", padx=15)

    def advance(generation):
        if not window.winfo_exists() or not playing[0] or generation != play_generation[0]:
            return
        if index.get() >= len(frames) - 1:
            playing[0] = False
            play_button.configure(text="Play")
            return
        index.set(index.get() + 1)
        show()
        window.after(16, lambda: advance(generation))

    def toggle():
        playing[0] = not playing[0]
        play_generation[0] += 1
        play_button.configure(text="Pause" if playing[0] else "Play")
        if playing[0]:
            advance(play_generation[0])

    play_button = _button(window, "Play", toggle)
    play_button.pack(anchor="w", padx=15, pady=(3, 15))
    show()


def _show_mission_events(parent, events: list[dict]) -> None:
    mission_events = [event for event in events if str(event.get("kind", "")).startswith("mission_")]
    window = tk.Toplevel(parent)
    window.title("Continuo  |  Mission hit trace")
    window.geometry("1000x620")
    window.configure(bg=BG)
    tk.Label(window, text=f"Mission hit trace  |  {len(mission_events)} events",
             bg=BG, fg=ACCENT, font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=14, pady=(12, 4))
    tk.Label(window, text="Select an action or damage event to compare the sampled evidence with the mission decision.",
             bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=14)
    body = tk.Frame(window, bg=BG)
    body.pack(fill="both", expand=True, padx=14, pady=10)
    rows = tk.Listbox(body, bg=PANEL, fg=TEXT, selectbackground="#315778",
                      font=("Consolas", 9), exportselection=False)
    rows.pack(side="left", fill="both", expand=True)
    scrollbar = tk.Scrollbar(body, command=rows.yview)
    scrollbar.pack(side="left", fill="y")
    rows.configure(yscrollcommand=scrollbar.set)
    detail = tk.Text(window, height=12, bg=PANEL, fg=TEXT, insertbackground=TEXT,
                     font=("Consolas", 9), wrap="none")
    detail.pack(fill="x", padx=14, pady=(0, 14))
    detail.configure(state="disabled")
    for event in mission_events:
        values = event.get("values") or {}
        kind = event.get("kind", "")
        if kind == "mission_event":
            summary = (f"{values.get('kind', ''):16} {values.get('slot', ''):6} "
                       f"action {values.get('action_id', 0):>4}  "
                       f"{str(values.get('labels', ''))[:26]:26}  "
                       f"damage {values.get('damage', 0):>5}  HP {values.get('hp', 0):>6}")
        elif kind == "mission_decision":
            summary = (f"DECISION {values.get('kind', ''):7} step {values.get('step', 0):>2} "
                       f"{str(values.get('current', ''))[:26]:26}  "
                       f"pending {str(values.get('pending', False)):5}  "
                       f"window {values.get('pending_earliest_hit_ns', 0)}")
        else:
            summary = (f"RESULT   step {values.get('step', 0):>2} "
                       f"{str(values.get('current', ''))[:26]:26}  "
                       f"pending {str(values.get('pending', False)):5}  "
                       f"restart {values.get('restart', '')}")
        rows.insert("end", f"{float(event.get('time', 0)):12.3f}  {summary}")

    def selected(_event=None):
        selection = rows.curselection()
        if not selection:
            return
        event = mission_events[selection[0]]
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", json.dumps(event, indent=2))
        detail.configure(state="disabled")

    rows.bind("<<ListboxSelect>>", selected)
    if mission_events:
        rows.selection_set(0)
        selected()


def _show_window(root):
    global _WINDOW
    if _WINDOW is not None and _WINDOW.winfo_exists():
        _WINDOW.lift()
        _WINDOW.focus_force()
        return
    win = tk.Toplevel(root)
    _WINDOW = win
    win.title("Continuo  |  Settings")
    win.geometry("480x430")
    win.resizable(False, False)
    win.configure(bg=BG)
    tk.Label(win, text="Settings & Sessions", bg=BG, fg=TEXT,
             font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=18, pady=(17, 6))
    tk.Label(win, text="Settings archives include preferences and editable profiles.\n"
                       "Music files, dumps, logs and generated exports stay separate.",
             bg=BG, fg=MUTED, justify="left", font=("Segoe UI", 9)).pack(anchor="w", padx=18)
    row = tk.Frame(win, bg=BG)
    row.pack(anchor="w", padx=18, pady=(13, 0))
    default_dir = Path(user_data_path()).parent

    def export_click():
        name = f"TvCContinuo-settings-{datetime.now():%Y%m%d}.zip"
        selected = filedialog.asksaveasfilename(parent=win, initialdir=str(default_dir),
                                                 initialfile=name, defaultextension=".zip",
                                                 filetypes=[("Settings archive", "*.zip")])
        if not selected:
            return
        try:
            count = export_settings(Path(selected))
            messagebox.showinfo("Settings exported", f"Saved {count} setting files.", parent=win)
        except Exception as error:
            messagebox.showerror("Export failed", str(error), parent=win)

    def import_click():
        selected = filedialog.askopenfilename(parent=win, initialdir=str(default_dir),
                                               filetypes=[("Settings archive", "*.zip")])
        if not selected:
            return
        try:
            count = schedule_settings_import(Path(selected))
            messagebox.showinfo("Settings ready",
                                f"Validated {count} setting files. Restart Continuo to import them.", parent=win)
        except Exception as error:
            messagebox.showerror("Import failed", str(error), parent=win)

    _button(row, "Export settings", export_click).pack(side="left", padx=(0, 8))
    _button(row, "Import settings", import_click).pack(side="left")
    from tvcgui.features.overlay.customize_window import open_window as open_overlay_customizer
    _button(win, "Customize overlay", open_overlay_customizer).pack(anchor="w", padx=18, pady=(10, 0))
    from tvcgui.ui.help_window import open_help_window
    _button(win, "Help", open_help_window).pack(anchor="w", padx=18, pady=(6, 0))
    tk.Frame(win, height=1, bg="#2b3b4d").pack(fill="x", padx=18, pady=15)
    tk.Label(win, text="Recorded session", bg=BG, fg=TEXT,
             font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=18)
    tk.Label(win, text="Records HUD, music, mission actions, damage, and hit decisions. Replay is read-only.",
             bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(2, 8))
    status = tk.StringVar(value="")
    buttons = tk.Frame(win, bg=BG)
    buttons.pack(anchor="w", padx=18)

    def toggle_recording():
        active, _count = recorder.status()
        recorder.stop() if active else recorder.start()
        active, count = recorder.status()
        record_button.configure(text="Stop recording" if active else "Start recording")
        status.set(f"{'Recording' if active else 'Stopped'}  |  {count} frames, {len(recorder.events())} events")

    record_button = _button(buttons, "Start recording", toggle_recording)
    record_button.pack(side="left", padx=(0, 8))

    def save_click():
        selected = filedialog.asksaveasfilename(
            parent=win, initialdir=str(default_dir),
            initialfile=f"TvCContinuo-session-{datetime.now():%Y%m%d-%H%M%S}.json",
            defaultextension=".json", filetypes=[("Session recording", "*.json")])
        if not selected:
            return
        try:
            count = recorder.save(Path(selected))
            messagebox.showinfo("Session saved",
                                f"Saved {count} frames and {len(recorder.events())} events.",
                                parent=win)
        except Exception as error:
            messagebox.showerror("Save failed", str(error), parent=win)

    def replay_click():
        selected = filedialog.askopenfilename(parent=win, initialdir=str(default_dir),
                                               filetypes=[("Session recording", "*.json")])
        if not selected:
            return
        try:
            payload = load_session_payload(Path(selected))
            _show_replay(win, payload["frames"])
            _show_mission_events(win, payload["events"])
        except Exception as error:
            messagebox.showerror("Replay failed", str(error), parent=win)

    _button(buttons, "Save session", save_click).pack(side="left", padx=(0, 8))
    _button(buttons, "Open replay", replay_click).pack(side="left")
    tk.Label(win, textvariable=status, bg=BG, fg=ACCENT,
             font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(10, 0))

    def refresh():
        if not win.winfo_exists():
            return
        active, count = recorder.status()
        record_button.configure(text="Stop recording" if active else "Start recording")
        status.set(f"{'Recording' if active else 'Stopped'}  |  {count} frames, {len(recorder.events())} events")
        win.after(500, refresh)

    refresh()


def open_window():
    tk_call(_show_window)
