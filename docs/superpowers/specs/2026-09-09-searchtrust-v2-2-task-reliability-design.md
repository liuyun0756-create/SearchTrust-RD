# SearchTrust v2.2 task reliability design

Date: 2026-09-09  
Milestone: V22-080  
Status: approved design, not implemented

## 1. Objective

V22-080 makes paid report generation recoverable and financially correct across
temporary provider outages, worker loss, browser disconnects and terminal failures.
It extends the existing Redis-backed v2.2 task runtime, checkpointed stages, Case
payments and database callback boundary. It does not replace them with a new workflow
engine.

The milestone must guarantee:

- one logical task has at most one current writer;
- completed stages are not repeated after a recoverable failure;
- provider outages do not cause uncontrolled retry traffic or quota waste;
- a task that does not produce a report within 20 minutes reaches a safe terminal
  failure;
- every failed paid attempt returns exactly one permanent account credit;
- generating again creates a new task and consumes one account credit again;
- a browser can recover current state after disconnect, refresh or reopening;
- duplicate requests, callbacks and worker recovery cannot duplicate reports, charges
  or compensation.

## 2. Confirmed business rules

1. The first attempt may consume the paid Case entitlement.
2. Every later attempt consumes one general account credit.
3. Every generation attempt uses a new logical task ID and its own idempotency key.
4. If an attempt starts but does not produce a report, it returns exactly one general
   account credit, regardless of whether its original charge came from the Case
   purchase or the general balance.
5. A returned credit is permanent, joins the existing account credit balance, and may
   be used for another Case.
6. Trying again consumes one credit. If that new attempt fails, it returns one credit
   again.
7. A successful attempt consumes its charge and receives no compensation.
8. Cash refunds remain a separate payment process. Task failure does not automatically
   issue a Dodo payment refund.
9. A task may retry, wait for a provider and recover for at most 20 minutes from its
   creation time.

## 3. Scope decomposition

V22-080 is one milestone with three coordinated workstreams:

1. **Runtime reliability:** retry policy, deadline, heartbeats, leases, fencing,
   stalled-run recovery and provider circuit breakers.
2. **Financial terminal effects:** atomic attempt charging, terminal result ownership,
   compensation ledger and general-credit balance updates.
3. **Client recovery:** authenticated live progress, polling fallback, refresh recovery,
   monotonic revisions and explicit compensation state.

They share fixed contracts but remain independently testable. Provider adapters do not
decide compensation, the browser does not decide task terminality, and the database
does not decide whether an exception is retryable.

## 4. Architecture

### 4.1 Reliability policy catalog

A pure versioned policy catalog maps an operation to:

- operation class and provider;
- deterministic or retryable error categories;
- maximum operation attempts;
- deterministic bounded backoff;
- circuit-breaker namespace;
- stage checkpoint/resume behavior;
- whether a failure is visible to the user or remains an internal waiting state.

The worker and provider adapters consume this catalog. They may not define local retry
counts that conflict with it.

The policy is:

- deterministic input, identity, authorization-scope and contract failures: zero
  automatic retries;
- timeout, connection failure, HTTP 429 and eligible 5xx provider failures: at most
  three operation attempts;
- database result persistence and state-callback delivery: at most five attempts and
  never a reason to rerun report generation;
- all work stops at the task deadline even when an operation still has retry budget.

Backoff includes stable task-based jitter so a restart reproduces the schedule without
causing synchronized retry bursts.

### 4.2 Runtime lease and fencing

Each running task owns a renewable Redis lease bound to:

- logical task ID;
- `run_generation`;
- opaque lease token digest;
- acquired time;
- heartbeat time;
- task deadline.

The worker renews the lease every 30 seconds in an independent heartbeat loop. Renewal
also updates the active-task sorted-set score used by the reconciler.

A task is stale after 180 seconds without a valid heartbeat. Recovery atomically:

1. verifies the task is still non-terminal and the observed generation is current;
2. increments `run_generation`;
3. invalidates the old lease;
4. marks the state queued for recovery;
5. enqueues a new physical ARQ job ID containing the new generation.

Every worker state transition, checkpoint commit, report persistence request and
terminal callback carries the expected generation. The store rejects or no-ops a
stale generation. A recovered old worker exits on its next heartbeat or write and
cannot publish a report or terminal effect.

### 4.3 Provider circuit breaker

