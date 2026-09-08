# SearchTrust v2.2 — V22-071 跨来源 Findings 设计

日期：2026-09-08

状态：用户已批准对话设计，等待书面规格复核后进入实施计划。

主要仓库：`SearchTrust-RD`。本里程碑复用 V22-070 的可信快照解析、第一方 Findings、
Evidence 身份和 checkpoint 基础，只新增 Railway 内部跨源关联能力；`search-trust` 不新增
页面、浏览器接口或数据库迁移。

## 1. 目标与范围

V22-071 把同一 Case、同一 parent report、同一批可信第一方快照中的两个来源进行有界关联，
输出可追踪、可复现且不越过因果边界的跨来源 Findings。每条业务 Finding 必须由两个健康、
匹配、未过期且可比的来源共同支持。

本轮交付：

1. GSC 与 GA4 的页面级、整体趋势和完整周序列关联。
2. 可选官方 GBP 与 GSC/GA4 的整体趋势关联。
3. 两侧方向相反时的测量一致性 Finding。
4. 页面身份规范化、时间窗口对齐、严格双来源证据校验。
5. 三态评估、确定性排序、输出限制和可恢复 checkpoint。

本轮不实现：

- 用户级、会话级或事件级 join；
- 渠道、排名、改版或具体行动的因果归因；
- 模糊页面匹配、标题/slug 猜测或 redirect/canonical 推断；
- 行业基准、自动异常发现、机器学习或 LLM 事实判断；
- 新证据重新排序、核心问题重写、三项行动或 30/60/90 路线图；
- parent report 版本差异或最终 Verified Client Action Plan；
- 浏览器端数据提交、实时 Google 调用或正式 Verified Generation。

V22-072～074 分别承接重新优先级、版本差异和执行路线图。

## 2. 已批准的产品决策

- 采用确定性双源规则矩阵，不建立通用信号图或自动关系发现器。
- 至少两个健康且可比的数据源同时支持，业务跨源 Finding 才能触发。
- 第三来源缺失、不健康或不可比不会否定已由两源证明的事实，但必须成为明确限制。
- 覆盖 GSC↔GA4 页面/整体/日期规则，以及官方 GBP 可用时的 GSC↔GBP、GA4↔GBP
  整体规则；GBP 不做关键词或页面级强行关联。
- 同向趋势要求两侧分别通过 V22-070 样本门槛，并各自达到至少 20% 相对变化。
- 方向冲突只生成测量一致性 Finding，不判断跟踪、网站、排名或业务原因。
- 两个 90 天窗口结束日期最多相差 3 天；日期级分析只用共同完整自然周，并排除 GA4
  最近 2 天。
- 日期级共同波动使用 Spearman；至少 8 个完整共同周，相关系数绝对值门槛为 0.70。
- 每个匹配页面独立评估，同时保留整体趋势；每个页面规则族最多输出 5 条业务 Finding。
- 本轮不把跨源 Finding 接入行动优先级，V22-072 再处理排序。

## 3. 方案比较与选择

### 方案 A：确定性双源规则矩阵（采用）

为 GSC↔GA4、GSC↔GBP 和 GA4↔GBP 分别定义输入资格、指标组合、样本、阈值、证据、
限制和固定文案。它最容易审计、解释和回归，也保持 V22-071 与后续优先级逻辑分离。

### 方案 B：通用信号图

先把所有来源转换为统一信号节点，再用关系规则查询。扩展性较高，但会在本轮提前引入较重
抽象，也容易与 V22-072 的评分和优先级职责混合。

### 方案 C：统计关联引擎

自动枚举指标并寻找相关关系，发现范围更广，但更容易产生偶然关联、难以固定解释边界，
不适合作为当前报告事实层。

## 4. 架构与数据流

V22-071 是 V22-070 之后的独立确定性阶段，不修改已冻结的单来源结果。

输入包含：

- V22-070 已严格验证的 `FirstPartyFindingsInput`；
- 对应 `FirstPartyFindingsResult`；
- Case、parent report、统一 evaluated time、快照身份；
- V22-070 输入摘要和结果 checksum。

