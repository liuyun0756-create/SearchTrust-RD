# SearchTrust v2.2 网站与公开 GBP 对齐设计

日期：2026-09-01

状态：用户已批准完整书面规格，并于 2026-09-01 确认开始实施。

范围：V22-032 的客户实体范围选择，以及网站名称、地址、电话、服务区域与客户公开 GBP 的离线实质对齐。

主要仓库：SearchTrust-RD。search-trust 只增加共享合同样例测试，不修改产品运行时或页面。

## 1. 目标和已确认产品口径

本轮在现有网站业务候选与客户公开 GBP 快照之上，实现四项可复核、确定性的对齐判断，并把现有四项 `not_checked/gbp_alignment_not_implemented` 检查替换为真实 RuleEvaluation 和必要的 Finding。

用户确认的 `BusinessIdentity` 是客户实体、站点、经营模式及主要市场的唯一权威边界。公开网站或 GBP 的实际字段仍是待审计事实；确认身份不能覆盖、改写或补造实际观察值。

已确认规则如下：

- 同一确认实体下允许多个名称、地址、电话及服务区域候选；不选择第一条、首页第一条或任意“主值”。
- 名称、地址和电话只要至少一个合资格网站候选与 GBP 匹配，即为对齐。其他未匹配候选保留为差异披露，不自动构成冲突。
- 来源可用且内容成功检查后，一侧有值、另一侧没有值属于问题；两侧都没有值同样属于问题。
- 来源缺失/失效、请求失败、网站必要页面未完成检查或客户实体归属不明确时为 `not_checked`，不能把未知伪装成缺失或匹配。
- 经营模式控制适用性：storefront 必查地址、服务区域允许不适用；service_area 必查服务区域、公开地址允许不适用；hybrid 两项都必查；三种模式均必查名称和电话。
- 服务区域分为完全匹配、部分匹配、不匹配。部分匹配生成低严重度改进 Finding，不匹配生成更高严重度 Finding。
- 允许受控、可解释的确定性语义等价；禁止模糊相似度、关键词包含、LLM 判断、联网补值或地理编码。

本轮不实现生产执行器接线、数据库、API、UI/PDF、Dify、Actions、八层规则映射、真实供应商采集或发布。不开启任何生产开关。

## 2. 现状和方案选择

当前代码已经具备：

- `AnalyzeRequest.business_identity`：用户在正式分析前确认的商家名称、站点、规范化域名、经营模式、主要地点和可选公开 GBP URL。
- `build_site_business_facts`：从绑定的保存 HTML 中提取多值网站候选和精确原文位置，不选择主值、不判断冲突。
- `CustomerPublicGbpSnapshot`：绑定用户确认参考、强标识、字段状态、健康和身份状态的离线公开 GBP 快照。
- `build_public_findings`：构建并复核 Evidence、RuleEvaluation、Finding 和 roll-up；四项 GBP 检查目前仅做覆盖记账。

比较过三条路径：

1. 在现有 v2.2 Findings 流水线中显式接入，同时把选择、规范化和比较放在独立模块。该方案能在一个可信边界内重建候选和证据，并直接完成本轮 Finding；用户已选择此方案。
2. 先返回独立对齐结果、以后再接 Findings。隔离更强，但会保留一次无实际 Finding 的中间状态，不符合本轮范围。
3. 复用 v2.1 比较器。开发较快，但其主值、来源和多值语义与本轮批准规则不一致，不能直接复用最终结果。

可参考 v2.1 的失败样例和有限缩写表，不调用 v2.1 `build_page_facts` 或 `evaluate_gbp_rules` 作为 v2.2 判断来源，不修改 v2.1 文件。

## 3. 架构与入口

继续使用纯入口 `build_public_findings(PublicFindingsInput | dict) -> PublicFindingsResult`。`PublicFindingsInput` 新增：

- `business_identity: BusinessIdentity | None = None`：用户确认的客户实体范围；保持可选以确保旧调用完全兼容。
- `site_business_limits: SiteBusinessLimits = Field(default_factory=SiteBusinessLimits)`：可下调的网站候选提取上限。
- `site_gbp_alignment_limits: SiteGbpAlignmentLimits = Field(default_factory=SiteGbpAlignmentLimits)`：可下调的选择、比较、证据和输出上限。

