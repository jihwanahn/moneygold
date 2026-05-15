"""KIS 재무 엔드포인트 가용성 확인.

KIS Open API 문서상 손익계산서·재무비율·성장성비율 등을 제공.
실제 호출해서 분기 추세를 받을 수 있는지 검증.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from moneygold.config import load_config  # noqa
from moneygold.data.kis_client import KISClient  # noqa


def call(client: KISClient, path: str, tr_id: str, ticker: str, fid_div: str = "0") -> dict:
    """범용 호출 헬퍼. fid_div: 0=년간, 1=분기."""
    params = {
        "FID_DIV_CLS_CODE": fid_div,
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": ticker,
    }
    return client._get(path, tr_id, params)


def main() -> int:
    cfg = load_config()
    client = KISClient(cfg.kis)

    # KIS Open API 문서에서 추정한 finance 엔드포인트들
    endpoints = [
        ("손익계산서", "/uapi/domestic-stock/v1/finance/income-statement", "FHKST66430200"),
        ("대차대조표", "/uapi/domestic-stock/v1/finance/balance-sheet", "FHKST66430100"),
        ("재무비율", "/uapi/domestic-stock/v1/finance/financial-ratio", "FHKST66430300"),
        ("수익성비율", "/uapi/domestic-stock/v1/finance/profit-ratio", "FHKST66430400"),
        ("성장성비율", "/uapi/domestic-stock/v1/finance/growth-ratio", "FHKST66430600"),
    ]

    ticker = "005930"  # 삼성전자
    print(f"=== KIS 재무 엔드포인트 진단 (ticker={ticker}) ===\n")
    for name, path, tr_id in endpoints:
        print(f"[{name}] tr_id={tr_id}")
        for div, label in [("0", "년간"), ("1", "분기")]:
            try:
                data = call(client, path, tr_id, ticker, fid_div=div)
                rt_cd = data.get("rt_cd")
                msg = data.get("msg1", "")
                out = data.get("output", []) or []
                if isinstance(out, dict):
                    sample_keys = list(out.keys())[:8]
                    n = 1
                else:
                    sample_keys = list(out[0].keys())[:8] if out else []
                    n = len(out)
                print(f"  {label} (FID_DIV={div}): rt_cd={rt_cd} rows={n} keys={sample_keys}")
                if out:
                    sample = out if isinstance(out, dict) else out[0]
                    # 한국어 키 + 값 (앞 5개)
                    excerpt = {k: v for i, (k, v) in enumerate(sample.items()) if i < 5}
                    print(f"      first sample: {excerpt}")
            except Exception as e:
                print(f"  {label} (FID_DIV={div}): ERROR {type(e).__name__}: {str(e)[:120]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
