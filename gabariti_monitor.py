"""Монитор габаритов товара в юнитках WB и Ozon → канал «АС Фарм изменения».

Раз в сутки читает Д/Ш/В из:
  - WB  «юнит-экономика»  (столбцы L/M/N, товар в A) — берём первое вхождение;
  - Ozon «юнитка NEW»     (столбцы L/M/N, товар в A).
Сравнивает с сохранённым снимком (data/gabariti_state.json). Если у товара
габариты в юнитке изменились (или впервые заданы) — шлёт в Telegram-канал
сообщение и НЕ трогает товарную матрицу (ждёт ручного «обновляю» через Claude).

Первый запуск только фиксирует базовый снимок + одно служебное сообщение,
без «портянки» по каждому товару.

Секреты/окружение:
  GSHEETS_SA_JSON     — JSON сервисного аккаунта (чтение таблиц)
  TELEGRAM_BOT_TOKEN  — бот @asfarm_changes_bot (как у монитора оферт)
  TELEGRAM_CHAT_ID    — группа «АС Фарм изменения»
  STATE_PATH          — путь к снимку (по умолч. data/gabariti_state.json)
"""
from __future__ import annotations

import os
import re
import json
import tempfile

import gspread
from google.oauth2.service_account import Credentials

import notify

STATE_PATH = os.environ.get("STATE_PATH", "data/gabariti_state.json")
# Очередь НОВЫХ товаров — НЕ в ТГ-канал, а сюда: Claude читает её в начале
# сессий АС Фарм и поднимает с владельцем «сверим габариты и добавим в матрицу».
NEW_PRODUCTS_PATH = os.environ.get("NEW_PRODUCTS_PATH", "data/gabariti_new_products.json")

# (метка платформы для сообщения, id таблицы, имя вкладки)
SOURCES = [
    ("ВБ", "1Uf4vxCEImkqDLJSOFSireJFX1srE_8H1QS3GfBr8RZw", "юнит-экономика"),
    # ПЕРЕЕЗД 2026-09-11: старая таблица 11ijV3JWhkyUxysv… больше НЕ используется.
    ("ОЗОН", "18PHOlKcIoXUFiw3ReXCDESzy-_TXQvcdFcVmJJUiCjs", "юнитка NEW"),
]

# МАТРИЦА (ОП АС Фарм): проверяем, что товар из юнитки заведён сюда (владелец
# «в юнитку завожу, в матрицу могу забыть»). Нет в матрице / нет эталона V → алерт.
MATRIX = ("1sHlFGSVB-7V8V4q6kvcTR1rrw19EaabIaOgrCHU0DHE", "МАТРИЦА")
_PLAT_WORDS = {"ozon", "озон", "оз", "wb", "вб", "ям", "дм", "yandex", "детмир"}

def _norm(s):
    return re.sub(r"[^a-z0-9а-яё]", "", (s or "").lower())

def read_matrix(gc):
    """Возвращает (arts, has_v): множество нормализованных артикулов матрицы и
    dict{норм.артикул: есть ли эталон габаритов в столбце V}. Столбцы ищем по
    ЗАГОЛОВКУ (матрица двигается — по буквам нельзя)."""
    sid, tab = MATRIX
    rows = gc.open_by_key(sid).worksheet(tab).get_all_values()
    cN = cV = None
    for r in rows[:4]:
        for j, h in enumerate(r):
            hs = (h or "").strip().lower()
            if hs == "артикул" and cN is None:
                cN = j
            if "габарит" in hs and cV is None:
                cV = j
    arts, has_v = set(), {}
    if cN is None:
        return arts, has_v
    for r in rows:
        cell = r[cN] if len(r) > cN else ""
        gab = (r[cV].strip() if cV is not None and len(r) > cV else "")
        for line in str(cell).split("\n"):
            toks = [t for t in line.split() if t.lower() not in _PLAT_WORDS]
            key = _norm(" ".join(toks))
            if len(key) >= 3:
                arts.add(key)
                has_v[key] = has_v.get(key, False) or bool(gab)
    return arts, has_v


def msg_not_in_matrix(art) -> str:
    return (f"🧩 Товар <b>{_esc(art)}</b> есть в юнитке, но НЕ заведён в МАТРИЦУ."
            "\nДобавь в матрицу (или напиши в Claude — заведу и сверю габариты).")

def msg_no_etalon(art) -> str:
    return (f"🧩 Товар <b>{_esc(art)}</b> в матрице БЕЗ эталона габаритов (столбец V)."
            "\nПроставь габариты (или напиши в Claude).")


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


