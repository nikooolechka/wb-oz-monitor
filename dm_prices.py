# -*- coding: utf-8 -*-
# ДетМир: сам тянет detmir API (публичный) и пишет блок «Дет Мир» Лист1 таблицы цен.
# V «до СПП» = prices.old ; W «с СПП» = prices.old (как было) ; X «с промокодом» = цена с полки МИНУС промокод из labels.
# Y (СПП) — формула, НЕ трогаем. Нет склада 1120 в Москве -> "нет в наличии" в V/W/X. Штамп U1.
# Работает из облака (GitHub Actions), ПК не нужен. Секрет: GSHEETS_SA_JSON.
import os, re, json, ssl, urllib.request, urllib.parse, datetime
from google.oauth2 import service_account
import google.auth.transport.requests as gtr
CTX=ssl._create_unverified_context()
SID=os.environ.get("PRICES_SHEET_ID","1fIXDZIwbRwPXUNoqT3sGoYxfmLl4fbN7TJ5YVt4_DxM")
OP="1sHlFGSVB-7V8V4q6kvcTR1rrw19EaabIaOgrCHU0DHE"; SHEET="Лист1"
def norm(s): return re.sub(r"[^a-z0-9]","",(s or "").lower())
sa=os.environ.get("GSHEETS_SA_JSON")
if sa: 
    creds=service_account.Credentials.from_service_account_info(json.loads(sa),scopes=["https://www.googleapis.com/auth/spreadsheets"])
else:
    creds=service_account.Credentials.from_service_account_file(os.path.expanduser("~/Desktop/files/claude-sheets-497816-f533a416fa81.json"),scopes=["https://www.googleapis.com/auth/spreadsheets"])
creds.refresh(gtr.Request()); TOK=creds.token
def gapi(m,u,b=None):
    r=urllib.request.Request(u,data=(json.dumps(b).encode() if b is not None else None),method=m,headers={"Authorization":"Bearer "+TOK,"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(r,context=CTX,timeout=120).read())
def q(s): return urllib.parse.quote(s)
def det(pid):
    r=urllib.request.Request(f"https://api.detmir.ru/v2/products/{pid}",headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"})
    return json.loads(urllib.request.urlopen(r,context=CTX,timeout=40).read()).get("item",{})

GID=[s["properties"]["sheetId"] for s in gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}?fields=sheets(properties(sheetId,title))")["sheets"] if s["properties"]["title"]==SHEET][0]
# артикул->detmir id из ссылок МАТРИЦЫ (столбец C, richtext)
u=f"https://sheets.googleapis.com/v4/spreadsheets/{OP}?ranges=%D0%9C%D0%90%D0%A2%D0%A0%D0%98%D0%A6%D0%90!C1:C60&fields=sheets(data(rowData(values(formattedValue,textFormatRuns(format(link(uri)))))))"
id_by_art={}
for row in gapi("GET",u)["sheets"][0]["data"][0]["rowData"]:
    vs=row.get("values",[{}]); c=vs[0] if vs else {}; fv=c.get("formattedValue","")
    for tr in (c.get("textFormatRuns") or []):
        uri=(tr.get("format",{}).get("link") or {}).get("uri","")
        if "detmir.ru" in uri:
            m=re.search(r"/id/(\d+)",uri) or re.search(r"(\d{5,})",uri)
            if m: id_by_art[norm(re.split(r"\s",fv)[0])]=m.group(1)
# U-колонка Лист1 -> строки
rows=gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{q(SHEET+'!U2:U40')}").get("values",[])
writes=[]; n_ok=0; n_oos=0
for i,r in enumerate(rows):
    art=(r[0] if r else "").strip()
    if not art: continue
    row=2+i; pid=id_by_art.get(norm(art))
    if not pid: continue   # нет ссылки ДМ — не трогаем (оставляем как есть)
    try:
        it=det(pid)
        wh=((it.get("available") or {}).get("online") or {}).get("warehouse_codes") or []
        if "1120" not in wh:
            for col in ("V","W","X"): writes.append({"range":f"{SHEET}!{col}{row}","values":[["нет в наличии"]]})
            n_oos+=1; continue
        old=(it.get("prices") or {}).get("old"); sale=(it.get("price") or {}).get("price")
        pct=0
        for l in (it.get("labels") or []):
            if l.get("name")=="promocode":
                m=re.search(r"-?(\d+)\s*%",l.get("title","")); pct=int(m.group(1)) if m else 0
        promo = round(sale*(1-pct/100)) if pct else sale
        writes.append({"range":f"{SHEET}!V{row}","values":[[old]]})
        writes.append({"range":f"{SHEET}!W{row}","values":[[old]]})
        writes.append({"range":f"{SHEET}!X{row}","values":[[promo]]})
        n_ok+=1
        print(f"  {art:24} доСПП={old} сСПП={old} промокод={promo} (-{pct}%)")
    except Exception as e:
        print(f"  {art:24} ошибка {str(e)[:50]}")
gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values:batchUpdate",{"valueInputOption":"USER_ENTERED","data":writes})
now=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).strftime("%Y-%m-%d %H:%M")
gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}:batchUpdate",{"requests":[{"updateCells":{"rows":[{"values":[{"note":f"ДМ обновлено {now} (detmir прямая, промокод из labels; {n_ok} в наличии/{n_oos} нет)"}]}],"fields":"note","start":{"sheetId":GID,"rowIndex":0,"columnIndex":20}}}]})
print(f"ЗАПИСАНО: в наличии {n_ok}, нет в наличии {n_oos}, ячеек {len(writes)}. Штамп U1 обновлён.")
