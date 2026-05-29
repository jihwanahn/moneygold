"""가속화 장기투자 (Accelerated Long-Term / DGI) 전략.

모멘텀 시스템(Weinstein Stage + Minervini Template + Darvas)과 **완전히 분리된**
별도의 DGI(Dividend Growth Investing) 스크리너.

컨셉: 배당 재투자(DRIP) + 정기 추가납입 + 주가 우상향 + 배당성장의 복리 선순환으로
자산증식을 가속화한다. ValueTrader 가치투자 점수표에서 영감을 받았으나, deep-value
(저PER/저PBR) 게이트는 DGI 컨셉과 맞지 않아 제외하고 배당 가중치를 강화.

구성:
  - dart_client.py    DART OpenAPI (자사주 소각·보유)
  - kis_dividends.py  KIS 배당 이력 fetcher (PR-B)
  - scoring_rules.py  100점 DGI 점수 함수 (PR-C)
  - scoring.py        score_ticker() + ScoreBreakdown (PR-C)
  - drip.py           DRIP/적립식 시뮬레이터 (PR-D)
"""
from .dart_client import DartClient

__all__ = ["DartClient"]
