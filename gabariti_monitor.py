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
import json
import tempfile

import gspread
from google.oauth2.service_account import Credentials

import notify

STATE_PATH = os.environ.get("STATE_PATH", "data/gabariti_state.json")

# (метка платформы для сообщения, id таблицы, имя вкладки)
SOURCES = [
    ("ВБ", "1Uf4vxCEImkqDLJSOFSireJFX1srE_8H1QS3GfBr8RZw", "юнит-экономика"),
    ("ОЗОН", "11ijV3JWhkyUxysv_BrfT7dH5-VP0dU98iBaxBD8sgjg", "юнитка NEW"),
]


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


def read_dims(gc, sid, tab) -> dict:
    """{артикул: 'ДхШхВ'} — первое вхождение, только полностью заполненные."""
    ws = gc.open_by_key(sid).worksheet(tab)
    A = ws.col_values(1)
    L = ws.col_values(12)
    M = ws.col_values(13)
    N = ws.col_values(14)
    out = {}
    for i, name in enumerate(A):
        name = (name or "").strip()
        if not name or name in ("ФОРМУЛЫ", "Название товара"):
            continue
        if name in out:
            continue  # первое вхождение
        l = (L[i] if i < len(L) else "").strip()
        m = (M[i] if i < len(M) else "").strip()
        n = (N[i] if i < len(N) else "").strip()
        if not (l and m and n):
            continue  # неполные габариты не трекаем
        out[name] = f"{l}х{m}х{n}"
    return out


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def msg_changed(platform, art, ref, new) -> str:
    head = f"📦 В юнитке {platform} поменялись габариты на <b>{_esc(art)}</b>."
    if ref:
        head += f"\nБыло {ref} → стало {new}."
    else:
        head += f"\nЗаданы габариты: {new}."
    head += "\nЕсли изменение финальное — напиши в Claude и я обновлю товарную матрицу 👀"
    return head


def msg_reverted(platform, art) -> str:
    return (f"✌️ По артикулу <b>{_esc(art)}</b> (юнитка {platform}) "
            "габариты вернулись к прежним — матрицу не трогаю.")


def run_once() -> None:
    """Состояние: {key: {"ref": эталон, "last": последнее видимое}}.
    ref — стабильное значение (то, что считаем «в матрице»); меняется при ручном
    подтверждении (обновлении матрицы). last — что видели в прошлый прогон,
    чтобы не слать одно и то же каждый день.
    """
    state = _load()
    first_run = not state
    gc = _client()

    current = {}
    for platform, sid, tab in SOURCES:
        try:
            dims = read_dims(gc, sid, tab)
        except Exception as e:
            print(f"[WARN] {platform}/{tab}: ошибка чтения: {e}", flush=True)
            continue
        for art, val in dims.items():
            current[f"{platform}|{art}"] = val

    if first_run:
        # тихий старт: фиксируем базу без поста в группу, чтобы не спамить
        _save({k: {"ref": v, "last": v} for k, v in current.items()})
        print(f"[OK] baseline зафиксирован: {len(current)} позиций (без уведомления)", flush=True)
        return

    notified = 0
    for key, new in current.items():
        rec = state.get(key)
        platform, art = key.split("|", 1)
        if rec is None:
            # новый артикул с габаритами — уведомляем (заданы), эталоном считаем новое
            notify.send(msg_changed(platform, art, None, new))
            state[key] = {"ref": new, "last": new}
            print(f"[ALERT] {platform} {art}: заданы {new}", flush=True)
            notified += 1
            continue
        ref, last = rec.get("ref"), rec.get("last")
        if new == last:
            continue  # с прошлого прогона ничего нового — молчим
        # появилось новое событие изменения с прошлой проверки
        if new == ref:
            # вернули к эталону (к тому, что было/в матрице)
            notify.send(msg_reverted(platform, art))
            print(f"[ALERT] {platform} {art}: вернулись к {ref}", flush=True)
        else:
            notify.send(msg_changed(platform, art, ref, new))
            print(f"[ALERT] {platform} {art}: {ref} → {new}", flush=True)
        rec["last"] = new  # ref не трогаем — он сменится при ручном апдейте матрицы
        notified += 1

    _save(state)
    print(f"[OK] отправлено уведомлений: {notified}" if notified else "[OK] изменений габаритов нет", flush=True)


if __name__ == "__main__":
    run_once()
