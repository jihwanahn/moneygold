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
    mode.add_argument("--us", action="store_true",
                      help="미국 시스템 전체 sync: 마스터 + 일봉 + 지수 + 분기재무 + 컨센서스")

    parser.add_argument("--tickers", help="특정 종목만. 콤마 구분. 예: 005930,000660")
    parser.add_argument("--limit", type=int, help="첫 N개 종목만 (디버그)")
    parser.add_argument("--years", type=int, default=2, help="백필 연수 (기본 2)")
    parser.add_argument("--asof", help="기준일 YYYYMMDD. 기본은 오늘.")
    parser.add_argument("--skip-indices", action="store_true", help="지수 sync 건너뛰기")
    parser.add_argument("--force-financials", action="store_true",
                        help="--financials 모드에서 캐시 무시 후 재 다운로드")
    parser.add_argument("--force-consensus", action="store_true",
                        help="--consensus 모드에서 캐시 무시 후 재 다운로드")
    parser.add_argument("--us-source", choices=["sp500", "nasdaq_trader"], default=None,
                        help="--us 모드 종목 소스. 기본은 config의 US_SOURCE.")
    parser.add_argument("--us-mcap-min", type=float, default=None,
                        help="--us 모드 mcap 컷오프 (USD). 기본은 config의 US_MCAP_MIN_USD.")
    args = parser.parse_args(argv)

    cfg = load_config()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("moneygold.cli.sync")

    # 모드 디폴트
    if not (args.universe or args.backfill or args.daily or args.indices or args.financials or args.consensus or args.us):
        args.daily = True

    kis = KISClient(cfg.kis)
    sync = DataSync(kis, Path(cfg.data_dir))

    # 미국 시스템 전체 sync (yfinance 단독)
    if args.us:
        from .. import consensus as cons_mod
        us_source = args.us_source or cfg.universe.us_source
        us_mcap_min = (
            args.us_mcap_min if args.us_mcap_min is not None
            else cfg.universe.us_mcap_min_usd
        )
        log.info("== US sync: 마스터 (source=%s, mcap_min=$%.0fM) ==",
                 us_source, us_mcap_min / 1e6)
        us_master = sync.sync_universe_us(
            source=us_source, mcap_min_usd=us_mcap_min,
        )
        tickers = us_master["ticker"].tolist()
        if args.limit:
            tickers = tickers[: args.limit]

        log.info("== US sync: 지수 (^GSPC, ^IXIC, ^RUT) ==")
        idx_stats = sync.sync_indices_us()
        log.info("Indices: %s", idx_stats)

        log.info("== US sync: 일봉 (%d 종목) ==", len(tickers))
        bars_stats = sync.sync_bars_all_us(tickers)
        log.info("Bars: total=%d updated=%d no_change=%d failed=%d",
                 bars_stats["total"], bars_stats["updated"],
                 bars_stats["no_change"], len(bars_stats["failed"]))

        log.info("== US sync: 분기 재무 ==")
        fin_stats = sync.sync_financials_us(tickers, force=args.force_financials)
        log.info("Financials: total=%d updated=%d cached=%d failed=%d",
                 fin_stats["total"], fin_stats["updated"],
                 fin_stats["cached"], len(fin_stats["failed"]))

        log.info("== US sync: 컨센서스 ==")
        # consensus는 yfinance라 _yf_symbol에서 .KS/.KQ 추가하면 안 됨 — US는 그대로
        # 임시 우회: market='US'를 빈 suffix로 처리하는 별도 호출 필요
        # 일단 yf_symbol을 활용하기 위해 US 종목들도 동일 함수 호출
        # consensus._yf_symbol("AAPL", "US") → "AAPL." → 잘못됨
        # → consensus.fetch_consensus는 한국 .KS/.KQ 가정. US용 wrapper 필요.
        # 임시: ticker 그대로 fetch + 캐싱
        rows = [(t, "US") for t in tickers]
        cons_stats = cons_mod.sync_consensus_for(
            Path(cfg.data_dir), rows, force=args.force_consensus,
        )
        log.info("Consensus: %s", cons_stats)
        return 0

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
