# VoiceInputAI (`voice_input_for_ai`)

> **Windows 11 本地核显实时 AI 语音输入法：按住热键「边说边在光标处出字」，松开按需轻校对。**
> **100% 本地离线 · 零云端 / 零订阅 · 无需独显 · 不破坏剪贴板。**

---

## 为什么选择它？

| 对比维度 | 云端订阅工具 *(如 Wispr Flow)* | 传统离线工具 *(如 Handy / CapsWriter)* | **VoiceInputAI (本项目)** |
|---|---|---|---|
| **隐私 / 成本** | 音频上传云端，订阅付费 | 本地免费 | **100% 本地离线，永久免费开源** |
| **出字方式** | 多为说完后等待并返回粘贴 | 多为松手后推理，剪贴板粘贴 | **边说边直接在光标处出字**（Zero-Clipboard Unicode 直写） |
| **硬件加速** | 云端算力 | 多偏 CPU / NVIDIA CUDA | **专为轻薄本核显优化**（DirectML + Vulkan） |
| **LLM 校对** | 易大幅改写或偏离原意 | 需手动触发角色 | **Qwen3-4B 核显按需微校对**（仅去口癖/补标点，不改用词） |
| **Win11 键位** | 部分支持 | 多用 `CapsLock` / 鼠标侧键 | **硬件级吞掉 `Copilot (F23)` 弹窗 + 任意多热键并发** |

---

## 核心特性

- **边说边在光标处实时出字（Zero-Clipboard）**
  - **真流式上屏**：说话期间约每 `420ms` 增量推理一次，直接用 Win32 `SendInput` Unicode 注入当前焦点窗口（IDE / 终端 / 浏览器 / 微信），不覆盖剪贴板（仅超长文本超阈值才自动切粘贴）。
  - **不卡顿不误删**：预览采用非阻塞锁（松手最终识别永远优先）；按住热键期间仅单调追加尾部新字（避免 `Win+Shift+Backspace` 误触发 Electron 快捷键）；松手替换大段文本时用 `Shift+Left` 批量选区一次性替换。
  - **无焦点悬浮气泡**：`WS_EX_NOACTIVATE` 不抢焦点，支持左键拖动并记忆位置。
- **双引擎 100% 本地核显加速（零独显要求）**
  - **SenseVoice-Small FP16 ONNX (DirectML, `:18081`)**：支持中英混排与数字 ITN，预设 `dml_pad_to=30` 定长填充 + 双轮预热，避免变长音频重编译卡顿。
  - **Qwen3-4B-Q4_K_M (`llama.cpp` Vulkan, `:18082`)**：`-ngl 99` 全层跑在核显上，开启 Prompt Cache 与流式输出；**仅在含口癖或缺标点时触发**，严禁改词、删句或动英文/技术词。
- **Win11 AI PC 底层适配与高可用**
  - **屏蔽 Copilot 键弹窗**：重写 `WH_KEYBOARD_LL` 钩子真吞掉 `LShift + LWin + VK_F23`，配合 `0xE8` 掩码安全撤防修饰键，防弹开始菜单或误切输入法。
  - **多热键与一键自愈**：设置面板支持录制任意单键/组合键并**勾选多个同时生效**；休眠唤醒自动重载麦克风、后端掉线自动拉起、托盘支持一键恢复与高权限自启。

---

## 工作流程

```text
按下热键 (WH_KEYBOARD_LL 吞掉系统按键)
  ├── 说话期间 (~420ms) ──▶ SenseVoice DirectML (:18081) ──▶ 悬浮窗预览 + SendInput 单调追加上屏
  └── 松开热键 (120ms去抖) ─▶ SenseVoice 最终识别
                               ├── 语句已完整 ──────────────────────┐
                               └── 含口癖/缺标点 ─▶ Qwen3-4B Vulkan ─┴─▶ LCP差分 / Shift+Left 原地定稿
```

