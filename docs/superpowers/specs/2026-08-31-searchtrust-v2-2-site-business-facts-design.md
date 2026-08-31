# SearchTrust v2.2 网站业务信息提取设计

日期：2026-08-31

状态：用户已批准本轮范围、多值处理原则、独立模块总体方案及本书面细则；本地实施与回归完成，见同日 site-business-facts-completion。未推送、未部署，未接入生产流程。

范围：V22-032 后续的网站业务候选与原文证据基础；不是网站与 GBP 的实质对齐、客户身份确认或八层评级。

主要仓库：SearchTrust-RD。search-trust 仅验证新共享证据样例，不修改产品页面。

## 1. 已确认目标与非目标

从客户网站已经保存的网页快照中提取商家名称、地址、电话和服务区域。每项候选保留来源快照、页面和可复核的原文位置；明确区分未观察到、缺少可用内容、解析失败和归属不明确。

多个名称、门店地址、号码或服务城市不自动表示冲突。本轮不选择主门店、主名称、主号码，不按照来源优先级丢弃其他有效候选，也不把不同页面或不同结构化实体自动合并为同一商家。

不新增抓取、浏览器渲染、外部请求、地理编码、号码查询、LLM 提取、数据库记录或随机 UUID。不使用客户确认信息、GBP、竞品、预检信号、页面标题或搜索词补全业务值。不实现冲突判定、GBP 对齐 Finding、Actions、八层语义规则、完整报告组装、UI/PDF 或生产接线。

“未观察到”仅表示在这批可用快照和本版本支持的提取规则内未取得候选，不代表网站或商家没有该信息。

## 2. 代码现状与方案选择

- `app/collectors/site_inventory_models.py` 的 DeepPageSnapshot 已保存 HTML、文本、实际响应 URL、状态、采样时刻和上游内容摘要；普通库存记录只有结构信息，不能据此假装取得正文。
- `app/report_v22/evidence_adapters/site.py` 已生成结构证据和有限文本片段。片段可能被截短，不能反向当作完整页面输入。
- `app/report_v21/page_facts.py` 已有候选、名称/电话优先级筛选及地址/区域处理，但这些最终筛选规则不等同于本轮“保留多候选”的要求。
- 客户公开 GBP 离线快照基础已完成；四项 GBP 对齐检查仍为未检查。

比较过两条路径：封装旧提取器能复用识别能力，但需要分离旧选择及比较语义；独立 v2.2 模块需要新增规则和测试，但范围与输出可单独固定。用户已选择独立模块。

可以参考旧测试中的失败案例，不调用旧 `build_page_facts` 的最终筛选结果，不修改 v2.1 文件，不顺带迁移旧规则。

## 3. 架构与入口

新增纯入口 `build_site_business_facts(SiteBusinessFactsInput | dict) -> SiteBusinessFactsResult`。模块职责划分如下：

- `site_business_models.py`：严格输入、候选、位置、页面字段状态和结果模型。
- `site_business_bindings.py`：客户来源、时间、摘要及页面归属预检。
- `site_business_html.py`：有界 HTML 解析、元素位置、显式标签和值的关联；不执行页面内容。
- `site_business_jsonld.py`：有界结构化记录与属性提取；不解析远程上下文或追踪引用。
- `site_business_facts.py`：组合候选、状态、确定性编号及完整结果限额。
- `site_business_evidence.py`：把候选标量及其可复核位置转换为本轮独立证据索引。
- `site_business_errors.py`：固定、安全、无载荷回显的异常。

流水顺序为：重验输入与绑定 → 判断来源和页面是否可提取 → 分别提取结构化字段与显式页面字段 → 保留候选及归属线索 → 构建证据 → 复核所有引用和大小 → 一次性返回结果。

新入口返回本轮新增的候选证据，不修改或隐式扩展 `build_evidence_index` 的默认结果；不修改其 site/competitor 适配器，不让 `build_public_findings` 自动消费新候选。后续对齐模块必须显式接入并定义主体选择和比较规则。

