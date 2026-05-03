"""
close_price 백필 스크립트.
supply_demand에 데이터는 있지만 close_price가 NULL인 종목에 대해
ka10008을 다시 호출해서 종가만 업데이트.
"""
import sys, logging
sys.path.insert(0, '.')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

import psycopg2, psycopg2.extras
from agents.market_data import MarketDataAgent
from config import config

conn = psycopg2.connect(config.database_url, cursor_factory=psycopg2.extras.RealDictCursor)
cur = conn.cursor()

cur.execute("""
    SELECT stock_code, COUNT(*) as days, COUNT(close_price) as filled
    FROM supply_demand
    GROUP BY stock_code
    HAVING COUNT(close_price) < COUNT(*)
    ORDER BY stock_code
""")
targets = cur.fetchall()
logging.info("close_price 백필 대상: %d개 종목", len(targets))

m = MarketDataAgent()

for idx, row in enumerate(targets, 1):
    ticker = row['stock_code']
    try:
        rows = m.get_foreign_holding(ticker, max_days=500)
        updates = [(r['close_price'], ticker, r['date']) for r in rows if r.get('close_price')]
        if updates:
            cur.executemany(
                "UPDATE supply_demand SET close_price = %s WHERE stock_code = %s AND date = %s",
                updates
            )
            conn.commit()
            logging.info("[%d/%d] %s — 종가 %d일 업데이트", idx, len(targets), ticker, len(updates))
        else:
            logging.info("[%d/%d] %s — 종가 데이터 없음", idx, len(targets), ticker)
    except Exception as e:
        conn.rollback()
        logging.error("[%d/%d] %s 오류: %s", idx, len(targets), ticker, e)

conn.close()
logging.info("백필 완료")
