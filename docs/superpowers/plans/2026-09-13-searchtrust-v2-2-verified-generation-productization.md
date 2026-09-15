# SearchTrust V2.2 Verified Generation Productization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接通 SearchTrust V2.2 Verified Generation 的可信数据、原子 credit、Dodo 单 credit 购买、durable Worker pipeline、Connection Center 和正式发布闭环。

**Architecture:** 浏览器只提交“为当前 Case 生成”的意图；Next.js 先调用 service-role-only Supabase RPC 原子绑定原始 Prospect、当前合格 GSC/GA4 快照与公共 GBP Evidence，并扣除 1 credit，再把只含不可变身份的 Verified envelope 发送给 Railway。现有 Worker 根据 envelope 类型路由到独立 Verified executor，从数据库解析完整可信输入并依次运行 V22-070～074；结果由独立 RPC 原子落库，技术失败沿现有事件结算返还一次 credit。

**Tech Stack:** Next.js 16、React 19、TypeScript 6、Supabase/PostgreSQL、Dodo Payments、FastAPI、Pydantic、ARQ/Redis、pytest、Vitest、Testing Library、Playwright。

---

设计来源：`docs/superpowers/specs/2026-09-13-searchtrust-v2-2-verified-generation-productization-design.md`。

实施仓库：

- 后端与计划：`/Users/liuyun3/Documents/SearchTurst前后端/SearchTrust-RD`
- 前端与数据库：`/Users/liuyun3/Documents/SearchTurst前后端/search-trust`

所有 migration 都是正向新增；不得改写已应用 migration。Verified 双开关在 Task 10 正式验收前保持关闭。

## 文件结构

新增后端文件：

- `app/jobs_v22/verified_models.py`：小型 Verified queue envelope 与数据库解析结果合同。
- `app/jobs_v22/verified_input_resolver.py`：按 job identity 从 Supabase 解析可信完整输入。
- `app/jobs_v22/verified_report_pipeline.py`：重建公开阶段并串联 V22-070～074。
- `app/jobs_v22/verified_executor.py`：Verified 执行、结果校验和持久化边界。
- `app/jobs_v22/verified_reconciler.py`：补偿从未成功注册进 Redis 的过期数据库任务。
- `app/jobs_v22/verified_result_persistence.py`：调用独立 Verified 结果 RPC。
- `tests/verified_pipeline_helpers.py`：Verified 产品化测试的确定性 fixture builder。
- `tests/test_v22_verified_input_resolver.py`、`test_v22_verified_report_pipeline.py`、
  `test_v22_verified_executor.py`、`test_v22_verified_result_persistence.py`：对应单元测试。

新增前端/数据库文件：

- `supabase/migrations/20260913100000_add_v2_2_verified_analysis_jobs.sql`：输入绑定、启动、解析、结果持久化与 lineage。
- `supabase/migrations/20260913110000_add_v2_2_verified_credit_payments.sql`：`$19 / 1 credit` 订单与入账。
- `src/lib/verified-analysis-v22/contracts.ts`、`repository.ts`、`handlers.ts`、`server.ts`、`index.ts`：Verified 提交服务端边界。
- `src/lib/verified-analysis-v22/digest.ts`：与后端 canonical JSON 一致的身份摘要。
- `src/lib/verified-credits-v22/contracts.ts`、`repository.ts`、`handlers.ts`、`index.ts`：Verified credit checkout。
- `src/app/api/v2/cases/[id]/verified-analysis/route.ts`：浏览器生成入口。
- `src/app/api/v2/cases/[id]/verified-credit/checkout/route.ts` 与
  `src/app/api/v2/cases/[id]/verified-credit/checkout/confirm/route.ts`：购买与确认入口。
- 对应 `*.test.ts`、`*.test.tsx` 和 Playwright fixture/旅程文件。

修改现有文件只承担以下职责：

- `app/api/v2/models.py`、`runtime.py`、`dependencies.py`：公开后端小型请求合同与双开关。
- `app/jobs_v22/executor.py`、`worker.py`：envelope router、job kind 与 Worker 装配。
- `app/core/config.py`、`.env.example`：后端 Verified flag。
- `src/lib/connection-center/*`、`src/components/google/connection-center.tsx`：lineage、余额、CTA 与任务恢复。
- `src/lib/payments-v22/dodo.ts`、`src/app/api/webhook/dodo/route.ts`：共享安全 Dodo client 和 purchase-kind 分派。
- `src/types/database.ts`：新表、列与 RPC 类型。
- `playwright.config.ts`、`e2e/support/api-router.ts`、`e2e/fixtures/*`、
  `e2e/verified-upgrade.spec.ts`：全离线端到端旅程。

### Task 1: 建立 Verified job 的原子数据库合同

**Files:**

- Create: `../search-trust/supabase/migrations/20260913100000_add_v2_2_verified_analysis_jobs.sql`
- Modify: `../search-trust/src/lib/database/v22Migration.test.ts`
- Modify: `../search-trust/supabase/tests/database/v22_schema.test.sql`
- Modify: `../search-trust/src/types/database.ts`

- [ ] **Step 1: 写失败的数据库迁移测试**

在 `v22Migration.test.ts` 新增一个 `describe("verified analysis job contract")`，至少创建以下 fixture：一个 active Case、一个已持久化 Prospect、三份 Prospect source snapshots、healthy/matched 的 GSC/GA4 binding 与最新 snapshot、5 个初始 credit。断言：

```ts
const started = await db.query<{
  job_id: string; created: boolean; idempotent: boolean;
  parent_report_id: string; gsc_snapshot_id: string; ga4_snapshot_id: string;
  public_gbp_snapshot_id: string; audit_credits: number;
}>(
  `select * from public.start_v22_verified_analysis($1,$2,$3,$4,$5,$6,null)`,
  [ownerId, caseId, verifiedJobId, "verified:case:attempt:1", parentChecksum, prospectReportId],
);
expect(started.rows[0]).toMatchObject({
  job_id: verifiedJobId,
  created: true,
  idempotent: false,
  parent_report_id: prospectReportId,
  gsc_snapshot_id: gscSnapshotId,
  ga4_snapshot_id: ga4SnapshotId,
  public_gbp_snapshot_id: publicGbpSnapshotId,
  audit_credits: 4,
});
```

同一 job/idempotency key 重放必须返回 `idempotent=true` 且余额仍为 4；换 job ID 复用相同 key、跨用户 Case、0 credit、缺 GSC、缺 GA4、过期 snapshot、identity mismatch、inactive connection、公共 GBP 缺失、无 parent Prospect 和非法 previous job 必须拒绝且不产生 charge/ledger/job。

- [ ] **Step 2: 运行测试并确认因 RPC 不存在而失败**

Run（工作目录 `search-trust`）：

```bash
npm run test:database
```

Expected: FAIL，错误包含 `start_v22_verified_analysis` 不存在或新 schema assertion 缺失。

- [ ] **Step 3: 新增不可变输入表和 lineage 列**

Migration 中创建以下物理结构：

