"""Find matching defined strings and incoming references in the flat DOL."""
import argparse
from pathlib import Path
import ghidra_bridge

p = argparse.ArgumentParser()
p.add_argument('terms', nargs='+')
p.add_argument('--output', default='research/agent_mapping/string_hits.txt')
a = p.parse_args()
b = ghidra_bridge.GhidraBridge(response_timeout=180)
if b.remote_eval('currentProgram.getName()') != 'main.dol':
    raise RuntimeError('Expected main.dol')
b.remote_exec('''
def find_strings(terms):
    lines = []
    it = currentProgram.getListing().getDefinedData(True)
    rm = currentProgram.getReferenceManager()
    while it.hasNext():
        d = it.next()
        if d.hasStringValue():
            s = unicode(d.getValue()).encode('ascii', 'replace')
            if any(t.lower() in s.lower() for t in terms):
                refs = ','.join(str(r.getFromAddress()) for r in rm.getReferencesTo(d.getAddress()))
                lines.append(str(d.getAddress()) + ' | ' + s[:150] + ' | ' + refs)
    return '\\n'.join(lines)
''')
result = b.remote_eval('find_strings(terms)', terms=a.terms)
Path(a.output).write_text(result, encoding='utf-8')
print(f'Saved {len(result.splitlines())} matching strings to {a.output}')
