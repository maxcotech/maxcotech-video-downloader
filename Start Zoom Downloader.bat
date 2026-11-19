@echo off
setlocal
cd /d "%~dp0"
title Zoom Batch Downloader

rem The engine lives in a short path so Windows' 260-character limit is never hit.
set "VENV=%LOCALAPPDATA%\ZoomBatchDownloader\venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VPYW=%VENV%\Scripts\pythonw.exe"

if exist "%VPY%" goto :check

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% --version >nul 2>nul
if errorlevel 1 goto :nopython

echo First run: setting things up. This takes about a minute...
%PY% -m venv "%VENV%"
if errorlevel 1 goto :fail

:check
"%VPY%" -c "import yt_dlp, requests" >nul 2>nul
if not errorlevel 1 goto :launch
echo Installing the download engine...
"%VPY%" -m pip install --disable-pip-version-check -q -U "yt-dlp[default]" requests
if errorlevel 1 goto :fail

:launch
start "" "%VPYW%" "%~dp0zoom_downloader.py"
exit /b 0

:nopython
echo.
echo Python is not installed. Open PowerShell and run:
echo     winget install Python.Python.3.12
echo then double-click this file again.
pause
exit /b 1

:fail
echo.
echo Setup failed. Check your internet connection and try again.
pause
exit /b 1
