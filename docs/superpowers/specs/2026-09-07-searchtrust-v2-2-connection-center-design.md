# SearchTrust v2.2 — V22-063 Connection Center 设计

日期：2026-09-07

状态：用户已批准对话设计，等待书面规格复核后进入实施计划。

主要仓库：`search-trust` 负责 Connection Center 聚合读取合同、私有 API、页面与组件；
`SearchTrust-RD` 只在共享报告合同或开发文档需要保持一致时修改。本里程碑不新增
数据库表，不修改 Railway 同步 Worker，也不实现 Verified Client Action Plan 生成。

## 1. 目标与范围

V22-063 把现有 Google 账号、资源绑定、身份确认、GSC/GA4 同步和可选官方 GBP
Performance 同步整合为一个 Case 级 Connection Center。页面同时服务首次连接与后续
维护，让用户立即知道：哪些证据已就绪、当前能生成哪一级报告、以及唯一的下一步。

本轮交付：

1. 一个 Case 所有权保护的 Connection Center 聚合读取接口。
2. 公开 GBP、GSC、GA4 三张平级状态卡，以及折叠的官方 GBP Performance 可选区。
3. Verified Core 与 Full Evidence 的确定性覆盖门槛。
4. 连接、资源选择、身份确认、同步、进度、失败修复和重试的统一页面编排。
5. 为后续 M7 预留生成按钮与就绪状态，但不提交当前 Worker 不支持的 verified job。

本轮不实现：

- V22-070～074 的第一方 Findings、跨源 Findings、重新优先级、版本差异或路线图；
- 自动或定时同步；
- 新的 Google provider、OAuth scope、同步任务表或快照格式；
- 官方 GBP API 准入、真实账号验收或打开生产同步开关；
- 将公开 SerpAPI 数据表示为官方 GBP Performance 或 Full Evidence。

## 2. 已批准的产品决策

- Connection Center 同时承担首次设置和长期维护，不拆成向导与监控两个页面。
- 采用三源状态卡布局：公开 GBP、GSC、GA4 平级展示。
- 卡片首先使用任务导向文案；技术状态、资源、覆盖时间和健康原因在详情中展示。
- 页面顶部显示 Verified Core 进度，并始终给出一个按优先级计算的下一步。
- 满足门槛后，页面底部显示 Verified Client Action Plan 已就绪。
- 生成仍必须由用户明确点击，不允许因同步完成而自动生成。
- 本轮保持里程碑边界：M7 未启用时按钮安全禁用；M7 上线后复用同一按钮直接提交。
- 公开 GBP 是 Verified Core 的硬门槛。找不到、存在多个候选或未确认时，要求用户
  提供或确认 Google Maps/GBP 链接，不允许仅凭 GSC 与 GA4 继续。
- 官方 GBP Performance 是可选增强，未连接时不阻止 Verified Core。

## 3. 方案比较与选择

### 方案 A：浏览器聚合现有接口

前端分别请求连接、绑定和同步状态，再在浏览器计算总状态。代码量较少，但多个请求可能
来自不同时间点，容易短暂显示互相矛盾的门槛和动作。

### 方案 B：服务端只读聚合接口（采用）

服务端从现有真实记录计算一个稳定投影，单次返回来源状态、任务、快照、修复动作与覆盖
门槛。现有 mutation API 保持不变。该方案提供一致的权限和时间边界，也可被 M7 直接复用。

### 方案 C：新增持久化状态表

读取快，但会复制连接、绑定、任务和快照状态，需要处理双写和失效，对当前规模不必要。

## 4. 架构与组件边界

新增私有只读接口：

```text
GET /api/v2/cases/{caseId}/connection-center
```

接口使用 Clerk 用户身份，先验证 Case 属于当前用户且状态为 `active`。服务层在一个请求中
并发读取：

- `client_cases` 的业务身份、公开 GBP URL、版本时间和最新报告；
- 当前用户的活动 Google connections；
- Case 的活动 `case_source_bindings`；
- 每个绑定最近一次 `google_sync_jobs`；
- 每个绑定最近一份仍有效的 `data_snapshots`；
- 当前 Case 最新可用的不可变 v2.2 父报告。

不新增缓存或持久化聚合结果。连接、授权、资源发现、绑定、断开、GSC/GA4/官方 GBP
同步与重试继续使用现有 API。任一 mutation 成功后重新获取聚合投影；只有任务处于
`queued` 或 `running` 时页面才短轮询聚合接口。

