# -*- coding: utf-8 -*-
"""
Облачный процессор анализа карточек (GitHub Actions, без Мака).
Поток: очередь M1 (бот накидал) -> сбор (WB облаком / Ozon через ПК) -> анализ Gemini
       -> карточка (PIL) -> пост в бот -> обновить дашборд -> убрать из очереди.
Запускается воркфлоу analiz.yml, который дёргает CF-воркер при новой ссылке.
"""
import os, sys, json, re, ssl, time, io, base64, urllib.request, gzip
from collections import Counter

SA = json.loads(os.environ["GSHEETS_SA_JSON"])
PULT = "1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"
QUEUE_CELL = "Лист1!M1"; BUSY_CELL = "Лист1!M2"
PRICE_CMD = "Лист1!A2"; PRICE_LOG = "Лист1!A1"; PRICE_ST = "Лист1!A3"; PRICE_HB = "Лист1!A4"
BOT = os.environ["ANALIZ_BOT_TOKEN"]
DASH_REPO = "nikooolechka/nikooolechka.github.io"
GH_PAT = os.environ["GH_PAT"]
OWNER_CHAT = "339473235"  # личка владельца @nikooolechka — сюда алерт о поломке бота

# Наши собственные товары (СТМ АС-Фарм) — WB SKU из CLAUDE.md, как подстраховка на случай,
# если МАТРИЦА (ниже) окажется недоступна. ОСНОВНОЙ источник — живая МАТРИЦА (ОП АС Фарм),
# там уже есть ссылки на ВСЕ наши товары по ВСЕМ площадкам (WB/Ozon/ЯМ/ДМ), ведётся вручную
# владельцем — не надо дублировать в код и забывать обновлять на каждый новый SKU.
STM_WB_NM = {205348527, 583154383, 583155047, 140759945, 860793985, 892991707, 76952248,
             860789726, 140595726, 227067968, 363137625, 349314212, 93054004, 388153628,
             76942273, 87180591, 97076035, 144662550, 206024627, 917665198, 1055320329}
MATRIX_SID = "1sHlFGSVB-7V8V4q6kvcTR1rrw19EaabIaOgrCHU0DHE"
_STM_CACHE = {"wb": None, "oz": None, "ym": None, "dm": None}
def _stm_ids_from_matrix():
    # МАТРИЦА, столбец C: ячейки вида "Dental20 OZON\nDental20 WB\nDental20 ЯМ..." со ссылками,
    # вшитыми В ПОДСТРОКИ (textFormatRuns), а не на всю ячейку — так же, как уже читает ym_run.py.
    # Ссылки могут быть с доп. параметрами/хвостами (ЯМ особенно — cpc/do-waremd5/nid и т.д.),
    # поэтому точные форматы (проверено живьём на реальных ссылках матрицы 23.10.2026):
    #   WB: wildberries.ru/catalog/<nm>/...
    #   Ozon: ozon.ru/product/<slug>-<pid>/...
    #   ЯМ: market.yandex.ru/card/<slug>/<sku>?... — sku идёт СРАЗУ после слага, до "?"
    #   ДМ: detmir.ru/product/index/id/<id>/
    if _STM_CACHE["wb"] is not None:
        return _STM_CACHE["wb"], _STM_CACHE["oz"], _STM_CACHE["ym"], _STM_CACHE["dm"]
    wb_ids, oz_ids, ym_ids, dm_ids = set(), set(), set(), set()
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            f"https://sheets.googleapis.com/v4/spreadsheets/{MATRIX_SID}"
            f"?ranges={urllib.parse.quote('МАТРИЦА!C:C')}&fields=sheets(data(rowData(values(textFormatRuns,hyperlink))))",
            headers={"Authorization": "Bearer " + sheets_tok()}), timeout=20)
        data = json.loads(r.read())
        for sh in data.get("sheets", []):
            for blk in sh.get("data", []):
                for row in blk.get("rowData", []):
                    for cell in row.get("values", []):
                        links = [cell.get("hyperlink")] if cell.get("hyperlink") else []
                        for run in (cell.get("textFormatRuns") or []):
                            u = ((run.get("format") or {}).get("link") or {}).get("uri")
                            if u: links.append(u)
                        for u in links:
                            if not u: continue
                            if "wildberries." in u or ".wb.ru" in u:
                                m = re.search(r"/catalog/(\d+)", u)
                                if m: wb_ids.add(int(m.group(1)))
                            elif "ozon." in u:
                                m = re.search(r"/product/[^/]*?(\d{6,})", u)
                                if m: oz_ids.add(int(m.group(1)))
                            elif "market.yandex." in u:
                                m = re.search(r"/card/[^/?]+/(\d+)", u)
                                if m: ym_ids.add(int(m.group(1)))
                            elif "detmir." in u:
                                m = re.search(r"/id/(\d+)", u)
                                if m: dm_ids.add(int(m.group(1)))
    except Exception as e:
        print("_stm_ids_from_matrix FAILED (использую только резервный список):", repr(e)[:200])
    _STM_CACHE["wb"], _STM_CACHE["oz"], _STM_CACHE["ym"], _STM_CACHE["dm"] = wb_ids, oz_ids, ym_ids, dm_ids
    return wb_ids, oz_ids, ym_ids, dm_ids
def _norm_brand(s):
    return re.sub(r"[^a-zа-я0-9]", "", (s or "").lower())
def is_stm(platform, idnum, brand="", name=""):
    if "асфарм" in _norm_brand(brand) or "asfarm" in _norm_brand(brand): return True
    if "асфарм" in _norm_brand(name) or "asfarm" in _norm_brand(name): return True
    try: idnum = int(idnum)
    except Exception: return False
    wb_ids, oz_ids, ym_ids, dm_ids = _stm_ids_from_matrix()
    if platform == "WB": return idnum in STM_WB_NM or idnum in wb_ids
    if platform == "OZ": return idnum in oz_ids
    if platform == "YM": return idnum in ym_ids
    if platform == "DM": return idnum in dm_ids
    return False
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155 Safari/537.36"

# ---------- Google Sheets (SA JWT) ----------
def _b64url(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
def _sheets_token():
    import hashlib
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    now = int(time.time())
    hdr = _b64url(json.dumps({"alg":"RS256","typ":"JWT"}).encode())
    clm = _b64url(json.dumps({"iss":SA["client_email"],"scope":"https://www.googleapis.com/auth/spreadsheets",
                              "aud":"https://oauth2.googleapis.com/token","exp":now+3600,"iat":now}).encode())
    signing = (hdr+"."+clm).encode()
    key = serialization.load_pem_private_key(SA["private_key"].encode(), None)
    sig = key.sign(signing, padding.PKCS1v15(), hashes.SHA256())
    jwt = hdr+"."+clm+"."+_b64url(sig)
    r = urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token",
        data=urllib.parse.urlencode({"grant_type":"urn:ietf:params:oauth:grant-type:jwt-bearer","assertion":jwt}).encode()))
    return json.loads(r.read())["access_token"]

import urllib.parse
_TOK=[None,0]
def sheets_tok():
    if not _TOK[0] or time.time()>_TOK[1]: _TOK[0]=_sheets_token(); _TOK[1]=time.time()+3000
    return _TOK[0]
def sread(cell):
    r=urllib.request.urlopen(urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{PULT}/values/{urllib.parse.quote(cell)}",
        headers={"Authorization":"Bearer "+sheets_tok()}))
    v=json.loads(r.read()).get("values")
    return (v[0][0] if v and v[0] else "")
def swrite(cell,val):
    urllib.request.urlopen(urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{PULT}/values/{urllib.parse.quote(cell)}?valueInputOption=RAW",
        data=json.dumps({"values":[[val]]}).encode(), method="PUT",
        headers={"Authorization":"Bearer "+sheets_tok(),"Content-Type":"application/json"}))
def sread_col(rng):
    # Читает диапазон-столбец (напр. M10:M60) → список значений ячеек. Для БОЛЬШОГО канала отзывов.
    r=urllib.request.urlopen(urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{PULT}/values/{urllib.parse.quote(rng)}",
        headers={"Authorization":"Bearer "+sheets_tok()}))
    v=json.loads(r.read()).get("values") or []
    return [(row[0] if row else "") for row in v]

# ---------- HTTP ----------
def get(url, hdr=None, data=None, t=40):
    h={"User-Agent":UA,"Accept":"*/*","Accept-Language":"ru"}
    if hdr: h.update(hdr)
    r=urllib.request.urlopen(urllib.request.Request(url,data=data,headers=h),timeout=t)
    b=r.read()
    if b[:2]==b"\x1f\x8b": b=gzip.decompress(b)
    return b

