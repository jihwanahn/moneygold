# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

moneygold — KOSPI/KOSDAQ **종목 추천 대시보드**. Weinstein Stage + Minervini Trend Template + Darvas Box + 분기 펀더멘털 + 애널 컨센서스 합성. **자동 주문 없음** — 시스템은 워치리스트만 만들고, 진입가·시점·수량은 사용자가 차트 보고 직접 결정해 HTS/MTS에서 주문.

전체 설계는 [ARCHITECTURE.md](./ARCHITECTURE.md), 사용자 가이드는 [README.md](./README.md). 이 파일은 거기서 못 보는 운영/협업 메모만.

## Project state

PR0~PR6(F·K·C) 완료. PR7 (portfolio KIS 잔고 동기화) + PR8 (알림) 미완. ARCHITECTURE.md §14 로드맵 참조.

**"가속화 장기투자" (DGI) 탭 추가 (2026-05)**: 모멘텀 시스템과 *분리된* 별도 시스템. `strategies/value_long_term/` + `app/streamlit_app.py`의 5번째 탭. 사용자 컨셉: 배당 재투자 + 추가납입 + 주가/배당성장 선순환으로 자산증식 가속.

**KIS → pykrx + DART 마이그레이션 (2026-05, PR-F/H1/H2/I)**: 일봉/지수/배당/재무 ROE/배당빈도 모두 KIS에서 이전. KIS는 잔고(`inquire-balance`)만 담당. 기존 KIS fetcher는 `source="kis"` fallback 옵션으로 보존. ARCHITECTURE.md §2 참조.

레거시 v1 코드(`screener.py`/`analyzer.py`/`advisor.py`/`screen.py`/`track.py`)는 git history(`c7eb991`)에만 있음. `git log --diff-filter=D --name-only -1`로 확인.

## Setup (개발용)

```bash
conda create -n moneygold python=3.11 -y && conda activate moneygold
pip install -e ".[dev,ui]"
cp .env.example .env  # KRX_ID/PW + KIS_APP_KEY/SECRET 채우기
python scripts/verify_kis.py
```

신규 사용자 가이드는 README.md "5단계 Quickstart"에 별도.

## Architecture (한 줄씩)

- `src/moneygold/config.py` — `.env` 로드, 동결 dataclass로 설정 노출. **모든 파라미터는 여기 통과**. `KISConfig` / `DartConfig` 분리.
- `src/moneygold/data/` — **pykrx 일봉/펀더멘털 + DART 재무지표/배당공시 + KIS 잔고**. 종목 단위 parquet 스토어 + sync 오케스트레이터.
  - `pykrx_bars.py` — 일봉·지수 OHLCV 종목별 fetcher (`fetch_bars_pykrx`, `fetch_index_bars_pykrx`). `backfill_bars`의 기본 source. 긴 기간/신규 종목 backfill에 적합.
  - `sync.py::sync_bars_kr_pykrx_batch` / `sync_indices_kr_pykrx` — **일자별 batch fetch** (5d × 2시장 = 10 호출, ~20초). 매일 incremental에 최적. cli `--daily` + streamlit "🇰🇷 한국 갱신"이 이걸 사용.
  - `dividends.py` — pykrx 12월말 DPS → `fiscal_year` 컬럼으로 회계연도 귀속. KIS 예탁원 fetcher는 legacy로 보존.
  - `dart_indicators.py` — DART `fnlttSinglIndx.json` 결과 (ROE 등). DGI 펀더멘털 점수의 ROE 출처.
  - `dart_business.py` — DART 사업보고서 주요사항 (회사정보 + 증자/감자 + 자사주 흐름 + 원본 재무제표). KOSPI200+KOSDAQ150 한정 sync. Streamlit 상세 화면 expander 4종이 이걸 표시. `kospi200_kosdaq150_tickers()` helper 포함.
  - `kis_client.py` — 잔고 (`inquire-balance`)만 실사용. 나머지 fetcher (`fetch_daily_bars`, `fetch_index_bars`, finance) 는 `source="kis"` fallback용 보존.
