# SearchTrust v2.2 公共 Findings 检查点阶段实施计划

日期：2026-09-02

状态：设计已批准，用户已确认开始实施。

依据：[2026-09-02-searchtrust-v2-2-public-findings-stage-design.md](./2026-09-02-searchtrust-v2-2-public-findings-stage-design.md)

## 目标

实现独立的 `CheckpointedPublicFindingsStage`，把现有公共 Findings 纯构建器包装为严格版本化、输入绑定、结果带校验和且可从 Redis 检查点恢复的任务阶段。本轮不接入主 Worker，不生成 Actions 或最终 ReportV22，不开启生产开关。

## Task 1：以失败测试锁定阶段合同

文件：

- 新增 `tests/test_v22_public_findings_stage.py`

步骤：

1. 使用现有 `findings_helpers.request()` 构造包含网站、SERP 和竞争对手证据的严格 `PublicFindingsInput`。
2. 使用 `fakeredis.aioredis.FakeRedis` 和真实 `JobCheckpoints`。
3. 写入首次构建和重复命中测试，断言第二次不调用 builder。
4. 写入输入变化测试，断言检查点键变化并重新构建。
5. 写入检查点封装字段和结果校验和测试。
6. 先运行该测试文件并确认因阶段模块尚不存在而失败。

验证：

```bash
.venv/bin/pytest -q tests/test_v22_public_findings_stage.py
```

## Task 2：实现最小严格阶段

文件：

- 新增 `app/jobs_v22/public_findings_stage.py`

步骤：

1. 定义私有严格 `_PublicFindingsCheckpoint` 模型，固定 `schema_version` 和 `ruleset_version`。
2. 定义稳定的阶段版本、规则版本、检查点键及安全用户消息常量。
3. 构造函数默认使用 `build_public_findings`，并允许测试注入等价同步 builder。
4. `build()` 重新校验输入，摘要显式绑定阶段版本、规则版本和标准输入。
5. 先读取现有检查点；命中时严格验证任务、摘要、规则版本、结果模型和校验和。
6. 未命中时通过 `JobCheckpoints.run_once` 执行无副作用纯构建、重新验证结果并保存封装。
7. `run_once` 返回后再次走统一验证路径，确保并发首次写入时双方读取同一标准值。
8. 运行 Task 1 测试直至通过。

## Task 3：锁定错误分类、损坏拒绝和并发语义

文件：

- 修改 `tests/test_v22_public_findings_stage.py`
- 必要时修改 `app/jobs_v22/public_findings_stage.py`

步骤：

1. 参数化覆盖错结构、错阶段版本、错任务 ID、错输入摘要、错规则版本、错结果校验和和非法嵌套结果。
2. 断言所有检查点错误均为稳定、不可重试的 `DeterministicJobError`，且消息不含测试敏感值。
3. 参数化覆盖 `FindingsError` 的输入、校验和、绑定、引用、ID 冲突和限制代码映射。
4. 断言未知 `RuntimeError` 原样传播，不被改成业务缺失或已知确定性错误。
5. 增加并发首次执行测试，断言最终结果相同且只有一个标准检查点。
6. 增加日志捕获测试，断言日志不含地址、电话、网页正文和完整输入。
7. 运行阶段测试和相关 Findings 测试。

验证：

```bash
.venv/bin/pytest -q tests/test_v22_public_findings_stage.py tests/test_v22_findings_builder.py tests/test_v22_public_gbp_findings.py tests/test_v22_site_gbp_findings.py
```

## Task 4：完整回归与完成记录

文件：

- 新增 `docs/superpowers/specs/2026-09-02-searchtrust-v2-2-public-findings-stage-completion.md`

步骤：

1. 运行格式/差异检查。
2. 运行后端完整测试集，记录实际通过数量和耗时。
3. 确认前端仓库无变化，后端只包含本轮范围内文件。
4. 确认 `V22_ANALYZE_ENABLED` 等生产开关保持关闭，Worker 仍使用 `UnavailableV22Executor`。
5. 编写完成记录，列出实现、测试证据、明确未实现项和下一阶段入口。
6. 提交业务实现和完成记录，不推送、不部署。

验证：

```bash
git diff --check
.venv/bin/pytest -q
git status --short
```

## 完成判定

- 新阶段可以独立构建、保存和恢复完整 `PublicFindingsResult`。
- 相同任务与输入幂等，输入变化隔离，并发执行采用同一标准值。
- 损坏或错误绑定检查点明确失败。
- 已知 Findings 错误稳定映射，未知程序错误不被吞掉。
- 所有相关测试和后端完整测试集通过。
- Worker、API、前端、生产开关和部署状态均保持不变。
