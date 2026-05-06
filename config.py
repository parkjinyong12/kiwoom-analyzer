from __future__ import annotations

import os
from dataclasses import dataclass, field

# .env 로드
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())


@dataclass
class KiwoomConfig:
    # REST API 인증
    app_key: str = field(default_factory=lambda: os.environ.get("KIWOOM_APP_KEY", ""))
    app_secret: str = field(default_factory=lambda: os.environ.get("KIWOOM_APP_SECRET", ""))
    base_url: str = "https://api.kiwoom.com"

    # 계좌 정보
    acnt_no: str = field(default_factory=lambda: os.environ.get("KIWOOM_ACNT_NO", ""))
    acnt_pwd: str = field(default_factory=lambda: os.environ.get("KIWOOM_ACNT_PWD", ""))

    # 요청 제한
    tr_delay_seconds: float = 1.0       # 요청 간격 (초)
    tr_per_minute_limit: int = 60       # 분당 최대 요청 수

    # 데이터 수집 기본값
    default_daily_count: int = 200      # 일봉 기본 조회 수
    default_minute_count: int = 200     # 분봉 기본 조회 수


@dataclass
class SlackConfig:
    webhook_url: str = field(default_factory=lambda: os.environ.get("SLACK_WEBHOOK_URL", ""))
    channel: str = "#stock-agent-message"


@dataclass
class RiskConfig:
    min_confidence: float = 0.65
    signal_cooldown_hours: int = 4      # 동일 종목 동일 방향 알림 쿨다운
    market_drop_threshold: float = -2.0 # 코스피 급락 차단 기준 (%)


@dataclass
class SmtpConfig:
    user: str = field(default_factory=lambda: os.environ.get("SMTP_USER", ""))
    password: str = field(default_factory=lambda: os.environ.get("SMTP_PASSWORD", ""))
    host: str = "smtp.gmail.com"
    port: int = 587


@dataclass
class AppConfig:
    kiwoom: KiwoomConfig = field(default_factory=KiwoomConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    smtp: SmtpConfig = field(default_factory=SmtpConfig)

    # 감시 종목 리스트
    watchlist: list[str] = field(default_factory=lambda: [
        "005930",   # 삼성전자
        "000660",   # SK하이닉스
        "035420",   # NAVER
        "051910",   # LG화학
        "006400",   # 삼성SDI
    ])

    # 분석 타임프레임
    timeframes: list[str] = field(default_factory=lambda: ["D", "60", "15"])

    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))
    log_dir: str = "logs"
    min_market_cap: int = field(default_factory=lambda: int(os.environ.get("MIN_MARKET_CAP", "5000000000000")))


config = AppConfig()
