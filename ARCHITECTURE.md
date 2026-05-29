# ARCHITECTURE.md

v1.1. 시그널 생성기 — **자동 주문 없음, 수동 매매 보조 도구**. 기존 코드(`screener.py` / `analyzer.py` / `advisor.py` / `screen.py` / `track.py`)는 모두 폐기, 처음부터 다시 작성.

## 0. 시스템 범위

**시스템 1차 목적: 매일 "좋은 종목 추천"**. 매수 가격·시점·수량은 사용자가 차트 보고 직접 결정.

이 시스템이 **하는 일**:
- 매일 장 마감 후 KOSPI/KOSDAQ 전 종목 데이터 동기화
- **BUY 후보 풀** 생성 — Stage 2 + Minervini Template 8/8 통과 종목을 RS rank desc로 정렬해 워치리스트로 제공
- 각 후보의 Darvas 박스 상태(SEARCHING/FORMING/CONFIRMED/BREAKOUT)와 권장 stop 가이드 표시
- **박스 돌파 종목 강조** (BREAKOUT_TODAY/GAP) — *지금이 좋은 진입 시점인 후보*
- 보유 종목의 Stage 변화·30wMA 이탈·트레일링 스톱 갱신 추적
- 사이드카: 뉴스 빨간배지, 펀더멘털 가속 체크, 알림 발송

이 시스템이 **하지 않는 일**:
- 자동 주문 실행 (KIS `order-cash` 등 주문 API 사용 안 함)
- 진입가/시점 자동 결정 (사용자가 차트로 결정)
- 실시간/인트라데이 트리거 (EOD 일봉 기반)
- 사용자 의사결정 대체

**백테스트(`backtest.py`)는 보조 도구**. "Darvas 돌파 = 자동 매수" 가정 하의 시뮬레이션이라 실제 사용 양상과 다름 — 시스템의 *상한선* 추정용. 실 운영에선 사용자가 워치리스트를 보고 더 선별적으로 매수.

사용자 워크플로우:
```
저녁 sync (자동/스케줄)
→ 다음날 아침 시그널 알림 확인
→ HTS/MTS에서 수동 주문
→ 다음 sync 시 KIS 잔고에서 신규 보유 자동 감지
→ 메타데이터(스톱/박스) 보강 프롬프트
→ 이후부터 SELL/HOLD 시그널 정상 동작
```

## 1. 전략 개요

세 전략을 **레이어/역할 분담**으로 합성. 단일 가중합 스코어 아님.

| 역할 | 전략 | 산출물 |
| --- | --- | --- |
| 유니버스 게이트 | **Weinstein Stage Analysis** | Stage 2만 통과 |
| 품질 스코어 | **Minervini Trend Template + SEPA** | 8조건 통과 + RS rank ≥ 70 |
| 진입/청산 트리거 | **Darvas Box** | 박스 천장 거래량 동반 돌파 = BUY, 박스 바닥 이탈 = SELL |

원칙:
- **점수가 아니라 이벤트.** 매일 종목별로 줄세우는 게 아니라, 종목당 상태머신을 돌려 "오늘 트리거 떴나"를 묻는다.
- **시그널이 곧 권고, 결정은 사용자.** LLM은 사이드카(뉴스/공시 빨간배지)만.
- **재현성.** 같은 입력 → 같은 출력. 모든 함수는 명시적 `asof` 파라미터를 받고 `datetime.now()` 호출 금지.
- **피라미딩 없음 (v1).** 보유 중인 종목이 새 박스 돌파를 다시 만족해도 추가 BUY 시그널은 발생하지 않음. 트레일링 스톱만 갱신.
- **모든 파라미터는 백테스트로 튜닝.** 본 문서의 기본값은 출발점일 뿐, PR4에서 KOSPI/KOSDAQ 5~10년 데이터로 검증.

## 2. 데이터 레이어

데이터 출처는 **목적별 분담**. KIS는 사실상 잔고만 담당 (PR-F~I에서 KRX 통신을 직접 쓰는 pykrx/DART로 마이그레이션 완료).

### 1차 출처: pykrx (KRX 직접 통신)

| 용도 | 함수 | 비고 |
| --- | --- | --- |
| **종목 마스터** | `get_market_ticker_list(market=KOSPI/KOSDAQ)` | KOSPI + KOSDAQ 보통주. 거래정지/액면병합 직후 일시 누락 가능 → `_REIT_NAME` 필터 false positive에 유의 (예: "메리츠"의 "리츠" 매치 → `(?<!메)리츠`로 보호) |
| **일봉 OHLCV** | `get_market_ohlcv_by_date(start, end, ticker, adjusted=True)` | 수정주가. 페이지네이션 없음 (1콜 전체 기간). pykrx 응답이 가끔 거래대금 컬럼을 누락하므로 정규화 시 0 fill |
| **지수 일봉** | `get_index_ohlcv_by_date(start, end, code)` | KOSPI=1001, KOSDAQ=2001, KOSPI200=1028, KOSDAQ150=2203 (KIS와 코드 체계 다름) |
| **연간 DPS / PER / PBR / EPS / BPS / DIV (종목별)** | `get_market_fundamental_by_date` | 트레일링 12개월 누적. **12월 마지막 거래일의 DPS = (year-1) 회계연도 결산배당**으로 귀속 |
| **연간 DPS / DIV / EPS / BPS (일별 전종목 batch)** | `get_market_fundamental(date, market)` | 한 호출에 그 날 전 KR 종목 fundamental. 매 연도 12월말 × 2시장 = 24 호출로 11년치 전종목 sync. `--dividends` 기본 source(`pykrx_batch`) — 종목별 호출(~2.5h)이 ~50초로 단축 |
| **일별 batch OHLCV (전종목)** | `get_market_ohlcv(date, market=...)` | `--daily` 모드의 최근 N영업일 sync — 2 콜/일 × 5일 = 10콜로 5,800종목 처리 |

