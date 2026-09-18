# SearchTrust V2.2 Client Report Delivery Implementation Plan

**Goal:** 将 Client 报告改为可直接发给客户、无需顾问讲解也能理解的独立交付，同时保持 Advisor 报告的完整证据链。

**Architecture:** 后端在完整 ReportV22 内新增必填 `client_delivery`，使用版本化固定模板和已验证 Finding/Evidence/Action 构造客户文案；报告模型严格校验引用、数量、字段长度和禁止内容。前端 Client view model 只投影最小报告头与 `client_delivery`，网页、分享链接和 Client PDF 共用同一模型。Advisor 继续使用完整报告。

**Tech Stack:** Python 3.13、Pydantic、FastAPI、pytest、Next.js 16、React 19、TypeScript 6、AJV、Vitest、Playwright、React PDF、Supabase/PostgreSQL、Railway、Vercel。

---

设计来源：`docs/superpowers/specs/2026-09-18-searchtrust-v2-2-client-report-design.md`

实施仓库：

- 后端、合同与本计划：`/Users/liuyun3/Documents/SearchTurst前后端/SearchTrust-RD`
- 前端、数据库与发布工作树：`/Users/liuyun3/Documents/SearchTurst前后端/search-trust/.worktrees/v22-verified-generation`

实施约束：

- 每个 Task 先写失败测试，再写最小实现。
- 后端 Pydantic 是 ReportV22 合同的唯一事实源；Schema、fixture、manifest 和 TypeScript 类型不得手改代替生成。
- 数据库只新增正向 migration，不改写已应用 migration。
- 不对 `2.2.0` 旧报告建立兼容 Client 投影。
- 任何 Client 安全失败都必须使报告任务失败，并沿现有结算流程退回 1 个点数。
- 不使用 Advisor 文案、Finding statement 或完整 URL 作为 Client 兜底。

## 文件结构

新增后端文件：

- `app/report_v22/client_delivery_catalog.py`：客户交付固定模板、规则/行动类型映射与禁止语言定义。
- `app/report_v22/client_delivery.py`：代表性证据选择、客户文案渲染、覆盖附录和结构校验。
- `tests/test_v22_client_delivery.py`：选择、追溯、安全边界、资源上限和确定性测试。

新增前端文件：

- `src/components/report/v22/client-decision.tsx`
- `src/components/report/v22/client-evidence-cards.tsx`
- `src/components/report/v22/client-priority-actions.tsx`
- `src/components/report/v22/client-roadmap.tsx`
- `src/components/report/v22/client-coverage-appendix.tsx`

新增数据库文件：

- `supabase/migrations/20260918100000_require_v22_client_delivery.sql`：将生产报告持久化、Verified 启动/持久化、成功重放与点数结算门禁升级为 `2.2.1`。

### Task 1: 建立 `client_delivery` 严格合同

**Files:**

- Modify: `app/report_v22/models.py`
- Create: `tests/test_v22_client_delivery_models.py`

- [ ] **Step 1: 先写失败的模型与合同测试**

测试直接构造最小合法 `ClientDelivery` 子模型，并断言：

- evidence cards 只能 1–3 张，每张 1–2 个 Finding 且最多 4 个 Evidence；
- priority actions 必须恰好三项、顺序为 1/2/3；
- roadmap 必须恰好三阶段；
- 所有字段长度和列表上限与设计文档一致。

- [ ] **Step 2: 运行目标测试确认红灯**

```bash
.venv/bin/python -m pytest -q tests/test_v22_client_delivery_models.py
```

Expected: FAIL，错误明确指向 `client_delivery` 子模型不存在。

- [ ] **Step 3: 实现 Pydantic 模型与 ReportV22 不变式**

在 `models.py` 增加：

- `ClientDecision`
- `ClientEvidenceCard`
- `ClientPriorityAction`
- `ClientRoadmapPhase`
- `ClientCoverageAppendix`
- `ClientDelivery`

本 Task 只建立可独立测试的严格子模型，暂不将字段接入 `ReportV22`，也不提升合同版本。这样不会在合同产物尚未同步时破坏现有 fixtures。

- [ ] **Step 4: 运行模型测试**

```bash
.venv/bin/python -m pytest -q tests/test_v22_client_delivery_models.py
```

Expected: PASS。

- [ ] **Step 5: 提交严格合同骨架**

```bash
git add app/report_v22/models.py tests/test_v22_client_delivery_models.py
git commit -m "feat(v2.2): add strict client delivery contract"
```

