#!/usr/bin/env python3
"""
Скрипт для обработки голосовых сообщений из Telegram и создания задач в ClickUp
"""

import argparse
import logging
import os
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import requests
from requests.exceptions import RequestException
from datetime import datetime, timedelta
import tempfile
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import dateparser
try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows only
    fcntl = None

try:
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - POSIX only
    msvcrt = None

from clickup_client import build_clickup_payload, create_clickup_task, create_clickup_reminder

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / "state.json"
LOCK_FILE = PROJECT_ROOT / ".processor.lock"
CACHE_DIR = PROJECT_ROOT / ".cache"
MEMBER_CACHE_FILE = CACHE_DIR / "clickup_members.json"
DEFAULT_LOG_RETENTION_DAYS = 30
DEFAULT_TASK_RETENTION_DAYS = 30
DEFAULT_MEMBER_CACHE_TTL_MINUTES = 30
DEFAULT_TRANSCRIPTION_MAX_CHARS = 4000
DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_REMINDER_OFFSET_HOURS = 2
DEFAULT_MAX_WORKERS = 3
DEFAULT_OPENAI_MAX_ATTEMPTS = 3
DEFAULT_DOWNLOAD_WORKERS = 3

logger = logging.getLogger("telegram_clickup")

ASSIGNEE_SPLIT_RE = re.compile(r"[;,/&]|\b(?:и|and)\b", re.IGNORECASE)
COMBINED_CONJUNCTION_RE = re.compile(r"\b(?:и\s*/\s*или|and\s*/\s*or)\b", re.IGNORECASE)
RETRYABLE_STATUS_CODES: Set[int] = {408, 425, 429, 500, 502, 503, 504}
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
@dataclass
class PreparedVoiceMessage:
    index: int
    voice: Dict[str, Any]
    log_entry: Dict[str, Any]
    audio_path: str


def configure_logging() -> None:
    """Настраивает базовое логирование по уровню из LOG_LEVEL."""

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


