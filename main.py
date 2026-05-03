"""
키움 차트 분석 시스템 실행 진입점.

사용법:
    python main.py                          # 스케줄 실행 (30분 간격)
    python main.py --interval 15            # 15분 간격
    python main.py --once                   # 즉시 1회 실행 후 종료
    python main.py --collect-history        # 수급 이력 일괄 수집 (500일, 전 종목)
    python main.py --collect-history --days 200  # 200일치만
"""
from __future__ import annotations

import sys

from orchestrator import Orchestrator, setup_logging
from config import config


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="키움 차트 분석 시스템")
    parser.add_argument("--interval", type=int, default=30, help="장 중 분석 주기 (분, 기본 30)")
    parser.add_argument("--once", action="store_true", help="스케줄 없이 즉시 1회 실행")
    parser.add_argument(
        "--collect-history",
        action="store_true",
        help="전체 감시 종목 수급 이력 일괄 수집 후 종료",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=500,
        help="--collect-history 시 수집할 최대 일수 (기본 500)",
    )
    args = parser.parse_args()

    setup_logging(config.log_dir)

    orchestrator = Orchestrator(interval_minutes=args.interval)

    if args.collect_history:
        orchestrator.collect_supply_history(max_days=args.days)
    elif args.once:
        orchestrator.run_once()
    else:
        orchestrator.start()


if __name__ == "__main__":
    main()
