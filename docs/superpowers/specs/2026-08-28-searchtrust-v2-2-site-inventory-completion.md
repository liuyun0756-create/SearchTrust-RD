# SearchTrust v2.2 全站库存完成记录

日期：2026-08-28

里程碑：V22-021

状态：已完成

设计：`2026-08-28-searchtrust-v2-2-site-inventory-design.md`

实施计划：`2026-08-28-searchtrust-v2-2-site-inventory-implementation-plan.md`

## 1. 完成范围

V22-021 已交付独立的 v2.2 全站库存采集能力：

- 严格、版本化的内部库存模型；
- 安全 URL 规范化、`www`/非 `www` 主机范围和低价值 URL 过滤；
- 最多 500 个规范页面的硬上限与重复 URL/canonical 合并；
- DNS 公网校验、固定地址连接、逐跳重定向复查和 SSRF 防护；
- 单站并发、访问频率、超时、响应大小和 64 KB 解压后读取分块限制；
- SearchTrust 专用 User-Agent 的 robots 规则和 crawl delay 执行；
- robots sitemap、默认 sitemap、有限 sitemap index、gzip sitemap 和站内链接发现；
- Firecrawl Map 最多一次的补充发现适配器及响应大小限制；
- 全库存结构检查、HTML 结构提取和 17 类固定页面分类；
- 首页、业务相关性、页面类型覆盖和软内容类型限制的重点页选择；
- 健康、未过期、身份匹配的 GSC 页面优先级输入；
- 最多 50 个重点页的有界深度快照和失败补位；
- `SiteInventorySnapshot -> SiteInventorySummary` 冻结合同映射；
- 页面请求、Firecrawl、库存清单、选择结果和最终快照检查点；
- Worker 中断恢复、坏检查点拒绝和最终快照复用。

## 2. 运行边界

本里程碑没有新增公开 API，也没有替换生产 `UnavailableV22Executor`。`V22_ANALYZE_ENABLED` 仍默认关闭，完整 v2.2 报告任务不会因本次交付提前开放。

新阶段通过 `CheckpointedSiteInventoryStage` 暴露给未来真实执行器。它接收冻结 `AnalyzeRequest`，使用 `GenerationLimits` 的 500/50 上限，并输出严格 `SiteInventorySnapshot`。

V22-021 不导入或调用 v2.1 pipeline；现有 v2.1 抓取和报告路径未修改。

## 3. URL 与网络安全

### 3.1 主机和 URL

- 只允许 HTTP(S)、80/443 和已确认主机的严格 `www` 对应形式；
- 其他子域名、站外 URL、用户信息、危险端口和非 Web 协议被拒绝；
- 登录、账户、购物车、结账、搜索筛选、管理和副作用路径不进入库存；
- 静态资源、会话参数和追踪参数不进入页面队列；
- 查询参数稳定排序，规范 URL 作为去重身份；
- 检查点键和结构化日志边界只使用 URL SHA-256 摘要，不包含原始查询参数。

### 3.2 SSRF 和响应边界

- 每次请求先解析全部地址，并拒绝任一私网、回环、链路本地、保留或非公共地址；
- HTTP 连接固定到已验证地址，同时保留原始 Host 和 TLS 主机名；
- 每次重定向重新解析并验证，跨允许主机跳转在第二次请求前失败；
- 不信任环境代理，不携带站点 Cookie，不提交表单，不执行 JavaScript；
- 结构 HTML 默认 256 KB，深度 HTML 默认 2 MB；
- sitemap 原始响应与解压后大小分别有上限，拒绝 DOCTYPE/ENTITY 和超限 XML；
- Firecrawl 响应也使用有界流式读取，不保留原始错误正文。

## 4. 发现、分类和选择

### 4.1 发现顺序

1. robots 声明的 sitemap；
2. 默认 `/sitemap.xml`；
3. 首页和已检查页面中的站内链接；
4. 原生来源不足时的一次 Firecrawl Map。

所有来源在入队前执行相同的规范化、主机、robots 和过滤规则。达到请求限制或 500 硬上限后停止扩展，并记录覆盖截断。

### 4.2 页面分类

实现固定分类：首页、服务列表、服务详情、服务地区、地点、关于、联系、团队、评价、案例、FAQ、博客列表、博客文章、产品分类、产品详情、法律和其他。

分类按结构化数据、路径、title/H1 和链接语义的固定顺序执行；相同输入产生相同类型和原因，无法可靠判断时使用 `other`。

### 4.3 重点页

