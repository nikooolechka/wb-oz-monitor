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
PRICE_CMD = "Лист1!A2"; PRICE_LOG = "Лист1!A1"; PRICE_ST = "Лист1!A3"
BOT = os.environ["ANALIZ_BOT_TOKEN"]
DASH_REPO = "nikooolechka/nikooolechka.github.io"
GH_PAT = os.environ["GH_PAT"]
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
def wb_collect(nm):
    nm=int(nm); vol=nm//100000; part=nm//1000; cj=None; base=None
    hosts=[basket_host(vol)]+[f"{i:02d}" for i in range(1,25)]
    for h in dict.fromkeys(hosts):
        try:
            base=f"https://basket-{h}.wbbasket.ru/vol{vol}/part{part}/{nm}"
            cj=json.loads(get(base+"/info/ru/card.json")); break
        except Exception: continue
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
    reviews=[]; raw=[]
    for host in ("feedbacks1","feedbacks2"):
        try:
            fb=json.loads(get(f"https://{host}.wb.ru/feedbacks/v2/{imt}")); raw=fb.get("feedbacks") or []
            out["skleyka_count"]=fb.get("feedbackCount")
            for e in (fb.get("nmValuationDistribution") or []):
                if e.get("nm")==nm:
                    vd={int(k):v for k,v in (e.get("valuationDistribution") or {}).items()}
                    out["stars"]={str(k):vd.get(k,0) for k in (5,4,3,2,1)}
                    out["reviews_total"]=sum(vd.values())
                    out["rating"]=round(sum(k*v for k,v in vd.items())/max(1,sum(vd.values())),1)
            if raw: break
        except Exception: pass
    for f in raw:
        if f.get("nmId")!=nm: continue
        reviews.append({"s":f.get("productValuation"),"t":f.get("text","") or "","p":f.get("pros","") or "","m":f.get("cons","") or ""})
    out["reviews"]=reviews
    if "reviews_total" not in out:
        out["reviews_total"]=len(reviews); out["rating"]=None
        st=Counter(r["s"] for r in reviews if r["s"]); out["stars"]={str(k):st.get(k,0) for k in (5,4,3,2,1)} if st else None
    return out

# ---------- Ozon сбор (через ПК по пульту) ----------
def oz_collect(pid, timeout=420):
    swrite(PRICE_CMD, "PS:$env:PYTHONIOENCODING='utf-8';$d=\"$env:USERPROFILE\\asfarm\";$py=\"$d\\py\\python.exe\";"
           "irm 'https://storage.yandexcloud.net/asfarm-install-4d31f85f/oz_card_pc.py' -OutFile \"$d\\oz_card_pc.py\";"
           f"& $py \"$d\\oz_card_pc.py\" {pid} 2>&1 | Select-String 'OZCARD64' | Select-Object -Last 1")
    start=time.time()
    while time.time()-start<timeout:
        time.sleep(15)
        a1=sread(PRICE_LOG)
        m=re.search(r"OZCARD64<<([A-Za-z0-9+/=\s]*?)>>", a1, re.S)
        if m:
            try:
                d=json.loads(base64.b64decode(re.sub(r"\s","",m.group(1))).decode("utf-8"))
                if d.get("name"):
                    return {"platform":"OZ","pid":pid,"url":f"https://www.ozon.ru/product/{pid}/",
                            "name":d.get("name"),"brand":"","price":d.get("price_card") or d.get("price"),
                            "price_old":d.get("price_old"),"rating":d.get("rating"),"reviews_total":d.get("reviews_count"),
                            "stars":d.get("stars"),"stars_collected":d.get("stars_collected"),
                            "photo":(d.get("gallery") or [None])[0],"description":"","sostav":"",
                            "reviews":d.get("reviews") or []}
                return None
            except Exception: return None
    return None

