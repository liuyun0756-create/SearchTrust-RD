# SearchTrust v2.2 合同冻结实施计划

状态：已完成

日期：2026-08-26

范围：V22-003

设计依据：`docs/superpowers/specs/2026-08-26-searchtrust-v2-2-contract-design.md`

## 1. 后端模型

1. 新建 `app/report_v22` 严格 Pydantic 模型和合同版本常量。
2. 新建 `app/api/v2` 请求、响应与持久任务 envelope 模型，不注册运行时路由。
3. 在模型级验证器中实现报告类型、三项行动、ID 唯一性、引用完整性和 Full Evidence Coverage 约束。

验收：正向模型可实例化，所有关键不变量有负向测试。

## 2. 合同产物

1. 建立 prospect 和 verified 两份共享 fixture。
2. 编写确定性导出脚本，输出报告 Schema、API Schema 和 manifest。
3. 使用稳定 JSON 序列化、SHA-256 和临时目录原子替换。
4. 支持显式 `--frontend-dir` 同步合同包。

验收：重复导出字节一致，manifest 哈希与文件一致，无效 fixture 不覆盖正式产物。

## 3. 后端测试

1. 覆盖正向 fixtures、未知字段、边界数量、重复 ID、悬空引用和报告类型条件。
2. 覆盖 verified 三源 snapshot、Full Evidence Coverage 和 parent Finding 外部引用。
3. 覆盖 API 严格字段及 Google token 拒绝。
4. 运行全部 v2.1 回归。

验收：新增测试与原有测试全部通过。

## 4. 前端合同消费

1. 创建同名实现分支。
2. 安装 AJV、json-schema-to-typescript 和 Vitest。
3. 同步后端合同包。
4. 从 JSON Schema 生成 `generated/types.ts`，禁止手写生成类型。
5. 实现 AJV 结构校验、manifest 哈希校验和引用语义校验。

验收：两份共享 fixture 返回强类型报告，负向输入返回稳定安全错误。

## 5. 前端测试与脚本

1. 增加合同生成、检查、测试和 typecheck npm scripts。
2. 测试正向 fixtures、未知字段、错误枚举、缺失字段、重复 ID 和悬空引用。
3. 检查重新生成 TypeScript 后没有 Git 差异。

验收：合同测试、`tsc --noEmit` 和生产构建通过。

## 6. 双仓最终验收

1. 对比两个仓库的 manifest、Schema 和 fixture SHA-256。
2. 运行后端全部 pytest。
3. 运行前端合同测试、typecheck 和 build。
4. 确认 `/api/v1` 与 v2.1 文件没有行为改动。
5. 分别提交前后端实现。

完成后 V22-003 进入已验收状态，下一阶段为 V22-010 Supabase migration。
