"""Проверка оферты (договора поставки) Ozon через Scrapfly.

docs.ozon.ru нельзя спарсить напрямую — жёсткий антибот (режет даже резидентные
IP и headless-браузеры). Scrapfly с режимом ASP его обходит (проверено 5/5),
отдаёт полный документ. Скрипт тянет текст договора, сравнивает с прошлой
версией и при изменении шлёт выжимку в Telegram; если без изменений — пишет
статус «изменений не обнаружено».

КООРДИНАЦИЯ ДВУХ МАКОВ: общее состояние — в репозитории (data/ozon_shared.json)
через GitHub API. Если сегодня Ozon уже проверен, второй Мак пропускает.

Секреты — из ~/.wb-oz-monitor/.env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_REPO, SCRAPFLY_KEY.
"""
from __future__ import annotations

import os
import sys
import json
import base64
import hashlib
import subprocess
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = os.path.expanduser("~/.wb-oz-monitor")
ENV_FILE = os.path.join(HOME_DIR, ".env")

OZON_URL = "https://docs.ozon.ru/legal/partners/b2b/standard-terms/"
OZON_TITLE = "Ozon — Условия договора поставки (B2B, для продавцов)"
SHARED_PATH = "data/ozon_shared.json"
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


def now_msk_str() -> str:
    return datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК")


def fetch_ozon_text() -> str:
    """Тянет договор Ozon через Scrapfly: ASP обходит антибот, RU-прокси + JS-рендер.
    Возвращает текст блока <article> (договор без бокового меню). Работает из
    облака/с любого Мака — не зависит от Safari."""
    import re
    import requests
    from urllib.parse import quote
    key = os.environ.get("SCRAPFLY_KEY", "").strip()  # читаем после load_env()
    api = ("https://api.scrapfly.io/scrape?key=" + key
           + "&url=" + quote(OZON_URL, safe="")
           + "&asp=true&render_js=true&country=ru")
    r = requests.get(api, timeout=120)
    r.raise_for_status()
    html = ((r.json().get("result") or {}).get("content") or "")
    m = re.search(r"(?is)<article[^>]*>(.*?)</article>", html)
    chunk = m.group(1) if m else html
    sys.path.insert(0, HERE)
    from sources import _strip_html
    text = _strip_html(chunk)
    if "договор" not in text.lower() or len(text) < 2000:
        raise RuntimeError("Scrapfly вернул не тот контент (договор не найден)")
    return text


def _gh_headers() -> dict:
    import requests  # noqa: F401 (ensure available)
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN'].strip()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def read_shared() -> tuple[dict, str | None]:
    """Возвращает (данные, sha файла). Пустой dict если файла нет."""
    import requests
    repo = os.environ["GITHUB_REPO"].strip()
    url = f"https://api.github.com/repos/{repo}/contents/{SHARED_PATH}?ref=main"
    r = requests.get(url, headers=_gh_headers(), timeout=30)
    if r.status_code == 404:
        return {}, None
    r.raise_for_status()
    j = r.json()
    raw = base64.b64decode(j["content"]).decode("utf-8").strip()
    return (json.loads(raw) if raw else {}), j["sha"]


def write_shared(data: dict, sha: str | None) -> None:
    import requests
    repo = os.environ["GITHUB_REPO"].strip()
    url = f"https://api.github.com/repos/{repo}/contents/{SHARED_PATH}"
    body = {
        "message": "ozon coordination update",
        "content": base64.b64encode(
            json.dumps(data, ensure_ascii=False).encode("utf-8")).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=_gh_headers(), json=body, timeout=30)
    r.raise_for_status()


def main() -> None:
    load_env()
    sys.path.insert(0, HERE)
    from sources import normalize
    from summarize import make_diff, summarize, fallback_summary
    import notify
    import html as _html

    has_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))

    # 1) общий флаг: проверено ли сегодня (координация двух Маков)
    try:
        shared, sha = read_shared()
    except Exception as e:
        print(f"[WARN] не прочитал общий флаг: {e}", flush=True)
        return
    if shared.get("date") == today_msk():
        print("[SKIP] Ozon уже проверен сегодня — пропускаю (другой Мак)", flush=True)
        return

    # 2) читаем документ из Safari
    try:
        raw = fetch_ozon_text()
    except Exception as e:
        print(f"[WARN] Ozon (Safari): {e}", flush=True)
        return  # дату НЕ ставим — пусть следующий запуск/Мак попробует снова

    text = normalize(raw)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    prev_hash = shared.get("hash")

    # 3) первая фиксация
    if not prev_hash:
        write_shared({"date": today_msk(), "hash": digest, "text": text}, sha)
        notify.send(
            "🔵 <b>Ozon подключён к мониторингу</b>\n"
            f"Отслеживаю «{OZON_TITLE}». Сообщу при изменении."
        )
        print("[OK] Ozon: базовая версия зафиксирована", flush=True)
        return

    # 4) без изменений — помечаем день, пишем в чат «всё проверено, изменений нет»
    if digest == prev_hash:
        shared["date"] = today_msk()
        write_shared(shared, sha)
        notify.send(
            "🔵 <b>Ozon проверен</b> — изменений в договоре не обнаружено ✅\n"
            f"<i>{now_msk_str()}</i>"
        )
        print("[OK] Ozon: без изменений (статус отправлен)", flush=True)
        return

    # 5) изменение — выжимка + уведомление
    diff_text = make_diff(shared.get("text", ""), text)
    try:
        summary = summarize("OZON", OZON_TITLE, diff_text) if has_llm else fallback_summary(diff_text)
    except Exception as e:
        print(f"[WARN] выжимка: {e}", flush=True)
        summary = fallback_summary(diff_text)

    write_shared({"date": today_msk(), "hash": digest, "text": text}, sha)

    if summary is None:
        print("[INFO] Ozon: изменение незначимо", flush=True)
        return

    notify.send(
        "🔵 Ozon — <b>изменение в документе</b>\n"
        f"<b>{_html.escape(OZON_TITLE)}</b>\n\n"
        f"{_html.escape(summary)}\n\n"
        f"🔗 {OZON_URL}"
    )
    print("[ALERT] Ozon: отправлено уведомление", flush=True)


if __name__ == "__main__":
    main()
