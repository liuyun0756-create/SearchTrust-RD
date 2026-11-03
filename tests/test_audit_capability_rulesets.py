"""P0-02 inventory: offline SQL execution and fail-closed CLI behavior."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ruleset_audit", ROOT / "scripts/audit_capability_rulesets.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def inventory():
    group = {key: 0 for key in audit.COUNT_FIELDS}
    group.update(ruleset_version="v22_capability_public_v5", report_count=1)
    return dict(observed_at="2026-11-02T10:00:00+00:00", transaction_read_only="on",
                report_count=1, groups=[group])


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare-array", "rows-wrapper"])
def test_valid_zero_is_distinct_from_unreadable_or_missing_data(wrapped):
    value = inventory()
    value.update(report_count=0, groups=[])
    rows = [{"inventory": value}]
    assert audit.parse_inventory(json.dumps({"rows": rows} if wrapped else rows)) == value
    for output in ["", "{}", "[]", "[{}]", '{"rows":[]}', '{"rows":null}', '{"rows":[{}]}', "permission denied"]:
        with pytest.raises(audit.AuditError, match="INVENTORY_RESULT_INVALID"):
            audit.parse_inventory(output)


@pytest.mark.parametrize("mutation", ["writable", "missing_count", "negative", "bool", "total",
    "duplicate", "invalid_version", "inconsistent", "no_timezone", "private_extra"])
@pytest.mark.parametrize("wrapped", [False, True], ids=["bare-array", "rows-wrapper"])
def test_invalid_or_partial_results_never_become_zero_counts(mutation, wrapped):
    value = inventory()
    group = value["groups"][0]
    if mutation == "writable": value["transaction_read_only"] = "off"
    if mutation == "missing_count": del group["shared_reports"]
    if mutation == "negative": group["share_count"] = -1
    if mutation == "bool": group["share_count"] = True
    if mutation == "total": value["report_count"] = 2
    if mutation == "duplicate": value["groups"].append(deepcopy(group)); value["report_count"] = 2
    if mutation == "invalid_version": group["ruleset_version"] = 3
    if mutation == "inconsistent": group["active_share_reports"] = 1
    if mutation == "no_timezone": value["observed_at"] = "2026-11-02"
    if mutation == "private_extra": value["token"] = "must-not-be-printed"
    rows = [{"inventory": value}]
    with pytest.raises(audit.AuditError, match="INVENTORY_RESULT_INVALID"):
        audit.parse_inventory(json.dumps({"rows": rows} if wrapped else rows))


def linked(tmp_path):
    folder = tmp_path / "supabase/.temp"
    folder.mkdir(parents=True)
    (folder / "project-ref").write_text("expected-project")
    return tmp_path


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare-array", "rows-wrapper"])
def test_cli_only_runs_fixed_read_only_file_for_the_expected_project(tmp_path, monkeypatch, wrapped):
    root = linked(tmp_path)
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        rows = [{"inventory": inventory()}]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"rows": rows} if wrapped else rows))
    monkeypatch.setattr(audit.subprocess, "run", run)
    result = audit.audit(root, "expected-project")
    assert result["status"] == "ok"
    assert result["groups"] == inventory()["groups"]
    assert calls[0][0] == ["npx", "--no-install", "supabase", "db", "query", "--linked", "--file", str(audit.SQL_PATH), "-o", "json"]
    assert calls[0][1] == dict(cwd=root, capture_output=True, text=True, timeout=60, check=False)
    with pytest.raises(audit.AuditError, match="LINKED_PROJECT_MISMATCH"):
        audit.audit(root, "other-project")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["denied", "timeout", "unavailable", "partial", "table"])
def test_query_failure_exits_not_checked_without_counts_or_secret_errors(tmp_path, monkeypatch, capsys, failure):
    root = linked(tmp_path)
    def run(*args, **kwargs):
        if failure == "timeout": raise subprocess.TimeoutExpired("secret-connection", 60)
        if failure == "unavailable": raise OSError("secret-connection")
        if failure == "table":
            return SimpleNamespace(returncode=0, stdout=' inventory\n-----------\n {"report_count": 0}\n(1 row)\n')
        return SimpleNamespace(returncode=1 if failure == "denied" else 0,
                               stdout="secret-connection", stderr="secret-token")
    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.main(["--frontend-dir", str(root), "--project-ref", "expected-project"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "not_checked"
    if failure == "table": assert json.loads(output)["error_code"] == "INVENTORY_RESULT_INVALID"
    assert "secret" not in output and "report_count" not in output


# Uses the frontend's existing embedded PostgreSQL dependency, with no network or live DB.
SQL_RUNNER = r"""
const { PGlite } = require('@electric-sql/pglite');
const fs = require('node:fs');
const { sql, tryWrite } = JSON.parse(fs.readFileSync(0, 'utf8'));
const db = new PGlite();
(async () => {
 await db.exec(`
 create table capability_report_versions(id text primary key, user_id text, case_id text, payload jsonb, completed boolean);
 create table report_content_revisions(id text primary key, report_id text, user_id text, case_id text, revision int, status text);
 create table capability_report_shares(id text primary key, report_id text, revision_id text, user_id text, case_id text, revoked_at timestamptz, expires_at timestamptz);
 create function get_v22_completed_capability_report(u text,c text,r text) returns setof capability_report_versions
 language sql stable as $$ select * from capability_report_versions where id=r and user_id=u and case_id=c and completed $$;
 insert into capability_report_versions values
 ('old-a','u','c','{"input_manifest":{"ruleset_version":"v22_capability_public_v3"}}',true),
 ('old-b','u','c','{"input_manifest":{"ruleset_version":"v22_capability_public_v3"}}',true),
 ('old-c','u','c','{"input_manifest":{"ruleset_version":"v22_capability_public_v3"}}',false),
 ('new-a','u','c','{"input_manifest":{"ruleset_version":"v22_capability_public_v5"}}',true),
 ('new-b','u','c','{"input_manifest":{"ruleset_version":"v22_capability_public_v5"}}',true),
 ('new-expired','u','c','{"input_manifest":{"ruleset_version":"v22_capability_public_v5"}}',true),
 ('new-revoked','u','c','{"input_manifest":{"ruleset_version":"v22_capability_public_v5"}}',true),
 ('unknown-a','u','c','{}',false),
 ('unknown-b','u','c','{"input_manifest":{"ruleset_version":42}}',false);
 insert into report_content_revisions values
 ('a1','old-a','u','c',1,'approved'),('a2','old-a','u','c',2,'approved'),('a3','old-a','u','c',3,'draft'),
 ('b1','old-b','u','c',1,'approved'),('c1','old-c','u','c',1,'approved'),
 ('n1','new-a','u','c',1,'draft'),('n2','new-b','u','c',1,'approved'),
 ('ne','new-expired','u','c',1,'approved'),('nr','new-revoked','u','c',1,'approved');
 insert into capability_report_shares values
 ('s1','old-a','a1','u','c',null,now()+interval '1 day'),
 ('s2','old-a','a2','u','c',null,now()+interval '1 day'),
 ('s3','old-a','a1','u','c',null,now()-interval '1 day'),
 ('s4','old-a','a1','u','c',now(),now()+interval '1 day'),
 ('s5','old-c','c1','u','c',null,now()+interval '1 day'),
 ('s6','new-b','n2','u','c',null,now()+interval '1 day'),
 ('s7','new-b','n2','u','c',null,now()+interval '1 day'),
 ('s8','new-expired','ne','u','c',null,now()-interval '1 day'),
 ('s9','new-revoked','nr','u','c',now(),now()+interval '1 day');
 `);
 const dump = async () => JSON.stringify(await Promise.all(
  ['capability_report_versions','report_content_revisions','capability_report_shares'].map(t => db.query(`select * from ${t} order by id`))));
 const before = await dump();
 if (tryWrite) {
   try {
     await db.exec(sql.replace('with per_report as (', "insert into capability_report_versions(id) values ('forbidden');\nwith per_report as ("));
     throw new Error('write unexpectedly allowed');
   } catch(e) {
     if (!String(e).includes('read-only transaction')) throw e;
     await db.exec('rollback');
   }
   console.log(JSON.stringify({writeBlocked:true, unchanged:before === await dump()}));
 } else {
   const results = await db.exec(sql);
   const rows = results.find(r => r.rows?.[0]?.inventory).rows;
   console.log(JSON.stringify({rows, unchanged:before === await dump()}));
 }
 await db.close();
})().catch(e => { console.error(e); process.exitCode=1; });
"""


def run_sql(*, try_write=False):
    result = subprocess.run(["node", "-e", SQL_RUNNER], cwd=ROOT.parent / "search-trust",
                            input=json.dumps(dict(sql=audit.SQL_PATH.read_text(), tryWrite=try_write)),
                            text=True, capture_output=True, check=True, timeout=45)
    return json.loads(result.stdout)


def test_real_sql_groups_reports_without_multiplying_revisions_or_shares():
    result = run_sql()
    assert result["unchanged"]
    value = audit.parse_inventory(json.dumps(result))
    assert value["report_count"] == 9
    groups = {g["ruleset_version"]: g for g in value["groups"]}
    old = groups["v22_capability_public_v3"]
    assert old == dict(ruleset_version="v22_capability_public_v3", report_count=3, completed_reports=2,
                       ever_approved_reports=3, latest_approved_reports=2, deliverable_approved_reports=1,
                       shared_reports=2, share_count=5, active_share_reports=1)
    assert groups["v22_capability_public_v5"] == dict(
        ruleset_version="v22_capability_public_v5", report_count=4, completed_reports=4,
        ever_approved_reports=3, latest_approved_reports=3, deliverable_approved_reports=3,
        shared_reports=3, share_count=4, active_share_reports=1)
    assert groups[None]["report_count"] == 2  # Missing ruleset is unknown, never silently v5.


def test_sql_transaction_rejects_writes_and_leaves_fixture_unchanged():
    assert run_sql(try_write=True) == {"writeBlocked": True, "unchanged": True}
