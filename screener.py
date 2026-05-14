from __future__ import annotations

import os
import time
import json
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, Any, List

import numpy as np
import pandas as pd
from pykrx import stock
from tqdm import tqdm
from functools import lru_cache
from constants import RESULT_DIR


def _get_fundamental_cached(biz_date: str, market: str) -> pd.DataFrame:
    f_path = _parquet_path("fundamental", market, biz_date)
    f = _load_parquet(f_path)
    if f is None:
        f = _fetch_with_retry(lambda: stock.get_market_fundamental_by_ticker(biz_date, market=market))
        f = f.rename(columns={"DIV": "DIV_YIELD"}).replace([np.inf, -np.inf], pd.NA)
        _save_parquet(f, f_path)
    # index = ticker
    return f


def _get_cap_cached(biz_date: str, market: str) -> pd.DataFrame:
    c_path = _parquet_path("cap", market, biz_date)
    cap = _load_parquet(c_path)
    if cap is None:
        cap = _fetch_with_retry(lambda: stock.get_market_cap_by_ticker(biz_date, market=market))
        cap = cap.rename(columns={"시가총액": "MCAP", "거래대금": "VALUE_TRADED"}).replace([np.inf, -np.inf], pd.NA)
        _save_parquet(cap, c_path)
    # index = ticker
    return cap


def build_user_interest_df(
    tickers: List[str],
    biz_date: str,
    market: str,
    score_params: ScoreParams
) -> pd.DataFrame:
    """
    user_interest ticker들에 대해 ranked_df와 같은 컬럼 구조를 최대한 맞춰 DF로 만든다.
    - fundamental/cap: biz_date 기준
    - MOM/VOL: 기존 OHLCV 캐시로 계산
    - SCORE 및 S_*: user_interest는 스크리닝/랭킹 목적이 아니므로 None으로 둔다(원하면 추후 계산 가능)
    """

    f = _get_fundamental_cached(biz_date, market=market)
    cap = _get_cap_cached(biz_date, market=market)

    # market="ALL"이면 f/cap에 KOSPI/KOSDAQ이 섞여 들어올 수 있어 그대로 join 시도
    base = pd.DataFrame({"TICKER": tickers})
    base["NAME"] = [stock.get_market_ticker_name(t) for t in tickers]

    # fundamental/cap 붙이기 (없으면 NaN)
    f_part = f.reindex(tickers)
    cap_part = cap.reindex(tickers)

    base["PBR"] = f_part["PBR"].values if "PBR" in f_part.columns else np.nan
    base["PER"] = f_part["PER"].values if "PER" in f_part.columns else np.nan
    # DIV -> DIV_YIELD로 이미 rename되어 있음
    base["DIV_YIELD"] = f_part["DIV_YIELD"].values if "DIV_YIELD" in f_part.columns else np.nan

    base["MCAP"] = cap_part["MCAP"].values if "MCAP" in cap_part.columns else np.nan
    base["VALUE_TRADED"] = cap_part["VALUE_TRADED"].values if "VALUE_TRADED" in cap_part.columns else np.nan

    # MOM/VOL 계산 (Stage2 로직 재사용)
    end = biz_date
    start = (pd.to_datetime(end) - pd.Timedelta(days=score_params.lookback_calendar_days)).strftime("%Y%m%d")
    start = get_biz_date(start)

    moms: Dict[str, float] = {}
    vols: Dict[str, float] = {}

    def do(t):
        ohlcv = get_ohlcv_cached(t, start, end)
        if ohlcv is None or ohlcv.empty or "종가" not in ohlcv.columns:
            return
        
        close = ohlcv["종가"].astype(float)

        if len(close) > score_params.mom_days:
            moms[t] = float(close.iloc[-1] / close.iloc[-score_params.mom_days] - 1.0)

        if len(close) > score_params.vol_days:
            ret = close.pct_change().dropna().tail(score_params.vol_days)
            if len(ret) >= 10:
                vols[t] = float(ret.std())

    if len(tickers) == 1:
        do(tickers[0])
    else:
        for t in tqdm(tickers, desc="UserInterest MOM/VOL 계산", unit="ticker"):
            do(t)

    base["MOM"] = base["TICKER"].map(moms)
    base["VOL"] = base["TICKER"].map(vols)

    # 스코어 관련 컬럼은 None (추후 원하면 score_and_rank와 동일한 방식으로 계산 가능)
    for c in ["SCORE", "S_PBR", "S_PER", "S_DIV", "S_MOM", "S_VOL"]:
        base[c] = np.nan

    return base


