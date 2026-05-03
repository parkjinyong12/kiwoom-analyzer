"""
Audit / Monitor Agent 단위 테스트.
실제 SQLite DB를 임시 파일로 생성해서 테스트.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agents.audit_monitor import (
    AuditDB,
    AuditEvent,
    AuditMonitorAgent,
    AsyncWriter,
)
from models import RiskCheckResult, TradeSignal


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    db_path = str(tmp_path / "test_audit.db")
    return AuditDB(db_path)


@pytest.fixture
def agent(tmp_path):
    with patch("agents.audit_monitor.config") as mock_cfg:
        mock_cfg.db_path = str(tmp_path / "test_audit.db")
        a = AuditMonitorAgent()
        yield a
        a.flush()


def make_trade_signal(direction: str = "BUY") -> TradeSignal:
    return TradeSignal(
        ticker="005930",
        signal=direction,
        confidence=0.75,
        strategy_name="골든크로스",
        reasons=["테스트"],
        timeframe="D",
        timestamp=datetime.now(),
        price=70000.0,
        target_price=72000.0,
        stop_loss=68500.0,
    )


def make_risk_result(approved: bool = True) -> RiskCheckResult:
    return RiskCheckResult(
        signal=make_trade_signal(),
        approved=approved,
        block_reasons=[] if approved else ["신뢰도 부족"],
        risk_level="LOW",
        adjusted_confidence=0.75,
    )


# ---------------------------------------------------------------------------
# AuditDB
# ---------------------------------------------------------------------------

class TestAuditDB:

    def test_스키마_초기화(self, tmp_db):
        import sqlite3
        conn = sqlite3.connect(tmp_db._path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "events" in tables
        assert "signals" in tables

    def test_이벤트_삽입_조회(self, tmp_db):
        event = AuditEvent(
            event_type="DATA_FETCH",
            agent="market_data",
            ticker="005930",
            status="SUCCESS",
            data={"bar_count": 200},
        )
        tmp_db.insert_event(event)
        count = tmp_db.get_daily_event_count()
        assert count.get("DATA_FETCH_SUCCESS", 0) == 1

    def test_신호_삽입(self, tmp_db):
        signal = make_trade_signal("BUY")
        tmp_db.insert_signal(signal)
        stats = tmp_db.get_signal_stats(days=1)
        assert stats.get("BUY", 0) == 1

    def test_에러_조회(self, tmp_db):
        event = AuditEvent(
            event_type="ERROR",
            agent="market_data",
            ticker="005930",
            status="FAIL",
            data={"title": "TR 실패"},
        )
        tmp_db.insert_event(event)
        errors = tmp_db.get_recent_errors(hours=1)
        assert len(errors) == 1
        assert errors[0]["agent"] == "market_data"

    def test_오래된_이벤트_삭제(self, tmp_db):
        import sqlite3
        from datetime import timedelta
        old_ts = (datetime.now() - timedelta(days=91)).isoformat()
        conn = sqlite3.connect(tmp_db._path)
        conn.execute(
            "INSERT INTO events (timestamp, event_type, agent, status) VALUES (?,?,?,?)",
            (old_ts, "DATA_FETCH", "market_data", "SUCCESS"),
        )
        conn.commit()
        conn.close()

        deleted = tmp_db.purge_old_events(days=90)
        assert deleted == 1

    def test_최신_이벤트_삭제_안됨(self, tmp_db):
        event = AuditEvent(
            event_type="SYSTEM", agent="orchestrator", status="SUCCESS"
        )
        tmp_db.insert_event(event)
        deleted = tmp_db.purge_old_events(days=90)
        assert deleted == 0


# ---------------------------------------------------------------------------
# AuditMonitorAgent 로깅
# ---------------------------------------------------------------------------

class TestAuditMonitorLogging:

    def test_data_fetch_성공_기록(self, agent):
        agent.log_data_fetch("005930", success=True, bar_count=200)
        agent.flush()
        stats = agent._db.get_daily_event_count()
        assert stats.get("DATA_FETCH_SUCCESS", 0) == 1

    def test_data_fetch_실패_기록(self, agent):
        agent.log_data_fetch("005930", success=False, error="timeout")
        agent.flush()
        errors = agent._db.get_recent_errors(hours=1)
        assert any(e["event_type"] == "DATA_FETCH" for e in errors)

    def test_신호_기록(self, agent):
        agent.log_signal(make_trade_signal("BUY"))
        agent.flush()
        stats = agent._db.get_signal_stats(days=1)
        assert stats.get("BUY", 0) == 1

    def test_hold_신호는_signals_테이블_미저장(self, agent):
        agent.log_signal(make_trade_signal("HOLD"))
        agent.flush()
        stats = agent._db.get_signal_stats(days=1)
        assert stats.get("HOLD", 0) == 0

    def test_risk_check_승인_기록(self, agent):
        agent.log_risk_check(make_risk_result(approved=True))
        agent.flush()
        stats = agent._db.get_daily_event_count()
        assert stats.get("RISK_CHECK_SUCCESS", 0) == 1

    def test_risk_check_차단_기록(self, agent):
        agent.log_risk_check(make_risk_result(approved=False))
        agent.flush()
        stats = agent._db.get_daily_event_count()
        assert stats.get("RISK_CHECK_BLOCKED", 0) == 1

    def test_에러_slack_알림_전달(self, tmp_path):
        mock_slack = MagicMock()
        with patch("agents.audit_monitor.config") as mock_cfg:
            mock_cfg.db_path = str(tmp_path / "test.db")
            a = AuditMonitorAgent(slack_notifier=mock_slack)
            a.log_error("market_data", "TR 실패", "timeout")
            a.flush()
        mock_slack.send_error.assert_called_once_with("TR 실패", "timeout")

    def test_에러_slack_없으면_조용히(self, agent):
        # slack 없어도 예외 발생 안 함
        agent.log_error("market_data", "TR 실패")
        agent.flush()

    def test_notification_기록(self, agent):
        agent.log_notification("005930", "#stock-signals", success=True)
        agent.flush()
        stats = agent._db.get_daily_event_count()
        assert stats.get("NOTIFICATION_SUCCESS", 0) == 1

    def test_get_stats_구조(self, agent):
        stats = agent.get_stats()
        assert "today" in stats
        assert "signals_30d" in stats
        assert "recent_errors" in stats

    def test_purge_old_logs(self, agent):
        # 예외 없이 실행되는지 확인
        agent.purge_old_logs()


# ---------------------------------------------------------------------------
# AsyncWriter — 비동기 처리
# ---------------------------------------------------------------------------

class TestAsyncWriter:

    def test_큐_드롭_없이_처리(self, tmp_db):
        writer = AsyncWriter(tmp_db)
        for i in range(10):
            writer.put_event(AuditEvent(
                event_type="SYSTEM",
                agent="test",
                status="SUCCESS",
                data={"i": i},
            ))
        writer.flush()
        count = tmp_db.get_daily_event_count()
        assert count.get("SYSTEM_SUCCESS", 0) == 10
