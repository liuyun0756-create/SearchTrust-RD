# SearchTrust v2.2 完整开发计划

状态：实施中（V22-090 前端测试基础设施已完成）

日期：2026-08-26

适用仓库：

- 前端：`/Users/liuyun3/Documents/SearchTurst前后端/search-trust`
- 后端：`/Users/liuyun3/Documents/SearchTurst前后端/SearchTrust-RD`

## 1. 版本目标

SearchTrust v2.2 将当前“单页信任审计报告”升级为“Local SEO 客户决策与行动系统”。同一个客户项目支持两个连续阶段：

1. **Local SEO 获客报告（Prospect Opportunity Report）**：无需 Google 数据授权，使用全站公开数据、SerpAPI、公开 GBP 和真实竞争对手，帮助顾问向潜在客户证明机会并给出三项行动。
2. **Local SEO 验证执行计划（Verified Client Action Plan）**：在同一客户项目中连接 GSC、GBP 和 GA4，用第一方数据验证或调整结论，生成三项可执行行动、30/60/90 天路线图和复查基线。

v2.2 的商业验证目标不是“用户觉得报告不错”，而是验证 Local SEO 顾问是否愿意真实支付 $19、把报告用于客户工作、连接第一方数据并执行建议。

## 2. 发布门槛

以下能力全部完成、通过验收后，版本才能标记为 v2.2：

- 一个客户项目、多份不可变报告版本；
- 零授权获客流程；
- 全站结构检查和重点页面深度分析；
- SerpAPI Maps、Local Pack、自然搜索和公开 GBP 数据；
- 三个真实本地竞争对手；
- 完整八层信任结构；
- 一个核心问题和严格排序的三项行动包；
- 结论、比较、行动到原始证据的追溯；
- 顾问版、客户版、PDF 和安全分享链接；
- GSC OAuth、资源选择、同步和健康检查；
- GBP OAuth、Location 选择、资料/Performance 同步和健康检查；
- GA4 OAuth、Property 选择、同步和健康检查；
- GSC、GBP、GA4 与客户实体的匹配；
- 跨数据源结论和 Full Evidence Coverage；
- 获客报告升级为验证执行计划；
- Confirmed、Reprioritized、Refined、Replaced、New 版本差异；
- 付费失败恢复、任务持久化、成本限制和商业埋点；
- GBP API 合规、令牌安全、数据删除和30天内容保留策略。

## 3. 不进入 v2.2 的范围

- 大规模 Geo-grid；
- 全量外链审计；
- Citation 管理；
- 自动修改网站；
- 自动发布或修改 GBP；
- CRM 收入归因；
- 大规模关键词持续监控；
- 完整月度订阅和行动项目管理；
- 自动后台同步 GBP；v2.2 的 GBP 同步由用户在产品中主动触发。

## 4. 当前系统基线

### 4.1 前端现状

技术栈：Next.js 16、React 19、TypeScript、Clerk、Supabase、Dodo Payments、PostHog、React PDF。

已有能力：

- Clerk 用户认证；
- Credits 和 Dodo Payments 支付；
- 报告创建、SSE 进度、持久化、历史列表、PDF 和邮件发送；
- `report_v2_1` 标准化读取；
- 八层报告组件、GBP 对齐、证据展示和客户预览；
- Supabase `reports`、`users`、`orders` 基础表。

主要缺口：

- 没有客户项目模型；
- 当前报告仍以 `page_url` 为核心；
- 没有报告父子版本和升级差异；
- 没有 Google 数据连接、资源绑定、令牌存储和数据同步；
- 没有公开数据预检和商家/竞品确认流程；
- 没有前端自动化测试体系；
- 支付仍以审计 Credit 为中心，而不是客户项目交付。

### 4.2 后端现状

技术栈：FastAPI、Pydantic、异步任务、Firecrawl/Jina、SerpAPI、Dify。

已有能力：

- 页面和内部页面抓取；
- GBP 自动发现、公开资料和评论抓取；
- v2.1 八层规则、证据账本、确定性评分和质量校验；
- Dify 仅生成报告文案、后端绑定证据的基础；
- SSE 任务进度和失败状态；
- 完整的 pytest 规则、GBP、证据和报告契约测试。

主要缺口：

- 当前 API 仍是页面级 `/api/v1/analyze`；
- 没有客户、目标服务、目标地区、关键词和竞品上下文；
- 没有完整站点库存与分层深度分析；
- 没有独立 SERP/竞品采集层；
- 没有 GSC、GBP Performance、GA4 证据输入；
- 没有 v2.2 报告契约、跨源规则和版本差异；
- 当前任务状态存储在单进程内存，重启会丢失，不适合更长的付费 v2.2 流水线。

## 5. 统筹关系与工程取舍

### 5.1 首次转化摩擦 ↔ 第一方数据完整度

当前阶段优先首次转化，最低限度是不牺牲客户交付完整性：

- 获客版付款前零 Google 授权；
- 顾问拿下客户后进入统一数据连接中心；
- 使用增量授权，不在首次 Google 同意页一次请求所有权限；
- 验证执行版必须实现 GSC、GBP、GA4，但允许单个数据源显示不健康并给出修复路径。

失衡预警：付款前出现 OAuth、授权退出率上升、用户无法在没有客户权限时购买。

### 5.2 开发速度 ↔ 报告可信度

当前阶段优先可信度，最低限度是保留可控交付速度：

- 事实、数字、匹配、覆盖和优先级由确定性代码负责；
- LLM 只输出绑定 `finding_id` 和 `evidence_id` 的说明文案；
- 没有证据的文案校验失败，不允许静默降级成“看起来完整”的报告；
- 复用 v2.1 证据与规则能力，不重写已经稳定的基础。

失衡预警：报告出现无法追溯数字、相同输入产生不同前三项行动、LLM 改写原始数据。

### 5.3 全站深度 ↔ API 成本和生成时间

当前平衡点：

- 最多发现并做结构检查的客户 URL：500；
- 最多深度分析的客户页面：50；
- 每个竞争对手最多深度分析页面：10；
- 竞争对手数量：3；
- 核心查询：3—5；
- 基础报告搜索位置：1个主要目标点；
- PageSpeed 深度检查页面：最多5个；
- 评论样本：每个商家最多30条，保存和展示遵守数据源政策。

超过限制必须显示覆盖范围，不得声称无限全站检查。

失衡预警：P95 报告生成时间超过15分钟、单份公开报告外部 API 成本超过内部预算、抓取失败率持续上升。

### 5.4 新架构 ↔ v2.1 兼容

当前阶段优先隔离：

- 保留 `/api/v1` 和 `report_v2_1` 只读兼容；
- 新增 `/api/v2`、`report_v2_2` 和客户项目数据模型；
- 不原地改变 v2.1 Pydantic/TypeScript 合同语义；
- 新报告必须写入 v2.2 字段，旧报告继续由现有适配器读取。

