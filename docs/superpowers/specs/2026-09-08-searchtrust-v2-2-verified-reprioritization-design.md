# SearchTrust v2.2 — V22-072 Verified 重新优先级设计

日期：2026-09-08

范围：V22-072。

## 1. 目标

V22-072 把已冻结的获客报告 Findings/行动计划、V22-070 单来源 Findings 和
V22-071 跨来源 Findings 放入一个独立、确定性的重排阶段，重新选出恰好三项
行动并确定核心业务问题。新证据只能在严格目标匹配后增强或降低原公开问题的
优先级；不允许模型猜测关联，不允许不健康数据提高 verified confidence。

完成本阶段不等于完成 Verified Client Action Plan。V22-073 继续生成版本差异，
V22-074 继续生成执行基线和路线图。

## 2. 已确认决策

- 采用混合行动边界：业务 Findings 只为现有公开行动候选加权或降级；测量 Findings
  可生成受控的测量修复/一致性检查行动。
- 采用严格目标匹配，不使用宽泛规则类别或全局分数扩散。
- 只有会阻断可靠判断的 GSC/GA4 测量问题才强制第一；方向冲突是普通测量候选。
- 核心问题仍由入选业务行动的原始公开 Finding 表达；新 Findings 用于证实、增强或
  削弱优先级，不用流量变化文案替代业务问题。
- 同目标的可靠增长可使修复行动降低一个验证等级，但不能覆盖 HTTP 错误、noindex、
  身份缺失等硬性公开事实。
- 采用独立重排阶段，不改变已上线的公开 Findings/行动、V22-070 或 V22-071 行为。

## 3. 架构与边界

新阶段命名为 `v22_verified_reprioritization_v1`。它读取上游已验证结果，不读数据库、
不调用真实 provider、不修改上游对象、不调用 LLM。

阶段分为六个隔离组件：

1. 上游绑定与重算校验；
2. 公开行动候选重建；
3. 新 Finding 目标映射与关系分类；
4. 测量阻断器和测量行动生成；
5. 离散等级排序、三项选择和核心问题选择；
6. 引用完整性、资源上限和 checkpoint。

公开行动模块保持冻结。重排阶段使用原行动计划的 `selection_audit`、原公开 Findings
和固定行动目录重建全部候选，因此原未入选候选可因新证据进入前三。

## 4. 输入合同

`VerifiedReprioritizationInput` 固定包含：

- `case_id`、`parent_report_id`、`evaluated_at` 和 `planning_date`；
- `public_findings_input`、`public_findings_result` 与各自 checksum；
- `public_action_plan`、生成它时使用的 `public_action_limits` 与 plan checksum；
- `first_party_input`、`first_party_result` 与各自 checksum；
- `cross_source_normalized_domain`、`cross_source_limits`、`cross_source_result` 与其 checksum；
- 版本化资源上限。

所有结果必须属于同一 Case、同一 parent report 和同一评估时点。阶段先重新运行现有公开
Findings/行动生成器、V22-070 与 V22-071，再对规范化输出做逐字节比较。V22-071 输入
由本请求中已验证的 first-party 输入/结果、normalized domain 和 cross-source limits 重建，
不再接受一份重复的含 raw Content 嵌套输入。任意不一致为确定性拒绝，不使用调用方
提供的分数或关联。

## 5. 输出合同

`VerifiedReprioritizationResult` 使用 `verified_reprioritization_v1` schema，包含：

- 上游绑定 checksum 和规则/行动目录版本；
- 恰好三条 `RankedVerifiedAction`，序号固定为 1、2、3；
- `core_problem_finding_id`，必须是排名最高业务行动的公开 anchor Finding；
- 所有公开行动候选和测量候选的 `VerifiedSelectionAudit`；
- 全部 `FindingRelation`，包含 `supports`、`reduces_urgency`、
  `measurement_conflict` 或 `unmatched`；
- 未被任何行动消费的限定 Finding 引用及固定原因。

Finding 引用不只使用 ID，而使用以下限定形式：

- `origin_stage`: `public_findings` / `first_party_findings` / `cross_source_findings`；
- `ruleset_version`；
- `finding_id`。

这防止不同规则阶段偶然生成相同 ID 时出现引用混淆。

## 6. 公开行动候选重建

阶段不仅重排原三项。它从已验证的 `selection_audit` 恢复所有公开候选，并通过
原公开 Finding 和固定模板目录重建目标、步骤、完成标准和稳定行动 ID。原来未入选的候选
可被选中。

重建后的公开行动保持原 `action_id`、`template_key`、Finding 集合、精确目标、步骤、所有者、
工作量和完成标准。本阶段只重建入选顺序、保留的技术依赖和基于新序号的 30/60/90 天
复核日期。

## 7. 目标映射

映射使用版本化 allowlist，不读 Finding 自然语言 statement。

