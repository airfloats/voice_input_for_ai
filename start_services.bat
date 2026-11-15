@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ===================================================
echo   VoiceInputAI 本地核显加速服务 (DirectML + Vulkan)
echo ===================================================
echo.
echo [1/2] 启动 SenseVoice DirectML 语音识别服务 (端口 18081)...
start "SenseVoice-ASR (18081)" cmd /k "python "%~dp0sensevoice_server.py""

echo [2/2] 启动 Qwen3-4B Vulkan 核显加速润色服务 (端口 18082)...
start "Qwen3-4B-Vulkan (18082)" cmd /k ""%~dp0bin\llama\llama-server.exe" -m "%~dp0models\Qwen3-4B-Q4_K_M.gguf" --host 127.0.0.1 --port 18082 -ngl 99 -fa off -np 1 -c 1024"

echo.
echo 服务已启动！
echo   - ASR 接口: http://127.0.0.1:18081/inference
echo   - LLM 接口: http://127.0.0.1:18082/v1/chat/completions
echo.
pause
