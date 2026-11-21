"""Search unaligned big-endian byte patterns in the currently loaded flat file."""
import argparse
import ghidra_bridge

p = argparse.ArgumentParser()
p.add_argument('values', nargs='+', help='32-bit values, e.g. 0x8053cb20')
a = p.parse_args()
b = ghidra_bridge.GhidraBridge(response_timeout=120)
if b.remote_eval('currentProgram.getName()') != 'main.dol':
    raise RuntimeError('Expected main.dol')
b.remote_exec('''
from jarray import zeros
def scan_word(value):
    mem = currentProgram.getMemory()
    start = currentProgram.getMinAddress()
    size = int(currentProgram.getMaxAddress().subtract(start)) + 1
    raw = zeros(size, 'b')
    mem.getBytes(start, raw)
    needle = [(value >> shift) & 255 for shift in (24,16,8,0)]
    matches = []
    for i in xrange(size - 3):
        if (raw[i] & 255) == needle[0] and all((raw[i+j] & 255) == needle[j] for j in xrange(1,4)):
            matches.append(hex(i))
    return matches
''')
for value in a.values:
    print(value, b.remote_eval('scan_word(value)', value=int(value, 0)))