处理顺序：

1. 重新运行纯函数 V22-070 引擎，并对比其字节级结果；同时验证输入/结果版本、checksum、
   Case、parent、快照和 Evidence 引用。
2. 把 GSC、GA4 和可选官方 GBP 构造成只读 `CrossSourceView`。
3. 页面规范化器只建立可证明的 GSC URL ↔ GA4 landing path 对应关系。
4. 时间对齐器确定共同覆盖区间、完整自然周和日期资格。
5. 双源规则矩阵分别运行页面、整体、周序列和冲突规则。
6. 合并 V22-070 Evidence 与本阶段新增 Evidence，执行双来源、健康和 scope 验证。
7. 应用确定性限制与排序，构建并验证 checkpoint。

组件保持单一职责：

- `cross_source_findings_models.py`：严格输入、目标、评估、组合资格与结果合同；
- `cross_source_pages.py`：页面解析、规范化、唯一匹配和安全合并；
- `cross_source_time.py`：覆盖窗口、完整周和 Spearman；
- `cross_source_gsc_ga4.py`：页面、整体和周序列规则；
- `cross_source_gbp.py`：官方 GBP 与 GSC/GA4 整体规则；
- `cross_source_findings.py`：验证、Evidence 合并、排序和结果入口；
- `cross_source_findings_stage.py`：摘要绑定和 checkpoint 执行。

## 5. 输入与来源资格

`CrossSourceFindingsInput` 必须绑定同一组 V22-070 对象。入口重新计算并验证：

1. `case_id`、`parent_report_id` 和 `evaluated_at` 完全一致。
2. 输入摘要、第一方结果 checksum 和规则集版本一致，且以同一可信输入重新执行 V22-070
   得到的结果与所提交结果字节级相同。
3. GSC、GA4 必需；GBP 仍为可选。
4. 每份快照的 schema、checksum、资源、覆盖期和健康摘要通过 V22-070 验证。
5. 第一方结果的每个 Evidence、Trace、Finding 和 evaluation 引用完整。

每个来源组合独立评估资格。业务规则要求该组合两侧均为 `eligible_for_business`。第三来源
不合格只写入组合限制。测量冲突规则也要求两侧数据结构有效且比较充分；来源完全缺失、过期
或不健康时不能把“未观察”写成冲突。

请求级身份、checksum、引用或版本错误使整个阶段失败。合法的数据缺失、限制或门槛不足只
改变对应 evaluation，不影响其他来源组合。

## 6. 页面身份与安全规范化

GSC 页面与 GA4 landing page 只按下列确定性规则关联：

1. GSC URL 主机必须等于 Case 确认域名或它的 `www` 形式；其他主机拒绝参与。
2. GA4 landing page 必须是以 `/` 开头的站内绝对路径；`(not set)`、完整外部 URL、控制字符、
   非法 percent encoding、反斜杠和 dot-segment 路径不参与。
3. 协议归一为 HTTPS，主机转小写并移除默认端口和 fragment。
4. 只移除明确跟踪参数：`utm_*`、`gclid`、`dclid`、`fbclid`、`gbraid`、`wbraid`；保留其他
   参数并按 key/value 稳定排序。
5. percent encoding 只解码 RFC unreserved 字符；路径大小写保持不变。
6. 根路径固定为 `/`，非根路径去除多余尾斜杠；不合并内部重复斜杠。
7. 不读取网页标题，不抓 redirect，不使用 canonical，不做 slug 或编辑距离匹配。

多个仅跟踪参数不同的原始行规范为同一页面时，可以在同一来源、同一周期内合并：计数相加，
比率和平均值使用对应计数加权，Evidence 保留全部组成行。若规范后出现不能证明口径一致的
冲突，该页面相关规则为 `not_checked`，原因固定为 `page_identity_ambiguous`。

Top rows 中目标只出现在单侧时不得补零。任一相关视图截断时原因是 `detail_truncated`；未截断
但缺少同周期可比行时原因是 `comparison_unavailable`。

## 7. 时间窗口与周序列

趋势规则先验证两来源的 current/previous 窗口：

