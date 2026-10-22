"""Ловля перехода товара (поставки) на платное хранение WB.

Задача владельца (дословно): «идея - ловить когда товар перешел на платное хранение».

Как работает WB paid_storage: платное хранение считается НЕ по товару в целом, а
ПО КАЖДОЙ ПОСТАВКЕ отдельно (поле giId в отчёте seller-analytics-api/paid_storage).
Строк с warehousePrice==0 отчёт НЕ отдаёт вообще — пока поставка в бесплатном периоде,
её просто нет в отчёте. Проверено live 2026-10-22: 203 строки за 3 дня, 0 строк с
warehousePrice==0 — WB не показывает "бесплатные" строки, только платные.

Значит детект перехода = ПЕРВОЕ появление конкретного giId в отчёте с ненулевой
стоимостью. Без своих расчётов "сколько дней бесплатно" (правила WB могут меняться
по категориям/сезону) — просто доверяем отчёту WB: как появился giId — значит,
WB начал его платно хранить.

Секреты: WB_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. Состояние: data/wb_paid_storage_state.json.
"""
from __future__ import annotations

import os
import json
import time
import ssl
import urllib.request
import datetime

import notify
import state

ANALYTICS = "https://seller-analytics-api.wildberries.ru"
WB_TOKEN = os.environ["WB_TOKEN"].strip()
HEAD = {"Authorization": WB_TOKEN}
DRY = os.environ.get("DRY") == "1"
_ctx = ssl._create_unverified_context()


def _get(url):
    req = urllib.request.Request(url, headers=HEAD)
    with urllib.request.urlopen(req, timeout=120, context=_ctx) as r:
        return json.loads(r.read() or b"null")


def fetch_paid_storage(dfrom, dto):
    """Один запрос (асинхронная задача WB, период ≤8 дней). Возвращает сырые строки отчёта."""
    task = _get(f"{ANALYTICS}/api/v1/paid_storage?dateFrom={dfrom}&dateTo={dto}")["data"]["taskId"]
    for _ in range(20):   # до ~2.5 мин ожидания готовности
        time.sleep(7)
        s = _get(f"{ANALYTICS}/api/v1/paid_storage/tasks/{task}/status")
        if s and s.get("data", {}).get("status") == "done":
            break
    return _get(f"{ANALYTICS}/api/v1/paid_storage/tasks/{task}/download") or []


def main():
    st = state.load()
    seen = st.get("giids", {})   # {str(giId): {"date":..., "vendorCode":...}}
    baseline_done = st.get("baseline_done", False)

    # данные WB с лагом ~2-3 дня; окно 8 дней (максимум одной задачи) полностью
    # перекрывает недельный интервал между прогонами (четверг→четверг)
    dto = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()
    dfrom = (datetime.date.today() - datetime.timedelta(days=9)).isoformat()

    try:
        rows = fetch_paid_storage(dfrom, dto)
    except Exception as e:  # noqa: BLE001
        print("WB paid_storage недоступен:", str(e)[:150])
        return

    # последняя (по дате) строка на каждый giId — для карточки алерта
    latest = {}
    for r in rows:
        gi = str(r.get("giId") or "")
        price = r.get("warehousePrice")
        if not gi or price in (None, 0):
            continue
        d = r.get("date") or ""
        if gi not in latest or d > latest[gi]["date"]:
            latest[gi] = {
                "date": d, "vendorCode": (r.get("vendorCode") or "").strip(),
                "nmId": r.get("nmId"), "warehouse": r.get("warehouse") or "",
                "price": round(float(price), 2),
            }

    if not baseline_done:
        # первый прогон — тихая база (поставки, уже давно на платном хранении, не алертим)
        for gi, info in latest.items():
            seen[gi] = {"date": info["date"], "vendorCode": info["vendorCode"]}
        st.update(baseline_done=True, giids=seen)
        if not DRY:
            state.save(st)
        print(f"[baseline] поставок на платном хранении: {len(seen)}. Алерт не шлём.")
        return

    new_gi = {gi: info for gi, info in latest.items() if gi not in seen}
    for gi, info in new_gi.items():
        seen[gi] = {"date": info["date"], "vendorCode": info["vendorCode"]}

    if new_gi:
        lines = ["<b>📦 Товар перешёл на платное хранение WB</b>"]
        for gi, info in sorted(new_gi.items(), key=lambda x: x[1]["vendorCode"]):
            name = info["vendorCode"] or f"nmId {info['nmId']}"
            lines.append(f"• {name} — поставка {gi}, с {info['date']}, {info['warehouse']}, ~{info['price']} ₽/день")
        msg = "\n".join(lines)
        print(msg)
        if not DRY:
            notify.send(msg)
    else:
        print("Новых переходов на платное хранение нет.")

    st.update(baseline_done=True, giids=seen)
    if not DRY:
        state.save(st)


if __name__ == "__main__":
    main()
