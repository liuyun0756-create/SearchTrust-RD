# SearchTrust v2.2 — V22-070 第一方 Findings 设计

日期：2026-09-08

状态：用户已批准对话设计，等待书面规格复核后进入实施计划。

主要仓库：`SearchTrust-RD` 负责可信快照解析客户端、第一方证据、规则目录、确定性
Findings 引擎和 checkpoint；`search-trust` 负责一个仅 `service_role` 可调用的可信快照
解析 RPC 及数据库权限测试。本里程碑不新增用户页面，也不开放 Verified Generation。

## 1. 目标与范围

V22-070 把已经同步并绑定到当前 Case 的 GSC、GA4 和可选官方 GBP 数据转换为可追踪的
单来源 Findings。每条结论只能由一个第一方来源独立触发，并保留其数值证据、比较证据、
规则版本、适用范围、限制和改变条件。

本轮交付：

1. 从快照 ID 解析可信第一方输入的私有数据库边界。
2. GSC 机会、GA4 行为/转化、GBP Performance 和测量配置规则。
3. `triggered`、`not_triggered`、`not_checked` 三态评估及来源资格摘要。
4. 稳定排序、边界限制、严格引用校验和可恢复 checkpoint。
5. 单元、数据库、集成与安全回归测试。

本轮不实现：

- GSC、GA4、GBP 之间的跨来源关联或因果归因；
- 新证据驱动的行动重新排序、核心问题重写或 30/60/90 路线图；
- parent report 的 Confirmed/Reprioritized/Refined/Replaced/New 版本差异；
- 浏览器端指标提交、自动同步、实时 Google 调用或正式 Verified 报告生成；
- 行业平均值、外部 CTR 曲线、机器学习异常模型或 LLM 事实判断。

V22-071～074 分别承接跨来源 Findings、重新优先级、版本差异和执行路线图。

## 2. 已批准的产品决策

- 采用确定性规则目录。相同可信输入和规则版本必须生成完全相同的结果。
- 规则引擎决定事实是否成立；文案层以后可以润色，但不能改变数值、结论或证据。
- 业务 Finding 只使用身份匹配、完整、未过期且满足相应数据资格的来源。
- 不健康来源不能生成业务表现结论，但可以用明确健康原因生成测量配置 Finding。
- 完全未连接或同步失败的来源不生成事实 Finding，只形成覆盖限制和 `not_checked`。
- 官方 GBP Performance 仍是可选增强；缺失时不阻断 Verified Core，但不能宣称 Full
  Evidence，也不能生成 GBP Performance Finding。
- 阈值使用当前 90 天对前 90 天的相对变化以及站点内基线，并同时设置最小样本门槛。
- 比较期没有数据时不计算增长率或下降率；Top rows 中缺失的维度不能补零。
- 输出决策相关的正向、负向和测量配置 Findings，不把每个正常指标都变成结论。

## 3. 方案比较与选择

### 方案 A：确定性规则目录（采用）

显式定义数据资格、最小样本、相对阈值、证据要求、严重度和改变条件。优点是稳定、可
审计、可回归和可解释，最适合报告事实层；缺点是需要维护规则版本和边界用例。

### 方案 B：统计异常模型

根据历史波动动态识别异常。它能适应不同体量，但当前只有相邻两个 90 天窗口，无法建立
可靠季节性和方差基线，容易把正常波动误报为机会。

### 方案 C：LLM 直接解读

表达灵活，但事实触发不稳定，难以保证同一输入幂等，也更容易越过证据边界。因此 LLM
不得决定本轮 Finding 是否成立。

## 4. 架构与组件边界

实现分为五个独立单元：

1. **可信快照解析 RPC**：输入 Case、parent report 与 2～3 个快照 ID；在数据库内校验
   Case、活动 binding、来源类型、身份、健康、有效期、内容状态和不可变摘要，只允许
   `service_role` 调用。
2. **解析客户端/仓储**：Railway 使用服务端凭据调用 RPC，把数据库响应严格解析为来源
   明确的 GSC、GA4、GBP 输入；不接受浏览器提供的指标 payload。
3. **第一方证据构建器**：复用现有证据身份与 trace 结构，为当前值和比较值分别建立
   Evidence ID；不估算、不聚合不同 breakdown，也不补零。
4. **来源规则目录**：GSC、GA4、GBP 各自拥有小型、纯函数规则模块；共享模块只处理三态
   输出、相对变化、最小样本、置信度上限、稳定排序和引用校验。
5. **checkpoint 阶段**：输入摘要包含阶段版本、规则集版本、Case、parent report、快照
   摘要与限制。命中已验证 checkpoint 时复用结果；损坏或不一致时安全失败。

