"""Regression tests for fundamentals.py — primarily the (year, q) YoY matcher.

Positional shift(4) was the previous approach; it broke when quarters were
missing (sporadic KIS reporting for KOSDAQ micros), matching the current row
to a non-prior-year row and producing absurd ratios (e.g., +18900%).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from moneygold.fundamentals import (
    FundamentalsResult,
    _attach_yoy_by_year_q,
    build_fundamentals_from_cache,
)


def _quarters(rows: list[tuple[int, int, float, float]]) -> pd.DataFrame:
    """rows = [(year, q, revenue, op_income), ...] — quarter label auto-derived."""
    return pd.DataFrame(
        {
            "quarter": [f"{y}Q{q}" for y, q, _, _ in rows],
            "year": [y for y, _, _, _ in rows],
            "q": [q for _, q, _, _ in rows],
            "revenue": [r for _, _, r, _ in rows],
            "op_income": [o for _, _, _, o in rows],
            "op_margin": [np.nan for _ in rows],
        }
    )


def test_yoy_matches_prior_year_same_quarter_when_contiguous():
    df = _quarters([
        (2024, 1, 100.0, 10.0),
        (2024, 2, 110.0, 11.0),
        (2024, 3, 120.0, 12.0),
        (2024, 4, 130.0, 13.0),
        (2025, 1, 150.0, 20.0),
        (2025, 4, 195.0, 26.0),
    ])
    _attach_yoy_by_year_q(df, [("revenue", "revenue_yoy"), ("op_income", "op_income_yoy")])

    last = df.iloc[-1]
    assert last["quarter"] == "2025Q4"
    assert last["revenue_yoy"] == pytest_approx(50.0)  # 195/130 - 1
    assert last["op_income_yoy"] == pytest_approx(100.0)  # 26/13 - 1

    q1 = df[df["quarter"] == "2025Q1"].iloc[0]
    assert q1["revenue_yoy"] == pytest_approx(50.0)  # 150/100 - 1


def test_yoy_handles_missing_quarters_without_misaligning():
    # Mimics 0009K0 pattern — KIS sometimes reports only Q3+Q4 for micro caps.
    # Positional shift(4) would have matched 2025Q4 (380) to 2024Q3 (2.0) → +18900%.
    # (year-1, same q) match should pair 2025Q4 with 2024Q4 (114) → +233%.
    df = _quarters([
        (2024, 3, 2.0, -46.0),
        (2024, 4, 114.0, 85.0),
        (2025, 3, 1.0, -59.0),
        (2025, 4, 380.0, 256.0),
    ])
    _attach_yoy_by_year_q(df, [("revenue", "revenue_yoy"), ("op_income", "op_income_yoy")])

    last = df.iloc[-1]
    assert last["quarter"] == "2025Q4"
    assert last["revenue_yoy"] == pytest_approx(233.333, rel=1e-3)
    # op income: (256 / 85 - 1) * 100 = 201.18
    assert last["op_income_yoy"] == pytest_approx(201.176, rel=1e-3)


def test_yoy_is_nan_when_prior_year_missing():
    df = _quarters([
        (2025, 1, 100.0, 10.0),  # no 2024Q1 row
        (2025, 2, 110.0, 11.0),  # no 2024Q2 row
    ])
    _attach_yoy_by_year_q(df, [("revenue", "revenue_yoy"), ("op_income", "op_income_yoy")])
    assert df["revenue_yoy"].isna().all()
    assert df["op_income_yoy"].isna().all()


def test_yoy_is_nan_when_prior_is_zero():
    df = _quarters([
        (2024, 4, 0.0, 0.0),
        (2025, 4, 100.0, 10.0),
    ])
    _attach_yoy_by_year_q(df, [("revenue", "revenue_yoy"), ("op_income", "op_income_yoy")])
    assert pd.isna(df.iloc[-1]["revenue_yoy"])
    assert pd.isna(df.iloc[-1]["op_income_yoy"])


def test_build_fundamentals_from_cache_self_heals_buggy_stored_yoy():
    # Older parquets shipped with positional shift(4) YoY already baked in.
    # Reading should overwrite with the correct (year, q) match.
    df = _quarters([
        (2024, 3, 2.0, -46.0),
        (2024, 4, 114.0, 85.0),
        (2025, 3, 1.0, -59.0),
        (2025, 4, 380.0, 256.0),
    ])
    df["revenue_yoy"] = [np.nan, np.nan, np.nan, 18900.0]  # buggy stored value
    df["op_income_yoy"] = [np.nan, np.nan, np.nan, -656.0]

    r = build_fundamentals_from_cache(df)
    assert isinstance(r, FundamentalsResult)
    assert r.latest_revenue_yoy == pytest_approx(233.333, rel=1e-3)
    assert r.latest_op_income_yoy == pytest_approx(201.176, rel=1e-3)


# tiny pytest.approx shim so we don't add a pytest dep statement here
def pytest_approx(val, rel=1e-6):
    import pytest
    return pytest.approx(val, rel=rel)
