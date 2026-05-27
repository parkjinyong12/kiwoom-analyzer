# 화면별 기획서

> push 시 Claude Code가 자동 업데이트합니다.

---

## 대시보드 (dashboard)

**개요**  
앱 전체 현황을 한눈에 파악하는 홈 화면. 별도 조작 없이 진입 즉시 자동 로딩된다.

**레이아웃 구성**

```
[ 요약 카드 × 4 ]  감시 종목 수 | 오늘 신호 | 수급 경보 | 오늘 에러
─────────────────────────────────────────────────────────────────
[ 최근 매매 신호 테이블 ]        [ 30일 신호 분포 파이차트 ]
  시간·종목·신호·가격·신뢰도       BUY / HOLD / SELL 비율
```

**주요 기능**
- 진입 시 `loadDashboard()` 자동 호출 → 4개 요약 지표 + 최근 신호 목록 표시
- 파이차트(`canvas#signal-pie`)에 30일 신호 통계를 Chart.js로 렌더링
- 버튼 없음 (읽기 전용 화면)

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/dashboard` | 없음 | `watched_count`, `signals_today`, `errors_today`, `supply_alerts_today`, `recent_signals[]`, `signal_stats_30d` |

---

## 수급 현황 (supply)

**개요**  
종목별 외국인·기관 수급 추이를 다중 차트로 시각화한다. 종목 선택 → 기간 선택 → 차트 갱신 순서로 조작.

**레이아웃 구성**

```
[ 요약 카드 × 4 ]  수집 완료 종목 | 총 수집 행수 | 종목당 평균 | 미수집 종목
─────────────────────────────────────────────────────────────────
[ 종목 자동완성 input ]  [ 기간 버튼: 2W / 1M / 3M / 6M / 9M / 1Y / 1.5Y / 2Y / MAX ]
─────────────────────────────────────────────────────────────────
[ 가격 vs 누적수급 차트 ]    [ 외국인 누적 차트 ]
[ 기관 누적 차트       ]    [ 외국인 일별 차트 ]
[ 기관 일별 차트       ]
─────────────────────────────────────────────────────────────────
[ 일별 수급 상세 테이블 ]
```

**주요 기능**
- 종목 검색: `input#supply-stock-input` 입력 시 자동완성 드롭다운 표시
- 기간 버튼 클릭(`setPeriod(el, days)`) → `hidden#supply-stock-code` 코드 기준으로 API 재호출 및 차트 재렌더링
- 차트 5개(Chart.js): 가격/외국인누적/기관누적/외국인일별/기관일별 동시 갱신
- 진입 시 `loadSupplySummary()` 로 요약 카드 자동 로드

**폼 입력**
| 요소 | 용도 |
|------|------|
| `input#supply-stock-input` | 종목명 자동완성 검색 |
| `hidden#supply-stock-code` | 선택된 종목 코드 보관 |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/supply_demand/stocks` | 없음 | `[{stock_code, stock_name}]` |
| GET | `/api/supply_demand/{stock_code}` | URL: `stock_code` | `[{date, for_hold_ratio, for_chg_qty, for_net_qty, orgn_net_qty, ind_net_qty, cumul_orgn, cumul_for, close_price}]` |
| GET | `/api/supply_demand/summary` | 없음 | `collected_stocks`, `total_rows`, `avg_days_per_stock`, `watched_without_data` |

---

## 수급↑ 가격↔ (divergence)

**개요**  
수급은 증가하는데 가격이 횡보 중인 종목을 추려낸다. 세 가지 파라미터를 조정해 원하는 조건의 종목을 탐색.

**레이아웃 구성**

```
[ 조회 기간 select ]  [ 가격 상승 허용 select ]  [ 작은 수급 무시 select ]  [ 조회 버튼 ]
─────────────────────────────────────────────────────────────────
[ 결과 카운트 ]
[ 다이버전스 종목 카드 목록 (동적 렌더링) ]
```

**주요 기능**
- [조회] 버튼 → `loadDivergence()` 호출, 현재 select 값으로 API 요청
- 결과를 종목 카드 형태로 렌더링 (종목코드·이름·수급증가량·가격변화율 표시)
- 필터 변경 시 즉시 재조회하지 않고 버튼 클릭 시에만 조회

**폼 입력**
| 요소 | 옵션 | 용도 |
|------|------|------|
| `select#div-window` | 10 / 20 / 40 / 60일 | 조회 기간 |
| `select#div-price-th` | 0% / 3% / 5% / 10% | 허용 가격 상승폭 |
| `select#div-ignore-ratio` | 5% / 10% / 15% / 25% / 50% | 소량 수급 무시 비율 |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/supply_divergence` | `window`(기본20), `price_th`(기본3.0), `ignore_ratio`(기본0.15) | 다이버전스 종목 배열 |

---

## 기간별 변화 (snapshot)

**개요**  
선택한 여러 기간 기준으로 수급 변화량 상위 종목을 비교한다. 가격/외국인/기관 탭으로 구분 조회.

**레이아웃 구성**

```
[ 비교 기간 input: "1,3,5,10,20" ]  [ 감시 종목만 체크 ]  [ 가격↓+수급↑ 필터 체크 ]  [ 조회 버튼 ]
─────────────────────────────────────────────────────────────────
[ 탭: 가격 | 외국인 수급 | 기관 수급 ]
[ 기간별 변화량 테이블 (동적 렌더링) ]
  헤더: 종목 | 1일 | 3일 | 5일 | 10일 | 20일 (기간별 컬럼)
