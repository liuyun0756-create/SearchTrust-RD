# SearchTrust v2.2 Dify 客户文案合同设计

日期：2026-09-03

状态：用户已批准完整书面设计；尚未开始业务代码实施。

范围：V22-034。基于 V22-033 已确定的公共 Findings 和三项 Action Skeleton，建立只允许受控事实引用、由后端安全渲染客户英文文案的 Dify 合同与有限重试边界。

主要仓库：SearchTrust-RD。本轮不修改 search-trust 前端运行时。

## 1. 目标与已确认产品口径

本轮为 Prospect 公共报告建立独立的 v2.2 文案边界。Dify 只决定如何组织后端允许的表达，不能拥有、修改或新增事实。

用户已确认：

- 采用“受控文案片段”方案，以事实安全优先于自由写作能力。
- Dify 只接收选中的 Finding/Action Skeleton 及其允许引用的最小事实集合。
- Dify 不返回任意客户文本；只选择版本化英文句式并为句式槽位绑定既有事实 ID。
- 事实、数字、URL、商家信息和行动内容由后端持有并在校验通过后填入句式。
- 首版只支持英文；中文或其他非英文输出视为无效。
- 文案校验或临时调用失败最多尝试三次。
- 三次均失败时本次最终报告生成失败；保留已有 Findings、Action Plan 和脱敏错误信息，不生成兜底文案。
- 本轮实现合同、受控渲染、有限重试和测试，不接入正式报告执行器，不开启生产开关。

## 2. 非目标

本轮不实现：

- 最终 Prospect Report 执行器、Roadmap、ExecutiveDecision、ClientSummary 或完整 `ReportV22` 组装；
- Redis 检查点、Worker 阶段接线、HTTP API、前端、PDF 或部署；
- Dify 工作流创建、发布或线上密钥配置；
- verified execution、GSC、授权 GBP、GA4 或客户私有数据文案；
- 任意自由文本事实判断、第二个 LLM 审核器或语义幻觉分类器；
- 复用 v2.1 的缺失文案补全、固定说明兜底或宽松 `extra=ignore` 合同。

网络传输层可以在后续总执行器接线时复用现有底层 Dify SSE 能力，但 v2.2 的请求、响应、校验、错误和重试编排必须保持独立。

## 3. 方案选择

比较过三种方式：

1. **受控句式与事实槽位**：后端提供版本化句式和事实 ID，Dify 只选择句式并绑定允许的事实；后端最终渲染。事实安全最强，文案自由度适中；用户已选择该方案。
2. **带引用的自由文案**：Dify 自由写作，每句话声明 Finding/Fact 引用。可以检查 ID 和数字，但难以可靠发现没有数字的新增事实。
3. **自由文案后置检查**：只检查结构、ID、数字和长度。最灵活，但无法满足“拒绝无证据事实”的验收要求。

首版不允许模型提供任何自由文本。若后续需要更丰富的语气，应增加后端审核过的句式目录版本，而不是放宽当前合同。

## 4. 整体数据流

1. 后端接收已验证的 `PublicActionPlan` 及其来源 `PublicFindingsResult`。
2. 后端交叉验证 Findings 校验和、行动引用、顺序和 `copy_requirements`。
3. 后端为三项行动构建最小 `CopyRequestV1`：受控事实原子、可用句式和固定合同元数据。
4. Dify 返回 `CopyResponseV1`：每项行动为两个文案槽位选择句式，并将句式参数绑定到允许的事实 ID。
5. 后端严格解析并验证版本、校验和、行动覆盖、ID 所属关系、句式槽位和禁止内容。
6. 后端使用本地句式目录和原始事实值渲染 `why_now` 与 `client_facing_explanation`。
7. 后端比较所有 Action Skeleton 非文案字段，确认未改变后再生成三项安全文案结果。

Dify 负责“如何组织表达”；后端始终拥有“表达了什么事实”。

## 5. 建议模块与纯入口

建议新增：

- `app/report_v22/copy_models.py`：严格请求、响应、事实原子、句式选择和最终文案结果模型。
- `app/report_v22/copy_catalog.py`：版本化英文句式、槽位类型和行动模板适配规则的唯一目录。
- `app/report_v22/copy_contract.py`：构建请求、解析响应、验证引用、渲染并复核不变量。
- `app/report_v22/copy_adapter.py`：注入式 Dify 调用接口与最多三次的有限重试编排。
- `app/report_v22/copy_errors.py`：稳定、脱敏、可判定是否重试的 v2.2 文案错误。

