# SearchTrust V2.2 Verified Generation 产品化设计

日期：2026-09-13  
里程碑：V22-093 正式发布前置工作  
状态：已完成分段评审，等待书面规格复核

## 1. 决策与目标

V22-070 至 V22-074 已实现第一方 Findings、跨来源 Findings、Verified 重新优先级、
版本差异和最终执行计划的内部确定性阶段，但正式产品链路仍未接通。前端目前固定关闭
Verified Generation，公开分析合同只提交 `prospect`，后端执行器也拒绝非 `prospect`
任务。因此，Verified Generation 不能作为 V22-093 已可发布的能力直接打开。

本设计补齐以下产品化边界：

1. Connection Center 中的生成入口和 credit 状态；
2. GSC、GA4、公开 GBP Evidence 与父级 Prospect 的可信绑定；
3. 独立的 Verified 任务启动、扣费、失败返还和结果持久化；
4. `$19 / 1 credit` 的 Dodo Payments 购买入口；
5. 现有 Worker 队列中的独立 Verified pipeline；
6. 前后端双开关、正式环境验收和故障关闭。

完成本项后，才允许执行直接正式发布设计中“verified generation、evidence changes 和
report-version comparison”对应的开放步骤。

## 2. 已确认产品规则

- 每次 Verified Generation 消耗 1 个账户 credit。
- 技术失败准确返还 1 个 credit；同一失败最多返还一次。
- 失败后仍保留再次生成入口；再次生成属于新尝试并重新消耗 1 个 credit。
- 余额为 0 时提供 `$19 / 1 credit` 的 Dodo checkout。
- 购买成功只把 1 个 credit 加到账户余额，不自动开始生成；用户必须再次明确点击生成。
- GSC 与 GA4 只能通过现有 Google OAuth 连接，且必须同时健康、身份匹配、未过期。
- 不接受 CSV、手工上传或缺少 GSC/GA4 的低置信度替代生成。
- 官方 GBP OAuth 不是当前前置条件；GBP 使用现有 SerpAPI 公共资料 Evidence。
- 官方 GBP 前后端同步开关继续关闭，直到拥有正式账号并单独完成真实验收。
- 不变 Evidence 不显示变化标记；只有真实发生变化的结论才向用户展示差异。
- 不保留 V2.1 兼容逻辑。

## 3. 架构选择

采用“独立 Verified 入口与 pipeline，复用现有可靠队列基础设施”的方案。

不把现有 Prospect 执行器改造成一个承担全部模式的通用执行器，也不新建第二套 Worker
部署。公开 API、数据库启动函数、结果持久化函数和 pipeline 保持模式隔离；Redis 队列、
任务领取、lease、heartbeat、重试、事件结算、checkpoint 和 Worker 进程继续复用。

该边界可以避免 Verified 的第一方数据、父报告和付费规则污染已经稳定的 Prospect 流程，
同时避免重复建设一套任务可靠性系统。

## 4. 端到端数据流

1. 用户在 Connection Center 打开当前 Case。
2. 服务端重新读取 Case、当前父级 Prospect、GSC/GA4 最新合格快照以及公共 GBP Evidence；
   不信任浏览器上传的报告或 snapshot 对象。
3. 服务端计算 Verified Core：父报告存在，GSC 与 GA4 均健康、匹配、未过期，公共 GBP
   Evidence 可用，前后端双开关同时开启。
4. 用户有 credit 时明确确认“Generate Verified Action Plan · uses 1 credit”。
5. 新数据库原子入口校验所有绑定，扣除 1 credit，创建 `verified_report` job、attempt 和
   charge，然后才允许将不可变请求信封放入现有队列。
6. Worker 按 `job_type=verified_report` 路由到独立 Verified pipeline，不重新抓取网站或
   竞品，不重跑 Prospect 市场发现。
7. Pipeline 使用已验证的父报告、第一方快照和公开 Evidence 依次执行 V22-070 至 V22-074。
8. 独立结果 RPC 原子保存新的 `verified_execution` 报告并完成任务。
9. 前端复用现有任务状态、刷新恢复与报告查看机制，成功后打开新报告。

