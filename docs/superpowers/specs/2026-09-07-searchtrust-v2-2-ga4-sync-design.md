# SearchTrust V2.2 — V22-061 GA4 synchronization design

日期：2026-09-07。状态：产品设计已确认，等待书面规格复核。

## 1. 目标与范围

V22-061 为已确认匹配的 GA4 Property 提供用户主动触发的持久化同步，生成
不可变 `ga4_sync_v1` 快照与数据源健康结论。同步使用最近完整 90 天和前 90 天，
只统计当前 Case 网站域名，读取落地页、互动和关键事件聚合数据。

本里程碑不实现 GA4 用户级数据、实时报告、广告费用、电商收入、自定义指标、
GA4/GBP 报告 Findings、完整 Connection Center 或自动定时采集。GSC 既有合同、
报告生成和付款流程不在本轮修改范围内。

## 2. 已确认的产品决策

- 绑定对象保持 GA4 Property；同步数据通过 `hostName` 限定到当前 Case 网站。
- Case 为 `www.example.com` 时同时接受 `www.example.com` 和 `example.com`；
  其他子域名仅接受精确主机名，不把 Property 内其他网站混入快照。
- 同步全部已配置关键事件，并保存当前有数据的前 100 个事件名称。
- Property 未配置关键事件为不健康；已配置但当前周期计数为零只显示警告。
- 当前周期为请求日前两天结束的 90 个完整自然日；前一周期为紧邻的前 90 天。
  日期在创建任务时冻结，重试不得漂移。
- 只保存聚合数据和必要配置元数据，不保存 URL 查询参数、用户或设备标识符、
  Client ID、IP、事件参数、访问令牌或 Google 原始响应。

## 3. 方案比较与选择

### 方案 A：复用 `google_sync_jobs`，增加 GA4 独立合同（采用）

扩展现有通用任务表的 source 类型和生命周期，在 API、Provider、健康评估与
快照提交处保持 GSC/GA4 独立。复用租约、重试、令牌代理和前端状态模式，减少
重复基础设施，同时不让两种来源共享指标模型。

### 方案 B：新建 `ga4_sync_jobs`

隔离强、迁移直观，但复制所有权、幂等、租约、重试和查询逻辑，后续 GBP 又会
产生第三套状态机，维护与安全审计成本较高。

### 方案 C：在前端请求中直接同步

实现较短，但会受 Serverless 超时和刷新中断影响，无法可靠恢复，也容易让短期
令牌生命周期与用户请求耦合，因此不采用。

## 4. 架构与组件边界

1. 私有 Next.js GA4 sync API 只负责认证、输入约束、任务请求和安全状态投影。
2. Supabase `google_sync_jobs` 记录用户意图、固定时间范围、Case/绑定/连接版本、
   过滤主机名、尝试次数和租约；浏览器角色无表或 RPC 权限。
3. Railway 现有 ARQ Worker 的协调器只分发已存在且到期的用户请求，不创建周期任务。
4. GA4 Token Broker 客户端复用现有 HMAC 请求合同，purpose 仍为 `source_sync`，
   source 固定为 `ga4`。访问令牌只存在于 Worker 内存。
5. GA4 Provider 分为 Admin 配置读取、Data API 报告、严格规范化三个小单元。
6. GA4 健康评估器只读取规范化快照，不访问网络和数据库。
7. 数据库完成 RPC 在同一事务内重新验证当前身份并插入不可变快照、更新绑定和任务。

GSC 和 GA4 可共享任务仓储与执行外壳，但 Provider、payload schema、健康规则、
错误码前缀和提交验证必须保持来源明确。不得为复用而把不同指标塞进宽松字典。

## 5. 请求与身份数据流

1. 用户在 GA4 资源页点击同步并发送稳定 UUID idempotency key。
2. API 验证 Clerk 用户、活动 Case、活动 GA4 绑定、`matched` 身份状态、活动连接，
   以及 `openid`、`email`、`profile`、`analytics.readonly` 全部 scope。
3. Case URL 在服务端解析为规范化 ASCII 小写主机名。拒绝凭据、端口、IP、无效域名、
   空值和超长值。只有 apex/`www` 对可生成两个允许主机；其他子域名只生成一个。
4. request RPC 按 connection → Case → binding 顺序加锁，保存 Case `updated_at`、
   Property ID、允许主机列表和固定日期边界。同一请求 UUID 返回原任务；同一绑定的
   不同活动请求返回冲突，不静默冒充幂等成功。
