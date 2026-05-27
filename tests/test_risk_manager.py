"""
Risk Manager Agent 단위 테스트.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
KST = ZoneInfo("Asia/Seoul")

import pytest

from agents.risk_manager import CooldownStore, MarketContext, RiskManagerAgent
from models import TradeSignal


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def make_signal(
    ticker: str = "005930",
    direction: str = "BUY",
    confidence: float = 0.75,
) -> TradeSignal:
    return TradeSignal(
        ticker=ticker,
        signal=direction,
        confidence=confidence,
        strategy_name="테스트전략",
        reasons=["테스트 근거"],
        timeframe="D",
        timestamp=datetime.now(),
        price=70000.0,
        target_price=72000.0,
        stop_loss=68500.0,
    )


# ---------------------------------------------------------------------------
# CooldownStore
# ---------------------------------------------------------------------------

class TestCooldownStore:

    def test_초기_상태_쿨다운_없음(self):
        store = CooldownStore()
        in_cd, remaining = store.is_in_cooldown("005930", "BUY", 4)
        assert in_cd is False
        assert remaining is None

    def test_기록_후_쿨다운_활성(self):
        store = CooldownStore()
        store.record("005930", "BUY")
        in_cd, remaining = store.is_in_cooldown("005930", "BUY", 4)
        assert in_cd is True
        assert remaining is not None and remaining > 3.9

    def test_다른_방향은_쿨다운_무관(self):
        store = CooldownStore()
        store.record("005930", "BUY")
        in_cd, _ = store.is_in_cooldown("005930", "SELL", 4)
        assert in_cd is False

    def test_다른_종목은_쿨다운_무관(self):
        store = CooldownStore()
        store.record("005930", "BUY")
        in_cd, _ = store.is_in_cooldown("000660", "BUY", 4)
        assert in_cd is False

    def test_clear_특정_종목(self):
        store = CooldownStore()
        store.record("005930", "BUY")
        store.record("000660", "BUY")
        store.clear("005930")
        in_cd1, _ = store.is_in_cooldown("005930", "BUY", 4)
        in_cd2, _ = store.is_in_cooldown("000660", "BUY", 4)
        assert in_cd1 is False
        assert in_cd2 is True

    def test_clear_전체(self):
        store = CooldownStore()
        store.record("005930", "BUY")
        store.record("000660", "BUY")
        store.clear()
        assert store.is_in_cooldown("005930", "BUY", 4)[0] is False
        assert store.is_in_cooldown("000660", "BUY", 4)[0] is False


# ---------------------------------------------------------------------------
# MarketContext
# ---------------------------------------------------------------------------

class TestMarketContext:

    def test_초기_상태_급락_아님(self):
        ctx = MarketContext()
        assert ctx.is_market_crash(-2.0) is False

    def test_급락_감지(self):
        ctx = MarketContext()
        ctx.update(-2.5)
        assert ctx.is_market_crash(-2.0) is True

    def test_하락_기준_미달(self):
        ctx = MarketContext()
        ctx.update(-1.5)
        assert ctx.is_market_crash(-2.0) is False

    def test_오래된_데이터_스킵(self):
        ctx = MarketContext()
        ctx.update(-3.0)
        # 강제로 업데이트 시간을 31분 전으로 조작
        ctx._updated_at = datetime.now(tz=KST) - timedelta(minutes=31)
        assert ctx.is_market_crash(-2.0) is False


# ---------------------------------------------------------------------------
# RiskManagerAgent.check
# ---------------------------------------------------------------------------

class TestRiskManagerCheck:

    def setup_method(self):
        self.agent = RiskManagerAgent()

    def test_정상_buy_승인(self):
        s = make_signal("005930", "BUY", confidence=0.75)
        result = self.agent.check(s)
        assert result.approved is True
        assert result.block_reasons == []

    def test_hold_차단(self):
        s = make_signal("005930", "HOLD", confidence=0.8)
        result = self.agent.check(s)
        assert result.approved is False
        assert any("HOLD" in r for r in result.block_reasons)

    def test_낮은_confidence_차단(self):
        s = make_signal("005930", "BUY", confidence=0.50)
        result = self.agent.check(s)
        assert result.approved is False
        assert any("신뢰도" in r for r in result.block_reasons)

    def test_confidence_경계값_통과(self):
        # config.risk.min_confidence = 0.65
        s = make_signal("005930", "BUY", confidence=0.65)
        result = self.agent.check(s)
        assert result.approved is True

    def test_buy_쿨다운_차단(self):
        s = make_signal("005930", "BUY", confidence=0.75)
        # 첫 번째 승인 → 쿨다운 기록
        r1 = self.agent.check(s)
        assert r1.approved is True
        # 두 번째 동일 신호 → 쿨다운 차단
        r2 = self.agent.check(s)
        assert r2.approved is False
        assert any("쿨다운" in r for r in r2.block_reasons)

    def test_sell_쿨다운_없음(self):
        # BUY 쿨다운이 걸려있어도 SELL은 통과
        self.agent.cooldown.record("005930", "BUY")
        s = make_signal("005930", "SELL", confidence=0.75)
        result = self.agent.check(s)
        assert result.approved is True

    def test_sell_연속_승인(self):
        s = make_signal("005930", "SELL", confidence=0.75)
        r1 = self.agent.check(s)
        r2 = self.agent.check(s)
        assert r1.approved is True
        assert r2.approved is True

    def test_시장_급락_buy_차단(self):
        self.agent.market_ctx.update(-3.0)
        s = make_signal("005930", "BUY", confidence=0.80)
        result = self.agent.check(s)
        assert result.approved is False
        assert any("급락" in r for r in result.block_reasons)

    def test_시장_급락에도_sell_통과(self):
        self.agent.market_ctx.update(-3.0)
        s = make_signal("005930", "SELL", confidence=0.75)
        result = self.agent.check(s)
        assert result.approved is True

    def test_다른_종목_독립_쿨다운(self):
        # 005930 BUY 쿨다운
        r1 = self.agent.check(make_signal("005930", "BUY", 0.75))
        assert r1.approved is True
        # 000660 BUY는 무관
        r2 = self.agent.check(make_signal("000660", "BUY", 0.75))
        assert r2.approved is True


# ---------------------------------------------------------------------------
# 리스크 레벨 & confidence 조정
# ---------------------------------------------------------------------------

class TestRiskLevel:

    def setup_method(self):
        self.agent = RiskManagerAgent()

    def test_high_confidence_low_risk(self):
        s = make_signal(confidence=0.85)
        result = self.agent.check(s)
        assert result.risk_level == "LOW"
        assert result.adjusted_confidence == 0.85

    def test_medium_confidence_medium_risk(self):
        s = make_signal(confidence=0.72)
        result = self.agent.check(s)
        assert result.risk_level == "MEDIUM"
        assert result.adjusted_confidence == round(0.72 - 0.02, 4)

    def test_low_confidence_high_risk(self):
        # 0.65 이상이어야 승인, 0.65~0.69는 HIGH 리스크
        s = make_signal(confidence=0.67)
        result = self.agent.check(s)
        assert result.risk_level == "HIGH"
        assert result.adjusted_confidence == round(0.67 - 0.05, 4)


# ---------------------------------------------------------------------------
# check_batch
# ---------------------------------------------------------------------------

class TestCheckBatch:

    def test_배치_처리(self):
        agent = RiskManagerAgent()
        signals = [
            make_signal("005930", "BUY", 0.75),
            make_signal("000660", "BUY", 0.50),   # confidence 부족 → BLOCK
            make_signal("035420", "SELL", 0.80),
        ]
        results = agent.check_batch(signals)
        assert len(results) == 3
        assert results[0].approved is True
        assert results[1].approved is False
        assert results[2].approved is True
