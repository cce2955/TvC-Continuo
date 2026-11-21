import struct,zipfile
from pathlib import Path
with zipfile.ZipFile('TvC_Recomp Package.zip') as z:d=z.read('main.dol')
o=struct.unpack_from('>18I',d,0);v=struct.unpack_from('>18I',d,0x48);n=struct.unpack_from('>18I',d,0x90)
targets=[0x803f5640,0x803f5643,0x803f564b,0x803f564f,0x803f5663]
out=[]
for off,base,size in zip(o[:7],v[:7],n[:7]):
 if not size:continue
 w=struct.unpack_from('>%dI'%(size//4),d,off)
 for i,x in enumerate(w):
  if x>>26 !=15 or (x>>16)&31 != 0:continue
  hi=(x&65535)<<16; reg=(x>>21)&31
  if hi not in (0x803f0000,0x80400000):continue
  for j in range(i+1,min(i+18,len(w))):
   y=w[j]; op=y>>26; ra=(y>>16)&31; imm=y&65535; signed=imm if imm<32768 else imm-65536
   if ra!=reg:continue
   value=(hi+signed)&0xffffffff
   if any(abs(value-t)<=0x100 for t in targets) and op in (14,32,34,36,38,40,42):
    out.append(f'{base+i*4:08x} {base+j*4:08x} opcode={op} base=0x{value:08x} reg={(y>>21)&31} word={y:08x}')
Path('research/agent_mapping/cpu_global_scan.txt').write_text('\n'.join(out)+'\n',encoding='utf-8')
print('matches',len(out));print('\n'.join(out[:50]))
