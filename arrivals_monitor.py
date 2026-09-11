#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Монитор ПРИХОДОВ товара (WB + Ozon, FBS + FBO) → канал «АС Фарм изменения».

Идея (как ловля Казани, обобщённая): раз в день смотрим остаток по каждому
складу/схеме и, если он ВЫРОС (пришла поставка / товар приехал на новый склад) —
шлём алерт. Пуш маркетплейсы не дают, поэтому опрос по расписанию.

Источники остатков (проверено live 2026-09-11):
- WB FBS: свои FBS-склады (/api/v3/warehouses) × остаток (/api/v3/stocks/{wid} по баркодам).
- WB FBO: отчёт warehouse_remains (seller-analytics-api, async) по складам WB.
- Ozon FBS: /v4/product/info/stocks (type fbs, present) — суммой (один FBS-склад).
- Ozon FBO: /v2/analytics/stock_on_warehouses по складам Ozon.

Алерт (первая строка жирная):
- FBS: «Товар приехал на склад {name} ({ВБ/Озон} FBS): {N} шт остатка.\nСклад {id} готов к заказам.»
- FBO: «Товар приехал на склад {name} ({ВБ/Озон} FBO): {N} шт.»

Дедуп/шум: алерт только при росте остатка >= ARRIVAL_MIN (по умолч. 30 шт) —
чтобы возвраты по 1-2 шт не спамили. Первый прогон — тихая база (без алертов).
Состояние: data/arrivals_state.json (коммитится).

