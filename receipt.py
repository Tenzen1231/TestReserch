from decimal import Decimal
from math import ceil
from money import price_units


def format_total(amount, prefix=''):
    # Приставка сохраняется при обычном обновлении PR.
    return prefix + f"{Decimal(price_units(amount)) / 100:.2f}"


def format_currency_total(amount):
    return format_total(amount) + ' RUB'


def round_up_total(amount):
    return ceil(float(amount))
