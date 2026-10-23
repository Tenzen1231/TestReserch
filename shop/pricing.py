"""Расчёт суммы заказа. Все суммы — целые копейки."""

COURIER_FEE_CENTS = 30000
FREE_COURIER_FROM_CENTS = 300000
DELIVERY_METHODS = ("pickup", "courier")


def line_total(item):
    return item.price_cents * item.qty


def subtotal(items):
    return sum(line_total(item) for item in items)


def promo_discount(amount, promo_code):
    """Скидка по промокоду в копейках; не больше суммы amount."""
    if promo_code is None:
        return 0
    if promo_code == "SAVE10":
        return amount // 10
    if promo_code == "MINUS500":
        return min(50000, amount)
    raise ValueError(f"неизвестный промокод: {promo_code}")


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


def calculate_total(items, customer=None, promo_code=None, delivery="pickup"):
    """Итоговая сумма заказа в копейках."""
    if not items:
        raise ValueError("пустой заказ")
    amount = subtotal(items) - customer_discount(items, customer)
    amount -= promo_discount(amount, promo_code)
    return amount + delivery_cost(items, delivery)
