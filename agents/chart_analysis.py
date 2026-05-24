"""
Chart Analysis Agent
OHLCV DataFrame → 기술적 지표 계산 → ChartSignal 반환.

pandas-ta 사용 (ta-lib 없이도 동작).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

import numpy as np
import pandas as pd
import pandas_ta as ta

from models import (
    ChartSignal,
    MarketState, SwingType, EffortResult, BreakType,
    SwingPoint, StructureBreak, LiquidityPool, LiquiditySweep,
    MarketStructureResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------

class InsufficientDataError(Exception):
    """지표 계산에 필요한 최소 데이터 미달."""


# ---------------------------------------------------------------------------
# 최소 데이터 요구 수
# ---------------------------------------------------------------------------

MIN_BARS: dict[str, int] = {
    "D": 130,   # MA120 계산 위해 최소 130봉
    "60": 60,
    "30": 60,
    "15": 60,
    "5": 60,
    "3": 60,
    "1": 60,
}


# ---------------------------------------------------------------------------
# 지지/저항 계산
# ---------------------------------------------------------------------------

def _find_support_resistance(
    df: pd.DataFrame,
    window: int = 20,
    current_price: float = 0.0,
) -> tuple[float, float]:
    """
    최근 window 봉에서 로컬 저점/고점을 찾아 지지/저항 반환.
    현재가 기준 가장 가까운 아래 = 지지, 위 = 저항.
    """
    recent = df.tail(window)
    price = current_price or float(df["close"].iloc[-1])

    lows = recent["low"].values
    highs = recent["high"].values

    # 로컬 저점/고점: 앞뒤 봉보다 낮은/높은 봉
    local_lows = []
    local_highs = []
    for i in range(1, len(lows) - 1):
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            local_lows.append(float(lows[i]))
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            local_highs.append(float(highs[i]))

    support = max((v for v in local_lows if v < price), default=float(recent["low"].min()))
    resistance = min((v for v in local_highs if v > price), default=float(recent["high"].max()))

    return support, resistance


# ---------------------------------------------------------------------------
# 캔들 패턴 감지
# ---------------------------------------------------------------------------

def _detect_candle_patterns(df: pd.DataFrame) -> list[str]:
    """
    최근 3봉 기준 주요 캔들 패턴 감지.
    pandas-ta candlestick 패턴 활용.
    """
    patterns: list[str] = []
    if len(df) < 3:
        return patterns

    recent = df.tail(3).copy()
    o, h, l, c = (
        recent["open"].values,
        recent["high"].values,
        recent["low"].values,
        recent["close"].values,
    )

    body = abs(c - o)
    full_range = h - l
    prev_body = abs(c[-2] - o[-2])

    # 도지 (Doji): 몸통이 전체 범위의 10% 미만
    if full_range[-1] > 0 and body[-1] / full_range[-1] < 0.1:
        patterns.append("도지")

    # 망치 (Hammer): 아래 꼬리 > 몸통 * 2, 위 꼬리 짧음
    lower_shadow = min(o[-1], c[-1]) - l[-1]
    upper_shadow = h[-1] - max(o[-1], c[-1])
    if body[-1] > 0 and lower_shadow > body[-1] * 2 and upper_shadow < body[-1] * 0.5:
        if c[-1] > o[-1]:
            patterns.append("망치형(양봉)")
        else:
            patterns.append("망치형(음봉)")

    # 역망치 / 유성형
    if body[-1] > 0 and upper_shadow > body[-1] * 2 and lower_shadow < body[-1] * 0.5:
        patterns.append("역망치형")

    # 장악형 (Engulfing)
    if len(df) >= 2:
        prev_o, prev_c = o[-2], c[-2]
        curr_o, curr_c = o[-1], c[-1]
        # 상승 장악형: 이전 음봉을 현재 양봉이 완전히 감쌈
        if prev_c < prev_o and curr_c > curr_o:
            if curr_o <= prev_c and curr_c >= prev_o:
                patterns.append("상승장악형")
        # 하락 장악형
        if prev_c > prev_o and curr_c < curr_o:
            if curr_o >= prev_c and curr_c <= prev_o:
                patterns.append("하락장악형")

    # 장대양봉 / 장대음봉: 몸통 > 직전 봉 몸통 * 2
    if prev_body > 0 and body[-1] > prev_body * 2:
        if c[-1] > o[-1]:
            patterns.append("장대양봉")
        else:
            patterns.append("장대음봉")

    return patterns


# ---------------------------------------------------------------------------
# 시장 구조 분석 (Market Structure Analysis)
# ---------------------------------------------------------------------------

_MS_LOOKBACK = 5   # 스윙 포인트 확인에 필요한 양쪽 봉 수
_MS_MIN_BARS = 30  # 시장 구조 분석 최소 데이터 수


def _is_choppy(df: pd.DataFrame, window: int = 20) -> bool:
    """
    Choppy 시장 필터:
    - ATR 대비 실질 변위가 작고 (가격이 제자리)
    - 평균 몸통 비율이 낮을 때 (꼬리 지배적) True 반환.
    """
    if len(df) < window:
        return False
    recent = df.tail(window)
    close = recent["close"].astype(float).values
    high  = recent["high"].astype(float).values
    low   = recent["low"].astype(float).values
    open_ = recent["open"].astype(float).values

    avg_range   = float(np.mean(high - low))
    displacement = abs(float(close[-1]) - float(close[0]))

    if avg_range > 0 and displacement < avg_range * 1.2:
        full_ranges = np.maximum(high - low, 1e-9)
        avg_body_ratio = float(np.mean(np.abs(close - open_) / full_ranges))
        if avg_body_ratio < 0.35:
            return True
    return False


def _find_swing_points_raw(
    highs: np.ndarray,
    lows: np.ndarray,
    lookback: int = _MS_LOOKBACK,
) -> list[tuple[int, float, bool]]:
    """
    각 인덱스의 양쪽 lookback 봉을 모두 이기면 스윙 포인트로 인정.
    Returns: (index, price, is_high) 리스트
    """
    n = len(highs)
    result: list[tuple[int, float, bool]] = []
    for i in range(lookback, n - lookback):
        is_sh = all(
            highs[i] > highs[i - k] and highs[i] > highs[i + k]
            for k in range(1, lookback + 1)
        )
        is_sl = all(
            lows[i] < lows[i - k] and lows[i] < lows[i + k]
            for k in range(1, lookback + 1)
        )
        if is_sh:
            result.append((i, float(highs[i]), True))
        if is_sl:
            result.append((i, float(lows[i]), False))
    return result


def _classify_swing_types(
    raw_swings: list[tuple[int, float, bool]],
) -> list[SwingPoint]:
    """HH/HL/LH/LL 분류: 고점과 저점을 각각 시계열 순으로 비교."""
    sh_list = [(i, p) for i, p, h in raw_swings if h]
    sl_list = [(i, p) for i, p, h in raw_swings if not h]

    result: list[SwingPoint] = []

    for j, (i, p) in enumerate(sh_list):
        st = SwingType.HH if (j == 0 or p > sh_list[j - 1][1]) else SwingType.LH
        result.append(SwingPoint(index=i, price=p, swing_type=st, is_high=True))

    for j, (i, p) in enumerate(sl_list):
        st = SwingType.HL if (j == 0 or p > sl_list[j - 1][1]) else SwingType.LL
        result.append(SwingPoint(index=i, price=p, swing_type=st, is_high=False))

    result.sort(key=lambda x: x.index)
    return result


def _classify_market_state_from_swings(
    swing_points: list[SwingPoint],
    df: pd.DataFrame,
) -> MarketState:
    """최근 스윙 포인트 패턴 + Choppy 필터로 시장 상태 결정."""
    if _is_choppy(df):
        return MarketState.CHOPPY
    if len(swing_points) < 4:
        return MarketState.RANGING

    recent   = swing_points[-8:]
    h_types  = [s.swing_type for s in recent if s.is_high]
    l_types  = [s.swing_type for s in recent if not s.is_high]

    if len(h_types) < 2 or len(l_types) < 2:
        return MarketState.RANGING

    hh = h_types.count(SwingType.HH)
    lh = h_types.count(SwingType.LH)
    hl = l_types.count(SwingType.HL)
    ll = l_types.count(SwingType.LL)

    if hh > lh and hl > ll:
        return MarketState.UPTREND
    if ll > hl and lh > hh:
        return MarketState.DOWNTREND
    return MarketState.RANGING


def _detect_structure_breaks(
    df: pd.DataFrame,
    swing_points: list[SwingPoint],
    market_state: MarketState,
    vol_ma20: Optional[float],
) -> list[StructureBreak]:
    """
    종가 기준 마지막 확정 스윙 포인트 돌파 감지.
    - 추세 방향 돌파 → BOS
    - 추세 역방향 돌파 → CHoCH
    """
    close  = df["close"].astype(float).values
    volume = df["volume"].astype(float).values
    n = len(close)
    if n < 2 or not swing_points:
        return []

    last_close = close[-1]
    last_vol   = volume[-1]
    vol_avg    = vol_ma20 if (vol_ma20 and vol_ma20 > 0) else float(np.mean(volume))
    vol_ok     = bool(last_vol > vol_avg * 1.2)

    swing_highs = [s for s in swing_points if s.is_high and s.index < n - 1]
    swing_lows  = [s for s in swing_points if not s.is_high and s.index < n - 1]

    breaks: list[StructureBreak] = []

    if swing_highs and last_close > swing_highs[-1].price:
        bt = BreakType.CHOCH if market_state == MarketState.DOWNTREND else BreakType.BOS
        breaks.append(StructureBreak(
            bar_index=n - 1,
            break_type=bt,
            direction="BUY",
            price=last_close,
            broken_swing_price=swing_highs[-1].price,
            volume=last_vol,
            volume_confirmed=vol_ok,
        ))

    if swing_lows and last_close < swing_lows[-1].price:
        bt = BreakType.CHOCH if market_state == MarketState.UPTREND else BreakType.BOS
        breaks.append(StructureBreak(
            bar_index=n - 1,
            break_type=bt,
            direction="SELL",
            price=last_close,
            broken_swing_price=swing_lows[-1].price,
            volume=last_vol,
            volume_confirmed=vol_ok,
        ))

    return breaks


def _find_liquidity_pools(
    swing_points: list[SwingPoint],
    tolerance_pct: float = 0.005,
) -> list[LiquidityPool]:
    """
    가격이 거의 동일한 스윙 포인트 2개 이상 → 유동성 풀로 지정.
    Equal Highs / Equal Lows 개념.
    """
    pools: list[LiquidityPool] = []
    for is_high in (True, False):
        group = [s for s in swing_points if s.is_high == is_high]
        if len(group) < 2:
            continue
        used = [False] * len(group)
        for i in range(len(group)):
            if used[i]:
                continue
            cluster = [group[i]]
            used[i] = True
            ref = group[i].price
            for j in range(i + 1, len(group)):
                if not used[j] and abs(group[j].price - ref) / max(ref, 1) <= tolerance_pct:
                    cluster.append(group[j])
                    used[j] = True
            if len(cluster) >= 2:
                avg_price = sum(s.price for s in cluster) / len(cluster)
                pools.append(LiquidityPool(
                    price=round(avg_price, 0),
                    touch_count=len(cluster),
                    is_high=is_high,
                ))
    return pools


def _detect_liquidity_sweeps(
    df: pd.DataFrame,
    liq_pools: list[LiquidityPool],
    lookback: int = 3,
) -> list[LiquiditySweep]:
    """
    꼬리(Wick)만 유동성 풀 경계를 이탈하고 종가가 복귀한 경우 → Sweep 감지.
    Smart Money의 손절 사냥 후 역방향 진입 기회.
    """
    sweeps: list[LiquiditySweep] = []
    if len(df) < lookback + 1 or not liq_pools:
        return sweeps

    window = df.tail(lookback + 1)
    highs  = window["high"].astype(float).values
    lows   = window["low"].astype(float).values
    closes = window["close"].astype(float).values
    offset = len(df) - len(closes)

    for k in range(1, len(closes)):
        h, l, c = highs[k], lows[k], closes[k]
        for pool in liq_pools:
            p = pool.price
            if pool.is_high and h > p > c:
                sweeps.append(LiquiditySweep(
                    bar_index=offset + k,
                    pool_price=p,
                    is_high=True,
                    direction="SELL",
                    close_reverted=True,
                ))
            elif not pool.is_high and l < p < c:
                sweeps.append(LiquiditySweep(
                    bar_index=offset + k,
                    pool_price=p,
                    is_high=False,
                    direction="BUY",
                    close_reverted=True,
                ))
    return sweeps


def _classify_effort_result(
    body: float,
    volume: float,
    avg_body: float,
    avg_volume: float,
) -> EffortResult:
    """와이코프 Effort vs Result: 거래량과 캔들 몸통 크기로 4가지 상태 분류."""
    high_vol  = volume > avg_volume * 1.3
    long_body = body   > avg_body   * 1.3

    if high_vol and long_body:
        return EffortResult.TREND_CONFIRM
    if high_vol:
        return EffortResult.ABSORPTION
    if long_body:
        return EffortResult.TRAP
    return EffortResult.EXHAUSTION


def _analyze_market_structure(
    df: pd.DataFrame,
    vol_ma20: Optional[float],
) -> Optional[MarketStructureResult]:
    """OHLCV DataFrame → MarketStructureResult. 데이터 부족 시 None."""
    if len(df) < _MS_MIN_BARS:
        return None

    highs  = df["high"].astype(float).values
    lows   = df["low"].astype(float).values
    close  = df["close"].astype(float).values
    open_  = df["open"].astype(float).values
    volume = df["volume"].astype(float).values

    raw_swings   = _find_swing_points_raw(highs, lows)
    if not raw_swings:
        return None

    swing_points  = _classify_swing_types(raw_swings)
    market_state  = _classify_market_state_from_swings(swing_points, df)
    struct_breaks = _detect_structure_breaks(df, swing_points, market_state, vol_ma20)
    liq_pools     = _find_liquidity_pools(swing_points)
    liq_sweeps    = _detect_liquidity_sweeps(df, liq_pools)

    bodies    = np.abs(close - open_)
    avg_body  = float(np.mean(bodies[-20:]))
    avg_vol   = vol_ma20 if (vol_ma20 and vol_ma20 > 0) else float(np.mean(volume[-20:]))
    effort    = _classify_effort_result(float(bodies[-1]), float(volume[-1]), avg_body, avg_vol)

    last_bos   = next((b for b in reversed(struct_breaks) if b.break_type == BreakType.BOS), None)
    last_choch = next((b for b in reversed(struct_breaks) if b.break_type == BreakType.CHOCH), None)

    return MarketStructureResult(
        market_state=market_state,
        swing_points=swing_points,
        structure_breaks=struct_breaks,
        liquidity_pools=liq_pools,
        liquidity_sweeps=liq_sweeps,
        last_bos=last_bos,
        last_choch=last_choch,
        effort_result=effort,
    )


# ---------------------------------------------------------------------------
# Chart Analysis Agent
# ---------------------------------------------------------------------------

class ChartAnalysisAgent:
    """
    OHLCV DataFrame → 기술적 지표 계산 → ChartSignal 반환.

    사용 예:
        agent = ChartAnalysisAgent()
        signal = agent.analyze("005930", df, timeframe="D")
        multi = agent.analyze_multi("005930", {"D": df_d, "60": df_60})
    """

    def analyze(
        self,
        ticker: str,
        df: pd.DataFrame,
        timeframe: str = "D",
    ) -> Optional[ChartSignal]:
        """
        단일 타임프레임 분석.

        Args:
            ticker:    종목코드
            df:        OHLCV DataFrame (columns: date, open, high, low, close, volume)
            timeframe: 'D', '60', '30', '15', '5' 등

        Returns:
            ChartSignal, 데이터 부족 시 None
        """
        min_bars = MIN_BARS.get(timeframe, 60)
        if len(df) < min_bars:
            logger.warning(
                "%s [%s] 데이터 부족: %d봉 (최소 %d봉 필요)",
                ticker, timeframe, len(df), min_bars,
            )
            return None

        df = df.copy().reset_index(drop=True)

        try:
            indicators = self._calc_indicators(df)
        except Exception as e:
            logger.error("%s [%s] 지표 계산 오류: %s", ticker, timeframe, e)
            return None

        patterns = _detect_candle_patterns(df)
        current_price = float(df["close"].iloc[-1])
        support, resistance = _find_support_resistance(df, current_price=current_price)
        ms_result = _analyze_market_structure(df, indicators.get("vol_ma20"))

        logger.info(
            "%s [%s] 분석 완료 — 패턴: %s | 지지: %.0f | 저항: %.0f | 시장상태: %s",
            ticker, timeframe, patterns or "없음", support, resistance,
            ms_result.market_state.value if ms_result else "N/A",
        )

        return ChartSignal(
            ticker=ticker,
            timeframe=timeframe,
            timestamp=datetime.now(tz=KST),
            indicators=indicators,
            patterns=patterns,
            support=support,
            resistance=resistance,
            market_structure=ms_result,
        )

    def analyze_multi(
        self,
        ticker: str,
        ohlcv_map: dict[str, pd.DataFrame],
    ) -> dict[str, ChartSignal]:
        """
        멀티 타임프레임 분석.

        Args:
            ohlcv_map: {timeframe: DataFrame}

        Returns:
            {timeframe: ChartSignal} — 분석 실패한 타임프레임은 제외
        """
        results: dict[str, ChartSignal] = {}
        for tf, df in ohlcv_map.items():
            signal = self.analyze(ticker, df, timeframe=tf)
            if signal is not None:
                results[tf] = signal
        return results

    # ------------------------------------------------------------------
    # 지표 계산
    # ------------------------------------------------------------------

    def _calc_indicators(self, df: pd.DataFrame) -> dict:
        """
        전체 지표 계산 후 최신 값 딕셔너리로 반환.
        NaN 값은 None으로 변환.
        """
        close = df["close"].astype(float)
        high  = df["high"].astype(float)
        low   = df["low"].astype(float)
        volume = df["volume"].astype(float)

        ind: dict = {}

        # --- 이동평균 ---
        for p in [5, 20, 60, 120]:
            if len(df) >= p:
                ind[f"ma{p}"] = self._last(close.rolling(p).mean())
            else:
                ind[f"ma{p}"] = None

        # --- EMA ---
        for p in [12, 26]:
            ind[f"ema{p}"] = self._last(close.ewm(span=p, adjust=False).mean())

        # --- MACD (12, 26, 9) ---
        macd_df = ta.macd(close, fast=12, slow=26, signal=9)
        ind["macd"]        = self._last(macd_df["MACD_12_26_9"])
        ind["macd_signal"] = self._last(macd_df["MACDs_12_26_9"])
        ind["macd_hist"]   = self._last(macd_df["MACDh_12_26_9"])

        # --- RSI (14) ---
        ind["rsi"] = self._last(ta.rsi(close, length=14))

        # --- Stochastic (5, 3, 3) ---
        stoch_df = ta.stoch(high, low, close, k=5, d=3, smooth_k=3)
        ind["stoch_k"] = self._last(stoch_df["STOCHk_5_3_3"])
        ind["stoch_d"] = self._last(stoch_df["STOCHd_5_3_3"])

        # --- 볼린저밴드 (20, 2) ---
        bb_df = ta.bbands(close, length=20, std=2)
        ind["bb_upper"] = self._last(bb_df["BBU_20_2.0_2.0"])
        ind["bb_mid"]   = self._last(bb_df["BBM_20_2.0_2.0"])
        ind["bb_lower"] = self._last(bb_df["BBL_20_2.0_2.0"])
        ind["bb_width"] = self._last(bb_df["BBB_20_2.0_2.0"])
        ind["bb_pct"]   = self._last(bb_df["BBP_20_2.0_2.0"])

        # --- ATR (14) ---
        ind["atr"] = self._last(ta.atr(high, low, close, length=14))

        # --- OBV ---
        ind["obv"] = self._last(ta.obv(close, volume))

        # --- 거래량 이동평균 ---
        for p in [5, 20]:
            if len(df) >= p:
                ind[f"vol_ma{p}"] = self._last(volume.rolling(p).mean())
            else:
                ind[f"vol_ma{p}"] = None

        # --- 파생 지표 (Strategy Agent에서 활용) ---
        ind["close"]  = float(close.iloc[-1])
        ind["volume"] = float(volume.iloc[-1])

        # 거래량 비율 (현재 거래량 / 20일 평균)
        if ind["vol_ma20"] and ind["vol_ma20"] > 0:
            ind["vol_ratio"] = round(ind["volume"] / ind["vol_ma20"], 2)
        else:
            ind["vol_ratio"] = None

        # MA 배열 상태 (정배열 여부)
        vals = [ind.get(f"ma{p}") for p in [5, 20, 60, 120]]
        if all(v is not None for v in vals):
            ind["ma_aligned_bull"] = all(
                vals[i] > vals[i + 1] for i in range(len(vals) - 1)
            )
            ind["ma_aligned_bear"] = all(
                vals[i] < vals[i + 1] for i in range(len(vals) - 1)
            )
        else:
            ind["ma_aligned_bull"] = None
            ind["ma_aligned_bear"] = None

        return ind

    @staticmethod
    def _last(series: Optional[pd.Series]) -> Optional[float]:
        """Series 마지막 값 반환. NaN이면 None."""
        if series is None or series.empty:
            return None
        val = series.iloc[-1]
        return None if pd.isna(val) else round(float(val), 4)
