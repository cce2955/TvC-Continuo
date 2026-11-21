"""List existing Ghidra function names matching terms, without modifying them."""
import argparse
from pathlib import Path
import ghidra_bridge

p = argparse.ArgumentParser()
p.add_argument('terms', nargs='+')
p.add_argument('--output', default='research/agent_mapping/function_name_hits.txt')
a = p.parse_args()
b = ghidra_bridge.GhidraBridge(response_timeout=120)
if b.remote_eval('currentProgram.getName()') != 'main.dol':
    raise RuntimeError('Expected main.dol')
b.remote_exec('''
def search_function_names(terms):
    result = []
    for f in currentProgram.getFunctionManager().getFunctions(True):
        name = str(f.getName())
        for term in terms:
            if term.lower() in name.lower():
                result.append((str(f.getEntryPoint()), name))
                break
    return result
''')
data = b.remote_eval('search_function_names(terms)', terms=a.terms)
Path(a.output).write_text('\n'.join(f'{addr} {name}' for addr, name in data), encoding='utf-8')
print(f'Saved {len(data)} names to {a.output}')