### Task 2: 实现确定性 Client Delivery Builder

**Files:**

- Create: `app/report_v22/client_delivery_catalog.py`
- Create: `app/report_v22/client_delivery.py`
- Create: `tests/test_v22_client_delivery.py`
- Modify: `tests/support/golden_reports.py`

- [ ] **Step 1: 写失败的选择与文案安全测试**

覆盖 Prospect 和 Verified 输入，断言：

- 相同输入始终生成相同字节结果；
- 优先按 TopAction 顺序选择证据，尽量覆盖不同 source type，最多三张卡；
- 每张卡只选择已被 Finding 引用且存在于最终 `evidence_index` 的健康 Evidence；
- 行动标题、原因、验收结果使用固定目录，不使用完整 `client_facing_explanation`；
- 没有合法映射的 action template/rule 明确失败，不把 Finding statement 原样复制给客户；
- 任何可展示文本包含 `http://`、`https://`、`v22_`、`fn_`、`ev_`、语义化版本串或已知原始错误码时失败；
- 拒绝排名、流量、线索或收入保证；
- `not_connected` / `not_checked` / 部分数据不得转译成“正常”或“通过”。

- [ ] **Step 2: 建立版本化 Client 文案目录**

`client_delivery_catalog.py` 使用 action template key 与有限的 rule family 映射，提供：

- `title`、`why_now`、`expected_result` 固定模板；
- evidence `observation`、`decision_relevance` 固定模板；
- 安全的 source label 与页面 subject label 渲染；
- Prospect 公开行动与 Verified measurement action 的完整覆盖。

模板只插入已验证的少量标量，不插入 URL 列表、Finding statement 或限制列表。

- [ ] **Step 3: 实现 builder 和安全扫描**

`client_delivery.py` 暴露 Prospect/Verified 两个明确入口，内部共享：

- 事实索引与引用校验；
- 代表性证据确定性选择；
- 客户文案渲染；
- 路线图与客户资产去重；
- checked/unavailable source 客户语言汇总；
- 递归可展示文本安全扫描。

- [ ] **Step 4: 运行 builder 测试**

```bash
.venv/bin/python -m pytest -q tests/test_v22_client_delivery.py
```

Expected: PASS，包括资源上限、非法引用、安全语言和重复执行测试。

- [ ] **Step 5: 提交 builder**

```bash
git add app/report_v22/client_delivery_catalog.py app/report_v22/client_delivery.py \
  tests/test_v22_client_delivery.py tests/support/golden_reports.py
git commit -m "feat(v2.2): build traceable client delivery copy"
```

### Task 3: 集成 Prospect / Verified 组装链路

**Files:**

- Modify: `app/report_v22/assembler.py`
- Modify: `app/report_v22/verified_report_assembler.py`
- Modify: `app/report_v22/models.py`
- Modify: `app/report_v22/execution_plan_models.py`
- Modify: `app/report_v22/examples.py`
- Modify: `tests/test_v22_report_assembler.py`
- Modify: `tests/test_v22_prospect_report_pipeline.py`
- Modify: `tests/test_v22_verified_report_pipeline.py`
- Modify: `tests/test_v22_prospect_executor.py`
- Modify: `tests/test_v22_verified_executor.py`
- Modify: `tests/test_v22_execution_plan_models.py`
- Modify: `tests/fixtures/report_v22_golden/*.json`

- [ ] **Step 1: 写失败的双链路集成测试**

断言 Prospect 与 Verified 最终报告：

- 都为 schema `2.2.1`；
- 都包含通过模型校验的 `client_delivery`；
- 重启/checkpoint 重放仍保持字节级稳定；
- Verified 报告的 Client action 顺序与 V22-072/074 一致；
- Client 生成失败会中止报告组装，不返回部分 ReportV22。
- Prospect/Verified executor 会将该失败转换为安全的 terminal failed 事件，不持久化报告或泄露客户原文。

- [ ] **Step 2: 在两个 assembler 中调用 builder**

首先将 `client_delivery: ClientDelivery` 接入 `ReportV22`，并在 `validate_contract_invariants()` 增加 Finding/Evidence 引用、共属 Evidence、TopAction 同源性与路线图顺序校验。同时将 `ReportVersion.schema_version` 改为 literal `2.2.1`。

Prospect assembler 使用 `PublicActionPlan.actions[*].template_key`、已保留 Evidence 和最终 DataCoverage 生成 Client Delivery。Verified assembler 使用 `verified_result.actions` 内的 public/measurement template key、最终 Findings/Evidence 与 first-party coverage 生成对应 Client Delivery。

