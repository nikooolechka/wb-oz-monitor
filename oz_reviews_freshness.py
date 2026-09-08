#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сторож свежести отзывов Ozon. Сбор идёт на удалённом ПК (браузер) и может
молча встать (раздутый профиль Chrome, падение сессии ПК). Тогда еженедельная
сводка тихо показывает 0. Этот сторож читает архив reviews_ozon и, если самый
свежий отзыв старше порога, шлёт ОДИН алерт в канал. Кроном ежедневно.
Логика «свежести — по факту данных», не по «скрипт отработал»."""
import os, json
from datetime import datetime, timedelta, timezone
import notify
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEET = "1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"
OZ_TAB = "reviews_ozon"
STALE_DAYS = int(os.environ.get("OZ_REV_STALE_DAYS", "4"))  # молчание > N дней = тревога
STATE = "data/oz_reviews_freshness_state.json"
DRY = os.environ.get("DRY") == "1"


def latest_date():
    sa = json.loads(os.environ["GSHEETS_SA_JSON"])
    cred = Credentials.from_service_account_info(sa, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = build("sheets", "v4", credentials=cred, cache_discovery=False)
    rows = svc.spreadsheets().values().get(spreadsheetId=SHEET, range=f"{OZ_TAB}!D2:D100000").execute().get("values", [])
    dates = [r[0] for r in rows if r and r[0]]
    return max(dates) if dates else None


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(s):
    os.makedirs("data", exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f)


def main():
    today = datetime.now(timezone.utc).date()
    last = latest_date()
    if not last:
        print("архив пуст — тихо пропускаю (нет базы)")
        return
    last_d = datetime.strptime(last, "%Y-%m-%d").date()
    age = (today - last_d).days
    print(f"свежий отзыв Ozon: {last} (возраст {age} дн, порог {STALE_DAYS})", flush=True)

    st = load_state()
    if age > STALE_DAYS:
        # тревога, но не спамить: один алерт пока не починится (по дате last)
        if st.get("alerted_for") == last:
            print("уже алертил по этой дате — пропуск (дубля не будет)")
            return
        msg = (f"<b>Отзывы Ozon не собираются — молчат с {last_d.strftime('%d.%m')} ({age} дн).</b>\n"
               f"Сбор отзывов на удалённом ПК встал (обычно раздулся профиль Chrome или упала сессия ПК). "
               f"Николь, зайди — перезапущу сбор.")
        print("--- АЛЕРТ ---\n" + msg, flush=True)
        if DRY:
            print("DRY=1 — не отправлено")
        else:
            notify.send(msg)
            st["alerted_for"] = last
            save_state(st)
            print("алерт отправлен в канал")
    else:
        # свежо — снять флаг (чтобы при новой поломке снова алертнуло)
        if st.get("alerted_for"):
            st.pop("alerted_for", None)
            save_state(st)
        print("свежо — молчу")


if __name__ == "__main__":
    main()