新增内部 `FirstPartyFindingsInput` 和 `FirstPartyFindingsResult`，不扩宽现有
`PublicFindingsInput`。结果合同版本固定为 `first_party_findings_v1`，至少包含：

- `evidence_result`；
- `findings`；
- `rule_evaluations`；
- `source_assessments`；
- `ruleset_version`。

现有冻结 `Finding` 模型保持不变。第一方专用的 target、reason code 和 source assessment
使用新内部模型，避免把公开报告专用枚举改成含义宽松的通用字典。

## 5. 可信数据资格

数据库解析边界必须一次性验证：

1. Case 存在、仍 active，并与 parent report 属于同一 Case。
2. parent report 是当前 Case 的可用 v2.2 prospect 报告。
3. GSC 与 GA4 快照 ID 均存在；GBP ID 可以省略。
4. 每份快照属于当前 Case、当前活动 binding 和声明的来源类型。
5. binding 仍为 `matched` 且确认未失效，资源 ID 与快照请求上下文一致。
6. 快照 checksum、schema、覆盖日期和 payload 形状可验证。
7. GSC/GA4 未过期；GBP `expires_at` 尚未到达且 `raw_payload` 尚未清理。

跨 Case、旧 binding、错误来源、摘要不一致或 parent report 不匹配属于请求级安全错误，
整个阶段不运行。来源业务健康不足不是结构错误：该来源的业务规则变为 `not_checked`，
只允许明确的测量配置规则继续运行。

数据库 RPC 不授予 `anon` 或 `authenticated`。它不返回 token、OAuth subject、连接密文、
任务 lease 或 provider 错误正文。GBP Content 只在受信服务间、现有 30 天保留期内用于
本次派生，不进入浏览器、日志、checkpoint 或长期新字段。

## 6. 规则状态与统一语义

每个规则/目标组合必须产生且只产生一种状态：

- `triggered`：条件成立，必须生成一个引用完整的 Finding。
- `not_triggered`：数据足够且条件明确不成立，不生成 Finding。
- `not_checked`：来源不可用、不健康、比较期不足、目标行不可证明或限制阻止判断，不能
  生成 Finding。

`not_checked` 原因至少区分：来源缺失、身份不合格、来源不健康、比较期不可用、最小样本
不足、字段未观察、Top rows 截断、采样/阈值限制、GBP Content 过期。来源完全缺失时仍保留
source assessment，便于后续覆盖与 UI 解释。

只有经规则明确允许的健康原因可以触发测量配置 Finding。未配置关键事件、资料字段缺失
等是已观察事实；网络失败或“无数据”本身不能被改写成“业务表现差”。

## 7. GSC 规则目录

GSC 只使用 `gsc_sync_v1` 当前和前一 90 天的 totals、queries、pages 及相关限制。不同
breakdown 不相加。

### 7.1 查询机会

- 当前查询曝光至少 50；
- 平均位置在 4～20；
- 查询 CTR 不高于当前站点整体 CTR 的 80%；
- totals 与查询行均为可用观察。

触发后陈述“该查询已有可见需求和接近第一页/第一页非领先位置，但点击效率低于本站当前
整体基线”。不使用行业 CTR 曲线，也不声称 Google 返回了全部查询。

### 7.2 页面机会

- 当前页面曝光至少 100；
- 平均位置在 4～20；
- 页面 CTR 不高于当前站点整体 CTR 的 80%。

URL 必须来自 GSC 页面维度的可验证值。无法安全规范化的 URL 只保留内部 scope，不进入
`affected_urls`。

### 7.3 查询或页面下降

- 当前和上期存在同一规范化维度；
- 上期至少 50 次曝光或 10 次点击；
- 曝光或点击下降至少 20%。

若任一周期相关视图截断且目标只在单侧出现，则该目标为 `not_checked`，不得把另一侧补零。

### 7.4 正向增长

- 当前和上期存在同一规范化维度；
- 上期至少 10 次点击；
- 当前点击增长至少 20%。

增长 Finding 只陈述观察变化，不推断具体优化导致了变化。

### 7.5 GSC 测量覆盖

`GSC_NO_CURRENT_DATA`、`GSC_NO_QUERY_ROWS`、`GSC_NO_PAGE_ROWS`、近期活动缺失或长活动空档
可以形成测量覆盖 Finding。它们使用“需要检查覆盖或低活动原因”，不得断言跟踪代码损坏。

## 8. GA4 规则目录

GA4 只使用 Case host-filtered 的 `ga4_sync_v1` totals、landing pages、dates、key events
和配置元数据。

