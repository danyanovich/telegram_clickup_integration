import unittest

from process_voice_messages import _execute_with_retry


class RetryHelperTests(unittest.TestCase):
    def test_operation_eventually_succeeds(self):
        calls = {"count": 0}

        def flaky():
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("boom")
            return "ok"

        result = _execute_with_retry(flaky, "test", max_attempts=5, base_delay=0)
        self.assertEqual(result, "ok")
        self.assertEqual(calls["count"], 3)

    def test_operation_raises_after_max_attempts(self):
        calls = {"count": 0}

        def always_fail():
            calls["count"] += 1
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            _execute_with_retry(always_fail, "test", max_attempts=2, base_delay=0)
        self.assertEqual(calls["count"], 2)


if __name__ == "__main__":
    unittest.main()
