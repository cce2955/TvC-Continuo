"""Summarize a Research CSV/JSONL capture without changing source evidence."""
import collections
import csv
import json
import sys
from pathlib import Path

csv_path, json_path, output = map(Path, sys.argv[1:4])
sessions = {}
orientation = collections.Counter()
tatsu = collections.Counter()
shape_flags = collections.Counter()
inputs = {}
last_inputs = {}
with csv_path.open(encoding='utf-8-sig', newline='') as handle:
    for row in csv.DictReader(handle):
        sid = row['session_id']
        s = sessions.setdefault(sid, {'snapshots': 0, 'slots': collections.Counter(), 'characters': collections.Counter(),
                                      'status': collections.Counter(), 'settings': collections.Counter(),
                                      'samples': set(), 'first': row['timestamp_utc'], 'last': '', 'read_failures': 0,
                                      'phases': collections.Counter(), 'actions_ko': collections.Counter(),
                                      'assist_profiles': collections.Counter()})
        s['snapshots'] += 1
        s['slots'][row['slot']] += 1
        if row['character_id']:
            s['characters'][(row['slot'], row['character_id'])] += 1
        s['status'][row['status']] += 1
        s['settings'][(row['capture_mode'], row['advanced_enabled'], row['mailbox_enabled'])] += 1
        s['samples'].add(row['sample_id'])
        s['last'] = row['timestamp_utc']
        s['read_failures'] += int(row['read_failures'] or 0)
        if row.get('director_phase'): s['phases'][row['director_phase']] += 1
        if row.get('ko_flag') == 'True': s['actions_ko'][(row['slot'], row.get('character_id'), row.get('action'))] += 1
        s['assist_profiles'][row.get('assist_profile_selected', '')] += 1
        if row.get('actor') and row.get('input_current'):
            key = (sid, row['slot'])
            info = inputs.setdefault(key, {'valid_samples': 0, 'pressed_samples': 0, 'released_samples': 0,
                                          'held_changes': 0, 'actions': collections.Counter()})
            info['valid_samples'] += 1
            try:
                held, pressed, released = (int(row.get(name) or '0') for name in ('input_current', 'input_pressed', 'input_released'))
                info['pressed_samples'] += bool(pressed)
                info['released_samples'] += bool(released)
                old = last_inputs.get(key)
                identity = (row['actor'], row['character_id'])
                if old and old[0] == identity and old[1] != held:
                    info['held_changes'] += 1
                last_inputs[key] = (identity, held)
            except ValueError:
                pass
            if row.get('action'):
                info['actions'][(row['character_id'], row['action'])] += 1
        else:
            last_inputs.pop((sid, row['slot']), None)
        if row['slot'] == 'P1-C1' and row.get('character_id') == '12' and row.get('advanced_enabled') == 'True':
            if row.get('orientation_d4_raw') and row.get('orientation_d8_raw') and row.get('input_current'):
                try:
                    d4, d8 = float(row['orientation_d4_raw']), float(row['orientation_d8_raw'])
                    word = int(row['input_current'])
                    orientation[(round(d4, 1), round(d8, 1), bool(word & 0x8000))] += 1
                except ValueError:
                    pass
            if row.get('action') in {'273','274','275','307','308','309','323','324','325'}:
                tatsu[(row['action'], row.get('action_frame_raw', ''))] += 1
            if all(row.get(f'hitbox_slot{i}_flags_raw') for i in range(3)):
                try:
                    shape_flags[tuple(int(row[f'hitbox_slot{i}_flags_raw']) & 1 for i in range(3))] += 1
                except ValueError:
                    pass

events = collections.Counter()
metadata = []
milestones = []
music = []
with json_path.open(encoding='utf-8-sig') as handle:
    for line in handle:
        e = json.loads(line)
        typ = e.get('type', 'unknown')
        events[typ] += 1
        if typ == 'export_metadata': metadata.append(e)
        elif typ in ('observed_individual_ko', 'probable_team_defeat', 'character_changed', 'actor_changed'):
            milestones.append({k: e.get(k) for k in ('type', 'timestamp_utc', 'sample_id', 'slot', 'actor', 'character_id', 'old_character_id', 'old_value', 'new_value', 'provisional_label')})
        elif typ == 'music_observation':
            new = e.get('new_value') or {}
            music.append({'timestamp_utc': e.get('timestamp_utc'), 'sample_id': e.get('sample_id'),
                          'controller': new.get('controller'), 'raw': {k:v.get('value') for k,v in (new.get('raw') or {}).items()}})

def cv_counter(counter):
    return [{'key': list(k) if isinstance(k, tuple) else k, 'count': v} for k, v in counter.most_common()]

for s in sessions.values():
    s['sample_count'] = len(s.pop('samples'))
    for key in ('slots', 'characters', 'status', 'settings', 'phases', 'actions_ko', 'assist_profiles'):
        s[key] = cv_counter(s[key])
for info in inputs.values():
    info['actions'] = cv_counter(info['actions'])[:20]
summary = {'sessions': sessions, 'event_counts': dict(events),
           'metadata': [{k:e.get(k) for k in ('session_id','settings_observed','dropped_events','dropped_snapshots','read_failures','assist_profile_selected_observed')} for e in metadata],
           'milestones': milestones, 'music_observations': music,
           'ryu_orientation_candidates': cv_counter(orientation), 'ryu_tatsu_action_frames': cv_counter(tatsu),
           'ryu_hitbox_allocated_bits': cv_counter(shape_flags),
           'inputs_by_session_slot': [{'session': k[0], 'slot': k[1], **v} for k,v in inputs.items()]}
output.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
print(f'sessions={len(sessions)} event_types={dict(events)} milestones={len(milestones)} music={len(music)}; saved {output}')
