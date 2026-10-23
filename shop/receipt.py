"""Текстовый чек заказа."""
from shop.money import rub
from shop.pricing import is_gold, line_total, price_breakdown


def format_receipt(items, customer=None, promo_code=None, *, delivery="pickup"):
    parts = price_breakdown(items, customer, promo_code=promo_code, delivery=delivery)
    lines = [f"{item.title} × {item.qty}: {rub(line_total(item))}" for item in items]
    if is_gold(customer):
        lines.append("Скидка клиента: −" + rub(parts["customer_discount"]))
    if promo_code is not None:
        lines.append(f"Промокод {promo_code}: −{rub(parts['promo_discount'])}")
    if delivery == "courier":
        lines.append("Доставка: " + rub(parts["delivery"]))
    lines.append("Итого: " + rub(parts["total"]))
    return "\n".join(lines)
