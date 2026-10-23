"""Текстовый чек заказа."""
from shop.pricing import calculate_total, line_total, subtotal


def format_money(cents):
    rubles, kopecks = divmod(cents, 100)
    return f"{rubles:,}".replace(",", " ") + f",{kopecks:02d} ₽"


def format_receipt(items, customer=None, promo_code=None):
    lines = [f"{item.title} × {item.qty}: {format_money(line_total(item))}" for item in items]
    total = calculate_total(items, customer, promo_code)
    if promo_code is not None:
        discount = subtotal(items) - total
        lines.append(f"Промокод {promo_code}: −{format_money(discount)}")
    lines.append("Итого: " + format_money(total))
    return "\n".join(lines)
