"""Сторож витринных цен (WB / ОЗОН / ЯМ) на Лист1 «Цены АС Фарм».

Каждый парсер при успешной записи штампует в ПРИМЕЧАНИЕ к своей ячейке-заголовку
(строка 1): ВБ→B1, ОЗОН→H1, ЯМ→O1 — строку вида «... обновлено YYYY-MM-DD HH:MM ...».
Озон при разлогине штампует «ОЗ LOGOUT YYYY-MM-DD HH:MM».

Правило владельца:
  • ранняя проверка 07:45 МСК — если ВБ не собрал сегодня, пробуем починиться (re-dispatch wb_prices).
    Озон/ЯМ живут на удалённом компе — из облака их не перезапустить, ждут своего 13:00.
  • финальная проверка 13:45 МСК — если маркет так и не обновился СЕГОДНЯ (оба прогона мимо) →
    ОДНО сообщение в канал «АС Фарм изменения». Для Озон-разлогина — отдельный текст.
Дедуп: максимум одно сообщение на маркет в день (data/price_watchdog_state.json).
"""
import os, json, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

MSK = timezone(timedelta(hours=3))
SHEET_ID = os.environ["PRICES_SHEET_ID"]
SA = json.loads(os.environ["GSHEETS_SA_JSON"])
# .strip() ОБЯЗАТЕЛЕН: секрет TELEGRAM_BOT_TOKEN хранится с лишним переносом
# строки. Без strip URL становится '/bot<token>\n/sendMessage' → urllib падает
# «URL can't contain control characters» и алерт молча не уходит. Именно из-за
# этого сторож молчал неделю, пока цены с ПК стояли (notify.py strip уже делает).
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "nikooolechka/wb-oz-monitor")
GH_TOKEN = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN", "")
STATE_FILE = "data/price_watchdog_state.json"

# маркет -> индекс столбца (0-based) ячейки-заголовка в строке 1 Лист1
MARKETS = [("WB", 1), ("ОЗОН", 7), ("ЯМ", 14), ("ДМ", 20)]

def _svc():
    cred = Credentials.from_service_account_info(
        SA, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    return build("sheets", "v4", credentials=cred, cache_discovery=False)

def _notes():
    """Примечания ячеек A1..O1 Лист1 -> {col0: note}."""
    svc = _svc()
    res = svc.spreadsheets().get(
        spreadsheetId=SHEET_ID, ranges=["Лист1!A1:U1"],
        fields="sheets.data.rowData.values.note").execute()
    out = {}
    try:
        row = res["sheets"][0]["data"][0]["rowData"][0]["values"]
        for i, cell in enumerate(row):
            n = cell.get("note")
            if n:
                out[i] = n
    except Exception:
        pass
    return out

def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_state(st):
    os.makedirs("data", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

def _tg(text):
    if not (TG_TOKEN and TG_CHAT):
        print("[watchdog] нет TELEGRAM_* — сообщение не отправлено:", text, flush=True); return
    body = urllib.parse.urlencode({
        "chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=body, timeout=30).read()
        print("[watchdog] отправлено в канал:", text[:60], flush=True)
    except Exception as e:
        print("[watchdog] ошибка отправки:", e, flush=True)

def _redispatch_wb():
    if not GH_TOKEN:
        print("[watchdog] нет токена для re-dispatch WB", flush=True); return
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GH_REPO}/actions/workflows/wb_prices.yml/dispatches",
        data=json.dumps({"ref": "main", "inputs": {"dry": ""}}).encode(),
        headers={"Authorization": "token " + GH_TOKEN,
                 "Accept": "application/vnd.github+json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=30).read()
        print("[watchdog] самопочинка: перезапустил wb_prices", flush=True)
    except Exception as e:
        print("[watchdog] re-dispatch не удался:", e, flush=True)

def run():
    now = datetime.now(MSK)
    today = now.strftime("%Y-%m-%d")
    phase = "early" if now.hour < 11 else "late"
    notes = _notes()
    st = _load_state()
    if st.get("date") != today:
        st = {"date": today, "alerted": []}
    col = {m: c for m, c in MARKETS}

    stale = []      # маркеты, не обновлённые сегодня
    logout = []     # Озон разлогинен сегодня
    for name, c in MARKETS:
        note = notes.get(c, "")
        if not note:
            print(f"[watchdog] {name}: примечания нет (парсер ещё не штамповал) — пропуск", flush=True)
            continue  # базы ещё нет — не тревожим
        fresh = today in note
        is_logout = ("LOGOUT" in note) and (today in note)
        print(f"[watchdog] {name}: fresh={fresh} logout={is_logout} note={note!r}", flush=True)
        if is_logout:
            logout.append(name)
        elif not fresh:
            stale.append(name)

    if phase == "early":
        # самопочинка: только ВБ (облачный). Озон/ЯМ на компе — ждут 13:00.
        if "WB" in stale:
            _redispatch_wb()
        print("[watchdog] ранняя фаза: чиню, не алертю", flush=True)
        _save_state(st); return

    # финальная фаза 13:45 — алерт по тем, кто так и не обновился (один раз в день на маркет)
    for name in logout:
        if name in st["alerted"]:
            continue
        _tg("<b>Озон разлогинился в удалённом ПК — надо залогиниться для поддержания парсера цен.</b>")
        st["alerted"].append(name)
    for name in stale:
        if name in st["alerted"]:
            continue
        _tg(f"<b>{name} цены — сегодня не обновил, что-то сломалось.</b>\n"
            f"Николь, зайди пожалуйста в сессию — починим.")
        st["alerted"].append(name)
    if not (logout or stale):
        print("[watchdog] всё свежее — тишина", flush=True)
    _save_state(st)

if __name__ == "__main__":
    run()
