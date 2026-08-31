# SearchTrust v2.2 客户公开 GBP 快照与证据接入设计

日期：2026-08-31

范围：V22-032 后续的客户公开 GBP 数据基础，不是四项对齐规则或完整八层评级。

状态：用户已确认范围、总体方案及本书面细则；批准范围已实施并通过本地回归，见同日 customer-public-gbp-implementation-plan 与 customer-public-gbp-completion。

主要仓库：SearchTrust-RD。search-trust 仅参与共享样例和冻结合同兼容性验证，不新增 UI。

## 1. 目标、交付口径与非目标

为已经确认的客户商家建立独立、可验证、可追踪的公开 GBP 内部快照，并接入 V22-031 证据索引。后续名称、地址、电话、服务区域对齐应读取这些来源，而不是把预检候选、竞品资料或第一方 Performance 指标当作客户公开资料。

本轮交付三部分：

1. 严格的客户公开 GBP 参考身份、采样输入和快照模型，以及离线快照构建函数。
2. 绑定、完整载荷摘要、身份和时效检查，以及实际字段/缺口的证据适配器。
3. 已有公开 Findings 构建器的兼容接入、准确的未检查原因、双端样例和回归测试。

本轮先完成离线数据接入边界：采样输入由可信调用方提供，测试使用固定虚构记录。不实现新的 HTTP 采集器或供应商原始 JSON 解析器，不发起实时查询，不扩充预检返回结构。真实采集、供应商字段适配、请求预算、重试和生产任务编排留待后续接入；不能将本轮完成称为线上客户 GBP 已能自动采集。

不实现四项业务对齐规则、网站联系信息抽取、八层语义评分、Actions、Dify 文案、完整报告组装、第一方数据/OAuth、数据库迁移、快照存储服务、清理任务、UI 或 PDF。

不改变冻结报告/API 模型、Schema、合同版本 2.2.0 和生成类型。不修改旧版行为，不将代码推送或部署，不开启生产分析、预检或竞品发现开关，不替换 UnavailableV22Executor。

## 2. 已核实的基础与方案取舍

- preflight_v22.gbp 的 GbpCandidate 是轻量候选，只含名称、网站、公开 URL、电话、地址等线索，不是带完整绑定、采样状态和字段来源的正式快照。
- competitors_v22.PublicGbpProfile 属于竞品；现有模型和字段清理白名单没有完整的电话/服务区域支持，不能原样充当客户资料输入。
- 冻结 EvidenceItem 允许 source_type=gbp；既有 prospect 示例已同时包含公开 GBP 证据和未连接的第一方 GBP Performance。因此无须修改冻结来源枚举。
- 当前内部 EvidenceSource 只有 site、serp、competitor、first_party；gbp 实际输入目前仅属于第一方 envelope。来源资格和 missing_sources 冲突检查需要识别新增公开分支，不能只比较 gbp 字符串。
- EvidenceBuildContext 尚未承载客户确认的 GBP 参考身份，必须补充可选内部上下文；不得用一份快照里的自我声明替代独立确认信息。
- 当前四条 GBP 检查记录固定为 customer_public_gbp_missing；加入真实来源后必须区分“没有资料”“资料不可用”和“比较规则尚未实现”。

采用独立客户公开快照和适配器，复用摘要、来源路径、稳定证据编号及安全校验。没有采用扩大预检职责的方案：虽然可以减少一个输入通道，但会把候选发现、确认和正式证据保留混在同一流程，也会增加对冻结预检合同的压力。本轮不泛化或重写竞品采集器。

## 3. 模块与数据流

拟新增模块：

