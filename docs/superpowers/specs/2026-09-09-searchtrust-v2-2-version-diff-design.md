# SearchTrust v2.2 — V22-073 版本差异设计

日期：2026-09-09

范围：V22-073。

## 1. 目标

V22-073 把不可变的父级获客报告与 V22-072 Verified 重新优先级结果进行确定性比较，
生成只包含真实变化的 `VersionDiff`。它回答四个问题：旧结论是什么、哪项新证据或
排序决定造成变化、当前结论是什么、为什么归入该变化类型。

本阶段不会把所有旧 Findings 罗列为差异。没有新关系、结论内容未变化且行动入选状态和
顺序也未变化的旧 Finding 进入内部 `unchanged` 审计，不进入面向用户的差异列表。

完成本阶段不等于完成 Verified Client Action Plan。V22-074 继续生成指标基线、成功条件、
最终行动内容和 30/60/90 路线图。

## 2. 已确认决策

- 采用 Finding 中心的确定性差异账本，不采用行动中心摘要或自然语言相似度匹配。
- 每条发生变化的父 Finding 最多产生一条主差异；多种变化同时发生时使用固定优先级。
- 新 Finding 已被用于解释旧 Finding 后，不再重复生成 `new`。
- `replaced` 只允许明确反证规则推翻旧结论且存在替代 Finding 时生成；当前上游没有此类
  规则，因此 V22-073 v1 保留合同类型但不生成 `replaced`。
- 不变旧 Finding 不标记；差异的目的仅是告诉用户哪里发生了变化。
- 父 Finding 指纹覆盖完整规范化内容，而不只覆盖陈述或 ID。

## 3. 架构与边界

新增独立阶段 `v22_version_diff_v1`。它只执行纯规则计算：

1. 父报告绑定与不可变性校验；
2. V22-072 上游重算校验；
3. 当前 Finding/Evidence 统一索引；
4. 新关系到具体父 Finding 的严格归属；
5. 变化分类、决策证据选择和 `new` 去重；
6. 引用闭合、资源上限和 checkpoint。

阶段不读数据库、不调用 provider、不调用 LLM、不修改父报告或 V22-072 对象。现有公开
Findings/行动、V22-070、V22-071、V22-072 和正式 `ReportV22` 合同保持冻结。

## 4. 输入合同

`VersionDiffBuildInput` 固定包含：

- `case_id`、`parent_report_id` 和 `evaluated_at`；
- 完整父级 `ReportV22` 与 `parent_report_checksum`；
- `verified_reprioritization_input`、`verified_reprioritization_result` 及各自 checksum；
- 版本化资源上限。

绑定规则：

- 父报告必须是 schema `2.2.0` 的 `prospect` 报告；
- 父报告 `identity.case_id == case_id`；
- 父报告 `report_version.report_id == parent_report_id`；
- 父报告不得已有 parent，且必须使用空的 initial version diff；
- V22-072 的 Case、parent、evaluated_at 必须与本请求相同；
- 父报告 Findings、Evidence 和 Top Action 身份必须与 V22-072 输入中的已验证公开
  Findings/行动一致。客户文案可以是父报告已保存的渲染结果，但事实 ID、Finding 引用、
  行动 ID、序号、依赖和日期不得偏离权威公开阶段输出。

阶段先校验所有 checksum，再重新运行 V22-072 并对规范化结果逐字节比较。重新签名后的
篡改仍会被上游重算拒绝。

## 5. 父报告不可变性与指纹

每个 `PreviousFindingReference.fingerprint` 固定为父报告中完整 Finding 规范化 JSON 的
SHA-256。参与指纹的字段包括 Finding ID、陈述、Evidence/Comparator、规则和版本、分类、
严重度、范围、置信度、受影响 URL/查询、缺失数据和改变条件。

父报告整体 checksum 绑定完整 `ReportV22`。生成器从不回写父报告；重复执行必须得到相同
指纹。任意父 Finding 内容、顺序敏感字段或引用被改写时确定性拒绝，而不是生成一份看似
合理的新差异。

## 6. 当前 Finding 与 Evidence 索引

当前 Finding 集合由以下已重算结果组成：

- 公开 Findings；
- V22-070 单来源 Findings；
- V22-071 跨来源 Findings。

当前 Evidence 集合由公开 Evidence 与 V22-071 Evidence 合并；V22-071 已包含 V22-070
Evidence，因此不重复插入。相同 ID 和相同规范化内容只保留一份；相同 ID 对应不同内容时
返回 `ID_CONFLICT`。最终普通 `FindingId` 和 `EvidenceId` 必须在当前报告命名空间内全局唯一。

内部审计继续使用 V22-072 的限定 Finding 引用：`origin_stage + ruleset_version + finding_id`，
避免在合并前丢失来源边界。

## 7. 新关系到父 Finding 的严格归属

V22-072 的关系先定位公开行动候选，再通过该候选的 `ActionTarget.finding_ids` 定位具体父
Finding，不把候选级关系扩散到同一行动包中的所有旧 Findings。