```

**주요 기능**
- [조회] 버튼 → `loadSnapshot()` 호출, 쉼표 구분 기간 파싱 후 API 요청
- 탭 전환(`snapSetTab('price' | 'for' | 'orgn')`) → 같은 데이터를 다른 지표 기준으로 재정렬
- 가격↓+수급↑ 필터: 체크 시 가격 하락 + 수급 증가 교집합만 표시
- 감시 종목만 체크 시 `watched_only=true` 파라미터 전송

**폼 입력**
| 요소 | 용도 |
|------|------|
| `input#snap-periods` | 비교 기간 (예: "1,3,5,10,20") |
| `input#snap-watched` (checkbox) | 감시 종목만 조회 |
| `input#snap-filter-diverge` (checkbox) | 가격↓+수급↑ 필터 |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/snapshot` | `periods`(콤마구분, 기본"1,3,5,10,20"), `watched_only`(기본"true") | 기간별 수급 스냅샷 배열 |

---

## 매매 신호 (signals)

**개요**  
전략 에이전트가 생성한 매수·관망·매도 신호 목록을 조회한다. 신호 방향·종목으로 실시간 필터링 가능.

**레이아웃 구성**

```
[ 신호 방향 select: 전체 / BUY / HOLD / SELL ]  [ 종목 검색 input ]
─────────────────────────────────────────────────────────────────
[ 매매 신호 테이블 ]
  시간 | 종목코드 | 종목명 | 신호 | 현재가 | 목표가 | 손절가 | 신뢰도 | 전략
```

**주요 기능**
- 진입 시 `loadSignals()` → 최근 100건 로드 후 `allSignals` 전역 배열에 저장
- `filterSignals()`: select·input 값 기준으로 클라이언트 측 필터링 (API 재호출 없음)
- 신호 유형별 색상 구분: BUY=초록, SELL=빨강, HOLD=회색

**폼 입력**
| 요소 | 용도 |
|------|------|
| `select#signal-filter` | 신호 방향 필터 |
| `input#signal-search` | 종목코드·이름 검색 |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/signals` | 없음 | `[{ticker, signal, price, target_price, stop_loss, confidence, strategy, ts}]` |

---

## 보유종목 리포트 (report)

**개요**  
타사 보유종목 기준 일일 리포트를 생성·발송 관리하는 화면. 미리보기 → 수신자 관리 → 스케줄 설정 순서로 구성.

**레이아웃 구성**

```
┌──────────────────────────────┬─────────────────────────────┐
│ 리포트 미리보기 패널          │ ① 즉시 발송 패널            │
│  [미리보기 생성] [지금 발송]  │ ② 수신자 관리 패널          │
│  HTML 렌더링 영역             │    이메일 input + [추가]     │
│                              │    수신자 목록 (활성/삭제)   │
│                              │ ③ SMTP 상태 패널            │
│                              │ ④ 자동 발송 스케줄 패널     │
│                              │    시간·요일 설정            │
│                              │ ⑤ 발송 이력 패널            │
└──────────────────────────────┴─────────────────────────────┘
```

**주요 기능**
- [미리보기 생성] `reportGenPreview()` → `/api/report/preview` 호출, HTML을 iframe 또는 div에 렌더링
- [지금 발송] `reportSendNow()` → `/api/report/send` POST, 활성 수신자 전체에 발송
- 수신자 추가: 이메일 입력 후 `reportAddEmail()` 호출
- 수신자 행 토글 버튼 → 활성/비활성 전환, 삭제 버튼 → 수신자 제거
- 스케줄 설정: 요일·시·분 입력, `onchange="reportSaveSchedule()"` 으로 즉시 저장

**폼 입력**
| 요소 | 용도 |
|------|------|
| `input#report-email-input` (email) | 수신자 이메일 추가 |
| `input#report-sched-enabled` (checkbox) | 자동 발송 활성화 |
| `input#report-sched-hour` / `#report-sched-min` | 발송 시·분 |
| `select#report-sched-days` | 발송 요일 (매일/평일) |

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

**개요**  
타사 증권사 보유종목을 수동 입력·관리하는 화면. 종목별 평가손익·비중을 실시간 계산해 표시.

**레이아웃 구성**

```
[ 예수금 인라인 편집 ]                         [ + 종목 추가 ]
[ 요약 카드 × 4 ]  총 포트폴리오 | 투자금액 | 평가손익 | 예수금 비율
[ 증권사 필터 버튼 ]  [ 종목 검색 input ]
─────────────────────────────────────────────────────────────────
[ 보유종목 테이블 ]
  증권사 | 종목코드 | 종목명 | 수량 | 평균가 | 현재가 | 평가금 | 손익 | 수익률 | 비중 | 메모 | 관리
```

