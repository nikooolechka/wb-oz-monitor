#!/usr/bin/env python3
# Обновлятор баз знаний Dify СВЕЖИМИ данными компании (слой 2): раз в час читает таблицы
# (цены, остатки, матрица) и ПЕРЕЗАЛИВАЕТ документы «🔄 LIVE …» в базы WB/Ozon, заменяя прошлые.
# Так бот отвечает всегда по свежим данным, а не по разовому снимку.
import os, sys, json, time, urllib.request, ssl
ssl._create_default_https_context = ssl._create_unverified_context
from google.oauth2 import service_account
from googleapiclient.discovery import build

DIFY = os.environ.get("DIFY_BASE", "http://82.148.225.41")
DKEY = os.environ["KB_DATASET_KEY"]  # ТОЛЬКО из секрета — репо публичный, ключ в код НЕ класть
WB_DS = "36022934-9298-41a5-afd1-6c5212724d63"
OZ_DS = "1beefd57-68ac-4d5c-af47-79cb71f3f8c9"

def _sa():
    j = os.environ.get("GSHEETS_SA_JSON")
    if j: return service_account.Credentials.from_service_account_info(json.loads(j), scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    return service_account.Credentials.from_service_account_file(os.path.expanduser('~/Desktop/files/claude-sheets-497816-f533a416fa81.json'), scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
SV = build('sheets', 'v4', credentials=_sa(), cache_discovery=False)

def rng(sid, a1):
    try: return SV.spreadsheets().values().get(spreadsheetId=sid, range=a1).execute().get('values', [])
    except Exception as e: print("read err", a1, str(e)[:100]); return []

def _dify(method, path, body=None, timeout=60):
    req = urllib.request.Request(f"{DIFY}{path}", data=(json.dumps(body).encode() if body is not None else None),
        headers={"Authorization": f"Bearer {DKEY}", "Content-Type": "application/json"}, method=method)
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def replace_doc(ds_id, name, text):
    # удалить прошлые версии этого LIVE-дока по имени, создать свежую
    try:
        docs = _dify("GET", f"/v1/datasets/{ds_id}/documents?page=1&limit=100").get("data", [])
        for d in docs:
            if d.get("name") == name:
                try: _dify("DELETE", f"/v1/datasets/{ds_id}/documents/{d['id']}")
                except Exception as e: print("del err", str(e)[:80])
    except Exception as e: print("list err", str(e)[:80])
    try:
        _dify("POST", f"/v1/datasets/{ds_id}/document/create-by-text",
              {"name": name, "text": text, "indexing_technique": "high_quality", "process_rule": {"mode": "automatic"}})
        print("  ✓", name, "->", ds_id[:8], f"({len(text)} симв)")
    except Exception as e: print("  ✗ upload", name, str(e)[:120])

# ---------------- источники ----------------
PRICES = "1fIXDZIwbRwPXUNoqT3sGoYxfmLl4fbN7TJ5YVt4_DxM"
PULT = "1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"
OP = "1sHlFGSVB-7V8V4q6kvcTR1rrw19EaabIaOgrCHU0DHE"

def doc_prices():
    v = rng(PRICES, "Лист1!A1:R45")
    def block(rows, cols, title):
        out = [f"# {title} (витринные цены, обновлено автоматически)"]
        for r in rows[2:]:
            def g(i): return (r[i].strip() if i < len(r) and r[i] else "")
            art = g(cols[0])
            if not art or "ВБ" in art or "Озон" in art or "ЯМ" in art: continue
            vals = [f"{h}: {g(c)}" for h, c in zip(cols[1], cols[2]) if g(c)]
            if vals: out.append(f"- {art} — " + "; ".join(vals))
        return "\n".join(out)
    wb = block(v, (1, ["до СПП", "с СПП", "с кошельком"], [2, 3, 4]), "Wildberries — текущие витринные цены")
    oz = block(v, (7, ["текущая", "с др. банками", "с картой Ozon"], [9, 10, 11]), "Ozon — текущие витринные цены")
    return wb, oz

def doc_stock():
    v = rng(PULT, "остатки!A1:G60")
    out = ["# Остатки товара на складе (Свободно, обновляется ежечасно)"]
    for r in v[1:]:
        def g(i): return (r[i].strip() if i < len(r) and r[i] else "")
        nom, art, sv = g(0), g(1), g(2)
        if not (nom or art) or "того" in nom or "пиндов" in nom: continue
        line = f"- {art or nom}: свободно {sv}"
        if g(5): line += f"; дата поступления {g(5)}"
        if g(6): line += f"; {g(6)}"
        out.append(line)
    return "\n".join(out)

def doc_matrix():
    v = rng(OP, "МАТРИЦА!A1:AF60")
    if not v: return ""
    hdr = v[1] if len(v) > 1 else v[0]
    def col(sub):
        for i, h in enumerate(hdr):
            if sub.lower() in (h or "").lower(): return i
        return -1
    cC = 2  # артикул в C
    cV = col("габарит"); cS = col("состав"); cT = col("свойств"); cU = col("применен")
    out = ["# Матрица товаров: габариты, состав, свойства, применение"]
    for r in v[2:]:
        def g(i): return (r[i].strip().replace("\n", " ") if 0 <= i < len(r) and r[i] else "")
        art = g(cC)
        if not art: continue
        parts = []
        if g(cV): parts.append(f"габариты {g(cV)}")
        if g(cS): parts.append(f"состав: {g(cS)[:300]}")
        if g(cT): parts.append(f"свойства: {g(cT)[:300]}")
        if g(cU): parts.append(f"применение: {g(cU)[:300]}")
        if parts: out.append(f"- {art.splitlines()[0]} — " + "; ".join(parts))
    return "\n".join(out)

if __name__ == "__main__":
    print("=== обновление баз свежими данными компании ===")
    wb_pr, oz_pr = doc_prices()
    stock = doc_stock()
    matrix = doc_matrix()
    # цены — в свою базу; остатки и матрица — в обе (товары общие, спросят под любым маркером)
    if wb_pr: replace_doc(WB_DS, "🔄 LIVE — цены WB", wb_pr)
    if oz_pr: replace_doc(OZ_DS, "🔄 LIVE — цены Ozon", oz_pr)
    for ds in (WB_DS, OZ_DS):
        if stock: replace_doc(ds, "🔄 LIVE — остатки на складе", stock)
        if matrix: replace_doc(ds, "🔄 LIVE — матрица (габариты/состав)", matrix)
    print("готово")
