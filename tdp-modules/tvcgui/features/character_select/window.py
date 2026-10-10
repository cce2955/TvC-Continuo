from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from . import runtime

try:
    from tvcgui.core.tk_host import tk_call
except Exception:  # pragma: no cover
    tk_call = None

_WIN: tk.Toplevel | None = None
_BG = "#151821"
_CARD = "#1f2430"
_TEXT = "#f2f5ff"
_MUTED = "#aeb6c8"
_ACCENT = "#7788ff"


def _label(parent: tk.Misc, text: str, *, bold: bool = False, muted: bool = False) -> tk.Label:
    return tk.Label(
        parent,
        text=text,
        bg=_CARD,
        fg=(_MUTED if muted else _TEXT),
        font=("Segoe UI", 10, "bold" if bold else "normal"),
        anchor="w",
        justify="left",
        wraplength=820,
    )


def _button(parent: tk.Misc, text: str, command: Any) -> tk.Button:
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg="#2b3142",
        fg=_TEXT,
        activebackground="#394058",
        activeforeground=_TEXT,
        relief="flat",
        padx=10,
        pady=8,
        font=("Segoe UI", 10, "bold"),
    )


def _card(parent: tk.Misc) -> tk.Frame:
    return tk.Frame(parent, bg=_CARD, bd=0, highlightthickness=1, highlightbackground="#2c3345")


