"""
Market Data Agent 단위 테스트 (REST API 방식).
실제 HTTP 요청 없이 requests 모킹으로 동작.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from agents.market_data import MarketDataAgent, RateLimiter, TokenManager


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def make_daily_rows(n: int = 3) -> list[dict]:
    base = datetime(2024, 1, 1)
    return [
        {
            "stck_bsop_date": (base.replace(day=i + 1)).strftime("%Y%m%d"),
            "stck_oprc": "70000",
            "stck_hgpr": "72000",
            "stck_lwpr": "69000",
            "stck_clpr": str(71000 + i * 100),
            "acml_vol": "1000000",
        }
        for i in range(n)
    ]


def make_minute_rows(n: int = 2) -> list[dict]:
    return [
        {
            "stck_cntg_hour": f"2024010109{30 + i:02d}00",
            "stck_oprc": "70000",
            "stck_hgpr": "70500",
            "stck_lwpr": "69800",
            "stck_clpr": str(70300 + i * 100),
            "acml_vol": "50000",
        }
        for i in range(n)
    ]


@pytest.fixture
def agent():
    """실제 HTTP 요청 없이 동작하는 MarketDataAgent."""
    with patch("agents.market_data.config") as mock_cfg:
        mock_cfg.kiwoom.app_key = "test_key"
        mock_cfg.kiwoom.app_secret = "test_secret"
        mock_cfg.kiwoom.base_url = "https://openapi.kiwoom.com:9443"
        mock_cfg.kiwoom.tr_delay_seconds = 0.0
        mock_cfg.kiwoom.tr_per_minute_limit = 1000
        with patch("agents.market_data.TokenManager") as MockToken:
            MockToken.return_value.get_token.return_value = "mock_token"
            a = MarketDataAgent()
            yield a


# ---------------------------------------------------------------------------
# _parse_ohlcv
# ---------------------------------------------------------------------------

class TestParseOHLCV:

    def test_일봉_정상_파싱(self, agent):
        rows = make_daily_rows(2)
        df = agent._parse_ohlcv(rows, date_fmt="%Y%m%d")
        assert len(df) == 2
        assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
        assert df.iloc[0]["close"] == 71000
        assert df.iloc[1]["close"] == 71100

    def test_날짜_오름차순_정렬(self, agent):
        rows = [
            {"stck_bsop_date": "20240103", "stck_oprc": "1", "stck_hgpr": "1",
             "stck_lwpr": "1", "stck_clpr": "1", "acml_vol": "1"},
            {"stck_bsop_date": "20240101", "stck_oprc": "1", "stck_hgpr": "1",
             "stck_lwpr": "1", "stck_clpr": "1", "acml_vol": "1"},
        ]
        df = agent._parse_ohlcv(rows, date_fmt="%Y%m%d")
        dates = df["date"].tolist()
        assert dates == sorted(dates)

    def test_음수_가격_절댓값_처리(self, agent):
        rows = [{
            "stck_bsop_date": "20240101",
            "stck_oprc": "-70000", "stck_hgpr": "-72000",
            "stck_lwpr": "-69000", "stck_clpr": "-71500", "acml_vol": "500000",
        }]
        df = agent._parse_ohlcv(rows, date_fmt="%Y%m%d")
        assert df.iloc[0]["close"] == 71500
        assert df.iloc[0]["open"] == 70000

    def test_빈_입력(self, agent):
        df = agent._parse_ohlcv([], date_fmt="%Y%m%d")
        assert df.empty

    def test_잘못된_행_스킵(self, agent):
        rows = [
            {"stck_bsop_date": "20240101", "stck_oprc": "70000", "stck_hgpr": "72000",
             "stck_lwpr": "69000", "stck_clpr": "71500", "acml_vol": "1000000"},
            {"stck_bsop_date": "INVALID", "stck_oprc": "abc", "stck_hgpr": "1",
             "stck_lwpr": "1", "stck_clpr": "1", "acml_vol": "1"},
        ]
        df = agent._parse_ohlcv(rows, date_fmt="%Y%m%d")
        assert len(df) == 1

    def test_분봉_정상_파싱(self, agent):
        rows = make_minute_rows(2)
        df = agent._parse_ohlcv(rows, date_fmt="%Y%m%d%H%M%S")
        assert len(df) == 2
        assert df.iloc[0]["date"] == datetime(2024, 1, 1, 9, 30, 0)


# ---------------------------------------------------------------------------
# get_daily_ohlcv
# ---------------------------------------------------------------------------

class TestGetDailyOHLCV:

    def test_정상_반환(self, agent):
        mock_resp = {"rt_cd": "0", "output2": make_daily_rows(5)}
        with patch.object(agent, "_get", return_value=mock_resp):
            df = agent.get_daily_ohlcv("005930", count=3)
        assert len(df) == 3

    def test_count_제한(self, agent):
        mock_resp = {"rt_cd": "0", "output2": make_daily_rows(10)}
        with patch.object(agent, "_get", return_value=mock_resp):
            df = agent.get_daily_ohlcv("005930", count=5)
        assert len(df) == 5


# ---------------------------------------------------------------------------
# get_minute_ohlcv
# ---------------------------------------------------------------------------

class TestGetMinuteOHLCV:

    def test_정상_반환(self, agent):
        mock_resp = {"rt_cd": "0", "output2": make_minute_rows(3)}
        with patch.object(agent, "_get", return_value=mock_resp):
            df = agent.get_minute_ohlcv("005930", timeframe="60")
        assert len(df) == 3


# ---------------------------------------------------------------------------
# get_watchlist_ohlcv
# ---------------------------------------------------------------------------

class TestGetWatchlistOHLCV:

    def test_실패_종목_제외(self, agent):
        good_df = pd.DataFrame({
            "date": [datetime(2024, 1, 1)],
            "open": [1000], "high": [1100], "low": [900],
            "close": [1050], "volume": [100000],
        })

        def side_effect(ticker, count):
            if ticker == "999999":
                raise RuntimeError("조회 실패")
            return good_df

        agent.get_daily_ohlcv = side_effect
        result = agent.get_watchlist_ohlcv(["005930", "999999"], timeframe="D")

        assert "005930" in result
        assert "999999" not in result

    def test_빈_데이터_제외(self, agent):
        agent.get_daily_ohlcv = lambda ticker, count: pd.DataFrame()
        result = agent.get_watchlist_ohlcv(["005930"], timeframe="D")
        assert result == {}


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

class TestRateLimiter:

    def test_최소_간격_보장(self):
        import time
        limiter = RateLimiter(min_interval=0.05, per_minute=100)
        t0 = time.monotonic()
        limiter.wait()
        limiter.wait()
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.05
