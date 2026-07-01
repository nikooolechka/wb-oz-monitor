"""Монитор габаритов Ozon: сверка карточек с ЭТАЛОНОМ матрицы → канал изменений.

Ozon на приёмке сам перемеряет ОВХ и переписывает габариты В КАРТОЧКУ
(подтверждено справкой Ozon). Эталон = габариты из товарной матрицы
(вкладка МАТРИЦА, столбец «габариты товара»). Ежедневно тянем габариты из
Ozon Seller API, считаем объём (л) и сравниваем с эталоном матрицы.

Классификация (вариант C — любое отклонение сверх допуска MIN_DELTA_L):
  карточка > эталона → Ozon ЗАВЫСИЛ → переплата логистики → оспаривать 🍿
  карточка < эталона → занижено → нам свезло 🤲 (+ риск платы за занижение ОВХ)
  вернулось к эталону → ✌️

Первый прогон — ТИХИЙ: фиксирует базу (в канал ничего), печатает картину в лог.

Секреты: OZON_CLIENT_ID, OZON_API_KEY, GSHEETS_SA_JSON,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
STATE_PATH (data/oz_gabariti_state.json), MIN_DELTA_L (0.05).
"""
from __future__ import annotations

import os
import re
import math
import json
import tempfile

import requests
import gspread
from google.oauth2.service_account import Credentials

import notify

STATE_PATH = os.environ.get("STATE_PATH", "data/oz_gabariti_state.json")
MIN_DELTA_L = float(os.environ.get("MIN_DELTA_L", "0.05"))
CLIENT_ID = os.environ["OZON_CLIENT_ID"].strip()
API_KEY = os.environ["OZON_API_KEY"].strip()
BASE = "https://api-seller.ozon.ru"
HEAD = {"Client-Id": CLIENT_ID, "Api-Key": API_KEY, "Content-Type": "application/json"}

OP_SHEET = "1sHlFGSVB-7V8V4q6kvcTR1rrw19EaabIaOgrCHU0DHE"
MATRIX_TAB = "МАТРИЦА"
PLATFORM = {"OZON", "OZ", "OZON ", "WB", "ЯМ", "ДМ", "ВБ"}
# Ozon offer_id -> артикул в матрице (когда названия разошлись)
ALIAS = {"spraydlyapolostyrta": "OralLubrikant"}

# Плата Ozon за обработку ОВХ при занижении (с 15.09.2025), по разнице объёма (л)
OVH_TIERS = [(0.6, 0), (1, 150), (2, 300), (3, 600), (5, 900), (10, 1200), (float("inf"), 1500)]


def ovh_fee(gap_l: float) -> int:
    for hi, fee in OVH_TIERS:
        if gap_l <= hi:
            return fee
    return 1500


