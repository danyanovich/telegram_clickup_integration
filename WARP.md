# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project overview

Automation that scans a Telegram group for new voice/audio messages, transcribes them with OpenAI Whisper, extracts structured tasks with GPT-4, and creates ClickUp tasks. State is tracked to avoid reprocessing, and run logs are written to disk.

## Commands (setup, run, logs)

- Install deps
  ```bash
  pip install -r requirements.txt
  ```

- Run the hourly processor once (manual run)
  ```bash
  python3 process_voice_messages.py
  ```

- Recreate ClickUp tasks from the latest saved JSON
  ```bash
  python3 create_clickup_tasks.py
  ```

- Inspect recent logs
  ```bash
  ls -lt logs | head -5
  # e.g.
  # cat logs/processing_log_2025-01-01T12-00-00Z.md
  ```

Notes:
- The README references these key files: `process_voice_messages.py`, `create_clickup_tasks.py`, `clickup_client.py`, `config.json`, `logs/`, and `state.json`. If any are missing locally, check your current branch or sync from the deployment source.

## Configuration and secrets

- App config (`config.json` at repo root)
  ```json
  {
    "clickup_list_id": "901515871754",
    "telegram_check_hours": 1,
    "default_priority": 3
  }
  ```

- Required environment variables (or load from your secret store):
  - TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  - OPENAI_API_KEY
  - CLICKUP_TOKEN

Do not commit secrets. Prefer environment variables over files.

## Testing and linting

- No test or lint configuration files were found (e.g., pytest, ruff/flake8). There are no repository-defined commands to run tests or lint. If a test suite is added later, document how to run a single test here.

## High-level architecture

- Ingestion (Telegram):
  - Polls the Telegram Bot API for new updates in a target group. Supported content types per README: voice notes, forwarded voice, audio files (mp3/ogg/m4a/wav), channel posts, forwarded audio.
  - Idempotency via `state.json` that stores the last processed `update_id`.

- Transcription (OpenAI Whisper):
  - Downloads audio and sends it to Whisper for ASR.

- Task extraction (GPT-4):
  - Prompts an LLM to extract structured tasks (title, description, due date if present, priority 1–4, assignee if present) from the transcript.

- Task creation (ClickUp):
  - Uses ClickUp REST API (with `CLICKUP_TOKEN`) to create tasks in a configured list (`clickup_list_id`).

- Orchestration scripts:
  - `process_voice_messages.py`: end-to-end job (poll → transcribe → extract → create tasks → update state → write logs).
  - `create_clickup_tasks.py`: utility to recreate ClickUp tasks from the last saved JSON payload (useful for retries).
  - `clickup_client.py`: shared ClickUp API helpers.

- Observability:
  - Writes detailed markdown logs to `logs/` including counts, transcripts, extracted tasks, created task IDs, and errors.

## Operational notes (from README)

- The job is intended to run hourly (e.g., via cron or a scheduler outside this repo). Manual invocation is supported via `python3 process_voice_messages.py`.
- Troubleshooting tips (summarized): ensure the bot is an admin in the group; verify ClickUp list ID and token; inspect `logs/`; remove `state.json` to start from the current window if needed; improve audio quality for better ASR.
