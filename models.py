from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal, Optional


@dataclass
class OHLCVBar:
    date: datetime
    open: int
    high: int
    low: int
    close: int
    volume: int


@dataclass
class CurrentPrice:
    ticker: str
    name: str
    price: int
    change: int
    change_rate: float
    volume: int
    timestamp: datetime


# ---------------------------------------------------------------------------
# 시장 구조 분석 모델 (Market Structure Analysis)
# ---------------------------------------------------------------------------

class MarketState(str, Enum):
    UPTREND   = "UPTREND"
    DOWNTREND = "DOWNTREND"
    RANGING   = "RANGING"
    CHOPPY    = "CHOPPY"


class SwingType(str, Enum):
    HH = "HH"  # Higher High
    HL = "HL"  # Higher Low
    LH = "LH"  # Lower High
    LL = "LL"  # Lower Low


class EffortResult(str, Enum):
    TREND_CONFIRM = "TREND_CONFIRM"  # High Vol + Long Body  — Real Money 참여
    ABSORPTION    = "ABSORPTION"     # High Vol + Short Body — 흡수, 반전 경고
    TRAP          = "TRAP"           # Low Vol  + Long Body  — 거래량 없는 이동
    EXHAUSTION    = "EXHAUSTION"     # Low Vol  + Short Body — 에너지 고갈


class BreakType(str, Enum):
    BOS   = "BOS"    # Break of Structure  — 추세 지속
    CHOCH = "CHoCH"  # Change of Character — 추세 전환


@dataclass
class SwingPoint:
    index:      int
    price:      float
    swing_type: SwingType
    is_high:    bool


@dataclass
class StructureBreak:
    bar_index:          int
    break_type:         BreakType
    direction:          Literal['BUY', 'SELL']
    price:              float
    broken_swing_price: float
    volume:             float
    volume_confirmed:   bool


@dataclass
class LiquidityPool:
    price:       float
    touch_count: int
    is_high:     bool  # True = 저항 클러스터, False = 지지 클러스터


@dataclass
class LiquiditySweep:
    bar_index:      int
    pool_price:     float
    is_high:        bool
    direction:      Literal['BUY', 'SELL']  # 스윕 후 예상 방향
    close_reverted: bool                     # 꼬리만 이탈, 종가는 경계 안쪽으로 복귀


@dataclass
class MarketStructureResult:
    market_state:     MarketState
    swing_points:     list[SwingPoint]
    structure_breaks: list[StructureBreak]
    liquidity_pools:  list[LiquidityPool]
    liquidity_sweeps: list[LiquiditySweep]
    last_bos:         Optional[StructureBreak]
    last_choch:       Optional[StructureBreak]
    effort_result:    EffortResult


# ---------------------------------------------------------------------------
# 차트 분석 신호
# ---------------------------------------------------------------------------

@dataclass
class ChartSignal:
    ticker: str
    timeframe: str          # 'D', '60', '30', '15', '5', '3', '1'
    timestamp: datetime
    indicators: dict
    patterns: list[str] = field(default_factory=list)
    support: float = 0.0
    resistance: float = 0.0
    market_structure: Optional[MarketStructureResult] = None


@dataclass
class TradeSignal:
    ticker: str
    signal: Literal['BUY', 'SELL', 'HOLD']
    confidence: float
    strategy_name: str
    reasons: list[str]
    timeframe: str
    timestamp: datetime
    price: float
    target_price: float = 0.0
    stop_loss: float = 0.0


@dataclass
class RiskCheckResult:
    signal: TradeSignal
    approved: bool
    block_reasons: list[str] = field(default_factory=list)
    risk_level: Literal['LOW', 'MEDIUM', 'HIGH'] = 'LOW'
    adjusted_confidence: float = 0.0


@dataclass
class AccountHolding:
    """계좌 보유종목 단건."""
    stock_code: str
    stock_name: str
    hold_qty: int           # 보유수량
    buy_avg_price: float    # 매입평균가 (pur_pric)
    pur_amount: float       # 매입금액 합계 (pur_amt, API 원본값)
    current_price: float    # 현재가
    eval_amount: float      # 평가금액
    pnl_amount: float       # 손익금액
    pnl_rate: float         # 손익률(%)


@dataclass
class SupplyDemandFinding:
    """수급 트렌드 분석 결과."""
    stock_code: str
    stock_name: str
    alerts: list[str]   # 경보 문자열 목록 (예: "기관 3일 연속 순매수")
    details: dict       # 세부 수치 (경보 유형 → 수치 딕셔너리)