`ExecutionPlanBuildResult` 新增不变式：最终 report Client action IDs 与执行计划完全一致。

- [ ] **Step 3: 更新确定性 examples 与金色报告**

```bash
.venv/bin/python scripts/check_v22_report_goldens.py --accept
.venv/bin/python -m pytest -q tests/test_v22_report_goldens.py
```

检查金色差异：只允许 schema 版本、`client_delivery` 与由它导致的确定性摘要变化；Advisor 事实与 Evidence 不得意外变化。

- [ ] **Step 4: 运行目标集成测试**

```bash
.venv/bin/python -m pytest -q \
  tests/test_v22_report_assembler.py \
  tests/test_v22_prospect_report_pipeline.py \
  tests/test_v22_verified_report_pipeline.py \
  tests/test_v22_prospect_executor.py \
  tests/test_v22_verified_executor.py \
  tests/test_v22_execution_plan_models.py \
  tests/test_v22_report_goldens.py
```

Expected: PASS。

- [ ] **Step 5: 保留本地变更并直接进入 Task 4**

此时 `ReportV22` 已发生破坏性变更，而已提交的 Schema/fixture 仍是 `2.2.0`。不单独提交这个中间状态；Task 4 完成生成与同步后，再把后端集成与合同产物一起提交。

### Task 4: 导出 `2.2.1` 合同并同步前端

**Files:**

- Modify: `contracts/v2.2/report_v2_2.schema.json`
- Modify: `contracts/v2.2/api_v2.schema.json`
- Modify: `contracts/v2.2/fixtures/prospect.json`
- Modify: `contracts/v2.2/fixtures/verified.json`
- Modify: `contracts/v2.2/manifest.json`
- Modify: `tests/fixtures/report_v22_validation/*`
- Modify: `app/report_v22/contract_version.py`
- Modify: `tests/test_report_v22_contract.py`
- Modify: `tests/test_api_v2_contract.py`
- Modify: `../search-trust/.worktrees/v22-verified-generation/src/lib/report-v22/contracts/*`
- Modify: `../search-trust/.worktrees/v22-verified-generation/src/lib/report-v22/generated/types.ts`
- Modify: `../search-trust/.worktrees/v22-verified-generation/src/lib/report-v22/generated/normalization-data.json`
- Modify: `../search-trust/.worktrees/v22-verified-generation/src/lib/report-v22/test-fixtures/validation/*`

- [ ] **Step 1: 先运行合同检查确认漂移**

```bash
.venv/bin/python scripts/export_v22_contracts.py \
  --frontend-dir '/Users/liuyun3/Documents/SearchTurst前后端/search-trust/.worktrees/v22-verified-generation' \
  --check
```

Expected: FAIL，列出 Schema、fixture 和 manifest 差异。

- [ ] **Step 2: 使用后端生成器同步两仓库**

先将 `app/report_v22/contract_version.py` 的 `CONTRACT_VERSION` 改为 `2.2.1`，并将合同测试的版本期望更新为 `2.2.1`。然后执行：

```bash
.venv/bin/python scripts/export_v22_contracts.py \
  --frontend-dir '/Users/liuyun3/Documents/SearchTurst前后端/search-trust/.worktrees/v22-verified-generation'
.venv/bin/python scripts/sync_v22_validation_resources.py \
  --frontend-dir '/Users/liuyun3/Documents/SearchTurst前后端/search-trust/.worktrees/v22-verified-generation'
```

- [ ] **Step 3: 生成 TypeScript 类型并验证合同对等**

```bash
npm run contracts:generate
npm run test:contract
npm run contracts:check
```

工作目录：前端工作树。Expected: PASS，生成类型中包含严格 `ClientDelivery`。

- [ ] **Step 4: 分仓库提交合同产物**

后端：

```bash
git add app/report_v22/contract_version.py app/report_v22/models.py \
  app/report_v22/assembler.py app/report_v22/verified_report_assembler.py \
  app/report_v22/execution_plan_models.py app/report_v22/examples.py \
  tests/test_v22_report_assembler.py tests/test_v22_prospect_report_pipeline.py \
  tests/test_v22_verified_report_pipeline.py tests/test_v22_prospect_executor.py \
  tests/test_v22_verified_executor.py tests/test_v22_execution_plan_models.py \
  tests/test_report_v22_contract.py tests/test_api_v2_contract.py \
  tests/fixtures/report_v22_golden tests/fixtures/report_v22_validation contracts/v2.2
git commit -m "feat(v2.2): assemble client-ready report contract"
```