**KRX 인증**: pykrx는 일부 호출에 KRX 계정 필요. `.env`의 `KRX_ID`/`KRX_PW` 필수.

### 2차 출처: DART (금감원 전자공시)

| 용도 | 엔드포인트 | 비고 |
| --- | --- | --- |
| **재무지표 (ROE, ROA, 이익률 등)** | `fnlttSinglIndx.json` | 사업보고서 기준 연간. DGI 펀더멘털 20점 항목 |
| **자사주 취득/소각 공시** | `list.json` + `pblntf_detail_ty=B001` | 최근 3년 카운트. 주주환원 점수 (DGI 10점) |
| **배당결정 공시 카운트** | `list.json` + `report_nm == '현금ㆍ현물배당결정'` | 분기/반기/연 배당 빈도 추정 (5점). 자회사·정정 공시 제외 |
| **사업보고서 자사주 보유비율** | `document.xml` 파싱 | `SUM_TRS_RT` ACODE 우선, 없으면 `SUM_TRS_STK / (SUM_TRS_STK + SUM_FLT_STK)` |
| **회사 기본정보** | `company.json` | corp_name / ceo_nm / est_dt / induty_code / IR URL. Streamlit 상세 화면 |
| **증자/감자 이력** | `irdsSttus.json` | 일자별 유상증자/주식배당/무상증자 등. 사업보고서 단위 (5년치 ≈ 회사 설립부터 누적) |
| **자기주식 흐름** | `tesstkAcqsDspsSttus.json` | 사업보고서 단위 기초/취득/처분/기말 수량 |
| **원본 재무제표 전체** | `fnlttSinglAcntAll.json` (fs_div=CFS/OFS) | 재무상태표 + 손익 + 현금흐름 + 자본변동 raw 계정 (종목당 ~1,000행). CFS(연결) 우선, 없으면 OFS(별도) fallback |
| corp_code 매핑 | `corpCode.xml` (zip) | 전체 종목 1회 다운로드 후 `store/dart_cache/corp_codes.json` 영속 |

**권한 한계**: 기본키로 가능. 일부 endpoint (`cashDvdndDcsn.json`, `affiliation.json` 등)는 status=101 "잘못된 URL"로 거절 → `list.json` 기반 우회.

### 3차 출처: KIS Open API (잔고 전용)

| 용도 | 엔드포인트 | 비고 |
| --- | --- | --- |
| **계좌 잔고 (read-only)** | `inquire-balance` | 보유 종목/평단/수량. pykrx 미제공이라 KIS만이 유일한 출처 |

**주문 엔드포인트는 절대 사용 안 함.** `order-cash`, 정정/취소 등 transactional API 전부 미사용.

KIS 클라이언트 운영 디테일은 잔고 sync에 한정해서만 적용:
- **OAuth 토큰** TTL 24h, 디스크 캐시 (`~/.kis_token.json`)
- **Rate limit 20 req/s/app key** → 토큰 버킷 throttle
- **base URL**: `https://openapi.koreainvestment.com:9443` (실전만)

> 일봉/지수/재무 KIS fetcher (`fetch_daily_bars`, `fetch_index_bars`, `fetch_finance_table` 등)는 호환성을 위해 코드는 보존하되 `source="kis"` 옵션으로만 호출 가능. 기본 `source="pykrx"` 또는 DART.

### 4차 출처: korea-stock-analyzer MCP (보조)

| 도구 | 용도 |
| --- | --- |
| `search_news` | BUY 시그널 사이드카 (부정 키워드 빨간 배지) — 단 더미 데이터 응답 가능성 ↑ |
| `get_supply_demand` | 외인/기관 누적 순매수 (옵션, 미구현) |
| `analyze_equity` / `calculate_dcf` / `get_technical_indicators` / `get_financial_data` | **사용 안 함** — pykrx 의존 도구는 PythonExecutor stdout 파싱 충돌로 깨짐 |

MCP 결과는 로컬 캐시 (`store/mcp_cache/{tool}/{asof}__{ticker}.json`).

### PR0 검증 결과 (히스토릭)

`scripts/verify_kis.py` 실행으로 확정됐던 사실들. 일부는 PR-F~I로 마이그레이션 후 더 이상 적용 안 됨:

1. ~~**2년 일봉 백필 → 페이지네이션 필요**~~ — PR-F로 pykrx 전환되어 1콜로 처리. KIS 페이지네이션 코드는 `source="kis"` fallback에만 사용.
2. **종목 마스터 → pykrx 전용 의존성** (그대로) + PR-F~I 이후 pykrx 의존 범위 확장.
3. **상장폐지 종목 과거 데이터 → KIS/pykrx 모두 미제공** — v1 백테스트는 *현재 상장 중인 종목*만, 결과 리포트 헤더에 *낙관적 생존편향* 경고. KRX 폐지 데이터 확보는 v2 과제.

### 로컬 데이터 스토어

