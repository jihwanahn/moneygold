# moneygold

KOSPI/KOSDAQ 스윙 트레이딩 **종목 추천 대시보드**.
Weinstein Stage Analysis + Minervini Trend Template + Darvas Box + 펀더멘털 + 애널 컨센서스 합성.

> ⚠️ **자동 매매 안 함.** 시스템은 *좋은 후보 종목 리스트*만 제공합니다. 매수 가격·시점·수량은 사용자가 차트 보고 직접 결정해 HTS/MTS에서 주문합니다.

설계 문서: [ARCHITECTURE.md](./ARCHITECTURE.md)

<p align="center"><sub>이 도구는 개인 매매 보조용이며, 투자 손익은 사용자 책임입니다.</sub></p>

---

## 5단계 Quickstart

다른 누가 fork했어도 아래 5단계만 따라하면 동작합니다.

### 1) 필수 자격증명 발급

| 출처 | 용도 | 발급 |
| --- | --- | --- |
| **한국투자증권 OpenAPI** | 일봉·지수·재무 데이터 (필수) | https://apiportal.koreainvestment.com → 가입 → "OpenAPI 신청 → 실전투자". 발급되는 `app_key`/`app_secret`을 .env에 |
| **KRX 데이터 포털 계정** | 종목 마스터·업종·시총 (필수) | https://data.krx.co.kr → 무료 회원가입. ID/비밀번호를 .env에 |
| Yahoo Finance | 컨센서스 (선택) | API 키 없음. 라이브러리만 설치하면 자동 |

### 2) Python 환경 + 의존성

```bash
# 권장: conda
conda create -n moneygold python=3.11 -y
conda activate moneygold

git clone <fork URL>
cd moneygold
pip install -e ".[dev,ui]"
```

설치 그룹 의미:
- `dev` = pytest, ruff, vcrpy
- `ui` = streamlit, plotly (대시보드)
- 핵심: pandas, pyarrow, requests, pykrx, yfinance, python-dotenv

### 3) `.env` 파일 작성

```bash
cp .env.example .env
$EDITOR .env   # 또는 nano/vim/code
```

**필수 4줄만 채우면 동작합니다**:
```ini
KRX_ID=<KRX 포털 ID>
KRX_PW=<KRX 포털 비밀번호>
KIS_APP_KEY=<한투 OpenAPI 앱키>
KIS_APP_SECRET=<한투 OpenAPI 앱시크릿>
```

나머지 (계좌번호, 알림 토큰, 전략 파라미터 등)는 기본값으로 두면 됩니다.

### 4) 초기 데이터 수집 (한 번에 ~50분)

```bash
# (a) 종목 마스터 + 지수 + 일봉 2년 백필 (~40분)
python -m moneygold.cli.sync --backfill

# (b) 펀더멘털 — 분기 매출/영업이익/EPS (~15분)
python -m moneygold.cli.sync --financials

# (c) 컨센서스 — 애널리스트 목표가/추정 EPS (~43분, 선택)
python -m moneygold.cli.sync --consensus
```

각 단계는 *증분 실행* 가능 — 이미 받은 데이터는 자동 skip. 이후 일일 운영은 `--daily` (몇 분).

### 5) 대시보드 실행

```bash
streamlit run src/moneygold/app/streamlit_app.py
```

