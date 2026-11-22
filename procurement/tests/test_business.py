"""Business contracts and failure boundaries; these tests do not call an LLM."""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from procurement.domain import PurchaseRequest, choose_offer, plan_digest, verify_draft
from procurement.fixtures import offers_for
from procurement.server import create_app
from procurement.store import IntentConflict, Store, WriteNotAuthorized

APPROVER = {"Authorization": "Bearer approver-secret"}


def request(**overrides):
    return PurchaseRequest.model_validate(
        {
            "reference": "PO-test",
            "sku": "BRG-6204",
            "quantity": 20,
            "max_lead_days": 5,
            **overrides,
        }
    )


def writing_job(store, confirmed=True, **overrides):
    """Put a task into creating_draft with its plan, optionally approved by a person."""
    purchase = request(**overrides)
    store.submit(purchase.reference, purchase.model_dump(mode="json"))
    plan, _ = choose_offer(purchase, offers_for("standard"))
    digest = plan_digest(plan)
    result = {"plan": plan.model_dump(mode="json"), "plan_hash": digest}
    if confirmed:
        result["confirmed_plan_hash"] = digest
    store.finish(purchase.reference, "creating_draft", result)
    return plan, digest


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("standard", "Q-NORTH-01"),
        ("expired", "Q-HARBOR-01"),
        ("shortage", None),
        ("mismatch", "Q-HARBOR-01"),
        ("missing_currency", "Q-HARBOR-01"),
    ],
)
def test_business_selection(scenario, expected):
    plan, _ = choose_offer(request(), offers_for(scenario))
    assert (plan.quote_id if plan else None) == expected
    if scenario == "standard":
        assert plan.total == Decimal("240.00")


def test_no_partial_box_purchase_or_currency_conversion():
    plan, decisions = choose_offer(request(quantity=15), offers_for("standard"))
    assert plan.quote_id == "Q-HARBOR-01"
    assert "fractional_pack" in decisions[0].reasons
    assert "currency_mismatch" in decisions[2].reasons


def test_expiry_and_lead_time_boundaries():
    offers = offers_for("standard")
    offers[0]["valid_until"] = "2026-11-11"
    plan, _ = choose_offer(request(max_lead_days=3), offers)
    assert plan.quote_id == "Q-NORTH-01"
    plan, _ = choose_offer(request(max_lead_days=1), offers)
    assert plan is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", "NaN"),
        ("price", "Infinity"),
        ("price", "-1"),
        ("pack_size", 0),
        ("pack_size", True),
        ("stock", -1),
    ],
)
def test_invalid_observation_is_not_eligible(field, value):
    offer = {**offers_for("standard")[0], field: value}
    plan, decisions = choose_offer(request(), [offer])
    assert plan is None
    assert decisions[0].reasons == ["invalid_observation"]


def test_duplicate_quote_identity_requires_review():
    offer = offers_for("standard")[0]
    plan, decisions = choose_offer(request(), [offer, offer])
    assert plan is None
    assert decisions[-1].reasons == ["duplicate_quote_identity"]


@pytest.mark.parametrize("quantity", [True, 0, -1, 1.5, "20"])
def test_request_does_not_coerce_invalid_quantity(quantity):
    with pytest.raises(ValidationError):
        request(quantity=quantity)


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", 21),
        ("currency", "USD"),
        ("sku", "BRG-6205"),
        ("total", "NaN"),
        ("total", "241.00"),
        ("reference", "different"),
    ],
)
def test_record_readback_is_independent_of_task_success(field, value):
    plan, _ = choose_offer(request(), offers_for("standard"))
    record = {**plan.model_dump(mode="json"), field: value}
    assert verify_draft(plan, record)


def test_concurrent_equivalent_submission_and_conflicting_reuse(tmp_path):
    store = Store(tmp_path / "jobs.db")
    payload = request().model_dump(mode="json")
    with ThreadPoolExecutor(max_workers=8) as pool:
        admissions = list(pool.map(lambda _: store.submit("PO-test", payload), range(16)))
    assert sum(admissions) == 1
    with pytest.raises(IntentConflict):
        store.submit("PO-test", {**payload, "quantity": 99})


def test_restart_is_explicit_and_resume_admission_is_atomic(tmp_path):
    store = Store(tmp_path / "jobs.db")
    store.submit("PO-test", request().model_dump(mode="json"))
    store.finish("PO-test", "creating_draft", {"plan": {"reference": "PO-test"}})
    assert store.interrupt_unfinished() == 1
    assert store.job("PO-test")["result"]["plan"]["reference"] == "PO-test"
    assert store.requeue("PO-test") is True
    assert store.requeue("PO-test") is False


