import unittest
from datetime import datetime, timedelta

from process_voice_messages import normalize_due_date_value, DEFAULT_TIMEZONE


class DueDateParsingTests(unittest.TestCase):
    def test_iso_format_kept(self):
        self.assertEqual(
            normalize_due_date_value("2030-01-05", DEFAULT_TIMEZONE),
            "2030-01-05",
        )

    def test_relative_russian_phrase(self):
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        parsed = normalize_due_date_value("завтра", DEFAULT_TIMEZONE)
        self.assertEqual(parsed, tomorrow)

    def test_past_date_discarded(self):
        self.assertIsNone(
            normalize_due_date_value("1999-01-01", DEFAULT_TIMEZONE)
        )


if __name__ == "__main__":
    unittest.main()
