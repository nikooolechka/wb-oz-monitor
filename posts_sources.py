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

# t.me/s/ заблокирован с дата-центровых IP (GitHub Actions/Яндекс) с ~середины
# июля 2026 — прямой requests.get даёт «Failed to resolve t.me». Их прокси-сеть
# t.me достаёт → читаем через Scrapfly (плейн datacenter-прокси, ~1 кредит/стр.).
# Ключ — отдельный соц-аккаунт Scrapfly (фолбэк на основной). Без ключа (локально
# с жилого IP) — прямой запрос, бесплатно.
SCRAPFLY_KEY = (os.environ.get("SCRAPFLY_KEY_SOCIAL")
                or os.environ.get("SCRAPFLY_KEY") or "").strip()
# Прямое чтение бесплатное → листаем полными сутками (ранний обрыв сам
# остановится, как дойдём до позавчера). Кредиты не при чём — прямой путь основной.
MAX_PAGES = 8


class DirectDownError(RuntimeError):
    """Прямой t.me недоступен подряд > лимита → обход НЕ спасение, надо чинить."""


# Scrapfly — КРАЙНЯЯ мера, лимит 3 срабатывания ПОДРЯД (у Scrapfly свой лимит,
# в который мы не влезаем — гонять по нему дайджест каждый день НЕЛЬЗЯ). Если
# обход сработал >3 раз подряд → прямой t.me лёг широко: стоп жечь кредиты и
# сигнал «чинить». Любой успех прямого пути обнуляет счётчик.
MAX_FALLBACK_STREAK = 3
_fallback_streak = 0

# ── Защита от ПРИРОДЫ июльского сбоя: DNS-резолв t.me на раннере GitHub упал
# («Failed to resolve 't.me'»), при этом остальная сеть работала. Значит чиним
# именно DNS: если системный резолвер не смог t.me — берём его IP через DoH
# (dns.google по HTTPS, свой резолвер) и «пиним» на уровне сокета, после чего
# читаем t.me НАПРЯМУЮ (бесплатно). Это тир ДО Scrapfly. ─────────────────────
import socket as _socket
_PIN: dict = {}
_orig_getaddrinfo = _socket.getaddrinfo
def _patched_getaddrinfo(host, *a, **kw):
    return _orig_getaddrinfo(_PIN.get(host, host), *a, **kw)
_socket.getaddrinfo = _patched_getaddrinfo


def _doh_ip(host: str = "t.me"):
    """IP host'а через DoH (dns.google, HTTPS) — в обход сломанного DNS раннера."""
    r = requests.get("https://dns.google/resolve",
                     params={"name": host, "type": "A"}, timeout=15, verify=VERIFY)
    for ans in (r.json().get("Answer") or []):
        if ans.get("type") == 1 and ans.get("data"):
            return ans["data"]
    return None


def _direct(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=VERIFY)
    r.raise_for_status()
    return r.text


def _fetch_html(url: str) -> str:
    """HTML t.me/s/. Три тира: (1) прямой — бесплатно, ВСЕГДА первый; (2) DoH-пин
    IP + прямой — бесплатно, лечит DNS-сбой раннера (природа июля); (3) Scrapfly —
    крайняя мера, не чаще 3 раз подряд, дальше сигнал «чинить»."""
    global _fallback_streak
    # Тир 1 — прямой
    try:
        html = _direct(url)
        _fallback_streak = 0
        return html
    except Exception as e1:
        pass
    # Тир 2 — DoH-пин IP t.me + прямой (бесплатно, против DNS-сбоя раннера)
    try:
        ip = _doh_ip("t.me")
        if ip:
            _PIN["t.me"] = ip
            html = _direct(url)
            _fallback_streak = 0
            print(f"[DoH] DNS t.me сбоил на раннере → резолв через dns.google={ip}, "
                  f"читаю НАПРЯМУЮ (0 кредитов)", flush=True)
            return html
    except Exception:
        pass
    # Тир 3 — Scrapfly, крайняя мера с лимитом
    if not SCRAPFLY_KEY:
        raise RuntimeError(f"t.me недоступен и прямой, и через DoH: {url}")
    if _fallback_streak >= MAX_FALLBACK_STREAK:
        raise DirectDownError(
            f"прямой t.me (и DoH) недоступны подряд >{MAX_FALLBACK_STREAK} раз")
    _fallback_streak += 1
    from urllib.parse import quote
    api = ("https://api.scrapfly.io/scrape?key=" + SCRAPFLY_KEY
           + "&url=" + quote(url, safe=""))
    r = requests.get(api, timeout=120, verify=VERIFY)
    r.raise_for_status()
    html = (r.json().get("result") or {}).get("content") or ""
    if not html:
        raise RuntimeError(f"и прямой, и DoH, и Scrapfly пусто для {url}")
    print(f"[FALLBACK {_fallback_streak}/{MAX_FALLBACK_STREAK}] прямой+DoH t.me "
          f"не дали → временно Scrapfly (крайняя мера)", flush=True)
    return html

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

    for _ in range(MAX_PAGES):
        url = f"https://t.me/s/{channel}"
        if before_id is not None:
            url += f"?before={before_id}"
        page_posts = _parse(channel, _fetch_html(url))
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
