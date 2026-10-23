import unittest

from shop.models import Customer, Item
from shop.pricing import calculate_total
from shop.receipt import format_receipt


class PromoPricingTests(unittest.TestCase):
    def test_save10_takes_ten_percent(self):
        self.assertEqual(calculate_total([Item("a", "Чай", 15000, 2)], promo_code="SAVE10"), 27000)

    def test_save10_rounds_discount_down(self):
        # подытог 999 коп. -> скидка 99 коп., итог 900
        self.assertEqual(calculate_total([Item("a", "Спички", 999)], promo_code="SAVE10"), 900)

    def test_minus500_subtracts_500_rubles(self):
        self.assertEqual(calculate_total([Item("b", "Кофе", 80000)], promo_code="MINUS500"), 30000)

    def test_minus500_total_not_below_zero(self):
        self.assertEqual(calculate_total([Item("a", "Чай", 15000)], promo_code="MINUS500"), 0)

    def test_unknown_code_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_total([Item("a", "Чай", 15000)], promo_code="FREE")

    def test_no_code_keeps_subtotal(self):
        self.assertEqual(calculate_total([Item("a", "Чай", 15000, 2)], Customer("c1")), 30000)


class PromoReceiptTests(unittest.TestCase):
    def test_receipt_shows_save10_line_before_total(self):
        text = format_receipt([Item("a", "Чай", 15000, 2)], promo_code="SAVE10")
        self.assertEqual(text, "Чай (a) x 2: 300,00 ₽\nПромокод SAVE10: −30,00 ₽\nИтого: 270,00 ₽\nв т.ч. НДС 20 %: 45,00 ₽")

    def test_receipt_shows_capped_minus500(self):
        text = format_receipt([Item("a", "Чай", 15000)], promo_code="MINUS500")
        self.assertEqual(text, "Чай (a) x 1: 150,00 ₽\nПромокод MINUS500: −150,00 ₽\nИтого: 0,00 ₽\nв т.ч. НДС 20 %: 0,00 ₽")

    def test_receipt_without_code_unchanged(self):
        text = format_receipt([Item("a", "Чай", 15000, 2)])
        self.assertEqual(text, "Чай (a) x 2: 300,00 ₽\nИтого: 300,00 ₽\nв т.ч. НДС 20 %: 50,00 ₽")


if __name__ == "__main__":
    unittest.main()
