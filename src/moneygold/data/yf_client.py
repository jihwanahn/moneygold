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


# yfinance 분기 손익 row name 별칭 (ABBV 등 일부 종목이 비표준 이름 사용)
_REVENUE_ALIASES = ["Total Revenue", "Revenue", "Operating Revenue", "Total Revenues"]
_OP_INCOME_ALIASES = [
    "Operating Income",
    "Operating Income or Loss",
    "Total Operating Income As Reported",
    "Operating Income From Continuing Operations",
]
_NET_INCOME_ALIASES = [
    "Net Income",
    "Net Income Common Stockholders",
    "Net Income Continuous Operations",
    "Net Income From Continuing Operation Net Minority Interest",
    "Net Income Including Noncontrolling Interests",
]
_DILUTED_EPS_ALIASES = ["Diluted EPS", "Diluted EPS Continuing Operations"]
_BASIC_EPS_ALIASES = ["Basic EPS", "Basic EPS Continuing Operations"]


def fetch_quarterly_financials(ticker: str) -> pd.DataFrame:
    """분기 손익 (Quarterly Income Statement). yfinance가 *분기 단독값* 반환.

    Returns
    -------
    DataFrame  columns = ['quarter', 'year', 'q', 'revenue', 'op_income',
                          'net_income', 'op_margin', 'eps', '...yoy']
        한국 fundamentals.build_fundamentals와 동일 스키마 (정규화 단계 X).
    """
    import yfinance as yf

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        t = yf.Ticker(ticker)
        q = t.quarterly_income_stmt
    if q is None or q.empty:
        return pd.DataFrame()

    # q: index = 손익 항목 (Total Revenue 등), columns = 분기 timestamp (최신 → 과거)
    rows = []
    for col in sorted(q.columns):
        y = col.year
        qn = (col.month - 1) // 3 + 1
        def _v_any(names: list[str]) -> float:
            for name in names:
                if name in q.index:
                    v = q.loc[name, col]
                    try:
                        if pd.notna(v):
                            return float(v)
                    except Exception:
                        continue
            return float("nan")
        revenue = _v_any(_REVENUE_ALIASES)
        op_income = _v_any(_OP_INCOME_ALIASES)
        net_income = _v_any(_NET_INCOME_ALIASES)
        eps = _v_any(_DILUTED_EPS_ALIASES)
        if pd.isna(eps):
            eps = _v_any(_BASIC_EPS_ALIASES)
        op_margin = (op_income / revenue * 100.0) if (revenue and revenue != 0 and pd.notna(op_income)) else float("nan")
        rows.append({
            "quarter": f"{y}Q{qn}",
            "year": y, "q": qn,
            "revenue": revenue,
            "op_income": op_income,
            "net_income": net_income,
            "op_margin": op_margin,
            "eps": eps,
        })
    df = pd.DataFrame(rows).sort_values(["year", "q"]).reset_index(drop=True)

    # YoY: (year-1, same q) 매칭 — yfinance도 일부 종목 분기 누락 있음
    from ..fundamentals import _attach_yoy_by_year_q
    _attach_yoy_by_year_q(df, [("revenue", "revenue_yoy"),
                                ("op_income", "op_income_yoy"),
                                ("eps", "eps_yoy")])
    return df


# ============================================================
# Helpers
# ============================================================

def _empty_bars_df() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "ticker", "date", "open", "high", "low", "close",
        "volume", "value", "adj_factor",
    ])
