"""Fixed acceptance checks for this synthetic collaboration experiment."""

import importlib
import json
from pathlib import Path
import sys


def main():
    sys.dont_write_bytecode = True
    candidate = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(candidate))
    results = []
    try:
        for module_name, function_name in (
            ("receipt", "format_total"),
            ("email_receipt", "format_email_total"),
        ):
            function = getattr(importlib.import_module(module_name), function_name)
            for amount in (0, 0.01, 12.34, 100):
                expected = f"{amount:.2f}"
                actual = function(amount)
                results.append({"check": f"{module_name}:{amount}",
                                "expected": expected, "actual": actual,
                                "passed": actual == expected})
        flags = [getattr(importlib.import_module(f"feature_{name}"), "ENABLED")
                 for name in "abc"]
        results.append({"check": "three_way_interaction", "flags": flags,
                        "passed": not all(flags)})
    except Exception as error:
        results.append({"check": "required_public_api", "passed": False,
                        "error": f"{type(error).__name__}: {error}"})
    passed = all(result["passed"] for result in results)
    print(json.dumps({"passed": passed, "checks": results}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
