# -*- coding: utf-8 -*-
# Облачный сбор цен Ozon «с картой» из репрайсера Huckster (без ПК) -> вкладка "oz_цены"
# таблицы «АС ФАРМ клод код». История за 2 недели (строки старше удаляются).
# Запуск: GitHub Actions по часу (Ozon в Huckster обновляется раз в час).
# Env: HUCKSTER_USER, HUCKSTER_PASS, GSHEETS_SA_JSON (или GSHEETS_SA_FILE для локали).
import os, re, json, ssl, datetime, urllib.request, urllib.parse
from google.oauth2 import service_account
import google.auth.transport.requests as gtr

CTX = ssl._create_unverified_context()
HBASE = "https://wbs.e-teleport.ru"
HUSER = os.environ.get("HUCKSTER_USER", "")
HPASS = os.environ.get("HUCKSTER_PASS", "")
OZ_SHOP = "189077_FBO"
SHEET = "1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"  # АС ФАРМ клод код
TAB = "oz_цены"
# (marketplace Huckster, shop_id, вкладка-архив). Пишем цену С КАРТОЙ/КОШЕЛЬКОМ (market_card_price).
MARKETS = [
    ("ozon", "189077_FBO", "oz_цены"),
    ("wildberries", "1884480", "wb_цены"),
    ("yandex", "22610087", "ym_цены"),
]
STATE_TAB = "oz_alert_state"
KEEP_DAYS = 14

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "-1004333189794").strip()

# Вилки допустимых цен с картой Ozon (min, max ₽), владелец 2026-10-23.
# Цена вышла за вилку хоть на 1 ₽ -> алерт в канал (с дедупом по событию).
RANGES = {
    "Dental20": (515, 525), "Dental_40": (765, 775), "Dental_100": (1270, 1290),
    "Dental50": (780, 800), "Dental_100_zemlyanika": (1230, 1250), "Dental_100_banan": (1180, 1200),
    "Dental_20_zemlyanika": (505, 515), "Dental_40_zemlyanika": (765, 775), "Dental_40_natural": (775, 785),
    "Zub_pasta_det": (375, 385), "Irrigator_500": (670, 690), "Irrigator_1000": (1150, 1170),
    "OptikaSpray_new": (365, 375), "spraydlyapolostyrta": (630, 650), "Oral_cherry": (650, 670),
    "CrioGel1l": (1230, 1250), "CrioGel_5": (3520, 3560), "Crio_L25(new)": (3730, 3770),
    "CrioL50": (6310, 6390), "cryolipolysis25": (3520, 3560), "Cryolipolysis50": (5660, 5720),
    "makeup_30": (580, 600), "extract_romashka": (750, 770), "extract_pihta": (750, 770),
    "makeup_50": (950, 970), "irrigator_new_05": (670, 690), "irrigator_new_1": (1150, 1170),
}

def msk():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)

