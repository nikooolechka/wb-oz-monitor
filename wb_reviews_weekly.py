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


def snapshot_wb_ratings():
    """Снимок рейтинга WB ПО КАЖДОЙ СКЛЕЙКЕ через публичный виджет feedbacks.wb.ru:
    feedbackCount (ВСЕГО оценок, вкл. чистые звёзды) + valuationDistribution (по звёздам).
    Из облака (виджет отдаёт gzip, не гео-локан). Пишет в rating_snapshots. Дельта пн→пн = недельный счёт.
    ⚠️ НЕ /count — он считает архивные/удалённые (30475 vs реальных 22304 по карточкам). См. грабли в памяти."""
    import gzip
    try:
        token = os.environ["WB_TOKEN"].strip()
        def getj(url, hdr, data=None):
            r = urllib.request.Request(url, data=(json.dumps(data).encode() if data else None), headers=hdr)
            raw = urllib.request.urlopen(r, context=CTX, timeout=60).read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
        cl = getj("https://content-api.wildberries.ru/content/v2/get/cards/list",
                  {"Authorization": token, "Content-Type": "application/json"},
                  {"settings": {"cursor": {"limit": 100}, "filter": {"withPhoto": -1}}}).get("cards") or []
        byimt = {}
        for c in cl:
            byimt.setdefault(c.get("imtID"), []).append(c.get("vendorCode"))
        UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
              "Accept": "*/*", "Referer": "https://www.wildberries.ru/", "Accept-Encoding": "gzip"}
        today = datetime.now(MSK).date().isoformat()
        rows = []; tot = 0; dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}; txt = 0; n = 0
        for imt, vcs in byimt.items():
            if not imt:
                continue
            d = None
            for host in ("feedbacks2.wb.ru", "feedbacks1.wb.ru"):
                try:
                    d = getj(f"https://{host}/feedbacks/v2/{imt}", UA)
                    if d and d.get("feedbackCount") is not None:
                        break
                except Exception:
                    d = None
            if not d or d.get("feedbackCount") is None:
                continue
            fc = d.get("feedbackCount") or 0; vd = d.get("valuationDistribution") or {}
            nm = ",".join([v for v in vcs if v])[:60]
            rows.append([today, "WB", "card", str(imt), fc,
                         int(vd.get("5", 0)), int(vd.get("4", 0)), int(vd.get("3", 0)),
                         int(vd.get("2", 0)), int(vd.get("1", 0)), nm])
            tot += fc; txt += d.get("feedbackCountWithText") or 0
            for s in dist:
                dist[s] += int(vd.get(str(s), 0) or 0)
            n += 1; time.sleep(0.4)
        if not n:
            print("снимок WB: виджет не отдал ни одной карточки (гео-блок?)", flush=True); return
        rows.append([today, "WB", "global", "vse kartochki", tot,
                     dist[5], dist[4], dist[3], dist[2], dist[1], f"itog WB (vsego, s textom {txt})"])
        import gspread
        from google.oauth2.service_account import Credentials
        sa = json.loads(os.environ["GSHEETS_SA_JSON"])
        gc = gspread.authorize(Credentials.from_service_account_info(
            sa, scopes=["https://www.googleapis.com/auth/spreadsheets"]))
        gc.open_by_key(SHEET).worksheet("rating_snapshots").append_rows(rows, value_input_option="RAW")
        print(f"снимок WB рейтинга: склеек {n}, всего {tot}, с текстом {txt}", flush=True)
    except Exception as e:
        print("снимок WB рейтинга не удался (не критично):", str(e)[:150], flush=True)


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


def _snap_globals():
    """global-снимки рейтинга из rating_snapshots: {'WB':[{date,total,dist}...], 'OZON':[...]}, по дате."""
    import gspread
    from google.oauth2.service_account import Credentials
    sa = json.loads(os.environ["GSHEETS_SA_JSON"])
    gc = gspread.authorize(Credentials.from_service_account_info(
        sa, scopes=["https://www.googleapis.com/auth/spreadsheets"]))
    ws = gc.open_by_key(SHEET).worksheet("rating_snapshots")
    out = {"WB": [], "OZON": []}
    for r in ws.get_all_values()[1:]:
        if len(r) >= 10 and r[2] == "global" and r[1] in out:
            try:
                out[r[1]].append({"date": r[0], "total": int(r[4] or 0),
                                  "dist": {5: int(r[5] or 0), 4: int(r[6] or 0), 3: int(r[7] or 0),
                                           2: int(r[8] or 0), 1: int(r[9] or 0)}})
            except (ValueError, TypeError):
                pass
    for p in out:
        out[p].sort(key=lambda x: x["date"])
    return out


