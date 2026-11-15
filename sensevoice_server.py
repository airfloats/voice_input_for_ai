# -*- coding: utf-8 -*-
"""
SenseVoice-Small FP16 ONNX DirectML 核显语音识别 HTTP 服务
监听 127.0.0.1:18081，兼容 / (健康检查) 与 /inference (multipart wav 上传)
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import threading
import time
import wave
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / "py_deps"))
sys.path.insert(0, str(BASE_DIR))

import numpy as np
from sensevoice_engine import ASREngineConfig, SenseVoiceInference

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_handlers: list[logging.Handler] = []
if os.environ.get("VOICEINPUT_MANAGED") != "1" or sys.stdout is None:
    _handlers.append(logging.FileHandler(LOG_DIR / "sensevoice_server.log", encoding="utf-8"))
if sys.stdout is not None:
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=_handlers)
logger = logging.getLogger("SenseVoiceServer")

HOST = os.environ.get("ASR_HOST", "127.0.0.1")
PORT = int(os.environ.get("ASR_PORT", "18081"))

MODEL_DIR = BASE_DIR / "models" / "sensevoice"
ENGINE: SenseVoiceInference | None = None
ENGINE_LOCK = threading.Lock()


def init_engine() -> SenseVoiceInference:
    cfg = ASREngineConfig(
        encoder_path=str(MODEL_DIR / "SenseVoice-Encoder.fp16.onnx"),
        decoder_path=str(MODEL_DIR / "SenseVoice-CTC.fp16.onnx"),
        tokenizer_path=str(MODEL_DIR / "tokenizer.bpe.model"),
        onnx_provider="DML",
        dml_pad_to=30,
    )
    t0 = time.perf_counter()
    eng = SenseVoiceInference(cfg)
    dummy = np.zeros(16000, dtype=np.float32)
    eng.recognize(dummy, lid="zh", itn=True)
    eng.recognize(dummy, lid="zh", itn=True)
    logger.info("SenseVoice DirectML 核显引擎初始化+预热完成，耗时 %.0f ms", (time.perf_counter() - t0) * 1000)
    return eng


def wav_bytes_to_float32(wav_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if sw != 2:
        raise ValueError(f"Unsupported sample width: {sw}")
    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    return arr


VALID_LIDS = {"auto", "zh", "en", "yue", "ja", "ko"}


def parse_multipart_fields(content_type: str, body: bytes) -> dict[str, bytes]:
    raw_msg = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1") + body
    msg = BytesParser(policy=default).parsebytes(raw_msg)
    fields: dict[str, bytes] = {}
    if not msg.is_multipart():
        return fields
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            payload = part.get_payload(decode=True)
            if payload is not None:
                fields[name] = payload
    return fields


class ASRHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True

    def _send_json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def do_GET(self) -> None:
        if ENGINE is None:
            self._send_json(503, {"status": "loading"})
        else:
            self._send_json(200, {"status": "ok", "backend": "DirectML"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/inference":
            self._send_json(404, {"error": "not found"})
            return
        if ENGINE is None:
            self._send_json(503, {"error": "engine loading"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        fields = parse_multipart_fields(ctype, body) if "multipart/form-data" in ctype else {}
        wav_bytes = fields.get("file") if "multipart/form-data" in ctype else body
        if not wav_bytes:
            self._send_json(400, {"error": "missing file"})
            return

        qs = parse_qs(parsed.query)
        lid = (fields.get("lid", b"").decode("utf-8", "ignore") or (qs.get("lid", ["zh"])[0])).strip().lower()
        if lid not in VALID_LIDS:
            lid = "zh"
        preview_flag = (fields.get("preview", b"").decode("utf-8", "ignore") or (qs.get("preview", ["0"])[0])).strip().lower()
        is_preview = preview_flag in ("1", "true", "yes")

        audio = wav_bytes_to_float32(wav_bytes)
        if len(audio) < 160:
            self._send_json(200, {"text": "", "elapsed_ms": 0.0})
            return

        if not ENGINE_LOCK.acquire(blocking=not is_preview):
            # 预览请求不排队，避免积压拖慢松开后的最终识别
            self._send_json(200, {"text": "", "busy": True})
            return

        t0 = time.perf_counter()
        try:
            res = ENGINE.recognize(audio, lid=lid, itn=True)
        finally:
            ENGINE_LOCK.release()
        text = re.sub(r"<\|.*?\|>", "", res.text or "").strip()
        cost_ms = (time.perf_counter() - t0) * 1000
        if not is_preview:
            logger.info("识别完成 [lid=%s] (%.1f ms): %r", lid, cost_ms, text)
        self._send_json(200, {"text": text, "elapsed_ms": round(cost_ms, 1)})


class FastHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32


def main() -> None:
    global ENGINE
    ENGINE = init_engine()
    server = FastHTTPServer((HOST, PORT), ASRHandler)
    logger.info("SenseVoice DirectML ASR 服务已监听 http://%s:%d", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
