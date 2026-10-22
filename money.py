def price_units(amount, discount_percent=0):
    return round(float(amount) * 100 * (100 - discount_percent) / 100)
