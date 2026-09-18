#!/usr/bin/env bash
# Запуск на macOS и Linux. То же, что и .bat: первый раз готовит
# окружение рядом с программой, дальше открывается сразу.
set -e
cd "$(dirname "$0")"

PY=""
for c in python3.12 python3.11 python3 python; do
  command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
done
[ -z "$PY" ] && { echo "Не найден Python 3. Установите его и запустите снова."; exit 1; }

if [ ! -x ".venv/bin/python" ]; then
  echo "Первый запуск: готовлю окружение…"
  "$PY" -m venv .venv
fi

if ! cmp -s requirements.txt .venv/requirements.stamp 2>/dev/null; then
  echo "Устанавливаю библиотеки…"
  .venv/bin/python -m pip install --upgrade pip --quiet
  .venv/bin/python -m pip install -r requirements.txt --quiet
  cp requirements.txt .venv/requirements.stamp
fi

exec .venv/bin/python launcher.py