```
store/
  bars/{ticker}.parquet           # append-only 일봉, (ticker, date) unique 강제
                                  # 컬럼: date, open, high, low, close, volume, value, adj_factor
  index/{KOSPI200|KOSDAQ150}.parquet
  meta/master.parquet             # ticker, name, market, sector, listed_date, delisted_date
  meta/flags.parquet              # asof, ticker, flag (관리/경고/위험/정지)
  flows/{ticker}.parquet          # date, foreign_net_krw, institution_net_krw, individual_net_krw
  financials/{ticker}.parquet     # 분기 손익 + ROE (KIS — 레거시, --financials 모드)
  dividends/{ticker}.parquet      # pykrx 출처 배당 이력 (fiscal_year 컬럼 채워짐) + KIS legacy 행 공존 가능 — scoring은 pykrx 우선
  dart_cache/                     # DART corp_codes.json + treasury_activity/holding/dividend_decisions JSON 캐시
  dart_indicators/{ticker}.parquet  # DART fnlttSinglIndx.json 결과 (연도별 ROE 등)
  dart_business/                  # DART 사업보고서 주요사항 (KOSPI200+KOSDAQ150 한정 sync)
    company_info/{ticker}.json    # company.json — corp_name/ceo_nm/est_dt/induty 등
    share_issuance/{ticker}.parquet   # irdsSttus.json — 증자/감자 이력
    treasury_status/{ticker}.parquet  # tesstkAcqsDspsSttus.json — 자사주 흐름
    financials_raw/{ticker}.parquet   # fnlttSinglAcntAll.json — 재무제표 전체 raw
  value_scores/{asof}.parquet     # DGI screen 결과 (cli.dgi)
  mcp_cache/{tool}/{key}.json
  signals/{biz_date}.json         # 일일 시그널 (PR3+)
  portfolio.json                  # 메타데이터 캐시 (정의는 §8 참조)
  logs/{yyyymm}.jsonl             # JSON-line 운영 로그
```

**무결성 규칙:**
- Parquet 쓰기는 **tmp + atomic rename**. 중간 끊김 시 부분 쓰기 방지.
- Append 시 `(ticker, date)` unique 검증 — 중복 행 거부 후 경고.

## 3. 인디케이터 명세 (`indicators.py`)

모두 `pd.Series` → `pd.Series` 순수 함수. 외부 호출 없음.

| 함수 | 정의 |
| --- | --- |
| `sma(close, n)` | 단순이동평균. 표준 n=50, 150, 200. **30주 MA는 일봉 150 SMA로 근사** (주봉 리샘플 미채택, 이유: 일봉 시그널과 일관성) |
| `ema(close, n)` | 지수이동평균 (필요 시) |
| `atr(high, low, close, n=20)` | True Range의 n-period Wilder smoothing |
| `rolling_high(high, n)` / `rolling_low(low, n)` | 롤링 max/min — **고가/저가 기준** |
| `slope_normalized(series, lookback)` | 최근 lookback 봉 선형회귀 기울기 ÷ 평균값. 부호 + 크기 비교 가능 |
| `rs_line(stock_close, index_close)` | (stock/index) 시계열, **시작점 100으로 정규화**. Stage 분류기의 RS slope(시간 변화)에만 사용 — 횡단면 비교용 아님 |
| `rs_momentum(close, periods, weights)` | IBD-style 4Q 가중 수익률 스칼라. 기본: 63d×40% + 126d×20% + 189d×20% + 252d×20%. 데이터 부족 시 NaN |
| `rs_rank(scores: pd.Series)` | 종목별 `rs_momentum` 결과의 횡단면 백분위 0~100. 시장(KOSPI/KOSDAQ)별 분리 계산 |
| `volume_ratio(vol, n=50)` | `vol / sma(vol, n)` |

**RS rank는 시장별 분리 계산.** KOSPI 종목은 KOSPI200 지수 대비 RS line → KOSPI 종목들끼리 백분위. KOSDAQ도 동일. 두 그룹 백분위를 그대로 사용 (시장 간 합산 랭킹 X).

**섹터 RS rank (WatchlistEntry.sector_rs_rank).** 같은 (market, sector) 그룹 안에서만 다시 백분위. "섹터 안에서 주도주" 식별용 표시 컬럼 — 게이트 아님. peer < 10 또는 sector="UNKNOWN" 인 그룹은 NaN.

**Equity 정의:** §8의 `equity_krw` = 현금 + 보유 종목 평가액(전일 종가 기준).

## 4. Weinstein Stage Classifier (`stage.py`)

TradingView 일반 구현 일치 + 책 *"Secrets for Profiting in Bull and Bear Markets"* 원전. **History-dependent 상태머신** — 이전 상태가 현재 라벨에 영향.

**RS는 Stage 판정에 사용하지 않음** — 책 원전엔 가격+MA만 사용. RS는 Minervini Template 조건 8로 별도 게이트.

```
파라미터 (일봉 기본):
  ma_length              = 150   # 30주
  slope_lookback         = 20    # 4주
  slope_threshold_pct    = 0.001 # 0.1%
  band_pct               = 0.03  # MA ±3%
  ma_type                = "SMA" # EMA도 옵션

상태 전이:
  r = (ma - ma[20]) / ma
  above = close > ma * (1 + band)
  below = close < ma * (1 - band)
  flat  = |r| <= threshold

  if (r > threshold) AND above:    Stage 2
  elif (r < -threshold) AND below: Stage 4
  elif flat:
      if prev in (2, 3):           Stage 3   # 추세 후 평탄 = 분배
      else:                        Stage 1   # 그 외 평탄 = 베이스
  else:                            prev       # 직전 상태 유지 (band 안 + 추세)
```

