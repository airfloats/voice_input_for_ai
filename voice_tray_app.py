# -*- coding: utf-8 -*-
"""
VoiceInputAI - Intel Arc 核显本地语音输入法托盘总控
支持：
1. 系统托盘图标与右键菜单（服务状态、开启/关闭、重启服务、开关核显润色/实时预览、退出）
2. 按住 Copilot 键 (F23，或可选 CapsLock) 说话 -> 悬浮气泡实时预览 -> 松开后 Whisper + Qwen3-4B 核显重整并自动粘贴上屏
3. 后台无黑框托管 whisper-server.exe (8081) 与 llama-server.exe (8082, Vulkan)
"""

from __future__ import annotations

import ctypes
import io
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import wave
from pathlib import Path
from typing import Optional

import numpy as np
import pyperclip
import pystray
import requests
import sounddevice as sd
from PIL import Image, ImageDraw
from pynput import keyboard as pynput_keyboard

# ================= 路径与服务配置 =================
BASE_DIR = Path(__file__).resolve().parent
BIN_WHISPER = BASE_DIR / "bin" / "whisper" / "Release" / "whisper-server.exe"
BIN_SENSEVOICE = BASE_DIR / "sensevoice_server.py"
BIN_LLAMA = BASE_DIR / "bin" / "llama" / "llama-server.exe"
MODEL_WHISPER = BASE_DIR / "models" / "ggml-large-v3-turbo-q5_0.bin"
MODEL_LLM = BASE_DIR / "models" / "Qwen3-4B-Q4_K_M.gguf"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
APP_LOG = LOG_DIR / "voice_tray.log"
ASR_PID_FILE = LOG_DIR / "asr_server.pid"
OVERLAY_POS_FILE = BASE_DIR / "overlay_pos.json"
APP_CONFIG_FILE = BASE_DIR / "app_config.json"

ASR_HOST = "127.0.0.1"
ASR_PORT = 18081
LLM_HOST = "127.0.0.1"
LLM_PORT = 18082

ASR_URL = f"http://{ASR_HOST}:{ASR_PORT}/inference"
LLM_URL = f"http://{LLM_HOST}:{LLM_PORT}/v1/chat/completions"

SAMPLE_RATE = 16000
MIN_HOLD_SECONDS = 0.25
PREVIEW_INTERVAL = 0.42  # 边说边出字预览间隔（秒）
RELEASE_DEBOUNCE_SEC = 0.12  # Copilot 物理按键去抖延迟（秒，防连发微断触发误松开）
MAX_HOLD_SECONDS = 180.0

SYSTEM_PROMPT = (
    "你是语音输入法校对助手，只做最小限度的轻度校对，严禁改写原意、删减分句或替换用词。"
    "仅允许：1) 删除纯粹犹豫语气词（嗯、啊、呃、那个）；"
    "2) 修正明显同音错别字；3) 补充或修正标点符号。"
    "必须逐字保留用户的原本用词、句式、主谓宾和英文/技术词（如fakeTCP、check、API），"
    "严禁同义词替换（如把“看一下”改成“看看”、把“还有什么”改成“有什么”），"
    "严禁删减“我想问一下”“这个”“然后”等正常表达或连接词，严禁翻译。只输出最终正文。/no_think"
)

# ================= 日志配置 =================
_log_handlers: list[logging.Handler] = [logging.FileHandler(APP_LOG, encoding="utf-8")]
if sys.stdout is not None:
    _log_handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger("VoiceInputAI")

# ================= 界面多语言 (UI i18n: zh / en) =================
UI_LANG = "zh"
_STRINGS: dict[str, dict[str, str]] = {
    # --- tray menu ---
    "m_asr": {"zh": "SenseVoice 核显 (18081): {s}", "en": "SenseVoice iGPU (18081): {s}"},
    "m_llm": {"zh": "Qwen3-4B 核显 (18082): {s}", "en": "Qwen3-4B iGPU (18082): {s}"},
    "st_on": {"zh": "● 在线", "en": "● Online"},
    "st_off": {"zh": "○ 离线", "en": "○ Offline"},
    "m_polish": {"zh": "启用 Qwen3-4B 核显重整润色", "en": "Enable Qwen3-4B polishing (iGPU)"},
    "m_live": {"zh": "边说边在光标处出字（松开重整）", "en": "Type at cursor while speaking (polish on release)"},
    "m_preview": {"zh": "按住说话时悬浮窗实时预览", "en": "Live preview overlay while speaking"},
    "m_drag": {"zh": "调整悬浮窗位置（显示 15 秒可拖动）", "en": "Move overlay (draggable for 15 s)"},
    "m_reset_pos": {"zh": "重置悬浮窗位置（回右下角）", "en": "Reset overlay position (bottom-right)"},
    "m_capslock": {"zh": "同时启用 CapsLock 备用热键", "en": "Also use CapsLock as backup hotkey"},
    "m_autostart": {"zh": "开机自动启动（高权限自启）", "en": "Start at login (elevated)"},
    "m_recover": {"zh": "🔄 一键恢复（重置热键 + 麦克风）", "en": "🔄 Quick recover (reset hotkeys + mic)"},
    "m_start": {"zh": "▶ 启动后端模型服务（当前：{s}）", "en": "▶ Start model backends (now: {s})"},
    "m_stop": {"zh": "⏹ 关闭后端模型服务（当前：{s}）", "en": "⏹ Stop model backends (now: {s})"},
    "m_restart": {"zh": "🔄 重启后端模型服务（当前：{s}）", "en": "🔄 Restart model backends (now: {s})"},
    "b_off": {"zh": "○ 已关闭", "en": "○ Stopped"},
    "b_run": {"zh": "● 运行中", "en": "● Running"},
    "b_part": {"zh": "◐ 部分运行", "en": "◐ Partially running"},
    "m_settings": {"zh": "⚙ 设置...", "en": "⚙ Settings..."},
    "m_open_dir": {"zh": "📂 打开 {d} 目录", "en": "📂 Open {d}"},
    "m_log": {"zh": "📄 查看运行日志", "en": "📄 View log"},
    "m_exit": {"zh": "❌ 退出（关闭全部服务）", "en": "❌ Quit (stop all services)"},
    # --- tray tooltip ---
    "tip_listening": {"zh": "VoiceInputAI - 正在聆听...", "en": "VoiceInputAI - Listening..."},
    "tip_ready": {
        "zh": "VoiceInputAI - 就绪（按住 Copilot 键边说边出字，松开重整）",
        "en": "VoiceInputAI - Ready (hold your hotkey to speak, release to polish)",
    },
    "tip_stopped": {"zh": "VoiceInputAI - 后端服务已关闭", "en": "VoiceInputAI - Backends stopped"},
    "tip_starting": {
        "zh": "VoiceInputAI - 启动中 (ASR:{asr} / 核显LLM:{llm})",
        "en": "VoiceInputAI - Starting (ASR: {asr} / iGPU LLM: {llm})",
    },
    "tip_loading": {"zh": "VoiceInputAI - 正在启动核显模型...", "en": "VoiceInputAI - Loading iGPU models..."},
    "w_on": {"zh": "在线", "en": "online"},
    "w_off": {"zh": "离线", "en": "offline"},
    # --- overlay ---
    "ov_drag_title": {"zh": "✋ 拖动此悬浮窗到理想位置", "en": "✋ Drag this overlay where you like"},
    "ov_drag_body": {
        "zh": "按住左键拖动即可调整，松开自动保存（15 秒后自动隐藏）",
        "en": "Hold the left button to drag; saved on release (hides after 15 s)",
    },
    "ov_saved": {"zh": "✅ 设置已保存并生效", "en": "✅ Settings saved and applied"},
    "ov_saved_partial": {"zh": "⚠ 部分设置已保存", "en": "⚠ Some settings saved"},
    "ov_saved_partial_body": {
        "zh": "开机自启计划任务更新失败（需管理员权限）",
        "en": "Failed to update the auto-start task (administrator rights required)",
    },
    "ov_mic_fail": {"zh": "麦克风打开失败，正在刷新设备", "en": "Failed to open microphone, refreshing devices"},
    "ov_listening": {"zh": "🎤 正在聆听（松开即重整）", "en": "🎤 Listening (release to polish)"},
    "ov_live": {"zh": "🎤 实时识别 ({t}s)", "en": "🎤 Live recognition ({t}s)"},
    "ov_no_mic": {"zh": "⚠ 未收到麦克风声音", "en": "⚠ No microphone audio received"},
    "ov_no_mic_body": {"zh": "已自动刷新音频设备，请再试一次", "en": "Audio devices refreshed, please try again"},
    "ov_finalizing": {"zh": "⚡ 核显识别精校中...", "en": "⚡ Finalizing on iGPU..."},
    "ov_no_speech": {"zh": "未识别到有效语音", "en": "No valid speech detected"},
    "ov_polishing": {"zh": "✨ Qwen3-4B 核显重整中...", "en": "✨ Polishing with Qwen3-4B..."},
    "ov_polishing_live": {"zh": "✨ 核显实时重整中...", "en": "✨ Polishing on iGPU..."},
    "ov_done": {"zh": "✅ 已重整上屏 ({ms} ms)", "en": "✅ Typed ({ms} ms)"},
    "ov_recovered": {"zh": "✅ 已恢复热键与麦克风", "en": "✅ Hotkeys and microphone recovered"},
    "ov_recovered_body": {"zh": "按住 Copilot 键即可正常说话", "en": "Hold your hotkey to speak"},
    "ov_autostart_on": {"zh": "✅ 开机自启已开启", "en": "✅ Auto-start enabled"},
    "ov_autostart_off": {"zh": "✅ 开机自启已关闭", "en": "✅ Auto-start disabled"},
    "ov_autostart_fail": {"zh": "⚠ 开机自启切换失败", "en": "⚠ Failed to toggle auto-start"},
    "ov_autostart_fail_body": {
        "zh": "更新计划任务需要管理员权限",
        "en": "Updating the scheduled task requires administrator rights",
    },
    # --- settings window ---
    "s_title": {"zh": "VoiceInputAI 设置", "en": "VoiceInputAI Settings"},
    "s_header": {"zh": "⚙ VoiceInputAI 偏好设置", "en": "⚙ VoiceInputAI Preferences"},
    "s_autostart": {
        "zh": "开机自启（以高权限计划任务自启，防 Copilot 弹搜索）",
        "en": "Start at login (elevated scheduled task, prevents Copilot search popup)",
    },
    "s_capslock": {"zh": "同时启用 CapsLock 备用热键", "en": "Also use CapsLock as a backup hotkey"},
    "s_polish": {
        "zh": "启用 Qwen3-4B 核显智能校对（仅含口癖/缺标点时触发）",
        "en": "Enable Qwen3-4B polishing (only for fillers / missing punctuation)",
    },
    "s_live_cursor": {
        "zh": "边说边在光标处实时出字（松开替换为终稿）",
        "en": "Type at the cursor while speaking (replaced by final text on release)",
    },
    "s_preview": {"zh": "按住说话时悬浮窗实时预览", "en": "Live preview overlay while speaking"},
    "s_interval": {"zh": "实时预览刷新间隔 (ms):", "en": "Live preview interval (ms):"},
    "s_paste_thr": {"zh": "长句改用剪贴板粘贴阈值 (字):", "en": "Use clipboard paste above (chars):"},
    "s_hotkeys": {
        "zh": "按住说话热键（可勾选多个同时生效，支持单键或组合键）:",
        "en": "Push-to-talk hotkeys (tick several to enable at once; single keys or combos):",
    },
    "s_delete": {"zh": "删除", "en": "Delete"},
    "s_record": {"zh": "🎙 录制新增热键", "en": "🎙 Record new hotkey"},
    "s_rec_cancelled": {"zh": "已取消录制", "en": "Recording cancelled"},
    "s_rec_added": {"zh": "已添加并勾选热键: {spec}", "en": "Added and enabled hotkey: {spec}"},
    "s_rec_waiting": {"zh": "⌛ 等待按键（再点或 Esc 取消）", "en": "⌛ Press a key (click again or Esc to cancel)"},
    "s_rec_hint": {
        "zh": "请按下想要设定的单键或组合键（Esc 取消录制）...",
        "en": "Press the key or combo you want (Esc to cancel)...",
    },
    "s_checked_all": {"zh": "已勾选全部热键", "en": "All hotkeys enabled"},
    "s_reset_default_done": {"zh": "已恢复默认 Copilot 热键", "en": "Restored the default Copilot hotkey"},
    "s_check_all": {"zh": "全选启用", "en": "Enable all"},
    "s_reset_default": {"zh": "恢复默认", "en": "Restore default"},
    "s_move_overlay": {"zh": "调整悬浮窗位置（可拖动）", "en": "Move overlay (draggable)"},
    "s_reset_overlay": {"zh": "重置到右下角", "en": "Reset to bottom-right"},
    "s_cancel": {"zh": "取消", "en": "Cancel"},
    "s_save": {"zh": "保存并生效", "en": "Save & apply"},
}


