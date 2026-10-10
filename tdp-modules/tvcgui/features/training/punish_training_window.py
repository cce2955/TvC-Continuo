from __future__ import annotations

import copy
import tkinter as tk
from tkinter import ttk, simpledialog
from typing import Any, Callable

try:
    from tvcgui.core.tk_host import tk_call
except Exception:
    tk_call = None

try:
    from tvcgui.features.frame_data.widgets import apply_titlebar_icon
except Exception:
    def apply_titlebar_icon(_win, _parent=None):
        return None

_OPEN_WINDOW: tk.Toplevel | None = None

_BG = '#0D1018'
_PANEL = '#171D2A'
_PANEL_ALT = '#1B2639'
_PANEL_2 = '#243550'
_BORDER = '#344963'
_TEXT = '#ECF4FF'
_MUTED = '#A9B8CC'
_ACCENT = '#75B8FF'
_FIELD = '#111827'
_GOOD = '#7CE0B5'
_WARN = '#F0BE72'

_TRANSITION_LABELS = {
    'Cancel ASAP': 'cancel',
    'Wait for neutral': 'neutral',
    'Delay N frames': 'delay',
    'Random delay': 'random_delay',
}
_TRANSITION_NAMES = {value: key for key, value in _TRANSITION_LABELS.items()}


def _clean(value: Any) -> str:
    return ' '.join(str(value or '').replace('\r', ' ').replace('\n', ' ').split())[:128]


def _label(parent: tk.Misc, text: str, *, muted: bool = False, bold: bool = False, size: int = 9, color: str | None = None) -> tk.Label:
    return tk.Label(
        parent,
        text=text,
        bg=parent.cget('bg'),
        fg=color or (_MUTED if muted else _TEXT),
        font=('Segoe UI', size, 'bold' if bold else 'normal'),
        anchor='w',
        justify='left',
        wraplength=720,
    )


def _button(parent: tk.Misc, text: str, command: Callable[[], None] | None = None, *, width: int | None = None, primary: bool = False) -> tk.Button:
    return tk.Button(
        parent,
        text=text,
        command=command,
        width=width or 0,
        bg='#2C5E91' if primary else _PANEL_2,
        fg='#FFFFFF' if primary else _TEXT,
        activebackground='#3C73AB' if primary else '#315A87',
        activeforeground='#FFFFFF',
        relief='flat',
        bd=0,
        padx=10,
        pady=6,
        font=('Segoe UI', 9, 'bold' if primary else 'normal'),
        highlightthickness=1,
        highlightbackground='#6094C7' if primary else _BORDER,
        highlightcolor=_ACCENT,
        cursor='hand2',
    )


def _card(parent: tk.Misc, *, alt: bool = False) -> tk.Frame:
    outer = tk.Frame(parent, bg=_BORDER, padx=1, pady=1)
    inner = tk.Frame(outer, bg=_PANEL_ALT if alt else _PANEL, padx=12, pady=10)
    inner.pack(fill='both', expand=True)
    outer.inner = inner  # type: ignore[attr-defined]
    return outer


def _configure_styles(win: tk.Misc) -> None:
    try:
        style = ttk.Style(win)
        style.theme_use('clam')
        style.configure(
            'Punish.TCombobox',
            fieldbackground=_FIELD,
            background=_PANEL_2,
            foreground=_TEXT,
            arrowcolor=_TEXT,
            bordercolor=_BORDER,
            lightcolor=_BORDER,
            darkcolor=_BORDER,
            padding=5,
            font=('Segoe UI', 9),
        )
        style.map(
            'Punish.TCombobox',
            fieldbackground=[('readonly', _FIELD)],
            foreground=[('readonly', _TEXT)],
            selectbackground=[('readonly', '#315A87')],
            selectforeground=[('readonly', _TEXT)],
        )
        style.configure(
            'Punish.Treeview',
            background=_FIELD,
            fieldbackground=_FIELD,
            foreground=_TEXT,
            bordercolor=_BORDER,
            rowheight=25,
            font=('Consolas', 9),
        )
        style.map('Punish.Treeview', background=[('selected', '#315A87')], foreground=[('selected', _TEXT)])
        style.configure(
            'Punish.Treeview.Heading',
            background=_PANEL_2,
            foreground=_TEXT,
            bordercolor=_BORDER,
            font=('Segoe UI', 9, 'bold'),
        )
    except Exception:
        pass


def _clamp_float(value: Any, default: float = 3.0, low: float = 0.25, high: float = 30.0) -> float:
    try:
        value = float(value)
    except Exception:
        value = default
    return round(max(low, min(high, value)), 2)


def _clamp_frames(value: Any, default: int = 0) -> int:
    try:
        value = int(value)
    except Exception:
        value = default
    return max(0, min(180, value))


def _normalize_step(step: Any) -> dict[str, Any] | None:
    if not isinstance(step, dict):
        return None
    try:
        action_id = int(step.get('action_id') or 0) & 0xFFFF
    except Exception:
        return None
    if action_id <= 0:
        return None
    low = _clamp_frames(step.get('random_min_frames'), 1)
    high = _clamp_frames(step.get('random_max_frames'), max(1, low))
    if low > high:
        low, high = high, low
    label = _clean(step.get('move_label') or step.get('label')) or f'0x{action_id:03X}'
    kind = str(step.get('kind') or '').strip().lower()
    if kind not in {'jump', 'super_jump', 'move'}:
        folded = label.casefold()
        if action_id == 0x01F and folded.startswith('super jump'):
            kind = 'super_jump'
        elif action_id == 0x013 and folded.startswith('jump'):
            kind = 'jump'
        else:
            kind = 'move'
    transition = str(step.get('transition') or 'cancel')
    if transition not in {'cancel', 'neutral', 'delay', 'random_delay'}:
        transition = 'cancel'
    return {
        'move_label': label,
        'action_id': action_id,
        'kind': kind,
        'transition': transition,
        'delay_frames': _clamp_frames(step.get('delay_frames'), 0),
        'random_min_frames': low,
        'random_max_frames': high,
    }