- 매 영업일 1/2/3/4 중 하나 라벨링. 워밍업(MA 미완성) 구간은 0(UNKNOWN).
- Stage 3는 *Stage 2 → flat*에서만 발생. Stage 4 → flat은 Stage 1.
- 거래량은 Stage 분류 미사용 — 거래량 확인은 Darvas 돌파 단계에서.

**강화 신호 (BUY 게이트 추가):**
- 최근 5/20일 외인+기관 누적 순매수가 우상향 (양수 + slope > 0)
- `STAGE2_REQUIRE_INST_FLOW=true` (기본) 일 때 이 조건 미충족이면 BUY는 차단

## 5. Minervini Trend Template (`template.py`)

TradingView 일반 구현 + Mark Minervini *"Trade Like a Stock Market Wizard"* 원전.
8조건 모두 통과해야 BUY 후보.

| # | 조건 | 디폴트 임계 |
| --- | --- | --- |
| 1 | `close > sma150 AND close > sma200` | — |
| 2 | `sma150 > sma200` | — |
| 3 | `sma200 > sma200[100]` | **100봉 우상향** (책 p.79 권장 minimum 4–5개월, 22로 낮추면 TV 수준) |
| 4 | `sma50 > sma150 AND sma50 > sma200` | — |
| 5 | `close > sma50` | — |
| 6 | `close >= lowest(low, 260) × 1.30` | **저가** 기준, **30% 회복** (책 p.79 "at least 30 percent above its 52-week low") |
| 7 | `close >= highest(high, 260) × 0.75` | **고가** 기준, 25% 이내 (책 p.79) |
| 8 | `rs_rank >= 70` | IBD 4Q 가중 모멘텀의 시장 횡단면 백분위 (책 p.79 "no less than 70") |

조건 6/7은 **고가/저가 기준** (종가 아님). 한국시장 ±30% 가격제한과 무관하게 원전 정의 충실.

**2026-05 검증**: PDF *Trade Like a Stock Market Wizard* (Mark Minervini, p.79) 의 8조건을
1:1 대조해 책 원전 수치로 통일. 이전 25%/22봉(TV 통례)에서 30%/100봉(책 권장)으로 상향 →
게이트 더 엄격해서 후보 풀은 줄어들지만 *진위 신호* 농도 ↑.

선택 9번 (Minervini growth, `FUNDAMENTAL_REQUIRED=true` 일 때만): 분기 EPS YoY ≥ 25% **또는** 직전 2분기 EPS 가속 (`Q-1 yoy > Q-2 yoy`). 같은 기준으로 매출. MCP `get_financial_data` 결과로 판정. 펀더멘털 미공시 종목은 건너뜀. **기본 false** — Weinstein·Darvas만으로 시작.

## 6. Darvas Box (`darvas.py`)

Nicolas Darvas *"How I Made $2,000,000 in the Stock Market"* CH4 원전 충실.
원전 정의: "stocks moved in a series of frames... oscillate between a low and a high point... 10% to 20% each way." 원전엔 **시간 임계나 거래량 임계가 없음** — PR4 sweep으로 우리만의 추가였던 거래량 1.5×·검증 15일·확정 3일 튜닝 (G3 채택).

### 박스 가격 기준

- **천장 = `rolling_high(high, BOX_HIGH_LOOKBACK)` 의 고가**
- **바닥 = 천장 확정 후 도달한 `low`의 최저값**
- **돌파 판정 = `close`** (장중 일시 돌파 무시, 종가 확정)

### 박스 상태머신 (PR4 sweep G3 디폴트)

```
SEARCHING        # 천장 후보 탐색 중
  → (천장 후보 BOX_HIGH_CONFIRM=1일 동안 깨지지 않음) → TOP_CONFIRMED
TOP_CONFIRMED    # 천장 확정, 바닥 형성 대기
  → (저점이 천장-12% 안에 머묾) → FORMING
  → (저점이 천장-12% 초과 하락) → SEARCHING (박스 무효)
FORMING          # 박스 안에서 횡보 중
  → (BOX_VALID_MIN_DAYS=5일 누적) → CONFIRMED
  → (저점이 박스 바닥 미만) → BROKEN_DOWN → SEARCHING
CONFIRMED        # 정식 박스, 돌파 대기
  → (close > top × (1 + BREAKOUT_BUFFER=0.003) and volume_ratio_50 >= BREAKOUT_VOLUME_MULT=1.0)
       → BREAKOUT_TODAY
  → (close < bottom) → BROKEN_DOWN → SEARCHING
  → (BOX_STALE_DAYS=60일 경과) → STALE → SEARCHING
BREAKOUT_TODAY   # 돌파 당일 — BUY 시그널 트리거
  → 다음 영업일부터 새 박스 탐색 시작 (포지션 보유 중이면 트레일링 박스로 전환)
```

PR4 백테스트 결과(2025-11~2026-05, 전체 종목): G3 trades 9건 / Total +55.3% / Alpha -52.3%p (베이스 디폴트 trades 6건 / Total +39.4% / Alpha -68.1%p 대비 개선).

### 트레일링 박스

매수 후 가격이 박스 천장 위로 이탈하면 새 박스 형성 대기. 새 박스가 `CONFIRMED` 되면 **스톱을 새 박스 바닥으로 갱신** (단, 항상 직전 스톱보다 높을 때만 — 스톱은 후퇴하지 않음).

### 한국시장 보정

