"""P&L WB из собственного кэша WB-данных (НЕ из дашборда).

  Прибыль = Выплата(accrual) − Себес − Налог
  - Выплата = отчёт реализации WB (к перечислению за период)
  - Себес   = выкупы(Продажа−Возврат по SKU) × закуп (конфиг SEBES ниже)
  - Налог   = 11% × Сумма реализации
  - Реклама отдельной строкой НЕ вычитается; ДРР% = реклама / реализация.
"""
from __future__ import annotations

import os
import json
import urllib.request
import ssl

import wb_finance as wf

# Закуп по nm_id (источник — «юнит-экономика» дашборда). НЕ данные WB API.
SEBES = {
    76952248: 43, 140759945: 72, 205348527: 147, 140595726: 99, 583154383: 147,
    583155047: 147, 227067968: 94, 363137625: 161, 206024627: 62, 349314212: 53,
    93054004: 181, 388153628: 748, 97076035: 569, 144662550: 1138, 76942273: 569,
    87180591: 1138, 860793985: 72, 860789726: 43, 892991707: 72, 917665198: 89,
    1055320329: 53, 432606167: 37, 420607125: 132, 238005390: 70, 314277274: 83,
    654905827: 101,
}
TAX_RATE = 0.11


def fetch_ad_spend(date_from: str, date_to: str) -> float:
    ctx = ssl._create_unverified_context() if os.environ.get("INSECURE_SSL") == "1" else None
    req = urllib.request.Request(
        f"https://advert-api.wildberries.ru/adv/v1/upd?from={date_from}&to={date_to}",
        headers={"Authorization": os.environ["WB_TOKEN"].strip()})
    with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
        data = json.loads(r.read() or b"[]")
    if isinstance(data, dict):
        data = data.get("upd") or []
    return sum(float(x.get("updSum") or 0) for x in data
               if date_from <= (x.get("updTime") or "")[:10] <= date_to)


def compute_pnl(date_from: str, date_to: str, *, ad_spend=None) -> dict:
    rows = wf.fetch_realization(date_from, date_to)
    S = lambda f: sum(float(r.get(f) or 0) for r in rows)
    realiz = (sum(float(r.get("retail_amount") or 0) for r in rows if r.get("doc_type_name") == "Продажа")
              - sum(float(r.get("retail_amount") or 0) for r in rows if r.get("doc_type_name") == "Возврат"))
    payout = (S("ppvz_for_pay") - S("delivery_rub") - S("storage_fee")
              - S("penalty") - S("deduction") - S("acceptance") + S("additional_payment"))
    units = {}
    for r in rows:
        q = int(r.get("quantity") or 0)
        nm = r.get("nm_id")
        if r.get("supplier_oper_name") == "Продажа":
            units[nm] = units.get(nm, 0) + q
        elif r.get("supplier_oper_name") == "Возврат":
            units[nm] = units.get(nm, 0) - q
    sebes = sum(q * SEBES.get(nm, 0) for nm, q in units.items())
    missing = {nm: q for nm, q in units.items() if nm not in SEBES and q}
    nalog = TAX_RATE * realiz
    if ad_spend is None:
        ad_spend = fetch_ad_spend(date_from, date_to)
    profit = payout - sebes - nalog
    drr = (ad_spend / realiz * 100) if realiz else 0
    return {"period": f"{date_from}..{date_to}", "realization": realiz, "payout": payout,
            "units": sum(units.values()), "sebes": sebes, "missing_sebes": missing,
            "nalog": nalog, "ad_spend": ad_spend, "drr_pct": drr, "profit": profit}


def _f(x): return f"{x:,.0f}".replace(",", " ")


def print_pnl(p: dict) -> None:
    print(f"\n===== P&L WB — {p['period']} (accrual, из WB API) =====")
    print(f"Реализация (налог. база) : {_f(p['realization']):>12}")
    print(f"Выкуплено, шт            : {p['units']:>12}")
    print(f"Выплата (к перечислению) : {_f(p['payout']):>12}")
    print(f"- Себес                  : {_f(p['sebes']):>12}")
    print(f"- Налог 11%              : {_f(p['nalog']):>12}")
    print(f"= ПРИБЫЛЬ                 : {_f(p['profit']):>12}")
    print(f"Реклама ФАКТ             : {_f(p['ad_spend']):>12}")
    print(f"ДРР %                    : {p['drr_pct']:>11.1f}%")
    if p.get("missing_sebes"):
        print(f"[!] нет себеса для SKU: {p['missing_sebes']}")


if __name__ == "__main__":
    import sys
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    df, dt = (a + ["2026-06-01", "2026-06-07"])[:2]
    print_pnl(compute_pnl(df, dt))
