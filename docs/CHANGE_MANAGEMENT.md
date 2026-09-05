# 跨系统变更与数据库交接规范

SearchTrust 的一项功能可能同时涉及前端、FastAPI 后端、Supabase 数据库和 Dify 工作流。本规范定义每次改动的责任边界与完成条件。

## 1. 变更面清单

每次开始实施前，必须识别并在交付说明中列出下列受影响的面：

| 变更面 | 责任方 | 完成依据 |
| --- | --- | --- |
| 前端 | Codex | 代码构建通过，目标页面/接口已验证 |
| 后端 | Codex | 测试通过，代码已推送并部署到 Railway |
| Dify 工作流 | 用户在 Dify 控制台执行；Codex 提供节点级步骤、Prompt、Schema 和验收方法 | 修改已发布，生产 API 调用已验证 |
| Supabase 数据库 | 用户在 Supabase SQL Editor 手动执行 | 用户确认 SQL 已在目标项目执行，且字段/索引/RLS 查询验证通过 |

## 2. 数据库变更的强制交接

只要改动触及以下任一情况，就视为数据库变更：

- 新增、删除、重命名或修改 Supabase 表、列、类型、默认值、约束、索引或 RLS Policy。
- 前端的 `reports` 查询、插入、更新或持久化字段发生变化。
- `src/types/database.ts`、`supabase/migrations/` 或报告持久化接口引用了新的字段。
- 报告 JSON 结构需要新增独立的持久化列，或现有 JSONB 数据需要回填。

发生数据库变更时，Codex 必须在实施前或随实现一并提供以下内容，不能只修改应用代码：

1. 目标 Supabase 项目、表名和每个字段的名称、类型、是否可空、默认值。
2. 可直接在 Supabase SQL Editor 执行的 SQL，包含安全的 `if exists` / `if not exists` 处理、必要索引、回填和回滚说明。
3. 对现有数据、RLS、前端读取和部署顺序的影响。
4. 用户需要手动执行的明确步骤，以及执行后的验证 SQL 或界面检查点。

**完成门槛：** 只要数据库 SQL 仍需要用户手动执行，相关任务必须标记为“等待数据库迁移”，不能声明为完成。代码中存在兼容回退也不改变这个门槛。

## 3. 当前已知数据库契约

前端报告持久化使用 Supabase 的 `reports` 表。Native v2.1 报告依赖以下字段：

| 表 | 字段 | 类型 | 用途 |
| --- | --- | --- | --- |
| `reports` | `report_v2_1` | `jsonb null` | 保存后端校验后的原生 `report_v2_1` 对象，供网页与 PDF 渲染 |

对应迁移文件位于前端项目：

`search-trust/supabase/migrations/20260707000000_add_report_v2_1_to_reports.sql`

```sql
alter table reports
  add column if not exists report_v2_1 jsonb null;
```

`/api/report-status` 在检测到列缺失时会退回保存旧报告字段，以保护旧环境；该回退会导致 Native v2.1 JSON 没有写入数据库。因此生产验收必须确认 `savedReportV21: true`，不能只以“报告已完成”或“旧字段已保存”作为成功标准。

## 4. 推荐实施顺序

1. Codex 完成前后端代码、测试、Dify Schema/Prompt 资料和数据库迁移 SQL。
2. 用户先在 Supabase 目标项目执行迁移，并确认验证结果。
3. 用户在 Dify 修改并发布工作流；若工作流改为新的 Dify App，还必须更新 Railway 的 `DIFY_API_KEY`。
4. Codex 推送后端/前端代码并等待对应部署完成。
5. 用真实 URL 验证完整链路：Dify 输出、后端契约、Supabase `reports.report_v2_1` 持久化、网页展示和 PDF 导出。

## 5. 交付格式

任何涉及多系统的变更，交付结果必须包含：

- 已完成：前端、后端、Dify、数据库中的具体完成项。
- 用户手动操作：Dify 节点/发布动作与 Supabase SQL（若有）。
- 阻塞项：未执行的数据库迁移、未发布的 Dify 工作流或未完成的部署。
- 验证结果：测试、生产任务 ID、持久化状态、网页与 PDF 结果。

当不存在数据库变更时，也必须明确写出“本次无数据库字段或迁移变更”。

## 6. v2.1 Evidence And Agency PDF Scope (2026-07)

The current v2.1 evidence-quality, GBP profile/alignment, schema summary, coverage,
and Lite Agency PDF changes all remain inside the existing `reports.report_v2_1`
JSONB payload or the request used to render one PDF.

