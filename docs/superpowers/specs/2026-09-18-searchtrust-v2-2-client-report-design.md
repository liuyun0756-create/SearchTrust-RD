# SearchTrust v2.2 — Advisor / Client 报告分层交付设计

日期：2026-09-18

范围：将当前仍带有技术诊断语言的 Client 视图，改为可直接发给客户、无需顾问讲解也能理解的独立报告；完整证据链仍保留在 Advisor 报告。

## 1. 背景与问题

现有前端已有 Advisor 和 Client 两种页面结构，但 Client 视图直接复用 `client_summary`、`top_actions.client_facing_explanation`、`why_now` 和 `roadmap` 中的技术化文案。真实生产报告因此会出现：

- 一句行动中包含大量 URL、完成条件和限制语；
- 错误码、数据采集限制和技术诊断直接暴露给客户；
- 客户很难先理解业务判断，再理解为什么和怎么做；
- Client 网页、分享链接和 Client PDF 虽然有独立入口，但内容边界缺少系统级保护。

本设计的目标不是降低证据要求，而是将同一份已验证报告分成两种明确产品语言：

- Advisor：完整诊断、完整证据、完整实施细节；
- Client：业务判断、少量代表性证据、三项行动和客户配合项。

## 2. 已确认决策

- Client 报告以“顾问直接发给客户，客户可独立阅读”为主要场景。
- 不做只有结论的黑盒；保留 1–3 张代表性证据卡，每张最多绑定 1–2 条真实 Finding。
- Client 报告收敛为四个核心部分：核心判断、代表性证据、三项优先行动、90 天计划与客户配合项；最后增加简短覆盖附录。
- 采用“后端独立生成 + 前端强制投影”的双重约束，不依赖前端对 Advisor 文案的字符串截断。
- Client 内容不能创造新事实；每一项结论必须可追溯到现有 Finding、Evidence 或 Action。
- 不回填 Advisor 原文。Client 内容缺失或未通过安全校验时，整份报告生成失败。
- 不兼容或回填旧报告。线上没有真实用户，现有测试报告不得促生长期兼容代码。

## 3. 非目标

- 不改变 Advisor 报告的规则、证据、八层诊断、数据覆盖或证据抽屉。
- 不降低 Prospect 或 Verified 报告的事实、引用、隐私和因果边界。
- 不将 Client 报告改成无证据的销售文案，也不承诺排名、流量、线索或收入结果。
- 不在本轮重设 Advisor 页面视觉。
- 不建立旧报告自动迁移或二次文案生成任务。

## 4. 报告信息架构

### 4.1 核心判断

首屏只回答三个问题：

1. 现在最重要的决策是什么；
2. 这个问题对当前业务意味着什么；
3. 如果按顺序解决，可以获得什么机会。

不在首屏显示 URL、Finding 语句、验收规则、数据限制列表或实施步骤。

### 4.2 代表性证据

显示 1–3 张证据卡，优先覆盖不同的决策来源，例如站点、市场或公开 GBP。每张卡片包含：

- 客户可读的证据类型；
- 一句可验证现象；
- 一句与当前决策的关系；
- 1–2 个 Finding ID 和必要的 Evidence ID，仅用于内部追溯，不直接展示。

卡片不显示规则 ID、规则版本、置信度、原始错误码或 URL。只有当单个页面本身是理解证据所必需的对象时，才能显示不包含协议、域名或查询参数的简短页面标签。

### 4.3 三项优先行动

按已验证的 TopAction 顺序展示恰好三项。每项只显示：

- 简短行动名称；
- 为什么现在做；
- 可验收的结果，但不使用排名或收入承诺；
- 目标时间或复查日期；
- 工作量档位；
- 客户需提供的确认或资产。

完整 `exact_targets`、`implementation_steps`、`specification`、`validation_metrics`、`dependencies`、`data_sources` 和 `definition_of_done` 只在 Advisor 报告。

### 4.4 90 天计划与客户配合项

保留 1–30、31–60、61–90 天顺序，但每个阶段只使用一个客户可读目标和一个简短验收结果。不复用当前包含 URL 列表和限制语的 `roadmap.objective`。

三项行动的 `required_client_assets` 去重后汇总为客户配合清单，并显示下一个复查日期。

### 4.5 简短覆盖附录

附录只说明：

- 本次使用了哪些来源；
- 哪些来源没有连接或不可用；
- 这对结论边界有什么影响。

