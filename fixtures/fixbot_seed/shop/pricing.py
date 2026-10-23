"""Расчёт суммы заказа. Все суммы — целые копейки."""


def line_total(item):
    return item.price_cents * item.qty


def subtotal(items):
    return sum(line_total(item) for item in items)


def calculate_total(items, customer=None):
    """Итоговая сумма заказа в копейках."""
    if not items:
        raise ValueError("пустой заказ")
    return subtotal(items)
