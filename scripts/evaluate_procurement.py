"""Run real-model purchase tasks against fixed, separately stored expected outcomes."""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def grade(case: dict, job: dict, draft: dict | None) -> bool:
    """Use fixed expected records rather than the model's completion claim."""
    if case["quote_id"] is None:
        return job["status"] == "review_required" and draft is None
    if job["status"] not in {"verified", "draft_saved_unchecked"} or draft is None:
        return False
    purchase = job["request"]
    return (
        draft.get("reference") == job["reference"]
        and draft.get("quote_id") == case["quote_id"]
        and draft.get("quantity") == purchase["quantity"]
        and draft.get("currency") == purchase["currency"]
        and draft.get("sku") == purchase["sku"]
        and Decimal(str(draft.get("total"))) == Decimal(case["total"])
    )


async def evaluate(args: argparse.Namespace) -> None:
    """Preserve every run before writing an aggregate; never overwrite prior results."""
    effort = running_reasoning_effort()
    cases = json.loads((ROOT / "procurement/evaluation/cases.json").read_text(encoding="utf-8"))
    cases = [case for case in cases if case["split"] == args.split]
    if args.limit:
        cases = cases[: args.limit]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # Earlier runs used the provider default (high); only non-default settings get a suffix.
    suffix = "" if effort == "high" else f"-effort-{effort}"
    out = ROOT / "procurement/evaluation/results" / (stamp + "-" + args.split + suffix)
    out.mkdir(parents=True, exist_ok=False)
    semaphore = asyncio.Semaphore(2)
    records = []

    async def run(case: dict, policy: str) -> None:
        async with semaphore:
            reference = f"E-{stamp}-{case['id']}-{policy}"
            body = {
                "reference": reference,
                "sku": "BRG-6204",
                "quantity": 20,
                "currency": "CNY",
                "max_lead_days": 5,
                "as_of": "2026-11-11",
                **case["request"],
                "policy": policy,
            }
            start = time.monotonic()
            async with httpx.AsyncClient(base_url=args.url, timeout=30, trust_env=False) as client:
                response = await client.post("/api/jobs", json=body)
                response.raise_for_status()
                for _ in range(360):
                    response = await client.get("/api/jobs/" + reference)
                    response.raise_for_status()
                    job = response.json()
                    if job["status"] not in {"queued", "collecting", "creating_draft"}:
                        break
                    await asyncio.sleep(2)
                response = await client.get("/api/drafts/" + reference)
                draft = response.json() if response.status_code == 200 else None
            record = {
                "case_id": case["id"],
                "split": args.split,
                "policy": policy,
                "elapsed_seconds": round(time.monotonic() - start, 2),
                "correct": grade(case, job, draft),
                "expected_quote_id": case["quote_id"],
                "job": job,
                "actual_draft": draft,
            }
            (out / f"{case['id']}-{policy}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            records.append(record)
            print(
                json.dumps({key: record[key] for key in ("case_id", "policy", "correct", "elapsed_seconds")}),
                flush=True,
            )

    await asyncio.gather(*(run(case, policy) for case in cases for policy in args.policies))
    summary = {
        "scope": "Synthetic task variants, one real-model trial per case and policy",
        "model": "deepseek-flash",
        "reasoning_effort": effort,
        "split": args.split,
        "cases": len(cases),
        "policies": {
            policy: {
                "correct": sum(r["correct"] for r in records if r["policy"] == policy),
                "trials": sum(r["policy"] == policy for r in records),
            }
            for policy in args.policies
        },
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(str(out), json.dumps(summary), flush=True)


def running_reasoning_effort() -> str:
    """Read the effort the running containers apply; DeepSeek's default is high."""

    def printenv(service: str, name: str) -> str:
        command = ["docker", "compose", "-f", "compose.procurement.yml", "exec", "-T"]
        result = subprocess.run(
            [*command, service, "printenv", name], check=False, cwd=ROOT, capture_output=True, text=True
        )
        return result.stdout.strip()

    if "workbench" not in printenv("skyvern", "OPENAI_COMPATIBLE_API_BASE"):
        return "high"
    return printenv("workbench", "PROCUREMENT_REASONING_EFFORT") or "high"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["development", "heldout"], default="development")
    parser.add_argument(
        "--policies",
        nargs="+",
        choices=["model_only", "verified"],
        default=["model_only", "verified"],
    )
    parser.add_argument("--url", default="http://127.0.0.1:8091")
    parser.add_argument("--limit", type=int)
    asyncio.run(evaluate(parser.parse_args()))
