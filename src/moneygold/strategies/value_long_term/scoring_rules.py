"""DGI (가속화 장기투자) 점수표. 100점 만점.

| 카테고리      | 항목                  | 배점 |
|---------------|-----------------------|------|
| 배당 (40)     | 배당수익률            | 10   |
|               | 연속 인상 연수        | 10   |
|               | 5년 DPS CAGR          | 10   |
|               | 배당성향 안정         |  5   |
|               | 분기/월 배당 빈도     |  5   |
| 자본이득 (30) | 5년 주가 CAGR         | 15   |
|               | 200일선 위 거래일 %   | 10   |
|               | 5년 총수익 양수       |  5   |
| 펀더멘털 (20) | 5년 ROE 평균          | 10   |
|               | EPS 변동계수          | 10   |
| 주주환원 (10) | 자사주 소각 이력      |  5   |
|               | 연간 소각 빈도        |  5   |

ValueTrader 점수표와 달리 PER/PBR 저평가 게이트 없음 — DGI는 PER 15~25 정상.
모든 함수는 ``Optional[float]`` 또는 ``Optional[int]``를 받아 데이터 부재(None)는 0점.
순수 함수 — 외부 호출 없음. ARCHITECTURE.md 원칙에 부합.
"""
from __future__ import annotations

# ============================================================
# 배당 (40점)
# ============================================================

def score_dividend_yield(yield_pct: float | None) -> int:
    """현재 배당수익률(%). DRIP의 초기 추진력."""
    if yield_pct is None or yield_pct <= 0:
        return 0
    if yield_pct >= 7:
        return 10
    if yield_pct >= 5:
        return 7
    if yield_pct >= 3:
        return 5
    if yield_pct >= 1:
        return 2
    return 0


def score_consecutive_increase(years: int | None) -> int:
    """배당 연속 인상 연수. 동결은 인정 X (ValueTrader 명세와 동일)."""
    if years is None:
        return 0
    if years >= 10:
        return 10
    if years >= 5:
        return 7
    if years >= 3:
        return 4
    return 0


def score_dps_cagr(cagr_pct: float | None) -> int:
    """5년 DPS CAGR(%). 복리 가속의 직접 지표."""
    if cagr_pct is None:
        return 0
    if cagr_pct >= 15:
        return 10
    if cagr_pct >= 10:
        return 7
    if cagr_pct >= 5:
        return 4
    if cagr_pct >= 0:
        return 1
    return 0


def score_payout_stability(payout_ratio_pct: float | None) -> int:
    """배당성향(%). 20~70%가 sweet spot (너무 낮으면 의지 X, 너무 높으면 위태).

    음수(적자)나 100% 초과 모두 위험 — 0점.
    """
    if payout_ratio_pct is None:
        return 0
    if payout_ratio_pct < 0:
        return 0
    if 20 <= payout_ratio_pct <= 70:
        return 5
    if 10 <= payout_ratio_pct <= 80:
        return 3
    return 0


def score_dividend_frequency(payments_per_year: int | None) -> int:
    """1년 배당 빈도. 분기(4회)+가 DRIP 복리에 가장 유리하나 연 1회 결산도 DGI 우량 종목 가능.

    격차를 완만하게 — 분기 5, 반기 4, 연 1회 3, 무배당 0.
    DGI의 본질은 *성장*(DPS CAGR + 연속 인상)이라 빈도는 부차적.
    """
    if payments_per_year is None:
        return 0
    if payments_per_year >= 4:
        return 5
    if payments_per_year >= 2:
        return 4
    if payments_per_year >= 1:
        return 3
    return 0


# ============================================================
# 자본이득 (30점)
# ============================================================

def score_price_cagr(cagr_pct: float | None) -> int:
    """5년 주가 CAGR(%). 배당이 아니라 *capital* 가속."""
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
    """5년 거래일 중 종가가 200일선 위에 있던 비율 (0~1). 추세 견고성."""
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
    """5년 총수익(배당 포함)이 양수면 5점."""
    if total_return_pct is None:
        return 0
    return 5 if total_return_pct > 0 else 0


# ============================================================
# 펀더멘털 (20점)
# ============================================================

def score_roe_5y_avg(avg_roe_pct: float | None) -> int:
    """5년 ROE 평균(%). DGI의 안정성 핵심."""
    if avg_roe_pct is None:
        return 0
    if avg_roe_pct >= 15:
        return 10
    if avg_roe_pct >= 12:
        return 7
    if avg_roe_pct >= 8:
        return 4
    if avg_roe_pct >= 5:
        return 2
    return 0


def score_eps_stability(cv: float | None) -> int:
    """EPS 변동계수 (CV = std/mean). 낮을수록 안정. 음수/0 mean은 0점.

    cv 입력은 *비율* (0.2 = 20% 변동). NaN/None → 0.
    """
    if cv is None or cv < 0:
        return 0
    if cv < 0.20:
        return 10
    if cv < 0.30:
        return 7
    if cv < 0.50:
        return 4
    return 0


# ============================================================
# 주주환원 (10점)
# ============================================================

def score_has_cancellation(has: bool | None) -> int:
    """최근 3년 내 자사주 소각 공시가 1건이라도 있으면 5점."""
    if has is None:
        return 0
    return 5 if has else 0


def score_cancellation_frequency(per_year: float | None) -> int:
    """연평균 소각 공시 건수. ValueTrader heuristic과 동일 임계값."""
    if per_year is None:
        return 0
    if per_year >= 0.7:
        return 5
    if per_year >= 0.3:
        return 3
    return 0


# ============================================================
# Totals & grading
# ============================================================

DIVIDEND_MAX = 40
CAPITAL_MAX = 30
FUNDAMENTAL_MAX = 20
SHAREHOLDER_RETURN_MAX = 10
TOTAL_MAX = DIVIDEND_MAX + CAPITAL_MAX + FUNDAMENTAL_MAX + SHAREHOLDER_RETURN_MAX

GRADE_A_THRESHOLD = 80  # 우량 DGI
GRADE_B_THRESHOLD = 70  # 매수 고려


def grade(total: int) -> str:
    if total >= GRADE_A_THRESHOLD:
        return "A"
    if total >= GRADE_B_THRESHOLD:
        return "B"
    return "C"
