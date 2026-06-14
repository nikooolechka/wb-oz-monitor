"""Главный цикл мониторинга."""
from __future__ import annotations

import os
import time
import html
import traceback
from datetime import datetime, timezone, timedelta

import sources
import state
import notify
from summarize import make_diff, summarize, fallback_summary

HAS_LLM = bool(os.environ.get("ANTHROPIC_API_KEY"))
INTERVAL = float(os.environ.get("CHECK_INTERVAL_HOURS", "3")) * 3600
MSK = timezone(timedelta(hours=3))


def _now() -> str:
    return datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК")


def check_once(st: dict) -> None:
    first_run = not st
    new_baselines = 0

    for src in sources.SOURCES:
        try:
            text, digest = sources.fetch(src)
        except Exception as e:
            print(f"[WARN] {src.key}: ошибка загрузки: {e}", flush=True)
            continue

        prev = st.get(src.key)
        if prev is None:
            st[src.key] = {"hash": digest, "text": text, "checked": _now()}
            new_baselines += 1
            continue

        if prev["hash"] == digest:
            st[src.key]["checked"] = _now()
            continue

        diff_text = make_diff(prev.get("text", ""), text)
        try:
            if HAS_LLM:
                summary = summarize(src.platform, src.title, diff_text)
            else:
                summary = fallback_summary(diff_text)
        except Exception as e:
            print(f"[WARN] {src.key}: ошибка выжимки: {e}", flush=True)
            summary = fallback_summary(diff_text)

        st[src.key] = {"hash": digest, "text": text, "checked": _now()}

        if summary is None:
            print(f"[INFO] {src.key}: незначимо", flush=True)
            continue

        badge = "🟣 WB" if src.platform == "WB" else "🔵 Ozon"
        message = (
            f"{badge} — <b>изменение в документе</b>\n"
            f"<b>{html.escape(src.title)}</b>\n"
            f"<i>{_now()}</i>\n\n"
            f"{html.escape(summary)}\n\n"
            f"🔗 {html.escape(src.url)}"
        )
        notify.send(message)
        print(f"[ALERT] {src.key}: отправлено", flush=True)

    state.save(st)

    if first_run:
        notify.send(
            "✅ <b>Мониторинг оферт и правил запущен</b>\n"
            f"Отслеживаю {new_baselines} документ(ов) WB и Ozon.\n"
            f"Проверка каждые {INTERVAL/3600:g} ч. Сообщу об изменениях."
        )


def run_once() -> None:
    st = state.load()
    check_once(st)
    os.makedirs(os.path.dirname(state.STATE_PATH) or ".", exist_ok=True)
    with open("data/last_run.txt", "w", encoding="utf-8") as f:
        f.write(_now() + "\n")


def main() -> None:
    if os.environ.get("RUN_ONCE"):
        print("[RUN_ONCE] один прогон", flush=True)
        run_once()
        return
    print(f"[START] worker, интервал {INTERVAL/3600:g} ч", flush=True)
    while True:
        st = state.load()
        try:
            check_once(st)
        except Exception:
            print("[ERROR] сбой:\n" + traceback.format_exc(), flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