### 8.1 落地页互动缺口

- 当前页面至少 20 次 sessions；
- 页面 engagement rate 不高于当前站点整体 engagement rate 的 80%。

若整体 engagement rate 为零或相关指标受 restriction，则规则为 `not_checked`。

### 8.2 落地页转化缺口

- Property 已配置至少一个 key event；
- 当前页面至少 20 次 sessions；
- 页面 key events 为零，或其 session key event rate 不高于整体比率的 50%。

整体比率为零时只允许“有会话但未观察到关键事件”的事实，不做相对低于整体的判断。

### 8.3 页面流量/互动变化

- 当前和上期存在同一 landing page；
- 两期各至少 20 次 sessions；
- sessions、engagement rate 或 session key event rate 的相对变化至少 20%。

每个页面合并为一个方向一致的 Finding；方向冲突的指标分别保留证据，但不拼成单一好坏
判断。

### 8.4 GA4 测量配置

- `GA4_NO_CONFIGURED_KEY_EVENTS`：高严重度测量配置 Finding；
- `GA4_CONFIGURED_KEY_EVENTS_NO_ACTIVITY`：中严重度、说明已配置但当前未观察到活动；
- `GA4_LANDING_PAGE_NOT_SET`：存在 `(not set)` 且对应 sessions 大于零时形成覆盖 Finding；
- 无当前 sessions、近期活动缺失或长活动空档：形成需要检查的覆盖 Finding。

sampling、thresholding、other-row data loss 和 metric restrictions 默认是证据限制；当它们
直接阻止某条规则时该规则为 `not_checked`，不把平台处理限制称作客户配置错误。

## 9. GBP 规则目录

GBP 只在官方 `gbp_sync_v1` Content 仍有效时，读取固定七项跨行业 Performance 指标和
月度关键词。公开 SerpAPI GBP 不得进入本规则目录，也不得冒充官方 Performance。

### 9.1 曝光变化

- 上期四项 Search/Maps、Desktop/Mobile impressions 合计至少 50；
- 当前总曝光相对变化至少 20%。

允许同时保留四项构成证据，但不把重叠指标重复相加。

### 9.2 客户行动变化

- 上期 `CALL_CLICKS`、`WEBSITE_CLICKS`、`BUSINESS_DIRECTION_REQUESTS` 合计至少 10；
- 当前同口径合计相对变化至少 20%。

结果只称为“Google Business Profile 客户行动”，不等同于销售、leads 或收入。

### 9.3 搜索需求信号

规则运行期间只用仍在保留期内的精确 `value` 判断稳定的需求档位和顺序，不把关键词文本
或精确次数写入 checkpoint、长期 EvidenceItem 或报告。长期结果只保留不可逆的派生事实，
例如“已观察到一个达到规则门槛的领先搜索需求主题”，以及指向临时快照的 Evidence ID。
只有 `threshold` 的关键词可以形成定性覆盖限制，不转换为具体值，也不与精确值混排成精确
榜单。空关键词表不证明没有搜索需求。

### 9.4 GBP 资料与测量问题

未验证、非营业状态、缺 title、website、phone、primary category、regular hours、地址和
服务区均可生成来源明确的配置 Finding。`GBP_NO_CURRENT_IMPRESSIONS` 生成覆盖/可见度检查
Finding，不直接断言商家没有客户或没有排名。

## 10. 排序、置信度与输出限制

规则先输出全部合法结果，再按固定键排序：来源顺序 GSC → GA4 → GBP、规则优先级、严重度、
影响量和规范化 scope。相同键产生不同内容属于 ID 冲突并安全失败。

默认每个来源最多保留 10 条业务 Finding 和 5 条测量 Finding，总 Finding 上限 45；规则
评估仍保留被数量上限排除的目标状态及原因。限制只在完整排序后应用，不能依赖输入行顺序。

置信度受证据质量上限约束：

- 精确 totals/配置事实且无相关限制：最高 `high`；
- Top-row 或当前期单侧观察：最高 `medium`；
- sampling、thresholding、截断或不完整覆盖：降低到 `low`，或在会误导时直接
  `not_checked`。

严重度由规则目录确定，并可按变化幅度进入更高固定档位；不得由文案模型修改。

每条 Finding 必须包含 evidence IDs、comparison IDs、rule ID/version、classification、
severity、scope、confidence、affected URLs/queries、missing data 和 change conditions。

## 11. 幂等、引用与 30 天 GBP 生命周期

