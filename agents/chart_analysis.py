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

import numpy as np
import pandas as pd
import pandas_ta as ta

from models import ChartSignal

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

        logger.info(
            "%s [%s] 분석 완료 — 패턴: %s | 지지: %.0f | 저항: %.0f",
            ticker, timeframe, patterns or "없음", support, resistance,
        )

        return ChartSignal(
            ticker=ticker,
            timeframe=timeframe,
            timestamp=datetime.now(),
            indicators=indicators,
            patterns=patterns,
            support=support,
            resistance=resistance,
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
