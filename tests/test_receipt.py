import unittest

from shop.models import Item
from shop.money import rub
from shop.receipt import format_receipt


class ReceiptTests(unittest.TestCase):
    def test_money_format(self):
        self.assertEqual(rub(123450), "1 234,50 ₽")

    def test_receipt_lists_lines_and_total(self):
        text = format_receipt([Item("a", "Чай", 15000, 2)])
        self.assertEqual(text, "Чай (a) x 2: 300,00 ₽\nК оплате: 300,00 ₽\nв т.ч. НДС 20 %: 50,00 ₽")
