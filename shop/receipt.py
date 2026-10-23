"""Текстовый чек заказа."""
from shop.money import rub
from shop.pricing import is_gold, line_total, price_breakdown

VAT_PERCENT = 20


def vat_included(total_cents):
    """НДС, входящий в сумму: total x 20 / 120, до копейки; половина копейки — вверх."""
    numerator = total_cents * VAT_PERCENT
    denominator = 100 + VAT_PERCENT
    return (2 * numerator + denominator) // (2 * denominator)


def format_receipt(items, customer=None, promo_code=None, *, delivery="pickup"):
    parts = price_breakdown(items, customer, promo_code=promo_code, delivery=delivery)
    lines = [f"{item.title} ({item.sku}) x {item.qty}: {rub(line_total(item))}" for item in items]
    if is_gold(customer):
        lines.append("Скидка клиента: −" + rub(parts["customer_discount"]))
    if promo_code is not None:
        lines.append(f"Промокод {promo_code}: −{rub(parts['promo_discount'])}")
    if delivery == "courier":
        lines.append("Доставка: " + rub(parts["delivery"]))
    lines.append("К оплате: " + rub(parts["total"]))
    lines.append(f"в т.ч. НДС {VAT_PERCENT} %: {rub(vat_included(parts['total']))}")
    return "\n".join(lines)
