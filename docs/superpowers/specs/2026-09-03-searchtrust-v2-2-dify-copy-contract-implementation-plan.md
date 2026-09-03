# SearchTrust v2.2 Dify 客户文案合同实施计划

日期：2026-09-03

状态：设计已批准，等待用户确认实施计划后开始业务代码。

依据：[2026-09-03-searchtrust-v2-2-dify-copy-contract-design.md](./2026-09-03-searchtrust-v2-2-dify-copy-contract-design.md)

## 目标

实现 V22-034 独立受控文案合同：从已验证的公共 Findings 和三项 Action Plan 构造最小稳定请求，允许 Dify 只选择版本化英文句式和既有事实 ID，由后端严格校验并渲染 `why_now` 与 `client_facing_explanation`。无效输出最多尝试三次，耗尽后明确失败且没有任何文案或事实兜底。

本轮不接入正式 ReportV22 执行器、Worker、API、前端或生产 Dify 配置。

## Task 1：锁定严格模型与脱敏错误边界

文件：

- 新增 `app/report_v22/copy_models.py`
- 新增 `app/report_v22/copy_errors.py`
- 新增 `tests/test_v22_copy_models.py`

步骤：

1. 先写失败测试，锁定 `CopyRequestV1`、`CopyActionInput`、`CopyFact`、`CopyPatternSpec`、`CopyResponseV1`、`PatternSelection`、`RenderedActionCopy` 和 `PublicActionCopyResult`。
2. 所有模型继承 v2.2 `StrictModel`，拒绝类型强制转换与额外字段。
3. 锁定 actions 恰好三项、sequence 1/2/3、稳定 ID 格式、语言只能为 `en`、校验和格式、列表去重和数量上限。
4. 为事实、句式、槽位、单项文本、原始响应和最终结果定义可测试下调的硬限制。
5. 实现本地不可重试错误、Dify 输出可重试错误、临时 provider 可重试错误和永久 provider 不可重试错误；错误详情只能包含固定路径/分类，不能包含事实值或原始输出。
6. 运行模型测试并确认先失败，随后实现最小模型直至通过。

验证：

```bash
.venv/bin/python -m pytest -q tests/test_v22_copy_models.py
```

## Task 2：建立版本化英文句式目录

文件：

- 新增 `app/report_v22/copy_catalog.py`
- 新增 `tests/test_v22_copy_catalog.py`

步骤：

1. 为 V22-033 五类 Action template 建立 `why_now` 和 `client_facing_explanation` 句式目录。
2. 每个句式固定用途、兼容 Action template、有序槽位、槽位 cardinality、允许 fact type、安全选择说明、英文模板和渲染长度上限。
3. 为多目标和 limitation 使用列表槽位，规定稳定连接与标点规则；当前每类行动的 required limitation 必须都有可覆盖路径。
4. 目录不得包含排名、流量、线索、收入或永久性结果承诺，不得引入未经 Findings/Action 支持的具体事实。
5. 测试当前五类 Action template 映射完整、句式键唯一、槽位可满足、模板占位符与规格完全一致。

验证：

```bash
.venv/bin/python -m pytest -q tests/test_v22_copy_catalog.py
```

## Task 3：构建稳定最小请求和事实原子

文件：

- 新增 `app/report_v22/copy_contract.py`
- 修改 `app/report_v22/actions.py`
- 新增 `tests/v22_copy_helpers.py`
- 新增 `tests/test_v22_copy_request.py`

步骤：

1. 先写基于真实 `build_public_findings` 与 `build_public_action_plan` 输出的请求构建测试。
2. 将 V22-033 `_canonical_findings` 提升为不修改输入的内部公共规范化函数，V22-033 与 V22-034 共用同一 Findings checksum 算法；保持现有行动输出字节不变。
3. 严格复核 Action Plan 的 `findings_checksum`、Action 数量/顺序、Finding 引用、template/version 和 `copy_requirements`。
4. 从明确允许的 Finding 字段、结构化 Action targets、definition of done 和 required limitations 生成稳定 `CopyFact`。
5. Fact ID 只基于合同版本、action ID、来源路径、来源 Finding IDs 和规范值；输入数组顺序、进程和系统日期不能影响结果。
6. 对完整规范化 Action Plan 计算 `action_plan_checksum`，再构造只含当前三项行动事实及兼容句式规格的 `CopyRequestV1`。
7. 排除 selection audit、未选 Findings、原始 Evidence payload、私有数据、完整可编辑步骤/依赖/指标副本和执行元数据。
8. 复核输入/输出字节限制以及调用方对象在成功和失败路径都未改变。

验证：

```bash
.venv/bin/python -m pytest -q tests/test_v22_public_actions.py tests/test_v22_copy_request.py
```

## Task 4：解析、反篡改校验与后端渲染

文件：

- 修改 `app/report_v22/copy_contract.py`
- 新增 `tests/test_v22_copy_contract.py`

步骤：

