"""Loopback-only XTTS-v2 sidecar, run with the isolated Coqui environment."""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import threading
import time
import uuid
import re
import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import soundfile as sf
import torch
import yaml
import numpy as np
import librosa
from TTS.api import TTS
from server.config import VRM_BASE_URL

MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
XTTS_VOICE_ID = "riko"
SUPPORTED_LANGUAGES = {
    "ar", "zh-cn", "cs", "nl", "en", "fr", "de", "it", "pl", "pt",
    "ru", "es", "tr", "ja", "ko", "hu", "hi",
}
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEVICE_SETTING = os.getenv("XTTS_DEVICE", "auto").strip().lower()
if DEVICE_SETTING not in ("auto", "cpu", "xpu"):
    raise ValueError("XTTS_DEVICE must be one of: auto, cpu, xpu")
_model: Optional[TTS] = None
_active_device: Optional[str] = None
_model_lock = threading.Lock()
_http_session = requests.Session()
_tts_settings: dict[str, Any] = {
    "temperature": 0.6,
    "length_penalty": 1.0,
    "repetition_penalty": 3.0,
    "top_k": 50,
    "top_p": 0.8,
    "speed": 1.0,
    "enable_text_splitting": False,
    "stream_chunk_size": 60,
}
_DEFAULT_SETTINGS = dict(_tts_settings)
_latents: Optional[tuple] = None
_streams: dict[str, "_StreamJob"] = {}
_streams_lock = threading.Lock()
_STREAM_TTL_S = 300

# Sequential synthesis queue to guarantee strict order across all requests
_synthesis_queue = queue.Queue()

def _synthesis_worker():
    """Background worker that processes TTS requests sequentially to maintain strict order."""
    while True:
        try:
            # Get the next request: (text, language, handler)
            item = _synthesis_queue.get()
            if item is None: break

            text, language, handler = item

            # Split text into sentences
            segments = re.split(r'(?<=[.!?])\s+', text)
            if not segments:
                segments = [text]

            for i, segment in enumerate(segments):
                if not segment.strip():
                    continue

                # Synthesize and save the segment
                audio_bytes = handler._synthesize(segment, language)
                filename = f"tts_seg_{uuid.uuid4().hex}.wav"
                audio_path = PROJECT_ROOT / "client" / "audio" / filename
                audio_path.parent.mkdir(parents=True, exist_ok=True)
                audio_path.write_bytes(audio_bytes)

                # Calculate duration
                info = sf.info(io.BytesIO(audio_bytes))
                duration = info.duration

                notification = {
                    "audio_path": f"{XTTS_BASE_URL}/tts_stream/{filename}",
                    "expression": "neutral",
                    "audio_text": segment,
                    "audio_duraction": duration
                }

                # To maximize fluidity and minimize first-word lag:
                # We send every segment IMMEDIATELY after it's synthesized.
                try:
                    _http_session.post(f"{VRM_BASE_URL}/talk", json=notification, timeout=5)
                    print(f"[xtts_service] Segment {i+1}/{len(segments)} sent to /talk", flush=True)
                except Exception as e:
                    print(f"[xtts_service] Failed to notify /talk: {e}", flush=True)

            print(f"[xtts_service] Completed synthesis for: {text[:30]}...", flush=True)
            _synthesis_queue.task_done()
        except Exception as e:
            print(f"[xtts_service] Synthesis worker error: {e}", flush=True)
            import traceback
            traceback.print_exc()

# Start the single sequential synthesis worker
threading.Thread(target=_synthesis_worker, daemon=True).start()

class _StreamJob:
    def __init__(self) -> None:
        self.chunks: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.error: Optional[Exception] = None
        self.finished_at: Optional[float] = None

def _cleanup_old_audio():
    """Periodic cleanup of temporary synthesis files to prevent disk leaks."""
    try:
        audio_dir = PROJECT_ROOT / "client" / "audio"
        if not audio_dir.exists():
            return

        now = time.time()
        # Delete files older than 15 minutes (900s)
        for file in audio_dir.glob("tts_seg_*.wav"):
            if now - file.stat().st_mtime > 900:
                file.unlink()
    except Exception as e:
        print(f"[xtts_//_service] Audio cleanup failed: {e}", flush=True)

