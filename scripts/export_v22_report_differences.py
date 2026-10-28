"""Independent derived metadata; never changes existing report contracts."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.report_v22.report_difference import ReportDifference,build_report_difference
from app.report_v22.review_report import build_verified_review_report
from tests.test_v22_review_generation import generation
from tests.test_v22_review_decisions import item,evidence_for
from tests.support.review_projection_fixtures import bundle_raw
from tests.test_v22_report_difference import later_generation,observed_evidence

def artifacts():
 cases={}
 for name,kind in [('baseline','continue'),('updated','adjust'),('removed','defer'),('added','new')]:
  first=build_verified_review_report(generation())
  if name=='baseline':v=generation();r=first;prior=None
  else:v,r=later_generation(first,kind);prior=first
  cases[name]=dict(report=r.model_dump(mode='json'),difference=build_report_difference(v,r,prior).model_dump(mode='json'))
 v=generation(saved=observed_evidence());r=build_verified_review_report(v)
 cases['observed']=dict(report=r.model_dump(mode='json'),difference=build_report_difference(v,r).model_dump(mode='json'))
 v2,r2=later_generation(r)
 cases['unselected']=dict(report=r2.model_dump(mode='json'),difference=build_report_difference(v2,r2,r).model_dump(mode='json'))
 return {'schema.json':ReportDifference.model_json_schema(mode='serialization'),'fixture.json':dict(kind='synthetic_report_differences',cases=cases)}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--frontend-dir',type=Path);p.add_argument('--check',action='store_true');a=p.parse_args()
 roots=[ROOT/'contracts/v2.2-report-differences']
 if a.frontend_dir:
  if not (a.frontend_dir/'package.json').exists():raise SystemExit('Invalid frontend root')
  roots.append(a.frontend_dir/'src/lib/review-differences/contracts')
 for root in roots:
  for name,value in artifacts().items():
   file=root/name;data=(json.dumps(value,sort_keys=True,indent=2)+'\n').encode()
   if a.check:
    if not file.is_file() or file.read_bytes()!=data:raise SystemExit('Report difference contract drift')
   else:root.mkdir(parents=True,exist_ok=True);file.write_bytes(data)
 print(json.dumps(dict(ok=True,check=a.check,destinations=len(roots))))
