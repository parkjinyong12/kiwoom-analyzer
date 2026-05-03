"""
Strategy Agent 단위 테스트.
ChartSignal을 직접 구성해서 전략 로직 검증.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from agents.strategy import (
    BollingerBreakoutStrategy,
    CandlePatternStrategy,
    GoldenCrossStrategy,
    RSIReversalStrategy,
    StrategyAgent,
)
from models import ChartSignal


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def make_signal(indicators: dict, patterns: list[str] = None, support: float = 60000, resistance: float = 80000) -> ChartSignal:
    base = {
        "close": 70000.0, "volume": 1_000_000.0,
        "ma5": 70000, "ma20": 69000, "ma60": 68000, "ma120": 67000,
        "ema12": 70100, "ema26": 69500,
        "macd": 100.0, "macd_signal": 80.0, "macd_hist": 20.0,
        "rsi": 50.0,
        "stoch_k": 50.0, "stoch_d": 48.0,
        "bb_upper": 73000.0, "bb_mid": 70000.0, "bb_lower": 67000.0,
        "bb_width": 0.09, "bb_pct": 0.5,
        "atr": 500.0,
        "obv": 5_000_000.0,
        "vol_ma5": 900_000.0, "vol_ma20": 800_000.0,
        "vol_ratio": 1.25,
        "ma_aligned_bull": True, "ma_aligned_bear": False,
    }
    base.update(indicators)
    return ChartSignal(
        ticker="005930",
        timeframe="D",
        timestamp=datetime.now(),
        indicators=base,
        patterns=patterns or [],
        support=support,
        resistance=resistance,
    )


# ---------------------------------------------------------------------------
# GoldenCrossStrategy
# ---------------------------------------------------------------------------

class TestGoldenCrossStrategy:

    def setup_method(self):
        self.strategy = GoldenCrossStrategy()

    def test_골든크로스_buy_신호(self):
        s = make_signal({"ma5": 71000, "ma20": 69000, "vol_ratio": 1.8})
        result = self.strategy.run(s)
        assert result is not None
        assert result.direction == "BUY"
        assert result.confidence > 0.6

    def test_데드크로스_sell_신호(self):
        s = make_signal({"ma5": 68000, "ma20": 70000, "vol_ratio": 1.8})
        result = self.strategy.run(s)
        assert result is not None
        assert result.direction == "SELL"

    def test_정배열_bonus_confidence(self):
        s_no_align  = make_signal({"ma5": 71000, "ma20": 69000, "vol_ratio": 1.0, "ma_aligned_bull": False})
        s_aligned   = make_signal({"ma5": 71000, "ma20": 69000, "vol_ratio": 1.0, "ma_aligned_bull": True})
        r1 = self.strategy.run(s_no_align)
        r2 = self.strategy.run(s_aligned)
        assert r2.confidence > r1.confidence

    def test_지표_없으면_None(self):
        s = make_signal({"ma5": None, "ma20": None})
        assert self.strategy.run(s) is None


# ---------------------------------------------------------------------------
# RSIReversalStrategy
# ---------------------------------------------------------------------------

class TestRSIReversalStrategy:

    def setup_method(self):
        self.strategy = RSIReversalStrategy()

    def test_과매도_buy(self):
        s = make_signal({"rsi": 25.0, "macd_hist": 10.0})
        result = self.strategy.run(s)
        assert result is not None
        assert result.direction == "BUY"

    def test_과매수_sell(self):
        s = make_signal({"rsi": 75.0, "macd_hist": -10.0})
        result = self.strategy.run(s)
        assert result is not None
        assert result.direction == "SELL"

    def test_중립_RSI_None(self):
        s = make_signal({"rsi": 50.0})
        assert self.strategy.run(s) is None

    def test_stoch_동반_confidence_증가(self):
        s_no_stoch = make_signal({"rsi": 28.0, "macd_hist": 5.0, "stoch_k": 50.0})
        s_stoch    = make_signal({"rsi": 28.0, "macd_hist": 5.0, "stoch_k": 15.0})
        r1 = self.strategy.run(s_no_stoch)
        r2 = self.strategy.run(s_stoch)
        assert r2.confidence > r1.confidence


# ---------------------------------------------------------------------------
# BollingerBreakoutStrategy
# ---------------------------------------------------------------------------

class TestBollingerBreakoutStrategy:

    def setup_method(self):
        self.strategy = BollingerBreakoutStrategy()

    def test_상단_돌파_buy(self):
        s = make_signal({
            "close": 73100.0,
            "bb_upper": 73000.0, "bb_lower": 67000.0,
            "bb_pct": 1.02, "bb_width": 0.05,
            "vol_ratio": 2.0,
        })
        result = self.strategy.run(s)
        assert result is not None
        assert result.direction == "BUY"

    def test_하단_이탈_sell(self):
        s = make_signal({
            "close": 66900.0,
            "bb_upper": 73000.0, "bb_lower": 67000.0,
            "bb_pct": -0.02, "bb_width": 0.05,
            "vol_ratio": 2.0,
        })
        result = self.strategy.run(s)
        assert result is not None
        assert result.direction == "SELL"

    def test_수축_돌파_confidence_더_높음(self):
        s_wide   = make_signal({"close": 73100.0, "bb_upper": 73000.0, "bb_lower": 67000.0,
                                "bb_pct": 1.02, "bb_width": 0.15, "vol_ratio": 1.0})
        s_squeeze = make_signal({"close": 73100.0, "bb_upper": 73000.0, "bb_lower": 67000.0,
                                 "bb_pct": 1.02, "bb_width": 0.05, "vol_ratio": 1.0})
        r1 = self.strategy.run(s_wide)
        r2 = self.strategy.run(s_squeeze)
        assert r2.confidence > r1.confidence

    def test_중간권_None(self):
        s = make_signal({"bb_pct": 0.5, "bb_width": 0.09})
        assert self.strategy.run(s) is None


# ---------------------------------------------------------------------------
# CandlePatternStrategy
# ---------------------------------------------------------------------------

class TestCandlePatternStrategy:

    def setup_method(self):
        self.strategy = CandlePatternStrategy()

    def test_지지선_망치형_buy(self):
        s = make_signal(
            {"close": 60500.0},
            patterns=["망치형(양봉)"],
            support=60000,
            resistance=75000,
        )
        result = self.strategy.run(s)
        assert result is not None
        assert result.direction == "BUY"

    def test_저항선_역망치형_sell(self):
        s = make_signal(
            {"close": 79500.0},
            patterns=["역망치형"],
            support=60000,
            resistance=80000,
        )
        result = self.strategy.run(s)
        assert result is not None
        assert result.direction == "SELL"

    def test_패턴_없으면_None(self):
        s = make_signal({}, patterns=[])
        assert self.strategy.run(s) is None

    def test_지지저항_멀면_None(self):
        # 지지선과 거리 5% → 기준(1.5%) 초과 → None
        s = make_signal({"close": 70000.0}, patterns=["망치형(양봉)"],
                        support=60000, resistance=90000)
        assert self.strategy.run(s) is None


# ---------------------------------------------------------------------------
# StrategyAgent 통합
# ---------------------------------------------------------------------------

class TestStrategyAgent:

    def setup_method(self):
        self.agent = StrategyAgent()

    def test_강한_buy_신호_반환(self):
        # 골든크로스 + RSI 과매도 + 거래량 폭증 → BUY
        s = make_signal({
            "ma5": 71000, "ma20": 69000, "vol_ratio": 2.5,
            "rsi": 28.0, "macd_hist": 15.0, "stoch_k": 18.0,
            "ma_aligned_bull": True,
        })
        result = self.agent.run("005930", s)
        assert result.signal == "BUY"
        assert result.confidence >= 0.6

    def test_낮은_confidence_hold(self):
        # 모든 지표 중립
        s = make_signal({
            "ma5": 70000, "ma20": 70000, "vol_ratio": 1.0,
            "rsi": 50.0, "macd_hist": 0.0,
            "bb_pct": 0.5, "bb_width": 0.1,
        })
        result = self.agent.run("005930", s)
        assert result.signal == "HOLD"

    def test_target_stop_atr_기반(self):
        s = make_signal({
            "ma5": 71000, "ma20": 69000, "vol_ratio": 2.0,
            "rsi": 27.0, "macd_hist": 20.0,
            "close": 70000.0, "atr": 1000.0,
        })
        result = self.agent.run("005930", s)
        if result.signal == "BUY":
            assert result.target_price == 72000.0   # 70000 + 1000*2
            assert result.stop_loss == 68500.0      # 70000 - 1000*1.5

    def test_reasons_한국어(self):
        s = make_signal({
            "ma5": 71000, "ma20": 69000, "vol_ratio": 2.0,
            "rsi": 27.0, "macd_hist": 20.0,
        })
        result = self.agent.run("005930", s)
        for reason in result.reasons:
            # ASCII만으로 이뤄진 reasons 없어야 함 (한국어 포함)
            assert any(ord(c) > 127 for c in reason), f"한국어 없는 reason: {reason}"

    def test_멀티_타임프레임(self):
        s_d  = make_signal({"ma5": 71000, "ma20": 69000, "vol_ratio": 2.0, "rsi": 27.0, "macd_hist": 20.0})
        s_60 = make_signal({"ma5": 71000, "ma20": 69000, "vol_ratio": 1.8, "rsi": 32.0, "macd_hist": 10.0})
        s_60.timeframe = "60"

        result = self.agent.run_multi("005930", {"D": s_d, "60": s_60}, primary_tf="D")
        assert result.signal in ("BUY", "SELL", "HOLD")
        assert 0.0 <= result.confidence <= 1.0
