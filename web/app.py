"""
Flask 웹 대시보드
PostgreSQL DB를 직접 읽어 분석 현황을 시각화.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

import psycopg2
import psycopg2.extras
import json
import re
import subprocess
import glob
import signal
import shlex
import logging
import threading
import secrets
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, jsonify, render_template, request, session, redirect
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import config

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "kiwoom-analyzer-secret-change-in-prod")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _ensure_spec_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS spec_document (
                id         INTEGER PRIMARY KEY DEFAULT 1,
                content    TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT NOW(),
                CHECK (id = 1)
            )
        """)


def _sync_spec_to_db():
    """SPEC.md가 있으면 DB에 동기화 (앱 시작 시 호출)."""
    if not os.path.exists(_SPEC_FILE):
        return
    try:
        with open(_SPEC_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return
        with get_conn() as conn:
            conn.cursor().execute("""
                INSERT INTO spec_document (id, content, updated_at)
                VALUES (1, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content, updated_at = NOW()
            """, (content,))
    except Exception as e:
        logging.warning("[spec] SPEC.md → DB 동기화 실패: %s", e)


def _ensure_app_settings_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   VARCHAR(100) PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)


def _ensure_batch_schedules_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS batch_schedules (
                job_id           VARCHAR(50) PRIMARY KEY,
                enabled          BOOLEAN NOT NULL DEFAULT FALSE,
                hour             SMALLINT NOT NULL DEFAULT 9,
                minute           SMALLINT NOT NULL DEFAULT 0,
                days             VARCHAR(20) NOT NULL DEFAULT 'weekdays',
                interval_mode    BOOLEAN NOT NULL DEFAULT FALSE,
                interval_minutes INTEGER NOT NULL DEFAULT 60,
                last_run         TIMESTAMP,
                updated_at       TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE batch_schedules ADD COLUMN IF NOT EXISTS interval_mode    BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE batch_schedules ADD COLUMN IF NOT EXISTS interval_minutes INTEGER NOT NULL DEFAULT 60")
        cur.execute("ALTER TABLE batch_schedules ADD COLUMN IF NOT EXISTS interval_start   SMALLINT NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE batch_schedules ADD COLUMN IF NOT EXISTS interval_end     SMALLINT NOT NULL DEFAULT 1439")


def _ensure_users_auth_columns():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS login_id VARCHAR(50) UNIQUE")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)")
        cur.execute("ALTER TABLE supply_demand ADD COLUMN IF NOT EXISTS close_price BIGINT")


def _ensure_user_preferences_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id  INTEGER NOT NULL,
                key      VARCHAR(100) NOT NULL,
                value    TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            )
        """)


def _ensure_report_tables():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS report_email_config (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL UNIQUE,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS report_send_log (
                id SERIAL PRIMARY KEY,
                sent_at TIMESTAMP DEFAULT NOW(),
                recipients TEXT,
                stock_count INT,
                status VARCHAR(20),
                error_msg TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_alert_emails (
                id         SERIAL PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                email      VARCHAR(255) NOT NULL,
                active     BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, email)
            )
        """)


def _ensure_manual_holdings_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS manual_holdings (
                id          SERIAL PRIMARY KEY,
                brokerage   VARCHAR(50)    NOT NULL DEFAULT '',
                stock_code  VARCHAR(10)    NOT NULL,
                stock_name  VARCHAR(100)   NOT NULL DEFAULT '',
                quantity    BIGINT         NOT NULL DEFAULT 0,
                avg_price   NUMERIC(15, 2) NOT NULL DEFAULT 0,
                memo        TEXT           DEFAULT '',
                created_at  TIMESTAMP      DEFAULT NOW(),
                updated_at  TIMESTAMP      DEFAULT NOW()
            )
        """)


def _ensure_trade_history_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id               SERIAL PRIMARY KEY,
                user_id          INTEGER        REFERENCES users(id),
                stock_code       VARCHAR(20)    NOT NULL,
                stock_name       VARCHAR(100)   NOT NULL DEFAULT '',
                direction        VARCHAR(4)     NOT NULL,
                brokerage        VARCHAR(50)    NOT NULL DEFAULT '',
                quantity         BIGINT         NOT NULL,
                price            NUMERIC(15, 2) NOT NULL,
                amount           BIGINT         NOT NULL,
                avg_price_before NUMERIC(15, 2),
                realized_pnl     BIGINT,
                source           VARCHAR(30)    NOT NULL DEFAULT 'manual',
                executed_at      TIMESTAMP      NOT NULL DEFAULT NOW()
            )
        """)


_DEFAULT_BROKERAGES = [
    ("BROKERAGE", "MAS",  "미래에셋증권",   1),
    ("BROKERAGE", "NH",   "NH투자증권",     2),
    ("BROKERAGE", "SS",   "삼성증권",       3),
    ("BROKERAGE", "KIS",  "한국투자증권",   4),
    ("BROKERAGE", "KB",   "KB증권",         5),
    ("BROKERAGE", "SH",   "신한투자증권",   6),
    ("BROKERAGE", "KIW",  "키움증권",       7),
    ("BROKERAGE", "DS",   "대신증권",       8),
    ("BROKERAGE", "HN",   "하나증권",       9),
    ("BROKERAGE", "MZ",   "메리츠증권",    10),
]

_DEFAULT_ASSET_TYPES: list = []  # 자산종류 코드는 사용자가 직접 공통코드 관리에서 추가


def _ensure_market_power_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_power_scores (
                id                   SERIAL       PRIMARY KEY,
                user_id              INTEGER      REFERENCES users(id),
                stock_code           VARCHAR(20)  NOT NULL,
                stock_name           VARCHAR(100) NOT NULL DEFAULT '',
                scored_at            DATE         NOT NULL DEFAULT CURRENT_DATE,
                supply_bottleneck    SMALLINT     NOT NULL DEFAULT 0,
                irreplaceability     SMALLINT     NOT NULL DEFAULT 0,
                pricing_power        SMALLINT     NOT NULL DEFAULT 0,
                demand_visibility    SMALLINT     NOT NULL DEFAULT 0,
                expansion_difficulty SMALLINT     NOT NULL DEFAULT 0,
                customer_lockin      SMALLINT     NOT NULL DEFAULT 0,
                total_score          SMALLINT     NOT NULL DEFAULT 0,
                grade                VARCHAR(2)   NOT NULL DEFAULT '',
                price_attractiveness SMALLINT     DEFAULT NULL,
                earnings_momentum    SMALLINT     DEFAULT NULL,
                composite_score      DECIMAL(6,2) DEFAULT NULL,
                memo                 TEXT         DEFAULT '',
                created_at           TIMESTAMP    NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, stock_code, scored_at)
            )
        """)
        # 기존 테이블 마이그레이션
        for col, typedef in [
            ("price_attractiveness", "SMALLINT DEFAULT NULL"),
            ("earnings_momentum",    "SMALLINT DEFAULT NULL"),
            ("composite_score",      "DECIMAL(6,2) DEFAULT NULL"),
        ]:
            try:
                cur.execute(f"ALTER TABLE market_power_scores ADD COLUMN IF NOT EXISTS {col} {typedef}")
            except Exception:
                pass


def _ensure_qualitative_tables():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qualitative_items (
                id          SERIAL       PRIMARY KEY,
                name        VARCHAR(100) NOT NULL,
                category    VARCHAR(50)  DEFAULT '',
                description TEXT         DEFAULT '',
                sort_order  SMALLINT     DEFAULT 0,
                active      BOOLEAN      DEFAULT TRUE,
                created_at  TIMESTAMP    DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qualitative_scores (
                id         SERIAL       PRIMARY KEY,
                item_id    INTEGER      NOT NULL REFERENCES qualitative_items(id) ON DELETE CASCADE,
                score      DECIMAL(5,1) NOT NULL,
                scored_at  DATE         NOT NULL DEFAULT CURRENT_DATE,
                comment    TEXT         DEFAULT '',
                created_at TIMESTAMP    DEFAULT NOW()
            )
        """)


