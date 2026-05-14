"""환경변수 로딩 + 설정 dataclass. ARCHITECTURE.md §13 참조."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _int(key: str, default: int) -> int:
    v = os.getenv(key)
    return int(v) if v not in (None, "") else default


def _float(key: str, default: float) -> float:
    v = os.getenv(key)
    return float(v) if v not in (None, "") else default


def _bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class KISConfig:
    app_key: str
    app_secret: str
    account_no: str
    account_prod_cd: str
    base_url: str = "https://openapi.koreainvestment.com:9443"
    token_cache_path: Path = Path.home() / ".kis_token.json"


@dataclass(frozen=True)
class SizingConfig:
    default_equity_krw: int
    max_risk_per_trade_pct: float
    max_position_weight_pct: float
    max_positions: int


@dataclass(frozen=True)
class UniverseFilter:
    liquidity_min_krw: int
    mcap_min_krw: int


@dataclass(frozen=True)
class StrategyParams:
    stage2_require_inst_flow: bool
    rs_rank_min: int
    sma200_slope_lookback: int
    box_high_lookback: int
    box_high_confirm: int
    box_height_max_pct: float
    box_valid_min_days: int
    box_stale_days: int
    breakout_buffer: float
    breakout_volume_mult: float
    gap_buy_on_pullback: bool
    fundamental_required: bool


@dataclass(frozen=True)
class NotifyConfig:
    channels: tuple[str, ...]
    slack_webhook_url: str
    telegram_bot_token: str
    telegram_chat_id: str


@dataclass(frozen=True)
class AppConfig:
    kis: KISConfig
    sizing: SizingConfig
    universe: UniverseFilter
    strategy: StrategyParams
    notify: NotifyConfig
    mcp_server: str
    data_dir: Path
    result_dir: Path
    log_level: str
    timezone: str
    benchmark_index: str


def load_config() -> AppConfig:
    return AppConfig(
        kis=KISConfig(
            app_key=_str("KIS_APP_KEY"),
            app_secret=_str("KIS_APP_SECRET"),
            account_no=_str("KIS_ACCOUNT_NO"),
            account_prod_cd=_str("KIS_ACCOUNT_PROD_CD", "01"),
        ),
        sizing=SizingConfig(
            default_equity_krw=_int("DEFAULT_EQUITY_KRW", 10_000_000),
            max_risk_per_trade_pct=_float("MAX_RISK_PER_TRADE_PCT", 1.0),
            max_position_weight_pct=_float("MAX_POSITION_WEIGHT_PCT", 20.0),
            max_positions=_int("MAX_POSITIONS", 10),
        ),
        universe=UniverseFilter(
            liquidity_min_krw=_int("LIQUIDITY_MIN_KRW", 1_000_000_000),
            mcap_min_krw=_int("MCAP_MIN_KRW", 50_000_000_000),
        ),
        strategy=StrategyParams(
            stage2_require_inst_flow=_bool("STAGE2_REQUIRE_INST_FLOW", True),
            rs_rank_min=_int("RS_RANK_MIN", 70),
            sma200_slope_lookback=_int("SMA200_SLOPE_LOOKBACK", 100),
            box_high_lookback=_int("BOX_HIGH_LOOKBACK", 20),
            box_high_confirm=_int("BOX_HIGH_CONFIRM", 3),
            box_height_max_pct=_float("BOX_HEIGHT_MAX_PCT", 12.0),
            box_valid_min_days=_int("BOX_VALID_MIN_DAYS", 15),
            box_stale_days=_int("BOX_STALE_DAYS", 60),
            breakout_buffer=_float("BREAKOUT_BUFFER", 0.003),
            breakout_volume_mult=_float("BREAKOUT_VOLUME_MULT", 1.5),
            gap_buy_on_pullback=_bool("GAP_BUY_ON_PULLBACK", True),
            fundamental_required=_bool("FUNDAMENTAL_REQUIRED", False),
        ),
        notify=NotifyConfig(
            channels=tuple(c.strip() for c in _str("NOTIFY_CHANNEL", "console").split(",") if c.strip()),
            slack_webhook_url=_str("SLACK_WEBHOOK_URL"),
            telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        ),
        mcp_server=_str("MCP_SERVER", "korea-stock-analyzer"),
        data_dir=Path(_str("DATA_DIR", "./store")),
        result_dir=Path(_str("RESULT_DIR", "./result")),
        log_level=_str("LOG_LEVEL", "INFO"),
        timezone=_str("TIMEZONE", "Asia/Seoul"),
        benchmark_index=_str("BENCHMARK_INDEX", "KOSPI"),
    )
