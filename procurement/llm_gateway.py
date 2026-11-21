"""Pass-through model endpoint that applies DeepSeek's reasoning effort to browser tasks.

Skyvern calls OpenAI-compatible models through LiteLLM with drop_params=True, and LiteLLM
does not list reasoning_effort for an unrecognised OpenAI-compatible model, so the setting
is removed before the request leaves Skyvern. Pointing Skyvern's API base here adds the
field before forwarding. The caller's Authorization header is forwarded unchanged; this
process stores no provider key. Each call appends latency, token counts and the purchase
reference found in the prompt to llm-calls.jsonl in the data directory; prompt and
response content are not stored.
"""

import json
import os
import re
import time
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

EFFORTS = {"low", "high", "max"}
# Prompts embed the purchase request or plan as JSON, possibly inside an escaped string.
REFERENCE = re.compile(r'reference\\*"\s*:\s*\\*"([A-Za-z0-9._-]+)')
router = APIRouter()


@router.post("/llm/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Forward one non-streaming chat completion, setting reasoning_effort when configured."""
    body = await request.json()
    if body.get("stream"):
        raise HTTPException(400, "Streaming is not supported by this endpoint")
    effort = os.environ.get("PROCUREMENT_REASONING_EFFORT", "")
    if effort:
        if effort not in EFFORTS:
            raise HTTPException(500, "PROCUREMENT_REASONING_EFFORT must be low, high or max")
        body["reasoning_effort"] = effort
    upstream = os.environ.get("PROCUREMENT_LLM_UPSTREAM", "https://api.deepseek.com/v1")
    started_at = datetime.now(UTC).isoformat()
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
        response = await client.post(
            upstream.rstrip("/") + "/chat/completions",
            json=body,
            headers={"Authorization": request.headers.get("authorization", "")},
        )
    record_call(request, body, response, started_at, time.monotonic() - started)
    return Response(
        response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )


def record_call(
    request: Request, body: dict, response: httpx.Response, started_at: str, seconds: float
) -> None:
    """Append one metadata line; never the prompt or the answer."""
    try:
        usage = response.json().get("usage") or {}
    except ValueError:
        usage = {}
    match = REFERENCE.search(json.dumps(body.get("messages", [])))
    line = {
        "started_at": started_at,
        "seconds": round(seconds, 3),
        "status": response.status_code,
        "reasoning_effort": body.get("reasoning_effort"),
        "reference": match.group(1) if match else None,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
    }
    path = request.app.state.data_dir / "llm-calls.jsonl"
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(line) + "\n")
