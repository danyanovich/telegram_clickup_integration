import unittest

from process_voice_messages import (
    DEFAULT_TRANSCRIPTION_MAX_CHARS,
    normalize_config,
)


class NormalizeConfigTests(unittest.TestCase):
    def test_normalize_clamps_values(self):
        cfg = normalize_config(
            {
                "telegram_check_hours": 0,
                "default_priority": 9,
                "log_retention_days": -5,
                "tasks_retention_days": "7",
                "store_transcriptions": "no",
                "transcription_max_chars": "invalid",
                "assignee_map": None,
                "assignee_aliases": None,
                "summary_chat_id": "   \t",
                "send_summary_to_telegram": "false",
                "download_max_workers": 0,
            }
        )

        self.assertEqual(cfg["telegram_check_hours"], 1)
        self.assertEqual(cfg["default_priority"], 3)
        self.assertEqual(cfg["log_retention_days"], 0)
        self.assertEqual(cfg["tasks_retention_days"], 7)
        self.assertFalse(cfg["store_transcriptions"])
        self.assertEqual(
            cfg["transcription_max_chars"],
            DEFAULT_TRANSCRIPTION_MAX_CHARS,
        )
        self.assertEqual(cfg["assignee_map"], {})
        self.assertEqual(cfg["assignee_aliases"], {})
        self.assertFalse(cfg["send_summary_to_telegram"])
        self.assertEqual(cfg["summary_chat_id"], "")
        self.assertEqual(cfg["download_max_workers"], 1)


if __name__ == "__main__":
    unittest.main()