核心纯入口：

```python
def build_copy_request(
    findings: PublicFindingsResult,
    action_plan: PublicActionPlan,
) -> CopyRequestV1: ...

def validate_and_render_copy(
    request: CopyRequestV1,
    outputs: object,
) -> PublicActionCopyResult: ...
```

异步编排入口使用注入的 provider callable，不在合同模块直接读取密钥或发起网络请求：

```python
async def generate_public_action_copy(
    request: CopyRequestV1,
    provider: CopyProvider,
    *,
    max_attempts: int = 3,
) -> PublicActionCopyResult: ...
```

测试使用模拟 provider，不依赖网络或真实 Dify 账户。

## 6. 输入合同 `CopyRequestV1`

请求至少包含：

- `schema_version = "v22_copy_request_v1"`；
- `copy_contract_version = "v22_controlled_copy_v1"`；
- `copy_catalog_version = "v22_english_action_copy_v1"`；
- `language = "en"`；
- `findings_checksum`；
- `action_plan_checksum`；
- `actions`：恰好三项，sequence 必须为 1、2、3。

每个 `CopyActionInput` 包含：

- `action_id`、`sequence`、`template_key`、`template_version`；
- 当前行动允许引用的 `finding_ids`；
- `facts`：当前行动的受控事实原子；
- `why_now_patterns`：可用于 `why_now` 的句式键及槽位规格；
- `explanation_patterns`：可用于 `client_facing_explanation` 的句式键及槽位规格；
- `required_limitation_ids`：必须在最终表达中保留的限制语义。

请求不包含：

- 未入选 Findings、selection audit 或其他行动的事实；
- 原始抓取正文、完整 Evidence payload 或竞品页面全文；
- 客户私有数据、密钥、内部错误、堆栈和执行元数据；
- Dify 无需决定的实现步骤、指标、依赖、日期等完整可编辑副本。

`action_plan_checksum` 由完整规范化 `PublicActionPlan` 计算，覆盖 schema、目录版本、计划日期、全部 Action Skeleton 和选择审计，但不包含 Dify 请求本身。构建请求时必须重新计算 Findings checksum 和 Action Plan checksum，不能只信任调用方传入的声明。相同 Findings、Action Plan 和目录版本必须生成字节稳定的同一请求。

## 7. 受控事实原子

每个 `CopyFact` 至少包含：

- 稳定 `fact_id`；
- 所属 `action_id`；
- `source_finding_ids`：Finding 字段必须恰好一个，Action 字段可以为空；
- `source_path`：明确来自 Finding、Action target、definition of done 或 required limitation；
- `fact_type`；
- `value`；
- 可用于哪些句式槽位；
- 是否带有限制条件。

事实只从 V22-033 `copy_requirements.allowed_fact_fields` 允许的字段，以及同一 `copy_requirements.required_limitations` 生成：

- Finding statement、scope、severity、confidence；
- 已验证目标 URL、query、page type、GBP field 或 site；
- Action definition of done；
- `required_limitations` 对应的后端固定限制语。

Finding 字段的 `source_finding_ids` 必须与该 Finding 完全一致；target 与 definition-of-done 字段沿用 Action Skeleton 已绑定的 Finding 范围；required limitation 是行动级事实，不伪造单一 Finding 来源。

`fact_id` 基于合同版本、action ID、finding ID、字段路径和规范值生成稳定摘要，不依赖输入数组顺序、系统日期、随机数或 Dify。

后端不把推断出的同义词、数字换算、URL 简写、商家名称补全、地名补全或排名承诺生成新事实原子。

## 8. 句式目录与槽位

句式目录由后端版本控制。每个 `CopyPattern` 定义：

- 稳定 `pattern_key`；
- 用途：`why_now` 或 `client_facing_explanation`；
- 允许使用的 Action `template_key`；
- 有序槽位及每个槽位允许的 `fact_type`；
- 是否必须绑定 limitation；
- 后端持有的英文模板；
- 可发送给 Dify 的安全选择说明，只描述句式用途而不新增业务事实；
- 渲染后的长度上限。

示意：

```text
why_now.confirmed_gap.v1
"This action is timely because {finding_statement}"

explanation.target_and_done.v1
"Address {target} so that {definition_of_done}"
```

