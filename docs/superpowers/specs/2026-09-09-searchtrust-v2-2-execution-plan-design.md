# SearchTrust v2.2 — V22-074 执行基线、路线图与 Verified 报告设计

日期：2026-09-09

范围：V22-074。

## 1. 目标

V22-074 把 V22-072 已重新排序的三项行动和 V22-073 已验证的版本差异转换为可执行、
可验收的 Verified Client Action Plan，并组装最终 `verified_execution` ReportV22。

每项行动必须回答：当前基线是什么、最低成功标准是什么、是否需要保护指标、何时复查、
依赖什么前置条件。报告不得为了填满数字而使用无关指标，也不得承诺排名、流量或收入结果。

## 2. 已确认决策

- 每项行动固定一个主要成功指标，只有存在真实回归风险时才增加保护指标。
- 不同类型行动使用不同成功规则，不使用统一提升百分比。
- 最低成功标准是对应规则在新的合格 Evidence 上不再触发。
- GSC/GA4 测量修复是强制门禁；通过前后续业务行动不能进入正式效果验收。
- 官方 GBP Performance 缺失时使用公开 GBP 与网站字段的结构性匹配基线，不阻断
  Verified Core，也不推测官方经营数据。
- GSC、GA4 可展示合格 Evidence 中的实际数值；官方 GBP Performance 只展示合规档位，
  公开 GBP 只展示字段匹配状态。
- 数据健康且无测量门禁时，三项行动分别进入 1–30、31–60、61–90 天，按优先级和依赖
  严格推进，不默认并行。
- 客户文案使用版本化固定模板，不在本阶段调用在线 LLM。

## 3. 架构与隔离边界

V22-074 由三个独立单元组成。

### 3.1 `v22_execution_plan_v1`

纯确定性执行计划生成器，负责：

1. 输入、checksum 和 Case/parent/time 绑定；
2. V22-072、V22-073 重算校验；
3. 当前 Finding/Evidence 索引；
4. 每项行动的基线、主要指标、保护指标与门禁；
5. 30/60/90 路线图；
6. 引用、隐私、资源上限和 checkpoint。

### 3.2 Verified 行动呈现适配器

把 V22-072 的 `RankedVerifiedAction` 转换为最终 `TopAction`：

- 原公开行动保留冻结行动目录中的目标、步骤、规格、资产、依赖、owner 和完成定义；
- 新进入前三的公开行动使用同一冻结目录的确定性文案；
- `restore_verified_measurement` 和 `review_measurement_consistency` 使用专用固定模板；
- V22-074 指标替换公开阶段的占位验证指标；
- 不改变 V22-072 的 action ID 或顺序。

### 3.3 Verified Report 组装器

从验证后的阶段结果重新构造 `ReportV22(report_type="verified_execution")`。它不会复制父报告
后原地修改，也不会重新分类版本差异。

本阶段不读写数据库、不调用 Google、SerpAPI、OAuth、付款、Dify 或其他 LLM，不增加
公开 API 或前端页面。任务编排和按钮开放不属于本阶段。

## 4. 输入合同

`ExecutionPlanBuildInput` 固定包含：

- `case_id`、`parent_report_id`、`verified_report_id`；
- `evaluated_at`、`planning_date`；
- V22-072 输入、结果及各自 checksum；
- V22-073 输入、结果及各自 checksum；
- `copy_model_version`，固定为确定性 Verified 模板版本；
- 版本化资源上限。

绑定规则：

- V22-073 内的 parent report 是唯一父报告来源；
- V22-072 与 V22-073 的 Case、parent、evaluated_at 必须完全相同；
- V22-073 必须准确引用所提供的 V22-072 输入/结果 checksum；
- `verified_report_id` 不得等于 Case 或 parent report ID；
- `planning_date` 必须等于 V22-072 已验证的 planning date；
- 输出报告 generated time 使用 `evaluated_at`，不读取系统当前时间；
- 所有 checksum 在任何 checkpoint 查询前重新验证。

