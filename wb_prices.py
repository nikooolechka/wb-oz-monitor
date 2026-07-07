"""Ежедневная выгрузка витринных цен WB → таблица «цены АС фарм».

4 цифры на артикул (регион — единый витринный, Москва):
  до СПП       — WB seller-API discounts-prices (discountedPrice)
  с СПП        — MPStats item.final_price
  с кошельком  — MPStats item.wallet_price

Пишет в spreadsheet PRICES_SHEET_ID:
  • Лист1 (СНИМОК) — WB-блок: по имени артикула в колонке B ставит C/D/E.
    Столбец F (формула СПП) и другие площадки не трогает. Оба прогона.
  • «история WB» (СВОДНАЯ МАТРИЦА) — только прогон 13:00. Дизайн владельца:
    A=Артикул(закр.) | B=серый столбец месяца (вертик.) | блоки дат по 3 столбца,
    свежая дата сразу за серым (C:E), старее — правее. При смене месяца
    прошедший месяц группируется+сворачивается, новый серый месяц — слева.
    Раскладка/стиль — см. память reference_prices_history_layout.

Источники — MPStats + WB seller-API (Scrapfly НЕ нужен). Облако (GitHub Actions).
Запись истории включается env PRICES_HIST_ENABLED=1.
Секреты: MPSTATS_TOKEN, WB_TOKEN, GSHEETS_SA_JSON, PRICES_SHEET_ID, TELEGRAM_*.
"""
from __future__ import annotations

import os
import ssl
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

MSK = timezone(timedelta(hours=3))
DRY = os.environ.get("DRY") == "1"
ALLOWED_MISS = 2
HIST_ENABLED = os.environ.get("PRICES_HIST_ENABLED", "") == "1"
SHEET_ID = os.environ.get("PRICES_SHEET_ID", "").strip()
SNAP_TAB = os.environ.get("PRICES_SNAPSHOT_TAB", "Лист1")
HIST_TAB = os.environ.get("PRICES_HISTORY_TAB", "история WB")

MP_TOKEN = os.environ.get("MPSTATS_TOKEN", "").strip()
WB_TOKEN = os.environ.get("WB_TOKEN", "").strip()
OZ_CID = os.environ.get("OZON_CLIENT_ID", "").strip()
OZ_KEY = os.environ.get("OZON_API_KEY", "").strip()
OZ_BANK_HEADER = "Цена с другими банками"   # колонка Ozon-блока Лист1, которую заполняем
OZ_ENABLED = os.environ.get("PRICES_OZ_ENABLED", "") == "1"  # Ozon включается только после сверки

# Список артикулов НЕ хардкодится — берётся из кабинета WB (vendorCode→nmID).
# Исключаем заведомо списанные/непрофильные (не добавлять их в Лист1/историю).
EXCLUDE = {"Dental100_Animal", "Lapomoyka_500", "Gel_peeling", "Spray_fresh_new",
           "men_spray", "makeup_30"}
SUBHEAD = ["до СПП", "с СПП", "с кошельком"]
MONTHS_RU = {1: "ЯНВАРЬ", 2: "ФЕВРАЛЬ", 3: "МАРТ", 4: "АПРЕЛЬ", 5: "МАЙ", 6: "ИЮНЬ",
             7: "ИЮЛЬ", 8: "АВГУСТ", 9: "СЕНТЯБРЬ", 10: "ОКТЯБРЬ", 11: "НОЯБРЬ", 12: "ДЕКАБРЬ"}

# стиль владельца (считан из листа)
HEADER_BG = {"red": 0.6, "green": 0.0, "blue": 1.0}
GRAYM_BG = {"red": 0.7176471, "green": 0.7176471, "blue": 0.7176471}
SUB_BG = {"red": 0.9294118, "green": 0.9294118, "blue": 0.9294118}
ART_BG = {"red": 0.8470588, "green": 0.9098039, "blue": 0.9568627}
WHITE = {"red": 1, "green": 1, "blue": 1}


def _ctx():
    return ssl._create_unverified_context()


def _get(url, headers, retries=3):
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers),
                    timeout=40, context=_ctx()) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(3 + 2 * i); continue
            break
        except Exception as e:
            last = e; time.sleep(2 + 2 * i)
    raise RuntimeError(f"{url[:50]} → {last}")


