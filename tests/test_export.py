import json
import unittest

from shop.export import export_json
from shop.models import Customer, Item
from shop.pricing import price_breakdown
from shop.receipt import vat_included


class ExportJsonTests(unittest.TestCase):
    def test_simple_order(self):
        data = json.loads(export_json([Item("a", "Чай", 15000, 2)]))
        self.assertEqual(data, {
            "currency": "RUB",
            "items": [{"sku": "a", "title": "Чай", "price_cents": 15000, "qty": 2}],
            "subtotal_cents": 30000,
            "customer_discount_cents": 0,
            "promo_code": None,
            "promo_discount_cents": 0,
            "delivery": "pickup",
            "delivery_cents": 0,
            "total_cents": 30000,
            "vat_percent": 20,
            "vat_cents": 5000,
        })

    def test_returns_string_without_ascii_escapes(self):
        text = export_json([Item("a", "Чай", 15000)])
        self.assertIsInstance(text, str)
        self.assertIn("Чай", text)
        self.assertNotIn(r"\u", text)

    def test_gold_promo_and_courier_are_separate_fields(self):
        data = json.loads(export_json([Item("a", "Кофе", 100000)], Customer("g1", "gold"),
                                      promo_code="SAVE10", delivery="courier"))
        self.assertEqual(data["subtotal_cents"], 100000)
        self.assertEqual(data["customer_discount_cents"], 5000)
        self.assertEqual(data["promo_code"], "SAVE10")
        self.assertEqual(data["promo_discount_cents"], 10000)
        self.assertEqual(data["delivery"], "courier")
        self.assertEqual(data["delivery_cents"], 30000)
        self.assertEqual(data["total_cents"], 115000)
        self.assertEqual(data["vat_cents"], 19167)

    def test_amounts_match_project_calculations(self):
        items = [Item("a", "Ноутбук", 1234567), Item("b", "Мышь", 99900, 3)]
        customer = Customer("g2", "gold")
        data = json.loads(export_json(items, customer, "MINUS500", delivery="courier"))
        parts = price_breakdown(items, customer, promo_code="MINUS500", delivery="courier")
        self.assertEqual(data["subtotal_cents"], parts["subtotal"])
        self.assertEqual(data["customer_discount_cents"], parts["customer_discount"])
        self.assertEqual(data["promo_discount_cents"], parts["promo_discount"])
        self.assertEqual(data["delivery_cents"], parts["delivery"])
        self.assertEqual(data["total_cents"], parts["total"])
        self.assertEqual(data["vat_cents"], vat_included(parts["total"]))

    def test_items_keep_order_and_fields(self):
        items = [Item("b", "Мышь", 99900, 3), Item("a", "Чай", 15000)]
        data = json.loads(export_json(items))
        self.assertEqual([i["sku"] for i in data["items"]], ["b", "a"])
        self.assertEqual(data["items"][1], {"sku": "a", "title": "Чай", "price_cents": 15000, "qty": 1})

    def test_errors_come_from_pricing(self):
        with self.assertRaises(ValueError):
            export_json([])
        with self.assertRaises(ValueError):
            export_json([Item("a", "Чай", 15000)], promo_code="BAD")
        with self.assertRaises(ValueError):
            export_json([Item("a", "Чай", 15000)], delivery="drone")


if __name__ == "__main__":
    unittest.main()