```sql
alter table public.client_cases
  add column latest_verified_report_id uuid references public.reports(id) on delete set null;

create table public.verified_analysis_inputs (
  job_id uuid primary key references public.analysis_jobs(id) on delete cascade,
  case_id uuid not null references public.client_cases(id) on delete cascade,
  parent_report_id uuid not null references public.reports(id) on delete restrict,
  gsc_snapshot_id uuid not null references public.data_snapshots(id) on delete restrict,
  ga4_snapshot_id uuid not null references public.data_snapshots(id) on delete restrict,
  public_gbp_snapshot_id uuid not null,
  parent_snapshot_ids uuid[] not null,
  parent_payload jsonb not null,
  parent_payload_checksum text not null check (parent_payload_checksum ~ '^sha256:[0-9a-f]{64}$'),
  input_schema_version text not null default 'v22_verified_job_input_v1'
    check (input_schema_version = 'v22_verified_job_input_v1'),
  created_at timestamptz not null default now(),
  constraint verified_analysis_inputs_snapshot_distinct check (gsc_snapshot_id <> ga4_snapshot_id),
  constraint verified_analysis_inputs_parent_payload_object check (jsonb_typeof(parent_payload) = 'object')
);

create unique index uq_verified_analysis_inputs_case_job
  on public.verified_analysis_inputs(case_id, job_id);

alter table public.verified_analysis_inputs enable row level security;
grant select, insert, update, delete on public.verified_analysis_inputs to service_role;
revoke all on public.verified_analysis_inputs from public, anon, authenticated;
```

`parent_payload_checksum` 由服务端传入且必须与完整 `ReportV22` 的 canonical SHA-256 相同。服务端同时传入实际被摘要的 `p_expected_parent_report_id`；RPC 在 Case 锁内解析 parent 后必须比较该 ID，缺失或不符在扣费、创建 job/input 前返回 `V22_VERIFIED_PARENT_CHANGED`。幂等重放也必须比较被冻结的 parent ID 与 checksum，不符返回身份冲突。RPC 把已锁定的 exact `report_v2_2` 复制进输入表，结果持久化时同时比较 JSONB 与 checksum，防止后续 report row 变化。增加初次调用及重放 parent 不符的无新增扣费/无新增 job 回归测试。

新增 `validate_v22_case_latest_verified_report()` constraint trigger，要求
`latest_verified_report_id` 指向同 user、同 Case、`report_type='verified_execution'` 的报告；
继续保留现有 `latest_report_id` trigger。

- [ ] **Step 4: 实现 `start_v22_verified_analysis`**

函数签名固定为：

```sql
create function public.start_v22_verified_analysis(
  p_user_id uuid,
  p_case_id uuid,
  p_job_id uuid,
  p_idempotency_key text,
  p_parent_payload_checksum text,
  p_expected_parent_report_id uuid,
  p_previous_job_id uuid default null
) returns table (
  job_id uuid, created boolean, idempotent boolean,
  parent_report_id uuid, gsc_snapshot_id uuid, ga4_snapshot_id uuid,
  public_gbp_snapshot_id uuid, audit_credits integer
)
language plpgsql
set search_path = public;
```

函数在一个事务中按以下固定顺序执行：锁定 owned active Case；从 `latest_report_id` 读取当前报告，如果它是 `prospect` 就直接作为 parent，如果是 `verified_execution` 就沿其 `parent_report_id` 读取 Prospect；要求 parent `status='paid_full'`、schema `2.2.0`、无 parent、JSON report ID/Case/type 一致。随后从 parent `evidence_index` 找到与 Case `business_identity.public_gbp_url` 相同 locator URL 的 healthy/matched GBP Evidence，并取得唯一 snapshot ID；从 active GSC/GA4 bindings 各选最新 snapshot，要求连接 active、binding matched/confirmed、snapshot healthy、未过期、schema 分别为 `gsc_sync_v1`/`ga4_sync_v1`。

完成资格验证后锁定 `users` 行并执行：

```sql
update public.users
set audit_credits = audit_credits - 1
where id = p_user_id and audit_credits >= 1
returning audit_credits into resulting_balance;

insert into public.analysis_jobs(
  id, case_id, job_type, status, current_stage, progress, attempt_count,
  idempotency_key, cost_counters, run_generation, deadline_at, previous_job_id
) values (
  p_job_id, p_case_id, 'verified_report', 'queued', 'queued', 0, 0,
  p_idempotency_key, '{}'::jsonb, 1, now() + interval '20 minutes', p_previous_job_id
);

insert into public.analysis_attempt_charges(user_id, case_id, job_id, source)
values (p_user_id, p_case_id, p_job_id, 'account_credit');

insert into public.audit_credit_ledger(user_id, case_id, job_id, kind, delta, balance_after)
values (p_user_id, p_case_id, p_job_id, 'attempt_debit', -1, resulting_balance);
```

然后插入 `verified_analysis_inputs`。相同 job/key 重放在读取 charge 和 input 后返回同一绑定；任何身份冲突都抛错。`p_previous_job_id` 只接受同 Case、`job_type='verified_report'`、`status='failed'` 的 job。

- [ ] **Step 5: 实现 job-bound 输入解析、结果持久化与孤儿补偿 RPC**

新增 `resolve_v22_verified_analysis_input(p_job_id uuid, p_case_id uuid, p_run_generation integer) returns jsonb`，只从 `verified_analysis_inputs` 绑定读取：冻结 parent、parent 的 `site/serp/competitor/public GBP` 四份 `data_snapshots`、公开 GBP 快照当时的原始 `CustomerPublicGbpReference`、GSC、GA4。输出固定字段：

```json
{
  "schema_version": "v22_verified_resolved_input_v1",
  "job_id": "uuid",
  "case_id": "uuid",
  "parent_report": {},
  "parent_payload_checksum": "sha256:...",
  "site_snapshot": {},
  "serp_snapshot": {},
  "competitor_snapshot": {},
  "first_party_snapshots": [{}, {}]
}
```

新增 `persist_v22_verified_result(p_job_id uuid, p_case_id uuid, p_report_payload jsonb, p_run_generation integer) returns table(report_id uuid,idempotent boolean)`。它锁定 job/input，要求 active generation、job 尚未 terminal、报告类型为 `verified_execution`、report ID 等于 job ID、Case/parent 等于绑定、version number 等于 parent version + 1、GSC/GA4 snapshot IDs 与绑定一致、所有 Evidence snapshot ID 属于 parent snapshots、公共 GBP、GSC 或 GA4。插入 `reports` 后设置 `analysis_jobs.report_id`、`client_cases.latest_report_id` 与 `latest_verified_report_id` 为新报告；parent Prospect 保持不变。

新增 `expire_v22_stale_verified_jobs(p_now timestamptz, p_limit integer default 100) returns table(job_id uuid)`。它只锁定 `job_type='verified_report'`、`status='queued'`、`report_id is null`、`deadline_at <= p_now` 的行，并以 `state_revision + 1` 调用现有 `apply_analysis_job_event` 写入 terminal failed：error code `V22_VERIFIED_ENQUEUE_TIMEOUT`、stage `failed`、安全用户文案和 completed time。这样数据库启动成功但 Redis 永远没有注册的 job 会通过同一个 charge settlement 恢复一次 credit；已 running、已落报告或已 terminal 的 job 不受影响。

对三个新 RPC 执行：

```sql
revoke all on function public.start_v22_verified_analysis(uuid,uuid,uuid,text,text,uuid,uuid)
  from public, anon, authenticated;
grant execute on function public.start_v22_verified_analysis(uuid,uuid,uuid,text,text,uuid,uuid)
  to service_role;
```

同样只把 resolve/persist/expire 的 execute 授予 `service_role`。

- [ ] **Step 6: 更新 schema/type 测试并运行**

`v22_schema.test.sql` 断言表、列、FK、RLS、索引、四个函数签名与权限；`database.ts` 增加 `latest_verified_report_id`、`verified_analysis_inputs`、RPC 参数/返回类型。数据库测试用过去 deadline 的 queued Verified job 调用 expire 两次，第一次余额恢复且 charge 为 compensated，第二次无余额变化。

Run：

```bash
npm run test:database
npm test -- src/lib/database/v22Migration.test.ts
```

Expected: PASS；数据库测试数增加，transaction rollback 后无 fixture 残留。

- [ ] **Step 7: 提交数据库 job 合同**

