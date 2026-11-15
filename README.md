# VoiceInputAI

Windows 11 本地核显实时语音输入法（DirectML ASR + Vulkan LLM 轻量校对 + Win32 底层全局热键与无焦点流式上屏）。

## 核心特性

- **极低延迟本地识别**：基于 **SenseVoice-Small FP16 ONNX (DirectML)** 在核显上加速推理，支持中英混排、ITN 数字规范化与实时流式出字。
- **边说边在光标处出字（Zero-Clipboard 流式上屏）**：通过 Win32 `SendInput` Unicode 事件直接在任意编辑器/终端光标处实时更新文字，不破坏剪贴板（仅超长句可设阈值回退粘贴）。
- **松开后智能轻度校对**：可选本地 **Qwen3-4B (llama.cpp Vulkan)** 核显润色，仅对存在口癖/明显语病/缺标点的句段触发最小化校对，保留原始用词与技术术语。
- **Win32 底层全局热键拦截 (`WH_KEYBOARD_LL`)**：
  - 默认支持拦截 Windows 11 **Copilot 物理键 (`LShift + LWin + VK_F23`)**，防止弹出 Windows 搜索/Copilot 面板，并安全释放修饰键（带 `0xE8` 掩码防触发输入法中英切换或开始菜单）。
  - 设置面板支持**可视化录制并多选同时生效**任意单键（如 `RAlt`、`CapsLock`、`LWin` 等）或组合键（如 `Ctrl+Space`、`Ctrl+RAlt`）。
- **无焦点悬浮预览气泡 & 托盘控制台**：
  - `WS_EX_NOACTIVATE` 悬浮窗不抢焦点、支持左键拖动与位置记忆。
  - 托盘菜单支持状态查看、一键启停/重启后端服务、一键恢复（重置键盘钩子 + PortAudio 麦克风热重连）、GUI 偏好设置。

## 项目结构

```text
voice_input_for_ai/
├── voice_tray_app.py        # 托盘主程序、Win32 热键钩子、实时上屏、设置界面与后端托管
├── sensevoice_server.py     # SenseVoice FP16 ONNX DirectML 本地 HTTP ASR 服务 (:18081)
├── sensevoice_engine/       # SenseVoice ONNX 纯净推理封装（前端梅尔频谱 / Encoder / CTC Decoder / 热词）
├── scripts/polish_test.py   # Qwen3-4B 润色接口测试脚本
├── start_tray.bat           # 启动系统托盘主程序
├── start_services.bat       # 手动调试启动 ASR + LLM 后端服务
└── requirements.txt         # Python 依赖清单
```

## 环境与目录准备

1. **安装 Python 依赖**（Windows Python 3.11+）：
   ```bash
   pip install -r requirements.txt
   ```
   也可将重型依赖（如 `onnxruntime-directml`、`sentencepiece` 等）独立解压到 `py_deps/` 目录。

2. **放置模型与推理二进制**（未包含在仓库内）：
   - `models/sensevoice/`：放入 `SenseVoice-Encoder.fp16.onnx`、`SenseVoice-CTC.fp16.onnx`、`tokenizer.bpe.model`
   - `models/Qwen3-4B-Q4_K_M.gguf`（若开启本地 LLM 润色）
   - `bin/llama/llama-server.exe`（llama.cpp Vulkan 版本）

3. **启动**：
   双击 `start_tray.bat`（或直接运行 `pythonw voice_tray_app.py`），托盘主程序会自动拉起 `:18081` 与 `:18082` 后端服务。
   - 如需开机以高权限启动并屏蔽 Copilot 键系统响应，可在托盘右键菜单或设置界面开启 **开机自动启动**（按需创建高权限计划任务 `VoiceInputAI_Tray`）。
