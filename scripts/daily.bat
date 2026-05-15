@echo off
REM moneygold 일일 운영 (Windows)
REM Task Scheduler에 등록해 매일 17:00 자동 실행 권장.
REM Usage (Anaconda Prompt):  scripts\daily.bat
setlocal
if "%MONEYGOLD_ENV%"=="" set MONEYGOLD_ENV=moneygold

cd /d "%~dp0\.."
call conda activate %MONEYGOLD_ENV% || exit /b 1

if not exist store\logs mkdir store\logs

echo [%date% %time%] sync --daily
python -m moneygold.cli.sync --daily || exit /b 1

echo [%date% %time%] signals --export
python -m moneygold.cli.signals --export --top 50 || exit /b 1

echo [%date% %time%] Done.

endlocal
