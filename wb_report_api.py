"""Облачный раннер: считает WB-API сторону P&L за период и кладёт отчёт в data/reports/.

Запускается в GitHub Actions (.github/workflows/wb_pnl.yml). Период — env
PERIOD_FROM/PERIOD_TO; WB-токен — секрет WB_TOKEN. Дашборд-сторона тут НЕ
считается (её собираем на маке при открытии — мгновенно, без бана).
"""
from __future__ import annotations
import os
import json
import pathlib
import wb_pnl

DF = os.environ.get("PERIOD_FROM", "2026-06-01")
DT = os.environ.get("PERIOD_TO", "2026-06-07")

p = wb_pnl.compute_pnl(DF, DT)
out = pathlib.Path("data/reports")
out.mkdir(parents=True, exist_ok=True)
(out / f"wb_api_{DF}_{DT}.json").write_text(
    json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
wb_pnl.print_pnl(p)
print(f"\n[ok] отчёт сохранён: data/reports/wb_api_{DF}_{DT}.json")
