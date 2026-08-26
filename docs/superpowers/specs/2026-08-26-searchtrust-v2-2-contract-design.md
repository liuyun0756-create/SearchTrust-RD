# SearchTrust v2.2 合同冻结设计

状态：已批准，等待实施计划

日期：2026-08-26

范围：V22-003

适用仓库：

- 后端：`SearchTrust-RD`
- 前端：`search-trust`

## 1. 摘要

V22-003 为 SearchTrust v2.2 冻结跨服务合同。后端 Pydantic 模型是唯一事实源，自动导出 JSON Schema、共享 fixture 和合同 manifest；前端根据 Schema 生成 TypeScript 类型，并使用 AJV 对运行时数据进行严格校验。

本阶段只建立合同和验证链路，不实现数据库迁移、公开数据采集、OAuth、持久任务队列、正式 `/api/v2` 路由或报告 UI。

## 2. 目标

- 冻结 `report_v2_2` 根结构、枚举、字段类型和跨字段不变量。
- 冻结 `/api/v2/preflight`、`/api/v2/analyze` 和任务接口的请求、响应模型。
- 让 Pydantic、JSON Schema 和 TypeScript 由同一合同源产生。
- 建立一份 prospect 和一份 verified 共享 fixture。
- 在前后端验证相同 fixture，并检测合同产物漂移。
- 保持 `/api/v1`、`report_v2_1`、历史报告和现有 PDF 路径不变。

## 3. 非目标

- 不创建或修改 Supabase 表。
- 不实现 Redis、ARQ、worker 或任务恢复。
- 不连接 GSC、GA4、GBP 或其他第三方 API。
- 不实现站点、SERP、竞品或 PageSpeed 采集。
- 不实现 v2.2 页面、支付流程、PDF 或分享链接。
- 不发布独立 npm/Python 合同包。

## 4. 方案选择

### 4.1 采用方案

采用 Pydantic 单一事实源：

1. 在后端定义严格 Pydantic 模型。
2. 由 Pydantic 导出 JSON Schema 2020-12。
3. 后端用 Pydantic 校验共享 fixture。
4. 前端从 JSON Schema 生成 TypeScript 类型。
5. 前端用 AJV 校验同一批 fixture 和运行时报告。

该方案延续后端现有 Pydantic 能力，同时避免 v2.1 中 Python 模型、手写 TypeScript 类型和手写前端验证器分别演进造成的漂移。

### 4.2 未采用方案

- JSON Schema 单一事实源：语言中立，但复杂 Pydantic 校验、错误信息和跨字段规则需要额外生成层。
- Python 与 TypeScript 手工维护：初期快，但难以保证大型长期合同的一致性。

## 5. 合同架构

### 5.1 后端目录

```text
app/report_v22/
  __init__.py
  models.py
  contract_version.py
app/api/v2/
  __init__.py
  models.py
scripts/
  export_v22_contracts.py
contracts/v2.2/
  report_v2_2.schema.json
  api_v2.schema.json
  manifest.json
  fixtures/
    prospect.json
    verified.json
tests/
  test_report_v22_contract.py
  test_api_v2_contract.py
  test_v22_contract_export.py
```

`app/report_v22/models.py` 只拥有报告正文合同。`app/api/v2/models.py` 拥有 API request/response 和任务 envelope，并引用报告模型。两者分开，避免任务失败时为了满足 envelope 而伪造空报告。

### 5.2 前端目录

```text
src/lib/report-v22/
  contracts/
    report_v2_2.schema.json
    api_v2.schema.json
    manifest.json
    fixtures/
      prospect.json
      verified.json
  generated/
    types.ts
  validate.ts
  contract.test.ts
```

`generated/types.ts` 是生成文件，不允许手工修改。`validate.ts` 负责 JSON Schema 结构验证和 Schema 无法表达的引用完整性验证。

### 5.3 版本

首个冻结合同版本为 `2.2.0`。每个报告和 manifest 都必须携带该版本。合同版本不等同于产品部署版本；任何破坏已提交 fixture 或消费者的字段变化都必须提升合同版本。

## 6. `report_v2_2` 根合同

下列根字段始终存在：

```text
report_v2_2
  identity
  case_context
  report_version
  data_coverage
  market_snapshot
  site_inventory_summary
  competitor_analysis
  first_party_performance
    gsc
    gbp
    ga4
  executive_decision
  eight_layers
  findings
  top_actions
  roadmap_30_60_90
  client_summary
  evidence_index
  version_diff
  limitations
```

所有模型使用严格模式和 `extra="forbid"`。正式合同不使用无约束的 `Any` 或任意 `Record<string, unknown>`。需要扩展的结构必须先定义可接受的键和值类型。

