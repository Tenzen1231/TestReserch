# Второй круг: alice, прежние фичи сохраняются.
from decimal import Decimal
from math import ceil
from money import price_units


def format_total(amount, prefix='', suffix='', thousands_separator='', show_plus=False):
    money_value = Decimal(price_units(amount)) / 100
    integer_part, decimal_part = f'{abs(money_value):.2f}'.split('.')
    if thousands_separator:
        remaining = integer_part
        groups = []
        width = 3
        while remaining:
            groups.append(remaining[-width:])
            remaining = remaining[:-width]
        integer_part = thousands_separator.join(reversed(groups))
    number_text = integer_part + ('.' + decimal_part if decimal_part else '')
    if money_value < 0:
        number_text = '-' + number_text
    elif money_value > 0 and show_plus:
        number_text = '+' + number_text
    return prefix + number_text + suffix


def format_currency_total(amount):
    return format_total(amount) + ' RUB'


def round_up_total(amount):
    return ceil(float(amount))
