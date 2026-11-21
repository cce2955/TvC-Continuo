import csv, json, struct
from tvcgui.features.combat.research_capture import ChangeRecorder, FighterSampler, SLOT_ADDRESSES

class Memory:
    def __init__(self):
        self.data=bytearray(0x5000); self.base=0x90001000; self.slot=SLOT_ADDRESSES['P1-C1']; self.root=self.base
        for off,val in [(4,0),(0x14,12),(0x24,10000),(0x1e8,7),(0x1ec,8)]: self.put(off,val)
        self.data[0xf0:0xf4]=struct.pack('>f',1.25)
    def put(self,off,val): struct.pack_into('>I',self.data,off,val)
    def read(self,a,n):
        o=a-self.base
        return bytes(self.data[o:o+n]) if 0<=o and o+n<=len(self.data) else b''
    def read32(self,a):
        if a==self.slot:return self.root
        b=self.read(a,4);return struct.unpack('>I',b)[0] if len(b)==4 else None
    @staticmethod
    def valid(a):return isinstance(a,int) and 0x80000000<=a<0x94000000
    def resolve_base(self,_):return self.base,True
    def sampler(self):return FighterSampler(self.read,self.read32,self.valid,self)

def test_validation_and_unavailable_values():
    m=Memory();s=m.sampler().sample('P1-C1')
    assert s['actor']==m.base and s['fields']['action']['value']==7 and s['fields']['position_x']['value']==1.25
    assert s['position_y'] is None and s['position_z'] is None and s['frame'] is None
    assert s['fields']['director_phase']['value'] is None
    m.put(4,1);assert m.sampler().sample('P1-C1')['actor'] is None

def test_stale_pointer_and_nan():
    m=Memory();m.root=0x90008000;assert m.sampler().sample('P1-C1')['actor'] is None
    m.root=m.base;m.data[0xf0:0xf4]=struct.pack('>f',float('nan'))
    assert m.sampler().sample('P1-C1')['fields']['position_x']['value'] is None

def test_change_only_buffer_and_identity():
    m=Memory();r=ChangeRecorder(limit=2);s=m.sampler().sample('P1-C1');r.accept(s,'t0');r.accept(s,'t1')
    assert len(r.events)==1 and r.events[0]['type']=='actor_acquired' and len(r.snapshots)==2
    for v in (8,9,10):m.put(0x1e8,v);r.accept(m.sampler().sample('P1-C1'),'t')
    assert len(r.events)==2 and r.dropped_events==2
    assert r.events[-1]['old_value']==9 and r.events[-1]['new_value']==10 and r.events[-1]['address']==m.base+0x1e8
    m.root=0;r.accept(m.sampler().sample('P1-C1'),'t2')
    assert r.events[-1]['type']=='actor_lost' and r.events[-1]['new_value'] is None


def test_boundary_events_survive_noisy_ring_and_export_once(tmp_path):
    recorder = ChangeRecorder(limit=3)
    recorder._event({'type': 'music_observation', 'session_id': 's', 'new_value': {'scene': 'battle'}})
    for value in range(8):
        recorder._event({'type': 'field_change', 'session_id': 's', 'field': 'position_x', 'new_value': value})
    retained = recorder.retained_events()
    assert [item['type'] for item in retained] == ['music_observation', 'field_change', 'field_change', 'field_change']
    assert recorder.dropped_events == 6 and recorder.priority_dropped == 0
    j, c = tmp_path/'events.jsonl', tmp_path/'snapshots.csv'
    recorder.export(j, c)
    events = [json.loads(line) for line in j.read_text(encoding='utf8').splitlines()]
    assert sum(item['type'] == 'music_observation' for item in events) == 1
    assert [item['recorder_event_sequence'] for item in events if item['type'] != 'export_metadata'] == [1, 7, 8, 9]


def test_priority_ring_is_bounded_and_reports_evictions():
    recorder = ChangeRecorder(limit=2)
    for number in range(4):
        recorder._event({'type': 'music_observation', 'new_value': number})
    assert len(recorder.priority_events) == 2
    assert recorder.priority_dropped == 2
    assert [item['new_value'] for item in recorder.retained_events()] == [2, 3]

