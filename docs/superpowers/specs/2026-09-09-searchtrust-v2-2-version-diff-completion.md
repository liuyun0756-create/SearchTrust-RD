# SearchTrust v2.2 版本差异完成记录

日期：2026-09-09

范围：V22-073。

状态：实现、全量回归和关闭状态正式发布完成。V22-074 尚未实现，因此本里程碑不会生成
最终 Verified 报告、执行基线、成功条件、最终客户文案或 30/60/90 路线图。

## 完成内容

- 新增 `version_diff_result_v1` 严格内部合同。输入绑定 Case、parent report、评估时间、
  parent checksum、V22-072 输入/结果及 checksum；输出包含可直接进入最终报告的
  `VersionDiff`、逐条审计、unchanged 父引用以及 consumed/new 完整分区。
- 父报告必须是同 Case、同 report ID、schema 2.2.0 的不可变 Prospect 初始报告。阶段逐项
  校验父 Findings、Evidence 和公开 Top Action 的事实身份，并重新执行 V22-072 后逐字节
  比较；调用方不能通过重新签名篡改差异依据。
- 父 Finding 指纹覆盖 Finding 完整规范化 JSON，包括 statement、Evidence/Comparator、
  规则、分类、严重度、范围、置信度、受影响目标、缺失数据和改变条件。
- V22-072 关系通过公开候选的 exact targets 严格归属到具体旧 Finding。页面沿用同域 URL
  规范化，查询只做 NFKC、空白折叠和 casefold，聚合仅归属明确 site 市场目标；不读取
  statement，不使用标题、canonical、redirect 或语义相似度。
- 只输出真实变化。主分类优先级固定为 `refined > reprioritized > confirmed`：可靠增长只
  降低紧迫度；入选状态或前三顺序改变才是重新排序；严格支持且位置不变才是确认。无变化
  旧 Finding 只进入内部审计。`replaced` 在当前无反证规则时不可达。
- 已用于解释旧变化的新 Finding 只消费一次，不再重复生成 `new`；未消费的新业务、测量、
  跨源冲突和审计 Finding 各生成一次 New。直接证据、排序边界证据与 assessment-only
  测量门禁的父 Evidence fallback 均明确区分，fallback 不被描述为新证据或旧结论反证。
- 新增 `version_diff_checkpoint_v1`。摘要绑定阶段/规则/理由目录版本、Case、parent、评估
  时间、三个 checksum 和资源上限；命中前重新验证输入 checksum，checkpoint 只保存结果。
- 固定上限为父 Findings 10,000、新 Findings 15,000、Evidence 50,000、关系 30,000、
  差异/审计各 25,000、单条当前 Findings 1,000、Evidence 10,000、输出 20 MB；超限不
  截断、不返回部分结果，错误不回显业务陈述、Evidence 值或 provider payload。

## 验证结果

- V22-073 合同、父绑定、完整指纹、四类可达变化、严格目标归属、无变化隐藏、消费去重、
  测量门禁、上限、隐私和 checkpoint：14 项通过。
- V22-073 与报告合同/组装、V22-070、V22-071、V22-072 定向联合回归：68 项通过。
- 后端完整回归：1,565 项测试通过；Python compile 与 diff check 通过。
- 前端零改动回归：64 个文件、561 项测试通过；TypeScript 类型检查、合同生成一致性和
  Next.js 16.2.4 生产构建通过。
- 所有新增测试使用合成输入、脱敏 fixture 和内存 checkpoint；未调用真实 Google、
  SerpAPI、OAuth、付款或 LLM。

## 发布边界

- 实现提交 `2f98cd0` 已进入 GitHub `main`。
- Railway API 部署 `367a8b92-9ca6-42c8-a2bc-76f77b64a114` 与 Worker 部署
  `f431b303-4b31-414f-b3ab-6c339c4ce2e6` 均成功；队列健康检查返回 API、Redis 和 Worker
  正常，pending callbacks 为 0，Worker 记录失败任务为 0。
- 本里程碑无数据库迁移、无前端变更、无公开 API、无实时 provider 调用。版本差异阶段
  未接入现有公开路由或 Worker，因此默认不可达。
- 正式环境 `V22_GSC_SYNC_ENABLED`、`V22_GA4_SYNC_ENABLED`、`V22_GBP_SYNC_ENABLED`
  继续缺失并按默认值关闭；现有公开获客分析开关保持原状。

## 回滚与下一步

如发现问题，保持 Verified Generation 不可达并回滚 Railway 代码即可；本里程碑没有
数据库状态需要回滚。

下一步进入 V22-074：把已验证三项行动绑定到可靠指标 baseline、success condition、
复核日期和 30/60/90 依赖路线图，随后才能组装最终 Verified Client Action Plan。
