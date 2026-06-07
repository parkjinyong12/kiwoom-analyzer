"""
Market Data Agent — 키움 REST API 기반 OHLCV 수집.

인증 흐름:
  app_key + secretkey → POST /oauth2/token → token (만료일 expires_dt)
  이후 모든 요청 헤더에 authorization: Bearer {token} 포함.

주요 엔드포인트:
  일봉  : POST /api/dostk/chart   (api-id: ka10081)
  분봉  : POST /api/dostk/chart   (api-id: ka10080)
  현재가: POST /api/dostk/mrkcond (api-id: ka10007)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def resolve_exchange() -> str:
    """시간대에 따라 거래소를 반환 (평일 기준).

    08:00~08:50  → NXT (장전 시간외)
    09:00~15:30  → KRX (정규장)
    15:30~18:00  → NXT (장후 시간외)
    그 외        → KRX
    """
    now = datetime.now(tz=KST)
    if now.weekday() >= 5:
        return "KRX"
    t = now.hour * 100 + now.minute  # HHMM 정수 비교
    if 800 <= t < 850:
        return "NXT"
    if 1530 <= t < 1800:
        return "NXT"
    return "KRX"

import pandas as pd
import requests

from config import config
from models import AccountHolding, OHLCVBar

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 요청 속도 제한
# ---------------------------------------------------------------------------

class RateLimiter:
    """최소 간격 + 분당 횟수 제한."""

    def __init__(self, min_interval: float, per_minute: int) -> None:
        self._min_interval = min_interval
        self._per_minute = per_minute
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._timestamps: list[float] = []

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()

            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
                now = time.monotonic()

            cutoff = now - 60.0
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            if len(self._timestamps) >= self._per_minute:
                sleep_for = 60.0 - (now - self._timestamps[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()

            self._timestamps.append(now)
            self._last_call = now


# ---------------------------------------------------------------------------
# 토큰 관리
# ---------------------------------------------------------------------------

_TOKEN_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", ".token_cache.json")


class TokenManager:
    """액세스 토큰 발급 및 자동 갱신 (만료 5분 전 선제 갱신).

    토큰을 파일로 캐싱하여 프로세스 재시작 시 재사용.
    """

    def __init__(self, app_key: str, app_secret: str, base_url: str) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._base_url = base_url
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            with open(_TOKEN_CACHE_PATH, "r") as f:
                cached = json.load(f)
            token = cached.get("token")
            expires_unix = cached.get("expires_unix", 0.0)
            if token and time.time() < expires_unix - 300:
                self._token = token
                # monotonic 기준으로 변환
                remaining = expires_unix - time.time()
                self._expires_at = time.monotonic() + remaining
                logger.info("캐시된 토큰 재사용 (만료: %ds 후)", int(remaining))
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            pass

    def _save_cache(self, expires_in: float) -> None:
        try:
            with open(_TOKEN_CACHE_PATH, "w") as f:
                json.dump({"token": self._token, "expires_unix": time.time() + expires_in}, f)
        except OSError:
            pass

    def get_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires_at - 300:
                return self._token
            self._issue_token()
            return self._token  # type: ignore[return-value]

    def force_refresh(self) -> str:
        """서버 측 토큰 무효화(8005 등) 시 강제 재발급."""
        with self._lock:
            self._expires_at = 0.0
            self._token = None
            self._issue_token()
            return self._token  # type: ignore[return-value]

    def _issue_token(self) -> None:
        url = f"{self._base_url}/oauth2/token"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("return_code", -1) != 0:
            raise RuntimeError(f"토큰 발급 실패: {data.get('return_msg')}")
        self._token = data["token"]
        expires_dt = data.get("expires_dt", "")
        if expires_dt:
            exp = datetime.strptime(expires_dt, "%Y%m%d%H%M%S").replace(tzinfo=KST)
            expires_in = max(0, (exp - datetime.now(tz=KST)).total_seconds())
        else:
            expires_in = 86400
        self._expires_at = time.monotonic() + expires_in
        self._save_cache(expires_in)
        logger.info("키움 액세스 토큰 발급 완료 (만료: %ds 후)", int(expires_in))


# ---------------------------------------------------------------------------
# Market Data Agent
# ---------------------------------------------------------------------------

class MarketDataAgent:
    """키움 REST API로 OHLCV 데이터를 수집하는 에이전트."""

    def __init__(self) -> None:
        cfg = config.kiwoom
        if not cfg.app_key or not cfg.app_secret:
            raise ValueError("KIWOOM_APP_KEY / KIWOOM_APP_SECRET 환경변수가 설정되지 않았습니다.")

        self._base_url = cfg.base_url
        self._token_mgr = TokenManager(cfg.app_key, cfg.app_secret, cfg.base_url)
        self._limiter = RateLimiter(
            min_interval=cfg.tr_delay_seconds,
            per_minute=cfg.tr_per_minute_limit,
        )

    # ------------------------------------------------------------------
    # 공통 요청
    # ------------------------------------------------------------------

    def _post(self, path: str, api_id: str, body: dict, _retry: bool = True) -> dict:
        self._limiter.wait()
        url = f"{self._base_url}{path}"
        headers = {
            "authorization": f"Bearer {self._token_mgr.get_token()}",
            "api-id": api_id,
            "Content-Type": "application/json;charset=UTF-8",
        }
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("return_code", -1) != 0:
            msg = data.get("return_msg", "알 수 없는 오류")
            # 토큰 무효화(8005) → 강제 재발급 후 1회 재시도
            if _retry and "8005" in msg:
                logger.warning("토큰 무효화(8005) 감지 — 강제 재발급 후 재시도")
                self._token_mgr.force_refresh()
                return self._post(path, api_id, body, _retry=False)
            raise RuntimeError(f"키움 API 오류 [{api_id}]: {msg}")

        return data

    # ------------------------------------------------------------------
    # 일봉 OHLCV (ka10081)
    # ------------------------------------------------------------------

    def get_daily_ohlcv(self, ticker: str, count: int = 200, stex_tp: str | None = None) -> pd.DataFrame:
        """일봉 데이터 조회 (최근 count개 봉).

        stex_tp: 거래소 지정 ("KRX"/"NXT"). None이면 현재 시간 기준 자동 선택.
        NXT 조회 시 종목코드에 _NX 접미사를 붙여 요청 (API 스펙).
        """
        exch = stex_tp or resolve_exchange()
        stk_cd = f"{ticker}_NX" if exch == "NXT" else ticker
        body = {
            "stk_cd": stk_cd,
            "base_dt": datetime.now(tz=KST).strftime("%Y%m%d"),
            "upd_stkpc_tp": "1",
        }
        data = self._post("/api/dostk/chart", "ka10081", body)
        rows = data.get("stk_dt_pole_chart_qry", [])
        df = self._parse_daily(rows)
        logger.debug("일봉 조회: %s → %d건", ticker, len(df))
        return df.tail(count).reset_index(drop=True)

    # ------------------------------------------------------------------
    # 분봉 OHLCV (ka10080)
    # ------------------------------------------------------------------

    def get_minute_ohlcv(self, ticker: str, timeframe: str = "60", count: int = 200) -> pd.DataFrame:
        """분봉 데이터 조회 (timeframe: 1/3/5/10/15/30/45/60)."""
        body = {
            "stk_cd": ticker,
            "tic_scope": timeframe,
            "upd_stkpc_tp": "1",
            "base_dt": datetime.now(tz=KST).strftime("%Y%m%d"),
        }
        data = self._post("/api/dostk/chart", "ka10080", body)
        rows = data.get("stk_min_pole_chart_qry", [])
        df = self._parse_minute(rows)
        logger.debug("분봉(%s) 조회: %s → %d건", timeframe, ticker, len(df))
        return df.tail(count).reset_index(drop=True)

    # ------------------------------------------------------------------
    # 전체 종목 리스트 (ka10099)
    # ------------------------------------------------------------------

    def get_all_stocks(self, mrkt_tp: str) -> list[dict]:
        """시장 전체 종목 리스트 조회 (페이지네이션 자동 처리).

        mrkt_tp: '0'=코스피, '10'=코스닥
        """
        self._limiter.wait()
        url = f"{self._base_url}/api/dostk/stkinfo"
        fetched_at = datetime.now(tz=KST)
        results: list[dict] = []
        cont_yn = ""
        next_key = ""

        while True:
            headers = {
                "authorization": f"Bearer {self._token_mgr.get_token()}",
                "api-id": "ka10099",
                "Content-Type": "application/json;charset=UTF-8",
            }
            if cont_yn == "Y":
                headers["cont-yn"] = cont_yn
                headers["next-key"] = next_key

            resp = requests.post(url, headers=headers, json={"mrkt_tp": mrkt_tp}, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("return_code", -1) != 0:
                raise RuntimeError(f"종목리스트 조회 오류: {data.get('return_msg')}")

            for item in data.get("list", []):
                results.append({
                    "stock_code": item.get("code", ""),
                    "stock_name": item.get("name", ""),
                    "market_code": item.get("marketCode", ""),
                    "market_name": item.get("marketName", ""),
                    "state": item.get("state", ""),
                    "last_price": item.get("lastPrice", ""),
                    "list_count": item.get("listCount", ""),
                    "fetched_at": fetched_at,
                })

            cont_yn = resp.headers.get("cont-yn", "")
            next_key = resp.headers.get("next-key", "")
            if cont_yn != "Y":
                break
            self._limiter.wait()

        logger.info("종목리스트 조회 완료: mrkt_tp=%s → %d건", mrkt_tp, len(results))
        return results

    # ------------------------------------------------------------------
    # 종목정보 (ka10100)
    # ------------------------------------------------------------------

    def get_stock_info(self, ticker: str) -> dict:
        """종목 기본정보 조회 (ka10100)."""
        data = self._post("/api/dostk/stkinfo", "ka10100", {"stk_cd": ticker})
        return {
            "stock_code": data.get("code", ticker),
            "stock_name": data.get("name", ""),
            "market_code": data.get("marketCode", ""),
            "market_name": data.get("marketName", ""),
            "state": data.get("state", ""),
            "last_price": data.get("lastPrice", ""),
            "fetched_at": datetime.now(tz=KST),
        }

    # ------------------------------------------------------------------
    # 현재가 (ka10007)
    # ------------------------------------------------------------------

    def get_current_price(self, ticker: str, stex_tp: str | None = None) -> Optional[float]:
        """현재가 단일 조회.

        stex_tp: 거래소 지정 ("KRX"/"NXT"). None이면 현재 시간 기준 자동 선택.
        NXT 조회 시 종목코드에 _NX 접미사를 붙여 요청 (API 스펙).
        """
        exch = stex_tp or resolve_exchange()
        stk_cd = f"{ticker}_NX" if exch == "NXT" else ticker
        try:
            data = self._post("/api/dostk/mrkcond", "ka10007", {"stk_cd": stk_cd})
            price_str = data.get("cur_prc", "0").replace(",", "").replace("+", "").replace("-", "")
            return float(price_str) if price_str else None
        except Exception as e:
            logger.error("현재가 조회 실패 (%s): %s", ticker, e)
            return None

    # ------------------------------------------------------------------
    # 감시 종목 일괄
    # ------------------------------------------------------------------

    def get_watchlist_prices(self, tickers: list[str]) -> dict[str, float]:
        """감시 종목 현재가 일괄 조회."""
        result: dict[str, float] = {}
        for ticker in tickers:
            price = self.get_current_price(ticker)
            if price:
                result[ticker] = price
        return result

    def get_foreign_holding(self, ticker: str, max_days: int = 60) -> list[dict]:
        """외국인 일별 보유수량 조회 (ka10008).

        max_days > 60이면 cont-yn/next-key 페이지네이션으로 여러 페이지 수집.
        """
        url = f"{self._base_url}/api/dostk/frgnistt"
        results: list[dict] = []
        cont_yn = ""
        next_key = ""

        while len(results) < max_days:
            self._limiter.wait()
            headers = {
                "authorization": f"Bearer {self._token_mgr.get_token()}",
                "api-id": "ka10008",
                "Content-Type": "application/json;charset=UTF-8",
            }
            if cont_yn == "Y":
                headers["cont-yn"] = cont_yn
                headers["next-key"] = next_key

            resp = requests.post(url, headers=headers, json={"stk_cd": ticker}, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("return_code", -1) != 0:
                raise RuntimeError(f"외국인보유 조회 오류 ({ticker}): {data.get('return_msg')}")

            rows = data.get("stk_frgnr", [])
            if not rows:
                break

            for row in rows:
                try:
                    results.append({
                        "date": datetime.strptime(row["dt"], "%Y%m%d").date(),
                        "for_hold_qty": int(
                            row.get("poss_stkcnt", "0").replace(",", "").replace("+", "").lstrip("-") or "0"
                        ),
                        "for_chg_qty": self._signed_int(row.get("chg_qty", "0")),
                        "for_hold_ratio": row.get("wght", ""),
                        "close_price": abs(self._signed_int(row.get("close_pric", "0"))),
                    })
                except Exception:
                    continue

            cont_yn = resp.headers.get("cont-yn", "")
            next_key = resp.headers.get("next-key", "")
            if cont_yn != "Y":
                break

        logger.debug("외국인보유 조회: %s → %d행", ticker, len(results))
        return results[:max_days]

    def get_investor_netbuy(self, ticker: str, date: str) -> dict | None:
        """투자자별 순매수수량 단일 날짜 조회 (ka10059). date: YYYYMMDD"""
        try:
            data = self._post("/api/dostk/stkinfo", "ka10059", {
                "dt": date,
                "stk_cd": ticker,
                "amt_qty_tp": "2",
                "trde_tp": "0",
                "unit_tp": "1",
            })
            rows = data.get("stk_invsr_orgn", [])
            if not rows:
                return None
            return self._parse_investor_row(rows[0])
        except Exception as e:
            logger.error("투자자 순매수 조회 실패 (%s): %s", ticker, e)
            return None

    def get_investor_netbuy_history(self, ticker: str, max_days: int = 500) -> list[dict]:
        """투자자별 순매수 이력 페이지네이션 수집 (ka10059).

        기관/외국인/개인 + 기관 세부(금융투자/보험/투신/은행/연기금/사모) 포함.
        최근 거래일 기준 최대 max_days일 반환 (날짜 오름차순).
        """
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y%m%d")
        url = f"{self._base_url}/api/dostk/stkinfo"
        results: list[dict] = []
        cont_yn = ""
        next_key = ""

        while len(results) < max_days:
            self._limiter.wait()
            headers = {
                "authorization": f"Bearer {self._token_mgr.get_token()}",
                "api-id": "ka10059",
                "Content-Type": "application/json;charset=UTF-8",
            }
            if cont_yn == "Y":
                headers["cont-yn"] = cont_yn
                headers["next-key"] = next_key

            resp = requests.post(
                url, headers=headers,
                json={"dt": today, "stk_cd": ticker, "amt_qty_tp": "2", "trde_tp": "0", "unit_tp": "1"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("return_code", -1) != 0:
                raise RuntimeError(f"투자자 이력 조회 오류 ({ticker}): {data.get('return_msg')}")

            rows = data.get("stk_invsr_orgn", [])
            if not rows:
                break

            for row in rows:
                parsed = self._parse_investor_row(row)
                if parsed:
                    results.append(parsed)

            cont_yn = resp.headers.get("cont-yn", "")
            next_key = resp.headers.get("next-key", "")
            if cont_yn != "Y":
                break

        logger.debug("투자자 이력 조회: %s → %d행", ticker, len(results))
        # 최신순으로 수집됐으므로 오름차순 정렬
        results.sort(key=lambda r: r["date"])
        return results[:max_days]

    def _parse_investor_row(self, row: dict) -> Optional[dict]:
        """ka10059 단일 행 파싱."""
        try:
            return {
                "date":         datetime.strptime(row["dt"], "%Y%m%d").date(),
                "orgn_net_qty": self._signed_int(row.get("orgn", "0")),
                "for_net_qty":  self._signed_int(row.get("frgnr_invsr", "0")),
                "ind_net_qty":  self._signed_int(row.get("ind_invsr", "0")),
                "fnnc_invt":    self._signed_int(row.get("fnnc_invt", "0")),
                "insrnc":       self._signed_int(row.get("insrnc", "0")),
                "invtrt":       self._signed_int(row.get("invtrt", "0")),
                "bank":         self._signed_int(row.get("bank", "0")),
                "penfnd_etc":   self._signed_int(row.get("penfnd_etc", "0")),
                "samo_fund":    self._signed_int(row.get("samo_fund", "0")),
            }
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 계좌 보유종목 (ka10085)
    # ------------------------------------------------------------------

    def get_account_holdings(self) -> list[AccountHolding]:
        """계좌 보유종목 전체 조회 (ka10085).

        .env에 KIWOOM_ACNT_NO, KIWOOM_ACNT_PWD 필요.
        페이지네이션 자동 처리.
        """
        cfg = config.kiwoom
        if not cfg.acnt_no or not cfg.acnt_pwd:
            raise ValueError("KIWOOM_ACNT_NO / KIWOOM_ACNT_PWD 환경변수가 설정되지 않았습니다.")

        url = f"{self._base_url}/api/dostk/acnt"
        results: list[AccountHolding] = []
        cont_yn = ""
        next_key = ""

        while True:
            self._limiter.wait()
            headers = {
                "authorization": f"Bearer {self._token_mgr.get_token()}",
                "api-id": "ka10085",
                "Content-Type": "application/json;charset=UTF-8",
            }
            if cont_yn == "Y":
                headers["cont-yn"] = cont_yn
                headers["next-key"] = next_key

            body = {
                "acnt_no": cfg.acnt_no,
                "acnt_pwd": cfg.acnt_pwd,
                "qry_tp": "1",
                "stex_tp": "KRX",  # KRX=한국거래소
            }
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("return_code", -1) != 0:
                raise RuntimeError(f"계좌잔고 조회 오류: {data.get('return_msg')}")

            # 응답 키: acnt_prft_rt, 수량 필드: rmnd_qty(실시간) / setl_remn(결제잔고)
            rows = data.get("acnt_prft_rt", [])
            for row in rows:
                try:
                    # crd_tp=99 는 신용 집계용 더미 행 — 실제 포지션 아님
                    if row.get("crd_tp") == "99":
                        continue
                    rmnd_qty  = int(row.get("rmnd_qty",  "0").replace(",", "") or "0")
                    setl_remn = int(row.get("setl_remn", "0").replace(",", "") or "0")
                    qty = rmnd_qty if rmnd_qty > 0 else setl_remn
                    if qty == 0:
                        continue
                    cur_prc  = self._to_float(row.get("cur_prc",  "0"))
                    pur_pric = self._to_float(row.get("pur_pric", "0"))
                    pur_amt  = self._to_float(row.get("pur_amt",  "0"))
                    eval_amt = cur_prc * qty
                    # pur_amt=0 이면 API가 매입단가 미제공 → 손익 산출 불가
                    pnl_amt = eval_amt - pur_amt if pur_amt > 0 else float("nan")
                    pnl_rt  = (pnl_amt / pur_amt * 100) if pur_amt > 0 else float("nan")
                    results.append(AccountHolding(
                        stock_code=row.get("stk_cd", "").strip(),
                        stock_name=row.get("stk_nm", "").strip().lstrip("*"),
                        hold_qty=qty,
                        buy_avg_price=pur_pric,
                        pur_amount=pur_amt,
                        current_price=cur_prc,
                        eval_amount=eval_amt,
                        pnl_amount=pnl_amt,
                        pnl_rate=pnl_rt,
                    ))
                except Exception as e:
                    logger.warning("보유종목 파싱 오류: %s / row=%s", e, row)

            cont_yn = resp.headers.get("cont-yn", "")
            next_key = resp.headers.get("next-key", "")
            if cont_yn != "Y":
                break

        logger.info("계좌 보유종목 조회 완료: %d개", len(results))
        return results

    def _to_float(self, val: str) -> float:
        try:
            return float(val.replace(",", "").replace("+", "").lstrip("-") or "0")
        except ValueError:
            return 0.0

    def _signed_int(self, val: str) -> int:
        """'+123', '-456', '789' 형태 문자열을 int로 변환."""
        try:
            return int(val.replace(",", "") or "0")
        except ValueError:
            return 0

    def get_watchlist_ohlcv(
        self,
        tickers: list[str],
        timeframe: str = "D",
        count: int = 200,
    ) -> dict[str, pd.DataFrame]:
        """감시 종목 OHLCV 일괄 조회. 실패 종목은 제외."""
        result: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            try:
                df = (
                    self.get_daily_ohlcv(ticker, count)
                    if timeframe == "D"
                    else self.get_minute_ohlcv(ticker, timeframe, count)
                )
                if not df.empty:
                    result[ticker] = df
            except Exception as e:
                logger.error("OHLCV 조회 실패 (%s %s): %s", ticker, timeframe, e)
        return result

    # ------------------------------------------------------------------
    # 파싱
    # ------------------------------------------------------------------

    def _parse_daily(self, rows: list[dict]) -> pd.DataFrame:
        """일봉 응답 rows → DataFrame."""
        records = []
        for row in rows:
            try:
                dt = datetime.strptime(row["dt"], "%Y%m%d").replace(tzinfo=KST)

                def to_float(val: str) -> float:
                    return float(val.replace(",", "").replace("+", "").lstrip("-") or "0")

                records.append({
                    "date":   dt,
                    "open":   to_float(row.get("open_pric", "0")),
                    "high":   to_float(row.get("high_pric", "0")),
                    "low":    to_float(row.get("low_pric", "0")),
                    "close":  to_float(row.get("cur_prc", "0")),
                    "volume": to_float(row.get("trde_qty", "0")),
                })
            except Exception:
                continue

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
        return df[df["close"] > 0].reset_index(drop=True)

    def _parse_minute(self, rows: list[dict]) -> pd.DataFrame:
        """분봉 응답 rows → DataFrame."""
        records = []
        for row in rows:
            try:
                dt = datetime.strptime(row["cntr_tm"], "%Y%m%d%H%M%S").replace(tzinfo=KST)

                def to_float(val: str) -> float:
                    return float(val.replace(",", "").replace("+", "").lstrip("-") or "0")

                records.append({
                    "date":   dt,
                    "open":   to_float(row.get("open_pric", "0")),
                    "high":   to_float(row.get("high_pric", "0")),
                    "low":    to_float(row.get("low_pric", "0")),
                    "close":  to_float(row.get("cur_prc", "0")),
                    "volume": to_float(row.get("trde_qty", "0")),
                })
            except Exception:
                continue

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
        return df[df["close"] > 0].reset_index(drop=True)
