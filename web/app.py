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
import logging
import secrets
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, jsonify, render_template, request, session, redirect
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import config

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "kiwoom-analyzer-secret-change-in-prod")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


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
                job_id     VARCHAR(50) PRIMARY KEY,
                enabled    BOOLEAN NOT NULL DEFAULT FALSE,
                hour       SMALLINT NOT NULL DEFAULT 9,
                minute     SMALLINT NOT NULL DEFAULT 0,
                days       VARCHAR(20) NOT NULL DEFAULT 'weekdays',
                last_run   TIMESTAMP,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)


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
        cur.execute("ALTER TABLE cash_assets ADD COLUMN IF NOT EXISTS purchase_price BIGINT DEFAULT NULL")


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


def _get_total_cash() -> int:
    """현금성 자산 합계. cash_assets 테이블 우선, 없으면 legacy portfolio_cash_* fallback."""
    rows = query("SELECT COALESCE(SUM(amount), 0) AS total FROM cash_assets")
    ca_total = int(rows[0]["total"]) if rows else 0
    if ca_total > 0:
        return ca_total
    settings = _get_app_settings()
    return sum(int(v or 0) for k, v in settings.items() if k.startswith("portfolio_cash_"))


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
}


def _find_pid(match: str) -> int | None:
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
    # macOS 폴백
    try:
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True)
        for line in r.stdout.splitlines()[1:]:
            if pattern.search(line):
                parts = line.split()
                if len(parts) > 1:
                    return int(parts[1])
    except Exception:
        pass
    return None


def _latest_log(prefix: str) -> str | None:
    files = sorted(glob.glob(os.path.join(BASE_DIR, "logs", f"{prefix}_*.log")))
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# DB 헬퍼
# ---------------------------------------------------------------------------

def get_conn():
    return psycopg2.connect(
        config.database_url,
        cursor_factory=psycopg2.extras.RealDictCursor,
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
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
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
        (datetime.now() - timedelta(days=30),),
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
# API — 감사 로그
# ---------------------------------------------------------------------------

@app.route("/api/events")
def api_events():
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
    """이메일 발송 설정 조회."""
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
    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE common_codes SET active = NOT active WHERE id = %s", (cid,)
        )
    return jsonify({"ok": True})


@app.route("/api/common_codes/<int:cid>", methods=["DELETE"])
def api_common_codes_delete(cid: int):
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM common_codes WHERE id = %s", (cid,))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — 타사 보유종목 (manual_holdings)
# ---------------------------------------------------------------------------

@app.route("/api/manual_holdings")
def api_manual_holdings_list():
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
        ORDER BY mh.brokerage, mh.stock_name, mh.stock_code
    """)
    for r in rows:
        r["avg_price"]     = float(r["avg_price"])     if r["avg_price"]     is not None else 0.0
        r["current_price"] = int(r["current_price"])   if r["current_price"] is not None else None
        r["price_date"]    = r["price_date"].strftime("%Y-%m-%d") if r["price_date"] else None
        r["created_at"]    = r["created_at"].strftime("%Y-%m-%d") if r["created_at"] else ""
    return jsonify(rows)


@app.route("/api/manual_holdings", methods=["POST"])
def api_manual_holdings_create():
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
        INSERT INTO manual_holdings (brokerage, stock_code, stock_name, quantity, avg_price, memo)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (brokerage, stock_code, stock_name, quantity, avg_price, memo))
    return jsonify({"ok": True, "id": row["id"]}), 201


@app.route("/api/manual_holdings/<int:hid>", methods=["PUT"])
def api_manual_holdings_update(hid: int):
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
            WHERE id = %s
        """, (brokerage, stock_code, stock_name, quantity, avg_price, memo, hid))
    return jsonify({"ok": True})


@app.route("/api/manual_holdings/<int:hid>", methods=["DELETE"])
def api_manual_holdings_delete(hid: int):
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM manual_holdings WHERE id = %s", (hid,))
    return jsonify({"ok": True})