def test_erp_response_loss_does_not_imply_write_failure(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        plan, _ = writing_job(app.state.store, confirm_before_write=True)
        payload = plan.model_dump(mode="json")
        response = client.post("/purchasing?lose_response=1", data=payload)
        assert response.status_code == 503
        saved = client.get("/api/drafts/PO-test").json()
        assert not verify_draft(plan, saved)
        # A retry using an equivalent decimal representation must reuse the record.
        response = client.post("/purchasing", data={**payload, "total": "240"})
        assert response.status_code == 200
        assert client.get("/api/drafts/PO-test").json()["id"] == saved["id"]
        response = client.post("/purchasing", data={**payload, "quantity": 21})
        assert response.status_code == 403


def test_erp_write_boundary_requires_the_approved_in_progress_plan(tmp_path):
    store = Store(tmp_path / "jobs.db")
    plan, _ = choose_offer(request(), offers_for("standard"))
    fields = plan.model_dump(mode="json")
    digest = plan_digest(plan)
    with pytest.raises(WriteNotAuthorized, match="no_purchase_task"):
        store.create_authorized_draft(fields, digest, allow_unconfirmed=False)

    writing_job(store, confirmed=False, confirm_before_write=True)
    with pytest.raises(WriteNotAuthorized, match="plan_not_approved"):
        store.create_authorized_draft(fields, digest, allow_unconfirmed=False)
    # Evaluation mode does not waive approval for a task that asked for it.
    with pytest.raises(WriteNotAuthorized, match="plan_not_approved"):
        store.create_authorized_draft(fields, digest, allow_unconfirmed=True)

    other = {**fields, "quantity": 30}
    with pytest.raises(WriteNotAuthorized, match="draft_differs_from_plan"):
        store.create_authorized_draft(other, "0" * 64, allow_unconfirmed=False)

    job = store.job("PO-test")
    store.finish("PO-test", "verified", {**job["result"], "confirmed_plan_hash": digest})
    with pytest.raises(WriteNotAuthorized, match="task_not_writing"):
        store.create_authorized_draft(fields, digest, allow_unconfirmed=False)
    assert store.draft("PO-test") is None


def test_evaluation_mode_admits_only_tasks_submitted_without_confirmation(tmp_path):
    store = Store(tmp_path / "jobs.db")
    plan, digest = writing_job(store, confirmed=False)
    fields = plan.model_dump(mode="json")
    with pytest.raises(WriteNotAuthorized, match="plan_not_approved"):
        store.create_authorized_draft(fields, digest, allow_unconfirmed=False)
    assert store.create_authorized_draft(fields, digest, allow_unconfirmed=True)["reused"] is False
    baseline = {**fields, "reference": "PO-baseline"}
    assert store.create_authorized_draft(baseline, "x", allow_unconfirmed=True)["reused"] is False


def test_unconfirmed_submission_is_refused_outside_evaluation_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("PROCUREMENT_ALLOW_UNCONFIRMED_WRITES", raising=False)
    with TestClient(create_app(tmp_path)) as client:
        response = client.post("/api/jobs", json=request().model_dump(mode="json"))
        assert response.status_code == 403
        assert client.get("/api/jobs/PO-test").status_code == 404


def test_concurrent_confirmations_and_draft_writes_have_one_effect(tmp_path):
    store = Store(tmp_path / "jobs.db")
    purchase = request(confirm_before_write=True)
    store.submit(purchase.reference, purchase.model_dump(mode="json"))
    plan, _ = choose_offer(purchase, offers_for("standard"))
    digest = plan_digest(plan)
    store.finish(
        purchase.reference,
        "awaiting_confirmation",
        {"plan": plan.model_dump(mode="json"), "plan_hash": digest},
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda i: store.confirm_job("PO-test", digest, f"a{i}"), range(16)))
    assert outcomes.count("confirmed") == 1
    assert sum(e["stage"] == "plan_confirmed" for e in store.job("PO-test")["events"]) == 1

    store.finish("PO-test", "creating_draft", store.job("PO-test")["result"])
    fields = plan.model_dump(mode="json")
    with ThreadPoolExecutor(max_workers=8) as pool:
        writes = list(
            pool.map(lambda _: store.create_authorized_draft(fields, digest, False), range(16))
        )
    assert sum(not w["reused"] for w in writes) == 1
    assert len({w["id"] for w in writes}) == 1


