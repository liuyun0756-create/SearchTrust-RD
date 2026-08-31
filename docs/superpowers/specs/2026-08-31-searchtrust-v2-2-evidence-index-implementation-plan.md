# SearchTrust v2.2 证据索引实施计划

日期：2026-08-31

范围：V22-031

依据：已于 2026-08-31 获用户批准的 `2026-08-30-searchtrust-v2-2-evidence-index-design.md`。

状态：2026-08-31 已完成以下 10 项任务及本地验证；详见同日 `2026-08-31-searchtrust-v2-2-evidence-index-completion.md`。未推送、未部署、未启用生产分析。

writing-plans 技能在当前环境不可用，本计划按已批准设计手工编写，未改变功能范围。按依赖顺序在当前任务内执行，不启动并行子代理。

## 1. 交付边界与执行方法

后端仓库为 `SearchTrust-RD`，前端仓库为同级 `search-trust`。本轮交付离线证据构建器、来源适配器和共享证据样例；不交付快照写入服务、数据库迁移、生产 executor 或完整报告。

开发顺序：内部模型与失败样例 → 稳定身份与来源绑定 → 各来源适配器 → 合并构建器 → 双端兼容性与完整回归。

每项任务先补最小失败测试，确认失败来自目标行为尚未实现，再实现对应逻辑并运行定向回归。共享样例的预期不能只由待测实现自证，必须同时检查明确的值、来源、编号规则和缺口类型。不得通过修改冻结合同或放宽既有校验消除失败。

计划编写时已复验：后端完整 pytest 715 项通过；前端合同相关测试 159 项通过；前端非增量类型检查、两端冻结合同及 V22-030 辅助资源一致性检查通过。这些是开发前基线，不是本功能的验收结果。

## 2. 任务 1：内部模型、测试输入及安全错误

新增文件：

- `app/report_v22/evidence_models.py`
- `app/report_v22/evidence_errors.py`
- `tests/test_v22_evidence_models.py`
- `tests/fixtures/report_v22_evidence/inputs/`

实施步骤：

1. 定义严格内部类型：`EvidenceBuildContext`、`SnapshotBinding`、按来源区分的载荷封装、`MissingEvidenceSource`、`EvidenceObservation`、`EvidenceSourceTrace`、`EvidenceSourceSummary`、`EvidenceCoverageGap`、`EvidenceBuildResult`、`EvidenceBuildLimits`。
2. 复用现有 SiteInventory/SerpMarket/CompetitorCollection/FirstPartySnapshot 模型，不复制或放宽这些载荷合同。字段中需要维度或路径时使用明确键值/路径类型，不以任意 JSON 代替正式内部接口。
3. 使用设计中的五类安全错误代码：SOURCE_INVALID、SNAPSHOT_BINDING_INVALID、CHECKSUM_MISMATCH、ID_CONFLICT、LIMIT_EXCEEDED，均带 `V22_EVIDENCE_` 前缀。统一固定错误说明，不携带原始载荷。
4. 建立仅含虚构 `.test` 站点、固定 UUID、固定时区时间的输入 fixtures。分别包含公开模式、第一方健康模式和覆盖缺口场景，校验和按对应数据实际计算，不能用重复字符假摘要冒充内容校验和。
5. 写明哪些字段属于可信调用方提供的真实快照绑定；模型不得随机生成 snapshot UUID，也不得从 job UUID 推导快照 UUID。

定向测试：`tests/test_v22_evidence_models.py`。

完成标准：未知字段、错误来源载荷组合、非法时间/数值及无绑定实际载荷被拒绝；明确缺少快照的来源可以表达为缺口，不能生成伪造 Evidence。

## 3. 任务 2：稳定身份与观察去重基础

新增文件：

- `app/report_v22/evidence_identity.py`
- `tests/test_v22_evidence_identity.py`

实施步骤：

