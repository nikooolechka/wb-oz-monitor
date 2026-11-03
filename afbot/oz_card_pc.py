# -*- coding: utf-8 -*-
# Ozon card collector НА ПК (РФ-IP). ЗАЛОГИНЕННЫЙ Chrome (uc, ozprofile) — видит реальную
# (персональную/акционную) цену; HTTP — только запасной, если браузер не поднялся.
# Собирает ВСЕ отзивы постранично и САМ считает разбивку по звёздам. Печатает OZCARD64<<base64(json)>>.
import sys, os, json, re, base64, time, glob
from collections import Counter
# Аргумент может быть ПОЛНЫМ адресом карточки (с ЧПУ-названием, напр.
# /product/salfetki-...-40sht-3731025845/) ИЛИ просто номером SKU. Полный адрес с названием
# однозначно указывает на нужный вариант — Ozon по нему НЕ редиректит на основной вариант
# группы (в отличие от голого /product/<номер>/, который изредка подменялся). Поэтому, если
# пришёл полный адрес — ходим именно по нему.
ARG = sys.argv[1] if len(sys.argv) > 1 else "1829369911"
NONCE = sys.argv[2] if len(sys.argv) > 2 else ""   # разовый код запроса — облако примет результат
                                                    # ТОЛЬКО с этим кодом (защита от путаницы пульта)
if "/product/" in ARG:
    PRODUCT_PATH = ARG.split("?")[0].split("#")[0]
    if not PRODUCT_PATH.startswith("/"):
        PRODUCT_PATH = "/product/" + PRODUCT_PATH.split("/product/", 1)[1]
    if not PRODUCT_PATH.endswith("/"): PRODUCT_PATH += "/"
    _m = re.search(r"(\d{6,})", PRODUCT_PATH)
    PID = _m.group(1) if _m else ARG
else:
    PID = re.sub(r"\D", "", ARG) or ARG
    PRODUCT_PATH = f"/product/{PID}/"
REVIEW_BASE = PRODUCT_PATH.rstrip("/")  # для постраничных отзывов того же варианта
BASE = "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url="
D = os.path.join(os.environ.get("USERPROFILE","."), "asfarm")

def served_skus(pj):
    # ВСЕ SKU, которые реально фигурируют на отданной Ozon странице (из разных мест). Запрошенный
    # PID должен быть СРЕДИ них — иначе Ozon отдал ДРУГОЙ товар (вариант группы). Собираем из
    # максимума источников, чтобы надёжно сверить (23.10.2026: без-вкуса отдавал банан, одиночный
    # источник промахивался → публиковался чужой товар).
    s=set()
    def add(x):
        x=re.sub(r"\D","",str(x or ""))
        if len(x)>=6: s.add(x)
    try: add(((pj.get("pageInfo") or {}).get("analyticsInfo") or {}).get("sku"))
    except Exception: pass
    try:
        pu=(pj.get("pageInfo") or {}).get("url") or ""
        m=re.search(r"-(\d{6,})/?(?:\?|$)", pu) or re.search(r"/product/(\d{6,})", pu)
        if m: add(m.group(1))
    except Exception: pass
    seo=pj.get("seo") or {}
    try:
        for sc in (seo.get("script") or []):
            if sc.get("type")=="application/ld+json":
                d=json.loads(sc.get("innerHTML") or "{}")
                add(d.get("sku"))
                off=d.get("offers") or {}
                if isinstance(off,dict):
                    m=re.search(r"-(\d{6,})/?$",(off.get("url") or "").rstrip("/"))
                    if m: add(m.group(1))
    except Exception: pass
    try:
        for ln in (seo.get("link") or []):
            if ln.get("rel") in ("canonical","alternate"):
                m=re.search(r"-(\d{6,})/?$", (ln.get("href","") or "").rstrip("/"))
                if m: add(m.group(1))
    except Exception: pass
    # webDetailSKU (кнопка "Артикул: N") + webProductHeading
    ws=pj.get("widgetStates") or {}
    for k,v in ws.items():
        pfx=k.split("-")[0]
        if pfx in ("webDetailSKU","webProductHeading","webReviewProductScore","webSingleProductScore"):
            try:
                d=json.loads(v)
                for fld in ("copyText",):
                    add(d.get(fld))
                add((d.get("button") or {}).get("text"))
                add(d.get("itemId")); add(d.get("sku"))
            except Exception: pass
    return s