**주요 기능**
- 예수금 인라인 편집: `input#eh-cash-input` 포커스 해제 시 `ehSaveCash()` 자동 저장
- 증권사 필터 버튼: `ehSetFilter(brokerage)` → 해당 증권사 종목만 표시 (클라이언트 필터)
- [+ 종목 추가] → 모달(`#eh-modal`) 열기
- 종목 자동완성: 모달 내 종목명 입력 시 드롭다운 표시 → 선택 시 종목코드 자동 입력
- 행 편집 버튼: 수정 모달 재진입, 삭제 버튼: 확인 후 DELETE API 호출
- 요약 카드: 테이블 렌더링 후 `ehUpdateSummary()` 재계산

**모달: 보유종목 추가/수정 (`#eh-modal`)**
| 필드 | 용도 |
|------|------|
| `select#eh-brokerage` | 증권사 선택 |
| `input#eh-stock-input` | 종목 자동완성 |
| `input#eh-stock-name` | 종목명 |
| `input#eh-quantity` | 보유수량 |
| `input#eh-avg-price` | 매수평균가 |
| `input#eh-memo` | 메모 |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/manual_holdings` | 없음 | `[{id, brokerage, stock_code, stock_name, quantity, avg_price, memo, current_price, price_date}]` |
| POST | `/api/manual_holdings` | Body: `{brokerage, stock_code, stock_name, quantity, avg_price, memo}` | `{ok, id}` |
| PUT | `/api/manual_holdings/{id}` | URL: `id`, Body: 동일 | `{ok}` |
| DELETE | `/api/manual_holdings/{id}` | URL: `id` | `{ok}` |

---

## 현재가 관리 (price-mgmt)

**개요**  
타사 보유종목의 현재가를 키움 API(ka10081)로 일괄 동기화하고, 개별 수동 수정도 지원.

**레이아웃 구성**

```
[ 동기화 시작 버튼 ]  [ 동기화 상태 표시 ]
[ 요약 카드 × 3 ]  대상 종목 수 | 가격 보유 | 가격 없음
[ 실행 중 로그 패널 (실행 시에만 표시) ]
─────────────────────────────────────────────────────────────────
[ 종목별 현재가 테이블 ]
  종목코드 | 종목명 | 현재가 | 가격 기준일 | 조회 시각 | 수동 수정
```

**주요 기능**
- [동기화 시작] `pmStartSync()` → `POST /api/batch/sync_prices/start` 실행 후 3초 폴링으로 진행 로그 표시
- 수동 수정: 테이블 현재가 셀 인라인 클릭 → 입력 후 `PUT /api/price_sync/manual` 저장
- 로그 패널: 배치 실행 중에만 표시, 완료 시 자동 숨김

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/price_sync/stocks` | 없음 | `[{stock_code, stock_name, current_price, price_date, fetched_at}]` |
| PUT | `/api/price_sync/manual` | Body: `{stock_code, price}` | `{ok, stock_code, price, date}` |
| POST | `/api/batch/sync_prices/start` | 없음 | `{ok, log_file}` |
| GET | `/api/batch` | 없음 | 배치 실행 상태 확인 |

---

## 현금성 자산 (cash-assets)

**개요**  
현금·예수금·단기금융상품 등 비주식 자산을 관리한다. 종목 또는 매크로 지표와 연동해 자동 시세 동기화 가능.

**레이아웃 구성**

```
[ + 자산 추가 ]  [ 전체 동기화 ]  [ 새로고침 ]
[ 요약 카드 × 4 ]  총 현금성자산 | 자산 종류 수 | 주식 포트폴리오 대비 비율 | 최근 수정
─────────────────────────────────────────────────────────────────
[ 현금성 자산 테이블 ]
  자산명 | 증권사 | 수량 | 단가 | 매수가 | 평가금액 | 연동 | 메모 | 수정 | 동기화 | 삭제
```

**주요 기능**
- 가격 연동 3가지 모드:
  - **직접 입력**: 단가·평가금액 수동 관리
  - **종목 연동**: 특정 주식 종가로 단가 자동 갱신
  - **매크로 연동**: 금리·환율 등 매크로 지표로 단가 자동 갱신
- [동기화] (행 단위): `POST /api/cash_assets/{id}/sync` → 연동된 최신 시세로 갱신
- [전체 동기화]: `POST /api/cash_assets/sync_all` → 연동 자산 전체 일괄 갱신

**모달: 현금성 자산 추가/수정 (`#ca-modal`)**
| 필드 | 용도 |
|------|------|
| `input#ca-modal-name` | 자산명 |
| `select#ca-modal-brokerage` | 증권사 |
| `radio[name="ca-link-type"]` | 가격 연동 방식 (none / stock / macro) |
| `input#ca-stock-search` | 종목 연동 시 종목 자동완성 |
| `input#ca-macro-search` | 매크로 연동 시 지표 자동완성 |
| `input#ca-modal-qty` / `#ca-modal-price` | 수량 / 단가 |
| `input#ca-modal-purchase-price` | 매수 단가 |
| `input#ca-modal-amount` | 평가금액 (직접 입력 모드) |
| `input#ca-modal-note` | 메모 |

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

**개요**  
금리·환율 등 거시 경제 지표를 수동 입력하거나 네이버 금융에서 자동 조회한다. 현금성 자산의 매크로 연동 소스로 사용.

**레이아웃 구성**

