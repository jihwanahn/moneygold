"""US DGI (가속화 장기투자) 점수표. 100점 만점. US 시장 특성에 맞춰 튜닝.

KR(`scoring_rules.py`) 대비 차이:
  - **yield 비중 ↓** (10→8, 임계 완화): US 배당성장주는 yield 1.5~3%가 정상. 고yield는 오히려
    저성장/위험 신호인 경우가 많음.
  - **연속 인상 비중 ↑↑** (10→15): US DGI의 핵심 지표. 배당귀족(25년)/킹(50년) 문화.
  - **배당 빈도 제외**: US는 거의 모든 배당주가 분기배당 → 변별력 없음.
  - **주주환원(자사주) → 배당귀족 지위로 대체** (10점): US 자사주 데이터는 신뢰도 낮음.
    연속 인상 연수 기반 King/Aristocrat/Champion/Contender 등급으로 장수 프리미엄 부여.

| 카테고리      | 항목                  | 배점 |
|---------------|-----------------------|------|
| 배당 (45)     | 현재 배당수익률       |  8   |
|               | 연속 인상 연수        | 15   |
|               | 5년 DPS CAGR          | 12   |
|               | 배당성향 안정         | 10   |
| 자본이득 (30) | 5년 주가 CAGR         | 15   |
|               | 200일선 위 거래일 %   | 10   |
|               | 5년 총수익 양수       |  5   |
| 펀더멘털 (15) | ROE (현재, yfinance)  |  8   |
|               | EPS 변동계수 (SEC)    |  7   |
| 귀족지위 (10) | King/Aristocrat/...   | 10   |

연속 인상이 배당(15)+귀족지위(10) 양쪽에 반영 — US DGI 장수 프리미엄 의도적 강조.
순수 함수 — 외부 호출 없음.
"""
from __future__ import annotations

# ============================================================
# 배당 (45점)
# ============================================================

def score_dividend_yield(yield_pct: float | None) -> int:
    """현재 배당수익률(%). US 성장주는 yield 낮음 → 임계 완화, 최대 8점."""
    if yield_pct is None or yield_pct <= 0:
        return 0
    if yield_pct >= 5:
        return 8
    if yield_pct >= 3:
        return 6
    if yield_pct >= 2:
        return 4
    if yield_pct >= 1:
        return 2
    return 0


def score_consecutive_increase(years: int | None) -> int:
    """배당 연속 인상 연수. US DGI 핵심 — 최대 15점, 15년에서 포화."""
    if years is None:
        return 0
    if years >= 15:
        return 15
    if years >= 10:
        return 12
    if years >= 5:
        return 8
    if years >= 3:
        return 4
    return 0


def score_dps_cagr(cagr_pct: float | None) -> int:
    """5년 DPS CAGR(%). US 우량 성장주 ~6~10%. 최대 12점."""
    if cagr_pct is None:
        return 0
    if cagr_pct >= 10:
        return 12
    if cagr_pct >= 7:
        return 9
    if cagr_pct >= 5:
        return 6
    if cagr_pct >= 3:
        return 3
    if cagr_pct >= 0:
        return 1
    return 0


def score_payout_stability(payout_ratio_pct: float | None) -> int:
    """배당성향(%). US sweet spot 30~70%. 최대 10점.

    음수(적자)나 100% 초과는 위험 — 0점.
    """
    if payout_ratio_pct is None:
        return 0
    if payout_ratio_pct < 0:
        return 0
    if 30 <= payout_ratio_pct <= 70:
        return 10
    if 15 <= payout_ratio_pct <= 80:
        return 6
    if payout_ratio_pct <= 90:
        return 2
    return 0


# ============================================================
# 자본이득 (30점) — KR과 동일
# ============================================================

def score_price_cagr(cagr_pct: float | None) -> int:
    """5년 주가 CAGR(%). 최대 15점."""
    if cagr_pct is None:
        return 0
    if cagr_pct >= 15:
        return 15
    if cagr_pct >= 10:
        return 10
    if cagr_pct >= 5:
        return 5
    if cagr_pct >= 0:
        return 2
    return 0


