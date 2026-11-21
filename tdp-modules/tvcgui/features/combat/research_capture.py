"""Read-only, bounded fighter evidence capture for the existing Research tab."""
from __future__ import annotations

import csv
import json
import math
import struct
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from tvcgui.core.constants import BAD_PTRS, INDIR_PROBES, POSX_OFF, SLOTS
from tvcgui.platform.dolphin import addr_in_ram, rbytes, rd32
from tvcgui.tools.scanners.fighter_resolver import SlotResolver, looks_like_hp

SCHEMA_VERSION = 4
FIELDS = {
    'character_id': (0x14, 'u32'), 'max_hp': (0x24, 'u32'), 'current_hp': (0x28, 'u32'),
    'action': (0x1e8, 'u32'), 'action_secondary': (0x1ec, 'u32'), 'pending_action': (0x1f4, 'u32'),
    'action_frame_raw': (0x1d8, 'f32'), 'position_x': (POSX_OFF, 'f32'),
    'fighter_flags': (0x00, 'u32'), 'input_current': (0x13cc, 'u32'),
    'input_pressed': (0x13d0, 'u32'), 'input_released': (0x13d4, 'u32'),
    'input_13d8_raw': (0x13d8, 'u32'), 'input_13dc_raw': (0x13dc, 'u32'),
    'field_44a0_raw': (0x44a0, 'u32'), 'assist_44a4_raw': (0x44a4, 'u32'),
    'assist_44a8_raw': (0x44a8, 'u32'), 'assist_request_raw': (0x44ac, 'u32'),
    'assist_timer_raw': (0x44b8, 'u32'), 'assist_44d8_raw': (0x44d8, 'u32'),
    'assist_44e8_raw': (0x44e8, 'u32'), 'combo_count_raw': (0x11c4, 'u32'),
    'assist_combo_gate_raw': (0x456c, 'u32'),
}
ADVANCED_FIELDS = {
    'state_index_214c_raw': (0x214c, 'u32'), 'state_pointer_4310_raw': (0x4310, 'u32'),
    'linked_43f4_raw': (0x43f4, 'u32'), 'linked_43e4_raw': (0x43e4, 'u32'),
    'blockstun_1204_raw': (0x1204, 'u32'), 'field_4588_raw': (0x4588, 'u32'),
    'field_44e0_raw': (0x44e0, 'u32'), 'field_21c_raw': (0x21c, 'u32'),
    'field_44c4_raw': (0x44c4, 'u32'), 'field_4600_raw': (0x4600, 'u32'),
    'field_4638_raw': (0x4638, 'u32'), 'field_4490_raw': (0x4490, 'u32'),
    'orientation_position_8c_raw': (0x8c, 'f32'), 'orientation_d4_raw': (0xd4, 'f32'),
    'orientation_d8_raw': (0xd8, 'f32'), 'orientation_100_raw': (0x100, 'u32'),
    'orientation_flags_60_raw': (0x60, 'u32'), 'orientation_flags_70_raw': (0x70, 'u32'),
    'command_progress_13ec_raw': (0x13ec, 'u32'), 'command_counter_13f0_raw': (0x13f0, 'u32'),
    'command_counter_13f4_raw': (0x13f4, 'u32'), 'command_counter_13f8_raw': (0x13f8, 'u32'),
    'command_table_198c_pointer_raw': (0x198c, 'u32'), 'command_accepted_1994_raw': (0x1994, 'u32'),
    'command_flags_2108_raw': (0x2108, 'u32'), 'command_index_210c_raw': (0x210c, 'u32'),
    'command_metadata_2110_raw': (0x2110, 'u32'), 'command_parameter_2114_raw': (0x2114, 'u32'),
    'cpu_control_4304_pointer_raw': (0x4304, 'u32'), 'cpu_route_4308_raw': (0x4308, 'u32'),
    'hitbox_slot0_flags_raw': (0x500, 'u32'), 'hitbox_slot1_flags_raw': (0x540, 'u32'),
    'hitbox_slot2_flags_raw': (0x580, 'u32'),
    'hitbox_slot0_x_raw': (0x524, 'f32'), 'hitbox_slot0_y_raw': (0x528, 'f32'), 'hitbox_slot0_radius_raw': (0x53c, 'f32'),
    'hitbox_slot1_x_raw': (0x564, 'f32'), 'hitbox_slot1_y_raw': (0x568, 'f32'), 'hitbox_slot1_radius_raw': (0x57c, 'f32'),
    'hitbox_slot2_x_raw': (0x5a4, 'f32'), 'hitbox_slot2_y_raw': (0x5a8, 'f32'), 'hitbox_slot2_radius_raw': (0x5bc, 'f32'),
}
EXPERIMENT_FIELDS = {name for name in ADVANCED_FIELDS if name.startswith(('orientation_', 'command_', 'cpu_', 'hitbox_'))}
DERIVED_ADVANCED_FIELDS = ('primary_meter_current', 'primary_meter_max', 'primary_meter_levels',
                           'polimar_linked_state_20', 'field_21c_signed', 'field_4600_signed')
CAPTURE_FIELDS = list(FIELDS) + ['point_flag_raw', 'ko_flag', 'director_phase'] + list(ADVANCED_FIELDS) + list(DERIVED_ADVANCED_FIELDS)
CSV_BASE_FIELDS = [
    'schema_version', 'session_id', 'sample_id', 'timestamp_utc', 'slot', 'actor',
    'character_id', 'status', 'read_failures', 'frame', 'frame_source',
    'position_y', 'position_y_status', 'position_z', 'position_z_status',
    'capture_mode', 'advanced_enabled', 'mailbox_enabled', 'unavailable_fields',
]
CSV_ASSIST_FIELDS = [
    'assist_profile_selected', 'assist_profile_label', 'assist_selector_word',
    'assist_wrapper_address', 'assist_state_wrapper_address', 'assist_inferred_state',
    'assist_runtime_state', 'assist_patch_status', 'assist_window',
    'attA', 'attB', 'mv_id_display',
]


