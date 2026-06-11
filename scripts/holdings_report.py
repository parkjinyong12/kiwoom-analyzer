import argparse
import logging
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import config
import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB 조회
# ---------------------------------------------------------------------------

def get_manual_holdings(conn) -> list[dict]:
    """manual_holdings 테이블에서 보유종목 조회 (현재가 포함, 종목코드별 합산)."""
    sql = """
        WITH latest_close AS (
            SELECT DISTINCT ON (stock_code)
                stock_code, close_price
            FROM supply_demand
            WHERE close_price IS NOT NULL AND close_price > 0
            ORDER BY stock_code, date DESC
        )
        SELECT
            mh.stock_code,
            mh.stock_name,
            SUM(mh.quantity)                        AS hold_qty,
            SUM(mh.quantity * mh.avg_price)         AS pur_amt_total,
            COALESCE(
                lc.close_price,
                CASE WHEN st.last_price ~ '^[0-9]+$' THEN st.last_price::BIGINT ELSE NULL END
            )                                       AS current_price
        FROM manual_holdings mh
        LEFT JOIN latest_close lc ON lc.stock_code = mh.stock_code
        LEFT JOIN stocks st ON st.stock_code = mh.stock_code
        WHERE mh.quantity > 0
        GROUP BY mh.stock_code, mh.stock_name, lc.close_price, st.last_price
        ORDER BY mh.stock_name
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("manual_holdings 조회 실패: %s", e)
        return []

    holdings = []
    for r in rows:
        hold_qty     = int(r["hold_qty"] or 0)
        pur_amt      = float(r["pur_amt_total"] or 0)
        cur_price    = int(r["current_price"]) if r["current_price"] else None
        eval_amt     = hold_qty * cur_price if cur_price else hold_qty * (pur_amt / hold_qty if hold_qty else 0)
        has_cost     = pur_amt > 0
        pnl_amt      = (eval_amt - pur_amt) if has_cost else None
        pnl_rt       = (pnl_amt / pur_amt * 100) if (has_cost and pur_amt > 0) else None
        holdings.append({
            "stock_code":    r["stock_code"],
            "stock_name":    r["stock_name"],
            "hold_qty":      hold_qty,
            "pur_amt_total": pur_amt,
            "eval_amount":   eval_amt,
            "current_price": cur_price or (pur_amt / hold_qty if hold_qty else 0),
            "has_cost_data": has_cost,
            "pnl_amount":    pnl_amt,
            "pnl_rate":      pnl_rt,
        })
    return holdings


def get_supply_history(stock_code: str, conn) -> list[dict]:
    """supply_demand 테이블에서 최근 12행 조회."""
    sql = """
        SELECT date, close_price, orgn_net_qty, for_net_qty, ind_net_qty
        FROM supply_demand
        WHERE stock_code = %s AND close_price IS NOT NULL AND close_price > 0
        ORDER BY date DESC
        LIMIT 12
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (stock_code,))
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("supply_demand 조회 실패 (%s): %s", stock_code, e)
        return []


def get_recipients(conn) -> list[str]:
    """report_email_config 테이블에서 active=TRUE인 수신자 목록 조회."""
    sql = "SELECT email FROM report_email_config WHERE active = TRUE"
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        logger.warning("수신자 목록 조회 실패: %s", e)
        return []


