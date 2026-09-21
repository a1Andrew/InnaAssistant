"""Питає в Telegram, чи бачить бот звичайні повідомлення в групах."""
import json
import sys
import urllib.request

try:
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{sys.argv[1]}/getMe", timeout=15
    ) as resp:
        me = json.load(resp)["result"]
except Exception as e:                      # мережа, кривий токен тощо
    print("не вдалося спитати Telegram:", e)
    raise SystemExit

print("бот: @" + me.get("username", "?"))
if me.get("can_read_all_group_messages"):
    print("режим груп: ✔ бачить УСІ повідомлення")
else:
    print("режим груп: ✘ ТІЛЬКИ КОМАНДИ (/...) — звичайний текст не доходить")
    print("   @BotFather -> /mybots -> бот -> Bot Settings -> Group Privacy -> Turn off,")
    print("   а потім ВИДАЛИТИ бота з групи і додати знову — інакше для неї нічого не зміниться")