失衡预警：v2.2 迁移导致历史报告打不开、PDF 失败或 v2.1 线上流程回归。

### 5.5 证据长期追溯 ↔ GBP 数据政策

当前阶段优先合规：

- GBP API 原始内容设置不超过30个日历日的 TTL；
- v2.2 不做自动后台 GBP 同步，必须由授权用户主动触发；
- 官方 GBP API 只用于用户已授权管理的客户 Location；获客预检、潜在客户发现和竞品发现继续使用 SerpAPI 与公开网页，禁止使用 GBP `GoogleLocations` endpoint 做 lead generation；
- 报告只保留经政策审查允许的必要派生结论、来源元数据和覆盖说明；
- 到期证据显示“原始 GBP 快照已按政策过期，需要重新同步”，不得伪装成仍可打开；
- 断开客户关系时立即撤销绑定、删除令牌，并在七个工作日政策要求内完成解除。

失衡预警：原始 GBP 内容超过30天、用户不能自助断开、报告把过期数据显示为当前数据。

## 6. 目标架构

```text
Clerk User
   ↓
Next.js Case & OAuth Layer
   ├─ Supabase: cases / bindings / snapshots / report versions
   ├─ Dodo Payments
   └─ Encrypted Google tokens
   ↓ bounded, normalized context only
FastAPI v2 Analysis API
   ├─ Durable Redis/ARQ job queue
   ├─ Site collector
   ├─ SerpAPI market collector
   ├─ Competitor collector
   ├─ Evidence store builder
   ├─ Cross-source decision engine
   ├─ Dify narrative copy adapter
   └─ report_v2_2 validator
   ↓
Immutable report_v2_2 result
   ↓
Next.js persistence / advisor view / client view / PDF / share
```

### 6.1 边界决定

- Clerk 继续负责 SearchTrust 登录，不与 Google 数据授权混合。
- Next.js 负责 Google OAuth、令牌加密、资源选择、数据同步和 Supabase 持久化。
- Python 后端永远不接收 Google refresh token。
- Next.js 将经过裁剪和标准化的 GSC/GBP/GA4 快照传给后端分析；原始令牌只存在于 Next.js 服务端。
- 后端负责公开数据采集、证据归一化、规则、优先级、报告合同和版本差异。
- Dify 不拥有事实和数字，只负责基于固定 finding/action 输入生成客户可读文案。
- Redis/ARQ（或团队锁定的等价 Redis 异步队列）替换付费 v2.2 流程的纯内存任务状态；v1 暂时保留旧路径。

## 7. 数据模型计划

在前端仓库新增 Supabase migration，不破坏现有表。

### 7.1 `client_cases`

核心字段：

- `id uuid`；
- `user_id uuid`；
- `site_url text`；
- `normalized_domain text`；
- `business_name text`；
- `business_identity jsonb`；
- `operating_model text`：`storefront` / `service_area` / `hybrid`；
- `primary_service text`；
- `target_market jsonb`；
- `status text`；
- `latest_report_id uuid`；
- `created_at`、`updated_at`。

约束：同一用户可有多个相同域名但不同 Location 的 Case；唯一性不能只用域名。

### 7.2 `case_source_bindings`

每个 Case 绑定外部资源：

- `source_type`：`gsc` / `ga4` / `gbp`；
- `connection_id`；
- `external_resource_id`；
- `external_resource_name`；
- `identity_match_status`；
- `identity_match_evidence jsonb`；
- `health_status`；
- `health_reasons jsonb`；
- `last_synced_at`。

每个 Case 每种 source 只允许一个 active binding。

### 7.3 `google_connections`

- `user_id`；
- Google subject/account identity；
- granted scopes；
- 加密 access token、refresh token；
- token expiry；
- connection status；
- last error；
- revoked/deleted timestamps。

安全要求：

- AES-256-GCM 或经过安全评审的等价应用层加密；
- 加密密钥仅在服务端 Secret Store；
- 数据库永远不保存明文 refresh token；
- 日志、PostHog、Sentry 和错误响应不得出现 token；
- 提供自助 Disconnect，撤销 Google token 并删除本地密文。

### 7.4 `data_snapshots`

- `case_id`；
- `source_type`：`site` / `serp` / `competitor` / `gsc` / `gbp` / `ga4` / `pagespeed`；
- `schema_version`；
- `coverage_start`、`coverage_end`；
- `fetched_at`、`expires_at`；
- `sync_trigger`；
- `health_status`；
- `health_reasons`；
- `normalized_payload jsonb`；
- `raw_payload jsonb`，仅在允许且必要时保存；
- `payload_checksum`；
- `provider_request_context jsonb`；
- `supersedes_snapshot_id`。

快照不可修改，只能新建。GBP Content 的 `expires_at` 不得超过获取后30天，并由清理任务删除受限原始内容。

### 7.5 扩展现有 `reports`

新增：

- `case_id`；
- `report_type`：`prospect` / `verified_execution`；
- `schema_version`；
- `version_number`；
- `parent_report_id`；
- `report_v2_2 jsonb`；
- `snapshot_ids uuid[]`；
- `coverage_state jsonb`；
- `version_diff jsonb`；
- `generation_config jsonb`；
- `ruleset_version`；
- `copy_model_version`。

旧报告这些字段为空，继续走 v2.1 渲染路径。v2.2 报告写入后不可 PATCH 修改正文，只允许修改分享、品牌等展示元数据。

### 7.6 `analysis_jobs`

用于跨服务恢复和审计：

- `id`、`case_id`、`report_id`；
- `job_type`；
- `status`；
- `current_stage`；
- `progress`；
- `attempt_count`；
- `error_code`、`user_message`；
- `cost_counters jsonb`；
- `started_at`、`heartbeat_at`、`completed_at`。

Redis 负责队列和实时状态，Supabase `analysis_jobs` 保存持久审计状态。服务重启后可识别未完成任务并安全重试或标记失败退款。

## 8. Google OAuth 与数据同步

2026-09-07 决策更新：v2.2 默认使用现有 SerpAPI 三 Key 轮换链路获取公开 GBP
资料、评论与公开活跃信号，不将官方 GBP 后台账号作为 Verified Core 的阻塞条件。
官方 GBP Performance 保留为可选增强；只有 GSC、官方 GBP Performance 和 GA4 全部
健康且身份匹配时，才允许显示 Full Evidence。

### 8.1 OAuth scopes

- GSC：`https://www.googleapis.com/auth/webmasters.readonly`；
- GA4：`https://www.googleapis.com/auth/analytics.readonly`；
- GBP：`https://www.googleapis.com/auth/business.manage`。

GBP scope 本身允许管理能力，但 v2.2 代码只实现读取方法，不实现修改、回复、发布和更新接口。

### 8.2 增量授权流程