- 每个窗口应为 90 天；
- 两来源对应窗口的结束日期差不超过 3 天；
- current 与 previous 口径在每个来源内部连续且不重叠；
- 只比较相同定义的相对变化，不直接相加不同来源数值。

日期级规则只在 GSC 与 GA4 之间运行。它把共同日期按 ISO 自然周聚合，并执行：

1. 从两侧共同区间移除 GA4 coverage end 前最后 2 天。
2. 丢弃共同区间首尾不完整周。
3. 要求至少 8 个完整周，每周两侧都存在观察；缺失日期不补零。
4. GSC 使用 weekly clicks，GA4 使用 weekly sessions。
5. 任一序列恒定、非有限或不足 8 周时为 `not_checked`。
6. 使用固定实现的 Spearman rank correlation；ties 采用平均 rank。
7. `rho >= 0.70` 触发共同波动业务 Finding；`rho <= -0.70` 触发测量一致性 Finding；其他为
   `not_triggered`。

结果只称为观察到的共同或相反波动，不输出 p-value，不使用“显著”“导致”“归因”“提升收入”
等超出证据的表述。

## 8. GSC ↔ GA4 规则目录

### 8.1 页面同向增长

- 安全规范为同一页面；
- 两周期两侧均存在该页面；
- GSC 上期 clicks 至少 10；
- GA4 当前和上期 sessions 均至少 20；
- GSC clicks 与 GA4 sessions 均增长至少 20%。

Finding 只陈述搜索点击与站内 sessions 在相邻两个 90 天窗口中共同增长，不说明增长原因。

### 8.2 页面同向下降

资格与增长规则相同，两侧均下降至少 20%。下降达到 50% 时可进入固定高严重度，但仍不推断
排名、内容、技术或竞争对手导致变化。

### 8.3 搜索机会被站内数据确认

- GSC 页面当前曝光至少 100、平均位置 4～20、CTR 不高于站点当前基线 80%；
- GA4 同一页面当前 sessions 至少 20；
- GA4 engagement rate 不高于站点基线 80%，或已配置 key event 且页面为零 key events，
  或页面 session key-event rate 不高于站点基线 50%。

Finding 陈述“该页面同时具有搜索可见度/点击效率缺口与站内互动或关键事件缺口”。它不称为
排名原因、转化原因或预计收益。

### 8.4 页面方向冲突

同一页面通过样本门槛后，GSC clicks 与 GA4 sessions 一侧增长至少 20%、另一侧下降至少
20%，生成测量一致性 Finding。固定限制说明 Google 产品口径、渠道构成、cookie/consent、
时区或页面身份都可能影响差异，当前证据不能选择具体原因。

### 8.5 整体同向趋势与冲突

使用 GSC totals clicks 与 GA4 totals sessions。GSC 上期 clicks 至少 10，GA4 两期 sessions
均至少 20；两侧同向达到 20% 触发业务 Finding，反向达到 20% 触发测量一致性 Finding。

### 8.6 完整周共同波动

使用第 7 节的 weekly Spearman 规则。它与 current/previous 趋势分别评估，不能用相关系数
替代 90 天变化门槛。

## 9. 官方 GBP 跨源规则目录

这些规则只接受官方 `gbp_sync_v1`，公开 SerpAPI GBP 永远不能进入。

### 9.1 GSC ↔ GBP 可见度趋势

- GSC totals impressions 与 GBP 四项 Search/Maps Desktop/Mobile impressions 分别计算相对
  变化；
- 两侧上期 impressions 均至少 50；
- 两侧同向达到 20% 触发业务 Finding；反向达到 20% 触发测量一致性 Finding。

Finding 只陈述两个 Google 产品中的可见度观察方向，不合并数值，也不声称排名变化。

### 9.2 GA4 ↔ GBP 行为趋势

- GA4 totals sessions 当前和上期均至少 20；
- GBP `CALL_CLICKS`、`WEBSITE_CLICKS`、`BUSINESS_DIRECTION_REQUESTS` 上期合计至少 10；
- 两侧同向达到 20% 触发业务 Finding；反向达到 20% 触发测量一致性 Finding。

