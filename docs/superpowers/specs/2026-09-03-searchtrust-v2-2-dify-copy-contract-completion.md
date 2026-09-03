# SearchTrust v2.2 Dify 客户文案合同完成记录

日期：2026-09-03

范围：V22-034 受控 Dify 文案合同、后端反篡改渲染与有限重试。

依据：

- [设计规格](./2026-09-03-searchtrust-v2-2-dify-copy-contract-design.md)
- [实施计划](./2026-09-03-searchtrust-v2-2-dify-copy-contract-implementation-plan.md)

## 已完成

- 新增严格 `CopyRequestV1` / `CopyResponseV1` / `PublicActionCopyResult` 及事实、句式、槽位模型，全部拒绝额外字段与类型偷换。
- 新增版本化英文句式目录，覆盖当前五类公共行动的 `why_now` 与 `client_facing_explanation`。
- 从已验证 Findings、Action targets、definition of done 和 required limitations 构造稳定事实原子与 Fact ID。
- 对完整规范化 Action Plan 绑定 checksum；重新执行 V22-033 并逐字节比较，拒绝步骤、指标、日期或 copy requirements 的本地篡改。
- Dify 响应只能返回 action ID、pattern key 和 fact ID 槽位绑定，不能提交任意客户文案、数字、URL 或新增事实。
- 后端验证合同/目录版本、英文语言、checksum、恰好三项行动、顺序、句式用途、槽位类型、跨行动引用及全部目标、完成标准和限制语覆盖。
- 最终文案只由后端句式和后端事实渲染；原始商家名、查询词、URL、数字和非英文专有事实值保持原样。
- 原生对象和合法 JSON 字符串均受支持；Markdown JSON、重复 JSON key、多余 workflow output、未知字段或自由文本全部拒绝。
- 新增注入式异步 provider 协议和最多三次调用的重试编排；每次请求字节一致。
- 临时网络、限流、服务或无效输出可有限重试；鉴权、权限、配置、本地合同错误立即失败。
- 重试耗尽只返回脱敏 `V22_COPY_RETRY_EXHAUSTED`，没有部分结果、v2.1 fallback、空文案或固定通用文案。
- 错误详情只保留固定字段路径和代码；攻击者自定义字段名、原始响应及客户事实不会进入错误对象。
- V22-033 Findings 规范化函数提升为下游可复用的内部公共函数，原行动结果保持不变。

## 新增与修改文件

- `app/report_v22/copy_models.py`
- `app/report_v22/copy_errors.py`
- `app/report_v22/copy_catalog.py`
- `app/report_v22/copy_contract.py`
- `app/report_v22/copy_adapter.py`
- `app/report_v22/actions.py`
- `tests/v22_copy_helpers.py`
- `tests/test_v22_copy_models.py`
- `tests/test_v22_copy_catalog.py`
- `tests/test_v22_copy_request.py`
- `tests/test_v22_copy_contract.py`
- `tests/test_v22_copy_adapter.py`

## 验证结果

相关测试：

```text
70 passed in 4.17s
```

完整后端测试：

```text
1366 passed in 14.43s
```

其他检查：

- `python -m compileall -q app/report_v22` 通过；
- `git diff --check` 通过；
- 不同 `PYTHONHASHSEED` 下 CopyRequest 输出一致；
- SearchTrust-RD 改动仅包含本轮 v2.2 copy 模块、测试及 `actions.py` 规范化函数可见性调整；
- search-trust 前端工作目录干净，当前提交仍为 `49f2436`；
- `V22_ANALYZE_ENABLED`、`V22_PREFLIGHT_ENABLED` 和 `V22_COMPETITOR_DISCOVERY_ENABLED` 仍为 `False`。

## 保持未接入

- 未调用真实 Dify 网络或读取 Dify 密钥；
- 未接入正式 ReportV22、ExecutiveDecision、Roadmap 或 ClientSummary 组装；
- 未修改 Worker、Redis checkpoint、HTTP API、前端或 PDF；
- 未开启生产开关；
- 未推送、未部署。

## 完成判定

V22-034 本轮设计与实施计划中的安全合同、事实绑定、反篡改渲染、最多三次重试、无 fallback 和完整回归要求均已满足。后续总报告执行器可以注入真实 Dify provider，但必须继续使用本合同并保持现有事实边界。