阶段随后重算 V22-072 和 V22-073，并对规范化结果逐字节比较。重新签名后的篡改仍会失败。

## 5. 当前 Findings 与 Evidence

最终 Findings 按来源合并：

1. 公开 Findings；
2. V22-070 第一方 Findings；
3. V22-071 跨源 Findings。

最终 Evidence 按来源合并：

1. 公开 Evidence；
2. V22-071 Evidence 集合，后者已包含 V22-070 Evidence。

相同 ID 和相同规范化内容只保留一份；相同 ID 对应不同内容时返回 `ID_CONFLICT`。普通
FindingId/EvidenceId 在最终报告命名空间内必须全局唯一。

不健康、不匹配、过期或不完整来源的 Evidence 可以用于说明测量状态，但不能成为业务成功
指标。每个指标必须引用当前索引内的 Evidence 或引用明确的公开结构 Finding；不允许引用
父报告之外的隐藏值。

## 6. 指标合同

每项 `ExecutableAction` 包含：

- 原 V22-072 action identity、sequence、kind 和 Finding refs；
- 一个 `primary_metric`；
- 零个或一个 `guardrail_metric`；
- `review_date`；
- `execution_gate`；
- 固定 `metric_rule_id`、`metric_rule_version`；
- metric selection audit。

每个 `ExecutionMetric` 包含：

- `metric_key` 和角色 `primary/guardrail`；
- 基线类型 `exact_value/band/structural_state/source_readiness`；
- 用户可显示的 baseline 文本；
- 成功条件文本；
- source types；
- Evidence IDs、Comparator IDs 和 Finding refs；
- 最低样本要求；
- 固定 success evaluator；
- limitations。

不在结果中保存通用表达式、可执行代码或调用方提供的自由文本条件。

## 7. 指标选择原则

指标选择严格按以下顺序：

1. V22-072 中与行动目标严格关联的 `supports`、`reduces_urgency` 或 measurement relation；
2. 同一 URL、查询、聚合目标和同一来源的合格 V22-070/071 Evidence；
3. 公开 Finding 和冻结行动目录中的结构性复查指标。

不能从 statement 关键词推断指标，不能把候选级关系扩散到未明确列出的目标，不能因为某
来源有数据就把它附加到所有行动。一个指标必须能解释其 action target 和 Finding 关系。

每项行动只选一个 primary。只有既有成功指标可能因分母/样本下降而“假改善”，或规则目录
明确要求保护时才选 guardrail；不得增加第二个普通结果指标冒充 guardrail。

## 8. 结构性行动规则

### 8.1 网站访问和索引

- baseline：保存的 HTTP 或明确 noindex Finding；
- primary：目标 URL 新合格快照不再触发相同规则；
- guardrail：无，除非该行动同时具有严格目标级 GSC Evidence；
- 不把索引恢复描述为排名提升。

### 8.2 重复标题

- baseline：保存样本中的重复规范化 title group；
- primary：所有目标 final URLs 的新快照不再触发重复标题 Finding；
- guardrail：仅在同页面存在合格 GSC Evidence 时使用曝光样本保护；
- 不声称标题差异必然提高 CTR。

### 8.3 市场可见性

- baseline：相同查询/地点/设备/result type 下客户域名未出现或落后于已确认竞品；
- primary：保存新的同口径 SERP 样本并重新评估原 Finding；
- 如果有严格查询级 GSC Evidence，可把 GSC 有效曝光样本作为 guardrail；
- 不设置承诺名次，也不推断排名原因。

### 8.4 页面类型差异

- baseline：客户样本缺少目标 page type，多个确认竞品样本存在；
- primary：新库存支持已批准的新建页面或明确的 no-build 决策，并完成原 Finding 重评估；
- 只有同一目标存在合格 GSC/GA4 Evidence 时才添加样本 guardrail；
- 不把竞品页面存在视为客户必须复制的证明。

### 8.5 公开 GBP 对齐

