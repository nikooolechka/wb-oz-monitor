# -*- coding: utf-8 -*-
# Сторож остатков-по-запросу. Алерт в канал, если:
#  (1) приёмник (Яндекс-шлюз) не доставляет — в Telegram webhook pending>0 И свежая ошибка доставки;
#  (2) слушатель на ПК замолчал — сердцебиение (пульт Лист1!E5) устарело (>15 мин).
# Дедуп: один алерт на проблему; при восстановлении — сбрасываем (и шлём ✅).
import os, json, ssl, re, urllib.request, urllib.parse, datetime
CTX=ssl.create_default_context()
try: CTX=ssl._create_unverified_context()
except Exception: pass
TGTOK=os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT=os.environ["TELEGRAM_CHAT_ID"].strip()
SA=json.loads(os.environ["GSHEETS_SA_JSON"])
PULT="1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"; HB_CELL="Лист1!E5"; CMD_CELL="Лист1!E2"; RES_CELL="Лист1!E3"
STATE="data/ostatki_watchdog_state.json"
MSK=datetime.timezone(datetime.timedelta(hours=3))

def tg(method,params):
    body=json.dumps(params).encode()
    urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{TGTOK}/{method}",data=body,headers={"Content-Type":"application/json"}),context=CTX,timeout=30).read()
def tapi(method):
    return json.loads(urllib.request.urlopen(f"https://api.telegram.org/bot{TGTOK}/{method}",context=CTX,timeout=30).read()).get("result",{})

def sa_token():
    from google.oauth2 import service_account
    import google.auth.transport.requests as gtr
    cr=service_account.Credentials.from_service_account_info(SA,scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    cr.refresh(gtr.Request()); return cr.token
def cell(c):
    u=f"https://sheets.googleapis.com/v4/spreadsheets/{PULT}/values/{urllib.parse.quote(c)}"
    d=json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={"Authorization":"Bearer "+sa_token()}),context=CTX,timeout=30).read())
    v=d.get("values"); return (v[0][0] if v and v[0] else "")

def load():
    try: return json.load(open(STATE,encoding="utf-8"))
    except Exception: return {}
def save(s):
    os.makedirs("data",exist_ok=True); json.dump(s,open(STATE,"w",encoding="utf-8"),ensure_ascii=False,indent=2)

now=datetime.datetime.now(MSK)
st=load(); problems=[]

# (1) приёмник: webhook pending>0 И свежая ошибка (<30 мин) = команда реально застряла
try:
    wi=tapi("getWebhookInfo")
    pending=wi.get("pending_update_count",0); led=wi.get("last_error_date")
    err_recent = bool(led) and (now.timestamp()-led < 30*60)
    gw_down = pending>0 and err_recent
    print(f"[wd] webhook pending={pending} last_error={wi.get('last_error_message')} recent={err_recent} -> gw_down={gw_down}",flush=True)
except Exception as e:
    gw_down=False; print("[wd] webhook check err",str(e)[:120],flush=True)

# (2) слушатель ПК: сердцебиение
pc_down=False
try:
    hb=cell(HB_CELL)  # "alive YYYY-MM-DD HH:MM:SS"
    m=re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",hb or "")
    if m:
        t=datetime.datetime.strptime(m.group(1),"%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
        age=(now-t).total_seconds()/60
        pc_down = age>15
        print(f"[wd] heartbeat age={age:.1f} мин -> pc_down={pc_down}",flush=True)
    else:
        print("[wd] heartbeat нет/пусто (слушатель ещё не писал?) — НЕ алертим",flush=True)
except Exception as e:
    print("[wd] heartbeat check err",str(e)[:120],flush=True)

# алерты с дедупом
def handle(flag,key,msg):
    was=st.get(key,False)
    if flag and not was:
        tg("sendMessage",{"chat_id":CHAT,"text":msg,"parse_mode":"HTML"}); print("[wd] ALERT:",key,flush=True)
    if (not flag) and was:
        rec={"gw":"приёмник","pc":"слушатель ПК","stuck":"запрос"}.get(key,key)
        tg("sendMessage",{"chat_id":CHAT,"text":"✅ Остатки по запросу — "+rec+" снова в норме."}); print("[wd] recovered:",key,flush=True)
    st[key]=flag

# (3) ЗАВИСШИЙ ЗАПРОС: команда в E2 есть, а результата (DONE/FAIL этого же id) в E3 нет дольше 10 мин
stuck=False
try:
    cmd=cell(CMD_CELL); res=cell(RES_CELL)
    mc=re.search(r"OSTATKI\s+(\S+)",cmd or ""); mr=re.search(r"(DONE|FAIL)\s+(\S+)",res or "")
    cid=mc.group(1) if mc else None
    done_id=mr.group(2) if mr else None
    if cid and cid!=done_id:                 # есть незавершённая команда
        pc_seen=st.get("pending")            # {id, first_ts}
        if not pc_seen or pc_seen.get("id")!=cid:
            st["pending"]={"id":cid,"first_ts":now.timestamp()}   # впервые видим этот запрос
        else:
            age=(now.timestamp()-pc_seen.get("first_ts",now.timestamp()))/60
            print(f"[wd] незавершённый запрос {cid} возраст {age:.1f} мин",flush=True)
            if age>10: stuck=True
    else:
        st["pending"]=None                   # завершён -> сбрасываем
except Exception as e:
    print("[wd] stuck check err",str(e)[:120],flush=True)

handle(gw_down,"gw","<b>🔴 Приёмник остатков (шлюз) не отвечает</b>\nКоманда «остатки» сейчас застревает. Проверь/подними приёмник.")
handle(pc_down,"pc","<b>🔴 Слушатель остатков на ПК замолчал</b>\nОстатки по запросу «остатки» сейчас не отработают. Проверь ПК.")
handle(stuck,"stuck","<b>🔴 Запрос «остатки» завис</b>\nКоманда пришла, но за 10+ мин не отработала и нет ответа. Проверь ПК/1С.")
save(st)
print("GOTOVO",flush=True)