def test_exports(tmp_path):
    m=Memory();r=ChangeRecorder();r.accept(m.sampler().sample('P1-C1'),'t0');m.put(0x1e8,42);r.accept(m.sampler().sample('P1-C1'),'t1')
    j,c=tmp_path/'capture.jsonl',tmp_path/'capture.csv';r.export(j,c)
    e=json.loads(j.read_text(encoding='utf8').splitlines()[-1]);assert (e['field'],e['old_value'],e['new_value'])==('action',7,42)
    with c.open(encoding='utf8',newline='') as f:rows=list(csv.DictReader(f))
    assert rows[-1]['action']=='42' and rows[-1]['frame']=='' and rows[-1]['position_y']==''

def test_passive_existing_trace_reads_entry_lr_and_arguments():
    from tvcgui.features.combat.research_capture import ExistingDamageTrace
    from tvcgui.features.training import attack_property_profiler as n
    blob=bytearray(n.RESOLVER_MAILBOX_SIZE)
    struct.pack_into('>III',blob,0,0,n.RESOLVER_MAILBOX_MAGIC,n.RESOLVER_MAILBOX_VERSION)
    def read(_address,size):return bytes(blob[:size])
    def read32(address):
        if address==n.RESOLVER_HOOK_ADDR:return n.resolver_hook_word()
        if address==n.RESOLVER_MAILBOX_ADDR:return struct.unpack_from('>I',blob,0)[0]
        return None
    trace=ExistingDamageTrace(read,read32);assert trace.poll()==[]
    struct.pack_into('>I',blob,0,1)
    pos=n.RESOLVER_MAILBOX_HEADER_SIZE+n.RESOLVER_ENTRY_SIZE
    for offset,value in [(n.RES_OFF_SEQUENCE,1),(n.RES_OFF_ATTACKER,0x90001000),(n.RES_OFF_DEFENDER,0x90002000),(n.RES_OFF_CALLER_LR,0x8004e8a8),(n.RES_OFF_PROPERTY_A,0x1234)]:
        struct.pack_into('>I',blob,pos+offset,value)
    event=trace.poll()[0]
    assert event['function_entry']==n.RESOLVER_HOOK_ADDR and event['caller_return_address']==0x8004e8a8
    assert event['entry_arguments']['r3']==0x90001000 and event['entry_arguments']['r6']==0x1234
    assert event['return_value'] is None and event['associated_actor_snapshot'] is None

def test_rejects_actor_when_slot_changes_mid_sample():
    m=Memory(); original=m.read32; slot_reads=0
    def read32(address):
        nonlocal slot_reads
        if address==m.slot:
            slot_reads+=1
            if slot_reads>1:return 0
        return original(address)
    snap=FighterSampler(m.read,read32,m.valid,m).sample('P1-C1')
    assert snap['actor'] is None and snap['fields']=={}
    assert 'changed during read' in snap['status']


def test_passive_trace_rejects_uninstalled_hook():
    from tvcgui.features.combat.research_capture import ExistingDamageTrace
    trace=ExistingDamageTrace(lambda _a,_n:b'',lambda _a:0)
    assert trace.poll()==[] and 'not active' in trace.status

def test_capture_is_opened_from_lab_as_a_standalone_window():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    monitor = (root/'tvcgui/features/combat/projectile_scanner.py').read_text(encoding='utf8')
    dock = (root/'tvcgui/ui/components.py').read_text(encoding='utf8')
    main = (root/'main.py').read_text(encoding='utf8')
    assert 'ResearchCapturePanel(research_tab' not in monitor
    assert '("research_recorder", 148)' in dock
    assert 'dock.research_recorder.collidepoint(mx, my)' in main
    assert 'open_research_recorder(' in main


def test_standalone_recorder_reuses_window_and_closes_panel(monkeypatch):
    from tvcgui.features.combat import research_window

    created = []
    panels = []
    class Window:
        def __init__(self, _master): self.closed = False; self.focused = False; created.append(self)
        def title(self, _value): pass
        def configure(self, **_kwargs): pass
        def geometry(self, _value): pass
        def winfo_exists(self): return not self.closed
        def lift(self): self.focused = True
        def focus_force(self): pass
        def protocol(self, _name, fn): self.close_callback = fn
        def destroy(self): self.closed = True
    class Panel:
        def __init__(self, window, slot, provider):
            self.window, self.slot, self.provider, self.closed = window, slot, provider, False
            panels.append(self)
        def close(self): self.closed = True
    monkeypatch.setattr(research_window, '_window', None)
    monkeypatch.setattr(research_window.tk, 'Toplevel', Window)
    monkeypatch.setattr(research_window, 'ResearchCapturePanel', Panel)
    monkeypatch.setattr(research_window, 'tk_call', lambda fn: fn(object()))
    provider = lambda: {}
    research_window.open_research_recorder(provider)
    research_window.open_research_recorder(provider)
    assert len(created) == len(panels) == 1
    assert panels[0].provider is provider and created[0].focused
    created[0].close_callback()
    assert panels[0].closed and created[0].closed and research_window._window is None