# ---- Huckster ----
def _hpost(path, body):
    r = urllib.request.Request(HBASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0"}, method="POST")
    resp = urllib.request.urlopen(r, context=CTX, timeout=30)
    return resp

def huckster_session():
    h = _hpost("/md5", {"input": HPASS}).read().decode()
    resp = _hpost("/auth/credentials", {"userName": HUSER, "password": h})
    sc = resp.getheader("Set-Cookie")
    sid = re.search(r"ss-id=([^;]+)", sc).group(1)
    pid = re.search(r"ss-pid=([^;]+)", sc).group(1)
    return {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0",
            "Cookie": f"ss-id={sid}; ss-pid={pid}"}

def huckster_cards(H, mp, shop):
    r = urllib.request.Request(HBASE + "/markets/integrations/repricer/items/list",
        data=json.dumps({"marketplace": mp, "shop_id": shop, "limit": 200, "offset": 0}).encode(),
        headers=H, method="POST")
    items = json.loads(urllib.request.urlopen(r, context=CTX, timeout=30).read()).get("result", [])
    out = {}
    for it in items:
        cp = it.get("market_card_price")
        if it.get("parsing_status") == "Работает" and cp:   # активные с ценой
            out[it["uid"]] = int(round(cp))
    return out

# ---- Google Sheets ----
def _tok():
    env = os.environ.get("GSHEETS_SA_JSON", "").strip()
    if env.startswith("{"):
        cr = service_account.Credentials.from_service_account_info(json.loads(env), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    else:
        cr = service_account.Credentials.from_service_account_file(
            os.environ.get("GSHEETS_SA_FILE", os.path.expanduser("~/Desktop/files/claude-sheets-497816-f533a416fa81.json")),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
    cr.refresh(gtr.Request()); return cr.token

def gapi(method, url, body=None):
    r = urllib.request.Request(url, data=(json.dumps(body).encode() if body is not None else None), method=method,
        headers={"Authorization": "Bearer " + _tok(), "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, context=CTX, timeout=60).read())

def ensure_tab(name):
    meta = gapi("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}?fields=sheets(properties(sheetId,title))")
    for s in meta["sheets"]:
        if s["properties"]["title"] == name:
            return s["properties"]["sheetId"]
    res = gapi("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}:batchUpdate",
        {"requests": [{"addSheet": {"properties": {"title": name}}}]})
    return res["replies"][0]["addSheet"]["properties"]["sheetId"]

def read_vals(rng):
    d = gapi("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/{urllib.parse.quote(rng)}")
    return d.get("values", [])

# ---- алерт по вилке ----
def notify(text):
    if not TG_TOKEN:
        print("[WARN] нет TELEGRAM_BOT_TOKEN — алерт не отправлен"); return
    silent = msk().weekday() >= 5   # выходные (сб/вс) — без звука
    body = json.dumps({"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_notification": silent}).encode()
    r = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=body,
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, context=CTX, timeout=25).read()

def alert_text(name, cur, lo, hi):
    d = f"↓ ниже минимума на {lo-cur} ₽" if cur < lo else f"↑ выше максимума на {cur-hi} ₽"
    return (f"<b>❗️Ozon — цена вышла за допустимый диапазон</b>\n\n"
            f"<b>{name}</b>\n"
            f"сейчас: <b>{cur} ₽</b>\n"
            f"допустимо: {lo}–{hi} ₽\n{d}")

def _dirstr(c, lo, hi):
    return f"↓ на {lo-c} ₽" if c < lo else f"↑ на {c-hi} ₽"

def combined_text(items):
    """>2 товаров за раз -> одно сообщение со свёрнутым (раскрывающимся) списком."""
    lines = [f"• <b>{n}</b>: {c} ₽ — вилка {lo}–{hi}, {_dirstr(c,lo,hi)}" for (n, c, lo, hi, stt) in items]
    return (f"<b>❗️Ozon — {len(items)} товаров вышли за допустимый диапазон</b>\n"
            f"<blockquote expandable>" + "\n".join(lines) + "</blockquote>")

def _cur_status():
    """{товар: (status, cur, lo, hi)} по текущим ценам (только те, что есть)."""
    out = {}
    def inner(cards):
        for name, (lo, hi) in RANGES.items():
            cur = cards.get(name)
            if cur is None:
                continue
            stt = "below" if cur < lo else ("above" if cur > hi else "ok")
            out[name] = (stt, cur, lo, hi)
        return out
    return inner

def morning_summary(night, cur):
    """Сводка за ночь: что выходило за вилку + исправилось/нет сейчас."""
    lines = []
    for name, rec in night.items():
        lo, hi = rec["lo"], rec["hi"]
        now = cur.get(name)
        if now is None:
            tail = "сейчас нет данных"
        elif now[0] == "ok":
            tail = f"сейчас {now[1]} ₽ — исправилось"
        else:
            where = "всё ещё ниже" if now[0] == "below" else "всё ещё выше"
            tail = f"сейчас {now[1]} ₽ — {where}"
        lines.append(f"• <b>{name}</b>: ночью {rec['worst']} ₽ (вилка {lo}–{hi}) → {tail}")
    head = f"<b>❗️Ozon — за ночь {len(lines)} цен вне диапазона</b>\n"
    if len(lines) > 2:
        return head + "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"
    return head + "\n" + "\n".join(lines)

def check_alerts(cards):
    ensure_tab(STATE_TAB)
    try:
        v = read_vals(f"{STATE_TAB}!A1")
        st = json.loads(v[0][0]) if v and v[0] and v[0][0] else {}
    except Exception:
        st = {}
    day_status = st.get("day_status", {})
    night = st.get("night", {})
    h = msk().hour
    today = msk().strftime("%Y-%m-%d")
    summary_date = st.get("summary_date", "")
    cur = _cur_status()(cards)
    is_night = (h >= 20 or h < 8)
    sent = 0
    if is_night:
        # ночь: копим события, в канал НЕ шлём
        for name, (stt, c, lo, hi) in cur.items():
            if stt != "ok":
                rec = night.get(name)
                worst = c if not rec else (min(rec["worst"], c) if stt == "below" else max(rec["worst"], c))
                night[name] = {"dir": stt, "worst": worst, "lo": lo, "hi": hi}
        day_status = {n: s[0] for n, s in cur.items()}        # тихая синхронизация
    else:
        # день 8–19
        if night and summary_date != today:
            # утренняя сводка за ночь — ОДИН раз в день, на первом дневном прогоне
            # (устойчиво к пропуску планировщиком ровно 8:00)
            notify(morning_summary(night, cur)); sent = 1
            print(f"[SUMMARY] ночных товаров: {len(night)}", flush=True)
            summary_date = today
            night = {}
            day_status = {n: s[0] for n, s in cur.items()}    # без индивидуальных алертов на сводочном прогоне
        else:
            # собираем НОВЫЕ выходы за вилку (один раз на событие)
            newly = []
            for name, (stt, c, lo, hi) in cur.items():
                old = day_status.get(name, "ok")
                if stt != old and stt != "ok":
                    newly.append((name, c, lo, hi, stt))
                day_status[name] = stt
            if len(newly) > 2:
                # больше двух за раз -> одно свёрнутое сообщение (как дайджест)
                notify(combined_text(newly)); sent = 1
                print(f"[ALERT-COMBINED] {len(newly)} товаров вне вилки", flush=True)
            else:
                for (name, c, lo, hi, stt) in newly:
                    notify(alert_text(name, c, lo, hi)); sent += 1
                    print(f"[ALERT] {name}: {c} вне {lo}-{hi} ({stt})", flush=True)
    gapi("PUT", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/{urllib.parse.quote(STATE_TAB+'!A1')}?valueInputOption=RAW",
         {"values": [[json.dumps({"day_status": day_status, "night": night, "summary_date": summary_date}, ensure_ascii=False)]]})
    print(f"[OK] отправлено сообщений: {sent} (час {h} МСК, {'ночь' if is_night else 'день'})")

def record(cards, tab):
    """Снимок цен во вкладку tab + катим историю 2 недели (старые строки сверху удаляем)."""
    sid = ensure_tab(tab)
    rows = read_vals(f"{tab}!A1:ZZ10000")
    header = rows[0] if rows else []
    if not header:
        header = ["дата-время"] + sorted(cards.keys())
        gapi("PUT", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/{urllib.parse.quote(tab+'!A1')}?valueInputOption=RAW",
             {"values": [header]})
    new_cols = [t for t in sorted(cards) if t not in header]
    if new_cols:
        header = header + new_cols
        gapi("PUT", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/{urllib.parse.quote(tab+'!A1')}?valueInputOption=RAW",
             {"values": [header]})
        print(f"[INFO] {tab}: новые товары-колонки: {new_cols}")
    ts = msk().strftime("%Y-%m-%d %H:%M")
    row = [ts] + [cards.get(h, "") for h in header[1:]]
    gapi("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/{urllib.parse.quote(tab+'!A1')}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
         {"values": [row]})
    print(f"[OK] {tab}: снимок {ts}, товаров {len(cards)}")
    cutoff = (msk() - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d %H:%M")
    rows = read_vals(f"{tab}!A1:A10000")
    old = 0
    for r in rows[1:]:
        if r and r[0] and r[0] < cutoff:
            old += 1
        else:
            break
    if old:
        gapi("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}:batchUpdate",
            {"requests": [{"deleteDimension": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 1, "endIndex": 1 + old}}}]})
        print(f"[OK] {tab}: удалено старых строк (>{KEEP_DAYS} дн): {old}")

def main():
    H = huckster_session()
    for mp, shop, tab in MARKETS:
        try:
            cards = huckster_cards(H, mp, shop)
        except Exception as e:
            print(f"[WARN] {mp}: ошибка Huckster: {e}"); continue
        if not cards:
            print(f"[WARN] {mp}: нет цен — пропуск"); continue
        if mp == "ozon":
            check_alerts(cards)          # вилки/алерты пока только по Ozon
        record(cards, tab)

if __name__ == "__main__":
    main()
