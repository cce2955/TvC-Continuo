"""Read big-endian float constants at r2-relative addresses from the DOL archive.

This does not extract, alter, or import the DOL. The r2 base is supported by
startup instructions at runtime 0x800064d8..0x800064dc. Pass signed offsets,
for example: py read_sda_constants.py -- -0x5a0c -0x5a08.
"""
import argparse
import struct
import zipfile

p = argparse.ArgumentParser()
p.add_argument('offsets', nargs='+')
p.add_argument('--archive', default='TvC_Recomp Package.zip')
a = p.parse_args()
with zipfile.ZipFile(a.archive) as archive:
    dol = archive.read('main.dol')
offsets = struct.unpack_from('>18I', dol, 0)
addresses = struct.unpack_from('>18I', dol, 0x48)
sizes = struct.unpack_from('>18I', dol, 0x90)
for offset in a.offsets:
    address = 0x8056a460 + int(offset, 0)
    for file_base, runtime_base, size in zip(offsets, addresses, sizes):
        if runtime_base <= address and address + 4 <= runtime_base + size:
            location = file_base + address - runtime_base
            bits = struct.unpack_from('>I', dol, location)[0]
            value = struct.unpack_from('>f', dol, location)[0]
            print(f'{offset}: runtime 0x{address:08x}, DOL offset 0x{location:08x}, '
                  f'bits 0x{bits:08x}, float32 {value!r}')
            break
    else:
        raise ValueError(f'0x{address:08x} is outside initialized DOL sections')
