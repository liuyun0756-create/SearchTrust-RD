# SearchTrust v2.2 网站与公开 GBP 对齐完成记录

日期：2026-09-01

状态：已完成并通过本地全量验证；未推送、未部署、未开启生产开关。

依据：

- `2026-09-01-searchtrust-v2-2-site-gbp-alignment-design.md`
- `2026-09-01-searchtrust-v2-2-site-gbp-alignment-implementation-plan.md`

## 已完成范围

- `PublicFindingsInput` 增加可选用户确认 `BusinessIdentity`、网站候选限制和网站/GBP 对齐限制；未提供身份时继续使用原有覆盖分支。
- 实现 `site_business_selection_v1`，按页面类型、声明实体、确认名称/市场锚点、结构化印证和跨最终 URL 重复选择 eligible/unresolved/excluded 候选。
- 实现四个离线版本化比较器：名称、完整电话和分机、保守地址组件、服务区域集合。
- 实现 storefront/service_area/hybrid 适用性，以及 exact、semantic、partial、mismatch、单侧缺失、双侧缺失、not_applicable、not_checked 状态。
- 为网站/GBP 缺失生成 `site_gbp_alignment_v1` 专用 coverage Evidence，追踪到真实库存/HTML或 GBP 字段状态路径。
- 四项 GBP 规则在显式新分支使用 `1.1.0`，生成真实 RuleEvaluation、必要 Finding 和站点 roll-up；服务区域部分匹配为 low。
- 新对齐结果在进入 Findings 前从绑定原始输入独立重建；身份、checksum、引用、ID、时间差和资源上限均失败关闭。
- 增加 12 个前后端共享冻结场景和唯一导出/漂移检查脚本。

## 关键语义确认

- 名称、地址、电话有任一合资格网站候选匹配 GBP 即视为对齐；其他候选保留在内部审计结果中。
- 一侧缺失和双方缺失均为 Finding；只有来源或检查不可靠时才是 not_checked。
- 服务区域部分重叠与完全不重叠分别生成 low 和默认 medium Finding。
- 用户确认身份只界定客户实体、站点、经营模式和市场，不能覆盖网站或 GBP 的实际观察值。
- 候选资格从不使用 GBP 是否匹配作为依据。
- 未使用模糊匹配、LLM、联网补值、地理编码或任意包含关系。

## 兼容性

- `business_identity=None` 时四项旧 GBP Evaluation 仍为规则版本 `1.0.0`，旧 Evidence/Finding ID 与冻结 fixtures 保持不变。
- 冻结 HTTP API、ReportV22 Schema、TypeScript 报告类型和八层输出均未修改。
- 四项 GBP RuleSpec 单独保存在新目录中，不扩张旧 `RULES` 冻结集合。
- 前端只增加共享样例合同测试，没有运行时或界面变更。

## 固定共享场景

- exact_match
- semantic_match
- multiple_candidates_one_match
- site_missing
- gbp_missing
- both_missing
- service_area_partial
- service_area_mismatch
- operating_model_not_applicable
- identity_unresolved
- source_unavailable
- comparison_time_gap

## 验证结果

- 后端：`1261 passed`。
- 前端：`16` 个测试文件、`274 passed`。
- 前端 TypeScript：`tsc --noEmit` 通过。
- Python 模块编译检查通过。
- fixture `--check` 通过，重复生成无漂移。
- 两个仓库 `git diff --check` 通过。

## 明确未实施

- 生产执行器把 AnalyzeRequest.business_identity 传入 Findings 的接线。
- 数据库、HTTP API、后台任务持久化或供应商实时采集。
- UI、PDF、Dify、Actions 和八层语义映射。
- 发布、部署、生产特性开关或远程推送。

后续只有在生产执行器接线获得单独批准后，才应把已确认 `BusinessIdentity` 显式传入本轮 opt-in 路径，并再次进行端到端、持久化和发布前验证。