1. 用户在获得客户权限后点击 `Connect Client Data`；
2. 先解释连接 GSC 的价值并请求 GSC scope；
3. 选择 GSC Property；
4. 再解释 GA4 并请求 GA4 scope；
5. 选择 GA4 Property；
6. 再解释 GBP 广泛 scope 的原因并请求 GBP scope；
7. 选择 GBP Account/Location；
8. 分别检查 granted scopes，任何未授权源保持独立状态；
9. 用户主动点击 `Sync and verify data`；
10. 同步完成后计算数据健康和 Full Evidence Coverage。

一个 Google 账号可以完成三个来源的连接，但不能假设三个资源都属于同一账号；产品允许分别连接不同 Google 账号。

### 8.3 GSC 规范化快照

默认90天，对比前90天（有数据时）：

- query；
- page；
- date；
- country；
- device；
- clicks；
- impressions；
- ctr；
- position；
- API 返回限制和隐私过滤说明。

限制：保存用于决策的前1000个 query/page 行和聚合数据，不声称覆盖所有查询。

健康检查：

- Property 与 Case 域名匹配；
- 当前90天有数据；
- 至少一个 query 和 page 行；
- 日期没有不可解释的长时间中断；
- 样本与过滤限制进入 `health_reasons`。

### 8.4 GA4 规范化快照

默认90天，对比前90天：

- landing page；
- sessions；
- users；
- engaged sessions；
- engagement rate；
- key events；
- event names；
- device category；
- country/city，在隐私与阈值允许时；
- API metadata、thresholding/sampling 说明。

健康检查：

- Property 对应 Case 域名；
- 有 landing page 和 session 数据；
- 关键事件存在；
- 关键事件不是明显全部缺失或异常重复；
- 转化不健康时，允许行为分析，但禁止确定性转化结论。

### 8.5 GBP 规范化快照

读取：

- Account/Location identity；
- 名称、地址/服务区域、电话、网站；
- 类别、服务、营业时间和公开资料；
- Performance 搜索关键词；
- impressions；
- calls；
- direction requests；
- website clicks；
- API 能提供的设备和日期维度。

该 connector 只能读取用户已经有权管理并主动绑定到 Case 的 Location，不参与公开商家、潜在客户或竞争对手发现。

健康检查：

- 用户对目标 Location 有权限；
- Location 与 Case 实体匹配；
- profile 状态可用；
- 目标报告期内 Performance 可用；
- 数据不可用和API限制明确写入 coverage。

GBP 原始内容保留不超过30天。v2.2 不在后台定时抓取，必须由用户主动同步。

## 9. v2.2 后端 API 合同

保留 `/api/v1`。新增 `/api/v2`：

### 9.1 `POST /api/v2/preflight`

输入：

- site URL；
- 可选 GBP URL；
- 可选用户提供的服务/地区。

输出：

- 规范化站点身份；
- GBP 候选及匹配置信度；
- 推断的主要服务和地区候选；
- SERP/竞品可用性；
- 可生成模块；
- 明确的数据缺口；
- 预估请求成本和生成时间桶，不向用户暴露供应商成本。

预检不输出核心结论和三项行动。

### 9.2 `POST /api/v2/analyze`

输入：

- `case_id`；
- `report_type`；
- 已确认的 business identity；
- primary service、target market、3—5个查询；
- 已确认或系统选择的竞争对手；
- first-party snapshot envelopes；
- parent report（升级时）；
- generation limits。

不接收 Google refresh token。

输出：现有任务 envelope，使用持久 job ID。

### 9.3 任务接口

- `GET /api/v2/tasks/{job_id}`；
- `GET /api/v2/tasks/{job_id}/stream`；
- `POST /api/v2/tasks/{job_id}/retry`，仅允许可重试失败；
- 终态结果包含 `report_v2_2`、safe diagnostics 和成本计数，不包含供应商密钥或 OAuth token。

## 10. `report_v2_2` 合同

后端新增 `app/report_v22/`，不直接扩展 v2.1 模型。

根结构：

```text
report_v2_2
  identity
  case_context
  report_version
  data_coverage
  market_snapshot
  site_inventory_summary
  competitor_analysis
  first_party_performance
    gsc
    gbp
    ga4
  executive_decision
  eight_layers
  findings
  top_actions[3]
  roadmap_30_60_90
  client_summary
  evidence_index
  version_diff
  limitations
```

### 10.1 Evidence

每条 evidence 至少包含：

- `evidence_id`；
- `snapshot_id`；
- `source_type`；
- `source_locator`；
- 原始值、标准化值；
- URL/Property/Location；
- query、坐标、设备、语言；
- 获取时间和覆盖期；
- confidence；
- health status；
- limitations。

### 10.2 Finding

每条 finding 至少包含：

- `finding_id`；
- 可证伪的问题陈述；
- evidence IDs；
- comparator IDs；
- deterministic rule ID/version；
- fact/inference/estimate 分类；
- severity、scope、confidence；
- affected URLs/queries；
- missing data；
- 可改变该结论的条件。

### 10.3 Top Action

必须恰好三项，每项包含：

- `action_id`；
- sequence 1—3；
- finding IDs；
- why now；
- exact targets；
- ordered implementation steps；
- content/GBP/technical specification；
- required client assets；
- dependencies；
- owner suggestion；
- effort bucket；
- definition of done；
- validation metrics；
- data source；
- review date；
- client-facing explanation。

### 10.4 版本差异

升级报告对上一版本的 Finding 分类：

- Confirmed；
- Reprioritized；
- Refined；
- Replaced；
- New。

每个变化必须引用新证据并解释原因，旧报告不覆盖。

## 11. 采集与决策模块

后端建议新增边界清晰的模块：

```text
app/collectors/site_inventory.py
app/collectors/serp_market.py
app/collectors/competitors.py
app/collectors/pagespeed.py
app/report_v22/models.py
app/report_v22/normalize.py
app/report_v22/validate.py
app/report_v22/coverage.py
app/report_v22/evidence.py
app/report_v22/market_findings.py
app/report_v22/first_party_findings.py
app/report_v22/cross_source_findings.py
app/report_v22/prioritization.py
app/report_v22/actions.py
app/report_v22/version_diff.py
app/report_v22/copy_contract.py
```

### 11.1 站点库存

- sitemap、内部链接、Firecrawl map 组合发现；
- URL canonicalization 和去重；
- 页面类型分类；
- 全部发现页做结构检查；
- 公开模式按页面类型和启发式选择50个深度页面；
- 验证模式优先选择有 GSC 曝光/点击的页面；
- 输出覆盖上限和未检查范围。

### 11.2 市场和竞争对手

- 对3—5个确认查询抓取同一地点和设备上下文；
- 使用出现频率、排名和业务相关性选择竞争对手；
- 用户确认后锁定3个竞争对手；
- 保存 SERP 时间、坐标、设备和 query；
- 竞品网站每个最多深度检查10页；
- 同一报告内的比较必须使用可比上下文。