Finding 使用“站内 sessions 与 GBP 客户行动共同变化”，不把 GBP 行动等同于 session、lead、
销售或收入，也不声称 GBP 造成 GA4 变化。

### 9.3 GBP 持久化限制

GBP 原始 Content 仅在现有 30 天保留期内参与内存计算。跨源结果只保留：

- `increase_20_to_49_percent`、`increase_50_plus_percent`、`decrease_20_to_49_percent`、
  `decrease_50_plus_percent` 等变化档位；
- `at_least_50_impressions`、`at_least_10_actions` 等样本门槛档位；
- 固定、不可逆的 impact 档位；
- snapshot、Evidence、规则和限制引用。

不得保存 GBP 精确数值、精确比例、关键词文本、关键词次数或可逆影响分。

## 10. 三态、冲突与文案边界

每个规则/目标组合只能产生一种状态：

- `triggered`：双来源条件成立，必须生成一个 Finding；
- `not_triggered`：两侧数据充分、可比，但未达到固定门槛；
- `not_checked`：来源、身份、健康、时间、页面、样本、字段或平台限制阻止判断。

`not_checked` 原因至少包含：`source_missing`、`source_unhealthy`、`source_expired`、
`window_misaligned`、`comparison_unavailable`、`insufficient_sample`、`detail_truncated`、
`page_identity_ambiguous`、`series_incomplete`、`series_constant`、`provider_limitation` 和
`output_limit`。

同向 Finding 属于业务事实；方向冲突属于 measurement。冲突 Finding 必须使用固定中性文案
和限制，不得把以下内容当作已证明事实：跟踪损坏、consent 丢失、排名变化、站点故障、GBP
配置错误、渠道迁移或业务转化下降。

固定文案不经过 LLM。每条跨源业务 Finding 必须包含“共同变化不证明因果或渠道归因”限制。

## 11. 合同、Evidence 与身份

新增内部模型：

- `CrossSourceFindingsInput`；
- `CrossSourceFindingTarget`，支持 `page`、`aggregate` 和 `weekly_series`；
- `CrossSourceRuleEvaluation`；
- `CrossSourcePairAssessment`；
- `CrossSourceFindingsLimits`；
- `CrossSourceFindingsResult`，版本为 `cross_source_findings_v1`。

每条触发 Finding 必须：

- 引用恰好一个来源组合中的至少两个 Evidence IDs；
- Evidence source set 恰好等于 evaluation 的两个来源；
- 两侧 Evidence 均来自绑定的当前快照且 business Finding 的 Evidence 均 healthy；
- Finding 与 evaluation 的 rule、version、evidence、comparators 一致；
- 具有稳定 scope、限制和 change condition。

第一方 Evidence 可以复用；缺少跨源判断所需字段时新增来源内 Evidence。合并后 Evidence 与
Trace 必须一一对应，重复 ID 内容冲突时安全失败。

Finding ID 使用 Case、跨源规则集版本、rule/version、有序来源组合、规范化目标、快照 IDs、
Evidence 和 comparator IDs。它不包含 statement、遍历顺序或输入行顺序。

来源组合顺序固定为 `gsc_ga4`、`gsc_gbp`、`ga4_gbp`；组合内部来源顺序固定为
GSC → GA4 → GBP，不能由输入顺序决定。

## 12. 排序与输出限制

规则先产生完整合法 evaluations，再应用输出限制。默认限制：

- 每个页面业务规则族最多 5 条 triggered Findings；
- 每个来源组合最多 2 条整体业务 Findings；
- 全部 measurement consistency Findings 最多 5 条；
- Findings 总上限 25；
- evaluations、Evidence 和结果字节数分别有固定硬上限。

页面业务 Finding 按规则优先级、严重度、固定影响档位、规范化页面和 Finding ID 排序。整体
规则先于页面规则，共同下降先于共同增长，measurement 独立排序。被上限排除的合法目标保留
evaluation，状态改为 `not_checked/output_limit`，Finding ID 清空，不产生孤立 Finding。

