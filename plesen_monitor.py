#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Монитор волны отзывов про ПЛЕСЕНЬ (WB + Ozon).

Логика (по требованию владельца):
- НЕ пересылать сами тексты отзывов — только динамика.
- Копим НОВЫЕ отзывы, где текст упоминает плесень/гниль (дедуп по id/uuid).
- Если за трейлинг-окно 14 дней набралось >= POROG (5) новых таких отзывов —
  один раз шлём в канал: «Зафиксировал N отзывов про плесень за период ДД.ММ–ДД.ММ»
  (+ разбивка WB/Ozon и по артикулам). Пока волна держится — повторно не спамим;
  перевзводимся, когда за 14 дней снова < RESET (3).
- Первый прогон — тихий (только базовая инициализация seen), без алерта.

Источники:
- WB: feedbacks-api (секрет WB_TOKEN), все товары сразу, dateFrom = -30 дней.
- Ozon: composer-api через curl_cffi(chrome) (обход антибота), по списку дентал-артикулов;
  offer_id -> sku (Seller API OZON_CLIENT_ID/OZON_API_KEY) -> slug -> reviews.
"""
from __future__ import annotations
import os, re, json, ssl, time, urllib.request, urllib.error
from datetime import datetime, timezone

import notify
import state

POROG = 5          # порог алерта: новых «плесневых» за 14 дней
RESET = 3          # ниже этого за 14 дней — снова готовы алертить
WINDOW_DAYS = 14
MOLD_RE = re.compile(r"плесен|плеснев|заплесн|гнил|протух|тухл", re.IGNORECASE)

# Ozon: мониторим ВСЕ дентал-салфетки (список тянется динамически из Seller API,
# чтобы автоматически подхватывать новые). WB и так ловит все товары через feedbacks.
CTX = ssl._create_unverified_context()


def _oz_headers():
    return {"Client-Id": os.environ.get("OZON_CLIENT_ID", "189077"),
            "Api-Key": os.environ.get("OZON_API_KEY", "cabc217d-d73a-4956-8f78-f2cd021035ae"),
            "Content-Type": "application/json"}


def oz_dental_offers():
    """Все дентал-салфетки на Ozon (offer_id содержит 'dental'), кроме списанного Animal."""
    body = json.dumps({"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1000}).encode()
    req = urllib.request.Request("https://api-seller.ozon.ru/v3/product/list", data=body,
                                 headers=_oz_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as x:
            d = json.loads(x.read().decode())
    except Exception as e:
        print("Ozon product/list err:", str(e)[:80]); return []
    offers = [it.get("offer_id") for it in (d.get("result") or {}).get("items", [])]
    return [o for o in offers if o and "dental" in o.lower() and "animal" not in o.lower()]


# ---------- WB ----------
def wb_mold():
    token = os.environ.get("WB_TOKEN", "").strip()
    if not token:
        print("нет WB_TOKEN — пропускаю WB"); return []
    base = "https://feedbacks-api.wildberries.ru/api/v1/feedbacks"
    date_from = int(time.time()) - 30 * 86400
    out = []
    for answered in ("false", "true"):
        params = f"?isAnswered={answered}&take=5000&skip=0&order=dateDesc&dateFrom={date_from}"
        req = urllib.request.Request(base + params, headers={"Authorization": token})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
                    d = json.loads(r.read().decode())
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    wait = int(e.headers.get("X-Ratelimit-Retry", "60")) + 3
                    print(f"WB 429 -> жду {wait}s"); time.sleep(wait); continue
                print("WB ошибка", e.code); d = {}; break
        for f in (d.get("data") or {}).get("feedbacks") or []:
            txt = " ".join([f.get("text") or "", f.get("pros") or "", f.get("cons") or ""])
            if MOLD_RE.search(txt):
                pd = f.get("productDetails") or {}
                out.append({"id": "wb_" + str(f.get("id")), "platform": "WB",
                            "product": pd.get("supplierArticle") or str(pd.get("nmId")),
                            "date": (f.get("createdDate") or "")[:10],
                            "score": f.get("productValuation")})
        time.sleep(1.5)
    return out


# ---------- Ozon ----------
def oz_sku(offer_id):
    body = json.dumps({"offer_id": [offer_id]}).encode()
    req = urllib.request.Request("https://api-seller.ozon.ru/v3/product/info/list", data=body,
        headers=_oz_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as x:
            d = json.loads(x.read().decode())
    except Exception as e:
        print("Ozon sku err", offer_id, str(e)[:80]); return None
    items = d.get("items") or (d.get("result") or {}).get("items") or []
    return str(items[0]["sku"]) if items else None


def oz_mold():
    try:
        from curl_cffi import requests as creq
    except Exception as e:
        print("нет curl_cffi — пропускаю Ozon:", str(e)[:80]); return []
    cutoff = int(time.time()) - 30 * 86400
    base = "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url="

    # одна «прогретая» сессия на все товары — так антибот видит нас как обычный браузер
    sess = creq.Session(impersonate="chrome")
    try:
        sess.get("https://www.ozon.ru/", timeout=30); time.sleep(1.5)
    except Exception:
        pass

    def scrape_one(offer, sku):
        """Собрать плесневые отзывы одного товара. Возвращает список или None (сбой антибота)."""
        s = sess
        s.get(f"https://www.ozon.ru/product/{sku}/", timeout=30)
        d = json.loads(s.get(base + f"%2Fproduct%2F{sku}%2F", timeout=30).text)
        canon = next((l["href"] for l in (d.get("seo") or {}).get("link", []) if l.get("rel") == "canonical"), None)
        if not canon or str(sku) not in canon:
            return None
        slug = canon.replace("https://www.ozon.ru", "").rstrip("/")
        res, q, page = [], f"{slug}/reviews/", 1
        while page <= 6:
            r = s.get(base + creq.utils.quote(q, safe=""), timeout=30)
            wl = None
            try:
                w = json.loads(r.text)["widgetStates"]
                wk = [k for k in w if k.startswith("webListReviews")]
                if wk:
                    wl = json.loads(w[wk[0]])
            except Exception:
                return None            # не-JSON/челлендж → считаем сбоем
            if wl is None:
                break
            revs = wl.get("reviews") or []
            if revs and str(revs[0].get("itemId")) != str(sku):
                return None            # пришла чужая карточка → сбой, повторим
            old = False
            for rv in revs:
                if str(rv.get("itemId")) != str(sku):
                    continue
                ts = rv.get("publishedAt") or rv.get("createdAt") or 0
                if ts and ts < cutoff:
                    old = True; continue
                if not ts:
                    continue
                c = rv.get("content") or {}
                txt = " ".join([c.get("comment", ""), c.get("positive", ""), c.get("negative", "")])
                if MOLD_RE.search(txt):
                    res.append({"id": "oz_" + str(rv.get("uuid")), "platform": "Ozon",
                                "product": offer,
                                "date": time.strftime("%Y-%m-%d", time.gmtime(ts)),
                                "score": c.get("score")})
            nxt = None
            for lk in (wl.get("paging") or {}).get("links") or []:
                mm = re.search(r"page=(\d+)", lk.get("urlParams", ""))
                if mm and int(mm.group(1)) == page + 1:
                    nxt = lk["urlParams"]; break
            if old or not revs or not nxt:
                break
            q = f"{slug}/reviews/{nxt}"; page += 1; time.sleep(0.5)
        return res

    offers = oz_dental_offers()
    print("Ozon дентал-артикулов в мониторинге:", len(offers))
    out = []
    for offer in offers:
        sku = oz_sku(offer)
        if not sku:
            continue
        got = None
        for attempt in range(3):          # ретраи: антибот под нагрузкой иногда даёт чужую карточку
            try:
                got = scrape_one(offer, sku)
            except Exception as e:
                got = None; print(f"  {offer} попытка {attempt+1} ошибка: {str(e)[:80]}")
            if got is not None:
                break
            time.sleep(2 + attempt * 2)
        if got is None:
            print(f"  {offer}: не удалось получить свои отзывы за 3 попытки — пропуск (WB подстрахует)")
        else:
            out += got
        time.sleep(2)                      # пауза между товарами — не злить антибот
    return out


def main():
    st = state.load()
    node = st.get("plesen") or {}
    seen = set(node.get("seen", []))
    events = node.get("events", [])      # [{id,platform,product,date,score}]
    alerted = node.get("alerted", False)
    initialized = node.get("initialized", False)

    found = wb_mold() + oz_mold()
    new = [f for f in found if f["id"] not in seen]
    for f in new:
        seen.add(f["id"])

    if not initialized:
        # первый прогон — помечаем текущие отзывы как известные, events НЕ копим
        # (иначе базовый уровень сработал бы как «волна» на след. запуске), алерт не шлём
        node.update({"seen": list(seen)[-5000:], "events": [],
                     "alerted": False, "initialized": True})
        st["plesen"] = node; state.save(st)
        print(f"первичная инициализация: {len(new)} отзывов помечены как база, алерт не шлём")
        return

    # дальше — копим ТОЛЬКО новые (появившиеся после инициализации), чистим старше 45 дней
    known_ids = {e["id"] for e in events}
    for f in new:
        if f["id"] not in known_ids:
            events.append(f)
    today = datetime.now(timezone.utc).date()
    def days_ago(ds):
        try: return (today - datetime.strptime(ds, "%Y-%m-%d").date()).days
        except Exception: return 999
    events = [e for e in events if days_ago(e["date"]) <= 45]

    # окно 14 дней
    win = [e for e in events if days_ago(e["date"]) <= WINDOW_DAYS]
    n = len(win)
    print(f"новых плесневых: {len(new)} | в окне {WINDOW_DAYS}д: {n} | alerted={alerted}")

    if n >= POROG and not alerted:
        dates = sorted(e["date"] for e in win)
        d1 = datetime.strptime(dates[0], "%Y-%m-%d").strftime("%d.%m")
        d2 = datetime.strptime(dates[-1], "%Y-%m-%d").strftime("%d.%m")
        wb_n = sum(1 for e in win if e["platform"] == "WB")
        oz_n = sum(1 for e in win if e["platform"] == "Ozon")
        by_prod = {}
        for e in win:
            by_prod[e["product"]] = by_prod.get(e["product"], 0) + 1
        top = ", ".join(f"{k} ×{v}" for k, v in sorted(by_prod.items(), key=lambda x: -x[1])[:5])
        msg = (f"🧫 <b>Плесень в отзывах — волна.</b>\n"
               f"Зафиксировал <b>{n}</b> отзыв(ов) про плесень за период {d1}–{d2}.\n"
               f"Площадки: WB {wb_n} · Ozon {oz_n}.\n"
               f"Артикулы: {top}")
        notify.send(msg)
        alerted = True
        print("АЛЕРТ отправлен:", n)
    elif alerted and n < RESET:
        alerted = False
        print("волна спала — перевзвёлся")

    node.update({"seen": list(seen)[-5000:], "events": events, "alerted": alerted, "initialized": True})
    st["plesen"] = node; state.save(st)


if __name__ == "__main__":
    main()
