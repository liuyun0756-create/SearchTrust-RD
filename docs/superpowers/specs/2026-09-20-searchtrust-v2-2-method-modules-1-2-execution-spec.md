# SearchTrust V2.2 — Our Method 模块 1、2 执行规格

- 日期：2026-09-20
- 状态：规格草案，供内容审阅与后续实施；本文件不代表页面已实现或已发布。
- 目标页面：前端 `/framework`，导航名称保持 `Our Method`。
- 目标用户：为本地企业开展获客、交付和持续优化的 SEO 从业者、顾问及代理机构。
- 上站语言：英文。本文中文为执行说明；带编号的英文文案为本轮建议上站版本。
- 范围：模块 1「整套方法的工作价值」与模块 2「获客阶段的公开证据诊断」。
- 内容依据：本轮逐模块讨论；新增的英文表达、布局细节与组件拆分是本规格建议，不追溯标记为已获用户确认。

## 1. 目标与成功标准

本轮不是把旧版八层模型介绍换一套口号，而是解释完整工作流如何帮助 SEO 从发现问题走到可解释、可执行、可复查的建议。

读完模块 1，应能回答：

1. SearchTrust 服务的是 SEO 从业者，而不是直接代替企业经营 SEO。
2. 获客时可以准备具体发现、证据与初始行动；服务时可以用客户数据重新审视优先级；复盘时可以说明判断变化。
3. 产品整理证据与工作细节，SEO 保留审核、业务确认和专业判断。

读完模块 2，应能回答：

1. 没有客户 GSC/GA4 权限时，能使用哪些公开证据。
2. 为什么先确认服务、地域、搜索意图与真实竞品。
3. 如何从页面承接、商家一致性和页面可信度形成具体发现。
4. 为什么发现不自动等于行动，建议仍可能需要客户确认条件。
5. 售前交付如何用于说明第一阶段工作范围。

验收不以提高成交率、排名或流量为本轮完成条件；这些不是内容更新即可验证的结果。

## 2. 范围与不做事项

### 2.1 本轮实施范围

- 替换现有 `MethodV22` 前两个 section：旧 hero 和六步抽象工作流。
- 加入模块 1 的价值主张、三个工作阶段、收束文案和模块 2 锚点入口。
- 加入模块 2 的三步诊断过程、三个诊断问题、完整方法示例、交付说明。
- 所有正文集中存储，避免组件与内容两处维护。
- 补充组件、内容结构、锚点与窄屏验证。

### 2.2 不在本轮范围

- 不改获客流程、竞品发现逻辑、分析规则、报告合同、支付、点数或登录。
- 不增加 Google 连接、客户数据读取或任何生成 API 调用。
- 不新增数据库表、迁移、环境变量或第三方依赖。
- 不修改首页、定价页、全局导航及底部 CTA 的商业规则。
- 不自动重构后续模块 3–7；它们后续仍需逐模块讨论。
- 不新增精细埋点；沿用现有 PostHog 配置。
- 不包含发布、生产环境操作、提交或清理已有工作区改动。

## 3. 内容原则与产品边界

| 原则 | 执行要求 |
| --- | --- |
| 工作成果优先 | 先讲具体发现、证据、行动和复查安排，再介绍方法名词。 |
| 面向专业人员 | 可使用 search intent、landing page、CTR、GSC、GA4 等术语，但首次出现重要产品概念时说明用途。 |
| 事实与判断分开 | 已观察到的问题、推断机会、待确认条件不能写成同一确定性。 |
| 公共与私有数据分开 | 模块 2 不暗示能获得潜在客户私有搜索流量、转化、线索质量或收入。 |
| 范围准确 | 使用 checked pages、available evidence、within the checked scope；不声称完整抓取、全量排名监测或穷尽所有原因。 |
| 人工与产品分工清楚 | 业务优先级、遗漏页面、服务能力、实施背景等可能需要 SEO 与客户核实；不写成系统全部自动完成。 |
| 八层模型定位 | L1–L8 是页面诊断工具之一，不是整套方法，也不是 Google 排名公式。 |
| 不伪造案例 | 本轮示例明确为 illustrative example，不使用虚构客户、提升百分比、收入、真实来源链接或真实采集日期。 |
| 不宣称因果证明 | 数据支持判断和复查，不自动证明某项修改造成增长。 |
| 三个行动的语境 | 指成功生成、证据条件满足后的报告成果；不能为了满足数量编造发现。 |

