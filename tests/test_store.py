"""store.py: atomic write + dedup."""
from __future__ import annotations

import pandas as pd
import pytest

from moneygold.data import store


def test_read_missing_returns_none(tmp_path):
    assert store.read_parquet_safe(tmp_path / "nope.parquet") is None


def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "x.parquet"
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    store.write_parquet_atomic(df, p)
    out = store.read_parquet_safe(p)
    pd.testing.assert_frame_equal(out, df)


def test_append_dedup_first_write(tmp_path):
    p = tmp_path / "bars.parquet"
    new = pd.DataFrame({"date": ["20260101", "20260102"], "close": [100, 101]})
    added, skipped = store.append_dedup(p, new, dedup_keys=["date"], sort_keys=["date"])
    assert (added, skipped) == (2, 0)
    out = store.read_parquet_safe(p)
    assert list(out["date"]) == ["20260101", "20260102"]


def test_append_dedup_with_overlap(tmp_path):
    p = tmp_path / "bars.parquet"
    initial = pd.DataFrame({"date": ["20260101", "20260102"], "close": [100, 101]})
    store.write_parquet_atomic(initial, p)

    incoming = pd.DataFrame({"date": ["20260102", "20260103"], "close": [999, 102]})
    added, skipped = store.append_dedup(p, incoming, dedup_keys=["date"], sort_keys=["date"])
    assert added == 1   # 20260103만 신규
    assert skipped == 1  # 20260102는 중복

    out = store.read_parquet_safe(p)
    assert list(out["date"]) == ["20260101", "20260102", "20260103"]
    # keep="first" → 기존 값(101) 유지, 999는 버려짐
    assert out.loc[out["date"] == "20260102", "close"].iloc[0] == 101


def test_append_dedup_internal_dupes_in_new(tmp_path):
    p = tmp_path / "bars.parquet"
    incoming = pd.DataFrame({"date": ["20260101", "20260101"], "close": [100, 200]})
    added, skipped = store.append_dedup(p, incoming, dedup_keys=["date"], sort_keys=["date"])
    assert (added, skipped) == (1, 1)


def test_append_empty_input_noop(tmp_path):
    p = tmp_path / "bars.parquet"
    empty = pd.DataFrame({"date": [], "close": []})
    added, skipped = store.append_dedup(p, empty, dedup_keys=["date"], sort_keys=["date"])
    assert (added, skipped) == (0, 0)
    assert not p.exists()
