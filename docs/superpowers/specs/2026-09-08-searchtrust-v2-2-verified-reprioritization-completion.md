# SearchTrust v2.2 Verified 重新优先级完成记录

日期：2026-09-08

范围：V22-072。

状态：实现、全量回归和关闭状态正式发布完成。V22-073～074 尚未实现，因此本里程碑
不会生成版本差异、执行基线、最终文案或开放 Verified Client Action Plan。

## 完成内容

- 新增 `verified_reprioritization_v1` 严格内部合同。输入绑定 Case、parent report、公开
  评估时间上界、本次评估时间、原公开行动 planning date、本次 planning date 及所有
  上游 checksum；输出固定三项行动、核心公开 Finding、全候选审计、完整关系和未使用引用。
- 执行前重算公开 Findings、公开行动、V22-070 与重建的 V22-071，并对规范化结果逐字节
  比较；调用方不能重新签名后篡改 Finding、行动、分数或关联。公开评估可以早于验证评估，
  V22-070/071 必须与本次 Case、parent 和评估时点一致。
- 从公开行动 `selection_audit` 和冻结目录恢复全部候选，不只重排原前三。页面仅按
  V22-071 Case-host URL 规则精确匹配，查询仅做 NFKC、空白折叠和 casefold，聚合业务信号
  仅匹配市场可见性；不使用 statement、标题、page type、canonical、redirect 或模糊推断。
- 验证等级固定为跨源支持、单源支持、仅公开证据和增长降级。重复 Findings 不累积分数；
  同目标可靠增长只降低一级，HTTP、noindex 和身份类硬事实不接受增长降级。
- 跨来源方向冲突按来源对合并为普通 `review_measurement_consistency` 候选，排序低于任何
  合格单源支持但高于仅公开证据。GSC/GA4 健康、身份、必要比较或关键指标限制若阻断可靠
  判断，最多合并为一条 `restore_verified_measurement` 并固定第一；官方 GBP 缺失不强制。
- 最终始终选择三项，重新应用入选公开行动之间的技术依赖，复核日期固定为本次 planning
  date 后 30/60/90 天。测量行动可排在业务行动之前，但核心问题始终保留最高业务行动的
  原公开 anchor Finding。
- 新增 `verified_reprioritization_checkpoint_v1`。摘要绑定阶段、规则、行动目录和上游输入，
  对无序集合使用语义稳定摘要；checkpoint 只存最终验证结果，不存上游 Evidence、可信
  snapshot、GBP raw Content、精确 Performance 或关键词。
- 固定上限为公开候选 1,000、新 Findings 15,000、关系 30,000、审计 2,000、问题代码
  100、输出 20 MB；错误只返回安全固定代码，不返回部分行动或输入 payload。

## 验证结果

- V22-072 合同、严格映射、排序、增长保护、测量边界、核心问题和 checkpoint：19 项通过。
- V22-072 与公开 Findings/行动、V22-070、V22-071 定向联合回归：99 项通过。
- 后端完整回归：1,551 项测试通过；Python compile 与 staged diff check 通过。
- 前端零改动回归：64 个文件、561 项测试通过；TypeScript 类型检查、合同生成一致性和
  Next.js 16.2.4 生产构建通过。
- 所有新增测试使用合成输入、脱敏 fixture 和内存 checkpoint；未调用真实 Google、
  SerpAPI、OAuth、付款或 LLM。

## 发布边界

- 本里程碑无数据库迁移、无前端变更、无公开 API、无实时 provider 调用。
- Railway API 与 `SearchTrust-v2-2-Worker-Production` 已成功发布包含实现提交
  `f5ac84c` 的同一 `main` 分支；API 健康检查通过，Worker 健康记录为零失败任务。
- GSC、GA4、GBP 同步变量和 Verified Generation 变量在正式环境均保持缺失，因此按
  后端默认值关闭；V22-072 也没有被现有 Worker 或公开路由调用。

## 回滚与下一步

如果发现问题，保持 Verified Generation 关闭并回滚 Railway 代码即可；本里程碑没有
数据库状态需要回滚。

下一步进入 V22-073：在 parent report 不可变的前提下生成
Confirmed/Reprioritized/Refined/Replaced/New 版本差异，并解释旧结论、新证据、新结论和原因。
