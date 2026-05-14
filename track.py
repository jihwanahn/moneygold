from constants import USER_INTEREST_PATH, CONFIG_PATH
from screener import Screener
from analyzer import Analyzer
from advisor import Advisor
import os
import json
from typing import Optional, Dict, Tuple, Any, List
from tqdm import tqdm
import requests
from pykrx.website.comm import webio
from datetime import datetime


def days_between(date1: str, date2: str) -> int:
    """
    "yyyymmdd" 형식 문자열 두 개 사이의 일수 차이를 반환한다.
    
    예:
    days_between("20260301", "20260310") -> 9
    days_between("20260310", "20260301") -> -9
    """
    d1 = datetime.strptime(date1, "%Y%m%d")
    d2 = datetime.strptime(date2, "%Y%m%d")
    return abs((d2 - d1).days)


def load_screening_history():
    with open("./data/screening_history.json", "r", encoding="utf-8") as f:
        return json.load(f)

def save_screening_history(history):
    with open("./data/screening_history.json", "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

# =========================
# User Interest
# =========================


def load_user_interest(path: str = USER_INTEREST_PATH) -> List[Dict[str, str]]:
    """
    기대 형식:
    {
      "items": [{"ticker":"000660","name":"SK하이닉스"}, ...]
    }
    """
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)

    items = obj.get("items", [])
    out = []
    for it in items:
        t = str(it.get("ticker", "")).strip()
        n = str(it.get("name", "")).strip()
        if not t:
            continue
        out.append({"ticker": t, "name": n})
    return out

def load_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_text_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

# =========================
# Login
# =========================

def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()
login_id = config["id"]
login_pw = config["password"]

_session = requests.Session()

def _session_post_read(self, **params):
      return _session.post(self.url, headers=self.headers, data=params)

def _session_get_read(self, **params):
    return _session.get(self.url, headers=self.headers, params=params)

webio.Post.read = _session_post_read
webio.Get.read = _session_get_read

def login_krx(login_id: str, login_pw: str) -> bool:
    """
    KRX data.krx.co.kr 로그인 후 세션 쿠키(JSESSIONID)를 갱신합니다.

    로그인 흐름:
    1. GET MDCCOMS001.cmd  → 초기 JSESSIONID 발급
    2. GET login.jsp       → iframe 세션 초기화
    3. POST MDCCOMS001D1.cmd → 실제 로그인
    4. CD011(중복 로그인) → skipDup=Y 추가 후 재전송
    """
    _LOGIN_PAGE = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd"
    _LOGIN_JSP  = "https://data.krx.co.kr/contents/MDC/COMS/client/view/login.jsp?site=mdc"
    _LOGIN_URL  = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001D1.cmd"
    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    # 초기 세션 발급
    _session.get(_LOGIN_PAGE, headers={"User-Agent": _UA}, timeout=15)
    _session.get(_LOGIN_JSP, headers={"User-Agent": _UA, "Referer": _LOGIN_PAGE}, timeout=15)

    payload = {
        "mbrNm": "", "telNo": "", "di": "", "certType": "",
        "mbrId": login_id, "pw": login_pw,
    }
    headers = {"User-Agent": _UA, "Referer": _LOGIN_PAGE}

    # 로그인 POST
    resp = _session.post(_LOGIN_URL, data=payload, headers=headers, timeout=15)
    data = resp.json()
    error_code = data.get("_error_code", "")

    # CD011 중복 로그인 처리
    if error_code == "CD011":
        payload["skipDup"] = "Y"
        resp = _session.post(_LOGIN_URL, data=payload, headers=headers, timeout=15)
        data = resp.json()
        error_code = data.get("_error_code", "")

    return error_code == "CD001"  # CD001 = 정상

def main():
    if not login_krx(login_id, login_pw):
        raise Exception("Login failed")

    user_interest = load_user_interest()

    screener = Screener()
    print(f"biz date : {screener.biz_date}")

    print("Screening")
    quants = []
    for each in tqdm(user_interest):
        quant_path = screener.get_result_file_path(each["ticker"])
        if os.path.exists(quant_path):
            quants.append(load_json_file(quant_path))
        else:
            quants.append(screener.build_target_quant(each["ticker"]))
    
    analyzer = Analyzer()
    print("Analyzing")
    detailed_quants = []
    for each in tqdm(quants):
        detailed_quant_path = analyzer.get_result_file_path(each)
        if os.path.exists(detailed_quant_path):
            detailed_quants.append(load_json_file(detailed_quant_path))
        else:
            g = analyzer.decorate_dart_to_target_quant(each)
            if g is None:
                continue
            detailed_quants.append(g)

    advisor = Advisor(postfix="_detailed")
    print("Advising")
    for each in detailed_quants:
        advice_file_path = advisor.get_result_file_path(each)
        if os.path.exists(advice_file_path):
            print(load_text_file(advice_file_path))
        else:
            print(advisor.get_advice(each))

if __name__ == "__main__":
    main()
    