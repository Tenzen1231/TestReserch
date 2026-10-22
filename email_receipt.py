from money import price_units


def format_email_total(amount):
    return f"{price_units(amount):.2f}"