```bash
git add supabase/migrations/20260913100000_add_v2_2_verified_analysis_jobs.sql \
  supabase/tests/database/v22_schema.test.sql src/lib/database/v22Migration.test.ts \
  src/types/database.ts
git commit -m "feat(v2.2): add verified analysis job contract"
```

### Task 2: 建立 `$19 / 1 credit` 的数据库支付合同

**Files:**

- Create: `../search-trust/supabase/migrations/20260913110000_add_v2_2_verified_credit_payments.sql`
- Modify: `../search-trust/src/lib/database/v22Migration.test.ts`
- Modify: `../search-trust/supabase/tests/database/v22_schema.test.sql`
- Modify: `../search-trust/src/types/database.ts`

- [ ] **Step 1: 写失败的购买、重放与退款测试**

新增测试：`case_verified_credit` pending order 必须 `case_id != null`、`credits_purchased=1`、`amount=1900`、`currency='USD'`；`fulfill_v22_verified_credit_payment` 首次把 0 变 1，confirm/webhook 重放仍为 1；错误金额、币种、owner、Case、payment ID 或 purchase kind 被拒绝。退款时若 credit 尚在余额中则扣回一次；若余额为 0 则返回 `reversal_applied=false, manual_review=true` 并保持余额 0，不制造负数。

```ts
expect((await db.query(
  `select * from public.fulfill_v22_verified_credit_payment($1,$2,$3,$4,1900,'USD')`,
  [orderId, "pay_verified_1", clerkUserId, caseId],
)).rows[0]).toMatchObject({
  fulfilled: true,
  idempotent: false,
  credits_added: 1,
  audit_credits: 1,
});
```

- [ ] **Step 2: 运行测试并确认新 purchase kind/RPC 尚不存在**

Run：

```bash
npm run test:database
```

Expected: FAIL，指向 `orders_purchase_kind_check` 或缺少 fulfillment RPC。

- [ ] **Step 3: 扩展订单与 credit ledger 约束**

正向 migration 重建命名 check constraints，使 `orders.purchase_kind` 接受
`legacy_credit | case_prospect_report | case_verified_credit`，并固定：

```sql
(purchase_kind = 'case_verified_credit'
 and case_id is not null
 and credits_purchased = 1
 and amount = 1900
and currency = 'USD')
```

同步扩展 `orders_payment_reference_check`，只允许新的 purchase kind 在 `pending/failed` 时尚无
provider reference；`paid/refunded` 必须已有 `payment_id`。现有 Prospect 与 legacy order shape
保持原义。

新增只约束 pending 状态的唯一索引，允许同一 Case 在完成一次购买后再次购买：

```sql
create unique index uq_orders_pending_case_verified_credit_checkout
  on public.orders(case_id)
  where purchase_kind = 'case_verified_credit' and status = 'pending';
```

`audit_credit_ledger` 增加 nullable `order_id` FK，kind 增加
`purchase_credit | payment_refund_debit | payment_refund_manual_review`；delta 分别固定为 `1 | -1 | 0`，并增加 partial unique `(order_id, kind)`。

- [ ] **Step 4: 实现 fulfillment 与 refund RPC**

新增：

```sql
public.fulfill_v22_verified_credit_payment(
  p_local_order_id uuid, p_payment_id text, p_clerk_user_id text,
  p_case_id uuid, p_amount integer, p_currency text
)
returns table(
  fulfilled boolean, idempotent boolean, credits_added integer, audit_credits integer
)
```

函数锁定 order 与 user，严格要求 `1900/USD/case_verified_credit/credits_purchased=1`，首次 paid 时 `audit_credits + 1` 并写 `purchase_credit` ledger；重放相同 payment ID 返回 `idempotent=true`，不同 payment ID 拒绝。

新增 `refund_v22_verified_credit_payment(...) returns table(refunded boolean,idempotent boolean,reversal_applied boolean,manual_review boolean,audit_credits integer)`。余额至少 1 时扣 1 并写 `payment_refund_debit`；余额为 0 时不扣、不变负，写 delta 0 的 `payment_refund_manual_review`。两个函数均只授权 `service_role`。

- [ ] **Step 5: 更新类型、schema assertions 并运行**

Run：

```bash
npm run test:database
npm test -- src/lib/database/v22Migration.test.ts
```

Expected: PASS；purchase、重放、可撤销退款和已消费退款四条路径均有不可变 ledger 证据。

- [ ] **Step 6: 提交支付数据库合同**

```bash
git add supabase/migrations/20260913110000_add_v2_2_verified_credit_payments.sql \
  supabase/tests/database/v22_schema.test.sql src/lib/database/v22Migration.test.ts \
  src/types/database.ts
git commit -m "feat(v2.2): add verified credit purchase ledger"
```

### Task 3: 增加前端的可信 Verified 提交边界

**Files:**

- Create: `../search-trust/src/lib/verified-analysis-v22/contracts.ts`
- Create: `../search-trust/src/lib/verified-analysis-v22/digest.ts`
- Create: `../search-trust/src/lib/verified-analysis-v22/repository.ts`
- Create: `../search-trust/src/lib/verified-analysis-v22/handlers.ts`
- Create: `../search-trust/src/lib/verified-analysis-v22/server.ts`
- Create: `../search-trust/src/lib/verified-analysis-v22/index.ts`
- Create: `../search-trust/src/lib/verified-analysis-v22/handlers.test.ts`
- Create: `../search-trust/src/lib/verified-analysis-v22/repository.test.ts`
- Create: `../search-trust/src/lib/verified-analysis-v22/digest.test.ts`
- Create: `../search-trust/src/app/api/v2/cases/[id]/verified-analysis/route.ts`

- [ ] **Step 1: 写失败的 handler/repository 测试**

浏览器 POST body 只能为空对象；job ID 和 idempotency key 来自 headers。测试证明 repository 忽略任何 browser snapshot/parent 字段，RPC 返回的 binding 才进入 Railway 请求：

```ts
expect(fetcher).toHaveBeenCalledWith(
  "https://railway.invalid/api/v2/verified-analyze",
  expect.objectContaining({
    method: "POST",
    body: JSON.stringify({
      schema_version: "v22_verified_task_request_v1",
      case_id: caseId,
      parent_report_id: parentId,
      gsc_snapshot_id: gscId,
      ga4_snapshot_id: ga4Id,
      public_gbp_snapshot_id: publicGbpId,
      input_checksum: bindingChecksum,
    }),
  }),
);
```

覆盖未登录 401、flag off 404、非法 ID 400、资格/credit 失败 409、Railway timeout 504、上游 invalid contract 502 和重放 202。

- [ ] **Step 2: 运行测试并确认模块缺失**

Run：

```bash
npm test -- src/lib/verified-analysis-v22
```

Expected: FAIL，模块无法解析。

- [ ] **Step 3: 实现合同与 service-role repository**

`contracts.ts` 固定：

```ts
export interface VerifiedStartBinding {
  job_id: string;
  created: boolean;
  idempotent: boolean;
  parent_report_id: string;
  gsc_snapshot_id: string;
  ga4_snapshot_id: string;
  public_gbp_snapshot_id: string;
  audit_credits: number;
}

export interface VerifiedTaskRequest {
  schema_version: "v22_verified_task_request_v1";
  case_id: string;
  parent_report_id: string;
  gsc_snapshot_id: string;
  ga4_snapshot_id: string;
  public_gbp_snapshot_id: string;
  input_checksum: `sha256:${string}`;
}
```

