"""Connect the business workflow to real upstream Skyvern browser tasks."""

import asyncio
import json
import os
import time
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from procurement.domain import (
    Offer,
    PurchasePlan,
    PurchaseRequest,
    choose_offer,
    plan_digest,
    verify_draft,
)
from procurement.store import Store


class ObservationBatch(BaseModel):
    """Contract for browser-extracted quotations and the model's own choice."""

    offers: list[Offer]
    selected_quote_id: str | None
    explanation: str


def local_api_key() -> str:
    """Read a local key without putting credentials in task prompts or responses."""
    key = os.environ.get("SKYVERN_API_KEY")
    if key and key != "PLACEHOLDER":
        return key
    path = Path(os.environ.get("SKYVERN_CREDENTIALS_FILE", ".skyvern/credentials.toml"))
    with path.open("rb") as file:
        config = tomllib.load(file)
    return config["skyvern"]["configs"][0]["orgs"][0]["cred"]


class SkyvernClient:
    """Submit bounded browser tasks and retain safe, business-relevant results."""

    def __init__(self) -> None:
        """Configure the upstream endpoint; no fallback to a simulated executor."""
        self.base_url = os.environ.get("SKYVERN_BASE_URL", "http://127.0.0.1:8020/v1")

    async def run(
        self, prompt: str, url: str, schema: dict | None = None, max_steps: int = 8
    ) -> dict[str, Any]:
        """Run an actual Skyvern task, cancelling it on the caller's deadline."""
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers={"x-api-key": local_api_key()},
            timeout=30,
            trust_env=False,
        ) as client:
            payload = {
                "prompt": prompt,
                "url": url,
                "max_steps": max_steps,
                "engine": "skyvern-1.0",
                "proxy_location": "NONE",
                "data_extraction_schema": schema,
            }
            if schema is not None:
                # v1 navigation with an explicit URL leaves data_extraction_goal unset.
                # The supported legacy extraction endpoint provides that goal explicitly.
                endpoint = self.base_url.removesuffix("/v1") + "/api/v1/tasks"
                response = await client.post(
                    endpoint,
                    json={
                        "url": url,
                        "data_extraction_goal": prompt,
                        "extracted_information_schema": schema,
                        "proxy_location": "NONE",
                    },
                    headers={"x-max-steps-override": str(max_steps)},
                )
            else:
                response = await client.post("/run/tasks", json=payload)
            response.raise_for_status()
            run_id = response.json().get("run_id") or response.json()["task_id"]
            deadline = time.monotonic() + 300
            try:
                while time.monotonic() < deadline:
                    response = await client.get(f"/runs/{run_id}")
                    response.raise_for_status()
                    run = response.json()
                    if run["status"] not in {"created", "queued", "running", "paused"}:
                        return {
                            key: run.get(key)
                            for key in (
                                "run_id",
                                "status",
                                "output",
                                "failure_reason",
                                "created_at",
                                "started_at",
                                "finished_at",
                                "step_count",
                                "total_cost",
                            )
                        }
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                await client.post(f"/runs/{run_id}/cancel")
                raise
            await client.post(f"/runs/{run_id}/cancel")
            raise TimeoutError(f"Skyvern task {run_id} exceeded 300 seconds")


