"""계좌 보유종목 조회 및 출력.

사용법:
  python scripts/show_holdings.py
"""
from __future__ import annotations

import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import config
from agents.market_data import MarketDataAgent


def main() -> None:
    agent = MarketDataAgent()

    print("계좌 보유종목 조회 중...")
    try:
        raw = agent.get_account_holdings()
    except ValueError as e:
        print(f"[오류] {e}")
        sys.exit(1)

    if not raw:
        print("보유종목이 없습니다.")
        return

    # 동일 종목(신용 대출건별 분리) 합산
    grouped: dict[str, dict] = {}
    for h in raw:
        key = h.stock_code
        if key not in grouped:
            grouped[key] = {
                "stock_code":    h.stock_code,
                "stock_name":    h.stock_name,
                "hold_qty":      0,
                "pur_amt_total": 0.0,
                "eval_amount":   0.0,
                "current_price": h.current_price,
            }
        g = grouped[key]
        g["hold_qty"]      += h.hold_qty
        g["pur_amt_total"] += h.buy_avg_price * h.hold_qty
        g["eval_amount"]   += h.eval_amount

    holdings = []
    for g in grouped.values():
        qty = g["hold_qty"]
        pur_amt = g["pur_amt_total"]
        eval_amt = g["eval_amount"]
        avg_price = (pur_amt / qty) if qty else 0.0
        pnl_amt = eval_amt - pur_amt
        pnl_rt  = (pnl_amt / pur_amt * 100) if pur_amt else 0.0
        holdings.append({
            **g,
            "avg_price": avg_price,
            "pnl_amt":   pnl_amt,
            "pnl_rt":    pnl_rt,
        })

    holdings.sort(key=lambda x: x["eval_amount"], reverse=True)

    total_eval = sum(h["eval_amount"]   for h in holdings)
    total_pur  = sum(h["pur_amt_total"] for h in holdings)
    total_pnl  = total_eval - total_pur

    print()
    print(f"  계좌번호: {config.kiwoom.acnt_no}  |  보유종목 {len(holdings)}개")
    print()
    hdr = f"{'No':>3}  {'종목코드':<8} {'종목명':<16} {'수량':>6} {'평균단가':>10} {'현재가':>10} {'평가금액':>13} {'손익금액':>12} {'손익률':>7}"
    print(hdr)
    print("-" * len(hdr))

    for i, h in enumerate(holdings, 1):
        sign = "+" if h["pnl_amt"] >= 0 else ""
        print(
            f"{i:>3}  {h['stock_code']:<8} {h['stock_name']:<16} "
            f"{h['hold_qty']:>6,} "
            f"{h['avg_price']:>10,.0f} "
            f"{h['current_price']:>10,.0f} "
            f"{h['eval_amount']:>13,.0f} "
            f"{sign}{h['pnl_amt']:>11,.0f} "
            f"{sign}{h['pnl_rt']:>6.2f}%"
        )

    print("-" * len(hdr))
    total_sign = "+" if total_pnl >= 0 else ""
    total_rt   = (total_pnl / total_pur * 100) if total_pur else 0.0
    print(
        f"{'합계':>3}  {'':8} {'':16} {'':>6} {'':>10} {'':>10} "
        f"{total_eval:>13,.0f} "
        f"{total_sign}{total_pnl:>11,.0f} "
        f"{total_sign}{total_rt:>6.2f}%"
    )


if __name__ == "__main__":
    main()
