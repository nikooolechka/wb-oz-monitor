# -*- coding: utf-8 -*-
"""MPStats-разделы для бота анализа карточек: per-article (продажи/выручка/OOS/рост отзывов/
позиция/ключи), УЗКАЯ ниша (прямые конкуренты по типу товара), топ-бренды/продавцы, ценовые
сегменты, свободные подниши. ТОЛЬКО открытые данные MPStats (WB/Ozon; ЯМ/ДМ не покрывает).

ЖЕЛЕЗНЫЕ ПРАВИЛА (владелец 2026-10-24):
- Ниша = прямые конкуренты ТОГО ЖЕ ТИПА товара, не широкая категория. Слова include/exclude даёт
  ИИ под КАЖДЫЙ товар (в т.ч. исключение чужой аудитории: детское→без животных/18+, и т.п.).
- Доли брендов/продавцов — по ВСЕМ SKU из топ-100 (склейку НЕ схлопывать; дедуп только для витрины).
- НИКАКИХ подстановок наших кабинетных чисел — всем одинаково через MPStats.
- MPStats по продажам НЕДОСТОВЕРЕН (особенно склейки Ozon) — на дашборде висит красная сноска.
Сбой любого шага НЕ должен валить карточку: все публичные вызовы обёрнуты, возвращаем что собралось.
"""
import os, urllib.request, json, ssl, urllib.parse, statistics, re, datetime
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor

CTX = ssl._create_unverified_context()
TOKEN = os.environ.get("MPSTATS_TOKEN", "")  # секрет (репо публичный — в код НЕ класть)
MONTHS = {"01":"янв","02":"фев","03":"мар","04":"апр","05":"май","06":"июн",
          "07":"июл","08":"авг","09":"сен","10":"окт","11":"ноя","12":"дек"}

def _today():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)  # МСК

def _get(url):
    r = urllib.request.Request(url, headers={"X-Mpstats-TOKEN": TOKEN})
    return json.load(urllib.request.urlopen(r, timeout=40, context=CTX))

def _cat_page(mp, path, s, e, d1, d2):
    u = f"https://mpstats.io/api/{mp}/get/category?path={urllib.parse.quote(path)}&d1={d1}&d2={d2}"
    body = json.dumps({"startRow": s, "endRow": e, "sortModel": [{"colId": "revenue", "sort": "desc"}], "filterModel": {}}).encode()
    r = urllib.request.Request(u, data=body, headers={"X-Mpstats-TOKEN": TOKEN, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=40, context=CTX)).get("data") or []

def _itemurl(mp, sku):
    return f"https://www.wildberries.ru/catalog/{sku}/detail.aspx" if mp == "wb" else f"https://www.ozon.ru/product/{sku}/"

