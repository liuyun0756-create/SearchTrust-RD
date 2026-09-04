# SearchTrust v2.2 Durable Jobs Operations

## Service layout

Deploy three independent services:

1. FastAPI Web using `railway.toml`;
2. ARQ Worker using `railway.worker.toml`;
3. a persistent Redis instance shared by Web and Worker.

The Web service accepts and streams v2.2 task state. It never executes a v2.2 analysis. The Worker is the only process that executes jobs. The existing v1 in-process path is intentionally unchanged.

## Required secrets

Configure the same values on the appropriate services:

- `V22_REDIS_URL`: private persistent Redis URL on Web and Worker;
- `V22_INTERNAL_API_TOKEN`: long random server-to-server token on Web and Next.js;
- `V22_CALLBACK_URL`: Next.js internal callback URL on Worker;
- `V22_CALLBACK_SECRET`: a different long random value on Worker and Next.js as `V22_JOB_CALLBACK_SECRET`;
- `V22_SUPABASE_URL` and `V22_SUPABASE_SERVICE_ROLE_KEY`: Worker-only credentials used solely to call the atomic `persist_v22_prospect_result` RPC;
- `V22_DIFY_API_KEY`: Worker-only controlled-copy application key.

Never place these values in source control or expose them with a `NEXT_PUBLIC_` prefix.

## Safe rollout

1. Provision persistent Redis and confirm persistence is enabled.
2. Apply all Supabase migrations, including the analysis result persistence RPC.
3. Deploy FastAPI Web with `V22_ANALYZE_ENABLED=false`.
4. Deploy the ARQ Worker with result persistence configured and confirm its health key appears.
5. Configure the signed Next.js callback and verify a test event updates `analysis_jobs` once.
6. Check `/api/v2/health/queue`: Redis and Worker should both be healthy and pending callbacks should drain.
7. Keep `V22_ANALYZE_ENABLED=false` until the end-to-end paid prospect smoke test is ready.

## Health and recovery

- Web liveness remains `/api/v1/health`, so a Redis incident does not remove v1 from service.
- Queue health is `/api/v2/health/queue`.
- ARQ writes the Worker health key every configured heartbeat interval.
- A growing pending-callback count means the Next.js callback is unavailable, misconfigured, or rejecting signatures.
- Stale queued/running tasks are inspected by the Worker reconciler every 30 seconds.

## Secret rotation

To rotate a callback secret without losing tasks, briefly pause Worker deployments, update `V22_JOB_CALLBACK_SECRET` in Next.js, update `V22_CALLBACK_SECRET` in Worker, and restart Worker. Redis retains unsynchronized revisions for later delivery.

To rotate the internal API token, update Next.js first while v2 submission remains disabled, then update FastAPI Web.

## Local verification

Copy `.env.example` to a private `.env`, replace placeholder secrets, and start the Compose project. Confirm the Redis, Web, and Worker health checks pass. Do not enable v2 analysis merely to test infrastructure; use the automated deterministic executor tests instead.
