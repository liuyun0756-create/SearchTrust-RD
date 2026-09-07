# SearchTrust v2.2 SerpAPI 公开 GBP 决策

日期：2026-09-07

状态：已批准并实施。

## 决策

v2.2 将 SerpAPI 公开 Google Maps/GBP 数据作为默认 GBP 证据路径。用户不需要拥有官方
GBP 后台账号，也不需要为 GBP 完成 Google OAuth。

官方 GBP connector 保留在独立开关后，仅在项目获得 Google API 准入后作为可选增强。

## 证据边界

SerpAPI 公开 GBP 支持商家身份、资料完整度、网站对齐、评分、评论、图片、帖子和公开搜索排名。

它不支持业主后台独有的 Search/Maps 曝光、电话点击、路线请求、网站点击或真实搜索词曝光。
这些数据缺失必须表示为“官方 GBP Performance 未连接”，不能表示为 0、healthy 或已验证。

## 覆盖等级

- Verified Core：已确认的公开 GBP + 健康、匹配的 GSC 和 GA4 快照。
- Full Evidence：在 Verified Core 基础上，再有健康、匹配的官方 GBP Performance 快照。

Verified Core 可生成验证执行报告，但 `full_evidence_coverage` 必须为 `false`。

## 失败处理

- 优先使用用户确认的 Google Maps/GBP URL 中的 Place ID、data ID 或 CID 精确查询。
- 没有链接时，使用网站域名、商家名称和城市进行严格身份匹配。
- 找不到或多候选无法唯一确定时，必须要求用户提供/确认链接，不得自动绑定低置信候选。
- 三个 SerpAPI Key 按既有逻辑去重、轮换，并在失效、限流或额度耗尽时切换。
