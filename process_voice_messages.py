#!/usr/bin/env python3
"""
Скрипт для обработки голосовых сообщений из Telegram и создания задач в ClickUp
"""

import logging
import os
import json
import re
import requests
from requests.exceptions import RequestException
from datetime import datetime, timedelta
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from clickup_client import build_clickup_payload, create_clickup_task

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / "state.json"


ASSIGNEE_SPLIT_RE = re.compile(r"[;,/]|\\b(?:и|and|&|и/или)\\b", re.IGNORECASE)


def _normalize_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


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


def resolve_assignee_ids(assignee_value: Any, assignee_map: Dict[str, List[int]]) -> List[int]:
    if not assignee_value or not assignee_map:
        return []

    candidates: List[str] = []
    if isinstance(assignee_value, str):
        candidates.append(assignee_value)
        candidates.extend(filter(None, (part.strip() for part in ASSIGNEE_SPLIT_RE.split(assignee_value))))
    elif isinstance(assignee_value, list):
        for value in assignee_value:
            if isinstance(value, str):
                candidates.append(value)

    resolved: List[int] = []
    for candidate in candidates:
        normalized = _normalize_name(candidate)
        if not normalized:
            continue
        ids = assignee_map.get(normalized)
        if not ids:
            continue
        for member_id in ids:
            if member_id not in resolved:
                resolved.append(member_id)

    return resolved


def fetch_clickup_member_map(token: str, list_id: str) -> Dict[str, List[int]]:
    if not token or not list_id:
        return {}

    url = f"https://api.clickup.com/api/v2/list/{list_id}"
    headers = {"Authorization": token}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
    except RequestException as exc:
        logging.warning("Не удалось загрузить участников списка ClickUp: %s", exc)
        return {}

    data = response.json()
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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# Загрузка конфигурации
def load_config():
    """Загружает конфигурацию из файла"""
    config_path = PROJECT_ROOT / "config.json"
    with open(config_path, 'r') as f:
        return json.load(f)

# Загрузка API секретов
def load_api_secrets():
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

    if any(not secrets[key] for key in primary_keys):
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
    cutoff_time = datetime.now() - timedelta(hours=hours_back)
    voice_messages: List[Dict[str, Any]] = []
    max_update_id = last_update_id
    chat_id_int = int(chat_id)
    offset = last_update_id + 1 if last_update_id is not None else None

    while True:
        params: Dict[str, Any] = {'limit': limit}
        if offset is not None:
            params['offset'] = offset

        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
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
            if msg_time <= cutoff_time:
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
        response = requests.get(url, params={'file_id': file_id}, timeout=30)
        response.raise_for_status()
        data = response.json()
    except RequestException as exc:
        raise RuntimeError(f"Failed to get Telegram file metadata: {exc}") from exc

    if not data.get('ok'):
        raise Exception(f"Failed to get file path: {data}")

    file_path = data['result']['file_path']

    # Скачиваем файл
    download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    try:
        with requests.get(download_url, stream=True, timeout=60) as response:
            response.raise_for_status()

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


def transcribe_audio(audio_path, api_key):
    """
    Транскрибирует аудио файл через OpenAI Whisper API
    Возвращает текст транскрипции
    """
    client = get_openai_client(api_key)
    
    with open(audio_path, 'rb') as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ru"
        )
    
    return transcript.text

def extract_tasks_from_text(text, api_key):
    """
    Использует GPT для извлечения задач из транскрипции
    Возвращает список задач с полями: название, описание, дедлайн, приоритет, ответственный
    """
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
    
    response = client.responses.create(
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
        }
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