# ---------- WB сбор (облако) ----------
def basket_host(vol):
    for lim,h in [(143,"01"),(287,"02"),(431,"03"),(719,"04"),(1007,"05"),(1061,"06"),(1115,"07"),(1169,"08"),
                  (1313,"09"),(1601,"10"),(1655,"11"),(1919,"12"),(2045,"13"),(2189,"14"),(2405,"15"),(2621,"16"),
                  (2837,"17"),(3053,"18"),(3269,"19"),(3485,"20"),(3701,"21"),(3917,"22")]:
        if vol<=lim: return h
    return "23"
def _wb_card(nm):
    # WB постоянно добавляет новые баскеты — таблица basket_host() может отставать
    # (23.10.2026: nm с vol=5831 жил на basket-30, таблица/перебор до 24 его не находили
    # вообще, карточка не собиралась). Живьём проверено 23.10: баскеты существуют до 65
    # (66+ NXDOMAIN), перебор расширен до 90 — с запасом на будущий рост.
    vol=nm//100000; part=nm//1000
    hosts=[basket_host(vol)]+[f"{i:02d}" for i in range(1,91)]
    for h in dict.fromkeys(hosts):
        try:
            base=f"https://basket-{h}.wbbasket.ru/vol{vol}/part{part}/{nm}"
            return json.loads(get(base+"/info/ru/card.json")), base
        except Exception: continue
    return None, None
def wb_collect(nm):
    nm=int(nm)
    cj, base = _wb_card(nm)
    if not cj: return None
    imt=cj.get("imt_id")
    opts={}
    for o in cj.get("options",[]) or []: opts[o.get("name")]=o.get("value")
    for g in cj.get("grouped_options",[]) or []:
        for o in g.get("options",[]) or []: opts[o.get("name")]=o.get("value")
    out={"platform":"WB","nm":nm,"url":f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
         "name":cj.get("imt_name") or "","brand":(cj.get("selling") or {}).get("brand_name") or "",
         "description":cj.get("description","") or "","sostav":opts.get("Состав") or opts.get("Состав/материал") or "",
         "characteristics":opts,"photo":f"{base}/images/big/1.webp"}
    reviews=[]; raw=[]; sibling_nms=[]
    for host in ("feedbacks1","feedbacks2"):
        try:
            fb=json.loads(get(f"https://{host}.wb.ru/feedbacks/v2/{imt}")); raw=fb.get("feedbacks") or []
            out["skleyka_count"]=fb.get("feedbackCount")
            for e in (fb.get("nmValuationDistribution") or []):
                enm=e.get("nm")
                if enm and enm!=nm: sibling_nms.append(enm)
                if enm==nm:
                    vd={int(k):v for k,v in (e.get("valuationDistribution") or {}).items()}
                    out["stars"]={str(k):vd.get(k,0) for k in (5,4,3,2,1)}
                    out["reviews_total"]=sum(vd.values())
                    out["rating"]=round(sum(k*v for k,v in vd.items())/max(1,sum(vd.values())),1)
            if raw: break
        except Exception: pass
    # другие товары в этой склейке — реальные названия (не просто счётчик), ограничено 6 штук для скорости
    skleyka_names=[]
    for sn in sibling_nms[:6]:
        try:
            scj, _ = _wb_card(sn)
            if not scj: continue
            sname=scj.get("imt_name") or scj.get("subj_name")
            if sname: skleyka_names.append(sname)
        except Exception: continue
    out["skleyka"]=skleyka_names
    for f in raw:
        if f.get("nmId")!=nm: continue
        reviews.append({"s":f.get("productValuation"),"t":f.get("text","") or "","p":f.get("pros","") or "","m":f.get("cons","") or "","date":f.get("createdDate","")})
    out["reviews"]=reviews
    if "reviews_total" not in out:
        out["reviews_total"]=len(reviews); out["rating"]=None
        st=Counter(r["s"] for r in reviews if r["s"]); out["stars"]={str(k):st.get(k,0) for k in (5,4,3,2,1)} if st else None
    return out

# ---------- Ozon сбор (через ПК по пульту) ----------
def _pc_heartbeat_fresh(max_age_sec=600):
    """ПК-агент пишет heartbeat 'alive YYYY-MM-DD HH:MM:SS' в A4 каждые ~120с.
    Свежий heartbeat = сессия ПК жива и слушает команды."""
    import datetime
    hb = sread(PRICE_HB)
    m = re.search(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})", hb or "")
    if not m: return False
    try:
        t = datetime.datetime.strptime(m.group(1)+" "+m.group(2), "%Y-%m-%d %H:%M:%S")
        t = t.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=3)))  # МСК
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
        return (now - t).total_seconds() < max_age_sec
    except Exception: return False

def wb_price_pc(nm, timeout=300):
    """Живая цена WB с витрины — только через ПК-браузер (card.wb.ru блокирует отовсюду).
    Возвращает {"price":..,"price_old":..} или None при сбое — тогда карточка идёт БЕЗ цены,
    а не падает (контент/отзывы уже собраны облаком, это отдельный необязательный шаг)."""
    if not _pc_heartbeat_fresh():
        wait_start = time.time()
        while time.time() - wait_start < 1800:
            time.sleep(30)
            if _pc_heartbeat_fresh(): break
        else:
            return None
    swrite(PRICE_CMD, "PS:$env:PYTHONIOENCODING='utf-8';$d=\"$env:USERPROFILE\\asfarm\";$py=\"$d\\py\\python.exe\";"
           "irm 'https://storage.yandexcloud.net/asfarm-install-4d31f85f/wb_price_pc.py' -OutFile \"$d\\wb_price_pc.py\";"
           f"& $py \"$d\\wb_price_pc.py\" {nm} 2>&1 | Select-String 'WBPRICE' | Select-Object -Last 1")
    start=time.time()
    while time.time()-start<timeout:
        time.sleep(15)
        a1=sread(PRICE_LOG)
        m=re.search(r"WBPRICE<<([A-Za-z0-9+/=\s]*?)>>", a1, re.S)
        if m:
            try:
                d=json.loads(base64.b64decode(re.sub(r"\s","",m.group(1))).decode("utf-8"))
                if d.get("price"): return {"price": d.get("price"), "price_old": d.get("price_old")}
                return None
            except Exception: return None
    return None

