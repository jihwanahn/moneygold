"""pykrx 일봉 fetcher 테스트.

pykrx HTTP 호출은 monkeypatch로 가짜 응답. 스키마 정규화 + 빈/잘못된 응답 처리 검증.
"""
from __future__ import annotations

import pandas as pd
import pytest

from moneygold.data import pykrx_bars


def _make_pykrx_response(dates, opens, highs, lows, closes, vols, vals):
    """pykrx get_market_ohlcv_by_date 형식의 DataFrame 생성."""
    return pd.DataFrame(
        {"시가": opens, "고가": highs, "저가": lows, "종가": closes,
         "거래량": vols, "거래대금": vals},
        index=pd.DatetimeIndex(dates, name="날짜"),
    )


# ----------------------------------------------------------------------
# fetch_bars_pykrx
# ----------------------------------------------------------------------

def test_fetch_bars_normalizes_to_kis_schema(monkeypatch):
    """pykrx 한글 컬럼 → KIS 호환 스키마 (ticker, date, open, ..., adj_factor)."""
    fake = _make_pykrx_response(
        ["2026-01-02", "2026-01-03"],
        opens=[71000, 72000], highs=[72000, 73000], lows=[70500, 71500],
        closes=[71500, 72500], vols=[1000, 1100], vals=[71_500_000, 79_750_000],
    )
    monkeypatch.setattr(
        "pykrx.stock.get_market_ohlcv_by_date",
        lambda start, end, ticker, adjusted=True: fake,
    )
    df = pykrx_bars.fetch_bars_pykrx("005930", "20260101", "20260131")
    assert list(df.columns) == [
        "ticker", "date", "open", "high", "low", "close", "volume", "value", "adj_factor",
    ]
    assert df["ticker"].iloc[0] == "005930"
    assert df["date"].tolist() == ["20260102", "20260103"]
    assert df["close"].iloc[1] == 72500
    assert df["adj_factor"].iloc[0] == 1.0


def test_fetch_bars_drops_zero_close_rows(monkeypatch):
    """휴장/거래정지 등 close=0 행은 제외."""
    fake = _make_pykrx_response(
        ["2026-01-02", "2026-01-03"],
        opens=[71000, 0], highs=[72000, 0], lows=[70500, 0],
        closes=[71500, 0], vols=[1000, 0], vals=[71_500_000, 0],
    )
    monkeypatch.setattr(
        "pykrx.stock.get_market_ohlcv_by_date",
        lambda start, end, ticker, adjusted=True: fake,
    )
    df = pykrx_bars.fetch_bars_pykrx("005930", "20260101", "20260131")
    assert len(df) == 1
    assert df["date"].iloc[0] == "20260102"


def test_fetch_bars_clips_to_window(monkeypatch):
    """응답이 요청 윈도우 밖 날짜를 포함하면 잘라냄."""
    fake = _make_pykrx_response(
        ["2025-12-30", "2026-01-02", "2026-02-01"],
        opens=[100, 200, 300], highs=[110, 210, 310], lows=[95, 195, 295],
        closes=[105, 205, 305], vols=[1, 2, 3], vals=[105, 410, 915],
    )
    monkeypatch.setattr(
        "pykrx.stock.get_market_ohlcv_by_date",
        lambda start, end, ticker, adjusted=True: fake,
    )
    df = pykrx_bars.fetch_bars_pykrx("005930", "20260101", "20260131")
    assert df["date"].tolist() == ["20260102"]


def test_fetch_bars_returns_empty_when_pykrx_empty(monkeypatch):
    monkeypatch.setattr(
        "pykrx.stock.get_market_ohlcv_by_date",
        lambda start, end, ticker, adjusted=True: pd.DataFrame(),
    )
    df = pykrx_bars.fetch_bars_pykrx("005930", "20260101", "20260131")
    assert df.empty
    assert "date" in df.columns


def test_fetch_bars_returns_empty_when_pykrx_raises(monkeypatch):
    """pykrx 일시 실패 → 빈 DataFrame (호출자에서 BackfillResult 처리)."""
    def fake(*a, **kw):
        raise RuntimeError("KRX timeout")
    monkeypatch.setattr("pykrx.stock.get_market_ohlcv_by_date", fake)
    df = pykrx_bars.fetch_bars_pykrx("005930", "20260101", "20260131")
    assert df.empty


def test_fetch_bars_rejects_inverted_window(monkeypatch):
    """start > end → 빈 DataFrame, pykrx 호출 안 함."""
    calls = []
    monkeypatch.setattr(
        "pykrx.stock.get_market_ohlcv_by_date",
        lambda *a, **kw: calls.append(1) or pd.DataFrame(),
    )
    df = pykrx_bars.fetch_bars_pykrx("005930", "20260201", "20260101")
    assert df.empty
    assert calls == []


# ----------------------------------------------------------------------
# fetch_index_bars_pykrx
# ----------------------------------------------------------------------

def test_fetch_index_bars_uses_correct_code(monkeypatch):
    captured = {}

    def fake(start, end, code):
        captured["args"] = (start, end, code)
        return _make_pykrx_response(
            ["2026-01-02"], [2500.0], [2520.0], [2490.0], [2510.0], [0], [0],
        )

    monkeypatch.setattr("pykrx.stock.get_index_ohlcv_by_date", fake)
    df = pykrx_bars.fetch_index_bars_pykrx("KOSPI", "20260101", "20260131")
    # KOSPI 코드 = '1001' (pykrx)
    assert captured["args"] == ("20260101", "20260131", "1001")
    assert len(df) == 1
    assert df["close"].iloc[0] == 2510


def test_fetch_index_bars_rejects_unknown_label():
    with pytest.raises(ValueError, match="unknown index label"):
        pykrx_bars.fetch_index_bars_pykrx("SPY", "20260101", "20260131")
