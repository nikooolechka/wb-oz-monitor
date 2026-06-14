"""Источники и загрузка/нормализация их содержимого."""
from __future__ import annotations

import io
import os
import re
import time
import hashlib
from dataclasses import dataclass
from urllib.parse import quote

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
}

TIMEOUT = 45


@dataclass(frozen=True)
class Source:
    key: str
    platform: str
    title: str
    url: str
    kind: str


SOURCES: list[Source] = [
    Source(
        key="wb_offer_pdf",
        platform="WB",
        title="Оферта о реализации товара на Wildberries (полный текст)",
        url="https://static-basket-02.wb.ru/vol20/offers/prd/product/latest.pdf",
        kind="pdf",
    ),
    Source(
        key="oz_b2b_standard_terms",
        platform="OZON",
        title="Ozon — Условия договора поставки (B2B, для продавцов)",
        url="https://docs.ozon.ru/legal/partners/b2b/standard-terms/",
        kind="html",
    ),
]


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|head).*?</\1>", " ", html)
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", "\n", html)
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'"))
    return text


def _pdf_to_text(data: bytes) -> str:
    import fitz
    parts = []
    with fitz.open(stream=io.BytesIO(data), filetype="pdf") as doc:
        for page in doc:
            parts.append(page.get_text("text"))
    return "\n".join(parts)


def normalize(text: str) -> str:
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t ]+", " ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    lines = [ln for ln in lines if not re.fullmatch(r"\d{1,4}", ln)]
    return "\n".join(lines)


SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "").strip()


def _get(url: str, kind: str) -> requests.Response:
    use_proxy = kind == "html" and SCRAPERAPI_KEY
    if not use_proxy:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp

    proxied = (
        "https://api.scraperapi.com/"
        f"?api_key={SCRAPERAPI_KEY}&render=true&country_code=ru&url={quote(url, safe='')}"
    )
    last_exc = None
    for attempt in range(2):
        try:
            resp = requests.get(proxied, headers=HEADERS, timeout=60)
            if resp.status_code in (500, 429, 502, 503):
                last_exc = requests.HTTPError(f"{resp.status_code} от ScraperAPI")
                time.sleep(3)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            time.sleep(3)
    raise last_exc


def fetch(source: Source) -> tuple[str, str]:
    resp = _get(source.url, source.kind)
    if source.kind == "pdf":
        raw = _pdf_to_text(resp.content)
    else:
        resp.encoding = resp.apparent_encoding or "utf-8"
        raw = _strip_html(resp.text)
    norm = normalize(raw)
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return norm, digest
