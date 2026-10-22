def price_units(amount, fee_kopeks=0):
    return round(float(amount) * 100) + fee_kopeks