## 4. 严格输入与来源绑定

SiteBusinessFactsInput 包含：

- context：独立的 SiteBusinessFactsContext，仅有 case_id（UUID）、report_type（本轮固定 prospect）、site_url（HTTP(S) URL，最多 2083 字符）、evaluated_at（有时区时间）。不接收 GBP/竞品/确认名称等补值参数。
- source：必须显式提供，类型为现有 SiteEvidenceSource 或 None。只接受一个客户 site 来源，不接收来源列表、候选数组或人工拼接正文。
- limits：第 11 节规定的可下调资源限制。

所有新增模型拒绝额外字段和隐式类型转换；观察文字保留大小写和首尾空格，不能因通用模型默认去空格而静默改值。非有限数字和非法嵌套模型在入口统一拒绝。无来源时只返回 no_snapshot 状态，不分配 UUID、不构造 Evidence。

对于实际来源，在任何解析器运行前完成：

1. 重验 SiteEvidenceSource 及其嵌套模型，不信任 model_copy/update 跳过校验的对象。
2. binding.case_id 对应 context；kind=site、source_type=site；快照版本与载荷一致。
3. 完整载荷的 request_digest 与 binding.payload_checksum 一致；不能以 HTML 摘要替代整个快照校验。
4. root_url 与 context.site_url 完整 URL 一致；canonical_host 使用现有精确主域名规则核对，不进行子域名包含匹配。
5. binding.fetched_at=payload.completed_at，started_at <= completed_at <= evaluated_at；存在 expires_at 时必须晚于 fetched_at。
6. selected page 必须对应当前库存中的实际页面。deep.url 与该请求 URL 一致，深度记录的 page_type/crawl_depth 与对应 selected record 一致；deep.collected_at 位于本次库存 started_at 与 completed_at 之间。
7. 请求页面必须属于客户站点允许的主机集合。沿用 SiteScope 的根主机及 www 对应主机边界，不把任意子域名自动视为客户页面；URL 只做本地语法检查，不进行 DNS 或跳转请求。

上游 content_checksum 是原始响应字节摘要；本模块没有原始响应字节，不宣称能够从解码后 HTML 重算它。另算 decoded_html_checksum=request_digest({html: 已保存 HTML})，仅用于本轮定位和复核。

实际来源过期或不健康只产生来源级诊断，不解析其业务字段。依次判断：expires_at<=evaluated_at 或 health_status=expired 为 source_expired；健康状态不在 healthy/not_checked 中为 source_unavailable；明确 identity_match_status=mismatch 为 source_identity_mismatch；其余继续。绑定的健康/身份标签由可信调用方提供，本模块不能证明数据库内的真实授权或来源标签真实性，not_checked 也不代表商家主体已被验证。

## 5. 页面资格和读取边界

来源合资格时，结果保留库存内每个请求页面的记录，以区分未选入深度采样与采样后未找到字段；来源缺失或失效时按第 8 节只输出来源状态。只解析同时满足下列条件的深度页面：

- 库存结构检查 checked，实际深度快照存在；
- 深度响应为 2xx，媒体类型为 text/html 或 application/xhtml+xml，忽略类型参数和大小写；
- 深度 final_url 属于客户允许主机且 URL 语法安全；
- HTML 含有非空内容，来源未过期或失效。

使用深度响应的 final_url 作为 Evidence 定位 URL，同时保留库存中的 requested_url；不能把抓取请求 URL 冒充实际返回地址。真实的站外跳转记为 out_of_scope_redirect，不采纳跳转后页面的商家信息。

没有深度快照、非 HTML、错误响应或空 HTML 均记为未检查及具体原因，不能用 title/h1/schema_types 等结构元数据补成业务值。即使 text 有内容但 HTML 为空，本轮也只记 html_missing；text 可能经过压缩、截断或转换，不作为失去 DOM 结构后的推断兜底。