- 页面关系只归属同一规范化 URL 目标列出的旧 Finding IDs；
- 查询关系只归属同一 NFKC/空白折叠/casefold 查询目标列出的旧 Finding IDs；
- 聚合市场关系使用 `site` 目标中明确列出的市场 Finding IDs；
- 无匹配、仅审计和测量关系不强行绑定旧业务 Finding；
- 不读取 Finding statement，不使用标题、page type、canonical、redirect 或语义相似度。

每个被归属的新 Finding 记录为该旧 Finding 的 `direct_relation`。同一新 Finding 可以严格
支持同一行动包中的多个明确旧目标，但只要已进入至少一个旧差异条目，就不会再独立标记
为 `new`。

## 8. 变化分类

每条旧 Finding 的主分类按以下优先级确定：

1. `refined`：存在严格归属的 `reduces_urgency` 关系。旧 Finding 保留，原因明确说明可靠
   增长只降低紧迫度，不构成反证；HTTP、noindex 和身份硬事实不会进入此分支。
2. `reprioritized`：所属公开行动的入选状态发生变化，或同为入选时序号发生变化。直接
   支持可以同时写入原因，但主分类仍以用户可见的行动位置变化为准。
3. `confirmed`：存在至少一条严格归属的 `supports` 关系，且所属行动位置未变化。结论
   陈述保持不变，新增 Evidence 提高其验证依据。
4. 无以上条件：不生成差异，写入内部 unchanged 审计。

`replaced` 在 v1 中不可达。跨源方向冲突只表示测量一致性问题，增长只降低一级，二者均
不证明旧结论错误。

## 9. 新 Findings

V22-070/071 中未被任何旧差异条目作为 `direct_relation` 消费的 Finding 生成 `new`：

- 新的 GSC/GA4 测量阻断；
- 跨来源方向冲突；
- 没有旧公开目标可对应的独立页面、查询或聚合机会；
- 仅审计但仍构成已保存 Finding 的新观察。

`new` 不伪造 `previous_finding`。其 `current_finding_ids` 只包含该新 Finding，自有
Evidence 和 Comparator 合并进入 `evidence_ids`。如果 Finding 没有完整 Evidence 引用，
上游合同或本阶段引用校验会拒绝它。

## 10. 当前 Finding 与 Evidence 引用

对旧 Finding 生成的条目：

- `current_finding_ids` 至少包含当前报告中原样保留的公开 Finding ID；
- `confirmed/refined` 还包含直接归属的新 Finding IDs；
- `evidence_ids` 使用直接新 Findings 的 Evidence 与 Comparator，并按 ID 去重排序；
- `reprioritized` 若有直接关系，使用直接关系 Evidence；若纯粹由排序穿越造成，则使用
  导致穿越的最小决策证据集合。

最小决策证据按以下规则选择：

- 旧未入选行动进入前三：使用该行动提高等级的关系 Findings；
- 旧入选行动退出前三：使用越过它的新入选候选之关系 Findings；
- 两个旧入选行动换序：使用造成二者验证等级变化的关系 Findings；
- 测量修复占据第一：使用该测量行动绑定的 measurement Findings。

若测量阻断仅来自来源 assessment、没有独立 Finding Evidence，差异条目使用原旧 Finding
的公开 Evidence 保持合同引用闭合，固定原因明确说明“行动因来源健康/身份/比较门禁调整，
该 Evidence 只支撑原业务结论，不是旧结论的反证”；内部审计另外保留 V22-072 的安全问题
代码。不得把无关新 Evidence 伪装成目标级支持。

## 11. 输出合同

`VersionDiffBuildResult` 使用 `version_diff_result_v1` schema，包含：

- Case、parent、evaluation time 和所有上游 checksum；
- 可直接进入最终 `ReportV22` 的 `VersionDiff(kind="upgrade", parent_report_id, entries)`；
- `VersionDiffAuditEntry` 列表，记录每个输出条目的稳定 audit ID、限定 Finding 引用、
  relation IDs、候选/action ID、原因代码、决策问题代码和 Evidence 来源；
- `unchanged_previous_findings`；
- `consumed_new_findings`；
- 未消费且已生成 `new` 的限定 Finding 引用。

面向用户的 `reason` 不由 LLM 生成，只能来自版本化固定文案目录。文案说明具体变化，不
推断流量因果、不宣布任一测量源错误、不声称增长推翻直接公开事实。

## 12. 顺序与去重

差异条目顺序固定为：

1. 当前三项行动相关变化，按新序号 1、2、3；
2. 其他旧结论变化，按父行动原序号、候选顺序和旧 Finding ID；
3. `new` Findings，按来源阶段、规则、目标和 Finding ID。

同一旧 Finding 最多一条差异；同一新 Finding 最多一条 `new`。所有集合在序列化前按稳定
键排序。输入无序集合的遍历顺序不改变输出 JSON 字节、fingerprint 或 checkpoint key。

## 13. 完整性和确定性错误

输出前验证：

