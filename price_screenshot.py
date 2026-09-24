#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Скрин цен в ЛИЧНЫЙ Telegram владельца — ТОЛЬКО при свежих ценах прогона.

Логика (правило владельца 2026-09-08):
- Прогон цен на ПК заполняет Лист1 «Цены АС Фарм» в 10:00 и 17:00 МСК и штампует
  свежесть в примечания B1(ВБ)/H1(Озон)/O1(ЯМ).
- Этот автомат идёт в 10:30 и 17:30. Если ВБ/Озон/ЯМ РЕАЛЬНО свежие за это окно
  (штамп = сегодня и время попадает в окно) → шлёт ДВА скрина ОДНИМ альбомом
  в личку (chat_id владельца), подпись «цены на ДД.ММ от ЧЧ:ММ».
- Если цены НЕ свежие → НИЧЕГО не шлёт (в личку скринов нет). Про сбой цен
  сообщает в канал «изменения» отдельный сторож price_watchdog — здесь не дублируем.

Скрин = PDF-экспорт диапазона Лист1 → PNG (реальное форматирование), автообрезка
сносок-примечаний. Всё из облака, без ПК/браузера.

Нижняя граница диапазона — ДИНАМИЧЕСКАЯ (последняя заполненная строка блока), чтобы
новые артикулы (их периодически добавляют в таблицу) сами попадали в скрин и не
обрезались. См. block_last_row().

Куда шлём: ЧАТ ОТДЕЛА (с коллегами) — НЕ личка и НЕ канал «изменения» (три разных
места, не путать!). chat_id чата отдела = секрет PRICES_SCREENSHOT_CHAT_ID; пока
он не задан — НЕ шлём никуда. Бот @asfarm_changes_bot должен быть участником чата.
Будни (пн–пт): 10:30 и 17:30, СО звуком. Выходные (сб/вс): только 10:30 и БЕЗ звука
(disable_notification только в выходные).

