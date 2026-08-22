#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сторож свежести архива отзывов Ozon (reviews_ozon).
Отзывы Ozon собирает браузер на удалённом ПК (из облака антибот режет). Если сбор
там встал — архив перестаёт пополняться. Сторож ежедневно смотрит, растёт ли архив;
если N дней без роста — ОДИН раз шлёт алерт в канал «АС Фарм изменения».
DRY=1 — считает, в канал НЕ шлёт."""
import os, json
from datetime import datetime, timezone
import notify
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEET = "1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"
TAB = "reviews_ozon"
STALE_DAYS = 8           # столько дней без нового отзыва = вероятно, сбор на ПК встал
STATE_PATH = "data/oz_reviews_watchdog_state.json"
DRY = os.environ.get("DRY") == "1"


def archive_count():
    sa = json.loads(os.environ["GSHEETS_SA_JSON"])
    cred = Credentials.from_service_account_info(
        sa, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = build("sheets", "v4", credentials=cred, cache_discovery=False)
    col = svc.spreadsheets().values().get(
        spreadsheetId=SHEET, range=f"{TAB}!A2:A100000").execute().get("values", [])
    return sum(1 for r in col if r and str(r[0]).strip())


def load():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(d):
    os.makedirs("data", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def main():
    today = datetime.now(timezone.utc).date()
    cur = archive_count()
    st = load()
    last_count = st.get("last_count")
    last_growth = st.get("last_growth_date")
    alerted = st.get("alerted", False)
    print(f"архив reviews_ozon: строк={cur} | было={last_count} | последний рост={last_growth} | alerted={alerted}")

    # первый прогон — просто запоминаем базу, без алерта
    if last_count is None:
        save({"last_count": cur, "last_growth_date": today.isoformat(), "alerted": False})
        print("первичная инициализация сторожа — база записана, алерт не шлём")
        return

    if cur > last_count:            # архив вырос — сбор жив
        save({"last_count": cur, "last_growth_date": today.isoformat(), "alerted": False})
        print(f"архив вырос (+{cur - last_count}) — сбор жив, перевзвёлся")
        return

    # роста нет — считаем, сколько дней стоим
    try:
        stale = (today - datetime.strptime(last_growth, "%Y-%m-%d").date()).days
    except Exception:
        stale = 0
    print(f"архив не растёт {stale} дн (порог {STALE_DAYS})")

    if stale >= STALE_DAYS and not alerted:
        msg = (f"<b>Архив отзывов Озон не пополняется {stale} дней.</b>\n"
               f"Похоже, встал сбор отзывов Ozon на удалённом ПК (браузерный сборщик). "
               f"Николь, зайди проверь — иначе еженедельная сводка Озон опустеет.")
        if DRY:
            print("DRY=1 — в канал НЕ отправлено:\n" + msg)
        else:
            notify.send(msg)
            print("АЛЕРТ отправлен")
        st["alerted"] = True
    # сохраняем текущее состояние (last_count/last_growth не двигаем — роста не было)
    st.setdefault("last_count", last_count)
    st.setdefault("last_growth_date", last_growth)
    save(st)


if __name__ == "__main__":
    main()
