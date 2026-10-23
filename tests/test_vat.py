import unittest

from shop.models import Customer, Item
from shop.receipt import format_receipt, vat_included


class VatAmountTests(unittest.TestCase):
    def test_vat_is_one_sixth_of_total(self):
        self.assertEqual(vat_included(12000), 2000)
        self.assertEqual(vat_included(0), 0)

    def test_half_kopeck_rounds_up(self):
        # 3 × 20 / 120 = 0,5 коп. -> 1; 9 -> 1,5 -> 2
        self.assertEqual(vat_included(3), 1)
        self.assertEqual(vat_included(9), 2)

    def test_below_half_rounds_down_above_half_rounds_up(self):
        # 100 -> 16,67 -> 17; 101 -> 16,83 -> 17; 98 -> 16,33 -> 16
        self.assertEqual(vat_included(100), 17)
        self.assertEqual(vat_included(101), 17)
        self.assertEqual(vat_included(98), 16)


class VatReceiptTests(unittest.TestCase):
    def test_vat_line_goes_right_after_total(self):
        text = format_receipt([Item("a", "Чай", 15000, 2)])
        self.assertEqual(text, "Чай × 2: 300,00 ₽\nИтого: 300,00 ₽\nв т.ч. НДС 20 %: 50,00 ₽")

    def test_vat_is_taken_from_final_total_with_discounts_and_delivery(self):
        text = format_receipt([Item("a", "Кофе", 100000)], Customer("g1", "gold"),
                              promo_code="SAVE10", delivery="courier")
        self.assertEqual(text.splitlines()[-2:], ["Итого: 1 150,00 ₽", "в т.ч. НДС 20 %: 191,67 ₽"])

    def test_vat_line_uses_money_format_with_thousands(self):
        text = format_receipt([Item("a", "Ноутбук", 1234567)])
        # 1 234 567 / 6 = 205 761,17 коп. -> 205 761 коп.
        self.assertEqual(text.splitlines()[-1], "в т.ч. НДС 20 %: 2 057,61 ₽")

    def test_half_kopeck_in_receipt_rounds_up(self):
        text = format_receipt([Item("a", "Спичка", 3)])
        self.assertEqual(text.splitlines()[-1], "в т.ч. НДС 20 %: 0,01 ₽")

    def test_zero_total_has_zero_vat(self):
        text = format_receipt([Item("a", "Чай", 15000)], promo_code="MINUS500")
        self.assertEqual(text.splitlines()[-1], "в т.ч. НДС 20 %: 0,00 ₽")


if __name__ == "__main__":
    unittest.main()
