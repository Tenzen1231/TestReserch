from money import price_units


def format_total(amount, suffix=''):
    return f"{price_units(amount) / 100:.2f}" + suffix


def format_currency_total(amount):
    return format_total(amount) + ' RUB'
