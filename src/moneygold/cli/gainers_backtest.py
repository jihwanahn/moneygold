"""Gainers 시그너처 검증 백테스트 CLI.

기간 [start, end]의 모든 +N% 상승 이벤트를 수집해 시그너처 그룹별 forward
return 분포를 비교한다. *전략* 백테스트가 아니라 *feature validation*용.

사용:
    python -m moneygold.cli.gainers_backtest --start 20240101 --end 20260514 --market US
    python -m moneygold.cli.gainers_backtest --start 20240101 --end 20260514 \
        --market US --horizons 1,5,20,60 --min-pct 0.01 \
        --export result/gainers_backtest_us.csv
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .. import gainers_backtest as gb
from ..config import load_config


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gainers 시그너처 event study 백테스트",
    )
    parser.add_argument("--start", required=True, help="시작일 YYYYMMDD")
    parser.add_argument("--end", required=True, help="종료일 YYYYMMDD")
    parser.add_argument("--market", default="US",
                        help="시장 (US/KOSPI/KOSDAQ). 전체는 ALL.")
    parser.add_argument("--min-pct", type=float, default=0.01,
                        help="gainer 컷오프 (기본 0.01 = +1%%)")
    parser.add_argument("--horizons", default="1,5,20",
                        help="forward return 일수 (콤마구분, 기본 1,5,20)")
    parser.add_argument("--export", help="이벤트 CSV 경로 (선택)")
    parser.add_argument("--export-report", help="그룹 리포트 CSV 경로 (선택)")
    args = parser.parse_args(argv)

    cfg = load_config()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("moneygold.cli.gainers_backtest")

    market = None if args.market.upper() == "ALL" else args.market
    horizons = tuple(int(h) for h in args.horizons.split(","))

    log.info("collect_events: market=%s start=%s end=%s min_pct=%.3f horizons=%s",
             market or "ALL", args.start, args.end, args.min_pct, horizons)
    events = gb.collect_events(
        Path(cfg.data_dir),
        start=args.start, end=args.end,
        market=market, min_pct=args.min_pct,
        horizons=horizons,
    )
    if events.empty:
        log.warning("이벤트 없음.")
        return 1

    log.info("총 이벤트: %d", len(events))

    # --- 그룹 리포트 ---
    rpt = gb.group_report(events, horizons=horizons)
    if rpt.empty:
        log.warning("리포트 비어있음.")
        return 1

    # 콘솔 출력 (그룹 × horizon 격자)
    print()
    print("=" * 100)
    print(f"Gainers event study  [{args.start} ~ {args.end}]  market={args.market}"
          f"  min_pct={args.min_pct*100:.1f}%  n_events={len(events)}")
    print("=" * 100)
    for h in horizons:
        sub = rpt[rpt["horizon_d"] == h]
        if sub.empty:
            continue
        print(f"\n--- forward return @ t+{h}d ---")
        disp = sub[["group", "n", "mean", "median", "win_rate", "sharpe", "p25", "p75"]].copy()
        disp["mean_%"] = (disp["mean"] * 100).round(3)
        disp["median_%"] = (disp["median"] * 100).round(3)
        disp["win_%"] = (disp["win_rate"] * 100).round(1)
        disp["p25_%"] = (disp["p25"] * 100).round(2)
        disp["p75_%"] = (disp["p75"] * 100).round(2)
        disp["sharpe"] = disp["sharpe"].round(3)
        print(
            disp[["group", "n", "mean_%", "median_%", "win_%", "sharpe", "p25_%", "p75_%"]]
            .to_string(index=False)
        )

    # --- edge table (baseline=ALL) ---
    edge = gb.edge_table(rpt, baseline="ALL")
    if not edge.empty:
        print("\n" + "=" * 100)
        print("Edge vs ALL baseline (bps = 0.01%)")
        print("=" * 100)
        for h in horizons:
            sub = edge[edge["horizon_d"] == h]
            if sub.empty:
                continue
            print(f"\n--- t+{h}d ---")
            disp = sub.copy()
            disp["mean_%"] = disp["mean_pct"].round(3)
            disp["edge_bps"] = disp["edge_vs_all_bps"].round(1)
            disp["win_%"] = (disp["win_rate"] * 100).round(1)
            disp["sharpe"] = disp["sharpe"].round(3)
            print(
                disp[["group", "n", "mean_%", "edge_bps", "win_%", "sharpe"]]
                .to_string(index=False)
            )

    if args.export:
        out = Path(args.export)
        out.parent.mkdir(parents=True, exist_ok=True)
        events.to_csv(out, index=False, encoding="utf-8-sig")
        log.info("events exported: %s", out)
    if args.export_report:
        out = Path(args.export_report)
        out.parent.mkdir(parents=True, exist_ok=True)
        rpt.to_csv(out, index=False, encoding="utf-8-sig")
        log.info("report exported: %s", out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
