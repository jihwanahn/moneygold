"""US 배당 fetcher 테스트 (us_dividends). yfinance는 monkeypatch로 모킹."""
from __future__ import annotations

import pandas as pd
import pytest

from moneygold.data import us_dividends as ud
from moneygold.data.dividends import DIV_COLUMNS
from moneygold.strategies.value_long_term import scoring


def _yf_div_series(year_amounts: dict[int, list[float]]) -> pd.Series:
    """{연도: [지급액들]} → yfinance dividends 형식 (tz-aware DatetimeIndex Series)."""
    idx, vals = [], []
    for year, amounts in year_amounts.items():
        for i, amt in enumerate(amounts):
            month = 3 * i + 3  # 분기: 3,6,9,12월
            idx.append(pd.Timestamp(f"{year}-{month:02d}-15", tz="America/New_York"))
            vals.append(amt)
    return pd.Series(vals, index=pd.DatetimeIndex(idx))


# ----------------------------------------------------------------------
# normalize_yf_dividends
# ----------------------------------------------------------------------

def test_normalize_sums_quarterly_into_annual():
    """분기배당 4건 → 연 1행으로 합산, fiscal_year = 지급연도."""
    series = _yf_div_series({
        2023: [0.46, 0.46, 0.46, 0.46],   # 연 1.84
        2024: [0.48, 0.48, 0.48, 0.48],   # 연 1.92
    })
    df = ud.normalize_yf_dividends(series, "KO", "20260528")
    assert list(df.columns) == DIV_COLUMNS
    assert len(df) == 2
    r23 = df[df["fiscal_year"] == 2023].iloc[0]
    assert abs(r23["per_sto_divi_amt"] - 1.84) < 1e-6
    assert r23["divi_kind"] == "결산"
    assert r23["stk_kind"] == "보통"
    assert r23["record_date"] == "20231231"


def test_normalize_empty_series():
    df = ud.normalize_yf_dividends(pd.Series([], dtype=float), "X", "20260528")
    assert df.empty
    assert list(df.columns) == DIV_COLUMNS


def test_normalize_none():
    df = ud.normalize_yf_dividends(None, "X", "20260528")
    assert df.empty


def test_normalize_skips_nonpositive():
    series = _yf_div_series({2023: [0.5, 0.0, -0.1, 0.5]})
    df = ud.normalize_yf_dividends(series, "X", "20260528")
    # 0.5 + 0.5 = 1.0 (0과 음수 제외)
    assert abs(df.iloc[0]["per_sto_divi_amt"] - 1.0) < 1e-6


def test_normalize_rate_based_absorbs_timing_artifact():
    """KO 2001 회귀: 한 해 5건(0.18 이중지급 포함)이 합산을 부풀려 연속인상 끊김 → 중앙값×빈도로 복원.

    실제 KO 2000~2003 데이터: 2000=4×0.085, 2001=[0.09,0.09,0.18,0.09,0.09], 2002=4×0.10, 2003=4×0.11.
    합산이면 2001(0.54) > 2002(0.40)로 역전되나, 중앙값×4면 0.34<0.36<0.40<0.44 단조.
    """
    idx, vals = [], []
    data = {
        2000: [0.085, 0.085, 0.085, 0.085],
        2001: [0.09, 0.09, 0.18, 0.09, 0.09],   # 5건 (이중지급)
        2002: [0.10, 0.10, 0.10, 0.10],
        2003: [0.11, 0.11, 0.11, 0.11],
    }
    for year, amts in data.items():
        for i, a in enumerate(amts):
            idx.append(pd.Timestamp(f"{year}-{(i % 4) * 3 + 2:02d}-15", tz="America/New_York"))
            vals.append(a)
    series = pd.Series(vals, index=pd.DatetimeIndex(idx))
    df = ud.normalize_yf_dividends(series, "KO", "20260528")
    annual = scoring.annual_dps_per_year(df)
    # 중앙값×4 → 단조 증가
    assert annual[2000] == pytest.approx(0.34)
    assert annual[2001] == pytest.approx(0.36)
    assert annual[2002] == pytest.approx(0.40)
    assert annual[2003] == pytest.approx(0.44)
    # 연속 인상 끊기지 않음
    assert scoring.consecutive_increase_years(annual, asof="20260528") == 3  # 2000→2003


def test_normalize_frequency_change_annual_to_quarterly():
    """회귀 (MCD형): 연배당(n=1)→분기배당(n=4) 전환 시 인공 삭감/급증 없이 연속 증가 보존.

    전역 최빈빈도(4)를 옛 연배당 연도에 곱하던 버그 → MCD 연속인상 17년 과소집계.
    연도별 윈도우 빈도 + (건수=빈도면 sum) 로 전환 매끄럽게.
    """
    idx, vals = [], []
    # 2005~2007 연 1회, 2008~2011 분기 4회, 매년 rate 증가
    annual_payers = {2005: [0.67], 2006: [1.00], 2007: [1.50]}
    quarterly = {
        2008: [0.375, 0.375, 0.375, 0.50],   # 연중 인상, sum=1.625 > 1.50
        2009: [0.50, 0.50, 0.50, 0.55],       # sum=2.05
        2010: [0.55, 0.55, 0.55, 0.61],       # sum=2.26
        2011: [0.61, 0.61, 0.61, 0.70],       # sum=2.53
    }
    for data in (annual_payers, quarterly):
        for year, amts in data.items():
            for i, a in enumerate(amts):
                mo = (12 if len(amts) == 1 else (i % 4) * 3 + 3)
                idx.append(pd.Timestamp(f"{year}-{mo:02d}-15", tz="America/New_York"))
                vals.append(a)
    series = pd.Series(vals, index=pd.DatetimeIndex(idx))
    df = ud.normalize_yf_dividends(series, "MCD", "20260528")
    annual = scoring.annual_dps_per_year(df)
    # 전환 연도(2007 연배당 1.50 → 2008 분기 sum 1.625)에서 끊기지 않음
    assert annual[2007] == pytest.approx(1.50)
    assert annual[2008] == pytest.approx(1.625)
    assert annual[2007] < annual[2008]
    # 2005~2011 매년 증가 → 연속인상 6년 (2006~2011, 2005는 시작점)
    assert scoring.consecutive_increase_years(annual, asof="20260528") == 6


