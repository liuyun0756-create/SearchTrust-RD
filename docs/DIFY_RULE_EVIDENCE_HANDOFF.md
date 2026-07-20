# Dify 规则与后端证据分线交接

本次后端改动不需要数据库迁移，也不需要前端改动。Dify 控制台仍需由项目负责人手动修改并发布。

## 1. Start 节点

新增必填文本输入：

| variable | default | purpose |
| --- | --- | --- |
| `review_corpus` | `[]` | 后端固定生成的页面评论/证言与最近30条GBP评论快照 |

删除旧输入 `evidence_ledger`。后端不再把 Evidence Ledger 发给 Dify。

## 2. Rule 37、38、39

三个节点都必须读取 `review_corpus`，不要再从完整 `content` 中自行寻找评论，也不要直接读取 `gbp_data.review_list`。

- Rule 37：语料为空或所有评论都没有具体服务实体时为 `true`；至少一条存在具体服务实体时为 `false`。
- Rule 38：语料为空或所有评论都没有具体地理实体时为 `true`；至少一条存在具体地理实体时为 `false`。
- Rule 39：语料为空时为 `false`；存在至少一条明确的评论服务主题与页面主服务冲突时为 `true`，否则为 `false`。

节点仍只输出对应的一个布尔 JSON，不输出证据、解释或原文。

## 3. GBP 缺失语义

`web_scraper` 代码节点中的规则26-29必须保持可执行：

```python
gbp_exists = bool(gbp)
deterministic_applicability = {key: True for key in deterministic_results}
if not gbp_exists:
    deterministic_results = {key: True for key in deterministic_results}
```

含义：当前任务没有获得GBP数据时，规则26-29全部命中；报告 Coverage 仍由后端诚实显示GBP不可用，后端不会生成虚假的逐项比较结论。

## 4. 删除 Evidence Linker

删除 `evidence_linker` 节点及其连线，并从 End 节点删除 `rule_evidence_ids` 输出。Dify最终只需要返回：

- `report_copy_v2_1`
- `rule_results`
- `rule_applicability`
- `rule_errors`

旧的评分节点输出即使保留，后端也不会用于最终评分。

## 5. SaaS 报告节点

SaaS节点只负责英文话术：Page Level、8层解释、Key Issues、建议、执行项、路线图和Client Summary。不要输出或推断：

- Evidence / raw excerpts
- Coverage
- GBP状态
- 规则ID或层级状态
- Overall / Ranking Potential / Risk Level评分

这些字段全部由后端根据同一任务数据快照和完整规则向量生成。

## 6. 发布顺序

1. 在Dify完成上述修改并发布。
2. 再部署包含 `review_corpus` 输入的后端。
3. 用完全相同的Spot On输入连续运行3次。
4. 对比38个启用规则、8层状态和三个综合评分，三次必须完全一致。
