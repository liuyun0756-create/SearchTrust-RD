# V22-032 首批公开 Findings 完成记录

日期：2026-08-31

状态：已完成批准范围的本地实施与回归；未推送、未部署、未接入生产执行器。完整 V22-032 尚未全部完成。

依据：同日 public-findings-design 与 public-findings-implementation-plan。设计和实施均经用户批准；顺序实施，未启动子代理。

## 1. 已交付

- 新增纯函数 `app.report_v22.findings.build_public_findings`、严格内部输入/结果模型、稳定安全错误、版本化规则目录、只读证据视图和汇总。
- 仅接受 prospect；拒绝第一方实际快照、不同同类快照及客户/竞品域名歧义。先重验全部绑定，再构建证据，不接受外部拼接的索引；沿用 V22-031 的篡改/归属错误。
- 六条规则：已保存 HTTP 400—599、明确 noindex、不同最终 URL 的归一化重复标题、当前 SERP 组未观察到客户域名、至少两家确认竞品位置领先、三个固定页面类型的采样覆盖差距。
- 市场按快照、查询、结果类型、坐标、国家、语言和设备隔离；只按 URL 域名确认身份，使用同一 rank_source 的最小实际位置，保留并列引用。第三家未知而未达到两家阈值时保持未检查。
- 资产差距依据当前库存内 checked/2xx/HTML 的真实零与正计数；全部有效客户/竞品库存的实际完成时间跨度不得超过 24 小时，不选择有利子集、不借用较晚的 collection 时间。
- site 适配器及嵌套 competitor site 复用路径新增四项派生计数，选择器包含 `eligible_html_counts_v1`，来源路径指向 pages/completed_at。旧观察内容与编号不变。
- Findings 编号为 `fn_` 加完整 SHA-256，身份版本 `v22_public_finding_identity_v1`；规则版本 `1.0.0`、规则集 `v22_public_findings_v1`。按目标/引用稳定编号，重复合并、冲突失败，不依赖随机数、时钟、遍历顺序或生成文案。
- 固定英文模板保留样本范围、完整来源限制、置信度上限及非空改变条件，不生成归因或排名改善承诺。
- 所有实际 Finding/比较证据/汇总引用进行完整性审查，含客户与竞品归属、市场上下文、计数类别、实际页面集群和八层状态检查。
- 上限：10,000 条唯一 Finding、20,000 条 RuleEvaluation、20,000,000 字节完整结果（含 evidence_result、汇总与元数据）。只允许下调，超限整体失败、不截断。

## 2. 覆盖与明确未交付项

每个目标分别记录 triggered / not_triggered / not_checked。只有 triggered 生成冻结 Finding；缺来源、过期、采样不足或不可比都不能成为“正常”或虚假发现。

客户站点缺失或不合资格时，各采样计数为 null，页面集群为空；可用市场仍独立生成发现。页面集群只来自实际 checked 的 SitePageType；跨集群标题发现引用同一个 ID，市场和资产差距不分配到虚构页面。

客户 GBP 的 name/address/phone/service_area 四项保留 not_checked/customer_public_gbp_missing。本轮没有客户真实公开 GBP 的实质对齐能力。

站点及所有集群的八层严格使用原 REQUIRED_LAYER_KEYS 顺序，全部 not_checked，层级 Finding/Evidence 引用为空，并明确 semantic_rules_not_implemented。技术和采样发现保留在外层，不能据此判断业务资格或八层整体优劣。

仍待后续：客户公开 GBP 绑定与对齐、八层语义规则、精确服务/城市资产语义、V22-033 行动及 V22-034 文案、第一方/跨源规则、完整报告组装、UI/PDF 和生产持久化/授权接入。

## 3. 入口与可信边界

```python
from app.report_v22.findings import build_public_findings
from app.report_v22.findings_models import PublicFindingsInput

request = PublicFindingsInput.model_validate_json(authorized_input_json)
result = build_public_findings(request)
```