聚合不是数据库事务快照。Repository 先冻结 Case `updated_at` 与各活动 binding ID，再按
这些 ID 读取任务和快照，最后重新确认 Case 版本与活动 binding ID 未变化。发生并发替换时
只自动重读一次；再次变化则返回固定 `CONNECTION_CENTER_BUSY`，页面保留原状态并允许重试。
Projector 永远只接受明确属于已冻结 binding 的快照，不能把新旧资源拼成一个 ready gate。

服务拆为三个小单元：

1. repository：只读取拥有者范围内的数据库记录，不解释产品状态；
2. projector：无 IO 的确定性状态与 gate 计算；
3. handler：认证、Case ID 约束、feature flag、`no-store` 和安全错误投影。

前端拆为页面容器、Coverage Summary、Source Card、Optional Performance 和 Generation CTA。
现有资源选择与三个同步控件可被组合或抽取，但不得在新页面复制 OAuth、身份或同步逻辑。

## 5. `connection_center_v1` 响应合同

响应只包含页面必需的安全字段：

```text
schema_version: connection_center_v1
case: id, business_name, site_url, updated_at
coverage:
  verified_core_ready
  full_evidence_ready
  verified_generation_enabled
  parent_report_id
  eligible_snapshot_ids: gsc, ga4, optional gbp_performance
  blockers[]
  next_action
sources:
  public_gbp
  gsc
  ga4
optional_sources:
  official_gbp_performance
```

每个来源投影包含：`source_key`、`required_for_verified_core`、`user_status`、`technical_status`、
`summary`、`action`、安全的 binding 摘要、最新任务摘要和最新有效快照摘要。快照摘要最多包含
ID、有效健康状态、身份状态、fetched/expiry/coverage 时间、固定健康原因代码和公开修复文案。

响应禁止包含 access/refresh token、ciphertext、OAuth subject、Supabase 密钥、原始或规范化
provider payload、Google 响应正文、搜索词、指标数值、内部异常、lease、checksum 或清理字段。

## 6. 来源状态模型

用户状态使用固定枚举：

```text
needs_profile
needs_connection
needs_resource
needs_identity_confirmation
ready_to_sync
syncing
healthy
needs_attention
optional_unavailable
```

GSC/GA4 的状态优先级：连接被撤销或 scope 缺失 → 无活动绑定 → 身份 mismatch/待确认 →
活动任务 → 无快照 → 快照过期/不健康 → 健康。最新任务失败但仍有一份有效健康快照时，
卡片保持 `healthy`，同时显示“最近一次刷新失败”警告和重试动作；失败任务不得覆盖旧快照。

公开 GBP 不使用 Google connection。它必须同时满足：

1. 当前 Case 存在规范化的 `business_identity.public_gbp_url`；
2. 最新合格父报告属于同一 Case，且其业务身份与当前 Case 一致；
3. 父报告中的公开 GBP 证据存在、身份匹配、健康且引用同一商家链接。

任一条件不满足时，状态为 `needs_profile` 或 `needs_attention`。修复动作引导用户提供/确认
链接并重新运行公开 GBP 身份检查；不允许直接保存一个未经验证的 URL 来打开 gate。

## 7. Coverage gate

`verified_core_ready=true` 仅当：

- 当前 Case 与父报告身份一致且公开 GBP 满足第 6 节条件；
- GSC 当前活动绑定为 `matched`，并有属于该绑定、未过期且 `healthy` 的快照；
- GA4 当前活动绑定为 `matched`，并有属于该绑定、未过期且 `healthy` 的快照。

`full_evidence_ready=true` 还要求官方 GBP Performance 当前活动绑定为 `matched`，并有属于
该绑定、未过期、Content 可用且 `healthy` 的官方快照。公开 GBP 永远不能替代该条件。

`blockers` 按固定顺序返回：Case/父报告 → 公开 GBP → GSC 连接/资源/身份/同步/健康 →
GA4 连接/资源/身份/同步/健康。页面只把第一项显示为主下一步，其余保留在详情中。

本轮 `verified_generation_enabled=false`。未来 M7 使用前端
`GOOGLE_VERIFIED_ANALYSIS_ENABLED` 与 Railway Worker `V22_VERIFIED_ANALYSIS_ENABLED`
两个独立开关；只打开一侧时安全失败。按钮状态为：

