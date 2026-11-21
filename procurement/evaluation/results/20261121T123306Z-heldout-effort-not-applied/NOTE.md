# 未生效的推理强度试验

这次运行把 Skyvern 的 OPENAI_COMPATIBLE_REASONING_EFFORT 设成 low，但 LiteLLM 以 drop_params=True 调用 OpenAI 兼容模型时会静默删除 reasoning_effort，请求实际仍按 DeepSeek 默认的 high 执行。发现后中断，保留 10 条记录作为排查证据，不计入任何统计。修复方式见 procurement/llm_gateway.py。
