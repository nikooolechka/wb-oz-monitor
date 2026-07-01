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


def _liter(v):
    # Логистика Ozon округляет объём вверх до ЦЕЛОГО литра (проверено по реальным
    # списаниям: внутри одного литра — та же цена; ступени по целым литрам).
    return math.ceil(round(v, 6))


def classify(oz_vol, et_vol):
    """ДВА сигнала: (1) логистика — сменился ли целый литр; (2) штраф ОВХ —
    карточка ниже эталона на ≥0,6 л. Тревога, если сработал любой."""
    cl, el = _liter(oz_vol), _liter(et_vol)
    gap = et_vol - oz_vol
    fee = ovh_fee(gap) if gap > 0 else 0
    if cl > el:
        return "high"
    if cl < el or fee > 0:
        return "low"
    return "ok"


def build_digest(items):
    """Одно сообщение на прогон по ВСЕМ изменившимся артикулам."""
    highs = [i for i in items if i["status"] == "high"]
    lows = [i for i in items if i["status"] == "low"]
    revs = [i for i in items if i["status"] == "revert"]
    p = []
    if highs:
        p.append("🍿 <b>Пупупу — OZON завысил габариты, надо оспаривать:</b>")
        for i in highs:
            p.append(f"• {_esc(i['off'])}: эталон {i['et_dims']} = {_l(i['et_vol'])} л "
                     f"→ на Ozon {i['oz_dims']} = {_l(i['oz_vol'])} л")
        p.append("Логистику считают по завышенному объёму.")
        p.append("")
    if lows:
        p.append("🤲 <b>OZON поменял габариты в меньшую сторону — нам свезло:</b>")
        for i in lows:
            line = (f"• {_esc(i['off'])}: эталон {i['et_dims']} = {_l(i['et_vol'])} л "
                    f"→ на Ozon {i['oz_dims']} = {_l(i['oz_vol'])} л")
            if i.get("fee"):
                line += f" — риск штрафа за занижение ~{i['fee']} ₽"
            p.append(line)
        p.append("Пока платим по заниженному — свезло. Но если Ozon перемерит до "
                 "реального — начислит плату за занижение и поднимет логистику. Всё в наших руках)")
        p.append("")
    if revs:
        p.append("✌️ <b>OZON вернул габариты к эталону в нашей матрице:</b>")
        for i in revs:
            p.append(f"• {_esc(i['off'])}: {i['oz_dims']} = {_l(i['oz_vol'])} л")
    return "\n".join(p).strip()


def _collect(cards, et):
    out = []
    for off, card in cards.items():
        e = et.get(ALIAS.get(off, off)) or et.get(off)
        if not e:
            continue
        et_vol, et_dims = e
        st = classify(card["vol"], et_vol)
        out.append({"off": off, "status": st, "et_dims": et_dims, "et_vol": et_vol,
                    "oz_dims": _dims_cm(card["mm"]), "oz_vol": card["vol"],
                    "fee": ovh_fee(et_vol - card["vol"]) if (et_vol - card["vol"]) > 0 else 0})
    return out


def send_current():
    """Разовая отправка ТЕКУЩИХ несоответствий одним сообщением (по запросу)."""
    items = [i for i in _collect(fetch_cards(), load_etalon()) if i["status"] in ("high", "low")]
    if items:
        notify.send(build_digest(items))
        print(f"[SEND_NOW] отправлено артикулов: {len(items)}", flush=True)
    else:
        print("[SEND_NOW] текущих несоответствий нет", flush=True)


def run_once():
    state = _load()
    first = not state
    et = load_etalon()
    cards = fetch_cards()
    recs = state.get("skus", {})
    changed = []
    for it in _collect(cards, et):
        off, status = it["off"], it["status"]
        prev = recs.get(off, {})
        if first:
            recs[off] = {"status": status, "oz_dims": it["oz_dims"], "oz_vol": it["oz_vol"]}
            print(f"[BASE] {off}: {status} (эталон {it['et_dims']}={_l(it['et_vol'])} / Ozon {it['oz_dims']}={_l(it['oz_vol'])})", flush=True)
            continue
        if it["oz_dims"] == prev.get("oz_dims"):
            continue  # карточка не менялась
        eff = status
        if status == "ok":
            if prev.get("status") in ("high", "low"):
                eff = "revert"
            else:
                recs[off] = {"status": status, "oz_dims": it["oz_dims"], "oz_vol": it["oz_vol"]}
                continue
        it2 = dict(it); it2["status"] = eff
        changed.append(it2)
        recs[off] = {"status": status, "oz_dims": it["oz_dims"], "oz_vol": it["oz_vol"]}
        print(f"[ALERT] {off}: {prev.get('oz_dims')} -> {it['oz_dims']} ({eff})", flush=True)

    if not first and changed:
        notify.send(build_digest(changed))
    _save({"skus": recs})
    if first:
        print(f"[OK] базовый снимок: {len(recs)} товаров (в канал ничего)", flush=True)
    else:
        print(f"[OK] изменений: {len(changed)}; сообщение {'отправлено' if changed else 'не требуется'}", flush=True)


if __name__ == "__main__":
    if os.environ.get("SEND_NOW"):
        send_current()
    else:
        run_once()
