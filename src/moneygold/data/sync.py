"""일일 incremental + 2년 백필 오케스트레이션.

- sync_universe: pykrx 마스터 갱신 → store/meta/master.parquet
- backfill_bars: 종목 단위 일봉 백필/incremental
- sync_bars_all: 마스터의 모든 보통주에 대해 백필 일괄 실행

지수(RS 분모) sync는 PR2에서 인디케이터와 함께. 본 모듈은 stock bars만.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from ..universe import fetch_master_from_pykrx, filter_master
from . import store
from .kis_client import KISAPIError, KISClient
from .. import fundamentals as fund
from . import yf_client as yfc

log = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    ticker: str
    added: int           # 새로 들어간 행 수
    skipped: int         # 중복으로 버린 행 수
    fetched: int         # KIS에서 받아온 행 수
    error: str | None = None


class DataSync:
    """KIS + 로컬 parquet 스토어를 묶어주는 오케스트레이터."""

    def __init__(self, kis: KISClient, data_dir: Path):
        self.kis = kis
        self.data_dir = Path(data_dir)

    # ----------- Universe -----------

    def sync_universe(self) -> pd.DataFrame:
        """pykrx로 마스터 받아와 필터 후 store/meta/master.parquet."""
        raw = fetch_master_from_pykrx()
        clean = filter_master(raw)
        path = store.master_path(self.data_dir)
        store.write_parquet_atomic(clean, path)
        log.info("Universe synced: %d tickers -> %s", len(clean), path)
        return clean

    def load_universe(self) -> pd.DataFrame:
        """저장된 마스터 로드. 없으면 raise."""
        df = store.read_parquet_safe(store.master_path(self.data_dir))
        if df is None or df.empty:
            raise FileNotFoundError(
                f"마스터 없음: {store.master_path(self.data_dir)} — 먼저 sync_universe() 실행"
            )
        return df

    # ----------- Bars -----------

    def backfill_bars(
        self,
        ticker: str,
        *,
        years: int = 2,
        asof: str | None = None,
    ) -> BackfillResult:
        """한 종목의 일봉을 [asof - years*365, asof] 범위로 incremental 백필.

        기존 parquet이 있으면 가장 최근 날짜 + 1일부터 받음.
        """
        end = asof or datetime.now().strftime("%Y%m%d")
        path = store.bars_path(self.data_dir, ticker)
        existing = store.read_parquet_safe(path)

        if existing is None or existing.empty:
            start_dt = datetime.strptime(end, "%Y%m%d") - timedelta(days=years * 365)
            start = start_dt.strftime("%Y%m%d")
        else:
            latest = str(existing["date"].max())
            next_dt = datetime.strptime(latest, "%Y%m%d") + timedelta(days=1)
            start = next_dt.strftime("%Y%m%d")
            if start > end:
                return BackfillResult(ticker, 0, 0, 0)

        try:
            df = self.kis.fetch_daily_bars(ticker, start, end)
        except KISAPIError as e:
            return BackfillResult(ticker, 0, 0, 0, error=f"KIS {e.rt_cd}: {e.msg}")
        except Exception as e:
            return BackfillResult(ticker, 0, 0, 0, error=str(e))

        fetched = len(df)
        if fetched == 0:
            return BackfillResult(ticker, 0, 0, 0)

        added, skipped = store.append_dedup(
            path, df, dedup_keys=["date"], sort_keys=["date"]
        )
        return BackfillResult(ticker, added=added, skipped=skipped, fetched=fetched)

    def sync_bars_all(
        self,
        tickers: list[str],
        *,
        years: int = 2,
        asof: str | None = None,
        progress: bool = True,
    ) -> dict:
        """주어진 종목 전체에 대해 backfill_bars. 통계 dict 반환."""
        stats = {"total": len(tickers), "updated": 0, "no_change": 0, "failed": []}
        it = tqdm(tickers, desc="bars sync", unit="tk") if progress else tickers
        for tk in it:
            r = self.backfill_bars(tk, years=years, asof=asof)
            if r.error:
                stats["failed"].append((tk, r.error))
            elif r.added > 0:
                stats["updated"] += 1
            else:
                stats["no_change"] += 1
        return stats

    # ----------- Index bars -----------

    def backfill_index(
        self,
        index_code: str,
        *,
        years: int = 2,
        asof: str | None = None,
    ) -> BackfillResult:
        """한 지수의 일봉을 incremental 백필. 종목과 같은 로직."""
        end = asof or datetime.now().strftime("%Y%m%d")
        path = store.index_path(self.data_dir, index_code)
        existing = store.read_parquet_safe(path)

        if existing is None or existing.empty:
            start_dt = datetime.strptime(end, "%Y%m%d") - timedelta(days=years * 365)
            start = start_dt.strftime("%Y%m%d")
        else:
            latest = str(existing["date"].max())
            next_dt = datetime.strptime(latest, "%Y%m%d") + timedelta(days=1)
            start = next_dt.strftime("%Y%m%d")
            if start > end:
                return BackfillResult(index_code, 0, 0, 0)

        try:
            df = self.kis.fetch_index_bars(index_code, start, end)
        except KISAPIError as e:
            return BackfillResult(index_code, 0, 0, 0, error=f"KIS {e.rt_cd}: {e.msg}")
        except Exception as e:
            return BackfillResult(index_code, 0, 0, 0, error=str(e))

        fetched = len(df)
        if fetched == 0:
            return BackfillResult(index_code, 0, 0, 0)

        added, skipped = store.append_dedup(
            path, df, dedup_keys=["date"], sort_keys=["date"]
        )
        return BackfillResult(index_code, added=added, skipped=skipped, fetched=fetched)

    def sync_indices(
        self,
        index_codes: list[str] | None = None,
        *,
        years: int = 2,
        asof: str | None = None,
    ) -> dict:
        """기본 4개 지수 (KOSPI/KOSDAQ/KOSPI200/KOSDAQ150) sync."""
        codes = index_codes or ["KOSPI", "KOSDAQ", "KOSPI200", "KOSDAQ150"]
        stats = {"total": len(codes), "updated": 0, "no_change": 0, "failed": []}
        for code in codes:
            r = self.backfill_index(code, years=years, asof=asof)
            if r.error:
                stats["failed"].append((code, r.error))
                log.warning("Index %s failed: %s", code, r.error)
            elif r.added > 0:
                stats["updated"] += 1
                log.info("Index %s: +%d rows", code, r.added)
            else:
                stats["no_change"] += 1
                log.info("Index %s: no change", code)
        return stats

    # ----------- US (yfinance) -----------

    def sync_universe_us(
        self,
        *,
        source: str = "sp500",
        enrich: bool = True,
        mcap_min_usd: float | None = None,
    ) -> pd.DataFrame:
        """미국 마스터를 master.parquet에 *append* (한국 기존 종목 유지).

        Parameters
        ----------
        source
            'sp500' (≈505) 또는 'nasdaq_trader' (NYSE+NASDAQ+AMEX 전종목).
        enrich
            yfinance Ticker.info로 sector/industry/mcap 보강.
        mcap_min_usd
            지정 시 enrich 단계에서 mcap < threshold 종목 제외.

        market='US' 컬럼으로 한국과 구분. 같은 ticker 충돌은 가정상 없음
        (한국 6자리 숫자 vs 미국 알파벳).
        """
        from .. import universe_us as uni_us
        us = uni_us.fetch_master_us(
            source=source, enrich=enrich, mcap_min_usd=mcap_min_usd,
        )
        path = store.master_path(self.data_dir)
        existing = store.read_parquet_safe(path)
        if existing is not None and not existing.empty:
            kept = existing[existing["market"] != "US"]
            combined = pd.concat([kept, us], ignore_index=True)
        else:
            combined = us
        # 컬럼 정합성 (kr에 industry 없어도 됨)
        for c in ["sector", "mcap", "industry"]:
            if c in combined.columns:
                combined[c] = combined[c].fillna(0 if c == "mcap" else "UNKNOWN")
        combined = combined.drop_duplicates(subset=["ticker"], keep="last").reset_index(drop=True)
        store.write_parquet_atomic(combined, path)
        log.info("US universe synced: %d tickers (total master rows %d)", len(us), len(combined))
        return us

    def sync_kis_tradable_us(self) -> dict:
        """KIS 해외주식 마스터로 master.parquet의 US 종목에 tradable_kis 컬럼 추가.

        Returns
        -------
        dict  {'kis_total': N, 'us_master': M, 'tradable': T}
            kis_total = KIS 마스터 총 종목 수 (3개 거래소 합)
            us_master = master.parquet 내 US 종목 수
            tradable = us_master 중 KIS-tradable로 매칭된 종목 수
        """
        from . import kis_overseas_master as kis_om

        path = store.master_path(self.data_dir)
        existing = store.read_parquet_safe(path)
        if existing is None or existing.empty:
            log.error("master.parquet 없음 — universe sync 먼저.")
            return {"kis_total": 0, "us_master": 0, "tradable": 0}

        kis_df = kis_om.fetch_kis_overseas_all()
        annotated = kis_om.annotate_tradable_kis(existing, kis_df)
        store.write_parquet_atomic(annotated, path)

        n_us = int((annotated["market"] == "US").sum())
        n_tradable = int(((annotated["market"] == "US") & annotated["tradable_kis"]).sum())
        stats = {
            "kis_total": len(kis_df),
            "us_master": n_us,
            "tradable": n_tradable,
        }
        log.info("KIS tradable 동기화: %s", stats)
        return stats

    def backfill_bars_us(self, ticker: str, *, period: str = "2y") -> BackfillResult:
        """yfinance로 일봉 받아서 store/bars/{ticker}.parquet."""
        try:
            df = yfc.fetch_daily_bars(ticker, period=period)
        except Exception as e:
            return BackfillResult(ticker, 0, 0, 0, error=str(e))
        if df is None or df.empty:
            return BackfillResult(ticker, 0, 0, 0)
        added, skipped = store.append_dedup(
            store.bars_path(self.data_dir, ticker), df,
            dedup_keys=["date"], sort_keys=["date"],
        )
        return BackfillResult(ticker, added=added, skipped=skipped, fetched=len(df))

    def sync_bars_all_us(
        self,
        tickers: list[str],
        *,
        period: str = "2y",
        progress: bool = True,
        batch: bool = True,
        batch_size: int = 50,
    ) -> dict:
        """US 종목 일봉 sync.

        Parameters
        ----------
        batch
            True (기본)면 ``yf.download`` 멀티 ticker 배치 호출 — threaded HTTP로
            ticker별 호출 대비 5-10배 빠름. False면 기존 per-ticker 호출.
        batch_size
            batch 모드에서 1회 호출 ticker 개수 (메모리/실패 risk 트레이드오프).
        """
        stats = {"total": len(tickers), "updated": 0, "no_change": 0, "failed": []}

        if not batch:
            it = tqdm(tickers, desc="us bars", unit="tk") if progress else tickers
            for tk in it:
                r = self.backfill_bars_us(tk, period=period)
                if r.error:
                    stats["failed"].append((tk, r.error))
                elif r.added > 0:
                    stats["updated"] += 1
                else:
                    stats["no_change"] += 1
            return stats

        # Batch 모드: ticker별 fetch 대신 yf.download 멀티
        unique = list(dict.fromkeys(tickers))
        batches = [unique[i : i + batch_size] for i in range(0, len(unique), batch_size)]
        it = tqdm(batches, desc="us bars batch", unit="batch") if progress else batches
        for batch_list in it:
            try:
                dfs = yfc.fetch_daily_bars_batch(
                    batch_list, period=period, batch_size=batch_size,
                )
            except Exception as e:
                for tk in batch_list:
                    stats["failed"].append((tk, f"batch error: {e}"))
                continue

            for tk in batch_list:
                df = dfs.get(tk)
                if df is None or df.empty:
                    stats["failed"].append((tk, "empty"))
                    continue
                try:
                    added, _ = store.append_dedup(
                        store.bars_path(self.data_dir, tk), df,
                        dedup_keys=["date"], sort_keys=["date"],
                    )
                except Exception as e:
                    stats["failed"].append((tk, str(e)))
                    continue
                if added > 0:
                    stats["updated"] += 1
                else:
                    stats["no_change"] += 1
        return stats

    def sync_indices_us(self, codes: list[str] | None = None, *, period: str = "2y") -> dict:
        codes = codes or ["^GSPC", "^IXIC", "^RUT"]
        stats = {"total": len(codes), "updated": 0, "no_change": 0, "failed": []}
        for code in codes:
            try:
                df = yfc.fetch_index_bars(code, period=period)
                if df is None or df.empty:
                    stats["failed"].append((code, "empty"))
                    continue
                added, _ = store.append_dedup(
                    store.index_path(self.data_dir, code), df,
                    dedup_keys=["date"], sort_keys=["date"],
                )
                if added > 0:
                    stats["updated"] += 1
                    log.info("Index %s: +%d rows", code, added)
                else:
                    stats["no_change"] += 1
            except Exception as e:
                stats["failed"].append((code, str(e)))
        return stats

    def sync_financials_us(
        self,
        tickers: list[str],
        *,
        force: bool = False,
        progress: bool = True,
    ) -> dict:
        """yfinance 분기 손익 (이미 단독값이라 정규화 불필요)."""
        stats = {"total": len(tickers), "updated": 0, "cached": 0, "failed": []}
        it = tqdm(tickers, desc="us financials", unit="tk") if progress else tickers
        for tk in it:
            path = fund.financials_path(self.data_dir, tk)
            if path.exists() and not force:
                stats["cached"] += 1
                continue
            try:
                df = yfc.fetch_quarterly_financials(tk)
                if df is None or df.empty:
                    stats["failed"].append((tk, "empty"))
                    continue
                store.write_parquet_atomic(df, path)
                stats["updated"] += 1
            except Exception as e:
                stats["failed"].append((tk, str(e)))
        return stats

    # ----------- Fundamentals (KIS finance 엔드포인트) -----------

    def sync_financials_for(
        self,
        tickers: list[str],
        *,
        force: bool = False,
        progress: bool = True,
    ) -> dict:
        """종목별 분기 펀더멘털 sync. 종목당 2회 KIS 호출 (income stmt + financial ratio).

        force=True면 캐시 무시하고 재 호출. False면 캐시 있는 종목 skip.
        """
        stats = {"total": len(tickers), "updated": 0, "cached": 0, "failed": []}
        it = tqdm(tickers, desc="financials", unit="tk") if progress else tickers
        for tk in it:
            path = fund.financials_path(self.data_dir, tk)
            if path.exists() and not force:
                stats["cached"] += 1
                continue
            try:
                r = fund.fetch_and_cache(self.kis, self.data_dir, tk, force=force)
                if r.error:
                    stats["failed"].append((tk, r.error))
                elif not r.quarters.empty:
                    stats["updated"] += 1
                else:
                    stats["failed"].append((tk, "empty quarters"))
            except Exception as e:
                stats["failed"].append((tk, str(e)))
        return stats
