#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# WB ФФ low-stock: раз в сутки (будни 10:00 МСК) — остаток ВБ FBS по КАЖДОМУ ФФ-складу и артикулу.
# Ниже порога (14-дн оборачиваемость, свой порог на товар×склад) -> ОДИН объединённый алерт в канал
# «АС Фарм изменения», тег @Ira_Zorina. Жирная только первая строка.
# Эпизодная логика (без спама): алерт ОДИН раз при падении ниже порога; восстановили выше -> снова
# вооружён; если товар 0 -> алерт один раз, дальше молчим (решили не грузить на этот ФФ); появился >0 ->
# снова пристальная слежка. Несколько просадок за прогон -> один общий алерт, не плодим.
# Надёжность (уроки прошлых поломок): Яндекс-таймер (крон GitHub пропускает окна); 429 -> паузы+ретрай;
# TELEGRAM_BOT_TOKEN .strip() (висячий \n); состояние коммитит воркфлоу (git pull --rebase + retry).
import os, re, json, ssl, time, urllib.request, urllib.error

CTX = ssl._create_unverified_context()
WB = os.environ["WB_TOKEN"].strip()
MP = "https://marketplace-api.wildberries.ru"
CONTENT = "https://content-api.wildberries.ru"
STATE_FILE = "data/wb_ff_state.json"
DRY = os.environ.get("DRY") == "1"

