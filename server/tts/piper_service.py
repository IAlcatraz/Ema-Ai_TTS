"""Piper TTS sidecar server for Riko Project.
Replaces the heavy XTTS-v2 with the lightweight Piper engine for maximum stability.
Includes automatic voice downloading.
"""

from __future__ import annotations

import io
import json
import os
import threading
import wave
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import numpy as np
try:
    from piper.voice import PiperVoice
except ImportError:
    PiperVoice = None

# --- Configuration ---
# a more reliable voice mapping
# we use 'en_US-lessac-medium' but we'll fix the URL path.
VOICE_NAME = "en_US-lessac-medium"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = PROJECT_ROOT / "models" / "piper"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

_voice: Optional[PiperVoice] = None
_voice_lock = threading.Lock()

def _download_voice(voice_name: str):
    """Downloads the .onnx and .json files for the specified Piper voice."""
    # Correct HuggingFace path: /voices/<voice_name>/<voice_name>.onnx
    # For en_US-lessac-medium, the structure is often simplified in the latest repo
    base_url = f"https://huggingface.co/rhasspy/piper-voices/resolve/main"

    # Adjusted mapping to match actual HuggingFace folder structure
    # Format: /en/en_US/lessac/medium/en_US-lessac-medium.onnx
    # We'll try a few common patterns if the first fails.
    path_patterns = [
        f"en/en_US/lessac/medium",
        f"en/en_US/{voice_name.split('-')[2]}/{voice_name.split('-')[3]}",
        f"voices/{voice_name}"
    ]

    success = False
    for pattern in path_patterns:
        files_to_download = [
            f"{voice_name}.onnx",
            f"{voice_name}.onnx.json" # Note: some models use .onnx.json instead of .json
        ]

        try:
            # Test first file
            filename = files_to_download[0]
            target_path = MODELS_DIR / filename
            url = f"{base_url}/{pattern}/{filename}"

            print(f"[piper_service] Trying pattern {pattern} for {filename}...", flush=True)
            with urllib.request.urlopen(url, timeout=10) as response:
                with open(target_path, 'wb') as out_file:
                    out_file.write(response.read())

            # If first file works, try the json
            # Try both .json and .onnx.json
            json_found = False
            for j_ext in [".onnx.json", ".json"]:
                j_filename = voice_name + j_ext
                j_target = MODELS_DIR / j_filename
                j_url = f"{base_url}/{pattern}/{j_filename}"
                try:
                    with urllib.request.urlopen(j_url, timeout=10) as resp:
                        with open(j_target, 'wb') as out_f:
                            out_f.write(resp.read())
                    json_found = True
                    print(f"[piper_service] Successfully downloaded {j_filename}", flush=True)
                    break
                except:
                    continue

            if json_found:
                print(f"[piper_service] Successfully downloaded voice {voice_name} using pattern {pattern}", flush=True)
                success = True
                break
        except Exception as e:
            print(f"[piper_service] Pattern {pattern} failed: {e}", flush=True)
            continue

    if not success:
        raise RuntimeError(f"Could not download voice files for {voice_name} from any known pattern.")

def _load_voice():
    global _voice
    if _voice is not None:
        return _voice

    with _voice_lock:
        if _voice is not None:
            return _voice

        try:
            model_path = MODELS_DIR / f"{VOICE_NAME}.onnx"

            # Search for any json file that matches the voice name
            config_path = None
            for f in MODELS_DIR.glob(f"{VOICE_NAME}*.json"):
                config_path = f
                break

            if not model_path.exists() or config_path is None:
                print(f"[piper_service] Voice files missing. Starting download...", flush=True)
                _download_voice(VOICE_NAME)
                # Refresh path after download
                model_path = MODELS_DIR / f"{VOICE_NAME}.onnx"
                for f in MODELS_DIR.glob(f"{VOICE_NAME}*.json"):
                    config_path = f
                    break

            print(f"[piper_service] Loading Piper model from {model_path}", flush=True)
            _voice = PiperVoice.load(str(model_path))
            print(f"[piper_service] Piper voice loaded successfully!", flush=True)
        except Exception as e:
            print(f"[piper_service] CRITICAL ERROR loading Piper voice: {e}", flush=True)
            _voice = None
    return _voice

class PiperHandler(BaseHTTPRequestHandler):
    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Cache-Control")
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, status: int, payload: dict) -> None:
        self._respond(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _read_json_request(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if not 0 < content_length <= 1_000_000:
            raise ValueError("request body must be between 1 byte and 1 MB")
        return json.loads(self.rfile.read(content_length))

    def _synthesize(self, text: str) -> bytes:
        voice = _load_voice()
        if voice is None:
            raise RuntimeError("Piper voice not loaded")

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
        buffer.seek(0)
        return buffer.read()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Cache-Control")
        self.end_headers()

    def do_GET(self) -> None:
        route = urlparse(self.path).path.rstrip("/")
        if route == "/health":
            self._respond_json(200, {"status": "ok", "provider": "piper", "model": VOICE_NAME})
            return
        self._respond_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        route = urlparse(self.path).path.rstrip("/")

        if route not in ("/tts", "/tts_stream"):
            self._respond_json(404, {"error": "not found"})
            return

        try:
            request = self._read_json_request()
            text = request.get("text", "")
            if not text or not text.strip():
                self._respond_json(400, {"error": "text cannot be empty"})
                return

            audio_data = self._synthesize(text)
            self._respond(200, audio_data, "audio/wav")

        except Exception as e:
            import traceback
            print(f"[piper_service] Synthesis error: {e}", flush=True)
            traceback.print_exc()
            self._respond_json(500, {"error": str(e)})

if __name__ == "__main__":
    host, port = "127.0.0.1", 8020
    print(f"[piper_service] Starting Piper TTS on http://{host}:{port}", flush=True)
    with ThreadingHTTPServer((host, port), PiperHandler) as httpd:
        httpd.serve_forever()
