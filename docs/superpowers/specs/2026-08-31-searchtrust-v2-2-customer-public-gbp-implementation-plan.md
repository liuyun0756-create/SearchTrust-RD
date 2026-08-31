# 客户公开 GBP 快照与证据接入实施计划

日期：2026-08-31

依据：用户已批准同日 customer-public-gbp-design。状态：批准范围已实施并通过本地回归；详见同日 customer-public-gbp-completion。

writing-plans 技能不可用，按已批准设计手工制定本计划；顺序执行，未启动子代理。先建立失败测试，再实现和验证，不重复设计审批。

## 1. 严格快照与身份基础

新增 public_gbp_models / identity / snapshot / errors 及测试辅助输入。模型包括独立确认参考、请求目标、返回强标识、六项必需 state/value 字段、采样元数据及推导身份标签。采用固定时间、固定虚构标识的失败测试验证类型、字段上限、请求与参考绑定、同类型标识冲突优先、域名冲突、无标识待确认和真实 false。

实现离线纯构建入口：不分配 UUID、不访问时钟或网络；重验嵌套输入，计算完整参考摘要及身份/健康状态，执行 30 天显式期限及输入/输出 1 MB 上限。错误不泄露载荷。

## 2. 来源绑定和证据索引

扩展内部 EvidenceSource 为 public_gbp，冻结 source_type 保持 gbp；为参考上下文及内部摘要/缺口补充设计允许的字段。保留旧 selector 内容与编号。

新增来源专属验证，先验证全部来源再适配；重算参考摘要、身份标签和完整载荷校验，限制一份不同公开快照，区分真实来源身份失败与非法绑定。公开来源只支持 prospect；公开资料与第一方 GBP 未连接缺口可共存。

适配器为实际字段、强标识和来源 URL 生成标量证据，为缺字段、身份待确认/冲突、过期或失败生成覆盖证据；所有路径可在当前绑定快照中解析，不冒充原始响应复核。验证旧 Evidence 内容和编号不变。

## 3. Findings 兼容

独立生成四项 GBP 未检查记录：无资料、来源不合资格、字段未取得或比较尚未实现分别表达。GBP 引用必须来自本次客户公开来源、同一字段或其覆盖，不能借用竞品/第一方证据。六条实判规则保持不变，八层全部未检查。

## 4. 共享样例与安全导出

新增 matched / partial / identity_conflict / expired 四组固定虚构输入及独立导出器，保存实际 Evidence/Finding/LayerAssessment 片段与 manifest。前端只接收生成输出，使用冻结定义验证结构、引用、哈希和反例。检查只读模式、路径预检、符号链接、额外文件和漂移，不改旧 fixtures。

## 5. 回归和交付

后端复验：

```bash
.venv/bin/python -m pytest tests/test_v22_public_gbp_*.py -q
.venv/bin/python -m pytest -q
.venv/bin/python scripts/export_v22_contracts.py --check --frontend-dir ../search-trust
.venv/bin/python scripts/sync_v22_validation_resources.py --check --frontend-dir ../search-trust
.venv/bin/python scripts/export_v22_evidence_fixtures.py --check --frontend-dir ../search-trust
.venv/bin/python scripts/export_v22_findings_fixtures.py --check --frontend-dir ../search-trust
.venv/bin/python scripts/export_v22_public_gbp_fixtures.py --check --frontend-dir ../search-trust
git diff --check
```

前端复验：

```bash
npm run test:contract
npm test
npm run typecheck -- --incremental false
npm run contracts:check
git diff --check
```

自查冻结报告/API/Schema/类型、v2.1、现有样例、采集器、配置与 worker/executor 不变。记录真实测试结果与剩余能力后保存本地提交，不推送、不部署。本轮不是实时采集、四项实质对齐、八层评级或完整 v2.2 发布。
