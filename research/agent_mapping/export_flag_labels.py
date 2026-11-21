"""Export native debug label tables from the archived DOL, without extraction."""
import csv
import struct
import zipfile
from pathlib import Path

archive_path = Path('TvC_Recomp Package.zip')
output_path = Path('research/agent_mapping/native_flag_labels.csv')
with zipfile.ZipFile(archive_path) as archive:
    dol = archive.read('main.dol')
file_offsets = struct.unpack_from('>18I', dol, 0)
runtime_bases = struct.unpack_from('>18I', dol, 0x48)
sizes = struct.unpack_from('>18I', dol, 0x90)

def file_offset(address):
    for offset, base, size in zip(file_offsets, runtime_bases, sizes):
        if base <= address < base + size:
            return offset + address - base
    raise ValueError(f'0x{address:08x} is outside initialized DOL sections')

with output_path.open('w', newline='', encoding='utf-8') as output:
    writer = csv.writer(output)
    writer.writerow(('hit_data_offset', 'bit', 'mask', 'native_label', 'label_runtime_address'))
    for field, table in ((0x1c, 0x803689c0), (0x20, 0x80368a40)):
        table_offset = file_offset(table)
        for bit in range(32):
            address = struct.unpack_from('>I', dol, table_offset + bit * 4)[0]
            start = file_offset(address)
            end = dol.index(0, start)
            label = dol[start:end].decode('ascii', errors='replace').strip()
            writer.writerow((f'0x{field:02x}', bit, f'0x{1 << bit:08x}', label, f'0x{address:08x}'))
print(f'Wrote 64 labels to {output_path}')
