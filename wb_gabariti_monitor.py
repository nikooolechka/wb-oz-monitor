"""Монитор габаритов WB (еженедельно) → канал «АС Фарм изменения».

WB карточку НЕ переписывает: свой обмер он кладёт в отчёты и применяет
штраф/коэффициент. Два сигнала:
  A) Удержания (reportDetailByPeriod, через wb_finance): строки штрафов/логистики,
     связанные с габаритами (по bonus_type_name + аномальная логистика).
  B) Платное хранение (paid_storage): ИЗМЕРЕННЫЙ WB объём (л) по nmId — сравниваем
     с эталоном матрицы. Логистика WB пропорциональна (шаг 0,1 л), штраф за
     расхождение >10% = коэффициент ×5/×10.

Тексты — один дайджест на прогон (как на Ozon). DRY=1 → только лог, в канал НЕ шлём.

Секреты: WB_TOKEN, GSHEETS_SA_JSON, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import os
import re
import json
import time
import ssl
import math
import urllib.request

import gspread
from google.oauth2.service_account import Credentials

import notify
import wb_finance as wf

OP_SHEET = "1sHlFGSVB-7V8V4q6kvcTR1rrw19EaabIaOgrCHU0DHE"
MATRIX_TAB = "МАТРИЦА"
PLATFORM = {"OZON", "OZ", "WB", "ЯМ", "ДМ", "ВБ"}
ANALYTICS = "https://seller-analytics-api.wildberries.ru"
DRY = os.environ.get("DRY") == "1"
# ключевые слова для строк удержаний, связанных с габаритами/обмером
GAB_KW = ("габарит", "овх", "характеристик", "объ", "коэффициент", "логист", "хранени")

OVH = ()  # (у WB штраф свой — см. отчёт; калибруем по факту)


def _ctx():
    if os.environ.get("INSECURE_SSL") == "1":
        return ssl._create_unverified_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# ---------- эталон из матрицы (WB-сторона габаритов) ----------
def _nums(seg):
    return [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", seg)]


def parse_etalon_wb(v):
    if not v:
        return None
    seg = v
    if "ВБ" in v:                       # комбинированная «ВБ 5х5х14  ОЗ 4х4х12»
        seg = v.split("ВБ", 1)[1].split("ОЗ", 1)[0]
    elif "ОЗ" in v:
        seg = v.split("ОЗ", 1)[0]
    n = _nums(seg)
    if len(n) < 3:
        return None
    a, b, c = n[:3]
    dims = "х".join((str(int(x)) if x % 1 == 0 else str(x).replace(".", ",")) for x in (a, b, c))
    return round(a * b * c / 1000.0, 3), dims


def load_etalon():
    info = json.loads(os.environ["GSHEETS_SA_JSON"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    ws = gspread.authorize(creds).open_by_key(OP_SHEET).worksheet(MATRIX_TAB)
    C, V = ws.col_values(3), ws.col_values(22)
    et = {}
    for i, cell in enumerate(C):
        cell = (cell or "").strip()
        if not cell or cell == "артикул":
            continue
        p = parse_etalon_wb(V[i] if i < len(V) else "")
        if not p:
            continue
        for tok in re.split(r"[\s\n]+", cell):
            tok = tok.strip()
            if tok and tok.upper() not in PLATFORM:
                et.setdefault(tok, p)
    return et


# ---------- WB paid_storage (измеренный объём) ----------
def _wb_get(url):
    req = urllib.request.Request(url, headers={"Authorization": os.environ["WB_TOKEN"].strip()})
    with urllib.request.urlopen(req, timeout=120, context=_ctx()) as r:
        return r.status, json.loads(r.read() or b"null")


def fetch_paid_storage(dfrom, dto):
    """Возвращает {vendorCode: {'vol': измеренный объём л, 'nmId':..}} по свежей дате."""
    req = urllib.request.Request(
        f"{ANALYTICS}/api/v1/paid_storage?dateFrom={dfrom}&dateTo={dto}",
        headers={"Authorization": os.environ["WB_TOKEN"].strip()}, method="GET")
    with urllib.request.urlopen(req, timeout=120, context=_ctx()) as r:
        task = json.loads(r.read())["data"]["taskId"]
    # ждём готовности (макс ~2 мин)
    for _ in range(20):
        time.sleep(7)
        st, s = _wb_get(f"{ANALYTICS}/api/v1/paid_storage/tasks/{task}/status")
        if s and s.get("data", {}).get("status") == "done":
            break
    st, rows = _wb_get(f"{ANALYTICS}/api/v1/paid_storage/tasks/{task}/download")
    latest = {}
    for r in (rows or []):
        vc = (r.get("vendorCode") or "").strip()
        vol = r.get("volume")
        d = r.get("date") or ""
        if not vc or vol in (None, 0):
            continue
        if vc not in latest or d > latest[vc]["date"]:
            latest[vc] = {"vol": round(float(vol), 3), "nmId": r.get("nmId"), "date": d}
    return latest


def _l(v):
    return f"{v:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def run(dfrom, dto):
    et = load_etalon()
    print(f"[WB] эталон: {len(et)} артикулов", flush=True)

    # --- A) удержания/штрафы из реализации ---
    rows = wf.fetch_realization(dfrom, dto)
    print(f"[WB] реализация {dfrom}..{dto}: {len(rows)} строк", flush=True)
    by_reason = {}
    pen_by_art = {}
    for r in rows:
        pen = float(r.get("penalty") or 0)
        reason = (r.get("bonus_type_name") or "").strip()
        if pen:
            by_reason[reason] = by_reason.get(reason, 0) + pen
            art = (r.get("sa_name") or r.get("supplierArticle") or "").strip()
            if any(k in reason.lower() for k in GAB_KW):
                pen_by_art[art] = pen_by_art.get(art, 0) + pen
    print("[WB] виды штрафов (penalty) за период:")
    for reason, s in sorted(by_reason.items(), key=lambda x: -abs(x[1])):
        print(f"    {s:>10.2f} ₽  {reason or '(без причины)'}", flush=True)

    # --- B) измеренный объём из платного хранения ---
    try:
        storage = fetch_paid_storage(dfrom, dto)
    except Exception as e:
        storage = {}
        print(f"[WB] paid_storage недоступен: {e}", flush=True)
    print(f"[WB] платное хранение: {len(storage)} артикулов с объёмом", flush=True)
    dev = []
    for vc, info in storage.items():
        e = et.get(vc)
        if not e:
            continue
        et_vol = e[0]; wb_vol = info["vol"]
        diff = round(wb_vol - et_vol, 3)
        pct = (diff / et_vol * 100) if et_vol else 0
        tag = "ok" if abs(round(wb_vol, 1) - round(et_vol, 1)) < 0.05 else ("выше" if diff > 0 else "ниже")
        if tag != "ok":
            dev.append((vc, et_vol, wb_vol, diff, pct))
        print(f"    {vc:<24} эталон {_l(et_vol)} / WB измерил {_l(wb_vol)} ({diff:+.3f}, {pct:+.0f}%) [{tag}]", flush=True)

    if DRY:
        print("[WB][DRY] калибровочный прогон — в канал ничего не отправлено.", flush=True)
        return

    # боевой режим — дайджест (форматы утвердим после калибровки)
    parts = []
    gab_pen = {a: p for a, p in pen_by_art.items() if p}
    if gab_pen:
        parts.append("⚠️ <b>WB: удержания, связанные с габаритами/обмером:</b>")
        for a, p in sorted(gab_pen.items(), key=lambda x: -x[1]):
            parts.append(f"• {a}: {p:.0f} ₽")
        parts.append("")
    if dev:
        parts.append("📏 <b>WB измерил объём иначе, чем в эталоне матрицы:</b>")
        for vc, ev, wv, diff, pct in sorted(dev, key=lambda x: -abs(x[4])):
            line = f"• {vc}: эталон {_l(ev)} л → WB {_l(wv)} л ({pct:+.0f}%)"
            if abs(pct) > 10:
                line += " — риск коэффициента ×5/×10 и штрафа"
            parts.append(line)
    if parts:
        notify.send("\n".join(parts).strip())
        print(f"[WB] отправлено в канал", flush=True)
    else:
        print("[WB] изменений нет", flush=True)


if __name__ == "__main__":
    import sys
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    df, dt = (a + [os.environ.get("WB_FROM", "2026-06-23"), os.environ.get("WB_TO", "2026-06-30")])[:2]
    run(df, dt)
