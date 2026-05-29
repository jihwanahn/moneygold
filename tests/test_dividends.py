"""배당 이력 모듈 테스트.

KIS HTTP는 monkeypatch로 가짜 응답. 정규화, dedup, year-window slicing 검증.
"""
from __future__ import annotations

import pandas as pd
import pytest

from moneygold.data import dividends as div

# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------

def test_year_windows_single_year():
    from datetime import datetime
    wins = div._year_windows(datetime(2025, 1, 1), datetime(2025, 6, 30))
    assert wins == [("20250101", "20250630")]


def test_year_windows_multi_year_no_gaps():
    """다년 윈도우: 갭 없고 전체 범위 커버. 윤년 때문에 windows 수는 years 또는 years+1."""
    from datetime import datetime
    wins = div._year_windows(datetime(2023, 1, 1), datetime(2025, 12, 31))
    assert wins[0][0] == "20230101"
    assert wins[-1][1] == "20251231"
    assert len(wins) in (3, 4)  # 365-day 슬라이딩 + 윤년 → ±1
    # 인접 윈도우 사이에 갭 없어야 함
    for (_, t1), (f2, _) in zip(wins, wins[1:], strict=False):
        assert pd.Timestamp(t1) + pd.Timedelta(days=1) == pd.Timestamp(f2)


def test_safe_float_zero_padded():
    assert div._safe_float("000000000600") == 600.0
    assert div._safe_float(" 12.00") == 12.0
    assert div._safe_float("") != div._safe_float("")  # NaN
    assert div._safe_float(None) != div._safe_float(None)


# ----------------------------------------------------------------------
# normalize_dividend_rows
# ----------------------------------------------------------------------

def test_normalize_keeps_valid_rejects_malformed():
    rows = [
        {  # valid
            "record_date": "20240326", "sht_cd": "000720", "isin_name": "현대건설",
            "divi_kind": "결산", "per_sto_divi_amt": "000000000600",
            "divi_rate": " 12.00", "stk_divi_rate": "  0.00",
            "divi_pay_dt": "20240425", "stk_kind": "보통",
        },
        {  # malformed date — skip
            "record_date": "abcd", "per_sto_divi_amt": "100", "divi_kind": "결산",
        },
        {  # also valid (분기)
            "record_date": "20240701", "sht_cd": "000720",
            "divi_kind": "분기", "per_sto_divi_amt": "000000000200",
            "divi_rate": "  4.00", "stk_divi_rate": "  0.00",
            "divi_pay_dt": "", "stk_kind": "보통",
        },
    ]
    df = div.normalize_dividend_rows(rows, ticker="000720", asof="20260527")
    assert len(df) == 2
    assert set(df["divi_kind"]) == {"결산", "분기"}
    valid = df[df["divi_kind"] == "결산"].iloc[0]
    assert valid["per_sto_divi_amt"] == 600.0
    assert valid["divi_rate_pct"] == 12.0
    assert valid["ticker"] == "000720"
    assert valid["fetched_at"] == "20260527"


def test_normalize_dedup_within_batch():
    """같은 (ticker, record_date, divi_kind, stk_kind)는 batch 내에서도 중복 제거."""
    rows = [
        {"record_date": "20240326", "divi_kind": "결산", "per_sto_divi_amt": "500",
         "divi_rate": "10", "stk_divi_rate": "0", "divi_pay_dt": "", "stk_kind": "보통"},
        {"record_date": "20240326", "divi_kind": "결산", "per_sto_divi_amt": "600",  # 후 입력이 keep='last'로 우선
         "divi_rate": "12", "stk_divi_rate": "0", "divi_pay_dt": "", "stk_kind": "보통"},
    ]
    df = div.normalize_dividend_rows(rows, ticker="005930", asof="20260527")
    assert len(df) == 1
    assert df.iloc[0]["per_sto_divi_amt"] == 600.0


def test_normalize_empty_returns_empty_with_schema():
    df = div.normalize_dividend_rows([], "005930", "20260527")
    assert df.empty
    assert list(df.columns) == div.DIV_COLUMNS


def test_normalize_sets_fiscal_year_to_na_for_kis_source():
    """KIS 출처는 fiscal_year=NA (record_date로 추론되는 게 정상)."""
    rows = [{
        "record_date": "20240326", "divi_kind": "결산",
        "per_sto_divi_amt": "600", "divi_rate": "12.00",
        "stk_divi_rate": "0", "divi_pay_dt": "", "stk_kind": "보통",
    }]
    df = div.normalize_dividend_rows(rows, "005930", "20260527")
    assert "fiscal_year" in df.columns
    assert df["fiscal_year"].iloc[0] is pd.NA or pd.isna(df["fiscal_year"].iloc[0])