请求进入队列前发生的验证错误不扣 credit。进入任务后的技术失败通过现有事件结算边界
返还 credit，不能把部分结果保存为完整报告。

## 5. 父报告、版本和不可变性

V22-073 的已冻结合同要求 parent 必须是 schema `2.2.0`、无 parent 且使用 initial diff
的 `prospect` 报告。产品化入口遵守该规则，不扩展为 Verified 链式 parent。

- 第一次 Verified 以当前 Case 的原始 Prospect 为 parent。
- 同一 Case 再次生成仍以该原始 Prospect 为 parent。
- 每次成功生成一个新的、不可变的 `verified_execution` 报告和独立版本号。
- 旧 Prospect 与旧 Verified 报告均不被回写。
- `VersionDiff(kind="upgrade")` 只包含相对 Prospect 的真实变化。
- 未发生变化的旧 Finding 只保留内部 unchanged 审计，不进入用户差异列表。
- 每个 `previous_finding` 必须指向同一个 parent Prospect。

该模型保持 V22-073 的确定性与审计语义，也避免多次重新生成形成无限父链。

## 6. 来源资格与绑定

### 6.1 GSC 与 GA4

GSC、GA4 均为 Verified Core，缺一不可。启动时必须验证：

- snapshot 属于同一用户和 Case；
- source type 唯一且分别为 `gsc`、`ga4`；
- connection state 可用；
- health status 为 healthy；
- identity match 为 matched；
- snapshot 未过期；
- OAuth token 的服务端连接仍可使用。

浏览器只能表达“开始生成”的意图，不能选择或替换可信 snapshot ID。数据库入口绑定服务端
解析出的 snapshot ID 与 checksum；Worker 和结果持久化必须使用同一组绑定。

### 6.2 公共 GBP Evidence

当前不要求官方 GBP OAuth。Verified 使用 Prospect/SerpAPI 链路已经保存并绑定的公共 GBP
资料 Evidence。若一侧存在而另一侧不存在，结论为不匹配；若两侧均无 GBP 信息，同样视为
错误，因为 GBP 信息是该产品排名建议的必要依据。

公共 GBP Evidence 缺失或不再能绑定当前 Case 时，启动在扣费前失败并要求用户修复，不能
通过关闭覆盖率提示继续生成。官方 GBP snapshot 即使不存在，也不影响本阶段资格。

## 7. 数据库与安全边界

通过正向 migration 增加 Verified 产品化合同，不修改已应用 migration。

### 7.1 任务输入绑定

`analysis_jobs` 为 Verified job 保存或通过一对一受保护输入记录绑定：

- `case_id`；
- `parent_report_id`；
- GSC snapshot ID 与 checksum；
- GA4 snapshot ID 与 checksum；
- 公共 GBP Evidence/来源 snapshot ID 与 checksum；
- Verified 请求 schema/ruleset 版本；
- 服务端请求摘要。

最终采用增加 nullable job 绑定列还是新增一对一输入表，由实施计划根据现有表约束选择；
无论物理结构如何，数据库必须能在完成任务时独立重验绑定，而不能只依赖 Redis 中的请求。

### 7.2 `start_v22_verified_analysis`

新增 service-role-only 原子入口，负责：

1. 验证调用用户、Case 所有权和 Case 状态；
2. 验证唯一、不可变的 parent Prospect；
3. 解析并验证当前合格的 GSC/GA4 与公共 GBP Evidence；
4. 验证 `users.audit_credits >= 1`；
5. 原子扣减 1 credit；
6. 创建 `verified_report` job、attempt、charge、credit ledger 和输入绑定；
7. 对相同幂等键返回同一个 job，不重复扣费。

身份、来源、父报告、开关或余额问题必须在扣费前返回稳定错误。

### 7.3 `persist_v22_verified_result`

新增 service-role-only 原子结果入口，负责：

- 只接受 `verified_execution`；
- 校验 job 类型、Case、attempt、parent、snapshot 和 checksum；
- 校验最终 `ReportV22` 的 parent、version diff、第一方来源和引用闭合；
- 拒绝过期 lease、错误 job 或跨 Case 结果；
- 幂等插入报告并完成 job；
- 重复提交返回原结果，不创建第二份报告。