Circuit state is stored in Redis and shared by API and Worker processes. The primary
key is `provider + operation`. SerpAPI adds a subordinate key per configured API key.
No circuit key or record contains the secret itself, request payload, query text,
customer identity or raw response.

The states are:

- `closed`: calls are allowed and eligible failures update the rolling counter;
- `open`: real calls are rejected locally with a safe retry time;
- `half_open`: after cooldown, one owner receives a Redis probe lease;
- a successful probe closes and resets the circuit;
- a failed probe reopens it with the next cooldown.

The operation circuit opens after five eligible failure outcomes in a rolling 60-second
window, or immediately when every configured SerpAPI key is isolated. Cooldowns are
60 seconds, 120 seconds and then capped at 300 seconds. One successful closed-state or
half-open call clears the rolling failure count. Only retryable transport, rate-limit
and eligible provider-service failures affect the aggregate circuit. User errors,
business-rule outcomes and local contract validation do not.

For SerpAPI, a rate-limit, exhausted-quota or key-rejection response immediately
isolates that key; three consecutive transport/service failures also isolate it. Key
cooldown uses the same 60/120/300-second schedule. Other healthy keys remain usable.
When every key is unavailable, the operation returns a local circuit-open result and
consumes no remote request.

### 4.4 Terminal-effects coordinator

The backend callback remains the only path from Redis job state to durable database
job state. The database owns charge consumption and compensation in the same terminal
transaction. Provider code and frontend code cannot modify credits.

A terminal event is accepted only when all of these match:

- task, Case and account ownership;
- monotonically increasing revision;
- current `run_generation`;
- expected non-terminal database state;
- unique terminal event and charge identities.

The transaction either applies all terminal effects or none.

### 4.5 Client recovery controller

The Case workspace uses an authenticated server-owned task recovery controller:

- Case load resolves the latest task from the server, not only session storage;
- an active task first uses authenticated server-sent events;
- disconnect automatically falls back to status polling;
- polling backs off to at most 10 seconds;
- `online`, page visibility and explicit retry events trigger immediate reconnection;
- responses apply only when their task ID and revision are current;
- terminal state stops every connection and timer.

The browser displays state but never infers a task failure from a lost connection or
elapsed local timer.

## 5. Durable data model

### 5.1 Report attempt charge

Add an append-auditable attempt charge record with:

- charge ID;
- user ID, Case ID and task ID;
- source: `case_purchase` or `account_credit`;
- state: `reserved`, `consumed` or `compensated`;
- debit amount fixed at one credit-equivalent;
- reservation, terminal and compensation timestamps;
- successful report ID when consumed;
- safe failure code and diagnostic ID when compensated;
- unique compensation key;
- created and updated timestamps.

There is exactly one charge per logical task and at most one compensation for that
charge.

### 5.2 Credit ledger

Add an immutable account credit ledger. A row contains:

- user ID;
- signed credit delta;
- reason: purchase, report attempt, failed-attempt compensation or administrative
  adjustment;
- source charge/order/task identifiers;
- stable idempotency key;
- timestamp.

The existing `users.audit_credits` account balance remains the fast current balance and
is the shared pool used by legacy and v2.2 report attempts. Each balance change and its
ledger entry occur in one transaction. Failed-attempt credits have no expiry.

### 5.3 Case entitlement state

The paid Case entitlement remains the first-attempt funding source. After a failed
first attempt it is closed as compensated and cannot be reserved again. The returned
value exists in the general account balance. This prevents both reusing the original
Case entitlement and spending its returned credit.

Historical `available`, `reserved`, `consumed` and payment-refund states remain
readable. The migration is additive and does not delete or rewrite historical orders,
Cases, reports or completed jobs.

### 5.4 Task reliability fields

Durable task state adds or formalizes:

- `deadline_at`;
- current `run_generation`;
- last accepted heartbeat;
- terminal-effects state: pending, applied or attention-required;
- bound attempt charge ID;
- latest safe diagnostic ID.

Redis remains the live execution source of truth; Postgres remains the durable
ownership, payment, report and terminal-effects source of truth.

## 6. Transactional flows

### 6.1 Create an attempt

One database function receives user, Case, new task ID and idempotency key.

