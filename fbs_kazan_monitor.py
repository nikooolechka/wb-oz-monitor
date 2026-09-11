#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Монитор FBS-заказов по складам (по дням) + артикульная разбивка, WB + Ozon.

Каждый прогон (ежедневно, УТРОМ по МСК; запуск с Яндекс-таймера prices-trigger,
крон GitHub — дублёр):
  1. WB: FBS-склады (/api/v3/warehouses) — Краснодар 1884480, МП Карго Софьино
     2033332, Казань (по «казан» в названии). Заказы /api/v3/orders: по дням,
     складам И артикулам (article), 1 заказ = 1 шт.
  2. Ozon: FBS-отправления /v3/posting/fbs/list: по дням, складам и артикулам
     (offer_id), штуки = сумма quantity.
  3. Пишет во вкладку FBS (ОП АС Фарм) со строки 65: два блока рядом —
     WB (B: Дата/Краснодар/Софьино/Казань/Итого), Ozon (I: Дата/склады/Итого).
     Период 01.08.2026 → ВЧЕРА (сегодня неполный не пишем).
     ВЛОЖЕННЫЕ ГРУППЫ (плюсики): месяц ▸ дни ▸ артикулы. По умолчанию ВСЁ
     свёрнуто до месяцев (видны итоги месяцев + ВСЕГО). Разбивка по артикулам —
     ОБЩИЙ список на дату для обоих маркетов, вписываются только проданные.
     ⚠️ Группы трогаем ТОЛЬКО в своей зоне (строки >= 65) — верхний блок владельца
     (группы на строках 2–33 и 36–63) НЕ трогать.
  4. Ловит приход товара в Казань → алерт в канал «АС Фарм изменения» (дедуп).

