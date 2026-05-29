"""DART 사업보고서 모듈 테스트."""
from __future__ import annotations

import pandas as pd

from moneygold.data import dart_business as db


# ----------------------------------------------------------------------
# Path helpers
# ----------------------------------------------------------------------

def test_path_helpers_distinct(tmp_path):
    assert db.share_issuance_path(tmp_path, "005930") != db.treasury_status_path(tmp_path, "005930")
    assert db.financials_raw_path(tmp_path, "005930") != db.company_info_path(tmp_path, "005930")


# ----------------------------------------------------------------------
# Normalize
# ----------------------------------------------------------------------

def test_normalize_adds_ticker_fiscal_year_fetched_at():
    rows = [{"isu_dcrs_de": "20240819", "isu_dcrs_stle": "-"}]
    df = db._normalize_rows(rows, ticker="086790", fiscal_year=2024, asof="20260528")
    assert df["ticker"].iloc[0] == "086790"
    assert df["fiscal_year"].iloc[0] == 2024
    assert df["fetched_at"].iloc[0] == "20260528"


def test_normalize_empty():
    df = db._normalize_rows([], "086790", 2024, "20260528")
    assert df.empty


# ----------------------------------------------------------------------
# fetch_business_for_ticker with mock DartClient
# ----------------------------------------------------------------------

class _FakeDart:
    """3 endpoint 응답을 미리 정의 + corp_code 매핑 있음."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch_share_issuance(self, ticker, bsns_year, reprt_code="11011"):
        self.calls.append(("issu", ticker, bsns_year))
        return self.responses.get(("issu", ticker, bsns_year), [])

    def fetch_treasury_status(self, ticker, bsns_year, reprt_code="11011"):
        self.calls.append(("trsy", ticker, bsns_year))
        return self.responses.get(("trsy", ticker, bsns_year), [])

    def fetch_financial_statements_all(self, ticker, bsns_year, reprt_code="11011", fs_div="CFS"):
        self.calls.append(("fin", ticker, bsns_year))
        return self.responses.get(("fin", ticker, bsns_year), [])

    def fetch_company_info(self, ticker):
        self.calls.append(("info", ticker))
        return self.responses.get(("info", ticker))


def test_fetch_business_for_ticker_iterates_5_years():
    """asof_year=2026 → end_year=2025 (가장 최근 발표 사업보고서), years=5 → 2021~2025."""
    fake = _FakeDart({
        ("issu", "086790", 2024): [{"isu_dcrs_de": "20240819"}],
        ("trsy", "086790", 2023): [{"acqs_mth1": "A"}],
        ("fin", "086790", 2024): [{"sj_nm": "재무상태표", "account_nm": "자산총계"}],
    })
    out = db.fetch_business_for_ticker(fake, "086790", asof="20260528", years=5)
    assert "share_issuance" in out
    assert "treasury_status" in out
    assert "financials_raw" in out
    # 호출 — 5년 × 3 endpoint = 15
    fetch_calls = [c for c in fake.calls if c[0] in ("issu", "trsy", "fin")]
    assert len(fetch_calls) == 5 * 3
    years_called = sorted({c[2] for c in fetch_calls})
    assert years_called == [2021, 2022, 2023, 2024, 2025]
    # 실제 데이터 있는 것만 빈 아님
    assert not out["share_issuance"].empty
    assert not out["treasury_status"].empty


def test_sync_dart_business_writes_files(tmp_path):
    fake = _FakeDart({
        ("issu", "086790", 2025): [{"isu_dcrs_de": "20240819", "isu_dcrs_stle": "주식배당"}],
        ("trsy", "086790", 2025): [{"acqs_mth1": "기타취득"}],
        ("fin", "086790", 2025): [{"sj_nm": "재무상태표", "account_nm": "자산총계",
                                   "thstrm_amount": "637847513000000"}],
        ("info", "086790"): {"corp_name": "(주)하나금융지주", "ceo_nm": "함영주",
                              "est_dt": "20051201", "induty_code": "64992"},
    })
    stats = db.sync_dart_business(fake, tmp_path, ["086790"],
                                   asof="20260528", years=1)
    assert stats["updated"] == 1
    assert stats["failed"] == []
    # parquet 파일들
    assert db.share_issuance_path(tmp_path, "086790").exists()
    assert db.treasury_status_path(tmp_path, "086790").exists()
    assert db.financials_raw_path(tmp_path, "086790").exists()
    assert db.company_info_path(tmp_path, "086790").exists()
    # company info 내용
    info = db.load_company_info(tmp_path, "086790")
    assert info["corp_name"] == "(주)하나금융지주"
    assert info["ceo_nm"] == "함영주"
    assert info["fetched_at"] == "20260528"


def test_sync_continues_after_individual_failure(tmp_path):
    class _PartialDart(_FakeDart):
        def fetch_share_issuance(self, ticker, bsns_year, reprt_code="11011"):
            if ticker == "BADTKR":
                raise RuntimeError("DART timeout")
            return super().fetch_share_issuance(ticker, bsns_year, reprt_code)

    fake = _PartialDart({
        ("issu", "086790", 2025): [{"isu_dcrs_de": "20240819"}],
        ("info", "086790"): {"corp_name": "테스트"},
    })
    stats = db.sync_dart_business(fake, tmp_path, ["BADTKR", "086790"],
                                   asof="20260528", years=1)
    assert len(stats["failed"]) == 1
    assert stats["failed"][0][0] == "BADTKR"
    # 086790은 정상 처리
    assert db.share_issuance_path(tmp_path, "086790").exists()


def test_sync_skip_company_info_when_disabled(tmp_path):
    fake = _FakeDart({
        ("issu", "086790", 2025): [{"isu_dcrs_de": "20240819"}],
        ("info", "086790"): {"corp_name": "테스트"},
    })
    db.sync_dart_business(fake, tmp_path, ["086790"],
                          asof="20260528", years=1, include_company_info=False)
    assert not db.company_info_path(tmp_path, "086790").exists()
    # but share_issuance is written
    assert db.share_issuance_path(tmp_path, "086790").exists()


# ----------------------------------------------------------------------
# load_* fallback
# ----------------------------------------------------------------------

def test_loaders_return_empty_for_missing(tmp_path):
    assert db.load_share_issuance(tmp_path, "NONE").empty
    assert db.load_treasury_status(tmp_path, "NONE").empty
    assert db.load_financials_raw(tmp_path, "NONE").empty
    assert db.load_company_info(tmp_path, "NONE") is None
