import unittest

from shop.models import Item
from shop.receipt import format_money, format_receipt


class ReceiptTests(unittest.TestCase):
    def test_money_format(self):
        self.assertEqual(format_money(123450), "1 234,50 ₽")

    def test_receipt_lists_lines_and_total(self):
        text = format_receipt([Item("a", "Чай", 15000, 2)])
        self.assertEqual(text, "Чай × 2: 300,00 ₽\nИтого: 300,00 ₽")
