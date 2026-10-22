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
}
REGISTRY_PATH = ".queue/requirements.json"


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
