# Automatic implementation attachment cleanup

Approved scope: reuse the existing production Worker, without a new paid service,
new analysis, credit consumption, or synthetic production implementation records.

## Design

- Run an ARQ cron every minute at second 28, disabled by default behind
  `V22_IMPLEMENTATION_ATTACHMENT_CLEANUP_ENABLED`.
- Reuse the existing service-role-only `prepare_v22_attachment_cleanup` and
  `finish_v22_attachment_cleanup` RPCs. Preserve the SQL retention rules, 20-row
  limit, two-minute upload grace and daily tombstone checks. No schema changes.
- Delete only a canonical `files/<attachment UUID>` returned as due by SQL.
  Validate the entire batch before deleting anything. Check that the private
  bucket still has the expected MIME and size policy. Confirm object absence
  after deleting, then acknowledge completion. Errors leave candidates retryable.
- Use the existing Worker Supabase credentials and bounded HTTP transport.
  Never list/delete arbitrary objects, use public/signed URLs, or log credentials,
  file names, attachment identities or provider bodies.
- Use a Redis owner lease (150 seconds), a 90-second run deadline, 12-second
  item deadlines and at most four concurrent items. A compare-owner Lua script
  atomically saves the last sanitized result (seven-day TTL) and releases the
  lease. Busy or stale owners cannot overwrite another run.
- Log start/results with timestamps and counters. A disabled flag makes no
  maintenance I/O. Missing configuration fails this maintenance task closed,
  without preventing report processing.

Native Worker scheduling was selected over a public maintenance HTTP endpoint
or a dedicated cron service to avoid an additional access surface, secret and
deployment. A feature flag provides rollback without touching stored reports.

## Verification and acceptance limits

Unit tests cover protocol, strict identity/due-date validation, bucket policy,
absence verification, partial failure, deadlines and wiring. Isolated real Redis
tests cover concurrent exclusion, expiry/ownership and atomic result recording.
Run repository quality gates before publishing. Enable only on the production
Worker, observe at least two independently scheduled runs, and recheck business
state/queue health. Current production has no attachments: empty automatic runs
prove scheduling and RPC access, not real file deletion. Real attachment upload
and removal acceptance remains skipped at the user's request.