@app.route("/api/price_sync/stocks")
def api_price_sync_stocks():
    """타사 보유종목 현재가 현황 (현재가 관리 화면용)."""
    rows = query("""
        WITH holdings AS (
            SELECT stock_code, MAX(stock_name) AS stock_name
            FROM manual_holdings
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
    """)
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
            COALESCE(rt.target_ratio, 0) AS target_ratio
        FROM holdings_agg ha
        LEFT JOIN latest_close lc ON lc.stock_code = ha.stock_code
        LEFT JOIN stocks st ON st.stock_code = ha.stock_code
        LEFT JOIN rebalance_targets rt ON rt.stock_code = ha.stock_code
        ORDER BY ha.stock_name
    """)

    settings          = _get_app_settings()
    total_cash        = _get_total_cash()
    alert_up          = float(settings.get("rebalance_alert_up",   30))
    alert_down        = float(settings.get("rebalance_alert_down", 25))
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
        "cash_target_ratio": cash_target_ratio,
    })


@app.route("/api/rebalance/target", methods=["PUT"])
def api_rebalance_target():
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
            INSERT INTO rebalance_targets (stock_code, target_ratio, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (stock_code)
            DO UPDATE SET target_ratio = EXCLUDED.target_ratio, updated_at = NOW()
        """, (stock_code, target_ratio))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — 신용 포지션 충당금 관리
# ---------------------------------------------------------------------------

@app.route("/api/credit_positions")
def api_credit_positions_list():
    rows = query("""
        SELECT id, brokerage, purchase_amount, loan_amount, note,
               TO_CHAR(updated_at, 'YYYY-MM-DD HH24:MI') AS updated_at
        FROM credit_positions ORDER BY brokerage
    """)
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
        GROUP BY mh.brokerage
        ORDER BY mh.brokerage
    """)
    broker_stock_eval = {(r["brokerage"] or ""): round(float(r["stock_eval"] or 0)) for r in broker_rows}

    cash_rows = query("""
        SELECT brokerage, SUM(amount) AS cash_eval
        FROM cash_assets
        WHERE brokerage != ''
        GROUP BY brokerage
    """)
    broker_cash_eval = {(r["brokerage"] or ""): round(float(r["cash_eval"] or 0)) for r in cash_rows}

    return jsonify({"positions": positions, "broker_stock_eval": broker_stock_eval, "broker_cash_eval": broker_cash_eval})


@app.route("/api/credit_positions", methods=["POST"])
def api_credit_positions_upsert():
    """증권사당 1건 — 증권사 기준 UPSERT."""
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
            INSERT INTO credit_positions (brokerage, purchase_amount, loan_amount, note, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (brokerage)
            DO UPDATE SET purchase_amount = EXCLUDED.purchase_amount,
                          loan_amount     = EXCLUDED.loan_amount,
                          note            = EXCLUDED.note,
                          updated_at      = NOW()
        """, (brokerage, purchase, loan, note))
    return jsonify({"ok": True})


@app.route("/api/credit_positions/<int:pid>", methods=["PUT"])
def api_credit_positions_update(pid: int):
    data = request.get_json() or {}
    try:
        purchase = int(str(data.get("purchase_amount") or 0).replace(",", ""))
        loan     = int(str(data.get("loan_amount")     or 0).replace(",", ""))
    except (ValueError, TypeError):
        return jsonify({"error": "금액 오류"}), 400
    note = (data.get("note") or "").strip()
    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE credit_positions SET purchase_amount=%s, loan_amount=%s, note=%s, updated_at=NOW() WHERE id=%s",
            (purchase, loan, note, pid),
        )
    return jsonify({"ok": True})


@app.route("/api/credit_positions/<int:pid>", methods=["DELETE"])
def api_credit_positions_delete(pid: int):
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM credit_positions WHERE id=%s", (pid,))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — 테마 리밸런싱
# ---------------------------------------------------------------------------

