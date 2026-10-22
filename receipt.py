from money import price_units


def format_total(amount):
    return f"{price_units(amount) / 100:.2f}"


def format_currency_total(amount):
    return format_total(amount) + ' RUB'
