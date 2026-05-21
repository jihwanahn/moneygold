"""백테스트 엔진 — 워치리스트 forward return 검증.

목적: 우리 엔진(Stage + Template + ...)이 골라낸 BUY 후보 풀이 *실제로* 시장보다
잘 가는가? 과거 N개 시점에 워치리스트를 재현하고, 각 종목의 +5d/+10d/+20d 수익률을
계산해 factor (Stage / RS / growth_q / 거래량 수급 / 순익률 등) 별로 평균을 본다.

특성:
  - **재현 가능**. signals.generate_signals 를 그대로 재사용 (asof만 바꿔 호출) 이라
    대시보드의 현재 화면과 동일 로직.
  - **Stride 샘플링**. 매일 돌리는 대신 weekly(기본 5영업일) 또는 사용자 설정.
    3개월 ≈ 12 snapshots, ~10s/snapshot → 합리적 시간.
  - **Forward return = (close[asof + N영업일] / close[asof] - 1) × 100**.
    asof + N일이 데이터 범위 밖이면 NaN.
  - **시장 비교** 옵션: 같은 기간 KOSPI/KOSDAQ/SP500 수익률과 alpha 산출.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from . import consensus as cons
from . import fundamentals as fund
from . import indicators as ind
from . import signals as sg
from .config import AppConfig
from .data import store

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """forward return 분석을 위한 long-format DataFrame.

    Columns:
      asof              YYYYMMDD
      ticker, name, market
      rs_rank, rs_momentum, stage
      growth_quarters, op_growth_quarters
      op_margin, net_margin
      revenue_yoy, op_income_yoy
      vol_acc_ratio
      accelerating
      cons_target_upside_pct, cons_eps_net_revisions_30d
      fwd_Nd          forward return (%) — 각 horizon 별 컬럼
      close_at_asof
    """
    entries: pd.DataFrame
    horizons: tuple[int, ...]
    asofs: list[str]
    market_returns: dict[str, pd.DataFrame]   # market -> DataFrame[asof, fwd_5d, ...] (시장 지수 수익률)


def _business_days_between(start: str, end: str, stride: int = 5) -> list[str]:
    """YYYYMMDD 두 날짜 사이의 영업일 (월~금) 중 stride 간격으로 샘플."""
    sd = datetime.strptime(start, "%Y%m%d").date()
    ed = datetime.strptime(end, "%Y%m%d").date()
    out: list[str] = []
    cur = sd
    step = 0
    while cur <= ed:
        if cur.weekday() < 5:   # Mon~Fri
            if step % stride == 0:
                out.append(cur.strftime("%Y%m%d"))
            step += 1
        cur += timedelta(days=1)
    return out


def _forward_return(close_series: pd.Series, dates: pd.Series, asof: str, horizon: int) -> float:
    """asof 시점 종가 → asof + horizon 영업일 후 종가의 수익률 (%)."""
    idx_at = dates[dates <= asof].index
    if len(idx_at) == 0:
        return float("nan")
    i0 = idx_at[-1]
    i1 = i0 + horizon
    if i1 >= len(close_series):
        return float("nan")
    c0 = float(close_series.iloc[i0])
    c1 = float(close_series.iloc[i1])
    if c0 <= 0:
        return float("nan")
    return (c1 / c0 - 1.0) * 100.0


def run_backtest(
    data_dir: Path,
    cfg: AppConfig,
    start_date: str,
    end_date: str,
    *,
    stride_days: int = 5,
    horizons: tuple[int, ...] = (5, 10, 20),
    allowed_stages: tuple[int, ...] = (2,),
    required_template_conditions: tuple[int, ...] = tuple(range(1, 9)),
    progress_callback=None,
) -> BacktestResult:
    """기간 내 stride_days 간격으로 워치리스트 + forward return 시뮬레이션.

    Parameters
    ----------
    progress_callback : function(i, total, asof) — Streamlit progress bar 용 (선택)
    """
    asofs = _business_days_between(start_date, end_date, stride_days)
    if not asofs:
        return BacktestResult(entries=pd.DataFrame(), horizons=horizons, asofs=[], market_returns={})

    # 마스터 + 지수 한 번만 로드 (asof 별로 클립)
    master = pd.read_parquet(data_dir / "meta" / "master.parquet")
    indices: dict[str, pd.Series] = {}
    for code, mkt in [("KOSPI200", "KOSPI"), ("KOSDAQ150", "KOSDAQ"), ("^GSPC", "US")]:
        p = store.index_path(data_dir, code)
        if p.exists():
            df = pd.read_parquet(p)
            if not df.empty and "close" in df.columns:
                indices[mkt] = df.set_index("date")["close"].astype(float)

    # 시장별 forward return 계산 (시장 지수)
    market_returns: dict[str, pd.DataFrame] = {}
    for mkt, ser in indices.items():
        idx_df = ser.reset_index().rename(columns={"index": "date"}) if "date" not in ser.index.names else None
        # ser는 date(YYYYMMDD) → close
        dates_idx = ser.index.to_series().reset_index(drop=True)
        close_idx = ser.reset_index(drop=True)
        rows = []
        for asof in asofs:
            row = {"asof": asof}
            for h in horizons:
                row[f"fwd_{h}d"] = _forward_return(close_idx, dates_idx, asof, h)
            rows.append(row)
        market_returns[mkt] = pd.DataFrame(rows)

    # 펀더멘털 / 컨센서스 캐시 (모든 종목 사전 로드 — asof와 무관)
    fundamentals_map: dict[str, fund.FundamentalsResult] = {}
    for t in master["ticker"]:
        p = fund.financials_path(data_dir, t)
        if p.exists():
            q = store.read_parquet_safe(p)
            if q is not None and not q.empty:
                fundamentals_map[t] = fund.build_fundamentals_from_cache(q)

    consensus_map: dict[str, cons.ConsensusResult] = {}
    import json
    for t in master["ticker"]:
        p = cons.consensus_path(data_dir, t)
        if p.exists():
            try:
                consensus_map[t] = cons.from_dict(json.loads(p.read_text()))
            except Exception:
                pass

    # 종목별 bars (전 기간)를 한 번에 로드
    bars_by_ticker: dict[str, pd.DataFrame] = {}
    for t in master["ticker"]:
        bp = store.bars_path(data_dir, t)
        if not bp.exists():
            continue
        df = pd.read_parquet(bp)
        if df is None or df.empty or "close" not in df.columns:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        bars_by_ticker[t] = df
    log.info("Loaded bars for %d tickers", len(bars_by_ticker))

    has_real_mcap = "mcap" in master.columns
    mcap_map = dict(zip(master["ticker"], master["mcap"])) if has_real_mcap else {}

    all_entries: list[pd.DataFrame] = []

    for i, asof in enumerate(asofs):
        if progress_callback:
            progress_callback(i, len(asofs), asof)

        # asof 기준 종목 데이터 준비
        tickers: list[sg.TickerData] = []
        rs_rows = []
        for t in master["ticker"]:
            bars = bars_by_ticker.get(t)
            if bars is None:
                continue
            bars_cut = bars[bars["date"] <= asof].reset_index(drop=True)
            if len(bars_cut) < 253:
                continue
            if has_real_mcap and mcap_map.get(t, 0) > 0:
                mcap = float(mcap_map[t])
            else:
                avg_value_20 = float(bars_cut["value"].tail(20).mean()) if "value" in bars_cut.columns else 0.0
                mcap = avg_value_20 * 50
            row = master[master["ticker"] == t].iloc[0]
            tickers.append(sg.TickerData(
                ticker=t, name=row["name"], market=row["market"],
                bars=bars_cut, mcap=mcap, flagged=False,
            ))
            rs_rows.append({
                "ticker": t, "market": row["market"],
                "rs_mom": ind.rs_momentum(bars_cut["close"].astype(float)),
            })

        if not tickers:
            continue

        rs_df = pd.DataFrame(rs_rows)
        rs_df["rs_rank"] = float("nan")
        for _mkt, g in rs_df.groupby("market"):
            rs_df.loc[g.index, "rs_rank"] = ind.rs_rank(g["rs_mom"]).values
        rs_rank_map = dict(zip(rs_df["ticker"], rs_df["rs_rank"]))
        rs_mom_map = dict(zip(rs_df["ticker"], rs_df["rs_mom"]))

        # asof 기준 지수 (date <= asof)
        idx_cut = {mkt: ser[ser.index <= asof] for mkt, ser in indices.items()}

        sigs = sg.generate_signals(
            asof, tickers, {}, rs_rank_map, idx_cut, cfg,
            rs_momentum_map=rs_mom_map,
            fundamentals_map=fundamentals_map,
            consensus_map=consensus_map,
            allowed_stages=allowed_stages,
            required_template_conditions=required_template_conditions,
        )

        if not sigs.watchlist:
            continue

        # 워치리스트를 DataFrame으로
        entries = pd.DataFrame([w.__dict__ for w in sigs.watchlist])
        entries["asof"] = asof
        entries["close_at_asof"] = entries["close"]

        # forward returns
        for h in horizons:
            col = f"fwd_{h}d"
            vals = []
            for t in entries["ticker"]:
                b = bars_by_ticker.get(t)
                if b is None:
                    vals.append(float("nan"))
                    continue
                vals.append(_forward_return(
                    b["close"].astype(float), b["date"].astype(str), asof, h,
                ))
            entries[col] = vals

        all_entries.append(entries)

    if not all_entries:
        return BacktestResult(entries=pd.DataFrame(), horizons=horizons, asofs=asofs, market_returns=market_returns)

    combined = pd.concat(all_entries, ignore_index=True)
    keep_cols = [
        "asof", "ticker", "name", "market",
        "rs_rank", "rs_momentum", "stage",
        "growth_quarters", "op_growth_quarters",
        "op_margin", "net_margin", "revenue_yoy", "op_income_yoy",
        "vol_acc_ratio", "accelerating",
        "cons_target_upside_pct", "cons_eps_net_revisions_30d",
        "close_at_asof",
    ] + [f"fwd_{h}d" for h in horizons]
    keep_cols = [c for c in keep_cols if c in combined.columns]
    return BacktestResult(
        entries=combined[keep_cols],
        horizons=horizons,
        asofs=asofs,
        market_returns=market_returns,
    )


# ============================================================
# Factor analysis helpers
# ============================================================

def factor_summary(
    entries: pd.DataFrame,
    factor_col: str,
    horizons: tuple[int, ...] = (5, 10, 20),
    *,
    bins: list[float] | None = None,
    bin_labels: list[str] | None = None,
) -> pd.DataFrame:
    """factor_col 값을 구간으로 나눠 forward return 평균/중간값/적중률 집계.

    bins 미지정 시 categorical 데이터는 그대로 그룹화.
    """
    if entries.empty or factor_col not in entries.columns:
        return pd.DataFrame()

    df = entries.copy()
    if bins is not None:
        df["_bucket"] = pd.cut(df[factor_col], bins=bins, labels=bin_labels, include_lowest=True)
    else:
        df["_bucket"] = df[factor_col]

    rows = []
    for bucket, g in df.groupby("_bucket", observed=True):
        row = {"bucket": str(bucket), "n": len(g)}
        for h in horizons:
            col = f"fwd_{h}d"
            if col not in g.columns:
                continue
            vals = g[col].dropna()
            row[f"mean_{h}d"] = float(vals.mean()) if len(vals) else float("nan")
            row[f"median_{h}d"] = float(vals.median()) if len(vals) else float("nan")
            row[f"hit_{h}d"] = float((vals > 0).mean() * 100.0) if len(vals) else float("nan")
        rows.append(row)
    return pd.DataFrame(rows)
