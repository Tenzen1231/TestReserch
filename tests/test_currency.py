import unittest

from shop.currency import CURRENCY


class CurrencyTests(unittest.TestCase):
    def test_currency_is_rub(self):
        self.assertEqual(CURRENCY, "RUB")
