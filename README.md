# VoiceInputAI (`voice_input_for_ai`)

> **Windows 11 开源、100% 本地核显加速、真正「边说边在光标处实时出字」的 AI 语音输入法。**
> **零云端 API、零订阅费用、零剪贴板污染、断网可用，你的声音与文字全程不出本机。**

---

## 为什么做这个项目？（与同类方案的差异）

市面上常见的 AI 语音输入工具主要分两类，但各自存在明显痛点：

| 维度 | 云端订阅型商业工具<br>*(如 Wispr Flow 等)* | 传统开源离线输入工具<br>*(如 Handy / CapsWriter 等)* | **VoiceInputAI (本项目)** |
|---|---|---|---|
| **隐私与网络** | 音频与文本上传云端服务器 | 完全本地离线 | **100% 本地离线（核显直跑，断网可用）** |
| **使用成本** | 月度/年度订阅收费或 API 按量计费 | 免费开源 | **永久免费开源，无任何 API Key 依赖** |
| **出字时机** | 多数需说完松手后等待云端返回 | 需说完松手后推理，再一次性贴出 | **边说边实时流式上屏**（~420ms 刷新） |
| **上屏方式** | 多数借用系统剪贴板 `Ctrl+V` | 多数写入剪贴板后粘贴 | **Zero-Clipboard `SendInput` Unicode 直写**（保留剪贴板内容） |
| **轻量核显加速** | 依赖云端算力 | 多偏向 CPU 或 NVIDIA CUDA | **专为轻薄本核显优化**（DirectML + Vulkan） |
| **LLM 轻校对** | 强改写，常把口语总结改偏原意 | 需外接或手动触发角色 | **Qwen3-4B 核显按需触发流式微校对**，严禁改词删句 |
| **Win11 AI 键支持** | 部分支持 | 多以 `CapsLock` / 鼠标侧键为主 | **底层硬件级拦截 `Copilot (F23)` + 任意多热键同时生效** |

---

## 核心技术亮点

### 1. 真正「边说边在光标处实时出字」（Zero-Clipboard 流式上屏）

很多语音工具虽然叫“实时语音”，实际上依然是**“按住说话 → 松开等待 → 占用剪贴板粘贴”**。VoiceInputAI 在你按住热键说话的过程中，就直接在当前任意窗口（VS Code、终端、浏览器、微信、Word 等）的光标处持续出字：

- **非阻塞双优先 ASR 调度**（`sensevoice_server.py`）：
  实时预览请求采用非阻塞锁（`blocking=False`），若引擎正忙则自动跳过当帧预览，确保松开热键时的最终识别**零排队积压**。
- **单调前缀差分 + 防快捷键误触算法**（`_sync_cursor_text_locked`）：
  - 在物理热键（如 Copilot 键伴随的 `LShift + LWin`）仍处于按下状态时，程序**严禁发送 `VK_BACK` 退格**（避免 Electron/Chromium 类应用误判为 `Win+Shift+Backspace` 导致光标错乱），而是执行**单调尾部追加**并精确跟踪已输出字符串；
  - 松开热键后触发最终校验，仅对尾段做最小前缀差分；当需要替换超过 8 个字符时，自动采用 **`Shift+Left` 批量选区 + 一次性替换**，避免连发几十次退格在现代富文本/IDE 中造成闪烁和卡顿。
- **不破坏系统剪贴板**：
  默认全程走 Win32 `SendInput` + `KEYEVENTF_UNICODE` 注入，你刚刚 `Ctrl+C` 复制的代码或链接不会被覆盖；仅当单次上屏超过长句阈值（默认 `300` 字，可在设置里调节 `20–1000` 字）时才自动改走剪贴板安全粘贴。
- **无焦点悬浮预览气泡**：
  `WS_EX_NOACTIVATE` 悬浮窗永不抢夺目标窗口输入焦点，支持左键自由拖动并自动记住坐标（`overlay_pos.json`）。

### 2. 100% 本地核显双引擎（无需独显、零费用、全离线）

无需 NVIDIA 独立显卡，Windows 11 轻薄本自带核显（如 Intel Arc 核显等支持 DirectML 与 Vulkan 的集显）即可完成全部识别与润色：

- **ASR 语音识别：`SenseVoice-Small FP16 ONNX (DirectML)`**（本地端口 `18081`）
  - 支持中英混排、ITN 中文数字规范化（如自动将语音数字转为阿拉伯数字）；
  - 预设 **`dml_pad_to=30`** 固定长度填充，配合启动双轮预热，彻底避免 DirectML 因音频时长变化反复重编译 GPU 算子导致的停顿。