# =========================
# Config / Cache
# =========================

CACHE_DIR = "./pykrx_cache"
os.makedirs(CACHE_DIR, exist_ok=True)


RESULT_DIR = "./result"
os.makedirs(RESULT_DIR, exist_ok=True)


def get_today_date_string() -> str:
    return datetime.now().strftime("%Y%m%d")


@lru_cache(maxsize=1)
def _kospi_set():
    return set(stock.get_market_ticker_list(market="KOSPI"))

@lru_cache(maxsize=1)
def _kosdaq_set():
    return set(stock.get_market_ticker_list(market="KOSDAQ"))

def get_listing_market(ticker: str) -> str:
    if ticker in _kospi_set():
        return "KOSPI"
    if ticker in _kosdaq_set():
        return "KOSDAQ"
    return "UNKNOWN"  # 스팩/상폐/우선주/기타 예외 대비

def get_biz_date(date_yyyymmdd: str) -> str:
    return stock.get_nearest_business_day_in_a_week(date_yyyymmdd)


def _parquet_path(prefix: str, *parts: str) -> str:
    safe = "_".join(str(p) for p in parts)
    return os.path.join(CACHE_DIR, f"{prefix}_{safe}.parquet")


def _load_parquet(path: str) -> Optional[pd.DataFrame]:
    if os.path.exists(path):
        try:
            return pd.read_parquet(path)
        except Exception:
            return None
    return None


def _save_parquet(df: pd.DataFrame, path: str) -> None:
    tmp = path + ".tmp"
    df.to_parquet(tmp, index=True)
    os.replace(tmp, path)


def _fetch_with_retry(fetch_fn, retries: int = 3, base_sleep: float = 0.7):
    last = None
    for i in range(retries):
        try:
            return fetch_fn()
        except Exception as e:
            last = e
            time.sleep(base_sleep * (i + 1))
    raise last


def _cached_df(
    prefix: str,
    parts: Tuple[Any, ...],
    fetch_fn,
    sleep_s: float = 0.12,
) -> pd.DataFrame:
    """
    범용 parquet 캐시 래퍼.
    - parts로 캐시 키 구성 (ticker/start/end/on 등 포함)
    - cache hit면 네트워크 호출 0
    """
    path = _parquet_path(prefix, *[str(p) for p in parts])
    cached = _load_parquet(path)
    if cached is not None:
        return cached

    time.sleep(sleep_s)
    df = _fetch_with_retry(fetch_fn)
    if isinstance(df, pd.DataFrame):
        _save_parquet(df, path)
    return df


# =========================
# Helpers
# =========================

def _winsorize(s: pd.Series, lower_q=0.01, upper_q=0.99) -> pd.Series:
    lo = s.quantile(lower_q)
    hi = s.quantile(upper_q)
    return s.clip(lower=lo, upper=hi)


def _pct_rank(s: pd.Series, high_is_good: bool) -> pd.Series:
    r = s.rank(pct=True, ascending=not high_is_good)
    return r.astype(float)


def _safe_float(x):
    try:
        if x is None:
            return None
        if isinstance(x, float) and (np.isnan(x) or np.isinf(x)):
            return None
        return float(x)
    except Exception:
        return None


def _safe_int(x):
    try:
        if x is None:
            return None
        if isinstance(x, float) and (np.isnan(x) or np.isinf(x)):
            return None
        return int(x)
    except Exception:
        return None


def _calc_return_by_trading_days(close: pd.Series, n: int) -> Optional[float]:
    if close is None or close.empty or len(close) <= n:
        return None
    return float(close.iloc[-1] / close.iloc[-n] - 1.0)


def _pick_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    if df is None or df.empty:
        return None
    for c in candidates:
        if c in df.columns:
            return c
    return None


# =========================
# Params
# =========================

