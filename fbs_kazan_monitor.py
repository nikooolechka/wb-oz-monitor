"""Монитор FBS-заказов по складам (по дням): WB + Ozon, в одну вкладку FBS.

Что делает каждый прогон (раз в сутки, УТРОМ по МСК):
  1. WB: список FBS-складов (/api/v3/warehouses) — Краснодар (1884480),
     МП Карго Софьино (2033332), Казань (распознаётся по «казан» в названии,
     id подхватывается сам). Заказы — /api/v3/orders по дням и по складам.
  2. Ozon: FBS-отправления (/v3/posting/fbs/list) по дням и по складам
     (штуки = сумма quantity товаров в отправлении).
  3. Пишет ДВА блока во вкладку FBS (ОП АС Фарм), оба со строки 65:
       - WB  — со столбца B: Дата / Краснодар / МП Карго Софьино / Казань / Итого;
       - Ozon — со столбца I: Дата / <склады FBS Ozon> / Итого.
     Период: с 01.08.2026 ПО ВЧЕРА включительно (МСК). Сегодняшний день НЕ пишем —
     он не закончился, цифра неточная (требование владельца).
  4. Ловит приход товара в Казань и шлёт разовые алерты в канал
     «АС Фарм изменения» (дедуп через data/fbs_kazan_state.json):
       - склад Казань впервые появился в кабинете WB;
       - на складе Казань впервые появился остаток FBS (товар приехал);
       - из Казани впервые поехал FBS-заказ.

Секреты: WB_TOKEN, OZON_CLIENT_ID, OZON_API_KEY, GSHEETS_SA_JSON,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. DRY=1 → только лог, ничего не пишем/не шлём.
"""
from __future__ import annotations

import os
import json
import time
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

import notify

OP_SHEET = "1sHlFGSVB-7V8V4q6kvcTR1rrw19EaabIaOgrCHU0DHE"
TAB = "FBS"
MP = "https://marketplace-api.wildberries.ru"
CONTENT = "https://content-api.wildberries.ru"
OZ = "https://api-seller.ozon.ru"
START = datetime(2026, 8, 1, tzinfo=timezone(timedelta(hours=3)))  # начало периода
MSK = timezone(timedelta(hours=3))
FIRST_ROW = 65          # оба блока начинаются со строки 65 (владелец удалила лишние строки вверху 08.09)
WB_COL = 2              # блок WB со столбца B
OZ_COL = 9              # блок Ozon со столбца I
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


def _post(url, body, headers=None):
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {"Authorization": TOKEN})
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    with urllib.request.urlopen(req, context=CTX, timeout=90) as r:
        return json.load(r)


def _oz(path, body):
    return _post(OZ + path, body, headers={
        "Client-Id": os.environ["OZON_CLIENT_ID"].strip(),
        "Api-Key": os.environ["OZON_API_KEY"].strip(),
        "Content-Type": "application/json"})


# ---------- WB склады ----------
def wb_warehouses():
    wh = _get(f"{MP}/api/v3/warehouses")
    kazan = None
    for w in wh:
        if "казан" in (w.get("name") or "").lower():
            kazan = w
    return wh, kazan


# ---------- WB заказы по дням ----------
def wb_orders_by_day(date_to):
    """{'YYYY-MM-DD': {wid: units}} за [START, date_to). WB: 1 заказ = 1 шт."""
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


# ---------- Ozon FBS-отправления по дням ----------
def oz_orders_by_day(date_to):
    """({'YYYY-MM-DD': {wid: units}}, {wid: name}) за [START, date_to).
    Штуки = сумма quantity товаров в каждом FBS-отправлении."""
    data, names = {}, {}
    since = START.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to = date_to.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    offset = 0
    for _ in range(100):
        r = _oz("/v3/posting/fbs/list", {
            "dir": "asc", "filter": {"since": since, "to": to, "status": ""},
            "limit": 1000, "offset": offset,
            "with": {"analytics_data": False, "financial_data": False}})
        ps = r.get("result", {}).get("postings", [])
        for p in ps:
            ca = p.get("created_at") or p.get("in_process_at")
            if not ca:
                continue
            dt = datetime.fromisoformat(ca.replace("Z", "+00:00")).astimezone(MSK)
            if dt < START or dt >= date_to:
                continue
            ds = dt.strftime("%Y-%m-%d")
            dm = p.get("delivery_method", {}) or {}
            wid = dm.get("warehouse_id")
            names[wid] = dm.get("warehouse") or str(wid)
            units = sum(int(pr.get("quantity", 0) or 0) for pr in p.get("products", []))
            data.setdefault(ds, {})
            data[ds][wid] = data[ds].get(wid, 0) + units
        if len(ps) < 1000:
            break
        offset += 1000
        time.sleep(0.2)
    return data, names


# ---------- остаток FBS по Казани (WB) ----------
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


def end_stop():
    """Сегодня 00:00 МСК = граница (последний записанный день — вчера)."""
    return datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)


