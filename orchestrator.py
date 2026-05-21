"""
Orchestrator
전체 분석 파이프라인 조율 및 스케줄 관리.

파이프라인 순서:
  Market Data → Chart Analysis → Strategy → Risk Manager → Slack → Audit

스케줄 (KST):
  08:50  장 전 준비 (전일 데이터 수집 + 시스템 상태 알림)
  09:05~ 장 중 주기 분석 (interval_minutes 간격)
  15:35  장 마감 일일 요약 알림

주문 관련 코드 절대 포함 금지.
"""
from __future__ import annotations

import logging
import signal
import sys
from datetime import date, datetime, timedelta
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.audit_monitor import AuditMonitorAgent
from agents.chart_analysis import ChartAnalysisAgent
from agents.risk_manager import RiskManagerAgent
from agents.slack_notifier import SlackNotifierAgent
from agents.strategy import StrategyAgent
from config import config

logger = logging.getLogger(__name__)

# 키움 API는 Windows COM 환경에서만 동작
# 로컬(Mac/Linux) 실행 시 MarketDataAgent import 스킵
try:
    from agents.market_data import MarketDataAgent
    _MARKET_DATA_AVAILABLE = True
except ImportError:
    _MARKET_DATA_AVAILABLE = False
    logger.warning("MarketDataAgent 불가 (Windows COM 환경 아님) — dry-run 모드")


# ---------------------------------------------------------------------------
# 파이프라인
# ---------------------------------------------------------------------------

