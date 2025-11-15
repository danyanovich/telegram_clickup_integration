import unittest

import process_voice_messages as pvm


class NormalizeConfigTests(unittest.TestCase):
    def test_defaults_are_applied(self):
        cfg = pvm.normalize_config({})
        self.assertEqual(cfg["openai_max_workers"], pvm.DEFAULT_MAX_WORKERS)
        self.assertEqual(cfg["download_max_workers"], pvm.DEFAULT_MAX_WORKERS)
        self.assertEqual(cfg["openai_max_attempts"], pvm.DEFAULT_OPENAI_MAX_ATTEMPTS)
        self.assertEqual(cfg["clickup_member_cache_hours"], 1)

    def test_invalid_workers_fallback(self):
        cfg = pvm.normalize_config({"openai_max_workers": "abc"})
        self.assertEqual(cfg["openai_max_workers"], pvm.DEFAULT_MAX_WORKERS)
        cfg = pvm.normalize_config({"download_max_workers": 0})
        self.assertEqual(cfg["download_max_workers"], 1)

    def test_invalid_attempts_fallback(self):
        cfg = pvm.normalize_config({"openai_max_attempts": 0})
        self.assertEqual(cfg["openai_max_attempts"], pvm.DEFAULT_OPENAI_MAX_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