def oz_collect(pid, timeout=420, url=None):
    # Если ПК сейчас не отвечает (сессия упала) — НЕ кидаем команду вхолостую. На маке уже крутится
    # сторож com.asfarm.pcwatch, который сам следит за heartbeat A4 и сам поднимает сессию ПК через
    # RDP, если та упала (реагирует после ~25 мин простоя). Мы его не трогаем и не дублируем — просто
    # терпеливо ждём, пока он сделает свою работу, и продолжаем как только heartbeat снова свежий.
    if not _pc_heartbeat_fresh():
        wait_start = time.time()
        while time.time() - wait_start < 1800:  # 30 мин — с запасом на цикл сторожа (~25 мин) + реконнект
            time.sleep(30)
            if _pc_heartbeat_fresh(): break
        else:
            return {"_pc_down": True}

    # Передаём ПОЛНЫЙ путь карточки с ЧПУ-названием (/product/<slug>-<sku>/) — он однозначно
    # указывает на нужный вариант, Ozon по нему не подменяет товар. Чистим от query и опасных
    # для командной строки символов (берём только путь, без ? и #; в slug только латиница/цифры/-).
    arg = str(pid)
    if url:
        mu = re.search(r"/product/([A-Za-z0-9\-]*\d{6,})/?", url)
        if mu: arg = "/product/" + mu.group(1) + "/"
    # Разовый код этого запроса — результат примем ТОЛЬКО с ним. Пульт A1 общий (цены/др. сессии),
    # поэтому без nonce можно подхватить чужой/старый результат → отчёт уходил не на тот товар.
    import uuid
    nonce = "n" + uuid.uuid4().hex[:12]
    swrite(PRICE_CMD, "PS:$env:PYTHONIOENCODING='utf-8';$d=\"$env:USERPROFILE\\asfarm\";$py=\"$d\\py\\python.exe\";"
           "irm 'https://storage.yandexcloud.net/asfarm-install-4d31f85f/oz_card_pc.py' -OutFile \"$d\\oz_card_pc.py\";"
           f"& $py \"$d\\oz_card_pc.py\" \"{arg}\" \"{nonce}\" 2>&1 | Select-String 'OZCARD64' | Select-Object -Last 1")
    start=time.time()
    while time.time()-start<timeout:
        time.sleep(15)
        a1=sread(PRICE_LOG)
        m=re.search(r"OZCARD64<<([A-Za-z0-9+/=\s]*?)>>", a1, re.S)
        if m:
            try:
                d=json.loads(base64.b64decode(re.sub(r"\s","",m.group(1))).decode("utf-8"))
                # Принимаем ТОЛЬКО результат с НАШИМ разовым кодом (и артикулом) — иначе это чужой/
                # старый результат на общем пульте → ждём свежий. Это и держит очередь строго по
                # порядку: каждый запрос получает ровно свой результат (23.10.2026).
                if str(d.get("nonce") or "") != nonce or str(d.get("pid") or "") != str(pid):
                    continue
                if d.get("_sku_mismatch"):
                    # Ozon упорно отдаёт ДРУГОЙ вариант группы вместо запрошенного — анализировать
                    # под именем запрошенного нельзя (иначе два разных товара дадут один отчёт).
                    return {"_wrong_variant": True, "served": d.get("_sku_mismatch")}
                if d.get("name"):
                    # БОЛЬШОЙ КАНАЛ ОТЗЫВОВ: если ПК положил все отзывы в диапазон ячеек (reviews_store)
                    # — читаем их оттуда (так проходит 1000+ отзывов полным текстом, минуя лимит A1).
                    reviews = d.get("reviews") or []
                    rs = d.get("reviews_store")
                    if rs and str(rs.get("nonce"))==nonce:
                        try:
                            start=int(rs.get("start_row") or 10); n=int(rs.get("chunks") or 0)
                            rows=sread_col(f"Лист1!M{start}:M{start+n-1}")
                            parts={}
                            for cellv in rows:
                                mm=re.match(r"^([^|]+)\|(\d+)\|(.*)$", cellv, re.S)
                                if mm and mm.group(1)==nonce: parts[int(mm.group(2))]=mm.group(3)
                            if len(parts)==n and n>0:
                                blob="".join(parts[i] for i in range(n))
                                reviews=json.loads(base64.b64decode(blob).decode("utf-8"))
                        except Exception as e:
                            print("reviews_store read FAILED (беру что в A1):", repr(e)[:150])
                    # reviews_total по товару = РЕАЛЬНО собранные по ЭТОМУ SKU (reviews_all),
                    # НЕ склеечный счётчик (15823 — на всю ГРУППУ вариантов, не на товар).
                    return {"platform":"OZ","pid":pid,"url":f"https://www.ozon.ru/product/{pid}/",
                            "name":d.get("name"),"brand":"","price":d.get("price_card") or d.get("price"),
                            "price_old":d.get("price_old"),"rating":d.get("rating"),
                            "reviews_total":d.get("reviews_all") or d.get("stars_collected") or 0,
                            "skleyka_reviews_count":d.get("skleyka_reviews_count"),
                            "skleyka":d.get("skleyka") or [],
                            "stars":d.get("stars"),"stars_collected":d.get("stars_collected"),
                            "photo":(d.get("gallery") or [None])[0],
                            "description":d.get("content_for_analysis") or "","sostav":"",
                            "reviews":reviews}
                return None
            except Exception: return None
    return None

# ---------- Анализ — Claude (Anthropic API, ключ владельца, $100/мес бесплатных кредитов) ----------
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
# Порог значимости темы — считается КОДОМ, не зависит от того, послушался ли ИИ инструкции в тексте промпта.
# Правило владельца (2026-10-22), применяется к КАЖДОЙ проверке, для любого товара, любой площадки:
#   <4 упоминаний темы за последний месяц   -> игнорируем (шум, в рекомендации не попадает)
#   4-9 упоминаний за последний месяц        -> "на заметку": разрешена МЯГКАЯ формулировка с явным числом
#                                                и пометкой "единичные случаи", БЕЗ смены рецептуры/вкуса/состава
#   10+ упоминаний за последний месяц        -> "значимо": разрешена ЛЮБАЯ рекомендация, включая смену рецептуры
MINOR_COUNT = 4     # от скольки жалоб вообще стоит упомянуть тему как "на заметку"
MIN_COUNT = 10      # от скольки — тема значима, можно предлагать менять рецептуру/вкус/состав
RECIPE_WORDS = ["рецепт", "состав", "вкус", "аромат", "формул", "пропитк", "материал", "текстур", "плотн"]

def _scale_level(count):
    """minor: 4-9 (на заметку, без смены рецептуры) | significant: 10+ (можно любые правки) | none: <4 (игнор)."""
    if count >= MIN_COUNT: return "significant"
    if count >= MINOR_COUNT: return "minor"
    return "none"

def _is_significant(count, total=None):
    return count >= MIN_COUNT

def _stems(text):
    """Грубый стемминг — первые 5 символов слова, чтобы 'горький'/'горькие'/'горечь' совпадали."""
    words = re.findall(r"[а-яёa-z]{4,}", (text or "").lower())
    return {w[:5] for w in words}

def _real_count(theme_title, neg_texts):
    """Считаем упоминания темы САМИ, не доверяя числу из ответа ИИ (может завысить/выдумать).
    Стемминг-совпадение (не точная подстрока — иначе 'горький' не матчит 'горькие')."""
    stems = _stems(theme_title)
    if not stems: return 0
    return sum(1 for t in neg_texts if _stems(t) & stems)

def _recent_reviews(revs, days=30):
    """Отзывы за последние N дней (по полю даты, разные платформы кладут её по-разному)."""
    import datetime
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    out = []
    for r in revs:
        raw = r.get("date") or r.get("d") or ""
        dt = None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(str(raw)[:19], fmt); break
            except Exception:
                continue
        if dt is None or dt >= cutoff:
            out.append(r)  # нет даты — не выбрасываем (платформа могла её не отдать), считаем потенциально свежим
    return out

def _looks_like_recipe_change(text):
    t = (text or "").lower()
    return any(w in t for w in RECIPE_WORDS)

def _existing_categories():
    # Список уже использованных категорий дашборда — чтобы ИИ переиспользовал подходящую,
    # а не плодил почти-дубли ("Детские денталы" / "Дентал-салфетки для детей" и т.п.).
    try:
        raw = get("https://raw.githubusercontent.com/" + DASH_REPO + "/main/docs/analiz_data.json", t=15)
        cur = json.loads(raw)
        cats = sorted({p.get("category") for p in cur.get("products", []) if p.get("category")})
        return cats
    except Exception:
        return []