获客报告没有第一方数据时，`gsc`、`ga4` 和 `gbp` 节点仍然存在，并明确标记为 `not_connected`。Web、PDF、邮件和分享视图由此共享同一个 view model，不依赖删除字段或猜测字段是否存在。

## 7. 核心类型与不变量

### 7.1 通用规则

- `report_type` 仅允许 `prospect` 或 `verified_execution`。
- 时间使用带时区的 ISO 8601 UTC。
- 金额使用最小货币单位整数，不使用浮点金额。
- `queries` 包含 3 至 5 个规范化、非空、去重查询。
- 正式分析输入和报告必须恰好包含 3 个不同竞争对手。
- 无法形成 3 个真实竞争对手时，预检返回缺口；分析不得生成占位竞品。
- ID 列表不得重复。

### 7.2 Evidence

Evidence 至少包含：

- `evidence_id`
- `snapshot_id`
- `source_type`
- `source_locator`
- 原始值和规范化值
- URL、Property 或 Location 身份
- query、坐标、设备和语言上下文
- 获取时间和覆盖期
- confidence、health status 和 limitations

Evidence ID 根据来源类型、快照、定位信息和规范化值确定性生成。相同规范化输入必须产生相同 ID。缺失数据结论使用明确的 coverage Evidence，不能创建无证据 Finding。

### 7.3 Finding

Finding 至少包含可证伪陈述、Evidence IDs、Comparator IDs、规则 ID/版本、事实分类、严重度、范围、置信度、受影响 URL/查询、缺失数据和改变结论的条件。

每个 Finding 至少引用一个存在于 `evidence_index` 的 Evidence。Finding ID 在一份报告内唯一。

### 7.4 Top Action

- `top_actions` 必须恰好三项。
- `sequence` 必须依次为 1、2、3。
- 每项 Action 至少引用一个存在的 Finding。
- Action 必须包含实施目标、步骤、依赖、客户素材、负责人建议、工作量、完成定义、验证指标、数据来源、复查日期和客户说明。
- Action ID 在一份报告内唯一。

### 7.5 报告类型条件

- prospect 报告没有 `parent_report_id`，第一方来源状态为 `not_connected`，`version_diff` 标记为初始版本。
- verified 报告必须提供 `parent_report_id`。GSC、GA4 和 GBP 三个来源 envelope 必须全部存在，并各自引用一次主动同步产生的 snapshot；snapshot 可以标记为不健康或无数据，但不能缺席。
- `full_evidence_coverage` 只有 GSC、GA4 和 GBP 全部健康且身份匹配通过时才可为 `true`。
- 不健康、未匹配或过期来源可以在 coverage 和 limitations 中展示，但不能贡献 verified confidence。
- version diff 的每个变化必须包含分类、当前 Finding 引用、相关新 Evidence 和变化原因。除 `new` 外，还必须包含上一版本 Finding 的外部引用；`new` 不得伪造上一版本引用。

变化分类仅允许：`confirmed`、`reprioritized`、`refined`、`replaced`、`new`。

### 7.6 语义引用验证

Pydantic 模型级验证器和前端语义验证器都检查：

- Evidence、Finding、Action ID 唯一性；
- Finding 到 Evidence 的引用完整性；
- Action 到 Finding 的引用完整性；
- version diff 到当前 Finding 和当前 Evidence 的引用完整性；
- 上一版本 Finding 引用的 `report_id` 必须等于根 `parent_report_id`，并携带 `finding_id`、原陈述和稳定 fingerprint；
- Action 顺序及数量；
- report type、parent report、first-party coverage 的条件一致性。

## 8. `/api/v2` 合同

### 8.1 Preflight

`POST /api/v2/preflight` 输入站点 URL、可选 GBP URL、可选服务和地区。输出仅包含：

- 规范化站点身份；
- GBP、服务、地区和竞品候选；
- 匹配置信度；
- 可用数据模块；
- 数据缺口；
- 预计生成时间桶和用户可见的覆盖说明。

预检合同禁止返回 Finding、Top Action 或付费报告核心结论。

### 8.2 Analyze

`POST /api/v2/analyze` 输入：

- `case_id`；
- `report_type`；
- 已确认 business identity；
- primary service 和 target market；
- 3 至 5 个查询；
- 3 个已确认或系统锁定的竞争对手；
- 第一方 snapshot envelopes；
- verified 升级时的 parent report 引用；
- 有界 generation limits。

请求模型不定义 access token 或 refresh token 字段。严格未知字段校验确保误传 token 会被拒绝。

### 8.3 Task envelope

v2 统一使用 `job_id`，不与 v1 的 `task_id` 混用。任务状态仅允许：

- `queued`
- `running`
- `succeeded`
- `failed`

成功终态包含 `report_v2_2` 和安全诊断摘要。失败终态包含稳定 `error_code`、安全 `user_message`、`retryable`、当前阶段和诊断 ID，不包含供应商原始响应、客户敏感载荷或密钥。

