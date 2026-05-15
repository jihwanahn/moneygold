"""일일 시그널 생성 CLI.

사용:
    python -m moneygold.cli.signals
    python -m moneygold.cli.signals --asof 20260514
    python -m moneygold.cli.signals --limit 50    # 첫 N종목만 (디버그)
    python -m moneygold.cli.signals --export      # store/signals/{asof}.json 저장

ARCHITECTURE.md §7. 시그널은 사용자 매매 보조용 — 자동 주문 안 함.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from .. import indicators as ind
from .. import signals as sg
from ..config import load_config
from ..data import store


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_index_close(data_dir: Path, index_code: str) -> pd.Series:
    p = store.index_path(data_dir, index_code)
    df = store.read_parquet_safe(p)
    if df is None or df.empty:
        raise FileNotFoundError(f"지수 {index_code} 없음: {p}")
    return df.set_index("date")["close"].astype(float)


def _load_portfolio(path: Path) -> dict[str, sg.PositionMeta]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return {}
    meta = obj.get("meta", {})
    out: dict[str, sg.PositionMeta] = {}
    for tk, m in meta.items():
        out[tk] = sg.PositionMeta(
            ticker=tk,
            entry_date=str(m.get("entry_date", "")),
            entry_price=float(m.get("entry_price", 0.0)),
            current_stop=float(m.get("current_stop", 0.0)),
            current_box_top=m.get("current_box", {}).get("top"),
            current_box_bottom=m.get("current_box", {}).get("bottom"),
            highest_close_since_entry=m.get("highest_close_since_entry"),
        )
    return out


def _build_ticker_data(
    master: pd.DataFrame,
    data_dir: Path,
    asof: str,
    log: logging.Logger,
) -> list[sg.TickerData]:
    """마스터의 종목별로 bars + mcap을 읽어와 TickerData 리스트 생성.

    master에 'mcap' 컬럼이 있으면(pykrx sector classifications에서 받음) 그대로 사용.
    없으면 value×50 거친 proxy로 fallback (구 마스터 호환).
    """
    has_real_mcap = "mcap" in master.columns
    mcap_map = dict(zip(master["ticker"], master["mcap"])) if has_real_mcap else {}

    out: list[sg.TickerData] = []
    for row in master.itertuples(index=False):
        p = store.bars_path(data_dir, row.ticker)
        bars = store.read_parquet_safe(p)
        if bars is None or bars.empty:
            continue
        bars_clip = bars[bars["date"] <= asof]
        if bars_clip.empty:
            continue

        if has_real_mcap and mcap_map.get(row.ticker, 0) > 0:
            mcap = float(mcap_map[row.ticker])
        else:
            # fallback: 평균 거래대금 × 50 (구 master 또는 mcap=0인 종목)
            avg_value = float(bars_clip["value"].tail(20).mean()) if "value" in bars_clip.columns else 0.0
            mcap = avg_value * 50

        out.append(sg.TickerData(
            ticker=row.ticker, name=row.name, market=row.market,
            bars=bars, mcap=mcap, flagged=False,
        ))
    return out


def _compute_rs_maps(
    tickers: list[sg.TickerData],
    sma_window_for_min_data: int = 252,
) -> tuple[dict[str, float], dict[str, float]]:
    """모든 종목의 rs_momentum 계산 + 시장별 횡단면 백분위.

    Returns (rs_rank_map, rs_momentum_map).
    """
    rows = []
    for td in tickers:
        bars = td.bars
        if bars is None or bars.empty or len(bars) < sma_window_for_min_data + 1:
            continue
        close = bars["close"].astype(float)
        rs_mom = ind.rs_momentum(close)
        rows.append({"ticker": td.ticker, "market": td.market, "rs_momentum": rs_mom})

    if not rows:
        return {}, {}
    df = pd.DataFrame(rows)
    df["rs_rank"] = float("nan")
    for market, group in df.groupby("market"):
        df.loc[group.index, "rs_rank"] = ind.rs_rank(group["rs_momentum"]).values
    rank_map = dict(zip(df["ticker"], df["rs_rank"]))
    mom_map = dict(zip(df["ticker"], df["rs_momentum"]))
    return rank_map, mom_map


def _print_report(sigs: sg.DailySignals, master: pd.DataFrame, watchlist_top: int = 30) -> None:
    name_map = dict(zip(master["ticker"], master["name"]))
    print(f"\n=== 일일 추천 {sigs.asof} ===")
    print(
        f"후보 풀: {len(sigs.watchlist)}  |  박스 돌파: {len(sigs.new_buys)}  "
        f"|  보유 HOLD: {len(sigs.holds)}  |  SELL 경고: {len(sigs.sells)}\n"
    )

    # 1) 박스 돌파 종목 — 즉시 검토 대상
    if sigs.new_buys:
        print("⭐ [박스 돌파 — 즉시 검토]")
        print(f"  {'ticker':>7} {'name':>12} {'mkt':>6} {'RS':>5} {'close':>10} {'entry guide':>12} {'stop':>10} {'box days':>9} {'vol×':>7} {'gap':>5}")
        for b in sigs.new_buys:
            print(f"  {b.ticker:>7} {b.name[:12]:>12} {b.market:>6} {b.rs_rank:>5.0f} "
                  f"{b.box_top:>10,.0f} {b.entry_guide:>12,.0f} {b.stop:>10,.0f} "
                  f"{b.days_in_box:>9} {b.volume_ratio:>6.2f}x {('YES' if b.is_gap_breakout else '-'):>5}")
        print()

    # 2) 워치리스트 — Stage 2 + Template 통과, RS 상위 TOP N
    if sigs.watchlist:
        shown = sigs.watchlist[:watchlist_top]
        print(f"[BUY 후보 풀 — Stage 2 + Template 통과, RS desc TOP {len(shown)}/{len(sigs.watchlist)}]")
        print(f"  {'ticker':>7} {'name':>12} {'mkt':>6} {'RS':>6} {'rs_mom':>7} "
              f"{'close':>10} {'box':>12} {'box top':>10} {'box bot':>10} {'days':>5} {'stop hint':>10}")
        for w in shown:
            box_top_str = f"{w.box_top:,.0f}" if w.box_top is not None else "-"
            box_bot_str = f"{w.box_bottom:,.0f}" if w.box_bottom is not None else "-"
            marker = "⭐" if w.box_state.startswith("BREAKOUT") else (
                "•" if w.box_state == "CONFIRMED" else " "
            )
            mom_str = f"{w.rs_momentum:>+6.2f}" if not pd.isna(w.rs_momentum) else "    -"
            print(f"  {w.ticker:>7} {w.name[:12]:>12} {w.market:>6} {w.rs_rank:>6.1f} {mom_str:>7} "
                  f"{w.close:>10,.0f} {marker} {w.box_state[:10]:>10} "
                  f"{box_top_str:>10} {box_bot_str:>10} {w.days_in_box:>5} {w.suggested_stop:>10,.0f}")
        print()

    # 3) 보유 — Stage 변화 + trailing 갱신
    if sigs.holds:
        print(f"[HOLD] {len(sigs.holds)}개")
        updated = [h for h in sigs.holds if h.trail_updated]
        for h in updated[:20]:
            print(f"  {h.ticker} {h.name:>10}  close {h.current_close:,.0f}  "
                  f"stop {h.current_stop:,.0f} → {h.new_stop:,.0f}  (trail ↑)")
        if not updated:
            print("  (트레일링 갱신 없음)")
        print()

    # 4) SELL 경고
    if sigs.sells:
        print("⚠ [SELL 경고 — 검토 필요]")
        for s in sigs.sells:
            label = f"  {s.label}" if s.label else ""
            print(f"  {s.ticker} {s.name:>10}  {s.reason:>15}  종가 {s.exit_guide:,.0f}{label}")
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="moneygold 일일 시그널")
    parser.add_argument("--asof", help="기준일 YYYYMMDD. 기본은 오늘.")
    parser.add_argument("--limit", type=int, help="첫 N개 종목만 (디버그)")
    parser.add_argument("--top", type=int, default=30, help="워치리스트 출력 상위 N (기본 30)")
    parser.add_argument("--export", action="store_true", help="store/signals/{asof}.json 저장")
    args = parser.parse_args(argv)

    cfg = load_config()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("moneygold.cli.signals")

    asof = args.asof or datetime.now().strftime("%Y%m%d")
    data_dir = Path(cfg.data_dir)

    master = store.read_parquet_safe(store.master_path(data_dir))
    if master is None or master.empty:
        log.error("마스터 없음. 먼저 `python -m moneygold.cli.sync --universe`")
        return 1
    if args.limit:
        master = master.head(args.limit).copy()

    try:
        idx_kospi = _load_index_close(data_dir, "KOSPI200")
        idx_kosdaq = _load_index_close(data_dir, "KOSDAQ150")
    except FileNotFoundError as e:
        log.error(str(e))
        return 1
    idx_close_by_market = {"KOSPI": idx_kospi[idx_kospi.index <= asof],
                            "KOSDAQ": idx_kosdaq[idx_kosdaq.index <= asof]}

    log.info("Loading bars for %d tickers ...", len(master))
    tickers = _build_ticker_data(master, data_dir, asof, log)
    log.info("Loaded %d / %d tickers", len(tickers), len(master))

    log.info("Computing RS rank ...")
    rs_rank_map, rs_momentum_map = _compute_rs_maps(tickers)
    log.info("RS rank computed for %d tickers", len(rs_rank_map))

    portfolio_path = data_dir / "portfolio.json"
    portfolio = _load_portfolio(portfolio_path)
    if portfolio:
        log.info("Portfolio loaded: %d positions", len(portfolio))

    log.info("Generating signals as of %s ...", asof)
    sigs = sg.generate_signals(
        asof, tickers, portfolio, rs_rank_map, idx_close_by_market, cfg,
        rs_momentum_map=rs_momentum_map,
    )

    _print_report(sigs, master, watchlist_top=args.top)

    if args.export:
        out_path = data_dir / "signals" / f"{asof}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(sg.to_dict(sigs), f, ensure_ascii=False, indent=2)
        log.info("Exported: %s", out_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