### 11.3 跨源规则

首批确定性规则至少覆盖：

- GSC 高曝光、低 CTR；
- GSC 查询存在但缺少匹配页面；
- 多页面争夺同一查询；
- GSC 有点击、GA4 参与低；
- GA4 有流量、关键事件弱或缺失；
- GBP 有曝光、用户行动弱；
- GBP 页面身份/服务/地区不一致；
- 客户缺少竞品普遍存在的关键服务/地区资产；
- 八层基础阻塞导致下游内容扩张不应优先；
- 数据测量配置本身阻塞验证。

每条规则必须有 fixtures、阈值解释和边界测试。不得把 GSC、GA4、GBP 当成同一用户级漏斗。

### 11.4 优先级

优先级由以下确定性维度组成：

- severity；
- affected scope；
- search/market opportunity；
- competitor gap；
- evidence confidence；
- dependency/blocking power；
- effort/feasibility；
- measurability。

总分用于排序但不直接向客户展示黑箱数字。报告展示“为什么先做”的构成因素。

## 12. 前端产品与路由计划

新增建议路径：

```text
src/app/cases/page.tsx
src/app/cases/new/page.tsx
src/app/cases/[caseId]/page.tsx
src/app/cases/[caseId]/preflight/page.tsx
src/app/cases/[caseId]/connections/page.tsx
src/app/cases/[caseId]/reports/[reportId]/page.tsx
src/app/cases/[caseId]/reports/[reportId]/compare/page.tsx
src/app/share/[shareToken]/page.tsx
src/app/api/cases/**
src/app/api/google/oauth/**
src/app/api/cases/[caseId]/sources/**
src/app/api/cases/[caseId]/reports/**
src/lib/report-v22/**
src/components/report/v22/**
```

### 12.1 获客流程

1. 选择 `I want to win a new client`；
2. 输入网站；
3. 运行免费预检；
4. 确认商家、服务、地区和竞品；
5. 展示数据覆盖，不泄露完整结论；
6. 支付 $19；
7. 创建 Case 和 prospect report job；
8. 显示真实进度；
9. 展示顾问版；
10. 生成客户版、PDF、分享链接；
11. 引导 `Connect Client Data`。

### 12.2 连接流程

- 一个统一 Connection Center；
- GSC、GA4、GBP 分别显示 Not connected/Connected/Available/Healthy/Verified；
- 每次请求 scope 前解释用途；
- 资源选择后显示身份匹配证据；
- 用户主动同步；
- 不健康数据源显示具体修复方式；
- 三个源完成后允许生成 Verified Client Action Plan。

### 12.3 报告阅读

顾问版：完整事实、限制、证据、实施细节、版本差异。

客户版：核心问题、竞争差距、三项行动、30/60/90计划和客户配合事项。

两种模式共享同一规范化 view model，Web、PDF、邮件不得各自重新解释数据。

## 13. 分阶段实施任务

以下按依赖顺序执行。每一阶段必须先通过自动化测试和验收，再进入依赖阶段；允许无依赖工作流并行。

### M0：外部审批与合同冻结

#### V22-001 GBP API 项目审批

- 创建/确认生产 Google Cloud Project；
- 使用企业域名邮箱申请 GBP API access；
- 启用 Business Profile Performance、Business Information、Account Management 等所需 API；
- 记录配额、审批状态和生产账号测试方案；
- 准备 Google 要求的 live demo account。

验收：生产项目 quota 非0，授权账号可列出真实测试 Location 并读取 Performance。

#### V22-002 OAuth 和政策准备

- 配置 OAuth consent screen；
- 准备隐私政策、数据使用说明、断开/删除说明；
- 提交敏感 scope 验证；
- 定义令牌事件审计和泄漏响应；
- 法务/负责人确认 GBP 30天内容保留和第三方政策。

验收：OAuth 生产 redirect、domain verification、scope justification 和删除流程完成。

#### V22-003 v2.2 合同冻结

- 冻结 DB migration 草案；
- 冻结 `/api/v2` request/response；
- 冻结 `report_v2_2` JSON Schema/Pydantic/TypeScript 对应关系；
- 建立一份 prospect 和一份 full verified fixture。

验收：前后端对同一 fixture 校验通过。

### M1：客户项目、报告版本和持久任务

#### V22-010 Supabase migration

前端仓库新增 migration：

- `client_cases`；
- `google_connections`；
- `case_source_bindings`；
- `data_snapshots`；
- `analysis_jobs`；
- `reports` v2.2 扩展；
- 索引、唯一约束、RLS 和 service_role 权限；
- GBP raw content TTL 辅助字段。

测试：SQL lint/本地 Supabase migration、唯一性、外键、删除和旧报告兼容。

#### V22-011 Case API

实现创建、读取、更新确认信息和归档，不允许跨用户访问。

测试：未授权、跨用户、重复 Location、正常 CRUD。

#### V22-012 持久任务

- 后端引入 Redis/ARQ；
- Web API 只入队；
- Worker 执行；
- Redis 状态同步到 `analysis_jobs`；
- SSE 从持久状态恢复；
- 服务重启后任务可重试或进入明确终态；
- 幂等键避免重复扣费/重复报告。

测试：重启恢复、重复提交、worker失败、SSE重连、终态幂等。

验收：处理中重启 Web 服务不会永久丢失付费任务。

### M2：公开数据预检与获客采集

#### V22-020 预检 API

- 域名规范化和安全校验；
- GBP候选；
- 服务/地区候选；
- 数据可用性；
- 不输出付费结论。

#### V22-021 全站库存

- 最多500 URL；
- URL去重和分类；
- 最多50重点页；
- 输出覆盖统计；
- 验证模式支持 GSC 页面优先。

#### V22-022 SERP市场采集

- 3—5查询；
- 单目标点；
- Maps、Local Pack、organic；
- query/坐标/设备/时间证据；
- 每报告 provider call budget；
- SerpAPI failover 延用现有机制。

#### V22-023 竞品选择和采集

- 系统候选；
- 用户确认；
- 3个竞品；
- 每个最多10页；
- 竞品公开 GBP 和评论样本。

测试：无sitemap、大站、重复URL、同名商家、模糊竞品、SerpAPI空结果、限流和预算截断。

验收：真实测试客户在无 Google 授权时得到可追溯站点、市场和竞品快照。

### M3：v2.2 证据、规则和行动合同

#### V22-030 v2.2 模型和校验器

- Pydantic模型；
- JSON Schema；
- 严格 `extra=forbid`；
- TypeScript 类型；
- Python/TypeScript共享fixtures。

#### V22-031 证据索引

- 扩展v2.1 evidence ledger；
- 支持 snapshot、query、location、competitor、GSC、GBP、GA4；
- stable evidence ID；
- source health 和 limitation。

#### V22-032 公开模式 Findings

