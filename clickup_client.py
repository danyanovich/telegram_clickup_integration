"""Утилиты для создания задач в ClickUp через REST API."""

import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import ceil
from typing import Any, Dict

import requests


def to_epoch_millis(date_str: str) -> int:
    """Преобразует дату YYYY-MM-DD в миллисекунды Unix времени (UTC)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return int(dt.timestamp() * 1000)


def build_clickup_payload(task: Dict[str, Any], default_priority: int = 3) -> Dict[str, Any]:
    """
    Подготавливает полезную нагрузку для API ClickUp из словаря задачи.
    Некорректные значения дедлайна и приоритета игнорируются.
    """
    name = task.get("name") or "Без названия"
    description = task.get("description") or ""
    priority_raw = task.get("priority")
    priority = default_priority
    if isinstance(priority_raw, int):
        priority = priority_raw
    elif isinstance(priority_raw, str):
        try:
            priority = int(priority_raw)
        except ValueError:
            priority = default_priority

    if priority not in (1, 2, 3, 4):
        priority = default_priority

    payload: Dict[str, Any] = {
        "name": name,
        "description": description,
        "priority": priority,
    }

    due_date_str = task.get("due_date")
    if due_date_str and isinstance(due_date_str, str):
        try:
            payload["due_date"] = to_epoch_millis(due_date_str)
        except ValueError:
            # Игнорируем некорректный формат дедлайна
            pass

    return payload


def _parse_retry_after(value: str, default_seconds: int = 2) -> int:
    """Пытается преобразовать Retry-After к секундам ожидания.

    Сначала пытаемся интерпретировать как число секунд (дробное). Если не
    получилось, трактуем как HTTP-дату. В случае любой ошибки возвращаем
    значение по умолчанию.
    """

    if not value:
        logging.warning(
            "Retry-After header is missing; using default %s seconds", default_seconds
        )
        return default_seconds

    try:
        seconds = float(value)
        if seconds < 0:
            raise ValueError("Retry-After seconds cannot be negative")
        return ceil(seconds)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at is None:
                raise ValueError("Unable to parse Retry-After date")
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta_seconds = (retry_at - now).total_seconds()
            if delta_seconds <= 0:
                raise ValueError("Retry-After date is not in the future")
            return ceil(delta_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            logging.warning(
                "Failed to parse Retry-After header '%s': %s. Using default %s seconds.",
                value,
                exc,
                default_seconds,
            )
            return default_seconds


def create_clickup_task(token: str, list_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Создает задачу в ClickUp. При получении 429 выполняет повторную попытку.
    """
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
    }

    max_attempts = 4

    for attempt in range(max_attempts):
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        if response.status_code == 429 and attempt < max_attempts - 1:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            backoff = retry_after * (2 ** attempt)
            logging.info(
                "Received 429 from ClickUp. Waiting %s seconds before retry %s/%s.",
                backoff,
                attempt + 1,
                max_attempts,
            )
            time.sleep(backoff)
            continue
        response.raise_for_status()
        return response.json()

    # Если ни одна попытка не увенчалась успехом, поднимем исключение
    response.raise_for_status()
    return {}