- 每个 previous reference 指向同一 parent report 且 fingerprint 正确；
- 每个 current Finding 和 Evidence ID 在当前索引中存在；
- 每个 direct relation 与 V22-072 关系、候选目标和具体旧 Finding 一致；
- 所有发生可见变化的旧 Finding 恰好有一个条目；所有无变化旧 Finding 没有条目；
- consumed 与 `new` 互斥，二者并集等于全部新 Findings；
- `replaced` 在当前规则版本中不存在；
- 不存在重复或冲突 ID。

固定错误代码：

- `INPUT_INVALID`；
- `BINDING_INVALID`；
- `CHECKSUM_MISMATCH`；
- `UPSTREAM_MISMATCH`；
- `PARENT_MISMATCH`；
- `FINGERPRINT_MISMATCH`；
- `REFERENCE_INVALID`；
- `ID_CONFLICT`；
- `UNSUPPORTED_CHANGE`；
- `LIMIT_EXCEEDED`。

错误只返回固定安全文案，不回显父报告、Finding 陈述、Evidence 值或 provider payload；不
返回部分差异。

## 14. 资源上限与隐私

默认上限：

- 父 Findings 10,000；
- 新 Findings 15,000；
- 当前 Evidence 50,000；
- V22-072 relations 30,000；
- 差异条目 25,000；
- 审计条目 25,000；
- 每条差异 current Findings 1,000、Evidence 10,000；
- 输出 20 MB。

超限确定性失败，不截断、不抽样。结果、checkpoint 和日志不得包含 GBP 精确 Performance、
关键词、raw Content、OAuth token 或父报告完整内容。旧 Finding statement 只出现在合同
要求的 `PreviousFindingReference.statement` 中。

## 15. Checkpoint

checkpoint schema 为 `version_diff_checkpoint_v1`。摘要绑定：

- stage、分类规则和固定文案版本；
- Case、parent 和 evaluation time；
- 父报告 checksum；
- V22-072 输入/结果 checksum；
- 资源上限。

checkpoint 只存验证后的 `VersionDiffBuildResult`。命中时重新验证 job、摘要、版本、结果
checksum、引用闭合和字节上限；损坏或属于另一 job/input 的值确定性拒绝。

## 16. 测试计划

### 16.1 父报告与上游绑定

- 错 Case、report ID、report type、parent、initial diff 和 schema；
- 父报告整体 checksum、完整 Finding fingerprint 和公开结果/行动一致性；
- V22-072 输入/结果 checksum、重算差异和重新签名篡改。

### 16.2 分类

- 严格支持且位置不变为 `confirmed`；
- 增长降级为 `refined`，硬公开事实不受影响；
- 进入、退出前三和 1/2/3 换序为 `reprioritized`；
- 多条件时执行 `refined > reprioritized > confirmed`；
- 无变化旧 Finding 不输出；
- 当前规则不生成 `replaced`。

### 16.3 新 Finding 和去重

- 已直接归属的新 Finding 被消费，不重复 `new`；
- 未匹配机会、测量阻断、跨源冲突和审计 Finding 生成 `new`；
- 一个新 Finding 严格关联多个明确旧目标时消费一次、旧条目各自引用；
- consumed 与 new 完整分区。

### 16.4 决策证据和引用

- 入选边界穿越、换序和测量修复的最小证据；
- assessment-only 阻断使用公开 Evidence fallback 且固定披露其边界；
- 未知 Finding/Evidence/relation/candidate、冲突 ID 和错误 fingerprint；
- previous/current/evidence 引用全部闭合。

### 16.5 确定性、隐私和 checkpoint

- Findings、Evidence、relations 和输入集合换序后结果字节一致；
- GBP 精确值、关键词和 raw Content 扫描；
- 各资源上限、非有限值、损坏 checkpoint、错误 job/input 和版本变化；
- checkpoint 只含结果，不含父报告或 V22-072 输入。

### 16.6 回归

- 公开报告合同/组装、公开 Findings/行动、V22-070、V22-071、V22-072 定向回归；
- 后端完整回归；
- 前端合同生成一致性、类型、完整测试和生产构建。

## 17. 非目标与发布

本阶段不：

- 生成最终 Verified 报告或客户文案；
- 生成指标 baseline、success condition 或 30/60/90 路线图；
- 修改父报告、旧 Findings 或旧 Evidence；
- 调用数据库、Google、SerpAPI、OAuth、付款或 LLM；
- 新增前端页面、公开 API 或数据库迁移；
- 开放 Verified Generation。

实现作为默认不可达的 Railway 后端内部能力发布。GSC、GA4、GBP 同步和 Verified
Generation 开关继续缺失/关闭。

## 18. 验收标准

V22-073 完成需同时满足：

- 父报告完整绑定、指纹和不可变性可验证；
- V22-072 可重算且调用方不能篡改差异依据；
- 只输出真实变化，不变旧 Finding 不标记；
- `refined/reprioritized/confirmed/new` 分类及优先级稳定；
- 当前没有反证时不生成 `replaced`；
- 新 Findings 的直接消费和 `new` 分区完整、不重复；
- 排名穿越证据最小、诚实且引用闭合；
- 输出顺序、JSON、fingerprint 和 checkpoint 幂等；
- 隐私、上限、定向和全量回归通过；
- 无数据库/前端变更，Railway 关闭能力发布成功。
