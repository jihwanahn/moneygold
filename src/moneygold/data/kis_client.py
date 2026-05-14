"""KIS Open API 클라이언트.

책임:
  - OAuth 토큰 발급/캐시 (파일 영속) + 만료 임박 시 prefetch
  - Rate limit (20 req/s 토큰 버킷, 안전 마진 15 req/s 목표)
  - 401 발생 시 토큰 재발급 후 1회 재시도
  - 일봉 페이지네이션 (기간 자르기): KIS는 한 호출에 최근 100영업일치만 반환

자동 주문 API는 의도적으로 노출하지 않음 (ARCHITECTURE §0).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from ..config import KISConfig
from . import kis_endpoints as ep

log = logging.getLogger(__name__)


# ============================================================
# Token cache
# ============================================================

@dataclass
class _Token:
    access_token: str
    expires_at: float   # epoch seconds
    issued_at: float
    key_hash: str


def _app_key_hash(app_key: str) -> str:
    return hashlib.sha256(app_key.encode("utf-8")).hexdigest()[:16]


def _load_token(path: Path, expected_key_hash: str) -> _Token | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        tok = _Token(
            access_token=raw["access_token"],
            expires_at=float(raw["expires_at"]),
            issued_at=float(raw["issued_at"]),
            key_hash=raw["key_hash"],
        )
    except Exception:
        return None
    if tok.key_hash != expected_key_hash:
        return None
    return tok


def _save_token(path: Path, tok: _Token) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({
        "access_token": tok.access_token,
        "expires_at": tok.expires_at,
        "issued_at": tok.issued_at,
        "key_hash": tok.key_hash,
    }, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ============================================================
# Rate limiter (token bucket, deque-based)
# ============================================================

class _RateLimiter:
    """1초 window 내 최대 N건 허용. 초과 시 sleep."""

    def __init__(self, max_per_sec: int = 15, window_sec: float = 1.0):
        self.max_per_sec = max_per_sec
        self.window = window_sec
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            while self.calls and self.calls[0] <= now - self.window:
                self.calls.popleft()
            if len(self.calls) >= self.max_per_sec:
                sleep_for = self.window - (now - self.calls[0]) + 0.01
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                while self.calls and self.calls[0] <= now - self.window:
                    self.calls.popleft()
            self.calls.append(time.monotonic())


# ============================================================
# Client
# ============================================================

class KISAPIError(Exception):
    def __init__(self, rt_cd: str, msg: str, payload: dict | None = None):
        super().__init__(f"KIS rt_cd={rt_cd} msg={msg!r}")
        self.rt_cd = rt_cd
        self.msg = msg
        self.payload = payload or {}


class KISClient:
    """KIS API HTTP 클라이언트.

    Parameters
    ----------
    cfg : KISConfig
        config.load_config().kis 를 전달
    rate_per_sec : int
        초당 최대 호출 수. KIS 공식은 20, 안전 마진 15 권장.
    request_timeout : float
        HTTP 요청 timeout(초).
    """

    def __init__(
        self,
        cfg: KISConfig,
        rate_per_sec: int = 15,
        request_timeout: float = 15.0,
    ):
        if not cfg.app_key or not cfg.app_secret:
            raise ValueError("KIS_APP_KEY / KIS_APP_SECRET 미설정")
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "moneygold/0.1"})
        self.timeout = request_timeout
        self.limiter = _RateLimiter(max_per_sec=rate_per_sec)
        self._key_hash = _app_key_hash(cfg.app_key)
        self._token: _Token | None = _load_token(cfg.token_cache_path, self._key_hash)
        self._token_lock = threading.Lock()

    # ---------- OAuth ----------

    def _issue_token(self) -> _Token:
        """토큰 발급. 1분당 1회 제한이 있어 issued_at 기반 throttle."""
        # 기존 토큰이 60초 이내 발급된 거면 대기
        if self._token and (time.time() - self._token.issued_at) < 60:
            wait = 60 - (time.time() - self._token.issued_at) + 1
            log.info("Token re-issue cooldown: sleeping %.1fs", wait)
            time.sleep(wait)
        url = self.cfg.base_url + ep.Path.OAUTH_TOKEN
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret,
        }
        r = self.session.post(url, json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if "access_token" not in data:
            raise RuntimeError(f"OAuth 실패: {data}")
        now = time.time()
        # expires_in (sec) 또는 access_token_token_expired (yyyy-MM-dd HH:mm:ss)
        if "expires_in" in data:
            expires_at = now + float(data["expires_in"])
        elif "access_token_token_expired" in data:
            try:
                expires_at = datetime.strptime(
                    data["access_token_token_expired"], "%Y-%m-%d %H:%M:%S"
                ).timestamp()
            except ValueError:
                expires_at = now + 86400
        else:
            expires_at = now + 86400
        tok = _Token(
            access_token=data["access_token"],
            expires_at=expires_at,
            issued_at=now,
            key_hash=self._key_hash,
        )
        _save_token(self.cfg.token_cache_path, tok)
        log.info("KIS token issued, expires in %.1f hours", (expires_at - now) / 3600)
        return tok

    def _ensure_token(self) -> _Token:
        """만료 1시간 전까지는 캐시 사용. 그 외 재발급."""
        with self._token_lock:
            now = time.time()
            if self._token and (self._token.expires_at - now) > 3600:
                return self._token
            self._token = self._issue_token()
            return self._token

    def _auth_headers(self, tr_id: str) -> dict[str, str]:
        tok = self._ensure_token()
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {tok.access_token}",
            "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    # ---------- HTTP ----------

    def _get(self, path: str, tr_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """rate-limit → GET → 401(토큰만료) / 429(rate) / 5xx(일시) 재시도. KIS 응답 dict 반환.

        최대 5회 시도, 5xx는 지수 백오프 (1, 2, 4초).
        """
        url = self.cfg.base_url + path
        max_attempts = 5
        backoff_5xx = [1.0, 2.0, 4.0, 8.0]

        last_err: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            self.limiter.acquire()
            headers = self._auth_headers(tr_id)
            try:
                r = self.session.get(url, headers=headers, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = e
                wait = backoff_5xx[min(attempt - 1, len(backoff_5xx) - 1)]
                log.warning("network error on attempt %d: %s, retrying in %.1fs", attempt, e, wait)
                time.sleep(wait)
                continue

            if r.status_code == 401:
                log.warning("401 from KIS, re-issuing token (attempt %d)", attempt)
                with self._token_lock:
                    self._token = self._issue_token()
                continue
            if r.status_code == 429:
                log.warning("429 rate limit (attempt %d), backing off 2s", attempt)
                time.sleep(2.0)
                continue
            if 500 <= r.status_code < 600:
                wait = backoff_5xx[min(attempt - 1, len(backoff_5xx) - 1)]
                log.warning("%d from KIS (attempt %d), retrying in %.1fs", r.status_code, attempt, wait)
                time.sleep(wait)
                continue

            r.raise_for_status()
            data = r.json()
            rt_cd = str(data.get("rt_cd", ""))
            if rt_cd not in ("0", ""):
                raise KISAPIError(rt_cd, str(data.get("msg1", "")), data)
            return data

        if last_err is not None:
            raise last_err
        raise RuntimeError(f"KIS GET {path} failed after {max_attempts} attempts")

    # ---------- Daily bars ----------

    def fetch_daily_bars(
        self,
        ticker: str,
        start: str,
        end: str,
        market_div: str = "J",
        adjusted: bool = True,
    ) -> pd.DataFrame:
        """`[start, end]` 기간의 일봉을 페이지네이션으로 모두 수집.

        KIS는 한 호출에 최근 100영업일만 반환. 가장 오래된 응답일 - 1일을
        다음 호출의 FID_INPUT_DATE_2로 설정해 반복.

        Returns
        -------
        DataFrame  columns = ['ticker', 'date', 'open', 'high', 'low', 'close', 'volume', 'value', 'adj_factor']
                   date는 YYYYMMDD 문자열, 가격은 int, 거래대금은 int (KRW).
                   비어있으면 empty DataFrame.
        """
        if start > end:
            return _empty_bars_df()

        all_rows: list[dict[str, Any]] = []
        cur_end = end
        seen_oldest = None  # 무한 루프 가드

        while True:
            params = {
                "FID_COND_MRKT_DIV_CODE": market_div,
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": start,
                "FID_INPUT_DATE_2": cur_end,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0" if adjusted else "1",
            }
            data = self._get(ep.Path.DAILY_CHART, ep.TrId.DAILY_CHART, params)
            rows = data.get("output2", []) or []
            if not rows:
                break
            # output2는 최신→과거 순
            all_rows.extend(rows)
            oldest = rows[-1].get("stck_bsop_date")
            if not oldest or oldest <= start:
                break
            if seen_oldest is not None and oldest >= seen_oldest:
                log.warning("Pagination didn't advance (ticker=%s oldest=%s), aborting", ticker, oldest)
                break
            seen_oldest = oldest
            # 다음 호출은 oldest - 1일을 종료일로
            cur_end = _prev_day(oldest)
            if cur_end < start:
                break

        return _normalize_bars(ticker, all_rows, start_inclusive=start, end_inclusive=end)


# ============================================================
# Normalization helpers (모듈 함수, 단위 테스트 용이)
# ============================================================

def _empty_bars_df() -> pd.DataFrame:
    return pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close", "volume", "value", "adj_factor"])


def _normalize_bars(ticker: str, rows: list[dict[str, Any]], start_inclusive: str, end_inclusive: str) -> pd.DataFrame:
    """KIS output2 list → 표준 DataFrame.

    중복 (같은 날짜) 제거, [start, end] 범위로 클립, 날짜 오름차순 정렬.
    숫자 변환 실패한 행은 버림.
    """
    if not rows:
        return _empty_bars_df()
    df = pd.DataFrame(rows)
    keep = list(ep.DAILY_BAR_FIELDS)
    keep = [c for c in keep if c in df.columns]
    df = df[keep].rename(columns=ep.DAILY_BAR_FIELDS).copy()

    # 숫자 변환
    for c in ("open", "high", "low", "close", "volume", "value"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"])
    # 정수형 (volume/value는 매우 클 수 있어 Int64)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype("int64")
    for c in ("volume", "value"):
        if c in df.columns:
            df[c] = df[c].fillna(0).astype("int64")

    df["date"] = df["date"].astype(str)
    df["ticker"] = ticker
    df["adj_factor"] = 1.0  # KIS 수정주가 반환 시 adj_factor는 placeholder

    # 범위 클립 + 중복 제거 + 정렬
    df = df[(df["date"] >= start_inclusive) & (df["date"] <= end_inclusive)]
    df = df.drop_duplicates(subset=["date"], keep="first")
    df = df.sort_values("date").reset_index(drop=True)

    return df[["ticker", "date", "open", "high", "low", "close", "volume", "value", "adj_factor"]]


def _prev_day(yyyymmdd: str) -> str:
    """YYYYMMDD 문자열에서 -1일 (캘린더). 주말은 KIS가 알아서 빈 응답으로 처리."""
    d = datetime.strptime(yyyymmdd, "%Y%m%d") - timedelta(days=1)
    return d.strftime("%Y%m%d")
