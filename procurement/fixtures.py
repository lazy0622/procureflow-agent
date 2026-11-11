"""Synthetic supplier data; no real prices, suppliers, or purchase orders."""

from copy import deepcopy

OFFERS = [
    {
        "quote_id": "Q-NORTH-01",
        "supplier": "Northline",
        "sku": "BRG-6204",
        "description": "6204 bearing, sealed, 20mm bore",
        "price": "120.00",
        "currency": "CNY",
        "unit": "box",
        "pack_size": 10,
        "stock": 200,
        "lead_days": 3,
        "valid_until": "2026-12-31",
    },
    {
        "quote_id": "Q-HARBOR-01",
        "supplier": "Harbor",
        "sku": "BRG-6204",
        "description": "6204 bearing, sealed, 20mm bore",
        "price": "15.00",
        "currency": "CNY",
        "unit": "piece",
        "pack_size": 1,
        "stock": 80,
        "lead_days": 2,
        "valid_until": "2026-12-31",
    },
    {
        "quote_id": "Q-SUMMIT-01",
        "supplier": "Summit",
        "sku": "BRG-6204",
        "description": "6204 bearing, sealed, 20mm bore",
        "price": "8.00",
        "currency": "USD",
        "unit": "piece",
        "pack_size": 1,
        "stock": 100,
        "lead_days": 2,
        "valid_until": "2026-12-31",
    },
]


def offers_for(scenario: str) -> list[dict]:
    """Produce supplier-page variants without embedding expected decisions."""
    offers = deepcopy(OFFERS)
    if scenario == "expired":
        offers[0]["valid_until"] = "2026-10-01"
    elif scenario == "shortage":
        for offer in offers:
            offer["stock"] = 5
    elif scenario == "mismatch":
        offers[0]["sku"] = "BRG-6205"
        offers[0]["description"] = "6205 bearing, sealed, 25mm bore"
    elif scenario == "missing_currency":
        offers[0]["currency"] = None
    return offers