后端必须将内部限制转换为客户语言。例如 `SERP_LOCAL_PACK_SECTION_INVALID` 不得直接显示，可转换为“本次未能完整验证所有本地搜索结果，因此不将该部分用作排名原因。”

## 5. 后端合同与生成流程

### 5.1 独立 `client_delivery`

ReportV22 增加必填 `client_delivery`，最小结构为：

- `decision`：`headline`、`business_impact`、`opportunity`；
- `evidence_cards`：1–3 项，包含 `source_label`、可选 `subject_label`、`observation`、`decision_relevance`、`finding_ids`、`evidence_ids`；
- `priority_actions`：恰好三项，包含 `action_id`、`sequence`、`title`、`why_now`、`expected_result`、`effort_bucket`、`review_date`、`required_client_assets`；
- `roadmap`：恰好三个阶段，包含 `period`、`objective`、`expected_result`、`action_ids`；
- `coverage_appendix`：`checked_sources`、`unavailable_sources`、`boundary_summary`；
- `next_review_date`。

Advisor 依然读取完整 ReportV22 字段；Client 展示层只读取 `identity`、最小报告头信息和 `client_delivery`。

这是必填字段的破坏性合同变更。ReportV22 `schema_version` 从 `2.2.0` 升为 `2.2.1`；产品版本仍为 v2.2。所有 JSON Schema、Pydantic 模型、前端生成类型、fixture、API 文档和金色报告必须同步升级，不允许一部分仍接受 `2.2.0`。

### 5.2 显示合同上限

下列上限按 Unicode 字符数计算：

- `decision.headline` 最多 160；`business_impact` 和 `opportunity` 各最多 320；
- `evidence_cards` 数量 1–3；`source_label` 最多 40，`subject_label` 最多 100，`observation` 和 `decision_relevance` 各最多 240；
- 每张证据卡绑定 1–2 个 Finding ID，每个 Finding 最多选择 2 个 Evidence ID，单卡 Evidence ID 总数最多 4；
- `priority_actions` 恰好 3 项；标题最多 120，`why_now` 和 `expected_result` 各最多 240，单项客户资产最多 3 条且每条最多 200；
- `roadmap` 恰好 3 项；每个阶段的 `objective` 和 `expected_result` 各最多 180；
- `checked_sources` 和 `unavailable_sources` 各最多 4 项，每项最多 80；`boundary_summary` 最多 320；
- 所有 Client 可展示文本中禁止原始 `http://` 或 `https://` URL。如果证据必须区分页面，只使用不含协议、查询参数和域名的 `subject_label`。

### 5.3 可追溯性不变式

- 每张证据卡的 Finding ID 必须存在，且必须有至少一条可用 Evidence。
- Evidence ID 必须存在于报告的 `evidence_index`，且必须被同一张卡的 Finding 引用。
- 每项 Client 行动的 `action_id`、顺序、工作量、复查日期和客户资产必须与对应 TopAction 一致。
- Client 路线图的 action refs 必须与 TopAction 和完整路线图一致。
- Client 文案不得引入原 Finding、Evidence、Action 中不存在的数字、页面、查询、竞品、因果或结果。

### 5.4 受控文案

继续遵守 v2.2 现有受控文案原则：文案生成只能选择已批准模板和已验证事实原子，不能改写事实。

本次新增 Client 专用文案用途与长度限制，使“客户交付文案”与“Advisor 实施说明”在数据模型上分离。不再把 `client_facing_explanation` 同时当作客户行动标题、完成定义、限制语和路线图目标的承载容器。

## 6. Client 安全校验

后端在组装正式 ReportV22 前执行安全校验：

- 可追溯性、顺序和引用不变式；
- 各字段长度、列表数量和总字符上限；
- 禁止规则 ID、版本号、Finding/Evidence 内部 ID、原始错误码和调试标识进入可展示文案；
- 禁止一个字段中出现多个 URL 或以分号串联长目标列表；
- 禁止未经支持的因果、排名承诺、流量承诺、线索承诺和收入承诺；
- 禁止把未检查、未连接或部分数据描述为正常、通过或“零问题”。

前端再执行第二层保护：`ClientReportV22ViewModel` 不包含 Advisor 专用字段，Client 组件无法接收 Findings、Evidence 原文、规则信息或完整实施规格。页面、分享链接和 PDF 都复用这个投影。

## 7. 前端组件边界

