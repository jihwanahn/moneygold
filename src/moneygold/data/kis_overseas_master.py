"""KIS 해외주식 마스터 파일 다운로드 + 파싱.

KIS는 매일 NAS / NYS / AMS 시장의 주문 가능 종목 목록을 .mst.zip 파일로 배포.
이 모듈은 그 파일을 받아 ticker 목록으로 변환해, NASDAQ Trader 기반 universe와
*교차검증*해 'tradable_kis' 플래그를 master.parquet에 추가하는 데 쓴다.

파일 형식 (tab-separated, cp949):
    col 1: 국가코드 (US)
    col 3: 거래소코드 (NAS/NYS/AMS)
    col 4: 거래소명 (한글)
    col 5: ticker (e.g., AACB, BRK A — 클래스주는 공백)
    col 7: 한글명
    col 8: 영문명
    ... (총 24컬럼; 13=기준가, 16-17=거래시간 등)

KIS는 ticker가 'BRK A' 같이 공백을 쓰지만 yfinance는 'BRK-B' 형식 → 정규화 필요.

ARCHITECTURE.md 데이터 무결성: 다운로드 실패 / 형식 변경 시 raise.
"""
from __future__ import annotations

import io
import logging
import zipfile
from typing import Literal

import pandas as pd
import requests

log = logging.getLogger(__name__)


_MASTER_URL_TEMPLATE = "https://new.real.download.dws.co.kr/common/master/{name}.zip"
# (KIS exchange code, file basename)
_KIS_EXCHANGES: dict[str, str] = {
    "NAS": "nasmst.cod",
    "NYS": "nysmst.cod",
    "AMS": "amsmst.cod",
}
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"
)


def _normalize_ticker(raw: str) -> str:
    """KIS ticker → yfinance/NASDAQ Trader 호환 형식.

    KIS는 클래스주를 'BRK A' (공백) 또는 'BRK.A' (점) 등으로 표기.
    NASDAQ Trader / yfinance는 'BRK-B' (하이픈) 형식.
    """
    s = str(raw).strip()
    return s.replace(" ", "-").replace(".", "-")


def fetch_kis_overseas_listed(
    exchange: Literal["NAS", "NYS", "AMS"],
) -> pd.DataFrame:
    """단일 거래소 KIS master 파일을 다운로드해 ticker 목록 반환.

    Returns
    -------
    DataFrame  columns = ['ticker', 'name_en', 'name_kr', 'kis_exchange']
        ticker는 yfinance 호환 형식 (BRK A → BRK-A).
    """
    if exchange not in _KIS_EXCHANGES:
        raise ValueError(f"Unknown exchange: {exchange!r} (allowed: {list(_KIS_EXCHANGES)})")
    filename = _KIS_EXCHANGES[exchange]
    url = _MASTER_URL_TEMPLATE.format(name=filename)

    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"KIS master download 실패 ({exchange}): {e}") from e

    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
            if not names:
                raise RuntimeError(f"KIS master zip 비어있음: {exchange}")
            with zf.open(names[0]) as f:
                raw_bytes = f.read()
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"KIS master zip 손상: {exchange}: {e}") from e

    # cp949 → utf-8, tab-separated
    text = raw_bytes.decode("cp949", errors="replace")
    df = pd.read_csv(
        io.StringIO(text), sep="\t", header=None, dtype=str,
        on_bad_lines="skip", encoding=None,
    )
    if df.shape[1] < 8:
        raise RuntimeError(
            f"KIS master {exchange} 컬럼 수 비정상: {df.shape[1]} < 8"
        )

    out = pd.DataFrame({
        "ticker": df.iloc[:, 4].map(_normalize_ticker),
        "name_kr": df.iloc[:, 6].fillna("").astype(str).str.strip(),
        "name_en": df.iloc[:, 7].fillna("").astype(str).str.strip(),
        "kis_exchange": exchange,
    })
    # 공백/NaN ticker 제거
    out = out[out["ticker"].str.len() > 0].drop_duplicates(subset=["ticker"]).reset_index(drop=True)
    log.info("KIS %s: %d tickers", exchange, len(out))
    return out


def fetch_kis_overseas_all() -> pd.DataFrame:
    """3개 거래소(NAS/NYS/AMS) master 합친 단일 DataFrame.

    Returns
    -------
    DataFrame  ['ticker', 'name_en', 'name_kr', 'kis_exchange']
        ticker는 거래소 간 unique. 충돌 시 NAS > NYS > AMS 우선 (kept='first').
    """
    parts = []
    for exch in ("NAS", "NYS", "AMS"):
        try:
            parts.append(fetch_kis_overseas_listed(exch))
        except Exception as e:
            log.warning("KIS %s 마스터 fetch 실패, 건너뜀: %s", exch, e)
    if not parts:
        raise RuntimeError("KIS overseas master 모두 fetch 실패")
    out = pd.concat(parts, ignore_index=True)
    out = out.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    log.info("KIS overseas total: %d tickers", len(out))
    return out


def annotate_tradable_kis(
    master: pd.DataFrame,
    kis_tickers: pd.DataFrame,
) -> pd.DataFrame:
    """master.parquet에 'tradable_kis' 컬럼 추가.

    Parameters
    ----------
    master
        기존 universe master. 'market' 컬럼이 'US'/'KOSPI'/'KOSDAQ' 등.
    kis_tickers
        ``fetch_kis_overseas_all()`` 결과.

    Returns
    -------
    master + 'tradable_kis' (bool):
        - US 시장: ticker가 KIS 마스터에 있으면 True, 없으면 False.
        - 한국 시장: True (KIS 국내 API로 항상 주문 가능 가정).
    """
    out = master.copy()
    kis_set = set(kis_tickers["ticker"].astype(str))
    is_us = out["market"] == "US"
    # 시작값을 명시적 bool dtype 으로 (KR=True, US는 아래에서 덮어쓰기)
    tradable = pd.Series(False, index=out.index, dtype=bool)
    tradable.loc[~is_us] = True  # KR 등 비-US는 기본 True
    tradable.loc[is_us] = out.loc[is_us, "ticker"].isin(kis_set).values
    out["tradable_kis"] = tradable
    return out
