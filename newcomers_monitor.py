#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ЛОВЛЯ НОВИНОК (владелец 2026-11-10, «тонкое место»): новая номенклатура появляется в 1С
(остатки) ИЛИ в юнитках (очередь gabariti) — поймать и завести ПО ВСЕМ направлениям.
Этот монитор: детектит новую/непривязанную номенклатуру → алерт в канал с ЧЕКЛИСТОМ заведения.
Облачно (GitHub Actions), БЕЗ ПК — читает Google Sheets + шлёт Telegram. Не трогает пульт 1С.
Дедуп: один алерт на новинку (state в data/newcomers_state.json, коммитится обратно).
"""
import os, json, re, ssl, urllib.request
ssl._create_default_https_context = ssl._create_unverified_context
from google.oauth2 import service_account
from googleapiclient.discovery import build

PULT = "1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"           # клод-код, вкладка «остатки»
STATE_PATH = os.environ.get("NEWCOMERS_STATE", "data/newcomers_state.json")
GAB_QUEUE = os.environ.get("NEW_PRODUCTS_PATH", "data/gabariti_new_products.json")
TG_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
TG_CHAT = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()    # канал «АС Фарм изменения»
DRY = os.environ.get("DRY") == "1"

# Детект новинки — по ИМЕНИ номенклатуры (первый прогон запоминает все текущие → существующие
# НЕ алертят, даже редкие вроде экстрактов/спреев). Доп.сигнал — явно непривязанная строка
# (пустой артикул или «новая позиция», как их помечает ostatki_monitor). Хардкод-списка НЕТ
# (он бы фолс-позитивил на реальных товарах, которых в нём нет).

def _sa():
    j = os.environ.get("GSHEETS_SA_JSON")
    if j: return service_account.Credentials.from_service_account_info(json.loads(j), scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    return service_account.Credentials.from_service_account_file(os.path.expanduser('~/Desktop/files/claude-sheets-497816-f533a416fa81.json'), scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
SV = build('sheets', 'v4', credentials=_sa(), cache_discovery=False)

def load(path, default):
    try: return json.load(open(path, encoding="utf-8"))
    except Exception: return default
def save(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def tg(text):
    if DRY or not (TG_TOKEN and TG_CHAT):
        print("[DRY/no-tg]\n" + text); return
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, data=data, timeout=30)
    except Exception as e: print("tg err", str(e)[:120])

import urllib.parse

CHECKLIST = ("\nЗавести по направлениям:\n"
 "• остатки — правило распознавания в ostatki_monitor.article()\n"
 "• даты готовности — строка + потребность/неснижаемый\n"
 "• юнитки ВБ/Озон — себес, габариты, объём\n"
 "• матрица — габариты, состав (recipes_1c по 1С-коду), ABC\n"
 "• цены — SKU ВБ/Озон/ЯМ в wb_run/oz_run/ym_run/card_wb\n"
 "• склейки — в какую группу входит\n"
 "• базы знаний Dify — в карту товаров")

def main():
    st = load(STATE_PATH, {})
    known_names = set(st.get("known_names", []))
    alerted = set(st.get("alerted", []))
    first_run = not known_names

    # 1) номенклатура из 1С-остатков
    rows = SV.spreadsheets().values().get(spreadsheetId=PULT, range="остатки!A2:C200").execute().get("values", [])
    found = []  # (name, article, reason)
    for r in rows:
        name = (r[0].strip() if len(r) > 0 and r[0] else "")
        art  = (r[1].strip() if len(r) > 1 and r[1] else "")
        if not name: continue
        unmapped = (not art) or ("нов" in art.lower())   # явно непривязана (ostatki_monitor пометил)
        is_new_name = name not in known_names
        if is_new_name or unmapped:
            reason = []
            if is_new_name: reason.append("новое имя в 1С")
            if unmapped: reason.append("артикул не привязан")
            found.append((name, art, ", ".join(reason)))

    # 2) очередь новинок из юниток (gabariti)
    for item in load(GAB_QUEUE, []):
        nm = item.get("name") if isinstance(item, dict) else str(item)
        if nm: found.append((nm, item.get("platform","") if isinstance(item,dict) else "", "новый товар в юнитке ВБ/Озон"))

    # первый прогон — тихая база
    if first_run:
        for name, *_ in found: known_names.add(name)
        # плюс все текущие имена остатков в базу
        for r in rows:
            if r and r[0] and r[0].strip(): known_names.add(r[0].strip())
        save(STATE_PATH, {"known_names": sorted(known_names), "alerted": sorted(alerted)})
        print("первый прогон — база из %d имён, алертов нет" % len(known_names)); return

    # алерты по НОВЫМ (не алертованным)
    n = 0
    for name, art, reason in found:
        key = name.strip()
        if key in alerted: continue
        msg = ("<b>🆕 Новая позиция — поймал, нужно завести везде</b>\n"
               "«%s»%s\n<i>(%s)</i>\n%s" % (
               name, (" — артикул %s" % art) if art and "нов" not in art.lower() else "", reason, CHECKLIST))
        tg(msg); alerted.add(key); known_names.add(key); n += 1
    # все виденные имена — в базу (чтобы в след. раз не повторять по имени)
    for r in rows:
        if r and r[0] and r[0].strip(): known_names.add(r[0].strip())
    save(STATE_PATH, {"known_names": sorted(known_names), "alerted": sorted(alerted)})
    print("новинок-алертов: %d" % n)

if __name__ == "__main__":
    main()