- **LLM 智能校对：`Qwen3-4B-Q4_K_M` via `llama.cpp (Vulkan)`**（本地端口 `18082`）
  - 通过 `-ngl 99` 将推理全层卸载至核显，开启 `cache_prompt` 复用系统提示词 KV Cache，并关闭思考过程（`enable_thinking=False`）直接流式输出；
  - **按需介入**：仅当识别结果含句中口癖（如“嗯、啊、呃、哦、唉”）或缺少句末标点时触发校对；本身已干净完整的句子直接定稿，省去无效等待；
  - **严格保真 Prompt**：只删口癖、改明显同音错别字和补标点，强制逐字保留用户原本用词、句式及英文/技术术语（如 `API`、`fakeTCP`、`check`），严禁擅自总结或替换同义词。

### 3. 专为 Windows 11 打造的底层热键拦截 & 自愈守护

- **硬件级屏蔽 Copilot 物理键弹窗**（`LowLevelKeyboardListener`）：
  Windows 11 笔记本的 Copilot 键硬件上是 `LShift + LWin + VK_F23` 组合发送。原生 `pynput` 过滤返回 `False` 仍会透传系统触发 Windows Search / 开始菜单；本项目重写了底层 `WH_KEYBOARD_LL` 转换逻辑并调用 `suppress_event()` 真正吞掉热键事件，同时在释放修饰键时注入 `0xE8` 未分配键码掩码与 `LCtrl` 撤防脉冲，防止孤立 `Shift` 意外切换中英文输入法或弹出开始菜单。
- **可视化的多热键设置面板**：
  支持图形化录制单键（`Copilot (VK_F23)`、`RAlt`、`CapsLock`、`LWin` 等）、纯修饰组合键（如 `Ctrl+RAlt`）或常规组合键（如 `Ctrl+Space`），并可**通过多选复选框让多个热键同时生效**。
- **高可用工程细节**：
  - 支持 `120ms` 按键去抖，防止物理微动瞬间断触导致录音被截断；
  - 笔记本休眠唤醒后若 PortAudio 音频流异常无采样，自动热重载音频后端；
  - 后台健康监控线程检测到 ASR / LLM 异常退出时自动拉起；托盘内置 **一键恢复（重置热键 + 麦克风）** 与 **高权限计划任务自启**（避免普通权限钩子被管理员权限前景窗口的 UIPI 拦截）。

---

## 整体工作流架构

```text
[按下自定义热键 / Copilot 物理键]
        │
        ▼ (Win32 WH_KEYBOARD_LL 吞掉系统按键 + 0xE8 掩码安全撤防)
[sounddevice 16kHz 连续采集]
        │
        ├──▶ 说话期间 (~420ms 间隔，非阻塞预览)
        │         └──▶ SenseVoice FP16 ONNX (DirectML :18081)
        │                   ├──▶ WS_EX_NOACTIVATE 悬浮窗实时预览
        │                   └──▶ SendInput Unicode 单调前缀差分 → 直接在光标处出字
        │
        └──▶ 松开热键 (120ms 去抖确认)
                  └──▶ SenseVoice 最终完整识别
                            ├──▶ 语句已完整 ───────────────────┐
                            └──▶ 含口癖 / 缺标点 ─▶ Qwen3-4B    │
                                 (Vulkan :18082 流式轻校对)     │
                                                               ▼
                                          LCP 差分 / Shift+Left 原地替换为终稿
```

---

## 项目结构

```text
voice_input_for_ai/
├── voice_tray_app.py        # 托盘总控、Win32 底层热键钩子、实时差分上屏、GUI 设置与后端托管
├── sensevoice_server.py     # SenseVoice FP16 ONNX DirectML 本地 HTTP ASR 服务 (:18081)
├── sensevoice_engine/       # 纯净版 SenseVoice ONNX 推理封装（梅尔频谱 / Encoder / CTC / 热词 / ITN）
├── scripts/polish_test.py   # Qwen3-4B 本地润色接口独立测试脚本
├── start_tray.bat           # 一键启动系统托盘主程序
├── start_services.bat       # 手动调试启动 ASR + LLM 双后端窗口
└── requirements.txt         # Python 核心依赖清单
```

---

## 快速开始

### 1. 环境准备

- 操作系统：**Windows 10 / 11 (x64)**
- Python：**3.11 – 3.14**
- 安装依赖：

```bash
pip install -r requirements.txt
```

> 若希望保持主 Python 环境精简，也可将 `onnxruntime-directml`、`sentencepiece`、`numpy` 等推理依赖单独安装到仓库根目录的 `py_deps/` 下（服务脚本会自动优先加载）。

### 2. 下载模型与推理框架（仓库未包含大体积二进制文件）

所有依赖的开源模型与 GPU 推理框架均提供免费官方下载地址，按需获取后放入对应目录即可：

#### 下载总览

