# SearchTrust v2.2 公开模式 Findings 设计

日期：2026-08-31

范围：V22-032 当前证据可支持的第一批公开规则及覆盖汇总。

状态：用户已确认“不新增采集、缺证据标记未检查”的范围及总体设计；本书面规范待用户审阅，业务代码尚未实施。

主要仓库：`SearchTrust-RD`。前端 `search-trust` 仅参与共享样例与冻结合同兼容性验证，不新增 UI。

## 1. 已确认的目标与范围

将 V22-031 已校验快照和证据索引转换为可追踪、可证伪、可重复执行的公开 Findings。每条发现具有固定规则 ID/版本、证据引用、适用范围、置信度、缺失数据和改变结论的条件。

已比较两种推进方式：先实现现有证据支持的规则，或先增加客户公开 GBP 等采集。用户选择前者。本轮保留客户 GBP 对齐的未检查记录，不额外查询供应商，也不以竞品公开 GBP 冒充客户资料。

交付边界：

- 站点结构：已观察到的 HTTP 异常、明确 noindex 标记、重复标题。
- 市场：同一采样上下文内客户站点域名是否出现，以及与确认竞品的可比位置差异。
- 竞品：客户与至少两家确认竞品在已检查库存内的指定页面类型差异。
- 汇总：客户已检查站点与页面类型集群的发现、覆盖状态，以及保留原含义的八层未检查/覆盖记录。
- 仅为规则所需的现有快照字段补充可追踪证据映射，不新增采集数据。

不交付第一方指标规则、跨源业绩归因、三项行动、优先级引擎、Dify 文案、完整报告组装、版本差异、UI、PDF、快照持久化或授权服务。冻结报告/API 模型、合同版本 `2.2.0`、Schema 和生成类型不变。v2.1 行为不变。

两个 v2.2 开关继续保持默认 false，worker 继续使用 `UnavailableV22Executor`；不推送、不部署。

**完成口径：本轮完成公开规则第一批及覆盖汇总，不等于完整 V22-032 的全部语义分析已完成。客户 GBP 实质对齐和八层完整语义规则仍有后续工作，不能在完成记录中隐藏这些边界。**

## 2. 当前基础及不可越过的语义边界

已核对：

- `EvidenceBuildInput` 提供固定 case、站点、市场、查询、设备/语言、确认竞品、evaluated_at 和带绑定的来源；`build_evidence_index` 是纯函数并完成摘要、归属、时效与资格检查。
- `EvidenceBuildResult` 包含 evidence_index、source_traces、source_summaries、coverage_gaps。逻辑选择器和真实快照引用已存在，不需要从证据文案猜字段。
- 冻结 `Finding` 要求至少一条 evidence_id；冻结 `LayerAssessment` 允许 not_checked 和空引用。因此缺证据应进入内部检查记录，不制造 Finding 或伪造证据。
- 客户站点快照、SERP 和竞品 collection 已存在。客户自己的完整公开 GBP 不在当前输入模型中。
- 旧版 `parse_rule_results` 的大量语义规则来自 Dify 规则向量；不能将它们直接包装成新的确定性规则。
- 旧版 Foundation 指业务资格基础，不是通用 HTTP 健康；Algorithm Fit 也不是 SERP 名次。技术异常、重复标题和排名差异都不足以直接确定相应信任层的整体优劣。

本轮保持“发现”和“八层语义评级”分离：结构/采样事实可以成为 Finding；没有相应语义规则覆盖时，八层仍为 not_checked，包括可能八层全部未检查的合法结果。

## 3. 内部接口与处理流程

建议入口为 `build_public_findings`，输入为新的严格内部 `PublicFindingsInput`，包含：

- `evidence_input: EvidenceBuildInput`；
- 本模块内部资源限制 `PublicFindingsLimits`。

本轮要求 context.report_type 为 prospect，拒绝实际第一方快照；missing_sources 内的第一方未连接说明可保留，但不产生第一方规则。后续验证模式复用公开规则需另行明确输入边界。

每次构建固定流程：

