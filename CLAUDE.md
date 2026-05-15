# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

moneygold — KOSPI/KOSDAQ **종목 추천 대시보드**. Weinstein Stage + Minervini Trend Template + Darvas Box + 분기 펀더멘털 + 애널 컨센서스 합성. **자동 주문 없음** — 시스템은 워치리스트만 만들고, 진입가·시점·수량은 사용자가 차트 보고 직접 결정해 HTS/MTS에서 주문.

전체 설계는 [ARCHITECTURE.md](./ARCHITECTURE.md), 사용자 가이드는 [README.md](./README.md). 이 파일은 거기서 못 보는 운영/협업 메모만.

## Project state

PR0~PR6(F·K·C) 완료. PR7 (portfolio KIS 잔고 동기화) + PR8 (알림) 미완. ARCHITECTURE.md §14 로드맵 참조.

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

- `src/moneygold/config.py` — `.env` 로드, 동결 dataclass로 설정 노출. **모든 파라미터는 여기 통과**.
- `src/moneygold/data/` — KIS 클라이언트 (시세·재무 양쪽), parquet 스토어, sync 오케스트레이터.
- `src/moneygold/{indicators,stage,template,darvas}.py` — 전략 코어. 모두 *pure functions* (외부 호출 없음).
- `src/moneygold/{fundamentals,consensus}.py` — 펀더멘털(KIS) / 컨센서스(yfinance) — 캐싱 포함, signals.py가 옵션으로 받음.
- `src/moneygold/signals.py` — 합성. **단일 진실: BUY/HOLD/SELL/Watchlist 결정은 여기서만**.
- `src/moneygold/backtest.py` — 워크포워드 시뮬레이터 (signals.py를 그대로 재사용).
- `src/moneygold/universe.py` — pykrx 마스터 + 업종 + 시가총액.
- `src/moneygold/app/` — Streamlit 대시보드. `_glossary.py`가 모든 툴팁/도움말 텍스트.
- `src/moneygold/cli/` — `python -m moneygold.cli.<name>` 진입점들.
- `scripts/` — 진단 도구 (verify_kis, probe_kis_finance, inspect_*, funnel).

## 코드 작성 시 못박힌 원칙

- **재현성.** 모든 함수에 `asof: str` 또는 명시적 날짜를 받기. `datetime.now()` 호출 금지 (CLI 진입점에서만 1회).
- **순수 함수 우선.** `indicators.py`는 `pd.Series → pd.Series`, 외부 호출 없음. `stage.py`/`template.py`/`darvas.py`도 동일.
- **Parquet 원자성.** 쓰기는 항상 tmp + atomic rename. `data/store.py` 헬퍼 사용.
- **(ticker, date) unique.** Append 시 중복 거부.
- **자동 주문 금지.** KIS 주문 엔드포인트(`order-cash` 등) 코드에 들어가면 안 됨. PR 분기점에서 발견되면 즉시 reject.
- **단일 책임의 시그널 레이어.** `signals.py`가 BUY/HOLD/SELL/Watchlist 결정. 다른 모듈은 신호 생성/관찰만, 결정 안 함.
- **외부 데이터는 캐시.** KIS 호출 비싸니 `store/`에 parquet/JSON으로 보관. signals.py는 캐시만 읽음.
- **UI는 view-only.** `app/`은 signals.py 결과를 렌더만. 비즈니스 로직 들어가면 안 됨.

## 자주 쓸 명령

```bash
pytest -q                          # 테스트 (100+)
ruff check src tests              # 린트

python scripts/verify_kis.py      # KIS 사전검증
python -m moneygold.cli.sync --backfill        # 초기 2년 백필 (~40분)
python -m moneygold.cli.sync --daily           # 일일 incremental
python -m moneygold.cli.sync --financials      # KIS 분기 손익 (~15분)
python -m moneygold.cli.sync --consensus       # yfinance 컨센서스 (~43분)
python -m moneygold.cli.signals --top 30       # 콘솔 워치리스트
python -m moneygold.cli.classify               # Stage 분포
python -m moneygold.cli.backtest --start 20251101 --end 20260514

streamlit run src/moneygold/app/streamlit_app.py
```

## MCP 주의

`korea-stock-analyzer` MCP는 *PythonExecutor가 child Python stdout을 통째 JSON 파싱*해서, pykrx가 KRX 로그인 메시지를 stdout으로 print하면 깨짐 — `get_financial_data`/`get_technical_indicators`/`analyze_equity` 등 pykrx 의존 도구는 사용 불가. `search_news`는 동작하지만 더미 데이터. KIS finance 엔드포인트(`fundamentals.py`) + yfinance(`consensus.py`)로 우회.

## 변경 시 주의

- 시그널 결과 스키마(WatchlistEntry 등)는 `signals.py`/`cli/signals.py`/`app/streamlit_app.py` 세 곳이 같이 쓰므로 변경 시 일치 확인.
- `app/streamlit_app.py`는 `@st.cache_data` 데코레이터를 씀. 새 파라미터 추가 시 Streamlit 프로세스 재시작 필요 (`Ctrl+C` → `streamlit run`).
- ARCHITECTURE.md의 파라미터 표(§4 Stage, §5 Template, §6 Darvas, §13 env)는 백테스트(PR4) 결과로 조정됨. 코드는 모든 파라미터를 `config.py` 경유로 받아야 하며 하드코딩 금지.
- yfinance 한국 종목 커버리지는 대형주·중형주만. 소형주는 `available=False` 정상. 모든 펀더멘털/컨센서스 필터는 NaN을 *통과*시켜야 (데이터 없는 종목을 강제로 거르면 안 됨).
