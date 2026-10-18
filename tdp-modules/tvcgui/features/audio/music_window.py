"""Compact, Continuo-themed music controls on the existing Tk host.

No global mouse-wheel trapping, no oversized scrolling form. Three small tabs;
playback status and Apply remain visible on 720p or scaled displays.
"""
from __future__ import annotations

import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tvcgui.core.tk_host import tk_call
from tvcgui.core.constants import CHAR_NAMES
from tvcgui.features.audio.music_player import controller, STAGE_NAMES, stage_label, stage_id_from_label
from tvcgui.features.audio.music_catalog import RESOURCES, BY_KEY

_WINDOW = None
BG = '#060a11'
PANEL = '#0a111c'
PANEL3 = '#122138'
BORDER = '#2b3b4d'
TEXT = '#e8f0f8'
MUTED = '#9daec2'
DIM = '#65778d'
BLUE = '#35aadc'
GREEN = '#43c99c'
GOLD = '#e8b756'
RED = '#df5266'
FONT = ('Segoe UI', 9)


def _label(parent, text='', *, color=MUTED, size=9, bold=False, **kw):
    return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=color,
                    font=('Segoe UI', size, 'bold' if bold else 'normal'),
                    anchor='w', justify='left', **kw)


def _button(parent, text, command):
    return tk.Button(parent, text=text, command=command, bg=PANEL3, fg=TEXT,
                     activebackground='#24405a', activeforeground=TEXT,
                     relief='flat', bd=0, highlightthickness=1,
                     highlightbackground=BORDER, cursor='hand2', font=FONT,
                     padx=10, pady=5)


def _picklist(parent, variable, choices):
    group = tk.Frame(parent, bg=parent.cget('bg'))
    buttons = []
    for label, value in choices:
        b = _button(group, label, lambda v=value: variable.set(v))
        b.pack(side='left', padx=(0, 5))
        buttons.append((b, value))

    def update(*_):
        for b, value in buttons:
            selected = variable.get() == value
            b.configure(bg='#194257' if selected else PANEL3,
                        fg=TEXT if selected else MUTED,
                        highlightbackground=BLUE if selected else BORDER)

    variable.trace_add('write', update)
    update()
    return group


def _option(parent, variable, values, width=14):
    obj = tk.OptionMenu(parent, variable, *values)
    obj.configure(bg=PANEL3, fg=TEXT, relief='flat', borderwidth=0,
                  highlightthickness=1, highlightbackground=BORDER,
                  activebackground='#194257', activeforeground=TEXT,
                  font=FONT, width=width, anchor='w', padx=5)
    obj['menu'].configure(bg=PANEL3, fg=TEXT,
                          activebackground='#194257', activeforeground=TEXT,
                          font=FONT, tearoff=0)
    return obj


def _card(parent):
    return tk.Frame(parent, bg=PANEL, highlightthickness=1,
                    highlightbackground=BORDER, padx=12, pady=5)


def _parse_loop_time(value: str) -> float:
    """Accept seconds or mm:ss.s for a song position."""
    parts = value.strip().split(':')
    if len(parts) == 1:
        seconds = float(parts[0])
    elif len(parts) == 2 and parts[0].isdigit():
        remainder = float(parts[1])
        if not 0 <= remainder < 60:
            raise ValueError('Seconds after the colon must be below 60.')
        seconds = int(parts[0]) * 60 + remainder
    else:
        raise ValueError('Use seconds or mm:ss.s.')
    if not 0 <= seconds < float('inf'):
        raise ValueError('Use a nonnegative time.')
    return seconds


def _format_loop_time(seconds: float) -> str:
    minutes, remainder = divmod(seconds, 60)
    return f'{int(minutes)}:{remainder:06.3f}'.rstrip('0').rstrip('.')


def _loop_group(name: str) -> tuple[str, str, str]:
    parts = name.split('/')
    categories = {'stages': 'Stages', 'characters': 'Characters',
                  'menus': 'Menus', 'extras': 'Extras'}
    if parts[0] in categories:
        return categories[parts[0]], '/'.join(parts[1:-1]), parts[-1]
    if parts[0] == 'low_health' and len(parts) >= 4:
        kind = 'Stages' if parts[1] == 'stages' else 'Characters'
        return 'Low health', f'{kind} / {"/".join(parts[2:-1])}', parts[-1]
    return 'Legacy scenes', parts[0].replace('_', ' ').title(), parts[-1]


