# SearchTrust v2.2 公共三项行动生成设计

日期：2026-09-02

状态：用户已批准完整书面设计；尚未开始业务代码实施。

范围：V22-033。根据已验证的 Prospect 公共 Findings，确定性生成恰好三个证据支持、目标不同、依赖有序的行动骨架，供后续 V22-034 文案合同消费。

主要仓库：SearchTrust-RD。本轮不修改 search-trust 前端运行时。

## 1. 目标与已确认产品口径

本轮实现一个纯确定性的公共行动规划器。相同 Findings、相同计划基准日期和相同规则版本必须得到字节稳定的同一结果。

用户已确认：

- 必须恰好生成三个行动；不能返回零、一、二或四个。
- 每个行动必须关联至少一个真实、已触发的 Finding。
- 只有零到两个合法行动工作包时整体失败，不能拆分、凑数或虚构第三项。
- 多个 Findings 指向同一修复目标时合并为一个工作包；无关问题不能混合。
- 先按严重度、置信度、事实类型、受影响范围和稳定键确定优先级，再按明确依赖排序。
- 行动 ID 不包含展示顺序；行动重新排序时身份不改变。
- 调用方必须传入计划基准日期；三个行动的复盘日期固定为基准日后 30、60、90 天，不能读取系统当天日期。
- V22-033 只生成行动骨架。`why_now` 和 `client_facing_explanation` 最终客户文案留给 V22-034；Dify 不能改变行动事实、引用、步骤、数字、依赖或排序。

## 2. 非目标

本轮不实现：

- Dify 调用、重试或文案输出校验；
- 最终 `TopAction`、Roadmap、ClientSummary 或完整 `ReportV22` 组装；
- verified execution 或 GSC、授权 GBP、GA4 行动；
- 新的 Finding 规则或对现有 Findings 的重新判断；
- Redis 检查点、Worker 接线、HTTP API、前端、PDF、生产开关或部署。

行动规划是低成本、无外部副作用的纯计算。本轮不单独增加 Redis 检查点；后续 Prospect 总执行器可以在更高层保存行动计划或最终报告阶段。

## 3. 方案选择

比较过三种选择方式：

1. 版本化行动目录 + 修复目标分组 + 确定性优先级。能合并重复问题、支持稳定身份和依赖审计，并让后续 Dify 只处理文案；用户已选择此方案。
2. 直接取优先级最高的三个 Findings，一条 Finding 对应一条 Action。实现简单，但可能三个 Action 都在修同一问题。
3. 固定网站、GBP、市场各保留一个名额。结构整齐，但可能忽略更严重的问题，并为了类别完整而选择低价值行动。

## 4. 模块与入口

建议新增：

- `app/report_v22/action_models.py`：严格内部输入、行动骨架、选择审计及限制模型。
- `app/report_v22/public_action_catalog.py`：规则版本到行动模板、修复家族、固定要求和依赖规则的唯一目录。
- `app/report_v22/actions.py`：输入验证、候选构建、分组、优先级、选择、依赖排序、稳定 ID 和最终复核。
- `app/report_v22/action_errors.py`：不含业务原文的稳定确定性错误。

纯入口：

```python
def build_public_action_plan(
    value: PublicActionPlanInput | dict,
) -> PublicActionPlan: ...
```

`PublicActionPlanInput` 包含：

- `findings_result: PublicFindingsResult`；
- `planning_date: date`；
- 可下调的数量和字节限制。

规划器不读取网络、文件、数据库、环境变量、系统日期或随机数，不分配 UUID，不调用 LLM，也不修改传入对象。

## 5. 内部输出合同

`PublicActionPlan` 至少包含：

- `schema_version = "public_action_plan_v1"`；
- `action_catalog_version = "v22_public_actions_v1"`；
- `source_ruleset_version = "v22_public_findings_v1"`；
- `planning_date`；
- `findings_checksum`；
- `actions`：恰好三个、有序且唯一的 `ActionSkeleton`；
- `selection_audit`：所有候选的分组键、锚点 Finding、优先级组成、选择状态和固定原因；
- `unselected_finding_ids`：未进入前三个行动的真实 Findings，便于审计但不暗示它们无价值。

`ActionSkeleton` 包含：

- `action_id`、`sequence`、`finding_ids`；
- `template_key`、`template_version`；
- 结构化 `exact_targets`；
- 有序 implementation steps；
- content/GBP/technical requirements；
- required client assets；
- dependencies；
- owner suggestion；
- effort bucket；
- definition of done；
- validation metrics；
- data sources；
- review date；
- `copy_requirements`：V22-034 可使用的 Finding IDs、允许的事实字段和必须保留的限制。

