from __future__ import annotations

from constants import RESULT_DIR

import os
import time
import json
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import re

import numpy as np
import pandas as pd
import dart_fss as dart
from pykrx import stock
from tqdm import tqdm

import pdb

# =========================
# Config
# =========================

DART_API_KEY = "다트 키"
dart.set_api_key(DART_API_KEY)

DART_BASE = "https://opendart.fss.or.kr/api"
SESSION = requests.Session()

# 요청 과다 방지 (상황에 따라 0.2~0.5 권장)
REQ_SLEEP = 0.25


REPRT_FALLBACK = ["11011", "11012", "11014", "11013"] 

REPRT_CODE_TO_TYPE = {
    "11011": "사업보고서",    # Annual
    "11012": "반기보고서",    # Semiannual
    "11013": "1분기보고서",   # Q1
    "11014": "3분기보고서",   # Q3
}

REVENUE_KEYS = [
    "매출액",
    "영업수익",
    "수익(매출)",
    "수익(매출액)",
    # 금융권/보험권에서 자주 등장
    "이자수익",
    "수수료수익",
    "보험료수익",
]

OP_KEYS = [
    "영업이익",
    "영업이익(손실)",
]

NI_KEYS = [
    "지배기업 소유주 지분",
    "지배기업의 소유주 지분",

    "당기순이익",
    "당기순이익(손실)",
    "연결당기순이익",
    "분기순이익",
    "반기순이익",
]

CAPEX_KEYS = [
    "유형자산의 취득",
    "유형자산취득",
    "유형자산의 구입",
    "건설중인자산의 취득",
    "무형자산의 취득",
]

CAPEX_COMPONENT_KEYS = [
    "토지의 취득",
    "건물의 취득",
    "구축물의 취득",
    "기계장치의 취득",
    "비품의 취득",
    "차량운반구의 취득",
    "공구와기구의 취득",
    "금형의 취득",
    "건설중인자산의 취득",
    # 무형자산을 capex에 포함할지 정책 선택(보통 포함하기도 함)
    "소프트웨어의 취득",
    "특허권의 취득",
]


# =========================
# Debug
# =========================

DEBUG_TICKER: Optional[str] = "0004V0"   # <- 여기! None이면 디버그 꺼짐
DEBUG_OUT_DIR = "./debug_dart"
os.makedirs(DEBUG_OUT_DIR, exist_ok=True)

def _debug_enabled_for(ticker: str) -> bool:
    return (DEBUG_TICKER is not None) and (ticker == DEBUG_TICKER)

def _save_debug_csv(df: pd.DataFrame, path: str) -> None:
    # 핵심 컬럼만 안전하게 저장
    cols = [c for c in ["sj_div","account_nm","thstrm_amount","thstrm_add_amount","ord","currency"] if c in df.columns]
    df2 = df[cols].copy()
    df2.to_csv(path, index=False, encoding="utf-8-sig")

def _debug_dump_when_missing(
    *, ticker: str, corp_code: str, meta: Dict[str, Any], df: pd.DataFrame,
    revenue, op_profit, net_income, cfo, capex
) -> None:
    missing = []
    for k, v in [("revenue", revenue), ("op_profit", op_profit), ("net_income", net_income),
                 ("cfo", cfo), ("capex", capex)]:
        if v is None:
            missing.append(k)
    if not missing:
        return

    tag = f"{ticker}_{corp_code}_{meta.get('bsns_year')}_{meta.get('reprt_code')}_{meta.get('fs_div_used')}_{meta.get('rcept_no')}"
    tag = tag.replace("/", "_")

    tqdm.write(f"[DEBUG-MISSING] {tag} -> {missing}")
    # 1) raw head
    _save_debug_csv(df.head(500), os.path.join(DEBUG_OUT_DIR, f"{tag}__raw_head.csv"))

    # 2) IS/CIS block
    is_block = df[df["sj_div"].astype(str).str.strip().isin(["IS","CIS"])].copy()
    _save_debug_csv(is_block, os.path.join(DEBUG_OUT_DIR, f"{tag}__is_block.csv"))

    # 3) CF block
    cf_block = df[df["sj_div"].astype(str).str.strip().isin(["CF","CFS"])].copy()
    _save_debug_csv(cf_block, os.path.join(DEBUG_OUT_DIR, f"{tag}__cf_block.csv"))

    # 4) keyword hit
    kw = []
    kw += REVENUE_KEYS
    kw += OP_KEYS
    kw += NI_KEYS
    kw += ["영업활동현금흐름","영업활동으로 인한 현금흐름","영업활동으로인한현금흐름","영업활동 순현금흐름"]
    kw += ["유형자산의 취득","유형자산취득","유형자산의 구입","건설중인자산의 취득","무형자산의 취득"]
    kw = list(dict.fromkeys(kw))  # dedupe

    s = df["account_nm"].astype(str)
    hit = df[s.apply(lambda x: any(k in x for k in kw))].copy()
    _save_debug_csv(hit, os.path.join(DEBUG_OUT_DIR, f"{tag}__keyword_hit.csv"))

def _dump_df(df: pd.DataFrame, path: str, cols: Optional[List[str]] = None, max_rows: int = 500) -> None:
    if cols is not None:
        d = df[cols].copy()
    else:
        d = df.copy()
    d.head(max_rows).to_csv(path, index=False, encoding="utf-8-sig")

