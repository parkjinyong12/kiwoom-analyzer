"""
Slack Notifier Agent
RiskCheckResult → Slack Block Kit 포맷 → Webhook 발송.

모든 메시지는 #stock-agent-message 단일 채널로 발송.
Webhook URL: config.slack.webhook_url (환경변수 SLACK_WEBHOOK_URL로 오버라이드 가능)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from slack_sdk.webhook import WebhookClient
from slack_sdk.errors import SlackApiError

from config import config
from models import RiskCheckResult, SupplyDemandFinding, TradeSignal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 발송 결과
# ---------------------------------------------------------------------------

@dataclass
class SendResult:
    success: bool
    channel: str
    message_ts: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 1


# ---------------------------------------------------------------------------
# 일일 요약 집계용 카운터
# ---------------------------------------------------------------------------

@dataclass
class DailySummary:
    date: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    buy_count: int = 0
    sell_count: int = 0
    blocked_count: int = 0
    error_count: int = 0

    def record_signal(self, signal: TradeSignal) -> None:
        if signal.signal == "BUY":
            self.buy_count += 1
        elif signal.signal == "SELL":
            self.sell_count += 1

    def record_block(self) -> None:
        self.blocked_count += 1

    def record_error(self) -> None:
        self.error_count += 1

    def reset(self) -> None:
        self.date = datetime.now().strftime("%Y-%m-%d")
        self.buy_count = 0
        self.sell_count = 0
        self.blocked_count = 0
        self.error_count = 0


# ---------------------------------------------------------------------------
# Block Kit 빌더
# ---------------------------------------------------------------------------

class BlockKitBuilder:
    """Slack Block Kit 메시지 블록 생성."""

    _RISK_COLOR = {"LOW": "#2EB67D", "MEDIUM": "#ECB22E", "HIGH": "#E01E5A"}
    _SIGNAL_EMOJI = {"BUY": "📈", "SELL": "📉"}
    _SIGNAL_KO = {"BUY": "매수", "SELL": "매도"}
    _RISK_KO = {"LOW": "낮음", "MEDIUM": "보통", "HIGH": "높음"}

    def build_trade_signal(self, result: RiskCheckResult) -> dict:
        s = result.signal
        emoji = self._SIGNAL_EMOJI.get(s.signal, "📊")
        signal_ko = self._SIGNAL_KO.get(s.signal, s.signal)
        color = self._RISK_COLOR.get(result.risk_level, "#36C5F0")
        time_str = s.timestamp.strftime("%H:%M")
        reasons_text = "\n".join(f"• {r}" for r in s.reasons) if s.reasons else "• 복합 신호"

        blocks: list[dict] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{emoji} [{signal_ko} 신호] {s.ticker}*\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"💰 현재가: *{s.price:,.0f}원*\n"
                        f"🎯 목표가: {s.target_price:,.0f}원\n"
                        f"🛡️ 손절가: {s.stop_loss:,.0f}원\n"
                        f"📊 신뢰도: *{result.adjusted_confidence * 100:.0f}%*\n"
                        f"⚠️ 리스크: {self._RISK_KO[result.risk_level]}\n"
                        f"⏰ 시간: {time_str}"
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*📋 근거*\n{reasons_text}"},
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"🔍 전략: {s.strategy_name} | 타임프레임: {s.timeframe}"}
                ],
            },
        ]
        return {"attachments": [{"color": color, "blocks": blocks}]}

    def build_error(self, title: str, detail: str) -> dict:
        return {
            "attachments": [
                {
                    "color": "#E01E5A",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"⚠️ *[시스템 에러]* {title}\n"
                                    f"━━━━━━━━━━━━━━━\n"
                                    f"```{detail}```\n"
                                    f"_발생 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_"
                                ),
                            },
                        }
                    ],
                }
            ]
        }

    def build_daily_summary(self, summary: DailySummary) -> dict:
        total = summary.buy_count + summary.sell_count
        return {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*📊 [일일 요약] {summary.date}*\n"
                            f"━━━━━━━━━━━━━━━\n"
                            f"✅ 발송 신호: 매수 {summary.buy_count}건 / 매도 {summary.sell_count}건 (총 {total}건)\n"
                            f"🚫 필터 차단: {summary.blocked_count}건\n"
                            f"⚠️ 에러: {summary.error_count}건"
                        ),
                    },
                }
            ]
        }

    def build_supply_demand_alert(self, finding: SupplyDemandFinding) -> dict:
        """수급 트렌드 경보 Block Kit 메시지."""
        alerts_text = "\n".join(f"• {a}" for a in finding.alerts)

        # 매수/증가 우세면 초록, 매도/감소 우세면 빨강, 혼합이면 주황
        positive = sum(1 for a in finding.alerts if any(k in a for k in ("순매수", "증가")))
        negative = sum(1 for a in finding.alerts if any(k in a for k in ("순매도", "감소")))
        if positive > negative:
            color = "#2EB67D"
            trend_emoji = "📈"
        elif negative > positive:
            color = "#E01E5A"
            trend_emoji = "📉"
        else:
            color = "#ECB22E"
            trend_emoji = "🔄"

        header = (
            f"*{trend_emoji} [수급 변화 감지] {finding.stock_code}"
            + (f" {finding.stock_name}" if finding.stock_name else "")
            + "*\n━━━━━━━━━━━━━━━\n"
            + alerts_text
        )

        # 세부 수치 블록 (연속 매매 최근 수량)
        detail_lines: list[str] = []
        for investor in ("기관", "외국인"):
            key = f"{investor}_consec"
            if key in finding.details:
                d = finding.details[key]
                qty = d["recent_qty"] or 0
                detail_lines.append(f"{investor} 최근 순{d['direction'][2:]}: {qty:+,}주")

        if detail_lines:
            header += "\n\n*📋 최근 수치*\n" + "\n".join(detail_lines)

        header += f"\n\n_분석 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}_"

        return {
            "attachments": [
                {
                    "color": color,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": header},
                        }
                    ],
                }
            ]
        }

    def build_system_status(self, status: str, detail: str = "") -> dict:
        emoji = "🟢" if status == "시작" else "🔴"
        text = f"{emoji} *[시스템 {status}]* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        if detail:
            text += f"\n{detail}"
        return {"text": text}


# ---------------------------------------------------------------------------
# Slack Notifier Agent
# ---------------------------------------------------------------------------

class SlackNotifierAgent:
    """
    Slack Webhook 기반 알림 발송 에이전트.
    모든 메시지는 단일 채널(config.slack.channel)로 전송.
    """

    _MAX_RETRIES = 3
    _RETRY_DELAY = 1.0

    def __init__(self) -> None:
        url = config.slack.webhook_url
        if not url:
            logger.warning("SLACK_WEBHOOK_URL 미설정 — dry-run 모드")
        self._client = WebhookClient(url=url) if url else None
        self._builder = BlockKitBuilder()
        self.summary = DailySummary()

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    def send_signal(self, result: RiskCheckResult) -> SendResult:
        if not result.approved:
            self.summary.record_block()
            return SendResult(success=False, channel="", error="미승인 신호")

        payload = self._builder.build_trade_signal(result)
        send_result = self._send(payload)

        if send_result.success:
            self.summary.record_signal(result.signal)
            logger.info("신호 알림 발송: %s %s", result.signal.ticker, result.signal.signal)
        else:
            self.summary.record_error()

        return send_result

    def send_error(self, title: str, detail: str = "") -> SendResult:
        payload = self._builder.build_error(title, detail)
        result = self._send(payload)
        if not result.success:
            logger.error("에러 알림 발송 실패: %s | %s", title, result.error)
        return result

    def send_daily_summary(self, summary: Optional[DailySummary] = None) -> SendResult:
        target = summary or self.summary
        payload = self._builder.build_daily_summary(target)
        result = self._send(payload)
        if result.success:
            logger.info("일일 요약 발송 완료: %s", target.date)
        return result

    def send_supply_demand_alert(self, finding: SupplyDemandFinding) -> SendResult:
        """수급 트렌드 경보 발송."""
        payload = self._builder.build_supply_demand_alert(finding)
        result = self._send(payload)
        if result.success:
            logger.info(
                "수급 알림 발송: %s %s | %s",
                finding.stock_code, finding.stock_name, finding.alerts,
            )
        else:
            logger.warning("수급 알림 발송 실패: %s | %s", finding.stock_code, result.error)
        return result

    def send_system_status(self, status: str, detail: str = "") -> SendResult:
        payload = self._builder.build_system_status(status, detail)
        return self._send(payload)

    # ------------------------------------------------------------------
    # 내부 발송
    # ------------------------------------------------------------------

    def _send(self, payload: dict) -> SendResult:
        channel = config.slack.channel

        if not self._client:
            logger.info("[DRY-RUN] %s | keys: %s", channel, list(payload.keys()))
            return SendResult(success=True, channel=channel, message_ts="dry-run")

        last_error: Optional[str] = None

        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                response = self._client.send(**payload)
                if response.status_code == 200:
                    return SendResult(success=True, channel=channel, attempts=attempt)
                last_error = f"HTTP {response.status_code}: {response.body}"
                logger.warning("Slack Webhook 실패 (시도 %d/%d): %s", attempt, self._MAX_RETRIES, last_error)
            except Exception as e:
                last_error = str(e)
                logger.error("Slack Webhook 오류: %s", e)
                break

            if attempt < self._MAX_RETRIES:
                time.sleep(self._RETRY_DELAY)

        return SendResult(success=False, channel=channel, error=last_error, attempts=self._MAX_RETRIES)
