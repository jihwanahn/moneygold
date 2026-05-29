"""DART 재무지표 모듈 테스트."""
from __future__ import annotations

import pandas as pd
import pytest

from moneygold.config import DartConfig
from moneygold.data import dart_indicators as di
from moneygold.strategies.value_long_term.dart_client import DartClient


# ----------------------------------------------------------------------
# _safe_float
# ----------------------------------------------------------------------

def test_safe_float_dash_and_empty():
    assert pd.isna(di._safe_float("-"))
    assert pd.isna(di._safe_float(""))
    assert pd.isna(di._safe_float(None))
    assert pd.isna(di._safe_float("N/A"))


def test_safe_float_normal_numbers():
    assert di._safe_float("8.999") == 8.999
    assert di._safe_float("1,234.5") == 1234.5
    assert di._safe_float(0) == 0.0


# ----------------------------------------------------------------------
# extract_annual_roe
# ----------------------------------------------------------------------

def test_extract_annual_roe_returns_dict():
    df = pd.DataFrame([
        {"ticker": "086790", "fiscal_year": 2022, "idx_nm": "ROE", "idx_val": 8.5},
        {"ticker": "086790", "fiscal_year": 2023, "idx_nm": "ROE", "idx_val": 9.0},
        {"ticker": "086790", "fiscal_year": 2024, "idx_nm": "ROE", "idx_val": 8.999},
        {"ticker": "086790", "fiscal_year": 2024, "idx_nm": "ROA", "idx_val": 0.7},  # 다른 지표
    ])
    out = di.extract_annual_roe(df)
    assert out == {2022: 8.5, 2023: 9.0, 2024: 8.999}


def test_extract_annual_roe_empty():
    assert di.extract_annual_roe(pd.DataFrame()) == {}
    assert di.extract_annual_roe(None) == {}


# ----------------------------------------------------------------------
# fetch + sync
# ----------------------------------------------------------------------

class _FakeDart:
    """DartClient stand-in."""

    def __init__(self, response_map):
        self.response_map = response_map
        self.calls = []

    def fetch_financial_indicators(self, ticker, bsns_year, idx_cl_code, reprt_code):
        self.calls.append((ticker, bsns_year, idx_cl_code))
        return self.response_map.get((ticker, bsns_year, idx_cl_code), [])

    # 시그니처 호환 (idx_cl_code 키워드 인자 default)
    IDX_PROFITABILITY = DartClient.IDX_PROFITABILITY
    REPORT_ANNUAL = DartClient.REPORT_ANNUAL


def test_fetch_indicators_iterates_years_and_classes():
    fake = _FakeDart({
        ("086790", 2023, DartClient.IDX_PROFITABILITY): [
            {"idx_cl_code": "M210000", "idx_cl_nm": "수익성지표",
             "idx_nm": "ROE", "idx_val": "9.5"},
        ],
        ("086790", 2024, DartClient.IDX_PROFITABILITY): [
            {"idx_cl_code": "M210000", "idx_cl_nm": "수익성지표",
             "idx_nm": "ROE", "idx_val": "8.999"},
        ],
    })
    out = di.fetch_indicators_for_ticker(fake, "086790", asof="20260528", years=2)
    # 직전 2년 사업보고서 = end_year=2025, start=2024. 근데 2024,2025 호출
    # 결과: 2024 한 행 (ROE 8.999), 2025는 빈 응답.
    assert len(out) == 1
    assert out.iloc[0]["fiscal_year"] == 2024
    assert out.iloc[0]["idx_val"] == 8.999


def test_fetch_indicators_empty_when_no_corp(monkeypatch):
    # DartClient API에 corp_code가 없으면 빈 리스트 반환하는 fake
    class _NoCorpDart(_FakeDart):
        def fetch_financial_indicators(self, ticker, bsns_year, idx_cl_code, reprt_code):
            return []
    fake = _NoCorpDart({})
    out = di.fetch_indicators_for_ticker(fake, "999999", asof="20260528", years=3)
    assert out.empty
    assert list(out.columns) == di.INDICATOR_COLUMNS