这些都是内部输入，不修改冻结 HTTP API、ReportV22 Schema 或 TypeScript 报告类型。后续生产执行器接线时，才能显式从已有 AnalyzeRequest 传入 `business_identity`。

新增或调整模块：

- `site_business_selection.py`：把完整网站候选划分为 eligible、unresolved、excluded；不比较 GBP 值。
- `site_gbp_comparators.py`：版本化名称、电话、地址和服务区域规范化及两两比较；不选择实体、不生成 Finding。
- `site_gbp_alignment_models.py`：严格的选择审计、规范化记录、匹配对、字段结果和资源上限。
- `site_gbp_alignment.py`：组合网站结果、GBP 字段、缺失证据和四项字段结果，并独立复核引用。
- `public_gbp_findings.py`：旧输入继续走覆盖路径；新输入仅把已复核对齐结果转为 RuleOutcome。
- `findings.py`：显式构建网站业务结果、合并经复核的新证据、调用对齐模块并进行最终全局引用验证。
- `public_rule_catalog.py` / `findings_common.py`：为四项规则增加正式 RuleSpec，并允许新分支使用独立规则版本和服务区域部分匹配的严重度覆盖。

各模块不读取网络、时钟、文件、环境变量或数据库，不分配 UUID，不执行页面脚本或 CSS。

## 4. 数据流和兼容分支

完整新路径顺序固定：

1. 重验 PublicFindingsInput 及全部嵌套模型，拒绝模型实例混入字典后绕过校验。
2. 使用原流程构建 EvidenceBuildResult。
3. 校验 business_identity 与案件上下文、站点和客户公开 GBP 参考。
4. 从 EvidenceBuildInput 中唯一 site 来源构造最小 SiteBusinessFactsInput，调用真实 `build_site_business_facts`；不接受调用方提交的候选或结果。
5. 校验并选择合资格网站候选。
6. 读取同一请求内唯一且合资格的 public_gbp 来源，构建四项字段对齐结果。
7. 把网站候选 Evidence 和必要的字段状态 coverage Evidence 合并到本次 EvidenceBuildResult，更新对应来源摘要的 evidence_count 和完整限制说明。
8. 生成四项 RuleOutcome，与其他站点、市场和竞品结果统一去重、排序和计数。
9. 独立重建并验证候选选择、规范化键、匹配对、证据、追踪、RuleEvaluation 和 Finding。
10. 构建现有 roll-up，并重新验证完整 PublicFindingsResult 大小和引用。

兼容规则：

- `business_identity is None` 时完全不运行网站业务提取/对齐，继续输出现有四项 GBP 覆盖结果；旧输入、结果字节、Evidence ID、Finding ID 和 fixtures 不变。
- 有 business_identity 但 site/public_gbp 来源缺失、失效或身份不合资格时，新规则为 `not_checked`，不生成业务冲突 Finding。
- 新网站 Evidence 仅存在于显式新路径；`build_evidence_index` 的默认输出不改变。
- 新对齐不由其他 Finding 规则自动消费；coverage Evidence 的例外使用范围严格限定为四项 GBP 对齐。

## 5. 确认身份与绑定

在任何候选选择前验证：

- business_identity.site_url 与 EvidenceBuildContext.site_url 为同一完整规范 URL；normalized_domain 等于现有精确主域名规则结果。
- business_identity.primary_location 与 context.target_market 完整相同；不允许两个“主要市场”并存。
- business_identity.business_name 非空且只作实体范围锚点，不冒充网站或 GBP 观察。
- business_identity.operating_model 为 storefront/service_area/hybrid，并控制第 9 节适用性。
- context.customer_public_gbp 必须与 public_gbp 来源的 subject_reference_checksum、case_id 和 site_url 绑定。
- business_identity.public_gbp_url 非空时，必须与 CustomerPublicGbpReference.public_gbp_url 为同一规范 Google GBP URL；为空时不从网站或其他输入回填，但已绑定的客户 GBP 参考仍可用于本轮比较。
- site 与 public_gbp 来源分别通过现有完整摘要、案件、版本、时间、健康和身份校验；两个来源不要求同一采样时刻，但必须满足第 12 节时间可比性。

任一明确错配整体失败并返回固定安全错误；来源缺少或可信状态不足返回 `not_checked`，二者不能混用。

