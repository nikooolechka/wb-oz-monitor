# -*- coding: utf-8 -*-
# БОЕВОЙ почасовой писатель цен «Цены АС Фарм» Лист1 из Huckster (витрина) + API (базовые цены).
# Заменяет сбор на ПК. Запуск: python boevaya_write.py        -> LIVE (пишет)
#                               python boevaya_write.py dry    -> только печать, без записи
# Витрина (ВБ D/E, Озон K/L, ЯМ Q/R) = Huckster market_price/market_card_price.
#   статус "Работает"+цена>0 -> число; OOS-статус -> "не в продаже"; иначе -> "ошибка";
#   товара нет в Huckster -> ячейку НЕ трогаем (оставляем прежнее).
# База (ВБ C до-СПП = WB API; Озон J = Seller API; ЯМ P = Partner API) — как у ПК.
# Формулы F/M/S и блок ДМ (U-Y) НЕ трогаем. Штампы свежести B1/H1/O1.
import os, re, json, ssl, sys, datetime, urllib.request, urllib.parse
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
import google.auth.transport.requests as gtr

DRY = (len(sys.argv) > 1 and sys.argv[1] == "dry")
CTX = ssl._create_unverified_context()
SA = os.environ.get("GSHEETS_SA_FILE", os.path.expanduser("~/Desktop/files/claude-sheets-497816-f533a416fa81.json"))
SAJSON = os.environ.get("GSHEETS_SA_JSON", "").strip()
SID = "1fIXDZIwbRwPXUNoqT3sGoYxfmLl4fbN7TJ5YVt4_DxM"; SHEET = "Лист1"
HBASE = "https://wbs.e-teleport.ru"
HUSER = os.environ.get("HUCKSTER_USER", "")
HPASS = os.environ.get("HUCKSTER_PASS", "")
WB_TOKEN = os.environ.get("WB_TOKEN", "")
OZH = {"Client-Id": os.environ.get("OZON_CLIENT_ID", ""), "Api-Key": os.environ.get("OZON_API_KEY", ""), "Content-Type": "application/json"}
YM_KEY = os.environ.get("YM_KEY", ""); YM_BIZ = "2489351"
WB_SKU = {"dental100":205348527,"dental100zemlyanika":583154383,"dental100banan":583155047,"dental40":140759945,
"dental40zemlyanika":860793985,"dental40natural":892991707,"dental20":76952248,"dental20zemlyanika":860789726,
"dental50":140595726,"irrigator500":227067968,"irrigator1000":363137625,"spraydlyapolostyrta":349314212,
"criogel1l":93054004,"criogel5":388153628,"cryolipolysis25":76942273,"cryolipolysis50":87180591,
"criol25new":97076035,"criol50":144662550,"optikaspraynew":206024627,"zubpastadet":917665198,"oralcherry":1055320329}
OOS = "не в продаже"; ERR = "ошибка"; STALE = "не обновляется"
OOS_STATUSES = {"нет на складе", "out_of_stock", "нет в наличии", "outofstock"}
STALE_H = 8   # last_update старше -> Huckster отвалился по товару (не в продаже тут ни при чём): не пишем старое число, ставим пометку

def norm(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower())
def msk(): return datetime.datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)

if SAJSON.startswith("{"):
    cr = service_account.Credentials.from_service_account_info(json.loads(SAJSON), scopes=["https://www.googleapis.com/auth/spreadsheets"])
else:
    cr = service_account.Credentials.from_service_account_file(SA, scopes=["https://www.googleapis.com/auth/spreadsheets"])
cr.refresh(gtr.Request()); TOK = cr.token
def gapi(m, u, b=None):
    r = urllib.request.Request(u, data=(json.dumps(b).encode() if b is not None else None), method=m,
        headers={"Authorization": "Bearer " + TOK, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, context=CTX, timeout=90).read())
def qq(s): return urllib.parse.quote(s)
GID = [s["properties"]["sheetId"] for s in gapi("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{SID}?fields=sheets(properties(sheetId,title))")["sheets"] if s["properties"]["title"] == SHEET][0]

snapshot=gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}?ranges={qq(SHEET+'!A1:AZ100')}&includeGridData=true")
headers=snapshot["sheets"][0]["data"][0]["rowData"][0]["values"]
expected={2:"цена до СПП",3:"цена с спп",4:"цена с кошельком",9:"Текущая цена",10:"Цена с другими банками",11:"Цена с Озон картой",15:"Цена продажи",16:"Цена до СПП",17:"Цена с СПП"}
for index,name in expected.items():assert headers[index].get("formattedValue","").strip().lower()==name.lower(),("Price header moved",index)

