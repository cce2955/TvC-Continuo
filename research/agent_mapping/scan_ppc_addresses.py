"""Locate simple PPC lis/addi address constructions in archived original DOL.
Candidates are instruction evidence only: register overwrites are not modeled.
"""
import argparse, struct, zipfile
p=argparse.ArgumentParser()
p.add_argument('low',type=lambda x:int(x,0)); p.add_argument('high',type=lambda x:int(x,0))
p.add_argument('--output',required=True); a=p.parse_args()
with zipfile.ZipFile('TvC_Recomp Package.zip') as z: d=z.read('main.dol')
o=struct.unpack_from('>18I',d,0); v=struct.unpack_from('>18I',d,0x48); sizes=struct.unpack_from('>18I',d,0x90)
rows=[]
for off,base,size in zip(o[:7],v[:7],sizes[:7]):
    if not size: continue
    w=struct.unpack_from('>%dI'%(size//4),d,off)
    for i,x in enumerate(w):
        if x>>26!=15 or ((x>>16)&31)!=0: continue
        reg=(x>>21)&31; upper=(x&65535)<<16
        for j in range(i+1,min(i+65,len(w))):
            y=w[j]; op=y>>26
            if op not in (14,24): continue
            src=(y>>16)&31
            if src!=reg: continue
            imm=y&65535
            value=(upper+(imm-65536 if imm&32768 else imm))&0xffffffff if op==14 else upper|imm
            if a.low<=value<=a.high: rows.append(f'{base+i*4:08x} {base+j*4:08x} {value:08x} {x:08x} {y:08x}')
open(a.output,'w',encoding='utf8').write('\n'.join(rows)+'\n')
print('saved',len(rows),'candidates')
