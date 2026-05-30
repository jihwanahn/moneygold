"""Momentum Breakout 파라미터.

모든 매직넘버는 여기로. env 키는 ``MOMO_*`` 접두. CLAUDE.md 원칙: 동결 dataclass.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(key: str, default: int) -> int:
    v = os.getenv(key)
    return int(v) if v not in (None, "") else default


def _float(key: str, default: float) -> float:
    v = os.getenv(key)
    return float(v) if v not in (None, "") else default


def _str(key: str, default: str) -> str:
    v = os.getenv(key)
    return v if v not in (None, "") else default


def _opt_int(key: str) -> int | None:
    v = os.getenv(key)
    if v is None or v.strip() == "" or v.strip().lower() in ("none", "off", "null"):
        return None
    try:
        return int(v)
    except ValueError:
        return None


@dataclass(frozen=True)
class MomentumConfig:
    """진입·포지션 파라미터 묶음. 변경은 .env 또는 직접 생성자.

    Fields
    ------
    stop_loss_pct : 초기 손절 비율. 0.10 = entry × 0.90.
    profit_trigger_pct : INITIAL → TRAILING 전환 트리거. 0.20 = +20% 도달 시.
    new_high_lookback : 신고가 계산 lookback (영업일). 기본 60.
    fresh_window : "직전 N일 동안 위 신고가가 깨진 적 없음" — 반복 돌파 컷오프.
        구현: 오늘 종가가 직전 fresh_window 일 *최고 종가*를 처음 초과해야 통과.
    trailing_ma_period : TRAILING 상태에서 stop 갱신용 MA. 기본 20.
    top_n_value : 당일 거래대금 상위 N위 컷 (KR 그룹: KOSPI+KOSDAQ 통합). 기본 100.
    top_n_marketcap : 시가총액 상위 N위 컷 (KR 그룹). None = 미적용.
    top_n_value_us : 거래대금 상위 N위 컷 (US 그룹). 기본 **1000**.
        US는 일 거래대금이 메가캡에 극단적으로 쏠려, KR과 동일한 top-100 rank 컷을
        쓰면 후보가 시총 $148B+ 초대형주로만 수렴한다 (분석: top_n_value=100·mcap=100
        → 최근 5거래일 US 시그널 2건, Stage2+Template watchlist 와 교집합 0건). 그래서
        US는 rank 컷을 완화 (top 1000 ≈ 상위 ~31% 거래대금) 해 유동 중형주까지 포착.
        시장별 비대칭은 *수식 차이가 아니라 거래대금 분포 차이* 때문 — 엔진은 동일.
    top_n_marketcap_us : 시가총액 상위 N위 컷 (US 그룹). 기본 **None** (미적용).
        US는 시총 컷이 메가캡 쏠림을 악화시키므로 끈다. 유동성은 top_n_value_us 가 담당.
    volume_spike_ratio : 당일 거래대금 / 20일 평균 ≥ ratio. 기본 1.5. (시장 중립)
    volume_avg_window : volume_spike의 평균 윈도우 (영업일). 기본 20.
    min_listed_days : 신규상장 컷. 종목 bars 길이가 이 이하면 진입 후보 제외.
    gap_down_exit_policy : 'open' = 시가 청산, 'close' = 종가 청산.
        TRAILING 상태에서 시가가 stop 아래로 갭다운 시 어느 가격에 청산할지.
    """
    stop_loss_pct: float = 0.10
    profit_trigger_pct: float = 0.20
    new_high_lookback: int = 60
    fresh_window: int = 20
    trailing_ma_period: int = 20
    top_n_value: int = 100
    top_n_marketcap: int | None = 100
    top_n_value_us: int = 1000
    top_n_marketcap_us: int | None = None
    volume_spike_ratio: float = 1.5
    volume_avg_window: int = 20
    min_listed_days: int = 60
    gap_down_exit_policy: str = "open"   # "open" | "close"

    def top_n_for_group(self, group: tuple[str, ...]) -> tuple[int, int | None]:
        """시장 그룹별 (top_n_value, top_n_marketcap).

        US 그룹은 거래대금 분포가 메가캡에 쏠려 별도(완화된) 컷을 쓴다 — 위 필드 설명 참조.
        그 외(KR) 그룹은 공통 scalar 값. 수식은 시장 공통이고 *임계값만* 다르다.
        """
        if "US" in group:
            return self.top_n_value_us, self.top_n_marketcap_us
        return self.top_n_value, self.top_n_marketcap


def load_momentum_config() -> MomentumConfig:
    """env 에서 파라미터 로드. 미설정 키는 dataclass default."""
    return MomentumConfig(
        stop_loss_pct=_float("MOMO_STOP_LOSS_PCT", 0.10),
        profit_trigger_pct=_float("MOMO_PROFIT_TRIGGER_PCT", 0.20),
        new_high_lookback=_int("MOMO_NEW_HIGH_LOOKBACK", 60),
        fresh_window=_int("MOMO_FRESH_WINDOW", 20),
        trailing_ma_period=_int("MOMO_TRAILING_MA_PERIOD", 20),
        top_n_value=_int("MOMO_TOP_N_VALUE", 100),
        top_n_marketcap=_opt_int("MOMO_TOP_N_MARKETCAP"),
        top_n_value_us=_int("MOMO_TOP_N_VALUE_US", 1000),
        top_n_marketcap_us=_opt_int("MOMO_TOP_N_MARKETCAP_US"),
        volume_spike_ratio=_float("MOMO_VOLUME_SPIKE_RATIO", 1.5),
        volume_avg_window=_int("MOMO_VOLUME_AVG_WINDOW", 20),
        min_listed_days=_int("MOMO_MIN_LISTED_DAYS", 60),
        gap_down_exit_policy=_str("MOMO_GAP_DOWN_EXIT_POLICY", "open"),
    )
