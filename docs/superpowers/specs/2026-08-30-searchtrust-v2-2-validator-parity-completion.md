# V22-030 模型与校验器一致性完成记录

日期：2026-08-30

范围：同日已批准的 validator-parity-design；后端 `SearchTrust-RD` 和前端 `search-trust`。

状态：本地实施与验证完成；未推送、未部署、未启用生产分析。

## 1. 完成内容

- 保留 Pydantic 模型、API 模型和冻结 `2.2.0` 合同，不新增报告字段。
- 前端补齐查询非空与 Unicode 去重、Evidence/GSC/GBP/GA4 日期先后、三条页面数量关系、路线图阶段顺序校验。
- 修正地区对象键顺序、可选字段缺省/null、局部字符串空白比较，以及竞品完整网址规范化后的唯一性判断。
- 新增后端生成的 Unicode 15.0.0 casefold 与空白字符表。前端运行时仅引用生成表，不依赖 Python、测试样例或测试加载器。
- 建立 121 个共享报告样例，两端消费相同接受/拒绝预期，前端还检查错误代码与字段路径。涵盖设计中的 12 个复现样例、既有语义规则及合法边界。
- 新增严格样例加载器，非法操作/路径、重复 ID、未知 fixture 均失败；通过深拷贝及输入前后比较验证测试和校验器不改写原报告。
- 新增独立辅助资源同步器，检查资源和基础 fixture 哈希，只写明确目标文件，不替换代码目录；测试覆盖漂移、只读检查、非法目标、符号链接越界及无关文件保留。
- 新增语义错误保持现有公开代码，使用固定说明，不回显查询内容。

## 2. 验证结果

修复前：初始 113 个共享样例揭示 29 个前端失败案例，后端预期全部通过。先记录失败，再修复校验逻辑；后续补充样例后总计 121 个。

最终结果：

| 验证 | 结果 |
| --- | --- |
| 后端完整 pytest | 715 passed |
| 后端新增样例、加载器、同步器测试 | 152 passed |
| 前端完整 Vitest | 201 passed，11 个测试文件 |
| 前端非增量 TypeScript 检查 | 通过 |
| 后端及前端冻结合同导出检查 | 通过 |
| 辅助资源双端只读一致性检查 | 通过 |
| TypeScript 类型重新生成与差异检查 | 通过，无类型变更 |
| 改动格式与冻结路径检查 | 通过 |

前端测试有既存 Node `module.register()` 弃用警告，不影响测试结果。本轮未执行浏览器/UI 验收或生产外部服务联调，因为没有修改 UI 或接入生产服务。

## 3. 复验与维护入口

后端仓库：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/export_v22_contracts.py --check --frontend-dir ../search-trust
.venv/bin/python scripts/sync_v22_validation_resources.py --check --frontend-dir ../search-trust
```

修改后端维护的共享样例，或明确升级字符规范化基准后，使用以下命令生成/同步辅助资源，再运行双端测试。不要手动修改前端副本或绕过差异检查。

```bash
.venv/bin/python scripts/sync_v22_validation_resources.py --frontend-dir ../search-trust
```

前端仓库：

```bash
npm run test:contract
npm test
npm run typecheck -- --incremental false
npm run contracts:check
```

`contracts:check` 会重新生成类型后检测差异；辅助资源 `--check` 则完全只读。

## 4. 未改变的边界

- 后端报告/API 模型、Schema、原始合法 fixtures、合同 manifest 及前端生成类型没有变化。
- 没有修改配置或执行器；代码默认的两个 v2.2 开关仍为 false，生产 worker 仍使用 `UnavailableV22Executor`。
- 不增加行动依赖环、引用列表去重等新业务规则，不实现 V22-031 证据索引或后续 Findings/Actions。
- 本轮保证已列出的语义规则与共享样例一致，不宣称 Pydantic 和 AJV 对所有任意原始 JSON 的格式或默认行为完全等价。
- 未调用外部数据提供方，未变更数据库、v2.1 或 PDF 路径。

下一里程碑：V22-031 证据索引。该里程碑尚未实施。
