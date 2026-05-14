"""kis_client.py: 응답 정규화 + 페이지네이션 + rate limiter."""
from __future__ import annotations

import time

import pandas as pd
import pytest

from moneygold.data import kis_client


# ---------- _normalize_bars ----------

def _sample_row(date: str, close: int = 1000) -> dict:
    return {
        "stck_bsop_date": date,
        "stck_oprc": str(close - 10),
        "stck_hgpr": str(close + 5),
        "stck_lwpr": str(close - 20),
        "stck_clpr": str(close),
        "acml_vol": "1234",
        "acml_tr_pbmn": str(close * 1234),
        "garbage": "ignored",
    }


def test_normalize_empty_returns_empty_df():
    df = kis_client._normalize_bars("005930", [], "20260101", "20260131")
    assert df.empty
    assert list(df.columns) == ["ticker", "date", "open", "high", "low", "close", "volume", "value", "adj_factor"]


def test_normalize_basic_fields():
    rows = [_sample_row("20260103", 1000), _sample_row("20260102", 999), _sample_row("20260101", 998)]
    df = kis_client._normalize_bars("005930", rows, "20260101", "20260131")
    assert len(df) == 3
    # ascending by date
    assert list(df["date"]) == ["20260101", "20260102", "20260103"]
    assert df["close"].tolist() == [998, 999, 1000]
    assert df["open"].tolist() == [988, 989, 990]
    assert df["high"].tolist() == [1003, 1004, 1005]
    assert df["low"].tolist() == [978, 979, 980]
    assert (df["ticker"] == "005930").all()
    assert (df["adj_factor"] == 1.0).all()


def test_normalize_dedup_same_date():
    rows = [_sample_row("20260103", 1000), _sample_row("20260103", 9999)]
    df = kis_client._normalize_bars("005930", rows, "20260101", "20260131")
    # 첫 등장 보존
    assert len(df) == 1
    assert df["close"].iloc[0] == 1000


def test_normalize_clip_to_range():
    rows = [_sample_row("20260301", 100), _sample_row("20260201", 99), _sample_row("20260101", 98)]
    df = kis_client._normalize_bars("005930", rows, "20260201", "20260228")
    assert list(df["date"]) == ["20260201"]


def test_normalize_drops_bad_rows():
    rows = [
        _sample_row("20260101", 100),
        {**_sample_row("20260102"), "stck_oprc": "not-a-number"},
    ]
    df = kis_client._normalize_bars("005930", rows, "20260101", "20260131")
    # 손상 행은 to_numeric에서 NaN -> dropna로 제거
    assert list(df["date"]) == ["20260101"]


# ---------- _prev_day ----------

def test_prev_day_calendar_minus_one():
    assert kis_client._prev_day("20260102") == "20260101"
    assert kis_client._prev_day("20260301") == "20260228"


# ---------- _RateLimiter ----------

def test_rate_limiter_allows_below_threshold():
    rl = kis_client._RateLimiter(max_per_sec=5, window_sec=1.0)
    t0 = time.monotonic()
    for _ in range(5):
        rl.acquire()
    assert (time.monotonic() - t0) < 0.05   # 5건은 거의 즉시


def test_rate_limiter_throttles_above_threshold():
    rl = kis_client._RateLimiter(max_per_sec=3, window_sec=0.3)
    t0 = time.monotonic()
    for _ in range(6):
        rl.acquire()
    elapsed = time.monotonic() - t0
    # 6건 / 3 req per 0.3s window → 최소 ~0.3초 대기 필요
    assert elapsed >= 0.25
