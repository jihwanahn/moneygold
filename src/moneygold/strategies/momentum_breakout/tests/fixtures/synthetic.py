"""테스트용 합성 OHLCV 빌더.

bars 컬럼: date(YYYYMMDD str), open, high, low, close, volume, value.
date 는 영업일이 아니라도 됨 (단조 증가 정렬만 보장).
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd


def _date_range(start_yyyymmdd: str, n: int) -> list[str]:
    d0 = date(int(start_yyyymmdd[:4]), int(start_yyyymmdd[4:6]), int(start_yyyymmdd[6:8]))
    out = []
    for i in range(n):
        d = d0 + timedelta(days=i)
        out.append(d.strftime("%Y%m%d"))
    return out


def make_bars(
    closes: list[float] | np.ndarray,
    volumes: list[float] | np.ndarray | None = None,
    start: str = "20250101",
    ticker: str = "TEST",
) -> pd.DataFrame:
    """단순 close 시리즈에서 bars DataFrame 생성.

    high = close × 1.005, low = close × 0.995, open = 직전 close (첫 봉은 close).
    volume 미지정 시 1_000_000 고정.
    value = close × volume.
    """
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    if volumes is None:
        volumes = np.full(n, 1_000_000.0)
    volumes = np.asarray(volumes, dtype=float)

    dates = _date_range(start, n)
    high = closes * 1.005
    low = closes * 0.995
    opens = np.concatenate([[closes[0]], closes[:-1]])
    value = closes * volumes

    return pd.DataFrame({
        "ticker": [ticker] * n,
        "date": dates,
        "open": opens,
        "high": high,
        "low": low,
        "close": closes,
        "volume": volumes,
        "value": value,
    })


def trending_then_breakout(
    *,
    n_pre: int = 100,
    pre_level: float = 100.0,
    pre_amplitude: float = 5.0,
    breakout_close: float = 110.0,
    breakout_volume_mult: float = 2.0,
    seed: int = 42,
    start: str = "20250101",
    ticker: str = "BREAK",
) -> pd.DataFrame:
    """결정적 "초기 피크 → 횡보 → 돌파" 합성.

    프로필:
      - 첫 1/3 봉: pre_level + pre_amplitude × {0, 0.3, 0.6, 1.0, 0.6, 0.3, 0} 패턴 (피크 형성).
      - 중간 1/3 봉: pre_level 근처에서 ±작은 노이즈 (seed 결정적). 피크 미초과.
      - 마지막 1/3 봉: pre_level - 0.5 근처 평탄 (직전 fresh_window 봉이 신고가 깨지 않게).
      - 최후 1봉: ``breakout_close``.

    이 패턴은 "피크가 lookback 범위 안에 있지만 fresh_window 안에는 없는" 전형적
    consolidation → breakout 시나리오를 재현. random 시드와 무관하게 fresh_window 안
    내부 돌파가 없도록 보장.
    """
    rng = np.random.default_rng(seed)
    n_peak = max(7, n_pre // 3)
    n_mid = max(1, n_pre // 3)
    n_tail = n_pre - n_peak - n_mid
    if n_tail < 0:
        n_tail = 0

    # 1) 피크 형성 (시작부분)
    peak_profile = pre_level + pre_amplitude * np.linspace(0, 1, n_peak // 2 + 1)
    peak_profile = np.concatenate([peak_profile, peak_profile[-2::-1]])
    peak_profile = peak_profile[:n_peak]

    # 2) 중간 — 피크 아래 노이즈
    mid_noise = pre_level + rng.uniform(-pre_amplitude * 0.3, pre_amplitude * 0.3, size=n_mid)
    mid_noise = np.minimum(mid_noise, pre_level + pre_amplitude - 0.5)  # 피크 미초과 보장

    # 3) 꼬리 — 직전 fresh_window 영역. pre_level 아래로 안정.
    tail = np.full(n_tail, pre_level - 0.5)

    pre = np.concatenate([peak_profile, mid_noise, tail])
    # 안전장치 — 모든 pre 값은 breakout_close 보다 작아야
    pre = np.minimum(pre, breakout_close - 0.01)

    closes = np.concatenate([pre, [breakout_close]])

    vols = np.full(len(closes), 1_000_000.0)
    vols[-1] = 1_000_000.0 * breakout_volume_mult
    return make_bars(closes, volumes=vols, start=start, ticker=ticker)