@contextmanager
def file_lock(lock_path: Path):
    """Гарантирует, что одновременно работает только один процесс."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None and msvcrt is None:
        yield
        return

    with open(lock_path, "w") as lock_file:
        locked = False
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                locked = True
            elif msvcrt is not None:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
        except OSError as exc:
            logger.warning("Не удалось установить блокировку %s: %s", lock_path, exc)
            yield
            return

        try:
            yield
        finally:
            if locked:
                try:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    elif msvcrt is not None:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass


def _request_with_retries(
    method: str,
    url: str,
    *,
    max_attempts: int = 4,
    backoff_factor: float = 1.5,
    retry_statuses: Optional[Set[int]] = None,
    **kwargs: Any,
) -> requests.Response:
    """Выполняет HTTP-запрос с повторными попытками и экспоненциальным ожиданием."""
    retry_statuses = retry_statuses or RETRYABLE_STATUS_CODES
    last_exc: Optional[RequestException] = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.request(method, url, **kwargs)
        except RequestException as exc:
            last_exc = exc
            if attempt >= max_attempts:
                raise
            sleep_for = backoff_factor * attempt
            logger.warning(
                "Запрос %s %s не удался (попытка %s/%s): %s. Повтор через %.1f с.",
                method.upper(),
                url,
                attempt,
                max_attempts,
                exc,
                sleep_for,
            )
            time.sleep(sleep_for)
            continue

        if response.status_code in retry_statuses and attempt < max_attempts:
            sleep_for = backoff_factor * (2 ** (attempt - 1))
            logger.warning(
                "Получен статус %s для %s %s (попытка %s/%s). Повтор через %.1f с.",
                response.status_code,
                method.upper(),
                url,
                attempt,
                max_attempts,
                sleep_for,
            )
            response.close()
            time.sleep(sleep_for)
            continue

        try:
            response.raise_for_status()
            return response
        except RequestException as exc:
            last_exc = exc
            response.close()
            if attempt >= max_attempts:
                raise
            sleep_for = backoff_factor * (2 ** (attempt - 1))
            logger.warning(
                "Ответ %s %s вернул ошибку (попытка %s/%s): %s. Повтор через %.1f с.",
                method.upper(),
                url,
                attempt,
                max_attempts,
                exc,
                sleep_for,
            )
            time.sleep(sleep_for)

    if last_exc:
        raise last_exc
    raise RuntimeError(f"Не удалось выполнить запрос {method} {url}")


def _execute_with_retry(
    operation: Callable[[], Any],
    description: str,
    *,
    max_attempts: int = DEFAULT_OPENAI_MAX_ATTEMPTS,
    base_delay: float = 2.0,
) -> Any:
    """Повторяет выполнение произвольной операции с экспоненциальным ожиданием."""

    last_exc: Optional[Exception] = None
    attempts = max(1, max_attempts)

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except KeyboardInterrupt:  # pragma: no cover - важно пропускать
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= attempts:
                break
            sleep_for = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "%s не удался (попытка %s/%s): %s. Повтор через %.1f с.",
                description,
                attempt,
                attempts,
                exc,
                sleep_for,
            )
            time.sleep(sleep_for)

    if last_exc:
        raise last_exc
    raise RuntimeError(f"{description} завершился без результата")


def _atomic_write_text(file_path: Path, content: str) -> None:
    """Атомарно записывает текст, чтобы избежать порчи файла при сбое."""

    file_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(file_path.parent), delete=False
    ) as tmp_file:
        tmp_file.write(content)
        tmp_path = Path(tmp_file.name)

    os.replace(tmp_path, file_path)
    try:
        os.chmod(file_path, 0o600)
    except OSError:
        pass


def _atomic_write_json(file_path: Path, payload: Dict[str, Any]) -> None:
    """Сохраняет JSON атомарно, чтобы не повредить файл при прерывании."""

    content = json.dumps(payload, ensure_ascii=False, indent=2)
    _atomic_write_text(file_path, content)


def _cleanup_file(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError:
        pass


def _prepare_audio_job(
    index: int,
    voice_message: Dict[str, Any],
    vm_log: Dict[str, Any],
    bot_token: str,
) -> PreparedVoiceMessage:
    audio_type_label = "голосового" if voice_message.get('type') == 'voice' else "аудио"
    forwarded_label = " (пересланное)" if voice_message.get('is_forwarded') else ""
    logger.info(
        "Обработка %s%s от %s",
        audio_type_label,
        forwarded_label,
        voice_message.get('from_user'),
    )

    suffix = _guess_audio_suffix(voice_message.get('mime_type', 'audio/ogg'))
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
        audio_path = tmp_file.name

    try:
        download_audio_file(bot_token, voice_message['file_id'], audio_path)
    except Exception:
        _cleanup_file(audio_path)
        raise

    logger.info("Аудио файл скачан: %s", audio_path)
    return PreparedVoiceMessage(
        index=index,
        voice=voice_message,
        log_entry=vm_log,
        audio_path=audio_path,
    )


def _load_member_cache(list_id: str, ttl_minutes: int) -> Optional[Dict[str, List[int]]]:
    """Читает кэш участников ClickUp, если он свежий."""

    if ttl_minutes <= 0 or not MEMBER_CACHE_FILE.exists():
        return None

    try:
        with open(MEMBER_CACHE_FILE, "r", encoding="utf-8") as cache_file:
            cache_data = json.load(cache_file)
    except (OSError, json.JSONDecodeError):
        return None

    lists_data = cache_data.get("lists")
    if not isinstance(lists_data, dict):
        return None

    record = lists_data.get(list_id)
    if not isinstance(record, dict):
        return None

    fetched_at_str = record.get("fetched_at")
    if not isinstance(fetched_at_str, str):
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched_at_str)
    except ValueError:
        return None

    if datetime.now() - fetched_at > timedelta(minutes=ttl_minutes):
        return None

    members = record.get("members")
    if not isinstance(members, dict):
        return None

    validated: Dict[str, List[int]] = {}
    for key, value in members.items():
        if not isinstance(key, str) or not isinstance(value, list):
            continue
        cleaned: List[int] = []
        for element in value:
            try:
                cleaned.append(int(element))
            except (TypeError, ValueError):
                continue
        validated[key] = cleaned

    logger.debug(
        "Используем кэш участников ClickUp: %s (list_id=%s)", MEMBER_CACHE_FILE, list_id
    )
    return validated


def _save_member_cache(list_id: str, members: Dict[str, List[int]]) -> None:
    """Сохраняет кэш участников ClickUp."""

    MEMBER_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(MEMBER_CACHE_FILE, "r", encoding="utf-8") as cache_file:
            payload = json.load(cache_file)
    except (OSError, json.JSONDecodeError):
        payload = {}

    lists_data = payload.setdefault("lists", {})
    lists_data[list_id] = {
        "fetched_at": datetime.now().isoformat(),
        "members": members,
    }
    try:
        _atomic_write_json(MEMBER_CACHE_FILE, payload)
    except OSError as exc:  # pragma: no cover - крайне редкая ситуация
        logger.warning("Не удалось сохранить кэш участников %s: %s", MEMBER_CACHE_FILE, exc)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def cleanup_old_files(directory: Path, pattern: str, retention_days: int) -> None:
    """Удаляет файлы старше указанного количества дней."""

    if retention_days <= 0:
        return

    cutoff = datetime.now() - timedelta(days=retention_days)
    for file_path in directory.glob(pattern):
        try:
            mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
        except OSError as exc:
            logger.warning("Не удалось получить время изменения %s: %s", file_path, exc)
            continue

        if mtime <= cutoff:
            try:
                file_path.unlink()
            except OSError as exc:
                logger.warning("Не удалось удалить устаревший файл %s: %s", file_path, exc)




def _normalize_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _initial_vm_log(message: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'from_user': message.get('from_user'),
        'date': message.get('date'),
        'duration': message.get('duration'),
        'type': message.get('type'),
        'is_forwarded': message.get('is_forwarded', False),
        'update_id': message.get('update_id'),
    }


def _guess_audio_suffix(mime_type: Optional[str]) -> str:
    mime = (mime_type or "").lower()
    if "ogg" in mime:
        return ".ogg"
    if "mpeg" in mime or "mp3" in mime:
        return ".mp3"
    if "mp4" in mime or "m4a" in mime:
        return ".m4a"
    if "wav" in mime:
        return ".wav"
    return ".ogg"


def _store_transcription(vm_log: Dict[str, Any], text: str, enabled: bool, limit: int) -> None:
    if not enabled or limit == 0:
        return

    if limit > 0 and len(text) > limit:
        truncated = text[:limit].rstrip()
        vm_log['transcription'] = truncated + "…"
        vm_log['transcription_truncated'] = True
    else:
        vm_log['transcription'] = text


def build_summary_message(
    *,
    message_count: int,
    created: int,
    failed: int,
    duration_seconds: float,
    dry_run: bool,
    log_path: Path,
) -> str:
    parts = [
        "📋 Telegram → ClickUp",
        f"Сообщений: {message_count}",
        f"Создано задач: {created}",
        f"Ошибок: {failed}",
        f"Время: {duration_seconds:.1f} с",
    ]
    if dry_run:
        parts.append("Режим: dry-run")
    parts.append(f"Лог: {log_path.name}")
    return "\n".join(parts)


def send_summary_notification(bot_token: str, chat_id: str, text: str) -> None:
    if not bot_token or not chat_id or not text:
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        with _request_with_retries("post", url, json=payload, timeout=30):
            pass
    except RequestException as exc:
        logger.warning("Не удалось отправить сводку в Telegram: %s", exc)


def normalize_due_date_value(raw_value: Any, timezone_name: str) -> Optional[str]:
    if raw_value is None:
        return None

    if isinstance(raw_value, (list, tuple)) and raw_value:
        raw_value = raw_value[0]

    candidate_date: Optional[datetime.date] = None

    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIMEZONE)

    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return None
        if ISO_DATE_RE.match(text):
            try:
                candidate_date = datetime.strptime(text, "%Y-%m-%d").date()
            except ValueError:
                candidate_date = None
        else:
            settings = {
                "TIMEZONE": timezone_name,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": datetime.now(tz),
                "DATE_ORDER": "DMY",
                "STRICT_PARSING": False,
            }
            parsed = dateparser.parse(
                text,
                settings=settings,
                languages=["ru", "en"],
            )
            if not parsed:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            candidate_date = parsed.astimezone(tz).date()
    elif isinstance(raw_value, (int, float)):
        candidate_date = datetime.fromtimestamp(float(raw_value), tz).date()

    if not candidate_date:
        return None

    today = datetime.now(tz).date()
    if candidate_date < today:
        return None
    return candidate_date.isoformat()


def _max_voice_update_id(messages: List[Dict[str, Any]]) -> Optional[int]:
    ids = [vm.get('update_id') for vm in messages if isinstance(vm.get('update_id'), int)]
    return max(ids) if ids else None


def prepare_assignee_map(raw_map: Any) -> Dict[str, List[int]]:
    if not isinstance(raw_map, dict):
        return {}

    prepared: Dict[str, List[int]] = {}
    for raw_key, raw_value in raw_map.items():
        if not isinstance(raw_key, str):
            continue

        normalized_key = _normalize_name(raw_key)
        if not normalized_key:
            continue

        values = raw_value if isinstance(raw_value, list) else [raw_value]
        assignee_ids: List[int] = []
        for item in values:
            if item is None:
                continue
            try:
                assignee_ids.append(int(item))
            except (TypeError, ValueError):
                continue

        if assignee_ids:
            prepared[normalized_key] = assignee_ids

    return prepared


def prepare_alias_map(raw_aliases: Any) -> Dict[str, str]:
    if not isinstance(raw_aliases, dict):
        return {}

    alias_map: Dict[str, str] = {}
    for alias, canonical in raw_aliases.items():
        if not isinstance(alias, str) or not isinstance(canonical, str):
            continue
        alias_norm = _normalize_name(alias)
        canonical_norm = _normalize_name(canonical)
        if alias_norm and canonical_norm:
            alias_map[alias_norm] = canonical_norm
    return alias_map


def resolve_assignee_ids(
    assignee_value: Any,
    assignee_map: Dict[str, List[int]],
    alias_map: Optional[Dict[str, str]] = None,
) -> List[int]:
    if not assignee_value or not assignee_map:
        return []

    candidates: List[str] = []
    if isinstance(assignee_value, str):
        normalized_text = COMBINED_CONJUNCTION_RE.sub(" и ", assignee_value)
        candidates.append(assignee_value)
        candidates.extend(
            filter(None, (part.strip() for part in ASSIGNEE_SPLIT_RE.split(normalized_text)))
        )
    elif isinstance(assignee_value, list):
        for value in assignee_value:
            if isinstance(value, str):
                candidates.append(value)

    resolved: List[int] = []
    for candidate in candidates:
        normalized = _normalize_name(candidate)
        if not normalized:
            continue
        lookup_key = alias_map.get(normalized, normalized) if alias_map else normalized
        ids = assignee_map.get(lookup_key)
        if not ids:
            continue
        for member_id in ids:
            if member_id not in resolved:
                resolved.append(member_id)

    return resolved


def fetch_clickup_member_map(
    token: str,
    list_id: str,
    *,
    cache_ttl_minutes: Optional[int] = None,
) -> Dict[str, List[int]]:
    if not token or not list_id:
        return {}

    ttl_minutes = _safe_int(cache_ttl_minutes, 0) if cache_ttl_minutes is not None else 0
    cached = _load_member_cache(list_id, ttl_minutes)
    if cached is not None:
        return cached

    url = f"https://api.clickup.com/api/v2/list/{list_id}"
    headers = {"Authorization": token}

    try:
        with _request_with_retries("get", url, headers=headers, timeout=30) as response:
            data = response.json()
    except RequestException as exc:
        logger.warning("Не удалось загрузить участников списка ClickUp: %s", exc)
        return {}

    members = data.get("members") or []
    if not isinstance(members, list):
        return {}

    member_map: Dict[str, List[int]] = {}
    for member in members:
        user = member.get("user") if isinstance(member, dict) else None
        if not isinstance(user, dict):
            continue

        raw_id = user.get("id")
        try:
            member_id = int(raw_id)
        except (TypeError, ValueError):
            continue

        name_candidates = {
            user.get("username"),
            user.get("email"),
            user.get("color"),
            user.get("initials"),
        }

        profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
        name_candidates.update(
            {
                profile.get("first_name"),
                profile.get("last_name"),
                profile.get("full_name"),
            }
        )

        for candidate in filter(None, name_candidates):
            normalized = _normalize_name(candidate)
            if not normalized:
                continue
            members_for_key = member_map.setdefault(normalized, [])
            if member_id not in members_for_key:
                members_for_key.append(member_id)

    if ttl_minutes > 0:
        _save_member_cache(list_id, member_map)

    return member_map


def load_state() -> Dict[str, Any]:
    """Загружает сохраненное состояние обработки."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            # Игнорируем поврежденный файл состояния
            return {}
    return {}


