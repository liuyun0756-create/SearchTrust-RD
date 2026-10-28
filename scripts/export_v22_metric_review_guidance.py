"""Independent owner reading contract; preserves saved report/projection schemas."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.report_v22.metric_review_guidance import MetricReviewGuidance,metric_review_guidance
from tests.support.metric_review_fixtures import examples

def artifacts():
    return {'schema.json':MetricReviewGuidance.model_json_schema(mode='serialization'),
            'fixtures.json':{'kind':'synthetic_metric_review_cases_not_empirical_calibration',
            'cases':{name:{'facts':v.model_dump(mode='json'),'guide':metric_review_guidance(v).model_dump(mode='json')} for name,v in examples()}}}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--frontend-dir',type=Path);p.add_argument('--check',action='store_true');a=p.parse_args()
    roots=[ROOT/'contracts/v2.2-metric-review-guidance']
    if a.frontend_dir:
        if not (a.frontend_dir/'package.json').is_file():raise SystemExit('Invalid frontend root')
        roots.append(a.frontend_dir/'src/lib/metric-review-guidance/contracts')
    data=artifacts()
    for root in roots:
        for name,value in data.items():
            file=root/name;body=(json.dumps(value,sort_keys=True,indent=2)+'\n').encode()
            if a.check:
                if not file.is_file() or file.read_bytes()!=body:raise SystemExit('Metric review guidance contract drift')
            else:root.mkdir(parents=True,exist_ok=True);file.write_bytes(body)
    print(json.dumps({'ok':True,'check':a.check,'cases':len(data['fixtures.json']['cases'])}))
