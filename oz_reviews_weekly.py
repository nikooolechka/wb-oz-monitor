#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Еженедельная сводка отзывов Ozon в канал (понедельник, то же время что WB).
Считает ПРОШЛУЮ неделю (пн–вс) из архива reviews_ozon (сбор — на удалённом ПК,
curl_cffi). Формат/причины/дедуп — как у WB. DRY=1 — считает, в канал НЕ шлёт."""
import os, json
from datetime import datetime, timedelta, timezone
import notify
from wb_reviews_weekly import cluster_reasons  # переиспользуем категоризацию причин
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEET = "1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"
OZ_TAB = "reviews_ozon"
DRY = os.environ.get("DRY") == "1"


def read_archive():
    sa = json.loads(os.environ["GSHEETS_SA_JSON"])
    cred = Credentials.from_service_account_info(sa, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = build("sheets", "v4", credentials=cred, cache_discovery=False)
    rows = svc.spreadsheets().values().get(spreadsheetId=SHEET, range=f"{OZ_TAB}!A2:I100000").execute().get("values", [])
    out = []
    for r in rows:
        r = list(r) + [""] * (9 - len(r))
        try:
            score = int(str(r[4]).strip()) if str(r[4]).strip() else None
        except ValueError:
            score = None
        out.append({"article": r[2], "date": r[3], "score": score,
                    "pros": r[5], "cons": r[6], "text": r[7]})
    return out


def main():
    today = datetime.now(timezone.utc).date()
    mon_this = today - timedelta(days=today.weekday())
    lo, hi = mon_this - timedelta(days=7), mon_this - timedelta(days=1)
    los, his = lo.isoformat(), hi.isoformat()

    reviews = read_archive()
    wk = [r for r in reviews if r["date"] and los <= r["date"] <= his]
    st = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
    for r in wk:
        if r["score"] in st:
            st[r["score"]] += 1
    reasons = cluster_reasons([r for r in wk if (r["score"] or 5) <= 3])

    lines = ["📊 <b>Отзывы Ozon за неделю</b>",
             f"{lo.strftime('%d.%m')} – {hi.strftime('%d.%m')} было <b>{len(wk)} отзывов!</b>", "",
             f"⭐️ 5 звёзд — <b>{st[5]}</b>",
             f"⭐️ 4 звезды — <b>{st[4]}</b>",
             f"⭐️ 3 звезды — <b>{st[3]}</b>",
             f"⭐️ 2 звезды — <b>{st[2]}</b>",
             f"⭐️ 1 звезда — <b>{st[1]}</b>"]
    if reasons:
        lines += ["", "⚠️ <b>Самые частые причины негатива:</b>"] + reasons
    lines += ["", "Человек, обрати внимание😏"]
    msg = "\n".join(lines)
    print("--- СВОДКА OZON ---\n" + msg, flush=True)

    week_key = mon_this.isoformat()
    state_path = "data/oz_reviews_weekly_state.json"
    try:
        with open(state_path, encoding="utf-8") as f:
            already = json.load(f).get("last_sent_week") == week_key
    except (FileNotFoundError, json.JSONDecodeError):
        already = False
    if DRY:
        print("DRY=1 — в канал НЕ отправлено")
    elif already:
        print(f"сводка за неделю {week_key} уже отправлена — пропуск (дубля не будет)")
    else:
        notify.send(msg)
        print("отправлено в канал")
        os.makedirs("data", exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"last_sent_week": week_key}, f)


if __name__ == "__main__":
    main()
