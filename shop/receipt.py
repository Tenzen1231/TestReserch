"""Текстовый чек заказа."""
from shop.pricing import is_gold, line_total, price_breakdown


def format_money(cents):
    rubles, kopecks = divmod(cents, 100)
    return f"{rubles:,}".replace(",", " ") + f",{kopecks:02d} ₽"


def format_receipt(items, customer=None, promo_code=None, *, delivery="pickup"):
    parts = price_breakdown(items, customer, promo_code=promo_code, delivery=delivery)
    lines = [f"{item.title} ({item.sku}) × {item.qty}: {format_money(line_total(item))}" for item in items]
    if is_gold(customer):
        lines.append("Скидка клиента: −" + format_money(parts["customer_discount"]))
    if promo_code is not None:
        lines.append(f"Промокод {promo_code}: −{format_money(parts['promo_discount'])}")
    if delivery == "courier":
        lines.append("Доставка: " + format_money(parts["delivery"]))
    lines.append("Итого: " + format_money(parts["total"]))
    return "\n".join(lines)