行动骨架不包含最终 `why_now` 或 `client_facing_explanation`。V22-034 完成后才把已验证文案与骨架组合为正式 `TopAction`。

### 5.1 结构化目标

为防止字符串文案产生新事实，骨架使用结构化 `ActionTarget`：

- `kind`: `url` / `query` / `page_type` / `gbp_field` / `site`；
- 对应的 URL、query、page_type 或 GBP 字段；
- 产生该目标的 Finding IDs。

目标只能来自 Finding 的 affected URLs、affected queries、已验证 RuleTarget 或行动目录的固定字段映射。规划器不能猜测新页面 URL、地点、服务、GBP 值或排名目标。

## 6. 规则到工作包的映射

首版目录只接受当前公共 Findings 规则及其批准版本。

| 工作包 | 支持规则 | 分组语义 |
| --- | --- | --- |
| 网站访问与索引恢复 | `site_http_error`、`site_explicit_noindex` | 合并全部受影响 URL；同一轮只形成一个技术恢复工作包 |
| 标题差异化 | `site_duplicate_title` | 合并已触发标题组及其 URL；形成一个标题工作包 |
| 市场可见性核查与改进 | `market_site_domain_unobserved`、`market_confirmed_competitors_ahead` | 合并已确认查询和同类市场观察；不推断排名原因 |
| 页面类型差距 | `competitor_sample_page_type_gap` | 每个 page_type 是独立工作包；不能把不同页面类型合成一个虚构页面 |
| 网站/GBP 一致性 | 四项 `gbp_*_alignment` | 名称、地址、电话、服务区域合并为一个一致性工作包，保留各字段目标 |

目录为每个工作包固定定义：

- 模板键和版本；
- 允许的规则及规则版本；
- implementation steps；
- specification；
- 通用客户素材清单；
- owner、effort、definition of done；
- 定性验证指标定义；
- 可使用的数据来源；
- 明确依赖规则；
- Dify 文案允许引用的事实类型和限制。

任何已触发 Finding 使用未知 rule ID、未知 rule version 或没有行动模板时整体失败。不能静默丢弃一个可能高优先级的问题。

## 7. 候选分组与引用

规划器只消费 `findings_result.findings`。`not_checked` 和 `not_triggered` RuleEvaluation 没有 Finding，不能转成 Action。

每个 Finding 必须满足：

- Finding ID 唯一；
- rule ID/version 在目录中；
- evidence IDs 和 comparator IDs 全部存在于同一 Findings 结果；
- affected URLs/queries 与其来源证据和规则目标保持现有合同允许的关系；
- Finding 的 severity、confidence、classification 和数据来源可用于选择，但不能被行动模板改写。

工作包分组键由 `template_key + 标准目标身份` 构成：

- 网站访问/索引、标题和市场家族在同一报告内分别聚合为一个工作包；
- 页面类型差距按 page_type 分组；
- GBP 对齐按客户实体聚合为一个工作包，并保留实际触发字段。

同一 Finding 只能归入一个工作包。工作包中的 Finding IDs 去重并按稳定 ID 排序。

## 8. 优先级与前三项选择

每个工作包先选择一个锚点 Finding。锚点按以下确定性元组选择：

1. severity：`critical > high > medium > low`；
2. confidence：`high > medium > low`；
3. classification：`fact > inference > estimate`；
4. 影响范围；
5. Finding ID。

工作包优先级以前四项的锚点值为主。关联低置信度 Finding 不会拉低同工作包中已存在的高置信度锚点，也不能靠堆叠重复 Finding 抬高前三项。

影响范围只用于同等级细分：

- 去重 affected URLs 数量，最多计 5；
- 去重 affected queries 数量，最多计 5；
- 去重 Finding 数量，最多计 5。

三项相加形成有上限的范围值。最终相同则按目录中固定工作包顺序、标准目标键和 action ID 排序。输入数组顺序、Python 哈希种子和进程不能影响结果。

选择优先级最高的三个不同工作包。若合法工作包少于三个，抛出 `V22_ACTIONS_INSUFFICIENT_ACTIONABLE_FINDINGS`，不返回部分结果。

## 9. 稳定 Action ID

Action ID 基于：

- action catalog version；
- template key/version；
- 标准结构化目标身份。

格式为可读前缀加固定摘要后缀，例如 `ac_align_public_gbp_a1b2c3d4e5f6`。ID 不包含 sequence、planning date、Finding 顺序或客户文案。

相同修复类型和相同目标在重新排序或新增无关 Finding 后仍保持同一 Action ID。目标真实改变时生成新 ID。

