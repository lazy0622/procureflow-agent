"""Combine repeated held-out runs into per-task pass^k (tau-bench style), no model calls.

pass^k is the chance that k independent trials of the same task all succeed, estimated
per task as C(c, k) / C(n, k) from c successes in n trials, then averaged over tasks.
"""

import json
from collections import defaultdict
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "procurement/evaluation/results"


def main() -> None:
    runs = sorted(path.parent for path in RESULTS.glob("*-heldout/summary.json"))
    outcomes: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for run in runs:
        for path in run.glob("*.json"):
            if path.name in {"summary.json", "manifest.json"}:
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            outcomes[record["policy"]][record["case_id"]].append(bool(record["correct"]))
    policies = {}
    for policy, cases in sorted(outcomes.items()):
        trials = min(len(results) for results in cases.values())
        pass_hat = {
            f"pass^{k}": round(sum(comb(sum(r), k) / comb(len(r), k) for r in cases.values()) / len(cases), 3)
            for k in range(1, trials + 1)
        }
        policies[policy] = {
            "tasks": len(cases),
            "trials_per_task": trials,
            "correct_trials": sum(sum(r) for r in cases.values()),
            "total_trials": sum(len(r) for r in cases.values()),
            **pass_hat,
            "tasks_not_always_correct": {case: f"{sum(r)}/{len(r)}" for case, r in sorted(cases.items()) if not all(r)},
        }
    payload = {
        "scope": "Held-out synthetic tasks, independent repeated real-model runs",
        "runs": [run.name for run in runs],
        "policies": policies,
    }
    out = ROOT / "procurement/evaluation/heldout-repeats.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
