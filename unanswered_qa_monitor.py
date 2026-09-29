# -*- coding: utf-8 -*-
# ВБ-монитор: если вопрос/отзыв без ответа >36 часов — ОДИН алерт в канал.
# Каждые 3 часа. После алерта пауза 24ч (отдельно для вопросов и отзывов).
# Вопросы и отзывы — раздельно. Источник — WB feedbacks-api (бесплатно).
import os, json, ssl, time, urllib.request, urllib.parse, datetime

WB=os.environ["WB_TOKEN"]; BOT=os.environ["TELEGRAM_BOT_TOKEN"]; CHAT=os.environ["TELEGRAM_CHAT_ID"]
STATE="data/unanswered_qa_state.json"
CTX=ssl.create_default_context()
THRESH_H=36.0; PAUSE_H=24.0
FB="https://feedbacks-api.wildberries.ru"

def parse_dt(s):
    s=(s or "").strip().replace("Z","+00:00")
    dt=datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None: dt=dt.replace(tzinfo=datetime.timezone.utc)
    return dt

def wb_get(path):
    for a in range(4):
        try:
            r=urllib.request.Request(FB+path, headers={"Authorization":WB})
            return json.loads(urllib.request.urlopen(r,context=CTX,timeout=45).read())
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(22); continue
            raise
    return None

def oldest_age_h(kind):
    # kind = questions | feedbacks ; берём самые старые первыми
    d=wb_get(f"/api/v1/{kind}?isAnswered=false&take=30&skip=0&order=dateAsc")
    if d is None: return None
    arr=((d.get("data") or {}).get(kind)) or []
    if not arr: return 0.0
    now=datetime.datetime.now(datetime.timezone.utc)
    ages=[(now-parse_dt(it.get("createdDate"))).total_seconds()/3600 for it in arr if it.get("createdDate")]
    return max(ages) if ages else 0.0

def send(text):
    body=urllib.parse.urlencode({"chat_id":CHAT,"text":text,"parse_mode":"HTML","disable_web_page_preview":"true"}).encode()
    urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{BOT}/sendMessage",data=body),timeout=25).read()

def load():
    try: return json.load(open(STATE))
    except Exception: return {}
def save(s):
    os.makedirs("data",exist_ok=True); json.dump(s,open(STATE,"w"),ensure_ascii=False,indent=1)

def main():
    st=load(); now=datetime.datetime.now(datetime.timezone.utc)
    plan=[("questions","💧","вопросы"),("feedbacks","✏️","отзывы")]
    for kind,emoji,word in plan:
        age=oldest_age_h(kind)
        if age is None:
            print(f"{kind}: API не ответил — пропускаю"); continue
        print(f"{kind}: самый старый без ответа = {age:.1f} ч (порог {THRESH_H})")
        if age > THRESH_H:
            la=st.get(kind,{}).get("last_alert")
            paused = la is not None and (now-parse_dt(la)).total_seconds()/3600 < PAUSE_H
            if paused:
                print(f"  есть >36ч, но пауза 24ч ещё идёт (last_alert {la}) — молчу")
            else:
                send(f"{emoji}Появляются <b>{word}</b> без ответа на ВБ, висит уже более 36 часов, покупатель ждет :)")
                st.setdefault(kind,{})["last_alert"]=now.isoformat()
                print(f"  АЛЕРТ отправлен ({word})")
    save(st); print("GOTOVO")

if __name__=="__main__": main()
