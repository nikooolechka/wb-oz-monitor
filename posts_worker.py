"""Мониторинг ПОСТОВ Telegram-каналов про маркетплейсы → ИИ-дайджест.

Раз в сутки обходит каналы (posts_sources), берёт вчерашние посты, прогоняет
каждый через классификатор (posts_classify — бесплатный Gemini, фолбэк Anthropic)
по профилю АС Фарм и собирает дайджест: только то, что прошло порог.

Состояние постов — data/posts_state.json.

РЕЖИМ: по умолчанию АВТОПУБЛИКАЦИЯ. POSTS_PREVIEW=1 — превью без отправки.

ЧЕСТНОСТЬ СТАТУСА (важно, правило владельца 2026-07-10): если посты за вчера
БЫЛИ, но классификатор не смог разобрать НИ ОДНОГО (кончились кредиты / лимит /
сбой мозга) — это НЕ «ничего полезного». В этом случае шлём fail-loud сообщение
(один раз в день) и РОНЯЕМ прогон (exit!=0), чтобы воркфлоу стал failure и на
дашборде карточка автоматически покраснела. Посты при этом НЕ помечаем
обработанными — разберём в следующий раз. «Ничего полезного» уходит ТОЛЬКО когда
мозг реально отработал и ничего не прошло порог.
"""
from __future__ import annotations

import os
import re
import json
import time
import tempfile
import traceback
from datetime import datetime, timezone, timedelta

import notify
import posts_sources as src
import posts_classify as clf

STATE_PATH = os.environ.get("POSTS_STATE_PATH", "data/posts_state.json")
WINDOW_HOURS = float(os.environ.get("POSTS_WINDOW_HOURS", "168"))
PREVIEW = os.environ.get("POSTS_PREVIEW") == "1"
MSK = timezone(timedelta(hours=3))
PACE_SEC = float(os.environ.get("POSTS_PACE_SEC", "4.5"))  # раздаём запросы к Gemini под минутный лимит free-тарифа
NOTHING_MSG = "Прогнал все каналы, сегодня ничего полезного🫡"

_ORDER = ["правила", "связка", "фишка", "кейс", "данные", "инструмент", "ресурс"]


def _load() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    dir_ = os.path.dirname(STATE_PATH) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def collect_new(state: dict) -> tuple[list[src.Post], dict]:
    """Возвращает (новые посты к разбору, обновлённое состояние last_id).

    Дайджест — ВСЕГДА строго за ВЧЕРА (одни завершённые сутки МСК). Состояние
    двигаем только по включённым постам.
    """
    today = datetime.now(MSK).date()
    cutoff = today - timedelta(days=2)  # окно догоняния: если прогон пропущен/опоздал (крон дропнулся),
    # вчерашние посты не проскочат — берём все НЕразобранные за 2 завершённых суток (дедуп по last_id не даст повторов)
    new_posts: list[src.Post] = []
    dbg_win = dbg_skip = 0; dbg_all = []  # диагностика: сколько в 2-дн окне и сколько отсечено указателем (уже разобраны)
    for ch in src.CHANNELS:
        try:
            posts = src.fetch_channel(ch)
        except Exception as e:
            print(f"[WARN] @{ch}: ошибка загрузки: {e}", flush=True)
            continue
        if not posts:
            continue
        last_id = state.get(ch)
        dbg_all += [p.dt.astimezone(MSK).date() for p in posts]
        in_win = [p for p in posts if cutoff <= p.dt.astimezone(MSK).date() < today]
        dbg_win += len(in_win)
        dbg_skip += len([p for p in in_win if last_id is not None and p.post_id <= last_id])
        cand = posts if last_id is None else [p for p in posts if p.post_id > last_id]
        eligible = [p for p in cand if cutoff <= p.dt.astimezone(MSK).date() < today]
        new_posts.extend(eligible)
        if eligible:
            state[ch] = max(p.post_id for p in eligible)
        elif last_id is not None:
            state[ch] = last_id
    _dr = f"{min(dbg_all)}..{max(dbg_all)}" if dbg_all else "нет"
    print(f"[DIAG] всего постов из каналов={len(dbg_all)} (даты {_dr}); в 2-дн окне={dbg_win}, отсечено указателем={dbg_skip}, к разбору={len(new_posts)}", flush=True)
    new_posts.sort(key=lambda p: p.dt)
    return new_posts, state


_DEDUP_STOP = set((
    "wildberries wildberry wb вб ozon озон маркетплейс маркетплейса маркетплейсов "
    "продавцов селлеров селлер продавец товаров товары новый новая новые для при "
    "как что это его наши нам себе теперь снова опять будет может если чтобы "
    "который которые тарифы тариф комиссия комиссии банк").split())