def _client():
    info = json.loads(os.environ["GSHEETS_SA_JSON"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    return gspread.authorize(creds)


def read_unit(gc, sid, tab):
    """Возвращает (names, dims):
      names — все артикулы из колонки A (известные товары, в т.ч. без габаритов);
      dims  — {артикул: 'ДхШхВ'} только для полностью заполненных, первое вхождение.
    """
    ws = gc.open_by_key(sid).worksheet(tab)
    A = ws.col_values(1)
    L = ws.col_values(12)
    M = ws.col_values(13)
    N = ws.col_values(14)
    names, dims, seen = [], {}, set()
    for i, name in enumerate(A):
        name = (name or "").strip()
        if not name or name in ("ФОРМУЛЫ", "Название товара") or name in seen:
            continue
        seen.add(name)
        names.append(name)
        l = (L[i] if i < len(L) else "").strip()
        m = (M[i] if i < len(M) else "").strip()
        n = (N[i] if i < len(N) else "").strip()
        if l and m and n:
            dims[name] = f"{l}х{m}х{n}"
    return names, dims


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def msg_changed(platform, art, ref, new) -> str:
    return (f"📦 В юнитке {platform} поменялись габариты на <b>{_esc(art)}</b>."
            f"\nБыло {ref} → стало {new}."
            "\nЕсли изменение финальное — напиши в Claude и я обновлю товарную матрицу 👀")


def msg_reverted(platform, art) -> str:
    return (f"✌️ По артикулу <b>{_esc(art)}</b> (юнитка {platform}) "
            "габариты вернулись к прежним — матрицу не трогаю.")


def _load_new_products() -> list:
    try:
        with open(NEW_PRODUCTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_new_products(items: list) -> None:
    os.makedirs(os.path.dirname(NEW_PRODUCTS_PATH) or ".", exist_ok=True)
    with open(NEW_PRODUCTS_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def run_once() -> None:
    """Состояние state: {"known": [имена товаров], "dims": {key: {"ref","last"}}}.
      known — все товары, когда-либо виденные в юнитках (для детекта НОВОГО товара);
      dims.ref — эталон («как в матрице»), меняется при ручном апдейте матрицы;
      dims.last — что видели в прошлый прогон (чтобы не слать одно и то же).
    Поменялись/вернулись → ТГ-канал. НОВЫЙ товар → очередь NEW_PRODUCTS_PATH (ко мне).
    """
    state = _load()
    first_run = not state
    gc = _client()

    name_plat = {}     # имя -> {платформы, где встречен}
    cur_dims = {}      # 'платформа|имя' -> 'ДхШхВ'
    for platform, sid, tab in SOURCES:
        try:
            names, dims = read_unit(gc, sid, tab)
        except Exception as e:
            print(f"[WARN] {platform}/{tab}: ошибка чтения: {e}", flush=True)
            continue
        for n in names:
            name_plat.setdefault(n, set()).add(platform)
        for art, val in dims.items():
            cur_dims[f"{platform}|{art}"] = val

    if first_run:
        _save({"known": sorted(name_plat), "dims": {k: {"ref": v, "last": v} for k, v in cur_dims.items()}})
        print(f"[OK] baseline: товаров {len(name_plat)}, габаритов {len(cur_dims)} (без уведомления)", flush=True)
        return

    known = set(state.get("known", []))
    dims_state = state.get("dims", {})

    # 1) НОВЫЕ товары (имя не встречалось раньше) → очередь (ко мне) + АЛЕРТ в канал,
    #    но ТОЛЬКО если новый товар забыт в МАТРИЦЕ или без эталона V (владелец: «в юнитку
    #    завожу, в матрицу могу забыть»). Старое/мусор уже в known → не триггерит, лавины нет.
    new_names = sorted(n for n in name_plat if n not in known)
    if new_names:
        queue = _load_new_products()
        pending = {q["art"] for q in queue}
        try:
            marts, mhasv = read_matrix(gc)
        except Exception as e:
            marts, mhasv = None, {}
            print(f"[WARN] матрица не прочитана: {e}", flush=True)
        for n in new_names:
            label = " и ".join(sorted(name_plat[n]))
            dv = next((cur_dims[f"{p}|{n}"] for p in sorted(name_plat[n]) if f"{p}|{n}" in cur_dims), None)
            if n not in pending:
                queue.append({"art": n, "platform": label, "dims": dv})
            known.add(n)
            # зафиксируем габариты нового товара как эталон, чтобы не словить его же как «изменение»
            for p in name_plat[n]:
                k = f"{p}|{n}"
                if k in cur_dims:
                    dims_state[k] = {"ref": cur_dims[k], "last": cur_dims[k]}
            # алерт в канал — только если новый товар реально «забыт» в матрице
            if marts is not None:
                key = _norm(n)
                if key not in marts:
                    notify.send(msg_not_in_matrix(n))
                    print(f"[MATRIX-ALERT] {n}: нет в матрице", flush=True)
                elif not mhasv.get(key, False):
                    notify.send(msg_no_etalon(n))
                    print(f"[MATRIX-ALERT] {n}: в матрице без эталона V", flush=True)
            print(f"[NEW] новый товар: {n} ({label}) {dv or 'без габаритов'}", flush=True)
        _save_new_products(queue)

    # 2) поменялись / вернулись по СТАРЫМ товарам → ТГ-канал
    notified = 0
    new_set = set(new_names)
    for key, new in cur_dims.items():
        platform, art = key.split("|", 1)
        if art in new_set:
            continue  # уже учтён как новый товар
        rec = dims_state.get(key)
        if rec is None:
            # старый товар, габариты появились впервые — тихо берём за эталон, без спама
            dims_state[key] = {"ref": new, "last": new}
            continue
        ref, last = rec.get("ref"), rec.get("last")
        if new == last:
            continue
        if new == ref:
            notify.send(msg_reverted(platform, art))
            print(f"[ALERT] {platform} {art}: вернулись к {ref}", flush=True)
        else:
            notify.send(msg_changed(platform, art, ref, new))
            print(f"[ALERT] {platform} {art}: {ref} → {new}", flush=True)
        rec["last"] = new
        notified += 1

    _save({"known": sorted(known), "dims": dims_state})
    print(f"[OK] в канал отправлено: {notified}; новых товаров: {len(new_names)}", flush=True)


if __name__ == "__main__":
    run_once()