## 6. 网站候选资格

候选选择不根据 GBP 是否匹配来决定资格，防止“先看答案再选择来源”。选择输入只有确认身份、网站候选、页面状态和候选自身归属。

每项候选生成 `CandidateEligibility`：

- `state`: eligible / unresolved / excluded。
- `candidate_id`、field、entity_key、requested/final URL 和全部原 Evidence ID。
- 固定 reasons 集合，不含自由生成原因或原始值。
- `qualification_version=site_business_selection_v1`。

首版固定资格原因是 core_declared_entity、noncore_name_anchor、noncore_market_anchor、explicit_label_core、repeated_across_final_pages、structured_value_corroboration、ownership_unresolved、external_entity_hint、noncore_anchor_missing、page_not_eligible。原因只描述资格路径；不能包含实际业务值或把匹配 GBP 作为原因。

### 6.1 页面集合

核心页面类型为 home、contact、about、location。业务核心扩展类型为 service_index、service_detail、service_area。其他页面（team、review/testimonial、case study、blog、product、legal、other 等）属于非核心页面。

页面类型来自已绑定库存，不根据 URL 单词重新猜测。同一 final_url 从不同 requested_url 到达只算一个实际页面，保留全部来源但不能增加重复印证计数。

### 6.2 JSON-LD 记录

- 候选必须为 `source_kind=jsonld_property` 且 `ownership_status=declared_entity`；external hint、非法 hint 或其他原因形成的 unresolved 记录不参与比较。
- 核心和业务核心扩展页面上的明确商家记录可进入客户范围。
- 非核心页面记录只有在整条记录存在独立锚点时才合资格：记录名称与确认名称按第 8.1 节等价，或者记录地址/服务区域与确认 TargetMarket 按本地可验证文字明确相符。
- TargetMarket.latitude/longitude 不反向地理编码；只有 display_name 和 country_code 可作为文字锚点。
- 一条记录一旦合资格，其全部有效候选一起进入比较，不能仅选择恰好匹配 GBP 的字段。
- 多条合资格记录同时保留；不同 entity_key 不合并为一个虚构实体。

### 6.3 明确 DOM 标签

核心和业务核心扩展页面的 `source_kind=dom_label` 可进入比较。非核心页面的明确标签仍须由同一规范化值的跨页重复或合资格结构化记录印证。

标签规则和实际值继续由 `site_business_extraction_v1` 提供；本模块不能重新从全文、标题或邻近段落创造候选。

### 6.4 普通电话和地址元素

`tel_link` 和 `dom_address` 在同一规范化值已存在于合资格 JSON-LD 记录时直接由 structured_value_corroboration 获得资格。否则必须同时满足：

- 出现在至少两个不同 final_url；
- 至少一个页面类型为 home/contact/location/about；
- 每个来源页面本身可解析且属于已确认站点；

否则保持 unresolved。不能用“它与 GBP 相同”作为印证条件。

### 6.5 排除、归属不明与字段充分性

- external_entity_hint 等明确站外实体为 excluded；非核心记录缺少独立锚点、非核心标签未被印证及普通电话/地址未达到重复门槛时为 unresolved。证据保留但不参与字段结果。
- 只有归属不明候选、没有任何 eligible 候选时，该字段为 `not_checked/identity_unresolved`，不是 site_missing。
- 有 excluded 候选但没有 eligible/unresolved 候选，且必要页面已完整检查时，可以判 site_missing；被排除的第三方信息不能填补客户字段。

## 7. 网站“成功检查”的充分条件

不能因为只采样了任意一个页面就声称整个网站缺少字段。每个字段单独计算必要检查集合：

- business_name、phone：所有库存中已发现的 home/contact/about/location 页面。
- address：所有 home/contact/about/location 页面。
- service_area：所有 home/contact/about/location/service_area 页面。

至少必须存在并完成一个 home 深度页面。必要集合内任何页面结构检查失败、未深度采样、HTML 不可用、站外跳转、HTML/JSON 通道整体失败或出现无法解释的字段形态，该字段为 not_checked。

业务核心扩展页若已深度采样，其有效候选可以参与比较；它未被采样不阻止必要集合完成，service_area 类型除外。非核心页面不参与缺失充分性。

