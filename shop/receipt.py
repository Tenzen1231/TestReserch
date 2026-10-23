"""Текстовый чек заказа."""
from shop.pricing import calculate_total, line_total


def format_money(cents):
    rubles, kopecks = divmod(cents, 100)
    return f"{rubles:,}".replace(",", " ") + f",{kopecks:02d} ₽"


def format_receipt(items, customer=None):
    lines = [f"{item.title} × {item.qty}: {format_money(line_total(item))}" for item in items]
    lines.append("Итого: " + format_money(calculate_total(items, customer)))
    return "\n".join(lines)
