"""Read-only scan for rendered instruction fragments in recognized functions."""
import argparse
from pathlib import Path
import ghidra_bridge

p = argparse.ArgumentParser()
p.add_argument('fragments', nargs='+', help='case-insensitive instruction substrings')
p.add_argument('--output', default='research/agent_mapping/instruction_hits.txt')
a = p.parse_args()
b = ghidra_bridge.GhidraBridge(response_timeout=180)
if b.remote_eval('currentProgram.getName()') != 'main.dol':
    raise RuntimeError('Expected main.dol')
b.remote_exec('''
def scan_fragments(fragments):
    hits = []
    listing = currentProgram.getListing()
    fm = currentProgram.getFunctionManager()
    it = listing.getInstructions(True)
    while it.hasNext():
        ins = it.next()
        s = str(ins).lower()
        if any(x.lower() in s for x in fragments):
            f = fm.getFunctionContaining(ins.getAddress())
            hits.append(str(ins.getAddress()) + ' | ' + str(f) + ' | ' + str(ins))
    return '\\n'.join(hits)
''')
result = b.remote_eval('scan_fragments(parts)', parts=a.fragments)
Path(a.output).write_text(result, encoding='utf-8')
print(f'Saved {len(result.splitlines())} hits to {a.output}')