def _ensure_theme_tables():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_themes (
                stock_code VARCHAR(20) PRIMARY KEY,
                themes     TEXT        NOT NULL DEFAULT ''
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS theme_targets (
                theme        VARCHAR(50)   PRIMARY KEY,
                target_ratio DECIMAL(6, 2) NOT NULL DEFAULT 0,
                alert_up     DECIMAL(6, 2),
                alert_down   DECIMAL(6, 2),
                updated_at   TIMESTAMP     DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE theme_targets ADD COLUMN IF NOT EXISTS alert_up   DECIMAL(6, 2)")
        cur.execute("ALTER TABLE theme_targets ADD COLUMN IF NOT EXISTS alert_down DECIMAL(6, 2)")


def _ensure_macro_rates_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS macro_rates (
                id         SERIAL PRIMARY KEY,
                key        VARCHAR(50) UNIQUE NOT NULL,
                name       VARCHAR(100) NOT NULL,
                value      DECIMAL(20, 4),
                unit       VARCHAR(30) DEFAULT '',
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            INSERT INTO macro_rates (key, name, unit)
            VALUES ('USD_KRW', '달러/원 환율', '원/달러')
            ON CONFLICT (key) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO macro_rates (key, name, unit)
            VALUES ('GOLD_KRX', '국내 금 시세', '원/g')
            ON CONFLICT (key) DO NOTHING
        """)


def _ensure_cash_assets_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cash_assets (
                id          SERIAL PRIMARY KEY,
                name        VARCHAR(100) NOT NULL,
                brokerage   VARCHAR(50) NOT NULL DEFAULT '',
                quantity    DECIMAL(20, 4) DEFAULT NULL,
                unit_price  BIGINT DEFAULT NULL,
                amount      BIGINT NOT NULL DEFAULT 0,
                link_type   VARCHAR(20) NOT NULL DEFAULT 'none',
                link_key    VARCHAR(50) NOT NULL DEFAULT '',
                note        TEXT NOT NULL DEFAULT '',
                updated_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE cash_assets ADD COLUMN IF NOT EXISTS link_type      VARCHAR(20) NOT NULL DEFAULT 'none'")
        cur.execute("ALTER TABLE cash_assets ADD COLUMN IF NOT EXISTS link_key       VARCHAR(50) NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE cash_assets ADD COLUMN IF NOT EXISTS brokerage      VARCHAR(50) NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE cash_assets ADD COLUMN IF NOT EXISTS purchase_price   BIGINT       DEFAULT NULL")
        cur.execute("ALTER TABLE cash_assets ADD COLUMN IF NOT EXISTS asset_type_code VARCHAR(20)  NOT NULL DEFAULT ''")


def _ensure_credit_positions_table():
    """신용 포지션 충당금 관리 테이블 (증권사당 1건)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS credit_positions (
                id              SERIAL PRIMARY KEY,
                brokerage       VARCHAR(50)  NOT NULL UNIQUE,
                purchase_amount BIGINT NOT NULL DEFAULT 0,
                loan_amount     BIGINT NOT NULL DEFAULT 0,
                note            TEXT NOT NULL DEFAULT '',
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE credit_positions ADD COLUMN IF NOT EXISTS brokerage VARCHAR(50) NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE credit_positions DROP COLUMN IF EXISTS name")
        cur.execute("""
            DO $$ BEGIN
                ALTER TABLE credit_positions ADD CONSTRAINT credit_positions_brokerage_unique UNIQUE (brokerage);
            EXCEPTION WHEN duplicate_table THEN NULL;
            END $$
        """)


def _current_uid() -> int:
    """현재 세션의 실효 user_id. 관리자가 다른 사용자로 보기 중이면 그 uid 반환."""
    return session.get("view_as_uid") or session["user_id"]


def _backfill_null_user_ids(uid: int) -> None:
    """마이그레이션 미완료 시 user_id=NULL 레코드를 현재 사용자에게 귀속 (on-demand 보정)."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE rebalance_targets SET user_id = %s WHERE user_id IS NULL", (uid,))
            cur.execute("UPDATE theme_targets SET user_id = %s WHERE user_id IS NULL", (uid,))
    except Exception:
        pass


def _get_total_cash(uid: int) -> int:
    """현금성 자산 합계. cash_assets 테이블 우선, 없으면 legacy portfolio_cash_* fallback."""
    rows = query("SELECT COALESCE(SUM(amount), 0) AS total FROM cash_assets WHERE user_id = %s", (uid,))
    ca_total = int(rows[0]["total"]) if rows else 0
    if ca_total > 0:
        return ca_total
    settings = _get_app_settings()
    return sum(int(v or 0) for k, v in settings.items() if k.startswith("portfolio_cash_"))


def _run_migration_step(fn):
    """마이그레이션 단계를 독립 트랜잭션으로 실행. 실패해도 다른 단계에 영향 없음."""
    try:
        with get_conn() as conn:
            fn(conn.cursor())
    except Exception as e:
        logging.warning("[migration] 단계 실패 (무시): %s", e)


def _ensure_user_id_migration():
    """각 테이블 user_id 컬럼 추가 및 기존 데이터 이관.
    단계별 독립 트랜잭션으로 실행 — 한 단계 실패가 다른 단계를 막지 않음."""

    # ── manual_holdings / cash_assets / credit_positions ──────────────────────
    def _step_holdings_col(cur):
        for tbl in ("manual_holdings", "cash_assets", "credit_positions"):
            cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)")
    _run_migration_step(_step_holdings_col)

    def _step_holdings_backfill(cur):
        for tbl in ("manual_holdings", "cash_assets", "credit_positions"):
            cur.execute(f"UPDATE {tbl} SET user_id = (SELECT MIN(id) FROM users WHERE id IS NOT NULL) WHERE user_id IS NULL AND EXISTS (SELECT 1 FROM users)")
    _run_migration_step(_step_holdings_backfill)

    def _step_credit_constraint(cur):
        cur.execute("""
            DO $$ BEGIN
                ALTER TABLE credit_positions DROP CONSTRAINT IF EXISTS credit_positions_brokerage_unique;
            EXCEPTION WHEN others THEN NULL; END $$
        """)
        cur.execute("""
            DO $$ BEGIN
                ALTER TABLE credit_positions ADD CONSTRAINT credit_positions_user_brokerage_unique UNIQUE (user_id, brokerage);
            EXCEPTION WHEN duplicate_object THEN NULL; END $$
        """)
    _run_migration_step(_step_credit_constraint)

    # ── rebalance_targets ─────────────────────────────────────────────────────
    def _step_rb_col(cur):
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)")
    _run_migration_step(_step_rb_col)

    def _step_rb_backfill(cur):
        cur.execute("UPDATE rebalance_targets SET user_id = (SELECT MIN(id) FROM users WHERE id IS NOT NULL) WHERE user_id IS NULL AND EXISTS (SELECT 1 FROM users)")
    _run_migration_step(_step_rb_backfill)

    def _step_rb_constraint(cur):
        cur.execute("""
            DO $$ BEGIN
                ALTER TABLE rebalance_targets DROP CONSTRAINT IF EXISTS rebalance_targets_pkey;
            EXCEPTION WHEN others THEN NULL; END $$
        """)
        cur.execute("""
            DO $$ BEGIN
                ALTER TABLE rebalance_targets ADD CONSTRAINT rebalance_targets_user_stock_unique UNIQUE (user_id, stock_code);
            EXCEPTION WHEN duplicate_object THEN NULL; END $$
        """)
    _run_migration_step(_step_rb_constraint)

    # ── theme_targets ─────────────────────────────────────────────────────────
    def _step_theme_col(cur):
        cur.execute("ALTER TABLE theme_targets ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)")
    _run_migration_step(_step_theme_col)

    def _step_theme_backfill(cur):
        cur.execute("UPDATE theme_targets SET user_id = (SELECT MIN(id) FROM users WHERE id IS NOT NULL) WHERE user_id IS NULL AND EXISTS (SELECT 1 FROM users)")
    _run_migration_step(_step_theme_backfill)

    def _step_theme_constraint(cur):
        cur.execute("""
            DO $$ BEGIN
                ALTER TABLE theme_targets DROP CONSTRAINT IF EXISTS theme_targets_pkey;
            EXCEPTION WHEN others THEN NULL; END $$
        """)
        cur.execute("""
            DO $$ BEGIN
                ALTER TABLE theme_targets ADD CONSTRAINT theme_targets_user_theme_unique UNIQUE (user_id, theme);
            EXCEPTION WHEN duplicate_object THEN NULL; END $$
        """)
    _run_migration_step(_step_theme_constraint)


def _ensure_rebalance_targets_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rebalance_targets (
                stock_code   VARCHAR(20)   PRIMARY KEY,
                target_ratio DECIMAL(6, 2) NOT NULL DEFAULT 0,
                updated_at   TIMESTAMP     DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS alert_up          DECIMAL(6,2) DEFAULT NULL")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS alert_down        DECIMAL(6,2) DEFAULT NULL")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS watch_up          DECIMAL(6,2) DEFAULT NULL")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS watch_down        DECIMAL(6,2) DEFAULT NULL")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS position_tier     VARCHAR(20)  DEFAULT 'MID'")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS max_change_pp     DECIMAL(4,2) DEFAULT 1.5")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS overweight_band_pp DECIMAL(4,2) DEFAULT 3.0")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS review_band_pp    DECIMAL(4,2) DEFAULT 1.5")
        # 역할점수 컬럼 (테마 내 배분 가중치)
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS theme_purity_score          SMALLINT     DEFAULT 0")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS theme_leader_score          SMALLINT     DEFAULT 0")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS bottleneck_centrality_score SMALLINT     DEFAULT 0")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS earnings_sensitivity_score  SMALLINT     DEFAULT 0")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS portfolio_role_score        SMALLINT     DEFAULT 0")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS role_score                 SMALLINT     DEFAULT 0")
        cur.execute("ALTER TABLE rebalance_targets ADD COLUMN IF NOT EXISTS role_weight                DECIMAL(4,2) DEFAULT 1.00")


def _role_weight_from_score(score: int) -> float:
    """역할점수(0~25) → role_weight 변환."""
    if score >= 23: return 1.25
    if score >= 20: return 1.15
    if score >= 17: return 1.05
    if score >= 14: return 1.00
    if score >= 11: return 0.90
    if score >= 8:  return 0.85
    return 0.80


def _ensure_common_codes_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS common_codes (
                id          SERIAL PRIMARY KEY,
                code_group  VARCHAR(50)  NOT NULL,
                code        VARCHAR(50)  NOT NULL,
                name        VARCHAR(100) NOT NULL,
                sort_order  SMALLINT     NOT NULL DEFAULT 0,
                active      BOOLEAN      NOT NULL DEFAULT TRUE,
                created_at  TIMESTAMP    DEFAULT NOW(),
                UNIQUE (code_group, code)
            )
        """)
        for grp, code, name, sort in _DEFAULT_BROKERAGES:
            cur.execute("""
                INSERT INTO common_codes (code_group, code, name, sort_order)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (code_group, code) DO NOTHING
            """, (grp, code, name, sort))
        # 이전에 잘못 자동 삽입된 ASSET_TYPE 기본 코드 제거 (사용자가 직접 추가한 것만 유지)
        _auto_inserted = ["KRW", "USD", "JPY", "GOLD", "ETF", "BOND", "REIT", "LOAN_AVAIL", "OTHER"]
        cur.execute(
            "DELETE FROM common_codes WHERE code_group = 'ASSET_TYPE' AND code = ANY(%s)",
            (_auto_inserted,),
        )

BATCH_JOBS = {
    "collect_history": {
        "name": "수급 히스토리 수집",
        "desc": "시총 5조 이상 종목 수급 500일치 수집",
        "match": "main.py.*collect-history",
        "cmd": "python -u main.py --collect-history --days 500",
        "log_prefix": "supply_collect",
    },
    "backfill_close": {
        "name": "종가 백필",
        "desc": "close_price NULL 종목 종가 채우기",
        "match": "backfill_close_price",
        "cmd": "python -u scripts/backfill_close_price.py",
        "log_prefix": "backfill",
    },
    "holdings_report": {
        "name": "보유종목 리포트",
        "desc": "보유종목 가격·수급 변동 리포트 생성 및 이메일 발송",
        "match": "scripts/holdings_report",
        "cmd": "python -u scripts/holdings_report.py --send",
        "log_prefix": "holdings_report",
    },
    "sync_prices": {
        "name": "현재가 동기화",
        "desc": "타사 보유종목 최신 종가를 키움 API(ka10081)로 조회하여 DB 업데이트",
        "match": "scripts/sync_prices",
        "cmd": "python -u scripts/sync_prices.py",
        "log_prefix": "sync_prices",
    },
    "run_once": {
        "name": "매매신호 갱신",
        "desc": "전 감시종목 파이프라인 1회 실행 (수급 수집 + 차트 분석 + 매매신호 생성)",
        "match": "main.py.*--once",
        "cmd": "python -u main.py --once",
        "log_prefix": "run_once",
    },
    "rebalance_alert": {
        "name": "리밸런싱 알림",
        "desc": "종목/테마 리밸런싱 신호 감지 시 이메일 발송 (신호 변화 시에만 발송)",
        "match": "scripts/rebalance_alert",
        "cmd": "python -u scripts/rebalance_alert.py",
        "log_prefix": "rebalance_alert",
    },
}


def _find_pid(match: str, _ps_lines: list[str] | None = None) -> int | None:
    """매치 패턴으로 실행 중인 프로세스 PID 검색.
    _ps_lines를 전달하면 ps aux를 재실행하지 않고 재사용한다."""
    pattern = re.compile(match)
    # Linux (container): /proc 스캔
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace")
                if pattern.search(cmdline):
                    return int(entry)
            except Exception:
                continue
        return None
    except FileNotFoundError:
        pass
    # macOS 폴백 — ps aux 출력 재사용 (호출자가 한 번만 실행)
    try:
        if _ps_lines is None:
            r = subprocess.run(["ps", "aux"], capture_output=True, text=True)
            _ps_lines = r.stdout.splitlines()[1:]
        for line in _ps_lines:
            if pattern.search(line):
                parts = line.split()
                if len(parts) > 1:
                    return int(parts[1])
    except Exception:
        pass
    return None


def _latest_log(prefix: str) -> str | None:
    # 단일 누적 파일 방식 (신규)
    p = os.path.join(BASE_DIR, "logs", f"{prefix}.log")
    if os.path.exists(p):
        return p
    # 구 타임스탬프 파일 방식 (하위 호환)
    files = sorted(glob.glob(os.path.join(BASE_DIR, "logs", f"{prefix}_*.log")))
    return files[-1] if files else None


def _append_run_separator(log_file: str) -> None:
    ts = datetime.now(tz=KST).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n=== 새 실행 시작 · {ts} ===\n{'='*60}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# DB 헬퍼
# ---------------------------------------------------------------------------

def get_conn():
    return psycopg2.connect(
        config.database_url,
        cursor_factory=psycopg2.extras.RealDictCursor,
        options="-c timezone=Asia/Seoul",
    )


def query(sql: str, params: tuple = ()) -> list[dict]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# 에러 핸들러 — API 호출 시 HTML 대신 JSON 반환
# ---------------------------------------------------------------------------

@app.errorhandler(500)
def handle_500(e):
    logging.exception("Internal server error")
    return jsonify({"error": "서버 내부 오류", "detail": str(e)}), 500


@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return str(e), 404


# ---------------------------------------------------------------------------
# 페이지 라우트
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if not session.get("user_id"):
        return redirect("/login")
    return render_template("index.html")


@app.route("/login")
def login_page():
    if session.get("user_id"):
        return redirect("/")
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    """로그인 (세션 발급)."""
    data = request.get_json() or {}
    login_id = (data.get("login_id") or "").strip()
    password = data.get("password") or ""
    if not login_id or not password:
        return jsonify({"error": "아이디와 비밀번호를 입력해주세요"}), 400
    user = query_one(
        "SELECT id, name, password_hash FROM users WHERE login_id = %s", (login_id,)
    )
    if not user or not user.get("password_hash"):
        return jsonify({"error": "아이디 또는 비밀번호가 올바르지 않습니다"}), 401
    if not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "아이디 또는 비밀번호가 올바르지 않습니다"}), 401
    session["user_id"] = user["id"]
    return jsonify({"ok": True, "user_id": user["id"], "name": user["name"]})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    """로그아웃 (세션 삭제)."""
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/view_as/<int:uid>", methods=["POST"])
def api_view_as(uid: int):
    """다른 사용자 데이터로 보기 전환."""
    if not session.get("user_id"):
        return jsonify({"error": "not logged in"}), 401
    if uid == session["user_id"]:
        session.pop("view_as_uid", None)
    else:
        target = query_one("SELECT id FROM users WHERE id = %s", (uid,))
        if not target:
            return jsonify({"error": "사용자 없음"}), 404
        session["view_as_uid"] = uid
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    """현재 로그인 사용자 정보 조회."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "not logged in"}), 401
    user = query_one("SELECT id, name FROM users WHERE id = %s", (uid,))
    if not user:
        session.clear()
        return jsonify({"error": "not logged in"}), 401
    return jsonify(dict(user))


# ---------------------------------------------------------------------------
# API — 대시보드
# ---------------------------------------------------------------------------

@app.route("/api/dashboard")
def api_dashboard():
    """대시보드 현황 요약 (감시종목수·오늘 신호·오류건수, 최근 신호 목록, 30일 통계)."""
    today_kst = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)

    watched = query_one("SELECT COUNT(*) AS cnt FROM stocks WHERE watched = TRUE")
    signals_today = query_one(
        "SELECT COUNT(*) AS cnt FROM signals WHERE timestamp >= %s", (today_kst,)
    )
    errors_today = query_one(
        "SELECT COUNT(*) AS cnt FROM events WHERE status = 'FAIL' AND timestamp >= %s",
        (today_kst,),
    )
    supply_alerts = query_one(
        """
        SELECT COUNT(*) AS cnt FROM events
        WHERE event_type = 'SYSTEM' AND data->>'status' = '수급 경보'
          AND timestamp >= %s
        """,
        (today_kst,),
    )

    recent_signals = query(
        """
        SELECT s.ticker,
               COALESCE(st.stock_name, '') AS stock_name,
               s.signal, s.price, s.confidence, s.strategy,
               s.timestamp AT TIME ZONE 'Asia/Seoul' AS ts
        FROM signals s
        LEFT JOIN stocks st ON st.stock_code = s.ticker
        ORDER BY s.timestamp DESC
        LIMIT 10
        """
    )
    for r in recent_signals:
        r["ts"] = r["ts"].strftime("%m/%d %H:%M") if r["ts"] else ""
        r["price"] = f"{int(r['price']):,}" if r["price"] is not None else "-"
        r["confidence_pct"] = f"{r['confidence'] * 100:.0f}%" if r["confidence"] is not None else "-"

    signal_stats = query(
        """
        SELECT signal, COUNT(*) AS cnt
        FROM signals
        WHERE timestamp >= %s
        GROUP BY signal
        ORDER BY cnt DESC
        """,
        (datetime.now(tz=KST) - timedelta(days=30),),
    )

    return jsonify({
        "watched_count": (watched or {}).get("cnt", 0),
        "signals_today": (signals_today or {}).get("cnt", 0),
        "errors_today": (errors_today or {}).get("cnt", 0),
        "supply_alerts_today": (supply_alerts or {}).get("cnt", 0),
        "recent_signals": recent_signals,
        "signal_stats_30d": {r["signal"]: r["cnt"] for r in signal_stats},
    })


# ---------------------------------------------------------------------------
# API — 수급 현황
# ---------------------------------------------------------------------------

@app.route("/api/supply_demand/stocks")
def api_supply_stocks():
    """수급 데이터가 있는 종목 목록 조회."""
    rows = query(
        """
        SELECT DISTINCT sd.stock_code, COALESCE(st.stock_name, '') AS stock_name
        FROM supply_demand sd
        LEFT JOIN stocks st ON st.stock_code = sd.stock_code
        ORDER BY sd.stock_code
        """
    )
    return jsonify(rows)


@app.route("/api/supply_demand/<stock_code>")
def api_supply_demand(stock_code: str):
    """종목별 외국인·기관 수급 추이 및 누적 집계 (최대 500일)."""
    rows = query(
        """
        SELECT date, for_hold_qty, for_chg_qty, for_hold_ratio,
               orgn_net_qty, for_net_qty, ind_net_qty,
               fnnc_invt, insrnc, invtrt, bank, penfnd_etc, samo_fund, close_price
        FROM supply_demand
        WHERE stock_code = %s
        ORDER BY date DESC
        LIMIT 500
        """,
        (stock_code,),
    )
    result = []
    cumul_orgn = 0
    cumul_for  = 0
    for r in reversed(rows):
        cumul_orgn += r["orgn_net_qty"] or 0
        cumul_for  += r["for_net_qty"]  or 0
        result.append({
            "date":          r["date"].strftime("%Y-%m-%d"),
            "date_short":    r["date"].strftime("%m/%d"),
            "for_hold_ratio": r["for_hold_ratio"],
            "for_chg_qty":   r["for_chg_qty"],
            "for_net_qty":   r["for_net_qty"],
            "orgn_net_qty":  r["orgn_net_qty"],
            "ind_net_qty":   r["ind_net_qty"],
            "fnnc_invt":     r["fnnc_invt"],
            "insrnc":        r["insrnc"],
            "invtrt":        r["invtrt"],
            "bank":          r["bank"],
            "penfnd_etc":    r["penfnd_etc"],
            "samo_fund":     r["samo_fund"],
            "cumul_orgn":    cumul_orgn,
            "cumul_for":     cumul_for,
            "close_price":   r["close_price"],
        })
    return jsonify(result)


@app.route("/api/supply_divergence")
def api_supply_divergence():
    """수급 상승 + 가격 비상승 다이버전스 종목 탐지."""
    from agents.audit_monitor import AuditDB
    window   = int(request.args.get("window", 20))
    price_th = float(request.args.get("price_th", 3.0))
    ig_ratio = float(request.args.get("ignore_ratio", 0.15))
    db = AuditDB(config.database_url)
    rows = db.get_supply_price_divergence(
        window_days=window,
        price_flat_pct=price_th,
        ignore_ratio=ig_ratio,
    )
    return jsonify(rows)


@app.route("/api/snapshot")
def api_snapshot():
    """종목별 최신일 기준 N일 전 대비 가격·수급 변화 스냅샷."""
    from agents.audit_monitor import AuditDB
    raw = request.args.get("periods", "1,3,5,10,20")
    try:
        periods = [int(p) for p in raw.split(",") if p.strip().isdigit()]
    except ValueError:
        periods = [1, 3, 5, 10, 20]
    watched_only = request.args.get("watched_only", "true").lower() != "false"
    db = AuditDB(config.database_url)
    return jsonify(db.get_snapshot_compare(periods=periods, watched_only=watched_only))


@app.route("/api/supply_demand/summary")
def api_supply_summary():
    """수급 데이터 수집 현황 요약."""
    total_stocks = query_one("SELECT COUNT(DISTINCT stock_code) AS cnt FROM supply_demand")
    total_rows = query_one("SELECT COUNT(*) AS cnt FROM supply_demand")
    avg_days = query_one(
        "SELECT ROUND(AVG(day_cnt)) AS avg FROM (SELECT COUNT(*) AS day_cnt FROM supply_demand GROUP BY stock_code) t"
    )
    watched_without_data = query_one(
        """
        SELECT COUNT(*) AS cnt FROM stocks
        WHERE watched = TRUE
          AND stock_code NOT IN (SELECT DISTINCT stock_code FROM supply_demand)
        """
    )
    return jsonify({
        "collected_stocks": (total_stocks or {}).get("cnt", 0),
        "total_rows": (total_rows or {}).get("cnt", 0),
        "avg_days_per_stock": int((avg_days or {}).get("avg") or 0),
        "watched_without_data": (watched_without_data or {}).get("cnt", 0),
    })


# ---------------------------------------------------------------------------
# API — 매매 신호
# ---------------------------------------------------------------------------

@app.route("/api/signals")
def api_signals():
    """전략 에이전트 생성 매매신호 목록 조회 (최근 100건)."""
    rows = query(
        """
        SELECT s.ticker,
               COALESCE(st.stock_name, '') AS stock_name,
               s.signal, s.price, s.target_price, s.stop_loss,
               s.confidence, s.strategy,
               s.timestamp AT TIME ZONE 'Asia/Seoul' AS ts
        FROM signals s
        LEFT JOIN stocks st ON st.stock_code = s.ticker
        ORDER BY s.timestamp DESC
        LIMIT 100
        """
    )
    result = []
    for r in rows:
        result.append({
            "ticker": r["ticker"],
            "stock_name": r["stock_name"],
            "signal": r["signal"],
            "price": f"{int(r['price']):,}" if r["price"] else "-",
            "target_price": f"{int(r['target_price']):,}" if r["target_price"] else "-",
            "stop_loss": f"{int(r['stop_loss']):,}" if r["stop_loss"] else "-",
            "confidence": f"{r['confidence'] * 100:.0f}%" if r["confidence"] else "-",
            "strategy": r["strategy"] or "-",
            "ts": r["ts"].strftime("%Y-%m-%d %H:%M") if r["ts"] else "",
        })
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — 시장 구조 분석 신호
# ---------------------------------------------------------------------------

@app.route("/api/market_structure_signals")
def api_market_structure_signals():
    """시장구조 전략이 포함된 매매신호 목록 (최근 200건)."""
    rows = query(
        """
        SELECT s.ticker,
               COALESCE(st.stock_name, '') AS stock_name,
               s.signal, s.price, s.target_price, s.stop_loss,
               s.confidence, s.strategy, s.reasons,
               s.timestamp AT TIME ZONE 'Asia/Seoul' AS ts
        FROM signals s
        LEFT JOIN stocks st ON st.stock_code = s.ticker
        WHERE s.strategy LIKE '%%시장구조%%'
        ORDER BY s.timestamp DESC
        LIMIT 200
        """
    )
    result = []
    for r in rows:
        reasons = r["reasons"] if r["reasons"] else []
        reasons_text = " | ".join(reasons) if isinstance(reasons, list) else str(reasons)

        # 구조 유형 추론 (reasons 텍스트 파싱)
        struct_type = "-"
        if any("CHoCH" in s for s in reasons):
            struct_type = "CHoCH"
        elif any("BOS" in s for s in reasons):
            struct_type = "BOS"
        elif any("스윕" in s for s in reasons):
            struct_type = "Sweep"

        # 시장 상태 추론
        market_state = "-"
        for rr in reasons:
            if "상승추세" in rr:
                market_state = "UPTREND"
                break
            if "하락추세" in rr:
                market_state = "DOWNTREND"
                break
            if "CHoCH(BUY)" in rr or "CHoCH(SELL)" in rr:
                market_state = "전환"
                break

        result.append({
            "ticker":       r["ticker"],
            "stock_name":   r["stock_name"],
            "signal":       r["signal"],
            "price":        int(r["price"]) if r["price"] else None,
            "target_price": int(r["target_price"]) if r["target_price"] else None,
            "stop_loss":    int(r["stop_loss"]) if r["stop_loss"] else None,
            "confidence":   round(float(r["confidence"]) * 100) if r["confidence"] else None,
            "strategy":     r["strategy"] or "-",
            "struct_type":  struct_type,
            "market_state": market_state,
            "reasons":      reasons if isinstance(reasons, list) else [],
            "reasons_text": reasons_text,
            "ts":           r["ts"].strftime("%Y-%m-%d %H:%M") if r["ts"] else "",
        })
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — 감사 로그
# ---------------------------------------------------------------------------

@app.route("/api/events")
def api_events():
    """에이전트 이벤트 감사 로그 전체 조회."""
    rows = query(
        """
        SELECT event_type, agent, ticker, status, data,
               timestamp AT TIME ZONE 'Asia/Seoul' AS ts
        FROM events
        ORDER BY timestamp DESC
        LIMIT 200
        """
    )
    result = []
    for r in rows:
        data = r["data"] or {}
        detail = data.get("detail") or data.get("title") or data.get("status") or ""
        result.append({
            "ts": r["ts"].strftime("%m/%d %H:%M:%S") if r["ts"] else "",
            "event_type": r["event_type"],
            "agent": r["agent"],
            "ticker": r["ticker"] or "-",
            "status": r["status"],
            "detail": str(detail)[:80],
        })
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — 종목 목록
# ---------------------------------------------------------------------------

@app.route("/api/stocks")
def api_stocks():
    """감시 종목 목록 조회 (코드·이름·시총·최근가)."""
    rows = query(
        """
        SELECT stock_code, stock_name, market_name, last_price, list_count, watched,
               fetched_at AT TIME ZONE 'Asia/Seoul' AS fetched_at
        FROM stocks
        WHERE watched = TRUE
        ORDER BY
            CASE WHEN last_price ~ '^[0-9]+$' AND list_count ~ '^[0-9]+$'
                 THEN (last_price::BIGINT * list_count::BIGINT) ELSE 0 END DESC
        LIMIT 200
        """
    )
    result = []
    for r in rows:
        try:
            cap = int(r["last_price"] or 0) * int(r["list_count"] or 0)
            cap_str = f"{cap // 100_000_000:,}억"
        except (ValueError, TypeError):
            cap_str = "-"
        result.append({
            "stock_code": r["stock_code"],
            "stock_name": r["stock_name"],
            "market_name": r["market_name"] or "-",
            "last_price": f"{int(r['last_price']):,}" if r["last_price"] and r["last_price"].isdigit() else "-",
            "market_cap": cap_str,
            "fetched_at": r["fetched_at"].strftime("%Y-%m-%d") if r["fetched_at"] else "-",
        })
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — 보유종목 리포트
# ---------------------------------------------------------------------------

@app.route("/api/report/preview")
def api_report_preview():
    """리포트 HTML 미리보기 생성 (이메일 발송 없음)."""
    import sys, os
    sys.path.insert(0, BASE_DIR)
    try:
        from scripts.holdings_report import main as gen_report
        html = gen_report(send=False)
        return jsonify({"ok": True, "html": html})
    except Exception as e:
        logging.exception("리포트 미리보기 실패")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/report/send", methods=["POST"])
def api_report_send():
    """리포트 즉시 발송."""
    import sys
    sys.path.insert(0, BASE_DIR)
    try:
        from scripts.holdings_report import main as gen_report
        gen_report(send=True)
        return jsonify({"ok": True})
    except Exception as e:
        logging.exception("리포트 발송 실패")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/report/config")
def api_report_config_get():
    """이메일 수신자 설정 및 SMTP 구성 조회."""
    emails = query("SELECT id, email, active FROM report_email_config ORDER BY id")
    schedule = query_one("SELECT enabled, hour, minute, days FROM batch_schedules WHERE job_id = 'holdings_report'")
    smtp_user = os.environ.get("SMTP_USER", "")
    return jsonify({
        "emails": [{"id": r["id"], "email": r["email"], "active": r["active"]} for r in emails],
        "smtp_configured": bool(smtp_user),
        "smtp_user": smtp_user,
        "schedule": dict(schedule) if schedule else {"enabled": False, "hour": 8, "minute": 0, "days": "weekdays"},
    })


@app.route("/api/report/config/email", methods=["POST"])
def api_report_email_add():
    """수신자 이메일 추가."""
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "유효하지 않은 이메일"}), 400
    try:
        with get_conn() as conn:
            conn.cursor().execute(
                "INSERT INTO report_email_config (email) VALUES (%s) ON CONFLICT (email) DO UPDATE SET active = TRUE",
                (email,)
            )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/report/config/email/<int:eid>", methods=["DELETE"])
def api_report_email_delete(eid: int):
    """수신자 이메일 삭제."""
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM report_email_config WHERE id = %s", (eid,))
    return jsonify({"ok": True})


@app.route("/api/report/config/email/<int:eid>/toggle", methods=["POST"])
def api_report_email_toggle(eid: int):
    """수신자 활성/비활성 토글."""
    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE report_email_config SET active = NOT active WHERE id = %s", (eid,)
        )
    return jsonify({"ok": True})


@app.route("/api/report/history")
def api_report_history():
    """발송 이력 조회."""
    rows = query(
        """
        SELECT sent_at AT TIME ZONE 'Asia/Seoul' AS ts,
               recipients, stock_count, status, error_msg
        FROM report_send_log
        ORDER BY sent_at DESC
        LIMIT 30
        """
    )
    for r in rows:
        r["ts"] = r["ts"].strftime("%Y-%m-%d %H:%M") if r["ts"] else ""
    return jsonify(rows)


# ---------------------------------------------------------------------------
# API — 리밸런싱 알림 수신자 관리 (user_alert_emails)
# ---------------------------------------------------------------------------

@app.route("/api/alert_emails")
def api_alert_emails_list():
    """전체 사용자별 리밸런싱 알림 수신자 목록."""
    users = query("SELECT id, name, login_id FROM users ORDER BY id")
    emails = query(
        "SELECT id, user_id, email, active FROM user_alert_emails ORDER BY user_id, id"
    )
    email_map: dict[int, list] = {u["id"]: [] for u in users}
    for e in emails:
        uid = e["user_id"]
        if uid in email_map:
            email_map[uid].append({"id": e["id"], "email": e["email"], "active": e["active"]})
    return jsonify([
        {"user_id": u["id"], "user_name": u["name"] or u["login_id"], "emails": email_map[u["id"]]}
        for u in users
    ])


@app.route("/api/alert_emails", methods=["POST"])
def api_alert_emails_add():
    """리밸런싱 알림 수신자 추가."""
    data = request.get_json() or {}
    uid   = data.get("user_id")
    email = (data.get("email") or "").strip().lower()
    if not uid or not email:
        return jsonify({"error": "user_id, email 필수"}), 400
    try:
        with get_conn() as conn:
            conn.cursor().execute(
                """INSERT INTO user_alert_emails (user_id, email)
                   VALUES (%s, %s)
                   ON CONFLICT (user_id, email) DO UPDATE SET active = TRUE""",
                (uid, email),
            )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/alert_emails/<int:eid>", methods=["DELETE"])
def api_alert_emails_delete(eid: int):
    """리밸런싱 알림 수신자 삭제."""
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM user_alert_emails WHERE id = %s", (eid,))
    return jsonify({"ok": True})


@app.route("/api/alert_emails/<int:eid>/toggle", methods=["POST"])
def api_alert_emails_toggle(eid: int):
    """리밸런싱 알림 수신자 활성/비활성 토글."""
    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE user_alert_emails SET active = NOT active WHERE id = %s", (eid,)
        )
    return jsonify({"ok": True})


@app.route("/api/alert_emails/history")
def api_alert_emails_history():
    """리밸런싱 알림 발송 이력."""
    rows = query(
        """
        SELECT sent_at AT TIME ZONE 'Asia/Seoul' AS ts,
               signal_hash, stock_buy_cnt, stock_sell_cnt,
               theme_buy_cnt, theme_sell_cnt, recipients, status, error_msg
        FROM rebalance_alert_log
        ORDER BY sent_at DESC
        LIMIT 30
        """
    )
    result = []
    for r in rows:
        result.append({
            "ts":             r["ts"].strftime("%Y-%m-%d %H:%M") if r["ts"] else "",
            "stock_buy_cnt":  r["stock_buy_cnt"],
            "stock_sell_cnt": r["stock_sell_cnt"],
            "theme_buy_cnt":  r["theme_buy_cnt"],
            "theme_sell_cnt": r["theme_sell_cnt"],
            "recipients":     r["recipients"] or "",
            "status":         r["status"] or "",
        })
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — 공통코드 (common_codes)
# ---------------------------------------------------------------------------

@app.route("/api/common_codes/<group>")
def api_common_codes_list(group: str):
    """활성 코드 목록 (드롭다운용)."""
    rows = query("""
        SELECT id, code, name, sort_order, active
        FROM common_codes
        WHERE code_group = %s
        ORDER BY sort_order, name
    """, (group.upper(),))
    return jsonify(rows)


@app.route("/api/common_codes/<group>", methods=["POST"])
def api_common_codes_create(group: str):
    """공통코드 항목 추가."""
    data = request.get_json() or {}
    code = (data.get("code") or "").strip().upper()
    name = (data.get("name") or "").strip()
    sort_order = int(data.get("sort_order") or 0)
    if not code or not name:
        return jsonify({"error": "코드와 명칭을 입력해주세요"}), 400
    try:
        row = query_one("""
            INSERT INTO common_codes (code_group, code, name, sort_order)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        """, (group.upper(), code, name, sort_order))
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"error": "이미 존재하는 코드입니다"}), 409
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "id": row["id"]}), 201


@app.route("/api/common_codes/<int:cid>", methods=["PUT"])
def api_common_codes_update(cid: int):
    """공통코드 항목 수정."""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    sort_order = int(data.get("sort_order") or 0)
    if not name:
        return jsonify({"error": "명칭을 입력해주세요"}), 400
    with get_conn() as conn:
        conn.cursor().execute("""
            UPDATE common_codes SET name = %s, sort_order = %s WHERE id = %s
        """, (name, sort_order, cid))
    return jsonify({"ok": True})


@app.route("/api/common_codes/<int:cid>/toggle", methods=["POST"])
def api_common_codes_toggle(cid: int):
    """공통코드 항목 활성화/비활성화 토글."""
    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE common_codes SET active = NOT active WHERE id = %s", (cid,)
        )
    return jsonify({"ok": True})


@app.route("/api/common_codes/<int:cid>", methods=["DELETE"])
def api_common_codes_delete(cid: int):
    """공통코드 항목 삭제."""
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM common_codes WHERE id = %s", (cid,))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — 타사 보유종목 (manual_holdings)
# ---------------------------------------------------------------------------

@app.route("/api/manual_holdings")
def api_manual_holdings_list():
    """타사 보유종목 목록 조회."""
    uid = _current_uid()
    rows = query("""
        WITH latest_close AS (
            SELECT DISTINCT ON (stock_code)
                stock_code,
                close_price,
                date AS price_date
            FROM supply_demand
            WHERE close_price IS NOT NULL AND close_price > 0
            ORDER BY stock_code, date DESC
        )
        SELECT
            mh.id, mh.brokerage, mh.stock_code, mh.stock_name,
            mh.quantity, mh.avg_price, mh.memo,
            mh.created_at AT TIME ZONE 'Asia/Seoul' AS created_at,
            COALESCE(
                lc.close_price,
                CASE WHEN st.last_price ~ '^[0-9]+$' THEN st.last_price::BIGINT ELSE NULL END
            ) AS current_price,
            lc.price_date
        FROM manual_holdings mh
        LEFT JOIN latest_close lc ON lc.stock_code = mh.stock_code
        LEFT JOIN stocks st ON st.stock_code = mh.stock_code
        WHERE mh.user_id = %s
        ORDER BY mh.brokerage, mh.stock_name, mh.stock_code
    """, (uid,))
    for r in rows:
        r["avg_price"]     = float(r["avg_price"])     if r["avg_price"]     is not None else 0.0
        r["current_price"] = int(r["current_price"])   if r["current_price"] is not None else None
        r["price_date"]    = r["price_date"].strftime("%Y-%m-%d") if r["price_date"] else None
        r["created_at"]    = r["created_at"].strftime("%Y-%m-%d") if r["created_at"] else ""
    return jsonify(rows)


@app.route("/api/manual_holdings", methods=["POST"])
def api_manual_holdings_create():
    """타사 보유종목 추가."""
    uid = _current_uid()
    data = request.get_json() or {}
    brokerage  = (data.get("brokerage") or "").strip()
    stock_code = (data.get("stock_code") or "").strip()
    stock_name = (data.get("stock_name") or "").strip()
    memo       = (data.get("memo") or "").strip()
    try:
        quantity  = int(data.get("quantity") or 0)
        avg_price = float(data.get("avg_price") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "수량·매수평균가는 숫자로 입력해주세요"}), 400
    if not stock_code:
        return jsonify({"error": "종목코드를 입력해주세요"}), 400
    if quantity <= 0:
        return jsonify({"error": "보유수량은 1 이상이어야 합니다"}), 400
    row = query_one("""
        INSERT INTO manual_holdings (user_id, brokerage, stock_code, stock_name, quantity, avg_price, memo)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (uid, brokerage, stock_code, stock_name, quantity, avg_price, memo))
    return jsonify({"ok": True, "id": row["id"]}), 201