真实目录中的标点、大小写、冠词和连接词由后端固定。Dify 只能返回 `pattern_key` 和每个槽位的 `fact_id`，不能返回或覆盖模板文本。

目录只提供与当前 Action 模板兼容的句式。新增或修改句式必须提升 `copy_catalog_version` 并增加合同测试。

## 9. 输出合同 `CopyResponseV1`

Dify 输出至少包含：

- `schema_version = "v22_copy_response_v1"`；
- 与请求一致的 `copy_contract_version`、`copy_catalog_version` 和 `language`；
- 原样返回的 `action_plan_checksum`；
- `actions`：恰好三项，顺序与请求一致。

每个 `CopyActionSelection` 只包含：

- `action_id`；
- `sequence`；
- `why_now`：一个 `PatternSelection`；
- `client_facing_explanation`：一个 `PatternSelection`。

每个 `PatternSelection` 只包含：

- `pattern_key`；
- 按句式定义命名的 `slot_bindings`；
- 每个槽位绑定一个有序 `fact_id` 列表，目录为该槽位规定最小/最大数量和允许的 fact type。

列表式槽位允许完整绑定一个行动的多个 required limitations 或多个已确认目标；Dify 不能通过只选第一项来丢掉合同要求保留的限制。

所有响应模型使用严格类型和 `extra=forbid`。响应中不允许出现任意自由文本、模型解释、Markdown、合同 ID 之外的数字、URL、额外事实、候选列表或修改后的 Action Skeleton。

## 10. 校验和安全复核

后端按固定顺序执行：

1. 解析 Dify workflow outputs 中唯一允许的响应键；兼容该键值是原生对象或合法 JSON 字符串，但不接受 Markdown fenced JSON。
2. 严格校验 schema、合同版本、目录版本和英文语言。
3. 验证 `action_plan_checksum` 与本次请求相同，防止迟到响应或跨任务响应被误用。
4. 验证恰好三项行动，sequence 为 1、2、3，action ID 与请求逐项一致、唯一且无遗漏。
5. 验证句式存在、用途正确且兼容当前行动模板。
6. 验证槽位名称和数量完全一致。
7. 验证每个 fact ID 属于当前行动、当前 Finding 范围，并且 fact type 与槽位匹配。
8. 验证所有 required limitation 均被允许的句式或绑定事实覆盖。
9. 只用后端目录和后端事实渲染文本；规范化空白，但不重写事实值。
10. 验证两段结果非空、长度合规，并确认所有连接语都来自英文句式目录。后端事实值作为不可改写的原始字面值保留，因此合法商家名、查询词或地点可以含非英文字符。
11. 将渲染结果绑定回 action ID，并比较输入 Action Plan 的规范摘要，确认事实、顺序、步骤、目标、依赖、指标和日期未改变。

由于响应合同中不存在自由文本值，Dify 无法提交修改后的数字、URL 或无证据事实。schema/version/action/fact 等稳定 ID 中允许合同规定的数字字符；除此之外，任何数字、URL、中文或其他自由文本注入都会表现为额外字段、未知 ID、未知句式或错误槽位，并在渲染前被拒绝。

## 11. 最终内部结果

`PublicActionCopyResult` 至少包含：

- `schema_version = "public_action_copy_result_v1"`；
- 合同和目录版本；
- `findings_checksum` 与 `action_plan_checksum`；
- `actions`：恰好三项 `RenderedActionCopy`；
- 每项包含 action ID、sequence、最终 `why_now`、最终 `client_facing_explanation`、使用的 pattern key 和 fact IDs；
- `attempt_count` 只由编排层在成功时记录，不进入事实校验和。

本轮不直接构造正式 `TopAction`，避免提前扩大到完整报告组装。后续阶段可根据 action ID 把这两个已验证字符串合并进对应 Action Skeleton。

## 12. 错误模型与有限重试

错误分为两类：

### 12.1 可重试

- Dify 临时网络、超时、限流或受支持的临时服务错误；
- workflow 没有 outputs；
- JSON、schema、版本、校验和、行动覆盖、顺序、句式、槽位、事实引用、语言或长度无效；
- Dify 夹带自由文本、未知 ID、额外字段、合同 ID 之外的数字或 URL。

这些错误使用稳定代码，例如：