@dataclass
class UniverseParams:
    date: str
    market: str = "ALL"
    mcap_min_krw: int = 50_000_000_000
    value_traded_min_krw: int = 300_000_000
    top_k_stage2: int = 400


@dataclass
class ScoreParams:
    lookback_calendar_days: int = 260
    mom_days: int = 120
    vol_days: int = 60

    w_pbr: float = 0.25
    w_per: float = 0.25
    w_div: float = 0.20
    w_mom: float = 0.20
    w_vol: float = 0.10

    exploration: int = 3
    winsorize: bool = True


@dataclass
class EnrichParams:
    """
    JSON enrich에 필요한 추가 파라미터
    """
    price_lookback_calendar_days: int = 420   # 12m(252영업일) 수익률 계산 여유
    history_days: int = 60                   # 최근 N영업일 히스토리
    investor_lookback_calendar_days: int = 14 # 투자자 수급을 위해 캘린더 14일치(영업일 ~10일)
    investor_last_n: int = 5                 # 최근 N영업일 요약


# =========================
# Data fetchers (cached)
# =========================

def get_ohlcv_cached(ticker: str, start: str, end: str, sleep_s: float = 0.12) -> pd.DataFrame:
    return _cached_df(
        prefix="ohlcv",
        parts=(ticker, start, end),
        fetch_fn=lambda: stock.get_market_ohlcv_by_date(start, end, ticker),
        sleep_s=sleep_s,
    )


def get_trading_value_by_date_cached(ticker: str, start: str, end: str, on: Optional[str], sleep_s: float = 0.12) -> pd.DataFrame:
    # on: None(순매수) / '매수' / '매도'
    on_key = "NET" if on is None else str(on)
    return _cached_df(
        prefix="trading_value_by_date",
        parts=(ticker, start, end, on_key),
        fetch_fn=lambda: stock.get_market_trading_value_by_date(start, end, ticker, on=on),
        sleep_s=sleep_s,
    )


def get_trading_volume_by_date_cached(ticker: str, start: str, end: str, on: Optional[str], sleep_s: float = 0.12) -> pd.DataFrame:
    on_key = "NET" if on is None else str(on)
    return _cached_df(
        prefix="trading_volume_by_date",
        parts=(ticker, start, end, on_key),
        fetch_fn=lambda: stock.get_market_trading_volume_by_date(start, end, ticker, on=on),
        sleep_s=sleep_s,
    )


# =========================
# Stage 1: Universe
# =========================

def build_universe(u: UniverseParams, biz_date: str) -> Tuple[str, pd.DataFrame]:

    f_path = _parquet_path("fundamental", u.market, biz_date)
    c_path = _parquet_path("cap", u.market, biz_date)

    f = _load_parquet(f_path)
    if f is None:
        f = _fetch_with_retry(lambda: stock.get_market_fundamental_by_ticker(biz_date, market=u.market))
        f = f.rename(columns={"DIV": "DIV_YIELD"}).replace([np.inf, -np.inf], pd.NA)
        _save_parquet(f, f_path)

    cap = _load_parquet(c_path)
    if cap is None:
        cap = _fetch_with_retry(lambda: stock.get_market_cap_by_ticker(biz_date, market=u.market))
        cap = cap.rename(columns={"시가총액": "MCAP", "거래대금": "VALUE_TRADED"}).replace([np.inf, -np.inf], pd.NA)
        _save_parquet(cap, c_path)

    df = f.join(cap[["MCAP", "VALUE_TRADED"]], how="inner")

    df = df[
        (df["PBR"].notna()) & (df["PBR"] > 0) &
        (df["PER"].notna()) & (df["PER"] > 0) &
        (df["DIV_YIELD"].notna()) & (df["DIV_YIELD"] >= 0) &
        (df["MCAP"].notna()) & (df["MCAP"] > 0) &
        (df["VALUE_TRADED"].notna()) & (df["VALUE_TRADED"] > 0)
    ].copy()

    df = df[(df["MCAP"] >= u.mcap_min_krw) & (df["VALUE_TRADED"] >= u.value_traded_min_krw)].copy()

    df["NAME"] = [stock.get_market_ticker_name(t) for t in df.index]
    df.insert(0, "TICKER", df.index)

    df["_rough"] = (1 - df["PBR"].rank(pct=True)) + (1 - df["PER"].rank(pct=True)) + (df["DIV_YIELD"].rank(pct=True))
    print(f"[Stage1] universe size (after basic filters): {len(df)}")

    df = (
        df.sort_values("_rough", ascending=False)
          .head(u.top_k_stage2)
          .drop(columns=["_rough"])
          .reset_index(drop=True)
    )

    return df