- 站点结构；
- GBP公开对齐；
- 市场排名；
- 竞品差距；
- 八层全站/页面集群roll-up。

#### V22-033 三项行动生成

- 确定性选择 exactly 3；
- 依赖排序；
- implementation requirements；
- definition of done；
- metrics；
- 客户素材清单。

#### V22-034 Dify copy contract

- Dify只接收finding/action skeleton；
- 输出只能引用既有ID；
- 后端拒绝无证据事实和数字变化；
- copy失败允许有限重试，不得用虚构内容兜底。

测试：相同快照重复生成、无证据文案、数字篡改、动作不足/重复、八层顺序、低置信度优先级。

验收：同一 fixture 的事实、核心问题和行动顺序完全确定。

### M4：获客产品、支付和报告

#### V22-040 新入口和预检UI

- 两种工作目标；
- URL输入；
- 商家/服务/地区/竞品确认；
- 数据覆盖预览；
- 无授权付款。

#### V22-041 Case级支付

- Dodo checkout metadata 写入 `case_id`；
- 支付成功只解锁该 Case 的首次 prospect report；
- webhook 幂等；
- 技术失败自动返还生成权益；
- 暂时兼容旧 credits，但 v2.2 UI 不以 credits 为核心。

#### V22-042 v2.2报告UI

- Advisor/Client模式；
- 核心问题；
-市场和竞品；
- 三项行动；
- 30/60/90；
- 八层；
- evidence drawer；
- limitations。

#### V22-043 PDF和分享

- Web和PDF共享view model；
- share token可撤销、不可猜测；
-客户版不泄漏内部诊断；
- Agency Branding沿用现有能力。

测试：支付重放、webhook重复、分享权限、PDF一致性、无GBP/模糊竞品/大站覆盖提示。

验收：零 Google 授权完成真实 $19 购买并生成可分享报告。

### M5：Google连接基础设施

#### V22-050 OAuth加密与增量授权

- OAuth state/PKCE/CSRF保护；
- scope增量授权；
- granted scope检查；
- refresh token加密；
- token刷新；
- Disconnect/revoke/delete；
- secret-safe日志。

#### V22-051 资源选择和Case绑定

- GSC sites；
- GA4 account/property；
- GBP account/location；
- 支持不同Google账号；
- 每个资源显示匹配线索。

#### V22-052 身份匹配

- GSC domain/url-prefix；
- GA4 web stream/domain；
- GBP网站、名称、地址/服务区；
- 自动匹配只有高置信度才可确认；
- 中低置信度必须用户确认；
- 保存确认者和时间。

测试：OAuth拒绝、部分scope、无refresh token、过期、撤销、多个账号、错误Property、跨用户Case。

验收：三个数据源均能连接、选择、绑定、断开，令牌不出现在浏览器或日志。

### M6：GSC、GA4、GBP同步和健康检查

#### V22-060 GSC connector

- 90天和前90天；
- query/page/date/device/country；
- top rows和限制说明；
- immutable snapshot；
- health evaluator。

#### V22-061 GA4 connector

- Admin资源发现；
- Data API runReport；
- landing page/engagement/key events；
- thresholding/sampling metadata；
- health evaluator。

#### V22-062 GBP connector

- 默认公开路径：SerpAPI Google Maps place details、评论、图片与帖子的有界采集；
- 用户确认 GBP/Google Maps 身份，找不到时要求提供链接；
- 公开 GBP 证据支持 Verified Core，但不得冒充官方 Performance；
- 可选官方路径：account/location、Business Information、Performance、read-only code path、
  30 天原始 Content TTL、user-triggered sync 和 health evaluator；
- 官方开关默认关闭，通过 Google GBP API 准入后才启用。

#### V22-063 Connection Center

- 三源状态；
- Sync按钮；
- 失败修复说明；
- Full Evidence Coverage gate；
- 数据同步进度和重试。

完成于 2026-09-07：已交付 `connection_center_v1` 私有只读聚合接口、Case 所有权与
一致性重读边界、公开 GBP / GSC / GA4 三源确定性门禁、可选官方 GBP 折叠区、唯一
下一步动作和统一响应式页面。Verified Core 要求三项必需证据全部 healthy + matched；
Full Evidence 额外要求未过期且内容可用的官方 GBP Performance。V22-063 不创建验证
任务，生成按钮继续锁定到 M7 的双端开关与执行链路完成。无数据库迁移，正式环境
Google 连接与同步开关继续关闭。详见
`2026-09-07-searchtrust-v2-2-connection-center-completion.md`。

测试使用fake providers和recorded sanitized fixtures；GBP没有沙箱，不允许CI调用真实商家。

验收：公开 GBP 快照 + GSC/GA4 健康快照可继续生成 Verified Core；错误绑定和
不健康数据被准确阻止或降级。官方 GBP Performance 未连接时 Full Evidence 必须为 false。

### M7：验证执行计划和版本升级

#### V22-070 第一方 Findings

- GSC机会；
- GA4行为/转化；
- GBP Performance；
- 测量配置问题。

完成于 2026-09-08：已交付 `v22_first_party_findings_v1` 确定性单来源规则引擎、三态规则
评估、严格 Evidence/Trace 引用、可信快照解析客户端与可恢复 checkpoint。GSC/GA4 必需，
官方 GBP Performance 为可选增强；缺失或不合格来源不会伪造业务事实。正式数据库已部署
仅 `service_role` 可调用的 Case/parent/binding/current-snapshot 解析 RPC，Railway API 与正式
Worker 已运行相同最新提交。GBP 精确 Performance 和关键词只在 30 天 Content 保留期内存
中计算，持久结果仅保存不可逆档位。Verified Generation 与 Google 同步开关继续关闭。
详见 `2026-09-08-searchtrust-v2-2-first-party-findings-completion.md`。

#### V22-071 跨源 Findings

- 只做聚合和页面/日期级合理关联；
- 禁止用户级因果归因；
- 每条结论列出数据源和限制。

完成于 2026-09-08：已交付 `v22_cross_source_findings_v1` 内部确定性规则阶段。
GSC↔GA4 支持保守的同域页面、聚合 90 天对比和至少 8 个完整 ISO 周相关；
可选官方 GBP 支持与 GSC 曝光、GA4 sessions 的聚合配对。双源同时健康、
时间窗可比且两侧都达到固定样本与 20% 门槛才能生成业务 Finding；反向只生成
测量一致性 Finding，不宣称因果。页面匹配不使用标题、重定向、canonical 或模糊匹配，
无效/不明确身份为 `not_checked`。GBP 精确数值仅在内存中计算，持久结果仅保存档位。
阶段已支持摘要绑定、引用校验、独立输出上限和可恢复 checkpoint；未新增数据库、
前端路由或实时 provider 调用，Verified Generation 仍关闭。详见
`2026-09-08-searchtrust-v2-2-cross-source-findings-completion.md`。

