# moneygold

KOSPI/KOSDAQ 스윙 트레이딩 시그널 생성기. Weinstein Stage Analysis + Minervini Trend Template + Darvas Box 합성.

**자동 주문 안 함.** 매일 시그널을 출력하면 사용자가 HTS/MTS에서 수동 주문.

상세 설계: [ARCHITECTURE.md](./ARCHITECTURE.md)

## 현재 상태

- ✅ PR0: 스캐폴드 + KIS 사전검증 스크립트
- ⬜ PR1: KIS 클라이언트 + 데이터 sync
- ⬜ PR2: 인디케이터 + Stage 분류기
- ⬜ PR3: Minervini Template + Darvas Box + 시그널 생성
- ⬜ PR4: 백테스트
- ⬜ PR5: Portfolio 동기화 + SELL/HOLD
- ⬜ PR6: 알림 + 사이드카

## 셋업

```bash
# 1) 가상환경 + 의존성 (conda 권장)
conda create -n moneygold python=3.11 -y
conda activate moneygold
pip install -e ".[dev,ui]"   # ui = streamlit + plotly

# 2) 환경변수
cp .env.example .env
# .env 열어서 채우기:
#   KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO  (KIS API)
#   KRX_ID / KRX_PW                                 (pykrx가 KRX 로그인 요구)

# 3) KIS 사전검증 (PR1 진입 전 필수)
python scripts/verify_kis.py
```

## API 키 받는 법

**KIS Open API (한국투자증권)**
1. https://apiportal.koreainvestment.com 접속, 회원가입/로그인
2. **앱 등록** → "OpenAPI 신청" → 실전투자 신청
3. **앱키(`app_key`)** + **앱시크릿(`app_secret`)** 발급
4. 계좌번호는 본인 HTS/MTS에서 종합계좌번호 확인 (예: `12345678-01`)
   - `KIS_ACCOUNT_NO` = 앞 8자리
   - `KIS_ACCOUNT_PROD_CD` = 뒤 2자리 (보통 `01`)

**MCP (korea-stock-analyzer)**
- 이미 Claude Code MCP로 연결되어 있어 별도 키 필요 없음

## 대시보드 (Streamlit)

```bash
# 데이터가 이미 sync 돼 있다고 가정
streamlit run src/moneygold/app/streamlit_app.py
```

브라우저에 자동으로 열림 (보통 http://localhost:8501).

- **사이드바**: 기준일, 시장, RS rank 최소, 박스 상태 필터, 표시 개수
- **상단 카드**: 후보 풀 수, 박스 돌파 수, RS rank 평균/rs_mom 최대
- **좌측 워치리스트**: Stage 2 + Template 통과 종목 (정렬·검색·행 선택 가능)
- **우측 차트**: 선택 종목의 캔들 + SMA50/150/200 + Darvas 박스 + Stage 배경색
- **8조건 패널**: 미네비니 Template 통과/실패 항목별
- **하단**: 오늘 박스 돌파 종목 표 + RS rank 분포 히스토그램

## 라이선스 / 면책

본 시스템은 개인 매매 *보조* 도구. 투자 손익은 사용자 책임.