# =========================
# Stage 2: MOM/VOL for candidates (cached OHLCV)
# =========================

def add_mom_vol(biz_date: str, df: pd.DataFrame, s: ScoreParams) -> pd.DataFrame:
    end = biz_date
    start = (pd.to_datetime(end) - pd.Timedelta(days=s.lookback_calendar_days)).strftime("%Y%m%d")
    start = get_biz_date(start)

    moms: Dict[str, float] = {}
    vols: Dict[str, float] = {}

    tickers = df["TICKER"].tolist()
    skipped_empty = 0
    skipped_short = 0

    for t in tqdm(tickers, desc="Stage2 MOM/VOL 계산", unit="ticker"):
        ohlcv = get_ohlcv_cached(t, start, end)
        if ohlcv is None or ohlcv.empty or "종가" not in ohlcv.columns:
            skipped_empty += 1
            continue

        close = ohlcv["종가"].astype(float)

        if len(close) > s.mom_days:
            moms[t] = float(close.iloc[-1] / close.iloc[-s.mom_days] - 1.0)
        else:
            skipped_short += 1

        if len(close) > s.vol_days:
            ret = close.pct_change().dropna().tail(s.vol_days)
            if len(ret) >= 10:
                vols[t] = float(ret.std())

    print(f"[Stage2] tickers={len(tickers)} empty/invalid={skipped_empty} too_short={skipped_short}")

    out = df.copy()
    out["MOM"] = out["TICKER"].map(moms)
    out["VOL"] = out["TICKER"].map(vols)
    return out


# =========================
# Scoring + Top-N
# =========================

def score_and_rank(df: pd.DataFrame, s: ScoreParams, tracking_tickers: List[str], visited_tickers: List[str]) -> pd.DataFrame:
    df = df.dropna(subset=["PBR", "PER", "DIV_YIELD", "MOM", "VOL"]).copy()
    if df.empty:
        return df

    work = df.copy()
    if s.winsorize:
        for col in ["PBR", "PER", "DIV_YIELD", "MOM", "VOL"]:
            work[col] = _winsorize(work[col])

    work["S_PBR"] = _pct_rank(work["PBR"], high_is_good=False)
    work["S_PER"] = _pct_rank(work["PER"], high_is_good=False)
    work["S_DIV"] = _pct_rank(work["DIV_YIELD"], high_is_good=True)
    work["S_MOM"] = _pct_rank(work["MOM"], high_is_good=True)
    work["S_VOL"] = _pct_rank(work["VOL"], high_is_good=False)

    work["SCORE"] = (
        s.w_pbr * work["S_PBR"] +
        s.w_per * work["S_PER"] +
        s.w_div * work["S_DIV"] +
        s.w_mom * work["S_MOM"] +
        s.w_vol * work["S_VOL"]
    )

    cols = [
        "TICKER", "NAME", "SCORE",
        "PBR", "PER", "DIV_YIELD", "MCAP", "VALUE_TRADED",
        "MOM", "VOL",
        "S_PBR", "S_PER", "S_DIV", "S_MOM", "S_VOL",
    ]

    tracking_set = set(tracking_tickers or [])
    visited_set = set(visited_tickers or [])

    # 1) tracking_tickers에 속한 아이템들 (있으면 전부 포함)
    tracked = work[work["TICKER"].isin(tracking_set)].copy()

    # 2) visited_tickers에 속하지 않은 아이템들 중 SCORE 상위 exploration개 (tracking은 중복 방지 위해 제외)
    exploration_n = int(getattr(s, "exploration", 0) or 0)
    explore_pool = work[~work["TICKER"].isin(visited_set) & ~work["TICKER"].isin(tracking_set)].copy()
    explored = (
        explore_pool.sort_values("SCORE", ascending=False)
        .head(exploration_n)
    )

    # 3) 하나의 테이블로 합치기 (중복 방지 후 SCORE 내림차순 정렬)
    out = pd.concat([tracked, explored], ignore_index=True)
    if out.empty:
        return out

    out = (
        out.drop_duplicates(subset=["TICKER"], keep="first")
           .sort_values("SCORE", ascending=False)
           .loc[:, cols]
           .reset_index(drop=True)
    )
    return out


