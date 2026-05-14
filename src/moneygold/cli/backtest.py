"""백테스트 CLI.

사용:
    python -m moneygold.cli.backtest --start 20240801 --end 20260514
    python -m moneygold.cli.backtest --start 20250101 --end 20260514 --equity 50000000
    python -m moneygold.cli.backtest --limit 200       # 첫 N종목만 (디버그·빠른 sanity)
    python -m moneygold.cli.backtest --export
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

from ..backtest import BacktestParams, run_backtest
from ..config import load_config
from ..data import store


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_indices(data_dir: Path) -> dict[str, pd.Series]:
    out = {}
    for code in ("KOSPI", "KOSDAQ", "KOSPI200", "KOSDAQ150"):
        df = store.read_parquet_safe(store.index_path(data_dir, code))
        if df is not None and not df.empty:
            out[code] = df.set_index("date")["close"].astype(float)
    # signals.py가 기대하는 키는 KOSPI/KOSDAQ (시장명). 매핑:
    #   KOSPI 종목 → KOSPI200, KOSDAQ 종목 → KOSDAQ150
    if "KOSPI200" in out:
        out["KOSPI"] = out["KOSPI200"]   # 시장명 키로 alias
    if "KOSDAQ150" in out:
        out["KOSDAQ"] = out["KOSDAQ150"]
    return out


def _load_bars_for_master(data_dir: Path, master: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    for tk in master["ticker"]:
        df = store.read_parquet_safe(store.bars_path(data_dir, tk))
        if df is None or df.empty:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        out[tk] = df
    return out


def _print_summary(result, params: BacktestParams) -> None:
    s = result.stats
    print(f"\n{result.survivorship_warning}\n")
    print(f"=== Backtest {params.start} → {params.end} ===")
    print(f"  initial: {s['initial_equity']:>14,.0f}")
    print(f"  final:   {s['final_equity']:>14,.0f}")
    print(f"  trading days: {s['trading_days']}")
    print()
    print(f"  Total return:  {s['total_return_pct']:>+8.2f}%")
    print(f"  CAGR:          {s['cagr_pct']:>+8.2f}%")
    print(f"  MDD:           {s['mdd_pct']:>+8.2f}%")
    print(f"  MAR:           {s['mar']:>+8.3f}")
    if s.get("benchmark_total_return_pct") is not None:
        print(f"  Benchmark ({params.benchmark}): {s['benchmark_total_return_pct']:>+8.2f}%")
        print(f"  Alpha:         {s['alpha_pct']:>+8.2f}%p")
    print()
    print(f"  Trades:        {s['n_trades']}")
    print(f"  Win rate:      {s['win_rate_pct']:>+8.2f}%")
    print(f"  Avg R:         {s['avg_r']:>+8.3f}")
    print(f"  Avg win R:     {s['avg_win_r']:>+8.3f}")
    print(f"  Avg loss R:    {s['avg_loss_r']:>+8.3f}")
    print(f"  Avg days held: {s['avg_days_held']:>+8.1f}")
    print(f"  Expectancy R:  {s['expectancy_r']:>+8.3f}")

    # Exit reason distribution
    if result.trades:
        reasons = {}
        for t in result.trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        print("\n  Exit reasons:")
        for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {r:>18}: {c:>4}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="moneygold 백테스트")
    parser.add_argument("--start", required=True, help="시작 YYYYMMDD")
    parser.add_argument("--end", help="종료 YYYYMMDD (기본 오늘)")
    parser.add_argument("--equity", type=float, default=10_000_000, help="초기 자본 KRW (기본 1천만)")
    parser.add_argument("--benchmark", default=None, help="비교 지수 (기본 cfg.benchmark_index)")
    parser.add_argument("--limit", type=int, help="첫 N종목만 (디버그)")
    parser.add_argument("--export", action="store_true", help="result/backtest/{start}_{end}/에 저장")
    args = parser.parse_args(argv)

    cfg = load_config()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("moneygold.cli.backtest")

    end = args.end or datetime.now().strftime("%Y%m%d")
    params = BacktestParams(
        start=args.start, end=end, initial_equity=args.equity,
        benchmark=args.benchmark or cfg.benchmark_index,
    )

    data_dir = Path(cfg.data_dir)
    master = store.read_parquet_safe(store.master_path(data_dir))
    if master is None or master.empty:
        log.error("마스터 없음")
        return 1
    if args.limit:
        master = master.head(args.limit).copy()

    log.info("Loading bars for %d tickers ...", len(master))
    bars_by_ticker = _load_bars_for_master(data_dir, master)
    log.info("Loaded bars for %d tickers", len(bars_by_ticker))

    indices = _load_indices(data_dir)
    if not indices:
        log.error("지수 없음. `--indices`로 sync 먼저")
        return 1

    log.info("Running backtest %s → %s ...", params.start, params.end)
    result = run_backtest(params, master, bars_by_ticker, indices, cfg)
    _print_summary(result, params)

    if args.export:
        out_dir = Path(cfg.result_dir) / "backtest" / f"{params.start}_{params.end}"
        out_dir.mkdir(parents=True, exist_ok=True)
        result.equity_curve.to_csv(out_dir / "equity.csv", index=False)
        if not result.benchmark_curve.empty:
            result.benchmark_curve.to_csv(out_dir / "benchmark.csv", index=False)
        with open(out_dir / "trades.json", "w", encoding="utf-8") as f:
            json.dump([asdict(t) for t in result.trades], f, ensure_ascii=False, indent=2)
        with open(out_dir / "stats.json", "w", encoding="utf-8") as f:
            json.dump({
                "params": asdict(params),
                "stats": result.stats,
                "survivorship_warning": result.survivorship_warning,
            }, f, ensure_ascii=False, indent=2)
        log.info("Exported: %s", out_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
