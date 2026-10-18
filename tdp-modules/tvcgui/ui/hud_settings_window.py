"""Focused HUD controls, built-in sets, and user presets."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from tvcgui.core.tk_host import tk_call
from tvcgui.features.overlay import customization
from tvcgui.ui import hud_settings


_WINDOW = None
_LABELS = (
    ("show_interaction_card", "Hit / Block"),
    ("show_combo_card", "Combo"),
    ("show_damage_badge", "Damage scaling"),
    ("show_untech_panel", "Hitstun"),
    ("show_meter_panel", "Meter gain / spend"),
    ("show_red_health_panel", "Red health values"),
    ("show_tag_card", "Tag"),
)
_VISIBLE_LABELS = (
    ("show_health", "Health bars / Baroque"),
    ("show_meter_rail", "Meter bars"),
    ("show_inputs", "Inputs"),
    ("show_move_timing", "Move timing"),
)


def _show(root, snapshot):
    global _WINDOW
    if _WINDOW is not None and _WINDOW.winfo_exists():
        _WINDOW.lift()
        _WINDOW.focus_force()
        return
    win = tk.Toplevel(root)
    _WINDOW = win
    win.title("Continuo | HUD Settings")
    win.geometry("490x640")
    win.configure(bg="#0a111c")
    fg, bg, panel = "#e8f0f8", "#0a111c", "#122138"
    tk.Label(win, text="HUD Settings", bg=bg, fg=fg,
             font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=18, pady=(16, 4))
    tk.Label(win, text="Changes save immediately. Overlay ON/OFF stays in the main window.",
             bg=bg, fg="#9daec2").pack(anchor="w", padx=18)

    vars_by_key = {key: tk.BooleanVar(value=bool(snapshot.get(key, False)))
                   for key in (*hud_settings.HUD_KEYS, *hud_settings.VISIBLE_KEYS)}
    set_label = tk.StringVar(value=str(snapshot.get("hud_info_set", "CUSTOM")).title())

    def current_hud():
        return {key: bool(var.get()) for key, var in vars_by_key.items()
                if key in hud_settings.HUD_KEYS}

    def current_visible():
        return {key: bool(vars_by_key[key].get()) for key in hud_settings.VISIBLE_KEYS}

    def changed():
        set_label.set("Custom")
        hud_settings.save_visible_settings({**current_hud(), **current_visible()})
        hud_settings.submit("hud", current_hud())

    tk.Label(win, text="Set", bg=bg, fg=fg, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=18, pady=(16, 3))
    set_row = tk.Frame(win, bg=bg)
    set_row.pack(anchor="w", padx=18)
    for name in ("Core", "Research", "Full"):
        def apply_builtin(name=name):
            set_label.set(name)
            hud_settings.submit("builtin", name.upper())
            values = {
                "Core": (False, False, True, True, True, True, False),
                "Research": (False, False, True, True, True, True, False),
                "Full": (True, True, True, True, True, True, True),
            }[name]
            for key, value in zip(hud_settings.HUD_KEYS, values):
                vars_by_key[key].set(value)
            for key in hud_settings.VISIBLE_KEYS:
                vars_by_key[key].set(True)
            hud_settings.save_visible_settings({**current_hud(), **current_visible()})
        tk.Button(set_row, text=name, command=apply_builtin, bg=panel, fg=fg,
                  relief="flat", padx=12).pack(side="left", padx=(0, 7))
    tk.Label(set_row, textvariable=set_label, bg=bg, fg="#35aadc").pack(side="left", padx=8)

    tk.Label(win, text="Visible HUD information", bg=bg, fg=fg,
             font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=18, pady=(18, 5))
    checks = tk.Frame(win, bg=bg)
    checks.pack(fill="x", padx=18)
    for i, (key, label) in enumerate(_LABELS):
        tk.Checkbutton(checks, text=label, variable=vars_by_key[key], command=changed,
                       bg=bg, fg=fg, selectcolor=panel, activebackground=bg,
                       activeforeground=fg).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 30), pady=2)

    tk.Label(win, text="Main overlay", bg=bg, fg=fg,
             font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=18, pady=(13, 5))
    main_checks = tk.Frame(win, bg=bg)
    main_checks.pack(fill="x", padx=18)
    for i, (key, label) in enumerate(_VISIBLE_LABELS):
        tk.Checkbutton(main_checks, text=label, variable=vars_by_key[key], command=changed,
                       bg=bg, fg=fg, selectcolor=panel, activebackground=bg,
                       activeforeground=fg).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 30), pady=2)

    tools = tk.Frame(win, bg=bg)
    tools.pack(anchor="w", padx=18, pady=(12, 4))
    from tvcgui.features.overlay.customize_window import open_window as customize
    tk.Button(tools, text="Appearance and positions", command=customize,
              bg=panel, fg=fg, relief="flat", padx=10).pack(side="left", padx=(0, 8))
    def clear():
        for var in vars_by_key.values():
            var.set(False)
        changed()
    tk.Button(tools, text="Clear cards", command=clear,
              bg=panel, fg=fg, relief="flat", padx=10).pack(side="left")
    tk.Button(tools, text="Attack Research", command=lambda: hud_settings.submit("research", None),
              bg=panel, fg=fg, relief="flat", padx=10).pack(side="left", padx=(8, 0))

    tk.Label(win, text="Saved presets", bg=bg, fg=fg,
             font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=18, pady=(18, 4))
    tk.Label(win, text="Presets include these switches, colors, opacity, and panel positions.",
             bg=bg, fg="#9daec2").pack(anchor="w", padx=18)
    selected = tk.StringVar()
    chooser = ttk.Combobox(win, textvariable=selected, state="readonly", width=42)
    chooser.pack(anchor="w", padx=18, pady=(8, 5))
    def refresh():
        chooser["values"] = sorted(hud_settings.load_presets(), key=str.casefold)
    refresh()

    def save():
        name = simpledialog.askstring("Save HUD preset", "Preset name:", parent=win)
        if name is None:
            return
        try:
            name = hud_settings.save_preset(name, {**current_hud(), **current_visible()}, customization.load())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Preset not saved", str(exc), parent=win)
            return
        refresh()
        selected.set(name)

    def load():
        preset = hud_settings.load_presets().get(selected.get())
        if preset is None:
            return
        for key, value in preset["hud"].items():
            vars_by_key[key].set(value)
        try:
            customization.save(preset["overlay"])
        except OSError as exc:
            messagebox.showerror("Preset not loaded", str(exc), parent=win)
            return
        set_label.set(selected.get())
        hud_settings.save_visible_settings({**current_hud(), **current_visible()})
        hud_settings.submit("hud", current_hud())

    def delete():
        if not selected.get():
            return
        if not messagebox.askyesno("Delete preset", f"Delete '{selected.get()}'?", parent=win):
            return
        hud_settings.delete_preset(selected.get())
        selected.set("")
        refresh()

    row = tk.Frame(win, bg=bg)
    row.pack(anchor="w", padx=18)
    for label, callback in (("Save current", save), ("Load", load), ("Delete", delete)):
        tk.Button(row, text=label, command=callback, bg=panel, fg=fg,
                  relief="flat", padx=10).pack(side="left", padx=(0, 8))


def open_window(snapshot):
    tk_call(lambda root: _show(root, dict(snapshot)))