同一计划内出现重复 ID 或同一 ID 对应不同内容时整体失败。

## 10. 依赖与最终顺序

依赖只来自目录中的固定规则和实际目标重叠：

- 若选中的标题行动与选中的访问/索引行动包含相同 URL，标题行动依赖访问/索引行动；
- 若选中的市场复查行动与选中的访问/索引行动指向同一客户站点/URL，市场行动依赖访问/索引行动；
- 不根据相关性、时间先后习惯或 SEO 常识推测其他依赖。

只在最终选中的三个行动之间建立依赖；未入选行动不能成为悬空依赖。

最终使用稳定拓扑排序：

1. 先满足依赖；
2. 同一可执行层按第 8 节优先级排序；
3. sequence 固定为 1、2、3；
4. review date 固定为 planning date + 30、60、90 天。

未知依赖、自依赖、循环依赖、重复 sequence 或日期溢出整体失败。

## 11. 行动内容和事实边界

所有非文案字段由版本化模板和 Findings 共同生成：

- 步骤只描述该工作包允许的检查、修复、客户批准和复查过程；
- exact targets 只来自已绑定目标；
- required client assets 使用固定通用类别，如“客户确认的正确 GBP 字段值”，不能把未知值写成已知值；
- definition of done 描述可验证状态，不承诺排名、流量、线索或收入结果；
- validation metrics 使用定性、证据可复核基线，如“已观察到不匹配”或“已保存页面返回错误状态”；
- success condition 只能是新快照中错误消失、字段对齐、页面可访问/可索引或同类数据完成复查；
- 禁止虚构百分比、流量、排名目标、截止承诺、页面 URL、服务内容、地址、电话或客户素材。

data sources 从实际引用 Evidence 的 source_type 与目录允许集合的交集生成。没有真实引用的数据源不能出现在行动中。

V22-034 只能写入明确的文案槽位；后端必须比较并拒绝 Dify 对上述字段的任何改变。

## 12. 错误和资源限制

新增安全、不可重试的确定性错误类别：

- 输入或结果结构无效；
- Findings 校验和/引用无效；
- 未支持的 rule ID/version；
- 合法工作包不足三个；
- Action ID 冲突；
- 依赖无效或存在循环；
- 数量、目标、步骤、素材、审计或总字节超过限制。

错误代码和用户消息不能包含 Finding statement、URL、query、地址、电话、GBP 值、Evidence 原文或内部堆栈。

首版限制模型必须对以下类别设置硬上限并允许测试下调：

- 输入 Findings 数量；
- 候选工作包数量；
- 每个行动的 Findings 和目标数量；
- 步骤、要求、素材、指标和依赖数量；
- 选择审计数量；
- 最终 JSON 字节数。

## 13. 验证与测试

测试至少覆盖：

1. 当前所有网站、市场、竞争对手和 GBP rule ID/version 均有明确映射。
2. HTTP/noindex、市场、GBP 相关 Findings 正确合并；不同页面类型正确分开。
3. 无关 Findings 不合并，同一 Finding 不重复归属。
4. severity、confidence、classification、受影响范围和固定键排序。
5. 低置信度行动不能越过同严重度的高置信度行动。
6. 大量重复市场观察不能无限抬高范围优先级。
7. 输入顺序变化、重复构建和不同 Python 哈希种子输出一致。
8. 行动重新排序时稳定 ID 不变；目标改变时 ID 改变。
9. 访问/索引到标题和市场行动的目标重叠依赖正确，拓扑顺序稳定。
10. 未知规则、未知版本、未知引用、重复 ID、自依赖和循环依赖稳定失败。
11. 合法工作包为零、一或两个时返回“行动证据不足”；三个以上时只返回前三个。
12. actions 恰好三个，sequence 为 1/2/3，日期为 +30/+60/+90，依赖只引用已选行动。
13. Action 不出现 Evidence 未支持的数据源、目标、事实或数字。
14. 资源限制覆盖所有输出类别，输入不被修改。
15. 后端完整测试集通过，前端、API、Worker 和生产开关不变。

## 14. 完成标准

满足以下条件即视为 V22-033 完成：

- `build_public_action_plan` 对有效输入稳定生成恰好三个行动骨架；
- 三个行动是不同修复工作包，全部由真实 Finding 支持；
- 选择、分组、稳定 ID、依赖、日期和未入选项可审计；
- 证据不足时明确失败，不生成部分或占位行动；
- 不创建无证据事实、目标、数字或结果承诺；
- V22-034 可以直接使用严格骨架和 copy requirements，而无须重写选择逻辑；
- 前端、正式 ReportV22 合同、Worker、生产开关和部署状态保持不变。
