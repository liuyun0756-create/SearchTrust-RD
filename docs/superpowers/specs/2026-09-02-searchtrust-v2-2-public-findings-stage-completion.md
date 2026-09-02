# SearchTrust v2.2 公共 Findings 检查点阶段完成记录

日期：2026-09-02

状态：方案 A 已实现并通过完整后端回归；未推送、未部署、未开启生产功能。

依据：

- [公共 Findings 检查点阶段设计](./2026-09-02-searchtrust-v2-2-public-findings-stage-design.md)
- [公共 Findings 检查点阶段实施计划](./2026-09-02-searchtrust-v2-2-public-findings-stage-implementation-plan.md)

## 1. 已完成内容

新增 `CheckpointedPublicFindingsStage`，把现有 `build_public_findings` 封装为独立的正式任务阶段：

- 严格重验 `PublicFindingsInput`，模型输入与等价 JSON 输入使用同一检查点；
- 输入摘要显式绑定阶段版本、公共 Findings 规则版本和完整标准请求；
- 使用任务隔离的版本化检查点键；
- 检查点封装绑定 `job_id`、输入摘要、规则版本、结果校验和和完整 `PublicFindingsResult`；
- 命中检查点时跳过 Findings 重建；
- 首次并发执行沿用 `JobCheckpoints.run_once` 的首次写入为准语义，双方返回同一标准结果；
- 对损坏、错版本、错任务、错输入摘要、错规则版本、错结果校验和和非法结果结构统一安全失败；
- 已知 `FindingsError` 保留原有稳定代码和不可重试分类；
- 未知程序错误不被吞掉或伪装成业务 `not_checked`；
- 超过请求结果字节限制的输出不会写入检查点；
- 日志仅包含任务 ID、摘要后缀、命中状态和计数，不记录请求、网页、地址、电话或证据原文。

## 2. 代码与测试

新增：

- `app/jobs_v22/public_findings_stage.py`
- `tests/test_v22_public_findings_stage.py`

更新：

- 实施计划中的测试命令统一改为仓库既有的 `.venv/bin/python -m pytest` 入口。

新增 21 项阶段测试，覆盖：

- 首次保存和重复恢复；
- 输入变化隔离；
- 模型/JSON 等价输入；
- 六类损坏或错误绑定检查点；
- 六类已知 Findings 错误；
- 未知程序错误；
- 非法 builder 结果；
- 输出资源上限；
- 输入重验先于 builder；
- 并发首次执行；
- 敏感日志排除。

## 3. 验证证据

相关阶段和 Findings 回归：

```text
67 passed in 1.69s
```

后端完整测试集：

```text
1282 passed in 8.54s
```

其他检查：

- Python 编译检查通过；
- `git diff --check` 通过；
- 当前环境未安装 Ruff，因此未额外运行 Ruff；
- 前端 `search-trust` 工作区保持干净，最后提交仍为 `49f2436`；
- 后端 `V22_ANALYZE_ENABLED`、`V22_PREFLIGHT_ENABLED`、`V22_COMPETITOR_DISCOVERY_ENABLED` 默认值仍为 `False`；
- Worker 仍显式使用 `UnavailableV22Executor`。

## 4. 明确未实现

本轮没有实现或改变：

- Actions 和“恰好三条行动建议”；
- Dify 文案合同；
- 最终 `ReportV22` 组装；
- Prospect 总执行器及 Worker 接线；
- GSC、授权 GBP、GA4 verified execution；
- HTTP API、前端页面或共享前端合同；
- 生产功能开关、推送、部署或发布。

## 5. 后续入口

下一阶段若继续完整方案 B，可由 Prospect 执行器直接构造 `PublicFindingsInput` 并调用 `CheckpointedPublicFindingsStage.build(...)`。本阶段的输入绑定、错误分类、幂等和恢复语义无须重写。
