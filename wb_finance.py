"""Независимый сборщик финансовых данных WB → собственный кэш (не из дашборда).

Источник истины — WB API напрямую (statistics-api). Тянем РАЗ за период и
кэшируем в data/wb/, чтобы не ходить в WB повторно и не упираться в лимит.

Лимит reportDetailByPeriod очень строгий. Дисциплина 429: НЕ повторять вообще —
на 429 сразу выход с ошибкой. Любой повтор во время бана продлевает кулдаун.
Период тянем ОДИН раз и кэшируем навсегда; под запрос — читаем кэш, WB не дёргаем.

Токен — из env WB_TOKEN (в GitHub Actions — секрет; локально — ~/.wb-oz-monitor/.env).
Себестоимость тут НЕ трогаем: это закуп продавца, не данные WB (см. wb_pnl.py).
"""
from __future__ import annotations

import os
import json
import time
import pathlib
import urllib.request
import urllib.error

BASE = "https://statistics-api.wildberries.ru"
CACHE_DIR = pathlib.Path(__file__).parent / "data" / "wb"
INSECURE = os.environ.get("INSECURE_SSL") == "1"  # только для песочницы


def _token() -> str:
    t = os.environ.get("WB_TOKEN", "").strip()
    if not t:
        raise RuntimeError("нет WB_TOKEN в окружении")
    return t


def _get(path: str, params: dict) -> tuple[int, list | dict, dict]:
    """Один GET. Возвращает (http_code, body, headers). Без ретраев внутри."""
    import ssl
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{BASE}{path}?{qs}", headers={"Authorization": _token()})
    if INSECURE:
        ctx = ssl._create_unverified_context()
    else:
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
            return r.status, json.loads(r.read() or b"null"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, {}, dict(e.headers or {})


def fetch_realization(date_from: str, date_to: str, *, refresh: bool = False) -> list:
    """Отчёт реализации за период. Кэшируется в data/wb/realization_<from>_<to>.json.

    На 429 — СРАЗУ выход с ошибкой, БЕЗ сна и повторов (автоповтор в бане продлевает бан).
    Пагинация по rrdid с паузой 61с между страницами (лимит 1/мин).
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"realization_{date_from}_{date_to}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())

    rows: list = []
    rrd = 0
    while True:
        code, body, hdr = _get("/api/v5/supplier/reportDetailByPeriod",
                               {"dateFrom": date_from, "dateTo": date_to,
                                "limit": 100000, "rrdid": rrd})
        if code == 429:
            retry = int(hdr.get("X-Ratelimit-Retry") or hdr.get("Retry-After") or 60)
            raise RuntimeError(
                f"WB 429 (бан rate-limit). НЕ повторять автоматически — продлевает бан. "
                f"Вручную не раньше ~{retry}с (~{retry//3600}ч {retry%3600//60}м). "
                f"Кэш появится после успешного ручного запуска.")
        if code != 200:
            raise RuntimeError(f"WB HTTP {code}: {str(body)[:200]}")
        if not isinstance(body, list) or not body:
            break
        rows.extend(body)
        if len(body) < 100000:
            break
        rrd = body[-1]["rrd_id"]
        print(f"[pager] +{len(body)} строк, пауза 61с (лимит 1/мин)", flush=True)
        time.sleep(61)

    cache.write_text(json.dumps(rows, ensure_ascii=False))
    print(f"[ok] реализация {date_from}..{date_to}: {len(rows)} строк → {cache.name}", flush=True)
    return rows


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    df, dt = (a + ["2026-06-01", "2026-06-30"])[:2]
    fetch_realization(df, dt, refresh="--refresh" in a)
