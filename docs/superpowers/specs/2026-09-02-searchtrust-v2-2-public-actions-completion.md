# SearchTrust v2.2 公共三项行动生成完成记录

日期：2026-09-02

状态：V22-033 已实现并通过完整后端回归；未推送、未部署、未开启生产功能。

依据：

- [公共三项行动生成设计](./2026-09-02-searchtrust-v2-2-public-actions-design.md)
- [公共三项行动生成实施计划](./2026-09-02-searchtrust-v2-2-public-actions-implementation-plan.md)

## 1. 已完成内容

新增纯确定性的 `build_public_action_plan`：

- 严格重验 `PublicFindingsResult`、计划基准日期和资源限制；
- 复核 Finding、RuleEvaluation、Evidence、SourceTrace、roll-up、URL 和 query 引用闭包；
- 使用版本化目录覆盖当前全部网站、市场、竞争对手和 GBP 公共 Finding 规则；
- 将 HTTP/noindex、市场观察及 GBP 字段按修复目标合并；页面类型差距按 page_type 分开；
- 按严重度、置信度、事实类型、封顶影响范围和稳定键选择前三个工作包；
- 低置信度问题不会越过同严重度的高置信度问题，重复观察不会无限提高优先级；
- Action ID 由模板和结构化目标生成，不包含顺序、日期或文案；
- 只在已选行动间建立经批准的目标重叠依赖，并稳定拓扑排序；
- 固定生成 sequence 1/2/3 和基准日后 30/60/90 天复盘日期；
- 输出所有候选的选择审计及未入选 Finding IDs；
- 合法工作包少于三个时明确失败，不返回部分、占位或虚构行动；
- 不包含最终 `why_now` 或 `client_facing_explanation`，为 V22-034 保留严格 copy requirements。

## 2. 新增内部合同

新增：

- `app/report_v22/action_errors.py`：安全、不可重试的稳定行动错误。
- `app/report_v22/action_models.py`：输入、限制、结构化目标、行动骨架、验证指标、文案要求、选择审计和计划合同。
- `app/report_v22/public_action_catalog.py`：五类行动模板及当前全部公共规则的精确版本映射。
- `app/report_v22/actions.py`：引用复核、候选分组、优先级、稳定身份、依赖和计划生成。

正式 `ReportV22`、`TopAction`、共享 JSON Schema 和 TypeScript 合同均未修改。本轮输出仍是后端内部行动骨架。

## 3. 行动目录

首版行动目录包含：

1. 网站访问与索引恢复；
2. 页面标题差异化；
3. 市场可见性核查与改进；
4. 按页面类型关闭经验证的竞品样本差距；
5. 网站与公开 GBP 字段一致性。

每个模板固定步骤、规范、客户素材、建议负责人、工作量、完成标准、定性指标、允许数据源、文案允许字段和必须保留的限制。未知 rule ID/version 整体失败，不能静默遗漏。

## 4. 测试证据

新增 37 项测试，分布于：

- `tests/test_v22_action_models.py`
- `tests/test_v22_public_action_catalog.py`
- `tests/test_v22_public_actions.py`

覆盖：

- 严格三项合同、sequence、日期、依赖和审计一致性；
- 全部当前公共规则和版本的目录覆盖；
- 相关 Findings 合并、无关 Findings 分离；
- severity/confidence/classification/impact 排序；
- 影响范围封顶、输入顺序无关、跨进程稳定；
- planning date 不改变 Action ID、真实目标变化会改变对应 ID；
- 少于三项、未知规则/版本/引用、无证据目标、非有限值和日期溢出；
- 候选、Finding、目标、步骤、素材、审计和字节资源上限；
- 输出不含最终 Dify 文案字段，目标和数据源全部来自输入引用。

相关 Actions 与 Findings 回归：

```text
83 passed in 2.52s
```

后端完整测试集：

```text
1319 passed in 10.92s
```

其他检查：

- Python 编译检查通过；
- `git diff --check` 通过；
- 前端 `search-trust` 工作区保持干净，最后提交仍为 `49f2436`；
- `V22_ANALYZE_ENABLED`、`V22_PREFLIGHT_ENABLED`、`V22_COMPETITOR_DISCOVERY_ENABLED` 默认值仍为 `False`；
- Worker 仍使用 `UnavailableV22Executor`。

## 5. 明确未实现

本轮没有实现或改变：

- V22-034 Dify 文案请求、重试、反篡改和最终文案合并；
- 最终 `TopAction`、Roadmap、ClientSummary 或 ReportV22 组装；
- Prospect 总执行器与 Worker 接线；
- verified execution 和第一方数据行动；
- HTTP API、前端、PDF、共享合同；
- 生产开关、推送、部署或发布。

## 6. 后续入口

下一步 V22-034 可以直接读取 `PublicActionPlan.actions[*].copy_requirements`，只生成并校验 `why_now` 与 `client_facing_explanation`。后端必须逐字段比较行动骨架，拒绝 Dify 对 ID、Findings、目标、步骤、要求、素材、负责人、工作量、完成标准、指标、数据源、依赖、日期和排序的任何改变。
