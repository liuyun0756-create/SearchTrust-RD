# SearchTrust v2.2 GBP 只读同步设计

日期：2026-09-07

范围：V22-062 GBP connector。

状态：用户已批准对话设计和书面规格，可进入实施计划。

主要仓库：`SearchTrust-RD` 负责提供者、严格规范化、健康评估、耐久 Worker 和到期清理；`search-trust` 负责数据库迁移、用户发起同步、状态查询和 GBP 同步控件。

## 1. 目标与非目标

为已授权、已选择、已确认属于当前 Case 的 Google Business Profile Location 提供用户主动触发的只读同步。一次成功同步同时取得 Business Information、180 天 Performance 和月度搜索关键词，并产生可追踪的健康结果。

本轮交付：

1. GBP 只读 HTTP provider、严格输入模型、规范化和健康评估。
2. 复用现有 `google_sync_jobs` 的 GBP 任务、令牌代理、耐久执行、失败重试和原子快照写入。
3. GBP Content 最长 30 个日历日的临时保留和不依赖同步开关的到期清理。
4. 用户发起的 GBP 同步 API、状态轮询与独立同步控件。
5. fake provider、脱敏固定 fixture、数据库约束和前端状态测试。

本轮不实现：

- GBP 资料编辑、帖子发布、评论回复、验证或任何写接口。
- `GoogleLocations` 的商家发现，也不用官方 GBP API 进行获客或竞品发现。
- 自动后台数据同步、报告生成时隐式同步或前端刷新时自动建任务。
- V22-063 三源 Connection Center，以及 V22-070 将 GBP 数据转换为完整第一方 Findings。
- 在 CI 或无正式 GBP API 权限的环境中调用真实商家。

## 2. 已核实基础与方案取舍

现有系统已完成 GBP Account/Location 发现、`business.manage` scope、Location 选择、Case 身份评估和用户显式确认。GSC 与 GA4 已共用 `google_sync_jobs`、五分钟租约、三次重试、服务端令牌代理和不可变快照。

数据模型已允许 `data_snapshots.source_type='gbp'`、`retention_policy='gbp_content_30d'`、最长 30 天的 `expires_at` 和仅允许 `raw_payload` 单向清空的不可变规则。

采用“扩展统一队列”方案：将 `gbp` 加入现有任务生命周期，但保持 provider、模型、健康评估和清理单元独立。不采用 GBP 专用任务表，因为它会复制权限、租约、重试和状态查询逻辑。不采用同步 HTTP 请求，因为 Google 限流、超时或页面关闭会丢失任务状态。

## 3. 依赖与合规边界

实现以 2026-09-07 核实的 Google 官方文档为准：

- Business Information `locations.get`：<https://developers.google.com/my-business/reference/businessinformation/rest/v1/locations/get>
- Location 资源：<https://developers.google.com/my-business/reference/businessinformation/rest/v1/locations>
- Performance API：<https://developers.google.com/my-business/reference/performance/rest>
- DailyMetric：<https://developers.google.com/my-business/reference/performance/rest/v1/DailyMetric>
- 月度搜索关键词：<https://developers.google.com/my-business/reference/performance/rest/v1/locations.searchkeywords.impressions.monthly/list>
- GBP API 政策：<https://developers.google.com/my-business/content/policies>

系统只能读取用户有权管理的 Location。Google 返回的 Business Information、Performance 数值和搜索关键词均视为受限 Content，不得通过“规范化”复制到长期字段来绕过 30 天限制。

应用只保留提高当前项目性能所需的有限 Content，安全保存且不超过 30 个日历日。长期仅保留不能重建 Google 返回值的请求口径、校验摘要、健康原因、覆盖状态和已生成的派生结论。

## 4. 架构和组件

### 4.1 `search-trust` 服务端

- GBP 同步 handler 校验 Clerk 登录、Case 所有权、有效 Google connection、`business.manage` scope、当前 active GBP binding、`matched` 身份和确认时间。
- handler 生成或接收幂等 `request_key`，调用仅 `service_role` 可执行的 `request_v22_gbp_sync` RPC。
- 状态服务只返回任务状态、有效健康状态、原因码、抓取时间和到期时间，不向浏览器返回 token 或原始 GBP Content。
- GBP 同步控件与 GSC/GA4 使用相同的排队、轮询、失败和过期交互语义，但用 GBP 专属文案和原因映射。