只有以下条件全部满足且没有 eligible/unresolved 候选，才是 site_missing：

- site 来源 ready；
- 必要页面集合完整；
- 对应支持通道已处理；
- 没有影响该字段的 jsonld_parse_failed、unsupported_context、unsupported_field_shape 或 html_parse_failed；
- 仅有 invalid_field_value 时视为“已检查但没有有效值”，可形成 site_missing，并在 coverage Evidence 中披露无效值存在但不复制其原文。

此“缺失”只表示已保存的批准样本及规则范围内没有合资格值，不宣称互联网或完整网站永久不存在该信息。

## 8. 版本化规范化与比较

规范化只生成内部键，不改变 SiteBusinessCandidate、GBP 原值或 EvidenceItem.original_value。每个键携带规范化版本、字段、country_code 和实际变换标签。

禁止 Levenshtein/Jaro 等模糊分数、向量相似度、关键词包含、任意子串、翻译、音译、LLM、远程字典或联网地理关系。

### 8.1 名称

`business_name_normalization_v1`：

1. Unicode NFKC；
2. 首尾去空白、连续 Unicode 空白合并；
3. casefold；
4. 安全展示标点转为空格，`&` 与独立词 `and` 统一；
5. 仅移除末尾、完整词匹配、版本化白名单中的法定实体后缀及其标点形式；白名单按确认 country_code 固定。

不删除服务词、品牌词、地点词、数字，不重排词，不扩展缩写，不使用确认名称替代实际候选。

原文经 NFKC 和空白折叠后完全相等为 exact_match；名称键相等为 semantic_match；否则 mismatch。

### 8.2 电话

`business_phone_normalization_v1` 复用仓库已锁定的 `phonenumbers==9.0.36` 及其本地数据，根据 TargetMarket.country_code 解析；运行时不联网。更改依赖或元数据版本必须升级规范化版本并重新生成新分支 fixtures。

- 比较完整国际号码；展示空格、括号、连字符、点号和本地/国际前缀差异可等价。
- 分机单独保留。基础号码一致但一侧缺分机为 semantic_match 并披露；双方明确分机不同则该对不匹配。
- 无法解析、长度或国家不可能的号码不进入匹配对；若成功检查后没有其他有效 eligible 电话，按 site_missing/gbp_missing 处理并披露 invalid value 状态。
- 不截短号码、不只比较末尾位数、不默认任意国家码。

### 8.3 地址

`business_address_normalization_v1` 为保守词法/组件比较，不做地理编码：

- NFKC、casefold、空白和安全标点统一；
- 结构化网站地址保持 streetAddress、locality、region、postalCode、country 分组件；美国地址复用仓库已锁定的 `usaddress==0.5.16` 解析 GBP 标量和网站标量。其他国家只对已有结构化组件及可确定的完整词法键做比较，无法可靠拆分的标量为 incomparable；更改解析依赖或数据表必须升级规范化版本；
- 统一受控方向词、street/road/avenue 等道路类型、地区和邮编展示格式；
- 门牌和街道核心必须一致；双方都观察到的 locality、region、postalCode、country 或 unit 不能冲突；
- 一侧缺邮编或 unit、其他核心一致时可以 semantic_match，并披露未比较组件；
- 原始规范化组件全部相同为 exact_match；上述受控等价为 semantic_match；明确组件冲突为 mismatch；解析不足以识别门牌/街道核心时该对 incomparable，不猜测匹配。

若所有实际地址对都 incomparable，字段结果为 not_checked/comparator_unsupported，而不是不匹配。成功检查但一侧没有任何地址仍按缺失规则处理。

### 8.4 服务区域

`service_area_normalization_v1`：NFKC、casefold、空白/安全标点统一，并在确认国家内统一同一地区名称附带的 region/country 展示后缀。不得从一个任意营销短语拆出多个城市，不推断城市属于县、都会区或半径范围。

对双方有效区域形成去重集合：

- 集合相等：exact_match；
- 交集非空但集合不等：partial_match；
- 双方非空且交集为空：mismatch。

匹配对、仅网站集合和仅 GBP 集合全部保留 Evidence ID。GBP 重复值不改变集合关系，但所有唯一来源追踪继续保留。

## 9. 经营模式和字段状态

字段适用性矩阵：