```
[ + 지표 추가 ]
[ 거시 지표 테이블 ]
  키 | 지표명 | 현재값 | 단위 | 최근 수정 | 관리(수정·네이버조회·삭제)
```

**주요 기능**
- [네이버 조회] 버튼: `POST /api/macro_rates/{id}/sync_naver` → 네이버 금융 크롤링으로 최신값 자동 갱신
- 값 인라인 수정: 셀 클릭 → 입력 → blur 시 `PUT /api/macro_rates/{id}` 저장
- 지표 키(코드)는 현금성 자산 매크로 연동 시 `link_key` 로 참조됨

**모달: 매크로 지표 추가 (`#macro-add-modal`)**
| 필드 | 용도 |
|------|------|
| `input#macro-add-key` | 키 코드 (예: KRW_USD, KOSPI) |
| `input#macro-add-name` | 지표명 |
| `input#macro-add-value` | 현재값 |
| `input#macro-add-unit` | 단위 (예: %, 원) |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/macro_rates` | 없음 | `[{id, key, name, value, unit, updated_at}]` |
| POST | `/api/macro_rates` | Body: `{key, name, value, unit}` | `{ok}` |
| PUT | `/api/macro_rates/{id}` | URL: `id`, Body: `{name, value, unit}` | `{ok}` |
| DELETE | `/api/macro_rates/{id}` | URL: `id` | `{ok}` |
| POST | `/api/macro_rates/{id}/sync_naver` | URL: `id` | `{ok, value, key}` |

---

## 현금 대 주식 리밸런싱 (rebalance)

**개요**  
현금 목표 비중 대비 현재 비중을 분석해 리밸런싱 방향을 제시한다. 현금 과다/부족 경보 임계값 및 주의 구간(watch) 설정 가능.

**레이아웃 구성**

```
[ 새로고침 ]
[ 기준 설정 패널 ]
  현금 목표비율 % | 과다보유 기준 +% | 부족보유 기준 -% | 주의 상향 % / 하향 %  [ 저장 ]
[ 요약 카드 × 4 ]  총 자산 | 주식 평가금 | 현금성 자산 | 현금 현재비율/목표비율
─────────────────────────────────────────────────────────────────
[ 비율 시각화 바 ]  주식 ■■■■■■■□□□ 현금 (목표 마커 표시)
[ 리밸런싱 수치 ]  매수/매도 필요 금액 표시
─────────────────────────────────────────────────────────────────
[ 거래 계획 테이블 (토글) ]  주식별 매도 제안 목록
```

**주요 기능**
- 기준값 저장: `rbSaveThresholds()` → `PUT /api/settings` 에 alert/watch 임계값 일괄 저장
- 비율 바: 현재 현금 비중과 목표 비중을 시각적으로 비교, 경보 임계값 구간 강조
- 거래 계획 토글: 현금 부족 시 '어떤 종목 얼마나 매도' 제안 테이블 표시

**폼 입력**
| 요소 | 용도 |
|------|------|
| `input#rb-cash-target-input` | 현금 목표 비율 (%) |
| `input#rb-threshold-up` | 과다보유 경보 기준 — 리밸런싱 필요 상향 (%) |
| `input#rb-threshold-down` | 부족보유 경보 기준 — 리밸런싱 필요 하향 (%) |
| `input#rb-watch-up` | 주의 구간 상향 기준 (기본값: alert_up × 0.5) |
| `input#rb-watch-down` | 주의 구간 하향 기준 (기본값: alert_down × 0.5) |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/rebalance` | 없음 | `{holdings[], portfolio_total, stock_total, total_cash, alert_up, alert_down, watch_up, watch_down, cash_target_ratio}` |
| PUT | `/api/rebalance/stock_setting` | Body: `{stock_code, target_ratio, alert_up, alert_down, watch_up, watch_down}` | `{ok}` |
| PUT | `/api/rebalance/target` | Body: `{stock_code, target_ratio}` | `{ok}` |

---

## 신용 관리 (credit)

**개요**  
신용 잔고·대출금을 입력하고 담보비율을 계산해 반대매매 위험을 모니터링한다.

**레이아웃 구성**

```
[ 새로고침 ]
[ 기준 설정 패널 ]  신용담보비율 기준 %  [ 저장 ]
[ 추정 자산 요약 카드 (담보비율 임박 시 강조 표시) ]
─────────────────────────────────────────────────────────────────
[ + 포지션 추가 ]
[ 신용 포지션 테이블 ]
  증권사 | 주식 평가금 | 현금 평가금 | 대출금 | 담보비율 | 메모 | 수정 | 삭제