### 4.2 `SearchTrust-RD` 后端

- `GbpProvider` 仅暴露只读 `collect`，调用 Business Information Location GET、Performance multi-daily metrics GET 和月度关键词 GET。
- 严格模型与规范化函数拒绝未知枚举、超长字段、越界日期、重复日期/指标/关键词、负数、溢出和非预期响应形状。
- `evaluate_health` 为纯函数，只使用已验证的内存模型输出固定原因码。
- `execute_v22_gbp_sync` 复用现有令牌代理、租约和重试。它不记录 token、响应体、Location 名称、关键词或业务字段。
- 清理单元通过专用 RPC 小批量清除到期 Content。清理调度不依赖 `V22_GBP_SYNC_ENABLED`，避免关闭同步后留下过期数据。

## 5. 请求与数据口径

### 5.1 任务时间范围

`request_v22_gbp_sync` 在数据库内固化 `coverage_end = (UTC 当天) - 3 天`，使后端执行时不因日期跨界改变请求含义。

- 当前期：`coverage_end - 89` 至 `coverage_end`，共 90 天。
- 上一期：`coverage_end - 179` 至 `coverage_end - 90`，共 90 天。
- Performance 一次请求覆盖连续 180 天，规范化时按上述边界拆分。
- 关键词从覆盖起始日所在自然月到覆盖结束日所在自然月。该月份范围单独披露，不声称为精确 90 天数据。

### 5.2 Business Information

Location GET 的 `readMask` 固定包含：

`name,title,phoneNumbers,categories,storefrontAddress,websiteUri,regularHours,specialHours,serviceArea,openInfo,metadata,profile,moreHours,serviceItems`

这些字段支持 Location 绑定复核、健康检查和后续第一方 Findings。本轮不请求 reviews、media、posts、Q&A 或任何编辑专用数据。

### 5.3 Performance

固定读取七项跨行业指标：

1. `BUSINESS_IMPRESSIONS_DESKTOP_SEARCH`
2. `BUSINESS_IMPRESSIONS_MOBILE_SEARCH`
3. `BUSINESS_IMPRESSIONS_DESKTOP_MAPS`
4. `BUSINESS_IMPRESSIONS_MOBILE_MAPS`
5. `CALL_CLICKS`
6. `BUSINESS_DIRECTION_REQUESTS`
7. `WEBSITE_CLICKS`

预订、消息、外卖和菜单等行业专属指标不在 V22-062 读取或健康判定范围内。Google 对某天的零值可以不返回 `value`；内存分析按官方语义将该点视为零，同时保留覆盖提示。

### 5.4 Search Keywords

月度关键词每页最多 100 条，最多请求 10 页，即单次同步最多保留 1,000 条当期 Content。若第十页仍有 `nextPageToken`，记录 `GBP_KEYWORDS_TRUNCATED`。

`InsightsValue` 必须且只能含 `value` 或 `threshold`。阈值不转换为猜测值。空列表可以表示无数据或隐私阈值，只生成覆盖提示，不单独导致不健康。

## 6. 快照与 30 天 Content 生命周期

成功任务写入 `schema_version='gbp_sync_v1'`、`source_type='gbp'`、`sync_trigger='user_sync'` 和 `retention_policy='gbp_content_30d'`。

### 6.1 临时 Content

`raw_payload` 仅包含此次同步实际收到的有限 Google 响应对象：

- Business Information Location 响应；
- 180 天 multi-daily Performance 响应；
- 已读取的月度关键词分页响应。

不保存 access token、refresh token、请求头、响应头、错误正文、用户邮箱或任意未列入的 API 响应。`expires_at` 不得迟于 `fetched_at + 30 days`。

### 6.2 长期非 Content 记录

`normalized_payload` 仅保留：

- `schema_version`、绑定的 `resource_id`、覆盖日期和关键词月份范围；
- 请求指标名、请求页数、截断标记和限制码；
- 资料完整性检查的布尔结果，不保留实际名称、电话、网站、地址、营业时间、类别、服务或 Profile 正文；
- 当前期是否有曝光、比较期是否可用、关键词是否可用等覆盖布尔值，不保留指标数值或关键词；
- 内容 SHA-256 校验摘要、健康原因码和不可逆的派生结论引用。