`digest.ts` 使用 `node:crypto.createHash("sha256")`，对象 key 按 UTF-16 递归排序、数组保持顺序、原始值用 `JSON.stringify`，输出 `sha256:<64 hex>`。数字统一采用有限 IEEE-754 binary64 的 ECMAScript 语义：`-0→0`、`1.0→1`、`1e-6→0.000001`、`1e-7→1e-7`、`1e20` 使用定点、`1e21` 使用指数；非法或非纯 JSON 值拒绝。

后端 `app/jobs_v22/digest.py` 增加 Verified 专用 `verified_canonical_json_bytes` / `verified_request_digest`，用于该边界的 parent 与 stable identity checksum。测试锁定相同数字边界、精度、完整 schema-valid Prospect 改写 fixture 的字节/摘要；现有 `canonical_json_bytes` / `request_digest` 的 orjson 编码保持不变，避免更改已有 checkpoint/source snapshot 身份。

`SupabaseVerifiedAnalysisRepository.start()` 先读取 Case 的 `latest_report_id`；如果 latest 是 Verified，就沿它的 `parent_report_id` 读取原始 Prospect，否则直接读取 latest Prospect。使用该 canonical digest 计算完整原始 JSON 的 parent checksum，再将服务端解析的 exact parent ID 作为 `p_expected_parent_report_id` 一并调用 `start_v22_verified_analysis`；对稳定身份 `{case_id, job_id, parent_report_id, gsc_snapshot_id, ga4_snapshot_id, public_gbp_snapshot_id}` 计算 `input_checksum`，绑定 ID 仅取经严格校验的 RPC 返回值。不得接收浏览器传入 parent/snapshot ID；不得将可变的 created/idempotent/audit_credits 纳入摘要。

- [ ] **Step 4: 实现 submit handler 与路由**

handler 的依赖注入与 `analysis-v22/handlers.ts` 保持相同超时、安全错误和内部 bearer header。处理顺序为 user→flag→Case ID/header→空 body→验证上游 base URL/token→RPC start→Railway `/api/v2/verified-analyze`；配置缺失或无效时返回 503，且零 start、零 fetch。超时覆盖收到 headers 后读取 response body 的阶段，AbortError 返回 504、网络读取错误保留服务不可用语义，仅 JSON 语法/合同错误返回 502。若数据库已扣费但 Railway 立即不可达，不直接修改余额；让 durable database job 通过 Task 7 的 reconciliation 进入失败事件并补偿一次，避免 handler 级重复退款。

`server.ts` 自己加载 `V22_API_BASE_URL` 与 `V22_INTERNAL_API_TOKEN`，构造并导出
`submitVerifiedAnalysis`。Route 固定 fail closed：

```ts
import { submitVerifiedAnalysis } from "@/lib/verified-analysis-v22/server";

export const dynamic = "force-dynamic";
export const POST = submitVerifiedAnalysis;
```

- [ ] **Step 5: 运行 focused tests 与类型检查**

```bash
npm test -- src/lib/verified-analysis-v22
npm run typecheck
```

Expected: PASS；浏览器不能控制任何可信 binding。

- [ ] **Step 6: 提交 Verified submit boundary**

```bash
git add src/lib/verified-analysis-v22 src/app/api/v2/cases/\[id\]/verified-analysis/route.ts
git commit -m "feat(v2.2): add verified analysis submit boundary"
```

### Task 4: 增加后端 Verified 请求合同与 durable runtime 路由

**Files:**

- Create: `app/jobs_v22/verified_models.py`
- Modify: `app/api/v2/models.py`
- Modify: `app/api/v2/dependencies.py`
- Modify: `app/api/v2/runtime.py`
- Modify: `app/core/config.py`
- Modify: `.env.example`
- Modify: `tests/test_api_v2_contract.py`
- Modify: `tests/test_api_v2_jobs.py`
- Modify: `tests/test_v22_job_config.py`
- Modify: `tests/test_v22_deployment_config.py`
- Modify: `contracts/v2.2/api_v2.schema.json`
- Modify: `contracts/v2.2/manifest.json`
- Modify: `../search-trust/src/lib/report-v22/contracts/api_v2.schema.json`
- Modify: `../search-trust/src/lib/report-v22/contracts/manifest.json`

- [ ] **Step 1: 写失败的合同、flag 和 runtime 测试**

测试 `VerifiedTaskRequest` 只接受六个 binding 字段和固定 schema；重复 source IDs、非 SHA-256、额外字段拒绝。`POST /api/v2/verified-analyze` 在 flag false 返回 404/503 的现有安全风格，flag true 注册 `VerifiedRequestEnvelope` 并入同一个 ARQ queue，不调用 competitor discovery verifier。

```python
assert registered_payload == {
    "schema_version": "v22_verified_request_envelope_v1",
    "verified_request": valid_verified_task_request,
}
assert queued == [(VERIFIED_JOB_ID, 1)]
```

- [ ] **Step 2: 运行测试并确认合同/route/flag 不存在**

Run（工作目录 `SearchTrust-RD`）：

```bash
.venv/bin/python -m pytest -q tests/test_api_v2_contract.py tests/test_api_v2_jobs.py \
  tests/test_v22_job_config.py tests/test_v22_deployment_config.py
```

Expected: FAIL，缺少 `VerifiedTaskRequest`、route 或 config field。

- [ ] **Step 3: 实现小型严格合同**

`verified_models.py`：

```python
class VerifiedTaskRequest(StrictModel):
    schema_version: Literal["v22_verified_task_request_v1"] = "v22_verified_task_request_v1"
    case_id: UUID
    parent_report_id: UUID
    gsc_snapshot_id: UUID
    ga4_snapshot_id: UUID
    public_gbp_snapshot_id: UUID
    input_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def distinct(self) -> "VerifiedTaskRequest":
        if len({self.parent_report_id, self.gsc_snapshot_id, self.ga4_snapshot_id,
                self.public_gbp_snapshot_id}) != 4:
            raise ValueError("verified input identities must be distinct")
        return self

class VerifiedRequestEnvelope(StrictModel):
    schema_version: Literal["v22_verified_request_envelope_v1"]
    verified_request: VerifiedTaskRequest
```

- [ ] **Step 4: 实现独立 runtime submit 和双开关**

`V22JobRuntime.submit_verified()` 不调用 discovery verifier；它把 envelope 交给相同 `DurableJobStore.register_job` 和 queue。新增 `require_v22_verified_analysis_enabled` 依赖与 route：

```python
@router.post("/verified-analyze", response_model=TaskCreateResponse, status_code=202)
async def submit_verified_analysis(
    body: VerifiedTaskRequest,
    job_id: Annotated[UUID, Header(alias="X-SearchTrust-Job-ID")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    _: Annotated[None, Depends(require_internal_auth)],
    __: Annotated[None, Depends(require_v22_verified_analysis_enabled)],
    runtime: Annotated[V22JobRuntime, Depends(get_v22_runtime)],
) -> TaskCreateResponse:
    return await runtime.submit_verified(job_id=job_id, idempotency_key=idempotency_key, request=body)
```

`Settings` 与 `.env.example` 增加 `V22_VERIFIED_ANALYSIS_ENABLED=false`；不要把它与 `V22_ANALYZE_ENABLED` 合并。

- [ ] **Step 5: 更新导出 JSON Schema 并运行测试**

将 `VerifiedTaskRequest` 放入 `ApiV2ContractBundle`，运行：

```bash
.venv/bin/python scripts/export_v22_contracts.py --frontend-dir ../search-trust
.venv/bin/python -m pytest -q tests/test_api_v2_contract.py tests/test_api_v2_jobs.py \
  tests/test_v22_job_config.py tests/test_v22_deployment_config.py
```

Expected: PASS，生成的 `contracts/api_v2.schema.json` 包含新定义。

- [ ] **Step 6: 提交后端 request/runtime 合同**

