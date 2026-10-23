import unittest

from shop.models import Customer, Item
from shop.pricing import calculate_total
from shop.receipt import format_receipt

MESSAGE = "минимальная сумма заказа 100 ₽"


class MinimumOrderTests(unittest.TestCase):
    """Минимум 100 ₽ проверяется по подытогу товаров: до скидок и без доставки."""

    def test_exactly_100_rubles_is_allowed(self):
        self.assertEqual(calculate_total([Item("a", "Чай", 10000)]), 10000)

    def test_one_kopeck_below_minimum_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            calculate_total([Item("a", "Чай", 9999)])
        self.assertEqual(str(ctx.exception), MESSAGE)

    def test_several_lines_together_reach_minimum(self):
        self.assertEqual(calculate_total([Item("a", "Чай", 5000), Item("b", "Кофе", 2500, 2)]), 10000)

    def test_quantity_counts_towards_minimum(self):
        self.assertEqual(calculate_total([Item("a", "Спички", 2000, 5)]), 10000)
        with self.assertRaises(ValueError):
            calculate_total([Item("a", "Спички", 2000, 4)])

    def test_promo_after_minimum_may_drop_total_below_100(self):
        # подытог 105 ₽ >= 100 ₽, SAVE10 -> 94,50 ₽
        self.assertEqual(calculate_total([Item("a", "Чай", 10500)], promo_code="SAVE10"), 9450)

    def test_gold_discount_does_not_affect_minimum(self):
        # подытог 100 ₽, gold -5 % -> 95 ₽
        self.assertEqual(calculate_total([Item("a", "Чай", 10000)], Customer("g1", "gold")), 9500)

    def test_courier_fee_does_not_count_towards_minimum(self):
        # товары 50 ₽ + доставка 300 ₽: подытог всё равно меньше 100 ₽
        with self.assertRaises(ValueError) as ctx:
            calculate_total([Item("a", "Чай", 5000)], delivery="courier")
        self.assertEqual(str(ctx.exception), MESSAGE)


class MinimumReceiptTests(unittest.TestCase):
    def test_receipt_below_minimum_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            format_receipt([Item("a", "Чай", 9999)])
        self.assertEqual(str(ctx.exception), MESSAGE)

    def test_receipt_at_minimum(self):
        text = format_receipt([Item("a", "Чай", 10000)])
        self.assertEqual(text, "Чай (a) × 1: 100,00 ₽\nИтого: 100,00 ₽\nв т.ч. НДС 20 %: 16,67 ₽")


if __name__ == "__main__":
    unittest.main()
