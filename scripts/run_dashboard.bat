@echo off
REM moneygold 대시보드 (Windows)
REM Usage (Anaconda Prompt):  scripts\run_dashboard.bat
setlocal
if "%MONEYGOLD_ENV%"=="" set MONEYGOLD_ENV=moneygold

cd /d "%~dp0\.."
call conda activate %MONEYGOLD_ENV% || exit /b 1

if not exist store\meta\master.parquet (
    echo [!] 마스터 데이터가 없습니다. 먼저: scripts\init_data.bat
    exit /b 1
)

echo [^>] Starting Streamlit dashboard at http://localhost:8501 ...
echo     ^(종료: Ctrl+C^)
streamlit run src\moneygold\app\streamlit_app.py

endlocal
