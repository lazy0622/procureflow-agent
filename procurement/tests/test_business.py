"""Business contracts and failure boundaries; these tests do not call an LLM."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from procurement.domain import PurchaseRequest, choose_offer, verify_draft
from procurement.fixtures import offers_for
from procurement.server import create_app
from procurement.store import IntentConflict, Store


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
    plan, _ = choose_offer(request(), offers_for("standard"))
    payload = plan.model_dump(mode="json")
    with TestClient(app) as client:
        response = client.post("/purchasing?lose_response=1", data=payload)
        assert response.status_code == 503
        saved = client.get("/api/drafts/PO-test").json()
        assert not verify_draft(plan, saved)
        # A retry using an equivalent decimal representation must reuse the record.
        response = client.post("/purchasing", data={**payload, "total": "240"})
        assert response.status_code == 200
        assert client.get("/api/drafts/PO-test").json()["id"] == saved["id"]
        response = client.post("/purchasing", data={**payload, "quantity": 21})
        assert response.status_code == 409


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
