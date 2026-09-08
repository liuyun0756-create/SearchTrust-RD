# SearchTrust v2.2 第一方 Findings 完成记录

日期：2026-09-08

范围：V22-070。

状态：实现、全量回归和关闭状态正式发布完成。V22-071～074 尚未实现，因此本里程碑不
生成跨来源结论、重新排序、版本差异或最终 Verified Client Action Plan。

## 完成内容

- 新增 `v22_first_party_findings_v1` 内部合同，固定 GSC、GA4、可选官方 GBP 输入、三态
  rule evaluation、source assessment、Finding 和 Evidence 输出关系。
- 新增只接收 Case、parent report 和 snapshot IDs 的 Railway 解析客户端；它使用服务端
  凭据调用数据库，限制响应大小、不跟随重定向，并区分临时故障和确定性拒绝。
- 新增仅 `service_role` 可调用的 `resolve_v22_first_party_findings_input` RPC。它在数据库内
  复核 active Case、当前 paid prospect parent、active/confirmed/matched binding、活动连接、
  来源/schema/资源、当前最新快照、健康摘要、覆盖日期、expiry、checksum 形状和 GBP Content。
- GSC 规则覆盖查询/页面 CTR 机会、查询或页面下降、点击增长和测量覆盖；比较期缺失、
  Top-row 单侧缺失和样本不足均为 `not_checked`，不补零。
- GA4 规则覆盖落地页互动、关键事件转化、sessions/engagement/key-event-rate 同向变化及
  测量配置；冲突方向分开表达，sampling/thresholding 降低置信度，受限指标不参与判断。
- 官方 GBP 规则覆盖曝光变化、客户行动变化、搜索需求档位及资料/测量问题。公开 SerpAPI
  GBP 不进入本目录，也不冒充官方 Performance。
- 新增稳定 Evidence/Finding 身份、输入行无关排序、每源业务/测量输出上限、完整引用校验、
  结果体积限制和 checkpoint 摘要/版本/checksum 复核。

## 三态和数据边界

- `triggered` 必须一对一生成完整 Finding；`not_triggered` 只表示数据充分且条件不成立；
  来源、比较、样本、字段或平台限制不足统一使用 `not_checked`。
- 不健康但结构有效的来源只能生成 allowlist 内的测量配置 Finding，不能生成业务表现结论。
- GSC/GA4 缺失会使请求无效；官方 GBP 缺失仅形成覆盖缺口，不阻断 Verified Core，也不能
  宣称 Full Evidence。
- GBP raw Content、精确 Performance、关键词文本和精确次数不写入 Findings、Evidence、
  checkpoint、日志或新数据库字段；影响排序只使用固定不可逆档位分值。
- RPC 拒绝跨 Case、错误 parent、旧 binding、非 matched identity、被替换的旧快照、过期
  内容和资源上下文不一致；浏览器不能提交指标 payload。

## 验证结果

- 后端完整回归：1,512 项测试通过。
- 第一方 Findings 合同、规则、解析客户端和 checkpoint 定向回归：23 项通过。
- 第一方来源同步与 Evidence 兼容回归：111 项通过。
- 前端完整回归：64 个文件、561 项测试通过，其中数据库语义测试 20 项通过。
- TypeScript 类型检查、合同生成一致性检查和 Next.js 16.2.4 生产构建通过。
- Python compile、两仓库 staged diff check 通过。
- 所有测试使用 fake transport、脱敏 fixture 和本地 PGlite；未调用真实 Google、SerpAPI、
  OAuth、付款或 Verified Generation。

## 正式发布

- Supabase 已应用 `20260908000000_add_v2_2_first_party_findings_input.sql`，本地/远端 migration
  版本一致。
- Railway API 与 `SearchTrust-v2-2-Worker-Production` 均部署了包含实现提交 `1b9db8b`
  的 `main` 分支，验证时状态为 `SUCCESS`、实例状态为 `RUNNING`；API
  `/api/v1/health` 返回正常。
- Vercel 生产部署状态 `Ready`，提交 `4048934` 已绑定 `trysearchtrust.com`；正式首页返回
  HTTP 200，最近一小时错误日志为空。
- Railway 最近日志显示 API 正常启动，Worker reconcile jobs 均成功且没有 failed/retried/
  ongoing/queued 积压。
- GSC、GA4、GBP 后端同步开关仍 absent；前端 Google/Verified 开关未配置。现有 Prospect
  Analyze 与 Competitor Discovery 保持原有启用状态，不属于 Verified Generation 开关。

## 回滚与下一步

如后续发现问题，先继续保持 Verified Generation 关闭，再回滚 Railway 代码。数据库函数
只读且权限封闭，可安全保留；如必须移除，使用新的前向迁移，不删除历史快照。

下一步进入 V22-071：跨来源 Findings。只允许聚合和页面/日期级合理关联，禁止用户级因果
归因；每条结论必须列出来源、限制和无法证明的部分。
