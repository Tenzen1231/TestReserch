"""Trusted catalogue regression checks; no model output defines expectations."""

from contextlib import redirect_stdout
from decimal import Decimal
import hashlib
from io import StringIO
import itertools
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import acceptance


OLD = {"prefix", "suffix", "round-up", "discount", "fee", "currency"}
ROUNDS = (("decimal-separator", "thousands", "explicit-sign"),
          ("trim-zeros", "grouping-pattern", "negative-parentheses"),
          ("minimum-fraction", "grouping-threshold", "zero-text"))
NEW = set().union(*ROUNDS)
OPTION_CONTRACT = {"prefix": "prefix", "suffix": "suffix", "decimal_separator": "decimal-separator",
                   "thousands_separator": "thousands", "show_plus": "explicit-sign", "trim_zeros": "trim-zeros",
                   "grouping": "grouping-pattern", "negative_style": "negative-parentheses",
                   "min_fraction_digits": "minimum-fraction", "group_min_digits": "grouping-threshold",
                   "zero_text": "zero-text"}


def base_number(amount):
    return f"{Decimal(round(float(amount) * 100)) / 100:.2f}"


def old_formatter(amount, prefix="", suffix="", **options):
    return prefix + base_number(amount) + suffix


class FormattingCatalogueTests(unittest.TestCase):
    def selected(self, requirements):
        return {case[0]: case for case in acceptance.contract_cases(requirements)}

    def oracle(self, formatter, requirements, flags=(False, False, False)):
        # Independent old-feature fixture. These functions are not assembled from
        # the catalogue; the real oracle executes them and compares literal data.
        modules = {
            "money": SimpleNamespace(price_units=lambda amount, discount_percent=0, fee_kopeks=0:
                                     round(float(amount) * 100 * (100 - discount_percent) / 100) + fee_kopeks),
            "receipt": SimpleNamespace(format_total=formatter, round_up_total=lambda amount: math.ceil(float(amount)),
                                       format_currency_total=lambda amount: formatter(amount) + " RUB"),
            "email_receipt": SimpleNamespace(format_email_total=base_number),
            **{"feature_" + name: SimpleNamespace(ENABLED=value) for name, value in zip("abc", flags)},
        }
        output = StringIO()
        with patch.object(sys, "argv", ["acceptance.py", str(Path.cwd()), json.dumps(sorted(requirements))]), \
                patch.object(sys, "path", list(sys.path)), \
                patch.object(acceptance.importlib, "import_module", side_effect=modules.__getitem__), \
                redirect_stdout(output):
            code = acceptance.main()
        return code, json.loads(output.getvalue())

    def test_original_41_vectors_are_byte_for_byte_unchanged(self):
        cases = acceptance.contract_cases(OLD)
        self.assertEqual(41, len(cases))
        digest = hashlib.sha256(json.dumps(cases, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        # Captured before any new formatting catalogue was added.
        self.assertEqual("4110684bc090093ec6dcc2e0d3c519a620a304b17761ce6479b68615b39d4583", digest)
        self.assertEqual(cases, acceptance.contract_cases(OLD | NEW)[:41])

    def test_original_oracle_still_runs_all_42_checks(self):
        code, report = self.oracle(old_formatter, OLD)
        self.assertEqual(0, code)
        self.assertTrue(report["passed"])
        self.assertEqual(42, len(report["checks"]))
        self.assertEqual("three_way_interaction", report["checks"][-1]["check"])
        code, report = self.oracle(old_formatter, OLD, flags=(True, True, True))
        self.assertEqual(1, code)
        self.assertFalse(report["checks"][-1]["passed"])

    def test_every_option_declares_its_required_contract(self):
        self.assertEqual(OLD | NEW | {"legacy-rubles"}, set(acceptance.CONTRACTS))
        names = []
        for name, required, amount, options, expected in acceptance.FORMATTING_CASES:
            names.append(name)
            self.assertTrue(set(required) & NEW, name)
            self.assertTrue(set(required) <= OLD | NEW, name)
            self.assertTrue({OPTION_CONTRACT[key] for key in options} <= set(required), name)
            self.assertIsInstance(expected, str)
        self.assertEqual(len(names), len(set(names)))
        with self.assertRaises(ValueError):
            acceptance.contract_cases({"unknown-formatting-feature"})

    def test_each_round_has_independent_pair_and_triple_activation(self):
        pair_cases = (
            (("decimal-separator", "thousands"), "r1-decimal-thousands"),
            (("decimal-separator", "explicit-sign"), "r1-decimal-sign"),
            (("thousands", "explicit-sign"), "r1-thousands-sign"),
            (("trim-zeros", "grouping-pattern"), "r2-trim-grouping"),
            (("trim-zeros", "negative-parentheses"), "r2-trim-parentheses"),
            (("grouping-pattern", "negative-parentheses"), "r2-grouping-parentheses"),
            (("minimum-fraction", "grouping-threshold"), "r3-minimum-threshold"),
            (("minimum-fraction", "zero-text"), "r3-minimum-zero"),
            (("grouping-threshold", "zero-text"), "r3-threshold-zero"),
        )
        previous = set(OLD)
        for index, round_ids in enumerate(ROUNDS, 1):
            for order in itertools.permutations(round_ids):
                accepted = set(previous)
                old_cases = self.selected(accepted)
                for requirement in order:
                    accepted.add(requirement)
                    selected = self.selected(accepted)
                    self.assertEqual(old_cases, {name: selected[name] for name in old_cases})
                    for pair, name in pair_cases:
                        self.assertEqual(set(pair) <= accepted, "format:" + name in selected, (order, name))
                    triple = "format:r" + str(index) + ("-all-negative" if index == 3 else "-all")
                    self.assertEqual(set(round_ids) <= accepted, triple in selected, order)
                    old_cases = selected
            previous.update(round_ids)

    def test_no_new_option_leaks_into_any_partial_catalogue(self):
        for mask in range(1 << 9):
            accepted = OLD | {name for bit, name in enumerate(sum(ROUNDS, ())) if mask & (1 << bit)}
            cases = acceptance.contract_cases(accepted)
            self.assertEqual(len(cases), len({case[0] for case in cases}))
            for name, module, function, args, options, expected in cases:
                if name.startswith("format:"):
                    self.assertTrue({OPTION_CONTRACT[key] for key in options} <= accepted, name)

    def test_manually_reviewed_boundary_vectors(self):
        selected = self.selected(OLD | NEW)
        vectors = (
            ("decimal-empty", 12.34, {"decimal_separator": ""}, "1234"),
            ("sign-rounded-zero", 0.001, {"show_plus": True}, "0.00"),
            ("sign-rounded-negative-zero", -0.001, {"show_plus": True}, "0.00"),
            ("grouping-six", 123456, {"grouping": "3-2", "thousands_separator": ","}, "1,23,456.00"),
            ("minimum-one-padded", 12.3, {"min_fraction_digits": 2, "trim_zeros": True}, "12.30"),
            ("minimum-decimal-multi", 100,
             {"min_fraction_digits": 1, "trim_zeros": True, "decimal_separator": "::"}, "100::0"),
            ("threshold-excludes-minus", -1234.5, {"group_min_digits": 5, "thousands_separator": ","}, "-1234.50"),
            ("threshold-after-rounding", 999.999, {"group_min_digits": 4, "thousands_separator": ","}, "1,000.00"),
            ("threshold-indian-below", 123456.7,
             {"group_min_digits": 7, "grouping": "3-2", "thousands_separator": ","}, "123456.70"),
            ("zero-text-empty", 0, {"zero_text": ""}, ""),
            ("zero-text-rounded-negative", -0.001, {"zero_text": "free"}, "free"),
            ("zero-text-affixes-literal", -0.001,
             {"zero_text": "0.00+", "trim_zeros": True, "negative_style": "parentheses", "decimal_separator": ",",
              "thousands_separator": "_", "show_plus": True, "prefix": ".+", "suffix": ".-"}, ".+0.00+.-"),
        )
        for name, amount, options, expected in vectors:
            self.assertEqual(("format:" + name, "receipt", "format_total", [amount], options, expected),
                             selected["format:" + name])
        self.assertEqual("Итого (12.34.567,80) руб.", selected["format:r3-all-negative"][-1])
        self.assertEqual("Итого +1234567,80 руб.", selected["format:r3-all-below-threshold"][-1])
        self.assertEqual("Итого ZERO руб.", selected["format:r3-all-zero"][-1])
        self.assertEqual("Итого 0,00 руб.", selected["format:r3-all-zero-disabled"][-1])

    def test_global_decimal_replacement_is_rejected(self):
        def wrong(amount, prefix="", suffix="", decimal_separator="."):
            return (prefix + base_number(amount) + suffix).replace(".", decimal_separator)
        code, report = self.oracle(wrong, OLD | {"decimal-separator"})
        self.assertEqual(1, code)
        self.assertIn("format:decimal-affixes", {x["check"] for x in report["checks"] if not x["passed"]})

    def test_raw_amount_sign_is_rejected_for_monetary_zero(self):
        def wrong(amount, prefix="", suffix="", show_plus=False):
            return prefix + ("+" if show_plus and amount > 0 else "") + base_number(amount) + suffix
        code, report = self.oracle(wrong, OLD | {"explicit-sign"})
        self.assertEqual(1, code)
        self.assertIn("format:sign-rounded-zero", {x["check"] for x in report["checks"] if not x["passed"]})

    def test_trimming_rendered_string_is_rejected(self):
        def wrong(amount, prefix="", suffix="", trim_zeros=False, decimal_separator="."):
            rendered = prefix + base_number(amount).replace(".", decimal_separator) + suffix
            return rendered.rstrip("0").rstrip(".") if trim_zeros else rendered
        code, report = self.oracle(wrong, OLD | {"trim-zeros", "decimal-separator"})
        self.assertEqual(1, code)
        failed = {x["check"] for x in report["checks"] if not x["passed"]}
        self.assertTrue({"format:trim-decimal-empty-fraction", "format:trim-affixes"} <= failed)

    def test_raw_amount_zero_override_is_rejected(self):
        def wrong(amount, prefix="", suffix="", zero_text=None):
            return prefix + (zero_text if amount == 0 and zero_text is not None else base_number(amount)) + suffix
        code, report = self.oracle(wrong, OLD | {"zero-text"})
        self.assertEqual(1, code)
        failed = {x["check"] for x in report["checks"] if not x["passed"]}
        self.assertTrue({"format:zero-text-rounded", "format:zero-text-rounded-negative"} <= failed)

    def test_returned_options_are_not_shared_with_the_frozen_catalogue(self):
        case = self.selected(OLD | NEW)["format:decimal-comma"]
        case[4]["decimal_separator"] = "edited by caller"
        self.assertEqual(",", self.selected(OLD | NEW)["format:decimal-comma"][4]["decimal_separator"])


if __name__ == "__main__":
    unittest.main()
