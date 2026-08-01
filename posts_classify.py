"""Оценка поста по профилю интересов АС Фарм — мозг БЕСПЛАТНЫЙ (Gemini),
с фолбэком на Anthropic. Раньше был только Claude Haiku (платные кредиты) —
2026-07-10 кредиты кончились и дайджест молча слал «ничего полезного»; перевели
на бесплатный Gemini, чтобы не зависеть от предоплаты. classify() при сбое мозга
БРОСАЕТ исключение (не глотает) — чтобы posts_worker увидел массовый сбой и
не выдал ложное «ничего полезного».

Ссылки берём из распарсенного поста (posts_sources), не из ответа модели.
"""
from __future__ import annotations

import os
import re
import ssl
import json
import time
import urllib.request
import urllib.error

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
# Отдельный ключ дайджеста (свой free-лимит, НЕ делит с постером → почти всегда
# ИИ-классификация, а не грубый keyword-фолбэк → реклама не проскакивает).
# Фолбэк на общий GEMINI_KEY, если отдельный не задан.
GEMINI_KEY = (os.environ.get("GEMINI_KEY_DIGEST")
              or os.environ.get("GEMINI_KEY", "")).strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")  # 2.0-flash Google лишил free-tier (limit:0) 2026-07 → latest

_CTX = ssl.create_default_context()
try:
    _CTX.check_hostname = False
    _CTX.verify_mode = ssl.CERT_NONE  # на маке бывает CERTIFICATE_VERIFY_FAILED; в CI не мешает
except Exception:
    pass

# Профиль релевантности — главный конфиг. Правится по результатам калибровки.
_SYSTEM = """Ты — аналитик-ассистент продавца «АС Фарм» на маркетплейсах. Тебе дают ОДИН пост из Telegram-канала про маркетплейсы. Реши, есть ли в нём что-то реально полезное для нас, и если да — извлеки суть.

ПЛОЩАДКИ: Wildberries, Ozon, Яндекс Маркет, Детский мир.

РЕАЛЬНЫЕ ТОВАРЫ АС ФАРМ (только эти, ничего не выдумывай сверх списка):
- Дентал — гигиенические САЛФЕТКИ для зубов/полости рта (число в названии = кол-во салфеток в коробке; есть со вкусами). Это НЕ таблетки, НЕ паста, НЕ наборы.
- Ирригаторы — ЖИДКОСТЬ (концентрат) для ирригатора, 2-в-1 как ополаскиватель для рта (не сам прибор).
- Крио-салфетки и крио-гель — расходники для косметической процедуры КРИОЛИПОЛИЗА (заморозка жира, тело/бьюти).
- 18+ спреи для минета: OralLubrikant (черника), Oral Cherry (вишня).
- Оптика-спрей — для чистки стёкол очков.
- Детская зубная паста — для молочных зубов (ниша мамы-дети).
НЕ продаём: спрей для полости рта (списание), товары для животных (списаны).

НИШИ для переносимых фишек: гигиена полости рта, 18+ интим, бьюти/косметология (процедуры), оптика, мамы-дети.

ЧТО ЛОВИМ (со склонностью к включению, но совет должен быть ДЕЛЬНЫМ и применимым, а не общей водой):
- рабочие связки (реклама/SEO/выкуп), гипотезы под тест, фишки карточек/логистики/продвижения;
- изменения правил/комиссий/тарифов МП, новые инструменты/сервисы;
- кейсы с конкретными цифрами, ИЗ КОТОРЫХ ПОНЯТНО, ЧТО ПОВТОРИТЬ;
- ПОЛЕЗНЫЕ РЕСУРСЫ: только если ресурс САМ по себе даёт пользу (готовая таблица с цифрами/формулами, чек-лист с конкретикой). Простое упоминание «вот калькулятор/сервис» со ссылкой — это НЕ ресурс, а реклама.

ЧТО ОТСЕКАЕМ (keep=false), будь строг:
- ВНЕШНЯЯ и блогерская реклама сейчас НЕ актуальна для нас: рилсы, бартер с блогерами, инфлюенсеры, платный внешний трафик, «аудит карточки перед внешкой» — НЕ брать ни кейсы, ни советы про это, даже если совет дельный. (Бесплатные доп. площадки размещения вроде Яндекс Ритма — это НЕ блогерская реклама, их брать можно.)
- посты, где «совет» по сути ПРИМАНКА к платному сервису/курсу/практикуму — НЕ брать;
- посты, которые по сути ПРОДВИГАЮТ один внешний сервис/сайт/калькулятор/бота (даже бесплатный) без конкретной маркетплейс-специфичной пользы — это РЕКЛАМА, keep=false. Пример: «вот калькулятор юнит-экономики» со ссылкой, без цифр и без привязки к площадке — реклама, НЕ брать;
- абстрактную бизнес-философию, мотивацию, «бизнес-путь»;
- пустую болтовню/шутки/поздравления, потребительские посты для покупателей, оффтоп, повтор новостей без сути.
Прежде чем оставить — перепроверь: совет реально дельный и его можно применить? Если нет — keep=false.

Верни СТРОГО JSON, без markdown, по схеме:
{"keep": true|false,
 "category": "правила|фишка|связка|кейс|ресурс|инструмент|данные",
 "headline": "короткий заголовок сути (до 90 симв)",
 "argument": "СУТЬ С КОНКРЕТНЫМИ ЦИФРАМИ из поста: что именно и почему это работает. Без воды. 1-3 предложения.",
 "apply": "как применить нам. Привязывай к реальным товарам выше ТОЛЬКО если это уместно; НЕ перечисляй все товары списком ради объёма. Если непонятно к какому товару — пиши общо про нашу нишу и НЕ выдумывай товары/форматы/наборы. 1 короткое предложение."}
Если keep=false — остальные поля можно оставить пустыми. Никогда не выдумывай цифры, которых нет в посте."""

