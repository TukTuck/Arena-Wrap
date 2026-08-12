@echo off
cd /d "%USERPROFILE%\Arena Wrap\privategpt-sprachassistent\diktat"
"%USERPROFILE%\diktat\.venv\Scripts\python" sprachassistent.py %*
echo.
echo Beendet. (Dieses Fenster schliessen)
pause >nul
