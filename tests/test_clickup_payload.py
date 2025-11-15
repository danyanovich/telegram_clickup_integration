import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from clickup_client import build_clickup_payload  # noqa: E402


class ClickUpPayloadTests(unittest.TestCase):
    def test_due_date_accepts_iso_datetime_with_offset(self):
        task = {
            "name": "Задача",
            "description": "Тест",
            "due_date": "2025-01-05T12:30:00+03:00",
        }

        payload = build_clickup_payload(task)

        expected_ts = int(
            datetime(2025, 1, 5, 9, 30, tzinfo=timezone.utc).timestamp() * 1000
        )
        self.assertEqual(payload["due_date"], expected_ts)

    def test_due_date_accepts_zulu_time(self):
        task = {
            "name": "Задача",
            "description": "Тест",
            "due_date": "2025-02-10T00:00:00Z",
        }

        payload = build_clickup_payload(task)

        expected_ts = int(
            datetime(2025, 2, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        self.assertEqual(payload["due_date"], expected_ts)

    def test_invalid_due_date_is_ignored(self):
        task = {
            "name": "Задача",
            "description": "Тест",
            "due_date": "not-a-date",
        }

        payload = build_clickup_payload(task)

        self.assertNotIn("due_date", payload)


if __name__ == "__main__":
    unittest.main()