Секреты: WB_TOKEN, OZON_CLIENT_ID, OZON_API_KEY, GSHEETS_SA_JSON,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. DRY=1 — только лог.
"""
from __future__ import annotations

import os, json, time, ssl, re, urllib.request
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
START = datetime(2026, 8, 1, tzinfo=timezone(timedelta(hours=3)))
MSK = timezone(timedelta(hours=3))
FIRST_ROW = 65
MYZONE_START0 = 64          # 0-индекс: группы трогаем только начиная с этой строки (строка 65)
WB_COL = 2                  # B
OZ_COL = 9                  # I
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


def wb_wh_list():
    """Все FBS-склады продавца WB динамически. Порядок: Краснодар, Софьино, затем новые по id.
    Появился новый склад → он сам станет колонкой (Ozon-блок сдвинется правее)."""
    wh = _get(f"{MP}/api/v3/warehouses")
    order = {KRASNODAR: 0, SOFINO: 1}
    wh = sorted(wh, key=lambda w: (order.get(w.get("id"), 2), w.get("id") or 0))
    return [(w["id"], (w.get("name") or str(w.get("id")))) for w in wh]


def end_stop():
    return datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------- WB заказы: по дням / складам / артикулам ----------
def wb_detailed(date_to):
    """data[ds][article][wid] = шт (1 заказ = 1 шт)."""
    data, seen = {}, set()
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
                art = (o.get("article") or str(o.get("nmId") or "?")).strip()
                wid = o.get("warehouseId")
                data.setdefault(ds, {}).setdefault(art, {})
                data[ds][art][wid] = data[ds][art].get(wid, 0) + 1
            nxt = d.get("next", 0)
            if not orders or not nxt:
                break
            time.sleep(0.25)
        cur = cur + timedelta(days=25)
    return data


# ---------- Ozon отправления: по дням / складам / артикулам ----------
def oz_detailed(date_to):
    """(data[ds][article][wid]=шт, {wid:name})."""
    data, names = {}, {}
    since = START.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to = date_to.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    offset = 0
    for _ in range(100):
        r = _oz("/v3/posting/fbs/list", {
            "dir": "asc", "filter": {"since": since, "to": to, "status": ""},
            "limit": 1000, "offset": offset, "with": {"analytics_data": False, "financial_data": False}})
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
            for pr in p.get("products", []):
                art = (pr.get("offer_id") or "?").strip()
                q = int(pr.get("quantity", 0) or 0)
                data.setdefault(ds, {}).setdefault(art, {})
                data[ds][art][wid] = data[ds][art].get(wid, 0) + q
        if len(ps) < 1000:
            break
        offset += 1000
        time.sleep(0.2)
    return data, names


def kazan_stock(kazan_id):
    try:
        barcodes = []
        cursor = {"limit": 1000}
        for _ in range(30):
            d = _post(f"{CONTENT}/content/v2/get/cards/list",
                      {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}})
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
            st = _post(f"{MP}/api/v3/stocks/{kazan_id}", {"skus": barcodes[i:i + 1000]})
            for s in st.get("stocks", []):
                total += s.get("amount", 0) or 0
            time.sleep(0.3)
        return total
    except Exception as e:
        print("stock check failed:", e)
        return None


def _norm(a):
    return re.sub(r"[^a-z0-9]", "", (a or "").lower())


def _sheet():
    info = json.loads(os.environ["GSHEETS_SA_JSON"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(OP_SHEET)


def build(wb_data, oz_data, wb_whs, oz_wids, oz_names, stamp):
    """Строит матрицу + группы. Колонки WB динамические, блок Ozon сдвигается за WB.
    Раскладка: B=Дата, склады WB.., Итого(wb_last); разрыв 2 стб (WB-фиол./Ozon-син.);
    OZW=Дата Ozon, склады Ozon.., Итого(oz_last)."""
    end = end_stop()
    wb_names = [n for _, n in wb_whs]
    ncols_wb = len(wb_whs)
    wb_last = 2 + ncols_wb + 1                 # столбец «Итого» ВБ
    OZW = wb_last + 3                          # 2 столбца-разрыва, затем «Дата» Ozon
    n_oz = max(1, len(oz_wids))
    oz_list = oz_wids if oz_wids else [None]
    oz_wnames = [oz_names.get(w, "FBS Краснодар") for w in oz_list]
    oz_last = OZW + n_oz + 1                    # столбец «Итого» Ozon
    W = oz_last - 1                             # число ячеек в строке (B..oz_last)

    def blank_row():
        return [""] * W

    def put(row, col1, val):                   # col1 — 1-индекс листа; B=2 → индекс 0
        row[col1 - 2] = val

    def wb_cells(row, label, per_wh, total):
        put(row, 2, label)
        for i, (wid, _) in enumerate(wb_whs):
            put(row, 3 + i, per_wh.get(wid, 0) if per_wh else 0)
        put(row, wb_last, total)
    def oz_cells(row, label, per_wh, total):
        put(row, OZW, label)
        for i, wid in enumerate(oz_list):
            put(row, OZW + 1 + i, (per_wh.get(wid, 0) if per_wh else 0))
        put(row, oz_last, total)

    # шапка
    rows = []
    hdr_title = blank_row()
    put(hdr_title, 2, "ВБ · заказы FBS по дням (шт)")
    put(hdr_title, wb_last + 2, "OZON · заказы FBS по дням (шт)")   # столбец Ozon-заголовка (после WB-разрыва)
    rows.append(hdr_title)
    sub = blank_row(); put(sub, 2, f"с 01.08.2026 · {stamp} · плюсики: месяц ▸ дни ▸ артикулы")
    rows.append(sub)
    head = blank_row()
    put(head, 2, "Дата")
    for i, n in enumerate(wb_names): put(head, 3 + i, n)
    put(head, wb_last, "Итого")
    put(head, OZW, "Дата")
    for i, n in enumerate(oz_wnames): put(head, OZW + 1 + i, n)
    put(head, oz_last, "Итого")
    rows.append(head)

    days = []
    d = START
    while d < end:
        days.append(d); d += timedelta(days=1)
    # дни по месяцам (порядок сохраняется)
    months = []
    for d in days:
        key = (d.year, d.month)
        if not months or months[-1][0] != key:
            months.append((key, []))
        months[-1][1].append(d)

    def day_totals(ds):
        wb_day = wb_data.get(ds, {}); oz_day = oz_data.get(ds, {})
        wb_t = {wid: 0 for wid, _ in wb_whs}
        for art, perwh in wb_day.items():
            for wid, q in perwh.items():
                if wid in wb_t: wb_t[wid] += q
        oz_t = {wid: 0 for wid in oz_list}
        for art, perwh in oz_day.items():
            for wid, q in perwh.items():
                if wid in oz_t: oz_t[wid] += q
        return wb_day, oz_day, wb_t, oz_t

    def article_rows(wb_day, oz_day):
        keys = {}
        for art in wb_day: keys.setdefault(_norm(art), art)
        for art in oz_day: keys.setdefault(_norm(art), art)
        out = []
        for k, disp in sorted(keys.items(), key=lambda kv: kv[1].lower()):
            arow = blank_row()
            wb_perwh = {}; wt = 0
            for art, perwh in wb_day.items():
                if _norm(art) == k:
                    for wid, q in perwh.items():
                        wb_perwh[wid] = wb_perwh.get(wid, 0) + q; wt += q
            oz_perwh = {}; ot = 0
            for art, perwh in oz_day.items():
                if _norm(art) == k:
                    for wid, q in perwh.items():
                        oz_perwh[wid] = oz_perwh.get(wid, 0) + q; ot += q
            put(arow, 2, disp)
            for i, (wid, _) in enumerate(wb_whs):
                v = wb_perwh.get(wid, 0); put(arow, 3 + i, v if v else "")
            put(arow, 2 + ncols_wb + 1, wt or "")
            if ot > 0:
                put(arow, OZW, disp)
                for i, wid in enumerate(oz_list):
                    v = oz_perwh.get(wid, 0); put(arow, OZW + 1 + i, v if v else "")
                put(arow, OZW + 1 + n_oz, ot or "")
            out.append(arow)
        return out

    groups = []
    subtotal_rows = []
    day_rows_meta = []
    grand = {"wb": [0] * ncols_wb, "oz": [0] * n_oz}
    r = FIRST_ROW + 3

    for (yy, mm), mdays in months:
        # итоги месяца считаем заранее — строку «Итого месяц» пишем СВЕРХУ
        m_wb = [0] * ncols_wb; m_oz = [0] * n_oz
        prepared = []
        for d in mdays:
            ds = d.strftime("%Y-%m-%d")
            wb_day, oz_day, wb_t, oz_t = day_totals(ds)
            for i, (wid, _) in enumerate(wb_whs):
                m_wb[i] += wb_t.get(wid, 0); grand["wb"][i] += wb_t.get(wid, 0)
            for i, wid in enumerate(oz_list):
                m_oz[i] += oz_t.get(wid, 0); grand["oz"][i] += oz_t.get(wid, 0)
            prepared.append((d, wb_day, oz_day, wb_t, oz_t))
        # СТРОКА ИТОГ МЕСЯЦА — сверху (на ней будет плюсик, раскрывающий дни ниже)
        mrow = blank_row()
        wb_cells(mrow, f"Итого {MONTHS_RU[mm]}", {wb_whs[i][0]: m_wb[i] for i in range(ncols_wb)}, sum(m_wb))
        oz_cells(mrow, f"Итого {MONTHS_RU[mm]}", {oz_list[i]: m_oz[i] for i in range(n_oz)}, sum(m_oz))
        rows.append(mrow); subtotal_rows.append(r); r += 1
        month_start = r
        # ДНИ месяца — ниже итога
        for (d, wb_day, oz_day, wb_t, oz_t) in prepared:
            drow = blank_row()
            wb_cells(drow, f"{d:%d.%m.%Y} {WD[d.weekday()]}", wb_t, sum(wb_t.values()))
            oz_cells(drow, f"{d:%d.%m.%Y} {WD[d.weekday()]}", oz_t, sum(oz_t.values()))
            rows.append(drow); day_rows_meta.append(("day", r)); r += 1
            art_start = r
            for arow in article_rows(wb_day, oz_day):
                rows.append(arow); day_rows_meta.append(("art", r)); r += 1
            art_end = r - 1
            if art_end >= art_start:                      # артикулы дня — ниже даты
                groups.append({"start": art_start, "end": art_end, "depth": 2, "collapsed": True})
        month_end = r - 1
        if month_end >= month_start:                      # дни месяца — ниже итога месяца
            groups.append({"start": month_start, "end": month_end, "depth": 1, "collapsed": True})

    # ВСЕГО — внизу, не в группе
    grow = blank_row()
    wb_cells(grow, "ВСЕГО за период", {wb_whs[i][0]: grand["wb"][i] for i in range(ncols_wb)}, sum(grand["wb"]))
    oz_cells(grow, "ВСЕГО за период", {oz_list[i]: grand["oz"][i] for i in range(n_oz)}, sum(grand["oz"]))
    rows.append(grow); total_row = r
    meta = {"header": FIRST_ROW + 2, "subtotals": subtotal_rows, "total": total_row,
            "day_rows": [rr for t, rr in day_rows_meta if t == "day"],
            "art_rows": [rr for t, rr in day_rows_meta if t == "art"],
            "last_col": oz_last, "oz_col": OZW, "wb_last": wb_last,
            "ncols_wb": ncols_wb, "n_oz": n_oz}
    return rows, groups, meta


def clear_my_groups(sh, sheet_id):
    """Надёжно удаляет ВСЕ группы строк в моей зоне (startIndex>=64), верхние не трогает.
    За проход сносим все группы самого глубокого уровня, повторяем пока не пусто."""
    for _ in range(60):
        meta = sh.fetch_sheet_metadata({"fields": "sheets(properties(sheetId),rowGroups(range,depth))"})
        mine = []
        for sh_ in meta.get("sheets", []):
            if sh_["properties"]["sheetId"] == sheet_id:
                mine = [g for g in sh_.get("rowGroups", []) if g["range"].get("startIndex", 0) >= MYZONE_START0]
        if not mine:
            return
        maxd = max(g.get("depth", 1) for g in mine)
        reqs = [{"deleteDimensionGroup": {"range": g["range"]}} for g in mine if g.get("depth", 1) == maxd]
        sh.batch_update({"requests": reqs})


def write_table(sh, rows, groups, meta):
    ws = sh.worksheet(TAB)
    sid = ws.id
    last = FIRST_ROW + len(rows) - 1
    lastcol = meta["last_col"]
    a = lambda rr, cc: rowcol_to_a1(rr, cc)
    # 1) снять мои группы (иначе при сжатии строк собьются)
    clear_my_groups(sh, sid)
    # 2) чистим зону и пишем значения
    # чистим с ЗАПАСОМ по ширине (до P) — на случай, если раньше блок был шире (сменилось число складов)
    ws.batch_clear([f"{a(FIRST_ROW, 2)}:{a(last + 400, max(lastcol, 16))}"])
    ws.update(f"{a(FIRST_ROW, 2)}:{a(last, lastcol)}", rows, value_input_option="RAW")

    dark = {"red": 0.13, "green": 0.28, "blue": 0.53}
    blue = {"red": 0.2, "green": 0.4, "blue": 0.66}
    green = {"red": 0.85, "green": 0.91, "blue": 0.83}
    yellow = {"red": 0.99, "green": 0.85, "blue": 0.4}
    beige = {"red": 1, "green": 0.97, "blue": 0.88}
    white = {"red": 1, "green": 1, "blue": 1}
    grey = {"red": 0.4, "green": 0.4, "blue": 0.45}
    hdr = meta["header"]

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
        return {"repeatCell": {"range": rng(r0, c0, r1, c1), "cell": {"userEnteredFormat": cf}, "fields": fields}}

    black = {"red": 0, "green": 0, "blue": 0}

    def rc(r0, c0, r1, c1, uef, fields):
        return {"repeatCell": {"range": rng(r0, c0, r1, c1), "cell": {"userEnteredFormat": uef}, "fields": fields}}

    B, L = 2, lastcol
    body0 = hdr + 1
    wb_num_last = meta["wb_last"]              # столбец «Итого» ВБ
    oz_col = meta["oz_col"]                    # столбец «Дата» Ozon
    oz_num_first = oz_col + 1                  # первый склад Ozon
    purple = {"red": 0.42, "green": 0.32, "blue": 0.62}   # ВБ
    GAPW = wb_num_last + 1   # столбец-разрыв со стороны ВБ (фиолетовый); дальше синий Ozon
    reqs = [rc(FIRST_ROW, B, last, L, {"backgroundColor": white}, "userEnteredFormat.backgroundColor")]
    # ЗАГОЛОВКИ БЛОКОВ: ВБ фиолетовый (B..G), ОЗОН синий (H..L)
    reqs.append(fmt(FIRST_ROW, B, FIRST_ROW, GAPW, cell(bg=purple, bold=True, fs=12, color=white, halign="CENTER", valign="MIDDLE")))
    reqs.append(fmt(FIRST_ROW, GAPW + 1, FIRST_ROW, L, cell(bg=blue, bold=True, fs=12, color=white, halign="CENTER", valign="MIDDLE")))
    reqs.append(fmt(FIRST_ROW + 1, B, FIRST_ROW + 1, L, cell(bg={"red": 0.9, "green": 0.93, "blue": 0.98}, fs=9, color=grey, halign="CENTER", valign="MIDDLE", italic=True)))
    # шапка колонок: ВБ фиолетовая (B..G), ОЗОН синяя (H..L)
    reqs.append(fmt(hdr, B, hdr, GAPW, cell(bg=purple, bold=True, color=white, halign="CENTER", valign="MIDDLE")))
    reqs.append(fmt(hdr, GAPW + 1, hdr, L, cell(bg=blue, bold=True, color=white, halign="CENTER", valign="MIDDLE")))
    # --- ТЕЛО: точечные поля, чтобы стили не затирали друг друга ---
    # база: артикульные строки мельче/серее (потом строки-итоги дней вернём к 10/чёрному)
    reqs.append(rc(body0, B, last, L, {"textFormat": {"fontSize": 9, "foregroundColor": grey}},
                   "userEnteredFormat.textFormat.fontSize,userEnteredFormat.textFormat.foregroundColor"))
    # ВЫРАВНИВАНИЕ: метки (даты/артикулы) в колонках B и I — по левому краю; все числа — по центру
    reqs.append(rc(body0, B, last, B, {"horizontalAlignment": "LEFT"}, "userEnteredFormat.horizontalAlignment"))
    reqs.append(rc(body0, oz_col, last, oz_col, {"horizontalAlignment": "LEFT"}, "userEnteredFormat.horizontalAlignment"))
    reqs.append(rc(body0, 3, last, wb_num_last, {"horizontalAlignment": "CENTER"}, "userEnteredFormat.horizontalAlignment"))
    reqs.append(rc(body0, oz_num_first, last, L, {"horizontalAlignment": "CENTER"}, "userEnteredFormat.horizontalAlignment"))
    # строки-итоги ДНЕЙ: обычный размер 10, чёрный (перекрываем «мелкий серый» базы)
    for rr in meta["day_rows"]:
        reqs.append(rc(rr, B, rr, L, {"textFormat": {"fontSize": 10, "foregroundColor": black}},
                       "userEnteredFormat.textFormat.fontSize,userEnteredFormat.textFormat.foregroundColor"))
    # итоги месяцев — зелёный фон + жирный + 10/чёрный
    for rr in meta["subtotals"]:
        reqs.append(rc(rr, B, rr, L, {"backgroundColor": green}, "userEnteredFormat.backgroundColor"))
        reqs.append(rc(rr, B, rr, L, {"textFormat": {"bold": True, "fontSize": 10, "foregroundColor": black}},
                       "userEnteredFormat.textFormat.bold,userEnteredFormat.textFormat.fontSize,userEnteredFormat.textFormat.foregroundColor"))
    # ВСЕГО — жёлтый + жирный + 11/чёрный
    tr = meta["total"]
    reqs.append(rc(tr, B, tr, L, {"backgroundColor": yellow}, "userEnteredFormat.backgroundColor"))
    reqs.append(rc(tr, B, tr, L, {"textFormat": {"bold": True, "fontSize": 11, "foregroundColor": black}},
                   "userEnteredFormat.textFormat.bold,userEnteredFormat.textFormat.fontSize,userEnteredFormat.textFormat.foregroundColor"))
    # даты/метки — текстовый формат (колонки B и I), чтобы не превращались в числа
    reqs.append(rc(body0, B, last, B, {"numberFormat": {"type": "TEXT"}}, "userEnteredFormat.numberFormat"))
    reqs.append(rc(body0, oz_col, last, oz_col, {"numberFormat": {"type": "TEXT"}}, "userEnteredFormat.numberFormat"))
    # границы всего блока
    solid = {"style": "SOLID", "color": {"red": 0.8, "green": 0.8, "blue": 0.8}}
    med = {"style": "SOLID_MEDIUM"}
    reqs.append({"updateBorders": {"range": rng(FIRST_ROW, B, last, L), "top": med, "bottom": med, "left": med, "right": med, "innerHorizontal": solid, "innerVertical": solid}})
    # объединения шапки: снять старые, затем ВБ-заголовок (B..F) и ОЗОН-заголовок (I..L) отдельно, подзаголовок — на всю ширину
    reqs.append({"unmergeCells": {"range": rng(FIRST_ROW, B, FIRST_ROW + 1, 16)}})   # с запасом (до P), чтобы покрыть старые объединения при смене ширины
    reqs.append({"mergeCells": {"range": rng(FIRST_ROW, B, FIRST_ROW, wb_num_last), "mergeType": "MERGE_ALL"}})   # ВБ-заголовок B:F
    reqs.append({"mergeCells": {"range": rng(FIRST_ROW, GAPW + 1, FIRST_ROW, L), "mergeType": "MERGE_ALL"}})       # OZON-заголовок H:L
    reqs.append({"mergeCells": {"range": rng(FIRST_ROW + 1, B, FIRST_ROW + 1, L), "mergeType": "MERGE_ALL"}})      # подзаголовок B:L
    sh.batch_update({"requests": reqs})

    # 3) группы (плюсики): создаём, потом сворачиваем по ФАКТИЧЕСКИМ range+depth
    if groups:
        addr = [{"addDimensionGroup": {"range": {"sheetId": sid, "dimension": "ROWS",
                 "startIndex": g["start"] - 1, "endIndex": g["end"]}}} for g in groups]
        sh.batch_update({"requests": addr})
        # Google сам переназначает depth при вложении → перечитываем реальные группы моей зоны
        time.sleep(0.6)
        m2 = sh.fetch_sheet_metadata({"fields": "sheets(properties(sheetId),rowGroups(range,depth))"})
        mine = []
        for s_ in m2.get("sheets", []):
            if s_["properties"]["sheetId"] == sid:
                mine = [g for g in s_.get("rowGroups", []) if g["range"].get("startIndex", 0) >= MYZONE_START0]
        upd = [{"updateDimensionGroup": {"dimensionGroup": {"range": g["range"], "depth": g["depth"],
                "collapsed": True}, "fields": "collapsed"}} for g in mine]
        if upd:
            sh.batch_update({"requests": upd})


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(st):
    os.makedirs("data", exist_ok=True)
    json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False, indent=2)


def main():
    end = end_stop()
    stamp = f"обновлено {datetime.now(MSK):%d.%m.%Y %H:%M} МСК"
    wb_whs = wb_wh_list()                        # ВСЕ FBS-склады WB динамически
    wb_data = wb_detailed(end)
    oz_data, oz_names = oz_detailed(end)
    oz_wids = sorted([w for w in oz_names.keys()], key=lambda x: (x is None, x))
    rows, groups, meta = build(wb_data, oz_data, wb_whs, oz_wids, oz_names, stamp)
    print(f"строк: {len(rows)} | групп: {len(groups)} | WB склады: {[n for _, n in wb_whs]} | "
          f"Ozon: {[oz_names[w] for w in oz_wids] or ['FBS Краснодар']}")
    if not DRY:
        write_table(_sheet(), rows, groups, meta)
    # Алерты о приходе товара (Казань и любой новый склад) вынесены в отдельный автомат «Приходы».


if __name__ == "__main__":
    main()
