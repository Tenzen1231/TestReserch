import unittest

from shop.models import Item
from shop.pricing import calculate_total
from shop.receipt import format_receipt


class QuantityLimitTests(unittest.TestCase):
    def test_qty_99_is_allowed(self):
        self.assertEqual(calculate_total([Item("a", "Чай", 100, 99)]), 9900)

    def test_qty_above_99_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            calculate_total([Item("a", "Чай", 100, 100)])
        self.assertEqual(str(ctx.exception), "слишком много")

    def test_limit_applies_to_each_line(self):
        items = [Item("a", "Чай", 100, 2), Item("b", "Кофе", 100, 150)]
        with self.assertRaises(ValueError):
            calculate_total(items)

    def test_receipt_rejects_too_many(self):
        with self.assertRaises(ValueError):
            format_receipt([Item("a", "Чай", 100, 100)])


class ReceiptSkuTests(unittest.TestCase):
    def test_line_shows_sku(self):
        text = format_receipt([Item("a", "Чай", 15000, 2)])
        self.assertEqual(text, "Чай (a) x 2: 300,00 ₽\nК оплате: 300,00 ₽\nв т.ч. НДС 20 %: 50,00 ₽")

    def test_each_line_shows_its_own_sku(self):
        text = format_receipt([Item("a", "Чай", 15000, 2), Item("b", "Кофе", 40000)])
        self.assertEqual(text, "Чай (a) x 2: 300,00 ₽\nКофе (b) x 1: 400,00 ₽\nК оплате: 700,00 ₽\nв т.ч. НДС 20 %: 116,67 ₽")


if __name__ == "__main__":
    unittest.main()
