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


GOLD_TIER = "gold"
GOLD_DISCOUNT_PERCENT = 5


def is_gold(customer):
    return customer is not None and customer.tier == GOLD_TIER


def customer_discount(items, customer=None):
    """Скидка клиента в копейках; копейки округляются вниз в пользу магазина."""
    if not is_gold(customer):
        return 0
    return subtotal(items) * GOLD_DISCOUNT_PERCENT // 100


def calculate_total(items, customer=None, delivery="pickup"):
    """Итоговая сумма заказа в копейках."""
    if not items:
        raise ValueError("пустой заказ")
    return subtotal(items) - customer_discount(items, customer) + delivery_cost(items, delivery)
