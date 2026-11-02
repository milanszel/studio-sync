@echo off
rem Double click to build Studio Sync for Windows and make Studio-Sync-Setup-VERSION.exe.
setlocal
cd /d "%~dp0"

set "PY=py -3.12"
%PY% --version >nul 2>&1
if not errorlevel 1 goto havepython
rem No py launcher entry (as on build servers): use python if it is 3.12.
set "PY=python"
python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if errorlevel 1 goto nopython
:havepython

echo 1/5  Preparing the build environment (the first build downloads about 200 MB)...
if not exist ".venv-build\Scripts\python.exe" %PY% -m venv .venv-build
if not exist ".venv-build\Scripts\python.exe" goto fail
.venv-build\Scripts\python.exe -m pip install --quiet --only-binary=:all: --no-binary=proxy-tools -r requirements.txt -r packaging\build-requirements.txt
if errorlevel 1 goto fail
for /f "delims=" %%v in ('.venv-build\Scripts\python.exe packaging\version.py') do set VERSION=%%v
if "%VERSION%"=="" goto fail

echo 2/5  Building Studio Sync %VERSION%...
if exist "dist\Studio Sync" rmdir /s /q "dist\Studio Sync"
.venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean --log-level WARN --distpath dist --workpath build\pyinstaller packaging\studio_sync.spec
if errorlevel 1 goto fail

echo 3/5  Checking the built app...
if not exist build mkdir build
if exist build\self-test.txt del build\self-test.txt
start "" /wait "dist\Studio Sync\Studio Sync.exe" --self-test "%CD%\build\self-test.txt"
findstr /b "OK" build\self-test.txt >nul 2>&1
if errorlevel 1 (
  type build\self-test.txt 2>nul
  echo The built app is missing something, listed above.
  goto fail
)

echo 4/5  Finding Inno Setup...
call :findiscc
if not defined ISCC (
  echo      Installing Inno Setup with winget...
  winget install -e --id JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements
  call :findiscc
)
if not defined ISCC (
  echo Inno Setup is needed to make the installer: https://jrsoftware.org/isdl.php
  echo The app itself is ready in "dist\Studio Sync".
  goto fail
)

echo 5/5  Making the installer...
"%ISCC%" /Q /DAppVersion=%VERSION% packaging\windows_installer.iss
if errorlevel 1 goto fail

echo.
echo Done: dist\Studio-Sync-Setup-%VERSION%.exe
echo Run it on this PC and copy it to the other Windows laptops.
if defined CI exit /b 0
explorer /select,"dist\Studio-Sync-Setup-%VERSION%.exe"
pause
exit /b 0

:findiscc
set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
exit /b 0

:nopython
echo Building needs Python 3.12. Install it from:
echo https://www.python.org/downloads/release/python-31210/
:fail
echo.
echo Build failed. Copy the messages above for troubleshooting.
if not defined CI pause
exit /b 1
