"""Export the two US stream path pointer tables from the archived DOL."""
import csv
import struct
import zipfile
from pathlib import Path

with zipfile.ZipFile('TvC_Recomp Package.zip') as archive:
    dol = archive.read('main.dol')
sections = zip(struct.unpack_from('>18I', dol, 0),
               struct.unpack_from('>18I', dol, 0x48),
               struct.unpack_from('>18I', dol, 0x90))
sections = list(sections)

def file_offset(address):
    for offset, base, size in sections:
        if base <= address < base + size:
            return offset + address - base
    raise ValueError(f'Address 0x{address:08x} is outside initialized sections')

out = Path('research/agent_mapping/native_audio_stream_paths.csv')
with out.open('w', encoding='utf-8', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(('table', 'index', 'table_runtime_address', 'path_runtime_address', 'path'))
    for name, base, count in (('bgm', 0x8038af50, 0x41), ('voice', 0x8038b058, 0x3c)):
        for i in range(count):
            pointer = struct.unpack_from('>I', dol, file_offset(base + 4*i))[0]
            if pointer:
                start = file_offset(pointer)
                path = dol[start:dol.index(0, start)].decode('ascii', errors='replace')
            else:
                path = ''
            writer.writerow((name, i, f'0x{base + 4*i:08x}', f'0x{pointer:08x}', path))
print(f'Wrote {out}')
