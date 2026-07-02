"""Монитор габаритов WB (еженедельно) → канал «АС Фарм изменения».

WB карточку НЕ переписывает: свой обмер он кладёт в отчёты. Сигнал —
платное хранение (paid_storage): ИЗМЕРЕННЫЙ WB объём (л) по nmId сравниваем
с эталоном матрицы. Флагуем по факту удорожания/удешевления, не по абстрактному %:
  - ступень: WB тарифицирует объём с точностью 0,1 л; выше эталонной 0,1-л
    ступени — дороже (🍿), ниже — пока платим меньше (🤲);
  - коэффициент: расхождение >10% в любую сторону → WB включает ×5/×10.

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

OP_SHEET = "1sHlFGSVB-7V8V4q6kvcTR1rrw19EaabIaOgrCHU0DHE"
MATRIX_TAB = "МАТРИЦА"
PLATFORM = {"OZON", "OZ", "WB", "ЯМ", "ДМ", "ВБ"}
ANALYTICS = "https://seller-analytics-api.wildberries.ru"
DRY = os.environ.get("DRY") == "1"


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


import pathlib
STATE = pathlib.Path("data/wb_gabariti_state.json")
# порог штрафного коэффициента WB (×5/×10 при отклонении объёма >10%)
WB_PEN_PCT = int(os.environ.get("WB_PEN_PCT", "10"))


def _step(v):
    """0,1-л тарифная ступень WB (объём округляется до 0,1 л)."""
    return round(float(v) + 1e-9, 1)


def _classify(ev, wv, pct):
    """high — WB объём в более высокой 0,1-л ступени или расхождение >10% (дороже);
    low — в более низкой ступени или < -10% (пока платим меньше); иначе ok."""
    if _step(wv) > _step(ev) or pct >= WB_PEN_PCT:
        return "high"
    if _step(wv) < _step(ev) or pct <= -WB_PEN_PCT:
        return "low"
    return "ok"


def _load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save_state(d):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2))


def _default_period():
    import datetime
    dto = datetime.date.today() - datetime.timedelta(days=1)
    dfrom = dto - datetime.timedelta(days=6)
    return dfrom.isoformat(), dto.isoformat()


def run(dfrom=None, dto=None):
    if not dfrom or not dto:
        dfrom, dto = _default_period()
    et = load_etalon()
    state = _load_state()
    first_b = "skus" not in state
    recs = state.get("skus", {})
    print(f"[WB] период {dfrom}..{dto}, эталон {len(et)}, база {'нет (тихо)' if first_b else 'есть'}", flush=True)

    # --- объём хранения vs эталон (детект изменений в обе стороны) ---
    b_high, b_low, b_revert = [], [], []
    try:
        storage = fetch_paid_storage(dfrom, dto)
    except Exception as e:
        storage = {}
        print(f"[WB] paid_storage недоступен: {e}", flush=True)
    for vc, info in storage.items():
        e = et.get(vc)
        if not e:
            continue
        ev, wv = e[0], info["vol"]
        pct = round((wv - ev) / ev * 100) if ev else 0
        status = _classify(ev, wv, pct)
        prev = recs.get(vc, {})
        if first_b:
            recs[vc] = {"status": status, "wb_vol": wv, "pct": pct}
            continue
        changed = prev.get("status") != status or abs(wv - float(prev.get("wb_vol") or 0)) >= 0.05
        if status == "high" and changed:
            b_high.append((vc, ev, wv, pct))
        elif status == "low" and changed:
            b_low.append((vc, ev, wv, pct))
        elif status == "ok" and prev.get("status") in ("high", "low"):
            b_revert.append((vc, ev, wv))
        recs[vc] = {"status": status, "wb_vol": wv, "pct": pct}

    _save_state({"skus": recs})
    if first_b:
        print(f"[WB] базовый снимок объёмов ({len(recs)}) — B в канал не слал", flush=True)

    # --- дайджест (первая строка блока — жирная) ---
    parts = []
    if b_high:
        parts.append("🍿 <b>WB пересчитал объёмы на бОльшие — переплата за хранение и логистику:</b>")
        for vc, ev, wv, pct in sorted(b_high, key=lambda x: -x[3]):
            parts.append(f"• {vc}: эталон {_l(ev)} л → WB считает {_l(wv)} л (+{pct}%)")
        parts.append("Необходимо направить запрос в поддержку на переобмер, когда поедет новая поставка.")
        parts.append("")
    if b_low:
        parts.append("🤲 <b>WB пересчитал объёмы на меньшие — пока платим меньше:</b>")
        for vc, ev, wv, pct in sorted(b_low, key=lambda x: x[3]):
            parts.append(f"• {vc}: эталон {_l(ev)} л → WB считает {_l(wv)} л ({pct}%)")
        parts.append("Свезло. При переобмере WB вернёт к реальному — тогда станет дороже.")
        parts.append("")
    if b_revert:
        parts.append("✌️ <b>WB вернул объём к эталону:</b>")
        for vc, ev, wv in b_revert:
            parts.append(f"• {vc}: {_l(wv)} л (эталон {_l(ev)} л)")
    msg = "\n".join(parts).strip()

    if DRY:
        print("[WB][DRY] сообщение (в канал НЕ отправлено):\n" + (msg or "(пусто)"), flush=True)
        return
    if msg:
        notify.send(msg)
        print(f"[WB] отправлено (объём↑:{len(b_high)} объём↓:{len(b_low)} вернулось:{len(b_revert)})", flush=True)
    else:
        print("[WB] изменений нет — в канал ничего", flush=True)


# Разовая фиксация текущих несоответствий (обмер WB из калибровочного прогона
# 2026-06-30). Эталон берём ЖИВЬЁМ из матрицы, объём WB — из уже полученных данных
# (без запроса к WB API, чтобы не ловить лимит).
WB_MEASURED_SNAPSHOT = {
    "CrioGel1l": 2.121, "Dental_40_natural": 0.599, "Dental20": 0.294,
    "Dental_100_banan": 1.063, "Crio_L25(new)": 8.303,
    "Dental_100_zemlyanika": 1.008, "OptikaSpray_new": 0.756,
}


def send_current():
    et = load_etalon()
    items = []
    for art, wv in WB_MEASURED_SNAPSHOT.items():
        e = et.get(art)
        if not e:
            print(f"[SEND_CURRENT] нет эталона для {art}", flush=True)
            continue
        ev = e[0]
        pct = round((wv - ev) / ev * 100) if ev else 0
        items.append((art, ev, wv, pct))
    items.sort(key=lambda x: -x[3])
    lines = ["🍿 <b>WB пересчитал объёмы на бОльшие — переплата за хранение и логистику:</b>"]
    for art, ev, wv, pct in items:
        lines.append(f"• {art}: эталон {_l(ev)} л → WB считает {_l(wv)} л (+{pct}%)")
    lines.append("Необходимо направить запрос в поддержку на переобмер, когда поедет новая поставка.")
    notify.send("\n".join(lines))
    print(f"[SEND_CURRENT] отправлено артикулов: {len(items)}", flush=True)


if __name__ == "__main__":
    import sys
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if os.environ.get("SEND_CURRENT"):
        send_current()
    else:
        run(os.environ.get("WB_FROM") or (a[0] if len(a)>0 else None),
            os.environ.get("WB_TO") or (a[1] if len(a)>1 else None))
