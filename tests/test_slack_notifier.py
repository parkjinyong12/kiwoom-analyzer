"""
Slack Notifier Agent 단위 테스트 (WebhookClient 방식).
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agents.slack_notifier import (
    BlockKitBuilder,
    DailySummary,
    SendResult,
    SlackNotifierAgent,
)
from models import RiskCheckResult, TradeSignal


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def make_trade_signal(direction: str = "BUY", confidence: float = 0.78) -> TradeSignal:
    return TradeSignal(
        ticker="005930", signal=direction, confidence=confidence,
        strategy_name="골든크로스", reasons=["MA5 > MA20 골든크로스", "거래량 180% 증가"],
        timeframe="D", timestamp=datetime.now(),
        price=75200.0, target_price=78000.0, stop_loss=73500.0,
    )


def make_risk_result(direction: str = "BUY", approved: bool = True, risk_level: str = "LOW", confidence: float = 0.78) -> RiskCheckResult:
    return RiskCheckResult(
        signal=make_trade_signal(direction=direction, confidence=confidence),
        approved=approved,
        block_reasons=[] if approved else ["테스트 차단"],
        risk_level=risk_level,
        adjusted_confidence=confidence,
    )


@pytest.fixture
def agent_dry():
    """webhook_url 없는 dry-run 에이전트."""
    with patch("agents.slack_notifier.config") as mock_cfg:
        mock_cfg.slack.webhook_url = ""
        mock_cfg.slack.channel = "#stock-agent-message"
        yield SlackNotifierAgent()


@pytest.fixture
def agent_live():
    """Mock WebhookClient를 가진 에이전트."""
    with patch("agents.slack_notifier.config") as mock_cfg:
        mock_cfg.slack.webhook_url = "https://hooks.slack.com/test"
        mock_cfg.slack.channel = "#stock-agent-message"
        with patch("agents.slack_notifier.WebhookClient") as MockClient:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.body = "ok"
            mock_client.send.return_value = mock_resp
            MockClient.return_value = mock_client
            agent = SlackNotifierAgent()
            agent._mock_client = mock_client
            yield agent


# ---------------------------------------------------------------------------
# BlockKitBuilder
# ---------------------------------------------------------------------------

class TestBlockKitBuilder:

    def setup_method(self):
        self.builder = BlockKitBuilder()

    def test_buy_신호_블록_생성(self):
        result = make_risk_result("BUY")
        payload = self.builder.build_trade_signal(result)
        text = payload["attachments"][0]["blocks"][0]["text"]["text"]
        assert "매수" in text
        assert "75,200" in text
        assert "78,000" in text

    def test_sell_신호_블록_생성(self):
        result = make_risk_result("SELL")
        payload = self.builder.build_trade_signal(result)
        text = payload["attachments"][0]["blocks"][0]["text"]["text"]
        assert "매도" in text

    def test_리스크_컬러_low(self):
        result = make_risk_result(risk_level="LOW")
        payload = self.builder.build_trade_signal(result)
        assert payload["attachments"][0]["color"] == "#2EB67D"

    def test_리스크_컬러_high(self):
        result = make_risk_result(risk_level="HIGH")
        payload = self.builder.build_trade_signal(result)
        assert payload["attachments"][0]["color"] == "#E01E5A"

    def test_근거_목록_포함(self):
        result = make_risk_result()
        payload = self.builder.build_trade_signal(result)
        reasons_block = payload["attachments"][0]["blocks"][2]["text"]["text"]
        assert "골든크로스" in reasons_block

    def test_에러_블록_생성(self):
        payload = self.builder.build_error("API 연결 실패", "timeout")
        text = payload["attachments"][0]["blocks"][0]["text"]["text"]
        assert "시스템 에러" in text
        assert "API 연결 실패" in text

    def test_일일_요약_블록(self):
        summary = DailySummary(date="2025-01-15", buy_count=3, sell_count=1, blocked_count=5)
        payload = self.builder.build_daily_summary(summary)
        text = payload["blocks"][0]["text"]["text"]
        assert "2025-01-15" in text
        assert "매수 3건" in text
        assert "차단: 5건" in text


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------

class TestSlackNotifierDryRun:

    def test_미설정_dry_run_성공(self, agent_dry):
        assert not agent_dry.is_configured
        result = agent_dry.send_signal(make_risk_result())
        assert result.success is True
        assert result.message_ts == "dry-run"

    def test_미승인_신호_스킵(self, agent_dry):
        result = agent_dry.send_signal(make_risk_result(approved=False))
        assert result.success is False
        assert "미승인" in result.error

    def test_에러_알림_dry_run(self, agent_dry):
        result = agent_dry.send_error("테스트 에러", "detail")
        assert result.success is True

    def test_일일_요약_dry_run(self, agent_dry):
        result = agent_dry.send_daily_summary()
        assert result.success is True


# ---------------------------------------------------------------------------
# live (mock WebhookClient)
# ---------------------------------------------------------------------------

class TestSlackNotifierLive:

    def test_신호_발송_성공(self, agent_live):
        result = agent_live.send_signal(make_risk_result())
        assert result.success is True
        assert result.channel == "#stock-agent-message"

    def test_에러_발송(self, agent_live):
        result = agent_live.send_error("오류", "traceback")
        assert result.success is True

    def test_요약_발송(self, agent_live):
        result = agent_live.send_daily_summary()
        assert result.success is True

    def test_발송_성공시_summary_카운트(self, agent_live):
        agent_live.send_signal(make_risk_result("BUY"))
        agent_live.send_signal(make_risk_result("BUY"))
        agent_live.send_signal(make_risk_result("SELL"))
        assert agent_live.summary.buy_count == 2
        assert agent_live.summary.sell_count == 1

    def test_미승인_blocked_카운트(self, agent_live):
        agent_live.send_signal(make_risk_result(approved=False))
        agent_live.send_signal(make_risk_result(approved=False))
        assert agent_live.summary.blocked_count == 2

    def test_http_실패_재시도_3회(self, agent_live):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.body = "error"
        agent_live._mock_client.send.return_value = mock_resp
        result = agent_live.send_signal(make_risk_result())
        assert result.success is False
        assert result.attempts == 3
        assert agent_live._mock_client.send.call_count == 3

    def test_1회_성공(self, agent_live):
        result = agent_live.send_signal(make_risk_result())
        assert result.success is True
        assert result.attempts == 1


# ---------------------------------------------------------------------------
# DailySummary
# ---------------------------------------------------------------------------

class TestDailySummary:

    def test_카운트_집계(self):
        summary = DailySummary()
        summary.record_signal(make_trade_signal("BUY"))
        summary.record_signal(make_trade_signal("BUY"))
        summary.record_signal(make_trade_signal("SELL"))
        summary.record_block()
        summary.record_error()
        assert summary.buy_count == 2
        assert summary.sell_count == 1
        assert summary.blocked_count == 1
        assert summary.error_count == 1

    def test_reset(self):
        summary = DailySummary()
        summary.buy_count = 5
        summary.reset()
        assert summary.buy_count == 0
