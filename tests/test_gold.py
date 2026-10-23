import unittest

from shop.models import Customer, Item
from shop.pricing import calculate_total, customer_discount
from shop.receipt import format_receipt


class GoldDiscountTests(unittest.TestCase):
    def test_gold_gets_five_percent_off(self):
        items = [Item("a", "Чай", 15000, 2)]
        self.assertEqual(calculate_total(items, Customer("g1", "gold")), 28500)

    def test_discount_kopecks_rounded_down_for_shop(self):
        items = [Item("a", "Булка", 1999)]
        # 1999 * 5 // 100 = 99 (а не 99,95 -> 100)
        self.assertEqual(customer_discount(items, Customer("g1", "gold")), 99)
        self.assertEqual(calculate_total(items, Customer("g1", "gold")), 1900)

    def test_regular_and_no_customer_without_discount(self):
        items = [Item("a", "Чай", 15000, 2)]
        self.assertEqual(calculate_total(items, Customer("c1")), 30000)
        self.assertEqual(calculate_total(items), 30000)
        self.assertEqual(customer_discount(items, Customer("c1")), 0)
        self.assertEqual(customer_discount(items), 0)

    def test_gold_receipt_has_discount_line_before_total(self):
        text = format_receipt([Item("a", "Чай", 15000, 2)], Customer("g1", "gold"))
        self.assertEqual(
            text,
            "Чай (a) x 2: 300,00 ₽\nСкидка клиента: −15,00 ₽\nИтого к оплате: 285,00 ₽\nв т.ч. НДС 20 %: 47,50 ₽",
        )

    def test_regular_receipt_unchanged(self):
        items = [Item("a", "Чай", 15000, 2)]
        expected = "Чай (a) x 2: 300,00 ₽\nИтого к оплате: 300,00 ₽\nв т.ч. НДС 20 %: 50,00 ₽"
        self.assertEqual(format_receipt(items, Customer("c1")), expected)
        self.assertEqual(format_receipt(items), expected)