1. 实现标量规范化及显式类型标签，区分 bool、int、float、str、null；在调用 JSON 编码器前拒绝非有限数值。
2. 实现固定版本 `v22_evidence_identity_v1` 的规范化身份编码。复用既有排序 JSON 编码能力，输入包含来源、snapshot UUID、逻辑选择器、规范化值和类型；字典键、维度映射和语义集合明确排序。
3. 输出 `ev_` 加完整 64 位 SHA-256；不得使用遍历行号、当前时间、随机 UUID、report ID 或 Python 进程 hash。
4. 页面 URL/字段、文本片段、SERP record、竞品 ID、第一方维度/指标/单位分别具有稳定选择器。长上下文以摘要表达，完整内容保留于内部 trace。
5. 提供严格的“同 ID 内容一致”比较基础，避免 Python 中 `True == 1` 等宽松相等掩盖冲突。

定向测试：`tests/test_v22_evidence_identity.py`。

完成标准：相同输入跨进程仍相同；改变 snapshot、source、query、坐标、设备、竞品、维度、单位或值时不会误合并；字典键序和处理顺序不影响身份；非法数值不会被编码为 null。

## 4. 任务 3：快照绑定、归属与资格校验

新增文件：

- `app/report_v22/evidence_bindings.py`
- `tests/test_v22_evidence_bindings.py`

实施步骤：

1. 校验全部来源后再允许提取证据。公开快照验证完整模型 JSON 载荷摘要，第一方验证 normalized_payload 摘要；核对各自绑定 UUID、来源、版本与获取/过期信息。
2. 维护本次构建的 UUID 注册表，比较完整已验证来源对象与绑定。一个 UUID 对应不同内容、资源上下文、健康元数据或来源时整次失败。
3. 核对当前 case、客户站点、市场查询/地区/有效语言设备、确认竞品及竞品站点。支持已授权共享市场快照的原 UUID；不把不同 source job 当成错误来源，也不省略确认上下文核对。
4. 从固定 evaluated_at 得到资格：取得时间在未来拒绝；到期时只产生覆盖观察；未声明到期时间不自行编造时限。prospect 拒绝第一方快照。
5. 第一方需 healthy、matched、未过期才允许业务指标；同一模块将身份未确认、不匹配、健康异常等映射为设计规定的缺口原因。
6. 把“无快照”的正常缺口与“有载荷无绑定”的错误分开。此模块只校验本地输入，不伪称检查了数据库存在性或租户授权。

定向测试：`tests/test_v22_evidence_bindings.py`。

完成标准：篡改、错绑、同 UUID 元数据变化都被拒绝；正确绑定/已授权共享输入可用；过期边界使用固定时点可重复；原始页面字节摘要不被误当成解码 HTML 摘要复核。

## 5. 任务 4：站点证据适配器

新增文件：

- `app/report_v22/evidence_adapters/__init__.py`
- `app/report_v22/evidence_adapters/common.py`
- `app/report_v22/evidence_adapters/site.py`
- `tests/test_v22_evidence_site.py`

实施步骤：

1. common 层只封装观察构造、受限列表说明、来源物理路径与稳定选择器，不拥有来源特有规则。
2. 对结构检查成功页面，按白名单提取 HTTP/页面结构信息及真实统计；列表字段按实际元素形成标量观察，不把完整对象 JSON 字符串化。
3. 对有真实深度快照的页面，以最小页面上下文调用旧版 `build_evidence_ledger`。保留旧版文本清理能力，忽略其顺序编号，不调用规则结论函数。
4. 片段来源同时保留深度文本的物理路径与可重复执行的片段选择器；去重片段合并原位置，不能虚构 DOM 行号。
5. 对未深度采集、失败或 robots 禁止的页面只提供实际覆盖状态，不生成正文证据。达到旧版片段数量/长度限额时明确披露，不修改旧版函数行为。

定向测试：新 `tests/test_v22_evidence_site.py`，以及既有 `tests/test_evidence_quality.py`、`tests/test_evidence_readability.py`。

完成标准：新证据指回本页和本快照，重复片段稳定去重；未取得文本的页面没有正文证据；旧版证据测试继续通过。

## 6. 任务 5：SERP 市场证据适配器

新增文件：

- `app/report_v22/evidence_adapters/serp.py`
- `tests/test_v22_evidence_serp.py`

实施步骤：

1. 遍历已验证的 query run、call 与 result，只提取设计白名单的标量字段、实际结果计数和调用状态。
2. 选择器保留 result/call 标识、结果类型及字段名；locator/trace 保留 query、坐标、国家、语言、设备和存在的 URL。
3. 调用失败不得产生成功结果事实；成功空结果只支持“本次结果为空”，不能推断市场上没有竞品。
4. 对共享市场快照使用已核验的源 snapshot UUID，不用当前分析 job 覆盖它。

