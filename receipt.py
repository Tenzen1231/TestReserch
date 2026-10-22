# Второй круг: alice, прежние фичи сохраняются.
from decimal import Decimal
from math import ceil
from money import price_units


def format_total(amount, prefix='', suffix=''):
    return prefix + f"{Decimal(price_units(amount)) / 100:.2f}" + suffix


def format_currency_total(amount):
    return format_total(amount) + ' RUB'


def round_up_total(amount):
    return ceil(float(amount))