@app.route("/api/manual_holdings/<int:hid>", methods=["PUT"])
def api_manual_holdings_update(hid: int):
    """타사 보유종목 수정."""
    uid = _current_uid()
    data = request.get_json() or {}
    brokerage  = (data.get("brokerage") or "").strip()
    stock_code = (data.get("stock_code") or "").strip()
    stock_name = (data.get("stock_name") or "").strip()
    memo       = (data.get("memo") or "").strip()
    try:
        quantity  = int(data.get("quantity") or 0)
        avg_price = float(data.get("avg_price") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "수량·매수평균가는 숫자로 입력해주세요"}), 400
    if not stock_code:
        return jsonify({"error": "종목코드를 입력해주세요"}), 400
    if quantity <= 0:
        return jsonify({"error": "보유수량은 1 이상이어야 합니다"}), 400
    with get_conn() as conn:
        conn.cursor().execute("""
            UPDATE manual_holdings
            SET brokerage = %s, stock_code = %s, stock_name = %s,
                quantity = %s, avg_price = %s, memo = %s, updated_at = NOW()
            WHERE id = %s AND user_id = %s
        """, (brokerage, stock_code, stock_name, quantity, avg_price, memo, hid, uid))
    return jsonify({"ok": True})


@app.route("/api/manual_holdings/<int:hid>", methods=["DELETE"])
def api_manual_holdings_delete(hid: int):
    """타사 보유종목 삭제."""
    uid = _current_uid()
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM manual_holdings WHERE id = %s AND user_id = %s", (hid, uid))
    return jsonify({"ok": True})


@app.route("/api/manual_holdings/trade", methods=["POST"])
def api_manual_holdings_trade():
    """거래 체결 내용을 이력에 pending 상태로 기록.
    보유종목 반영은 /api/trade_history/<id>/approve 호출 시 수행."""
    uid = _current_uid()
    data = request.get_json() or {}
    stock_code      = (data.get("stock_code")  or "").strip()
    stock_name      = (data.get("stock_name")  or "").strip()
    direction       = (data.get("direction")   or "").lower()
    brokerage       = (data.get("brokerage")   or "").strip()
    source          = (data.get("source")      or "manual").strip()
    executed_at_str = (data.get("executed_at") or "").strip() or None
    try:
        quantity = int(data.get("quantity") or 0)
        price    = float(data.get("price")  or 0)
    except (ValueError, TypeError):
        return jsonify({"error": "수량·가격 오류"}), 400

    if not stock_code or direction not in ("buy", "sell") or quantity <= 0 or price <= 0:
        return jsonify({"error": "필수 항목 누락 또는 잘못된 값"}), 400

    amount = round(quantity * price)
    row = query_one("""
        INSERT INTO trade_history
            (user_id, stock_code, stock_name, direction, brokerage,
             quantity, price, amount, source, executed_at, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s::timestamp, NOW()), 'pending')
        RETURNING id
    """, (uid, stock_code, stock_name or stock_code, direction, brokerage,
          quantity, price, amount, source, executed_at_str))
    return jsonify({"ok": True, "id": row["id"] if row else None})


@app.route("/api/trade_history/<int:tid>/approve", methods=["POST"])
def api_trade_history_approve(tid: int):
    """pending 거래를 승인: 보유종목 반영 후 status='approved'로 변경."""
    uid = _current_uid()
    trade = query_one(
        "SELECT * FROM trade_history WHERE id=%s AND user_id=%s", (tid, uid)
    )
    if not trade:
        return jsonify({"error": "거래 이력을 찾을 수 없습니다"}), 404
    if trade["status"] != "pending":
        return jsonify({"error": "승인 대기 상태가 아닙니다"}), 400

    stock_code = trade["stock_code"]
    stock_name = trade["stock_name"]
    direction  = trade["direction"]
    brokerage  = trade["brokerage"]
    quantity   = int(trade["quantity"])
    price      = float(trade["price"])

    avg_price_before = None

    with get_conn() as conn:
        cur = conn.cursor()
        existing = query_one(
            "SELECT id, quantity, avg_price FROM manual_holdings WHERE user_id=%s AND brokerage=%s AND stock_code=%s",
            (uid, brokerage, stock_code),
        )
        if direction == "buy":
            if existing:
                old_qty = int(existing["quantity"] or 0)
                old_avg = float(existing["avg_price"] or 0)
                new_qty = old_qty + quantity
                new_avg = round((old_qty * old_avg + quantity * price) / new_qty, 2)
                cur.execute(
                    "UPDATE manual_holdings SET quantity=%s, avg_price=%s WHERE id=%s",
                    (new_qty, new_avg, existing["id"]),
                )
            else:
                if not brokerage:
                    return jsonify({"error": "신규 매수 시 증권사 입력 필요"}), 400
                cur.execute(
                    "INSERT INTO manual_holdings (user_id, brokerage, stock_code, stock_name, quantity, avg_price) VALUES (%s,%s,%s,%s,%s,%s)",
                    (uid, brokerage, stock_code, stock_name or stock_code, quantity, price),
                )
        else:  # sell
            if not existing:
                return jsonify({"error": "해당 증권사에 보유 종목 없음"}), 404
            avg_price_before = float(existing["avg_price"] or 0)
            new_qty = max(0, int(existing["quantity"] or 0) - quantity)
            if new_qty == 0:
                cur.execute("DELETE FROM manual_holdings WHERE id=%s", (existing["id"],))
            else:
                cur.execute(
                    "UPDATE manual_holdings SET quantity=%s WHERE id=%s",
                    (new_qty, existing["id"]),
                )

        realized_pnl = round((price - avg_price_before) * quantity) if avg_price_before is not None else None
        cur.execute(
            "UPDATE trade_history SET status='approved', avg_price_before=%s, realized_pnl=%s WHERE id=%s",
            (avg_price_before, realized_pnl, tid),
        )
    return jsonify({"ok": True})


@app.route("/api/trade_history/<int:tid>/cancel", methods=["POST"])
def api_trade_history_cancel(tid: int):
    """pending 거래를 취소 (보유종목 변경 없음)."""
    uid = _current_uid()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE trade_history SET status='cancelled' WHERE id=%s AND user_id=%s AND status='pending'",
            (tid, uid),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "승인 대기 상태가 아닙니다"}), 400
    return jsonify({"ok": True})


@app.route("/api/manual_holdings/brokerages")
def api_manual_holdings_brokerages():
    """보유종목에 등록된 증권사 목록 (체결 입력 드롭다운용)."""
    uid = _current_uid()
    rows = query(
        "SELECT DISTINCT brokerage FROM manual_holdings WHERE user_id=%s AND brokerage IS NOT NULL ORDER BY brokerage",
        (uid,),
    )
    return jsonify([r["brokerage"] for r in rows])


@app.route("/api/trade_history")
def api_trade_history():
    """거래 이력 조회. 필터: stock_code, direction, status, from_date, to_date, limit, offset"""
    uid = _current_uid()
    stock_filter  = request.args.get("stock_code", "").strip()
    direction     = request.args.get("direction",  "").strip()
    status_filter = request.args.get("status",     "").strip()
    from_date     = request.args.get("from_date",  "").strip()
    to_date       = request.args.get("to_date",    "").strip()
    try:
        limit  = min(int(request.args.get("limit",  200)), 500)
        offset = max(int(request.args.get("offset", 0)),   0)
    except (ValueError, TypeError):
        limit, offset = 200, 0

    where  = ["user_id = %s"]
    params: list = [uid]
    if stock_filter:
        where.append("(stock_code ILIKE %s OR stock_name ILIKE %s)")
        params += [f"%{stock_filter}%", f"%{stock_filter}%"]
    if direction in ("buy", "sell"):
        where.append("direction = %s")
        params.append(direction)
    if status_filter in ("pending", "approved", "cancelled"):
        where.append("status = %s")
        params.append(status_filter)
    if from_date:
        where.append("executed_at >= %s")
        params.append(from_date)
    if to_date:
        where.append("executed_at < (%s::date + interval '1 day')")
        params.append(to_date)

    wc    = " AND ".join(where)
    total = (query_one(f"SELECT COUNT(*) AS cnt FROM trade_history WHERE {wc}", params) or {}).get("cnt", 0)
    rows  = query(
        f"""
        WITH filtered AS (
            SELECT * FROM trade_history WHERE {wc}
        )
        SELECT f.*,
               lc.close_price AS current_price,
               CASE WHEN f.direction = 'buy' AND lc.close_price IS NOT NULL
                    THEN ROUND((lc.close_price - f.price) * f.quantity)
                    ELSE NULL
               END AS eval_pnl
        FROM filtered f
        LEFT JOIN LATERAL (
            SELECT close_price
            FROM supply_demand
            WHERE stock_code = f.stock_code
              AND close_price IS NOT NULL AND close_price > 0
            ORDER BY date DESC
            LIMIT 1
        ) lc ON true
        ORDER BY f.executed_at ASC
        LIMIT %s OFFSET %s
        """,
        params + [limit, offset],
    )
    return jsonify({"total": total, "rows": [dict(r) for r in rows]})


@app.route("/api/trade_history/<int:tid>", methods=["DELETE"])
def api_trade_history_delete(tid: int):
    """거래 이력 단건 삭제 (보유종목 수량은 변경하지 않음)."""
    uid = _current_uid()
    with get_conn() as conn:
        conn.cursor().execute(
            "DELETE FROM trade_history WHERE id = %s AND user_id = %s",
            (tid, uid),
        )
    return jsonify({"ok": True})


@app.route("/api/trade_history/stats")
def api_trade_history_stats():
    """거래 이력 종목별·전체 집계 통계."""
    uid = _current_uid()
    overall = query_one("""
        SELECT
            COALESCE(SUM(CASE WHEN direction='sell' THEN realized_pnl ELSE 0 END), 0) AS total_realized_pnl,
            COUNT(*) FILTER (WHERE direction='buy')  AS buy_count,
            COUNT(*) FILTER (WHERE direction='sell') AS sell_count,
            COALESCE(SUM(amount), 0) AS total_amount
        FROM trade_history WHERE user_id = %s AND status = 'approved'
    """, (uid,))
    by_stock = query("""
        SELECT
            stock_code,
            MAX(stock_name) AS stock_name,
            COUNT(*) FILTER (WHERE direction='buy')  AS buy_count,
            COUNT(*) FILTER (WHERE direction='sell') AS sell_count,
            COALESCE(SUM(CASE WHEN direction='buy'  THEN amount ELSE 0 END), 0) AS total_buy_amount,
            COALESCE(SUM(CASE WHEN direction='sell' THEN amount ELSE 0 END), 0) AS total_sell_amount,
            COALESCE(SUM(CASE WHEN direction='sell' THEN realized_pnl ELSE 0 END), 0) AS realized_pnl,
            MAX(executed_at) AS last_trade_at
        FROM trade_history WHERE user_id = %s AND status = 'approved'
        GROUP BY stock_code
        ORDER BY last_trade_at DESC
    """, (uid,))
    return jsonify({
        "overall":  dict(overall) if overall else {},
        "by_stock": [dict(r) for r in by_stock],
    })