def test_reused_address_sentinel_and_legacy_alias():
    m=Memory(); r=ChangeRecorder(); m.put(0x1e8, 0xffffffff)
    first=m.sampler().sample('P1-C1'); first['sample_id']=1; r.accept(first)
    assert first['fields']['action']['status']=='raw 0xFFFFFFFF sentinel candidate'
    assert first['fields']['point_flag_raw']['value']==first['fields']['field_44a0_raw']['value']
    m.put(0x14, 13); second=m.sampler().sample('P1-C1'); second['sample_id']=2; r.accept(second)
    event=r.events[-1]
    assert event['type']=='character_changed' and event['old_character_id']==12 and event['character_id']==13
    assert event['old_value']==event['new_value']==m.base


def test_advanced_invalid_pointer_and_primary_meter():
    m=Memory(); m.put(0x48,50000); m.put(0x4c,20000); m.put(0x43f4,0x1234)
    snap=m.sampler().sample('P1-C1',advanced=True)
    assert snap['fields']['primary_meter_current']['value']==20000
    assert snap['fields']['primary_meter_max']['value']==50000
    assert snap['fields']['primary_meter_levels']['value']==2.0
    assert snap['fields']['polimar_linked_state_20']['value'] is None
    assert 'unavailable' in snap['fields']['polimar_linked_state_20']['status']
    m.put(0x48,0)
    assert m.sampler().sample('P1-C1',advanced=True)['fields']['primary_meter_levels']['value'] is None


def test_experiment_fields_are_raw_and_do_not_dereference_candidates():
    m = Memory()
    m.data[0xd4:0xd8] = struct.pack('>f', -90.0)
    m.put(0x100, 1)
    m.put(0x198c, 0x80000000)
    m.put(0x210c, 0x30)
    m.put(0x4304, 0x80000000)
    snap = m.sampler().sample('P1-C1', advanced=True)
    fields = snap['fields']
    assert fields['orientation_d4_raw']['value'] == -90.0
    assert fields['orientation_100_raw']['address'] == m.base + 0x100
    assert fields['command_index_210c_raw']['value'] == 0x30
    assert fields['command_table_198c_pointer_raw']['value'] == 0x80000000
    assert fields['cpu_control_4304_pointer_raw']['value'] == 0x80000000
    assert 'unverified' in fields['orientation_d4_raw']['status']
    assert 'orientation_d4_raw' not in m.sampler().sample('P1-C1')['fields']


def test_experiment_button_starts_advanced_all_and_stops():
    from tvcgui.features.combat.research_capture_panel import ResearchCapturePanel

    class Service:
        def __init__(self): self.running = False; self.advanced = False; self.mode = 'slot'
        def state(self): return {'running': self.running, 'advanced': self.advanced, 'mode': self.mode}
        def set_advanced(self, value): self.advanced = value
        def start(self, slot): self.mode = 'all' if slot == 'ALL' else 'slot'; self.running = True
        def stop(self): self.running = False

    class Flag:
        def set(self, value): self.value = value

    panel = ResearchCapturePanel.__new__(ResearchCapturePanel)
    panel.service = Service()
    panel.advanced = Flag()
    panel.start_experiments()
    assert panel.service.state() == {'running': True, 'advanced': True, 'mode': 'all'}
    assert panel.advanced.value is True
    panel.start_experiments()
    assert not panel.service.running


def test_music_observation_keeps_missing_values_and_raw_addresses(monkeypatch):
    import sys
    from tvcgui.features.combat.research_capture import MUSIC_RAW_ADDRESSES, read_music_observation

    monkeypatch.delitem(sys.modules, 'tvcgui.features.audio.music_player', raising=False)
    result = read_music_observation(lambda addr: 9 if addr == MUSIC_RAW_ADDRESSES['stage_result_raw'] else None)
    assert result['raw']['stage_result_raw']['value'] == 9
    assert result['raw']['stream_index_candidate_raw']['value'] is None
    assert result['raw']['stream_index_candidate_raw']['address'] == 0x8056169c
    assert result['controller'] is None and 'not loaded' in result['controller_status']


