import zipfile,re
from pathlib import Path
instruction=re.compile(r'\s*// ([0-9A-F]{8}):\s+(.*)')
allins=[]
with zipfile.ZipFile('TvC_Recomp Package.zip') as z:
 for name in z.namelist():
  if not name.endswith('.c'):continue
  for line in z.read(name).decode('utf8',errors='replace').splitlines():
   m=instruction.match(line)
   if m:allins.append((int(m[1],16),m[2]))
out=[]
for i,(addr,op) in enumerate(allins):
 m=re.match(r'lwz\s+r(\d+),\s*17156\(r(\d+)\)',op)
 if not m:continue
 reg=m[1]
 for a,op2 in allins[i+1:i+60]:
  if a-addr>240:break
  k=re.match(r'(stw|sth|stb)\s+r(\d+),\s*(12|216|276)\(r'+reg+r'\)',op2)
  if k:out.append((addr,a,op,op2))
Path('research/agent_mapping/arcade_cpu_linked_writers.txt').write_text('\n'.join(f'{x:08x} {y:08x} {z} | {w}' for x,y,z,w in out)+'\n',encoding='utf8')
print('candidate stores',len(out));print('\n'.join(f'{x:08x} {y:08x} {z} | {w}' for x,y,z,w in out[:100]))