def build_block(day_data, columns, title, subtitle):
    """columns = [(wid, 'Имя'), ...]. Возвращает (rows, subtotal_rows, total_row).
    Строки строятся тем же периодом, что и второй блок → индексы совпадают."""
    end = end_stop()
    wids = [c[0] for c in columns]
    names = [c[1] for c in columns]
    ncol = len(columns)
    rows = [[title] + [""] * (ncol + 1),
            [subtitle] + [""] * (ncol + 1),
            ["Дата"] + names + ["Итого"]]
    days = []
    d = START
    while d < end:
        days.append(d); d += timedelta(days=1)
    subtotal_rows, totals = [], [0] * ncol
    msum = {}
    r_idx = FIRST_ROW + 3
    prev_month = None
    for d in days:
        if prev_month is not None and d.month != prev_month:
            vals = msum[prev_month]
            rows.append([f"Итого {MONTHS_RU[prev_month]}"] + vals + [sum(vals)])
            subtotal_rows.append(r_idx); r_idx += 1
        prev_month = d.month
        ds = d.strftime("%Y-%m-%d")
        wd = day_data.get(ds, {})
        vals = [wd.get(w, 0) if w is not None else 0 for w in wids]
        rows.append([f"{d:%d.%m.%Y} {WD[d.weekday()]}"] + vals + [sum(vals)])
        totals = [t + v for t, v in zip(totals, vals)]
        m = msum.get(d.month, [0] * ncol)
        msum[d.month] = [a + b for a, b in zip(m, vals)]
        r_idx += 1
    if prev_month is not None:
        vals = msum[prev_month]
        rows.append([f"Итого {MONTHS_RU[prev_month]}"] + vals + [sum(vals)])
        subtotal_rows.append(r_idx); r_idx += 1
    rows.append(["ВСЕГО за период"] + totals + [sum(totals)])
    return rows, subtotal_rows, r_idx, names


