# 验收记录

首次验收：2026-11-12。更新：2026-11-13，增加三种实现对照、外部站点只读验证和写前人工确认；2026-11-21，增加保留任务三次重复、分阶段延迟与成本、进程崩溃恢复测试。

验收范围是可复现的合成采购业务：三家合成供应商、一个商品族、一个本地采购后台。结果不代表接入真实企业系统后的表现。

## 结果

| 验收项 | 实际结果 | 证据 |
| --- | --- | --- |
| 完整业务链路 | DeepSeek + Skyvern 采集报价、填写草稿、回读记录均跑通 | `evaluation/baseline/full-normal.json`、`evaluation/ui-smoke.json` |
| 合同与 HTTP 集成测试 | 36 项通过 | `tests/` |
| 核心模块类型检查 | 5 个业务源码文件通过 mypy | 复核命令见下 |
| 静态检查 | Ruff 通过 | 复核命令见下 |
| 开发任务 | 4 个任务，各策略一次；两组均 4/4 | `evaluation/results/20261111T194808Z-development/` |
| 保留任务 | 20 个合成变体，各策略一次；模型直接选报价 19/20，业务规则选报价 20/20 | `evaluation/results/20261111T195306Z-heldout/` |
| 保留任务三次独立重复 | 每题每策略 3 次，共 120 次作业：模型直接选报价 57/60，pass^3 = 0.95（h03 三次全错）；业务规则选报价 60/60，pass^3 = 1.0 | `evaluation/heldout-repeats.json` |
| 分阶段延迟与成本 | 128 次真实作业，单次均值 48.6s、p95 79.1s；写草稿的作业中浏览器填写占 70.7%、报价采集占 20.7%；按 DeepSeek 公开价估算每次 0.0107 美元（高峰）/ 0.0054 美元（低谷） | `evaluation/latency-cost.json` |
| 进程崩溃恢复 | 在 3 个位置直接杀掉工作台进程（退出码 70），重启后任务为 `interrupted`，恢复后均为 `verified`，每个单号恰好 1 张草稿 | `evaluation/crash-recovery.json` |
| 三种实现对照 | 4 个已知任务各一次：直接调 API 4/4（均值 0.145s），固定选择器 Playwright 3/4（2.496s，卡片布局超时），Skyvern 4/4（66.004s） | `evaluation/results/20261113T121839Z-simple-baselines/` |
| 外部站点只读抽取 | books.toscrape.com 两个分类，对照实时 HTML：固定选择器 Playwright 0/2，Skyvern 2/2（均值 18.148s），无写入 | `evaluation/results/20261113T122452Z-external-sites/` |
| 写前人工确认 | 任务停在待确认，草稿未创建；界面确认后继续写入并通过核验（`PO-e8b5237f`，草稿 49），无控制台错误 | `evaluation/confirmation-ui-smoke.json` |
| 确认后报价变化 | 写前重读报价，摘要不一致时阻断写入并标记 `quote_changed_after_confirmation` | `tests/test_business.py` |
| 重复提交 | 16 个等价 HTTP 请求全部复用原任务，事件数不增加、草稿编号不变；需求冲突返回 409 | `evaluation/http-replay.json` |
| 故障与恢复 | 保存后返回 503 的真实浏览器任务通过；服务重启后对账恢复有集成测试，并有上面的真实崩溃测试 | 开发任务 response-loss、`tests/test_business.py` |
| 页面 | 工作台表单真实提交完成；桌面和移动端初始布局无 JS 错误，移动端无横向溢出 | `evaluation/ui-smoke.json`、`evaluation/assets/` |
| 上游 UI 与截图访问 | 本地代理后诊断 403 消除；截图代理 200，越界路径 403 | `evaluation/ui-smoke.json`、`evaluation/artifact-access.json` |

20 个保留任务是同一合成环境中的参数与布局变体。40 次采购作业不等于 40 次模型调用：一次作业包含采集和填写两个 Skyvern 任务，每个任务又会多次调用模型。

## 失败案例：h03 整箱约束

任务要求采购 25 件。Northline 每箱 10 件、每箱 120 CNY；Harbor 按件 15 CNY。

- 两种策略各一次真实运行，抽取的报价内容规范化后相同，模型在两次运行中都选择了 Northline。
- 模型直接选报价时，创建了 25 件、300 CNY 的草稿，相当于 2.5 箱，违反整箱采购约束。
- 业务规则把该报价标为 `fractional_pack`，改用 Harbor，创建 25 件、375 CNY 的草稿，回读记录符合需求。
- 用同一份观察数据回放规则，仍然选择 Harbor。见 `evaluation/h03-attribution.json`。

这个案例能说明整箱规则的作用。单次 20 题对照不能推出普遍的提升比例或生产错误率。两组平均耗时为 51.92 秒和 47.51 秒，差异受模型随机性影响，不归因于规则。

三次重复后，模型直接选报价在 h03 上 0/3，其余 19 题 3/3；业务规则 20 题全部 3/3。h03 的错误是稳定复现的决策错误，不是偶发波动。

