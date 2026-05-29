"""pykrx 일봉 / 지수 fetcher.

KIS 일봉 (페이지네이션, 500 retry 빈도)의 대안. 한 호출에 전체 기간 받음.
출력 스키마는 KIS 호환:
    ticker, date(YYYYMMDD str), open, high, low, close, volume, value, adj_factor

PR-F: ``DataSync.backfill_bars`` / ``backfill_index``의 KIS 의존성 제거 후 기본 source.
KIS는 옵션으로 보존.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)


# pykrx 지수 코드: moneygold 내부 라벨 → KRX index 코드
# 기존 ``data/kis_endpoints.py`` INDEX_CODES와 코드 체계가 다르므로 별도 매핑.
PYKRX_INDEX_CODES = {
    "KOSPI": "1001",
    "KOSDAQ": "2001",
    "KOSPI200": "1028",
    "KOSDAQ150": "2203",
}


_OHLCV_RENAME = {
    "시가": "open", "고가": "high", "저가": "low", "종가": "close",
    "거래량": "volume", "거래대금": "value",
}


def _normalize_pykrx_ohlcv(
    raw: pd.DataFrame,
    ticker: str,
    start_inclusive: str,
    end_inclusive: str,
) -> pd.DataFrame:
    """pykrx OHLCV DataFrame → moneygold 표준 스키마.

    pykrx 응답의 index는 DatetimeIndex (날짜), columns에 한글 OHLCV.
    빈 row 또는 0-close는 제외 (휴장/거래정지 등).
    """
    if raw is None or raw.empty:
        return _empty_bars()

    df = raw.reset_index()
    # 날짜 컬럼 이름은 '날짜' 또는 'index'. 둘 다 처리.
    date_col = "날짜" if "날짜" in df.columns else df.columns[0]
    df = df.rename(columns={date_col: "date", **_OHLCV_RENAME})
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
    df = df[(df["date"] >= start_inclusive) & (df["date"] <= end_inclusive)].copy()

    # OHLCV 컬럼 보강 — pykrx가 가끔 거래대금 등을 누락. 누락 컬럼은 0으로 채움.
    for col in ("open", "high", "low", "close", "volume", "value"):
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")

    df = df[df["close"] > 0].copy()
    df["ticker"] = ticker
    df["adj_factor"] = 1.0
    cols = ["ticker", "date", "open", "high", "low", "close", "volume", "value", "adj_factor"]
    return df[cols].sort_values("date").reset_index(drop=True)


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "ticker", "date", "open", "high", "low", "close", "volume", "value", "adj_factor",
    ])


def fetch_bars_pykrx(
    ticker: str,
    start: str,
    end: str,
    *,
    adjusted: bool = True,
) -> pd.DataFrame:
    """``[start, end]`` 기간 일봉을 pykrx에서 받아 표준 스키마로 반환.

    KIS와 동일 스키마 (ticker, date, open, high, low, close, volume, value, adj_factor).
    pykrx는 한 호출에 전체 기간을 반환하므로 페이지네이션 불필요.
    """
    if start > end:
        return _empty_bars()
    try:
        from pykrx import stock as _pkx
    except ImportError as e:
        raise RuntimeError("pykrx not installed") from e

    try:
        raw = _pkx.get_market_ohlcv_by_date(start, end, ticker, adjusted=adjusted)
    except Exception as e:  # noqa: BLE001 — pykrx KRX 통신 실패
        log.debug("[%s] pykrx ohlcv fetch failed: %s", ticker, e)
        return _empty_bars()
    return _normalize_pykrx_ohlcv(raw, ticker, start, end)


def fetch_index_bars_pykrx(
    label: str,
    start: str,
    end: str,
) -> pd.DataFrame:
    """KOSPI/KOSDAQ/KOSPI200/KOSDAQ150 일봉.

    label은 moneygold 내부 라벨 ('KOSPI' 등). 내부적으로 KRX index 코드로 변환.
    ticker 컬럼 자리에 label을 그대로 사용 (지수는 종목 코드 없음).
    """
    if label not in PYKRX_INDEX_CODES:
        raise ValueError(f"unknown index label: {label!r}. "
                         f"Use one of {list(PYKRX_INDEX_CODES)}")
    if start > end:
        return _empty_bars()
    try:
        from pykrx import stock as _pkx
    except ImportError as e:
        raise RuntimeError("pykrx not installed") from e

    code = PYKRX_INDEX_CODES[label]
    try:
        raw = _pkx.get_index_ohlcv_by_date(start, end, code)
    except Exception as e:  # noqa: BLE001
        log.debug("[%s] pykrx index ohlcv fetch failed: %s", label, e)
        return _empty_bars()
    out = _normalize_pykrx_ohlcv(raw, label, start, end)
    return out