브라우저가 자동으로 열림 (http://localhost:8501).
좌측 워치리스트에서 종목 선택 → 우측에 차트·미네비니 8조건·박스 상태가 표시됩니다.

처음 사용자는 페이지 상단의 **"❓ 이 대시보드 사용법"** 펼침 클릭. 도움말에 박스·Stage·차트 색상이 모두 설명되어 있습니다.

---

## 일상 운영

매일 장 마감(15:30) 이후:

```bash
python -m moneygold.cli.sync --daily       # 일봉 incremental
python -m moneygold.cli.signals --top 30   # 콘솔에 워치리스트 출력
# 또는 streamlit run ... (이미 떠 있으면 새로고침 → "캐시 초기화" 버튼)
```

주 1회/월 1회 추천:
```bash
python -m moneygold.cli.sync --financials --force-financials   # 분기 결산 시기에
python -m moneygold.cli.sync --consensus --force-consensus     # 컨센서스 변화 갱신
```

---

## 명령 치트시트

| 명령 | 용도 | 소요 |
| --- | --- | --- |
| `python -m moneygold.cli.sync --universe` | 종목 마스터·업종·시총만 (pykrx) | 1분 |
| `python -m moneygold.cli.sync --backfill` | 마스터 + 종목 일봉 2년 + 지수 | ~40분 |
| `python -m moneygold.cli.sync --daily` | 일일 incremental sync | ~10분 |
| `python -m moneygold.cli.sync --financials` | 분기 펀더멘털 (KIS) | ~15분 |
| `python -m moneygold.cli.sync --consensus` | 애널 컨센서스 (yfinance) | ~43분 |
| `python -m moneygold.cli.signals --top 30` | 콘솔 워치리스트 | ~30초 |
| `python -m moneygold.cli.classify` | Stage 분포 + Stage 2 종목 | ~40초 |
| `python -m moneygold.cli.backtest --start 20250101 --end 20260514` | 백테스트 (자동매매 가정) | ~12분 |
| `streamlit run src/moneygold/app/streamlit_app.py` | 대시보드 | — |
| `python scripts/verify_kis.py` | KIS API 사전검증 | ~10초 |
| `python scripts/inspect_template.py 005930` | 미네비니 8조건 진단 | ~5초 |
| `python scripts/inspect_stage.py 005930` | Weinstein Stage 변화 | ~1초 |
| `python scripts/funnel.py --asof 20260514` | 게이트별 통과율 | ~10초 |

---

## 디렉토리 구조

```
moneygold/
  ARCHITECTURE.md          ← 전략·시스템 설계 문서
  README.md                ← 이 파일
  pyproject.toml           ← 의존성
  .env                     ← 사용자 자격증명 (gitignored)
  .env.example             ← 템플릿

  src/moneygold/
    config.py              ← .env 로드 + 동결 dataclass
    indicators.py          ← SMA/ATR/RS/52w (pure functions)
    stage.py               ← Weinstein 4단계 상태머신
    template.py            ← Minervini 8조건
    darvas.py              ← 박스 상태머신
    fundamentals.py        ← 분기 매출/영업이익 정규화
    consensus.py           ← 애널 목표가/추정 + 상향 조정 추세
    signals.py             ← 합성 → DailySignals
    backtest.py            ← 워크포워드 시뮬레이터
    universe.py            ← pykrx 마스터·업종·시총
    data/
      kis_client.py        ← OAuth + rate limit + 페이지네이션
      kis_endpoints.py     ← URL/tr_id 매핑
      store.py             ← parquet atomic write
      sync.py              ← DataSync 오케스트레이터
    app/                   ← Streamlit 대시보드
      streamlit_app.py
      charts.py            ← plotly 빌더
      _glossary.py         ← 사이드바·컬럼 툴팁
    cli/                   ← python -m moneygold.cli.<name>
      sync.py · signals.py · classify.py · backtest.py

  store/                   ← 데이터 (gitignored)
    bars/{ticker}.parquet  ← 일봉
    index/{code}.parquet   ← KOSPI / KOSDAQ / KOSPI200 / KOSDAQ150
    meta/master.parquet    ← 종목 마스터 + 업종 + 시총
    financials/{ticker}.parquet   ← 분기 손익
    consensus/{ticker}.json       ← 애널 컨센서스
    signals/{biz_date}.json       ← 일일 시그널

  scripts/                 ← 진단·검증 도구
  tests/                   ← pytest (100+ tests)
```

---

## 전략 합성 (3-Layer Funnel)

```
2578 종목 (KOSPI + KOSDAQ)
     │  유동성 + MCAP 게이트
     ▼
~1300 종목
     │  Weinstein Stage 2 (장기 추세 상승)
     ▼
~1000 종목
     │  Minervini Template 8/8 (미국 대가의 품질 체크)
     ▼
~350 종목     ← 워치리스트
     │  Darvas Box 천장 돌파 (이벤트)
     ▼
0~10 종목/일  ← "오늘 즉시 검토" ⭐
```

추가 필터 (사이드바):
- 업종 / 시가총액 / 매출 YoY / 영업이익률 / 연속 성장 분기 / 가속
- 애널수 / 목표가 상승여력 / EPS 추정 상향 폭 / 30일 순 상향 분석가 수

---

## 트러블슈팅

**"KRX 로그인 실패"**
→ `.env`에 KRX_ID / KRX_PW가 비어있거나 잘못됨. data.krx.co.kr 직접 접속해서 로그인 되는지 확인.

**"KIS_APP_KEY / KIS_APP_SECRET 미설정"**
→ `.env` 채우기. 발급에 1-2일 걸릴 수 있음.

**KIS 500 에러 (재시도 후 성공)**
→ KIS 서버 일시적 응답 실패. 자동 재시도 (지수 백오프 5회). 무시 가능.

**pykrx 마스터 빈 응답**
→ KRX 포털 일시 점검 또는 휴장일. 다음날 다시 시도.

**Streamlit 코드 변경 미반영**
→ Streamlit이 의존 모듈을 캐싱. **Ctrl+C 후 `streamlit run ...` 재실행**.

**yfinance 404 (특정 종목)**
→ Yahoo Finance가 한국 소형주는 커버 안 함. 컨센서스 컬럼이 NaN으로 표시됨, 정상 동작.

**시그널이 거의 안 나옴**
→ 사이드바 필터를 너무 강하게 걸었을 수 있음. RS rank 70 / 박스 모든 상태 / 시총 전체 범위로 리셋해보세요.

---

## 자동화 (선택)

매일 같은 시각 자동 실행하려면 crontab:

```cron
# 매 영업일 17:00에 일일 sync, 17:30에 시그널 JSON 저장
0 17 * * 1-5  cd /path/to/moneygold && conda run -n moneygold python -m moneygold.cli.sync --daily   >> store/logs/sync.log 2>&1
30 17 * * 1-5 cd /path/to/moneygold && conda run -n moneygold python -m moneygold.cli.signals --export >> store/logs/signals.log 2>&1
```

---

## 라이선스 / 면책

본 시스템은 **개인 매매 보조 도구**입니다. 어떤 종목도 매수·매도를 권유하지 않으며, 시그널 결과를 따랐을 때 발생하는 손익은 전적으로 사용자 책임입니다. 백테스트는 자동매매 가정 하의 *과거 시뮬레이션*이며 미래 수익을 보장하지 않습니다.