- **±30% 가격제한 / 상한가 돌파.** 갭상승 돌파는 `BREAKOUT_GAP`으로 별도 라벨링. `GAP_BUY_ON_PULLBACK=true` (기본): 시그널 종류만 표시하고 진입가 가이드를 "박스 천장+0.3%" 대신 "박스 천장 ~ 갭 메우는 첫 되돌림가" 범위로 표시. 사용자가 보고 판단.
- **무상증자/액면분할.** KIS의 `adj_factor` 변화 감지 → 해당 종목 박스 상태 클리어, 14일 cooldown 후 재탐색.
- **거래량.** KIS 일봉 거래량이 정규시장만인지 시간외 포함인지 PR1에서 확인. 시간외 포함이면 분리 가능 여부 확인, 안 되면 그대로 사용하되 문서화.

## 7. Signal Composition (`signals.py`)

```
def generate_signals(asof, universe, portfolio, rs_rank_map):
    buys, holds, sells = [], [], []

    for ticker in universe:
        bars = load_bars(ticker, up_to=asof)
        if bars is None or len(bars) < 252:
            continue

        if liquidity_20d(bars) < LIQUIDITY_MIN_KRW: continue
        if mcap(ticker, asof) < MCAP_MIN_KRW: continue
        if is_flagged(ticker, asof): continue

        stage = classify_stage(...)
        template_ok = check_template(bars, rs_rank_map[ticker])
        box = box_state(bars)
        flow = supply_demand(ticker, asof)

        if ticker in portfolio:
            pos = portfolio[ticker]
            prev_close = bars.close.iloc[-1]

            # 갭다운 처리: 스톱 hit이지만 시가가 스톱보다 한참 아래면 라벨 강화
            sell_reason = None
            gap_label = None
            if prev_close <= pos.current_stop:
                sell_reason = "STOP_HIT"
                if bars.open.iloc[-1] < pos.current_stop * 0.97:
                    gap_label = "URGENT_GAP_DOWN"
            elif prev_close < sma150(bars).iloc[-1]:
                sell_reason = "30W_MA_BREAK"
            elif stage in (3, 4):
                sell_reason = f"STAGE_{stage}"

            if sell_reason:
                sells.append({"ticker": ticker, "reason": sell_reason,
                              "label": gap_label, "exit_guide": ...})
            else:
                new_stop = maybe_trail_stop(pos, box, bars)
                holds.append({"ticker": ticker, "current_stop": new_stop,
                              "trail_updated": new_stop != pos.current_stop, ...})
        else:
            # 신규 후보
            if stage != 2: continue
            if not template_ok: continue
            if box is None or box.state != "BREAKOUT_TODAY": continue
            if STAGE2_REQUIRE_INST_FLOW and not flow.is_accumulating: continue

            entry_guide = box.top * (1 + BREAKOUT_BUFFER)
            stop = box.bottom
            buys.append({
                "ticker": ticker,
                "entry_guide": entry_guide,
                "stop": stop,
                "risk_per_share": entry_guide - stop,
                "suggested_size_krw": position_size(...),
                "box": {"top": box.top, "bottom": box.bottom, "days_in_box": box.days, "gap": box.is_gap_breakout},
                "rs_rank": rs_rank_map[ticker],
                "stage_2_since": ...,
                "template_pass": [...],
                "volume_confirmation": "...",
                "news_risk": search_news_red_flags(ticker, days=30),
            })

    # 동시 BUY 시그널 우선순위: RS rank desc → 박스 in-days desc → MCAP desc
    buys.sort(key=lambda b: (-b["rs_rank"], -b["box"]["days_in_box"], -mcap(b["ticker"], asof)))
    buys = buys[:MAX_POSITIONS - len(portfolio)]  # 동시 보유 상한 고려 후 상위만

    return {"asof": asof, "new_buys": buys, "holds": holds, "sells": sells}
```

**우선순위 / 상한 룰:**
- BUY 후보가 많을 때 정렬: `(rs_rank desc, days_in_box desc, mcap desc)`
- 출력 상한: `MAX_POSITIONS - len(현재 보유)` — 동시 보유 가능 슬롯 수
- 피라미딩 없음: 이미 보유 중인 종목은 신규 BUY 후보에서 자동 제외 (위 `if ticker in portfolio`)

**`risk_per_share <= 0` 가드.** 박스 천장 + 버퍼 < 박스 바닥인 케이스는 박스 정의상 불가능하지만, 호가 단위 라운딩으로 0이 될 수 있음. 0/음수면 해당 시그널 드롭 + 경고 로그.

## 8. Portfolio (`portfolio.py`)

**Source of truth = KIS 잔고 조회.** `portfolio.json`은 *메타데이터 캐시* (스톱·박스·진입 사유) 일 뿐.

```json
{
  "synced_at": "20260514T160000+09:00",
  "kis_balance": {
    "005930": {"shares": 100, "avg_price": 71200}
  },
  "meta": {
    "005930": {
      "entry_date": "20260201",
      "initial_stop": 65800,
      "current_stop": 68500,
      "current_box": {"top": 78000, "bottom": 68500, "since": "20260315"},
      "highest_close_since_entry": 77200,
      "buy_signal_id": "20260201__005930__darvas_breakout"
    }
  },
  "equity_krw_estimate": 19120000
}
```

**동기화 흐름 (`sync_portfolio` CLI):**