# ---- Huckster ----
def hpost(p, b):
    r = urllib.request.Request(HBASE + p, data=json.dumps(b).encode(), headers={"Content-Type":"application/json","Accept":"application/json","User-Agent":"Mozilla/5.0"}, method="POST")
    return urllib.request.urlopen(r, context=CTX, timeout=30)
hh = hpost("/md5", {"input": HPASS}).read().decode()
resp = hpost("/auth/credentials", {"userName": HUSER, "password": hh}); sc = resp.getheader("Set-Cookie")
sid = re.search(r"ss-id=([^;]+)", sc).group(1); pid = re.search(r"ss-pid=([^;]+)", sc).group(1)
HH = {"Content-Type":"application/json","Accept":"application/json","User-Agent":"Mozilla/5.0","Cookie":f"ss-id={sid}; ss-pid={pid}"}
def huck(mp, shop):
    r = urllib.request.Request(HBASE + "/markets/integrations/repricer/items/list",
        data=json.dumps({"marketplace": mp, "shop_id": shop, "limit": 200, "offset": 0}).encode(), headers=HH, method="POST")
    out = {}
    for it in json.loads(urllib.request.urlopen(r, context=CTX, timeout=30).read()).get("result", []):
        out[norm(it.get("uid", ""))] = {"mp": it.get("market_price"), "cp": it.get("market_card_price"),
                                        "st": it.get("parsing_status"), "lu": (it.get("last_update") or "")[:19]}
    return out
HO = huck("ozon", "189077_FBO"); HW = huck("wildberries", "1884480"); HY = huck("yandex", "22610087")
print(f"Huckster: ozon={len(HO)} wb={len(HW)} ym={len(HY)}")

def _stale(lu):
    try:
        dt = datetime.datetime.strptime((lu or "")[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return True
    return (msk() - dt).total_seconds() > STALE_H * 3600

def vit(rec, which):
    """Витринное значение по правилам OOS/ошибка/устарело. which='mp'|'cp'. rec=None -> None (не трогать)."""
    if rec is None:
        return None  # товара нет в Huckster -> не трогаем ячейку
    st = (rec.get("st") or "").strip(); v = rec.get(which)
    if st == "Работает" and v and v > 0:
        if _stale(rec.get("lu")):
            return STALE  # Huckster перестал обновлять этот товар -> не пишем старое число
        return int(round(v))
    if norm(st) in {norm(x) for x in OOS_STATUSES}:
        return OOS
    return ERR

# ---- базовые API ----
def wb_dospp():
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request("https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1000", headers={"Authorization": WB_TOKEN}), context=CTX, timeout=60).read())
        return {g["nmID"]: int(round(g["sizes"][0]["discountedPrice"])) for g in d["data"]["listGoods"] if g.get("sizes")}
    except Exception as e:
        print("WB API err", str(e)[:60]); return {}
def wb_vendorcode_map():
    """vendorCode (= наш текстовый артикул, норм.) -> nmID, через WB Content API (cards/list).
    Резерв на случай, когда у строки ещё нет гиперссылки в столбце B и нет в хардкод-словаре WB_SKU —
    так новый товар подхватывается АВТОМАТИЧЕСКИ, если его карточка уже создана на WB."""
    out = {}
    headers = {"Authorization": WB_TOKEN, "Content-Type": "application/json"}
    cursor = {"limit": 100}
    for _ in range(50):
        body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
        try:
            r = urllib.request.Request("https://content-api.wildberries.ru/content/v2/get/cards/list",
                                        data=json.dumps(body).encode(), headers=headers, method="POST")
            d = json.loads(urllib.request.urlopen(r, context=CTX, timeout=60).read())
        except Exception as e:
            print("WB content-api err", str(e)[:60]); break
        for c in (d.get("cards") or []):
            vc, nm = c.get("vendorCode"), c.get("nmID")
            if vc and nm: out[norm(vc)] = nm
        cur = d.get("cursor") or {}
        if cur.get("total", 0) < cursor.get("limit", 100): break
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
    return out
def oz_prices(offer_ids):
    try:
        r = urllib.request.Request("https://api-seller.ozon.ru/v5/product/info/prices", data=json.dumps({"filter":{"offer_id":offer_ids,"visibility":"ALL"},"limit":200}).encode(), headers=OZH, method="POST")
        out = {}
        for it in (json.loads(urllib.request.urlopen(r, context=CTX, timeout=60).read()).get("items") or []):
            p = it.get("price", {}).get("price")
            if p is not None: out[norm(it.get("offer_id"))] = int(round(float(p)))
        return out
    except Exception as e:
        print("Ozon API err", str(e)[:60]); return {}