- 首页必选；
- 主营服务和目标市场匹配提高优先级；
- 服务、地区、品牌、联系、团队、评价、案例和 FAQ 保持代表性；
- 博客文章和产品详情受类型限制，避免单一类型占满名额；
- GSC 只在数据健康、当前有效、身份匹配且 URL 属于当前站点时参与加权；
- 分数相同时按规范 URL 排序；
- 深度抓取失败后继续尝试下一候选，候选耗尽时诚实返回较少成功页和限制。

## 5. 检查点和恢复

检查点使用 `site_inventory_v1` 版本前缀，覆盖：

- 每个有界页面、robots 和 sitemap 请求；
- 单次 Firecrawl Map 结果；
- 库存页面摘要清单；
- 最终重点页选择摘要；
- 完整 `SiteInventorySnapshot`。

页面检查点保存响应校验摘要并在恢复时重新验证模型、Base64、SHA-256、响应大小、媒体类型和允许主机。坏检查点产生稳定的确定性错误，不进入后续分析。

任务在 Firecrawl 或后续步骤前中断时，已完成页面请求不会在普通重试中重复；最终快照存在时直接复用。现有 Job 租约继续负责并发执行互斥，未承诺跨并发进程的供应商绝对 exactly-once。

## 6. 冻结合同一致性

摘要映射再次使用冻结 `SiteInventorySummary` 验证：

- `discovered_url_count <= 500`；
- `structurally_checked_count <= discovered_url_count`；
- `deep_analyzed_count <= 50`；
- `deep_analyzed_count <= structurally_checked_count`；
- `deep_analyzed_count` 等于 `selected_pages.deep_analyzed=true` 的数量；
- `selected_pages` 最多 50 个；
- V22-031 前 evidence ID 保持空列表，或只接受调用方提供的合法冻结 evidence ID。

共享 Python、JSON Schema 和 TypeScript 冻结合同没有修改或漂移。

## 7. 配置

新增 `V22_SITE_INVENTORY_` 配置：

- 并发与每秒请求数；
- 连接、读取、总超时与重定向上限；
- 结构、深度、sitemap 原始和解压后字节限制；
- sitemap index 深度和文件数；
- 结构检查批次大小；
- Firecrawl 补充发现开关。

配置只能在代码定义的安全上下界内调整，不能提高冻结 500/50 上限。环境模板不含真实密钥，完整分析开关保持 `false`。

## 8. 验证结果

最终代码执行并通过：

```text
V22-021 专项测试：82 passed
后端全量测试：469 passed
v2.2 冻结合同导出检查：passed
Python app/tests 编译检查：passed
Git 差异格式检查：passed
```

专项覆盖：

- URL 规范化、主机范围、过滤和稳定摘要；
- HTML 提取、多段 JSON-LD、canonical 和固定分类；
- 私网/跨主机重定向、DNS 固定连接、超时、媒体类型和响应上限；
- robots、crawl delay、普通/gzip/index sitemap、外部实体和压缩限制；
- 无 sitemap、重复 URL、500 上限逻辑、页面失败和 Firecrawl 补充；
- 页面类型覆盖、GSC 加权、noindex、博客占比和稳定并列；
- 深度页面失败补位、候选耗尽和冻结摘要计数；
- 中断恢复、坏检查点、最终快照复用和生产 Worker 隔离。

## 9. 未执行的真实外部烟测

自动化验证没有访问真实客户站点，也没有消耗真实 Firecrawl 额度。当前阶段没有用户提供的批准测试域名和供应商测试预算；同时完整生产执行器仍按设计关闭。

在未来真实执行器接入前，应使用明确授权的测试站点执行一次受监控烟测，验证真实 robots 差异、sitemap 规模、服务器限流和 Firecrawl 响应形态。烟测不得作为打开完整分析入口的单独依据。

## 10. 已知后续边界

- JavaScript 渲染站点可能只能获得服务端 HTML 中可见的页面和链接；
- 其他子域名不会自动进入当前主站库存；
- GSC 连接、令牌刷新和原始数据拉取仍由后续第一方数据里程碑实现；
- 正式 evidence ID 由 V22-031 Evidence Ledger 提供；
- 市场 SERP 采集属于 V22-022；
- 竞品选择与竞品页面采集属于 V22-023；
- 完整报告执行器仍需后续采集、证据、规则、文案和持久化里程碑共同完成。

## 11. 本里程碑提交

- `c8bce43`：全站库存设计；
- `f3f455d`：全站库存实施计划；
- `2943bfb`：配置与严格库存合同；
- `98c3042`：URL、HTML 和页面分类纯规则；
- `639e6c9`：有界抓取、robots 和 sitemap；
- `df3f4ba`：组合发现与 Firecrawl 补充；
- `cf18c49`：重点页、GSC 和冻结摘要；
- `0a74116`：持久检查点阶段适配器；
- `d6f509e`：响应分块内存边界加固。