1. KIS `inquire-balance` 호출 → `kis_balance` 채움
2. `kis_balance`엔 있는데 `meta`에 없는 종목 → **신규 보유 감지**:
   - 직전 14일 signals 파일을 역방향으로 스캔해 동일 ticker의 `new_buys` 항목 검색
   - 매칭 성공: `entry_date`, `initial_stop`, `current_box`, `buy_signal_id` 자동 복사
   - 매칭 실패: CLI 프롬프트로 `initial_stop` 수동 입력 요청 (박스는 현재 박스 상태로 자동 채움)
3. `meta`엔 있는데 `kis_balance`엔 없는 종목 → **청산 감지**: `meta`에서 제거 + 로그 기록
4. 수량 변경 감지 (분할 매수/매도): 경고 출력. 메타는 그대로 유지 (스톱은 사용자가 필요 시 수동 조정).

**불일치 정책:** KIS 잔고와 메타가 어긋날 때 시스템은 **차단하지 않고 경고만 출력**. 사용자가 보고 판단.

### 사이즈 산정 (시그널 표시용)

자동 주문이 없으므로 `equity_krw`는 *추천 사이즈 계산용* 기준값일 뿐. 사용자가 실제로 따라가지 않아도 무관.

- `equity_krw = DEFAULT_EQUITY_KRW` (env, 기본 1천만 원) 또는 KIS 잔고로부터 자동 추정
- `risk_per_share = entry_guide - stop`
- `max_loss_krw = equity_krw * MAX_RISK_PER_TRADE_PCT / 100`
- `shares = floor(max_loss_krw / risk_per_share)`
- `cap_by_weight = equity_krw * MAX_POSITION_WEIGHT_PCT / 100`
- `shares = floor(min(shares * entry_guide, cap_by_weight) / entry_guide)`
- 가격은 KOSPI/KOSDAQ tick rule로 round (가독성용, 사용자 주문 시 어차피 자동)

## 9. 알림 & 리포트 (`report.py`, `notify.py`) — PR6

자동 주문이 없으므로 **사용자가 시그널을 매일 보는 것이 시스템의 전부**. 알림이 약하면 시스템 자체가 무용지물.

### 출력 채널 (`NOTIFY_CHANNEL`)

| 채널 | 동작 |
| --- | --- |
| `console` | 시그널 CLI가 표/색 텍스트로 stdout 출력 |
| `file` | `result/reports/{biz_date}.md` 마크다운 리포트 |
| `slack` | `SLACK_WEBHOOK_URL` 로 POST. BUY/SELL/URGENT는 메인 채널, 나머지는 thread |
| `telegram` | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` |

다중 채널 동시 지원 (콤마 구분: `console,file,slack`).

### 리포트 구성

매일 sync + signals 완료 후:

```
=== 2026-05-14 (목) 시그널 ===

[신규 BUY] 3개
  005930 삼성전자  진입가가이드 73,500  스톱 68,500 (-6.8%)  추천 100주
                 RS 87  박스 21일  거래량 1.8x  📰 ⚠ 1
  ...

[보유 HOLD] 4개
  035720 카카오    현재 51,200  스톱 47,800 (전일 47,000에서 ↑)
  ...

[SELL] 1개
  ⚠️ URGENT 263750 펄어비스  STOP_HIT  종가 12,300 < 스톱 12,500
                                       GAP_DOWN: 시가 11,800 손실 확대 가능
```

### 운영 시각 (참고)

- **저녁 sync + 시그널 생성**: 정규장 마감(15:30) + 데이터 안정화(예: 16:00) 이후. 권장 16:30.
- **사용자 알림 도달**: 16:30 직후 (저녁) 또는 다음날 아침 (사용자 선호).
- **다음날 주문은 동시호가/시초가에 사용자가 수동.**

스케줄링은 cron / systemd timer / 수동 중 택일. v1은 CLI 제공만, 자동화 권장 설정은 README 가이드.

## 10. Backtest (`backtest.py`) — PR4

일별 시뮬레이션. 같은 `signals.py`를 그대로 사용 — 백테스트와 라이브가 코드 경로를 공유하는 게 핵심.

```
sim_portfolio = empty()
for biz_date in trading_days(start, end):
    universe = build_universe(biz_date)            # 해당 시점 상장 종목
    rs_rank_map = compute_rs_rank(universe, biz_date)
    signals = generate_signals(biz_date, universe, sim_portfolio, rs_rank_map)

    # SELL 먼저 처리
    for s in signals["sells"]:
        if s["label"] == "URGENT_GAP_DOWN":
            exit_price = bars[s.ticker].open.loc[biz_date_next]   # 갭다운: 시가 청산
        elif s["reason"] == "STOP_HIT":
            exit_price = min(s.stop, bars[s.ticker].open.loc[biz_date_next])
        else:
            exit_price = bars[s.ticker].open.loc[biz_date_next]   # 다음날 시초가
        sim_portfolio.close(s.ticker, biz_date_next, exit_price)

    # 그 다음 BUY
    for s in signals["new_buys"]:
        if can_open(sim_portfolio):
            entry_price = bars[s.ticker].open.loc[biz_date_next]  # 다음날 시초가
            if entry_price > s.entry_guide * 1.03: continue        # 갭상승 시 추격 매수 차단 옵션
            sim_portfolio.open(s.ticker, biz_date_next, entry_price, stop=s.stop)

    record_equity_curve(biz_date)