def _detect_system_lang() -> str:
    try:
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "zh" if (lang_id & 0x3FF) == 0x04 else "en"
    except Exception:
        return "zh"


def set_ui_language(code: str) -> str:
    """code: 'zh' | 'en' | 'auto'（auto 跟随 Windows 显示语言，非中文则用英文）。"""
    global UI_LANG
    UI_LANG = code if code in ("zh", "en") else _detect_system_lang()
    return UI_LANG


def tr(key: str, **kwargs) -> str:
    entry = _STRINGS.get(key)
    if entry is None:
        return key
    text = entry.get(UI_LANG) or entry["zh"]
    return text.format(**kwargs) if kwargs else text


# ================= Win32 常量 & SendInput（防抢焦点 / 去修饰键 / Unicode 实时出字） =================
VK_BACK = 0x08
VK_LEFT = 0x25
VK_F23 = 0x86
VK_CAPITAL = 0x14
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = 0x56
VK_MASK_UNASSIGNED = 0xE8
VOICE_INPUT_EXTRA_INFO = 0x564F4943  # "VOIC"：标记程序自身注入的按键事件

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
KEY_DOWN_MSGS = (WM_KEYDOWN, WM_SYSKEYDOWN)
KEY_UP_MSGS = (WM_KEYUP, WM_SYSKEYUP)

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
EXTENDED_VKS = {VK_LWIN, VK_RWIN, VK_RCONTROL, VK_RMENU, VK_LEFT}

DEFAULT_HOTKEY_SPEC = "Copilot (VK_F23)"
_MOD_VK_GROUPS: dict[str, tuple[int, ...]] = {
    "Ctrl": (VK_LCONTROL, VK_RCONTROL, VK_CONTROL),
    "Shift": (VK_LSHIFT, VK_RSHIFT, VK_SHIFT),
    "Alt": (VK_LMENU, VK_RMENU, VK_MENU),
    "Win": (VK_LWIN, VK_RWIN),
}
_ALL_MOD_VKS = {vk for group in _MOD_VK_GROUPS.values() for vk in group}
_VK_NAME_MAP: dict[int, str] = {
    VK_F23: "Copilot (VK_F23)",
    VK_CAPITAL: "CapsLock",
    0x20: "Space",
    0x09: "Tab",
    0x0D: "Enter",
    0x1B: "Esc",
    0x13: "Pause",
    0x91: "ScrollLock",
    0x2D: "Insert",
    VK_LMENU: "LAlt",
    VK_RMENU: "RAlt",
    VK_LCONTROL: "LCtrl",
    VK_RCONTROL: "RCtrl",
    VK_LSHIFT: "LShift",
    VK_RSHIFT: "RShift",
    VK_LWIN: "LWin",
    VK_RWIN: "RWin",
    0x5D: "Apps",
    0x2E: "Delete",
    0x24: "Home",
    0x23: "End",
    0x21: "PageUp",
    0x22: "PageDown",
    **{0x70 + i: f"F{i + 1}" for i in range(24) if (0x70 + i) != VK_F23},
    **{0x41 + i: chr(0x41 + i) for i in range(26)},
    **{0x30 + i: str(i) for i in range(10)},
}
_NAME_VK_MAP: dict[str, int] = {name.lower(): vk for vk, name in _VK_NAME_MAP.items()}
_NAME_VK_MAP.update({"copilot": VK_F23, "f23": VK_F23})


def _format_vk_name(vk: int) -> str:
    return _VK_NAME_MAP.get(vk, f"VK_0x{vk:02X}")


def _parse_hotkey_spec(spec: str) -> Optional[tuple[frozenset[str], int]]:
    parts = [p.strip() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        return None
    mods: set[str] = set()
    mod_alias = {"ctrl": "Ctrl", "control": "Ctrl", "shift": "Shift", "alt": "Alt", "win": "Win"}
    for p in parts[:-1]:
        m = mod_alias.get(p.lower())
        if not m:
            return None
        mods.add(m)
    key_str = parts[-1].lower()
    vk = _NAME_VK_MAP.get(key_str)
    if vk is None and key_str.startswith("vk_0x"):
        try:
            vk = int(key_str[3:], 16)
        except ValueError:
            vk = None
    if vk is None:
        return None
    return frozenset(mods), vk

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
SW_SHOWNOACTIVATE = 4

user32 = ctypes.windll.user32
ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("_union", _INPUT_UNION),
    ]


user32.SendInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint32


ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000


def _create_no_window_flags(above_normal: bool = False) -> int:
    base = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return (base | ABOVE_NORMAL_PRIORITY_CLASS) if above_normal else base


def _elevate_self_priority() -> None:
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.SetPriorityClass.restype = ctypes.c_int
        ok = kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS)
        if not ok:
            logger.warning("SetPriorityClass 失败: err=%s", ctypes.GetLastError())
    except Exception:
        pass


def _send_vk(vk: int, key_up: bool = False) -> None:
    flags = KEYEVENTF_KEYUP if key_up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFFFF
    inp = INPUT(
        type=INPUT_KEYBOARD,
        _union=_INPUT_UNION(
            ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=VOICE_INPUT_EXTRA_INFO)
        ),
    )
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _make_vk_input(vk: int, key_up: bool = False) -> INPUT:
    flags = KEYEVENTF_KEYUP if key_up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFFFF
    return INPUT(
        type=INPUT_KEYBOARD,
        _union=_INPUT_UNION(
            ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=VOICE_INPUT_EXTRA_INFO)
        ),
    )


def _send_input_batch(events: list[INPUT]) -> None:
    if not events:
        return
    arr = (INPUT * len(events))(*events)
    user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def _is_key_down(vk: int) -> bool:
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


def _release_modifiers_safely() -> bool:
    """分阶段屏蔽并释放修饰键：先以 0xE8 掩码并释放 Shift/Alt（防孤立 Shift 切输入法与 Ctrl+Shift），再短按 LCtrl 撤防 Windows 11 开始菜单后释放 Win。"""
    shift_alt = [vk for vk in (VK_LSHIFT, VK_RSHIFT, VK_LMENU, VK_RMENU) if _is_key_down(vk)]
    wins = [vk for vk in (VK_LWIN, VK_RWIN) if _is_key_down(vk)]
    ctrls = [vk for vk in (VK_LCONTROL, VK_RCONTROL) if _is_key_down(vk)]
    if not (shift_alt or wins or ctrls):
        return False

    if shift_alt or wins:
        batch1 = [
            _make_vk_input(VK_MASK_UNASSIGNED, key_up=False),
            _make_vk_input(VK_MASK_UNASSIGNED, key_up=True),
        ]
        for vk in shift_alt:
            batch1.append(_make_vk_input(vk, key_up=True))
        _send_input_batch(batch1)
        time.sleep(0.008 if wins else 0.005)

    if wins and not ctrls and any(_is_key_down(vk) for vk in wins):
        _send_input_batch([_make_vk_input(VK_LCONTROL, key_up=False)])
        time.sleep(0.006)
        _send_input_batch([_make_vk_input(VK_LCONTROL, key_up=True)])
        time.sleep(0.008)

    remaining = [
        _make_vk_input(vk, key_up=True)
        for vk in (VK_LWIN, VK_RWIN, VK_LCONTROL, VK_RCONTROL, VK_LSHIFT, VK_RSHIFT, VK_LMENU, VK_RMENU)
        if _is_key_down(vk)
    ]
    if remaining:
        _send_input_batch(remaining)
        time.sleep(0.005)
    return True


def _tap_start_mask() -> None:
    """Win 仍按住时轻点一次无副作用的 0xE8 掩码键（同 AutoHotkey #MenuMaskKey），使松开 Win 时不弹开始菜单/搜索。"""
    _send_input_batch(
        [
            _make_vk_input(VK_MASK_UNASSIGNED, key_up=False),
            _make_vk_input(VK_MASK_UNASSIGNED, key_up=True),
        ]
    )


def _modifiers_down() -> bool:
    return any(
        _is_key_down(vk)
        for vk in (VK_LWIN, VK_RWIN, VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU)
    )


def _wait_modifiers_up(timeout: float = 0.4) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if not _modifiers_down():
            return True
        time.sleep(0.01)
    return not _modifiers_down()


def _make_unicode_events(text: str) -> list[INPUT]:
    raw = text.encode("utf-16-le")
    events: list[INPUT] = []
    for i in range(0, len(raw), 2):
        code_unit = int.from_bytes(raw[i : i + 2], "little")
        events.append(
            INPUT(
                type=INPUT_KEYBOARD,
                _union=_INPUT_UNION(
                    ki=KEYBDINPUT(
                        wVk=0,
                        wScan=code_unit,
                        dwFlags=KEYEVENTF_UNICODE,
                        time=0,
                        dwExtraInfo=VOICE_INPUT_EXTRA_INFO,
                    )
                ),
            )
        )
        events.append(
            INPUT(
                type=INPUT_KEYBOARD,
                _union=_INPUT_UNION(
                    ki=KEYBDINPUT(
                        wVk=0,
                        wScan=code_unit,
                        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        time=0,
                        dwExtraInfo=VOICE_INPUT_EXTRA_INFO,
                    )
                ),
            )
        )
    return events


def _pcm_to_wav_bytes(pcm_int16: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_int16.tobytes())
    return buf.getvalue()


