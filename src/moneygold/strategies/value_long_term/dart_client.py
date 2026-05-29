"""DART (금감원 전자공시) OpenAPI client.

자사주 취득/소각 공시 + 사업보고서 자사주 보유비율 추출. "가속화 장기투자" 점수표의
"주주환원" 10점 산출에 사용.

원본: /mnt/d/01_Career/ValueTrader/src/dart_api.py (Apr 2026). 포팅 변경점:
  - moneygold 패턴 준수: config.DartConfig 주입, logging 사용, datetime.now() 제거.
  - 모든 조회 메서드가 명시적 ``asof: str`` (YYYYMMDD)를 받아 재현성 확보 (CLAUDE.md 원칙).
  - 캐시 경로를 ``data_dir / "dart_cache"`` 하위로 통합.

DART 공시유형 코드 (이 클라이언트가 사용하는 것):
  pblntf_detail_ty='B001'  자기주식취득결정
  pblntf_ty='A'            정기공시 (사업보고서 검색용)
  report_nm '소각' 포함     자기주식 소각 공시 (정확한 코드가 없어 이름 기반)
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import requests

from ...config import DartConfig

log = logging.getLogger(__name__)


class DartQuotaExceeded(RuntimeError):
    """DART 일일 호출 한도(~10,000/일) 초과. status=020. 한국 시간 자정 reset."""


def _asof_to_date(asof: str) -> datetime:
    """'YYYYMMDD' → datetime. asof 인자는 모든 조회 메서드의 *기준일*."""
    return datetime.strptime(asof, "%Y%m%d")


class DartClient:
    """DART OpenAPI 박스. corp_code 매핑은 메모리 + 디스크 캐시.

    모든 메서드는 ``asof: str`` (YYYYMMDD) 기준일을 받아 결정론적으로 동작한다.
    실시간 클럭 (``datetime.now()``) 사용 금지 — CLI 진입점에서 1회 주입.
    """

    def __init__(self, cfg: DartConfig, data_dir: Path):
        if not cfg.api_key:
            raise ValueError(
                "DART_API_KEY가 비어 있습니다. .env에 키를 설정하거나 "
                "DGI 점수표의 '주주환원' 항목을 NaN으로 두세요."
            )
        self.cfg = cfg
        self.cache_dir = data_dir / "dart_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._corp_map: dict | None = None
        self._last_call = 0.0
        self._min_interval = 1.0 / max(cfg.rate_per_sec, 1)

    # ------------------------------------------------------------------
    # Low-level
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        delta = time.time() - self._last_call
        if delta < self._min_interval:
            time.sleep(self._min_interval - delta)
        self._last_call = time.time()

    def _get_json(self, endpoint: str, params: dict) -> dict:
        self._throttle()
        params = {**params, "crtfc_key": self.cfg.api_key}
        r = requests.get(f"{self.cfg.base_url}/{endpoint}", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        # DART 일일 한도 초과 (~10K/일 개인키). 더 부르면 자원만 소모하므로 즉시 abort.
        if data.get("status") == "020":
            raise DartQuotaExceeded(
                f"DART 일일 한도 초과 (endpoint={endpoint}). "
                "한국 시간 자정 이후 재시도. 응답: " + str(data.get("message"))
            )
        return data

    def _get_binary(self, endpoint: str, params: dict, timeout: int = 30) -> requests.Response:
        self._throttle()
        params = {**params, "crtfc_key": self.cfg.api_key}
        r = requests.get(f"{self.cfg.base_url}/{endpoint}", params=params, timeout=timeout)
        r.raise_for_status()
        return r
    # ------------------------------------------------------------------
    # Corp code mapping (전체 종목 한 번에 zip으로 받음 — 캐시 강력)
    # ------------------------------------------------------------------

    def _corp_code_cache_path(self) -> Path:
        return self.cache_dir / "corp_codes.json"

    def _load_corp_map(self) -> dict:
        if self._corp_map is not None:
            return self._corp_map
        cache_path = self._corp_code_cache_path()
        if cache_path.exists():
            with cache_path.open(encoding="utf-8") as f:
                self._corp_map = json.load(f)
            return self._corp_map

        log.info("DART corpCode.xml 다운로드 (전체 종목 매핑)")
        r = self._get_binary("corpCode.xml", {})
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            with z.open("CORPCODE.xml") as xf:
                tree = ET.parse(xf)
        mapping: dict[str, dict] = {}
        for node in tree.getroot().findall("list"):
            stock = (node.findtext("stock_code") or "").strip()
            corp = (node.findtext("corp_code") or "").strip()
            name = (node.findtext("corp_name") or "").strip()
            if stock and corp:
                mapping[stock] = {"corp_code": corp, "corp_name": name}
        # atomic write
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False)
        tmp.replace(cache_path)
        self._corp_map = mapping
        return mapping

    def corp_code(self, ticker: str) -> str | None:
        """6자리 종목코드 → DART 8자리 corp_code. 미등록 종목이면 None."""
        rec = self._load_corp_map().get(ticker.zfill(6))
        return rec["corp_code"] if rec else None

    # ------------------------------------------------------------------
    # Disclosure listing
    # ------------------------------------------------------------------

    def list_disclosures(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        pblntf_detail_ty: str | None = None,
        pblntf_ty: str | None = None,
    ) -> list[dict]:
        """list.json 페이지네이션 전부 합쳐 반환. status=013(데이터 없음)이면 빈 리스트."""
        results: list[dict] = []
        page_no = 1
        while True:
            params: dict = {
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "page_no": page_no,
                "page_count": 100,
            }
            if pblntf_detail_ty:
                params["pblntf_detail_ty"] = pblntf_detail_ty
            if pblntf_ty:
                params["pblntf_ty"] = pblntf_ty
            data = self._get_json("list.json", params)
            status = data.get("status")
            if status == "013":
                break
            if status != "000":
                log.debug("DART list.json status=%s msg=%s", status, data.get("message"))
                break
            results.extend(data.get("list", []))
            if page_no >= int(data.get("total_page", 1) or 1):
                break
            page_no += 1
        return results

    # ------------------------------------------------------------------
    # 사업보고서 주요사항 (증자/감자, 자기주식, 회사정보, raw 재무제표)
    # 모두 사업보고서(reprt_code=11011) 단위로 fetch. annual 5년치 표준.
    # ------------------------------------------------------------------

    def fetch_share_issuance(self, ticker: str, bsns_year: int,
                              reprt_code: str = "11011") -> list[dict]:
        """증자(감자) 현황 ``irdsSttus.json``.

        rows: 일자(``isu_dcrs_de``), 형식(``isu_dcrs_stle``), 주식종류, 신주발행가, 신주수 등.
        empty list if no data or 권한 부족.
        """
        corp = self.corp_code(ticker)
        if not corp:
            return []
        try:
            data = self._get_json("irdsSttus.json", {
                "corp_code": corp, "bsns_year": str(bsns_year),
                "reprt_code": reprt_code,
            })
        except requests.HTTPError:
            return []
        if data.get("status") != "000":
            return []
        return data.get("list", []) or []

    def fetch_treasury_status(self, ticker: str, bsns_year: int,
                               reprt_code: str = "11011") -> list[dict]:
        """자기주식 취득 및 처분 현황 ``tesstkAcqsDspsSttus.json``.

        분기·반기·연 단위로 재고/취득/처분 흐름. 보고서별 누적.
        """
        corp = self.corp_code(ticker)
        if not corp:
            return []
        try:
            data = self._get_json("tesstkAcqsDspsSttus.json", {
                "corp_code": corp, "bsns_year": str(bsns_year),
                "reprt_code": reprt_code,
            })
        except requests.HTTPError:
            return []
        if data.get("status") != "000":
            return []
        return data.get("list", []) or []

    def fetch_company_info(self, ticker: str) -> dict | None:
        """회사 기본정보 ``company.json``: corp_name, ceo_nm, est_dt, induty_code 등.

        한 번 받으면 거의 변경 없음. corp_code 발급 후 캐시 영속 가능.
        """
        corp = self.corp_code(ticker)
        if not corp:
            return None
        try:
            data = self._get_json("company.json", {"corp_code": corp})
        except requests.HTTPError:
            return None
        if data.get("status") != "000":
            return None
        return {k: v for k, v in data.items() if k not in ("status", "message")}

    def fetch_financial_statements_all(
        self, ticker: str, bsns_year: int,
        reprt_code: str = "11011", fs_div: str = "CFS",
    ) -> list[dict]:
        """전체 재무제표 ``fnlttSinglAcntAll.json``.

        fs_div: 'CFS' = 연결재무제표, 'OFS' = 별도재무제표. 연결 우선.
        rows: 재무상태표/손익계산서/현금흐름표/자본변동표의 모든 계정.
        """
        corp = self.corp_code(ticker)
        if not corp:
            return []
        try:
            data = self._get_json("fnlttSinglAcntAll.json", {
                "corp_code": corp, "bsns_year": str(bsns_year),
                "reprt_code": reprt_code, "fs_div": fs_div,
            })
        except requests.HTTPError:
            return []
        if data.get("status") != "000":
            # CFS가 없으면 OFS로 fallback
            if fs_div == "CFS":
                return self.fetch_financial_statements_all(
                    ticker, bsns_year, reprt_code, fs_div="OFS",
                )
            return []
        return data.get("list", []) or []

    # ------------------------------------------------------------------
    # 배당 결정 공시 카운트 (분기/반기/연 배당 빈도 추정용)
    # ------------------------------------------------------------------

    def dividend_decisions_per_year(
        self, ticker: str, asof: str, years: int = 2,
    ) -> float | None:
        """``asof`` 기준 직전 ``years``년 '현금ㆍ현물배당결정' 공시의 연평균 건수.

        DGI 점수표 "분기/월 배당 빈도" (5점)의 입력. 정정/취소/자회사 공시 제외.
        값 해석:
            ≥ 4   분기배당 또는 월배당 (분기 4회, 월 12회)
            ~ 2   반기배당
            ~ 1   연 결산만
            0     배당 없음

        모르겠으면 None (corp_code 매핑 실패 등).
        캐시: ``store/dart_cache/dividend_decisions/{ticker}_{asof}_{years}y.json``
        """
        cached = self._read_cache(f"dividend_decisions/{ticker}_{asof}_{years}y.json")
        if cached is not None:
            return cached.get("per_year")

        corp = self.corp_code(ticker)
        if not corp:
            self._write_cache(f"dividend_decisions/{ticker}_{asof}_{years}y.json", {"per_year": None})
            return None

        end = _asof_to_date(asof)
        bgn = end - timedelta(days=365 * years)
        bgn_s, end_s = bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d")

        all_disc = self.list_disclosures(corp, bgn_s, end_s)
        # report_nm == '현금ㆍ현물배당결정' 정확히 매치. 자회사·정정·취소 제외.
        decisions = [
            d for d in all_disc
            if (d.get("report_nm") or "").strip() == "현금ㆍ현물배당결정"
        ]
        per_year = round(len(decisions) / max(years, 1), 2)
        self._write_cache(
            f"dividend_decisions/{ticker}_{asof}_{years}y.json",
            {"per_year": per_year, "count": len(decisions), "years": years},
        )
        return per_year

    # ------------------------------------------------------------------
    # 재무지표 (fnlttSinglIndx.json)
    # ------------------------------------------------------------------

    # 보고서 코드. DART 정기공시 분류.
    REPORT_Q1 = "11013"
    REPORT_H1 = "11012"
    REPORT_Q3 = "11014"
    REPORT_ANNUAL = "11011"

    # 지표분류 코드.
    IDX_PROFITABILITY = "M210000"   # 수익성 — ROE, ROA, 영업이익률 등
    IDX_STABILITY = "M220000"        # 안정성 — 부채비율, 유동비율 등
    IDX_GROWTH = "M230000"           # 성장성 — 매출증가율 등
    IDX_ACTIVITY = "M240000"         # 활동성 — 자산회전율 등

    def fetch_financial_indicators(
        self,
        ticker: str,
        bsns_year: int,
        idx_cl_code: str = IDX_PROFITABILITY,
        reprt_code: str = REPORT_ANNUAL,
    ) -> list[dict]:
        """단일 종목의 재무지표 fetch (fnlttSinglIndx.json).

        Returns
        -------
        list of dict — DART list 응답 그대로. 주요 키:
            idx_cl_code  : 지표분류 (M210000 등)
            idx_cl_nm    : 지표분류명 (수익성지표 등)
            idx_nm       : 지표명 (한글, 예: 'ROE')
            idx_val      : 지표값 (str, '8.999' 또는 '-' 등)

        해당 corp_code/year/reprt에 데이터가 없으면 빈 리스트.
        """
        corp = self.corp_code(ticker)
        if not corp:
            return []
        try:
            data = self._get_json("fnlttSinglIndx.json", {
                "corp_code": corp,
                "bsns_year": str(bsns_year),
                "reprt_code": reprt_code,
                "idx_cl_code": idx_cl_code,
            })
        except requests.HTTPError as e:
            log.debug("[%s] DART fnlttSinglIndx %d failed: %s", ticker, bsns_year, e)
            return []
        if data.get("status") != "000":
            return []
        return data.get("list", []) or []

    # ------------------------------------------------------------------
    # 자사주 활동 (취득 / 소각)
    # ------------------------------------------------------------------

    def treasury_activity(self, ticker: str, asof: str, years: int = 3) -> dict:
        """``asof`` 기준 직전 ``years``년 자사주 공시 카운트.

        Returns dict:
            years_window         : 윈도우 길이(년)
            acquire_count        : 자기주식 취득결정(B001) 건수
            cancel_count         : 자기주식 소각 공시 건수 (report_nm '소각' 포함, '[기재정정]' 제외)
            latest_cancel_date   : 가장 최근 소각 공시 일자(YYYYMMDD) 또는 None
            cancel_reports       : 최대 10건의 (rcept_dt, report_nm, rcept_no)

        캐시: ``store/dart_cache/treasury_activity/{ticker}_{asof}_{years}y.json``
        """
        cached = self._read_cache(f"treasury_activity/{ticker}_{asof}_{years}y.json")
        if cached is not None:
            return cached

        corp = self.corp_code(ticker)
        empty = {
            "years_window": years, "acquire_count": 0, "cancel_count": 0,
            "latest_cancel_date": None, "cancel_reports": [],
        }
        if not corp:
            self._write_cache(f"treasury_activity/{ticker}_{asof}_{years}y.json", empty)
            return empty

        end = _asof_to_date(asof)
        bgn = end - timedelta(days=365 * years)
        bgn_s, end_s = bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d")

        acquire = self.list_disclosures(corp, bgn_s, end_s, pblntf_detail_ty="B001")
        all_disc = self.list_disclosures(corp, bgn_s, end_s)
        cancel = [
            d for d in all_disc
            if "소각" in (d.get("report_nm") or "")
            and "[기재정정]" not in (d.get("report_nm") or "")
        ]
        cancel_sorted = sorted(cancel, key=lambda d: d.get("rcept_dt", ""), reverse=True)
        result = {
            "years_window": years,
            "acquire_count": len(acquire),
            "cancel_count": len(cancel),
            "latest_cancel_date": cancel_sorted[0]["rcept_dt"] if cancel_sorted else None,
            "cancel_reports": [
                {"rcept_dt": d["rcept_dt"], "report_nm": d["report_nm"], "rcept_no": d["rcept_no"]}
                for d in cancel_sorted[:10]
            ],
        }
        self._write_cache(f"treasury_activity/{ticker}_{asof}_{years}y.json", result)
        return result

    # ------------------------------------------------------------------
    # 자사주 보유 비율 (사업보고서 document.xml)
    # ------------------------------------------------------------------

    def _latest_business_report_rcept(self, corp_code: str, asof: str) -> str | None:
        """``asof`` 기준 직전 2년 사업보고서 rcept_no. 정정본('[기재정정]')은 제외 우선."""
        end = _asof_to_date(asof)
        bgn = end - timedelta(days=365 * 2)
        data = self._get_json("list.json", {
            "corp_code": corp_code,
            "bgn_de": bgn.strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"),
            "pblntf_ty": "A",
            "page_no": 1,
            "page_count": 50,
        })
        if data.get("status") != "000":
            return None
        items = data.get("list", [])
        for item in items:
            nm = item.get("report_nm") or ""
            if "사업보고서" in nm and "[기재정정]" not in nm:
                return item.get("rcept_no")
        return items[0]["rcept_no"] if items else None

    def treasury_holding_pct(self, ticker: str, asof: str) -> float | None:
        """자사주 보유비율(%) = SUM_TRS_STK / (SUM_TRS_STK + SUM_FLT_STK) × 100.

        DART 사업보고서 document.xml의 '주식의 총수 현황' 표에서 ACODE 속성으로 추출.
        직접 비율 ACODE='SUM_TRS_RT'가 있으면 우선 사용. 추출 실패 시 None.

        캐시: ``store/dart_cache/treasury_holding/{ticker}_{asof}.json``
        """
        cache_key = f"treasury_holding/{ticker}_{asof}.json"
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached.get("ratio_pct")

        corp = self.corp_code(ticker)
        if not corp:
            self._write_cache(cache_key, {"ratio_pct": None})
            return None
        rcept = self._latest_business_report_rcept(corp, asof)
        if not rcept:
            self._write_cache(cache_key, {"ratio_pct": None})
            return None

        try:
            r = self._get_binary("document.xml", {"rcept_no": rcept})
        except requests.HTTPError:
            self._write_cache(cache_key, {"ratio_pct": None})
            return None

        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                names = [n for n in z.namelist() if n.endswith(".xml")]
                main = f"{rcept}.xml"
                ordered = ([main] if main in names else []) + [n for n in names if n != main]
                raw: str | None = None
                for n in ordered:
                    text = z.read(n).decode("utf-8", errors="replace")
                    if "SUM_TRS_STK" in text or "TRS_STK" in text:
                        raw = text
                        break
        except zipfile.BadZipFile:
            self._write_cache(cache_key, {"ratio_pct": None})
            return None
        if raw is None:
            self._write_cache(cache_key, {"ratio_pct": None})
            return None

        ratio = _parse_treasury_ratio(raw)
        self._write_cache(cache_key, {"ratio_pct": ratio})
        return ratio

    # ------------------------------------------------------------------
    # Cache helpers (JSON, atomic)
    # ------------------------------------------------------------------

    def _read_cache(self, rel_path: str) -> dict | None:
        path = self.cache_dir / rel_path
        if not path.exists():
            return None
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, rel_path: str, payload: dict) -> None:
        path = self.cache_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp.replace(path)


# ============================================================
# Pure parsing helpers (testable without network)
# ============================================================

_TE_PATTERN = re.compile(r'<TE[^>]*ACODE="([A-Z0-9_]+)"[^>]*>([^<]*)</TE>')


def _parse_treasury_ratio(raw: str) -> float | None:
    """document.xml 텍스트에서 자사주 보유비율(%) 추출. 순수 함수, 네트워크 무관."""
    acode: dict[str, str] = {}
    for m in _TE_PATTERN.finditer(raw):
        acode.setdefault(m.group(1), m.group(2).strip())

    rt = acode.get("SUM_TRS_RT", "").replace(",", "")
    if rt and rt != "-":
        try:
            return round(float(rt), 3)
        except ValueError:
            pass

    def _int_or_none(key: str) -> int | None:
        v = acode.get(key, "").replace(",", "")
        if v in ("", "-"):
            return 0 if key in acode else None
        try:
            return int(v)
        except ValueError:
            return None

    treasury = _int_or_none("SUM_TRS_STK")
    floating = _int_or_none("SUM_FLT_STK")
    issued = _int_or_none("ISU_STK2") or _int_or_none("SUM_ISU_STK2")
    if treasury is None:
        return None
    denom: int | None = None
    # floating>0 조건이 핵심: SUM_FLT_STK='-'(0) 또는 0이면 분모가 treasury만 되어
    # 비율이 100%로 튀므로, issued로 fallback.
    if floating is not None and floating > 0:
        denom = treasury + floating
    elif issued and issued > 0:
        denom = issued
    if not denom:
        return None
    return round(treasury / denom * 100, 3)
