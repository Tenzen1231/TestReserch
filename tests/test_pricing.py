import unittest

from shop.models import Customer, Item
from shop.pricing import calculate_total, subtotal


class PricingTests(unittest.TestCase):
    def test_subtotal_sums_lines(self):
        items = [Item("a", "Чай", 15000, 2), Item("b", "Кофе", 40000)]
        self.assertEqual(subtotal(items), 70000)

    def test_total_without_options_is_subtotal(self):
        self.assertEqual(calculate_total([Item("a", "Чай", 15000, 2)], Customer("c1")), 30000)

    def test_empty_order_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_total([])
