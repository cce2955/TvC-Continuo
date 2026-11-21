"""Export stage resource tags from the DOL table used by path-builder case 12."""
import csv,struct,zipfile
from pathlib import Path
with zipfile.ZipFile('TvC_Recomp Package.zip') as z: dol=z.read('main.dol')
sections=list(zip(struct.unpack_from('>18I',dol,0),struct.unpack_from('>18I',dol,0x48),struct.unpack_from('>18I',dol,0x90)))
def loc(a):
    return next(o+a-v for o,v,n in sections if v<=a<v+n)
out=Path('research/agent_mapping/native_stage_resource_tags.csv')
with out.open('w',encoding='utf-8',newline='') as f:
    w=csv.writer(f); w.writerow(('result_index','tag','tag_runtime_address'))
    for i in range(19):
        p=struct.unpack_from('>I',dol,loc(0x80369158+i*4))[0]; x=loc(p)
        w.writerow((i,dol[x:dol.index(0,x)].decode('ascii',errors='replace'),f'0x{p:08x}'))
print('Wrote',out)
