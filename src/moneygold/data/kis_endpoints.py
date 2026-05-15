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
    INDEX_DAILY_CHART = "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"

    # 국내주식 재무 (분기/연간)
    FINANCE_INCOME_STMT = "/uapi/domestic-stock/v1/finance/income-statement"
    FINANCE_BALANCE_SHEET = "/uapi/domestic-stock/v1/finance/balance-sheet"
    FINANCE_RATIO = "/uapi/domestic-stock/v1/finance/financial-ratio"
    FINANCE_PROFIT_RATIO = "/uapi/domestic-stock/v1/finance/profit-ratio"

    # 계좌 (read-only)
    BALANCE = "/uapi/domestic-stock/v1/trading/inquire-balance"


class TrId:
    """tr_id 헤더 값. 실전투자 기준."""

    DAILY_CHART = "FHKST03010100"
    SEARCH_STOCK_INFO = "CTPF1604R"
    INVESTOR_FLOW = "FHKST01010900"
    INDEX_DAILY_CHART = "FHKUP03500100"
    BALANCE = "TTTC8434R"
    # Finance
    FINANCE_INCOME_STMT = "FHKST66430200"
    FINANCE_BALANCE_SHEET = "FHKST66430100"
    FINANCE_RATIO = "FHKST66430300"
    FINANCE_PROFIT_RATIO = "FHKST66430400"


# KIS 지수 코드 (FID_INPUT_ISCD).
# 시장 구분 코드(FID_COND_MRKT_DIV_CODE)는 'U' (업종지수).
INDEX_CODES = {
    "KOSPI": "0001",
    "KOSDAQ": "1001",
    "KOSPI200": "2001",
    "KOSDAQ150": "2003",
}


# 지수 일봉 응답 컬럼 → 내부 스키마. 주식과 컬럼명 다름.
INDEX_BAR_FIELDS = {
    "stck_bsop_date": "date",
    "bstp_nmix_oprc": "open",
    "bstp_nmix_hgpr": "high",
    "bstp_nmix_lwpr": "low",
    "bstp_nmix_prpr": "close",
    "acml_vol": "volume",
    "acml_tr_pbmn": "value",
}


# KIS 주식 일봉 응답 컬럼 → 내부 스키마
DAILY_BAR_FIELDS = {
    "stck_bsop_date": "date",     # YYYYMMDD 문자열
    "stck_oprc": "open",
    "stck_hgpr": "high",
    "stck_lwpr": "low",
    "stck_clpr": "close",
    "acml_vol": "volume",
    "acml_tr_pbmn": "value",       # 거래대금 KRW
}