def served_sku(pj):
    sk=served_skus(pj)
    return next(iter(sk)) if len(sk)==1 else (sorted(sk)[0] if sk else None)

def parse_ws(ws):
    out = {}
    def find(pfx):
        for k,v in ws.items():
            if k.split("-")[0]==pfx:
                try: return json.loads(v)
                except: return None
        return None
    def find_all(pfx):
        r=[]
        for k,v in ws.items():
            if k.split("-")[0]==pfx:
                try: r.append(json.loads(v))
                except: pass
        return r
    h=find("webProductHeading") or {}
    out["name"]=h.get("title")
    pr=find("webPrice") or {}
    dig=lambda x: (re.sub(r"\D","",str(x)) or None) if x is not None else None
    out["price_card"]=dig(pr.get("cardPrice")); out["price"]=dig(pr.get("price")); out["price_old"]=dig(pr.get("originalPrice"))
    # Наличие — с ЖИВОЙ карточки (есть/нет): есть кнопка в корзину / цена и нет виджета «нет в наличии»
    _wk=" ".join(ws.keys()).lower()
    out["in_stock"]=("outofstock" not in _wk and "soldout" not in _wk) and (("addtocart" in _wk) or bool(out.get("price")) or bool(out.get("price_card")))
    sc=find("webReviewProductScore") or find("webSingleProductScore") or {}
    out["rating"]=sc.get("totalScore") or sc.get("score")
    out["reviews_count"]=sc.get("reviewsCount") or sc.get("itemsCount") or sc.get("commentsCount")
    g=find("webGallery") or {}
    imgs=g.get("images") or g.get("coverImage") or []
    if isinstance(imgs,dict): imgs=[imgs]
    out["gallery"]=[i.get("src") or i.get("image") for i in imgs if isinstance(i,dict)][:12]
    ch=find("webShortCharacteristics") or find("webCharacteristics") or {}
    out["chars"]=ch if ch and len(json.dumps(ch,ensure_ascii=False))<3000 else None
    # ТЕКСТ КАРТОЧКИ для анализа «чем цепляет покупателя» (что обещает продавец): у Ozon нет
    # стабильного поля описания в API, поэтому собираем характеристики + описание/рич-контент,
    # что есть. Без этого ИИ не из чего вытащить обещания продавца → блок выходил пустым.
    cparts=[]
    def _walk(o):
        if isinstance(o,dict):
            for kk,vv in o.items():
                if kk in ("text","title","content","value","shortName","name") and isinstance(vv,str) and 1<len(vv)<300:
                    cparts.append(vv)
                else: _walk(vv)
        elif isinstance(o,list):
            for it in o: _walk(it)
    _walk(ch)
    for wn in ("webDescription","webRichContent","richAnnotation","webCharacteristics","webProductHeading"):
        w=find(wn)
        if w: _walk(w)
    seen_c=set(); uniq=[]
    for s in cparts:
        s=s.strip()
        if s and s not in seen_c: seen_c.add(s); uniq.append(s)
    out["content_for_analysis"]=(" · ".join(uniq))[:2500] or None
    # «Другие товары под карточкой» = варианты из webAspects (аналог WB-склейки): вкусы/фасовки
    # и т.п. Берём ЧИТАЕМЫЙ ярлык каждого варианта (напр. "Банан", "Без вкуса", "40 шт").
    skl=[]
    asp=find("webAspects") or {}
    for a in (asp.get("aspects") or []):
        aname=(a.get("aspectName") or "").lower()
        unit = "шт" if ("едини" in aname or "товаре" in aname) else ""
        for var in (a.get("variants") or []):
            data=var.get("data") or {}
            label=(data.get("searchableText") or "").strip()
            if not label: continue
            chip=(label+" "+unit).strip() if unit else label
            if chip not in skl: skl.append(chip)
    out["skleyka"]=skl[:16]
    revs=[]
    for w in find_all("webListReviews")+find_all("webReviews"):
        for rv in (w.get("reviews") or w.get("items") or []):
            c=rv.get("content") or rv
            ts = rv.get("publishedAt") or c.get("publishedAt")
            date_iso = ""
            if ts:
                try:
                    import datetime
                    date_iso = datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                except Exception: pass
            revs.append({"s":rv.get("score") or c.get("score"),
                         "t":(c.get("comment") or c.get("text") or "")[:300],
                         "p":(c.get("positive") or "")[:120],"m":(c.get("negative") or "")[:200],
                         "date":date_iso,
                         "uuid": rv.get("uuid") or c.get("uuid") or "",   # для НАДЁЖНОГО дедупа
                         # ID ТОВАРА, к которому Ozon прикрепил отзыв. ПО НЕМУ фильтруем отзывы
                         # к нужному артикулу — а НЕ по словам в тексте (владелец 23.10.2026:
                         # «только по айди»). Эндпоинт отдаёт общий пул группы, у каждого свой itemId.
                         "item_id": str(rv.get("itemId") or c.get("itemId") or "")})
    out["reviews"]=revs
    return out

