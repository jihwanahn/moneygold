"""가속화 장기투자(DGI) 스크리너 CLI.

사용:
    python -m moneygold.cli.dgi --screen --asof 20260527
    python -m moneygold.cli.dgi --screen --tickers 086790,316140,138040
    python -m moneygold.cli.dgi --screen --limit 50 --no-dart   # DART 호출 생략 (빠른 디버그)

전제: store/dividends/{ticker}.parquet 가 채워져 있어야 (사전에 ``sync --dividends``).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from ..config import load_config
from ..data import store
from ..data.kis_client import KISClient
from ..data.sync import DataSync
from ..strategies.value_long_term import scoring
from ..strategies.value_long_term.dart_client import DartClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="가속화 장기투자 (DGI) 스크리너")
    parser.add_argument("--screen", action="store_true",
                        help="전체/지정 종목에 대해 DGI 점수 산출")
    parser.add_argument("--market", choices=["kr", "us"], default="kr",
                        help="kr=KOSPI/KOSDAQ (pykrx+DART), us=S&P500 (yfinance+SEC). 기본 kr.")
    parser.add_argument("--asof", help="기준일 YYYYMMDD. 기본은 오늘.")
    parser.add_argument("--tickers", help="특정 종목만. 콤마 구분.")
    parser.add_argument("--limit", type=int, help="첫 N개 종목만 (디버그)")
    parser.add_argument("--top", type=int, default=30, help="콘솔 출력 상위 N개")
    parser.add_argument("--no-dart", action="store_true",
                        help="[kr] DART(주주환원) 항목 skip. DART 키 없거나 빠른 디버그 시.")
    parser.add_argument("--min-grade", choices=["A", "B", "C"], default="C",
                        help="이 등급 이상만 저장. 기본 C(전체)")
    args = parser.parse_args(argv)

    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("moneygold.cli.dgi")

    if not args.screen:
        parser.print_help()
        return 1

    asof = args.asof or datetime.now().strftime("%Y%m%d")
    data_dir = Path(cfg.data_dir)
    grade_order = {"A": 3, "B": 2, "C": 1}
    min_g = grade_order[args.min_grade]

    if args.market == "us":
        return _screen_us(args, cfg, data_dir, asof, min_g, log)
    return _screen_kr(args, cfg, data_dir, asof, min_g, log)


def _screen_kr(args, cfg, data_dir, asof, min_g, log) -> int:
    grade_order = {"A": 3, "B": 2, "C": 1}
    # 종목 선택
    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        name_map: dict[str, str] = {}
    else:
        kis = KISClient(cfg.kis)
        sync = DataSync(kis, data_dir)
        master = sync.load_universe()
        master = master[master["market"].isin(["KOSPI", "KOSDAQ"])]
        if args.limit:
            master = master.head(args.limit)
        tickers = master["ticker"].tolist()
        name_map = dict(zip(master["ticker"], master.get("name", master["ticker"]), strict=False))

    log.info("=== DGI screen [KR]: asof=%s, tickers=%d ===", asof, len(tickers))

    dart: DartClient | None = None
    if not args.no_dart:
        if not cfg.dart.api_key:
            log.warning("DART_API_KEY 미설정 — 주주환원(10점) 항목 NaN으로 진행")
        else:
            try:
                dart = DartClient(cfg.dart, data_dir)
            except ValueError as e:
                log.warning("DART 비활성: %s", e)

    df = scoring.screen(tickers, asof, data_dir, dart=dart, name_map=name_map)
    if df.empty:
        log.warning("결과 없음")
        return 0
    df_filtered = df[df["grade"].map(grade_order) >= min_g].copy()
    path = scoring.save_scores(df_filtered, data_dir, asof)
    log.info("저장: %s (%d rows, A=%d B=%d C=%d)", path, len(df_filtered),
             (df_filtered["grade"] == "A").sum(), (df_filtered["grade"] == "B").sum(),
             (df_filtered["grade"] == "C").sum())
    show_cols = [
        "ticker", "name", "total", "grade",
        "dividend_total", "capital_total", "fundamental_total", "shareholder_total",
        "dividend_yield_pct", "consecutive_increase_years", "dps_cagr_5y_pct",
        "price_cagr_5y_pct", "roe_5y_avg_pct",
    ]
    available = [c for c in show_cols if c in df_filtered.columns]
    print(df_filtered.head(args.top)[available].to_string(index=False))
    return 0


def _screen_us(args, cfg, data_dir, asof, min_g, log) -> int:
    from ..strategies.value_long_term import scoring_us

    grade_order = {"A": 3, "B": 2, "C": 1}
    # 종목 선택: 명시 tickers > 동기화된 us_dividends 글롭 (= S&P500 sync 결과)
    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        synced = sorted(p.stem for p in (data_dir / "us_dividends").glob("*.parquet"))
        if not synced:
            log.error("동기화된 US 배당 데이터 없음. 먼저: "
                      "python -m moneygold.cli.sync --us-dividends --scope sp500")
            return 1
        tickers = synced
    if args.limit:
        tickers = tickers[: args.limit]

    # name_map (master에서)
    name_map: dict[str, str] = {}
    master = store.read_parquet_safe(store.master_path(data_dir))
    if master is not None and "name" in master.columns:
        us = master[master["market"] == "US"]
        name_map = dict(zip(us["ticker"], us["name"], strict=False))

    log.info("=== DGI screen [US]: asof=%s, tickers=%d ===", asof, len(tickers))

    df = scoring_us.screen_us(tickers, asof, data_dir, name_map=name_map)
    if df.empty:
        log.warning("결과 없음")
        return 0
    df_filtered = df[df["grade"].map(grade_order) >= min_g].copy()
    path = scoring_us.save_us_scores(df_filtered, data_dir, asof)
    log.info("저장: %s (%d rows, A=%d B=%d C=%d)", path, len(df_filtered),
             (df_filtered["grade"] == "A").sum(), (df_filtered["grade"] == "B").sum(),
             (df_filtered["grade"] == "C").sum())
    show_cols = [
        "ticker", "name", "total", "grade", "aristocrat_label",
        "dividend_total", "capital_total", "fundamental_total", "aristocrat_total",
        "dividend_yield_pct", "consecutive_increase_years", "dps_cagr_5y_pct",
        "price_cagr_5y_pct", "roe_pct",
    ]
    available = [c for c in show_cols if c in df_filtered.columns]
    print(df_filtered.head(args.top)[available].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