def _make_scroll_frame(win: tk.Toplevel) -> tk.Frame:
    outer = tk.Frame(win, bg=_BG)
    outer.pack(fill="both", expand=True)

    canvas = tk.Canvas(outer, bg=_BG, highlightthickness=0)
    yscroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=yscroll.set)

    yscroll.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    content = tk.Frame(canvas, bg=_BG)
    window_id = canvas.create_window((0, 0), window=content, anchor="nw")

    def _update_scrollregion(_event: tk.Event | None = None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _fit_width(event: tk.Event) -> None:
        canvas.itemconfigure(window_id, width=event.width)

    def _mousewheel(event: tk.Event) -> None:
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = int(-1 * (event.delta / 120))
        canvas.yview_scroll(delta, "units")

    content.bind("<Configure>", _update_scrollregion)
    canvas.bind("<Configure>", _fit_width)
    canvas.bind_all("<MouseWheel>", _mousewheel)
    canvas.bind_all("<Button-4>", _mousewheel)
    canvas.bind_all("<Button-5>", _mousewheel)
    return content


def _get_clone_choices() -> list[str]:
    try:
        state = runtime.get_roster_patch_state()
        clone_slots = list(state.get("clone_slots") or [])
    except Exception:
        clone_slots = []
    if not clone_slots:
        clone_slots = [
            "Yami 1 clone slot 0x1B (ID 0x17)",
            "Yami 2 clone slot 0x1C (ID 0x18)",
            "Yami 3 clone slot 0x1D (ID 0x19)",
        ]
    return clone_slots


def _format_snapshot(snapshot: dict[str, Any]) -> str:
    if not snapshot:
        return "No selector snapshot yet."
    lines: list[str] = []
    lines.append(f"hover index: {snapshot.get('hover_index', '')}")
    for item in snapshot.get("fields", []) or []:
        lines.append(f"{item.get('addr', '')}  {item.get('label', '')}: {item.get('value', '')}  {item.get('display', '')}")
    lines.append("")
    lines.append("near Yami rows:")
    for item in snapshot.get("table", []) or []:
        slot = str(item.get("slot", ""))
        if slot in {"0x1A", "0x1B", "0x1C", "0x1D"}:
            lines.append(f"{slot}  {item.get('addr', '')}  {item.get('char_label', '')}")
    return "\n".join(lines)


def _format_visual_table(snapshot: dict[str, Any]) -> str:
    if not snapshot:
        return "No visual table snapshot yet."
    lines: list[str] = []
    lines.append(f"visual index table: {snapshot.get('index_table_addr', '')}")
    lines.append("index values:")
    lines.append(" ".join(snapshot.get("index_table") or []))
    lines.append("")
    lines.append(f"char ID table: {snapshot.get('char_table_addr', '')}")
    lines.append("char values:")
    lines.append(" ".join(snapshot.get("char_table") or []))
    lines.append("")
    lines.append(f"append index addr: {snapshot.get('append_index_addr', '')}")
    lines.append(f"append char addr: {snapshot.get('append_char_addr', '')}")
    return "\n".join(lines)


def _parse_slot(text: str) -> int:
    import re
    m = re.search(r"slot\s+0x([0-9A-Fa-f]+)", text or "")
    if m:
        return int(m.group(1), 16)
    return 0x1B


def _show_char_test_window(master: tk.Misc | None = None) -> None:
    global _WIN
    if _WIN is not None:
        try:
            if _WIN.winfo_exists():
                _WIN.deiconify()
                _WIN.lift()
                return
        except Exception:
            _WIN = None

    win = tk.Toplevel(master) if master is not None else tk.Toplevel()
    _WIN = win
    win.title("Character Select - Visual Overrides")
    win.geometry("1040x820")
    win.configure(bg=_BG)
    win.minsize(820, 600)

    def _on_close() -> None:
        global _WIN
        _WIN = None
        try:
            win.destroy()
        except Exception:
            pass

    win.protocol("WM_DELETE_WINDOW", _on_close)

    root = _make_scroll_frame(win)
    body = tk.Frame(root, bg=_BG)
    body.pack(fill="both", expand=True, padx=12, pady=12)

    header = tk.Frame(body, bg=_BG)
    header.pack(fill="x", pady=(0, 10))
    tk.Label(header, text="Character Select", bg=_BG, fg=_TEXT, font=("Segoe UI", 15, "bold")).pack(side="left")
    tk.Label(header, text="Visual override lab", bg=_BG, fg=_ACCENT, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(10, 0))

    status_var = tk.StringVar(value="Ready.")
    clone_choices = _get_clone_choices()
    clone_slot_var = tk.StringVar(value=clone_choices[0])

    main_card = _card(body)
    main_card.pack(fill="x", pady=(0, 10))
    _label(main_card, "Goal", bold=True).pack(fill="x", padx=12, pady=(12, 4))
    _label(
        main_card,
        "Yami 1/2/3 keep their real fighter IDs. The controls below force presentation only: large face/card/background donor, cursor/wheel icon donor, and the readable custom name label. Ryu icon is the default for all three.",
        muted=True,
    ).pack(fill="x", padx=12, pady=(0, 10))

    try:
        initial_visual_state = runtime.get_yami_visual_override_state()
        donor_choices = runtime.get_yami_visual_donor_choices()
    except Exception:
        initial_visual_state = {}
        donor_choices = [{"id": None, "label": "Native", "short": "Native"}]

    visual_target_var = tk.IntVar(value=0x17)
    visual_summary_var = tk.StringVar(value="")
    name_vars: dict[int, tk.StringVar] = {}
    for _yid in (0x17, 0x18, 0x19):
        _row = initial_visual_state.get(f"0x{_yid:02X}", {}) if isinstance(initial_visual_state, dict) else {}
        name_vars[_yid] = tk.StringVar(value=str(_row.get("name") or f"YAMI {_yid - 0x16}"))

    visual_card = _card(body)
    visual_card.pack(fill="x", pady=(0, 10))
    _label(visual_card, "Visual overrides", bold=True).pack(fill="x", padx=12, pady=(12, 4))
    _label(
        visual_card,
        "Panel donor uses the recomp-confirmed Character Select presentation-tag tables. Icon donor uses only the live thumbnail material ID. They are independent, so a Yami can keep a Ryu icon while borrowing a different large panel/background.",
        muted=True,
    ).pack(fill="x", padx=12, pady=(0, 8))

    target_row = tk.Frame(visual_card, bg=_CARD)
    target_row.pack(fill="x", padx=12, pady=(0, 8))
    _label(target_row, "Edit", bold=True).pack(side="left", padx=(0, 8))
    for _yid in (0x17, 0x18, 0x19):
        rb = tk.Radiobutton(
            target_row,
            text=f"Yami {_yid - 0x16}",
            variable=visual_target_var,
            value=_yid,
            indicatoron=False,
            bg="#2b3142",
            fg=_TEXT,
            selectcolor="#394058",
            activebackground="#394058",
            activeforeground=_TEXT,
            relief="flat",
            padx=12,
            pady=6,
            font=("Segoe UI", 10, "bold"),
        )
        rb.pack(side="left", padx=3)
    tk.Label(target_row, textvariable=visual_summary_var, bg=_CARD, fg=_ACCENT, font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left", padx=(12, 0))

    quick_row = tk.Frame(visual_card, bg=_CARD)
    quick_row.pack(fill="x", padx=12, pady=(0, 8))

    def _force_all_icons_ryu() -> None:
        for _yid in (0x17, 0x18, 0x19):
            runtime.queue_yami_icon_donor(_yid, 0x0C)
        _set_status("Ryu icon force queued for Yami 1/2/3.")

    def _force_all_panels_native() -> None:
        for _yid in (0x17, 0x18, 0x19):
            runtime.queue_yami_panel_donor(_yid, None)
        _set_status("Native panel restore queued for Yami 1/2/3.")

    def _force_all_panels_ryu() -> None:
        for _yid in (0x17, 0x18, 0x19):
            runtime.queue_yami_panel_donor(_yid, 0x0C)
        _set_status("Ryu large-panel force queued for Yami 1/2/3.")

    _button(quick_row, "Ryu icons x3", _force_all_icons_ryu).pack(side="left", padx=(0, 6))
    _button(quick_row, "Native panels x3", _force_all_panels_native).pack(side="left", padx=6)
    _button(quick_row, "Ryu panels x3", _force_all_panels_ryu).pack(side="left", padx=6)

    def _force_panel_donor(donor_id: Any) -> None:
        yid = int(visual_target_var.get())
        result = runtime.queue_yami_panel_donor(yid, donor_id)
        _set_status(
            f"Yami {yid - 0x16} panel -> {result.get('donor', donor_id)}."
            if result.get("ok") else str(result.get("error") or "Panel force failed.")
        )

    def _force_icon_donor(donor_id: Any) -> None:
        yid = int(visual_target_var.get())
        result = runtime.queue_yami_icon_donor(yid, donor_id)
        _set_status(
            f"Yami {yid - 0x16} icon -> {result.get('donor', donor_id)}."
            if result.get("ok") else str(result.get("error") or "Icon force failed.")
        )

    panel_wrap = tk.Frame(visual_card, bg=_CARD)
    panel_wrap.pack(fill="x", padx=12, pady=(0, 8))
    _label(panel_wrap, "Large panel / background donor", bold=True).pack(fill="x", pady=(0, 4))
    panel_grid = tk.Frame(panel_wrap, bg=_CARD)
    panel_grid.pack(fill="x")
    for i, donor in enumerate(donor_choices):
        donor_id = donor.get("id")
        label = str(donor.get("short") or donor.get("label") or donor_id)
        tk.Button(
            panel_grid,
            text=label,
            command=lambda d=donor_id: _force_panel_donor(d),
            bg="#2b3142",
            fg=_TEXT,
            activebackground="#394058",
            activeforeground=_TEXT,
            relief="flat",
            padx=6,
            pady=5,
            font=("Segoe UI", 9, "bold"),
        ).grid(row=i // 7, column=i % 7, sticky="ew", padx=2, pady=2)
    for c in range(7):
        panel_grid.columnconfigure(c, weight=1)

    icon_wrap = tk.Frame(visual_card, bg=_CARD)
    icon_wrap.pack(fill="x", padx=12, pady=(0, 8))
    _label(icon_wrap, "Cursor / wheel icon donor", bold=True).pack(fill="x", pady=(0, 4))
    icon_grid = tk.Frame(icon_wrap, bg=_CARD)
    icon_grid.pack(fill="x")
    for i, donor in enumerate(donor_choices):
        donor_id = donor.get("id")
        label = str(donor.get("short") or donor.get("label") or donor_id)
        tk.Button(
            icon_grid,
            text=label,
            command=lambda d=donor_id: _force_icon_donor(d),
            bg="#2b3142",
            fg=_TEXT,
            activebackground="#394058",
            activeforeground=_TEXT,
            relief="flat",
            padx=6,
            pady=5,
            font=("Segoe UI", 9, "bold"),
        ).grid(row=i // 7, column=i % 7, sticky="ew", padx=2, pady=2)
    for c in range(7):
        icon_grid.columnconfigure(c, weight=1)

    name_wrap = tk.Frame(visual_card, bg=_CARD)
    name_wrap.pack(fill="x", padx=12, pady=(0, 12))
    _label(name_wrap, "Custom visible label", bold=True).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
    for row_index, yid in enumerate((0x17, 0x18, 0x19), start=1):
        _label(name_wrap, f"Yami {yid - 0x16}").grid(row=row_index, column=0, sticky="w", padx=(0, 8), pady=3)
        ent = tk.Entry(
            name_wrap,
            textvariable=name_vars[yid],
            bg="#11151e",
            fg=_TEXT,
            insertbackground=_TEXT,
            relief="flat",
            font=("Segoe UI", 10, "bold"),
        )
        ent.grid(row=row_index, column=1, sticky="ew", padx=(0, 6), pady=3)

        def _apply_name(target_id: int = yid) -> None:
            result = runtime.queue_yami_custom_label(target_id, name_vars[target_id].get())
            _set_status(
                f"Yami {target_id - 0x16} label -> {result.get('text', '')}."
                if result.get("ok") else str(result.get("error") or "Name update failed.")
            )

        _button(name_wrap, "Set", _apply_name).grid(row=row_index, column=2, sticky="ew", pady=3)
        ent.bind("<Return>", lambda _event, target_id=yid: runtime.queue_yami_custom_label(target_id, name_vars[target_id].get()))
    name_wrap.columnconfigure(1, weight=1)

    setup_grid = tk.Frame(main_card, bg=_CARD)
    setup_grid.pack(fill="x", padx=12, pady=(0, 10))
    _label(setup_grid, "Force target", bold=True).grid(row=0, column=0, sticky="w", padx=(0, 10), pady=4)
    ttk.Combobox(setup_grid, textvariable=clone_slot_var, values=clone_choices, state="readonly", width=48).grid(row=0, column=1, sticky="ew", pady=4)
    setup_grid.columnconfigure(1, weight=1)

    def _set_status(text: str) -> None:
        status_var.set(text)

    def _restore() -> None:
        runtime.queue_roster_restore()
        _set_status("Restore queued.")

    def _snapshot() -> None:
        runtime.queue_roster_snapshot()
        _set_status("Snapshot queued.")

    def _shell_attempt() -> None:
        runtime.queue_yami_shell_attempt("zero")
        _set_status("Shell attempt queued.")

    def _visual_snapshot() -> None:
        runtime.queue_yami_visual_table_snapshot()
        _set_status("Visual table snapshot queued.")

    def _frank_append() -> None:
        runtime.queue_yami_visual_table_frank_append()
        _set_status("Frank visual rows queued.")

    def _native_append() -> None:
        runtime.queue_yami_visual_table_native_append()
        _set_status("Native Yami visual rows queued.")

    def _force_yami() -> None:
        slot = _parse_slot(clone_slot_var.get())
        target = {0x1B: 0x17, 0x1C: 0x18, 0x1D: 0x19}.get(slot, 0x17)
        runtime.queue_yami_force_hover(slot=slot, target=target)
        _set_status(f"Force target queued for slot 0x{slot:02X}.")

    def _native_name_test() -> None:
        runtime.queue_yami_native_name_test(5.0)
        _set_status("Native name-pointer rescan queued. Overlay will hide for 5 seconds.")

    button_card = _card(body)
    button_card.pack(fill="x", pady=(0, 10))
    _label(button_card, "Actions", bold=True).pack(fill="x", padx=12, pady=(12, 4))
    btns = tk.Frame(button_card, bg=_CARD)
    btns.pack(fill="x", padx=12, pady=(0, 12))
    buttons = [
        ("Snapshot", _snapshot),
        ("Visual table snapshot", _visual_snapshot),
        ("Shell attempt", _shell_attempt),
        ("Frank visual rows", _frank_append),
        ("Native Yami rows", _native_append),
        ("Force target", _force_yami),
        ("Test native name (5s)", _native_name_test),
        ("Restore", _restore),
    ]
    for i, (text, cmd) in enumerate(buttons):
        _button(btns, text, cmd).grid(row=i // 3, column=i % 3, sticky="ew", padx=4, pady=4)
    for c in range(3):
        btns.columnconfigure(c, weight=1)

    status_card = _card(body)
    status_card.pack(fill="both", expand=True, pady=(0, 10))
    _label(status_card, "State", bold=True).pack(fill="x", padx=12, pady=(12, 4))
    tk.Label(status_card, textvariable=status_var, bg=_CARD, fg=_ACCENT, font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", padx=12, pady=(0, 6))
    state_text = tk.Text(status_card, height=20, bg="#11151e", fg=_TEXT, insertbackground=_TEXT, relief="flat", wrap="word")
    state_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _refresh() -> None:
        try:
            state = runtime.get_roster_patch_state()
            lines: list[str] = []
            lines.append(f"last action: {state.get('last_action', '')}")
            lines.append(f"last error: {state.get('last_error', '')}")
            lines.append(f"patches: {state.get('patches', 0)}  failed: {state.get('failed', 0)}  queued: {state.get('queued', 0)}")
            lines.append(f"visual table mode: {state.get('visual_table_patch_mode', '')}")
            overrides = state.get("yami_visual_overrides") or {}
            active_yid = int(visual_target_var.get())
            active_override = overrides.get(f"0x{active_yid:02X}", {}) if isinstance(overrides, dict) else {}
            visual_summary_var.set(
                f"Panel: {active_override.get('panel_label', 'Native')}   Icon: {active_override.get('icon_label', 'Ryu')}"
            )
            lines.append(f"panel force: {state.get('yami_visual_panel_detail', '') or 'idle'}")
            lines.append(f"icon force: {state.get('yami_visual_icon_detail', '') or 'idle'}")
            lines.append(f"Yami overlay name: {state.get('yami_custom_name_text', '') or 'idle'}")
            lines.append(f"Yami overlay detail: {state.get('yami_custom_name_detail', '')}")
            lines.append(f"Native name: {state.get('yami_native_name_pointer_text', '') or 'not bound'}")
            lines.append(f"Native name detail: {state.get('yami_native_name_pointer_detail', '')}")
            native = state.get("yami_native_name_session") or {}
            if native:
                lines.append(
                    "Native evidence: "
                    f"source={native.get('source_texture_addr', '') or '-'} "
                    f"scratch={native.get('scratch_addr', '') or '-'} "
                    f"verified={len(native.get('verified_candidates') or [])} "
                    f"raw_refs={len(native.get('raw_texture_refs') or [])} "
                    f"tpl_refs={len(native.get('tpl_refs') or [])} "
                    f"header_refs={len(native.get('header_refs') or [])}"
                )
                if native.get("verified_candidates"):
                    lines.append("Verified pointer fields: " + ", ".join(native.get("verified_candidates")[:8]))
            lines.append("")
            lines.append("selector:")
            lines.append(_format_snapshot(state.get("last_snapshot") or {}))
            lines.append("")
            lines.append("visual table:")
            lines.append(_format_visual_table(state.get("visual_table_snapshot") or {}))
            state_text.delete("1.0", "end")
            state_text.insert("1.0", "\n".join(lines))
        except Exception as exc:
            state_text.delete("1.0", "end")
            state_text.insert("1.0", repr(exc))
        try:
            win.after(500, _refresh)
        except Exception:
            pass

    _refresh()


def open_char_test_window(master: tk.Misc | None = None) -> None:
    if tk_call is not None:
        tk_call(lambda root: _show_char_test_window(master or root))
        return
    _show_char_test_window(master)


def show_char_test_window(master: tk.Misc | None = None) -> None:
    if tk_call is not None:
        tk_call(lambda root: _show_char_test_window(master or root))
        return
    _show_char_test_window(master)