前端：

```bash
git add src/lib/report-v22/contracts src/lib/report-v22/generated \
  src/lib/report-v22/test-fixtures/validation
git commit -m "chore(v2.2): sync client delivery contract"
```

### Task 5: 用正向 migration 升级数据库报告门禁

**Files:**

- Create: `supabase/migrations/20260918100000_require_v22_client_delivery.sql`
- Modify: `src/lib/database/v22Migration.test.ts`
- Modify: `supabase/tests/database/v22_release_validation.test.sql`

- [ ] **Step 1: 写失败的数据库回归测试**

新增 `2.2.1` 合法 Prospect/Verified payload，并断言：

- `persist_v22_prospect_result` 只接受内外 schema 均为 `2.2.1` 且存在 `client_delivery` 的报告；
- `start_v22_verified_analysis` 只选择 `2.2.1` Prospect parent；
- `persist_v22_verified_result`、success replay、report-link settlement 只接受 `2.2.1`；
- 缺失 `client_delivery`、内外版本不一致或 `2.2.0` payload 均在写入/结算前失败；
- 失败不产生报告链接、成功结算或重复扣点。

- [ ] **Step 2: 运行数据库测试确认旧门禁失败**

```bash
npm run test:database
```

Expected: FAIL，因现有生产函数仍要求 `2.2.0`。

- [ ] **Step 3: 新增正向 migration**

Migration 从当前最新函数定义复制并仅做必要升级，覆盖：

- Prospect 结果持久化；
- Verified 启动、输入解析、结果持久化；
- Verified 成功重放与 report-link settlement；
- 与已成功报告绑定的点数防重/补偿保护。

所有函数保持现有签名、权限、`search_path`、幂等性和锁顺序。新增 `jsonb_typeof(report_v2_2->'client_delivery') = 'object'` 门禁，不在数据库内重复完整 Pydantic/AJV 语义校验。

- [ ] **Step 4: 运行数据库套件**

```bash
npm run test:database
```

Expected: PASS。

- [ ] **Step 5: 提交 migration**

```bash
git add supabase/migrations/20260918100000_require_v22_client_delivery.sql \
  src/lib/database/v22Migration.test.ts supabase/tests/database/v22_release_validation.test.sql
git commit -m "feat(v2.2): require client delivery reports"
```

### Task 6: 建立严格 Client view model

**Files:**

- Modify: `src/lib/report-v22/view-model.ts`
- Modify: `src/lib/report-v22/view-model.test.ts`
- Modify: `src/components/report/v22/report-v22.test.tsx`

- [ ] **Step 1: 写失败的隔离测试**

对 `buildReportV22ViewModel(report, "client")` 序列化结果断言：

- 仅含最小 header、decision、evidenceCards、priorityActions、roadmap、clientInputs、coverageAppendix；
- 不含 `findings`、`evidence`、`layers`、`dataCoverage`、`marketSnapshot`、`siteInventory`、`reportMetadata`、`implementationSteps`、`validationMetrics`或 `exactTargets`；
- 不含 fixture 中任何 rule/Finding/Evidence ID、错误码或 URL；
- Advisor view model 仍包含完整证据字段。

- [ ] **Step 2: 改造 Client 投影**

删除 Client view model 对 `client_summary`、`competitor_analysis`、`top_actions.client_facing_explanation` 和原 `roadmap` 文案的展示依赖；只映射 `client_delivery`。

Advisor 类型和构造逻辑不变，除了适配 schema `2.2.1`。

- [ ] **Step 3: 运行投影与渲染测试**

```bash
npm test -- src/lib/report-v22/view-model.test.ts \
  src/components/report/v22/report-v22.test.tsx
```

Expected: PASS。

- [ ] **Step 4: 提交 Client 数据隔离**

```bash
git add src/lib/report-v22/view-model.ts src/lib/report-v22/view-model.test.ts \
  src/components/report/v22/report-v22.test.tsx
git commit -m "refactor(v2.2): isolate client report data"
```

### Task 7: 实现四段式 Client 网页

**Files:**

