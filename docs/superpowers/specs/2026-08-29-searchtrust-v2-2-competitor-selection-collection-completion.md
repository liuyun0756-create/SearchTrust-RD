# SearchTrust v2.2 V22-023 竞品选择与公开数据采集完成记录

日期：2026-08-29  
分支：`codex/v2.2-durable-jobs`

## 结论

V22-023 已按审批的设计和实施计划完成。系统现在具备独立可恢复的竞品发现任务、最多 6 个可追溯候选、恰好 3 家用户确认门槛、24 小时市场快照复用、每家 50/10 网站采集、每家最多 30 条公开评论，以及三家共享 15 次实际供应商尝试的持久预算。

正式 v2.2 生产分析仍然安全关闭：`V22_ANALYZE_ENABLED=false`、`V22_COMPETITOR_DISCOVERY_ENABLED=false`，Worker 仍装配 `UnavailableV22Executor`。本里程碑只将严格竞品快照暴露给后续 V22-031 真实执行器边界，未生成评分、优势、差距或伪造证据 ID。

## 已完成范围

1. 新增严格发现请求、状态、结果、错误、候选审计和竞品采集快照模型。
2. 实现强标识、域名和名称/地址递降合并，客户自身、目录/社交/聚合站和非本地结果过滤，共享官网抑制，以及 45/30/20/5 确定性排名。
3. 抽取 V22-022 通用市场上下文入口，用带校验和的 Redis 快照在 24 小时内复用相同市场搜索。
4. 实现独立 Redis 命名空间、ARQ 物理任务 ID、幂等、SSE revision、自动/手动重试、7 天状态保留和丢失心跳恢复。
5. 手动补充 URL 必须出现于当前市场快照，并经过单页受限官网身份/服务/市场校验。
6. 正式 `AnalyzeRequest` 正文保持冻结，通过 `X-SearchTrust-Discovery-ID` 校验过期、Case、上下文、候选身份和用户确认，内部信封参与幂等摘要。
7. 竞品站点采集每家独立检查点，硬上限 50/10，不读取 GSC 或其他竞品私有数据，单站失败可降级。
8. 公开 GBP/评论采集复用共享 SerpAPI 密钥轮换，仅跟随 `next_page_token`，过滤头像、用户主页、贡献者标识、照片和未知字段。
9. 最终 `CompetitorCollectionSnapshot` 严格对齐三家竞品、市场快照、来源健康、实际页数和成本计数。

## 验收与验证

- V22-023 专项：52 项通过。
- SERP 市场、站点库存和 SerpAPI 回归：121 项通过。
- GBP、预检和正式任务 API 回归：132 项通过。
- 全量 Python 测试：563 项通过。
- `scripts/export_v22_contracts.py --check`：通过，冻结报告/API/TypeScript 导出无差异。
- `python -m compileall -q app tests`：通过。
- `git diff --check`：通过。
- 自动化测试全部使用脱敏固定数据和替身，未发起真实 SerpAPI、Firecrawl 或外部网站请求，未消耗供应商额度。

## 验收标准核对

设计文档第 19 节的 15 项验收标准均已在代码边界和自动化测试中覆盖。其中“无 Google 授权”路径通过纯公开站点/SERP/GBP 数据替身验证，不依赖 GSC、GA4 或 Google Business Profile OAuth。真实供应商冒烟验证需在后续开启预发现功能开关、配置 Redis/SerpAPI 后执行，不属于本次默认关闭的生产发布动作。

## 提交记录

- `6444d86` `feat(v2.2): define competitor discovery contracts`
- `14ff63c` `feat(v2.2): rank traceable competitor candidates`
- `1cc9258` `feat(v2.2): reuse expiring market discoveries`
- `c2eb129` `feat(v2.2): run durable competitor discovery`
- `7291f9a` `feat(v2.2): require confirmed competitor discovery`
- `3efeb6e` `feat(v2.2): checkpoint competitor site collection`
- `e53548e` `feat(v2.2): collect bounded public competitor reviews`
- `71c2e40` `feat(v2.2): checkpoint competitor collection snapshots`

## 已知边界与后续

- 生产分析及竞品发现功能开关保持关闭，未执行部署或真实供应商调用。
- V22-031 需将本里程碑的竞品快照注册为正式证据，并在证据支持下生成比较结论；本里程碑刻意不提前实现该部分。
- 当前后端工作目录中没有可同步的独立前端仓库；冻结合同导出检查已确认无计划外变化。