def _dedup(entries: list[dict]) -> list[dict]:
    """Схлопывает одну и ту же новость, взятую из разных каналов (сильное
    пересечение значимых слов заголовка+сути). Оставляет вариант с самым
    содержательным argument (длиннее/с цифрами)."""
    def toks(e):
        s = (e["res"].get("headline", "") + " " + e["res"].get("argument", "")[:140]).lower()
        return set(w for w in re.findall(r"[а-яёa-z0-9]+", s) if len(w) > 3 and w not in _DEDUP_STOP)
    kept: list[dict] = []
    kept_toks: list[set] = []
    for e in sorted(entries, key=lambda x: -len(x["res"].get("argument", ""))):  # сначала самые содержательные
        t = toks(e)
        if any(len(t & kt) >= 4 and len(t & kt) >= 0.5 * min(len(t), len(kt)) for kt in kept_toks if t and kt):
            continue  # та же новость — уже есть более содержательный вариант
        kept.append(e); kept_toks.append(t)
    kept.sort(key=lambda x: x["post"].dt)  # обратно по времени
    return kept


def build_digest(entries: list[dict]) -> list[str]:
    dates = sorted({e["post"].dt.astimezone(MSK).date() for e in entries})
    if len(dates) <= 1:
        per = (dates[0] if dates else datetime.now(MSK).date()).strftime("%d.%m")
    else:
        per = f"{dates[0].strftime('%d.%m')}–{dates[-1].strftime('%d.%m')}"
    header = f"🗞 <b>Дайджест за {per} · {len(entries)} тем</b>"

    def quote(i: int, e: dict) -> str:
        r, p = e["res"], e["post"]
        arg = _esc(r.get("argument", ""))
        if len(arg) > 700:
            arg = arg[:700].rstrip() + "…"
        b = (f"<b>{i}. {_esc(r.get('headline',''))}</b>\n"
             f"<i>@{p.channel} · {p.dt.astimezone(MSK).strftime('%d.%m')}</i>\n"
             f"{arg}\n"
             f"🎯 Нам: {_esc(r.get('apply',''))}")
        if p.links and r.get("category") in ("ресурс", "инструмент"):
            b += "\n🔗 " + _esc(" | ".join(p.links[:5]))
        return f"<blockquote expandable>{b}</blockquote>"

    chunks: list[str] = []
    buf = header
    for i, e in enumerate(entries, 1):
        q = quote(i, e)
        if len(buf) + len(q) + 1 > 4050:
            chunks.append(buf)
            buf = q
        else:
            buf = f"{buf}\n{q}" if buf else q
    if buf:
        chunks.append(buf)
    return chunks


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fail_cause(err: str) -> str:
    low = (err or "").lower()
    if any(k in low for k in ("credit balance", "quota", "resource_exhausted", "429", "too many requests")):
        return "кончились кредиты / упёрлись в лимит"
    return "мозг классификатора недоступен"