补充：产品要求确认至少 1 个、最多 3 个竞品，不表示单个竞品足以触发所有市场比较规则。本轮不必突出数量，但不能写成「必须 3 个」或「没有竞品也能完成」。

本轮不在顶部宣称「Google 官方认证」「资深 SEO 自动替身」「自动穷尽所有问题」或「多年客户实战验证」。Google 依据与产品规则的系统说明保留给后续方法来源模块。

## 4. 页面信息架构及旧内容处置

```text
现有全局 Header（不改）
main
  模块 1 #method-value
    受众标签 + H1 + 两段介绍
    三个阶段：Prospect / Deliver / Review
    方法收束 + 进入模块 2 的链接
  模块 2 #prospect-method
    标签 + H2 + 介绍
    Step 01：建立竞争背景
    Step 02：三个诊断问题
    Step 03：将发现转成建议
      #prospect-example：完整公开证据方法示例
    交付说明 + 人工审核边界 + 报告链接
  现有 #diagnostic-map 及其后全部内容（暂保留）
现有全局 Footer（不改）
```

| 现有元素 | 本轮处置 |
| --- | --- |
| Hero：Find the problem… | 由模块 1 替换，不保留重复主张。 |
| Hero 右侧四问卡片 | 本轮从渲染中移除，其原则由正文具体说明；不把旧卡片复制进新模块。 |
| A disciplined SEO workflow…及六步 stages | 由模块 2 替换；删除仅被它使用的局部常量，不清理无关文件。 |
| 旧 hero 的 Explore the method | 改为指向 `#prospect-method` 的明确动作。 |
| #diagnostic-map、L1–L8、#reasoning | 保留结构、内容、既有锚点与功能。 |
| 下方 Prospect/Verified 概览 | 暂保留，不借本轮重写模块 3；其简短概览与新增获客详情可暂时共存。 |
| FAQ 与 FAQ JSON-LD、MethodCTA | 不改。 |

下方旧的 `01 / The complete diagnostic view` 等标签本轮不重新编号：它们是旧页面的局部标记，不是新内容模块编号。新顶部使用语义标签 `Our method` 和 `Before private access`，不额外显示「模块 1/2」，避免混用编号。完整重排留到剩余内容确认后。

## 5. 模块 1：内容与结构

### 5.1 内容契约

- 根锚点：`method-value`。
- 唯一 H1；阶段标题为 H2，避免没有父标题的层级跳跃。
- 内容顺序：定位 → 工作价值 → 成果 → 审核责任 → 下钻入口。
- 三个阶段固定按 Prospect、Deliver、Review 排列，不做轮播或标签切换。
- 默认完整展示正文和成果行；不折叠关键价值。

### 5.2 英文上站文案

**M1.01 — Eyebrow**

Our method / For local SEO professionals

**M1.02 — H1**

From the first proposal to the next review, ground your SEO recommendations in evidence.

**M1.03 — Intro paragraph 1**

SearchTrust brings website checks, public business information and competitor observations into one client case. Connected Search Console and GA4 data add performance evidence when the client grants access.

**M1.04 — Intro paragraph 2**

Use specific findings to frame the proposal. Use client data to reassess priorities. Define what to change, why it deserves attention and how to review it.

**M1.05 — Stage 1 label / H2**

Prospect / Start the proposal with this business’s specific problems.

**M1.06 — Stage 1 body**

