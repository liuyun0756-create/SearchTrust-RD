# V22-031 快照证据索引完成记录

日期：2026-08-31

状态：已完成本地实施与回归；未推送、未部署，尚未接入生产分析执行器。

依据：已批准的 evidence-index-design 与 evidence-index-implementation-plan。顺序实施，未启动子代理。

## 1. 交付内容

- 新增严格内部输入、快照绑定、逻辑选择器、来源追踪、来源摘要、覆盖缺口及构建结果模型，冻结报告合同没有增加字段。
- 新增纯函数 `app.report_v22.evidence.build_evidence_index`。输入含固定 `evaluated_at`，不读取时钟、网络、数据库或 Redis；先验证所有来源，再提取证据。
- 以 `v22_evidence_identity_v1` 和完整 SHA-256 生成稳定证据编号，区分标量类型、快照、查询/坐标/设备、竞品、维度与指标单位；同编号内容冲突报错，完全重复观察合并物理路径。
- 校验完整公开快照摘要、第一方 normalized_payload 摘要、当前 case/站点/市场/竞品上下文及快照元数据。同一轮内同 UUID 对应不同完整来源对象时拒绝。
- 接入客户站点、SERP、竞品与 GSC/GBP/GA4。只读取已有观察或指标，不新增采集、不估算或补零；竞品公开 GBP 仍标为 competitor。
- 页面文本只复用旧版 `build_evidence_ledger` 的提取入口，不复用旧顺序编号或规则结论。保留 240 段/360 字符上限并披露，原始响应字节摘要不以解码 HTML 重新验证。
- 过期、第一方不健康或身份不匹配时只输出覆盖证据。无快照只生成内部缺口，不造快照 UUID。真实空响应、部分覆盖和零指标分别处理。
- 内部 trace 保存完整逻辑上下文与物理 JSON 路径；长查询和 tablet 保留在 trace，不能容纳的冻结 locator 字段留空并披露限制。
- 输出按编号/路径/限制确定性排序，最多 50,000 条唯一证据、完整结果最多 20,000,000 字节，超限失败、不截断；保留全部来源限制，单条 Evidence 限制最多 20 项。
- 新增独立样例导出器和前端 EvidenceItem 合同测试，不改变前端运行时校验器。前端只接收生成的证据及 manifest，不复制原始快照。

## 2. 使用入口及可信边界

应用接入时先创建严格内部输入，再调用构建器：

```python
from app.report_v22.evidence import build_evidence_index
from app.report_v22.evidence_models import EvidenceBuildInput

request = EvidenceBuildInput.model_validate_json(authorized_input_json)
result = build_evidence_index(request)
# 后续组装器只将 result.evidence_index 放入冻结报告。
# source_traces/source_summaries/coverage_gaps 是内部数据，不是报告根字段。
```

`authorized_input_json` 必须由可信应用边界提供，不能把任意浏览器提交的 UUID 或身份标签直接视为真实绑定。当前模块不证明数据库记录存在，也不执行租户授权；持久化接入时仍须验证 case 归属、真实快照 ID、资源授权和共享市场使用权限。

公开绑定 `fetched_at` 对应快照 `completed_at`，版本及摘要必须匹配；第一方绑定的 UUID、获取/过期时间、版本、健康/身份及摘要须与 envelope 一致。来源适配器是内部实现，外部应调用构建入口以执行完整资格检查。

竞品 collection 必须与本轮提供的对应 SERP 绑定配套，核对 market_snapshot_id/checksum 后才能验证市场上下文。共享 SERP 可附 `shared_snapshot`：保留原 UUID、原 source job 和过期信息，核对其快照内容；不以当前 job 重新命名。共享授权仍由调用方负责。

有载荷但无绑定为错误；明确缺少的来源使用 `missing_sources`。同一来源类型不能一边提供快照、一边声明没有快照；竞品内部部分缺失由 collection 的来源状态表达。未提供 expires_at 不自行猜测时效。

五类固定错误：`V22_EVIDENCE_SOURCE_INVALID`、`V22_EVIDENCE_SNAPSHOT_BINDING_INVALID`、`V22_EVIDENCE_CHECKSUM_MISMATCH`、`V22_EVIDENCE_ID_CONFLICT`、`V22_EVIDENCE_LIMIT_EXCEEDED`。公开错误说明不回显来源数据。

## 3. 样例与验证

三个输入均为固定时区、固定 UUID 的虚构样例；manifest 明确标记 synthetic_snapshots。UUID 不代表已持久化记录，上游请求/响应标识为模拟值；持有的完整输入与绑定摘要真实按规范化载荷计算。

| 样例 | 生成 Evidence 数量 | 覆盖内容 |
| --- | --- | --- |
| public | 88 | 客户页面、三个查询、三个确认竞品、评论/回复及覆盖缺口 |
| verified | 6 | GSC/GBP/GA4 的 aggregate、row 与实际零值 |
| gaps | 1 | 有快照的不健康覆盖；另有无快照/未连接内部缺口，不生成假 Evidence |

最终验证：

- 后端新增证据模型、身份、绑定、四类适配器、构建器和导出器测试：73 passed。
- 后端完整 pytest：788 passed。
- 前端合同目录测试：172 passed；完整 Vitest：214 passed，12 个测试文件。
- 前端非增量 TypeScript 检查通过。
- 两端冻结合同导出、V22-030 辅助样例、新证据样例只读一致性检查通过。
- 冻结 TypeScript 类型重新生成与差异检查通过，无类型变更。
- 新增与修改文件差异检查通过。既有 Node `module.register()` 弃用警告仍存在，不影响测试。

测试覆盖跨进程编号稳定性、重复路径合并、bool/int 冲突、篡改/错误归属、共享快照编号、六类来源过期边界、第一方身份与健康门槛、SERP 三类结果及四查询中三完整的局部失败、竞品嵌套页面、旧提取器限制、资源超限、只读导出/漂移/符号链接和无关文件保留。

前端 AJV 只证明生成证据符合冻结结构，不证明事实真伪；业务语义由后端明确预期测试验证。未执行 UI/浏览器验收或生产服务联调。

## 4. 复验命令

后端：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/export_v22_contracts.py --check --frontend-dir ../search-trust
.venv/bin/python scripts/sync_v22_validation_resources.py --check --frontend-dir ../search-trust
.venv/bin/python scripts/export_v22_evidence_fixtures.py --check --frontend-dir ../search-trust
git diff --check
```

前端：

```bash
npm run test:contract
npm test
npm run typecheck -- --incremental false
npm run contracts:check
git diff --check
```

仅在明确维护证据样例时去掉新导出器的 `--check`。导出器预检所有输入与输出，只操作三个预定 JSON 和独立 manifest；发现额外文件或不安全路径就停止，不整体替换目录。`--check` 不写文件，也不修改文件时间。

## 5. 未变更及下一步

- 报告/API 模型、冻结 Schema/类型、原合同 fixtures、V22-030 validation 资源、旧版 ledger 均未修改。
- 未修改配置、数据库、worker/executor、PDF 或 UI；两个 v2.2 开关默认仍为 false，worker 仍使用 `UnavailableV22Executor`。
- 未增加 OAuth、采集请求、快照持久化、Findings/Actions 或占位完整报告。
- 本轮离线能力完成，不代表线上分析已经可用。

下一里程碑为 V22-032 Findings：使用证据索引生成有引用的规则发现，仍须遵守冻结合同与后续批准范围。
