"""Проба headless Chromium (как делают люди): рендерит JS → проходит пассивную
SmartCaptcha → читает цену карточки ЯМ. Через socks-прокси Happ (уже поднят
setup_oz_proxy на 10808). Диагностика: печатает, была ли капча и какие цены видны."""
import os, time, re
from playwright.sync_api import sync_playwright

CARD = os.environ.get("YM_CARD", "https://market.yandex.ru/card/x/102196144368")
PROXY = os.environ.get("OZ_PROXY", "")  # socks5://127.0.0.1:10808 или пусто (напрямую)

with sync_playwright() as pw:
    launch = {"headless": True, "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"]}
    if PROXY:
        launch["proxy"] = {"server": PROXY}
    b = pw.chromium.launch(**launch)
    ctx = b.new_context(locale="ru-RU",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        viewport={"width": 1366, "height": 900})
    pg = ctx.new_page()
    try:
        pg.goto("https://market.yandex.ru/", wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)
        pg.goto(CARD, wait_until="domcontentloaded", timeout=60000)
        time.sleep(6)
        html = pg.content()
        low = html.lower()
        cap = ("smartcaptcha" in low or "showcaptcha" in low or "проверка" in low) and len(html) < 200000
        print("URL:", pg.url, flush=True)
        print("title:", pg.title(), flush=True)
        print("len:", len(html), "| captcha?:", cap, flush=True)
        # пробуем прочитать цены
        for sel in ['[data-auto="snippet-price-current"]', '[data-auto="price-value"]',
                    '[data-auto*="price"]', 'h3:has-text("₽")']:
            try:
                els = pg.query_selector_all(sel)
                for e in els[:3]:
                    txt = (e.inner_text() or "").strip().replace("\n", " ")
                    if txt:
                        print("  price[%s]: %s" % (sel, txt[:50]), flush=True)
            except Exception:
                pass
        # грубо — все ₽-числа на странице
        prices = re.findall(r"(\d[\d\s]{1,7})\s*₽", pg.inner_text("body"))
        print("  ₽-числа на странице:", [p.strip() for p in prices[:10]], flush=True)
    except Exception as e:
        print("ERR:", str(e)[:150], flush=True)
    finally:
        b.close()