@app.route("/api/price_sync/stocks")
def api_price_sync_stocks():
    """타사 보유종목 현재가 현황 (현재가 관리 화면용)."""
    uid = _current_uid()
    rows = query("""
        WITH holdings AS (
            SELECT stock_code, MAX(stock_name) AS stock_name
            FROM manual_holdings
            WHERE user_id = %s
            GROUP BY stock_code
        ),
        latest_close AS (
            SELECT DISTINCT ON (stock_code)
                stock_code, close_price, date AS price_date
            FROM supply_demand
            WHERE close_price IS NOT NULL AND close_price > 0
            ORDER BY stock_code, date DESC
        )
        SELECT
            h.stock_code,
            h.stock_name,
            COALESCE(
                lc.close_price,
                CASE WHEN st.last_price ~ '^[0-9]+$' THEN st.last_price::BIGINT ELSE NULL END
            ) AS current_price,
            lc.price_date,
            st.fetched_at AT TIME ZONE 'Asia/Seoul' AS fetched_at
        FROM holdings h
        LEFT JOIN latest_close lc ON lc.stock_code = h.stock_code
        LEFT JOIN stocks st ON st.stock_code = h.stock_code
        ORDER BY h.stock_code
    """, (uid,))
    for r in rows:
        r["current_price"] = int(r["current_price"]) if r["current_price"] is not None else None
        r["price_date"]    = r["price_date"].strftime("%Y-%m-%d") if r["price_date"] else None
        r["fetched_at"]    = r["fetched_at"].strftime("%Y-%m-%d %H:%M") if r["fetched_at"] else None
    return jsonify(rows)


