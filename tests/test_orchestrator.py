"""
Orchestrator / Pipeline 단위 테스트.
외부 의존성 전부 모킹.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from config import config
from models import ChartSignal, RiskCheckResult, TradeSignal


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def make_df(n: int = 200) -> pd.DataFrame:
    import numpy as np
    rng = np.random.default_rng(42)
    closes = 70000 + np.cumsum(rng.normal(0, 500, n))
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n),
        "open":  (closes + rng.normal(0, 200, n)).astype(int),
        "high":  (closes + abs(rng.normal(0, 300, n))).astype(int),
        "low":   (closes - abs(rng.normal(0, 300, n))).astype(int),
        "close": closes.astype(int),
        "volume": rng.integers(500_000, 2_000_000, n),
    })


def make_chart_signal(ticker: str = "005930", tf: str = "D") -> ChartSignal:
    return ChartSignal(
        ticker=ticker, timeframe=tf, timestamp=datetime.now(),
        indicators={"close": 70000.0, "atr": 500.0, "vol_ratio": 1.5,
                    "ma5": 71000, "ma20": 69000, "rsi": 45.0,
                    "macd": 100.0, "macd_signal": 80.0, "macd_hist": 20.0,
                    "volume": 1_000_000.0, "vol_ma20": 800_000.0},
        patterns=[], support=68000.0, resistance=73000.0,
    )


def make_trade_signal(direction: str = "BUY") -> TradeSignal:
    return TradeSignal(
        ticker="005930", signal=direction, confidence=0.75,
        strategy_name="골든크로스", reasons=["테스트"],
        timeframe="D", timestamp=datetime.now(),
        price=70000.0, target_price=71000.0, stop_loss=69000.0,
    )


def make_risk_result(approved: bool = True) -> RiskCheckResult:
    return RiskCheckResult(
        signal=make_trade_signal(),
        approved=approved,
        block_reasons=[] if approved else ["신뢰도 부족"],
        risk_level="LOW",
        adjusted_confidence=0.75,
    )


@pytest.fixture
def mocked_pipeline():
    """모든 에이전트를 Mock으로 교체한 Pipeline."""
    from orchestrator import Pipeline

    market  = MagicMock()
    chart   = MagicMock()
    strategy = MagicMock()
    risk    = MagicMock()
    slack   = MagicMock()
    audit   = MagicMock()

    # 기본 반환값 설정
    market.get_daily_ohlcv.return_value = make_df()
    market.get_minute_ohlcv.return_value = make_df()
    chart.analyze_multi.return_value = {"D": make_chart_signal()}
    strategy.run_multi.return_value = make_trade_signal("BUY")
    risk.check.return_value = make_risk_result(approved=True)
    slack.send_signal.return_value = MagicMock(success=True, error=None)

    pipeline = Pipeline(market, chart, strategy, risk, slack, audit)
    return pipeline, market, chart, strategy, risk, slack, audit


# ---------------------------------------------------------------------------
# Pipeline 정상 흐름
# ---------------------------------------------------------------------------

class TestPipelineHappyPath:

    def test_buy_신호_전체_흐름(self, mocked_pipeline):
        pipeline, market, chart, strategy, risk, slack, audit = mocked_pipeline
        pipeline.run("005930", ["D"])

        market.get_daily_ohlcv.assert_called_once_with("005930", count=200)
        chart.analyze_multi.assert_called_once()
        strategy.run_multi.assert_called_once()
        risk.check.assert_called_once()
        slack.send_signal.assert_called_once()
        audit.log_notification.assert_called_once()

    def test_멀티_타임프레임_데이터_수집(self, mocked_pipeline):
        pipeline, market, chart, _, _, _, _ = mocked_pipeline
        chart.analyze_multi.return_value = {
            "D": make_chart_signal("005930", "D"),
            "60": make_chart_signal("005930", "60"),
        }
        pipeline.run("005930", ["D", "60"])

        market.get_daily_ohlcv.assert_called_once()
        market.get_minute_ohlcv.assert_called_once()

    def test_sell_신호_처리(self, mocked_pipeline):
        pipeline, _, _, strategy, risk, slack, _ = mocked_pipeline
        strategy.run_multi.return_value = make_trade_signal("SELL")
        risk.check.return_value = make_risk_result(approved=True)

        pipeline.run("005930", ["D"])
        slack.send_signal.assert_called_once()


# ---------------------------------------------------------------------------
# Pipeline 차단 / 스킵 시나리오
# ---------------------------------------------------------------------------

class TestPipelineBlocking:

    def test_hold_신호_이후_단계_스킵(self, mocked_pipeline):
        pipeline, _, _, strategy, risk, slack, audit = mocked_pipeline
        strategy.run_multi.return_value = make_trade_signal("HOLD")

        pipeline.run("005930", ["D"])

        risk.check.assert_not_called()
        slack.send_signal.assert_not_called()

    def test_risk_차단_slack_스킵(self, mocked_pipeline):
        pipeline, _, _, _, risk, slack, audit = mocked_pipeline
        risk.check.return_value = make_risk_result(approved=False)

        pipeline.run("005930", ["D"])

        slack.send_signal.assert_not_called()
        audit.log_risk_check.assert_called_once()

    def test_빈_데이터_파이프라인_중단(self, mocked_pipeline):
        pipeline, market, chart, strategy, _, _, audit = mocked_pipeline
        market.get_daily_ohlcv.return_value = pd.DataFrame()

        pipeline.run("005930", ["D"])

        chart.analyze_multi.assert_not_called()
        strategy.run_multi.assert_not_called()

    def test_데이터_수집_예외_파이프라인_계속(self, mocked_pipeline):
        """일부 타임프레임 실패해도 나머지로 계속 진행."""
        pipeline, market, chart, strategy, risk, slack, audit = mocked_pipeline
        market.get_daily_ohlcv.side_effect = Exception("TR 오류")
        market.get_minute_ohlcv.return_value = make_df()
        chart.analyze_multi.return_value = {"60": make_chart_signal("005930", "60")}

        pipeline.run("005930", ["D", "60"])

        audit.log_error.assert_called()
        chart.analyze_multi.assert_called_once()

    def test_차트_분석_결과_없음_중단(self, mocked_pipeline):
        pipeline, _, chart, strategy, _, _, _ = mocked_pipeline
        chart.analyze_multi.return_value = {}

        pipeline.run("005930", ["D"])

        strategy.run_multi.assert_not_called()

    def test_slack_실패_audit_에러_기록(self, mocked_pipeline):
        pipeline, _, _, _, _, slack, audit = mocked_pipeline
        slack.send_signal.return_value = MagicMock(success=False, error="channel_not_found")

        pipeline.run("005930", ["D"])

        audit.log_error.assert_called()


# ---------------------------------------------------------------------------
# Orchestrator 초기화
# ---------------------------------------------------------------------------

class TestOrchestratorInit:

    def test_스케줄_등록(self):
        from orchestrator import Orchestrator
        with patch("orchestrator._MARKET_DATA_AVAILABLE", False):
            orc = Orchestrator(interval_minutes=15)

        job_ids = {job.id for job in orc._scheduler.get_jobs()}
        assert "pre_market" in job_ids
        assert "intraday" in job_ids
        assert "market_close" in job_ids
        assert "purge_logs" in job_ids

    def test_run_once_실행(self):
        from orchestrator import Orchestrator
        with patch("orchestrator._MARKET_DATA_AVAILABLE", False):
            orc = Orchestrator()

        orc._pipeline = MagicMock()
        orc.run_once(timeframes=["D"])

        # watchlist 종목 수만큼 pipeline.run 호출됐는지 확인
        assert orc._pipeline.run.call_count == len(config.watchlist)

    def test_stop_flush_호출(self):
        from orchestrator import Orchestrator
        with patch("orchestrator._MARKET_DATA_AVAILABLE", False):
            orc = Orchestrator()

        orc._audit = MagicMock()
        orc._slack = MagicMock()
        orc.stop()

        orc._audit.flush.assert_called_once()
        orc._audit.log_system.assert_called()
        orc._slack.send_system_status.assert_called_with("종료")