- app/report_v22/public_gbp_models.py：参考身份、强标识、字段状态、采样输入、快照和本模块资源限制。
- app/report_v22/public_gbp_identity.py：参考身份一致性和实际返回实体的确定性身份检查；不联网解析短链或查询商家。
- app/report_v22/public_gbp_snapshot.py：纯函数 build_customer_public_gbp_snapshot，将严格采样输入转换为 CustomerPublicGbpSnapshot，不生成数据库 UUID。
- app/report_v22/public_gbp_errors.py：本模块固定、无载荷内容的错误。
- app/report_v22/evidence_adapters/public_gbp.py：将合资格快照内实际字段及缺口转换为 EvidenceObservation。

现有 evidence_models、evidence_bindings、evidence、findings_models、findings 只增加本轮所需的内部支持。来源专属判断放在独立模块，既有 site/serp/competitor/first_party 分支保持原义。

流程：可信调用方提供已确认参考身份和一次采样记录 → 严格构建公开快照 → 调用方提供真实快照绑定 → 证据构建器复核全部来源 → 输出业务证据、内部追踪和覆盖缺口 → Findings 仅更新未检查原因。

两个构建入口都不得访问网络、数据库、Redis、LLM 或运行时时钟；所有时间来自输入。不得修改输入对象，不使用进程 hash、随机 ID 或列表下标充当逻辑身份。

## 4. 独立确认的客户参考身份

新增 CustomerPublicGbpReference，放在 EvidenceBuildContext.customer_public_gbp 可选字段中；默认 None，旧输入继续合法。字段包括：

- case_id、site_url：必须与本次 EvidenceBuildContext 完全对应，网站 URL 使用既有 URL 模型；域名比较沿用小写 host 并移除单个 www.。
- public_gbp_url：已确认的公开 Google 商家 URL，最长 2083 字符，沿用现有 validate_gbp_url 的纯语法校验。不展开短链，不在此模块执行 DNS/HTTP。
- entity_keys：零到三项强标识，类型固定为 place_id、data_id、cid；同类型至多一个。值为非空、不带首尾空白的字符串，最长 480 字符；不大小写折叠，不猜测不同类型间的等价关系。
- confirmation_source：固定 user。此标签须由可信应用边界提供，不证明浏览器用户确实完成了确认。
- confirmed_at：带时区时间；有采样时满足第 8 节的完整时间顺序，仅提供参考时也不得晚于 evaluated_at。

entity_keys 可以为空，以保存“用户确认了 URL，但尚无可比强标识”的真实状态；这类来源不能仅凭 URL 文本、名称或电话被提升为 matched。

强标识条目固定为 kind/value 两个字段。参考、请求目标和返回记录中的标识集合均在验证后按 place_id、data_id、cid 顺序排列，保证集合遍历顺序不影响参考摘要；同类型重复直接拒绝，不后写覆盖。

新公开来源必须具有上述独立上下文。来源请求目标的公开 URL 和强标识集合必须与参考身份一致；快照保存 subject_reference_checksum，即经过模型验证的完整参考身份摘要。修改商家选择或确认信息后，旧快照不能继续冒用新参考身份。

已有 BusinessIdentity/AnalyzeRequest 不加字段。未来应用层负责把已确认身份转换为该内部参考；本轮不开发该生产接线或修改确认 UI。

本轮仅允许 prospect 使用新增客户公开来源和公开缺口声明。verified_execution 继续使用已有第一方输入；同时使用公开与第一方实际 GBP 快照的后续模式不在本轮交付内。

## 5. 快照与采样输入

快照 schema_version 固定为 customer_public_gbp_snapshot_v1，包含：

