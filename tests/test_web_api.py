"""
Web API 응답 구조 통합 테스트.
Flask test client를 사용해 DB 연결 없이 응답 스키마/타입을 검증.
DB 의존 엔드포인트는 쿼리를 패치해서 고정 픽스처로 대체.
"""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch

import pytest

# web 디렉토리의 app을 임포트
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

from app import app as flask_app


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    flask_app.config["SECRET_KEY"] = "test-secret"
    with flask_app.test_client() as c:
        with flask_app.test_request_context():
            with c.session_transaction() as sess:
                sess["user_id"] = 1
        yield c


DASHBOARD_FIXTURE = {
    "watched_count": 42,
    "signals_today": 3,
    "errors_today": 0,
    "supply_alerts_today": 1,
    "recent_signals": [
        {
            "ticker": "005930",
            "stock_name": "삼성전자",
            "signal": "BUY",
            "price": "75,200",
            "confidence_pct": "78%",
            "strategy": "골든크로스",
            "ts": "05/05 09:30",
        }
    ],
    "signal_stats_30d": {"BUY": 12, "SELL": 5, "HOLD": 8},
}

USERS_FIXTURE = [
    {
        "id": 1,
        "name": "테스트유저",
        "login_id": "test",
        "visible_menus": ["dashboard", "supply", "signals"],
        "supply_default_stock": "",
        "supply_default_period": 500,
    }
]

SIGNALS_FIXTURE = [
    {
        "ticker": "005930",
        "stock_name": "삼성전자",
        "signal": "BUY",
        "price": "75,200",
        "target_price": "78,000",
        "stop_loss": "73,500",
        "confidence": "78%",
        "strategy": "골든크로스",
        "ts": "2026-05-05 09:30",
    }
]

EVENTS_FIXTURE = [
    {
        "ts": "05/05 09:30:00",
        "event_type": "SIGNAL",
        "agent": "strategy",
        "ticker": "005930",
        "status": "SUCCESS",
        "detail": "BUY 신호 생성",
    }
]

STOCKS_FIXTURE = [
    {
        "stock_code": "005930",
        "stock_name": "삼성전자",
        "market_name": "KOSPI",
        "last_price": "75,200",
        "market_cap": "4,489,344억",
        "fetched_at": "2026-05-05",
    }
]

BATCH_FIXTURE = [
    {
        "id": "collect_history",
        "name": "수급 히스토리 수집",
        "desc": "시총 5조 이상 종목 수급 500일치 수집",
        "running": False,
        "pid": None,
        "last_line": "",
        "log_file": None,
    }
]

SUPPLY_SUMMARY_FIXTURE = {
    "collected_stocks": 120,
    "total_rows": 54321,
    "avg_days_per_stock": 450,
    "watched_without_data": 2,
}


# ---------------------------------------------------------------------------
# /api/dashboard
# ---------------------------------------------------------------------------

class TestDashboardApi:
    def test_200_and_required_fields(self, client):
        with patch("app.query_one", return_value={"cnt": 42}), \
             patch("app.query", return_value=[]):
            res = client.get("/api/dashboard")
        assert res.status_code == 200
        d = res.get_json()
        for field in ("watched_count", "signals_today", "errors_today",
                      "supply_alerts_today", "recent_signals", "signal_stats_30d"):
            assert field in d, f"필드 누락: {field}"

    def test_watched_count_is_int(self, client):
        with patch("app.query_one", return_value={"cnt": 10}), \
             patch("app.query", return_value=[]):
            res = client.get("/api/dashboard")
        d = res.get_json()
        assert isinstance(d["watched_count"], int)

    def test_recent_signals_is_list(self, client):
        with patch("app.query_one", return_value={"cnt": 0}), \
             patch("app.query", return_value=[]):
            res = client.get("/api/dashboard")
        d = res.get_json()
        assert isinstance(d["recent_signals"], list)

    def test_signal_stats_is_dict(self, client):
        with patch("app.query_one", return_value={"cnt": 0}), \
             patch("app.query", return_value=[]):
            res = client.get("/api/dashboard")
        d = res.get_json()
        assert isinstance(d["signal_stats_30d"], dict)

    def test_recent_signal_item_fields(self, client):
        """신호 항목에 프론트엔드가 사용하는 필드가 모두 있는지 확인."""
        fake_row = {
            "ticker": "005930", "stock_name": "삼성전자",
            "signal": "BUY", "price": 75200, "confidence": 0.78,
            "strategy": "골든크로스",
            "ts": datetime(2026, 5, 5, 9, 30),
        }
        with patch("app.query_one", return_value={"cnt": 1}), \
             patch("app.query", side_effect=[fake_row if False else [fake_row], []]):
            res = client.get("/api/dashboard")
        d = res.get_json()
        if d["recent_signals"]:
            item = d["recent_signals"][0]
            for field in ("ticker", "stock_name", "signal", "price",
                          "confidence_pct", "strategy", "ts"):
                assert field in item, f"신호 항목 필드 누락: {field}"

    def test_confidence_zero_not_dash(self, client):
        """신뢰도 0.0 이 '-' 가 아닌 '0%' 로 반환되어야 한다."""
        fake_row = {
            "ticker": "005930", "stock_name": "삼성전자",
            "signal": "BUY", "price": 75200, "confidence": 0.0,
            "strategy": "test", "ts": datetime(2026, 5, 5, 9, 30),
        }
        with patch("app.query_one", return_value={"cnt": 0}), \
             patch("app.query", side_effect=[[fake_row], []]):
            res = client.get("/api/dashboard")
        d = res.get_json()
        assert d["recent_signals"][0]["confidence_pct"] == "0%"

    def test_price_none_returns_dash(self, client):
        """price None 이면 '-' 반환."""
        fake_row = {
            "ticker": "005930", "stock_name": "삼성전자",
            "signal": "BUY", "price": None, "confidence": None,
            "strategy": None, "ts": datetime(2026, 5, 5, 9, 30),
        }
        with patch("app.query_one", return_value={"cnt": 0}), \
             patch("app.query", side_effect=[[fake_row], []]):
            res = client.get("/api/dashboard")
        d = res.get_json()
        assert d["recent_signals"][0]["price"] == "-"
        assert d["recent_signals"][0]["confidence_pct"] == "-"