定向测试：`tests/test_v22_evidence_serp.py`。

完成标准：maps/local_pack/organic 和多个查询不串来源；相同商家在不同查询或位置的观察保持区别；空结果与部分失败有准确覆盖记录。

## 7. 任务 6：竞品证据适配器

新增文件：

- `app/report_v22/evidence_adapters/competitor.py`
- `tests/test_v22_evidence_competitor.py`

实施步骤：

1. 核对 collection 中的三个确认竞品，所有观察的逻辑选择器必须带 competitor ID。
2. 复用 site 适配器的提取逻辑，但明确使用 competitor 来源类型、竞品身份及实际 collection 快照内的物理路径。嵌套站点没有独立已绑定快照时不新造 snapshot UUID。
3. 从真实公开资料提取白名单字段，从实际评论提取文本、评分、存在的时间及商家回复；保留 review/request 标识，不额外采集。
4. 输出实际出现次数与最佳位置，并指回 collection 的对应字段；不把这些数值当成新的排名计算结果。
5. 处理 unavailable/partial 和评论采样限制，不将竞品公开 GBP 误标为客户第一方 gbp，不因某来源缺失补造内容。

定向测试：`tests/test_v22_evidence_competitor.py`。

完成标准：同名不同竞品不混淆，所有路径属于确认身份；每一条评论、回复和站点观察可溯源；部分成功保留成功观察与失败限制。

## 8. 任务 7：第一方指标适配器

新增文件：

- `app/report_v22/evidence_adapters/first_party.py`
- `tests/test_v22_evidence_first_party.py`

实施步骤：

1. 共用资格校验结果，为 GSC、GBP、GA4 的 aggregates 和 rows 中每个已有指标生成观察，不额外求和、估计、换算或填零。
2. 区分 aggregate 与 row；选择器包含外部资源、指标 key、unit 及全部已提供维度。维度按 key 规范化，重复 key 明确拒绝。
3. 将实际 query、支持的 device 和来源日期映射到 locator/覆盖期；缺失信息不从客户主查询或市场默认值猜填。
4. tablet 等无法直接放入冻结 locator 的上下文保留于 trace 和身份，locator.device 留空并附固定限制；不扩枚举。
5. 将不健康、身份未确认/不匹配、过期或真正空数据交给覆盖处理，不输出不合资格的业务指标。测量结果为零且已存在指标时正常保留。

定向测试：`tests/test_v22_evidence_first_party.py`。

完成标准：三类来源均有行/汇总及状态边界测试；两个相同数值但维度或单位不同的指标不合并；第一方字段和缺口不进入 prospect 的业务证据。

## 9. 任务 8：构建入口、覆盖记录及完整性

新增文件：

- `app/report_v22/evidence.py`
- `tests/test_v22_evidence_builder.py`

实施步骤：

1. 实现 `build_evidence_index` 纯函数入口，接收固定上下文、已绑定来源、明确缺少的来源及内部资源限制；不读取运行时时钟、网络、数据库或 Redis。
2. 全部来源验证完成后才调用适配器；将观察转换为现有 `EvidenceItem`，以稳定 ID 合并，字段冲突立即失败，不返回部分成功结果。
3. 有真实快照的失败/空数据生成 coverage Evidence 与对应 gap；没有快照的来源仅生成 gap。覆盖原因、健康、身份与置信度按设计映射，不混成业务事实。
4. 生成完整 source_traces/source_summaries，保留全部限制及来源路径；单个 Evidence 的限制超过 20 项时保留前 19 项与固定溢出提示。
5. 结果、来源、路径和限制采用固定排序；检测重复、悬空或无法解析的 trace。输入前后必须完全一致。
6. 实现最多 50,000 个唯一 Evidence 和完整结果 20,000,000 字节的默认上限；可使用较低测试参数。达到上限明确失败，不截断成看似完整的结果。

定向测试：`tests/test_v22_evidence_builder.py`，随后运行全部 `tests/test_v22_evidence_*.py`。

完成标准：公开/第一方/缺口场景都可离线构建；重复调用和来源遍历顺序变化得到一致结果；快照内部篡改必须触发校验失败；无伪造 ID、值或来源；资源及错误信息边界有测试。