```

### 지표

- 누적수익률, CAGR
- 승률, 평균 R 멀티플 (실현 손익 ÷ 초기 리스크)
- 최대낙폭 (MDD), MAR ratio (CAGR / MDD)
- 평균 보유기간, 평균 회전율
- **벤치마크 비교**: 동일 기간 KOSPI/KOSDAQ 단순 매수보유 대비 알파/베타/IR
- 트레이드별 P&L 분포 + 손실 트레이드 톱 N (실패 원인 분석)

### 튜닝 대상 (수동 또는 그리드)

`BOX_HEIGHT_MAX_PCT`, `BOX_VALID_MIN_DAYS`, `BREAKOUT_VOLUME_MULT`, `RS_RANK_MIN`, 30주 MA slope lookback, `STAGE2_REQUIRE_INST_FLOW`, `GAP_BUY_ON_PULLBACK`, sma200 slope lookback (100d 외 후보).

### 현실성 보정

- 슬리피지: 시초가 진입 + 0.2%
- 수수료: 매수/매도 각 0.015%, 매도 시 거래세 0.2%
- 거래정지/관리종목 편입 시점 정확히 반영 (`meta/flags.parquet`)
- adj_factor 변화 반영 (박스 리셋)
- **생존편향**: KIS 폐지 종목 데이터 미제공 확정 (§2 PR0 결과). "현재 상장 중인 종목"만 시뮬레이션 → 결과는 *낙관적 편향*. 리포트 헤더에 반드시 다음 같은 경고 출력: `⚠️ 이 백테스트는 현재 KIS에서 조회 가능한 종목만 포함합니다. 상장폐지된 종목은 빠져 있어 결과가 실제보다 우호적일 수 있습니다.`

## 11. Korean Market 특이사항

| 항목 | 처리 |
| --- | --- |
| ±30% 가격제한 | 갭상승/하한가 별도 라벨링, 박스 돌파 판정에 갭 라벨 |
| 무상증자/액면분할 | adj_factor 모니터링, 박스 리셋 + 14일 cooldown |
| 관리종목/투자경고/투자위험/거래정지 | 유니버스 게이트에서 컷, 보유 중이면 SELL 시그널에 차단 사실 라벨 |
| 단일가 매매 | 단일가 기간 거래량은 박스 돌파 검증에서 제외 (PR1 확인) |
| 외인/기관/개인 매매 | Stage 2 강화 신호 |
| 거래시간 | 정규장 09:00–15:30 (KST). 시간외 단일가 별도. sync는 16:00 이후 권장 |
| 호가 단위 | 가격대별 tick (1/5/10/50/100/500/1000원). entry_guide/stop 표시 시 round |
| 우선주 / 스팩 / ETF / ETN / 리츠 | 유니버스에서 제외 (종목 코드 패턴 + 종목 마스터 분류) |
| RS 분모 | KOSPI 종목 vs KOSPI200, KOSDAQ 종목 vs KOSDAQ150 (분리) |
| 휴장일 | KIS 응답이 빈 응답/에러일 수 있음. sync는 거래일 캘린더(`get_market_open_dates` 또는 자체 보관)로 사전 판단 |

## 12. 디렉토리 구조 (PR6까지 반영)

```
moneygold/
  ARCHITECTURE.md
  CLAUDE.md
  README.md                  # 5단계 Quickstart + 명령 치트시트
  pyproject.toml             # ui extra: streamlit/plotly
  .env.example               # KRX·KIS 키만 채우면 동작
  .gitignore
  .streamlit/config.toml     # runOnSave 등

  src/moneygold/
    config.py                # env 로드, frozen dataclass
    indicators.py            # SMA/ATR/52w/RS/slope_normalized
    stage.py                 # Weinstein 4-stage 상태머신
    template.py              # Minervini 8 조건
    darvas.py                # 박스 상태머신
    fundamentals.py          # KIS 분기 손익 정규화 + YoY/연속/가속
    consensus.py             # yfinance 컨센서스 + revision 추세
    signals.py               # 합성 → DailySignals (watchlist 포함)
    backtest.py              # 워크포워드 시뮬레이터
    universe.py              # pykrx 마스터 + sector + mcap
    data/
      kis_client.py          # OAuth, rate limit, 페이지네이션, 재시도
      kis_endpoints.py       # URL + tr_id 매핑 (시세·재무 모두)
      store.py               # atomic parquet read/write
      sync.py                # DataSync 오케스트레이터
    app/                     # Streamlit 대시보드 (PR5)
      streamlit_app.py
      charts.py              # plotly 빌더 (캔들+MA+박스+Stage 배경)
      _glossary.py           # 사이드바·컬럼·박스 설명 텍스트
    cli/                     # python -m moneygold.cli.<name>
      sync.py · signals.py · classify.py · backtest.py

  scripts/                   # 진단 도구
    verify_kis.py · probe_kis_finance.py
    inspect_template.py · inspect_stage.py · funnel.py
    sweep_*.sh

  store/                     # 데이터 (gitignored)
    bars/{ticker}.parquet
    index/{code}.parquet     # KOSPI / KOSDAQ / KOSPI200 / KOSDAQ150
    meta/master.parquet      # ticker, name, market, sector, mcap
    financials/{ticker}.parquet     # 분기 매출/영익/EPS
    consensus/{ticker}.json         # yfinance 캐시 (per ticker JSON)
    signals/{biz_date}.json         # 일일 시그널 export
    logs/{yyyymm}.jsonl

  tests/                     # 100+ tests
```

## 13. 설정 / 환경 변수

`.env` (gitignored):

```ini
# KRX (pykrx 인증 — 일봉/펀더멘털/마스터)
KRX_ID=
KRX_PW=

# DART OpenAPI (재무지표 + 자사주 + 배당결정 공시)
DART_API_KEY=
DART_RATE_PER_SEC=8

