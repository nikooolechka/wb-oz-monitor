# -*- coding: utf-8 -*-
# Облачный сбор цен Ozon «с картой» из репрайсера Huckster (без ПК) -> вкладка "oz_цены"
# таблицы «АС ФАРМ клод код». История за 2 недели (строки старше удаляются).
# Запуск: GitHub Actions по часу (Ozon в Huckster обновляется раз в час).
# Env: HUCKSTER_USER, HUCKSTER_PASS, GSHEETS_SA_JSON (или GSHEETS_SA_FILE для локали).
import os, re, json, ssl, datetime, urllib.request, urllib.parse
from google.oauth2 import service_account
import google.auth.transport.requests as gtr

CTX = ssl._create_unverified_context()
HBASE = "https://wbs.e-teleport.ru"
HUSER = os.environ.get("HUCKSTER_USER", "andrianov-vladimir@yandex.ru")
HPASS = os.environ.get("HUCKSTER_PASS", "Dkflbvbh 555556")
OZ_SHOP = "189077_FBO"
SHEET = "1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"  # АС ФАРМ клод код
TAB = "oz_цены"
KEEP_DAYS = 14

def msk():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)

# ---- Huckster ----
def _hpost(path, body):
    r = urllib.request.Request(HBASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0"}, method="POST")
    resp = urllib.request.urlopen(r, context=CTX, timeout=30)
    return resp

def huckster_oz_cards():
    h = _hpost("/md5", {"input": HPASS}).read().decode()
    resp = _hpost("/auth/credentials", {"userName": HUSER, "password": h})
    sc = resp.getheader("Set-Cookie")
    sid = re.search(r"ss-id=([^;]+)", sc).group(1)
    pid = re.search(r"ss-pid=([^;]+)", sc).group(1)
    r = urllib.request.Request(HBASE + "/markets/integrations/repricer/items/list",
        data=json.dumps({"marketplace": "ozon", "shop_id": OZ_SHOP, "limit": 200, "offset": 0}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0",
                 "Cookie": f"ss-id={sid}; ss-pid={pid}"}, method="POST")
    items = json.loads(urllib.request.urlopen(r, context=CTX, timeout=30).read()).get("result", [])
    out = {}
    for it in items:
        cp = it.get("market_card_price")
        if it.get("parsing_status") == "Работает" and cp:   # активные с ценой
            out[it["uid"]] = int(round(cp))
    return out

# ---- Google Sheets ----
def _tok():
    env = os.environ.get("GSHEETS_SA_JSON", "").strip()
    if env.startswith("{"):
        cr = service_account.Credentials.from_service_account_info(json.loads(env), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    else:
        cr = service_account.Credentials.from_service_account_file(
            os.environ.get("GSHEETS_SA_FILE", os.path.expanduser("~/Desktop/files/claude-sheets-497816-f533a416fa81.json")),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
    cr.refresh(gtr.Request()); return cr.token

def gapi(method, url, body=None):
    r = urllib.request.Request(url, data=(json.dumps(body).encode() if body is not None else None), method=method,
        headers={"Authorization": "Bearer " + _tok(), "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, context=CTX, timeout=60).read())

def get_sheet_id():
    meta = gapi("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}?fields=sheets(properties(sheetId,title))")
    for s in meta["sheets"]:
        if s["properties"]["title"] == TAB:
            return s["properties"]["sheetId"]
    # создаём вкладку
    res = gapi("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}:batchUpdate",
        {"requests": [{"addSheet": {"properties": {"title": TAB}}}]})
    return res["replies"][0]["addSheet"]["properties"]["sheetId"]

def read_vals(rng):
    d = gapi("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/{urllib.parse.quote(rng)}")
    return d.get("values", [])

def main():
    cards = huckster_oz_cards()
    if not cards:
        print("[WARN] Huckster не отдал цены Ozon — выхожу без записи"); return
    sid = get_sheet_id()
    rows = read_vals(f"{TAB}!A1:ZZ10000")
    header = rows[0] if rows else []
    if not header:
        header = ["дата-время"] + sorted(cards.keys())
        gapi("PUT", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/{urllib.parse.quote(TAB+'!A1')}?valueInputOption=RAW",
             {"values": [header]})
        rows = [header]
    # авто-добавление новых товаров как колонок
    new_cols = [t for t in sorted(cards) if t not in header]
    if new_cols:
        header = header + new_cols
        gapi("PUT", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/{urllib.parse.quote(TAB+'!A1')}?valueInputOption=RAW",
             {"values": [header]})
        print("[INFO] новые товары-колонки:", new_cols)
    # строка-снимок в порядке header
    ts = msk().strftime("%Y-%m-%d %H:%M")
    row = [ts] + [cards.get(h, "") for h in header[1:]]
    gapi("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/{urllib.parse.quote(TAB+'!A1')}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
         {"values": [row]})
    print(f"[OK] записан снимок {ts}: товаров {len(cards)}")
    # удаление строк старше KEEP_DAYS (самые старые — сверху, после заголовка)
    cutoff = (msk() - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d %H:%M")
    rows = read_vals(f"{TAB}!A1:A10000")
    old = 0
    for r in rows[1:]:
        if r and r[0] and r[0] < cutoff:
            old += 1
        else:
            break
    if old:
        gapi("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}:batchUpdate",
            {"requests": [{"deleteDimension": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 1, "endIndex": 1 + old}}}]})
        print(f"[OK] удалено старых строк (>{KEEP_DAYS} дн): {old}")

if __name__ == "__main__":
    main()