def field_status(snap, name):
    item = snap.get('fields', {}).get(name)
    if item is not None:
        return item.get('status', 'unavailable: status missing')
    if (name in ADVANCED_FIELDS or name in DERIVED_ADVANCED_FIELDS) and not snap.get('advanced_enabled', False):
        return 'unavailable: advanced capture disabled'
    return 'unavailable: actor or field not validated'


def unavailable_fields(snap):
    missing = {name: field_status(snap, name) for name in CAPTURE_FIELDS
               if snap.get('fields', {}).get(name, {}).get('value') is None}
    if snap.get('frame') is None: missing['global_game_frame'] = snap.get('frame_source') or 'unavailable: no validated game-frame source'
    if snap.get('position_y') is None: missing['position_y'] = snap.get('position_y_status') or 'unavailable: coordinate unverified'
    if snap.get('position_z') is None: missing['position_z'] = snap.get('position_z_status') or 'unavailable: coordinate unverified'
    return missing
SLOT_ADDRESSES = {label: address for label, address, _ in SLOTS}
SLOT_INDICES = {'P1-C1': 0, 'P2-C1': 1, 'P1-C2': 2, 'P2-C2': 3}
DIRECTOR_POINTER = 0x803efb4c
SENTINEL_FIELDS = {'action', 'action_secondary', 'pending_action', 'director_phase'}
TIMELINE_FIELDS = {'action', 'action_secondary', 'pending_action', 'ko_flag', 'director_phase',
                   'field_44a0_raw', 'assist_44a4_raw', 'assist_request_raw', 'assist_timer_raw',
                   'assist_44d8_raw', 'assist_44e8_raw', 'combo_count_raw', 'assist_combo_gate_raw'} | (set(ADVANCED_FIELDS) - EXPERIMENT_FIELDS)
ASSOCIATIONS = {154: 'provisional: observed with defeated fighters in supplied captures',
                419: 'provisional: observed with incoming Zero in supplied captures',
                426: 'provisional: observed during Chun-Li assist cycle',
                430: 'provisional: observed after Chun-Li assist cycle'}

MUSIC_RAW_ADDRESSES = {
    'stage_result_raw': 0x803eb8f4,
    'battle_setup_latch_raw': 0x80561140,
    'stream_index_candidate_raw': 0x8056169c,
    'stream_companion_candidate_raw': 0x805616a0,
}


def read_music_observation(read32=rd32):
    """Read raw candidates and copy existing controller state, without starting it."""
    fields = {}
    for name, address in MUSIC_RAW_ADDRESSES.items():
        try:
            value = read32(address)
        except Exception:
            value = None
        fields[name] = {'address': address, 'value': value,
                        'status': 'observed raw; meaning unverified' if value is not None else 'unavailable: read failed'}
    try:
        director = read32(DIRECTOR_POINTER)
        phase_address = director + 0x0c if isinstance(director, int) and addr_in_ram(director) and addr_in_ram(director + 0x0f) else None
        phase = read32(phase_address) if phase_address is not None else None
        if director is not None and read32(DIRECTOR_POINTER) != director:
            phase, phase_address = None, None
    except Exception:
        director, phase_address, phase = None, None, None
    fields['director_pointer_raw'] = {'address': DIRECTOR_POINTER, 'value': director,
                                      'status': 'observed raw' if director is not None else 'unavailable: read failed'}
    fields['director_phase_raw'] = {'address': phase_address, 'value': phase,
                                    'status': 'observed raw; interpretation provisional' if phase is not None else 'unavailable: director invalid or changed'}
    module = sys.modules.get('tvcgui.features.audio.music_player')
    controller = getattr(module, 'controller', None) if module is not None else None
    try:
        cached = controller.research_snapshot() if controller is not None else None
        status = 'cached controller decisions; not proof of audible playback' if cached is not None else 'unavailable: music controller not loaded'
    except Exception as exc:
        cached = None
        status = 'unavailable: music controller snapshot failed: ' + str(exc)
    return {'raw': fields, 'controller': cached, 'controller_status': status}


def read_music_operations(sequence):
    """Copy already-recorded host mixer operations without starting the player."""
    module = sys.modules.get('tvcgui.features.audio.music_player')
    controller = getattr(module, 'controller', None) if module is not None else None
    if controller is None:
        return None
    return controller.research_operations_since(sequence)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _value(data, offset, kind):
    """Decode a big-endian field without converting missing data to zero."""
    if data is None or offset < 0 or offset + 4 > len(data):
        return None
    value = struct.unpack_from('>f' if kind == 'f32' else '>I', data, offset)[0]
    return value if kind != 'f32' or math.isfinite(value) else None


