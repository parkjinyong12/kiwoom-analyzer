# 화면별 기획서

> push 시 Claude Code가 자동 업데이트합니다.

---

## 대시보드 (dashboard)
전체 분석 현황 요약. 감시 종목 수, 오늘 신호·오류 건수, 최근 신호 목록, 30일 신호 통계를 표시합니다.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/dashboard` | 없음 | `watched_count`, `signals_today`, `errors_today`, `supply_alerts_today`, `recent_signals[]`, `signal_stats_30d` |

---

## 수급 현황 (supply)
종목별 외국인·기관 수급 추이 및 누적 차트. 종목 선택 → 기간 선택 → 차트 갱신.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/supply_demand/stocks` | 없음 | `[{stock_code, stock_name}]` |
| GET | `/api/supply_demand/{stock_code}` | URL: `stock_code` | `[{date, for_hold_ratio, for_chg_qty, for_net_qty, orgn_net_qty, ind_net_qty, cumul_orgn, cumul_for, close_price}]` |
| GET | `/api/supply_demand/summary` | 없음 | `collected_stocks`, `total_rows`, `avg_days_per_stock`, `watched_without_data` |

---

## 수급↑ 가격↔ (divergence)
수급은 증가하나 가격이 횡보 중인 종목 선별. 파라미터 슬라이더로 필터링.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/supply_divergence` | `window`(기본20), `price_th`(기본3.0), `ignore_ratio`(기본0.15) | 다이버전스 종목 배열 |

---

## 기간별 변화 (snapshot)
선택한 기간(1·3·5·10·20일 등) 기준 수급 변화량 상위 종목 비교.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/snapshot` | `periods`(콤마구분, 기본"1,3,5,10,20"), `watched_only`(기본"true") | 기간별 수급 스냅샷 배열 |

---

## 매매 신호 (signals)
전략 에이전트가 생성한 매수·관망·매도 신호 목록. 종목·날짜·유형별 필터 지원.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/signals` | 없음 | `[{ticker, signal, price, target_price, stop_loss, confidence, strategy, ts}]` |

---

## 보유종목 리포트 (report)
보유종목 가격·수급 변동 일일 리포트 미리보기 및 이메일 발송 관리.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/report/preview` | 없음 | `{ok, html}` |
| POST | `/api/report/send` | 없음 | `{ok}` |
| GET | `/api/report/config` | 없음 | `{emails[], smtp_configured, smtp_user, schedule}` |
| POST | `/api/report/config/email` | Body: `{email}` | `{ok}` |
| POST | `/api/report/config/email/{id}/toggle` | URL: `id` | `{ok}` |
| DELETE | `/api/report/config/email/{id}` | URL: `id` | `{ok}` |
| GET | `/api/report/history` | 없음 | `[{ts, recipients, stock_count, status, error_msg}]` |
| PUT | `/api/schedule/holdings_report` | Body: `{enabled, hour, minute, days}` | `{ok}` |

---

## 보유종목 (ext-holdings)
타사 증권사 보유종목 수동 입력 및 관리.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/manual_holdings` | 없음 | `[{id, brokerage, stock_code, stock_name, quantity, avg_price, memo, current_price, price_date}]` |
| POST | `/api/manual_holdings` | Body: `{brokerage, stock_code, stock_name, quantity, avg_price, memo}` | `{ok, id}` |
| PUT | `/api/manual_holdings/{id}` | URL: `id`, Body: 동일 | `{ok}` |
| DELETE | `/api/manual_holdings/{id}` | URL: `id` | `{ok}` |
| GET | `/api/settings` | 없음 | 앱 설정 딕셔너리 |

---

## 현재가 관리 (price-mgmt)
타사 보유종목 현재가 조회 및 수동 업데이트. 배치(`sync_prices`) 연동.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/price_sync/stocks` | 없음 | `[{stock_code, stock_name, current_price, price_date, fetched_at}]` |
| PUT | `/api/price_sync/manual` | Body: `{stock_code, price}` | `{ok, stock_code, price, date}` |
| GET | `/api/batch` | 없음 | 배치 실행 상태 확인용 |
| POST | `/api/batch/sync_prices/start` | 없음 | `{ok, log_file}` |

