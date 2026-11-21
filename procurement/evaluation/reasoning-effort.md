# 推理强度对照：low 与 high

日期：2026-11-21。模型 `deepseek-flash`，20 道保留题，两种选报价策略，每轮 40 次作业。DeepSeek 默认推理强度是 high。

## 起因

延迟拆解显示，写草稿的作业里浏览器填写占 71% 耗时，填写阶段输出 token 一半是推理 token。假设：调低推理强度能缩短填写时间、降低成本，且不影响准确率。

## 第一次尝试没有生效

把 Skyvern 的 `OPENAI_COMPATIBLE_REASONING_EFFORT` 设为 `low` 后，前 10 次作业的 token 计数和以前一样。排查过程：

1. 在 Skyvern 容器里确认配置已读入：`LLMConfig.reasoning_effort == "low"`，请求参数里也有 `reasoning_effort`。
2. 直接用 LiteLLM 发同一个请求，报 `UnsupportedParamsError`：LiteLLM 不认为这个 OpenAI 兼容模型支持 `reasoning_effort`。
3. Skyvern 调用时带 `drop_params=True`，所以这个参数被静默删除，请求实际按默认 high 执行。

这一轮中断作废，记录保留在 `results/20261121T123306Z-heldout-effort-not-applied/`，不计入统计。

## 修复

工作台新增 `/llm/v1/chat/completions` 转发接口（`procurement/llm_gateway.py`）。Skyvern 的模型地址指向它，它按 `PROCUREMENT_REASONING_EFFORT` 补上参数后转发给 DeepSeek。调用方的鉴权头原样转发，接口本身不保存密钥。每次调用在 `.local/business/llm-calls.jsonl` 记一行耗时、token 和单号，不记录提示词和回答。

直接对比同一提示：不经转发接口的 `low` 请求输出 1186 token（推理 988），经转发接口的输出 759 token（推理 543），确认参数生效。

## 结果

| 轮次 | 推理强度 | 模型直接选 | 业务规则 | 填写均值 | 填写推理 token |
| --- | --- | ---: | ---: | ---: | ---: |
| 20261111T195306Z | high | 19/20 | 20/20 | 46.0s | 2433 |
| 20261120T164524Z | high | 19/20 | 20/20 | 44.5s | 2734 |
| 20261120T170405Z | high | 19/20 | 20/20 | 42.0s | 2685 |
| 20261121T131808Z | high（经转发接口显式设置） | 19/20 | 20/20 | 50.3s | 2607 |
| 20261121T124140Z | low | 19/20 | 20/20 | 43.9s | 2216 |
| 20261121T130012Z | low | 19/20 | 20/20 | 50.3s | 2126 |

同一晚上 high 一轮与 low 两轮相比：填写阶段推理 token 少 16.7%，输出 token 少 7.7%，单次模型调用耗时 6.93s → 6.09s，单次作业成本（高峰价）0.0106 → 0.0100 美元。两种强度下 h03 都错，其余题全对。

## 为什么填写没有变快

用转发接口的调用日志和 Skyvern 动作时间戳拆分一次约 50s 的填写任务（`step-timing-*.json`）：

| 部分 | high | low |
| --- | ---: | ---: |
| 模型调用（约 3 次） | 27.7s（55%） | 25.1s（50%） |
| 浏览器动作（约 9 个） | 13.0s（26%） | 13.9s（28%） |
| 截图、抓取页面、步骤切换 | 9.6s（19%） | 11.4s（23%） |

模型只占一半时间，推理强度只能缩短这一半里的一部分。填写均值在轮次之间从 42.0s 到 50.3s 波动，比推理强度带来的差别大，所以这组数据不能证明提速。

## 结论与下一步

- `low` 在这 20 题上没有损失准确率，稳定省下约 17% 推理 token、约 6% 成本。
- 填写耗时的瓶颈在模型调用次数和逐字段的浏览器动作，推理强度不是主要因素。下一步应减少调用和动作次数，例如一次调用填完整张表，或在目标系统有 API 时直接调 API。
- 默认配置不变；需要省成本时设置 `PROCUREMENT_LLM_API_BASE=http://workbench:8091/llm/v1` 和 `PROCUREMENT_REASONING_EFFORT=low`。
- 样本是 20 题、每种强度 2 到 4 轮，结论只覆盖这个合成环境。

## 复现

```powershell
$env:PROCUREMENT_LLM_API_BASE = "http://workbench:8091/llm/v1"
$env:PROCUREMENT_REASONING_EFFORT = "low"
docker compose -f compose.procurement.yml up -d --no-deps --force-recreate workbench skyvern
python scripts/evaluate_procurement.py --split heldout
python scripts/summarize_repeats.py --suffix=-effort-low
python scripts/analyze_latency_cost.py --runs "*-heldout-effort-low" --out latency-cost-effort-low.json
python scripts/analyze_step_timing.py --run <结果目录名>
```

评测脚本从运行中的容器读取实际生效的推理强度，非默认值的结果目录带 `-effort-low` 后缀。
