# SearchTrust v2.2 公共三项行动生成实施计划

日期：2026-09-02

状态：设计已批准，用户已确认开始实施。

依据：[2026-09-02-searchtrust-v2-2-public-actions-design.md](./2026-09-02-searchtrust-v2-2-public-actions-design.md)

## 目标

实现纯确定性的 `build_public_action_plan`，从已验证的公共 Findings 中构造、分组、排序并复核恰好三个行动骨架。证据不足时明确失败；不生成最终 Dify 文案，不接入 Worker、API 或生产开关。

## Task 1：锁定内部合同与错误边界

文件：

- 新增 `app/report_v22/action_models.py`
- 新增 `app/report_v22/action_errors.py`
- 新增 `tests/test_v22_action_models.py`

步骤：

1. 先写严格输入、结构化目标、行动骨架、优先级、选择审计、copy requirements、行动计划和限制模型测试。
2. 锁定 actions 恰好三个、sequence 1/2/3、Action ID 唯一、依赖只引用已选行动及复盘日期递增。
3. 锁定结构化目标字段与 kind 一致、Finding 引用去重、步骤连续、data sources 和验证指标非空。
4. 锁定安全、不可重试的稳定错误代码。
5. 运行模型测试并确认先失败，随后实现最小模型直至通过。

验证：

```bash
.venv/bin/python -m pytest -q tests/test_v22_action_models.py
```

## Task 2：建立完整版本化行动目录

文件：

- 新增 `app/report_v22/public_action_catalog.py`
- 新增 `tests/test_v22_public_action_catalog.py`

步骤：

1. 为网站访问/索引、标题、市场、页面类型和 GBP 一致性定义五类模板。
2. 显式映射当前全部公共 rule ID/version；不接受未知规则或版本。
3. 固定模板步骤、规范、素材、负责人、工作量、完成标准、定性指标、允许数据源、文案字段和限制。
4. 测试当前规则集合与目录完全一致，无遗漏、无额外规则、无无证据数字或排名承诺。

验证：

```bash
.venv/bin/python -m pytest -q tests/test_v22_public_action_catalog.py
```

## Task 3：以失败测试锁定候选分组、排序和稳定 ID

文件：

- 新增 `tests/test_v22_public_actions.py`

步骤：

1. 使用真实 `build_public_findings` 输出构造网站恢复、标题、市场和页面类型候选。
2. 测试 HTTP/noindex、市场和 GBP 合并；不同 page_type 分离。
3. 测试 severity、confidence、classification、封顶影响范围和稳定键排序。
4. 测试输入顺序无关、重复生成、跨进程哈希种子一致。
5. 测试行动 ID 不含 sequence/planning date，目标变化才改变 ID。
6. 测试零到两个候选、未知规则/版本/引用、输入超限稳定失败。
7. 先运行并确认因生成器尚未实现而失败。

## Task 4：实现纯行动规划器

文件：

- 新增 `app/report_v22/actions.py`

步骤：

1. 严格重验输入并计算 Findings 校验和，不修改调用方对象。
2. 复核 Finding、RuleEvaluation、Evidence 和 SourceTrace 引用闭包。
3. 根据目录生成候选，按批准分组规则构造结构化目标。
4. 生成稳定 Action ID 和选择审计。
5. 按批准优先级选择前三个候选。
6. 仅在已选行动之间建立目标重叠依赖并执行稳定拓扑排序。
7. 设置 sequence 和 +30/+60/+90 review dates，生成行动骨架。
8. 重新验证结果、引用、依赖、资源限制和最终字节数。
9. 运行 Task 1—3 测试直至通过。

## Task 5：边界、反幻觉与完整回归

文件：

- 修改 `tests/test_v22_public_actions.py`
- 必要时修改上述实现文件

步骤：

1. 覆盖少于三项、重复 ID、自依赖、循环、未知依赖、日期溢出和所有资源上限。
2. 断言目标、数据源和 Finding IDs 全部来自输入引用。
3. 扫描目录输出，禁止百分比、排名/流量承诺、猜测 URL、地址、电话或具体客户值。
4. 断言未选择 Findings 有完整审计，输入在成功和失败路径都不变。
5. 运行相关 Findings/Actions 测试和后端完整测试集。

验证：

```bash
.venv/bin/python -m pytest -q tests/test_v22_action_models.py tests/test_v22_public_action_catalog.py tests/test_v22_public_actions.py
.venv/bin/python -m pytest -q
```

## Task 6：完成记录与提交

文件：

- 新增 `docs/superpowers/specs/2026-09-02-searchtrust-v2-2-public-actions-completion.md`

步骤：

1. 运行 `git diff --check` 和 Python 编译检查。
2. 记录相关测试及完整测试的实际通过数量和耗时。
3. 确认前端、API、Worker、ReportV22 正式合同及生产开关未改变。
4. 编写完成记录并提交；不推送、不部署。

## 完成判定

- 有效输入稳定生成恰好三个不同、证据支持的行动骨架。
- 相关 Findings 合并，无关 Findings 分开，少于三个候选明确失败。
- 选择、稳定 ID、依赖、日期、未选 Findings 和资源限制均可审计。
- 不生成最终文案或无证据事实、目标、数字与结果承诺。
- 所有新增、相关和完整后端测试通过。
- 前端、API、Worker、正式报告合同和生产状态保持不变。
