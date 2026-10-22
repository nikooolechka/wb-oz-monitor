"""Монитор удержаний и штрафов (Ozon + WB) — алерт в канал «АС Фарм изменения».

Задача владельца (дословно): «агент ловит новые удержания/штрафы WB и Ozon
(логистика, брак ОВХ, хранение, недостача, прочее) и шлёт алерт с причиной и
суммой в канал». Зачем: видеть списания сразу, не искать по отчётам задним числом.

── OZON (работает на текущем ключе) ─────────────────────────────────────────
Источник: НОВЫЙ finance API `/v1/finance/accrual/by-day` (старый
`/v3/finance/transaction/list` Ozon отключил 08.09.2026). По дню, курсор last_id.
Удержания/штрафы = начисления категории **NON_ITEM** (не рутинная логистика по
заказу — та в POSTING/ITEM). Тип расшифровывается через `/v1/finance/accrual/types`.
Рекламу (клик/продвижение/трафареты/…) ИСКЛЮЧАЕМ — у неё свой контроль ДРР,
иначе ежедневный рекламный расход завалит сигнал.

── WB (нужен токен категории «Финансы») ─────────────────────────────────────
Старый `/api/v5/supplier/reportDetailByPeriod` WB отключил 15.07.2026 → новый
`POST finance-api.wildberries.ru/api/finance/v1/sales-reports/*`. Текущий
read-only токен даёт 403 «token does not satisfy additional requirements» —
новый финансовый API WB read-only не пускает. Когда владелец заведёт токен
категории «Финансы» (секрет WB_FIN_TOKEN) — WB-ветка активируется сама.

Секреты: OZON_CLIENT_ID, OZON_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
         [опц.] WB_FIN_TOKEN. Состояние: data/uderzhaniya_state.json (коммитится).
"""
from __future__ import annotations

import os
import json
import time
import ssl
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone

import notify

CID = os.environ["OZON_CLIENT_ID"].strip()
AK = os.environ["OZON_API_KEY"].strip()
OZ_HEAD = {"Client-Id": CID, "Api-Key": AK, "Content-Type": "application/json"}
OZ_BASE = "https://api-seller.ozon.ru"

WB_FIN_TOKEN = os.environ.get("WB_FIN_TOKEN", "").strip()
WB_FIN_BASE = "https://finance-api.wildberries.ru"

STATE_PATH = os.environ.get("STATE_PATH", "data/uderzhaniya_state.json")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "4"))   # Ozon начисления идут с лагом
DRY = os.environ.get("DRY") == "1"
CTX = ssl._create_unverified_context()

# Рекламные/маркетинговые типы NON_ITEM — у рекламы свой контроль (ДРР),
# в мониторе удержаний это шум. Всё остальное NON_ITEM считаем удержанием.
OZ_EXCLUDE_TYPES = {
    3, 4, 5,      # продвижение бренда / подключение / брендовая полка
    19,           # внешнее продвижение
    23,           # реклама в сети Интернет на сайте
    33,           # рекламные услуги
    36,           # привлечение предзаказов
    41,           # оплата за клик
    47,           # баллы за отзывы
    49, 50,       # Premium cashback продвижение / рассылка
    54,           # продвижение товара
    55,           # рассылка пуш
    61,           # закрепление отзыва
    70,           # приобретение отзывов
    75,           # трафареты
    80,           # генерация видеообложки
    87,           # реклама в соцсетях
    96,           # ускоренный сбор отзывов
    116,          # сбор первых отзывов
    130,          # размещение медийной рекламы
}
# Типы-ШТРАФЫ (отмечаем ⚠️ для акцента)
OZ_FINE_TYPES = {14, 15, 34, 89, 90, 91, 92, 93, 94, 101}


def _oz(path: str, body):
    data = json.dumps(body).encode()
    last = None
    for attempt in range(5):
        req = urllib.request.Request(OZ_BASE + path, data=data, headers=OZ_HEAD)
        try:
            with urllib.request.urlopen(req, timeout=90, context=CTX) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                last = f"HTTP {e.code}"; time.sleep(4 * (attempt + 1)); continue
            raise
        except Exception as e:  # noqa: BLE001
            last = str(e)[:120]; time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"Ozon {path}: не удалось после ретраев ({last})")


def oz_types() -> dict:
    t = _oz("/v1/finance/accrual/types", {}).get("accrual_types", [])
    return {x["id"]: (x.get("description") or x.get("name") or str(x["id"])) for x in t}