def claude_analyze(data):
    revs = data.get("reviews") or []  # анализируем ВСЕ собранные отзывы, не только последний месяц
    revs_month = _recent_reviews(revs, 30)  # ТОЛЬКО для порога значимости темы (10+ за месяц), не для анализа
    total = data.get("reviews_total") or len(revs) or 1
    pos = [(r.get("t") or r.get("p") or "") for r in revs if (r.get("s") in (5, 4)) and (r.get("t") or r.get("p"))]
    neg = [(str(r.get("s")) + "★ " + ((r.get("t") or "") + " " + (r.get("m") or "")).strip())
           for r in revs if r.get("s") and r.get("s") <= 3 and (r.get("t") or r.get("m"))]
    neg_month = [(str(r.get("s")) + "★ " + ((r.get("t") or "") + " " + (r.get("m") or "")).strip())
                 for r in revs_month if r.get("s") and r.get("s") <= 3 and (r.get("t") or r.get("m"))]
    # В ПРОМПТ ИИ уходит крупная выборка (чтобы запрос на 1000+ отзывов не раздулся), а ЧИСЛА
    # (сколько жалоб/похвал по теме) код считает ниже по ВСЕМ отзывам (_enforce_scale) — поэтому
    # выборка не занижает цифры (владелец, 23.10.2026: «1000 отзывов — все обрабатывать»).
    PROMPT_CAP = 400
    pos_s = pos[:PROMPT_CAP]
    neg_s = neg[:PROMPT_CAP]
    prompt = f"""Ты маркетинговый аналитик маркетплейсов. Разбери карточку конкурента ТОЛЬКО по фактам ниже. Без воды, без «возможно». Будь ИСЧЕРПЫВАЮЩИМ — не сокращай список, если есть о чём писать: распиши ВСЕ заметные темы жалоб (а не только 1-2 главные), дай МИНИМУМ 5-7 пунктов в "ideal" (упаковка, инфографика, позиционирование, контроль качества, доп-SKU и т.д.) — пустой/короткий ответ хуже длинного и подробного.

🚫 ЖЕЛЕЗНОЕ ПРАВИЛО ТОЧНОСТИ (ошибка тут бьёт по прибыли компании, недопустима):
1. Анализируй ТОЛЬКО этот товар «{data.get('name')}». На маркетплейсах под одной карточкой бывают РАЗНЫЕ варианты (вкусы/фасовки/цвета) с ОБЩИМИ отзывами. Если отзыв явно про ДРУГОЙ вариант (например, в отзыве сказано «без вкуса», а анализируемый товар — с бананом; или размер/фасовка не тот) — ЭТОТ ОТЗЫВ ИГНОРИРУЙ, не вписывай его в жалобы/похвалу этого товара.
2. НИКОГДА не приписывай товару свойство/вкус/характеристику, которых у него нет. Вывод вроде «без вкуса, как указано в описании» для товара СО ВКУСОМ банана — грубая ложь, так писать ЗАПРЕЩЕНО. Сверяйся с названием и описанием товара выше.
3. ПЕРЕД тем как выдать ответ — перечитай каждый пункт complaints/praise/gaps/ideal и проверь: он точно про ЭТОТ товар и НЕ противоречит его названию/вкусу/описанию? Если противоречит или сомнительно — убери пункт. Лучше меньше пунктов, но все верные.

В "complaints" и "praise" поле "desc" — ПОДРОБНОЕ качественное описание с конкретикой (что именно пишут покупатели, какие слова используют, в каких ситуациях), 1-2 предложения, НЕ короткая фраза в 3 слова. БЕЗ чисел и слов вроде "несколько"/"много" (числа посчитает код отдельно, не придумывай их).
Для "ideal" — у каждого пункта укажи "scope": "recipe" (меняет состав/рецептуру/вкус/материал/текстуру товара) или "fix" (упаковка/инфографика/позиционирование/контроль качества/доп-SKU — без изменения продукта), и "based_on" — список тем жалоб (t), на которых основан пункт (пустой список для общей рекомендации). ВСЕГДА давай несколько "fix"-пунктов, даже если явных жалоб мало — по сильным сторонам/позиционированию/расширению тоже есть что сказать.

ТОВАР: {data.get('name')}
ПЛОЩАДКА: {data.get('platform')}
РЕЙТИНГ: {data.get('rating')} | ВСЕГО ОТЗЫВОВ: {total}
СОСТАВ: {data.get('sostav')}
ОПИСАНИЕ ПРОДАВЦА: {(data.get('description') or '')[:1500]}
ПОХВАЛА ИЗ ОТЗЫВОВ (выборка {len(pos_s)} из {len(pos)}): {json.dumps(pos_s, ensure_ascii=False)}
ЖАЛОБЫ ИЗ ОТЗЫВОВ (выборка {len(neg_s)} из всего {len(neg)} — темы бери отсюда, числа посчитаются по всем): {json.dumps(neg_s, ensure_ascii=False)}

УЖЕ ИСПОЛЬЗУЕМЫЕ НА ДАШБОРДЕ КАТЕГОРИИ: {json.dumps(_existing_categories(), ensure_ascii=False)}
Для поля "category" — если товар подходит под одну из них, используй ЕЁ ДОСЛОВНО (не создавай почти-дубль с другой формулировкой). Если не подходит ни одна — придумай новую короткую категорию в том же стиле (2-4 слова, по-русски, тип товара как его понял бы покупатель — напр. "Интимная гигиена", "Детские денталы", "Жидкость для ирригатора", "Подгузники").

Верни СТРОГО валидный JSON, без markdown-обрамления, по схеме:
{{"ca":"кто покупает и зачем, 1-2 предложения",
"category":"короткая категория товара, 2-4 слова",
"seller_claims":["что продавец обещает в карточке, 4-6 пунктов"],
"praise":[{{"t":"тема простыми словами","desc":"подробно, с конкретикой, БЕЗ чисел"}}],
"complaints":[{{"t":"тема жалобы простыми словами","desc":"подробно, с конкретикой, БЕЗ чисел"}}],
"gaps":[{{"claim":"что заявлено","reality":"что по отзывам на деле"}}],
"ideal":[{{"text":"конкретная рекомендация","scope":"recipe или fix","based_on":["тема1"]}}]}}"""
    # Ретраи: ИИ — ЕДИНСТВЕННЫЙ источник выводов (универсальный для любого товара, не только нашей ниши).
    # Если сбойнуло один раз (сеть/лимит) — пробуем ещё, вместо того чтобы тихо откатываться на
    # фальшивый нишевой fallback (владелец, 2026-10-22: "зашитые УТП" недопустимы для любого товара).
    #
    # КОРЕНЬ прежнего бага: max_tokens был мал -> ответ ИИ обрывался на середине JSON -> парсинг падал.
    # Чтобы это НЕ повторилось ни для какого товара (у разных товаров разный объём описания/отзывов,
    # фиксированное число токенов рано или поздно снова окажется мало) — проверяем ЯВНЫЙ сигнал API
    # stop_reason=="max_tokens" (100% надёжный признак обрезки, не гадание по тексту ошибки) и, если он
    # есть, САМИ удваиваем лимит на следующей попытке — лимит подстраивается под любой товар автоматически.
    last_err = None
    ATTEMPTS = 10
    cur_max_tokens = 6000
    for attempt in range(ATTEMPTS):
        try:
            body = json.dumps({"model": "claude-sonnet-4-5-20250929", "max_tokens": cur_max_tokens,
                                "messages": [{"role": "user", "content": prompt}]}).encode()
            r = get("https://api.anthropic.com/v1/messages",
                    hdr={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                    data=body, t=90)
            resp = json.loads(r)
            if resp.get("stop_reason") == "max_tokens":
                cur_max_tokens = min(cur_max_tokens * 2, 16000)
                raise RuntimeError(f"ответ обрезан по лимиту токенов, увеличиваю до {cur_max_tokens} и повторяю")
            txt = resp["content"][0]["text"]
            txt = re.sub(r"^```json|```$", "", txt.strip(), flags=re.M).strip()
            m = re.search(r"\{.*\}", txt, re.S)
            raw = json.loads(m.group(0))
            break
        except Exception as e:
            last_err = e
            print(f"claude_analyze попытка {attempt+1}/{ATTEMPTS} провалилась (max_tokens={cur_max_tokens}): {repr(e)[:200]}")
            if attempt < ATTEMPTS - 1: time.sleep(10)
    else:
        raise RuntimeError(f"claude_analyze: все {ATTEMPTS} попыток провалились: {repr(last_err)[:200]}")
    # Значимость темы (10+ за месяц) считаем ТОЛЬКО кодом. Полный текст жалоб/похвал (вся история)
    # идёт на подсчёт реального числа для КАЖДОЙ темы — она всегда попадает в вывод явным числом.
    return _enforce_scale(raw, total, neg_month, neg, pos)

def _enforce_scale(raw, total, neg_month_texts, neg_all_texts, pos_texts=None):
    """Код, не ИИ, решает всё, что касается чисел и допустимости менять продукт:
    - для каждой темы жалобы считаем РЕАЛЬНОЕ число (всего и за месяц) и ВСЕГДА пишем его явно;
    - recipe-level правки (менять вкус/состав/материал) допускаем только для тем с 10+ жалоб за месяц;
    - темы с 4-9 жалобами за месяц получают программно собранную пометку «на заметку» с точным числом;
    - темы <4 за месяц — слишком мало, из рекомендаций исключаем (но из complaints не убираем,
      если по ВСЕЙ истории накопилось заметное число — информативность важнее)."""
    complaints = raw.get("complaints") or []
    sig = {}; levels = {}; counts_all = {}; counts_month = {}
    kept_complaints = []
    for c in complaints:
        t = c.get("t") or ""
        c_all = _real_count(t, neg_all_texts)
        c_month = _real_count(t, neg_month_texts)
        counts_all[t] = c_all; counts_month[t] = c_month
        lvl = _scale_level(c_month)
        levels[t] = lvl; sig[t] = (lvl == "significant")
        if c_all < 2 and c_month < 2:
            continue  # совсем единичный случай (1 раз) — шум, не информативно даже для списка
        desc = (c.get("desc") or c.get("n") or "").strip()
        c["n"] = f"{c_all} {_plural(c_all)} всего" + (f", {c_month} за последний месяц" if c_month and c_month != c_all else "") + (f" — {desc}" if desc else "")
        c.pop("desc", None); c.pop("count", None)
        kept_complaints.append(c)
    raw["complaints"] = kept_complaints or [{"t": "Явных жалоб не выявлено", "n": ""}]

    # Похвала — та же глубина: реальное число + подробное описание (владелец, 2026-10-22: "сейчас сжато").
    praise_out = []
    for p in (raw.get("praise") or []):
        if isinstance(p, str):
            praise_out.append(p); continue  # старый формат без t/desc — не ломаем
        t = p.get("t") or ""; desc = (p.get("desc") or "").strip()
        cnt = _real_count(t, pos_texts or []) if pos_texts is not None else 0
        line = (f"{cnt} {_plural(cnt, 'отзыв')}" if cnt >= 2 else "").strip()
        text = f"{t}" + (f" — {desc}" if desc else "")
        praise_out.append(f"{text} ({line})" if line else text)
    raw["praise"] = praise_out

    sig_stems = {t: _stems(t) for t, s in sig.items() if s}  # стемы ТОЛЬКО значимых тем

    def _justified(text, based_on):
        if any(sig.get(t) for t in (based_on or [])): return True
        item_stems = _stems(text)
        return any(item_stems & st for st in sig_stems.values())

    kept = []
    for item in (raw.get("ideal") or []):
        text = item if isinstance(item, str) else (item.get("text") or "")
        declared_scope = (item.get("scope") if isinstance(item, dict) else None) or ""
        based_on = (item.get("based_on") if isinstance(item, dict) else None) or []
        # НЕ доверяем заявленному scope "fix" — ИИ может так обойти гейт. Своя эвристика по тексту ИЛИ.
        is_recipe = (declared_scope == "recipe") or _looks_like_recipe_change(text)
        if is_recipe and not _justified(text, based_on):
            continue  # единичные жалобы — не повод менять продукт, вырезаем пункт
        kept.append(text)

    # финальный независимый проход: ещё раз режем по уже готовому тексту, без доверия к scope вообще —
    # перепроверка зашита в самой автоматизации, пока не сойдётся — наружу не уходит.
    final = []
    for text in kept:
        if _looks_like_recipe_change(text) and not _justified(text, []):
            continue
        final.append(text)

    # "НА ЗАМЕТКУ" — программно, не от ИИ: для тем с 4-9 жалобами за месяц, которых ещё нет
    # среди kept-рекомендаций, добавляем мягкий пункт с ТОЧНЫМ числом (владелец, 2026-10-22):
    # "если 4-9 отзывов на тему — тоже учитывай, но пиши сколько и что это единичные возражения".
    mentioned = set()
    for text in final:
        for t in levels:
            if _stems(t) & _stems(text): mentioned.add(t)
    for t, lvl in levels.items():
        if lvl == "minor" and t not in mentioned:
            cm = counts_month.get(t, 0); ca = counts_all.get(t, 0)
            final.append(f"На заметку: «{t.lower()}» — {cm} {_plural(cm)} за последний месяц"
                         f"{f' ({ca} за всё время)' if ca != cm else ''}. Это единичные возражения, "
                         f"менять продукт рано, но стоит последить за динамикой.")

    if not final:
        final = ["Товар в целом принимают хорошо — менять состав/рецептуру нет оснований; усилить подачу сильных сторон в карточке."]
    raw["ideal"] = final
    return raw

# ---------- Анализ СВОИМ кодом (правила) — запасной вариант, если ключ недоступен ----------
# Темы жалоб/похвал для ниши гигиены полости рта (салфетки/спреи/паста/ирригатор).
COMPLAINT_THEMES = [
    ("Салфетки сухие / мало пропитаны", ["сух", "не влажн", "мало пропит", "высыха", "подсохл"]),
    ("Несоответствие возраста (0+ на карточке vs ограничение на упаковке)", ["до 12 лет","с 12 лет","возраст","0+ а на","не для детей","не использовать детям"]),
    ("Тонкие / легко рвутся / нет текстуры", ["тонк","рвут","рвётся","порвал","гладк","без текстур","разлезаются"]),
    ("Вкус не подошёл ребёнку (горечь, отказ)", ["вкус","горьк","не понрав","рвотн","противн","невкус","не стал"]),
    ("Слабо снимает налёт / не очищает", ["не очища","не убира","налёт","налет","бесполезн","не чистит","не справ"]),
    ("Запах / сомнения в составе", ["запах","химоз","состав","сомнева","аллерг","покрасн"]),
    ("Брак фасовки: пустые саше / недостача / посторонние предметы", ["пуст","недосчит","не хватает","посторонн","брак","мусор","две штук"]),
    ("Дорого за количество/качество", ["дорог","цена","переплат","не стоит","деньги на ветер"]),
    ("Отбеливание не работает", ["не отбел","цвет зубов","не изменил","не бел"]),
]
PRAISE_THEMES = [
    ("Убирает налёт / реальный эффект", ["убира","очища","налёт","белые зуб","эффект","чист"]),
    ("Удобно в дорогу / вне дома", ["дорог","поездк","в гост","на работ","в кафе","с собой","путешеств"]),
    ("Ребёнку нравится / приучает к гигиене", ["нрав","ребён","малыш","охотно","приуч","дают чист","не психу"]),
    ("Свежесть дыхания", ["свеж","мят","ментол","дыхан"]),
    ("Безопасный состав / рекомендует стоматолог", ["состав","ксилит","безопас","стоматолог","гипоаллерг","натурал"]),
]
CLAIM_KEYWORDS = [
    ("Ксилит — защита от кариеса", ["ксилит","кариес"]),
    ("Для детей 0+ / с рождения", ["0+","с рождения","новорожд","малыш","молочн"]),
    ("Помогает при прорезывании зубов", ["прорезыван","резц","дёсн","десен"]),
    ("Удаляет налёт и бактерии", ["налёт","налет","бактери","антибактер"]),
    ("Отбеливание зубов", ["отбел"]),
    ("Свежесть / приятный вкус", ["свеж","вкус","мят","аромат"]),
    ("Удобно брать с собой", ["дорог","прогулк","поездк","с собой","гермет","индивидуальн"]),
    ("Безопасность / сертификат (СГР)", ["гипоаллерг","безопас","сгр","сертификат","натурал"]),
]
def _plural(n, word="жалоб"):
    forms = {"жалоб": ("жалоб", "жалоба", "жалобы"), "отзыв": ("отзывов", "отзыв", "отзыва")}
    many, one, few = forms.get(word, forms["жалоб"])
    m=n%100; d=n%10
    return many if (11<=m<=14 or d==0 or d>=5) else (one if d==1 else few)
def rule_analyze(data):
    revs=data.get("reviews") or []  # анализ — по ВСЕМ собранным отзывам
    revs_month=_recent_reviews(revs, 30)  # последний месяц — ТОЛЬКО для порога значимости темы
    neg=[((r.get("t") or "")+" "+(r.get("m") or "")).lower() for r in revs if r.get("s") and r.get("s")<=3]
    neg_month=[((r.get("t") or "")+" "+(r.get("m") or "")).lower() for r in revs_month if r.get("s") and r.get("s")<=3]
    pos=[((r.get("t") or "")+" "+(r.get("p") or "")).lower() for r in revs if r.get("s") and r.get("s")>=4]
    desc=((data.get("description") or "")+" "+" ".join(str(v) for v in (data.get("characteristics") or {}).values())).lower()
    # жалобы с числами (по всему архиву — для картины), отдельно считаем за месяц (для порога recipe-гейта)
    total=data.get("reviews_total") or len(revs) or 1
    complaints=[]; counts={}; counts_month={}
    for name,kws in COMPLAINT_THEMES:
        c=sum(1 for t in neg if any(k in t for k in kws))
        cm=sum(1 for t in neg_month if any(k in t for k in kws))
        if c>0:
            complaints.append({"t":name,"n":f"{c} {_plural(c)} в собранных отзывах","_c":c})
            counts[name]=c; counts_month[name]=cm
    complaints.sort(key=lambda x:-x["_c"])
    for x in complaints: x.pop("_c",None)
    # похвала
    praise=[]
    for name,kws in PRAISE_THEMES:
        c=sum(1 for t in pos if any(k in t for k in kws))
        if c>0: praise.append((c,name))
    praise.sort(reverse=True); praise=[n for _,n in praise[:5]]
    # заявления продавца (по описанию)
    claims=[name for name,kws in CLAIM_KEYWORDS if any(k in desc for k in kws)]
    # слабые места: заявлено vs жалоба по теме
    gap_map=[("Отбеливание зубов","Отбеливание не работает","обещано отбеливание — часть покупателей не видит эффекта"),
             ("Удаляет налёт и бактерии","Слабо снимает налёт / не очищает","обещано удаление налёта — на деле берёт слабо у части"),
             ("Для детей 0+ / с рождения","Несоответствие возраста (0+ на карточке vs ограничение на упаковке)","на карточке 0+, а на упаковке ограничение по возрасту — прямое противоречие"),
             ("Свежесть / приятный вкус","Вкус не подошёл ребёнку (горечь, отказ)","обещан приятный вкус — часть детей его отвергает")]
    ctitles={c["t"] for c in complaints}
    gaps=[]
    for cl,comp,real in gap_map:
        if cl in claims and comp in ctitles:
            gaps.append({"claim":cl,"reality":real})
    if not gaps and complaints:
        gaps.append({"claim":"Качество/характеристики из описания","reality":"в отзывах всплывает: "+complaints[0]["t"].lower()})
    # идеальный товар — по топ-жалобам, пропорционально
    ideal_map={
        "Салфетки сухие / мало пропитаны":"Усилить и стабилизировать пропитку + герметичность саше — «сухие салфетки» это топ-претензия, прямой путь к возвратам.",
        "Несоответствие возраста (0+ на карточке vs ограничение на упаковке)":"Срочно привести карточку и упаковку к одному честному возрасту — противоречие рушит доверие и даёт 1★.",
        "Тонкие / легко рвутся / нет текстуры":"Уплотнить материал и добавить текстуру — «тонкие, рвутся» повторяется и оправдывает цену.",
        "Вкус не подошёл ребёнку (горечь, отказ)":"Смягчить вкус/аромат, убрать горечь — снимает главный барьер неприятия ребёнком.",
        "Слабо снимает налёт / не очищает":"Честно позиционировать как мягкий ежедневный уход, не обещать снятие тяжёлого налёта — меньше завышенных ожиданий.",
        "Запах / сомнения в составе":"Прозрачно показать состав и сертификаты в инфографике — закрывает сомнения, не меняя продукт.",
        "Брак фасовки: пустые саше / недостача / посторонние предметы":"Навести контроль фасовки (комплектность, отсутствие посторонних) — операционное, но бьёт по доверию.",
        "Дорого за количество/качество":"Либо подтянуть качество под цену, либо честно показать ценность (состав, эффект) — снять ощущение переплаты.",
        "Отбеливание не работает":"Не обещать видимое отбеливание, если эффекта нет — заменить на «снятие налёта + свежесть».",
    }
    # recipe-level темы (меняют сам продукт — вкус/состав/материал) допускаем в "идеальном товаре"
    # ТОЛЬКО если тема значима по масштабу (та же проверка, что у Claude-пути, _is_significant)
    RECIPE_THEMES={"Салфетки сухие / мало пропитаны","Тонкие / легко рвутся / нет текстуры","Вкус не подошёл ребёнку (горечь, отказ)"}
    ideal=[]
    for c in complaints:
        t=c["t"]
        if t not in ideal_map: continue
        if t in RECIPE_THEMES and not _is_significant(counts_month.get(t,0)):
            continue  # единичные жалобы — не повод менять продукт
        ideal.append(ideal_map[t])
    ideal=ideal[:5]
    if not ideal: ideal=["Товар в целом принимают хорошо — менять состав/рецептуру нет оснований; усилить подачу сильных сторон в инфографике."]
    # ЦА
    kid = any(k in desc for k in ["0+","дет","малыш","молочн","прорезыван"])
    ca=("Родители маленьких детей — уход за первыми зубами, прорезывание, гигиена в дороге." if kid
        else "Взрослые — свежесть и уход за полостью рта вне дома, в дороге и после еды.")
    return {"ca":ca,"seller_claims":claims or ["Гигиена полости рта"],"praise":praise or ["Удобство использования"],
            "complaints":complaints or [{"t":"Явных частых жалоб в собранных отзывах не выявлено","n":""}],
            "gaps":gaps,"ideal":ideal}

# ---------- Карточка (PIL, Montserrat — чистый премиальный геометричный шрифт с кириллицей) ----------
_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Montserrat.ttf")
_FONT_CACHE = {}
def _font(weight, sz):
    from PIL import ImageFont
    key = (weight, sz)
    if key not in _FONT_CACHE:
        f = ImageFont.truetype(_FONT_PATH, sz)
        f.set_variation_by_axes([weight])  # 400 regular, 700 bold, 800 extrabold/black
        _FONT_CACHE[key] = f
    return _FONT_CACHE[key]
def _star(dr, cx, cy, r, fill):
    # Звезда рисуется многоугольником, НЕ текстовым глифом "★" — у Montserrat (и у многих
    # других шрифтов) этого символа просто нет, PIL рисовал на его месте пустой квадрат.
    import math
    pts = []
    for i in range(10):
        ang = math.pi/2 + i*math.pi/5
        rad = r if i % 2 == 0 else r*0.42
        pts.append((cx+rad*math.cos(ang), cy-rad*math.sin(ang)))
    dr.polygon(pts, fill=fill)

def card_png(d, path):
    from PIL import Image, ImageDraw, ImageFilter
    W=H=1080
    img=Image.new("RGB",(W,H),(7,11,17)); dr=ImageDraw.Draw(img)
    for y in range(H):
        t=y/H; dr.line([(0,y),(W,y)],fill=(int(9+4*t),int(14+7*t),int(20+14*t)))
    def glow(cx,cy,rad,col,op):
        lay=Image.new("RGBA",(W,H),(0,0,0,0)); ImageDraw.Draw(lay).ellipse([cx-rad,cy-rad,cx+rad,cy+rad],fill=col+(op,))
        lay=lay.filter(ImageFilter.GaussianBlur(120)); img.paste(Image.alpha_composite(img.convert("RGBA"),lay).convert("RGB"),(0,0))
    glow(90,70,300,(25,182,255),150); glow(1010,1010,320,(139,92,246),140); glow(540,1120,320,(46,230,200),100)
    dr=ImageDraw.Draw(img); PAD=64
    dr.text((PAD,52),"АС-ФАРМ · АНАЛИЗ КАРТОЧКИ",font=_font(700,24),fill=(46,230,200))
    PS=340; px=PAD; py=104
    try:
        raw=get(d["photo"], t=20); from PIL import Image as I2; ph=I2.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        ph=Image.new("RGB",(PS,PS),(20,30,40))
    ph=ph.resize((PS,PS)); mask=Image.new("L",(PS,PS),0); ImageDraw.Draw(mask).rounded_rectangle([0,0,PS,PS],32,fill=255)
    gl=Image.new("RGBA",(W,H),(0,0,0,0)); ImageDraw.Draw(gl).rounded_rectangle([px-6,py-6,px+PS+6,py+PS+6],38,fill=(46,230,200,120))
    gl=gl.filter(ImageFilter.GaussianBlur(24)); img.paste(Image.alpha_composite(img.convert("RGBA"),gl).convert("RGB"),(0,0))
    dr=ImageDraw.Draw(img); img.paste(ph,(px,py),mask)
    rx=px+PS+40; rw=W-PAD-rx; ry=py+4
    def wrap(txt,fnt,maxw):
        out=[]; cur=""
        for w in (txt or "").split():
            tt=(cur+" "+w).strip()
            if dr.textlength(tt,font=fnt)<=maxw: cur=tt
            else: out.append(cur); cur=w
        if cur: out.append(cur)
        return out
    for ln in wrap(d["name"],_font(700,34),rw)[:4]:
        dr.text((rx,ry),ln,font=_font(700,34),fill=(234,242,247)); ry+=43
    ry+=12
    pill=d.get("platform","WB"); pw=dr.textlength(pill,font=_font(700,24))+32
    dr.rounded_rectangle([rx,ry,rx+pw,ry+42],21,fill=(46,230,200)); dr.text((rx+16,ry+8),pill,font=_font(700,24),fill=(4,18,26))
    cy=ry+21; cx=rx+pw+20
    _star(dr,cx+13,cy,13,(255,210,63)); cx+=36
    if d.get("rating"): dr.text((cx,cy),str(d["rating"]),font=_font(700,30),anchor="lm",fill=(255,210,63))
    ry+=60
    if d.get("reviews_total"):
        # На Ozon число по одному SKU недоступно → это СОБРАННЫЕ по товару (не склеечные 15k);
        # на WB reviews_total — точное per-nm из nmValuationDistribution.
        rlabel = (" отзывов собрано" if d.get("platform")=="OZ" else " отзывов на товар")
        dr.text((rx,ry),f"{d['reviews_total']:,}".replace(","," ")+rlabel,font=_font(400,25),fill=(142,163,180)); ry+=46
    if d.get("price"):
        num=f"{int(d['price']):,}".replace(","," ")
        dr.text((rx,ry),num,font=_font(800,46),fill=(234,242,247)); nx=rx+dr.textlength(num,font=_font(800,46))+12
        dr.text((nx,ry+8),"₽",font=_font(700,36),fill=(234,242,247)); nx+=dr.textlength("₽",font=_font(700,36))
        if d.get("price_old"):
            oldn=f"{int(d['price_old']):,}".replace(","," ")+" ₽"
            ox=nx+22; dr.text((ox,ry+14),oldn,font=_font(400,26),fill=(120,135,150))
            ow=dr.textlength(oldn,font=_font(400,26)); dr.line([(ox,ry+28),(ox+ow,ry+28)],fill=(120,135,150),width=2)
    # ── НИЖНЯЯ ПЛАШКА — ФИКСИРОВАННАЯ 1/4 КАРТИНКИ, НЕ ЕРЗАЕТ (владелец, 23.10.2026) ──
    bx0,bx1=PAD,W-PAD
    PANEL_H=H//4                      # ровно четверть картинки
    PANEL_BM=30                       # отступ снизу
    panel_top=H-PANEL_H-PANEL_BM      # всегда одно и то же место
    panel_bot=panel_top+PANEL_H
    # Контент сверху (кто покупает / чем цепляет) рисуем ТОЛЬКО до плашки; если не влезает —
    # сокращаем текст (меньше строк), а плашку НЕ двигаем.
    content_limit=panel_top-14
    by=py+PS+44
    dr.line([(PAD,by),(W-PAD,by)],fill=(40,54,72),width=2); by+=24
    dr.text((PAD,by),"КТО ПОКУПАЕТ",font=_font(700,21),fill=(46,230,200)); by+=32
    for ln in wrap(d.get("ca",""),_font(400,26),W-2*PAD)[:2]:
        if by+30>content_limit: break
        dr.text((PAD,by),ln,font=_font(400,26),fill=(206,220,230)); by+=34
    by+=12
    if by+30<=content_limit:
        dr.text((PAD,by),"ЧЕМ ЦЕПЛЯЕТ ПОКУПАТЕЛЯ",font=_font(700,21),fill=(46,230,200)); by+=36
        claims=[t for t in (d.get("seller_claims") or [])[:3] if t]
        n=len(claims) or 1
        avail=content_limit-by
        # сколько места на пункт. Если на 2 строки каждому не хватает — даём по 1 строке
        # (текст сокращаем с «…»), чтобы ПОКАЗАТЬ ВСЕ пункты, а не выкидывать и не двигать плашку.
        per=avail/n
        two_lines = per>=(2*33+6)
        fclaim=_font(400,25)
        def _one_line(t):
            lns=wrap(t,fclaim,W-2*PAD-28)
            if len(lns)<=1: return lns[0] if lns else ""
            s=lns[0]
            while s and dr.textlength(s+"…",font=fclaim)>W-2*PAD-28: s=s[:-1]
            return s+"…"
        for t in claims:
            if by+30>content_limit: break
            dr.ellipse([PAD+2,by+11,PAD+11,by+20],fill=(46,230,200))
            lns = wrap(t,fclaim,W-2*PAD-28)[:2] if two_lines else [_one_line(t)]
            if len(lns)==2 and by+2*33>content_limit: lns=[_one_line(t)]
            for ln in lns:
                if by+30>content_limit: break
                dr.text((PAD+26,by),ln,font=fclaim,fill=(234,242,247)); by+=33
            by+=6
    # панель на фиксированном месте
    by=panel_top; bh=PANEL_H; stars=d.get("stars")
    pan=Image.new("RGBA",(W,H),(0,0,0,0))
    ImageDraw.Draw(pan).rounded_rectangle([bx0,by,bx1,by+bh],24,fill=(255,255,255,12),outline=(255,255,255,26),width=1)
    img.paste(Image.alpha_composite(img.convert("RGBA"),pan).convert("RGB"),(0,0)); dr=ImageDraw.Draw(img)
    iy=by+18; ix=bx0+26
    if stars and sum(stars.values())>0:
        dr.text((ix,iy),"ОЦЕНКИ ПОКУПАТЕЛЕЙ",font=_font(700,20),fill=(46,230,200))
        if d.get("stars_collected") and d["stars_collected"]<d.get("reviews_total",0):
            dr.text((bx1-26,iy),f"собрано {d['stars_collected']}/{d['reviews_total']}".replace(",",""),font=_font(400,19),anchor="ra",fill=(142,163,180))
        iy+=38
        rows=[(k,stars.get(k,0)) for k in ("5","4","3","2","1")]; mx=max(1,*[c for _,c in rows])
        colors={"5":(72,229,155),"4":(46,230,200),"3":(255,210,63),"2":(255,150,80),"1":(255,107,125)}
        # 5 строк РОВНО в фиксированную панель — высота строки считается от её размера.
        tx0=ix+66; tx1=bx1-26-150; tw=tx1-tx0; rowh=(by+bh-iy-12)/5
        for k,c in rows:
            cyr=iy+rowh/2
            dr.text((ix,cyr),k,font=_font(700,24),anchor="lm",fill=colors[k]); _star(dr,ix+26+11,cyr+1,11,(255,210,63))
            th=14; ty=cyr-th/2; dr.rounded_rectangle([tx0,ty,tx1,ty+th],7,fill=(26,36,48))
            fw=int(tw*(c/mx))
            if fw>8: dr.rounded_rectangle([tx0,ty,tx0+fw,ty+th],7,fill=colors[k])
            dr.text((bx1-26,cyr),f"{c:,}".replace(","," ")+" отз.",font=_font(700,22),anchor="rm",fill=(234,242,247))
            iy+=rowh
    else:
        dr.text((ix,iy),"ГЛАВНОЕ ИЗ ОТЗЫВОВ",font=_font(700,20),fill=(46,230,200)); iy+=38
        pr=(d.get("praise") or [""])[0]; cp=(d.get("complaints") or [{}])[0].get("t","")
        dr.text((ix,iy),"За что хвалят:",font=_font(700,20),fill=(72,229,155)); iy+=30
        for ln in wrap(pr,_font(400,24),bx1-ix-26)[:2]:
            if iy+28>panel_bot-10: break
            dr.text((ix,iy),ln,font=_font(400,24),fill=(234,242,247)); iy+=31
        iy+=12
        if iy+28<=panel_bot-10:
            dr.text((ix,iy),"Главная жалоба:",font=_font(700,20),fill=(255,107,125)); iy+=30
            for ln in wrap(cp,_font(400,24),bx1-ix-26)[:3]:
                if iy+28>panel_bot-10: break
                dr.text((ix,iy),ln,font=_font(400,24),fill=(255,220,224)); iy+=31
    img.save(path,"PNG")

# ---------- Обновление дашборда (docs/analiz_data.json в pages-репо) ----------
def update_dashboard(entry):
    api=f"https://api.github.com/repos/{DASH_REPO}/contents/docs/analiz_data.json"
    h={"Authorization":"Bearer "+GH_PAT,"Accept":"application/vnd.github+json","User-Agent":"analiz"}
    r=json.loads(get(api,hdr=h))
    cur=json.loads(base64.b64decode(r["content"]).decode())
    cur["products"]=[entry]+[p for p in cur.get("products",[]) if p.get("id")!=entry["id"]]
    cur["updated"]=entry["analyzed"]
    body={"message":f"анализ: {entry['name'][:40]}","content":base64.b64encode(json.dumps(cur,ensure_ascii=False,indent=1).encode()).decode(),"sha":r["sha"]}
    urllib.request.urlopen(urllib.request.Request(api,data=json.dumps(body).encode(),method="PUT",headers={**h,"Content-Type":"application/json"}))

# ---------- Пост в бот ----------
def post_card(chat, entry, img_path):
    # Сбой отправки в Telegram НЕ должен ронять весь прогон (иначе очередь не очистится,
    # следующие товары встанут). Логируем и идём дальше — дашборд уже обновлён к этому моменту.
    try:
        import uuid
        price = (f"цена {int(entry['price']):,}".replace(","," ")+" ₽, ") if entry.get("price") else ""
        rat = (f"рейтинг {entry['rating']}" if entry.get("rating") else "")
        cap=(f"<b>Проанализировал ✅</b>\n"
             f'<a href="{entry["url"]}">{entry["name"]}</a> ({entry["platform"]}) — {price}{rat}\n'
             f"{(entry.get('reviews_total') or 0):,}".replace(","," ")+" отзывов")
        kb=json.dumps({"inline_keyboard":[[{"text":"Полный разбор","url":"https://nikooolechka.github.io/analiz.html"}]]})
        b="----b"+uuid.uuid4().hex; data=open(img_path,"rb").read()
        def fld(n,v): return (f'--{b}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n').encode()
        body=fld("chat_id",chat)+fld("caption",cap)+fld("parse_mode","HTML")+fld("reply_markup",kb)
        body+=(f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="c.png"\r\nContent-Type: image/png\r\n\r\n').encode()+data+b"\r\n"+(f'--{b}--\r\n').encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{BOT}/sendPhoto",data=body,
            headers={"Content-Type":f"multipart/form-data; boundary={b}"}),timeout=60)
    except Exception as e:
        print("post_card FAILED (не роняем прогон):", repr(e)[:300])

