"""
InnaAssistant — особистий AI-асистент в одному місці.

Для кого: власниця кількох напрямків (робота з командою, Instagram-контент,
спорт і харчування, навчання, особисті проєкти). Замість блокнота — одна база,
де AI сам розкладає все по пазлах: пріоритети на день, зріз за тиждень і місяць.

Що вміє:
  * напрямки (проєкти) і цілі по кожному;
  * задачі з пріоритетом, дедлайном, виконавцем (контроль співробітників);
  * контент-план Instagram + метрики публікацій і аналіз, що зайшло;
  * трекінг тіла: тренування, харчування, вага, самопочуття;
  * навчання: маркетинг, англійська — скільки і що робила;
  * нотатки, нагадування;
  * САМ пише вранці план на день, увечері підбиває підсумок,
    у неділю — аналіз тижня, 1-го числа — зріз місяця.

Запуск:  python3 inna_assistant.py
Ключі — з .env поруч із цим файлом.

БЕЗПЕКА: у цього бота НЕМАЄ bash, читання/запису довільних файлів і доступу
до сервера. Тільки своя база inna_assistant.db. Він фізично не може нашкодити VPS.
"""

import os
import json
import shutil
import asyncio
import tempfile
import sqlite3
import logging
import threading
from datetime import datetime, timedelta, time as dtime, date
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from anthropic import AsyncAnthropic
from telegram import Update, BotCommand
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ─────────────────────────────────────────────────────────────
#  НАЛАШТУВАННЯ (.env)
# ─────────────────────────────────────────────────────────────

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Окремий токен для цього бота. Якщо його немає — падаємо назад на спільний,
# але тримати два боти на одному токені не можна: буде конфлікт polling.
TELEGRAM_BOT_TOKEN = os.getenv("ASSISTANT_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

MODEL = os.getenv("ASSISTANT_MODEL", "claude-opus-5")


def _ids(raw: str) -> set[int]:
    return {int(x) for x in (raw or "").replace(",", " ").split() if x.strip().isdigit()}


# Хто має доступ. За замовчуванням — власниця; плюс усі з ALLOWED_USER_IDS
# (той самий whitelist, що й у agent_bot.py), щоб адмін міг керувати системою.
OWNER_ID = int(os.getenv("ASSISTANT_OWNER_ID", "595153958"))
OWNER_NAME = os.getenv("ASSISTANT_OWNER_NAME", "Інна")
ALLOWED_IDS = (
    _ids(os.getenv("ASSISTANT_USER_IDS", str(OWNER_ID)))
    | _ids(os.getenv("ALLOWED_USER_IDS", ""))
    | {OWNER_ID}
)

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("ASSISTANT_DB", os.path.join(WORK_DIR, "inna_assistant.db"))

TZ = ZoneInfo(os.getenv("ASSISTANT_TZ", "Europe/Kyiv"))
MORNING_AT = os.getenv("MORNING_AT", "08:30")     # план на день
EVENING_AT = os.getenv("EVENING_AT", "21:00")     # підсумок дня
WEEKLY_AT = os.getenv("WEEKLY_AT", "19:00")       # неділя — аналіз тижня
MONTHLY_AT = os.getenv("MONTHLY_AT", "10:00")     # 1-ше число — зріз місяця

MAX_HISTORY = int(os.getenv("MAX_HISTORY", "30"))

# Голосові: Claude не чує аудіо, тому спершу розшифровка через OpenAI.
# Без ключа бот просто ввічливо скаже, що голосові не ввімкнені.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
STT_MODEL = os.getenv("STT_MODEL", "gpt-transcribe")
STT_URL = "https://api.openai.com/v1/audio/transcriptions"
VOICE_MAX_SEC = int(os.getenv("VOICE_MAX_SEC", "600"))      # довші не беремо
VOICE_MAX_BYTES = 20 * 1024 * 1024                          # ліміт API — 25 МБ
MAX_STEPS = int(os.getenv("ASSISTANT_MAX_STEPS", "14"))
MAX_TOKENS = int(os.getenv("ASSISTANT_MAX_TOKENS", "16000"))

TELEGRAM_LIMIT = 4000
TOOL_OUTPUT_LIMIT = 6000

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
log = logging.getLogger("assistant")

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# Історія діалогу в пам'яті процесу. Дані живуть у базі, не в історії.
conversations: dict[int, list] = {}


# ─────────────────────────────────────────────────────────────
#  БАЗА ДАНИХ
# ─────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    kind        TEXT    DEFAULT 'work',      -- work | content | health | study | personal
    description TEXT    DEFAULT '',
    status      TEXT    DEFAULT 'active',    -- active | paused | done
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL,
    project_id  INTEGER,
    title       TEXT    NOT NULL,
    details     TEXT    DEFAULT '',
    priority    INTEGER DEFAULT 2,           -- 1 високий, 2 звичайний, 3 низький
    status      TEXT    DEFAULT 'todo',      -- todo | doing | waiting | done | cancelled
    due_date    TEXT,                        -- дедлайн, YYYY-MM-DD
    plan_date   TEXT,                        -- на який день заплановано
    assignee    TEXT    DEFAULT '',          -- хто виконує (співробітник)
    repeat      TEXT    DEFAULT '',          -- daily | weekly | monthly | ''
    est_min     INTEGER,
    created_at  TEXT,
    done_at     TEXT
);

CREATE TABLE IF NOT EXISTS goals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL,
    project_id  INTEGER,
    title       TEXT    NOT NULL,
    horizon     TEXT    DEFAULT 'month',     -- week | month | quarter | year
    target_date TEXT,
    metric      TEXT    DEFAULT '',          -- як міряємо результат
    progress    TEXT    DEFAULT '',
    status      TEXT    DEFAULT 'active',    -- active | done | dropped
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS content (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     INTEGER NOT NULL,
    project_id   INTEGER,
    title        TEXT    NOT NULL,
    format       TEXT    DEFAULT 'reels',    -- reels | post | stories | carousel | video
    rubric       TEXT    DEFAULT '',         -- рубрика / напрямок контенту
    idea         TEXT    DEFAULT '',         -- суть, хук, сценарій
    status       TEXT    DEFAULT 'idea',     -- idea | draft | filmed | scheduled | published
    publish_date TEXT,
    views        INTEGER, likes INTEGER, comments INTEGER, saves INTEGER, reach INTEGER,
    notes        TEXT    DEFAULT '',
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL,
    category   TEXT NOT NULL,                -- training | nutrition | body | study | mood | other
    item       TEXT NOT NULL,                -- «ноги», «англійська», «вага»
    value      REAL,
    unit       TEXT DEFAULT '',              -- кг, хв, ккал, км
    note       TEXT DEFAULT '',
    date       TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL,
    project_id INTEGER,
    title      TEXT NOT NULL,
    text       TEXT DEFAULT '',
    tags       TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL,
    text       TEXT NOT NULL,
    at         TEXT NOT NULL,                -- YYYY-MM-DD HH:MM (локальний час)
    sent       INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    owner_id INTEGER PRIMARY KEY,
    chat_id  INTEGER,
    updated  TEXT
);