# ----------------------------------------------------------------------
# pykrx fetch
# ----------------------------------------------------------------------

def test_fetch_dividends_pykrx_attributes_to_prior_year(monkeypatch):
    """pykrx 12월말 DPS → (year - 1) 회계연도로 귀속."""

    def fake_fund(start, end, ticker):
        year = int(start[:4])
        # 가짜 응답: 12월 마지막 거래일에 그 해 -1 회계연도 배당이 반영된 DPS
        return pd.DataFrame(
            {"DPS": [year * 100], "DIV": [3.5], "EPS": [year * 1000], "BPS": [year * 5000]},
            index=pd.DatetimeIndex([f"{year}-12-30"]),
        )

    monkeypatch.setattr("pykrx.stock.get_market_fundamental_by_date", fake_fund)
    out = div.fetch_dividends_pykrx("005930", asof="20260527", years=2)
    # 2024-12-30, 2025-12-30, 2026-12-30 = 3개? years=2 → asof_year=2026, start_year=2024, end_year=2026 → 3
    assert len(out) == 3
    # fiscal_year = year - 1
    fy = sorted(out["fiscal_year"].dropna().tolist())
    assert fy == [2023, 2024, 2025]
    # DPS 값
    # year=2025의 fundamental → fiscal_year=2024 → DPS = 2025*100 = 202500
    row_2024 = out[out["fiscal_year"] == 2024].iloc[0]
    assert row_2024["per_sto_divi_amt"] == 202500
    assert row_2024["divi_kind"] == "결산"
    assert row_2024["stk_kind"] == "보통"


def test_fetch_dividends_pykrx_skips_empty_years(monkeypatch):
    def fake_fund(start, end, ticker):
        year = int(start[:4])
        if year < 2023:
            return pd.DataFrame()  # 데이터 없음
        return pd.DataFrame(
            {"DPS": [1000], "DIV": [2.0], "EPS": [5000], "BPS": [50000]},
            index=pd.DatetimeIndex([f"{year}-12-30"]),
        )

    monkeypatch.setattr("pykrx.stock.get_market_fundamental_by_date", fake_fund)
    out = div.fetch_dividends_pykrx("005930", asof="20260527", years=5)
    assert len(out) == 4  # 2023, 2024, 2025, 2026


def test_sync_dividends_pykrx_writes_parquet(tmp_path, monkeypatch):
    """source='pykrx'로 sync → parquet 저장."""

    def fake_fund(start, end, ticker):
        year = int(start[:4])
        return pd.DataFrame(
            {"DPS": [1000.0 * (year - 2020)], "DIV": [3.0], "EPS": [5000.0], "BPS": [50000.0]},
            index=pd.DatetimeIndex([f"{year}-12-30"]),
        )

    monkeypatch.setattr("pykrx.stock.get_market_fundamental_by_date", fake_fund)
    stats = div.sync_dividends(tmp_path, tickers=["005930"], asof="20260527",
                                years=3, source="pykrx")
    assert stats["updated"] == 1
    assert stats["failed"] == []
    loaded = div.load_dividends(tmp_path, "005930")
    assert "fiscal_year" in loaded.columns
    assert len(loaded) == 4  # 2023~2026


def test_sync_dividends_rejects_kis_source_without_client(tmp_path):
    with pytest.raises(ValueError, match="source='kis'"):
        div.sync_dividends(tmp_path, ["005930"], asof="20260527", source="kis")


def test_sync_dividends_rejects_unknown_source(tmp_path):
    with pytest.raises(ValueError, match="unknown source"):
        div.sync_dividends(tmp_path, ["005930"], asof="20260527", source="bloomberg")


# ----------------------------------------------------------------------
# pykrx_batch (전종목 일별)
# ----------------------------------------------------------------------

def test_resolve_yearend_date_backs_off_holidays(monkeypatch):
    """1/1, 12/31이 휴장일 시 backoff. 비어 있으면 그 다음 영업일 시도."""
    from datetime import datetime
    calls = []

    def fake(date, market):
        calls.append(date)
        # 20241231~20241229는 빈 응답, 20241228만 정상
        if date == "20241228":
            return pd.DataFrame({"DPS": [100]}, index=pd.Index(["005930"], name="티커"))
        return pd.DataFrame()

    monkeypatch.setattr("pykrx.stock.get_market_fundamental", fake)
    result = div._resolve_yearend_date(2024, datetime(2026, 5, 28))
    assert result == "20241228"
    # 20241231, 30, 29, 28 순으로 시도
    assert calls[:4] == ["20241231", "20241230", "20241229", "20241228"]


