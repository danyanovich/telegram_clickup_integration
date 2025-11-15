#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[run.sh] Не найден интерпретатор '$PYTHON_BIN'. Укажите PYTHON_BIN или установите python3." >&2
  exit 1
fi

LOCK_FILE="$SCRIPT_DIR/requirements.lock"
if [[ ! -f "$LOCK_FILE" ]]; then
  echo "[run.sh] Не найден $LOCK_FILE. Сначала запустите pip-compile." >&2
  exit 1
fi

echo "[run.sh] Установка зависимостей из requirements.lock..."
"$PYTHON_BIN" -m pip install -r "$LOCK_FILE"

echo "[run.sh] Запуск process_voice_messages.py..."
"$PYTHON_BIN" "$SCRIPT_DIR/process_voice_messages.py"
