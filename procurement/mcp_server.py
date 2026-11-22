"""MCP server that lets any agent host use ProcureFlow without being able to approve writes.

Tools call the workbench HTTP API. There are two permission modes, chosen by the operator
through PROCUREFLOW_MCP_MODE:

- ``readonly`` (default): look up offers, purchase tasks and drafts.
- ``propose``: also submit a purchase request. Submission always forces
  ``confirm_before_write`` and the verified policy, so the task stops with a reviewed
  plan before anything is written to the purchasing system.

No tool confirms a plan. A person opens the returned review link and confirms in the
workbench, so an agent cannot approve its own write. The purchase reference is the
idempotency key: the same request returns the same task, a different request under a
used reference is refused.

Run over stdio:  python -m procurement.mcp_server
"""

import asyncio
import os
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

MODES = ("readonly", "propose")
DEFAULT_AS_OF = date(2026, 11, 11)  # fixed date of the synthetic quotations
# States in which a task will not change again without a person or a restart.
SETTLED = {
    "awaiting_confirmation",
    "verified",
    "review_required",
    "failed",
    "interrupted",
    "draft_saved_unchecked",
}
READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
PROPOSE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def summarize(job: dict, base_url: str) -> dict[str, Any]:
    """Keep what a caller needs to decide the next step; drop raw browser output."""
    result = job.get("result") or {}
    summary: dict[str, Any] = {
        "reference": job["reference"],
        "status": job["status"],
        "plan": result.get("plan"),
        "rejected_offers": [
            {"quote_id": d.get("quote_id"), "reasons": d.get("reasons")}
            for d in result.get("decisions") or []
            if d.get("reasons")
        ],
        "errors": result.get("errors") or [result[key] for key in ("error", "error_type") if result.get(key)],
        "draft": result.get("draft"),
    }
    if job["status"] == "awaiting_confirmation":
        summary["next_step"] = (
            "A person must review and confirm this plan in the workbench. This server cannot confirm it."
        )
        summary["review_url"] = f"{base_url}/?reference={job['reference']}"
    return summary


def build_server(
    base_url: str | None = None,
    mode: str | None = None,
    client: Callable[[], httpx.AsyncClient] | None = None,
    poll_seconds: float = 2.0,
    wait_seconds: float = 240.0,
) -> FastMCP:
    """Create the server; ``client`` lets tests route requests to an in-process app."""
    base_url = (base_url or os.environ.get("PROCUREFLOW_URL", "http://127.0.0.1:8091")).rstrip("/")
    mode = mode or os.environ.get("PROCUREFLOW_MCP_MODE", "readonly")
    if mode not in MODES:
        raise ValueError(f"PROCUREFLOW_MCP_MODE must be one of {MODES}")
    open_client = client or (lambda: httpx.AsyncClient(base_url=base_url, timeout=30, trust_env=False))
    server = FastMCP(
        "procureflow",
        instructions=(
            "Purchasing tools for a synthetic supplier portal and purchasing system. "
            "Purchase requests stop at a plan that a person must confirm; never claim "
            "a purchase was made unless get_purchase reports status 'verified'."
        ),
    )

    async def get_json(path: str, missing: str) -> Any:
        async with open_client() as http:
            response = await http.get(path)
        if response.status_code == 404:
            raise ToolError(missing)
        response.raise_for_status()
        return response.json()

    @server.tool(annotations=READ)
    async def list_offers() -> list[dict[str, Any]]:
        """List current supplier quotations from the trusted source API."""
        return await get_json("/api/offers", "No quotations available")

    @server.tool(annotations=READ)
    async def get_purchase(
        reference: str = Field(description="Purchase reference, e.g. PO-1234"),
    ) -> dict[str, Any]:
        """Show a purchase task's status, chosen plan, rejected offers and draft."""
        job = await get_json(f"/api/jobs/{reference}", f"No purchase task {reference}")
        return summarize(job, base_url)

    @server.tool(annotations=READ)
    async def get_draft(
        reference: str = Field(description="Purchase reference, e.g. PO-1234"),
    ) -> dict[str, Any]:
        """Read the purchase draft saved in the purchasing system, if any."""
        return await get_json(f"/api/drafts/{reference}", f"No draft saved for {reference}")

    if mode == "propose":

        @server.tool(annotations=PROPOSE)
        async def propose_purchase(
            reference: str = Field(
                pattern=r"^[a-zA-Z0-9_-]{1,80}$",
                description="Unique purchase reference; reusing it returns the same task",
            ),
            sku: str = Field(description="Product code, e.g. BRG-6204"),
            quantity: int = Field(gt=0, le=10000, description="Pieces to buy"),
            max_lead_days: int = Field(ge=0, le=365, description="Latest acceptable delivery"),
            currency: Literal["CNY", "USD", "EUR"] = "CNY",
            as_of: Annotated[date, Field(description="Date used to check quote validity")] = DEFAULT_AS_OF,
        ) -> dict[str, Any]:
            """Collect quotations, apply purchasing rules and prepare a plan for review.

            Nothing is written to the purchasing system. The task stops at
            'awaiting_confirmation' with a review_url for a person, or at
            'review_required' when no quotation satisfies the request.
            """
            body = {
                "reference": reference,
                "sku": sku,
                "quantity": quantity,
                "currency": currency,
                "max_lead_days": max_lead_days,
                "as_of": as_of.isoformat(),
                "policy": "verified",
                "confirm_before_write": True,
            }
            async with open_client() as http:
                response = await http.post("/api/jobs", json=body)
                if response.status_code == 409:
                    raise ToolError(
                        f"Reference {reference} is already used for a different request; choose a new reference"
                    )
                if response.status_code == 422:
                    raise ToolError("The purchase request is invalid")
                response.raise_for_status()
                reused = response.json()["reused"]
                loop = asyncio.get_running_loop()
                deadline = loop.time() + wait_seconds
                while True:
                    job = (await http.get(f"/api/jobs/{reference}")).json()
                    if job["status"] in SETTLED or loop.time() > deadline:
                        break
                    await asyncio.sleep(poll_seconds)
            return {"reused_existing_task": reused, **summarize(job, base_url)}

    return server


if __name__ == "__main__":
    build_server().run(transport="stdio")