def screen_institutional_value(
    biz_date: str,
    universe_params: UniverseParams,
    score_params: ScoreParams,
    tracking_tickers: List[str],
    visited_tickers: List[str]
):
    uni = build_universe(universe_params, biz_date)
    enriched = add_mom_vol(biz_date, uni, score_params)
    ranked = score_and_rank(enriched, score_params, tracking_tickers, visited_tickers)
    return ranked


# =========================
# Enrich: price/volume + investor flow (Top-N only)
# =========================

def get_price_volume_features_for_ticker(
    ticker: str,
    biz_date: str,
    lookback_calendar_days: int,
    history_days: int,
) -> Dict[str, Any]:
    end = biz_date
    start = (pd.to_datetime(end) - pd.Timedelta(days=lookback_calendar_days)).strftime("%Y%m%d")
    start = get_biz_date(start)

    ohlcv = get_ohlcv_cached(ticker, start, end)
    if ohlcv is None or ohlcv.empty or "종가" not in ohlcv.columns:
        return {}

    close = ohlcv["종가"].astype(float)
    vol = ohlcv["거래량"] if "거래량" in ohlcv.columns else None
    val = ohlcv["거래대금"] if "거래대금" in ohlcv.columns else None
    if val is None and ("종가" in ohlcv.columns and "거래량" in ohlcv.columns):
        val = (ohlcv["종가"].astype(float) * ohlcv["거래량"].astype(float))
    # returns

    def frac_to_pct(x):
        return None if x is None else float(x * 100.0)

    change_1m = _calc_return_by_trading_days(close, 21)
    change_3m = _calc_return_by_trading_days(close, 63)
    change_6m = _calc_return_by_trading_days(close, 126)
    change_12m = _calc_return_by_trading_days(close, 252)

    price = {
        "current": _safe_float(close.iloc[-1]),
        "unit": "KRW_per_share",
        "change_1m": change_1m,
        "change_1m_percent": frac_to_pct(change_1m),
        "change_3m": change_3m,
        "change_3m_percent": frac_to_pct(change_3m),
        "change_6m": change_6m,
        "change_6m_percent": frac_to_pct(change_6m),
        "change_12m": change_12m,
        "change_12m_percent": frac_to_pct(change_12m),
    }

    volume = {
        "avg_volume_20d": _safe_float(vol.tail(20).mean()) if vol is not None and len(vol) >= 20 else None,
        "avg_value_traded_20d": _safe_float(val.tail(20).mean()) if val is not None and len(val) >= 20 else None,
    }

    hist = []
    tail = ohlcv.tail(history_days)

    val_tail = val.tail(history_days) if val is not None else None

    for (dt, row), v in zip(tail.iterrows(), (val_tail.tolist() if val_tail is not None else [None]*len(tail))):
        hist.append({
            "date": pd.to_datetime(dt).strftime("%Y%m%d"),
            "close": _safe_float(row.get("종가")),
            "volume": _safe_int(row.get("거래량")) if "거래량" in tail.columns else None,
            "value_traded": _safe_int(v),
        })

    return {"price": price, "volume": volume, "recent_history": hist}


