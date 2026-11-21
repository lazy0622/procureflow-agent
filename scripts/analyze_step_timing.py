"""Split browser-task time into model calls, browser actions and the rest, for one run.

Reads the run's job records, Skyvern's task and action timestamps from Postgres, and the
model gateway's call log (.local/business/llm-calls.jsonl). Model calls are attributed to a
task when they carry the task's purchase reference and start inside the task's window.
Only draft-creation prompts carry the reference, so only that stage is split. Collection
runs one extraction action whose model call happens inside the action. Makes no model calls.

    python scripts/analyze_step_timing.py --run 20261121T130012Z-heldout-effort-low
"""

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "procurement/evaluation"


def query(sql: str) -> list:
    command = ["docker", "compose", "-f", "compose.procurement.yml", "exec", "-T", "postgres"]
    result = subprocess.run(
        [*command, "psql", "-U", "skyvern", "-d", "skyvern", "-tA", "-c", sql],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout or "[]")


def utc(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    run = parser.parse_args().run
    tasks: dict[str, dict] = {}
    for path in sorted((EVAL / "results" / run).glob("*.json")):
        if path.name in {"summary.json", "manifest.json"}:
            continue
        job = json.loads(path.read_text(encoding="utf-8"))["job"]
        for event in job.get("events", []):
            if event["stage"] in {"collection_run", "creation_run"}:
                tasks[event["detail"]["run_id"]] = {
                    "stage": event["stage"],
                    "reference": job["reference"],
                }
    ids = ",".join(f"'{task_id}'" for task_id in tasks if task_id.startswith("tsk_"))
    for row in query(
        "SELECT coalesce(json_agg(t), '[]') FROM (SELECT task_id, started_at, finished_at "
        f"FROM tasks WHERE task_id IN ({ids})) t"
    ):
        if not row["started_at"] or not row["finished_at"]:
            continue
        tasks[row["task_id"]].update(start=utc(row["started_at"]), end=utc(row["finished_at"]))
    actions = query(
        "SELECT coalesce(json_agg(a), '[]') FROM (SELECT task_id, started_at, finished_at "
        f"FROM actions WHERE task_id IN ({ids}) AND started_at IS NOT NULL) a"
    )
    calls = [
        json.loads(line)
        for line in (ROOT / ".local/business/llm-calls.jsonl").read_text().splitlines()
    ]
    windows = [(t["start"], t["end"]) for t in tasks.values() if "start" in t]
    run_start, run_end = min(s for s, _ in windows), max(e for _, e in windows)
    calls = [c for c in calls if run_start <= utc(c["started_at"]) <= run_end]
    references = {task["reference"] for task in tasks.values()}
    rows: list[dict] = []
    for task_id, task in tasks.items():
        if "start" not in task or task["stage"] != "creation_run":
            continue
        total = (task["end"] - task["start"]).total_seconds()
        own = [
            (utc(a["started_at"]), utc(a["finished_at"]))
            for a in actions
            if a["task_id"] == task_id
        ]
        model = [
            c["seconds"]
            for c in calls
            if c["reference"] == task["reference"]
            and task["start"] <= utc(c["started_at"]) <= task["end"]
        ]
        action_seconds = sum((end - start).total_seconds() for start, end in own)
        rows.append(
            {
                "seconds": total,
                "model_calls": len(model),
                "model_seconds": sum(model),
                "actions": len(own),
                "action_seconds": action_seconds,
                "other_seconds": total - sum(model) - action_seconds,
            }
        )
    unattributed = [c for c in calls if c["reference"] not in references]

    def share(rows: list[dict], key: str) -> float | None:
        total = sum(r["seconds"] for r in rows)
        return round(sum(r[key] for r in rows) / total, 3) if total else None

    payload = {
        "run": run,
        "scope": "Draft-creation browser tasks; model time counts calls carrying the job reference",
        "creation_tasks": len(rows),
        "mean": {key: round(mean(row[key] for row in rows), 2) for key in rows[0]},
        "share_model": share(rows, "model_seconds"),
        "share_actions": share(rows, "action_seconds"),
        "share_other": share(rows, "other_seconds"),
        "calls_without_job_reference": {
            "note": "Collection and some check calls hold no job reference; not attributed",
            "count": len(unattributed),
            "seconds": round(sum(c["seconds"] for c in unattributed), 1),
        },
    }
    out = EVAL / f"step-timing-{run}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
