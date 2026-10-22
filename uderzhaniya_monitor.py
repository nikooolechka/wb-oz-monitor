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
DRY = os.environ.get("DRY") == "1"
FORCE = os.environ.get("FORCE") == "1"   # игнорировать дедуп недели (для ручного прогона)
CTX = ssl._create_unverified_context()

# БЕЛЫЙ СПИСОК: только настоящие удержания/штрафы. Логистика (кросс-докинг,
# магистраль, последняя миля, перемещения…), эквайринг/комиссия, реклама,
# подписки — это рутинные расходы, НЕ штрафы → в монитор НЕ берём.
OZ_FINE_TYPES = {14, 89, 90, 91, 92, 93, 94}   # штрафы (отмечаем ⚠️):
#   14 операц. ошибки продавца · 89 запрещ. контент · 90 запрещ. товар
#   91 наруш. интеллект. прав · 92 жалобы покупателей · 93 превыш. индекса ошибок
#   94 отгрузка в нерекомендованный слот
OZ_DEDUCT_TYPES = {
    77, 82, 101,          # обработка товара (ОВХ) / доп. обработка ОВХ / нестандарт
    46, 60, 78, 79, 102,  # размещение/хранение (в т.ч. долгосрочн./кратк. размещ. возврата)
    8, 10, 25, 76, 104,   # претензии / компенсация / товарная компенсация / страховка
    15, 103,              # утилизация
    11, 57, 72, 129,      # инвентаризация взаиморасчётов / корректировки / взаимозачёт
}
OZ_INCLUDE_TYPES = OZ_FINE_TYPES | OZ_DEDUCT_TYPES


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


def oz_collect(days_list):
    """Вернуть {accrual_id: (type_id, amount, date)} по NON_ITEM за дни из days_list."""
    out = {}
    for day in days_list:
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
    """WB удержания/штрафы из нового finance API (read-only токен категории «Финансы»,
    тип acc:3/for:self — WB_FIN_TOKEN). Метод /list уже отдаёт готовые суммы по НЕДЕЛЬНОМУ
    отчёту — детализацию тянуть не нужно. Берём последний завершённый отчёт.
    Возвращает (rows, period, note): rows = [(label, amount<0, is_fine)]."""
    if not WB_FIN_TOKEN:
        return [], None, "нет токена (WB_FIN_TOKEN, категория «Финансы»)"
    head = {"Authorization": WB_FIN_TOKEN, "Content-Type": "application/json"}
    dto = date.today(); dfr = dto - timedelta(days=21)
    try:
        req = urllib.request.Request(WB_FIN_BASE + "/api/finance/v1/sales-reports/list",
                                     data=json.dumps({"dateFrom": str(dfr), "dateTo": str(dto)}).encode(), headers=head)
        with urllib.request.urlopen(req, timeout=90, context=CTX) as r:
            reps = json.load(r)
    except urllib.error.HTTPError as e:
        return [], None, f"HTTP {e.code} ({e.read().decode('utf-8','ignore')[:80]})"
    except Exception as e:  # noqa: BLE001
        return [], None, str(e)[:120]
    reps = reps if isinstance(reps, list) else (reps.get("reports") or reps.get("data") or [])
    if not reps:
        return [], None, "нет отчётов за период"
    rep = max(reps, key=lambda x: str(x.get("dateTo", "")))   # последний завершённый недельный отчёт

    def f(k):
        try: return abs(float(rep.get(k) or 0))
        except (TypeError, ValueError): return 0.0
    # логистику (deliveryServiceSum) НЕ берём — это не штраф/удержание
    FIELDS = [("penaltySum", "Штрафы", True), ("deductionSum", "Удержания", False),
              ("paidStorageSum", "Хранение", False), ("paidAcceptanceSum", "Приёмка", False),
              ("additionalPaymentSum", "Доплаты", False)]
    rows = [(label, -v, fine) for (k, label, fine) in FIELDS if (v := f(k))]
    period = f"{rep.get('dateFrom', '')[5:]}–{rep.get('dateTo', '')[5:]}"
    return rows, period, None


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


def _week_window(today: date):
    """Окно пн–пн: [понедельник прошлой недели, понедельник этой недели).
    Запуск по вторникам → отчёт за ПОЛНУЮ прошедшую неделю (7 дней, пн..вс)."""
    this_mon = today - timedelta(days=today.weekday())   # понедельник текущей недели
    start = this_mon - timedelta(days=7)                 # понедельник прошлой недели
    end = this_mon                                       # исключительно
    return start, end


def main():
    st = _load_state()
    today = date.today()
    start, end = _week_window(today)
    week_key = str(start)

    # Шлём итог прошедшей недели во ВТОРНИК (weekday 1). Крон гоняем ежедневно:
    # если GitHub-крон пропустит вторник — отправим в первый же прогон до конца недели
    # (дедуп по week_key не даст повтора). В понедельник ещё рано — ждём вторника.
    if today.weekday() < 1 and not FORCE:
        print("Понедельник — рано (ждём вторника).")
        return
    if st.get("last_week") == week_key and not (DRY or FORCE):
        print(f"За неделю {week_key} дайджест уже отправлен — пропуск.")
        return

    days = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((end - start).days)]
    period = f"{start:%d.%m}–{(end - timedelta(days=1)):%d.%m}"

    tmap = oz_types()
    oz_now = oz_collect(days)

    # Ozon: агрегат NON_ITEM за неделю (реклама исключена)
    agg = {}
    for _, (tid, amt, _dt) in oz_now.items():
        if tid not in OZ_INCLUDE_TYPES:   # только настоящие удержания/штрафы
            continue
        c, s = agg.get(tid, (0, 0.0))
        agg[tid] = (c + 1, s + amt)

    # WB — последний завершённый недельный отчёт (суммы из /list)
    wb_rows, wb_period, wb_note = wb_collect()

    lines = []
    grand = 0.0
    if agg:
        block = ["<b>Ozon:</b>"]
        total = 0.0
        for tid, (c, s) in sorted(agg.items(), key=lambda x: x[1][1]):
            total += s
            mark = "⚠️ " if tid in OZ_FINE_TYPES else "• "
            block.append(f"{mark}{tmap.get(tid, f'тип {tid}')}: {_fmt_rub(s)}" + (f" ({c})" if c > 1 else ""))
        block.append(f"Ozon итого: {_fmt_rub(total)}")
        grand += total
        lines.append("\n".join(block))

    if wb_rows:
        block = [f"<b>WB</b> <i>(отчёт {wb_period}):</i>" if wb_period else "<b>WB:</b>"]
        total = 0.0
        for label, amt, fine in sorted(wb_rows, key=lambda x: x[1]):
            total += amt
            mark = "⚠️ " if fine else "• "
            block.append(f"{mark}{label}: {_fmt_rub(amt)}")
        block.append(f"WB итого: {_fmt_rub(total)}")
        grand += total
        lines.append("\n".join(block))

    header = f"<b>💸 Удержания и штрафы с {period}</b>"
    if lines:
        body = "\n\n".join(lines) + f"\n\n<b>Итого за неделю: {_fmt_rub(grand)}</b>"
    else:
        body = "За неделю удержаний и штрафов не было 👍"
    # WB появится, когда будет не-read-only токен с доступом к финансам
    footer = "" if WB_FIN_TOKEN else "\n\nWB добавлю, когда заведёшь финансовый токен (не «только чтение»)."
    msg = f"{header}\n\n{body}{footer}"

    print(msg)
    if not DRY:
        notify.send(msg)
        st.update(last_week=week_key, wb_note=wb_note)
        _save_state(st)


if __name__ == "__main__":
    main()
