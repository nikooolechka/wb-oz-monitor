"""Поднимает xray socks5-прокси (127.0.0.1:10808) через подписку Happ с FAILOVER.

Тянет список серверов из подписки (обязателен заголовок X-Hwid, иначе заглушки),
перебирает trojan-серверы и оставляет запущенным первый рабочий. Так падение
отдельных серверов (а они мрут раз в пару дней) не ломает автомат — берётся
следующий живой; список сам обновляется провайдером.

Нужен бинарь ./xray рядом. Секреты: HAPP_SUB_URL, HAPP_HWID.
Exit 0 если прокси поднят (xray остаётся жить, detached), иначе 1.
"""
import os
import ssl
import sys
import json
import time
import base64
import subprocess
import urllib.request

SUB = os.environ.get("HAPP_SUB_URL", "").strip()
HWID = os.environ.get("HAPP_HWID", "").strip()
PORT = 10808
MAX_TRY = 15   # ограничиваем время: обычно рабочий находится в первых серверах


def _ctx():
    return ssl._create_unverified_context()


def fetch_servers():
    req = urllib.request.Request(SUB, headers={"User-Agent": "Happ/4.11.0", "X-Hwid": HWID})
    with urllib.request.urlopen(req, timeout=40, context=_ctx()) as r:
        raw = r.read().decode("utf-8", "ignore")
    txt = raw
    try:
        cand = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
        json.loads(cand)
        txt = cand
    except Exception:
        pass
    arr = json.loads(txt)
    servers = []
    for el in arr:
        if not isinstance(el, dict):
            continue
        for o in el.get("outbounds", []):
            if o.get("protocol") == "trojan":
                srv = (o.get("settings", {}).get("servers") or [{}])[0]
                addr = srv.get("address")
                if addr and addr != "0.0.0.0":
                    servers.append(o)
                break
    return servers


def _test():
    try:
        from curl_cffi import requests as cr
        s = cr.Session(impersonate="chrome")
        s.proxies = {"http": "socks5://127.0.0.1:%d" % PORT, "https": "socks5://127.0.0.1:%d" % PORT}
        r = s.get("https://api.ipify.org", timeout=15)
        if r.status_code == 200 and r.text.strip():
            return r.text.strip()
    except Exception:
        return None
    return None


def main():
    if not SUB or not HWID:
        print("[proxy] нет HAPP_SUB_URL/HAPP_HWID", flush=True)
        return 1
    try:
        servers = fetch_servers()
    except Exception as e:
        print("[proxy] подписка недоступна: %s" % str(e)[:120], flush=True)
        return 1
    print("[proxy] trojan-серверов в подписке: %d" % len(servers), flush=True)
    for ob in servers[:MAX_TRY]:
        addr = (ob.get("settings", {}).get("servers") or [{}])[0].get("address")
        cfg = {"log": {"loglevel": "warning"},
               "inbounds": [{"tag": "socks", "port": PORT, "listen": "127.0.0.1",
                             "protocol": "socks", "settings": {"udp": False}}],
               "outbounds": [dict(ob, tag="proxy"), {"protocol": "freedom", "tag": "direct"}]}
        with open("xray_config.json", "w") as f:
            json.dump(cfg, f)
        p = subprocess.Popen(["./xray", "run", "-c", "xray_config.json"],
                             stdout=open("xray.log", "a"), stderr=subprocess.STDOUT,
                             start_new_session=True)
        time.sleep(5)
        ip = _test()
        if ip:
            print("[proxy] поднят через %s → IP %s" % (addr, ip), flush=True)
            return 0
        try:
            p.terminate(); p.wait(3)
        except Exception:
            p.kill()
        time.sleep(1)
    print("[proxy] ни один сервер не поднялся", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