`payload_checksum` 对经过严格 JSON 形状校验后的当期 Content 做稳定摘要。清理 Content 后保留摘要，用于证明当时处理输入的一致性，但摘要不能用来重建原值。

### 6.3 到期清理

专用 `cleanup_v22_expired_gbp_content` RPC 只允许将已到期 GBP 快照的 `raw_payload` 置空并设置 `raw_content_deleted_at`。每次处理固定上限的最旧记录，可幂等重试。

清理后：

- 快照有效状态为 `expired`，状态查询显示必须重新同步；
- 若它仍是该 binding 最新 GBP 快照，则 binding 状态改为 `expired` 并加入 `GBP_CONTENT_EXPIRED`；
- 若已有更新快照，清理旧快照不能降级新状态；
- 已保存的报告派生结论、快照关系和校验摘要保持不变。

## 7. 同步数据流和一致性

1. 用户点击 GBP 同步，前端服务端验证所有权和明确的 `confirm_sync=true`。
2. RPC 按 connection → Case → binding → job 的共享锁顺序创建任务，固化 `case_updated_at`、`resource_id`、`coverage_end` 和请求键。
3. Worker 发现待执行任务，只有在 Case、binding、connection、scope 和身份仍有效时才获得租约。
4. 令牌代理仅为 `purpose=source_sync, source=gbp` 返回短期 access token。
5. Provider 在四分钟任务上限内读取 Location、Performance 和最多十页关键词，然后删除内存 token 引用。
6. 严格规范化和健康评估在内存中完成。
7. finish RPC 重新验证租约和绑定，校验 schema、resource、日期、内容形状、健康码和摘要后，原子写入快照并完成任务。
8. 状态 API 返回最新任务和最新快照的有效状态。刷新或轮询不会建立新同步。

同一 binding 只允许一个 `queued` 或 `running` 任务。重复 `request_key` 必须返回同一逻辑任务；相同键若与 Case、binding 或请求口径冲突则拒绝。任务期间修改 Case、切换 binding、断开 connection 或撤销 scope 均导致 finish 失败，不保存半成品。

## 8. 健康规则

`healthy` 必须同时满足：

1. Google 授权和 Location 读取成功；
2. 资源 ID 与已确认 binding 相同，binding 仍为当前 Case 的 `matched` 资源；
3. `metadata.hasVoiceOfMerchant=true`；
4. `openInfo.status='OPEN'`；
5. title、websiteUri、primary phone、primary category 和 regular hours 均存在；
6. storefront address 或 service area 至少一种存在；
7. 当前 90 天四项 impressions 的总和大于零。

任一上述业务条件不满足时，成功快照的 `health_status='unhealthy'`，并使用固定原因码：

- `GBP_LOCATION_UNVERIFIED`
- `GBP_LOCATION_NOT_OPEN`
- `GBP_TITLE_MISSING`
- `GBP_WEBSITE_MISSING`
- `GBP_PHONE_MISSING`
- `GBP_PRIMARY_CATEGORY_MISSING`
- `GBP_REGULAR_HOURS_MISSING`
- `GBP_ADDRESS_AND_SERVICE_AREA_MISSING`
- `GBP_NO_CURRENT_IMPRESSIONS`

不单独导致不健康的覆盖码：

- `GBP_COMPARISON_UNAVAILABLE`：上一期无曝光；
- `GBP_KEYWORDS_UNAVAILABLE`：月度关键词为空；
- `GBP_KEYWORDS_THRESHOLD_APPLIED`：存在阈值而非精确数值；
- `GBP_KEYWORDS_TRUNCATED`：达到 1,000 条边界后仍有下一页。

本 connector 不重新执行网站与 GBP 字段级完全/部分/不匹配规则。该规则在之前的绑定确认和后续 Findings 中使用；V22-062 仅保证当前读取的 Location 没有与 binding 资源 ID 漂移。

## 9. 错误分类与重试

以下情况不保存快照：

- token 不可用且刷新失败；
- 用户丧失 Location 权限；
- Google 403/404 非限流拒绝；
- 资源 ID 不一致；
- Business Information、Performance 或必需的关键词请求结构无法验证；
- Case、binding、connection、scope 或租约在执行中改变。

HTTP 429、Google 5xx、网络中断和超时为可重试错误，最多执行三次。权限拒绝、资源不存在、请求非法、响应结构不合法和绑定变化为不可重试错误。

