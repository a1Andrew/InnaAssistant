#!/usr/bin/env bash
# Діагностика: чи живий бот, що в нього налаштовано і що вже є в базі.
# Запуск:  cd /root/InnaAssistant && ./deploy/status.sh
# Нічого не змінює, тільки показує. Переглядач (less) не відкриває.

DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || PY=python3
export SYSTEMD_PAGER=cat SYSTEMD_PAGERSECURE=0

echo "═══ СЕРВІС ═══"
if command -v systemctl >/dev/null; then
    state=$(systemctl is-active inna-assistant 2>/dev/null)
    since=$(systemctl show inna-assistant -p ActiveEnterTimestamp --value 2>/dev/null)
    echo "стан: ${state:-невідомо}   з: ${since:-—}"
else
    echo "systemctl недоступний"
fi

echo
echo "═══ НАЛАШТУВАННЯ (.env) ═══"
if [ -f "$DIR/.env" ]; then
    for key in ANTHROPIC_API_KEY ASSISTANT_BOT_TOKEN ASSISTANT_OWNER_ID \
               ASSISTANT_OWNER_NAME ASSISTANT_USER_IDS OPENAI_API_KEY; do
        val=$(grep -E "^$key=" "$DIR/.env" | head -1 | cut -d= -f2-)
        case "$key" in
            # ключі й токени не друкуємо, лише факт наявності
            *KEY|*TOKEN) [ -n "$val" ] && echo "$key: ✔ заповнено" || echo "$key: ✘ ПОРОЖНЬО" ;;
            *) echo "$key: ${val:-✘ ПОРОЖНЬО}" ;;
        esac
    done
else
    echo "файла .env немає!"
fi

echo
echo "═══ БАЗА ═══"
"$PY" - "$DIR/inna_assistant.db" <<'PY'
import os, sqlite3, sys
path = sys.argv[1]
if not os.path.exists(path):
    print("бази ще немає — бот жодного разу нічого не записав")
    raise SystemExit
db = sqlite3.connect(path)
row = db.execute("SELECT chat_id FROM settings").fetchone()
print("чат для розкладу:", row[0] if row else "— (бот ще не отримав жодного повідомлення)")
topics = db.execute("SELECT chat_id, title FROM topics ORDER BY rowid").fetchall()
if topics:
    print(f"теми ({len(topics)}) у чаті {topics[0][0]}:")
    print("   " + ", ".join(t[1] for t in topics))
else:
    print("теми: — (/setup у групі ще не виконувався)")
for table, label in (("tasks", "задач"), ("content", "контенту"),
                     ("logs", "записів щоденника"), ("notes", "нотаток")):
    n = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    print(f"{label}: {n}")

last = db.execute(
    "SELECT created_at, title FROM tasks ORDER BY id DESC LIMIT 6").fetchall()
if last:
    print("\nостанні записані задачі:")
    for created, title in last:
        print(f"   {created}  {title[:60]}")
PY

echo
echo "═══ ОСТАННЯ АКТИВНІСТЬ (крім опитувань Telegram) ═══"
if command -v journalctl >/dev/null; then
    LOG=$(journalctl -u inna-assistant --no-pager --since "2 days ago" 2>/dev/null)

    echo -n "оброблено звернень (бот відповів): "
    echo "$LOG" | grep -c "steps=" 
    echo "останнє оброблене звернення:"
    echo "$LOG" | grep "steps=" | tail -1 | cut -c1-120
    echo

    echo "ВІДХИЛЕНІ (ID не в списку дозволених):"
    rejected=$(echo "$LOG" | grep -oE "Чужий користувач id=[0-9]+" | sort | uniq -c)
    if [ -n "$rejected" ]; then
        echo "$rejected"
        echo "↑ якщо тут є ID Інни — просто додай його в ASSISTANT_USER_IDS у .env"
    else
        echo "   немає — усі повідомлення приймалися"
    fi
    echo

    echo "останні помилки, якщо були:"
    errors=$(echo "$LOG" | grep -E "Помилка|Traceback|ERROR" | tail -5 | cut -c1-150)
    echo "${errors:-   немає}"
else
    echo "journalctl недоступний"
fi