def ym_basic():
    try:
        r = urllib.request.Request(f"https://api.partner.market.yandex.ru/businesses/{YM_BIZ}/offer-mappings?limit=200", data=b"{}", headers={"Api-Key": YM_KEY, "Content-Type": "application/json"}, method="POST")
        out = {}
        for om in json.loads(urllib.request.urlopen(r, context=CTX, timeout=60).read())["result"]["offerMappings"]:
            o = om.get("offer", {}); bp = (o.get("basicPrice") or {}).get("value")
            if bp is not None: out[norm(o.get("offerId", ""))] = int(round(bp))
        return out
    except Exception as e:
        print("Partner API err", str(e)[:60]); return {}

# ---- читаем боевую: артикулы+строки, ВБ nmID из гиперссылок B ----
def col(rng): return gapi("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{qq(SHEET+'!'+rng)}").get("values", [])
# ВБ: B с гиперссылками -> nmID
uu = f"https://sheets.googleapis.com/v4/spreadsheets/{SID}?ranges={qq(SHEET+'!B2:B60')}&fields=sheets(data(rowData(values(formattedValue,hyperlink,textFormatRuns(format(link(uri)))))))"
rrB = gapi("GET", uu)["sheets"][0]["data"][0].get("rowData", [])
wb_rows = []  # (art, row, nmID|None)
for i, rw in enumerate(rrB):
    vs = rw.get("values", [{}]); c = vs[0] if vs else {}
    art = c.get("formattedValue")
    if not art or not str(art).strip(): continue
    link = c.get("hyperlink")
    if not link:
        for tr in (c.get("textFormatRuns") or []):
            uri = (tr.get("format", {}).get("link") or {}).get("uri")
            if uri: link = uri; break
    nm = None
    if link:
        mm = re.search(r"/catalog/(\d+)/", link); nm = int(mm.group(1)) if mm else None
    if nm is None: nm = WB_SKU.get(norm(art))
    wb_rows.append((str(art).strip(), 2 + i, nm))
# Строки без nmID (нет ссылки в B и нет в WB_SKU) -> доискиваем по vendorCode через Content API.
# Это автоматика на будущее: новый товар не требует правки кода, достаточно чтобы на WB была создана карточка
# с vendorCode = нашему текстовому артикулу (как у всех текущих товаров).
if WB_TOKEN and os.environ.get("SKIP_WB_API") != "1" and any(nm is None for _, _, nm in wb_rows):
    VCMAP = wb_vendorcode_map()
    fixed = []
    for art, row, nm in wb_rows:
        if nm is None:
            nm2 = VCMAP.get(norm(art))
            if nm2:
                nm = nm2
            else:
                print(f"[WB до-СПП] нет nmID для «{art}»: ни ссылки в B, ни в WB_SKU, ни карточки на WB с таким vendorCode — скорее всего товар ещё не выставлен на WB")
        fixed.append((art, row, nm))
    wb_rows = fixed
Hb = col("H2:H60"); oz_rows = [(str(r[0]).strip(), 2 + i) for i, r in enumerate(Hb) if r and str(r[0]).strip()]
Ob = col("O2:O60"); ym_rows = [(str(r[0]).strip(), 2 + i) for i, r in enumerate(Ob) if r and str(r[0]).strip()]

def wb_cell(rng):
    try: return gapi("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{qq(SHEET+'!'+rng)}").get("values", [[""]])[0][0]
    except Exception: return ""
