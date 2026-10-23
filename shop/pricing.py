"""Расчёт суммы заказа. Все суммы — целые копейки."""

COURIER_FEE_CENTS = 30000
FREE_COURIER_FROM_CENTS = 300000
DELIVERY_METHODS = ("pickup", "courier")


def line_total(item):
    return item.price_cents * item.qty


def subtotal(items):
    return sum(line_total(item) for item in items)


def delivery_cost(items, delivery="pickup"):
    """Стоимость доставки в копейках; неизвестный способ — ValueError."""
    if delivery not in DELIVERY_METHODS:
        raise ValueError(f"неизвестный способ доставки: {delivery!r}")
    if delivery == "courier" and subtotal(items) < FREE_COURIER_FROM_CENTS:
        return COURIER_FEE_CENTS
    return 0


def calculate_total(items, customer=None, delivery="pickup"):
    """Итоговая сумма заказа в копейках."""
    if not items:
        raise ValueError("пустой заказ")
    return subtotal(items) + delivery_cost(items, delivery)