- `src/moneygold/strategies/value_long_term/` — "가속화 장기투자" (DGI) 모듈.
  - `dart_client.py` — DART 클라이언트 (자사주 공시 + 재무지표 + 배당결정 카운트). `asof` 명시 인자.
  - `scoring.py`, `scoring_rules.py` — DGI 100점 점수표 (배당 40 + 자본이득 30 + 펀더멘털 20 + 주주환원 10).
  - `drip.py` — 배당재투자 + 적립식 시뮬레이터 (순수 함수).
- `src/moneygold/{indicators,stage,template,darvas}.py` — 모멘텀 전략 코어. 모두 *pure functions* (외부 호출 없음).
- `src/moneygold/{fundamentals,consensus}.py` — KIS finance(레거시) / yfinance 컨센서스 — 캐싱 포함, signals.py가 옵션으로 받음.
- `src/moneygold/signals.py` — 합성. **단일 진실: BUY/HOLD/SELL/Watchlist 결정은 여기서만** (모멘텀 시스템). DGI 시스템과는 *완전 분리*.
- `src/moneygold/backtest.py` — 워크포워드 시뮬레이터 (signals.py를 그대로 재사용).
- `src/moneygold/universe.py` — pykrx 마스터 + 업종 + 시가총액. `_REIT_NAME = (?<!메)리츠`로 메리츠 false positive 보호.
- `src/moneygold/app/` — Streamlit 대시보드 (4+1 탭: BUY 후보/오늘 상승/Momentum Breakout/백테스트/💎가속화 장기투자). `_glossary.py`가 모든 툴팁/도움말 텍스트.
- `src/moneygold/cli/` — `python -m moneygold.cli.<name>` 진입점들. `dgi.py`는 DGI 스크리닝 CLI.
- `scripts/` — 진단 도구 (verify_kis, probe_kis_finance, inspect_*, funnel).

## 코드 작성 시 못박힌 원칙

- **재현성.** 모든 함수에 `asof: str` 또는 명시적 날짜를 받기. `datetime.now()` 호출 금지 (CLI 진입점에서만 1회).
- **순수 함수 우선.** `indicators.py`는 `pd.Series → pd.Series`, 외부 호출 없음. `stage.py`/`template.py`/`darvas.py`/`drip.py`도 동일.
- **Parquet 원자성.** 쓰기는 항상 tmp + atomic rename. `data/store.py` 헬퍼 사용.
- **(ticker, date) unique.** Append 시 중복 거부.
- **자동 주문 금지.** KIS 주문 엔드포인트(`order-cash` 등) 코드에 들어가면 안 됨. PR 분기점에서 발견되면 즉시 reject.
- **KIS 의존성 최소.** 신규 KIS read 엔드포인트 추가 금지. 잔고 외엔 pykrx + DART 사용. 기존 KIS fetcher는 `source="kis"` 옵션으로만 유지.
- **단일 책임의 시그널 레이어.** `signals.py`가 모멘텀 BUY/HOLD/SELL/Watchlist 결정. DGI는 별도 `strategies/value_long_term/scoring.py`에서. 두 시스템 교차 의존 금지.
- **외부 데이터는 캐시.** pykrx/DART 호출도 비싸니 `store/`에 parquet/JSON으로 보관. signals.py와 scoring.py는 캐시만 읽음.
- **UI는 view-only.** `app/`은 signals.py / scoring.py 결과를 렌더만. 비즈니스 로직 들어가면 안 됨.

## 자주 쓸 명령

```bash
pytest -q                          # 테스트 (330+)
ruff check src tests              # 린트

python scripts/verify_kis.py      # KIS 사전검증 (잔고용)
python -m moneygold.cli.sync --universe        # pykrx 마스터 갱신 (분당 1회면 충분)
python -m moneygold.cli.sync --backfill        # 초기 2년 일봉 백필 (pykrx, ~10-15분)
python -m moneygold.cli.sync --daily           # 일일 incremental (pykrx batch, ~30초)
python -m moneygold.cli.sync --financials      # KIS 분기 손익 (~15분, 레거시)
python -m moneygold.cli.sync --dividends       # pykrx 일별 batch (~1분, 24 호출로 11년치 전종목)
python -m moneygold.cli.sync --dart-indicators # DART 재무지표 (ROE 등, 전체 ~28분)
python -m moneygold.cli.sync --dart-business --scope k200kq150  # K200+KQ150 회사/증자/자사주/raw 재무 (~15분, 350종목)
python -m moneygold.cli.sync --consensus       # yfinance 컨센서스 (~43분)
python -m moneygold.cli.signals --top 30       # 모멘텀 워치리스트
python -m moneygold.cli.dgi --screen --top 30  # DGI 가속화 장기투자 점수표
python -m moneygold.cli.classify               # Stage 분포
python -m moneygold.cli.backtest --start 20251101 --end 20260514

streamlit run src/moneygold/app/streamlit_app.py
```