-- Теми (topics) у груповому чаті: кожен напрямок — своя гілка.
CREATE TABLE IF NOT EXISTS topics (
    chat_id    INTEGER NOT NULL,
    thread_id  INTEGER NOT NULL,
    purpose    TEXT    NOT NULL,          -- plan | work | content | health | study | ...
    title      TEXT    NOT NULL,
    project    TEXT    DEFAULT '',        -- напрямок за замовчуванням для цієї теми
    created_at TEXT,
    PRIMARY KEY (chat_id, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_owner   ON tasks(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_dates   ON tasks(owner_id, plan_date, due_date);
CREATE INDEX IF NOT EXISTS idx_logs_owner    ON logs(owner_id, category, date);
CREATE INDEX IF NOT EXISTS idx_content_owner ON content(owner_id, status, publish_date);
"""

_db_lock = threading.RLock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
# Вбудований lower() у SQLite знає лише латиницю, тож «Інна» != «інна».
# Реєструємо свою функцію, щоб пошук і збіги працювали з кирилицею.
db.create_function("ufold", 1, lambda v: v.casefold() if isinstance(v, str) else v)
with _db_lock:
    db.executescript(SCHEMA)
    db.commit()


def q(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _db_lock:
        return db.execute(sql, params).fetchall()


def run(sql: str, params: tuple = ()) -> int:
    """INSERT/UPDATE/DELETE. Повертає lastrowid для INSERT, інакше rowcount."""
    with _db_lock:
        cur = db.execute(sql, params)
        db.commit()
        return cur.lastrowid if cur.lastrowid else cur.rowcount


# ─────────────────────────────────────────────────────────────
#  ДАТИ
# ─────────────────────────────────────────────────────────────

WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]


def now_local() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now_local().date()


def iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def parse_date(value: str | None) -> str | None:
    """'today' / 'завтра' / '2026-09-20' / '20.09' → 'YYYY-MM-DD'. Сміття → None."""
    if not value:
        return None
    v = str(value).strip().lower()
    shortcuts = {
        "today": 0, "сьогодні": 0, "сегодня": 0,
        "tomorrow": 1, "завтра": 1,
        "послезавтра": 2, "післязавтра": 2,
        "yesterday": -1, "вчора": -1, "вчера": -1,
    }
    if v in shortcuts:
        return iso(today() + timedelta(days=shortcuts[v]))
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d.%m", "%d/%m"):
        try:
            d = datetime.strptime(v, fmt).date()
            if fmt in ("%d.%m", "%d/%m"):
                d = d.replace(year=today().year)
            return iso(d)
        except ValueError:
            continue
    return None


def parse_dt(value: str | None) -> str | None:
    """'2026-09-20 14:30' або '20.09 14:30' → 'YYYY-MM-DD HH:MM'."""
    if not value:
        return None
    v = str(value).strip()
    parts = v.split()
    if len(parts) == 2:
        d = parse_date(parts[0])
        t = parts[1]
        if d:
            try:
                datetime.strptime(t, "%H:%M")
                return f"{d} {t}"
            except ValueError:
                return None
    return None


def week_bounds(ref: date | None = None) -> tuple[str, str]:
    ref = ref or today()
    start = ref - timedelta(days=ref.weekday())
    return iso(start), iso(start + timedelta(days=6))


def month_bounds(ref: date | None = None) -> tuple[str, str]:
    ref = ref or today()
    start = ref.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return iso(start), iso(nxt - timedelta(days=1))


# ─────────────────────────────────────────────────────────────
#  ФОРМАТУВАННЯ
# ─────────────────────────────────────────────────────────────

PRIO = {1: "A", 2: "B", 3: "C"}
MARK = {"todo": "⬜", "doing": "🔸", "waiting": "⏳", "done": "✅", "cancelled": "✖️"}


def project_name(project_id) -> str:
    if not project_id:
        return ""
    rows = q("SELECT name FROM projects WHERE id = ?", (project_id,))
    return rows[0]["name"] if rows else ""


def fmt_task(r: sqlite3.Row) -> str:
    bits = [f"#{r['id']} {MARK.get(r['status'], '•')} [{PRIO.get(r['priority'], 'B')}] {r['title']}"]
    extra = []
    pname = project_name(r["project_id"])
    if pname:
        extra.append(pname)
    if r["plan_date"]:
        extra.append(f"на {r['plan_date']}")
    if r["due_date"]:
        extra.append(f"дедлайн {r['due_date']}")
    if r["assignee"]:
        extra.append(f"@{r['assignee']}")
    if r["repeat"]:
        extra.append(f"повтор: {r['repeat']}")
    if r["est_min"]:
        extra.append(f"~{r['est_min']} хв")
    if extra:
        bits.append("(" + ", ".join(extra) + ")")
    if r["details"]:
        bits.append(f"— {r['details'][:200]}")
    return " ".join(bits)


def fmt_content(r: sqlite3.Row) -> str:
    head = f"#{r['id']} [{r['status']}] {r['format']}: {r['title']}"
    extra = []
    if r["publish_date"]:
        extra.append(r["publish_date"])
    if r["rubric"]:
        extra.append(r["rubric"])
    metrics = [
        f"{label} {r[key]}"
        for key, label in (("views", "перегляди"), ("reach", "охоплення"),
                           ("likes", "лайки"), ("comments", "коменти"), ("saves", "збереження"))
        if r[key] is not None
    ]
    if metrics:
        extra.append("; ".join(metrics))
    if extra:
        head += " (" + " | ".join(extra) + ")"
    if r["idea"]:
        head += f"\n    ідея: {r['idea'][:300]}"
    return head


def fmt_log(r: sqlite3.Row) -> str:
    val = ""
    if r["value"] is not None:
        val = f" — {r['value']:g} {r['unit']}".rstrip()
    note = f" ({r['note']})" if r["note"] else ""
    return f"#{r['id']} {r['date']} {r['category']}: {r['item']}{val}{note}"


def fmt_goal(r: sqlite3.Row) -> str:
    pname = project_name(r["project_id"])
    bits = [f"#{r['id']} [{r['status']}] {r['title']}", f"({r['horizon']}"]
    if r["target_date"]:
        bits[-1] += f" до {r['target_date']}"
    if pname:
        bits[-1] += f", {pname}"
    bits[-1] += ")"
    if r["metric"]:
        bits.append(f"метрика: {r['metric']}")
    if r["progress"]:
        bits.append(f"прогрес: {r['progress']}")
    return " ".join(bits)


# ─────────────────────────────────────────────────────────────
#  ІНСТРУМЕНТИ: опис для моделі
# ─────────────────────────────────────────────────────────────

DATE_HINT = "Дата у форматі YYYY-MM-DD (можна 'today'/'tomorrow')."

TOOLS = [
    {
        "name": "now",
        "description": "Поточні дата, час, день тижня, межі поточного тижня і місяця.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_project",
        "description": (
            "Створити напрямок (проєкт): «Наша робота», «Право і порядок», Instagram, "
            "спорт, англійська тощо. Якщо напрямок уже є — не дублюй, використай наявний."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["work", "content", "health", "study", "personal"],
                },
                "description": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_projects",
        "description": "Список напрямків із кількістю відкритих задач.",
        "input_schema": {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["active", "paused", "done", "all"]}},
        },
    },
    {
        "name": "update_project",
        "description": "Змінити напрямок: назву, опис, статус (active/paused/done).",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "status": {"type": "string", "enum": ["active", "paused", "done"]},
            },
            "required": ["id"],
        },
    },
    {
        "name": "add_task",
        "description": (
            "Додати задачу. project — назва напрямку (створиться автоматично, якщо новий). "
            "assignee — ім'я співробітника, якщо задача не на неї. "
            "priority: 1 важливо і терміново, 2 звичайна, 3 колись. "
            "repeat: daily/weekly/monthly для регулярних справ."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "project": {"type": "string"},
                "details": {"type": "string"},
                "priority": {"type": "integer", "enum": [1, 2, 3]},
                "due_date": {"type": "string", "description": DATE_HINT},
                "plan_date": {"type": "string", "description": "День, на який ставимо в план. " + DATE_HINT},
                "assignee": {"type": "string"},
                "repeat": {"type": "string", "enum": ["daily", "weekly", "monthly"]},
                "est_min": {"type": "integer", "description": "Скільки хвилин займе"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_tasks",
        "description": (
            "Задачі за фільтром. scope: today (на сьогодні + прострочені), tomorrow, week, "
            "overdue, open (всі відкриті), done (закриті за N днів), all."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["today", "tomorrow", "week", "overdue", "open", "done", "all"],
                },
                "project": {"type": "string"},
                "assignee": {"type": "string"},
                "days": {"type": "integer", "description": "Для scope=done: за скільки останніх днів"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "update_task",
        "description": (
            "Змінити задачу за id: статус (todo/doing/waiting/done/cancelled), пріоритет, "
            "дати, виконавця, текст. Закриття задачі з repeat автоматично створює наступну."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "status": {"type": "string", "enum": ["todo", "doing", "waiting", "done", "cancelled"]},
                "priority": {"type": "integer", "enum": [1, 2, 3]},
                "title": {"type": "string"},
                "details": {"type": "string"},
                "due_date": {"type": "string", "description": DATE_HINT},
                "plan_date": {"type": "string", "description": DATE_HINT},
                "assignee": {"type": "string"},
                "repeat": {"type": "string", "enum": ["daily", "weekly", "monthly", "none"]},
                "est_min": {"type": "integer"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "add_goal",
        "description": "Ціль на тиждень/місяць/квартал/рік із метрикою, за якою її міряємо.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "horizon": {"type": "string", "enum": ["week", "month", "quarter", "year"]},
                "project": {"type": "string"},
                "target_date": {"type": "string", "description": DATE_HINT},
                "metric": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_goals",
        "description": "Цілі за статусом і горизонтом.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["active", "done", "dropped", "all"]},
                "horizon": {"type": "string", "enum": ["week", "month", "quarter", "year"]},
            },
        },
    },
    {
        "name": "update_goal",
        "description": "Оновити ціль: прогрес, статус, дату, назву, метрику.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "status": {"type": "string", "enum": ["active", "done", "dropped"]},
                "progress": {"type": "string"},
                "title": {"type": "string"},
                "metric": {"type": "string"},
                "target_date": {"type": "string", "description": DATE_HINT},
            },
            "required": ["id"],
        },
    },
    {
        "name": "add_content",
        "description": (
            "Додати одиницю контент-плану (ідея, рілс, пост, сторіс). "
            "status: idea → draft → filmed → scheduled → published."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "format": {"type": "string", "enum": ["reels", "post", "stories", "carousel", "video"]},
                "rubric": {"type": "string", "description": "Рубрика/напрямок контенту"},
                "idea": {"type": "string", "description": "Хук, суть, сценарій"},
                "publish_date": {"type": "string", "description": DATE_HINT},
                "status": {"type": "string", "enum": ["idea", "draft", "filmed", "scheduled", "published"]},
                "project": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_content",
        "description": "Контент-план. scope: plan (ще не опубліковано), week, published, all.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["plan", "week", "published", "all"]},
                "days": {"type": "integer", "description": "Для scope=published: за скільки днів"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "update_content",
        "description": (
            "Оновити публікацію: статус, дату, метрики (перегляди, охоплення, лайки, "
            "коменти, збереження), висновки. Метрики потрібні для аналізу, що заходить."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "status": {"type": "string", "enum": ["idea", "draft", "filmed", "scheduled", "published"]},
                "publish_date": {"type": "string", "description": DATE_HINT},
                "title": {"type": "string"},
                "idea": {"type": "string"},
                "rubric": {"type": "string"},
                "views": {"type": "integer"},
                "reach": {"type": "integer"},
                "likes": {"type": "integer"},
                "comments": {"type": "integer"},
                "saves": {"type": "integer"},
                "notes": {"type": "string"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "add_log",
        "description": (
            "Записати факт у щоденник тіла/навчання: тренування, харчування, вага, "
            "самопочуття, урок англійської чи маркетингу. "
            "Приклади: category=training item='ноги' value=60 unit='хв'; "
            "category=body item='вага' value=58.4 unit='кг'; "
            "category=study item='англійська' value=45 unit='хв'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["training", "nutrition", "body", "study", "mood", "other"],
                },
                "item": {"type": "string"},
                "value": {"type": "number"},
                "unit": {"type": "string"},
                "note": {"type": "string"},
                "date": {"type": "string", "description": DATE_HINT},
            },
            "required": ["category", "item"],
        },
    },
    {
        "name": "list_logs",
        "description": "Записи щоденника за категорією і періодом (за замовчуванням 14 днів).",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["training", "nutrition", "body", "study", "mood", "other", "all"],
                },
                "days": {"type": "integer"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "add_note",
        "description": "Зберегти нотатку/ідею/домовленість, щоб не тримати в голові.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "text": {"type": "string"},
                "project": {"type": "string"},
                "tags": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "search",
        "description": "Пошук по задачах, нотатках і контент-плану за словом.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "add_reminder",
        "description": (
            "Разове нагадування у конкретний час. at — 'YYYY-MM-DD HH:MM' у локальному часі. "
            "Бот сам напише в чат у цей момент."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "at": {"type": "string"}},
            "required": ["text", "at"],
        },
    },
    {
        "name": "list_reminders",
        "description": "Найближчі невідправлені нагадування.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "review",
        "description": (
            "Зведені цифри за період для аналізу: day, week, month (або власні дати). "
            "Повертає зроблені/відкриті/прострочені задачі по напрямках і виконавцях, "
            "публікації з метриками, тренування/навчання, цілі. "
            "Використовуй перед будь-яким підсумком дня, тижня чи місяця."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["day", "week", "month"]},
                "date_from": {"type": "string", "description": DATE_HINT},
                "date_to": {"type": "string", "description": DATE_HINT},
            },
        },
    },
]


# ─────────────────────────────────────────────────────────────
#  ІНСТРУМЕНТИ: реалізація
# ─────────────────────────────────────────────────────────────


def _stamp() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M")


def _find_or_create_project(uid: int, name: str, kind: str = "work") -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    rows = q(
        "SELECT id FROM projects WHERE owner_id = ? AND ufold(name) = ufold(?)",
        (uid, name),
    )
    if rows:
        return rows[0]["id"]
    return run(
        "INSERT INTO projects (owner_id, name, kind, created_at) VALUES (?, ?, ?, ?)",
        (uid, name, kind, _stamp()),
    )


def t_now(uid: int, a: dict) -> str:
    n = now_local()
    ws, we = week_bounds()
    ms, me = month_bounds()
    return (
        f"Зараз: {n:%Y-%m-%d %H:%M} ({WEEKDAYS[n.weekday()]}), часовий пояс {TZ.key}\n"
        f"Тиждень: {ws} — {we}\nМісяць: {ms} — {me}"
    )


def t_add_project(uid: int, a: dict) -> str:
    name = (a.get("name") or "").strip()
    if not name:
        return "Потрібна назва напрямку."
    existing = q(
        "SELECT id FROM projects WHERE owner_id = ? AND ufold(name) = ufold(?)", (uid, name)
    )
    if existing:
        return f"Напрямок «{name}» уже є (#{existing[0]['id']})."
    pid = run(
        "INSERT INTO projects (owner_id, name, kind, description, created_at) VALUES (?, ?, ?, ?, ?)",
        (uid, name, a.get("kind", "work"), a.get("description", ""), _stamp()),
    )
    return f"Напрямок «{name}» створено (#{pid})."


def t_list_projects(uid: int, a: dict) -> str:
    status = a.get("status", "active")
    sql = "SELECT * FROM projects WHERE owner_id = ?"
    params: list = [uid]
    if status != "all":
        sql += " AND status = ?"
        params.append(status)
    rows = q(sql + " ORDER BY id", tuple(params))
    if not rows:
        return "Напрямків поки немає."
    out = []
    for r in rows:
        open_n = q(
            "SELECT count(*) c FROM tasks WHERE owner_id = ? AND project_id = ? "
            "AND status IN ('todo','doing','waiting')",
            (uid, r["id"]),
        )[0]["c"]
        line = f"#{r['id']} {r['name']} [{r['kind']}/{r['status']}] — відкритих задач: {open_n}"
        if r["description"]:
            line += f"\n    {r['description'][:200]}"
        out.append(line)
    return "\n".join(out)


def t_update_project(uid: int, a: dict) -> str:
    return _update_row(uid, "projects", a, ("name", "description", "status"))


def t_add_task(uid: int, a: dict) -> str:
    title = (a.get("title") or "").strip()
    if not title:
        return "Потрібна назва задачі."
    pid = _find_or_create_project(uid, a.get("project", ""))
    tid = run(
        "INSERT INTO tasks (owner_id, project_id, title, details, priority, due_date, "
        "plan_date, assignee, repeat, est_min, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            uid, pid, title, a.get("details", ""), int(a.get("priority") or 2),
            parse_date(a.get("due_date")), parse_date(a.get("plan_date")),
            (a.get("assignee") or "").strip(), a.get("repeat", "") or "",
            a.get("est_min"), _stamp(),
        ),
    )
    return f"Задача #{tid} додана: {title}"


def t_list_tasks(uid: int, a: dict) -> str:
    scope = a.get("scope", "open")
    limit = int(a.get("limit") or 60)
    t = iso(today())
    sql = "SELECT * FROM tasks WHERE owner_id = ?"
    params: list = [uid]

    if scope == "done":
        days = int(a.get("days") or 7)
        since = iso(today() - timedelta(days=days))
        sql += " AND status = 'done' AND date(done_at) >= ?"
        params.append(since)
    elif scope == "all":
        pass
    else:
        sql += " AND status IN ('todo','doing','waiting')"
        if scope == "today":
            sql += " AND (plan_date <= ? OR due_date <= ? OR (plan_date IS NULL AND due_date IS NULL AND priority = 1))"
            params += [t, t]
        elif scope == "tomorrow":
            tm = iso(today() + timedelta(days=1))
            sql += " AND (plan_date = ? OR due_date = ?)"
            params += [tm, tm]
        elif scope == "week":
            ws, we = week_bounds()
            sql += " AND ((plan_date BETWEEN ? AND ?) OR (due_date BETWEEN ? AND ?))"
            params += [ws, we, ws, we]
        elif scope == "overdue":
            sql += " AND ((due_date IS NOT NULL AND due_date < ?) OR (plan_date IS NOT NULL AND plan_date < ?))"
            params += [t, t]

    if a.get("project"):
        pid = _find_or_create_project(uid, a["project"])
        sql += " AND project_id = ?"
        params.append(pid)
    if a.get("assignee"):
        sql += " AND ufold(assignee) = ufold(?)"
        params.append(a["assignee"].strip())

    sql += " ORDER BY priority, COALESCE(due_date, plan_date, '9999'), id LIMIT ?"
    params.append(limit)
    rows = q(sql, tuple(params))
    if not rows:
        return "Задач за цим фільтром немає."
    return f"Знайдено {len(rows)}:\n" + "\n".join(fmt_task(r) for r in rows)


def _next_date(d: str, repeat: str) -> str:
    base = datetime.strptime(d, "%Y-%m-%d").date()
    if repeat == "daily":
        return iso(base + timedelta(days=1))
    if repeat == "weekly":
        return iso(base + timedelta(days=7))
    nxt = (base.replace(day=1) + timedelta(days=32)).replace(day=1)
    try:
        return iso(nxt.replace(day=base.day))
    except ValueError:  # 31-ше в короткому місяці
        return iso((nxt + timedelta(days=32)).replace(day=1) - timedelta(days=1))


def t_update_task(uid: int, a: dict) -> str:
    rows = q("SELECT * FROM tasks WHERE owner_id = ? AND id = ?", (uid, a.get("id")))
    if not rows:
        return f"Задачі #{a.get('id')} немає."
    task = rows[0]

    sets, params = [], []
    for field in ("title", "details", "assignee", "est_min"):
        if a.get(field) is not None:
            sets.append(f"{field} = ?")
            params.append(a[field])
    if a.get("priority"):
        sets.append("priority = ?")
        params.append(int(a["priority"]))
    for field in ("due_date", "plan_date"):
        if a.get(field) is not None:
            sets.append(f"{field} = ?")
            params.append(parse_date(a[field]))
    if a.get("repeat") is not None:
        sets.append("repeat = ?")
        params.append("" if a["repeat"] == "none" else a["repeat"])

    created_next = ""
    if a.get("status"):
        sets.append("status = ?")
        params.append(a["status"])
        if a["status"] == "done":
            sets.append("done_at = ?")
            params.append(_stamp())
            repeat = a.get("repeat") if a.get("repeat") not in (None, "none") else task["repeat"]
            if repeat:
                base = task["plan_date"] or task["due_date"] or iso(today())
                nd = _next_date(base, repeat)
                nid = run(
                    "INSERT INTO tasks (owner_id, project_id, title, details, priority, "
                    "due_date, plan_date, assignee, repeat, est_min, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        uid, task["project_id"], task["title"], task["details"],
                        task["priority"], nd if task["due_date"] else None, nd,
                        task["assignee"], repeat, task["est_min"], _stamp(),
                    ),
                )
                created_next = f" Наступна — #{nid} на {nd}."

    if not sets:
        return "Нічого змінювати."
    params += [uid, a["id"]]
    run(f"UPDATE tasks SET {', '.join(sets)} WHERE owner_id = ? AND id = ?", tuple(params))
    return f"Задачу #{a['id']} оновлено.{created_next}"


def t_add_goal(uid: int, a: dict) -> str:
    title = (a.get("title") or "").strip()
    if not title:
        return "Потрібна назва цілі."
    pid = _find_or_create_project(uid, a.get("project", ""))
    gid = run(
        "INSERT INTO goals (owner_id, project_id, title, horizon, target_date, metric, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (uid, pid, title, a.get("horizon", "month"), parse_date(a.get("target_date")),
         a.get("metric", ""), _stamp()),
    )
    return f"Ціль #{gid} додана: {title}"


def t_list_goals(uid: int, a: dict) -> str:
    sql = "SELECT * FROM goals WHERE owner_id = ?"
    params: list = [uid]
    status = a.get("status", "active")
    if status != "all":
        sql += " AND status = ?"
        params.append(status)
    if a.get("horizon"):
        sql += " AND horizon = ?"
        params.append(a["horizon"])
    rows = q(sql + " ORDER BY COALESCE(target_date,'9999'), id", tuple(params))
    return "\n".join(fmt_goal(r) for r in rows) if rows else "Цілей за фільтром немає."


def t_update_goal(uid: int, a: dict) -> str:
    return _update_row(uid, "goals", a, ("title", "status", "progress", "metric"), dates=("target_date",))


def t_add_content(uid: int, a: dict) -> str:
    title = (a.get("title") or "").strip()
    if not title:
        return "Потрібна назва/тема публікації."
    pid = _find_or_create_project(uid, a.get("project", ""), kind="content")
    cid = run(
        "INSERT INTO content (owner_id, project_id, title, format, rubric, idea, status, "
        "publish_date, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (uid, pid, title, a.get("format", "reels"), a.get("rubric", ""), a.get("idea", ""),
         a.get("status", "idea"), parse_date(a.get("publish_date")), _stamp()),
    )
    return f"Контент #{cid} додано: {title}"


def t_list_content(uid: int, a: dict) -> str:
    scope = a.get("scope", "plan")
    limit = int(a.get("limit") or 40)
    sql = "SELECT * FROM content WHERE owner_id = ?"
    params: list = [uid]
    if scope == "plan":
        sql += " AND status != 'published'"
    elif scope == "published":
        days = int(a.get("days") or 30)
        sql += " AND status = 'published' AND publish_date >= ?"
        params.append(iso(today() - timedelta(days=days)))
    elif scope == "week":
        ws, we = week_bounds()
        sql += " AND publish_date BETWEEN ? AND ?"
        params += [ws, we]
    sql += " ORDER BY COALESCE(publish_date,'9999'), id LIMIT ?"
    params.append(limit)
    rows = q(sql, tuple(params))
    return "\n".join(fmt_content(r) for r in rows) if rows else "Контенту за фільтром немає."


def t_update_content(uid: int, a: dict) -> str:
    return _update_row(
        uid, "content", a,
        ("status", "title", "idea", "rubric", "notes", "views", "reach", "likes", "comments", "saves"),
        dates=("publish_date",),
    )


def t_add_log(uid: int, a: dict) -> str:
    cat = a.get("category")
    item = (a.get("item") or "").strip()
    if not cat or not item:
        return "Потрібні category та item."
    d = parse_date(a.get("date")) or iso(today())
    lid = run(
        "INSERT INTO logs (owner_id, category, item, value, unit, note, date, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (uid, cat, item, a.get("value"), a.get("unit", ""), a.get("note", ""), d, _stamp()),
    )
    return f"Записано #{lid}: {d} {cat} / {item}"


def t_list_logs(uid: int, a: dict) -> str:
    days = int(a.get("days") or 14)
    limit = int(a.get("limit") or 60)
    since = iso(today() - timedelta(days=days))
    sql = "SELECT * FROM logs WHERE owner_id = ? AND date >= ?"
    params: list = [uid, since]
    cat = a.get("category", "all")
    if cat and cat != "all":
        sql += " AND category = ?"
        params.append(cat)
    rows = q(sql + " ORDER BY date DESC, id DESC LIMIT ?", tuple(params + [limit]))
    return "\n".join(fmt_log(r) for r in rows) if rows else f"Записів за {days} днів немає."


def t_add_note(uid: int, a: dict) -> str:
    title = (a.get("title") or "").strip()
    if not title:
        return "Потрібна назва нотатки."
    pid = _find_or_create_project(uid, a.get("project", ""))
    nid = run(
        "INSERT INTO notes (owner_id, project_id, title, text, tags, created_at) VALUES (?,?,?,?,?,?)",
        (uid, pid, title, a.get("text", ""), a.get("tags", ""), _stamp()),
    )
    return f"Нотатка #{nid} збережена: {title}"


def t_search(uid: int, a: dict) -> str:
    query = (a.get("query") or "").strip()
    if not query:
        return "Потрібен текст запиту."
    like = f"%{query}%".casefold()
    limit = int(a.get("limit") or 15)
    out = []
    tasks = q(
        "SELECT * FROM tasks WHERE owner_id = ? AND (ufold(title) LIKE ? OR ufold(details) LIKE ? OR ufold(assignee) LIKE ?) "
        "ORDER BY id DESC LIMIT ?",
        (uid, like, like, like, limit),
    )
    if tasks:
        out.append("ЗАДАЧІ:\n" + "\n".join(fmt_task(r) for r in tasks))
    notes = q(
        "SELECT * FROM notes WHERE owner_id = ? AND (ufold(title) LIKE ? OR ufold(text) LIKE ? OR ufold(tags) LIKE ?) "
        "ORDER BY id DESC LIMIT ?",
        (uid, like, like, like, limit),
    )
    if notes:
        out.append("НОТАТКИ:\n" + "\n".join(
            f"#{r['id']} {r['created_at']} {r['title']}: {r['text'][:300]}" for r in notes
        ))
    cont = q(
        "SELECT * FROM content WHERE owner_id = ? AND (ufold(title) LIKE ? OR ufold(idea) LIKE ? OR ufold(rubric) LIKE ?) "
        "ORDER BY id DESC LIMIT ?",
        (uid, like, like, like, limit),
    )
    if cont:
        out.append("КОНТЕНТ:\n" + "\n".join(fmt_content(r) for r in cont))
    return "\n\n".join(out) if out else f"За запитом «{query}» нічого не знайшла."


def t_add_reminder(uid: int, a: dict) -> str:
    when = parse_dt(a.get("at"))
    text = (a.get("text") or "").strip()
    if not when or not text:
        return "Потрібні text і at у форматі 'YYYY-MM-DD HH:MM'."
    rid = run(
        "INSERT INTO reminders (owner_id, text, at, created_at) VALUES (?,?,?,?)",
        (uid, text, when, _stamp()),
    )
    return f"Нагадування #{rid} на {when}: {text}"


def t_list_reminders(uid: int, a: dict) -> str:
    rows = q(
        "SELECT * FROM reminders WHERE owner_id = ? AND sent = 0 ORDER BY at LIMIT 30", (uid,)
    )
    if not rows:
        return "Активних нагадувань немає."
    return "\n".join(f"#{r['id']} {r['at']} — {r['text']}" for r in rows)


def t_review(uid: int, a: dict) -> str:
    period = a.get("period", "week")
    if a.get("date_from") or a.get("date_to"):
        d_from = parse_date(a.get("date_from")) or iso(today())
        d_to = parse_date(a.get("date_to")) or iso(today())
    elif period == "day":
        d_from = d_to = iso(today())
    elif period == "month":
        d_from, d_to = month_bounds()
    else:
        d_from, d_to = week_bounds()

    out = [f"ЗРІЗ {d_from} — {d_to}"]

    done = q(
        "SELECT * FROM tasks WHERE owner_id = ? AND status = 'done' AND date(done_at) BETWEEN ? AND ?",
        (uid, d_from, d_to),
    )
    open_rows = q(
        "SELECT * FROM tasks WHERE owner_id = ? AND status IN ('todo','doing','waiting')", (uid,)
    )
    overdue = [r for r in open_rows if (r["due_date"] or "9999") < iso(today())]
    out.append(
        f"\nЗАДАЧІ: закрито {len(done)}, відкрито зараз {len(open_rows)}, прострочено {len(overdue)}"
    )

    by_project: dict[str, list[int]] = {}
    for r in done:
        by_project.setdefault(project_name(r["project_id"]) or "без напрямку", [0, 0])[0] += 1
    for r in open_rows:
        by_project.setdefault(project_name(r["project_id"]) or "без напрямку", [0, 0])[1] += 1
    for name, (d_cnt, o_cnt) in sorted(by_project.items()):
        out.append(f"  • {name}: закрито {d_cnt}, відкрито {o_cnt}")

    by_person: dict[str, list[int]] = {}
    for r in done:
        if r["assignee"]:
            by_person.setdefault(r["assignee"], [0, 0])[0] += 1
    for r in open_rows:
        if r["assignee"]:
            by_person.setdefault(r["assignee"], [0, 0])[1] += 1
    if by_person:
        out.append("\nПО ЛЮДЯХ:")
        for name, (d_cnt, o_cnt) in sorted(by_person.items()):
            out.append(f"  • {name}: закрито {d_cnt}, у роботі {o_cnt}")
    if overdue:
        out.append("\nПРОСТРОЧЕНІ:\n" + "\n".join(fmt_task(r) for r in overdue[:15]))

    pub = q(
        "SELECT * FROM content WHERE owner_id = ? AND status = 'published' "
        "AND publish_date BETWEEN ? AND ? ORDER BY publish_date",
        (uid, d_from, d_to),
    )
    planned = q(
        "SELECT count(*) c FROM content WHERE owner_id = ? AND status != 'published'", (uid,)
    )[0]["c"]
    out.append(f"\nКОНТЕНТ: опубліковано {len(pub)}, у плані {planned}")
    if pub:
        views = [r["views"] for r in pub if r["views"] is not None]
        if views:
            out.append(
                f"  перегляди: сума {sum(views)}, середнє {sum(views) // len(views)}, "
                f"максимум {max(views)}"
            )
        out.extend("  " + fmt_content(r) for r in pub)

    logs = q(
        "SELECT category, item, count(*) n, sum(value) s, max(unit) u FROM logs "
        "WHERE owner_id = ? AND date BETWEEN ? AND ? GROUP BY category, item ORDER BY category, n DESC",
        (uid, d_from, d_to),
    )
    if logs:
        out.append("\nТІЛО І НАВЧАННЯ:")
        for r in logs:
            total = f", разом {r['s']:g} {r['u']}".rstrip() if r["s"] is not None else ""
            out.append(f"  • {r['category']}/{r['item']}: {r['n']} записів{total}")
        body = q(
            "SELECT * FROM logs WHERE owner_id = ? AND category = 'body' AND date <= ? "
            "ORDER BY date DESC LIMIT 5",
            (uid, d_to),
        )
        if body:
            out.append("  динаміка тіла: " + "; ".join(
                f"{r['date']} {r['item']} {r['value']:g}{r['unit']}"
                for r in body if r["value"] is not None
            ))

    goals = q("SELECT * FROM goals WHERE owner_id = ? AND status = 'active' ORDER BY horizon", (uid,))
    if goals:
        out.append("\nАКТИВНІ ЦІЛІ:\n" + "\n".join(fmt_goal(r) for r in goals))
    return "\n".join(out)


def _update_row(uid: int, table: str, a: dict, fields: tuple, dates: tuple = ()) -> str:
    """Спільний UPDATE для простих таблиць. Назви колонок — лише з білого списку."""
    row_id = a.get("id")
    if not row_id:
        return "Потрібен id."
    if not q(f"SELECT id FROM {table} WHERE owner_id = ? AND id = ?", (uid, row_id)):
        return f"Запису #{row_id} немає."
    sets, params = [], []
    for f in fields:
        if a.get(f) is not None:
            sets.append(f"{f} = ?")
            params.append(a[f])
    for f in dates:
        if a.get(f) is not None:
            sets.append(f"{f} = ?")
            params.append(parse_date(a[f]))
    if not sets:
        return "Нічого змінювати."
    params += [uid, row_id]
    run(f"UPDATE {table} SET {', '.join(sets)} WHERE owner_id = ? AND id = ?", tuple(params))
    return f"Запис #{row_id} оновлено."


TOOL_IMPL = {
    "now": t_now,
    "add_project": t_add_project,
    "list_projects": t_list_projects,
    "update_project": t_update_project,
    "add_task": t_add_task,
    "list_tasks": t_list_tasks,
    "update_task": t_update_task,
    "add_goal": t_add_goal,
    "list_goals": t_list_goals,
    "update_goal": t_update_goal,
    "add_content": t_add_content,
    "list_content": t_list_content,
    "update_content": t_update_content,
    "add_log": t_add_log,
    "list_logs": t_list_logs,
    "add_note": t_add_note,
    "search": t_search,
    "add_reminder": t_add_reminder,
    "list_reminders": t_list_reminders,
    "review": t_review,
}


def execute_tool(name: str, args: dict, uid: int) -> str:
    fn = TOOL_IMPL.get(name)
    if not fn:
        return f"Невідомий інструмент: {name}"
    try:
        return str(fn(uid, args or {}))[:TOOL_OUTPUT_LIMIT]
    except Exception as e:                      # інструмент не має ронити бота
        log.exception("Інструмент %s впав", name)
        return f"Помилка інструмента {name}: {e}"


# ─────────────────────────────────────────────────────────────
#  СИСТЕМНИЙ ПРОМПТ
# ─────────────────────────────────────────────────────────────

SYSTEM = f"""Ти — персональна асистентка-організатор. Твоя робота: тримати всі напрямки
життя й роботи господині в одній системі та щодня складати з них зрозумілий пазл.

ГОСПОДИНЯ СИСТЕМИ — її звати {OWNER_NAME}. Це вона керує ботом і всіма напрямками,
з нею ти й розмовляєш. Звертайся до неї на ім'я, відмінюючи його правильно за
мовою розмови. Інші імена, що з'являються в задачах, — це її команда, виконавці.
Задача, де виконавця не вказано, — її власна.

НАПРЯМКИ, які ти ведеш (усі рівноцінні):
- робота з командою: задачі співробітникам, контроль виконання, дедлайни;
- окремі проєкти (кожен — свій напрямок у базі);
- Instagram: контент-стратегія, контент-план, аналіз публікацій за метриками;
- тіло: тренування, харчування, вага, самопочуття, тенденції;
- навчання: маркетинг, англійська — регулярність і прогрес;
- побутові та особисті справи.

ГОЛОВНЕ ПРАВИЛО: усе, що вона сказала, має опинитися в базі через інструменти.
Не «запам'ятовуй» у голові — історія чату коротка і зникне. Почула задачу, ідею,
цифру, домовленість, вагу, тренування — одразу записала відповідним інструментом.
Одне повідомлення може містити кілька записів — зроби всі.

ЯК ПРАЦЮВАТИ
- Спершу подивись у базу (list_tasks, review, list_content), потім відповідай.
  Ніколи не вигадуй цифри й задачі, яких немає в базі.
- База пам'ятає все від самого початку, нічого не застаріває. Питають про
  давнє — не кажи, що не знаєш: постав review з date_from і date_to за той
  період, list_logs чи list_tasks з великим days, або search за словом.
- Не допитуйся. Якщо бракує дрібниці (проєкт, дата) — постав розумне припущення,
  запиши і скажи, що саме припустила. Питай лише тоді, коли без відповіді запис
  буде беззмістовним.
- Пріоритети розставляй сама: 1 — дедлайн горить або блокує інших людей;
  2 — рухає ціль цього тижня; 3 — може почекати.
- План на день: 3-5 головних справ, не більше. Спочатку прострочене й дедлайни,
  потім те, що веде до цілей. Великі справи ріж на кроки по 30-60 хв.
  Враховуй, що день скінченний: якщо задач більше, ніж часу — скажи прямо,
  що переносиш і чому.
- У підсумках тижня/місяця спирайся на review: що закрито, що зависло, де
  просідає напрямок, що дали публікації, чи є регулярність у спорті й навчанні.
  Давай 2-3 конкретні висновки і 1-2 рекомендації на наступний період, без води.
- Задачі, які вона делегує комусь із команди, завжди з assignee — саме так ти
  потім контролюєш виконання. Якщо щось висить на людині кілька днів — підсвічуй
  це окремо. Без assignee задача вважається її власною.
- Контент аналізуй цифрами: що зайшло за переглядами й збереженнями, які рубрики
  й формати працюють, що варто повторити.

ТОН: тепло, по-людськи, коротко. Як жива помічниця, а не робот.
Списки — короткими рядками, без зайвого форматування (Telegram, без markdown-таблиць).
Відповідай тією мовою, якою пише господиня (російською — російською).
"""


def build_system() -> list[dict]:
    n = now_local()
    text = SYSTEM + f"\n\nСЬОГОДНІ: {n:%Y-%m-%d}, {WEEKDAYS[n.weekday()]}. Часовий пояс: {TZ.key}."
    # Стабільний префікс (tools + system) кешується — дешевше в багатокрокових циклах.
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# ─────────────────────────────────────────────────────────────
#  ЦИКЛ АГЕНТА
# ─────────────────────────────────────────────────────────────


def is_allowed(uid: int) -> bool:
    return uid in ALLOWED_IDS


async def send_chunks(bot, chat_id: int, text: str, thread_id: int | None = None) -> None:
    for i in range(0, len(text), TELEGRAM_LIMIT):
        await bot.send_message(
            chat_id=chat_id, text=text[i:i + TELEGRAM_LIMIT], message_thread_id=thread_id
        )


async def run_agent(uid: int, chat_id: int, bot, history: list,
                    thread_id: int | None = None) -> None:
    """Крутить діалог з інструментами, поки модель не завершить відповідь."""
    system = build_system()
    for step in range(MAX_STEPS):
        resp = await claude.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=TOOLS,
            messages=history,
        )

        if resp.stop_reason == "refusal":
            await send_chunks(bot, chat_id,
                              "⚠️ Модель відмовилась відповідати на цей запит.", thread_id)
            return

        for b in resp.content:
            if b.type == "text" and b.text.strip():
                await send_chunks(bot, chat_id, b.text.strip(), thread_id)

        history.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            log.info("uid=%s steps=%s in=%s out=%s", uid, step + 1,
                     resp.usage.input_tokens, resp.usage.output_tokens)
            return

        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            log.info("TOOL %s %s", b.name, json.dumps(b.input, ensure_ascii=False)[:300])
            out = execute_tool(b.name, b.input or {}, uid)
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        history.append({"role": "user", "content": results})

    await send_chunks(
        bot, chat_id,
        f"⚠️ Зупинилась після {MAX_STEPS} кроків. Давай розіб'ємо це на менші частини.",
        thread_id,
    )


# ─────────────────────────────────────────────────────────────
#  ГОЛОСОВІ ПОВІДОМЛЕННЯ
# ─────────────────────────────────────────────────────────────


async def _to_mp3(data: bytes, suffix: str) -> tuple[bytes, str]:
    """Telegram шле ogg/opus, а API приймає mp3/m4a/wav/webm. Конвертуємо ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log.warning("ffmpeg не встановлений — шлю аудіо як є (%s)", suffix)
        return data, f"voice{suffix}"
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, f"in{suffix}")
        dst = os.path.join(tmp, "out.mp3")
        with open(src, "wb") as f:
            f.write(data)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-loglevel", "error", "-i", src,
            "-ac", "1", "-ar", "16000", "-b:a", "48k", dst,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(dst):
            raise RuntimeError(f"ffmpeg не впорався: {err.decode()[:200]}")
        with open(dst, "rb") as f:
            return f.read(), "voice.mp3"


async def transcribe(data: bytes, suffix: str) -> str:
    """Аудіо → текст. Порожній результат означає, що нічого не розібрали."""
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "Голосові поки не ввімкнені: у .env немає OPENAI_API_KEY."
        )
    audio, filename = await _to_mp3(data, suffix)
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            STT_URL,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (filename, audio, "audio/mpeg")},
            data={"model": STT_MODEL},
        )
    if r.status_code != 200:
        raise RuntimeError(f"розшифровка не вдалася ({r.status_code}): {r.text[:200]}")
    return (r.json().get("text") or "").strip()


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Голосове, аудіо або кружечок → текст → звичайний розбір задачі."""
    uid = update.effective_user.id
    chat = update.effective_chat
    if not is_allowed(uid):
        log.warning("Чужий користувач id=%s надіслав аудіо", uid)
        return

    msg = update.message
    media = msg.voice or msg.audio or msg.video_note
    if media is None:
        return
    thread_id = msg.message_thread_id if msg.is_topic_message else None

    if (media.duration or 0) > VOICE_MAX_SEC:
        await msg.reply_text(
            f"Це задовге аудіо ({media.duration // 60} хв). "
            f"Розшифровую до {VOICE_MAX_SEC // 60} хв — запиши коротшими шматками."
        )
        return
    if (media.file_size or 0) > VOICE_MAX_BYTES:
        await msg.reply_text("Файл завеликий, максимум 20 МБ.")
        return

    await chat.send_action(ChatAction.TYPING, message_thread_id=thread_id)
    try:
        tg_file = await ctx.bot.get_file(media.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        suffix = os.path.splitext(tg_file.file_path or "")[1] or ".ogg"
        text = await transcribe(data, suffix)
    except Exception as e:
        log.exception("Не вдалося розшифрувати голосове")
        await msg.reply_text(f"🎙 Не вийшло розібрати голосове: {e}")
        return

    if not text:
        await msg.reply_text("🎙 Нічого не розібрала — спробуй ще раз, будь ласка.")
        return

    # Показуємо розшифровку, щоб було видно, що саме бот почув.
    await send_chunks(ctx.bot, chat.id, f"🎙 Почула: {text}", thread_id)
    await process_text(update, ctx, text)


# ─────────────────────────────────────────────────────────────
#  ОБРОБНИК ПОВІДОМЛЕНЬ
# ─────────────────────────────────────────────────────────────


def remember_chat(chat_id: int) -> None:
    """Куди писати плани й підсумки за розкладом. Група має пріоритет над особистим чатом."""
    current = q("SELECT chat_id FROM settings WHERE owner_id = ?", (OWNER_ID,))
    if current and current[0]["chat_id"] and current[0]["chat_id"] < 0 and chat_id > 0:
        return                                   # вже прив'язані до групи — не перебиваємо
    run(
        "INSERT INTO settings (owner_id, chat_id, updated) VALUES (?,?,?) "
        "ON CONFLICT(owner_id) DO UPDATE SET chat_id = excluded.chat_id, updated = excluded.updated",
        (OWNER_ID, chat_id, _stamp()),
    )


def topic_of(chat_id: int, thread_id: int | None) -> sqlite3.Row | None:
    if thread_id is None:
        return None
    rows = q("SELECT * FROM topics WHERE chat_id = ? AND thread_id = ?", (chat_id, thread_id))
    return rows[0] if rows else None


def thread_for(chat_id: int, purpose: str) -> int | None:
    """Гілка під конкретну роль (план, підсумки). Немає — пишемо в загальний чат."""
    rows = q(
        "SELECT thread_id FROM topics WHERE chat_id = ? AND purpose = ? LIMIT 1",
        (chat_id, purpose),
    )
    return rows[0]["thread_id"] if rows else None


def history_for(chat_id: int, thread_id: int | None) -> list:
    return conversations.setdefault(f"{chat_id}:{thread_id or 0}", [])


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    chat = update.effective_chat
    if not is_allowed(uid):
        # У групі чужих просто ігноруємо, щоб не спамити; в особистому — пояснюємо.
        log.warning("Чужий користувач id=%s у чаті %s", uid, chat.id)
        if chat.type == "private":
            await update.message.reply_text(f"⛔️ Немає доступу. Твій ID: {uid}")
        return
    await process_text(update, ctx, update.message.text)


async def process_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Спільний шлях для написаного тексту й розшифрованого голосового."""
    chat = update.effective_chat
    chat_id = chat.id
    thread_id = update.message.message_thread_id if update.message.is_topic_message else None
    remember_chat(chat_id)

    topic = topic_of(chat_id, thread_id)
    if topic:
        # Тема гілки = контекст: задачі з неї одразу лягають у правильний напрямок.
        hint = f"[тема гілки: {topic['title']}"
        if topic["project"]:
            hint += f"; напрямок за замовчуванням: {topic['project']}"
        text = hint + "]\n" + text

    history = history_for(chat_id, thread_id)
    history.append({"role": "user", "content": text})
    if len(history) > MAX_HISTORY:
        del history[: len(history) - MAX_HISTORY]

    await chat.send_action(ChatAction.TYPING, message_thread_id=thread_id)
    try:
        await run_agent(OWNER_ID, chat_id, ctx.bot, history, thread_id)
    except Exception as e:
        conversations.pop(f"{chat_id}:{thread_id or 0}", None)   # обірваний tool_use ламає історію
        log.exception("Помилка асистента")
        await update.message.reply_text(
            f"❌ Помилка: {e}\n(історію діалогу очищено, дані в базі цілі)"
        )