Before private analytics access, review the website, public Business Profile and confirmed competitors in the context of the client’s services and locations. Separate observed problems from opportunities that still need validation.

**M1.07 — Stage 1 output**

What you get: Findings with supporting evidence and three priority actions to discuss as an initial scope of work.

**M1.08 — Stage 2 label / H2**

Deliver / Use actual performance to decide what comes first.

**M1.09 — Stage 2 body**

With connected Search Console and GA4 data, relate eligible performance evidence to the relevant page, query or supported scope. Reassess which findings are supported, which are less urgent and which need measurement checks.

**M1.10 — Stage 2 output**

What you get: Prioritized actions with specific targets, implementation requirements and completion criteria.

**M1.11 — Stage 3 label / H2**

Review / Explain why the plan stays—or changes.

**M1.12 — Stage 3 body**

Review fresh evidence within the same client case. New observations may support the original direction or change what deserves attention next. When the judgment changes, explain where it changed and why.

**M1.13 — Stage 3 output**

What you get: Validation metrics, baselines and review dates, plus explanations of material changes in a subsequent report.

**M1.14 — Closing statement**

Start with the business and its competitive context. Connect page-level findings to available performance evidence. Then define the work and how to review it.

**M1.15 — Responsibility statement**

SearchTrust organizes the evidence and work details into reports you can review, share and update. You retain the professional judgment.

**M1.16 — Primary navigation link**

See how prospect diagnosis works

- 目标：`#prospect-method`。
- 原生同页链接，不创建任务，不登录、不扣点。
- 模块 1 不增加购买按钮，不把现有页底转化入口复制到每个阶段。

### 5.3 文案审阅重点

- 「复盘」是后续报告与 SEO 复查，不暗示后台持续自动监控、自动完成行动或自动证实结果。
- 「query or supported scope」不表示 GSC 查询可逐用户关联至 GA4 转化。
- 这一模块暂不放规则阈值、八层评分、价格、赠送点数与长篇免责声明。

## 6. 模块 2：内容与结构

### 6.1 内容契约

- 根锚点：`prospect-method`；主标题 H2。
- 三步标题 H3；步骤内三个诊断问题标题 H4。
- 示例使用 H4；具体字段使用 `dl/dt/dd`，不是额外重复小标题。
- 不使用「免费完整分析」表述。没有客户后台权限与分析不耗点是两回事。
- 所有核心内容默认可读；不要求 hover、展开或切换才能看到条件和限制。

### 6.2 英文上站文案：开场

**M2.01 — Eyebrow**

Before private access / Prospect diagnosis

**M2.02 — H2**

Before you have analytics access, find something worth discussing.

**M2.03 — Intro**

Start with the prospect’s services, target locations and website. Combine public business information with confirmed competitors to examine the market context, the pages serving that demand and the information customers need to choose the business.

**M2.04 — Intro outcome**

Turn those observations into evidence-backed findings and initial actions for a specific sales conversation.

### 6.3 Step 01：建立竞争背景

**M2.05 — H3**

01 / Establish a relevant competitive context.

**M2.06 — Body**

A useful comparison starts with the service and market—not just another website in the same industry.

**M2.07 — 四项检查维度**

| Label | Copy |
| --- | --- |
| Service | What does the business actually offer? |
| Location | Which areas does it serve, and where was the search observed? |
| Search intent | Is the searcher choosing a provider, comparing options or looking for information? |
| Competitor | Is this a relevant business competitor, or a directory or content site appearing in the results? |

**M2.08 — Interpretation**

Keep the query and location context attached to the observation. Treat organic and local results separately. Use competitors to understand the market—not as proof that copying their pages will improve rankings.

执行说明：这是方法中的检查维度，不声明搜索意图分类、服务能力判断和竞品身份确认均为无需人工的全自动能力。用户确认是流程的一部分。

### 6.4 Step 02：检查客户如何承接需求