- subject_reference_checksum、request_target（参考公开 URL 及强标识集合）。
- started_at、completed_at、expires_at，均为带时区时间。
- provider 固定为内部标签 serpapi_public；它描述上游来源，不代表本模块执行了请求。
- request_record_id，沿用 req_ 加 16—80 位小写字母/数字的请求标识约束。
- response_checksum：上游原始响应字节摘要；成功采样必填，失败时可为空。它只作为追踪，不用规范化 JSON 重算后冒充原响应验证。
- collection_status：succeeded 或 failed；失败时带固定 failure_code（timeout、provider_unavailable、request_rejected、invalid_response），成功时该字段为空。
- record：包含 observed_entity_keys、observed_public_gbp_url 和 fields。observed_entity_keys 沿用第 4 节的类型、数量、长度和排序约束；observed_public_gbp_url 可为 None，存在时执行同样的 Google URL 语法及长度校验。fields 是第 6 节六个必需具名字段的对象，不允许任意字典键。
- identity_rule_version=customer_public_gbp_identity_v1、推导的 identity_match_status、固定 identity_reasons、推导的 health_status 和完整 limitations。

采样输入 CustomerPublicGbpSnapshotInput 明确包含 reference、request_target、started_at、completed_at、expires_at、provider、request_record_id、response_checksum、collection_status、failure_code、record、limitations、limits。不接收 snapshot_id、已计算摘要、身份或健康标签。除 failure_code/response_checksum 在上述允许情况下为空外，来源元数据必须显式提供；limitations 默认空，limits 默认本模块上限。

快照构建函数接受该模型或严格 dict，验证后计算参考摘要、身份状态和健康状态，返回 CustomerPublicGbpSnapshot；reference 和 limits 不放入返回载荷，载荷保留 subject_reference_checksum。构建时不读取时钟；completed_at 与 evaluated_at 的关系由后续证据绑定检查负责。

成功采样的 health_status 为 healthy，表示来源记录可解析，不表示字段齐全或与网站一致；字段不足单独记录。失败采样为 unavailable，observed_entity_keys 必须为空，observed_public_gbp_url 必须为空，六个字段均为 request_failed，不保留失败情况下猜测的商家资料。

failed 表示有一次实际尝试记录，不是“根本没有快照”；从未采样应使用缺口声明，不伪造请求 ID、响应摘要或快照 UUID。

不保存完整供应商 JSON、API key、请求头、令牌、评论者身份、评论正文、照片、帖子或任意额外字段。本轮不宣称规范化输入已经与原始供应商响应逐字段复核；真正的供应商适配器以后必须实现这一映射与审计。

## 6. 字段范围、状态和真实缺口

快照保留六个固定字段：business_name、website_url、address、phone、service_areas、service_area_business。后两者分别表示实际返回的区域列表和服务区域商家布尔标记；两者不能互相推导。

business_name 最长 240 字符，address 最长 500，phone 最长 120，website_url 为 HTTP(S) URL 且最长 2083。service_areas 最多 50 项、每项最长 240。观察到的文本必须非空白；保留其实际字符串，不在本轮做公司名、地址、电话或地区语义归一。

字段必须显式提供状态，不能靠默认空列表或 None 推测是否执行过采样：

每个具名字段采用 state/value 包装且两个字段都必填。文本/URL 字段的 value 是对应类型或 None，布尔字段为严格 bool 或 None，service_areas.value 为字符串列表；不得让模型自动补出“未返回”状态。

| 状态 | 值约束 | 证据含义 |
| --- | --- | --- |
| observed | 文本/URL 有实际值；区域列表至少一项；布尔可为 true 或 false | 实际返回的业务字段观察 |
| not_returned | 标量为 None，列表为空 | 成功返回中没有取得该字段，不证明商家不存在该信息 |
| returned_empty | 标量为 None，列表为空 | 上游明确返回空值/空列表；不等同于零服务范围 |
| unsupported | 标量为 None，列表为空 | 该采样通道未支持此字段，不能当作已检查 |
| request_failed | 标量为 None，列表为空 | 请求失败，没有有效字段结果 |

成功采样不能包含 request_failed；失败采样只允许 request_failed。类型严格，不把字符串 false/0 转为布尔或数字；拒绝 NaN、Infinity、未知字段及超长值，不截断后当作成功。

地址未返回时，不自动判断隐藏地址或服务区域经营模式；服务区域未返回时，不从客户目标市场、地址城市、网站文案或竞品区域补齐。service_area_business=false 只表示观察到了该布尔值，本轮不据此给四项规则判定“不适用”。