def run_once() -> None:
    state = _load()
    orig = dict(state)  # снимок ДО мутации — при сбое мозга не теряем посты
    first_run = not state

    # 🔴 ДЕДУП ДО СБОРА (критично для бюджета Scrapfly): если дайджест за сегодня
    # уже отправлен — выходим НЕ качая каналы. Иначе резервные кроны (10:00/10:30)
    # жгли бы кредиты ×3 в день впустую (сбор идёт раньше, чем проверка «уже слал»).
    if not PREVIEW and state.get("_digest_date") == datetime.now(MSK).date().isoformat():
        print("[INFO] дайджест за сегодня уже отправлен — выхожу ДО сбора каналов, "
              "кредиты Scrapfly не трогаю", flush=True)
        return

    # ── Прямой (бесплатный) путь жив? Проба на одном канале. Обход (Scrapfly)
    # включаем ТОЛЬКО если прямой лёг ШИРОКО, и не дольше 2 дней подряд; на 3-й
    # день — отруб обхода + алерт «чинить» (правило владелицы: 2 дня терпим —
    # канал не молчит; 3-й день сломан → тревога, лимит не жжём). ──────────────
    today_iso = datetime.now(MSK).date().isoformat()
    if PREVIEW:
        src.ALLOW_SCRAPFLY = False           # превью — только бесплатно, лимит не трогаем
    else:
        if src.probe_direct():
            src.ALLOW_SCRAPFLY = False        # прямой жив → бесплатный день
            state["_scrapfly_days"] = 0
        else:
            days = int(orig.get("_scrapfly_days", 0))
            if days >= 2:                     # это уже 3-й день сбоя → отруб + алерт
                msg = ("<b>Дайджест отвалился — прямой сбор t.me не работает 3-й день.</b>\n"
                       "Обход (Scrapfly) остановлен, чтобы не жечь лимит. "
                       "Николь, зайди пожалуйста — надо чинить прямой сбор.")
                if orig.get("_directdown_date") != today_iso:
                    notify.send(msg)
                    print("[ALERT] прямой t.me сломан 3-й день — алерт, обход отрублен", flush=True)
                fail = dict(orig); fail["_directdown_date"] = today_iso
                fail["_scrapfly_days"] = days
                _save(fail)
                raise SystemExit("direct t.me down 3-й день — обход отрублен")
            src.ALLOW_SCRAPFLY = True          # день 1 или 2 сбоя → обход разрешён
            state["_scrapfly_days"] = days + 1
            print(f"[WARN] прямой t.me лёг — день {days+1}/2 сбора через обход (Scrapfly)", flush=True)

    new_posts, state = collect_new(state)
    print(f"[INFO] новых постов к разбору: {len(new_posts)}", flush=True)

    entries: list[dict] = []
    ok_cnt = err_cnt = 0
    last_err = ""
    for p in new_posts:
        try:
            res = clf.classify(p.channel, p.text)
            ok_cnt += 1
        except Exception as e:
            err_cnt += 1
            last_err = str(e)
            print(f"[WARN] @{p.channel}/{p.post_id}: ошибка разбора: {e}", flush=True)
            continue
        tag = "KEEP" if res.get("keep") else "skip"
        print(f"  [{tag}] @{p.channel}/{p.post_id} {res.get('headline','')}", flush=True)
        if res.get("keep"):
            entries.append({"post": p, "res": res})
        time.sleep(PACE_SEC)  # не бомбим Gemini пачкой → не ловим 429 по минутному лимиту

    # МАССОВЫЙ СБОЙ МОЗГА: посты были, но НИ ОДИН не разобран → поломка, не «пусто».
    if new_posts and ok_cnt == 0 and err_cnt > 0:
        cause = _fail_cause(last_err)
        msg = (f"<b>Дайджест не собрался — {cause}.</b>\n"
               f"Посты за вчера есть ({len(new_posts)} шт), но разобрать не удалось. "
               f"Николь, зайди пожалуйста в сессию — починим.")
        today_iso = datetime.now(MSK).date().isoformat()
        already = orig.get("_brain_fail_date") == today_iso
        if PREVIEW:
            print("[PREVIEW] BRAIN FAIL — в группу ушло бы: " + msg, flush=True)
        else:
            if not already:
                notify.send(msg)
                print("[ALERT] массовый сбой разбора — fail-loud отправлен в группу", flush=True)
            else:
                print("[INFO] массовый сбой разбора — уже алертил сегодня, повтор не шлю", flush=True)
            # сохраняем ТОЛЬКО метку даты сбоя поверх ИСХОДНОГО состояния (посты не двигаем)
            fail_state = dict(orig)
            fail_state["_brain_fail_date"] = today_iso
            _save(fail_state)
        raise SystemExit("classification wholesale failure: посты есть, разобрать не смогли")

    today_iso = datetime.now(MSK).date().isoformat()
    if entries:
        before = len(entries)
        entries = _dedup(entries)
        if len(entries) < before:
            print(f"[DEDUP] схлопнул дубли новости: {before} → {len(entries)}", flush=True)
        chunks = build_digest(entries)
        if PREVIEW:
            print("\n" + "=" * 60 + "\nПРЕВЬЮ ДАЙДЖЕСТА (в группу НЕ отправлено):\n" + "=" * 60)
            for c in chunks:
                print(c)
        else:
            for c in chunks:
                notify.send(c)
            state["_digest_date"] = today_iso  # отметка: дайджест за сегодня отправлен
            print(f"[ALERT] дайджест отправлен в группу ({len(entries)} пунктов, {len(chunks)} сообщ.)", flush=True)
    else:
        already_today = state.get("_digest_date") == today_iso
        if PREVIEW:
            print("[PREVIEW] пусто — в группу ушло бы: " + NOTHING_MSG, flush=True)
        elif already_today:
            # резервный крон-прогон после уже отправленного дайджеста — молчим, не шлём ложное «пусто»
            print("[INFO] дайджест за сегодня уже отправлен — резервный прогон молчит", flush=True)
        elif not new_posts:
            # постов к разбору НЕ было (сбор дал 0) — НЕ шлём «прогнал все каналы, ничего полезного»:
            # это было бы враньё (ничего не прогоняли). 0 постов = сигнал проблемы сбора, ловится в логе/у меня, не в канал.
            print("[INFO] 0 постов к разбору — в канал НЕ шлём (нечего было прогонять)", flush=True)
        else:
            notify.send(NOTHING_MSG)
            print("[INFO] мозг отработал, ничего не прошло порог — статус-сообщение в группу", flush=True)

    if PREVIEW:
        print("[PREVIEW] состояние НЕ сохранено — превью, посты остаются", flush=True)
    else:
        state.pop("_brain_fail_date", None)  # успех — снимаем метку сбоя
        _save(state)
        if first_run:
            notify.send(
                "✅ <b>Мониторинг постов каналов запущен</b>\n"
                f"Слежу за {len(src.CHANNELS)} каналами."
            )


if __name__ == "__main__":
    try:
        run_once()
    except SystemExit:
        raise
    except Exception:
        print("[ERROR] сбой:\n" + traceback.format_exc(), flush=True)
        raise
