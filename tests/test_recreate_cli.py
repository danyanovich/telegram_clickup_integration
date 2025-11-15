import json
import tempfile
from pathlib import Path
from unittest import mock, TestCase

import create_clickup_tasks


class RecreateCliTests(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_config = {
            "clickup_list_id": "list123",
            "default_priority": 3,
            "assignee_map": {},
            "assignee_aliases": {},
            "clickup_member_cache_hours": 0,
            "timezone": "UTC",
            "reminder_offset_hours": 2,
            "create_clickup_reminders": False,
            "openai_max_workers": 1,
            "download_max_workers": 1,
        }

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_tasks(self, tasks):
        path = Path(self.tmpdir.name) / "tasks.json"
        payload = {
            "clickup_list_id": "list123",
            "voice_messages": [
                {
                    "tasks": tasks,
                }
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False))
        return path

    def _base_patches(self):
        return mock.patch.multiple(
            create_clickup_tasks,
            load_config=mock.DEFAULT,
            load_api_secrets=mock.DEFAULT,
            fetch_clickup_member_map=mock.DEFAULT,
            prepare_assignee_map=mock.DEFAULT,
            prepare_alias_map=mock.DEFAULT,
            create_clickup_task=mock.DEFAULT,
            create_clickup_reminder=mock.DEFAULT,
        )

    def test_dry_run_skips_creation(self):
        tasks_file = self._write_tasks([
            {"name": "Task A", "description": "", "priority": 3},
        ])
        with self._base_patches() as patches:
            patches["load_config"].return_value = self.base_config
            patches["load_api_secrets"].return_value = {"clickup_token": "token"}
            patches["fetch_clickup_member_map"].return_value = {}
            patches["prepare_assignee_map"].return_value = {}
            patches["prepare_alias_map"].return_value = {}
            create_clickup_tasks.main(["--file", str(tasks_file), "--dry-run"])

        patches["create_clickup_task"].assert_not_called()
        out_file = tasks_file.with_name(tasks_file.stem + "_with_clickup.json")
        data = json.loads(out_file.read_text())
        assert data["voice_messages"][0]["tasks"][0].get("clickup_dry_run") is True

    def test_force_recreates_existing_task(self):
        tasks_file = self._write_tasks([
            {"name": "Task B", "description": "", "clickup_task_id": "abc"},
        ])
        with self._base_patches() as patches:
            patches["load_config"].return_value = self.base_config
            patches["load_api_secrets"].return_value = {"clickup_token": "token"}
            patches["fetch_clickup_member_map"].return_value = {}
            patches["prepare_assignee_map"].return_value = {}
            patches["prepare_alias_map"].return_value = {}
            patches["create_clickup_task"].return_value = {"id": "new-task"}

            # Без --force задача пропускается
            create_clickup_tasks.main(["--file", str(tasks_file)])
            patches["create_clickup_task"].assert_not_called()

            # С --force задача создаётся повторно
            patches["create_clickup_task"].reset_mock()
            create_clickup_tasks.main(["--file", str(tasks_file), "--force"])
            patches["create_clickup_task"].assert_called_once()

    def test_limit_stops_after_n_tasks(self):
        tasks = [
            {"name": f"Task {idx}", "description": ""}
            for idx in range(3)
        ]
        tasks_file = self._write_tasks(tasks)
        with self._base_patches() as patches:
            patches["load_config"].return_value = self.base_config
            patches["load_api_secrets"].return_value = {"clickup_token": "token"}
            patches["fetch_clickup_member_map"].return_value = {}
            patches["prepare_assignee_map"].return_value = {}
            patches["prepare_alias_map"].return_value = {}
            patches["create_clickup_task"].side_effect = (
                {"id": f"id-{idx}"} for idx in range(10)
            )

            create_clickup_tasks.main(["--file", str(tasks_file), "--limit", "2"])

        assert patches["create_clickup_task"].call_count == 2