@app.route("/api/price_sync/manual", methods=["PUT"])
def api_price_sync_manual():
    """종목 현재가 수기 입력 (supply_demand + stocks 업데이트)."""
    data = request.get_json() or {}
    stock_code  = (data.get("stock_code") or "").strip()
    price_raw   = data.get("price")
    try:
        price = int(str(price_raw).replace(",", "").strip())
        if price <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"error": "유효한 가격을 입력해주세요"}), 400
    if not stock_code:
        return jsonify({"error": "종목코드 필수"}), 400

    from datetime import date as date_cls
    today = date_cls.today()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO supply_demand (stock_code, date, close_price)
            VALUES (%s, %s, %s)
            ON CONFLICT (stock_code, date)
            DO UPDATE SET close_price = EXCLUDED.close_price
        """, (stock_code, today, price))
        cur.execute("""
            UPDATE stocks SET last_price = %s, fetched_at = NOW()
            WHERE stock_code = %s
        """, (str(price), stock_code))
    return jsonify({"ok": True, "stock_code": stock_code, "price": price, "date": str(today)})


# ---------------------------------------------------------------------------
# API — 리밸런싱
# ---------------------------------------------------------------------------

@app.route("/api/rebalance")
def api_rebalance():
    """보유종목 통합(전 증권사) 리밸런싱 데이터."""
    uid = _current_uid()
    rows = query("""
        WITH latest_close AS (
            SELECT DISTINCT ON (stock_code)
                stock_code, close_price
            FROM supply_demand
            WHERE close_price IS NOT NULL AND close_price > 0
            ORDER BY stock_code, date DESC
        ),
        holdings_agg AS (
            SELECT
                stock_code,
                MAX(stock_name) AS stock_name,
                SUM(quantity) AS total_qty,
                SUM(quantity * avg_price) / NULLIF(SUM(quantity), 0) AS weighted_avg_price
            FROM manual_holdings
            WHERE user_id = %s
            GROUP BY stock_code
        )
        SELECT
            ha.stock_code,
            ha.stock_name,
            ha.total_qty,
            ROUND(ha.weighted_avg_price::NUMERIC, 0) AS avg_price,
            COALESCE(
                lc.close_price,
                CASE WHEN st.last_price ~ '^[0-9]+$' THEN st.last_price::BIGINT ELSE NULL END
            ) AS current_price,
            COALESCE(rt.target_ratio, 0) AS target_ratio,
            rt.alert_up, rt.alert_down, rt.watch_up, rt.watch_down
        FROM holdings_agg ha
        LEFT JOIN latest_close lc ON lc.stock_code = ha.stock_code
        LEFT JOIN stocks st ON st.stock_code = ha.stock_code
        LEFT JOIN rebalance_targets rt ON rt.stock_code = ha.stock_code AND rt.user_id = %s
        ORDER BY ha.stock_name
    """, (uid, uid))

    _backfill_null_user_ids(uid)
    settings          = _get_user_settings(uid)
    total_cash        = _get_total_cash(uid)
    alert_up          = float(settings.get("rebalance_alert_up",   30))
    alert_down        = float(settings.get("rebalance_alert_down", 25))
    watch_up          = float(settings.get("rebalance_watch_up",   round(alert_up  * 0.5, 1)))
    watch_down        = float(settings.get("rebalance_watch_down", round(alert_down * 0.5, 1)))
    cash_target_ratio = float(settings.get("cash_target_ratio",    0))

    result = []
    stock_total = 0
    for r in rows:
        qty       = int(r["total_qty"] or 0)
        cur_price = r["current_price"]
        avg_price = float(r["avg_price"] or 0)
        eval_amt  = qty * (int(cur_price) if cur_price is not None else avg_price)
        stock_total += eval_amt
        result.append({
            "stock_code":   r["stock_code"],
            "stock_name":   r["stock_name"],
            "total_qty":    qty,
            "avg_price":    int(avg_price),
            "current_price": int(cur_price) if cur_price is not None else None,
            "eval_amt":     round(eval_amt),
            "has_price":    cur_price is not None,
            "target_ratio": float(r["target_ratio"] or 0),
            "alert_up":     float(r["alert_up"])   if r["alert_up"]   is not None else None,
            "alert_down":   float(r["alert_down"]) if r["alert_down"] is not None else None,
            "watch_up":     float(r["watch_up"])   if r["watch_up"]   is not None else None,
            "watch_down":   float(r["watch_down"]) if r["watch_down"] is not None else None,
        })

    portfolio_total = stock_total + total_cash

    for r in result:
        current_ratio = r["eval_amt"] / portfolio_total * 100 if portfolio_total > 0 else 0
        target_ratio  = r["target_ratio"]
        deviation_pp  = round(current_ratio - target_ratio, 2)
        deviation_rel = round(deviation_pp / target_ratio * 100, 1) if target_ratio > 0 else None
        r["current_ratio"]  = round(current_ratio, 2)
        r["deviation_pp"]   = deviation_pp
        r["deviation_rel"]  = deviation_rel

    return jsonify({
        "holdings":          result,
        "portfolio_total":   round(portfolio_total),
        "stock_total":       round(stock_total),
        "total_cash":        total_cash,
        "alert_up":          alert_up,
        "alert_down":        alert_down,
        "watch_up":          watch_up,
        "watch_down":        watch_down,
        "cash_target_ratio": cash_target_ratio,
    })


@app.route("/api/rebalance/stock_setting", methods=["PUT"])
def api_rebalance_stock_setting():
    """종목별 목표 비중 및 알림 임계값 설정."""
    uid = _current_uid()
    data = request.get_json() or {}
    stock_code = (data.get("stock_code") or "").strip()
    if not stock_code:
        return jsonify({"error": "종목코드 필수"}), 400
    def _to_float_or_none(v):
        try:
            return float(v) if v not in (None, "") else None
        except (ValueError, TypeError):
            return None
    target_ratio       = _to_float_or_none(data.get("target_ratio"))
    alert_up           = _to_float_or_none(data.get("alert_up"))
    alert_down         = _to_float_or_none(data.get("alert_down"))
    watch_up           = _to_float_or_none(data.get("watch_up"))
    watch_down         = _to_float_or_none(data.get("watch_down"))
    position_tier      = (data.get("position_tier") or "").strip() or None
    max_change_pp      = _to_float_or_none(data.get("max_change_pp"))
    overweight_band_pp = _to_float_or_none(data.get("overweight_band_pp"))
    review_band_pp     = _to_float_or_none(data.get("review_band_pp"))

    # 티어 기본값 자동 적용
    TIER_DEFAULTS = {
        "CORE":      (2.0, 5.0, 2.5),
        "MID":       (1.5, 3.0, 1.5),
        "SATELLITE": (1.0, 2.0, 1.0),
        "OPTION":    (1.0, 1.5, 0.75),
    }
    if position_tier and position_tier in TIER_DEFAULTS:
        defs = TIER_DEFAULTS[position_tier]
        if max_change_pp      is None: max_change_pp      = defs[0]
        if overweight_band_pp is None: overweight_band_pp = defs[1]
        if review_band_pp     is None: review_band_pp     = defs[2]

    with get_conn() as conn:
        conn.cursor().execute("""
            INSERT INTO rebalance_targets
                (user_id, stock_code, target_ratio, alert_up, alert_down, watch_up, watch_down,
                 position_tier, max_change_pp, overweight_band_pp, review_band_pp, updated_at)
            VALUES (%s, %s, COALESCE(%s, 0), %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (user_id, stock_code) DO UPDATE SET
                target_ratio       = COALESCE(EXCLUDED.target_ratio, rebalance_targets.target_ratio),
                alert_up           = EXCLUDED.alert_up,
                alert_down         = EXCLUDED.alert_down,
                watch_up           = EXCLUDED.watch_up,
                watch_down         = EXCLUDED.watch_down,
                position_tier      = COALESCE(EXCLUDED.position_tier,      rebalance_targets.position_tier),
                max_change_pp      = COALESCE(EXCLUDED.max_change_pp,      rebalance_targets.max_change_pp),
                overweight_band_pp = COALESCE(EXCLUDED.overweight_band_pp, rebalance_targets.overweight_band_pp),
                review_band_pp     = COALESCE(EXCLUDED.review_band_pp,     rebalance_targets.review_band_pp),
                updated_at         = NOW()
        """, (uid, stock_code, target_ratio, alert_up, alert_down, watch_up, watch_down,
              position_tier, max_change_pp, overweight_band_pp, review_band_pp))
    return jsonify({"ok": True})


@app.route("/api/rebalance/target", methods=["PUT"])
def api_rebalance_target():
    """종목 목표 비중 설정 (단건)."""
    uid = _current_uid()
    data = request.get_json() or {}
    stock_code = (data.get("stock_code") or "").strip()
    if not stock_code:
        return jsonify({"error": "종목코드 필수"}), 400
    try:
        target_ratio = float(data.get("target_ratio") or 0)
        if not (0 <= target_ratio <= 100):
            return jsonify({"error": "비율은 0~100 사이여야 합니다"}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "잘못된 비율 값"}), 400
    with get_conn() as conn:
        conn.cursor().execute("""
            INSERT INTO rebalance_targets (user_id, stock_code, target_ratio, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id, stock_code)
            DO UPDATE SET target_ratio = EXCLUDED.target_ratio, updated_at = NOW()
        """, (uid, stock_code, target_ratio))
    return jsonify({"ok": True})


@app.route("/api/rebalance/targets", methods=["PUT"])
def api_rebalance_targets_batch():
    """종목 목표 비중 일괄 저장. body: [{stock_code, target_ratio}, ...]"""
    uid = _current_uid()
    items = request.get_json() or []
    if not isinstance(items, list):
        return jsonify({"error": "배열 형식 필요"}), 400
    with get_conn() as conn:
        cur = conn.cursor()
        for item in items:
            stock_code = (item.get("stock_code") or "").strip()
            if not stock_code:
                continue
            try:
                target_ratio = float(item.get("target_ratio") or 0)
                if not (0 <= target_ratio <= 100):
                    continue
            except (ValueError, TypeError):
                continue
            cur.execute("""
                INSERT INTO rebalance_targets (user_id, stock_code, target_ratio, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id, stock_code)
                DO UPDATE SET target_ratio = EXCLUDED.target_ratio, updated_at = NOW()
            """, (uid, stock_code, target_ratio))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — 신용 포지션 충당금 관리
# ---------------------------------------------------------------------------

@app.route("/api/credit_positions")
def api_credit_positions_list():
    """신용 포지션 목록 조회."""
    uid = _current_uid()
    rows = query("""
        SELECT id, brokerage, purchase_amount, loan_amount, note,
               TO_CHAR(updated_at, 'YYYY-MM-DD HH24:MI') AS updated_at
        FROM credit_positions WHERE user_id = %s ORDER BY brokerage
    """, (uid,))
    positions = [{
        "id":              r["id"],
        "brokerage":       r["brokerage"] or "",
        "purchase_amount": int(r["purchase_amount"]),
        "loan_amount":     int(r["loan_amount"]),
        "note":            r["note"] or "",
        "updated_at":      r["updated_at"],
    } for r in rows]

    broker_rows = query("""
        WITH latest_close AS (
            SELECT DISTINCT ON (stock_code) stock_code, close_price
            FROM supply_demand
            WHERE close_price IS NOT NULL AND close_price > 0
            ORDER BY stock_code, date DESC
        )
        SELECT
            mh.brokerage,
            COALESCE(SUM(mh.quantity * COALESCE(
                lc.close_price,
                CASE WHEN st.last_price ~ '^[0-9]+$' THEN st.last_price::BIGINT ELSE NULL END,
                mh.avg_price,
                0
            )), 0) AS stock_eval
        FROM manual_holdings mh
        LEFT JOIN latest_close lc ON lc.stock_code = mh.stock_code
        LEFT JOIN stocks st ON st.stock_code = mh.stock_code
        WHERE mh.user_id = %s
        GROUP BY mh.brokerage
        ORDER BY mh.brokerage
    """, (uid,))
    broker_stock_eval = {(r["brokerage"] or ""): round(float(r["stock_eval"] or 0)) for r in broker_rows}

    cash_rows = query("""
        SELECT brokerage, SUM(amount) AS cash_eval
        FROM cash_assets
        WHERE user_id = %s
          AND brokerage != ''
          AND asset_type_code != 'LAD'
        GROUP BY brokerage
    """, (uid,))
    broker_cash_eval = {(r["brokerage"] or ""): round(float(r["cash_eval"] or 0)) for r in cash_rows}

    return jsonify({"positions": positions, "broker_stock_eval": broker_stock_eval, "broker_cash_eval": broker_cash_eval})


@app.route("/api/credit_positions", methods=["POST"])
def api_credit_positions_upsert():
    """증권사당 1건 — 증권사 기준 UPSERT."""
    uid = _current_uid()
    data = request.get_json() or {}
    brokerage = (data.get("brokerage") or "").strip()
    if not brokerage:
        return jsonify({"error": "증권사 필수"}), 400
    try:
        purchase = int(str(data.get("purchase_amount") or 0).replace(",", ""))
        loan     = int(str(data.get("loan_amount")     or 0).replace(",", ""))
    except (ValueError, TypeError):
        return jsonify({"error": "금액 오류"}), 400
    note = (data.get("note") or "").strip()
    with get_conn() as conn:
        conn.cursor().execute("""
            INSERT INTO credit_positions (user_id, brokerage, purchase_amount, loan_amount, note, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (user_id, brokerage)
            DO UPDATE SET purchase_amount = EXCLUDED.purchase_amount,
                          loan_amount     = EXCLUDED.loan_amount,
                          note            = EXCLUDED.note,
                          updated_at      = NOW()
        """, (uid, brokerage, purchase, loan, note))
    return jsonify({"ok": True})


@app.route("/api/credit_positions/<int:pid>", methods=["PUT"])
def api_credit_positions_update(pid: int):
    """신용 포지션 수정."""
    uid = _current_uid()
    data = request.get_json() or {}
    try:
        purchase = int(str(data.get("purchase_amount") or 0).replace(",", ""))
        loan     = int(str(data.get("loan_amount")     or 0).replace(",", ""))
    except (ValueError, TypeError):
        return jsonify({"error": "금액 오류"}), 400
    note = (data.get("note") or "").strip()
    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE credit_positions SET purchase_amount=%s, loan_amount=%s, note=%s, updated_at=NOW() WHERE id=%s AND user_id=%s",
            (purchase, loan, note, pid, uid),
        )
    return jsonify({"ok": True})


@app.route("/api/credit_positions/<int:pid>", methods=["DELETE"])
def api_credit_positions_delete(pid: int):
    """신용 포지션 삭제."""
    uid = _current_uid()
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM credit_positions WHERE id=%s AND user_id=%s", (pid, uid))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — 테마 리밸런싱
# ---------------------------------------------------------------------------

@app.route("/api/theme_rebalance")
def api_theme_rebalance():
    """테마별 포트폴리오 비중 분석."""
    uid = _current_uid()
    rows = query("""
        WITH latest_close AS (
            SELECT DISTINCT ON (stock_code)
                stock_code, close_price
            FROM supply_demand
            WHERE close_price IS NOT NULL AND close_price > 0
            ORDER BY stock_code, date DESC
        ),
        holdings_agg AS (
            SELECT
                stock_code,
                MAX(stock_name) AS stock_name,
                SUM(quantity) AS total_qty,
                SUM(quantity * avg_price) / NULLIF(SUM(quantity), 0) AS weighted_avg_price
            FROM manual_holdings
            WHERE user_id = %s
            GROUP BY stock_code
        )
        SELECT
            ha.stock_code,
            ha.stock_name,
            ha.total_qty,
            ROUND(ha.weighted_avg_price::NUMERIC, 0) AS avg_price,
            COALESCE(lc.close_price,
                CASE WHEN st.last_price ~ '^[0-9]+$' THEN st.last_price::BIGINT ELSE NULL END
            ) AS current_price,
            COALESCE(sth.themes, '') AS themes
        FROM holdings_agg ha
        LEFT JOIN latest_close lc ON lc.stock_code = ha.stock_code
        LEFT JOIN stocks st ON st.stock_code = ha.stock_code
        LEFT JOIN stock_themes sth ON sth.stock_code = ha.stock_code
        ORDER BY ha.stock_name
    """, (uid,))

    settings          = _get_user_settings(uid)
    total_cash        = _get_total_cash(uid)
    alert_up          = float(settings.get("rebalance_alert_up",   30))
    alert_down        = float(settings.get("rebalance_alert_down", 25))
    watch_up          = float(settings.get("rebalance_watch_up",   round(alert_up   * 0.5, 1)))
    watch_down        = float(settings.get("rebalance_watch_down", round(alert_down * 0.5, 1)))
    cash_target_ratio = float(settings.get("cash_target_ratio", 0))

    # Calc eval amounts
    stock_total = 0
    stocks = []
    for r in rows:
        qty       = int(r["total_qty"] or 0)
        cur_price = r["current_price"]
        avg_price = float(r["avg_price"] or 0)
        eval_amt  = qty * (int(cur_price) if cur_price is not None else avg_price)
        stock_total += eval_amt
        themes_str  = (r["themes"] or "").strip()
        themes_list = [t.strip() for t in themes_str.split(",") if t.strip()]
        stocks.append({
            "stock_code":   r["stock_code"],
            "stock_name":   r["stock_name"],
            "total_qty":    qty,
            "avg_price":    int(avg_price),
            "current_price": int(cur_price) if cur_price is not None else None,
            "eval_amt":     round(eval_amt),
            "has_price":    cur_price is not None,
            "themes":       themes_list,
            "themes_str":   themes_str,
        })

    portfolio_total = stock_total + total_cash
    for s in stocks:
        s["current_ratio"] = round(s["eval_amt"] / stock_total * 100, 2) if stock_total > 0 else 0

    # Attach individual stock rebalancing signals (over/under-weighted)
    _backfill_null_user_ids(uid)
    rb_targets = {r["stock_code"]: float(r["target_ratio"])
                  for r in query("SELECT stock_code, target_ratio FROM rebalance_targets WHERE user_id = %s AND target_ratio > 0", (uid,))}
    for s in stocks:
        rb_tgt = rb_targets.get(s["stock_code"])
        if rb_tgt and rb_tgt > 0:
            s["rb_dev_rel"] = round((s["current_ratio"] - rb_tgt) / rb_tgt * 100, 1)
        else:
            s["rb_dev_rel"] = None
        s["rb_target_ratio"] = rb_tgt

    # Aggregate by theme (split eval_amt equally across themes)
    theme_data: dict[str, dict] = {}
    for s in stocks:
        themes = s["themes"]
        bucket = themes if themes else ["__untagged__"]
        n = len(bucket)
        for t in bucket:
            if t not in theme_data:
                theme_data[t] = {"eval_amt": 0.0, "stocks": []}
            theme_data[t]["eval_amt"] += s["eval_amt"] / n
            theme_data[t]["stocks"].append({
                "stock_code": s["stock_code"],
                "stock_name": s["stock_name"],
            })

    target_rows = query("SELECT theme, target_ratio, alert_up, alert_down FROM theme_targets WHERE user_id = %s", (uid,))
    targets = {r["theme"]: {
        "target_ratio": float(r["target_ratio"]),
        "alert_up":   float(r["alert_up"])   if r["alert_up"]   is not None else None,
        "alert_down": float(r["alert_down"]) if r["alert_down"] is not None else None,
    } for r in target_rows}

    theme_result = []
    for tname, data in sorted(theme_data.items(), key=lambda x: -x[1]["eval_amt"]):
        eval_amt     = data["eval_amt"]
        cur_ratio    = round(eval_amt / stock_total * 100, 2) if stock_total > 0 else 0
        tdata        = targets.get(tname, {})
        tgt_ratio    = tdata.get("target_ratio", 0)
        is_untagged  = tname == "__untagged__"
        dev_pp       = round(cur_ratio - tgt_ratio, 2) if not is_untagged else None
        dev_rel      = round(dev_pp / tgt_ratio * 100, 1) if (dev_pp is not None and tgt_ratio > 0) else None
        theme_result.append({
            "theme":         tname,
            "is_untagged":   is_untagged,
            "stocks":        data["stocks"],
            "stock_count":   len(data["stocks"]),
            "eval_amt":      round(eval_amt),
            "current_ratio": cur_ratio,
            "target_ratio":  tgt_ratio,
            "deviation_pp":  dev_pp,
            "deviation_rel": dev_rel,
            "theme_alert_up":   tdata.get("alert_up"),
            "theme_alert_down": tdata.get("alert_down"),
        })

    return jsonify({
        "themes":            theme_result,
        "stocks":            stocks,
        "portfolio_total":   round(portfolio_total),
        "stock_total":       round(stock_total),
        "total_cash":        total_cash,
        "alert_up":          alert_up,
        "alert_down":        alert_down,
        "watch_up":          watch_up,
        "watch_down":        watch_down,
        "cash_target_ratio": cash_target_ratio,
    })


@app.route("/api/theme_rebalance/theme_targets", methods=["PUT"])
def api_theme_rebalance_targets_batch():
    """테마 목표 비중 일괄 저장. body: [{theme, target_ratio}, ...]"""
    uid = _current_uid()
    items = request.get_json() or []
    if not isinstance(items, list):
        return jsonify({"error": "배열 형식 필요"}), 400
    with get_conn() as conn:
        cur = conn.cursor()
        for item in items:
            theme = (item.get("theme") or "").strip()
            if not theme:
                continue
            try:
                target_ratio = float(item.get("target_ratio") or 0)
                if not (0 <= target_ratio <= 100):
                    continue
            except (ValueError, TypeError):
                continue
            cur.execute("""
                INSERT INTO theme_targets (user_id, theme, target_ratio, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id, theme) DO UPDATE
                SET target_ratio = EXCLUDED.target_ratio, updated_at = NOW()
            """, (uid, theme, target_ratio))
    return jsonify({"ok": True})


@app.route("/api/theme_rebalance/stock_themes", methods=["PUT"])
def api_theme_rebalance_stock_themes():
    """종목에 테마 태그 지정."""
    data = request.get_json() or {}
    stock_code = (data.get("stock_code") or "").strip()
    themes_str = (data.get("themes") or "").strip()
    if not stock_code:
        return jsonify({"error": "종목코드 필수"}), 400
    with get_conn() as conn:
        conn.cursor().execute("""
            INSERT INTO stock_themes (stock_code, themes)
            VALUES (%s, %s)
            ON CONFLICT (stock_code) DO UPDATE SET themes = EXCLUDED.themes
        """, (stock_code, themes_str))
    return jsonify({"ok": True})


@app.route("/api/theme_rebalance/theme_target", methods=["PUT"])
def api_theme_rebalance_theme_target():
    """테마 목표 비중 설정."""
    uid = _current_uid()
    data = request.get_json() or {}
    theme = (data.get("theme") or "").strip()
    if not theme:
        return jsonify({"error": "테마명 필수"}), 400
    try:
        target_ratio = float(data.get("target_ratio") or 0)
        if not (0 <= target_ratio <= 100):
            return jsonify({"error": "비율은 0~100 사이여야 합니다"}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "잘못된 비율 값"}), 400
    with get_conn() as conn:
        conn.cursor().execute("""
            INSERT INTO theme_targets (user_id, theme, target_ratio, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id, theme) DO UPDATE
            SET target_ratio = EXCLUDED.target_ratio, updated_at = NOW()
        """, (uid, theme, target_ratio))
    return jsonify({"ok": True})


@app.route("/api/theme_rebalance/theme_alert", methods=["PUT"])
def api_theme_rebalance_theme_alert():
    """테마별 과다/부족 기준 개별 설정 (NULL = 전역 기준 사용)."""
    uid = _current_uid()
    data = request.get_json() or {}
    theme = (data.get("theme") or "").strip()
    if not theme:
        return jsonify({"error": "테마명 필수"}), 400
    def to_float_or_none(v):
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    alert_up   = to_float_or_none(data.get("alert_up"))
    alert_down = to_float_or_none(data.get("alert_down"))
    with get_conn() as conn:
        conn.cursor().execute("""
            INSERT INTO theme_targets (user_id, theme, target_ratio, alert_up, alert_down, updated_at)
            VALUES (%s, %s, 0, %s, %s, NOW())
            ON CONFLICT (user_id, theme) DO UPDATE
            SET alert_up = EXCLUDED.alert_up, alert_down = EXCLUDED.alert_down, updated_at = NOW()
        """, (uid, theme, alert_up, alert_down))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — 매크로 지표 관리
# ---------------------------------------------------------------------------

@app.route("/api/macro_rates")
def api_macro_rates_list():
    """거시 지표(금리·환율 등) 목록 조회."""
    rows = query("""
        SELECT id, key, name, value, unit,
               TO_CHAR(updated_at, 'YYYY-MM-DD HH24:MI') AS updated_at
        FROM macro_rates ORDER BY id
    """)
    return jsonify([{
        "id":         r["id"],
        "key":        r["key"],
        "name":       r["name"],
        "value":      float(r["value"]) if r["value"] is not None else None,
        "unit":       r["unit"] or "",
        "updated_at": r["updated_at"],
    } for r in rows])


@app.route("/api/macro_rates", methods=["POST"])
def api_macro_rates_create():
    """거시 지표 추가."""
    data = request.get_json() or {}
    key  = (data.get("key")  or "").strip().upper().replace(" ", "_")
    name = (data.get("name") or "").strip()
    if not key or not name:
        return jsonify({"error": "키와 명칭 필수"}), 400
    unit = (data.get("unit") or "").strip()
    val  = data.get("value")
    value = float(val) if val not in (None, "") else None
    with get_conn() as conn:
        conn.cursor().execute(
            "INSERT INTO macro_rates (key, name, value, unit) VALUES (%s,%s,%s,%s) ON CONFLICT (key) DO NOTHING",
            (key, name, value, unit),
        )
    return jsonify({"ok": True})


@app.route("/api/macro_rates/<int:mid>", methods=["PUT"])
def api_macro_rates_update(mid):
    """거시 지표 수정."""
    data  = request.get_json() or {}
    name  = (data.get("name") or "").strip()
    unit  = (data.get("unit") or "").strip()
    val   = data.get("value")
    value = float(val) if val not in (None, "") else None
    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE macro_rates SET name=%s, value=%s, unit=%s, updated_at=NOW() WHERE id=%s",
            (name, value, unit, mid),
        )
    return jsonify({"ok": True})


@app.route("/api/macro_rates/<int:mid>", methods=["DELETE"])
def api_macro_rates_delete(mid):
    """거시 지표 삭제."""
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM macro_rates WHERE id=%s", (mid,))
    return jsonify({"ok": True})


# 네이버 금융에서 자동 조회 가능한 키 → reuters 코드 매핑
_NAVER_REUTERS_MAP: dict[str, str] = {
    "USD_KRW": "FX_USDKRW",
    "EUR_KRW": "FX_EURKRW",
    "JPY_KRW": "FX_JPYKRW",
    "GOLD_KRX": "M04020000",  # 국내 금 시세 (KRX 금시장)
    "CNY_KRW": "FX_CNYKRW",
    "GBP_KRW": "FX_GBPKRW",
}


def _fetch_naver_gold_krx() -> float:
    """네이버 증권 API에서 국내 금 시세(원/g) 조회.

    stock.naver.com/marketindex/metals/M04020000/price 페이지가 내부적으로
    호출하는 REST API를 직접 사용.
    응답 JSON: {"closePrice": "136,780", ...}
    """
    _NAVER_GOLD_URLS = [
        "https://api.stock.naver.com/marketindex/metals/M04020000",
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://stock.naver.com/",
        "Accept": "application/json",
    }
    import requests as _req
    last_err: Exception | None = None
    for url in _NAVER_GOLD_URLS:
        try:
            resp = _req.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            # API 응답 필드명 후보: closePrice, price, currentPrice, close
            for field in ("closePrice", "price", "currentPrice", "close"):
                raw = data.get(field)
                if raw is not None:
                    price = float(str(raw).replace(",", ""))
                    if price > 0:
                        return price
            # 리스트 형태 응답 처리
            if isinstance(data, list) and data:
                item = data[0]
                for field in ("closePrice", "price", "currentPrice", "close"):
                    raw = item.get(field)
                    if raw is not None:
                        price = float(str(raw).replace(",", ""))
                        if price > 0:
                            return price
        except Exception as e:
            last_err = e
    raise ValueError(f"금 시세 조회 실패: {last_err}")


@app.route("/api/macro_rates/<int:mid>/sync_naver", methods=["POST"])
def api_macro_rates_sync_naver(mid):
    """네이버 금융에서 환율/금 시세를 파싱해 macro_rates 값 업데이트.

    환율: finance.naver.com HTML (EUC-KR, span class='noX')
    금:   api.stock.naver.com REST API (JSON closePrice)
    """
    import re as _re
    import requests as _req

    row = query_one("SELECT key FROM macro_rates WHERE id = %s", (mid,))
    if not row:
        return jsonify({"error": "지표 없음"}), 404

    key = row["key"]
    reuters_code = _NAVER_REUTERS_MAP.get(key)
    if not reuters_code:
        return jsonify({"error": f"'{key}'는 네이버 자동 동기화를 지원하지 않습니다"}), 400

    if key == "GOLD_KRX":
        try:
            close_price = _fetch_naver_gold_krx()
        except Exception as e:
            logging.exception("네이버 금 시세 조회 실패")
            return jsonify({"error": f"네이버 조회 실패: {e}"}), 502
    else:
        try:
            url = f"https://finance.naver.com/marketindex/exchangeDetail.nhn?marketindexCd={reuters_code}"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            }
            resp = _req.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            resp.encoding = "euc-kr"
            html = resp.text

            # 현재가 블록 추출 (no_today … txt_won)
            block_m = _re.search(
                r"class=[\"']no_today[\"'].*?class=[\"']txt_won[\"']",
                html, _re.DOTALL,
            )
            if not block_m:
                raise ValueError("환율 블록을 찾을 수 없습니다")
            block = block_m.group(0)

            # no[숫자] → 해당 숫자, shim → ',', jum → '.' 순서대로 조립
            price_str = ""
            for m in _re.finditer(r"class=[\"'](no\d|shim|jum)[\"']", block):
                cls = m.group(1)
                if cls.startswith("no"):
                    price_str += cls[2]   # 'no4' → '4'
                elif cls == "shim":
                    price_str += ","
                else:
                    price_str += "."

            if not price_str:
                raise ValueError("환율 숫자를 파싱하지 못했습니다")

            close_price = float(price_str.replace(",", ""))

        except Exception as e:
            logging.exception("네이버 환율 조회 실패: %s", key)
            return jsonify({"error": f"네이버 조회 실패: {e}"}), 502

    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE macro_rates SET value = %s, updated_at = NOW() WHERE id = %s",
            (close_price, mid),
        )
    return jsonify({"ok": True, "value": close_price, "key": key})


# ---------------------------------------------------------------------------
# API — 현금성 자산 관리
# ---------------------------------------------------------------------------

def _parse_cash_asset_body(data: dict):
    """Request body → (name, brokerage, qty, up, purchase_price, amount, link_type, link_key, note, asset_type_code)."""
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("자산명 필수")
    brokerage = (data.get("brokerage") or "").strip()
    raw_qty = data.get("quantity")
    raw_up  = data.get("unit_price")
    raw_pp  = data.get("purchase_price")
    qty_val = float(raw_qty) if raw_qty not in (None, "") else None
    up_val  = int(raw_up)   if raw_up  not in (None, "") else None
    pp_val  = int(raw_pp)   if raw_pp  not in (None, "") else None
    if qty_val is not None and up_val is not None:
        amount = round(qty_val * up_val)
    else:
        try:
            amount = int(str(data.get("amount") or 0).replace(",", ""))
        except (ValueError, TypeError):
            raise ValueError("평가금액 오류")
    link_type       = (data.get("link_type")       or "none").strip()
    link_key        = (data.get("link_key")        or "").strip()
    note            = (data.get("note")            or "").strip()
    asset_type_code = (data.get("asset_type_code") or "").strip().upper()
    return name, brokerage, qty_val, up_val, pp_val, amount, link_type, link_key, note, asset_type_code


def _resolve_linked_price(link_type: str, link_key: str):
    """연동 설정에 따른 최신 단가(원) 반환. 없으면 None."""
    if link_type == "stock" and link_key:
        rows = query("""
            SELECT COALESCE(
                (SELECT close_price FROM supply_demand
                 WHERE stock_code=%s AND close_price>0 ORDER BY date DESC LIMIT 1),
                (SELECT CASE WHEN last_price ~ '^[0-9]+$' THEN last_price::BIGINT ELSE NULL END
                 FROM stocks WHERE stock_code=%s)
            ) AS price
        """, (link_key, link_key))
        if rows and rows[0]["price"] is not None:
            return int(rows[0]["price"])
    elif link_type == "macro" and link_key:
        rows = query("SELECT value FROM macro_rates WHERE key=%s", (link_key,))
        if rows and rows[0]["value"] is not None:
            return float(rows[0]["value"])
    return None


@app.route("/api/cash_assets")
def api_cash_assets_list():
    """현금성 자산 목록 및 합계 조회."""
    uid = _current_uid()
    rows = query("""
        SELECT id, name, brokerage, asset_type_code, quantity, unit_price, purchase_price, amount, link_type, link_key, note,
               TO_CHAR(updated_at, 'YYYY-MM-DD HH24:MI') AS updated_at
        FROM cash_assets WHERE user_id = %s ORDER BY brokerage, id
    """, (uid,))
    items = []
    total = 0
    for r in rows:
        amt = int(r["amount"])
        total += amt
        items.append({
            "id":               r["id"],
            "name":             r["name"],
            "brokerage":        r["brokerage"]       or "",
            "asset_type_code":  r["asset_type_code"] or "",
            "quantity":         float(r["quantity"])       if r["quantity"]       is not None else None,
            "unit_price":       int(r["unit_price"])       if r["unit_price"]     is not None else None,
            "purchase_price":   int(r["purchase_price"])   if r["purchase_price"] is not None else None,
            "amount":           amt,
            "link_type":        r["link_type"] or "none",
            "link_key":         r["link_key"]  or "",
            "note":             r["note"] or "",
            "updated_at":       r["updated_at"],
        })
    return jsonify({"items": items, "total": total})


@app.route("/api/cash_assets", methods=["POST"])
def api_cash_assets_create():
    """현금성 자산 추가."""
    uid = _current_uid()
    try:
        name, brokerage, qty, up, pp, amount, lt, lk, note, atc = _parse_cash_asset_body(request.get_json() or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    with get_conn() as conn:
        conn.cursor().execute(
            "INSERT INTO cash_assets (user_id, name, brokerage, asset_type_code, quantity, unit_price, purchase_price, amount, link_type, link_key, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (uid, name, brokerage, atc, qty, up, pp, amount, lt, lk, note),
        )
    return jsonify({"ok": True})


@app.route("/api/cash_assets/<int:aid>", methods=["PUT"])
def api_cash_assets_update(aid):
    """현금성 자산 수정."""
    uid = _current_uid()
    try:
        name, brokerage, qty, up, pp, amount, lt, lk, note, atc = _parse_cash_asset_body(request.get_json() or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE cash_assets SET name=%s, brokerage=%s, asset_type_code=%s, quantity=%s, unit_price=%s, purchase_price=%s, amount=%s, link_type=%s, link_key=%s, note=%s, updated_at=NOW() WHERE id=%s AND user_id=%s",
            (name, brokerage, atc, qty, up, pp, amount, lt, lk, note, aid, uid),
        )
    return jsonify({"ok": True})


@app.route("/api/cash_assets/<int:aid>", methods=["DELETE"])
def api_cash_assets_delete(aid):
    """현금성 자산 삭제."""
    uid = _current_uid()
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM cash_assets WHERE id=%s AND user_id=%s", (aid, uid))
    return jsonify({"ok": True})


@app.route("/api/cash_assets/<int:aid>/sync", methods=["POST"])
def api_cash_assets_sync(aid):
    """연동 자산 현재 시세 동기화."""
    uid = _current_uid()
    rows = query("SELECT quantity, link_type, link_key FROM cash_assets WHERE id=%s AND user_id=%s", (aid, uid))
    if not rows:
        return jsonify({"error": "없음"}), 404
    r   = rows[0]
    qty = float(r["quantity"]) if r["quantity"] is not None else None
    if not qty:
        return jsonify({"error": "수량 없음"}), 400
    price = _resolve_linked_price(r["link_type"], r["link_key"])
    if price is None:
        return jsonify({"error": "최신 가격 없음"}), 404
    amount = round(qty * price)
    up_int = round(price)
    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE cash_assets SET amount=%s, unit_price=%s, updated_at=NOW() WHERE id=%s",
            (amount, up_int, aid),
        )
    return jsonify({"ok": True, "amount": amount, "unit_price": up_int})


@app.route("/api/cash_assets/sync_all", methods=["POST"])
def api_cash_assets_sync_all():
    """전체 연동 자산 시세 일괄 동기화."""
    uid = _current_uid()
    rows = query("SELECT id, quantity, link_type, link_key FROM cash_assets WHERE user_id=%s AND link_type != 'none' AND link_key != ''", (uid,))
    updated, failed = 0, 0
    for r in rows:
        qty = float(r["quantity"]) if r["quantity"] is not None else None
        if not qty:
            failed += 1
            continue
        price = _resolve_linked_price(r["link_type"], r["link_key"])
        if price is None:
            failed += 1
            continue
        with get_conn() as conn:
            conn.cursor().execute(
                "UPDATE cash_assets SET amount=%s, unit_price=%s, updated_at=NOW() WHERE id=%s",
                (round(qty * price), round(price), r["id"]),
            )
        updated += 1
    return jsonify({"ok": True, "updated": updated, "failed": failed})


# ---------------------------------------------------------------------------
# API — 정성 점수 관리
# ---------------------------------------------------------------------------

@app.route("/api/qualitative/items")
def api_qualitative_items():
    """정성 평가 항목 목록 조회."""
    rows = query("""
        WITH ranked AS (
            SELECT item_id, score, scored_at, comment,
                   ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY scored_at DESC, created_at DESC) AS rn
            FROM qualitative_scores
        )
        SELECT
            qi.id, qi.name, qi.category, qi.description, qi.sort_order,
            r1.score      AS latest_score,
            r1.scored_at  AS latest_date,
            r1.comment    AS latest_comment,
            r2.score      AS prev_score
        FROM qualitative_items qi
        LEFT JOIN ranked r1 ON r1.item_id = qi.id AND r1.rn = 1
        LEFT JOIN ranked r2 ON r2.item_id = qi.id AND r2.rn = 2
        WHERE qi.active = TRUE
        ORDER BY qi.sort_order, qi.name
    """)
    for r in rows:
        r["latest_score"] = float(r["latest_score"]) if r["latest_score"] is not None else None
        r["prev_score"]   = float(r["prev_score"])   if r["prev_score"]   is not None else None
        r["latest_date"]  = r["latest_date"].strftime("%Y-%m-%d") if r["latest_date"] else None
        if r["latest_score"] is not None and r["prev_score"] is not None:
            r["delta"] = round(r["latest_score"] - r["prev_score"], 1)
        else:
            r["delta"] = None
    return jsonify(rows)


@app.route("/api/qualitative/items", methods=["POST"])
def api_qualitative_items_create():
    """정성 평가 항목 추가."""
    data = request.get_json() or {}
    name     = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip()
    desc     = (data.get("description") or "").strip()
    if not name:
        return jsonify({"error": "항목명 필수"}), 400
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO qualitative_items (name, category, description)
            VALUES (%s, %s, %s) RETURNING id
        """, (name, category, desc))
        row = cur.fetchone()
    return jsonify({"ok": True, "id": row["id"]}), 201