def _ranges():
    t = _today(); d2 = t.strftime("%Y-%m-%d"); d1 = (t - datetime.timedelta(days=183)).strftime("%Y-%m-%d")
    d30 = (t - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    return d1, d2, d30

# ---------- per-article: продажи/выручка/OOS/рост отзывов/позиция/ключи ----------
def mp_item(mp, sku):
    d1, d2, d30 = _ranges()
    base = f"https://mpstats.io/api/{mp}/get/item/{sku}"
    s = _get(base + f"/sales?d1={d1}&d2={d2}")
    s = [x for x in s if not x.get("no_data")]; s.sort(key=lambda x: x["data"])
    if not s: return None
    # дата появления
    first = None
    try:
        if mp == "wb":
            it = _get(base); first = (it.get("item", it) or {}).get("first_date")
        if not first:
            w = _get(base + "/sales?d1=2024-01-01&d2=" + d2); w = [x for x in w if not x.get("no_data")]; w.sort(key=lambda x: x["data"])
            if w: f = w[0]["data"]; first = f
    except Exception: pass
    fd = (f"{first[8:10]}.{first[5:7]}.{first[:4]}" if first and len(first) >= 10 else first)
    last30 = s[-30:]
    daily = [{"d": x["data"][5:], "u": x.get("sales") or 0, "r": int((x.get("sales") or 0) * (x.get("final_price") or 0))} for x in last30]
    mon = OrderedDict(); yrs = set()
    for x in s:
        k = x["data"][:7]; yrs.add(x["data"][:4]); mon.setdefault(k, {"u": 0, "r": 0, "cmax": 0})
        mon[k]["u"] += x.get("sales") or 0; mon[k]["r"] += int((x.get("sales") or 0) * (x.get("final_price") or 0)); mon[k]["cmax"] = max(mon[k]["cmax"], x.get("comments") or 0)
    monthly = [{"m": MONTHS[k[5:]], "u": v["u"], "r": v["r"]} for k, v in mon.items()]
    rg = []; prev = None
    for k, v in mon.items():
        c = v["cmax"]; rg.append({"m": MONTHS[k[5:]], "c": c, "delta": (c - prev if prev is not None else None)}); prev = c
    oos = []; cur = None
    for x in s:
        if x.get("balance") == 0:
            cur = [x["data"], x["data"]] if cur is None else [cur[0], x["data"]]
        else:
            if cur: oos.append(cur); cur = None
    if cur: oos.append(cur)
    fm = lambda dd: f"{dd[8:10]}.{dd[5:7]}"
    oos_txt = [f"{fm(a)} по {fm(b)}" if a != b else fm(a) for a, b in oos]
    # Остаток: берём ПОСЛЕДНИЙ день; если сегодня 0 (бывает артефактом) — смотрим вчера.
    # Если и вчера 0 — товар РЕАЛЬНО не в наличии → balance_oos.
    recent = [x.get("balance") for x in s[-2:]]
    bal = None
    for b in reversed(recent):
        if b: bal = b; break
    bal_oos = not bal
    out = {"first_date": fd, "balance": (bal or 0), "balance_oos": bal_oos, "sales30_u": sum(x["u"] for x in daily), "sales30_r": sum(x["r"] for x in daily),
           "daily": daily, "daily_from": daily[0]["d"].replace("-", ".") if daily else "", "daily_to": (last30[-1]["data"][8:10] + "." + last30[-1]["data"][5:7] + "." + last30[-1]["data"][:4]) if last30 else "",
           "monthly": monthly, "months_year": "–".join(sorted(yrs)), "reviews_growth": rg, "oos": oos_txt,
           "position": None, "keywords": None, "buyout": None, "updated": d2}
    # daily_from в формате ДД.ММ
    if daily: out["daily_from"] = f"{last30[0]['data'][8:10]}.{last30[0]['data'][5:7]}"
    # WB: позиция в категории + ключи
    if mp == "wb":
        try:
            pos = [{"d": x["data"][5:], "p": x.get("category_position_avg")} for x in last30 if x.get("category_position_avg")]
            if pos:
                cnt = last30[-1].get("category_count")
                catname = None
                try:
                    bc = _get(base + "/by_category"); cats = list((bc.get("categories") or {}).keys())
                    catname = max(cats, key=lambda c: len(c.split("/"))) if cats else None
                except Exception: pass
                out["position"] = {"category": catname or "—", "from": pos[0]["d"].replace("-", "."), "to": pos[-1]["d"].replace("-", "."), "points": pos, "count": cnt}
        except Exception: pass
        try:
            kw = _get(base + "/by_keywords").get("words", {}); seen = set(); cand = []
            for q, w in (kw.items() if isinstance(kw, dict) else []):
                nq = w.get("norm_query") or q
                if nq in seen or len(nq.split()) < 2: continue
                ap = w.get("avgPos") or w.get("avg_pos") or 0; tot = w.get("total") or 0
                if not ap: continue
                seen.add(nq); cand.append({"q": nq, "pos": int(ap), "total": int(tot)})
            cand.sort(key=lambda z: z["total"], reverse=True)
            if cand: out["keywords"] = cand[:3]
        except Exception: pass
    return out

# ---------- узкая ниша + бренды/продавцы + сегменты ----------
def _mk_match(itype, ictx, exclude, aud_exclude):
    # Конкурент = совпала ФОРМА товара (itype) И НАЗНАЧЕНИЕ (ictx). Не ИЛИ — иначе «салфет» ловит
    # все детские салфетки. Если ictx пуст — только по форме. aud_exclude режет чужую аудиторию.
    typ = [t.lower() for t in (itype or []) if t]
    ctx = [t.lower() for t in (ictx or []) if t]
    exc = [t.lower() for t in (exclude or []) if t]
    aud = [t.lower() for t in (aud_exclude or []) if t]
    def m(name):
        n = (name or "").lower()
        if aud and any(a in n for a in aud): return False
        core = bool(typ) and any(t in n for t in typ) and (not ctx or any(c in n for c in ctx))
        if exc and any(e in n for e in exc) and not core: return False
        return core
    return m

def mp_niche(mp, sku, cats, itype, ictx, exclude, aud_exclude, label):
    d1, d2, d30 = _ranges()
    match = _mk_match(itype, ictx, exclude, aud_exclude)
    # Страницы категорий тянем ПАРАЛЛЕЛЬНО (независимые запросы) — главный выигрыш по времени.
    clist = (cats or [])[:3]
    tasks = [(c, s0) for c in clist for s0 in range(0, 300, 100)]
    def _fetch(t):
        c, s0 = t
        try: return t, _cat_page(mp, c, s0, s0 + 100, d30, d2)
        except Exception: return t, []
    res = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, data in ex.map(_fetch, tasks):
            res[t] = data
    pool = []; seen = set()
    for c in clist:
        for s0 in range(0, 300, 100):
            data = res.get((c, s0)) or []
            if not data: break   # для этой категории дальше пусто — как при последовательном сборе
            for x in data:
                if x.get("id") not in seen: seen.add(x.get("id")); pool.append(x)
    items = sorted([x for x in pool if match(x.get("name"))], key=lambda z: z.get("revenue") or 0, reverse=True)
    if not items: return None, None
    top100 = items[:100]
    tot_rev = sum(x.get("revenue") or 0 for x in top100); tot_sales = sum(x.get("sales") or 0 for x in top100)
    prices = [x.get("final_price") for x in top100 if x.get("final_price")]
    avg_price = round(sum(prices) / len(prices)) if prices else None
    bd = defaultdict(lambda: [0, 0]); sd = defaultdict(lambda: [0, 0])
    for x in top100:
        b = x.get("brand") or "—"; s = x.get("seller") or "—"; rv = x.get("revenue") or 0
        bd[b][0] += rv; bd[b][1] += 1; sd[s][0] += rv; sd[s][1] += 1
    brands = sorted([{"name": k, "rev": v[0], "items": v[1], "share": round(v[0] / tot_rev * 100) if tot_rev else 0} for k, v in bd.items()], key=lambda z: z["rev"], reverse=True)[:6]
    sellers = sorted([{"name": k, "rev": v[0], "items": v[1], "share": round(v[0] / tot_rev * 100) if tot_rev else 0} for k, v in sd.items()], key=lambda z: z["rev"], reverse=True)[:6]
    this_item = next((x for x in top100 if str(x.get("id")) == str(sku)), None)
    this_rev = this_item.get("revenue") if this_item else None
    seen2 = set(); disp = []
    for x in top100:
        iid = x.get("itemid") or x.get("id")
        if iid in seen2: continue
        seen2.add(iid); disp.append(x)
    our_rank = next((i + 1 for i, x in enumerate(disp) if str(x.get("id")) == str(sku)), None)
    top5 = [{"name": (x.get("name") or "")[:70], "brand": x.get("brand") or "—", "rev": x.get("revenue") or 0,
             "price": x.get("final_price") or 0, "rating": x.get("rating") or 0, "url": _itemurl(mp, x.get("id"))}
            for x in disp if str(x.get("id")) != str(sku)][:5]
    thr = 10000 if mp == "wb" else 5000
    young = sorted([x for x in disp if (x.get("comments") or 0) < thr and (x.get("sales") or 0) > 0 and str(x.get("id")) != str(sku)], key=lambda z: z.get("sales") or 0, reverse=True)[:3]
    rising = [{"name": (x.get("name") or "")[:60], "brand": x.get("brand") or "—", "sales": x.get("sales") or 0, "comments": x.get("comments") or 0, "url": _itemurl(mp, x.get("id"))} for x in young]
    bk = [(0, 500), (500, 800), (800, 1100), (1100, 1500), (1500, 10 ** 9)]; bl = ["до 500", "500–800", "800–1100", "1100–1500", "1500₽+"]
    seg = []; ts = sum(x.get("sales") or 0 for x in disp); tr = sum(x.get("revenue") or 0 for x in disp)
    for i, (lo, hi) in enumerate(bk):
        gs = [x for x in disp if lo <= (x.get("final_price") or 0) < hi]
        seg.append({"label": bl[i], "n": len(gs), "sales_pct": round(sum(x.get("sales") or 0 for x in gs) / ts * 100) if ts else 0, "rev_pct": round(sum(x.get("revenue") or 0 for x in gs) / tr * 100) if tr else 0})
    niche = {"name": label or "ниша", "total": len(disp), "win_rev": tot_rev, "win_sales": tot_sales, "win_n": len(disp),
             "avg_price": avg_price, "price_min": min(prices) if prices else None, "price_med": round(statistics.median(prices)) if prices else None, "price_max": max(prices) if prices else None,
             "our_rev": this_rev, "our_rank": our_rank, "top5": top5, "narrow": True, "segments": seg}
    market = {"brands": brands, "sellers": sellers, "rising": rising, "win_rev": tot_rev}
    return niche, market

# ---------- свободные подниши (подкатегории родителя: спрос ÷ конкуренция) ----------
def mp_free(mp, cats):
    # родитель = путь на один уровень выше самой глубокой категории товара
    deepest = max(cats, key=lambda c: len(c.split("/"))) if cats else None
    if not deepest or "/" not in deepest: return None
    parent = deepest.rsplit("/", 1)[0]
    d1, d2, d30 = _ranges()
    try:
        u = f"https://mpstats.io/api/{mp}/get/category/subcategories?path={urllib.parse.quote(parent)}&d1={d30}&d2={d2}"
        arr = _get(u)
    except Exception:
        return None
    if not isinstance(arr, list) or not arr: return None
    rows = []
    for s in arr:
        if (s.get("name") or "").lower().startswith("текущ"): continue  # сам родитель
        its = s.get("items_with_sells") or 0; rev = s.get("revenue") or 0
        rows.append({"name": s.get("name"), "brands": s.get("brands") or 0,
                     "rev_per": round(rev / its) if its else 0, "rating": round(s.get("rating") or 0, 2)})
    if not rows: return None
    brs = sorted(r["brands"] for r in rows); med_br = brs[len(brs) // 2]
    rps = sorted(r["rev_per"] for r in rows); med_rp = rps[len(rps) // 2]
    out = []
    for r in sorted(rows, key=lambda z: z["rev_per"], reverse=True)[:6]:
        out.append({**r, "opp": (r["brands"] < med_br and r["rev_per"] >= med_rp)})
    return {"parent": parent.split("/")[-1], "items": out}

# ---------- фолбэк: найти категорию в дереве, если by_category пустой ----------
_CATS_CACHE = {}
def _all_categories(mp):
    if mp not in _CATS_CACHE:
        try: _CATS_CACHE[mp] = _get(f"https://mpstats.io/api/{mp}/get/categories")
        except Exception: _CATS_CACHE[mp] = []
    return _CATS_CACHE[mp]

def _find_category(mp, itype, ictx):
    """by_category пустой (MPStats не трекает позицию товара) ≠ категории нет. Ищем в дереве:
    листья, чьё имя содержит слово ФОРМЫ (itype), а путь/имя — слово НАЗНАЧЕНИЯ (ictx);
    валидируем, фетча категорию и считая совпадения по терминам товара. Берём самую «жирную»."""
    cats = _all_categories(mp)
    if not cats: return None
    _stem = lambda w: re.sub(r"[ьиыяейоёъаую]+$", "", w.lower())  # гель/гели→гел, салфетки→салфетк
    typ = [_stem(t) for t in (itype or []) if t]; ctx = [_stem(c) for c in (ictx or []) if c]
    typ = [t for t in typ if len(t) >= 3]
    if not typ: return None
    cand = []
    for c in cats:
        nm = (c.get("name") or "").lower(); pth = (c.get("path") or "").lower()
        if any(t in nm for t in typ) and (not ctx or any(x in pth or x in nm for x in ctx)):
            cand.append(c.get("path"))
    if not cand: return None
    d1, d2, d30 = _ranges()
    match = _mk_match(itype, ictx, [], [])
    best = None; best_rev = 0
    for p in cand[:8]:
        try: data = _cat_page(mp, p, 0, 100, d30, d2)
        except Exception: continue
        rev = sum(x.get("revenue") or 0 for x in data if match(x.get("name")))
        if rev > best_rev: best_rev = rev; best = p
    return best

# ---------- оркестратор ----------
def build_mpstats(platform, sku, url, name, niche_terms):
    """platform: 'WB'/'OZ'; sku: строка; niche_terms: {label, include, exclude, aud_exclude} от ИИ.
    Возвращает dict mpstats (per-article + niche + market) или None. Никогда не бросает."""
    mp = "wb" if platform == "WB" else ("oz" if platform == "OZ" else None)
    if not mp or not sku: return None
    # per-article и by_category НЕ зависят друг от друга — тянем ПАРАЛЛЕЛЬНО.
    res = {}; cats = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_item = ex.submit(mp_item, mp, str(sku))
        f_bc = ex.submit(_get, f"https://mpstats.io/api/{mp}/get/item/{sku}/by_category")
        try: res = f_item.result() or {}
        except Exception as e: print("mp_item fail:", repr(e)[:150]); res = {}
        try:
            bc = f_bc.result()
            cats = sorted(list((bc.get("categories") or {}).keys()), key=lambda c: len(c.split("/")), reverse=True)
        except Exception as e: print("by_category fail:", repr(e)[:120])
    nt = niche_terms or {}
    itype = nt.get("include_type") or []; ictx = nt.get("include_context") or []
    exc = nt.get("exclude") or []; aud = nt.get("aud_exclude") or []
    label = nt.get("label") or ""
    # Где строить нишу: по категориям самого товара; если нет — ищем в дереве ЕГО площадки;
    # если и там нет (MPStats не трекает категорию на этой площадке) — берём рынок того же типа
    # на ДРУГОЙ площадке (кросс-платформа). by_category пустой ≠ категории нет.
    niche_mp, niche_cats, cross = mp, cats, False
    other = "oz" if mp == "wb" else "wb"
    if not niche_cats and itype:
        try:
            fc = _find_category(mp, itype, ictx)
            if fc: niche_cats = [fc]; print("category fallback (same):", fc)
        except Exception as e:
            print("find_category fail:", repr(e)[:120])
    if not niche_cats and itype:
        try:
            fc2 = _find_category(other, itype, ictx)
            if fc2: niche_mp, niche_cats, cross = other, [fc2], True; print("category fallback (cross):", other, fc2)
        except Exception as e:
            print("find_category cross fail:", repr(e)[:120])
    if niche_cats and itype:
        niche_sku = str(sku) if niche_mp == mp else ""   # наш sku есть только на своей площадке
        try:
            niche, market = mp_niche(niche_mp, niche_sku, niche_cats, itype, ictx, exc, aud, label)
            if niche:
                if cross:
                    niche["platform_note"] = ("Ниша и конкуренты — по рынку этого типа товара на "
                        + ("Ozon" if niche_mp == "oz" else "WB")
                        + " (на площадке товара MPStats его категорию не отслеживает).")
                res["niche"] = niche
            if market: res["market"] = market
        except Exception as e:
            print("mp_niche fail:", repr(e)[:150])
        try:
            fr = mp_free(niche_mp, niche_cats)
            if fr and res.get("niche"): res["niche"]["free"] = fr
        except Exception as e:
            print("mp_free fail:", repr(e)[:120])
    # Если per-article собрался, но ниши нет — честная пометка (не выдумываем нишу).
    if res and res.get("sales30_u") is not None and not res.get("niche"):
        res["no_niche_reason"] = ("MPStats не категоризирует этот товар — рыночной ниши и конкурентов по нему в MPStats нет."
                                  if not cats else
                                  "По этому товару MPStats не нашёл прямых конкурентов того же типа.")
    return res or None
