# -*- coding: utf-8 -*-
# Остатки 1С: письмо -> вкладка «остатки» (обновляю ТОЛЬКО «Свободно»).
# Матч: по «Номенклатура 1С», запас — по «Артикул» (тогда подставляю реальную номенклатуру из письма).
# Артикул есть, в письме нет -> 0. Новый артикул -> добавляю строкой. Скрин = ВСЕ строки и столбцы.
# ENV: MAILRU_APP_PASS, GSHEETS_SA_JSON|GSHEETS_SA_FILE, TELEGRAM_BOT_TOKEN, OSTATKI_CHAT_ID, DRY
import os,re,html,ssl,json,io,urllib.request,urllib.parse,imaplib,email,zipfile,datetime
from email.header import decode_header
CTX=ssl._create_unverified_context()
SID="1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"; TAB="остатки"; GID=1290662357
MAIL_USER="nikol-oleinik@mail.ru"; SENDER="as-farm-as-farm@yandex.ru"
DRY=os.environ.get("DRY")=="1"
def dec(s):
    if not s: return ""
    return "".join(t.decode(e or "utf-8","replace") if isinstance(t,bytes) else t for t,e in decode_header(s))
def norm(s):
    s=(s or "").lower().replace("шк"," "); s=re.sub(r"\d{6,}"," ",s)
    s=re.sub(r"[^0-9a-zа-яё]+"," ",s); return re.sub(r"\s+"," ",s).strip()
def n1c(s):
    s=(s or "").strip().split(",")[0]; s=re.sub(r"[^\d]","",s); return int(s) if s else 0
def article(name):
    n=name.lower()
    if "экстракт" in n and "купан" in n:
        if "ромашк" in n: return "extract_romashka"
        if "пихт" in n: return "extract_pihta"
    if "ирригатор" in n:
        if "new 2026 1" in n: return "irrigator_new_1"
        if "new 2026 500" in n: return "irrigator_new_05"
        if "1л" in n: return "Irrigator_1000"
        if "500" in n: return "Irrigator_500"
    if "паста" in n: return "Zub_pasta_det"
    if "криогель" in n: return "CrioGel1l" if "1 кг" in n else ("CrioGel_5" if "5 кг" in n else "")
    if "очиститель" in n: return "OptikaSpray_new"
    if "криолиполиза" in n:
        big=("50 шт" in n) or ("(50" in n)
        if ("l (340" in n) or ("размер l" in n): return "CrioL50" if big else "Crio_L25(new)"
        if "м (290" in n: return "Cryolipolysis50" if big else "cryolipolysis25"
    if ("снятия макияжа" in n) or ("make up" in n):
        return "makeup_30" if ("30 шт" in n or "30шт" in n) else "makeup_50"
    if ("орального" in n) or ("минет" in n): return "Oral_cherry" if "вишн" in n else "spraydlyapolostyrta"
    if ("за зубками" in n) or ("дентальн" in n):
        if "дентальн" in n: return "Dental50"
        size="100" if "100шт" in n else ("40" if "40шт" in n else ("20" if "20шт" in n else "?"))
        if "земляник" in n: return {"100":"Dental_100_zemlyanika","40":"Dental_40_zemlyanika","20":"Dental_20_zemlyanika"}.get(size,"")
        if "банан" in n: return "Dental_100_banan"
        if "без вкуса" in n: return "Dental_40_natural"
        return {"100":"Dental_100","40":"Dental_40","20":"Dental20"}.get(size,"")
    return ""

# 1. письмо
PASS=os.environ["MAILRU_APP_PASS"]
M=imaplib.IMAP4_SSL("imap.mail.ru",ssl_context=CTX); M.login(MAIL_USER,PASS); M.select("INBOX")
typ,dj=M.search(None,f'(FROM "{SENDER}")'); ost_uids=[]; latest=None
for i in dj[0].split():
    typ,d=M.fetch(i,"(RFC822)"); msg=email.message_from_bytes(d[0][1]); subj=dec(msg.get("Subject"))
    if "Остатки товаров" in subj: ost_uids.append(i); latest=(i,msg,subj)
