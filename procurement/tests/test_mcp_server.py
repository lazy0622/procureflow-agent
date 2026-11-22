"""MCP tools over an in-process workbench with a fake browser; no LLM is called."""

import asyncio
import json

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from procurement.domain import PurchasePlan, plan_digest
from procurement.fixtures import offers_for
from procurement.mcp_server import build_server
from procurement.server import create_app

RealAsyncClient = httpx.AsyncClient
REQUEST = {"reference": "PO-mcp", "sku": "BRG-6204", "quantity": 20, "max_lead_days": 5}


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    """Workbench app whose browser collects the fixture offers and saves the plan."""
    from procurement.agent import SkyvernClient

    app = create_app(tmp_path)
    browser_runs = []

    def in_process():
        return RealAsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://workbench")

    async def fake_run(self, prompt, url, schema=None, max_steps=8):
        browser_runs.append(url)
        if "/suppliers" in url:
            output = {"offers": offers_for("standard"), "selected_quote_id": None, "explanation": ""}
            return {"run_id": "tsk_collect", "status": "completed", "output": output}
        plan = json.loads(prompt.split("values: ", 1)[1].split(". Do not pay", 1)[0])
        digest = plan_digest(PurchasePlan.model_validate(plan))
        app.state.store.create_authorized_draft(plan, digest, False)
        return {"run_id": "tsk_write", "status": "completed", "output": None}

    # The agent re-reads the source API before writing; send it to the same app.
    monkeypatch.setattr("procurement.agent.httpx.AsyncClient", lambda *a, **k: in_process())
    monkeypatch.setattr(SkyvernClient, "run", fake_run)
    monkeypatch.setenv("PROCUREMENT_APPROVER_TOKENS", "alice=approver-secret")
    return app, in_process, browser_runs


def structured(result):
    assert not result.isError, result.content
    return result.structuredContent.get("result", result.structuredContent)


@pytest.mark.asyncio
async def test_readonly_mode_exposes_only_read_tools(workbench):
    _, client, _ = workbench
    server = build_server("http://workbench", "readonly", client)
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        assert sorted(t.name for t in tools) == ["get_draft", "get_purchase", "list_offers"]
        assert all(t.annotations.readOnlyHint for t in tools)
        offers = structured(await session.call_tool("list_offers", {}))
        assert {o["quote_id"] for o in offers} == {o["quote_id"] for o in offers_for("standard")}
        missing = await session.call_tool("get_purchase", {"reference": "PO-none"})
        assert missing.isError


@pytest.mark.asyncio
async def test_propose_stops_for_a_person_and_is_idempotent(workbench):
    app, client, browser_runs = workbench
    server = build_server("http://workbench", "propose", client, poll_seconds=0.01)
    async with create_connected_server_and_client_session(server) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert "propose_purchase" in tools and not any("confirm" in n for n in tools)
        assert tools["propose_purchase"].annotations.readOnlyHint is False

        first = structured(await session.call_tool("propose_purchase", REQUEST))
        assert first["status"] == "awaiting_confirmation"
        assert first["review_url"] == "http://workbench/?reference=PO-mcp"
        assert first["plan"]["quote_id"] == "Q-NORTH-01"
        assert (await session.call_tool("get_draft", {"reference": "PO-mcp"})).isError

        again = structured(await session.call_tool("propose_purchase", REQUEST))
        assert again["reused_existing_task"] is True and again["plan"] == first["plan"]
        conflict = await session.call_tool("propose_purchase", {**REQUEST, "quantity": 30})
        assert conflict.isError and "different request" in conflict.content[0].text
        assert len(browser_runs) == 1  # collection only; no write before approval

        # The person confirms in the workbench, outside the MCP server.
        job = app.state.store.job("PO-mcp")
        async with client() as http:
            confirm = await http.post(
                "/api/jobs/PO-mcp/confirm",
                json={"plan_hash": job["result"]["plan_hash"]},
                headers={"Authorization": "Bearer approver-secret"},
            )
            assert confirm.status_code == 202
        for _ in range(200):
            done = structured(await session.call_tool("get_purchase", {"reference": "PO-mcp"}))
            if done["status"] == "verified":
                break
            await asyncio.sleep(0.01)
        assert done["status"] == "verified" and done["errors"] == []
        draft = structured(await session.call_tool("get_draft", {"reference": "PO-mcp"}))
        assert draft["quote_id"] == "Q-NORTH-01" and draft["quantity"] == 20


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        build_server("http://workbench", "admin")
