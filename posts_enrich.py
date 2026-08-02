"""Обогащение советов дайджеста НАШИМИ реальными данными (on-demand, свежие,
без хранения). v1 — цены (таблица «Цены АС Фарм»); дальше добавляются остатки,
отзывы/рейтинг, ДРР по мере проверки источников.

Идея: дайджест уже собран (keep + первичный apply). Для оставленных тем, где
уместно, точечно тянем наши цифры и переписываем apply предметно
(«у нас Dental20 470₽ → +2 п.п. = −9₽»). Жёсткий таймаут — если не успели,
оставляем первичный совет, дайджест НЕ задерживается.
"""
import os, json, time, ssl, urllib.request

PRICES_SHEET = "1fIXDZIwbRwPXUNoqT3sGoYxfmLl4fbN7TJ5YVt4_DxM"
_CTX = ssl.create_default_context()
if os.environ.get("INSECURE_SSL") == "1":
    _CTX.check_hostname = False; _CTX.verify_mode = ssl.CERT_NONE


def _sheets():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    sa = json.loads(os.environ["GSHEETS_SA_JSON"])
    cred = Credentials.from_service_account_info(sa, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    return build("sheets", "v4", credentials=cred, cache_discovery=False)


def our_prices() -> dict:
    """Наши текущие витринные цены по товарам (свежие, 1 чтение за прогон).
    Возврат: {артикул_норм: {"wb_do":..,"wb_spp":..,"oz_tek":..,"oz_bank":..,"oz_card":..}}"""
    svc = _sheets()
    g = svc.spreadsheets().values().batchGet(spreadsheetId=PRICES_SHEET,
        ranges=["Лист1!B2:E40", "Лист1!H2:L40"]).execute()["valueRanges"]
    def norm(s): return "".join(ch for ch in str(s).lower() if ch.isalnum())
    out = {}
    for row in g[0].get("values", []):        # WB: B арт, C до СПП, D с СПП, E кошелёк
        if row and row[0]:
            out.setdefault(norm(row[0]), {}).update(
                wb_do=row[1] if len(row) > 1 else "", wb_spp=row[2] if len(row) > 2 else "")
    for row in g[1].get("values", []):        # OZ: H арт, J текущая, K без карты, L с картой
        if row and row[0]:
            out.setdefault(norm(row[0]), {}).update(
                oz_tek=row[2] if len(row) > 2 else "", oz_bank=row[3] if len(row) > 3 else "",
                oz_card=row[4] if len(row) > 4 else "")
    return out


# --- Обогащение через ИИ (2-й проход, только по оставленным темам) ---
_ENRICH_SYS = """Ты дорабатываешь совет для продавца «АС Фарм» реальными цифрами компании.
Дают: пост, текущий (общий) совет, и НАШИ актуальные данные (цены по товарам).
Задача: если данные РЕЛЕВАНТНЫ теме поста — перепиши совет ПРЕДМЕТНО с цифрами:
назови КОНКРЕТНЫЕ наши товары, которых касается тема, посчитай эффект в рублях/%,
дай точное действие. Если данные к теме НЕ относятся (пост про чистую механику без
ценового/товарного угла) — верни совет БЕЗ выдумок, можно прежний.
Не выдумывай числа, которых нет в данных. Верни СТРОГО JSON: {"apply":"..."}. 2-4 предложения."""


def _gemini_enrich(post, cur_apply, data):
    key = (os.environ.get("GEMINI_KEY_DIGEST") or os.environ.get("GEMINI_KEY", "")).strip()
    if not key:
        return None
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    user = (f"ПОСТ: {post[:1200]}\n\nТЕКУЩИЙ СОВЕТ: {cur_apply}\n\n"
            f"НАШИ ДАННЫЕ (цены, ₽): {json.dumps(data, ensure_ascii=False)}")
    body = {"systemInstruction": {"parts": [{"text": _ENRICH_SYS}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 700,
                                 "responseMimeType": "application/json"}}
    data_b = json.dumps(body).encode()
    for _ in range(3):
        try:
            req = urllib.request.Request(url, data=data_b, headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=40, context=_CTX) as r:
                j = json.loads(r.read())
            txt = j["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(txt).get("apply")
        except Exception:
            time.sleep(1)
    return None


def enrich(entries: list, budget_sec: float = 120) -> None:
    """Дорабатывает apply у оставленных тем нашими цифрами. In-place. С таймаутом:
    как только бюджет исчерпан — остальные оставляем с первичным советом."""
    try:
        prices = our_prices()      # 1 свежее чтение цен за прогон (не храним)
    except Exception as e:
        print(f"[ENRICH] цены не прочитал ({e}) — оставляю первичные советы", flush=True)
        return
    t0 = time.time(); done = 0
    for e in entries:
        if time.time() - t0 > budget_sec:
            print(f"[ENRICH] бюджет {budget_sec}с исчерпан на {done}/{len(entries)} — остальные с первичным советом", flush=True)
            break
        post = e["post"].text
        new = _gemini_enrich(post, e["res"].get("apply", ""), prices)
        if new and len(new) > 10:
            e["res"]["apply"] = new
            done += 1
    print(f"[ENRICH] обогащено советов: {done}/{len(entries)}", flush=True)
