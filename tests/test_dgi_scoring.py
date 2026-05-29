"""DGI scoring 모듈 테스트.

점수 함수 (scoring_rules) + feature 산출 함수 (scoring.compute_*) 위주.
score_ticker는 통합 테스트로 가짜 데이터 직접 parquet에 써서 검증.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from moneygold.data import dividends as div_mod
from moneygold.data import store
from moneygold.fundamentals import financials_path
from moneygold.strategies.value_long_term import scoring
from moneygold.strategies.value_long_term import scoring_rules as R

# ============================================================
# Scoring rules (점수 함수)
# ============================================================

@pytest.mark.parametrize("yld, expected", [
    (None, 0), (0, 0), (-1, 0), (0.5, 0),
    (1.0, 2), (2.9, 2),
    (3.0, 5), (4.9, 5),
    (5.0, 7), (6.9, 7),
    (7.0, 10), (15.0, 10),
])
def test_score_dividend_yield(yld, expected):
    assert R.score_dividend_yield(yld) == expected


@pytest.mark.parametrize("years, expected", [
    (None, 0), (0, 0), (2, 0),
    (3, 4), (4, 4),
    (5, 7), (9, 7),
    (10, 10), (20, 10),
])
def test_score_consecutive_increase(years, expected):
    assert R.score_consecutive_increase(years) == expected


@pytest.mark.parametrize("cagr, expected", [
    (None, 0),
    (-5, 0),
    (0, 1), (4.9, 1),
    (5, 4), (9.9, 4),
    (10, 7), (14.9, 7),
    (15, 10), (100, 10),
])
def test_score_dps_cagr(cagr, expected):
    assert R.score_dps_cagr(cagr) == expected


@pytest.mark.parametrize("payout, expected", [
    (None, 0),
    (-10, 0),
    (5, 0),                # 너무 낮음
    (15, 3),               # 10~80 외곽
    (30, 5), (50, 5), (70, 5),   # sweet spot
    (75, 3), (80, 3),
    (90, 0), (110, 0),
])
def test_score_payout_stability(payout, expected):
    assert R.score_payout_stability(payout) == expected


@pytest.mark.parametrize("freq, expected", [
    (None, 0), (0, 0),
    (1, 3),                  # 연 1회 결산
    (2, 4), (3, 4),          # 반기
    (4, 5), (12, 5),         # 분기·월
])
def test_score_dividend_frequency(freq, expected):
    assert R.score_dividend_frequency(freq) == expected


def test_score_freq_round_half_up_regression():
    """0.5/yr (2년 윈도우에 1건) → 1로 반올림 → 3점.

    회귀 방지: Python round()는 banker's rounding이라 round(0.5)=0이 되어
    연 1회 배당 종목이 빈도 점수 0이 되는 버그가 있었음 (2026-05 발견).
    scoring.py의 ``int(per_year + 0.5)`` 패턴이 살아 있는지 검증.
    """
    half = 0.5
    rounded = int(half + 0.5)
    assert rounded == 1
    assert R.score_dividend_frequency(rounded) == 3


@pytest.mark.parametrize("cv, expected", [
    (None, 0), (-0.1, 0),
    (0.0, 10), (0.19, 10),
    (0.20, 7), (0.29, 7),
    (0.30, 4), (0.49, 4),
    (0.50, 0), (1.0, 0),
])
def test_score_eps_stability(cv, expected):
    assert R.score_eps_stability(cv) == expected


def test_grade_thresholds():
    assert R.grade(85) == "A"
    assert R.grade(80) == "A"
    assert R.grade(79) == "B"
    assert R.grade(70) == "B"
    assert R.grade(69) == "C"
    assert R.grade(0) == "C"


# ============================================================
# Feature computation
# ============================================================

def _div_row(ticker, record_date, divi_kind, amt, stk_kind="보통"):
    """배당 한 줄 helper."""
    return {
        "ticker": ticker, "record_date": record_date, "divi_kind": divi_kind,
        "per_sto_divi_amt": float(amt), "divi_rate_pct": 0.0,
        "stk_divi_rate_pct": 0.0, "divi_pay_dt": "",
        "stk_kind": stk_kind, "fetched_at": "20260527",
    }


def test_annual_dps_attributes_jan_to_apr_settlement_to_prior_year():
    """결산배당이 다음해 1~4월 record_date면 전년도 회계연도로 귀속."""
    df = pd.DataFrame([
        _div_row("005930", "20250326", "결산", 1000),  # → 2024 회계연도
        _div_row("005930", "20240331", "결산", 800),   # → 2023 회계연도
        _div_row("005930", "20240701", "분기", 200),   # → 2024 (분기는 그대로)
    ])
    annual = scoring.annual_dps_per_year(df)
    assert annual == {2024: 1200.0, 2023: 800.0}


def test_annual_dps_pykrx_rows_override_kis_legacy():
    """fiscal_year 채워진 pykrx 행이 있으면 KIS legacy 행은 무시 (이중 합산 방지)."""
    df = pd.DataFrame([
        # KIS legacy (fiscal_year=NaN)
        {**_div_row("005930", "20250326", "결산", 1000), "fiscal_year": pd.NA},
        # pykrx (fiscal_year=2024 — 같은 회계연도)
        {**_div_row("005930", "20251230", "결산", 1444), "fiscal_year": 2024},
    ])
    df["fiscal_year"] = df["fiscal_year"].astype("Int64")
    annual = scoring.annual_dps_per_year(df)
    # pykrx 행만 사용 → 1444원만
    assert annual == {2024: 1444.0}


def test_annual_dps_filters_preferred_stock():
    df = pd.DataFrame([
        _div_row("005930", "20250326", "결산", 1000, stk_kind="보통"),
        _div_row("005930", "20250326", "결산", 1500, stk_kind="우선"),  # 제외
    ])
    annual = scoring.annual_dps_per_year(df)
    assert annual == {2024: 1000.0}


def test_consecutive_increase_streak():
    annual = {2019: 100, 2020: 110, 2021: 120, 2022: 130, 2023: 140, 2024: 150}
    assert scoring.consecutive_increase_years(annual, asof="20260527") == 5


def test_consecutive_increase_breaks_on_freeze():
    annual = {2020: 100, 2021: 110, 2022: 110, 2023: 120, 2024: 130}
    # 인상 체인: 2024>2023 (✓), 2023>2022 (✓), 2022>2021 (✗ 동결) → 2
    assert scoring.consecutive_increase_years(annual, asof="20260527") == 2


def test_consecutive_increase_breaks_on_year_gap():
    annual = {2018: 100, 2020: 110, 2021: 120, 2022: 130}
    assert scoring.consecutive_increase_years(annual, asof="20260527") == 2


def test_dps_cagr_5y_exact():
    """100 → 200 in 5 years → CAGR ≈ 14.87%"""
    annual = {2019: 100, 2024: 200}
    cagr = scoring.dps_cagr_5y_pct(annual, asof="20260527")
    assert cagr is not None
    assert 14.0 < cagr < 15.5


def test_dps_cagr_uses_fallback_when_5yrs_missing():
    """5년치 없으면 가장 옛날 해로 fallback."""
    annual = {2022: 100, 2024: 144}  # 2년 동안 44% 성장 → CAGR ≈ 20%
    cagr = scoring.dps_cagr_5y_pct(annual, asof="20260527")
    assert cagr is not None
    assert 19.0 < cagr < 21.0


def test_dps_cagr_returns_none_for_insufficient_data():
    assert scoring.dps_cagr_5y_pct({}, "20260527") is None
    assert scoring.dps_cagr_5y_pct({2024: 100}, "20260527") is None


def test_dividend_yield_uses_last_completed_year():
    annual = {2023: 1000, 2024: 1500}
    assert scoring.dividend_yield_pct(annual, current_price=50000, asof="20260527") == 3.0


def test_dividend_yield_skips_current_year():
    annual = {2024: 1500, 2026: 2000}  # 2026은 진행 중 → 무시
    assert scoring.dividend_yield_pct(annual, current_price=50000, asof="20260527") == 3.0


# ----- 가격 -----

def _bars(n: int = 1500, start_price: float = 100.0, daily_pct: float = 0.0005,
          start_date: str = "20210101") -> pd.DataFrame:
    """단순 우상향 시계열. n일."""
    dates = pd.date_range(start_date, periods=n, freq="B").strftime("%Y%m%d").tolist()
    closes = start_price * (1.0 + daily_pct) ** np.arange(n)
    return pd.DataFrame({
        "ticker": "TEST", "date": dates,
        "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": 1000, "value": 100000,
    })


def test_price_cagr_upward_series():
    bars = _bars(n=1300, start_price=100.0, daily_pct=0.0005, start_date="20210101")
    # ~5년 동안 daily +0.05% → ~14% annual
    cagr = scoring.price_cagr_5y_pct(bars, asof="20260101")
    assert cagr is not None
    assert 12.0 < cagr < 16.0


def test_price_cagr_negative_when_downward():
    bars = _bars(n=1300, start_price=200.0, daily_pct=-0.0005, start_date="20210101")
    cagr = scoring.price_cagr_5y_pct(bars, asof="20260101")
    assert cagr is not None
    assert cagr < 0


def test_above_sma200_ratio_strong_uptrend():
    bars = _bars(n=1500, start_price=100.0, daily_pct=0.001, start_date="20210101")
    ratio = scoring.above_sma200_ratio_5y(bars, asof="20260101")
    assert ratio is not None
    assert ratio >= 0.95  # 강한 우상향에선 거의 항상 200선 위


def test_above_sma200_returns_none_for_short_series():
    bars = _bars(n=50)
    assert scoring.above_sma200_ratio_5y(bars, asof="20260527") is None


# ----- 펀더멘털 -----

def test_roe_5y_avg_uses_latest_quarter_per_year():
    """연도별 마지막 분기의 ROE를 5년 평균."""
    df = pd.DataFrame([
        {"year": 2020, "q": 4, "roe": 10.0, "eps": 100},
        {"year": 2021, "q": 4, "roe": 12.0, "eps": 120},
        {"year": 2022, "q": 2, "roe": 14.0, "eps": 70},   # H1만 보고
        {"year": 2022, "q": 4, "roe": 13.5, "eps": 140},  # 더 늦은 분기
        {"year": 2023, "q": 4, "roe": 15.0, "eps": 150},
        {"year": 2024, "q": 4, "roe": 14.0, "eps": 160},
    ])
    avg = scoring.roe_5y_avg_pct(df)
    assert avg is not None
    assert abs(avg - (10.0 + 12.0 + 13.5 + 15.0 + 14.0) / 5) < 0.01


def test_roe_returns_none_when_column_missing():
    df = pd.DataFrame({"year": [2024], "q": [4], "eps": [100]})
    assert scoring.roe_5y_avg_pct(df) is None


def test_roe_dart_indicators_takes_precedence_over_financials():
    """DART indicators의 ROE가 있으면 financials parquet의 roe는 무시."""
    fins = pd.DataFrame([
        {"year": 2020, "q": 4, "roe": 5.0},
        {"year": 2021, "q": 4, "roe": 5.0},
        {"year": 2022, "q": 4, "roe": 5.0},
    ])
    dart = pd.DataFrame([
        {"ticker": "086790", "fiscal_year": 2022, "idx_nm": "ROE", "idx_val": 15.0},
        {"ticker": "086790", "fiscal_year": 2023, "idx_nm": "ROE", "idx_val": 16.0},
        {"ticker": "086790", "fiscal_year": 2024, "idx_nm": "ROE", "idx_val": 17.0},
    ])
    avg = scoring.roe_5y_avg_pct(fins, dart_indicators_df=dart)
    assert avg == 16.0  # DART 평균


def test_roe_falls_back_to_financials_when_dart_empty():
    fins = pd.DataFrame([
        {"year": 2020, "q": 4, "roe": 12.0},
        {"year": 2021, "q": 4, "roe": 13.0},
        {"year": 2022, "q": 4, "roe": 14.0},
        {"year": 2023, "q": 4, "roe": 15.0},
        {"year": 2024, "q": 4, "roe": 16.0},
    ])
    avg = scoring.roe_5y_avg_pct(fins, dart_indicators_df=pd.DataFrame())
    assert avg == 14.0


def test_eps_cv_stable_low():
    """안정적 EPS → 낮은 CV."""
    df = pd.DataFrame({
        "year": [2020]*4 + [2021]*4 + [2022]*4 + [2023]*4 + [2024]*4,
        "q": [1, 2, 3, 4]*5,
        "eps": [25, 25, 25, 25]*5,  # 100/year, 완벽 안정
    })
    cv = scoring.eps_cv(df)
    assert cv == 0.0


def test_eps_cv_returns_none_for_negative_mean():
    df = pd.DataFrame({"year": [2020, 2021, 2022], "eps": [-100, -50, -30]})
    assert scoring.eps_cv(df) is None


# ============================================================
# Integration: score_ticker with parquet files on disk
# ============================================================

def test_score_ticker_end_to_end(tmp_path):
    """가짜 parquet 3종 (bars/dividends/financials) 작성 후 score_ticker 통과 확인."""
    ticker = "TEST01"

    # bars: 5+ years uptrend
    bars = _bars(n=1400, start_price=100.0, daily_pct=0.001, start_date="20200601")
    bars["ticker"] = ticker
    store.write_parquet_atomic(bars, store.bars_path(tmp_path, ticker))

    # dividends: 5 years of growing settlement dividends
    div_rows = [
        _div_row(ticker, "20210326", "결산", 500),
        _div_row(ticker, "20220326", "결산", 600),
        _div_row(ticker, "20230326", "결산", 720),
        _div_row(ticker, "20240326", "결산", 864),
        _div_row(ticker, "20250326", "결산", 1037),
    ]
    div_df = pd.DataFrame(div_rows)
    store.write_parquet_atomic(div_df, div_mod.dividends_path(tmp_path, ticker))

    # financials: 5 years with stable ROE
    fin = pd.DataFrame([
        {"year": y, "q": 4, "roe": 13.0, "eps": 5000.0}
        for y in (2020, 2021, 2022, 2023, 2024)
    ])
    store.write_parquet_atomic(fin, financials_path(tmp_path, ticker))

    sb = scoring.score_ticker(ticker, asof="20260101", data_dir=tmp_path, dart=None)

    # 합리적인 점수가 잡혀야 함
    assert sb.consecutive_increase_years == 4
    assert sb.dps_cagr_5y_pct is not None and sb.dps_cagr_5y_pct > 15
    assert sb.dividend_yield_pct is not None and sb.dividend_yield_pct > 0
    assert sb.price_cagr_5y_pct is not None and sb.price_cagr_5y_pct > 20
    assert sb.above_sma200_ratio_5y is not None and sb.above_sma200_ratio_5y > 0.9
    assert sb.roe_5y_avg_pct == 13.0
    # 등급 — A/B 후보
    assert sb.total >= 60
    assert sb.grade in ("A", "B")
    # 주주환원은 DART None이라 0
    assert sb.score_cancel_has == 0
    assert sb.score_cancel_freq == 0


def test_score_ticker_missing_data_graceful(tmp_path):
    """전혀 데이터 없는 종목 — 0점 + grade=C, 예외 없이 통과."""
    sb = scoring.score_ticker("NONEX", asof="20260527", data_dir=tmp_path, dart=None)
    assert sb.total == 0
    assert sb.grade == "C"
    assert "배당 이력 없음" in "; ".join(sb.notes)
