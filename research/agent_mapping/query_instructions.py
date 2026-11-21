"""Read instructions and containing function at DOL file offsets."""
import argparse
import ghidra_bridge

p = argparse.ArgumentParser()
p.add_argument('offsets', nargs='+', help='DOL file offsets')
p.add_argument('--count', type=int, default=24)
a = p.parse_args()
b = ghidra_bridge.GhidraBridge()
if b.remote_eval('currentProgram.getName()') != 'main.dol':
    raise RuntimeError('Expected main.dol')
b.remote_exec('''
def inspect_instructions(off, count):
    addr = toAddr(off)
    f = currentProgram.getFunctionManager().getFunctionContaining(addr)
    ins = currentProgram.getListing().getInstructionAt(addr)
    lines = ['OFFSET ' + str(addr) + ' FUNCTION ' + str(f)]
    for i in range(count):
        if ins is None:
            break
        lines.append(str(ins.getAddress()) + ' ' + str(ins))
        ins = ins.getNext()
    return '\\n'.join(lines)
''')
for off in a.offsets:
    print(b.remote_eval('inspect_instructions(off,count)', off=off, count=a.count))