业务字段来源路径指向持有快照中的实际值；状态来源指向对应 state 字段。来源追踪证明能找回当前规范化快照的字段，不冒称能定位未保留的供应商原始响应字节。

## 7. 身份判定与请求绑定

必须区分输入完整性错误和实际观察到的身份不一致。

以下属于完整性错误，整个构建拒绝：参考 case/站点不属于本次上下文、subject_reference_checksum 不符、请求目标不是用户确认的目标、同类型强标识重复、载荷摘要或绑定元数据不一致。

对于请求目标正确、但实际返回可能不同的商家，身份规则固定为：

1. 对参考和返回强标识按相同类型比较。任一共同类型值冲突，判为 mismatch，不以另一个匹配标识覆盖冲突。
2. 没有冲突但没有任何共同且相等的强标识，判为 needs_confirmation；不同类型之间不自动换算。
3. 至少一个共同类型相等、其他共同类型均不冲突时，再检查实际 observed website_url：若域名与客户站点不同，判为 mismatch；相同则通过。未观察到网站 URL 可以依靠强标识通过，但披露缺少域名复核。
4. 满足第 3 步时为 matched。名称、地址、电话或服务区域的差异不参与身份门槛，留给后续对齐；不能先要求这些字段一致，才准许检查它们是否冲突。

失败采样身份为 needs_confirmation，identity_reasons 为 [lookup_failed]。成功采样按上述优先顺序选中一个分支：强标识冲突时为 [strong_id_conflict]，无可比强标识为 [no_comparable_strong_id]，匹配强标识但网站域名冲突为 [website_domain_conflict]。matched 时为 [strong_id_matched]，未取得网站字段则另外加入 website_not_observed。最终原因按字典序输出，不用自由文本控制业务真假。

reported public GBP URL 只作来源定位和审计。其与确认 URL 的字符串差异不自动视为不同实体，字符串相同也不替代强标识。只验证允许的 Google URL 语法，不在线消歧。

身份 mismatch/needs_confirmation 的来源只产生覆盖证据，不输出其名称、地址等为“客户业务事实”。它们不是四项对齐 Finding，也不升级或降低任何信任层。

本模块不能证明真实数据库归属、用户授权或供应商返回真实性。正式接入时由可信应用层负责选择确认记录、加载真实快照和验证租户权限。

## 8. 快照绑定、时效及规模

新增 PublicGbpEvidenceSource：kind=public_gbp，binding 仍使用 SnapshotBinding，binding.source_type=gbp，payload 为 CustomerPublicGbpSnapshot。

绑定必须满足：

- case_id 对应当前 case；snapshot_id 是调用方提供的真实快照标识，不能从 job、商家标识或时间推导，也不由本模块随机分配。
- schema_version 对应本轮快照版本；payload_checksum 对完整已验证快照计算，沿用现有 request_digest。
- fetched_at=payload.completed_at，expires_at 与载荷一致；健康和身份标签与载荷一致。
- 重算参考摘要和身份判断，检查载荷推导字段没有伪造；不能只相信 binding 上的 matched。
- confirmed_at <= started_at <= completed_at <= evaluated_at；expires_at > completed_at。

本轮公开快照必须显式提供 expires_at，且不晚于 completed_at 后 30 天，允许调用方设置更短窗口。该上限复用开发主计划已有的工程验收约束，不代表已核验全部第三方使用许可或数据保留义务。本轮不实现存储或删除；生产接入仍须独立处理保留政策、授权和清理任务。

当 expires_at <= evaluated_at 时视为过期，边界包含相等，不输出过期业务字段。来源资格判断顺序：过期 → 请求失败/不可用 → 身份 mismatch/needs_confirmation → 合资格。字段缺失不会使其他已观察字段自动失效。

