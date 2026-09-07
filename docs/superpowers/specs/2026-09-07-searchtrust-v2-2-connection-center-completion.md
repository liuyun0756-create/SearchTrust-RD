# SearchTrust v2.2 Connection Center 完成记录

日期：2026-09-07

范围：V22-063。

状态：实现与本地回归完成；无数据库迁移。生产发布保持 Google 连接、GSC、GA4、
官方 GBP 与 Verified Analysis 开关关闭。

## 完成内容

- 新增私有只读 `GET /api/v2/cases/{caseId}/connection-center`，返回固定
  `connection_center_v1` 浏览器安全投影。
- 聚合 active Case、当前 v2.2 parent report、用户 Google connections、active bindings、
  有界的最近 jobs 与 snapshots；查询不读取 token、ciphertext、raw/normalized payload、
  metrics、checksum、lease 或内部异常文本。
- 读取后复核 Case `updated_at` 与 active binding signature；发生并发变化时重读一次，
  再次变化则返回固定 `CONNECTION_CENTER_BUSY`，避免混合两个时点的状态。
- Verified Core 固定要求：当前 Case 的 confirmed public GBP 证据，以及 healthy、matched、
  未过期的 GSC 与 GA4 快照。两侧公共 GBP 都缺失、一侧缺失、身份变化、资源错误或
  数据不健康都会阻断，不会把缺失数据解释为零。
- Full Evidence 在 Verified Core 之上额外要求可用的官方 GBP Performance；官方 GBP
  仍是可选增强，未连接不会阻断 Verified Core。
- 统一 Connection Center 页面提供三源卡片、进度、唯一下一步、用户状态优先的说明、
  可展开安全技术详情、官方 GBP 折叠区和移动端单列布局。
- 现有 Google 授权、资源发现、身份确认、解绑、GSC/GA4/GBP user-triggered sync 与重试
  控件均复用；每次成功变更后刷新聚合状态，聚合页面只在任务 queued/running 时轮询。
- M7 尚未交付，因而生成按钮即使三源门禁已通过仍明确锁定；V22-063 没有提交 verified
  job 的代码路径，服务端 factory 也强制 `verified_generation_enabled=false`。

## 状态和失败语义

- 用户状态固定为 profile/connection/resource/identity/sync/healthy/attention/optional 九类，
  每类同时有文字和图标提示，不依赖颜色表达。
- 最新刷新失败但仍有有效 healthy snapshot 时继续允许该快照进入 Verified Core，同时
  显示刷新失败 warning 和 retry；没有可用快照时则阻断。
- public GBP 缺少 URL、parent report、snapshot ID、采集时间、healthy 状态或 matched
  身份中的任意一项都不会通过门禁。
- 错误响应只有固定 code/message；无权限、跨用户、archived 或不存在 Case 均被隐藏为
  not found。

## 验证结果

- 前端完整回归：64 个文件、560 项测试通过。
- Connection Center 与既有 Google 控件定向回归：8 个文件、36 项测试通过；新增投影、
  coherent retry、路由安全、状态顺序、进度、可选来源和锁定 CTA 覆盖。
- 后端完整回归：`PYTHONPATH=. .venv/bin/pytest -q`，1,489 项通过。
- TypeScript：`npm run typecheck` 通过。
- Next.js 16.2.4 生产构建：`npm run build` 通过，新接口与连接页均被正确识别为动态路由。
- `git diff --check` 通过。
- 自动浏览器首次检查发现本地 `.env.local` 保存的是 Vercel 引用占位值，Clerk 在未登录
  本地环境拒绝该 publishable key；因此未把该环境问题误报为页面通过。核心 UI 通过
  静态组件渲染、生产构建和响应式 class 合同验证，正式环境仍由 feature flag 隐藏。

## 发布边界

- V22-063 不新增或修改数据库对象，不需要执行 SQL。
- 正式环境的 `GOOGLE_CONNECTIONS_ENABLED`、`GOOGLE_GSC_SYNC_ENABLED`、
  `GOOGLE_GA4_SYNC_ENABLED`、`GOOGLE_GBP_SYNC_ENABLED` 以及后端对应同步开关必须继续
  absent / non-`true`。
- 不配置或打开 `GOOGLE_VERIFIED_ANALYSIS_ENABLED`；M7 必须实现前端与 Railway 双侧
  校验后才能开放 generation。
- 发布验收仅检查构建、HTTP 健康、disabled/private 边界和近期错误日志；不发起真实
  OAuth、SerpAPI、Google sync、verified generation 或付款。

## 下一步

进入 M7 / V22-070：第一方 Findings。它消费这里冻结的 eligible GSC、GA4 与可选 GBP
Performance snapshot IDs，但必须在后端重新校验 Case、binding、identity、health、expiry
和 parent report，不能信任浏览器聚合响应。
