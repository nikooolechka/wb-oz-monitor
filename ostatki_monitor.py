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
# СТРОГИЙ КЛЮЧ матча = (артикул + упаковка Nуп): устойчив к порядку строк и вариациям имени,
# ФИЗИЧЕСКИ исключает перестановку значений (денталы 24/80 — один артикул, разные короба). См. память.
def _pak(s): mm=re.search(r"(\d+)\s*уп",s or ""); return mm.group(1) if mm else ""
def _can(a): a=(a or "").strip().lower(); return {"orallubrikant":"spraydlyapolostyrta"}.get(a,a)
ekey={}; edup=set()
for _k,(raw,sv) in items.items():
    ea=_can(article(raw))
    if ea:
        _kk=(ea,_pak(raw))
        if _kk in ekey: edup.add(_kk)
        ekey[_kk]=_k
for ri in range(2,len(vals)):
    row=vals[ri]; nm=row[cN] if len(row)>cN else ""; art=row[cA] if len(row)>cA else ""
    if not (nm or art): continue
    if nm.strip().upper()=="ИТОГО":
        updates.append({"range":f"{TAB}!{L(cS)}{ri+1}","values":[[total]]}); continue
    last_data_row=ri+1
    matched=None; sk=(_can(art),_pak(nm))
    if sk[0] and sk in ekey and sk not in edup and ekey[sk] not in used:
        matched=ekey[sk]                                   # 1) строгий ключ артикул+упаковка
    if matched is None:
        kn=norm(nm)
        if kn and kn in items and kn not in used: matched=kn   # 2) запас — по наименованию
    if matched:
        raw,sv=items[matched]; used.add(matched)
        updates.append({"range":f"{TAB}!{L(cS)}{ri+1}","values":[[sv]]})
        if norm(raw)!=norm(nm):  # номенклатура пустая/иная -> подставляю реальную из письма
            updates.append({"range":f"{TAB}!{L(cN)}{ri+1}","values":[[raw]]})
    elif art:  # артикул есть, в письме нет -> 0
        updates.append({"range":f"{TAB}!{L(cS)}{ri+1}","values":[[0]]})
# новые (в письме, нет в листе) -> дописать строкой; Артикул = ЖИРНОЕ "новая позиция",
# чтобы владелец увидела в скрине и прислала настоящий артикул. Шкалы у новой строки НЕТ
# (нет потребности -> D пустая, без ошибки). Свободно всё равно собираем и показываем.
new=[(k,items[k]) for k in items if k not in used]
rowbase=last_data_row+1
newfmt=[]
for j,(k,(raw,sv)) in enumerate(new):
    rr=rowbase+j
    updates.append({"range":f"{TAB}!{L(cN)}{rr}","values":[[raw]]})
    updates.append({"range":f"{TAB}!{L(cA)}{rr}","values":[["новая позиция"]]})
    updates.append({"range":f"{TAB}!{L(cS)}{rr}","values":[[sv]]})
    newfmt.append({"repeatCell":{"range":{"sheetId":1290662357,"startRowIndex":rr-1,"endRowIndex":rr,"startColumnIndex":cA,"endColumnIndex":cA+1},"cell":{"userEnteredFormat":{"textFormat":{"bold":True}}},"fields":"userEnteredFormat.textFormat.bold"}})
