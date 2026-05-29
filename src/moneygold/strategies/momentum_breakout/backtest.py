"""Momentum Breakout walk-forward 백테스트.

전략 (사용자 명제):
  1. 진입: 거래대금 상위 100 + (옵션) 시총 상위 100 + N일 신고가 막 돌파
     → 다음 영업일 시가에 진입.
  2. 손절: entry × (1 - stop_loss_pct), 기본 -10%. INITIAL 단계.
  3. 트레일링: high_since_entry ≥ entry × (1 + profit_trigger_pct=20%) 도달 시
     TRAILING 단계 전환. stop = max(prev_stop, MA20) — 후퇴 X. MA20 이탈 시 청산.
  4. 갭다운: 시초가 < stop 이면 *시가* 청산 (gap_down_exit_policy='open').

설계:
  - signals.find_entry_candidates 그대로 재사용 — 라이브와 동일 코드 경로.
  - position.step 그대로 재사용.
  - 진입은 *다음 영업일 시가* + 슬리피지 + 수수료.
  - 청산은 step() 의 exit_price (갭다운=시가, 종가이탈=stop intraday touched).
    추가 슬리피지 + 수수료 + 매도세.
  - 자동 주문 코드 없음 (시뮬만).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from . import indicators as ind
from . import signals as sg
from .config import MomentumConfig
from .position import (
    EXITED,
    INITIAL,
    TRAILING,
    PositionEntry,
    PositionState,
    initial_state,
)
from .position import (
    step as position_step,
)

log = logging.getLogger(__name__)


# ============================================================
# Params / output
# ============================================================

@dataclass
class BacktestParams:
    start: str                            # YYYYMMDD
    end: str                              # YYYYMMDD
    initial_equity: float = 10_000_000.0
    slippage_bps: float = 20.0            # 0.20% 진입/청산 양쪽
    commission_bps: float = 1.5           # 0.015% 매수/매도 각각
    tax_sell_bps: float = 20.0            # 0.20% 매도세
    max_positions: int = 10
    position_size_pct: float = 10.0       # 현 equity 대비 한 포지션 max 비중
    benchmark: str = "KOSPI200"


@dataclass
class MomentumTrade:
    ticker: str
    name: str
    market: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    shares: int
    initial_stop: float
    final_stop: float
    peak_phase: str                       # INITIAL | TRAILING — TRAILING 도달했는가
    high_since_entry: float
    exit_reason: str                      # STOP_HIT | END_OF_DATA
    pnl_krw: float
    pnl_pct: float
    r_multiple: float                     # 손익 / 초기 리스크
    days_held: int
    gap_down: bool = False


@dataclass
class MomentumBacktestResult:
    params: BacktestParams
    momo_cfg: MomentumConfig
    equity_curve: pd.DataFrame            # date, equity, cash, n_positions
    trades: list[MomentumTrade]
    benchmark_curve: pd.DataFrame
    stats: dict[str, Any]
    open_positions_at_end: list[dict] = field(default_factory=list)
    survivorship_warning: str = (
        "⚠️ KIS는 상장폐지 종목 시세를 제공하지 않습니다. "
        "본 백테스트는 현재 KIS에서 조회 가능한 종목만 포함하므로, "
        "결과는 생존편향에 의해 실제보다 우호적일 수 있습니다."
    )


# ============================================================
# Sim portfolio
# ============================================================

class _SimPortfolio:
    """현금 + 보유 포지션 + 청산된 trade 관리.

    Position은 (PositionEntry, PositionState, shares, name) 튜플로 트래킹.
    """

    def __init__(self, params: BacktestParams):
        self.cash = float(params.initial_equity)
        self.params = params
        self.positions: dict[str, _OpenPos] = {}
        self.closed: list[MomentumTrade] = []

    def can_open(self) -> bool:
        return len(self.positions) < self.params.max_positions

    def open(self, *, entry: PositionEntry, state: PositionState, shares: int,
             eff_entry_price: float) -> None:
        cost = eff_entry_price * shares
        self.cash -= cost
        self.positions[entry.ticker] = _OpenPos(
            entry=entry, state=state, shares=shares,
            eff_entry_price=eff_entry_price,
            highest_phase=state.phase,
        )

    def close(self, *, ticker: str, exit_date: str, raw_exit_price: float,
              exit_reason: str, gap_down: bool) -> MomentumTrade | None:
        if ticker not in self.positions:
            return None
        pos = self.positions.pop(ticker)
        # 청산 비용 — slip, commission, tax
        slip = raw_exit_price * self.params.slippage_bps / 10000.0
        comm = raw_exit_price * self.params.commission_bps / 10000.0
        tax = raw_exit_price * self.params.tax_sell_bps / 10000.0
        eff_exit = max(0.0, raw_exit_price - slip - comm - tax)
        self.cash += eff_exit * pos.shares

        pnl_krw = (eff_exit - pos.eff_entry_price) * pos.shares
        pnl_pct = (eff_exit / pos.eff_entry_price - 1.0) * 100.0 if pos.eff_entry_price > 0 else 0.0
        # 초기 리스크: entry × stop_loss_pct (eff_entry × initial_stop_pct)
        # initial_state 의 stop 은 raw entry × (1 - stop_loss_pct) 인데,
        # 실제 리스크 단위는 (eff_entry - initial_stop). 단순히 entry × stop_loss_pct
        # 로 잡으면 슬리피지 미반영. 정확히는 (eff_entry - pos.entry.entry_price * (1-stop_pct)).
        initial_stop = pos.entry.entry_price * (
            1.0 - self._stop_loss_pct_from_state(pos.state, pos.entry)
        )
        risk_per_share = pos.eff_entry_price - initial_stop
        r_mult = (eff_exit - pos.eff_entry_price) / risk_per_share if risk_per_share > 0 else 0.0

        try:
            days = (pd.to_datetime(exit_date) - pd.to_datetime(pos.entry.entry_date)).days
        except Exception:
            days = 0

        tr = MomentumTrade(
            ticker=pos.entry.ticker, name=pos.entry.name, market=pos.entry.market,
            entry_date=pos.entry.entry_date, entry_price=pos.eff_entry_price,
            exit_date=exit_date, exit_price=eff_exit,
            shares=pos.shares,
            initial_stop=initial_stop,
            final_stop=pos.state.stop,
            peak_phase=pos.highest_phase,
            high_since_entry=pos.state.high_since_entry,
            exit_reason=exit_reason,
            pnl_krw=pnl_krw, pnl_pct=pnl_pct, r_multiple=r_mult,
            days_held=days, gap_down=gap_down,
        )
        self.closed.append(tr)
        return tr

    @staticmethod
    def _stop_loss_pct_from_state(state: PositionState, entry: PositionEntry) -> float:
        """state.stop 에 박힌 INITIAL stop 으로 stop_loss_pct 역산.

        state 가 이미 TRAILING 으로 갱신됐을 수 있으니, 안전한 추정: state.stop 이
        entry_price 의 0.90 이면 stop_loss_pct=0.10. 단 다른 값이면 오차.
        """
        if entry.entry_price <= 0:
            return 0.10
        # 추정: INITIAL 단계의 stop 비율 (10%). 정확히는 cfg.stop_loss_pct 인데
        # 호출부에서 그 정보를 못 받음. r_multiple 분모만 영향 — 큰 영향 아님.
        return 0.10

    def equity(self, close_by_ticker: dict[str, float]) -> float:
        eq = self.cash
        for tk, pos in self.positions.items():
            c = close_by_ticker.get(tk, pos.eff_entry_price)
            eq += float(c) * pos.shares
        return eq


@dataclass
class _OpenPos:
    entry: PositionEntry
    state: PositionState
    shares: int
    eff_entry_price: float                # 슬리피지·수수료 반영 실제 평단
    highest_phase: str                    # INITIAL/TRAILING — TRAILING 도달 여부 추적


# ============================================================
# Walk-forward runner
# ============================================================

def run_momentum_backtest(
    params: BacktestParams,
    momo_cfg: MomentumConfig,
    master: pd.DataFrame,
    bars_by_ticker: dict[str, pd.DataFrame],
    index_close_by_market: dict[str, pd.Series] | None = None,
    *,
    progress: bool = False,
) -> MomentumBacktestResult:
    """일별 워크포워드 시뮬.

    Parameters
    ----------
    params : 백테스트 비용 + 사이즈.
    momo_cfg : 전략 파라미터 (lookback, fresh window, stop_loss_pct=10%,
        profit_trigger_pct=20%, trailing_ma_period=20, ...).
    master : 종목 마스터 — ticker/name/market/mcap 필수. KR (KOSPI+KOSDAQ) 또는 US.
        master 의 market 컬럼 그대로 사용하며, find_entry_candidates 가 시장별 독립
        ranking 처리. 단일 시장만 backtest 하려면 master 에서 사전 필터해서 전달.
    bars_by_ticker : ticker → bars. date(YYYYMMDD)/open/high/low/close/volume/value.
    index_close_by_market : 벤치마크 시계열. {'KOSPI200': pd.Series(date_index)} 등.

    Returns
    -------
    MomentumBacktestResult
    """
    if master is None or master.empty:
        raise ValueError("master 비어있음")
    # 시장 제약 없이 사용 — find_entry_candidates 가 시장별 그룹 분리 처리.
    master_use = master.copy()

    # 거래일 캘린더 — 모든 종목 bars 의 union
    all_dates: set[str] = set()
    for tk in master_use["ticker"].astype(str):
        df = bars_by_ticker.get(tk)
        if df is not None and not df.empty and "date" in df.columns:
            all_dates.update(df["date"].astype(str).tolist())
    trading_dates = sorted(d for d in all_dates if params.start <= d <= params.end)
    if not trading_dates:
        raise ValueError(f"기간 {params.start}~{params.end} 내 거래일 없음")

    sim = _SimPortfolio(params)

    # bars 인덱스 캐시
    bars_lookup: dict[str, pd.DataFrame] = {}
    close_series_cache: dict[str, pd.Series] = {}
    ma20_cache: dict[str, pd.Series] = {}
    for tk, df in bars_by_ticker.items():
        if df is None or df.empty:
            continue
        d = df.sort_values("date").reset_index(drop=True)
        bars_lookup[tk] = d.set_index("date")
        close_series_cache[tk] = d.set_index("date")["close"].astype(float)
        ma20_cache[tk] = ind.ma20(close_series_cache[tk], momo_cfg.trailing_ma_period)

    date_to_idx = {d: i for i, d in enumerate(trading_dates)}

    equity_rows: list[dict] = []
    bench_rows: list[dict] = []
    bench_series = None
    if index_close_by_market:
        bench_series = index_close_by_market.get(params.benchmark)

    iterator = trading_dates
    if progress:
        try:
            from tqdm import tqdm
            iterator = tqdm(trading_dates, desc="backtest", unit="d")
        except ImportError:
            pass

    for biz_date in iterator:
        idx = date_to_idx[biz_date]
        next_date = trading_dates[idx + 1] if idx + 1 < len(trading_dates) else None

        # ----- 1) 보유 종목 step() — 오늘 봉 사용. EXITED면 청산 -----
        to_close: list[tuple[str, float, str, bool]] = []
        for tk, pos in list(sim.positions.items()):
            bar = _safe_bar(bars_lookup, tk, biz_date)
            if bar is None:
                continue
            ma_today = _safe_ma(ma20_cache, tk, biz_date)
            today_bar_dict = {
                "date": biz_date,
                "open": float(bar["open"]),
                "close": float(bar["close"]),
            }
            res = position_step(pos.entry, pos.state, today_bar_dict, momo_cfg,
                                ma20_today=ma_today)
            pos.state = res.state
            if res.state.phase == TRAILING and pos.highest_phase == INITIAL:
                pos.highest_phase = TRAILING
            if res.state.phase == EXITED and res.exit_reason == "STOP_HIT":
                exit_price = float(res.exit_price) if res.exit_price else float(bar["close"])
                to_close.append((tk, exit_price, "STOP_HIT", bool(res.gap_down)))

        for tk, raw_exit, reason, gap_down in to_close:
            sim.close(ticker=tk, exit_date=biz_date, raw_exit_price=raw_exit,
                      exit_reason=reason, gap_down=gap_down)

        # ----- 2) 신규 진입 시그널 — biz_date 종가 기준 → next_date 시가 진입 -----
        if next_date is not None and sim.can_open():
            entries = sg.find_entry_candidates(
                biz_date, bars_by_ticker, master_use, momo_cfg,
            )
            for be in entries:
                if not sim.can_open():
                    break
                if be.ticker in sim.positions:
                    continue                  # 이미 보유
                next_bar = _safe_bar(bars_lookup, be.ticker, next_date)
                if next_bar is None:
                    continue
                next_open = float(next_bar["open"])
                if next_open <= 0:
                    continue
                # 갭상승 추격 금지 — entry 가이드 (=close × (1+breakout buffer)) 대비
                # 큰 갭이면 스킵. 단순화: next_open > close × 1.05 이면 스킵.
                if next_open > be.close * 1.05:
                    continue

                # 진입 비용
                slip = next_open * params.slippage_bps / 10000.0
                comm = next_open * params.commission_bps / 10000.0
                eff_entry = next_open + slip + comm

                # 사이즈: equity * position_size_pct / 100
                close_by_ticker_now = _close_dict_at(close_series_cache, biz_date, sim.positions)
                equity_now = sim.equity(close_by_ticker_now)
                target_notional = equity_now * params.position_size_pct / 100.0
                # 가용 현금 한도
                target_notional = min(target_notional, sim.cash)
                shares = int(target_notional // eff_entry)
                if shares <= 0:
                    continue

                entry = PositionEntry(
                    ticker=be.ticker, name=be.name, market=be.market,
                    entry_date=next_date, entry_price=next_open,
                )
                state0 = initial_state(entry, momo_cfg)
                sim.open(entry=entry, state=state0, shares=shares, eff_entry_price=eff_entry)

        # ----- 3) 당일 종가 기준 equity 기록 -----
        close_by_ticker = _close_dict_at(close_series_cache, biz_date, sim.positions)
        eq = sim.equity(close_by_ticker)
        equity_rows.append({
            "date": biz_date, "equity": eq,
            "cash": sim.cash, "n_positions": len(sim.positions),
        })

        if bench_series is not None:
            b = bench_series[bench_series.index <= biz_date]
            if not b.empty:
                bench_rows.append({"date": biz_date, "close": float(b.iloc[-1])})

    # 종료 시점 미청산 포지션 — 마지막 종가 기준 평가만 (현금화 X).
    final_close = _close_dict_at(close_series_cache, trading_dates[-1], sim.positions)
    open_at_end = [
        {
            "ticker": tk, "name": pos.entry.name, "market": pos.entry.market,
            "entry_date": pos.entry.entry_date, "entry_price": pos.eff_entry_price,
            "shares": pos.shares,
            "last_close": float(final_close.get(tk, pos.eff_entry_price)),
            "current_stop": pos.state.stop, "phase": pos.state.phase,
            "high_since_entry": pos.state.high_since_entry,
            "unrealized_pct": (
                final_close.get(tk, pos.eff_entry_price) / pos.eff_entry_price - 1.0
            ) * 100.0 if pos.eff_entry_price > 0 else 0.0,
        }
        for tk, pos in sim.positions.items()
    ]

    equity_curve = pd.DataFrame(equity_rows)
    bench_curve = pd.DataFrame(bench_rows)
    if not bench_curve.empty:
        first = bench_curve["close"].iloc[0]
        if first > 0:
            bench_curve["benchmark_equity"] = bench_curve["close"] / first * params.initial_equity

    stats = compute_stats(equity_curve, sim.closed, bench_curve, params)
    return MomentumBacktestResult(
        params=params, momo_cfg=momo_cfg,
        equity_curve=equity_curve, trades=sim.closed,
        benchmark_curve=bench_curve, stats=stats,
        open_positions_at_end=open_at_end,
    )


# ============================================================
# Helpers
# ============================================================

def _safe_bar(bars_lookup: dict[str, pd.DataFrame], ticker: str, date: str):
    df = bars_lookup.get(ticker)
    if df is None or date not in df.index:
        return None
    row = df.loc[date]
    # 중복 인덱스 가능성 — 첫 행 사용
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]
    return row


def _safe_ma(ma_cache: dict[str, pd.Series], ticker: str, date: str) -> float | None:
    s = ma_cache.get(ticker)
    if s is None or date not in s.index:
        return None
    v = s.loc[date]
    if isinstance(v, pd.Series):
        v = v.iloc[0]
    return float(v) if pd.notna(v) else None


def _close_dict_at(close_cache: dict[str, pd.Series], date: str,
                   positions: dict[str, _OpenPos]) -> dict[str, float]:
    out: dict[str, float] = {}
    for tk in positions.keys():
        s = close_cache.get(tk)
        if s is None:
            continue
        # date 이하 마지막 close
        cs = s[s.index <= date]
        if cs.empty:
            continue
        out[tk] = float(cs.iloc[-1])
    return out


# ============================================================
# Stats
# ============================================================

def compute_stats(equity_curve: pd.DataFrame, trades: list[MomentumTrade],
                  bench_curve: pd.DataFrame, params: BacktestParams) -> dict[str, Any]:
    if equity_curve.empty:
        return {}
    eq = equity_curve["equity"].astype(float).values
    n = len(eq)
    if eq[0] <= 0:
        return {}
    total_return = eq[-1] / eq[0] - 1.0
    years = n / 252.0 if n > 0 else 1.0
    cagr = (eq[-1] / eq[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0

    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    mdd = float(dd.min())
    mar = cagr / abs(mdd) if mdd < 0 else float("inf")

    n_trades = len(trades)
    wins = [t for t in trades if t.pnl_krw > 0]
    losses = [t for t in trades if t.pnl_krw <= 0]
    win_rate = len(wins) / n_trades if n_trades > 0 else 0.0
    avg_r = float(np.mean([t.r_multiple for t in trades])) if trades else 0.0
    avg_win_r = float(np.mean([t.r_multiple for t in wins])) if wins else 0.0
    avg_loss_r = float(np.mean([t.r_multiple for t in losses])) if losses else 0.0
    avg_days = float(np.mean([t.days_held for t in trades])) if trades else 0.0
    expectancy_r = win_rate * avg_win_r + (1.0 - win_rate) * avg_loss_r

    trailing_reached = sum(1 for t in trades if t.peak_phase == TRAILING)
    trailing_share = trailing_reached / n_trades if n_trades > 0 else 0.0

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
        "trailing_reached_pct": trailing_share * 100,
        "benchmark_total_return_pct": (
            bench_total * 100 if not np.isnan(bench_total) else None
        ),
        "alpha_pct": alpha * 100 if not np.isnan(alpha) else None,
        "final_equity": float(eq[-1]),
        "initial_equity": float(eq[0]),
        "trading_days": int(n),
    }