输入绑定须由可信应用层提供；本模块不证明数据库记录存在或租户权限。使用输入 evaluated_at，不访问网络、数据库、Redis、LLM 或运行时时钟。RuleEvaluation、rollup 和完整 evidence_result 均为内部结果，不给冻结报告根节点新增字段，也不伪造占位报告。

新增固定错误为 V22_FINDINGS_INPUT_INVALID、V22_FINDINGS_REFERENCE_INVALID、V22_FINDINGS_ID_CONFLICT、V22_FINDINGS_LIMIT_EXCEEDED；公开说明不回显查询、URL 或原始载荷。

## 4. 双端样例

新增独立导出器 `scripts/export_v22_findings_fixtures.py`。后端保留输入，前端仅接收实际构建器导出的 Finding / EvidenceItem / LayerAssessment 片段和哈希 manifest。

| 样例 | Findings | Evidence | LayerAssessment | 独立语义期望 |
| --- | ---: | ---: | ---: | --- |
| triggered | 7 | 228 | 16 | 覆盖全部六条规则；站点及真实页面集群均无八层评级 |
| clear | 0 | 248 | 40 | 样本规则未触发，不代表业务或八层整体良好 |
| gaps | 0 | 1 | 8 | 过期来源只有覆盖证据、未知计数、全部未检查 |

LayerAssessment 数量是站点及所有真实集群的八层记录总数。全部输入为固定时间与虚构 UUID/域名，不代表已持久化客户数据。

V22-031 public 生成样例从 88 项增加为 92 项；新增仅为四项客户库存计数。其原始输入未变，固定 legacy-evidence-hashes 回归核对原 88 项内容及编号完全保留。verified/gaps 旧样例不变。

导出器预检全部输入及输出目录，拒绝额外文件、符号链接、越界路径；只操作预定文件，保留用户无关文件。`--check` 只读，不改变文件时间，不创建缺失目录。

## 5. 实际验证结果

- 新增后端 Findings/计数/边界/导出测试：96 passed。
- 后端完整 pytest：884 passed。
- 前端合同测试：189 passed；完整测试：231 passed，13 个测试文件。
- 前端非增量 TypeScript 检查通过。
- 冻结合同、V22-030 辅助资源、V22-031 证据样例、新 Findings 样例的双端只读一致性检查通过。
- 冻结类型重新生成及差异检查通过；没有合同/类型变更。
- 差异检查通过。现有 Node module.register 弃用警告仍在，不影响测试。

测试包括严格类型/额外字段、绑定篡改、过期、真实零、各规则阈值、跨上下文/排名基准、第三家未知、超窗第三家、404 竞品、单竞品多页、长查询、最低支持置信度、完整来源限制、跨进程稳定编号、输入不变、顺序/重复、同编号冲突、引用伪造、完整字节精确边界、集群/八层错误防护及安全导出。

前端 AJV 复用冻结 `$defs`，只证明结构与引用符合约束，不证明商业事实真伪；语义由后端明确期望测试覆盖。未运行 UI/浏览器验收或生产外部服务联调，不将这些项目写为通过。

## 6. 复验与未变更项

完整复验命令保存在同日 implementation-plan 第 6 节；新增导出检查为：

```bash
.venv/bin/python scripts/export_v22_findings_fixtures.py --check --frontend-dir ../search-trust
```

冻结报告/API/Schema/类型、v2.1、V22-030 validation/规范化资源、旧证据输入均未修改。未改采集器、数据库、配置、worker/executor、前端运行时、UI 或 PDF。

V22_ANALYZE_ENABLED / V22_PREFLIGHT_ENABLED 默认保持 false；V22_COMPETITOR_DISCOVERY_ENABLED 也保持 false；worker 仍使用 UnavailableV22Executor。没有新增采集或 OAuth 请求，没有推送、部署或打开生产分析开关。

结论：V22-032 已批准的第一批公开规则与覆盖汇总完成；完整 V22-032 及 v2.2 发布条件尚未完成。
