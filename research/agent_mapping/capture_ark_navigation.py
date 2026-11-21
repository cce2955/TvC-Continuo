"""Read-only stage-carousel transition capture; no hooks or game writes.

Run from the repository root with .venv/Scripts/python.exe. While capturing,
move across Training/Ark in both directions with the Ark option enabled. Polls
can miss intermediate values and the controller word is a candidate, not a
proven mapping for this menu.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tdp-modules'))
from tvcgui.platform import dolphin as mem
from tvcgui.features.stage_select import runtime, yami_tile


def stamp():
    return datetime.now(timezone.utc).isoformat()


def run(path, seconds, interval):
    mem.dme.hook()
    if not mem.dme.is_hooked():
        raise RuntimeError('Dolphin Memory Engine did not attach; no game memory was read')
    deadline = time.monotonic() + seconds
    last = None
    menu_key = None
    with path.open('x', encoding='utf-8', newline='\n') as f:
        def emit(obj):
            f.write(json.dumps(obj, ensure_ascii=False) + '\n')
            f.flush()
        emit({'type':'metadata', 'timestamp_utc':stamp(), 'method':'read-only sequential polling',
              'interval_seconds':interval, 'duration_limit_seconds':seconds,
              'note':'No frame ID, write, hook or execution trace. Input mapping is unverified.'})
        while time.monotonic() < deadline:
            menu = yami_tile._read_live_menu()
            if not menu:
                sample = {'menu':'unavailable'}
            else:
                base, root = menu
                if menu_key != menu:
                    # These are loaded-container-relative offsets, not DOL offsets.
                    regions = {}
                    for offset in (0x25b40, 0x260c0):
                        data = mem.rbytes(base + offset, 0xc0)
                        regions[hex(offset)] = data.hex() if data and len(data) == 0xc0 else None
                    emit({'type':'menu_observed', 'timestamp_utc':stamp(), 'base':base, 'root':root,
                          'region_offsets_are':'loaded container relative', 'regions':regions,
                          'patch_bytes':{hex(off):mem.rd8(base+off) for off in yami_tile._PATCH}})
                    menu_key = menu
                blob = mem.rbytes(root, 0x100)
                sample = {'menu':'validated', 'base':base, 'root':root,
                          'cursor_global':mem.rd32(runtime.STAGE_GLOBAL_SELECTION),
                          'cursor_object':mem.rd32(root + runtime.RUNTIME_SELECTION_OFF),
                          'stage_result':mem.rd32(runtime.STAGE_RESULT_ID),
                          'input_word_candidate':mem.rd32(0x80473878),
                          'root_words':{f'+0x{off:02x}':int.from_bytes(blob[off:off+4], 'big')
                                        for off in range(0, 0x100, 4)} if blob and len(blob)==0x100 else None}
            if sample != last:
                emit({'type':'observation', 'timestamp_utc':stamp(), **sample})
                last = sample
            time.sleep(interval)
        emit({'type':'end', 'timestamp_utc':stamp()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=None, help='new file; existing files are never overwritten')
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--interval', type=float, default=0.025)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120 or not 0.01 <= args.interval <= 1:
        parser.error('seconds must be 1..120 and interval 0.01..1')
    if args.output is None:
        args.output = ROOT / 'research/agent_mapping' / ('ark_navigation_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.jsonl')
    try: run(args.output, args.seconds, args.interval)
    except (OSError, RuntimeError) as exc:
        print('Capture unavailable:', exc, file=sys.stderr)
        return 1
    print('Saved read-only observations to', args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
