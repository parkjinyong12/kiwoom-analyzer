"""리밸런싱 알림 배치 스크립트.

5분마다 실행 → 신호 감지 → 이메일 발송 (신호 변화 시에만).

체크 항목:
  - 종목리밸런싱 매매추천내역에 매수/매도 종목이 1건 이상
  - 테마리밸런싱 전체 계획에 매수/매도 대상이 1건 이상
"""

import argparse
import hashlib
import logging
import math
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

# ── 프론트엔드와 동일한 상수 ──────────────────────────────────────────────────
RB_ALERT_ADJ  = 0.60   # 종목 리밸런싱: 리밸런싱 필요 시 편차의 60% 조정
RB_WATCH_ADJ  = 0.33   # 종목 리밸런싱: 주의 시 편차의 33% 조정
RB_MIN_AMT    = 50_000 # 종목 리밸런싱: 관망 최소금액 (5만원)

TRB_ALERT_ADJ = 0.40   # 테마 리밸런싱: 리밸런싱 필요 시 편차의 40% 조정
TRB_WATCH_ADJ = 0.20   # 테마 리밸런싱: 주의 시 편차의 20% 조정
TRB_MIN_AMT   = 50_000 # 테마 리밸런싱: 최소 조정금액 (5만원)


# ---------------------------------------------------------------------------
# DB 헬퍼
# ---------------------------------------------------------------------------

