# P0-08 金标准输入与离线评估

本目录保存真实网页快照和空白标注模板。`scripts/eval_gold.py` 只读运行当前规则的发现生成，并比对人工答案；不把空白答案、缺少生成输入或未抓到信息解释为“没有”。这是内部评估工具，不修改客户报告或生产生成逻辑。

## 六站输入

| 目录 | 组 | 保存范围 | 完整发现生成输入 |
| --- | --- | --- | --- |
| nova-service | A | 引用原始 v5 报告、快照、生成配方；三个文件的 SHA-256 写入 input.json | 本机可用，依赖仓库外的私有冻结文件 |
| pr-plumbing-fresno | A | 2026-11-03 官网首页静态 HTTP 响应 | 待提供 |
| all-star-plumbing-fresno | A | 2026-11-03 官网首页静态 HTTP 响应 | 待提供 |
| vision-tech-swansea | B | 原 P0-03 保存的 Swansea 页面 HTML，逐字节复制 | 待提供，未复制测试中的虚构 GBP 和身份绑定 |
| seamless-gutters-pensacola | B | 2026-11-03 gutter-installation 页面静态 HTTP 响应 | 待提供 |
| volcanoes-hiking-guides | B | 2026-11-03 官网首页静态 HTTP 响应 | 待提供 |

Vision-Tech 的网址为 https://visiontechav.com/swansea/；Volcanoes 的网址为 https://www.volcanoeshikingguides.com/。其余网址按任务卡。`capture.json` 记录抓取时间、最终 URL、HTTP 状态、字节数和校验值；Volcanoes 首次因 2 MB 上限失败，原记录保留，成功重试记录为 `capture-retry.json`，页面为 2,141,616 字节。未截断页面，也未增加生产抽取器的输入上限。

`page.html` 是 HTTP 响应正文原始字节，不代表渲染后的页面。`input.json.snapshot_files` 中 `base=case` 相对于该站目录；`base=private` 相对于工作区 `.release-private/2026-10-29/`。Nova 的冻结文件不复制入版本库、不改写；在缺少私有文件的机器上不可宣称该站已完成重放。

只有 Nova 有完整的既有生成上下文。生产发现生成器还要求服务、目标市场、查询词和已确认竞品；仅凭一个 HTML 不能填造这些输入。没有为其他五站伪造生成配方、GBP、经营身份或身份确认。

## truth.json 填写约定

由产品负责人或验收人填写，实施者没有填写真实答案。当前 `checks.reported_issue` 仅记录任务卡反馈或负责人要求的通用检查，不是已经验证的答案。

- `reviewed_by`、`reviewed_at`：标注者和带时区的时间；尚未标注时为 null。
- `site_fields`：`business_name`、`address`、`phone`、`opening_hours`、`service_area` 各有 `present`（true / false / null）、`values`（字符串数组或 null）、`evidence_refs`。null 表示未知；确认不存在才能写 false 和空数组。必须注明页面、片段和采集来源。
- `gbp_fields`：上述字段加 `category`；`state` 可填写 observed / confirmed_absent / not_checked；`values` 为字符串数组或 null。不能把网站上的电话复制过来当作 GBP 观察值。
- `operating_model.value`：人工确认的经营模式或 null；地址是否应对齐必须结合这一项。
- `local_entities.values`：人工确认的页面本地实体数组或 null；建议每项记录 name、type、page_url、excerpt。
- `trust_claims.values`：人工确认的声明数组或 null；每项记录 text、page_url、href、resolved_url、destination_entity、belongs_to_business（true / false / null）。必须区分原始链接与已检查的跳转终点。
- `checks`：必须回归的问题。`rule_id` 为当前实现的规则 ID 或 null；`target` 待从实际生成的 RuleEvaluation 中复制完整目标，避免把不同 URL、搜索词或采样范围混在一起；`expected_triggered` 由人工填写 true / false / null。未实现的类别、信任链接、本地内容检查不能冒充已通过。
- 每项 `evidence_refs` 记录来源文件和可复核定位；不要只填写一个结论。尚未标注时保持空数组。

Vision-Tech 模板已有电话、地址和营业时间三个反馈槽位；Volcanoes 已有类别、地址与经营模式、评分链接目标、本地内容四个槽位。负责人确认 Seamless 没有已记录的具体错误，已改为通用检查：电话/地址/营业时间、服务页定位、服务区、客户可见弱依据行动，所有人工答案仍为空。