async def ask_assistant(bot, chat_id: int, prompt: str, thread_id: int | None = None) -> None:
    """Разовий запит без історії діалогу — для планів, підсумків, нагадувань."""
    try:
        await run_agent(OWNER_ID, chat_id, bot, [{"role": "user", "content": prompt}], thread_id)
    except Exception:
        log.exception("Помилка планового запуску")




# ─────────────────────────────────────────────────────────────
#  ГРУПА З ТЕМАМИ (кожен напрямок — окрема гілка)
# ─────────────────────────────────────────────────────────────

# purpose, назва теми, напрямок за замовчуванням, вітальне слово
TOPIC_PLAN = [
    ("plan", "🌅 План дня", "",
     "Сюди щоранку приходить план на день, а ввечері — підсумок. Пиши тут, що зробила. Можна голосовим — я розшифрую і занесу."),
    ("work", "📋 Наша робота", "Наша робота",
     "Задачі команді й контроль виконання. Свою задачу пиши як є: «договір до "
     "п'ятниці». Задачу комусь із команди — з іменем на початку: «Ім'я — "
     "підготувати позов до вівторка»."),
    ("projects", "⚖️ Проєкти", "Проєкти",
     "Окремі проєкти. Для великого проєкту можна зробити свою тему: /newtopic Назва."),
    ("content", "📸 Контент", "Instagram",
     "Контент-план, ідеї, сценарії і цифри публікацій: «рілс про докази — 12к переглядів»."),
    ("health", "💪 Тіло", "Тіло",
     "Тренування, харчування, вага, самопочуття: «ноги 50 хвилин, вага 58.4»."),
    ("study", "📚 Навчання", "Навчання",
     "Маркетинг, англійська: «англійська 45 хвилин, present perfect»."),
    ("home", "🏡 Побут", "Побут",
     "Побутові справи, щоб не тримати їх у голові."),
    ("review", "📊 Підсумки", "",
     "Аналіз тижня (неділя) і зріз місяця (1-ше число) приходять сюди."),
    ("ideas", "💡 Ідеї", "",
     "Все, що спало на думку. Розберемо потім."),
]