# ================= 后端进程管理器 =================
class BackendServiceManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.asr_proc: Optional[subprocess.Popen] = None
        self.llm_proc: Optional[subprocess.Popen] = None
        self.asr_online = False
        self.llm_online = False
        self.stopping = False

    def _port_alive(self, url: str) -> bool:
        try:
            r = requests.get(url, proxies={"http": None, "https": None}, timeout=1.5)
            return r.status_code == 200
        except Exception:
            return False

    def refresh_status(self) -> tuple[bool, bool]:
        self.asr_online = self._port_alive(f"http://{ASR_HOST}:{ASR_PORT}/")
        self.llm_online = self._port_alive(f"http://{LLM_HOST}:{LLM_PORT}/health")
        return self.asr_online, self.llm_online

    def _kill_existing_binaries(self) -> None:
        if ASR_PID_FILE.exists():
            try:
                old_pid = int(ASR_PID_FILE.read_text(encoding="utf-8").strip())
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(old_pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_create_no_window_flags(),
                    timeout=5,
                )
            except Exception:
                pass
            try:
                ASR_PID_FILE.unlink()
            except Exception:
                pass
        for exe in ("whisper-server.exe", "llama-server.exe"):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", exe],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_create_no_window_flags(),
                    timeout=5,
                )
            except Exception:
                pass

    def _warmup_llm(self) -> None:
        for _ in range(90):
            if self.stopping:
                return
            if self._port_alive(f"http://{LLM_HOST}:{LLM_PORT}/health"):
                break
            time.sleep(1.0)
        else:
            logger.warning("Qwen3-4B 等待就绪超时 (90s)，跳过启动预热")
            return
        try:
            t0 = time.perf_counter()
            for sample in ("嗯那个测试预热", "今天check一下API"):
                requests.post(
                    LLM_URL,
                    json={
                        "model": "qwen3-4b",
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": sample},
                        ],
                        "temperature": 0.0,
                        "max_tokens": 24,
                        "cache_prompt": True,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                    proxies={"http": None, "https": None},
                    timeout=60.0,
                )
            logger.info("Qwen3-4B Vulkan 核显预热+KV缓存就绪 (%.0f ms)", (time.perf_counter() - t0) * 1000)
        except Exception as e:
            logger.warning("Qwen3-4B 预热异常: %s", e)

    def start_services(self) -> None:
        try:
            with self._lock:
                self.stopping = False
                self.refresh_status()
                flags = _create_no_window_flags(above_normal=True)

                if not self.asr_online and (self.asr_proc is None or self.asr_proc.poll() is not None):
                    asr_log = open(LOG_DIR / "sensevoice_server.log", "ab")
                    py_dir = Path(sys.executable).parent
                    py_console = py_dir / "python.exe"
                    py_exe = str(py_console if py_console.exists() else sys.executable)
                    cmd_asr = [py_exe, "-u", str(BIN_SENSEVOICE)]
                    logger.info("启动 SenseVoice DirectML 核显 ASR 服务: %s", " ".join(cmd_asr))
                    asr_env = dict(os.environ)
                    asr_env["VOICEINPUT_MANAGED"] = "1"
                    self.asr_proc = subprocess.Popen(
                        cmd_asr,
                        cwd=str(BASE_DIR),
                        env=asr_env,
                        stdin=subprocess.DEVNULL,
                        stdout=asr_log,
                        stderr=subprocess.STDOUT,
                        creationflags=flags,
                    )
                    ASR_PID_FILE.write_text(str(self.asr_proc.pid), encoding="utf-8")

                if not self.llm_online and (self.llm_proc is None or self.llm_proc.poll() is not None):
                    llm_log = open(LOG_DIR / "llama_server.log", "ab")
                    cmd_llm = [
                        str(BIN_LLAMA),
                        "-m", str(MODEL_LLM),
                        "--host", LLM_HOST,
                        "--port", str(LLM_PORT),
                        "-ngl", "99",
                        "-fa", "off",
                        "-np", "1",
                        "-c", "1024",
                    ]
                    logger.info("启动 Qwen3-4B Vulkan 核显服务: %s", " ".join(cmd_llm))
                    self.llm_proc = subprocess.Popen(
                        cmd_llm,
                        cwd=str(BIN_LLAMA.parent),
                        stdin=subprocess.DEVNULL,
                        stdout=llm_log,
                        stderr=subprocess.STDOUT,
                        creationflags=flags,
                    )
                    threading.Thread(target=self._warmup_llm, daemon=True).start()
        except Exception:
            logger.exception("启动后端服务失败")

    def stop_services(self) -> None:
        with self._lock:
            self.stopping = True
            for proc in (self.asr_proc, self.llm_proc):
                if proc is not None and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=3)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            self.asr_proc = None
            self.llm_proc = None
            self._kill_existing_binaries()
            self.asr_online = False
            self.llm_online = False
            logger.info("已关闭后端 ASR 与 Qwen3-4B 服务")

    def restart_services(self) -> None:
        logger.info("正在重启后端服务...")
        self.stop_services()
        time.sleep(0.8)
        self.start_services()


