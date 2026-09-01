# SearchTrust v2.2 网站与公开 GBP 对齐实施计划

日期：2026-09-01

状态：已完成。所有批次于 2026-09-01 实施并通过全量验证。

依据：[2026-09-01-searchtrust-v2-2-site-gbp-alignment-design.md](./2026-09-01-searchtrust-v2-2-site-gbp-alignment-design.md)

目标：在保持所有未提供 `business_identity` 的旧输入与冻结输出完全不变的前提下，实现客户实体范围选择、网站与公开 GBP 四字段确定性比较、缺失证明、RuleEvaluation、Finding 和 roll-up 接入。

## 实施原则

- 每个批次先增加失败测试，再写最小实现，并在批次结束运行相关回归。
- 新逻辑只在显式提供并通过绑定校验的 `business_identity` 时启用。
- 不读取网络、时钟、数据库或环境变量，不生成随机 ID，不改生产开关。
- 调用方只能提供原始离线来源，不能提交预选候选、比较结果或 Finding。
- 所有新结果可由输入、版本化规则和 Evidence 确定性重建。
- 每个阶段都保留可提交、可回滚、旧 fixtures 不变的工作区。

## 批次 1：输入合同、错误和兼容边界

涉及文件：

- `app/report_v22/findings_models.py`
- `app/report_v22/findings_errors.py`
- `app/report_v22/public_rule_catalog.py`
- `app/report_v22/findings_common.py`
- `tests/test_v22_findings_models.py`
- `tests/test_v22_public_gbp_findings.py`
- `tests/test_v22_findings_boundaries.py`

步骤：

1. 冻结旧输入的 JSON 输出、Evidence ID、RuleEvaluation 和 Finding 回归样例。
2. 为 `PublicFindingsInput` 增加可选 `business_identity` 与两个严格 limits 模型。
3. 增加固定安全错误 `BINDING_INVALID`、`CHECKSUM_MISMATCH`。
4. 为四项 GBP 规则增加正式 RuleSpec，并让 `decision` 接受默认不变的可选 rule version。
5. 证明 `business_identity=None` 时完整输出逐字节不变。

完成条件：相关模型、边界和旧 findings 测试通过；旧 fixtures 无差异。

## 批次 2：版本化规范化与字段比较器

涉及文件：

- `app/report_v22/site_gbp_alignment_models.py`
- `app/report_v22/site_gbp_comparators.py`
- `tests/test_v22_site_gbp_comparators.py`
- `tests/test_v22_site_gbp_boundaries.py`

步骤：

1. 定义严格的规范化记录、比较对、字段状态、not_checked 原因及资源上限。
2. 实现名称 NFKC、空白、标点、and/& 和国家法定后缀等价。
3. 使用锁定的 `phonenumbers` 实现完整号码与分机比较。
4. 使用锁定的 `usaddress` 和保守组件规则实现地址比较及 incomparable。
5. 实现服务区域集合 exact/partial/mismatch。
6. 覆盖错误国家、无效值、不同分机、地址组件冲突、重复值和比较上限。

完成条件：比较器单元测试全部通过；没有模糊、联网或地理推断路径。

## 批次 3：客户实体绑定和网站候选选择

涉及文件：

- `app/report_v22/site_business_selection.py`
- `app/report_v22/site_gbp_alignment_models.py`
- `tests/test_v22_site_business_selection.py`
- `tests/test_v22_site_gbp_boundaries.py`

步骤：

1. 校验确认站点、规范域名、主要市场、经营模式和可选 GBP URL 的完整绑定。
2. 将页面划分为核心、业务核心扩展和非核心，不从 URL 重新猜测页面类型。
3. 按固定原因码选择 JSON-LD、明确 DOM 标签、重复电话/地址及结构化印证候选。
4. 保留 eligible、unresolved、excluded 及其全部证据引用，不依据 GBP 值反向选择。
5. 按字段计算网站必要检查集合、成功检查、missing 与 identity_unresolved。
6. 增加顺序无关、重复 final URL、非核心第三方实体和限制边界测试。