def _flavor_sets(ws_main, pid):
    """Из webAspects главной страницы: (все вкусы группы, вкус ЗАПРОШЕННОГО товара).
    Вкус нужен, чтобы выкинуть из анализа отзывы про другие вкусы (напр. 'без вкуса' у банана)."""
    group=set(); mine=None
    try:
        asp=None
        for k,v in ws_main.items():
            if k.split("-")[0]=="webAspects":
                try: asp=json.loads(v); break
                except Exception: pass
        for a in ((asp or {}).get("aspects") or []):
            an=(a.get("aspectName") or "").lower()
            if "аромат" not in an and "вкус" not in an and "цвет" not in an: continue
            for var in (a.get("variants") or []):
                lab=((var.get("data") or {}).get("searchableText") or "").strip()
                if not lab: continue
                group.add(lab)
                if str(var.get("sku"))==str(pid): mine=lab
    except Exception: pass
    return group, mine

def _apply_flavor_to_name(out, my_flavor):
    # Вкус варианта В НАЗВАНИЕ — чтобы карточки разных артикулов одной группы были ЯВНО разными
    # (Ваниль vs Без аромата vs Малина), даже если базовый заголовок у них общий.
    nm=out.get("name") or ""
    if my_flavor and nm and my_flavor.lower() not in nm.lower():
        out["name"]=f"{nm} — {my_flavor}"
    out["variant_flavor"]=my_flavor

def filter_by_item_id(allrev, pid):
    """ТОЛЬКО ПО ID (владелец 23.10.2026): оставляем отзывы, у которых itemId == артикул
    анализируемого товара, независимо от того, что написано в тексте. Чужие по itemId — убираем.
    Ozon отдаёт общий пул группы, но каждый отзыв прикреплён к своему SKU — берём свои по ID.
    Если у отзывов itemId не проставлен (поле пустое у всех) — не режем (оставляем как есть,
    чтобы не потерять отзывы там, где Ozon ID не отдал)."""
    pid=str(pid)
    tagged=[r for r in allrev if r.get("item_id")]
    if not tagged:
        return allrev  # ни у одного нет itemId — резать нечем, оставляем всё
    return [r for r in allrev if str(r.get("item_id") or "")==pid]

def _paging(ws):
    """Из виджета отзывов: (links, total). Ozon листает КУРСОРОМ page_key (простой ?page=N
    упирается в ~300), поэтому следующую страницу берём по urlParams из paging.links."""
    for k,v in ws.items():
        if k.split("-")[0] in ("webListReviews","webReviews"):
            try:
                pg=json.loads(v).get("paging") or {}
                return (pg.get("links") or []), (pg.get("total") or pg.get("commonTotal"))
            except Exception: return [],None
    return [],None

