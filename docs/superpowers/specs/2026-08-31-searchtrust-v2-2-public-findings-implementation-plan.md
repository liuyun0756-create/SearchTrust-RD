# V22-032 首批公开 Findings 实施计划

日期：2026-08-31

依据：已批准的 `2026-08-31-searchtrust-v2-2-public-findings-design.md`，用户明确授权实施。

状态：六个工作单元均已完成本地实施与验证，结果见同日 public-findings-completion。writing-plans 技能不可用，本计划按批准设计手工编写；顺序执行，未启动子代理。

## 1. 严格输入、规则目录与身份

先增加失败测试，再新增 findings_models、findings_errors、public_rule_catalog 和 findings_identity 模块。仅支持 prospect，拒绝多份不同同类快照、第一方实际快照和域名歧义。固定六条实判规则、四条 GBP 未检查记录及规则版本。验证 fn_ SHA-256 编号、不同版本/目标/引用、重复与跨进程稳定性、严格额外字段及资源限制。

## 2. 现有快照计数与只读视图

在 site 适配器及其竞品复用路径增加 eligible_html_page_count 和三类 eligible_page_type_count。过滤 checked、2xx、HTML 媒体类型，记录实际库存完成时间和 pages/completed_at 路径。新增计数不能改变旧证据内容或编号。

建立只读 EvidenceView，索引必须绑定同一快照及物理来源，保留字段存在性、完整返回集合与实际采样时间。确认旧版回归与原证据测试通过。

## 3. 六条规则

新增 site_findings、market_findings、competitor_findings，按已批准文档的阈值与状态顺序实施：HTTP 400—599、明确 noindex、两个不同最终 URL 的重复标题、采样未观察到客户域名、至少两家确认竞品领先、三个固定页面类型的采样差距。

先补目标行为失败测试，再实现。覆盖查询类型/身份/排名基准隔离、空响应和缺 URL、真实零与未采集、两家阈值与第三家未知、库存采样时间恰好/超过 24 小时、最终 URL 重复/冲突、无 title 不补缺失结论。

## 4. 构建入口与汇总

实现 build_public_findings：全部验证 → 证据构建 → 规则执行 → 引用审查 → 排序去重 → 汇总/输出上限。保留安全错误与正常未检查的区别。覆盖数量/完整字节上限、错误引用、同编号冲突、重复遍历及输入不变。

八层始终保持本批批准的 not_checked，不输出虚假评级；真实页面类型集群只汇总客户采样范围。缺站点时计数为 null、集群为空，独立可用市场仍可输出发现。

## 5. 双端共享样例

新增独立 public-findings fixtures 输入/生成目录与导出器，前端仅接收 Finding/Evidence/LayerAssessment 样例及 manifest。覆盖触发、未触发和缺口；使用实际构建器输出加人工明确的语义期望，不伪造完整报告。

导出器支持 --frontend-dir 与只读 --check，预检全部目录，拒绝越界/符号链接/额外文件并保留用户文件。因新增计数更新 V22-031 生成输出，不改其输入或旧编号。前端用冻结定义验证结构及引用、哈希与反例。

## 6. 验证与交付

后端运行：

```bash
.venv/bin/python -m pytest tests/test_v22_findings_*.py tests/test_v22_evidence_*.py -q
.venv/bin/python -m pytest -q
.venv/bin/python scripts/export_v22_contracts.py --check --frontend-dir ../search-trust
.venv/bin/python scripts/sync_v22_validation_resources.py --check --frontend-dir ../search-trust
.venv/bin/python scripts/export_v22_evidence_fixtures.py --check --frontend-dir ../search-trust
.venv/bin/python scripts/export_v22_findings_fixtures.py --check --frontend-dir ../search-trust
git diff --check
```

前端运行：

```bash
npm run test:contract
npm test
npm run typecheck -- --incremental false
npm run contracts:check
git diff --check
```

审查冻结合同/API/类型、v2.1、V22-030 样例、配置、worker/executor 均不变；记录实际测试结果与仍未实现的客户 GBP/八层语义评级。保存范围内本地提交，不推送、不部署、不启用生产分析。