def test_music_director_phase_rejects_changed_pointer():
    from tvcgui.features.combat.research_capture import DIRECTOR_POINTER, read_music_observation

    reads = 0
    def read32(addr):
        nonlocal reads
        if addr == DIRECTOR_POINTER:
            reads += 1
            return 0x90001000 if reads == 1 else 0x90002000
        return 9

    raw = read_music_observation(read32)['raw']
    assert raw['director_phase_raw']['value'] is None
    assert 'changed' in raw['director_phase_raw']['status']


def test_experiment_exports_music_changes_without_claiming_playback(tmp_path):
    import time
    from tvcgui.features.combat.research_capture import CaptureService

    class Sampler:
        def sample(self, slot, advanced=False):
            return {'slot': slot, 'actor': None, 'character_id': None, 'fields': {},
                    'status': 'unavailable', 'read_failures': 0}

    data = {'phase': 4}
    def music():
        return {'raw': {}, 'controller': {'native_phase': data['phase'], 'track': 'song'},
                'controller_status': 'cached'}

    service = CaptureService(sampler=Sampler(), interval=0.005, music_reader=music)
    try:
        service.set_advanced(True)
        service.start('ALL')
        deadline = time.monotonic() + 2
        while service.state()['music'] is None and time.monotonic() < deadline: time.sleep(0.005)
        data['phase'] = 9
        while service.state()['music']['controller']['native_phase'] != 9 and time.monotonic() < deadline: time.sleep(0.005)
        service.stop()
        j, c = tmp_path / 'music.jsonl', tmp_path / 'music.csv'
        service.export_copy(j, c)
        events = [json.loads(line) for line in j.read_text(encoding='utf8').splitlines()]
        changes = [event for event in events if event['type'] == 'music_observation']
        assert len(changes) == 2
        assert changes[-1]['old_value']['controller']['native_phase'] == 4
        assert changes[-1]['new_value']['controller']['native_phase'] == 9
        assert 'do not establish audible playback' in changes[-1]['uncertainty']
    finally:
        service.close()


def test_advanced_polimar_linked_pointer_ownership():
    m=Memory(); m.put(0x14,4); m.put(0x43f4,m.base); m.put(0x4310,m.base+0x4800); m.put(0x4820,0x131)
    m.put(0x1e8,99)
    snap=m.sampler().sample('P1-C1',advanced=True)
    assert snap['fields']['polimar_linked_state_20']['value']==0x131
    assert snap['fields']['polimar_linked_state_20']['address']==m.base+0x4820
    assert snap['fields']['action']['value']==99
    m.put(0x4310,0x80000000)
    assert m.sampler().sample('P1-C1',advanced=True)['fields']['polimar_linked_state_20']['value'] is None


def test_all_slot_batch_partial_availability_and_export(tmp_path):
    import time
    from tvcgui.features.combat.research_capture import CaptureService
    class FakeSampler:
        def sample(self, slot, advanced=False):
            if slot == 'P2-C1': raise ValueError('read failed')
            return {'slot':slot, 'actor':0x90000000+list(SLOT_ADDRESSES).index(slot)*0x10000,
                    'character_id':12, 'status':'validated actor', 'timestamp_utc':'individual-'+slot,
                    'fields':{'action':{'address':0x900001e8,'value':10,'status':'observed'}}, 'read_failures':0}
    service=CaptureService(sampler=FakeSampler(),interval=0.01,limit=100)
    try:
        service.start('ALL')
        deadline=time.monotonic()+2
        while service.state()['snapshots'] < 4 and time.monotonic()<deadline: time.sleep(0.01)
        service.stop()
        outj,outc=tmp_path/'all.jsonl',tmp_path/'all.csv'; service.export_copy(outj,outc)
        with outc.open(encoding='utf8',newline='') as f: rows=list(csv.DictReader(f))
        first=[r for r in rows if r['sample_id']=='1']
        assert len(first)==4 and len({r['session_id'] for r in first})==1
        assert {r['slot'] for r in first}==set(SLOT_ADDRESSES)
        assert len({r['timestamp_utc'] for r in first})==4
        assert next(r for r in first if r['slot']=='P2-C1')['read_failures']=='1'
        assert service.state()['read_failures']>=1
        assert all(json.loads(line)['session_id']==first[0]['session_id'] for line in outj.read_text(encoding='utf8').splitlines())
        count=service.state()['snapshots']; time.sleep(0.04); assert service.state()['snapshots']==count
    finally: service.close()

