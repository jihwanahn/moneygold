"""백테스트 — 워크포워드 일별 시뮬.

ARCHITECTURE.md §10 참조.

핵심 원칙:
  - signals.py를 그대로 재사용 (라이브와 같은 코드 경로)
  - SELL 먼저 → BUY 처리
  - 진입가 = 다음 영업일 시가 + 슬리피지
  - 청산가:
      STOP_HIT: min(stop, 다음날 시가) — 갭다운 시 시가 청산
      그 외:    다음날 시가 (시장가 매도 가정)
  - 수수료 + 매도세
  - 생존편향: KIS 폐지 종목 미제공 → 백테스트 결과 헤더 경고

자동 주문 코드는 들어가지 않음 (시뮬만).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from tqdm import tqdm

from . import indicators as ind
from . import signals as sg

log = logging.getLogger(__name__)


# ============================================================
# Params
# ============================================================

@dataclass
class BacktestParams:
    start: str            # YYYYMMDD
    end: str              # YYYYMMDD
    initial_equity: float = 10_000_000.0
    slippage_bps: float = 20.0          # 0.20% 진입/청산 양쪽
    commission_bps: float = 1.5         # 0.015% 매수/매도 각각
    tax_sell_bps: float = 20.0          # 0.20% 매도세
    benchmark: str = "KOSPI"            # KOSPI / KOSDAQ / KOSPI200 / KOSDAQ150


# ============================================================
# Trade / Position
# ============================================================

@dataclass
class OpenPosition:
    ticker: str
    name: str
    market: str
    entry_date: str
    entry_price: float           # 슬리피지·수수료 *포함된 실제 평단*
    shares: int
    initial_stop: float
    current_stop: float
    box_top: float
    box_bottom: float


@dataclass
class Trade:
    ticker: str
    name: str
    market: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    shares: int
    initial_stop: float
    exit_reason: str
    pnl_krw: float
    pnl_pct: float
    r_multiple: float            # 손익 / 초기 리스크 (R 단위)
    days_held: int
    gap_down: bool = False


# ============================================================
# SimPortfolio
# ============================================================

class SimPortfolio:
    """백테스트 포지션 관리.

    수수료·슬리피지·세금은 open/close 시점에 가격에 직접 반영해서 cash를 차감/가산.
    """

    def __init__(self, initial_equity: float, params: BacktestParams):
        self.cash = float(initial_equity)
        self.initial_equity = float(initial_equity)
        self.positions: dict[str, OpenPosition] = {}
        self.closed_trades: list[Trade] = []
        self.params = params

    def can_afford(self, entry_price_eff: float, shares: int) -> bool:
        return self.cash >= entry_price_eff * shares

    def open(self, *, ticker: str, name: str, market: str, date: str,
             entry_open: float, stop: float, box_top: float, box_bottom: float,
             desired_shares: int) -> OpenPosition | None:
        """진입. 슬리피지·수수료 반영 후 자금 한도 내 실제 매수가능 주수만."""
        slip = entry_open * self.params.slippage_bps / 10000.0
        comm = entry_open * self.params.commission_bps / 10000.0
        eff_price = entry_open + slip + comm   # 실제 진입 단가 (체결 + 비용 부담)

        if eff_price <= 0 or desired_shares <= 0:
            return None
        # 자금 한도 반영
        max_by_cash = int(self.cash // eff_price)
        shares = min(desired_shares, max_by_cash)
        if shares <= 0:
            return None

        cost = eff_price * shares
        self.cash -= cost

        pos = OpenPosition(
            ticker=ticker, name=name, market=market,
            entry_date=date, entry_price=eff_price, shares=shares,
            initial_stop=float(stop), current_stop=float(stop),
            box_top=float(box_top), box_bottom=float(box_bottom),
        )
        self.positions[ticker] = pos
        return pos

    def close(self, *, ticker: str, date: str, exit_open: float, reason: str,
              forced_price: float | None = None, gap_down: bool = False) -> Trade | None:
        """청산. STOP_HIT일 때 forced_price=min(stop, open). 그 외엔 open 사용."""
        if ticker not in self.positions:
            return None
        pos = self.positions.pop(ticker)
        raw_exit = forced_price if forced_price is not None else exit_open
        slip = raw_exit * self.params.slippage_bps / 10000.0
        comm = raw_exit * self.params.commission_bps / 10000.0
        tax = raw_exit * self.params.tax_sell_bps / 10000.0
        eff_exit = raw_exit - slip - comm - tax
        if eff_exit < 0:
            eff_exit = 0.0

        proceeds = eff_exit * pos.shares
        self.cash += proceeds

        pnl_krw = (eff_exit - pos.entry_price) * pos.shares
        pnl_pct = (eff_exit / pos.entry_price - 1.0) * 100.0 if pos.entry_price > 0 else 0.0
        risk_per_share = pos.entry_price - pos.initial_stop
        r_mult = (eff_exit - pos.entry_price) / risk_per_share if risk_per_share > 0 else 0.0

        try:
            days = (pd.to_datetime(date) - pd.to_datetime(pos.entry_date)).days
        except Exception:
            days = 0

        tr = Trade(
            ticker=pos.ticker, name=pos.name, market=pos.market,
            entry_date=pos.entry_date, entry_price=pos.entry_price,
            exit_date=date, exit_price=eff_exit,
            shares=pos.shares, initial_stop=pos.initial_stop,
            exit_reason=reason, pnl_krw=pnl_krw, pnl_pct=pnl_pct,
            r_multiple=r_mult, days_held=days, gap_down=gap_down,
        )
        self.closed_trades.append(tr)
        return tr

    def equity(self, close_by_ticker: dict[str, float]) -> float:
        """현금 + 보유 종목 평가액 (종가 기준)."""
        eq = self.cash
        for tk, pos in self.positions.items():
            c = close_by_ticker.get(tk, pos.entry_price)
            eq += float(c) * pos.shares
        return eq

    def update_trail_stops(self, holds: list[sg.HoldSignal]) -> None:
        for h in holds:
            if h.ticker in self.positions and h.trail_updated:
                self.positions[h.ticker].current_stop = float(h.new_stop)
                # 박스 정보는 holds에 없음 — 현 박스 그대로 유지

    def to_portfolio_dict(self) -> dict[str, sg.PositionMeta]:
        """signals.generate_signals에 넘길 PositionMeta dict."""
        return {
            tk: sg.PositionMeta(
                ticker=tk,
                entry_date=p.entry_date,
                entry_price=p.entry_price,
                current_stop=p.current_stop,
                current_box_top=p.box_top,
                current_box_bottom=p.box_bottom,
            )
            for tk, p in self.positions.items()
        }


# ============================================================
# Walk-forward simulator
# ============================================================

@dataclass
class BacktestResult:
    params: BacktestParams
    equity_curve: pd.DataFrame     # columns: date, equity, n_positions, cash
    trades: list[Trade]
    benchmark_curve: pd.DataFrame  # columns: date, benchmark_equity
    stats: dict[str, Any]
    survivorship_warning: str = (
        "⚠️ KIS는 상장폐지 종목 시세를 제공하지 않습니다. "
        "본 백테스트는 현재 KIS에서 조회 가능한 종목만 포함하므로, "
        "결과는 생존편향에 의해 실제보다 우호적일 수 있습니다 (ARCHITECTURE §2 PR0 검증)."
    )


def run_backtest(
    params: BacktestParams,
    master: pd.DataFrame,
    bars_by_ticker: dict[str, pd.DataFrame],
    index_close_by_market: dict[str, pd.Series],
    cfg,                                          # AppConfig
    progress: bool = True,
) -> BacktestResult:
    """일별 워크포워드 시뮬.

    각 거래일에 대해:
      1) generate_signals 호출 (asof = biz_date)
      2) SELL 먼저: 다음 영업일 시가/스톱으로 청산
      3) BUY: 다음 영업일 시가로 진입
      4) HOLD: 트레일링 스톱 갱신
      5) 그 날 종가로 equity 평가
    """
    # 거래일 캘린더: 모든 종목 bars의 union date set
    all_dates: set[str] = set()
    for df in bars_by_ticker.values():
        all_dates.update(df["date"].astype(str).tolist())
    trading_dates = sorted(d for d in all_dates if params.start <= d <= params.end)
    if not trading_dates:
        raise ValueError(f"기간 {params.start}~{params.end} 내 거래일 없음")

    # 다음 날짜 인덱스
    date_to_idx = {d: i for i, d in enumerate(trading_dates)}

    # 시뮬 포트폴리오
    sim = SimPortfolio(params.initial_equity, params)

    # 벤치마크: 시작일 close 기준 정규화
    bench_series = index_close_by_market.get(params.benchmark)
    if bench_series is None:
        # KOSPI/KOSDAQ map은 KOSPI200/KOSDAQ150 키일 수 있음 — KOSPI 별도
        bench_series = index_close_by_market.get("KOSPI200") or index_close_by_market.get("KOSPI")
    bench_series = bench_series.sort_index() if bench_series is not None else pd.Series(dtype=float)

    bench_curve_rows = []
    equity_rows = []

    # bars 인덱스 캐시: ticker → date → row
    bars_lookup = {tk: df.set_index("date") for tk, df in bars_by_ticker.items()}

    it = tqdm(trading_dates, desc="backtest", unit="d") if progress else trading_dates
    for biz_date in it:
        idx = date_to_idx[biz_date]
        next_date = trading_dates[idx + 1] if idx + 1 < len(trading_dates) else None

        # 1) signals 생성
        tickers = _build_ticker_data_for_date(master, bars_lookup, biz_date)
        rs_rank_map = _rs_rank_map(tickers)
        portfolio_meta = sim.to_portfolio_dict()
        sigs = sg.generate_signals(
            biz_date, tickers, portfolio_meta, rs_rank_map,
            {m: s[s.index <= biz_date] for m, s in index_close_by_market.items()},
            cfg,
        )

        # 2) SELL 처리 (다음 영업일 시가)
        if next_date is not None:
            for s in sigs.sells:
                bar = _safe_bar(bars_lookup, s.ticker, next_date)
                if bar is None:
                    # 다음날 데이터 없음 → 당일 종가 청산
                    last_close = _safe_bar(bars_lookup, s.ticker, biz_date)
                    if last_close is None: continue
                    raw_exit = float(last_close["close"])
                    exec_date = biz_date
                else:
                    raw_exit = float(bar["open"])
                    exec_date = next_date

                pos = sim.positions.get(s.ticker)
                if pos is None: continue

                if s.reason == "STOP_HIT":
                    forced = min(pos.current_stop, raw_exit)
                    sim.close(ticker=s.ticker, date=exec_date, exit_open=raw_exit,
                              reason=s.reason, forced_price=forced,
                              gap_down=(s.label == "URGENT_GAP_DOWN"))
                else:
                    sim.close(ticker=s.ticker, date=exec_date, exit_open=raw_exit,
                              reason=s.reason)

        # 3) HOLD 트레일링 갱신
        sim.update_trail_stops(sigs.holds)

        # 4) BUY 처리 (다음 영업일 시가)
        if next_date is not None:
            for b in sigs.new_buys:
                bar = _safe_bar(bars_lookup, b.ticker, next_date)
                if bar is None:
                    continue
                entry_open = float(bar["open"])
                # 갭상승 시 추격 금지 (선택): open > entry_guide × 1.03 → 스킵
                if entry_open > b.entry_guide * 1.03:
                    continue
                # 다음날 시가가 스톱보다 낮으면 진입 불가 (이상 케이스)
                if entry_open <= b.stop:
                    continue
                ticker_meta = master[master["ticker"] == b.ticker]
                name = ticker_meta["name"].iloc[0] if not ticker_meta.empty else b.name
                market = ticker_meta["market"].iloc[0] if not ticker_meta.empty else b.market
                sim.open(
                    ticker=b.ticker, name=name, market=market,
                    date=next_date, entry_open=entry_open,
                    stop=b.stop, box_top=b.box_top, box_bottom=b.box_bottom,
                    desired_shares=b.suggested_shares,
                )

        # 5) Equity 기록 (당일 종가)
        close_by_ticker = {}
        for tk in sim.positions.keys():
            bar = _safe_bar(bars_lookup, tk, biz_date)
            if bar is not None:
                close_by_ticker[tk] = float(bar["close"])
        eq = sim.equity(close_by_ticker)
        equity_rows.append({
            "date": biz_date, "equity": eq,
            "n_positions": len(sim.positions), "cash": sim.cash,
        })

        # 벤치마크
        if not bench_series.empty:
            b = bench_series[bench_series.index <= biz_date]
            if not b.empty:
                bench_curve_rows.append({"date": biz_date, "close": float(b.iloc[-1])})

    equity_curve = pd.DataFrame(equity_rows)
    bench_curve = pd.DataFrame(bench_curve_rows)
    if not bench_curve.empty:
        first = bench_curve["close"].iloc[0]
        bench_curve["benchmark_equity"] = bench_curve["close"] / first * params.initial_equity

    stats = compute_stats(equity_curve, sim.closed_trades, bench_curve, params)
    return BacktestResult(
        params=params,
        equity_curve=equity_curve,
        trades=sim.closed_trades,
        benchmark_curve=bench_curve,
        stats=stats,
    )


# ============================================================
# Helpers — ticker data & RS rank
# ============================================================

def _build_ticker_data_for_date(
    master: pd.DataFrame,
    bars_lookup: dict[str, pd.DataFrame],
    asof: str,
) -> list[sg.TickerData]:
    """master에 mcap 컬럼이 있으면 사용, 없으면 value×50 fallback."""
    has_real_mcap = "mcap" in master.columns
    mcap_map = dict(zip(master["ticker"], master["mcap"])) if has_real_mcap else {}

    out: list[sg.TickerData] = []
    for row in master.itertuples(index=False):
        df = bars_lookup.get(row.ticker)
        if df is None:
            continue
        bars = df.loc[df.index <= asof].copy().reset_index()
        if bars.empty or len(bars) < 252:
            continue
        if has_real_mcap and mcap_map.get(row.ticker, 0) > 0:
            mcap = float(mcap_map[row.ticker])
        else:
            avg_value_20 = float(bars["value"].tail(20).mean()) if "value" in bars.columns else 0.0
            mcap = avg_value_20 * 50
        out.append(sg.TickerData(
            ticker=row.ticker, name=row.name, market=row.market,
            bars=bars, mcap=mcap, flagged=False,
        ))
    return out


def _rs_rank_map(tickers: list[sg.TickerData]) -> dict[str, float]:
    rows = []
    for td in tickers:
        if td.bars is None or len(td.bars) < 253:
            continue
        rs_mom = ind.rs_momentum(td.bars["close"].astype(float))
        rows.append({"ticker": td.ticker, "market": td.market, "rs_momentum": rs_mom})
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    df["rs_rank"] = float("nan")
    for market, group in df.groupby("market"):
        df.loc[group.index, "rs_rank"] = ind.rs_rank(group["rs_momentum"]).values
    return dict(zip(df["ticker"], df["rs_rank"]))


def _safe_bar(bars_lookup: dict[str, pd.DataFrame], ticker: str, date: str):
    df = bars_lookup.get(ticker)
    if df is None:
        return None
    if date not in df.index:
        return None
    return df.loc[date]


# ============================================================
# Stats
# ============================================================

def compute_stats(
    equity_curve: pd.DataFrame,
    trades: list[Trade],
    bench_curve: pd.DataFrame,
    params: BacktestParams,
) -> dict[str, Any]:
    if equity_curve.empty:
        return {}

    eq = equity_curve["equity"].astype(float).values
    n = len(eq)
    total_return = eq[-1] / eq[0] - 1.0
    # 영업일 ~ 252일/연 가정
    years = n / 252.0 if n > 0 else 1.0
    cagr = (eq[-1] / eq[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0

    # Max Drawdown
    peak = np.maximum.accumulate(eq)
    drawdown = (eq - peak) / peak
    mdd = float(drawdown.min())   # 음수
    mar = cagr / abs(mdd) if mdd < 0 else float("inf")

    # Trade stats
    n_trades = len(trades)
    wins = [t for t in trades if t.pnl_krw > 0]
    losses = [t for t in trades if t.pnl_krw <= 0]
    win_rate = len(wins) / n_trades if n_trades > 0 else 0.0
    avg_r = float(np.mean([t.r_multiple for t in trades])) if trades else 0.0
    avg_win_r = float(np.mean([t.r_multiple for t in wins])) if wins else 0.0
    avg_loss_r = float(np.mean([t.r_multiple for t in losses])) if losses else 0.0
    avg_days = float(np.mean([t.days_held for t in trades])) if trades else 0.0
    expectancy_r = win_rate * avg_win_r + (1.0 - win_rate) * avg_loss_r

    # Benchmark
    bench_total = float("nan")
    alpha = float("nan")
    if not bench_curve.empty and "benchmark_equity" in bench_curve.columns:
        be = bench_curve["benchmark_equity"].astype(float).values
        if len(be) > 1 and be[0] > 0:
            bench_total = be[-1] / be[0] - 1.0
            alpha = total_return - bench_total

    return {
        "total_return_pct": total_return * 100,
        "cagr_pct": cagr * 100,
        "mdd_pct": mdd * 100,
        "mar": mar,
        "n_trades": n_trades,
        "win_rate_pct": win_rate * 100,
        "avg_r": avg_r,
        "avg_win_r": avg_win_r,
        "avg_loss_r": avg_loss_r,
        "avg_days_held": avg_days,
        "expectancy_r": expectancy_r,
        "benchmark_total_return_pct": bench_total * 100 if not np.isnan(bench_total) else None,
        "alpha_pct": alpha * 100 if not np.isnan(alpha) else None,
        "final_equity": float(eq[-1]),
        "initial_equity": float(eq[0]),
        "trading_days": int(n),
    }