def test_normalize_feeds_scoring_functions():
    """정규화 결과가 scoring 순수 함수에 그대로 들어가 연속인상/CAGR 산출."""
    series = _yf_div_series({
        2019: [1.00], 2020: [1.10], 2021: [1.21], 2022: [1.33], 2023: [1.46], 2024: [1.61],
    })
    df = ud.normalize_yf_dividends(series, "GROW", "20260528")
    annual = scoring.annual_dps_per_year(df)
    assert annual[2024] == pytest.approx(1.61)
    # 2019→2024 매년 인상 → 연속인상 5년
    assert scoring.consecutive_increase_years(annual, asof="20260528") == 5
    # DPS CAGR ~10%
    cagr = scoring.dps_cagr_5y_pct(annual, asof="20260528")
    assert cagr is not None and 9.0 < cagr < 11.0


# ----------------------------------------------------------------------
# fetch + sync (yfinance mocked)
# ----------------------------------------------------------------------

class _FakeTicker:
    def __init__(self, dividends, info):
        self._dividends = dividends
        self._info = info

    @property
    def dividends(self):
        return self._dividends

    @property
    def info(self):
        return self._info


def _install_fake_yf(monkeypatch, ticker_map):
    import types
    fake_yf = types.SimpleNamespace(Ticker=lambda t: ticker_map[t])
    import sys
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)


def test_fetch_info_payout_roe_to_percent(monkeypatch):
    """payout/ROE는 소수(0.65) → % (65.0) 변환. yield는 그대로 (이미 %)."""
    series = _yf_div_series({2024: [0.48, 0.48, 0.48, 0.48]})
    ticker = _FakeTicker(series, {
        "dividendYield": 2.64, "payoutRatio": 0.65,
        "returnOnEquity": 0.43, "fiveYearAvgDividendYield": 2.89,
        "sharesOutstanding": 4_300_000_000,
    })
    _install_fake_yf(monkeypatch, {"KO": ticker})
    div_df, info = ud.fetch_us_dividends_and_info("KO", "20260528")
    assert not div_df.empty
    assert info["dividend_yield_pct"] == pytest.approx(2.64)
    assert info["payout_ratio_pct"] == pytest.approx(65.0)
    assert info["roe_pct"] == pytest.approx(43.0)


def test_fetch_yield_not_inflated_for_sub_one_percent(monkeypatch):
    """회귀: 0.83(=0.83% WMT류)을 83%로 부풀리지 않음 — 그대로 0.83."""
    ticker = _FakeTicker(_yf_div_series({2024: [1.0]}), {"dividendYield": 0.83})
    _install_fake_yf(monkeypatch, {"WMT": ticker})
    _, info = ud.fetch_us_dividends_and_info("WMT", "20260528")
    assert info["dividend_yield_pct"] == pytest.approx(0.83)


def test_fetch_yield_sanity_clips_garbage(monkeypatch):
    """yfinance가 83.0 같은 garbage yield를 주면 저장 전 None으로 clip (>25%)."""
    ticker = _FakeTicker(_yf_div_series({2024: [1.0]}), {"dividendYield": 83.0})
    _install_fake_yf(monkeypatch, {"WMT": ticker})
    _, info = ud.fetch_us_dividends_and_info("WMT", "20260528")
    assert info["dividend_yield_pct"] is None


def test_sync_writes_parquet_and_json(tmp_path, monkeypatch):
    series = _yf_div_series({2022: [1.7], 2023: [1.8], 2024: [1.9]})
    ticker = _FakeTicker(series, {"dividendYield": 2.5, "payoutRatio": 0.6,
                                   "returnOnEquity": 0.25})
    _install_fake_yf(monkeypatch, {"KO": ticker})
    stats = ud.sync_us_dividends(tmp_path, ["KO"], asof="20260528")
    assert stats["updated"] == 1
    assert stats["failed"] == []
    # parquet
    df = ud.load_us_dividends(tmp_path, "KO")
    assert len(df) == 3
    # json
    info = ud.load_us_info(tmp_path, "KO")
    assert info["roe_pct"] == pytest.approx(25.0)
    assert info["fetched_at"] == "20260528"


def test_sync_continues_after_failure(tmp_path, monkeypatch):
    good = _FakeTicker(_yf_div_series({2024: [1.0]}), {"dividendYield": 2.0})

    class _BoomTicker:
        @property
        def dividends(self):
            raise RuntimeError("yf network error")

        @property
        def info(self):
            raise RuntimeError("yf network error")

    import sys
    import types
    tmap = {"GOOD": good, "BAD": _BoomTicker()}
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=lambda t: tmap[t]))
    # BAD는 fetch 내부 try/except로 빈 결과 → no_data, GOOD는 updated
    stats = ud.sync_us_dividends(tmp_path, ["BAD", "GOOD"], asof="20260528")
    assert stats["updated"] == 1
    assert ud.load_us_dividends(tmp_path, "GOOD").shape[0] == 1
    assert ud.load_us_dividends(tmp_path, "BAD").empty


def test_load_missing_returns_empty(tmp_path):
    assert ud.load_us_dividends(tmp_path, "NONE").empty
    assert ud.load_us_info(tmp_path, "NONE") is None