class Pipeline:
    """
    단일 종목 분석 파이프라인.
    Market Data → Chart Analysis → Strategy → Risk → Slack
    각 단계 결과를 Audit에 기록.
    """

    def __init__(
        self,
        market_data,
        chart: ChartAnalysisAgent,
        strategy: StrategyAgent,
        risk: RiskManagerAgent,
        slack: SlackNotifierAgent,
        audit: AuditMonitorAgent,
    ) -> None:
        self._market = market_data
        self._chart = chart
        self._strategy = strategy
        self._risk = risk
        self._slack = slack
        self._audit = audit
        # 당일 수급 알림 중복 방지: {ticker: alert_date}
        self._supply_alerted: dict[str, date] = {}

    def run(self, ticker: str, timeframes: list[str]) -> None:
        """단일 종목 전체 파이프라인 실행."""
        logger.info("▶ 파이프라인 시작: %s %s", ticker, timeframes)

        # ── 1. 데이터 수집 ──────────────────────────────
        ohlcv_map: dict = {}
        for tf in timeframes:
            try:
                if self._market is None:
                    logger.debug("[dry-run] %s %s 데이터 수집 스킵", ticker, tf)
                    continue
                if tf == "D":
                    df = self._market.get_daily_ohlcv(ticker, count=config.kiwoom.default_daily_count)
                else:
                    df = self._market.get_minute_ohlcv(ticker, timeframe=tf, count=config.kiwoom.default_minute_count)

                if df.empty:
                    self._audit.log_data_fetch(ticker, success=False, timeframe=tf, error="빈 데이터")
                    continue

                ohlcv_map[tf] = df
                self._audit.log_data_fetch(ticker, success=True, timeframe=tf, bar_count=len(df))

            except Exception as e:
                self._audit.log_error("market_data", f"{ticker} {tf} 데이터 수집 실패", str(e), ticker)
                logger.error("%s %s 데이터 수집 오류: %s", ticker, tf, e)

        if not ohlcv_map:
            logger.warning("%s 수집된 데이터 없음 — 파이프라인 중단", ticker)
            return

        # ── 1-b. 수급 데이터 수집 ─────────────────────────
        if self._market:
            try:
                today_str = datetime.now().strftime("%Y%m%d")
                holding_rows = self._market.get_foreign_holding(ticker)
                netbuy = self._market.get_investor_netbuy(ticker, today_str)

                for row in holding_rows:
                    sd = {
                        "stock_code": ticker,
                        "date": row["date"],
                        "for_hold_qty": row["for_hold_qty"],
                        "for_chg_qty": row["for_chg_qty"],
                        "for_hold_ratio": row["for_hold_ratio"],
                        "orgn_net_qty": None,
                        "for_net_qty": None,
                        "ind_net_qty": None,
                    }
                    if netbuy and str(row["date"]) == str(netbuy["date"]):
                        sd["orgn_net_qty"] = netbuy["orgn_net_qty"]
                        sd["for_net_qty"] = netbuy["for_net_qty"]
                        sd["ind_net_qty"] = netbuy["ind_net_qty"]
                    self._audit._db.upsert_supply_demand(sd)
            except Exception as e:
                logger.error("%s 수급 데이터 수집 오류: %s", ticker, e)

        # ── 1-c. 수급 트렌드 분석 및 알림 ────────────────────
        if self._market:
            today = datetime.now().date()
            if self._supply_alerted.get(ticker) != today:
                try:
                    finding = self._audit.analyze_supply_demand(ticker)
                    if finding:
                        self._slack.send_supply_demand_alert(finding)
                        self._audit.log_system(
                            "수급 경보",
                            f"{ticker} {finding.alerts}",
                        )
                        self._supply_alerted[ticker] = today
                        logger.info("%s 수급 경보 발송: %s", ticker, finding.alerts)
                except Exception as e:
                    logger.error("%s 수급 분석 오류: %s", ticker, e)

        # ── 2. 차트 분석 ──────────────────────────────
        chart_signals = self._chart.analyze_multi(ticker, ohlcv_map)

        for tf, cs in chart_signals.items():
            self._audit.log_analysis(ticker, tf, success=True, patterns=cs.patterns)

        if not chart_signals:
            logger.warning("%s 차트 분석 결과 없음 — 파이프라인 중단", ticker)
            return

        # ── 3. 전략 신호 생성 ─────────────────────────
        primary_tf = "D" if "D" in chart_signals else next(iter(chart_signals))
        trade_signal = self._strategy.run_multi(ticker, chart_signals, primary_tf=primary_tf)
        self._audit.log_signal(trade_signal)

        if trade_signal.signal == "HOLD":
            logger.debug("%s HOLD — 이후 단계 스킵", ticker)
            return

        # ── 4. 리스크 검증 ────────────────────────────
        risk_result = self._risk.check(trade_signal)
        self._audit.log_risk_check(risk_result)

        if not risk_result.approved:
            logger.info("%s 신호 차단: %s", ticker, risk_result.block_reasons)
            return

        # ── 5. Slack 알림 ─────────────────────────────
        send_result = self._slack.send_signal(risk_result)
        self._audit.log_notification(
            ticker,
            config.slack.channel,
            success=send_result.success,
        )
        if not send_result.success:
            self._audit.log_error(
                "slack_notifier",
                f"{ticker} 알림 발송 실패",
                send_result.error,
                ticker,
            )

        logger.info("✔ 파이프라인 완료: %s → %s (confidence=%.2f)",
                    ticker, trade_signal.signal, risk_result.adjusted_confidence)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """
    전체 시스템 조율자.
    APScheduler로 장 전/장 중/장 마감 파이프라인을 스케줄링.
    """

    def __init__(self, interval_minutes: int = 30) -> None:
        self._interval = interval_minutes
        self._scheduler = BlockingScheduler(timezone="Asia/Seoul")

        # 에이전트 초기화
        self._audit  = AuditMonitorAgent()
        self._slack  = SlackNotifierAgent()
        self._audit._slack = self._slack   # 에러 알림 연결

        self._chart    = ChartAnalysisAgent()
        self._strategy = StrategyAgent()
        self._risk     = RiskManagerAgent()

        market = None
        if _MARKET_DATA_AVAILABLE:
            market = MarketDataAgent()

        self._market = market
        self._pipeline = Pipeline(
            market_data=market,
            chart=self._chart,
            strategy=self._strategy,
            risk=self._risk,
            slack=self._slack,
            audit=self._audit,
        )

        self._sync_watchlist()
        self._setup_schedules()
        self._setup_signal_handlers()

    # ------------------------------------------------------------------
    # 종목 동기화
    # ------------------------------------------------------------------

    def _sync_watchlist(self) -> None:
        """코스피+코스닥 전체 종목을 DB에 저장하고, config.watchlist 종목을 watched=True로 설정.
        오늘 이미 동기화된 경우 watched 설정만 갱신하고 전체 조회는 스킵.
        """
        if not self._market:
            logger.warning("MarketDataAgent 없음 — 종목 동기화 스킵")
            return

        db = self._audit._db

        if db.is_stocks_synced_today():
            stock_count = db.get_stock_count()
            logger.info("오늘 이미 종목 동기화 완료 (%d개) — 전체 조회 스킵", stock_count)
        else:
            total = 0
            for mrkt_tp, market_name in [("0", "코스피"), ("10", "코스닥")]:
                try:
                    logger.info("%s 전체 종목 조회 중...", market_name)
                    stocks = self._market.get_all_stocks(mrkt_tp)
                    count = db.upsert_stocks_bulk(stocks)
                    total += count
                    logger.info("%s 종목 저장 완료: %d건", market_name, count)
                except Exception as e:
                    logger.error("%s 종목 조회 실패: %s", market_name, e)
            logger.info("전체 종목 동기화 완료: %d건 저장", total)

        if config.min_market_cap > 0:
            count = db.set_watched_by_market_cap(config.min_market_cap)
            logger.info("시가총액 %d억원 이상 감시 종목 %d개 설정 완료", config.min_market_cap // 100_000_000, count)
        else:
            db.set_watched(config.watchlist)
            logger.info("감시 종목 %d개 설정 완료", len(config.watchlist))

    def _get_watchlist(self) -> list[str]:
        """DB에서 watched=True 종목 반환. manual_holdings 종목도 항상 포함."""
        stocks = self._audit._db.get_watchlist()
        codes = set(s["stock_code"] for s in stocks) if stocks else set(config.watchlist)

        # 보유종목 화면의 종목은 시총 무관하게 수급 수집 대상에 포함
        try:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(config.database_url, cursor_factory=psycopg2.extras.RealDictCursor)
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT DISTINCT stock_code FROM manual_holdings WHERE quantity > 0")
                    for r in cur.fetchall():
                        codes.add(r["stock_code"])
            conn.close()
        except Exception as e:
            logger.warning("manual_holdings 종목 조회 실패: %s", e)

        return list(codes)

    # ------------------------------------------------------------------
    # 스케줄 설정
    # ------------------------------------------------------------------

    def _setup_schedules(self) -> None:
        # 장 전 준비 (08:50 KST)
        self._scheduler.add_job(
            self._job_pre_market,
            CronTrigger(hour=8, minute=50, timezone="Asia/Seoul"),
            id="pre_market",
            name="장 전 준비",
        )

        # 장 중 주기 분석 (09:05 ~ 15:25, interval_minutes 간격)
        self._scheduler.add_job(
            self._job_intraday,
            CronTrigger(
                hour="9-15",
                minute=f"5/{self._interval}",
                day_of_week="mon-fri",
                timezone="Asia/Seoul",
            ),
            id="intraday",
            name=f"장 중 분석 ({self._interval}분 간격)",
        )

        # 장 마감 요약 (15:35 KST)
        self._scheduler.add_job(
            self._job_market_close,
            CronTrigger(hour=15, minute=35, timezone="Asia/Seoul"),
            id="market_close",
            name="장 마감 요약",
        )

        # 로그 정리 (매일 00:10)
        self._scheduler.add_job(
            self._job_purge_logs,
            CronTrigger(hour=0, minute=10, timezone="Asia/Seoul"),
            id="purge_logs",
            name="오래된 로그 정리",
        )

        logger.info(
            "스케줄 등록 완료: 장 전(08:50) / 장 중(%d분 간격) / 장 마감(15:35)",
            self._interval,
        )

    # ------------------------------------------------------------------
    # 잡 구현
    # ------------------------------------------------------------------

    def _job_pre_market(self) -> None:
        """장 전 준비: 전일 일봉 수집 + 시스템 시작 알림."""
        logger.info("=== 장 전 준비 시작 (08:50) ===")
        self._audit.log_system("장 전 준비 시작")
        watchlist = self._get_watchlist()
        self._slack.send_system_status("시작", f"감시 종목 {len(watchlist)}개")

        # 코스피 등락률 업데이트 (구현 시 Market Data Agent에서 수신)
        # self._risk.market_ctx.update(kospi_change_rate)

        for ticker in watchlist:
            try:
                self._pipeline.run(ticker, timeframes=["D"])
            except Exception as e:
                self._audit.log_error("orchestrator", f"장 전 분석 오류 ({ticker})", str(e), ticker)

        logger.info("=== 장 전 준비 완료 ===")

    def _job_intraday(self) -> None:
        """장 중 주기 분석."""
        now = datetime.now()
        logger.info("=== 장 중 분석 시작 (%s) ===", now.strftime("%H:%M"))
        self._audit.monitor.record_pipeline_run()

        for ticker in self._get_watchlist():
            try:
                self._pipeline.run(ticker, timeframes=config.timeframes)
            except Exception as e:
                self._audit.log_error("orchestrator", f"장 중 분석 오류 ({ticker})", str(e), ticker)

        logger.info("=== 장 중 분석 완료 (%s) ===", now.strftime("%H:%M"))

    def _job_market_close(self) -> None:
        """장 마감: 일일 요약 발송 + 쿨다운 초기화."""
        logger.info("=== 장 마감 처리 시작 (15:35) ===")
        self._slack.send_daily_summary()
        self._risk.cooldown.clear()   # 익일을 위해 쿨다운 초기화
        self._audit.log_system("장 마감 처리 완료")
        logger.info("=== 장 마감 처리 완료 ===")

    def _job_purge_logs(self) -> None:
        """90일 초과 로그 정리."""
        self._audit.purge_old_logs()

    # ------------------------------------------------------------------
    # 실행 / 종료
    # ------------------------------------------------------------------

    def start(self) -> None:
        logger.info("Orchestrator 시작 (감시 종목: %s)", self._get_watchlist())
        self._audit.start_monitor()
        self._audit.log_system("시스템 시작")
        try:
            self._scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            self.stop()

    def stop(self) -> None:
        logger.info("Orchestrator 종료 중...")
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:
            pass
        self._slack.send_system_status("종료")
        self._audit.log_system("시스템 종료")
        self._audit.stop_monitor()
        self._audit.flush()
        logger.info("Orchestrator 종료 완료")

    def run_once(self, timeframes: Optional[list[str]] = None) -> None:
        """스케줄 없이 즉시 1회 전체 분석 실행 (테스트/수동 실행용)."""
        tfs = timeframes or config.timeframes
        logger.info("수동 1회 실행: 타임프레임=%s", tfs)
        for ticker in self._get_watchlist():
            self._pipeline.run(ticker, timeframes=tfs)

    def collect_supply_history(self, max_days: int = 500) -> None:
        """
        전체 감시 종목의 외국인 보유 이력을 일괄 수집 (일회성 히스토리 적재).

        - ka10008 페이지네이션으로 종목당 최대 max_days일 수집
        - 이미 DB에 있는 날짜는 건너뜀 (재실행 안전)
        - 기관/외국인 순매수(ka10059)는 일별 파이프라인에서 계속 누적
        """
        if not self._market:
            logger.error("MarketDataAgent 없음 — 수급 이력 수집 불가")
            return

        watchlist = self._get_watchlist()
        total = len(watchlist)
        db = self._audit._db
        total_new = 0

        logger.info("=== 수급 이력 수집 시작: %d개 종목, 최대 %d일 ===", total, max_days)

        # 최근 영업일 계산 (토/일 제외)
        today = date.today()
        weekday = today.weekday()
        if weekday == 5:    # 토요일
            latest_biz = today - timedelta(days=1)
        elif weekday == 6:  # 일요일
            latest_biz = today - timedelta(days=2)
        else:
            latest_biz = today

        for idx, ticker in enumerate(watchlist, 1):
            try:
                # 최근 영업일 데이터가 이미 있으면 스킵 (재실행 안전)
                latest = db.get_supply_demand_latest_date(ticker)
                if latest and latest >= latest_biz - timedelta(days=3):
                    existing_cnt = len(db.get_supply_demand_dates(ticker))
                    logger.info(
                        "[%d/%d] %s 스킵 (최근 데이터 %s 이미 존재, %d일)",
                        idx, total, ticker, latest, existing_cnt,
                    )
                    total_new += 0
                    continue

                existing = db.get_supply_demand_dates(ticker)
                logger.info("[%d/%d] %s 수집 중... (기존 %d일)", idx, total, ticker, len(existing))

                # ── 외국인 보유비율 이력 (ka10008 페이지네이션)
                holding_rows = self._market.get_foreign_holding(ticker, max_days=max_days)
                new_holding = [
                    {"stock_code": ticker, "date": r["date"],
                     "for_hold_qty": r["for_hold_qty"], "for_chg_qty": r["for_chg_qty"],
                     "for_hold_ratio": r["for_hold_ratio"], "close_price": r.get("close_price")}
                    for r in holding_rows if r["date"] not in existing
                ]
                db.upsert_supply_demand_batch(new_holding)
                for r in new_holding:
                    existing.add(r["date"])

                # ── 투자자별 순매수 이력 (ka10059 페이지네이션)
                inv_rows = self._market.get_investor_netbuy_history(ticker, max_days=max_days)
                inv_batch = [
                    {"stock_code": ticker, "date": r["date"],
                     "orgn_net_qty": r["orgn_net_qty"], "for_net_qty": r["for_net_qty"],
                     "ind_net_qty": r["ind_net_qty"], "fnnc_invt": r["fnnc_invt"],
                     "insrnc": r["insrnc"], "invtrt": r["invtrt"], "bank": r["bank"],
                     "penfnd_etc": r["penfnd_etc"], "samo_fund": r["samo_fund"]}
                    for r in inv_rows
                ]
                db.upsert_supply_demand_batch(inv_batch)
                new_count = sum(1 for r in inv_rows if r["date"] not in existing)

                total_new += new_count
                logger.info(
                    "[%d/%d] %s 완료: 외국인 %d일 / 투자자 %d일 저장",
                    idx, total, ticker, len(holding_rows), len(inv_rows),
                )

            except Exception as e:
                logger.error("[%d/%d] %s 수급 이력 수집 오류: %s", idx, total, ticker, e)

        logger.info("=== 수급 이력 수집 완료: 총 %d건 신규 저장 ===", total_new)

    def _setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT,  lambda s, f: self.stop())
        signal.signal(signal.SIGTERM, lambda s, f: self.stop())


# ---------------------------------------------------------------------------
# 로깅 설정
# ---------------------------------------------------------------------------

def setup_logging(log_dir: str = "logs") -> None:
    import os
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"{datetime.now().strftime('%Y%m%d')}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="키움 차트 분석 시스템")
    parser.add_argument("--interval", type=int, default=30, help="장 중 분석 주기 (분, 기본 30)")
    parser.add_argument("--once", action="store_true", help="스케줄 없이 즉시 1회 실행")
    args = parser.parse_args()

    setup_logging(config.log_dir)

    orchestrator = Orchestrator(interval_minutes=args.interval)

    if args.once:
        orchestrator.run_once()
    else:
        orchestrator.start()
