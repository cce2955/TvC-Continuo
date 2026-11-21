"""Export original-DOL dispatch tables and compare against the open flat Ghidra image."""
import csv, struct, zipfile
from pathlib import Path
import ghidra_bridge
root=Path(__file__).resolve().parents[2]
with zipfile.ZipFile(root/'TvC_Recomp Package.zip') as z:d=z.read('main.dol')
o=struct.unpack_from('>18I',d,0); v=struct.unpack_from('>18I',d,0x48); n=struct.unpack_from('>18I',d,0x90)
def file_offset(address,size):
 for off,base,length in zip(o,v,n):
  if length and base<=address and address+size<=base+length:return off+address-base
 raise ValueError(hex(address))
b=ghidra_bridge.GhidraBridge(response_timeout=30)
if b.remote_eval('currentProgram.getName()')!='main.dol':raise RuntimeError('Expected main.dol')
for name,addr,count in [('cpu_sequence_dispatch_table.csv',0x8036db30,256),('cpu_candidate_dispatch_table.csv',0x80366868,25)]:
 off=file_offset(addr,count*4)
 words=struct.unpack_from('>'+str(count)+'I',d,off)
 remote=b.remote_eval('[getInt(toAddr(start + 4*i)) & 0xffffffff for i in range(count)]',start=off,count=count)
 mismatch=[i for i,(x,y) in enumerate(zip(words,remote)) if x!=y]
 if len(remote)!=count or mismatch:raise RuntimeError(f'{name}: Ghidra mismatch at {mismatch[:8]}')
 output=root/'research/agent_mapping'/name
 with output.open('w',encoding='utf8',newline='') as f:
  w=csv.writer(f);w.writerow(['index','table_runtime_address','dol_file_offset','case_wii_address','evidence'])
  for i,word in enumerate(words):w.writerow([f'0x{i:02x}',f'0x{addr+4*i:08x}',f'0x{off+4*i:08x}',f'0x{word:08x}','Ghidra flat image and archived DOL agree; case meaning not automatically established'])
 print(name,count,'entries verified')