def _cleanup_loop():
    """Background thread to trigger cleanup every 30 minutes."""
    while True:
        _cleanup_old_audio()
        time.sleep(300)

# Start cleanup thread
threading.Thread(target=_cleanup_loop, daemon=True).start()

def _ffmpeg_path() -> str:
    configured = os.getenv("XTTS_FFMPEG_PATH", "").strip()
    if configured:
        if Path(configured).is_file():
            return configured
        raise RuntimeError(f"XTTS_FFMPEG_PATH does not exist: {configured}")
    path = shutil.which("ffmpeg")
    if path:
        return path
    raise RuntimeError(
        "ffmpeg is required for streamed TTS. Set XTTS_FFMPEG_PATH to ffmpeg.exe."
    )

def _read_character_settings() -> tuple[str, str]:
    character = os.getenv("RIKO_CHARACTER", "riko").strip().lower() or "riko"
    config_path = (
        PROJECT_ROOT / "character_config.yaml"
        if character == "riko"
        else PROJECT_ROOT / f"character_config_{character}.yaml"
    )
    with config_path.open("r", encoding="utf-8") as config_file:
        character_config = yaml.safe_load(config_file)

    base_url = str(
        character_config.get("xtts_base_url", "http://127.0.0.1:8020")
    ).strip().rstrip("/")
    reference = Path(
        character_config.get(
            "xtts_reference_wav",
            "client/characters/riko/voice/main_sample.wav",
        )
    )
    if not reference.is_absolute():
        reference = PROJECT_ROOT / reference
    return base_url, str(reference)

XTTS_BASE_URL, XTTS_REFERENCE_WAV = _read_character_settings()

def _get_latents(model: TTS) -> tuple[Any, Any]:
    global _latents
    if _latents is not None:
        return _latents[1], _latents[2]

    print(f"[xtts_service] calculating voice latents from {XTTS_REFERENCE_WAV}...", flush=True)
    assert model.synthesizer is not None
    xtts = model.synthesizer.tts_model
    assert xtts is not None
    cond, emb = xtts.get_conditioning_latents(audio_path=[XTTS_REFERENCE_WAV])
    _latents = (XTTS_REFERENCE_WAV, cond, emb)
    return cond, emb

def _load_model() -> TTS:
    global _model, _active_device
    if _model is None:
        _active_device = "xpu"
        if hasattr(torch, 'xpu') and torch.xpu.is_available():
            try:
                torch.xpu.set_device(0)
                print(f"[xtts_service] Attempting to load {MODEL_NAME} on GPU 0 (XPU) for speed", flush=True)
            except Exception as e:
                print(f"[xtts_service] Failed to set XPU device 0: {e}. Falling back to CPU.", flush=True)
                _active_device = "cpu"
        else:
            _active_device = "cpu"
            print(f"[xtts_service] XPU not available, falling back to CPU", flush=True)

        print(
            f"[xtts_service] loading {MODEL_NAME} on {_active_device} "
            f"(setting={DEVICE_SETTING})",
            flush=True,
        )
        _model = TTS(MODEL_NAME, progress_bar=True).to(_active_device)
        print(f"[xtts_service] XTTS-v2 ready on {_active_device}", flush=True)

        try:
            _get_latents(_model)
            print("[xtts_service] Voice latents pre-calculated and cached", flush=True)
        except Exception as e:
            print(f"[xtts_service] Warning: failed to pre-calculate latents: {e}", flush=True)

    return _model

