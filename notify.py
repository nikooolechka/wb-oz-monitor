"""Отправка сообщений в Telegram через Bot API.

Шлём в один чат (TELEGRAM_CHAT_ID) — это id группы, где сидят собственник и
РОП. Бот должен быть добавлен в группу. Для группы id отрицательный
(например -1001234567890).
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


MSK = ZoneInfo("Europe/Moscow")


def automatic_alert_silent(now: datetime | None = None) -> bool:
    """Тихий режим служебных алертов: вне 09:00–20:00 МСК и все выходные."""
    current = now.astimezone(MSK) if now is not None else datetime.now(MSK)
    return current.weekday() >= 5 or current.hour < 9 or current.hour >= 20


def _silent() -> bool:
    """Обратная совместимость для старых импортов."""
    return automatic_alert_silent()

# .strip() — секреты часто копируют с лишним переносом строки/пробелом,
# из-за чего адрес Telegram становится битым (bot<token>%0A) → 404.
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
API = "https://api.telegram.org"


def _send_chunk(text: str, silent: bool) -> None:
    resp = requests.post(
        f"{API}/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        },
        timeout=30,
    )
    resp.raise_for_status()


def send(text: str) -> None:
    """Telegram режет сообщения на 4096 символов — бьём на части.

    Режем ТОЛЬКО по границам строк, иначе кусок может разорвать HTML-тег
    (<b>…</b>) пополам и Telegram вернёт 400 «can't parse entities».
    """
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    limit = 4096  # потолок Telegram; готовые блоки дайджеста ≤4050 уходят целиком, цитаты не рвутся
    silent = automatic_alert_silent()  # один режим на всё логическое сообщение
    chunk = ""
    for line in text.split("\n"):
        # одиночная строка длиннее лимита — режем жёстко (редкий край)
        while len(line) > limit:
            if chunk:
                _send_chunk(chunk, silent); time.sleep(0.4); chunk = ""
            _send_chunk(line[:limit], silent); time.sleep(0.4)
            line = line[limit:]
        if len(chunk) + len(line) + 1 > limit:
            _send_chunk(chunk, silent); time.sleep(0.4)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        _send_chunk(chunk, silent)
