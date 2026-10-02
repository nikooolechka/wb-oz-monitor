# -*- coding: utf-8 -*-
# МЕСЯЧНЫЙ автомат (21–25 числа; ТОЛЬКО вкладка «даты готовности» таблицы 1pRT8):
#  21-го берём потребность на СЛЕДУЮЩИЙ месяц (менеджеры вносят вкладку-месяц до 20-го) ->
#  пишем в колонку «потребность» + ставим B1 = СЛЕДУЮЩИЙ месяц ЗАГЛАВНЫМИ.
#  Если к 22-му не записали (нет вкладки / потребность нулевая) -> 22-го алерт в канал изменений.
#  Пишем ТОЛЬКО значения (цвет/комментарии не трогаем). Столбцы по заголовкам. Гейт от повторов (B1==target).
# ENV: GSHEETS_SA_JSON|GSHEETS_SA_FILE, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DRY
import os,re,json,ssl,urllib.request,urllib.parse,datetime
CTX=ssl._create_unverified_context()
POTR="1SO8Oak6UHKCKVCMny9vUpxH96ZLrnw_EujIsfck4v6s"
DG="1pRT8ALdpE3JhJbstbigX2V48DyUMXX1awiBBnFOjChY"; DGTAB="даты готовности"
DRY=os.environ.get("DRY")=="1"
MONTHS={1:"январь",2:"февраль",3:"март",4:"апрель",5:"май",6:"июнь",7:"июль",8:"август",9:"сентябрь",10:"октябрь",11:"ноябрь",12:"декабрь"}
ALERT="<b>Неснижаемый остаток производство - 🧲таблица не записала потребности, Николь, проверь пожалуйста🧲</b>"
def tok_sheets():
    from google.oauth2 import service_account
    import google.auth.transport.requests as gtr
    env=os.environ.get("GSHEETS_SA_JSON","").strip()
    if env.startswith("{"): cr=service_account.Credentials.from_service_account_info(json.loads(env),scopes=["https://www.googleapis.com/auth/spreadsheets"])
    else: cr=service_account.Credentials.from_service_account_file(os.environ.get("GSHEETS_SA_FILE"),scopes=["https://www.googleapis.com/auth/spreadsheets"])
    cr.refresh(gtr.Request()); return cr.token
TOK=tok_sheets()
def gapi(m,u,b=None):
    r=urllib.request.Request(u,data=(json.dumps(b).encode() if b is not None else None),method=m,headers={"Authorization":"Bearer "+TOK,"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(r,context=CTX,timeout=60).read())
def gv(sid,rng): return gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{urllib.parse.quote(rng)}").get("values",[])
def L(i):
    s=""; i+=1
    while i>0: i,r=divmod(i-1,26); s=chr(65+r)+s
    return s
def canon(a): return (a or "").strip().lower()
def alert():
    try:
        t=os.environ["TELEGRAM_BOT_TOKEN"].strip(); c=os.environ["TELEGRAM_CHAT_ID"].strip()
        body=urllib.parse.urlencode({"chat_id":c,"text":ALERT,"parse_mode":"HTML","disable_web_page_preview":"true"}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{t}/sendMessage",data=body),context=CTX,timeout=25).read()
        print("АЛЕРТ отправлен в канал")
    except Exception as e: print("алерт ошибка:",str(e)[:120])

now=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
tgt=MONTHS[(now.month % 12)+1]; TGT_UP=tgt.upper()
print("целевой месяц (следующий):",tgt,"| день:",now.day)

# ГЕЙТ: B1 уже = целевой месяц -> уже загрузили, выходим без алерта
b=gv(DG,f"{DGTAB}!B1"); b1cur=(b[0][0].strip().upper() if (b and b[0] and b[0][0]) else "")
if b1cur==TGT_UP:
    print("B1 уже",TGT_UP,"— загружено, выход"); raise SystemExit(0)

# готовность потребности следующего месяца
meta=gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{POTR}?fields=sheets(properties(title,hidden))")
vis=[p["properties"]["title"] for p in meta["sheets"] if p["properties"]["title"] in MONTHS.values() and not p["properties"].get("hidden")]
potr={}
if tgt in vis:
    for r in gv(POTR,f"{tgt}!A1:D40")[1:]:
        bcol=(r[1] if len(r)>1 else "").strip()
        if not bcol or bcol.upper()=="ВБ" or bcol.lower()=="тотал": break
        digs=re.sub(r"[^\d]","",(r[3] if len(r)>3 else "")); potr[canon(bcol)]=int(digs) if digs else 0
ready = bool(potr) and any(v>0 for v in potr.values())
print("вкладка есть:",tgt in vis,"| артикулов:",len(potr),"| готово:",ready)

if ready:
    dg=gv(DG,f"{DGTAB}!A1:L80"); dh=[str(x).lower() for x in dg[0]]
    def find(*keys):
        for i,h in enumerate(dh):
            if all(k in h for k in keys): return i
        return None
    i_art=find("артикул"); i_potr=find("потребность"); i_nm=find("назван") or 0
    if i_art is None or i_potr is None:
        print("нет заголовков артикул/потребность — выход"); raise SystemExit(1)
    upd=[]; seen=set()
    for i in range(1,len(dg)):
        r=dg[i]; nm=r[i_nm] if len(r)>i_nm else ""; art=(r[i_art] if len(r)>i_art else "").strip()
        if not art or art.lower()=="тотал" or str(nm).strip().lower()=="тотал": continue
        al=canon(art)
        if al in seen: continue
        if al in potr: upd.append({"range":f"{DGTAB}!{L(i_potr)}{i+1}","values":[[potr[al]]]}); seen.add(al)
    if not DRY:
        if upd: gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{DG}/values:batchUpdate",{"valueInputOption":"USER_ENTERED","data":upd})
        gapi("PUT",f"https://sheets.googleapis.com/v4/spreadsheets/{DG}/values/{urllib.parse.quote(DGTAB+'!B1')}?valueInputOption=USER_ENTERED",{"values":[[TGT_UP]]})
    print(f"{'[DRY] ' if DRY else ''}потребность {len(upd)} строк + B1 -> {TGT_UP}")
else:
    print("потребность след. месяца НЕ готова (нет вкладки / нули) — не пишу")
    if now.day==22:    # алерт один раз, 22-го
        if DRY: print("[DRY] был бы алерт в канал")
        else: alert()
print("GOTOVO")