1. 严格验证输入、有限数值及当前公开模式限制。
2. 调用现有证据构建器验证全部来源，生成本次完整证据及内部追踪；不接受外部随意拼装的 Finding 或未复核索引。
3. 建立只读证据视图：按快照、来源、页面 URL、逻辑字段、查询及竞品 ID 索引，同时保留同一已验证快照的字段存在性、实际采样时间与完整返回集合，用于覆盖和可比性判断。触发 Finding 所依据的事实必须有证据引用，不能只读取原始字段而省略其证据映射。
4. 在版本化规则目录下执行站点、市场、竞品规则，生成内部 RuleEvaluation 和触发的 Finding。
5. 汇总客户采样站点及页面类型集群；构建八层覆盖记录。
6. 统一验证全部引用、去重、确定性排序和资源上限后返回。任何完整性错误均不返回部分成功结果。

输入不得被修改；不得访问网络、数据库、Redis、LLM 或运行时时钟。实际时效只使用输入 evaluated_at。

同一来源类型在本轮最多提供一个不同绑定：site、serp、competitor 各一个，无论其后续资格检查是否通过。完全重复输入可由 V22-031 去重；多个不同快照不能通过“取列表最后一项”或猜测最新记录消歧，应返回明确错误，由调用方选择同一分析批次。

来源绑定仍由可信应用层提供。本模块不证明数据库记录存在、租户权限或共享市场授权，不从 job UUID 派生 snapshot UUID。

## 4. 输出及规则状态

内部 `PublicFindingsResult` 包含：

- `ruleset_version`：`v22_public_findings_v1`。
- `evidence_result`：本次构建所得 EvidenceBuildResult，包括必要的新增派生计数证据。
- `findings`：冻结 Finding 的列表，允许内部结果为空。
- `rule_evaluations`：每个实际评估目标的规则状态、固定原因、有效证据/比较证据、关联 finding_id 和限制。
- `site_rollup`：客户采样站点的发现引用、覆盖计数、八层记录。
- `cluster_rollups`：按现有 SitePageType 分组的客户页面集群汇总，不按猜测的业务主题另建聚类。

RuleEvaluation 状态固定为：

| 状态 | 含义 | 是否生成 Finding |
| --- | --- | --- |
| triggered | 规则条件成立，引用充分 | 是 |
| not_triggered | 此规则在明确覆盖范围内具备判定条件，但未满足触发条件 | 否 |
| not_checked | 缺少输入、输入不可用、不够可比或本轮尚无语义规则 | 否 |

not_triggered 不等于页面、业务或信任层整体良好。无法比较的情况不写成 not_triggered。

固定原因至少包括 source_missing、source_ineligible、field_not_observed、insufficient_sample、identity_unresolved、rank_basis_mismatch、comparison_time_gap、ambiguous_page_observations、customer_public_gbp_missing、semantic_rules_not_implemented。完整来源限制另外保留，不解析自由文本 limitation 来决定规则真假。

规则覆盖记录没有证据时可以使用空引用；Finding 不可以。构建器结果不得通过添加假 Finding 来满足完整报告的非空约束。

无站点来源时，三条站点规则各产生一个站点级 not_checked，不编造页面目标；无 SERP 时，两条市场规则按已确认 query 和三个 result_type 产生检查记录；竞品规则按三个固定页面类型产生检查记录。已有来源时，HTTP/noindex 按真实页面评估；标题规则为每个重复组产生 triggered 记录，另有站点级覆盖记录保存不可比较页面，避免部分标题重复掩盖其他页面没有取得标题。

## 5. 证据视图及必要映射补充

### 5.1 复用规则

只有来源资格通过的业务证据可以参与业务判断。coverage 证据仅支持检查状态和限制，不能作为“客户缺少服务”“业务能力弱”等业务事实的依据。

引用通过内部 selector、source_trace、snapshot_id 和 locator 联合确认，不能仅靠 source_type 或文本相似度归组。SERP 中相同商家跨查询、结果类型或时间上下文的证据不得混用。

客户 URL、确认竞品 URL 的归属使用现有 URL 模型与严格域名匹配：小写 host 并移除单个 `www.` 前缀；不做任意子域、子串或同名商家匹配。客户和确认竞品域名互相重叠、或多个确认竞品域名归一化后重叠，应拒绝为输入歧义。

### 5.2 计数证据

竞品差距不能用“没找到某条 evidence”证明客户页面类型数量为零。现有 page_type_counts 又可能包含非 2xx 的 checked 页，不能直接当成可用 HTML 页面数量。

因此在 site 适配器（含嵌套 competitor site）中补充以下确定性派生计数：

- `eligible_html_page_count`：check_status=checked、HTTP 200—299、content_type 的媒体类型为 text/html 或 application/xhtml+xml 的页面记录数量。
- `eligible_page_type_count`：上述范围内 service_detail、service_area、location 三种类型分别的数量。

