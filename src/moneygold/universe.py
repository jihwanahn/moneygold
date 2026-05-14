"""종목 마스터 + 유니버스 필터.

pykrx로 KOSPI/KOSDAQ 종목 리스트를 받아 우선주/스팩/ETF/ETN/리츠를 제외한다.
관리종목·투자경고·거래정지 플래그는 PR3에서 추가 (KIS search-stock-info 또는
pykrx 별도 조회). 본 모듈은 마스터 sync에만 책임.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

import pandas as pd

log = logging.getLogger(__name__)


# 우선주: 종목코드 6자리 마지막이 5, 7, 9 (또는 우선주B/C 변종). 5/7이 압도적 다수.
_PREFERRED_TAIL = re.compile(r"[5-9]$")

# 종목명 패턴으로 제외할 비즈니스 형태
_SPAC_NAME = re.compile(r"스팩")
_REIT_NAME = re.compile(r"리츠")
# ETF/ETN은 종목명에 "ETF"/"ETN" 또는 운용사 prefix 가 흔하지만 명확하지 않아
# pykrx의 etf/etn 리스트로 별도 컷.


def fetch_master_from_pykrx() -> pd.DataFrame:
    """KOSPI + KOSDAQ 종목 마스터 + 시장구분.

    Returns
    -------
    DataFrame  columns = ['ticker', 'name', 'market']
    """
    from pykrx import stock

    rows: list[dict[str, str]] = []
    for market in ("KOSPI", "KOSDAQ"):
        tickers = stock.get_market_ticker_list(market=market)
        for tk in tickers:
            try:
                name = stock.get_market_ticker_name(tk)
            except Exception as e:
                log.warning("get_market_ticker_name failed for %s: %s", tk, e)
                name = ""
            rows.append({"ticker": tk, "name": name, "market": market})
    df = pd.DataFrame(rows)
    log.info("Fetched %d tickers from pykrx (KOSPI+KOSDAQ)", len(df))
    return df


def _fetch_etp_tickers() -> set[str]:
    """ETF/ETN 종목 set. pykrx의 별도 함수 사용."""
    from pykrx import stock

    out: set[str] = set()
    for fn_name in ("get_etf_ticker_list", "get_etn_ticker_list"):
        fn = getattr(stock, fn_name, None)
        if fn is None:
            continue
        try:
            out.update(fn())
        except Exception as e:
            log.warning("%s failed: %s", fn_name, e)
    return out


def is_preferred_share(ticker: str) -> bool:
    """우선주 추정. 6자리 종목코드의 마지막 자리가 5~9면 우선주/우선주B/우선주C 등."""
    if len(ticker) != 6:
        return False
    return bool(_PREFERRED_TAIL.match(ticker[-1]))


def filter_master(
    df: pd.DataFrame,
    *,
    drop_preferred: bool = True,
    drop_spac: bool = True,
    drop_reit: bool = True,
    drop_etp: bool = True,
    extra_exclude: Iterable[str] | None = None,
) -> pd.DataFrame:
    """우선주/스팩/리츠/ETF·ETN 제외한 보통주만 반환.

    종목명 패턴 + 종목코드 패턴 + pykrx etf/etn 리스트의 합집합으로 컷.
    """
    if df.empty:
        return df.copy()

    out = df.copy()
    n0 = len(out)

    if drop_preferred:
        mask = ~out["ticker"].map(is_preferred_share)
        n_dropped = (~mask).sum()
        out = out[mask]
        log.debug("Dropped %d preferred shares", n_dropped)

    if drop_spac:
        mask = ~out["name"].astype(str).str.contains(_SPAC_NAME, na=False)
        out = out[mask]

    if drop_reit:
        mask = ~out["name"].astype(str).str.contains(_REIT_NAME, na=False)
        out = out[mask]

    if drop_etp:
        etp = _fetch_etp_tickers()
        if etp:
            out = out[~out["ticker"].isin(etp)]

    if extra_exclude:
        out = out[~out["ticker"].isin(set(extra_exclude))]

    out = out.drop_duplicates(subset=["ticker"]).sort_values("ticker").reset_index(drop=True)
    log.info("Universe filter: %d -> %d (dropped %d)", n0, len(out), n0 - len(out))
    return out


def is_flagged(ticker: str, asof: str) -> bool:
    """관리종목/투자경고/거래정지 여부.

    PR1 범위에선 항상 False 반환 (placeholder). PR3 시그널 단계에서
    KIS search-stock-info 또는 pykrx로 실제 플래그 조회 구현.
    """
    return False