# ---------------------------------------------------------------------------
# /api/users
# ---------------------------------------------------------------------------

class TestUsersApi:
    def test_200_and_list(self, client):
        with patch("app.query", return_value=[{"id": 1, "name": "admin", "login_id": "admin"}]), \
             patch("app._get_user_prefs", return_value={}):
            res = client.get("/api/users")
        assert res.status_code == 200
        assert isinstance(res.get_json(), list)

    def test_user_item_fields(self, client):
        with patch("app.query", return_value=[{"id": 1, "name": "admin", "login_id": "admin"}]), \
             patch("app._get_user_prefs", return_value={}):
            res = client.get("/api/users")
        item = res.get_json()[0]
        for field in ("id", "name", "login_id", "visible_menus",
                      "supply_default_stock", "supply_default_period"):
            assert field in item, f"users 항목 필드 누락: {field}"

    def test_visible_menus_is_list(self, client):
        with patch("app.query", return_value=[{"id": 1, "name": "admin", "login_id": "admin"}]), \
             patch("app._get_user_prefs", return_value={}):
            res = client.get("/api/users")
        assert isinstance(res.get_json()[0]["visible_menus"], list)


# ---------------------------------------------------------------------------
# /api/signals
# ---------------------------------------------------------------------------

class TestSignalsApi:
    def test_200_and_list(self, client):
        with patch("app.query", return_value=[]):
            res = client.get("/api/signals")
        assert res.status_code == 200
        assert isinstance(res.get_json(), list)

    def test_signal_item_fields(self, client):
        fake = {
            "ticker": "005930", "stock_name": "삼성전자",
            "signal": "BUY", "price": 75200, "target_price": 78000,
            "stop_loss": 73500, "confidence": 0.78, "strategy": "골든크로스",
            "ts": datetime(2026, 5, 5, 9, 30),
        }
        with patch("app.query", return_value=[fake]):
            res = client.get("/api/signals")
        item = res.get_json()[0]
        for field in ("ticker", "stock_name", "signal", "price",
                      "target_price", "stop_loss", "confidence", "strategy", "ts"):
            assert field in item, f"signals 항목 필드 누락: {field}"


# ---------------------------------------------------------------------------
# /api/events
# ---------------------------------------------------------------------------

class TestEventsApi:
    def test_200_and_list(self, client):
        with patch("app.query", return_value=[]):
            res = client.get("/api/events")
        assert res.status_code == 200
        assert isinstance(res.get_json(), list)

    def test_event_item_fields(self, client):
        fake = {
            "event_type": "SIGNAL", "agent": "strategy", "ticker": "005930",
            "status": "SUCCESS", "data": {"detail": "BUY"},
            "ts": datetime(2026, 5, 5, 9, 30),
        }
        with patch("app.query", return_value=[fake]):
            res = client.get("/api/events")
        item = res.get_json()[0]
        for field in ("ts", "event_type", "agent", "ticker", "status", "detail"):
            assert field in item, f"events 항목 필드 누락: {field}"


# ---------------------------------------------------------------------------
# /api/supply_demand/summary
# ---------------------------------------------------------------------------

class TestSupplySummaryApi:
    def test_200_and_fields(self, client):
        with patch("app.query_one", return_value={"cnt": 0, "avg": 0}):
            res = client.get("/api/supply_demand/summary")
        assert res.status_code == 200
        d = res.get_json()
        for field in ("collected_stocks", "total_rows",
                      "avg_days_per_stock", "watched_without_data"):
            assert field in d, f"supply summary 필드 누락: {field}"

    def test_values_are_numeric(self, client):
        with patch("app.query_one", return_value={"cnt": 5, "avg": 10}):
            res = client.get("/api/supply_demand/summary")
        d = res.get_json()
        for field in ("collected_stocks", "total_rows",
                      "avg_days_per_stock", "watched_without_data"):
            assert isinstance(d[field], (int, float)), f"{field} 숫자 타입 아님"


# ---------------------------------------------------------------------------
# /api/batch
# ---------------------------------------------------------------------------

class TestBatchApi:
    def test_200_and_list(self, client):
        res = client.get("/api/batch")
        assert res.status_code == 200
        assert isinstance(res.get_json(), list)

    def test_batch_item_fields(self, client):
        res = client.get("/api/batch")
        items = res.get_json()
        assert len(items) > 0
        for field in ("id", "name", "desc", "running", "pid", "last_line", "log_file"):
            assert field in items[0], f"batch 항목 필드 누락: {field}"

    def test_running_is_bool(self, client):
        res = client.get("/api/batch")
        assert isinstance(res.get_json()[0]["running"], bool)
