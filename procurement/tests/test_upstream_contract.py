"""Regression tests for the extraction endpoint discovered in the real baseline."""

import json

import httpx
import pytest

from procurement.agent import SkyvernClient


@pytest.mark.asyncio
@pytest.mark.parametrize("extract", [True, False])
async def test_navigation_and_extraction_use_their_actual_api_contracts(monkeypatch, extract):
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            if extract:
                assert request.url.path == "/api/v1/tasks"
                assert body["data_extraction_goal"] == "Read quotation"
                assert "navigation_goal" not in body
                assert request.headers["x-max-steps-override"] == "5"
                return httpx.Response(200, json={"task_id": "tsk_contract"})
            assert request.url.path == "/v1/run/tasks"
            assert body["prompt"] == "Read quotation"
            return httpx.Response(200, json={"run_id": "tsk_contract"})
        assert request.url.path == "/v1/runs/tsk_contract"
        return httpx.Response(
            200,
            json={
                "run_id": "tsk_contract",
                "status": "completed",
                "output": {"price": "120"},
                "step_count": 1,
            },
        )

    original = httpx.AsyncClient

    def isolated_client(**kwargs):
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setenv("SKYVERN_API_KEY", "unit-test-token")
    monkeypatch.setenv("SKYVERN_BASE_URL", "http://upstream.test/v1")
    monkeypatch.setattr(httpx, "AsyncClient", isolated_client)
    result = await SkyvernClient().run(
        "Read quotation",
        "http://supplier.test/",
        {"type": "object"} if extract else None,
        max_steps=5,
    )
    assert result["output"] == {"price": "120"}
    assert len(requests) == 2


def test_boolean_quantity_cannot_pass_record_verification():
    from procurement.domain import PurchasePlan, verify_draft

    plan = PurchasePlan(
        reference="PO-1",
        quote_id="q1",
        supplier="s1",
        sku="x1",
        quantity=1,
        currency="CNY",
        total="1.00",
    )
    assert "draft_quantity_mismatch" in verify_draft(
        plan, {**plan.model_dump(mode="json"), "quantity": True}
    )