def write_block(sh, start_col, rows, subtotal_rows, total_row, names):
    ws = sh.worksheet(TAB)
    sid = ws.id
    ncols = len(names) + 2                      # Дата + склады + Итого
    last = FIRST_ROW + len(rows) - 1
    a1 = lambda r, c: rowcol_to_a1(r, c)
    # чистим прежний блок (с запасом вниз)
    ws.batch_clear([f"{a1(FIRST_ROW, start_col)}:{a1(last + 40, start_col + ncols - 1)}"])
    ws.update(f"{a1(FIRST_ROW, start_col)}:{a1(last, start_col + ncols - 1)}",
              rows, value_input_option="RAW")

    dark = {"red": 0.13, "green": 0.28, "blue": 0.53}
    blue = {"red": 0.2, "green": 0.4, "blue": 0.66}
    green = {"red": 0.85, "green": 0.91, "blue": 0.83}
    yellow = {"red": 0.99, "green": 0.85, "blue": 0.4}
    beige = {"red": 1, "green": 0.97, "blue": 0.88}
    white = {"red": 1, "green": 1, "blue": 1}
    grey = {"red": 0.35, "green": 0.35, "blue": 0.4}
    hdr = FIRST_ROW + 2
    c0, c1 = start_col, start_col + ncols - 1   # первая и последняя колонки блока

    def rng(r0, cc0, r1, cc1):
        return {"sheetId": sid, "startRowIndex": r0 - 1, "endRowIndex": r1,
                "startColumnIndex": cc0 - 1, "endColumnIndex": cc1}

    def cell(bg=None, bold=False, fs=10, color=None, halign=None, valign=None, italic=False):
        tf = {"fontSize": fs, "bold": bold, "italic": italic}
        if color: tf["foregroundColor"] = color
        cf = {"textFormat": tf}
        if bg: cf["backgroundColor"] = bg
        if halign: cf["horizontalAlignment"] = halign
        if valign: cf["verticalAlignment"] = valign
        return cf

    def fmt(r0, cc0, r1, cc1, cf, fields="userEnteredFormat"):
        return {"repeatCell": {"range": rng(r0, cc0, r1, cc1),
                               "cell": {"userEnteredFormat": cf}, "fields": fields}}

    reqs = [fmt(FIRST_ROW, c0, last, c1, cell(bg=white), "userEnteredFormat.backgroundColor")]
    reqs.append(fmt(FIRST_ROW, c0, FIRST_ROW, c1,
                    cell(bg=dark, bold=True, fs=12, color=white, halign="CENTER", valign="MIDDLE")))
    reqs.append(fmt(FIRST_ROW + 1, c0, FIRST_ROW + 1, c1,
                    cell(bg={"red": 0.9, "green": 0.93, "blue": 0.98}, fs=9, color=grey,
                         halign="CENTER", valign="MIDDLE", italic=True)))
    reqs.append(fmt(hdr, c0, hdr, c1, cell(bg=blue, bold=True, color=white, halign="CENTER", valign="MIDDLE")))
    reqs.append(fmt(hdr + 1, c0, last, c0, cell(halign="LEFT")))          # даты слева
    reqs.append(fmt(hdr + 1, c0 + 1, last, c1, cell(halign="CENTER")))    # числа по центру
    # колонка Казань (если есть) — бежевый фон
    for i, nm in enumerate(names):
        if nm == "Казань":
            kc = start_col + 1 + i
            reqs.append(fmt(hdr, kc, last - 1, kc, cell(bg=beige, halign="CENTER"),
                            "userEnteredFormat.backgroundColor"))
            reqs.append(fmt(hdr, kc, hdr, kc, cell(bg=blue, bold=True, color=white,
                                                   halign="CENTER", valign="MIDDLE")))
    reqs.append(fmt(hdr + 1, c1, last, c1, cell(bold=True), "userEnteredFormat.textFormat.bold"))
    for r in subtotal_rows:
        reqs.append(fmt(r, c0, r, c1, cell(bg=green, bold=True)))
    reqs.append(fmt(total_row, c0, total_row, c1, cell(bg=yellow, bold=True, fs=11)))
    reqs.append({"repeatCell": {"range": rng(hdr + 1, c0, last, c0),
                                "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                                "fields": "userEnteredFormat.numberFormat"}})
    solid = {"style": "SOLID", "color": {"red": 0.8, "green": 0.8, "blue": 0.8}}
    med = {"style": "SOLID_MEDIUM"}
    reqs.append({"updateBorders": {"range": rng(FIRST_ROW, c0, last, c1),
                                   "top": med, "bottom": med, "left": med, "right": med,
                                   "innerHorizontal": solid, "innerVertical": solid}})
    for rr in (FIRST_ROW, FIRST_ROW + 1):
        reqs.append({"mergeCells": {"range": rng(rr, c0, rr, c1), "mergeType": "MERGE_ALL"}})
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
    end = end_stop()
    stamp = f"обновлено {datetime.now(MSK):%d.%m.%Y %H:%M} МСК"

    # ---- WB ----
    _, kazan = wb_warehouses()
    kazan_id = kazan["id"] if kazan else st.get("kazan_id")
    wb_data = wb_orders_by_day(end)
    wb_cols = [(KRASNODAR, "Краснодар"), (SOFINO, "МП Карго Софьино"), (kazan_id, "Казань")]
    wb_rows, wb_subs, wb_total, _ = build_block(
        wb_data, wb_cols,
        "ЗАКАЗЫ FBS ПО ДНЯМ (ШТ) · WB",
        f"WB · с 01.08.2026 · {stamp} · источник: WB API /api/v3/orders")
    kz_orders = sum(wb_data.get(d, {}).get(kazan_id, 0) for d in wb_data) if kazan_id else 0

    # ---- Ozon ----
    oz_data, oz_names = oz_orders_by_day(end)
    oz_wids = sorted(oz_names.keys(), key=lambda x: (x is None, x))
    if not oz_wids:  # ещё не было ни одного FBS-отправления
        oz_cols = [(None, "FBS Краснодар")]
    else:
        oz_cols = [(w, oz_names[w]) for w in oz_wids]
    oz_rows, oz_subs, oz_total, _ = build_block(
        oz_data, oz_cols,
        "ЗАКАЗЫ FBS ПО ДНЯМ (ШТ) · OZON",
        f"Ozon · с 01.08.2026 · {stamp} · источник: Seller API /v3/posting/fbs/list")

    print(f"WB days→{(end - timedelta(days=1)):%d.%m.%Y}; Ozon складов: {[c[1] for c in oz_cols]}")

    if not DRY:
        sh = _sheet()
        write_block(sh, WB_COL, wb_rows, wb_subs, wb_total, [c[1] for c in wb_cols])
        write_block(sh, OZ_COL, oz_rows, oz_subs, oz_total, [c[1] for c in oz_cols])

    # ---------- алерты по Казани (WB) ----------
    alerts = []
    if kazan and not st.get("kazan_seen"):
        st["kazan_seen"] = True
        st["kazan_id"] = kazan["id"]
        alerts.append(f"<b>WB: в кабинете появился FBS-склад «{kazan['name']}».</b>\n"
                      f"Отслеживаю приход товара и заказы — колонка Казань в таблице FBS начнёт наполняться.")
    if kazan_id and not st.get("kazan_stock_alerted"):
        stock = kazan_stock(kazan_id)
        if stock and stock > 0:
            st["kazan_stock_alerted"] = True
            alerts.append(f"<b>Товар приехал на склад Казань (FBS): {stock} шт остатка.</b>\n"
                          f"Склад {kazan_id} готов к заказам.")
    if kazan_id and kz_orders > 0 and not st.get("kazan_order_alerted"):
        st["kazan_order_alerted"] = True
        alerts.append(f"<b>Первый FBS-заказ из Казани.</b>\n"
                      f"Всего по Казани уже {kz_orders} шт за период — колонка в таблице FBS обновлена.")
    for msg in alerts:
        print("ALERT:", msg.replace("\n", " / "))
        if not DRY:
            notify.send(msg); time.sleep(0.5)

    if not DRY:
        save_state(st)


if __name__ == "__main__":
    main()