```bash
git add app/jobs_v22/verified_models.py app/api/v2/models.py app/api/v2/dependencies.py \
  app/api/v2/runtime.py app/core/config.py .env.example contracts/v2.2/api_v2.schema.json \
  contracts/v2.2/manifest.json \
  tests/test_api_v2_contract.py tests/test_api_v2_jobs.py tests/test_v22_job_config.py \
  tests/test_v22_deployment_config.py
git commit -m "feat(v2.2): register verified durable jobs"
```

前端仓库同步提交共享合同：

```bash
git add src/lib/report-v22/contracts/api_v2.schema.json src/lib/report-v22/contracts/manifest.json
git commit -m "chore(v2.2): sync verified task contract"
```

### Task 5: 实现后端可信输入解析与 Verified 结果持久化

**Files:**

- Create: `app/jobs_v22/verified_input_resolver.py`
- Create: `app/jobs_v22/verified_result_persistence.py`
- Create: `tests/test_v22_verified_input_resolver.py`
- Create: `tests/test_v22_verified_result_persistence.py`
- Modify: `app/jobs_v22/verified_models.py`
- Modify: `tests/provider_contracts/test_delivery_contracts.py`
- Modify: `tests/fixtures/provider_contracts/manifest.json`

- [ ] **Step 1: 写失败的 resolver/persister 测试**

resolver 测试用 `httpx.MockTransport` 断言只调用
`/rest/v1/rpc/resolve_v22_verified_analysis_input`，传 job/case/generation；验证超时/429/5xx 为 `TransientJobError`，4xx、过大 body、schema/ID/checksum 不匹配为 `DeterministicJobError`，响应文本不进入日志。

persister 测试断言 exact payload：

```python
assert sent.json() == {
    "p_job_id": str(JOB_ID),
    "p_case_id": str(CASE_ID),
    "p_report_payload": report.model_dump(mode="json"),
    "p_run_generation": 1,
}
assert sent.url.path == "/rest/v1/rpc/persist_v22_verified_result"
```

- [ ] **Step 2: 运行测试并确认两个 adapters 不存在**

```bash
.venv/bin/python -m pytest -q tests/test_v22_verified_input_resolver.py \
  tests/test_v22_verified_result_persistence.py tests/provider_contracts/test_delivery_contracts.py
```

Expected: FAIL，imports 不存在。

- [ ] **Step 3: 定义解析结果合同并实现 resolver**

`VerifiedResolvedInput` 必须包含：`job_id`、`case_id`、完整 `ReportV22 parent_report`、
`parent_payload_checksum`、`SiteInventorySnapshot`、`SerpMarketSnapshot` 数据行及 expires/checksum、
`CompetitorCollectionSnapshot`、`CustomerPublicGbpSnapshot`、该公开 GBP 快照当时的 `CustomerPublicGbpReference` 和恰好两份 `TrustedFirstPartySnapshot`（GSC、GA4）。验证：所有 Case、snapshot identity、source type、schema、checksum、timestamp、health、identity、reference checksum、parent report、public GBP Evidence 与小型 request 完全匹配。

Task 5 校验 `parent_payload_checksum` 和小型 request 的 `input_checksum` 时必须使用 Task 3 配套增加的 `verified_request_digest`；parent 摘要针对数据库返回的原始 JSON 值，在模型重序列化前计算。`input_checksum` 使用与 Next.js 相同的六个稳定身份字段。已有来源 snapshot、流水线 stage/checkpoint 的摘要仍使用原来的 `request_digest`，不得全面替换。

从持久化 SERP row 重建 `SharedMarketSnapshot` 时，`snapshot_id/checksum/created_at/expires_at`
来自该 row，`source_job_id` 来自 `SerpMarketSnapshot.job_id`，`input_digest` 使用从 parent report
重建的 `AnalyzeRequest` 调用 `analysis_discovery_input_digest()` 得到；不得伪造常量 digest。

resolver 使用 service role bearer，最大响应 25 MB，20 秒 timeout，不 follow redirects，不记录响应 payload。

- [ ] **Step 4: 实现 Verified persister**

`SupabaseVerifiedResultPersister.persist()` 先用 `ReportV22.model_validate` 本地重验：type、report ID、Case、parent、GSC/GA4 snapshot；然后调用独立 RPC。HTTP 错误分类、响应大小和返回 report ID 校验与现有 `SupabaseResultPersister` 一致，但不传或写入新的 public snapshots。

- [ ] **Step 5: 运行测试并更新 provider manifest**

```bash
.venv/bin/python -m pytest -q tests/test_v22_verified_input_resolver.py \
  tests/test_v22_verified_result_persistence.py tests/provider_contracts/test_delivery_contracts.py
```

Expected: PASS；manifest 明确列出 resolve/persist 两个 service-role RPC 且没有 OAuth token 字段。

- [ ] **Step 6: 提交解析与持久化 adapters**

```bash
git add app/jobs_v22/verified_input_resolver.py app/jobs_v22/verified_result_persistence.py \
  tests/test_v22_verified_input_resolver.py tests/test_v22_verified_result_persistence.py \
  tests/provider_contracts/test_delivery_contracts.py tests/fixtures/provider_contracts/manifest.json
git commit -m "feat(v2.2): resolve and persist verified inputs"
```

### Task 6: 串联 V22-070～074 为独立 Verified pipeline

> 规格审查修正：Task 6 同时补齐 Prospect 生产链的客户公开 GBP 先决条件。
> Prospect Worker 必须通过独立 checkpointed SerpAPI 阶段（三密钥轮换、最多 3 次尝试、
> 30 天过期）生成且原子传递 `CustomerPublicGbpSnapshot` 与原始
> `CustomerPublicGbpReference`；缺失、无强身份 ID 或不匹配均 fail closed。
> 该修正只使 Prospect 父报告的四来源图可到达，不表示 Task 7 Verified executor 已完成。

**Files:**

- Create: `app/jobs_v22/verified_report_pipeline.py`
- Create: `app/jobs_v22/customer_public_gbp_stage.py`
- Create: `tests/verified_pipeline_helpers.py`
- Create: `tests/test_v22_verified_report_pipeline.py`
- Modify: `app/jobs_v22/prospect_report_pipeline.py`
- Modify: `app/jobs_v22/executor.py`
- Modify: `app/jobs_v22/result_persistence.py`
- Modify: `app/jobs_v22/worker.py`

- [ ] **Step 1: 写失败的完整 pipeline 测试**

测试构建一个真实 Prospect report/source graph 与 GSC/GA4 snapshots，运行 pipeline 后断言：

```python
assert report.report_version.report_type == "verified_execution"
assert report.report_version.report_id == VERIFIED_JOB_ID
assert report.report_version.parent_report_id == PROSPECT_REPORT_ID
assert report.report_version.version_number == 2
assert {report.first_party_performance.gsc.snapshot_id,
        report.first_party_performance.ga4.snapshot_id} == {GSC_ID, GA4_ID}
assert all(entry.change_type != "unchanged" for entry in report.version_diff.entries)
```

spy 证明没有调用站点、SerpAPI、competitor collection 或 Dify；相同 job/request 的第二次执行全部命中 checkpoint 且 report JSON 字节相同。篡改 parent/source/snapshot/checksum 必须 deterministic fail。

- [ ] **Step 2: 运行测试并确认 pipeline 不存在**

```bash
.venv/bin/python -m pytest -q tests/test_v22_verified_report_pipeline.py
```

Expected: FAIL，`VerifiedReportPipeline` 无法 import。

- [ ] **Step 3: 提取可复用的公开 evidence 重建函数**