## 9. 生成与同步

合同生成顺序固定为：

1. 导入 Pydantic 报告和 API 模型。
2. 生成 JSON Schema 2020-12。
3. 在内存中验证 prospect 和 verified fixture。
4. 使用稳定键排序和固定缩进序列化产物。
5. 计算 Schema 与 fixture 的 SHA-256。
6. 写入包含 `contract_version` 和哈希的 manifest。
7. 全部成功后原子替换后端正式合同目录。
8. 通过显式 `--frontend-dir` 同步合同包到前端。
9. 前端从 Schema 生成 TypeScript 类型。
10. 前端校验 manifest、fixture 和生成结果。

manifest 不包含生成时间、绝对路径或环境相关值，确保相同模型产生字节级相同的产物。

脚本不硬编码开发者本地绝对路径。两个仓库独立提交同一合同包，运行时不需要访问另一个仓库。

## 10. 前端运行时验证

前端使用 AJV 严格模式：

- 不转换类型；
- 不注入默认值；
- 不删除未知字段；
- 收集全部结构错误；
- 使用报告 Schema 对未知输入进行验证。

AJV 通过后，再运行 ID 唯一性和引用完整性语义检查。当前报告内的引用必须能在当前合同中解析。上一版本 Finding 是外部引用：前端验证其 `report_id` 与根 `parent_report_id` 一致并包含稳定 fingerprint；后端在生成报告时额外对照实际 parent report 验证该引用。`validateReportV22()` 返回判别式结果：成功时返回生成类型的报告，失败时返回稳定错误代码、字段路径和安全描述。

无效 v2.2 报告不进入正常渲染。前端显示“报告数据未通过完整性校验”和安全诊断 ID，不使用默认内容伪造报告。v2.1 报告继续走现有适配器。

## 11. 错误处理

稳定错误分类：

- `V22_SCHEMA_INVALID`
- `V22_FIXTURE_INVALID`
- `V22_CONTRACT_DRIFT`
- `REPORT_CONTRACT_INVALID`
- `REPORT_REFERENCE_INVALID`

导出写入临时目录。任何 Schema、fixture、哈希或同步失败都保留上一份完整合同，不留下部分更新产物。

错误只记录规则、字段路径和安全描述。日志、异常、manifest 和用户响应不得包含 Google token、供应商密钥、完整客户载荷或原始供应商响应。

## 12. 测试设计

### 12.1 后端

- prospect 和 verified 正向 fixture；
- 未知字段、缺失根字段和错误枚举；
- queries、竞争对手和 Action 数量边界；
- 重复 ID 和悬空引用；
-错误 Action 顺序；
- prospect 携带 verified 状态；
- verified 缺少 parent report 或第一方快照；
- Full Evidence Coverage 与来源健康状态矛盾；
- Schema 重复生成结果完全一致；
- manifest 哈希与实际文件一致；
- `/api/v1` 和 v2.1 合同回归。

### 12.2 前端

- AJV 接受两份共享正向 fixture；
- AJV 拒绝未知字段、错误枚举和缺失字段；
- 语义验证器拒绝重复 ID 和悬空引用；
- 生成 TypeScript 通过 `tsc --noEmit`；
- Schema、fixture 与 manifest 哈希一致；
- 重新生成 TypeScript 后 Git 不产生差异。

V22-003 建立最小 Vitest 合同测试基础。组件测试和 Playwright 仍属于 V22-090。

## 13. CI 门槛

后端 CI：

1. 运行 v2.2 合同测试。
2. 运行全部 v2.1 回归测试。
3. 重新导出合同。
4. 检查已提交合同目录没有差异。

前端 CI：

1. 验证合同 manifest。
2. 重新生成 TypeScript 类型。
3. 运行合同 fixture 和负向测试。
4. 运行 `tsc --noEmit`。
5. 检查合同与生成文件没有差异。

## 14. 验收标准

V22-003 只有同时满足以下条件才完成：

- Pydantic、JSON Schema、TypeScript 使用同一合同源；
- prospect 和 verified fixture 在前后端全部通过；
- 跨字段和引用不变量有正向与负向测试；
- API 模型无法接收 Google token；
- 合同导出可重复且产物字节稳定；
- 两个仓库的 manifest、Schema 和 fixture 哈希一致；
- 前后端类型、校验和回归测试通过；
- `/api/v1` 与 v2.1 行为未改变；
- 没有实现超出 V22-003 的运行时功能。

## 15. 后续顺序

合同冻结后按以下顺序继续：

1. V22-010 Supabase migration；
2. V22-011 Case API；
3. V22-012 Redis/ARQ 持久任务；
4. V22-020 Preflight API；
5. V22-021 站点库存。

GBP API 审批与 OAuth 验证材料属于外部关键路径，应与上述工程工作并行推进。