```

**주요 기능**
- 담보비율 실시간 계산: `(주식평가금 + 현금평가금) / 대출금 × 100`
- 기준 미달 시 행 배경 빨간색 경보 표시
- 기준 저장: `crSaveSettings()` → 앱 설정 테이블에 저장

**모달: 신용 포지션 추가/수정 (`#cp-modal`)**
| 필드 | 용도 |
|------|------|
| `select#cp-modal-brokerage` | 증권사 |
| `input#cp-modal-loan` | 대출금 |
| `input#cp-modal-note` | 메모 |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/credit_positions` | 없음 | `{positions[], broker_stock_eval, broker_cash_eval}` |
| POST | `/api/credit_positions` | Body: `{brokerage, purchase_amount, loan_amount, note}` | `{ok}` |
| PUT | `/api/credit_positions/{id}` | URL: `id`, Body: 동일 | `{ok}` |
| DELETE | `/api/credit_positions/{id}` | URL: `id` | `{ok}` |

---

## 주식 간 리밸런싱 (stock-rebalance)

**개요**  
보유 주식 종목 간 목표 비중을 설정하고 현재 비중과의 차이를 3단계 신호로 분석한다.  
상승장 주도주 보호를 위해 목표비중 100% 조정 대신 부분 조정 방식을 사용한다.  
목록 뷰 ↔ 개별 종목 설정 뷰로 전환.

**레이아웃 구성**

```
[ 새로고침 ]
─────── 목록 뷰 ────────────────────────────────────────────────
[ 요약 카드 × 4 ]  총 포트폴리오 | 목표비율 합계 | 리밸런싱 필요(+ 주의 종목 수) | 목표 미설정
[ 매매 추천 패널 (토글) ]
  총 매수금액 | 총 매도금액 | 순 현금 변동 | 거래 후 예상 현금
  STEP 1 — 매도 (60% 부분 조정)
  STEP 2 — 매수 (60% 부분 조정)
  관망 섹션 — 주의 구간 또는 조정 효과 5만원 미만
[ 종목별 비율 현황 테이블 ]
  종목명(임계값 표시) | 평가금액 | 현재비율 | 목표비율 | 편차 | 신호 | 조정수량

─────── 설정 뷰 (종목 행 클릭 진입) ───────────────────────────
[ ← 목록으로 돌아가기 ]
[ 종목 정보 헤더 ]
[ 목표비율 % ]
[ 리밸런싱 필요 구간 ]  +편차 % (alert_up)  |  -편차 % (alert_down)  → 초과분 60% 조정
[ 주의 구간 ]           +편차 % (watch_up)  |  -편차 % (watch_down)  → 초과분 33% 조정
[ 저장 ]  [ 개별 기준 초기화 ]
```

**신호 3단계**
| 상태 | 색상 | 조건 | 조정 액션 |
|------|------|------|-----------|
| 정상 범위 | 초록 | `\|relDev\| < watch` | 없음 (유지) |
| ⚠ 주의 (과다/부족) | 주황 | `watch ≤ \|relDev\| < alert` | 초과분의 **33%** 부분 조정 |
| 차익실현/추가매수 검토 | 빨강/파랑 | `\|relDev\| ≥ alert` | 초과분의 **60%** 부분 조정 |

- `relDev`: 상대 편차 (%) = `(현재비율 - 목표비율) / 목표비율 × 100`
- 조정 금액 < 5만원 → 수량 대신 **관망** 표시 (상태는 유지)

**기본 임계값**
- `alert_up` / `alert_down`: 전역 설정 (기본 30% / 25%)
- `watch_up` / `watch_down`: 전역 설정 (기본 alert × 0.5 = 15% / 12.5%)
- 종목별 개별 설정 가능 (`rebalance_targets` 테이블)

**주요 기능**
- 목록 뷰: 종목별 현재비중·목표비중·편차를 3단계 색상으로 표시
- 조정수량: 단계별 부분 조정 비율 적용, 5만원 미만 효과는 '관망'
- 설정 뷰: 종목 행 클릭 → `showStockSetting(stockCode)` → 인라인 설정 화면
- [저장] `ssSave()` → `PUT /api/rebalance/stock_setting`
- [초기화] `ssClear()` → 개별 alert/watch 기준 전체 초기화 (전역값으로 복귀)

**폼 입력 (설정 뷰)**
| 요소 | 용도 |
|------|------|
| `input#ss-target` | 목표 비율 (%) |
| `input#ss-alert-up` | 리밸런싱 필요 — 상방 편차 기준 (%) |
| `input#ss-alert-down` | 리밸런싱 필요 — 하방 편차 기준 (%) |
| `input#ss-watch-up` | 주의 구간 — 상방 편차 기준 (%) |
| `input#ss-watch-down` | 주의 구간 — 하방 편차 기준 (%) |

