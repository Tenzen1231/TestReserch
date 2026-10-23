from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    sku: str
    title: str
    price_cents: int
    qty: int = 1


@dataclass(frozen=True)
class Customer:
    customer_id: str
    tier: str = "regular"
