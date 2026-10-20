import importlib.util
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("keyword_reply", SCRIPTS / "keyword_reply.py")
keyword_reply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keyword_reply)


class RemainingTest(unittest.TestCase):
    def test_null_from_quota_check_means_unlimited(self):
        self.assertEqual(keyword_reply._m_remaining("null"), "unlimited")

    def test_keeps_words_and_counts(self):
        for given, expected in ((None, None), ("unlimited", "unlimited"), ("unknown", "unknown"),
                                ("0", 0), ("12", 12)):
            self.assertEqual(keyword_reply._m_remaining(given), expected)

    def test_rejects_other_values(self):
        for given in ("-1", "None", "", "abc"):
            with self.assertRaises(keyword_reply.Fail):
                keyword_reply._m_remaining(given)


if __name__ == "__main__":
    unittest.main()