| operating_model | business_name | phone | address | service_area |
| --- | --- | --- | --- | --- |
| storefront | required | required | required | not_applicable |
| service_area | required | required | not_applicable | required |
| hybrid | required | required | required | required |

not_applicable 不生成 Finding；若该字段实际有双方值，可以保留 Evidence 和“未评估”限制，但不转换成对齐结论。

每字段内部 `SiteGbpFieldResult.state` 固定：

- exact_match
- semantic_match
- partial_match（仅 service_area）
- mismatch
- site_missing
- gbp_missing
- both_missing
- not_applicable
- not_checked

not_checked 另有固定 reason：source_missing、source_ineligible、identity_unresolved、site_content_not_checked、comparison_time_gap、comparator_unsupported。它不能携带 Finding ID。

判断顺序：

1. 经营模式不适用；
2. 来源、身份、时间和网站检查资格；
3. eligible/unresolved 候选状态；
4. 双方字段是否有有效值；
5. 实际两两比较。

公开 GBP 字段语义：

- collection succeeded 且 state=observed：有值；
- collection succeeded 且 state=not_returned/returned_empty/unsupported：字段缺失；
- request_failed、来源缺失/失效、identity 非 matched：not_checked。
- storefront 且 service_area_business=false 时 service_area 为 not_applicable；service_area/hybrid 仍按 required 处理。

结果映射：

| 内部状态 | RuleEvaluation | Finding |
| --- | --- | --- |
| exact_match | not_triggered/exact_match | 无 |
| semantic_match | not_triggered/semantic_match | 无 |
| partial_match | triggered/partial_match | low |
| mismatch | triggered/value_mismatch | 字段默认严重度 |
| site_missing | triggered/site_field_missing | 字段默认严重度 |
| gbp_missing | triggered/gbp_field_missing | 字段默认严重度 |
| both_missing | triggered/both_fields_missing | 字段默认严重度 |
| not_applicable | not_triggered/field_not_applicable | 无 |
| not_checked | not_checked/具体原因 | 无 |

需要在内部 EvaluationReason 增加上述稳定原因；冻结 Finding 结构不变。

## 10. 证据和缺失证明

### 10.1 实际值

网站实际值使用 site_business_facts 生成的 source_type=site Evidence；GBP 实际值使用 public_gbp_field Evidence。对齐模块不复制或重编号实际值。

RuleOutcome 中：

- 网站值放 `evidence_ids`；
- GBP 值放 `comparator_ids`；
- 匹配和未匹配候选均保留在内部字段结果；Finding 至少引用所有决定状态所必需的值，不通过只引用匹配值掩盖差异。

### 10.2 字段缺失 coverage Evidence

缺失是经过检查得出的状态，不虚构业务标量。新增 opt-in coverage Evidence：

- selector.category=coverage；
- record_context 使用 `site_gbp_alignment_v1`、字段、来源侧、快照、必要页面摘要、提取/比较版本和状态；
- normalized/original value 为固定状态标记 site_missing 或 gbp_missing；
- source_type=coverage、confidence=low；
- site trace 指向实际参与充分性判断的 selected page HTML 路径及必要库存 pages 路径；GBP trace 指向实际字段 state 路径；
- 固定说明明确它证明的是已保存样本中的检查状态，不是业务值或完整互联网缺失。

both_missing 必须同时具有一条网站 coverage Evidence 和一条 GBP coverage Evidence。单侧缺失必须具有缺失侧 coverage Evidence及另一侧全部合资格实际值 Evidence。

只有四项 GBP 对齐的专用验证器可以让 Finding 引用这些 coverage Evidence；其他规则继续执行现有“coverage 不可作为业务 Finding 证据”限制。

### 10.3 选择与匹配审计

`SiteGbpAlignmentResult` 内部保存：

- business_identity 的完整摘要，不复制为业务 Evidence；
- site/GBP snapshot ID 和 payload checksum；
- 每个 CandidateEligibility；
- 每字段全部网站/GBP Evidence ID；
- 每个规范化键的版本和变换标签；
- matched pairs（两侧 Evidence ID）；
- unmatched、incomparable 和 unresolved ID；
- 字段 state/reason、affected URLs、限制说明。

完整复核必须从原输入重建上述结果，验证任何候选不能跨字段、跨组件、跨页面、跨实体、跨快照借用。