async def execute_purchase(request: PurchaseRequest, store: Store) -> None:
    """Collect, decide, act and verify one synthetic purchase task."""
    reference = request.reference
    site = os.environ.get("PROCUREMENT_SITE_URL", "http://127.0.0.1:8091").rstrip("/")
    try:
        previous = store.job(reference)
        previous_result = (previous or {}).get("result") or {}
        existing = store.draft(reference)
        if existing and previous and previous["result"] and previous["result"].get("plan"):
            recovered_plan = PurchasePlan.model_validate(previous["result"]["plan"])
            errors = verify_draft(recovered_plan, existing)
            store.event(reference, "reconcile", {"draft_id": existing["id"], "errors": errors})
            outcome = "verified" if request.policy == "verified" else "draft_saved_unchecked"
            store.finish(
                reference,
                "review_required" if errors else outcome,
                {
                    **previous["result"],
                    "draft": existing,
                    "errors": errors,
                    "reconciled": True,
                },
            )
            return
        browser = SkyvernClient()
        plan: PurchasePlan | None
        confirmed_hash = previous_result.get("confirmed_plan_hash")
        if confirmed_hash and previous_result.get("plan"):
            plan = PurchasePlan.model_validate(previous_result["plan"])
            result = dict(previous_result)
            if plan_digest(plan) != confirmed_hash:
                store.event(
                    reference,
                    "confirmation_invalidated",
                    {"expected_plan_hash": confirmed_hash, "current_plan_hash": plan_digest(plan)},
                )
                store.finish(
                    reference,
                    "review_required",
                    {**result, "errors": ["plan_changed_after_confirmation"]},
                )
                return
            if request.policy == "verified":
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    response = await client.get(
                        f"{site}/api/offers", params={"scenario": request.scenario}
                    )
                    response.raise_for_status()
                latest_offers = response.json()
                latest_plan, latest_decisions = choose_offer(request, latest_offers)
                if latest_plan is None or plan_digest(latest_plan) != confirmed_hash:
                    new_hash = plan_digest(latest_plan) if latest_plan else None
                    result.pop("confirmed_plan_hash", None)
                    result["decisions"] = [
                        decision.model_dump(mode="json") for decision in latest_decisions
                    ]
                    store.event(
                        reference,
                        "confirmation_invalidated",
                        {"expected_plan_hash": confirmed_hash, "current_plan_hash": new_hash},
                    )
                    store.finish(
                        reference,
                        "review_required",
                        {**result, "errors": ["quote_changed_after_confirmation"]},
                    )
                    return
        else:
            store.finish(reference, "collecting", {})
            store.event(reference, "collecting", {"scenario": request.scenario})
            prompt = (
                "Read every supplier quotation on this page. Extract the displayed values exactly. "
                "Do not guess missing currency or convert prices into another unit in the offers. "
                "If currency is missing use null. Also select the lowest-cost quotation satisfying "
                "the purchase request, or select null when none qualify. Stock is in pieces; price "
                "is per displayed unit and whole boxes must be purchased. Request: "
                + request.model_dump_json(
                    exclude={"policy", "reference", "scenario", "layout", "confirm_before_write"}
                )
            )
            collected = await browser.run(
                prompt,
                f"{site}/suppliers?scenario={request.scenario}&layout={request.layout}",
                ObservationBatch.model_json_schema(),
            )
            store.event(reference, "collection_run", collected)
            if collected["status"] != "completed":
                store.finish(reference, "failed", {"error": "collection_not_completed"})
                return
            output = collected["output"]
            if isinstance(output, str):
                output = json.loads(output)
            observed = ObservationBatch.model_validate(output)
            observations = [offer.model_dump(mode="json") for offer in observed.offers]
            plan, decisions = choose_offer(request, observations)
            if request.policy == "model_only":
                selected = next(
                    (
                        offer
                        for offer in observed.offers
                        if offer.quote_id == observed.selected_quote_id
                    ),
                    None,
                )
                plan = (
                    PurchasePlan(
                        reference=reference,
                        quote_id=selected.quote_id,
                        supplier=selected.supplier,
                        sku=selected.sku,
                        quantity=request.quantity,
                        currency=selected.currency or "",
                        total=(selected.price * request.quantity / selected.pack_size).quantize(
                            Decimal("0.01")
                        ),
                    )
                    if selected
                    else None
                )
            result = {
                "observations": observations,
                "model_choice": observed.selected_quote_id,
                "decisions": [decision.model_dump(mode="json") for decision in decisions],
                "collection_run_id": collected["run_id"],
            }
            if plan is None:
                store.finish(reference, "review_required", result)
                return
            # Re-read a supplier's public source before any write. This is not a hidden gold answer.
            if request.policy == "verified":
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    response = await client.get(
                        f"{site}/api/offers", params={"scenario": request.scenario}
                    )
                    response.raise_for_status()
                original = {
                    item["quote_id"]: Offer.model_validate(item) for item in response.json()
                }
                extracted = {item.quote_id: item for item in observed.offers}
                if original != extracted:
                    store.finish(
                        reference,
                        "review_required",
                        {
                            **result,
                            "errors": ["quote_source_changed_or_extraction_mismatch"],
                        },
                    )
                    return
            digest = plan_digest(plan)
            result["plan"] = plan.model_dump(mode="json")
            result["plan_hash"] = digest
            if request.confirm_before_write:
                store.event(
                    reference,
                    "awaiting_confirmation",
                    {"plan_hash": digest, "plan": result["plan"]},
                )
                store.finish(reference, "awaiting_confirmation", result)
                return
        store.finish(reference, "creating_draft", result)
        store.event(reference, "plan", result["plan"])
        # Reference is the business identity across retries; ERP has its own unique constraint.
        existing = store.draft(reference)
        if existing is None:
            suffix = "?lose_response=1" if request.scenario == "response_loss" else ""
            created = await browser.run(
                "Fill and submit this purchase DRAFT form exactly once using these values: "
                + plan.model_dump_json()
                + ". Do not pay or finalize a purchase. If the response is lost, inspect the "
                "draft lookup on this page using reference before resubmitting.",
                f"{site}/purchasing{suffix}",
                max_steps=10,
            )
            store.event(reference, "creation_run", created)
            result["creation_run_id"] = created["run_id"]
        record = store.draft(reference)
        if record is None:
            store.finish(reference, "failed", {**result, "errors": ["draft_not_found"]})
            return
        errors = verify_draft(plan, record)
        result.update({"draft": record, "errors": errors})
        store.event(reference, "read_back", {"draft_id": record["id"], "errors": errors})
        outcome = "verified" if request.policy == "verified" else "draft_saved_unchecked"
        store.finish(reference, outcome if not errors else "review_required", result)
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, OSError) as exc:
        # Persist the plan for reconciliation while keeping provider/server error bodies private.
        previous = store.job(reference)
        result = previous["result"] if previous and previous["result"] else {}
        store.finish(reference, "failed", {**result, "error_type": type(exc).__name__})