def _pick_before(snaps, on_or_before):
    best = None
    for s in snaps:
        if s["date"] <= on_or_before:
            best = s
    return best


def build_delta_report(mon_this):
    """Корректный недельный отчёт = ДЕЛЬТА рейтинг-снимков пн→пн (всего + звёзды, включая чистые
    звёзды). Одно сообщение WB+Ozon. Возвращает статус:
      ("DELTA", msg) — дельта собрана, слать;
      ("WAIT", None) — снимок ПРОШЛОЙ недели есть у обеих площадок, но ЭТОЙ недели ещё нет
                        (напр. Ozon-снимок 09/12:00 ещё не отработал) → НЕ слать, НЕ дедупить, ждать след. прогон;
      ("OLD",  None) — снимка прошлой недели нет (первый понедельник) → caller шлёт старый отчёт."""
    g = _snap_globals()
    prev_lo = (mon_this - timedelta(days=9)).isoformat()   # окно снимка ПРОШЛОЙ недели ~ прошлый пн
    prev_hi = (mon_this - timedelta(days=5)).isoformat()
    cur_min = (mon_this - timedelta(days=1)).isoformat()   # снимок ЭТОЙ недели (дата >= почти пн)
    title = {"WB": "🟣 <b>Wildberries</b>", "OZON": "🔵 <b>Ozon</b>"}
    prevs = {}; curs = {}
    for p in ("WB", "OZON"):
        pv = [s for s in g[p] if prev_lo <= s["date"] <= prev_hi]
        prevs[p] = pv[-1] if pv else None
        cv = [s for s in g[p] if s["date"] >= cur_min]
        curs[p] = cv[-1] if cv else None
    prev_ok = all(prevs[p] for p in ("WB", "OZON"))
    cur_ok = all(curs[p] for p in ("WB", "OZON"))
    if not prev_ok:
        return ("OLD", None)
    if not cur_ok:
        return ("WAIT", None)
    blocks = []; period_lo = None
    for p in ("WB", "OZON"):
        cur = curs[p]; prev = prevs[p]
        dt = cur["total"] - prev["total"]
        dd = {s: cur["dist"][s] - prev["dist"][s] for s in (5, 4, 3, 2, 1)}
        if period_lo is None or prev["date"] > period_lo:
            period_lo = prev["date"]
        negs_src = archive_week if p == "WB" else _oz_archive_week
        negs = [r for r in negs_src(prev["date"], cur["date"]) if (r["score"] or 5) <= 3]
        reasons = cluster_reasons(negs)
        b = [title[p], f"Новых отзывов: <b>{max(dt,0)}</b>",
             f"⭐️5 — <b>{max(dd[5],0)}</b>   ⭐️4 — <b>{max(dd[4],0)}</b>   ⭐️3 — <b>{max(dd[3],0)}</b>   "
             f"⭐️2 — <b>{max(dd[2],0)}</b>   ⭐️1 — <b>{max(dd[1],0)}</b>"]
        if reasons:
            b += ["<b>Частые причины негатива:</b>"] + reasons
        blocks.append("\n".join(b))
    from datetime import date as _date
    lo = _date.fromisoformat(period_lo); hi = mon_this - timedelta(days=1)
    head = ["📊 <b>Отзывы за неделю</b>", f"{lo.strftime('%d.%m')} – {hi.strftime('%d.%m')}", ""]
    msg = "\n".join(head) + "\n\n".join(blocks) + "\n\nЧеловек, обрати внимание😏"
    return ("DELTA", msg)