前述计数全部按当前不可变库存记录计算，绝不代表互联网全站页面总量。即使类型计数为零，也必须存在至少一个 eligible HTML 页面，才允许将其用于客户“采样中未观察到该类型”的判断；总 eligible 数量为零时只说明无法检查。

派生计数用标量 int 表达，保留现有真实 snapshot_id、实际来源类型和 competitor_id。选择器 record_context 包含根 URL、计数类型、页面类型（如适用）及独立派生版本 `eligible_html_counts_v1`；origin_paths 指向该快照内的 pages 数组及 completed_at。collected_at 使用对应 SiteInventorySnapshot.completed_at，嵌套竞品站点不以较晚的 collection 完成时间冒充采样时间。trace 中的选择器与版本能够重新执行过滤和计数。

本轮不增加缺失 title/h1 的业务规则，不以 null、空列表或未生成证据推定页面功能缺失。noindex 仅在已有实际 token 出现时触发；无 token 的检查记录只能说明当前快照字段中未观察到它。

新增计数不修改既有观察的内容、ID 算法或 ID。测试必须保证相同输入的旧 evidence ID 集仍为新结果的子集。V22-031 生成样例可因新增观察而更新，但原始快照输入、冻结合同、V22-030 样例和字符表不改。

## 6. 首批规则目录

以下 ID 的初始 rule_version 均为 `1.0.0`。这些是本产品的确定性审核规则，不是 Google 官方处罚、质量或排名阈值。

### 6.1 站点结构

**`v22_public.site_http_error`**

- 目标为客户库存中 check_status=checked 且具有实际 status_code 的页面。
- 400—599 触发；500—599 severity=high，400—499 severity=medium。其余实际状态不触发本规则，不据此判断其他功能正常。
- statement 只说明该 URL 在该快照响应了某状态码；429、超时类上下文等不能写成永久不可访问。采集失败或 robots_disallowed 不伪造 HTTP 状态，进入 not_checked。
- 每 URL 一个 Finding，引用 status_code 与相关实际检查证据；classification=fact，confidence 不高于 medium。
- 改变条件为取得该 URL 新的有效快照并重新核实状态，而不是承诺修复后排名提升。

**`v22_public.site_explicit_noindex`**

- 仅评估 eligible HTML 页面；实际 meta_robots 条目按逗号/空白分词并大小写归一，精确 token `noindex` 存在则触发。不得对子串匹配，不在本轮额外解释未保存的响应头或其他指令。
- 引用真实 meta_robots 证据和页面状态；classification=fact，severity=medium，confidence 不高于 medium。
- 只声称快照观察到了 noindex 标记，不声称 Google 已删除索引，也不自动认定业务方应移除该标记。
- 空列表最多使当前字段检查 not_triggered，须明确只检查已保存的 meta_robots 范围，不声明页面不存在任何形式的索引限制。

**`v22_public.site_duplicate_title`**

- 仅使用 eligible HTML 页面上实际存在的非空 title；首尾空白去除、连续空白折叠并 casefold 后比较，不删除标点、不作语义改写。
- 至少两个不同有效页面 URL 的标题归一化后相同，产生一个标题组 Finding。URL 身份使用 final_url（删除 fragment，保留 path/query）；多个原 URL 指向同一个 final_url 只算一个页面。
- 同一个 final_url 在库存中对应相互冲突的标题时，相关记录标记 ambiguous_page_observations，不择一猜测。同组标题但一个页面没有取得 title，不将其补入该组。
- 引用每个页面的实际 title、status_code/final_url；classification=fact，severity=low，confidence 不高于 medium。声明“采样页面重复标题”，不推论正文重复、索引冗余或页面没有独立价值。
- 全站只有一个可比较页面或输入不足时 not_checked；已具备至少两个可比较页面且没有重复组，才能将当前样本检查记为 not_triggered。

### 6.2 市场采样

分组键为同一 SERP snapshot、query、result_type、坐标、country、language、device。maps、local_pack、organic 分开；只处理 succeeded 调用的实际返回记录。source_trace 中保留 call/record 标识。

客户/竞品只通过返回 URL 的归一化 host 与确认站点域名相等来定位，不以 display_name 代替身份。每个域名在同组中多次出现时，先确认其匹配记录的 rank_source 一致，才使用其最小实际 position，引用所有并列最小位置及其 URL 证据；同域名记录的 rank_source 冲突则该域名位置不可比较，不推算不存在的位置。