**DB 테이블 (`rebalance_targets`)**
| 컬럼 | 타입 | 용도 |
|------|------|------|
| `stock_code` | VARCHAR PK | 종목코드 |
| `target_ratio` | DECIMAL(6,2) | 목표 비율 (%) |
| `alert_up` | DECIMAL(6,2) NULL | 종목별 리밸런싱 상향 기준 (NULL = 전역값) |
| `alert_down` | DECIMAL(6,2) NULL | 종목별 리밸런싱 하향 기준 (NULL = 전역값) |
| `watch_up` | DECIMAL(6,2) NULL | 종목별 주의 상향 기준 (NULL = 전역값) |
| `watch_down` | DECIMAL(6,2) NULL | 종목별 주의 하향 기준 (NULL = 전역값) |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/rebalance` | 없음 | `{holdings[{...watch_up, watch_down}], alert_up, alert_down, watch_up, watch_down, ...}` |
| PUT | `/api/rebalance/stock_setting` | Body: `{stock_code, target_ratio, alert_up, alert_down, watch_up, watch_down}` | `{ok}` |

---

## 테마 리밸런싱 (theme-rebalance)

**개요**  
종목에 테마·섹터 태그를 붙이고 테마별 목표 비중을 설정해 포트폴리오를 관리한다.  
핵심 철학: **강제 목표비중 맞추기가 아닌 큰 방향 점검 + 테마 간 자금이동 한도 + 종목별 충돌 방지**.

**3단계 신호 체계**

| 단계 | 조건 | 조정비율 |
|------|------|----------|
| 정상 | relDev 기준 이하 | 없음 |
| 주의 | alert×50% < relDev <= alert | 20% |
| 리밸런싱 필요 | relDev > alert | 40% |

**충돌 방지 규칙**
- 매도: 개별비중 낮은 종목 제외, 개별 과다 종목 우선
- 매수: 개별비중 높은 종목 제외, 개별 부족 종목 우선
- 현금 목표 미달: 매수 = 매도 이내로 자동 제한
- 5만원 미만: 관망 처리

**레이아웃 구성**

```
[ 새로고침 ]
[ 기준 설정 패널 ]  과다보유 기준 +% | 부족보유 기준 -%  [ 저장 ]
[ 요약 카드 × 4 ]  총 포트폴리오 | 테마 목표합계 | 리밸런싱 필요 | 테마 미설정 종목
─────────────────────────────────────────────────────────────────
[ 테마별 비중 테이블 ]
  테마 | 현재금액 | 현재비율 | 목표비율 | 차이 | 편차 상태 | 목표 설정 | 알림
[ 리밸런싱 계획 패널 (토글) ]
  매수 방식 라디오: 현금 / 신용
  신용담보비율 input (신용 모드 시)
  계획 테이블: 테마·종목별 매수/매도 금액
─────────────────────────────────────────────────────────────────
[ 종목별 테마 설정 테이블 ]
  종목코드 | 종목명 | 현재가 | 평가금 | 테마 태그 (인라인 편집 가능)
```

**주요 기능**
- 테마 태그 인라인 편집: 종목 행 테마 셀 클릭 → 멀티 체크박스 → `PUT /api/theme_rebalance/stock_themes`
- 테마 목표비율 인라인 편집: 테마 행 목표비율 셀 클릭 → `PUT /api/theme_rebalance/theme_target`
- 리밸런싱 계획 토글: 현금/신용 라디오 변경 시 `trbRenderFullPlan()` 자동 재계산

**폼 입력**
| 요소 | 용도 |
|------|------|
| `input#trb-threshold-up` | 과다보유 경보 기준 (+%) |
| `input#trb-threshold-down` | 부족보유 경보 기준 (-%) |
| `radio[name="trb-buy-mode"]` | 매수 방식 (현금/신용) |
| `input#trb-credit-ratio` | 신용담보비율 (신용 모드) |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/theme_rebalance` | 없음 | `{themes[], stocks[{rb_dev_rel,...}], portfolio_total, stock_total, total_cash, alert_up, alert_down, watch_up, watch_down, cash_target_ratio}` |
| PUT | `/api/theme_rebalance/stock_themes` | Body: `{stock_code, themes[]}` | `{ok}` |
| PUT | `/api/theme_rebalance/theme_target` | Body: `{theme, target_ratio}` | `{ok}` |
| PUT | `/api/theme_rebalance/theme_alert` | Body: `{theme, alert_up, alert_down}` | `{ok}` |

---

## 정성 점수 (qualitative)

**개요**  
주식 종목·이슈·전략 등에 대해 1~10점 정성 점수를 기록하고 추이를 관리한다. 목록 뷰 ↔ 상세 뷰로 전환.

**레이아웃 구성**

```
[ 새로고침 ]  [ + 항목 추가 ]
[ 요약 카드 × 4 ]  전체 항목 | 최근 7일 업데이트 | 주의 항목 | 미평가 항목
─────────────────────────────────────────────────────────────────
[ 항목 목록 테이블 ]
  이름 | 카테고리 | 최근 점수 | 이전 점수 | 변화 | 최근 날짜 | 관리

─────── 상세 뷰 (항목 행 클릭 진입) ───────────────────────────
[ ← 닫기 ]  [ 항목명 · 카테고리 헤더 ]
[ 점수 입력 폼 ]
  날짜 | 점수 슬라이더(1~10, 0.5단위) + 숫자 입력 연동 | 코멘트 | [저장]