def _transition_text(step: dict[str, Any], is_last: bool) -> str:
    mode = str(step.get('transition') or 'cancel')
    if mode == 'neutral':
        return 'Wait for neutral'
    if mode == 'delay':
        return f"Delay {int(step.get('delay_frames') or 0)}F"
    if mode == 'random_delay':
        return f"Random {int(step.get('random_min_frames') or 0)}-{int(step.get('random_max_frames') or 0)}F"
    return 'Cancel ASAP'


def open_punish_training_window(
    state: dict,
    save_func: Callable[[dict], None] | None = None,
    roster_provider: Callable[[], dict] | None = None,
) -> None:
    def _open(tk_root: tk.Misc | None = None) -> None:
        global _OPEN_WINDOW
        if _OPEN_WINDOW is not None:
            try:
                if _OPEN_WINDOW.winfo_exists():
                    _OPEN_WINDOW.deiconify()
                    _OPEN_WINDOW.lift()
                    _OPEN_WINDOW.focus_force()
                    return
            except Exception:
                pass
            _OPEN_WINDOW = None

        win = tk.Toplevel(tk_root) if tk_root is not None else tk.Toplevel()
        _OPEN_WINDOW = win
        win.title('Punish Trainer')
        win.configure(bg=_BG)
        win.geometry('790x720')
        win.minsize(620, 500)
        win.resizable(True, True)
        try:
            apply_titlebar_icon(win)
        except Exception:
            pass
        _configure_styles(win)

        # Local editor state.
        slot_var = tk.StringVar(value=str(state.get('target_slot') or 'P2-C1'))
        move_var = tk.StringVar(value='Choose move')
        cooldown_var = tk.StringVar(value=f"{_clamp_float(state.get('cooldown_sec')):g}")
        random_interval_var = tk.BooleanVar(value=bool(state.get('random_interval', False)))
        random_min_var = tk.StringVar(value=f"{_clamp_float(state.get('random_min_sec'), 5.0):g}")
        random_max_var = tk.StringVar(value=f"{_clamp_float(state.get('random_max_sec'), 10.0):g}")
        countdown_var = tk.BooleanVar(value=bool(state.get('show_countdown', False)))
        step_timeout_var = tk.StringVar(value=str(_clamp_frames(state.get('step_timeout_frames'), 60)))
        randomize_presets_var = tk.BooleanVar(value=bool(state.get('randomize_presets', False)))
        enabled_var = tk.BooleanVar(value=bool(state.get('enabled', False)))
        auto_add_jump_var = tk.BooleanVar(value=bool(state.get('auto_add_jump', True)))
        preset_name_var = tk.StringVar(value='')
        preset_var = tk.StringVar(value='')

        # New sequence rows are deliberately boring: Cancel ASAP. Timing is
        # edited only after the move is in the sequence. Older saved rows keep
        # whatever timing they already had.
        sticky_transition = 'cancel'
        sticky_low = 1
        sticky_high = 4
        if sticky_low > sticky_high:
            sticky_low, sticky_high = sticky_high, sticky_low
        new_step_defaults: dict[str, Any] = {
            'transition': sticky_transition,
            'delay_frames': 0,
            'random_min_frames': sticky_low,
            'random_max_frames': sticky_high,
        }
        transition_var = tk.StringVar(value=_TRANSITION_NAMES.get(sticky_transition, 'Cancel ASAP'))
        delay_var = tk.StringVar(value=str(new_step_defaults['delay_frames']))
        random_delay_min_var = tk.StringVar(value=str(sticky_low))
        random_delay_max_var = tk.StringVar(value=str(sticky_high))

        sequence: list[dict[str, Any]] = []
        for raw in state.get('sequence') or []:
            step = _normalize_step(raw)
            if step:
                sequence.append(step)
        presets: dict[str, list[dict[str, Any]]] = {}
        if isinstance(state.get('presets'), dict):
            for name, raw_seq in state['presets'].items():
                clean_seq = [step for raw in (raw_seq or []) if (step := _normalize_step(raw))]
                if clean_seq:
                    presets[_clean(name)[:48]] = clean_seq

        roster: dict[str, dict[str, Any]] = {}
        move_lookup: dict[str, dict[str, Any]] = {}
        suppress_step_events = False
        last_roster_signature: tuple[Any, ...] | None = None

        # Fixed header.
        shell = tk.Frame(win, bg=_BG, padx=12, pady=12)
        shell.pack(fill='both', expand=True)

        header = _card(shell, alt=True)
        header.pack(fill='x', pady=(0, 8))
        hi = header.inner  # type: ignore[attr-defined]
        head_row = tk.Frame(hi, bg=_PANEL_ALT)
        head_row.pack(fill='x')
        _label(head_row, 'PUNISH TRAINER', bold=True, size=12, color=_ACCENT).pack(side='left')
        live_text = _label(head_row, '', muted=True)
        live_text.pack(side='right')
        _label(
            hi,
            'Deterministic repeat loop: target idle → countdown → run sequence → target idle → fresh countdown.',
            muted=True,
        ).pack(anchor='w', pady=(4, 0))

        # Scrollable middle.  This is the only vertically growing area.
        body_wrap = tk.Frame(shell, bg=_BG)
        body_wrap.pack(fill='both', expand=True)
        canvas = tk.Canvas(body_wrap, bg=_BG, highlightthickness=0, bd=0)
        vscroll = ttk.Scrollbar(body_wrap, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        body = tk.Frame(canvas, bg=_BG, padx=1, pady=1)
        body_id = canvas.create_window((0, 0), window=body, anchor='nw')

        def _sync_scrollregion(_event=None) -> None:
            try:
                canvas.configure(scrollregion=canvas.bbox('all'))
            except Exception:
                pass

        def _fit_body(event) -> None:
            try:
                canvas.itemconfigure(body_id, width=max(100, int(event.width)))
            except Exception:
                pass

        body.bind('<Configure>', _sync_scrollregion)
        canvas.bind('<Configure>', _fit_body)

        def _wheel(event) -> str:
            try:
                delta = int(event.delta)
                steps = -1 if delta > 0 else 1
                if abs(delta) >= 120:
                    steps = int(-delta / 120)
                canvas.yview_scroll(steps, 'units')
            except Exception:
                pass
            return 'break'

        def _wheel_linux_up(_event) -> str:
            canvas.yview_scroll(-1, 'units')
            return 'break'

        def _wheel_linux_down(_event) -> str:
            canvas.yview_scroll(1, 'units')
            return 'break'

        win.bind('<MouseWheel>', _wheel, add='+')
        win.bind('<Button-4>', _wheel_linux_up, add='+')
        win.bind('<Button-5>', _wheel_linux_down, add='+')

        # Target and move builder.
        target_card = _card(body)
        target_card.pack(fill='x', pady=(0, 8))
        target = target_card.inner  # type: ignore[attr-defined]
        target.grid_columnconfigure(1, weight=1)
        _label(target, 'TARGET + ADD MOVE', bold=True, size=9, color=_GOOD).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 8))
        _label(target, 'Dummy slot', bold=True).grid(row=1, column=0, sticky='w', padx=(0, 10), pady=(0, 7))
        slot_combo = ttk.Combobox(target, textvariable=slot_var, values=['P1-C1', 'P1-C2', 'P2-C1', 'P2-C2'], state='readonly', style='Punish.TCombobox', width=14)
        slot_combo.grid(row=1, column=1, sticky='w', pady=(0, 7))
        _label(target, 'Move', bold=True).grid(row=2, column=0, sticky='w', padx=(0, 10))
        add_row = tk.Frame(target, bg=_PANEL)
        add_row.grid(row=2, column=1, sticky='ew')
        add_row.grid_columnconfigure(0, weight=1)
        move_combo = ttk.Combobox(add_row, textvariable=move_var, values=['Choose move'], state='readonly', style='Punish.TCombobox')
        move_combo.grid(row=0, column=0, sticky='ew')
        add_btn = _button(add_row, '+ Add', width=8)
        add_btn.grid(row=0, column=1, padx=(7, 0))
        auto_jump_check = tk.Checkbutton(
            target,
            text='Auto-add Jump before aerial moves',
            variable=auto_add_jump_var,
            bg=_PANEL, fg=_TEXT, activebackground=_PANEL, activeforeground=_TEXT,
            selectcolor=_FIELD, font=('Segoe UI', 9, 'bold'), anchor='w', cursor='hand2',
        )
        auto_jump_check.grid(row=3, column=1, sticky='w', pady=(7, 0))

        # Sequence editor.
        seq_card = _card(body, alt=True)
        seq_card.pack(fill='x', pady=(0, 8))
        seq = seq_card.inner  # type: ignore[attr-defined]
        _label(seq, 'SEQUENCE', bold=True, size=9, color=_ACCENT).pack(anchor='w')
        _label(seq, 'Add moves in order. Every new move is Cancel ASAP. Click a row only when you want to change its timing.', muted=True).pack(anchor='w', pady=(2, 7))

        tree_frame = tk.Frame(seq, bg=_PANEL_ALT)
        tree_frame.pack(fill='x')
        sequence_tree = ttk.Treeview(tree_frame, columns=('move', 'after'), show='headings', height=7, style='Punish.Treeview', selectmode='browse')
        sequence_tree.heading('move', text='Move')
        sequence_tree.heading('after', text='Timing')
        sequence_tree.column('move', width=420, minwidth=180, stretch=True)
        sequence_tree.column('after', width=180, minwidth=130, stretch=False)
        tree_scroll = ttk.Scrollbar(tree_frame, orient='vertical', command=sequence_tree.yview)
        sequence_tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side='right', fill='y')
        sequence_tree.pack(side='left', fill='x', expand=True)

        toolbar = tk.Frame(seq, bg=_PANEL_ALT)
        toolbar.pack(fill='x', pady=(7, 0))
        up_btn = _button(toolbar, 'Up', width=6)
        up_btn.pack(side='left')
        down_btn = _button(toolbar, 'Down', width=6)
        down_btn.pack(side='left', padx=(6, 0))
        remove_btn = _button(toolbar, 'Remove', width=8)
        remove_btn.pack(side='left', padx=(6, 0))
        clear_btn = _button(toolbar, 'Clear All', width=9)
        clear_btn.pack(side='left', padx=(6, 0))

        editor = tk.Frame(seq, bg=_PANEL_ALT)
        editor.pack(fill='x', pady=(10, 0))
        selected_timing_text = tk.StringVar(value='Select a move to edit timing')
        timing_caption = _label(editor, '', bold=True)
        timing_caption.configure(textvariable=selected_timing_text)
        timing_caption.pack(anchor='w')
        timing_buttons = tk.Frame(editor, bg=_PANEL_ALT)
        timing_buttons.pack(fill='x', pady=(6, 0))
        cancel_timing_btn = _button(timing_buttons, 'Cancel ASAP', width=12, primary=True)
        cancel_timing_btn.pack(side='left')
        fixed_timing_btn = _button(timing_buttons, 'Delay this move…', width=16)
        fixed_timing_btn.pack(side='left', padx=(7, 0))
        random_timing_btn = _button(timing_buttons, 'Random delay…', width=14)
        random_timing_btn.pack(side='left', padx=(7, 0))
        _label(editor, 'Random delay is rolled again every repetition.', muted=True).pack(anchor='w', pady=(5, 0))

        # Timing.
        timing_card = _card(body)
        timing_card.pack(fill='x', pady=(0, 8))
        timing = timing_card.inner  # type: ignore[attr-defined]
        _label(timing, 'ROUND TIMING', bold=True, size=9, color=_GOOD).pack(anchor='w')
        timing_top = tk.Frame(timing, bg=_PANEL)
        timing_top.pack(fill='x', pady=(7, 0))
        _label(timing_top, 'Countdown', bold=True).pack(side='left')
        cooldown_spin = tk.Spinbox(timing_top, textvariable=cooldown_var, width=6, from_=0.25, to=30.0, increment=0.25, bg=_FIELD, fg=_TEXT, disabledbackground='#202838', disabledforeground='#748297', insertbackground=_TEXT, buttonbackground=_PANEL_2, relief='flat', bd=1, highlightthickness=1, highlightbackground=_BORDER, highlightcolor=_ACCENT, font=('Segoe UI', 9))
        cooldown_spin.pack(side='left', padx=(7, 4))
        _label(timing_top, 'sec', muted=True).pack(side='left')
        random_interval_check = tk.Checkbutton(timing_top, text='Random range', variable=random_interval_var, bg=_PANEL, fg=_TEXT, activebackground=_PANEL, activeforeground=_TEXT, selectcolor=_FIELD, font=('Segoe UI', 9, 'bold'), cursor='hand2')
        random_interval_check.pack(side='left', padx=(18, 6))
        random_min_spin = tk.Spinbox(timing_top, textvariable=random_min_var, width=5, from_=0.25, to=30.0, increment=0.25, bg=_FIELD, fg=_TEXT, disabledbackground='#202838', disabledforeground='#748297', insertbackground=_TEXT, buttonbackground=_PANEL_2, relief='flat', bd=1, highlightthickness=1, highlightbackground=_BORDER, highlightcolor=_ACCENT, font=('Segoe UI', 9))
        random_min_spin.pack(side='left')
        _label(timing_top, 'to', muted=True).pack(side='left', padx=4)
        random_max_spin = tk.Spinbox(timing_top, textvariable=random_max_var, width=5, from_=0.25, to=30.0, increment=0.25, bg=_FIELD, fg=_TEXT, disabledbackground='#202838', disabledforeground='#748297', insertbackground=_TEXT, buttonbackground=_PANEL_2, relief='flat', bd=1, highlightthickness=1, highlightbackground=_BORDER, highlightcolor=_ACCENT, font=('Segoe UI', 9))
        random_max_spin.pack(side='left')

        timeout_row = tk.Frame(timing, bg=_PANEL)
        timeout_row.pack(fill='x', pady=(8, 0))
        _label(timeout_row, 'Stuck step give-up', bold=True).pack(side='left')
        step_timeout_spin = tk.Spinbox(
            timeout_row, textvariable=step_timeout_var, width=5, from_=15, to=300, increment=5,
            bg=_FIELD, fg=_TEXT, insertbackground=_TEXT, buttonbackground=_PANEL_2,
            relief='flat', bd=1, highlightthickness=1, highlightbackground=_BORDER,
            highlightcolor=_ACCENT, font=('Segoe UI', 9),
        )
        step_timeout_spin.pack(side='left', padx=(7, 4))
        _label(timeout_row, 'frames', muted=True).pack(side='left')
        _label(
            timing,
            'If a mailbox step is not accepted in time, that rep is aborted, the request is cleared, and the target is driven back to native idle before a fresh countdown.',
            muted=True,
        ).pack(anchor='w', pady=(3, 0))

        _label(
            timing,
            'Target-idle rule: countdown runs only while the selected target is native idle (0x001).',
            bold=True,
        ).pack(anchor='w', pady=(8, 0))
        _label(
            timing,
            'If the target leaves idle before zero, that countdown is discarded. The next idle starts a full fresh countdown. Active sequences keep running until the target returns to idle.',
            muted=True,
        ).pack(anchor='w', pady=(2, 0))
        countdown_check = tk.Checkbutton(
            timing,
            text='Show countdown on HUD',
            variable=countdown_var,
            bg=_PANEL, fg=_TEXT, activebackground=_PANEL, activeforeground=_TEXT,
            selectcolor=_FIELD, font=('Segoe UI', 9, 'bold'), anchor='w', cursor='hand2',
        )
        countdown_check.pack(anchor='w', pady=(7, 0))

        # Presets.
        preset_card = _card(body)
        preset_card.pack(fill='x', pady=(0, 8))
        preset = preset_card.inner  # type: ignore[attr-defined]
        _label(preset, 'PRESETS / MIXUP SET', bold=True, size=9, color=_WARN).pack(anchor='w')
        preset_row = tk.Frame(preset, bg=_PANEL)
        preset_row.pack(fill='x', pady=(7, 0))
        preset_combo = ttk.Combobox(preset_row, textvariable=preset_var, values=sorted(presets), state='readonly', style='Punish.TCombobox', width=22)
        preset_combo.pack(side='left')
        load_btn = _button(preset_row, 'Load', width=7)
        load_btn.pack(side='left', padx=(6, 0))
        delete_btn = _button(preset_row, 'Delete', width=7)
        delete_btn.pack(side='left', padx=(6, 12))
        preset_name_entry = tk.Entry(preset_row, textvariable=preset_name_var, bg=_FIELD, fg=_TEXT, insertbackground=_TEXT, relief='flat', bd=1, highlightthickness=1, highlightbackground=_BORDER, highlightcolor=_ACCENT, font=('Segoe UI', 9), width=20)
        preset_name_entry.pack(side='left', fill='x', expand=True)
        save_preset_btn = _button(preset_row, 'Save Current', width=12)
        save_preset_btn.pack(side='left', padx=(6, 0))
        randomize_presets_check = tk.Checkbutton(
            preset,
            text='Randomly choose a saved preset each round',
            variable=randomize_presets_var,
            bg=_PANEL, fg=_TEXT, activebackground=_PANEL, activeforeground=_TEXT,
            selectcolor=_FIELD, font=('Segoe UI', 9, 'bold'), anchor='w', cursor='hand2',
        )
        randomize_presets_check.pack(anchor='w', pady=(8, 0))

        # Fixed footer.  Arm and test controls never scroll off-screen.
        footer = tk.Frame(shell, bg=_BG)
        footer.pack(fill='x', pady=(8, 0))
        controls = tk.Frame(footer, bg=_BG)
        controls.pack(fill='x')
        arm_btn = _button(controls, '', primary=True)
        arm_btn.pack(side='left', fill='x', expand=True)
        test_btn = _button(controls, 'Perform Once', width=14)
        test_btn.pack(side='left', padx=(8, 0))
        reset_btn = _button(controls, 'Reset', width=8)
        reset_btn.pack(side='left', padx=(8, 0))
        status = _label(footer, _clean(state.get('last_status')), muted=True)
        status.pack(fill='x', pady=(7, 0))

        def _read_roster() -> dict:
            if roster_provider is None:
                return {}
            try:
                data = roster_provider()
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

        def _selected_entry() -> dict[str, Any]:
            return roster.get(slot_var.get()) or {}

        def _refresh_live() -> None:
            entry = _selected_entry()
            slot = slot_var.get()
            name = _clean(entry.get('name')) or 'Waiting'
            base = int(entry.get('base') or 0)
            phase = _clean(state.get('phase')).replace('_', ' ').upper() or 'OFF'
            live_text.configure(text=f"{slot}  |  {name}  |  {f'0x{base:08X}' if base else 'not live'}  |  {phase}")

        def _refresh_arm_button() -> None:
            if bool(enabled_var.get()):
                arm_btn.configure(text='● TRAINER ARMED', bg='#176B4C', fg='#F3FFF9', activebackground='#1D805C', highlightbackground='#53C992')
            else:
                arm_btn.configure(text='○ TRAINER OFF', bg='#2C5E91', fg='#FFFFFF', activebackground='#3C73AB', highlightbackground='#6094C7')

        def _refresh_timing_controls() -> None:
            random_on = bool(random_interval_var.get())
            try:
                cooldown_spin.configure(state='disabled' if random_on else 'normal')
                random_min_spin.configure(state='normal' if random_on else 'disabled')
                random_max_spin.configure(state='normal' if random_on else 'disabled')
            except Exception:
                pass

        def _refresh_transition_controls() -> None:
            idx = _selected_index()
            if idx is None or not (0 <= idx < len(sequence)):
                selected_timing_text.set('Select a move to edit timing')
                return
            step = sequence[idx]
            timing_text = _timing_text_for_move(idx)
            if idx == 0:
                selected_timing_text.set(f"Selected: 1. {step.get('move_label', '')}  •  Start of sequence")
            else:
                selected_timing_text.set(f"Selected: {idx + 1}. {step.get('move_label', '')}  •  {timing_text}")

        def _refresh_moves(*, preserve: bool = True) -> None:
            nonlocal move_lookup
            entry = _selected_entry()
            options = ['Choose move']
            move_lookup = {}
            for item in entry.get('moves') or []:
                if not isinstance(item, dict):
                    continue
                display = _clean(item.get('display'))
                try:
                    action_id = int(item.get('action_id')) & 0xFFFF
                except Exception:
                    continue
                if display:
                    options.append(display)
                    move_lookup[display] = {
                        'action_id': action_id,
                        'kind': str(item.get('kind') or 'move'),
                        'label': _clean(item.get('label') or display),
                    }
            move_combo.configure(values=options)
            if not preserve or move_var.get() not in options:
                move_var.set('Choose move')
            _refresh_live()

        def _selected_index() -> int | None:
            selected = sequence_tree.selection()
            if not selected:
                return None
            try:
                return int(str(selected[0]).replace('step_', ''))
            except Exception:
                return None

        def _timing_payload(step: dict[str, Any] | None = None) -> dict[str, Any]:
            step = step or {}
            return {
                'transition': str(step.get('transition') or 'cancel'),
                'delay_frames': _clamp_frames(step.get('delay_frames'), 0),
                'random_min_frames': _clamp_frames(step.get('random_min_frames'), 1),
                'random_max_frames': _clamp_frames(step.get('random_max_frames'), 4),
            }

        def _entry_timing(index: int) -> dict[str, Any] | None:
            # Runtime stores timing on the preceding row.  The UI deliberately
            # presents it as timing for the move being entered, because that is
            # how humans build the sequence: select j.B, then delay j.B.
            if index <= 0 or index >= len(sequence):
                return None
            return _timing_payload(sequence[index - 1])

        def _set_entry_timing(index: int, payload: dict[str, Any]) -> bool:
            if index <= 0 or index >= len(sequence):
                return False
            sequence[index - 1].update(_timing_payload(payload))
            return True

        def _sequence_pairs() -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
            return [(step, _entry_timing(idx)) for idx, step in enumerate(sequence)]

        def _restore_pairs(pairs: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> None:
            sequence[:] = [step for step, _timing in pairs]
            # Reset all outgoing boundaries, then rebuild them from each move's
            # incoming timing so timing follows the move through Up/Down/remove.
            for step in sequence:
                step.update({'transition': 'cancel', 'delay_frames': 0, 'random_min_frames': 1, 'random_max_frames': 4})
            for idx in range(1, len(pairs)):
                payload = pairs[idx][1] or {'transition': 'cancel', 'delay_frames': 0, 'random_min_frames': 1, 'random_max_frames': 4}
                _set_entry_timing(idx, payload)

        def _timing_text_for_move(index: int) -> str:
            if index <= 0:
                return 'Start'
            payload = _entry_timing(index) or {}
            return _transition_text(payload, False)

        def _refresh_sequence(select_index: int | None = None) -> None:
            nonlocal suppress_step_events
            suppress_step_events = True
            for item in sequence_tree.get_children():
                sequence_tree.delete(item)
            total = len(sequence)
            for idx, step in enumerate(sequence):
                sequence_tree.insert('', 'end', iid=f'step_{idx}', values=(f"{idx + 1:02d}. {step['move_label']} [0x{int(step['action_id']):03X}]", _timing_text_for_move(idx)))
            if total:
                if select_index is None:
                    select_index = min(total - 1, 0)
                select_index = max(0, min(total - 1, int(select_index)))
                iid = f'step_{select_index}'
                sequence_tree.selection_set(iid)
                sequence_tree.focus(iid)
                sequence_tree.see(iid)
                step = sequence[select_index]
                transition_var.set(_TRANSITION_NAMES.get(str(step.get('transition') or 'cancel'), 'Cancel ASAP'))
                delay_var.set(str(int(step.get('delay_frames') or 0)))
                random_delay_min_var.set(str(int(step.get('random_min_frames') or 1)))
                random_delay_max_var.set(str(int(step.get('random_max_frames') or 4)))
            else:
                transition_var.set(_TRANSITION_NAMES.get(str(new_step_defaults.get('transition') or 'cancel'), 'Cancel ASAP'))
                delay_var.set(str(int(new_step_defaults.get('delay_frames') or 0)))
                random_delay_min_var.set(str(int(new_step_defaults.get('random_min_frames') or 1)))
                random_delay_max_var.set(str(int(new_step_defaults.get('random_max_frames') or 4)))
            suppress_step_events = False
            _refresh_transition_controls()
            _sync_scrollregion()

        def _refresh_presets() -> None:
            names = sorted(name for name in presets if name)
            preset_combo.configure(values=names)
            if preset_var.get() not in names:
                preset_var.set(names[0] if names else '')

        def _apply(*, reset_runtime: bool = True) -> None:
            state['enabled'] = bool(enabled_var.get())
            state['target_slot'] = slot_var.get()
            state['sequence'] = copy.deepcopy(sequence)
            if sequence:
                state['move_label'] = sequence[0]['move_label']
                state['action_id'] = sequence[0]['action_id']
            else:
                state['move_label'] = ''
                state['action_id'] = 0
            state['presets'] = copy.deepcopy(presets)
            state['randomize_presets'] = bool(randomize_presets_var.get())
            state['require_neutral'] = True  # compatibility field; runtime rule is mandatory
            state['cooldown_sec'] = _clamp_float(cooldown_var.get())
            low = _clamp_float(random_min_var.get(), 5.0)
            high = _clamp_float(random_max_var.get(), 10.0)
            if low > high:
                low, high = high, low
            state['random_interval'] = bool(random_interval_var.get())
            state['random_min_sec'] = low
            state['random_max_sec'] = high
            state['show_countdown'] = bool(countdown_var.get())
            state['step_timeout_frames'] = max(15, min(300, _clamp_frames(step_timeout_var.get(), 60)))
            step_timeout_var.set(str(state['step_timeout_frames']))
            state['auto_add_jump'] = bool(auto_add_jump_var.get())
            state['last_transition'] = str(new_step_defaults.get('transition') or 'cancel')
            state['last_delay_frames'] = _clamp_frames(new_step_defaults.get('delay_frames'), 0)
            state['last_random_min_frames'] = _clamp_frames(new_step_defaults.get('random_min_frames'), 1)
            state['last_random_max_frames'] = _clamp_frames(new_step_defaults.get('random_max_frames'), 4)
            cooldown_var.set(f"{state['cooldown_sec']:g}")
            random_min_var.set(f"{low:g}")
            random_max_var.set(f"{high:g}")
            if reset_runtime:
                state['runtime_reset_requested'] = True
            if save_func is not None:
                save_func(state)
            _refresh_arm_button()
            _refresh_timing_controls()
            _refresh_live()

        def _looks_aerial(label: str) -> bool:
            text = _clean(label).split('[', 1)[0].strip().casefold()
            compact = text.replace(' ', '')
            if compact.startswith(('j.', 'j-', 'jump.')):
                return True
            return any(token in f' {text} ' for token in (' air ', ' aerial ', ' airborne '))

        def _jump_step(*, super_jump: bool = False) -> dict[str, Any]:
            return {
                'move_label': 'Super Jump' if super_jump else 'Jump',
                'action_id': 0x01F if super_jump else 0x013,
                'kind': 'super_jump' if super_jump else 'jump',
                'transition': 'cancel',
                'delay_frames': 0,
                'random_min_frames': 1,
                'random_max_frames': 4,
            }

        def _add_move() -> None:
            selected = move_var.get()
            meta = move_lookup.get(selected) or {}
            action_id = int(meta.get('action_id') or 0)
            if not action_id:
                status.configure(text='Choose a move to add.')
                return
            kind = str(meta.get('kind') or 'move').strip().lower()
            label = _clean(meta.get('label') or selected)
            auto_jump = bool(auto_add_jump_var.get()) and kind not in {'jump', 'super_jump'} and _looks_aerial(label) and not (
                sequence and str(sequence[-1].get('kind') or '').lower() in {'jump', 'super_jump'}
            )
            pairs = _sequence_pairs()
            default_timing = {'transition': 'cancel', 'delay_frames': 0, 'random_min_frames': 1, 'random_max_frames': 4}
            if auto_jump:
                pairs.append((_jump_step(), copy.deepcopy(default_timing) if pairs else None))
            if kind in {'jump', 'super_jump'}:
                step = _jump_step(super_jump=(kind == 'super_jump'))
            else:
                step = {
                    'move_label': selected,
                    'action_id': action_id,
                    'kind': 'move',
                    'transition': 'cancel',
                    'delay_frames': 0,
                    'random_min_frames': 1,
                    'random_max_frames': 4,
                }
            pairs.append((step, copy.deepcopy(default_timing) if pairs else None))
            _restore_pairs(pairs)
            select_idx = len(sequence) - 1
            _refresh_sequence(select_idx)
            _apply()
            if auto_jump:
                status.configure(text=f'Inserted Jump before {label}. Both steps default to Cancel ASAP.')

        def _remove_step() -> None:
            idx = _selected_index()
            if idx is None:
                return
            pairs = _sequence_pairs()
            pairs.pop(idx)
            _restore_pairs(pairs)
            _refresh_sequence(min(idx, len(sequence) - 1) if sequence else None)
            _apply()

        def _move_step(delta: int) -> None:
            idx = _selected_index()
            if idx is None:
                return
            new_idx = idx + delta
            if not (0 <= new_idx < len(sequence)):
                return
            pairs = _sequence_pairs()
            pairs[idx], pairs[new_idx] = pairs[new_idx], pairs[idx]
            _restore_pairs(pairs)
            _refresh_sequence(new_idx)
            _apply()

        def _clear_sequence() -> None:
            sequence.clear()
            _refresh_sequence()
            _apply()

        def _sequence_selected(_event=None) -> None:
            # Selection only changes which row the timing buttons edit.
            _refresh_transition_controls()

        def _set_selected_timing(mode: str, *, delay: int = 0, low: int = 1, high: int = 4) -> None:
            idx = _selected_index()
            if idx is None or not (0 <= idx < len(sequence)):
                status.configure(text='Select a sequence move first.')
                return
            step = sequence[idx]
            if idx == 0:
                status.configure(text='The first move starts the sequence. Use the round countdown to delay it.')
                return
            if low > high:
                low, high = high, low
            payload = {
                'transition': mode,
                'delay_frames': _clamp_frames(delay, 0),
                'random_min_frames': _clamp_frames(low, 1),
                'random_max_frames': _clamp_frames(high, 4),
            }
            _set_entry_timing(idx, payload)
            _refresh_sequence(idx)
            _apply()
            if mode == 'delay':
                status.configure(text=f"{step.get('move_label', 'Move')}: delayed {payload['delay_frames']}F.")
            elif mode == 'random_delay':
                status.configure(text=f"{step.get('move_label', 'Move')}: random delay {payload['random_min_frames']}-{payload['random_max_frames']}F, rerolled each repetition.")
            else:
                status.configure(text=f"{step.get('move_label', 'Move')}: Cancel ASAP.")

        def _timing_cancel() -> None:
            _set_selected_timing('cancel', delay=0)

        def _timing_fixed() -> None:
            idx = _selected_index()
            if idx is None or not (0 <= idx < len(sequence)):
                status.configure(text='Select a sequence move first.')
                return
            if idx == 0:
                status.configure(text='The first move starts the sequence. Use the round countdown to delay it.')
                return
            timing = _entry_timing(idx) or {}
            current = _clamp_frames(timing.get('delay_frames'), 0)
            value = simpledialog.askinteger(
                'Delay next move',
                'Delay this move by how many frames?',
                parent=win,
                initialvalue=current,
                minvalue=0,
                maxvalue=180,
            )
            if value is None:
                return
            _set_selected_timing('delay', delay=value)

        def _timing_random() -> None:
            idx = _selected_index()
            if idx is None or not (0 <= idx < len(sequence)):
                status.configure(text='Select a sequence move first.')
                return
            step = sequence[idx]
            if idx == 0:
                status.configure(text='The first move starts the sequence. Use the round countdown to delay it.')
                return
            timing = _entry_timing(idx) or {}
            low = simpledialog.askinteger(
                'Random delay',
                'Minimum delay before this move:',
                parent=win,
                initialvalue=_clamp_frames(timing.get('random_min_frames'), 1),
                minvalue=0,
                maxvalue=180,
            )
            if low is None:
                return
            high = simpledialog.askinteger(
                'Random delay',
                'Maximum delay before this move:',
                parent=win,
                initialvalue=max(low, _clamp_frames(timing.get('random_max_frames'), max(1, low))),
                minvalue=low,
                maxvalue=180,
            )
            if high is None:
                return
            _set_selected_timing('random_delay', low=low, high=high)

        def _save_preset() -> None:
            name = _clean(preset_name_var.get())[:48]
            if not name:
                status.configure(text='Give the preset a name first.')
                return
            if not sequence:
                status.configure(text='Build a sequence before saving a preset.')
                return
            presets[name] = copy.deepcopy(sequence)
            preset_var.set(name)
            preset_name_var.set('')
            _refresh_presets()
            _apply(reset_runtime=False)
            status.configure(text=f'Saved preset: {name}')

        def _load_preset() -> None:
            name = preset_var.get()
            saved = presets.get(name)
            if not saved:
                return
            sequence[:] = copy.deepcopy(saved)
            _refresh_sequence(0)
            _apply()
            status.configure(text=f'Loaded preset: {name}')

        def _delete_preset() -> None:
            name = preset_var.get()
            if name in presets:
                presets.pop(name, None)
                _refresh_presets()
                _apply(reset_runtime=False)
                status.configure(text=f'Deleted preset: {name}')

        def _toggle() -> None:
            turning_on = not bool(enabled_var.get())
            if turning_on and not sequence and not (randomize_presets_var.get() and presets):
                status.configure(text='Add at least one move or enable a saved preset set before arming.')
                return
            enabled_var.set(turning_on)
            _apply(reset_runtime=True)

        def _manual_test() -> None:
            if not sequence:
                status.configure(text='Build a sequence first.')
                return
            _apply(reset_runtime=False)
            state['manual_test_requested'] = True
            status.configure(text='Perform Once queued. It will wait until the dummy can accept the first action.')

        def _reset() -> None:
            enabled_var.set(False)
            slot_var.set('P2-C1')
            move_var.set('Choose move')
            sequence.clear()
            cooldown_var.set('3')
            random_interval_var.set(False)
            random_min_var.set('5')
            random_max_var.set('10')
            countdown_var.set(False)
            randomize_presets_var.set(False)
            auto_add_jump_var.set(True)
            new_step_defaults.update({'transition': 'cancel', 'delay_frames': 0, 'random_min_frames': 1, 'random_max_frames': 4})
            _refresh_sequence()
            _refresh_moves(preserve=False)
            _apply(reset_runtime=True)
            status.configure(text='Trainer defaults restored. Saved presets were kept.')

        def _slot_changed(_event=None) -> None:
            nonlocal roster
            roster = _read_roster()
            _refresh_moves(preserve=False)
            _apply(reset_runtime=True)

        def _timing_changed(_event=None) -> None:
            _refresh_timing_controls()
            _apply(reset_runtime=True)

        # Wire controls.
        arm_btn.configure(command=_toggle)
        test_btn.configure(command=_manual_test)
        reset_btn.configure(command=_reset)
        add_btn.configure(command=_add_move)
        remove_btn.configure(command=_remove_step)
        up_btn.configure(command=lambda: _move_step(-1))
        down_btn.configure(command=lambda: _move_step(1))
        clear_btn.configure(command=_clear_sequence)
        save_preset_btn.configure(command=_save_preset)
        load_btn.configure(command=_load_preset)
        delete_btn.configure(command=_delete_preset)
        random_interval_check.configure(command=_timing_changed)
        randomize_presets_check.configure(command=lambda: _apply(reset_runtime=True))
        countdown_check.configure(command=lambda: _apply(reset_runtime=False))
        step_timeout_spin.bind('<FocusOut>', lambda _event: _apply(reset_runtime=False), add='+')
        step_timeout_spin.bind('<Return>', lambda _event: _apply(reset_runtime=False), add='+')
        auto_jump_check.configure(command=lambda: _apply(reset_runtime=False))
        slot_combo.bind('<<ComboboxSelected>>', _slot_changed)
        sequence_tree.bind('<<TreeviewSelect>>', _sequence_selected)
        cancel_timing_btn.configure(command=_timing_cancel)
        fixed_timing_btn.configure(command=_timing_fixed)
        random_timing_btn.configure(command=_timing_random)
        for widget in (cooldown_spin, random_min_spin, random_max_spin):
            widget.bind('<FocusOut>', _timing_changed)
            widget.bind('<Return>', _timing_changed)

        roster = _read_roster()
        _refresh_moves(preserve=True)
        _refresh_sequence(0 if sequence else None)
        _refresh_presets()
        _refresh_timing_controls()
        _refresh_arm_button()
        _refresh_live()
        _sync_scrollregion()

        def _poll() -> None:
            nonlocal roster, last_roster_signature
            try:
                if not win.winfo_exists():
                    return
                fresh = _read_roster()
                entry = fresh.get(slot_var.get()) or {}
                signature = (
                    slot_var.get(),
                    int(entry.get('base') or 0),
                    _clean(entry.get('name')),
                    tuple((int(m.get('action_id') or 0), _clean(m.get('display'))) for m in (entry.get('moves') or []) if isinstance(m, dict)),
                )
                roster = fresh
                if signature != last_roster_signature:
                    last_roster_signature = signature
                    _refresh_moves(preserve=True)
                runtime_status = _clean(state.get('last_status'))
                if runtime_status:
                    status.configure(text=runtime_status)
                enabled_var.set(bool(state.get('enabled', False)))
                _refresh_arm_button()
                _refresh_live()
                win.after(150, _poll)
            except Exception:
                return

        def _close() -> None:
            global _OPEN_WINDOW
            _OPEN_WINDOW = None
            try:
                win.destroy()
            except Exception:
                pass

        win.protocol('WM_DELETE_WINDOW', _close)
        win.after(150, _poll)
        win.transient(None)
        win.lift()
        win.focus_force()

    if tk_call is not None:
        try:
            tk_call(_open)
            return
        except Exception:
            pass
    _open()


__all__ = ['open_punish_training_window']