def save_state(state: Dict[str, Any]) -> None:
    """Сохраняет состояние обработки."""
    _atomic_write_json(STATE_FILE, state)

# Загрузка конфигурации
def load_config() -> Dict[str, Any]:
    """Загружает и нормализует конфигурацию из файла."""
    config_path = PROJECT_ROOT / "config.json"
    with open(config_path, 'r', encoding='utf-8') as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise ValueError("config.json должен содержать JSON-объект")

    return normalize_config(raw)


def normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(config)

    normalized['telegram_check_hours'] = max(1, _safe_int(config.get('telegram_check_hours'), 1))

    default_priority = _safe_int(config.get('default_priority'), 3)
    if default_priority not in (1, 2, 3, 4):
        default_priority = 3
    normalized['default_priority'] = default_priority

    normalized['log_retention_days'] = max(
        0,
        _safe_int(config.get('log_retention_days'), DEFAULT_LOG_RETENTION_DAYS),
    )
    normalized['tasks_retention_days'] = max(
        0,
        _safe_int(config.get('tasks_retention_days'), DEFAULT_TASK_RETENTION_DAYS),
    )

    normalized['store_transcriptions'] = _to_bool(
        config.get('store_transcriptions'),
        True,
    )
    normalized['transcription_max_chars'] = max(
        0,
        _safe_int(
            config.get('transcription_max_chars'),
            DEFAULT_TRANSCRIPTION_MAX_CHARS,
        ),
    )

    normalized['clickup_member_cache_hours'] = max(
        0,
        _safe_int(
            config.get('clickup_member_cache_hours'),
            max(1, DEFAULT_MEMBER_CACHE_TTL_MINUTES // 60),
        ),
    )

    normalized['openai_max_workers'] = max(
        1,
        _safe_int(config.get('openai_max_workers'), DEFAULT_MAX_WORKERS),
    )

    attempts_value = _safe_int(
        config.get('openai_max_attempts'),
        DEFAULT_OPENAI_MAX_ATTEMPTS,
    )
    normalized['openai_max_attempts'] = (
        attempts_value if attempts_value > 0 else DEFAULT_OPENAI_MAX_ATTEMPTS
    )

    normalized['download_max_workers'] = max(
        1,
        _safe_int(
            config.get('download_max_workers'),
            DEFAULT_DOWNLOAD_WORKERS,
        ),
    )

    normalized['create_clickup_reminders'] = _to_bool(
        config.get('create_clickup_reminders'),
        True,
    )
    normalized['reminder_offset_hours'] = max(
        0,
        _safe_int(
            config.get('reminder_offset_hours'),
            DEFAULT_REMINDER_OFFSET_HOURS,
        ),
    )

    normalized['send_summary_to_telegram'] = _to_bool(
        config.get('send_summary_to_telegram'),
        False,
    )
    summary_chat = config.get('summary_chat_id')
    normalized['summary_chat_id'] = str(summary_chat).strip() if summary_chat else ""

    timezone_name = str(config.get('timezone') or DEFAULT_TIMEZONE).strip()
    try:
        ZoneInfo(timezone_name)
    except Exception:
        timezone_name = DEFAULT_TIMEZONE
    normalized['timezone'] = timezone_name

    if not isinstance(normalized.get('assignee_map'), dict):
        normalized['assignee_map'] = {}
    if not isinstance(normalized.get('assignee_aliases'), dict):
        normalized['assignee_aliases'] = {}

    clickup_id = normalized.get('clickup_list_id')
    if clickup_id is not None:
        normalized['clickup_list_id'] = str(clickup_id).strip()
    team_id = normalized.get('clickup_team_id')
    if team_id is not None:
        normalized['clickup_team_id'] = str(team_id).strip()

    return normalized

# Загрузка API секретов
def load_api_secrets(require_telegram_openai: bool = True):
    """Загружает API секреты из файла"""
    # 1) Пробуем взять из переменных окружения
    secrets = {
        'bot_token': os.getenv('TELEGRAM_BOT_TOKEN'),
        'chat_id': os.getenv('TELEGRAM_CHAT_ID'),
        'openai_api_key': os.getenv('OPENAI_API_KEY'),
        'clickup_token': os.getenv('CLICKUP_TOKEN'),
    }

    primary_keys = ('bot_token', 'chat_id', 'openai_api_key')
    missing_primary = [key for key in primary_keys if not secrets[key]]
    missing_clickup = not secrets['clickup_token']

    # 2) Файл секретов в домашней директории (дополняем недостающие значения)
    if missing_primary or missing_clickup:
        secrets_path = Path.home() / ".api_secret_infos" / "api_secrets.json"
        if secrets_path.exists():
            with open(secrets_path, 'r', encoding='utf-8') as f:
                all_secrets = json.load(f)
            telegram_secrets = all_secrets.get('TELEGRAM', {}).get('secrets', {})
            openai_secrets = all_secrets.get('OPENAI', {}).get('secrets', {})
            clickup_secrets = all_secrets.get('CLICKUP', {}).get('secrets', {})

            if not secrets['bot_token']:
                secrets['bot_token'] = telegram_secrets.get('BOT_TOKEN')
            if not secrets['chat_id']:
                secrets['chat_id'] = telegram_secrets.get('CHAT_ID')
            if not secrets['openai_api_key']:
                secrets['openai_api_key'] = openai_secrets.get('API_KEY')
            if not secrets['clickup_token']:
                secrets['clickup_token'] = clickup_secrets.get('API_TOKEN') or clickup_secrets.get('TOKEN')

    if require_telegram_openai and any(not secrets[key] for key in primary_keys):
        raise FileNotFoundError(
            "Не найдены секреты Telegram/OpenAI. Установите переменные окружения TELEGRAM_BOT_TOKEN, "
            "TELEGRAM_CHAT_ID, OPENAI_API_KEY или дополните файл ~/.api_secret_infos/api_secrets.json."
        )

    if not secrets['clickup_token']:
        raise FileNotFoundError(
            "Не найден ClickUp токен. Установите переменную окружения CLICKUP_TOKEN или добавьте секцию CLICKUP "
            "в ~/.api_secret_infos/api_secrets.json."
        )

    return secrets

def get_recent_voice_messages(
    bot_token: str,
    chat_id: str,
    hours_back: int = 1,
    last_update_id: Optional[int] = None
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """
    Получает голосовые и аудио сообщения из Telegram группы за последние N часов
    Обрабатывает: обычные голосовые, пересланные голосовые, аудио файлы, channel_post
    """
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"

    limit = 100
    apply_time_filter = last_update_id is None and bool(hours_back)
    cutoff_time = None
    if apply_time_filter:
        cutoff_time = datetime.now() - timedelta(hours=hours_back)
    voice_messages: List[Dict[str, Any]] = []
    max_update_id = last_update_id
    chat_id_int = int(chat_id)
    offset = last_update_id + 1 if last_update_id is not None else None

    while True:
        params: Dict[str, Any] = {'limit': limit}
        if offset is not None:
            params['offset'] = offset

        with _request_with_retries("get", url, params=params, timeout=30) as response:
            data = response.json()

        if not data.get('ok'):
            raise Exception(f"Telegram API error: {data}")

        updates = data.get('result', [])

        for update in updates:
            update_id = update.get('update_id')
            if update_id is not None:
                max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)

            message = update.get('message') or update.get('channel_post') or update.get('edited_message')
            if not message:
                continue

            message_chat_id = message.get('chat', {}).get('id')
            if message_chat_id != chat_id_int:
                continue

            msg_time = datetime.fromtimestamp(message['date'])
            if cutoff_time and msg_time <= cutoff_time:
                continue

            from_user = 'Unknown'
            if 'from' in message:
                from_user = message['from'].get('first_name', 'Unknown')
            elif 'forward_from' in message:
                from_user = message['forward_from'].get('first_name', 'Unknown')
            elif 'forward_origin' in message:
                origin = message['forward_origin']
                if origin.get('type') == 'user' and 'sender_user' in origin:
                    from_user = origin['sender_user'].get('first_name', 'Unknown')
            elif 'sender_chat' in message:
                from_user = message['sender_chat'].get('title', 'Channel')

            audio_data = None
            audio_type = None

            if 'voice' in message:
                audio_data = message['voice']
                audio_type = 'voice'
            elif 'audio' in message:
                audio_data = message['audio']
                audio_type = 'audio'

            if audio_data:
                voice_messages.append({
                    'update_id': update_id,
                    'file_id': audio_data['file_id'],
                    'duration': audio_data.get('duration', 0),
                    'date': msg_time.isoformat(),
                    'from_user': from_user,
                    'type': audio_type,
                    'mime_type': audio_data.get('mime_type', 'unknown'),
                    'is_forwarded': 'forward_from' in message or 'forward_origin' in message
                })

        if not updates or len(updates) < limit:
            break

        if max_update_id is None:
            break

        offset = max_update_id + 1

    return voice_messages, max_update_id

def download_audio_file(bot_token, file_id, output_path):
    """
    Скачивает голосовой или аудио файл из Telegram
    """
    # Получаем путь к файлу
    url = f"https://api.telegram.org/bot{bot_token}/getFile"
    try:
        with _request_with_retries("get", url, params={'file_id': file_id}, timeout=30) as response:
            data = response.json()
    except RequestException as exc:
        raise RuntimeError(f"Failed to get Telegram file metadata: {exc}") from exc

    if not data.get('ok'):
        raise Exception(f"Failed to get file path: {data}")

    file_path = data['result']['file_path']

    # Скачиваем файл
    download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    try:
        with _request_with_retries("get", download_url, stream=True, timeout=60) as response:
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
    except RequestException as exc:
        raise RuntimeError(f"Failed to download Telegram file: {exc}") from exc

    return output_path

_openai_clients: Dict[str, Any] = {}


def get_openai_client(api_key: str):
    if api_key in _openai_clients:
        return _openai_clients[api_key]

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    _openai_clients[api_key] = client
    return client


def transcribe_audio(
    audio_path: str,
    api_key: str,
    *,
    max_attempts: int = DEFAULT_OPENAI_MAX_ATTEMPTS,
) -> str:
    """Транскрибирует аудио файл через OpenAI Whisper API."""

    client = get_openai_client(api_key)

    def _operation() -> str:
        with open(audio_path, 'rb') as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru",
            )
        return transcript.text

    return _execute_with_retry(
        _operation,
        "Whisper транскрипция",
        max_attempts=max_attempts,
    )


