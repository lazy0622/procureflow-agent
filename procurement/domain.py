"""Deterministic procurement rules, independent of model and browser execution."""

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PurchaseRequest(BaseModel):
    """A bounded purchase intent in the synthetic demo."""

    model_config = ConfigDict(extra="forbid")
    reference: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    sku: str = Field(min_length=1, max_length=80)
    quantity: int = Field(strict=True, gt=0, le=10000)
    currency: Literal["CNY", "USD", "EUR"] = "CNY"
    max_lead_days: int = Field(strict=True, ge=0, le=365)
    as_of: date = date(2026, 11, 11)
    scenario: Literal[
        "standard", "expired", "shortage", "mismatch", "missing_currency", "response_loss"
    ] = "standard"
    policy: Literal["verified", "model_only"] = "verified"
    layout: Literal["table", "cards"] = "table"


class Offer(BaseModel):
    """An observed quotation; missing or invalid fields require review."""

    model_config = ConfigDict(extra="forbid")
    quote_id: str = Field(min_length=1, max_length=80)
    supplier: str = Field(min_length=1, max_length=80)
    sku: str = Field(min_length=1, max_length=80)
    description: str = Field(max_length=500)
    price: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=14, decimal_places=4)
    currency: str | None
    unit: Literal["piece", "box"]
    pack_size: int = Field(strict=True, ge=1, le=10000)
    stock: int = Field(strict=True, ge=0)
    lead_days: int = Field(strict=True, ge=0)
    valid_until: date


class OfferDecision(BaseModel):
    """Explain whether an observed quotation satisfies the purchase intent."""

    quote_id: str
    eligible: bool
    reasons: list[str]
    total: Decimal | None = None


class PurchasePlan(BaseModel):
    """The exact expected contents of the target purchase draft."""

    reference: str
    quote_id: str
    supplier: str
    sku: str
    quantity: int
    currency: str
    total: Decimal


def assess_offer(request: PurchaseRequest, offer: Offer) -> OfferDecision:
    """Apply explicit SKU, currency, unit, stock, lead-time and expiry rules."""
    reasons = []
    if offer.sku != request.sku:
        reasons.append("sku_mismatch")
    if offer.currency != request.currency:
        reasons.append("currency_missing" if not offer.currency else "currency_mismatch")
    if offer.valid_until < request.as_of:
        reasons.append("quote_expired")
    if offer.stock < request.quantity:
        reasons.append("insufficient_stock")
    if offer.lead_days > request.max_lead_days:
        reasons.append("delivery_too_late")
    if offer.unit == "piece" and offer.pack_size != 1:
        reasons.append("inconsistent_unit")
    if request.quantity % offer.pack_size:
        reasons.append("fractional_pack")
    total = (offer.price * request.quantity / offer.pack_size).quantize(Decimal("0.01"))
    return OfferDecision(
        quote_id=offer.quote_id,
        eligible=not reasons,
        reasons=reasons,
        total=total if not reasons else None,
    )


def choose_offer(
    request: PurchaseRequest, observations: list[dict[str, Any]]
) -> tuple[PurchasePlan | None, list[OfferDecision]]:
    """Choose the cheapest eligible quotation, preserving rejection explanations."""
    decisions = []
    eligible: list[tuple[Decimal, Offer]] = []
    seen: set[str] = set()
    for item in observations:
        identifier = str(item.get("quote_id", "unidentified"))
        try:
            offer = Offer.model_validate(item)
        except ValidationError:
            decisions.append(
                OfferDecision(quote_id=identifier, eligible=False, reasons=["invalid_observation"])
            )
            continue
        if offer.quote_id in seen:
            # Duplicated identity is ambiguous even if one copy looks eligible.
            return None, decisions + [
                OfferDecision(
                    quote_id=offer.quote_id, eligible=False, reasons=["duplicate_quote_identity"]
                )
            ]
        seen.add(offer.quote_id)
        decision = assess_offer(request, offer)
        decisions.append(decision)
        if decision.eligible and decision.total is not None:
            eligible.append((decision.total, offer))
    if not eligible:
        return None, decisions
    total, selected = min(eligible, key=lambda pair: (pair[0], pair[1].quote_id))
    return PurchasePlan(
        reference=request.reference,
        quote_id=selected.quote_id,
        supplier=selected.supplier,
        sku=request.sku,
        quantity=request.quantity,
        currency=request.currency,
        total=total,
    ), decisions


def verify_draft(plan: PurchasePlan, record: dict[str, Any]) -> list[str]:
    """Compare a read-back business record with the expected purchase plan."""
    errors = []
    for key in ("reference", "quote_id", "supplier", "sku", "quantity", "currency"):
        if record.get(key) != getattr(plan, key) or (
            key == "quantity" and type(record.get(key)) is not int
        ):
            errors.append(f"draft_{key}_mismatch")
    try:
        actual_total = Decimal(str(record.get("total")))
        if not actual_total.is_finite() or actual_total != plan.total:
            errors.append("draft_total_mismatch")
    except (ValueError, ArithmeticError):
        errors.append("draft_total_invalid")
    return errors