### 7.1 页面

- 页面级新 Finding 只能匹配具有同一规范化 URL 目标的公开行动候选。
- URL 使用 V22-071 已冻结的 Case-host 页面身份规则。
- 不做标题、page type、canonical、redirect 或模糊匹配。

### 7.2 查询

- 查询级新 Finding 只能匹配包含同一规范化查询的 `review_market_visibility`。
- 规范化仅做 Unicode NFKC、外端空白移除、内部空白折叠和 casefold；不做近义词或词干匹配。

### 7.3 聚合

- GSC、GA4 以及包含官方 GBP 的跨来源聚合业务趋势只能匹配
  `review_market_visibility`。
- 聚合信号不匹配单页技术、标题、page type 或公开 GBP 字段对齐行动。

### 7.4 无匹配

page type、无明确 URL/查询的 profile 信号以及不在 allowlist 中的规则统一记录为
`unmatched`，不影响排序。未知规则不会被默默忽略：已声明支持但未注册的规则使请求
确定性失败；明确列入“仅审计”的规则才能生成 `unmatched`。

## 8. 关系分类

每条合格新 Finding 对每个候选最多形成一条关系：

- `supports`：机会、下降、低互动、低转化或同向下降，增强同目标修复的必要性；
- `reduces_urgency`：同目标的可靠增长降低一个验证等级；
- `measurement_conflict`：跨来源反向或强逆相关，只进入测量一致性候选；
- `unmatched`：无严格对应目标，只记录审计。

硬性公开问题 `site_http_error`、`site_explicit_noindex` 和固定身份缺失/不匹配不接受
`reduces_urgency`。新证据不能反驳其已保存的直接公开事实。

## 9. 排序模型

排序不使用原始流量数值或 Findings 数量累加，而使用确定性字典序元组。

### 9.1 验证等级

业务候选使用四档，数值仅作确定性比较：

1. `cross_source_supported`：有至少一条严格匹配的 V22-071 业务支持；
2. `single_source_supported`：没有跨源支持，但有至少一条严格匹配的 V22-070 业务支持；
3. `public_only`：只有原公开证据；
4. `reduced_by_growth`：原本可用的最高等级因同目标可靠增长下降一档。

增长只下降一档，不叠加。同一来源族的多条支持只保留最强一条作为排序依据，其余仍保留
在审计关系中。

### 9.2 字典序

普通业务候选按以下顺序排列：

1. 验证等级；
2. 原 anchor Finding 严重度；
3. 原 anchor Finding 置信度；
4. 原 anchor Finding 事实/推断等级；
5. 已封顶的 URL、查询和 Finding 影响范围；
6. 原模板顺序；
7. 规范化目标与稳定 action ID。

这保留原排序的可解释性，同时让严格匹配的 verified 证据真正改变优先级。
为了与业务候选共用一个确定性顺序，普通 `review_measurement_consistency` 位于
`single_source_supported` 业务候选之后、`public_only` 业务候选之前。它因此可以入选，
但不会超过任何有合格单源或跨源支持的业务行动。

## 10. 测量行动

### 10.1 强制阻断修复

最多生成一条 `restore_verified_measurement` 行动。它仅合并以下必需来源问题：

- GSC 或 GA4 不健康、过期、身份未匹配；
- 必需的 GSC/GA4 比较数据不可用；
- 用于已声明 verified 规则的关键 GSC/GA4 指标被 provider 限制。

官方 GBP 是可选增强，因此未连接、缺失、过期或不健康不强制第一。阻断行动若存在，
固定序号 1，再选两条业务行动。

该行动 ID 由模板版本、受影响来源和排序后问题代码生成，与输入遍历顺序无关。行动仅包含
固定修复步骤：确认授权/资源、修复采集或限制、重新同步、复核健康和可比窗口。

### 10.2 方向冲突

V22-071 `measurement` Finding 可按来源对合并为 `review_measurement_consistency` 普通候选。
它使用固定中等优先级和固定 20 分档，不强制第一、不增强业务候选，也不声明哪一个
来源错误。

## 11. 三项选择、依赖与核心问题

- 无阻断器时，从公开业务候选与普通测量一致性候选中选出三条。
- 有阻断器时，`restore_verified_measurement` 固定第一，两条最高业务候选填满余额；
  普通方向冲突不再占用名额。
- 不足三项时确定性失败，不返回部分计划。
- 公开行动之间重新应用原技术依赖规则。只保留入选集内依赖，依赖行动必须排在后续行动之前。
- 行动复核日期固定为 `planning_date + 30/60/90 天`。
- `core_problem_finding_id` 始终是入选的排名最高业务行动之原公开 anchor Finding。测量修复或
  方向冲突可以排在它之前，但不替代核心业务问题。

## 12. 健康、置信度和隐私

