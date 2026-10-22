# Второй круг: alice, прежние фичи сохраняются.
from decimal import Decimal
from math import ceil
from money import price_units


def format_total(amount, prefix='', suffix='', decimal_separator='.', trim_zeros=False, thousands_separator='', show_plus=False):
    rounded_amount = Decimal(price_units(amount)) / 100
    whole_digits, fraction_digits = f'{abs(rounded_amount):.2f}'.split('.')
    if thousands_separator:
        remaining_digits = whole_digits
        pieces = []
        chunk_size = 3
        while remaining_digits:
            pieces.append(remaining_digits[-chunk_size:])
            remaining_digits = remaining_digits[:-chunk_size]
        whole_digits = thousands_separator.join(reversed(pieces))
    if trim_zeros:
        fraction_digits = fraction_digits.rstrip('0')
    formatted = whole_digits + (decimal_separator + fraction_digits if fraction_digits else '')
    if rounded_amount < 0:
        formatted = '-' + formatted
    elif rounded_amount > 0 and show_plus:
        formatted = '+' + formatted
    return prefix + formatted + suffix


def format_currency_total(amount):
    return format_total(amount) + ' RUB'


def round_up_total(amount):
    return ceil(float(amount))