@app.route("/api/qualitative/items/<int:item_id>", methods=["PUT"])
def api_qualitative_items_update(item_id: int):
    """정성 평가 항목 수정."""
    data = request.get_json() or {}
    name     = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip()
    desc     = (data.get("description") or "").strip()
    if not name:
        return jsonify({"error": "항목명 필수"}), 400
    with get_conn() as conn:
        conn.cursor().execute("""
            UPDATE qualitative_items SET name=%s, category=%s, description=%s WHERE id=%s
        """, (name, category, desc, item_id))
    return jsonify({"ok": True})


@app.route("/api/qualitative/items/<int:item_id>", methods=["DELETE"])
def api_qualitative_items_delete(item_id: int):
    """정성 평가 항목 삭제."""
    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE qualitative_items SET active=FALSE WHERE id=%s", (item_id,)
        )
    return jsonify({"ok": True})


@app.route("/api/qualitative/items/<int:item_id>/scores")
def api_qualitative_item_scores(item_id: int):
    """정성 평가 항목별 점수 이력 조회."""
    rows = query("""
        SELECT id, score, scored_at, comment,
               LAG(score) OVER (ORDER BY scored_at, created_at) AS prev_score
        FROM qualitative_scores
        WHERE item_id = %s
        ORDER BY scored_at, created_at
    """, (item_id,))
    result = []
    for r in rows:
        score      = float(r["score"])
        prev_score = float(r["prev_score"]) if r["prev_score"] is not None else None
        result.append({
            "id":         r["id"],
            "score":      score,
            "scored_at":  r["scored_at"].strftime("%Y-%m-%d"),
            "comment":    r["comment"] or "",
            "delta":      round(score - prev_score, 1) if prev_score is not None else None,
        })
    return jsonify(result)


@app.route("/api/qualitative/scores", methods=["POST"])
def api_qualitative_scores_create():
    """정성 평가 점수 추가."""
    data = request.get_json() or {}
    item_id   = data.get("item_id")
    score_raw = data.get("score")
    scored_at = (data.get("scored_at") or "").strip()
    comment   = (data.get("comment") or "").strip()
    if not item_id:
        return jsonify({"error": "항목 ID 필수"}), 400
    try:
        score = float(score_raw)
        if not (1 <= score <= 10):
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"error": "점수는 1~10 사이여야 합니다"}), 400
    from datetime import date as date_cls
    try:
        from datetime import datetime
        dt = datetime.strptime(scored_at, "%Y-%m-%d").date() if scored_at else date_cls.today()
    except ValueError:
        dt = date_cls.today()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO qualitative_scores (item_id, score, scored_at, comment)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (item_id, score, dt, comment))
        row = cur.fetchone()
    return jsonify({"ok": True, "id": row["id"]}), 201


@app.route("/api/qualitative/scores/<int:score_id>", methods=["DELETE"])
def api_qualitative_scores_delete(score_id: int):
    """정성 평가 점수 삭제."""
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM qualitative_scores WHERE id=%s", (score_id,))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — 공급자 마켓파워 점수
# ---------------------------------------------------------------------------

def _mp_grade(total: int) -> str:
    if total >= 85: return "S"
    if total >= 75: return "A"
    if total >= 65: return "B"
    if total >= 55: return "C"
    if total >= 45: return "D"
    return "E"


@app.route("/api/market_power")
def api_market_power_list():
    """종목별 최신 점수 목록."""
    uid = _current_uid()
    rows = query("""
        SELECT DISTINCT ON (stock_code)
            id, stock_code, stock_name, scored_at,
            supply_bottleneck, irreplaceability, pricing_power,
            demand_visibility, expansion_difficulty, customer_lockin,
            total_score, grade,
            price_attractiveness, earnings_momentum, composite_score, memo
        FROM market_power_scores
        WHERE user_id = %s
        ORDER BY stock_code, scored_at DESC
    """, [uid])
    return jsonify([dict(r) for r in rows])


@app.route("/api/market_power/<stock_code>/history")
def api_market_power_history(stock_code: str):
    """특정 종목의 점수 이력."""
    uid = _current_uid()
    rows = query("""
        SELECT id, scored_at, supply_bottleneck, irreplaceability, pricing_power,
               demand_visibility, expansion_difficulty, customer_lockin,
               total_score, grade, memo
        FROM market_power_scores
        WHERE user_id = %s AND stock_code = %s
        ORDER BY scored_at DESC
    """, [uid, stock_code])
    return jsonify([dict(r) for r in rows])


@app.route("/api/market_power", methods=["POST"])
def api_market_power_save():
    """점수 저장 (같은 날짜·종목이면 UPDATE)."""
    uid  = _current_uid()
    body = request.json or {}
    stock_code = body.get("stock_code", "").strip()
    stock_name = body.get("stock_name", "").strip()
    scored_at  = body.get("scored_at") or None

    dims = {
        "supply_bottleneck":    min(max(int(body.get("supply_bottleneck",    0)), 0), 25),
        "irreplaceability":     min(max(int(body.get("irreplaceability",     0)), 0), 20),
        "pricing_power":        min(max(int(body.get("pricing_power",        0)), 0), 20),
        "demand_visibility":    min(max(int(body.get("demand_visibility",    0)), 0), 15),
        "expansion_difficulty": min(max(int(body.get("expansion_difficulty", 0)), 0), 10),
        "customer_lockin":      min(max(int(body.get("customer_lockin",      0)), 0), 10),
    }
    total = sum(dims.values())
    grade = _mp_grade(total)
    memo  = body.get("memo", "")

    pa_raw = body.get("price_attractiveness")
    em_raw = body.get("earnings_momentum")
    pa = min(max(int(pa_raw), 0), 100) if pa_raw not in (None, "") else None
    em = min(max(int(em_raw), 0), 100) if em_raw not in (None, "") else None
    composite = round(total * 0.6 + pa * 0.25 + em * 0.15, 2) if (pa is not None and em is not None) else None

    with get_conn() as conn:
        conn.cursor().execute("""
            INSERT INTO market_power_scores
                (user_id, stock_code, stock_name, scored_at,
                 supply_bottleneck, irreplaceability, pricing_power,
                 demand_visibility, expansion_difficulty, customer_lockin,
                 total_score, grade,
                 price_attractiveness, earnings_momentum, composite_score, memo)
            VALUES (%s,%s,%s,COALESCE(%s::date, CURRENT_DATE),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id, stock_code, scored_at) DO UPDATE SET
                stock_name=EXCLUDED.stock_name,
                supply_bottleneck=EXCLUDED.supply_bottleneck,
                irreplaceability=EXCLUDED.irreplaceability,
                pricing_power=EXCLUDED.pricing_power,
                demand_visibility=EXCLUDED.demand_visibility,
                expansion_difficulty=EXCLUDED.expansion_difficulty,
                customer_lockin=EXCLUDED.customer_lockin,
                total_score=EXCLUDED.total_score,
                grade=EXCLUDED.grade,
                price_attractiveness=EXCLUDED.price_attractiveness,
                earnings_momentum=EXCLUDED.earnings_momentum,
                composite_score=EXCLUDED.composite_score,
                memo=EXCLUDED.memo
        """, [uid, stock_code, stock_name, scored_at,
              dims["supply_bottleneck"], dims["irreplaceability"], dims["pricing_power"],
              dims["demand_visibility"], dims["expansion_difficulty"], dims["customer_lockin"],
              total, grade, pa, em, composite, memo])
    return jsonify({"ok": True, "total_score": total, "grade": grade, "composite_score": composite})


@app.route("/api/market_power/suggestions")
def api_market_power_suggestions():
    """마켓파워 기반 종목별 목표비율 제안 (3-점수 체계).

    전략점수       = MP×75% + 실적모멘텀×20% + 가격매력×5%
    allocation     = (전략점수/100)^1.5 × (역할점수/20)^1.8
    매수우선점수   = MP×30% + 가격매력×45% + 실적모멘텀×25%
    감액우선점수   = 초과비중×50% + 가격부담×30% + 모멘텀둔화×20%

    제안비율 = 테마 목표비율 × (allocation / 테마 내 allocation 합)
    """
    from collections import defaultdict
    uid = _current_uid()

    # 최신 마켓파워 점수
    mp_rows = query("""
        SELECT DISTINCT ON (stock_code)
            stock_code, stock_name, total_score, grade,
            price_attractiveness, earnings_momentum, composite_score
        FROM market_power_scores
        WHERE user_id = %s
        ORDER BY stock_code, scored_at DESC
    """, [uid])

    # 테마 매핑
    theme_rows = query("SELECT stock_code, themes FROM stock_themes")
    theme_map  = {}
    for r in theme_rows:
        t = (r["themes"] or "").split(",")[0].strip()
        if t:
            theme_map[r["stock_code"]] = t

    # 테마 목표비율
    tt_rows = query("SELECT theme, target_ratio FROM theme_targets WHERE user_id = %s", [uid])
    theme_target = {r["theme"]: float(r["target_ratio"] or 0) for r in tt_rows}

    # 기존 목표비율 + 티어 정보 + 역할점수
    rb_rows = query("""
        SELECT stock_code, target_ratio,
               COALESCE(position_tier, 'MID')       AS position_tier,
               COALESCE(max_change_pp, 1.5)         AS max_change_pp,
               COALESCE(overweight_band_pp, 3.0)    AS overweight_band_pp,
               COALESCE(review_band_pp, 1.5)        AS review_band_pp,
               COALESCE(role_score, 0)              AS role_score,
               COALESCE(role_weight, 1.00)          AS role_weight
        FROM rebalance_targets WHERE user_id = %s
    """, [uid])
    rb_map = {r["stock_code"]: r for r in rb_rows}

    # 현재 보유 평가금액 (감액점수의 초과비중 계산용)
    hold_rows = query("""
        WITH lc AS (
            SELECT DISTINCT ON (stock_code) stock_code, close_price
            FROM supply_demand WHERE close_price IS NOT NULL AND close_price > 0
            ORDER BY stock_code, date DESC
        )
        SELECT mh.stock_code, SUM(mh.quantity) AS qty,
               COALESCE(lc.close_price, 0) AS price
        FROM manual_holdings mh
        LEFT JOIN lc ON lc.stock_code = mh.stock_code
        WHERE mh.user_id = %s AND mh.quantity > 0
        GROUP BY mh.stock_code, lc.close_price
    """, [uid])
    stock_eval = {r["stock_code"]: int(r["qty"] or 0) * int(r["price"] or 0) for r in hold_rows}
    total_eval = sum(stock_eval.values())

    # 종목별 3-점수 계산
    stocks = []
    for r in mp_rows:
        code = r["stock_code"]
        mp   = float(r["total_score"]          or 0)
        pa   = float(r["price_attractiveness"] or 0) if r["price_attractiveness"] is not None else 0.0
        em   = float(r["earnings_momentum"]    or 0) if r["earnings_momentum"]    is not None else 0.0

        target_score = round(mp * 0.75 + em * 0.20 + pa * 0.05, 2)
        buy_score    = round(mp * 0.30 + pa * 0.45 + em * 0.25, 2)

        rb          = rb_map.get(code, {})
        existing    = float(rb.get("target_ratio") or 0)
        ob_pp       = float(rb.get("overweight_band_pp") or 3.0)
        role_weight = float(rb.get("role_weight") or 1.00)
        role_score  = int(rb.get("role_score") or 0)

        # allocation_score: 비선형 배분 점수
        # (전략점수/100)^1.5 × (역할점수/20)^1.8  — 역할점수 10 이하는 제안 제외
        if role_score > 10:
            allocation_score = round((target_score / 100) ** 1.5 * (role_score / 20) ** 1.8, 6)
        elif role_score > 0:
            allocation_score = 0.0  # 후보 제외
        else:
            allocation_score = round((target_score / 100) ** 1.5, 6)  # 역할점수 미입력: 전략점수만

        # 현재비율 (주식총액 기준)
        cur_eval   = stock_eval.get(code, 0)
        cur_ratio  = round(cur_eval / total_eval * 100, 2) if total_eval > 0 else 0.0

        # 감액우선점수 (현재비율 > 목표비율 인 경우만 의미)
        overweight_raw   = cur_ratio - existing
        overweight_score = min(100.0, max(0.0, overweight_raw / ob_pp * 100)) if ob_pp > 0 else 0.0
        sell_score       = round(overweight_score * 0.50 + (100 - pa) * 0.30 + (100 - em) * 0.20, 2)

        stocks.append({
            "stock_code":           code,
            "stock_name":           r["stock_name"],
            "theme":                theme_map.get(code, ""),
            "mp_score":             mp,
            "grade":                r["grade"],
            "price_attractiveness": r["price_attractiveness"],
            "earnings_momentum":    r["earnings_momentum"],
            "composite_score":      r["composite_score"],
            "target_score":         target_score,
            "allocation_score":     allocation_score,
            "role_score":           role_score,
            "role_weight":          role_weight,
            "buy_score":            buy_score,
            "sell_score":           sell_score,
            "existing_target":      existing,
            "current_ratio":        cur_ratio,
            "position_tier":        rb.get("position_tier") or "MID",
            "max_change_pp":        float(rb.get("max_change_pp") or 1.5),
        })

    # 테마별 allocation_score 합산
    theme_alloc_sum: dict[str, float] = defaultdict(float)
    for s in stocks:
        if s["theme"]:
            theme_alloc_sum[s["theme"]] += s["allocation_score"]

    # 제안비율 + 앵커링
    for s in stocks:
        t = s["theme"]
        if t and theme_alloc_sum[t] > 0 and t in theme_target:
            s["theme_target_ratio"] = theme_target[t]
            suggested = round(theme_target[t] * s["allocation_score"] / theme_alloc_sum[t], 2)
            s["suggested_ratio"] = suggested
            s["diff"]            = round(suggested - s["existing_target"], 2)
        else:
            s["theme_target_ratio"] = theme_target.get(t)
            s["suggested_ratio"]    = None
            s["diff"]               = None

    stocks.sort(key=lambda x: (x["theme"] or "zzz", -(x["allocation_score"] or 0)))
    return jsonify(stocks)


# ─── 역할점수 관리 API ──────────────────────────────────────────────────────

@app.route("/api/role_scores")
def api_role_scores_get():
    """종목별 역할점수 조회."""
    uid = _current_uid()
    rows = query("""
        SELECT rt.stock_code,
               COALESCE(mps.stock_name, rt.stock_code)  AS stock_name,
               COALESCE(st.themes, '')                   AS theme,
               COALESCE(rt.theme_purity_score, 0)          AS theme_purity_score,
               COALESCE(rt.theme_leader_score, 0)          AS theme_leader_score,
               COALESCE(rt.bottleneck_centrality_score, 0) AS bottleneck_centrality_score,
               COALESCE(rt.earnings_sensitivity_score, 0)  AS earnings_sensitivity_score,
               COALESCE(rt.portfolio_role_score, 0)        AS portfolio_role_score,
               COALESCE(rt.role_score, 0)                  AS role_score,
               COALESCE(rt.role_weight, 1.00)              AS role_weight
        FROM rebalance_targets rt
        LEFT JOIN (
            SELECT DISTINCT ON (stock_code) stock_code, stock_name
            FROM market_power_scores WHERE user_id = %s
            ORDER BY stock_code, scored_at DESC
        ) mps ON mps.stock_code = rt.stock_code
        LEFT JOIN stock_themes st ON st.stock_code = rt.stock_code
        WHERE rt.user_id = %s
        ORDER BY st.themes NULLS LAST, rt.stock_code
    """, [uid, uid])
    return jsonify([dict(r) for r in rows])


@app.route("/api/role_scores/<stock_code>", methods=["PUT"])
def api_role_scores_put(stock_code):
    """종목 역할점수 저장 (자동으로 role_score/role_weight 계산)."""
    uid = _current_uid()
    data = request.get_json() or {}

    tp = max(0, min(5, int(data.get("theme_purity_score")          or 0)))
    tl = max(0, min(5, int(data.get("theme_leader_score")          or 0)))
    bc = max(0, min(5, int(data.get("bottleneck_centrality_score") or 0)))
    es = max(0, min(5, int(data.get("earnings_sensitivity_score")  or 0)))
    pr = max(0, min(5, int(data.get("portfolio_role_score")        or 0)))

    role_score  = tp + tl + bc + es + pr
    role_weight = _role_weight_from_score(role_score)

    with get_conn() as conn:
        conn.cursor().execute("""
            UPDATE rebalance_targets
            SET theme_purity_score          = %s,
                theme_leader_score          = %s,
                bottleneck_centrality_score = %s,
                earnings_sensitivity_score  = %s,
                portfolio_role_score        = %s,
                role_score                  = %s,
                role_weight                 = %s,
                updated_at                  = NOW()
            WHERE user_id = %s AND stock_code = %s
        """, (tp, tl, bc, es, pr, role_score, role_weight, uid, stock_code))
    return jsonify({"ok": True, "role_score": role_score, "role_weight": role_weight})