- gate 未满足：禁用并显示首个 blocker；
- gate 满足、M7 未启用：禁用并显示 `Ready for verified analysis`；
- gate 满足且 M7 启用：允许用户明确点击提交，不自动运行。

## 8. 页面与交互

桌面端顶部为 Case 标识、Connection Center 标题和 Google 账号管理入口。其下依次是：

1. Verified Core 汇总卡：完成度、进度条和唯一下一步；
2. 三列来源卡：公开 GBP、GSC、GA4；窄屏按同一顺序纵向堆叠；
3. 折叠的 `Optional: Official GBP Performance` 区；
4. Verified Client Action Plan CTA。

卡片标题旁只显示用户状态。展开区显示安全的技术状态、绑定资源、身份依据摘要、覆盖时间、
最近任务和健康原因。连接/换账号、选资源、确认身份、同步、重试和查看证据使用动作明确的
按钮文本。页面刷新从真实任务状态恢复，不依赖浏览器内存继续计时。

加载时保留卡片骨架，不能把未知状态闪烁成未连接。聚合读取失败显示页面级错误和重试；
单个 mutation 失败保留其余来源状态。不可重试错误给出重新授权、重新选择、确认身份或修复
测量配置的具体动作；可重试失败显示重试，且说明旧有效快照仍被保留。

## 9. 安全、错误与可访问性

- 未登录返回 401/重定向，跨用户或归档 Case 对外统一为 404。
- 接口保持 `cache-control: no-store`，拒绝无效 UUID、额外输入和超长参数。
- 聚合读取不获取 Google access token，不调用 Google、SerpAPI 或 Railway。
- 数据库异常映射为固定错误码，不把查询、表名、provider body 或异常对象返回浏览器。
- mutation 仍保留 same-origin、明确确认、幂等 key 和现有服务端权限检查。
- 状态不只依赖颜色；所有进度、警告和错误具有文本与可访问标签。
- 轮询在页面离开、任务结束或请求失败时停止；不创建后台自动同步。

## 10. 测试策略

Projector 单元测试覆盖完整状态矩阵：公开 GBP 缺失/模糊/变化；GSC/GA4 未连接、scope
缺失、无绑定、待确认、mismatch、可同步、queued/running、健康、不健康、过期、失败重试；
旧健康快照与新失败任务并存；官方 GBP 缺失时 Verified Core 为真但 Full Evidence 为假。

Repository/handler 测试覆盖 Case 所有权、归档 Case、无父报告、父报告身份陈旧、查询失败、
一次聚合读取、安全字段白名单、`no-store`、feature flag 和无外部 provider 调用。

组件测试覆盖三源卡顺序、状态文案与详情、唯一下一步、同步进度与轮询停止、错误恢复、
移动端堆叠、官方 GBP 折叠区，以及三个生成按钮状态。既有连接、资源、身份、GSC、GA4、
GBP 同步测试必须继续通过。

交付前运行前端全部测试、类型检查、正式构建、后端全部测试和差异检查。测试只使用 fake
repository 与脱敏 fixture，不调用真实 Google、SerpAPI、Supabase 生产库或付费接口。

## 11. 发布与回滚

继续使用现有 Git 正式发布链路。生产 `GOOGLE_CONNECTIONS_ENABLED`、各同步开关和未来
verified-analysis 开关保持关闭。发布后只验证 Vercel READY、正式域名、私有路由保护、
Railway/Redis/Worker 健康及新增错误日志；不发起真实授权或同步。

本轮无数据库迁移。回滚只需恢复前端提交或关闭 Connection Center/Google connections
入口，不影响现有连接、绑定、任务、快照或报告。

## 12. 完成标准与后续

完成标准是聚合合同、状态 projector、私有 API、统一页面、自动化测试、关闭状态生产发布
全部完成。它证明系统能可靠判断 Verified Core/Full Evidence 就绪状态，不代表 verified
报告生成已实现。

下一里程碑 V22-070 读取本接口给出的合格快照 ID，构建第一方 Findings；V22-071～074
完成跨源结论、重新优先级、版本差异和执行路线图。M7 两端开关启用后，既有 CTA 才执行
用户确认的 Verified Client Action Plan 生成。