def cabinet():
    """{vendorCode: {'nm': nmID, 'do': цена продавца до СПП}} из кабинета WB."""
    d = _get("https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1000",
             {"Authorization": WB_TOKEN})
    m = {}
    for g in (d.get("data") or {}).get("listGoods") or []:
        vc = (g.get("vendorCode") or "").strip()
        dp = (g.get("sizes") or [{}])[0].get("discountedPrice")
        if vc:
            m[vc] = {"nm": g.get("nmID"), "do": round(dp) if dp else None}
    return m


def mpstats_prices(nm):
    d = _get(f"https://mpstats.io/api/wb/get/item/{nm}",
             {"X-Mpstats-TOKEN": MP_TOKEN, "Content-Type": "application/json"})
    it = (d or {}).get("item") or {}
    return it.get("final_price"), it.get("wallet_price")


# ---------- Ozon: «цена с картами других банков» = MPStats oz final_price ----------
def ozon_offer_to_sku(offer_ids):
    """{offer_id: sku (витринный)} через Ozon seller API. Резолвит SKU по имени
    артикула — новый артикул в Лист1 подхватывается сам."""
    body = json.dumps({"offer_id": list(offer_ids), "product_id": [], "sku": []}).encode()
    req = urllib.request.Request("https://api-seller.ozon.ru/v3/product/info/list",
        data=body, method="POST",
        headers={"Client-Id": OZ_CID, "Api-Key": OZ_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60, context=_ctx()) as r:
        d = json.loads(r.read())
    return {it.get("offer_id"): it.get("sku") for it in (d.get("items") or []) if it.get("sku")}


def mpstats_oz_final(sku):
    d = _get(f"https://mpstats.io/api/oz/get/item/{sku}",
             {"X-Mpstats-TOKEN": MP_TOKEN, "Content-Type": "application/json"})
    return ((d or {}).get("item") or {}).get("final_price")


def oz_bank_prices(oz_names):
    """{name: цена с картами других банков} по артикулам из Ozon-блока Лист1."""
    if not oz_names:
        return {}
    sku_map = ozon_offer_to_sku(oz_names)
    out = {}
    for name in oz_names:
        sku = sku_map.get(name)
        if not sku:
            continue
        try:
            fp = mpstats_oz_final(sku)
        except Exception as e:
            print(f"[prices] MPStats oz {name}: {e}", flush=True)
            fp = None
        if fp:
            out[name] = fp
        time.sleep(0.25)
    return out


def write_oz_bank(sh, oz_data):
    """Пишет «цену с другими банками» в Ozon-блок Лист1: по имени артикула в
    колонке H, в колонку с заголовком OZ_BANK_HEADER (ищем по шапке, не по букве)."""
    ws = sh.worksheet(SNAP_TAB)
    header = ws.row_values(1)
    kcol = None
    for i, h in enumerate(header, start=1):
        if (h or "").strip() == OZ_BANK_HEADER:
            kcol = i
            break
    if not kcol:
        print("[prices] колонка Ozon 'с другими банками' не найдена", flush=True)
        return 0
    col_h = ws.col_values(8)  # колонка H — артикулы Ozon-блока
    reqs = []
    for row_i, name in enumerate(col_h, start=1):
        key = (name or "").strip()
        if key in oz_data:
            a1 = rowcol_to_a1(row_i, kcol)
            reqs.append({"range": f"{SNAP_TAB}!{a1}", "values": [[oz_data[key]]]})
    if reqs:
        ws.spreadsheet.values_batch_update({"valueInputOption": "USER_ENTERED", "data": reqs})
    return len(reqs)


def collect(l1_names):
    """Отслеживаем РОВНО артикулы из Лист1 (колонка B, WB-блок) — контролируемый
    владельцем список. SKU резолвим авто из кабинета по vendorCode. Новый артикул,
    добавленный в Лист1, подхватывается сам (по нему парсятся цены + строка в истории).
    Возвращает (data{name:(до,спп,кошелёк)}, misses)."""
    cab = cabinet()
    data, misses = {}, []
    for name in l1_names:
        if name in EXCLUDE or name not in cab:
            continue
        info = cab[name]
        try:
            sp, wp = mpstats_prices(info["nm"])
        except Exception as e:
            print(f"[prices] MPStats {name}: {e}", flush=True)
            sp = wp = None
        do = info["do"]
        if sp is None or do is None:
            misses.append(name)
        data[name] = (do, sp, wp)
        time.sleep(0.25)
    return data, misses


def _open():
    info = json.loads(os.environ["GSHEETS_SA_JSON"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def write_snapshot(sh, data):
    """Обновляет C/D/E по имени артикула в колонке B. F (формула СПП) и другие
    площадки не трогает. Новый артикул, добавленный владельцем в B, получит цены."""
    ws = sh.worksheet(SNAP_TAB)
    col_b = ws.col_values(2)
    reqs, written = [], 0
    for row_i, name in enumerate(col_b, start=1):
        key = (name or "").strip()
        if key in data:
            b, s, w = data[key]
            reqs.append({"range": f"{SNAP_TAB}!C{row_i}:E{row_i}", "values": [[b, s, w]]})
            written += 1
    if reqs:
        ws.spreadsheet.values_batch_update(
            {"valueInputOption": "USER_ENTERED", "data": reqs})
    return written


# ---------- история ----------
def _article_rows(ws):
    """Имена артикулов из колонки A начиная со строки 3 (без хвостовых пустых)."""
    col_a = ws.col_values(1)
    names = [(col_a[i] or "").strip() for i in range(2, len(col_a))]
    while names and names[-1] == "":
        names.pop()
    return names


def _setup_month_col(ws, sid, label, last_row):
    """Оформляет серый столбец месяца B (после вставки пустого столбца в индекс 1)."""
    ws.update([[label]], "B1", value_input_option="RAW")
    ws.spreadsheet.batch_update({"requests": [
        {"mergeCells": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 2,
            "startColumnIndex": 1, "endColumnIndex": 2}, "mergeType": "MERGE_ALL"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": last_row,
            "startColumnIndex": 1, "endColumnIndex": 2},
            "cell": {"userEnteredFormat": {"backgroundColor": GRAYM_BG, "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE", "textFormat": {"bold": True, "fontSize": 7},
                "textRotation": {"vertical": True}}},
            "fields": "userEnteredFormat"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
            "startIndex": 1, "endIndex": 2}, "properties": {"pixelSize": 24}, "fields": "pixelSize"}},
    ]})


