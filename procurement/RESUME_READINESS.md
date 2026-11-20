# 简历就绪审查 · 2026-11-12

**结论：可以作为“大模型应用 / Agent 开发”的个人项目展示。当前验收范围是可复现的合成采购业务 MVP，不是企业生产交付。**

项目一 DataTrail 本轮未修改。本项目提供另一条主线：从理解供应商页面，到实际操作采购后台并核对结果。

## 可核查的结果

| 验收项 | 实际结果 | 证据 |
| --- | --- | --- |
| 完整业务链路 | DeepSeek + Skyvern 采集报价、填写草稿、回读记录均跑通 | `evaluation/baseline/full-normal.json`、`evaluation/ui-smoke.json` |
| 业务合同和 HTTP 集成测试 | 34 项通过 | `evaluation/test-results.xml`、`tests/` |
| 核心 Python 类型检查 | 5 个业务源码文件通过 mypy | 本轮命令见下 |
| 新增代码静态检查 | Ruff 通过 | 本轮命令见下 |
| 开发任务 | 4 个任务，各策略一次；两组均 4/4 | `evaluation/results/20261111T194808Z-development/` |
| 保留任务 | 20 个合成变体，各策略一次；模型选择 19/20，业务规则版本 20/20 | `evaluation/results/20261111T195306Z-heldout/` |
| 真实 HTTP 重复提交 | 16 个等价请求全部复用，事件数不增加、草稿编号不变；冲突需求返回 409 | `evaluation/http-replay.json` |
| 故障与恢复 | 保存后返回 503 的真实浏览器任务已通过；ASGI 重启后对账恢复有集成测试 | 开发任务 response-loss、`tests/test_business.py` |
| 页面 | 工作台表单真实提交完成；桌面/移动端初始布局检查无 JS 错误、无移动端横向溢出 | `evaluation/ui-smoke.json`、`evaluation/assets/` |
| 上游 UI 及截图 | 本地代理后诊断 403 消除，UI 无 JS 错误；截图代理 200、越界路径 403 | `evaluation/ui-smoke.json`、`evaluation/artifact-access.json` |

20 个任务是**同一合成环境中的参数与布局变体**，不是 20 个真实供应商项目。40 次采购作业不等于 40 次模型 API 调用；一次作业可以包含采集和填写两个 Skyvern 任务，每个任务又可以多次调用模型。

## 一个能讲清的真实失败

任务 h03 要求采购 25 件。Northline 每箱 10 件、每箱 120 CNY；Harbor 按件 15 CNY。

- 两个真实模型运行都选择了 Northline，且提取出的报价内容经规范化后相同。
- model_only 创建了 25 件、300 CNY 的草稿，相当于按 2.5 箱计算，不满足整箱采购约束。
- verified 将该报价标为 fractional_pack，使用 Harbor 创建 25 件、375 CNY 的草稿，回读记录符合需求。
- 用基线的同一观察数据回放规则，仍选择 Harbor。见 `evaluation/h03-attribution.json`。

因此可以说明这一个案例的规则贡献；不能由单次 20 题对照推出普遍提升比例、显著性或生产错误率。延迟均值本次为 51.92 秒与 47.51 秒，但受模型随机性影响，不宣称规则本身带来提速。

## 个人能力与上游分工

上游 Skyvern 提供浏览器 Agent、模型适配、通用任务/工作流与原有 UI。个人增量是采购领域模型、计价与约束、来源一致性、业务状态、幂等意图、草稿核验、恢复对账、工作台和评测。具体文件见根目录 `PROCUREMENT.md`。

能展示的能力：Python 服务与异步任务、结构化模型输出、模型/规则分工、外部系统副作用处理、业务验收和实验归因。业务编排目前是固定流程，页面理解与操作由上游 Agent 完成；不写成自研通用规划内核。

## 可使用的项目表述

项目名：**采购执行与核验智能体（ProcureFlow）**，基于 Skyvern 构建的个人开源项目。

- 基于 Skyvern 构建采购执行工作台，串联供应商报价采集、采购条件决策、浏览器填写草稿与后台记录核验，保留业务事件和上游任务关联。
- 使用 Decimal 处理箱/件计价与采购金额，结合币种、库存、交期和报价有效期约束；实现业务意图去重、冲突拒绝及已有草稿对账恢复。
- 建立 34 项合同与 HTTP 集成测试，以及 20 个合成保留任务的真实模型对照；本次模型选择组 19/20、业务规则组 20/20，保留整箱约束错误及逐任务证据。

面试时应能亲自解释代码并演示：为什么用浏览器、什么时候 API 更合适；模型完成为何不等于业务完成；请求幂等和业务记录唯一性有什么区别；为什么单个正确案例不能证明整体改进。

## 仍需保留的限制

1. 尚无真实企业采购接入、真实用户、外部站点迁移或生产并发结果。
2. 单进程 SQLite、两个任务槽；中断任务需要显式恢复，不是自动分布式容灾。
3. 来源 API 是合成夹具的一部分。真实部署需要可信来源及权限适配，尚未证明比 API 或固定 Playwright 脚本更好。
4. 未实现长期记忆、技能自学习、RSI、模型训练与多智能体协作。
5. 上游镜像版本标 1.0.57，运行摘要已固定；开发提交与镜像的部分源码摘要不同，不能称字节一致。个人扩展来自本仓库挂载代码。
6. 已导出步骤 token 计数；上游自定义模型费用值未校准，记录为未知，不把零解释成免费。
7. `pre-commit run --all-files` 在隔离工作区实际尝试但未全通过：主机拦截导致上游文件缺失，Windows 缺少部分 POSIX API/bash，另有上游格式修正。没有把隔离检查产生的无关修改带回项目，也不声称全仓检查全绿。

本轮保留任务冻结实现后运行。随后只进行了局部变量类型收敛和异常分支空值处理，并重新通过类型/合同检查及一次真实 UI 全链路测试；没有使用保留题调业务选择规则。

## 复核命令

```text
python -m pytest -c procurement/tests/pytest.ini procurement/tests -q
ruff check procurement scripts/configure_procurement.py scripts/evaluate_procurement.py scripts/export_procurement_metrics.py
python -m mypy procurement/agent.py procurement/domain.py procurement/store.py procurement/server.py procurement/fixtures.py --follow-imports=silent --ignore-missing-imports
python scripts/evaluate_procurement.py --split development
python scripts/evaluate_procurement.py --split heldout
python scripts/export_procurement_metrics.py
```

真实模型评测会产生 API 费用。原始结果保留，重跑创建新目录；不能用重跑中最好的一次替换历史结果。

## 代码与历史

[GitHub 私有仓库](https://github.com/lazy0622/procureflow-agent)。为避免浅克隆祖先对象缺失，发布历史先导入有明确署名与许可的 Skyvern 固定快照，再单独提交个人扩展，可直接查看二者差异。本地原开发分支仍保留。

新仓库没有启用继承的上游发布自动化，当前验证为本地执行。密钥、模型配置、运行数据库和原始浏览器目录均未提交。

## 下一轮优先项

扩大不同商品、供应商和页面的任务集，并增加多次试验；在明确授权的真实站点验证迁移。只有出现可重复的流程复用收益后，再考虑记忆或技能学习。当前无需为了热词继续堆框架。