def paginate(get_ws, review_base):
    """Собираем ВСЕ отзывы эндпоинта по КУРСОРУ page_key (а не ?page=N, который даёт лишь ~300).
    Так доходим до total (736 у eilimix), а потом фильтруем по itemId. Дедуп по uuid."""
    seen=set(); allr=[]
    def key(r):
        u=r.get("uuid")
        return ("u", u) if u else ("st", r.get("s"), (r.get("t") or "")[:80], (r.get("date") or ""))
    params="?page=1"; cur_page=1; total=None
    for _ in range(120):   # 120 страниц × 30 = 3600 отзывов потолок, с запасом
        ws=get_ws(f"{review_base}/reviews/{params}")
        rv=parse_ws(ws).get("reviews") if ws else []
        if not rv: break
        for r in rv:
            k=key(r)
            if k in seen: continue
            seen.add(k); allr.append(r)
        links, tot = _paging(ws)
        if tot: total=tot
        total_pages = ((int(total)+29)//30) if total else (cur_page+1)
        if cur_page >= total_pages: break
        # page_key + sort из ссылок текущей страницы → строим следующую
        pk=srt=None
        for ln in links:
            up=ln.get("urlParams") or ""
            mk=re.search(r"page_key=([^&]+)", up); ms=re.search(r"sort=([^&]+)", up)
            if mk and not pk: pk=mk.group(1)
            if ms and not srt: srt=ms.group(1)
        if not pk: break   # нет курсора — дальше листать нечем
        cur_page+=1
        params=f"?page={cur_page}&page_key={pk}" + (f"&sort={srt}" if srt else "")
    return allr

def _aggregate_rating(pj):
    # Рейтинг/число отзывов из JSON-LD (seo.script) ЭТОЙ ЖЕ карточки — более надёжный источник,
    # чем иногда пустой виджет webReviewProductScore. НЕ трогает тексты отзывов и не подменяет
    # их с других SKU (отзывы банана и отзывы без-вкуса — РАЗНЫЕ товары, их нельзя путать —
    # владелец, 23.10.2026, жёстко). Если текстов отзывов по этому SKU нет — гейт в
    # analiz_process.py как и раньше честно откажется публиковать, это ПРАВИЛЬНО.
    try:
        for s in ((pj.get("seo") or {}).get("script") or []):
            if s.get("type") == "application/ld+json":
                d = json.loads(s.get("innerHTML") or "{}")
                ar = d.get("aggregateRating") or {}
                rating = ar.get("ratingValue"); cnt = ar.get("reviewCount")
                return (float(rating) if rating else None), (int(cnt) if cnt else None)
    except Exception: pass
    return None, None

def finalize(out, allrev, pid):
    # ФИЛЬТР ТОЛЬКО ПО ID: берём отзывы, прикреплённые Ozon к ЭТОМУ артикулу (itemId==pid),
    # независимо от текста. Чужие по ID убираем. НЕ по смыслу/словам (владелец 23.10.2026).
    before=len(allrev)
    out["reviews_pool_total"]=before   # сколько собрано со страниц ДО фильтра по ID (пул группы).
                                       # Нужно отличить ЧЕСТНЫЙ 0 (пул собран, но по этому itemId
                                       # нет ни одного) от ОШИБКИ сбора (пул пуст = антибот/сбой).
    allrev=filter_by_item_id(allrev, pid)
    out["reviews_dropped_other_variant"]=before-len(allrev)
    for r in allrev:
        r.pop("item_id", None)       # служебное наружу не нужно
        r.pop("uuid", None)
        r.pop("p", None)             # позитив-поле не нужно (pos берёт из t)
        if r.get("t"): r["t"]=r["t"][:300]
        if r.get("m"): r["m"]=r["m"][:200]
        if r.get("date"): r["date"]=r["date"][:10]
    out["reviews_all"]=len(allrev)   # ИСТИННОЕ число по этому артикулу (по ID)
    out["reviews"]=allrev            # все отзывы артикула (уйдут через большой канал)
    c=Counter(r.get("s") for r in allrev if r.get("s") in (1,2,3,4,5))
    if c:
        out["stars"]={str(k):c.get(k,0) for k in (5,4,3,2,1)}
        out["stars_collected"]=sum(c.values())
    return out

def collect_questions_oz(fetch_ws, pid):
    # Вопросы к товару (composer /product/PID/questions/). Фильтр itemId==pid (как отзывы, НЕ по словам).
    # ПУЛЕНЕПРОБИВАЕМО: что бы тут ни случилось — возвращаем [], сбор карточки это НИКОГДА не ломает.
    try:
        ws=fetch_ws(f"/product/{pid}/questions/")
        out=[]
        for k,v in (ws or {}).items():
            if not re.search(r"question",k,re.I): continue
            try: wv=json.loads(v)
            except Exception: continue
            if not isinstance(wv,dict): continue
            for q in (wv.get("questions") or []):
                if not isinstance(q,dict): continue
                if str(q.get("itemId"))!=str(pid): continue
                txt=re.sub(r"<[^>]+>","",((q.get("text") or {}).get("text") or "")).strip()
                ans=""
                for a in (q.get("answers") or []):
                    if not isinstance(a,dict): continue
                    at=re.sub(r"<[^>]+>","",((a.get("text") or {}).get("text") or "")).strip()
                    if at: ans=at; break
                if txt: out.append({"t":txt[:300],"a":ans[:300]})
            if out: break
        return out[:40]
    except Exception as e:
        print("questions collect err:", repr(e)[:120]); return []

def via_http():
    import urllib.request, gzip
    def get(u,data=None):
        h={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/155 Safari/537.36","Accept":"application/json","Accept-Language":"ru"}
        r=urllib.request.urlopen(urllib.request.Request(u,data=data,headers=h),timeout=40); b=r.read()
        if b[:2]==b"\x1f\x8b": b=gzip.decompress(b)
        return b
    try: get("https://www.ozon.ru/api/composer-api.bx/_action/setBirthdate?isAlco=false",data=b'{"birthdate":"1990-01-01"}')
    except: pass
    pj=json.loads(get(BASE+PRODUCT_PATH)); ws=pj.get("widgetStates") or {}
    if not ws: raise RuntimeError("no widgetStates")
    out=parse_ws(ws); out["_via"]="http"; out.pop("reviews",None)
    sks=served_skus(pj)
    if sks and str(PID) not in sks: out["_sku_mismatch"]=",".join(sorted(sks))
    ld_rating, ld_count = _aggregate_rating(pj)
    if not out.get("rating") and ld_rating: out["rating"]=ld_rating
    out["skleyka_reviews_count"]=ld_count or out.get("reviews_count")  # общий по ГРУППЕ вариантов, НЕ по товару
    out["reviews_count"]=None  # по одному SKU Ozon точного числа не даёт — покажем ТОЛЬКО собранные
    grp_fl, my_fl = _flavor_sets(ws, PID)
    _apply_flavor_to_name(out, my_fl)
    def get_ws(path):
        try: return json.loads(get(BASE+path)).get("widgetStates") or {}
        except: return {}
    out["questions"]=collect_questions_oz(get_ws, PID)
    return finalize(out, paginate(get_ws, REVIEW_BASE), PID)

def _chrome_ver():
    try:
        import winreg
        for hk in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for path in (r"Software\Google\Chrome\BLBeacon", r"Software\Wow6432Node\Google\Chrome\BLBeacon"):
                try:
                    k=winreg.OpenKey(hk,path); v,_=winreg.QueryValueEx(k,"version"); return int(v.split(".")[0])
                except Exception: pass
    except Exception: pass
    return None

def _make_driver():
    # Та же боевая логика, что в oz_run.py/wb_run.py (проверено живьём годами): перед стартом
    # убить зависшие chrome.exe/chromedriver (из-за них была SessionNotCreatedException —
    # поймано 23.10 на этом самом сборщике), почистить lock-файлы Singleton*, сбросить
    # раздутый Preferences (>50МБ — известный повторяющийся баг, см. CLAUDE.md), 3 попытки.
    import subprocess, shutil
    sys.path.insert(0, os.path.join(D,"py","Lib","site-packages"))
    import undetected_chromedriver as uc
    prof=os.path.join(D,"ozprofile"); ver=_chrome_ver()
    for im in ("chrome.exe","chromedriver.exe","undetected_chromedriver.exe"):
        try: subprocess.run(["taskkill","/F","/IM",im,"/T"],capture_output=True)
        except Exception: pass
    for lf in ("SingletonLock","SingletonCookie","SingletonSocket"):
        try: os.remove(os.path.join(prof,lf))
        except Exception: pass
    try:
        pref=os.path.join(prof,"Default","Preferences")
        if os.path.exists(pref) and os.path.getsize(pref) > 50*1024*1024:
            os.remove(pref)
            sp=os.path.join(prof,"Default","Secure Preferences")
            if os.path.exists(sp): os.remove(sp)
    except Exception: pass
    time.sleep(1)
    last=None
    for attempt in range(3):
        opts=uc.ChromeOptions(); opts.add_argument("--window-size=1200,900")
        kw={"user_data_dir":prof,"options":opts}
        if ver: kw["version_main"]=ver
        try:
            drv=uc.Chrome(**kw); drv.set_page_load_timeout(60); return drv
        except Exception as e:
            last=e
            for im in ("chromedriver.exe","undetected_chromedriver.exe"):
                try: subprocess.run(["taskkill","/F","/IM",im,"/T"],capture_output=True)
                except Exception: pass
            try: shutil.rmtree(os.path.join(os.environ.get("APPDATA",""),"undetected_chromedriver"),ignore_errors=True)
            except Exception: pass
            time.sleep(4)
    raise last

def _profile_busy():
    # Профиль ozprofile занят, если на нём уже держит сессию другой процесс Chrome
    # (напр. продовый сбор цен 10:00/17:00) — у него lock-файлы Singleton*.
    return bool(glob.glob(os.path.join(D,"ozprofile","Singleton*")))

def _simple_launch():
    # Чистый запуск БЕЗ taskkill — используется, пока профиль похож на занятый живым
    # процессом (ждём, не трогаем чужую сессию, см. _get_driver).
    sys.path.insert(0, os.path.join(D,"py","Lib","site-packages"))
    import undetected_chromedriver as uc
    prof=os.path.join(D,"ozprofile"); ver=_chrome_ver()
    opts=uc.ChromeOptions(); opts.add_argument("--window-size=1200,900")
    kw={"user_data_dir":prof,"options":opts}
    if ver: kw["version_main"]=ver
    drv=uc.Chrome(**kw); drv.set_page_load_timeout(60); return drv

def _get_driver(max_wait=120, interval=10):
    # 1) Если профиль похож на занятый (Singleton*-локи) — ждём до 2 минут, НЕ трогая
    #    ничего: это может быть живой продовый сбор цен (oz_run.py, та же папка ozprofile).
    # 2) Как только освободился — пробуем чистый запуск.
    # 3) Если так и не освободился ЗА 2 МИНУТЫ (похоже на забытые/осиротевшие lock-файлы
    #    после краша, а не на живой процесс) ИЛИ чистый запуск всё равно не поднялся
    #    (SessionNotCreatedException и т.п., поймано живьём 23.10) — самочинимся: убиваем
    #    зависшие chrome.exe/chromedriver, чистим Singleton*, сбрасываем раздутый Preferences
    #    (>50МБ — известный повторяющийся баг), пробуем ещё раз. Это проверенная логика из
    #    oz_run.py/wb_run.py — сюда просто донесена, чтобы та же беда не повторялась здесь.
    waited=0
    while _profile_busy() and waited<max_wait:
        time.sleep(interval); waited+=interval
    if not _profile_busy():
        try: return _simple_launch()
        except Exception: pass
    return _make_driver()

def via_browser():
    drv=_get_driver()
    try:
        drv.get("https://www.ozon.ru/")
        drv.execute_script("return fetch('/api/composer-api.bx/_action/setBirthdate?isAlco=false',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({birthdate:'1990-01-01'})}).then(r=>r.status).catch(e=>0)")
        def jfetch(path):
            return drv.execute_async_script("var cb=arguments[arguments.length-1];fetch(arguments[0]).then(r=>r.text()).then(t=>cb(t)).catch(e=>cb('ERR'+e))", path)
        # Берём страницу по ПОЛНОМУ адресу с ЧПУ-названием (PRODUCT_PATH) — он однозначно
        # указывает на нужный вариант, Ozon по нему не подменяет на основной вариант группы.
        # Если всё же отдал не тот SKU — пробуем ещё раз (до 4), и только если упорно даёт
        # чужой — помечаем mismatch (лучше честный отказ, чем анализ не того товара).
        # Пробуем оба адреса (полный slug и голый /product/PID/) — Ozon на один из них может
        # отдать нужный товар. Берём тот ответ, где запрошенный PID РЕАЛЬНО среди SKU страницы.
        pj={}; ws={}; paths=[PRODUCT_PATH]
        if PRODUCT_PATH!=f"/product/{PID}/": paths.append(f"/product/{PID}/")
        ok=False
        for _try in range(4):
            for pth in paths:
                pt=jfetch(BASE+pth)
                _pj=json.loads(pt) if pt and not pt.startswith("ERR") else {}
                if not _pj: continue
                pj=_pj; ws=pj.get("widgetStates") or {}
                sks=served_skus(pj)
                if sks and str(PID) in sks: ok=True; break
            if ok: break
            time.sleep(2.5)
        out=parse_ws(ws); out["_via"]="browser"; out.pop("reviews",None)
        sks=served_skus(pj)
        if sks and str(PID) not in sks: out["_sku_mismatch"]=",".join(sorted(sks))
        ld_rating, ld_count = _aggregate_rating(pj)
        if not out.get("rating") and ld_rating: out["rating"]=ld_rating
        out["skleyka_reviews_count"]=ld_count or out.get("reviews_count")  # общий по ГРУППЕ вариантов, НЕ по товару
        out["reviews_count"]=None  # по одному SKU Ozon точного числа не даёт — покажем ТОЛЬКО собранные
        grp_fl, my_fl = _flavor_sets(ws, PID)
        _apply_flavor_to_name(out, my_fl)
        def get_ws(path):
            t=jfetch(BASE+path)
            if not t or t.startswith("ERR"): return {}
            try: return json.loads(t).get("widgetStates") or {}
            except: return {}
        out["questions"]=collect_questions_oz(get_ws, PID)
        return finalize(out, paginate(get_ws, REVIEW_BASE), PID)
    finally:
        try: drv.quit()
        except: pass

res=None; err=[]
for fn in (via_browser, via_http):
    try:
        r=fn()
        if r.get("name"): res=r; break
        err.append(fn.__name__+":no name")
    except Exception as e:
        err.append(fn.__name__+":"+repr(e)[:80])
def _enc(o): return base64.b64encode(json.dumps(o,ensure_ascii=False).encode("utf-8")).decode()

def _sheets_write(rng, values):
    # Пишем в лист пульта через sa.json (есть на ПК у ценовых скриптов). Для БОЛЬШОГО канала
    # отзывов: кладём все отзывы (base64, кусками) в диапазон M10:M.. — ячейка одна мала (~50k),
    # а диапазон держит сколько угодно (1000+ отзывов полным текстом).
    import urllib.request, urllib.parse, ssl as _ssl
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GRequest
    import requests as _rq
    SID="1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"
    creds=service_account.Credentials.from_service_account_file(os.path.join(D,"sa.json"),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sess=_rq.Session(); sess.verify=False
    creds.refresh(GRequest(sess))
    url=f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{urllib.parse.quote(rng)}?valueInputOption=RAW"
    req=urllib.request.Request(url, data=json.dumps({"values":values}).encode(), method="PUT",
            headers={"Authorization":"Bearer "+creds.token,"Content-Type":"application/json"})
    urllib.request.urlopen(req, timeout=40, context=_ssl._create_unverified_context()).read()

if not res: res={"error":"; ".join(err)}
res["pid"]=str(PID)       # запрошенный артикул (вторичная сверка)
res["nonce"]=str(NONCE)   # разовый код запроса — ОСНОВНАЯ сверка
# ВСЕ отзывы (полным текстом) кладём в БОЛЬШОЙ канал — диапазон M10:M.., а в пульт A1 отдаём
# только метаданные + ссылку. Так облако получит сколько угодно отзывов, не упираясь в размер ячейки.
reviews = res.pop("reviews", []) if isinstance(res.get("reviews"), list) else []
res["reviews_analyzed"] = len(reviews)
if reviews:
    try:
        blob=_enc(reviews); CH=45000
        chunks=[blob[i:i+CH] for i in range(0,len(blob),CH)]
        vals=[[f"{NONCE}|{i}|{c}"] for i,c in enumerate(chunks)] + [[""]]*3   # с заголовком-кодом + чистим хвост
        _sheets_write(f"Лист1!M10:M{10+len(vals)-1}", vals)
        res["reviews_store"]={"nonce":str(NONCE),"chunks":len(chunks),"start_row":10}
    except Exception as e:
        # запасной путь: кладём сколько влезет прямо в A1 (чтобы не остаться совсем без отзывов)
        res["reviews_store_err"]=repr(e)[:150]
        res["reviews"]=reviews
        while len(res.get("reviews",[]))>20 and len(_enc(res))>47000:
            res["reviews"]=res["reviews"][:int(len(res["reviews"])*0.9)]
print("OZCARD64<<"+_enc(res)+">>")
