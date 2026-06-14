"""Выжимка изменений через Claude."""
from __future__ import annotations

import os
import difflib

import anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
MAX_DIFF_CHARS = 16000

_SYSTEM = (
    "Ты — ассистент селлера на маркетплейсах. Тебе дают изменения (diff) в "
    "оферте/правилах Wildberries или Ozon. Сделай короткую деловую выжимку на "
    "русском: что конкретно поменялось и чем это грозит продавцу. "
    "Игнорируй косметику. Если ничего важного не изменилось — ответь ровно: НЕЗНАЧИМО. "
    "Иначе — 2–6 пунктов списком, по делу."
)


def make_diff(old: str, new: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        lineterm="", n=2, fromfile="было", tofile="стало",
    )
    text = "\n".join(diff)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n…(diff обрезан)"
    return text


def fallback_summary(diff_text: str) -> str | None:
    added, removed = [], []
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            added.append(line[1:].strip())
        elif line.startswith("-"):
            removed.append(line[1:].strip())
    added = [x for x in added if len(x) > 3]
    removed = [x for x in removed if len(x) > 3]
    if not added and not removed:
        return None

    def block(title, items):
        items = items[:12]
        body = "\n".join(f"• {x[:300]}" for x in items)
        more = "\n…(и ещё изменения, см. документ)" if len(items) == 12 else ""
        return f"{title}\n{body}{more}" if body else ""

    parts = [p for p in (
        block("➖ Удалено/было:", removed),
        block("➕ Добавлено/стало:", added),
    ) if p]
    return ("⚠️ Документ изменился (авто-сводка без LLM):\n\n"
            + "\n\n".join(parts))


def summarize(platform: str, title: str, diff_text: str) -> str | None:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=MODEL,
        max_tokens=900,
        system=_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"Площадка: {platform}\nДокумент: {title}\n\n"
                f"Изменения (unified diff):\n{diff_text}"
            ),
        }],
    )
    out = "".join(b.text for b in msg.content if b.type == "text").strip()
    if not out or out.upper().startswith("НЕЗНАЧИМО"):
        return None
    return out