**M2.09 — H3**

02 / Check how the business serves that demand.

三个诊断条目依次展示；每项包含问题、检查范围、用于何种决策。桌面可采用三列，窄屏按同一顺序堆叠。不得为保持等高截断文本。

**M2.10 — Question 1**

- Title: Is there a suitable page for the service?
- Check: Discovered pages, service and location topics, checked page responses and observed competitor page types.
- Decision: Decide whether to improve an existing page or investigate a possible gap before recommending a new one.

**M2.11 — Question 2**

- Title: Do the website and public business information agree?
- Check: Available names, contact details, website links, locations and service-area information.
- Decision: Identify specific conflicts or missing information and clarify what the client needs to confirm or correct.

**M2.12 — Question 3**

- Title: Does the page give customers enough reason to choose the business?
- Check: Service detail, local context, business identity, accountability and credible supporting information.
- Decision: Turn content and credibility gaps into specific requirements instead of a generic request for more content.

**M2.13 — Eight-layer placement**

The L1–L8 trust model supports this page-level diagnosis. It is one part of the wider method, alongside business context, competitor evidence and performance analysis when available.

不在本模块再次渲染八层详情，现有下方详情入口仍保留。

### 6.5 Step 03：把观察转成建议

**M2.14 — H3**

03 / Turn a finding into a scope of work.

**M2.15 — Body**

Identify the affected page or business information, show the supporting observation and explain the proposed work. Keep client confirmations visible instead of treating every apparent gap as an automatic task.

**M2.16 — Reading structure**

Target → Observation → Judgment → Proposed action → Client confirmation

上述结构用有序列表或自然换行呈现，不使用在移动端必须横向滑动的箭头长条。只是说明顺序，不设置可点击状态。

### 6.6 完整方法示例

- 锚点：`prospect-example`。
- 位置：Step 03 下方，始终展开。
- 使用与网站真实报告相近的排版语言，但不仿造真实运行状态、证据编号、客户网址或分析时间。

**M2.17 — Visible example label**

Illustrative example — not a customer result

**M2.18 — Example H4**

Does this service need a dedicated page?

**M2.19 — Example fields**

| Label | Copy |
| --- | --- |
| Target | The website’s coverage of a core service. |
| Observation | Within the checked pages, service information is spread across a general page. A confirmed competitor has a dedicated page explaining the equivalent service. |
| Judgment | The service’s page coverage deserves review. The competitor’s page alone does not establish a ranking cause. |
| Proposed action | First confirm whether a relevant page was missed. If one exists, assess improving it. If the gap is confirmed, consider a dedicated service page. |
| Client confirmation | Is this a priority service, can the business deliver it, and can the client supply the information needed to describe it accurately? |

不增添「竞争者排名第 1」「客户流失 40% 询盘」等示例数据。不得把未发现页面写成确定不存在页面。

### 6.7 交付与边界

**M2.20 — Closing H3**

Bring evidence to the proposal.

**M2.21 — Closing body**

Get findings specific to the business, traceable observations and three priority actions. Explain which pages or business details need attention, why the work deserves consideration and what still needs the client’s confirmation.

**M2.22 — Next-stage bridge**

Use that to frame the initial scope of work. Once the client connects analytics, reassess those judgments with performance evidence.

**M2.23 — Division of responsibility**

SearchTrust provides the findings, evidence and proposed actions. You review them with the client’s business priorities and confirm the scope before proposing the work.

**M2.24 — Scope note**

Public evidence can support a proposal. It does not establish the prospect’s private search traffic, lead quality or revenue.

**M2.25 — Supporting link**

View a sample report

- 目标：现有 `/sample-report`。
- 不声称目标页面就是上方示例的真实客户报告。
- 不新增通往尚未实现模块 3 的锚点；下一阶段说明本轮仅用正文承接。

## 7. 布局与视觉规格

### 7.1 总体