def _amount(block) -> float:
    try:
        return float((block or {}).get("amount", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def oz_collect(days: int):
    """Вернуть {accrual_id: (type_id, amount, date)} по NON_ITEM за последние `days`."""
    out = {}
    for dd in range(1, days + 1):
        day = (date.today() - timedelta(days=dd)).strftime("%Y-%m-%d")
        last_id = None
        while True:
            body = {"date": day}
            if last_id:
                body["last_id"] = last_id
            d = _oz("/v1/finance/accrual/by-day", body)
            acc = d.get("accruals", [])
            for a in acc:
                if a.get("accrued_category") != "NON_ITEM":
                    continue
                nif = a.get("non_item_fee") or {}
                tid = nif.get("type_id")
                amt = _amount(a.get("total_amount")) or _amount(nif.get("accrued"))
                out[a["accrual_id"]] = (tid, amt, a.get("date") or day)
            last_id = d.get("last_id")
            if not last_id or not acc:
                break
    return out


def wb_collect():
    """WB удержания из нового finance API. Нужен токен категории «Финансы».
    Возвращает (rows, note): rows = [(line_id, reason, amount, date)], либо [] + причина."""
    if not WB_FIN_TOKEN:
        return [], "нет токена (нужен WB_FIN_TOKEN категории «Финансы»)"
    dto = date.today()
    dfr = dto - timedelta(days=14)
    head = {"Authorization": WB_FIN_TOKEN, "Content-Type": "application/json"}

    def post(path, body):
        req = urllib.request.Request(WB_FIN_BASE + path, data=json.dumps(body).encode(), headers=head)
        with urllib.request.urlopen(req, timeout=120, context=CTX) as r:
            return json.load(r)

    try:
        # список отчётов за период → затем детализация по reportId
        lst = post("/api/finance/v1/sales-reports/list", {"dateFrom": str(dfr), "dateTo": str(dto)})
        reports = lst if isinstance(lst, list) else (lst.get("reports") or lst.get("data") or [])
        rows = []
        DED_HINTS = ("penalt", "штраф", "storage", "хранен", "deduct", "удерж",
                     "accept", "приёмк", "приемк", "additional", "доплат", "fine")
        for rep in reports:
            rid = rep.get("reportId") or rep.get("id") or rep
            det = post(f"/api/finance/v1/sales-reports/detailed/{rid}", {})
            lines = det if isinstance(det, list) else (det.get("data") or det.get("report") or [])
            for ln in lines:
                reason = (ln.get("bonusTypeName") or ln.get("supplierOperName") or "")
                amt = 0.0
                for k, v in ln.items():
                    if isinstance(v, (int, float)) and v and any(h in k.lower() for h in DED_HINTS):
                        amt += v
                if amt:
                    lid = str(ln.get("rrdId") or ln.get("rrd_id") or ln.get("srid") or f"{rid}:{ln.get('nmId','')}:{amt}")
                    rows.append((lid, reason or "удержание", amt, ln.get("rrDt") or ln.get("date") or ""))
        return rows, None
    except urllib.error.HTTPError as e:
        return [], f"HTTP {e.code} ({e.read().decode('utf-8','ignore')[:80]})"
    except Exception as e:  # noqa: BLE001
        return [], str(e)[:120]


def _fmt_rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ").replace("-", "−") + " ₽"


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(st: dict):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def main():
    st = _load_state()
    baseline_done = st.get("baseline_done", False)
    oz_seen = st.get("oz_seen", {})          # {accrual_id(str): date}
    wb_seen = set(st.get("wb_seen", []))

    tmap = oz_types()
    oz_now = oz_collect(LOOKBACK_DAYS)

    # новые Ozon-удержания (не виденные раньше, не реклама)
    new_oz = {}
    for aid, (tid, amt, dt) in oz_now.items():
        if str(aid) in oz_seen:
            continue
        if tid in OZ_EXCLUDE_TYPES:
            oz_seen[str(aid)] = dt    # помечаем виденным, но не репортим
            continue
        new_oz[aid] = (tid, amt, dt)
        oz_seen[str(aid)] = dt

    # WB
    wb_rows, wb_note = wb_collect()
    new_wb = [(lid, reason, amt, dt) for (lid, reason, amt, dt) in wb_rows if lid not in wb_seen]
    for lid, *_ in new_wb:
        wb_seen.add(lid)

    # prune oz_seen старше 21 дня (ограничить рост файла состояния)
    cutoff = (date.today() - timedelta(days=21)).strftime("%Y-%m-%d")
    oz_seen = {k: v for k, v in oz_seen.items() if (v or "9999") >= cutoff}

    # первый прогон — тихая база, не спамим историей
    if not baseline_done:
        st.update(baseline_done=True, oz_seen=oz_seen, wb_seen=sorted(wb_seen),
                  wb_note=wb_note)
        if not DRY:
            _save_state(st)
        print(f"[baseline] Ozon NON_ITEM виденных: {len(oz_seen)}; WB: {wb_note or len(wb_seen)}. Алерт не шлём.")
        return

    # агрегат Ozon по типам
    lines = []
    if new_oz:
        agg = {}
        for _, (tid, amt, _dt) in new_oz.items():
            c, s = agg.get(tid, (0, 0.0))
            agg[tid] = (c + 1, s + amt)
        block = ["<b>🧾 Новые удержания Ozon</b>"]
        total = 0.0
        for tid, (c, s) in sorted(agg.items(), key=lambda x: x[1][1]):
            total += s
            mark = "⚠️ " if tid in OZ_FINE_TYPES else "• "
            name = tmap.get(tid, f"тип {tid}")
            block.append(f"{mark}{name}: {_fmt_rub(s)}" + (f" ({c})" if c > 1 else ""))
        block.append(f"Итого за период: {_fmt_rub(total)}")
        lines.append("\n".join(block))

    # WB
    if new_wb:
        wagg = {}
        for _, reason, amt, _dt in new_wb:
            c, s = wagg.get(reason, (0, 0.0))
            wagg[reason] = (c + 1, s + amt)
        block = ["<b>🧾 Новые удержания WB</b>"]
        total = 0.0
        for reason, (c, s) in sorted(wagg.items(), key=lambda x: x[1][1]):
            total += s
            block.append(f"• {reason}: {_fmt_rub(s)}" + (f" ({c})" if c > 1 else ""))
        block.append(f"Итого: {_fmt_rub(total)}")
        lines.append("\n".join(block))

    st.update(baseline_done=True, oz_seen=oz_seen, wb_seen=sorted(wb_seen), wb_note=wb_note)

    if lines:
        msg = "\n\n".join(lines)
        print(msg)
        if not DRY:
            notify.send(msg)
    else:
        print(f"Новых удержаний нет. Ozon ok; WB: {wb_note or 'ok'}")

    if not DRY:
        _save_state(st)


if __name__ == "__main__":
    main()