[ 점수 추이 라인차트 (canvas#qa-chart) ]
[ 점수 이력 테이블 ]  날짜 | 점수 | 전 점수 대비 변화 | 코멘트 | 삭제
```

**주요 기능**
- 슬라이더 ↔ 숫자 입력 연동: `oninput="qaUpdateScoreColor()"` → 점수값에 따라 배경색 변화 (빨강→노랑→초록)
- 점수 추이 차트: 항목 상세 진입 시 Chart.js 라인차트 렌더링
- 항목 수정: 연필 아이콘 → 모달 재진입

**모달: 항목 추가/수정 (`#qa-item-modal`)**
| 필드 | 용도 |
|------|------|
| `input#qa-item-name` | 항목명 (예: 삼성전자 사업전망) |
| `input#qa-item-category` | 카테고리 (예: 종목, 매크로) |
| `textarea#qa-item-desc` | 평가 기준 설명 |

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

**개요**  
에이전트가 기록한 전체 이벤트 로그를 조회한다. 이벤트 타입·상태·종목으로 필터링 가능.

**레이아웃 구성**

```
[ 이벤트 타입 select ]  [ 상태 select ]  [ 종목코드 input ]
─────────────────────────────────────────────────────────────────
[ 이벤트 로그 테이블 ]
  시간 | 이벤트 타입 | 에이전트 | 종목 | 상태 | 상세 내용
```

**주요 기능**
- 진입 시 `loadEvents()` 자동 호출
- 필터 변경 즉시 `filterEvents()` → 클라이언트 측 필터 (API 재호출 없음)
- 상태별 색상: SUCCESS=초록, FAIL=빨강, BLOCKED=노랑

**폼 입력**
| 요소 | 옵션 | 용도 |
|------|------|------|
| `select#event-type-filter` | DATA_FETCH / ANALYSIS / SIGNAL / RISK_CHECK / NOTIFICATION / ERROR / SYSTEM | 이벤트 타입 |
| `select#event-status-filter` | 전체 / SUCCESS / FAIL / BLOCKED | 처리 상태 |
| `input#event-ticker-filter` | 자유 입력 | 종목코드 검색 |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/events` | 없음 | `[{ts, event_type, agent, ticker, status, detail}]` |

---

## 감시 종목 (stocks)

**개요**  
수급·차트 분석 대상 종목 목록을 조회한다. 검색·시장 필터로 원하는 종목 빠르게 탐색.

**레이아웃 구성**

```
[ 종목 검색 input ]  [ 시장 select: 전체 / 코스피 / 코스닥 ]
─────────────────────────────────────────────────────────────────
[ 감시 종목 테이블 ]
  종목코드 | 종목명 | 시장 | 현재가 | 시가총액 | 기준일
```

**주요 기능**
- 진입 시 `loadStocks()` 자동 호출
- 검색·시장 필터 변경 즉시 `filterStocks()` → 클라이언트 필터
- 읽기 전용 (추가·삭제는 별도 DB 작업)

**폼 입력**
| 요소 | 용도 |
|------|------|
| `input#stocks-search` | 종목코드·이름 검색 |
| `select#stocks-market` | 시장 필터 |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/stocks` | 없음 | `[{stock_code, stock_name, market_name, last_price, market_cap, fetched_at}]` |

---

## 배치 관리 (batch)

**개요**  
5개 백그라운드 배치의 실행 상태 모니터링과 스케줄 설정을 관리한다. 로그 팝업으로 실시간 로그 확인 가능.

**레이아웃 구성**

```
[ 배치 목록 테이블 ]
  배치명 | 실행 상태(ON/OFF) | 스케줄 | 스케줄 요약 | 마지막 실행 | 액션(시작·중지·로그·스케줄)
```

**주요 기능**
- [시작] → `POST /api/batch/{job_id}/start`
- [중지] → `POST /api/batch/{job_id}/stop`
- [로그] → `window.open('/batch/{id}/log-viewer', ...)` 별도 팝업 창 오픈
  - 팝업: 3초 폴링으로 최근 200줄 자동 갱신, 실행 중 초록 점 애니메이션, 자동 스크롤 토글
  - 로그 파일: `{prefix}.log` 단일 파일에 누적 append, 실행마다 구분선 자동 삽입
- [스케줄] → 스케줄 설정 모달(`#sch-modal`) 오픈

**모달: 스케줄 설정 (`#sch-modal`)**
| 필드 | 용도 |
|------|------|
| `input#sch-m-enabled` (checkbox) | 스케줄 활성화 |
| `select#sch-m-mode` | 실행 방식 (지정시간 / 반복주기) |
| `input#sch-m-hour` / `#sch-m-minute` | 지정 시간 (시·분) |
| `select#sch-m-days` | 실행 요일 (매일/평일/주말) |
| `input#sch-m-intmin` | 반복 주기 (분 단위) |
| `input#sch-m-start` / `#sch-m-end` | 반복 허용 시간대 |
| `input#sch-m-mincap` | 수집 최소 시총 기준 (collect_history 전용) |
| `input#batch-email-input` | 리포트 수신자 이메일 (holdings_report 전용) |

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

**개요**  
프로젝트 전체 기획 문서를 3개 탭으로 구성한다. push 시 Claude Code가 자동 업데이트하고 앱 시작 시 DB에 동기화된다.

**레이아웃 구성**

```
[ 탭: 프로젝트 개요 | 화면별 기획서 | API 목록 ]

── 프로젝트 개요 ──────────────────────────────────────
  SPEC.md → marked.js 렌더링

── 화면별 기획서 ──────────────────────────────────────
  [ 모두 펴기 ] [ 모두 접기 ]          [ 처음 진입 시: 펼친 상태 / 접힌 상태 ]
  H2 아코디언 섹션 목록
    ▶ 대시보드 (접힘)
    ▼ 수급 현황 (펼침)
      내용 + /api/ 코드 클릭 → API 목록 탭 이동

── API 목록 ────────────────────────────────────────────
  [ 경로·설명 검색 ]  [ ALL | GET | POST | PUT | DELETE 필터 ]  (N개)
  리소스별 그룹 헤더 (접기/펼치기)
    GET  /api/supply_demand/{stock_code}  종목별 외국인·기관 수급 추이...
         ▼ 설명 + Path Parameters
```

**주요 기능**
- **화면별 기획서 아코디언**: H2 기준 섹션 접기/펼치기, 상태를 `localStorage`에 저장 → 다른 화면 이동 후 복귀해도 유지
- **기본값 설정**: '처음 진입 시' 버튼으로 전체 펼침/접힘 기본값 저장 (개별 토글이 기본값보다 우선)
- **API 클릭 연동**: 화면별 기획서의 `/api/` 코드 클릭 → API 목록 탭 전환 + 해당 엔드포인트 하이라이트 + 자동 스크롤
- **API 목록 그룹핑**: 경로 두 번째 세그먼트(`/api/[리소스]`) 기준 25개 그룹
- **한 번만 렌더링**: `_screenSpecLoaded` 플래그로 탭 전환 시 DOM 재생성 방지

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/spec` | 없음 | `{content, updated_at}` |
| GET | `/api/spec/screens` | 없음 | `{content, updated_at}` |
| GET | `/api/spec/apis` | 없음 | `[{path, methods[], doc, path_params[]}]` |

---

## 공통코드 관리 (common-codes)

**개요**  
시스템 내 공통코드(증권사, 테마 등)를 그룹별로 관리한다. 코드는 다른 화면의 select 옵션 소스로 사용됨.

**레이아웃 구성**

```
[ 탭: 증권사 코드 | 테마 코드 ]
[ 새 코드 input ]  [ 새 명칭 input ]  [ + 추가 ]
─────────────────────────────────────────────────────────────────
[ 공통코드 테이블 ]
  코드 | 명칭 | 정렬순서 | 활성 | 수정 | 토글 | 삭제
```

**주요 기능**
- 그룹 탭 클릭: `ccSelectGroup('BROKERAGE' | 'THEME')` → 해당 그룹 코드 로드
- [+ 추가]: 코드·명칭 입력 후 `ccAdd()` → POST
- 토글: 활성/비활성 전환 → 비활성 코드는 다른 화면 select에서 제외
- 수정 모달에서 명칭·정렬순서 변경 가능 (코드 자체는 변경 불가)

**모달: 코드 수정 (`#cc-edit-modal`)**
| 필드 | 용도 |
|------|------|
| `input#cc-edit-code` (disabled) | 코드 (읽기 전용) |
| `input#cc-edit-name` | 명칭 수정 |
| `input#cc-edit-sort` | 정렬 순서 |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/common_codes/{group}` | URL: `group` | `[{id, code, name, sort_order, active}]` |
| POST | `/api/common_codes/{group}` | URL: `group`, Body: `{code, name, sort_order}` | `{ok, id}` |
| PUT | `/api/common_codes/{id}` | URL: `id`, Body: `{name, sort_order}` | `{ok}` |
| POST | `/api/common_codes/{id}/toggle` | URL: `id` | `{ok}` |
| DELETE | `/api/common_codes/{id}` | URL: `id` | `{ok}` |

---

## 사용자 관리 (usermgmt)

**개요**  
로그인 계정을 생성하고 계정별 접근 가능 메뉴와 수급 기본값을 설정한다.

**레이아웃 구성**

```
[ 새 사용자 이름 input ]  [ + 추가 ]
─────────────────────────────────────────────────────────────────
[ 사용자 목록 테이블 (동적 렌더링) ]
  이름 | 로그인 ID | 접근 메뉴 수 | 로그인 설정 | 메뉴 권한 | 삭제
  ▼ 펼침 시: 로그인 ID·비밀번호 입력 폼 / 메뉴 체크박스 목록
```

**주요 기능**
- [+ 추가]: 이름 입력 후 `umAddUser()` (Enter 키도 동작) → POST
- 로그인 설정 행 펼침: ID·비밀번호 입력 → `PUT /api/users/{id}/credentials`
- 메뉴 권한 행 펼침: 전체 메뉴 체크박스 → 체크 변경 즉시 `PUT /api/users/{id}/preferences`
- 수급 기본 종목·기간도 사용자 기본값으로 저장 가능

**폼 입력**
| 요소 | 용도 |
|------|------|
| `input#um-new-name` (maxlength:20) | 신규 사용자 이름 |

| 메서드 | 경로 | 파라미터 | 주요 응답 |
|--------|------|----------|-----------|
| GET | `/api/users` | 없음 | `[{id, name, login_id, visible_menus[], supply_default_stock, supply_default_period}]` |
| POST | `/api/users` | Body: `{name}` | `{id, name, visible_menus, ...}` |
| PUT | `/api/users/{id}/credentials` | URL: `id`, Body: `{login_id, password}` | `{ok}` |
| PUT | `/api/users/{id}/preferences` | URL: `id`, Body: `{visible_menus[], supply_default_stock, supply_default_period}` | `{ok}` |
| DELETE | `/api/users/{id}` | URL: `id` | `{ok}` |
| GET | `/api/me` | 없음 (session 기반) | `{id, name, visible_menus[], ...}` |
