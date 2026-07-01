"""Монитор габаритов КАРТОЧЕК Ozon → канал «АС Фарм изменения».

Ozon на приёмке сам перемеряет ОВХ и переписывает габариты В КАРТОЧКУ
(подтверждено справкой Ozon: новые значения вносятся в карточку, править их
в ЛК уже нельзя). Значит изменение габаритов/объёма в карточке = Ozon
перемерил. Ежедневно тянем Д/Ш/В из Ozon Seller API, считаем объём (л),
сравниваем со снимком (data/oz_gabariti_state.json).

Изменилось:
  объём ВЫРОС  → Ozon завысил → переплата по логистике → оспаривать 🍿
  объём УПАЛ   → пересчитал в меньшую (нам в плюс), но могут перемерить снова 🤲
  вернулось к базе → ✌️
Первый запуск только фиксирует базу, без сообщений.

Секреты: OZON_CLIENT_ID, OZON_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
STATE_PATH (по умолч. data/oz_gabariti_state.json), MIN_DELTA_L (шум, 0.05).
"""
from __future__ import annotations

import os
import json
import tempfile

import requests

import notify

STATE_PATH = os.environ.get("STATE_PATH", "data/oz_gabariti_state.json")
CLIENT_ID = os.environ["OZON_CLIENT_ID"].strip()
API_KEY = os.environ["OZON_API_KEY"].strip()
BASE = "https://api-seller.ozon.ru"
HEAD = {"Client-Id": CLIENT_ID, "Api-Key": API_KEY, "Content-Type": "application/json"}
# ниже этого расхождения (л) считаем округлением стороны и НЕ трубим
MIN_DELTA_L = float(os.environ.get("MIN_DELTA_L", "0.05"))


def _post(path, body):
    r = requests.post(BASE + path, headers=HEAD, json=body, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_cards():
    """offer_id -> {'dims': 'ДxШxВ' (мм), 'vol': литры}. Только видимые карточки."""
    out, last = {}, ""
    while True:
        d = _post("/v4/product/info/attributes",
                  {"filter": {"visibility": "VISIBLE"}, "limit": 100, "last_id": last})
        items = d.get("result") or []
        for it in items:
            off = (it.get("offer_id") or "").strip()
            dp, w, h = it.get("depth"), it.get("width"), it.get("height")
            if off and dp and w and h:
                out[off] = {"dims": f"{dp}x{w}x{h}", "vol": round(dp * w * h / 1_000_000, 3)}
        last = d.get("last_id", "")
        if not last or len(items) < 100:
            break
    return out


def _load():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    d = os.path.dirname(STATE_PATH) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _l(v):
    return f"{v:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def msg_up(art, old, new):
    return (f"🍿 <b>Пупупу — OZON поменял после обмера габариты {_esc(art)}</b>\n"
            f"с {old['dims']} мм ({_l(old['vol'])} л) на {new['dims']} мм ({_l(new['vol'])} л).\n"
            f"Объём вырос — надо запускать оспаривание 🍿")


def msg_down(art, old, new):
    return (f"🤲 <b>OZON поменял после обмера габариты {_esc(art)}</b>\n"
            f"с {old['dims']} мм ({_l(old['vol'])} л) на {new['dims']} мм ({_l(new['vol'])} л).\n"
            f"Нам повезло, пересчитал в меньшую сторону — но потом могут померить повторно, всё в ваших руках 🤲")


def msg_revert(art, ref):
    return (f"✌️ <b>OZON вернул габариты {_esc(art)}</b> к прежним "
            f"{ref['dims']} мм ({_l(ref['vol'])} л) — всё как надо.")


def run_once():
    state = _load()
    first = not state
    cards = fetch_cards()

    if first:
        _save({"dims": {k: {"ref": v, "last": v} for k, v in cards.items()}})
        print(f"[OK] baseline: {len(cards)} карточек, без уведомлений", flush=True)
        return

    dims_state = state.get("dims", {})
    notified = 0
    for off, cur in cards.items():
        rec = dims_state.get(off)
        if rec is None:
            dims_state[off] = {"ref": cur, "last": cur}   # новая карточка — тихо в базу
            continue
        ref, last = rec.get("ref"), rec.get("last")
        if not last or cur["dims"] == last.get("dims"):
            continue
        if abs(cur["vol"] - last.get("vol", cur["vol"])) < MIN_DELTA_L:
            rec["last"] = cur   # мелочь (округление стороны) — молча подвинем базу
            continue
        if ref and cur["dims"] == ref.get("dims"):
            notify.send(msg_revert(off, ref))
            print(f"[ALERT] {off}: вернулись к {ref['dims']}", flush=True)
        elif cur["vol"] > last.get("vol", 0):
            notify.send(msg_up(off, last, cur))
            print(f"[ALERT] {off}: {last['dims']} -> {cur['dims']} (вверх)", flush=True)
        else:
            notify.send(msg_down(off, last, cur))
            print(f"[ALERT] {off}: {last['dims']} -> {cur['dims']} (вниз)", flush=True)
        rec["last"] = cur
        notified += 1

    _save({"dims": dims_state})
    print(f"[OK] отправлено в канал: {notified}", flush=True)


if __name__ == "__main__":
    run_once()