**`v22_public.market_site_domain_unobserved`**

- 对成功且非空的分组，如果全部记录都有可解析 URL，而没有客户站点域名匹配，则触发。
- 任一记录缺少 URL 时，此项 absence 判断为 not_checked；成功空响应也只作为覆盖缺口，不生成市场弱势结论。
- Finding 只说“本次该类返回结果未观察到客户站点域名”，不说客户商家不存在、没有曝光、绝对排名落后或排在第 N 名之外。
- 引用该组成功调用、完整返回 URL 证据；classification=fact，severity=low，confidence=low。范围必须包含本次查询、地点、设备和结果类型。

**`v22_public.market_confirmed_competitors_ahead`**

- 客户在该组有已识别位置，至少两家不同的确认竞品也有位置，且至少两家的最佳实际 position 严格小于客户最佳 position，触发。
- 客户与参与判断的竞品必须使用相同 rank_source。provider_position 与 response_order 不混比；不同基准的竞品标为不可比较，只有至少两家与客户同基准时才具备触发条件。相同 position 不算领先。
- 两家已识别竞品已满足领先条件时，可触发并披露第三家的缺口；未满足触发条件且三家竞品未全部可比较时，记为 not_checked，不据部分样本宣告不存在差距。三家全部可比且不足两家领先才 not_triggered。
- evidence_ids 引用客户位置、URL 及调用上下文；comparator_ids 引用参与判断的竞品位置、URL。全部引用保留原 SERP snapshot，不使用 competitor collection 的跨查询 best_position 作本次比较。
- classification=fact，severity=medium，confidence 不高于 medium。只陈述当前比较，不给出排名原因或业绩预测。

### 6.3 竞品采样资产差异

**`v22_public.competitor_sample_page_type_gap`**

- 只针对 service_detail、service_area、location 三个已存在的页面分类，逐类型执行，不将笼统分类解释为某项精确服务或某个具体城市的缺失。
- 客户 eligible_html_page_count>0，目标 eligible_page_type_count=0；至少两家不同确认竞品的有效库存中 eligible_html_page_count>0 且同类型计数>0，触发。
- 客户 evidence_ids 引用实际零计数、eligible 总数及库存范围证据；comparator_ids 引用每家参与竞品的正计数、eligible 总数及身份范围，至少两家，不以同一竞品的两页冒充两家。
- classification=inference，severity=low，confidence=low。statement 为“已检查样本中存在页面类型覆盖差异”，并明确页面分类为既有启发式结果、客户/竞品采集上限不同。
- 跨来源比较增加固定可比性条件：客户 site 与参与比较的各 competitor.site_inventory 的 completed_at 最大值减最小值不超过 24 小时，边界包含恰好 24 小时；使用真实站点采样完成时间，不使用 collection 较晚的完成时间。计算时纳入客户及三个确认竞品中所有资格通过且 eligible_html_page_count>0 的库存，无论其目标类型计数是否为零；整体超窗则该类型比较 not_checked，不选择有利子集凑两家。此窗口是本产品采样对比限制，不是快照过期规则，不修改 V22-031 eligibility，也不为 expires_at=None 编造过期时间。
- 判断顺序先检查客户资格，再看客户类型计数：客户有效且类型计数>0，直接 not_triggered，无需比较竞品；客户采样不可用则 not_checked。仅客户为零时执行竞品及时间窗口比较：可比竞品不足两家时 not_checked；全部三家都可比较、但仅零或一家具有该类型时 not_triggered；只看到部分竞品且不足两家支持触发时 not_checked。
- 改变条件包括新增同批次可比库存、发现客户该类型页面、重新核实页面分类或确认竞品范围改变。

### 6.4 客户公开 GBP 对齐

保留 name/address/phone/service_area 四个明确的内部检查目标，对应 `v22_public.gbp_name_alignment`、`v22_public.gbp_address_alignment`、`v22_public.gbp_phone_alignment`、`v22_public.gbp_service_area_alignment`。本轮均为 not_checked/customer_public_gbp_missing，不产生 Finding。

这不是已完成的 GBP 比较能力。未来补齐客户真实公开资料和可比页面事实后才可启用相应规则；不得用竞品资料、查询展示名称、只有 URL 的用户输入或第一方 Performance 指标填补。

## 7. 站点、集群与八层汇总