- 复用现有 Our Method 的浅米绿背景、深绿文字、衬线标题和无衬线正文。
- 最大内容容器沿用 `max-w-7xl`；长段落最大宽度约 65–75ch，避免全宽长行。
- 不新增外部字体、插画、图库素材、背景视频或动画库。
- 避免把所有内容包成相同卡片：模块 1 用三个阶段列；模块 2 用纵向步骤、诊断条目与一张重点示例卡。
- 保留静态页面的专业文档感，不加入伪仪表盘、计分器和数据加载效果。

### 7.2 模块 1

- 移除旧左右 hero 的四问 aside，采用全宽标题介绍，下接阶段内容。
- H1 建议使用响应式 36–64px 范围，行高 1.08–1.18；不写死 `<br>` 强制各屏同样换行。
- 介绍正文 16–18px、行高至少 1.6；阶段正文至少 15–16px。
- 三个阶段：小于 1024px 单列，1024px 起三列，按内容自然增高；不采用两列产生孤立第三卡。
- 每阶段区分 label、标题、说明、成果行；成果行以分隔线或字体强调，不只用颜色。
- 收束段落与模块内链接在阶段内容之后，不设置浮动、sticky 或覆盖正文的按钮。

### 7.3 模块 2

- 白底，与模块 1 背景形成轻量区分；保留现有页面边框语言。
- 三个步骤纵向排列，步骤间距明确，编号只标步骤，不误作得分。
- Step 01 四项维度：640px 以下单列，640px 起可 2×2。
- Step 02 三个问题：1024px 以下单列，1024px 起三列；每项内部 label 可见。
- 示例卡始终一张：768px 起 label/正文两列；以下垂直排列。内容与 DOM 顺序保持一致。
- 示例说明与五个字段全部可见，不藏在 tooltip 或折叠项里。
- 模块间距建议手机 48–64px、桌面 80–96px；具体对齐复用现有 spacing，不扩大为全站 token 重构。

### 7.4 对比度与窄屏

- 普通文本对比度目标至少 4.5:1，大字至少 3:1；现有颜色也需实际检查，不能假设通过。
- 如现有强调色在新用途下不足，仅调整本模块组合，不改全局主题。
- 320px 不出现页面横向溢出；长单词和链接允许适当换行。
- 不用 `line-clamp`、固定正文高度或隐藏溢出来解决排版。

## 8. 交互、语义与状态

| 元素 | 默认与交互 | 其他状态 |
| --- | --- | --- |
| 阶段/步骤/示例 | 静态内容；无 hover 手势、无整卡点击 | 无 loading、error、disabled；不造占位状态 |
| 模块 1 锚点链接 | 点击或 Enter 定位 `#prospect-method`；有 underline/hover/focus-visible | 不依赖 JS；锚点上方留出 Header 高度 |
| 模块 2 报告链接 | 用现有 Link 进入 `/sample-report` | 保持路由原有行为，不自动启动分析 |

- 页面仍只有一个 H1；结构见各模块内容契约。
- 使用原生 section、ol、article、dl 和链接；不要给静态卡片添加 button role/tabIndex。
- section 的 `aria-labelledby` 指向各自的可见标题；所有 ID 全页唯一。
- 装饰图标 `aria-hidden="true"`，没有只靠图标才能理解的动作。
- 锚点目标设置合适 `scroll-margin-top`，实测标题不被 Header 挡住。
- 键盘焦点清晰；不劫持 Tab、不强制脚本焦点切换。
- 不新增滚动动画；如果复用全局 smooth scroll，需尊重 reduced-motion。
- 在 200% 缩放及移动端上保持全部文案可读。
- 不新增用户输入，不保存浏览状态，不读取客户案例。

## 9. 前端落地设计

前端根目录：`/Users/liuyun3/Documents/SearchTurst前后端/search-trust`。

### 9.1 建议文件

