"""One-shot diagnostic for TvC Training-vs-Versus Baroque behavior.

Run from the tvcgui project directory with Dolphin in a stable live match.
This is NOT a game-mode classifier until the readouts are validated.
Only writes the known Training BaroquePct control byte; always restores it.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from tvcgui.platform.dolphin import rd8, rd32, wd8
from tvcgui.core.constants import (SLOTS, OFF_CHAR_ID, OFF_MAX_HP,
                                    OFF_CUR_HP, OFF_AUX_HP)
from tvcgui.features.training.flags import TR_BAROQUE_PCT_ADDR

BAROQUE_ACTIVE_OFFSET = 0x44BC
BAROQUE_RED_SPENT_OFFSET = 0x454C
SAFE_RANGES = ((0x80000000, 0x81800000), (0x90000000, 0x94000000))


def read_u32(addr):
    try:
        val = rd32(addr)
        return int(val) if val is not None else None
    except Exception:
        return None


def snapshot():
    fighters = {}
    for slot, pointer, _ in SLOTS:
        base = read_u32(pointer)
        if base is None or not any(lo <= base < hi - 0x4600 for lo, hi in SAFE_RANGES):
            continue
        maximum = read_u32(base + OFF_MAX_HP)
        current = read_u32(base + OFF_CUR_HP)
        aux = read_u32(base + OFF_AUX_HP)
        if maximum is None or not 0 < maximum <= 80000:
            continue
        fighters[slot] = {
            'base': f'{base:#010x}', 'character_id': read_u32(base + OFF_CHAR_ID),
            'maximum_hp': maximum, 'current_hp': current, 'aux_hp': aux,
            'recoverable_hp': max(0, aux-current) if aux is not None and current is not None else None,
            'baroque_active': read_u32(base + BAROQUE_ACTIVE_OFFSET),
            'baroque_red_spent': read_u32(base + BAROQUE_RED_SPENT_OFFSET),
        }
    return fighters


def safe_read_flag():
    try:
        val = rd8(TR_BAROQUE_PCT_ADDR)
        return int(val) if val is not None else None
    except Exception:
        return None


def main():
    if len(sys.argv) != 2 or sys.argv[1].lower() not in ('training', 'versus'):
        raise SystemExit('Usage: python baroque_mode_probe.py training|versus')
    label = sys.argv[1].lower()
    original = safe_read_flag()
    if original is None or not 0 <= original <= 10:
        raise SystemExit(f'Cannot safely read BaroquePct (value={original!r}). No writes performed.')
    first = snapshot()
    if not first:
        raise SystemExit('No valid fighter structs found. No writes performed.')
    data = {
        'mode_as_selected_by_user': label,
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'original_flag': original,
        'samples': [{'setting': original, 'flag_readback': original, 'fighters': first}],
    }
    restore_error = None
    try:
        for setting in (3, 7):  # 30%, then 70%; 0..10 is 0..100%
            wd8(TR_BAROQUE_PCT_ADDR, setting)
            time.sleep(0.30)
            readback = safe_read_flag()
            data['samples'].append({
                'setting': setting, 'flag_readback': readback, 'fighters': snapshot(),
            })
    finally:
        try:
            wd8(TR_BAROQUE_PCT_ADDR, original)
            time.sleep(0.15)
            restore_actual = safe_read_flag()
            if restore_actual != original:
                restore_error = f'Restored readback {restore_actual!r}, expected {original!r}'
        except Exception as exc:
            restore_error = f'Restore write failed: {exc!r}'
        data['restored_flag_readback'] = safe_read_flag()
        data['restore_error'] = restore_error
        data['samples'].append({'setting': 'restored', 'flag_readback': safe_read_flag(),
                                'fighters': snapshot()})
    output = Path(f'baroque_probe_{label}.json')
    output.write_text(json.dumps(data, indent=2), encoding='utf-8')
    print(f'Saved {output.resolve()}')
    print('Flag changes:', [(s['setting'], s['flag_readback']) for s in data['samples']])
    if restore_error:
        print('WARNING:', restore_error)
    else:
        print('Original BaroquePct restored and verified.')
    print('This is diagnostic telemetry, NOT yet proof of the game mode.')


if __name__ == '__main__':
    main()
