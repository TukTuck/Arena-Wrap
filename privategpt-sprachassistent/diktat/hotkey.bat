@echo off
cd /d "%USERPROFILE%\Arena Wrap\privategpt-sprachassistent\diktat"
"%USERPROFILE%\diktat\.venv\Scripts\python" hotkey.py %*
echo.
echo Hotkey beendet. (Dieses Fenster schliessen)
pause >nul
