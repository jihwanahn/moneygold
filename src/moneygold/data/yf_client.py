"""yfinance 기반 미국 주식 데이터 fetcher.

KIS 클라이언트와 같은 인터페이스로 시세/지수/재무를 제공하되 yfinance 호출만 사용.
종목 코드 형식:
  - 일반: AAPL, MSFT
  - 클래스주: BRK-B, BF-B  (한국 종목은 005930)

분기 재무 (Income Statement)는 yfinance가 *분기 단독값*으로 줘서 한국 KIS의
YTD 누적 정규화가 필요 없음.
"""
from __future__ import annotations

import logging
import warnings

import pandas as pd

log = logging.getLogger(__name__)


def fetch_daily_bars(
    ticker: str,
    *,
    period: str = "2y",
    auto_adjust: bool = True,
) -> pd.DataFrame:
    """일봉 OHLCV.

    Returns
    -------
    DataFrame  columns = ['ticker', 'date', 'open', 'high', 'low', 'close',
                          'volume', 'value', 'adj_factor']
        date는 YYYYMMDD 문자열 (KIS와 동일).
        value(거래대금) = close * volume (yfinance 직접 제공 안 함).
    """
    import yfinance as yf

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = yf.Ticker(ticker).history(period=period, auto_adjust=auto_adjust)
    if df is None or df.empty:
        return _empty_bars_df()

    out = pd.DataFrame({
        "ticker": ticker,
        "date": df.index.strftime("%Y%m%d"),
        "open": df["Open"].astype(float).values,
        "high": df["High"].astype(float).values,
        "low": df["Low"].astype(float).values,
        "close": df["Close"].astype(float).values,
        "volume": df["Volume"].fillna(0).astype("int64").values,
    })
    out["value"] = (out["close"] * out["volume"]).astype("int64")
    out["adj_factor"] = 1.0
    out = out.dropna(subset=["close"]).reset_index(drop=True)
    return out


def fetch_daily_bars_batch(
    tickers: list[str],
    *,
    period: str = "2y",
    auto_adjust: bool = True,
    batch_size: int = 50,
) -> dict[str, pd.DataFrame]:
    """여러 ticker를 yf.download로 한 번에 받아 ticker별 표준 OHLCV DataFrame dict 반환.

    `yf.download` 는 threaded HTTP로 ticker별 호출보다 5-10배 빠름. 단 일부
    ticker는 batch 내에서 누락될 수 있으므로 *항상* 결과 dict의 keys를 확인.

    Parameters
    ----------
    tickers : 종목 코드 리스트.
    period : yfinance period 문자열 ('2y', '1y', '6mo', ...).
    auto_adjust : 분할/배당 자동 조정.
    batch_size : yf.download 1회 호출당 ticker 개수 (메모리/실패 risk 트레이드오프).

    Returns
    -------
    dict[ticker, DataFrame]  단일 ticker DataFrame은 ``fetch_daily_bars`` 결과와
        동일 포맷 (ticker/date/OHLCV/value/adj_factor). 데이터 없는 ticker는
        빈 DataFrame.
    """
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    if not tickers:
        return out
    unique = list(dict.fromkeys(tickers))

    for i in range(0, len(unique), batch_size):
        batch = unique[i : i + batch_size]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                df = yf.download(
                    batch, period=period, group_by="ticker",
                    auto_adjust=auto_adjust, threads=True, progress=False,
                )
        except Exception as e:
            log.warning("yf.download batch 실패: %s — 빈 결과로 처리", e)
            for tk in batch:
                out[tk] = _empty_bars_df()
            continue

        if df is None or df.empty:
            for tk in batch:
                out[tk] = _empty_bars_df()
            continue

        # 단일 ticker일 때는 MultiIndex 안 만들어짐. wrap it.
        if not isinstance(df.columns, pd.MultiIndex):
            df.columns = pd.MultiIndex.from_product([batch, df.columns])

        for tk in batch:
            try:
                sub = df[tk]
            except KeyError:
                out[tk] = _empty_bars_df()
                continue
            if sub is None or sub.empty:
                out[tk] = _empty_bars_df()
                continue
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                out[tk] = _empty_bars_df()
                continue
            tdf = pd.DataFrame({
                "ticker": tk,
                "date": sub.index.strftime("%Y%m%d"),
                "open": sub["Open"].astype(float).values,
                "high": sub["High"].astype(float).values,
                "low": sub["Low"].astype(float).values,
                "close": sub["Close"].astype(float).values,
                "volume": sub["Volume"].fillna(0).astype("int64").values,
            })
            tdf["value"] = (tdf["close"] * tdf["volume"]).astype("int64")
            tdf["adj_factor"] = 1.0
            out[tk] = tdf.reset_index(drop=True)

    return out


def fetch_index_bars(symbol: str = "^GSPC", period: str = "2y") -> pd.DataFrame:
    """지수 일봉 — ^GSPC (S&P 500), ^IXIC (NASDAQ Composite), ^RUT (Russell 2000)."""
    import yfinance as yf

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = yf.Ticker(symbol).history(period=period, auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame(columns=["index_code", "date", "open", "high", "low", "close", "volume", "value"])

    out = pd.DataFrame({
        "index_code": symbol,
        "date": df.index.strftime("%Y%m%d"),
        "open": df["Open"].astype(float).values,
        "high": df["High"].astype(float).values,
        "low": df["Low"].astype(float).values,
        "close": df["Close"].astype(float).values,
        "volume": df.get("Volume", pd.Series(0)).fillna(0).astype("int64").values,
    })
    out["value"] = 0
    out = out.dropna(subset=["close"]).reset_index(drop=True)
    return out


# NOTE: 분기 손익은 SEC EDGAR (data/sec_edgar.py)로 이관됨 — yfinance는 분기 ~5개만
# 줘서 'consecutive growth quarters' 같은 지표 측정 불가. yfinance 모듈은 가격(bars/
# index)과 master enrichment, consensus 용도로만 유지.

# ============================================================
# Helpers
# ============================================================

def _empty_bars_df() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "ticker", "date", "open", "high", "low", "close",
        "volume", "value", "adj_factor",
    ])