_EMOJI = {
    "правила": "📋", "фишка": "🎯", "связка": "🔗", "кейс": "📊",
    "ресурс": "🧰", "инструмент": "🛠", "данные": "📈",
}


def _extract_json(s: str) -> dict:
    s = s.strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return {"keep": False}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"keep": False}


def _gemini(user: str) -> str:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    body = {
        "systemInstruction": {"parts": [{"text": _SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        # gemini-flash-lite-latest — единственная с реальным free-лимитом (2026-08):
        # flash-latest/2.0-flash упираются в 429 почти на каждом запросе → всё
        # валилось на keyword-фолбэк (шаблонный совет всем). flash-lite НЕ «думает»
        # → thinkingConfig НЕ передавать (даёт 400 Bad Request), он тут и не нужен.
        "generationConfig": {"temperature": 0, "maxOutputTokens": 1200,
                             "responseMimeType": "application/json"},
    }
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(4):  # ретрай на минутный лимит (429) / временную недоступность (503)
        req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
                d = json.loads(r.read().decode())
            return d["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 503) and attempt < 3:
                time.sleep(20 * (attempt + 1))  # 20/40/60с — переждать минутное окно
                continue
            raise
    raise last


# Фолбэк по ключевым словам — работает БЕЗ ИИ. Чтобы дайджест НИКОГДА не зависел от
# лимита/кредитов Gemini и не слал ложное «ничего полезного» (2026-07-11: Gemini выел
# дневной лимит → 429 на каждом посте → снова ложное «пусто»). Грубее ИИ, но всегда живой.
_KW_SKIP = ("розыгрыш", "конкурс", "вебинар", "практикум", "марафон", "инфлюенс", "блогер",
            "бартер", "рилс", "reels", "подписывайт", "промокод на курс", "#реклама",
            "с днём", "поздравля", "доброе утро")
_KW_KEEP = ("комисси", "тариф", "логистик", "удержани", "штраф", "регламент", "правил",
            "приёмк", "возврат", "продвижен", "реклам", "ставк", "автобиддер", "биддер",
            "выкуп", "органик", "seo", "сео", "карточк", "инфографик", "воронк", "конверс",
            "оборачива", "остатк", "поставк", "демпинг", "акци", "буст", "озон", "ozon",
            "wildberries", "вайлдберриз", "маркет", "дрр", "рублей", "₽")


def _keyword_classify(channel: str, text: str) -> dict:
    low = text.lower()
    if any(k in low for k in _KW_SKIP):
        return {"keep": False}
    if sum(1 for k in _KW_KEEP if k in low) < 3:  # порог, чтобы не тащить болтовню
        return {"keep": False}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    cat = "правила" if any(k in low for k in ("комисси", "тариф", "правил", "штраф", "удержани", "регламент")) else "фишка"
    return {"keep": True, "category": cat,
            "headline": (lines[0] if lines else text[:90])[:90],
            "argument": " ".join(lines[:4])[:500],
            "apply": "проверить применимость к нашим товарам", "_kw": True}


def classify(channel: str, text: str, client=None) -> dict:
    """Разбор поста. keep=False — пропустить. ИИ недоступен (лимит/кредиты/нет ключа) →
    НЕ падаем и НЕ врём «пусто», а разбираем по ключевым словам (фолбэк)."""
    if len(text.strip()) < 25:  # совсем короткие (мемы/реакции) не гоняем
        return {"keep": False}
    user = f"Канал: @{channel}\n\nПост:\n{text[:6000]}"
    try:
        if GEMINI_KEY:
            out = _gemini(user)
        elif os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic
            client = client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())
            msg = client.messages.create(model=MODEL, max_tokens=500, system=_SYSTEM,
                                         messages=[{"role": "user", "content": user}])
            out = "".join(b.text for b in msg.content if b.type == "text")
        else:
            raise RuntimeError("нет LLM-ключа")
        res = _extract_json(out)
        res["keep"] = bool(res.get("keep"))
        return res
    except Exception as e:
        print(f"[KW-FALLBACK] @{channel}: ИИ недоступен ({str(e)[:50]}) → ключевые слова", flush=True)
        return _keyword_classify(channel, text)


def emoji(category: str) -> str:
    return _EMOJI.get(category, "•")
