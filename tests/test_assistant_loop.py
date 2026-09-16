"""Перевірка циклу агента: виклик інструмента → результат → відповідь у потрібну гілку."""
import os, sys, asyncio, tempfile, types

db_path = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.update({"ASSISTANT_DB": db_path, "ANTHROPIC_API_KEY": "sk-ant-test",
                   "ASSISTANT_BOT_TOKEN": "123:TEST"})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import inna_assistant as ab

FAILS = []
ok = lambda c, w: print(("PASS " if c else "FAIL ") + w) or (c or FAILS.append(w))


def blk(**kw):
    b = types.SimpleNamespace(**kw)
    return b


class FakeResp:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason
        self.usage = types.SimpleNamespace(input_tokens=10, output_tokens=5)


class FakeMessages:
    def __init__(self): self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            return FakeResp([
                blk(type="text", text="Записую."),
                blk(type="tool_use", id="tu_1", name="add_task",
                    input={"title": "Договір", "assignee": "Оля", "priority": 1}),
            ], "tool_use")
        return FakeResp([blk(type="text", text="Готово, задача в системі.")], "end_turn")


class FakeBot:
    def __init__(self): self.sent = []

    async def send_message(self, chat_id, text, message_thread_id=None):
        self.sent.append((chat_id, message_thread_id, text))


async def main():
    fake = FakeMessages()
    ab.claude = types.SimpleNamespace(messages=fake)
    bot = FakeBot()
    history = [{"role": "user", "content": "постав Олі договір, терміново"}]
    await ab.run_agent(ab.OWNER_ID, -100500, bot, history, thread_id=7)

    ok(len(fake.calls) == 2, "два кроки: інструмент, потім відповідь")
    ok(fake.calls[0]["tools"] is ab.TOOLS, "інструменти передані моделі")
    ok(fake.calls[0]["system"][0]["cache_control"]["type"] == "ephemeral", "системний промпт кешується")
    ok("СЬОГОДНІ" in fake.calls[0]["system"][0]["text"], "дата в системному промпті")
    ok(all(t == 7 for _, t, _ in bot.sent), "усі відповіді пішли у свою гілку теми")
    ok(bot.sent[-1][2] == "Готово, задача в системі.", "фінальний текст доставлено")

    rows = ab.q("SELECT * FROM tasks WHERE owner_id = ?", (ab.OWNER_ID,))
    ok(len(rows) == 1 and rows[0]["assignee"] == "Оля" and rows[0]["priority"] == 1,
       "інструмент реально записав задачу в базу")

    # history — той самий список, який бачила модель на другому кроці
    results = [m for m in history if m["role"] == "user" and isinstance(m["content"], list)
               and m["content"] and m["content"][0].get("type") == "tool_result"]
    ok(len(results) == 1, "результат інструмента повернуто моделі")
    ok(results[0]["content"][0]["tool_use_id"] == "tu_1", "tool_use_id збігається")
    ok("Задача #" in results[0]["content"][0]["content"], "у результаті — відповідь інструмента")
    ok(history[-1]["role"] == "assistant", "історія завершується відповіддю моделі")

    # відмова моделі не ламає бота
    class Refuse:
        async def create(self, **kw): return FakeResp([], "refusal")
    ab.claude = types.SimpleNamespace(messages=Refuse())
    bot2 = FakeBot()
    await ab.run_agent(ab.OWNER_ID, -100500, bot2, [{"role": "user", "content": "…"}], 7)
    ok("відмовилась" in bot2.sent[0][2], "stop_reason=refusal оброблено")

    print("\nПОМИЛОК:", len(FAILS), FAILS)
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
