import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase, mock
from zoneinfo import ZoneInfo

import process_voice_messages as pvm


class PipelineIntegrationTests(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.original_paths = {
            "PROJECT_ROOT": pvm.PROJECT_ROOT,
            "STATE_FILE": pvm.STATE_FILE,
            "LOCK_FILE": pvm.LOCK_FILE,
            "CACHE_DIR": pvm.CACHE_DIR,
            "MEMBER_CACHE_FILE": pvm.MEMBER_CACHE_FILE,
        }
        new_root = Path(self.tmpdir.name)
        pvm.PROJECT_ROOT = new_root
        pvm.STATE_FILE = new_root / "state.json"
        pvm.LOCK_FILE = new_root / ".processor.lock"
        pvm.CACHE_DIR = new_root / ".cache"
        pvm.MEMBER_CACHE_FILE = pvm.CACHE_DIR / "clickup_members.json"

    def tearDown(self):
        for name, value in self.original_paths.items():
            setattr(pvm, name, value)

    @mock.patch("process_voice_messages.create_clickup_reminder")
    @mock.patch("process_voice_messages.create_clickup_task")
    @mock.patch("process_voice_messages._transcribe_and_extract")
    @mock.patch("process_voice_messages._prepare_audio_job")
    @mock.patch("process_voice_messages.fetch_clickup_member_map")
    @mock.patch("process_voice_messages.get_recent_voice_messages")
    @mock.patch("process_voice_messages.load_api_secrets")
    @mock.patch("process_voice_messages.load_config")
    def test_run_once_full_pipeline(
        self,
        mock_load_config,
        mock_load_secrets,
        mock_get_messages,
        mock_fetch_members,
        mock_prepare_job,
        mock_transcribe_extract,
        mock_create_task,
        mock_create_reminder,
    ):
        config = pvm.normalize_config(
            {
                "clickup_list_id": "123",
                "telegram_check_hours": 1,
                "default_priority": 2,
                "log_retention_days": 0,
                "tasks_retention_days": 0,
                "store_transcriptions": False,
                "transcription_max_chars": 100,
                "openai_max_workers": 1,
                "openai_max_attempts": 2,
                "download_max_workers": 1,
                "create_clickup_reminders": False,
            }
        )
        mock_load_config.return_value = config
        mock_load_secrets.return_value = {
            "bot_token": "telegram-bot",
            "chat_id": "-100",
            "openai_api_key": "openai-key",
            "clickup_token": "clickup-key",
        }
        voice_message = {
            "update_id": 42,
            "file_id": "file-1",
            "duration": 5,
            "date": "2025-10-01T12:00:00",
            "from_user": "Иван",
            "type": "voice",
            "mime_type": "audio/ogg",
            "is_forwarded": False,
        }
        mock_get_messages.return_value = ([voice_message], 42)
        mock_fetch_members.return_value = {"иван": [777]}
        vm_log = {
            "from_user": voice_message["from_user"],
            "date": voice_message["date"],
            "duration": voice_message["duration"],
            "type": voice_message["type"],
            "is_forwarded": False,
            "update_id": voice_message["update_id"],
        }

        def fake_prepare(idx, vm, log_entry, bot_token):
            dummy_audio = Path(self.tmpdir.name) / f"message_{idx}.ogg"
            dummy_audio.write_bytes(b"data")
            return pvm.PreparedVoiceMessage(idx, vm, log_entry, str(dummy_audio))

        mock_prepare_job.side_effect = fake_prepare

        tasks = [
            {
                "name": "Сделать отчёт",
                "description": "Подготовить отчёт",
                "due_date": "2030-01-05",
                "priority": 1,
                "assignee": "Иван",
            },
            {
                "name": "Обновить документацию",
                "description": "Описание",
                "due_date": None,
                "priority": None,
                "assignee": None,
            },
        ]
        mock_transcribe_extract.return_value = ("text", tasks)

        created_payloads = []

        def fake_create(token, list_id, payload):
            created_payloads.append(payload)
            return {"id": f"task-{len(created_payloads)}"}

        mock_create_task.side_effect = fake_create

        tasks_file = pvm.run_once(dry_run=False)

        self.assertEqual(mock_create_task.call_count, 2)
        self.assertTrue(Path(tasks_file).exists())
        expected_due = int(
            datetime(2030, 1, 5, tzinfo=ZoneInfo("UTC")).timestamp() * 1000
        )
        self.assertEqual(created_payloads[0]["due_date"], expected_due)
        self.assertEqual(created_payloads[0]["assignees"], [777])
        with open(pvm.STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual(state["last_update_id"], 42)
        mock_create_reminder.assert_not_called()
