"""Export the 33 contiguous native character-tag pointers from the archived DOL."""
import csv
import struct
import zipfile
from pathlib import Path

with zipfile.ZipFile('TvC_Recomp Package.zip') as archive:
    dol = archive.read('main.dol')
sections = list(zip(struct.unpack_from('>18I', dol, 0),
                    struct.unpack_from('>18I', dol, 0x48),
                    struct.unpack_from('>18I', dol, 0x90)))

def file_offset(address):
    for offset, base, size in sections:
        if base <= address < base + size:
            return offset + address - base
    raise ValueError(f'0x{address:08x} is outside initialized DOL sections')

out = Path('research/agent_mapping/native_character_resource_tags.csv')
with out.open('w', encoding='utf-8', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(('index', 'tag', 'tag_runtime_address'))
    for index in range(33):
        pointer = struct.unpack_from('>I', dol, file_offset(0x803690d0 + index*4))[0]
        start = file_offset(pointer)
        tag = dol[start:dol.index(0, start)].decode('ascii', errors='replace')
        writer.writerow((index, tag, f'0x{pointer:08x}'))
print(f'Wrote {out}')
