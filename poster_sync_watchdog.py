"""Сторож автосверки календаря автопостера → канал «АС Фарм изменения».

Проверяет, что автодетект публикаций Дзена (dzen_sync в репо
nikooolechka.github.io) не залип, и что квота Scrapfly не на нуле. Контроль за
сверкой лежит на системе, а не на человеке.

Сигнал «залипло» = ОБА условия сразу:
  - есть что подтверждать (статьи released, но не published), И
  - синк давно не отрабатывал успешно (dzen_watch.json checked_at старше MAX_AGE).
Так вечно-неопубликованные статьи (Дзен берёт не все) не дают ложных тревог:
пока синк успешно бегает, checked_at свежий — тревоги нет.

Сам НЕ тратит скрейп-кредиты: читает публичные queue.json / dzen_watch.json
(GitHub raw) и только account-эндпоинт Scrapfly (бесплатный).

Дедуп через data/poster_watchdog_state.json. Секреты: SCRAPFLY_KEY, TELEGRAM_*.
"""
from __future__ import annotations

import os
import ssl
import json
import pathlib
import urllib.request
from datetime import datetime, timezone, timedelta

import notify

RAW = "https://raw.githubusercontent.com/nikooolechka/nikooolechka.github.io/main"
QUEUE_RAW = RAW + "/content/queue.json"
DZEN_WATCH_RAW = RAW + "/data/dzen_watch.json"
STATE = pathlib.Path("data/poster_watchdog_state.json")
MAX_AGE_DAYS = 5        # синк троттлится на 72ч; не отрабатывал >5 дней при наличии работы = залип
SCRAPFLY_LOW = 150      # остаток кредитов, ниже которого предупреждаем заранее
MSK = timezone(timedelta(hours=3))
DRY = os.environ.get("DRY") == "1"


def _ctx():
    return ssl._create_unverified_context()


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "asfarm-watchdog"})
    with urllib.request.urlopen(req, timeout=60, context=_ctx()) as r:
        return json.loads(r.read())


def _load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save_state(d):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2))


def _pending_count(queue):
    """Дзен: released есть, published_at нет — ждёт подтверждения."""
    n = 0
    for p in queue:
        dz = (p.get("channels") or {}).get("dzen") or {}
        if dz.get("released_at") and not dz.get("published_at"):
            n += 1
    return n


def _sync_age_days(now):
    """Сколько дней назад dzen_sync успешно отработал (по checked_at)."""
    try:
        w = _get_json(DZEN_WATCH_RAW)
        cd = datetime.fromisoformat(w["checked_at"])
        if cd.tzinfo is None:
            cd = cd.replace(tzinfo=MSK)
        return (now - cd).total_seconds() / 86400, w["checked_at"][:10]
    except Exception as e:
        print(f"[watchdog] dzen_watch недоступен: {e}", flush=True)
        return None, None


def _scrapfly(key):
    try:
        d = _get_json(f"https://api.scrapfly.io/account?key={key}")
        sc = ((d.get("subscription") or {}).get("usage") or {}).get("scrape") or {}
        per = (d.get("subscription") or {}).get("period") or {}
        return sc.get("remaining"), sc.get("limit"), (per.get("end") or "")[:10]
    except Exception as e:
        print(f"[watchdog] Scrapfly account недоступен: {e}", flush=True)
        return None, None, None


def run():
    now = datetime.now(MSK).replace(microsecond=0)
    key = os.environ.get("SCRAPFLY_KEY", "").strip()
    try:
        queue = _get_json(QUEUE_RAW)
    except Exception as e:
        print(f"[watchdog] queue.json недоступен: {e}", flush=True)
        return

    pending = _pending_count(queue)
    age, last_day = _sync_age_days(now)
    remaining, limit, reset = _scrapfly(key)
    print(f"[watchdog] pending={pending} sync_age={age} дн (посл. {last_day}) "
          f"Scrapfly={remaining}/{limit} сброс {reset}", flush=True)

    state = _load_state()
    parts = []

    # 1) Сверка залипла: есть работа И синк давно не бегал
    stuck = pending > 0 and age is not None and age > MAX_AGE_DAYS
    if stuck:
        if remaining == 0:
            sig = "stuck:zeroquota"
            body = (f"{pending} статей Дзена ждут подтверждения, синк не отрабатывал с {last_day}. "
                    f"Причина: квота Scrapfly на нуле, сброс {reset}. После сброса досверится сам.")
        elif isinstance(remaining, int) and remaining > 0:
            sig = "stuck:livequota"
            body = (f"{pending} статей Дзена ждут подтверждения, синк не отрабатывал с {last_day}, "
                    f"а квота Scrapfly есть (остаток {remaining}). Похоже, синк падает — разобраться.")
        else:
            sig = "stuck:unknownquota"
            body = (f"{pending} статей Дзена ждут подтверждения, синк не отрабатывал с {last_day}. "
                    "Квоту Scrapfly опросить не удалось — проверить.")
        if state.get("stuck_sig") != sig:
            parts.append("⚠️ <b>Автосверка календаря залипла.</b>")
            parts.append(body)
            state["stuck_sig"] = sig
    else:
        state.pop("stuck_sig", None)

    # 2) Квота на исходе — предупреждаем заранее (правило про лимиты)
    if isinstance(remaining, int) and 0 < remaining < SCRAPFLY_LOW:
        sig = f"low:{remaining // 50}"
        if state.get("low_sig") != sig:
            parts.append("🟡 <b>Scrapfly: квота на исходе.</b>")
            parts.append(f"Осталось {remaining} из {limit} кредитов (сброс {reset}). "
                         "Если закончится — автосверка календаря встанет до сброса.")
            state["low_sig"] = sig
    elif isinstance(remaining, int) and remaining >= SCRAPFLY_LOW:
        state.pop("low_sig", None)

    msg = "\n".join(parts).strip()
    if DRY:
        print("[watchdog][DRY]:\n" + (msg or "(тихо, всё в норме)"), flush=True)
        _save_state(state)
        return
    if msg:
        notify.send(msg)
        print("[watchdog] алерт отправлен", flush=True)
    else:
        print("[watchdog] всё в норме — молчим", flush=True)
    _save_state(state)


if __name__ == "__main__":
    run()
