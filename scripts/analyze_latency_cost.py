"""Break purchase-job latency and token cost down by stage from saved evaluation records.

Reads persisted job events and the exported Skyvern step counters. Makes no model calls.
Cost uses DeepSeek's published deepseek-flash list prices (USD per 1M tokens, checked
2026-11-21): input cache miss 0.30 / 0.15, output 1.20 / 0.60 (peak / off-peak).
Reasoning tokens are a subset of output tokens and are not added again.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "procurement/evaluation"
PRICES = {"peak": (0.30, 1.20), "off_peak": (0.15, 0.60)}


def seconds(run: dict) -> float | None:
    """Duration that Skyvern itself reports for one browser task."""
    if not run.get("started_at") or not run.get("finished_at"):
        return None
    start = datetime.fromisoformat(run["started_at"])
    return (datetime.fromisoformat(run["finished_at"]) - start).total_seconds()


def describe(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "mean": round(mean(ordered), 4),
        "p50": round(median(ordered), 4),
        "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 4),
    }


def analyze(patterns: list[str], out_name: str) -> dict:
    runs = sorted(
        path.parent.name for pattern in patterns for path in (EVAL / "results").glob(f"{pattern}/summary.json")
    )
    tokens = {m["run_id"]: m for m in json.loads((EVAL / "step-metrics.json").read_text())}
    stages: dict[str, dict[str, list[float]]] = {
        name: {
            "seconds": [],
            "steps": [],
            "input_tokens": [],
            "output_tokens": [],
            "reasoning_tokens": [],
        }
        for name in ("collection_run", "creation_run")
    }
    job_totals: list[float] = []
    write_jobs: list[tuple[float, float, float]] = []
    job_costs: dict[str, list[float]] = {name: [] for name in PRICES}
    missing_counters = 0
    for run in runs:
        for path in sorted((EVAL / "results" / run).glob("*.json")):
            if path.name in {"summary.json", "manifest.json"}:
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            job_totals.append(record["elapsed_seconds"])
            job_in = job_out = 0
            stage_seconds: dict[str, float] = {}
            for event in record["job"].get("events", []):
                if event["stage"] not in stages:
                    continue
                detail = event["detail"]
                bucket = stages[event["stage"]]
                if (value := seconds(detail)) is not None:
                    bucket["seconds"].append(value)
                    stage_seconds[event["stage"]] = value
                if detail.get("step_count") is not None:
                    bucket["steps"].append(detail["step_count"])
                counter = tokens.get(detail.get("run_id"))
                if counter is None:
                    missing_counters += 1
                    continue
                bucket["input_tokens"].append(counter["input_tokens"])
                bucket["output_tokens"].append(counter["output_tokens"])
                bucket["reasoning_tokens"].append(counter["reasoning_tokens"] or 0)
                job_in += counter["input_tokens"]
                job_out += counter["output_tokens"]
            if {"collection_run", "creation_run"} <= stage_seconds.keys():
                write_jobs.append(
                    (
                        record["elapsed_seconds"],
                        stage_seconds["collection_run"],
                        stage_seconds["creation_run"],
                    )
                )
            for name, (price_in, price_out) in PRICES.items():
                job_costs[name].append((job_in * price_in + job_out * price_out) / 1e6)
    total = describe(job_totals)
    write_total = sum(job for job, _, _ in write_jobs)
    creation = stages["creation_run"]
    payload = {
        "scope": "Real-model jobs, both policies, runs matching: " + ", ".join(patterns),
        "runs": runs,
        "jobs": len(job_totals),
        "job_seconds": total,
        "stages": {name: {key: describe(values) for key, values in bucket.items()} for name, bucket in stages.items()},
        "share_of_time_in_jobs_that_wrote_a_draft": {
            "jobs": len(write_jobs),
            "collection_run": round(sum(c for _, c, _ in write_jobs) / write_total, 3),
            "creation_run": round(sum(w for _, _, w in write_jobs) / write_total, 3),
        },
        "creation_reasoning_share_of_output": round(
            sum(creation["reasoning_tokens"]) / sum(creation["output_tokens"]), 3
        ),
        "cost_usd_per_job": {name: describe(values) for name, values in job_costs.items()},
        "browser_runs_without_token_counter": missing_counters,
        "pricing_source": "https://api-docs.deepseek.com/quick_start/pricing (2026-11-21)",
        "notes": [
            "Jobs stopped by business rules before writing have no creation_run.",
            "Cache-hit discounts are ignored; exported counters report 0 cached tokens.",
        ],
    }
    out = EVAL / out_name
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", nargs="+", default=["*-development", "*-heldout"], help="run-directory globs")
    parser.add_argument("--out", default="latency-cost.json")
    args = parser.parse_args()
    print(json.dumps(analyze(args.runs, args.out), ensure_ascii=False, indent=2))
