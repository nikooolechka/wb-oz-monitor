"""Чтение постов Telegram-каналов через публичное веб-превью t.me/s/<канал>.

Без симки, Telethon и API: страница t.me/s/ отдаёт последние ~16-20 постов
канала с точной датой (<time datetime>), дословным текстом и живыми ссылками.
Парсим сырой HTML — НЕ через LLM-пересказчик (тот теряет даты, искажает текст
и домысливает).

Пагинация (?before=<id>) — до 3 страниц назад, пока не покроем вчерашние сутки.
Это нужно для активных каналов, где к утру вчерашние посты вытеснены из
последних ~20.

Чат (группа) через t.me/s/ не читается — такие источники сюда не добавлять,
они уходят в этап с Telethon. @viktor_gamm_mp — это чат, поэтому его тут нет.
"""
from __future__ import annotations

import re
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from html import unescape

import requests

# В песочнице исходящий трафик идёт через прокси с self-signed сертификатом,
# поэтому для локальной калибровки можно выключить проверку (INSECURE_SSL=1).
# На GitHub Actions проверка серта работает штатно — флаг там не ставить.
VERIFY = os.environ.get("INSECURE_SSL") != "1"
if not VERIFY:
    requests.packages.urllib3.disable_warnings()  # type: ignore

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}
TIMEOUT = 30

# 28 каналов. Чат @viktor_gamm_mp исключён (см. docstring).
CHANNELS = [
    "rbordunov", "wbbillion", "dnative", "daryamelanich", "linnik_wb",
    "prodazinawb", "petrochenkow", "marketplace_hogwarts", "ababruev",
    "ozonmarketplace", "andrey_pro_business", "maksimenkoprobusines",
    "ilya_krasinsky", "wildberriesru_official",
    # добавлены 2026-06-15 пользователем
    "burlak_na_mp", "kultura_analitiki", "ozonmaximal", "ozonwoman",
    "roma_iz_sellium", "kogteva_pro_ozon",
    # добавлены 2026-06-16 пользователем (marketplace_hogwarts уже был — не дублируем)
    "kovpak_kitai", "vladlen_strokan", "maxim_popov_wb", "redman",
    "sellermp", "postavleno", "marketpapa_channel", "wbsellerofficial",
]


@dataclass(frozen=True)
class Post:
    channel: str
    post_id: int           # монотонный id поста (из data-post="канал/ID")
    dt: datetime           # время поста (UTC)
    text: str              # дословный текст
    links: tuple[str, ...] # внешние ссылки из поста


def _clean(html_fragment: str) -> str:
    h = html_fragment.replace("<br>", "\n").replace("<br/>", "\n")
    h = re.sub(r"<[^>]+>", "", h)
    return unescape(h).strip()


def _parse(channel: str, page: str) -> list[Post]:
    posts: list[Post] = []
    blocks = re.split(r'<div class="tgme_widget_message_wrap', page)
    for b in blocks[1:]:
        mid = re.search(r'data-post="[^/]+/(\d+)"', b)
        dt = re.search(r'<time[^>]*datetime="([^"]+)"', b)
        if not (mid and dt):
            continue
        try:
            t = datetime.fromisoformat(dt.group(1)).astimezone(timezone.utc)
        except ValueError:
            continue
        tm = re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*'
            r'<div class="tgme_widget_message_footer',
            b, re.DOTALL,
        ) or re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            b, re.DOTALL,
        )
        raw = tm.group(1) if tm else ""
        text = _clean(raw)
        links = tuple(dict.fromkeys(
            h for h in re.findall(r'href="(https?://[^"]+)"', raw)
            if "t.me/" not in h  # внутренние ссылки на ТГ не тащим в дайджест
        ))
        posts.append(Post(channel, int(mid.group(1)), t, text, links))
    return posts


def fetch_channel(channel: str) -> list[Post]:
    """Посты канала с пагинацией (?before=<id>) до 3 страниц назад.

    Останавливается, как только появляется пост старше вчерашних суток МСК —
    это гарантирует, что вчерашние посты активных каналов не теряются.
    """
    MSK = timezone(timedelta(hours=3))
    yesterday = (datetime.now(MSK) - timedelta(days=1)).date()

    all_posts: list[Post] = []
    seen_ids: set[int] = set()
    before_id: int | None = None

    for _ in range(8):  # было 3 — активные каналы за утро не докручивали до вчера
        url = f"https://t.me/s/{channel}"
        if before_id is not None:
            url += f"?before={before_id}"
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=VERIFY)
        resp.raise_for_status()
        page_posts = _parse(channel, resp.text)
        if not page_posts:
            break
        new_posts = [p for p in page_posts if p.post_id not in seen_ids]
        if not new_posts:
            break
        seen_ids.update(p.post_id for p in new_posts)
        all_posts.extend(new_posts)
        oldest = min(p.dt.astimezone(MSK).date() for p in new_posts)
        if oldest < yesterday:
            break
        before_id = min(p.post_id for p in new_posts)

    return all_posts
