import unittest

from shop.models import Customer, Item
from shop.pricing import calculate_total
from shop.receipt import format_receipt

GOLD = Customer("g1", "gold")


class CombinationTests(unittest.TestCase):
    """Решения тимлида по сочетаниям фич раунда 1 (их не задавало ни одно задание по отдельности)."""

    def test_gold_and_save10_are_both_taken_from_the_subtotal(self):
        self.assertEqual(calculate_total([Item("a", "Кофе", 100000)], GOLD, promo_code="SAVE10"), 85000)

    def test_gold_and_minus500_never_go_below_zero(self):
        self.assertEqual(calculate_total([Item("a", "Кофе", 40000)], GOLD, promo_code="MINUS500"), 0)

    def test_promo_does_not_reduce_courier_fee(self):
        self.assertEqual(calculate_total([Item("a", "Чай", 40000)], promo_code="MINUS500", delivery="courier"), 30000)

    def test_free_delivery_threshold_uses_the_subtotal_before_discounts(self):
        items = [Item("a", "Набор", 300000)]
        self.assertEqual(calculate_total(items, GOLD, promo_code="SAVE10", delivery="courier"), 255000)

    def test_promo_code_is_positional_as_specified_and_delivery_is_keyword_only(self):
        self.assertEqual(calculate_total([Item("a", "Чай", 15000)], GOLD, "SAVE10"), 12750)
        with self.assertRaises(TypeError):
            calculate_total([Item("a", "Чай", 15000)], GOLD, None, "courier")

    def test_receipt_lines_add_up_to_the_total(self):
        text = format_receipt([Item("a", "Кофе", 100000)], GOLD, promo_code="SAVE10", delivery="courier")
        self.assertEqual(text, "Кофе (a) × 1: 1 000,00 ₽\nСкидка клиента: −50,00 ₽\nПромокод SAVE10: −100,00 ₽\n"
                               "Доставка: 300,00 ₽\nИтого: 1 150,00 ₽\nв т.ч. НДС 20 %: 191,67 ₽")