def log_send(conn, recipients: list[str], stock_count: int, status: str, error_msg: str | None = None) -> None:
    """이메일 발송 로그를 DB에 저장."""
    sql = """
        INSERT INTO report_send_log (sent_at, recipients, stock_count, status, error_msg)
        VALUES (NOW(), %s, %s, %s, %s)
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (",".join(recipients), stock_count, status, error_msg))
        conn.commit()
    except Exception as e:
        logger.warning("발송 로그 저장 실패: %s", e)


# ---------------------------------------------------------------------------
# 데이터 계산
# ---------------------------------------------------------------------------

def _fmt_k(value: int | None) -> str:
    """정수를 K단위 문자열로 변환. 예: 12345 → '+12.3K', -234 → '-0.2K'"""
    if value is None:
        return "-"
    abs_val = abs(value)
    k_val = abs_val / 1000
    sign = "+" if value >= 0 else "-"
    if k_val >= 1:
        return f"{sign}{k_val:,.1f}K"
    else:
        # 1K 미만이면 실수 값 그대로 (천단위 콤마)
        return f"{sign}{abs_val:,}"


def _color_class(value: float | int | None) -> str:
    """양수/음수/없음 색상 코드 반환."""
    if value is None:
        return "#64748b"
    if value > 0:
        return "#4ade80"
    if value < 0:
        return "#f87171"
    return "#64748b"


def build_stock_row(holding: dict, history: list[dict]) -> dict:
    """종목별 리포트 데이터 계산.

    기준: 10거래일 전 (history[10])
    컬럼: 10D기준 → 9D → 8D → … → 1D → 현재

    가격: 10D 종가 대비 각 시점 등락률
    수급: 10D 이후 누적 순매수 (10D 당일은 포함 안 함)
    """
    current_price: float = holding["current_price"]

    # 10D 기준 종가
    base_close = None
    if len(history) > 10:
        v = history[10].get("close_price")
        if v and v > 0:
            base_close = float(v)

    # ── 누적 등락률 (10D 기준 대비) ──────────────────────────
    price_chg_cum: dict = {"10D": None}
    for n in range(9, 0, -1):
        if base_close and len(history) > n:
            close_n = history[n].get("close_price")
            if close_n and float(close_n) > 0:
                price_chg_cum[n] = round((float(close_n) - base_close) / base_close * 100, 2)
            else:
                price_chg_cum[n] = None
        else:
            price_chg_cum[n] = None
    price_chg_cum["현재"] = (
        round((current_price - base_close) / base_close * 100, 2)
        if base_close else None
    )

    # ── 일별 등락률 (전일 대비 당일 변화) ───────────────────
    price_chg_daily: dict = {"10D": None}
    for n in range(9, 0, -1):
        if len(history) > n + 1:
            close_n    = history[n].get("close_price")
            close_prev = history[n + 1].get("close_price")
            if close_n and close_prev and float(close_n) > 0 and float(close_prev) > 0:
                price_chg_daily[n] = round((float(close_n) - float(close_prev)) / float(close_prev) * 100, 2)
            else:
                price_chg_daily[n] = None
        else:
            price_chg_daily[n] = None
    # 현재: 당일 실시간 vs 직전 종가(history[0])
    if history:
        close_0 = history[0].get("close_price")
        price_chg_daily["현재"] = (
            round((current_price - float(close_0)) / float(close_0) * 100, 2)
            if close_0 and float(close_0) > 0 else None
        )
    else:
        price_chg_daily["현재"] = None

    # 하위 호환 키 유지
    price_chg = price_chg_cum

    # ── 수급 누적 (10D 이후 기준점에서 각 시점까지 합산) ─────
    # history 인덱스: 0=최신, …, 9=9D ago (= 10D 기준 다음날부터 1일 누적)
    # 9D ago 까지의 누적: history[9] 만
    # 8D ago 까지의 누적: history[8] + history[9]
    # …
    # 1D ago 까지의 누적: history[1..9]
    # 현재 까지의 누적:   history[0..9]
    orgn_cum: dict = {"10D": None}
    for_cum:  dict = {"10D": None}

    for n in range(9, 0, -1):  # 9D~1D: history[n]~history[9] 합산
        slice_ = history[n:10]  # n일전 ~ 9일전 (10D 이후부터 n일전까지)
        o_vals = [r.get("orgn_net_qty") for r in slice_ if r.get("orgn_net_qty") is not None]
        f_vals = [r.get("for_net_qty")  for r in slice_ if r.get("for_net_qty")  is not None]
        orgn_cum[n] = sum(o_vals) if o_vals else None
        for_cum[n]  = sum(f_vals) if f_vals else None

    # 현재: history[0..9] 전체 합산
    slice_all = history[0:10]
    o_all = [r.get("orgn_net_qty") for r in slice_all if r.get("orgn_net_qty") is not None]
    f_all = [r.get("for_net_qty")  for r in slice_all if r.get("for_net_qty")  is not None]
    orgn_cum["현재"] = sum(o_all) if o_all else None
    for_cum["현재"]  = sum(f_all) if f_all else None

    return {
        "stock_code":      holding["stock_code"],
        "stock_name":      holding["stock_name"],
        "hold_qty":        holding["hold_qty"],
        "current_price":   current_price,
        "eval_amount":     holding["eval_amount"],
        "pnl_amount":      holding["pnl_amount"],
        "pnl_rate":        holding["pnl_rate"],
        "price_chg":       price_chg,       # 누적 (하위호환)
        "price_chg_cum":   price_chg_cum,   # 누적 (10D 기준 대비)
        "price_chg_daily": price_chg_daily, # 일별 (전일 대비)
        "orgn_cum":        orgn_cum,
        "for_cum":         for_cum,
        "base_close":      base_close,
    }


# ---------------------------------------------------------------------------
# HTML 생성
# ---------------------------------------------------------------------------

def generate_html(report_rows: list[dict], generated_at: str) -> str:
    """다크 테마 HTML 이메일 리포트 생성.

    종목당 4행:
      [헤더행] 종목명 · 종목코드 (좌) / 현재가 · 손익 · 손익률 (우)  ← full-width
      [가격행] 구분 + 10D기준→현재  누적(큰)/일별(작은) 이중 표시
      [기관행] 구분 + 누적 순매수  종목내 상대 히트맵
      [외국인] 구분 + 누적 순매수  종목내 상대 히트맵
    """

    total_eval      = sum(r["eval_amount"] for r in report_rows)
    total_pnl       = sum(r["pnl_amount"] for r in report_rows if r["pnl_amount"] is not None)
    total_pnl_color = _color_class(total_pnl)
    total_sign      = "+" if total_pnl >= 0 else ""
    no_cost_cnt     = sum(1 for r in report_rows if r["pnl_amount"] is None)

    COLS    = ["10D", 9, 8, 7, 6, 5, 4, 3, 2, 1, "현재"]
    N_COLS  = 1 + len(COLS)   # 구분(1) + 데이터(11) = 12

    def col_label(c) -> str:
        if c == "10D":  return "기준"
        if c == "현재": return "현재"
        return f"{c}D"

    # ── 셀 스타일 기준 ────────────────────────────────────────
    _TD  = "padding:5px 8px;border-bottom:1px solid #1e2535;text-align:right;vertical-align:top"
    _TDL = "padding:5px 8px;border-bottom:2px solid #2d3748;text-align:right;vertical-align:top"

    # ── 히트맵 배경 ───────────────────────────────────────────
    def _bg(val: float | None, ref: float | None = None) -> str:
        if val is None: return ""
        denom = ref if (ref and ref > 0) else 4.0
        intensity = min(abs(val) / denom, 1.0) * 0.55
        if val > 0: return f"background:rgba(74,222,128,{intensity:.2f});"
        return          f"background:rgba(248,113,113,{intensity:.2f});"

    # ── 가격 셀: 숫자=누적 등락률, 색상/히트맵=일별 변화율 기준 ──
    def pc_cell(cum: float | None, daily: float | None, is_base: bool = False, ref: float | None = None) -> str:
        if is_base:
            return f'<td style="{_TD};color:#475569;text-align:center;font-size:11px">기준</td>'
        if cum is None:
            return f'<td style="{_TD};font-size:12px;color:#475569">-</td>'
        color_src = daily if daily is not None else cum
        bg = _bg(color_src, ref)
        s  = "+" if cum >= 0 else ""
        return f'<td style="{_TD};{bg};font-size:12px;color:{_color_class(color_src)}">{s}{cum:.2f}%</td>'

    # ── 수급 셀: 숫자=누적 순매수, 색상/히트맵=일별 변화량 기준 ──
    def sc_cell(val: int | None, daily_val: int | None = None, ref: float | None = None, last: bool = False) -> str:
        td = _TDL if last else _TD
        if val is None:
            return f'<td style="{td};font-size:12px;color:#475569">-</td>'
        color_src = daily_val if daily_val is not None else val
        bg = _bg(float(color_src), ref)
        return f'<td style="{td};{bg};font-size:12px;color:{_color_class(color_src)}">{_fmt_k(val)}</td>'

    # ── SVG 차트 헬퍼 ────────────────────────────────────────
    _CW, _CH_L, _CH_B = 760, 54, 42  # chart width, line height, bar height
    _CP = 5                            # padding

    def _daily_from_cum(cum: dict) -> list[int | None]:
        """누적 수급 dict → 시간 순(9D→현재) 일별 값 리스트."""
        data_cols = [9, 8, 7, 6, 5, 4, 3, 2, 1, "현재"]
        result = []
        for i, c in enumerate(data_cols):
            curr = cum.get(c)
            if i == 0:
                result.append(curr)
            else:
                prev = cum.get(data_cols[i - 1])
                result.append((curr - prev) if (curr is not None and prev is not None) else curr)
        return result

    def _daily_dict(cum: dict) -> dict:
        """누적 수급 dict → 컬럼별 일별 변화량 dict (색상 기준용)."""
        data_cols = [9, 8, 7, 6, 5, 4, 3, 2, 1, "현재"]
        result: dict = {"10D": None}
        for i, c in enumerate(data_cols):
            curr = cum.get(c)
            if i == 0:
                result[c] = curr
            else:
                prev = cum.get(data_cols[i - 1])
                result[c] = (curr - prev) if (curr is not None and prev is not None) else curr
        return result

    def _line_svg(values: list[float | None]) -> str:
        """가격 라인 차트 SVG (누적 등락률, 기준=0%)."""
        n = len(values)
        indexed = [(i, v) for i, v in enumerate(values) if v is not None]
        if len(indexed) < 2:
            return ""
        all_v = [v for _, v in indexed]
        vmin = min(min(all_v), 0.0)
        vmax = max(max(all_v), 0.0)
        rng  = (vmax - vmin) or 1.0
        W, H, P = _CW, _CH_L, _CP

        def px(i): return P + i * (W - 2*P) / (n - 1)
        def py(v): return H - P - (v - vmin) / rng * (H - 2*P)

        base_y = py(0)
        line_pts  = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in indexed)
        # 면적 채우기 (기준선 기준)
        area_pts = (
            " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in indexed)
            + f" {px(indexed[-1][0]):.1f},{base_y:.1f}"
            + f" {px(indexed[0][0]):.1f},{base_y:.1f}"
        )
        trend_col = "#4ade80" if all_v[-1] >= 0 else "#f87171"

        svg = (
            f'<svg viewBox="0 0 {W} {H}" width="100%" height="{H}" xmlns="http://www.w3.org/2000/svg">'
            f'<line x1="{P}" y1="{base_y:.1f}" x2="{W-P}" y2="{base_y:.1f}" stroke="#2d3748" stroke-width="0.8" stroke-dasharray="3 2"/>'
            f'<polygon points="{area_pts}" fill="{trend_col}" opacity="0.12"/>'
            f'<polyline points="{line_pts}" fill="none" stroke="{trend_col}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for i, v in indexed:
            c = "#4ade80" if v >= 0 else "#f87171"
            svg += f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="2.5" fill="{c}"/>'
        svg += '</svg>'
        return svg

    def _bar_svg(values: list[int | None]) -> str:
        """수급 바 차트 SVG (일별 순매수)."""
        n = len(values)
        if not n:
            return ""
        valid = [v for v in values if v is not None]
        if not valid:
            return ""
        vmax_abs = max(abs(v) for v in valid) or 1
        W, H, P = _CW, _CH_B, _CP
        mid_y  = H / 2
        bar_w  = (W - 2*P) / n

        svg = (
            f'<svg viewBox="0 0 {W} {H}" width="100%" height="{H}" xmlns="http://www.w3.org/2000/svg">'
            f'<line x1="{P}" y1="{mid_y:.1f}" x2="{W-P}" y2="{mid_y:.1f}" stroke="#2d3748" stroke-width="0.8"/>'
        )
        for i, v in enumerate(values):
            if v is None:
                continue
            bar_h = max(abs(v) / vmax_abs * (mid_y - P), 1)
            bx = P + i * bar_w + 1
            bw = max(bar_w - 2, 1)
            color = "#4ade80" if v >= 0 else "#f87171"
            by    = (mid_y - bar_h) if v >= 0 else mid_y
            svg  += f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bar_h:.1f}" fill="{color}" opacity="0.85" rx="1"/>'
        svg += '</svg>'
        return svg

    def _chart_row(svg_html: str, bg: str, last: bool = False) -> str:
        td = _TDL if last else _TD
        return (
            f'<tr style="{bg}">'
            f'<td style="{td};color:#334155;font-size:10px"></td>'
            f'<td colspan="{len(COLS)}" style="{td};padding:3px 6px">{svg_html}</td>'
            f'</tr>'
        )

    # ── 컬럼 헤더 (구분 + 11개) ───────────────────────────────
    th_base = "padding:8px 10px;background:#1e2535;font-size:11px;white-space:nowrap"
    day_headers = "".join(
        f'<th style="{th_base};color:{"#475569" if c == "10D" else "#60a5fa"};'
        f'text-align:{"center" if c == "10D" else "right"}">'
        f'{col_label(c)}'
        f'{"<br><span style=\'font-size:9px;color:#334155;font-weight:400\'>누적↑&nbsp;일별↓</span>" if c == "10D" else ""}'
        f'</th>'
        for c in COLS
    )

    # ── 종목 행 생성 ──────────────────────────────────────────
    body_html = ""
    for i, row in enumerate(report_rows):
        bg_h = "background:#1a2235;" if i % 2 == 0 else "background:#161b27;"
        bg_d = "background:#12161f;" if i % 2 == 0 else "background:#0f1421;"

        pc       = row["price_chg_cum"]
        pd_daily = row["price_chg_daily"]
        oc       = row["orgn_cum"]
        fc       = row["for_cum"]

        # 일별 변화량 dict (색상/히트맵 기준)
        orgn_daily = _daily_dict(oc)
        for_daily  = _daily_dict(fc)

        # 정규화 기준값: 일별 변화량의 최대 절댓값 기준
        price_ref = max((abs(v) for v in pd_daily.values() if v is not None), default=None)
        orgn_ref  = max((abs(v) for v in orgn_daily.values() if v is not None), default=None)
        for_ref   = max((abs(v) for v in for_daily.values() if v is not None), default=None)

        pnl_amt   = row["pnl_amount"]
        pnl_rt    = row["pnl_rate"]
        pnl_color = _color_class(pnl_amt)
        pnl_sign  = "+" if (pnl_amt is not None and pnl_amt >= 0) else ""
        rt_sign   = "+" if (pnl_rt  is not None and pnl_rt  >= 0) else ""

        # ① 종목 헤더행 (full-width)
        body_html += f"""
        <tr style="{bg_h}border-top:2px solid #2d3748">
          <td colspan="{N_COLS}" style="padding:7px 14px;border-bottom:1px solid #1e2535">
            <span style="font-size:13px;font-weight:700;color:#f1f5f9">{row["stock_name"]}</span>
            <span style="font-size:11px;color:#475569;margin-left:6px">{row["stock_code"]}</span>
            <span style="float:right;font-size:12px;line-height:1.8">
              <span style="color:#64748b">현재가</span>
              <span style="color:#e2e8f0;font-weight:600;margin-left:5px">{row["current_price"]:,.0f}</span>
              <span style="color:#64748b;margin-left:16px">손익</span>
              <span style="color:{pnl_color};font-weight:600;margin-left:5px">{"매입단가 없음" if pnl_amt is None else f"{pnl_sign}{pnl_amt:,.0f}"}</span>
              {"" if pnl_rt is None else f'<span style="color:{pnl_color};margin-left:4px">({rt_sign}{pnl_rt:.2f}%)</span>'}
            </span>
          </td>
        </tr>"""

        DATA_COLS = [9, 8, 7, 6, 5, 4, 3, 2, 1, "현재"]

        # ② 가격행 + 라인 차트
        pc_cells = "".join(pc_cell(pc.get(c), pd_daily.get(c), is_base=(c == "10D"), ref=price_ref) for c in COLS)
        body_html += f"""
        <tr style="{bg_d}">
          <td style="{_TD};color:#60a5fa;font-size:11px;font-weight:700;white-space:nowrap">가격</td>
          {pc_cells}
        </tr>"""
        price_daily_vals = [pd_daily.get(c) for c in DATA_COLS]
        body_html += _chart_row(_bar_svg(price_daily_vals), bg_d)

        # ③ 기관행 + 바 차트
        oc_cells = "".join(sc_cell(oc.get(c), daily_val=orgn_daily.get(c), ref=orgn_ref) for c in COLS)
        body_html += f"""
        <tr style="{bg_d}">
          <td style="{_TD};color:#86efac;font-size:11px;font-weight:700;white-space:nowrap">기관</td>
          {oc_cells}
        </tr>"""
        body_html += _chart_row(_bar_svg(_daily_from_cum(oc)), bg_d)

        # ④ 외국인행 + 바 차트
        fc_cells = "".join(sc_cell(fc.get(c), daily_val=for_daily.get(c), ref=for_ref, last=True) for c in COLS)
        body_html += f"""
        <tr style="{bg_d}">
          <td style="{_TDL};color:#c084fc;font-size:11px;font-weight:700;white-space:nowrap">외국인</td>
          {fc_cells}
        </tr>"""
        body_html += _chart_row(_bar_svg(_daily_from_cum(fc)), bg_d, last=True)

        # ── 종목 간 여백 ─────────────────────────────────────
        body_html += f'<tr><td colspan="{N_COLS}" style="height:8px;background:#0f1117"></td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>보유종목 일일 리포트</title>
</head>
<body style="margin:0;padding:0;background:#0f1117;font-family:system-ui,-apple-system,sans-serif;color:#e2e8f0">
<div style="max-width:1400px;margin:0 auto;padding:24px 16px">

  <div style="margin-bottom:20px">
    <h1 style="margin:0 0 4px;font-size:20px;font-weight:700;color:#f1f5f9">보유종목 일일 리포트</h1>
    <p style="margin:0;font-size:13px;color:#64748b">{generated_at}</p>
  </div>

  <div style="display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap">
    <div style="background:#161b27;border:1px solid #1e2535;border-radius:8px;padding:14px 20px;flex:1;min-width:150px">
      <p style="margin:0 0 3px;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.05em">총 평가금액</p>
      <p style="margin:0;font-size:18px;font-weight:700;color:#f1f5f9">{total_eval:,.0f}<span style="font-size:12px;color:#94a3b8"> 원</span></p>
    </div>
    <div style="background:#161b27;border:1px solid #1e2535;border-radius:8px;padding:14px 20px;flex:1;min-width:150px">
      <p style="margin:0 0 3px;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.05em">총 손익 <span style="color:#334155;font-size:10px">(매입단가 있는 종목 기준)</span></p>
      <p style="margin:0;font-size:18px;font-weight:700;color:{total_pnl_color}">{total_sign}{total_pnl:,.0f}<span style="font-size:12px;color:#94a3b8"> 원</span></p>
      {"" if no_cost_cnt == 0 else f'<p style="margin:4px 0 0;font-size:11px;color:#64748b">매입단가 없음 {no_cost_cnt}종목 제외</p>'}
    </div>
    <div style="background:#161b27;border:1px solid #1e2535;border-radius:8px;padding:14px 20px;flex:1;min-width:150px">
      <p style="margin:0 0 3px;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.05em">보유 종목</p>
      <p style="margin:0;font-size:18px;font-weight:700;color:#f1f5f9">{len(report_rows)}<span style="font-size:12px;color:#94a3b8"> 종목</span></p>
    </div>
  </div>

  <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;background:#161b27;border-radius:8px;overflow:hidden">
      <thead>
        <tr>
          <th style="{th_base};color:#94a3b8;text-align:left">구분</th>
          {day_headers}
        </tr>
      </thead>
      <tbody>{body_html}
      </tbody>
    </table>
  </div>

  <p style="margin-top:24px;font-size:11px;color:#334155;text-align:center">
    키움 API 차트 분석 시스템 · 본 리포트는 참고용이며 투자 권유가 아닙니다.
  </p>
</div>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# 이메일 발송
# ---------------------------------------------------------------------------

def send_email(html: str, recipients: list[str]) -> bool:
    """Gmail SMTP로 HTML 이메일 발송."""
    smtp_cfg = config.smtp
    if not smtp_cfg.user or not smtp_cfg.password:
        logger.error("SMTP_USER / SMTP_PASSWORD 환경변수가 설정되지 않았습니다.")
        return False
    if not recipients:
        logger.warning("수신자 목록이 비어있습니다.")
        return False

    now_str = datetime.now().strftime("%Y-%m-%d")
    subject = f"[보유종목 리포트] {now_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_cfg.user
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_cfg.host, smtp_cfg.port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_cfg.user, smtp_cfg.password)
            server.sendmail(smtp_cfg.user, recipients, msg.as_string())
        logger.info("이메일 발송 완료: %s → %s", subject, recipients)
        return True
    except Exception as e:
        logger.error("이메일 발송 실패: %s", e)
        return False


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main(send: bool = False) -> str:
    """보유종목 리포트 생성 (HTML 반환). send=True이면 이메일도 발송."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    conn = psycopg2.connect(config.database_url, options="-c timezone=Asia/Seoul")
    try:
        # 보유종목 화면(manual_holdings)의 종목으로 리포트 생성
        holdings = get_manual_holdings(conn)

        if not holdings:
            logger.warning("보유종목이 없습니다.")
            return "<html><body><p>보유종목이 없습니다.</p></body></html>"

        # 수급 이력 조회 + 행 데이터 계산
        report_rows: list[dict] = []
        for h in holdings:
            history = get_supply_history(h["stock_code"], conn)
            row = build_stock_row(h, history)
            report_rows.append(row)

        # 손익금액 큰 순 정렬 (None은 맨 뒤)
        report_rows.sort(key=lambda r: r["pnl_amount"] if r["pnl_amount"] is not None else float("-inf"), reverse=True)

        generated_at = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
        html = generate_html(report_rows, generated_at)

        if send:
            recipients = get_recipients(conn)
            ok = send_email(html, recipients)
            status = "success" if ok else "failed"
            log_send(conn, recipients, len(report_rows), status)
    finally:
        conn.close()

    return html


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="보유종목 일일 리포트 생성 및 발송")
    parser.add_argument("--send", action="store_true", help="리포트 생성 후 이메일 발송")
    args = parser.parse_args()

    result_html = main(send=args.send)
    if not args.send:
        print(f"리포트 생성 완료 ({len(result_html):,} bytes)")
