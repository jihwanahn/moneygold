"""universe.py: 필터 룰."""
from __future__ import annotations

import pandas as pd
import pytest

from moneygold import universe


def test_is_preferred_share():
    assert universe.is_preferred_share("005935") is True   # 삼성전자우
    assert universe.is_preferred_share("005930") is False  # 삼성전자
    assert universe.is_preferred_share("00100K") is False  # 6자리 아님은 false
    assert universe.is_preferred_share("12345") is False   # 길이 미달


def test_filter_master_drops_preferred(monkeypatch):
    monkeypatch.setattr(universe, "_fetch_etp_tickers", lambda: set())
    df = pd.DataFrame([
        {"ticker": "005930", "name": "삼성전자", "market": "KOSPI"},
        {"ticker": "005935", "name": "삼성전자우", "market": "KOSPI"},
        {"ticker": "066570", "name": "LG전자", "market": "KOSPI"},
    ])
    out = universe.filter_master(df)
    assert set(out["ticker"]) == {"005930", "066570"}


def test_filter_master_drops_spac(monkeypatch):
    monkeypatch.setattr(universe, "_fetch_etp_tickers", lambda: set())
    df = pd.DataFrame([
        {"ticker": "000001", "name": "한국조선해양", "market": "KOSPI"},
        {"ticker": "300001", "name": "어떤스팩4호", "market": "KOSDAQ"},
        {"ticker": "300002", "name": "엔에이치스팩제30호", "market": "KOSDAQ"},
    ])
    out = universe.filter_master(df)
    assert "300001" not in set(out["ticker"])
    assert "300002" not in set(out["ticker"])
    assert "000001" in set(out["ticker"])


def test_filter_master_drops_reit(monkeypatch):
    monkeypatch.setattr(universe, "_fetch_etp_tickers", lambda: set())
    df = pd.DataFrame([
        {"ticker": "330590", "name": "롯데리츠", "market": "KOSPI"},
        {"ticker": "005930", "name": "삼성전자", "market": "KOSPI"},
    ])
    out = universe.filter_master(df)
    assert "330590" not in set(out["ticker"])


def test_filter_master_drops_etf_etn(monkeypatch):
    monkeypatch.setattr(universe, "_fetch_etp_tickers", lambda: {"069500", "114800"})
    df = pd.DataFrame([
        {"ticker": "069500", "name": "KODEX 200", "market": "KOSPI"},
        {"ticker": "114800", "name": "KODEX 인버스", "market": "KOSPI"},
        {"ticker": "005930", "name": "삼성전자", "market": "KOSPI"},
    ])
    out = universe.filter_master(df)
    assert set(out["ticker"]) == {"005930"}


def test_filter_master_empty(monkeypatch):
    monkeypatch.setattr(universe, "_fetch_etp_tickers", lambda: set())
    df = pd.DataFrame(columns=["ticker", "name", "market"])
    out = universe.filter_master(df)
    assert out.empty
