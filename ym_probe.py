"""Проба: можно ли взять карточку market.yandex.ru БЕЗ SmartCaptcha, если ходить
через сервер подписки Happ (гипотеза: с российского/чистого IP Яндекс не капчит).
Перебирает серверы (РФ первыми), поднимает xray→socks, тянет карточку, пишет,
была ли капча и нашлась ли цена. Только диагностика, ничего не пишет.
Секреты: HAPP_SUB_URL, HAPP_HWID, YM_CARD (URL карточки)."""
import os, ssl, json, subprocess, time, re, urllib.request

SUB = os.environ["HAPP_SUB_URL"]; HWID = os.environ["HAPP_HWID"]
CARD = os.environ.get("YM_CARD", "https://market.yandex.ru/card/x/102196144368")
PORT = 10856
ctx = ssl._create_unverified_context()

raw = urllib.request.urlopen(urllib.request.Request(SUB,
      headers={"User-Agent": "Happ/4.11.0", "X-Hwid": HWID}), timeout=40, context=ctx).read().decode("utf-8", "ignore")
try:
    arr = json.loads(raw)
except Exception:
    import base64
    arr = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore"))


def outs(pred):
    r = []
    for el in arr:
        if not isinstance(el, dict):
            continue
        rem = el.get("remarks", "")
        if not pred(rem):
            continue
        for o in el.get("outbounds", []):
            if o.get("protocol") in ("trojan", "vless"):
                s = o.get("settings", {})
                srv = (s.get("servers") or s.get("vnext") or [{}])[0]
                if srv.get("address") and srv["address"] != "0.0.0.0":
                    r.append((rem[:26], o.get("protocol"), o)); break
    return r

servers = outs(lambda r: "🇷🇺" in r) + [x for x in outs(lambda r: "🇷🇺" not in r)][:6]
print("серверов к пробе:", len(servers), flush=True)
from curl_cffi import requests as cr
for rem, proto, ob in servers[:14]:
    cfg = {"log": {"loglevel": "error"},
           "inbounds": [{"port": PORT, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": False}}],
           "outbounds": [dict(ob, tag="proxy"), {"protocol": "freedom", "tag": "direct"}]}
    open("p.json", "w").write(json.dumps(cfg))
    p = subprocess.Popen(["./xray", "run", "-c", "p.json"], stdout=open("px.log", "w"),
                         stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(5)
    try:
        s = cr.Session(impersonate="chrome")
        s.proxies = {"http": "socks5://127.0.0.1:%d" % PORT, "https": "socks5://127.0.0.1:%d" % PORT}
        ip = s.get("https://api.ipify.org", timeout=15).text.strip()
        r = s.get(CARD, timeout=40)
        t = r.text
        cap = ("captcha" in t.lower() and len(t) < 40000)
        print("[%s/%s] IP %s HTTP %s len %d captcha:%s" % (rem, proto, ip, r.status_code, len(t), cap), flush=True)
        if not cap and len(t) > 40000:
            print("  >>> БЕЗ КАПЧИ! куски с ценой:", flush=True)
            for kw in ["snippet-price-current", '"price"', "data-auto=\"price", "Плюс", "currentPrice"]:
                i = t.find(kw)
                if i > 0:
                    print("    ", kw, "→", re.sub(r"<[^>]+>", " ", t[i:i+90]).replace("\n", " "), flush=True)
            p.terminate(); break
    except Exception as e:
        print("[%s/%s] err %s" % (rem, proto, str(e)[:70]), flush=True)
    p.terminate()
    try:
        p.wait(2)
    except Exception:
        p.kill()
    time.sleep(1)
