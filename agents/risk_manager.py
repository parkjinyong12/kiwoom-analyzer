"""
Risk Manager Agent
TradeSignal → 리스크 조건 검증 → RiskCheckResult 반환.

승인(PASS) / 차단(BLOCK) 결정만 수행.
주문 수량, 금액 계산 로직 절대 포함 금지.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Literal, Optional

from config import config
from models import RiskCheckResult, TradeSignal

logger = logging.getLogger(__name__)

RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]


# ---------------------------------------------------------------------------
# 쿨다운 스토어 (인메모리, 프로세스 재시작 시 초기화)
# ---------------------------------------------------------------------------

class CooldownStore:
    """
    종목별 마지막 신호 발송 시간 추적.
    thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # {ticker: {"BUY": datetime, "SELL": datetime}}
        self._store: dict[str, dict[str, datetime]] = {}

    def is_in_cooldown(
        self,
        ticker: str,
        direction: str,
        cooldown_hours: int,
    ) -> tuple[bool, Optional[float]]:
        """
        쿨다운 여부 확인.

        Returns:
            (in_cooldown, remaining_hours)
        """
        with self._lock:
            last = self._store.get(ticker, {}).get(direction)
            if last is None:
                return False, None
            elapsed = (datetime.now() - last).total_seconds() / 3600
            remaining = cooldown_hours - elapsed
            return remaining > 0, round(remaining, 1) if remaining > 0 else None

    def record(self, ticker: str, direction: str) -> None:
        with self._lock:
            if ticker not in self._store:
                self._store[ticker] = {}
            self._store[ticker][direction] = datetime.now()

    def clear(self, ticker: Optional[str] = None) -> None:
        with self._lock:
            if ticker:
                self._store.pop(ticker, None)
            else:
                self._store.clear()


# ---------------------------------------------------------------------------
# 시장 상태 컨텍스트
# ---------------------------------------------------------------------------