| 相对前端根目录 | 操作与责任 |
| --- | --- |
| `src/components/framework/method-content.ts` | 新增 `methodValueContent` 与 `prospectMethodContent`；保留现有 diagnosticAreas、reasoningExamples、methodFAQ。 |
| `src/components/framework/MethodValueSection.tsx` | 新增模块 1，默认接收标准内容对象。 |
| `src/components/framework/ProspectMethodSection.tsx` | 新增模块 2，包含完整示例，默认接收标准内容对象。 |
| `src/components/framework/MethodV22.tsx` | 导入两个组件替换前两个 section；移除不再使用的 stages/导入；不整体覆盖此文件。 |
| `src/components/framework/MethodSections.test.tsx` | 新增组件内容、链接和语义测试。 |
| `src/app/framework/page.tsx` | 原则上无需修改；核对 metadata 与 FAQ JSON-LD 未受影响。 |

不修改 `LayerDetailBreakdown.tsx`、Header、首页及定价相关文件。当前工作区已有未提交改动和未跟踪页面文件，实施时以工作区当前内容为基线，不用仓库旧版本覆盖它们。

### 9.2 组件与内容 API

建议模块组件均为 server-compatible 静态组件，不新增 `use client`。

```ts
type MethodStage = {
  id: "prospect" | "deliver" | "review";
  label: string;
  title: string;
  body: string;
  output: string;
};

type MethodValueContent = {
  eyebrow: string;
  title: string;
  intro: readonly [string, string];
  stages: readonly [MethodStage, MethodStage, MethodStage];
  closing: string;
  responsibility: string;
  link: { label: string; href: "#prospect-method" };
};

type DiagnosticQuestion = {
  id: "page-coverage" | "business-alignment" | "page-credibility";
  title: string;
  check: string;
  decision: string;
};

type ExampleField = {
  id: "target" | "observation" | "judgment" | "action" | "confirmation";
  label: string;
  body: string;
};
```

模块 2 内容对象必须具备：eyebrow/title/intro/outcome、contextStep（标题、说明、四项维度、解释）、diagnosisStep（三个问题与 L1–L8 说明）、scopeStep（标题、说明、五步阅读结构、示例标签/标题/五个字段）、delivery（标题、正文、下一阶段说明、责任分工、范围说明、报告链接）。实现时将该形状定义为显式类型，不以 `any` 或任意 JSON 代替。

- Props：各组件仅需可选 `content`，默认值来自集中内容文件；布局锚点为组件固定常量。
- 必需内容缺失视为开发错误，通过类型与内容测试发现，不在生产页面补写营销文案。
- map 使用稳定 id 作为 key，不以展示顺序编号承担业务身份。
- 没有 CMS、远程内容请求或额外运行时校验库需求。

## 10. 实施步骤

1. 记录工作区状态，确认本规格与英文文案的审阅结果。
2. 在现有内容文件中加入两个独立内容对象及必要类型，不改原有数组。
3. 实现两个静态 section，先保证信息顺序和完整内容，再调整响应式排版。
4. 在 `MethodV22` 精确替换旧前两段；保留后文及导出组件。
5. 补充内容、结构、链接和示例标识测试。
6. 执行类型检查、测试与构建；再检查真实浏览器中的宽度、缩放、键盘和链接行为。
7. 交付实际截图、测试结果与剩余限制；未经发布流程不宣称上线。

## 11. 验收清单

### 11.1 内容验收

- [ ] M1.01–M1.16 与 M2.01–M2.25 全部有明确落点，无漏字、无占位文案。
- [ ] 模块 1 的三个阶段均有具体成果，不只剩抽象标题。
- [ ] 模块 2 的竞争背景、三个诊断问题、条件式建议和完整示例都默认可读。
- [ ] 首次出现私有表现数据时明确需要客户连接/授权。
- [ ] 核查意图、业务能力、页面遗漏等未被宣传为全自动确定结论。
- [ ] 示例标签醒目，与示例卡同处，不只在页脚免责声明中出现。
- [ ] 竞品页面不被用作排名因果证明，检查范围内未发现不等于全站不存在。
- [ ] L1–L8 只作为页面诊断组成部分出现。
- [ ] 不包含未经验证的获客率、收入、排名提升或耗时节省数字。
- [ ] 无「全部免费」「保证排名」「官方背书」「自动监控复盘」等错误承诺。