#### V22-072 重新优先级

- 新证据进入排序；
- 核心问题和三项行动重算；
- 不健康源不贡献verified confidence。

完成于 2026-09-08：已交付 `verified_reprioritization_v1` 后端内部确定性阶段。
阶段会重算并逐字节核验公开 Findings、公开行动、V22-070 和 V22-071，恢复全部
公开行动候选后再进行严格 URL、查询和聚合目标映射。跨源支持高于单源支持，可靠
增长仅降低同目标一个等级且不覆盖 HTTP、noindex 或身份硬事实；普通跨源方向冲突
形成测量一致性候选，只有阻断可靠判断的 GSC/GA4 问题才强制测量修复排第一。
输出始终为三项行动，重新应用技术依赖和 30/60/90 日期，核心问题仍绑定最高业务
行动的原公开 Finding。结果支持完整关系/未使用 Finding 审计、限定引用、资源上限和
仅结果 checkpoint；未新增数据库、前端、公开 API 或 provider 调用，Verified Generation
仍关闭。详见 `2026-09-08-searchtrust-v2-2-verified-reprioritization-completion.md`。

#### V22-073 版本差异

- parent report；
- Confirmed/Reprioritized/Refined/Replaced/New；
- 旧结论、新证据、新结论、原因；
- 旧报告不可变。

完成于 2026-09-09：已交付 `version_diff_result_v1` 后端内部确定性阶段。阶段校验完整
parent report checksum、Prospect/Case/report 绑定，并把父 Findings、Evidence 与公开行动
事实逐项绑定到 V22-072 输入；随后重算 V22-072 并逐字节比较，重新签名的父报告或上游
篡改均不能进入差异。每个父 Finding 使用完整规范化内容 SHA-256 指纹。

差异只展示真实变化：严格目标支持且行动位置不变为 Confirmed，可靠增长降低紧迫度为
Refined，行动入选状态或前三顺序变化为 Reprioritized；没有变化的旧 Finding 仅进入内部
unchanged 审计。新 Finding 已用于解释旧变化后不会重复显示为 New；未消费的新业务、
测量、冲突或审计 Finding 才生成 New。当前没有明确反证规则，因此合同保留 Replaced，
规则 v1 不生成它。阶段含固定理由目录、最小决策证据、完整引用/分区审计、资源上限和
仅结果 checkpoint；无数据库、前端、公开 API 或 provider 调用，Verified Generation 仍
不可达。详见 `2026-09-09-searchtrust-v2-2-version-diff-completion.md`。

#### V22-074 执行基线和路线图

- Action对应GSC/GBP/GA4指标；
- baseline、review date、success condition；
- 30/60/90依赖排序；
- 数据缺失时第一行动可为测量修复。

测试：全部健康、GSC无数据、GA4无事件、GBP无Performance、结论确认、结论推翻、低置信度、相同输入幂等。

验收：真实全覆盖Case从获客报告升级，用户能解释每个结论变化和每项行动的验证方式。

完成于 2026-09-09：已交付 `execution_plan_result_v1` 后端内部确定性阶段。
阶段会在任何 checkpoint 查询前校验 V22-072/V22-073 输入和结果 checksum，
重算两个上游阶段并逐字节比较。结果固定为三项行动和 30/60/90 三阶段，
每项一个 primary、最多一个必要 guardrail；公开结构行动以原 Finding 规则不再
触发为成功条件，仅在严格目标关系存在时加入可追溯 GSC/GA4 样本保护或合规
GBP 档位保护。

GSC/GA4 测量修复在需要时强制为第一行动，后两项在通过前不得正式验收；
普通跨源方向冲突使用“冲突不再触发”指标，不宣称任一来源错误。最终组装
`verified_execution` ReportV22，保留经验证的公开事实、合并当前 Findings/Evidence，
原样附加 V22-073 变化差异，并保证父报告字节不变。官方 GBP 缺失不阻断
Verified Core；只有 GSC、GA4、官方 GBP 均健康且 matched 时才标记 Full Evidence。
官方 GBP 精确 Performance、keywords 和 raw payload 不进入结果或 checkpoint。新增
仅结果 checkpoint、安全错误和资源上限；无数据库、前端、公开 API、provider 或开关变更，
Google 同步与 Verified Generation 仍保持关闭。详见
`2026-09-09-searchtrust-v2-2-execution-plan-completion.md`。

### M8：可靠性、安全、成本和可观测性

#### V22-080 任务可靠性

- job幂等；
- stage retry policy；
- provider circuit breaker；
- stalled job recovery；
-前端断线重连；
-失败权益返还。

完成于 2026-09-09：已交付 Redis 共享 lease、generation fencing、30 秒心跳、
180 秒失联接管和不可延长的 20 分钟任务截止时间。自动外部重试最多 3 次，
provider + operation 共享熔断器按 60/120/300 秒冷却；SerpAPI 每个密钥使用
独立熔断状态，鉴权、配额和限流错误立即隔离，连续 3 次传输/服务错误隔离。

新增每个逻辑生成任务唯一的 `analysis_attempt_charges` 和不可变
`audit_credit_ledger`。首次任务消耗 Case 付费权益；技术失败后该权益永久关闭为
`compensated`，账户只返还 1 个通用 credit。重复/乱序回调不会重复返还；
再次生成建立新 job ID 和新幂等键，先消耗 1 credit，若再次失败则再返还 1。
结果持久化和回调都校验 generation。

前端增加按 Case 查找服务端最新任务，优先使用认证 SSE，失败时以最多每 10 秒
一次的轮询恢复，并根据 revision 忽略过时状态。正式环境已先应用数据库迁移，
再发布 Railway API/Worker 和 Vercel 前端；生产失败、重复回调、单次返还及临时数据
清理演练通过。详见 `2026-09-09-searchtrust-v2-2-task-reliability-completion.md`。

#### V22-081 安全

- OAuth威胁模型；
- token加密和rotation演练；
- SSRF回归；
- share token权限；
- PII/secret日志扫描；
-数据删除和账户断开测试。

完成于 2026-09-10：已交付 OAuth 威胁矩阵、双密钥可恢复轮换、统一 SSRF
验证与固定 IP 传输、fragment-only 匿名分享、统一安全日志和浏览器产物扫描，
以及 Clerk 删除栅栏、Google 凭据撤销、原子级联删除、幂等回执和晚到创建阻断。

正式环境已先应用 Supabase 迁移，再发布 Vercel 与 Railway。前端 587 项、后端
1,592 项测试通过，类型、合同、生产构建、产物和部署日志检查通过。Clerk 正式
Webhook 已同时订阅 `user.created` 和 `user.deleted`；删除明确标记的合成用户后，
Svix 显示签名事件投递成功，本地用户、Case、Google 连接、报告和分享全部清除，
删除回执验证后也已精确清理。Google OAuth、GSC、GA4、官方 GBP 同步及 Verified
Generation 开关仍关闭。详见
`2026-09-10-searchtrust-v2-2-security-hardening-completion.md`。

