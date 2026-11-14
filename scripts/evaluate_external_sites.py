"""Validate read-only extraction on external public sandbox pages against live HTML oracle."""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from procurement.agent import SkyvernClient

EXTERNAL_PAGES = [
    {
        "id": "ext-books-mystery",
        "url": "https://books.toscrape.com/catalogue/category/books/mystery_3/index.html",
        "limit": 5,
    },
    {
        "id": "ext-books-travel",
        "url": "https://books.toscrape.com/catalogue/category/books/travel_2/index.html",
        "limit": 5,
    },
]


class ExternalBookItem(BaseModel):
    """Schema for read-only book extraction on external sandbox pages."""

    title: str
    price_gbp: Decimal
    in_stock: bool


class ExternalBookBatch(BaseModel):
    """Top-N extracted items from one public category page."""

    items: list[ExternalBookItem]


async def fetch_html_oracle(url: str, limit: int) -> list[dict]:
    """Build ground truth directly from live page HTML before agent execution."""
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url)
        response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    expected = []
    for pod in soup.select("article.product_pod")[:limit]:
        anchor = pod.select_one("h3 a")
        price_text = pod.select_one("p.price_color").get_text(strip=True)
        stock_text = pod.select_one("p.instock").get_text(strip=True)
        expected.append(
            {
                "full_title": str(anchor.get("title") or "").strip(),
                "display_title": anchor.get_text(strip=True),
                "price_gbp": str(Decimal(price_text.lstrip("£")).quantize(Decimal("0.01"))),
                "in_stock": "in stock" in stock_text.lower(),
            }
        )
    return expected


def normalize_title(text: str) -> str:
    return " ".join(text.replace("...", "").replace("…", "").split()).lower()


def titles_match(extracted: str, oracle_row: dict) -> bool:
    got = normalize_title(extracted)
    if not got:
        return False
    full = normalize_title(oracle_row["full_title"])
    disp = normalize_title(oracle_row["display_title"])
    return got == full or got == disp or (len(got) >= 8 and full.startswith(got))


def grade_extraction(extracted: list[dict], oracle: list[dict]) -> tuple[bool, list[str]]:
    errors = []
    if len(extracted) != len(oracle):
        return False, [f"count_mismatch:{len(extracted)}!={len(oracle)}"]
    for idx, (got, want) in enumerate(zip(extracted, oracle), start=1):
        if not titles_match(str(got.get("title") or ""), want):
            errors.append(f"row_{idx}_title_mismatch:{got.get('title')}")
        try:
            got_price = str(Decimal(str(got.get("price_gbp"))).quantize(Decimal("0.01")))
        except (InvalidOperation, ValueError):
            got_price = str(got.get("price_gbp"))
        if got_price != want["price_gbp"]:
            errors.append(f"row_{idx}_price_mismatch:{got_price}!={want['price_gbp']}")
        if bool(got.get("in_stock")) is not want["in_stock"]:
            errors.append(f"row_{idx}_stock_mismatch")
    return not errors, errors


async def fixed_table_playwright(url: str, limit: int) -> list[dict]:
    """Reuse the exact fixed table-adapter assumption from the synthetic portal."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=20000)
            return await page.locator("table").evaluate(
                f"table => [...table.rows].slice(1, {limit + 1}).map(r => r.textContent)",
                timeout=5000,
            )
        finally:
            await browser.close()


async def skyvern_extract(url: str, limit: int) -> tuple[dict, list[dict]]:
    """Run read-only Skyvern extraction with no site-specific CSS selector."""
    client = SkyvernClient()
    prompt = (
        f"Read-only extraction task. Do NOT click Add to basket or navigate away. "
        f"Extract the first {limit} books shown in order on this page. "
        "For each book, return title (full book title if available, otherwise displayed title), "
        "price_gbp (numeric value without currency symbol), and in_stock (boolean)."
    )
    run = await client.run(prompt, url, ExternalBookBatch.model_json_schema(), max_steps=6)
    output = run.get("output")
    if isinstance(output, str):
        output = json.loads(output)
    if not output:
        return run, []
    batch = ExternalBookBatch.model_validate(output)
    items = [
        {
            "title": item.title,
            "price_gbp": str(item.price_gbp.quantize(Decimal("0.01"))),
            "in_stock": item.in_stock,
        }
        for item in batch.items[:limit]
    ]
    return run, items


async def run(args: argparse.Namespace) -> None:
    """Execute external read-only validation and persist raw outputs."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "procurement/evaluation/results" / (stamp + "-external-sites")
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for case in EXTERNAL_PAGES[: args.limit]:
        oracle = await fetch_html_oracle(case["url"], case["limit"])
        for strategy in args.strategies:
            started = time.monotonic()
            record = {
                "case_id": case["id"],
                "url": case["url"],
                "strategy": strategy,
                "oracle": oracle,
            }
            try:
                if strategy == "playwright_fixed_table":
                    extracted = await fixed_table_playwright(case["url"], case["limit"])
                    passed, errors = grade_extraction(extracted, oracle)
                    record.update(extracted=extracted, correct=passed, errors=errors)
                else:
                    run_meta, extracted = await skyvern_extract(case["url"], case["limit"])
                    passed, errors = grade_extraction(extracted, oracle)
                    record.update(
                        run_meta=run_meta, extracted=extracted, correct=passed, errors=errors
                    )
            except Exception as exc:  # noqa: BLE001 -- preserve external failure evidence
                record.update(correct=False, failure=type(exc).__name__, errors=[str(exc)])
            record["seconds"] = round(time.monotonic() - started, 3)
            records.append(record)
            (output / f"{case['id']}-{strategy}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {
                        "case_id": record["case_id"],
                        "strategy": strategy,
                        "correct": record["correct"],
                        "seconds": record["seconds"],
                        "errors": record.get("errors", []),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    summary = {
        "scope": "Read-only public sandbox pages (books.toscrape.com); no purchasing writes",
        "strategies": {
            strategy: {
                "passed": sum(r["correct"] for r in records if r["strategy"] == strategy),
                "trials": sum(r["strategy"] == strategy for r in records),
                "mean_seconds": round(
                    sum(r["seconds"] for r in records if r["strategy"] == strategy)
                    / sum(r["strategy"] == strategy for r in records),
                    3,
                ),
            }
            for strategy in args.strategies
        },
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(str(output), json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strategies",
        nargs="+",
        choices=["playwright_fixed_table", "skyvern"],
        default=["playwright_fixed_table", "skyvern"],
    )
    parser.add_argument("--limit", type=int, default=2)
    asyncio.run(run(parser.parse_args()))
