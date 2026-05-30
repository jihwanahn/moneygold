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
    mode.add_argument("--dividends", action="store_true",
                      help='"가속화 장기투자" 탭용 배당 이력 sync (pykrx 펀더멘털, 최근 N년)')
    mode.add_argument("--dart-indicators", action="store_true",
                      help='"가속화 장기투자" 탭용 DART 재무지표 sync (ROE 등, 최근 5년)')
    mode.add_argument("--dart-business", action="store_true",
                      help='DART 사업보고서 주요사항 sync (증자/감자, 자기주식, 회사정보, raw 재무제표)')
    mode.add_argument("--us-dividends", action="store_true",
                      help='"US 가속화 장기투자" 탭용 배당 이력 + info(yield/payout/ROE) sync (yfinance)')
    mode.add_argument("--us", action="store_true",
                      help="미국 시스템 전체 sync: 마스터 + 일봉 + 지수 + 분기재무 + 컨센서스")
    mode.add_argument("--us-kis-crosscheck", action="store_true",
                      help="KIS 해외주식 master 다운로드 → master.parquet에 tradable_kis 추가")

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
    parser.add_argument("--no-batch", action="store_true",
                        help="--us 모드에서 ticker별 fetch (느림). 기본은 yf.download 배치 (~5-10배 빠름).")
    parser.add_argument("--scope", choices=["all", "k200kq150", "sp500"], default="all",
                        help="종목 범위. k200kq150=KOSPI200+KOSDAQ150 (dart-business), "
                             "sp500=S&P500 (us-dividends).")
    parser.add_argument("--us-bars-period", default="5y",
                        help="--us-dividends 시 일봉 backfill 기간 (yfinance period, 기본 5y). "
                             "5년 주가 CAGR/200일선 비율 산출에 필요.")
    parser.add_argument(
        "--force-refresh-recent", type=int, default=None,
        help="최근 N거래일을 강제 다시 fetch + 덮어쓰기. "
             "휴장/부분집계로 잘못 저장된 행 정정용. "
             "기본: --daily/--indices 모드에서 5, 그 외 0. 명시 시 그 값 사용.",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("moneygold.cli.sync")

    # 모드 디폴트
    if not (args.universe or args.backfill or args.daily or args.indices or args.financials or args.consensus or args.dividends or args.dart_indicators or args.dart_business or args.us_dividends or args.us or args.us_kis_crosscheck):
        args.daily = True

    # force_refresh_recent 기본값:
    #   --daily / --indices : 5 (휴장 패딩/부분집계 자동 정정)
    #   --backfill          : 0 (전체 재 fetch 라 의미 없음)
    #   그 외                : 0
    if args.force_refresh_recent is None:
        if args.daily or args.indices:
            args.force_refresh_recent = 5
        else:
            args.force_refresh_recent = 0

    kis = KISClient(cfg.kis)
    sync = DataSync(kis, Path(cfg.data_dir))

    if args.us_kis_crosscheck:
        log.info("== KIS 해외주식 master 다운로드 + 교차검증 ==")
        stats = sync.sync_kis_tradable_us()
        log.info("결과: KIS 전체=%d, US master=%d, tradable=%d (%.1f%%)",
                 stats["kis_total"], stats["us_master"], stats["tradable"],
                 100.0 * stats["tradable"] / max(stats["us_master"], 1))
        return 0

    # US 배당 이력 + info + 5년 일봉 backfill ("US 가속화 장기투자" 탭용)
    if args.us_dividends:
        from datetime import datetime as _dt

        from ..data import us_dividends as ud

        asof = args.asof or _dt.now().strftime("%Y%m%d")
        # 종목: 명시 tickers > scope=sp500 (S&P500 구성) > master의 전체 US
        if args.tickers:
            tickers = _parse_tickers_arg(args.tickers) or []
        elif args.scope == "sp500":
            from ..universe_us import fetch_sp500_constituents
            log.info("S&P500 구성 종목 fetch...")
            sp = fetch_sp500_constituents()
            tickers = sp["ticker"].tolist() if not sp.empty else []
            log.info("→ %d 종목", len(tickers))
        else:
            master = sync.load_universe()
            tickers = master[master["market"] == "US"]["ticker"].tolist()
        if args.limit:
            tickers = tickers[: args.limit]

        # 1) 5년 일봉 backfill (5년 주가 CAGR / 200일선 비율에 필요)
        log.info("== US bars backfill (%d 종목, period=%s) ==", len(tickers), args.us_bars_period)
        bstats = sync.sync_bars_all_us(tickers, batch=not args.no_batch,
                                        period=args.us_bars_period)
        log.info("Bars: total=%d updated=%d no_change=%d failed=%d",
                 bstats["total"], bstats["updated"], bstats["no_change"], len(bstats["failed"]))

        # 2) 배당 이력 + info (yfinance)
        log.info("== sync_us_dividends (asof=%s, %d 종목) ==", asof, len(tickers))
        stats = ud.sync_us_dividends(Path(cfg.data_dir), tickers, asof=asof)
        log.info("US dividends: total=%d updated=%d no_data=%d failed=%d",
                 stats["total"], stats["updated"], stats["no_data"], len(stats["failed"]))
        for tk, err in stats["failed"][:10]:
            log.warning("  %s: %s", tk, err)
        return 0

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

        log.info("== US sync: 일봉 (%d 종목, batch=%s) ==", len(tickers), not args.no_batch)
        bars_stats = sync.sync_bars_all_us(tickers, batch=not args.no_batch)
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
        rows = list(zip(master["ticker"], master["market"], strict=False))
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

    # 펀더멘털만 — 마스터에서 시장 확인 후 KR(KIS)/US(SEC EDGAR)로 자동 라우팅
    if args.financials:
        log.info("== sync_financials (market-aware: KR→KIS, US→SEC EDGAR) ==")
        master = sync.load_universe()
        if args.tickers:
            wanted = set(_parse_tickers_arg(args.tickers) or [])
            master = master[master["ticker"].isin(wanted)]
        if args.limit:
            master = master.head(args.limit)
        kr_tickers = master[master["market"].isin(["KOSPI", "KOSDAQ"])]["ticker"].tolist()
        us_tickers = master[master["market"] == "US"]["ticker"].tolist()
        log.info("Target: KR=%d, US=%d (force=%s)",
                 len(kr_tickers), len(us_tickers), args.force_financials)
        if kr_tickers:
            log.info("-- KR via KIS finance --")
            stats = sync.sync_financials_for(kr_tickers, force=args.force_financials)
            log.info("KR Financials: total=%d updated=%d cached=%d failed=%d",
                     stats["total"], stats["updated"], stats["cached"], len(stats["failed"]))
            for tk, err in stats["failed"][:10]:
                log.warning("  %s: %s", tk, err)
        if us_tickers:
            log.info("-- US via SEC EDGAR XBRL --")
            stats = sync.sync_financials_us(us_tickers, force=args.force_financials)
            log.info("US Financials: total=%d updated=%d cached=%d no_cik=%d no_data=%d failed=%d",
                     stats["total"], stats["updated"], stats["cached"],
                     stats.get("no_cik", 0), stats.get("no_data", 0),
                     len(stats["failed"]))
            for tk, err in stats["failed"][:10]:
                log.warning("  %s: %s", tk, err)
        return 0

    # 배당 이력 ("가속화 장기투자" 탭용)
    if args.dividends:
        from datetime import datetime as _dt

        from ..data import dividends as div

        asof = args.asof or _dt.now().strftime("%Y%m%d")
        # DGI는 5년 CAGR + 10년 연속 인상까지 보므로 기본 11년.
        # --years 의 argparse 기본값 2(backfill용)와 충돌하므로 명시적으로 11로 덮어쓰고,
        # 사용자가 --years를 의도적으로 다르게 줬을 때만 그 값을 사용한다.
        years = args.years if args.years and args.years != 2 else 11
        log.info("== sync_dividends (asof=%s, years=%d) ==", asof, years)
        if args.tickers:
            # 사용자가 명시한 종목은 master에 없어도 그대로 사용 (master가 오래되거나 누락 가능)
            tickers = _parse_tickers_arg(args.tickers) or []
        else:
            master = sync.load_universe()
            master = master[master["market"].isin(["KOSPI", "KOSDAQ"])]
            if args.limit:
                master = master.head(args.limit)
            tickers = master["ticker"].tolist()
        log.info("Target: %d KR tickers (source=pykrx_batch)", len(tickers))
        stats = div.sync_dividends(Path(cfg.data_dir), tickers, asof=asof, years=years,
                                    source="pykrx_batch")
        log.info("Dividends: total=%d updated=%d no_data=%d failed=%d",
                 stats["total"], stats["updated"], stats["no_data"], len(stats["failed"]))
        for tk, err in stats["failed"][:10]:
            log.warning("  %s: %s", tk, err)
        return 0

    # DART 재무지표 ("가속화 장기투자" 펀더멘털 점수용)
    if args.dart_indicators:
        from datetime import datetime as _dt

        from ..data import dart_indicators as di
        from ..strategies.value_long_term.dart_client import DartClient

        asof = args.asof or _dt.now().strftime("%Y%m%d")
        years = args.years if args.years and args.years != 2 else 5
        log.info("== sync_dart_indicators (asof=%s, years=%d) ==", asof, years)
        if not cfg.dart.api_key:
            log.error("DART_API_KEY 미설정. .env에 키 추가 후 재실행.")
            return 1
        try:
            dart = DartClient(cfg.dart, Path(cfg.data_dir))
        except ValueError as e:
            log.error("DART 초기화 실패: %s", e)
            return 1
        if args.tickers:
            tickers = _parse_tickers_arg(args.tickers) or []
        elif args.scope == "k200kq150":
            from ..data import dart_business as db
            log.info("KOSPI200 + KOSDAQ150 구성 종목 fetch...")
            tickers = db.kospi200_kosdaq150_tickers(asof=asof)
            log.info("→ %d 종목", len(tickers))
        else:
            master = sync.load_universe()
            master = master[master["market"].isin(["KOSPI", "KOSDAQ"])]
            if args.limit:
                master = master.head(args.limit)
            tickers = master["ticker"].tolist()
        log.info("Target: %d KR tickers", len(tickers))
        stats = di.sync_dart_indicators(dart, Path(cfg.data_dir), tickers,
                                         asof=asof, years=years)
        log.info("DART indicators: total=%d updated=%d no_data=%d failed=%d",
                 stats["total"], stats["updated"], stats["no_data"], len(stats["failed"]))
        for tk, err in stats["failed"][:10]:
            log.warning("  %s: %s", tk, err)
        return 0

    # DART 사업보고서 주요사항 (증자/자사주/회사정보/raw 재무제표)
    if args.dart_business:
        from datetime import datetime as _dt

        from ..data import dart_business as db
        from ..strategies.value_long_term.dart_client import DartClient

        asof = args.asof or _dt.now().strftime("%Y%m%d")
        years = args.years if args.years and args.years != 2 else 5
        log.info("== sync_dart_business (asof=%s, years=%d, scope=%s) ==",
                 asof, years, args.scope)
        if not cfg.dart.api_key:
            log.error("DART_API_KEY 미설정. .env에 키 추가 후 재실행.")
            return 1
        try:
            dart = DartClient(cfg.dart, Path(cfg.data_dir))
        except ValueError as e:
            log.error("DART 초기화 실패: %s", e)
            return 1
        if args.tickers:
            tickers = _parse_tickers_arg(args.tickers) or []
        elif args.scope == "k200kq150":
            log.info("KOSPI200 + KOSDAQ150 구성 종목 fetch (pykrx)...")
            tickers = db.kospi200_kosdaq150_tickers(asof=asof)
            log.info("→ %d 종목 (KOSPI200 ∪ KOSDAQ150)", len(tickers))
        else:
            master = sync.load_universe()
            master = master[master["market"].isin(["KOSPI", "KOSDAQ"])]
            if args.limit:
                master = master.head(args.limit)
            tickers = master["ticker"].tolist()
        if args.limit:
            tickers = tickers[: args.limit]
        log.info("Target: %d tickers", len(tickers))
        stats = db.sync_dart_business(dart, Path(cfg.data_dir), tickers,
                                       asof=asof, years=years)
        log.info("DART business: total=%d updated=%d no_data=%d failed=%d",
                 stats["total"], stats["updated"], stats["no_data"], len(stats["failed"]))
        for tk, err in stats["failed"][:10]:
            log.warning("  %s: %s", tk, err)
        return 0

    # 지수만
    if args.indices:
        log.info("== sync_indices (force_refresh_recent=%d) ==", args.force_refresh_recent)
        stats = sync.sync_indices(
            years=args.years, asof=args.asof,
            force_refresh_recent=args.force_refresh_recent,
        )
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

    # --daily 모드 + 전체 KR 종목 (--tickers 지정 안 함) → pykrx batch 사용 (200배 빠름)
    # 다른 경우 (특정 ticker / --backfill) 는 종목별 backfill 유지
    use_kr_batch = (
        args.daily and not args.tickers and not args.limit
        and args.force_refresh_recent > 0
    )
    if use_kr_batch:
        log.info("== sync_bars_kr_pykrx_batch (KR 전체, recent_days=%d) ==",
                 args.force_refresh_recent)
        bs = sync.sync_bars_kr_pykrx_batch(
            recent_days=args.force_refresh_recent, asof=args.asof,
        )
        log.info("KR Bars (pykrx batch): touched=%d rows=%d days_fetched=%d errors=%d",
                 bs["tickers_touched"], bs["total_rows"], bs["days_fetched"], len(bs["errors"]))
        for k, v in bs["errors"][:10]:
            log.warning("  %s: %s", k, v)
        # US 종목 별도 처리 — yfinance batch (sync_bars_all_us 가 이미 batch 모드)
        us_tickers = [t for t in tickers if (master[master["ticker"]==t]["market"].iloc[0] == "US")] if not args.tickers else []
        if us_tickers:
            log.info("== sync_bars_all_us (US %d 종목, yfinance batch) ==", len(us_tickers))
            us_stats = sync.sync_bars_all_us(us_tickers, batch=True)
            log.info("US Bars: total=%d updated=%d no_change=%d failed=%d",
                     us_stats["total"], us_stats["updated"], us_stats["no_change"], len(us_stats["failed"]))
    else:
        log.info("== sync_bars_all (%d tickers, years=%d, asof=%s, force_refresh_recent=%d) ==",
                 len(tickers), args.years, args.asof or "today", args.force_refresh_recent)
        stats = sync.sync_bars_all(
            tickers, years=args.years, asof=args.asof,
            force_refresh_recent=args.force_refresh_recent,
        )
        log.info("Bars: total=%d updated=%d no_change=%d failed=%d",
                 stats["total"], stats["updated"], stats["no_change"], len(stats["failed"]))
        if stats["failed"]:
            log.warning("Failed tickers (first 20):")
            for tk, err in stats["failed"][:20]:
                log.warning("  %s: %s", tk, err)

    # 3) 지수 — --daily 도 pykrx batch 사용
    if not args.skip_indices and not args.tickers:
        if use_kr_batch:
            log.info("== sync_indices_kr_pykrx (recent_days=%d) ==", args.force_refresh_recent)
            istats = sync.sync_indices_kr_pykrx(recent_days=args.force_refresh_recent, asof=args.asof)
            log.info("Indices (pykrx): updated=%d no_change=%d failed=%d",
                     istats["updated"], istats["no_change"], len(istats["failed"]))
        else:
            log.info("== sync_indices (force_refresh_recent=%d) ==", args.force_refresh_recent)
            istats = sync.sync_indices(
                years=args.years, asof=args.asof,
                force_refresh_recent=args.force_refresh_recent,
            )
            log.info("Indices: total=%d updated=%d no_change=%d failed=%d",
                     istats["total"], istats["updated"], istats["no_change"], len(istats["failed"]))
        for code, err in istats["failed"]:
            log.warning("  %s: %s", code, err)

    return 0


if __name__ == "__main__":
    sys.exit(main())