Evidence ID 继续由来源快照和精确 selector 稳定生成。Finding ID 使用现有 Finding 身份
算法，由 Case、规则版本、目标和证据集合确定。新快照自然产生新证据身份，V22-073 再用
规则、scope 和 fingerprint 进行版本关系判断。

checkpoint 输入摘要包含完整可信输入、stage version 和 ruleset version；结果同时保存摘要
并在读取时重新验证模型、大小、版本和 checksum。输入相同必须命中同一结果；规则升级必须
因版本变化而重新计算。

GBP 原始 Content、精确 Performance 数值、关键词文本和关键词次数不复制到 checkpoint。
GBP 的长期 EvidenceItem 只保存由规则产生且不能还原原始值的档位事实、证据引用、限制和
不可逆摘要；内存中的精确比较值在本次规则运行结束后丢弃。GBP Content 到期后：

- 不允许重新运行旧快照；
- 要求用户重新同步才能产生新 GBP Finding；
- 已保存报告中的派生结论与审计关系保持不变；
- 清理任务继续独立运行，不受 Verified Generation 开关影响。

## 12. 错误处理

请求级固定错误至少覆盖：输入无效、binding 无效、parent report 无效、checksum 不一致、
GBP Content 过期、引用不一致、ID 冲突、checkpoint 损坏和资源上限超出。对外错误只返回固定
代码和安全说明，不返回 SQL、表名、provider body、原始异常或 Content。

可重试范围只包括可信快照解析或 checkpoint 存储的临时网络/服务错误。合同、身份、过期、
摘要和引用错误不可重试，必须重新选择/同步或修复调用输入。失败不得保存部分 Findings，也
不得覆盖已验证 checkpoint 或旧报告。

## 13. 测试策略

### 13.1 规则单元测试

- 为每条规则覆盖门槛以下、等于门槛、超过门槛；
- 覆盖正向、负向、无变化、零分母、最小样本不足和比较期缺失；
- 覆盖单侧行、双侧截断、GSC breakdown 不相加；
- 覆盖 GA4 sampling、thresholding、other row、restriction 和 `(not set)`；
- 覆盖 GBP 精确关键词、threshold、空列表、分页截断和 Content 过期；
- 覆盖所有允许生成配置 Finding 的健康原因以及未列入 allowlist 的原因。

### 13.2 不变量与边界测试

- 输入顺序变化不改变输出；重复运行字节级一致；
- GSC 证据不能触发 GA4/GBP 规则，任何规则都不能读取第二个来源；
- `triggered` 必须有 Finding，`not_triggered/not_checked` 必须没有 Finding；
- 所有 evidence/comparator ID 存在、来源正确、scope 匹配；
- 不健康证据不能进入业务 Finding；
- 拒绝重复维度、未知指标、NaN、Infinity、超大数组、超长文本和结果体积越界。

### 13.3 数据库与集成测试

- RPC 只允许 `service_role`，拒绝 `anon/authenticated`；
- 覆盖跨用户/跨 Case、归档 Case、旧 binding、来源替换、身份失效和 parent report 错误；
- 覆盖 GSC/GA4 必需、GBP 可选、GBP raw Content 已清理和竞态重读；
- 覆盖 checkpoint 命中、版本变化、损坏、重试和半成品不落地；
- 日志和错误响应不含 token、OAuth subject、GBP Content、关键词或 provider body。

所有测试使用 fake repository 和脱敏固定 fixture；CI 不调用 Google、SerpAPI、Supabase
生产库或任何付费接口。

## 14. 发布、回滚与完成标准

发布顺序：

1. 在所有相关功能开关关闭时应用最小增量数据库迁移并运行数据库语义测试；
2. 发布 Railway API/Worker 第一方 Findings 代码；
3. 运行后端和前端全量测试、类型检查、生产构建与差异检查；
4. 验证 Vercel、Railway API、Worker、Redis 和近期错误日志；
5. 不发起真实 OAuth、同步、SerpAPI、付款或 Verified Generation。

`GOOGLE_CONNECTIONS_ENABLED`、GSC/GA4/GBP 同步开关以及前后端 Verified Analysis 开关继续
保持 absent 或非 `true`。本里程碑发布能力代码不等于向用户开放生成。

回滚首先保持/关闭 Verified Analysis 开关，再回滚 Railway 代码。数据库 RPC 为只读且权限
封闭，可以保留；若必须移除，应使用后续增量迁移撤销函数，不回滚或删除历史快照。

V22-070 完成标准是：可信快照解析、三套单来源规则、三态评估、严格证据引用、checkpoint、
自动化测试、文档和关闭状态生产发布全部通过。它不要求真实 Google 账号验收，也不生成
最终 Verified Client Action Plan。
