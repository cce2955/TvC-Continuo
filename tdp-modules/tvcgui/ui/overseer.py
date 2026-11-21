from __future__ import annotations

import json
import time
import traceback
import tkinter as tk
from tkinter import messagebox
from typing import Any, Callable

try:
    from tvcgui.core.tk_host import tk_call
except Exception:  # pragma: no cover
    tk_call = None

_OPEN_WINDOW: tk.Toplevel | None = None

_BG = "#0d1018"
_PANEL = "#171b27"
_BORDER = "#31384d"
_TEXT = "#e9edf7"
_MUTED = "#aeb8cc"
_ACCENT = "#89a7ff"
_ACTIVE = "#2a3553"
_DANGER = "#4a2230"


def _fmt_addr(value: Any) -> str:
    try:
        v = int(value or 0)
    except Exception:
        v = 0
    return "-" if not v else f"0x{v:08X}"


def _yes(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _age(value: Any) -> str:
    try:
        f = float(value)
    except Exception:
        return "-"
    if f < 0:
        return "-"
    return f"{f:.2f}s"


def _button(parent: tk.Misc, text: str, command: Callable[[], Any], *, danger: bool = False) -> tk.Button:
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=_DANGER if danger else _ACTIVE,
        fg=_TEXT,
        activebackground="#5c2b3c" if danger else "#35466c",
        activeforeground=_TEXT,
        relief="raised",
        bd=1,
        padx=10,
        pady=5,
        highlightthickness=1,
        highlightbackground=_BORDER,
        cursor="hand2",
    )




def _set_tip(var: tk.StringVar | None, text: str) -> None:
    if var is None:
        return
    try:
        var.set(text)
    except Exception:
        pass


def _tip_button(
    parent: tk.Misc,
    text: str,
    command: Callable[[], Any],
    tip_var: tk.StringVar,
    tip_text: str,
    *,
    danger: bool = False,
) -> tk.Button:
    btn = _button(parent, text, command, danger=danger)
    btn.bind("<Enter>", lambda _e: _set_tip(tip_var, tip_text), add="+")
    btn.bind("<Leave>", lambda _e: _set_tip(tip_var, DEFAULT_TIP_TEXT), add="+")
    return btn


DEFAULT_TIP_TEXT = (
    "Hover a button for details. Refresh and Dump only read/report. "
    "Safe Restore releases runtime holds. Hard Reset clears cached runtime state."
)