def _oz_archive_week(los, his):
    """Текстовые отзывы Ozon за неделю из архива reviews_ozon (для блока причин негатива)."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        sa = json.loads(os.environ["GSHEETS_SA_JSON"])
        gc = gspread.authorize(Credentials.from_service_account_info(
            sa, scopes=["https://www.googleapis.com/auth/spreadsheets"]))
        ws = gc.open_by_key(SHEET).worksheet("reviews_ozon")
        out = []
        for r in ws.get_all_values()[1:]:
            r = r + [""] * (8 - len(r)) if len(r) < 8 else r
            d = (r[3] or "").strip()
            if los <= d <= his:
                try:
                    sc = int(r[4])
                except (ValueError, TypeError):
                    sc = None
                out.append({"article": (r[2] or "").strip(), "score": sc,
                            "pros": (r[5] or "").strip(), "cons": (r[6] or "").strip(), "text": (r[7] or "").strip()})
        return out
    except Exception:
        return []


def main():
    today = datetime.now(MSK).date()

    # 1) СБОР В АРХИВ (ежедневно, мягко). На сбое не падаем — архив доберёт в следующий заход.
    try:
        reviews = wb_fetch(14)
        added = archive_append(reviews)
        print(f"собрано {len(reviews)}, в архив добавлено {added}", flush=True)
    except Exception as e:
        print("сбор не удался (не критично для отчёта):", str(e)[:150], flush=True)

    # 1b) СНИМОК рейтинга WB по карточкам (для недельной дельты пн→пн, включая чистые звёзды).
    #     Раз в неделю (понедельник), чтобы не плодить строки. SNAP=1 — форс (тест/базовый замер).
    if today.weekday() == 0 or os.environ.get("SNAP") == "1":
        snapshot_wb_ratings()

    # 2) ОТЧЁТ — только по понедельникам (или всегда при DRY для теста). Считаем ИЗ АРХИВА.
    is_monday = today.weekday() == 0
    if MODE == "archive" or (not is_monday and not DRY):
        print("режим архива / не понедельник — отчёт не формирую", flush=True)
        return

    mon_this = today - timedelta(days=today.weekday())
    week_key = mon_this.isoformat()
    state_path = "data/reviews_weekly_state.json"
    try:
        with open(state_path, encoding="utf-8") as f:
            already = json.load(f).get("last_sent_week") == week_key
    except (FileNotFoundError, json.JSONDecodeError):
        already = False

    def _send_and_save(msg):
        if DRY:
            print("DRY=1 — в канал НЕ отправлено"); return
        if already:
            print(f"сводка за неделю {week_key} уже отправлена — пропуск"); return
        notify.send(msg); print("отправлено в канал")
        os.makedirs("data", exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"last_sent_week": week_key}, f)

    # ПРЕДПОЧТИТЕЛЬНО: корректный ДЕЛЬТА-отчёт (всего+звёзды, WB+Ozon одним сообщением, вкл. чистые звёзды)
    status, payload = build_delta_report(mon_this)
    if status == "DELTA":
        print("--- ДЕЛЬТА-ОТЧЁТ (корректный) ---\n" + payload, flush=True)
        _send_and_save(payload)
        return
    if status == "WAIT":
        print("снимок ЭТОЙ недели ещё не готов (жду Ozon/WB-снимок) — НЕ шлю и НЕ дедуплю, поймает след. прогон", flush=True)
        return

    # ЗАПАС (первый понедельник — нет снимка прошлой недели): старый отчёт из архива (только WB, недосчёт)
    lo, hi = mon_this - timedelta(days=7), mon_this - timedelta(days=1)
    wk = archive_week(lo.isoformat(), hi.isoformat())
    st = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
    for r in wk:
        if r["score"] in st:
            st[r["score"]] += 1
    reasons = cluster_reasons([r for r in wk if (r["score"] or 5) <= 3])
    lines = ["📊 <b>Отзывы WB за неделю</b>",
             f"{lo.strftime('%d.%m')} – {hi.strftime('%d.%m')} было <b>{len(wk)} отзывов!</b>", "",
             f"⭐️ 5 звёзд — <b>{st[5]}</b>", f"⭐️ 4 звезды — <b>{st[4]}</b>",
             f"⭐️ 3 звезды — <b>{st[3]}</b>", f"⭐️ 2 звезды — <b>{st[2]}</b>",
             f"⭐️ 1 звезда — <b>{st[1]}</b>"]
    if reasons:
        lines += ["", "⚠️ <b>Самые частые причины негатива:</b>"] + reasons
    lines += ["", "Человек, обрати внимание😏"]
    msg = "\n".join(lines)
    print("--- СВОДКА (запасной старый формат, из архива) ---\n" + msg, flush=True)
    _send_and_save(msg)


if __name__ == "__main__":
    main()
