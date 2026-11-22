"""End-to-end: DeepSeek drives ProcureFlow through the MCP server over stdio.

1. readonly mode: check that no write tool is offered.
2. propose mode: give DeepSeek the MCP tools and a purchase request; it decides which
   tools to call. Expect it to stop at a plan awaiting confirmation.
3. This script then confirms in the workbench, standing in for the person, and the
   agent's tools read back the verified draft.
4. The same proposal is sent again through MCP; it must reuse the task. A changed
   quantity under the same reference must be refused.

Uses the running Docker workbench and real Skyvern + DeepSeek calls. Writes
procurement/evaluation/mcp-e2e.json.
"""

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8091"


def model_settings() -> tuple[str, str]:
    values = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values["OPENAI_COMPATIBLE_API_KEY"], values["OPENAI_COMPATIBLE_API_BASE"]


def server(mode: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "procurement.mcp_server"],
        cwd=str(ROOT),
        env={"PROCUREFLOW_MCP_MODE": mode, "PROCUREFLOW_URL": URL, "PYTHONPATH": str(ROOT)},
    )


def text(result) -> str:
    return "\n".join(part.text for part in result.content if getattr(part, "text", None))


async def agent_turn(session: ClientSession, instructions: str, task: str) -> dict:
    """A minimal tool-calling loop: MCP tool schemas become chat-completion tools."""
    key, base = model_settings()
    tools = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }
        for tool in (await session.list_tools()).tools
    ]
    messages = [{"role": "system", "content": instructions}, {"role": "user", "content": task}]
    calls = []
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
        for _ in range(8):
            response = await client.post(
                base.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={"model": "deepseek-flash", "messages": messages, "tools": tools},
            )
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            messages.append(message)
            if not message.get("tool_calls"):
                return {"tool_calls": calls, "final_answer": message.get("content")}
            for call in message["tool_calls"]:
                arguments = json.loads(call["function"]["arguments"] or "{}")
                started = time.monotonic()
                result = await session.call_tool(call["function"]["name"], arguments)
                calls.append(
                    {
                        "tool": call["function"]["name"],
                        "arguments": arguments,
                        "is_error": result.isError,
                        "seconds": round(time.monotonic() - started, 1),
                        "result": result.structuredContent or text(result),
                    }
                )
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": text(result)})
    return {"tool_calls": calls, "final_answer": None, "note": "round budget exhausted"}


async def main() -> None:
    reference = "PO-MCP-" + datetime.now(UTC).strftime("%H%M%S")
    report: dict = {"reference": reference, "run_at": datetime.now(UTC).isoformat()}

    async with stdio_client(server("readonly")) as streams, ClientSession(*streams) as session:
        await session.initialize()
        report["readonly_tools"] = sorted(t.name for t in (await session.list_tools()).tools)

    async with stdio_client(server("propose")) as streams, ClientSession(*streams) as session:
        init = await session.initialize()
        tools = (await session.list_tools()).tools
        report["propose_tools"] = {t.name: t.annotations.model_dump(exclude_none=True) for t in tools}
        task = (
            "Buy 25 pieces of BRG-6204 in CNY, delivered within 5 days. "
            f"Use purchase reference {reference}. Tell me the outcome."
        )
        report["agent"] = await agent_turn(session, init.instructions or "", task)
        proposals = [c for c in report["agent"]["tool_calls"] if c["tool"] == "propose_purchase"]

        async with httpx.AsyncClient(base_url=URL, timeout=30, trust_env=False) as http:
            job = (await http.get(f"/api/jobs/{reference}")).json()
            report["status_after_agent"] = job.get("status")
            report["draft_before_confirmation"] = (await http.get(f"/api/drafts/{reference}")).status_code
            if job.get("status") == "awaiting_confirmation":
                # The person's approval, done in the workbench and outside MCP.
                confirm = await http.post(
                    f"/api/jobs/{reference}/confirm",
                    json={"plan_hash": job["result"]["plan_hash"]},
                    headers={
                        "Authorization": "Bearer "
                        + os.environ.get("PROCUREMENT_APPROVER_TOKENS", "").split(",")[0].partition("=")[2]
                    },
                )
                report["human_confirmation_status"] = confirm.status_code
        status = None
        for _ in range(90):
            status = await session.call_tool("get_purchase", {"reference": reference})
            if status.isError or status.structuredContent["status"] not in {
                "queued",
                "creating_draft",
            }:
                break
            await asyncio.sleep(2)
        report["after_confirmation"] = status.structuredContent if status else None
        draft = await session.call_tool("get_draft", {"reference": reference})
        report["draft"] = draft.structuredContent or text(draft)

        if proposals:
            arguments = proposals[0]["arguments"]
            repeat = await session.call_tool("propose_purchase", arguments)
            report["repeat_proposal"] = {
                "reused_existing_task": repeat.structuredContent["reused_existing_task"],
                "status": repeat.structuredContent["status"],
            }
            conflict = await session.call_tool("propose_purchase", {**arguments, "quantity": arguments["quantity"] + 1})
            report["conflicting_proposal"] = {"is_error": conflict.isError, "text": text(conflict)}

    after = report["after_confirmation"] or {}
    checks = {
        "readonly_has_no_write_tool": "propose_purchase" not in report["readonly_tools"],
        "no_confirm_tool": not any("confirm" in name for name in report["propose_tools"]),
        "agent_used_propose": bool(proposals),
        "stopped_before_write": report["status_after_agent"] == "awaiting_confirmation"
        and report["draft_before_confirmation"] == 404,
        "verified_after_person_confirmed": after.get("status") == "verified",
        "repeat_reused_task": report.get("repeat_proposal", {}).get("reused_existing_task") is True,
        "conflict_refused": report.get("conflicting_proposal", {}).get("is_error") is True,
    }
    report["checks"] = checks
    report["passed"] = all(checks.values())
    out = ROOT / "procurement/evaluation/mcp-e2e.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(checks, indent=2))
    print("final answer:", report["agent"]["final_answer"])


if __name__ == "__main__":
    asyncio.run(main())