5. Worker 先领取带五分钟 fenced lease 的任务，随后才向令牌代理请求短期 token。
6. Provider 对每个报告同时添加 `platform=web` 与允许 `hostName` 精确匹配过滤。
7. 全部 Admin/Data API 请求成功并通过严格模型校验后计算 checksum 与健康状态。
8. finish RPC 重新按固定锁顺序验证连接、Case 版本、绑定、Property、过滤主机和 scope。
   只有匹配 lease 的当前执行可提交；撤销、替换或 Case 修改使旧任务失败。
9. 原子插入新快照并设置 `supersedes_snapshot_id`。任意请求失败时不写部分快照；
   上一份成功快照保持可读。

## 6. 时间范围与请求集合

日期按任务创建时的服务端日历计算：`coverage_end = request_date - 2 days`；当前周期
为 `[coverage_end - 89 days, coverage_end]`，前一周期紧接在当前周期之前且同为
90 个含首尾的自然日。两个周期分别请求，避免行归属与响应元数据混淆。

同步首先分页读取一次 Admin API `properties.keyEvents.list`，每页不超过 200，
最多 20 页；检测循环 page token、响应超限或字段异常即拒绝本次同步。随后每个周期
执行四个 `runReport`，共八个报告请求：

| 视图 | 维度 | 指标 | 行限制 |
| --- | --- | --- | --- |
| totals | 无 | sessions, activeUsers, engagedSessions, engagementRate, averageSessionDuration, screenPageViews, keyEvents, sessionKeyEventRate | 1 |
| landing_pages | landingPage | sessions, engagedSessions, engagementRate, averageSessionDuration, screenPageViews, keyEvents, sessionKeyEventRate | 250 + 1 |
| dates | date | sessions, engagedSessions, keyEvents | 90 |
| key_events | eventName | keyEvents | 100 + 1 |

明细按主指标降序，额外读取一行只用于判断截断，不进入快照。`landingPage` 不带查询
字符串；`(not set)` 可作为 Google 的真实维度值保留并标记限制。key_events 视图只
保留 `keyEvents > 0` 的行。关键事件配置保存 `eventName`、`countingMethod`、custom、
deletable 和 createTime 的严格白名单字段；不保存默认金额或其他可变配置内容。

## 7. `ga4_sync_v1` 快照合同

顶层字段：schema version、Property ID、允许主机、固定时区说明、current、previous、
configured_key_events、limitations。每个周期包含 start/end、四个视图和每个视图的
response metadata。

数字字段拒绝 NaN、Infinity、负数与意外字符串。比率限制为 0–1；GA4 计数型 int64
字符串必须无损解析为不超过 `2^53-1` 的非负整数，超界则安全失败；时长和比率使用
有限浮点数。快照因此保持可被前后端一致读取的确定性数字合同。响应 headers、
dimension headers 与 metric headers 必须和请求完全一致，行值数量必须匹配，不接受
重复维度键、未知字段或超过预期上限的响应。

每个报告只白名单保存：

- `subjectToThresholding`；
- `dataLossFromOtherRow`；
- 每个日期范围的 `samplesReadCount` 与 `samplingSpaceSize`；
- `emptyReason`；
- Property IANA `timeZone`；
- `schemaRestrictionResponse.activeMetricRestrictions` 的指标名和固定限制类型。

若多个响应返回不一致的 Property 时区则整次同步失败。采样比例由两个整数计算，
不得把采样结果表示为完整计数。所有视图显式声明 Host 过滤、Top-row 限制以及不同
明细不可相加。

## 8. 健康评估

下列任一当前周期问题使绑定为 `unhealthy`：

- `GA4_NO_CURRENT_SESSIONS`：当前 totals 无 sessions 或 sessions 为零；
- `GA4_NO_LANDING_PAGE_ROWS`：当前无落地页行；
- `GA4_NO_ACTIVITY_DATES`：当前无日期行；
- `GA4_NO_CONFIGURED_KEY_EVENTS`：Property 未配置关键事件；
- `GA4_RECENT_ACTIVITY_MISSING_REVIEW`：最近有 sessions 的日期距周期结尾超过 7 天；
- `GA4_ACTIVITY_GAP_REVIEW`：两个有 sessions 的日期之间连续空缺至少 14 天。

以下只产生限制或警告，不单独改变健康状态：

- 已配置关键事件但当前 keyEvents 为零；
- 当前 engagedSessions 为零；
- 前一周期无 sessions，无法比较；
- 任一响应被采样、受阈值限制、出现 `(other)` 数据损失或活动指标权限限制；
- 落地页/事件明细截断；
- Google 返回 `(not set)` 落地页；
- Sessions 与用户数为 GA4 估算指标，不能表述为绝对用户事实。

