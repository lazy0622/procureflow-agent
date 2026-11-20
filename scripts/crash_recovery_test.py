"""Kill the workbench process at fixed points of a real purchase job, then recover.

Each case recreates the workbench container with PROCUREMENT_CRASH_AT set, submits a
real Skyvern job, waits for the process to exit, restarts the container without the
variable, resumes the job and counts ERP drafts for its reference. Uses real model calls.
"""

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8091"
COMPOSE = ["docker", "compose", "-f", "compose.procurement.yml"]
CASES = [
    # (crash point, confirm before write, what the crash simulates)
    ("after_draft_commit", False, "ERP committed the draft; process died before replying"),
    ("before_read_back", False, "browser write finished; process died before verification"),
    ("before_browser_write", True, "human confirmed the plan; process died before writing"),
]
TERMINAL = {"verified", "review_required", "failed", "draft_saved_unchecked"}


def compose(*args: str, crash_at: str = "") -> str:
    env = {**os.environ, "PROCUREMENT_CRASH_AT": crash_at}
    result = subprocess.run([*COMPOSE, *args], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def container_state() -> dict:
    name = compose("ps", "-a", "-q", "workbench")
    raw = subprocess.run(
        ["docker", "inspect", "-f", "{{json .State}}", name],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(raw.stdout)


def restart_workbench(crash_at: str) -> None:
    compose("up", "-d", "--force-recreate", "--no-deps", "workbench", crash_at=crash_at)
    for _ in range(60):
        try:
            if httpx.get(URL + "/health", timeout=2, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("workbench did not become healthy")


def draft_count(reference: str) -> int:
    code = (
        "import sqlite3;"
        "c=sqlite3.connect('/state/procurement.db');"
        f"print(c.execute('SELECT count(*) FROM drafts WHERE reference=?',('{reference}',))"
        ".fetchone()[0])"
    )
    return int(compose("exec", "-T", "workbench", "python", "-c", code))


def wait_status(client: httpx.Client, reference: str, wanted: set[str]) -> dict:
    for _ in range(200):
        job = client.get(f"/api/jobs/{reference}").json()
        if job["status"] in wanted:
            return job
        time.sleep(2)
    raise TimeoutError(f"{reference} never reached {sorted(wanted)}")


def run_case(stamp: str, point: str, confirm: bool, meaning: str) -> dict:
    reference = f"CRASH-{stamp}-{point}".replace("_", "-")
    restart_workbench(point)
    body = {
        "reference": reference,
        "sku": "BRG-6204",
        "quantity": 20,
        "currency": "CNY",
        "max_lead_days": 5,
        "as_of": "2026-11-11",
        "scenario": "standard",
        "policy": "verified",
        "confirm_before_write": confirm,
    }
    started = time.monotonic()
    with httpx.Client(base_url=URL, timeout=30, trust_env=False) as client:
        client.post("/api/jobs", json=body).raise_for_status()
        if confirm:
            job = wait_status(client, reference, {"awaiting_confirmation"})
            client.post(
                f"/api/jobs/{reference}/confirm",
                json={"plan_hash": job["result"]["plan_hash"]},
            ).raise_for_status()
    for _ in range(200):
        state = container_state()
        if not state["Running"]:
            break
        time.sleep(2)
    else:
        raise TimeoutError("crash point was never reached")
    crashed_after = round(time.monotonic() - started, 1)
    restart_workbench("")
    with httpx.Client(base_url=URL, timeout=30, trust_env=False) as client:
        after_restart = client.get(f"/api/jobs/{reference}").json()
        drafts_before_resume = draft_count(reference)
        client.post(f"/api/jobs/{reference}/resume").raise_for_status()
        final = wait_status(client, reference, TERMINAL)
    # Give any still-running browser task time to retry a lost submission.
    time.sleep(45)
    drafts_final = draft_count(reference)
    stages = [event["stage"] for event in final["events"]]
    passed = (
        state["ExitCode"] == 70
        and after_restart["status"] == "interrupted"
        and final["status"] == "verified"
        and drafts_final == 1
        and not final["result"].get("errors")
    )
    return {
        "crash_point": point,
        "simulates": meaning,
        "reference": reference,
        "process_exit_code": state["ExitCode"],
        "seconds_until_crash": crashed_after,
        "status_after_restart": after_restart["status"],
        "drafts_before_resume": drafts_before_resume,
        "final_status": final["status"],
        "reconciled_existing_draft": bool(final["result"].get("reconciled")),
        "drafts_final": drafts_final,
        "draft_id": (final["result"].get("draft") or {}).get("id"),
        "stages": stages,
        "passed": passed,
    }


def main() -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results = []
    try:
        for point, confirm, meaning in CASES:
            results.append(run_case(stamp, point, confirm, meaning))
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    finally:
        restart_workbench("")
    payload = {
        "scope": "Real Skyvern + DeepSeek jobs on the synthetic workbench; one run per point",
        "run": stamp,
        "passed": sum(item["passed"] for item in results),
        "cases": len(results),
        "results": results,
    }
    out = ROOT / "procurement/evaluation/crash-recovery.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{payload['passed']}/{payload['cases']} crash points recovered")


if __name__ == "__main__":
    main()
