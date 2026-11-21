"""Extract unverified system-action candidates from the existing profile inventory."""
from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / 'research/agent_mapping'
SOURCE = ROOT / 'compiled_frame_profiles.csv'
DESTINATION = ROOT / 'system_action_candidates.csv'
ACTION_LABELS = {
    51: 'standing pushblock candidate', 52: 'air pushblock candidate',
    53: 'crouching pushblock candidate', 420: 'cross-counter tag-out candidate',
    421: 'VAR candidate', 423: 'VAR tag-in candidate', 427: 'cross-counter tag-in candidate',
}


def main():
    count = 0
    with SOURCE.open(encoding='utf-8', newline='') as source, DESTINATION.open('w', encoding='utf-8', newline='') as destination:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(destination, fieldnames=['system_action_candidate'] + reader.fieldnames)
        writer.writeheader()
        for row in reader:
            try:
                label = ACTION_LABELS.get(int(row['move_id_decimal']))
            except (TypeError, ValueError):
                continue
            if label:
                writer.writerow({'system_action_candidate': label, **row})
                count += 1
    print(f'Wrote {count} unverified system-action rows to {DESTINATION}')


if __name__ == '__main__':
    main()
