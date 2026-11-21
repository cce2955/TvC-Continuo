"""Export the bundled FPK-derived MOT inventory without interpreting combat timing."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'tdp-modules/data/animation/animation_frames.json'
DESTINATION = ROOT / 'research/agent_mapping/animation_inventory.csv'
FIELDS = ('character_key', 'motion_bank', 'database_action_id', 'clip_offset',
          'duration_field_offset', 'duration_seconds', 'total_frames', 'fps',
          'source_fpk', 'source_entry', 'note')


def main():
    document = json.loads(SOURCE.read_text(encoding='utf-8'))
    count = 0
    with DESTINATION.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for character, data in sorted(document['characters'].items()):
            for bank, motion in sorted(data.get('motions', {}).items()):
                for action, record in sorted(motion.get('actions', {}).items()):
                    writer.writerow({
                        'character_key': character, 'motion_bank': bank, 'database_action_id': action,
                        'clip_offset': record.get('clip_offset'),
                        'duration_field_offset': record.get('duration_field_offset'),
                        'duration_seconds': record.get('duration_seconds'),
                        'total_frames': record.get('total_frames'),
                        'fps': motion.get('fps', document.get('fps')),
                        'source_fpk': motion.get('source_fpk'), 'source_entry': motion.get('source_entry'),
                        'note': 'Animation clip inventory; action binding and combat timing require validation',
                    })
                    count += 1
    print(f'Wrote {count} animation records to {DESTINATION}')


if __name__ == '__main__':
    main()