class MarketContext:
    """
    시장 전체 상태 (코스피 등락률 등).
    Orchestrator가 주기적으로 업데이트.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._kospi_change_rate: Optional[float] = None
        self._updated_at: Optional[datetime] = None

    def update(self, kospi_change_rate: float) -> None:
        with self._lock:
            self._kospi_change_rate = kospi_change_rate
            self._updated_at = datetime.now()
        logger.debug("MarketContext 업데이트: 코스피 %.2f%%", kospi_change_rate)

    def is_market_crash(self, threshold: float) -> bool:
        """코스피가 threshold% 이상 하락 중이면 True."""
        with self._lock:
            if self._kospi_change_rate is None:
                return False
            # 데이터가 30분 이상 오래되면 신뢰 불가 → 차단하지 않음
            if self._updated_at and (datetime.now() - self._updated_at) > timedelta(minutes=30):
                logger.warning("MarketContext 데이터 오래됨 (30분 초과), 시장 급락 체크 스킵")
                return False
            return self._kospi_change_rate <= threshold

    @property
    def kospi_change_rate(self) -> Optional[float]:
        with self._lock:
            return self._kospi_change_rate


# ---------------------------------------------------------------------------
# Risk Manager Agent
# ---------------------------------------------------------------------------

class RiskManagerAgent:
    """
    TradeSignal의 알림 발송 여부를 최종 결정.

    사용 예:
        agent = RiskManagerAgent()
        result = agent.check(trade_signal)
        if result.approved:
            slack_agent.send(result.signal)
        else:
            audit_agent.log_block(result)
    """

    def __init__(self) -> None:
        self.cooldown = CooldownStore()
        self.market_ctx = MarketContext()

    # ------------------------------------------------------------------
    # 공개 인터페이스
    # ------------------------------------------------------------------

    def check(self, signal: TradeSignal) -> RiskCheckResult:
        """
        TradeSignal 리스크 검증.

        Returns:
            RiskCheckResult (approved=True → 알림 발송 가능)
        """
        block_reasons: list[str] = []

        # HOLD 신호는 알림 불필요 → 즉시 차단
        if signal.signal == "HOLD":
            return RiskCheckResult(
                signal=signal,
                approved=False,
                block_reasons=["HOLD 신호 — 알림 불필요"],
                risk_level="LOW",
                adjusted_confidence=0.0,
            )

        # --- 개별 체크 ---
        self._check_confidence(signal, block_reasons)
        self._check_cooldown(signal, block_reasons)
        self._check_volume(signal, block_reasons)
        self._check_market_crash(signal, block_reasons)

        approved = len(block_reasons) == 0
        risk_level = self._assess_risk_level(signal)
        adjusted_confidence = self._adjust_confidence(signal, risk_level)

        if approved:
            # 승인 시 쿨다운 기록 (SELL은 쿨다운 없음이지만 기록은 유지)
            if signal.signal == "BUY":
                self.cooldown.record(signal.ticker, signal.signal)
            logger.info(
                "[RiskManager] PASS %s %s (confidence=%.2f, risk=%s)",
                signal.ticker, signal.signal, adjusted_confidence, risk_level,
            )
        else:
            logger.info(
                "[RiskManager] BLOCK %s %s — %s",
                signal.ticker, signal.signal, " | ".join(block_reasons),
            )

        return RiskCheckResult(
            signal=signal,
            approved=approved,
            block_reasons=block_reasons,
            risk_level=risk_level,
            adjusted_confidence=adjusted_confidence,
        )

    def check_batch(self, signals: list[TradeSignal]) -> list[RiskCheckResult]:
        """여러 신호 일괄 검증."""
        return [self.check(s) for s in signals]

    # ------------------------------------------------------------------
    # 개별 체크 메서드
    # ------------------------------------------------------------------

    def _check_confidence(
        self,
        signal: TradeSignal,
        block_reasons: list[str],
    ) -> None:
        threshold = config.risk.min_confidence
        if signal.confidence < threshold:
            block_reasons.append(
                f"신뢰도 부족: {signal.confidence:.2f} < 기준 {threshold:.2f}"
            )

    def _check_cooldown(
        self,
        signal: TradeSignal,
        block_reasons: list[str],
    ) -> None:
        # SELL 신호는 쿨다운 적용 안 함
        if signal.signal == "SELL":
            return

        in_cd, remaining = self.cooldown.is_in_cooldown(
            signal.ticker,
            signal.signal,
            config.risk.signal_cooldown_hours,
        )
        if in_cd:
            block_reasons.append(
                f"쿨다운 중: {signal.ticker} {signal.signal} "
                f"— {remaining:.1f}시간 후 재알림 가능"
            )

    def _check_volume(
        self,
        signal: TradeSignal,
        block_reasons: list[str],
    ) -> None:
        """거래량 급감 상태 체크 (vol_ratio < 0.3 = 거래량 70% 이상 급감)."""
        # TradeSignal에 직접 지표가 없으므로 indicators에서 꺼냄
        # (ChartSignal → TradeSignal 흐름상 signal에 indicators 없으므로 스킵 가능)
        # Orchestrator에서 별도로 주입하는 방식으로 확장 가능
        pass

    def _check_market_crash(
        self,
        signal: TradeSignal,
        block_reasons: list[str],
    ) -> None:
        """시장 급락 중 BUY 신호 차단."""
        if signal.signal != "BUY":
            return

        threshold = config.risk.market_drop_threshold
        if self.market_ctx.is_market_crash(threshold):
            rate = self.market_ctx.kospi_change_rate
            block_reasons.append(
                f"시장 급락 중 BUY 차단: 코스피 {rate:.2f}% "
                f"(기준 {threshold:.1f}%)"
            )

    # ------------------------------------------------------------------
    # 리스크 레벨 평가
    # ------------------------------------------------------------------

    def _assess_risk_level(self, signal: TradeSignal) -> RiskLevel:
        """
        신호 특성 기반 리스크 레벨 산출.
        Slack 알림에 색상/이모지 표시용.
        """
        confidence = signal.confidence

        if confidence >= 0.80:
            return "LOW"
        elif confidence >= 0.70:
            return "MEDIUM"
        else:
            return "HIGH"

    def _adjust_confidence(
        self,
        signal: TradeSignal,
        risk_level: RiskLevel,
    ) -> float:
        """
        리스크 레벨에 따라 confidence 소폭 조정.
        HIGH 리스크 신호는 표시 confidence를 낮춰 주의 환기.
        """
        adjustment = {"LOW": 0.0, "MEDIUM": -0.02, "HIGH": -0.05}
        adjusted = signal.confidence + adjustment[risk_level]
        return round(max(adjusted, 0.0), 4)
