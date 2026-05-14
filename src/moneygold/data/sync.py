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