def test_director_sentinel_and_raw_coordinate_jump():
    from tvcgui.features.combat.research_capture import DIRECTOR_POINTER
    m=Memory(); director=m.base+0x4900; original=m.read32
    def read32(a):
        if a==DIRECTOR_POINTER: return director
        return original(a)
    m.put(0x490c,0xffffffff)
    sampler=FighterSampler(m.read,read32,m.valid,m); recorder=ChangeRecorder()
    first=sampler.sample('P1-C1'); recorder.accept(first)
    assert first['fields']['director_phase']['value']==0xffffffff
    assert first['fields']['director_phase']['status']=='raw 0xFFFFFFFF sentinel candidate'
    m.data[0xf0:0xf4]=struct.pack('>f',100.0)
    second=sampler.sample('P1-C1'); recorder.accept(second)
    assert second['fields']['position_x']['value']==100.0
    jump=[e for e in recorder.events if e['type']=='coordinate_jump_candidate']
    assert jump and jump[-1]['old_value']==1.25 and jump[-1]['new_value']==100.0


def test_export_has_unique_columns_and_bounded_buffers(tmp_path):
    m=Memory(); r=ChangeRecorder(limit=2)
    for _ in range(3): r.accept(m.sampler().sample('P1-C1'))
    assert r.dropped_snapshots==1 and len(r.snapshots)==2
    j,c=tmp_path/'out.jsonl',tmp_path/'out.csv'; r.export(j,c)
    with c.open(encoding='utf8',newline='') as f:
        reader=csv.DictReader(f); assert len(reader.fieldnames)==len(set(reader.fieldnames))
        rows=list(reader)
    assert rows[0]['point_flag_raw']==rows[0]['field_44a0_raw']
    assert rows[0]['schema_version']=='4'

def test_lifecycle_loss_reacquisition_and_signed_fields():
    m=Memory(); recorder=ChangeRecorder(); recorder.accept(m.sampler().sample('P1-C1'))
    m.root=0; recorder.accept(m.sampler().sample('P1-C1'))
    m.root=m.base; m.put(0x21c,0xffffffff); m.put(0x4600,0xfffffffe)
    snap=m.sampler().sample('P1-C1',advanced=True); recorder.accept(snap)
    assert [e['type'] for e in recorder.events]==['actor_acquired','actor_lost','actor_acquired']
    assert snap['fields']['field_21c_raw']['value']==0xffffffff
    assert snap['fields']['field_21c_signed']['value']==-1
    assert snap['fields']['field_4600_signed']['value']==-2


def test_cached_assist_identity_prevents_stale_attribution(monkeypatch):
    from tvcgui.features.combat.research_capture import CaptureService
    from tvcgui.features.assists import backend
    seen=[]
    def debug(snaps):
        seen.append(snaps)
        return {'slot_profiles':{'P1-C1':{'selected':True,'char_id':12,'fighter_base':0x90001000,
                    'label':'profile','word':123,'in_assist_window':True,'state_id':426}}}
    monkeypatch.setattr(backend, 'get_assist_runtime_debug_state', debug)
    hud={'P1-C1':{'base':0x90001000,'id':12,'attA':426,'mv_id_display':426}}
    service=CaptureService(hud_provider=lambda:hud)
    try:
        sample={'slot':'P1-C1','actor':0x90001000,'character_id':12}
        service._diagnostics([sample]); d=sample['assist_diagnostic']
        assert d['assist_profile_selected'] and d['attA']==426 and d['assist_window'] is True
        assert d['assist_inferred_state'] is None and d['assist_runtime_state']==426
        sample={'slot':'P1-C1','actor':0x90001000,'character_id':13}
        service._diagnostics([sample]); d=sample['assist_diagnostic']
        assert d['attA'] is None and d['assist_profile_selected'] is None and not seen[-1]
    finally: service.close()


def test_profile_monitor_preserves_all_slot_provider_and_partial_close(monkeypatch):
    from tvcgui.features.combat import projectile_scanner as scanner
    class Root:
        def title(self, *_): pass
        def geometry(self, *_): pass
        def minsize(self, *_): pass
        def configure(self, **_): pass
        def protocol(self, *_): pass
        def after(self, *_): pass
        def destroy(self): self.destroyed = True
    root = Root(); provider = lambda: {'P1-C1': {'id': 12}}
    monkeypatch.setattr(scanner.tk, 'Toplevel', lambda _: root)
    monkeypatch.setattr(scanner.ProjectileProfileMonitorWindow, '_build_styles', lambda self: None)
    observed = []
    monkeypatch.setattr(scanner.ProjectileProfileMonitorWindow, '_build_ui', lambda self: observed.append(self._get_all_snap))
    monkeypatch.setattr(scanner, '_clear_live_profile_label', lambda *_: None)
    window = scanner.ProjectileProfileMonitorWindow(None, 'P1-C1', lambda: {}, provider)
    assert observed == [provider]
    window._on_close()
    assert root.destroyed and window._closed