## 11. Findings 规则

规则 ID 保持：

- v22_public.gbp_name_alignment
- v22_public.gbp_address_alignment
- v22_public.gbp_phone_alignment
- v22_public.gbp_service_area_alignment

旧兼容分支继续使用当前 rule_version=1.0.0 和原 not_checked 输出。显式新对齐分支使用 `GBP_ALIGNMENT_RULE_VERSION=1.1.0`；`findings_common.decision` 增加可选 rule_version，默认仍为现有版本，其他规则及 ID 不变。

四项规则加入 RuleSpec：classification=fact；基础 confidence=low（公开来源和网站声明尚未获第一方授权确认）。默认严重度：

- name：medium
- address：high
- phone：medium
- service_area：medium；partial_match 显式覆盖为 low

固定 statement 不复制原始名称、电话或地址：

- 网站与公开 GBP 的已检查字段未对齐；
- 网站缺少必需字段；
- 公开 GBP 缺少必需字段；
- 网站与公开 GBP 均缺少必需字段；
- 服务区域只有部分对齐。

实际值、匹配对和差异只在 Evidence/内部评估中展示。change_condition 要求取得新的网站与 GBP 快照，补充或统一对应字段后重新检查。

新路径 RuleTarget.kind=customer_gbp，snapshot_id 使用 site 快照作为被审计目标；affected_urls 是参与比较或缺失充分性判断的实际网站 final URLs。GBP 快照只通过 comparator Evidence 绑定。Finding ID 继续由 case、rule、rule version、target 及全部证据 ID 生成，不依赖 statement 或遍历顺序。

本轮产生的 Finding 进入 site_rollup.finding_ids。八层 LayerAssessment 仍按现有逻辑保持 not_checked/空引用；不在本轮顺带制定层级状态映射。

## 12. 时间可比性

两个来源各自必须在 context.evaluated_at 之前完成并保持合资格。为避免把跨时期变化误判为冲突：

- 默认最大采样完成时间差 30 天，可由调用方下调，不能上调；
- 超过时四项均为 not_checked/comparison_time_gap；
- 时间差按 site payload.completed_at 与 public GBP payload.completed_at 的绝对值计算；不读取当前时钟；
- 同一天或同一时刻不获得更高置信度，只表示时间可比。

## 13. 资源边界和安全错误

`SiteGbpAlignmentLimits` 默认上限，可下调不可上调：

- 完整对齐内部结果 20,000,000 bytes；
- eligible/unresolved/excluded 候选合计 5,000；
- 单字段规范化值每侧 5,000（GBP 原模型仍最多 50 个服务区域）；
- 两两比较总数 250,000；在构造笛卡尔积前检查；
- matched/unmatched/incomparable 引用合计 20,000；
- 新增 coverage Evidence 100；
- 合并后的 Evidence 20,000、trace 20,000；
- affected URLs 500；
- 最大来源完成时间差 30 天。

同时遵守 SiteBusinessLimits 和 PublicFindingsLimits。任何上限超出整体失败，不截断、不选择“最佳”候选、不删除诊断绕过上限。

安全错误沿用 V22_FINDINGS_ 前缀和既有 INPUT_INVALID、REFERENCE_INVALID、ID_CONFLICT、LIMIT_EXCEEDED，并为新来源边界增加 BINDING_INVALID、CHECKSUM_MISMATCH；所有错误使用固定消息。不把原始网页、电话、地址、URL 查询、底层异常或 Pydantic 序列化 warning 写入错误消息。

无网络、DNS、时钟、UUID、数据库和文件 IO；运行时电话/地址规范化只能使用锁定的本地数据。

## 14. 测试矩阵

后端新增测试至少覆盖：

