"""Текстовый чек заказа."""
from shop.pricing import is_gold, line_total, price_breakdown

VAT_PERCENT = 20


def format_money(cents):
    rubles, kopecks = divmod(cents, 100)
    return f"{rubles:,}".replace(",", " ") + f",{kopecks:02d} ₽"


def vat_included(total_cents):
    """НДС, входящий в сумму: total × 20 / 120, до копейки; половина копейки — вверх."""
    numerator = total_cents * VAT_PERCENT
    denominator = 100 + VAT_PERCENT
    return (2 * numerator + denominator) // (2 * denominator)


def format_receipt(items, customer=None, promo_code=None, *, delivery="pickup"):
    parts = price_breakdown(items, customer, promo_code=promo_code, delivery=delivery)
    lines = [f"{item.title} × {item.qty}: {format_money(line_total(item))}" for item in items]
    if is_gold(customer):
        lines.append("Скидка клиента: −" + format_money(parts["customer_discount"]))
    if promo_code is not None:
        lines.append(f"Промокод {promo_code}: −{format_money(parts['promo_discount'])}")
    if delivery == "courier":
        lines.append("Доставка: " + format_money(parts["delivery"]))
    lines.append("Итого: " + format_money(parts["total"]))
    lines.append(f"в т.ч. НДС {VAT_PERCENT} %: {format_money(vat_included(parts['total']))}")
    return "\n".join(lines)