class XTTSHandler(BaseHTTPRequestHandler):
    server_version = "RikoXTTS/1.0"

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Cache-Control")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, status: int, payload: dict) -> None:
        self._respond(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _read_json_request(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if not 0 < content_length <= 1_000_000:
            raise ValueError("request body must be between 1 byte and 1 MB")
        request = json.loads(self.rfile.read(content_length))
        if not isinstance(request, dict):
            raise ValueError("request body must be a JSON object")
        return request

    def _synthesize(
        self, text: str, language: str = "fr", use_provider_settings: bool = False
    ) -> bytes:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        if len(text) > 10_000:
            raise ValueError("text exceeds the 10,000 character limit")
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError(f"unsupported language: {language}")
        if not Path(XTTS_REFERENCE_WAV).is_file():
            raise ValueError(f"XTTS reference WAV not found: {XTTS_REFERENCE_WAV}")

        with _model_lock:
            model = _load_model()
            xtts = model.synthesizer.tts_model
            cond, emb = _get_latents(model)
            params = _tts_settings if use_provider_settings else _DEFAULT_SETTINGS
            out = xtts.inference(
                text=text,
                language=language,
                gpt_cond_latent=cond,
                speaker_embedding=emb,
                temperature=params["temperature"],
                length_penalty=params["length_penalty"],
                repetition_penalty=params["repetition_penalty"],
                top_k=params["top_k"],
                top_p=params["top_p"],
                speed=params["speed"],
                enable_text_splitting=params["enable_text_splitting"],
            )
            audio = out["wav"]

        if hasattr(audio, 'detach'):
            audio = audio.detach().float().cpu().numpy()

        # --- FIX: Hallucination & Noise Reduction ---
        # 1. Normalize audio to avoid clipping/artifacts
        if audio.size > 0:
            max_val = np.max(np.abs(audio))
            if max_val > 0:
                audio = audio / max_val

        # 2. Trim silence and noise from start/end (crucial for short words like "...hop !")
        # top_db=30 means anything below -30dB is considered silence.
        audio, _ = librosa.effects.trim(audio, top_db=30)

        # 3. Resample to target frequency
        # We use soxr if available for significantly better performance than the default resampler.
        audio_resampled = librosa.resample(audio, orig_sr=24000, target_sr=44100, res_type="soxr_hq")

        wav = io.BytesIO()
        sf.write(wav, audio_resampled, 44100, format="WAV", subtype="PCM_16")
        return wav.getvalue()

    def _start_stream(self, text: str, language: str) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        if len(text) > 10_000:
            raise ValueError("text exceeds the 10,000 character limit")
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError(f"unsupported language: {language}")
        if not Path(XTTS_REFERENCE_WAV).is_file():
            raise ValueError(f"XTTS reference WAV not found: {XTTS_REFERENCE_WAV}")
        _ffmpeg_path()

        stream_id = uuid.uuid4().hex
        job = _StreamJob()
        with _streams_lock:
            expired = [
                key for key, value in _streams.items()
                if value.finished_at and time.monotonic() - value.finished_at > _STREAM_TTL_S
            ]
            for key in expired:
                del _streams[key]
            _streams[stream_id] = job

        def generate() -> None:
            try:
                print(f"[xtts_service] generate start (segmented) for text: {text[:30]}...", flush=True)

                segments = re.split(r'(?<=[.!?])\s+', text)
                if not segments:
                    segments = [text]

                for i, segment in enumerate(segments):
                    if not segment.strip():
                        continue

                    with _model_lock:
                        model = _load_model()
                        xtts = model.synthesizer.tts_model
                        cond, emb = _get_latents(model)

                        print(f"[xtts_service] synthesizing segment {i+1}/{len(segments)}: {segment[:20]}...", flush=True)
                        out = xtts.inference(
                            text=segment,
                            language=language,
                            gpt_cond_latent=cond,
                            speaker_embedding=emb,
                            temperature=_tts_settings["temperature"],
                            length_penalty=_tts_settings["length_penalty"],
                            repetition_penalty=_tts_settings["repetition_penalty"],
                            top_k=_tts_settings["top_k"],
                            top_p=_tts_settings["top_p"],
                            speed=_tts_settings["speed"],
                            enable_text_splitting=_tts_settings["enable_text_splitting"],
                        )
                        audio = out["wav"]
                        print(f"[xtts_service] generated audio for: {segment[:50]}... (len={len(audio)})", flush=True)

                    samples = audio
                    if hasattr(samples, 'detach'):
                        samples = samples.detach().float().cpu().numpy()

                    pcm = np.clip(samples, -1, 1)
                    segment_bytes = (pcm * 32767).astype("<i2").tobytes()

                    chunk_size_bytes = 4096 * 4
                    for j in range(0, len(segment_bytes), chunk_size_bytes):
                        job.chunks.put(segment_bytes[j : j + chunk_size_bytes])

                    print(f"[xtts_service] segment {i+1} delivered to queue", flush=True)

                print(f"[xtts_service] all segments finished. Total: {len(segments)}", flush=True)

            except Exception as e:
                job.error = e
                print(
                    f"[xtts_service] synthesis failed: {type(e).__name__}: {e}",
                    flush=True,
                )
                import traceback
                traceback.print_exc()
            finally:
                job.finished_at = time.monotonic()
                job.chunks.put(None)

        threading.Thread(target=generate, daemon=True).start()
        return f"{XTTS_BASE_URL}/tts_stream/{stream_id}"

    def _serve_stream(self, stream_id: str) -> None:
        with _streams_lock:
            job = _streams.get(stream_id)
        if job is None:
            self._respond_json(404, {"error": "unknown or expired stream"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            while True:
                chunk = job.chunks.get()
                if chunk is None:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            print(f"[xtts_service] stream error: {e}", flush=True)
        finally:
            pass

    def do_GET(self) -> None:
        route = urlparse(self.path).path.rstrip("/")
        if route.startswith("/tts_stream/"):
            filename = route.rsplit("/", 1)[-1]
            audio_path = PROJECT_ROOT / "client" / "audio" / filename
            if audio_path.is_file():
                try:
                    self._respond(200, audio_path.read_bytes(), "audio/wav")
                    return
                except Exception as e:
                    print(f"[xtts_service] failed to serve audio file {filename}: {e}")

            self._serve_stream(filename)
            return
        if route == "/health":
            self._respond_json(
                200,
                {
                    "status": "ok",
                    "model_loaded": _model is not None,
                    "device": _active_device,
                    "device_setting": DEVICE_SETTING,
                },
            )
            return
        if route == "/speakers":
            self._respond_json(
                200,
                [{
                    "name": XTTS_VOICE_ID,
                    "voice_id": XTTS_VOICE_ID,
                    "preview_url": f"{XTTS_BASE_URL}/sample/voice.wav",
                }],
            )
            return
        if route == "/sample/voice.wav":
            try:
                reference_path = Path(XTTS_REFERENCE_WAV)
                if not reference_path.is_file():
                    raise FileNotFoundError(
                        f"XTTS reference WAV not found: {reference_path}"
                    )
                self._respond(200, reference_path.read_bytes(), "audio/wav")
            except OSError as e:
                print(f"[xtts_service] voice preview failed: {e}", flush=True)
                self._respond_json(500, {"error": "voice preview unavailable"})
            return
        if route == "/languages":
            self._respond_json(200, {"languages": sorted(SUPPORTED_LANGUAGES)})
            return
        if route == "/get_tts_settings":
            with _model_lock:
                settings = dict(_tts_settings)
            self._respond_json(200, settings)
            return
        if route != "/":
            self._respond_json(404, {"error": "not found"})
            return
        self._respond_json(200, {"status": "ok", "service": "RikoXTTS"})

    def _warmup(self):
        """Force a small synthesis to wake up the GPU/XPU and cache CUDA kernels."""
        try:
            print("[xtts_service] Starting GPU warm-up synthesis...", flush=True)
            self._synthesize("Warm up", "fr")
            print("[xtts_service] GPU warm-up complete. Model is now hot.", flush=True)
        except Exception as e:
            print(f"[xtts_service] Warm-up failed: {e}", flush=True)

    def do_POST(self) -> None:
        route = urlparse(self.path).path.rstrip("/")
        if route == "/warmup":
            self._warmup()
            self._respond_json(200, {"status": "warmed up"})
            return
        if route not in ("/tts", "/tts_stream", "/tts_to_audio", "/set_tts_settings"):
            self._respond_json(404, {"error": "not found"})
            return

        try:
            request = self._read_json_request()
            if route == "/set_tts_settings":
                settings = {
                    "temperature": (float, 0.01, 1.0),
                    "length_penalty": (float, 0.5, 2.0),
                    "repetition_penalty": (float, 1.0, 10.0),
                    "top_k": (int, 1, 100),
                    "top_p": (float, 0.0, 1.0),
                    "speed": (float, 0.5, 2.0),
                    "stream_chunk_size": (int, 20, 400),
                }
                updated = dict(_tts_settings)
                for name, (value_type, minimum, maximum) in settings.items():
                    value = request.get(name, updated[name])
                    if isinstance(value, bool):
                        raise ValueError(f"{name} must be numeric")
                    try:
                        value = value_type(value)
                    except (TypeError, ValueError) as e:
                        raise ValueError(f"{name} must be numeric") from e
                    if not minimum <= value <= maximum:
                        print(f"[xtts_service] WARN: parameter {name}={value} out of range ({minimum}-{maximum}). Clamping.")
                        value = max(minimum, min(maximum, value))
                    updated[name] = value

                print(f"[xtts_service] UPDATING SETTINGS: {updated}")
                split = request.get(
                    "enable_text_splitting", updated["enable_text_splitting"]
                )
                if not isinstance(split, bool):
                    raise ValueError("enable_text_splitting must be a boolean")
                updated["enable_text_splitting"] = split
                with _model_lock:
                    _tts_settings.update(updated)
                self._respond_json(200, {"message": "Settings successfully applied"})
                return

            text = request.get("text")
            language = request.get("language", "fr")
            if not isinstance(language, str):
                raise ValueError("language must be a language code")
            language = language.lower()

            if route == "/tts_stream":
                print(f"[xtts_service] POST /tts_stream (Fast-Start): {str(text)[:30]}...", flush=True)
                _synthesis_queue.put((text, language, self))
                self._respond_json(200, {"status": "processing", "message": "Fast-Start synthesis started"})
                return

            if route == "/tts_to_audio":
                speaker = request.get("speaker_wav")
                if speaker != XTTS_VOICE_ID:
                    raise ValueError(f"unknown XTTS voice: {speaker}")

            audio = self._synthesize(
                text,
                language,
                use_provider_settings=(route == "/tts_to_audio"),
            )
            self._respond(200, audio, "audio/wav")
        except (ValueError, json.JSONDecodeError) as e:
            print(f"[xtts_service] invalid request: {e}", flush=True)
            self._respond_json(400, {"error": str(e)})
        except Exception as e:
            import traceback
            print(
                f"[xtts_service] CRITICAL ERROR in do_POST: {type(e).__name__}: {e}",
                flush=True,
            )
            traceback.print_exc()
            self._respond_json(
                500,
                {"error": f"XTTS synthesis failed: {type(e).__name__}: {e}"},
            )

    def log_message(self, format: str, *args: object) -> None:
        print(f"[xtts_service] {self.address_string()} {format % args}", flush=True)

    def do_OPTIONS(self) -> None:
        self._respond(204, b"", "text/plain")

if __name__ == "__main__":
    parsed = urlparse(XTTS_BASE_URL)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError(
            "xtts_base_url must use http and point to 127.0.0.1 또는 localhost"
        )
    host = "127.0.0.1"
    port = parsed.port or 8020

    # Pre-load the model and warm up the GPU at startup to avoid latency on the first request
    print("[xtts_service] Pre-loading model for immediate availability...", flush=True)
    try:
        # Create a temporary handler to trigger the warmup
        class WarmupHandler(XTTSHandler):
            def _synthesize(self, text, lang="fr", use_provider_settings=False):
                return b""

        # This triggers _load_model() and _get_latents()
        _load_model()
        _get_latents(_load_model())
        print("[xtts_service] Model and latents pre-loaded successfully", flush=True)
    except Exception as e:
        print(f"[xtts_service] Warning: Pre-loading failed: {e}", flush=True)

    with ThreadingHTTPServer((host, port), XTTSHandler) as httpd:
        print(
            f"[xtts_service] listening on http://{host}:{port} "
            f"reference={XTTS_REFERENCE_WAV}",
            flush=True,
        )
        httpd.serve_forever()
