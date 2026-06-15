"""Локальная проверка оферты WB через launchd.

Облачный cron GitHub для этого репозитория не срабатывает (планировщик
ненадёжен), поэтому WB проверяем с Мака. WB-оферта — публичный PDF без
антибота, тянется напрямую (без Safari, без окон).

Координация двух Маков — через data/state.json в репозитории (GitHub API):
если сегодня WB уже проверен, второй Мак молча пропускает.

Секреты — из ~/.wb-oz-monitor/.env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_REPO.
"""
from __future__ import annotations

import os
import sys
import json
import base64
import html as _html
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = os.path.expanduser("~/.wb-oz-monitor")
ENV_FILE = os.path.join(HOME_DIR, ".env")
STATE_PATH = "data/state.json"
KEY = "wb_offer_pdf"
MSK = timezone(timedelta(hours=3))


def load_env() -> None:
    if not os.path.exists(ENV_FILE):
        return
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def today_msk() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def now_msk() -> str:
    return datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК")


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN'].strip()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def read_state() -> tuple[dict, str | None]:
    import requests
    repo = os.environ["GITHUB_REPO"].strip()
    url = f"https://api.github.com/repos/{repo}/contents/{STATE_PATH}?ref=main"
    r = requests.get(url, headers=_gh_headers(), timeout=30)
    if r.status_code == 404:
        return {}, None
    r.raise_for_status()
    j = r.json()
    raw = base64.b64decode(j["content"]).decode("utf-8").strip()
    return (json.loads(raw) if raw else {}), j["sha"]


def write_state(data: dict, sha: str | None) -> None:
    import requests
    repo = os.environ["GITHUB_REPO"].strip()
    url = f"https://api.github.com/repos/{repo}/contents/{STATE_PATH}"
    body = {
        "message": "wb state update (local)",
        "content": base64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=_gh_headers(), json=body, timeout=30)
    r.raise_for_status()


def main() -> None:
    load_env()
    sys.path.insert(0, HERE)
    from sources import SOURCES, fetch
    from summarize import make_diff, summarize, fallback_summary
    import notify

    has_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))
    wb_src = next(s for s in SOURCES if s.key == KEY)

    # 1) общий флаг: проверено ли сегодня (координация двух Маков)
    try:
        state, sha = read_state()
    except Exception as e:
        print(f"[WARN] не прочитал state.json: {e}", flush=True)
        return
    wb = state.get(KEY, {})
    if wb.get("date") == today_msk():
        print("[SKIP] WB уже проверен сегодня — пропускаю (другой Мак)", flush=True)
        return

    # 2) скачиваем оферту WB (PDF, напрямую)
    try:
        text, digest = fetch(wb_src)
    except Exception as e:
        print(f"[WARN] WB fetch: {e}", flush=True)
        return  # дату не ставим — пусть следующий запуск попробует

    prev_hash = wb.get("hash")
    today = today_msk()

    # 3) первая фиксация (если базы ещё нет)
    if not prev_hash:
        state[KEY] = {"hash": digest, "text": text, "date": today, "checked": now_msk()}
        write_state(state, sha)
        notify.send(
            "🟣 <b>Мониторинг WB запущен</b>\n"
            f"Отслеживаю «{wb_src.title}». Сообщу при изменении."
        )
        print("[OK] WB: базовая версия зафиксирована", flush=True)
        return

    # 4) без изменений — помечаем день, пишем в чат «всё проверено, изменений нет»
    if digest == prev_hash:
        wb["date"] = today
        wb["checked"] = now_msk()
        state[KEY] = wb
        write_state(state, sha)
        notify.send(
            "🟣 <b>WB проверен</b> — изменений в оферте не обнаружено ✅\n"
            f"<i>{now_msk()}</i>"
        )
        print("[OK] WB: без изменений (статус отправлен)", flush=True)
        return

    # 5) изменение — выжимка + уведомление
    diff_text = make_diff(wb.get("text", ""), text)
    try:
        summary = summarize("WB", wb_src.title, diff_text) if has_llm else fallback_summary(diff_text)
    except Exception as e:
        print(f"[WARN] выжимка: {e}", flush=True)
        summary = fallback_summary(diff_text)

    state[KEY] = {"hash": digest, "text": text, "date": today, "checked": now_msk()}
    write_state(state, sha)

    if summary is None:
        print("[INFO] WB: изменение незначимо", flush=True)
        return

    notify.send(
        "🟣 WB — <b>изменение в документе</b>\n"
        f"<b>{_html.escape(wb_src.title)}</b>\n"
        f"<i>{now_msk()}</i>\n\n"
        f"{_html.escape(summary)}\n\n"
        f"🔗 {wb_src.url}"
    )
    print("[ALERT] WB: отправлено уведомление", flush=True)


if __name__ == "__main__":
    main()