@app.route("/api/market_power/theme_suggestions")
def api_market_power_theme_suggestions():
    """마켓파워 기반 테마 목표비율 제안.

    테마 종합점수 = MP평균×60% + 가격매력평균×25% + 실적모멘텀평균×15%
    변곡점 신호  = 20일 이탈률 기준 (±7% / ±12%)
    권장 목표비율 = 기존 ± 기본조정폭(1%p) / 강한조정폭(2%p)
    정규화 목표  = 권장 합계 → 100% 재조정
    """
    from collections import defaultdict
    uid        = _current_uid()
    adj_basic  = max(0.0, float(request.args.get("adj_basic",  1.0)))
    adj_strong = max(0.0, float(request.args.get("adj_strong", 2.0)))

    # 최신 마켓파워 점수 (종목별 1건)
    mp_rows = query("""
        SELECT DISTINCT ON (stock_code)
            stock_code, total_score, price_attractiveness, earnings_momentum
        FROM market_power_scores
        WHERE user_id = %s
        ORDER BY stock_code, scored_at DESC
    """, [uid])

    # 테마 매핑 (첫 번째 테마만)
    theme_rows = query("SELECT stock_code, themes FROM stock_themes")
    theme_map  = {}
    for r in theme_rows:
        t = (r["themes"] or "").split(",")[0].strip()
        if t:
            theme_map[r["stock_code"]] = t

    # 현재 테마 목표비율
    tt_rows      = query("SELECT theme, target_ratio FROM theme_targets WHERE user_id = %s", [uid])
    theme_target = {r["theme"]: float(r["target_ratio"] or 0) for r in tt_rows}

    # 보유종목 평가금액 (테마 현재비율 계산용)
    hold_rows = query("""
        WITH lc AS (
            SELECT DISTINCT ON (stock_code) stock_code, close_price
            FROM supply_demand WHERE close_price IS NOT NULL AND close_price > 0
            ORDER BY stock_code, date DESC
        )
        SELECT mh.stock_code, SUM(mh.quantity) AS qty,
               COALESCE(lc.close_price, 0) AS price
        FROM manual_holdings mh
        LEFT JOIN lc ON lc.stock_code = mh.stock_code
        WHERE mh.user_id = %s AND mh.quantity > 0
        GROUP BY mh.stock_code, lc.close_price
    """, [uid])
    stock_eval  = {r["stock_code"]: int(r["qty"] or 0) * int(r["price"] or 0) for r in hold_rows}
    total_eval  = sum(stock_eval.values())
    theme_eval: dict[str, float] = defaultdict(float)
    for code, val in stock_eval.items():
        t = theme_map.get(code, "")
        if t:
            theme_eval[t] += val

    # 테마별 점수 집계
    tm_mp: dict[str, list] = defaultdict(list)
    tm_pa: dict[str, list] = defaultdict(list)
    tm_em: dict[str, list] = defaultdict(list)
    for r in mp_rows:
        t = theme_map.get(r["stock_code"], "")
        if not t:
            continue
        tm_mp[t].append(float(r["total_score"] or 0))
        if r["price_attractiveness"] is not None:
            tm_pa[t].append(float(r["price_attractiveness"]))
        if r["earnings_momentum"] is not None:
            tm_em[t].append(float(r["earnings_momentum"]))

    # 20일 평균 마켓파워 (이력 기반, 없으면 None)
    hist_rows = query("""
        SELECT stock_code, AVG(total_score) AS avg_mp
        FROM market_power_scores
        WHERE user_id = %s AND scored_at >= CURRENT_DATE - INTERVAL '20 days'
        GROUP BY stock_code
    """, [uid])
    tm_hist_mp: dict[str, list] = defaultdict(list)
    for r in hist_rows:
        t = theme_map.get(r["stock_code"], "")
        if t and r["avg_mp"] is not None:
            tm_hist_mp[t].append(float(r["avg_mp"]))

    # 테마 목록 = 기존 목표 + 마켓파워 있는 테마 합집합
    all_themes = sorted(set(list(theme_target.keys()) + list(tm_mp.keys())))

    result = []
    for theme in all_themes:
        mp_list = tm_mp.get(theme, [])
        pa_list = tm_pa.get(theme, [])
        em_list = tm_em.get(theme, [])

        mp_avg = round(sum(mp_list) / len(mp_list), 2) if mp_list else None
        pa_avg = round(sum(pa_list) / len(pa_list), 2) if pa_list else None
        em_avg = round(sum(em_list) / len(em_list), 2) if em_list else None

        if mp_avg is not None and pa_avg is not None and em_avg is not None:
            composite = round(mp_avg * 0.6 + pa_avg * 0.25 + em_avg * 0.15, 2)
        elif mp_avg is not None:
            composite = mp_avg
        else:
            composite = None

        hist_list = tm_hist_mp.get(theme, [])
        avg_20d   = round(sum(hist_list) / len(hist_list), 2) if hist_list else None

        if composite is not None and avg_20d is not None and avg_20d > 0:
            deviation = round((composite - avg_20d) / avg_20d * 100, 2)
        else:
            deviation = None

        if deviation is None:
            signal = "20일평균 없음"
        elif deviation >= 12:
            signal = "강한 상향"
        elif deviation >= 7:
            signal = "상향 후보"
        elif deviation >= -7:
            signal = "유지"
        elif deviation >= -12:
            signal = "하향 후보"
        else:
            signal = "강한 하향"

        existing = theme_target.get(theme, 0.0)
        if signal == "강한 상향":
            recommended = existing + adj_strong
        elif signal == "상향 후보":
            recommended = existing + adj_basic
        elif signal == "하향 후보":
            recommended = existing - adj_basic
        elif signal == "강한 하향":
            recommended = existing - adj_strong
        else:
            recommended = existing
        recommended = round(max(0.0, recommended), 2)

        cur_ratio = round(theme_eval.get(theme, 0) / total_eval * 100, 2) if total_eval > 0 else 0

        result.append({
            "theme":         theme,
            "stock_count":   len(mp_list),
            "current_ratio": cur_ratio,
            "existing_target": existing,
            "mp_avg":        mp_avg,
            "pa_avg":        pa_avg,
            "em_avg":        em_avg,
            "composite":     composite,
            "avg_20d":       avg_20d,
            "deviation":     deviation,
            "signal":        signal,
            "recommended":   recommended,
        })

    # 마켓파워 점수 있는 테마만 (현금 등 비주식 테마 제외), recommended 합계 100%로 정규화
    result = [r for r in result if r["stock_count"] > 0]
    total_rec = sum(r["recommended"] for r in result)
    if total_rec > 0:
        for r in result:
            r["recommended"] = round(r["recommended"] / total_rec * 100, 2)
    result.sort(key=lambda x: -(x["composite"] or 0))
    return jsonify({"themes": result, "total_eval": total_eval})


@app.route("/api/market_power/theme_suggestions/apply", methods=["POST"])
def api_market_power_theme_suggestions_apply():
    """정규화 테마 목표비율 일괄 적용 → theme_targets."""
    uid   = _current_uid()
    items = request.json or []   # [{theme, target_ratio}, ...]
    with get_conn() as conn:
        cur = conn.cursor()
        for item in items:
            theme = (item.get("theme") or "").strip()
            if not theme:
                continue
            ratio = round(float(item.get("target_ratio") or 0), 2)
            cur.execute("""
                INSERT INTO theme_targets (user_id, theme, target_ratio, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id, theme) DO UPDATE
                SET target_ratio = EXCLUDED.target_ratio, updated_at = NOW()
            """, (uid, theme, ratio))
    return jsonify({"ok": True})


@app.route("/api/market_power/theme_suggestions/stock_preview", methods=["POST"])
def api_market_power_stock_preview():
    """테마 목표비율(안) → 종목별 예상 거래량 계산.

    요청: [{theme, target_ratio}, ...]
    응답: {stocks: [...], total_eval: int}
    """
    from collections import defaultdict
    uid      = _current_uid()
    proposed = request.json or []
    proposed_map = {item["theme"]: float(item["target_ratio"]) for item in proposed}

    # 현재 보유 종목 + 최신 현재가
    hold_rows = query("""
        WITH lc AS (
            SELECT DISTINCT ON (stock_code) stock_code, close_price
            FROM supply_demand WHERE close_price IS NOT NULL AND close_price > 0
            ORDER BY stock_code, date DESC
        )
        SELECT mh.stock_code, MAX(mh.stock_name) AS stock_name,
               SUM(mh.quantity) AS qty,
               COALESCE(lc.close_price, 0) AS price
        FROM manual_holdings mh
        LEFT JOIN lc ON lc.stock_code = mh.stock_code
        WHERE mh.user_id = %s AND mh.quantity > 0
        GROUP BY mh.stock_code, lc.close_price
    """, [uid])

    # 테마 매핑 (첫 번째 테마)
    theme_rows = query("SELECT stock_code, themes FROM stock_themes")
    theme_map  = {}
    for r in theme_rows:
        t = (r["themes"] or "").split(",")[0].strip()
        if t:
            theme_map[r["stock_code"]] = t

    # 최신 마켓파워 배분 점수
    mp_rows = query("""
        SELECT DISTINCT ON (stock_code)
            stock_code,
            COALESCE(composite_score, total_score, 0) AS alloc_score
        FROM market_power_scores
        WHERE user_id = %s
        ORDER BY stock_code, scored_at DESC
    """, [uid])
    mp_map = {r["stock_code"]: float(r["alloc_score"]) for r in mp_rows}

    # 종목별 현재 평가금액
    stocks   = []
    total_eval = 0
    for r in hold_rows:
        code     = r["stock_code"]
        qty      = int(r["qty"] or 0)
        price    = int(r["price"] or 0)
        eval_amt = qty * price
        total_eval += eval_amt
        theme = theme_map.get(code, "")
        stocks.append({
            "stock_code": code,
            "stock_name": r["stock_name"],
            "theme":      theme,
            "qty":        qty,
            "price":      price,
            "eval_amt":   eval_amt,
            "has_mp":     code in mp_map,
        })

    # 테마별 그룹 & eval 합계
    theme_stocks: dict[str, list] = defaultdict(list)
    theme_eval:   dict[str, float] = defaultdict(float)
    for s in stocks:
        t = s["theme"]
        if t:
            theme_stocks[t].append(s)
            theme_eval[t] += s["eval_amt"]

    result = []
    for theme, slist in theme_stocks.items():
        proposed_ratio = proposed_map.get(theme)
        if proposed_ratio is None:
            continue

        theme_target_eval = total_eval * proposed_ratio / 100

        # alloc_score: MP 있으면 점수, 없으면 현재 eval 비율(= 테마 내 현 비중 유지)
        th_eval = theme_eval[theme]
        for s in slist:
            s["alloc_score"] = mp_map[s["stock_code"]] if s["has_mp"] else (
                s["eval_amt"] / th_eval * 100 if th_eval > 0 else 1.0
            )

        total_alloc = sum(s["alloc_score"] for s in slist) or 1

        for s in slist:
            target_eval = round(theme_target_eval * s["alloc_score"] / total_alloc)
            diff_eval   = target_eval - s["eval_amt"]
            shares      = abs(diff_eval) // s["price"] if s["price"] > 0 else 0
            result.append({
                "stock_code":   s["stock_code"],
                "stock_name":   s["stock_name"],
                "theme":        theme,
                "qty":          s["qty"],
                "price":        s["price"],
                "current_eval": s["eval_amt"],
                "target_eval":  target_eval,
                "diff_eval":    diff_eval,
                "shares":       int(shares),
                "direction":    "매수" if diff_eval > 1000 else "매도" if diff_eval < -1000 else "유지",
                "has_mp":       s["has_mp"],
            })

    result.sort(key=lambda x: (x["theme"], -abs(x["diff_eval"])))
    return jsonify({"stocks": result, "total_eval": total_eval})


@app.route("/api/market_power/<int:sid>", methods=["DELETE"])
def api_market_power_delete(sid: int):
    """점수 레코드 삭제."""
    uid = _current_uid()
    with get_conn() as conn:
        conn.cursor().execute(
            "DELETE FROM market_power_scores WHERE id=%s AND user_id=%s", (sid, uid)
        )
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — 사용자 관리
# ---------------------------------------------------------------------------

ALL_MENUS = ["dashboard", "supply", "divergence", "snapshot", "signals", "report", "ext-holdings", "price-mgmt", "cash-assets", "macro", "rebalance", "credit", "stock-rebalance", "theme-rebalance", "qualitative", "market-power", "auditlog", "stocks", "batch", "spec", "common-codes", "usermgmt"]

@app.route("/api/users")
def api_users():
    """사용자 계정 목록 조회."""
    rows = query("SELECT id, name, login_id FROM users ORDER BY id")
    result = []
    for r in rows:
        prefs = _get_user_prefs(r["id"])
        result.append({
            "id": r["id"],
            "name": r["name"],
            "login_id": r.get("login_id") or "",
            "visible_menus":       json.loads(prefs.get("visible_menus", json.dumps(ALL_MENUS))),
            "supply_default_stock": prefs.get("supply_default_stock", ""),
            "supply_default_period": int(prefs.get("supply_default_period", "500")),
        })
    return jsonify(result)


@app.route("/api/users", methods=["POST"])
def api_create_user():
    """사용자 계정 추가."""
    data = request.get_json()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "이름을 입력해주세요"}), 400
    try:
        row = query_one("INSERT INTO users (name) VALUES (%s) RETURNING id, name", (name,))
    except Exception:
        return jsonify({"error": "이미 존재하는 이름입니다"}), 409
    return jsonify({"id": row["id"], "name": row["name"],
                    "visible_menus": ALL_MENUS,
                    "supply_default_stock": "", "supply_default_period": 500}), 201


@app.route("/api/users/<int:user_id>/credentials", methods=["PUT"])
def api_set_credentials(user_id: int):
    """로그인 ID·비밀번호 설정."""
    data = request.get_json() or {}
    login_id = (data.get("login_id") or "").strip()
    password = (data.get("password") or "").strip()
    if not login_id:
        return jsonify({"error": "로그인 아이디를 입력해주세요"}), 400
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            if password:
                cur.execute(
                    "UPDATE users SET login_id = %s, password_hash = %s WHERE id = %s",
                    (login_id, generate_password_hash(password), user_id),
                )
            else:
                cur.execute(
                    "UPDATE users SET login_id = %s WHERE id = %s",
                    (login_id, user_id),
                )
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"error": "이미 사용 중인 아이디입니다"}), 409
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
def api_delete_user(user_id: int):
    """사용자 계정 삭제."""
    cnt = query_one("SELECT COUNT(*) AS c FROM users")
    if (cnt or {}).get("c", 0) <= 1:
        return jsonify({"error": "마지막 사용자는 삭제할 수 없습니다"}), 400
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
    return jsonify({"ok": True})


@app.route("/api/users/<int:user_id>/preferences", methods=["PUT"])
def api_save_prefs(user_id: int):
    """사용자 메뉴 접근 권한 및 기본값 설정."""
    data = request.get_json()
    prefs = {
        "visible_menus":         json.dumps(data.get("visible_menus", ALL_MENUS)),
        "supply_default_stock":  data.get("supply_default_stock", ""),
        "supply_default_period": str(data.get("supply_default_period", 500)),
    }
    with get_conn() as conn:
        cur = conn.cursor()
        for key, value in prefs.items():
            cur.execute("""
                INSERT INTO user_preferences (user_id, key, value) VALUES (%s, %s, %s)
                ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value
            """, (user_id, key, value))
    return jsonify({"ok": True})


def _get_user_prefs(user_id: int) -> dict:
    rows = query("SELECT key, value FROM user_preferences WHERE user_id = %s", (user_id,))
    return {r["key"]: r["value"] for r in rows}


# ---------------------------------------------------------------------------
# API — 배치 관리
# ---------------------------------------------------------------------------

# 앱 내 실행 추적: job_id → 실제 Python 프로세스 PID
_batch_pids: dict[str, int] = {}
# 수동 중지된 job_id — 스케줄러가 자동 재실행하지 않도록 방지
_batch_manual_stopped: set[str] = set()


def _batch_running_pid(job_id: str) -> int | None:
    """실행 중인 배치 PID 반환. 없으면 None.
    인메모리 PID를 먼저 확인 후 /proc 스캔으로 폴백."""
    pid = _batch_pids.get(job_id)
    if pid:
        try:
            os.kill(pid, 0)   # 프로세스 존재 확인 (신호 0 = 체크용)
            return pid
        except (ProcessLookupError, PermissionError):
            _batch_pids.pop(job_id, None)   # 종료됐으면 제거
    # 앱 재시작 후 폴백: 패턴 기반 /proc 스캔
    j = BATCH_JOBS.get(job_id)
    if j:
        found = _find_pid(j["match"])
        if found:
            _batch_pids[job_id] = found
            return found
    return None


def _batch_launch(job_id: str, log_file: str, min_cap: str) -> int:
    """배치 프로세스를 새 세션으로 시작하고 PID를 반환."""
    j = BATCH_JOBS[job_id]
    cmd_parts = shlex.split(j["cmd"])
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "MIN_MARKET_CAP": min_cap}
    _append_run_separator(log_file)
    with open(log_file, "ab") as lf:
        proc = subprocess.Popen(
            cmd_parts, env=env, cwd=BASE_DIR,
            stdout=lf, stderr=subprocess.STDOUT,
            start_new_session=True,   # 독립 프로세스 그룹 → killpg로 확실히 종료
        )
    _batch_pids[job_id] = proc.pid
    _batch_manual_stopped.discard(job_id)   # 수동 중지 플래그 해제
    # 데몬 스레드에서 wait() 호출: zombie 수거 + 완료 시 _batch_pids 자동 정리
    def _reap(p=proc, jid=job_id):
        p.wait()
        if _batch_pids.get(jid) == p.pid:
            _batch_pids.pop(jid, None)
    threading.Thread(target=_reap, daemon=True).start()
    return proc.pid


def _batch_kill(job_id: str) -> bool:
    """배치 프로세스 그룹 전체에 SIGTERM. 성공 여부 반환."""
    pid = _batch_running_pid(job_id)
    if not pid:
        return False
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    _batch_pids.pop(job_id, None)
    return True


@app.route("/api/batch")
def api_batch():
    """배치 작업 실행 상태 목록 조회.
    인메모리 PID 딕셔너리 + os.kill(0) 체크만 사용 — subprocess/파일I/O 없음."""
    jobs = []
    for jid, j in BATCH_JOBS.items():
        pid = _batch_pids.get(jid)
        if pid:
            try:
                os.kill(pid, 0)   # syscall만 — subprocess/파일 I/O 없음
            except (ProcessLookupError, PermissionError):
                _batch_pids.pop(jid, None)
                pid = None
        log_basename = f"{j['log_prefix']}.log"
        log_path = os.path.join(BASE_DIR, "logs", log_basename)
        jobs.append({
            "id": jid,
            "name": j["name"],
            "desc": j["desc"],
            "running": pid is not None,
            "pid": pid,
            "manual_stopped": jid in _batch_manual_stopped,
            "log_file": log_basename if os.path.exists(log_path) else None,
            "last_line": "",
        })
    return jsonify(jobs)