一次证据构建最多一个不同 public_gbp 快照，无论是否过期；完全相同输入可去重。多份不同客户公开快照要求调用方明确选批次，不“取最后一项”。原全局 UUID 注册表仍防止同 UUID 对应不同来源或内容。

新增快照构建输入与输出各最多 1,000,000 字节，测试参数只允许下调；结合固定字段、列表及文本长度上限，超限整体失败、不截断。完整证据和 Findings 输出继续受既有 50,000 项证据/20 MB、10,000 条发现/20,000 项检查/20 MB 限制。

## 9. 公开 GBP 与第一方 GBP 的内部区分

冻结 EvidenceItem.source_type 继续使用 gbp；公开/第一方来源由内部 kind、快照版本、selector、trace 和来源说明明确区分，不靠 gbp 字符串判断用户是否授权。

新增公开观察选择器类别 public_gbp_field。既有 EvidenceSelector 不增加默认字段，保持旧选择器序列化内容及旧证据编号不变。新类别仅扩展内部枚举，旧的 metric 类别仍属于第一方指标。

MissingEvidenceSource、EvidenceSourceSummary、EvidenceCoverageGap 内部模型增加可选 gbp_origin，取 public_profile 或 first_party；非 gbp 来源只能为 None。对历史 gbp 缺口，省略/None 继续解释为 first_party，避免改变既有调用方的“未连接”含义。新增公开来源的摘要/缺口明确标为 public_profile。该内部元数据不得加入冻结 EvidenceItem 或报告根字段。

missing_sources 的冲突检查按 GBP 来源用途区分：

- public_gbp 实际快照与 gbp/public_profile 的“无快照”声明冲突。
- public_gbp 实际快照与历史 gbp/not_connected 的第一方缺口可以共存。
- 同一第一方来源既有快照又声明缺失仍拒绝。
- 无公开快照时，可声明 gbp_origin=public_profile、reason=no_snapshot 或 unavailable；not_connected 只用于第一方。

公开缺口不要求伪造参考身份或 snapshot_id。公开来源无快照时只生成内部缺口，不生成 Evidence。公开资料的存在不能修改 first_party_performance 的连接状态，也不能据此声称 Full Evidence Coverage。

本轮对内部摘要/缺口增加的可选字段是明确的内部接口变更；冻结合同、旧实际 Evidence/Finding 内容及编号不变。兼容测试不能只检查结构，还要检查历史 gbp 未连接语义和旧样例哈希。

## 10. 字段证据、追踪与编号

合资格公开来源的 observed 字段逐项产生 gbp Evidence：business_name、website_url、address、phone、service_area_business 各一项；service_areas 每个实际字符串一项。保留 false，不把非 observed 状态转换成 0、空字符串或 false。

合资格来源同时为实际返回的强标识、实际返回的公开 URL 生成审计观察，不能将请求参数里的商家标识冒充返回观察。参考/请求目标只参与绑定和 trace 上下文，不当作被采集的业务值。

SourceLocator.url 优先为实际返回的已验证 Google URL，未返回时为 None，不用确认 URL 冒充响应 URL；external_resource_id 使用确定性优先顺序 place_id、data_id、cid 下的已匹配标识及其类型。完整参考上下文保留在内部 selector.record_context，字段值不拼成 JSON 字符串。

选择器逻辑上下文至少包含 customer_public_gbp_v1 命名空间、subject_reference_checksum、返回实体强标识集合和字段名；标识按类型稳定排序，不依赖字段或数组遍历顺序。数组下标只出现在当前快照的物理来源路径中。

沿用 v22_evidence_identity_v1 和 ev_ 加 64 位 SHA-256，不整体升级旧算法。public_gbp_field 与 metric 的类别和上下文确保公开字段与第一方指标不会因同名而混为一条；全局 UUID 冲突仍提前拒绝。