def test_sync_dart_indicators_writes_parquet(tmp_path):
    fake = _FakeDart({
        ("086790", 2024, DartClient.IDX_PROFITABILITY): [
            {"idx_cl_code": "M210000", "idx_cl_nm": "수익성지표",
             "idx_nm": "ROE", "idx_val": "8.999"},
            {"idx_cl_code": "M210000", "idx_cl_nm": "수익성지표",
             "idx_nm": "총자산영업이익률", "idx_val": "0.79"},
        ],
    })
    stats = di.sync_dart_indicators(fake, tmp_path, ["086790"], asof="20250528", years=1)
    assert stats["updated"] == 1
    assert stats["failed"] == []
    df = di.load_indicators(tmp_path, "086790")
    assert len(df) == 2
    assert "ROE" in df["idx_nm"].values

    # 재 sync → dedup
    stats2 = di.sync_dart_indicators(fake, tmp_path, ["086790"], asof="20250528", years=1)
    df2 = di.load_indicators(tmp_path, "086790")
    assert len(df2) == 2  # 중복 추가 안 됨


def test_sync_continues_after_individual_failure(tmp_path):
    class _PartialDart(_FakeDart):
        def fetch_financial_indicators(self, ticker, bsns_year, idx_cl_code, reprt_code):
            if ticker == "BADTKR":
                raise RuntimeError("simulated DART error")
            return super().fetch_financial_indicators(ticker, bsns_year, idx_cl_code, reprt_code)

    fake = _PartialDart({
        ("086790", 2024, DartClient.IDX_PROFITABILITY): [
            {"idx_nm": "ROE", "idx_val": "9.0"},
        ],
    })
    stats = di.sync_dart_indicators(fake, tmp_path, ["BADTKR", "086790"],
                                     asof="20250528", years=1)
    assert stats["updated"] == 1
    assert len(stats["failed"]) == 1
    assert stats["failed"][0][0] == "BADTKR"


# ----------------------------------------------------------------------
# DartClient.fetch_financial_indicators (네트워크 mock)
# ----------------------------------------------------------------------

class _FakeResp:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


@pytest.fixture
def dart_client_with_corp(tmp_path, monkeypatch):
    """corp_code 매핑이 이미 들어 있는 DartClient (corpCode.xml fetch 회피)."""
    cfg = DartConfig(api_key="dummy", rate_per_sec=1000)
    dc = DartClient(cfg, data_dir=tmp_path)
    dc._corp_map = {"086790": {"corp_code": "00547583", "corp_name": "하나금융지주"}}
    return dc


def test_fetch_financial_indicators_uses_correct_params(dart_client_with_corp, monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = dict(params)
        return _FakeResp({
            "status": "000",
            "list": [{"idx_nm": "ROE", "idx_val": "9.5"}],
        })

    monkeypatch.setattr(
        "moneygold.strategies.value_long_term.dart_client.requests.get", fake_get,
    )
    out = dart_client_with_corp.fetch_financial_indicators("086790", bsns_year=2024)
    assert out == [{"idx_nm": "ROE", "idx_val": "9.5"}]
    assert captured["params"]["corp_code"] == "00547583"
    assert captured["params"]["bsns_year"] == "2024"
    assert captured["params"]["reprt_code"] == "11011"
    assert captured["params"]["idx_cl_code"] == "M210000"


def test_fetch_financial_indicators_returns_empty_when_no_data(dart_client_with_corp, monkeypatch):
    monkeypatch.setattr(
        "moneygold.strategies.value_long_term.dart_client.requests.get",
        lambda url, params=None, timeout=None: _FakeResp({"status": "013", "message": "조회된 데이타가 없습니다."}),
    )
    assert dart_client_with_corp.fetch_financial_indicators("086790", bsns_year=2024) == []


def test_fetch_financial_indicators_returns_empty_for_unknown_ticker(dart_client_with_corp):
    assert dart_client_with_corp.fetch_financial_indicators("999999", bsns_year=2024) == []
