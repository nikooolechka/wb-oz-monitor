"""Проверка оферт/договоров Ozon для продавцов через Scrapfly.

docs.ozon.ru нельзя спарсить напрямую — жёсткий антибот. Scrapfly (ASP +
render_js + RU) его обходит, отдаёт документ. Берём блок <article> каждого
документа, сравниваем с эталоном, и раз в 5 дней шлём в канал ОДНО сообщение:
изменений нет — по шаблону; есть — с указанием, в каком документе.

Документы (для продавца товаров):
  • Договор поставки            /legal/partners/b2b/standard-terms
  • Условия оказания услуг       /legal/partners/b2b/service-terms
  • Условия выполнения работ     /legal/partners/b2b/contract-work-terms
  • Золотые правила безопасности /legal/partners/b2b/safety-rules

Частота: раз в 5 дней (самотроттлинг по дате в общем состоянии). Воркфлоу может
запускаться хоть ежедневно — в «нерабочие» дни скрипт молча выходит, Scrapfly
не тратит. 4 док × ~30 кред = ~120/прогон, ~720/мес (в рамках бюджета 800).

Состояние (коорд. + эталоны) — data/ozon_shared.json через GitHub API.
Секреты — из ~/.wb-oz-monitor/.env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_REPO, SCRAPFLY_KEY.
"""
from __future__ import annotations

import os
import sys
import json
import base64
import hashlib
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = os.path.expanduser("~/.wb-oz-monitor")
ENV_FILE = os.path.join(HOME_DIR, ".env")
SHARED_PATH = "data/ozon_shared.json"
MSK = timezone(timedelta(hours=3))
CHECK_EVERY_DAYS = 0   # ежедневно, как ВБ-оферта (тексты снимает удалённый комп, Scrapfly не тратим)

OZON_DOCS = [
    {"key": "standard-terms", "title": "Договор поставки",
     "url": "https://docs.ozon.ru/legal/partners/b2b/standard-terms/"},
    {"key": "service-terms", "title": "Условия оказания услуг",
     "url": "https://docs.ozon.ru/legal/partners/b2b/service-terms/"},
    {"key": "contract-work-terms", "title": "Условия выполнения работ (подряд)",
     "url": "https://docs.ozon.ru/legal/partners/b2b/contract-work-terms/"},
    {"key": "safety-rules", "title": "Золотые правила безопасности",
     "url": "https://docs.ozon.ru/legal/partners/b2b/safety-rules/"},
]


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


def _days_since(date_str: str) -> int:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (datetime.now(MSK).date() - d).days
    except Exception:
        return 10 ** 6


def load_offers_from_sheet() -> dict:
    """Тексты оферт, снятые удалённым компом (настоящий Chrome, РФ-IP) — из вкладки
    ozon_offers таблицы «цены АС фарм». {key: text}. Scrapfly больше не нужен."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build as _build
    info = json.loads(os.environ["GSHEETS_SA_JSON"])
    sid = os.environ["PRICES_SHEET_ID"]
    cred = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = _build("sheets", "v4", credentials=cred, cache_discovery=False)
    vals = svc.spreadsheets().values().get(
        spreadsheetId=sid, range="ozon_offers!A2:D50").execute().get("values", [])
    out = {}
    for r in vals:
        if r and r[0].strip() and len(r) >= 4:
            out[r[0].strip()] = r[3]
    return out


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN'].strip()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def read_shared() -> tuple[dict, str | None]:
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
        "message": "ozon oferta state update",
        "content": base64.b64encode(
            json.dumps(data, ensure_ascii=False).encode("utf-8")).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=_gh_headers(), json=body, timeout=30)
    r.raise_for_status()


def _migrate(shared: dict) -> dict:
    """Старый одно-документный формат {hash,text} → новый {docs:{...}}."""
    if "docs" in shared:
        return shared
    docs = {}
    if shared.get("hash"):
        docs["standard-terms"] = {"hash": shared["hash"], "text": shared.get("text", "")}
    return {"date": shared.get("date", ""), "docs": docs}


def main() -> None:
    load_env()
    sys.path.insert(0, HERE)
    from sources import normalize
    from summarize import make_diff, summarize, fallback_summary
    import notify
    import html as _html

    DRY = os.environ.get("DRY_RUN") == "1"
    def _send(msg):
        if DRY:
            print("[DRY] в канал НЕ отправлено. Текст был бы:\n" + msg, flush=True)
        else:
            notify.send(msg)

    has_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))

    try:
        shared, sha = read_shared()
    except Exception as e:
        print(f"[WARN] не прочитал состояние: {e}", flush=True)
        return
    shared = _migrate(shared)
    docs_state = shared.get("docs", {})

    # самотроттлинг: проверяем раз в CHECK_EVERY_DAYS дней
    last = shared.get("date", "")
    if last and _days_since(last) < CHECK_EVERY_DAYS:
        print(f"[SKIP] Ozon: последняя проверка {last}, ещё не прошло {CHECK_EVERY_DAYS} дн — пропуск", flush=True)
        return

    try:
        offers = load_offers_from_sheet()
    except Exception as e:
        print(f"[WARN] не прочитал вкладку ozon_offers: {e}", flush=True)
        offers = {}

    changed = []       # (title, url, summary)
    fetched_any = False
    for d in OZON_DOCS:
        raw = offers.get(d["key"])
        if not raw or len(raw) < 800:
            print(f"[WARN] {d['key']}: нет свежего текста от компа — пропуск", flush=True)
            continue  # эталон этого дока не трогаем
        fetched_any = True
        text = normalize(raw)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        prev = docs_state.get(d["key"]) or {}
        prev_hash = prev.get("hash")

        if not prev_hash:
            # первая фиксация документа — эталон, без «изменения»
            docs_state[d["key"]] = {"hash": digest, "text": text}
            print(f"[OK] {d['key']}: эталон зафиксирован", flush=True)
            continue

        if digest == prev_hash:
            print(f"[OK] {d['key']}: без изменений", flush=True)
            continue

        # изменение
        diff_text = make_diff(prev.get("text", ""), text)
        try:
            summary = summarize("OZON", d["title"], diff_text) if has_llm else fallback_summary(diff_text)
        except Exception as e:
            print(f"[WARN] выжимка {d['key']}: {e}", flush=True)
            summary = fallback_summary(diff_text)
        docs_state[d["key"]] = {"hash": digest, "text": text}
        if summary is None:
            print(f"[INFO] {d['key']}: изменение незначимо", flush=True)
            continue
        changed.append((d["title"], d["url"], summary))

    if not fetched_any:
        print("[WARN] Ozon: ни один документ не получен — состояние не трогаю", flush=True)
        return

    # обновляем состояние (дата + эталоны) — один раз за прогон
    shared["date"] = today_msk()
    shared["docs"] = docs_state
    write_shared(shared, sha)

    # ОДНО сообщение в канал за прогон
    if not changed:
        _send(
            "🔵 Ozon проверен — изменений в договоре не обнаружено ✅\n"
            f"{now_msk_str()}"
        )
        print("[OK] Ozon: без изменений (статус отправлен)", flush=True)
        return

    lines = ["🔵 Ozon — <b>обнаружены изменения в документах:</b>", ""]
    for title, url, summary in changed:
        lines.append(f"<b>{_html.escape(title)}</b>")
        lines.append(_html.escape(summary))
        lines.append(f"🔗 {url}")
        lines.append("")
    lines.append(now_msk_str())
    _send("\n".join(lines).strip())
    print(f"[ALERT] Ozon: отправлено ({len(changed)} докум. изменилось)", flush=True)


if __name__ == "__main__":
    main()