def _wb(url, method="GET", body=None):
    for _try in range(2):
        try:
            r = urllib.request.Request(url, data=(json.dumps(body).encode() if body is not None else None),
                                       method=method, headers={"Authorization": WB, "Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(r, context=CTX, timeout=60).read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and _try == 0:
                print("  WB 429 -> пауза 25с, один ретрай"); time.sleep(25); continue
            raise
    return {}

def an(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower())

# порог: артикул -> (Москва/Карго Софьино, Казань, Екб). 14-дн оборачиваемость.
WBFF_THR = {
    "Dental20":(280,100,100), "Dental_40":(300,70,90), "Dental_100":(560,200,200), "Dental50":(140,20,20),
    "Dental_20_zemlyanika":(140,60,60), "Dental_40_zemlyanika":(140,40,40), "Dental_100_zemlyanika":(140,50,50),
    "Dental_100_banan":(100,50,50), "Dental_40_natural":(250,50,60), "extract_romashka":(54,54,54),
    "extract_pihta":(54,54,54), "makeup_50":(54,54,54), "CrioGel1l":(5,5,5), "Crio_L25(new)":(10,5,5),
    "CrioL50":(20,5,5), "cryolipolysis25":(10,3,3), "Cryolipolysis50":(10,3,3),
    "spraydlyapolostyrta":(110,30,50), "Oral_cherry":(42,42,42),
}
THRn = {an(k): v for k, v in WBFF_THR.items()}
CITY = {"мск":"Москва", "казань":"Казань", "екб":"Екб"}
COL = {"мск":0, "казань":1, "екб":2}
ORDER = {"мск":0, "казань":1, "екб":2}
def wb_city(name):
    n = (name or "").lower()
    if "краснодар" in n: return None
    if "софьино" in n or "карго" in n: return "мск"
    if "казан" in n: return "казань"
    if "екб" in n or "екат" in n or "черняхов" in n: return "екб"
    return None

def load_state():
    try: return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception: return {}
def save_state(st):
    os.makedirs("data", exist_ok=True)
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def tg(text, chat=None):
    tok = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    chat = chat or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    body = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    r = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=body, headers={"Content-Type":"application/json"})
    res = json.loads(urllib.request.urlopen(r, context=CTX, timeout=60).read())
    print("TG:", res.get("ok"), "" if res.get("ok") else res); return res.get("ok", False)

def collect():
    """Возвращает {(city, art): qty} по ФФ-складам + список складов."""
    whs = _wb(f"{MP}/api/v3/warehouses")
    bc2art = {}; cursor = {"limit": 1000}
    for _ in range(30):
        d = _wb(f"{CONTENT}/content/v2/get/cards/list", "POST", {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}})
        cards = d.get("cards", [])
        for c in cards:
            vc = c.get("vendorCode", "")
            for s in c.get("sizes", []):
                for bc in s.get("skus", []):
                    if bc: bc2art[str(bc)] = vc
        cur = d.get("cursor", {})
        if len(cards) < 1000: break
        cursor = {"limit": 1000, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
        time.sleep(0.3)
    allbc = list(bc2art)
    res = {}; ok = set(); failed = set()
    for w in whs:
        city = wb_city(w.get("name"))
        if not city: continue
        wid = w.get("id"); got = False
        for i in range(0, len(allbc), 1000):
            try:
                stx = _wb(f"{MP}/api/v3/stocks/{wid}", "POST", {"skus": allbc[i:i + 1000]})
                for s in stx.get("stocks", []):
                    a = bc2art.get(str(s.get("sku")), "")
                    res[(city, a)] = res.get((city, a), 0) + (s.get("amount", 0) or 0)
                got = True
            except Exception as e:
                print("  СКЛАД НЕ СОБРАЛСЯ:", city, str(e)[:60]); failed.add(city)
            time.sleep(3)   # пауза между батчами (WB /stocks лимит)
        if got and city not in failed: ok.add(city)
        time.sleep(5)       # пауза между складами
    if not allbc: failed.add("*нет баркодов*")   # карточки не отдались -> тоже поломка
    return res, ok, failed

def main():
    try:
        data, ok, failed = collect()
    except Exception as e:
        print("СБОР УПАЛ ПОЛНОСТЬЮ:", str(e)[:80])
        if not DRY: tg("<b>❌ остатки по ФФ сегодня не проверял, что то сломалось.</b>")
        return
    # СТОРОЖ: какой-то ФФ-склад (или все) не собрался -> алерт в канал, обычные просадки НЕ шлём
    # (чтобы пустой склад не дал ложных «0 шт»); состояние НЕ трогаем -> следующий прогон переиграет.
    if (not ok) or failed:
        print("СТОРОЖ: ok=", ok, "failed=", failed)
        if not DRY: tg("<b>❌ остатки по ФФ сегодня не проверял, что то сломалось.</b>")
        return
    st = load_state()
    drops = []  # (order, Город, art, qty, thr)
    for (city, art), qty in data.items():
        t = THRn.get(an(art))
        if not t: continue
        thr = t[COL[city]]; key = f"{city}|{an(art)}"
        prev = st.get(key, {}); prev_last = prev.get("last"); alerted = prev.get("alerted", False)
        if qty >= thr: alerted = False                       # выше порога -> снова вооружён
        elif prev_last == 0 and qty > 0: alerted = False     # появился из 0 -> пристальная слежка
        if qty < thr and not alerted:
            drops.append((ORDER[city], CITY[city], art, qty, thr)); alerted = True
        st[key] = {"last": qty, "alerted": alerted}
    if drops:
        drops.sort(key=lambda x: (x[0], -x[4], x[2]))
        by_city = {}
        for order, gorod, art, qty, thr in drops:
            by_city.setdefault((order, gorod), []).append((art, qty, thr))
        blocks = []
        for kc in sorted(by_city):
            ls = [f"🍟 <b>WB ФФ {kc[1]} остатки ниже порога:</b>"]
            for art, qty, thr in by_city[kc]:
                ls.append(f"• {art}: {qty} шт (порог {thr})")
            blocks.append("\n".join(ls))
        msg = "\n\n".join(blocks) + "\n\n@Ira_Zorina пора планировать поставку!"
        print("ALERT:\n" + msg)
        if not DRY: tg(msg)
    else:
        print("просадок нет")
    if not DRY: save_state(st)
    print(f"GOTOVO | складов ФФ: {len(ok)} ({sorted(ok)}) | просадок: {len(drops)}")

if __name__ == "__main__":
    main()
