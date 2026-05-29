"""DART 클라이언트 테스트.

네트워크를 타지 않는 순수 파서 + 캐시 동작 검증. 실제 DART OpenAPI 호출은
``requests.get``을 monkeypatch해서 가짜 응답으로 갈음.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from moneygold.config import DartConfig
from moneygold.strategies.value_long_term.dart_client import (
    DartClient,
    _parse_treasury_ratio,
)

# ----------------------------------------------------------------------
# Pure parsing — no network
# ----------------------------------------------------------------------

class TestParseTreasuryRatio:
    def test_direct_ratio_takes_precedence(self):
        raw = (
            '<TABLE><TE ACODE="SUM_TRS_RT" UNIT="%">5.42</TE>'
            '<TE ACODE="SUM_TRS_STK">100</TE>'
            '<TE ACODE="SUM_FLT_STK">100</TE></TABLE>'
        )
        assert _parse_treasury_ratio(raw) == 5.42

    def test_computed_from_treasury_and_floating(self):
        raw = (
            '<TE ACODE="SUM_TRS_STK">200</TE>'
            '<TE ACODE="SUM_FLT_STK">9800</TE>'
        )
        assert _parse_treasury_ratio(raw) == 2.0

    def test_fallback_to_issued(self):
        raw = (
            '<TE ACODE="SUM_TRS_STK">500</TE>'
            '<TE ACODE="SUM_FLT_STK">-</TE>'
            '<TE ACODE="ISU_STK2">10000</TE>'
        )
        assert _parse_treasury_ratio(raw) == 5.0

    def test_missing_treasury_returns_none(self):
        assert _parse_treasury_ratio("<TE ACODE='SUM_FLT_STK'>100</TE>") is None

    def test_empty_xml_returns_none(self):
        assert _parse_treasury_ratio("") is None

    def test_handles_commas_in_numbers(self):
        raw = (
            '<TE ACODE="SUM_TRS_STK">1,000,000</TE>'
            '<TE ACODE="SUM_FLT_STK">99,000,000</TE>'
        )
        assert _parse_treasury_ratio(raw) == 1.0


# ----------------------------------------------------------------------
# Client behavior with mocked HTTP
# ----------------------------------------------------------------------

@pytest.fixture
def dart_client(tmp_path):
    cfg = DartConfig(api_key="dummy-key", rate_per_sec=1000)  # high rate to skip throttle in tests
    return DartClient(cfg, data_dir=tmp_path)


def _make_corp_zip(mapping: dict[str, str]) -> bytes:
    """{stock_code: corp_code} 입력으로 DART corpCode.xml 형식의 zip 바이트를 만든다."""
    parts = ["<result>"]
    for stock, corp in mapping.items():
        parts.append(
            f"<list><corp_code>{corp}</corp_code>"
            f"<corp_name>회사{stock}</corp_name>"
            f"<stock_code>{stock}</stock_code></list>"
        )
    parts.append("</result>")
    xml_bytes = "\n".join(parts).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("CORPCODE.xml", xml_bytes)
    return buf.getvalue()


class _FakeResp:
    def __init__(self, *, json_data=None, content=None, status=200):
        self._json = json_data
        self.content = content or b""
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def test_corp_code_lookup_caches_to_disk(dart_client, monkeypatch):
    zip_bytes = _make_corp_zip({"005930": "00126380", "035720": "00258801"})
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return _FakeResp(content=zip_bytes)

    monkeypatch.setattr("moneygold.strategies.value_long_term.dart_client.requests.get", fake_get)

    assert dart_client.corp_code("005930") == "00126380"
    assert dart_client.corp_code("35720") == "00258801"  # zero-padded internally
    assert dart_client.corp_code("999999") is None

    cache_file = dart_client.cache_dir / "corp_codes.json"
    assert cache_file.exists()
    with cache_file.open(encoding="utf-8") as f:
        cached = json.load(f)
    assert cached["005930"]["corp_code"] == "00126380"

    # 두 번째 클라이언트는 디스크 캐시에서 읽고 네트워크 호출 없어야 함
    cfg = dart_client.cfg
    fresh = DartClient(cfg, data_dir=dart_client.cache_dir.parent)
    before = len(calls)
    assert fresh.corp_code("005930") == "00126380"
    assert len(calls) == before  # no new HTTP call


def test_treasury_activity_returns_empty_for_unknown_ticker(dart_client, monkeypatch):
    """corp_code 매핑에 없는 종목은 0건 + cancel_reports=[] 즉시 반환."""
    monkeypatch.setattr(
        "moneygold.strategies.value_long_term.dart_client.requests.get",
        lambda url, params=None, timeout=None: _FakeResp(content=_make_corp_zip({"005930": "00126380"})),
    )
    out = dart_client.treasury_activity("999999", asof="20260527", years=3)
    assert out == {
        "years_window": 3,
        "acquire_count": 0,
        "cancel_count": 0,
        "latest_cancel_date": None,
        "cancel_reports": [],
    }


def test_treasury_activity_counts_disclosures_and_caches(dart_client, monkeypatch):
    """list.json 응답을 모킹해서 소각/취득 카운팅과 캐시 동작 검증."""
    corp_zip = _make_corp_zip({"005930": "00126380"})

    # list.json 호출별 응답 큐: corpCode → acquire(B001) → all_disc
    responses = [
        _FakeResp(content=corp_zip),  # corpCode.xml
        _FakeResp(json_data={  # B001 acquire — 2건
            "status": "000", "total_page": 1,
            "list": [
                {"rcept_dt": "20251101", "report_nm": "자기주식취득결정", "rcept_no": "R1"},
                {"rcept_dt": "20250401", "report_nm": "자기주식취득결정", "rcept_no": "R2"},
            ],
        }),
        _FakeResp(json_data={  # all_disc — 다양한 공시 (소각 2건 + 정정본 1건 + 무관 1건)
            "status": "000", "total_page": 1,
            "list": [
                {"rcept_dt": "20260201", "report_nm": "자기주식 소각결정", "rcept_no": "R10"},
                {"rcept_dt": "20251215", "report_nm": "[기재정정]자기주식 소각결정", "rcept_no": "R11"},
                {"rcept_dt": "20250715", "report_nm": "주식소각결정", "rcept_no": "R12"},
                {"rcept_dt": "20250601", "report_nm": "주요사항보고서", "rcept_no": "R13"},
            ],
        }),
    ]
    idx = {"i": 0}

    def fake_get(url, params=None, timeout=None):
        resp = responses[idx["i"]]
        idx["i"] += 1
        return resp

    monkeypatch.setattr("moneygold.strategies.value_long_term.dart_client.requests.get", fake_get)

    result = dart_client.treasury_activity("005930", asof="20260527", years=3)
    assert result["acquire_count"] == 2
    assert result["cancel_count"] == 2  # 정정본은 제외
    assert result["latest_cancel_date"] == "20260201"
    assert len(result["cancel_reports"]) == 2

    # 두 번째 호출은 캐시에서 — 새 HTTP 호출 없음
    calls_before = idx["i"]
    result2 = dart_client.treasury_activity("005930", asof="20260527", years=3)
    assert result2 == result
    assert idx["i"] == calls_before


def test_missing_api_key_raises():
    with pytest.raises(ValueError, match="DART_API_KEY"):
        DartClient(DartConfig(api_key=""), data_dir=Path("/tmp"))


def test_dividend_decisions_per_year_counts_cash_dvdnd_only(dart_client, monkeypatch):
    """report_nm이 정확히 '현금ㆍ현물배당결정'인 공시만 카운트. 자회사·정정·취소 제외."""
    corp_zip = _make_corp_zip({"086790": "00547583"})
    responses = [
        _FakeResp(content=corp_zip),
        _FakeResp(json_data={
            "status": "000", "total_page": 1,
            "list": [
                {"rcept_dt": "20250425", "report_nm": "현금ㆍ현물배당결정", "rcept_no": "A1"},
                {"rcept_dt": "20250725", "report_nm": "현금ㆍ현물배당결정", "rcept_no": "A2"},
                {"rcept_dt": "20251028", "report_nm": "현금ㆍ현물배당결정", "rcept_no": "A3"},
                # 제외 — 자회사·정정
                {"rcept_dt": "20250724", "report_nm": "현금ㆍ현물배당결정(자회사의 주요경영사항)", "rcept_no": "A4"},
                {"rcept_dt": "20250726", "report_nm": "[기재정정]현금ㆍ현물배당결정", "rcept_no": "A5"},
                # 무관 공시
                {"rcept_dt": "20251216", "report_nm": "기타경영사항(자율공시)", "rcept_no": "A6"},
            ],
        }),
    ]
    idx = {"i": 0}

    def fake_get(url, params=None, timeout=None):
        resp = responses[idx["i"]]
        idx["i"] += 1
        return resp

    monkeypatch.setattr("moneygold.strategies.value_long_term.dart_client.requests.get", fake_get)
    per_year = dart_client.dividend_decisions_per_year("086790", asof="20260101", years=1)
    # 정확한 매치 3건 / 1년 = 3.0
    assert per_year == 3.0


def test_dividend_decisions_returns_none_for_unknown_ticker(dart_client, monkeypatch):
    monkeypatch.setattr(
        "moneygold.strategies.value_long_term.dart_client.requests.get",
        lambda url, params=None, timeout=None: _FakeResp(content=_make_corp_zip({"086790": "00547583"})),
    )
    assert dart_client.dividend_decisions_per_year("999999", asof="20260101", years=2) is None


def test_asof_changes_query_window(dart_client, monkeypatch):
    """다른 asof 값은 다른 캐시 파일에 떨어지고, list.json bgn_de/end_de도 그에 맞춰 달라져야 함."""
    corp_zip = _make_corp_zip({"005930": "00126380"})

    captured_params = []
    list_resp = _FakeResp(json_data={"status": "013", "total_page": 1, "list": []})

    def fake_get(url, params=None, timeout=None):
        captured_params.append((url, dict(params or {})))
        if url.endswith("corpCode.xml"):
            return _FakeResp(content=corp_zip)
        return list_resp

    monkeypatch.setattr("moneygold.strategies.value_long_term.dart_client.requests.get", fake_get)

    dart_client.treasury_activity("005930", asof="20260101", years=1)
    dart_client.treasury_activity("005930", asof="20250101", years=1)

    list_calls = [p for url, p in captured_params if url.endswith("list.json")]
    # 첫 윈도우는 end=20260101, 두 번째는 end=20250101
    end_dates = sorted({p["end_de"] for p in list_calls})
    assert end_dates == ["20250101", "20260101"]