class FighterSampler:
    """Resolve afresh and reject the resolver's stale-pointer TTL fallback."""
    def __init__(self, read=rbytes, read32=rd32, valid=addr_in_ram, resolver=None):
        self.read, self.read32, self.valid = read, read32, valid
        self.resolver = resolver or SlotResolver()

    def _range(self, address, size):
        return isinstance(address, int) and size > 0 and self.valid(address) and self.valid(address + size - 1)

    def _block(self, address, size):
        if not self._range(address, size):
            return None
        try:
            value = self.read(address, size)
        except Exception:
            return None
        return value if value is not None and len(value) == size else None

    def _word(self, address):
        if not self._range(address, 4):
            return None
        try:
            return self.read32(address)
        except Exception:
            return None

    def _resolve(self, slot):
        ptr_addr = SLOT_ADDRESSES.get(slot)
        if ptr_addr is None:
            return None, None, 'unknown slot'
        root = self._word(ptr_addr)
        if not self.valid(root) or root in BAD_PTRS:
            return None, root, 'slot pointer unavailable or initializing'
        try:
            base, _ = self.resolver.resolve_base(ptr_addr)
        except Exception:
            return None, root, 'slot resolver failed'
        if not self.valid(base) or base in BAD_PTRS:
            return None, root, 'fighter pointer unavailable'
        # SlotResolver may return a recent cached address. Require a current
        # pointer relationship before attributing a sample to that fighter.
        if root != base and not any(self._word(root + off) == base for off in INDIR_PROBES):
            return None, root, 'stale fighter pointer rejected'
        return base, root, None

    def sample(self, slot, advanced=False):
        stamp = utc_now()
        result = {'timestamp_utc': stamp, 'slot': slot, 'actor': None, 'character_id': None,
                  'status': 'unavailable', 'read_failures': 0, 'frame': None,
                  'frame_source': 'unavailable: no validated game-frame source',
                  'position_y': None, 'position_y_status': 'unverified: per-fighter Y offset',
                  'position_z': None, 'position_z_status': 'unverified coordinate convention', 'fields': {}}
        def field(name, address, value, reason=None):
            status = reason or ('observed' if value is not None else 'unavailable: read failed')
            if value == 0xffffffff and name in SENTINEL_FIELDS:
                status = 'raw 0xFFFFFFFF sentinel candidate'
            result['fields'][name] = {'address': address, 'value': value, 'status': status}
            if value is None and reason is None:
                result['read_failures'] += 1
        director = self._word(DIRECTOR_POINTER)
        phase_addr = director + 0x0c if self._range(director, 0x10) else None
        field('director_phase', phase_addr, self._word(phase_addr) if phase_addr else None,
              None if phase_addr else 'unavailable: director pointer invalid')
        base, root, reason = self._resolve(slot)
        if reason:
            if root is None:
                result['read_failures'] += 1
            result['status'] = 'unavailable: ' + reason
            return result
        head = self._block(base, 0x50)
        if head is None:
            result['status'] = 'unavailable: fighter header read failed'
            result['read_failures'] += 1
            return result
        if not looks_like_hp(_value(head, 0x24, 'u32'), None, None):
            result['status'] = 'initializing or invalid: max HP not validated'
            return result
        if _value(head, 4, 'u32') != SLOT_INDICES[slot]:
            result['status'] = 'unavailable: slot identity not validated'
            return result
        cid = _value(head, 0x14, 'u32')
        if cid is None or not 1 <= cid <= 30:
            result['status'] = 'initializing or invalid: character identity not validated'
            return result
        blocks = [(0, head), (0xf0, self._block(base + 0xf0, 0x108)),
                  (0x11c4, self._block(base + 0x11c4, 4)),
                  (0x13cc, self._block(base + 0x13cc, 0x14)),
                  (0x44a0, self._block(base + 0x44a0, 0x50)),
                  (0x456c, self._block(base + 0x456c, 4))]
        if advanced:
            blocks.extend((off, self._block(base + off, size)) for off, size in
                          [(0x21c, 4), (0x1204, 4), (0x214c, 4), (0x4310, 4),
                           (0x43e4, 4), (0x43f4, 4), (0x4490, 4), (0x4588, 4),
                           (0x4600, 4), (0x4638, 4), (0x60, 0x80), (0x13ec, 0x10),
                           (0x198c, 0x0c), (0x2108, 0x10), (0x4304, 8), (0x500, 0xc0)])
        specs = dict(FIELDS)
        if advanced:
            specs.update(ADVANCED_FIELDS)
        for name, (off, kind) in specs.items():
            value = next((_value(b, off-start, kind) for start, b in blocks
                          if start <= off and b is not None and off+4 <= start+len(b)), None)
            field(name, base + off, value,
                  'observed raw; interpretation unverified' if name in EXPERIMENT_FIELDS and value is not None else None)
        # Schema-1 alias retained for existing CSV consumers. It is not a point-fighter classification.
        result['fields']['point_flag_raw'] = dict(result['fields']['field_44a0_raw'], status='alias of field_44a0_raw')
        flags = result['fields']['fighter_flags']['value']
        field('ko_flag', base, None if flags is None else bool(flags & 0x1000))
        if advanced:
            primary_slot = 'P1-C1' if slot.startswith('P1-') else 'P2-C1'
            owner, owner_root, owner_reason = self._resolve(primary_slot)
            owner_head = self._block(owner, 0x50) if owner is not None else None
            if (owner_head is None or _value(owner_head, 4, 'u32') != SLOT_INDICES[primary_slot]
                    or not looks_like_hp(_value(owner_head, 0x24, 'u32'), None, None)
                    or not 1 <= (_value(owner_head, 0x14, 'u32') or 0) <= 30
                    or self._word(owner + 4) != SLOT_INDICES[primary_slot]
                    or self._word(owner + 0x14) != _value(owner_head, 0x14, 'u32')
                    or self._word(SLOT_ADDRESSES[primary_slot]) != owner_root):
                owner = None; owner_reason = owner_reason or 'primary owner not validated'
            maximum = _value(owner_head, 0x48, 'u32') if owner is not None else None
            current = _value(owner_head, 0x4c, 'u32') if owner is not None else None
            field('primary_meter_max', owner + 0x48 if owner is not None else None, maximum,
                  None if owner is not None else 'unavailable: ' + owner_reason)
            field('primary_meter_current', owner + 0x4c if owner is not None else None, current,
                  None if owner is not None else 'unavailable: ' + owner_reason)
            levels = current / (maximum / 5.0) if maximum not in (None, 0, 0xffffffff) and current not in (None, 0xffffffff) else None
            field('primary_meter_levels', None, levels, 'derived from owner +0x4c / (+0x48 / 5)' if levels is not None else 'unavailable: invalid meter maximum/current')
            # These gates use signed comparisons; retain both the raw word and interpretation.
            for raw_name, signed_name in [('field_21c_raw', 'field_21c_signed'), ('field_4600_raw', 'field_4600_signed')]:
                item = result['fields'][raw_name]
                raw = item['value']
                field(signed_name, item['address'], (raw - 0x100000000 if raw >= 0x80000000 else raw) if raw is not None else None,
                      'signed interpretation of raw word' if raw is not None else 'unavailable: source word read failed')
            # The Polimar check belongs to the selected linked fighter, not the source.
            linked = result['fields']['linked_43f4_raw']['value'] or result['fields']['linked_43e4_raw']['value']
            link_slot = next((label for label in SLOT_ADDRESSES if self._resolve(label)[0] == linked), None) if self.valid(linked) else None
            link_head = self._block(linked, 0x30) if link_slot else None
            link_cid = _value(link_head, 0x14, 'u32')
            if not link_slot or link_cid != 4 or _value(link_head, 4, 'u32') != SLOT_INDICES[link_slot] or not looks_like_hp(_value(link_head, 0x24, 'u32'), None, None):
                field('polimar_linked_state_20', None, None, 'unavailable: linked Polimar fighter not validated')
            else:
                ptr = self._word(linked + 0x4310)
                value = self._word(ptr + 0x20) if self._range(ptr, 0x24) else None
                if (self._word(linked + 0x4310) != ptr or self._resolve(link_slot)[0] != linked
                        or self._word(linked + 0x14) != 4 or self._word(linked + 4) != SLOT_INDICES[link_slot]
                        or (self._word(base + 0x43f4) or self._word(base + 0x43e4)) != linked):
                    value = None
                field('polimar_linked_state_20', ptr + 0x20 if self._range(ptr, 0x24) else None, value,
                      'observed on linked fighter pointee; ownership provisional' if value is not None else 'unavailable: linked state pointer invalid or changed')
        # Reject the entire actor sample if identity changed during the reads.
        if (self._word(SLOT_ADDRESSES[slot]) != root or self._word(base + 4) != SLOT_INDICES[slot]
                or self._word(base + 0x14) != cid or (root != base and not any(self._word(root + off) == base for off in INDIR_PROBES))):
            result.update(actor=None, character_id=None, status='unavailable: fighter changed during read', fields={})
            return result
        result.update(actor=base, character_id=cid, status='validated actor; read-only sample')
        return result


