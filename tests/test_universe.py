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


def test_filter_master_does_not_drop_meritz(monkeypatch):
    """회귀 방지: '메리츠'에 '리츠'가 포함되지만 REIT가 아님 (금융지주).
    negative lookbehind로 메리츠 계열은 통과해야.
    """
    monkeypatch.setattr(universe, "_fetch_etp_tickers", lambda: set())
    df = pd.DataFrame([
        {"ticker": "138040", "name": "메리츠금융지주", "market": "KOSPI"},
        {"ticker": "330590", "name": "롯데리츠", "market": "KOSPI"},           # 실제 REIT
        {"ticker": "293940", "name": "신한알파리츠", "market": "KOSPI"},        # 실제 REIT
        {"ticker": "140910", "name": "이리츠코크렙", "market": "KOSPI"},        # 실제 REIT (이리츠도 통과해야)
    ])
    out = universe.filter_master(df)
    survived = set(out["ticker"])
    assert "138040" in survived, "메리츠금융지주는 살아남아야"
    assert "330590" not in survived
    assert "293940" not in survived
    assert "140910" not in survived


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
