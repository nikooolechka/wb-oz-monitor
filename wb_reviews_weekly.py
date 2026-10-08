#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отзывы WB: ежедневный сбор в архив + понедельничная сводка в канал.

Как считаем (важно, 2026-10-08): отчёт за неделю строится ИЗ АРХИВА (вкладка
reviews_wb), а не из одного живого прогона. В архиве лежат ВСЕ отзывы, включая
БЕЗ ТЕКСТА (оценки-звёзды) — их в ~4 раза больше текстовых, и раньше они не
попадали в счёт, т.к. отчёт уходил с первого прогона до того, как архив добирал
их на поздних заходах. Теперь:
  • сбор в архив гоняется ЕЖЕДНЕВНО (мягко, на 429 не падаем) → к понедельнику
    прошлая неделя в архиве полная;
  • сводка (ТОЛЬКО по понедельникам) считает прошлую неделю ИЗ АРХИВА → полный счёт.
MODE=archive  — только собрать в архив (без отчёта), для ежедневных прогонов.
MODE=report   — собрать + в понедельник прислать сводку (дефолт).
DRY=1         — всё посчитать и показать в логе, но в канал НЕ слать."""
import os, json, ssl, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import notify

SHEET = "1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"
WB_TAB = "reviews_wb"
CTX = ssl._create_unverified_context()
DRY = os.environ.get("DRY") == "1"
MODE = (os.environ.get("MODE") or "report").strip().lower()
MSK = timezone(timedelta(hours=3))


def wb_fetch(days=14):
    """Тянет отзывы за N дней. На 429 — ОДИН повтор после паузы, потом мягко сдаётся
    (возвращает что успели), НЕ роняя прогон: архив доберёт в следующий заход."""
    token = os.environ["WB_TOKEN"].strip()
    dfrom = int(time.time()) - days * 86400
    base = "https://feedbacks-api.wildberries.ru/api/v1/feedbacks"
    res = {}
    for ans in ("false", "true"):
        d = {}
        for t in range(2):  # максимум 2 попытки (1 повтор) — не долбим WB
            try:
                u = base + "?" + urllib.parse.urlencode(
                    {"isAnswered": ans, "take": 5000, "skip": 0, "order": "dateDesc", "dateFrom": dfrom})
                req = urllib.request.Request(u, headers={"Authorization": token})
                with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
                    d = json.loads(r.read().decode()); break
            except urllib.error.HTTPError as e:
                if e.code == 429 and t == 0:
                    w = int(e.headers.get("X-Ratelimit-Retry", "65")) + 5
                    print(f"WB 429 ({ans}) -> ждём {w}с и ОДИН повтор", flush=True); time.sleep(w); continue
                print(f"WB {ans}: {e.code} — пропускаю бакет (доберём позже)", flush=True); break
        for f in (d.get("data") or {}).get("feedbacks") or []:
            pd = f.get("productDetails") or {}
            res["wb_" + str(f.get("id"))] = {
                "id": "wb_" + str(f.get("id")),
                "article": pd.get("supplierArticle") or str(pd.get("nmId")),
                "date": (f.get("createdDate") or "")[:10], "score": f.get("productValuation"),
                "pros": (f.get("pros") or "").strip(), "cons": (f.get("cons") or "").strip(),
                "text": (f.get("text") or "").strip()}
        time.sleep(20)  # пауза между бакетами — бережём лимит WB
    return list(res.values())


def _ws():
    import gspread
    from google.oauth2.service_account import Credentials
    sa = json.loads(os.environ["GSHEETS_SA_JSON"])
    gc = gspread.authorize(Credentials.from_service_account_info(
        sa, scopes=["https://www.googleapis.com/auth/spreadsheets"]))
    return gc.open_by_key(SHEET).worksheet(WB_TAB)


def archive_append(rows):
    try:
        ws = _ws()
        existing = set(ws.col_values(1))
        new = [r for r in rows if r["id"] not in existing]
        if new:
            vals = [[r["id"], "WB", r["article"], r["date"], r["score"], r["pros"], r["cons"], r["text"], ""]
                    for r in sorted(new, key=lambda x: x["date"])]
            ws.append_rows(vals, value_input_option="RAW")
        return len(new)
    except Exception as e:
        print("архив: ошибка", str(e)[:150]); return -1


def archive_week(los, his):
    """Читает ВСЕ отзывы недели [los..his] из архива (с текстом и без). Источник правды для счёта."""
    ws = _ws()
    out = []
    for r in ws.get_all_values()[1:]:
        r = r + [""] * (8 - len(r)) if len(r) < 8 else r
        d = (r[3] or "").strip()
        if los <= d <= his:
            try:
                sc = int(r[4])
            except (ValueError, TypeError):
                sc = None
            out.append({"article": (r[2] or "").strip(), "date": d, "score": sc,
                        "pros": (r[5] or "").strip(), "cons": (r[6] or "").strip(), "text": (r[7] or "").strip()})
    return out


import re
# короткие НЕЙТРАЛЬНЫЕ ярлыки причин негатива (описывают саму претензию, без привязки к товару).
# порядок = приоритет при равенстве частоты (специфичное выше общего).
_CATS = [
    (re.compile(r"плесен|плеснев|затхл|тух|гнил|прокис|порч|испорч|прогорк"), "затхлые / стухшие"),
    (re.compile(r"сух(ие|ая|ой|о)\b|пересох|высох"), "сухие салфетки"),
    (re.compile(r"аллерг|раздраж|покрасн|сыпь|\bзуд|ожог|жжени"), "аллергия / раздражение"),
    (re.compile(r"истёк срок|истек срок|срок годн|просроч|дата.*производ"), "истёк срок годности"),
    (re.compile(r"развод|не очищ|не отмы|размаз|плёнк|пленк"), "разводы, плохо очищает"),
    (re.compile(r"горьк|приторн|невкусн|тошнот|против.*вкус|вкус.*ужас"), "плохой вкус"),
    (re.compile(r"запах|воня|вонюч|химозн|химич"), "неприятный запах"),
    (re.compile(r"упаковк|вскрыт|повреж|порван|протек|пролит|недовлож|разлил"), "брак упаковки"),
    (re.compile(r"не помог|не работ|бесполезн|нет.*эффект|без эффект|ноль эффект|никак.*эффект"), "нет эффекта"),
]


def cluster_reasons(negs):
    if not negs:
        return []
    by = defaultdict(list)
    for f in negs:
        by[f["article"]].append(f)
    order = {lbl: i for i, (_, lbl) in enumerate(_CATS)}
    out = []
    for art, items in sorted(by.items(), key=lambda x: -len(x[1])):
        cats = defaultdict(int)
        for i in items:
            t = (i["pros"] + " " + i["cons"] + " " + i["text"]).lower()
            for rx, label in _CATS:
                if rx.search(t):
                    cats[label] += 1
        if cats:
            reason = sorted(cats.items(), key=lambda x: (-x[1], order[x[0]]))[0][0]
        else:
            reason = "негативный отзыв"
        out.append(f"• {reason} — <b>{art}</b> ({len(items)})")
    return out[:5]


def main():
    today = datetime.now(MSK).date()

    # 1) СБОР В АРХИВ (ежедневно, мягко). На сбое не падаем — архив доберёт в следующий заход.
    try:
        reviews = wb_fetch(14)
        added = archive_append(reviews)
        print(f"собрано {len(reviews)}, в архив добавлено {added}", flush=True)
    except Exception as e:
        print("сбор не удался (не критично для отчёта):", str(e)[:150], flush=True)

    # 2) ОТЧЁТ — только по понедельникам (или всегда при DRY для теста). Считаем ИЗ АРХИВА.
    is_monday = today.weekday() == 0
    if MODE == "archive" or (not is_monday and not DRY):
        print("режим архива / не понедельник — отчёт не формирую", flush=True)
        return

    mon_this = today - timedelta(days=today.weekday())
    lo, hi = mon_this - timedelta(days=7), mon_this - timedelta(days=1)
    los, his = lo.isoformat(), hi.isoformat()

    wk = archive_week(los, his)  # ВСЕ отзывы недели из архива: с текстом и без
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
    print("--- СВОДКА (из архива) ---\n" + msg, flush=True)

    # дедуп: одна сводка на неделю. Ключ недели = понедельник запуска.
    week_key = mon_this.isoformat()
    state_path = "data/reviews_weekly_state.json"
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
        notify.send(msg); print("отправлено в канал")
        os.makedirs("data", exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"last_sent_week": week_key}, f)


if __name__ == "__main__":
    main()
