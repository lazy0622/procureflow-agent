"""Local workbench and synthetic supplier/ERP applications for ProcureFlow."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from procurement.agent import execute_purchase
from procurement.domain import PurchaseRequest
from procurement.faults import crash_point
from procurement.fixtures import offers_for
from procurement.llm_gateway import router as llm_router
from procurement.store import IntentConflict, Store

SCENARIOS = {"standard", "expired", "shortage", "mismatch", "missing_currency", "response_loss"}


class DraftInput(BaseModel):
    """Fields accepted by the synthetic ERP; business policy is checked separately."""

    reference: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    quote_id: str = Field(min_length=1, max_length=80)
    supplier: str = Field(min_length=1, max_length=80)
    sku: str = Field(min_length=1, max_length=80)
    quantity: int = Field(gt=0, le=10000)
    currency: str = Field(max_length=10)
    total: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=16, decimal_places=2)

    @field_validator("total")
    @classmethod
    def canonical_total(cls, value: Decimal) -> Decimal:
        """Keep semantically equal money values identical across ERP retries."""
        return value.quantize(Decimal("0.01"))


class ConfirmInput(BaseModel):
    """Bind human approval to the exact reviewed purchase plan fingerprint."""

    model_config = ConfigDict(extra="forbid")
    plan_hash: str = Field(min_length=16, max_length=128)


def page(title: str, body: str) -> str:
    """Render a normal HTML page for both humans and browser agents."""
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        f"<title>{escape(title)} · ProcureFlow</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="stylesheet" href="/style.css">'
        '<header><a href="/">ProcureFlow</a><nav><a href="/suppliers">供应商报价</a>'
        '<a href="/purchasing">采购后台</a></nav></header>'
        f"<main>{body}</main></html>"
    )


def create_app(data_dir: Path | None = None) -> FastAPI:
    """Create an isolated, single-process local demonstration application."""
    directory = data_dir or Path(os.environ.get("PROCUREMENT_DATA_DIR", ".local/business"))
    store = Store(directory / "procurement.db")
    tasks: set[asyncio.Task] = set()
    semaphore = asyncio.Semaphore(2)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        store.interrupt_unfinished()
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="ProcureFlow", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8080", "http://localhost:8080"],
        allow_methods=["GET"],
    )
    app.state.store = store
    app.state.data_dir = directory
    app.include_router(llm_router)

    async def run(request: PurchaseRequest) -> None:
        try:
            async with semaphore:
                await execute_purchase(request, store)
        except asyncio.CancelledError:
            previous = store.job(request.reference)
            store.finish(request.reference, "interrupted", (previous or {}).get("result") or {})
            raise
        except Exception as exc:  # noqa: BLE001 -- worker boundary must persist a terminal outcome
            previous = store.job(request.reference)
            store.finish(
                request.reference,
                "failed",
                {
                    **((previous or {}).get("result") or {}),
                    "error_type": type(exc).__name__,
                },
            )

    def launch(request: PurchaseRequest) -> None:
        task = asyncio.create_task(run(request))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "data": "synthetic", "execution": "skyvern"}

    @app.get("/artifact/image")
    @app.get("/artifact/recording")
    def artifact(path: str) -> FileResponse:
        requested = Path(path.removeprefix("file://")).resolve()
        roots = [Path("/data/artifacts").resolve(), Path("/data/videos").resolve()]
        if not any(requested.is_relative_to(root) for root in roots):
            raise HTTPException(403, "Outside the demonstration artifact directories")
        if not requested.is_file():
            raise HTTPException(404, "Artifact not found")
        return FileResponse(requested)

    @app.get("/style.css")
    def stylesheet() -> HTMLResponse:
        return HTMLResponse(
            (Path(__file__).parent / "web" / "style.css").read_text(encoding="utf-8"),
            media_type="text/css",
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        content = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        return page("采购执行工作台", content)

    @app.get("/suppliers", response_class=HTMLResponse)
    def suppliers(scenario: str = "standard", layout: str = "table") -> str:
        if scenario not in SCENARIOS:
            raise HTTPException(404, "Unknown demonstration scenario")
        quotations = offers_for(scenario)
        columns = list(quotations[0])
        header = "".join(f"<th>{escape(key)}</th>" for key in columns)
        rows = "".join(
            "<tr>"
            + "".join(
                f"<td>{escape(str(row[key]) if row[key] is not None else 'Not specified')}</td>"
                for key in columns
            )
            + "</tr>"
            for row in quotations
        )
        if layout == "cards":
            content = "".join(
                "<section class='panel'><h2>"
                + escape(row["supplier"])
                + "</h2><dl>"
                + "".join(
                    f"<dt>{escape(key)}</dt><dd>{escape(str(value))}</dd>"
                    for key, value in row.items()
                )
                + "</dl></section>"
                for row in reversed(quotations)
            )
        elif layout == "table":
            content = f"<div class='table-scroll'><table><tr>{header}</tr>{rows}</table></div>"
        else:
            raise HTTPException(404, "Unknown layout")
        return page(
            "Supplier quotations",
            "<p class='eyebrow'>SYNTHETIC SUPPLIER PORTAL</p>"
            "<h1>供应商报价 / Supplier quotations</h1>"
            "<p>All prices are synthetic. Price is per stated unit; stock is in pieces. "
            "Do not assume currency conversion. Reference date: 2026-11-11.</p>" + content,
        )

    @app.get("/api/offers")
    def offers(scenario: str = "standard") -> list[dict]:
        if scenario not in SCENARIOS:
            raise HTTPException(404, "Unknown demonstration scenario")
        return offers_for(scenario)

    @app.post("/api/jobs", status_code=202)
    async def submit(request: PurchaseRequest) -> dict:
        try:
            fresh = store.submit(request.reference, request.model_dump(mode="json"))
        except IntentConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        if fresh:
            launch(request)
        return {
            "reference": request.reference,
            "reused": not fresh,
            "job": store.job(request.reference),
        }

    @app.get("/api/jobs/{reference}")
    def get_job(reference: str) -> dict:
        job = store.job(reference)
        if job is None:
            raise HTTPException(404, "Purchase task not found")
        return job

    @app.post("/api/jobs/{reference}/resume", status_code=202)
    async def resume(reference: str) -> dict:
        job = store.job(reference)
        if not job:
            raise HTTPException(404, "Purchase task not found")
        if not store.requeue(reference):
            raise HTTPException(409, "Only failed or interrupted tasks can be resumed")
        launch(PurchaseRequest.model_validate(job["request"]))
        return {"reference": reference, "status": "queued"}

    @app.post("/api/jobs/{reference}/confirm", status_code=202)
    async def confirm(reference: str, body: ConfirmInput) -> dict:
        outcome = store.confirm_job(reference, body.plan_hash)
        if outcome == "not_found":
            raise HTTPException(404, "Purchase task not found")
        if outcome == "invalid_status":
            raise HTTPException(409, "Only tasks awaiting confirmation can be confirmed")
        if outcome == "hash_mismatch":
            raise HTTPException(409, "Purchase plan changed; re-confirmation required")
        job = store.job(reference)
        if job is None:
            raise HTTPException(404, "Purchase task not found")
        launch(PurchaseRequest.model_validate(job["request"]))
        return {"reference": reference, "status": "queued", "plan_hash": body.plan_hash}

    @app.get("/purchasing", response_class=HTMLResponse)
    def purchasing(reference: str = "", lose_response: bool = False) -> str:
        record = store.draft(reference) if reference else None
        fields = "".join(
            f'<label>{key}<input name="{key}" aria-label="{key}" required '
            + ('type="number" min="1"' if key == "quantity" else 'type="text"')
            + "></label>"
            for key in DraftInput.model_fields
        )
        details = (
            "<h2>Saved purchase DRAFT</h2><pre>" + escape(str(record)) + "</pre>"
            if record
            else "<p>No matching saved draft.</p>"
        )
        target = "/purchasing?lose_response=1" if lose_response else "/purchasing"
        return page(
            "采购草稿后台",
            "<p class='eyebrow'>SYNTHETIC ERP · DRAFTS ONLY</p>"
            "<h1>采购草稿 / Purchase draft</h1><p>This form creates a draft only. "
            "No payment or final purchase occurs.</p><section class='panel'>"
            f"<form method='post' action='{target}'>{fields}"
            "<button type='submit'>Save purchase DRAFT</button></form></section>"
            "<section class='panel'><h2>Look up an existing draft</h2>"
            "<form method='get'><label>Purchase reference<input name='reference' "
            "aria-label='lookup reference'></label><button>Look up draft</button></form>"
            + details
            + "</section>",
        )

    @app.post("/purchasing")
    async def save_draft(request: Request, lose_response: bool = False) -> RedirectResponse:
        try:
            payload = DraftInput.model_validate(dict(await request.form()))
            record = store.create_draft(payload.model_dump(mode="json"))
        except IntentConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, "Invalid purchase draft fields") from exc
        crash_point("after_draft_commit")
        if lose_response and not record["reused"]:
            raise HTTPException(
                503, "Simulated response loss after draft was saved; look up reference"
            )
        return RedirectResponse(f"/purchasing?reference={payload.reference}", status_code=303)

    @app.get("/api/drafts/{reference}")
    def draft(reference: str) -> dict:
        record = store.draft(reference)
        if record is None:
            raise HTTPException(404, "Draft not found")
        return record

    return app


app = create_app()