#### V22-082 成本控制

每个job记录：

- Firecrawl/Jina请求；
- SerpAPI请求；
- PageSpeed请求；
- GSC/GA4/GBP请求；
- Dify tokens/调用；
- 总耗时和重试。

已批准调整：不设置总美元预算，不因节省成本而缩减报告。每个逻辑任务使用同一个
可恢复成本账本，对实际供应商尝试设置固定硬上限，失败、结果未知和自动重试均保守计数；
检查点复用不重复增加供应商成本。缺失单价显式记为未知，不当作零成本。核心合同因运行硬上限
无法完成时任务失败，并沿用 V22-080 一次性 credit 返还机制。成本数据仅存在内部，不进入
用户任务状态、报告、分享页或 PDF；报告仅保留原有的真实数据覆盖说明。

完成于 2026-09-10：已为报告生成、竞品发现和 GSC/GA4/GBP 同步接入 Redis 共享成本账本，
覆盖 SerpAPI、Firecrawl、Jina、PageSpeed、Dify 和 Google 数据请求。已按数据库、Railway
API/Worker、Vercel 顺序发布到正式环境；完整测试、权限验证、成本摘要幂等演练、
一次性 credit 返还与合成数据清理均通过。Google 同步与 Verified Generation 开关未改变。
详见 `2026-09-10-searchtrust-v2-2-cost-control-completion.md`。

#### V22-083 产品埋点

范围调整于 2026-09-10：当前阶段不建设自研精细埋点与独立事件数据仓。继续使用已接入的
PostHog 页面访问和现有粗粒度事件，仅用于估算从访问、登录、填写、结账到报告生成的大致转化率。

支付成功、credit 消耗/返还和退款仍以正式数据库不可变业务记录为准，不依赖 PostHog 作为
财务真实来源。PostHog 事件不得包含 OAuth token、完整原始搜索词、分享凭据或客户敏感数据。
本项按“现有 PostHog 足够，无新开发”关闭。

### M9：测试、迁移和发布

#### V22-090 前端测试基础设施

- Vitest + Testing Library：view model、health、coverage、diff；
- Playwright：获客支付stub、报告、OAuth fake、升级和分享；
- `npm run typecheck`、`npm run test`、`npm run build`进入CI。

完成于 2026-09-10：已建立 Vitest、Testing Library、Playwright 三层前端测试体系，
覆盖 6 条本地确定性旅程、11 个浏览器用例和关键组件交互；浏览器请求守卫会阻断
Supabase、Railway、Dodo、Google、PostHog 及任意外网请求。失败截图、日志和 trace
在上传前扫描，包含授权头、OAuth 参数、分享凭据或配置密钥的文件不会进入上传目录。
GitHub Actions 已改为 PR/main 双入口门禁，Vercel 正式部署必须等待代码质量和浏览器
两组任务全部成功。正式运行 `34481203591` 已验证两组前置任务和部署任务均通过，
部署 `dpl_A3r6t4vvUNynS2WaCN4NPD3Mo9E3` 已接管 `trysearchtrust.com` 并返回 HTTP 200。
Railway 自动发布仍留给 V22-091，Google 同步与 Verified
Generation 正式开关未改变。详见
`2026-09-10-searchtrust-v2-2-frontend-test-infrastructure-completion.md`。

#### V22-091 后端测试（已完成）

- pytest unit；
- provider contract tests；
- golden report fixtures；
- property-based/idempotency tests；
- queue/restart integration；
- 前后端 V2.1 产品路径完全退役，不保留报告兼容；
- `/api/v1/health` 作为 Railway 基础设施探针独立保留；
- GitHub Actions 全部通过后 Railway Web 与 Worker 才能发布。

完成于 2026-09-11：已建立 1,477 项后端确定性检查，包括 provider contract、
完整报告 golden、property/idempotency 与真实 Redis 7.4 重启恢复；意外外网请求默认
阻断，测试不读取线上数据或密钥。前后端可执行 V2.1 产品路径已全部删除，仅保留独立
健康探针。最终 GitHub Actions 运行 `34593665540` 成功后，Railway Web 部署
`0092c603-e606-4120-bb3b-41ed91fb1845` 与 Worker 部署
`40822911-92a8-46d1-bd75-dd70515dbb29` 才从等待状态继续，并将同一提交
`c7a649109b03d9fb10a7eb17c1425a68466db4c7` 发布为 `SUCCESS`；生产健康检查返回
HTTP 200，初始日志无错误级事件。V22-092 数据迁移和 V22-093 灰度发布边界未改变。
详见 `2026-09-11-searchtrust-v2-2-backend-test-infrastructure-completion.md`。

#### V22-092 数据迁移

- staging应用migration；
- 历史报告读取回归；
- 新表RLS和service role检查；
- 不强制回填旧报告Case；
- 回滚脚本只回滚新入口，不删除新数据。

#### V22-093 灰度发布

1. internal accounts；
2. 3名受控Local SEO顾问；
3. 10—15名付费验证用户；
4. 观察稳定性和商业指标；
5. 决定扩大流量或回滚入口。

## 14. 测试矩阵

### 14.1 核心场景

1. 无授权、唯一GBP、正常站点；
2. 无授权、多个同名GBP；
3. 无GBP；
4. Service Area Business；
5. 大于500 URL；
6. SerpAPI部分失败；
7. 三个Google源全部健康；
8. GSC Property错误；
9. GSC无搜索数据；
10. GA4有流量但无Key Events；
11. GA4 Property错误；
12. GBP Location错误；
13. GBP Performance为空；
14. 用户只批准部分scope；
15. token过期/撤销；
16. 同一Case重新生成；
17. 新数据确认旧结论；
18. 新数据推翻旧结论；
19.任务期间服务重启；
20.付款webhook重复；
21.分享链接撤销；
22.GBP快照30天到期和清理；
23.旧v2.1报告继续打开/PDF；
24.LLM输出无证据数字被拒绝。

### 14.2 非功能门槛

- P95 prospect report 完成时间目标：15分钟以内；
- P95 verified regeneration：10分钟以内，不含用户授权时间；
- 付费job永久丢失：0；
- 报告关键数字无 evidence ID：0；
- top actions 数量不等于3：0；
- token/secret日志泄漏：0；
- 跨用户Case访问：0；
- v2.1历史报告回归失败：0；
- GBP受限原始Content超过30天：0。

## 15. 商业验证计划

测试对象：10—15名真实 Local SEO 顾问，提交真实潜在客户或已管理客户。

禁止：免费赠送完整获客报告后询问“是否愿意付费”。必须实际进入 $19 checkout。

最低验证目标：

