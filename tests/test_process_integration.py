import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import process_voice_messages as pvm


class ProcessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        root = Path(self.tmp_dir.name)
        patchers = [
            mock.patch.object(pvm, "PROJECT_ROOT", root),
            mock.patch.object(pvm, "STATE_FILE", root / "state.json"),
            mock.patch.object(pvm, "LOCK_FILE", root / ".lock"),
            mock.patch.object(pvm, "CACHE_DIR", root / ".cache"),
            mock.patch.object(pvm, "MEMBER_CACHE_FILE", root / ".cache" / "members.json"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        (root / ".cache").mkdir(parents=True, exist_ok=True)
        self.root = root

        self.config = {
            "clickup_list_id": "list-1",
            "telegram_check_hours": 1,
            "default_priority": 2,
            "log_retention_days": 0,
            "tasks_retention_days": 0,
            "store_transcriptions": True,
            "transcription_max_chars": 200,
            "timezone": "Europe/Moscow",
            "clickup_member_cache_hours": 1,
            "download_max_workers": 1,
            "openai_max_workers": 1,
            "openai_max_attempts": 2,
            "create_clickup_reminders": True,
            "reminder_offset_hours": 1,
            "send_summary_to_telegram": True,
            "summary_chat_id": "summary-chat",
            "assignee_map": {"ivan": [101]},
            "assignee_aliases": {"ваня": "Иван"},
            "clickup_team_id": "team-1",
        }
        self.secrets = {
            "bot_token": "telegram-token",
            "chat_id": "telegram-chat",
            "openai_api_key": "openai-token",
            "clickup_token": "clickup-token",
        }
        self.voice_messages = [
            {
                "update_id": 123,
                "file_id": "file_1",
                "duration": 5,
                "date": "2025-01-01T10:00:00",
                "from_user": "Ivan",
                "type": "voice",
                "mime_type": "audio/ogg",
                "is_forwarded": False,
            }
        ]
        self.created_payloads = []
        self.reminders = []
        self.summary_texts = []

        self.patches = [
            mock.patch.object(pvm, "load_config", return_value=self.config),
            mock.patch.object(pvm, "load_api_secrets", return_value=self.secrets),
            mock.patch.object(
                pvm,
                "get_recent_voice_messages",
                return_value=(self.voice_messages, 200),
            ),
            mock.patch.object(
                pvm,
                "fetch_clickup_member_map",
                return_value={"иван": [101]},
            ),
            mock.patch.object(
                pvm,
                "download_audio_file",
                side_effect=self._fake_download,
            ),
            mock.patch.object(
                pvm,
                "transcribe_audio",
                return_value="Нужно сделать задачу для Вани",
            ),
            mock.patch.object(
                pvm,
                "extract_tasks_from_text",
                return_value=[
                    {
                        "name": "Task from audio",
                        "description": "Do something important",
                        "due_date": "2030-12-31",
                        "priority": 1,
                        "assignee": "Ваня",
                    }
                ],
            ),
            mock.patch.object(
                pvm,
                "create_clickup_task",
                side_effect=self._capture_clickup_payload,
            ),
            mock.patch.object(
                pvm,
                "create_clickup_reminder",
                side_effect=self._capture_reminder,
            ),
            mock.patch.object(
                pvm,
                "send_summary_notification",
                side_effect=lambda token, chat, text: self.summary_texts.append(
                    (token, chat, text)
                ),
            ),
        ]
        for patcher in self.patches:
            patcher.start()
        self.addCleanup(self._stop_patches)

    def _stop_patches(self):
        for patcher in reversed(self.patches):
            try:
                patcher.stop()
            except RuntimeError:
                pass

    def _fake_download(self, bot_token, file_id, output_path):
        Path(output_path).write_text("test", encoding="utf-8")
        return output_path

    def _capture_clickup_payload(self, token, list_id, payload):
        task_id = f"task-{len(self.created_payloads) + 1}"
        self.created_payloads.append(payload)
        return {"id": task_id}

    def _capture_reminder(self, token, team_id, task_id, remind_time, assignee_id):
        self.reminders.append((task_id, remind_time, assignee_id))

    def test_full_run_creates_task_and_summary(self):
        result_path = pvm.run_once()
        self.assertTrue(Path(result_path).exists())
        self.assertEqual(len(self.created_payloads), 1)
        payload = self.created_payloads[0]
        self.assertEqual(payload["name"], "Task from audio")
        self.assertEqual(payload["assignees"], [101])
        self.assertIn("due_date", payload)

        self.assertEqual(len(self.reminders), 1)
        self.assertEqual(self.reminders[0][0], "task-1")
        self.assertEqual(self.summary_texts[0][0], self.secrets["bot_token"])
        self.assertIn("Создано задач: 1", self.summary_texts[0][2])

        with open(result_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["total_tasks_created"], 1)
        self.assertEqual(len(data["voice_messages"]), 1)
        self.assertIn("transcription", data["voice_messages"][0])

        state_contents = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state_contents["last_update_id"], 123)


if __name__ == "__main__":
    unittest.main()
