"""Read-only capture controls embedded in the existing Research tab."""
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from tvcgui.features.combat.research_capture import CaptureService, SLOT_ADDRESSES, unavailable_fields

_BACKGROUND = '#0E1728'
_TEXT = '#DCE7F7'
_ACCENT = '#9FC1F0'
_CONTROL_BACKGROUND = '#142035'


def _field_text(fields, name):
    """Display raw sentinels and the reason for an unavailable field."""
    item = fields.get(name, {})
    value = item.get('value')
    if value is None:
        return item.get('status', 'unavailable')
    if item.get('status') == 'raw 0xFFFFFFFF sentinel candidate':
        return '0xFFFFFFFF (sentinel candidate)'
    return str(value)


def _timeline_text(event):
    """Format an observation without assigning meaning to its raw values."""
    if event.get('type') == 'music_operation':
        operation = event.get('operation') or {}
        return (f"{event.get('timestamp_utc', '')[11:23]} music {operation.get('operation')} "
                f"{operation.get('result', '')} {operation.get('track') or ''} "
                f"{operation.get('reason') or operation.get('detail') or operation.get('value', '')} {operation.get('error', '')}")
    if event.get('type') == 'music_operation_gap':
        return f"{event.get('timestamp_utc', '')[11:23]} music history lost {event.get('lost_count')} operations"
    result = (
        f"{event.get('timestamp_utc', '')[11:23]} {event.get('slot')} "
        f"{event.get('type')} {event.get('field', '')}: "
        f"{event.get('old_value')} → {event.get('new_value')}"
    )
    if event.get('old_character_id') != event.get('character_id') and event.get('type') == 'character_changed':
        result += f" character {event.get('old_character_id')} → {event.get('character_id')}"
    if event.get('reused_address_with_changed_character'):
        result += f" | reused actor: character {event.get('last_valid_character_id')} → {event.get('character_id')}"
    if event.get('provisional_label'):
        result += ' | ' + event['provisional_label']
    return result


def _live_slot_text(slot, sample):
    """Format one independently sampled fighter without inventing missing data."""
    if not sample:
        return f'{slot}: waiting for a sample'
    fields = sample.get('fields', {})
    value = lambda name: _field_text(fields, name)
    actor = sample.get('actor')
    actor_text = f'0x{actor:08X}' if isinstance(actor, int) else 'unavailable'
    stamp = sample.get('timestamp_utc', '')
    return (f"{slot} {stamp[11:23]} | {sample.get('status', 'unavailable')} | actor {actor_text} "
            f"ID {value('character_id')} HP {value('current_hp')}/{value('max_hp')} "
            f"act {value('action')}/{value('action_secondary')}/{value('pending_action')} "
            f"KO {value('ko_flag')} input {value('input_current')}/{value('input_pressed')}/{value('input_released')} "
            f"assist +44A4/+44B8 {value('assist_44a4_raw')}/{value('assist_timer_raw')}")


