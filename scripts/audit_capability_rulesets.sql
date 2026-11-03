-- P0-02: one consistent, read-only snapshot; no report bodies or share tokens returned.
begin transaction isolation level repeatable read read only;
set local statement_timeout = '30s';

with per_report as (
  select
    case when jsonb_typeof(r.payload #> '{input_manifest,ruleset_version}') = 'string'
      and btrim(r.payload #>> '{input_manifest,ruleset_version}') <> ''
      then r.payload #>> '{input_manifest,ruleset_version}' else null end as ruleset_version,
    exists (select 1 from public.get_v22_completed_capability_report(r.user_id, r.case_id, r.id)) as completed,
    exists (select 1 from public.report_content_revisions v
      where v.report_id = r.id and v.user_id = r.user_id and v.case_id = r.case_id
        and v.status = 'approved') as ever_approved,
    coalesce((select v.status = 'approved' from public.report_content_revisions v
      where v.report_id = r.id and v.user_id = r.user_id and v.case_id = r.case_id
      order by v.revision desc limit 1), false) as latest_approved,
    (select count(*) from public.capability_report_shares s where s.report_id = r.id) as share_count,
    exists (select 1 from public.capability_report_shares s
      join public.report_content_revisions v on v.id = s.revision_id
        and v.report_id = s.report_id and v.user_id = s.user_id and v.case_id = s.case_id
        and v.status = 'approved'
      where s.report_id = r.id and s.user_id = r.user_id and s.case_id = r.case_id
        and s.revoked_at is null and s.expires_at > current_timestamp) as active_share
  from public.capability_report_versions r
), grouped as (
  select ruleset_version,
    count(*) as report_count,
    count(*) filter (where completed) as completed_reports,
    count(*) filter (where ever_approved) as ever_approved_reports,
    count(*) filter (where latest_approved) as latest_approved_reports,
    count(*) filter (where latest_approved and completed) as deliverable_approved_reports,
    count(*) filter (where share_count > 0) as shared_reports,
    sum(share_count)::bigint as share_count,
    count(*) filter (where active_share and completed) as active_share_reports
  from per_report group by ruleset_version
)
select jsonb_build_object(
  'observed_at', current_timestamp,
  'transaction_read_only', current_setting('transaction_read_only'),
  'report_count', (select count(*) from per_report),
  'groups', coalesce((select jsonb_agg(to_jsonb(g) order by g.ruleset_version nulls first)
    from grouped g), '[]'::jsonb)
) as inventory;

rollback;