Секреты: WB_TOKEN, OZON_CLIENT_ID, OZON_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
DRY=1 — только лог, без канала и без записи состояния.
"""
import os, json, time, ssl, urllib.request

import notify

MP = "https://marketplace-api.wildberries.ru"
CONTENT = "https://content-api.wildberries.ru"
ANALYTICS = "https://seller-analytics-api.wildberries.ru"
OZ = "https://api-seller.ozon.ru"
ARRIVAL_MIN = int(os.environ.get("ARRIVAL_MIN", "30"))
STATE_FILE = "data/arrivals_state.json"
DRY = os.environ.get("DRY") == "1"


def _ctx():
    if os.environ.get("INSECURE_SSL") == "1":
        return ssl._create_unverified_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


CTX = _ctx()
WB = os.environ["WB_TOKEN"].strip()


def _wb(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": WB, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=CTX, timeout=120) as r:
        return json.load(r)


def _oz(path, body):
    req = urllib.request.Request(OZ + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Client-Id": os.environ["OZON_CLIENT_ID"].strip(),
                                          "Api-Key": os.environ["OZON_API_KEY"].strip(),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=CTX, timeout=120) as r:
        return json.load(r)


# ---------- WB FBS: остаток по своим FBS-складам ----------
def wb_fbs_stocks():
    whs = _wb(f"{MP}/api/v3/warehouses")
    # баркоды всех карточек
    barcodes = []
    cursor = {"limit": 1000}
    for _ in range(30):
        d = _wb(f"{CONTENT}/content/v2/get/cards/list", "POST",
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
    out = {}
    for w in whs:
        wid = w.get("id"); name = w.get("name") or str(wid)
        total = 0
        for i in range(0, len(barcodes), 1000):
            try:
                st = _wb(f"{MP}/api/v3/stocks/{wid}", "POST", {"skus": barcodes[i:i + 1000]})
                for s in st.get("stocks", []):
                    total += s.get("amount", 0) or 0
            except Exception as e:
                print("wb fbs stock err", wid, str(e)[:40])
            time.sleep(0.2)
        out[(f"WBFBS|{wid}", "ВБ FBS", name, str(wid))] = total
    return out


# ---------- WB FBO: остаток по складам WB (warehouse_remains, async) ----------
def wb_fbo_stocks():
    base = f"{ANALYTICS}/api/v1/warehouse_remains"
    tid = _wb(base + "?locale=ru&groupByWarehouse=true&groupBySubject=false&groupByBrand=false&groupByNm=true").get("data", {}).get("taskId")
    if not tid:
        return {}
    for _ in range(25):
        time.sleep(6)
        if _wb(base + f"/tasks/{tid}/status").get("data", {}).get("status") == "done":
            break
    rows = _wb(base + f"/tasks/{tid}/download")
    total = 0
    for r in rows:
        for w in r.get("warehouses", []):
            low = (w.get("warehouseName", "") or "").lower()
            if "пути" in low or "всего" in low or "находится" in low:
                continue                       # транзит/итоги — не приход
            total += w.get("quantity", 0) or 0
    # FBO считаем СУММОЙ по платформе: Ozon/WB сами перекладывают товар между РФЦ
    # (это не поставка), а реальный приход = рост общего остатка.
    return {("WBFBO", "ВБ FBO", "склады WB (все)", ""): total}


# ---------- Ozon FBS (сумма) ----------
def oz_fbs_total():
    total = 0; cursor = ""
    for _ in range(30):
        d = _oz("/v4/product/info/stocks", {"filter": {"visibility": "ALL"}, "limit": 1000, "cursor": cursor})
        items = d.get("items") or d.get("result", {}).get("items", [])
        for it in items:
            for s in it.get("stocks", []):
                if s.get("type") == "fbs":
                    total += s.get("present", 0) or 0
        cursor = d.get("cursor", "")
        if not cursor or not items:
            break
        time.sleep(0.2)
    return {("OZFBS", "Озон FBS", "FBS", ""): total}


# ---------- Ozon FBO по складам ----------
def oz_fbo_stocks():
    total = 0; offset = 0
    for _ in range(20):
        d = _oz("/v2/analytics/stock_on_warehouses", {"limit": 1000, "offset": offset, "warehouse_type": "ALL"})
        rows = d.get("result", {}).get("rows", [])
        for r in rows:
            total += r.get("free_to_sell_amount", 0) or 0
        if len(rows) < 1000:
            break
        offset += 1000
        time.sleep(0.2)
    return {("OZFBO", "Озон FBO", "склады Ozon (все)", ""): total}


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(st):
    os.makedirs("data", exist_ok=True)
    json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False, indent=2)


def main():
    current = {}
    # FBO отключён по решению владельца (2026-09-11): «будет каша» — Ozon/WB перекладывают
    # товар между десятками РФЦ, суммарный сигнал невнятный. Оставлен ТОЛЬКО FBS (по складам).
    for fn in (wb_fbs_stocks, oz_fbs_total):
        try:
            current.update(fn())
        except Exception as e:
            print("источник упал:", fn.__name__, str(e)[:80])

    st = load_state()
    initialized = st.get("_init", False)
    alerts = []
    for (key, scheme, name, wid), qty in current.items():
        last = st.get(key)
        # рост от базы = приход; впервые увиденный ключ считаем приходом ТОЛЬКО для нового
        # WB-FBS склада (появился склад с товаром). Суммарные FBO/Ozon-ключи при первом
        # появлении (например, источник восстановился после сбоя) НЕ алертим — просто база.
        increased = last is not None and qty - last >= ARRIVAL_MIN
        new_wh = last is None and qty >= ARRIVAL_MIN and key.startswith("WBFBS|")
        grew = increased or new_wh
        print(f"  {scheme:8} {name[:24]:24} остаток={qty} (было {last}) {'ПРИХОД' if grew and initialized else ''}")
        if initialized and grew:
            delta = qty - last if last is not None else qty
            if "FBS" in scheme:
                alerts.append(f"<b>Товар приехал на склад {name} ({scheme}): {qty} шт остатка.</b>\n"
                              f"Склад {wid} готов к заказам.")
            else:  # FBO — поставка на маркетплейс (сумма по платформе)
                alerts.append(f"<b>Поставка принята: {scheme} — +{delta} шт (всего {qty} на складах).</b>")
        st[key] = qty
    st["_init"] = True

    if not initialized:
        print(f"первичная база ({len(current)} складов) — алерты НЕ шлём")
    for msg in alerts:
        print("ALERT:", msg.replace("\n", " / "))
        if not DRY:
            notify.send(msg); time.sleep(0.5)
    if not DRY:
        save_state(st)
    print(f"итого складов: {len(current)} | приходов-алертов: {len(alerts) if initialized else 0}")


if __name__ == "__main__":
    main()
