@echo off
REM moneygold 초기 셋업 (Windows)
REM Usage (Anaconda Prompt):  scripts\setup.bat
setlocal
if "%MONEYGOLD_ENV%"=="" set MONEYGOLD_ENV=moneygold

cd /d "%~dp0\.."

where conda >nul 2>nul
if errorlevel 1 (
    echo [X] conda 명령을 찾을 수 없습니다. Miniconda/Anaconda Prompt에서 실행하세요.
    echo     https://docs.conda.io/en/latest/miniconda.html
    exit /b 1
)

call conda env list | findstr /B /C:"%MONEYGOLD_ENV% " >nul
if errorlevel 1 (
    echo [^>] Creating conda env "%MONEYGOLD_ENV%" (Python 3.11) ...
    call conda create -n %MONEYGOLD_ENV% python=3.11 -y || exit /b 1
) else (
    echo [v] conda env "%MONEYGOLD_ENV%" already exists.
)

call conda activate %MONEYGOLD_ENV% || exit /b 1

echo [^>] Installing project (editable) with dev + ui extras ...
pip install -e ".[dev,ui]" || exit /b 1

if not exist .env (
    copy /Y .env.example .env >nul
    echo.
    echo [v] .env created from .env.example
    echo.
    echo .env 파일을 열어 별표 ^(*^) 표시된 4줄을 채우세요:
    echo     KRX_ID, KRX_PW, KIS_APP_KEY, KIS_APP_SECRET
    echo.
    echo   notepad .env   또는   code .env
) else (
    echo [v] .env already exists — 그대로 사용합니다.
)

echo.
echo [^>] KIS 사전검증 실행^(.env 채운 후^):
echo     python scripts\verify_kis.py
echo.
echo [DONE] Setup 완료. 다음 단계: scripts\init_data.bat ^(~60분^)

endlocal
