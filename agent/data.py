"""The fake world the reference agent operates on.

Small and in-repo on purpose. The agent under test has to be *non-trivial*
(multi-tool, one retrieval hop) but it does not have to be *real* -- what the
gate measures is the agent's trajectory and token cost, and a deterministic
local dataset keeps every run-to-run difference attributable to the prompt
rather than to a flaky backend.
"""

from __future__ import annotations

ORDERS: dict[str, dict] = {
    "8841": {
        "order_id": "8841",
        "status": "in_transit",
        "shipped_days_ago": 20,
        "carrier": "Northwind Freight",
        "items": [{"sku": "TENT-2P", "name": "Ridgeline 2P Tent", "qty": 1}],
        "total_usd": 289.00,
    },
    "8842": {
        "order_id": "8842",
        "status": "delivered",
        "shipped_days_ago": 45,
        "carrier": "Northwind Freight",
        "items": [{"sku": "BOOT-11", "name": "Cascade Hiking Boot 11", "qty": 1}],
        "total_usd": 165.00,
    },
    "8843": {
        "order_id": "8843",
        "status": "processing",
        "shipped_days_ago": 0,
        "carrier": None,
        "items": [{"sku": "PACK-65", "name": "Traverse 65L Pack", "qty": 2}],
        "total_usd": 420.00,
    },
    "8844": {
        "order_id": "8844",
        "status": "delivered",
        "shipped_days_ago": 8,
        "carrier": "Northwind Freight",
        "items": [{"sku": "STOVE-X", "name": "Ember X Camp Stove", "qty": 1}],
        "total_usd": 74.50,
    },
}

INVENTORY: dict[str, dict] = {
    "TENT-2P": {"sku": "TENT-2P", "on_hand": 12, "warehouse": "SEA-1", "backordered": False},
    "BOOT-11": {"sku": "BOOT-11", "on_hand": 0, "warehouse": "SEA-1", "backordered": True},
    "PACK-65": {"sku": "PACK-65", "on_hand": 3, "warehouse": "DEN-2", "backordered": False},
    "STOVE-X": {"sku": "STOVE-X", "on_hand": 47, "warehouse": "DEN-2", "backordered": False},
}

# The retrieval corpus. `policy_search` and the grounding hop both read this.
POLICIES: list[dict] = [
    {
        "id": "refund-window",
        "title": "Refund window",
        "text": (
            "Refunds are available within 30 days of the ship date. Orders shipped "
            "more than 30 days ago are eligible for store credit only, not a refund."
        ),
        "keywords": ["refund", "return", "money back", "30 days", "credit"],
    },
    {
        "id": "shipping-times",
        "title": "Shipping times",
        "text": (
            "Standard shipping is 5-7 business days. An order in the in_transit state "
            "has left the warehouse; processing means it has not shipped yet."
        ),
        "keywords": ["shipping", "delivery", "transit", "where", "arrive", "status"],
    },
    {
        "id": "backorder",
        "title": "Backorders",
        "text": (
            "A SKU with zero on_hand and backordered=true restocks in 2-3 weeks. "
            "Customers may cancel a backordered line at any time for a full refund."
        ),
        "keywords": ["backorder", "stock", "inventory", "out of stock", "restock"],
    },
    {
        "id": "exchange",
        "title": "Exchanges",
        "text": (
            "Exchanges are accepted within 60 days if the replacement SKU is in stock. "
            "If the replacement is backordered, we issue a refund instead."
        ),
        "keywords": ["exchange", "swap", "replace", "different size"],
    },
    {
        "id": "damaged",
        "title": "Damaged goods",
        "text": (
            "Damaged-on-arrival items are refunded in full regardless of the 30-day "
            "refund window. No return shipment is required."
        ),
        "keywords": ["damaged", "broken", "defective", "arrived damaged"],
    },
]


def search_policies(query: str, limit: int = 2) -> list[dict]:
    """Deterministic keyword retrieval. Not a vector DB -- and doesn't need to be.

    The gate counts retrieval *hops*, not retrieval quality, so a scored
    keyword match is the honest amount of machinery for this job.
    """
    q = query.lower()
    scored: list[tuple[int, dict]] = []
    for doc in POLICIES:
        score = sum(1 for kw in doc["keywords"] if kw in q)
        score += 2 if doc["title"].lower() in q else 0
        if score:
            scored.append((score, doc))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    hits = [doc for _, doc in scored[:limit]]
    if not hits:
        # Always ground on something: an empty hop is worse than a weak one.
        hits = [POLICIES[0]]
    return hits


def lookup_order(order_id: str) -> dict:
    order = ORDERS.get(str(order_id).strip())
    if order is None:
        return {"error": f"no order {order_id!r}", "known_orders": sorted(ORDERS)}
    return order


def check_inventory(sku: str) -> dict:
    item = INVENTORY.get(str(sku).strip().upper())
    if item is None:
        return {"error": f"no sku {sku!r}", "known_skus": sorted(INVENTORY)}
    return item
