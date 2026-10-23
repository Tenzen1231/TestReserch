import unittest

from shop.models import Customer, Item
from shop.pricing import calculate_total
from shop.receipt import format_receipt


class DeliveryPricingTests(unittest.TestCase):
    def test_pickup_is_default_and_free(self):
        items = [Item("a", "Чай", 15000, 2)]
        self.assertEqual(calculate_total(items), 30000)
        self.assertEqual(calculate_total(items, delivery="pickup"), 30000)

    def test_courier_adds_fee_below_threshold(self):
        items = [Item("a", "Чай", 15000, 2)]
        self.assertEqual(calculate_total(items, Customer("c1"), delivery="courier"), 60000)

    def test_courier_just_below_threshold_is_paid(self):
        items = [Item("a", "Набор", 299999)]
        self.assertEqual(calculate_total(items, delivery="courier"), 329999)

    def test_courier_free_at_threshold(self):
        items = [Item("a", "Набор", 300000)]
        self.assertEqual(calculate_total(items, delivery="courier"), 300000)

    def test_courier_free_above_threshold(self):
        items = [Item("a", "Кофе", 200000, 2)]
        self.assertEqual(calculate_total(items, delivery="courier"), 400000)

    def test_unknown_delivery_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_total([Item("a", "Чай", 15000)], delivery="drone")


class DeliveryReceiptTests(unittest.TestCase):
    def test_pickup_receipt_unchanged(self):
        text = format_receipt([Item("a", "Чай", 15000, 2)], delivery="pickup")
        self.assertEqual(text, "Чай (a) × 2: 300,00 ₽\nИтого: 300,00 ₽")

    def test_courier_receipt_shows_fee_before_total(self):
        text = format_receipt([Item("a", "Чай", 15000, 2)], delivery="courier")
        self.assertEqual(text, "Чай (a) × 2: 300,00 ₽\nДоставка: 300,00 ₽\nИтого: 600,00 ₽")

    def test_courier_receipt_free_delivery(self):
        text = format_receipt([Item("a", "Набор", 300000)], delivery="courier")
        self.assertEqual(text, "Набор (a) × 1: 3 000,00 ₽\nДоставка: 0,00 ₽\nИтого: 3 000,00 ₽")

    def test_receipt_unknown_delivery_is_rejected(self):
        with self.assertRaises(ValueError):
            format_receipt([Item("a", "Чай", 15000)], delivery="drone")


if __name__ == "__main__":
    unittest.main()