last_row = rowbase+len(new)-1 if new else last_data_row
# ИТОГО переносим в самый низ? оставим где есть. last_row для скрина:
last_row=max(last_row, len(vals))
if DRY: print(f"[DRY] Свободно обновил бы: {len(used)} | новых: {len(new)} | дата {datestr}")
else:
    gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values:batchUpdate",{"valueInputOption":"USER_ENTERED","data":updates})
    if newfmt: gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}:batchUpdate",{"requests":newfmt})
    print(f"обновлено Свободно: {len(used)} | новых: {len(new)} | дата {datestr}")
    # САМОПРОВЕРКА (авто, БЕЗ алертов владельцу): перечитываю записанное и сверяю сумму И ПОСТРОЧНО.
    # Не сошлось -> скрин НЕ шлю и письмо НЕ удаляю -> следующий поток (кажд.10 мин/завтра) переиграет сам.
    try:
        _bk=gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{urllib.parse.quote(TAB+'!A1:L120')}?valueRenderOption=UNFORMATTED_VALUE").get("values",[])
        _bad=[]; _bsum=0
        for _ri in range(2,len(_bk)):
            _r=_bk[_ri]; _nm=_r[cN] if len(_r)>cN else ""; _at=_r[cA] if len(_r)>cA else ""
            if not(str(_nm).strip() or str(_at).strip()) or str(_nm).strip().upper()=="ИТОГО": continue
            _sv=n1c(_r[cS] if len(_r)>cS else 0); _bsum+=_sv
            _sk=(_can(_at),_pak(_nm))
            if _sk[0] and _sk in ekey and _sk not in edup:
                _exp=items[ekey[_sk]][1]
                if _sv!=_exp: _bad.append((_at,_pak(_nm),_sv,_exp))
        if (total is not None and _bsum!=total) or _bad:
            print(f"САМОПРОВЕРКА НЕ ПРОШЛА — скрин НЕ шлю, письмо НЕ удаляю. сумма={_bsum} итог={total} несоответствий={len(_bad)} {_bad[:6]}")
            M.logout(); raise SystemExit(1)
        print(f"самопроверка ОК: сумма {_bsum} == итог {total}, построчно сошлось ({len(used)} позиций)")
    except SystemExit: raise
    except Exception as _e:
        print("самопроверка: ошибка чтения (не блокирую отправку):",str(_e)[:100])
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

    # 3c. ПРОИЗВОДСТВО (цикл 22->22): произведено = свободно(база цикла) + Σ приростов; D=потребность, H=произведено, I=%, J=шкала(SPARKLINE)
    try:
        OSTGID=1290662357; _AL={"orallubrikant":"spraydlyapolostyrta"}
        def _cn(a): a=(a or "").strip().lower(); return _AL.get(a,a)
        def _pk(s2): mm=re.search(r"(\d+)\s*уп",s2 or ""); return mm.group(1) if mm else ""
        def _gh(p):
            p=max(0.0,min(p,1.2)); _st=[(0.0,(0.91,0.37,0.37)),(0.5,(1.0,0.84,0.42)),(0.8,(0.80,0.84,0.45)),(0.95,(0.52,0.80,0.47)),(1.2,(0.26,0.74,0.36))]
            for _i in range(len(_st)-1):
                a,ca=_st[_i]; b,cb=_st[_i+1]
                if p<=b:
                    t=(p-a)/(b-a) if b>a else 0
                    return "#%02X%02X%02X"%(int((ca[0]+(cb[0]-ca[0])*t)*255),int((ca[1]+(cb[1]-ca[1])*t)*255),int((ca[2]+(cb[2]-ca[2])*t)*255))
            c=_st[-1][1]; return "#%02X%02X%02X"%(int(c[0]*255),int(c[1]*255),int(c[2]*255))
        _n=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
        if _n.day>=22: _cy,_cm=_n.year,_n.month
        else:
            _cm=_n.month-1; _cy=_n.year
            if _cm==0: _cm=12; _cy-=1
        cyc=f"{_cy}-{_cm:02d}"; ST="произв_стейт"
        _m=gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}?fields=sheets(properties(title))")
        if ST not in [x["properties"]["title"] for x in _m["sheets"]]:
            gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}:batchUpdate",{"requests":[{"addSheet":{"properties":{"title":ST,"hidden":True,"gridProperties":{"rowCount":300,"columnCount":5}}}}]})
        stv=gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{urllib.parse.quote(ST+chr(33)+'A1:D300')}").get("values",[])
        stm={r[0]:(n1c(r[1]),n1c(r[2]),(r[3] if len(r)>3 else "")) for r in stv if r and r[0]}
        newst={}; prod_art={}
        for k,(raw,sv) in items.items():
            key=_cn(article(raw))+"|"+_pk(raw); st0=stm.get(key)
            if (st0 is None) or (st0[2]!=cyc): prod=sv; last=sv
            else:
                d=sv-st0[0]; prod=st0[1]+(d if d>0 else 0); last=sv
            newst[key]=(last,prod,cyc); _a=_cn(article(raw)); prod_art[_a]=prod_art.get(_a,0)+prod
        strows=[[k,newst[k][0],newst[k][1],newst[k][2]] for k in newst]
        gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{urllib.parse.quote(ST+chr(33)+'A1:D300')}:clear",{})
        if strows: gapi("PUT",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{urllib.parse.quote(ST+chr(33)+'A1')}?valueInputOption=USER_ENTERED",{"values":strows})
        _dg=gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{DG}/values/{urllib.parse.quote(DGTAB+chr(33)+'A1:L80')}").get("values",[])
        _dh=[str(x).lower() for x in _dg[0]]
        def _fd(*kk):
            for i,h in enumerate(_dh):
                if all(x in h for x in kk): return i
            return None
        _ia=_fd("артикул"); _ip=_fd("потребность"); potra={}
        for r in _dg[1:]:
            a=(r[_ia] if len(r)>_ia else "").strip()
            if not a or a.lower()=="тотал": continue
            v=n1c(r[_ip] if len(r)>_ip else ""); c=_cn(a)
            if c not in potra and v: potra[c]=v
        # расширить сетку до 11 столбцов (бар D + служебные H/I/J)
        gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}:batchUpdate",{"requests":[{"updateSheetProperties":{"properties":{"sheetId":OSTGID,"gridProperties":{"columnCount":11}},"fields":"gridProperties.columnCount"}}]})
        ov2=gapi("GET",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{urllib.parse.quote(TAB+chr(33)+'A1:K80')}").get("values",[])
        seen=set(); vw=[{"range":f"{TAB}!D1","values":[["шкала выполнения относительно потребности"]]},{"range":f"{TAB}!I1","values":[["потребность"]]},{"range":f"{TAB}!J1","values":[["% выполнения"]]}]; fmt=[]
        for i in range(2,len(ov2)):
            r=ov2[i]; nm=r[0] if r else ""; art=(r[1] if len(r)>1 else "").strip()
            if not art or art.upper()=="ИТОГО" or str(nm).strip().upper()=="ИТОГО": continue
            c=_cn(art)
            if c in seen: continue
            seen.add(c); row=i+1; pot=potra.get(c); prod=prod_art.get(c,0)
            vw.append({"range":f"{TAB}!H{row}","values":[[prod]]})          # произведено (служебн.)
            vw.append({"range":f"{TAB}!I{row}","values":[[pot if pot is not None else ""]]})  # потребность (служебн.)
            if pot:
                pct=prod/pot; vw.append({"range":f"{TAB}!J{row}","values":[[pct]]})           # % (служебн.)
                col=_gh(pct)
                bar=max(0,min(int(round(pct*100)),100))   # доля заливки 0..100 ЛИТЕРАЛОМ в формулу
                # число зашито ПРЯМО в SPARKLINE, без ссылок на H/I -> шкала НЕ ломается (#REF!), если владелец
                # удалит/сдвинет служебные колонки потребность/%. Цвет-градиент — по реальному pct.
                vw.append({"range":f"{TAB}!D{row}","values":[['=SPARKLINE('+str(bar)+';{"charttype"\\"bar";"max"\\100;"color1"\\"'+col+'";"empty"\\"zero"})']]})  # БАР в D (самодостаточный)
                fmt.append({"repeatCell":{"range":{"sheetId":OSTGID,"startRowIndex":i,"endRowIndex":i+1,"startColumnIndex":9,"endColumnIndex":10},"cell":{"userEnteredFormat":{"numberFormat":{"type":"PERCENT","pattern":"0%"}}},"fields":"userEnteredFormat.numberFormat"}})
            else:
                vw.append({"range":f"{TAB}!D{row}","values":[[""]]})   # нет потребности -> чистим шкалу (не оставляем #REF!)
        gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values:batchUpdate",{"valueInputOption":"USER_ENTERED","data":vw})
        if fmt: gapi("POST",f"https://sheets.googleapis.com/v4/spreadsheets/{SID}:batchUpdate",{"requests":fmt})
        print("производство: D/H/%/шкала,",len(seen),"арт | цикл",cyc)
    except Exception as e:
        print("производство: ошибка",str(e)[:140])

# 4. скрин: ВСЕ строки + ВСЕ столбцы (до последнего с данными)
png=None
try:
    import fitz; from PIL import Image
    lastcol=max((len(r) for r in vals), default=cS+1)
    rng=f"A1:G{last_row}"  # служебные H/I/J (произв/потребн/%) в скрин НЕ включаем
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
    CHAT=(os.environ.get("OSTATKI_CHAT_ID") or "339473235").strip(); TGTOK=os.environ["TELEGRAM_BOT_TOKEN"].strip(); cap="<b>🧬остатки на нашем складе сегодня</b>"
    if DRY: open("/tmp/ostatki_preview.png","wb").write(png); print("[DRY] скрин -> /tmp/ostatki_preview.png")
    else:
        boundary="----ost"; body=io.BytesIO()
        def part(n,v): body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n".encode())
        part("chat_id",CHAT); part("caption",cap); part("parse_mode","HTML")
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