固定严重度与 impact 档位为：两侧均下降至少 50% 时 `high/100`；搜索机会同时满足互动与
关键事件两个 GA4 缺口时 `high/100`；其他同向业务事实为 `medium/50`；测量一致性冲突为
`medium/20`。不得保存由 GBP 精确次数或精确比例直接计算的排序分。

相同输入、行顺序变化或来源顺序变化必须生成字节级相同结果。

## 13. Checkpoint 与错误处理

checkpoint 输入摘要包含：stage version、cross-source ruleset version、Case、parent、快照摘要、
V22-070 输入摘要、V22-070 result checksum 和本阶段限制。checkpoint 只保存验证后的跨源结果、
摘要、结果 checksum 和版本，不保存可信输入或 GBP raw Content。

命中 checkpoint 时重新验证 job、输入摘要、规则集、结果模型、结果 checksum、双来源引用和
大小。版本变化必须形成新 key；损坏、错 job 或引用不一致安全失败，不回退到半成品。

固定请求级错误覆盖：输入无效、V22-070 parent/result 不一致、snapshot/binding 不一致、页面
身份冲突、Evidence 引用错误、Finding ID 冲突、checkpoint 损坏和资源上限。只有 checkpoint
存储临时失败可重试。对外错误不得包含 SQL、token、OAuth subject、provider body、GBP Content
或原始异常。

## 14. 测试策略

### 14.1 页面与时间单元测试

- 根域/`www`、协议、默认端口、尾斜杠、fragment、跟踪参数和业务 query 参数；
- 大小写路径、非法 percent、反斜杠、dot segment、外部域名、`(not set)` 和归一后冲突；
- 3 天时间容差边界、4 天拒绝、GA4 最近 2 天剔除、完整周截断和共同区间；
- 8 周边界、7 周不足、ties、恒定序列，以及 Spearman `0.70/-0.70` 边界。

### 14.2 规则测试

- 每条规则的 20% 以下、等于和超过门槛；
- 同向增长、同向下降、方向冲突、零分母、样本不足和比较期缺失；
- 页面机会的 engagement、零 key events 和 relative key-event rate 三条分支；
- 无 GBP、GBP 过期、不健康、公开 GBP 不可冒充官方 GBP；
- GBP 精确值、比例、关键词和可逆 impact 零泄漏。

### 14.3 不变量和集成测试

- 输入/来源/行顺序变化不改变输出；重复运行字节级一致；
- triggered 与 Finding 一对一，其他状态无 Finding；
- 每条跨源 Finding 的 Evidence 来源集合恰好为声明的来源对；
- 第三来源不合格只成为限制，不污染已合格来源对；
- 不健康 Evidence 不能进入业务 Finding；
- 输出 caps、ID 冲突、未知引用、超大输入/结果和 checkpoint 损坏；
- 日志、错误和 checkpoint 不含敏感内容。

所有测试使用 fake repository、脱敏 fixture 和内存 checkpoint；不调用 Google、SerpAPI、
Supabase 正式库、Vercel、Railway 或任何付费接口。

## 15. 发布、回滚与完成标准

发布步骤：

1. 完成定向与全量后端测试；
2. 运行前端完整回归、TypeScript 和生产构建，证明共享合同未受影响；
3. 推送 `SearchTrust-RD` 并等待 Railway API/正式 Worker 自动部署；
4. 检查健康端点、Worker 队列和近期错误日志；
5. 保持 Verified Generation 与 GSC/GA4/GBP 同步开关关闭。

本轮没有数据库或前端运行时代码变更，不执行 Supabase migration，也不要求 Vercel 新部署。
不发起真实 OAuth、Google、SerpAPI、付款或 Verified report 请求。

回滚时首先保持 Verified Generation 关闭，再回滚 Railway 代码。V22-070 单来源结果和旧报告
保持不变。

V22-071 完成标准是：双源资格、页面规范化、窗口/周序列、全部规则、三态、双来源 Evidence、
确定性限制、checkpoint、自动化测试、文档及关闭状态 Railway 发布全部通过。它不能宣称完成
重新优先级、版本差异或最终 Verified Client Action Plan。
