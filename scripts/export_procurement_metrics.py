"""Export upstream step token counters without reading prompts or credentials."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    print(f"Exported {len(records)} upstream task counters; no credential or prompt fields.")


if __name__ == "__main__":
    export()