同一最终页面从多个请求 URL 到达时保留各自来源，不因 final_url 相同而把不同响应内容和时刻混为一条。不同页面提取相同值也不等于多份独立验证。

## 6. 第一版支持的提取规则

规则版本固定 `site_business_extraction_v1`。以下是本产品首版支持范围，不声称覆盖所有网站标记或行业。

### 6.1 结构化商家记录

读取实际 HTML 中 type 为 application/ld+json 的 script 内容。接受顶层对象、顶层对象数组以及这些对象内 @graph 的对象数组；不递归把任意 author/review/publisher 等子对象提升为独立商家。

商家 @type 白名单为 LocalBusiness、Organization、Plumber、HomeAndConstructionBusiness、ProfessionalService、Electrician、HVACBusiness、RoofingContractor、GeneralContractor、Locksmith、MovingCompany。接受这些精确短名及 schema.org 的 HTTP(S) 完整类型 URL；不通过类型后缀猜行业，不在线查询继承关系。单值类型或类型字符串数组中有白名单项即可。

只按实际键读取，不做 JSON-LD alias 展开。支持 @context 缺省、schema.org HTTP(S) 字符串及仅含这些字符串的数组；其他上下文形态记 unsupported_context，该块不产生结构化候选。@id 仅作记录线索，不通过它联网或跨块加载缺失字段。重复 JSON 对象键、NaN/Infinity、无效 JSON 均拒绝该块，记录诊断，不能采用“最后一个键胜出”。

| 候选字段 | 允许的实际属性 | 值和边界 |
| --- | --- | --- |
| business_name | 商家记录直属 name | 非空白字符串，最多 240 字符 |
| phone | 商家直属 telephone；其直属 contactPoint 对象/数组中的 telephone | 字符串或字符串数组，每项最多 120 字符，含至少一个数字；不补国家码、不去格式、不判断主号码 |
| address | 商家直属 address | 非空白字符串最多 500 字符，或下述有限地址组件对象；不把数字强转为地址 |
| service_area | 商家直属 areaServed 或 serviceArea | 字符串、含 name 的对象或这些值的数组，每项名称最多 240 字符；不根据城市名单扩张或推断覆盖范围 |

地址对象只保留 streetAddress、addressLocality、addressRegion、postalCode、addressCountry 的实际字符串组件；addressCountry 对象仅允许取其字符串 name。streetAddress 最多 500 字符，其他组件各最多 240 字符。未知属性不采纳；只有 @id、没有支持组件的对象不能生成地址。字符串数组或地址对象数组逐项保留，不猜测主地址。同一地址对象有部分组件无效时保留其他有效组件并记诊断；不能把两个地址对象的组件交叉拼成一个地址。

结构化地址不合成为伪原文地址：候选的 scalar_value 为 None，components 保存实际组件；每个组件分别生成标量证据。地址字符串候选的 components 为空。两种形式互斥，不能用缺失组件补全另一种。

属性缺省、null 或空数组均不生成候选，本身不算解析失败；纯空白字符串、错误类型或超长值记 invalid_field_value/unsupported_field_shape。节点有有效候选时同时保留其他值的诊断，不能因其中一项无效而删除有效项。service_area 仅保留名称原文，不宣称一个短语就是已解析的城市实体。

### 6.2 明确页面字段

新增独立的有界 DOM 提取，不调用旧优先级筛选。只支持：

- 电话链接：a 元素 href 以 tel: 开头，保留冒号后的实际字符串和原属性定位。不得从普通 URL、脚本或任意数字串猜电话号码。显示文字与 href 不同不构成冲突，也不自动替代 href。
- 地址元素：address 元素中可见文本组成一个候选；它可能是文章作者或其他机构地址，归属默认 unresolved，不能自动当成客户门店。
- 明确标签：同一 dl 中 dt 与紧随的 dd、同一 tr 内的标签单元格与紧随值单元格，以及 h1—h6 标签标题与同一父元素下紧随的 p/div/address/ul/ol 值元素。
- 标签必须完整匹配白名单，比较标签时允许去首尾空白、合并空白、去掉末尾一个中英文冒号并忽略英文大小写。不对业务值使用标签清洗规则。