class ChangeRecorder:
    """Store bounded observations while keeping identities separate by slot."""

    def __init__(self, limit=5000):
        self.events = deque(maxlen=limit)
        self.priority_events = deque(maxlen=min(limit, 4096))
        self.priority_dropped = 0
        self.event_sequence = 0
        self.snapshots = deque(maxlen=limit)
        self.previous = {}
        self.dropped_events = 0
        self.dropped_snapshots = 0
        self.session_id = uuid.uuid4().hex
        self.read_failures = 0
        self.team_defeat_latch = {}
        self.last_valid_identity = {}

    def clear(self):
        self.events.clear()
        self.priority_events.clear()
        self.priority_dropped = 0
        self.event_sequence = 0
        self.snapshots.clear()
        self.previous = {}
        self.dropped_events = self.dropped_snapshots = self.read_failures = 0
        self.session_id = uuid.uuid4().hex
        self.team_defeat_latch.clear()
        self.last_valid_identity.clear()

    def _event(self, event):
        self.event_sequence += 1
        event = dict(event, recorder_event_sequence=self.event_sequence)
        if len(self.events) == self.events.maxlen:
            self.dropped_events += 1
        self.events.append(event)
        priority = (event.get('type') in ('music_observation', 'music_operation', 'music_operation_gap', 'observed_individual_ko', 'probable_team_defeat',
                    'actor_acquired', 'actor_lost', 'actor_changed', 'character_changed', 'lifecycle_status')
                    or (event.get('type') == 'field_change' and event.get('field') in ('director_phase', 'ko_flag')))
        if priority:
            if len(self.priority_events) == self.priority_events.maxlen:
                self.priority_dropped += 1
            self.priority_events.append(event)

    def retained_events(self):
        """Merge the recent ring and protected boundary ring without duplicates."""
        merged = {event['recorder_event_sequence']: event for event in self.priority_events}
        merged.update({event['recorder_event_sequence']: event for event in self.events})
        return [merged[key] for key in sorted(merged)]

    def accept(self, snap, timestamp=None):
        stamp = timestamp or snap.get('timestamp_utc') or utc_now()
        snap = dict(snap, timestamp_utc=stamp, schema_version=SCHEMA_VERSION, session_id=self.session_id)
        slot = snap.get('slot')
        prev = self.previous.get(slot)
        current_id = snap.get('character_id', snap.get('fields', {}).get('character_id', {}).get('value'))
        snap['character_id'] = current_id
        self.read_failures += snap.get('read_failures', 0)
        common = {'schema_version': SCHEMA_VERSION, 'session_id': self.session_id, 'sample_id': snap.get('sample_id'),
                  'timestamp_utc': stamp, 'frame': snap.get('frame'), 'frame_source': snap.get('frame_source'),
                  'slot': slot, 'actor': snap.get('actor'), 'character_id': current_id,
                  'capture_method': 'sequential memory polling', 'uncertainty': 'not frame-atomic; changes between samples may be missed'}
        if prev is None or (snap.get('actor'), current_id, snap.get('status')) != (prev.get('actor'), prev.get('character_id'), prev.get('status')):
            old_actor = prev.get('actor') if prev else None
            old_char = prev.get('character_id') if prev else None
            last_actor, last_char = self.last_valid_identity.get(slot, (None, None))
            if snap.get('actor') is not None and old_actor is None:
                kind = 'actor_acquired'
            elif old_actor is not None and snap.get('actor') is None:
                kind = 'actor_lost'
            elif old_actor == snap.get('actor') and old_char != current_id:
                kind = 'character_changed'
            elif old_actor != snap.get('actor'):
                kind = 'actor_changed'
            else:
                kind = 'lifecycle_status'
            self._event(dict(common, type=kind, field='actor', address=SLOT_ADDRESSES.get(slot), old_value=old_actor,
                             new_value=snap.get('actor'), old_character_id=old_char, old_status=prev.get('status') if prev else None, new_status=snap.get('status'),
                             last_valid_actor=last_actor, last_valid_character_id=last_char,
                             reused_address_with_changed_character=(kind == 'actor_acquired' and last_actor is not None and last_actor == snap.get('actor') and last_char != current_id)))
        if prev is not None:
            same = (prev.get('actor'), prev.get('character_id')) == (snap.get('actor'), current_id)
            for name, item in snap.get('fields', {}).items():
                if name == 'point_flag_raw':
                    continue
                old = prev.get('fields', {}).get(name)
                # A reused address is not enough to make two actors identical.
                if old is None or not same or snap.get('actor') is None:
                    continue
                if (old.get('value'), old.get('address'), old.get('status')) != (item.get('value'), item.get('address'), item.get('status')):
                    value = item.get('value')
                    self._event(dict(common, type='field_change', field=name, address=item.get('address'), old_address=old.get('address'),
                                     old_value=old.get('value'), new_value=value, value_status=item.get('status'),
                                     provisional_label=ASSOCIATIONS.get(value) if name == 'action' else
                                         ('provisional capture association: second KO' if name == 'director_phase' and value == 9 else
                                          'provisional capture association: later phase' if name == 'director_phase' and value == 12 else None)))
                    if name == 'position_x' and isinstance(value, (int, float)) and isinstance(old.get('value'), (int, float)) and abs(value-old['value']) > 50:
                        self._event(dict(common, type='coordinate_jump_candidate', field=name, address=item.get('address'),
                                         old_value=old['value'], new_value=value, uncertainty='heuristic >50 units per poll; raw values preserved'))
        if self._ko_observed(snap) and (prev is None or
                (prev.get('actor'), prev.get('character_id')) != (snap.get('actor'), current_id) or not self._ko_observed(prev)):
            self._event(dict(common, type='observed_individual_ko', field='ko_flag',
                             address=snap['fields']['ko_flag']['address'],
                             provisional_label='individual KO observed: HP 0, KO flag true, action 154 (capture association)',
                             evidence=self._ko_evidence(snap)))
        if len(self.snapshots) == self.snapshots.maxlen:
            self.dropped_snapshots += 1
        self.snapshots.append(snap)
        self.previous[slot] = snap
        if snap.get('actor') is not None and current_id is not None:
            self.last_valid_identity[slot] = (snap['actor'], current_id)
        return snap

    @staticmethod
    def _ko_observed(snap):
        fields = snap.get('fields', {})
        return (snap.get('actor') is not None and fields.get('current_hp', {}).get('value') == 0
                and fields.get('ko_flag', {}).get('value') is True and fields.get('action', {}).get('value') == 154)

    @staticmethod
    def _ko_evidence(snap):
        return {'slot': snap.get('slot'), 'actor': snap.get('actor'), 'character_id': snap.get('character_id'),
                'timestamp_utc': snap.get('timestamp_utc'),
                'fields': {name: dict(snap['fields'][name]) for name in ('current_hp', 'ko_flag', 'action', 'director_phase') if name in snap.get('fields', {})}}

    def accept_batch(self, samples):
        """Record sequential reads sharing a batch ID, not a game frame."""
        accepted = [self.accept(sample) for sample in samples]
        by_slot = {sample.get('slot'): sample for sample in accepted}
        for team in ('P1', 'P2'):
            pair = [by_slot.get(team + '-C1'), by_slot.get(team + '-C2')]
            if any(s is None for s in pair):
                self.team_defeat_latch.pop(team, None)
                continue
            identity = tuple((s.get('actor'), s.get('character_id')) for s in pair)
            if any(actor is None for actor, _ in identity) or identity[0][0] == identity[1][0]:
                self.team_defeat_latch.pop(team, None)
                continue
            same_batch = pair[0].get('sample_id') is not None and pair[0].get('sample_id') == pair[1].get('sample_id')
            qualifies = (same_batch and all(self._ko_observed(s) for s in pair) and
                         any(s.get('fields', {}).get('director_phase', {}).get('value') == 9 for s in pair))
            if qualifies and self.team_defeat_latch.get(team) != identity:
                last = pair[-1]
                self._event({'schema_version': SCHEMA_VERSION, 'session_id': self.session_id,
                             'sample_id': last.get('sample_id'), 'timestamp_utc': last.get('timestamp_utc'),
                             'slot': team, 'actor': None, 'character_id': None, 'type': 'probable_team_defeat',
                             'field': 'team_ko_observations', 'address': None,
                             'capture_method': 'sequential memory polling',
                             'uncertainty': 'provisional association; same batch is not frame-atomic',
                             'provisional_label': 'probable team defeat: both fighters observed KO and director 9',
                             'evidence': [self._ko_evidence(s) for s in pair]})
                self.team_defeat_latch[team] = identity
            elif not qualifies:
                self.team_defeat_latch.pop(team, None)
        return accepted

    def export(self, jsonl_path, csv_path):
        """Write retained observations without reading game memory."""
        with Path(jsonl_path).open('w', encoding='utf-8', newline='\n') as f:
            sessions = {}
            for snap in self.snapshots:
                session = sessions.setdefault(snap.get('session_id'), {'settings': set(), 'unavailable_fields': {}, 'assist_profile_selected': set()})
                session['settings'].add((snap.get('capture_mode'), snap.get('advanced_enabled'), snap.get('mailbox_enabled')))
                session['assist_profile_selected'].add(snap.get('assist_diagnostic', {}).get('assist_profile_selected'))
                for name, reason in unavailable_fields(snap).items():
                    session['unavailable_fields'].setdefault(name, set()).add(reason)
            for event in self.retained_events():
                sessions.setdefault(event.get('session_id'), {'settings': set(), 'unavailable_fields': {}, 'assist_profile_selected': set()})
            for session_id, details in sessions.items():
                metadata = {'type': 'export_metadata', 'schema_version': SCHEMA_VERSION, 'session_id': session_id,
                            'settings_observed': [{'capture_mode': mode, 'advanced_enabled': advanced, 'mailbox_enabled': mailbox}
                                                  for mode, advanced, mailbox in sorted(details['settings'], key=str)],
                            'assist_profile_selected_observed': sorted(details['assist_profile_selected'], key=str),
                            'unavailable_fields': {name: sorted(reasons) for name, reasons in details['unavailable_fields'].items()},
                            'dropped_events': self.dropped_events, 'priority_events_retained': len(self.priority_events),
                            'priority_events_evicted': self.priority_dropped, 'dropped_snapshots': self.dropped_snapshots,
                            'read_failures': self.read_failures, 'uncertainty': 'sequential polling; not frame-atomic'}
                f.write(json.dumps(metadata, ensure_ascii=False, allow_nan=False) + '\n')
            for item in self.retained_events():
                f.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + '\n')

        columns = list(dict.fromkeys(
            CSV_BASE_FIELDS
            + CAPTURE_FIELDS
            + [name + '_address' for name in CAPTURE_FIELDS]
            + [name + '_status' for name in CAPTURE_FIELDS]
            + CSV_ASSIST_FIELDS
        ))
        with Path(csv_path).open('w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            for snap in self.snapshots:
                row = {key: snap.get(key) for key in CSV_BASE_FIELDS}
                row.update(snap.get('assist_diagnostic', {}))
                row['unavailable_fields'] = json.dumps(unavailable_fields(snap), ensure_ascii=False, sort_keys=True)
                for name in CAPTURE_FIELDS:
                    item = snap.get('fields', {}).get(name, {})
                    row[name] = item.get('value')
                    row[name + '_address'] = item.get('address')
                    row[name + '_status'] = field_status(snap, name)
                writer.writerow({key: row.get(key) for key in columns})


class ExistingDamageTrace:
    """Passively read the existing ATK6 ring; never arm or restore its hooks."""

    def __init__(self, read=rbytes, read32=rd32):
        from tvcgui.features.training import attack_property_profiler as native

        self.native = native
        self.read = read
        self.read32 = read32
        self.previous = None
        self.status = 'waiting for existing damage probe'

    def poll(self):
        n = self.native
        if self.read32(n.RESOLVER_HOOK_ADDR) != n.resolver_hook_word():
            self.previous = None
            self.status = 'existing damage probe not active'
            return []
        # The sequence is read twice so a concurrent mailbox update is not
        # mistaken for a coherent snapshot.
        before = self.read32(n.RESOLVER_MAILBOX_ADDR)
        blob = self.read(n.RESOLVER_MAILBOX_ADDR, n.RESOLVER_MAILBOX_SIZE)
        after = self.read32(n.RESOLVER_MAILBOX_ADDR)
        if before is None or before != after or not blob or len(blob) != n.RESOLVER_MAILBOX_SIZE:
            self.status = 'mailbox changed or unavailable'
            return []
        word = lambda off: struct.unpack_from('>I', blob, off)[0]
        if word(0) != after or word(4) != n.RESOLVER_MAILBOX_MAGIC or word(8) != n.RESOLVER_MAILBOX_VERSION:
            self.previous = None
            self.status = 'existing damage mailbox unavailable'
            return []
        current = after & 0xffffffff
        if self.previous is None:
            self.previous = current
            self.status = 'listening to existing damage probe'
            return []
        delta = (current - self.previous) & 0xffffffff
        if delta > 0x7fffffff:
            self.previous = current
            self.status = 'mailbox reset'
            return []
        missed = max(0, delta - n.RESOLVER_RING_COUNT)
        out = []
        for distance in range(min(delta, n.RESOLVER_RING_COUNT), 0, -1):
            seq = (current - distance + 1) & 0xffffffff
            offset = n.RESOLVER_MAILBOX_HEADER_SIZE + (seq & (n.RESOLVER_RING_COUNT-1)) * n.RESOLVER_ENTRY_SIZE
            entry = n.RuntimeAttackPropertyProfiler._decode_resolver_entry(blob[offset:offset+n.RESOLVER_ENTRY_SIZE], seq)
            if not entry:
                continue
            out.append({'type': 'function_entry', 'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                        'frame': None, 'frame_source': 'unavailable', 'function_entry': n.RESOLVER_HOOK_ADDR,
                        'capture_address': n.RESOLVER_HOOK_ADDR, 'caller_return_address': entry['caller_lr'],
                        'entry_arguments': {'r3': entry['attacker_base'], 'r4': entry['defender_base'],
                                            'r5': entry['property_b'], 'r6': entry['property_a'],
                                            'r7': entry['result_ptr_a'], 'r8': entry['result_ptr_b'],
                                            'r9': entry['route_arg'], 'r10': entry['packet']},
                        'return_value': None, 'associated_actor_snapshot': None, 'sequence': seq,
                        'capture_method': 'existing ATK6 entry-hook mailbox; passive read',
                        'uncertainty': 'timestamp is polling time; mailbox can lose events; nested completion is not paired; actor attribution unverified'})
        self.previous = current
        self.status = 'listening to existing damage probe' + (f'; at least {missed} entries overwritten' if missed else '')
        return out


class CaptureService:
    """One worker samples slots sequentially; no game writes or UI-thread reads."""

    def __init__(self, sampler=None, interval=0.1, limit=24000, hud_provider=None, music_reader=None):
        self.sampler = sampler or FighterSampler()
        self.interval, self.limit, self.hud_provider = interval, limit, hud_provider
        self.recorder = ChangeRecorder(limit)
        self.lock = threading.RLock()
        self.running = False
        self.closed = False
        self.slot = 'P1-C1'
        self.mode = 'slot'
        self.advanced = False
        self.trace_enabled = False
        self.trace = None
        self.trace_status = 'disabled'
        self.music_reader = music_reader or read_music_observation
        self.music_latest = None
        self.music_previous = None
        self.music_operation_cursor = 0
        self.music_capture_started = None
        self.music_operation_status = 'not recording'
        self.error = None
        self.latest = {}
        self.generation = 0
        self.sample_id = 0
        self.last_batch_started = None
        self.last_batch_interval = None
        self.last_batch_duration = None
        self.last_batch_costs = None
        self.last_cost_event_at = None
        self.thread = threading.Thread(target=self._run, name='research-capture', daemon=True)
        self.thread.start()

    def start(self, slot):
        with self.lock:
            self.mode = 'all' if slot == 'ALL' else 'slot'
            if slot in SLOT_ADDRESSES:
                self.slot = slot
            self.recorder.previous.clear()
            self.recorder.team_defeat_latch.clear()
            self.recorder.last_valid_identity.clear()
            self.recorder.session_id = str(uuid.uuid4())
            self.sample_id = 0
            self.last_batch_started = self.last_batch_interval = self.last_batch_duration = None
            self.last_batch_costs = None
            self.last_cost_event_at = None
            self.music_previous = None
            self.music_latest = None
            self.latest.clear()
            self.music_operation_cursor = 0
            self.music_capture_started = utc_now()
            self.music_operation_status = 'waiting for player'
            self.generation += 1
            self.running = True
            self.error = None

    def stop(self):
        with self.lock:
            self.running = False
            self.generation += 1

    def set_slot(self, slot):
        if slot not in SLOT_ADDRESSES:
            return
        with self.lock:
            self.slot = slot
            if self.mode == 'slot':
                self.generation += 1

    def set_advanced(self, enabled):
        with self.lock:
            self.advanced = bool(enabled)
            self.music_previous = None
            self.music_latest = None
            self.generation += 1

    def set_trace(self, enabled):
        with self.lock:
            self.trace_enabled = bool(enabled)
            self.generation += 1
            self.trace = None
            self.trace_status = 'waiting for existing damage probe' if enabled else 'disabled'

    def clear(self):
        with self.lock:
            self.recorder = ChangeRecorder(self.limit)
            self.sample_id = 0
            self.latest.clear()
            self.music_previous = None
            self.music_latest = None
            self.music_operation_cursor = 0
            self.music_capture_started = utc_now()
            self.music_operation_status = 'waiting for player'
            self.generation += 1

    def close(self):
        with self.lock:
            self.closed = True
            self.running = False
            self.generation += 1

    def state(self):
        with self.lock:
            events = []
            for e in reversed(self.recorder.events):
                if e.get('type') in ('music_operation', 'music_operation_gap', 'actor_acquired', 'actor_lost', 'actor_changed', 'character_changed', 'lifecycle_status', 'coordinate_jump_candidate', 'observed_individual_ko', 'probable_team_defeat') or (e.get('type') == 'field_change' and e.get('field') in TIMELINE_FIELDS):
                    events.append(e)
                    if len(events) == 20: break
            events.reverse()
            return {'running': self.running, 'mode': self.mode, 'advanced': self.advanced, 'latest': self.latest.get(self.slot), 'latest_by_slot': dict(self.latest), 'music': self.music_latest,
                    'batch_interval_seconds': self.last_batch_interval, 'batch_duration_seconds': self.last_batch_duration,
                    'batch_costs_seconds': self.last_batch_costs,
                    'events': len(self.recorder.events), 'priority_events': len(self.recorder.priority_events),
                    'priority_dropped': self.recorder.priority_dropped, 'snapshots': len(self.recorder.snapshots),
                    'dropped_events': self.recorder.dropped_events, 'dropped_snapshots': self.recorder.dropped_snapshots,
                    'music_operation_status': self.music_operation_status,
                    'read_failures': self.recorder.read_failures, 'trace_status': self.trace_status,
                    'error': self.error, 'timeline': events[-20:]}

    def export_copy(self, jsonl_path, csv_path):
        # Copy under the lock; file I/O must not block the capture worker.
        with self.lock:
            copy = ChangeRecorder(self.limit)
            copy.events.extend(self.recorder.events)
            copy.priority_events.extend(self.recorder.priority_events)
            copy.priority_dropped = self.recorder.priority_dropped
            copy.event_sequence = self.recorder.event_sequence
            copy.snapshots.extend(self.recorder.snapshots)
            copy.dropped_events = self.recorder.dropped_events
            copy.dropped_snapshots = self.recorder.dropped_snapshots
            copy.read_failures = self.recorder.read_failures
        copy.export(jsonl_path, csv_path)

    def _diagnostics(self, samples):
        if not self.hud_provider:
            return
        try:
            from tvcgui.features.assists.backend import get_assist_runtime_debug_state
            raw = self.hud_provider() or {}
            # Do not attribute a cached HUD snapshot to a different or reused actor.
            matching = {}
            for sample in samples:
                s = raw.get(sample['slot']) or {}
                base = s.get('base', s.get('fighter_base'))
                cid = s.get('id', s.get('char_id', s.get('character_id')))
                if sample.get('actor') is not None and base == sample['actor'] and cid == sample.get('character_id'):
                    matching[sample['slot']] = s
            debug = get_assist_runtime_debug_state(matching)
            for sample in samples:
                slot = sample['slot']
                profile = debug.get('slot_profiles', {}).get(slot, {})
                hud = matching.get(slot)
                valid_profile = (sample.get('actor') is not None and (not profile.get('selected') or
                                 (profile.get('char_id') == sample.get('character_id') and
                                  (not profile.get('fighter_base') or profile.get('fighter_base') == sample.get('actor')))))
                info = {'assist_profile_selected': profile.get('selected') if valid_profile else None,
                        'assist_profile_label': profile.get('label') if valid_profile else None,
                        'assist_selector_word': profile.get('word') if valid_profile else None,
                        'assist_wrapper_address': profile.get('wrapper_address') if valid_profile else None,
                        'assist_state_wrapper_address': profile.get('state_wrapper_address') if valid_profile else None,
                        'assist_inferred_state': None,  # chosen wrapper state is not cached by the swap implementation
                        'assist_runtime_state': profile.get('state_id') if hud else None,
                        'assist_patch_status': profile.get('patch_status', 'unavailable') if valid_profile else 'unavailable: profile identity not matched',
                        'assist_window': profile.get('in_assist_window') if hud else None,
                        'attA': hud.get('attA') if hud else None, 'attB': hud.get('attB') if hud else None,
                        'mv_id_display': hud.get('mv_id_display') if hud else None}
                sample['assist_diagnostic'] = info
        except Exception as exc:
            for sample in samples:
                sample['assist_diagnostic'] = {'assist_patch_status': 'unavailable: ' + str(exc)}

    def _run(self):
        while True:
            with self.lock:
                if self.closed:
                    return
                active, generation, advanced, mode = self.running, self.generation, self.advanced, self.mode
                slots = tuple(SLOT_ADDRESSES) if self.mode == 'all' else (self.slot,)
                trace_enabled = self.trace_enabled
                operation_cursor = self.music_operation_cursor
                if active:
                    self.sample_id += 1
                    sample_id = self.sample_id
            if active:
                batch_started = time.monotonic()
                costs = {}
                samples = []
                music = None
                operations = None
                if advanced:
                    section_started = time.monotonic()
                    try:
                        music = self.music_reader()
                    except Exception as exc:
                        music = {'raw': {}, 'controller': None, 'controller_status': 'unavailable: ' + str(exc)}
                    try:
                        operations = read_music_operations(operation_cursor)
                    except Exception:
                        operations = None
                    costs['music'] = time.monotonic() - section_started
                for slot in slots:
                    section_started = time.monotonic()
                    try:
                        sample = self.sampler.sample(slot, advanced=advanced)
                    except Exception as exc:
                        sample = {'slot': slot, 'actor': None, 'character_id': None, 'timestamp_utc': utc_now(),
                                  'status': 'unavailable: sample failed: ' + str(exc), 'read_failures': 1, 'fields': {},
                                  'frame': None, 'frame_source': 'unavailable'}
                    sample['sample_id'] = sample_id
                    sample.update(capture_mode=mode, advanced_enabled=advanced, mailbox_enabled=trace_enabled)
                    samples.append(sample)
                    costs[slot] = time.monotonic() - section_started
                section_started = time.monotonic()
                self._diagnostics(samples)
                costs['assist_diagnostics'] = time.monotonic() - section_started
                section_started = time.monotonic()
                with self.lock:
                    # Stop, clear, or an option change invalidates an in-flight batch.
                    if self.running and generation == self.generation:
                        self.last_batch_interval = (batch_started - self.last_batch_started) if self.last_batch_started is not None else None
                        self.last_batch_started = batch_started
                        self.last_batch_duration = time.monotonic() - batch_started
                        self.last_batch_costs = costs
                        for accepted in self.recorder.accept_batch(samples):
                            self.latest[accepted['slot']] = accepted
                        if music is not None:
                            self.music_latest = music
                            if music != self.music_previous:
                                self.recorder._event({
                                    'type': 'music_observation', 'schema_version': SCHEMA_VERSION,
                                    'session_id': self.recorder.session_id, 'sample_id': sample_id,
                                    'timestamp_utc': utc_now(), 'slot': None, 'actor': None,
                                    'character_id': None, 'capture_method': 'sequential memory polling and cached controller state',
                                    'uncertainty': 'not frame-atomic; cached decisions and native candidate words do not establish audible playback',
                                    'old_value': self.music_previous, 'new_value': music,
                                })
                                self.music_previous = music
                        if advanced and operations is None:
                            self.music_operation_status = 'unavailable: player history not accessible'
                        elif operations is not None:
                            lost = operations['lost'] if operation_cursor else 0
                            if lost:
                                self.recorder._event({'type': 'music_operation_gap', 'session_id': self.recorder.session_id,
                                    'sample_id': sample_id, 'timestamp_utc': utc_now(), 'lost_count': lost,
                                    'uncertainty': 'host player history was overwritten before recorder polling'})
                            for operation in operations['events']:
                                if self.music_capture_started and operation['timestamp_utc'] < self.music_capture_started:
                                    continue
                                self.recorder._event({'type': 'music_operation', 'schema_version': SCHEMA_VERSION,
                                    'session_id': self.recorder.session_id, 'sample_id': sample_id,
                                    'timestamp_utc': operation['timestamp_utc'], 'operation': operation,
                                    'capture_method': 'host music player operation history',
                                    'uncertainty': 'operation result and mixer busy do not prove audible output'})
                            self.music_operation_cursor = operations['latest_sequence']
                            self.music_operation_status = f"history through {self.music_operation_cursor}; missed {lost} this poll"
                costs['recording'] = time.monotonic() - section_started
                if trace_enabled:
                    section_started = time.monotonic()
                    try:
                        if self.trace is None:
                            self.trace = ExistingDamageTrace()
                        trace = self.trace
                        events = trace.poll()
                        with self.lock:
                            if self.running and generation == self.generation and self.trace_enabled and self.trace is trace:
                                self.trace_status = trace.status
                                for event in events:
                                    event.update(schema_version=SCHEMA_VERSION, session_id=self.recorder.session_id, sample_id=sample_id,
                                                 slot=None, actor=None, character_id=None)
                                    self.recorder._event(event)
                    except Exception as exc:
                        with self.lock:
                            self.trace_status = 'unavailable: ' + str(exc)
                    costs['mailbox'] = time.monotonic() - section_started
                with self.lock:
                    if self.running and generation == self.generation:
                        self.last_batch_costs = dict(costs)
                        self.last_batch_duration = time.monotonic() - batch_started
                        if self.last_cost_event_at is None or batch_started - self.last_cost_event_at >= 5.0:
                            self.recorder._event({'type': 'capture_performance', 'schema_version': SCHEMA_VERSION,
                                'session_id': self.recorder.session_id, 'sample_id': sample_id,
                                'timestamp_utc': utc_now(), 'costs_seconds': dict(costs),
                                'batch_duration_seconds': self.last_batch_duration,
                                'uncertainty': 'wall times include contention and exclude the sleep interval'})
                            self.last_cost_event_at = batch_started
            time.sleep(self.interval)
