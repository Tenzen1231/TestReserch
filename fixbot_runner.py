"""Trusted unittest runner executed inside the network-less sandbox.

Usage: python -I -B fixbot_runner.py <project dir> list|run
Every test module is imported and run in its own process, so module-level code
or a global patch in one module cannot change another module's outcome; a
module that tampers with unittest itself fails as a whole. Prints one JSON line.
"""
import fnmatch
import json
import os
import subprocess
import sys
import traceback
import unittest


def modules(project):
    """Importable test modules under tests/, as unittest's default discovery finds them."""
    found = []
    for root, directories, files in os.walk(os.path.join(project, "tests")):
        directories[:] = sorted(d for d in directories if os.path.isfile(os.path.join(root, d, "__init__.py")))
        package = os.path.relpath(root, project).replace(os.sep, ".")
        found.extend(package + "." + name[:-3] for name in sorted(files) if fnmatch.fnmatch(name, "test*.py"))
    return sorted(found)


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def fingerprint():
    import builtins
    return ({name: id(value) for name, value in vars(unittest.TestCase).items()},
            {name: id(value) for name, value in vars(unittest.TestResult).items()},
            {name: id(value) for name, value in vars(builtins).items()})


class Collector(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.outcomes = {}

    def addSuccess(self, test):
        self.outcomes[test.id()] = {"passed": True}

    def record(self, test, err, kind):
        self.outcomes[test.id()] = {"passed": False, "kind": kind,
                                    "detail": "".join(traceback.format_exception(*err))[-3000:]}

    def addFailure(self, test, err):
        self.record(test, err, "failure")

    def addError(self, test, err):
        self.record(test, err, "error")

    def addSkip(self, test, reason):
        self.outcomes[test.id()] = {"passed": False, "kind": "skipped", "detail": str(reason)[:500]}

    def addExpectedFailure(self, test, err):
        self.record(test, err, "expected_failure")

    def addUnexpectedSuccess(self, test):
        self.outcomes[test.id()] = {"passed": False, "kind": "unexpected_success", "detail": ""}


def one_module(project, name, mode):
    sys.__stdout__.reconfigure(encoding="utf-8")
    write, dumps = sys.__stdout__.write, json.dumps  # Bound before any project code runs.
    os.chdir(project)
    sys.path.insert(0, project)
    before = fingerprint()
    try:
        tests = list(flatten(unittest.TestLoader().loadTestsFromName(name)))
    except Exception:
        write(dumps({"ids": [name + ".import"], "outcomes": {name + ".import": {
            "passed": False, "kind": "import", "detail": traceback.format_exc()[-3000:]}}}) + "\n")
        return
    report = {"ids": [test.id() for test in tests]}
    if mode == "run":
        result = Collector()
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        for test in tests:
            test(result)
        report["outcomes"] = result.outcomes
    if fingerprint() != before:
        report["outcomes"] = {test_id: {"passed": False, "kind": "tampering",
                                        "detail": "the module changed unittest or builtins"} for test_id in report["ids"]}
    write(dumps(report, ensure_ascii=False) + "\n")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    project, mode = sys.argv[1], sys.argv[2]
    if len(sys.argv) > 3:
        one_module(project, sys.argv[3], mode)
        return
    report = {"ids": [], "loader_errors": [], "outcomes": {}}
    for name in modules(project):
        process = subprocess.run([sys.executable, "-I", "-B", os.path.abspath(__file__), project, mode, name],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        try:
            part = json.loads(process.stdout.strip().splitlines()[-1])
            if process.returncode:
                raise ValueError()
        except (ValueError, IndexError):
            part = {"ids": [name + ".process"], "outcomes": {name + ".process": {
                "passed": False, "kind": "crash", "detail": (process.stdout + process.stderr)[-3000:]}}}
        report["ids"].extend(part["ids"])
        report["outcomes"].update(part.get("outcomes", {}))
    report["ids"].sort()
    if mode == "run":
        report["passed"] = bool(report["ids"]) and all(
            report["outcomes"].get(test_id, {}).get("passed") for test_id in report["ids"])
    else:
        report.pop("outcomes")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
