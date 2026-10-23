"""Форматирование денежных сумм. Все суммы — целые копейки."""


def rub(cents):
    """Сумма в копейках как строка в рублях: 123450 -> "1 234,50 ₽"."""
    rubles, kopecks = divmod(cents, 100)
    return f"{rubles:,}".replace(",", " ") + f",{kopecks:02d} ₽"