def resolve_wbapi():
    # ВБ до-СПП (C) НЕ обновляем каждый час (WB-лимит). Обновляем перед скринами, 1 раз на окно (утро/вечер).
    if os.environ.get("SKIP_WB_API") == "1":
        return {}  # почасовой прогон — WB не трогаем
    win = "am" if msk().hour < 12 else "pm"
    tag = f"wbc {msk().strftime('%Y-%m-%d')} {win}"
    if wb_cell("AZ1") == tag:
        print("WB до-СПП уже обновлён в этом окне — пропуск (не долблю WB)"); return {}
    res = wb_dospp()
    if res:  # успех -> помечаем окно (AZ1), чтобы не дёргать WB повторно на следующих тиках
        gapi("PUT", f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{qq(SHEET+'!AZ1')}?valueInputOption=RAW", {"values": [[tag]]})
    return res
wbapi = resolve_wbapi()
ozapi = oz_prices([a for a, _ in oz_rows])
ymapi = ym_basic()
print(f"API: wb_доСПП={len(wbapi)} oz_J={len(ozapi)} ym_P={len(ymapi)}")

writes = []  # {range, value}
source_errors = []
if not ozapi:source_errors.append({"market":"ОЗОН","article":"Seller API","cell":"J","reason":"Нет базовых цен API"})
if not ymapi:source_errors.append({"market":"ЯМ","article":"Partner API","cell":"P","reason":"Нет базовых цен API"})
row_article = {("WB", row):art for art,row,_ in wb_rows}
row_article.update({("ОЗОН", row):art for art,row in oz_rows}); row_article.update({("ЯМ", row):art for art,row in ym_rows})
def put(col_letter, row, val):
    if val is None: return
    market = "WB" if col_letter in ["C","D","E"] else "ОЗОН" if col_letter in ["J","K","L"] else "ЯМ"
    if val in [ERR, STALE]:
        source_errors.append({"market":market,"article":row_article.get((market,row),""),"cell":col_letter+str(row),"reason":val})
        return  # Keep last verified number; never import an error into unit formulas.
    writes.append((f"{SHEET}!{col_letter}{row}", val))

# ВБ: C=API до-СПП, D=Huckster mp, E=Huckster cp
for art, row, nm in wb_rows:
    n = norm(art)
    if nm and nm in wbapi: put("C", row, wbapi[nm])
    rec = HW.get(n)
    put("D", row, vit(rec, "mp"))
    put("E", row, vit(rec, "cp"))
# Озон: J=API, K=Huckster mp, L=Huckster cp
for art, row in oz_rows:
    n = norm(art)
    if n in ozapi: put("J", row, ozapi[n])
    rec = HO.get(n)
    if rec is not None and (vit(rec, "mp") == ERR or vit(rec, "cp") == ERR):
        # Keep the rejected source response for diagnosis; price-writing rules stay unchanged.
        print("[OZ_SOURCE_ERROR] " + json.dumps({"article": art, "source": rec}, ensure_ascii=False), flush=True)
    put("K", row, vit(rec, "mp"))
    put("L", row, vit(rec, "cp"))
# ЯМ: P=API, Q=Huckster mp, R=Huckster cp
for art, row in ym_rows:
    n = norm(art)
    if n in ymapi: put("P", row, ymapi[n])
    rec = HY.get(n)
    put("Q", row, vit(rec, "mp"))
    put("R", row, vit(rec, "cp"))

print(f"ячеек к записи: {len(writes)}")
if DRY:
    for rng, v in writes: print(f"  {rng} = {v}")
    sys.exit(0)

# запись пачкой
data = [{"range": rng, "values": [[v]]} for rng, v in writes]
gapi("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values:batchUpdate",
     {"valueInputOption": "USER_ENTERED", "data": data})
# штампы свежести B1/H1/O1 (примечания)
ts = msk().strftime("%Y-%m-%d %H:%M")
col_idx = {"B": 1, "H": 7, "O": 14}
reqs = []
for c, lbl in (("B", "ВБ"), ("H", "ОЗ"), ("O", "ЯМ")):
    market={"B":"WB","H":"ОЗОН","O":"ЯМ"}[c]
    errors=[x for x in source_errors if x["market"]==market]
    note = (f"{lbl} ЧАСТИЧНОЕ обновление {ts}; не обновлены: "+", ".join(sorted({x["article"]for x in errors}))) if errors else f"{lbl} обновлено {ts} (Huckster, облако)"
    reqs.append({"updateCells": {"range": {"sheetId": GID, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": col_idx[c], "endColumnIndex": col_idx[c] + 1},
                                 "rows": [{"values": [{"note": note}]}], "fields": "note"}})
gapi("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{SID}:batchUpdate", {"requests": reqs})
print(f"[OK] записано {len(writes)} ячеек, штампы B1/H1/O1 {ts}")

health={"checked_at":datetime.datetime.now(ZoneInfo("Europe/Moscow")).isoformat(),"errors":source_errors,"complete":not source_errors,"written_cells":len(writes)}
os.makedirs("data",exist_ok=True)
with open("data/price_source_health.json.tmp","w")as f:json.dump(health,f,ensure_ascii=False,indent=2)
os.replace("data/price_source_health.json.tmp","data/price_source_health.json")
# Read the final values back; protected formulas/format/merges are verified by the audit.
values=col("A1:AZ100")
for rng,value in writes:
    address=rng.split("!")[-1]; letters=re.match(r"[A-Z]+",address).group(); row=int(re.search(r"\d+$",address).group())-1; index=0
    for char in letters:index=index*26+ord(char)-64
    actual=values[row][index-1]if row<len(values)and index-1<len(values[row])else ""
    if isinstance(value,(float,int)):
        assert abs(float(str(actual).replace(" ","").replace("\u00a0","").replace(",","."))-float(value))<0.01,(address,"readback mismatch")
    else:assert str(actual)==str(value),(address,"readback mismatch")
print("[PRICE_SOURCE_HEALTH] "+json.dumps(health,ensure_ascii=False))
