"""kis_overseas_master.py: KIS 해외주식 마스터 파싱."""
from __future__ import annotations

import io
import zipfile

import pandas as pd

from moneygold.data import kis_overseas_master as kis_om


def _make_fixture_zip(rows: list[list[str]], filename: str = "TEST.COD") -> bytes:
    """주어진 row들을 tab-joined cp949 cod 파일로 만들어 zip 바이트 반환."""
    text = "\n".join("\t".join(r) for r in rows) + "\n"
    raw = text.encode("cp949")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, raw)
    return buf.getvalue()


# --- _normalize_ticker --------------------------------------------------------

def test_normalize_ticker_space_to_hyphen():
    assert kis_om._normalize_ticker("BRK A") == "BRK-A"
    assert kis_om._normalize_ticker("BRK B") == "BRK-B"


def test_normalize_ticker_dot_to_hyphen():
    assert kis_om._normalize_ticker("BRK.B") == "BRK-B"


def test_normalize_ticker_strip():
    assert kis_om._normalize_ticker("  AAPL  ") == "AAPL"


# --- fetch_kis_overseas_listed ------------------------------------------------

def test_fetch_kis_overseas_listed_parses_columns(monkeypatch):
    """24-column tab-separated fixture가 정상 파싱."""
    rows = [
        # 24 columns: 5번째가 ticker, 7=한글, 8=영문
        ["US", "22", "NAS", "나스닥", "AAPL", "NASAAPL", "애플", "APPLE INC",
         "2", "USD", "4", "", "200.0", "1", "1", "930", "1600", "N", "", "000",
         "0", "0", "   ", "   "],
        ["US", "22", "NAS", "나스닥", "BRK A", "NASBRKA", "버크셔A", "BERKSHIRE HATHAWAY A",
         "2", "USD", "4", "", "500000.0", "1", "1", "930", "1600", "N", "", "000",
         "0", "0", "   ", "   "],
    ]
    fixture_bytes = _make_fixture_zip(rows, "NASMST.COD")

    class FakeResp:
        content = fixture_bytes
        def raise_for_status(self): pass

    monkeypatch.setattr(kis_om.requests, "get", lambda *a, **k: FakeResp())
    df = kis_om.fetch_kis_overseas_listed("NAS")
    assert list(df["ticker"]) == ["AAPL", "BRK-A"]  # 공백 → 하이픈
    assert df["name_en"].iloc[0] == "APPLE INC"
    assert df["name_kr"].iloc[0] == "애플"
    assert (df["kis_exchange"] == "NAS").all()


def test_fetch_kis_overseas_listed_invalid_exchange():
    import pytest
    with pytest.raises(ValueError, match="Unknown exchange"):
        kis_om.fetch_kis_overseas_listed("XXX")  # type: ignore[arg-type]


def test_fetch_kis_overseas_listed_dedup(monkeypatch):
    """같은 ticker 중복 시 drop_duplicates (first kept)."""
    rows = [
        ["US", "22", "NAS", "나스닥", "AAPL", "X", "애플", "APPLE INC"] + [""] * 16,
        ["US", "22", "NAS", "나스닥", "AAPL", "X", "애플", "APPLE INC"] + [""] * 16,
    ]
    fixture_bytes = _make_fixture_zip(rows)

    class FakeResp:
        content = fixture_bytes
        def raise_for_status(self): pass

    monkeypatch.setattr(kis_om.requests, "get", lambda *a, **k: FakeResp())
    df = kis_om.fetch_kis_overseas_listed("NAS")
    assert len(df) == 1


# --- annotate_tradable_kis ----------------------------------------------------

def test_annotate_tradable_kis_us_only_checked():
    master = pd.DataFrame([
        {"ticker": "AAPL", "market": "US", "name": "Apple"},
        {"ticker": "MSFT", "market": "US", "name": "Microsoft"},
        {"ticker": "NOTKIS", "market": "US", "name": "Not in KIS"},
        {"ticker": "005930", "market": "KOSPI", "name": "삼성전자"},
    ])
    kis = pd.DataFrame([
        {"ticker": "AAPL", "name_en": "APPLE INC", "name_kr": "애플", "kis_exchange": "NAS"},
        {"ticker": "MSFT", "name_en": "MICROSOFT CORP", "name_kr": "마이크로소프트", "kis_exchange": "NAS"},
    ])
    out = kis_om.annotate_tradable_kis(master, kis)
    by_tk = out.set_index("ticker")["tradable_kis"].to_dict()
    assert by_tk["AAPL"] is True
    assert by_tk["MSFT"] is True
    assert by_tk["NOTKIS"] is False, "KIS에 없는 US 종목은 tradable_kis=False"
    assert by_tk["005930"] is True, "KR은 기본 True (KIS 국내 API)"


def test_annotate_tradable_kis_preserves_other_columns():
    master = pd.DataFrame([
        {"ticker": "AAPL", "market": "US", "name": "Apple", "mcap": 3e12, "sector": "Tech"},
    ])
    kis = pd.DataFrame([
        {"ticker": "AAPL", "name_en": "APPLE", "name_kr": "애플", "kis_exchange": "NAS"},
    ])
    out = kis_om.annotate_tradable_kis(master, kis)
    assert "mcap" in out.columns
    assert "sector" in out.columns
    assert out["mcap"].iloc[0] == 3e12