| 组件 | 作用 | 下载地址（官方源 / 国内镜像） | 本地放置路径 |
|---|---|---|---|
| **SenseVoice-Small** | 开源语音识别原模型（ASR） | • GitHub: https://github.com/FunAudioLLM/SenseVoice<br>• ModelScope: https://modelscope.cn/models/iic/SenseVoiceSmall<br>• Hugging Face: https://huggingface.co/FunAudioLLM/SenseVoiceSmall | 见下方 ONNX 导出/获取方式 |
| **SenseVoice-ONNX FP16** | Pure-ONNX 导出脚本与 `fp16` 模型 | • 导出项目: https://github.com/HaujetZhao/SenseVoice-ONNX<br>• 预导出模型（可在整合包内提取）: https://github.com/HaujetZhao/CapsWriter-Offline/releases | `models/sensevoice/`<br>(`SenseVoice-Encoder.fp16.onnx`, `SenseVoice-CTC.fp16.onnx`, `tokenizer.bpe.model`) |
| **ONNX Runtime DirectML** | Windows 核显 / GPU 通用推理后端 | • PyPI: https://pypi.org/project/onnxruntime-directml/<br>• GitHub: https://github.com/microsoft/onnxruntime | `pip install onnxruntime-directml`（或解压至 `py_deps/`） |
| **Qwen3-4B-Q4_K_M.gguf** | 本地轻量语义纠偏与标点补全 | • Hugging Face: https://huggingface.co/Qwen/Qwen3-4B-GGUF<br>• 直链: https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf<br>• ModelScope: https://modelscope.cn/models/Qwen/Qwen3-4B-GGUF | `models/Qwen3-4B-Q4_K_M.gguf` |
| **llama.cpp (Windows Vulkan)** | Qwen3-4B 核显 Vulkan 推理服务端 | • Releases: https://github.com/ggml-org/llama.cpp/releases<br>*(选择 `llama-*-bin-win-vulkan-x64.zip`)*<br>• GitHub: https://github.com/ggml-org/llama.cpp | `bin/llama/`<br>*(含 `llama-server.exe`、`ggml-vulkan.dll` 等)* |

#### 快速获取命令示例

1. **导出或获取 SenseVoice-Small FP16 ONNX 模型**（放入 `models/sensevoice/`）：
   ```bash
   # 方式 A：使用 SenseVoice-ONNX 自行导出 fp16 模型
   pip install modelscope
   modelscope download --model iic/SenseVoiceSmall
   git clone https://github.com/HaujetZhao/SenseVoice-ONNX.git
   # 按该仓库说明依次运行 01-Export-Encoder.py / 02-Export-CTC.py / 03-Prepare-Assets.py

   # 方式 B：从 CapsWriter-Offline Releases 下载整合包，提取其中已导出的 fp16.onnx 与 tokenizer.bpe.model
   ```
   确保 `models/sensevoice/` 内包含以下 3 个文件：
   - `SenseVoice-Encoder.fp16.onnx`
   - `SenseVoice-CTC.fp16.onnx`
   - `tokenizer.bpe.model`

2. **下载 Qwen3-4B GGUF 模型**（放入 `models/`，若不需要本地润色可跳过）：
   ```bash
   # Hugging Face CLI / ModelScope CLI 或浏览器直链下载
   pip install modelscope
   modelscope download --model Qwen/Qwen3-4B-GGUF Qwen3-4B-Q4_K_M.gguf --local_dir ./models
   ```

3. **下载 llama.cpp Windows Vulkan 版**（解压放入 `bin/llama/`）：
   - 打开 https://github.com/ggml-org/llama.cpp/releases
   - 下载最新的 `llama-<tag>-bin-win-vulkan-x64.zip`
   - 将压缩包内全部 `.exe` / `.dll` 解压到 `bin/llama/`（需包含 `llama-server.exe` 与 `ggml-vulkan.dll`）。

### 3. 启动使用

双击 `start_tray.bat`（或直接运行 `pythonw voice_tray_app.py`）：
- 程序会在后台无黑框自动拉起 `:18081` (SenseVoice DirectML) 与 `:18082` (Qwen3-4B Vulkan) 服务；
- 托盘图标变为绿色后即可按住热键说话、实时出字；
- 右键托盘图标 → **⚙ 设置...** 可调整热键组合、预览刷新间隔、剪贴板回退阈值，并可一键开启 **开机自动启动（高权限计划任务，防前后台 UIPI 拦截及 Copilot 弹窗）**。

---

## 致谢与上游开源项目

- [FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice) — 多语种语音识别模型 SenseVoice-Small
- [HaujetZhao/SenseVoice-ONNX](https://github.com/HaujetZhao/SenseVoice-ONNX) — 纯净 ONNX 推理封装（`sensevoice_engine`）、DirectML 优化思路与 CTC 热词雷达
- [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) — 跨平台 Vulkan LLM 本地推理后端
- [Qwen/Qwen3-4B-GGUF](https://huggingface.co/Qwen/Qwen3-4B-GGUF) — 轻量高效的中文语义校对底座模型
- [microsoft/onnxruntime](https://github.com/microsoft/onnxruntime) — DirectML 硬件加速运行时

