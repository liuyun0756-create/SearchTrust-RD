# 网站业务候选提取实施计划

日期：2026-08-31。状态：用户批准的本轮本地实施及验证已完成，未推送、未部署。

依据同日 site-business-facts-design。writing-plans 技能不可用，手工编制本计划并按批准范围顺序实施；不启动子代理。

## 实施顺序

1. 先建立失败测试：严格模型、来源绑定、无来源/过期/缺正文状态及安全错误。实现新模型与独立绑定边界。
2. 实现有界 HTML 树和结构化 JSON 读取，保留实际字符区间、元素路径和 JSON Pointer；测试超深结构、重复键、局部损坏及隐藏区域。
3. 实现白名单字段、显式 DOM 标签、电话和地址候选；保留原值、多实体和归属不明确，不选主值、不比较 GBP。
4. 实现候选稳定编号、组件隔离、独立 Evidence 索引与引用复核；验证顺序/重复、Unicode、原文空格、字节和计数上限。
5. 新增五组固定虚构样例和安全导出器；前端仅验证冻结 Evidence 数组、哈希和反例，不修改运行时。
6. 完整回归、自查批准范围、保存完成记录与本地提交。禁止推送、部署、生产配置或旧入口行为变化。

## 验证

后端执行新增 site_business 定向测试、完整 pytest，以及以下双端只读检查：export_v22_contracts、sync_v22_validation_resources、export_v22_evidence_fixtures、export_v22_findings_fixtures、export_v22_public_gbp_fixtures、export_v22_site_business_fixtures。前端执行 test:contract、test、非增量 typecheck、contracts:check。双端执行 git diff --check。

必须保留冻结报告/API/Schema/类型、旧样例与编号、v2.1、采集器、数据库、配置、worker/executor、前端运行时及四项 GBP 未检查行为。固定声明保留候选/未验证主体语义。原响应摘要只引用，不冒称可从解码 HTML 复验原字节。

全新功能以定向失败测试先行，最终测试数量、真实执行结果及剩余能力写入完成记录。没有真实网络联调、浏览器/UI/PDF 验收，不将这些写为完成。

## 执行结果

上述六步均完成。新增后端测试 195 项，全量 1197 项通过；前端新增 15 项，合同 218 项、全量 260 项通过。非增量类型检查、冻结类型重新生成无差异、六类双端资源只读检查通过。

失败测试定位并修正了 HTMLParser 内部属性命名碰撞、旧 HTML 去空格与字符位置偏移、Pydantic 严格 JSON 时间重验、对象混入字典跳过嵌套校验、循环非法输入、嵌套字段误拼接、损坏脚本预算绕过、页面失败状态及完整限制说明汇总等问题。详情见同日 site-business-facts-completion。
