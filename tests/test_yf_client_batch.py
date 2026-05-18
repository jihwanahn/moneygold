"""yf_client.fetch_daily_bars_batch — yfinance 배치 다운로드 테스트.

실제 yfinance 호출 없이 monkeypatch로 yf.download mock.
"""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest


def _make_yf_module(download_fn):
    """가짜 yfinance 모듈 — yf.download 만 정의."""
    m = types.SimpleNamespace(download=download_fn)
    return m


@pytest.fixture(autouse=True)
def reset_yf(monkeypatch):
    """각 테스트마다 sys.modules에서 yfinance 제거."""
    monkeypatch.delitem(sys.modules, "yfinance", raising=False)
    yield
    monkeypatch.delitem(sys.modules, "yfinance", raising=False)


def _multiindex_df(tickers: list[str], dates: list[str], closes: dict[str, list[float]]) -> pd.DataFrame:
    """yf.download(group_by='ticker') 가 만드는 MultiIndex DataFrame 모방."""
    cols = []
    data = {}
    idx = pd.to_datetime(dates)
    for tk in tickers:
        for field in ["Open", "High", "Low", "Close", "Volume"]:
            cols.append((tk, field))
            if field == "Volume":
                data[(tk, field)] = [1000] * len(dates)
            else:
                data[(tk, field)] = closes[tk]
    df = pd.DataFrame(data, index=idx)
    df.columns = pd.MultiIndex.from_tuples(cols)
    return df


def test_batch_basic_returns_per_ticker_dict(monkeypatch):
    from moneygold.data import yf_client as yfc
    dates = ["2026-05-14", "2026-05-15"]
    closes = {"AAPL": [200.0, 205.0], "MSFT": [400.0, 410.0]}

    def fake_download(tickers, **kwargs):
        return _multiindex_df(tickers, dates, closes)

    monkeypatch.setitem(sys.modules, "yfinance", _make_yf_module(fake_download))
    out = yfc.fetch_daily_bars_batch(["AAPL", "MSFT"], period="2y", batch_size=10)
    assert set(out.keys()) == {"AAPL", "MSFT"}
    aapl = out["AAPL"]
    assert len(aapl) == 2
    assert list(aapl["date"]) == ["20260514", "20260515"]
    assert aapl["close"].iloc[-1] == 205.0
    assert aapl["volume"].iloc[-1] == 1000


def test_batch_empty_input_returns_empty_dict(monkeypatch):
    from moneygold.data import yf_client as yfc
    out = yfc.fetch_daily_bars_batch([], period="2y")
    assert out == {}


def test_batch_handles_yf_download_exception(monkeypatch):
    """yf.download이 예외 던지면 batch 전체 빈 DataFrame."""
    from moneygold.data import yf_client as yfc

    def fake_download(tickers, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setitem(sys.modules, "yfinance", _make_yf_module(fake_download))
    out = yfc.fetch_daily_bars_batch(["AAPL", "MSFT"], period="2y")
    assert set(out.keys()) == {"AAPL", "MSFT"}
    assert out["AAPL"].empty
    assert out["MSFT"].empty


def test_batch_missing_ticker_in_response(monkeypatch):
    """yf.download이 일부 ticker를 결과에서 누락하면 빈 DataFrame."""
    from moneygold.data import yf_client as yfc
    dates = ["2026-05-15"]
    closes = {"AAPL": [200.0]}

    def fake_download(tickers, **kwargs):
        # AAPL만 반환, MSFT 누락
        return _multiindex_df(["AAPL"], dates, closes)

    monkeypatch.setitem(sys.modules, "yfinance", _make_yf_module(fake_download))
    out = yfc.fetch_daily_bars_batch(["AAPL", "MSFT"], period="2y")
    assert not out["AAPL"].empty
    assert out["MSFT"].empty


def test_batch_single_ticker_flat_columns(monkeypatch):
    """yf.download이 단일 ticker일 때는 flat columns — wrap to MultiIndex 내부 처리."""
    from moneygold.data import yf_client as yfc
    dates = ["2026-05-15"]

    def fake_download(tickers, **kwargs):
        # Flat columns (단일 ticker 시 yfinance 동작)
        df = pd.DataFrame({
            "Open": [200.0], "High": [201.0], "Low": [199.0],
            "Close": [200.5], "Volume": [1500],
        }, index=pd.to_datetime(dates))
        return df

    monkeypatch.setitem(sys.modules, "yfinance", _make_yf_module(fake_download))
    out = yfc.fetch_daily_bars_batch(["AAPL"], period="2y")
    assert "AAPL" in out
    assert out["AAPL"]["close"].iloc[-1] == 200.5


def test_batch_chunks_respect_batch_size(monkeypatch):
    """batch_size보다 ticker 많으면 여러 batch로 분할."""
    from moneygold.data import yf_client as yfc
    dates = ["2026-05-15"]

    call_log = []

    def fake_download(tickers, **kwargs):
        call_log.append(list(tickers))
        closes = {tk: [100.0] for tk in tickers}
        return _multiindex_df(tickers, dates, closes)

    monkeypatch.setitem(sys.modules, "yfinance", _make_yf_module(fake_download))
    tickers = [f"T{i}" for i in range(125)]
    out = yfc.fetch_daily_bars_batch(tickers, period="2y", batch_size=50)
    # 50 + 50 + 25 = 3 batches
    assert len(call_log) == 3
    assert len(call_log[0]) == 50
    assert len(call_log[1]) == 50
    assert len(call_log[2]) == 25
    assert len(out) == 125
