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
    *,
    keep: str = "first",
) -> tuple[int, int]:
    """기존 parquet에 new_df를 합치고 dedup_keys 기준 중복 제거.

    Parameters
    ----------
    keep : 'first' (기본) — 기존 행 우선. 신규 데이터와 키 충돌 시 기존 유지.
           'last'         — 신규 행 우선. *덮어쓰기* 시맨틱.
        주의: 'last' 사용 시 *과거에 잘못 저장된 행*을 새 fetch 로 정정 가능.
        반대로 'first' 는 한 번 저장된 행이 영영 update 안 됨 — 데이터 무결성
        사고 (예: 휴장 패딩, 부분 집계) 시 영구 보존 위험.

    Returns
    -------
    (rows_added, rows_skipped_as_dup)
        added = 최종 파일에 들어간 신규 행 수 (keep='last' 시엔 *덮어쓴 행 포함*)
        skipped = new_df 중 dedup 으로 빠진 행 수
    """
    if new_df is None or new_df.empty:
        return 0, 0

    existing = read_parquet_safe(path)
    if existing is None or existing.empty:
        deduped_new = new_df.drop_duplicates(subset=list(dedup_keys), keep=keep)
        combined = deduped_new.copy()
        added = len(combined)
        skipped = len(new_df) - added
    else:
        before = len(existing)
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=list(dedup_keys), keep=keep)
        added = len(combined) - before
        skipped = len(new_df) - added

    if sort_keys:
        combined = combined.sort_values(list(sort_keys)).reset_index(drop=True)
    else:
        combined = combined.reset_index(drop=True)

    write_parquet_atomic(combined, path)
    return added, skipped


def upsert_dedup(
    path: Path,
    new_df: pd.DataFrame,
    dedup_keys: Sequence[str],
    sort_keys: Sequence[str] | None = None,
) -> tuple[int, int]:
    """append_dedup 의 keep='last' alias — 신규 데이터로 *덮어쓰기*.

    데이터 정정/치유 용도. 예: 휴장 패딩으로 잘못 저장된 행을 새 fetch 로 교체.
    """
    return append_dedup(path, new_df, dedup_keys, sort_keys, keep="last")


def bars_path(data_dir: Path, ticker: str) -> Path:
    return data_dir / "bars" / f"{ticker}.parquet"


def index_path(data_dir: Path, index_code: str) -> Path:
    return data_dir / "index" / f"{index_code}.parquet"


def master_path(data_dir: Path) -> Path:
    return data_dir / "meta" / "master.parquet"


def flags_path(data_dir: Path) -> Path:
    return data_dir / "meta" / "flags.parquet"
