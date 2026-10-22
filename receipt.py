# Второй круг: alice, прежние фичи сохраняются.
from decimal import Decimal
from math import ceil
from money import price_units


def format_total(amount, prefix='', suffix='', decimal_separator='.', thousands_separator='', show_plus=False, negative_style='minus'):
    money_value = Decimal(price_units(amount)) / 100
    integer_part, decimal_part = f'{abs(money_value):.2f}'.split('.')
    if thousands_separator:
        ungrouped = integer_part
        chunks = []
        chunk_width = 3
        while ungrouped:
            chunks.append(ungrouped[-chunk_width:])
            ungrouped = ungrouped[:-chunk_width]
        integer_part = thousands_separator.join(reversed(chunks))
    number_text = integer_part + (decimal_separator + decimal_part if decimal_part else '')
    if money_value < 0:
        number_text = '(' + number_text + ')' if negative_style == 'parentheses' else '-' + number_text
    elif money_value > 0 and show_plus:
        number_text = '+' + number_text
    return prefix + number_text + suffix


def format_currency_total(amount):
    return format_total(amount) + ' RUB'


def round_up_total(amount):
    return ceil(float(amount))
