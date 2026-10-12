"""Монитор ЗАПАСА КОНТЕНТА постера (автонапоминание готовить след. месяц).

Читает очередь постера (content/queue.json из репо nikooolechka.github.io),
считает, на сколько дней ещё хватает СВЕЖИХ постов (неопубликованных в TG,
1 пост/день), и заранее напоминает в канал «АС Фарм изменения» готовить контент
на следующий месяц — чтобы лента НИКОГДА не кончалась внезапно.

Договорённость владельца (2026-10-07): Claude сам, заранее, готовит контент на
след. месяц без пинка. Этот монитор — надёжный триггер «пора готовить».

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (канал изменений).
Cron: ежедневно ~09:00 МСК. Дедуп — data/content_depth_state.json.
"""
import os, json, ssl, urllib.request
from datetime import datetime, timezone, timedelta

QUEUE_URL = "https://raw.githubusercontent.com/nikooolechka/nikooolechka.github.io/main/content/queue.json"
STATE = os.environ.get("CONTENT_DEPTH_STATE", "data/content_depth_state.json")
MSK = timezone(timedelta(hours=3))
LOW_DAYS = 18      # меньше стольки дней запаса → алерт «пора готовить»
RECOVER_DAYS = 30  # выше стольки → сбрасываем флаг (алерт снова возможен)
_ctx = ssl._create_unverified_context()
MONTHS = ["","январь","февраль","март","апрель","май","июнь","июль","август",
          "сентябрь","октябрь","ноябрь","декабрь"]


def _pending_days() -> int:
    r = urllib.request.urlopen(QUEUE_URL, timeout=40, context=_ctx)
    q = json.load(r)
    return sum(1 for p in q
               if not p.get("channels", {}).get("tg", {}).get("posted_at"))


def _load_state() -> dict:
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def main():
    days = _pending_days()
    now = datetime.now(MSK)
    until = (now + timedelta(days=days)).strftime("%d.%m.%Y")
    nxt = MONTHS[(now.month % 12) + 1]
    st = _load_state()
    print(f"content_depth: запас {days} дней (до {until}).")

    # сброс флага, когда запас восстановили
    if days >= RECOVER_DAYS and st.get("alerted"):
        st["alerted"] = False
        _save_state(st)

    if days <= LOW_DAYS and not st.get("alerted"):
        from notify import send
        send(f"<b>📅 Контент постера заканчивается через {days} дней (до {until}).</b>\n"
             f"Пора готовить контент на <b>{nxt}</b> для всех соцсетей: обложки + тексты вперемешку. "
             f"Николь, зайди в сессию — соберём заранее.")
        st["alerted"] = True
        st["last_alert"] = now.isoformat()
        _save_state(st)
        print("АЛЕРТ отправлен.")
    else:
        print("алерт не нужен (запас ок или уже предупреждали).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
