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
import os, json, time, re, urllib.request, urllib.parse
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

# Пульт облачного агента на удалённом ПК (вкладка Лист1 таблицы «АС ФАРМ клод код»):
# A2=команда (пишем RUN), A3=статус (ждём «RUN done»), A4=heartbeat («alive ДАТА»).
# Через него сторож САМ чинит несвежие цены, не дёргая владельца.
PULT_ID = os.environ.get("PULT_SHEET_ID", "1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms")

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

def _pult_svc():
    cred = Credentials.from_service_account_info(
        SA, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=cred, cache_discovery=False).spreadsheets().values()

def _agent_alive():
    """(жив, heartbeat-строка). Агент штампует A4 «alive YYYY-MM-DD HH:MM:SS» ~каждые 2 мин."""
    try:
        v = _pult_svc().get(spreadsheetId=PULT_ID, range="Лист1!A4").execute().get("values", [])
        hb = v[0][0] if v and v[0] else ""
        m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", hb)
        if not m:
            return False, hb
        t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
        return (datetime.now(MSK) - t) < timedelta(minutes=15), hb
    except Exception as e:
        print("[watchdog] heartbeat недоступен:", e, flush=True)
        return False, ""

def _agent_run(wait_min=12):
    """Пишем RUN на пульт агента ПК, ждём «RUN done» (~8-10 мин прогон). True если дождались."""
    val = _pult_svc()
    try:
        val.update(spreadsheetId=PULT_ID, range="Лист1!A2",
                   valueInputOption="RAW", body={"values": [["RUN"]]}).execute()
        print("[watchdog] отправил RUN агенту ПК, жду завершения…", flush=True)
        done = False
        for _ in range(wait_min * 6):
            time.sleep(10)
            v = val.get(spreadsheetId=PULT_ID, range="Лист1!A3").execute().get("values", [])
            a3 = v[0][0] if v and v[0] else ""
            if "RUN done" in a3:
                print("[watchdog] агент отчитался:", a3, flush=True)
                done = True
                break
        val.update(spreadsheetId=PULT_ID, range="Лист1!A2",
                   valueInputOption="RAW", body={"values": [[""]]}).execute()  # снять команду
        return done
    except Exception as e:
        print("[watchdog] команда RUN не удалась:", e, flush=True)
        return False

def _recount(notes, today):
    stale, logout = [], []
    for name, c in MARKETS:
        note = notes.get(c, "")
        if not note:
            continue
        if ("LOGOUT" in note) and (today in note):
            logout.append(name)
        elif today not in note:
            stale.append(name)
    return stale, logout

def run():
    now = datetime.now(MSK)
    today = now.strftime("%Y-%m-%d")
    phase = "early" if now.hour < 11 else "late"
    notes = _notes()
    st = _load_state()
    if st.get("date") != today:
        st = {"date": today, "alerted": []}
    col = {m: c for m, c in MARKETS}

    stale, logout = _recount(notes, today)
    for name, c in MARKETS:
        print(f"[watchdog] {name}: note={notes.get(c,'')!r}", flush=True)

    # --- АВТО-РЕМОНТ через агента ПК (обе фазы) ---
    # Несвежесть (не из-за разлогина) чиним сами: если агент ПК жив — командуем RUN,
    # ждём и перечитываем штампы. Владельца дёргаем только если и это не помогло.
    agent_hb = ""
    if stale:
        alive, agent_hb = _agent_alive()
        if alive:
            print(f"[watchdog] несвежие {stale}: агент ПК жив ({agent_hb}) → RUN", flush=True)
            if _agent_run():
                notes = _notes()
                stale, logout = _recount(notes, today)
                print(f"[watchdog] после RUN: stale={stale} logout={logout}", flush=True)
            else:
                print("[watchdog] RUN не дал «done» вовремя", flush=True)
        else:
            print(f"[watchdog] несвежие {stale}, агент ПК не отвечает (hb={agent_hb!r}) — RUN не шлю", flush=True)

    if phase == "early":
        # облачный фолбэк для ВБ, если авто-ремонт не помог (или агент офлайн)
        if "WB" in stale:
            _redispatch_wb()
        print("[watchdog] ранняя фаза: чиню, не алертю", flush=True)
        _save_state(st); return

    # финальная фаза 13:45 — ОДИН дайджест-статус в день: и пруф «живо», и алерт при сбое.
    # Владелец просила постоянный контроль без сюрпризов → каждый день видит статус
    # всех 4 маркетов (✅ свежие / ❌ не обновились), даже когда всё хорошо.
    if "digest" not in st.get("alerted", []):
        lines = []
        for name, c in MARKETS:
            note = notes.get(c, "")
            if ("LOGOUT" in note) and (today in note):
                lines.append(f"❌ {name}: Озон разлогинен на ПК — нужен вход в кабинет")
            elif today in note:
                lines.append(f"✅ {name}: свежие сегодня")
            elif not note:
                lines.append(f"⚠️ {name}: базы ещё нет")
            else:
                last = note.split("обновлено")[-1].strip()[:16] if "обновлено" in note else note[:16]
                lines.append(f"❌ {name}: НЕ обновились (посл. {last})")
        bad = bool(logout or stale)
        head = ("<b>⚠️ Цены: сегодня обновились НЕ все</b>" if bad
                else "<b>✅ Цены собраны сегодня — все маркеты</b>")
        # если несвежесть осталась и агент ПК не отвечал — вероятно, ПК/агент офлайн
        pc_off = bool(stale) and not _agent_alive()[0]
        if bad and pc_off:
            tail = "\n\n⚠️ Агент на ПК не отвечает — автопочинка не сработала. Николь, проверь ПК."
        elif bad:
            tail = "\n\nАвтопочинка (RUN на ПК) не помогла. Николь, зайди пожалуйста — починим."
        else:
            tail = ""
        _tg(head + "\n" + "\n".join(lines) + tail)
        st.setdefault("alerted", []).append("digest")
        print("[watchdog] дайджест отправлен:", "СБОЙ" if bad else "всё ок", flush=True)
    _save_state(st)

if __name__ == "__main__":
    run()