客户页面集群只按已保存的 SitePageType 分组；页面类型来源为 checked 库存记录，并保留每页 URL 和证据。未检查页面另外计入覆盖缺口，不分配一个猜测的集群。

site_rollup 的 site 表示“本次客户库存范围”，不是证明抓取了整个网站。汇总列出 discovered、checked、eligible HTML、deep analyzed 数量和来源限制，语句必须保留采样范围。cluster_rollups 只为当前库存真实出现的类型建立记录，不生成虚构空集群。

客户 site 缺失或不合资格时，site_rollup 仍保留 case/站点范围与未检查说明，但各采样数量使用内部 nullable 字段表达未知，不能填零；cluster_rollups 为空。市场来源独立可用时仍可生成站点上下文的市场 Finding，不以缺少站点快照为由虚构或删除市场证据。

页面 Findings 按 affected_urls 的真实库存关系进入集群；跨集群重复标题的同一个 finding_id 可出现在多个相关集群，但站点汇总去重。市场及竞品资产差距属于站点采样上下文，不强行分摊到客户某个页面或尚不存在的页面集群。

八层使用冻结 REQUIRED_LAYER_KEYS 的固定顺序。需要严格区分：

- 站点/集群的 `finding_ids`：可以引用本轮的结构或采样发现。
- 每个 `LayerAssessment.finding_ids`：只能引用足以支持该层语义判断的发现，不能因为名字类似而映射。

本轮首批规则不是旧版 38 条语义规则的替代，也不足以对任何一层做完整语义评级。因此本版站点及集群的八层 status 均为 not_checked，层级 finding_ids/evidence_ids 为空，summary 明确说明当前范围和 semantic_rules_not_implemented；覆盖计数与结构/采样 Finding 引用保留在站点/集群外层。

这使得汇总结构和覆盖信息可以使用，同时不把重复标题当成“页面没有价值”、不把 noindex 当成“不具备业务资格”、不把排名当成算法信任评分。后续引入真正的层级语义规则时需独立扩充规则目录和汇总判定，不能自动将当前 not_triggered 升级为 good。

## 8. Finding 身份、文案及引用完整性

Finding ID 使用 `fn_` 加完整 64 位小写 SHA-256。身份版本为 `v22_public_finding_identity_v1`，哈希包含：case_id、rule_id、rule_version、规则集版本、规范化目标键、排序去重的 evidence_ids/comparator_ids。目标键包含 URL/标题组/页面类型或完整市场分组上下文；不使用列表下标、当前时间、随机 UUID 或生成文案。

规则版本变化必须改变身份；同一输入重复构建、来源遍历顺序改变、证据索引遍历顺序改变不应改变输出。快照内部数组改动但保留旧摘要仍由 V22-031 拒绝，不属于遍历顺序稳定性保证。

同 ID 内容完全相同才能合并；rule payload、classification、severity、scope、confidence、缺口、条件或引用发生冲突时整体失败，不后写覆盖。数组按语义固定排序，URL/查询/引用/缺口去重。长目标内容保留在内部目标结构，scope 用有界的说明/摘要标识，不截断 URL 后参与身份。

Finding 的 statement 和 change_conditions 采用固定英文模板，中文用于开发说明；不调用文案模型。模板不得加入没有相应证据的数值、业务名称、排名原因或承诺。fact/inference 的定义针对上述限定陈述，不为严重度、阈值或启发式分类背书。

confidence 不能高于最低支持证据置信度，且不能高于规则目录上限。本轮不产生 high confidence 或 estimate。severity 是明确版本化的产品审核级别，不等于经济损失、法律风险或搜索引擎处罚程度。

Finding 没有独立 limitations 字段：会影响判断的来源/采样限制及规则缺口，以固定模板写入 missing_data 和 statement 的范围说明；完整限制仍保留在 evidence_result 与 RuleEvaluation。change_conditions 必须非空且针对可取得的新证据，而不是空泛“进一步优化”。

返回前验证：所有 Finding 引用存在；comparator 为正确规则上下文和确认竞品；客户证据没有串到竞品；全部汇总引用存在且目标对应；不存在覆盖证据冒充业务指标、孤立 trace、同 ID 冲突或引用重复计数。

## 9. 错误、资源与实现边界

沿用 V22-031 的快照/摘要错误代码，不吞掉为缺失数据。Findings 层新增固定、无敏感数据的错误：

