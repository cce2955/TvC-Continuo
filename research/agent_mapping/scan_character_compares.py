"""Find nearby PPC field loads followed by a comparison with a chosen ID.

Matches are leads only: a +0x14 field can belong to any structure.
"""
import argparse
from pathlib import Path
import ghidra_bridge

p = argparse.ArgumentParser()
p.add_argument('value', type=lambda s: int(s, 0))
p.add_argument('--field', default='0x14')
p.add_argument('--output', default='research/agent_mapping/character_compare_hits.txt')
a = p.parse_args()
b = ghidra_bridge.GhidraBridge(response_timeout=180)
if b.remote_eval('currentProgram.getName()') != 'main.dol':
    raise RuntimeError('Expected main.dol')
b.remote_exec('''
import re
def scan_character_compare(field, value):
    listing = currentProgram.getListing()
    fm = currentProgram.getFunctionManager()
    it = listing.getInstructions(True)
    out = []
    pattern = re.compile(r'^lwz (r[0-9]+),' + re.escape(field) + r'\\(r[0-9]+\\)$')
    while it.hasNext():
        ins = it.next()
        match = pattern.match(str(ins))
        if match is None:
            continue
        reg = match.group(1)
        nxt = ins
        lines = []
        for i in range(8):
            if nxt is None:
                break
            lines.append(str(nxt.getAddress()) + ' ' + str(nxt))
            s = str(nxt)
            if s in ('cmpwi ' + reg + ',' + hex(value), 'cmplwi ' + reg + ',' + hex(value)):
                out.append('FUNCTION ' + str(fm.getFunctionContaining(ins.getAddress())) + '\\n' + '\\n'.join(lines))
                break
            nxt = nxt.getNext()
    return '\\n\\n'.join(out)
''')
result = b.remote_eval('scan_character_compare(field,value)', field=a.field, value=a.value)
Path(a.output).write_text(result, encoding='utf-8')
print(f'Saved {result.count("FUNCTION ")} candidate windows to {a.output}')
