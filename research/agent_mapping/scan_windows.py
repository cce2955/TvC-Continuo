"""Find instruction windows containing both user-specified fragments."""
import argparse
from pathlib import Path
import ghidra_bridge

p = argparse.ArgumentParser()
p.add_argument('first')
p.add_argument('second')
p.add_argument('--count', type=int, default=25)
p.add_argument('--output', default='research/agent_mapping/window_hits.txt')
a = p.parse_args()
b = ghidra_bridge.GhidraBridge(response_timeout=180)
if b.remote_eval('currentProgram.getName()') != 'main.dol':
    raise RuntimeError('Expected main.dol')
b.remote_exec('''
def windows(first, second, count):
    listing = currentProgram.getListing()
    fm = currentProgram.getFunctionManager()
    it = listing.getInstructions(True)
    out = []
    while it.hasNext():
        ins = it.next()
        if first.lower() not in str(ins).lower():
            continue
        f = fm.getFunctionContaining(ins.getAddress())
        nxt = ins
        buf = []
        for n in range(count):
            if nxt is None or fm.getFunctionContaining(nxt.getAddress()) != f:
                break
            buf.append(str(nxt.getAddress()) + ' ' + str(nxt))
            nxt = nxt.getNext()
        if any(second.lower() in line.lower() for line in buf):
            out.append('FUNCTION ' + str(f) + '\\n' + '\\n'.join(buf))
    return '\\n\\n'.join(out)
''')
result = b.remote_eval('windows(first,second,count)', first=a.first, second=a.second, count=a.count)
Path(a.output).write_text(result, encoding='utf-8')
print(f'Saved {result.count("FUNCTION ")} windows to {a.output}')
