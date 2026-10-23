import unittest

from shop.money import rub


class MoneyTests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(rub(0), "0,00 ₽")

    def test_kopecks_are_padded(self):
        self.assertEqual(rub(5), "0,05 ₽")
        self.assertEqual(rub(150), "1,50 ₽")

    def test_thousands_separated_by_space(self):
        self.assertEqual(rub(123450), "1 234,50 ₽")
        self.assertEqual(rub(123456789), "1 234 567,89 ₽")

    def test_old_name_is_gone(self):
        import shop.receipt
        self.assertFalse(hasattr(shop.receipt, "format_money"))


if __name__ == "__main__":
    unittest.main()