async def cmd_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Розгортає структуру тем у груповому чаті."""
    uid = update.effective_user.id
    chat = update.effective_chat
    if not is_allowed(uid):
        return
    if chat.type == "private":
        await update.message.reply_text(
            "Ця команда — для групи. Створи групу в Telegram, увімкни в ній Topics (Теми), "
            "додай мене адміном з правом керувати темами і напиши /setup там."
        )
        return
    if not getattr(chat, "is_forum", False):
        await update.message.reply_text(
            "У цій групі не ввімкнені Теми. Налаштування групи → Теми (Topics) → увімкнути, "
            "потім знову /setup."
        )
        return

    remember_chat(chat.id)
    created, existed, failed = [], [], []
    for purpose, title, project, intro in TOPIC_PLAN:
        if q("SELECT 1 FROM topics WHERE chat_id = ? AND purpose = ?", (chat.id, purpose)):
            existed.append(title)
            continue
        try:
            topic = await ctx.bot.create_forum_topic(chat_id=chat.id, name=title)
        except Exception as e:
            log.warning("Не змогла створити тему %s: %s", title, e)
            failed.append(f"{title} ({e})")
            continue
        run(
            "INSERT OR REPLACE INTO topics (chat_id, thread_id, purpose, title, project, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (chat.id, topic.message_thread_id, purpose, title, project, _stamp()),
        )
        if project:
            _find_or_create_project(OWNER_ID, project)
        created.append(title)
        try:
            await ctx.bot.send_message(
                chat_id=chat.id, message_thread_id=topic.message_thread_id, text=intro
            )
        except Exception:
            log.warning("Не змогла написати в тему %s", title)

    report = []
    if created:
        report.append("Створила теми:\n" + "\n".join(f"• {t}" for t in created))
    if existed:
        report.append("Уже були: " + ", ".join(existed))
    if failed:
        report.append(
            "Не вийшло:\n" + "\n".join(f"• {t}" for t in failed)
            + "\n\nПеревір, що я адмін групи з правом «Керувати темами»."
        )
    report.append("Можна додати свою тему: /newtopic Назва напрямку")
    await update.message.reply_text("\n\n".join(report))


async def cmd_newtopic(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/newtopic Назва — нова тема під окремий напрямок."""
    uid = update.effective_user.id
    chat = update.effective_chat
    if not is_allowed(uid):
        return
    name = " ".join(ctx.args).strip() if ctx.args else ""
    if not name:
        await update.message.reply_text("Напиши так: /newtopic Право і порядок")
        return
    if not getattr(chat, "is_forum", False):
        await update.message.reply_text("Теми працюють лише в групі з увімкненими Topics.")
        return
    try:
        topic = await ctx.bot.create_forum_topic(chat_id=chat.id, name=name)
    except Exception as e:
        await update.message.reply_text(f"Не змогла створити тему: {e}")
        return
    run(
        "INSERT OR REPLACE INTO topics (chat_id, thread_id, purpose, title, project, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (chat.id, topic.message_thread_id, "project", name, name, _stamp()),
    )
    _find_or_create_project(OWNER_ID, name)
    await ctx.bot.send_message(
        chat_id=chat.id, message_thread_id=topic.message_thread_id,
        text=f"Тема «{name}» готова. Все, що напишеш тут, іде в напрямок «{name}».",
    )