## 10. 任务 9：共享输出样例与前端兼容性

新增文件：

- `scripts/export_v22_evidence_fixtures.py`
- `tests/test_v22_evidence_export.py`
- `tests/fixtures/report_v22_evidence/generated/`
- 前端 `src/lib/report-v22/test-fixtures/evidence/`
- 前端 `src/lib/report-v22/evidence-fixtures.test.ts`

实施步骤：

1. 从后端固定输入 fixtures 调用实际构建器，生成公开、第一方健康和缺口场景的证据输出及独立 manifest。记录输入摘要、输出摘要和内部身份算法版本。
2. 后端原始输入仅留在 inputs 目录；前端同步生成的 Evidence 样例及所需 manifest，不复制原始 HTML 或完整 provider 载荷。前后端生成输出逐字节一致。
3. 同步器支持显式 `--frontend-dir` 和完全只读的 `--check`。只操作预定生成文件；拒绝越界路径和未经处理的额外文件，保留无关文件，不整体替换代码目录。
4. 前端使用现有 AJV2020 配置和冻结 Schema 的 `$defs/EvidenceItem` 验证每条生成证据；保留原 Schema 的其余 `$defs` 供引用解析，不手工复制/修改定义。
5. 前端断言 fixture manifest、全部样例被消费、输出不含新增字段，加入必要反例检查：未知字段、非法来源/ID/值/日期结构不能通过。语义正确性由后端构建器测试负责，不能把 AJV 结构通过宣称为事实正确。
6. 文件名使用现有 `test:contract` 的目录扫描范围，不改冻结 contracts 目录或 V22-030 validation 资源，不构造 Findings/Actions 占位报告。

定向验证：

```bash
.venv/bin/python -m pytest tests/test_v22_evidence_export.py -q
.venv/bin/python scripts/export_v22_evidence_fixtures.py --check --frontend-dir ../search-trust
```

前端运行 `npm run test:contract` 及 `npm run typecheck -- --incremental false`。

完成标准：真实构建器输出通过两端合同约束；手动改动生成输出会被漂移检查发现；仅消费既有静态 Schema，不新增运行时网络依赖。

## 11. 任务 10：完整回归、差异审查与交付

后端完成以下检查：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/export_v22_contracts.py --check --frontend-dir ../search-trust
.venv/bin/python scripts/sync_v22_validation_resources.py --check --frontend-dir ../search-trust
.venv/bin/python scripts/export_v22_evidence_fixtures.py --check --frontend-dir ../search-trust
git diff --check
```

前端完成以下检查：

```bash
npm run test:contract
npm test
npm run typecheck -- --incremental false
npm run contracts:check
git diff --check
```

说明：`contracts:check` 会重新生成 types.ts 再检查差异，其余产物 `--check` 不写文件。证据导出命令在任务 9 实现后才存在，不属于当前计划阶段已经执行的检查。

审查并记录：

- 后端 `app/report_v22/models.py`、API 模型、旧版 ledger、配置、worker/executor 和冻结 contracts 没有改变。
- 前端运行时校验器、冻结 contracts、generated/types.ts 及 V22-030 样例没有非预期改变。
- 没有引入快照 UUID 生成、源数据写入、网络调用、生产开关变化或报告占位数据。
- 测试数量和命令结果按实际记录；未执行的浏览器/生产联调不写成通过。
- 形成完成记录，说明构建器调用方式、来源追踪、未来持久化接入要求及未覆盖边界，按范围保存本地提交；不推送、不部署。

## 12. 执行中的处理规则

发现已有采集模型无法表达某个测试场景时，先检查是否是预期的模型约束；不得为制造 fixture 放宽模型。使用模型允许的真实形状表达局部失败，或在现有字段无法表达时记录不支持的输入边界。

需要更改冻结合同、增加正式快照持久化/授权、修改旧版行为或扩大生产接入时，停止该扩展并说明原因，继续可独立完成的已批准范围；不要自行把 V22-032/033 合并进来。

执行结束条件：上述任务及已批准设计验收项均完成，或出现确实需要新决策的范围边界。计划完成不代表 V22-031 代码完成，也不代表线上分析可用。