def get_investor_flow_features_for_ticker(
    ticker: str,
    biz_date: str,
    lookback_calendar_days: int,
    last_n: int,
    on: Optional[str] = None,  # None=순매수
) -> Dict[str, Any]:
    end = biz_date
    start = (pd.to_datetime(end) - pd.Timedelta(days=lookback_calendar_days)).strftime("%Y%m%d")
    start = get_biz_date(start)

    value_df = get_trading_value_by_date_cached(ticker, start, end, on=on)
    volume_df = get_trading_volume_by_date_cached(ticker, start, end, on=on)

    if isinstance(value_df, pd.DataFrame) and not value_df.empty:
        value_df = value_df.sort_index()
    if isinstance(volume_df, pd.DataFrame) and not volume_df.empty:
        volume_df = volume_df.sort_index()

    # 컬럼 후보 (환경에 따라 '기관합계' 대신 '기관'일 수 있어 후보를 둠)
    indiv_c = _pick_col(value_df, ["개인"])
    inst_c = _pick_col(value_df, ["기관합계", "기관"])
    forg_c = _pick_col(value_df, ["외국인합계", "외국인"])

    def sum_last_n(df: pd.DataFrame, col: Optional[str]) -> Optional[float]:
        if df is None or df.empty or col is None or col not in df.columns:
            return None
        return _safe_float(df[col].tail(last_n).sum())

    summary = {
        "window_last_n_days": last_n,
        "on": ("NET" if on is None else str(on)),
        "columns_value": list(value_df.columns) if isinstance(value_df, pd.DataFrame) else [],
        "columns_volume": list(volume_df.columns) if isinstance(volume_df, pd.DataFrame) else [],
        "net_value_sum_last_n": {
            "individual": sum_last_n(value_df, indiv_c),
            "institution": sum_last_n(value_df, inst_c),
            "foreign": sum_last_n(value_df, forg_c),
        },
        "timeseries_value": [],
        "timeseries_volume": [],
    }

    if isinstance(value_df, pd.DataFrame) and not value_df.empty:
        tail = value_df.tail(last_n)
        for dt, row in tail.iterrows():
            summary["timeseries_value"].append({
                "date": pd.to_datetime(dt).strftime("%Y%m%d"),
                **{k: _safe_float(row.get(k)) for k in tail.columns}
            })

    if isinstance(volume_df, pd.DataFrame) and not volume_df.empty:
        tail = volume_df.tail(last_n)
        for dt, row in tail.iterrows():
            summary["timeseries_volume"].append({
                "date": pd.to_datetime(dt).strftime("%Y%m%d"),
                **{k: _safe_float(row.get(k)) for k in tail.columns}
            })

    return summary


# =========================
# Final: Quant JSON payload (pre-OpenDART)
# =========================

def build_batch(
    ranked_df: pd.DataFrame,
    biz_date: str,
    market: str,
    universe_params: UniverseParams,
    score_params: ScoreParams,
    enrich_params: EnrichParams
) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []

    def do(idx, row):
        item = build_quant_payload_pre_dart(
            int(idx + 1),
            row,
            biz_date,
            market,
            universe_params,
            score_params,
            enrich_params)
        items.append(item)

    if len(ranked_df) == 1:
        for _, row in ranked_df.iterrows():
            do(-1, row)
    else:
        for idx, row in tqdm(ranked_df.iterrows(), total=len(ranked_df), desc="Build quant JSON", unit="ticker"):
            do(idx, row)
            
    payload = {
        "asof": biz_date,
        "market": market,
        "top_n": len(ranked_df),
        "items": items,
    }
    return payload