## MCP 주의

`korea-stock-analyzer` MCP는 *PythonExecutor가 child Python stdout을 통째 JSON 파싱*해서, pykrx가 KRX 로그인 메시지를 stdout으로 print하면 깨짐 — `get_financial_data`/`get_technical_indicators`/`analyze_equity` 등 pykrx 의존 도구는 사용 불가. `search_news`는 동작하지만 더미 데이터. **현재는 KIS 의존성도 거의 제거됐으므로 pykrx + DART 직접 호출이 표준이고 MCP는 보조용**.

## 변경 시 주의

- 시그널 결과 스키마(WatchlistEntry 등)는 `signals.py`/`cli/signals.py`/`app/streamlit_app.py` 세 곳이 같이 쓰므로 변경 시 일치 확인.
- DGI 점수표 스키마(`ScoreBreakdown`)는 `strategies/value_long_term/scoring.py`/`cli/dgi.py`/`app/streamlit_app.py` 세 곳에서 사용. 컬럼 추가·삭제 시 동기 유지.
- `app/streamlit_app.py`는 `@st.cache_data` 데코레이터를 씀. 새 파라미터 추가 시 Streamlit 프로세스 재시작 필요 (`Ctrl+C` → `streamlit run`).
- **sync 후 streamlit cache 무효화**: `app/streamlit_app.py`의 "🇰🇷 한국 갱신" 버튼은 sync 완료 시 `st.cache_data.clear()` + `st.session_state["asof_str"]` 자동 갱신. session_state는 cache_data.clear() 영향 X라 명시 갱신 필수 — 이게 빠지면 갱신 후에도 차트가 옛 날짜 그대로.
- **거래일 캘린더 union**: `_available_trading_days`는 KOSPI200/KOSDAQ150/^GSPC의 *union* (length-biased X). 가장 긴 캘린더만 택하면 ^GSPC 가 KR보다 길 때 picker max가 미국 latest로 잡혀 한국 거래일 선택 불가 버그.
- ARCHITECTURE.md의 파라미터 표(§4 Stage, §5 Template, §6 Darvas, §13 env)는 백테스트(PR4) 결과로 조정됨. 코드는 모든 파라미터를 `config.py` 경유로 받아야 하며 하드코딩 금지.
- yfinance 한국 종목 커버리지는 대형주·중형주만. 소형주는 `available=False` 정상. 모든 펀더멘털/컨센서스 필터는 NaN을 *통과*시켜야 (데이터 없는 종목을 강제로 거르면 안 됨).
- `dividends.parquet`의 `fiscal_year` 컬럼: pykrx 출처 행은 채워짐, KIS legacy 행은 NaN. `scoring.annual_dps_per_year`는 pykrx 행이 하나라도 있으면 *그것만* 사용 (이중 합산 방지).
- pykrx `get_market_fundamental_by_date`는 미래 날짜 요청 시 컬럼 KeyError 던짐 → 호출 전 `asof`로 윈도우 clip 필수 (`data/dividends.py:_pykrx_yearend_dps` 참조).
- **DGI 탭 fresh screen은 ~88분 (전체 2,580종목)**. Streamlit 탭에서 자동 트리거 절대 금지 — 사용자가 다른 asof 선택 시 캐시 미스 → 무한 루프처럼 보임. 정확한 asof 캐시 없으면 *가장 최근 value_scores 파일 fallback* + 경고만 표시. fresh screen은 명시 버튼 "🔄 DGI 점수 강제 재계산"으로만.
- **DART 일일 한도 ~10,000 호출** (개인키). 전체 KR × 사업보고서 4 endpoint × 5년 = ~50K로 한도 초과 → `--dart-business`는 `--scope k200kq150`이 기본 (350종목 × 4ep × 5y ≈ 7K 호출).
