"""
신용 관리 일별 스냅샷 배치 스크립트.
모든 사용자의 주식평가금·현금·대출금·추정자산·담보비율 합계를
오늘 날짜로 credit_snapshots 테이블에 기록 (upsert).

실행 시점: 에프터마켓 종료 후 20:10 자동 실행 (APScheduler 등록됨).
sync_prices.py 이후 실행 권장 (최신 종가 반영).
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

import psycopg2
import psycopg2.extras

from config import config


def main() -> None:
    conn = psycopg2.connect(config.database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    # 전체 사용자 조회
    cur.execute("SELECT id FROM users ORDER BY id")
    users = [r["id"] for r in cur.fetchall()]
    logger.info("대상 사용자: %d명", len(users))

    for uid in users:
        try:
            # credit_positions에 등록된 증권사 목록 + 대출금 합계
            # (화면에서 brokerEval[r.brokerage] ?? 0 로 매핑하는 것과 동일한 로직)
            cur.execute("""
                SELECT brokerage, COALESCE(loan_amount, 0) AS loan_amount
                FROM credit_positions
                WHERE user_id = %s
            """, (uid,))
            cp_rows = cur.fetchall()
            if not cp_rows:
                logger.info("uid=%d credit_positions 없음 — 스킵", uid)
                continue
            cp_brokerages = [r["brokerage"] for r in cp_rows]
            loan_amount   = sum(int(r["loan_amount"]) for r in cp_rows)

            # 주식 평가금 — credit_positions 증권사만 합산 (화면과 동일)
            cur.execute("""
                WITH latest_close AS (
                    SELECT DISTINCT ON (stock_code) stock_code, close_price
                    FROM supply_demand
                    WHERE close_price IS NOT NULL AND close_price > 0
                    ORDER BY stock_code, date DESC
                )
                SELECT
                    mh.brokerage,
                    COALESCE(SUM(mh.quantity * COALESCE(
                        lc.close_price,
                        CASE WHEN st.last_price ~ '^[0-9]+$' THEN st.last_price::BIGINT ELSE NULL END,
                        mh.avg_price,
                        0
                    )), 0) AS stock_eval
                FROM manual_holdings mh
                LEFT JOIN latest_close lc ON lc.stock_code = mh.stock_code
                LEFT JOIN stocks st ON st.stock_code = mh.stock_code
                WHERE mh.user_id = %s
                GROUP BY mh.brokerage
            """, (uid,))
            broker_stock = {(r["brokerage"] or ""): int(r["stock_eval"] or 0) for r in cur.fetchall()}
            stock_eval = sum(broker_stock.get(b or "", 0) for b in cp_brokerages)

            # 현금성 자산 — credit_positions 증권사만 합산, brokerage != '' 조건 동일
            cur.execute("""
                SELECT brokerage, COALESCE(SUM(amount), 0) AS cash_eval
                FROM cash_assets
                WHERE user_id = %s AND brokerage != '' AND asset_type_code != 'LAD'
                GROUP BY brokerage
            """, (uid,))
            broker_cash = {r["brokerage"]: int(r["cash_eval"] or 0) for r in cur.fetchall()}
            cash_eval = sum(broker_cash.get(b, 0) for b in cp_brokerages if b)

            # 추정자산 및 담보비율 계산
            collateral_asset  = stock_eval + cash_eval
            estimated_asset   = collateral_asset - loan_amount
            collateral_ratio  = round(collateral_asset / loan_amount * 100, 2) if loan_amount > 0 else None

            # upsert — 같은 날 재실행 시 덮어씀
            cur.execute("""
                INSERT INTO credit_snapshots
                    (user_id, record_date, stock_eval, cash_eval, loan_amount,
                     estimated_asset, collateral_ratio, created_at)
                VALUES (%s, CURRENT_DATE, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id, record_date) DO UPDATE SET
                    stock_eval       = EXCLUDED.stock_eval,
                    cash_eval        = EXCLUDED.cash_eval,
                    loan_amount      = EXCLUDED.loan_amount,
                    estimated_asset  = EXCLUDED.estimated_asset,
                    collateral_ratio = EXCLUDED.collateral_ratio,
                    created_at       = NOW()
            """, (uid, stock_eval, cash_eval, loan_amount, estimated_asset, collateral_ratio))
            conn.commit()

            logger.info(
                "uid=%d | 주식평가금 %s원 | 현금 %s원 | 대출 %s원 | 추정자산 %s원 | 담보비율 %s%%",
                uid,
                f"{stock_eval:,}", f"{cash_eval:,}", f"{loan_amount:,}",
                f"{estimated_asset:,}",
                f"{collateral_ratio:.1f}" if collateral_ratio is not None else "—",
            )
        except Exception as e:
            conn.rollback()
            logger.error("uid=%d 스냅샷 실패: %s", uid, e)

    conn.close()
    logger.info("신용 스냅샷 완료")


if __name__ == "__main__":
    main()
