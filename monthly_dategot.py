# -*- coding: utf-8 -*-
# МЕСЯЧНЫЙ автомат (раз в месяц, 1-го числа; НЕ связан с письмом/остатками):
#  1) B1 вкладки «даты готовности» = текущий месяц ЗАГЛАВНЫМИ (НОЯБРЬ…)
#  2) потребность текущего месяца -> колонка «потребность» в «даты готовности» (по заголовку), 1-я строка артикула
#  (3) [TODO: неснижаемый остаток — когда дадут таблицу)
# Источник потребности: «Заказ на производство МП ас фарм», вкладка = название месяца, B=артикул, D=«кратно коробу».
# ENV: GSHEETS_SA_JSON | GSHEETS_SA_FILE ; DRY=1 — не писать.
import os,re,json,ssl,urllib.request,urllib.parse,datetime
CTX=ssl._create_unverified_context()
POTR="1SO8Oak6UHKCKVCMny9vUpxH96ZLrnw_EujIsfck4v6s"
DG="1pRT8ALdpE3JhJbstbigX2V48DyUMXX1awiBBnFOjChY"; DGTAB="даты готовности"
DRY=os.environ.get("DRY")=="1"
MONTHS={1:"январь",2:"февраль",3:"март",4:"апрель",5:"май",6:"июнь",7:"июль",8:"август",9:"сентябрь",10:"октябрь",11:"ноябрь",12:"декабрь"}
ALIAS={"orallubrikant":"orallubrikant"}  # в обеих таблицах черника = OralLubrikant (алиас не нужен)
def tok():
    from google.oauth2 import service_account
    import google.auth.transport.requests as gtr
    env=os.environ.get("GSHEETS_SA_JSON","").strip()
    if env.startswith("{"): cr=service_account.Credentials.from_service_account_info(json.loads(env),scopes=["https://www.googleapis.com/auth/spreadsheets"])
    else: cr=service_account.Credentials.from_service_account_file(os.environ.get("GSHEETS_SA_FILE"),scopes=["https://www.googleapis.com/auth/spreadsheets"])
    cr.refresh(gtr.Request()); return cr.token
TOK=tok()
def gapi(m,u,b=None):
    r=urllib.request.Request(u,data=(json.dumps(b).encode() if b is not None else None),method=m,headers={"Authorization":"Bearer "+TOK,"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(r,context=CTX,timeout=60).read())
def gv(sid,rng): return gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{urllib.parse.quote(rng)}").get("values",[])
def L(i):
    s=""; i+=1
    while i>0: i,r=divmod(i-1,26); s=chr(65+r)+s
    return s
def canon(a): return (a or "").strip().lower()

now=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
# 21-го числа берём потребность на СЛЕДУЮЩИЙ месяц
tgt_num=(now.month % 12)+1
tgt=MONTHS[tgt_num]; TGT_UP=tgt.upper()
print("целевой месяц (следующий):",tgt)

# ГЕЙТ: если B1 уже = целевой месяц — значит уже загрузили, выходим (защита от повторов)
b=gv(DG,f"{DGTAB}!B1"); b1cur=(b[0][0].strip().upper() if (b and b[0] and b[0][0]) else "")
if b1cur==TGT_UP:
    print("B1 уже",TGT_UP,"— уже загружено, выход"); raise SystemExit(0)

# вкладка потребности СЛЕДУЮЩЕГО месяца должна существовать (вносят до 20-го); если нет — ждём
meta=gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{POTR}?fields=sheets(properties(title,hidden))")
vis=[p["properties"]["title"] for p in meta["sheets"] if p["properties"]["title"] in MONTHS.values() and not p["properties"].get("hidden")]
if tgt not in vis:
    print(f"вкладка '{tgt}' ещё не создана — пропускаю (попробую в следующий прогон)"); raise SystemExit(0)
tab=tgt
print("вкладка потребности:",tab)

# --- потребность: B=артикул, D=кратно коробу, до пустой/разбивки ---
rows=gv(POTR,f"{tab}!A1:D40"); potr={}
for r in rows[1:]:
    bcol=(r[1] if len(r)>1 else "").strip()
    if not bcol or bcol.upper()=="ВБ" or bcol.lower()=="тотал": break
    d=(r[3] if len(r)>3 else "").strip(); digs=re.sub(r"[^\d]","",d)
    potr[canon(bcol)]=int(digs) if digs else 0
print("артикулов потребности:",len(potr))

# --- 2) запись потребности в «даты готовности» (колонка по заголовку), 1-я строка артикула ---
dg=gv(DG,f"{DGTAB}!A1:L80"); dh=[str(x).lower() for x in dg[0]]
def find(*keys):
    for i,h in enumerate(dh):
        if all(k in h for k in keys): return i
    return None
i_art=find("артикул"); i_potr=find("потребность"); i_nm=find("назван") or 0
if i_art is None or i_potr is None:
    print("не нашёл заголовки артикул/потребность — выход"); raise SystemExit(1)
upd=[]; seen=set(); miss=[]
for i in range(1,len(dg)):
    r=dg[i]; nm=r[i_nm] if len(r)>i_nm else ""; art=(r[i_art] if len(r)>i_art else "").strip()
    if not art or art.lower()=="тотал" or str(nm).strip().lower()=="тотал": continue
    al=canon(art)
    if al in seen: continue
    if al in potr: upd.append({"range":f"{DGTAB}!{L(i_potr)}{i+1}","values":[[potr[al]]]}); seen.add(al)
    else: miss.append(art)
print("потребность к записи:",len(upd),"| не нашли:",miss)
if not DRY:
    if upd: gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{DG}/values:batchUpdate",{"valueInputOption":"USER_ENTERED","data":upd})
    # B1 = следующий месяц ТОЛЬКО после успешной записи потребности
    gapi("PUT",f"https://sheets.googleapis.com/v4/spreadsheets/{DG}/values/{urllib.parse.quote(DGTAB+'!B1')}?valueInputOption=USER_ENTERED",{"values":[[TGT_UP]]})
    print("потребность записана:",len(upd),"| B1 ->",TGT_UP)
else: print("[DRY] не пишу, B1 был бы",TGT_UP)
print("GOTOVO")
