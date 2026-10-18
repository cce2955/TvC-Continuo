"""Visual editor for the two compact in-game overlay panels."""
from __future__ import annotations

import json
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser

from tvcgui.core.paths import user_data_path
from tvcgui.core.tk_host import tk_call
from tvcgui.features.overlay import customization


_WINDOW = None
_LABELS = (
    ("show_health", "Health and Baroque"),
    ("show_stun", "Hitstun / untech"),
    ("show_inputs", "Inputs"),
    ("show_move_timing", "Move timing"),
    ("show_meter_rail", "Meter rail"),
)


def _damage_visible(data):
    override = data.get("show_damage_scaling")
    if override is not None:
        return bool(override)
    try:
        control = json.loads(Path(user_data_path("overlay", "master_overlay_control.json")).read_text(encoding="utf-8"))
        return str(control.get("hud_info_set", "CORE")).upper() == "CORE" or bool(control.get("show_damage_badge", False))
    except (OSError, ValueError, TypeError):
        return True


def _show(root):
    global _WINDOW
    if _WINDOW is not None and _WINDOW.winfo_exists():
        _WINDOW.lift()
        return
    data = customization.load()
    win = tk.Toplevel(root)
    _WINDOW = win
    win.title("Continuo | Overlay Customizer")
    win.geometry("550x610")
    win.configure(bg="#0a111c")
    fg, bg, panel = "#e8f0f8", "#0a111c", "#122138"
    tk.Label(win, text="Overlay Customizer", bg=bg, fg=fg,
             font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=18, pady=(15, 4))
    tk.Label(win, text="Changes save immediately. Use Edit positions to drag P1 and P2 on the game screen.",
             bg=bg, fg="#9daec2", font=("Segoe UI", 9)).pack(anchor="w", padx=18)
    canvas = tk.Canvas(win, width=510, height=245, bg="#172333", highlightthickness=1,
                       highlightbackground="#34445c")
    canvas.pack(padx=18, pady=(14, 10))
    checks = {}

    def save():
        data["positions"] = customization.load()["positions"]
        for key, var in checks.items():
            data[key] = bool(var.get())
        customization.save(data)
        draw_preview()

    def draw_preview():
        canvas.delete("all")
        for team in ("P1", "P2"):
            x = 10 if team == "P1" else 275
            color = data["p1_color" if team == "P1" else "p2_color"]
            canvas.create_rectangle(x, 12, x + 225, 232, fill=panel, outline=color, width=2)
            canvas.create_rectangle(x + 8, 22, x + 31, 39, fill=color, outline="")
            canvas.create_text(x + 40, 30, text=f"{team}  FIGHTER", fill=fg, anchor="w",
                               font=("Segoe UI", 10, "bold"))
            y = 53
            for key, label in _LABELS:
                if data[key]:
                    canvas.create_rectangle(x + 8, y, x + 217, y + 24, fill="#203047", outline="")
                    canvas.create_text(x + 15, y + 12, text=label, fill=fg, anchor="w",
                                       font=("Segoe UI", 9))
                    y += 30
            if _damage_visible(data):
                canvas.create_text(x + 15, min(y + 8, 218), text="Damage scaling", fill=color,
                                   anchor="w", font=("Segoe UI", 8))

    controls = tk.Frame(win, bg=bg)
    controls.pack(fill="x", padx=18)
    for index, (key, label) in enumerate(_LABELS):
        var = tk.BooleanVar(value=data[key])
        checks[key] = var
        tk.Checkbutton(controls, text=label, variable=var, command=save, bg=bg, fg=fg,
                       selectcolor=panel, activebackground=bg, activeforeground=fg,
                       font=("Segoe UI", 9)).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 35))

    damage_row = tk.Frame(win, bg=bg)
    damage_row.pack(anchor="w", padx=18)
    damage_var = tk.BooleanVar(value=_damage_visible(data))
    def change_damage():
        data["show_damage_scaling"] = bool(damage_var.get())
        save()
    tk.Checkbutton(damage_row, text="Damage scaling", variable=damage_var, command=change_damage,
                   bg=bg, fg=fg, selectcolor=panel, activebackground=bg,
                   activeforeground=fg).pack(side="left")
    def follow_dock():
        data["show_damage_scaling"] = None
        damage_var.set(_damage_visible(data))
        save()
    tk.Button(damage_row, text="Use dock setting", command=follow_dock,
              bg=panel, fg=fg, relief="flat", padx=8).pack(side="left", padx=16)

    edit_var = tk.BooleanVar(value=data["edit_mode"])
    def toggle_edit():
        data["edit_mode"] = bool(edit_var.get())
        save()
    tk.Checkbutton(win, text="Edit positions: drag P1 and P2 on the game screen", variable=edit_var,
                   command=toggle_edit, bg=bg, fg="#35aadc", selectcolor=panel,
                   activebackground=bg, activeforeground=fg).pack(anchor="w", padx=18, pady=(7, 4))

    row = tk.Frame(win, bg=bg)
    row.pack(anchor="w", padx=18, pady=(4, 0))
    for team in ("P1", "P2"):
        key = "p1_color" if team == "P1" else "p2_color"
        def choose(key=key):
            _rgb, color = colorchooser.askcolor(color=data[key], parent=win,
                                                 title=f"{key[:2].upper()} accent color")
            if color:
                data[key] = color
                save()
        tk.Button(row, text=f"{team} color", command=choose, bg=panel, fg=fg,
                  relief="flat", padx=12, pady=6).pack(side="left", padx=(0, 8))
    def reset_positions():
        data["positions"] = {}
        customization.save(data)
        draw_preview()
    tk.Button(row, text="Reset positions", command=reset_positions, bg=panel, fg=fg,
              relief="flat", padx=12, pady=6).pack(side="left")
    opacity = tk.DoubleVar(value=round(data["opacity"] * 100))
    def change_opacity(_value):
        data["opacity"] = float(opacity.get()) / 100.0
        save()
    tk.Scale(win, from_=25, to=100, orient="horizontal", variable=opacity,
             command=change_opacity, label="Panel opacity (%)", bg=bg, fg=fg,
             troughcolor=panel, highlightthickness=0, length=260).pack(anchor="w", padx=18, pady=(4, 0))
    tk.Label(win, text="Damage scaling and Inputs can be shown independently.",
             bg=bg, fg="#9daec2", font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(12, 0))
    draw_preview()


def open_window():
    tk_call(_show)