def tg_msg(chat,text):
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{BOT}/sendMessage",
            data=urllib.parse.urlencode({"chat_id":chat,"text":text}).encode()), timeout=30)
    except Exception as e:
        print("tg_msg FAILED (не роняем прогон):", repr(e)[:300])

# ---------- Главный разбор очереди ----------
def today():
    import datetime
    return (datetime.datetime.utcnow()+datetime.timedelta(hours=3)).strftime("%d.%m.%Y")

def process_one():
    # Общий FIFO: кто первый прислал — тот первый в очереди (берём САМУЮ СТАРУЮ ссылку, lines[0]).
    # Приёмник дописывает ссылки в конец по порядку прихода (от любого человека), обрабатываем
    # строго по этому порядку. Результат уходит ТОЛЬКО тому, кто запросил — chat зашит в строке
    # очереди, карточка/сообщение шлётся именно в этот chat (владелец, 23.10.2026).
    q=sread(QUEUE_CELL)
    lines=[x for x in q.split("\n") if x.strip()]
    if not lines: return "empty"
    swrite(BUSY_CELL,"1 "+today())
    line=lines[0]; parts=line.split("|")
    msgid=parts[0]; chat=parts[1]; url=parts[2]; who=parts[3] if len(parts)>3 else ""
    an=None
    try:
        if re.search(r"ozon\.", url):
            m=re.search(r"/product/[^/]*?(\d{6,})", url) or re.search(r"(\d{6,})", url)
            data=oz_collect(m.group(1), url=url) if m else None
        elif re.search(r"wildberries\.|\.wb\.ru", url):
            m=re.search(r"/catalog/(\d+)", url)
            data=wb_collect(m.group(1)) if m else None
            if data and not data.get("_pc_down"):
                # Живая цена WB — отдельный шаг через ПК-браузер (контент/отзывы уже собраны
                # облаком и не зависят от ПК). Сбой здесь НЕ валит карточку — просто без цены.
                pr = wb_price_pc(m.group(1))
                if pr: data["price"], data["price_old"] = pr.get("price"), pr.get("price_old")
        else:
            data=None
        if data and data.get("_pc_down"):
            tg_msg(chat,"Удалённый ПК сейчас недоступен (нужен для сбора с Ozon) — попробуй прислать ссылку чуть позже.")
            data=None
        elif data and data.get("_wrong_variant"):
            tg_msg(chat,"👻УПС - Ozon сейчас открывает по этой ссылке другой вариант товара, а не запрошенный. Чтобы не выдать анализ не того товара, не публикую. Пришли ссылку ещё раз чуть позже.")
            tg_msg(OWNER_CHAT,"бот аналитик: Ozon подменил вариант товара, не опубликовал")
            data=None
        elif not data:
            tg_msg(chat,"Не смог собрать карточку по этой ссылке :( Проверь ссылку.")
        elif not (data.get("stars") and sum(int(v or 0) for v in data["stars"].values())>0) or not data.get("rating"):
            # Карточка ПУБЛИКУЕТСЯ только с рейтингом И разбивкой по звёздам (владелец, 23.10.2026:
            # без рейтинга/отзывов — недопустимо). Нет звёзд/рейтинга = сбой сборщика (антибот/
            # занятый браузер), а не "отзывов нет" → честный отказ, не рисуем пустую карточку.
            tg_msg(chat,"👻УПС - что то сломалось\nСобрал карточку, но не смог собрать отзывы/рейтинг. Пришли ссылку ещё раз чуть позже.")
            tg_msg(OWNER_CHAT,"бот аналитик сломался, нужна починка")
            data=None
        elif not ANTHROPIC_KEY:
            tg_msg(chat,"ИИ-анализ недоступен (нет ключа) — товар не публикую, чтобы не выдумывать выводы.")
            an = None
        else:
            try:
                an = claude_analyze(data)
            except Exception as e:
                import traceback
                print("claude_analyze FAILED даже после ретраев:", repr(e)[:400])
                traceback.print_exc()
                # НИКОГДА не публикуем фальшивый нишевой fallback (владелец, 2026-10-22: "зашитые УТП"
                # недопустимы для ЛЮБОГО товара) — честно сообщаем и просим повторить.
                tg_msg(chat,"Не смог сделать разбор — ИИ не ответил после 10 попыток. Пришли ссылку ещё раз чуть позже.")
                an = None
        if data and an is not None:
            st = data.get("stars") or {}
            neg_total = sum(int(st.get(k, 0) or 0) for k in ("1", "2", "3")) if st else 0
            rt = data.get("reviews_total") or 0
            # ВСЕГДА фиксируем: сколько отзывов реально прочитано (объём, что дал ИИ) и за какой период —
            # честная плашка, отдельно от reviews_total (который может быть точным полем площадки, не выборкой).
            revs_all = data.get("reviews") or []
            processed_count = len(revs_all)
            pdates = sorted(r.get("date") for r in revs_all if r.get("date"))
            processed_period = None
            if pdates:
                def _fmt(d):
                    try: return f"{d[8:10]}.{d[5:7]}.{d[0:4]}"
                    except Exception: return d[:10]
                processed_period = f"{_fmt(pdates[0])} – {_fmt(pdates[-1])}"
            entry={"id":f"{data['platform'].lower()}-{data.get('nm') or data.get('pid')}","platform":data["platform"],
                   "analyzed":today(),"requested_by":who or "","name":data["name"],"brand":data.get("brand",""),
                   "url":data["url"],"photo":data.get("photo"),"price":(int(data["price"]) if data.get("price") else None),
                   "price_old":(int(data["price_old"]) if data.get("price_old") else None),"rating":data.get("rating"),
                   "reviews_total":rt,"stars":data.get("stars"),
                   "stars_collected":data.get("stars_collected"),
                   "skleyka_reviews_count":data.get("skleyka_reviews_count"),
                   "stars_note":(
                       (f"Ozon не даёт число отзывов по одному товару — показано {data.get('stars_collected') or rt} реально собранных по этому SKU"
                        + (f" (в общей группе вариантов всего ~{int(data['skleyka_reviews_count']):,} отзывов)".replace(","," ") if data.get("skleyka_reviews_count") else "")
                        + ".") if data["platform"]=="OZ" and data.get("stars_collected") else ""),
                   "neg_total": neg_total,
                   "neg_share": (f"{round(neg_total/rt*100,1)}%".replace(".0%","%") if rt else ""),
                   "skleyka": data.get("skleyka") or [],
                   "processed_count": processed_count,
                   "processed_period": processed_period,
                   "is_stm": is_stm(data["platform"], data.get("nm") or data.get("pid"), data.get("brand",""), data.get("name","")),
                   **an}
            card_png(entry, "/tmp/card.png")
            update_dashboard(entry)
            post_card(chat, entry, "/tmp/card.png")
    finally:
        q2=sread(QUEUE_CELL); rest=[x for x in q2.split("\n") if x.strip() and not x.startswith(msgid+"|")]
        swrite(QUEUE_CELL,"\n".join(rest)); swrite(BUSY_CELL,"")
    return "done" if lines else "empty"

if __name__ == "__main__":
    n=0
    while n<5:
        r=process_one()
        if r=="empty": break
        n+=1
    print("обработано:",n)