- Create: `src/components/report/v22/client-decision.tsx`
- Create: `src/components/report/v22/client-evidence-cards.tsx`
- Create: `src/components/report/v22/client-priority-actions.tsx`
- Create: `src/components/report/v22/client-roadmap.tsx`
- Create: `src/components/report/v22/client-coverage-appendix.tsx`
- Modify: `src/components/report/v22/client-report-view.tsx`
- Modify: `src/components/report/v22/report-v22-shell.tsx`
- Modify: `src/components/report/v22/report-v22.test.tsx`

- [ ] **Step 1: 写失败的结构与语义测试**

断言 Client 页面：

- 按顺序渲染 Core decision、Representative evidence、Three priority moves、90-day plan / Client inputs 和简短附录；
- 导航与页内 anchor 一致；
- 显示 `Client report`，不显示“Diagnostic findings”、“Eight layers”、“Coverage and source health”或“Interpretation limits”；
- 每个 action card 包含结果、复查日期、工作量和客户配合项；
- 没有可用来打开 Advisor Evidence Drawer 的控件。

- [ ] **Step 2: 实现已批准的视觉结构**

复用当前 SearchTrust 深绿/酸橙绿视觉系统和响应式断点，但将 Client 页面层级改为：

1. 高对比核心判断；
2. 1–3 张易读证据卡；
3. 三张按顺序行动卡；
4. 30/60/90 路线图和 Client inputs；
5. 弱化的覆盖附录。

不在渲染层做文案截断或正则替换；组件只展示已验证的 view model。

- [ ] **Step 3: 运行组件与类型测试**

```bash
npm test -- src/components/report/v22/report-v22.test.tsx
npm run typecheck
```

Expected: PASS。

- [ ] **Step 4: 提交 Client 网页**

```bash
git add src/components/report/v22/client-*.tsx \
  src/components/report/v22/client-report-view.tsx \
  src/components/report/v22/report-v22-shell.tsx \
  src/components/report/v22/report-v22.test.tsx
git commit -m "feat(v2.2): deliver focused client report view"
```

### Task 8: 对齐 Client PDF、分享链接和 E2E

**Files:**

- Modify: `src/components/report/pdf/ReportV22PDFDocument.tsx`
- Modify: `src/components/report/v22/shared-client-report-shell.tsx`
- Modify: `src/components/report/v22/shared-report-entry.tsx`
- Modify: `src/lib/report-shares/service.ts`
- Modify: `src/app/api/reports/[id]/pdf/route.ts`
- Modify: `src/components/report/v22/report-v22.test.tsx`
- Modify: `e2e/fixtures/report.ts`
- Modify: `e2e/prospect-report.spec.ts`
- Modify: `e2e/report-sharing.spec.ts`

- [ ] **Step 1: 先写 PDF/分享与端到端失败测试**

覆盖：

- Client PDF 按与网页相同顺序输出四段式内容；
- Client PDF 不包含 rule ID、Finding/Evidence ID、原始错误码、URL 列表、八层诊断或 Data Coverage 详情；
- Advisor PDF 仍包含完整 Findings、八层与 coverage；
- 公开分享页只解析 Client view model，无 Advisor 切换或内部数据；
- 无效 `client_delivery` 返回统一失败状态，不降级显示 Advisor 原文。

- [ ] **Step 2: 改造 Client PDF 组件**

PDF 复用 `ClientReportV22ViewModel`，保留 agency branding、客户名称和页脚；仅 Advisor 变体渲染诊断页、规则追溯与数据覆盖。

- [ ] **Step 3: 更新共享服务与 E2E fixture**

共享 service 在生成 view model 前完成 ReportV22 AJV 校验。E2E fixture 只来自同步的 `2.2.1` 合同 fixture，不手写另一份不完整 Client payload。

- [ ] **Step 4: 运行目标测试**

```bash
npm test -- src/components/report/v22/report-v22.test.tsx \
  src/lib/report-v22/view-model.test.ts
npx playwright test e2e/prospect-report.spec.ts e2e/report-sharing.spec.ts
```

Expected: PASS。

- [ ] **Step 5: 提交三个 Client 出口**

```bash
git add src/components/report/pdf/ReportV22PDFDocument.tsx \
  src/components/report/v22/shared-client-report-shell.tsx \
  src/components/report/v22/shared-report-entry.tsx \
  src/lib/report-shares/service.ts src/app/api/reports/[id]/pdf/route.ts \
  src/components/report/v22/report-v22.test.tsx \
  e2e/fixtures/report.ts e2e/prospect-report.spec.ts e2e/report-sharing.spec.ts
git commit -m "feat(v2.2): align client report exports"
```

### Task 9: 全量质量门与发布前复核

**Files:**