origin_paths 对应第 5—6 节确定的布局：标量字段为 /payload/record/fields/<field>/value，区域项为 /payload/record/fields/service_areas/value/<index>，状态为 /payload/record/fields/<field>/state，返回强标识为 /payload/record/observed_entity_keys/<index>/value，返回公开 URL 为 /payload/record/observed_public_gbp_url。重复区域值可以产生同一 Evidence 并合并全部物理路径，但不算多份独立证据。

业务观察置信度为 medium；覆盖证据为 low。业务 normalized_value 本轮与已验证原始标量相同，不进行电话/地址/商家名/地区语义比较。所有公开观察带固定限制：These observations are from a public customer GBP profile, not authorized GBP Performance data. 完整来源限制保留在摘要。

合资格快照中，not_returned/unsupported 对应 partial 覆盖、returned_empty 对应 empty 覆盖。覆盖证据引用字段 state 的真实路径，original_value 为状态标量，normalized_value 为覆盖原因；不将“缺字段”写成业务缺陷。

整个来源过期、不可用或身份未通过时，只输出来源级覆盖证据，不暴露其业务字段；覆盖 trace 必须包含 customer_public_gbp_v1 命名空间，并能找回对应 binding/payload 状态。

## 11. Findings 兼容接入

build_public_findings 接受新增 public_gbp 来源，但仍拒绝 first_party 实际快照。来源数量限制按内部 kind 生效，不将 public_gbp 当作已有 first_party。

四条 GBP 规则继续不生成 Finding，所有检查状态仍为 not_checked，但原因准确区分：

- 无公开快照：保留 customer_public_gbp_missing。
- 有快照但来源不合资格：source_ineligible，完整身份/过期/不可用原因保留于 evidence_result 和检查限制。
- 来源合资格但对应字段没有实际 observed 值：field_not_observed，引用对应字段覆盖记录。
- 来源合资格且对应字段已有实际观察：新增内部原因 gbp_alignment_not_implemented，引用相关公开字段证据，明确本轮尚未比较网站。

存在来源时，RuleTarget.customer_gbp 保存其 snapshot_id；检查引用属于客户公开分支，comparator_ids 为空，不能借用竞品证据。缺来源时不编造快照编号。

六条既有实际规则的判断、版本、编号和上限不变。GBP 检查仍是未实现的覆盖记录，不宣称四条实判规则已交付。八层依旧全部 not_checked 且层级引用为空，站点/页面类型集群不因公开资料接入而新增评级或虚构页面。

旧输入没有新增来源时，既有 Evidence/Finding 和共享样例不应变化。新来源影响的是证据与 GBP 检查原因，不得改变已有页面、市场或竞品结论。

## 12. 错误与失败边界

独立快照构建入口新增固定错误：

- V22_PUBLIC_GBP_INPUT_INVALID：类型、状态、时间、字段或模式非法。
- V22_PUBLIC_GBP_REFERENCE_INVALID：客户参考与请求目标不一致，或输入中存在身份绑定歧义。
- V22_PUBLIC_GBP_LIMIT_EXCEEDED：快照输入或输出超过本模块上限。

通过证据构建入口加载已有快照时，继续使用现有 V22_EVIDENCE_SOURCE_INVALID、V22_EVIDENCE_SNAPSHOT_BINDING_INVALID、V22_EVIDENCE_CHECKSUM_MISMATCH、V22_EVIDENCE_ID_CONFLICT、V22_EVIDENCE_LIMIT_EXCEEDED；不把这些完整性错误吞成缺数据。Findings 的引用及资源错误也沿用已有前缀。独立身份辅助函数不得将新异常直接穿透旧证据边界，应在该边界按上述固定代码映射；URL 语法错误也须转成对应入口的安全错误。

正常字段不足、实际身份冲突、待确认或过期是有来源可追踪的覆盖状态，不是构建异常。伪造 matched、篡改已绑定载荷或把他人 case 的确认记录带入当前 case 才是完整性错误。