- **Database action for this release:** none, provided `reports.report_v2_1 jsonb null` already exists in the target Supabase project.
- **Lite Agency PDF:** agency name, client name, footer note, and a locally uploaded PNG/JPEG logo are used only for the export request. They are not stored in Supabase, object storage, or a new table.
- **Future manual database handoff is required before implementation** if the product adds saved agency profiles, persisted logos, reusable client branding, share links, external-source history, or any new report column. Codex must first provide the table/field list and executable SQL under section 2.

## 7. v2.2 Revocable Report Shares (2026-09)

V22-043 adds one server-only table. The target is the same production Supabase
project that contains `users`, `client_cases`, and `reports`.

| Table | Field | Type | Null/default | Purpose |
| --- | --- | --- | --- | --- |
| `report_shares` | `id` | `uuid` | PK, `gen_random_uuid()` | Internal share record ID |
| `report_shares` | `user_id` | `uuid` | not null | Owner; cascades from `users` |
| `report_shares` | `case_id` | `uuid` | not null | Case boundary; cascades from `client_cases` |
| `report_shares` | `report_id` | `uuid` | not null | Shared report; cascades from `reports` |
| `report_shares` | `token_hash` | `text` | not null, unique | Lowercase SHA-256 digest; plaintext is never persisted |
| `report_shares` | `view_mode` | `text` | not null, `client` | Database-enforced client-only public view |
| `report_shares` | `expires_at` | `timestamptz` | not null, +30 days | Automatic link expiry |
| `report_shares` | `revoked_at` | `timestamptz` | null | Explicit revocation timestamp |
| `report_shares` | `last_accessed_at` | `timestamptz` | null | Last successful public access |
| `report_shares` | `created_at` | `timestamptz` | not null, `now()` | Audit timestamp |

The executable migration is:

`search-trust/supabase/migrations/20260904000000_add_v2_2_report_shares.sql`

It creates the table, token/expiry/ownership constraints, one-active-share index,
lookup indexes, RLS, service-role-only grants, and the atomic
`rotate_v22_report_share` function. Existing reports and v2.1 behavior are not
modified. Agency names, client names, footer notes, and uploaded logos remain
ephemeral PDF-request inputs and are not persisted.

Deployment order:

1. In the production Supabase SQL Editor, execute the complete migration file.
2. Run the three verification queries included at the bottom of that file.
3. Deploy the frontend application only after the table and RPC are present.
4. Create a share, open it signed out, revoke it, then confirm the same URL returns
   a not-found page.

Rollback is included as commented SQL at the bottom of the migration. It is
destructive because it removes every issued share.

Production migration status: **confirmed applied on 2026-09-04**. The user ran
the v2.2 Case model, Case invariants, job revision, Case payment, and report-share
migrations in dependency order; every Supabase SQL Editor run returned
`Success. No rows returned`. V22-043 is no longer waiting for database migration.

## 8. v2.2 Google OAuth Security Foundation (2026-09)

V22-050 extends the existing server-only `google_connections` table and adds
three server-only security tables in the same production Supabase project.

| Table | Change | Purpose |
| --- | --- | --- |
| `google_connections` | Adds `reauth_required` plus paired refresh-lease fields and stricter token-state constraints | Safe refresh coordination and terminal-state token clearing |
| `google_oauth_sessions` | Adds one-time state digest, encrypted PKCE verifier, requested scopes, and optional target connection | CSRF/PKCE protection for initial and incremental authorization |
| `google_connection_events` | Adds non-sensitive lifecycle events and stable result codes | Security audit without credentials or provider response bodies |
| `google_token_broker_requests` | Adds unique request ID and nonce SHA-256 digest | Durable replay protection across server instances and restarts |

The executable migration is:

`search-trust/supabase/migrations/20260905000000_add_v2_2_google_oauth_foundation.sql`

All four tables are protected by RLS and service-role-only grants. The database
stores no plaintext access token, refresh token, PKCE verifier, nonce, signature,
authorization code, or OAuth state. Existing connection token ciphertext remains
application-encrypted with versioned AES-256-GCM keys.

Deployment order:

1. Apply and verify the Supabase migration.
2. Deploy the frontend routes with `GOOGLE_CONNECTIONS_ENABLED` absent or false.
3. Complete Google consent-screen verification, GBP API approval, and production secret configuration.
4. Enable the feature only after a real-account acceptance test confirms scopes, refresh, revoke, and broker behavior.

Production migration status: **applied and verified on 2026-09-05**. The local and
remote migration catalogs both list version `20260905000000`. The OAuth routes
remain hidden and disabled in production pending external Google approval and a
separate live acceptance test.