Client 交付由以下小组件构成：

- `ClientDecision`：核心判断、商业影响和机会；
- `ClientEvidenceCards`：1–3 张代表性证据卡；
- `ClientPriorityActions`：恰好三项客户可读行动；
- `ClientRoadmap`：90 天顺序和验收结果；
- `ClientInputs`：去重的客户配合项与下次复查日期；
- `ClientCoverageAppendix`：客户语言的覆盖边界。

Client 左侧导航只保留与这些部分一致的条目。头部明确显示 `Client report`，顾问自有页面仍可切换 Advisor / Client；公开分享页不出现 Advisor 切换或 Advisor 专用控件。

Client PDF 必须使用同一 `ClientReportV22ViewModel`，不建立第二套文案组装逻辑。

## 8. 失败处理

- `client_delivery` 无法生成、引用不存在、文案越界或结构不合格时，整个报告任务失败。
- 失败沿用现有预留点数流程：已预留的 1 个点数退回，用户可再次生成。
- Client 页面、分享链接和 PDF 遇到缺失或非法结构时，显示统一的“此报告需要重新生成”状态，不使用 Advisor 原文兜底。
- Advisor 报告不因 Client 展示失败而改写或丢失证据；但本次产品定义要求一份正式报告同时完成两种交付，因此任务仍整体标记为失败。

## 9. 旧数据策略

不为没有 `client_delivery` 的旧 ReportV22 构建兼容投影。发布时：

1. 先发布后端合同、组装器和工作器；
2. 再发布前端 Client 投影、页面、分享与 PDF；
3. 清理或废弃当前测试报告，不进行回填；
4. 使用新合同重新生成一份生产验证报告。

清理范围必须仅针对已确认的测试 Case、Report、Job、Share 和与其绑定的生成数据；不删除账号、付款审计或点数账本。

## 10. 测试与验收

### 10.1 后端

- ReportV22 schema、Pydantic 模型、fixture 和 API 合同包含完整 `client_delivery`。
- Prospect 和 Verified 组装器均生成合格 Client 内容。
- 代表性证据选择在相同输入下可重现，引用完整且不越界。
- 安全校验拒绝规则 ID、版本号、错误码、过多 URL、长列表、无根据因果和结果承诺。
- 缺失引用、错误 action 顺序、不一致复查日期、非三项行动和非三阶段路线图都明确失败。
- Client 组装失败使报告 Job 失败，并通过现有点数结算流程退回点数。

### 10.2 前端

- Client view model 的序列化结果不包含 Findings、Evidence 原文、规则信息或 Advisor 实施规格。
- Client 页面只渲染已批准的四段式结构和简短附录。
- Client 页面、分享链接与 Client PDF 使用同一文案和数据顺序。
- Advisor 页面与 Advisor PDF 仍包含完整 Findings、八层诊断、Coverage 和证据追溯。
- 公开 Client 分享页不包含 Advisor 切换、Advisor 文字或内部证据 ID。
- 无效或缺失 `client_delivery` 时不可降级渲染技术内容。

### 10.3 生产验证

- 使用生产前后端和真实数据生成一份新 Prospect 报告。
- 确认成功任务消耗 1 个点数；另使用可控失败路径确认点数退回。
- 逐项对比 Advisor 和 Client，确认 Client 无规则 ID、原始错误码、长 URL 列表、八层诊断或证据抽屉。
- 验证 Client 分享链接、Client PDF 和 Advisor PDF。
- 确认生产错误日志不记录客户证据原文或密钥。

## 11. 发布顺序

1. 实现并验证后端 `client_delivery` 合同、选择器、受控文案和校验器。
2. 更新前端合同类型与严格 Client view model。
3. 实现 Client 网页、分享页和 PDF 组件，保留 Advisor 交付。
4. 在本地完成后端、前端、合同、PDF 和 E2E 测试。
5. 先部署后端工作器，再部署前端。
6. 清理已确认的测试报告数据，不动账号、支付审计和点数账本。
7. 执行一次生产全链路验收。

## 12. 成功标准

客户打开 Client 报告后，不需顾问讲解就能回答：

1. 最重要的问题是什么；
2. 为什么相信这个判断；
3. 接下来按什么顺序做哪三件事；
4. 自己需要提供什么；
5. 什么时候复查。

同时，顾问仍可在 Advisor 报告中找到每个结论的完整规则、证据和实施边界。
