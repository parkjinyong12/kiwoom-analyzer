"""
Chart Analysis Agent 단위 테스트.
실제 OHLCV 데이터를 numpy로 생성해서 테스트.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from agents.chart_analysis import (
    ChartAnalysisAgent,
    _detect_candle_patterns,
    _find_support_resistance,
)


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def make_ohlcv(n: int, base: float = 70000, seed: int = 42) -> pd.DataFrame:
    """재현 가능한 랜덤 OHLCV DataFrame 생성."""
    rng = np.random.default_rng(seed)
    closes = base + np.cumsum(rng.normal(0, 500, n))
    opens  = closes + rng.normal(0, 200, n)
    highs  = np.maximum(opens, closes) + abs(rng.normal(0, 300, n))
    lows   = np.minimum(opens, closes) - abs(rng.normal(0, 300, n))
    vols   = rng.integers(500_000, 2_000_000, n)

    dates = [datetime(2024, 1, 1) + timedelta(days=i) for i in range(n)]
    return pd.DataFrame({
        "date": dates,
        "open": opens.astype(int),
        "high": highs.astype(int),
        "low":  lows.astype(int),
        "close": closes.astype(int),
        "volume": vols,
    })


# ---------------------------------------------------------------------------
# ChartAnalysisAgent.analyze
# ---------------------------------------------------------------------------

class TestAnalyze:

    def setup_method(self):
        self.agent = ChartAnalysisAgent()

    def test_정상_분석_반환(self):
        df = make_ohlcv(200)
        signal = self.agent.analyze("005930", df, timeframe="D")

        assert signal is not None
        assert signal.ticker == "005930"
        assert signal.timeframe == "D"
        assert isinstance(signal.indicators, dict)
        assert isinstance(signal.patterns, list)
        assert signal.support > 0
        assert signal.resistance > signal.support

    def test_데이터_부족시_None_반환(self):
        df = make_ohlcv(50)   # MIN_BARS["D"] = 130 미달
        signal = self.agent.analyze("005930", df, timeframe="D")
        assert signal is None

    def test_모든_지표_키_존재(self):
        df = make_ohlcv(200)
        signal = self.agent.analyze("005930", df)

        expected_keys = [
            "ma5", "ma20", "ma60", "ma120",
            "ema12", "ema26",
            "macd", "macd_signal", "macd_hist",
            "rsi",
            "stoch_k", "stoch_d",
            "bb_upper", "bb_mid", "bb_lower", "bb_width", "bb_pct",
            "atr",
            "obv",
            "vol_ma5", "vol_ma20", "vol_ratio",
            "close", "volume",
            "ma_aligned_bull", "ma_aligned_bear",
        ]
        for key in expected_keys:
            assert key in signal.indicators, f"누락된 지표 키: {key}"

    def test_rsi_범위(self):
        df = make_ohlcv(200)
        signal = self.agent.analyze("005930", df)
        rsi = signal.indicators["rsi"]
        if rsi is not None:
            assert 0 <= rsi <= 100

    def test_ma_정배열_역배열_상호_배타(self):
        df = make_ohlcv(200)
        signal = self.agent.analyze("005930", df)
        bull = signal.indicators["ma_aligned_bull"]
        bear = signal.indicators["ma_aligned_bear"]
        if bull is not None and bear is not None:
            assert not (bull and bear), "정배열과 역배열이 동시에 True일 수 없음"

    def test_분봉_분석(self):
        df = make_ohlcv(200)
        signal = self.agent.analyze("005930", df, timeframe="60")
        assert signal is not None
        assert signal.timeframe == "60"


# ---------------------------------------------------------------------------
# analyze_multi
# ---------------------------------------------------------------------------

class TestAnalyzeMulti:

    def setup_method(self):
        self.agent = ChartAnalysisAgent()

    def test_멀티_타임프레임_반환(self):
        ohlcv_map = {
            "D":  make_ohlcv(200, seed=1),
            "60": make_ohlcv(200, seed=2),
        }
        results = self.agent.analyze_multi("005930", ohlcv_map)

        assert "D" in results
        assert "60" in results
        assert results["D"].timeframe == "D"
        assert results["60"].timeframe == "60"

    def test_데이터_부족_타임프레임_제외(self):
        ohlcv_map = {
            "D":  make_ohlcv(200, seed=1),
            "60": make_ohlcv(10, seed=2),   # 데이터 부족
        }
        results = self.agent.analyze_multi("005930", ohlcv_map)
        assert "D" in results
        assert "60" not in results


# ---------------------------------------------------------------------------
# 지지/저항
# ---------------------------------------------------------------------------

class TestSupportResistance:

    def test_지지_저항_순서(self):
        df = make_ohlcv(100)
        support, resistance = _find_support_resistance(df)
        assert support < resistance

    def test_지지_현재가_미만(self):
        df = make_ohlcv(100)
        price = float(df["close"].iloc[-1])
        support, _ = _find_support_resistance(df, current_price=price)
        assert support < price

    def test_저항_현재가_초과(self):
        df = make_ohlcv(100)
        price = float(df["close"].iloc[-1])
        _, resistance = _find_support_resistance(df, current_price=price)
        assert resistance > price


# ---------------------------------------------------------------------------
# 캔들 패턴
# ---------------------------------------------------------------------------

class TestCandlePatterns:

    def _make_df(self, rows: list[tuple]) -> pd.DataFrame:
        """(open, high, low, close, volume) 튜플 리스트 → DataFrame."""
        return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])

    def test_정상_데이터_패턴_감지(self):
        df = make_ohlcv(5)
        patterns = _detect_candle_patterns(df)
        assert isinstance(patterns, list)

    def test_데이터_부족시_빈_리스트(self):
        df = make_ohlcv(2)
        patterns = _detect_candle_patterns(df)
        assert patterns == []

    def test_장대양봉_감지(self):
        # 직전 봉 몸통 100, 현재 봉 몸통 300 (3배)
        rows = [
            (1000, 1200, 900, 1100, 100),   # 몸통 100
            (1000, 1200, 900, 1100, 100),   # 몸통 100
            (1000, 1400, 990, 1300, 200),   # 몸통 300 → 장대양봉
        ]
        df = self._make_df(rows)
        patterns = _detect_candle_patterns(df)
        assert "장대양봉" in patterns

    def test_상승장악형_감지(self):
        rows = [
            (1100, 1200, 900, 1000, 100),   # 음봉 (시가 > 종가)
            (1100, 1200, 900, 1000, 100),   # 음봉
            (950,  1300, 940, 1150, 200),   # 양봉, 이전 음봉 완전 포함
        ]
        df = self._make_df(rows)
        patterns = _detect_candle_patterns(df)
        assert "상승장악형" in patterns
