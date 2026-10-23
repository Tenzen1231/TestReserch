"""Расчёт суммы заказа. Все суммы — целые копейки."""


def line_total(item):
    return item.price_cents * item.qty


def subtotal(items):
    return sum(line_total(item) for item in items)


GOLD_TIER = "gold"
GOLD_DISCOUNT_PERCENT = 5


def is_gold(customer):
    return customer is not None and customer.tier == GOLD_TIER


def customer_discount(items, customer=None):
    """Скидка клиента в копейках; копейки округляются вниз в пользу магазина."""
    if not is_gold(customer):
        return 0
    return subtotal(items) * GOLD_DISCOUNT_PERCENT // 100


def calculate_total(items, customer=None):
    """Итоговая сумма заказа в копейках."""
    if not items:
        raise ValueError("пустой заказ")
    return subtotal(items) - customer_discount(items, customer)
