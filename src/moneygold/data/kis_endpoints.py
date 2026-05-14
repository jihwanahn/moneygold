"""KIS Open API 엔드포인트 path + tr_id 매핑.

실전 키 전용. 모의 환경 미지원.
ARCHITECTURE.md §2 참조.
"""
from __future__ import annotations


class Path:
    """엔드포인트 path. base URL에 붙여서 사용."""

    OAUTH_TOKEN = "/oauth2/tokenP"
    OAUTH_REVOKE = "/oauth2/revokeP"

    # 국내주식 시세
    DAILY_CHART = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    SEARCH_STOCK_INFO = "/uapi/domestic-stock/v1/quotations/search-stock-info"
    INVESTOR_FLOW = "/uapi/domestic-stock/v1/quotations/inquire-investor"

    # 계좌 (read-only)
    BALANCE = "/uapi/domestic-stock/v1/trading/inquire-balance"


class TrId:
    """tr_id 헤더 값. 실전투자 기준."""

    DAILY_CHART = "FHKST03010100"
    SEARCH_STOCK_INFO = "CTPF1604R"
    INVESTOR_FLOW = "FHKST01010900"
    BALANCE = "TTTC8434R"


# KIS 응답 컬럼 → 내부 스키마 매핑 (일봉)
DAILY_BAR_FIELDS = {
    "stck_bsop_date": "date",     # YYYYMMDD 문자열
    "stck_oprc": "open",
    "stck_hgpr": "high",
    "stck_lwpr": "low",
    "stck_clpr": "close",
    "acml_vol": "volume",
    "acml_tr_pbmn": "value",       # 거래대금 KRW
}
