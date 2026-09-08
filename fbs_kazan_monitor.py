"""Монитор FBS-заказов WB по складам (по дням) + ловля прихода товара в Казань.

Что делает каждый прогон (запускается раз в сутки, УТРОМ по МСК):
  1. Тянет список FBS-складов продавца (/api/v3/warehouses). Известные:
     Краснодар (1884480), МП Карго Софьино (2033332). Любой склад с «казан»
     в названии распознаётся как Казань — его id подхватывается автоматически.
  2. Считает FBS-заказы (/api/v3/orders) по дням и по складам за период
     с 01.08.2026 ПО ВЧЕРА включительно (МСК). Сегодняшний день НЕ пишем —
     он ещё не закончился и цифра неточная (требование владельца).
  3. Перестраивает таблицу во вкладке FBS (ОП АС Фарм), блок с B70: даты,
     колонки Краснодар / МП Карго Софьино / Казань / Итого, помесячные итоги
     и «ВСЕГО за период». Колонка Казань наполняется сама, когда пойдут заказы.
  4. Ловит приход товара в Казань и шлёт разовые алерты в канал
     «АС Фарм изменения» (дедуп через data/fbs_kazan_state.json):
       - склад Казань впервые появился в кабинете;
       - на складе Казань впервые появился остаток FBS (товар приехал);
       - из Казани впервые поехал FBS-заказ.

Секреты: WB_TOKEN, GSHEETS_SA_JSON, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
DRY=1 → в канал не шлём, таблицу не трогаем (только лог).
"""
from __future__ import annotations

import os
import json
import time
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials

import notify

OP_SHEET = "1sHlFGSVB-7V8V4q6kvcTR1rrw19EaabIaOgrCHU0DHE"
TAB = "FBS"
MP = "https://marketplace-api.wildberries.ru"
CONTENT = "https://content-api.wildberries.ru"
START = datetime(2026, 8, 1, tzinfo=timezone(timedelta(hours=3)))  # начало периода
MSK = timezone(timedelta(hours=3))
FIRST_ROW = 70          # таблица начинается со строки 70
STATE_FILE = "data/fbs_kazan_state.json"
DRY = os.environ.get("DRY") == "1"

