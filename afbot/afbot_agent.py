# Агент af-bot: слушает ОТДЕЛЬНЫЙ пульт (столбец C), выполняет PS-команды, пишет вывод. headless.
# Пульт (чтобы НЕ конфликтовать с SERVAF, который на столбце A): C2=команда, C1=лог, C4=heartbeat.
import os,sys,json,time,subprocess,urllib.request,urllib.parse,ssl,datetime
D=os.path.join(os.environ.get("USERPROFILE","."),"asfarm")
sys.path.insert(0,os.path.join(D,"py","Lib","site-packages"))
ssl._create_default_https_context=ssl._create_unverified_context
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GReq
import requests as rq
SID="1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"
CMD="Лист1!C2"; LOG="Лист1!C1"; HB="Лист1!C4"
cr=service_account.Credentials.from_service_account_file(os.path.join(D,"sa.json"),scopes=["https://www.googleapis.com/auth/spreadsheets"])
s=rq.Session(); s.verify=False
def tok(): cr.refresh(GReq(s)); return cr.token
def rd(c):
    u=f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{urllib.parse.quote(c)}"
    v=json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"Authorization":"Bearer "+tok()}),timeout=25)).get("values")
    return (v[0][0] if v and v[0] else "")
def wr(c,v):
    u=f"https://sheets.googleapis.com/v4/spreadsheets/{SID}/values/{urllib.parse.quote(c)}?valueInputOption=RAW"
    urllib.request.urlopen(urllib.request.Request(u,data=json.dumps({"values":[[v]]}).encode(),method="PUT",headers={"Authorization":"Bearer "+tok(),"Content-Type":"application/json"}),timeout=25).read()
def now(): return (datetime.datetime.utcnow()+datetime.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
last=""
try: wr(LOG, f"=== af-bot agent STARTED {now()} ===")
except Exception: pass
while True:
    try:
        try: wr(HB, "alive "+now())
        except Exception: pass
        cmd=rd(CMD)
        if cmd and cmd!=last:
            last=cmd
            if cmd.strip().startswith("PS:"):
                ps=cmd.strip()[3:]
                try:
                    o=subprocess.run(["powershell","-NoProfile","-Command",ps],capture_output=True,text=True,timeout=600,encoding="utf-8",errors="replace")
                    res=(o.stdout or "")+("\n--stderr--\n"+o.stderr if o.stderr else "")
                except Exception as e:
                    res="RUN ERR: "+repr(e)[:300]
                wr(LOG, f"=== PS {now()} ===\n$ {ps[:200]}\n\n{res[:45000]}")
                try: wr(CMD,"")
                except Exception: pass
    except Exception:
        pass
    time.sleep(20)
