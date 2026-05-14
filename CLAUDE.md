# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

moneygold v2 — KOSPI/KOSDAQ 스윙 트레이딩 **시그널 생성기**. Weinstein Stage + Minervini Trend Template + Darvas Box 합성. **자동 주문 없음** (read-only로 KIS API 사용, 사용자가 HTS/MTS에서 수동 매매).

전체 설계는 [ARCHITECTURE.md](./ARCHITECTURE.md)에 있고, 이 파일은 거기서 못 보는 운영/협업 메모만.

## Project state

PR0(스캐폴드 + KIS 사전검증 스크립트) 완료. 이후 PR1~PR6은 ARCHITECTURE.md §14 로드맵 참조. 현재 `src/moneygold/` 하위 모듈들은 대부분 빈 파일 — 각 PR에서 채워나감. 빈 모듈을 import해도 동작하지 않는 게 정상.

레거시 v1 코드(`screener.py`/`analyzer.py`/`advisor.py`/`screen.py`/`track.py`)는 git history에는 남아있지만 작업 트리에선 완전 삭제됨. `git log --diff-filter=D --name-only -1` 로 확인 가능.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # 후 KIS_APP_KEY / KIS_APP_SECRET 채우기
python scripts/verify_kis.py  # PR1 진입 전 사전검증
```

## Architecture (한 줄씩)

- `src/moneygold/config.py` — `.env` 로드, 동결 dataclass로 설정 노출. **모든 파라미터는 여기 통과**.
- `src/moneygold/data/` — KIS 클라이언트, parquet 스토어, MCP 래퍼, 일일 sync.
- `src/moneygold/{indicators,stage,template,darvas,signals}.py` — 전략 코어. 위에서 아래로 의존 (signals가 stage/template/darvas를 합성).
- `src/moneygold/{portfolio,sizing}.py` — 보유 종목 상태(KIS 잔고 truth) + 사이즈 가이드.
- `src/moneygold/{backtest,report,notify}.py` — 검증, 출력, 알림.
- `src/moneygold/cli/` — `python -m moneygold.cli.<name>` 진입점들.
- `scripts/verify_kis.py` — PR0 사전검증. PR1 진입 전 반드시 통과해야 함.

## 코드 작성 시 못박힌 원칙

- **재현성.** 모든 함수에 `asof: str` 또는 명시적 날짜를 받기. `datetime.now()` 호출 금지 (CLI 진입점에서만 1회).
- **순수 함수 우선.** `indicators.py`는 `pd.Series → pd.Series`, 외부 호출 없음.
- **Parquet 원자성.** 쓰기는 항상 tmp + atomic rename. `data/store.py` 헬퍼 사용.
- **(ticker, date) unique.** Append 시 중복 거부.
- **자동 주문 금지.** KIS 주문 엔드포인트(`order-cash` 등) 코드에 들어가면 안 됨. PR 분기점에서 발견되면 즉시 reject.
- **단일 책임의 시그널 레이어.** `signals.py`가 BUY/HOLD/SELL을 결정. 다른 모듈은 신호를 생성/관찰만, 결정 안 함.

## 자주 쓸 명령

```bash
pytest                           # 테스트
ruff check src tests            # 린트
python scripts/verify_kis.py    # KIS 사전검증
python -m moneygold.cli.sync    # (PR1+) 데이터 동기화
python -m moneygold.cli.signals # (PR3+) 일일 시그널
python -m moneygold.cli.backtest # (PR4+) 백테스트
python -m moneygold.cli.daily   # (PR6+) 종합 일일 실행 (sync → portfolio → signals → notify)
```

## 변경 시 주의

- ARCHITECTURE.md의 파라미터 표(§4 Stage, §5 Template, §6 Darvas, §13 env)는 백테스트(PR4) 결과로 *변경될 예정*. 코드는 모든 파라미터를 `config.py` 경유로 받아야 하며 하드코딩 금지.
- PR4 백테스트 결과는 ARCHITECTURE.md §2의 PR0 검증 결과에 의존. 폐지 종목 데이터 미확보가 확정되면 §10 백테스트 섹션에 *생존편향 경고*를 결과 헤더로 출력하도록 구현 필요.
- 시그널 출력 JSON 스키마는 `report.py`/`notify.py` 양쪽이 같이 쓰므로, 변경 시 둘 다 갱신.