def test_resolve_yearend_returns_none_for_future_year(monkeypatch):
    """asof보다 미래 연도는 호출 안 함."""
    from datetime import datetime
    monkeypatch.setattr("pykrx.stock.get_market_fundamental",
                        lambda *a, **kw: pytest.fail("should not be called"))
    assert div._resolve_yearend_date(2027, datetime(2026, 5, 28)) is None


def test_resolve_yearend_clips_to_asof_for_current_year(monkeypatch):
    """진행 중인 연도는 asof로 clip (12/31 호출 회피)."""
    from datetime import datetime
    calls = []

    def fake(date, market):
        calls.append(date)
        return pd.DataFrame({"DPS": [100]}, index=pd.Index(["005930"], name="티커"))

    monkeypatch.setattr("pykrx.stock.get_market_fundamental", fake)
    div._resolve_yearend_date(2026, datetime(2026, 5, 28))
    assert calls[0] == "20260528"


def test_fetch_dividends_pykrx_batch_aggregates_across_years(monkeypatch):
    """24 호출 시뮬레이션 — 종목별 fiscal_year=year-1 귀속."""
    from datetime import datetime

    def fake_resolve(year, asof_dt, max_backoff=7):
        if year > asof_dt.year:
            return None
        return f"{year}1230"

    def fake_fund(date, market):
        year = int(date[:4])
        # 시장별 다른 종목
        if market == "KOSPI":
            return pd.DataFrame(
                {"DPS": [year * 100], "DIV": [3.0], "EPS": [year * 1000], "BPS": [year * 5000]},
                index=pd.Index(["005930"], name="티커"),
            )
        return pd.DataFrame(
            {"DPS": [year * 50], "DIV": [2.5], "EPS": [year * 500], "BPS": [year * 2000]},
            index=pd.Index(["091990"], name="티커"),
        )

    monkeypatch.setattr("moneygold.data.dividends._resolve_yearend_date", fake_resolve)
    monkeypatch.setattr("pykrx.stock.get_market_fundamental", fake_fund)

    out = div.fetch_dividends_pykrx_batch(asof="20260528", years=2, log_progress=False)
    assert set(out.keys()) == {"005930", "091990"}
    # years=2, asof=2026 → start=2024, end=2026 → 3 years
    s_df = out["005930"]
    assert len(s_df) == 3
    fy = sorted(s_df["fiscal_year"].dropna().tolist())
    assert fy == [2023, 2024, 2025]
    # 2025 fundamental → fiscal_year=2024 → DPS=2025*100=202500
    row_2024 = s_df[s_df["fiscal_year"] == 2024].iloc[0]
    assert row_2024["per_sto_divi_amt"] == 202500


def test_sync_dividends_batch_writes_parquet(tmp_path, monkeypatch):
    """source='pykrx_batch'로 sync → parquet 저장 + 요청 종목만 처리."""
    from datetime import datetime

    monkeypatch.setattr("moneygold.data.dividends._resolve_yearend_date",
                        lambda y, a, **kw: f"{y}1230" if y <= a.year else None)
    monkeypatch.setattr(
        "pykrx.stock.get_market_fundamental",
        lambda date, market: pd.DataFrame(
            {"DPS": [500.0], "DIV": [2.0], "EPS": [5000.0], "BPS": [50000.0]},
            index=pd.Index(["005930"] if market == "KOSPI" else ["091990"], name="티커"),
        ),
    )
    # batch는 005930 + 091990 둘 다 fetch하지만 005930만 요청
    stats = div.sync_dividends(tmp_path, tickers=["005930"], asof="20260528",
                                years=1, source="pykrx_batch")
    assert stats["updated"] == 1
    loaded = div.load_dividends(tmp_path, "005930")
    assert not loaded.empty
    # 요청 안 한 091990은 저장 안 됨
    assert not div.dividends_path(tmp_path, "091990").exists()


