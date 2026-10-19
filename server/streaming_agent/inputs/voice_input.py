"""Voice input stream.

Listens to the microphone, records on detected speech, transcribes via Groq Whisper,
and emits a 'voice' Event with the transcribed text.

Built on server/asr/recorder.py (the microphone + speech detector) and
server/asr/transcribe.py (Groq Whisper).
"""
import logging
import os
import tempfile
from queue import Queue
from typing import Callable, Optional

from server.asr.recorder import record_on_speech
from server.asr.transcribe import transcribe_audio_groq
from server.config import ASR_PROMPT
from server.streaming_agent.event import Event
from server.streaming_agent.inputs.base_input import BaseInput
from server.streaming_agent.inputs.webcam_context import webcam_frame_for
from server.streaming_agent.state import AgentState

log = logging.getLogger(__name__)


class VoiceInput(BaseInput):
    """Continuously listens for user speech and emits transcribed events."""

    def __init__(
        self,
        event_queue: Queue,
        # 16k: what Silero requires, and what Whisper downsamples to anyway —
        # recording higher only made the upload bigger.
        samplerate: int = 16000,
        channels: int = 1,
        silence_threshold: float = 0.02,
        silence_duration: float = 1.0,
        device: Optional[int] = None,
        audio_path: Optional[str] = None,
        prompt: str = ASR_PROMPT,   # character-aware (server/config.py)
        responding_event=None,
        state: Optional[AgentState] = None,
        transcribe_fn: Optional[Callable] = None,
        interrupt_event=None,
        interrupt_callback: Optional[Callable[[], None]] = None,
    ):
        super().__init__(event_queue, name="VoiceInput", responding_event=responding_event)
        self.samplerate = samplerate
        self.channels = channels
        self.silence_threshold = silence_threshold
        self.silence_duration = silence_duration
        self.device = device
        self.audio_path = audio_path or os.path.join(
            tempfile.gettempdir(), "streaming_agent_voice.wav"
        )
        self.prompt = prompt
        # Optional state reference so we can flip voice_recording while the
        # mic is open. Without it, long utterances can let the silence-based
        # vision rule fire mid-speech (the user's voice ends up dropped).
        self.state = state
        # Speech-to-text, injected so a character can swap providers without
        # this file changing. Signature: (aud_path, prompt) -> str.
        # None means Groq Whisper, the default.
        self.transcribe_fn = transcribe_fn or transcribe_audio_groq
        self.interrupt_event = interrupt_event
        self.interrupt_callback = interrupt_callback

    def run(self) -> None:
        log.info("[%s] entering listen loop (audio_path=%s)", self.name, self.audio_path)
        while not self._stop_event.is_set():
            if self._is_responding():
                self._listen_for_interruption()
                continue

            self._wait_if_responding()
            if self._stop_event.is_set():
                break

            try:
                # Blocks until speech is detected and ends with sustained silence.
                # record_on_speech deletes any prior file at this path.
                if self.state is not None:
                    self.state.set_voice_recording(True)
                try:
                    record_on_speech(
                        output_file=self.audio_path,
                        samplerate=self.samplerate,
                        channels=self.channels,
                        silence_threshold=self.silence_threshold,
                        silence_duration=self.silence_duration,
                        device=self.device,
                    )
                finally:
                    if self.state is not None:
                        self.state.set_voice_recording(False)
                if self._stop_event.is_set():
                    break

                # If the AI started talking while we were recording, the audio
                # is most likely the AI's own TTS bleed-through — drop it.
                if self._is_responding():
                    log.info("[%s] AI is responding — discarding recording", self.name)
                    continue

                log.info("[%s] transcribing recorded audio...", self.name)
                text = self.transcribe_fn(aud_path=self.audio_path, prompt=self.prompt)
                if not text or not text.strip():
                    log.info("[%s] empty transcription, skipping emit", self.name)
                    continue

                if self._is_responding():
                    log.info("[%s] AI started responding mid-transcribe — dropping", self.name)
                    continue

                self.emit(Event(
                    type="voice",
                    content=text.strip(),
                    importance=1.0,
                    metadata={"audio_path": self.audio_path,
                              "image_path": webcam_frame_for(text)},
                ))
            except Exception:
                log.exception("[%s] error during listen/transcribe cycle", self.name)

    def _listen_for_interruption(self) -> None:
        """Capture sustained user speech while Riko is responding."""
        interrupted = False

        def interrupt() -> None:
            nonlocal interrupted
            if interrupted:
                return
            interrupted = True
            log.info("[%s] sustained speech detected — interrupting playback", self.name)
            if self.interrupt_callback is not None:
                self.interrupt_callback()
            elif self.interrupt_event is not None:
                self.interrupt_event.set()

        try:
            record_on_speech(
                output_file=self.audio_path,
                samplerate=self.samplerate,
                channels=self.channels,
                silence_threshold=self.silence_threshold,
                silence_duration=max(self.silence_duration, 0.35),
                device=self.device,
                speech_start_callback=interrupt,
                speech_start_duration=0.45,
            )
            if self._stop_event.is_set() or not interrupted:
                return

            log.info("[%s] transcribing interruption...", self.name)
            text = self.transcribe_fn(aud_path=self.audio_path, prompt=self.prompt)
            if not text or not text.strip():
                log.info("[%s] empty interruption transcription, skipping emit", self.name)
                return

            # The current turn must finish unwinding before this event is
            # offered to the decision engine, which is paused while responding.
            self._wait_if_responding()
            if self._stop_event.is_set():
                return

            self.emit(Event(
                type="voice",
                content=text.strip(),
                importance=1.0,
                metadata={"audio_path": self.audio_path, "interruption": True},
            ))
        except Exception:
            log.exception("[%s] error while capturing interruption", self.name)