def score_above_sma200_ratio(ratio_0to1: float | None) -> int:
    """5년 거래일 중 종가 ≥ 200일선 비율 (0~1). 최대 10점."""
    if ratio_0to1 is None or ratio_0to1 < 0:
        return 0
    if ratio_0to1 >= 0.80:
        return 10
    if ratio_0to1 >= 0.60:
        return 6
    if ratio_0to1 >= 0.40:
        return 3
    return 0


def score_positive_total_return(total_return_pct: float | None) -> int:
    """5년 총수익(배당 포함) 양수면 5점."""
    if total_return_pct is None:
        return 0
    return 5 if total_return_pct > 0 else 0


# ============================================================
# 펀더멘털 (15점)
# ============================================================

def score_roe(roe_pct: float | None) -> int:
    """ROE(%). US는 ROE 높은 편 → 임계 상향. 최대 8점.

    (KR은 5년 평균이지만 US는 yfinance 현재값 — 단일 시점 proxy)
    """
    if roe_pct is None:
        return 0
    if roe_pct >= 20:
        return 8
    if roe_pct >= 15:
        return 6
    if roe_pct >= 10:
        return 4
    if roe_pct >= 5:
        return 2
    return 0


def score_eps_stability(cv: float | None) -> int:
    """EPS 변동계수 (std/mean). 낮을수록 안정. 최대 7점. 음수/0 mean은 0점."""
    if cv is None or cv < 0:
        return 0
    if cv < 0.20:
        return 7
    if cv < 0.30:
        return 5
    if cv < 0.50:
        return 3
    return 0


# ============================================================
# 배당귀족 지위 (10점) — 주주환원 대체
# ============================================================

def score_aristocrat_status(consecutive_years: int | None) -> int:
    """연속 인상 연수 기반 배당귀족 등급. US DGI 장수 프리미엄.

    King (50년+)       → 10
    Aristocrat (25년+) →  8
    Champion (15년+)   →  5
    Contender (10년+)  →  3
    그 외              →  0
    """
    if consecutive_years is None:
        return 0
    if consecutive_years >= 50:
        return 10
    if consecutive_years >= 25:
        return 8
    if consecutive_years >= 15:
        return 5
    if consecutive_years >= 10:
        return 3
    return 0


def aristocrat_label(consecutive_years: int | None) -> str:
    """등급 라벨 (UI 표시용)."""
    if consecutive_years is None:
        return ""
    if consecutive_years >= 50:
        return "King (50년+)"
    if consecutive_years >= 25:
        return "Aristocrat (25년+)"
    if consecutive_years >= 15:
        return "Champion (15년+)"
    if consecutive_years >= 10:
        return "Contender (10년+)"
    return ""


# ============================================================
# Totals & grading
# ============================================================

DIVIDEND_MAX = 45
CAPITAL_MAX = 30
FUNDAMENTAL_MAX = 15
ARISTOCRAT_MAX = 10
TOTAL_MAX = DIVIDEND_MAX + CAPITAL_MAX + FUNDAMENTAL_MAX + ARISTOCRAT_MAX

# 등급 컷: income 컴파운더 보정. capital_total(30)이 5년 price CAGR≥15%를 요구해
# 성숙 배당킹(KO/JNJ 등)은 어느 5년 윈도우에서도 capital이 낮음 → 80/70 컷이면
# 대표 DGI 종목이 C로 떨어져 변별력 상실. 75/65로 하향: 배당킹은 B, 성장+배당 겸비는 A.
GRADE_A_THRESHOLD = 75
GRADE_B_THRESHOLD = 65


def grade(total: int) -> str:
    if total >= GRADE_A_THRESHOLD:
        return "A"
    if total >= GRADE_B_THRESHOLD:
        return "B"
    return "C"