- baseline：`exact_match/partial_match/mismatch/site_missing/gbp_missing/both_missing`；
- primary：新网站和公开 GBP 快照达到 `exact_match`；
- 两侧都缺失仍是错误，不是匹配；
- 官方 GBP Performance 不可用时不加 Performance 指标；
- 官方 GBP Performance 合格时只允许一个合规档位 guardrail，要求不下降。

## 9. GSC 和 GA4 规则

### 9.1 GSC CTR 机会

- baseline 显示目标当前 CTR、站点当前 CTR baseline 及覆盖时间；
- primary 成功条件：目标不再满足“排名 4–20 且 CTR 至少低于站点 baseline 20%”；
- guardrail：目标 impressions 仍达到目录最低样本，并且不是通过丢失有效曝光造成 CTR 假改善；
- 不把 CTR 改善表述为收入或排名保证。

### 9.2 GSC 页面/查询趋势

- primary 使用对应规则的 current/previous 可比窗口；
- 成功条件是原 decline/opportunity 规则不再触发；
- guardrail 保护原规则的最低 impressions/clicks 样本；
- 查询隐私过滤写入 limitation，不推断缺失查询为零。

### 9.3 GA4 参与度

- baseline 显示目标 engagement rate、站点 baseline 和 sessions；
- primary 成功条件：不再低于站点 baseline 20% 且原规则不再触发；
- guardrail：目标仍有至少 20 sessions；
- sessions 不足时结果为不可验收，不是成功。

### 9.4 GA4 key event

- baseline 显示目标 key events/key-event rate、站点 baseline 和 sessions；
- primary 成功条件：不再满足“无 key event 或 key-event rate 至少低于站点 baseline 50%”；
- guardrail：目标仍有至少 20 sessions；
- GA4 未配置关键事件时生成测量修复，不把零事件当成业务表现结论。

## 10. 官方 GBP Performance 规则

官方 GBP 的精确 Performance 数值和关键词不得进入 V22-074 结果、checkpoint、日志或最终
报告。生成器只接受 V22-070 已持久化的不可逆档位 Evidence。

- primary 或 guardrail baseline 只能显示固定档位名称；
- success condition 只能是档位改善、保持或相应规则不再触发；
- 档位相同不标记为增长；
- 缺失/过期/不合格官方 GBP 不阻断 Verified Core；
- 不允许从 SerpAPI 公开字段推测官方曝光、搜索词、通话或网站点击。

## 11. 测量行动规则

### 11.1 `restore_verified_measurement`

- 必须位于 sequence 1；
- primary baseline 为 GSC/GA4 的健康、身份、时间窗、比较和关键指标问题代码；
- success condition 为新绑定快照全部满足所需健康和比较门禁；
- 不设置流量、排名、CTR、转化率或收入目标；
- execution gate 为 `blocked_until_measurement_ready`；
- sequence 2、3 行动标记 `waiting_for_action_1`，在行动 1 通过前不可正式验收。

### 11.2 `review_measurement_consistency`

- primary baseline 为同一可比窗口的跨源方向冲突；
- success condition 为新可比快照不再触发冲突，或诚实记录持续不可比较的限制；
- 不宣布 GSC、GA4 或 GBP 任一来源错误；
- 它是普通候选，不自动阻断其他来源健康的业务行动。

## 12. 路线图和依赖

路线图固定三个阶段：

1. `days_1_30`：sequence 1；
2. `days_31_60`：sequence 2；
3. `days_61_90`：sequence 3。

每个阶段只包含一个 action ID。阶段 objective 来自版本化行动模板，不使用 LLM。exit
criteria 是该行动 definition of done、primary success condition、guardrail 条件和必要 gate
的稳定去重集合。

公开行动依赖只保留仍在最终三项中的依赖，并必须出现在较早阶段。V22-072 已重新应用依赖，
V22-074 再次验证而不自行重排。任何自依赖、未知依赖或晚阶段依赖早阶段的逆序关系均失败。