把 `prospect_report_pipeline.py` 中纯函数 `build_prospect_evidence_input` 保持兼容，并新增一个只接收已持久化 source graph 的构造函数；它必须产生与父 Prospect 当时相同的 `PublicFindingsInput`。通过 `request_digest(canonical_public_findings(...))` 与父报告 Findings/Actions 身份检查证明重建结果没有漂移。

- [ ] **Step 4: 实现 pipeline 的固定阶段顺序**

`VerifiedReportPipeline.build()` 固定执行：

```python
public_result = await self.public_stage.build(job_id=job_id, request=public_input, checkpoints=checkpoints)
public_plan = build_public_action_plan(PublicActionPlanInput(
    findings_result=public_result,
    planning_date=parent.report_version.generated_at.date(),
))
first_result = await self.first_party_stage.build(job_id=job_id, request=first_input, checkpoints=checkpoints)
cross_result = await self.cross_source_stage.build(job_id=job_id, request=cross_input, checkpoints=checkpoints)
verified_result = await self.reprioritization_stage.build(job_id=job_id, request=verified_input, checkpoints=checkpoints)
diff_result = await self.version_diff_stage.build(job_id=job_id, request=diff_input, checkpoints=checkpoints)
execution_result = await self.execution_plan_stage.build(job_id=job_id, request=execution_input, checkpoints=checkpoints)
return execution_result.report
```

每个 input 的 checksum 用 `request_digest`，first-party semantic checksum 使用
`semantic_first_party_input_checksum`，公开 result checksum 使用
`request_digest(canonical_public_findings(public_result))`。父级 public action plan 使用
`parent.report_version.generated_at.date()` 重建；Verified reprioritization 与 execution plan 使用
新的 `evaluated_at.date()`。`evaluated_at` 每个 job 只读取一次；copy version固定
`v22_verified_copy_v1`。

- [ ] **Step 5: 运行 pipeline 与冻结阶段回归**

```bash
.venv/bin/python -m pytest -q tests/test_v22_verified_report_pipeline.py \
  tests/test_v22_first_party_findings_stage.py tests/test_v22_cross_source_findings_stage.py \
  tests/test_v22_verified_reprioritization_stage.py tests/test_v22_version_diff_stage.py \
  tests/test_v22_execution_plan_stage.py
```

Expected: PASS；V22-070～074 原有测试不变。

- [ ] **Step 6: 提交 Verified pipeline**

```bash
git add app/jobs_v22/verified_report_pipeline.py app/jobs_v22/prospect_report_pipeline.py \
  tests/verified_pipeline_helpers.py tests/test_v22_verified_report_pipeline.py
git commit -m "feat(v2.2): compose verified report pipeline"
```

### Task 7: 接入 executor、Worker、回调与失败补偿

**Files:**

- Create: `app/jobs_v22/verified_executor.py`
- Create: `app/jobs_v22/verified_reconciler.py`
- Create: `tests/test_v22_verified_executor.py`
- Create: `tests/test_v22_verified_reconciler.py`
- Modify: `app/jobs_v22/executor.py`
- Modify: `app/jobs_v22/worker.py`
- Modify: `app/jobs_v22/reconciler.py`
- Modify: `tests/test_v22_job_worker.py`
- Modify: `tests/integration/test_worker_restart_recovery.py`
- Modify: `tests/test_v22_cost_persistence.py`

- [ ] **Step 1: 写失败的 executor/router/Worker 测试**

覆盖：prospect envelope 仍走 `ProspectV22Executor`；verified envelope 只走
`VerifiedV22Executor`；Verified resolver→pipeline→persister 顺序正确；terminal success cost kind 为
`verified_report`；terminal technical failure callback 触发一次数据库 compensation；自动 Worker retry 不产生新 charge；process restart 从同一 envelope/checkpoints 恢复。

```python
await router.execute(job_id=JOB_ID, request=verified_envelope, submitted_at=NOW,
                     checkpoints=checkpoints, cost_ledger=ledger)
verified_executor.execute.assert_awaited_once()
prospect_executor.execute.assert_not_awaited()
```

- [ ] **Step 2: 运行测试并确认 router/Verified executor 缺失**

```bash
.venv/bin/python -m pytest -q tests/test_v22_verified_executor.py \
  tests/test_v22_job_worker.py tests/integration/test_worker_restart_recovery.py
```

Expected: FAIL，Verified route 尚未装配。

- [ ] **Step 3: 实现 executor router 和 Verified executor**

`VerifiedV22Executor.execute()` 校验 envelope/job identity，调用 job-bound resolver，校验 resolver 返回与小型 envelope 完全相同，再调用 pipeline 和 persister。`V22ExecutorRouter` 只按精确 schema version 路由，未知 schema 返回 `V22_ANALYSIS_REQUEST_INVALID`。

Worker 不再预先只解析 `AnalysisRequestEnvelope`；把 dict 交给 router。根据 schema 计算：

```python
job_kind = (
    "verified_report"
    if request.get("schema_version") == "v22_verified_request_envelope_v1"
    else "prospect_report"
)
```

所有 success/final-failure `_enqueue_cost_summary` 都传该 `job_kind`。

- [ ] **Step 4: 装配双 executor 与 orphan reconciliation**

`on_startup` 在 `V22_ANALYZE_ENABLED` 时装 prospect executor；在
`V22_VERIFIED_ANALYSIS_ENABLED` 时另建只供 resolver/persister 的 Supabase HTTP client、Verified pipeline 与 executor。router 对关闭的一侧使用 `UnavailableV22Executor`。

`verified_reconciler.py` 每分钟通过 service-role 调用
`expire_v22_stale_verified_jobs(now, 100)`；数据库已创建、但 Redis 因前端到 Railway 失败而不存在的 `verified_report` job，在 deadline 后进入 terminal failed，从而让通用
`apply_analysis_job_event` 对 `account_credit` charge 只补偿一次。HTTP 429/5xx/timeout 只记录安全 warning 并留给下一轮；返回 job IDs 限制 100，不打印 Case 或 payload。将该函数以 ARQ cron 注册为 `reconcile_v22_verified_orphans`。

- [ ] **Step 5: 运行 Worker、重启与财务回归**

```bash
.venv/bin/python -m pytest -q tests/test_v22_verified_executor.py tests/test_v22_job_worker.py \
  tests/test_v22_verified_reconciler.py tests/test_v22_cost_persistence.py
.venv/bin/python scripts/run_v22_redis_integration.py
```

Expected: PASS；Verified 失败余额回到原值，重放 callback 后不再增加。

- [ ] **Step 6: 提交 Worker 集成**

```bash
git add app/jobs_v22/verified_executor.py app/jobs_v22/verified_reconciler.py \
  app/jobs_v22/executor.py app/jobs_v22/worker.py app/jobs_v22/reconciler.py \
  tests/test_v22_verified_executor.py tests/test_v22_verified_reconciler.py \
  tests/test_v22_job_worker.py tests/test_v22_cost_persistence.py \
  tests/integration/test_worker_restart_recovery.py
git commit -m "feat(v2.2): execute verified durable jobs"
```

### Task 8: 接通 `$19 / 1 credit` Dodo checkout

**Files:**

- Create: `../search-trust/src/lib/verified-credits-v22/contracts.ts`
- Create: `../search-trust/src/lib/verified-credits-v22/repository.ts`
- Create: `../search-trust/src/lib/verified-credits-v22/handlers.ts`
- Create: `../search-trust/src/lib/verified-credits-v22/index.ts`
- Create: `../search-trust/src/lib/verified-credits-v22/handlers.test.ts`
- Create: `../search-trust/src/lib/verified-credits-v22/repository.test.ts`
- Create: `../search-trust/src/app/api/v2/cases/[id]/verified-credit/checkout/route.ts`
- Create: `../search-trust/src/app/api/v2/cases/[id]/verified-credit/checkout/confirm/route.ts`
- Modify: `../search-trust/src/lib/payments-v22/dodo.ts`
- Modify: `../search-trust/src/lib/payments-v22/dodo.test.ts`
- Modify: `../search-trust/src/app/api/webhook/dodo/route.ts`
- Create: `../search-trust/src/app/api/webhook/dodo/route.test.ts`
- Modify: `../search-trust/src/lib/payments-v22/handlers.test.ts`