---

## 현금성 자산 (cash-assets)
현금·예수금·단기금융상품 등 비주식 자산 관리. 연동 자산은 자동 시세 동기화 지원.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/cash_assets` | 없음 | `{items[], total}` |
| POST | `/api/cash_assets` | Body: `{name, brokerage, quantity, unit_price, purchase_price, amount, link_type, link_key, note}` | `{ok}` |
| PUT | `/api/cash_assets/{id}` | URL: `id`, Body: 동일 | `{ok}` |
| DELETE | `/api/cash_assets/{id}` | URL: `id` | `{ok}` |
| POST | `/api/cash_assets/{id}/sync` | URL: `id` | `{ok, amount, unit_price}` |
| POST | `/api/cash_assets/sync_all` | 없음 | `{ok, updated, failed}` |

---

## 매크로 관리 (macro)
금리·환율 등 거시 지표 입력 및 네이버 자동 조회 동기화.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/macro_rates` | 없음 | `[{id, key, name, value, unit, updated_at}]` |
| POST | `/api/macro_rates` | Body: `{key, name, value, unit}` | `{ok}` |
| PUT | `/api/macro_rates/{id}` | URL: `id`, Body: `{name, value, unit}` | `{ok}` |
| DELETE | `/api/macro_rates/{id}` | URL: `id` | `{ok}` |
| POST | `/api/macro_rates/{id}/sync_naver` | URL: `id` | `{ok, value, key}` |

---

## 현금 대 주식 리밸런싱 (rebalance)
현금 비중 목표 대비 현재 비중 분석 및 리밸런싱 제안.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/rebalance` | 없음 | `{holdings[], portfolio_total, stock_total, total_cash, alert_up, alert_down, cash_target_ratio}` |
| PUT | `/api/rebalance/stock_setting` | Body: `{stock_code, target_ratio, alert_up, alert_down}` | `{ok}` |
| PUT | `/api/rebalance/target` | Body: `{stock_code, target_ratio}` | `{ok}` |

---

## 신용 관리 (credit)
신용 잔고 및 만기 일정 관리. 증권사별 신용 포지션 입력.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/credit_positions` | 없음 | `{positions[], broker_stock_eval, broker_cash_eval}` |
| POST | `/api/credit_positions` | Body: `{brokerage, purchase_amount, loan_amount, note}` | `{ok}` |
| PUT | `/api/credit_positions/{id}` | URL: `id`, Body: 동일 | `{ok}` |
| DELETE | `/api/credit_positions/{id}` | URL: `id` | `{ok}` |

---

## 주식 간 리밸런싱 (stock-rebalance)
보유 주식 종목 간 비중 재조정 시뮬레이션. `/api/rebalance` 동일 엔드포인트 사용.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/rebalance` | 없음 | 동일 (화면별 렌더링만 다름) |

---

## 테마 리밸런싱 (theme-rebalance)
테마·섹터 기준 포트폴리오 비중 관리.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/theme_rebalance` | 없음 | `{themes[], stocks[], portfolio_total, stock_total, total_cash, alert_up, alert_down}` |
| PUT | `/api/theme_rebalance/stock_themes` | Body: `{stock_code, themes[]}` | `{ok}` |
| PUT | `/api/theme_rebalance/theme_target` | Body: `{theme, target_ratio}` | `{ok}` |
| PUT | `/api/theme_rebalance/theme_alert` | Body: `{theme, alert_up, alert_down}` | `{ok}` |

---