def extract_tasks_from_text(
    text: str,
    api_key: str,
    *,
    max_attempts: int = DEFAULT_OPENAI_MAX_ATTEMPTS,
) -> List[Dict[str, Any]]:
    """Использует GPT для извлечения задач из транскрипции."""

    client = get_openai_client(api_key)
    
    prompt = f"""
Проанализируй следующий текст из голосового сообщения и извлеки все упомянутые задачи.
Для каждой задачи определи:
- Название задачи (краткое, до 100 символов)
- Описание задачи (подробное)
- Дедлайн (если упомянут, в формате YYYY-MM-DD, если нет - оставь null)
- Приоритет (1 - срочно, 2 - высокий, 3 - нормальный, 4 - низкий)
- Ответственный (имя человека, если упомянуто, иначе null)

Верни результат в формате JSON массива:
[
  {{
    "name": "Название задачи",
    "description": "Подробное описание",
    "due_date": "2025-10-05" или null,
    "priority": 3,
    "assignee": "Имя" или null
  }}
]

Текст голосового сообщения:
{text}
"""
    
    def _operation():
        return client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
            temperature=0.3,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "extracted_tasks",
                    "schema": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "due_date": {"type": ["string", "null"]},
                                "priority": {
                                    "type": ["integer", "null"],
                                    "minimum": 1,
                                    "maximum": 4
                                },
                                "assignee": {"type": ["string", "null"]}
                            },
                            "required": ["name", "description", "due_date", "priority", "assignee"],
                            "additionalProperties": False
                        }
                    }
                }
            },
        )

    response = _execute_with_retry(
        _operation,
        "Извлечение задач через GPT",
        max_attempts=max_attempts,
    )

    # Парсим JSON ответ согласно новому SDK
    parsed_output = getattr(response, "parsed", None)
    if parsed_output is not None:
        tasks = parsed_output
    else:
        output_items = getattr(response, "output", []) or []
        if not output_items or not getattr(output_items[0], "content", None):
            raise ValueError("GPT не вернул текст с задачами при извлечении")

        content_blocks = output_items[0].content
        text_blocks = [block for block in content_blocks if getattr(block, "type", "") == "output_text"]
        if not text_blocks:
            raise ValueError("GPT вернул ответ без текстового содержимого для задач")

        tasks_json = getattr(text_blocks[0], "text", "").strip()
        if not tasks_json:
            raise ValueError("GPT вернул пустой текст при извлечении задач")

        try:
            tasks = json.loads(tasks_json)
        except json.JSONDecodeError as json_err:
            raise ValueError(
                "Не удалось преобразовать ответ модели в JSON несмотря на требуемый формат"
            ) from json_err

    if not isinstance(tasks, list):
        raise ValueError("Ответ GPT должен быть списком задач")

    for idx, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"Элемент #{idx} ответа GPT не является объектом задачи")

    return tasks


