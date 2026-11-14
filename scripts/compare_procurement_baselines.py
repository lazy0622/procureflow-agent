"""Compare API, fixed Playwright and Skyvern on the same known synthetic tasks."""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from procurement.domain import PurchaseRequest, choose_offer, verify_draft


async def simple_task(client, purchase, strategy, browser):
    """Share decision rules so this comparison isolates the execution interface."""
    if strategy == "api":
        response = await client.get("/api/offers", params={"scenario": purchase.scenario})
        response.raise_for_status()
        observations = response.json()
    else:
        page = await browser.new_page()
        try:
            await page.goto(
                str(client.base_url)
                + f"suppliers?scenario={purchase.scenario}&layout={purchase.layout}"
            )
            # Deliberately fixed table adapter: no hidden fallback to API or model.
            observations = await page.locator("table").evaluate(
                "table => { const rows=[...table.rows]; "
                "const keys=[...rows.shift().cells].map(c=>c.textContent.trim()); "
                "return rows.map(r=>Object.fromEntries([...r.cells].map((c,i)=>"
                "[keys[i],c.textContent.trim()]))); }",
                timeout=5000,
            )
            for row in observations:
                for key in ("pack_size", "stock", "lead_days"):
                    row[key] = int(row[key])
                if row["currency"] == "Not specified":
                    row["currency"] = None
        finally:
            await page.close()
    plan, decisions = choose_offer(purchase, observations)
    if plan is None:
        return {"status": "review_required", "draft": None, "decisions": len(decisions)}
    payload = plan.model_dump(mode="json")
    suffix = "?lose_response=1" if purchase.scenario == "response_loss" else ""
    if strategy == "api":
        response = await client.post("/purchasing" + suffix, data=payload)
        if response.status_code not in {303, 503}:
            response.raise_for_status()
    else:
        page = await browser.new_page()
        try:
            await page.goto(str(client.base_url) + "purchasing" + suffix)
            form = page.locator("form[method='post']")
            for key, value in payload.items():
                await form.locator(f"input[name='{key}']").fill(str(value))
            await form.locator("button[type='submit']").click()
            await page.wait_for_load_state()
        finally:
            await page.close()
    response = await client.get("/api/drafts/" + purchase.reference)
    response.raise_for_status()
    record = response.json()
    return {"status": "verified", "draft": record, "errors": verify_draft(plan, record)}


async def run(args):
    """Retain failures and unique draft references; never overwrite an old run."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "procurement/evaluation/results" / (stamp + "-simple-baselines")
    output.mkdir(parents=True, exist_ok=False)
    all_cases = json.loads((ROOT / "procurement/evaluation/cases.json").read_text("utf-8"))
    ids = {"dev-standard", "dev-response-loss", "h03-unpacked", "h17-layout-box"}
    cases = [case for case in all_cases if case["id"] in ids]
    manifest = {
        "scope": "Known synthetic regression tasks; shared deterministic business rules",
        "strategies": args.strategies,
        "trials_per_case": args.repeats,
        "cases": cases,
        "notes": [
            "API has a complete source and write interface in this fixture.",
            "Fixed Playwright supports table layout only; cards failures are retained.",
            "Skyvern includes model extraction and browser writing; not an inner-loop ablation.",
            "HTTP/browser times include task execution; costs are not calibrated.",
        ],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
    records = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            async with httpx.AsyncClient(base_url=args.url, timeout=30, trust_env=False) as client:
                # Rotate order, rather than always warming one strategy first.
                for trial in range(args.repeats):
                    order = args.strategies[trial:] + args.strategies[:trial]
                    for case in cases:
                        for strategy in order:
                            reference = f"B-{stamp}-{trial}-{case['id']}-{strategy}"
                            purchase = PurchaseRequest.model_validate({
                                "reference": reference, "sku": "BRG-6204", "quantity": 20,
                                "currency": "CNY", "max_lead_days": 5,
                                **case["request"], "policy": "verified",
                            })
                            started = time.monotonic()
                            record = {"case": case["id"], "trial": trial, "strategy": strategy}
                            try:
                                if strategy == "skyvern":
                                    response = await client.post(
                                        "/api/jobs", json=purchase.model_dump(mode="json")
                                    )
                                    response.raise_for_status()
                                    for _ in range(330):
                                        response = await client.get("/api/jobs/" + reference)
                                        response.raise_for_status()
                                        job = response.json()
                                        if job["status"] not in {
                                            "queued", "collecting", "creating_draft"
                                        }:
                                            break
                                        await asyncio.sleep(2)
                                    result = {"status": job["status"], **(job["result"] or {})}
                                    record["job"] = job
                                else:
                                    result = await simple_task(client, purchase, strategy, browser)
                                draft = result.get("draft")
                                record["correct"] = bool(
                                    draft and result["status"] == "verified"
                                    and draft["quote_id"] == case["quote_id"]
                                    and str(draft["total"]) == case["total"]
                                    and draft["quantity"] == purchase.quantity
                                    and draft["reference"] == reference
                                    and draft["sku"] == purchase.sku
                                    and draft["currency"] == purchase.currency
                                    and not result.get("errors")
                                )
                                record["result"] = result
                            except (httpx.HTTPError, ValueError, TimeoutError) as exc:
                                record.update(correct=False, failure=type(exc).__name__)
                            except Exception as exc:  # noqa: BLE001 -- retain browser failures
                                record.update(correct=False, failure=type(exc).__name__)
                            record["seconds"] = round(time.monotonic() - started, 3)
                            records.append(record)
                            (output / f"{reference}.json").write_text(
                                json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
                            )
                            print(json.dumps({k: v for k, v in record.items()
                                              if k not in {"job", "result"}}), flush=True)
        finally:
            await browser.close()
    summary = {strategy: {
        "passed": sum(r["correct"] for r in records if r["strategy"] == strategy),
        "trials": sum(r["strategy"] == strategy for r in records),
        "mean_seconds": round(sum(r["seconds"] for r in records
                                  if r["strategy"] == strategy)
                              / sum(r["strategy"] == strategy for r in records), 3),
    } for strategy in args.strategies}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), "utf-8")
    print(str(output), json.dumps(summary), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8091/")
    parser.add_argument("--repeats", type=int, choices=[1, 2, 3], default=2)
    parser.add_argument("--strategies", nargs="+", choices=["api", "playwright", "skyvern"],
                        default=["api", "playwright", "skyvern"])
    asyncio.run(run(parser.parse_args()))
