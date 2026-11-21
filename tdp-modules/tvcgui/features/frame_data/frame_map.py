"""Read-only phase map derived from existing cached frame-data rows."""
from __future__ import annotations

import csv
from pathlib import Path

SYSTEM_GROUPS = (
    frozenset((51, 52, 53)),
    frozenset((420, 427)),
    frozenset((421, 423)),
)
RYU_TATSU = frozenset((273, 274, 275, 307, 308, 309, 323, 324, 325))
RYU_TATSU_ENTRIES = {307: 'Ground L', 308: 'Ground M', 309: 'Ground H',
                     323: 'Air L', 324: 'Air M', 325: 'Air H'}
MAP_FIELDS = ('character', 'move', 'action_id', 'phase', 'strength', 'active_start_candidate',
              'active_end_candidate', 'second_active_start_candidate', 'second_active_end_candidate',
              'animation_frames', 'recovery_candidate', 'recovery_source', 'hitstun_candidate',
              'blockstun_candidate', 'block_advantage_observed', 'block_advantage_derived',
              'hitstun_source', 'blockstun_source', 'source', 'record_address', 'evidence')


def _integer(value):
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None


def _related(selected, candidate, character_id):
    selected_id, candidate_id = _integer(selected.get('id')), _integer(candidate.get('id'))
    if selected_id is not None and selected_id == candidate_id:
        return True
    if character_id == 12 and selected_id in RYU_TATSU and candidate_id in RYU_TATSU:
        return True
    if any(selected_id in group and candidate_id in group for group in SYSTEM_GROUPS):
        return True
    family = selected.get('family_key')
    return bool(family and family == candidate.get('family_key'))


def build_map(character, character_id, moves, selected):
    """Keep every candidate row and its provenance; never infer a whole-move timeline."""
    if not isinstance(selected, dict):
        return []
    character_id = _integer(character_id)
    out = []
    for move in moves:
        if not isinstance(move, dict) or not _related(selected, move, character_id):
            continue
        action = _integer(move.get('id'))
        phase = move.get('family_phase') or move.get('move_name') or 'Unknown phase'
        strength = move.get('family_strength') or move.get('family_strength_guess') or (RYU_TATSU_ENTRIES.get(action) if character_id == 12 else None) or 'Unknown'
        if move.get('family_strength_guess'):
            strength = str(strength) + ' (candidate)'
        out.append({
            'character': str(character or 'Unknown'), 'move': str(move.get('move_name') or 'Unknown'),
            'action_id': f'0x{action:04X}' if action is not None and action >= 0 else '',
            'phase': str(phase), 'strength': str(strength),
            'active_start_candidate': move.get('active_start'), 'active_end_candidate': move.get('active_end'),
            'second_active_start_candidate': move.get('active2_start'), 'second_active_end_candidate': move.get('active2_end'),
            'animation_frames': move.get('animation_total_frames'), 'recovery_candidate': move.get('recovery'),
            'recovery_source': move.get('recovery_source') or 'Unknown',
            'hitstun_candidate': move.get('hitstun'), 'blockstun_candidate': move.get('blockstun'),
            'block_advantage_observed': move.get('adv_block_observed', move.get('observed_adv_block')),
            'block_advantage_derived': move.get('adv_block_derived'),
            'hitstun_source': move.get('hitstun_source') or move.get('stun_source') or 'Unknown',
            'blockstun_source': move.get('blockstun_source') or move.get('stun_source') or 'Unknown',
            'source': move.get('source') or move.get('profile_source') or 'Cached profile',
            'record_address': move.get('abs'),
            'evidence': 'Candidate cached values; action binding and combat timing unverified',
        })
    return out


def export_map(path, rows):
    with Path(path).open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=MAP_FIELDS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
