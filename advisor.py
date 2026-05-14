from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from constants import RESULT_DIR


SYSTEM_PROMPT = """\
너는 한국 주식(코스피/코스닥) 투자 조언가다.
입력 JSON(정량+재무+이벤트)을 기반으로 1차 판단을 내리고,
부족한 최신 이슈/뉴스/공시/업황은 반드시 web_search로 보강한다.

규칙:
- 출력 포맷 : JSON 단일 객체
- JSON 에 속하는 데이터는 출력 형식 참고
- 백틱 ``` 또는 ~~~ (코드펜스) 절대 금지
- 마크다운 절대 금지
"""

USER_PROMPT_TEMPLATE = """\
아래는 특정 종목의 정량 데이터와 DART 요약이 포함된 JSON이다.
반드시 web_search를 사용해 최근 90일 이슈/뉴스/공시를 확인한 뒤, 매수/매도 조언을 JSON으로 출력해라.

[입력 JSON]
{item_json}

[출력 형식 JSON]
{{
    "ticker": "종목코드 6자리",
    "name": "종목명",
    "asof": "YYYYMMDD",
    "action": "BUY|ACCUMULATE|HOLD|REDUCE|SELL",
    "conviction": 1,
    "horizon": "단기|중기|장기 등",
    "expectation": "투자 기대 결과(예측 수익률/기간 등)",
    "position_sizing": "분할매수/손절/비중 가이드(정량/정성)",
    "reasoning": "결론이 얻어진 과정의 디테일한 설명. 최대 15줄",
    "bull_case": ["상승(매수) 근거 핵심 bullet 3~8개"],
    "bear_case": ["하락(리스크) 근거 핵심 bullet 3~8개"],
    "key_levels": {{"support": "expected value", "resistance": "expected value", "stop_loss": "expected value"}}, # 가능하면
    "catalysts_next_90d": ["향후 90일 촉매(실적/공시/산업/이벤트)"],
    "sources": [참고한 웹사이트의 목록(링크를 제시)]
}}
"""

def build_llm(
    provider: str = "openai",
    model: str = "gpt-5.2",
    temperature: float = 0.2,
):
    """
    provider:
      - "openai": ChatOpenAI + web_search_preview tool binding
      - "gemini": ChatGoogleGenerativeAI (tool binding 없음. 프롬프트로만 검색 유도)
    """
    provider = (provider or "openai").lower()

    if provider == "openai":
        llm = ChatOpenAI(
            model=model,
            temperature=temperature,
        ).bind_tools([{"type": "web_search_preview"}])
        return llm

    if provider == "gemini":
        # 예: model="gemini-1.5-pro" 또는 "gemini-2.0-flash" 등
        llm = ChatGoogleGenerativeAI(
            model=model,
            temperature=temperature,
        )
        return llm

    raise ValueError(f"Unknown provider: {provider}")

def analyze_one(llm, item: Dict[str, Any]) -> str:
    item_json = json.dumps(item, ensure_ascii=False)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT_TEMPLATE.format(item_json=item_json)},
    ]

    resp = llm.invoke(messages)
    return extract_text(resp)

def extract_text(resp) -> str:
    if hasattr(resp, "text") and resp.text:
        return resp.text.strip()

    content = resp.content

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()

    return str(content).strip()


def get_result_dir(asof: str, base_dir: str):
    return os.path.join(base_dir, "advisor", asof)

def get_result_file_path(ticker: str, asof: str, base_dir: str, postfix: str= ""):
    return os.path.join(get_result_dir(asof, base_dir), f"{ticker}{postfix}.txt")

def save_advice_txt(ticker: str, asof: str, advice_str: str, base_dir: str, postfix: str= ""):
    ticker = ticker or "unknown"
    asof = asof or "unknown"

    dir_path = get_result_dir(asof, base_dir)
    os.makedirs(dir_path, exist_ok=True)
    file_path = get_result_file_path(ticker, asof, base_dir, postfix)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(advice_str)


class Advisor:
    def __init__(self, result_dir : Optional[str] = None, provider: str = "openai", model: str = "gpt-5.2", sleep_s: float = 0.25, postfix: str= ""):
        self.result_dir = RESULT_DIR if result_dir is None else result_dir
        self.sleep_s = sleep_s
        self.postfix = postfix
        self.llm = build_llm(provider=provider, model=model)

    def get_result_file_path(self, data: Dict[str, str]):
        date = data["analysis_date"]
        ticker = data["ticker"]
        return get_result_file_path(ticker, date, self.result_dir, self.postfix)

    def get_advice(self, data: Dict[str, str]):
        date = data["analysis_date"]
        ticker = data["ticker"]
        advice = analyze_one(self.llm, data)
        save_advice_txt(ticker, date, advice, self.result_dir, self.postfix)
        time.sleep(self.sleep_s)
        return advice