if not latest: print("нет письма остатков"); M.logout(); raise SystemExit(0)
i,msg,subj=latest
m=re.search(r"от\s+(\d{2})\.(\d{2})\.(\d{4})",subj); datestr=f"{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else datetime.date.today().strftime("%d.%m.%Y")
from email.utils import parsedate_to_datetime
try:
    _dt=parsedate_to_datetime(msg.get("Date")).astimezone(datetime.timezone(datetime.timedelta(hours=3))); timestr=_dt.strftime("%H:%M")
except Exception: timestr=""
title=(f"Остатки на складе — на {datestr} {timestr}").strip()
htmltext=None
for p in msg.walk():
    fn=p.get_filename()
    if fn and dec(fn).lower().endswith(".zip"):
        z=zipfile.ZipFile(io.BytesIO(p.get_payload(decode=True)))
        for nm in z.namelist():
            if nm.lower().endswith(".html"): htmltext=z.read(nm).decode("utf-8-sig")
if not htmltext: print("нет HTML"); M.logout(); raise SystemExit(1)

# 2. парс + сверка
def cells(tr): return [re.sub(r"\s+"," ",html.unescape(re.sub(r"<[^>]+>"," ",c))).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>",tr,re.S|re.I)]
items={}; total=None
for tr in re.findall(r"<tr[^>]*>(.*?)</tr>",htmltext,re.S|re.I):
    c=cells(tr)
    if len(c)==9 and c[0].startswith("Шпиндовск"): total=n1c(c[3]); continue
    if len(c)!=10 or not c[0] or c[0]=="Номенклатура, Артикул": continue
    items[norm(c[0])]=(c[0],n1c(c[4]))
svsum=sum(v[1] for v in items.values())
print(f"товаров: {len(items)} | сумма Свободно: {svsum} | итог 1С: {total}")
if total is not None and svsum!=total: print("СВЕРКА НЕ СОШЛАСЬ — не пишу"); M.logout(); raise SystemExit(1)
# индекс письма по артикулу
by_art={}
for k,(raw,sv) in items.items(): by_art.setdefault(article(raw),[]).append(k)

# 3. креды/лист
def creds_token():
    from google.oauth2 import service_account
    import google.auth.transport.requests as gtr
    env=os.environ.get("GSHEETS_SA_JSON","").strip()
    if env.startswith("{"): cr=service_account.Credentials.from_service_account_info(json.loads(env),scopes=["https://www.googleapis.com/auth/spreadsheets"])
    else: cr=service_account.Credentials.from_service_account_file(os.environ.get("GSHEETS_SA_FILE"),scopes=["https://www.googleapis.com/auth/spreadsheets"])
    cr.refresh(gtr.Request()); return cr.token
TOK=creds_token()
def gapi(m,u,b=None):
    r=urllib.request.Request(u,data=(json.dumps(b).encode() if b is not None else None),method=m,headers={"Authorization":"Bearer "+TOK,"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(r,context=CTX,timeout=60).read())
def L(i):
    s=""; i+=1
    while i>0: i,r=divmod(i-1,26); s=chr(65+r)+s
    return s
vals=gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{urllib.parse.quote(TAB+'!A1:L80')}").get("values",[])
hdr=vals[1]; cN=hdr.index("Номенклатура 1С"); cA=hdr.index("Артикул"); cS=hdr.index("Свободно")
updates=[{"range":f"{TAB}!A1","values":[[title]]}]
used=set(); last_data_row=2
for ri in range(2,len(vals)):
    row=vals[ri]; nm=row[cN] if len(row)>cN else ""; art=row[cA] if len(row)>cA else ""
    if not (nm or art): continue
    if nm.strip().upper()=="ИТОГО":
        updates.append({"range":f"{TAB}!{L(cS)}{ri+1}","values":[[total]]}); continue
    last_data_row=ri+1
    kn=norm(nm); matched=None
    if kn and kn in items and kn not in used: matched=kn
    elif art:
        for k in by_art.get(art,[]):
            if k not in used: matched=k; break
    if matched:
        raw,sv=items[matched]; used.add(matched)
        updates.append({"range":f"{TAB}!{L(cS)}{ri+1}","values":[[sv]]})
        if norm(raw)!=kn:  # номенклатура пустая/иная -> подставляю реальную из письма
            updates.append({"range":f"{TAB}!{L(cN)}{ri+1}","values":[[raw]]})
    elif art:  # артикул есть, в письме нет -> 0
        updates.append({"range":f"{TAB}!{L(cS)}{ri+1}","values":[[0]]})
# новые (в письме, нет в листе) -> дописать
new=[(k,items[k]) for k in items if k not in used]
rowbase=last_data_row+1
for j,(k,(raw,sv)) in enumerate(new):
    rr=rowbase+j
    updates.append({"range":f"{TAB}!{L(cN)}{rr}","values":[[raw]]})
    updates.append({"range":f"{TAB}!{L(cA)}{rr}","values":[[article(raw)]]})
    updates.append({"range":f"{TAB}!{L(cS)}{rr}","values":[[sv]]})
last_row = rowbase+len(new)-1 if new else last_data_row
# ИТОГО переносим в самый низ? оставим где есть. last_row для скрина:
last_row=max(last_row, len(vals))
if DRY: print(f"[DRY] Свободно обновил бы: {len(used)} | новых: {len(new)} | дата {datestr}")
else:
    gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values:batchUpdate",{"valueInputOption":"USER_ENTERED","data":updates})
    print(f"обновлено Свободно: {len(used)} | новых: {len(new)} | дата {datestr}")
    # 3b. текущие остатки в таблицу «даты готовности» — колонку ищем ПО ЗАГОЛОВКУ (устойчиво к вставке столбцов)
    try:
        DG="1pRT8ALdpE3JhJbstbigX2V48DyUMXX1awiBBnFOjChY"; DGTAB="даты готовности"
        ALIAS={"orallubrikant":"spraydlyapolostyrta"}
        def _pack(x):
            mm=re.search(r"(\d+)\s*уп",x or ""); return mm.group(1) if mm else ""
        def _canon(a): a=(a or "").strip().lower(); return ALIAS.get(a,a)
        orng=TAB+chr(33)+"A1:F60"
        ov=gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{urllib.parse.quote(orng)}").get("values",[])
        oh=ov[1]; _cN=oh.index("Номенклатура 1С"); _cA=oh.index("Артикул"); _cS=oh.index("Свободно")
        st={}; ba={}
        for rr in ov[2:]:
            if len(rr)<=_cS: continue
            aa=(rr[_cA] if len(rr)>_cA else "").strip(); nn=rr[_cN] if len(rr)>_cN else ""; vv=rr[_cS] if len(rr)>_cS else ""
            if not aa or aa.upper()=="ИТОГО": continue
            dd=re.sub(r"[^\d]","",str(vv)); vv=int(dd) if dd else 0
            ca=_canon(aa); st[(ca,_pack(nn))]=vv; ba.setdefault(ca,[]).append(vv)
        dgv=gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{DG}/values/{urllib.parse.quote(DGTAB+chr(33)+'A1:L80')}").get("values",[])
        dh=[str(x).lower() for x in dgv[0]]
        def _find(*keys):
            for idx,h in enumerate(dh):
                if all(k in h for k in keys): return idx
            return None
        i_art=_find("артикул"); i_ost=_find("остаток","сегодня"); i_nm=_find("назван")
        if i_nm is None: i_nm=0
        if i_art is None or i_ost is None:
            print("даты готовности: не нашёл заголовки (артикул/остаток сегодня) — пропускаю")
        else:
            up2=[]
            for ii in range(1,len(dgv)):
                rr=dgv[ii]; nm2=rr[i_nm] if len(rr)>i_nm else ""; ar2=(rr[i_art] if len(rr)>i_art else "").strip()
                if not ar2 or ar2.lower()=="тотал" or str(nm2).strip().lower()=="тотал": continue
                ca=_canon(ar2); pk=_pack(nm2); val=None
                if (ca,pk) in st: val=st[(ca,pk)]
                elif len(ba.get(ca,[]))==1: val=ba[ca][0]
                up2.append({"range":f"{DGTAB}!{L(i_ost)}{ii+1}","values":[[val if val is not None else 0]]})
            if up2: gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{DG}/values:batchUpdate",{"valueInputOption":"USER_ENTERED","data":up2})
            print("даты готовности: остаток обновлён,",len(up2),"строк (колонка по заголовку)")
    except Exception as e:
        print("даты готовности: ошибка",str(e)[:140])

# 4. скрин: ВСЕ строки + ВСЕ столбцы (до последнего с данными)
png=None
try:
    import fitz; from PIL import Image
    lastcol=max((len(r) for r in vals), default=cS+1)
    rng=f"A1:{L(lastcol-1)}{last_row}"
    params={"format":"pdf","gid":str(GID),"range":rng,"portrait":"true","fitw":"true","gridlines":"false","sheetnames":"false","printtitle":"false","pagenumbers":"false","top_margin":"0.1","bottom_margin":"0.1","left_margin":"0.1","right_margin":"0.1"}
    url=f"https://docs.google.com/spreadsheets/d/{SID}/export?"+urllib.parse.urlencode(params)
    pdf=urllib.request.urlopen(urllib.request.Request(url,headers={"Authorization":"Bearer "+TOK}),timeout=90,context=CTX).read()
    doc=fitz.open(stream=pdf,filetype="pdf")
    imgs=[Image.open(io.BytesIO(p.get_pixmap(matrix=fitz.Matrix(3,3)).tobytes("png"))).convert("RGB") for p in doc]
    w=max(im.width for im in imgs); h=sum(im.height for im in imgs)
    canvas=Image.new("RGB",(w,h),"white"); yy=0
    for im in imgs: canvas.paste(im,(0,yy)); yy+=im.height
    bbox=Image.eval(canvas.convert("L"),lambda p:255-p).getbbox()
    cropped=canvas.crop(bbox) if bbox else canvas
    b=io.BytesIO(); cropped.save(b,"PNG"); b.seek(0); png=b.read(); print("скрин готов:",rng,len(png),"б")
except Exception as e: print("скрин не удался:",str(e)[:150])

# 5. отправка
sent=False
if png:
    CHAT=(os.environ.get("OSTATKI_CHAT_ID") or "339473235").strip(); TGTOK=os.environ["TELEGRAM_BOT_TOKEN"].strip(); cap="🧬остатки на нашем складе сегодня"
    if DRY: open("/tmp/ostatki_preview.png","wb").write(png); print("[DRY] скрин -> /tmp/ostatki_preview.png")
    else:
        boundary="----ost"; body=io.BytesIO()
        def part(n,v): body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n".encode())
        part("chat_id",CHAT); part("caption",cap)
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"ost.png\"\r\nContent-Type: image/png\r\n\r\n".encode()); body.write(png); body.write(f"\r\n--{boundary}--\r\n".encode())
        try:
            res=json.loads(urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{TGTOK}/sendPhoto",data=body.getvalue(),headers={"Content-Type":f"multipart/form-data; boundary={boundary}"}),timeout=60,context=CTX).read()); sent=res.get("ok",False); print("отправка:",sent,"" if sent else res)
        except urllib.error.HTTPError as e:
            print("TG HTTP",e.code,"chat=",repr(CHAT),"tok_len=",len(TGTOK),"err:",e.read()[:200].decode(errors="replace"))

# 6. удалить письмо остатков (только после успеха)
if DRY: print("[DRY] письмо не удаляю")
elif sent and os.environ.get("NODELETE")!="1":
    for u in ost_uids: M.store(u,"+FLAGS","\\Deleted")
    M.expunge(); print(f"удалено писем остатков: {len(ost_uids)}")
elif os.environ.get("NODELETE")=="1": print("NODELETE=1 — письмо сохранено")
else: print("отправка не прошла — письмо НЕ удаляю")
M.logout(); print("GOTOVO")