## 정성 점수 (qualitative)
종목·이슈별 정성적 평가 점수 기록 및 이력 관리.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/qualitative/items` | 없음 | `[{id, name, category, description, latest_score, latest_date, prev_score, delta}]` |
| POST | `/api/qualitative/items` | Body: `{name, category, description}` | `{ok, id}` |
| PUT | `/api/qualitative/items/{id}` | URL: `id`, Body: 동일 | `{ok}` |
| DELETE | `/api/qualitative/items/{id}` | URL: `id` | `{ok}` |
| GET | `/api/qualitative/items/{id}/scores` | URL: `id` | `[{id, score, scored_at, comment, delta}]` |
| POST | `/api/qualitative/scores` | Body: `{item_id, score, scored_at, comment}` | `{ok, id}` |
| DELETE | `/api/qualitative/scores/{id}` | URL: `id` | `{ok}` |

---

## 감사 로그 (auditlog)
전체 에이전트 이벤트 로그 조회. 유형·상태 필터 지원.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/events` | 없음 | `[{ts, event_type, agent, ticker, status, detail}]` |

---

## 감시 종목 (stocks)
분석 대상 종목 목록 관리.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/stocks` | 없음 | `[{stock_code, stock_name, market_name, last_price, market_cap, fetched_at}]` |

---

## 배치 관리 (batch)
5개 백그라운드 배치 작업 실행 상태 모니터링 및 수동 제어. 로그 팝업 지원.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/batch` | 없음 | `[{id, name, desc, running, pid, last_line, log_file}]` |
| POST | `/api/batch/{job_id}/start` | URL: `job_id` | `{ok, log_file}` |
| POST | `/api/batch/{job_id}/stop` | URL: `job_id` | `{ok}` |
| GET | `/api/batch/{job_id}/logs` | URL: `job_id` | `[로그 라인 문자열]` (최근 200줄) |
| GET | `/api/schedule` | 없음 | `{job_id: {enabled, hour, minute, days, interval_mode, ...}}` |
| PUT | `/api/schedule/{job_id}` | URL: `job_id`, Body: `{enabled, hour, minute, days, interval_mode, interval_minutes, interval_start, interval_end}` | `{ok}` |
| GET | `/api/settings` | 없음 | `{min_market_cap, ...}` |

---

## 기획서 (spec)
현재 문서. SPEC.md / SCREEN_SPEC.md → DB 동기화 → Markdown 렌더링.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/spec` | 없음 | `{content, updated_at}` |
| GET | `/api/spec/screens` | 없음 | `{content, updated_at}` |
| GET | `/api/spec/apis` | 없음 | `[{path, methods[], endpoint, doc}]` |

---

## 공통코드 관리 (common-codes)
시스템 공통코드 그룹별 조회·수정.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/common_codes/{group}` | URL: `group` | `[{id, code, name, sort_order, active}]` |
| POST | `/api/common_codes/{group}` | URL: `group`, Body: `{code, name, sort_order}` | `{ok, id}` |
| PUT | `/api/common_codes/{id}` | URL: `id`, Body: `{name, sort_order}` | `{ok}` |
| POST | `/api/common_codes/{id}/toggle` | URL: `id` | `{ok}` |
| DELETE | `/api/common_codes/{id}` | URL: `id` | `{ok}` |

---

## 사용자 관리 (usermgmt)
로그인 계정 목록 및 사용자별 메뉴 접근 권한 관리.

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/users` | 없음 | `[{id, name, login_id, visible_menus[], supply_default_stock, supply_default_period}]` |
| POST | `/api/users` | Body: `{name}` | `{id, name, visible_menus, ...}` |
| PUT | `/api/users/{id}/credentials` | URL: `id`, Body: `{login_id, password}` | `{ok}` |
| PUT | `/api/users/{id}/preferences` | URL: `id`, Body: `{visible_menus[], supply_default_stock, supply_default_period}` | `{ok}` |
| DELETE | `/api/users/{id}` | URL: `id` | `{ok}` |
| GET | `/api/me` | 없음 (session 기반) | `{id, name, visible_menus[], ...}` |
