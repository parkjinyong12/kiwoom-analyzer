from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


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


@dataclass
class ChartSignal:
    ticker: str
    timeframe: str          # 'D', '60', '30', '15', '5', '3', '1'
    timestamp: datetime
    indicators: dict
    patterns: list[str] = field(default_factory=list)
    support: float = 0.0
    resistance: float = 0.0


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
class SupplyDemandFinding:
    """수급 트렌드 분석 결과."""
    stock_code: str
    stock_name: str
    alerts: list[str]   # 경보 문자열 목록 (예: "기관 3일 연속 순매수")
    details: dict       # 세부 수치 (경보 유형 → 수치 딕셔너리)