def save_processing_log(log_data, log_file):
    """
    Сохраняет лог обработки в файл
    """
    with open(log_file, 'w', encoding='utf-8') as f:
        f.write("# Отчет об обработке голосовых и аудио сообщений Telegram\n\n")
        f.write(f"**Дата обработки:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        if not log_data['voice_messages']:
            f.write("## Результат\n\nНовых голосовых/аудио сообщений не найдено.\n")
            return
        
        f.write(f"## Обработано сообщений: {len(log_data['voice_messages'])}\n\n")
        
        for idx, vm in enumerate(log_data['voice_messages'], 1):
            msg_type = "Голосовое" if vm.get('type') == 'voice' else "Аудио"
            forwarded = " (пересланное)" if vm.get('is_forwarded') else ""
            f.write(f"### {msg_type} сообщение #{idx}{forwarded}\n\n")
            f.write(f"- **От:** {vm['from_user']}\n")
            f.write(f"- **Дата:** {vm['date']}\n")
            f.write(f"- **Длительность:** {vm['duration']} сек\n")
            f.write(f"- **Тип:** {vm.get('type', 'voice')}\n\n")
            
            if 'transcription' in vm:
                f.write(f"**Транскрипция:**\n```\n{vm['transcription']}\n```\n\n")
            
            if 'error' in vm:
                f.write(f"**⚠️ Ошибка:** {vm['error']}\n\n")
            elif 'tasks' in vm and vm['tasks']:
                f.write(f"**Извлечено задач:** {len(vm['tasks'])}\n\n")
                if vm.get('clickup_created'):
                    f.write(f"- **Создано в ClickUp:** {vm['clickup_created']}\n")
                if vm.get('clickup_failed'):
                    f.write(f"- **Ошибок создания:** {vm['clickup_failed']}\n")
                if vm.get('clickup_created') or vm.get('clickup_failed'):
                    f.write("\n")
                for task_idx, task in enumerate(vm['tasks'], 1):
                    f.write(f"#### Задача {task_idx}\n")
                    f.write(f"- **Название:** {task['name']}\n")
                    f.write(f"- **Описание:** {task['description']}\n")
                    f.write(f"- **Дедлайн:** {task.get('due_date', 'Не указан')}\n")
                    f.write(f"- **Приоритет:** {task.get('priority', 3)}\n")
                    f.write(f"- **Ответственный:** {task.get('assignee', 'Не указан')}\n")
                    if 'clickup_task_id' in task:
                        f.write(f"- **ClickUp Task ID:** {task['clickup_task_id']}\n")
                    if task.get('clickup_error'):
                        f.write(f"- **Ошибка ClickUp:** {task['clickup_error']}\n")
                    f.write("\n")
            else:
                f.write("**Задач не найдено**\n\n")
            
            f.write("---\n\n")
        
        f.write(f"\n## Итого создано задач в ClickUp: {log_data['total_tasks_created']}\n")
        if log_data.get('total_tasks_failed'):
            f.write(f"## Итого ошибок создания задач: {log_data['total_tasks_failed']}\n")

def main():
    """
    Основная функция обработки
    """
    print("Запуск обработки голосовых сообщений из Telegram...")
    
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

    clickup_member_map = fetch_clickup_member_map(clickup_token, clickup_list_id)
    config_assignee_map = prepare_assignee_map(config.get('assignee_map'))
    assignee_map = dict(clickup_member_map)
    assignee_map.update(config_assignee_map)
    
    print(f"Проверка голосовых сообщений в чате {chat_id}...")
    
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

    print(f"Найдено голосовых сообщений: {len(voice_messages)}")
    
    log_data = {
        'voice_messages': [],
        'total_tasks_created': 0,
        'total_tasks_failed': 0,
        'clickup_list_id': clickup_list_id
    }
    
    # Обрабатываем каждое голосовое/аудио сообщение
    for vm in voice_messages:
        audio_type_label = "голосового" if vm['type'] == 'voice' else "аудио"
        forwarded_label = " (пересланное)" if vm.get('is_forwarded') else ""
        print(f"\nОбработка {audio_type_label}{forwarded_label} от {vm['from_user']}...")
        
        vm_log = {
            'from_user': vm['from_user'],
            'date': vm['date'],
            'duration': vm['duration'],
            'type': vm['type'],
            'is_forwarded': vm.get('is_forwarded', False)
        }
        
        audio_path = None
        try:
            # Определяем расширение файла по mime_type
            mime_type = vm.get('mime_type', 'audio/ogg')
            if 'ogg' in mime_type:
                suffix = '.ogg'
            elif 'mpeg' in mime_type or 'mp3' in mime_type:
                suffix = '.mp3'
            elif 'mp4' in mime_type or 'm4a' in mime_type:
                suffix = '.m4a'
            elif 'wav' in mime_type:
                suffix = '.wav'
            else:
                suffix = '.ogg'  # по умолчанию
            
            # Скачиваем аудио файл
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
                audio_path = tmp_file.name

            download_audio_file(bot_token, vm['file_id'], audio_path)
            print(f"  Аудио файл скачан: {audio_path}")
            
            # Транскрибируем
            transcription = transcribe_audio(audio_path, openai_api_key)
            print(f"  Транскрипция: {transcription[:100]}...")
            vm_log['transcription'] = transcription
            
            # Извлекаем задачи
            tasks = extract_tasks_from_text(transcription, openai_api_key)
            print(f"  Извлечено задач: {len(tasks)}")
            vm_log['tasks'] = tasks

            created_for_message = 0
            for task in tasks:
                assignee_ids = resolve_assignee_ids(task.get('assignee'), assignee_map)
                if assignee_ids:
                    task['assignee_ids'] = assignee_ids
                payload = build_clickup_payload(
                    task,
                    default_priority=default_priority,
                    assignee_ids=assignee_ids,
                )
                task_name = payload.get('name', 'Без названия')

                try:
                    response = create_clickup_task(clickup_token, clickup_list_id, payload)
                except Exception as create_err:
                    task['clickup_error'] = str(create_err)
                    log_data['total_tasks_failed'] += 1
                    vm_log['clickup_failed'] = vm_log.get('clickup_failed', 0) + 1
                    print(f"  Ошибка создания задачи '{task_name}': {create_err}")
                    continue

                task_id = response.get("id") or response.get("task", {}).get("id")
                if task_id:
                    task['clickup_task_id'] = task_id
                    created_for_message += 1
                    log_data['total_tasks_created'] += 1
                    print(f"  Создана задача в ClickUp: {task_id} — {task_name}")
                else:
                    task['clickup_error'] = "Task created without ID in response"
                    log_data['total_tasks_failed'] += 1
                    vm_log['clickup_failed'] = vm_log.get('clickup_failed', 0) + 1
                    print(f"  Задача создана без ID в ответе — {task_name}")

            if created_for_message:
                vm_log['clickup_created'] = created_for_message
            
        except Exception as e:
            print(f"  Ошибка при обработке: {e}")
            vm_log['error'] = str(e)
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass
        
        log_data['voice_messages'].append(vm_log)

    if max_update_id is not None and max_update_id != last_update_id:
        state['last_update_id'] = max_update_id
        save_state(state)

    # Сохраняем лог
    logs_dir = PROJECT_ROOT / "logs"
    os.makedirs(logs_dir, exist_ok=True)
    log_file = str(logs_dir / f"processing_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    save_processing_log(log_data, log_file)
    print(f"\nЛог сохранен: {log_file}")
    
    # Сохраняем данные задач для следующего шага
    tasks_file = str(PROJECT_ROOT / f"tasks_to_create_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(tasks_file, 'w', encoding='utf-8') as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)
    print(f"Данные задач сохранены: {tasks_file}")
    
    print(
        f"\nОбработка завершена. Всего задач создано: {log_data['total_tasks_created']}, "
        f"ошибок: {log_data['total_tasks_failed']}"
    )
    return tasks_file

if __name__ == "__main__":
    main()
