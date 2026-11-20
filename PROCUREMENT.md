# ProcureFlow · 报价采集与采购草稿核验 Agent

基于 Skyvern `26d2722b0fc9c7f3246389bdf7385ef18862b242` 构建的个人项目。
保留 AGPL-3.0 与上游署名。Skyvern 提供浏览器执行、任务 API、工作流和原有界面。
本项目的业务扩展位于 `procurement/`，开发过程及实测结论按下表更新。

## 当前目标与验收

用户输入采购规格、数量和交付要求，Agent 读取供应商报价，形成对比结果，
在本地演示采购后台建立草稿，并核对业务记录。所有供应商与交易数据均为合成演示数据。

1. 原始 Skyvern 服务和 UI 可运行，真实模型完成受控页面任务。
2. 个人实现的报价口径、结果核验和业务意图对账有对应代码与测试。
3. 完整业务任务形成输入、轨迹、目标记录和验收结果，不能只看 Agent 结束状态。
4. 开发任务与独立任务分开；真实模型、模拟输出和故障注入分别统计。
5. 公开保存上游/个人贡献、运行方式、已知失败和面试说明；无未测提升数字。

## 状态

| 阶段 | 状态 | 证据 |
| --- | --- | --- |
| 固定上游与依赖 | Docker 后端健康；镜像已固定摘要 | `compose.procurement.yml`、`evaluation/provenance.json` |
| 真实模型基线 | 图片、页面抽取、完整采购任务已运行 | `procurement/evaluation/baseline/` |
| 个人业务扩展 | 报价规则、草稿核验、意图去重与恢复已实现 | `procurement/`；34 项无模型测试通过 |
| 开发任务与故障验证 | 4 个开发任务 × 2 策略均符合预期 | `procurement/evaluation/results/20261111T194808Z-development/` |
| 独立任务验证 | 20 个保留任务 × 2 策略完成；19/20 对 20/20 | `procurement/evaluation/results/20261111T195306Z-heldout/` |
| 简历就绪审查 | 可作为合成业务环境的个人项目展示 | 完整边界见 `procurement/RESUME_READINESS.md` |

本地配置 `.env`、`.local/`、浏览器轨迹和运行数据库不提交 Git。

原始上游说明见 [UPSTREAM_README.md](UPSTREAM_README.md)。

## 本地启动

需要 Docker Desktop（Linux 容器）及模型配置文件。模型调用使用 DeepSeek Flash 的
图片能力；配置文件和生成的 API 凭据不进入 Git。

```powershell
# 在项目根目录运行；从本机文件读取密钥，不把密钥放进命令行。
python scripts/configure_procurement.py --model-config <你的本地模型配置文件>
./scripts/start-procurement.ps1
```

工作台：<http://127.0.0.1:8091/>。Skyvern 健康检查：
<http://127.0.0.1:8020/api/v1/heartbeat>。

第一次启动需要下载上游镜像并初始化独立数据库。镜像较大；下载完成不代表模型任务通过。
修改 `procurement/` 的 Python 代码后，需要 `docker compose -f compose.procurement.yml restart workbench`。
停止当前项目用 `docker compose -f compose.procurement.yml stop`，无需停止其他容器。

可选运行完整上游 UI（本机还需 Node.js；首轮在 `skyvern-frontend` 执行 `npm ci`）：

```powershell
./scripts/start-procurement.ps1 -WithUpstreamUI
```

上游 UI：<http://127.0.0.1:8080/>，用于查看任务、轨迹和截图；自己的业务工作台在 8091。
可选 UI 使用共享容器网络命名空间的本地代理（8021），让上游的 localhost 诊断仍从容器回环访问；
没有修改上游的本地访问检查，所有宿主机端口仍绑定 127.0.0.1。
主机曾出现火绒移除上游 `block.py` 的情况，本方案运行容器内的官方代码，
没有要求关闭防护或将整个项目加入信任区。

## 架构与个人贡献

```mermaid
flowchart LR
    U[采购需求] --> B[业务编排]
    B --> E[Skyvern 页面抽取]
    E --> Q[报价口径与采购条件]
    Q --> S[来源回读]
    S --> A[Skyvern 填写草稿]
    A --> ERP[合成采购后台]
    ERP --> V[业务记录核验]
    V --> R[通过或待复核]
```

