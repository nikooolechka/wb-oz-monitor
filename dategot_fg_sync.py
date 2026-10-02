# -*- coding: utf-8 -*-
# Синхрон F/G: «даты готовности» (дата поступления + комментарий) -> клод-код вкладка «остатки» столбцы F,G.
# Матч по (артикул, упаковка Nуп). Ежечасно 07:00–21:00 МСК. Идемпотентно. Только значения.
import os,re,json,ssl,urllib.request,urllib.parse
CTX=ssl._create_unverified_context()
OST="1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"; OSTTAB="остатки"
DG="1pRT8ALdpE3JhJbstbigX2V48DyUMXX1awiBBnFOjChY"; DGTAB="даты готовности"
ALIAS={"orallubrikant":"spraydlyapolostyrta"}   # даты готовности черника=OralLubrikant -> остатки=spraydlyapolostyrta
DRY=os.environ.get("DRY")=="1"
def pack(s):
    m=re.search(r"(\d+)\s*уп",s or ""); return m.group(1) if m else ""
def canon(a): a=(a or "").strip().lower(); return ALIAS.get(a,a)
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

# --- источник: даты готовности (по заголовкам) ---
dg=gv(DG,f"{DGTAB}!A1:L80"); dh=[str(x).lower() for x in dg[0]]
def find(*keys):
    for i,h in enumerate(dh):
        if all(k in h for k in keys): return i
    return None
i_art=find("артикул"); i_nm=find("назван") or 0; i_date=find("дата","поступ")
i_note=find("симаков") or find("нартов") or (i_date+1 if i_date is not None else None)
if i_art is None or i_date is None:
    print("не нашёл заголовки артикул/дата поступления — выход"); raise SystemExit(1)
src={}
for r in dg[1:]:
    art=(r[i_art] if len(r)>i_art else "").strip(); nm=r[i_nm] if len(r)>i_nm else ""
    if not art or art.lower()=="тотал": continue
    dt=(r[i_date] if len(r)>i_date else ""); note=(r[i_note] if (i_note is not None and len(r)>i_note) else "")
    src[(canon(art),pack(nm))]=(dt,note)
print("источник F/G:",len(src))

# --- расширить сетку вкладки «остатки» до >=8 столбцов (G может быть за пределами) ---
try:
    gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{OST}:batchUpdate",{"requests":[{"updateSheetProperties":{"properties":{"sheetId":1290662357,"gridProperties":{"columnCount":8}},"fields":"gridProperties.columnCount"}}]})
except Exception as _e: pass

# --- цель: клод-код «остатки» ---
ov=gv(OST,f"{OSTTAB}!A1:H80")
# заголовки F2/G2
upd=[{"range":f"{OSTTAB}!F2","values":[["дата поступления"]]},{"range":f"{OSTTAB}!G2","values":[["комментарий"]]}]
# данные с 3-й строки: Номенклатура=A(0), Артикул=B(1)
for i in range(2,len(ov)):
    r=ov[i]; nm=r[0] if r else ""; art=(r[1] if len(r)>1 else "").strip()
    if not art or art.upper()=="ИТОГО" or str(nm).strip().upper()=="ИТОГО": continue
    key=(canon(art),pack(nm))
    dt,note=src.get(key,src.get((canon(art),""),("","")))
    upd.append({"range":f"{OSTTAB}!F{i+1}","values":[[dt]]})
    upd.append({"range":f"{OSTTAB}!G{i+1}","values":[[note]]})
if not DRY:
    gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{OST}/values:batchUpdate",{"valueInputOption":"USER_ENTERED","data":upd})
print(f"{'[DRY] ' if DRY else ''}F/G обновлено строк: {(len(upd)-2)//2}")
print("GOTOVO")