def _fmt_date_block(ws, sid, last_row):
    """Оформляет блок свежей даты — он всегда в столбцах C:E (индексы 2..4)."""
    ws.spreadsheet.batch_update({"requests": [
        {"mergeCells": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 2, "endColumnIndex": 5}, "mergeType": "MERGE_ALL"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 2, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {"backgroundColor": HEADER_BG, "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE", "textFormat": {"bold": True, "foregroundColor": WHITE}}},
            "fields": "userEnteredFormat"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2,
            "startColumnIndex": 2, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"backgroundColor": SUB_BG, "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE", "textFormat": {"bold": True}, "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2,
            "startColumnIndex": 4, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {"backgroundColor": SUB_BG, "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE", "textFormat": {"bold": True, "fontSize": 8}, "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 2, "endRowIndex": last_row,
            "startColumnIndex": 2, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
            "startIndex": 2, "endIndex": 5}, "properties": {"pixelSize": 73}, "fields": "pixelSize"}},
    ]})


def push_history_column(sh, data, now):
    ws = sh.worksheet(HIST_TAB)
    sid = ws.id
    names = _article_rows(ws)
    # синхронизация артикулов: дописать новые (которых ещё нет в истории)
    missing = [n for n in data if n not in set(names)]
    if missing:
        start = 3 + len(names)
        ws.update([[n] for n in missing], f"A{start}", value_input_option="RAW")
        ws.spreadsheet.batch_update({"requests": [
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": start - 1,
                "endRowIndex": start - 1 + len(missing), "startColumnIndex": 0, "endColumnIndex": 1},
                "cell": {"userEnteredFormat": {"backgroundColor": ART_BG, "verticalAlignment": "MIDDLE",
                    "textFormat": {"bold": True}}}, "fields": "userEnteredFormat"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": start - 1,
                "endRowIndex": start - 1 + len(missing), "startColumnIndex": 1, "endColumnIndex": 2},
                "cell": {"userEnteredFormat": {"backgroundColor": GRAYM_BG}}, "fields": "userEnteredFormat"}},
        ]})
        names = names + missing
    last_row = 2 + len(names)
    cur_label = (ws.acell("B1").value or "").strip().upper()
    new_label = MONTHS_RU[now.month]
    month_changed = bool(cur_label) and cur_label != new_label

    if month_changed:
        # 1) группируем + сворачиваем столбцы-даты прошедшего месяца (C..последний с датой)
        row2 = ws.row_values(2)
        lastcol = len(row2)   # 1-based кол-во заполненных ячеек в строке подшапки
        if lastcol >= 3:
            grp = {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 2, "endIndex": lastcol}
            ws.spreadsheet.batch_update({"requests": [{"addDimensionGroup": {"range": grp}}]})
            ws.spreadsheet.batch_update({"requests": [{"updateDimensionGroup": {
                "dimensionGroup": {"range": grp, "depth": 1, "collapsed": True}, "fields": "collapsed"}}]})
        # 2) новый серый столбец месяца — слева (индекс 1)
        ws.spreadsheet.batch_update({"requests": [{"insertDimension": {"range": {
            "sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "inheritFromBefore": False}}]})
        _setup_month_col(ws, sid, new_label, last_row)

    # блок свежей даты — 3 столбца в индекс 2 (C), сразу за серым столбцом месяца
    ws.spreadsheet.batch_update({"requests": [{"insertDimension": {"range": {
        "sheetId": sid, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 5},
        "inheritFromBefore": False}}]})
    date = now.strftime("%d.%m.%Y")
    ws.update([[date]], "C1", value_input_option="RAW")
    ws.update([SUBHEAD], "C2")
    vals = [list(data.get(nm, ("", "", ""))) for nm in names]
    ws.update(vals, f"C3:E{last_row}", value_input_option="USER_ENTERED")
    _fmt_date_block(ws, sid, last_row)


def _alert(msg):
    if DRY:
        print("[prices][DRY-ALERT] " + msg, flush=True); return
    try:
        import notify
        notify.send(msg)
    except Exception as e:
        print(f"[prices] алерт не ушёл: {e}", flush=True)


def run():
    now = datetime.now(MSK).replace(microsecond=0)
    run_label = "07:00" if now.hour < 11 else "13:00"
    if not MP_TOKEN or not WB_TOKEN:
        raise RuntimeError("нет MPSTATS_TOKEN / WB_TOKEN")

    if not SHEET_ID:
        raise RuntimeError("нет PRICES_SHEET_ID")
    sh = _open()
    l1 = sh.worksheet(SNAP_TAB)
    l1_names = {(v or "").strip() for v in l1.col_values(2) if (v or "").strip()}

    data, misses = collect(l1_names)
    got = sum(1 for v in data.values() if v[1] is not None)
    print(f"[prices] {run_label}: собрано {got}/{len(data)}; без цены: {misses}", flush=True)

    if DRY:
        print("[prices][DRY] запись пропущена", flush=True); return

    try:
        w = write_snapshot(sh, data)
        # Ozon: «цена с картами других банков» (Лист1-driven — ловит новые артикулы из колонки H)
        if OZ_CID and OZ_KEY and OZ_ENABLED:
            try:
                oz_names = {(v or "").strip() for v in l1.col_values(8)[1:]
                            if (v or "").strip() and (v or "").strip() not in EXCLUDE
                            and (v or "").strip() != "озон"}
                oz_data = oz_bank_prices(oz_names)
                kw = write_oz_bank(sh, oz_data)
                print(f"[prices] Ozon 'с другими банками' обновлено: {kw}", flush=True)
            except Exception as e:
                print(f"[prices] Ozon пропущен (не критично): {e}", flush=True)
        if run_label == "13:00" and HIST_ENABLED:
            push_history_column(sh, data, now)
            print(f"[prices] снимок {w} артик.; в историю добавлен столбец {now:%d.%m.%Y}", flush=True)
        elif run_label == "13:00":
            print(f"[prices] снимок {w} артик.; история на ПАУЗЕ (PRICES_HIST_ENABLED!=1)", flush=True)
        else:
            print(f"[prices] снимок {w} артик.; 07:00 — историю не трогаю", flush=True)
    except Exception as e:
        print(f"[prices] ОШИБКА записи: {e}", flush=True)
        _alert(f"⚠️ <b>Цены WB: ошибка записи в таблицу.</b>\nПрогон {run_label}. {str(e)[:200]}")
        raise

    if len(misses) > ALLOWED_MISS:
        _alert(f"⚠️ <b>Цены WB: собрано только {got}/{len(data)}.</b>\n"
               f"Прогон {run_label}. Без цены: {', '.join(misses)}.")


if __name__ == "__main__":
    run()