def test_sync_dividends_batch_handles_pykrx_failure(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("KRX timeout")
    monkeypatch.setattr("moneygold.data.dividends.fetch_dividends_pykrx_batch", boom)
    stats = div.sync_dividends(tmp_path, ["005930"], asof="20260528",
                                years=1, source="pykrx_batch")
    assert stats["updated"] == 0
    assert len(stats["failed"]) == 1
    assert stats["failed"][0][0] == "__batch__"


# ----------------------------------------------------------------------
# fetch + sync with mocked KIS
# ----------------------------------------------------------------------

class _MockKIS:
    """KISClient stand-in: only the method we exercise."""

    def __init__(self, responses_by_year: dict[str, list[dict]]):
        self.responses_by_year = responses_by_year
        self.calls: list[tuple[str, str, str]] = []

    def fetch_dividend_history(self, ticker, from_date, to_date, gb="0"):
        self.calls.append((ticker, from_date, to_date))
        year = from_date[:4]
        return self.responses_by_year.get(year, [])


def test_fetch_dividends_for_ticker_slices_by_year():
    mock_responses = {
        "2024": [{"record_date": "20240326", "divi_kind": "결산",
                  "per_sto_divi_amt": "600", "divi_rate": "12.00",
                  "stk_divi_rate": "0", "divi_pay_dt": "20240425", "stk_kind": "보통"}],
        "2025": [{"record_date": "20250326", "divi_kind": "결산",
                  "per_sto_divi_amt": "700", "divi_rate": "13.00",
                  "stk_divi_rate": "0", "divi_pay_dt": "20250425", "stk_kind": "보통"}],
    }
    mock = _MockKIS(mock_responses)
    df = div.fetch_dividends_for_ticker(mock, "000720", asof="20260101", years=2)
    assert len(df) == 2
    # 슬라이딩이 1년 단위로 잘렸는지
    assert len(mock.calls) >= 2
    # 가장 빠른 윈도우는 약 2년 전
    earliest_from = min(c[1] for c in mock.calls)
    assert earliest_from[:4] in ("2023", "2024")  # ~2년 전


def test_sync_dividends_writes_parquet_and_dedups(tmp_path):
    mock = _MockKIS({
        "2025": [
            {"record_date": "20251101", "divi_kind": "결산",
             "per_sto_divi_amt": "800", "divi_rate": "8.0",
             "stk_divi_rate": "0", "divi_pay_dt": "", "stk_kind": "보통"},
        ],
    })
    stats = div.sync_dividends(tmp_path, tickers=["005930"], asof="20260101", years=1,
                                source="kis", kis=mock)
    assert stats["updated"] == 1
    assert stats["no_data"] == 0
    assert stats["failed"] == []

    path = div.dividends_path(tmp_path, "005930")
    assert path.exists()
    df = div.load_dividends(tmp_path, "005930")
    assert len(df) == 1
    assert df.iloc[0]["record_date"] == "20251101"

    # 같은 데이터 재 sync → 행 수 그대로 (dedup)
    stats2 = div.sync_dividends(tmp_path, tickers=["005930"], asof="20260101", years=1,
                                 source="kis", kis=mock)
    df2 = div.load_dividends(tmp_path, "005930")
    assert len(df2) == 1
    # updated는 신규 행이 들어갔는지 기준 — 이번엔 0 (전부 중복)
    assert stats2["updated"] == 0


def test_sync_dividends_continues_after_individual_failure(tmp_path):
    """한 종목이 예외를 던져도 다른 종목은 정상 처리되어야."""

    class _PartialMock(_MockKIS):
        def fetch_dividend_history(self, ticker, from_date, to_date, gb="0"):
            if ticker == "BADTKR":
                raise RuntimeError("simulated KIS error")
            return super().fetch_dividend_history(ticker, from_date, to_date, gb)

    mock = _PartialMock({
        "2025": [{"record_date": "20251101", "divi_kind": "결산",
                  "per_sto_divi_amt": "100", "divi_rate": "1",
                  "stk_divi_rate": "0", "divi_pay_dt": "", "stk_kind": "보통"}],
    })
    stats = div.sync_dividends(tmp_path, tickers=["BADTKR", "005930"],
                               asof="20260101", years=1, source="kis", kis=mock)
    assert stats["updated"] == 1
    assert len(stats["failed"]) == 1
    assert stats["failed"][0][0] == "BADTKR"
    assert div.dividends_path(tmp_path, "005930").exists()
    assert not div.dividends_path(tmp_path, "BADTKR").exists()


def test_load_dividends_missing_returns_empty_with_schema(tmp_path):
    df = div.load_dividends(tmp_path, "999999")
    assert df.empty
    assert list(df.columns) == div.DIV_COLUMNS