### 11.2 功能与语义验收

- [ ] `/framework` 仍正常呈现，唯一 H1，无重复 ID。
- [ ] `#prospect-method` 点击后定位正确，标题不被 Header 遮挡。
- [ ] `/sample-report` 链接与现有页面连通，未触发生成/登录/支付操作。
- [ ] 已有 `#diagnostic-map`、`#reasoning`、L1–L8、FAQ 与页底 CTA 正常。
- [ ] 键盘可访问全部链接，focus-visible 明显，静态卡片不产生多余焦点。
- [ ] FAQ JSON-LD 与可见 FAQ 保持现有一致性，metadata 不意外变化。

### 11.3 视觉验收

- [ ] 320、390、768、1024、1440px 宽度检查；页面无横向溢出。
- [ ] 200% 缩放不遮挡正文、不截断成果行或示例条件。
- [ ] 桌面三列阶段可读；窄屏阅读顺序与 DOM 相同。
- [ ] 示例在窄屏按字段顺序完整展示，无内部横向滚动依赖。
- [ ] 文字对比度和链接焦点样式实测通过目标。
- [ ] 不因字体加载或条件渲染发生明显布局跳动。

### 11.4 自动化与人工验证

组件测试至少覆盖：三个阶段稳定顺序、三项诊断问题、五个示例字段、示例标识、两个链接目标、单 H1、原有后文保留。行为断言优先于整页大快照。

建议从前端根目录执行：

```sh
npm run typecheck
npm test -- src/components/framework/MethodSections.test.tsx
npm test
npm run build
```

上述为后续实施要求，本规格撰写阶段不声称已运行。浏览器验证优先使用已有测试设施，不为了静态文案页面引入新依赖。构建若被现有环境配置阻塞，应明确记录原因，不为通过而更改生产认证或密钥。

浏览器验收只访问公开方法页和样例页，不创建付费分析、不使用真实客户资料、不消耗点数。

## 12. 交付、来源与后续维护

本规格通过后，实施交付应包括：

1. 更新后的页面与两个可独立维护的 section。
2. 完整英文内容源与测试。
3. 桌面、移动端截图及锚点/键盘检查记录。
4. 实际执行的测试结果与未完成项，不沿用旧任务的测试数量。
5. 明确「本地实现」或「已发布」状态，不混淆。

内容事实核对优先级：实际产品行为与合同 → 对应设计规格 → 本营销文案。若发现冲突，收窄文案或提出明确差异，不为适配文案扩大产品能力。

相关本地依据（相对本文件目录）：

- [V2.2 总体计划](2026-08-26-searchtrust-v2-2-development-plan.md)
- [公开发现设计](2026-08-31-searchtrust-v2-2-public-findings-design.md)
- [网站与 GBP 一致性设计](2026-09-01-searchtrust-v2-2-site-gbp-alignment-design.md)
- [公开行动设计](2026-09-02-searchtrust-v2-2-public-actions-design.md)
- [第一方发现设计](2026-09-08-searchtrust-v2-2-first-party-findings-design.md)
- [跨来源发现设计](2026-09-08-searchtrust-v2-2-cross-source-findings-design.md)

Google 来源沿用页面后续参考区；本轮不将其扩展为 Google 对产品的认证，也不添加本轮尚未核对的新权威主张。

后续更新规则：产品能力或报告字段变化时复核相关表述；模块 3–7 定稿后统一检查页面重复内容和编号。新的文案确认应回写本规格与集中内容对象，避免讨论稿、规格与页面各自演化。
