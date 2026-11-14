"""Export upstream step token counters and task-level telemetry summaries."""

import json
import subprocess
from collections import Counter
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]


def summarize_task_telemetry() -> dict:
    """Aggregate pass rates, latency percentiles and failure stages from evaluation logs."""
    results_root = ROOT / "procurement/evaluation/results"
    runs = []
    for summary_path in sorted(results_root.glob("*/summary.json")):
        run_dir = summary_path.parent
        files = [
            path
            for path in sorted(run_dir.glob("*.json"))
            if path.name not in {"summary.json", "manifest.json"}
        ]
        latencies: list[float] = []
        statuses: Counter[str] = Counter()
        failures: Counter[str] = Counter()
        passed = 0
        for file in files:
            record = json.loads(file.read_text(encoding="utf-8"))
            if record.get("correct"):
                passed += 1
            elapsed = record.get("elapsed_seconds") or record.get("seconds")
            if isinstance(elapsed, (int, float)):
                latencies.append(float(elapsed))
            job = record.get("job") or {}
            status = job.get("status") or record.get("result", {}).get("status")
            if status:
                statuses[status] += 1
            if not record.get("correct"):
                stage_errors = (job.get("result") or {}).get("errors") or record.get("errors") or []
                key = (
                    record.get("failure")
                    or (stage_errors[0] if stage_errors else None)
                    or status
                    or "unknown"
                )
                failures[str(key)] += 1
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))] if ordered else None
        runs.append(
            {
                "run": run_dir.name,
                "trials": len(files),
                "passed": passed,
                "mean_seconds": round(sum(ordered) / len(ordered), 2) if ordered else None,
                "p50_seconds": round(median(ordered), 2) if ordered else None,
                "p95_seconds": round(p95, 2) if p95 is not None else None,
                "statuses": dict(statuses),
                "failure_reasons": dict(failures),
            }
        )
    payload = {
        "scope": "Aggregated from persisted evaluation result directories; costs remain uncalibrated.",
        "runs": runs,
    }
    out = ROOT / "procurement/evaluation/task-telemetry.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def export() -> None:
    """Read the project's isolated database and keep unavailable costs explicit."""
    query = """
        SELECT coalesce(json_agg(row_to_json(m)), '[]'::json) FROM (
            SELECT task_id AS run_id, count(*) AS steps,
                   sum(input_token_count) AS input_tokens,
                   sum(output_token_count) AS output_tokens,
                   sum(reasoning_token_count) AS reasoning_tokens,
                   sum(cached_token_count) AS cached_tokens,
                   sum(step_cost) AS reported_cost
            FROM steps GROUP BY task_id ORDER BY task_id
        ) AS m;
    """
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.procurement.yml",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "skyvern",
            "-d",
            "skyvern",
            "-tA",
            "-c",
            query,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    records = json.loads(result.stdout)
    for record in records:
        if not record["reported_cost"]:
            record["reported_cost"] = None
            record["cost_note"] = "Custom-model pricing is not calibrated; zero is not free"
    path = ROOT / "procurement/evaluation/step-metrics.json"
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    telemetry = summarize_task_telemetry()
    print(
        f"Exported {len(records)} upstream task counters and "
        f"{len(telemetry['runs'])} task-telemetry runs; no credential or prompt fields."
    )


if __name__ == "__main__":
    export()