@app.route("/api/theme_rebalance")
def api_theme_rebalance():
    """테마별 포트폴리오 비중 분석."""
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
    """)

    settings   = _get_app_settings()
    total_cash = _get_total_cash()
    alert_up   = float(settings.get("rebalance_alert_up",   30))
    alert_down = float(settings.get("rebalance_alert_down", 25))

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
        s["current_ratio"] = round(s["eval_amt"] / portfolio_total * 100, 2) if portfolio_total > 0 else 0

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

    target_rows = query("SELECT theme, target_ratio, alert_up, alert_down FROM theme_targets")
    targets = {r["theme"]: {
        "target_ratio": float(r["target_ratio"]),
        "alert_up":   float(r["alert_up"])   if r["alert_up"]   is not None else None,
        "alert_down": float(r["alert_down"]) if r["alert_down"] is not None else None,
    } for r in target_rows}

    theme_result = []
    for tname, data in sorted(theme_data.items(), key=lambda x: -x[1]["eval_amt"]):
        eval_amt     = data["eval_amt"]
        cur_ratio    = round(eval_amt / portfolio_total * 100, 2) if portfolio_total > 0 else 0
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
        "themes":          theme_result,
        "stocks":          stocks,
        "portfolio_total": round(portfolio_total),
        "total_cash":      total_cash,
        "alert_up":        alert_up,
        "alert_down":      alert_down,
    })


@app.route("/api/theme_rebalance/stock_themes", methods=["PUT"])
def api_theme_rebalance_stock_themes():
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
            INSERT INTO theme_targets (theme, target_ratio, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (theme) DO UPDATE
            SET target_ratio = EXCLUDED.target_ratio, updated_at = NOW()
        """, (theme, target_ratio))
    return jsonify({"ok": True})


