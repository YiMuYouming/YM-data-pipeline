import importlib
import unittest


class LiveTestInventoryTests(unittest.TestCase):
    def test_legacy_live_named_files_are_explicit_discovered_skips(self):
        loader = unittest.defaultTestLoader
        for name in ("tests.test_iwencai", "tests.test_sources", "tests.test_pytdx"):
            with self.subTest(module=name):
                module = importlib.import_module(name)
                suite = loader.loadTestsFromModule(module)
                self.assertGreater(suite.countTestCases(), 0)
                cases = [case for group in suite for case in group]
                live_cases = [
                    case for case in cases if "LiveIntegration" in case.id()
                ]
                self.assertGreater(len(live_cases), 0)
                self.assertTrue(
                    all(getattr(case, "__unittest_skip__", False) for case in live_cases)
                )


if __name__ == "__main__":
    unittest.main()