1. 先写有效原生对象和合法 JSON 字符串响应测试，并锁定唯一 workflow output 键；Markdown fenced JSON 不兼容。
2. 严格校验 schema、合同/目录版本、语言、action plan checksum、三项行动顺序和完整覆盖。
3. 校验 pattern key 的用途和 Action template 兼容性、槽位名称/cardinality、fact type、fact 所属 action/Finding，以及 required limitations 全覆盖。
4. 拒绝行动缺失/重复/乱序、未知或跨行动 fact ID、未知句式、槽位缺失/多余、额外字段和任何自由文本。
5. 用后端目录和请求中已绑定的事实值渲染文案；列表槽位采用目录固定连接规则，不允许 Dify 提供连接符或标点。
6. 连接语必须来自英文目录；后端已有商家名、查询词、地点、URL 或数字作为不可改写事实原样保留。
7. 复核非空与长度限制，输出恰好三项 `RenderedActionCopy`，并保留使用的 pattern/fact IDs 供审计。
8. 比较 Action Plan 规范摘要，证明目标、步骤、规范、素材、依赖、负责人、工作量、完成标准、指标、数据源和日期没有变化。

验证：

```bash
.venv/bin/python -m pytest -q tests/test_v22_copy_request.py tests/test_v22_copy_contract.py
```

## Task 5：实现注入式 provider 与最多三次重试

文件：

- 新增 `app/report_v22/copy_adapter.py`
- 新增 `tests/test_v22_copy_adapter.py`

步骤：

1. 定义只接收规范 `CopyRequestV1` 并返回 workflow outputs 的异步 `CopyProvider` 协议。
2. 实现 `generate_public_action_copy`：调用 provider、执行完整合同校验并返回成功 attempt count。
3. 默认且最多调用三次；每次发送字节一致的请求，不能在失败后扩大事实或句式范围。
4. 对临时 provider 错误、缺失 outputs 和无效 Dify 输出重试；测试注入零等待，预留有上限退避 callable。
5. 对本地输入/目录错误和鉴权、权限、配置等永久 provider 错误立即失败，不浪费重试。
6. 重试耗尽后抛出脱敏最终错误，记录每次固定错误分类；不返回部分结果、不调用 v2.1 fallback、不生成固定通用文案。
7. 测试首轮成功、前两次失败第三次成功、连续三次无效、连续临时错误、永久错误和 provider 未被本地错误调用。

验证：

```bash
.venv/bin/python -m pytest -q tests/test_v22_copy_adapter.py
```

## Task 6：安全边界与完整回归

文件：

- 修改上述测试和实现文件

步骤：

1. 添加校验和错配、迟到/跨任务响应、未知 action/finding/fact ID、跨行动引用和 limitation 遗漏测试。
2. 添加额外字段、自由文本、Markdown、中文注入、数字/URL 注入和修改事实值测试，确认全部在渲染前失败。
3. 添加超长事实、facts/patterns/slots 超限、原始响应超限、渲染结果超限和错误详情截断测试。
4. 添加输入重排、重复构建和不同 `PYTHONHASHSEED` 的稳定性测试。
5. 确认 v2.1 copy contract、现有 Dify pipeline、V22-033 action output、正式 ReportV22 模型及所有旧测试保持不变。
6. 运行相关测试、Python 编译检查和完整后端测试集。

验证：

```bash
.venv/bin/python -m pytest -q tests/test_v22_copy_models.py tests/test_v22_copy_catalog.py tests/test_v22_copy_request.py tests/test_v22_copy_contract.py tests/test_v22_copy_adapter.py tests/test_v22_public_actions.py
.venv/bin/python -m compileall -q app/report_v22
.venv/bin/python -m pytest -q
```

## Task 7：完成记录与提交

文件：

- 新增 `docs/superpowers/specs/2026-09-03-searchtrust-v2-2-dify-copy-contract-completion.md`

步骤：

1. 运行 `git diff --check`，检查只包含本轮文件和批准的 `actions.py` 规范化函数可见性调整。
2. 记录新增测试、相关测试和完整测试集的实际通过数量与耗时。
3. 明确记录未接入真实 Dify 网络、正式报告执行器、Worker、API、前端和生产开关。
4. 确认 SearchTrust-RD 与 search-trust 工作目录状态，不覆盖用户无关修改。
5. 提交实现和完成记录；不推送、不部署。

## 完成判定

- 有效 Findings/Action Plan 稳定生成最小 `CopyRequestV1`、事实 ID 和 checksum。
- Dify 响应只能选择后端允许的句式和当前行动的事实 ID，不能提供任意文案。
- 后端拒绝无证据引用、数字/URL/自由文本注入、行动不足/重复、跨行动引用和合同漂移。
- 后端渲染恰好三项非空文案，并保持真实专有值和 Action Skeleton 全部非文案字段不变。
- 可重试错误最多尝试三次，耗尽后明确失败且没有部分结果或 fallback。
- 所有新增、相关和完整后端测试通过。
- 正式报告、Worker、API、前端、真实 Dify 配置和生产状态保持不变。