1. Lock the account, Case entitlement and any matching idempotency record.
2. If the new task is the eligible first attempt, reserve the paid Case entitlement.
3. Otherwise require at least one general credit and decrement the balance by one.
4. Insert the corresponding debit ledger row and attempt charge.
5. Create or bind the analysis job.
6. Commit and return the task/charge state.

Duplicate calls with the same identifiers return the original result. Conflicting
reuse fails without changing credit.

### 6.2 Successful terminal event

The terminal transaction:

1. accepts the newest current-generation success revision;
2. binds the persisted report;
3. marks the attempt charge consumed;
4. consumes or closes the original Case entitlement when applicable;
5. marks terminal effects applied.

No credit is returned. Duplicate callbacks are no-ops.

### 6.3 Failed terminal event

Any attempt that started but produced no report is eligible. The transaction:

1. accepts the newest current-generation failed revision;
2. confirms there is no successful report for the task;
3. closes the paid Case entitlement when it funded the attempt;
4. marks the attempt charge compensated;
5. increments the general account balance by one;
6. inserts one immutable compensation ledger row;
7. marks terminal effects applied.

The unique task/charge/compensation identities make repeated callbacks and concurrent
reconciliation idempotent.

### 6.4 Compensation delay

If a terminal callback cannot complete its database transaction, the Redis task may
be terminal failed while durable terminal effects remain pending. The callback queue
retries up to five times without rerunning the analysis. The client displays
"compensation processing" until the database reports applied.

After five failures the item enters attention-required state and emits a safe
operational alert. The idempotency key remains valid so a later reconciliation or
manual repair can apply the credit exactly once.

### 6.5 Generate again

Generate again creates a new task ID, idempotency key and checkpoint namespace. It
consumes one general credit through the create-attempt transaction and may reuse only
persisted immutable Case inputs and still-valid bound data snapshots; it does not trust
or copy checkpoint output from the old failed task. It never reopens the old terminal
job. Its success or compensation is independently auditable.

## 7. Deadline and stage behavior

`deadline_at` is created once and does not move during retries, circuit cooldown,
worker recovery or browser disconnect.

Before a Provider request, retry delay or stage start, the worker verifies enough time
remains for the bounded operation. When the deadline is reached:

- execution stops;
- the current generation writes `JOB_DEADLINE_EXCEEDED`;
- the failed terminal transaction returns one general credit;
- unfinished external calls are cancelled when safe;
- completed checkpoints remain available under their existing retention rules.

Stage retry starts from the newest validated checkpoint. Result persistence and
callback retry are isolated final stages and do not invalidate a completed report
build.

## 8. Safe errors and user states

Fixed reliability codes include:

- `JOB_DEADLINE_EXCEEDED`;
- `JOB_RETRY_EXHAUSTED`;
- `PROVIDER_CIRCUIT_OPEN`;
- `JOB_LEASE_LOST`;
- `JOB_COMPENSATION_PENDING`;
- `JOB_COMPENSATION_FAILED`;
- existing deterministic input, identity and entitlement errors.

Errors never include Provider response bodies, API keys, OAuth material, query lists,
customer content or database error text. `PROVIDER_CIRCUIT_OPEN` and `JOB_LEASE_LOST`
are internal control states rather than immediate user-visible terminal failures.

After failure the UI distinguishes:

- task failed and credit returned;
- task failed and compensation is processing;
- compensation needs support attention.

When compensation is applied, it shows the current account balance and a
"Generate again" action that states one credit will be used. The user-visible
diagnostic ID may be copied for support.

## 9. API and event contracts

The authenticated frontend boundary provides:

- latest task lookup by owned Case;
- task state lookup by owned task ID;
- authenticated event stream with revisioned state events;
- create-new-attempt operation with idempotency key;
- terminal-effects/compensation status and current credit balance.

An event includes only bounded public task fields, revision, progress, safe error,
terminal-effects state and report navigation identity. It excludes report payload,
Provider payloads and secret diagnostics.

The Python API continues publishing signed revisioned callbacks. The Next.js callback
handler validates signature, timestamp, Case/task binding, generation and monotonic
revision before invoking the database transaction.

## 10. Failure invariants

The implementation must preserve all of these under concurrency and restart:

- one attempt consumes exactly one funding unit;
- success and compensation are mutually exclusive;
- a failed attempt adds exactly one account credit;
- a task that never completed attempt creation cannot receive compensation;
- a compensated paid Case entitlement cannot be reused;
- stale generations cannot transition state, persist a report or trigger terminal
  effects;