def _show_window(root):
    global _WINDOW
    if _WINDOW is not None:
        try:
            if _WINDOW.winfo_exists():
                _WINDOW.lift()
                _WINDOW.focus_force()
                return
        except tk.TclError:
            pass

    win = tk.Toplevel(root)
    _WINDOW = win
    win.title('Continuo  |  Custom Music')
    win.geometry('735x550')
    win.minsize(575, 490)
    win.configure(bg=BG)
    win.columnconfigure(0, weight=1)
    win.rowconfigure(2, weight=1)

    st = controller.status()
    output = tk.StringVar(value=st['output'] if st['enabled'] else 'native')
    source = tk.StringVar(value=st['source'])
    stage_order = tk.StringVar(value=st['stage_order'])
    fighter_order = tk.StringVar(value=st['fighter_order'])
    stage_end_mode = tk.StringVar(value=st.get('stage_end_mode', 'match'))
    fighter_end_mode = tk.StringVar(value=st.get('fighter_end_mode', 'match'))
    fighter_switch = tk.StringVar(value=st['fighter_switch'])
    tag_scope = tk.StringVar(value=st.get('tag_scope', 'both'))
    low_health_enabled = tk.BooleanVar(value=st.get('low_health_enabled', False))
    low_health_threshold = tk.StringVar(value=str(st.get('low_health_threshold', 30)).rstrip('0').rstrip('.')
                                        if isinstance(st.get('low_health_threshold', 30), float) else
                                        str(st.get('low_health_threshold', 30)))
    ignored_actions = set(st.get('ignored_point_actions', []))
    ignore_tag_out = tk.BooleanVar(value='tag_out' in ignored_actions)
    ignore_dhc = tk.BooleanVar(value='dhc' in ignored_actions)
    ignore_var = tk.BooleanVar(value='var' in ignored_actions)
    ignore_cross_counter = tk.BooleanVar(value='cross_counter' in ignored_actions)
    fighter_change_cooldown = tk.StringVar(value=str(st.get('fighter_change_cooldown', 0)))
    slot = tk.StringVar(value=st['slot'] if st['slot'] != 'stage_only' else 'P1-point')
    scene = tk.StringVar(value=st['mode'])
    stage = tk.StringVar(value='Auto' if st['stage_override'] is None else stage_label(st['stage_override']))
    character = tk.StringVar(value='auto' if st['character_override'] is None else str(st['character_override']))
    manual_key = st.get('resource_override') or ''
    manual_item = BY_KEY.get(manual_key)
    resource_group = tk.StringVar(value=(manual_item.category.title() if manual_item else 'Auto'))
    resource = tk.StringVar(value=manual_key)
    volume = tk.DoubleVar(value=round(st['volume'] * 100))
    mute_message = tk.StringVar()
    now_playing = tk.StringVar()
    action_message = tk.StringVar()
    folder = tk.StringVar(value=st['folder'])
    detected = tk.StringVar()

    head = tk.Frame(win, bg=BG, padx=13, pady=8)
    head.grid(row=0, column=0, sticky='ew')
    _label(head, 'CONTINUO  /  MUSIC', color=BLUE, size=14, bold=True).pack(side='left')
    badge = _label(head, '', size=9, bold=True)
    badge.pack(side='right')

    tab_bar = tk.Frame(win, bg=BG, padx=13)
    tab_bar.grid(row=1, column=0, sticky='ew')
    inner = tk.Frame(win, bg=BG, padx=13, pady=9)
    inner.grid(row=2, column=0, sticky='nsew')
    inner.columnconfigure(0, weight=1)
    inner.rowconfigure(0, weight=1)
    tabs = {}
    tab_buttons = {}

    def show_tab(name):
        for key, page in tabs.items():
            if key == name:
                page.grid(row=0, column=0, sticky='nsew')
            else:
                page.grid_remove()
        for key, button in tab_buttons.items():
            selected = key == name
            button.configure(bg='#194257' if selected else PANEL3,
                             highlightbackground=BLUE if selected else BORDER)

    for name in ('Playback', 'Rules', 'Library', 'Loops', 'Advanced'):
        tabs[name] = tk.Frame(inner, bg=BG)
        b = _button(tab_bar, name, lambda tab=name: show_tab(tab))
        b.pack(side='left', padx=(0, 7))
        tab_buttons[name] = b

    # Main page: music behavior in two short rows rather than one long form.
    playback = tabs['Playback']
    playback.columnconfigure(0, weight=1)
    playback.columnconfigure(1, weight=1)

    main = _card(playback)
    main.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 6))
    main.columnconfigure(0, weight=1)
    main.columnconfigure(1, weight=1)
    out_group = tk.Frame(main, bg=PANEL)
    out_group.grid(row=0, column=0, sticky='w')
    _label(out_group, 'Audio output', color=TEXT, bold=True).pack(anchor='w')
    _picklist(out_group, output, [('Native', 'native'), ('Custom', 'custom'),
                                  ('No BGM', 'no_bgm')]).pack(anchor='w', pady=(5, 0))
    src_group = tk.Frame(main, bg=PANEL)
    src_group.grid(row=0, column=1, sticky='w', padx=(10, 0))
    _label(src_group, 'Music source', color=TEXT, bold=True).pack(anchor='w')
    _picklist(src_group, source, [('Stage', 'stage'), ('Fighter', 'fighter')]).pack(
        anchor='w', pady=(5, 0))

    stage_card = _card(playback)
    stage_card.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(0, 6))
    stage_order_group = tk.Frame(stage_card, bg=PANEL)
    stage_order_group.pack(side='left', anchor='nw')
    _label(stage_order_group, 'Stage and menu order', color=TEXT, bold=True).pack(anchor='w')
    _picklist(stage_order_group, stage_order,
              [('Sequential', 'sequential'), ('Random', 'random')]).pack(anchor='w', pady=(6, 0))
    stage_end_group = tk.Frame(stage_card, bg=PANEL)
    stage_end_group.pack(side='left', anchor='nw', padx=(18, 0))
    _label(stage_end_group, 'Stage song ends', color=TEXT, bold=True).pack(anchor='w')
    _picklist(stage_end_group, stage_end_mode,
              [('Repeat until next match', 'match'), ('Next song', 'playlist')]).pack(
                  anchor='w', pady=(6, 0))
    fighter_card = _card(playback)
    fighter_card.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(0, 6))
    fighter_order_group = tk.Frame(fighter_card, bg=PANEL)
    fighter_order_group.pack(side='left', anchor='nw')
    _label(fighter_order_group, 'Fighter order', color=TEXT, bold=True).pack(anchor='w')
    _picklist(fighter_order_group, fighter_order,
              [('Sequential', 'sequential'), ('Random', 'random')]).pack(anchor='w', pady=(6, 0))
    fighter_end_group = tk.Frame(fighter_card, bg=PANEL)
    fighter_end_group.pack(side='left', anchor='nw', padx=(18, 0))
    _label(fighter_end_group, 'Fighter song ends', color=TEXT, bold=True).pack(anchor='w')
    _picklist(fighter_end_group, fighter_end_mode,
              [('Repeat until next match', 'match'), ('Next song', 'playlist')]).pack(
                  anchor='w', pady=(6, 0))

    fighter_opts = _card(playback)
    fighter_opts.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(0, 6))
    line = tk.Frame(fighter_opts, bg=PANEL)
    line.pack(fill='x')
    _label(line, 'Fighter changes', color=TEXT, bold=True).pack(side='left', padx=(0, 12))
    _picklist(line, fighter_switch,
              [('Per match', 'match'), ('Per tag', 'tag')]).pack(side='left')
    follow = tk.Frame(fighter_opts, bg=PANEL)
    follow.pack(fill='x', pady=(6, 0))
    _label(follow, 'Start with', color=MUTED).pack(side='left', padx=(0, 12))
    _option(follow, slot, ['P1-point', 'P2-point', 'P1-C1', 'P1-C2', 'P2-C1', 'P2-C2'],
            width=11).pack(side='left')
    _label(follow, 'Choose the opening side; fixed C1/C2 slots stay fixed.', color=DIM,
           size=8).pack(side='left', padx=(10, 0))
    scope_line = tk.Frame(fighter_opts, bg=PANEL)
    scope_line.pack(fill='x', pady=(6, 0))
    _label(scope_line, 'Tag music follows', color=MUTED).pack(side='left', padx=(0, 12))
    _picklist(scope_line, tag_scope,
              [('Selected side', 'selected'), ('Both sides', 'both')]).pack(side='left')
    _label(scope_line, 'Applies to Per tag with P1-point or P2-point.', color=DIM,
           size=8).pack(side='left', padx=(10, 0))

    def source_changed(*_):
        if source.get() == 'fighter':
            fighter_card.grid()
            fighter_opts.grid()
        else:
            fighter_card.grid_remove()
            fighter_opts.grid_remove()
    source.trace_add('write', source_changed)
    source_changed()

    volume_card = _card(playback)
    volume_card.grid(row=4, column=0, columnspan=2, sticky='ew')
    vol_head = tk.Frame(volume_card, bg=PANEL)
    vol_head.pack(fill='x')
    _label(vol_head, 'Custom track volume', color=TEXT, bold=True).pack(side='left')
    vol_read = _label(vol_head, '', color=BLUE)
    vol_read.pack(side='right')
    _button(vol_head, 'Change music folder…', lambda: choose_folder()).pack(side='right', padx=(0, 12))
    volume.trace_add('write', lambda *_: vol_read.configure(text=f'{round(volume.get())}%'))
    vol_read.configure(text=f'{round(volume.get())}%')
    tk.Scale(volume_card, from_=0, to=100, showvalue=False, orient='horizontal',
             variable=volume, bg=PANEL, troughcolor=PANEL3,
             activebackground=BLUE, highlightthickness=0, bd=0,
             sliderrelief='flat').pack(fill='x')

    rules = tabs['Rules']
    low_card = _card(rules)
    low_card.pack(fill='x', pady=(0, 8))
    tk.Checkbutton(low_card, text='Use low health music', variable=low_health_enabled,
                   bg=PANEL, fg=TEXT, activebackground=PANEL, activeforeground=TEXT,
                   selectcolor=PANEL3, font=FONT).pack(anchor='w')
    low_line = tk.Frame(low_card, bg=PANEL)
    low_line.pack(fill='x', pady=(4, 0))
    _label(low_line, 'Survivor health at or below', color=MUTED).pack(side='left')
    tk.Entry(low_line, textvariable=low_health_threshold, width=6, bg=PANEL3,
             fg=TEXT, insertbackground=TEXT).pack(side='left', padx=(8, 4))
    _label(low_line, '%', color=MUTED).pack(side='left')
    _label(low_card, 'Triggers when one teammate is KO and the other is at or below the threshold.',
           color=DIM, size=8).pack(anchor='w', pady=(5, 0))
    _label(low_card, 'Add songs under low_health/stages/<stage> or low_health/characters/<fighter>.',
           color=DIM, size=8).pack(anchor='w')

    action_card = _card(rules)
    action_card.pack(fill='x')
    _label(action_card, 'Fighter music changes', color=TEXT, bold=True).pack(anchor='w')
    _label(action_card, 'Ignore a verified action; unclassified point changes still follow the normal setting.',
           color=DIM, size=8).pack(anchor='w', pady=(4, 4))
    _label(action_card, 'Applies to Fighter source with Per tag and a P1/P2 point slot.',
           color=DIM, size=8).pack(anchor='w', pady=(0, 4))
    action_grid = tk.Frame(action_card, bg=PANEL)
    action_grid.pack(fill='x')
    for row, (label, variable) in enumerate((('Tag out', ignore_tag_out), ('DHC', ignore_dhc),
                                             ('VAR', ignore_var), ('Cross counter', ignore_cross_counter))):
        tk.Checkbutton(action_grid, text=f'Ignore {label}', variable=variable,
                       bg=PANEL, fg=TEXT, activebackground=PANEL, activeforeground=TEXT,
                       selectcolor=PANEL3, font=FONT).grid(row=row // 2, column=row % 2,
                                                            sticky='w', padx=(0, 20))
    cooldown_line = tk.Frame(action_card, bg=PANEL)
    cooldown_line.pack(fill='x', pady=(8, 0))
    _label(cooldown_line, 'Minimum time between fighter music changes', color=MUTED).pack(side='left')
    tk.Entry(cooldown_line, textvariable=fighter_change_cooldown, width=6, bg=PANEL3,
             fg=TEXT, insertbackground=TEXT).pack(side='left', padx=(8, 4))
    _label(cooldown_line, 'seconds (default 0)', color=DIM).pack(side='left')

    # Library: path can be changed without rebuilding a frozen EXE.
    lib = tabs['Library']
    card = _card(lib)
    card.pack(fill='x')
    _label(card, 'Music folder', color=TEXT, bold=True).pack(anchor='w')
    _label(card, 'Stored outside the EXE. Continuo creates the tree automatically.',
           color=MUTED).pack(anchor='w', pady=(5, 8))
    path_label = _label(card, color=BLUE, wraplength=555, textvariable=folder)
    path_label.pack(anchor='w', fill='x', pady=(0, 12))
    controls = tk.Frame(card, bg=PANEL)
    controls.pack(fill='x')

    def redraw_path():
        folder.set(str(controller.root))

    def show_error(title, ex):
        messagebox.showerror(title, str(ex), parent=win)

    def open_folder():
        try:
            path = controller.ensure_library()
            if hasattr(os, 'startfile'):
                os.startfile(str(path))
            else:
                messagebox.showinfo('Music folder', str(path), parent=win)
        except Exception as ex:
            show_error('Music folder', ex)

    def choose_folder():
        selected = filedialog.askdirectory(parent=win, initialdir=str(controller.root),
                                           title='Choose Continuo music folder')
        if selected:
            try:
                controller.set_music_root(selected)
                redraw_path()
                refresh_loop_tracks()
                action_message.set('Library location saved; existing songs were not moved.')
            except Exception as ex:
                show_error('Choose folder', ex)

    def default_folder():
        try:
            controller.set_music_root(controller.default_music_root)
            redraw_path()
            refresh_loop_tracks()
            action_message.set('Default music location selected.')
        except Exception as ex:
            show_error('Default folder', ex)

    def repair_tree():
        try:
            controller.ensure_library()
            action_message.set('Folder tree created / verified.')
        except Exception as ex:
            show_error('Music folders', ex)

    _button(controls, 'Open', open_folder).pack(side='left', padx=(0, 6))
    _button(controls, 'Choose location', choose_folder).pack(side='left', padx=(0, 6))
    _button(controls, 'Default', default_folder).pack(side='left', padx=(0, 6))
    _button(controls, 'Repair folders', repair_tree).pack(side='left')
    _label(card, f'{len(RESOURCES)} SSD resources  •  stages/Yami - First Form  •  menus/Wi-Fi Mode  •  '
           'menus/Extra Menu  •  extras/Ending 1  •  characters/',
           color=DIM, size=8, wraplength=550).pack(fill='x', pady=(12, 0))

    # Every playable custom song can have its own loop setting.
    loops = tabs['Loops']
    loop_card = _card(loops)
    loop_card.pack(fill='both', expand=True)
    _label(loop_card, 'Custom song loops', color=TEXT, bold=True).pack(anchor='w')
    _label(loop_card, 'Expand a music folder, then choose a song.',
           color=MUTED).pack(anchor='w', pady=(3, 2))
    _label(loop_card, 'Enter seconds or m:ss (e.g. 0:35 to 2:10). End jumps back to Start; blank End uses file end.',
           color=MUTED).pack(anchor='w', pady=(0, 5))
    _label(loop_card, 'OGG and MP3 can seek; MP3 timing may vary. WAV loops can start at 0 only.',
           color=DIM, size=8).pack(anchor='w', pady=(0, 5))
    loop_list_frame = tk.Frame(loop_card, bg=PANEL)
    loop_list_frame.pack(fill='both', expand=True)
    loop_style = ttk.Style(win)
    loop_style.configure('ContinuoLoops.Treeview', background=PANEL3, fieldbackground=PANEL3,
                         foreground=TEXT, rowheight=22, font=FONT, borderwidth=0)
    loop_style.map('ContinuoLoops.Treeview', background=[('selected', '#194257')],
                   foreground=[('selected', TEXT)])
    loop_tree = ttk.Treeview(loop_list_frame, show='tree', selectmode='browse',
                             style='ContinuoLoops.Treeview', height=9)
    loop_tree.pack(side='left', fill='both', expand=True)
    loop_scroll = tk.Scrollbar(loop_list_frame, command=loop_tree.yview)
    loop_scroll.pack(side='right', fill='y')
    loop_tree.configure(yscrollcommand=loop_scroll.set)
    loop_start = tk.StringVar(value='0:00')
    loop_end = tk.StringVar(value='')
    first_play = tk.StringVar(value='Song beginning')
    loop_message = tk.StringVar(value='')
    song_length_text = tk.StringVar(value='Song length: select a song')
    song_length = [None]
    duration_cache = {}
    duration_results = queue.Queue()
    duration_worker = [None]
    preview_serial = [0]

    def selected_loop_name():
        chosen = loop_tree.selection()
        return chosen[0][5:] if chosen and chosen[0].startswith('song:') else None

    def show_song_length():
        name = selected_loop_name()
        song_length[0] = None
        if not name:
            song_length_text.set('Song length: select a song')
            return
        key = (str(controller.root), name)
        if key in duration_cache:
            duration, error = duration_cache[key]
            song_length[0] = duration
            song_length_text.set(f'Song length: {_format_loop_time(duration)}' if duration is not None
                                 else f'Song length unavailable: {error}')
            return
        song_length_text.set('Song length: reading...')
        if duration_worker[0] is not None:
            return

        def read_duration():
            try:
                result = (controller.loop_duration(name), None)
            except Exception as ex:
                result = (None, str(ex))
            duration_results.put((key, result))

        duration_worker[0] = threading.Thread(target=read_duration, daemon=True)
        duration_worker[0].start()

        def collect_duration():
            try:
                completed_key, result = duration_results.get_nowait()
            except queue.Empty:
                if win.winfo_exists():
                    win.after(80, collect_duration)
                return
            duration_cache[completed_key] = result
            duration_worker[0] = None
            if win.winfo_exists():
                show_song_length()

        win.after(80, collect_duration)

    def load_loop_choice(*_):
        name = selected_loop_name()
        setting = controller.loop_setting(name) if name else None
        loop_start.set(_format_loop_time(setting['start']) if setting else '0:00')
        loop_end.set(_format_loop_time(setting['end'])
                     if setting and setting['end'] is not None else '')
        first_play.set('Loop start' if setting and setting['first'] == 'loop'
                       else 'Song beginning')
        loop_message.set(name or 'Select a song inside a folder.')
        show_song_length()

    def refresh_loop_tracks():
        selected = selected_loop_name()
        opened = {item for category in loop_tree.get_children()
                  for item in loop_tree.get_children(category) if loop_tree.item(item, 'open')}
        loop_tree.delete(*loop_tree.get_children())
        names = controller.loop_tracks()
        for name in names:
            category, folder, filename = _loop_group(name)
            category_id = f'category:{category}'
            folder_id = f'folder:{category}/{folder}'
            if not loop_tree.exists(category_id):
                loop_tree.insert('', 'end', iid=category_id, text=category, open=True)
            if not loop_tree.exists(folder_id):
                loop_tree.insert(category_id, 'end', iid=folder_id, text=folder, open=folder_id in opened)
            marked = '[LOOP] ' if controller.loop_setting(name) else ''
            loop_tree.insert(folder_id, 'end', iid=f'song:{name}', text=marked + filename)
        if names:
            song_id = f'song:{selected if selected in names else names[0]}'
            loop_tree.selection_set(song_id)
            loop_tree.see(song_id)
        load_loop_choice()

    def save_loop():
        name = selected_loop_name()
        if not name:
            loop_message.set('Select a custom song first.')
            return
        try:
            controller.set_loop_setting(
                name, start=_parse_loop_time(loop_start.get()),
                end=_parse_loop_time(loop_end.get()) if loop_end.get().strip() else None,
                first='loop' if first_play.get() == 'Loop start' else 'beginning')
            refresh_loop_tracks()
            loop_message.set(f'Loop saved for {name}.')
        except (ValueError, OSError) as ex:
            loop_message.set(str(ex))

    def clear_loop():
        name = selected_loop_name()
        if name:
            controller.set_loop_setting(name)
            refresh_loop_tracks()
            loop_message.set(f'Loop removed for {name}.')

    def preview_loop():
        name = selected_loop_name()
        if not name:
            loop_message.set('Select a custom song first.')
            return
        try:
            before, boundary, repeat, after = controller.preview_loop(
                name, start=_parse_loop_time(loop_start.get()),
                end=_parse_loop_time(loop_end.get()) if loop_end.get().strip() else None)
        except Exception as ex:
            loop_message.set(f'Preview unavailable: {ex!s}')
            return
        preview_serial[0] += 1
        serial = preview_serial[0]
        seam = (f'{_format_loop_time(before)}–{_format_loop_time(boundary)} '
                f'→ {_format_loop_time(repeat)}–{_format_loop_time(after)}')
        loop_message.set(f'Previewing {seam} using the current fields. Save loop to keep them.')

        def check_preview():
            try:
                if not win.winfo_exists() or serial != preview_serial[0]:
                    return
                if controller.loop_preview_playing():
                    win.after(150, check_preview)
                else:
                    loop_message.set(f'Preview finished: {seam}')
            except tk.TclError:
                pass

        win.after(150, check_preview)

    def stop_preview():
        preview_serial[0] += 1
        controller.stop_loop_preview()
        loop_message.set('Preview stopped. Loop fields are unchanged.')

    loop_tree.bind('<<TreeviewSelect>>', load_loop_choice)
    loop_fields = tk.Frame(loop_card, bg=PANEL)
    loop_fields.pack(fill='x', pady=(8, 0), before=loop_list_frame)
    _label(loop_fields, 'Loop start', color=TEXT).pack(side='left')
    start_entry = tk.Entry(loop_fields, textvariable=loop_start, width=9, bg=PANEL3,
                           fg=TEXT, insertbackground=TEXT)
    start_entry.pack(side='left', padx=(6, 14))
    _label(loop_fields, 'Loop end', color=TEXT).pack(side='left')
    end_entry = tk.Entry(loop_fields, textvariable=loop_end, width=9, bg=PANEL3,
                         fg=TEXT, insertbackground=TEXT)
    end_entry.pack(side='left', padx=(6, 14))
    _label(loop_fields, 'First play from', color=TEXT).pack(side='left')
    _option(loop_fields, first_play, ['Song beginning', 'Loop start'], width=14).pack(
        side='left', padx=(6, 0))
    song_length_row = tk.Frame(loop_card, bg=PANEL)
    song_length_row.pack(fill='x', pady=(5, 0), before=loop_list_frame)
    _label(song_length_row, '', textvariable=song_length_text, color=BLUE).pack(side='left')

    def use_song_end():
        if song_length[0] is None:
            loop_message.set('Choose a song and wait for its length to appear.')
            return
        loop_end.set(_format_loop_time(song_length[0]))
        loop_message.set('Song length copied to Loop end. Adjust it, then save or preview.')

    _button(song_length_row, 'Use song end', use_song_end).pack(side='left', padx=(12, 0))
    loop_buttons = tk.Frame(loop_card, bg=PANEL)
    loop_buttons.pack(fill='x', pady=(8, 0), before=loop_list_frame)
    _button(loop_buttons, 'Save loop', save_loop).pack(side='left', padx=(0, 6))
    _button(loop_buttons, 'Preview loop', preview_loop).pack(side='left', padx=(0, 6))
    _button(loop_buttons, 'Stop preview', stop_preview).pack(side='left', padx=(0, 6))
    _button(loop_buttons, 'Remove loop', clear_loop).pack(side='left', padx=(0, 6))
    _button(loop_buttons, 'Refresh songs',
            lambda: (duration_cache.clear(), refresh_loop_tracks())).pack(side='left')
    _label(loop_card, '', color=BLUE, textvariable=loop_message, wraplength=660).pack(
        anchor='w', pady=(6, 6), before=loop_list_frame)
    start_entry.bind('<Return>', lambda _event: save_loop())
    end_entry.bind('<Return>', lambda _event: save_loop())
    refresh_loop_tracks()

    def close_window():
        controller.stop_loop_preview()
        win.destroy()

    win.protocol('WM_DELETE_WINDOW', close_window)

    # Advanced: optional overrides only. Most players never need this tab.
    adv = tabs['Advanced']
    acard = _card(adv)
    acard.pack(fill='x')
    _label(acard, 'Detection overrides', color=TEXT, bold=True).pack(anchor='w')
    _label(acard, 'Keep Auto unless testing an unrecognized stage or fighter.',
           color=MUTED).pack(anchor='w', pady=(3, 12))
    fields = tk.Frame(acard, bg=PANEL)
    fields.pack(fill='x')
    for col in range(3):
        fields.columnconfigure(col, weight=1)
    scene_col = tk.Frame(fields, bg=PANEL)
    scene_col.grid(row=0, column=0, sticky='w')
    _label(scene_col, 'Scene').pack(anchor='w')
    _option(scene_col, scene, ['auto', 'menu', 'character_select', 'stage_select', 'battle'],
            width=13).pack(anchor='w', pady=(4, 0))
    stage_col = tk.Frame(fields, bg=PANEL)
    stage_col.grid(row=0, column=1, sticky='w')
    _label(stage_col, 'Stage').pack(anchor='w')
    _option(stage_col, stage, ['Auto'] + [STAGE_NAMES[i] for i in sorted(STAGE_NAMES)],
            width=24).pack(anchor='w', pady=(4, 0))
    fighter_col = tk.Frame(fields, bg=PANEL)
    fighter_col.grid(row=0, column=2, sticky='w')
    _label(fighter_col, 'Fighter ID').pack(anchor='w')
    _option(fighter_col, character, ['auto'] + [str(n) for n in sorted(CHAR_NAMES)],
            width=11).pack(anchor='w', pady=(4, 0))
    _label(acard, 'Auto follows detected menus, the winner screen, and stage IDs including Yami.'
           ' Select a specific SSD for Wi-Fi, endings, or other screens without a live boundary.',
           color=DIM, size=8, wraplength=565).pack(anchor='w', pady=(9, 3))
    manual = tk.Frame(acard, bg=PANEL)
    manual.pack(fill='x', pady=(3, 0))
    _label(manual, 'SSD library', color=TEXT, bold=True).pack(side='left', padx=(0, 10))
    group_picker = _option(manual, resource_group, ['Auto', 'Stages', 'Menus', 'Extras'], width=9)
    group_picker.pack(side='left', padx=(0, 10))
    resource_label = tk.StringVar(value='Auto (detected scenes)')
    item_display = {entry.key: f'{entry.label} ({entry.key}.ssd)' for entry in RESOURCES}
    display_item = {text: key for key, text in item_display.items()}
    resource_picker = _option(manual, resource_label, ['Auto (detected scenes)'], width=33)
    resource_picker.pack(side='left')

    def select_resource_group(*_):
        category = resource_group.get().lower()
        allowed = [entry for entry in RESOURCES if entry.category == category]
        menu = resource_picker['menu']
        menu.delete(0, 'end')
        menu.add_command(label='Auto (detected scenes)',
                         command=lambda: resource_label.set('Auto (detected scenes)'))
        for entry in allowed:
            text = item_display[entry.key]
            menu.add_command(label=text, command=lambda v=text: resource_label.set(v))
        resource_label.set(item_display.get(resource.get(), 'Auto (detected scenes)')
                           if resource.get() in {entry.key for entry in allowed}
                           else 'Auto (detected scenes)')

    def select_resource(*_):
        value = display_item.get(resource_label.get(), '')
        if value != resource.get():
            resource.set(value)

    resource_group.trace_add('write', select_resource_group)
    resource_label.trace_add('write', select_resource)
    select_resource_group()
    detection_card = _card(adv)
    detection_card.pack(fill='x', pady=(8, 0))
    _label(detection_card, 'Detected game state', color=TEXT, bold=True).pack(anchor='w')
    _label(detection_card, '', color=BLUE, textvariable=detected,
           wraplength=530).pack(anchor='w', pady=(6, 0))

    show_tab('Playback')

    # Permanent footer: never scroll to locate playback status or Apply.
    footer = tk.Frame(win, bg=BG, padx=13, pady=4)
    footer.grid(row=3, column=0, sticky='ew')
    _label(footer, '', color=GOLD, textvariable=mute_message, wraplength=670).pack(
        anchor='w', fill='x')
    _label(footer, '', color=BLUE, textvariable=now_playing, wraplength=670).pack(
        anchor='w', fill='x')
    _label(footer, '', color=MUTED, textvariable=action_message, wraplength=670).pack(
        anchor='w', fill='x')

    action_bar = tk.Frame(win, bg=BG, padx=13, pady=6)
    action_bar.grid(row=4, column=0, sticky='ew')

    _apply_timer = [None]
    _auto_saving = [False]

    def apply():
        # Every UI control saves itself. The value-entry boxes are intentionally
        # validated only after the user finishes typing a legal ID.
        if _auto_saving[0]:
            return
        try:
            for title, value in (('Fighter ID', character.get()),):
                raw = value.strip().lower()
                if raw not in ('auto', '') and not 0 <= int(raw, 16 if raw.startswith('0x') else 10) <= 255:
                    action_message.set(f'{title}: enter Auto or 0–255; other choices are saved.')
                    return
            threshold = float(low_health_threshold.get())
            cooldown = float(fighter_change_cooldown.get())
            if not 1 <= threshold <= 100 or not 0 <= cooldown <= 300:
                action_message.set('Low health must be 1–100%; cooldown must be 0–300 seconds.')
                return
            _auto_saving[0] = True
            controller.configure(mode=scene.get(), source=source.get(), slot=slot.get(),
                                 stage_order=stage_order.get(), fighter_order=fighter_order.get(),
                                 stage_end_mode=stage_end_mode.get(),
                                 fighter_end_mode=fighter_end_mode.get(),
                                 fighter_switch=fighter_switch.get(), tag_scope=tag_scope.get(),
                                 low_health_enabled=low_health_enabled.get(),
                                 low_health_threshold=threshold,
                                 ignored_point_actions=[name for name, variable in (
                                     ('tag_out', ignore_tag_out), ('dhc', ignore_dhc),
                                     ('var', ignore_var), ('cross_counter', ignore_cross_counter))
                                     if variable.get()],
                                 fighter_change_cooldown=cooldown,
                                 volume=volume.get()/100,
                                 stage=stage_id_from_label(stage.get()) or 'auto',
                                 character=character.get() or 'auto',
                                 resource=resource.get())
            ok = controller.set_output_mode(output.get())
            action_message.set(controller.status()['message'] if ok else
                               'Preference saved. Waiting for Dolphin or music files.')
        except Exception as ex:
            action_message.set(f'Could not update music: {ex!s}')
        finally:
            _auto_saving[0] = False

    def schedule_apply(*_):
        if _auto_saving[0]:
            return
        if _apply_timer[0] is not None:
            try:
                win.after_cancel(_apply_timer[0])
            except tk.TclError:
                pass
        _apply_timer[0] = win.after(130, apply)

    # Segmented choices and volume take effect without pressing Apply.
    for control in (output, source, stage_order, fighter_order,
                    stage_end_mode, fighter_end_mode, fighter_switch, tag_scope,
                    low_health_enabled, low_health_threshold, ignore_tag_out,
                    ignore_dhc, ignore_var, ignore_cross_counter, fighter_change_cooldown,
                    slot, scene, stage, character, volume, resource):
        control.trace_add('write', schedule_apply)

    def verify():
        action_message.set(controller.retry_mute())

    def inspect_bgm():
        report = controller.bgm_diagnostic()
        popup = tk.Toplevel(win)
        popup.title('Continuo / AX BGM inspection')
        popup.configure(bg=BG)
        popup.geometry('760x510')
        _label(popup, 'Read-only AX voice inspection', color=BLUE, bold=True).pack(
            anchor='w', padx=12, pady=(10, 4))
        _label(popup, 'Capture this window during a match with audible native music.',
               color=MUTED).pack(anchor='w', padx=12, pady=(0, 6))
        report_box = tk.Text(popup, bg=PANEL, fg=TEXT, font=('Consolas', 9),
                             wrap='none', relief='flat', padx=10, pady=8)
        report_box.insert('1.0', report)
        report_box.configure(state='disabled')
        report_box.pack(fill='both', expand=True, padx=12, pady=(0, 12))

    _label(action_bar, 'Autosaved', color=GREEN, size=8).pack(side='left', padx=(0, 10))
    _button(action_bar, 'Next song', controller.skip).pack(side='left', padx=(0, 6))
    _button(action_bar, 'Check BGM', verify).pack(side='left')
    _button(action_bar, 'Inspect BGM', inspect_bgm).pack(side='left', padx=(6, 0))
    _label(action_bar, 'SSD override is manual for unverified scenes.', color=DIM,
           size=8).pack(side='right')

    def refresh():
        try:
            if not win.winfo_exists():
                return
            s = controller.status()
            badge.configure(text='CUSTOM' if s['enabled'] and s['output'] == 'custom'
                            else 'NO BGM' if s['enabled'] else 'NATIVE',
                            fg=GREEN if s['enabled'] and s['bgm_verified'] else
                               GOLD if s['enabled'] else MUTED)
            mute_message.set('Native BGM: ' + s['bgm_status'] + ('  [fallback]' if s.get('native_fallback') else ''))
            now_playing.set('Now playing: ' + (Path(s['track']).name if s['track'] else '—'))
            ev = s.get('scene_evidence') or {}
            clues = (f"Battle phase: {ev.get('native_phase', 'n/a')}  "
                     f"Stage UI: {int(bool(ev.get('stage_cursor')))}  "
                     f"Character asset: {int(bool(ev.get('select_asset')))}")
            identity = s.get('stage_identity', 'unverified')
            expected = s.get('expected_ssd')
            menu_asset = s.get('menu_asset_id')
            menu_cue = (f'page {menu_asset:03d}' if menu_asset is not None else
                        'root only' if ev.get('menu_object') else 'none')
            follow_id = s.get('music_owner_fighter')
            follow_name = CHAR_NAMES.get(follow_id, 'not verified') if follow_id else 'not verified'
            scope_label = 'both sides' if s.get('tag_scope') == 'both' else s.get('slot')
            detected.set(f"Scene: {s['scene']}    Stage: {stage_label(s['stage'])}    "
                         f"Identity: {identity}  (raw: {s.get('raw_stage', '—')})\n"
                         f"Following: {scope_label} / {follow_name} ({follow_id or '—'})    "
                         f"Point flags: {s['points']}\n"
                         f"SSD expectation: {(expected + '.ssd') if expected else 'UNVERIFIED'}    "
                         f"Playlist: {s.get('playlist', '?')} ({s.get('available_tracks', 0)} files)    "
                         f"Menu cue: {menu_cue}    "
                         f"KO/end phase seen: {int(bool(s.get('match_end_seen')))}    "
                         f"Next match pending: {int(bool(s.get('boundary_pending')))}\n"
                         f"{clues}" + ('    [AMBIGUOUS]' if ev.get('ambiguous') else ''))
            win.after(400, refresh)
        except tk.TclError:
            pass

    refresh()


def open_window():
    tk_call(_show_window)