- Modify only if tests expose in-scope defects.

- [ ] **Step 1: 运行后端快速质量门**

```bash
.venv/bin/python scripts/run_v22_backend_quality.py fast
.venv/bin/python scripts/export_v22_contracts.py \
  --frontend-dir '/Users/liuyun3/Documents/SearchTurst前后端/search-trust/.worktrees/v22-verified-generation' \
  --check
.venv/bin/python scripts/check_v22_report_goldens.py
```

Expected: PASS，合同和金色报告无漂移。

- [ ] **Step 2: 运行前端全量质量门**

```bash
npm run typecheck
npm test
npm run contracts:check
npm run build
npm run security:scan
npm run test:e2e:ci
```

Expected: PASS。

- [ ] **Step 3: 人工安全差异复核**

必须确认：

- Advisor 事实、Evidence、规则和实施边界未被删除或改写；
- Client 三个出口中没有内部 ID、原始错误码、完整 URL 列表或因果/结果承诺；
- 失败路径仍产生 terminal failed 事件并退回一个 credit；
- 没有密钥、token、原始 provider 载荷或客户证据原文出现在日志和测试产物。

- [ ] **Step 4: 保证两仓库工作树只包含本任务变更**

```bash
git status --short
git diff --check
```

Expected: 无意外文件、无空白错误。

### Task 10: 生产迁移、发布与真实报告验收

**Files:**

- Migration from Task 5.
- No application source edits unless production verification finds an in-scope defect.

- [ ] **Step 1: 先应用数据库 migration**

确认远程 migration 列表包含 `20260918100000_require_v22_client_delivery.sql`，并运行发布 SQL 验证：

- 所有重建函数的签名、owner、`search_path` 和 service-role-only 权限正确；
- `2.2.0` 无 Client Delivery payload 不能进入新生成链路；
- `2.2.1` 有 Client Delivery payload 可持久化与结算。

- [ ] **Step 2: 发布 Railway Worker**

部署后记录 deployment ID，等待明确 `SUCCESS`，并检查 health endpoint。不以“已推送代码”替代部署成功证据。

- [ ] **Step 3: 发布 Vercel 前端**

推送已通过质量门的前端 commit，确认生产 deployment 为 `READY`，并确认 `trysearchtrust.com` 指向新版本。

- [ ] **Step 4: 清理已确认的测试报告数据**

先用只读 SQL 列出精确 Case/Report/Job/Share/snapshot 目标及数量，再在单一事务中清理。不删除：

- Clerk/Supabase user 账号；
- 付款订单与 Dodo 审计；
- credit ledger 与现有余额；
- 迁移历史。

清理后复查所有关联计数，确认没有孤儿引用。

- [ ] **Step 5: 生成一份新的真实 Prospect 报告**

记录 Case ID、Job ID、Report ID、开始/结束时间、credit 前后余额和快照健康状态。验证：

- 成功生成只扣 1 credit；
- 报告为 schema `2.2.1` 且完整包含 `client_delivery`；
- Advisor 报告显示完整证据；
- Client 报告完全符合四段式结构；
- Client share 与 Client PDF 内容一致；
- Advisor PDF 仍有完整诊断。

- [ ] **Step 6: 验证可控失败与点数退回**

仅使用不会产生外部成本或不可恢复数据的可控失败路径。确认任务 terminal failed，credit 原子退回一次，重放不会多退。

- [ ] **Step 7: 交付发布记录**

最终交付包含：

- 后端与前端 commit ID；
- 远程 migration 版本；
- Railway deployment ID 和 SUCCESS；
- Vercel deployment URL/ID 和 READY；
- 生产 Case/Job/Report ID；
- Advisor、Client、Client share、Client PDF、Advisor PDF 验收结果；
- credit 成功扣减与失败退回证据；
- 未完成或需用户操作的项目（如有）。

## 完成定义

只有同时满足以下条件才能标记完成：

- ReportV22 `2.2.1` 合同在两仓库字节一致；
- Prospect 与 Verified 都生成可追溯、安全的 `client_delivery`；
- Advisor 完整证据链没有回归；
- Client 网页、分享链接和 PDF 不泄露内部诊断，且文案/顺序一致；
- 前后端全量质量门通过；
- 数据库 migration 先于代码发布并验证；
- Railway 和 Vercel 生产部署均明确成功；
- 一份全新真实生产报告通过 Advisor / Client / share / 双 PDF 验收；
- credit 成功扣减与失败退回都被实测。
