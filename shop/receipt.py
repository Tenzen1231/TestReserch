"""Текстовый чек заказа."""
from shop.pricing import calculate_total, delivery_cost, customer_discount, is_gold, line_total


def format_money(cents):
    rubles, kopecks = divmod(cents, 100)
    return f"{rubles:,}".replace(",", " ") + f",{kopecks:02d} ₽"


def format_receipt(items, customer=None, delivery="pickup"):
    lines = [f"{item.title} × {item.qty}: {format_money(line_total(item))}" for item in items]
    if is_gold(customer):
        lines.append("Скидка клиента: −" + format_money(customer_discount(items, customer)))
    total = calculate_total(items, customer, delivery)
    if delivery == "courier":
        lines.append("Доставка: " + format_money(delivery_cost(items, delivery)))
    lines.append("Итого: " + format_money(total))
    return "\n".join(lines)
