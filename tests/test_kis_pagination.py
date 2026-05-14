"""kis_client.fetch_daily_bars: 페이지네이션. KIS HTTP는 모킹."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from moneygold.config import KISConfig
from moneygold.data import kis_client


def _make_client(tmp_path: Path) -> kis_client.KISClient:
    cfg = KISConfig(
        app_key="dummy_key",
        app_secret="dummy_secret",
        account_no="11111111",
        account_prod_cd="01",
        token_cache_path=tmp_path / "token.json",
    )
    c = kis_client.KISClient(cfg, rate_per_sec=1000)
    # 인증을 우회: 가짜 토큰을 미리 박아둠
    c._token = kis_client._Token(
        access_token="dummy",
        expires_at=9999999999.0,
        issued_at=0.0,
        key_hash=kis_client._app_key_hash("dummy_key"),
    )
    return c


def _row(date: str) -> dict:
    return {
        "stck_bsop_date": date,
        "stck_oprc": "100", "stck_hgpr": "110", "stck_lwpr": "90", "stck_clpr": "105",
        "acml_vol": "1000", "acml_tr_pbmn": "100000",
    }


def _date_range(start: str, end: str) -> list[str]:
    """캘린더 날짜 리스트 (주말 포함). KIS 영업일과 다르지만 페이지네이션 검증엔 충분."""
    s = datetime.strptime(start, "%Y%m%d")
    e = datetime.strptime(end, "%Y%m%d")
    out = []
    d = s
    while d <= e:
        out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


def test_pagination_concatenates_pages(monkeypatch, tmp_path):
    """3 페이지에 걸쳐 일봉이 분할되어 와도 모두 모아진다."""
    client = _make_client(tmp_path)

    # 가짜 데이터: 20260101~20260120 (캘린더 20일)
    all_dates = _date_range("20260101", "20260120")
    # 최신→과거 정렬 (KIS output2 순서)
    all_dates_desc = list(reversed(all_dates))

    page_size = 8
    calls = []

    def fake_get(path, tr_id, params):
        calls.append(dict(params))
        cur_end = params["FID_INPUT_DATE_2"]
        start = params["FID_INPUT_DATE_1"]
        page = [d for d in all_dates_desc if start <= d <= cur_end][:page_size]
        return {"rt_cd": "0", "output2": [_row(d) for d in page]}

    monkeypatch.setattr(client, "_get", fake_get)
    df = client.fetch_daily_bars("005930", "20260101", "20260120")

    # 모든 20일이 정렬되어 들어와야 함
    assert list(df["date"]) == all_dates
    # 페이지네이션은 최소 3번 (20일 / 8 = 3 페이지)
    assert len(calls) >= 3
    # 두 번째 호출의 FID_INPUT_DATE_2가 첫 호출의 oldest - 1
    assert calls[1]["FID_INPUT_DATE_2"] < calls[0]["FID_INPUT_DATE_2"]


def test_pagination_stops_on_empty_page(monkeypatch, tmp_path):
    client = _make_client(tmp_path)
    calls = []

    def fake_get(path, tr_id, params):
        calls.append(1)
        if len(calls) == 1:
            return {"rt_cd": "0", "output2": [_row("20260105"), _row("20260104")]}
        return {"rt_cd": "0", "output2": []}

    monkeypatch.setattr(client, "_get", fake_get)
    df = client.fetch_daily_bars("005930", "20260101", "20260131")
    assert len(df) == 2
    assert len(calls) == 2


def test_pagination_guards_against_stuck(monkeypatch, tmp_path):
    """KIS가 같은 oldest를 계속 돌려줘서 진행이 안 될 때 무한루프 방지."""
    client = _make_client(tmp_path)

    def fake_get(path, tr_id, params):
        return {"rt_cd": "0", "output2": [_row("20260105")]}

    monkeypatch.setattr(client, "_get", fake_get)
    df = client.fetch_daily_bars("005930", "20260101", "20260131")
    # 한 행만 와도 진행이 안 되면 종료 (무한루프 X)
    assert len(df) == 1


def test_start_after_end_returns_empty(monkeypatch, tmp_path):
    client = _make_client(tmp_path)
    df = client.fetch_daily_bars("005930", "20260131", "20260101")
    assert df.empty