KRASNODAR = 1884480
SOFINO = 2033332
MONTHS_RU = {8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
             1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь", 7: "июль"}
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _ctx():
    if os.environ.get("INSECURE_SSL") == "1":
        return ssl._create_unverified_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


CTX = _ctx()
TOKEN = os.environ["WB_TOKEN"].strip()


def _get(url):
    req = urllib.request.Request(url, headers={"Authorization": TOKEN})
    with urllib.request.urlopen(req, context=CTX, timeout=90) as r:
        return json.load(r)


def _post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Authorization": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=CTX, timeout=90) as r:
        return json.load(r)


# ---------- склады ----------
def warehouses():
    wh = _get(f"{MP}/api/v3/warehouses")
    kazan = None
    for w in wh:
        if "казан" in (w.get("name") or "").lower():
            kazan = w
    return wh, kazan


# ---------- заказы по дням ----------
def orders_by_day(date_to):
    """{'YYYY-MM-DD': {wid: count}} за [START, date_to) — date_to не включаем."""
    data = {}
    seen = set()
    now_to = int(date_to.timestamp())
    cur = START
    while cur.timestamp() < now_to:
        w_from = int(cur.timestamp())
        w_to = min(int((cur + timedelta(days=25)).timestamp()), now_to)
        nxt = 0
        for _ in range(200):
            d = _get(f"{MP}/api/v3/orders?limit=1000&next={nxt}&dateFrom={w_from}&dateTo={w_to}")
            orders = d.get("orders", [])
            for o in orders:
                oid = o.get("id")
                if oid in seen:
                    continue
                seen.add(oid)
                dt = datetime.fromisoformat(o["createdAt"].replace("Z", "+00:00")).astimezone(MSK)
                if dt < START or dt >= date_to:
                    continue
                ds = dt.strftime("%Y-%m-%d")
                data.setdefault(ds, {})
                wid = o.get("warehouseId")
                data[ds][wid] = data[ds].get(wid, 0) + 1
            nxt = d.get("next", 0)
            if not orders or not nxt:
                break
            time.sleep(0.25)
        cur = cur + timedelta(days=25)
    return data


# ---------- остаток FBS по Казани ----------
def kazan_stock(kazan_id):
    """Сумма остатков FBS на складе Казань. None — если не смогли получить."""
    try:
        barcodes = []
        cursor = {"limit": 1000}
        for _ in range(30):
            body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
            d = _post(f"{CONTENT}/content/v2/get/cards/list", body)
            cards = d.get("cards", [])
            for c in cards:
                for s in c.get("sizes", []):
                    for bc in s.get("skus", []):
                        barcodes.append(bc)
            cur = d.get("cursor", {})
            if len(cards) < 1000:
                break
            cursor = {"limit": 1000, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
            time.sleep(0.3)
        barcodes = list({b for b in barcodes if b})
        total = 0
        for i in range(0, len(barcodes), 1000):
            chunk = barcodes[i:i + 1000]
            st = _post(f"{MP}/api/v3/stocks/{kazan_id}", {"skus": chunk})
            for s in st.get("stocks", []):
                total += s.get("amount", 0) or 0
            time.sleep(0.3)
        return total
    except Exception as e:
        print("stock check failed:", e)
        return None


# ---------- Sheets ----------
def _sheet():
    info = json.loads(os.environ["GSHEETS_SA_JSON"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(OP_SHEET)


def build_rows(day_data, kazan_id):
    """Строим значения таблицы + возвращаем индексы строк для форматирования."""
    end = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)  # сегодня 00:00 = стоп (вчера последний)
    rows = []
    rows.append(["ЗАКАЗЫ FBS ПО ДНЯМ (ШТ)", "", "", "", ""])
    period = f"WB · с 01.08.2026 · обновлено {datetime.now(MSK):%d.%m.%Y %H:%M} МСК · источник: WB API /api/v3/orders"
    rows.append([period, "", "", "", ""])
    rows.append(["Дата", "Краснодар", "МП Карго Софьино", "Казань", "Итого"])
    subtotal_rows, total_kr, total_sf, total_kz = [], 0, 0, 0
    msum = {}
    days = []
    d = START
    while d < end:
        days.append(d)
        d += timedelta(days=1)
    prev_month = None
    r_idx = FIRST_ROW + 3  # первая дата
    for d in days:
        if prev_month is not None and d.month != prev_month:
            k, s, z = msum[prev_month]
            rows.append([f"Итого {MONTHS_RU[prev_month]}", k, s, z, k + s + z])
            subtotal_rows.append(r_idx); r_idx += 1
        prev_month = d.month
        ds = d.strftime("%Y-%m-%d")
        wd = day_data.get(ds, {})
        kr = wd.get(KRASNODAR, 0); sf = wd.get(SOFINO, 0)
        kz = wd.get(kazan_id, 0) if kazan_id else 0
        rows.append([f"{d:%d.%m.%Y} {WD[d.weekday()]}", kr, sf, kz, kr + sf + kz])
        total_kr += kr; total_sf += sf; total_kz += kz
        m = msum.get(d.month, (0, 0, 0)); msum[d.month] = (m[0] + kr, m[1] + sf, m[2] + kz)
        r_idx += 1
    if prev_month is not None:  # итог последнего месяца
        k, s, z = msum[prev_month]
        rows.append([f"Итого {MONTHS_RU[prev_month]}", k, s, z, k + s + z])
        subtotal_rows.append(r_idx); r_idx += 1
    rows.append(["ВСЕГО за период", total_kr, total_sf, total_kz,
                 total_kr + total_sf + total_kz])
    total_row = r_idx
    return rows, subtotal_rows, total_row, (total_kr, total_sf, total_kz)


def write_table(sh, rows, subtotal_rows, total_row):
    ws = sh.worksheet(TAB)
    last = FIRST_ROW + len(rows) - 1
    # очищаем прежний блок (с запасом на случай, если раньше было больше строк)
    ws.batch_clear([f"B{FIRST_ROW}:F{last + 40}"])
    ws.update(f"B{FIRST_ROW}:F{last}", rows, value_input_option="RAW")
    sid = ws.id
    hdr = FIRST_ROW + 2
    dark = {"red": 0.13, "green": 0.28, "blue": 0.53}
    blue = {"red": 0.2, "green": 0.4, "blue": 0.66}
    green = {"red": 0.85, "green": 0.91, "blue": 0.83}
    yellow = {"red": 0.99, "green": 0.85, "blue": 0.4}
    beige = {"red": 1, "green": 0.97, "blue": 0.88}
    white = {"red": 1, "green": 1, "blue": 1}

    def rng(r0, c0, r1, c1):
        return {"sheetId": sid, "startRowIndex": r0 - 1, "endRowIndex": r1,
                "startColumnIndex": c0 - 1, "endColumnIndex": c1}

    def cell(bg=None, bold=False, fs=10, color=None, halign=None, valign=None, italic=False):
        tf = {"fontSize": fs, "bold": bold, "italic": italic}
        if color: tf["foregroundColor"] = color
        cf = {"textFormat": tf}
        if bg: cf["backgroundColor"] = bg
        if halign: cf["horizontalAlignment"] = halign
        if valign: cf["verticalAlignment"] = valign
        return cf

    def fmt(r0, c0, r1, c1, cf, fields="userEnteredFormat"):
        return {"repeatCell": {"range": rng(r0, c0, r1, c1),
                               "cell": {"userEnteredFormat": cf}, "fields": fields}}

    B, F = 2, 6
    reqs = []
    # сброс фона всего блока
    reqs.append(fmt(FIRST_ROW, B, last, F, cell(bg=white), "userEnteredFormat.backgroundColor"))
    # заголовок
    reqs.append(fmt(FIRST_ROW, B, FIRST_ROW, F,
                    cell(bg=dark, bold=True, fs=12, color=white, halign="CENTER", valign="MIDDLE")))
    reqs.append(fmt(FIRST_ROW + 1, B, FIRST_ROW + 1, F,
                    cell(bg={"red": 0.9, "green": 0.93, "blue": 0.98}, fs=9,
                         color={"red": 0.35, "green": 0.35, "blue": 0.4}, halign="CENTER",
                         valign="MIDDLE", italic=True)))
    # шапка колонок
    reqs.append(fmt(hdr, B, hdr, F, cell(bg=blue, bold=True, color=white, halign="CENTER", valign="MIDDLE")))
    # тело: даты слева (текст), числа по центру
    reqs.append(fmt(hdr + 1, B, last, B, cell(halign="LEFT")))
    reqs.append(fmt(hdr + 1, 3, last, F, cell(halign="CENTER")))
    # колонка Казань — лёгкий бежевый фон, чтобы выделялась
    reqs.append(fmt(hdr, 5, last - 1, 5, cell(bg=beige, halign="CENTER"),
                    "userEnteredFormat.backgroundColor"))
    reqs.append(fmt(hdr, 5, hdr, 5, cell(bg=blue, bold=True, color=white, halign="CENTER", valign="MIDDLE")))
    # Итого-колонка жирным
    reqs.append(fmt(hdr + 1, F, last, F, cell(bold=True), "userEnteredFormat.textFormat.bold"))
    # помесячные итоги — зелёные, жирные
    for r in subtotal_rows:
        reqs.append(fmt(r, B, r, F, cell(bg=green, bold=True)))
    # ВСЕГО — жёлтый, жирный
    reqs.append(fmt(total_row, B, total_row, F, cell(bg=yellow, bold=True, fs=11)))
    # даты — текстовый формат (чтобы не превращались в числа)
    reqs.append({"repeatCell": {"range": rng(hdr + 1, B, last, B),
                                "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                                "fields": "userEnteredFormat.numberFormat"}})
    # границы
    solid = {"style": "SOLID", "color": {"red": 0.8, "green": 0.8, "blue": 0.8}}
    med = {"style": "SOLID_MEDIUM"}
    reqs.append({"updateBorders": {"range": rng(FIRST_ROW, B, last, F),
                                   "top": med, "bottom": med, "left": med, "right": med,
                                   "innerHorizontal": solid, "innerVertical": solid}})
    # merge заголовка/подзаголовка (идемпотентно)
    for rr in (FIRST_ROW, FIRST_ROW + 1):
        reqs.append({"mergeCells": {"range": rng(rr, B, rr, F), "mergeType": "MERGE_ALL"}})
    sh.batch_update({"requests": reqs})


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(st):
    os.makedirs("data", exist_ok=True)
    json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False, indent=2)


def main():
    st = load_state()
    wh, kazan = warehouses()
    kazan_id = kazan["id"] if kazan else st.get("kazan_id")
    print("warehouses:", [(w["name"], w["id"]) for w in wh], "| kazan_id:", kazan_id)

    end = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)  # вчера — последний полный день
    day_data = orders_by_day(end)
    kz_orders = sum(day_data.get(d, {}).get(kazan_id, 0) for d in day_data) if kazan_id else 0

    rows, subs, total_row, totals = build_rows(day_data, kazan_id)
    print(f"days written up to {(end - timedelta(days=1)):%d.%m.%Y}; totals Кр/Соф/Каз = {totals}")

    if not DRY:
        write_table(_sheet(), rows, subs, total_row)

    # ---------- алерты ----------
    alerts = []
    if kazan and not st.get("kazan_seen"):
        st["kazan_seen"] = True
        st["kazan_id"] = kazan["id"]
        alerts.append(
            f"<b>WB: в кабинете появился FBS-склад «{kazan['name']}».</b>\n"
            f"Отслеживаю приход товара и заказы — колонка Казань в таблице FBS начнёт наполняться.")

    if kazan_id and not st.get("kazan_stock_alerted"):
        stock = kazan_stock(kazan_id)
        if stock and stock > 0:
            st["kazan_stock_alerted"] = True
            alerts.append(
                f"<b>Товар приехал на склад Казань (FBS): {stock} шт остатка.</b>\n"
                f"Склад {kazan_id} готов к заказам.")

    if kazan_id and kz_orders > 0 and not st.get("kazan_order_alerted"):
        st["kazan_order_alerted"] = True
        alerts.append(
            f"<b>Первый FBS-заказ из Казани.</b>\n"
            f"Всего по Казани уже {kz_orders} шт за период — колонка в таблице FBS обновлена.")

    for msg in alerts:
        print("ALERT:", msg.replace("\n", " / "))
        if not DRY:
            notify.send(msg)
            time.sleep(0.5)

    if not DRY:
        save_state(st)


if __name__ == "__main__":
    main()