@app.route("/api/batch/<job_id>/start", methods=["POST"])
def api_batch_start(job_id: str):
    """배치 작업 수동 실행."""
    j = BATCH_JOBS.get(job_id)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    if _batch_running_pid(job_id):
        return jsonify({"error": "이미 실행 중입니다"}), 409
    settings = _get_app_settings()
    min_cap = settings.get("min_market_cap", str(5_000_000_000_000))
    logs_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, f"{j['log_prefix']}.log")
    pid = _batch_launch(job_id, log_file, min_cap)
    logging.info("[batch] %s 수동 시작 (PID %d)", job_id, pid)
    return jsonify({"ok": True, "log_file": os.path.basename(log_file)})


@app.route("/api/batch/<job_id>/stop", methods=["POST"])
def api_batch_stop(job_id: str):
    """배치 작업 수동 중지. 스케줄러 자동 재실행도 다음 수동 시작 전까지 억제."""
    if not BATCH_JOBS.get(job_id):
        return jsonify({"error": "unknown job"}), 404
    if not _batch_running_pid(job_id):
        return jsonify({"error": "실행 중이 아닙니다"}), 409
    _batch_kill(job_id)
    _batch_manual_stopped.add(job_id)   # 스케줄러 재실행 방지
    logging.info("[batch] %s 수동 중지 — 스케줄러 자동 재실행 억제", job_id)
    return jsonify({"ok": True})


_USER_PREF_KEYS = {
    "cash_target_ratio",
    "rebalance_alert_up",
    "rebalance_alert_down",
    "rebalance_watch_up",
    "rebalance_watch_down",
}


def _get_app_settings() -> dict:
    try:
        rows = query("SELECT key, value FROM app_settings")
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}


def _get_user_settings(uid: int) -> dict:
    """app_settings + user_preferences(uid) 병합. 사용자 설정이 전역 설정보다 우선."""
    settings = _get_app_settings()
    if uid:
        try:
            rows = query(
                "SELECT key, value FROM user_preferences WHERE user_id = %s AND key = ANY(%s)",
                (uid, list(_USER_PREF_KEYS)),
            )
            for r in rows:
                settings[r["key"]] = r["value"]
        except Exception:
            pass
    return settings


@app.route("/api/settings")
def api_settings():
    """앱 설정값 조회."""
    return jsonify(_get_user_settings(_current_uid()))


@app.route("/api/settings", methods=["PUT"])
def api_save_settings():
    """앱 설정값 수정."""
    uid = _current_uid()
    data = request.get_json() or {}
    with get_conn() as conn:
        cur = conn.cursor()
        for key, value in data.items():
            if key in _USER_PREF_KEYS and uid:
                cur.execute("""
                    INSERT INTO user_preferences (user_id, key, value)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value
                """, (uid, key, str(value)))
            else:
                cur.execute("""
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """, (key, str(value)))
    return jsonify({"ok": True})


@app.route("/api/schedule")
def api_schedule_get():
    """배치 스케줄 설정 전체 조회."""
    rows = query("SELECT job_id, enabled, hour, minute, days, interval_mode, interval_minutes, interval_start, interval_end, last_run AT TIME ZONE 'Asia/Seoul' AS last_run FROM batch_schedules")
    result = {r["job_id"]: dict(r) for r in rows}
    for jid in BATCH_JOBS:
        if jid not in result:
            result[jid] = {"job_id": jid, "enabled": False, "hour": 9, "minute": 0, "days": "weekdays",
                           "interval_mode": False, "interval_minutes": 60,
                           "interval_start": 0, "interval_end": 1439, "last_run": None}
    return jsonify(result)


@app.route("/api/schedule/<job_id>", methods=["PUT"])
def api_schedule_save(job_id: str):
    """배치 스케줄 설정 수정 (cron 또는 인터벌 방식)."""
    if job_id not in BATCH_JOBS:
        return jsonify({"error": "unknown job"}), 404
    data = request.get_json() or {}
    enabled          = bool(data.get("enabled", False))
    hour             = int(data.get("hour", 9))
    minute           = int(data.get("minute", 0))
    days             = data.get("days", "weekdays")
    interval_mode    = bool(data.get("interval_mode", False))
    interval_minutes = max(1, int(data.get("interval_minutes", 60)))
    interval_start   = max(0, min(1439, int(data.get("interval_start", 0))))
    interval_end     = max(0, min(1439, int(data.get("interval_end", 1439))))
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO batch_schedules (job_id, enabled, hour, minute, days, interval_mode, interval_minutes, interval_start, interval_end, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (job_id) DO UPDATE
              SET enabled          = EXCLUDED.enabled,
                  hour             = EXCLUDED.hour,
                  minute           = EXCLUDED.minute,
                  days             = EXCLUDED.days,
                  interval_mode    = EXCLUDED.interval_mode,
                  interval_minutes = EXCLUDED.interval_minutes,
                  interval_start   = EXCLUDED.interval_start,
                  interval_end     = EXCLUDED.interval_end,
                  updated_at       = NOW()
        """, (job_id, enabled, hour, minute, days, interval_mode, interval_minutes, interval_start, interval_end))
    _reload_scheduler_job(job_id, enabled, hour, minute, days, interval_mode, interval_minutes, interval_start, interval_end)
    return jsonify({"ok": True})


@app.route("/api/batch/<job_id>/logs")
def api_batch_logs(job_id: str):
    """배치 작업 로그 조회 — ?from=N 지정 시 N줄 이후 증분만 반환, 미지정 시 최근 500줄."""
    j = BATCH_JOBS.get(job_id)
    if not j:
        return jsonify({"lines": [], "total": 0})
    log_path = _latest_log(j["log_prefix"])
    if not log_path:
        return jsonify({"lines": [], "total": 0})
    try:
        from_line = request.args.get("from", type=int, default=None)
        with open(log_path, "r", errors="replace") as f:
            all_lines = [ln.rstrip() for ln in f.readlines()]
        total = len(all_lines)
        if from_line is None:
            # 첫 로드: 최근 500줄
            start = max(0, total - 500)
            return jsonify({"lines": all_lines[start:], "total": total})
        if from_line > total:
            if total == 0:
                # 파일 일시적 비어있음 — rotated 아님, 클라이언트 상태 유지
                return jsonify({"lines": [], "total": 0})
            # 파일 교체됨 (새 실행 시작) — 처음부터 반환
            return jsonify({"lines": all_lines, "total": total, "rotated": True})
        # 증분: from_line 이후 새 줄만 반환 (변화 없으면 lines=[] 반환)
        return jsonify({"lines": all_lines[from_line:], "total": total})
    except Exception:
        return jsonify({"lines": [], "total": 0})


@app.route("/batch/<job_id>/log-viewer")
def batch_log_viewer(job_id: str):
    j = BATCH_JOBS.get(job_id)
    if not j:
        return "Job not found", 404
    name = request.args.get("name", j["name"])
    return render_template("log_viewer.html", job_id=job_id, name=name)


# ---------------------------------------------------------------------------
# 스케줄러
# ---------------------------------------------------------------------------

_DAYS_MAP = {
    "daily":    "mon,tue,wed,thu,fri,sat,sun",
    "weekdays": "mon,tue,wed,thu,fri",
    "weekends": "sat,sun",
}

_scheduler = BackgroundScheduler(timezone="Asia/Seoul")


def _run_scheduled_job(job_id: str, interval_start: int = 0, interval_end: int = 1439):
    j = BATCH_JOBS.get(job_id)
    if not j:
        return
    # 수동 중지 후 스케줄러 자동 재실행 억제
    if job_id in _batch_manual_stopped:
        logging.info("[scheduler] %s 수동 중지 상태 — 자동 실행 억제 (수동 시작 시 해제)", job_id)
        return
    # 반복 주기 모드의 시간 범위 체크 (interval_start/end: 자정 기준 분)
    if interval_start != 0 or interval_end != 1439:
        now = datetime.now(tz=KST)
        cur_min = now.hour * 60 + now.minute
        if not (interval_start <= cur_min <= interval_end):
            logging.info("[scheduler] %s 시간 범위 밖 — 스킵 (%02d:%02d, 허용 %02d:%02d~%02d:%02d)",
                         job_id, now.hour, now.minute,
                         interval_start // 60, interval_start % 60,
                         interval_end   // 60, interval_end   % 60)
            return
    if _batch_running_pid(job_id):
        logging.info("[scheduler] %s 이미 실행 중 — 스킵", job_id)
        return
    settings = _get_app_settings()
    min_cap = settings.get("min_market_cap", str(5_000_000_000_000))
    logs_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, f"{j['log_prefix']}.log")
    pid = _batch_launch(job_id, log_file, min_cap)
    logging.info("[scheduler] %s 자동 실행 시작 (PID %d) → %s", job_id, pid, log_file)
    try:
        with get_conn() as conn:
            conn.cursor().execute(
                "UPDATE batch_schedules SET last_run = NOW() WHERE job_id = %s", (job_id,)
            )
    except Exception:
        pass


def _reload_scheduler_job(job_id: str, enabled: bool, hour: int, minute: int, days: str,
                          interval_mode: bool = False, interval_minutes: int = 60,
                          interval_start: int = 0, interval_end: int = 1439):
    sched_id = f"batch_{job_id}"
    if _scheduler.get_job(sched_id):
        _scheduler.remove_job(sched_id)
    if not enabled:
        return
    if interval_mode:
        trigger = IntervalTrigger(minutes=interval_minutes, timezone="Asia/Seoul")
        range_str = f" ({interval_start//60:02d}:{interval_start%60:02d}~{interval_end//60:02d}:{interval_end%60:02d})" \
                    if (interval_start != 0 or interval_end != 1439) else " (24시간)"
        logging.info("[scheduler] %s 등록: %d분마다 반복%s", job_id, interval_minutes, range_str)
    else:
        day_str = _DAYS_MAP.get(days, "mon,tue,wed,thu,fri")
        trigger = CronTrigger(day_of_week=day_str, hour=hour, minute=minute, timezone="Asia/Seoul")
        logging.info("[scheduler] %s 등록: %02d:%02d [%s]", job_id, hour, minute, days)
    _scheduler.add_job(
        _run_scheduled_job,
        trigger,
        id=sched_id,
        args=[job_id, interval_start, interval_end],
        replace_existing=True,
        misfire_grace_time=None,   # 지연된 fire도 취소하지 않음
        coalesce=True,             # 밀린 여러 fire는 1회로 합산
    )


def _init_scheduler():
    try:
        rows = query("SELECT job_id, enabled, hour, minute, days, interval_mode, interval_minutes, interval_start, interval_end FROM batch_schedules")
        for r in rows:
            if r["enabled"]:
                _reload_scheduler_job(
                    r["job_id"], True, r["hour"], r["minute"], r["days"],
                    bool(r.get("interval_mode", False)), int(r.get("interval_minutes", 60)),
                    int(r.get("interval_start", 0)), int(r.get("interval_end", 1439)),
                )
    except Exception as e:
        logging.warning("[scheduler] 초기화 실패: %s", e)
    _scheduler.start()


# ---------------------------------------------------------------------------
# 기획서 (Spec) — DB 저장, SPEC.md → DB 자동 동기화
# ---------------------------------------------------------------------------

_SPEC_FILE = os.path.join(BASE_DIR, "SPEC.md")
_SCREEN_SPEC_FILE = os.path.join(BASE_DIR, "SCREEN_SPEC.md")


def _ensure_screen_spec_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS screen_spec_document (
                id         INTEGER PRIMARY KEY DEFAULT 1,
                content    TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT NOW(),
                CHECK (id = 1)
            )
        """)


def _sync_screen_spec_to_db():
    """SCREEN_SPEC.md가 있으면 DB에 동기화 (앱 시작 시 호출)."""
    if not os.path.exists(_SCREEN_SPEC_FILE):
        return
    try:
        with open(_SCREEN_SPEC_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return
        with get_conn() as conn:
            conn.cursor().execute("""
                INSERT INTO screen_spec_document (id, content, updated_at)
                VALUES (1, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content, updated_at = NOW()
            """, (content,))
    except Exception as e:
        logging.warning("[spec] SCREEN_SPEC.md → DB 동기화 실패: %s", e)


@app.route("/api/spec")
def api_spec():
    rows = query("SELECT content, updated_at FROM spec_document WHERE id = 1")
    if rows and rows[0]["content"]:
        r = rows[0]
        return jsonify({"content": r["content"], "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None})
    return jsonify({"content": "", "updated_at": None})


@app.route("/api/spec/screens")
def api_spec_screens():
    rows = query("SELECT content, updated_at FROM screen_spec_document WHERE id = 1")
    if rows and rows[0]["content"]:
        r = rows[0]
        return jsonify({"content": r["content"], "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None})
    return jsonify({"content": "", "updated_at": None})


@app.route("/api/spec/apis")
def api_spec_apis():
    """Flask url_map 기반 API 엔드포인트 목록 자동 생성 (/api/ 경로만 포함)."""
    import re as _re
    _PARAM_RE = _re.compile(r"<(?:(?:int|float|string|path|uuid):)?([^>]+)>")

    # path → {methods, doc, path_params} 로 누적 (같은 path, 다른 method 병합)
    by_path = {}
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        path = rule.rule
        if not path.startswith("/api/"):
            continue
        methods = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
        if not methods:
            continue
        fn = app.view_functions.get(rule.endpoint)
        doc = ""
        if fn and fn.__doc__:
            doc = fn.__doc__.strip().split("\n")[0]
        # path parameters
        path_params = _PARAM_RE.findall(path)
        # display path: <int:id> → {id}
        display_path = _PARAM_RE.sub(lambda m: "{" + m.group(1) + "}", path)
        if display_path not in by_path:
            by_path[display_path] = {"path": display_path, "methods": [], "doc": doc, "path_params": path_params}
        for m in methods:
            if m not in by_path[display_path]["methods"]:
                by_path[display_path]["methods"].append(m)
        if not by_path[display_path]["doc"] and doc:
            by_path[display_path]["doc"] = doc

    results = sorted(by_path.values(), key=lambda r: r["path"])
    return jsonify(results)


# ---------------------------------------------------------------------------
# 차트 데이터 (키움 REST API 직접 호출 + 시장구조 분석)
# ---------------------------------------------------------------------------

_chart_market_agent = None
_chart_agent_lock = threading.Lock()

def _get_chart_agent():
    global _chart_market_agent
    with _chart_agent_lock:
        if _chart_market_agent is None:
            from agents.market_data import MarketDataAgent
            _chart_market_agent = MarketDataAgent()
    return _chart_market_agent


@app.route("/api/chart_data/<ticker>")
def api_chart_data(ticker: str):
    """종목 OHLCV + 시장구조 분석 결과 반환 (TradingView Lightweight Charts 형식)."""
    timeframe = request.args.get("timeframe", "D")
    count     = min(int(request.args.get("count", "150")), 300)

    try:
        agent = _get_chart_agent()
        if timeframe == "D":
            df = agent.get_daily_ohlcv(ticker, count)
        else:
            df = agent.get_minute_ohlcv(ticker, timeframe, count)

        if df.empty:
            return jsonify({"error": "데이터 없음"}), 404

        from agents.chart_analysis import _analyze_market_structure
        vol_ma20 = float(df["volume"].rolling(20, min_periods=1).mean().iloc[-1])
        ms = _analyze_market_structure(df, vol_ma20)

        if ms is None:
            return jsonify({"error": f"데이터 부족 (최소 {30}봉 필요)"}), 422

        is_daily = (timeframe == "D")
        def _to_time(d):
            if is_daily:
                return d.strftime("%Y-%m-%d")
            kst = d if d.tzinfo else d.replace(tzinfo=KST)
            return int(kst.timestamp())

        candles = [{
            "time":   _to_time(row["date"]),
            "open":   int(row["open"]),
            "high":   int(row["high"]),
            "low":    int(row["low"]),
            "close":  int(row["close"]),
            "volume": int(row["volume"]),
        } for _, row in df.iterrows()]

        times = [c["time"] for c in candles]
        def idx2t(i):
            return times[max(0, min(int(i), len(times) - 1))]

        return jsonify({
            "ticker":    ticker,
            "timeframe": timeframe,
            "candles":   candles,
            "swing_points": [{
                "time":       idx2t(sp.index),
                "price":      sp.price,
                "swing_type": sp.swing_type.value,
                "is_high":    sp.is_high,
            } for sp in ms.swing_points],
            "structure_breaks": [{
                "time":               idx2t(sb.bar_index),
                "break_type":         sb.break_type.value,
                "direction":          sb.direction,
                "price":              sb.price,
                "broken_swing_price": sb.broken_swing_price,
                "volume_confirmed":   sb.volume_confirmed,
            } for sb in ms.structure_breaks],
            "liquidity_pools": [{
                "price":       lp.price,
                "touch_count": lp.touch_count,
                "is_high":     lp.is_high,
            } for lp in ms.liquidity_pools],
            "liquidity_sweeps": [{
                "time":          idx2t(ls.bar_index),
                "pool_price":    ls.pool_price,
                "is_high":       ls.is_high,
                "direction":     ls.direction,
                "close_reverted": ls.close_reverted,
            } for ls in ms.liquidity_sweeps],
            "market_state":  ms.market_state.value,
            "effort_result": ms.effort_result.value,
        })

    except Exception as e:
        app.logger.error("chart_data error [%s]: %s", ticker, e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------

try:
    _ensure_app_settings_table()
except Exception:
    pass

try:
    _ensure_batch_schedules_table()
except Exception:
    pass

try:
    _ensure_users_auth_columns()
except Exception:
    pass

try:
    _ensure_user_preferences_table()
except Exception:
    pass

try:
    _ensure_report_tables()
except Exception:
    pass

try:
    _ensure_manual_holdings_table()
except Exception:
    pass

try:
    _ensure_trade_history_table()
except Exception:
    pass

try:
    _run_migration_step(lambda cur: cur.execute(
        "ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'approved'"
    ))
except Exception:
    pass

try:
    _ensure_common_codes_table()
except Exception:
    pass

try:
    _ensure_rebalance_targets_table()
except Exception:
    pass

try:
    _ensure_theme_tables()
except Exception:
    pass

try:
    _ensure_qualitative_tables()
except Exception:
    pass

try:
    _ensure_market_power_table()
except Exception:
    pass

try:
    _ensure_macro_rates_table()
except Exception:
    pass

try:
    _ensure_cash_assets_table()
except Exception:
    pass

try:
    _ensure_credit_positions_table()
except Exception:
    pass

try:
    _ensure_user_id_migration()
except Exception:
    pass

try:
    with get_conn() as conn:
        conn.cursor().execute(
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS reasons JSONB"
        )
except Exception:
    pass

try:
    _ensure_spec_table()
    _sync_spec_to_db()
except Exception as e:
    logging.warning("[spec] 초기화 실패: %s", e)

try:
    _ensure_screen_spec_table()
    _sync_screen_spec_to_db()
except Exception as e:
    logging.warning("[spec] screen 초기화 실패: %s", e)

try:
    _init_scheduler()
except Exception as e:
    logging.warning("스케줄러 시작 실패: %s", e)

# 앱 시작 시 1회 PID 스캔 — 이미 실행 중인 배치 인식
try:
    ps_lines: list[str] | None = None
    if not os.path.exists("/proc"):
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=3)
        ps_lines = r.stdout.splitlines()[1:]
    for _jid, _j in BATCH_JOBS.items():
        if _jid not in _batch_pids:
            _found = _find_pid(_j["match"], ps_lines)
            if _found:
                _batch_pids[_jid] = _found
                logging.info("[startup] 실행 중인 배치 감지: %s (PID %d)", _jid, _found)
except Exception as e:
    logging.warning("[startup] 배치 PID 스캔 실패: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True, use_reloader=False)