是否更新 Case 的展示用 latest-report 指针不得改变“parent Prospect”的解析规则；必须使用
显式 `report_type=prospect` 查询或已绑定的 parent ID，不能把最近一份 Verified 当成下一次
生成的 parent。

### 7.4 权限

- 新函数和任何原始输入绑定表不向 `anon` 或 `authenticated` 开放直接执行/写入。
- 浏览器不能直接改 credit、charge、refund、job binding 或 report parent。
- RLS 保持 owner-scoped 读取；服务角色写入必须显式验证同一 Case。
- 错误与日志不回显 OAuth token、provider payload、完整搜索词、Evidence 原文或密钥。

## 8. Credit、失败返还与重试

Verified 不使用或重置 Case 的首次 Prospect entitlement。它始终从账户
`audit_credits` 消耗 1 credit。

- 启动事务成功：写入一笔 attempt charge 并扣减余额。
- 启动事务失败：不产生 charge，不改变余额。
- job 技术失败：事件结算只在该 charge 尚未 compensated 时返还 1 credit。
- Worker 重复失败事件：返回同一结算状态，不重复返还。
- 用户重试：使用新 idempotency key 创建新 attempt，再扣 1 credit。
- 用户取消、来源资格失效或确定性数据错误如何分类，由实施计划沿用现有 retryable/
  terminal error catalog；任何已扣费且未产出报告的 terminal 技术失败必须最终得到一次补偿。

账本是财务真实来源，PostHog 只用于粗粒度转化估算。

## 9. Dodo `$19 / 1 credit` 购买

在现有订单模型增加显式 `case_verified_credit` purchase kind：

- `case_id` 必填，用于用户返回当前 Connection Center；
- `credits_purchased=1`；
- 使用独立的正式 Dodo product/price 配置，不复用 Case Prospect entitlement 产品；
- checkout 创建、确认与 webhook 继续服务端验证 provider 身份和订单金额；
- fulfillment 原子把 1 credit 加入账户，并记录不可变订单/ledger 关联；
- 同一 Dodo payment/webhook 重放不会重复入账；
- 支付返回页只刷新 credit 状态，不自动提交 Verified job。

到账后的 credit 属于账户余额。当前 Case 是购买上下文，而不是把该 credit 永久锁死到一个
Case；真正生成时仍由 Verified 启动事务对目标 Case 做完整资格验证。

## 10. Connection Center 交互

Connection Center 显示当前账户 credit 余额，并由服务端状态决定主要动作：

1. 父报告或来源未满足：禁用生成，显示具体修复动作；
2. Verified Core 满足且余额大于 0：显示
   `Generate Verified Action Plan · uses 1 credit`；
3. Verified Core 满足但余额为 0：显示 `Buy 1 credit · $19`；
4. 购买成功返回：显示新增余额和生成按钮，不自动开始；
5. 任务运行中：显示现有 durable task 状态，刷新页面后从真实任务状态恢复；
6. 成功：进入新 Verified report；
7. 失败：明确说明已返还 1 credit，并提供再次生成入口。

界面不向用户展示覆盖率百分比或数据覆盖评分。它只说明三项必需来源是否已经满足、当前
阻断项和下一步动作；用户可操作的事实是“能否生成”，而不是一个可能被误解为报告质量或
成本保证的比例。

双击、刷新或网络重试使用客户端 idempotency key 和服务端 job/payment 幂等边界，不靠按钮
临时 disabled 状态保证财务正确性。

## 11. 功能开关

Verified 对外开放要求以下两个正式环境开关同时为 true：

- 前端：`GOOGLE_VERIFIED_ANALYSIS_ENABLED`；
- 后端/Worker：`V22_VERIFIED_ANALYSIS_ENABLED`。

任一开关关闭时，前端不允许开始任务，后端也必须独立拒绝。GSC/GA4 的连接和同步开关仍
分别受现有 Google flags 控制。官方 GBP flags 继续关闭；SerpAPI 公共 GBP 不依赖该开关。

部署数据库和代码时 Verified 双开关保持关闭。只有正式环境验收完成后才同时开启。严重
故障时先关闭双开关，停止新任务；已完成报告、credit 账本、支付 webhook 和已开始任务的
安全结算继续保留。

