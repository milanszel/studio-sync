@echo off
cd /d "%~dp0"
if exist ".venv312\Scripts\python.exe" goto deps
py -3.12 -m venv .venv312
if errorlevel 1 goto fail
:deps
rem Install only on first launch or when requirements change.
fc /b requirements.txt .venv312\requirements-installed.txt >nul 2>&1
if not errorlevel 1 goto run
.venv312\Scripts\python.exe -m pip install --only-binary=:all: --no-binary=proxy-tools -r requirements.txt
if errorlevel 1 goto fail
copy /y requirements.txt .venv312\requirements-installed.txt >nul
:run
.venv312\Scripts\python.exe web_app.py
pause
exit /b
:fail
echo Studio Sync requires Python 3.12. Install the Windows installer from:
echo https://www.python.org/downloads/release/python-31210/
echo If Python 3.12 is installed already, copy the dependency error above.
pause
