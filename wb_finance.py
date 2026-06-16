"""Независимый сборщик финансовых данных WB → собственный кэш (не из дашборда).

Источник истины — WB API напрямую (statistics-api). Тянем РАЗ за период и
кэшируем в data/wb/, чтобы не ходить в WB повторно и не упираться в лимит.
Лимит reportDetailByPeriod = 1 запрос/мин. На 429: читать X-Ratelimit-Retry и
ждать ровно столько, один повтор. Токен — из env WB_TOKEN.
"""
from __future__ import annotations

import os
import json
import time
import pathlib
import urllib.request
import urllib.error
import ssl

BASE = "https://statistics-api.wildberries.ru"
CACHE_DIR = pathlib.Path(__file__).parent / "data" / "wb"
INSECURE = os.environ.get("INSECURE_SSL") == "1"


def _token() -> str:
    t = os.environ.get("WB_TOKEN", "").strip()
    if not t:
        raise RuntimeError("нет WB_TOKEN в окружении")
    return t


def _get(path: str, params: dict):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{BASE}{path}?{qs}", headers={"Authorization": _token()})
    ctx = ssl._create_unverified_context() if INSECURE else None
    try:
        with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
            return r.status, json.loads(r.read() or b"null"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, {}, dict(e.headers or {})


def fetch_realization(date_from: str, date_to: str, *, refresh: bool = False) -> list:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"realization_{date_from}_{date_to}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())

    rows: list = []
    rrd = 0
    waited_429 = False
    while True:
        code, body, hdr = _get("/api/v5/supplier/reportDetailByPeriod",
                               {"dateFrom": date_from, "dateTo": date_to,
                                "limit": 100000, "rrdid": rrd})
        if code == 429:
            retry = int(hdr.get("X-Ratelimit-Retry") or hdr.get("Retry-After") or 60)
            if waited_429:
                raise RuntimeError(f"WB всё ещё 429 после ожидания (retry={retry}с) — повтор по расписанию")
            print(f"[429] жду X-Ratelimit-Retry={retry}с, без повторных стуков", flush=True)
            time.sleep(retry + 2)
            waited_429 = True
            continue
        if code != 200:
            raise RuntimeError(f"WB HTTP {code}: {str(body)[:200]}")
        if not isinstance(body, list) or not body:
            break
        rows.extend(body)
        if len(body) < 100000:
            break
        rrd = body[-1]["rrd_id"]
        print(f"[pager] +{len(body)} строк, пауза 61с", flush=True)
        time.sleep(61)

    cache.write_text(json.dumps(rows, ensure_ascii=False))
    print(f"[ok] реализация {date_from}..{date_to}: {len(rows)} строк", flush=True)
    return rows


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    df, dt = (a + ["2026-06-01", "2026-06-30"])[:2]
    fetch_realization(df, dt, refresh="--refresh" in a)
