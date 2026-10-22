# Второй круг: carol, прежние фичи сохраняются.
def price_units(amount, discount_percent=0, fee_kopeks=0):
    return round(float(amount) * 100 * (100 - discount_percent) / 100) + fee_kopeks
# Несовместимое требование проверяет остановку ремонта.
