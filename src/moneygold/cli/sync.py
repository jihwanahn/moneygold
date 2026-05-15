"""데이터 sync CLI.

사용:
    python -m moneygold.cli.sync --universe        # 마스터만 갱신
    python -m moneygold.cli.sync --backfill        # 마스터 + 전체 종목 2년 백필 + 지수
    python -m moneygold.cli.sync --daily           # 마스터 + 일일 incremental + 지수 (기본)
    python -m moneygold.cli.sync --indices         # 지수만 (KOSPI/KOSDAQ/KOSPI200/KOSDAQ150)
    python -m moneygold.cli.sync --tickers 005930,000660  # 특정 종목만 (지수 X)
    python -m moneygold.cli.sync --limit 20        # 첫 20종목만 (디버그)
    python -m moneygold.cli.sync --skip-indices    # 지수 sync 건너뛰기

기본 동작은 --daily.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ..config import load_config
from ..data.kis_client import KISClient
from ..data.sync import DataSync


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_tickers_arg(s: str | None) -> list[str] | None:
    if not s:
        return None
    return [t.strip() for t in s.split(",") if t.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="moneygold 데이터 sync")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--universe", action="store_true", help="마스터만 갱신 (pykrx)")
    mode.add_argument("--backfill", action="store_true", help="마스터 + 종목 2년 백필 + 지수")
    mode.add_argument("--daily", action="store_true", help="마스터 + 일일 incremental + 지수 (기본)")
    mode.add_argument("--indices", action="store_true", help="지수만 sync")
    mode.add_argument("--financials", action="store_true", help="펀더멘털만 sync (KIS finance 분기)")
    mode.add_argument("--consensus", action="store_true", help="컨센서스만 sync (yfinance, 대형주 위주)")

    parser.add_argument("--tickers", help="특정 종목만. 콤마 구분. 예: 005930,000660")
    parser.add_argument("--limit", type=int, help="첫 N개 종목만 (디버그)")
    parser.add_argument("--years", type=int, default=2, help="백필 연수 (기본 2)")
    parser.add_argument("--asof", help="기준일 YYYYMMDD. 기본은 오늘.")
    parser.add_argument("--skip-indices", action="store_true", help="지수 sync 건너뛰기")
    parser.add_argument("--force-financials", action="store_true",
                        help="--financials 모드에서 캐시 무시 후 재 다운로드")
    parser.add_argument("--force-consensus", action="store_true",
                        help="--consensus 모드에서 캐시 무시 후 재 다운로드")
    args = parser.parse_args(argv)

    cfg = load_config()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("moneygold.cli.sync")

    # 모드 디폴트
    if not (args.universe or args.backfill or args.daily or args.indices or args.financials or args.consensus):
        args.daily = True

    kis = KISClient(cfg.kis)
    sync = DataSync(kis, Path(cfg.data_dir))

    # 컨센서스만 (KIS 무관, yfinance만)
    if args.consensus:
        from .. import consensus as cons
        log.info("== sync_consensus (yfinance) ==")
        master = sync.load_universe()
        rows = list(zip(master["ticker"], master["market"]))
        if args.tickers:
            wanted = set(_parse_tickers_arg(args.tickers) or [])
            rows = [(t, m) for t, m in rows if t in wanted]
        if args.limit:
            rows = rows[: args.limit]
        log.info("Target tickers: %d (force=%s)", len(rows), args.force_consensus)
        stats = cons.sync_consensus_for(Path(cfg.data_dir), rows, force=args.force_consensus)
        log.info("Consensus: total=%d available=%d no_data=%d cached=%d failed=%d",
                 stats["total"], stats["available"], stats["no_data"],
                 stats["cached"], len(stats["failed"]))
        for tk, err in stats["failed"][:10]:
            log.warning("  %s: %s", tk, err)
        return 0

    # 펀더멘털만
    if args.financials:
        log.info("== sync_financials_for ==")
        if args.tickers:
            tickers = _parse_tickers_arg(args.tickers) or []
        else:
            master = sync.load_universe()
            tickers = master["ticker"].tolist()
        if args.limit:
            tickers = tickers[: args.limit]
        log.info("Target tickers: %d (force=%s)", len(tickers), args.force_financials)
        stats = sync.sync_financials_for(tickers, force=args.force_financials)
        log.info("Financials: total=%d updated=%d cached=%d failed=%d",
                 stats["total"], stats["updated"], stats["cached"], len(stats["failed"]))
        for tk, err in stats["failed"][:20]:
            log.warning("  %s: %s", tk, err)
        return 0

    # 지수만
    if args.indices:
        log.info("== sync_indices ==")
        stats = sync.sync_indices(years=args.years, asof=args.asof)
        log.info("Indices: total=%d updated=%d no_change=%d failed=%d",
                 stats["total"], stats["updated"], stats["no_change"], len(stats["failed"]))
        for code, err in stats["failed"]:
            log.warning("  %s: %s", code, err)
        return 0

    # 1) 마스터 (universe/backfill/daily 공통)
    log.info("== sync_universe ==")
    master = sync.sync_universe()
    log.info("Master rows: %d", len(master))
    if args.universe:
        return 0

    # 2) 대상 종목 선정
    if args.tickers:
        tickers = _parse_tickers_arg(args.tickers) or []
    else:
        master = sync.load_universe()
        tickers = master["ticker"].tolist()

    if args.limit:
        tickers = tickers[: args.limit]

    log.info("== sync_bars_all (%d tickers, years=%d, asof=%s) ==",
             len(tickers), args.years, args.asof or "today")
    stats = sync.sync_bars_all(tickers, years=args.years, asof=args.asof)

    log.info("Bars: total=%d updated=%d no_change=%d failed=%d",
             stats["total"], stats["updated"], stats["no_change"], len(stats["failed"]))
    if stats["failed"]:
        log.warning("Failed tickers (first 20):")
        for tk, err in stats["failed"][:20]:
            log.warning("  %s: %s", tk, err)

    # 3) 지수
    if not args.skip_indices and not args.tickers:
        log.info("== sync_indices ==")
        istats = sync.sync_indices(years=args.years, asof=args.asof)
        log.info("Indices: total=%d updated=%d no_change=%d failed=%d",
                 istats["total"], istats["updated"], istats["no_change"], len(istats["failed"]))
        for code, err in istats["failed"]:
            log.warning("  %s: %s", code, err)

    return 0


if __name__ == "__main__":
    sys.exit(main())
