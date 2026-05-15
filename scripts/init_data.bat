@echo off
REM moneygold 초기 데이터 수집 (Windows)
REM 첫 실행 시 ~60-100분. 자동 재개 가능.
REM Usage (Anaconda Prompt):  scripts\init_data.bat
setlocal
if "%MONEYGOLD_ENV%"=="" set MONEYGOLD_ENV=moneygold

cd /d "%~dp0\.."
call conda activate %MONEYGOLD_ENV% || exit /b 1

echo ==========================================================
echo  moneygold 초기 데이터 수집
echo ==========================================================
echo.
echo [1/4] KIS 사전검증 ...
python scripts\verify_kis.py
if errorlevel 1 (
    echo [X] KIS 검증 실패. .env의 KIS_APP_KEY/SECRET 확인.
    exit /b 1
)

echo.
echo [2/4] 종목 마스터^.일봉 2년 백필^.지수 ^(~40분^)
python -m moneygold.cli.sync --backfill || exit /b 1

echo.
echo [3/4] 분기 펀더멘털 ^(KIS finance, ~15분^)
python -m moneygold.cli.sync --financials || exit /b 1

echo.
echo [4/4] 컨센서스 — 애널 목표가/EPS 추정 ^(yfinance, ~43분, 선택^)
set /p YN="    컨센서스 sync 진행? (Y/n): "
if /i "%YN%"=="" set YN=Y
if /i "%YN:~0,1%"=="Y" (
    python -m moneygold.cli.sync --consensus || exit /b 1
) else (
    echo     skip — 나중에 'python -m moneygold.cli.sync --consensus'로 실행 가능.
)

echo.
echo [DONE] 초기 데이터 수집 완료.
echo.
echo 다음 단계:
echo   - 대시보드:        scripts\run_dashboard.bat
echo   - 콘솔 시그널:     python -m moneygold.cli.signals --top 30
echo   - 매일 운영:       scripts\daily.bat

endlocal