@app.route("/api/theme_rebalance/theme_alert", methods=["PUT"])
def api_theme_rebalance_theme_alert():
    """테마별 과다/부족 기준 개별 설정 (NULL = 전역 기준 사용)."""
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
            INSERT INTO theme_targets (theme, target_ratio, alert_up, alert_down, updated_at)
            VALUES (%s, 0, %s, %s, NOW())
            ON CONFLICT (theme) DO UPDATE
            SET alert_up = EXCLUDED.alert_up, alert_down = EXCLUDED.alert_down, updated_at = NOW()
        """, (theme, alert_up, alert_down))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — 매크로 지표 관리
# ---------------------------------------------------------------------------

@app.route("/api/macro_rates")
def api_macro_rates_list():
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
    """Request body → (name, brokerage, qty, up, purchase_price, amount, link_type, link_key, note)."""
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
    link_type = (data.get("link_type") or "none").strip()
    link_key  = (data.get("link_key")  or "").strip()
    note = (data.get("note") or "").strip()
    return name, brokerage, qty_val, up_val, pp_val, amount, link_type, link_key, note


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
    rows = query("""
        SELECT id, name, brokerage, quantity, unit_price, purchase_price, amount, link_type, link_key, note,
               TO_CHAR(updated_at, 'YYYY-MM-DD HH24:MI') AS updated_at
        FROM cash_assets ORDER BY brokerage, id
    """)
    items = []
    total = 0
    for r in rows:
        amt = int(r["amount"])
        total += amt
        items.append({
            "id":             r["id"],
            "name":           r["name"],
            "brokerage":      r["brokerage"] or "",
            "quantity":       float(r["quantity"])       if r["quantity"]       is not None else None,
            "unit_price":     int(r["unit_price"])       if r["unit_price"]     is not None else None,
            "purchase_price": int(r["purchase_price"])   if r["purchase_price"] is not None else None,
            "amount":         amt,
            "link_type":      r["link_type"] or "none",
            "link_key":       r["link_key"]  or "",
            "note":           r["note"] or "",
            "updated_at":     r["updated_at"],
        })
    return jsonify({"items": items, "total": total})


@app.route("/api/cash_assets", methods=["POST"])
def api_cash_assets_create():
    try:
        name, brokerage, qty, up, pp, amount, lt, lk, note = _parse_cash_asset_body(request.get_json() or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    with get_conn() as conn:
        conn.cursor().execute(
            "INSERT INTO cash_assets (name, brokerage, quantity, unit_price, purchase_price, amount, link_type, link_key, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (name, brokerage, qty, up, pp, amount, lt, lk, note),
        )
    return jsonify({"ok": True})


@app.route("/api/cash_assets/<int:aid>", methods=["PUT"])
def api_cash_assets_update(aid):
    try:
        name, brokerage, qty, up, pp, amount, lt, lk, note = _parse_cash_asset_body(request.get_json() or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE cash_assets SET name=%s, brokerage=%s, quantity=%s, unit_price=%s, purchase_price=%s, amount=%s, link_type=%s, link_key=%s, note=%s, updated_at=NOW() WHERE id=%s",
            (name, brokerage, qty, up, pp, amount, lt, lk, note, aid),
        )
    return jsonify({"ok": True})


@app.route("/api/cash_assets/<int:aid>", methods=["DELETE"])
def api_cash_assets_delete(aid):
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM cash_assets WHERE id=%s", (aid,))
    return jsonify({"ok": True})


@app.route("/api/cash_assets/<int:aid>/sync", methods=["POST"])
def api_cash_assets_sync(aid):
    rows = query("SELECT quantity, link_type, link_key FROM cash_assets WHERE id=%s", (aid,))
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
    rows = query("SELECT id, quantity, link_type, link_key FROM cash_assets WHERE link_type != 'none' AND link_key != ''")
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
    with get_conn() as conn:
        conn.cursor().execute(
            "UPDATE qualitative_items SET active=FALSE WHERE id=%s", (item_id,)
        )
    return jsonify({"ok": True})


@app.route("/api/qualitative/items/<int:item_id>/scores")
def api_qualitative_item_scores(item_id: int):
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
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM qualitative_scores WHERE id=%s", (score_id,))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — 사용자 관리
# ---------------------------------------------------------------------------

ALL_MENUS = ["dashboard", "supply", "divergence", "signals", "report", "ext-holdings", "price-mgmt", "cash-assets", "macro", "rebalance", "credit", "stock-rebalance", "theme-rebalance", "qualitative", "auditlog", "stocks", "batch", "common-codes", "usermgmt"]

@app.route("/api/users")
def api_users():
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
    cnt = query_one("SELECT COUNT(*) AS c FROM users")
    if (cnt or {}).get("c", 0) <= 1:
        return jsonify({"error": "마지막 사용자는 삭제할 수 없습니다"}), 400
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
    return jsonify({"ok": True})


@app.route("/api/users/<int:user_id>/preferences", methods=["PUT"])
def api_save_prefs(user_id: int):
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

@app.route("/api/batch")
def api_batch():
    jobs = []
    for jid, j in BATCH_JOBS.items():
        pid = _find_pid(j["match"])
        log_path = _latest_log(j["log_prefix"])
        last_line = ""
        if log_path:
            try:
                size = os.path.getsize(log_path)
                with open(log_path, "rb") as f:
                    f.seek(-min(2000, size), 2)
                    last_line = f.readlines()[-1].decode("utf-8", errors="replace").strip()
            except Exception:
                pass
        jobs.append({
            "id": jid,
            "name": j["name"],
            "desc": j["desc"],
            "running": pid is not None,
            "pid": pid,
            "last_line": last_line[:120],
            "log_file": os.path.basename(log_path) if log_path else None,
        })
    return jsonify(jobs)


@app.route("/api/batch/<job_id>/start", methods=["POST"])
def api_batch_start(job_id: str):
    j = BATCH_JOBS.get(job_id)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    if _find_pid(j["match"]):
        return jsonify({"error": "이미 실행 중입니다"}), 409
    settings = _get_app_settings()
    min_cap = settings.get("min_market_cap", str(5_000_000_000_000))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logs_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, f"{j['log_prefix']}_{ts}.log")
    subprocess.Popen(
        f"PYTHONUNBUFFERED=1 MIN_MARKET_CAP={min_cap} nohup {j['cmd']} > {log_file} 2>&1",
        shell=True, cwd=BASE_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return jsonify({"ok": True, "log_file": os.path.basename(log_file)})


@app.route("/api/batch/<job_id>/stop", methods=["POST"])
def api_batch_stop(job_id: str):
    j = BATCH_JOBS.get(job_id)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    if not _find_pid(j["match"]):
        return jsonify({"error": "실행 중이 아닙니다"}), 409
    # shell=True로 실행 시 셸 프로세스 + Python 자식 프로세스가 모두 생성되므로
    # pkill -f 로 매칭되는 모든 프로세스를 한 번에 종료
    subprocess.run(["pkill", "-TERM", "-f", j["match"]], check=False)
    return jsonify({"ok": True})


def _get_app_settings() -> dict:
    try:
        rows = query("SELECT key, value FROM app_settings")
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}


@app.route("/api/settings")
def api_settings():
    return jsonify(_get_app_settings())


@app.route("/api/settings", methods=["PUT"])
def api_save_settings():
    data = request.get_json() or {}
    with get_conn() as conn:
        cur = conn.cursor()
        for key, value in data.items():
            cur.execute("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """, (key, str(value)))
    return jsonify({"ok": True})


