"""Облачный раннер: считает WB-API сторону P&L за период и кладёт отчёт в data/reports/.

Период берётся из data/period.json ({"from":"YYYY-MM-DD","to":"YYYY-MM-DD"}),
иначе из env PERIOD_FROM/PERIOD_TO, иначе дефолт. Сменить период = править
data/period.json (обычный файл, не workflow) и запустить workflow вручную.
Дашборд-сторону тут НЕ считаем — собираем на месте при сверке.
"""
from __future__ import annotations
import os
import json
import pathlib
import wb_pnl


def _period():
    p = pathlib.Path("data/period.json")
    if p.exists():
        try:
            d = json.loads(p.read_text())
            return d["from"], d["to"]
        except Exception:
            pass
    return os.environ.get("PERIOD_FROM", "2026-06-01"), os.environ.get("PERIOD_TO", "2026-06-07")


DF, DT = _period()
p = wb_pnl.compute_pnl(DF, DT)
out = pathlib.Path("data/reports")
out.mkdir(parents=True, exist_ok=True)
(out / f"wb_api_{DF}_{DT}.json").write_text(
    json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
wb_pnl.print_pnl(p)
print(f"\n[ok] отчёт сохранён: data/reports/wb_api_{DF}_{DT}.json")
