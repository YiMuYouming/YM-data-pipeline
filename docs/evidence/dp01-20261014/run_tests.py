"""Offline unittest evidence, including every test ID and terminal outcome."""
import json
import pathlib
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

class Result(unittest.TextTestResult):
    def __init__(self, *args):
        super().__init__(*args)
        self.outcomes = {}
    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes[test.id()] = "passed"
    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.outcomes[test.id()] = "failed"
    def addError(self, test, err):
        super().addError(test, err)
        self.outcomes[test.id()] = "error"
    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.outcomes[test.id()] = "skipped"
    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err:
            self.outcomes[test.id()] = "failed"

def main():
    label = sys.argv[1]
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    if label == "baseline":
        def flatten(items):
            for item in items:
                if isinstance(item, unittest.TestSuite):
                    yield from flatten(item)
                else:
                    yield item
        suite = unittest.TestSuite(test for test in flatten(suite) if not test.id().startswith("test_dp01."))
    started = time.time()
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    (pathlib.Path(__file__).parent / f"{label}-ids.json").write_text(json.dumps({
        "started_unix": started, "elapsed_seconds": time.time() - started,
        "tests_run": result.testsRun, "outcomes": result.outcomes,
    }, indent=2) + "\n")
    sys.exit(not result.wasSuccessful())

if __name__ == "__main__":
    main()
