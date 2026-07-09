#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проба: тянется ли Ozon-оферта (docs.ozon.ru) через Happ-VPN socks5 (OZ_PROXY).
Проверяем: HTTP, есть ли <article>, есть ли реальный текст договора, хэш (сверка с эталоном)."""
import os, re, hashlib, sys
from curl_cffi import requests as cr
sys.path.insert(0, ".")
from sources import _strip_html

OZ_PROXY = os.environ.get("OZ_PROXY", "").strip()
URL = "https://docs.ozon.ru/legal/partners/b2b/standard-terms/"

s = cr.Session(impersonate="chrome")
if OZ_PROXY:
    s.proxies = {"http": OZ_PROXY, "https": OZ_PROXY}
    print("через прокси:", OZ_PROXY)
else:
    print("БЕЗ прокси (прямой)")

try:
    r = s.get(URL, timeout=60)
    print("HTTP", r.status_code, "| html", len(r.text))
    m = re.search(r"(?is)<article[^>]*>(.*?)</article>", r.text)
    chunk = m.group(1) if m else r.text
    text = _strip_html(chunk)
    print("has_article:", bool(m), "| text_len:", len(text),
          "| 'договор':", "договор" in text.lower())
    print("hash:", hashlib.sha256(text.encode()).hexdigest()[:16], "(эталон c1a69f43…)")
    print("---- первые 300 симв текста ----")
    print(text[:300].replace("\n", " "))
except Exception as e:
    print("ОШИБКА:", str(e)[:200])