def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.1f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(_format_value(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, default=str, sort_keys=True)
    return str(value)


def _format_state_text(state: dict[str, Any]) -> str:
    """Render the live runtime snapshot as a plain-text dashboard."""
    if not isinstance(state, dict):
        state = {"error": f"state_provider returned {type(state).__name__}"}

    lines: list[str] = []
    lines.append("TOOL STATE PANEL")
    lines.append("Live status for safety features, HUD holds, assists, and performance.")
    lines.append(f"Last update: {time.strftime('%H:%M:%S')}")
    lines.append(f"Dolphin hooked according to main: {_yes(state.get('hooked'))}")
    dolphin = state.get("dolphin") or {}
    candidates = dolphin.get("candidates") or []
    if dolphin.get("mem2_latched"):
        lines.append(f"MEM2 process: PID {dolphin.get('mem2_pid')}  {dolphin.get('mem2_path') or '(path unavailable)'}")
    else:
        lines.append(f"MEM2 process: waiting ({dolphin.get('mem2_note') or 'no live map'})")
    if len(candidates) > 1:
        lines.append("Multiple Dolphin instances: DME's MEM1 process cannot be identified here.")
    for candidate in candidates:
        lines.append(f"  Dolphin PID {candidate.get('pid')}: {candidate.get('path') or '(path unavailable)'}")
    lines.append(f"Overlay: {'ON' if state.get('overlay_enabled') else 'OFF'}")
    lines.append(f"KO Control: {state.get('ko_control') or 'OFF'}")
    lines.append("")

    recent_events = state.get("recent_events") or []
    lines.append("RECENT COMBAT EVENTS")
    if recent_events:
        for event in recent_events:
            lines.append(
                f"  #{event.get('seq')} {event.get('kind')} {event.get('slot')} "
                f"action={event.get('action')} damage={event.get('damage')} combo={event.get('combo')}"
            )
    else:
        lines.append("  No combat events yet.")
    lines.append("")

    mega = state.get("megacrash") or {}
    lines.append("MEGACRASH")
    lines.append(f"  enabled: {_yes(mega.get('enabled'))}")
    lines.append(f"  mode: {mega.get('mode', '-')}")
    lines.append(f"  chance: {mega.get('chance', 0)}%")
    lines.append(f"  pending pulses: {mega.get('pulse_count', 0)}")
    lines.append(f"  scheduled triggers: {mega.get('scheduled_count', 0)}")
    lines.append("")

    hud = state.get("hud_editor") or {}
    lines.append("HUD HELD WINS")
    lines.append(f"  force 0 WINS text: {_yes(hud.get('force_zero_as_win'))}")
    lines.append(f"  use HUD bank: {_yes(hud.get('use_hud', True))}")
    lines.append(f"  use SVM bank: {_yes(hud.get('use_svm'))}")
    holds = hud.get("holds") or {}
    for player in ("P1", "P2"):
        h = holds.get(player) or {}
        lines.append(
            f"  {player}: hold={_yes(h.get('enabled'))} "
            f"value={h.get('value', 0)} auto={_yes(h.get('auto', True))} "
            f"last_raw={h.get('last_raw', '-')}"
        )
    if hud.get("status"):
        lines.append(f"  status: {hud.get('status')}")
    if hud.get("error"):
        lines.append(f"  error: {hud.get('error')}")
    lines.append("")

    assist = state.get("assist") or {}
    snaps = state.get("slots") or {}
    slot_profiles = assist.get("slot_profiles") or {}
    active_quick = state.get("active_quick_assist_by_slot") or {}

    lines.append("ASSIST RUNTIME")
    if assist.get("error"):
        lines.append(f"  error: {assist.get('error')}")
    lines.append(f"  cache epoch: {assist.get('cache_epoch', '-')}")
    lines.append(f"  route cache count: {assist.get('route_cache_count', 0)}")
    lines.append(f"  route inflight count: {assist.get('route_inflight_count', 0)}")
    lines.append(f"  selected slot count: {assist.get('selected_slot_count', '-')}")
    lines.append("")

    for slot in ("P1-C1", "P1-C2", "P2-C1", "P2-C2"):
        snap = snaps.get(slot) or {}
        prof = slot_profiles.get(slot) or {}
        quick = active_quick.get(slot) or {}
        char = snap.get("name") or snap.get("char_name") or "-"
        cid = snap.get("char_id", snap.get("id", "-"))
        base = snap.get("base", 0)
        move = snap.get("move") or snap.get("move_label") or "-"

        selected_label = prof.get("label") or quick.get("label") or "none"
        word_raw = prof.get("word", quick.get("word"))
        if word_raw is None:
            word = "default" if (prof.get("selected") or quick) else "-"
        else:
            try:
                word = f"0x{int(word_raw):08X}"
            except Exception:
                word = str(word_raw)

        lines.append(f"  {slot}: {char} cid={cid} fighter={_fmt_addr(base)} move={move}")
        lines.append(f"    selected: {selected_label}")
        lines.append(
            f"    word: {word}; route={prof.get('route_type', '-')} "
            f"block={_fmt_addr(prof.get('route_block'))}"
        )
        lines.append(
            f"    assist window: {_yes(prof.get('in_assist_window'))}; "
            f"state={prof.get('state_id', '-')} last_write_age={_age(prof.get('last_write_age_sec'))}"
        )
    lines.append("")

    rpm = state.get("runtime_patch_manager") or {}
    lines.append("RUNTIME PATCH MANAGER")
    if rpm:
        lines.append(f"  writes ok/fail: {rpm.get('write_ok', 0)}/{rpm.get('write_fail', 0)}")
        lines.append(f"  skipped dirty/cache: {rpm.get('skip_dirty', 0)}/{rpm.get('skip_cache', 0)}")
        lines.append(f"  cache entries: {rpm.get('cache_entries', 0)}")
        lines.append(
            f"  last: key={rpm.get('last_write_key', '-')} "
            f"addr={_fmt_addr(rpm.get('last_write_addr'))} "
            f"size={rpm.get('last_write_size', 0)} "
            f"ms={_format_value(rpm.get('last_write_ms', 0.0))} "
            f"age={_age(rpm.get('last_write_age_sec'))}"
        )
        if rpm.get("last_error"):
            lines.append(f"  error: {rpm.get('last_error')}")
    else:
        lines.append("  no patch-manager state reported yet")
    lines.append("")

    perf = state.get("perf") or {}
    lines.append("PERFORMANCE")
    if perf:
        for key in sorted(perf.keys()):
            lines.append(f"  {key}: {_format_value(perf.get(key))} ms")
    else:
        lines.append("  no perf bucket has reported yet")
    lines.append("")

    other_keys = sorted(k for k in state.keys() if k not in {
        "hooked", "dolphin", "recent_events", "overlay_enabled", "ko_control",
        "megacrash", "hud_editor", "assist", "slots", "perf", "active_quick_assist_by_slot", "runtime_patch_manager",
    })
    if other_keys:
        lines.append("OTHER STATE")
        for key in other_keys:
            lines.append(f"  {key}: {_format_value(state.get(key))}")
        lines.append("")

    return "\n".join(lines)


def _initial_dashboard_text() -> str:
    return (
        "TOOL STATE PANEL\n"
        "This panel is alive. It will immediately request a runtime snapshot from main.py.\n\n"
        "If this text remains after pressing Refresh, the runtime snapshot callback is not being called.\n"
        "That would be a Tool State wiring bug, not a Dolphin/game-state issue.\n"
    )


def open_overseer_window(
    state_provider: Callable[[], dict[str, Any]],
    safe_restore_cb: Callable[[], Any],
    hard_reset_cb: Callable[[], Any],
    dump_cb: Callable[[], Any] | None = None,
) -> None:
    def _show(master: tk.Tk) -> None:
        global _OPEN_WINDOW
        try:
            if _OPEN_WINDOW is not None and _OPEN_WINDOW.winfo_exists():
                _OPEN_WINDOW.lift()
                _OPEN_WINDOW.focus_force()
                return
        except Exception:
            _OPEN_WINDOW = None

        win = tk.Toplevel(master)
        _OPEN_WINDOW = win
        win.title("Tool State - Runtime Controls")
        win.geometry("1040x800")
        win.minsize(900, 620)
        win.configure(bg=_BG)

        header = tk.Frame(win, bg=_BG, padx=12)
        header.pack(side="top", fill="x", pady=(10, 6))
        tk.Label(header, text="Tool State", bg=_BG, fg=_TEXT, font=("Segoe UI", 14, "bold")).pack(side="left")
        tk.Label(
            header,
            text="live status, quick safety controls",
            bg=_BG,
            fg=_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(12, 0))
        connection_badge = tk.Label(header, text="DOLPHIN WAITING", bg=_PANEL, fg=_MUTED,
                                    font=("Segoe UI", 9, "bold"), padx=9, pady=4)
        connection_badge.pack(side="right")
        ko_badge = tk.Label(header, text="KO OFF", bg=_PANEL, fg=_MUTED,
                            font=("Segoe UI", 9, "bold"), padx=9, pady=4)
        ko_badge.pack(side="right", padx=(0, 8))

        controls_shell = tk.Frame(win, bg=_BORDER, padx=1, pady=1)
        controls_shell.pack(side="top", fill="x", padx=12, pady=(0, 8))
        controls = tk.Frame(controls_shell, bg=_PANEL, padx=10, pady=8)
        controls.pack(side="top", fill="x")

        action_row_1 = tk.Frame(controls, bg=_PANEL)
        action_row_1.pack(side="top", fill="x")
        action_row_2 = tk.Frame(controls, bg=_PANEL)
        action_row_2.pack(side="top", fill="x", pady=(6, 0))

        status_var = tk.StringVar(value="Window opened. Runtime snapshot not requested yet.")
        tip_var = tk.StringVar(value=DEFAULT_TIP_TEXT)
        dashboard_var = tk.StringVar(value=_initial_dashboard_text())

        # Use a scrollable canvas+label body. A plain Label rendered reliably on the
        # current Windows/Tk setup, so keep the Label for content but place it
        # inside a Canvas with a real vertical scrollbar.
        body_shell = tk.Frame(win, bg=_BORDER, padx=1, pady=1)
        body_shell.pack(side="top", fill="both", expand=True, padx=12, pady=(0, 8))

        body_container = tk.Frame(body_shell, bg=_PANEL)
        body_container.pack(side="top", fill="both", expand=True)

        body_canvas = tk.Canvas(body_container, bg=_PANEL, highlightthickness=0, bd=0)
        body_scroll = tk.Scrollbar(body_container, orient="vertical", command=body_canvas.yview)
        body_canvas.configure(yscrollcommand=body_scroll.set)
        body_scroll.pack(side="right", fill="y")
        body_canvas.pack(side="left", fill="both", expand=True)

        body_inner = tk.Frame(body_canvas, bg=_PANEL)
        body_window_id = body_canvas.create_window((0, 0), window=body_inner, anchor="nw")

        body = tk.Label(
            body_inner,
            textvariable=dashboard_var,
            bg=_PANEL,
            fg=_TEXT,
            anchor="nw",
            justify="left",
            padx=12,
            pady=12,
            font=("Consolas", 10),
        )
        body.pack(side="top", fill="both", expand=True, anchor="nw")

        def _sync_body_width(_e=None):
            try:
                body_canvas.itemconfigure(body_window_id, width=body_canvas.winfo_width())
            except Exception:
                pass

        def _sync_scrollregion(_e=None):
            try:
                body_canvas.configure(scrollregion=body_canvas.bbox("all"))
            except Exception:
                pass

        body_inner.bind("<Configure>", _sync_scrollregion, add="+")
        body_canvas.bind("<Configure>", _sync_body_width, add="+")

        def _on_mousewheel(event):
            try:
                delta = getattr(event, "delta", 0)
                if delta:
                    body_canvas.yview_scroll(int(-1 * (delta / 120)), "units")
            except Exception:
                pass

        def _bind_mousewheel(_event=None):
            try:
                body_canvas.bind_all("<MouseWheel>", _on_mousewheel, add="+")
            except Exception:
                pass

        def _unbind_mousewheel(_event=None):
            pass

        body_canvas.bind("<Enter>", _bind_mousewheel, add="+")
        body_canvas.bind("<Leave>", _unbind_mousewheel, add="+")

        footer = tk.Frame(win, bg=_BG, padx=12)
        footer.pack(side="bottom", fill="x", pady=(0, 10))

        tip_box = tk.Label(
            footer,
            textvariable=tip_var,
            bg=_PANEL,
            fg=_TEXT,
            anchor="w",
            justify="left",
            padx=10,
            pady=7,
            relief="ridge",
            bd=1,
            wraplength=980,
        )
        tip_box.pack(side="top", fill="x", pady=(0, 6))
        tk.Label(footer, textvariable=status_var, bg=_BG, fg=_MUTED, anchor="w", justify="left").pack(fill="x")

        def refresh() -> None:
            status_var.set("Refreshing runtime snapshot from main.py...")
            win.update_idletasks()
            try:
                state = state_provider() or {}
                rendered = _format_state_text(state)
                dashboard_var.set(rendered)
                connection_badge.configure(
                    text="DOLPHIN HOOKED" if state.get("hooked") else "DOLPHIN WAITING",
                    bg="#1d4438" if state.get("hooked") else _PANEL,
                    fg="#b8f1d4" if state.get("hooked") else _MUTED,
                )
                ko_mode = str(state.get("ko_control") or "OFF").upper()
                ko_badge.configure(
                    text=f"KO {ko_mode}",
                    bg="#1d4438" if ko_mode == "ACTIVE" else "#514228" if ko_mode == "ARMED" else _PANEL,
                    fg="#b8f1d4" if ko_mode == "ACTIVE" else "#ffe0a0" if ko_mode == "ARMED" else _MUTED,
                )
                status_var.set(f"Live runtime state refreshed at {time.strftime('%H:%M:%S')}.")
            except Exception as e:
                err = traceback.format_exc()
                dashboard_var.set(
                    "TOOL STATE REFRESH FAILED\n\n"
                    "The window is visible, but the runtime snapshot callback raised an error.\n"
                    "This is a Tool State wiring/runtime-state bug, not Dolphin being unhooked.\n\n"
                    f"Error: {e!r}\n\n{err}"
                )
                status_var.set(f"Refresh failed: {e!r}")

        def copy_status() -> None:
            try:
                report = _format_state_text(state_provider() or {})
                win.clipboard_clear()
                win.clipboard_append(report)
                win.update_idletasks()
                status_var.set("Current status and recent combat events copied.")
            except Exception as e:
                status_var.set(f"Copy failed: {e!r}")

        def safe_restore() -> None:
            try:
                result = safe_restore_cb()
                status_var.set(f"Safe restore complete: {result}")
            except Exception as e:
                status_var.set(f"Safe restore failed: {e!r}")
                messagebox.showerror("Safe restore failed", str(e), parent=win)
            refresh()

        def hard_reset() -> None:
            if not messagebox.askyesno(
                "Hard reset runtime state",
                "Clear assist selections, route caches, HUD holds, Megacrash pulses, and local runtime latches?",
                parent=win,
            ):
                return
            try:
                result = hard_reset_cb()
                status_var.set(f"Hard reset complete: {result}")
            except Exception as e:
                status_var.set(f"Hard reset failed: {e!r}")
                messagebox.showerror("Hard reset failed", str(e), parent=win)
            refresh()

        def dump_state() -> None:
            if dump_cb is None:
                status_var.set("No dump callback is registered.")
                return
            try:
                result = dump_cb()
                status_var.set(f"Dumped state: {result}")
            except Exception as e:
                status_var.set(f"Dump failed: {e!r}")

        tk.Label(action_row_1, text="Status", bg=_PANEL, fg=_MUTED, width=10, anchor="w").pack(side="left")
        _tip_button(
            action_row_1,
            "Refresh",
            refresh,
            tip_var,
            "Refresh reads the current tool state and redraws this panel. It does not write to game memory.",
        ).pack(side="left", padx=(0, 8))
        _tip_button(
            action_row_1,
            "Copy Status",
            copy_status,
            tip_var,
            "Copy the current status and recent combat events as text.",
        ).pack(side="left", padx=(0, 8))
        _tip_button(
            action_row_1,
            "Dump State",
            dump_state,
            tip_var,
            "Dump State saves the current dashboard snapshot to debug_dumps so a broken setup can be inspected later.",
        ).pack(side="left", padx=(0, 8))

        tk.Label(action_row_2, text="Recovery", bg=_PANEL, fg=_MUTED, width=10, anchor="w").pack(side="left")
        _tip_button(
            action_row_2,
            "Safe Restore",
            safe_restore,
            tip_var,
            "Safe Restore turns off risky runtime holds: Megacrash pulses, HUD held wins, and quick-assist runtime selections. It is the normal panic button.",
        ).pack(side="left", padx=(0, 8))
        _tip_button(
            action_row_2,
            "Hard Reset",
            hard_reset,
            tip_var,
            "Hard Reset does Safe Restore, then also clears route caches and local runtime latches. Use it when stale state keeps coming back.",
            danger=True,
        ).pack(side="left", padx=(0, 8))

        def periodic() -> None:
            try:
                if not win.winfo_exists():
                    return
                refresh()
                win.after(2000, periodic)
            except Exception:
                # If refresh itself fails, it writes the error into the dashboard.
                # This catch is just to keep Tk's after callback alive.
                try:
                    win.after(2000, periodic)
                except Exception:
                    pass

        def on_close() -> None:
            global _OPEN_WINDOW
            _OPEN_WINDOW = None
            try:
                win.destroy()
            except Exception:
                pass

        win.protocol("WM_DELETE_WINDOW", on_close)

        # Do one direct refresh now. The previous builds showed placeholder text
        # forever on some setups because delayed after/idle refreshes did not run
        # before the window is opened. This makes the behavior explicit.
        refresh()
        win.after(2000, periodic)

    if tk_call is not None:
        tk_call(_show)
    else:
        root = tk.Tk()
        root.withdraw()
        _show(root)
        root.mainloop()