class ResearchCapturePanel:
    """Present read-only capture controls inside the existing Research tab."""

    def __init__(self, parent, initial_slot, hud_provider=None):
        self.service = CaptureService(hud_provider=hud_provider)
        self.messages = queue.Queue()
        self.frame = tk.LabelFrame(parent, text='Fighter evidence capture (read-only)', bg=_BACKGROUND, fg=_TEXT, padx=5, pady=4)
        self.frame.pack(fill='x', padx=8, pady=(6, 0))
        bar = tk.Frame(self.frame, bg=_BACKGROUND)
        bar.pack(fill='x')
        self.slot = tk.StringVar(value=initial_slot if initial_slot in SLOT_ADDRESSES else 'P1-C1')
        self.service.set_slot(self.slot.get())
        combo = ttk.Combobox(bar, textvariable=self.slot, state='readonly', width=9, values=tuple(SLOT_ADDRESSES))
        combo.pack(side='left')
        combo.bind('<<ComboboxSelected>>', lambda _event: self.service.set_slot(self.slot.get()))
        self.all_button = tk.Button(bar, text='Start all four', command=self.toggle_all, relief='flat', padx=7)
        self.all_button.pack(side='left', padx=3)
        self.experiment_button = tk.Button(bar, text='Start experiments', command=self.start_experiments, relief='flat', padx=7)
        self.experiment_button.pack(side='left', padx=3)
        for label, action in [('Start slot', self.start), ('Stop', self.service.stop), ('Clear', self.service.clear), ('Export JSONL + CSV', self.export)]:
            tk.Button(bar, text=label, command=action, relief='flat', padx=7).pack(side='left', padx=3)
        options = tk.Frame(self.frame, bg=_BACKGROUND)
        options.pack(fill='x')
        self.advanced = tk.BooleanVar(value=False)
        tk.Checkbutton(options, text='Advanced assist + experimental fields', variable=self.advanced,
                       command=lambda: self.service.set_advanced(self.advanced.get()), bg=_BACKGROUND, fg=_TEXT,
                       selectcolor=_CONTROL_BACKGROUND, activebackground=_BACKGROUND).pack(side='left')
        self.trace = tk.BooleanVar(value=False)
        tk.Checkbutton(options, text='Read existing damage mailbox (separate; no hooks installed)', variable=self.trace,
                       command=lambda: self.service.set_trace(self.trace.get()), bg=_BACKGROUND, fg=_TEXT,
                       selectcolor=_CONTROL_BACKGROUND, activebackground=_BACKGROUND).pack(side='left')
        self.status = tk.StringVar(value='Stopped.')
        tk.Label(self.frame, textvariable=self.status, bg=_BACKGROUND, fg=_ACCENT, anchor='w').pack(fill='x')
        self.summary = tk.StringVar(value='Start capture to inspect a validated fighter. Frame and Y/Z are unverified.')
        tk.Label(self.frame, textvariable=self.summary, bg=_BACKGROUND, fg=_TEXT, anchor='w', justify='left', wraplength=900).pack(fill='x')
        self.music_summary = tk.StringVar(value='Music boundaries: start experiments to observe.')
        tk.Label(self.frame, textvariable=self.music_summary, bg=_BACKGROUND, fg=_ACCENT, anchor='w', justify='left', wraplength=900).pack(fill='x')
        self.live_summary = tk.StringVar(value='Start all four or experiments to observe fighters live.')
        tk.Label(self.frame, textvariable=self.live_summary, bg=_BACKGROUND, fg=_TEXT, anchor='w', justify='left', wraplength=1050).pack(fill='x')
        tk.Label(self.frame, text='Observed timeline (provisional associations; polling can miss events)', bg=_BACKGROUND, fg=_ACCENT, anchor='w').pack(fill='x')
        self.timeline = tk.Listbox(self.frame, height=5, bg=_CONTROL_BACKGROUND, fg=_TEXT, exportselection=False)
        self.timeline.pack(fill='x')
        self.closed = False
        self.frame.after(250, self.refresh)

    def start(self):
        self.service.start(self.slot.get())

    def start_experiments(self):
        state = self.service.state()
        if state['running'] and state['mode'] == 'all' and state['advanced']:
            self.service.stop()
            return
        self.advanced.set(True)
        self.service.set_advanced(True)
        self.service.start('ALL')

    def toggle_all(self):
        state = self.service.state()
        if state['running'] and state['mode'] == 'all':
            self.service.stop()
        else:
            self.service.start('ALL')

    def export(self):
        selected = filedialog.asksaveasfilename(parent=self.frame, title='Export fighter evidence',
                                                defaultextension='.jsonl', filetypes=[('JSON Lines', '*.jsonl')])
        if not selected:
            return
        json_path = Path(selected)
        csv_path = json_path.with_suffix('.csv')

        def worker():
            try:
                self.service.export_copy(json_path, csv_path)
                self.messages.put('Exported ' + json_path.name + ' and ' + csv_path.name)
            except Exception as exc:
                self.messages.put('Export failed: ' + str(exc))

        # Disk writes can be slow; keep them off the Tk event loop.
        threading.Thread(target=worker, name='research-evidence-export', daemon=True).start()

    def refresh(self):
        if self.closed:
            return
        state = self.service.state()
        try:
            msg = self.messages.get_nowait()
        except queue.Empty:
            msg = None
        self.all_button.configure(text='Stop all four' if state['running'] and state['mode'] == 'all' else 'Start all four')
        self.experiment_button.configure(text='Stop experiments' if state['running'] and state['mode'] == 'all' and state['advanced'] else 'Start experiments')
        status = ('Recording ' + state['mode'] + ' at 100 ms; sequential, not frame-atomic' if state['running'] else 'Stopped')
        status += f" | {state['events']} recent changes, {state['snapshots']} snapshots | recent ring evictions {state['dropped_events']} / snapshot drops {state['dropped_snapshots']} | read failures {state['read_failures']} | mailbox: {state['trace_status']}"
        status += f" | preserved boundaries {state['priority_events']} (evicted {state['priority_dropped']})"
        if state['batch_interval_seconds'] is not None:
            status += f" | actual batch interval {state['batch_interval_seconds'] * 1000:.0f} ms"
        costs = state.get('batch_costs_seconds') or {}
        if costs:
            status += ' | read costs ms: ' + ', '.join(f'{name} {seconds * 1000:.0f}' for name, seconds in costs.items())
        status += ' | advanced: ' + ('ON' if state['advanced'] else 'OFF')
        status += ' | music operations: ' + state.get('music_operation_status', 'unavailable')
        if state['error']:
            status += ' | error: ' + state['error']
        if msg:
            status += ' | ' + msg
        self.status.set(status)
        music = state.get('music')
        if music:
            cached = music.get('controller') or {}
            raw = music.get('raw') or {}
            self.music_summary.set(
                f"Music: enabled {cached.get('enabled', 'unavailable')} | scene {cached.get('scene', 'unavailable')} | "
                f"phase {cached.get('last_fast_phase', 'unavailable')} | boundary pending {cached.get('boundary_pending', 'unavailable')} | "
                f"last playback request {cached.get('track') or 'none/unavailable'} | output {cached.get('output', 'unavailable')} | gate {cached.get('bgm_status', 'unavailable')}\n"
                f"Stage cached/raw {cached.get('stage')}/{cached.get('raw_stage')} | music serial {cached.get('match_serial_internal')} | owner {cached.get('owner')} | native fallback {cached.get('native_fallback')} | AX pair verified {cached.get('ax_pair_verified')}\n"
                f"Mixer busy (last observed, may be stale): {cached.get('mixer_busy_last_observed')}\n"
                f"Music message: {cached.get('message', 'unavailable')} | audible output cannot be verified here\n"
                f"Native setup latch: {_field_text(raw, 'battle_setup_latch_raw')} | Raw director phase / stage / stream candidates: {' / '.join(str(raw.get(n, {}).get('value')) for n in ('director_phase_raw', 'stage_result_raw', 'stream_index_candidate_raw', 'stream_companion_candidate_raw'))} | "
                + music.get('controller_status', 'unavailable'))
        else:
            self.music_summary.set('Music boundaries: start experiments to observe.')
        samples = state.get('latest_by_slot', {})
        slots = tuple(SLOT_ADDRESSES) if state['mode'] == 'all' else (self.slot.get(),)
        freshness = 'LIVE polling' if state['running'] else 'STOPPED — last samples, not live'
        self.live_summary.set(f"{freshness} | individual timestamps; samples are not simultaneous\n" +
                              '\n'.join(_live_slot_text(slot, samples.get(slot)) for slot in slots))
        s = state['latest']
        if s:
            fields = s.get('fields', {})
            def val(name):
                return _field_text(fields, name)
            actor = s.get('actor')
            addr = f'0x{actor:08X}' if actor is not None else 'unavailable'
            text = (f"{s['slot']} | actor {addr} | {s['status']} | ID {val('character_id')} | HP {val('current_hp')}/{val('max_hp')} | action selected/committed/pending {val('action')}/{val('action_secondary')}/{val('pending_action')} | X {val('position_x')} | Y/Z unverified | phase {val('director_phase')} | KO {val('ko_flag')}\n"
                    f"Input current/pressed/released/+13D8/+13DC: {val('input_current')}/{val('input_pressed')}/{val('input_released')}/{val('input_13d8_raw')}/{val('input_13dc_raw')} | action-frame candidate {val('action_frame_raw')}; global game frame unavailable\n"
                    f"Raw +44A0/+44A4/+44A8/+44AC: {val('field_44a0_raw')}/{val('assist_44a4_raw')}/{val('assist_44a8_raw')}/{val('assist_request_raw')} | +44B8/+44D8/+44E8: {val('assist_timer_raw')}/{val('assist_44d8_raw')}/{val('assist_44e8_raw')} | +11C4/+456C: {val('combo_count_raw')}/{val('assist_combo_gate_raw')}")
            d = s.get('assist_diagnostic', {})
            text += '\nLatest sample advanced diagnostics: ' + ('ON' if s.get('advanced_enabled') else 'OFF')
            selected = d.get('assist_profile_selected')
            selected_text = 'yes' if selected is True else 'no' if selected is False else 'unavailable (identity/cache not validated)'
            text += (f"\nAssist profile selected: {selected_text}; profile {d.get('assist_profile_label') or 'unavailable'}; attA/attB/mv {d.get('attA')}/{d.get('attB')}/{d.get('mv_id_display')}; window {d.get('assist_window')}; wrapper {d.get('assist_wrapper_address')}; selector {d.get('assist_selector_word')}; state wrapper/chosen state {d.get('assist_state_wrapper_address')}/{d.get('assist_inferred_state')} (unavailable if not cached); runtime state {d.get('assist_runtime_state')}; patch {d.get('assist_patch_status', 'unavailable')}")
            missing = unavailable_fields(s)
            other_missing = [name for name, reason in missing.items() if reason != 'unavailable: advanced capture disabled']
            text += '\nUnavailable fields: ' + (', '.join(other_missing) if other_missing else 'none among enabled fields')
            if any(reason == 'unavailable: advanced capture disabled' for reason in missing.values()):
                text += '; advanced fields unavailable (capture disabled)'
            if self.advanced.get():
                text += ('\nAdvanced raw +214C/+4310/+43F4/+43E4: ' + '/'.join(val(n) for n in ('state_index_214c_raw','state_pointer_4310_raw','linked_43f4_raw','linked_43e4_raw'))
                         + '\nRaw +1204/+4588/+44E0/+21C/+44C4/+4600/+4638/+4490: ' + '/'.join(val(n) for n in ('blockstun_1204_raw','field_4588_raw','field_44e0_raw','field_21c_raw','field_44c4_raw','field_4600_raw','field_4638_raw','field_4490_raw'))
                         + f"\nSigned +21C/+4600: {val('field_21c_signed')}/{val('field_4600_signed')} | Primary owner meter current/max/levels: {val('primary_meter_current')}/{val('primary_meter_max')}/{val('primary_meter_levels')} | linked Polimar +4310 pointee +20: {val('polimar_linked_state_20')}")
                text += ('\nExperimental raw orientation +8C/+D4/+D8/+100 and flags +60/+70: '
                         + '/'.join(val(n) for n in ('orientation_position_8c_raw', 'orientation_d4_raw', 'orientation_d8_raw', 'orientation_100_raw', 'orientation_flags_60_raw', 'orientation_flags_70_raw'))
                         + '\nExperimental command table/accepted/progress/flags/index: '
                         + '/'.join(val(n) for n in ('command_table_198c_pointer_raw', 'command_accepted_1994_raw', 'command_progress_13ec_raw', 'command_flags_2108_raw', 'command_index_210c_raw'))
                         + '\nRaw candidates only; facing, cross-up and command timing unverified.')
            self.summary.set(text)
        elif state['running']:
            self.summary.set('Waiting for a validated live fighter. Game frame and Y/Z unavailable.')
        self.timeline.delete(0, tk.END)
        for event in state['timeline']:
            self.timeline.insert(tk.END, _timeline_text(event))
        self.timeline.yview_moveto(1.0)
        try:
            self.frame.after(250, self.refresh)
        except tk.TclError:
            pass

    def close(self):
        self.closed = True
        self.service.close()