def _query(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _query_one(conn, sql, params=()):
    rows = _query(conn, sql, params)
    return rows[0] if rows else None


def _ensure_alert_log_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rebalance_alert_log (
                id               SERIAL PRIMARY KEY,
                sent_at          TIMESTAMP DEFAULT NOW(),
                signal_hash      VARCHAR(64) NOT NULL,
                stock_buy_cnt    INTEGER NOT NULL DEFAULT 0,
                stock_sell_cnt   INTEGER NOT NULL DEFAULT 0,
                theme_buy_cnt    INTEGER NOT NULL DEFAULT 0,
                theme_sell_cnt   INTEGER NOT NULL DEFAULT 0,
                recipients       TEXT,
                status           VARCHAR(20),
                error_msg        TEXT
            )
        """)
    conn.commit()


def _get_all_uids(conn) -> list[int]:
    """보유종목이 있는 모든 사용자 ID 반환.

    user_id=NULL 레코드는 미이주 데이터이므로 MIN(id) 사용자에게 귀속.
    """
    rows = _query(conn,
        "SELECT DISTINCT user_id FROM manual_holdings WHERE quantity > 0 AND user_id IS NOT NULL")
    uids = [int(r["user_id"]) for r in rows]

    # NULL user_id 레코드가 있으면 MIN(id) 사용자에게 포함
    null_row = _query_one(conn,
        "SELECT COUNT(*) AS cnt FROM manual_holdings WHERE quantity > 0 AND user_id IS NULL")
    if null_row and int(null_row["cnt"]) > 0:
        fallback = _query_one(conn, "SELECT MIN(id) AS uid FROM users")
        if fallback and fallback["uid"] is not None:
            fb_uid = int(fallback["uid"])
            if fb_uid not in uids:
                uids.append(fb_uid)

    return sorted(uids)


def _get_settings(conn, uid) -> dict:
    try:
        rows = _query(conn, "SELECT key, value FROM app_settings")
        settings = {r["key"]: r["value"] for r in rows}
    except Exception:
        settings = {}
    if uid:
        try:
            rows = _query(conn,
                "SELECT key, value FROM user_preferences WHERE user_id = %s",
                (uid,))
            for r in rows:
                settings[r["key"]] = r["value"]
        except Exception:
            pass
    return settings


def _get_total_cash(conn, uid) -> int:
    try:
        rows = _query(conn,
            "SELECT COALESCE(SUM(amount), 0) AS total FROM cash_assets WHERE user_id = %s",
            (uid,))
        total = int(rows[0]["total"]) if rows else 0
        if total > 0:
            return total
    except Exception:
        pass
    try:
        settings = _get_settings(conn, uid)
        return sum(int(v or 0) for k, v in settings.items() if k.startswith("portfolio_cash_"))
    except Exception:
        return 0


def get_recipients_for_user(conn, uid: int) -> list[str]:
    """사용자별 리밸런싱 알림 수신자 조회 (user_alert_emails 테이블).

    설정된 수신자가 없으면 report_email_config(보유종목 리포트 수신자)로 폴백.
    """
    try:
        rows = _query(conn,
            "SELECT email FROM user_alert_emails WHERE user_id = %s AND active = TRUE",
            (uid,))
        if rows:
            return [r["email"] for r in rows]
    except Exception:
        pass
    # 폴백: 보유종목 리포트 수신자
    try:
        rows = _query(conn, "SELECT email FROM report_email_config WHERE active = TRUE")
        return [r["email"] for r in rows]
    except Exception as e:
        logger.warning("수신자 목록 조회 실패 (uid=%d): %s", uid, e)
        return []


# ---------------------------------------------------------------------------
# 종목리밸런싱 신호 계산
# ---------------------------------------------------------------------------

def get_stock_signals(conn, uid: int) -> tuple[list[dict], list[dict]]:
    """종목리밸런싱 매매추천내역의 매수/매도 종목 반환.

    프론트엔드 stock-rebalance 페이지의 추천 로직과 동일.
    base = stock_total (주식 간 리밸런싱).
    """
    rows = _query(conn, """
        WITH latest_close AS (
            SELECT DISTINCT ON (stock_code)
                stock_code, close_price
            FROM supply_demand
            WHERE close_price IS NOT NULL AND close_price > 0
            ORDER BY stock_code, date DESC
        ),
        holdings_agg AS (
            SELECT
                stock_code,
                MAX(stock_name)                                          AS stock_name,
                SUM(quantity)                                            AS total_qty,
                SUM(quantity * avg_price) / NULLIF(SUM(quantity), 0)    AS weighted_avg_price
            FROM manual_holdings
            WHERE user_id = %s
            GROUP BY stock_code
        )
        SELECT
            ha.stock_code,
            ha.stock_name,
            ha.total_qty,
            ROUND(ha.weighted_avg_price::NUMERIC, 0)    AS avg_price,
            COALESCE(
                lc.close_price,
                CASE WHEN st.last_price ~ '^[0-9]+$' THEN st.last_price::BIGINT ELSE NULL END
            )                                            AS current_price,
            COALESCE(rt.target_ratio, 0)                AS target_ratio,
            rt.forward_per, rt.fair_per,
            rt.forward_eps, rt.eps_growth_rate,
            rt.alert_up, rt.alert_down, rt.watch_up, rt.watch_down
        FROM holdings_agg ha
        LEFT JOIN latest_close lc ON lc.stock_code = ha.stock_code
        LEFT JOIN stocks st ON st.stock_code = ha.stock_code
        LEFT JOIN rebalance_targets rt
               ON rt.stock_code = ha.stock_code AND rt.user_id = %s
        ORDER BY ha.stock_name
    """, (uid, uid))

    settings     = _get_settings(conn, uid)
    g_alert_up   = float(settings.get("rebalance_alert_up",   30))
    g_alert_down = float(settings.get("rebalance_alert_down", 25))
    g_watch_up   = float(settings.get("rebalance_watch_up",   round(g_alert_up  * 0.5, 1)))
    g_watch_down = float(settings.get("rebalance_watch_down", round(g_alert_down * 0.5, 1)))

    stock_total = 0
    holdings = []
    for r in rows:
        qty       = int(r["total_qty"] or 0)
        cur_price = r["current_price"]
        avg_price = float(r["avg_price"] or 0)
        eval_amt  = qty * (int(cur_price) if cur_price is not None else avg_price)
        stock_total += eval_amt
        holdings.append({
            "stock_code":    r["stock_code"],
            "stock_name":    r["stock_name"],
            "total_qty":     qty,
            "current_price": int(cur_price) if cur_price is not None else None,
            "eval_amt":      round(eval_amt),
            "target_ratio":  float(r["target_ratio"] or 0),
            "forward_eps":      float(r["forward_eps"])     if r["forward_eps"]     is not None else None,
            "eps_growth_rate":  float(r["eps_growth_rate"]) if r["eps_growth_rate"] is not None else None,
            # forward_per: 저장값 우선, 없으면 현재가/선행EPS로 대체
            "forward_per":   float(r["forward_per"]) if r["forward_per"] is not None
                             else None,  # _apply_per_adjustment 에서 fallback 처리
            "fair_per":      float(r["fair_per"])    if r["fair_per"]    is not None else None,
            "alert_up":      float(r["alert_up"])   if r["alert_up"]   is not None else None,
            "alert_down":    float(r["alert_down"]) if r["alert_down"] is not None else None,
            "watch_up":      float(r["watch_up"])   if r["watch_up"]   is not None else None,
            "watch_down":    float(r["watch_down"]) if r["watch_down"] is not None else None,
        })

    buy_items, sell_items = [], []

    for r in holdings:
        tgt       = r["target_ratio"]
        cur_price = r["current_price"]
        if not tgt or cur_price is None:
            continue

        cur     = r["eval_amt"] / stock_total * 100 if stock_total > 0 else 0.0
        rel_dev = (cur - tgt) / tgt * 100

        a_up   = r["alert_up"]   if r["alert_up"]   is not None else g_alert_up
        a_down = r["alert_down"] if r["alert_down"] is not None else g_alert_down
        w_up   = r["watch_up"]   if r["watch_up"]   is not None else g_watch_up
        w_down = r["watch_down"] if r["watch_down"] is not None else g_watch_down

        is_alert = rel_dev > a_up or rel_dev < -a_down
        is_watch = not is_alert and (rel_dev > w_up or rel_dev < -w_down)
        if not is_alert and not is_watch:
            continue

        adj_ratio    = RB_ALERT_ADJ if is_alert else RB_WATCH_ADJ
        target_eval  = stock_total * tgt / 100
        partial_diff = (target_eval - r["eval_amt"]) * adj_ratio
        shares       = math.floor(abs(partial_diff) / cur_price)
        adj_amt      = shares * cur_price

        if shares == 0 or adj_amt < RB_MIN_AMT:
            continue

        item = {
            "tier":          "alert" if is_alert else "watch",
            "stock_code":    r["stock_code"],
            "stock_name":    r["stock_name"],
            "current_price": cur_price,
            "current_ratio": round(cur, 2),
            "target_ratio":  tgt,
            "rel_dev":       round(rel_dev, 1),
            "shares":        shares,
            "adj_amt":       adj_amt,
        }
        if partial_diff > 0:
            buy_items.append(item)
        else:
            sell_items.append(item)

    buy_items.sort( key=lambda x: x["adj_amt"], reverse=True)
    sell_items.sort(key=lambda x: x["adj_amt"], reverse=True)
    return buy_items, sell_items


# ---------------------------------------------------------------------------
# 테마리밸런싱 신호 계산
# ---------------------------------------------------------------------------

def get_theme_signals(conn, uid: int) -> tuple[list[dict], list[dict]]:
    """테마리밸런싱 전체 계획의 매수/매도 행동 아이템 반환.

    프론트엔드 trbRenderFullPlan() 로직과 동일.
    """
    rows = _query(conn, """
        WITH latest_close AS (
            SELECT DISTINCT ON (stock_code)
                stock_code, close_price
            FROM supply_demand
            WHERE close_price IS NOT NULL AND close_price > 0
            ORDER BY stock_code, date DESC
        ),
        holdings_agg AS (
            SELECT
                stock_code,
                MAX(stock_name)                                          AS stock_name,
                SUM(quantity)                                            AS total_qty,
                SUM(quantity * avg_price) / NULLIF(SUM(quantity), 0)    AS weighted_avg_price
            FROM manual_holdings
            WHERE user_id = %s
            GROUP BY stock_code
        )
        SELECT
            ha.stock_code,
            ha.stock_name,
            ha.total_qty,
            ROUND(ha.weighted_avg_price::NUMERIC, 0)    AS avg_price,
            COALESCE(
                lc.close_price,
                CASE WHEN st.last_price ~ '^[0-9]+$' THEN st.last_price::BIGINT ELSE NULL END
            )                                            AS current_price,
            COALESCE(sth.themes, '')                    AS themes
        FROM holdings_agg ha
        LEFT JOIN latest_close lc ON lc.stock_code = ha.stock_code
        LEFT JOIN stocks st ON st.stock_code = ha.stock_code
        LEFT JOIN stock_themes sth ON sth.stock_code = ha.stock_code
        ORDER BY ha.stock_name
    """, (uid,))

    settings     = _get_settings(conn, uid)
    g_alert_up   = float(settings.get("rebalance_alert_up",   30))
    g_alert_down = float(settings.get("rebalance_alert_down", 25))

    # 개별 종목 리밸런싱 목표 비중 (rb_dev_rel 계산용) + PER 데이터
    rb_rows  = _query(conn,
        "SELECT stock_code, target_ratio FROM rebalance_targets WHERE user_id = %s AND target_ratio > 0",
        (uid,))
    rb_tgts  = {r["stock_code"]: float(r["target_ratio"]) for r in rb_rows}

    stock_total = 0
    stocks = []
    for r in rows:
        qty       = int(r["total_qty"] or 0)
        cur_price = r["current_price"]
        avg_price = float(r["avg_price"] or 0)
        eval_amt  = qty * (int(cur_price) if cur_price is not None else avg_price)
        stock_total += eval_amt
        themes_list = [t.strip() for t in (r["themes"] or "").split(",") if t.strip()]
        stocks.append({
            "stock_code":    r["stock_code"],
            "stock_name":    r["stock_name"],
            "current_price": int(cur_price) if cur_price is not None else 0,
            "eval_amt":      round(eval_amt),
            "themes":        themes_list,
        })

    # stock_total 기준 current_ratio + rb_dev_rel
    for s in stocks:
        cur_ratio = s["eval_amt"] / stock_total * 100 if stock_total > 0 else 0.0
        s["current_ratio"] = round(cur_ratio, 2)
        rb_tgt = rb_tgts.get(s["stock_code"])
        s["rb_dev_rel"] = round((cur_ratio - rb_tgt) / rb_tgt * 100, 1) if rb_tgt and rb_tgt > 0 else None

    # 테마별 집계
    theme_data: dict[str, dict] = {}
    for s in stocks:
        bucket = s["themes"] if s["themes"] else ["__untagged__"]
        n = len(bucket)
        for t in bucket:
            if t not in theme_data:
                theme_data[t] = {"eval_amt": 0.0, "stocks": []}
            theme_data[t]["eval_amt"] += s["eval_amt"] / n
            theme_data[t]["stocks"].append(s)

    # 테마별 목표 비중
    tgt_rows = _query(conn,
        "SELECT theme, target_ratio, alert_up, alert_down FROM theme_targets WHERE user_id = %s",
        (uid,))
    targets = {r["theme"]: {
        "target_ratio": float(r["target_ratio"]),
        "alert_up":     float(r["alert_up"])   if r["alert_up"]   is not None else None,
        "alert_down":   float(r["alert_down"]) if r["alert_down"] is not None else None,
    } for r in tgt_rows}

    stock_map = {s["stock_code"]: s for s in stocks}

    buy_plans, sell_plans = [], []

    for tname, data in theme_data.items():
        if tname == "__untagged__":
            continue
        tdata     = targets.get(tname, {})
        tgt_ratio = tdata.get("target_ratio", 0)
        if not tgt_ratio:
            continue

        cur_ratio = data["eval_amt"] / stock_total * 100 if stock_total > 0 else 0.0
        dev_pp    = cur_ratio - tgt_ratio
        dev_rel   = dev_pp / tgt_ratio * 100 if tgt_ratio > 0 else None
        if dev_rel is None:
            continue

        a_up   = tdata.get("alert_up")   if tdata.get("alert_up")   is not None else g_alert_up
        a_down = tdata.get("alert_down") if tdata.get("alert_down") is not None else g_alert_down
        w_up   = a_up   * 0.5
        w_down = a_down * 0.5

        if   dev_rel > a_up:    tier, adj_r, direction = "alert", TRB_ALERT_ADJ, "sell"
        elif dev_rel > w_up:    tier, adj_r, direction = "watch", TRB_WATCH_ADJ, "sell"
        elif dev_rel < -a_down: tier, adj_r, direction = "alert", TRB_ALERT_ADJ, "buy"
        elif dev_rel < -w_down: tier, adj_r, direction = "watch", TRB_WATCH_ADJ, "buy"
        else:
            continue

        excess_amt = abs(dev_pp) / 100 * stock_total
        adj_amt    = excess_amt * adj_r

        # 적합 종목 선별 (rb_dev_rel 충돌 방지)
        theme_stocks = [
            stock_map[s["stock_code"]]
            for s in data["stocks"]
            if s["stock_code"] in stock_map and stock_map[s["stock_code"]]["current_price"] > 0
        ]
        if direction == "sell":
            eligible = [s for s in theme_stocks if s["rb_dev_rel"] is None or s["rb_dev_rel"] >= 0]
            eligible.sort(key=lambda x: (x["rb_dev_rel"] or 0), reverse=True)
        else:
            eligible = [s for s in theme_stocks if s["rb_dev_rel"] is None or s["rb_dev_rel"] <= 0]
            eligible.sort(key=lambda x: (x["rb_dev_rel"] or 0))

        if adj_amt < TRB_MIN_AMT or not eligible:
            continue

        eligible_eval = sum(s["eval_amt"] for s in eligible) or 1
        trades = []
        for s in eligible:
            weight = s["eval_amt"] / eligible_eval
            alloc  = adj_amt * weight
            sh     = math.floor(alloc / s["current_price"])
            amt    = sh * s["current_price"]
            if sh > 0:
                trades.append({
                    "stock_code":    s["stock_code"],
                    "stock_name":    s["stock_name"],
                    "current_price": s["current_price"],
                    "shares":        sh,
                    "amt":           amt,
                })

        actual_amt = sum(t["amt"] for t in trades)
        if actual_amt < TRB_MIN_AMT:
            continue

        plan = {
            "tier":          tier,
            "theme":         tname,
            "direction":     direction,
            "cur_ratio":     round(cur_ratio, 2),
            "target_ratio":  tgt_ratio,
            "dev_rel":       round(dev_rel, 1),
            "adj_amt":       round(adj_amt),
            "actual_amt":    round(actual_amt),
            "trades":        trades,
        }
        if direction == "buy":
            buy_plans.append(plan)
        else:
            sell_plans.append(plan)

    buy_plans.sort( key=lambda x: (x["tier"] != "alert", x["dev_rel"]))
    sell_plans.sort(key=lambda x: (x["tier"] != "alert", -x["dev_rel"]))
    return buy_plans, sell_plans


# ---------------------------------------------------------------------------
# 중복 방지 (신호 해시)
# ---------------------------------------------------------------------------

def _build_signal_hash(sb, ss, tb, ts) -> str:
    """현재 신호 세트의 SHA-256 해시."""
    parts = (
        sorted(f"rb_buy:{x['stock_code']}" for x in sb)
        + sorted(f"rb_sell:{x['stock_code']}" for x in ss)
        + sorted(f"trb_buy:{x['theme']}" for x in tb)
        + sorted(f"trb_sell:{x['theme']}" for x in ts)
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _get_last_hash(conn) -> str | None:
    try:
        row = _query_one(conn,
            "SELECT signal_hash FROM rebalance_alert_log ORDER BY sent_at DESC LIMIT 1")
        return row["signal_hash"] if row else None
    except Exception:
        return None


def _save_log(conn, signal_hash, sb, ss, tb, ts, recipients, status, error_msg=None):
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO rebalance_alert_log
                    (signal_hash, stock_buy_cnt, stock_sell_cnt, theme_buy_cnt, theme_sell_cnt,
                     recipients, status, error_msg)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (signal_hash, len(sb), len(ss), len(tb), len(ts),
                  ",".join(recipients), status, error_msg))
        conn.commit()
    except Exception as e:
        logger.warning("알림 로그 저장 실패: %s", e)


# ---------------------------------------------------------------------------
# HTML 이메일 생성
# ---------------------------------------------------------------------------

def _fmt_won(n: int) -> str:
    return f"{n:,}원"


def _tier_badge(tier: str) -> str:
    if tier == "alert":
        return '<span style="background:#450a0a;color:#f87171;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700">리밸런싱필요</span>'
    return '<span style="background:#431407;color:#fb923c;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700">주의</span>'


def generate_html(stock_buy, stock_sell, theme_buy, theme_sell, generated_at: str) -> str:
    _BG   = "background:#0f1117"
    _CARD = "background:#161b27;border:1px solid #1e2535;border-radius:8px;padding:16px 20px;margin-bottom:16px"
    _TH   = "padding:8px 12px;background:#1e2535;font-size:11px;color:#94a3b8;text-align:left;font-weight:600"
    _TD   = "padding:8px 12px;font-size:13px;border-bottom:1px solid #1e2535"

    def section(title, color, items_html):
        return f"""
        <div style="{_CARD}">
          <h3 style="margin:0 0 14px;font-size:15px;font-weight:700;color:{color}">{title}</h3>
          {items_html}
        </div>"""

    def stock_table(items, direction):
        if not items:
            return '<p style="color:#64748b;font-size:13px;margin:0">해당 없음</p>'
        color = "#60a5fa" if direction == "buy" else "#f87171"
        label = "매수" if direction == "buy" else "매도"
        rows = "".join(
            f'<tr>'
            f'<td style="{_TD}">{_tier_badge(x["tier"])}</td>'
            f'<td style="{_TD};font-weight:600;color:#f1f5f9">{x["stock_name"]}</td>'
            f'<td style="{_TD};color:#64748b">{x["stock_code"]}</td>'
            f'<td style="{_TD};text-align:right">{x["current_price"]:,}원</td>'
            f'<td style="{_TD};text-align:right;color:#94a3b8">{x["current_ratio"]:.1f}% → {x["target_ratio"]:.1f}%</td>'
            f'<td style="{_TD};text-align:right;color:{"#f87171" if x["rel_dev"] > 0 else "#60a5fa"}">'
            f'{"+" if x["rel_dev"] >= 0 else ""}{x["rel_dev"]:.1f}%</td>'
            f'<td style="{_TD};text-align:right;color:{color};font-weight:600">{label} {x["shares"]:,}주</td>'
            f'<td style="{_TD};text-align:right;color:{color}">{_fmt_won(x["adj_amt"])}</td>'
            f'</tr>'
            for x in items
        )
        return f"""
        <table style="width:100%;border-collapse:collapse">
          <thead><tr>
            <th style="{_TH}">등급</th>
            <th style="{_TH}">종목명</th>
            <th style="{_TH}">코드</th>
            <th style="{_TH};text-align:right">현재가</th>
            <th style="{_TH};text-align:right">비중</th>
            <th style="{_TH};text-align:right">편차</th>
            <th style="{_TH};text-align:right">추천</th>
            <th style="{_TH};text-align:right">조정금액</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>"""

    def theme_table(plans, direction):
        if not plans:
            return '<p style="color:#64748b;font-size:13px;margin:0">해당 없음</p>'
        color = "#60a5fa" if direction == "buy" else "#f87171"
        label = "매수" if direction == "buy" else "매도"
        rows_html = ""
        for p in plans:
            trade_str = ", ".join(
                f'{t["stock_name"]} {t["shares"]:,}주({_fmt_won(t["amt"])})'
                for t in p["trades"]
            )
            rows_html += (
                f'<tr>'
                f'<td style="{_TD}">{_tier_badge(p["tier"])}</td>'
                f'<td style="{_TD};font-weight:700;color:#f1f5f9">{p["theme"]}</td>'
                f'<td style="{_TD};text-align:right;color:#94a3b8">'
                f'{p["cur_ratio"]:.1f}% → {p["target_ratio"]:.1f}%</td>'
                f'<td style="{_TD};text-align:right;color:{"#f87171" if p["dev_rel"] > 0 else "#60a5fa"}">'
                f'{"+" if p["dev_rel"] >= 0 else ""}{p["dev_rel"]:.1f}%</td>'
                f'<td style="{_TD};text-align:right;color:{color};font-weight:600">'
                f'{label} {_fmt_won(p["actual_amt"])}</td>'
                f'<td style="{_TD};font-size:12px;color:#94a3b8">{trade_str}</td>'
                f'</tr>'
            )
        return f"""
        <table style="width:100%;border-collapse:collapse">
          <thead><tr>
            <th style="{_TH}">등급</th>
            <th style="{_TH}">테마</th>
            <th style="{_TH};text-align:right">비중</th>
            <th style="{_TH};text-align:right">편차</th>
            <th style="{_TH};text-align:right">조정금액</th>
            <th style="{_TH}">대상 종목</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>"""

    has_stock = bool(stock_buy or stock_sell)
    has_theme = bool(theme_buy or theme_sell)

    stock_section = ""
    if has_stock:
        buy_html  = stock_table(stock_buy,  "buy")
        sell_html = stock_table(stock_sell, "sell")
        stock_section = section(
            f"📈 종목리밸런싱 매매추천 "
            f"(매수 {len(stock_buy)}종목 / 매도 {len(stock_sell)}종목)",
            "#60a5fa",
            f'<p style="font-size:12px;font-weight:600;color:#60a5fa;margin:0 0 8px">▲ 매수 대상</p>{buy_html}'
            f'<p style="font-size:12px;font-weight:600;color:#f87171;margin:16px 0 8px">▼ 매도 대상</p>{sell_html}',
        )

    theme_section = ""
    if has_theme:
        buy_html  = theme_table(theme_buy,  "buy")
        sell_html = theme_table(theme_sell, "sell")
        theme_section = section(
            f"🏷️ 테마리밸런싱 전체 계획 "
            f"(매수 {len(theme_buy)}건 / 매도 {len(theme_sell)}건)",
            "#a78bfa",
            f'<p style="font-size:12px;font-weight:600;color:#60a5fa;margin:0 0 8px">▲ 매수 대상</p>{buy_html}'
            f'<p style="font-size:12px;font-weight:600;color:#f87171;margin:16px 0 8px">▼ 매도 대상</p>{sell_html}',
        )

    total_buy_cnt  = len(stock_buy)  + len(theme_buy)
    total_sell_cnt = len(stock_sell) + len(theme_sell)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>리밸런싱 알림</title>
</head>
<body style="margin:0;padding:0;{_BG};font-family:system-ui,-apple-system,sans-serif;color:#e2e8f0">
<div style="max-width:1000px;margin:0 auto;padding:24px 16px">

  <div style="margin-bottom:20px">
    <h1 style="margin:0 0 4px;font-size:20px;font-weight:700;color:#f1f5f9">리밸런싱 알림</h1>
    <p style="margin:0;font-size:13px;color:#64748b">{generated_at}</p>
  </div>

  <div style="display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap">
    <div style="{_CARD};flex:1;min-width:120px;padding:14px 20px">
      <p style="margin:0 0 3px;font-size:11px;color:#64748b">매수 신호</p>
      <p style="margin:0;font-size:20px;font-weight:700;color:#60a5fa">{total_buy_cnt}<span style="font-size:12px;color:#94a3b8"> 건</span></p>
    </div>
    <div style="{_CARD};flex:1;min-width:120px;padding:14px 20px">
      <p style="margin:0 0 3px;font-size:11px;color:#64748b">매도 신호</p>
      <p style="margin:0;font-size:20px;font-weight:700;color:#f87171">{total_sell_cnt}<span style="font-size:12px;color:#94a3b8"> 건</span></p>
    </div>
  </div>

  {stock_section}
  {theme_section}

  <p style="margin-top:24px;font-size:11px;color:#334155;text-align:center">
    키움 API 차트 분석 시스템 · 본 알림은 참고용이며 투자 권유가 아닙니다.
  </p>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 이메일 발송
# ---------------------------------------------------------------------------

def send_email(html: str, recipients: list[str], stock_buy, stock_sell, theme_buy, theme_sell) -> bool:
    smtp_cfg = config.smtp
    if not smtp_cfg.user or not smtp_cfg.password:
        logger.error("SMTP_USER / SMTP_PASSWORD 환경변수 미설정")
        return False
    if not recipients:
        logger.warning("수신자 목록이 비어있습니다.")
        return False

    parts = []
    if stock_buy:  parts.append(f"종목매수{len(stock_buy)}건")
    if stock_sell: parts.append(f"종목매도{len(stock_sell)}건")
    if theme_buy:  parts.append(f"테마매수{len(theme_buy)}건")
    if theme_sell: parts.append(f"테마매도{len(theme_sell)}건")
    subject = f"[리밸런싱 알림] {', '.join(parts)} ({datetime.now().strftime('%m/%d %H:%M')})"

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

def main(force: bool = False, dry_run: bool = False) -> None:
    """리밸런싱 알림 실행.

    Args:
        force:   신호 변화가 없어도 강제 발송.
        dry_run: 신호 계산만 수행하고 이메일은 발송하지 않음.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = psycopg2.connect(config.database_url, options="-c timezone=Asia/Seoul")
    try:
        _ensure_alert_log_table(conn)

        uids = _get_all_uids(conn)
        if not uids:
            logger.warning("보유종목이 있는 사용자가 없습니다.")
            return

        # 사용자별 신호 계산 → 사용자별 수신자로 각각 발송
        generated_at = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
        any_sent     = False

        for uid in uids:
            sb, ss = get_stock_signals(conn, uid)
            tb, ts = get_theme_signals(conn, uid)
            logger.info(
                "uid=%d: 종목매수=%d 종목매도=%d 테마매수=%d 테마매도=%d",
                uid, len(sb), len(ss), len(tb), len(ts),
            )

            total = len(sb) + len(ss) + len(tb) + len(ts)
            if total == 0:
                logger.info("uid=%d: 신호 없음 — 건너뜀", uid)
                continue

            cur_hash  = _build_signal_hash(sb, ss, tb, ts)
            last_hash = _get_last_hash(conn)

            if not force and cur_hash == last_hash:
                logger.info("uid=%d: 신호 변화 없음 (hash=%s…) — 발송 생략", uid, cur_hash[:8])
                continue

            html       = generate_html(sb, ss, tb, ts, generated_at)

            if dry_run:
                logger.info("uid=%d: dry-run 모드 — 발송 생략", uid)
                print(html[:300], "...")
                continue

            recipients = get_recipients_for_user(conn, uid)
            ok         = send_email(html, recipients, sb, ss, tb, ts)
            status     = "success" if ok else "failed"
            _save_log(conn, cur_hash, sb, ss, tb, ts, recipients, status)
            any_sent   = True

        if not any_sent and not dry_run:
            logger.info("모든 사용자 발송 조건 미충족 — 종료")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="리밸런싱 알림 메일 발송")
    parser.add_argument("--force",   action="store_true", help="신호 변화가 없어도 강제 발송")
    parser.add_argument("--dry-run", action="store_true", help="신호 계산만 수행 (메일 미발송)")
    args = parser.parse_args()
    main(force=args.force, dry_run=args.dry_run)
