"""Расчёт суммы заказа. Все суммы — целые копейки."""


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


def calculate_total(items, customer=None, promo_code=None):
    """Итоговая сумма заказа в копейках."""
    if not items:
        raise ValueError("пустой заказ")
    amount = subtotal(items)
    return amount - promo_discount(amount, promo_code)
