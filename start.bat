@echo off
chcp 65001 >nul
setlocal EnableExtensions

REM Check admin privileges; if not elevated, trigger Windows UAC prompt automatically
net session >nul 2>&1
if errorlevel 1 (
    echo [VoiceInputAI] Requesting Administrator privileges ^(please click Yes in UAC prompt^)...
    set "VOICE_SELF=%~f0"
    set "VOICE_APP_DIR=%~dp0"
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath $env:VOICE_SELF -Verb RunAs -WorkingDirectory $env:VOICE_APP_DIR } catch { exit 1 }"
    if errorlevel 1 (
        echo [VoiceInputAI] Administrator permission denied or cancelled.
        pause
        exit /b 1
    )
    exit /b 0
)

cd /d "%~dp0"
set "VOICE_APP_DIR=%~dp0"

echo ===================================================
echo   VoiceInputAI Launch / Restart ^(Admin Mode^)
echo ===================================================
echo.

echo [1/3] Cleaning up stale or residual VoiceInputAI processes...
schtasks /End /TN "VoiceInputAI_Tray" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$base = $env:VOICE_APP_DIR.TrimEnd('\') + '\'; for ($i = 0; $i -lt 5; $i++) { $procs = Get-CimInstance Win32_Process | Where-Object { ($_.Name -in @('python.exe','pythonw.exe','py.exe','pyw.exe','llama-server.exe','whisper-server.exe')) -and ($_.CommandLine -like '*voice_tray_app.py*' -or $_.CommandLine -like '*sensevoice_server.py*' -or ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase))) }; if (-not $procs) { break }; foreach ($p in $procs) { taskkill /F /PID $p.ProcessId 2>$null | Out-Null }; Start-Sleep -Milliseconds 800 }"

if exist "%~dp0logs\asr_server.pid" del /f /q "%~dp0logs\asr_server.pid" >nul 2>&1

echo [2/3] Starting elevated VoiceInputAI...
schtasks /Query /TN "VoiceInputAI_Tray" >nul 2>&1
if not errorlevel 1 (
    schtasks /Run /TN "VoiceInputAI_Tray" >nul 2>&1
    if not errorlevel 1 goto :LAUNCHED
)

where pyw >nul 2>&1
if not errorlevel 1 (
    start "" /D "%~dp0" pyw -3 "%~dp0voice_tray_app.py"
    goto :LAUNCHED
)
where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" /D "%~dp0" pythonw "%~dp0voice_tray_app.py"
    goto :LAUNCHED
)
start "" /D "%~dp0" python "%~dp0voice_tray_app.py"

:LAUNCHED
echo [3/3] Launch command sent.
echo       Cold model warmup takes ~15-45s; wait for the tray icon to turn green.
powershell -NoProfile -Command "Start-Sleep -Seconds 2" >nul 2>&1
exit /b 0
