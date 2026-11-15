@echo off
cd /d "%~dp0"
where pyw >nul 2>&1
if %errorlevel%==0 (
    start "" pyw -3 "%~dp0voice_tray_app.py"
) else (
    start "" pythonw "%~dp0voice_tray_app.py"
)
