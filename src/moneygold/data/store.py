"""Parquet 스토어 헬퍼.

원자성: tmp + rename. 중복 거부: dedup_keys 기준 unique 강제.
ARCHITECTURE.md §2 무결성 규칙 참조.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pandas as pd


def read_parquet_safe(path: Path) -> pd.DataFrame | None:
    """파일 없거나 읽기 실패면 None."""
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        return None


def write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    """tmp 작성 후 atomic rename. 디렉터리 자동 생성."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def append_dedup(
    path: Path,
    new_df: pd.DataFrame,
    dedup_keys: Sequence[str],
    sort_keys: Sequence[str] | None = None,
) -> tuple[int, int]:
    """기존 parquet에 new_df를 합치고 dedup_keys 기준 중복 제거.

    Returns
    -------
    (rows_added, rows_skipped_as_dup)
        added = 최종 파일에 들어간 신규 행 수
        skipped = new_df 중 기존과 키 충돌로 버린 행 수
    """
    if new_df is None or new_df.empty:
        return 0, 0

    existing = read_parquet_safe(path)
    if existing is None or existing.empty:
        deduped_new = new_df.drop_duplicates(subset=list(dedup_keys), keep="first")
        combined = deduped_new.copy()
        added = len(combined)
        skipped = len(new_df) - added
    else:
        before = len(existing)
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=list(dedup_keys), keep="first")
        added = len(combined) - before
        skipped = len(new_df) - added

    if sort_keys:
        combined = combined.sort_values(list(sort_keys)).reset_index(drop=True)
    else:
        combined = combined.reset_index(drop=True)

    write_parquet_atomic(combined, path)
    return added, skipped


def bars_path(data_dir: Path, ticker: str) -> Path:
    return data_dir / "bars" / f"{ticker}.parquet"


def index_path(data_dir: Path, index_code: str) -> Path:
    return data_dir / "index" / f"{index_code}.parquet"


def master_path(data_dir: Path) -> Path:
    return data_dir / "meta" / "master.parquet"


def flags_path(data_dir: Path) -> Path:
    return data_dir / "meta" / "flags.parquet"