| 能力 | 归属与位置 |
| --- | --- |
| 模型/浏览器循环、原有任务管理、通用重试及 UI | 上游 Skyvern；不声称个人从零实现 |
| 箱/件换算、币种、库存、交期、报价有效期 | `procurement/domain.py` |
| 业务意图去重、冲突拒绝、草稿记录及阶段事件 | `procurement/store.py` |
| 页面采集、来源核对、填写草稿、回读对账与恢复 | `procurement/agent.py` |
| 业务页面、合成 ERP、响应丢失故障场景 | `procurement/server.py`、`fixtures.py`、`web/` |
| 无模型合同测试与真实任务评测 | `procurement/tests/`、`evaluation/` |

一个实际基线问题：现代任务接口在指定 URL 时只设置导航目标，虽然传了抽取 schema，
任务可能结束却没有抽取结果。本项目的适配器对抽取调用显式提供抽取目标的接口，
保留空输出的初次记录，并增加 API 合同回归。不能把任务 completed 直接当作数据已交付。

## 验证

本机可用 Python 3.12 + 项目 server 依赖，以及 pytest / pytest-asyncio / ruff：

```powershell
python -m pytest -c procurement/tests/pytest.ini procurement/tests -q
ruff check procurement scripts/configure_procurement.py scripts/evaluate_procurement.py
python scripts/evaluate_procurement.py --split development
# 会调用真实模型并发生 API 费用；保留任务冻结后再运行。
python scripts/evaluate_procurement.py --split heldout
```

评测器使用固定预期报价/金额与后台记录判定，标准答案不放入模型提示。
`model_only` 使用模型的报价选择；`verified` 使用业务规则和来源核对。
两者都真实调用 Skyvern 浏览器并创建草稿，不能把普通脚本用例计作模型任务。
开发任务当前两策略均 4/4，因此**不能声称代码核验已经带来准确率提升**。
保留任务单次对照为模型选择 19/20、业务规则版本 20/20；其中数量 25 件的任务，
模型选择了每箱 10 件的报价，规则版本拒绝非整箱采购并改选按件报价。
这个差异仅属于当前 20 个合成参数变体，不代表真实采购系统或通用 Agent 的准确率。

### 简单方案对照、外部只读验证与人工确认门禁（2026-11-13）

- **简单方案对照**（`scripts/compare_procurement_baselines.py`，4 个已知任务各 1 次）：`api` 4/4（均值 0.145s）、固定选择器 `playwright` 3/4（均值 2.496s，在 `cards` 卡片布局超时失败）、`skyvern` 4/4（均值 66.004s）。API 完备时应优先用 API；Skyvern 价值在于无专用选择器适配布局变化，而非速度。
- **外部公开站点只读验证**（`scripts/evaluate_external_sites.py`，`books.toscrape.com` 两类目共 10 本书对照实时 HTML）：固定表格选择器 `playwright_fixed_table` 0/2（等待 `table` 超时），`skyvern` 2/2（均值 18.148s，无写入）。
- **人工确认门禁与任务遥测**：`PurchaseRequest.confirm_before_write=True` 会在浏览器写草稿前停在 `awaiting_confirmation` 并计算 `plan_hash`；`POST /api/jobs/{reference}/confirm` 原子校验摘要后放行，若写前重读报价变化则记录 `quote_changed_after_confirmation` 并阻断写入。`scripts/export_procurement_metrics.py` 另汇总延迟分位数与失败阶段到 `procurement/evaluation/task-telemetry.json`。

## 当前限制

- 合成环境、三个供应商、一个示例商品族；不代表已接入企业采购系统或真实用户。
- 业务编排是固定的采集—决策—动作—核验流程，模型执行页面理解与操作；不是通用自主规划框架。
- 供应商公开源接口用于受控实验中的来源核对。真实系统需要独立的可信来源适配；
  对本来就有完整 API 的系统，应优先评估 API 方案，不证明浏览器优于 API/固定脚本。
- 当前单进程 SQLite 状态与两个并发任务槽；启动后把未完成任务标为 interrupted，
  由人工恢复并对账，不声称生产分布式恢复或任意外部系统 exactly-once。
- 开发源码固定提交与镜像均标 1.0.57，但关键模块字节摘要不同；运行以已固定镜像摘要为准。
  没有修改容器内上游内核，业务代码直接挂载本仓库 `procurement/`。
- 上游对自定义模型的成本值可能为零，不能解释成免费。实际 token 可另按步骤统计，
  未对齐服务商账单前不填写人民币成本或整体降本比例。
- 长期记忆、自动技能学习和 RSI 尚未实现，不写成已有功能。