## 12. 测试策略

### 12.1 数据库与支付

- 成功启动准确扣 1 credit；
- 余额不足不创建 job、不扣费；
- 重复启动幂等；
- 技术失败准确返还一次；
- 重试创建新 attempt 并重新扣费；
- `$19 / 1 credit` checkout 与订单金额/产品绑定正确；
- 支付成功只充值，不自动生成；
- 重复 webhook、confirm 和页面刷新不重复充值；
- 跨用户 Case、report、snapshot 和 order 访问被拒绝。

### 12.2 Pipeline 与报告

- Verified job 走独立 pipeline 并执行 V22-070 至 V22-074；
- 不重新抓取站点或竞品；
- GSC/GA4 缺失、异常、不匹配或过期时拒绝；
- 公共 GBP Evidence 缺失时拒绝；
- 结果始终绑定正确 Case 和原始 Prospect；
- 再次生成不会把旧 Verified 当成 parent；
- 不变 Findings 不出现在用户差异列表；
- checksum、引用、lease 或 parent 篡改被确定性拒绝；
- duplicate delivery 和 Worker 重启不产生重复报告或重复结算。

### 12.3 前端与浏览器旅程

- 未就绪、可生成、零余额、checkout 返回、运行、成功、失败和 refunded 状态；
- 购买后必须再次点击生成；
- 任务刷新恢复和报告跳转；
- 前后端 flag 不一致时 fail closed；
- 浏览器测试继续阻断真实 Dodo、Google、Supabase、Railway 和任意外网调用。

## 13. 正式发布顺序

1. 完成实现计划和代码评审；
2. 运行全部前端、后端、数据库、支付与浏览器门禁；
3. 备份并记录正式数据基线；
4. 先应用正向数据库 migration；
5. 发布 Railway Web 与 Worker；
6. 发布 Vercel 前端；
7. 保持 Verified 双开关关闭，验证配置、schema、权限、health 和任务路由；
8. 使用真实 Case 完成一次真实 credit 购买；
9. 验证到账 1 credit 且没有自动生成；
10. 明确点击生成，验证扣费、GSC/GA4/GBP 绑定和 `verified_execution` 报告；
11. 人工触发一次受控失败，验证一次返还和再次生成；
12. 核对 Dodo、订单、ledger、job、attempt、report 和版本绑定；
13. 同时开启前后端 Verified 开关；
14. 再执行 V22-093 直接正式发布验收，不做灰度。

发布失败时关闭 Verified 双开关，不删除报告、不反向修改 ledger、不回滚已应用 migration，
也不恢复 V2.1。

## 14. 验收标准

- Connection Center 不再硬编码关闭 Verified；
- 公开 Verified 请求不能由浏览器伪造父报告或 snapshot；
- GSC、GA4 与公开 GBP 资格规则全部执行；
- 每次尝试准确消耗 1 credit；
- 每个未产出报告的 terminal 技术失败准确补偿一次；
- `$19 / 1 credit` 购买可以在正式 Dodo 完成，且不自动生成；
- V22-070 至 V22-074 通过独立 Verified pipeline 运行；
- 成功结果是不可变 `verified_execution`，parent 始终为原始 Prospect；
- 用户只看到真实变化，不看到 unchanged 标记；
- 重复请求、回调、事件和 Worker delivery 均保持幂等；
- 双开关关闭时 fail closed，开启后完整正式旅程通过；
- 官方 GBP OAuth 仍关闭，SerpAPI 公共 GBP 可用；
- V2.1 路径没有恢复；
- 前后端仓库、正式部署提交、迁移和完成证据最终一致。

## 15. 非目标

- 官方 GBP OAuth 接入或真实账号验收；
- CSV/手工第一方数据上传；
- 缺少 GSC/GA4 时生成降级版 Verified 报告；
- Verified-to-Verified 父链或任意版本分支；
- 订阅、credit bundle、自动续费或购买后自动生成；
- 自研精细埋点或新分析仓库；
- V2.1 兼容、迁移或历史报告回填；
- 灰度用户、邀请制或百分比放量。
