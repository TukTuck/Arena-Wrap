@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Schaltwerk — OmniRoute Proxy-Austausch
cd /d "%~dp0"

echo.
echo  ============================================
echo   Schaltwerk  ·  Windows 10
echo   Schreibt nur manuelle Proxies nach OmniRoute
echo  ============================================
echo.

where py >nul 2>&1
if %errorlevel%==0 (
  set "PY=py -3"
  goto :have_py
)
where python >nul 2>&1
if %errorlevel%==0 (
  set "PY=python"
  goto :have_py
)

echo  [!] Python 3 wurde nicht gefunden.
echo      Bitte Python 3.11+ installieren:
echo      https://www.python.org/downloads/windows/
echo      Beim Setup Haken setzen: "Add python.exe to PATH"
echo.
pause
exit /b 1

:have_py
echo  Python:
%PY% --version
if errorlevel 1 (
  echo  [!] Python startet nicht.
  pause
  exit /b 1
)

echo  Pakete prüfen …
%PY% -m pip install --disable-pip-version-check -q -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo  [!] pip install fehlgeschlagen. Internet an? Als Admin nötig?
  pause
  exit /b 1
)

set HOST=127.0.0.1
set PORT=8765
echo.
echo  Starte Oberfläche auf http://127.0.0.1:8765
echo  Fenster offen lassen. Beenden: Strg+C
echo.
%PY% "%~dp0server.py"
echo.
echo  Server beendet.
pause
