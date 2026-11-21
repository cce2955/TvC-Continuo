"""Compile existing profile rows using Continuo's CSV serializer, without live scans."""
import csv, json, sys
from pathlib import Path
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/'tdp-modules'))
from tvcgui.features.frame_data.spreadsheet_export import CSV_FIELDS, FrameDataSpreadsheetExporter
sources=root/'tdp-modules/data/frame_data/frame_data_profiles'
out=root/'research/agent_mapping/compiled_frame_profiles.csv'
counts={}
with out.open('w',encoding='utf-8',newline='') as f:
 w=csv.DictWriter(f,fieldnames=('source_profile','source_profile_updated_at','raw_startup','timing_evidence_status',*CSV_FIELDS)); w.writeheader()
 for p in sorted(sources.glob('*.json')):
  data=json.loads(p.read_text(encoding='utf-8-sig'))
  slot={'char_id':data.get('char_id'), 'char_name':data.get('char_name'), 'profile_key':data.get('key'), 'data_origin':'existing stored profile'}
  rows=0
  for move in data.get('moves',[]):
   if not isinstance(move,dict):continue
   row=FrameDataSpreadsheetExporter._build_row(None,slot,move,seen_utc=str(data.get('updated_at') or ''))
   row['source_profile']=p.name;row['source_profile_updated_at']=data.get('updated_at') or ''
   row['raw_startup']=move.get('startup') if move.get('startup') is not None else ''
   row['timing_evidence_status']='unverified profile values; active_start is not proven startup'
   if move.get('active_start') is None or move.get('recovery') is None or move.get('blockstun') is None:
    row['timing_evidence_status']+='; incomplete timing inputs; advantage value not validated'
   w.writerow(row);rows+=1
  counts[p.name]=rows
print(f'{len(counts)} profiles; {sum(counts.values())} rows; {out.stat().st_size} bytes; {out}')
