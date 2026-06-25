"""Мониторинг ПОСТОВ Telegram-каналов про маркетплейсы → ИИ-дайджест.

Раз в сутки обходит каналы (posts_sources), берёт новые посты (id больше
сохранённого), прогоняет каждый через Haiku (posts_classify) по профилю
АС Фарм и собирает дайджест: только то, что прошло порог, с аргументами,
цифрами и живыми ссылками. БЕЗ лимита числа — выдаёт всё, что прошло.

Состояние постов — отдельный файл (data/posts_state.json), чтобы не
конфликтовать с состоянием оферты (data/state.json).

РЕЖИМ: по умолчанию АВТОПУБЛИКАЦИЯ — дайджест уходит в группу и состояние
двигается (посты помечаются обработанными). Для ручного превью без отправки —
POSTS_PREVIEW=1 (печатает в stdout, в группу не шлёт, состояние не трогает).
Облачный прогон работает автономно (флага нет → публикует); превью для
доводки запускаем локально с POSTS_PREVIEW=1.
"""
from __future__ import annotations

import os
import json
import tempfile
import traceback
from datetime import datetime, timezone, timedelta

import anthropic

import notify
import posts_sources as src
import posts_classify as clf

STATE_PATH = os.environ.get("POSTS_STATE_PATH", "data/posts_state.json")
WINDOW_HOURS = float(os.environ.get("POSTS_WINDOW_HOURS", "168"))  # первый прогон = неделя, далее дедуп по id
PREVIEW = os.environ.get("POSTS_PREVIEW") == "1"  # превью: не публиковать и не двигать состояние (по умолчанию — автопубликация)
MSK = timezone(timedelta(hours=3))
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

    Дайджест — ВСЕГДА строго за ВЧЕРА (одни завершённые календарные сутки МСК),
    и для новых каналов тоже — никаких многодневных «периодов»/backfill. Берём
    посты, у которых дата (МСК) == вчера. Сегодняшние ждут завтрашнего дайджеста.
    Состояние двигаем только по включённым постам.
    """
    today = datetime.now(MSK).date()
    yesterday = today - timedelta(days=1)
    new_posts: list[src.Post] = []
    for ch in src.CHANNELS:
        try:
            posts = src.fetch_channel(ch)
        except Exception as e:
            print(f"[WARN] @{ch}: ошибка загрузки: {e}", flush=True)
            continue
        if not posts:
            continue
        last_id = state.get(ch)
        # дедуп: уже виденные посты не берём (для новых каналов last_id нет)
        cand = posts if last_id is None else [p for p in posts if p.post_id > last_id]
        # ТОЛЬКО вчерашние сутки (МСК) — и для новых каналов тоже
        eligible = [p for p in cand if p.dt.astimezone(MSK).date() == yesterday]
        new_posts.extend(eligible)
        if eligible:
            state[ch] = max(p.post_id for p in eligible)
        elif last_id is not None:
            state[ch] = last_id  # сегодняшние/будущие посты ждут своего дня
    new_posts.sort(key=lambda p: p.dt)
    return new_posts, state


def build_digest(entries: list[dict]) -> list[str]:
    """Новый вид: заголовок «🗞 Дайджест за DD.MM · N тем» и сразу нумерованные
    РАЗВОРАЧИВАЮЩИЕСЯ цитаты (<blockquote expandable>) — каждая свёрнута, читатель
    раскрывает только интересные. Возвращает список сообщений (упаковка по максимуму,
    цитаты не рвём; потолок Telegram 4096)."""
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


def run_once() -> None:
    state = _load()
    first_run = not state
    new_posts, state = collect_new(state)
    print(f"[INFO] новых постов к разбору: {len(new_posts)}", flush=True)

    entries: list[dict] = []
    if new_posts:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())
        for p in new_posts:
            try:
                res = clf.classify(client, p.channel, p.text)
            except Exception as e:
                print(f"[WARN] @{p.channel}/{p.post_id}: ошибка разбора: {e}", flush=True)
                continue
            tag = "KEEP" if res.get("keep") else "skip"
            print(f"  [{tag}] @{p.channel}/{p.post_id} {res.get('headline','')}", flush=True)
            if res.get("keep"):
                entries.append({"post": p, "res": res})

    if entries:
        chunks = build_digest(entries)
        if PREVIEW:
            print("\n" + "=" * 60 + "\nПРЕВЬЮ ДАЙДЖЕСТА (в группу НЕ отправлено):\n" + "=" * 60)
            for c in chunks:
                print(c)
        else:
            for c in chunks:
                notify.send(c)
            print(f"[ALERT] дайджест отправлен в группу ({len(entries)} пунктов, {len(chunks)} сообщ.)", flush=True)
    else:
        if PREVIEW:
            print("[PREVIEW] пусто — в группу ушло бы: " + NOTHING_MSG, flush=True)
        else:
            notify.send(NOTHING_MSG)
            print("[INFO] ничего не прошло порог — отправил статус-сообщение в группу", flush=True)

    # Состояние двигаем (помечаем посты обработанными) ТОЛЬКО при реальной публикации,
    # иначе превью «съело» бы посты и при публикации их бы уже не было.
    if PREVIEW:
        print("[PREVIEW] состояние НЕ сохранено — превью, посты остаются", flush=True)
    else:
        _save(state)
        if first_run:
            notify.send(
                "✅ <b>Мониторинг постов каналов запущен</b>\n"
                f"Слежу за {len(src.CHANNELS)} каналами."
            )


if __name__ == "__main__":
    try:
        run_once()
    except Exception:
        print("[ERROR] сбой:\n" + traceback.format_exc(), flush=True)
        raise