def build_quant_payload_pre_dart(
    rank: int,
    item: pd.Series,
    biz_date: str,
    market: str,
    universe_params: UniverseParams,
    score_params: ScoreParams,
    enrich_params: EnrichParams
) -> Dict[str, Any]:
    stage2_universe_size = _safe_int(universe_params.top_k_stage2)
    
    ticker = str(item["TICKER"])
    listing_market = get_listing_market(ticker)

    pv = get_price_volume_features_for_ticker(
        ticker=ticker,
        biz_date=biz_date,
        lookback_calendar_days=enrich_params.price_lookback_calendar_days,
        history_days=enrich_params.history_days,
    )

    flow = get_investor_flow_features_for_ticker(
        ticker=ticker,
        biz_date=biz_date,
        lookback_calendar_days=enrich_params.investor_lookback_calendar_days,
        last_n=enrich_params.investor_last_n,
        on=None,  # 순매수 기준
    )

    item = {
        "rank": rank,
        "ticker": ticker,
        "name": str(item["NAME"]),
        "market": market,
        "listing_market": listing_market,
        "analysis_date": biz_date,

        "valuation": {
            "PBR": _safe_float(item.get("PBR")),
            "PER": _safe_float(item.get("PER")),
            "DIV_YIELD": _safe_float(item.get("DIV_YIELD")),
            "MCAP": _safe_int(item.get("MCAP")),
        },

        "liquidity": {
            "value_traded_today": _safe_int(item.get("VALUE_TRADED")),
            "avg_value_traded_20d": pv.get("volume", {}).get("avg_value_traded_20d"),
        },

        "volume": {
            "avg_volume_20d": pv.get("volume", {}).get("avg_volume_20d"),
        },

        "price": pv.get("price", {}),

        "recent_history": pv.get("recent_history", []),

        "momentum": {
            "mom": _safe_float(item.get("MOM")),
            "volatility": _safe_float(item.get("VOL")),
        },

        "investor_flow": flow,

        "score": {
            "total": _safe_float(item.get("SCORE")),
            "components": {
                "S_PBR": _safe_float(item.get("S_PBR")),
                "S_PER": _safe_float(item.get("S_PER")),
                "S_DIV": _safe_float(item.get("S_DIV")),
                "S_MOM": _safe_float(item.get("S_MOM")),
                "S_VOL": _safe_float(item.get("S_VOL")),
            },
            "weights": {
                "w_pbr": score_params.w_pbr,
                "w_per": score_params.w_per,
                "w_div": score_params.w_div,
                "w_mom": score_params.w_mom,
                "w_vol": score_params.w_vol,
            },
        },

        "context": {
            "stage2_universe_size": stage2_universe_size,
            "lookback_calendar_days": score_params.lookback_calendar_days,
            "mom_days": score_params.mom_days,
            "vol_days": score_params.vol_days,
            "winsorize": score_params.winsorize,
            "filters": {
                "mcap_min_krw": universe_params.mcap_min_krw,
                "value_traded_min_krw": universe_params.value_traded_min_krw,
                "top_k_stage2": universe_params.top_k_stage2,
            },
        },
    }

    return item


def save_json(payload: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# =========================
# Example run
# =========================


class Screener:
    def __init__(self, date_str: Optional[str] = None, result_dir: Optional[str] = None):
        if date_str is None:
            date_str = get_today_date_string()
        self.date_str = date_str
        self.biz_date = get_biz_date(date_str)
        self.enrich = EnrichParams(
            price_lookback_calendar_days=420,
            history_days=60,
            investor_lookback_calendar_days=14,
            investor_last_n=5,
        )
        if result_dir is None:
            result_dir = RESULT_DIR
        self.result_dir = result_dir
    
    def get_result_dir(self):
        return os.path.join(self.result_dir, "screener", self.biz_date)

    def get_result_file_path(self, ticker: str):
        return os.path.join(self.get_result_dir(), f"{ticker}.json")

    def screen(self, tracking_tickers: List[str], visited_tickers: List[str], exploration: int):

        u = UniverseParams(date=self.biz_date, market="ALL")
        s = ScoreParams(exploration=exploration)

        ranked = screen_institutional_value(self.biz_date, u, s, tracking_tickers, visited_tickers)

        enrich = EnrichParams(
            price_lookback_calendar_days=420,
            history_days=60,
            investor_lookback_calendar_days=14,
            investor_last_n=5,
        )

        payload = build_batch(
            ranked_df=ranked,
            biz_date=self.biz_date,
            market="ALL",
            universe_params=u,
            score_params=s,
            enrich_params=enrich
        )

        dir_path = self.get_result_dir()
        os.makedirs(dir_path, exist_ok=True)
        items = payload["items"]
        for i in range(len(items)):
            ticker = items[i]["ticker"]
            out_path = self.get_result_file_path(ticker)
            save_json(items[i], out_path)
        return items

    def build_target_quant(self, ticker: str):
        u = UniverseParams(date=self.biz_date, market="ALL")
        s = ScoreParams()
        user_df = build_user_interest_df(
            tickers=[ticker],
            biz_date=self.biz_date,
            market="ALL",
            score_params=s,
        )
        payload = build_batch(
            ranked_df=user_df,
            biz_date=self.biz_date,
            market="ALL",
            universe_params=u,
            score_params=s,
            enrich_params=self.enrich)
        item = payload["items"][0]
        dir_path = self.get_result_dir()
        os.makedirs(dir_path, exist_ok=True)
        out_path = self.get_result_file_path(ticker)
        save_json(item, out_path)
        return item