# ---------- Анализ — Claude (Anthropic API, ключ владельца, $100/мес бесплатных кредитов) ----------
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
# Порог значимости темы — считается КОДОМ, не зависит от того, послушался ли ИИ инструкции в тексте промпта.
# Тема "значима" (можно предлагать менять рецептуру/вкус/материал под неё), если встретилась
# хотя бы в MIN_SHARE доле отзывов ИЛИ хотя бы MIN_COUNT раз. Иначе — только фиксируем жалобу, но
# в "идеальный товар" формулировки про смену рецептуры/вкуса/материала не допускаем программно.
MIN_COUNT = 10      # жёсткий минимум: меньше 10 упоминаний темы — не повод менять продукт (владелец, 2026-10-22)
RECIPE_WORDS = ["рецепт", "состав", "вкус", "аромат", "формул", "пропитк", "материал", "текстур", "плотн"]

def _is_significant(count, total=None):
    return count >= MIN_COUNT

def _looks_like_recipe_change(text):
    t = (text or "").lower()
    return any(w in t for w in RECIPE_WORDS)

def claude_analyze(data):
    revs = data.get("reviews") or []
    total = data.get("reviews_total") or len(revs) or 1
    pos = [(r.get("t") or r.get("p") or "") for r in revs if (r.get("s") in (5, 4)) and (r.get("t") or r.get("p"))][:25]
    neg = [(str(r.get("s")) + "★ " + ((r.get("t") or "") + " " + (r.get("m") or "")).strip())
           for r in revs if r.get("s") and r.get("s") <= 3 and (r.get("t") or r.get("m"))][:30]
    prompt = f"""Ты маркетинговый аналитик маркетплейсов. Разбери карточку конкурента ТОЛЬКО по фактам ниже. Без воды, без «возможно».

ВАЖНО про "count": для КАЖДОЙ темы жалобы укажи ЧЕСТНОЕ целое число — сколько раз эта тема реально встретилась в списке ЖАЛОБ ниже (посчитай сам, не выдумывай). Это число будет проверено кодом.
Для "ideal" — у каждого пункта укажи "scope": "recipe" (если это предложение изменить состав/рецептуру/вкус/материал/текстуру товара) или "fix" (если это упаковка/инфографика/позиционирование/контроль качества — без изменения самого продукта), и "based_on" — список тем жалоб (t), на которых основан этот пункт (пустой список, если это общая рекомендация).

ТОВАР: {data.get('name')}
ПЛОЩАДКА: {data.get('platform')}
РЕЙТИНГ: {data.get('rating')} | ВСЕГО ОТЗЫВОВ: {total}
СОСТАВ: {data.get('sostav')}
ОПИСАНИЕ ПРОДАВЦА: {(data.get('description') or '')[:1500]}
ПОХВАЛА ИЗ ОТЗЫВОВ: {json.dumps(pos, ensure_ascii=False)}
ЖАЛОБЫ ИЗ ОТЗЫВОВ ({len(neg)} шт, пронумерованы для подсчёта): {json.dumps(neg, ensure_ascii=False)}

Верни СТРОГО валидный JSON, без markdown-обрамления, по схеме:
{{"ca":"кто покупает и зачем, 1-2 предложения",
"seller_claims":["что продавец обещает в карточке, 4-6 пунктов"],
"praise":["за что хвалят в отзывах, 3-5 пунктов"],
"complaints":[{{"t":"тема жалобы простыми словами","n":"человеческое пояснение сути","count":0}}],
"gaps":[{{"claim":"что заявлено","reality":"что по отзывам на деле"}}],
"ideal":[{{"text":"конкретная рекомендация","scope":"recipe или fix","based_on":["тема1"]}}]}}"""
    body = json.dumps({"model": "claude-sonnet-4-5-20250929", "max_tokens": 2200,
                        "messages": [{"role": "user", "content": prompt}]}).encode()
    r = get("https://api.anthropic.com/v1/messages",
            hdr={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            data=body, t=90)
    txt = json.loads(r)["content"][0]["text"]
    txt = re.sub(r"^```json|```$", "", txt.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    raw = json.loads(m.group(0))
    return _enforce_scale(raw, total)

def _enforce_scale(raw, total):
    """Код, не ИИ, решает: можно ли предлагать менять рецептуру/вкус/состав под эту тему.
    Срезает recipe-level пункты 'идеального товара', если их тема жалобы незначима по масштабу."""
    complaints = raw.get("complaints") or []
    sig = {}
    for c in complaints:
        cnt = int(c.get("count") or 0)
        sig[c.get("t")] = _is_significant(cnt, total)
    kept = []
    for item in (raw.get("ideal") or []):
        if isinstance(item, str):
            # старый формат без scope — пропускаем как есть, но если похоже на смену рецептуры
            # без привязки к конкретной значимой теме, на всякий случай не блокируем (нет данных для проверки)
            kept.append(item); continue
        text = item.get("text") or ""
        scope = item.get("scope") or ("recipe" if _looks_like_recipe_change(text) else "fix")
        based_on = item.get("based_on") or []
        if scope == "recipe":
            # разрешаем смену рецептуры/вкуса/материала ТОЛЬКО если хотя бы одна тема-основание значима
            if not based_on or not any(sig.get(t) for t in based_on):
                continue  # единичные жалобы — не повод менять продукт, вырезаем пункт
        kept.append(text)
    if not kept:
        kept = ["Товар в целом принимают хорошо — менять состав/рецептуру нет оснований; усилить подачу сильных сторон в карточке."]
    raw["ideal"] = kept
    for c in complaints:
        c.pop("count", None)  # во фронт не нужно, только для проверки
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
def _plural(n):
    m=n%100; d=n%10
    return "жалоб" if (11<=m<=14 or d==0 or d>=5) else ("жалоба" if d==1 else "жалобы")
def rule_analyze(data):
    revs=data.get("reviews") or []
    neg=[((r.get("t") or "")+" "+(r.get("m") or "")).lower() for r in revs if r.get("s") and r.get("s")<=3]
    pos=[((r.get("t") or "")+" "+(r.get("p") or "")).lower() for r in revs if r.get("s") and r.get("s")>=4]
    desc=((data.get("description") or "")+" "+" ".join(str(v) for v in (data.get("characteristics") or {}).values())).lower()
    # жалобы с числами
    total=data.get("reviews_total") or len(revs) or 1
    complaints=[]; counts={}
    for name,kws in COMPLAINT_THEMES:
        c=sum(1 for t in neg if any(k in t for k in kws))
        if c>0:
            complaints.append({"t":name,"n":f"{c} {_plural(c)} в собранных отзывах","_c":c})
            counts[name]=c
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
        if t in RECIPE_THEMES and not _is_significant(counts.get(t,0), total):
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

# ---------- Карточка (PIL, облачные шрифты DejaVu) ----------
def _font(bold, sz):
    from PIL import ImageFont
    base="/usr/share/fonts/truetype/dejavu/"
    f=base+("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    return ImageFont.truetype(f, sz)
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
    dr.text((PAD,52),"АС-ФАРМ · АНАЛИЗ КАРТОЧКИ",font=_font(1,24),fill=(46,230,200))
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
    for ln in wrap(d["name"],_font(1,34),rw)[:4]:
        dr.text((rx,ry),ln,font=_font(1,34),fill=(234,242,247)); ry+=43
    ry+=12
    pill=d.get("platform","WB"); pw=dr.textlength(pill,font=_font(1,24))+32
    dr.rounded_rectangle([rx,ry,rx+pw,ry+42],21,fill=(46,230,200)); dr.text((rx+16,ry+8),pill,font=_font(1,24),fill=(4,18,26))
    cy=ry+21; cx=rx+pw+20
    dr.text((cx,cy),"★",font=_font(0,26),anchor="lm",fill=(255,210,63)); cx+=dr.textlength("★",font=_font(0,26))+10
    if d.get("rating"): dr.text((cx,cy),str(d["rating"]),font=_font(1,30),anchor="lm",fill=(255,210,63))
    ry+=60
    if d.get("reviews_total"):
        dr.text((rx,ry),f"{d['reviews_total']:,}".replace(","," ")+" отзывов на товар",font=_font(0,25),fill=(142,163,180)); ry+=46
    if d.get("price"):
        num=f"{int(d['price']):,}".replace(","," ")
        dr.text((rx,ry),num,font=_font(1,46),fill=(234,242,247)); nx=rx+dr.textlength(num,font=_font(1,46))+12
        dr.text((nx,ry+8),"₽",font=_font(1,36),fill=(234,242,247)); nx+=dr.textlength("₽",font=_font(1,36))
        if d.get("price_old"):
            oldn=f"{int(d['price_old']):,}".replace(","," ")+" ₽"
            ox=nx+22; dr.text((ox,ry+14),oldn,font=_font(0,26),fill=(120,135,150))
            ow=dr.textlength(oldn,font=_font(0,26)); dr.line([(ox,ry+28),(ox+ow,ry+28)],fill=(120,135,150),width=2)
    by=py+PS+44
    dr.line([(PAD,by),(W-PAD,by)],fill=(40,54,72),width=2); by+=24
    dr.text((PAD,by),"КТО ПОКУПАЕТ",font=_font(1,21),fill=(46,230,200)); by+=32
    for ln in wrap(d.get("ca",""),_font(0,26),W-2*PAD)[:2]:
        dr.text((PAD,by),ln,font=_font(0,26),fill=(206,220,230)); by+=34
    by+=12
    dr.text((PAD,by),"ЧЕМ ЦЕПЛЯЕТ ПОКУПАТЕЛЯ",font=_font(1,21),fill=(46,230,200)); by+=36
    for t in (d.get("seller_claims") or [])[:3]:
        dr.ellipse([PAD+2,by+11,PAD+11,by+20],fill=(46,230,200))
        for ln in wrap(t,_font(0,25),W-2*PAD-28)[:2]:
            dr.text((PAD+26,by),ln,font=_font(0,25),fill=(234,242,247)); by+=33
        by+=6
    by+=10; bx0,bx1=PAD,W-PAD; bh=H-40-by
    pan=Image.new("RGBA",(W,H),(0,0,0,0))
    ImageDraw.Draw(pan).rounded_rectangle([bx0,by,bx1,by+bh],24,fill=(255,255,255,12),outline=(255,255,255,26),width=1)
    img.paste(Image.alpha_composite(img.convert("RGBA"),pan).convert("RGB"),(0,0)); dr=ImageDraw.Draw(img)
    iy=by+20; ix=bx0+26; stars=d.get("stars")
    if stars and sum(stars.values())>0:
        dr.text((ix,iy),"ОЦЕНКИ ПОКУПАТЕЛЕЙ",font=_font(1,20),fill=(46,230,200))
        if d.get("stars_collected") and d["stars_collected"]<d.get("reviews_total",0):
            dr.text((bx1-26,iy),f"собрано {d['stars_collected']}/{d['reviews_total']}".replace(",",""),font=_font(0,19),anchor="ra",fill=(142,163,180))
        iy+=42
        rows=[(k,stars.get(k,0)) for k in ("5","4","3","2","1")]; mx=max(1,*[c for _,c in rows])
        colors={"5":(72,229,155),"4":(46,230,200),"3":(255,210,63),"2":(255,150,80),"1":(255,107,125)}
        tx0=ix+66; tx1=bx1-26-150; tw=tx1-tx0; rowh=(by+bh-iy-14)/5
        for k,c in rows:
            cyr=iy+rowh/2
            dr.text((ix,cyr),k,font=_font(1,24),anchor="lm",fill=colors[k]); dr.text((ix+26,cyr+1),"★",font=_font(0,22),anchor="lm",fill=(255,210,63))
            th=14; ty=cyr-th/2; dr.rounded_rectangle([tx0,ty,tx1,ty+th],7,fill=(26,36,48))
            fw=int(tw*(c/mx))
            if fw>8: dr.rounded_rectangle([tx0,ty,tx0+fw,ty+th],7,fill=colors[k])
            dr.text((bx1-26,cyr),f"{c:,}".replace(","," ")+" отз.",font=_font(1,22),anchor="rm",fill=(234,242,247))
            iy+=rowh
    else:
        dr.text((ix,iy),"ГЛАВНОЕ ИЗ ОТЗЫВОВ",font=_font(1,20),fill=(46,230,200)); iy+=42
        pr=(d.get("praise") or [""])[0]; cp=(d.get("complaints") or [{}])[0].get("t","")
        dr.text((ix,iy),"За что хвалят:",font=_font(1,20),fill=(72,229,155)); iy+=30
        for ln in wrap(pr,_font(0,24),bx1-ix-26)[:2]: dr.text((ix,iy),ln,font=_font(0,24),fill=(234,242,247)); iy+=31
        iy+=14; dr.text((ix,iy),"Главная жалоба:",font=_font(1,20),fill=(255,107,125)); iy+=30
        for ln in wrap(cp,_font(0,24),bx1-ix-26)[:3]: dr.text((ix,iy),ln,font=_font(0,24),fill=(255,220,224)); iy+=31
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
def tg_msg(chat,text):
    urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{BOT}/sendMessage",
        data=urllib.parse.urlencode({"chat_id":chat,"text":text}).encode()))

# ---------- Главный разбор очереди ----------
def today():
    import datetime
    return (datetime.datetime.utcnow()+datetime.timedelta(hours=3)).strftime("%d.%m.%Y")

def process_one():
    q=sread(QUEUE_CELL)
    lines=[x for x in q.split("\n") if x.strip()]
    if not lines: return "empty"
    swrite(BUSY_CELL,"1 "+today())
    line=lines[0]; parts=line.split("|")
    msgid=parts[0]; chat=parts[1]; url=parts[2]; who=parts[3] if len(parts)>3 else ""
    try:
        if re.search(r"ozon\.", url):
            m=re.search(r"/product/[^/]*?(\d{6,})", url) or re.search(r"(\d{6,})", url)
            data=oz_collect(m.group(1)) if m else None
        elif re.search(r"wildberries\.|\.wb\.ru", url):
            m=re.search(r"/catalog/(\d+)", url)
            data=wb_collect(m.group(1)) if m else None
        else:
            data=None
        if not data:
            tg_msg(chat,"Не смог собрать карточку по этой ссылке :( Проверь ссылку.")
        else:
            try:
                an = claude_analyze(data) if ANTHROPIC_KEY else rule_analyze(data)
            except Exception:
                an = rule_analyze(data)
            st = data.get("stars") or {}
            neg_total = sum(int(st.get(k, 0) or 0) for k in ("1", "2", "3")) if st else 0
            rt = data.get("reviews_total") or 0
            entry={"id":f"{data['platform'].lower()}-{data.get('nm') or data.get('pid')}","platform":data["platform"],
                   "analyzed":today(),"requested_by":who or "","name":data["name"],"brand":data.get("brand",""),
                   "url":data["url"],"photo":data.get("photo"),"price":(int(data["price"]) if data.get("price") else None),
                   "price_old":(int(data["price_old"]) if data.get("price_old") else None),"rating":data.get("rating"),
                   "reviews_total":rt,"stars":data.get("stars"),
                   "stars_collected":data.get("stars_collected"),
                   "stars_note":("Ozon не публикует полную разбивку — показано по собранным отзывам." if data["platform"]=="OZ" and data.get("stars_collected") else ""),
                   "neg_total": neg_total,
                   "neg_share": (f"{round(neg_total/rt*100,1)}%".replace(".0%","%") if rt else ""),
                   "skleyka": data.get("skleyka") or [],
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