- [ ] **Step 1: 写失败的 checkout/confirm/webhook 测试**

断言 product 使用 `DODO_VERIFIED_CREDIT_PRODUCT_ID`，metadata purchase kind 为
`case_verified_credit`，return/cancel URL 回到 `/cases/{caseId}/connections`。GET 返回当前 balance；POST 复用同一 pending checkout，但已 paid order 不阻止下一次购买；confirm 首次显示新增 1 credit，重放 `already_confirmed=true`，且从不请求 Verified analysis endpoint。

Webhook 按 purchase kind 分派 Prospect 与 Verified credit；未知 kind 继续安全忽略。Verified refund 显示 `manual_review` 时记录结构化 warning，不泄漏 payment body。

- [ ] **Step 2: 运行测试并确认 credit payment 模块缺失**

```bash
npm test -- src/lib/verified-credits-v22 src/lib/payments-v22
```

Expected: FAIL，verified credit routes/contracts 不存在。

- [ ] **Step 3: 泛化 Dodo metadata 类型但保持 Prospect 行为**

`CreateCheckoutInput.metadata.purchase_kind` 改为联合类型
`"case_prospect_report" | "case_verified_credit"`；safe URL、redaction、amount 与 provider 解析不放宽。现有 Prospect tests 必须逐字保持通过。

- [ ] **Step 4: 实现 verified credit repository 和 handlers**

repository 固定 pending order：

```ts
{
  user_id: userId,
  case_id: caseId,
  purchase_kind: "case_verified_credit",
  amount: 1900,
  currency: "USD",
  credits_purchased: 1,
  status: "pending",
}
```

handlers 复用 `DodoClient`，但使用独立 product env。fulfill/refund 只调用 Task 2 的 Verified RPC。成功响应：

```ts
{
  ok: true,
  case_id: caseId,
  payment_id: payment.payment_id,
  credits_added: 1,
  audit_credits: result.audit_credits,
  already_confirmed: result.idempotent,
}
```

- [ ] **Step 5: 实现 routes 与 webhook dispatch**

checkout route 只在 `GOOGLE_VERIFIED_ANALYSIS_ENABLED=true`、Dodo API key、base URL 和
`DODO_VERIFIED_CREDIT_PRODUCT_ID` 都存在时开放。confirm 仍允许 flag 刚关闭时完成已支付订单，避免丢失 payment settlement。Webhook 根据 verified metadata 调用新的 fulfill/refund helper。

- [ ] **Step 6: 运行支付、类型和 webhook tests**

```bash
npm test -- src/lib/verified-credits-v22 src/lib/payments-v22 src/app/api/webhook/dodo/route.test.ts
npm run typecheck
```

Expected: PASS；购买成功不触发分析，Prospect checkout 无回归。

- [ ] **Step 7: 提交 Verified credit checkout**

```bash
git add src/lib/verified-credits-v22 src/lib/payments-v22/dodo.ts \
  src/lib/payments-v22/dodo.test.ts src/app/api/webhook/dodo/route.ts \
  src/lib/payments-v22/handlers.test.ts src/app/api/v2/cases/\[id\]/verified-credit
git commit -m "feat(v2.2): sell one verified analysis credit"
```

### Task 9: 完成 Connection Center 的余额、CTA、任务恢复与 lineage

**Files:**

- Modify: `../search-trust/src/lib/connection-center/contracts.ts`
- Modify: `../search-trust/src/lib/connection-center/projector.ts`
- Modify: `../search-trust/src/lib/connection-center/repository.ts`
- Modify: `../search-trust/src/lib/connection-center/index.ts`
- Modify: `../search-trust/src/lib/connection-center/projector.test.ts`
- Modify: `../search-trust/src/lib/connection-center/repository.test.ts`
- Modify: `../search-trust/src/components/google/connection-center.tsx`
- Modify: `../search-trust/src/components/google/connection-center.test.tsx`
- Modify: `../search-trust/src/components/google/connection-center.interaction.test.tsx`
- Modify: `../search-trust/src/app/cases/[caseId]/connections/page.tsx`

- [ ] **Step 1: 写失败的 projector/repository/UI 测试**

新增状态矩阵：blocker 优先；ready+credits>0 显示
`Generate Verified Action Plan · uses 1 credit`；ready+credits=0 显示
`Buy 1 credit · $19`；job queued/running 显示进度；failed+compensated 显示
`1 credit returned` 与再次生成；succeeded 打开 database report。

repository 测试覆盖 Case.latest_report 为 Verified 时，沿其 `parent_report_id` 读到原 Prospect，
而不是把 Verified 传入 V22-073。

- [ ] **Step 2: 运行测试并确认现有永久 disabled CTA 失败**

```bash
npm test -- src/lib/connection-center src/components/google/connection-center
```

Expected: FAIL，缺少 balance/job state/action，按钮仍 disabled。

- [ ] **Step 3: 扩展 Connection Center projection**

Response 增加：

```ts
billing: { audit_credits: number };
verified_job: {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  report_id: string | null;
  charge_state: "reserved" | "consumed" | "compensated";
  error_code: string | null;
} | null;
```

repository 同时读取 owner balance、最新 `verified_report` job/charge 和 latest report。若 latest
report 是 Verified，验证其 Case 后读取它的 parent Prospect；若 latest 是 Prospect，直接使用。
`ConnectionCenterParentReportInput` 增加 `current_lineage: boolean`，projector 不再要求
`case.latest_report_id === parent.id`，而要求该布尔值为 true。

`index.ts` 将 flag 改为：

```ts
verified_generation_enabled:
  process.env.GOOGLE_VERIFIED_ANALYSIS_ENABLED === "true",
```

删除用户可见的覆盖百分比、coverage score 和 progressbar。页面只展示三项必需来源各自的
ready/blocker、整体“Ready to generate”状态以及下一步动作；数据库内部 coverage 字段和报告
Evidence coverage 合同不删除。

- [ ] **Step 4: 实现 CTA 与确认/购买/恢复交互**

生成按钮 click 先弹出明确确认；确认后生成 UUID job/idempotency key，POST
`/api/v2/cases/${caseId}/verified-analysis`。余额 0 时 POST verified-credit checkout 并跳转 Dodo URL。
任务开始后复用 `/api/v2/tasks/{jobId}` 状态，每 4 秒轮询；success 使用
`database_report_id ?? report.report_version.report_id` 跳转；failure 刷新 Connection Center，
只有数据库返回 charge `compensated` 才显示已返还。

购买 return query 含 payment ID 时调用 confirm，刷新余额后停留，不自动调用 Verified POST。

- [ ] **Step 5: 运行组件、类型与可访问性回归**

```bash
npm test -- src/lib/connection-center src/components/google/connection-center
npm run test:components
npm run typecheck
```

Expected: PASS；CTA 可键盘操作，错误/退款使用 `aria-live`，double click 只有一个 job/order。

- [ ] **Step 6: 提交 Connection Center 产品旅程**

```bash
git add src/lib/connection-center src/components/google/connection-center.tsx \
  src/components/google/connection-center.test.tsx \
  src/components/google/connection-center.interaction.test.tsx \
  src/app/cases/\[caseId\]/connections/page.tsx
git commit -m "feat(v2.2): enable verified generation journey"
```