公开错误代码和说明不得回显商家名称、电话、地址、URL、查询、供应商载荷或密钥。失败路径不返回部分成功索引，不输出未经身份门槛的客户业务字段。

## 13. 共享样例与验收

后端新增 tests/fixtures/report_v22_public_gbp/inputs 与 generated 目录，以及 scripts/export_v22_public_gbp_fixtures.py。固定四组虚构场景：

1. matched：强标识与域名匹配，包含名称/地址/电话/区域及 false 布尔值，验证真实值、稳定编号和公开来源说明。
2. partial：身份已匹配，但部分字段 not_returned/returned_empty/unsupported；其他已观察字段仍可进入证据。
3. identity_conflict：有实际返回且强标识冲突，只输出覆盖；不能产生客户业务值。
4. expired：身份和字段本来有效，但到期边界已达到；只输出过期覆盖。

无快照、请求失败、缺少共同强标识、域名冲突、伪造状态、混入竞品、缺口用途冲突等另由后端参数化测试覆盖。

导出器输出实际构建器的 EvidenceItem、空 GBP Findings 与 LayerAssessment 片段、manifest，不伪造完整报告。前端接收生成片段，不复制客户参考或原始采样输入；继续使用冻结 Schema 的完整 $defs 验证 EvidenceItem/Finding/LayerAssessment，核对引用、哈希和非法字段反例。

导出器只操作预定文件，支持 --frontend-dir 和只读 --check，全部目录先预检，拒绝额外文件、越界和符号链接；不清空目录，不覆盖无关用户文件。

最低后端测试范围：

- 严格类型、额外字段、布尔 false、实际空与未返回、字段/列表/字节上限。
- case、站点、参考摘要、请求目标、确认时刻及快照绑定；重算身份标签，不能凭 matched 字符串放行。
- 同类型强标识相等/冲突、共同类型缺失、多标识中一项冲突优先、短链不能独立确认、网站域名冲突或未返回。
- 名称/电话/地址不同但强身份匹配时仍允许观察进入证据，防止身份门槛掩盖未来对齐冲突。
- 恰好到期、尚未到期、显式期限不超过 30 天、非法时间顺序、失败采样不带资料。
- 客户公开来源与第一方未连接缺口共存；公开来源与公开缺口矛盾拒绝；旧第一方实际来源与缺口矛盾仍拒绝。
- prospect 新公开分支合法、实际第一方仍非法；本轮 verified_execution 不接受新公开分支；旧 verified 第一方流程不变。
- 同 UUID 不同内容、多份不同公开快照、输入不变、来源/观察遍历顺序、跨进程编号、重复值合并路径及完整来源追踪。
- 四条 GBP 检查原因随资料资格变化但永不产生 Finding；六条旧规则输出不变、八层仍未检查。
- 无快照不造 Evidence/UUID，身份失败和过期不输出业务值，安全错误不泄露输入。
- 旧 V22-031 / V22-032 输入、Evidence/Finding 内容与编号、冻结模型/API/Schema/类型、v2.1 回归不变。

实施后应运行新增定向测试、后端完整 pytest、前端合同/完整测试及非增量类型检查、所有旧导出器与新导出器的双端只读漂移检查、差异审查。本节为设计阶段制定的验收要求；实际结果见同日 customer-public-gbp-completion。

## 14. 交付后仍需完成

- 客户公开 GBP 真实采集适配器、供应商字段映射、预算/重试、生产身份确认接线和真实快照存储。
- 生产数据保留与清理、租户授权及外部服务使用条件核验。
- 客户网站的名称、地址、电话、服务区域可追踪事实抽取，以及四项实质对齐规则。
- 八层完整语义判断、三项行动、文案及完整报告交付。

本轮完成口径仅为“客户公开 GBP 快照与证据离线接入基础完成”。不得把它标为完整 V22-032、完整 v2.2 或线上客户 GBP 分析已经可用。