## 运行与标注

在后端根目录执行（只输出到 stdout，不写任何快照或答案）：

```bash
.venv/bin/python scripts/eval_gold.py
.venv/bin/python scripts/eval_gold.py --site nova-service --private-root ../.release-private/2026-10-29
.venv/bin/python -m pytest -q tests/test_p0_08_gold.py
.venv/bin/python -m pytest -q -s tests/test_p0_08_gold.py::test_offline_actual_generation_counts_false_positive_and_false_negative
```

输出为 JSON。缺完整输入的五站按负责人答复输出 `status=无法判断`、`reason=generation_input_missing`，对应行动数为 null；这些站不混入“已生成报告”的行动总数。文件校验失败、错误输入和标注冲突列入 `errors` 并以非零退出码退出。仅仅缺输入不代表脚本运行失败，但 `summary.incomplete=true` 明确保留未完成覆盖。

`evaluation_catalog` 包含实际规则检查和完整目标。标注者从中复制 `rule_id` 与 `target` 到检查槽位，判断该范围内是否应生成发现后填写 `expected_triggered`；同一规则不同 URL、查询词、设备等不能共用答案。重复目标标注会被拒绝。`site_fields.present=true` 用于未检查率，不自动推导“对齐规则应不应触发”：页面有电话与电话是否正确对齐是不同问题，后者需比较人工确认的 GBP 值。

`checks.kind` 默认 `rule`。另外两类辅助回归检查为：`page_type`（`page_url`、人工填写的 `expected_page_type`）和 `client_weak_actions`（人工填写的 `expected_count`）。辅助结果在 `checks` 中显示符合/不符/无法判断，不混进每条发现的误报率。空答案统一显示未标注，即便系统已有可观察结果也不会冒充人工确认。

未来取得完整输入后，可在独立新文件中保存 `{manifest, request}`，将 `input.json.generation` 设为 `{"kind":"bound_input","input":{"base":"case","path":"bound.json","sha256":"文件原始字节的 SHA-256"}}`，并加入 `snapshot_files`。所有来源必须是真实绑定输入，不能从 truth 反向生成生产事实。也可提供经 `DeliveryRevision.validate_report` 校验、与重新生成报告相符的 `delivery_revision` 文件引用，按其显式可见行动选择评估；未提供时使用默认客户视图规则。禁止改写已冻结源文件。

## 已确认的统计口径

- `finding_metrics.false_positive_rate`：误报数 / 可判定的已生成发现数。不把正确的“未触发”加入分母，也不把未标注发现加入分母。
- 漏报：人工确认应生成，实际对应检查为 `not_triggered`；`not_checked`、没有对应检查、未实现规则和缺输入均是无法判断。
- 无可用分母时比率为 null。当前真实答案全部空白，所以计数为 0 不代表准确率良好，比率仍为 null。
- `field_metrics.not_checked_rate`：人工确认存在且系统判未检查的字段数 / 人工确认存在的字段数；`not_checked_by_reason` 复用实际对齐/规则结果的原因。缺整个生成输入时，不捏造系统的字段状态；相应字段计入 `unavailable_present_fields`，覆盖不全的比率显示 null。营业时间等尚无对齐规则的字段明确记 `semantic_rules_not_implemented`。
- `action_metrics.visible_resampling_actions`：客户可见行动中模板为 `review_market_visibility` 的数量。修复后附带复查步骤的行动不作为“再搜一次”类；未知模板不擅自按 0 计算。
- `visible_weak_only_actions`：可见行动的全部依据都未确认或低置信的数量，应由 P0-07 默认隐藏。`visible_actions_with_any_weak_basis` 另报混合依据数量；混合中只要有 confirmed 且非 low 的依据仍保留，沿用负责人已确认的 P0-07 口径。手工交付选择可以显式显示弱依据行动，统计会如实反映。
- 汇总 `action_totals_available_cases` 只统计有生成结果的站点，并同时输出已评估/不可评估站点数，不把五站未知值计作五个 0。

Nova 先用旧规则重放并验证其规范 JSON 字节与冻结 v5 报告相同，再只在内存中派生当前 v8 输入；旧文件不改变。脚本不请求网络、Firecrawl、搜索服务或模型。测试禁用 socket 连接和 DNS 后执行完整六站入口，并另外用手工合成输入证明 1 误报 / 2 可判定生成发现＝0.5，另有 1 漏报；合成数据不写入真实站点答案。