async def cmd_bind(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/bind Назва напрямку — прив'язати поточну тему до напрямку вручну."""
    uid = update.effective_user.id
    chat = update.effective_chat
    if not is_allowed(uid):
        return
    thread_id = update.message.message_thread_id if update.message.is_topic_message else None
    if thread_id is None:
        await update.message.reply_text("Цю команду треба писати всередині теми.")
        return
    name = " ".join(ctx.args).strip() if ctx.args else ""
    if not name:
        await update.message.reply_text("Напиши так: /bind Instagram")
        return
    run(
        "INSERT INTO topics (chat_id, thread_id, purpose, title, project, created_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(chat_id, thread_id) "
        "DO UPDATE SET project = excluded.project, title = excluded.title",
        (chat.id, thread_id, "project", name, name, _stamp()),
    )
    _find_or_create_project(OWNER_ID, name)
    await update.message.reply_text(f"Готово. Ця тема тепер веде напрямок «{name}».")


async def cmd_topics(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_allowed(uid):
        return
    rows = q("SELECT * FROM topics WHERE chat_id = ? ORDER BY rowid", (update.effective_chat.id,))
    if not rows:
        await update.message.reply_text("Тем ще немає. Напиши /setup у групі з увімкненими Topics.")
        return
    await update.message.reply_text("\n".join(
        f"• {r['title']} [{r['purpose']}]" + (f" → {r['project']}" if r["project"] else "")
        for r in rows
    ))


# ─────────────────────────────────────────────────────────────
#  КОМАНДИ
# ─────────────────────────────────────────────────────────────

HELP = (
    "Я твоя система: задачі, напрямки, контент-план, тіло, навчання — все в одному місці.\n\n"
    "Просто пиши або надиктовуй голосове — я розшифрую:\n"
    "• «договір для клієнта до п'ятниці, терміново»\n"
    "• «зроби контент-план на тиждень по темі суду»\n"
    "• «рілс про докази набрав 12к переглядів, 300 збережень»\n"
    "• «сьогодні ноги 50 хвилин, вага 58.4»\n"
    "• «що в мене на сьогодні?»\n\n"
    "Команди:\n"
    "/today — план на день\n"
    "/evening — підсумок дня\n"
    "/week — аналіз тижня\n"
    "/month — зріз місяця\n"
    "/tasks — відкриті задачі\n"
    "/content — контент-план\n"
    "/goals — цілі\n"
    "/projects — напрямки\n"
    "/export — вивантажити всю базу файлом\n"
    "/reset — почати діалог з чистого аркуша (дані залишаються)\n"
    "/info — стан системи\n"
    "/id — мій Telegram ID\n\n"
    "У групі з темами:\n"
    "/setup — розгорнути теми по напрямках\n"
    "/newtopic Назва — додати свою тему\n"
    "/bind Назва — прив'язати цю тему до напрямку\n"
    "/topics — список тем"
)

PROMPT_TODAY = (
    "Склади мені план на сьогодні. Спочатку подивись review за day, потім задачі "
    "scope=today і overdue, контент-план на тиждень і активні цілі. "
    "Дай 3-5 головних справ у порядку пріоритету, коротко поясни логіку, "
    "згадай дедлайни й те, що висить на людях. У кінці — одне питання дня, якщо потрібне."
)
PROMPT_EVENING = (
    "Підбий підсумок дня: що закрито (review за day), що лишилось відкритим, "
    "чи були тренування/навчання. Запропонуй, що перенести на завтра, і запитай, "
    "чи є щось, що я забула записати."
)
PROMPT_WEEK = (
    "Зроби аналіз тижня. Візьми review period=week і list_content scope=published за 7 днів. "
    "Покажи: що зроблено по кожному напрямку, де просідає, як спрацював контент за метриками, "
    "чи була регулярність у спорті й навчанні, що з людьми і їхніми задачами. "
    "Заверши 2-3 висновками і фокусом на наступний тиждень."
)
PROMPT_MONTH = (
    "Зроби зріз місяця: review period=month, опубліковані матеріали з метриками, "
    "динаміка тіла, навчання, прогрес по цілях. Порівняй напрямки між собою, "
    "скажи, куди реально йшов час, і запропонуй 3 пріоритети на наступний місяць."
)


def _thread(update: Update) -> int | None:
    msg = update.message
    return msg.message_thread_id if msg and msg.is_topic_message else None


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text(f"⛔️ Немає доступу. Твій ID: {uid}")
        return
    remember_chat(update.effective_chat.id)
    await update.message.reply_text("Привіт! 👋\n\n" + HELP)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if is_allowed(update.effective_user.id):
        await update.message.reply_text(HELP)


def _prompt_command(prompt: str):
    """Команда, яка просто ставить асистентці готове завдання."""
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        if not is_allowed(uid):
            if update.effective_chat.type == "private":
                await update.message.reply_text(f"⛔️ Немає доступу. Твій ID: {uid}")
            return
        chat_id = update.effective_chat.id
        thread_id = _thread(update)
        remember_chat(chat_id)
        await update.effective_chat.send_action(ChatAction.TYPING, message_thread_id=thread_id)
        conversations.pop(f"{chat_id}:{thread_id or 0}", None)
        await ask_assistant(ctx.bot, chat_id, prompt, thread_id)
    return handler


def _list_command(fn, args: dict):
    """Команда, яка читає базу напряму, без моделі (швидко й безкоштовно)."""
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        if not is_allowed(uid):
            return
        await send_chunks(ctx.bot, update.effective_chat.id, fn(OWNER_ID, args), _thread(update))
    return handler


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    conversations.pop(f"{update.effective_chat.id}:{_thread(update) or 0}", None)
    await update.message.reply_text("🧹 Діалог почато заново. Усі записи в базі на місці.")


async def cmd_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text(f"⛔️ Немає доступу. Твій ID: {uid}")
        return
    counts = {
        "напрямків": q("SELECT count(*) c FROM projects WHERE owner_id = ?", (OWNER_ID,))[0]["c"],
        "задач відкритих": q(
            "SELECT count(*) c FROM tasks WHERE owner_id = ? AND status IN ('todo','doing','waiting')",
            (OWNER_ID,))[0]["c"],
        "задач закритих": q(
            "SELECT count(*) c FROM tasks WHERE owner_id = ? AND status = 'done'", (OWNER_ID,))[0]["c"],
        "цілей": q(
            "SELECT count(*) c FROM goals WHERE owner_id = ? AND status='active'", (OWNER_ID,))[0]["c"],
        "контенту": q("SELECT count(*) c FROM content WHERE owner_id = ?", (OWNER_ID,))[0]["c"],
        "записів щоденника": q("SELECT count(*) c FROM logs WHERE owner_id = ?", (OWNER_ID,))[0]["c"],
        "нотаток": q("SELECT count(*) c FROM notes WHERE owner_id = ?", (OWNER_ID,))[0]["c"],
    }
    dest = q("SELECT chat_id FROM settings WHERE owner_id = ?", (OWNER_ID,))
    body = "\n".join(f"{k}: {v}" for k, v in counts.items())
    await update.message.reply_text(
        f"Модель: {MODEL}\nЧасовий пояс: {TZ.key}\n"
        f"План на день: {MORNING_AT}, підсумок: {EVENING_AT}\n"
        f"Тиждень: неділя {WEEKLY_AT}, місяць: 1-ше число {MONTHLY_AT}\n"
        f"Розклад пише в чат: {dest[0]['chat_id'] if dest else 'ще не визначено'}\n\n"
        f"{body}\n\nТвій Telegram ID: {uid}"
    )


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    dump = {
        table: [dict(r) for r in q(f"SELECT * FROM {table} WHERE owner_id = ?", (OWNER_ID,))]
        for table in ("projects", "tasks", "goals", "content", "logs", "notes", "reminders")
    }
    path = os.path.join(WORK_DIR, f"export_{today():%Y%m%d}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2)
    try:
        with open(path, "rb") as f:
            await ctx.bot.send_document(
                chat_id=update.effective_chat.id, document=f,
                filename=os.path.basename(path), message_thread_id=_thread(update),
                caption="Уся твоя база одним файлом.",
            )
    finally:
        os.remove(path)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Твій Telegram ID: {update.effective_user.id}")


# ─────────────────────────────────────────────────────────────
#  РОЗКЛАД: план дня, підсумки, нагадування
# ─────────────────────────────────────────────────────────────


def default_chat() -> int | None:
    rows = q("SELECT chat_id FROM settings WHERE owner_id = ?", (OWNER_ID,))
    return rows[0]["chat_id"] if rows and rows[0]["chat_id"] else None


def _scheduled(prompt: str, purpose: str,
               only_weekday: int | None = None, only_monthday: int | None = None):
    async def job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        n = now_local()
        if only_weekday is not None and n.weekday() != only_weekday:
            return
        if only_monthday is not None and n.day != only_monthday:
            return
        chat_id = default_chat()
        if not chat_id:
            log.info("Розклад: ще не знаю, куди писати — чекаю на перше повідомлення")
            return
        log.info("Плановий запуск: %s → чат %s", purpose, chat_id)
        await ask_assistant(ctx.bot, chat_id, prompt, thread_for(chat_id, purpose))
    return job


async def job_reminders(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    stamp = now_local().strftime("%Y-%m-%d %H:%M")
    rows = q("SELECT * FROM reminders WHERE sent = 0 AND at <= ?", (stamp,))
    chat_id = default_chat()
    for r in rows:
        if chat_id:
            await send_chunks(ctx.bot, chat_id, f"⏰ Нагадування: {r['text']}",
                              thread_for(chat_id, "plan"))
        run("UPDATE reminders SET sent = 1 WHERE id = ?", (r["id"],))


def _time(value: str, fallback: str) -> dtime:
    try:
        h, m = value.split(":")
        return dtime(hour=int(h), minute=int(m), tzinfo=TZ)
    except Exception:
        log.warning("Некоректний час «%s», беру %s", value, fallback)
        h, m = fallback.split(":")
        return dtime(hour=int(h), minute=int(m), tzinfo=TZ)


# ─────────────────────────────────────────────────────────────
#  СТАРТ
# ─────────────────────────────────────────────────────────────


# Меню команд у Telegram: з'являється по кнопці «/» — нічого шукати не треба.
BOT_COMMANDS = [
    ("today", "План на день"),
    ("evening", "Підсумок дня"),
    ("week", "Аналіз тижня"),
    ("month", "Зріз місяця"),
    ("tasks", "Відкриті задачі"),
    ("content", "Контент-план"),
    ("goals", "Цілі"),
    ("projects", "Напрямки"),
    ("setup", "Розгорнути теми в групі"),
    ("newtopic", "Нова тема під напрямок"),
    ("bind", "Прив'язати тему до напрямку"),
    ("topics", "Список тем"),
    ("export", "Вивантажити базу файлом"),
    ("reset", "Почати діалог заново"),
    ("info", "Стан системи"),
    ("id", "Мій Telegram ID"),
    ("help", "Що я вмію"),
]


async def on_start(app: Application) -> None:
    """Реєструє меню команд. Не вийшло — не біда, бот працює далі."""
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
        me = await app.bot.get_me()
        log.info("Бот @%s готовий, меню команд оновлено", me.username)
        # Найчастіша причина «бот мовчить у групі»: Telegram віддає йому лише
        # команди. Це вмикається за замовчуванням, і зміна в BotFather діє
        # тільки для груп, куди бота додали ПІСЛЯ неї.
        if not me.can_read_all_group_messages:
            log.warning(
                "У ГРУПАХ БОТ БАЧИТЬ ЛИШЕ КОМАНДИ (/...), звичайний текст до "
                "нього не доходить. Полагодити: @BotFather -> /mybots -> бот -> "
                "Bot Settings -> Group Privacy -> Turn off, а потім ВИДАЛИТИ "
                "бота з групи і додати знову."
            )
        else:
            log.info("У групах бот бачить усі повідомлення (privacy вимкнено)")
    except Exception as e:
        log.warning("Не вдалося оновити меню команд: %s", e)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("❌ Немає ASSISTANT_BOT_TOKEN у .env")
    if not ANTHROPIC_API_KEY:
        raise SystemExit("❌ Немає ANTHROPIC_API_KEY у .env")
    if not ALLOWED_IDS:
        raise SystemExit("❌ ASSISTANT_USER_IDS порожній — це персональна база, потрібен whitelist.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(on_start).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", _prompt_command(PROMPT_TODAY)))
    app.add_handler(CommandHandler("evening", _prompt_command(PROMPT_EVENING)))
    app.add_handler(CommandHandler("week", _prompt_command(PROMPT_WEEK)))
    app.add_handler(CommandHandler("month", _prompt_command(PROMPT_MONTH)))
    app.add_handler(CommandHandler("tasks", _list_command(t_list_tasks, {"scope": "open"})))
    app.add_handler(CommandHandler("content", _list_command(t_list_content, {"scope": "plan"})))
    app.add_handler(CommandHandler("goals", _list_command(t_list_goals, {"status": "active"})))
    app.add_handler(CommandHandler("projects", _list_command(t_list_projects, {"status": "active"})))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("newtopic", cmd_newtopic))
    app.add_handler(CommandHandler("bind", cmd_bind))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(
        filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, handle_voice))

    jq = app.job_queue
    if jq is None:
        log.warning(
            "JobQueue недоступна — плани й підсумки за розкладом не працюватимуть. "
            "Встанови: pip install 'python-telegram-bot[job-queue]'"
        )
    else:
        jq.run_daily(_scheduled(PROMPT_TODAY, "plan"),
                     time=_time(MORNING_AT, "08:30"), name="morning")
        jq.run_daily(_scheduled(PROMPT_EVENING, "plan"),
                     time=_time(EVENING_AT, "21:00"), name="evening")
        jq.run_daily(_scheduled(PROMPT_WEEK, "review", only_weekday=6),
                     time=_time(WEEKLY_AT, "19:00"), name="weekly")
        jq.run_daily(_scheduled(PROMPT_MONTH, "review", only_monthday=1),
                     time=_time(MONTHLY_AT, "10:00"), name="monthly")
        jq.run_repeating(job_reminders, interval=60, first=15, name="reminders")

    log.info("Модель: %s", MODEL)
    log.info("Власниця бази: %s, доступ: %s", OWNER_ID, ALLOWED_IDS)
    log.info("База: %s", DB_PATH)
    log.info("Голосові: %s", f"увімкнено ({STT_MODEL})" if OPENAI_API_KEY
             else "вимкнено (немає OPENAI_API_KEY)")
    log.info("✅ Асистент запущено")

    app.run_polling()


if __name__ == "__main__":
    main()