# ---------- эталон из матрицы ----------
def _sheets():
    info = json.loads(os.environ["GSHEETS_SA_JSON"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    return gspread.authorize(creds)


def _nums(seg: str):
    return [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", seg)]


def parse_etalon(v: str):
    """Из ячейки «габариты товара» -> (объём л, 'ДхШхВ'). Для комбинированных
    'ВБ 5х5х14  ОЗ 4х4х12' берём ОЗ-часть."""
    if not v:
        return None
    seg = v
    if "ОЗ" in v:
        seg = v.split("ОЗ", 1)[1]
    n = _nums(seg)
    if len(n) < 3:
        return None
    a, b, c = n[0], n[1], n[2]
    vol = round(a * b * c / 1000.0, 3)   # см³ -> л
    dims = "х".join(str(x).rstrip("0").rstrip(".").replace(".", ",") if x % 1 else str(int(x))
                    for x in (a, b, c))
    return vol, dims


def load_etalon():
    ws = _sheets().open_by_key(OP_SHEET).worksheet(MATRIX_TAB)
    C = ws.col_values(3)    # артикул
    V = ws.col_values(22)   # габариты товара
    et = {}
    for i, cell in enumerate(C):
        cell = (cell or "").strip()
        if not cell or cell == "артикул":
            continue
        vv = V[i] if i < len(V) else ""
        parsed = parse_etalon(vv)
        if not parsed:
            continue
        for tok in re.split(r"[\s\n]+", cell):
            tok = tok.strip()
            if tok and tok.upper() not in PLATFORM:
                et.setdefault(tok, parsed)
    return et


# ---------- карточки Ozon ----------
def fetch_cards():
    out, last = {}, ""
    while True:
        r = requests.post(BASE + "/v4/product/info/attributes", headers=HEAD,
                          json={"filter": {"visibility": "VISIBLE"}, "limit": 100, "last_id": last},
                          timeout=60)
        r.raise_for_status()
        d = r.json()
        items = d.get("result") or []
        for it in items:
            off = (it.get("offer_id") or "").strip()
            dp, w, h = it.get("depth"), it.get("width"), it.get("height")
            if off and dp and w and h:
                out[off] = {"mm": (dp, w, h), "vol": round(dp * w * h / 1_000_000, 3)}
        last = d.get("last_id", "")
        if not last or len(items) < 100:
            break
    return out


# ---------- утилиты ----------
def _load():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    d = os.path.dirname(STATE_PATH) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _l(v):
    return f"{v:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def _cm(mm):
    v = mm / 10.0
    return (str(int(v)) if v % 1 == 0 else f"{v:.2f}".rstrip("0").rstrip(".")).replace(".", ",")


def _dims_cm(mm3):
    return "×".join(_cm(x) for x in mm3)


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def msg_high(art, et_dims, et_l, oz_dims, oz_l):
    return (f"🍿 Пупупу — OZON поменял габариты: {_esc(art)}\n"
            f"эталон из матрицы {et_dims} = {et_l} л → на {oz_dims} = {oz_l} л.\n"
            f"Логистику считают по завышенному объёму — надо оспаривать")


def msg_low(art, et_dims, et_l, oz_dims, oz_l, fee):
    fee_txt = f"начислит плату за занижение ~{fee} ₽ и " if fee else ""
    return (f"🤲 OZON поменял габариты в меньшую сторону по {_esc(art)}\n"
            f"эталон из матрицы {et_dims} = {et_l} л → на Ozon {oz_dims} = {oz_l} л.\n"
            f"Пока платим по заниженному — нам свезло. Но если Ozon перемерит до реального, "
            f"{fee_txt}поднимет логистику. Всё в наших руках)")


def msg_revert(art, old_dims, old_l, et_dims, et_l):
    return (f"✌️ OZON вернул габариты {_esc(art)} к эталонным в нашей матрице — "
            f"с {old_dims} ({old_l} л) на {et_dims} ({et_l} л). Всё как надо.")


def _liter(v):
    # Логистика Ozon округляет объём вверх до ЦЕЛОГО литра (проверено по
    # реальным списаниям: объём внутри одного литра — та же цена; заметные
    # ступени по целым литрам, крупные скачки на 7 и 15 л).
    return math.ceil(round(v, 6))


def classify(oz_vol, et_vol):
    """ДВА сигнала:
      (1) логистика — сменился ли ЦЕЛЫЙ литр (тариф реально другой);
      (2) штраф ОВХ — карточка ниже эталона на ≥0,6 л (первая ступень платы за занижение).
    Тревога, если сработал любой. Направление: карточка выше литра эталона →
    'high' (логистика дороже, оспаривать); карточка ниже по литру ИЛИ есть риск
    штрафа → 'low' (занижено: свезло по логистике и/или риск платы)."""
    cl, el = _liter(oz_vol), _liter(et_vol)
    gap = et_vol - oz_vol
    fee = ovh_fee(gap) if gap > 0 else 0
    if cl > el:
        return "high"
    if cl < el or fee > 0:
        return "low"
    return "ok"


def run_once():
    state = _load()
    first = not state
    et = load_etalon()
    cards = fetch_cards()

    recs = state.get("skus", {})
    notified = 0
    for off, card in cards.items():
        key = ALIAS.get(off, off)
        e = et.get(key) or et.get(off)
        if not e:
            continue
        et_vol, et_dims = e
        oz_dims = _dims_cm(card["mm"])
        oz_vol = card["vol"]
        status = classify(oz_vol, et_vol)
        prev = recs.get(off, {})

        if first:
            recs[off] = {"status": status, "oz_dims": oz_dims, "oz_vol": oz_vol}
            print(f"[BASE] {off}: {status} (эталон {et_dims}={_l(et_vol)} / Ozon {oz_dims}={_l(oz_vol)})", flush=True)
            continue

        # шлём только когда карточка реально изменилась и это новый статус/значение
        if oz_dims == prev.get("oz_dims"):
            continue
        if status == "high":
            notify.send(msg_high(off, et_dims, _l(et_vol), oz_dims, _l(oz_vol)))
        elif status == "low":
            notify.send(msg_low(off, et_dims, _l(et_vol), oz_dims, _l(oz_vol), ovh_fee(et_vol - oz_vol)))
        elif prev.get("status") in ("high", "low"):
            notify.send(msg_revert(off, prev.get("oz_dims"), _l(prev.get("oz_vol", oz_vol)), et_dims, _l(et_vol)))
        else:
            recs[off] = {"status": status, "oz_dims": oz_dims, "oz_vol": oz_vol}
            continue
        print(f"[ALERT] {off}: {prev.get('oz_dims')} -> {oz_dims} ({status})", flush=True)
        recs[off] = {"status": status, "oz_dims": oz_dims, "oz_vol": oz_vol}
        notified += 1

    _save({"skus": recs})
    if first:
        print(f"[OK] базовый снимок: {len(recs)} товаров (в канал ничего)", flush=True)
    else:
        print(f"[OK] отправлено в канал: {notified}", flush=True)


if __name__ == "__main__":
    run_once()
