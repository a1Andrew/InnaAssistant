"""Димовий тест шару даних inna_assistant.py — без Telegram і без мережі."""
import os, sys, tempfile

db_path = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.update({
    "ASSISTANT_DB": db_path,
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "ASSISTANT_BOT_TOKEN": "123:TEST",
    "ASSISTANT_USER_IDS": "595153958",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import inna_assistant as ab

U = ab.OWNER_ID
ok = lambda cond, what: print(("PASS " if cond else "FAIL ") + what) or (cond or FAILS.append(what))
FAILS = []

print("owner:", U, "| allowed:", ab.ALLOWED_IDS)
ok(595153958 in ab.ALLOWED_IDS, "ID власниці у whitelist")

# дати
ok(ab.parse_date("today") == ab.iso(ab.today()), "parse_date today")
ok(ab.parse_date("завтра") == ab.iso(ab.today() + ab.timedelta(days=1)), "parse_date завтра")
ok(ab.parse_date("2026-09-20") == "2026-09-20", "parse_date iso")
ok(ab.parse_date("20.09.2026") == "2026-09-20", "parse_date dd.mm.yyyy")
ok(ab.parse_date("казна-що") is None, "parse_date сміття → None")
ok(ab.parse_dt("2026-09-20 14:30") == "2026-09-20 14:30", "parse_dt")
ok(ab.parse_dt("2026-09-20") is None, "parse_dt без часу → None")
ok(ab._next_date("2026-01-31", "monthly").startswith("2026-02"), "monthly з 31 числа не падає")
ok(ab._next_date("2026-09-16", "weekly") == "2026-09-23", "weekly +7")

# напрямки і задачі
print(ab.t_add_project(U, {"name": "Наша робота", "kind": "work"}))
print(ab.t_add_project(U, {"name": "наша РОБОТА"}))
ok(len(ab.q("SELECT * FROM projects WHERE owner_id=?", (U,))) == 1, "напрямок не дублюється")

r = ab.t_add_task(U, {"title": "Договір для клієнта", "project": "Право і порядок",
                      "assignee": "Співробітник", "priority": 1, "due_date": "today"})
print(r)
tid = int(r.split("#")[1].split()[0])
print(ab.t_add_task(U, {"title": "Зарядка", "repeat": "daily", "plan_date": "today"}))
print(ab.t_add_task(U, {"title": "Старий борг", "due_date": "2020-01-01"}))

out = ab.t_list_tasks(U, {"scope": "today"})
ok("Договір" in out and "Зарядка" in out, "scope=today показує сьогоднішні")
ok("@Співробітник" in out, "виконавець видно у списку")
ok("Старий борг" in ab.t_list_tasks(U, {"scope": "overdue"}), "scope=overdue ловить прострочене")
ok("Договір" in ab.t_list_tasks(U, {"assignee": "співробітник"}), "фільтр по виконавцю без регістру")
ok(ab.project_name(ab.q("SELECT project_id p FROM tasks WHERE id=?", (tid,))[0]["p"]) == "Право і порядок",
   "напрямок створився автоматично з задачі")

# повторювані задачі
rep_id = ab.q("SELECT id FROM tasks WHERE title='Зарядка'")[0]["id"]
res = ab.t_update_task(U, {"id": rep_id, "status": "done"})
print(res)
ok("Наступна" in res, "закриття повторюваної створює наступну")
ok(len(ab.q("SELECT * FROM tasks WHERE title='Зарядка'")) == 2, "нова копія в базі")
ok(ab.t_update_task(U, {"id": 9999, "status": "done"}).startswith("Задачі"), "неіснуючий id не падає")

# контент
c = ab.t_add_content(U, {"title": "Рілс про докази", "format": "reels",
                         "publish_date": "today", "rubric": "право"})
cid = int(c.split("#")[1].split()[0])
print(ab.t_update_content(U, {"id": cid, "status": "published", "views": 12000, "saves": 300}))
ok("12000" in ab.t_list_content(U, {"scope": "published"}), "метрики зберігаються")

# щоденник і навчання
print(ab.t_add_log(U, {"category": "training", "item": "ноги", "value": 50, "unit": "хв"}))
print(ab.t_add_log(U, {"category": "body", "item": "вага", "value": 58.4, "unit": "кг"}))
print(ab.t_add_log(U, {"category": "study", "item": "англійська", "value": 45, "unit": "хв"}))
ok("вага" in ab.t_list_logs(U, {"category": "body"}), "щоденник читається")

# цілі, нотатки, пошук, нагадування
g = ab.t_add_goal(U, {"title": "10к підписників", "horizon": "month", "metric": "підписники"})
gid = int(g.split("#")[1].split()[0])
print(ab.t_update_goal(U, {"id": gid, "progress": "6.2к"}))
ok("6.2к" in ab.t_list_goals(U, {}), "прогрес цілі зберігся")
print(ab.t_add_note(U, {"title": "Ідея рубрики", "text": "розбір справ підписників"}))
ok("Ідея рубрики" in ab.t_search(U, {"query": "РУБРИК"}), "пошук по нотатках (інший регістр)")
ok("Рілс" in ab.t_search(U, {"query": "докази"}), "пошук по контенту")
rem = ab.t_add_reminder(U, {"text": "дзвінок", "at": "2026-12-01 10:00"})
ok("#" in rem and "дзвінок" in ab.t_list_reminders(U, {}), "нагадування")
ok(ab.t_add_reminder(U, {"text": "х", "at": "не дата"}).startswith("Потрібні"), "кривий час відхилено")

# зріз
for period in ("day", "week", "month"):
    rv = ab.t_review(U, {"period": period})
    ok("ЗРІЗ" in rv and "ЗАДАЧІ" in rv, f"review {period}")
print("\n--- review week ---\n" + ab.t_review(U, {"period": "week"}))

# інструменти через диспетчер + ізоляція від чужого користувача
ok("Зараз:" in ab.execute_tool("now", {}, U), "диспетчер now")
ok("Невідомий" in ab.execute_tool("немає_такого", {}, U), "невідомий інструмент не падає")
ok("Задач" in ab.execute_tool("list_tasks", {"scope": "open"}, 111), "чужий uid бачить порожньо")
ok(all(t["name"] in ab.TOOL_IMPL for t in ab.TOOLS), "усі описані інструменти реалізовані")
ok(len(ab.TOOL_IMPL) == len(ab.TOOLS), "немає зайвих реалізацій")

# теми групи
ab.run("INSERT INTO topics (chat_id, thread_id, purpose, title, project, created_at) "
       "VALUES (?,?,?,?,?,?)", (-100123, 7, "content", "📸 Контент", "Instagram", ab._stamp()))
ok(ab.thread_for(-100123, "content") == 7, "гілка за призначенням")
ok(ab.topic_of(-100123, 7)["project"] == "Instagram", "напрямок теми")
ok(ab.topic_of(-100123, None) is None, "без гілки — None")

# куди пише розклад: група перебиває особистий чат, але не навпаки
ab.remember_chat(555)
ab.remember_chat(-100123)
ab.remember_chat(555)
ok(ab.default_chat() == -100123, "група лишається адресою розкладу")

# меню команд: назви валідні й кожна справді має обробник
import re
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "inna_assistant.py"), encoding="utf-8").read()
handlers = set(re.findall(r'CommandHandler\("(\w+)"', src))
menu = {c for c, _ in ab.BOT_COMMANDS}
ok(menu <= handlers, f"усі команди меню мають обробник (зайві: {menu - handlers})")
ok(all(re.fullmatch(r"[a-z0-9_]{1,32}", c) for c, _ in ab.BOT_COMMANDS),
   "назви команд у форматі Telegram")
ok(all(0 < len(d) <= 256 for _, d in ab.BOT_COMMANDS), "описи команд не порожні й не довгі")

print("\nПОМИЛОК:", len(FAILS), FAILS)
sys.exit(1 if FAILS else 0)