# ================= 无焦点悬浮状态气泡 =================
class FloatingOverlay:
    """悬浮预览/状态气泡（WS_EX_NOACTIVATE 永不抢焦点，支持鼠标左键拖动并记忆位置）"""

    def __init__(self, set_capture_cb=None) -> None:
        self._cmd_q: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self._settings_cb = None
        self._set_capture_cb = set_capture_cb
        self._thread = threading.Thread(target=self._ui_loop, daemon=True)
        self._thread.start()

    def open_settings(self, get_state_fn, apply_fn) -> None:
        self._settings_cb = (get_state_fn, apply_fn)
        self._cmd_q.put(("open_settings", "", ""))

    def show(self, title: str, body: str = "", bg: str = "#1E293B") -> None:
        self._cmd_q.put(("show", f"{title}||{bg}", body))

    def hide(self, delay_ms: int = 0) -> None:
        self._cmd_q.put(("hide", str(delay_ms), ""))

    def show_drag_preview(self) -> None:
        self.show(
            tr("ov_drag_title"),
            tr("ov_drag_body"),
            "#38BDF8",
        )
        self.hide(15000)

    def reset_position(self) -> None:
        self._cmd_q.put(("reset_pos", "", ""))

    def stop(self) -> None:
        self._cmd_q.put(("quit", "", ""))

    @staticmethod
    def _load_saved_pos() -> Optional[tuple[int, int]]:
        try:
            if OVERLAY_POS_FILE.exists():
                data = json.loads(OVERLAY_POS_FILE.read_text(encoding="utf-8"))
                return int(data["x"]), int(data["y"])
        except Exception:
            pass
        return None

    @staticmethod
    def _save_pos(pos: Optional[tuple[int, int]]) -> None:
        try:
            if pos is None:
                OVERLAY_POS_FILE.unlink(missing_ok=True)
            else:
                OVERLAY_POS_FILE.write_text(
                    json.dumps({"x": int(pos[0]), "y": int(pos[1])}),
                    encoding="utf-8",
                )
        except Exception as e:
            logger.warning("保存悬浮窗位置失败: %s", e)

    def _ui_loop(self) -> None:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.93)
        root.withdraw()

        frame = tk.Frame(
            root,
            bg="#1E293B",
            padx=14,
            pady=10,
            highlightthickness=1,
            highlightbackground="#38BDF8",
            cursor="fleur",
        )
        frame.pack(fill="both", expand=True)

        lbl_title = tk.Label(
            frame,
            text="",
            fg="#38BDF8",
            bg="#1E293B",
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="w",
            cursor="fleur",
        )
        lbl_title.pack(fill="x")

        lbl_body = tk.Label(
            frame,
            text="",
            fg="#F8FAFC",
            bg="#1E293B",
            font=("Microsoft YaHei UI", 11),
            wraplength=420,
            justify="left",
            anchor="w",
            cursor="fleur",
        )
        lbl_body.pack(fill="x", pady=(4, 0))

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if not hwnd:
            hwnd = root.winfo_id()
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)

        hide_after_id = None
        saved_pos: Optional[tuple[int, int]] = self._load_saved_pos()
        drag_state = {"active": False, "dx": 0, "dy": 0}

        def position_window() -> None:
            root.update_idletasks()
            w = max(root.winfo_reqwidth(), 240)
            h = root.winfo_reqheight()
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            if saved_pos is not None:
                x = max(0, min(saved_pos[0], max(0, sw - w)))
                y = max(0, min(saved_pos[1], max(0, sh - h)))
            else:
                x = sw - w - 28
                y = sh - h - 72
            root.geometry(f"{w}x{h}+{x}+{y}")

        def on_drag_start(event) -> None:
            nonlocal hide_after_id
            if hide_after_id is not None:
                root.after_cancel(hide_after_id)
                hide_after_id = None
            drag_state["active"] = True
            drag_state["dx"] = event.x_root - root.winfo_x()
            drag_state["dy"] = event.y_root - root.winfo_y()

        def on_drag_motion(event) -> None:
            nonlocal saved_pos
            if not drag_state["active"]:
                return
            w = max(root.winfo_width(), 240)
            h = max(root.winfo_height(), 40)
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            nx = max(0, min(event.x_root - drag_state["dx"], max(0, sw - w)))
            ny = max(0, min(event.y_root - drag_state["dy"], max(0, sh - h)))
            saved_pos = (nx, ny)
            root.geometry(f"+{nx}+{ny}")

        def on_drag_end(_event) -> None:
            if not drag_state["active"]:
                return
            drag_state["active"] = False
            self._save_pos(saved_pos)

        for widget in (frame, lbl_title, lbl_body):
            widget.bind("<ButtonPress-1>", on_drag_start)
            widget.bind("<B1-Motion>", on_drag_motion)
            widget.bind("<ButtonRelease-1>", on_drag_end)

        settings_win: Optional[tk.Toplevel] = None
        hotkey_capture_apply = None

        def open_settings_dialog() -> None:
            nonlocal settings_win, hotkey_capture_apply
            if not self._settings_cb:
                return
            if settings_win is not None and settings_win.winfo_exists():
                settings_win.deiconify()
                settings_win.lift()
                settings_win.focus_force()
                return

            get_state_fn, apply_fn = self._settings_cb
            state = get_state_fn()

            win = tk.Toplevel(root)
            settings_win = win
            win.withdraw()
            win.title(tr("s_title"))
            win.configure(bg="#0F172A")
            win.resizable(False, False)

            def on_close() -> None:
                nonlocal settings_win, hotkey_capture_apply
                hotkey_capture_apply = None
                if self._set_capture_cb:
                    self._set_capture_cb(None)
                try:
                    win.unbind_all("<MouseWheel>")
                except Exception:
                    pass
                settings_win = None
                win.destroy()

            win.protocol("WM_DELETE_WINDOW", on_close)

            container = tk.Frame(win, bg="#0F172A", padx=20, pady=16)
            container.pack(fill="both", expand=True)

            tk.Label(
                container,
                text=tr("s_header"),
                fg="#38BDF8",
                bg="#0F172A",
                font=("Microsoft YaHei UI", 13, "bold"),
                anchor="w",
            ).pack(fill="x", pady=(0, 12))

            v_autostart = tk.BooleanVar(value=bool(state.get("auto_start", False)))
            v_polish = tk.BooleanVar(value=bool(state.get("enable_llm_polish", True)))
            v_cursor = tk.BooleanVar(value=bool(state.get("enable_live_cursor_typing", True)))
            v_preview = tk.BooleanVar(value=bool(state.get("enable_live_preview", True)))
            v_capslock = tk.BooleanVar(value=bool(state.get("enable_capslock_backup", False)))
            v_interval = tk.StringVar(value=str(state.get("preview_interval_ms", 420)))
            v_paste_thr = tk.StringVar(value=str(state.get("paste_threshold_chars", 300)))
            v_lang = tk.StringVar(value=str(state.get("ui_language", "auto")))

            chk_opts = {
                "bg": "#0F172A",
                "fg": "#F8FAFC",
                "activebackground": "#0F172A",
                "activeforeground": "#F8FAFC",
                "selectcolor": "#1E293B",
                "font": ("Microsoft YaHei UI", 10),
                "anchor": "w",
                "pady": 4,
            }

            lang_row = tk.Frame(container, bg="#0F172A")
            lang_row.pack(fill="x", pady=(0, 6))
            tk.Label(
                lang_row,
                text="界面语言 / Language:",
                fg="#CBD5E1",
                bg="#0F172A",
                font=("Microsoft YaHei UI", 10),
            ).pack(side="left")
            for _val, _txt in (("auto", "Auto / 自动"), ("zh", "中文"), ("en", "English")):
                tk.Radiobutton(
                    lang_row,
                    text=_txt,
                    value=_val,
                    variable=v_lang,
                    bg="#0F172A",
                    fg="#F8FAFC",
                    activebackground="#0F172A",
                    activeforeground="#F8FAFC",
                    selectcolor="#1E293B",
                    font=("Microsoft YaHei UI", 10),
                ).pack(side="left", padx=(10, 0))

            tk.Checkbutton(
                container,
                text=tr("s_autostart"),
                variable=v_autostart,
                **chk_opts,
            ).pack(fill="x")
            chk_capslock = tk.Checkbutton(
                container,
                text=tr("s_capslock"),
                variable=v_capslock,
                **chk_opts,
            )
            chk_capslock.pack(fill="x")
            tk.Checkbutton(
                container,
                text=tr("s_polish"),
                variable=v_polish,
                **chk_opts,
            ).pack(fill="x")
            tk.Checkbutton(
                container,
                text=tr("s_live_cursor"),
                variable=v_cursor,
                **chk_opts,
            ).pack(fill="x")
            tk.Checkbutton(
                container,
                text=tr("s_preview"),
                variable=v_preview,
                **chk_opts,
            ).pack(fill="x")

            num_frame = tk.Frame(container, bg="#0F172A", pady=10)
            num_frame.pack(fill="x")

            tk.Label(
                num_frame,
                text=tr("s_interval"),
                fg="#CBD5E1",
                bg="#0F172A",
                font=("Microsoft YaHei UI", 10),
            ).grid(row=0, column=0, sticky="w", pady=4)
            tk.Spinbox(
                num_frame,
                from_=250,
                to=1000,
                increment=20,
                textvariable=v_interval,
                width=8,
                bg="#1E293B",
                fg="#F8FAFC",
                insertbackground="#F8FAFC",
                font=("Microsoft YaHei UI", 10),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0), pady=4)

            tk.Label(
                num_frame,
                text=tr("s_paste_thr"),
                fg="#CBD5E1",
                bg="#0F172A",
                font=("Microsoft YaHei UI", 10),
            ).grid(row=1, column=0, sticky="w", pady=4)
            tk.Spinbox(
                num_frame,
                from_=20,
                to=1000,
                increment=20,
                textvariable=v_paste_thr,
                width=8,
                bg="#1E293B",
                fg="#F8FAFC",
                insertbackground="#F8FAFC",
                font=("Microsoft YaHei UI", 10),
            ).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)

            hk_frame = tk.Frame(container, bg="#0F172A", pady=4)
            hk_frame.pack(fill="x")
            tk.Label(
                hk_frame,
                text=tr("s_hotkeys"),
                fg="#CBD5E1",
                bg="#0F172A",
                font=("Microsoft YaHei UI", 9, "bold"),
                anchor="w",
            ).pack(fill="x", pady=(0, 4))

            hk_box = tk.Frame(
                hk_frame,
                bg="#1E293B",
                highlightthickness=1,
                highlightbackground="#334155",
            )
            hk_box.pack(fill="x")

            hk_canvas = tk.Canvas(
                hk_box,
                height=112,
                bg="#1E293B",
                highlightthickness=0,
                bd=0,
            )
            hk_scroll = tk.Scrollbar(hk_box, orient="vertical", command=hk_canvas.yview)
            hk_rows_frame = tk.Frame(hk_canvas, bg="#1E293B")
            hk_canvas_win = hk_canvas.create_window((0, 0), window=hk_rows_frame, anchor="nw")
            hk_canvas.configure(yscrollcommand=hk_scroll.set)

            hk_canvas.pack(side="left", fill="both", expand=True)
            hk_scroll.pack(side="right", fill="y")

            def _sync_hk_scroll(_event=None) -> None:
                hk_canvas.configure(scrollregion=hk_canvas.bbox("all"))

            def _sync_hk_width(event) -> None:
                hk_canvas.itemconfigure(hk_canvas_win, width=event.width)

            hk_rows_frame.bind("<Configure>", _sync_hk_scroll)
            hk_canvas.bind("<Configure>", _sync_hk_width)

            def _on_hk_mousewheel(event) -> None:
                hk_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

            hk_canvas.bind("<Enter>", lambda _e: hk_canvas.bind_all("<MouseWheel>", _on_hk_mousewheel))
            hk_canvas.bind("<Leave>", lambda _e: hk_canvas.unbind_all("<MouseWheel>"))

            hk_entries: dict[str, tuple[tk.BooleanVar, tk.Frame]] = {}

            def add_hotkey_row(spec: str, checked: bool = True, scroll_to_end: bool = False) -> None:
                spec = (spec or "").strip()
                if not spec:
                    return
                if spec in hk_entries:
                    var, _ = hk_entries[spec]
                    var.set(bool(checked))
                    return

                var = tk.BooleanVar(value=bool(checked))
                row = tk.Frame(hk_rows_frame, bg="#1E293B", padx=6, pady=2)
                row.pack(fill="x")

                def on_toggle_row() -> None:
                    if spec == "CapsLock":
                        v_capslock.set(bool(var.get()))

                cb = tk.Checkbutton(
                    row,
                    text=spec,
                    variable=var,
                    command=on_toggle_row,
                    bg="#1E293B",
                    fg="#F8FAFC",
                    activebackground="#334155",
                    activeforeground="#FFFFFF",
                    selectcolor="#0F172A",
                    font=("Microsoft YaHei UI", 9),
                    anchor="w",
                    cursor="hand2",
                )
                cb.pack(side="left", fill="x", expand=True)

                def on_del_row() -> None:
                    entry = hk_entries.pop(spec, None)
                    if entry is not None:
                        _, r_frame = entry
                        r_frame.destroy()
                    if spec == "CapsLock":
                        v_capslock.set(False)
                    if not hk_entries:
                        add_hotkey_row(DEFAULT_HOTKEY_SPEC, checked=True)
                    hk_rows_frame.update_idletasks()
                    _sync_hk_scroll()

                tk.Button(
                    row,
                    text=tr("s_delete"),
                    command=on_del_row,
                    bg="#334155",
                    fg="#CBD5E1",
                    activebackground="#DC2626",
                    activeforeground="#FFFFFF",
                    relief="flat",
                    padx=6,
                    pady=1,
                    font=("Microsoft YaHei UI", 8),
                    cursor="hand2",
                ).pack(side="right")

                hk_entries[spec] = (var, row)
                if spec == "CapsLock" and checked:
                    v_capslock.set(True)
                if scroll_to_end:
                    hk_rows_frame.update_idletasks()
                    _sync_hk_scroll()
                    hk_canvas.yview_moveto(1.0)

            for hk in state.get("hotkeys", [DEFAULT_HOTKEY_SPEC]):
                add_hotkey_row(hk, checked=True)

            def on_toggle_capslock_chk() -> None:
                want_caps = bool(v_capslock.get())
                if want_caps:
                    add_hotkey_row("CapsLock", checked=True, scroll_to_end=True)
                elif "CapsLock" in hk_entries:
                    var, _ = hk_entries["CapsLock"]
                    var.set(False)

            chk_capslock.configure(command=on_toggle_capslock_chk)

            hk_btn_row = tk.Frame(hk_frame, bg="#0F172A", pady=4)
            hk_btn_row.pack(fill="x")
            btn_small = {
                "bg": "#1E293B",
                "fg": "#E2E8F0",
                "activebackground": "#334155",
                "activeforeground": "#FFFFFF",
                "relief": "flat",
                "padx": 8,
                "pady": 4,
                "font": ("Microsoft YaHei UI", 9),
                "cursor": "hand2",
            }
            lbl_rec_hint = tk.Label(hk_frame, text="", fg="#38BDF8", bg="#0F172A", font=("Microsoft YaHei UI", 9), anchor="w")
            lbl_rec_hint.pack(fill="x")

            btn_rec = tk.Button(hk_btn_row, text=tr("s_record"), **btn_small)
            recording_state = {"active": False}

            def _apply_captured_hotkey(spec: str) -> None:
                if not win.winfo_exists():
                    return
                recording_state["active"] = False
                btn_rec.configure(text=tr("s_record"), bg="#1E293B")
                if spec == "ESC_CANCEL":
                    lbl_rec_hint.configure(text=tr("s_rec_cancelled"))
                    return
                add_hotkey_row(spec, checked=True, scroll_to_end=True)
                lbl_rec_hint.configure(text=tr("s_rec_added", spec=spec))

            hotkey_capture_apply = _apply_captured_hotkey

            def start_record_hotkey() -> None:
                if not self._set_capture_cb:
                    return
                if recording_state["active"]:
                    recording_state["active"] = False
                    self._set_capture_cb(None)
                    btn_rec.configure(text=tr("s_record"), bg="#1E293B")
                    lbl_rec_hint.configure(text=tr("s_rec_cancelled"))
                    return

                recording_state["active"] = True
                btn_rec.configure(text=tr("s_rec_waiting"), bg="#0369A1")
                lbl_rec_hint.configure(text=tr("s_rec_hint"))
                win.focus_set()

                def _on_captured(spec: str) -> None:
                    self._cmd_q.put(("hotkey_captured", spec, ""))

                self._set_capture_cb(_on_captured)

            btn_rec.configure(command=start_record_hotkey)
            btn_rec.pack(side="left", padx=(0, 6))

            def check_all_hotkeys() -> None:
                for spec_k, (var, _) in hk_entries.items():
                    var.set(True)
                    if spec_k == "CapsLock":
                        v_capslock.set(True)
                lbl_rec_hint.configure(text=tr("s_checked_all"))

            def reset_default_hotkeys() -> None:
                for _, r_frame in list(hk_entries.values()):
                    r_frame.destroy()
                hk_entries.clear()
                add_hotkey_row(DEFAULT_HOTKEY_SPEC, checked=True)
                v_capslock.set(False)
                hk_rows_frame.update_idletasks()
                _sync_hk_scroll()
                lbl_rec_hint.configure(text=tr("s_reset_default_done"))

            tk.Button(hk_btn_row, text=tr("s_check_all"), command=check_all_hotkeys, **btn_small).pack(side="left", padx=(0, 6))
            tk.Button(hk_btn_row, text=tr("s_reset_default"), command=reset_default_hotkeys, **btn_small).pack(side="left")

            pos_frame = tk.Frame(container, bg="#0F172A", pady=6)
            pos_frame.pack(fill="x")
            btn_style = {
                "bg": "#1E293B",
                "fg": "#E2E8F0",
                "activebackground": "#334155",
                "activeforeground": "#FFFFFF",
                "relief": "flat",
                "padx": 10,
                "pady": 5,
                "font": ("Microsoft YaHei UI", 9),
                "cursor": "hand2",
            }
            tk.Button(
                pos_frame,
                text=tr("s_move_overlay"),
                command=self.show_drag_preview,
                **btn_style,
            ).pack(side="left", padx=(0, 8))
            tk.Button(
                pos_frame,
                text=tr("s_reset_overlay"),
                command=self.reset_position,
                **btn_style,
            ).pack(side="left")

            btn_bar = tk.Frame(container, bg="#0F172A", pady=8)
            btn_bar.pack(fill="x", pady=(10, 0))

            def on_save() -> None:
                try:
                    iv = max(250, min(1000, int(v_interval.get())))
                except ValueError:
                    iv = 420
                try:
                    pt = max(20, min(1000, int(v_paste_thr.get())))
                except ValueError:
                    pt = 300
                new_hotkeys = [spec_k for spec_k, (var, _) in hk_entries.items() if var.get()] or [DEFAULT_HOTKEY_SPEC]
                new_cfg = {
                    "auto_start": bool(v_autostart.get()),
                    "enable_llm_polish": bool(v_polish.get()),
                    "enable_live_cursor_typing": bool(v_cursor.get()),
                    "enable_live_preview": bool(v_preview.get()),
                    "enable_capslock_backup": "CapsLock" in new_hotkeys,
                    "hotkeys": new_hotkeys,
                    "preview_interval_ms": iv,
                    "paste_threshold_chars": pt,
                    "ui_language": v_lang.get(),
                }
                on_close()
                threading.Thread(target=apply_fn, args=(new_cfg,), daemon=True).start()

            tk.Button(
                btn_bar,
                text=tr("s_cancel"),
                command=on_close,
                bg="#334155",
                fg="#F8FAFC",
                activebackground="#475569",
                activeforeground="#FFFFFF",
                relief="flat",
                padx=16,
                pady=6,
                font=("Microsoft YaHei UI", 10),
                cursor="hand2",
            ).pack(side="right")
            tk.Button(
                btn_bar,
                text=tr("s_save"),
                command=on_save,
                bg="#0284C7",
                fg="#FFFFFF",
                activebackground="#0369A1",
                activeforeground="#FFFFFF",
                relief="flat",
                padx=18,
                pady=6,
                font=("Microsoft YaHei UI", 10, "bold"),
                cursor="hand2",
            ).pack(side="right", padx=(0, 10))

            win.update_idletasks()
            w = max(win.winfo_reqwidth() + 24, 500)
            h = max(win.winfo_reqheight() + 12, 620)
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            cx = max(0, (sw - w) // 2)
            cy = max(0, (sh - h) // 2)
            win.geometry(f"{w}x{h}+{cx}+{cy}")
            win.deiconify()
            win.lift()
            win.focus_force()

        def poll() -> None:
            nonlocal hide_after_id, saved_pos
            try:
                while True:
                    action, arg1, arg2 = self._cmd_q.get_nowait()
                    if action == "quit":
                        root.destroy()
                        return
                    if action == "open_settings":
                        open_settings_dialog()
                        continue
                    if action == "hotkey_captured":
                        if hotkey_capture_apply is not None:
                            hotkey_capture_apply(arg1)
                        continue
                    if action == "reset_pos":
                        saved_pos = None
                        self._save_pos(None)
                        position_window()
                        continue
                    if action == "show":
                        if hide_after_id is not None:
                            root.after_cancel(hide_after_id)
                            hide_after_id = None
                        title, border_color = arg1.split("||", 1)
                        frame.configure(highlightbackground=border_color)
                        lbl_title.configure(text=title, fg=border_color)
                        if arg2:
                            lbl_body.configure(text=arg2)
                            lbl_body.pack(fill="x", pady=(4, 0))
                        else:
                            lbl_body.pack_forget()
                        if not drag_state["active"]:
                            position_window()
                        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                    elif action == "hide":
                        if drag_state["active"]:
                            continue
                        delay = int(arg1) if arg1 else 0
                        if hide_after_id is not None:
                            root.after_cancel(hide_after_id)
                        if delay <= 0:
                            root.withdraw()
                            hide_after_id = None
                        else:
                            hide_after_id = root.after(delay, root.withdraw)
            except queue.Empty:
                pass
            root.after(25, poll)

        root.after(25, poll)
        root.mainloop()


# ================= 底层键盘拦截器（修复 Windows 11 Copilot 组合键泄漏） =================
import pynput._util.win32 as _pynput_win32
from pynput.keyboard._win32 import Listener as _BaseKeyboardListener


class LowLevelKeyboardListener(_BaseKeyboardListener):
    """
    重写 pynput 的 Windows 键盘钩子派发逻辑：
    原生 pynput 的 win32_event_filter 若返回 False，仅跳过内部 message_loop，
    但在钩子过程返回时依然调用 CallNextHookEx 放行给系统！
    只有调用 self.suppress_event() 抛出 SuppressException，底层的 LowLevelKeyboardProc 才会直接返回 1 阻止事件向系统传递。
    """

    def __init__(self, filter_fn=None, **kwargs):
        self._filter_fn = filter_fn
        super().__init__(**kwargs)

    def _convert(self, code, msg, lpdata):
        if code != _pynput_win32.SystemHook.HC_ACTION:
            return None
        data = ctypes.cast(lpdata, self._LPKBDLLHOOKSTRUCT).contents
        if self._filter_fn is not None:
            # 若自定义过滤器返回 False，立即抑制事件向 Windows 传递（吃掉该按键消息）
            if self._filter_fn(msg, data) is False:
                self.suppress_event()
        return super()._convert(code, msg, lpdata)


# ================= 主应用：录音 + 快捷键 + 托盘 =================
class VoiceInputApp:
    def __init__(self) -> None:
        self.svc = BackendServiceManager()
        self.overlay = FloatingOverlay(self._set_hotkey_capture)

        self.enable_llm_polish = True
        self.enable_live_preview = True
        self.enable_live_cursor_typing = True  # 边说边在光标处实时出字，松开重整替换
        self.enable_capslock_backup = False  # 默认专注 Copilot 键 (F23)，可在托盘开启 CapsLock 备用
        self.hotkeys: list[str] = [DEFAULT_HOTKEY_SPEC]
        self._compiled_hotkeys: list[tuple[frozenset[str], int]] = []
        self._active_req_mods: frozenset[str] = frozenset()
        self._capture_hotkey_cb = None
        self._cap_held_mods: set[int] = set()
        self.preview_interval_ms = int(PREVIEW_INTERVAL * 1000)
        self.paste_threshold_chars = 300
        self.ui_language = "auto"
        self._load_config()
        set_ui_language(self.ui_language)
        self._rebuild_hotkeys()
        self.auto_start = self._is_autostart_enabled()

        self._recording = False
        self._key_held = False
        self._hook_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._cursor_lock = threading.Lock()
        self._active_key_vk: Optional[int] = None
        self._release_timer: Optional[threading.Timer] = None
        self._session_id = 0
        self._live_cursor_text = ""
        self._record_start_ts = 0.0
        self._audio_chunks: list[np.ndarray] = []
        self._audio_lock = threading.Lock()
        self._stream: Optional[sd.InputStream] = None
        self._preview_stop_ev = threading.Event()
        self._processing_lock = threading.Lock()
        self._mod_release_lock = threading.Lock()
        self._last_preview_text = ""
        self._last_tray_state: Optional[tuple[bool, bool, bool, bool]] = None
        self._paste_seq = 0
        self._last_llm_active_ts = time.time()
        self._asr_session = requests.Session()
        self._asr_session.trust_env = False
        self._llm_session = requests.Session()
        self._llm_session.trust_env = False

        self._kb_listener: Optional[pynput_keyboard.Listener] = None
        self.tray_icon: Optional[pystray.Icon] = None
        self._stop_all = threading.Event()

    # ---------- 配置持久化与开机自启 ----------
    def _load_config(self) -> None:
        try:
            if APP_CONFIG_FILE.exists():
                cfg = json.loads(APP_CONFIG_FILE.read_text(encoding="utf-8"))
                self.enable_llm_polish = bool(cfg.get("enable_llm_polish", self.enable_llm_polish))
                self.enable_live_preview = bool(cfg.get("enable_live_preview", self.enable_live_preview))
                self.enable_live_cursor_typing = bool(cfg.get("enable_live_cursor_typing", self.enable_live_cursor_typing))
                self.enable_capslock_backup = bool(cfg.get("enable_capslock_backup", self.enable_capslock_backup))
                lang_cfg = str(cfg.get("ui_language", "auto"))
                if lang_cfg in ("auto", "zh", "en"):
                    self.ui_language = lang_cfg
                raw_hotkeys = cfg.get("hotkeys")
                if isinstance(raw_hotkeys, list):
                    cleaned = [str(x).strip() for x in raw_hotkeys if _parse_hotkey_spec(str(x)) is not None]
                    if cleaned:
                        self.hotkeys = cleaned
                elif self.enable_capslock_backup and "CapsLock" not in self.hotkeys:
                    self.hotkeys.append("CapsLock")
                self.preview_interval_ms = max(250, min(1000, int(cfg.get("preview_interval_ms", self.preview_interval_ms))))
                self.paste_threshold_chars = max(20, min(1000, int(cfg.get("paste_threshold_chars", self.paste_threshold_chars))))
        except Exception as e:
            logger.warning("读取配置失败，使用默认值: %s", e)

    def _save_config(self) -> None:
        try:
            APP_CONFIG_FILE.write_text(
                json.dumps(
                    {
                        "enable_llm_polish": self.enable_llm_polish,
                        "enable_live_preview": self.enable_live_preview,
                        "enable_live_cursor_typing": self.enable_live_cursor_typing,
                        "enable_capslock_backup": self.enable_capslock_backup,
                        "hotkeys": self.hotkeys,
                        "ui_language": self.ui_language,
                        "preview_interval_ms": self.preview_interval_ms,
                        "paste_threshold_chars": self.paste_threshold_chars,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("保存配置失败: %s", e)

    def _rebuild_hotkeys(self) -> None:
        compiled: list[tuple[frozenset[str], int]] = []
        seen: set[tuple[frozenset[str], int]] = set()
        for spec in self.hotkeys:
            parsed = _parse_hotkey_spec(spec)
            if parsed and parsed not in seen:
                compiled.append(parsed)
                seen.add(parsed)
        if not compiled:
            self.hotkeys = [DEFAULT_HOTKEY_SPEC]
            compiled = [(frozenset(), VK_F23)]
        self._compiled_hotkeys = compiled
        self.enable_capslock_backup = any(mods == frozenset() and vk == VK_CAPITAL for mods, vk in compiled)

    def _set_hotkey_capture(self, cb) -> None:
        self._cap_held_mods.clear()
        self._capture_hotkey_cb = cb

    @staticmethod
    def _is_autostart_enabled() -> bool:
        try:
            r = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-ScheduledTask -TaskName 'VoiceInputAI_Tray' -ErrorAction SilentlyContinue).Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' -and $_.Enabled } | Measure-Object | Select-Object -ExpandProperty Count",
                ],
                capture_output=True,
                text=True,
                timeout=6,
                creationflags=_create_no_window_flags(),
            )
            return r.returncode == 0 and int((r.stdout or "0").strip() or "0") > 0
        except Exception:
            return False

    @staticmethod
    def _set_autostart_enabled(enabled: bool) -> bool:
        ps = (
            "$t = Get-ScheduledTask -TaskName 'VoiceInputAI_Tray' -ErrorAction Stop;"
            "$tr = @(New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME);"
            "Set-ScheduledTask -TaskName 'VoiceInputAI_Tray' -Trigger $tr | Out-Null"
            if enabled
            else "$t = Get-ScheduledTask -TaskName 'VoiceInputAI_Tray' -ErrorAction Stop;"
            "$tr = @(New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(10));"
            "$tr[0].Enabled = $false;"
            "Set-ScheduledTask -TaskName 'VoiceInputAI_Tray' -Trigger $tr | Out-Null"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=_create_no_window_flags(),
            )
            if r.returncode != 0:
                logger.warning("设置开机自启失败: %s", r.stderr.strip())
                return False
            return True
        except Exception as e:
            logger.warning("设置开机自启异常: %s", e)
            return False

    def _get_settings_state(self) -> dict:
        return {
            "auto_start": self.auto_start,
            "enable_llm_polish": self.enable_llm_polish,
            "enable_live_cursor_typing": self.enable_live_cursor_typing,
            "enable_live_preview": self.enable_live_preview,
            "enable_capslock_backup": self.enable_capslock_backup,
            "hotkeys": list(self.hotkeys),
            "ui_language": self.ui_language,
            "preview_interval_ms": self.preview_interval_ms,
            "paste_threshold_chars": self.paste_threshold_chars,
        }

    def _apply_settings(self, new_cfg: dict) -> None:
        self.enable_llm_polish = bool(new_cfg["enable_llm_polish"])
        self.enable_live_cursor_typing = bool(new_cfg["enable_live_cursor_typing"])
        self.enable_live_preview = bool(new_cfg["enable_live_preview"])
        self.enable_capslock_backup = bool(new_cfg["enable_capslock_backup"])
        self.hotkeys = list(new_cfg.get("hotkeys") or [DEFAULT_HOTKEY_SPEC])
        if self.enable_capslock_backup and "CapsLock" not in self.hotkeys:
            self.hotkeys.append("CapsLock")
        elif not self.enable_capslock_backup and "CapsLock" in self.hotkeys and len(self.hotkeys) > 1:
            self.hotkeys = [h for h in self.hotkeys if h != "CapsLock"]
        self._rebuild_hotkeys()
        self.preview_interval_ms = int(new_cfg["preview_interval_ms"])
        self.paste_threshold_chars = int(new_cfg["paste_threshold_chars"])
        lang_new = str(new_cfg.get("ui_language", self.ui_language))
        if lang_new in ("auto", "zh", "en"):
            self.ui_language = lang_new
        set_ui_language(self.ui_language)
        self._save_config()

        want_autostart = bool(new_cfg["auto_start"])
        ok_auto = True
        if want_autostart != self._is_autostart_enabled():
            ok_auto = self._set_autostart_enabled(want_autostart)
        self.auto_start = self._is_autostart_enabled()

        self._update_tray_appearance()
        if self.tray_icon:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass
        if ok_auto:
            self.overlay.show(tr("ov_saved"), "", "#10B981")
        else:
            self.overlay.show(tr("ov_saved_partial"), tr("ov_saved_partial_body"), "#F59E0B")
        self.overlay.hide(1800)

    # ---------- 图标绘制 ----------
    @staticmethod
    def _make_icon_image(color: str) -> Image.Image:
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # 圆形底色
        d.ellipse((4, 4, size - 4, size - 4), fill=color)
        # 白色麦克风剪影
        d.rounded_rectangle((24, 14, 40, 36), radius=8, fill="white")
        d.arc((18, 22, 46, 44), start=0, end=180, fill="white", width=4)
        d.line((32, 44, 32, 52), fill="white", width=4)
        d.line((23, 52, 41, 52), fill="white", width=4)
        return img

    def _update_tray_appearance(self) -> None:
        if not self.tray_icon:
            return
        asr_ok, llm_ok = self.svc.asr_online, self.svc.llm_online
        if self._recording:
            color = "#EF4444"  # 红色：正在录音
            tip = tr("tip_listening")
        elif asr_ok and (llm_ok or not self.enable_llm_polish):
            color = "#10B981"  # 绿色：就绪
            tip = tr("tip_ready")
        elif self.svc.stopping:
            color = "#64748B"  # 灰色：已停止
            tip = tr("tip_stopped")
        else:
            color = "#F59E0B"  # 黄色：加载/部分离线
            tip = tr("tip_starting", asr=tr("w_on" if asr_ok else "w_off"), llm=tr("w_on" if llm_ok else "w_off"))

        self.tray_icon.icon = self._make_icon_image(color)
        self.tray_icon.title = tip

    # ---------- 录音与识别管线 ----------
    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if self._recording:
            with self._audio_lock:
                self._audio_chunks.append(indata.copy())

    def _get_recorded_pcm(self) -> np.ndarray:
        with self._audio_lock:
            if not self._audio_chunks:
                return np.zeros((0,), dtype=np.int16)
            return np.concatenate(self._audio_chunks, axis=0).reshape(-1)

    def _begin_hold_session(self, vk: int) -> None:
        # 不再主动抬起 Copilot 附带的 LShift/LWin（主动抬起会让系统误判为单击 Win 而弹出开始菜单/搜索），
        # 只在 Win 按住期间轻点掩码键，物理松开时由系统自然处理
        if vk == VK_F23 and (_is_key_down(VK_LWIN) or _is_key_down(VK_RWIN)):
            _tap_start_mask()
        self._start_recording(vk)

    def _release_held_modifiers(self) -> bool:
        """输出文字前确保修饰键已松开：按住说话期间不动修饰键；松开后等待物理抬起，超时才兜底安全释放。"""
        if self._key_held:
            return False
        with self._mod_release_lock:
            if _wait_modifiers_up(0.4):
                return False
            return _release_modifiers_safely()

    def _start_recording(self, vk: int) -> None:
        with self._state_lock:
            if self._recording or not self._key_held:
                return
            self._session_id += 1
            session_id = self._session_id
            self._recording = True
            self._active_key_vk = vk
            self._record_start_ts = time.time()
            self._last_preview_text = ""
            with self._cursor_lock:
                self._live_cursor_text = ""
            with self._audio_lock:
                self._audio_chunks.clear()

            try:
                self._stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    callback=self._audio_callback,
                )
                self._stream.start()
            except Exception as e:
                logger.error("打开麦克风失败: %s", e)
                self._recording = False
                self.overlay.show(tr("ov_mic_fail"), str(e), "#EF4444")
                self.overlay.hide(2500)
                threading.Thread(target=self._reinit_audio_backend, daemon=True).start()
                return

        self._update_tray_appearance()
        self.overlay.show(tr("ov_listening"), "", "#EF4444")

        if (
            self.enable_llm_polish
            and self.svc.llm_online
            and (time.time() - self._last_llm_active_ts > 8.0)
            and not self._processing_lock.locked()
        ):
            threading.Thread(target=self._wake_llm_if_idle, daemon=True).start()

        self._preview_stop_ev.clear()
        if self.enable_live_preview or self.enable_live_cursor_typing:
            threading.Thread(target=self._live_preview_worker, args=(session_id,), daemon=True).start()

    def _wake_llm_if_idle(self) -> None:
        """按下 Copilot 瞬间异步发送 1-token 轻唤醒，提前拉起核显频率与 KV 缓存，避免松开时冷等待。"""
        try:
            requests.post(
                LLM_URL,
                json={
                    "model": "qwen3-4b",
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": "."},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 1,
                    "cache_prompt": True,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                proxies={"http": None, "https": None},
                timeout=3.0,
            )
            self._last_llm_active_ts = time.time()
        except Exception:
            pass

    def _live_preview_worker(self, session_id: int) -> None:
        unchanged_count = 0
        while not self._preview_stop_ev.wait(self.preview_interval_ms / 1000.0):
            try:
                if not self._recording or session_id != self._session_id:
                    break
                elapsed = time.time() - self._record_start_ts
                if elapsed > MAX_HOLD_SECONDS:
                    logger.warning("录音达到 %.0f 秒上限，自动停止并重整", MAX_HOLD_SECONDS)
                    with self._hook_lock:
                        self._key_held = False
                        if self._release_timer is not None:
                            self._release_timer.cancel()
                            self._release_timer = None
                    threading.Thread(target=self._stop_recording, daemon=True).start()
                    break
                pcm = self._get_recorded_pcm()
                if len(pcm) < int(SAMPLE_RATE * 0.4):
                    continue
                raw = self._call_asr(pcm, timeout=2.5, is_preview=True)
                # 边说阶段剥离句首孤立语气词与句尾标点，避免停顿/删首字导致整句退格归零
                preview = re.sub(r"^(?:嗯|啊|呃|哦|唉)[，,、\s]+", "", raw).rstrip("。，、？！,.?!")
                if not preview or re.fullmatch(r"[\s，、？！,.?!]+", preview):
                    unchanged_count += 1
                    if unchanged_count in (3, 8):
                        logger.info("[预览 %.1fs] asr为空/仅标点 raw=%r", elapsed, raw)
                    continue

                with self._cursor_lock:
                    if not self._recording or session_id != self._session_id:
                        break
                    if preview != self._last_preview_text:
                        unchanged_count = 0
                        self._last_preview_text = preview
                        if self.enable_live_cursor_typing:
                            self._sync_cursor_text_locked(preview, is_live=True)
                        logger.info("[预览 %.1fs] asr=%r | cursor=%r", elapsed, preview, self._live_cursor_text)
                        if self.enable_live_preview:
                            self.overlay.show(tr("ov_live", t=f"{elapsed:.1f}"), preview, "#38BDF8")
                    else:
                        unchanged_count += 1
                        if unchanged_count in (4, 10):
                            logger.info("[预览 %.1fs] 识别结果未变化 len=%d", elapsed, len(preview))
            except Exception:
                logger.exception("实时预览线程异常")

    @staticmethod
    def _reinit_audio_backend() -> None:
        try:
            sd._terminate()
            sd._initialize()
            logger.info("PortAudio 音频后端已重新初始化")
        except Exception as e:
            logger.warning("PortAudio 重新初始化失败: %s", e)

    def _stop_recording(self) -> None:
        with self._state_lock:
            if not self._recording:
                return
            self._recording = False
            session_id = self._session_id
            self._preview_stop_ev.set()
            duration = time.time() - self._record_start_ts

            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

        self._update_tray_appearance()
        pcm = self._get_recorded_pcm()

        if duration < MIN_HOLD_SECONDS or len(pcm) < int(SAMPLE_RATE * 0.2):
            with self._cursor_lock:
                if self._live_cursor_text and session_id == self._session_id:
                    self._sync_cursor_text_locked("", is_live=False)
            if duration >= MIN_HOLD_SECONDS and len(pcm) < int(SAMPLE_RATE * 0.2):
                logger.warning("录音 %.1fs 但未收到麦克风采样 (samples=%d)，自动刷新音频设备", duration, len(pcm))
                self.overlay.show(tr("ov_no_mic"), tr("ov_no_mic_body"), "#EF4444")
                self.overlay.hide(2000)
                threading.Thread(target=self._reinit_audio_backend, daemon=True).start()
            else:
                self.overlay.hide(0)
            return

        threading.Thread(
            target=self._process_and_paste,
            args=(pcm, duration, session_id),
            daemon=True,
        ).start()

    def _call_asr(self, pcm: np.ndarray, timeout: float = 15.0, is_preview: bool = False) -> str:
        wav_bytes = _pcm_to_wav_bytes(pcm)
        try:
            resp = self._asr_session.post(
                ASR_URL,
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data={"lid": "zh", "preview": "1" if is_preview else "0"},
                timeout=timeout,
            )
            if resp.status_code == 200:
                return resp.json().get("text", "").strip()
        except Exception as e:
            logger.warning("ASR 请求异常 (preview=%s): %s", is_preview, e)
        return ""

    def _call_llm_polish_stream(self, raw_text: str, on_partial=None) -> str:
        if not raw_text:
            return ""
        max_tok = max(128, min(384, len(raw_text) * 2 + 32))
        payload = {
            "model": "qwen3-4b",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": raw_text},
            ],
            "temperature": 0.0,
            "max_tokens": max_tok,
            "cache_prompt": True,
            "stream": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        buf: list[str] = []
        last_emit_ts = 0.0
        last_emitted = ""
        try:
            with self._llm_session.post(
                LLM_URL,
                json=payload,
                stream=True,
                timeout=20.0,
            ) as resp:
                if resp.status_code != 200:
                    return raw_text
                for raw_line in resp.iter_lines():
                    if not raw_line or not raw_line.startswith(b"data: "):
                        continue
                    data_bytes = raw_line[6:]
                    if data_bytes == b"[DONE]":
                        break
                    try:
                        chunk = json.loads(data_bytes)
                        delta = chunk["choices"][0].get("delta", {}).get("content", "")
                    except Exception:
                        continue
                    if not delta:
                        continue
                    buf.append(delta)
                    if on_partial is not None:
                        joined = "".join(buf)
                        if "<think>" in joined and "</think>" not in joined:
                            continue
                        clean = re.sub(r"<think>.*?</think>", "", joined, flags=re.DOTALL).lstrip()
                        now = time.perf_counter()
                        if len(clean) >= 2 and clean != last_emitted and (now - last_emit_ts >= 0.075):
                            last_emit_ts = now
                            last_emitted = clean
                            on_partial(clean)
            self._last_llm_active_ts = time.time()
            full_clean = re.sub(r"<think>.*?</think>", "", "".join(buf), flags=re.DOTALL).strip()
            return full_clean or raw_text
        except Exception as e:
            logger.warning("Qwen3-4B 流式润色异常，降级为直接输出: %s", e)
        return raw_text

    @staticmethod
    def _common_prefix_len(a: str, b: str) -> int:
        limit = min(len(a), len(b))
        i = 0
        while i < limit and a[i] == b[i]:
            i += 1
        return i

    def _send_unicode_diff(self, backspaces: int, insert_text: str) -> None:
        if backspaces <= 0 and not insert_text:
            return
        self._release_held_modifiers()

        if backspaces > 0:
            bs_events: list[INPUT] = []
            for _ in range(backspaces):
                bs_events.append(_make_vk_input(VK_BACK, key_up=False))
                bs_events.append(_make_vk_input(VK_BACK, key_up=True))
            for start in range(0, len(bs_events), 200):
                _send_input_batch(bs_events[start : start + 200])
            time.sleep(0.015)

        if insert_text:
            chunk_size = 100
            for start in range(0, len(insert_text), chunk_size):
                chunk = insert_text[start : start + chunk_size]
                _send_input_batch(_make_unicode_events(chunk))

    def _select_left_chars(self, count: int) -> None:
        """用 Shift+Left 批量选中左侧旧字，避免大量 VK_BACK 在 Electron/Chromium 中逐字触发 DOM 重排动画。"""
        if count <= 0:
            return
        self._release_held_modifiers()
        _send_input_batch([_make_vk_input(VK_LSHIFT, key_up=False)])
        left_events: list[INPUT] = []
        for _ in range(count):
            left_events.append(_make_vk_input(VK_LEFT, key_up=False))
            left_events.append(_make_vk_input(VK_LEFT, key_up=True))
        for start in range(0, len(left_events), 40):
            _send_input_batch(left_events[start : start + 40])
            if start + 40 < len(left_events):
                time.sleep(0.003)
        _send_input_batch([_make_vk_input(VK_LSHIFT, key_up=True)])
        time.sleep(0.015)

    def _sync_cursor_text_locked(self, target_text: str, is_live: bool = True) -> None:
        old_text = self._live_cursor_text
        if old_text == target_text:
            return
        lcp = self._common_prefix_len(old_text, target_text)
        backspaces = len(old_text) - lcp
        insert_text = target_text[lcp:]

        # 物理修饰键（如 Copilot 键的 LWin+LShift）仍按住时，严禁向焦点窗口发送 VK_BACK，
        # 否则 Electron/Chromium 会把 Win+Shift+Backspace 当成快捷键并残留旧字导致文本错乱。
        # 若有新增长内容，改为单调追加尾部新字（避免停顿改标点/微调后整句卡死不再输出），
        # 并精确同步 _live_cursor_text，待松手后再一次性退格替换成最终校对稿。
        if is_live and backspaces > 0 and _modifiers_down():
            if len(target_text) > len(old_text):
                delta = target_text[len(old_text):]
                self._send_unicode_diff(0, delta)
                self._live_cursor_text = old_text + delta
            return

        if not is_live and backspaces > 8:
            self._select_left_chars(backspaces)
            if insert_text:
                if len(insert_text) > self.paste_threshold_chars:
                    self._paste_to_cursor(insert_text, wait_modifiers=False)
                else:
                    self._send_unicode_diff(0, insert_text)
            else:
                self._send_unicode_diff(1, "")
        elif not is_live and len(insert_text) > self.paste_threshold_chars:
            if backspaces > 0:
                self._send_unicode_diff(backspaces, "")
                time.sleep(0.015)
            self._paste_to_cursor(insert_text, wait_modifiers=False)
        else:
            self._send_unicode_diff(backspaces, insert_text)

        self._live_cursor_text = target_text

    def _paste_to_cursor(self, text: str, wait_modifiers: bool = True) -> None:
        if not text:
            return
        if wait_modifiers:
            time.sleep(0.03)
        self._release_held_modifiers()

        copied = False
        for _ in range(4):
            try:
                pyperclip.copy(text)
                time.sleep(0.015)
                if pyperclip.paste() == text:
                    copied = True
                    break
            except Exception:
                time.sleep(0.015)

        if not copied:
            logger.warning("剪贴板写入未就绪，回退为 Unicode 逐字输入")
            self._send_unicode_diff(0, text)
            return

        _send_input_batch(
            [
                _make_vk_input(VK_CONTROL, key_up=False),
                _make_vk_input(VK_V, key_up=False),
                _make_vk_input(VK_V, key_up=True),
                _make_vk_input(VK_CONTROL, key_up=True),
            ]
        )

    def _process_and_paste(self, pcm: np.ndarray, duration: float, session_id: int) -> None:
        with self._processing_lock:
            t0 = time.perf_counter()
            self.overlay.show(tr("ov_finalizing"), self._last_preview_text, "#F59E0B")
            raw_text = self._call_asr(pcm, timeout=15.0, is_preview=False)
            if not raw_text:
                raw_text = self._last_preview_text
            # 只有标点/空白（如误触产生的单个“。”）视为无效语音，不上屏
            if raw_text and not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", raw_text):
                raw_text = ""
            if not raw_text:
                with self._cursor_lock:
                    if self._live_cursor_text and session_id == self._session_id:
                        self._sync_cursor_text_locked("", is_live=False)
                self.overlay.show(tr("ov_no_speech"), "", "#64748B")
                self.overlay.hide(1000)
                return

            raw_text = re.sub(r"^(?:嗯|啊|呃|哦|唉)[，,、\s]+", "", raw_text)
            final_text = raw_text
            stream_wrote = False
            needs_polish = bool(
                re.search(r"(?:^|[，。、\s])(?:嗯|啊|呃|哦|唉)(?:[，。、\s]|$)", raw_text)
                or not raw_text.endswith(("。", "？", "！", ".", "?", "!"))
            )
            if self.enable_llm_polish and needs_polish:
                self.overlay.show(tr("ov_polishing"), raw_text, "#A855F7")

                def on_partial(partial_text: str) -> None:
                    self.overlay.show(tr("ov_polishing_live"), partial_text, "#A855F7")

                final_text = self._call_llm_polish_stream(raw_text, on_partial=on_partial)

            cost_ms = (time.perf_counter() - t0) * 1000
            logger.info("[%.1fs 音频 | %.0fms] 原始=%r -> 最终=%r", duration, cost_ms, raw_text, final_text)

            with self._cursor_lock:
                if session_id == self._session_id:
                    if self._live_cursor_text or stream_wrote:
                        self._sync_cursor_text_locked(final_text, is_live=False)
                    elif len(final_text) <= self.paste_threshold_chars:
                        self._send_unicode_diff(0, final_text)
                    else:
                        self._paste_to_cursor(final_text)
                    self._live_cursor_text = ""
                elif not stream_wrote:
                    # 用户紧接着按下了下一段发言且流式尚未写入，直接粘贴以免错位退格
                    self._paste_to_cursor(final_text)

            self.overlay.show(tr("ov_done", ms=f"{cost_ms:.0f}"), final_text, "#10B981")
            self.overlay.hide(1500)

    def _confirm_key_release(self, vk: int, expected_timer: threading.Timer) -> None:
        with self._hook_lock:
            if self._release_timer is not expected_timer or not self._key_held or self._active_key_vk != vk:
                return
            self._release_timer = None
            self._key_held = False
        logger.info("热键松开确认: vk=0x%X", vk)
        self._stop_recording()

    # ---------- 底层键盘拦截（支持自定义单键 / 组合键 / 多热键） ----------
    def _start_keyboard_listener(self) -> None:
        def _mods_satisfied(req_mods: frozenset[str]) -> bool:
            return all(any(_is_key_down(mvk) for mvk in _MOD_VK_GROUPS[m]) for m in req_mods)

        def win32_event_filter(msg, data):
            if msg not in (WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP):
                return True
            if getattr(data, "dwExtraInfo", None) == VOICE_INPUT_EXTRA_INFO:
                return True

            vk = data.vkCode

            # 处于设置窗口“录制热键”模式时，捕获原始键码（支持 Copilot / 单修饰键 / 组合键）
            cap_cb = self._capture_hotkey_cb
            if cap_cb is not None:
                if msg in KEY_DOWN_MSGS:
                    if vk == 0x1B:  # Esc 取消录制
                        self._capture_hotkey_cb = None
                        self._cap_held_mods.clear()
                        cap_cb("ESC_CANCEL")
                        return False
                    if vk in _ALL_MOD_VKS:
                        self._cap_held_mods.add(vk)
                        return True
                    if vk == VK_F23:
                        spec = DEFAULT_HOTKEY_SPEC
                    else:
                        mods = [
                            m
                            for m in ("Ctrl", "Shift", "Alt", "Win")
                            if any(_is_key_down(mvk) or mvk in self._cap_held_mods for mvk in _MOD_VK_GROUPS[m])
                        ]
                        spec = "+".join([*mods, _format_vk_name(vk)])
                    self._capture_hotkey_cb = None
                    self._cap_held_mods.clear()
                    cap_cb(spec)
                    return False
                elif msg in KEY_UP_MSGS and vk in _ALL_MOD_VKS and vk in self._cap_held_mods:
                    # 允许单独按任意修饰键（如 RAlt / RCtrl / RShift / LAlt / LWin）或修饰键组合并松开
                    other_mods = [
                        m
                        for m in ("Ctrl", "Shift", "Alt", "Win")
                        if not any(mvk == vk for mvk in _MOD_VK_GROUPS[m])
                        and any(_is_key_down(mvk) or mvk in self._cap_held_mods for mvk in _MOD_VK_GROUPS[m])
                    ]
                    spec = "+".join([*other_mods, _format_vk_name(vk)])
                    self._capture_hotkey_cb = None
                    self._cap_held_mods.clear()
                    cap_cb(spec)
                    return False
                return True

            if msg in KEY_DOWN_MSGS:
                matched = None
                for req_mods, trig_vk in self._compiled_hotkeys:
                    if trig_vk == vk and _mods_satisfied(req_mods):
                        matched = (req_mods, trig_vk)
                        break
                if matched is None:
                    return True
                req_mods, _ = matched
                with self._hook_lock:
                    if self._release_timer is not None:
                        self._release_timer.cancel()
                        self._release_timer = None
                    if not self._key_held:
                        self._key_held = True
                        self._active_key_vk = vk
                        self._active_req_mods = req_mods
                        mods_down = [hex(m) for m in (VK_LWIN, VK_LSHIFT, VK_LCONTROL) if _is_key_down(m)]
                        logger.info("热键按下: vk=0x%X req_mods=%s mods=%s", vk, sorted(req_mods), mods_down)
                        threading.Thread(target=self._begin_hold_session, args=(vk,), daemon=True).start()
                return False

            if msg in KEY_UP_MSGS:
                with self._hook_lock:
                    if self._key_held:
                        is_trigger_up = self._active_key_vk in (None, vk)
                        is_mod_up = any(vk in _MOD_VK_GROUPS[m] for m in self._active_req_mods)
                        if is_trigger_up or is_mod_up:
                            if self._release_timer is not None:
                                self._release_timer.cancel()
                            act_vk = self._active_key_vk if self._active_key_vk is not None else vk
                            holder: list[Optional[threading.Timer]] = [None]
                            timer = threading.Timer(
                                RELEASE_DEBOUNCE_SEC,
                                lambda h=holder, k=act_vk: self._confirm_key_release(k, h[0]),
                            )
                            holder[0] = timer
                            timer.daemon = True
                            self._release_timer = timer
                            timer.start()
                            return False if is_trigger_up else True
                return True

            return True

        self._kb_listener = LowLevelKeyboardListener(filter_fn=win32_event_filter)
        self._kb_listener.start()
        logger.info("键盘热键监听已启动，当前热键: %s", ", ".join(self.hotkeys))

    def _recover_input_and_audio(self) -> None:
        """一键恢复：清理卡死状态、重挂底层键盘钩子、刷新 PortAudio 麦克风，并在需要时拉起离线后端。"""
        logger.info("用户触发一键恢复（重置热键监听 + 麦克风）")
        with self._hook_lock:
            if self._release_timer is not None:
                self._release_timer.cancel()
                self._release_timer = None
            self._key_held = False
            self._active_key_vk = None

        with self._state_lock:
            self._recording = False
            self._preview_stop_ev.set()
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

        with self._cursor_lock:
            self._live_cursor_text = ""
            self._last_preview_text = ""

        if self._kb_listener is not None:
            try:
                self._kb_listener.stop()
            except Exception:
                pass
            self._kb_listener = None
        self._start_keyboard_listener()
        self._reinit_audio_backend()

        if not (self.svc.asr_online and (self.svc.llm_online or not self.enable_llm_polish)):
            threading.Thread(target=self.svc.start_services, daemon=True).start()

        self._update_tray_appearance()
        self.overlay.show(tr("ov_recovered"), tr("ov_recovered_body"), "#10B981")
        self.overlay.hide(1800)

    # ---------- 托盘菜单与状态巡检 ----------
    def _build_menu(self) -> pystray.Menu:
        def status_asr(_):
            return tr("m_asr", s=tr("st_on" if self.svc.asr_online else "st_off"))

        def status_llm(_):
            return tr("m_llm", s=tr("st_on" if self.svc.llm_online else "st_off"))

        def toggle_polish(icon, item):
            self.enable_llm_polish = not self.enable_llm_polish
            self._save_config()
            self._update_tray_appearance()

        def toggle_preview(icon, item):
            self.enable_live_preview = not self.enable_live_preview
            self._save_config()

        def toggle_live_cursor(icon, item):
            self.enable_live_cursor_typing = not self.enable_live_cursor_typing
            self._save_config()

        def toggle_capslock(icon, item):
            self.enable_capslock_backup = not self.enable_capslock_backup
            if self.enable_capslock_backup and "CapsLock" not in self.hotkeys:
                self.hotkeys.append("CapsLock")
            elif not self.enable_capslock_backup:
                self.hotkeys = [h for h in self.hotkeys if h != "CapsLock"]
            self._rebuild_hotkeys()
            self._save_config()

        def toggle_autostart(icon, item):
            def _do():
                want = not self.auto_start
                ok = self._set_autostart_enabled(want)
                self.auto_start = self._is_autostart_enabled()
                if self.tray_icon:
                    try:
                        self.tray_icon.update_menu()
                    except Exception:
                        pass
                if ok:
                    self.overlay.show(tr("ov_autostart_on" if self.auto_start else "ov_autostart_off"), "", "#10B981")
                else:
                    self.overlay.show(tr("ov_autostart_fail"), tr("ov_autostart_fail_body"), "#F59E0B")
                self.overlay.hide(1800)

            threading.Thread(target=_do, daemon=True).start()

        def backend_state_tag() -> str:
            if self.svc.stopping or (not self.svc.asr_online and not self.svc.llm_online):
                return tr("b_off")
            if self.svc.asr_online and self.svc.llm_online:
                return tr("b_run")
            return tr("b_part")

        def label_toggle_services(_):
            if self.svc.stopping or (not self.svc.asr_online and not self.svc.llm_online):
                return tr("m_start", s=backend_state_tag())
            return tr("m_stop", s=backend_state_tag())

        def label_restart_services(_):
            return tr("m_restart", s=backend_state_tag())

        def on_toggle_services(icon, item):
            def _do():
                if self.svc.stopping or (not self.svc.asr_online and not self.svc.llm_online):
                    self.svc.start_services()
                else:
                    self.svc.stop_services()
                self.svc.refresh_status()
                self._update_tray_appearance()
                if self.tray_icon:
                    try:
                        self.tray_icon.update_menu()
                    except Exception:
                        pass

            threading.Thread(target=_do, daemon=True).start()

        def on_restart_services(icon, item):
            def _do():
                self.svc.restart_services()
                self.svc.refresh_status()
                self._update_tray_appearance()
                if self.tray_icon:
                    try:
                        self.tray_icon.update_menu()
                    except Exception:
                        pass

            threading.Thread(target=_do, daemon=True).start()

        def on_open_settings(icon, item):
            self.overlay.open_settings(self._get_settings_state, self._apply_settings)

        def on_recover(icon, item):
            threading.Thread(target=self._recover_input_and_audio, daemon=True).start()

        def on_open_dir(icon, item):
            os.startfile(str(BASE_DIR))

        def on_open_log(icon, item):
            os.startfile(str(APP_LOG))

        def on_exit(icon, item):
            logger.info("用户通过托盘退出 VoiceInputAI...")
            self._stop_all.set()
            if self._kb_listener:
                self._kb_listener.stop()
            self.svc.stop_services()
            self.overlay.stop()
            icon.stop()

        return pystray.Menu(
            pystray.MenuItem(status_asr, None, enabled=False),
            pystray.MenuItem(status_llm, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _: tr("m_polish"),
                toggle_polish,
                checked=lambda _: self.enable_llm_polish,
            ),
            pystray.MenuItem(
                lambda _: tr("m_live"),
                toggle_live_cursor,
                checked=lambda _: self.enable_live_cursor_typing,
            ),
            pystray.MenuItem(
                lambda _: tr("m_preview"),
                toggle_preview,
                checked=lambda _: self.enable_live_preview,
            ),
            pystray.MenuItem(
                lambda _: tr("m_drag"),
                lambda icon, item: self.overlay.show_drag_preview(),
            ),
            pystray.MenuItem(
                lambda _: tr("m_reset_pos"),
                lambda icon, item: self.overlay.reset_position(),
            ),
            pystray.MenuItem(
                lambda _: tr("m_capslock"),
                toggle_capslock,
                checked=lambda _: self.enable_capslock_backup,
            ),
            pystray.MenuItem(
                lambda _: tr("m_autostart"),
                toggle_autostart,
                checked=lambda _: self.auto_start,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: tr("m_recover"), on_recover),
            pystray.MenuItem(label_toggle_services, on_toggle_services),
            pystray.MenuItem(label_restart_services, on_restart_services),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: tr("m_settings"), on_open_settings),
            pystray.MenuItem(lambda _: tr("m_open_dir", d=BASE_DIR), on_open_dir),
            pystray.MenuItem(lambda _: tr("m_log"), on_open_log),
            pystray.MenuItem(lambda _: tr("m_exit"), on_exit),
        )

    def _health_monitor_loop(self) -> None:
        offline_streak = 0
        while not self._stop_all.wait(3.0):
            asr_ok, llm_ok = self.svc.refresh_status()
            # 后端被意外杀掉时自动拉起（连续 3 次=约 9 秒离线才重启，避免启动阶段误判）
            if (not asr_ok or not llm_ok) and not self.svc.stopping:
                offline_streak += 1
                if offline_streak >= 3:
                    offline_streak = -20  # 重启后给约 60 秒加载时间
                    logger.warning("检测到后端离线 (ASR=%s, LLM=%s)，自动重新拉起", asr_ok, llm_ok)
                    threading.Thread(target=self.svc.start_services, daemon=True).start()
            elif asr_ok and llm_ok:
                offline_streak = 0
            state = (asr_ok, llm_ok, self._recording, self.svc.stopping)
            if state != self._last_tray_state:
                self._last_tray_state = state
                self._update_tray_appearance()
                if self.tray_icon:
                    self.tray_icon.update_menu()

    def run(self) -> None:
        logger.info("VoiceInputApp.run() 开始执行 PID=%d", os.getpid())
        # 启动后台 ASR 与 Qwen3-4B 核显服务
        threading.Thread(target=self.svc.start_services, daemon=True).start()
        # 启动 Copilot 键监听
        self._start_keyboard_listener()

        self.tray_icon = pystray.Icon(
            "VoiceInputAI",
            self._make_icon_image("#F59E0B"),
            tr("tip_loading"),
            menu=self._build_menu(),
        )
        threading.Thread(target=self._health_monitor_loop, daemon=True).start()
        logger.info("准备进入系统托盘消息循环")
        self.tray_icon.run()


if __name__ == "__main__":
    try:
        _elevate_self_priority()
        logger.info("VoiceInputAI 入口启动 PID=%d executable=%s", os.getpid(), sys.executable)
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\VoiceInputAI_Tray_Mutex")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            logger.info("VoiceInputAI 已经在运行中，跳过重复启动。")
            sys.exit(0)
        app = VoiceInputApp()
        app.run()
    except Exception:
        logger.exception("VoiceInputAI 主程序崩溃")
