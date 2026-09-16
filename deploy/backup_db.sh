#!/usr/bin/env bash
# Щоденна копія бази InnaAssistant. У крон:
#   0 4 * * * /root/InnaAssistant/deploy/backup_db.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$DIR/backups"
mkdir -p "$DEST"
sqlite3 "$DIR/inna_assistant.db" ".backup '$DEST/inna_assistant-$(date +%F).db'"
find "$DEST" -name 'inna_assistant-*.db' -mtime +30 -delete