def test_hostile_text_is_escaped_in_browser_pages(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert "text/css" in client.get("/style.css").headers["content-type"]
        assert client.get("/suppliers?scenario=unknown").status_code == 404
        assert client.get("/api/jobs/missing").status_code == 404
        assert client.get("/purchasing?reference=%3Cscript%3E").status_code == 200
        assert client.get("/artifact/image?path=/etc/passwd").status_code == 403
        assert (
            client.get("/artifact/image?path=/data/artifacts/../../etc/passwd").status_code == 403
        )


def test_http_resume_after_application_restart_retains_saved_draft(tmp_path, monkeypatch):
    import time

    from procurement.agent import SkyvernClient

    store = Store(tmp_path / "procurement.db")
    purchase = request()
    store.submit(purchase.reference, purchase.model_dump(mode="json"))
    plan, _ = choose_offer(purchase, offers_for("standard"))
    store.finish(purchase.reference, "creating_draft", {"plan": plan.model_dump(mode="json")})
    saved = store.create_draft(plan.model_dump(mode="json"))

    async def forbidden_run(*args, **kwargs):
        pytest.fail("Resume must reconcile a saved draft without additional browser calls")

    monkeypatch.setattr(SkyvernClient, "run", forbidden_run)
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/jobs/PO-test").json()["status"] == "interrupted"
        assert client.post("/api/jobs/PO-test/resume").status_code == 202
        for _ in range(50):
            job = client.get("/api/jobs/PO-test").json()
            if job["status"] == "verified":
                break
            time.sleep(0.02)
        assert job["status"] == "verified"
        assert client.get("/api/drafts/PO-test").json()["id"] == saved["id"]


@pytest.mark.asyncio
async def test_resume_reconciles_existing_draft_without_another_browser_run(tmp_path, monkeypatch):
    from procurement.agent import SkyvernClient, execute_purchase

    store = Store(tmp_path / "jobs.db")
    purchase = request()
    store.submit(purchase.reference, purchase.model_dump(mode="json"))
    plan, _ = choose_offer(purchase, offers_for("standard"))
    store.finish(purchase.reference, "creating_draft", {"plan": plan.model_dump(mode="json")})
    store.create_draft(plan.model_dump(mode="json"))

    async def forbidden_run(*args, **kwargs):
        pytest.fail("A saved and matching draft must not cause another browser write")

    monkeypatch.setattr(SkyvernClient, "run", forbidden_run)
    await execute_purchase(purchase, store)
    assert store.job(purchase.reference)["status"] == "verified"
    assert store.job(purchase.reference)["result"]["reconciled"] is True


def test_confirmation_gate_pauses_validates_hash_and_writes_on_approval(tmp_path, monkeypatch):
    import json
    import time

    from procurement.agent import SkyvernClient

    offers = offers_for("standard")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return offers

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            return FakeResponse()

    async def fake_run(self, prompt, url, schema=None, max_steps=8):
        calls.append(url)
        if "/suppliers" in url:
            return {
                "run_id": "tsk_collect",
                "status": "completed",
                "output": {
                    "offers": offers,
                    "selected_quote_id": "Q-NORTH-01",
                    "explanation": "ok",
                },
            }
        plan = json.loads(prompt.split("values: ", 1)[1].split(". Do not pay", 1)[0])
        app.state.store.create_authorized_draft(plan, plan_digest_of(plan), False)
        return {"run_id": "tsk_write", "status": "completed", "output": None}

    from procurement.domain import PurchasePlan

    def plan_digest_of(fields):
        return plan_digest(PurchasePlan.model_validate(fields))

    monkeypatch.setattr("procurement.agent.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(SkyvernClient, "run", fake_run)
    monkeypatch.delenv("PROCUREMENT_APPROVER_TOKENS", raising=False)
    app = create_app(tmp_path)
    payload = request(confirm_before_write=True).model_dump(mode="json")

    with TestClient(app) as client:
        submit = client.post("/api/jobs", json=payload)
        assert submit.status_code == 202
        for _ in range(50):
            job = client.get("/api/jobs/PO-test").json()
            if job["status"] == "awaiting_confirmation":
                break
            time.sleep(0.02)
        assert job["status"] == "awaiting_confirmation"
        assert client.get("/api/drafts/PO-test").status_code == 404
        plan_hash = job["result"]["plan_hash"]

        body = {"plan_hash": plan_hash}
        url = "/api/jobs/PO-test/confirm"
        assert client.post(url, json=body, headers=APPROVER).status_code == 503
        monkeypatch.setenv("PROCUREMENT_APPROVER_TOKENS", "alice=approver-secret")
        assert client.post(url, json=body).status_code == 401
        assert client.post(url, json=body, headers={"Authorization": "Bearer guess"}).status_code == 401
        assert client.get("/api/jobs/PO-test").json()["status"] == "awaiting_confirmation"

        bad = client.post(url, json={"plan_hash": "0" * 64}, headers=APPROVER)
        assert bad.status_code == 409
        ok = client.post(url, json=body, headers=APPROVER)
        assert ok.status_code == 202
        assert ok.json()["approver"] == "alice"
        assert client.post(url, json=body, headers=APPROVER).status_code == 409
        for _ in range(50):
            job = client.get("/api/jobs/PO-test").json()
            if job["status"] == "verified":
                break
            time.sleep(0.02)
        assert job["status"] == "verified"
        assert job["result"]["confirmed_by"] == "alice"
        assert client.get("/api/drafts/PO-test").json()["quote_id"] == "Q-NORTH-01"
        assert len(calls) == 2


def test_confirmation_survives_restart_and_blocks_if_quote_changes(tmp_path, monkeypatch):
    import time

    from procurement.agent import SkyvernClient

    current_offers = offers_for("standard")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return current_offers

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            return FakeResponse()

    async def collect_only(self, prompt, url, schema=None, max_steps=8):
        if "/purchasing" in url:
            pytest.fail("Stale confirmation must block draft write")
        return {
            "run_id": "tsk_collect",
            "status": "completed",
            "output": {
                "offers": offers_for("standard"),
                "selected_quote_id": "Q-NORTH-01",
                "explanation": "ok",
            },
        }

    monkeypatch.setattr("procurement.agent.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(SkyvernClient, "run", collect_only)
    monkeypatch.setenv("PROCUREMENT_APPROVER_TOKENS", "alice=approver-secret")

    with TestClient(create_app(tmp_path)) as client:
        client.post("/api/jobs", json=request(confirm_before_write=True).model_dump(mode="json"))
        for _ in range(50):
            job = client.get("/api/jobs/PO-test").json()
            if job["status"] == "awaiting_confirmation":
                break
            time.sleep(0.02)
        assert job["status"] == "awaiting_confirmation"
        plan_hash = job["result"]["plan_hash"]

    current_offers[0]["price"] = "130.00"
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/jobs/PO-test").json()["status"] == "awaiting_confirmation"
        confirmed = client.post(
            "/api/jobs/PO-test/confirm", json={"plan_hash": plan_hash}, headers=APPROVER
        )
        assert confirmed.status_code == 202
        for _ in range(50):
            job = client.get("/api/jobs/PO-test").json()
            if job["status"] == "review_required":
                break
            time.sleep(0.02)
        assert job["status"] == "review_required"
        assert job["result"]["errors"] == ["quote_changed_after_confirmation"]
        assert client.get("/api/drafts/PO-test").status_code == 404


def test_model_gateway_sets_reasoning_effort_and_forwards_caller_key(tmp_path, monkeypatch):
    import httpx

    seen = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": []})

    real_client = httpx.AsyncClient

    def client_with_mock(**kwargs):
        return real_client(transport=httpx.MockTransport(upstream), **kwargs)

    monkeypatch.setattr("procurement.llm_gateway.httpx.AsyncClient", client_with_mock)
    monkeypatch.setenv("PROCUREMENT_LLM_UPSTREAM", "https://provider.test/v1")
    body = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": 'Fill {"reference":"PO-7","total":"1"}'}],
    }
    with TestClient(create_app(tmp_path)) as client:
        monkeypatch.setenv("PROCUREMENT_REASONING_EFFORT", "low")
        response = client.post("/llm/v1/chat/completions", json=body, headers={"Authorization": "Bearer caller"})
        assert response.status_code == 200
        assert seen == {
            "url": "https://provider.test/v1/chat/completions",
            "auth": "Bearer caller",
            "body": {**body, "reasoning_effort": "low"},
        }
        monkeypatch.setenv("PROCUREMENT_REASONING_EFFORT", "")
        client.post("/llm/v1/chat/completions", json=body)
        assert "reasoning_effort" not in seen["body"]
        assert client.post("/llm/v1/chat/completions", json={**body, "stream": True}).status_code == 400
    calls = [json.loads(line) for line in (tmp_path / "llm-calls.jsonl").read_text().splitlines()]
    assert [(c["reference"], c["reasoning_effort"]) for c in calls] == [("PO-7", "low"), ("PO-7", None)]
    assert "Fill" not in (tmp_path / "llm-calls.jsonl").read_text()