标签白名单：名称为 Business name、Company name、商家名称、公司名称；电话为 Phone、Telephone、Tel、电话、联系电话；地址为 Address、Business address、地址、营业地址；服务区域为 Areas served、Service areas、We serve、服务区域、服务范围。

名称、电话、地址的值节点作为一个候选，值类型与长度限制沿用第 6.1 节；电话同样必须含数字。服务区域 ul/ol 按直接 li 子节点逐项提取；普通文本作为一项原文短语保留，不按逗号、and、斜杠或连字符猜拆城市。遇到只有标签、空值、嵌套归属无法确定或不支持形态时记对应字段的 invalid_field_value/unsupported_field_shape，不从邻近无关段落补值。

候选文本排除 script/style/noscript/template/svg 内容、hidden 元素及 aria-hidden=true 子树。只进行 HTML 实体解码、按 DOM 文档顺序拼接文本节点和 br 换行；不执行 CSS 或 JavaScript。因此“可见文本”只指本地标记规则下未显式隐藏的内容，不代表已验证实际渲染可见。

不实现全正文正则兜底、标题品牌推断、logo alt 品牌选择、版权名称推断、Microdata/RDFa 扩展或语言模型补全。其他语言、其他标记和未支持形态通过限制说明披露，后续显式扩展规则版本。

## 7. 候选模型和主体归属

SiteBusinessCandidate 包含 candidate_id、field、snapshot_id、requested_url、final_url、collected_at、source_kind、entity_key、ownership_status、declared_id/declared_url、scalar_value/components、evidence_ids、origins 和 limitations。

- candidate_id 使用 `sf_` 加 64 位 SHA-256，不是快照 UUID。
- field 固定 business_name/address/phone/service_area；source_kind 固定 jsonld_property/tel_link/dom_address/dom_label。
- ownership_status 只有 declared_entity/unresolved。declared_entity 表示字段属于页面中的某个明确结构化记录，不表示它就是已确认客户或主门店。
- JSON-LD 候选的 declared_id/declared_url 保留其记录的原始 @id/url 字符串线索，缺省为 None，每项最多 2083 字符；错误类型或超长线索不复制到候选，记 invalid_field_value 并将归属设为 unresolved。entity_key 由请求/最终页面 URL、规范化 JSON 记录内容摘要构成，不按名字、电话、地址合并实体。没有 @id 也可在当前块内保留独立实体组。
- 仅为判断站外线索，将相对 URL/片段在内存中相对 final_url 解析；不请求解析结果。有效 HTTP(S) 线索的主机不属于客户集合时标记 unresolved 并披露 external_entity_hint；其他无法解释的 URI 线索也设 unresolved。没有线索不阻止在当前 JSON 记录内关联字段。站内 URL 不证明客户主体身份，未通过任何 GBP 对照选择。
- DOM 候选的 ownership_status 默认 unresolved，entity_key=None。不把附近结构化记录隐式分配给所有页脚、电话链接或地址元素。

同一字段可以同时有多个 declared_entity 和 unresolved 候选；没有 primary 标记、选中值、客户一致性标签或业务 conflict 状态。无效值不进入候选索引，其错误类别与来源位置保留在诊断中。

## 8. 状态、缺口与错误

每页、每字段分别输出两个维度：observation_status=observed/not_observed/not_checked；ownership_status=declared_entity/unresolved/mixed/not_applicable。

- 有至少一个有效候选为 observed，即使另有解析失败或无效值，也必须同时保留这些诊断。
- 页面可解析、相关支持通道处理完成且没有候选，才为 not_observed。
- 内容不可用，或相关通道失败导致无法完整检查且没有候选，为 not_checked。
- ownership_status 由现有候选归属集合推导；无候选为 not_applicable。有多个 declared_entity 仍可标 declared_entity，但保留各 entity_key，不等于主体唯一。