- 10名合格测试者中至少3名真实付款；
- 至少70%认为核心结论可信；
- 至少70%认为客户版可以直接展示；
- 至少60%认为三项行动无需重新研究；
- 至少50%实际分享或导出；
- 购买者中至少30%进入Google数据连接；
- 成功拿到权限者中至少60%完成三源绑定或明确记录未完成原因；
- 技术原因退款率低于10%；
- 至少2名用户愿意继续为复查/监控付费。

商业北极星：

> 真实付款并且至少查看、复制、分享或执行一项行动的客户项目数量。

## 16. 排期与人员假设

以2名前/全栈工程师、1名后端工程师、产品/QA兼职参与为基准：

| 阶段 | 预计工程时间 | 依赖 |
|---|---:|---|
| M0 外部审批与合同 | 3—5工程日，审批时间另计 | 无，立即开始 |
| M1 Case/DB/持久任务 | 6—8工程日 | 合同草案 |
| M2 公开采集 | 8—12工程日 | M1部分接口 |
| M3 证据/决策/行动 | 8—12工程日 | M2 fixtures |
| M4 获客产品 | 8—10工程日 | M1、M2、M3 |
| M5 OAuth基础 | 6—9工程日 | M0、M1 |
| M6 三源connector | 10—15工程日 | M5、GBP审批 |
| M7 验证报告/版本差异 | 8—12工程日 | M3、M6 |
| M8 可靠性/安全/成本 | 6—10工程日 | 全流程 |
| M9 测试/发布 | 6—10工程日 | M4、M7、M8 |

并行执行后，目标日历周期为8—10周；GBP审批和OAuth验证不计入工程日，但位于关键路径。单人开发预计需要16—22周，不应压缩测试和安全阶段来换取日期。

### 建议并行轨道

- 轨道A：Case、前端流程、支付、报告UI；
- 轨道B：站点、SERP、竞品、v2.2证据和决策；
- 轨道C：OAuth、GSC、GA4、GBP、数据健康；
- 汇合：Verified report、版本差异、E2E、灰度。

## 17. 发布风险与应对

| 风险 | 级别 | 应对 |
|---|---|---|
| GBP API审批延迟/拒绝 | 阻塞 | 第一天申请；准备企业域名、合法用途、demo；未获批不得宣称Full Evidence |
| OAuth敏感scope验证延迟 | 阻塞 | M0完成材料；增量授权；最小scope；上线前生产验证 |
| GBP政策与长期证据冲突 | 高 | 原始Content TTL≤30天；到期提示；法务确认保留字段 |
| 全站抓取过慢/过贵 | 高 | 500/50/10硬上限、预算计数、覆盖降级 |
|错误商家/竞品 | 高 | 置信度门槛+用户确认+版本记录 |
| GA4埋点不健康 | 常见 | 健康检查；禁止转化结论；生成测量修复行动 |
| 跨源错误因果 | 高 | 只做允许维度关联；所有结论列限制；规则fixtures |
| Dify幻觉 | 高 | skeleton+ID合同；后端事实校验；无事实fallback |
| 任务重启丢失 | 高 | Redis/ARQ+analysis_jobs+幂等和退款 |
| v2.1回归 | 中 | `/v1`隔离、旧合同fixtures、双渲染回归 |
| $19成本失控 | 中 | provider budget、成本埋点、限制深度，不提前扩Geo-grid |
| 报告过长 | 中 | 核心结论/三行动优先，证据和八层折叠，Advisor/Client双视图 |

## 18. 发布前 Definition of Done

### 产品

- 两种入口真实可用；
- 无授权能购买并生成获客报告；
- GSC、GA4 健康且公开 GBP 已确认后能升级 Verified Core 执行报告；
- 三个官方数据源都健康时可升级为 Full Evidence；
- 用户能看到版本差异；
- 客户版可分享/PDF；
- 三项行动可直接实施和验收。

### 数据

- 每个关键数字有来源和时间；
- 每条核心Finding有证据；
- 每个Action反向链接Finding；
- Full Evidence只在GSC/GBP/GA4健康时显示；
- 不健康数据不会贡献verified结论；
- GBP TTL清理已自动化。

### 技术

- 前后端CI通过；
- v1回归通过；
- 任务重启恢复通过；
- payment/webhook幂等通过；
- OAuth安全评审通过；
- RLS和跨用户权限通过；
-生产环境变量和rollback runbook完成；
-无明文token或secret日志。

### 商业

- PostHog事件可形成从预检到付款、报告、分享、连接、升级的漏斗；
- 技术失败自动恢复或返还权益；
- 客观 Data-backed Report Guarantee 文案上线；
- 10—15名付费验证用户名单和访谈脚本准备完成。

## 19. 实施开始顺序

立即执行：

1. V22-001 GBP API审批；
2. V22-002 OAuth验证与政策材料；
3. V22-003 report/API/DB合同冻结；
4. V22-010 Supabase migration；
5. V22-012 Redis/ARQ持久任务；
6. V22-020预检与V22-021站点库存；
7. V22-050 OAuth基础；
8. 公开采集轨道和三源connector轨道并行；
9. 在稳定fixtures上实现v2.2决策和报告；
10. 合并为获客→连接→验证→分享的端到端流程。

不要先做完整报告UI再补数据合同，也不要等待获客版完成后才申请GBP权限。两个做法都会把关键风险推迟到版本末期。

## 20. 官方技术与政策依据

- Search Console OAuth：https://developers.google.com/webmaster-tools/v1/how-tos/authorizing
- Search Analytics API：https://developers.google.com/webmaster-tools/v1/searchanalytics/query
- GA4 Data API：https://developers.google.com/analytics/devguides/reporting/data/v1
- GA4 Data API reporting：https://developers.google.com/analytics/devguides/reporting/data/v1/basics
- Google OAuth增量授权：https://developers.google.com/identity/protocols/oauth2/resources/granular-permissions
- GBP基础设置与审批：https://developers.google.com/my-business/content/basic-setup
- GBP OAuth：https://developers.google.com/my-business/content/implement-oauth
- GBP APIs Overview：https://developers.google.com/my-business/ref_overview
- GBP政策与30天Content限制：https://developers.google.com/my-business/content/policies

## 21. 最终交付定义

v2.2 完成时，Local SEO 顾问应该能够：

1. 在没有客户权限时提交一个真实网站并支付 $19；
2. 获得包含全站、GBP公开表现、搜索市场、三个竞品、八层信任和三项行动的可追溯获客报告；
3. 将客户版直接分享给潜在客户；
4. 拿下客户后，在同一 Case 连接 GSC、GA4、GBP；
5. 识别错误、无数据或不健康的连接；
6. 用第一方数据重新生成验证执行计划；
7. 看清哪些结论被确认、调整或替换；
8. 按三项行动和30/60/90计划实施；
9. 用明确的数据基线向客户解释后续复查方式。

只有完整闭环通过真实付费客户验收，SearchTrust v2.2 才算完成。
