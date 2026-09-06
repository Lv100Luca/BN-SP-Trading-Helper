@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment and installing dependencies - one-time, takes a minute...
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -r requirements.txt || goto :err
)
start "" .venv\Scripts\pythonw.exe run.py %*
exit /b 0
:err
echo.
echo Setup failed. Is Python 3.10+ installed and on PATH?
pause
