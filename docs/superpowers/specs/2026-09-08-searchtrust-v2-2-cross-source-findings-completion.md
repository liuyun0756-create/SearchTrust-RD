# SearchTrust v2.2 跨来源 Findings 完成记录

日期：2026-09-08

范围：V22-071。

状态：实现与全量回归完成，作为关闭的后端内部能力发布。V22-072～074 尚未
实现，因此本里程碑不会重排行动、生成版本差异或开放 Verified Client Action Plan。

## 完成内容

- 新增 `v22_cross_source_findings_v1` 内部合同，固定三组顺序：GSC↔GA4、GSC↔GBP、
  GA4↔GBP；每组只有双源都健康、身份匹配、未过期且 90 天窗口结束日差不超过
  3 天时才可生成业务结论。
- GSC↔GA4 页面规则覆盖同向增长/下降、已确认搜索机会和反向测量冲突；聚合规则覆盖
  clicks↔sessions 90 天变化，周序列使用排除 GA4 最近 2 天后的完整 ISO 周和并列平均排名
  Spearman，至少 8 周且固定阈值为 ±0.70。
- 页面身份仅在 Case 域名内对齐 GSC URL 与 GA4 landing path；统一 HTTPS、root/www、
  默认端口、尾斜杠与 RFC unreserved 编码，只移除明确追踪参数。业务查询参数保留且排序；
  不合法百分号、反斜杠、点路径、外部域名和 `(not set)` 都不参与关联。
- GSC↔GBP 只比较曝光，GA4↔GBP 只比较 sessions 和 customer actions。GBP 必须是官方
  受权 Performance，公开 SerpAPI GBP 不参与。精确 GBP 次数、比例、关键词和原始 Content 不保存到
  Evidence、Finding、checkpoint 或日志，只保存变化档位和 0/20/50/100 固定分。
- 每条 triggered Finding 必须恰好引用对应的两个来源，并与 rule evaluation 一对一。第三来源
  缺失/不健康只作为限制，不阻断合格来源对；所有业务结论都固定声明相关性不证明因果。
- 新增版本化 checkpoint，输入摘要绑定 V22-070 输入/结果与本规则版本。执行前会重算
  V22-070 并进行逐字节比对；checkpoint 只写已校验的跨源结果，不写可信输入或 GBP raw Content。
- 固定上限为：每条页面业务规则最多 5 条、每组聚合业务最多 2 条、测量冲突全局最多
  5 条、Finding 总计最多 25 条；被截断的评估显式降为 `not_checked/output_limit`。

## 验证结果

- V22-071 合同、页面、时间、规则、GBP 隐私和 checkpoint 定向测试：20 项通过。
- 后端完整回归：1,532 项测试通过；Python compile 与 staged diff check 通过。
- 前端完整回归：64 个文件、561 项测试通过；TypeScript 类型检查、合同生成一致性和
  Next.js 16.2.4 生产构建通过。
- 所有规则测试使用 fake transport、脱敏 fixture 和内存 checkpoint；未调用真实 Google、
  SerpAPI、OAuth、付款或 Verified Generation。

## 发布边界

- 本里程碑无数据库迁移、无前端变更、无公开 API 和无实时 provider 调用。
- Railway API 与 `SearchTrust-v2-2-Worker-Production` 已发布包含实现提交
  `72aaa51` 的同一 `main` 分支；API 健康检查通过，Worker 正常运行。
- GSC、GA4、GBP 后端同步开关和 Verified Generation 前端开关仍保持缺失/关闭。

## 回滚与下一步

如果发现问题，保持 Verified Generation 关闭并回滚 Railway 代码即可；本里程碑没有数据库
状态需要回滚。

下一步进入 V22-072：将已验证的新证据进入排序，重算核心问题和三项行动，且不允许
不健康来源提高 verified confidence。