### Task 10: 补齐离线 E2E、全量门禁、文档与正式发布准备

**Files:**

- Modify: `../search-trust/playwright.config.ts`
- Modify: `../search-trust/e2e/fixtures/google.ts`
- Modify: `../search-trust/e2e/fixtures/checkout.ts`
- Modify: `../search-trust/e2e/fixtures/report.ts`
- Modify: `../search-trust/e2e/support/api-router.ts`
- Modify: `../search-trust/e2e/verified-upgrade.spec.ts`
- Modify: `../search-trust/src/lib/e2e-v22/fixture-contracts.test.ts`
- Modify: `../search-trust/supabase/tests/database/v22_release_validation.test.sql`
- Modify: `../search-trust/supabase/tests/database/v22_release_residue.test.sql`
- Modify: `docs/superpowers/specs/2026-08-26-searchtrust-v2-2-development-plan.md`
- Modify: `docs/superpowers/specs/2026-09-13-searchtrust-v2-2-direct-production-release-design.md`
- Create: `docs/superpowers/specs/2026-09-13-searchtrust-v2-2-verified-generation-productization-completion.md`

- [ ] **Step 1: 写失败的完整浏览器旅程**

`verified-upgrade.spec.ts` 覆盖两条完整离线旅程：

1. 余额 1→确认生成→queued/running→Verified report，且 changes section 只有真实变化；
2. 余额 0→`Buy 1 credit · $19`→本地 Dodo fixture→confirm 后余额 1 且未生成→点击生成→受控 terminal failure→显示已返还→再次生成使用新 job。

```ts
await page.getByRole("button", { name: "Buy 1 credit · $19" }).click();
await expect(page.getByText("1 credit available")).toBeVisible();
await expect(page).toHaveURL(new RegExp(`/cases/${E2E_IDS.caseId}/connections`));
await expect(page.getByText("Generating your Verified Action Plan")).toHaveCount(0);
```

- [ ] **Step 2: 运行 E2E 并确认 fixture/router 尚未支持旅程**

```bash
npm run test:e2e -- e2e/verified-upgrade.spec.ts
```

Expected: FAIL，verified checkout/analysis local routes 未注册或 CTA 状态不匹配。

- [ ] **Step 3: 实现完全本地的 verified fixture 状态机**

`LocalApiScenario` 增加 `verifiedBalance`、`verifiedCheckoutPaid`、`verifiedAttempt` 和
`verifiedFailureCompensated`。所有 verified 路由只返回 `.invalid` identity 与固定 synthetic IDs；
不得请求 Supabase、Railway、Dodo、Google、PostHog 或其他外网。`playwright.config.ts` 增加
`GOOGLE_VERIFIED_ANALYSIS_ENABLED=true`，不放任何 secret。

- [ ] **Step 4: 扩展 migration release 验证**

schema/release SQL 断言新表/RPC/RLS/权限、purchase kind、ledger kinds 和 lineage；transaction
acceptance 执行成功购买、重复 webhook、成功生成、失败返还和重试。residue test 断言所有
`searchtrust_release_validation_%` fixture 在 rollback 后为 0。

- [ ] **Step 5: 运行前端全量门禁**

Run（工作目录 `search-trust`）：

```bash
npm run typecheck
npm test
npm run contracts:check
npm run build
npm run security:scan
npm run test:e2e:ci
npm run test:e2e:paused
```

Expected: 全部 PASS；浏览器测试无外网、artifact secret scan 无命中。

- [ ] **Step 6: 运行后端全量门禁**

Run（工作目录 `SearchTrust-RD`）：

```bash
.venv/bin/python scripts/run_v22_backend_quality.py release
```

Expected: fast pytest 与 Redis restart integration 全部 PASS；测试数高于既有 1,477。

- [ ] **Step 7: 更新计划依赖和完成记录**

开发计划中把 V22-093 从灰度改为直接正式发布，并注明 Verified 产品化是执行前置；直接发布
设计的 current-state/acceptance 不再声称未接通的能力已存在。completion 文档记录：精确
commit、测试计数、migration 数、schema/acceptance/residue 数、双开关仍关闭、正式配置仍未
写入。不得记录 secret value、OAuth token、payment payload 或客户数据。

- [ ] **Step 8: 提交测试与文档**

前端仓库：

```bash
git add playwright.config.ts e2e src/lib/e2e-v22/fixture-contracts.test.ts \
  supabase/tests/database/v22_release_validation.test.sql \
  supabase/tests/database/v22_release_residue.test.sql
git commit -m "test(v2.2): verify verified generation journey"
```

后端仓库：

```bash
git add docs/superpowers/specs/2026-08-26-searchtrust-v2-2-development-plan.md \
  docs/superpowers/specs/2026-09-13-searchtrust-v2-2-direct-production-release-design.md \
  docs/superpowers/specs/2026-09-13-searchtrust-v2-2-verified-generation-productization-completion.md
git commit -m "docs(v2.2): complete verified generation productization"
```

- [ ] **Step 9: 推送并等待质量门禁，暂不打开 Verified**

```bash
git push origin main
```

两个仓库分别推送；等待 GitHub quality、Vercel、Railway Web/Worker 使用准确 successful commit。
确认生产 health 200、error-level 初始日志为空。此步仍保持：

```text
GOOGLE_VERIFIED_ANALYSIS_ENABLED=false
V22_VERIFIED_ANALYSIS_ENABLED=false
GOOGLE_GBP_SYNC_ENABLED=false
V22_GBP_SYNC_ENABLED=false
```

- [ ] **Step 10: 按已批准正式验收顺序开放**

从已验证的 `20260912100000` 基线开始，必须按以下顺序应用全部 9 条新增 migration，不得只应用最初的两条：

1. `20260913100000_add_v2_2_verified_analysis_jobs.sql`；
2. `20260913110000_add_v2_2_verified_credit_payments.sql`；
3. `20260914100000_freeze_v22_public_gbp_source.sql`；
4. `20260914110000_atomic_v22_verified_settlement.sql`；
5. `20260914120000_allow_v22_verified_success_replay.sql`；
6. `20260914130000_fence_v22_verified_takeover_replay.sql`；
7. `20260914140000_bind_v22_verified_payment_settlement.sql`；
8. `20260914150000_persist_v22_verified_refund_reviews.sql`；
9. `20260914160000_coordinate_v22_verified_checkout_initialization.sql`。

应用后先用 release validation 确认数据库为完整 29 条有序 migration，再发布后端和前端。配置正式 `DODO_VERIFIED_CREDIT_PRODUCT_ID`，验证
GSC/GA4/OAuth broker 与 SerpAPI 公共 GBP。使用真实 Case 完成一次 `$19 / 1 credit` 购买，
确认只到账不自动生成；随后显式生成、验证新报告；再执行一次受控技术失败和重试。账本、
order、job、attempt charge、report、parent/snapshot IDs 全部一致后，才把两个 Verified flags
同时改为 true。

Expected: 正式旅程可用、官方 GBP flags 仍 false、没有 V2.1 路径、没有灰度 gate。

## 最终完成条件

- 两个仓库均 clean 且等于 `origin/main`；
- 正式 Supabase migration 顺序与本地一致；
- Verified purchase/charge/refund/retry 均有不可变 ledger；
- V22-070～074 从真实 durable Worker 链路执行；
- parent 永远是原始 Prospect，再次生成不形成 Verified parent 链；
- 购买不会自动生成，失败准确返还一次，再试准确再扣一次；
- GSC/GA4 必须健康匹配，公共 GBP 必须存在；
- 官方 GBP OAuth 继续关闭；
- Vercel、Railway Web/Worker 精确对应成功提交；
- 完成文档含测试、部署、生产验收和双开关证据，但不含任何 secret。