页面级处理状态为 parsed/partial/not_checked/parse_failed。parsed 表示本版本支持的提取通道处理完毕，不代表全网站覆盖；partial 表示有局部块解析失败或遇到相关不支持/无效字段；parse_failed 表示页面解析失败且无可靠通道可用。无效 JSON-LD 块不能使另一有效块或有效电话链接消失，但会使没有候选的相关字段保持 not_checked。

固定诊断为 no_snapshot、source_expired、source_unavailable、source_identity_mismatch、page_not_checked、deep_snapshot_missing、http_error、content_unsupported、out_of_scope_redirect、html_missing、html_parse_failed、jsonld_parse_failed、duplicate_json_key、unsupported_context、unsupported_field_shape、invalid_field_value、external_entity_hint、ownership_unresolved。每条诊断包含 code、受影响字段集合及可取得的页面/块位置，不包含自由生成的原因。未支持字段形态或无效业务值按对应字段标为不完整；块级 JSON/上下文错误影响四个字段，DOM 整体错误影响全部 DOM 通道。归属线索错误影响 ownership，不把已观察到的值变成未观察到；page_not_checked 保留库存原 check_status/error_code，不统一伪装成 HTTP 错误。

来源/页面缺口只保留内部状态，不为“缺内容”虚构业务 Evidence 或把诊断文字当作页面原文。无来源时 pages、candidates、evidence_index、source_traces 均为空；来源失效时不解析页面，pages 为空，来源状态明确失效。

结构化输入、绑定、校验和、引用或资源上限错误必须整体失败，不返回部分候选。新增安全错误为 V22_SITE_FACTS_INPUT_INVALID、BINDING_INVALID、CHECKSUM_MISMATCH、REFERENCE_INVALID、ID_CONFLICT、LIMIT_EXCEEDED，均使用同一完整 V22_SITE_FACTS_ 前缀。不回显 HTML、地址、号码、URL 参数或底层解析异常正文；非法已构造模型的序列化警告也不得泄露载荷。

网页本身局部格式错误属于诊断；解析器资源耗尽属于 LIMIT_EXCEEDED，不能吞掉并伪装成“未找到”。

## 9. 原文位置与证据复核

每个 origin 指向当前绑定 source 中实际的 `/payload/selected_pages/<index>/deep_snapshot/html`，并包含 decoded_html_checksum、半开字符区间 [start,end)、来源方式及结构定位。字符下标以已保存 Python 字符串为准，不冒充 UTF-8 字节偏移。

- JSON-LD：字符区间定位 script 原内容，另存从该 JSON 根开始的 JSON Pointer，精确到字符串值或地址组件。复核时解析这段相同 JSON 并解析 Pointer，要求所得标量与候选/证据相同。
- DOM：区间定位原元素片段，另存确定性元素路径；电话注明 href 属性，文本注明本版本的文本节点拼接规则。复核同一原片段与定位，必须得到相同值。
- 每个 origin 的 excerpt 为原区间起始最多 360 字符的实际切片，超过时 excerpt_truncated=true。摘要仅用于展示，不代替区间和结构定位，不截断候选标量或地址组件。
- origin 的 JSON Pointer 不能指到另一页面；字符区间必须在源 HTML 内；结构定位不能跨区间借值。必须复核候选、证据、页面、时间、source snapshot 和对应字段的完整关联。

不复制整个 HTML 或脚本到每个候选。重复观察保留全部唯一 origin；每个输入页面的响应摘要、解码 HTML 摘要和采样时刻留在页面记录。

## 10. 独立结果、证据编号和兼容

SiteBusinessFactsResult 包含 schema_version=site_business_facts_v1、extraction_version、case_id、site_url、evaluated_at、source_snapshot_id/source_payload_checksum（无来源时均为 None）、source_status、pages、candidates、evidence_index、source_traces、limitations。

