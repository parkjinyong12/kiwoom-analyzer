"""
Audit / Monitor Agent
모든 파이프라인 이벤트 PostgreSQL 기록 + 시스템 상태 감시.

- DB write: 비동기 큐 처리 (파이프라인 블로킹 금지)
- 90일 초과 로그 자동 삭제
- 이상 감지 시 SlackNotifierAgent.send_error() 호출
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Optional

import psycopg2
import psycopg2.extras

from config import config
from models import RiskCheckResult, SupplyDemandFinding, TradeSignal

logger = logging.getLogger(__name__)

EventType = Literal[
    "DATA_FETCH", "ANALYSIS", "SIGNAL",
    "RISK_CHECK", "NOTIFICATION", "ERROR", "SYSTEM",
]
StatusType = Literal["SUCCESS", "FAIL", "BLOCKED"]


# ---------------------------------------------------------------------------
# 이벤트 데이터 클래스
# ---------------------------------------------------------------------------

@dataclass
class AuditEvent:
    event_type: EventType
    agent: str
    status: StatusType
    ticker: Optional[str] = None
    data: Optional[dict] = None
    timestamp: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.timestamp is None:
            self.timestamp = datetime.now()


# ---------------------------------------------------------------------------
# DB 연결 관리
# ---------------------------------------------------------------------------

class AuditDB:
    """PostgreSQL 연결 및 스키마 관리."""

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = psycopg2.connect(self._url, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id         BIGSERIAL PRIMARY KEY,
                    timestamp  TIMESTAMPTZ NOT NULL,
                    event_type TEXT NOT NULL,
                    agent      TEXT NOT NULL,
                    ticker     TEXT,
                    data       JSONB,
                    status     TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id           BIGSERIAL PRIMARY KEY,
                    timestamp    TIMESTAMPTZ NOT NULL,
                    ticker       TEXT NOT NULL,
                    signal       TEXT NOT NULL,
                    price        DOUBLE PRECISION NOT NULL,
                    target_price DOUBLE PRECISION,
                    stop_loss    DOUBLE PRECISION,
                    confidence   DOUBLE PRECISION,
                    strategy     TEXT,
                    result       TEXT,
                    result_price DOUBLE PRECISION
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_ticker ON events(ticker)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stocks (
                    stock_code  TEXT PRIMARY KEY,
                    stock_name  TEXT NOT NULL,
                    market_code TEXT,
                    market_name TEXT,
                    state       TEXT,
                    last_price  TEXT,
                    list_count  TEXT,
                    fetched_at  TIMESTAMPTZ NOT NULL,
                    watched     BOOLEAN DEFAULT FALSE
                )
            """)
            cur.execute("ALTER TABLE stocks ADD COLUMN IF NOT EXISTS watched BOOLEAN DEFAULT FALSE")
            cur.execute("ALTER TABLE stocks ADD COLUMN IF NOT EXISTS list_count TEXT")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS supply_demand (
                    id              BIGSERIAL PRIMARY KEY,
                    stock_code      TEXT NOT NULL,
                    date            DATE NOT NULL,
                    for_hold_qty    BIGINT,
                    for_chg_qty     BIGINT,
                    for_hold_ratio  TEXT,
                    orgn_net_qty    BIGINT,
                    for_net_qty     BIGINT,
                    ind_net_qty     BIGINT,
                    fnnc_invt       BIGINT,
                    insrnc          BIGINT,
                    invtrt          BIGINT,
                    bank            BIGINT,
                    penfnd_etc      BIGINT,
                    samo_fund       BIGINT,
                    UNIQUE(stock_code, date)
                )
            """)
            # 기존 테이블에 새 컬럼 추가 (없으면)
            for col in ("fnnc_invt", "insrnc", "invtrt", "bank", "penfnd_etc", "samo_fund"):
                cur.execute(f"ALTER TABLE supply_demand ADD COLUMN IF NOT EXISTS {col} BIGINT")

    # ------------------------------------------------------------------
    # 쓰기
    # ------------------------------------------------------------------

    def insert_event(self, event: AuditEvent) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO events (timestamp, event_type, agent, ticker, data, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    event.timestamp,
                    event.event_type,
                    event.agent,
                    event.ticker,
                    json.dumps(event.data, ensure_ascii=False) if event.data else None,
                    event.status,
                ),
            )

    def insert_signal(self, signal: TradeSignal) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO signals
                    (timestamp, ticker, signal, price, target_price, stop_loss,
                     confidence, strategy)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    signal.timestamp,
                    signal.ticker,
                    signal.signal,
                    signal.price,
                    signal.target_price,
                    signal.stop_loss,
                    signal.confidence,
                    signal.strategy_name,
                ),
            )

    def upsert_stock(self, info: dict, watched: bool = False) -> None:
        """종목 정보 저장 (없으면 INSERT, 있으면 UPDATE). watched는 명시적으로 전달한 경우만 갱신."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO stocks (stock_code, stock_name, market_code, market_name, state, last_price, fetched_at, watched)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stock_code) DO UPDATE SET
                    stock_name  = EXCLUDED.stock_name,
                    market_code = EXCLUDED.market_code,
                    market_name = EXCLUDED.market_name,
                    state       = EXCLUDED.state,
                    last_price  = EXCLUDED.last_price,
                    fetched_at  = EXCLUDED.fetched_at,
                    watched     = EXCLUDED.watched
                """,
                (
                    info["stock_code"],
                    info["stock_name"],
                    info.get("market_code", ""),
                    info.get("market_name", ""),
                    info.get("state", ""),
                    info.get("last_price", ""),
                    info["fetched_at"],
                    watched,
                ),
            )

    def upsert_stocks_bulk(self, stocks: list[dict], watched: bool = False) -> int:
        """종목 정보 일괄 저장. 저장된 건수 반환."""
        if not stocks:
            return 0
        with self._connect() as conn:
            cur = conn.cursor()
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO stocks (stock_code, stock_name, market_code, market_name, state, last_price, list_count, fetched_at, watched)
                VALUES %s
                ON CONFLICT (stock_code) DO UPDATE SET
                    stock_name  = EXCLUDED.stock_name,
                    market_code = EXCLUDED.market_code,
                    market_name = EXCLUDED.market_name,
                    state       = EXCLUDED.state,
                    last_price  = EXCLUDED.last_price,
                    list_count  = EXCLUDED.list_count,
                    fetched_at  = EXCLUDED.fetched_at
                """,
                [
                    (
                        s["stock_code"], s["stock_name"],
                        s.get("market_code", ""), s.get("market_name", ""),
                        s.get("state", ""), s.get("last_price", ""),
                        s.get("list_count", ""),
                        s["fetched_at"], watched,
                    )
                    for s in stocks
                ],
            )
            return len(stocks)

    def set_watched(self, stock_codes: list[str]) -> None:
        """지정 종목코드를 watched=True로 설정."""
        if not stock_codes:
            return
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE stocks SET watched = TRUE WHERE stock_code = ANY(%s)",
                (stock_codes,),
            )

    def set_watched_by_market_cap(self, min_cap: int) -> int:
        """시가총액(전일종가 × 상장주식수) 기준으로 watched 설정. watched 설정된 종목 수 반환."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT stock_code, last_price, list_count FROM stocks")
            rows = cur.fetchall()

        watched_codes = []
        for row in rows:
            try:
                price = int(row["last_price"].lstrip("0") or "0")
                count = int(row["list_count"].lstrip("0") or "0")
                if price * count >= min_cap:
                    watched_codes.append(row["stock_code"])
            except (ValueError, TypeError, AttributeError):
                continue

        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE stocks SET watched = FALSE")
            if watched_codes:
                cur.execute(
                    "UPDATE stocks SET watched = TRUE WHERE stock_code = ANY(%s)",
                    (watched_codes,),
                )
        return len(watched_codes)

    def get_watchlist(self) -> list[dict]:
        """watched=True 종목 목록 반환."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT stock_code, stock_name, market_name, fetched_at FROM stocks WHERE watched = TRUE ORDER BY stock_code"
            )
            return [dict(r) for r in cur.fetchall()]

    def get_stock_count(self) -> int:
        """전체 저장된 종목 수 반환."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as cnt FROM stocks")
            return cur.fetchone()["cnt"]

    def is_stocks_synced_today(self) -> bool:
        """오늘 날짜(UTC 기준)로 종목 동기화가 이미 완료됐는지 확인."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(fetched_at) as last_sync FROM stocks")
            row = cur.fetchone()
            if not row or not row["last_sync"]:
                return False
            from datetime import timezone
            now_utc = datetime.now(timezone.utc)
            last_utc = row["last_sync"].astimezone(timezone.utc)
            return last_utc.date() == now_utc.date()

    def purge_old_events(self, days: int = 90) -> int:
        """days일 초과 이벤트 삭제. 삭제 건수 반환."""
        cutoff = datetime.now() - timedelta(days=days)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM events WHERE timestamp < %s", (cutoff,))
            return cur.rowcount

    # ------------------------------------------------------------------
    # 읽기 (조회)
    # ------------------------------------------------------------------

    def get_recent_errors(self, hours: int = 1) -> list[dict]:
        cutoff = datetime.now() - timedelta(hours=hours)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT * FROM events
                WHERE status = 'FAIL' AND timestamp >= %s
                ORDER BY timestamp DESC
                """,
                (cutoff,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_signal_stats(self, days: int = 30) -> dict:
        """최근 N일 신호 통계."""
        cutoff = datetime.now() - timedelta(days=days)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT signal, COUNT(*) as cnt
                FROM signals
                WHERE timestamp >= %s
                GROUP BY signal
                """,
                (cutoff,),
            )
            return {r["signal"]: r["cnt"] for r in cur.fetchall()}

    def get_daily_event_count(self) -> dict:
        """오늘 이벤트 타입별 카운트."""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT event_type, status, COUNT(*) as cnt
                FROM events
                WHERE timestamp >= %s
                GROUP BY event_type, status
                """,
                (today,),
            )
            return {f"{r['event_type']}_{r['status']}": r["cnt"] for r in cur.fetchall()}

    def upsert_supply_demand(self, data: dict) -> None:
        """수급 데이터 저장. COALESCE로 기존 값 보존."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO supply_demand
                    (stock_code, date, for_hold_qty, for_chg_qty, for_hold_ratio,
                     orgn_net_qty, for_net_qty, ind_net_qty,
                     fnnc_invt, insrnc, invtrt, bank, penfnd_etc, samo_fund)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (stock_code, date) DO UPDATE SET
                    for_hold_qty   = COALESCE(EXCLUDED.for_hold_qty,   supply_demand.for_hold_qty),
                    for_chg_qty    = COALESCE(EXCLUDED.for_chg_qty,    supply_demand.for_chg_qty),
                    for_hold_ratio = COALESCE(EXCLUDED.for_hold_ratio, supply_demand.for_hold_ratio),
                    orgn_net_qty   = COALESCE(EXCLUDED.orgn_net_qty,   supply_demand.orgn_net_qty),
                    for_net_qty    = COALESCE(EXCLUDED.for_net_qty,    supply_demand.for_net_qty),
                    ind_net_qty    = COALESCE(EXCLUDED.ind_net_qty,    supply_demand.ind_net_qty),
                    fnnc_invt      = COALESCE(EXCLUDED.fnnc_invt,      supply_demand.fnnc_invt),
                    insrnc         = COALESCE(EXCLUDED.insrnc,         supply_demand.insrnc),
                    invtrt         = COALESCE(EXCLUDED.invtrt,         supply_demand.invtrt),
                    bank           = COALESCE(EXCLUDED.bank,           supply_demand.bank),
                    penfnd_etc     = COALESCE(EXCLUDED.penfnd_etc,     supply_demand.penfnd_etc),
                    samo_fund      = COALESCE(EXCLUDED.samo_fund,      supply_demand.samo_fund)
                """,
                (
                    data["stock_code"], data["date"],
                    data.get("for_hold_qty"), data.get("for_chg_qty"), data.get("for_hold_ratio"),
                    data.get("orgn_net_qty"),  data.get("for_net_qty"), data.get("ind_net_qty"),
                    data.get("fnnc_invt"),     data.get("insrnc"),      data.get("invtrt"),
                    data.get("bank"),          data.get("penfnd_etc"),  data.get("samo_fund"),
                ),
            )

    def upsert_supply_demand_batch(self, rows: list[dict]) -> int:
        """수급 데이터 배치 저장. 한 트랜잭션으로 처리."""
        if not rows:
            return 0
        sql = """
            INSERT INTO supply_demand
                (stock_code, date, for_hold_qty, for_chg_qty, for_hold_ratio,
                 orgn_net_qty, for_net_qty, ind_net_qty,
                 fnnc_invt, insrnc, invtrt, bank, penfnd_etc, samo_fund, close_price)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (stock_code, date) DO UPDATE SET
                for_hold_qty   = COALESCE(EXCLUDED.for_hold_qty,   supply_demand.for_hold_qty),
                for_chg_qty    = COALESCE(EXCLUDED.for_chg_qty,    supply_demand.for_chg_qty),
                for_hold_ratio = COALESCE(EXCLUDED.for_hold_ratio, supply_demand.for_hold_ratio),
                orgn_net_qty   = COALESCE(EXCLUDED.orgn_net_qty,   supply_demand.orgn_net_qty),
                for_net_qty    = COALESCE(EXCLUDED.for_net_qty,    supply_demand.for_net_qty),
                ind_net_qty    = COALESCE(EXCLUDED.ind_net_qty,    supply_demand.ind_net_qty),
                fnnc_invt      = COALESCE(EXCLUDED.fnnc_invt,      supply_demand.fnnc_invt),
                insrnc         = COALESCE(EXCLUDED.insrnc,         supply_demand.insrnc),
                invtrt         = COALESCE(EXCLUDED.invtrt,         supply_demand.invtrt),
                bank           = COALESCE(EXCLUDED.bank,           supply_demand.bank),
                penfnd_etc     = COALESCE(EXCLUDED.penfnd_etc,     supply_demand.penfnd_etc),
                samo_fund      = COALESCE(EXCLUDED.samo_fund,      supply_demand.samo_fund),
                close_price    = COALESCE(EXCLUDED.close_price,    supply_demand.close_price)
        """
        params = [
            (
                r["stock_code"], r["date"],
                r.get("for_hold_qty"), r.get("for_chg_qty"), r.get("for_hold_ratio"),
                r.get("orgn_net_qty"),  r.get("for_net_qty"), r.get("ind_net_qty"),
                r.get("fnnc_invt"),     r.get("insrnc"),      r.get("invtrt"),
                r.get("bank"),          r.get("penfnd_etc"),  r.get("samo_fund"),
                r.get("close_price"),
            )
            for r in rows
        ]
        with self._connect() as conn:
            cur = conn.cursor()
            psycopg2.extras.execute_batch(cur, sql, params, page_size=200)
        return len(rows)

    def get_stock_name(self, stock_code: str) -> str:
        """종목코드 → 종목명 조회. 없으면 빈 문자열."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT stock_name FROM stocks WHERE stock_code = %s", (stock_code,))
            row = cur.fetchone()
            return row["stock_name"] if row else ""

    def get_supply_demand_trend(self, stock_code: str, days: int = 10) -> list[dict]:
        """최근 N일 수급 데이터 (날짜 오름차순, 가장 최근이 마지막)."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT stock_code, date, for_hold_qty, for_chg_qty, for_hold_ratio,
                       orgn_net_qty, for_net_qty, ind_net_qty,
                       fnnc_invt, insrnc, invtrt, bank, penfnd_etc, samo_fund
                FROM supply_demand
                WHERE stock_code = %s
                ORDER BY date DESC
                LIMIT %s
                """,
                (stock_code, days),
            )
            rows = [dict(r) for r in cur.fetchall()]
        return list(reversed(rows))

    def get_supply_demand_dates(self, stock_code: str) -> set:
        """특정 종목의 이미 수집된 날짜 집합 반환 (중복 저장 방지용)."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT date FROM supply_demand WHERE stock_code = %s", (stock_code,))
            return {row["date"] for row in cur.fetchall()}

    def get_supply_demand_latest_date(self, stock_code: str):
        """특정 종목의 가장 최근 수급 데이터 날짜 반환. 없으면 None."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(date) AS latest FROM supply_demand WHERE stock_code = %s",
                (stock_code,),
            )
            row = cur.fetchone()
            return row["latest"] if row else None

    def get_supply_demand_recent(self, stock_code: str, days: int = 5) -> list[dict]:
        """최근 N일 수급 데이터 조회 (날짜 역순)."""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT stock_code, date, for_hold_qty, for_chg_qty, for_hold_ratio,
                       orgn_net_qty, for_net_qty, ind_net_qty
                FROM supply_demand
                WHERE stock_code = %s
                ORDER BY date DESC
                LIMIT %s
                """,
                (stock_code, days),
            )
            return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# 수급 트렌드 분석기
# ---------------------------------------------------------------------------

class SupplyDemandAnalyzer:
    """
    수급 데이터 트렌드 분석.

    감지 항목:
    1. 기관/외국인 연속 순매수·순매도 (CONSEC_DAYS일 이상)
    2. 외국인 보유비율 당일 급변 (± RATIO_DAY_THRESHOLD %p 이상)
    3. 외국인 보유비율 5일 누적 급변 (± RATIO_5D_THRESHOLD %p 이상)
    """

    CONSEC_DAYS = 3
    RATIO_DAY_THRESHOLD = 0.5    # 당일 보유비율 변화 기준 (%p)
    RATIO_5D_THRESHOLD  = 1.0    # 5일 누적 기준 (%p)

    def analyze(
        self,
        stock_code: str,
        stock_name: str,
        rows: list[dict],
    ) -> Optional[SupplyDemandFinding]:
        """
        rows: get_supply_demand_trend() 반환값 (날짜 오름차순).
        경보 없으면 None 반환.
        """
        if len(rows) < 2:
            return None

        alerts: list[str] = []
        details: dict = {}

        # ── 1. 연속 순매수/순매도 ──────────────────────────────
        for investor, col in [("기관", "orgn_net_qty"), ("외국인", "for_net_qty")]:
            consec = self._count_consecutive(rows, col)
            if consec >= self.CONSEC_DAYS:
                latest_qty = next(
                    (r[col] for r in reversed(rows) if r.get(col) is not None), 0
                )
                direction = "순매수" if (latest_qty or 0) > 0 else "순매도"
                alerts.append(f"{investor} {consec}일 연속 {direction}")
                details[f"{investor}_consec"] = {
                    "days": consec,
                    "direction": direction,
                    "recent_qty": latest_qty,
                }

        # ── 2. 외국인 보유비율 변화 ────────────────────────────
        ratios: list[Optional[float]] = []
        for r in rows:
            try:
                raw = (r.get("for_hold_ratio") or "").replace("+", "").strip()
                ratios.append(float(raw) if raw else None)
            except (ValueError, TypeError):
                ratios.append(None)

        valid = [(i, v) for i, v in enumerate(ratios) if v is not None]

        if len(valid) >= 2:
            latest_ratio = valid[-1][1]
            prev_ratio   = valid[-2][1]
            day_change   = latest_ratio - prev_ratio
            if abs(day_change) >= self.RATIO_DAY_THRESHOLD:
                direction = "증가" if day_change > 0 else "감소"
                alerts.append(
                    f"외국인 보유비율 당일 {day_change:+.2f}%p {direction} "
                    f"({prev_ratio:.2f}% → {latest_ratio:.2f}%)"
                )
                details["ratio_day"] = {
                    "change": day_change,
                    "latest": latest_ratio,
                    "prev": prev_ratio,
                }

        if len(valid) >= 5:
            latest_ratio  = valid[-1][1]
            five_ago_ratio = valid[-5][1]
            accum_change   = latest_ratio - five_ago_ratio
            if abs(accum_change) >= self.RATIO_5D_THRESHOLD:
                direction = "증가" if accum_change > 0 else "감소"
                alerts.append(
                    f"외국인 보유비율 5일 누적 {accum_change:+.2f}%p {direction} "
                    f"({five_ago_ratio:.2f}% → {latest_ratio:.2f}%)"
                )
                details["ratio_5d"] = {
                    "change": accum_change,
                    "latest": latest_ratio,
                    "five_days_ago": five_ago_ratio,
                }

        if not alerts:
            return None

        return SupplyDemandFinding(
            stock_code=stock_code,
            stock_name=stock_name,
            alerts=alerts,
            details=details,
        )

    def _count_consecutive(self, rows: list[dict], col: str) -> int:
        """가장 최근부터 연속으로 같은 부호(+/-)인 일수 반환."""
        valid = [r for r in rows if r.get(col) is not None]
        if not valid:
            return 0
        last_val = valid[-1][col]
        if not last_val:
            return 0
        sign = 1 if last_val > 0 else -1
        count = 0
        for r in reversed(valid):
            v = r[col]
            if v is None or v == 0:
                break
            if (1 if v > 0 else -1) != sign:
                break
            count += 1
        return count


# ---------------------------------------------------------------------------
# 비동기 쓰기 큐
# ---------------------------------------------------------------------------

class AsyncWriter:
    """
    DB write를 별도 스레드에서 처리.
    파이프라인 블로킹 방지.
    """

    def __init__(self, db: AuditDB) -> None:
        self._db = db
        self._queue: queue.Queue = queue.Queue(maxsize=1000)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def put_event(self, event: AuditEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning("AuditDB 큐 포화 — 이벤트 드롭: %s", event.event_type)

    def put_signal(self, signal: TradeSignal) -> None:
        try:
            self._queue.put_nowait(("signal", signal))
        except queue.Full:
            logger.warning("AuditDB 큐 포화 — 신호 드롭: %s", signal.ticker)

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if isinstance(item, AuditEvent):
                    self._db.insert_event(item)
                elif isinstance(item, tuple) and item[0] == "signal":
                    self._db.insert_signal(item[1])
            except Exception as e:
                logger.error("AuditDB 쓰기 오류: %s", e)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> None:
        """큐 비울 때까지 대기 (테스트/종료 시 사용)."""
        self._queue.join()


# ---------------------------------------------------------------------------
# 시스템 모니터
# ---------------------------------------------------------------------------

class SystemMonitor:
    """
    주기적 시스템 상태 감시.
    이상 감지 시 콜백(on_alert) 호출.
    """

    def __init__(
        self,
        on_alert: callable,
        check_interval: int = 300,   # 5분
    ) -> None:
        self._on_alert = on_alert
        self._interval = check_interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 파이프라인 지연 감시용
        self._last_pipeline_run: Optional[datetime] = None
        self._pipeline_timeout = 30   # 초

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("SystemMonitor 시작 (주기: %d초)", self._interval)

    def stop(self) -> None:
        self._stop_event.set()

    def record_pipeline_run(self) -> None:
        """파이프라인 실행 시점 기록."""
        self._last_pipeline_run = datetime.now()

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval):
            self._check_memory()

    def _check_memory(self) -> None:
        try:
            import psutil
            proc = psutil.Process()
            mem_mb = proc.memory_info().rss / 1024 / 1024
            cpu_pct = proc.cpu_percent(interval=1)

            if mem_mb > 500:
                self._on_alert(
                    "메모리 임계치 초과",
                    f"현재 사용량: {mem_mb:.0f}MB (기준 500MB)",
                )
            if cpu_pct > 80:
                self._on_alert(
                    "CPU 임계치 초과",
                    f"현재 사용률: {cpu_pct:.1f}% (기준 80%)",
                )
        except ImportError:
            pass   # psutil 미설치 시 스킵


# ---------------------------------------------------------------------------
# Audit / Monitor Agent
# ---------------------------------------------------------------------------

class AuditMonitorAgent:
    """
    파이프라인 이벤트 로깅 + 시스템 감시.

    사용 예:
        agent = AuditMonitorAgent()

        # 이벤트 기록
        agent.log_data_fetch("005930", success=True, bar_count=200)
        agent.log_signal(trade_signal)
        agent.log_risk_check(risk_result)
        agent.log_error("market_data", "TR 요청 실패", detail=str(e))

        # 조회
        stats = agent.get_stats()
    """

    def __init__(self, slack_notifier=None) -> None:
        self._db = AuditDB(config.database_url)
        self._writer = AsyncWriter(self._db)
        self._slack = slack_notifier
        self._monitor = SystemMonitor(on_alert=self._handle_alert)

    def start_monitor(self) -> None:
        self._monitor.start()

    def stop_monitor(self) -> None:
        self._monitor.stop()

    # ------------------------------------------------------------------
    # 이벤트 로깅 (에이전트별 편의 메서드)
    # ------------------------------------------------------------------

    def log_data_fetch(
        self,
        ticker: str,
        success: bool,
        timeframe: str = "D",
        bar_count: int = 0,
        error: Optional[str] = None,
    ) -> None:
        self._writer.put_event(AuditEvent(
            event_type="DATA_FETCH",
            agent="market_data",
            ticker=ticker,
            status="SUCCESS" if success else "FAIL",
            data={
                "timeframe": timeframe,
                "bar_count": bar_count,
                **({"error": error} if error else {}),
            },
        ))

    def log_analysis(
        self,
        ticker: str,
        timeframe: str,
        success: bool,
        patterns: Optional[list] = None,
    ) -> None:
        self._writer.put_event(AuditEvent(
            event_type="ANALYSIS",
            agent="chart_analysis",
            ticker=ticker,
            status="SUCCESS" if success else "FAIL",
            data={"timeframe": timeframe, "patterns": patterns or []},
        ))

    def log_signal(self, signal: TradeSignal) -> None:
        """Strategy Agent가 생성한 신호 기록."""
        self._writer.put_event(AuditEvent(
            event_type="SIGNAL",
            agent="strategy",
            ticker=signal.ticker,
            status="SUCCESS",
            data={
                "direction": signal.signal,
                "confidence": signal.confidence,
                "strategy": signal.strategy_name,
                "timeframe": signal.timeframe,
            },
        ))
        if signal.signal != "HOLD":
            self._writer.put_signal(signal)

    def log_risk_check(self, result: RiskCheckResult) -> None:
        """Risk Manager 결정 기록."""
        self._writer.put_event(AuditEvent(
            event_type="RISK_CHECK",
            agent="risk_manager",
            ticker=result.signal.ticker,
            status="SUCCESS" if result.approved else "BLOCKED",
            data={
                "approved": result.approved,
                "risk_level": result.risk_level,
                "adjusted_confidence": result.adjusted_confidence,
                "block_reasons": result.block_reasons,
            },
        ))

    def log_notification(self, ticker: str, channel: str, success: bool) -> None:
        """Slack 알림 발송 결과 기록."""
        self._writer.put_event(AuditEvent(
            event_type="NOTIFICATION",
            agent="slack_notifier",
            ticker=ticker,
            status="SUCCESS" if success else "FAIL",
            data={"channel": channel},
        ))

    def log_error(
        self,
        agent: str,
        title: str,
        detail: Optional[str] = None,
        ticker: Optional[str] = None,
    ) -> None:
        """에러 기록 + Slack #stock-errors 알림."""
        self._writer.put_event(AuditEvent(
            event_type="ERROR",
            agent=agent,
            ticker=ticker,
            status="FAIL",
            data={"title": title, "detail": detail or ""},
        ))
        if self._slack:
            self._slack.send_error(title, detail or "")

    def log_system(self, status: str, detail: str = "") -> None:
        """시스템 시작/종료 등 기록."""
        self._writer.put_event(AuditEvent(
            event_type="SYSTEM",
            agent="orchestrator",
            status="SUCCESS",
            data={"status": status, "detail": detail},
        ))

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """오늘 집계 및 최근 30일 신호 통계."""
        return {
            "today": self._db.get_daily_event_count(),
            "signals_30d": self._db.get_signal_stats(days=30),
            "recent_errors": self._db.get_recent_errors(hours=1),
        }

    # ------------------------------------------------------------------
    # 유지보수
    # ------------------------------------------------------------------

    def analyze_supply_demand(self, stock_code: str) -> Optional[SupplyDemandFinding]:
        """수급 트렌드 분석. 경보 발생 시 SupplyDemandFinding 반환, 없으면 None."""
        rows = self._db.get_supply_demand_trend(stock_code, days=10)
        if not rows:
            return None
        stock_name = self._db.get_stock_name(stock_code)
        return SupplyDemandAnalyzer().analyze(stock_code, stock_name, rows)

    def purge_old_logs(self) -> None:
        deleted = self._db.purge_old_events(days=90)
        logger.info("오래된 이벤트 %d건 삭제 완료", deleted)

    def flush(self) -> None:
        """큐 완전 소진 대기 (테스트/종료 시 사용)."""
        self._writer.flush()

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    def _handle_alert(self, title: str, detail: str) -> None:
        logger.warning("[SystemMonitor] %s: %s", title, detail)
        self.log_error("system_monitor", title, detail)
