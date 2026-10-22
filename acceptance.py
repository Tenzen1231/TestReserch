"""Trusted, self-contained contracts for the public collaboration experiment."""

import importlib
import json
from pathlib import Path
import sys


CONTRACTS = {
    "prefix": "receipt.format_total(amount, prefix='') prepends the exact prefix string to the original two-decimal amount. Preserve suffix if accepted, default formatting and every existing feature.",
    "suffix": "receipt.format_total(amount, suffix='') appends the exact suffix string to the original two-decimal amount. Preserve prefix if accepted; combined format_total(12.34,prefix='Итого ',suffix=' руб.') == 'Итого 12.34 руб.'. Preserve default formatting and every existing feature.",
    "round-up": "receipt.round_up_total(amount) returns ceil(float(amount)) as int: 12.34 -> 13, 12 -> 12, 0.01 -> 1, -12.34 -> -12. Preserve every existing receipt and pricing feature.",
    "discount": "price_units(amount, discount_percent=0): return integer kopeks after percent discount. Formula round(float(amount)*100*(100-discount_percent)/100). Preserve the default price and all existing APIs.",
    "fee": "price_units(amount, fee_kopeks=0): add an integer fee in kopeks AFTER rounding the discounted price. With discount accepted the joint formula is round(float(amount)*100*(100-discount_percent)/100)+fee_kopeks. Preserve all existing APIs.",
    "currency": "receipt.format_currency_total(amount) returns receipt.format_total(amount) + ' RUB'. Preserve the original format_total API and every accepted money feature.",
    "legacy-rubles": "Deliberately incompatible legacy request: money.price_units(amount) must return the numeric amount in RUBLES, so price_units(100)==100. Do not remove any previously accepted requirement. If incompatible, report no safe repair.",
    "decimal-separator": "receipt.format_total(amount, decimal_separator='.') replaces only the numeric decimal point with the exact supplied string, including empty or multi-character strings. Start with Decimal(price_units(amount))/100 rendered to two fractional digits. Never replace characters in prefix/suffix or a thousands separator. Preserve all accepted APIs and defaults.",
    "thousands": "receipt.format_total(amount, thousands_separator='') groups the absolute integer digits in threes from the right when the separator is nonempty, using its exact string. Empty means no grouping. Keep the fraction, sign and affixes separate; preserve decimal_separator, prefix/suffix and all accepted APIs.",
    "explicit-sign": "receipt.format_total(amount, show_plus=False) adds '+' only when the computed monetary value Decimal(price_units(amount))/100 is strictly positive. Negative values keep their negative sign; monetary zero, including amount 0.001, -0.001 and -0.0, never gets '+'. Place sign before the numeric block and inside prefix/suffix. Preserve every accepted feature.",
    "trim-zeros": "receipt.format_total(amount, trim_zeros=False) starts with the existing two-decimal monetary value. If true, remove only trailing zero digits from its fractional part; remove the decimal separator if no fraction remains. Never trim the integer, prefix or suffix. Preserve every previously accepted contract, including decimal-separator, thousands and explicit-sign.",
    "grouping-pattern": "receipt.format_total(amount, grouping='3') retains groups of three; grouping='3-2' uses three digits at the right and groups of two for the remaining integer digits. Group only the absolute integer part and only with a nonempty thousands_separator. Preserve every accepted contract and default, including the first round's formatting options.",
    "negative-parentheses": "receipt.format_total(amount, negative_style='minus') retains a leading minus; negative_style='parentheses' encloses a strictly negative monetary numeric block in parentheses instead. Positive values and monetary zero are not parenthesized. show_plus still applies only to positive money. Put prefix/suffix outside sign/parentheses. Preserve every accepted contract.",
    "minimum-fraction": "receipt.format_total(amount, min_fraction_digits=0) changes trimming only when trim_zeros=True: retain at least 0, 1 or 2 fractional digits after trimming; never remove significant fractional digits. When trim_zeros=False retain two digits. Apply decimal_separator only if a fraction remains. Preserve every first- and second-round accepted contract.",
    "grouping-threshold": "receipt.format_total(amount, group_min_digits=1) applies thousands grouping only when the rounded monetary integer magnitude has at least this many digits, before separators or sign are added. The threshold is a positive integer; it works with grouping='3' and '3-2'. Preserve every first- and second-round accepted contract and default.",
    "zero-text": "receipt.format_total(amount, zero_text=None) retains normal formatting by default. For computed monetary zero, including -0.0 and amounts +/-0.001, a supplied string replaces the entire numeric block and its sign exactly, including an empty string; prefix/suffix remain outside it. Do not process separators, trimming, plus or parentheses inside zero_text. Nonzero money is unaffected. Preserve all accepted contracts.",
}
REGISTRY_PATH = ".queue/requirements.json"


