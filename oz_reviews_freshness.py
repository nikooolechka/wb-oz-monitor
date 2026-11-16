#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сторож + САМОВОССТАНОВЛЕНИЕ сбора отзывов Ozon (браузер на af-bot).

РАНЬШЕ: пассивно алертил «Николь, зайди — перезапущу» (вечный Startup-цикл на ПК
тихо умирал → отзывы вставали каждую неделю). ТЕПЕРЬ (2026-11-16): сторож сам
ЕЖЕДНЕВНО перезапускает одноразовый сбор на af-bot через пульт (колонка C), а
тревожит владельца ТОЛЬКО если авто-перезапуск не помогает ≥ALERT_DAYS (ПК реально
недоступен). Сбор пишет прямо в reviews_ozon (дедуп по id) — повторный запуск безопасен.

Кроном/n8n ежедневно. Секреты: GSHEETS_SA_JSON, TELEGRAM_* (в notify)."""
import os, json, base64
from datetime import datetime, timedelta, timezone
import notify
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEET = "1Gz0zU-fT34Tr3LG-WSMZFVy5sgAFgjyC880_79S3Wms"
OZ_TAB = "reviews_ozon"
CMD_CELL = "Лист1!C2"      # пульт af-bot: команда
HB_CELL = "Лист1!C4"       # пульт af-bot: heartbeat («alive <ts>»)
STALE_DAYS = int(os.environ.get("OZ_REV_STALE_DAYS", "2"))    # архив не растёт > N дней = ещё не критично, но триггерим
ALERT_DAYS = int(os.environ.get("OZ_REV_ALERT_DAYS", "6"))    # авто-перезапуск не помог > N дней = тревога (ПК недоступен)
STATE = "data/oz_reviews_freshness_state.json"
DRY = os.environ.get("DRY") == "1"
BUCKET = "https://storage.yandexcloud.net/asfarm-analiz-pc/oz_reviews_scrape.py"

# PowerShell одноразового запуска сбора на af-bot (как ручной _revrun): чистим chrome, тянем свежий скрипт, гоним детачем.
LAUNCH_PS = (
    '$py="$env:LOCALAPPDATA\\Programs\\Python\\Python312\\python.exe"; $d="$env:USERPROFILE\\asfarm"; '
    'taskkill /f /im chrome.exe 2>$null | Out-Null; taskkill /f /im chromedriver.exe 2>$null | Out-Null; Start-Sleep 1; '
    'Invoke-WebRequest "' + BUCKET + '?t=$(Get-Random)" -OutFile "$d\\oz_reviews_scrape.py"; '
    'Start-Process -FilePath $py -ArgumentList "$d\\oz_reviews_scrape.py" '
    '-RedirectStandardOutput "$d\\oz_reviews_run.log" -RedirectStandardError "$d\\oz_reviews_err.log" -WindowStyle Hidden'
)


def _svc(write=False):
    sa = json.loads(os.environ["GSHEETS_SA_JSON"])
    scope = "https://www.googleapis.com/auth/spreadsheets" if write else "https://www.googleapis.com/auth/spreadsheets.readonly"
    cred = Credentials.from_service_account_info(sa, scopes=[scope])
    return build("sheets", "v4", credentials=cred, cache_discovery=False)


def latest_date(svc):
    rows = svc.spreadsheets().values().get(spreadsheetId=SHEET, range=f"{OZ_TAB}!D2:D100000").execute().get("values", [])
    dates = [r[0] for r in rows if r and r[0]]
    return max(dates) if dates else None


def _cell(svc, rng):
    v = svc.spreadsheets().values().get(spreadsheetId=SHEET, range=rng).execute().get("values", [])
    return (v[0][0] if v and v[0] else "")


def afbot_alive(svc):
    """heartbeat C4 = «alive YYYY-MM-DD HH:MM:SS» — считаем живым, если моложе 25 мин."""
    hb = _cell(svc, HB_CELL)
    try:
        ts = hb.split("alive", 1)[1].strip()
        t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=3)))
        age_min = (datetime.now(timezone(timedelta(hours=3))) - t).total_seconds() / 60
        return age_min < 25
    except Exception:
        return False


def trigger_scrape(svc):
    """Перезапуск сбора: пишем base64-PS в пульт af-bot C2 (если свободен и ПК жив)."""
    if not afbot_alive(svc):
        print("af-bot heartbeat устарел — ПК недоступен, перезапуск не шлю", flush=True)
        return False
    if (_cell(svc, CMD_CELL) or "").strip():
        print("пульт C2 занят другой командой — перезапуск отложен до следующего раза", flush=True)
        return False
    b64 = base64.b64encode(LAUNCH_PS.encode("utf-16-le")).decode()
    cmd = "PS:powershell -NoProfile -EncodedCommand " + b64
    svc.spreadsheets().values().update(spreadsheetId=SHEET, range=CMD_CELL,
                                       valueInputOption="RAW", body={"values": [[cmd]]}).execute()
    print("перезапуск сбора отправлен в пульт af-bot (C2)", flush=True)
    return True


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(s):
    os.makedirs("data", exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)


def main():
    today = datetime.now(timezone.utc).date()
    rsvc = _svc(write=False)
    last = latest_date(rsvc)
    st = load_state()
    age = None
    if last:
        age = (today - datetime.strptime(last, "%Y-%m-%d").date()).days
        print(f"свежий отзыв Ozon: {last} (возраст {age} дн; триггер>{STALE_DAYS}, алерт>{ALERT_DAYS})", flush=True)
    else:
        print("архив пуст", flush=True)

    # 1) САМОВОССТАНОВЛЕНИЕ: каждый день перезапускаем сбор на af-bot (дедуп по id делает это безопасным).
    triggered = False
    if not DRY:
        try:
            triggered = trigger_scrape(_svc(write=True))
        except Exception as e:
            print("триггер не удался:", str(e)[:120], flush=True)
    else:
        print("DRY=1 — перезапуск не шлю", flush=True)

    # 2) ТРЕВОГА только если застой ≥ALERT_DAYS (авто-перезапуск не помогает → ПК реально недоступен).
    if age is not None and age >= ALERT_DAYS:
        if st.get("alerted_for") == last:
            print("уже алертил по этой дате — пропуск", flush=True)
        else:
            msg = (f"<b>Отзывы Ozon не собираются {age} дней (с {datetime.strptime(last,'%Y-%m-%d').strftime('%d.%m')}).</b>\n"
                   f"Авто-перезапуск сбора на af-bot не помогает — похоже, ПК/сессия недоступны. "
                   f"Николь, глянь af-bot (сбор отзывов).")
            print("--- АЛЕРТ ---\n" + msg, flush=True)
            if not DRY:
                notify.send(msg)
                st["alerted_for"] = last
                save_state(st)
                print("алерт отправлен", flush=True)
    else:
        if st.get("alerted_for"):
            st.pop("alerted_for", None); save_state(st)
        print("в норме (застой в пределах порога или перезапуск отправлен)", flush=True)


if __name__ == "__main__":
    main()