def test_capture_replay_ko_associations_and_lifecycle():
    from pathlib import Path
    import pytest
    capture = Path(__file__).resolve().parents[2] / 'research/agent_mapping/all.csv'
    if not capture.exists(): pytest.skip('local four-slot capture not available')
    recorder = ChangeRecorder(limit=30000)
    with capture.open(encoding='utf-8-sig', newline='') as f: rows = list(csv.DictReader(f))
    for start in range(0, len(rows), 4):
        batch = []
        for row in rows[start:start+4]:
            fields = {}
            for name in ('current_hp','ko_flag','action','director_phase','position_x'):
                value = row[name]
                value = None if value == '' else (value == 'True' if name == 'ko_flag' else float(value) if name == 'position_x' else int(value))
                fields[name] = {'value': value, 'address': int(row[name+'_address']) if row[name+'_address'] else None, 'status': row[name+'_status']}
            batch.append({'slot':row['slot'], 'sample_id':int(row['sample_id']), 'timestamp_utc':row['timestamp_utc'],
                          'actor':int(row['actor']) if row['actor'] else None,
                          'character_id':int(row['character_id']) if row['character_id'] else None,
                          'status':row['status'], 'fields':fields})
        recorder.accept_batch(batch)
    ko = [e for e in recorder.events if e['type']=='observed_individual_ko']
    teams = [e for e in recorder.events if e['type']=='probable_team_defeat']
    assert [(e['sample_id'],e['character_id']) for e in ko] == [(40,28),(54,29),(598,6),(654,13)]
    assert [(e['sample_id'],e['slot']) for e in teams] == [(54,'P2'),(654,'P2')]
    assert all(len(e['evidence'])==2 and 'not frame-atomic' in e['uncertainty'] for e in teams)
    assert all(row['field_4600_raw_status']=='unavailable: advanced capture disabled' for row in rows)
    assert not any(row['assist_profile_selected']=='True' for row in rows)


def test_lifecycle_never_compares_fields_across_loss_or_reused_actor():
    m=Memory(); recorder=ChangeRecorder(); first=m.sampler().sample('P1-C1'); recorder.accept(first)
    lost=dict(first,actor=None,character_id=None,status='unavailable',fields={'director_phase':{'value':9,'address':123,'status':'observed'}})
    recorder.accept(lost)
    m.put(0x14,13); m.put(0x1e8,154); regained=m.sampler().sample('P1-C1'); recorder.accept(regained)
    assert [e['type'] for e in recorder.events] == ['actor_acquired','actor_lost','actor_acquired']
    assert recorder.events[-1]['reused_address_with_changed_character'] is True
    assert recorder.events[-1]['last_valid_character_id'] == 12
    recorder.accept(dict(regained, character_id=14, fields=dict(regained['fields'], action={'value':17,'address':m.base+0x1e8,'status':'observed'})))
    assert recorder.events[-1]['type']=='character_changed'
    assert not any(e.get('type')=='field_change' for e in recorder.events)


def test_metadata_records_settings_and_missing_reasons(tmp_path):
    m=Memory(); recorder=ChangeRecorder(); sample=m.sampler().sample('P1-C1')
    sample.update(sample_id=1,capture_mode='all',advanced_enabled=False,mailbox_enabled=False,
                  assist_diagnostic={'assist_profile_selected':False})
    recorder.accept(sample); j,c=tmp_path/'a.jsonl',tmp_path/'a.csv'; recorder.export(j,c)
    meta=json.loads(j.read_text(encoding='utf8').splitlines()[0])
    assert meta['type']=='export_metadata' and meta['settings_observed']==[{'capture_mode':'all','advanced_enabled':False,'mailbox_enabled':False}]
    assert meta['unavailable_fields']['field_4600_raw']==['unavailable: advanced capture disabled']
    assert meta['assist_profile_selected_observed']==[False]
    with c.open(encoding='utf8',newline='') as f: row=next(csv.DictReader(f))
    assert row['advanced_enabled']=='False' and row['assist_profile_selected']=='False'
    assert json.loads(row['unavailable_fields'])['field_4600_raw']=='unavailable: advanced capture disabled'
