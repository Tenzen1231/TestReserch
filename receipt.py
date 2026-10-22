# Второй круг: alice, прежние фичи сохраняются.
from decimal import Decimal
from math import ceil
from money import price_units


def format_total(amount, prefix='', suffix='', decimal_separator='.'):
    rounded_amount = Decimal(price_units(amount)) / 100
    whole_digits, fraction_digits = f'{abs(rounded_amount):.2f}'.split('.')
    formatted = whole_digits + (decimal_separator + fraction_digits if fraction_digits else '')
    if rounded_amount < 0:
        formatted = '-' + formatted
    return prefix + formatted + suffix


def format_currency_total(amount):
    return format_total(amount) + ' RUB'


def round_up_total(amount):
    return ceil(float(amount))
