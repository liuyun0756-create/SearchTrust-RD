#!/usr/bin/env python3
"""Offline phase-1 preview. Confirmation fixture is explicit, never inferred from truth."""
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.eval_gold import generate, read_json
from app.report_v22.identity_input import prepare_identity_input
from app.report_v22.capability_public import build_public_capability_report
from app.report_v22.findings import build_public_findings
from app.jobs_v22.digest import canonical_json_bytes


def replay(directory, confirmation, private_root):
    before, _, request, *_ = generate(read_json(directory / "input.json"), directory, private_root,
        ruleset="v22_capability_public_v12")
    manifest, updated, automatic, notes = prepare_identity_input(before.input_manifest, request, confirmation=confirmation)
    report = build_public_capability_report(manifest, updated)
    result = build_public_findings(updated, text_patterns=True, reviewed_evidence=True,
        rendered_pages=any(s.kind == "rendered" for s in updated.evidence_input.sources),
        resource_limits=True, sampling_quality=True, opening_hours=True, identity_v2=True)
    address = next(f for f in result._contact_alignment.fields if f.field == "address")
    address_findings = [f.id for f in report.findings if f.rule_id == "v22_public.gbp_address_alignment"]
    return report, dict(site_id=directory.name, automatic=automatic.model_dump(mode="json"),
        confirmation=confirmation, effective_mode=updated.business_identity.operating_model.value,
        operating_location=updated.business_identity.operating_location.model_dump(mode="json"),
        target_market=updated.evidence_input.context.target_market.model_dump(mode="json"),
        address=address.model_dump(mode="json"), notes=notes,
        before_findings=[dict(rule=f.rule_id,target=f.target) for f in before.findings],
        after_findings=[dict(rule=f.rule_id,target=f.target) for f in report.findings],
        address_actions=[a.id for a in report.action_candidates if set(a.finding_ids)&set(address_findings)],
        actions=[dict(id=a.id,title=a.title,target=a.target,materials=[m.model_dump(mode="json") for m in a.materials]) for a in report.action_candidates])


def saved_contact_observation():
    # Independent, explicitly saved P1-00b sample. Never inserted into bound_input.
    import gzip, hashlib
    from types import SimpleNamespace
    from app.report_v22.operating_model import extract_mode_evidence, assess_mode
    from app.report_v22.site_business_html import ParseBudget
    from app.report_v22.site_business_models import SiteBusinessLimits
    directory=ROOT/"tests/fixtures/p1-00b-contact/volcanoes"
    body=gzip.decompress((directory/"contact.html.gz").read_bytes())
    digest="sha256:"+hashlib.sha256(body).hexdigest()
    capture=read_json(directory/"capture-resumed.json")
    if not any(row.get("sha256")==digest for row in capture["responses"]):
        raise ValueError("saved_contact_checksum_mismatch")
    # Same independent-contact budget already used by P1-00b's offline check.
    limits=SiteBusinessLimits().model_dump();limits.update(max_html_bytes_per_page=4_000_000,max_html_bytes_total=10_000_000)
    rows,notes=extract_mode_evidence(body.decode(),url="https://www.volcanoeshikingguides.com/contact-us",
        domain="volcanoeshikingguides.com",checksum=digest,path="/html",budget=ParseBudget(SimpleNamespace(**limits)))
    return dict(source="tests/fixtures/p1-00b-contact/volcanoes/contact.html.gz",sha256=digest,
        included_in_bound_input=False,assessment=assess_mode(rows).model_dump(mode="json"),notes=notes)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, default=ROOT.parent/".release-private/2026-10-29")
    args=parser.parse_args()
    confirmations=read_json(ROOT/"tests/fixtures/p1-05-identity/confirmations.json")
    args.output_dir.mkdir(parents=True,exist_ok=True)
    rows=[]
    for name, confirmation in sorted(confirmations.items()):
        report,row=replay(ROOT/"tests/fixtures/gold"/name,confirmation,args.private_root)
        rows.append(row)
        (args.output_dir/f"{name}.json").write_bytes(canonical_json_bytes(report)+b"\n")
    summary=dict(external_calls=0, independent_saved_contact=saved_contact_observation(), cases=rows, confirmed_modes_match=sum(r["effective_mode"]==r["confirmation"]["value"] for r in rows))
    (args.output_dir/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(dict(external_calls=0,cases=len(rows),confirmed_modes_match=summary["confirmed_modes_match"],
        address={r["site_id"]:r["address"]["state"] for r in rows})))
if __name__=="__main__":main()
