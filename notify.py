"""Отправка сообщений в Telegram через Bot API."""
from __future__ import annotations

import os
import time

import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API = "https://api.telegram.org"


def _send_chunk(text: str) -> None:
    resp = requests.post(
        f"{API}/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    resp.raise_for_status()


def send(text: str) -> None:
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    limit = 3800
    while text:
        chunk, text = text[:limit], text[limit:]
        _send_chunk(chunk)
        if text:
            time.sleep(0.4)