完成条件：相同事实任意输入顺序产生完全一致的选择结果；归属不明不会被误判为缺失。

## 批次 4：四字段对齐、缺失证据与独立复核

涉及文件：

- `app/report_v22/site_gbp_alignment.py`
- `app/report_v22/site_gbp_alignment_models.py`
- `tests/test_v22_site_gbp_alignment.py`
- `tests/test_v22_site_gbp_provenance.py`
- `tests/test_v22_site_gbp_boundaries.py`

步骤：

1. 组合选择结果、公开 GBP 字段状态和经营模式适用性。
2. 按固定优先级输出 exact、semantic、partial、mismatch、单侧缺失、双侧缺失、not_applicable 或 not_checked。
3. 为网站/GBP 缺失创建专用 coverage Evidence，并引用真实页面或字段状态路径。
4. 保存匹配对、未匹配、不可比较和未解决候选 ID；执行 30 天时间差下限检查。
5. 从原始输入重建并复核规范化键、选择理由、证据 ID、trace、checksum 和资源上限。
6. 对篡改候选、引用、路径、checksum、版本和结果的测试必须安全失败。

完成条件：每种字段状态均有成功和篡改测试；所有 ID 与序列化结果确定且可重建。

## 批次 5：Findings 管线接入

涉及文件：

- `app/report_v22/public_gbp_findings.py`
- `app/report_v22/findings.py`
- `app/report_v22/findings_view.py`
- `app/report_v22/findings_rollup.py`
- `tests/test_v22_public_gbp_findings.py`
- `tests/test_v22_findings_builder.py`
- `tests/test_v22_findings_counts.py`

步骤：

1. 在新路径内部从唯一 site 来源构建真实 `SiteBusinessFactsResult`。
2. 合并经复核的网站值 Evidence 和缺失 coverage Evidence，更新来源摘要及限制说明。
3. 将四项字段结果映射为 1.1.0 RuleEvaluation 和静态 Finding 文案。
4. 仅允许这四项规则使用专用 coverage Evidence 证明缺失，其他规则约束不变。
5. 将 Finding 纳入 site roll-up；八层结果继续为空/not_checked。
6. 最终再次验证全局引用、计数、排序、大小和决定性 ID。

完成条件：新输入产生预期 Finding/roll-up；旧输入、旧 hash 和所有原规则结果保持不变。

## 批次 6：共享冻结样例和前端合同

涉及文件：

- `scripts/export_v22_site_gbp_alignment_fixtures.py`
- `tests/fixtures/report_v22_site_gbp_alignment/inputs/*.json`
- `tests/fixtures/report_v22_site_gbp_alignment/generated/*.json`
- `tests/test_v22_site_gbp_export.py`
- `search-trust/src/lib/report-v22/test-fixtures/site-gbp-alignment/*`
- `search-trust/src/lib/report-v22/*.test.ts`

固定场景：exact_match、semantic_match、multiple_candidates_one_match、site_missing、gbp_missing、both_missing、service_area_partial、service_area_mismatch、operating_model_not_applicable、identity_unresolved、source_unavailable、comparison_time_gap。

步骤：

1. 通过唯一导出脚本生成并二次生成，证明 fixtures 幂等。
2. 后端校验 manifest、输入/输出绑定、规则版本、ID 和全局引用。
3. 前端只读取共享生成样例并验证冻结报告合同，不增加运行时功能或 UI。
4. 确认旧共享 fixtures 及其 hash 完全未变。

完成条件：前后端共享样例测试通过；重复导出没有工作区差异。

## 批次 7：全量验证和交付

步骤：

1. 运行后端格式、静态检查（若仓库已配置）和全量测试。
2. 运行前端类型检查、相关测试及全量测试。
3. 检查两个工作区只包含本轮预期文件，无缓存、密钥或生成噪音。
4. 更新完成文档，记录范围、关键语义、测试数量、未实施项和后续生产接线条件。
5. 分别提交后端和前端变更，不推送、不部署、不打开生产开关。

完成条件：全量验证通过、两个仓库提交完成且工作区干净。