## 延迟与成本拆解

数据来自全部开发与保留任务的事件时间戳和 Skyvern 步骤 token 计数，不额外调用模型。

| 阶段 | 次数 | 均值耗时 | p95 | 平均步数 | 平均输入 / 输出 token |
| --- | --- | --- | --- | --- | --- |
| 报价采集 | 128 | 12.5s | 16.6s | 1 | 3845 / 1687 |
| 浏览器填写草稿 | 90 | 44.3s | 64.0s | 2.07 | 14776 / 5265 |

被业务规则提前拦下的 38 次作业不进入填写阶段。填写阶段输出中 50% 是推理 token，是延迟和费用的主要来源。下一步可以单独调低填写阶段的推理强度并重新跑保留任务对比，目前尚未实施。成本按公开价格估算，忽略缓存折扣，没有与账单核对。

## 崩溃恢复测试

`scripts/crash_recovery_test.py` 通过 `PROCUREMENT_CRASH_AT` 让工作台在指定位置 `os._exit(70)`，模拟进程被杀，不执行任何清理。每个位置一次真实模型作业。

| 崩溃位置 | 模拟情形 | 恢复路径 | 结果 |
| --- | --- | --- | --- |
| `after_draft_commit` | 后台已保存草稿，响应未返回 | 恢复时查到已有草稿，对账后核验 | `verified`，草稿 1 张 |
| `before_read_back` | 浏览器填写完成，尚未回读 | 同上 | `verified`，草稿 1 张 |
| `before_browser_write` | 人工已确认，尚未写入 | 重新校验已确认的计划摘要后写入 | `verified`，草稿 1 张 |

每个案例恢复后再等 45 秒复查草稿数，确认没有遗留浏览器任务补写重复草稿。测试只有单机一次运行，不等于分布式容灾。

## 三种实现对照的结论

目标系统有完整 API 时，直接调 API 最快也最稳。固定选择器脚本在页面结构变化时失效。Skyvern 不需要为每种布局写选择器，代价是单任务耗时高一到两个数量级。浏览器 Agent 适合没有 API、页面结构不稳定的来源。

## 上游与本项目分工

Skyvern 提供浏览器 Agent、模型适配、通用任务与工作流以及原有 UI。本项目实现采购领域模型、计价与约束、来源一致性核对、业务状态、幂等意图、写前确认、草稿核验、恢复对账、工作台和评测。文件对应关系见根目录 `PROCUREMENT.md`。

业务编排是固定的“采集—决策—确认—写入—核验”流程，页面理解与操作由 Skyvern 完成。

## 限制

1. 没有真实企业采购系统接入、真实用户或生产并发结果。
2. 单进程 SQLite，两个任务槽；中断任务需要显式恢复，不是分布式容灾。
3. 来源 API 是合成环境的一部分。真实部署需要可信来源和权限适配。
4. 没有实现长期记忆、技能学习或模型训练。
5. 上游镜像版本标为 1.0.57，运行时以固定的镜像摘要为准；开发提交与镜像中部分源码的字节摘要不同。业务代码由本仓库 `procurement/` 挂载进容器。
6. 已导出步骤 token 计数并按公开价估算成本；上游对自定义模型的费用值未校准，估算没有与账单核对。
7. 全仓 `pre-commit run --all-files` 在 Windows 上未全部通过：本机安全软件移除了一个上游文件，部分钩子依赖 POSIX 环境，另有上游格式问题。不声称全仓检查通过。

保留任务在实现冻结后运行。之后只做了类型收敛和异常分支空值处理，并重新通过类型检查、合同测试和一次真实 UI 全链路测试，没有用保留题调整选择规则。

## 复核命令

```text
python -m pytest -c procurement/tests/pytest.ini procurement/tests -q
ruff check procurement scripts/configure_procurement.py scripts/evaluate_procurement.py scripts/export_procurement_metrics.py
python -m mypy procurement/agent.py procurement/domain.py procurement/store.py procurement/server.py procurement/fixtures.py --follow-imports=silent --ignore-missing-imports
python scripts/evaluate_procurement.py --split development
python scripts/evaluate_procurement.py --split heldout
python scripts/compare_procurement_baselines.py
python scripts/evaluate_external_sites.py
python scripts/export_procurement_metrics.py
python scripts/summarize_repeats.py
python scripts/analyze_latency_cost.py
python scripts/crash_recovery_test.py
```

真实模型评测会产生 API 费用。原始结果保留，重跑会创建新目录，不用重跑中最好的一次替换历史结果。

## 代码历史

仓库先导入带署名与许可的 Skyvern 固定快照，再单独提交本项目的扩展，可以直接查看两者差异。上游的发布自动化没有启用，验证均在本地执行。密钥、模型配置、运行数据库和原始浏览器目录未提交。

## 下一步

扩大商品、供应商和页面类型，增加重复试验；在明确授权的真实站点上验证迁移。出现可重复的流程复用收益后，再考虑记忆或技能学习。
