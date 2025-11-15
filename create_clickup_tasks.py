#!/usr/bin/env python3
"""Утилита для повторного создания задач в ClickUp."""

import argparse
import json
import glob
import logging
import time
from pathlib import Path
from typing import List, Optional

from clickup_client import build_clickup_payload, create_clickup_task, create_clickup_reminder
from process_voice_messages import (
    _atomic_write_json,
    _safe_int,
    configure_logging,
    fetch_clickup_member_map,
    load_api_secrets,
    load_config,
    prepare_assignee_map,
    prepare_alias_map,
    normalize_due_date_value,
    resolve_assignee_ids,
)


PROJECT_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("telegram_clickup.recreate")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        type=Path,
        help="Путь к tasks_to_create_*.json. Если не указан — используется последний файл.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Не создавать задачи в ClickUp, только проверить полезную нагрузку.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Игнорировать существующие clickup_task_id и создавать задачи повторно.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Создать не более N задач (для осторожных повторных запусков).",
    )
    return parser.parse_args(argv)


def find_latest_tasks_file() -> Path:
    """Находит последний файл tasks_to_create_*.json в корне проекта."""
    pattern = str(PROJECT_ROOT / "tasks_to_create_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        raise FileNotFoundError("Не найден tasks_to_create_*.json. Сначала запустите process_voice_messages.py")
    return Path(files[0])


def main(argv: Optional[List[str]] = None):
    configure_logging()
    args = parse_args(argv)
    if args.file:
        tasks_file = args.file
        if not tasks_file.exists():
            raise FileNotFoundError(f"Указанный файл не найден: {tasks_file}")
    else:
        logger.info("Поиск последнего файла с задачами...")
        tasks_file = find_latest_tasks_file()
    logger.info("Используется файл: %s", tasks_file)

    config = load_config()
    secrets = load_api_secrets(require_telegram_openai=False)
    clickup_token = secrets["clickup_token"]
    with open(tasks_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    list_id = str(data.get("clickup_list_id") or config.get("clickup_list_id") or "").strip()
    if not list_id:
        raise RuntimeError("Не удалось определить ClickUp list_id (нет в JSON и config.json)")

    default_priority = config.get("default_priority", 3)
    member_cache_minutes = max(
        0,
        _safe_int(config.get("clickup_member_cache_hours"), 1),
    ) * 60
    clickup_member_map = fetch_clickup_member_map(
        clickup_token,
        list_id,
        cache_ttl_minutes=member_cache_minutes,
    )
    assignee_map = dict(clickup_member_map)
    assignee_map.update(prepare_assignee_map(config.get("assignee_map")))
    alias_map = prepare_alias_map(config.get("assignee_aliases"))
    timezone_name = config.get("timezone")

    reminder_offset_hours = config.get("reminder_offset_hours", 2)
    reminder_offset_ms = reminder_offset_hours * 3600 * 1000
    clickup_team_id = config.get("clickup_team_id")
    reminders_enabled = bool(clickup_team_id and config.get("create_clickup_reminders", True))

    total_created = 0
    skipped_existing = 0
    skipped_dry_run = 0
    processed = 0

    limit = args.limit if (args.limit is None or args.limit > 0) else None

    for vm in data.get("voice_messages", []):
        tasks = vm.get("tasks", [])
        for task in tasks:
            if limit is not None and processed >= limit:
                break
            processed += 1
            assignee_ids = task.get("assignee_ids")
            if not assignee_ids:
                resolved = resolve_assignee_ids(task.get("assignee"), assignee_map, alias_map)
                if resolved:
                    assignee_ids = resolved
                    task["assignee_ids"] = resolved
            task["due_date"] = normalize_due_date_value(
                task.get("due_date"),
                timezone_name,
            )
            payload = build_clickup_payload(
                task,
                default_priority=default_priority,
                assignee_ids=assignee_ids,
            )
            name = payload.get("name") or "Без названия"

            existing_id = task.get("clickup_task_id")
            if existing_id and not args.force:
                skipped_existing += 1
                logger.info(
                    "Пропуск задачи '%s' — уже есть ClickUp ID %s (используйте --force для пересоздания)",
                    name,
                    existing_id,
                )
                continue

            if args.dry_run:
                task["clickup_dry_run"] = True
                skipped_dry_run += 1
                logger.info("[dry-run] Пропуск создания задачи: %s", name)
                continue

            try:
                resp_json = create_clickup_task(clickup_token, list_id, payload)
                task_id = resp_json.get("id") or resp_json.get("task", {}).get("id")
                if task_id:
                    task["clickup_task_id"] = task_id
                    total_created += 1
                    logger.info("Создана задача в ClickUp: %s — %s", task_id, name)
                    if (
                        reminders_enabled
                        and payload.get("due_date")
                        and reminder_offset_ms > 0
                    ):
                        remind_time = payload["due_date"] - reminder_offset_ms
                        if remind_time > int(time.time() * 1000):
                            assignee_for_reminder = assignee_ids[0] if assignee_ids else None
                            try:
                                create_clickup_reminder(
                                    clickup_token,
                                    clickup_team_id,
                                    task_id,
                                    remind_time,
                                    assignee_for_reminder,
                                )
                            except Exception as reminder_err:
                                logger.warning(
                                    "Не удалось создать напоминание для %s: %s",
                                    task_id,
                                    reminder_err,
                                )
                            else:
                                task["clickup_reminder"] = remind_time
                else:
                    logger.warning("Создана задача без ID в ответе — %s", name)
            except Exception as e:
                task["clickup_error"] = str(e)
                logger.error("Ошибка создания задачи '%s': %s", name, e)
                continue

            # Небольшая пауза, чтобы не ловить rate limit только при реальном создании
            time.sleep(0.3)

        if limit is not None and processed >= limit:
            break
    if limit is not None and processed >= limit:
        logger.info("Достигнут лимит %s задач", limit)

    logger.info(
        "Итого: создано %s, пропущено (уже были) %s, пропущено (dry-run) %s, обработано %s",
        total_created,
        skipped_existing,
        skipped_dry_run,
        processed,
    )

    # Сохраняем расширенный файл результатов рядом
    out_file = tasks_file.with_name(tasks_file.stem + "_with_clickup.json")
    _atomic_write_json(out_file, data)
    logger.info("Результаты сохранены: %s", out_file)


if __name__ == "__main__":
    main()