- V22-070 业务 Finding 只有其 source assessment 为 `eligible_for_business` 且所有引用 Evidence
  为 `healthy` 时才能支持/降级候选。
- V22-071 业务 Finding 必须来自 `eligible_for_business` 来源对，并恰好引用该两个健康来源。
- 任意不健康、过期、身份未匹配或受限 Evidence 不提高 verified 等级。
- 原始流量、用户、事件、GBP 精确 Performance、关键词和 raw Content 均不写入本阶段结果。
- GBP 关系仅引用 V22-070/071 已持久的不可逆档位和 Finding ID，不重新展开 raw Content。

## 13. 确定性、上限和 checkpoint

所有输入列表在处理前按限定 Finding 引用、候选 ID、目标和问题代码排序。相同语义输入的
JSON 字节、行动 ID、关系、排序和 checkpoint key 必须一致。

默认上限：

- 公开候选 1,000；
- 新 Findings 15,000；
- Finding 关系 30,000；
- 审计条目 2,000；
- 测量问题代码 100；
- 输出 20 MB。

超限是确定性失败，不做静默截断。

checkpoint schema 为 `verified_reprioritization_checkpoint_v1`。输入摘要绑定 stage version、排序规则版本、
行动目录版本、Case、parent、planning date 和所有上游 checksum。checkpoint 只存验证后的
`VerifiedReprioritizationResult`，不存上游 Findings/Evidence 全量、快照或 GBP raw Content。

## 14. 确定性错误

错误只返固定代码和安全文案，不回显 payload：

- `INPUT_INVALID`；
- `BINDING_INVALID`；
- `CHECKSUM_MISMATCH`；
- `UPSTREAM_MISMATCH`；
- `UNSUPPORTED_RULE`；
- `REFERENCE_INVALID`；
- `ID_CONFLICT`；
- `DEPENDENCY_INVALID`；
- `INSUFFICIENT_ACTIONS`；
- `LIMIT_EXCEEDED`。

任何错误不返部分行动或部分审计。

## 15. 测试计划

### 15.1 合同与绑定

- Case、parent、evaluation time、planning date 与上游 checksum；
- 公开行动、V22-070、V22-071 重算结果不一致；
- 重复/冲突 ID、未知引用、非有限值和超限。

### 15.2 目标映射

- 规范化 URL 精确命中与相似 URL 隔离；
- NFKC/空白/casefold 查询命中与近义查询隔离；
- 聚合信号只映射市场可见性；
- page type、profile 和未登记规则的 `unmatched`/拒绝边界。

### 15.3 排序

- 跨源支持高于单源支持，单源支持高于仅公开证据；
- 每个来源族最强支持封顶，重复 Findings 不堆分；
- 增长只降一级，不影响 HTTP/noindex/身份硬性问题；
- 相同分稳定 tie-break，不同输入顺序输出字节一致；
- 原未入选候选获得支持后进入前三。

### 15.4 测量与核心问题

- GSC/GA4 阻断修复固定第一；
- 官方 GBP 缺失/不健康不强制第一；
- 方向冲突使用普通测量候选，不冒充业务支持；
- 测量行动在前时，核心问题仍指向最高业务行动的原公开 Finding；
- 不足三项、依赖环和未入选依赖引用失败。

### 15.5 隐私、checkpoint 与回归

- 结果、checkpoint、日志和错误的 GBP 精确数值/关键词/raw Content 扫描；
- checkpoint 命中、版本变化、损坏、错误 job/input 和并发重试；
- 公开 Findings/Actions、V22-070、V22-071 定向回归；
- 后端完整回归，前端合同、类型、测试和生产构建回归。

## 16. 非目标与发布

本阶段不：

- 更改现有公开行动生成器的对外行为；
- 修改 prospect report 或旧报告；
- 生成 Confirmed/Reprioritized/Refined/Replaced/New 版本差异；
- 生成最终文案、指标基线、成功条件或 30/60/90 路线图；
- 调用 Google、SerpAPI、OAuth、付款或 LLM；
- 新增前端路由或数据库迁移。

实现作为默认不可达的 Railway 内部能力发布。Google 同步和 Verified Generation 开关保持
缺失/关闭，直到 V22-073、074 和真实账号联调完成。

## 17. 验收标准

V22-072 完成需同时满足：

- 所有上游绑定可重算且防篡改；
- 公开全候选可重建，原未入选候选可因严格新证据进入前三；
- 页面、查询、聚合映射严格、可审计且不猜测；
- 离散验证等级、增长降级、硬性保护和封顶顺序稳定；
- GSC/GA4 阻断修复和普通方向冲突行动边界正确；
- 输出恰好三项，依赖、日期、核心问题和全审计引用完整；
- 不健康来源不提高 verified confidence，GBP 隐私边界不回退；
- checkpoint、资源上限、定向回归和全量回归通过；
- 无数据库/前端变更，Railway 关闭能力发布成功。
