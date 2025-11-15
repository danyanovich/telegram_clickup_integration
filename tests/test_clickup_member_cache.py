import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import process_voice_messages as pvm  # noqa: E402


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None

    def close(self):  # pragma: no cover - requests.Response defines close()
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ClickUpMemberCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp_dir.name) / "cache.json"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _write_cache(self, payload):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_uses_cache_when_fresh(self):
        now = datetime.now().isoformat()
        cached_members = {"ivan": [1], "maria": [2]}
        self._write_cache(
            {
                "lists": {
                    "abc": {
                        "fetched_at": now,
                        "members": cached_members,
                    }
                }
            }
        )

        with mock.patch.object(pvm, "MEMBER_CACHE_FILE", self.cache_path):
            with mock.patch.object(pvm, "_request_with_retries") as request_mock:
                result = pvm.fetch_clickup_member_map(
                    "token",
                    "abc",
                    cache_ttl_minutes=60,
                )

        self.assertEqual(result, cached_members)
        request_mock.assert_not_called()

    def test_refreshes_cache_when_expired(self):
        old = (datetime.now() - timedelta(minutes=180)).isoformat()
        self._write_cache(
            {
                "lists": {
                    "abc": {
                        "fetched_at": old,
                        "members": {"old": [99]},
                    }
                }
            }
        )

        api_payload = {
            "members": [
                {
                    "user": {
                        "id": "42",
                        "username": "Ivan",
                        "profile": {"full_name": "Ivan Petrov"},
                    }
                }
            ]
        }

        with mock.patch.object(pvm, "MEMBER_CACHE_FILE", self.cache_path):
            with mock.patch.object(
                pvm, "_request_with_retries", return_value=DummyResponse(api_payload)
            ):
                result = pvm.fetch_clickup_member_map(
                    "token",
                    "abc",
                    cache_ttl_minutes=60,
                )

        self.assertIn("ivan", result)
        self.assertEqual(result["ivan"], [42])
        self.assertIn("ivan petrov", result)
        saved = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["lists"]["abc"]["members"]["ivan"], [42])


if __name__ == "__main__":
    unittest.main()
