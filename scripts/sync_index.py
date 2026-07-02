"""
코스피/코스닥 지수 일별 시세 동기화 배치 스크립트.
네이버 증권 모바일 API(m.stock.naver.com)에서 일별 종가·시가·고가·저가를
가져와 index_prices 테이블에 기록 (upsert).

실행 시점: 장 마감 후 18:30 자동 실행 (APScheduler 등록됨).
snapshot_credit.py 이전 실행 권장 (신용관리 화면 코스피 비교에 최신값 반영).
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
import requests

from config import config

INDEX_CODES = ["KOSPI", "KOSDAQ"]
PAGES = 5          # 페이지당 20건 기준 최근 100일치
PAGE_SIZE = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://m.stock.naver.com/",
    "Accept": "application/json",
}


def _num_or_none(raw):
    if raw in (None, "", "-"):
        return None
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fetch_index_prices(code: str) -> list[dict]:
    """m.stock.naver.com에서 지수 일별 시세 조회 (페이지네이션)."""
    url = f"https://m.stock.naver.com/api/index/{code}/price"
    out: list[dict] = []
    for page in range(1, PAGES + 1):
        resp = requests.get(url, headers=_HEADERS, params={"page": page, "pageSize": PAGE_SIZE}, timeout=10)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        for r in rows:
            close = _num_or_none(r.get("closePrice"))
            if close is None:
                continue
            out.append({
                "trade_date":   r["localTradedAt"],
                "close_price":  close,
                "open_price":   _num_or_none(r.get("openPrice")),
                "high_price":   _num_or_none(r.get("highPrice")),
                "low_price":    _num_or_none(r.get("lowPrice")),
                "change_price": _num_or_none(r.get("compareToPreviousClosePrice")),
                "change_pct":   _num_or_none(r.get("fluctuationsRatio")),
            })
        if len(rows) < PAGE_SIZE:
            break
    return out


def main() -> None:
    conn = psycopg2.connect(config.database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    for code in INDEX_CODES:
        try:
            rows = fetch_index_prices(code)
        except Exception as e:
            logger.error("%s 시세 조회 실패: %s", code, e)
            continue

        if not rows:
            logger.warning("%s 조회된 데이터 없음", code)
            continue

        for r in rows:
            cur.execute("""
                INSERT INTO index_prices
                    (index_code, trade_date, close_price, open_price, high_price, low_price, change_price, change_pct, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                ON CONFLICT (index_code, trade_date) DO UPDATE SET
                    close_price  = EXCLUDED.close_price,
                    open_price   = EXCLUDED.open_price,
                    high_price   = EXCLUDED.high_price,
                    low_price    = EXCLUDED.low_price,
                    change_price = EXCLUDED.change_price,
                    change_pct   = EXCLUDED.change_pct,
                    updated_at   = NOW()
            """, (code, r["trade_date"], r["close_price"], r["open_price"], r["high_price"],
                  r["low_price"], r["change_price"], r["change_pct"]))
        conn.commit()

        latest = rows[0]
        logger.info("%s 동기화 완료: %d건 (최신 %s 종가 %s)", code, len(rows), latest["trade_date"], latest["close_price"])

    conn.close()
    logger.info("지수 시세 동기화 완료")


if __name__ == "__main__":
    main()