- one half-open circuit probe is active per circuit key;
- circuit rejection consumes no Provider request budget;
- browser reconnect cannot create a new task;
- generate-again idempotency cannot create or charge two tasks;
- no retry extends the 20-minute deadline.

## 11. Tests

### 11.1 Runtime unit tests

- policy classification and deterministic retry schedules;
- no retry for deterministic errors;
- three Provider attempts and five persistence/callback attempts;
- deadline checks before stages, waits and calls;
- circuit closed/open/half-open transitions;
- single-probe concurrency and cooldown escalation;
- independent SerpAPI key isolation and operation-wide exhaustion;
- secret-safe circuit storage and logs.

### 11.2 Lease and restart tests

- heartbeat every 30 seconds updates the lease and active index;
- 179 seconds is not stale and 180 seconds is stale;
- recovery atomically increments generation and enqueues a new physical ID;
- concurrent reconcilers recover once;
- an old worker cannot update progress, checkpoint final state, persist a report or
  publish a terminal event;
- restart during Provider call, checkpoint, report persistence and terminal callback;
- a recovered task resumes from validated checkpoints.

### 11.3 Database tests

- atomic paid-Case and general-credit attempt creation;
- insufficient balance and cross-user rejection without debit;
- duplicate and conflicting idempotency behavior;
- successful charge consumption without credit return;
- failed paid attempt closes Case entitlement and adds one general credit;
- failed account-credit attempt returns one general credit;
- retry consumes the returned credit and creates a new task;
- repeated failure returns one credit again;
- duplicate, reordered and concurrent terminal callbacks compensate once;
- success/failed terminal races have one winner;
- callback failure and later reconciliation;
- RLS denial and service-role-only mutation.

### 11.4 Frontend tests

- server recovery after refresh without session storage;
- authenticated SSE success path;
- SSE disconnect to polling and recovery back to SSE;
- online and visibility wakeups;
- stale revision rejection;
- terminal timers and connections are cleaned up;
- success navigation;
- failed, compensation-pending and compensated states;
- refreshed account balance;
- generate-again double-click and duplicate-request safety.

### 11.5 Integration and regression

- Redis/ARQ Worker kill-and-restart exercise;
- Provider outage long enough to open and recover a circuit;
- controlled 20-minute deadline using a fake clock;
- one paid attempt failure, one credit return and one new charged task;
- queue health and pending terminal-effects health;
- full backend, migration, frontend contract, type, test and production build suites;
- v1, V22-060 through V22-074 and existing payment/refund regression.

All automated tests use fake clocks, in-memory Redis or controlled local database
fixtures. They do not spend Provider quota, charge a card or issue a real refund.

## 12. Rollout and rollback

Release order is mandatory:

1. apply the additive database migration and verify functions, constraints and RLS;
2. deploy backend reliability contracts, leases, fencing, policy and circuits;
3. verify API, Redis, Worker heartbeat, stale recovery and terminal callback health;
4. deploy the frontend recovery controller and compensation states;
5. run one controlled failure drill proving one debit and one compensation;
6. monitor attention-required compensation and circuit-open counters.

The migration remains compatible with existing completed and in-flight rows. During
rollout, existing tasks may finish through the previous terminal path; new-attempt
creation switches to the new transaction only after its database contract is present.

Rollback disables new-attempt creation through the new reliability path, leaves
ledger and compensation history intact, and returns the runtime/frontend to the last
compatible reader. Rollback must never delete task, order, Case, report, ledger or
credit records.

GSC, GA4 and official GBP synchronization remain disabled. V22-080 does not activate
Verified Generation, introduce cost accounting from V22-082 or add product analytics
from V22-083.

## 13. Acceptance criteria

V22-080 is complete only when:

- runtime retry decisions come from one versioned policy;
- Provider retries, circuit states and 20-minute deadline are deterministic;
- 30-second leases and 180-second stale recovery fence old generations;
- every failed started attempt receives exactly one permanent general credit;
- every generate-again operation creates and charges one new task;
- terminal success, failure and compensation are atomic and idempotent;
- refresh and disconnect recover the server-owned latest state;
- all specified unit, concurrency, migration, integration and regression tests pass;
- production API, Redis, Worker and terminal-effects health pass after the ordered
  rollout;
- no Google sync or Verified Generation switch is opened by this milestone.