- `V22_COPY_PROVIDER_TRANSIENT`；
- `V22_COPY_OUTPUT_MISSING`；
- `V22_COPY_OUTPUT_INVALID`；
- `V22_COPY_CHECKSUM_MISMATCH`；
- `V22_COPY_REFERENCE_INVALID`；
- `V22_COPY_LANGUAGE_INVALID`。

### 12.2 不可重试

- 后端输入 Findings/Action Plan 本身无效；
- Findings checksum 与 Action Plan 声明不一致；
- Action Skeleton 使用未知模板或目录版本；
- 合法事实/句式不足以构造请求；
- 调用方配置的尝试次数不在 1 到 3 范围内；
- Dify 鉴权、权限、请求配置或其他明确的永久性 provider 错误。

这些错误代表本地合同或版本问题，重复调用 Dify 不会修复。

### 12.3 重试语义

- 默认且最大 `max_attempts = 3`；“3”表示总共三次调用，不是首次调用后再重试三次。
- 每次都使用字节相同的 `CopyRequestV1`；Dify 不能在失败后获得扩大的事实范围。
- 测试注入零等待策略；生产接线阶段可以提供有上限的退避策略。
- 三次均失败后抛出最终 v2.2 copy error，保留最后错误代码和各次脱敏分类，不返回部分 `PublicActionCopyResult`。
- 不调用 v2.1 fallback，不以空字符串、固定通用句、模型原文或推测内容填充文案。

错误信息不能包含 Finding statement、URL、query、地址、电话、事实值、Dify 原始输出、密钥或内部堆栈。日志只记录 task/case 的安全标识、attempt、稳定错误码、合同版本和数量信息。

## 13. 资源限制

请求与响应模型设置可测试下调的硬上限：

- actions 必须恰好三项；
- 每项 facts、patterns 和 fact references 数量；
- pattern slots 数量；
- 单个事实值和渲染文案字符数；
- 请求、原始响应和渲染结果的规范 JSON 字节数；
- 错误详情条数；
- 尝试次数最多三次。

在解析完整深层结构前先限制原始响应字节数。任何超限结果按可重试无效输出处理，不能被截断后继续使用。

## 14. 测试与验收

至少覆盖：

1. 同一个 Action Plan 重复构建得到完全相同的请求、事实 ID 和校验和。
2. Findings 或 Action 输入数组顺序变化不影响规范请求。
3. 正常响应渲染恰好三项非空英文文案。
4. 未知、跨行动、跨 Finding 或重复 fact ID 被拒绝。
5. 行动缺失、重复、乱序、错误 sequence 和未知 action ID 被拒绝。
6. schema、合同版本、目录版本、语言或 checksum 不匹配被拒绝。
7. 未知句式、错误用途、错误 Action 模板、缺少/多余槽位和 fact type 不匹配被拒绝。
8. 必需 limitation 未覆盖被拒绝。
9. 自由文本、Markdown、额外字段、合同 ID 之外的数字、URL 或修改后的事实值注入被拒绝。
10. 错误语言标识、Dify 注入的非英文文本、空文案、超长事实、超长响应和超长渲染结果被拒绝；后端已有的非英文专有事实值保持原样。
11. 前两次输出无效、第三次有效时成功，结果记录 attempt count 为三。
12. 连续三次合同失败或临时 provider 失败后明确终止，不返回部分或兜底文案。
13. 本地输入错误不调用 provider，也不重试。
14. 合并前后 Action Skeleton 的 action ID、顺序、finding IDs、目标、步骤、requirements、素材、依赖、owner、effort、完成标准、指标、数据源和日期完全不变。
15. v2.1 copy contract 和现有 Dify pipeline 行为不变。
16. 后端完整测试集通过，前端、API、Worker、生产开关和部署状态不变。

## 15. 完成标准

满足以下条件即视为 V22-034 本轮完成：

- 可以从有效 Public Findings 和三项 Action Plan 构建最小、稳定、可审计的英文文案请求；
- Dify 只能选择后端版本化句式和当前行动允许的事实 ID；
- 后端能拒绝无证据引用、数字/URL 注入、事实跨行动使用、行动不足/重复和任何合同漂移；
- 后端安全渲染恰好三项 `why_now` 与 `client_facing_explanation`；
- 文案失败最多尝试三次，耗尽后明确失败且没有任何事实或文案 fallback；
- Action Skeleton 的所有非文案内容保持不变；
- 模块可由后续总执行器注入真实 Dify provider，而无须改变事实合同；
- 正式报告、Worker、前端和生产配置保持不变。