Секреты/env: PRICES_SHEET_ID, GSHEETS_SA_JSON, TELEGRAM_BOT_TOKEN,
PRICES_SCREENSHOT_CHAT_ID (chat_id чата отдела). DRY=1 — проверить и собрать, НЕ слать.
"""
import os, io, re, json, ssl, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

import fitz
from PIL import Image
import requests
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

MSK = timezone(timedelta(hours=3))
SHEET = os.environ["PRICES_SHEET_ID"]
SA = json.loads(os.environ["GSHEETS_SA_JSON"])
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("PRICES_SCREENSHOT_CHAT_ID", "").strip()  # чат ОТДЕЛА; пусто → не шлём
DRY = os.environ.get("DRY") == "1"

# маркеты, чью свежесть ТРЕБУЕМ для отправки (основной прогон ПК): столбец-заголовок 0-based
REQUIRE = [("ВБ", 1), ("Озон", 7), ("ЯМ", 14), ("ДМ", 20)]
GID = 0
# два скрина: (левый столбец блока, правый столбец блока, столбцы-артикулы для поиска
# последней строки). Нижняя граница считается динамически в main() — новые артикулы
# подхватываются сами. HDR = минимальная строка (шапка + пара товаров), чтобы даже при
# пустом блоке скрин не выродился.
SCREEN_BLOCKS = [
    {"left": "B", "right": "M", "art_cols": ["B", "H"]},  # ВБ + Озон
    {"left": "O", "right": "Y", "art_cols": ["O", "U"]},  # ЯМ + Дет Мир (Y = СПП ДМ)
]
MIN_LAST = 6
STATE_FILE = "data/price_screenshot_state.json"


def _load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st):
    os.makedirs("data", exist_ok=True)
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def _creds(scopes):
    return Credentials.from_service_account_info(SA, scopes=scopes)


def block_last_row(svc, art_cols, floor=MIN_LAST, cap=300):
    """Последняя заполненная строка блока — максимум по столбцам-артикулам.
    Так новые товары, добавленные в конец блока, сами входят в скрин."""
    ranges = [f"Лист1!{c}2:{c}{cap}" for c in art_cols]
    res = svc.spreadsheets().values().batchGet(spreadsheetId=SHEET, ranges=ranges).execute()
    last = floor
    for vr in res.get("valueRanges", []):
        for i, row in enumerate(vr.get("values", [])):
            if row and str(row[0]).strip():
                last = max(last, 2 + i)
    return last


def notes_row1():
    svc = build("sheets", "v4", credentials=_creds(
        ["https://www.googleapis.com/auth/spreadsheets.readonly"]), cache_discovery=False)
    res = svc.spreadsheets().get(spreadsheetId=SHEET, ranges=["Лист1!A1:U1"],
                                 fields="sheets.data.rowData.values.note").execute()
    out = {}
    try:
        for i, c in enumerate(res["sheets"][0]["data"][0]["rowData"][0]["values"]):
            if c.get("note"):
                out[i] = c["note"]
    except Exception:
        pass
    return out


def fresh_for_window(note, today, phase):
    """True, если штамп = сегодня и попадает в текущее окно (утро <14ч / вечер >=14ч)."""
    if not note:
        return False, "нет штампа"
    if "LOGOUT" in note and today in note:
        return False, "разлогин"
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2})", note)
    if not m:
        return False, "не распарсил"
    d, hh = m.group(1), int(m.group(2))
    if d != today:
        return False, f"старый ({d})"
    if phase == "evening" and hh < 14:
        return False, f"утренний штамп ({hh}:xx), вечернего прогона нет"
    if phase == "morning" and hh >= 14:
        return False, f"дневной штамп ({hh}:xx), не утро"
    return True, f"{d} {hh:02d}:{m.group(3)}"


def export_png(rng, token):
    params = {"format": "pdf", "gid": str(GID), "range": rng,
              "portrait": "false", "fitw": "true", "gridlines": "true",
              "printtitle": "false", "sheetnames": "false", "pagenumbers": "false",
              "fzr": "false", "top_margin": "0.15", "bottom_margin": "0.15",
              "left_margin": "0.15", "right_margin": "0.15", "scale": "2"}
    url = f"https://docs.google.com/spreadsheets/d/{SHEET}/export?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    pdf = urllib.request.urlopen(req, timeout=90, context=ssl._create_unverified_context()).read()
    doc = fitz.open(stream=pdf, filetype="pdf")
    imgs = [Image.open(io.BytesIO(p.get_pixmap(matrix=fitz.Matrix(3, 3)).tobytes("png"))).convert("RGB")
            for p in doc]
    w = max(i.width for i in imgs); h = sum(i.height for i in imgs)
    canvas = Image.new("RGB", (w, h), "white"); yy = 0
    for im in imgs:
        canvas.paste(im, (0, yy)); yy += im.height
    # автообрезка: первый крупный пустой горизонтальный промежуток = конец таблицы (ниже — сноски)
    px = canvas.load(); step = max(1, w // 200); cut = canvas.height; run = 0
    for y in range(120, canvas.height):
        white = all(px[x, y][0] >= 240 and px[x, y][1] >= 240 and px[x, y][2] >= 240
                    for x in range(0, w, step))
        run = run + 1 if white else 0
        if run >= 60:
            cut = y - run + 1; break
    cropped = canvas.crop((0, 0, w, cut))
    bbox = Image.eval(cropped, lambda p: 255 - p).getbbox()
    if bbox:
        cropped = cropped.crop(bbox)
    b = io.BytesIO(); cropped.save(b, "PNG"); b.seek(0)
    return b


def main():
    now = datetime.now(MSK)
    today = now.strftime("%Y-%m-%d")
    phase = "morning" if now.hour < 14 else "evening"
    # ЖЁСТКОЕ окно отправки: утро 09–13 (штатный 10:30 + резерв 11:00), вечер 16–20
    # (17:30 + резерв 18:00). Воркфлоу дёргается и в 06:30 (Яндекс-таймер сбора), и в
    # кривое время (GitHub-крон срабатывает не в срок: 15:41, 21:14…). Вне окна НЕ шлём —
    # тогда скрин уходит только около 10:30 и 17:30, чем бы прогон ни запустился.
    SEND_WINDOW = {"morning": range(9, 12), "evening": range(16, 19)}  # утро 9–11, вечер 16–18
    if now.hour not in SEND_WINDOW[phase]:
        print(f"[screenshot] {now:%H:%M} вне окна отправки {phase} — пропуск (шлём ~10:30 и ~17:30)", flush=True)
        return
    # выходные (сб=5, вс=6): вечерний скрин НЕ шлём, только утренний 10:30
    if phase == "evening" and now.weekday() >= 5:
        print("[screenshot] выходной — вечерний скрин не шлём (только 10:30)", flush=True)
        return
    if not DRY and not CHAT_ID:
        print("[screenshot] PRICES_SCREENSHOT_CHAT_ID не задан (чат отдела) — никуда не шлю", flush=True)
        return
    # дедуп: один альбом на (день, окно) — чтобы ручной запуск и крон не прислали дважды
    st = _load_state()
    if not DRY and phase in st.get(today, []):
        print(f"[screenshot] за окно {phase} {today} уже отправлено — пропуск (без дубля)", flush=True)
        return
    notes = notes_row1()

    stale = []
    for name, col in REQUIRE:
        ok, why = fresh_for_window(notes.get(col, ""), today, phase)
        print(f"[screenshot] {name}: fresh={ok} ({why})", flush=True)
        if not ok:
            stale.append(name)

    if stale:
        print(f"[screenshot] цены не свежие ({', '.join(stale)}) — скрины в личку НЕ шлю "
              f"(про сбой сообщит сторож в канал)", flush=True)
        return

    # свежо — собираем скрины и шлём альбомом
    creds = _creds(["https://www.googleapis.com/auth/drive.readonly",
                    "https://www.googleapis.com/auth/spreadsheets.readonly"])
    creds.refresh(Request())
    # диапазоны считаем динамически: нижняя граница = последняя заполненная строка блока
    svc_v = build("sheets", "v4", credentials=creds, cache_discovery=False)
    screens = []
    for blk in SCREEN_BLOCKS:
        last = block_last_row(svc_v, blk["art_cols"])
        screens.append(f"{blk['left']}1:{blk['right']}{last}")
    print(f"[screenshot] диапазоны: {screens}", flush=True)
    photos = [export_png(r, creds.token) for r in screens]
    cap = f"цены на {now:%d.%m} от {now:%H:%M}"
    print(f"[screenshot] цены свежие — шлю альбом: «{cap}»", flush=True)
    if DRY:
        print("[screenshot] DRY=1 — не отправляю, скрины собраны ок")
        return
    files = {f"p{i}": p for i, p in enumerate(photos)}
    media = [{"type": "photo", "media": f"attach://p{i}", **({"caption": cap} if i == 0 else {})}
             for i in range(len(photos))]
    silent = "true" if now.weekday() >= 5 else "false"   # без звука ТОЛЬКО в выходные; будни — со звуком
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMediaGroup",
                      data={"chat_id": CHAT_ID, "media": json.dumps(media, ensure_ascii=False),
                            "disable_notification": silent},
                      files=files, timeout=120)
    d = r.json()
    print("[screenshot] отправлено:", d.get("ok"), "" if d.get("ok") else d)
    if d.get("ok"):
        st.setdefault(today, [])
        if phase not in st[today]:
            st[today].append(phase)
        # чистим старые даты, храним последние ~7
        for k in sorted(st.keys())[:-7]:
            st.pop(k, None)
        _save_state(st)


if __name__ == "__main__":
    main()