def _transcribe_and_extract(
    audio_path: str,
    api_key: str,
    max_attempts: int,
) -> Tuple[str, List[Dict[str, Any]]]:
    transcription = transcribe_audio(
        audio_path,
        api_key,
        max_attempts=max_attempts,
    )
    tasks = extract_tasks_from_text(
        transcription,
        api_key,
        max_attempts=max_attempts,
    )
    return transcription, tasks

def save_processing_log(log_data, log_file):
    """
    Сохраняет лог обработки в файл
    """

    log_path = Path(log_file)
    fragments: List[str] = []
    fragments.append("# Отчет об обработке голосовых и аудио сообщений Telegram\n\n")
    fragments.append(f"**Дата обработки:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

    if not log_data['voice_messages']:
        fragments.append("## Результат\n\nНовых голосовых/аудио сообщений не найдено.\n")
        _atomic_write_text(log_path, "".join(fragments))
        return

    fragments.append(f"## Обработано сообщений: {len(log_data['voice_messages'])}\n\n")

    for idx, vm in enumerate(log_data['voice_messages'], 1):
        msg_type = "Голосовое" if vm.get('type') == 'voice' else "Аудио"
        forwarded = " (пересланное)" if vm.get('is_forwarded') else ""
        fragments.append(f"### {msg_type} сообщение #{idx}{forwarded}\n\n")
        fragments.append(f"- **От:** {vm['from_user']}\n")
        fragments.append(f"- **Дата:** {vm['date']}\n")
        fragments.append(f"- **Длительность:** {vm['duration']} сек\n")
        fragments.append(f"- **Тип:** {vm.get('type', 'voice')}\n\n")

        if 'transcription' in vm:
            fragments.append(f"**Транскрипция:**\n```\n{vm['transcription']}\n```\n\n")

        if 'error' in vm:
            fragments.append(f"**⚠️ Ошибка:** {vm['error']}\n\n")
        elif 'tasks' in vm and vm['tasks']:
            fragments.append(f"**Извлечено задач:** {len(vm['tasks'])}\n\n")
            if vm.get('clickup_created'):
                fragments.append(f"- **Создано в ClickUp:** {vm['clickup_created']}\n")
            if vm.get('clickup_failed'):
                fragments.append(f"- **Ошибок создания:** {vm['clickup_failed']}\n")
            if vm.get('clickup_created') or vm.get('clickup_failed'):
                fragments.append("\n")
            for task_idx, task in enumerate(vm['tasks'], 1):
                fragments.append(f"#### Задача {task_idx}\n")
                fragments.append(f"- **Название:** {task['name']}\n")
                fragments.append(f"- **Описание:** {task['description']}\n")
                fragments.append(f"- **Дедлайн:** {task.get('due_date', 'Не указан')}\n")
                fragments.append(f"- **Приоритет:** {task.get('priority', 3)}\n")
                fragments.append(f"- **Ответственный:** {task.get('assignee', 'Не указан')}\n")
                if 'clickup_task_id' in task:
                    fragments.append(f"- **ClickUp Task ID:** {task['clickup_task_id']}\n")
                if task.get('clickup_error'):
                    fragments.append(f"- **Ошибка ClickUp:** {task['clickup_error']}\n")
                fragments.append("\n")
        else:
            fragments.append("**Задач не найдено**\n\n")

        fragments.append("---\n\n")

    fragments.append(f"\n## Итого создано задач в ClickUp: {log_data['total_tasks_created']}\n")
    if log_data.get('total_tasks_failed'):
        fragments.append(f"## Итого ошибок создания задач: {log_data['total_tasks_failed']}\n")

    _atomic_write_text(log_path, "".join(fragments))

def run_once(*, dry_run: bool = False, limit_messages: Optional[int] = None) -> str:
    """Выполняет одну итерацию обработки."""
    start_time = time.time()
    logger.info(
        "Запуск обработки голосовых сообщений из Telegram%s...",
        " (dry-run)" if dry_run else "",
    )

    # Загружаем конфигурацию и секреты
    config = load_config()
    secrets = load_api_secrets()
    
    bot_token = secrets['bot_token']
    chat_id = secrets['chat_id']
    openai_api_key = secrets['openai_api_key']
    clickup_token = secrets['clickup_token']
    
    if not bot_token or not chat_id:
        raise Exception("Не найдены BOT_TOKEN или CHAT_ID в секретах")
    
    if not openai_api_key:
        raise Exception("Не найден OPENAI API_KEY в секретах")

    clickup_list_id = str(config.get('clickup_list_id', '')).strip()
    if not clickup_list_id:
        raise Exception("В config.json отсутствует clickup_list_id")

    default_priority = config.get('default_priority', 3)
    log_retention_days = config.get('log_retention_days', DEFAULT_LOG_RETENTION_DAYS)
    tasks_retention_days = config.get('tasks_retention_days', DEFAULT_TASK_RETENTION_DAYS)
    store_transcriptions = config.get('store_transcriptions', True)
    transcription_max_chars = config.get('transcription_max_chars', DEFAULT_TRANSCRIPTION_MAX_CHARS)
    timezone_name = config.get('timezone', DEFAULT_TIMEZONE)
    member_cache_minutes = max(0, config.get('clickup_member_cache_hours', 1)) * 60
    alias_map = prepare_alias_map(config.get('assignee_aliases'))

    reminder_offset_hours = config.get('reminder_offset_hours', DEFAULT_REMINDER_OFFSET_HOURS)
    reminder_offset_ms = reminder_offset_hours * 3600 * 1000
    clickup_team_id = config.get('clickup_team_id')
    reminders_enabled = bool(clickup_team_id and config.get('create_clickup_reminders', True))
    summary_enabled = bool(config.get('send_summary_to_telegram', False))
    summary_chat_id = str(config.get('summary_chat_id') or chat_id).strip() or chat_id

    clickup_member_map = fetch_clickup_member_map(
        clickup_token,
        clickup_list_id,
        cache_ttl_minutes=member_cache_minutes,
    )
    config_assignee_map = prepare_assignee_map(config.get('assignee_map'))
    assignee_map = dict(clickup_member_map)
    assignee_map.update(config_assignee_map)
    
    logger.info("Проверка голосовых сообщений в чате %s...", chat_id)
    
    # Получаем голосовые сообщения за последний час
    state = load_state()
    last_update_id = state.get('last_update_id')
    hours_back = config.get('telegram_check_hours', 1)
    voice_messages, max_update_id = get_recent_voice_messages(
        bot_token,
        chat_id,
        hours_back=hours_back,
        last_update_id=last_update_id
    )

    if limit_messages is not None and limit_messages >= 0:
        voice_messages = voice_messages[:limit_messages]
        logger.info(
            "Найдено голосовых сообщений: %s (ограничено %s)",
            len(voice_messages),
            limit_messages,
        )
    else:
        logger.info("Найдено голосовых сообщений: %s", len(voice_messages))

    processed_update_id = _max_voice_update_id(voice_messages)
    
    log_data = {
        'voice_messages': [],
        'total_tasks_created': 0,
        'total_tasks_failed': 0,
        'clickup_list_id': clickup_list_id
    }
    openai_workers = config.get('openai_max_workers', DEFAULT_MAX_WORKERS)
    openai_max_attempts = config.get('openai_max_attempts', DEFAULT_OPENAI_MAX_ATTEMPTS)
    download_workers = config.get('download_max_workers', DEFAULT_DOWNLOAD_WORKERS)
    prepared_messages: List[PreparedVoiceMessage] = []

    if voice_messages:
        ordered_logs = [_initial_vm_log(vm) for vm in voice_messages]
        logger.info(
            "Загрузка аудио файлов (%s потоков)",
            download_workers,
        )
        with ThreadPoolExecutor(max_workers=download_workers) as executor:
            future_map = {
                executor.submit(
                    _prepare_audio_job,
                    idx,
                    vm,
                    ordered_logs[idx],
                    bot_token,
                ): idx
                for idx, vm in enumerate(voice_messages)
            }
            for future in as_completed(future_map):
                idx = future_map[future]
                vm_log = ordered_logs[idx]
                try:
                    prepared_messages.append(future.result())
                except Exception as exc:
                    logger.exception(
                        "Ошибка при подготовке сообщения от %s",
                        vm_log.get('from_user'),
                    )
                    vm_log['error'] = str(exc)
    else:
        ordered_logs = []

    if prepared_messages:
        logger.info(
            "Параллельная обработка аудио: %s поток(ов) OpenAI",
            openai_workers,
        )
        with ThreadPoolExecutor(max_workers=openai_workers) as executor:
            future_map = {
                executor.submit(
                    _transcribe_and_extract,
                    item.audio_path,
                    openai_api_key,
                    openai_max_attempts,
                ): item
                for item in prepared_messages
            }
            for future in as_completed(future_map):
                prepared = future_map[future]
                vm = prepared.voice
                vm_log = prepared.log_entry
                try:
                    transcription, tasks = future.result()
                except Exception as exc:
                    logger.exception(
                        "Ошибка при обработке сообщения от %s",
                        vm.get('from_user'),
                    )
                    vm_log['error'] = str(exc)
                else:
                    logger.debug("Фрагмент транскрипции: %s...", transcription[:100])
                    _store_transcription(
                        vm_log,
                        transcription,
                        store_transcriptions,
                        transcription_max_chars,
                    )
                    logger.info("Извлечено задач: %s", len(tasks))
                    vm_log['tasks'] = tasks

                    created_for_message = 0
                    for task in tasks:
                        task['due_date'] = normalize_due_date_value(task.get('due_date'), timezone_name)
                        assignee_ids = resolve_assignee_ids(task.get('assignee'), assignee_map, alias_map)
                        if assignee_ids:
                            task['assignee_ids'] = assignee_ids
                        payload = build_clickup_payload(
                            task,
                            default_priority=default_priority,
                            assignee_ids=assignee_ids,
                        )
                        task_name = payload.get('name', 'Без названия')

                        if dry_run:
                            task['clickup_dry_run'] = True
                            logger.info("[dry-run] Пропущено создание задачи в ClickUp: %s", task_name)
                            continue

                        try:
                            response = create_clickup_task(clickup_token, clickup_list_id, payload)
                        except Exception as create_err:
                            task['clickup_error'] = str(create_err)
                            log_data['total_tasks_failed'] += 1
                            vm_log['clickup_failed'] = vm_log.get('clickup_failed', 0) + 1
                            logger.error("Ошибка создания задачи '%s': %s", task_name, create_err)
                            continue

                        task_id = response.get("id") or response.get("task", {}).get("id")
                        if task_id:
                            task['clickup_task_id'] = task_id
                            created_for_message += 1
                            log_data['total_tasks_created'] += 1
                            logger.info("Создана задача в ClickUp: %s — %s", task_id, task_name)
                            if (
                                reminders_enabled
                                and payload.get('due_date')
                                and reminder_offset_ms > 0
                            ):
                                remind_time = payload['due_date'] - reminder_offset_ms
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
                                    except Exception as reminder_err:  # pragma: no cover - сеть/API
                                        logger.warning(
                                            "Не удалось создать напоминание для %s: %s",
                                            task_id,
                                            reminder_err,
                                        )
                                    else:
                                        task['clickup_reminder'] = remind_time
                        else:
                            task['clickup_error'] = "Task created without ID in response"
                            log_data['total_tasks_failed'] += 1
                            vm_log['clickup_failed'] = vm_log.get('clickup_failed', 0) + 1
                            logger.warning("Задача создана без ID в ответе — %s", task_name)

                    if created_for_message:
                        vm_log['clickup_created'] = created_for_message
                finally:
                    _cleanup_file(prepared.audio_path)

    # Собираем логи в исходном порядке
    log_data['voice_messages'] = [log for log in ordered_logs if log]

    state_update_id = processed_update_id if processed_update_id is not None else max_update_id
    if state_update_id is not None and state_update_id != last_update_id:
        state['last_update_id'] = state_update_id
        save_state(state)

    # Сохраняем лог
    logs_dir = PROJECT_ROOT / "logs"
    os.makedirs(logs_dir, exist_ok=True)
    log_path = logs_dir / f"processing_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    save_processing_log(log_data, log_path)
    logger.info("Лог сохранен: %s", log_path)
    cleanup_old_files(logs_dir, "processing_log_*.md", log_retention_days)
    
    # Сохраняем данные задач для следующего шага
    tasks_path = PROJECT_ROOT / f"tasks_to_create_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    _atomic_write_json(tasks_path, log_data)
    logger.info("Данные задач сохранены: %s", tasks_path)
    cleanup_old_files(PROJECT_ROOT, "tasks_to_create_*.json", tasks_retention_days)

    duration_seconds = time.time() - start_time
    logger.info(
        "Обработка завершена. Всего задач создано: %s, ошибок: %s",
        log_data['total_tasks_created'],
        log_data['total_tasks_failed'],
    )

    summary_text = build_summary_message(
        message_count=len(log_data['voice_messages']),
        created=log_data['total_tasks_created'],
        failed=log_data['total_tasks_failed'],
        duration_seconds=duration_seconds,
        dry_run=dry_run,
        log_path=log_path,
    )
    logger.info("Краткая сводка:\n%s", summary_text)
    if summary_enabled:
        send_summary_notification(bot_token, summary_chat_id, summary_text)

    return str(tasks_path)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Обработка голосовых сообщений Telegram → ClickUp")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Не создавать задачи в ClickUp, только выводить результат",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Обработать не более N недавно найденных сообщений",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None):
    """Точка входа скрипта с блокировкой от параллельных запусков."""
    args = parse_args(argv)
    configure_logging()
    logger.info("Попытка получить блокировку %s", LOCK_FILE)
    with file_lock(LOCK_FILE):
        return run_once(dry_run=args.dry_run, limit_messages=args.limit)

if __name__ == "__main__":
    main()