任务失败不覆盖或删除旧快照。前端必须同时显示“最近任务失败”和“旧快照已过期/仍在有效期”，不把旧结果当作新同步成功。

业务错误响应和日志只包含固定错误码和 diagnostic ID。不包含 Google 错误正文、token、Location 字段、Performance 数值或关键词。

## 10. 前端交互

GBP 资源卡在已选择 Location 后显示专属同步区域：

- 未确认身份时按钮禁用，提示先确认正确商家和分店。
- 点击同步后显示 queued/running，允许用户离开页面。
- 成功后显示抓取时间、healthy/unhealthy、可识别的原因文案和 Content 到期日期。
- 到期后只显示原始 GBP 快照已按政策过期，必须重新同步；不展示旧数值。
- 失败显示固定修复建议：检查授权、确认 Location，然后用新请求重试。
- 控件明确说明“只读，不会修改 Business Profile，不会购买或生成报告”。

## 11. 测试设计

### 11.1 后端单元与组件测试

- 使用 fake provider 和脱敏固定 fixture，覆盖完整 Location、门店和服务区商家、缺资料、未验证、关闭商家和非法响应。
- 验证 180 天边界、当前/上一 90 天分割、七项指标、缺失零值、越界/重复日期和大整数上限。
- 验证关键词空列表、精确值、阈值、分页、重复 token、第十页截断和超长字符串。
- 验证健康、无当前 Performance、无比较期、无关键词以及每个资料缺口的原因集合。
- 验证 Worker 租约、幂等完成、取消、超时、可重试/不可重试分类和 token 不进入持久输入。

### 11.2 数据库测试

- `source_type='gbp'` 任务必须绑定 GBP、已确认 `matched` 资源和具有 `business.manage` scope 的 active connection。
- 请求键幂等、一 binding 一个 active job、共享锁顺序、身份失效和租约丢失都有回归。
- finish 必须原子写入符合日期、schema、资源、健康、Content TTL 和摘要约束的快照。
- 使用可控时间验证第 29 天可用、第 30 天到期、单向清理、重复清理幂等和新快照不被旧快照降级。
- anon/authenticated 不能直接读写 jobs、调用内部 RPC 或读取 `raw_payload`。

### 11.3 前端测试

- handler 覆盖未登录、非 Case 所有者、非 GBP binding、未确认身份、丢 scope、重复请求和已在执行。
- 状态服务正确区分任务失败、有效旧快照、不健康快照和到期快照。
- 控件覆盖按钮禁用、排队、执行、成功、失败、健康、不健康、过期、离开页面后恢复轮询和无障碍状态文本。

CI 严禁访问真实 GBP API 或真实商家。集成 fixture 不得包含真实商家名称、电话、地址、网站或关键词。

## 12. 上线顺序和验收

1. 在 `V22_GBP_SYNC_ENABLED=false` 时应用数据库迁移。
2. 发布后端和前端代码，运行数据库目录、健康端点、旧 API 和 GSC/GA4 回归。
3. 保持 GBP 同步开关关闭，验证用户无法意外触发 Google 调用。
4. GBP 清理调度在存在存储配置时独立运行，不依赖同步开关。
5. 获得真实 GBP API 项目权限、通过 OAuth 和真实管理账号联调后，再单独开启 GBP 同步。

代码验收：

- 前后端全量自动测试、类型检查、限定范围 lint 和生产构建通过；
- 生产迁移和部署成功，开关关闭时不调用 Google；
- 用 fake provider 产生 healthy、unhealthy、failed 和 expired 路径；
- 到期 Content 可自动清理，不能从长期快照还原；
- 日志、浏览器和错误响应不包含 token 或 GBP Content。

真实凭据验收：用已批准 GBP API 的正式项目和可管理 Location 生成一份真实快照，验证 Business Information、Performance、关键词、严格健康原因和 30 天到期时间。没有真实权限时不得伪造该验收已通过。

## 13. 回滚

首先关闭 `V22_GBP_SYNC_ENABLED` 并停止新 GBP 任务调度。保留已完成 jobs、快照、健康摘要和审计关系，不破坏性删表。到期清理必须继续运行，直到所有受限 GBP Content 已删除。

回滚不改变 GSC、GA4、OAuth、Location 选择或旧报告语义。若未来迁移需要彻底移除 GBP job 支持，必须先证明所有 GBP Content 已按政策清理，再单独设计破坏性迁移。