- `V22_FINDINGS_INPUT_INVALID`：模式、来源数量或输入身份歧义。
- `V22_FINDINGS_REFERENCE_INVALID`：证据/比较对象/汇总引用不完整。
- `V22_FINDINGS_ID_CONFLICT`：同编号不同内容。
- `V22_FINDINGS_LIMIT_EXCEEDED`：输出资源超过上限。

正常缺数据、不够可比、来源过期使用 not_checked，不抛业务异常；非法来源、篡改或程序内部引用冲突不允许降级成成功结果。公开错误说明不回显 URL、查询、评论、原始载荷或令牌。

默认上限：唯一 Findings 10,000 条、RuleEvaluation 20,000 条、完整 PublicFindingsResult（包含 evidence_result）20,000,000 字节；内部测试可调低，不允许调用方调高。上游证据上限继续生效。超限明确失败，不截断；标题分组用索引而非全站逐页两两组合。

建议模块按职责拆分：严格模型/错误、规则目录/身份、只读证据视图、站点规则、市场规则、竞品规则、汇总、构建入口。现有证据适配器只增加第 5 节需要的计数，不执行 Finding 规则；不在旧版评分器中插入 v2.2 分支。

## 10. 验收与回归

每个规则必须有明确 expected values 的固定虚构输入，测试不能只对照待测实现自己导出的结果。

最低覆盖：

1. 三项站点规则：HTTP 399/400/499/500/599 边界、采集失败/robots 不伪造状态、noindex 精确 token/子串反例、空 meta 只说明当前字段、标题空白/大小写、同 final_url 去重及冲突、两个页面阈值、缺 title 不补缺失结论。
2. 市场：maps/local_pack/organic 分开；同查询不同地点/设备/语言不串；成功空响应、缺 URL、同名不同域名、客户已出现/未观察到、两家竞品阈值、并列位置、混合 rank_source、第三家缺失的触发与未检查边界；不使用跨查询 best_position。
3. 竞品：零/一家/两家/三家支持，客户真实零与无有效采样区分，404 分类页不算可用资产，单竞品多页不凑两家，collection/market 关联检查，24 小时及超过边界；page_type 是采样分类而非精确业务缺失。
4. 资格与身份：prospect 拒绝第一方快照；过期/不健康来源只提供覆盖；多个不同同类快照或域名歧义拒绝；无快照不生成 Evidence/Finding。
5. 编号与纯函数：跨进程重复、来源与处理顺序变化、输入不变、规则版本变化、引用去重、同 ID 冲突、长目标与字节/数量上限。
6. 汇总：真实页面类型集群、跨集群引用不重复计站点数、市场发现不归造页面、八层固定顺序且不以 not_triggered 生成 good，空 Findings 与八层 not_checked 为合法内部结果。
7. 兼容性：V22-031 原观察 ID 保持、增量计数可追溯、旧版回归通过、冻结模型/API/Schema/类型不变。

双端新增独立 Findings 样例目录和 manifest，不混入 V22-030 validation 或冻结 contracts。后端生成实际 Finding/Evidence 输出；前端用冻结 Schema 的 `$defs/Finding`、`$defs/EvidenceItem`、`$defs/LayerAssessment` 保留完整 `$defs` 验证，并检查样例引用、未知字段和非法标量等反例。不得构造占位 ExecutiveDecision/Actions 来伪装完整报告通过。

导出器延续预定文件、完整路径预检、哈希和只读 `--check` 约束，保留无关文件，不整体替换代码目录。V22-031 样例若更新，需由原导出器生成并同时复验两端，不手工改前端副本。

实施完成后应运行新增规则与证据定向测试、后端完整 pytest、前端合同及完整测试、非增量 TypeScript 检查、所有合同/辅助样例/证据/Findings 资源漂移检查及差异审查。浏览器或生产外部联调不属于本轮验收，不把未运行项目写成通过。

当前文档阶段只完成设计整理与自查，未运行尚不存在的 Findings 测试，也未宣称新增规则已经可用。

## 11. 后续仍待完成的能力

- 客户真实公开 GBP 资料绑定与 name/address/phone/service_area 实质对齐。
- 旧版各层语义规则的逐条确定性支持或明确的其他实现方案，以及有资格的八层 good/medium/weak 判定。
- 超出本次库存采样的全站资产、精确服务/地区语义和正文独特性分析。
- V22-033 行动生成、V22-034 文案合同，以及 V22-070/071 第一方/跨源规则。

书面设计获用户确认后，再制定实施计划；不因为总体设计已确认就跳过本文件中的阈值、覆盖口径与后续边界审阅。
