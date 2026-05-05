"""
Flask 웹 대시보드
PostgreSQL DB를 직접 읽어 분석 현황을 시각화.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import json
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
}


def _find_pid(match: str) -> int | None:
    try:
        r = subprocess.run(["pgrep", "-f", match], capture_output=True, text=True)
        pids = [int(p) for p in r.stdout.strip().split() if p]
        return pids[0] if pids else None
    except Exception:
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
    today_kst = datetime.now(timezone.utc).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)

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
        r["price"] = f"{int(r['price']):,}" if r["price"] else "-"
        r["confidence_pct"] = f"{r['confidence'] * 100:.0f}%" if r["confidence"] else "-"

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
# API — 사용자 관리
# ---------------------------------------------------------------------------

ALL_MENUS = ["dashboard", "supply", "divergence", "signals", "auditlog", "stocks", "batch", "usermgmt"]

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
    log_file = os.path.join(BASE_DIR, "logs", f"{j['log_prefix']}_{ts}.log")
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
    log_file = os.path.join(BASE_DIR, "logs", f"{j['log_prefix']}_{ts}.log")
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
    _init_scheduler()
except Exception as e:
    logging.warning("스케줄러 시작 실패: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True, use_reloader=False)
