"""universe_us.py: NASDAQ Trader 파서 + 필터."""
from __future__ import annotations

import pandas as pd

from moneygold import universe_us

# --- _parse_nasdaq_trader_text ------------------------------------------------

NASDAQ_FIXTURE = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
    "Round Lot Size|ETF|NextShares\n"
    "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
    "MSFT|Microsoft Corporation - Common Stock|Q|N|N|100|N|N\n"
    "QQQ|Invesco QQQ Trust - ETF|G|N|N|100|Y|N\n"
    "TESTTKR|Test Issue|Q|Y|N|100|N|N\n"
    "ZBADQ|Delinquent Issuer Common Stock|Q|N|D|100|N|N\n"
    "File Creation Time: 0511202515:30|||||\n"
)

OTHER_FIXTURE = (
    "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
    "Test Issue|NASDAQ Symbol\n"
    "A|Agilent Technologies, Inc. Common Stock|N|A|N|100|N|A\n"
    "BRK.B|Berkshire Hathaway Inc. Class B|N|BRK.B|N|100|N|BRK.B\n"
    "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY\n"
    "ABC$A|Some Preferred Stock|N|ABC.A|N|100|N|ABC.A\n"
    "XYZ.WS|Some Warrant Issue|N|XYZ.WS|N|100|N|XYZ.WS\n"
    "TESTAMS|Test|A|TST|N|100|Y|TST\n"
    "File Creation Time: 0511202515:30|||||||\n"
)


def test_parse_nasdaq_listed_drops_footer_and_etfs():
    df = universe_us._parse_nasdaq_trader_text(NASDAQ_FIXTURE, kind="nasdaq")
    # 푸터는 빠짐
    assert "File Creation Time" not in df["ticker"].iloc[-1]
    # 필드 매핑 확인
    assert set(df.columns) >= {"ticker", "name", "exchange", "test_issue", "etf_flag", "financial_status"}
    assert (df["exchange"] == "NASDAQ").all()


def test_parse_otherlisted_maps_exchange_code():
    df = universe_us._parse_nasdaq_trader_text(OTHER_FIXTURE, kind="other")
    # N → NYSE, P → NYSE_ARCA, A → NYSE_AMERICAN
    by_tk = df.set_index("ticker")["exchange"].to_dict()
    assert by_tk["A"] == "NYSE"
    assert by_tk["BRK.B"] == "NYSE"
    assert by_tk["SPY"] == "NYSE_ARCA"


def test_fetch_nasdaq_trader_filters_test_etf_preferred(monkeypatch):
    """end-to-end 필터: Test/ETF/Preferred/Warrant/Delinquent 제외, BRK.B → BRK-B."""

    def fake_get(url, *args, **kwargs):
        class R:
            def __init__(self, text: str) -> None:
                self.text = text

            def raise_for_status(self) -> None:
                pass

        if "nasdaqlisted" in url:
            return R(NASDAQ_FIXTURE)
        if "otherlisted" in url:
            return R(OTHER_FIXTURE)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(universe_us.requests, "get", fake_get)
    df = universe_us.fetch_nasdaq_trader_listed()
    tickers = set(df["ticker"])

    # 보통주만 잔존
    assert "AAPL" in tickers
    assert "MSFT" in tickers
    assert "A" in tickers
    # 클래스 표기 정규화
    assert "BRK-B" in tickers
    assert "BRK.B" not in tickers

    # 필터링된 종목들
    assert "QQQ" not in tickers, "ETF flag로 제외"
    assert "SPY" not in tickers, "ETF flag로 제외"
    assert "TESTTKR" not in tickers, "Test Issue 제외"
    assert "TESTAMS" not in tickers, "Test Issue 제외"
    assert "ZBADQ" not in tickers, "Financial Status=D 제외"
    assert not any("$" in t for t in tickers), "Preferred ($) 제외"
    assert not any(t.endswith("-WS") or "WS" in t.split("-")[-1:] for t in tickers if "WS" in t), \
        "Warrant 제외"


def test_fetch_master_us_nasdaq_trader_source(monkeypatch):
    """source='nasdaq_trader'로 호출 시 enrich=False면 sector/industry=UNKNOWN, mcap=0."""
    monkeypatch.setattr(
        universe_us, "fetch_nasdaq_trader_listed",
        lambda: pd.DataFrame({
            "ticker": ["AAPL", "MSFT"],
            "name": ["Apple Inc.", "Microsoft Corp."],
            "exchange": ["NASDAQ", "NASDAQ"],
        }),
    )
    df = universe_us.fetch_master_us(source="nasdaq_trader", enrich=False)
    assert list(df["ticker"]) == ["AAPL", "MSFT"]
    assert (df["market"] == "US").all()
    assert (df["sector"] == "UNKNOWN").all()
    assert (df["mcap"] == 0).all()
    assert set(df.columns) >= {"ticker", "name", "market", "sector", "mcap", "industry"}


def test_fetch_master_us_invalid_source_raises():
    import pytest
    with pytest.raises(ValueError, match="Unknown source"):
        universe_us.fetch_master_us(source="bogus")  # type: ignore[arg-type]


def test_enrich_with_mcap_applies_threshold(monkeypatch):
    """mcap_min_usd 컷오프로 미달 종목 제외."""
    class FakeTicker:
        _data = {
            "AAPL": {"marketCap": 3_000_000_000_000, "sector": "Tech", "industry": "Phones"},
            "TINY": {"marketCap": 50_000_000, "sector": "Tech", "industry": "Misc"},
        }

        def __init__(self, tk: str) -> None:
            self.tk = tk

        @property
        def info(self) -> dict:
            return self._data.get(self.tk, {})

    import sys
    import types
    fake_yf = types.SimpleNamespace(Ticker=FakeTicker)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    df = pd.DataFrame({
        "ticker": ["AAPL", "TINY"],
        "name": ["Apple", "Tiny Co"],
        "sector": ["UNKNOWN", "UNKNOWN"],
        "industry": ["UNKNOWN", "UNKNOWN"],
    })
    out = universe_us.enrich_with_mcap(
        df, mcap_min_usd=300_000_000, sleep_s=0, progress=False,
    )
    assert list(out["ticker"]) == ["AAPL"]
    assert out["mcap"].iloc[0] == 3_000_000_000_000
    assert out["sector"].iloc[0] == "Tech"