---

## 快速开始

### 1. 安装依赖（Windows 10/11, Python 3.11–3.14）

```bash
pip install -r requirements.txt
```

> 也可将 `onnxruntime-directml`、`sentencepiece`、`numpy` 等推理库安装至仓库根目录 `py_deps/`，服务脚本会自动优先加载。

### 2. 下载模型与推理框架

| 组件 | 官方下载地址 | 放置路径 |
|---|---|---|
| **SenseVoice-Small 原模型** | [GitHub](https://github.com/FunAudioLLM/SenseVoice) · [ModelScope](https://modelscope.cn/models/iic/SenseVoiceSmall) · [Hugging Face](https://huggingface.co/FunAudioLLM/SenseVoiceSmall) | 用于导出 ONNX |
| **SenseVoice FP16 ONNX** | 导出脚本：[SenseVoice-ONNX](https://github.com/HaujetZhao/SenseVoice-ONNX)<br>或从 [CapsWriter-Offline Releases](https://github.com/HaujetZhao/CapsWriter-Offline/releases) 整合包提取 | `models/sensevoice/`<br>(`SenseVoice-Encoder.fp16.onnx`, `SenseVoice-CTC.fp16.onnx`, `tokenizer.bpe.model`) |
| **ONNX Runtime DirectML** | [PyPI](https://pypi.org/project/onnxruntime-directml/) · [GitHub](https://github.com/microsoft/onnxruntime) | `pip install onnxruntime-directml`（或放 `py_deps/`） |
| **Qwen3-4B-Q4_K_M.gguf** | [Hugging Face](https://huggingface.co/Qwen/Qwen3-4B-GGUF) · [直链下载](https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf) · [ModelScope](https://modelscope.cn/models/Qwen/Qwen3-4B-GGUF) | `models/Qwen3-4B-Q4_K_M.gguf` *(可选)* |
| **llama.cpp (Win Vulkan)** | [Releases](https://github.com/ggml-org/llama.cpp/releases) *(选 `llama-*-bin-win-vulkan-x64.zip`)* | `bin/llama/` *(含 `llama-server.exe` 与 `ggml-vulkan.dll`)* |

<details>
<summary>命令行下载参考</summary>

```bash
# 1. SenseVoice ONNX 导出
pip install modelscope
modelscope download --model iic/SenseVoiceSmall
git clone https://github.com/HaujetZhao/SenseVoice-ONNX.git

# 2. Qwen3-4B GGUF 下载
modelscope download --model Qwen/Qwen3-4B-GGUF Qwen3-4B-Q4_K_M.gguf --local_dir ./models
```

</details>

### 3. 启动

双击 [`start_tray.bat`](start_tray.bat)（或运行 `pythonw voice_tray_app.py`）：
- 自动无黑框拉起 `:18081` 与 `:18082` 后端，托盘变绿即可说话；
- 右键托盘 → **⚙ 设置...** 可录制热键（支持多选同时生效）及开启高权限开机自启。

---

## 目录说明

```text
voice_input_for_ai/
├── voice_tray_app.py        # 托盘主控、Win32 热键钩子、实时上屏、设置弹窗、后端托管
├── sensevoice_server.py     # SenseVoice DirectML 本地 ASR 服务 (:18081)
├── sensevoice_engine/       # Pure-ONNX SenseVoice 推理封装
├── scripts/polish_test.py   # Qwen3-4B 润色接口测试脚本
├── start_tray.bat           # 启动托盘主程序
└── requirements.txt         # Python 依赖清单
```

## 致谢

- [FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice) · [HaujetZhao/SenseVoice-ONNX](https://github.com/HaujetZhao/SenseVoice-ONNX) · [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) · [Qwen/Qwen3-4B-GGUF](https://huggingface.co/Qwen/Qwen3-4B-GGUF) · [microsoft/onnxruntime](https://github.com/microsoft/onnxruntime)