@app.route("/api/schedule")
def api_schedule_get():
    rows = query("SELECT job_id, enabled, hour, minute, days, last_run FROM batch_schedules")
    result = {r["job_id"]: dict(r) for r in rows}
    for jid in BATCH_JOBS:
        if jid not in result:
            result[jid] = {"job_id": jid, "enabled": False, "hour": 9, "minute": 0, "days": "weekdays", "last_run": None}
    return jsonify(result)


@app.route("/api/schedule/<job_id>", methods=["PUT"])
def api_schedule_save(job_id: str):
    if job_id not in BATCH_JOBS:
        return jsonify({"error": "unknown job"}), 404
    data = request.get_json() or {}
    enabled = bool(data.get("enabled", False))
    hour    = int(data.get("hour", 9))
    minute  = int(data.get("minute", 0))
    days    = data.get("days", "weekdays")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO batch_schedules (job_id, enabled, hour, minute, days, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (job_id) DO UPDATE
              SET enabled = EXCLUDED.enabled,
                  hour    = EXCLUDED.hour,
                  minute  = EXCLUDED.minute,
                  days    = EXCLUDED.days,
                  updated_at = NOW()
        """, (job_id, enabled, hour, minute, days))
    _reload_scheduler_job(job_id, enabled, hour, minute, days)
    return jsonify({"ok": True})


@app.route("/api/batch/<job_id>/logs")
def api_batch_logs(job_id: str):
    j = BATCH_JOBS.get(job_id)
    if not j:
        return jsonify([])
    log_path = _latest_log(j["log_prefix"])
    if not log_path:
        return jsonify([])
    try:
        with open(log_path, "r", errors="replace") as f:
            lines = f.readlines()
        return jsonify([ln.rstrip() for ln in lines[-100:]])
    except Exception:
        return jsonify([])


# ---------------------------------------------------------------------------
# 스케줄러
# ---------------------------------------------------------------------------

_DAYS_MAP = {
    "daily":    "mon,tue,wed,thu,fri,sat,sun",
    "weekdays": "mon,tue,wed,thu,fri",
    "weekends": "sat,sun",
}

_scheduler = BackgroundScheduler(timezone="Asia/Seoul")


def _run_scheduled_job(job_id: str):
    j = BATCH_JOBS.get(job_id)
    if not j:
        return
    if _find_pid(j["match"]):
        logging.info("[scheduler] %s 이미 실행 중 — 스킵", job_id)
        return
    settings = _get_app_settings()
    min_cap = settings.get("min_market_cap", str(5_000_000_000_000))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logs_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, f"{j['log_prefix']}_{ts}.log")
    logging.info("[scheduler] %s 자동 실행 시작 → %s", job_id, log_file)
    subprocess.Popen(
        f"PYTHONUNBUFFERED=1 MIN_MARKET_CAP={min_cap} nohup {j['cmd']} > {log_file} 2>&1",
        shell=True, cwd=BASE_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with get_conn() as conn:
            conn.cursor().execute(
                "UPDATE batch_schedules SET last_run = NOW() WHERE job_id = %s", (job_id,)
            )
    except Exception:
        pass


def _reload_scheduler_job(job_id: str, enabled: bool, hour: int, minute: int, days: str):
    sched_id = f"batch_{job_id}"
    if _scheduler.get_job(sched_id):
        _scheduler.remove_job(sched_id)
    if not enabled:
        return
    day_str = _DAYS_MAP.get(days, "mon,tue,wed,thu,fri")
    _scheduler.add_job(
        _run_scheduled_job,
        CronTrigger(day_of_week=day_str, hour=hour, minute=minute, timezone="Asia/Seoul"),
        id=sched_id,
        args=[job_id],
        replace_existing=True,
    )
    logging.info("[scheduler] %s 등록: %02d:%02d [%s]", job_id, hour, minute, days)


def _init_scheduler():
    try:
        rows = query("SELECT job_id, enabled, hour, minute, days FROM batch_schedules")
        for r in rows:
            if r["enabled"]:
                _reload_scheduler_job(r["job_id"], True, r["hour"], r["minute"], r["days"])
    except Exception as e:
        logging.warning("[scheduler] 초기화 실패: %s", e)
    _scheduler.start()


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
    _init_scheduler()
except Exception as e:
    logging.warning("스케줄러 시작 실패: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True, use_reloader=False)
