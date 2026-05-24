"""
Strategy Agent
ChartSignal → 전략별 독립 실행 → 신호 통합 → TradeSignal 반환.

전략 추가: BaseStrategy 상속 후 StrategyAgent.strategies에 등록.
주문 로직 절대 포함 금지.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from models import (
    ChartSignal, TradeSignal,
    MarketState, EffortResult, BreakType,
)

logger = logging.getLogger(__name__)

Direction = Literal["BUY", "SELL", "HOLD"]


# ---------------------------------------------------------------------------
# 전략 결과 (내부용)
# ---------------------------------------------------------------------------

@dataclass
class StrategyResult:
    strategy_name: str
    direction: Direction
    confidence: float        # 0.0 ~ 1.0
    reasons: list[str]
    weight: float = 1.0      # 통합 시 가중치


# ---------------------------------------------------------------------------
# 기본 전략 추상 클래스
# ---------------------------------------------------------------------------

class BaseStrategy(ABC):
    """모든 전략의 기반 클래스."""

    name: str = "base"
    weight: float = 1.0

    def run(self, signal: ChartSignal) -> Optional[StrategyResult]:
        """
        전략 실행. 신호 판단 불가 시 None 반환.
        내부에서 예외 발생 시 로깅 후 None 반환.
        """
        try:
            return self._evaluate(signal)
        except Exception as e:
            logger.error("[%s] 전략 실행 오류 (%s): %s", self.name, signal.ticker, e)
            return None

    @abstractmethod
    def _evaluate(self, signal: ChartSignal) -> Optional[StrategyResult]:
        ...

    # ------------------------------------------------------------------
    # 헬퍼
    # ------------------------------------------------------------------

    @staticmethod
    def _ind(signal: ChartSignal, key: str) -> Optional[float]:
        return signal.indicators.get(key)

    @staticmethod
    def _result(
        name: str,
        direction: Direction,
        confidence: float,
        reasons: list[str],
        weight: float = 1.0,
    ) -> StrategyResult:
        return StrategyResult(
            strategy_name=name,
            direction=direction,
            confidence=min(max(confidence, 0.0), 1.0),
            reasons=reasons,
            weight=weight,
        )


# ---------------------------------------------------------------------------
# 전략 1: 골든크로스 / 데드크로스
# ---------------------------------------------------------------------------

class GoldenCrossStrategy(BaseStrategy):
    """
    MA5 × MA20 크로스 + 거래량 확인.
    - 골든크로스 + 거래량 증가 → BUY
    - 데드크로스 + 거래량 증가 → SELL
    """

    name = "골든크로스"
    weight = 1.2

    def _evaluate(self, signal: ChartSignal) -> Optional[StrategyResult]:
        ma5  = self._ind(signal, "ma5")
        ma20 = self._ind(signal, "ma20")
        vol_ratio = self._ind(signal, "vol_ratio")

        if any(v is None for v in [ma5, ma20, vol_ratio]):
            return None

        reasons: list[str] = []
        confidence = 0.0

        if ma5 > ma20:
            confidence += 0.5
            reasons.append(f"MA5({ma5:,.0f}) > MA20({ma20:,.0f}) 골든크로스")

            if vol_ratio >= 1.5:
                confidence += 0.2
                reasons.append(f"거래량 20일 평균 대비 {vol_ratio:.1f}배 증가")

            ma60 = self._ind(signal, "ma60")
            if ma60 and ma5 > ma60:
                confidence += 0.1
                reasons.append("MA60 상방 위치 (중기 상승 추세)")

            ma_bull = self._ind(signal, "ma_aligned_bull")
            if ma_bull:
                confidence += 0.15
                reasons.append("MA 정배열 확인")

            return self._result(self.name, "BUY", confidence, reasons, self.weight)

        elif ma5 < ma20:
            confidence += 0.5
            reasons.append(f"MA5({ma5:,.0f}) < MA20({ma20:,.0f}) 데드크로스")

            if vol_ratio >= 1.5:
                confidence += 0.2
                reasons.append(f"거래량 20일 평균 대비 {vol_ratio:.1f}배 증가")

            ma_bear = self._ind(signal, "ma_aligned_bear")
            if ma_bear:
                confidence += 0.15
                reasons.append("MA 역배열 확인")

            return self._result(self.name, "SELL", confidence, reasons, self.weight)

        return None


# ---------------------------------------------------------------------------
# 전략 2: RSI 과매도 반등
# ---------------------------------------------------------------------------

class RSIReversalStrategy(BaseStrategy):
    """
    RSI 과매도/과매수 + MACD 방향 확인.
    - RSI < 30 + MACD 히스토그램 상승 전환 → BUY
    - RSI > 70 + MACD 히스토그램 하락 전환 → SELL
    """

    name = "RSI반전"
    weight = 1.0

    def _evaluate(self, signal: ChartSignal) -> Optional[StrategyResult]:
        rsi       = self._ind(signal, "rsi")
        macd_hist = self._ind(signal, "macd_hist")
        macd      = self._ind(signal, "macd")
        macd_sig  = self._ind(signal, "macd_signal")

        if any(v is None for v in [rsi, macd_hist, macd, macd_sig]):
            return None

        reasons: list[str] = []
        confidence = 0.0

        if rsi < 30:
            confidence += 0.5
            reasons.append(f"RSI {rsi:.1f} — 과매도 구간")

            if macd_hist > 0:
                confidence += 0.25
                reasons.append("MACD 히스토그램 양전환 (모멘텀 회복)")
            elif macd > macd_sig:
                confidence += 0.15
                reasons.append("MACD 시그널선 상향 돌파")

            stoch_k = self._ind(signal, "stoch_k")
            if stoch_k is not None and stoch_k < 20:
                confidence += 0.1
                reasons.append(f"Stoch %K {stoch_k:.1f} — 과매도 동반")

            return self._result(self.name, "BUY", confidence, reasons, self.weight)

        elif rsi > 70:
            confidence += 0.5
            reasons.append(f"RSI {rsi:.1f} — 과매수 구간")

            if macd_hist < 0:
                confidence += 0.25
                reasons.append("MACD 히스토그램 음전환 (모멘텀 약화)")
            elif macd < macd_sig:
                confidence += 0.15
                reasons.append("MACD 시그널선 하향 이탈")

            stoch_k = self._ind(signal, "stoch_k")
            if stoch_k is not None and stoch_k > 80:
                confidence += 0.1
                reasons.append(f"Stoch %K {stoch_k:.1f} — 과매수 동반")

            return self._result(self.name, "SELL", confidence, reasons, self.weight)

        return None


# ---------------------------------------------------------------------------
# 전략 3: 볼린저밴드 돌파
# ---------------------------------------------------------------------------

class BollingerBreakoutStrategy(BaseStrategy):
    """
    볼린저밴드 수축 후 돌파.
    - 밴드폭 수축(낮은 bb_width) 후 상단 돌파 + 거래량 → BUY
    - 밴드폭 수축 후 하단 이탈 + 거래량 → SELL
    """

    name = "BB돌파"
    weight = 1.1

    # 밴드폭 수축 기준 (낮을수록 수축)
    _SQUEEZE_THRESHOLD = 0.08

    def _evaluate(self, signal: ChartSignal) -> Optional[StrategyResult]:
        close     = self._ind(signal, "close")
        bb_upper  = self._ind(signal, "bb_upper")
        bb_lower  = self._ind(signal, "bb_lower")
        bb_width  = self._ind(signal, "bb_width")
        bb_pct    = self._ind(signal, "bb_pct")
        vol_ratio = self._ind(signal, "vol_ratio")

        if any(v is None for v in [close, bb_upper, bb_lower, bb_width, bb_pct, vol_ratio]):
            return None

        reasons: list[str] = []
        confidence = 0.0
        is_squeeze = bb_width < self._SQUEEZE_THRESHOLD

        # 상단 돌파 (bb_pct > 1.0 = 상단 초과)
        if bb_pct > 0.9 and close >= bb_upper * 0.995:
            confidence += 0.45
            reasons.append(f"볼린저밴드 상단({bb_upper:,.0f}) 돌파")

            if is_squeeze:
                confidence += 0.2
                reasons.append(f"밴드폭 수축({bb_width:.3f}) 후 돌파 — 강한 신호")

            if vol_ratio >= 1.5:
                confidence += 0.2
                reasons.append(f"거래량 {vol_ratio:.1f}배 동반 돌파")

            return self._result(self.name, "BUY", confidence, reasons, self.weight)

        # 하단 이탈 (bb_pct < 0.0 = 하단 하회)
        elif bb_pct < 0.1 and close <= bb_lower * 1.005:
            confidence += 0.45
            reasons.append(f"볼린저밴드 하단({bb_lower:,.0f}) 이탈")

            if is_squeeze:
                confidence += 0.2
                reasons.append(f"밴드폭 수축({bb_width:.3f}) 후 이탈 — 강한 신호")

            if vol_ratio >= 1.5:
                confidence += 0.2
                reasons.append(f"거래량 {vol_ratio:.1f}배 동반 이탈")

            return self._result(self.name, "SELL", confidence, reasons, self.weight)

        return None


# ---------------------------------------------------------------------------
# 전략 4: 캔들 패턴 + 지지/저항 확인
# ---------------------------------------------------------------------------

class CandlePatternStrategy(BaseStrategy):
    """
    캔들 패턴 + 지지/저항 근접 확인.
    - 지지선 근처 반전 패턴 → BUY
    - 저항선 근처 반전 패턴 → SELL
    """

    name = "캔들패턴"
    weight = 0.8

    _PROXIMITY_PCT = 0.015   # 지지/저항 근접 기준 (1.5%)

    _BULLISH_PATTERNS = {"망치형(양봉)", "망치형(음봉)", "상승장악형", "장대양봉"}
    _BEARISH_PATTERNS = {"역망치형", "하락장악형", "장대음봉", "도지"}

    def _evaluate(self, signal: ChartSignal) -> Optional[StrategyResult]:
        close    = self._ind(signal, "close")
        support  = signal.support
        resistance = signal.resistance
        patterns = signal.patterns

        if not patterns or close is None:
            return None

        reasons: list[str] = []
        confidence = 0.0
        bullish = [p for p in patterns if p in self._BULLISH_PATTERNS]
        bearish = [p for p in patterns if p in self._BEARISH_PATTERNS]

        # 지지선 근처 (현재가가 지지선의 1.5% 이내)
        near_support = support > 0 and abs(close - support) / support <= self._PROXIMITY_PCT

        # 저항선 근처
        near_resistance = resistance > 0 and abs(close - resistance) / resistance <= self._PROXIMITY_PCT

        if bullish and near_support:
            confidence += 0.4 + 0.1 * len(bullish)
            reasons.append(f"상승 패턴 감지: {', '.join(bullish)}")
            reasons.append(f"지지선({support:,.0f}) 근처 반등 신호")
            return self._result(self.name, "BUY", confidence, reasons, self.weight)

        elif bearish and near_resistance:
            confidence += 0.4 + 0.1 * len(bearish)
            reasons.append(f"하락 패턴 감지: {', '.join(bearish)}")
            reasons.append(f"저항선({resistance:,.0f}) 근처 매도 신호")
            return self._result(self.name, "SELL", confidence, reasons, self.weight)

        return None


# ---------------------------------------------------------------------------
# 전략 5: 시장 구조 분석 (BOS / CHoCH / 유동성 트랩)
# ---------------------------------------------------------------------------

class MarketStructureStrategy(BaseStrategy):
    """
    시장 구조(Market Structure) 기반 매매 신호.

    우선순위:
    1. 유동성 스윕(Liquidity Sweep) — Smart Money 트랩 활용
    2. CHoCH(Change of Character)  — 추세 전환
    3. BOS(Break of Structure)     — 추세 지속
    Choppy 시장은 필터링(None 반환).
    와이코프 Effort vs Result로 신뢰도 보정.
    """

    name   = "시장구조"
    weight = 1.5

    _STATE_LABEL: dict[str, str] = {
        "UPTREND":   "상승추세",
        "DOWNTREND": "하락추세",
        "RANGING":   "횡보",
        "CHOPPY":    "혼조",
    }

    def _evaluate(self, signal: ChartSignal) -> Optional[StrategyResult]:
        ms = signal.market_structure
        if ms is None:
            return None

        if ms.market_state == MarketState.CHOPPY:
            return None

        reasons:    list[str]        = []
        confidence: float            = 0.0
        direction:  Optional[Direction] = None

        # 1. 유동성 스윕 (꼬리 이탈 후 종가 복귀 → 역방향 진입)
        recent_sweeps = [sw for sw in ms.liquidity_sweeps if sw.close_reverted]
        if recent_sweeps:
            sw = recent_sweeps[-1]
            direction  = sw.direction
            confidence = 0.70
            pool_side  = "저항" if sw.is_high else "지지"
            reasons.append(
                f"{pool_side} 유동성 스윕 감지 ({sw.pool_price:,.0f}) — "
                f"꼬리 이탈 후 종가 복귀 → {sw.direction} 트랩 해소"
            )

        # 2. CHoCH — 추세 전환 (유동성 스윕보다 약하면 덮어씀)
        if ms.last_choch is not None:
            choch     = ms.last_choch
            choch_c   = 0.75 if choch.volume_confirmed else 0.55
            if direction is None or choch_c > confidence:
                direction  = choch.direction
                confidence = choch_c
                vol_note   = "거래량 동반" if choch.volume_confirmed else "거래량 미동반"
                reasons    = [
                    f"CHoCH({choch.direction}) 확인 — {vol_note}",
                    f"돌파가: {choch.price:,.0f} / 기준 스윙: {choch.broken_swing_price:,.0f}",
                ]

        # 3. BOS — 추세 지속 (CHoCH 없을 때만 적용)
        elif ms.last_bos is not None and direction is None:
            bos = ms.last_bos
            if bos.volume_confirmed:
                state_str  = self._STATE_LABEL.get(ms.market_state.value, ms.market_state.value)
                direction  = bos.direction
                confidence = 0.65
                reasons    = [
                    f"BOS({bos.direction}) 확인 — {state_str} 지속",
                    f"거래량 동반 구조 돌파: {bos.price:,.0f}",
                ]

        if direction is None or confidence < 0.55:
            return None

        # 4. Effort vs Result 보정
        if ms.effort_result == EffortResult.ABSORPTION:
            confidence -= 0.10
            reasons.append("흡수 캔들: 대량 거래량 대비 이동 작음 — 반전 가능성 경고")
        elif ms.effort_result == EffortResult.TRAP:
            confidence -= 0.08
            reasons.append("트랩 캔들: 거래량 없는 큰 이동 — 지속 불가능")
        elif ms.effort_result == EffortResult.TREND_CONFIRM:
            confidence = min(confidence + 0.05, 1.0)
            reasons.append("추세 확인 캔들: 대량 거래량 + 강한 몸통")

        if confidence < 0.55:
            return None

        return self._result(self.name, direction, round(confidence, 4), reasons, self.weight)


# ---------------------------------------------------------------------------
# Strategy Agent
# ---------------------------------------------------------------------------

class StrategyAgent:
    """
    등록된 전략을 독립 실행 후 가중 평균으로 신호 통합.

    사용 예:
        agent = StrategyAgent()
        signal = agent.run("005930", chart_signal)
        signals = agent.run_multi("005930", {"D": cs_d, "60": cs_60})
    """

    def __init__(self) -> None:
        self.strategies: list[BaseStrategy] = [
            GoldenCrossStrategy(),
            RSIReversalStrategy(),
            BollingerBreakoutStrategy(),
            CandlePatternStrategy(),
            MarketStructureStrategy(),
        ]

    def run(
        self,
        ticker: str,
        chart_signal: ChartSignal,
    ) -> TradeSignal:
        """
        단일 ChartSignal에 전략 적용 → TradeSignal 반환.
        모든 전략이 HOLD이거나 신호 없으면 HOLD 반환.
        """
        results = [r for s in self.strategies if (r := s.run(chart_signal)) is not None]

        if not results:
            return self._hold(ticker, chart_signal)

        trade_signal = self._aggregate(ticker, chart_signal, results)
        logger.info(
            "[Strategy] %s → %s (confidence=%.2f) | 전략 %d개 실행",
            ticker, trade_signal.signal, trade_signal.confidence, len(results),
        )
        return trade_signal

    def run_multi(
        self,
        ticker: str,
        chart_signals: dict[str, ChartSignal],
        primary_tf: str = "D",
    ) -> TradeSignal:
        """
        멀티 타임프레임 신호 통합.
        primary_tf 가중치 2배 적용.
        """
        all_results: list[StrategyResult] = []

        for tf, cs in chart_signals.items():
            tf_results = [r for s in self.strategies if (r := s.run(cs)) is not None]
            if tf == primary_tf:
                # 주 타임프레임 가중치 2배
                for r in tf_results:
                    r.weight *= 2.0
            all_results.extend(tf_results)

        if not all_results:
            primary_cs = chart_signals.get(primary_tf) or next(iter(chart_signals.values()))
            return self._hold(ticker, primary_cs)

        primary_cs = chart_signals.get(primary_tf) or next(iter(chart_signals.values()))
        trade_signal = self._aggregate(ticker, primary_cs, all_results)
        logger.info(
            "[Strategy] %s 멀티TF → %s (confidence=%.2f)",
            ticker, trade_signal.signal, trade_signal.confidence,
        )
        return trade_signal

    # ------------------------------------------------------------------
    # 신호 통합
    # ------------------------------------------------------------------

    def _aggregate(
        self,
        ticker: str,
        chart_signal: ChartSignal,
        results: list[StrategyResult],
    ) -> TradeSignal:
        """
        가중 평균으로 최종 방향 결정.
        - BUY/SELL 가중합 비교
        - 우세한 방향 confidence가 0.6 미만이면 HOLD
        """
        buy_score  = sum(r.confidence * r.weight for r in results if r.direction == "BUY")
        sell_score = sum(r.confidence * r.weight for r in results if r.direction == "SELL")
        total_weight = sum(r.weight for r in results)

        if total_weight == 0:
            return self._hold(ticker, chart_signal)

        if buy_score >= sell_score:
            direction: Direction = "BUY"
            raw_confidence = buy_score / total_weight
            winning = [r for r in results if r.direction == "BUY"]
        else:
            direction = "SELL"
            raw_confidence = sell_score / total_weight
            winning = [r for r in results if r.direction == "SELL"]

        # confidence 0.6 미만 → HOLD
        if raw_confidence < 0.6:
            return self._hold(ticker, chart_signal)

        # 근거 통합 (중복 제거, 전략명 포함)
        all_reasons: list[str] = []
        seen: set[str] = set()
        for r in winning:
            for reason in r.reasons:
                if reason not in seen:
                    all_reasons.append(reason)
                    seen.add(reason)

        close = chart_signal.indicators.get("close", 0.0)
        atr   = chart_signal.indicators.get("atr") or 0.0

        # 목표가 / 손절가: ATR 기반
        if direction == "BUY":
            target = close + atr * 2.0
            stop   = close - atr * 1.5
        else:
            target = close - atr * 2.0
            stop   = close + atr * 1.5

        strategy_names = " + ".join(dict.fromkeys(r.strategy_name for r in winning))

        return TradeSignal(
            ticker=ticker,
            signal=direction,
            confidence=round(raw_confidence, 4),
            strategy_name=strategy_names,
            reasons=all_reasons,
            timeframe=chart_signal.timeframe,
            timestamp=datetime.now(),
            price=float(close),
            target_price=round(target, 0),
            stop_loss=round(stop, 0),
        )

    @staticmethod
    def _hold(ticker: str, chart_signal: ChartSignal) -> TradeSignal:
        return TradeSignal(
            ticker=ticker,
            signal="HOLD",
            confidence=0.0,
            strategy_name="없음",
            reasons=[],
            timeframe=chart_signal.timeframe,
            timestamp=datetime.now(),
            price=float(chart_signal.indicators.get("close", 0.0)),
        )
