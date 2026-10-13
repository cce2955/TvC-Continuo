"""Compact, Continuo-themed music controls on the existing Tk host.

No global mouse-wheel trapping, no oversized scrolling form. Three small tabs;
playback status and Apply remain visible on 720p or scaled displays.
"""
from __future__ import annotations

import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

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
    win.geometry('735x520')
    win.minsize(575, 460)
    win.configure(bg=BG)
    win.columnconfigure(0, weight=1)
    win.rowconfigure(2, weight=1)

    st = controller.status()
    output = tk.StringVar(value=st['output'] if st['enabled'] else 'native')
    source = tk.StringVar(value=st['source'])
    stage_order = tk.StringVar(value=st['stage_order'])
    fighter_order = tk.StringVar(value=st['fighter_order'])
    fighter_switch = tk.StringVar(value=st['fighter_switch'])
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

    for name in ('Playback', 'Library', 'Advanced'):
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
    _label(stage_card, 'Stage track order', color=TEXT, bold=True).pack(anchor='w')
    _picklist(stage_card, stage_order,
              [('Sequential', 'sequential'), ('Random', 'random')]).pack(anchor='w', pady=(6, 0))
    fighter_card = _card(playback)
    fighter_card.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(0, 6))
    _label(fighter_card, 'Fighter track order', color=TEXT, bold=True).pack(anchor='w')
    _picklist(fighter_card, fighter_order,
              [('Sequential', 'sequential'), ('Random', 'random')]).pack(anchor='w', pady=(6, 0))

    fighter_opts = _card(playback)
    fighter_opts.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(0, 6))
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
    _label(follow, 'Per tag: either team tags, newest point takes over (P1 wins ties).', color=DIM,
           size=8).pack(side='left', padx=(10, 0))

    def source_changed(*_):
        if source.get() == 'fighter':
            stage_card.grid_remove()
            fighter_card.grid()
            fighter_opts.grid()
        else:
            stage_card.grid()
            fighter_card.grid_remove()
            fighter_opts.grid_remove()
    source.trace_add('write', source_changed)
    source_changed()

    volume_card = _card(playback)
    volume_card.grid(row=3, column=0, columnspan=2, sticky='ew')
    vol_head = tk.Frame(volume_card, bg=PANEL)
    vol_head.pack(fill='x')
    _label(vol_head, 'Custom track volume', color=TEXT, bold=True).pack(side='left')
    vol_read = _label(vol_head, '', color=BLUE)
    vol_read.pack(side='right')
    volume.trace_add('write', lambda *_: vol_read.configure(text=f'{round(volume.get())}%'))
    vol_read.configure(text=f'{round(volume.get())}%')
    tk.Scale(volume_card, from_=0, to=100, showvalue=False, orient='horizontal',
             variable=volume, bg=PANEL, troughcolor=PANEL3,
             activebackground=BLUE, highlightthickness=0, bd=0,
             sliderrelief='flat').pack(fill='x')

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
                action_message.set('Library location saved; existing songs were not moved.')
            except Exception as ex:
                show_error('Choose folder', ex)

    def default_folder():
        try:
            controller.set_music_root(controller.default_music_root)
            redraw_path()
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
    _label(acard, 'Auto identifies known menus and normal stages. Select a specific SSD for'
           ' Yami, Wi-Fi, endings, or other unmapped screens.',
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
            _auto_saving[0] = True
            controller.configure(mode=scene.get(), source=source.get(), slot=slot.get(),
                                 stage_order=stage_order.get(), fighter_order=fighter_order.get(),
                                 fighter_switch=fighter_switch.get(), volume=volume.get()/100,
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
    for control in (output, source, stage_order, fighter_order, fighter_switch,
                    slot, scene, stage, character, volume, resource):
        control.trace_add('write', schedule_apply)

    def verify():
        action_message.set(controller.retry_mute())

    _label(action_bar, 'Autosaved', color=GREEN, size=8).pack(side='left', padx=(0, 10))
    _button(action_bar, 'Next song', controller.skip).pack(side='left', padx=(0, 6))
    _button(action_bar, 'Check BGM', verify).pack(side='left')
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
            detected.set(f"Scene: {s['scene']}    Stage: {stage_label(s['stage'])}    Point: {s['points']}"
                         f"    Expected SSD: {(s.get('expected_ssd') or 'unmapped')}.ssd\n"
                         f"Music follows: {s.get('music_owner_team') or '—'} / {s.get('music_owner_fighter') or '—'}    "
                         f"Playlist: {s.get('playlist', '?')} ({s.get('available_tracks', 0)} tracks)    "
                         f"{clues}" + ('    [AMBIGUOUS]' if ev.get('ambiguous') else ''))
            win.after(400, refresh)
        except tk.TclError:
            pass

    refresh()


def open_window():
    tk_call(_show_window)