健康文案使用“需要检查”而非“跟踪代码已损坏”。低流量、隐私阈值和处理延迟可能
导致安静期；不从缺失行推导零值，不从采样数据外推完整值。

## 9. 错误、重试与安全

- 网络、超时、HTTP 429、5xx 和明确配额临时限制为可重试；最多三次并延迟重排。
- 401 记为 token expired 并在下一次尝试重新获取 token。
- 撤权、Property 不可访问和非配额 403/404 为永久失败，提示重新连接或重新选择。
- 指标不兼容、响应过大、schema/header/数值异常为固定安全错误，不保存 Google 正文。
- 所有 HTTP 调用禁用重定向、设置有限超时和 2 MB 响应上限；只允许固定 Google HTTPS
  origin 和经正则验证的 `properties/[0-9]+` 路径，防止 SSRF 与路径注入。
- 日志只记录固定错误码、任务 ID 和阶段；不插入 token、Authorization header、URL
  查询、Google body 或异常对象。浏览器响应同样使用固定错误码。
- Worker 取消不标记成功；过期 lease 可恢复。旧 lease、重复完成和重复失败不能覆盖
  新执行结果。最终失败保留此前快照与其健康状态。

## 10. 前端与功能开关

新增 GA4 私有同步/status endpoint 和最小状态控件，沿用 GSC 的按钮、轮询、刷新、
失败与旧快照展示模式。公开报告、付款页和未登录流程没有入口。

前端 `GOOGLE_GA4_SYNC_ENABLED` 与 Worker `V22_GA4_SYNC_ENABLED` 默认关闭；前端还要求
既有 Google connection 配置启用。只开一侧时必须安全失败，不得产生 Google 请求。
正式发布保持两个开关关闭，真实账号验收与开放是独立操作。

## 11. 数据库迁移策略

以增量迁移扩展 `google_sync_jobs.source_type` 支持 `gsc` 与 `ga4`，添加受约束的允许
主机字段，并新增来源明确的 request/claim/finish/fail RPC 或通用内部 helper 与来源
包装 RPC。已有 GSC 任务和快照语义不改变；GA4 完成 RPC 只接受 `ga4_sync_v1`、GA4
日期结构和匹配 Property/主机。

迁移先在 PostgreSQL 语义测试中验证，再应用生产数据库，最后发布应用。回滚通过关闭
GA4 开关停止新任务，保留审计与快照数据，不破坏性删除生产历史。

## 12. 测试与验收

数据库测试覆盖：跨用户、缺少 scope、未确认身份、幂等 UUID、并发活动任务、固定日期、
host 冻结、claim lease、过期恢复、旧 lease、三次重试、Case 修改、绑定替换、撤权、
错误 payload、GA4/GSC 来源隔离、不可变快照和历史链接。

Provider/健康测试使用 fake HTTP 与脱敏 fixtures，覆盖：apex/`www`、子域精确过滤、两个
周期不重叠、一个可分页的 Admin 请求集合与八个报告请求、分页与循环 token、空数据、
已配置零事件、未配置事件、
Top rows、`(not set)`、采样、阈值、other row、指标权限、时区冲突、headers、数值边界、
HTTP 分类、响应上限及 secret-safe 日志。

前端测试覆盖：开关关闭、未登录、输入边界、所有权失败、活动冲突、同步进度、成功、
失败、上一快照保留、expired 状态、取消轮询和安全投影。交付前运行全部前后端测试、
前端类型检查、生产构建、迁移测试与差异检查。

发布验收仅确认迁移版本、Vercel READY、Railway API/Worker/Redis 健康、开关保持关闭和
无新增错误日志。真实 Google 验收需要批准的 OAuth 凭据和 owned test Case，需覆盖成功
同步、无关键事件、零事件、采样/阈值、重试、撤权和重新绑定；CI 不调用真实 GA4。

## 13. 完成标准与后续

完成标准是代码、迁移、自动化测试和关闭状态的生产部署全部通过；这不等于真实 Google
账号验收完成。V22-061 不升级报告。后续 V22-062 实现只读 GBP 同步，V22-063 再统一
三源 Connection Center 和 Evidence Coverage gate。

自检结论：没有未决产品选项；日期不重叠；Property 范围通过 Host 过滤收窄；关键事件
配置与零活动已区分；采样、阈值和 other row 不被当作完整数据；无用户级数据；没有
周期自动采集；GSC 合同和既有生产历史保持兼容。

## 参考资料

- https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport
- https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema
- https://developers.google.com/analytics/devguides/reporting/data/v1/reporting-data-expectations
- https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/ResponseMetaData
- https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.keyEvents/list
- https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/FilterExpression
