"""KIS Open API 사전검증 스크립트 (PR0).

ARCHITECTURE.md §2의 PR0 검증 항목 3종을 실제 호출로 검증한다:
  1. 2년치 일봉 백필이 한 종목 단위로 가능한가? (페이지네이션 필요/한도 확인)
  2. 종목 마스터(KOSPI/KOSDAQ 전종목 리스트) 출처를 KIS만으로 확보 가능한가?
  3. 상장폐지된 종목의 과거 데이터를 KIS가 제공하는가?

사용법:
    cp .env.example .env  # 후 KIS_APP_KEY / KIS_APP_SECRET 입력
    python scripts/verify_kis.py

본 스크립트는 read-only API만 호출. 주문/계좌변경 API 사용 안 함.
계좌 잔고 조회는 이 스크립트에선 안 함 (PR5에서 처리).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

# 프로젝트 루트의 src를 경로에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from moneygold.config import load_config  # noqa: E402

BASE = "https://openapi.koreainvestment.com:9443"
SESSION = requests.Session()


# ============================================================
# OAuth
# ============================================================

def get_access_token(app_key: str, app_secret: str) -> str:
    url = f"{BASE}/oauth2/tokenP"
    payload = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret,
    }
    r = SESSION.post(url, json=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"OAuth 응답에 access_token 없음: {data}")
    return data["access_token"]


def kis_headers(token: str, app_key: str, app_secret: str, tr_id: str) -> dict[str, str]:
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",  # 개인
    }


# ============================================================
# Test 1: 2년 일봉 백필
# ============================================================

def fetch_daily_chart(token: str, cfg, ticker: str, date_from: str, date_to: str) -> dict[str, Any]:
    """기간별 일봉 차트. tr_id=FHKST03010100.

    KIS 응답 한 번에 보통 100개 row까지. 더 길게 받으려면 date_to를 당겨가며 재호출.
    """
    url = f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    headers = kis_headers(token, cfg.kis.app_key, cfg.kis.app_secret, tr_id="FHKST03010100")
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",       # J=주식
        "FID_INPUT_ISCD": ticker,
        "FID_INPUT_DATE_1": date_from,        # YYYYMMDD
        "FID_INPUT_DATE_2": date_to,
        "FID_PERIOD_DIV_CODE": "D",           # D=일봉
        "FID_ORG_ADJ_PRC": "0",               # 0=수정주가
    }
    r = SESSION.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def test_2y_backfill(token, cfg) -> None:
    print("\n" + "=" * 70)
    print("Test 1: 2년 일봉 백필 가능 여부")
    print("=" * 70)

    samples = [
        ("005930", "삼성전자"),
        ("000660", "SK하이닉스"),
        ("035720", "카카오"),
    ]
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=365 * 2)).strftime("%Y%m%d")

    print(f"요청 기간: {start} ~ {end} (약 2년)")

    for tk, name in samples:
        time.sleep(0.3)  # rate limit 여유
        try:
            data = fetch_daily_chart(token, cfg, tk, start, end)
        except Exception as e:
            print(f"  [{tk} {name}] 호출 실패: {e}")
            continue

        rt_cd = data.get("rt_cd")
        msg = data.get("msg1", "")
        output2 = data.get("output2", []) or []
        n = len(output2)
        first = output2[-1].get("stck_bsop_date", "?") if output2 else "?"
        last = output2[0].get("stck_bsop_date", "?") if output2 else "?"

        verdict = "✅ 충분" if n >= 480 else ("⚠️ 부족 (페이지네이션 필요)" if 0 < n < 480 else "❌ 응답 비어있음")
        print(f"  [{tk} {name}] rt_cd={rt_cd} msg={msg!r}")
        print(f"     반환 rows={n}  실제 기간 {first} ~ {last}  → {verdict}")

    print("\n결론 가이드:")
    print("  - n ≥ 480: 단일 호출로 2년 백필 가능. PR1 sync 코드 단순.")
    print("  - 0 < n < 480: 페이지네이션 필요. tr_cont/ctx_area_* 헤더 사용 필요.")
    print("  - n = 0 또는 에러: 다른 엔드포인트 필요 (예: 장기 시세 별도 엔드포인트).")


# ============================================================
# Test 2: 종목 마스터 출처
# ============================================================

def test_master_source(token, cfg) -> None:
    print("\n" + "=" * 70)
    print("Test 2: 종목 마스터 (KOSPI/KOSDAQ 전종목 리스트) 확보")
    print("=" * 70)

    # KIS는 *전체 종목 리스트*를 한 번에 주는 깔끔한 엔드포인트가 없다.
    # 대안 A: 개별 종목 조회 (search-stock-info) — 종목코드를 이미 알고 있어야 함
    # 대안 B: KRX 종목 마스터 파일 (별도 다운로드)
    # 대안 C: pykrx get_market_ticker_list — 1회용 fallback
    print("⚠️  KIS는 전체 종목 일괄 다운로드 엔드포인트 없음 (확인된 사실).")
    print()

    print("개별 종목 메타 조회 (search-stock-info) 작동 여부 검증:")
    url = f"{BASE}/uapi/domestic-stock/v1/quotations/search-stock-info"
    headers = kis_headers(token, cfg.kis.app_key, cfg.kis.app_secret, tr_id="CTPF1604R")
    for tk, name in [("005930", "삼성전자"), ("035720", "카카오")]:
        time.sleep(0.3)
        params = {"PDNO": tk, "PRDT_TYPE_CD": "300"}
        try:
            r = SESSION.get(url, headers=headers, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            rt_cd = data.get("rt_cd")
            output = data.get("output", {}) or {}
            print(f"  [{tk} {name}] rt_cd={rt_cd}  키 일부={list(output.keys())[:8]}")
        except Exception as e:
            print(f"  [{tk} {name}] 호출 실패: {e}")

    print()
    print("결론 가이드:")
    print("  - KIS만으로는 전종목 리스트 불가. PR1에서 후보 출처 선택 필요:")
    print("    (A) KRX 종목 마스터 파일 (data.krx.co.kr) 다운로드 + 파싱")
    print("    (B) pykrx.get_market_ticker_list 의존성 1회용 유지")
    print("    (C) KIS 업종/지수 구성종목 엔드포인트 순회 (시간 오래 걸림)")


# ============================================================
# Test 3: 상장폐지 종목 과거 데이터
# ============================================================

def test_delisted(token, cfg) -> None:
    print("\n" + "=" * 70)
    print("Test 3: 상장폐지 종목 과거 일봉 데이터 제공 여부")
    print("=" * 70)

    # 데이터가 와도 *날짜 범위*를 봐야 진짜 폐지 종목인지 재발행 코드인지 판별 가능.
    # 매우 오래 전(5년+) 기간을 요청하고 가장 최근/오래된 응답일자를 출력한다.
    candidates = [
        ("011000", "㈜진로 (구) - 합병 소멸 추정"),
        ("003540", "대신증권우 - 코드 변경 추정"),
        ("037160", "엔글로벌 - 폐지 사례"),
        ("066570", "LG전자 (대조군 - 현재 상장중)"),
    ]

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=365 * 8)).strftime("%Y%m%d")
    today_int = int(end)

    print(f"요청 기간: {start} ~ {end} (약 8년, 폐지 시점 포착 목적)\n")

    for tk, label in candidates:
        time.sleep(0.3)
        try:
            data = fetch_daily_chart(token, cfg, tk, start, end)
        except Exception as e:
            print(f"  [{tk} {label}] 호출 실패: {e}")
            continue
        rt_cd = data.get("rt_cd")
        rows = data.get("output2", []) or []
        n = len(rows)
        if n == 0:
            print(f"  [{tk}] {label}")
            print(f"     rows=0  → ❌ 데이터 없음")
            continue

        # output2는 최신 → 과거 순. 첫 항목이 가장 최근.
        newest = rows[0].get("stck_bsop_date", "?")
        oldest = rows[-1].get("stck_bsop_date", "?")

        # 가장 최근 날짜와 오늘 사이 일수 차이
        try:
            days_since_last = today_int - int(newest)
            # 단순 정수 차이 (월말 경계는 무시) — 큰 추세만 보면 됨
            if days_since_last < 30:
                verdict = "🔄 재발행/현재 상장 (최근 거래 있음)"
            elif days_since_last < 365:
                verdict = "⚠️ 최근 폐지 또는 거래 정지"
            else:
                verdict = "✅ 폐지 종목 데이터 (과거 거래만 존재)"
        except ValueError:
            verdict = "? 날짜 파싱 실패"

        print(f"  [{tk}] {label}")
        print(f"     rows={n}  최신={newest}  최오래={oldest}  → {verdict}")

    print()
    print("결론 가이드:")
    print("  - '✅ 폐지 종목 데이터': KIS가 폐지 종목 과거 시세 *제공*. 백테스트 생존편향 해결 가능.")
    print("  - '🔄 재발행': 종목코드가 다른 회사에 재할당됨. 폐지 데이터 아님.")
    print("  - 모든 폐지 후보가 ❌/🔄 면 KIS는 폐지 데이터 미제공 — 별도 데이터셋 필요.")


# ============================================================
# Entry
# ============================================================

def main() -> int:
    cfg = load_config()

    if not cfg.kis.app_key or not cfg.kis.app_secret:
        print("❌ .env의 KIS_APP_KEY / KIS_APP_SECRET 가 비어있습니다.")
        print("   .env.example을 .env로 복사한 뒤 값을 채우세요.")
        return 1

    print("KIS 사전검증 시작...")
    print(f"  app_key: {cfg.kis.app_key[:8]}...")
    print(f"  base_url: {cfg.kis.base_url}")

    try:
        token = get_access_token(cfg.kis.app_key, cfg.kis.app_secret)
    except requests.HTTPError as e:
        print(f"\n❌ OAuth 실패: {e}")
        if e.response is not None:
            try:
                print(f"   응답: {json.dumps(e.response.json(), ensure_ascii=False)}")
            except Exception:
                print(f"   응답(raw): {e.response.text[:500]}")
        return 1
    except Exception as e:
        print(f"\n❌ OAuth 실패: {e}")
        return 1

    print(f"  ✅ access_token 획득 ({token[:12]}...)")

    test_2y_backfill(token, cfg)
    test_master_source(token, cfg)
    test_delisted(token, cfg)

    print("\n" + "=" * 70)
    print("검증 완료. 위 결과를 ARCHITECTURE.md §2 PR0 검증 섹션에 반영하세요.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