普通健康路径中，前阶段未达到完成条件时下一阶段可以准备资产，但不能标记为进入正式验收。
测量修复路径中门禁更严格：行动 1 未通过时，行动 2、3 不能用当前数据判断效果。

## 13. Verified 行动呈现

公开行动从 V22-072 的 `public_action` 复制冻结的非文案字段，并使用固定呈现目录生成：

- `why_now`：引用行动所绑定 Finding 的已验证状态，不复述原始数值；
- `client_facing_explanation`：说明要做什么和如何验收；
- exact targets：由结构化 target 确定性渲染；
- validation metrics：完全使用 V22-074 指标，不沿用公开占位指标。

父报告中相同行动的原安全文案可以在事实仍一致时复用；新进入前三的公开行动使用冻结目录
模板。测量行动使用专用步骤、规格、owner、effort、definition of done 和安全说明。

所有文本均受长度、固定目录版本和禁止短语扫描限制。不得出现排名保证、流量保证、收入
保证、来源错误归因或 GBP 精确 Performance。

## 14. 最终 ReportV22 组装

最终报告：

- schema `2.2.0`；
- `report_type="verified_execution"`；
- `version_number=parent.version_number + 1`；
- `parent_report_id` 指向不可变获客报告；
- `generated_at=evaluated_at`；
- ruleset/copy model version 使用 V22-074 固定版本。

报告内容：

- identity/case context/market/site/competitor 公共事实从已校验父报告保留；
- data coverage 根据 V22-070 source assessments 和绑定 snapshot 重建；
- first-party performance 只从合格持久 Evidence 构造；
- executive decision 使用 V22-072 core public Finding 和 sequence 1 业务说明；
- eight layers 保留公开层级评估，不把未定义层级映射的新 Finding 强塞入层；
- findings/evidence 使用第 5 节当前索引；
- top actions 使用第 13 节输出；
- roadmap 使用第 12 节输出；
- version diff 原样使用 V22-073 验证结果；
- limitations 合并父限制、source assessments、Finding limitations 和指标 limitations 后稳定去重。

`full_evidence_coverage` 只有 GSC、GA4、官方 GBP 均健康、matched 且快照合格时为 true。
官方 GBP 缺失时仍可生成 Verified Core，但不得显示 Full Evidence。

## 15. 输出合同

`ExecutionPlanBuildResult` 固定包含：

- schema、规则、指标目录、文案目录版本；
- Case、parent、verified report、evaluation/planning 时间；
- 所有上游 checksum；
- 三项 `ExecutableAction`；
- 三阶段 execution roadmap；
- 指标选择、门禁和引用审计；
- 完整最终 `ReportV22`；
- 结果限制摘要。

结果必须恰好三项行动、三阶段、每项一个 primary、最多一个 guardrail。报告 action IDs、顺序、
Finding refs、VersionDiff、roadmap 和 client summary 必须彼此一致。

## 16. 确定性错误

固定错误代码：

- `INPUT_INVALID`；
- `BINDING_INVALID`；
- `CHECKSUM_MISMATCH`；
- `UPSTREAM_MISMATCH`；
- `BASELINE_UNAVAILABLE`；
- `METRIC_TARGET_MISMATCH`；
- `REFERENCE_INVALID`；
- `ID_CONFLICT`；
- `DEPENDENCY_INVALID`；
- `PRIVACY_VIOLATION`；
- `LIMIT_EXCEEDED`。

错误只返回固定安全文案，不回显父报告、Evidence 值、Google resource ID、关键词、GBP
Performance 或 provider payload。失败时不返回部分行动或部分报告。

`BASELINE_UNAVAILABLE` 只用于规则明确要求第一方基线但没有结构性 fallback 的情况。普通
公开行动始终可以使用冻结结构 baseline；不得因为缺数字而错误失败。

## 17. 资源上限和隐私

默认上限：

- Findings 25,000；
- Evidence 50,000；
- relations 30,000；
- 每项指标 Evidence 10,000；
- 每项行动 Finding refs 1,000；
- limitations 5,000；
- audit 25,000；
- 输出 25 MB。

