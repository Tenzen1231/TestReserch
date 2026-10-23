import unittest

from shop.models import Customer, Item
from shop.pricing import calculate_total
from shop.receipt import format_receipt


class FreeSampleTests(unittest.TestCase):
    def test_single_sample_order_total_is_zero(self):
        self.assertEqual(calculate_total([Item("s", "Пробник", 0)]), 0)

    def test_sample_does_not_change_paid_total(self):
        items = [Item("a", "Чай", 15000, 2), Item("s", "Пробник", 0)]
        self.assertEqual(calculate_total(items), 30000)

    def test_samples_only_with_gold_and_promo_stay_zero(self):
        items = [Item("s", "Пробник", 0, 3)]
        self.assertEqual(calculate_total(items, Customer("g1", "gold"), "MINUS500"), 0)

    def test_samples_only_receipt_is_printed(self):
        text = format_receipt([Item("s", "Пробник", 0)])
        self.assertEqual(text, "Пробник (s) × 1: 0,00 ₽\nИтого к оплате: 0,00 ₽\nв т.ч. НДС 20 %: 0,00 ₽")

    def test_negative_price_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            calculate_total([Item("x", "Брак", -1)])
        self.assertEqual(str(ctx.exception), "цена не может быть отрицательной")

    def test_negative_price_rejected_in_receipt(self):
        with self.assertRaises(ValueError):
            format_receipt([Item("s", "Пробник", 0), Item("x", "Брак", -100)])


if __name__ == "__main__":
    unittest.main()
