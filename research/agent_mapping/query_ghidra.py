"""Read-only focused Ghidra query; run with the Python hosting ghidra_bridge."""
import argparse
from pathlib import Path
import ghidra_bridge

parser = argparse.ArgumentParser()
parser.add_argument('addresses', nargs='+', help='Wii executable runtime addresses in DOL text sections')
parser.add_argument('--output', default='research/agent_mapping/ghidra_excerpt.txt')
args = parser.parse_args()
b = ghidra_bridge.GhidraBridge()
if b.remote_eval('currentProgram.getName()') != 'main.dol':
    raise RuntimeError('Expected main.dol; refusing to query another program')
b.remote_exec('''
from ghidra.app.decompiler import DecompInterface
from ghidra.util.task import ConsoleTaskMonitor
di = DecompInterface()
di.openProgram(currentProgram)
fm = currentProgram.getFunctionManager()
rm = currentProgram.getReferenceManager()
def inspect_at(s):
    a = toAddr(s)
    f = fm.getFunctionContaining(a)
    if f is None:
        return 'No containing function for ' + s
    lines = ['QUERY ' + s + ' FUNCTION ' + str(f.getEntryPoint()) + ' ' + f.getName()]
    refs = rm.getReferencesTo(f.getEntryPoint())
    lines.append('ENTRY REFERENCES: ' + ', '.join(str(r.getFromAddress()) for r in refs))
    result = di.decompileFunction(f, 60, ConsoleTaskMonitor())
    if result.decompileCompleted():
        lines.append(result.getDecompiledFunction().getC())
    else:
        lines.append('DECOMPILE ERROR: ' + result.getErrorMessage())
    return '\\n'.join(lines)
''')
def dol_text_offset(value):
    a = int(value, 0)
    for runtime, offset, size in ((0x80004000, 0x100, 0x26a0),
                                  (0x800182c0, 0x27a0, 0x334540)):
        if runtime <= a < runtime + size:
            return offset + a - runtime
    raise ValueError(f'{value} is outside the documented DOL text sections')

resolved = [(address, dol_text_offset(address)) for address in args.addresses]
parts = []
for address, offset in resolved:
    parts.append(f'WII RUNTIME {address} -> DOL FILE OFFSET 0x{offset:08x}\n' +
                 b.remote_eval('inspect_at(addr)', addr=hex(offset)))
Path(args.output).write_text('\n\n'.join(parts), encoding='utf-8')
print(f'Saved {len(parts)} queries to {args.output}')
