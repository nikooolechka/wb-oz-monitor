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

ACTIVE = [
    ("Dental_100", 205348527), ("Dental_100_zemlyanika", 583154383),
    ("Dental_100_banan", 583155047), ("Dental_40", 140759945),
    ("Dental_40_zemlyanika", 860793985), ("Dental_40_natural", 892991707),
    ("Dental20", 76952248), ("Dental_20_zemlyanika", 860789726),
    ("Dental50", 140595726), ("Irrigator_500", 227067968),
    ("Irrigator_1000", 363137625), ("CrioGel1l", 93054004),
    ("CrioGel_5", 388153628), ("cryolipolysis25", 76942273),
    ("Cryolipolysis50", 87180591), ("Crio_L25(new)", 97076035),
    ("CrioL50", 144662550), ("OptikaSpray_new", 206024627),
    ("Zub_pasta_det", 917665198), ("Oral_cherry", 1055320329),
    ("spraydlyapolostyrta", 349314212),
]
SUBHEAD = ["до СПП", "с СПП", "с кошельком"]
MONTHS_RU = {1: "ЯНВАРЬ", 2: "ФЕВРАЛЬ", 3: "МАРТ", 4: "АПРЕЛЬ", 5: "МАЙ", 6: "ИЮНЬ",
             7: "ИЮЛЬ", 8: "АВГУСТ", 9: "СЕНТЯБРЬ", 10: "ОКТЯБРЬ", 11: "НОЯБРЬ", 12: "ДЕКАБРЬ"}

# стиль владельца (считан из листа)
HEADER_BG = {"red": 0.6, "green": 0.0, "blue": 1.0}
GRAYM_BG = {"red": 0.7176471, "green": 0.7176471, "blue": 0.7176471}
SUB_BG = {"red": 0.9294118, "green": 0.9294118, "blue": 0.9294118}
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


def seller_before_spp():
    d = _get("https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1000",
             {"Authorization": WB_TOKEN})
    out = {}
    for g in (d.get("data") or {}).get("listGoods") or []:
        sizes = g.get("sizes") or [{}]
        out[g.get("nmID")] = sizes[0].get("discountedPrice")
    return out


def mpstats_prices(nm):
    d = _get(f"https://mpstats.io/api/wb/get/item/{nm}",
             {"X-Mpstats-TOKEN": MP_TOKEN, "Content-Type": "application/json"})
    it = (d or {}).get("item") or {}
    return it.get("final_price"), it.get("wallet_price")


def collect():
    seller = seller_before_spp()
    data, misses = {}, []
    for name, nm in ACTIVE:
        try:
            final_p, wallet_p = mpstats_prices(nm)
        except Exception as e:
            print(f"[prices] MPStats {name}: {e}", flush=True)
            final_p = wallet_p = None
        before = seller.get(nm)
        before = round(before) if before else None
        if final_p is None or before is None:
            misses.append(name)
        data[name] = (before, final_p, wallet_p)
        time.sleep(0.25)
    return data, misses


def _open():
    info = json.loads(os.environ["GSHEETS_SA_JSON"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def write_snapshot(sh, data):
    ws = sh.worksheet(SNAP_TAB)
    col_b = ws.col_values(2)
    reqs, written = [], 0
    for row_i, name in enumerate(col_b, start=1):
        key = (name or "").strip()
        if key in data:
            before, spp, wallet = data[key]
            reqs.append({"range": f"{SNAP_TAB}!C{row_i}:E{row_i}",
                         "values": [[before, spp, wallet]]})
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

    data, misses = collect()
    got = sum(1 for v in data.values() if v[1] is not None)
    print(f"[prices] {run_label}: собрано {got}/{len(data)}; без цены: {misses}", flush=True)

    if not SHEET_ID:
        print("[prices] PRICES_SHEET_ID не задан — только сбор", flush=True); return
    if DRY:
        print("[prices][DRY] запись пропущена", flush=True); return

    try:
        sh = _open()
        w = write_snapshot(sh, data)
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
