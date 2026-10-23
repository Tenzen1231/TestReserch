"""Trusted unittest runner executed inside the network-less sandbox.

Usage: python -I -B fixbot_runner.py <project dir> list|run
Prints one JSON object: discovered test ids, and for ``run`` the outcome of each.
"""
import io
import json
import os
import sys
import traceback
import unittest


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


class Collector(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.outcomes = {}

    def addSuccess(self, test):
        self.outcomes[test.id()] = {"passed": True}

    def record(self, test, error, kind):
        text = "".join(traceback.format_exception(*error))
        self.outcomes[test.id()] = {"passed": False, "kind": kind, "detail": text[-3000:]}

    def addFailure(self, test, error):
        self.record(test, error, "failure")

    def addError(self, test, error):
        self.record(test, error, "error")

    def addSkip(self, test, reason):
        self.outcomes[test.id()] = {"passed": False, "kind": "skipped", "detail": str(reason)[:500]}

    def addExpectedFailure(self, test, error):
        self.record(test, error, "expected_failure")

    def addUnexpectedSuccess(self, test):
        self.outcomes[test.id()] = {"passed": False, "kind": "unexpected_success", "detail": ""}


def main():
    project, mode = sys.argv[1], sys.argv[2]
    os.chdir(project)
    sys.path.insert(0, project)
    loader = unittest.TestLoader()
    suite = loader.discover("tests", top_level_dir=project)
    tests = list(flatten(suite))
    # Import failures appear as synthetic tests; they must fail, never vanish.
    report = {"ids": sorted(test.id() for test in tests), "loader_errors": [e[-3000:] for e in loader.errors]}
    if mode == "run":
        result = Collector()
        stream = io.StringIO()
        sys.stdout, real = stream, sys.stdout
        try:
            for test in tests:
                test(result)
        finally:
            sys.stdout = real
        report["outcomes"] = result.outcomes
        report["passed"] = (not report["loader_errors"] and len(result.outcomes) == len(tests)
                            and all(item["passed"] for item in result.outcomes.values()))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