def debug_dump_for_fcf(
    *,
    ticker: str,
    corp_code: str,
    meta: Dict[str, Any],
    df: pd.DataFrame,
    picked: Dict[str, Any],
    reasons: List[str],
) -> None:
    """
    picked: {"cfo":..., "capex":..., "fcf":..., "cfo_hit":df, "capex_hit":df}
    """
    # 1) header
    print("\n" + "=" * 90)
    print(f"[DEBUG_FCF] ticker={ticker} corp_code={corp_code}")
    print("meta:", json.dumps(meta, ensure_ascii=False))
    print("picked:", {k: picked.get(k) for k in ["cfo", "capex", "fcf"]})
    if reasons:
        print("reasons:")
        for r in reasons:
            print(" -", r)

    # 2) sj_div distribution
    try:
        vc = df["sj_div"].astype(str).value_counts(dropna=False)
        print("\n[sj_div value_counts]")
        print(vc.to_string())
    except Exception as e:
        print("[sj_div value_counts] failed:", e)

    # 3) quick uniques
    try:
        uniq = df["sj_div"].dropna().astype(str).unique().tolist()
        print("\n[sj_div unique]", uniq)
    except Exception as e:
        print("[sj_div unique] failed:", e)

    # 4) 후보 덤프 (CF 관련)
    cols = ["sj_div", "account_nm", "thstrm_amount", "thstrm_add_amount"]
    cand_cf = df[df["sj_div"].astype(str).str.contains("CF", na=False, regex=False)].copy()
    print(f"\n[CF-like rows] count={len(cand_cf)} (sj_div contains 'CF')")
    if not cand_cf.empty:
        print(cand_cf[cols].head(80).to_string(index=False))

    # 5) 키워드 히트 덤프
    kw = [
        "영업활동", "영업활동으로", "영업활동으로부터", "영업활동순",
        "유형자산", "투자활동", "설비", "CAPEX",
        "현금및현금성", "현금 및 현금성"
    ]
    hit = df[df["account_nm"].astype(str).str.contains("|".join(map(re.escape, kw)), na=False)].copy()
    print(f"\n[keyword-hit rows] count={len(hit)}")
    if not hit.empty:
        print(hit[cols].head(200).to_string(index=False))

    # 6) 파일 저장(원하면)
    base = f"{ticker}_{meta.get('bsns_year')}_{meta.get('reprt_code')}_{meta.get('fs_div_used')}_{meta.get('rcept_no')}"
    safe = re.sub(r"[^0-9A-Za-z_\-\.]", "_", base)

    # 원본 DF 샘플 저장
    _dump_df(df, os.path.join(DEBUG_OUT_DIR, f"{safe}.raw_head.csv"), cols=cols, max_rows=1000)
    # CF 후보 저장
    _dump_df(cand_cf, os.path.join(DEBUG_OUT_DIR, f"{safe}.cf_candidates.csv"), cols=cols, max_rows=2000)
    # 키워드 히트 저장
    _dump_df(hit, os.path.join(DEBUG_OUT_DIR, f"{safe}.keyword_hit.csv"), cols=cols, max_rows=3000)

    # 요약 json 저장
    summary = {
        "ticker": ticker,
        "corp_code": corp_code,
        "meta": meta,
        "picked": {k: picked.get(k) for k in ["cfo", "capex", "fcf"]},
        "reasons": reasons,
        "sj_div_value_counts": df["sj_div"].astype(str).value_counts(dropna=False).to_dict() if "sj_div" in df.columns else None,
    }
    with open(os.path.join(DEBUG_OUT_DIR, f"{safe}.summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n[DEBUG_FCF] saved under: {DEBUG_OUT_DIR}/{safe}.*")
    print("=" * 90 + "\n")

# =========================
# DART Cache
# =========================


DART_CACHE_DIR = "./dart_cache"
os.makedirs(DART_CACHE_DIR, exist_ok=True)

def _dart_fs_cache_paths(corp_code: str, bsns_year: int, reprt_code: str, fs_div: str) -> tuple[str, str]:
    key = f"fnltt_{corp_code}_{bsns_year}_{reprt_code}_{fs_div}"
    return (
        os.path.join(DART_CACHE_DIR, f"{key}.parquet"),
        os.path.join(DART_CACHE_DIR, f"{key}.meta.json"),
    )

def _load_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _save_json_atomic(obj: Dict[str, Any], path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _load_parquet(path: str) -> Optional[pd.DataFrame]:
    if not os.path.exists(path):
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        return None

def _save_parquet_atomic(df: pd.DataFrame, path: str) -> None:
    tmp = path + ".tmp"
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _sleep():
    time.sleep(REQ_SLEEP)


def get_business_day(date: str) -> str:
    return stock.get_nearest_business_day_in_a_week(date)


def _dart_get(endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """OpenDART JSON API 호출 (status != 000이면 예외 대신 빈 dict 반환하도록 작성)"""
    _sleep()
    url = f"{DART_BASE}/{endpoint}"
    params = dict(params)
    params["crtfc_key"] = DART_API_KEY
    r = SESSION.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    # OpenDART는 status="000"이 정상인 경우가 많음
    if str(data.get("status")) != "000":
        return {}
    return data


def get_biz_date(date_yyyymmdd: str) -> str:
    return stock.get_nearest_business_day_in_a_week(date_yyyymmdd)


# =========================
# Corp code mapping (stock_code -> corp_code)
# =========================

_CORP_LIST_CACHE = None

def get_corp_code_by_stock_code(stock_code: str) -> Optional[str]:
    global _CORP_LIST_CACHE
    if _CORP_LIST_CACHE is None:
        _CORP_LIST_CACHE = dart.get_corp_list()

    corp = _CORP_LIST_CACHE.find_by_stock_code(stock_code)
    if corp is None:
        return None
    return corp.corp_code


# =========================
# Financial statements (OpenDART)
# - 단일회사 전체 재무제표: fnlttSinglAcntAll.json
#   (account_nm, sj_div 등 필드) :contentReference[oaicite:2]{index=2}
# =========================


# (YYYY.MM)에서 월로 보고서코드 결정
_YYYYMM_RE = re.compile(r"\((\d{4})\.(\d{2})\)")


MONTH_TO_REPRT = {
    3:  "11013",  # 1Q
    6:  "11012",  # Half
    9:  "11014",  # 3Q
    12: "11011",  # Annual
}

FIN_REPORT_NAME_KEYS = ["분기보고서", "반기보고서", "사업보고서"]


def _parse_bsns_year_and_reprt_from_report_nm(report_nm: str) -> Optional[Tuple[int, str]]:
    """
    예: "분기보고서 (2025.03)" -> (2025, "11013")
        "[기재정정] 반기보고서 (2024.06)" -> (2024, "11012")
    """
    m = _YYYYMM_RE.search(str(report_nm))
    if not m:
        return None
    y = int(m.group(1))
    mm = int(m.group(2))
    rc = MONTH_TO_REPRT.get(mm)
    if not rc:
        return None
    return y, rc


def _is_financial_report_disclosure(it: Dict[str, Any]) -> bool:
    rn = str(it.get("report_nm", ""))
    if not any(k in rn for k in FIN_REPORT_NAME_KEYS):
        return False
    # (YYYY.MM) 없는 공시(예: 단순 정정 공시 등) 제외
    if _parse_bsns_year_and_reprt_from_report_nm(rn) is None:
        return False
    return True


def fetch_latest_n_fnltt_by_rcept_dt(
    corp_code: str,
    asof_yyyymmdd: str,
    n: int = 6,
    lookback_days: int = 365 * 4,
    max_pages: int = 20,
) -> List[Dict[str, Any]]:
    """
    (1) 공시일(rcept_dt) 기준으로 진짜 최신 n개 재무제표를 선택한다.
    - list.json에서 보고서 공시를 수집
    - report_nm의 (YYYY.MM)로 bsns_year/reprt_code 결정
    - 같은 (bsns_year, reprt_code)는 rcept_dt 최신 1개만 유지(정정/재공시 대비)
    - 그 키로 fnlttSinglAcntAll 호출하여 DF 획득
    반환: [{"df": df, "meta": {...}} ...]  (rcept_dt desc 정렬)
    """
    end_de = asof_yyyymmdd
    bgn_de = (pd.to_datetime(end_de) - pd.Timedelta(days=lookback_days)).strftime("%Y%m%d")

    items = fetch_disclosure_list(
        corp_code=corp_code,
        bgn_de=bgn_de,
        end_de=end_de,
        page_count=100,
        max_pages=max_pages,
    )

    fin_items = [it for it in items if _is_financial_report_disclosure(it)]
    fin_items.sort(key=lambda x: x.get("rcept_dt", ""), reverse=True)

    # (bsns_year, reprt_code)별 최신 공시만 남김 (정정/재공시 대비)
    picked: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for it in fin_items:
        parsed = _parse_bsns_year_and_reprt_from_report_nm(it.get("report_nm", ""))
        if parsed is None:
            continue
        y, rc = parsed
        key = (y, rc)
        # fin_items가 rcept_dt desc 이므로 처음 본 게 최신
        if key not in picked:
            picked[key] = it

    # 이제 picked의 value들을 rcept_dt desc로 다시 정렬해서 상위 n개
    uniq = list(picked.values())
    uniq.sort(key=lambda x: x.get("rcept_dt", ""), reverse=True)
    uniq = uniq[:n]

    out: List[Dict[str, Any]] = []
    for it in uniq:
        parsed = _parse_bsns_year_and_reprt_from_report_nm(it.get("report_nm", ""))
        if parsed is None:
            continue
        y, rc = parsed
        expected_rcept_no = it.get("rcept_no")
        df, fs_used = _fetch_fnltt_best_effort_with_fsdiv_cached(corp_code, y, rc, expected_rcept_no=expected_rcept_no)
        if df.empty:
            continue
        out.append({
            "meta": {
                "bsns_year": y,
                "reprt_code": rc,
                "report_type": REPRT_CODE_TO_TYPE.get(rc, "UNKNOWN"),
                "rcept_dt": it.get("rcept_dt"),
                "rcept_no": it.get("rcept_no"),
                "report_nm": it.get("report_nm"),
                "fs_div_used": fs_used,
                "fs_pref": "CFS_then_OFS",
            },
            "df": df,
        })

    # 혹시 중간에 df empty로 빠져 n개 미만이면, 부족분만큼 “연도/코드 fallback”으로 채우고 싶다면 여기서 추가 가능.
    out.sort(key=lambda x: (x["meta"].get("rcept_dt", ""), x["meta"].get("reprt_code", "")), reverse=True)
    return out


def _normalize_fnltt_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    DART fnlttSinglAcntAll 결과를 안정적으로 매칭 가능하도록 정규화
    """

    if df.empty:
        return df

    # sj_div 정규화 (BS, IS, CIS, CF 등)
    if "sj_div" in df.columns:
        df["sj_div"] = (
            df["sj_div"]
            .astype("string")
            .str.replace("\u00a0", "", regex=False)   # NBSP 제거
            .str.replace(" ", "", regex=False)        # 공백 제거
            .str.strip()
            .str.upper()
        )

    # account_nm 정규화 (검색용)
    if "account_nm" in df.columns:

        # 원본 보존
        df["account_nm"] = (
            df["account_nm"]
            .astype("string")
            .str.replace("\u00a0", " ", regex=False)
            .str.strip()
        )

        # 검색용 정규화 컬럼
        df["account_nm_norm"] = (
            df["account_nm"]
            .astype("string")
            .str.replace("\u00a0", "", regex=False)
            .str.replace(" ", "", regex=False)
            .str.replace("（", "(", regex=False)
            .str.replace("）", ")", regex=False)
            .str.strip()
        )

    return df


def fetch_fnltt_singl_acnt_all(
    corp_code: str,
    bsns_year: int,
    reprt_code: str = "11011",   # 사업보고서
    fs_div: str = "CFS",         # CFS(연결) 우선, 없으면 OFS로 fallback
) -> pd.DataFrame:
    data = _dart_get(
        "fnlttSinglAcntAll.json",
        {
            "corp_code": corp_code,
            "bsns_year": str(bsns_year),
            "reprt_code": reprt_code,
            "fs_div": fs_div,
        },
    )
    rows = data.get("list", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # 금액은 문자열(콤마 포함)인 경우가 많음
    for c in ["thstrm_amount", "frmtrm_amount", "thstrm_add_amount", "frmtrm_add_amount"]:
        if c in df.columns:
            s = df[c].astype("string").str.replace(",", "", regex=False).str.strip()

            # "nan"/"None"/""/"-" 같은 케이스를 NA로
            s = s.replace(["nan", "None", "", "-"], pd.NA)

            # 경고 회피 + dtype 정리
            s = s.infer_objects(copy=False)

            df[c] = pd.to_numeric(s, errors="coerce")
    df = _normalize_fnltt_df(df)
    return df


def fetch_fnltt_cached(
    corp_code: str,
    bsns_year: int,
    reprt_code: str,
    fs_div: str,
    expected_rcept_no: Optional[str] = None,
) -> pd.DataFrame:
    pq_path, meta_path = _dart_fs_cache_paths(corp_code, bsns_year, reprt_code, fs_div)

    meta = _load_json(meta_path) or {}
    cached_df = _load_parquet(pq_path)

    # 1) 캐시가 있고, expected_rcept_no가 없거나(검증 불가) 같으면 캐시 사용
    if cached_df is not None:
        if expected_rcept_no is None or meta.get("rcept_no") == expected_rcept_no:
            return cached_df

    # 2) 없거나/정정 감지 → 새로 fetch
    df = fetch_fnltt_singl_acnt_all(corp_code, bsns_year, reprt_code, fs_div=fs_div)
    if df is None or df.empty:
        return pd.DataFrame()

    # 3) 저장
    _save_parquet_atomic(df, pq_path)
    new_meta = meta.copy()
    new_meta.update({
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
        "fs_div": fs_div,
        "rcept_no": expected_rcept_no,   # 중요: 최신 공시와 연결(정정 검출)
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    if expected_rcept_no is not None:
        new_meta["rcept_no"] = expected_rcept_no
    _save_json_atomic(new_meta, meta_path)
    return df


def fetch_fnltt_best_effort(corp_code: str, bsns_year: int, reprt_code: str = "11011") -> pd.DataFrame:
    # 연결(CFS) 먼저 → 없으면 개별(OFS)
    df = fetch_fnltt_singl_acnt_all(corp_code, bsns_year, reprt_code, fs_div="CFS")
    if df.empty:
        df = fetch_fnltt_singl_acnt_all(corp_code, bsns_year, reprt_code, fs_div="OFS")
    return df


def _fetch_fnltt_best_effort_with_fsdiv_cached(
    corp_code: str,
    bsns_year: int,
    reprt_code: str,
    expected_rcept_no: Optional[str] = None,
) -> Tuple[pd.DataFrame, Optional[str]]:
    df = fetch_fnltt_cached(corp_code, bsns_year, reprt_code, fs_div="CFS", expected_rcept_no=expected_rcept_no)
    if not df.empty:
        return df, "CFS"
    df = fetch_fnltt_cached(corp_code, bsns_year, reprt_code, fs_div="OFS", expected_rcept_no=expected_rcept_no)
    if not df.empty:
        return df, "OFS"
    return pd.DataFrame(), None


def _pick_amount_by_keywords(df: pd.DataFrame, sj_div: str, keywords: List[str]) -> Optional[float]:
    """
    sj_div: BS/IS/CF 등
    keywords: account_nm에서 포함 검색 (여러 변형 대비) :contentReference[oaicite:3]{index=3}
    """
    if df.empty:
        return None
    sub = df[df.get("sj_div").eq(sj_div)].copy()
    if sub.empty:
        return None

    # account_nm에 키워드 포함되는 첫 매칭 (우선순위 유지)
    for kw in keywords:
        col = "account_nm_norm" if "account_nm_norm" in sub.columns else "account_nm"
        kw_norm = kw.replace(" ", "").replace("（","(").replace("）",")")

        m = sub[sub[col].astype(str).str.contains(kw_norm, na=False, regex=False)]
        if not m.empty:
            # 보통 thstrm_amount가 당기금액
            val = m["thstrm_amount"].dropna()
            if not val.empty:
                return float(val.iloc[0])
    return None


def _best_value_from_row(m: pd.DataFrame, prefer_add_amount: bool) -> Optional[float]:
    # 1) add_amount 우선(단, 실제 값이 있을 때만)
    if prefer_add_amount and "thstrm_add_amount" in m.columns:
        s = m["thstrm_add_amount"]
        if s.notna().any():
            return float(s.dropna().iloc[0])

    # 2) amount fallback
    if "thstrm_amount" in m.columns:
        s = m["thstrm_amount"]
        if s.notna().any():
            return float(s.dropna().iloc[0])

    # 3) 최후: frmtrm_add_amount / frmtrm_amount까지(원하면)
    if prefer_add_amount and "frmtrm_add_amount" in m.columns:
        s = m["frmtrm_add_amount"]
        if s.notna().any():
            return float(s.dropna().iloc[0])

    if "frmtrm_amount" in m.columns:
        s = m["frmtrm_amount"]
        if s.notna().any():
            return float(s.dropna().iloc[0])

    return None


def _pick_amount_best_effort_multi_sj(df, sj_div_candidates, keywords, prefer_add_amount):
    if df.empty:
        return None

    sub = df[df["sj_div"].isin(sj_div_candidates)].copy()
    if sub.empty:
        return None

    col = "account_nm_norm" if "account_nm_norm" in sub.columns else "account_nm"

    for kw in keywords:
        kw_norm = kw.replace(" ", "").replace("（","(").replace("）",")")
        m = sub[sub[col].astype(str).str.contains(kw_norm, na=False, regex=False)]
        if m.empty:
            continue

        val = _best_value_from_row(m, prefer_add_amount=prefer_add_amount)
        if val is not None:
            return val

    return None


def debug_is_block(df: pd.DataFrame, n: int = 30):
    sub = df[df["sj_div"].isin(["IS","CIS"])].copy()
    print("sj_div unique:", df["sj_div"].dropna().unique())
    print("IS/CIS rows:", len(sub))
    print(sub[["sj_div","account_nm","thstrm_amount","thstrm_add_amount"]].head(n).to_string(index=False))


def _sum_amount_by_keywords_multi_sj(
    df: pd.DataFrame,
    sj_div_candidates: List[str],
    keywords: List[str],
    prefer_add_amount: bool,
) -> Optional[float]:
    if df.empty:
        return None
    sub = df[df["sj_div"].isin(sj_div_candidates)].copy()
    if sub.empty:
        return None

    col_primary = "thstrm_add_amount" if (prefer_add_amount and "thstrm_add_amount" in sub.columns) else "thstrm_amount"
    col_fallback = "thstrm_amount"

    total = 0.0
    found_any = False
    col_nm = "account_nm_norm" if "account_nm_norm" in sub.columns else "account_nm"

    for kw in keywords:
        kw_norm = kw.replace(" ", "").replace("（","(").replace("）",")")
        m = sub[sub[col_nm].astype(str).str.contains(kw_norm, na=False, regex=False)]
        if m.empty:
            continue

        s = m[col_primary].dropna()
        if s.empty and col_primary != col_fallback:
            s = m[col_fallback].dropna()

        if not s.empty:
            total += float(s.abs().sum())
            found_any = True

    return total if found_any else None


def extract_financial_metrics_cumulative_from_fnltt(
    df: pd.DataFrame,
    *,
    debug_ctx: Optional[Dict[str, Any]] = None,  # <- 추가
) -> Dict[str, Any]:
    total_assets = _pick_amount_best_effort_multi_sj(df, ["BS","BS1","BS2"], ["자산총계","자산 합계"], False)
    total_liab   = _pick_amount_best_effort_multi_sj(df, ["BS","BS1","BS2"], ["부채총계","부채 합계"], False)
    total_equity = _pick_amount_best_effort_multi_sj(df, ["BS","BS1","BS2"], ["자본총계","자본 합계","자본총계(자본)"], False)

    debt_ratio = None
    if total_liab is not None and total_equity not in (None, 0):
        debt_ratio = float(total_liab / total_equity)

    revenue    = _pick_amount_best_effort_multi_sj(df, ["IS","CIS"], REVENUE_KEYS, True)
    op_profit  = _pick_amount_best_effort_multi_sj(df, ["IS","CIS"], OP_KEYS, True)
    net_income = _pick_amount_best_effort_multi_sj(df, ["IS","CIS"], NI_KEYS, True)

    # CF는 CF/CFS 둘 다 허용
    cfo = _pick_amount_best_effort_multi_sj(
        df, ["CF","CFS"],
        ["영업활동현금흐름","영업활동으로 인한 현금흐름","영업활동으로인한현금흐름","영업활동 순현금흐름"],
        True
    )
    capex_raw = _pick_amount_best_effort_multi_sj(
        df, ["CF","CFS"],
        CAPEX_KEYS,
        True
    )

    if capex_raw is None:
        capex_outflow = _sum_amount_by_keywords_multi_sj(df, ["CF","CFS"], CAPEX_COMPONENT_KEYS, True)
        capex_method = "components_abs_sum"
    else:
        capex_outflow = float(abs(capex_raw))
        capex_method = "single_line_abs"

    fcf = None
    if cfo is not None and capex_outflow is not None:
        fcf = float(cfo - capex_outflow)

    return {
        "bs": {"total_assets": total_assets, "total_liabilities": total_liab, "total_equity": total_equity, "debt_ratio": debt_ratio},
        "is": {"revenue": revenue, "operating_profit": op_profit, "net_income": net_income},
        "cf": {
            "operating_cash_flow": cfo,
            "capex": capex_outflow,
            "fcf": fcf,
            "capex_calc": {
                "method": capex_method,
                "raw_value": float(capex_raw) if capex_raw is not None else None,
                "policy": "capex_as_outflow_abs; fcf=cfo-capex_outflow",
            },
        },
    }


def extract_financial_metrics_from_fnltt(df: pd.DataFrame) -> Dict[str, Any]:
    """
    BS: 자산총계/부채총계/자본총계 → debt_ratio
    IS: 매출/영업이익/당기순이익
    CF: 영업활동현금흐름(CFO), 유형자산의 취득(CAPEX) → FCF
    """
    # Balance Sheet
    total_assets = _pick_amount_by_keywords(df, "BS", ["자산총계", "자산 합계"])
    total_liab   = _pick_amount_by_keywords(df, "BS", ["부채총계", "부채 합계"])
    total_equity = _pick_amount_by_keywords(df, "BS", ["자본총계", "자본 합계", "자본총계(자본)"])

    debt_ratio = None
    if total_liab is not None and total_equity not in (None, 0):
        debt_ratio = float(total_liab / total_equity)

    # Income Statement
    revenue      = _pick_amount_by_keywords(df, "IS", ["매출액", "수익(매출)", "영업수익"])
    op_profit    = _pick_amount_by_keywords(df, "IS", ["영업이익", "영업이익(손실)"])
    net_income   = _pick_amount_by_keywords(df, "IS", ["당기순이익", "당기순이익(손실)", "분기순이익", "연결당기순이익"])

    # Cash Flow
    cfo = _pick_amount_by_keywords(df, "CF", ["영업활동현금흐름", "영업활동으로인한현금흐름", "영업활동 현금흐름"])
    # CAPEX(유형자산 취득) 계정명은 다양할 수 있어 후보를 넓게
    capex = _pick_amount_by_keywords(df, "CF", ["유형자산의 취득", "유형자산취득", "유형자산의취득", "유형자산의 구입"])
    fcf = None
    if cfo is not None and capex is not None:
        # 보통 '유형자산의 취득'은 현금 유출이라 음수로 나오기도 함 → 부호를 안전하게 처리
        fcf = float(cfo - abs(capex))

    return {
        "bs": {
            "total_assets": total_assets,
            "total_liabilities": total_liab,
            "total_equity": total_equity,
            "debt_ratio": debt_ratio,  # 예: 1.2 = 120%
        },
        "is": {
            "revenue": revenue,
            "operating_profit": op_profit,
            "net_income": net_income,
        },
        "cf": {
            "operating_cash_flow": cfo,
            "capex": capex,
            "fcf": fcf,
        },
    }


def _safe_sub(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return float(a - b)


def _need_prev_reprt_code(rc: str) -> Optional[str]:
    # 이 보고서의 "분기 단독(period)"을 만들려면 필요한 이전 누적 보고서
    if rc == "11013":  # Q1
        return None
    if rc == "11012":  # H1 = H1 - Q1
        return "11013"
    if rc == "11014":  # Q3(9M) = Q3 - H1
        return "11012"
    if rc == "11011":  # FY = FY - Q3
        return "11014"
    return None


def _period_availability_status(
    *, year_cum_map: Dict[int, Dict[str, Dict[str, Any]]], y: int, rc: str
) -> Dict[str, Any]:
    need_prev = _need_prev_reprt_code(rc)

    have_codes = sorted(list(year_cum_map.get(y, {}).keys()))
    prev_exists = (need_prev is None) or (need_prev in year_cum_map.get(y, {}))

    # “왜 period가 비는지”를 사람/기계 모두 읽기 좋게
    reason = None
    if need_prev is not None and not prev_exists:
        reason = f"missing_prev_cumulative_report:{need_prev}"
    # 추가로, 해당 rc 자체의 누적값(cum)이 비는 케이스도 분리하고 싶으면,
    # 여기서는 year_cum_map에 rc가 들어있다는 가정이라, 아래는 안전장치로만 둠.
    if rc not in year_cum_map.get(y, {}):
        reason = f"missing_current_cumulative_report:{rc}"

    return {
        "year": y,
        "reprt_code": rc,
        "need_prev_reprt_code": need_prev,
        "has_prev": prev_exists,
        "available_reprt_codes_for_year": have_codes,
        "reason_if_problem": reason,
    }


def _periodize_metrics_from_cumulative(
    reprt_code: str,
    cum: Dict[str, Any],
    prev_cum_q1: Optional[Dict[str, Any]],
    prev_cum_h1: Optional[Dict[str, Any]],
    prev_cum_q3: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    reprt_code 기준으로 '기간(분기 단독)' 값을 만든다.
    Q1: period = cum
    H1: period = cum - Q1
    Q3(9M): period = cum - H1
    Annual(FY): period = cum - Q3
    BS는 기간 개념이 없으니 cum 그대로 둔다.
    """
    # BS는 그대로
    bs_period = dict(cum.get("bs", {}) or {})

    is_c = cum.get("is", {}) or {}
    cf_c = cum.get("cf", {}) or {}

    def diff_is_cf(prev: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if prev is None:
            return (
                {"revenue": None, "operating_profit": None, "net_income": None},
                {"operating_cash_flow": None, "capex": None, "fcf": None},
            )
        prev_is = prev.get("is", {}) or {}
        prev_cf = prev.get("cf", {}) or {}
        is_p = {
            "revenue": _safe_sub(is_c.get("revenue"), prev_is.get("revenue")),
            "operating_profit": _safe_sub(is_c.get("operating_profit"), prev_is.get("operating_profit")),
            "net_income": _safe_sub(is_c.get("net_income"), prev_is.get("net_income")),
        }
        cf_p = {
            "operating_cash_flow": _safe_sub(cf_c.get("operating_cash_flow"), prev_cf.get("operating_cash_flow")),
            "capex": _safe_sub(cf_c.get("capex"), prev_cf.get("capex")),
        }
        fcf = None
        if cf_p["operating_cash_flow"] is not None and cf_p["capex"] is not None:
            fcf = float(cf_p["operating_cash_flow"] - cf_p["capex"])
        cf_p["fcf"] = fcf
        return is_p, cf_p

    if reprt_code == "11013":  # Q1
        is_period = dict(is_c)
        cf_period = dict(cf_c)
    elif reprt_code == "11012":  # H1
        is_period, cf_period = diff_is_cf(prev_cum_q1)
    elif reprt_code == "11014":  # Q3(9M)
        is_period, cf_period = diff_is_cf(prev_cum_h1)
    elif reprt_code == "11011":  # Annual(FY)
        is_period, cf_period = diff_is_cf(prev_cum_q3)
    else:
        is_period = {"revenue": None, "operating_profit": None, "net_income": None}
        cf_period = {"operating_cash_flow": None, "capex": None, "fcf": None}

    return {"bs": bs_period, "is": is_period, "cf": cf_period}


def _ensure_year_reports_for_periodize(
    corp_code: str,
    year: int,
    want_codes: List[str],
) -> Dict[str, Dict[str, Any]]:
    """
    같은 연도에서 누락된 보고서(코드)가 있으면 DART에서 best-effort로 가져와서
    cum metrics를 채우기 위한 맵을 만든다.
    반환: {reprt_code: {"cum": metrics_cum, "meta": {...}}}
    """
    out: Dict[str, Dict[str, Any]] = {}
    for rc in want_codes:
        df, fs_used = _fetch_fnltt_best_effort_with_fsdiv_cached(corp_code, year, rc, expected_rcept_no=None)
        if df.empty:
            continue
        cum = extract_financial_metrics_cumulative_from_fnltt(df)
        out[rc] = {
            "cum": cum,
            "meta": {
                "bsns_year": year,
                "reprt_code": rc,
                "report_type": REPRT_CODE_TO_TYPE.get(rc, "UNKNOWN"),
                "fs_div_used": fs_used,
                "fs_pref": "CFS_then_OFS",
                "note": "fetched_for_periodize",
            }
        }
    return out


# =========================
# Major events: CB / BW / EB issuance decisions (OpenDART DS005)
# - CB: bdIssDecsn.json :contentReference[oaicite:4]{index=4}
# - BW: bdwtIsDecsn.json :contentReference[oaicite:5]{index=5}
# - EB: exbdIsDecsn.json :contentReference[oaicite:6]{index=6}
# To find recent rcept_no, use list.json (공시목록)
# =========================

def fetch_disclosure_list(
    corp_code: str,
    bgn_de: str,
    end_de: str,
    page_count: int = 100,
    max_pages: int = 5,      # 과도 호출 방지(원하면 늘리기)
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    page_no = 1

    for _ in range(max_pages):
        data = _dart_get(
            "list.json",
            {
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "page_no": str(page_no),
                "page_count": str(page_count),
                "sort": "date",
                "sort_mth": "desc",
            },
        )
        items = data.get("list", []) or []
        if not items:
            break

        out.extend(items)

        total_page = int(data.get("total_page", 1) or 1)
        if page_no >= total_page:
            break
        page_no += 1

    return out


def _norm_report_nm(s: str) -> str:
    # 공백/특수prefix([기재정정] 등)로 매칭 실패하는 것 방지
    return str(s).replace(" ", "").replace("\u00a0", "")


def fetch_cb_detail(rcept_no: str) -> Dict[str, Any]:
    return _dart_get("cvbdIsDecsn.json", {"rcept_no": rcept_no})

def fetch_bw_detail(rcept_no: str) -> Dict[str, Any]:
    return _dart_get("bdwtIsDecsn.json", {"rcept_no": rcept_no})

def fetch_eb_detail(rcept_no: str) -> Dict[str, Any]:
    return _dart_get("exbdIsDecsn.json", {"rcept_no": rcept_no})


CB_KEYS = ["전환사채", "전환사채권"]
BW_KEYS = ["신주인수권부사채", "신주인수권부사채권"]
EB_KEYS = ["교환사채", "교환사채권"]


def debug_bond_keywords(items):
    kws = ["전환사채", "전환사채권", "신주인수권부사채", "신주인수권부사채권", "교환사채", "교환사채권", "발행", "결정", "사채"]
    hits = []
    for it in items:
        rn = str(it.get("report_nm", ""))
        if any(k in rn for k in kws):
            hits.append(rn)
    print("keyword-hit count:", len(hits))
    for rn in hits[:30]:
        print(rn)


def _pick_events(items: List[Dict[str, Any]], keys: List[str], max_n: int) -> List[Dict[str, Any]]:
    hit = []
    for it in items:
        rn = _norm_report_nm(it.get("report_nm", ""))
        if any(k.replace(" ", "") in rn for k in keys) and ("발행" in rn) and ("결정" in rn):
            hit.append(it)
    hit.sort(key=lambda x: x.get("rcept_dt", ""), reverse=True)
    return hit[:max_n]


def get_recent_bond_events(
    corp_code: str,
    asof_yyyymmdd: str,
    lookback_days: int = 365,
    max_events_each: int = 5,
) -> Dict[str, Any]:
    end_de = asof_yyyymmdd
    bgn_de = (pd.to_datetime(end_de) - pd.Timedelta(days=lookback_days)).strftime("%Y%m%d")

    items = fetch_disclosure_list(corp_code, bgn_de, end_de, page_count=100, max_pages=10)
    
    cb_list = _pick_events(items, CB_KEYS, max_events_each)
    bw_list = _pick_events(items, BW_KEYS, max_events_each)
    eb_list = _pick_events(items, EB_KEYS, max_events_each)

    cb_details = [{"meta": it, "detail": fetch_cb_detail(it["rcept_no"])} for it in cb_list]
    cb_details = [x for x in cb_details if x["detail"]]  # status!=000 필터

    bw_details = [{"meta": it, "detail": fetch_bw_detail(it["rcept_no"])} for it in bw_list]
    bw_details = [x for x in bw_details if x["detail"]]

    eb_details = [{"meta": it, "detail": fetch_eb_detail(it["rcept_no"])} for it in eb_list]
    eb_details = [x for x in eb_details if x["detail"]]

    return {"cb": cb_details, "bw": bw_details, "eb": eb_details}


# =========================
# Combine into GPT JSON
# =========================


def build_gpt_meta(asof: str) -> Dict[str, Any]:
    return {
        "asof": asof,
        "currency": "KRW",
        "units": {
            "price": "KRW_per_share",
            "market_cap": "KRW",
            "traded_value": "KRW",
            "volume": "shares",
            "ratio": "ratio",
            "percent": "percent",
            "return": "fraction",        # 예: 0.12 = +12%
            "volatility": "stdev_of_daily_returns",
        },
        "timeframe_trading_days": {
            "1m": 21,
            "3m": 63,
            "6m": 126,
            "12m": 252,
        },
        "definitions": {
            "PBR": "Price-to-Book Ratio (주가순자산비율): 낮을수록 저평가 가능성",
            "PER": "Price-to-Earnings Ratio (주가수익비율): 낮을수록 저평가 가능성(단, 이익 품질 필요)",
            "DIV_YIELD": "Dividend Yield (%): 높을수록 배당 매력",
            "MCAP": "Market Capitalization: 시가총액 (KRW)",
            "VALUE_TRADED": "Traded Value: 거래대금 (KRW)",
            "MOM": "Momentum: 최근 mom_days(기본 120영업일) 수익률",
            "VOL": "Volatility: 최근 vol_days(기본 60영업일) 일간수익률 표준편차",
            "investor_flow": "최근 N영업일 투자자별 순매수(+) / 순매도(-)",
        },
    }


def unitize_quant_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    기존 item(약어/숫자)을 그대로 두되,
    GPT가 바로 이해할 수 있게 unitized 섹션을 추가한다.
    """
    v = item.get("valuation", {}) or {}
    liq = item.get("liquidity", {}) or {}
    vol = item.get("volume", {}) or {}
    price = item.get("price", {}) or {}
    mom = item.get("momentum", {}) or {}

    unitized = {
        "valuation": {
            "price_to_book_ratio": {"value": v.get("PBR"), "unit": "ratio"},
            "price_to_earnings_ratio": {"value": v.get("PER"), "unit": "ratio"},
            "dividend_yield": {"value": v.get("DIV_YIELD"), "unit": "percent"},
            "market_cap": {"value": v.get("MCAP"), "unit": "KRW"},
        },
        "liquidity": {
            "value_traded_today": {"value": liq.get("value_traded_today"), "unit": "KRW"},
            "avg_value_traded_20d": {"value": liq.get("avg_value_traded_20d"), "unit": "KRW"},
        },
        "volume": {
            "avg_volume_20d": {"value": vol.get("avg_volume_20d"), "unit": "shares"},
        },
        "price": {
            "current": {"value": price.get("current"), "unit": "KRW_per_share"},
            "change_1m": {"value": price.get("change_1m"), "unit": "fraction"},
            "change_3m": {"value": price.get("change_3m"), "unit": "fraction"},
            "change_6m": {"value": price.get("change_6m"), "unit": "fraction"},
            "change_12m": {"value": price.get("change_12m"), "unit": "fraction"},
        },
        "momentum": {
            "mom": {"value": mom.get("mom"), "unit": "fraction"},
            "volatility": {"value": mom.get("volatility"), "unit": "stdev_of_daily_returns"},
        },
    }

    out = dict(item)
    out["meta_units"] = {"currency": "KRW"}  # item-level hint(옵션)
    out["unitized"] = unitized
    return out


def decorate_dart_to_quant(
    quant_item: Dict[str, Any]
) -> Dict[str, Any]:
    ticker = quant_item["ticker"]
    biz = quant_item["analysis_date"]

    name = quant_item.get("name") or stock.get_market_ticker_name(ticker)
    listing_market = quant_item.get("listing_market")

    try:
        corp_code = get_corp_code_by_stock_code(ticker)
    except Exception as error:
        print(f'{quant_item["ticker"]} => {error}')
        return None

    financials = None
    bond_events = None

    if corp_code:
        # (1) 공시일 기준 최신 n개
        latest_list = fetch_latest_n_fnltt_by_rcept_dt(
            corp_code=corp_code,
            asof_yyyymmdd=biz,
            n=6,
            lookback_days=365 * 4,
            max_pages=20,
        )

        if latest_list:
            # 최신 n개에 포함된 연도들에 대해 periodize에 필요한 이전 누적 보고서도 보강
            years = sorted({one["meta"]["bsns_year"] for one in latest_list}, reverse=True)

            # 연도별 누적 metrics 저장소 (periodize용)
            year_cum_map: Dict[int, Dict[str, Dict[str, Any]]] = {y: {} for y in years}

            for one in latest_list:
                y = one["meta"]["bsns_year"]
                rc = one["meta"]["reprt_code"]
                cum = extract_financial_metrics_cumulative_from_fnltt(one["df"])
                year_cum_map[y][rc] = {"cum": cum, "meta": one["meta"]}

            # 누락된 코드만 추가 fetch
            for y in years:
                missing = [rc for rc in ["11013","11012","11014","11011"] if rc not in year_cum_map[y]]
                if missing:
                    extra = _ensure_year_reports_for_periodize(corp_code, y, missing)
                    year_cum_map[y].update(extra)

            fin_items = []
            for one in latest_list:
                df = one["df"]
                meta = one["meta"]
                y = meta["bsns_year"]
                rc = meta["reprt_code"]

                debug_ctx = None
                if _debug_enabled_for(ticker):
                    debug_ctx = {
                        "enabled": True,
                        "ticker": ticker,
                        "corp_code": corp_code,
                        "meta": meta,  # 현재 보고서 메타
                    }

                # 누적 metrics (안정적으로 add_amount 우선)
                cum = extract_financial_metrics_cumulative_from_fnltt(df, debug_ctx=debug_ctx)

                # periodize에 필요한 이전 누적(같은 연도) 가져오기
                q1 = (year_cum_map.get(y, {}).get("11013", {}) or {}).get("cum")
                h1 = (year_cum_map.get(y, {}).get("11012", {}) or {}).get("cum")
                q3 = (year_cum_map.get(y, {}).get("11014", {}) or {}).get("cum")

                period = _periodize_metrics_from_cumulative(
                    reprt_code=rc,
                    cum=cum,
                    prev_cum_q1=q1,
                    prev_cum_h1=h1,
                    prev_cum_q3=q3,
                )

                period_status = _period_availability_status(year_cum_map=year_cum_map, y=y, rc=rc)

                fin_items.append({
                    "meta": meta,                 # rcept_dt/rcept_no/report_nm 포함
                    "metrics_cumulative": cum,    # 누적(누계)
                    "metrics_period": period,     # 기간(분기 단독)으로 정규화
                    "period_status": period_status,
                    "normalization": {
                        "method": "diff_within_same_fiscal_year",
                        "assumption": "IS/CF use thstrm_add_amount when available; else thstrm_amount",
                        "note": "BS is kept as point-in-time",
                    },
                })

            financials = {
                "latest_n": len(fin_items),
                "items": fin_items,
            }

        bond_events = get_recent_bond_events(corp_code, asof_yyyymmdd=biz, lookback_days=365)

    return {
        "ticker": ticker,
        "name": name,
        "listing_market": listing_market,
        "analysis_date": biz,
        "quant": unitize_quant_item(quant_item),
        "financials_dart": financials,
        "events_dart": {
            "convertible_bond_bw_eb_last_12m": bond_events
        },
    }


# =========================
# GPT JSON
# =========================


def build_gpt_inputs_from_quant_file(
    quant_json_path: str,
    out_path: str,
    refetch_pykrx_snapshot: bool = False,
) -> Dict[str, Any]:
    with open(quant_json_path, "r", encoding="utf-8") as f:
        quant_payload = json.load(f)

    asof = quant_payload["asof"]           # 예: "20260225"
    market = quant_payload.get("market", "ALL")
    items = quant_payload.get("items", [])

    results = []
    for item in tqdm(items, desc="DART enrich + GPT JSON", unit="ticker"):
        ticker = item["ticker"]
        g = decorate_dart_to_quant(
            ticker=ticker,
            analysis_date_yyyymmdd=asof,
            quant_item=item,
            market=market,
            refetch_pykrx_snapshot=refetch_pykrx_snapshot,
        )
        if g is None:
            continue
        results.append(g)

    out = {
        "meta": build_gpt_meta(asof),
        "asof": asof,
        "market": market,
        "count": len(results),
        "items": results,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    return out


class Analyzer:
    def __init__(self, result_dir: Optional[str] = None):
        self.result_dir = RESULT_DIR if result_dir is None else result_dir

    def get_result_dir(self, quant: Dict[str, str]):
        asof = quant["analysis_date"]
        return os.path.join(self.result_dir, "analyzer", asof)

    def get_result_file_path(self, quant: Dict[str, str]):
        ticker = quant["ticker"]
        return os.path.join(self.get_result_dir(quant), f"{ticker}.json")

    def decorate_dart_to_target_quant(self, quant: Dict[str, str]):
        g = decorate_dart_to_quant(quant)
        if g is None:
            return g
        dir_path = self.get_result_dir(quant)
        os.makedirs(dir_path, exist_ok=True)
        out_path = self.get_result_file_path(quant)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(g, f, ensure_ascii=False, indent=2)
        return g


# =========================
# Example
# =========================

if __name__ == "__main__":
    quant_path = "./result/quant_pre_dart_top30_plus_interest_20260226.json"
    out_path = "./result/gpt_input_top30_20260226.json"

    build_gpt_inputs_from_quant_file(
        quant_json_path=quant_path,
        out_path=out_path,
        refetch_pykrx_snapshot=False,  # <= 기본 False 추천
    )
    print(f"Saved -> {out_path}")