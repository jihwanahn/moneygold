"""moneygold strategy subpackages.

이 디렉터리는 *signals.py와 평행한* 전략 패키지를 담는다. 메인 signals.py가
BUY/HOLD/SELL/Watchlist 결정의 단일 진실인 것은 그대로 유지하되, 책별/
스타일별로 독립적인 진입·청산 시그널 모듈을 여기에 추가한다.

현재 패키지:
  - momentum_breakout : N일 신고가 돌파 + 거래대금 스파이크 (Minervini/O'Neil 스타일)
"""