1. business_identity 缺失的旧分支逐字节不变；身份 case/site/domain/market/GBP URL 错配和嵌套模型重验。
2. 唯一 site/public_gbp 来源、完整摘要、健康/身份/到期及 30 天时间边界。
3. 核心/业务核心/非核心页面资格，JSON declared/unresolved、标签资格及跨两个不同 final URL 重复印证。
4. 同一最终页面不同请求不增加印证；与 GBP 相同不能反向使候选合资格。
5. 多实体、多门店、多名称、多号码保留；任一合资格名称/地址/电话匹配即对齐，不选择第一项。
6. 名称安全标点、大小写、法定后缀；禁止子串、地点词删除和模糊近似。
7. 电话国家码、展示格式、分机、无效号码和锁定数据版本；禁止只比末尾数字。
8. 地址结构化组件、方向/道路缩写、邮编/unit 缺失和冲突；无法可靠解析时 not_checked，不做地理距离推断。
9. 服务区域集合完全相等、真子集/部分交集、无交集、重复值和不可拆营销短语。
10. storefront/service_area/hybrid 全矩阵；名称和电话始终 required。
11. site_missing、gbp_missing、both_missing、not_applicable；来源/解析/归属问题保持 not_checked。
12. 网站必要页面未采样、坏 JSON/HTML、不支持形态与只有 invalid value 的区别。
13. 缺失 coverage Evidence 的真实路径、状态标记、固定披露；其他规则不能借用。
14. 候选资格、规范化版本/键、匹配对、组件、Evidence、trace、RuleEvaluation、Finding 和 target 篡改。
15. ID 冲突、排序变化、跨进程稳定、输入不变和同一输入重复结果。
16. 每项可下调上限的精确边界和整体失败；无 IO、时钟或 UUID 分配。
17. 其他六项已实施 Finding、全部旧 rule evaluations、roll-up、旧导出样例和冻结合同保持不变。

## 15. 双端共享样例

新增独立目录：

- 后端输入：`tests/fixtures/report_v22_site_gbp_alignment/inputs`
- 后端生成：`tests/fixtures/report_v22_site_gbp_alignment/generated`
- 前端：`src/lib/report-v22/test-fixtures/site-gbp-alignment`
- 导出器：`scripts/export_v22_site_gbp_alignment_fixtures.py`

固定场景至少为：

- exact_match
- semantic_match
- multiple_candidates_one_match
- site_missing
- gbp_missing
- both_missing
- service_area_partial
- service_area_mismatch
- operating_model_not_applicable
- identity_unresolved
- source_unavailable
- comparison_time_gap

后端输入使用固定虚构 UUID、域名、时间和值，经真实 SiteBusiness、Evidence 和 PublicFindings 构建器生成。前端只接收 EvidenceItem、Finding、相关 RuleEvaluation 和必要 roll-up 片段，不接收 BusinessIdentity 原输入、HTML、内部候选/规范化键或伪造完整报告。

导出器必须全目标预检、拒绝额外文件/目录/符号链接、使用原子替换、不删除用户文件；`--check` 不修改字节/mtime、不创建缺失目录。前端目标失败时后端不能发生部分写入。

前端复用冻结 Schema `$defs` 验证 EvidenceItem、Finding、RuleEvaluation 所属内部样例形态及 LayerAssessment；验证哈希、ID 唯一性、证据引用、状态与场景计数，并加入错误 source_type、coverage 借用、字段/快照/ID 篡改反例。

## 16. 验收和未变更项

实施采用失败测试先行，顺序为：严格输入/身份绑定 → 候选资格 → 字段比较器 → 缺失证据 → RuleOutcome/引用复核 → 双端样例 → 完整回归。

验收执行新增定向测试、后端完整 pytest、前端合同及完整测试、非增量 TypeScript 检查、冻结类型生成无差异，以及合同、校验资源、旧证据、旧 Findings、公开 GBP、网站候选、新对齐样例的双端只读检查。双端执行 git diff --check 并审查所有冻结文件字节变化。

本轮只允许新增上述内部输入、选择/比较/对齐模块、四项规则的 opt-in 新分支、测试、样例和文档。冻结 ReportV22/API/JSON Schema/TypeScript 类型、v2.1、采集器、数据库、配置、worker/executor、前端运行时、UI/PDF、Dify 和 Actions 均不改变。

V22_ANALYZE_ENABLED、V22_PREFLIGHT_ENABLED、V22_COMPETITOR_DISCOVERY_ENABLED 继续默认 false；worker 继续使用 UnavailableV22Executor。没有真实网络联调、生产接线、推送或部署，不将其写成完成。

本轮完成口径是“用户确认实体范围内的网站与公开 GBP 四项离线对齐及 Findings 基础完成”。后续仍需八层语义映射、Actions、执行器接线、完整报告展示、真实供应商采集和端到端验收。