# (name, required contracts, amount, options, literal expected result).
# These vectors are fixed before the live experiment. A combined vector becomes
# active only after all its contracts are accepted, so PR ordering is arbitrary.
FORMATTING_CASES = (
    ("decimal-comma", ("decimal-separator",), 12.34, {"decimal_separator": ","}, "12,34"),
    ("decimal-negative", ("decimal-separator",), -12.34, {"decimal_separator": "::"}, "-12::34"),
    ("decimal-zero", ("decimal-separator",), 0, {"decimal_separator": "<>"}, "0<>00"),
    ("decimal-empty", ("decimal-separator",), 12.34, {"decimal_separator": ""}, "1234"),
    ("decimal-default", ("decimal-separator",), 12.34, {"decimal_separator": "."}, "12.34"),
    ("thousands-large", ("thousands",), 1234567.89, {"thousands_separator": ","}, "1,234,567.89"),
    ("thousands-negative", ("thousands",), -1234567.89, {"thousands_separator": " "}, "-1 234 567.89"),
    ("thousands-short", ("thousands",), 999.99, {"thousands_separator": "_"}, "999.99"),
    ("thousands-boundary", ("thousands",), 1000, {"thousands_separator": "_"}, "1_000.00"),
    ("thousands-zero", ("thousands",), 0, {"thousands_separator": "."}, "0.00"),
    ("thousands-empty", ("thousands",), 1234567.89, {"thousands_separator": ""}, "1234567.89"),
    ("thousands-multi", ("thousands",), 1234567.89, {"thousands_separator": "<>"}, "1<>234<>567.89"),
    ("sign-positive", ("explicit-sign",), 12.34, {"show_plus": True}, "+12.34"),
    ("sign-negative", ("explicit-sign",), -12.34, {"show_plus": True}, "-12.34"),
    ("sign-zero", ("explicit-sign",), 0, {"show_plus": True}, "0.00"),
    ("sign-negative-zero", ("explicit-sign",), -0.0, {"show_plus": True}, "0.00"),
    ("sign-rounded-zero", ("explicit-sign",), 0.001, {"show_plus": True}, "0.00"),
    ("sign-rounded-negative-zero", ("explicit-sign",), -0.001, {"show_plus": True}, "0.00"),
    ("sign-disabled", ("explicit-sign",), 12.34, {"show_plus": False}, "12.34"),
    ("r1-decimal-thousands", ("decimal-separator", "thousands"), 1234567.89,
     {"decimal_separator": ",", "thousands_separator": "."}, "1.234.567,89"),
    ("r1-decimal-sign", ("decimal-separator", "explicit-sign"), 1234.5,
     {"decimal_separator": ",", "show_plus": True}, "+1234,50"),
    ("r1-decimal-sign-negative", ("decimal-separator", "explicit-sign"), -1234.5,
     {"decimal_separator": "::", "show_plus": True}, "-1234::50"),
    ("r1-thousands-sign", ("thousands", "explicit-sign"), 1234.5,
     {"thousands_separator": "_", "show_plus": True}, "+1_234.50"),
    ("r1-thousands-sign-negative", ("thousands", "explicit-sign"), -1234.5,
     {"thousands_separator": "_", "show_plus": True}, "-1_234.50"),
    ("r1-all", ("decimal-separator", "thousands", "explicit-sign"), 1234567.89,
     {"decimal_separator": ",", "thousands_separator": " ", "show_plus": True}, "+1 234 567,89"),
    ("r1-all-negative", ("decimal-separator", "thousands", "explicit-sign"), -1234.5,
     {"decimal_separator": ",", "thousands_separator": ".", "show_plus": True}, "-1.234,50"),
    ("r1-all-zero", ("decimal-separator", "thousands", "explicit-sign"), 0.001,
     {"decimal_separator": ",", "thousands_separator": ".", "show_plus": True}, "0,00"),
    ("decimal-affixes", ("decimal-separator", "prefix", "suffix"), 12.34,
     {"decimal_separator": ",", "prefix": "p.q ", "suffix": " s.t"}, "p.q 12,34 s.t"),
    ("r1-affixes", ("decimal-separator", "thousands", "explicit-sign", "prefix", "suffix"), -1234.5,
     {"decimal_separator": "::", "thousands_separator": ".", "show_plus": True,
      "prefix": "v1. ", "suffix": ".end"}, "v1. -1.234::50.end"),

    ("trim-integer", ("trim-zeros",), 100, {"trim_zeros": True}, "100"),
    ("trim-one", ("trim-zeros",), 12.3, {"trim_zeros": True}, "12.3"),
    ("trim-two", ("trim-zeros",), 12.34, {"trim_zeros": True}, "12.34"),
    ("trim-negative", ("trim-zeros",), -12.3, {"trim_zeros": True}, "-12.3"),
    ("trim-zero", ("trim-zeros",), 0, {"trim_zeros": True}, "0"),
    ("trim-disabled", ("trim-zeros",), 12.3, {"trim_zeros": False}, "12.30"),
    ("trim-significant-zero", ("trim-zeros",), 100.01, {"trim_zeros": True}, "100.01"),
    ("grouping-empty-separator", ("grouping-pattern",), 1234567.89, {"grouping": "3-2"}, "1234567.89"),
    ("grouping-three", ("grouping-pattern", "thousands"), 1234567.89,
     {"grouping": "3", "thousands_separator": ","}, "1,234,567.89"),
    ("grouping-indian", ("grouping-pattern", "thousands"), 1234567.89,
     {"grouping": "3-2", "thousands_separator": ","}, "12,34,567.89"),
    ("grouping-long", ("grouping-pattern", "thousands"), 123456789.01,
     {"grouping": "3-2", "thousands_separator": ","}, "12,34,56,789.01"),
    ("grouping-four", ("grouping-pattern", "thousands"), 1000,
     {"grouping": "3-2", "thousands_separator": ","}, "1,000.00"),
    ("grouping-five", ("grouping-pattern", "thousands"), 12345,
     {"grouping": "3-2", "thousands_separator": ","}, "12,345.00"),
    ("grouping-six", ("grouping-pattern", "thousands"), 123456,
     {"grouping": "3-2", "thousands_separator": ","}, "1,23,456.00"),
    ("grouping-short", ("grouping-pattern", "thousands"), 999,
     {"grouping": "3-2", "thousands_separator": ","}, "999.00"),
    ("grouping-negative", ("grouping-pattern", "thousands"), -1234567.89,
     {"grouping": "3-2", "thousands_separator": ","}, "-12,34,567.89"),
    ("parentheses-negative", ("negative-parentheses",), -12.34, {"negative_style": "parentheses"}, "(12.34)"),
    ("parentheses-positive", ("negative-parentheses",), 12.34, {"negative_style": "parentheses"}, "12.34"),
    ("parentheses-zero", ("negative-parentheses",), 0, {"negative_style": "parentheses"}, "0.00"),
    ("parentheses-rounded-zero", ("negative-parentheses",), -0.001, {"negative_style": "parentheses"}, "0.00"),
    ("parentheses-disabled", ("negative-parentheses",), -12.34, {"negative_style": "minus"}, "-12.34"),
    ("r2-trim-grouping", ("trim-zeros", "grouping-pattern", "thousands"), 1234567.8,
     {"trim_zeros": True, "grouping": "3-2", "thousands_separator": ","}, "12,34,567.8"),
    ("r2-trim-parentheses", ("trim-zeros", "negative-parentheses"), -1200,
     {"trim_zeros": True, "negative_style": "parentheses"}, "(1200)"),
    ("r2-grouping-parentheses", ("grouping-pattern", "negative-parentheses", "thousands"), -1234567.89,
     {"grouping": "3-2", "negative_style": "parentheses", "thousands_separator": ","}, "(12,34,567.89)"),
    ("r2-all", ("trim-zeros", "grouping-pattern", "negative-parentheses", "thousands"), -1234567.8,
     {"trim_zeros": True, "grouping": "3-2", "negative_style": "parentheses", "thousands_separator": ","}, "(12,34,567.8)"),
    ("trim-decimal-empty-fraction", ("trim-zeros", "decimal-separator"), 100,
     {"trim_zeros": True, "decimal_separator": "::"}, "100"),
    ("parentheses-plus", ("negative-parentheses", "explicit-sign"), 12.34,
     {"negative_style": "parentheses", "show_plus": True}, "+12.34"),
    ("parentheses-plus-zero", ("negative-parentheses", "explicit-sign"), -0.001,
     {"negative_style": "parentheses", "show_plus": True}, "0.00"),
    ("trim-affixes", ("trim-zeros", "decimal-separator", "prefix", "suffix"), 100,
     {"trim_zeros": True, "decimal_separator": ".", "prefix": "1. ", "suffix": " .0"}, "1. 100 .0"),
    ("r2-affixes-negative", ("trim-zeros", "grouping-pattern", "negative-parentheses", "decimal-separator",
                            "thousands", "explicit-sign", "prefix", "suffix"), -1234567.8,
     {"trim_zeros": True, "grouping": "3-2", "negative_style": "parentheses", "decimal_separator": ",",
      "thousands_separator": ".", "show_plus": True, "prefix": "[", "suffix": "]"}, "[(12.34.567,8)]"),
    ("r2-affixes-positive", ("trim-zeros", "grouping-pattern", "negative-parentheses", "decimal-separator",
                            "thousands", "explicit-sign", "prefix", "suffix"), 1234567.8,
     {"trim_zeros": True, "grouping": "3-2", "negative_style": "parentheses", "decimal_separator": ",",
      "thousands_separator": ".", "show_plus": True, "prefix": "[", "suffix": "]"}, "[+12.34.567,8]"),

    ("minimum-without-trimming", ("minimum-fraction",), 12, {"min_fraction_digits": 1}, "12.00"),
    ("minimum-zero", ("minimum-fraction", "trim-zeros"), 100,
     {"min_fraction_digits": 0, "trim_zeros": True}, "100"),
    ("minimum-one", ("minimum-fraction", "trim-zeros"), 100,
     {"min_fraction_digits": 1, "trim_zeros": True}, "100.0"),
    ("minimum-two", ("minimum-fraction", "trim-zeros"), 100,
     {"min_fraction_digits": 2, "trim_zeros": True}, "100.00"),
    ("minimum-significant-one", ("minimum-fraction", "trim-zeros"), 12.3,
     {"min_fraction_digits": 0, "trim_zeros": True}, "12.3"),
    ("minimum-one-retained", ("minimum-fraction", "trim-zeros"), 12.3,
     {"min_fraction_digits": 1, "trim_zeros": True}, "12.3"),
    ("minimum-one-padded", ("minimum-fraction", "trim-zeros"), 12.3,
     {"min_fraction_digits": 2, "trim_zeros": True}, "12.30"),
    ("minimum-significant-two", ("minimum-fraction", "trim-zeros"), 12.34,
     {"min_fraction_digits": 0, "trim_zeros": True}, "12.34"),
    ("minimum-money-zero", ("minimum-fraction", "trim-zeros"), 0,
     {"min_fraction_digits": 1, "trim_zeros": True}, "0.0"),
    ("minimum-negative", ("minimum-fraction", "trim-zeros"), -100,
     {"min_fraction_digits": 2, "trim_zeros": True}, "-100.00"),
    ("minimum-decimal-empty", ("minimum-fraction", "trim-zeros", "decimal-separator"), 12.3,
     {"min_fraction_digits": 2, "trim_zeros": True, "decimal_separator": ""}, "1230"),
    ("minimum-decimal-multi", ("minimum-fraction", "trim-zeros", "decimal-separator"), 100,
     {"min_fraction_digits": 1, "trim_zeros": True, "decimal_separator": "::"}, "100::0"),
    ("threshold-empty-separator", ("grouping-threshold",), 1234567.89, {"group_min_digits": 5}, "1234567.89"),
    ("threshold-at", ("grouping-threshold", "thousands"), 1234.5,
     {"group_min_digits": 4, "thousands_separator": ","}, "1,234.50"),
    ("threshold-below", ("grouping-threshold", "thousands"), 1234.5,
     {"group_min_digits": 5, "thousands_separator": ","}, "1234.50"),
    ("threshold-five", ("grouping-threshold", "thousands"), 12345,
     {"group_min_digits": 5, "thousands_separator": ","}, "12,345.00"),
    ("threshold-six", ("grouping-threshold", "thousands"), 12345,
     {"group_min_digits": 6, "thousands_separator": ","}, "12345.00"),
    ("threshold-zero", ("grouping-threshold", "thousands"), 0,
     {"group_min_digits": 1, "thousands_separator": ","}, "0.00"),
    ("threshold-excludes-minus", ("grouping-threshold", "thousands"), -1234.5,
     {"group_min_digits": 5, "thousands_separator": ","}, "-1234.50"),
    ("threshold-after-rounding", ("grouping-threshold", "thousands"), 999.999,
     {"group_min_digits": 4, "thousands_separator": ","}, "1,000.00"),
    ("threshold-indian-at", ("grouping-threshold", "grouping-pattern", "thousands"), 123456.7,
     {"group_min_digits": 6, "grouping": "3-2", "thousands_separator": ","}, "1,23,456.70"),
    ("threshold-indian-below", ("grouping-threshold", "grouping-pattern", "thousands"), 123456.7,
     {"group_min_digits": 7, "grouping": "3-2", "thousands_separator": ","}, "123456.70"),
    ("threshold-multi", ("grouping-threshold", "grouping-pattern", "thousands"), 1234567.89,
     {"group_min_digits": 1, "grouping": "3-2", "thousands_separator": ".."}, "12..34..567.89"),
    ("zero-text-zero", ("zero-text",), 0, {"zero_text": "free"}, "free"),
    ("zero-text-negative-zero", ("zero-text",), -0.0, {"zero_text": "free"}, "free"),
    ("zero-text-rounded", ("zero-text",), 0.001, {"zero_text": "free"}, "free"),
    ("zero-text-rounded-negative", ("zero-text",), -0.001, {"zero_text": "free"}, "free"),
    ("zero-text-empty", ("zero-text",), 0, {"zero_text": ""}, ""),
    ("zero-text-disabled", ("zero-text",), 0, {"zero_text": None}, "0.00"),
    ("zero-text-positive", ("zero-text",), 12.34, {"zero_text": "free"}, "12.34"),
    ("zero-text-negative", ("zero-text",), -12.34, {"zero_text": "free"}, "-12.34"),
    ("zero-text-literal", ("zero-text",), 0, {"zero_text": "0.00+"}, "0.00+"),
    ("r3-minimum-threshold", ("minimum-fraction", "grouping-threshold", "trim-zeros", "thousands"), 1234,
     {"min_fraction_digits": 1, "group_min_digits": 5, "trim_zeros": True, "thousands_separator": ","}, "1234.0"),
    ("r3-minimum-threshold-at", ("minimum-fraction", "grouping-threshold", "trim-zeros", "thousands"), 1234,
     {"min_fraction_digits": 1, "group_min_digits": 4, "trim_zeros": True, "thousands_separator": ","}, "1,234.0"),
    ("r3-minimum-zero", ("minimum-fraction", "zero-text", "trim-zeros"), 0,
     {"min_fraction_digits": 2, "zero_text": "none", "trim_zeros": True}, "none"),
    ("r3-threshold-zero", ("grouping-threshold", "zero-text", "thousands"), -0.001,
     {"group_min_digits": 1, "zero_text": "No. 0", "thousands_separator": "."}, "No. 0"),
    ("r3-all-negative", ("minimum-fraction", "grouping-threshold", "zero-text", "trim-zeros", "grouping-pattern",
                         "negative-parentheses", "decimal-separator", "thousands", "explicit-sign", "prefix", "suffix"), -1234567.8,
     {"min_fraction_digits": 2, "group_min_digits": 7, "zero_text": "ZERO", "trim_zeros": True, "grouping": "3-2",
      "negative_style": "parentheses", "decimal_separator": ",", "thousands_separator": ".", "show_plus": True,
      "prefix": "Итого ", "suffix": " руб."}, "Итого (12.34.567,80) руб."),
    ("r3-all-positive", ("minimum-fraction", "grouping-threshold", "zero-text", "trim-zeros", "grouping-pattern",
                         "negative-parentheses", "decimal-separator", "thousands", "explicit-sign", "prefix", "suffix"), 1234567.8,
     {"min_fraction_digits": 2, "group_min_digits": 7, "zero_text": "ZERO", "trim_zeros": True, "grouping": "3-2",
      "negative_style": "parentheses", "decimal_separator": ",", "thousands_separator": ".", "show_plus": True,
      "prefix": "Итого ", "suffix": " руб."}, "Итого +12.34.567,80 руб."),
    ("r3-all-below-threshold", ("minimum-fraction", "grouping-threshold", "zero-text", "trim-zeros", "grouping-pattern",
                                "negative-parentheses", "decimal-separator", "thousands", "explicit-sign", "prefix", "suffix"), 1234567.8,
     {"min_fraction_digits": 2, "group_min_digits": 8, "zero_text": "ZERO", "trim_zeros": True, "grouping": "3-2",
      "negative_style": "parentheses", "decimal_separator": ",", "thousands_separator": ".", "show_plus": True,
      "prefix": "Итого ", "suffix": " руб."}, "Итого +1234567,80 руб."),
    ("r3-all-zero", ("minimum-fraction", "grouping-threshold", "zero-text", "trim-zeros", "grouping-pattern",
                     "negative-parentheses", "decimal-separator", "thousands", "explicit-sign", "prefix", "suffix"), -0.001,
     {"min_fraction_digits": 2, "group_min_digits": 7, "zero_text": "ZERO", "trim_zeros": True, "grouping": "3-2",
      "negative_style": "parentheses", "decimal_separator": ",", "thousands_separator": ".", "show_plus": True,
      "prefix": "Итого ", "suffix": " руб."}, "Итого ZERO руб."),
    ("r3-all-zero-disabled", ("minimum-fraction", "grouping-threshold", "zero-text", "trim-zeros", "grouping-pattern",
                              "negative-parentheses", "decimal-separator", "thousands", "explicit-sign", "prefix", "suffix"), -0.001,
     {"min_fraction_digits": 2, "group_min_digits": 7, "zero_text": None, "trim_zeros": True, "grouping": "3-2",
      "negative_style": "parentheses", "decimal_separator": ",", "thousands_separator": ".", "show_plus": True,
      "prefix": "Итого ", "suffix": " руб."}, "Итого 0,00 руб."),
    ("zero-text-affixes-literal", ("zero-text", "trim-zeros", "negative-parentheses", "decimal-separator",
                                  "thousands", "explicit-sign", "prefix", "suffix"), -0.001,
     {"zero_text": "0.00+", "trim_zeros": True, "negative_style": "parentheses", "decimal_separator": ",",
      "thousands_separator": "_", "show_plus": True, "prefix": ".+", "suffix": ".-"}, ".+0.00+.-"),
)


