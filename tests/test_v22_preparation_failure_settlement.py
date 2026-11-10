"""Offline execution of the worker's real failure event and SQL refund trigger."""
import json
from pathlib import Path
import subprocess

import pytest

from app.jobs_v22.errors import DeterministicJobError
from test_v22_capability_acquisition_worker import Harness


@pytest.fixture
def anyio_backend():
    return "asyncio"


SQL_RUNNER = r"""
const {PGlite} = require('@electric-sql/pglite');
const fs = require('node:fs');
const {event, owner} = JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{
 const db = new PGlite();
 try {
  await db.exec('create role anon; create role authenticated; create role service_role bypassrls;');
  for(const file of fs.readdirSync('supabase/migrations').filter(f=>f.endsWith('.sql')).sort())
   await db.exec(fs.readFileSync('supabase/migrations/'+file,'utf8'));
  await db.query("insert into users(id,clerk_user_id,email,credit_balance) values($1,'offline-owner','offline@example.invalid',1)",[owner]);
  await db.query(`insert into client_cases(id,user_id,site_url,normalized_domain,business_name,business_identity,operating_model,primary_service,target_market)
    values($1,$2,'https://offline.example.invalid','offline.example.invalid','Offline','{}','service_area','Plumbing','{}')`,[event.p_case_id,owner]);
  await db.query(`insert into analysis_jobs(id,case_id,job_type,status,current_stage,idempotency_key,state_revision,run_generation,attempt_count,created_at,deadline_at)
    values($1,$2,'prospect_report','running','building_evidence','offline-test',$3,1,1,$4::timestamptz-interval '20 minutes',$4)`,
    [event.p_job_id,event.p_case_id,event.p_revision-1,event.p_deadline_at]);
  await db.query(`insert into workflow_charges(id,user_id,case_id,workflow_kind,idempotency_key,analysis_job_id,discovery_job_id)
    values($1,$2,$3,'prospect','offline-test',$1,gen_random_uuid())`,[event.p_job_id,owner,event.p_case_id]);
  await db.query(`insert into credit_ledger(user_id,case_id,workflow_charge_id,analysis_job_id,kind,delta,balance_after,idempotency_key)
    values($1,$2,$3,$3,'prospect_debit',-1,1,'offline-debit')`,[owner,event.p_case_id,event.p_job_id]);
  const keys=Object.keys(event), args=keys.map((k,i)=>`${k}=>$${i+1}`).join(',');
  const apply=()=>db.query(`select * from apply_analysis_job_event(${args})`,keys.map(k=>event[k]));
  const first=(await apply()).rows[0];
  const duplicate=(await Promise.all([apply(),apply()])).map(r=>r.rows[0]);
  const job=(await db.query('select status,error_code,user_message from analysis_jobs where id=$1',[event.p_job_id])).rows[0];
  const charge=(await db.query('select state,settled_at is not null as settled from workflow_charges where id=$1',[event.p_job_id])).rows[0];
  const ledger=(await db.query("select kind,delta,idempotency_key from credit_ledger where kind='technical_failure_credit'")).rows;
  const balance=(await db.query('select credit_balance from users where id=$1',[owner])).rows[0].credit_balance;
  console.log(JSON.stringify({first,duplicate,job,charge,ledger,balance}));
 } finally {await db.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
"""


@pytest.mark.anyio
async def test_preparation_failure_event_refunds_once_using_real_migrations(monkeypatch):
    frontend=Path(__file__).resolve().parents[2]/"search-trust"
    if not (frontend/"node_modules/@electric-sql/pglite/package.json").is_file():
        pytest.skip("SQL refund proof requires sibling search-trust migrations and installed PGlite; SQL is not checked")
    h=Harness(monkeypatch)
    h.collector.collect_sources.side_effect=DeterministicJobError("SOURCE_INVALID","The source could not be validated.")
    try:
        await h.run()
        result=subprocess.run(["node","-e",SQL_RUNNER],cwd=frontend,
            input=json.dumps(dict(event=h.failed,owner=str(h.args["user_id"]))),
            text=True,capture_output=True,check=True,timeout=60)
        value=json.loads(result.stdout)
        assert value["first"]["applied"] and value["first"]["terminal_effects_applied"]
        assert all(not r["applied"] and not r["terminal_effects_applied"] for r in value["duplicate"])
        assert value["job"]==dict(status="failed",error_code="SOURCE_INVALID",user_message="The source could not be validated.")
        assert value["charge"]==dict(state="compensated",settled=True)
        assert value["balance"]==2
        assert value["ledger"]==[dict(kind="technical_failure_credit",delta=1,
            idempotency_key=f"workflow:{h.args['job_id']}:technical-failure")]
    finally: await h.close()