超限确定性失败，不截断、不抽样。结果、checkpoint 和日志禁止包含 OAuth token、raw Content、
GBP 精确 Performance、GBP keywords、未持久化 provider payload 或父报告完整副本。

## 18. Checkpoint

checkpoint schema 为 `execution_plan_checkpoint_v1`，摘要绑定：

- stage、规则、指标目录和文案目录版本；
- Case、parent、verified report、evaluation/planning 时间；
- V22-072 输入/结果 checksum；
- V22-073 输入/结果 checksum；
- copy model version；
- 资源上限。

查询 checkpoint 前先验证所有 supplied checksum。checkpoint 只存最终
`ExecutionPlanBuildResult`，命中后重新验证 job、digest、版本、result checksum、隐私、引用
闭合和字节上限。

## 19. 测试计划

### 19.1 绑定和重算

- 错 Case、parent、verified report ID、evaluation/planning time；
- V22-072/073 checksum 不一致；
- 重新签名篡改 action、Finding、Evidence、diff 或排序；
- parent version 和 ReportV22 不可变性。

### 19.2 指标选择

- 每项恰好一个 primary、最多一个 guardrail；
- 严格 URL、查询、聚合和 measurement target 归属；
- 结构 fallback 不需要无关数字；
- GSC CTR/趋势、GA4 参与/key event、GBP 档位和公开对齐；
- 最低样本不足时不可验收而非成功；
- 不健康 Evidence 不贡献业务基线。

### 19.3 测量边界

- GSC 无数据、GA4 无事件、身份 mismatch、不可比窗口；
- 测量修复固定第一并门禁后两项；
- 跨源一致性检查不错误升级为强制阻断；
- 官方 GBP 缺失仍生成结构性 GBP action；
- GBP 合格时只出现固定档位。

### 19.4 路线图和报告

- 普通三行动按 30/60/90 严格一阶段一项；
- 依赖顺序、退出条件、review date 和 client summary 一致；
- Confirmed、Refined、Reprioritized、New 与最终 Findings/Evidence/Actions 引用闭合；
- full evidence 与 Verified Core 边界；
- 新进入前三和 measurement action 的确定性呈现。

### 19.5 确定性、隐私和 checkpoint

- 无序输入换序后输出字节一致；
- GBP exact value、keywords、raw Content、token 和禁止承诺扫描；
- 各资源上限；
- checkpoint 复用、损坏、错误 job/input 和版本变化；
- checkpoint 只含最终结果。

### 19.6 回归

- ReportV22 合同/组装、公开 Findings/行动、V22-070、V22-071、V22-072、V22-073；
- 后端完整回归；
- 前端合同生成一致性、类型、完整测试和生产构建。

## 20. 非目标与发布

本阶段不：

- 修改数据库或创建生成任务编排；
- 新增/修改前端页面和按钮；
- 调用真实 Google、SerpAPI、OAuth、付款或 LLM；
- 开放 Google sync 或 Verified Generation；
- 评估行动实施后的真实效果；
- 生成排名、流量或收入承诺。

实现作为默认不可达的 Railway 后端内部能力发布。正式同步变量继续缺失/关闭；现有公开
获客分析功能保持原状。

## 21. 验收标准

V22-074 完成需同时满足：

- V22-072/073 可重算且调用方不能篡改行动、差异或指标依据；
- 三项行动各有一个可靠 primary 和最多一个必要 guardrail；
- 成功标准对应原规则不再触发并保留样本保护；
- GSC/GA4 实际值可追溯，GBP 只显示合规档位或公开匹配状态；
- 测量修复第一且对后续验收形成真实门禁；
- 普通路线严格一阶段一行动并满足依赖；
- 最终 verified_execution ReportV22 引用闭合、版本正确且父报告不可变；
- 输出、JSON 和 checkpoint 幂等；
- 隐私、上限、定向和全量回归通过；
- 无数据库/前端变更，Railway 关闭能力发布成功。