def contract_cases(requirements=()):
    """Literal expected results, independent of implementation and AI output."""
    requirements = set(requirements)
    if requirements - set(CONTRACTS):
        raise ValueError("Unknown requirement")
    cases = [(f"{module}:{amount}", module, function, [amount], {}, f"{amount:.2f}")
             for module, function in (("receipt", "format_total"), ("email_receipt", "format_email_total"))
             for amount in (0, 0.01, 12.34, 100)]
    if requirements & {"discount", "fee"}:
        cases.append(("price:default", "money", "price_units", [100], {}, 10000))
    for index, (amount, discount, fee, discounted, fee_only, combined) in enumerate((
            (100, 10, 500, 9000, 10500, 9500),
            (12.34, 25, 7, 926, 1241, 933),
            (0, 10, 30, 0, 30, 30),
            (19.99, 15, 123, 1699, 2122, 1822))):
        if "discount" in requirements:
            cases.append((f"discount:{index}", "money", "price_units", [amount], {"discount_percent": discount}, discounted))
        if "fee" in requirements:
            cases.append((f"fee:{index}", "money", "price_units", [amount], {"fee_kopeks": fee}, fee_only))
        if {"discount", "fee"} <= requirements:
            cases.append((f"combined:{index}", "money", "price_units", [amount],
                          {"discount_percent": discount, "fee_kopeks": fee}, combined))
    if "currency" in requirements:
        for amount in (0, 12.34, 100):
            cases.append((f"currency:{amount}", "receipt", "format_currency_total", [amount], {}, f"{amount:.2f} RUB"))
    if "legacy-rubles" in requirements:
        cases.append(("legacy:rubles", "money", "price_units", [100], {}, 100))
    for amount, rendered in ((0, "0.00"), (0.01, "0.01"), (12.34, "12.34"), (100, "100.00")):
        if "prefix" in requirements:
            cases.append((f"prefix:{amount}", "receipt", "format_total", [amount], {"prefix": "Итого "}, "Итого " + rendered))
        if "suffix" in requirements:
            cases.append((f"suffix:{amount}", "receipt", "format_total", [amount], {"suffix": " руб."}, rendered + " руб."))
        if {"prefix", "suffix"} <= requirements:
            cases.append((f"prefix-suffix:{amount}", "receipt", "format_total", [amount],
                          {"prefix": "Итого ", "suffix": " руб."}, "Итого " + rendered + " руб."))
    if "round-up" in requirements:
        for amount, expected in ((12.34, 13), (12, 12), (0.01, 1), (-12.34, -12), (0, 0)):
            cases.append((f"round-up:{amount}", "receipt", "round_up_total", [amount], {}, expected))
    for name, requires, amount, options, expected in FORMATTING_CASES:
        if set(requires) <= requirements:
            cases.append(("format:" + name, "receipt", "format_total", [amount], dict(options), expected))
    return cases


def main():
    sys.dont_write_bytecode = True
    candidate = Path(sys.argv[1]).resolve()
    requirements = json.loads(sys.argv[2]) if len(sys.argv) > 2 else []
    cases = contract_cases(requirements)
    sys.path.insert(0, str(candidate))
    results = []
    for name, module, function, args, kwargs, expected in cases:
        try:
            actual = getattr(importlib.import_module(module), function)(*args, **kwargs)
            results.append({"check": name, "expected": expected, "actual": actual,
                            "passed": type(actual) is type(expected) and actual == expected})
        except Exception as error:
            results.append({"check": name, "expected": expected, "passed": False,
                            "error": f"{type(error).__name__}: {error}"})
    try:
        flags = [getattr(importlib.import_module(f"feature_{name}"), "ENABLED")
                 for name in "abc"]
        results.append({"check": "three_way_interaction", "flags": flags,
                        "passed": all(type(flag) is bool for flag in flags) and not all(flags)})
    except Exception as error:
        results.append({"check": "three_way_interaction", "passed": False,
                        "error": f"{type(error).__name__}: {error}"})
    passed = all(result["passed"] for result in results)
    print(json.dumps({"passed": passed, "checks": results}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
