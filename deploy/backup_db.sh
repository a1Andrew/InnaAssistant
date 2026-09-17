#!/usr/bin/env bash
# Щоденна копія бази InnaAssistant. У крон:
#   0 4 * * * /root/InnaAssistant/deploy/backup_db.sh
#
# Копіюємо через sqlite3-модуль Python (він завжди є разом з ботом), а не через
# утиліту sqlite3 — її на сервері може не бути взагалі. Метод .backup коректно
# знімає копію навіть тоді, коли бот саме пише в базу.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$DIR/backups"
DB="$DIR/inna_assistant.db"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || PY=python3

if [ ! -f "$DB" ]; then
    echo "Бази ще немає: $DB"
    exit 0
fi

mkdir -p "$DEST"
OUT="$DEST/inna_assistant-$(date +%F).db"

"$PY" - "$DB" "$OUT" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
print(f"OK: {dst}")
PY

find "$DEST" -name 'inna_assistant-*.db' -mtime +30 -delete
