# Второй круг: alice, прежние фичи сохраняются.
from decimal import Decimal
from math import ceil
from money import price_units


def format_total(amount, prefix='', suffix='', thousands_separator=''):
    value = Decimal(price_units(amount)) / 100
    integral, fraction = f'{abs(value):.2f}'.split('.')
    if thousands_separator:
        remaining = integral
        groups = []
        width = 3
        while remaining:
            groups.append(remaining[-width:])
            remaining = remaining[:-width]
        integral = thousands_separator.join(reversed(groups))
    rendered = integral + ('.' + fraction if fraction else '')
    if value < 0:
        rendered = '-' + rendered
    return prefix + rendered + suffix


def format_currency_total(amount):
    return format_total(amount) + ' RUB'


def round_up_total(amount):
    return ceil(float(amount))
