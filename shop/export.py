"""Экспорт заказа в JSON. Все суммы — целые копейки из существующих расчётов."""
import json

from shop.currency import CURRENCY
from shop.pricing import price_breakdown
from shop.receipt import VAT_PERCENT, vat_included


def export_json(items, customer=None, promo_code=None, *, delivery="pickup"):
    """Заказ как JSON-строка: позиции, подытог, скидки, доставка, итог и входящий в него НДС."""
    parts = price_breakdown(items, customer, promo_code=promo_code, delivery=delivery)
    order = {
        "currency": CURRENCY,
        "items": [{"sku": item.sku, "title": item.title,
                   "price_cents": item.price_cents, "qty": item.qty} for item in items],
        "subtotal_cents": parts["subtotal"],
        "customer_discount_cents": parts["customer_discount"],
        "promo_code": promo_code,
        "promo_discount_cents": parts["promo_discount"],
        "delivery": delivery,
        "delivery_cents": parts["delivery"],
        "total_cents": parts["total"],
        "vat_percent": VAT_PERCENT,
        "vat_cents": vat_included(parts["total"]),
    }
    return json.dumps(order, ensure_ascii=False)