# KIS (잔고 전용. 시세는 pykrx로 마이그레이션 완료)
KIS_APP_KEY=
KIS_APP_SECRET=
KIS_ACCOUNT_NO=
KIS_ACCOUNT_PROD_CD=01

# 사이징 (시그널 표시용 — 자동 주문 아님)
DEFAULT_EQUITY_KRW=10000000
MAX_RISK_PER_TRADE_PCT=1.0
MAX_POSITION_WEIGHT_PCT=20.0
MAX_POSITIONS=10

# 유니버스 필터
LIQUIDITY_MIN_KRW=1000000000
MCAP_MIN_KRW=50000000000

# 전략 파라미터 (백테스트 후 확정)
STAGE2_REQUIRE_INST_FLOW=true
RS_RANK_MIN=70
SMA200_SLOPE_LOOKBACK=100   # 책 p.79 권장 minimum (4-5개월). 22로 낮추면 TV 수준 (1개월)

# Weinstein Stage (PR4: RS 제거 + history-dependent + TV 일치)
STAGE_MA_LENGTH=150
STAGE_MA_TYPE=SMA
STAGE_SLOPE_LOOKBACK=20
STAGE_SLOPE_THRESHOLD_PCT=0.001
STAGE_BAND_PCT=0.03

# Darvas (PR4 sweep G3, 원전 충실)
BOX_HIGH_LOOKBACK=20
BOX_HIGH_CONFIRM=1
BOX_HEIGHT_MAX_PCT=12
BOX_VALID_MIN_DAYS=5
BOX_STALE_DAYS=60
BREAKOUT_BUFFER=0.003
BREAKOUT_VOLUME_MULT=1.0
GAP_BUY_ON_PULLBACK=true
FUNDAMENTAL_REQUIRED=false

# 알림 (PR6)
NOTIFY_CHANNEL=console,file
SLACK_WEBHOOK_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# MCP
MCP_SERVER=korea-stock-analyzer

# 경로
DATA_DIR=./store
RESULT_DIR=./result

# 운영
LOG_LEVEL=INFO
TIMEZONE=Asia/Seoul
BENCHMARK_INDEX=KOSPI    # 백테스트 비교 지수
```

## 14. PR 로드맵

| PR | 범위 | 외부 의존 | 상태 |
| --- | --- | --- | --- |
| PR0 | `git init` + 레거시 격리/삭제 + 스캐폴드 + KIS 사전검증 | — | ✅ |
| PR1 | `kis_client` + `store` + `universe` + `sync` CLI. 2년 백필 + 일일 incremental | KIS | ✅ |
| PR2 | `indicators` + `stage` + `classify` CLI | — | ✅ |
| PR3 | `template` + `darvas` + `signals` + `signals` CLI | — | ✅ |
| PR4 | `backtest` + 파라미터 튜닝 + 벤치마크 비교. 원전 비교로 임계 재튜닝 | — | ✅ |
| PR5(UI) | `app/streamlit_app.py` + `charts.py` + 글로서리 툴팁 | streamlit/plotly | ✅ |
| PR6(F) | `universe`에 sector + 정확한 시총 (pykrx). 워치리스트 업종/시총 필터 | pykrx | ✅ |
| PR6(K) | `fundamentals` — KIS 분기 손익 + YoY/연속 성장/가속. 필터 + 컬럼 | KIS | ✅ |
| PR6(C) | `consensus` — yfinance 목표가/추정 EPS + 30일 상향 조정 추세 | yfinance | ✅ |
| PR7 | `portfolio` + KIS 잔고 동기화 + SELL/HOLD + 트레일링 | KIS | ⬜ |
| PR8 | `risk_news` + 알림(Slack/Telegram) + 리포트 강화 + cron 가이드 | (선택) | ⬜ |

**자동 주문 없음.** 향후 라이브 트레이딩 필요 시 별도 설계 단계 다시 진행.

PR3 머지 시점부터 시그널이 의미 있게 동작하나, PR4 백테스트 검증 전엔 시그널의 신뢰도가 미지수임을 사용자에게 README로 안내.

## 15. 테스트 & 옵저버빌리티

- **단위 테스트**: indicators, stage, darvas, sizing — 픽스처 시계열 기반. CI 가능.
- **통합 테스트**: signals.py가 픽스처 종목 셋에 대해 예상 시그널 생성하는지. KIS API는 `tests/fixtures/`의 정적 JSON 또는 vcr.py 카세트로 모킹.
- **백테스트 smoke**: 짧은 기간 + 소수 종목으로 30초 안에 통과하는 sanity.
- **JSON-line 로그**: `store/logs/{yyyymm}.jsonl` 에 `{ts, level, event, payload}`. 시그널 생성·KIS 호출·에러·메타 보강 모든 이벤트 기록.

## 16. v2+ (현 범위 밖)

- 인트라데이 웹소켓 트리거
- `compare_peers` 기반 Industry Leader 가중치
- 주봉/일봉 다중 시간프레임
- 포트폴리오 단위 리스크 (섹터 집중도, 상관계수)
- 자동 주문 (별도 설계 필요)
- 백테스트 그리드 서치 자동화

## 17. 본 문서가 답하지 않는 것

- 구체적 KIS 엔드포인트 URL/파라미터 / 응답 컬럼명 — PR1에서 실제 응답 받고 매핑 픽스
- 박스 검증기간 / RS 임계값 / 슬로프 lookback의 **최종값** — PR4 결과 보고 확정