source_status 包含 state=ready/missing/ineligible 和 reason；ready 的 reason 为 None，missing 固定 no_snapshot，ineligible 使用第 4 节三种来源失效原因。pages 按 requested_url 排序，内含实际库存/深度处理状态、采样元数据、四个字段状态及诊断；无深度内容时深度元数据为 None。候选和证据分别按 ID 排序，诊断及 origins 按规范化内容稳定排序去重，不依赖遍历次序。

evidence_index 只包含从本轮有效候选实际构建出的冻结 EvidenceItem，source_type=site、confidence=low、health_status=healthy。该置信度表达候选仍未被确认属于客户主实体。scalar_value 的证据 original_value=normalized_value=解码后的实际字符串；结构化地址每个组件一条证据，不把对象序列化成 JSON 字符串塞入标量，也不合成缺失字段。

每条证据固定披露：These values are declarations found in saved customer-site pages; they do not establish the confirmed customer entity, a primary location, or GBP alignment. 另披露本轮是规则受限的离线提取且没有验证实际渲染可见性。固定声明在 20 项限制说明上限内必须保留，完整说明保留在结果及候选内。

EvidenceSelector 仅新增内部类别 site_business_field，不加新的默认字段。其 record_context 包含 site_business_extraction_v1 命名空间、requested_url、final_url、decoded_html_checksum、entity_key、source_kind、field、value_group_digest、component 名（非地址组件为 None）。value_group_digest 对完整 scalar_value/components 计算，确保同一记录中不同地址的组件不会被交叉借用。physical page index、DOM 位置或 JSON 数组下标只放来源追踪，不作为跨页面选择主值的依据。

沿用 v22_evidence_identity_v1 和 ev_ 加 64 位 SHA-256；candidate_id 用固定版本、snapshot_id、同一逻辑上下文（component 为 None）及完整候选值/组件摘要计算。规范化 JSON 编码只服务于摘要稳定性，不改变原文值。同一快照内相同逻辑观察去重并合并 origins；同 ID 对应不同内容整体失败。跨页面或不同内容的结构化记录不按字段值合并；同一页面完全相同的结构化记录可以合并重复观察，必须保留全部出现位置，不能据此宣称实际实体唯一。

source_traces 使用已有 EvidenceSourceTrace 结构，origin_paths 指向真实源 HTML；更精确区间/Pointer/组件归属由候选 origins 保留。Evidence 的 source_locator.url 为实际 final_url，field_path 使用 selector 摘要。候选的每个 evidence_id 必须在本结果索引中存在并属于同一快照、页面、字段/组件；证据、追踪、候选之间不允许悬空或借用。

本轮不新增 EvidenceSource kind 或快照 schema，不生成新的持久化快照。新入口不返回 Finding 或 LayerAssessment，也不改变旧四项 GBP 未检查原因。已有旧证据、Findings、公开 GBP 样例、冻结报告/API/Schema/TypeScript 类型必须保持字节内容及编号不变。

## 11. 有界处理

以下为首版工程上限，不是完整网站覆盖承诺。允许测试/调用方下调，不允许上调：

- 完整输入和完整输出各 20,000,000 字节；输入包含原快照全部字段，输出包含诊断、候选、追踪和证据。
- 库存页面最多 500、深度页面最多 50，沿用现有载荷上限；不偷偷扩大发现或深度采样范围。
- 参与解析的单页 HTML 最多 1,000,000 UTF-8 字节，总量最多 10,000,000 字节。
- HTML 元素深度最多 64，单页节点最多 20,000，全批最多 200,000。
- 单页 JSON-LD 块最多 32，单块最多 65,536 UTF-8 字节，JSON 嵌套深度最多 32，单页解析节点总量最多 10,000。
- 单个字段的重复值/对象数组最多 50 项；单页有效候选最多 250，全批最多 5,000；唯一候选证据最多 20,000；完整 origin 总数最多 20,000。

