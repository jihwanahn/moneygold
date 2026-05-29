"""유니버스/유동성/시총/상장기간 필터.

universe.py 의 우선주/스팩/리츠/ETP 제외 로직을 그대로 재사용한다.
이 모듈은 *추가로* 다음을 한다:

  - 당일 거래대금 상위 N위 ticker set
  - 시가총액 상위 N위 ticker set (옵션)
  - 종목별 상장기간 (bars 길이) 검증
  - flagged 제외 (universe.is_flagged 위임 — 현재 placeholder)

모두 pure functions. asof 명시.
"""
from __future__ import annotations

import pandas as pd

from ... import universe as univ


def filter_master(master: pd.DataFrame, extra_exclude: list[str] | None = None) -> pd.DataFrame:
    """master에서 우선주/스팩/리츠/ETF/ETN 제외.

    universe.filter_master 의 thin wrapper. KOSPI+KOSDAQ 통합 그대로.
    US 종목은 KR universe.filter_master 의 spac/reit 패턴엔 안 걸릴 가능성이 있어
    호출자에서 market in ('KOSPI', 'KOSDAQ') 사전 필터 권장.
    """
    return univ.filter_master(
        master,
        drop_preferred=True,
        drop_spac=True,
        drop_reit=True,
        drop_etp=True,
        extra_exclude=extra_exclude,
    )


def top_n_by_value(
    today_value_by_ticker: dict[str, float] | pd.Series,
    n: int,
) -> set[str]:
    """당일 거래대금 상위 N ticker.

    Parameters
    ----------
    today_value_by_ticker : 종목별 당일 거래대금 (KRW). dict 또는 Series.
        bars['value'].iloc[-1] 로 추출해서 dict 만들어 넣는 게 일반적.
    n : 상위 N (>=1).

    Returns
    -------
    set[str]  상위 N개 ticker. 동률이면 dict/Series 순서대로.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    s = pd.Series(today_value_by_ticker, dtype=float)
    s = s.dropna()
    if s.empty:
        return set()
    top = s.sort_values(ascending=False).head(n)
    return set(top.index.astype(str).tolist())


def top_n_by_marketcap(
    master: pd.DataFrame,
    n: int,
) -> set[str]:
    """master의 시가총액 상위 N ticker. mcap 컬럼 필요."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if "mcap" not in master.columns or master.empty:
        return set()
    top = master.sort_values("mcap", ascending=False).head(n)
    return set(top["ticker"].astype(str).tolist())


def min_listed_days_ok(bars: pd.DataFrame, asof: str, min_days: int) -> bool:
    """asof 이하 bars 행 수가 min_days 이상인가.

    bars는 date 컬럼 (YYYYMMDD str) 보유. asof 시점까지의 봉 수로 상장 후 경과
    영업일을 근사. 정확한 상장일은 별도 source 필요하지만, 현 데이터 인프라에서
    가장 신뢰 가능한 proxy.
    """
    if bars is None or bars.empty or "date" not in bars.columns:
        return False
    clipped = bars[bars["date"] <= asof]
    return len(clipped) >= min_days


def is_flagged(ticker: str, asof: str) -> bool:
    """관리종목/거래정지/투자경고. 현재 universe.is_flagged 는 placeholder → 항상 False.

    TODO: PR3+ 에서 KIS search-stock-info 또는 pykrx 로 실제 플래그 조회.
    """
    return univ.is_flagged(ticker, asof)
