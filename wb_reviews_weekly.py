#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Еженедельная сводка отзывов WB в канал + пополнение архива.
Понедельник 15:00 МСК: считает прошлую неделю (пн–вс), шлёт сводку в «АС Фарм изменения»,
и дописывает новые отзывы в архив-таблицу (дедуп по id, без повторов).
DRY=1 — всё считает и печатает, но в канал НЕ шлёт (для теста)."""
import os, json, ssl, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import notify

SHEET = "1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"
WB_TAB = "reviews_wb"
CTX = ssl._create_unverified_context()
DRY = os.environ.get("DRY") == "1"


def wb_fetch(days=14):
    token = os.environ["WB_TOKEN"].strip()
    dfrom = int(time.time()) - days * 86400
    base = "https://feedbacks-api.wildberries.ru/api/v1/feedbacks"
    res = {}
    for ans in ("false", "true"):
        d = {}
        for t in range(5):
            try:
                u = base + "?" + urllib.parse.urlencode(
                    {"isAnswered": ans, "take": 5000, "skip": 0, "order": "dateDesc", "dateFrom": dfrom})
                req = urllib.request.Request(u, headers={"Authorization": token})
                with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
                    d = json.loads(r.read().decode()); break
            except urllib.error.HTTPError as e:
                if e.code == 429 and t < 4:
                    w = int(e.headers.get("X-Ratelimit-Retry", "60")) + 3
                    print("WB 429 ->", w, flush=True); time.sleep(w); continue
                raise
        for f in (d.get("data") or {}).get("feedbacks") or []:
            pd = f.get("productDetails") or {}
            res["wb_" + str(f.get("id"))] = {
                "id": "wb_" + str(f.get("id")),
                "article": pd.get("supplierArticle") or str(pd.get("nmId")),
                "date": (f.get("createdDate") or "")[:10], "score": f.get("productValuation"),
                "pros": (f.get("pros") or "").strip(), "cons": (f.get("cons") or "").strip(),
                "text": (f.get("text") or "").strip()}
        time.sleep(1.5)
    return list(res.values())


def archive_append(rows):
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        sa = json.loads(os.environ["GSHEETS_SA_JSON"])
        gc = gspread.authorize(Credentials.from_service_account_info(
            sa, scopes=["https://www.googleapis.com/auth/spreadsheets"]))
        ws = gc.open_by_key(SHEET).worksheet(WB_TAB)
        existing = set(ws.col_values(1))
        new = [r for r in rows if r["id"] not in existing]
        if new:
            vals = [[r["id"], "WB", r["article"], r["date"], r["score"], r["pros"], r["cons"], r["text"], ""]
                    for r in sorted(new, key=lambda x: x["date"])]
            ws.append_rows(vals, value_input_option="RAW")
        return len(new)
    except Exception as e:
        print("архив: ошибка", str(e)[:150]); return -1


import re
# категории причин негатива → чистая короткая формулировка (детерминированно, без LLM)
_CATS = [
    (re.compile(r"развод|не очищ|не чист|размаз|мут[ьи]|пелен|плёнк|пленк|не отт[ие]р"), "разводы, плохо очищает"),
    (re.compile(r"запах|вонюч|вон[яеё]|отдушк|химоз|аромат.*непри"), "резкий запах"),
    (re.compile(r"нет.*эффект|без эффект|ноль эффект|никак.*эффект|не работа|прост.*вод|сладк.*вод|ни о ч[её]м|бестолк|беспол"), "нет эффекта"),
    (re.compile(r"аллерг|покрасн|красн.*пятн|пятн.*рот|ожог|раздраж|сыпь|реакц"), "покраснение/аллергия у ребёнка"),
    (re.compile(r"плесен|плеснев|гнил|протух|тухл"), "плесень"),
    (re.compile(r"горьк|приторн|тошнот|рвот|невкусн|мятн.*непри|вкус.*ужас|ужас.*вкус"), "плохой вкус"),
    (re.compile(r"размер|больш|огромн|маленьк|резать|мелк"), "неудобный размер"),
    (re.compile(r"упаковк|вскрыт|грязн|промок|прокол|недовлож|порош|мят[аы]|повреж"), "брак упаковки"),
    (re.compile(r"срок годн|просроч|перебит.*дат|дата.*произв"), "вопрос к сроку годности"),
]


def cluster_reasons(negs):
    if not negs:
        return []
    by = defaultdict(list)
    for f in negs:
        by[f["article"]].append(f)
    out = []
    for art, items in sorted(by.items(), key=lambda x: -len(x[1])):
        cats = defaultdict(int)
        for i in items:
            t = (i["pros"] + " " + i["cons"] + " " + i["text"]).lower()
            for rx, label in _CATS:
                if rx.search(t):
                    cats[label] += 1
        if cats:
            top = [lbl for lbl, _ in sorted(cats.items(), key=lambda x: -x[1])[:2]]
            reason = " + ".join(top)
        else:
            # ни одна категория — берём короткий сниппет по границе слова
            snip = next((i["cons"] or i["text"] or i["pros"] for i in items if (i["cons"] or i["text"] or i["pros"])), "")
            snip = " ".join(snip.replace("\n", " ").split())
            if len(snip) > 55:
                snip = snip[:55].rsplit(" ", 1)[0] + "…"
            reason = snip or "негатив"
        out.append(f"• {reason} — <b>{art}</b> ({len(items)})")
    return out[:5]


def main():
    today = datetime.now(timezone.utc).date()
    mon_this = today - timedelta(days=today.weekday())
    lo, hi = mon_this - timedelta(days=7), mon_this - timedelta(days=1)
    los, his = lo.isoformat(), hi.isoformat()

    reviews = wb_fetch(14)
    added = archive_append(reviews)
    print(f"собрано {len(reviews)}, в архив добавлено {added}", flush=True)

    wk = [r for r in reviews if los <= r["date"] <= his]
    st = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
    for r in wk:
        if r["score"] in st:
            st[r["score"]] += 1
    reasons = cluster_reasons([r for r in wk if (r["score"] or 5) <= 3])

    lines = ["📊 <b>Отзывы WB за неделю</b>",
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
    print("--- СВОДКА ---\n" + msg, flush=True)
    if DRY:
        print("DRY=1 — в канал НЕ отправлено")
    else:
        notify.send(msg); print("отправлено в канал")


if __name__ == "__main__":
    main()
