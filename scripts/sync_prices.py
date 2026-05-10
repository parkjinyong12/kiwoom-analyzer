"""
타사 보유종목 현재가(종가) 동기화 스크립트.
키움 REST API ka10081(일봉)로 최신 종가를 가져와 DB에 저장.

처리 순서:
  1. manual_holdings에서 DISTINCT stock_code 목록 조회
  2. 각 종목별 ka10081 일봉 호출 → 최신 거래일 종가 취득
  3. supply_demand (stock_code, date, close_price) upsert
  4. stocks (last_price, fetched_at) upsert
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

from agents.market_data import MarketDataAgent
from config import config


def main() -> None:
    conn = psycopg2.connect(config.database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    # manual_holdings에서 종목코드 + 종목명 수집 (이름은 가장 최근 입력값 사용)
    cur.execute("""
        SELECT stock_code, MAX(stock_name) AS stock_name
        FROM manual_holdings
        GROUP BY stock_code
        ORDER BY stock_code
    """)
    targets = [dict(r) for r in cur.fetchall()]
    logger.info("동기화 대상: %d개 종목", len(targets))

    if not targets:
        logger.info("대상 종목 없음. 종료.")
        conn.close()
        return

    m = MarketDataAgent()
    updated = 0
    failed = 0

    for idx, t in enumerate(targets, 1):
        ticker     = t["stock_code"]
        stock_name = t["stock_name"] or ticker
        try:
            df = m.get_daily_ohlcv(ticker, count=5)
            if df.empty:
                logger.warning("[%d/%d] %s — 일봉 데이터 없음", idx, len(targets), ticker)
                failed += 1
                continue

            latest      = df.iloc[-1]
            trade_date  = latest["date"].date() if hasattr(latest["date"], "date") else latest["date"]
            close_price = int(abs(float(latest["close"])))

            if close_price <= 0:
                logger.warning("[%d/%d] %s — 종가 0, 스킵", idx, len(targets), ticker)
                failed += 1
                continue

            # supply_demand upsert (close_price만; 수급 데이터 컬럼은 건드리지 않음)
            cur.execute("""
                INSERT INTO supply_demand (stock_code, date, close_price)
                VALUES (%s, %s, %s)
                ON CONFLICT (stock_code, date)
                DO UPDATE SET close_price = EXCLUDED.close_price
            """, (ticker, trade_date, close_price))

            # stocks upsert (없으면 insert, 있으면 last_price + fetched_at만 갱신)
            cur.execute("""
                INSERT INTO stocks (stock_code, stock_name, last_price, fetched_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (stock_code)
                DO UPDATE SET last_price = EXCLUDED.last_price,
                              fetched_at = NOW()
            """, (ticker, stock_name, str(close_price)))

            conn.commit()
            logger.info("[%d/%d] %s %s — %s 종가 %d원",
                        idx, len(targets), ticker, stock_name, trade_date, close_price)
            updated += 1

        except Exception as e:
            conn.rollback()
            logger.error("[%d/%d] %s 오류: %s", idx, len(targets), ticker, e)
            failed += 1

    conn.close()
    logger.info("동기화 완료: 성공 %d개, 실패 %d개", updated, failed)


if __name__ == "__main__":
    main()
