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
# negative lookbehind로 "메리츠" 보호. "리츠" 앞에 "메"가 오면 매치 안 함.
# 실제 KRX REIT 종목명은 모두 "...리츠"로 끝나고 "메리츠"만 false positive를 일으킴.
# 예: 롯데리츠/신한알파리츠/한화리츠/이리츠코크렙 등은 매치 ✓, 메리츠금융지주는 패스 ✓
_REIT_NAME = re.compile(r"(?<!메)리츠")
# ETF/ETN은 종목명에 "ETF"/"ETN" 또는 운용사 prefix 가 흔하지만 명확하지 않아
# pykrx의 etf/etn 리스트로 별도 컷.


def fetch_master_from_pykrx(asof: str | None = None) -> pd.DataFrame:
    """KOSPI + KOSDAQ 종목 마스터 + 시장구분 + 업종 + 시가총액.

    pykrx.get_market_sector_classifications가 한 번에 sector + mcap을 줌.
    종목 마스터는 보통 매일 갱신할 필요 없지만, sector·mcap은 시가 기준이라
    가능하면 가장 최근 영업일 데이터로.

    Returns
    -------
    DataFrame  columns = ['ticker', 'name', 'market', 'sector', 'mcap']
        sector : KRX 표준 업종명 (e.g. '전기·전자', '화학', 'IT 서비스')
                 일부 종목은 sector 정보 없음 → 'UNKNOWN'
        mcap   : 시가총액 (KRW)
    """
    from datetime import datetime
    from pykrx import stock

    biz_date = asof or datetime.now().strftime("%Y%m%d")
    biz_date = stock.get_nearest_business_day_in_a_week(biz_date)

    rows: list[dict] = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            sec_df = stock.get_market_sector_classifications(biz_date, market)
        except Exception as e:
            log.warning("get_market_sector_classifications failed for %s: %s — fallback to ticker_list only", market, e)
            sec_df = None

        if sec_df is not None and not sec_df.empty:
            sec_df = sec_df.reset_index().rename(columns={
                "종목코드": "ticker", "종목명": "name",
                "업종명": "sector", "시가총액": "mcap",
            })
            sec_df["market"] = market
            sec_df["sector"] = sec_df["sector"].fillna("UNKNOWN").astype(str)
            sec_df["mcap"] = pd.to_numeric(sec_df["mcap"], errors="coerce").fillna(0).astype("int64")
            rows.extend(sec_df[["ticker", "name", "market", "sector", "mcap"]].to_dict("records"))
        else:
            # fallback: 종목 리스트만
            tickers = stock.get_market_ticker_list(market=market)
            for tk in tickers:
                try:
                    name = stock.get_market_ticker_name(tk)
                except Exception:
                    name = ""
                rows.append({"ticker": tk, "name": name, "market": market,
                             "sector": "UNKNOWN", "mcap": 0})

    df = pd.DataFrame(rows)
    log.info("Fetched %d tickers from pykrx (KOSPI+KOSDAQ) on %s", len(df), biz_date)
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
