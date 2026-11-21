import csv

from tvcgui.features.frame_data.frame_map import build_map, export_map


def test_tatsu_retains_each_candidate_without_inventing_strength_or_startup():
    moves = [
        {'id': 307, 'move_name': 'Tatsu L', 'active_start': 15, 'animation_total_frames': 16},
        {'id': 274, 'move_name': 'Tatsu Spin', 'active_start': 12, 'recovery': None},
        {'id': 274, 'move_name': 'Tatsu Spin', 'active_start': 18, 'family_strength_guess': 'M'},
        {'id': 310, 'move_name': 'Shoryu'},
    ]
    rows = build_map('Ryu', 12, moves, moves[0])
    assert len(rows) == 3
    assert [r['active_start_candidate'] for r in rows] == [15, 12, 18]
    assert rows[0]['strength'] == 'Ground L'
    assert rows[1]['strength'] == 'Unknown'
    assert rows[2]['strength'] == 'M (candidate)'
    assert all('unverified' in row['evidence'] for row in rows)
    assert 'startup' not in rows[0]


def test_system_action_group_and_export_preserve_unknown_and_provenance(tmp_path):
    moves = [
        {'id': 420, 'move_name': 'Cross Counter Tag out', 'blockstun': 0, 'source': 'table'},
        {'id': 427, 'move_name': 'Cross Counter Tag in', 'adv_block_observed': -3, 'adv_block_derived': -12},
        {'id': 421, 'move_name': 'VAR'},
    ]
    rows = build_map('Ryu', 12, moves, moves[0])
    assert len(rows) == 2
    output = tmp_path / 'map.csv'
    export_map(output, rows)
    with output.open(encoding='utf-8', newline='') as handle:
        saved = list(csv.DictReader(handle))
    assert saved[0]['blockstun_candidate'] == '0'
    assert saved[0]['recovery_candidate'] == ''
    assert saved[0]['source'] == 'table'
    assert saved[1]['block_advantage_observed'] == '-3'
    assert saved[1]['block_advantage_derived'] == '-12'


def test_empty_selection_and_unrelated_character():
    moves = [{'id': 307, 'move_name': 'Entry'}, {'id': 274, 'move_name': 'Unrelated'}]
    assert build_map('Other', 1, moves, None) == []
    assert len(build_map('Other', 1, moves, moves[0])) == 1
