"""Find possible writes near constructions of the native descriptor base.
Candidates require register-flow verification; no Ghidra changes are made.
"""
import struct,zipfile
from pathlib import Path
with zipfile.ZipFile('TvC_Recomp Package.zip') as z:d=z.read('main.dol')
o=struct.unpack_from('>18I',d,0);v=struct.unpack_from('>18I',d,0x48);n=struct.unpack_from('>18I',d,0x90)
rows=[]
for off,base,size in zip(o[:7],v[:7],n[:7]):
 if not size:continue
 words=struct.unpack_from('>%dI'%(size//4),d,off)
 for i,x in enumerate(words):
  if x>>26!=15 or x&65535!=0x803f or (x>>16)&31:continue
  r=(x>>21)&31
  for j in range(i+1,min(i+15,len(words))):
   y=words[j]
   if y>>26==14 and (y>>16)&31==r and y&65535 in (0xb7d8,0xb7f0):
    reg=(y>>21)&31
    for k in range(j+1,min(j+75,len(words))):
     z=words[k]
     if z>>26 in (36,37) and (z>>16)&31==reg and (z&65535) in (0x1c,0x20,0x34,0x38):
      rows.append(f'{base+j*4:08x} {base+k*4:08x} base-r{reg} stw-offset={z&65535:04x} word={z:08x}')
Path('research/agent_mapping/pass4_descriptor_writer_candidates.txt').write_text('\n'.join(rows)+'\n',encoding='utf-8')
print('saved',len(rows),'candidates')