候选字段超过文本上限是 invalid_field_value 诊断，不截短后采纳。页面/块/结构/数组规模或结果超限则整体 LIMIT_EXCEEDED。预算必须在解析或保留下一批内容前检查，不能在无界构建结束后才检测。不能通过删除候选、丢弃诊断或择优选择来源绕过限额。

## 12. 测试与共享样例

新增测试至少覆盖：

1. 严格类型、额外字段、无时区/未来时间、非法复制模型、安全错误及无序列化载荷警告。
2. case/site/schema/checksum/时刻错配；跨页面 HTML 引用、站外深度跳转、非法健康枚举及到期后不能凭 healthy 放行。
3. 有效结构化记录、对象/数组/@graph、多实体、仅 @id 引用、不支持 context、重复键、损坏 JSON、嵌套上限。
4. 电话链接、地址元素、标签表格/定义列表/标题块、服务区列表；脚本/隐藏区域排除及不支持 CSS 渲染的明确说明。
5. 多名称、多号码、多门店、跟踪号码、多个服务城市不自动选主值或判冲突；GBP、竞品、title/h1 不可补值。
6. 地址字符串与组件证据、JSON 转义、HTML 实体、首尾空格、Unicode 字符偏移及 excerpt 截短标记；原值不得静默清洗。
7. 真正未观察到与缺 HTML/深度快照/部分解析失败分别表达；一块损坏不能抹掉另一块有效观察。
8. 来源/页面/字段/组件引用篡改，候选与 Evidence/trace 编号冲突，重复来源合并、不同响应及不同页面隔离。
9. 输入不变；库存/selected_pages 排序改变后，重封合法绑定，原候选和证据 ID 不变，仅物理路径随输入位置变化；跨进程编号稳定。HTML 或实体记录内容变化不承诺维持原 ID。
10. 每项可下调上限的边界值；超限整体失败；没有网络、时钟和 UUID 分配。
11. 原构建入口在已有全部输入上的 Evidence/Finding/GBP 输出、限制说明及编号保持不变。

新增独立样例目录 `tests/fixtures/report_v22_site_business/inputs` 和 `generated`，导出器 `scripts/export_v22_site_business_fixtures.py`；前端目标为 `src/lib/report-v22/test-fixtures/site-business`。固定场景为 structured_single、multiple_candidates、partial_parse、no_content、expired，使用固定虚构 UUID/域名/时间。

后端测试验证完整候选及诊断语义。前端仅接收真实新入口生成的 EvidenceItem 数组及字节哈希 manifest，复用冻结定义做结构、类型、数量与反例检查；不接收原始 HTML、内部候选模型或伪造完整报告。没有 Evidence 的样例允许空数组，不制造占位值。

新导出器沿用全目标预检、预定文件集合、拒绝符号链接和额外文件、原子更新及只读漂移检查要求；不删除用户无关文件。所有旧导出器继续通过双端只读检查，不重新生成旧样例来掩盖变化。

## 13. 实施验收与未变更项

书面设计获确认后再写详细实施计划。实施采用失败测试先行，验证输入绑定与资源边界，再做结构化/DOM 提取、原文定位与候选证据、导出兼容和全量回归；本轮不启动子代理。

验收运行新增定向测试、后端完整 pytest、前端合同与完整测试、非增量 TypeScript 检查、冻结合同/校验资源/旧证据/旧 Findings/公开 GBP/新网站候选样例全部双端只读检查，以及 Git 差异审查。当前文件不宣称这些新增测试已执行或通过。

仅允许新增上述离线模块、内部 selector 类别、测试及独立样例/文档。冻结模型/API/Schema/类型、v2.1、旧采集器、旧证据/Findings 入口行为、数据库、配置、worker/executor、前端运行时、UI/PDF 均不改变；没有推送、部署或开启生产分析开关。

本轮完成口径为“网站业务候选提取及原文证据基础完成”。后续仍须定义客户实体/门店选择、电话与地址比较语义、服务区域关系以及四项实质 GBP 对齐；多值本身不是发现或评级。
