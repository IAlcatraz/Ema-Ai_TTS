"""Watches the desktop: active window, open windows, periodic screen glance.

Window info is cheap and refreshed every few seconds. A screenshot is described
by the local vision model at most every SCREEN_WATCH_INTERVAL_S, only when the
user is not speaking and she is not talking, and never for private windows.
Optionally (SCREEN_PROACTIVE) a fresh glance can make her speak up by herself,
rarely: a minimum gap, a random chance and a required quiet period apply.
"""
import logging
import random
import time
from queue import Queue
from typing import Optional

from server.config import (
    SCREEN_PROACTIVE, SCREEN_PROACTIVE_CHANCE, SCREEN_PROACTIVE_MIN_GAP_S,
    SCREEN_PROACTIVE_MIN_QUIET_S, SCREEN_WATCH_INTERVAL_S,
    SCREEN_WATCH_MAX_STALE_S, SCREEN_WATCH_QUIET_S,
)
from server.streaming_agent.desktop_context import (
    CONTEXT, active_window_title, capture_screen, is_private, open_window_titles,
    screen_signature,
)
from server.streaming_agent.event import Event
from server.streaming_agent.inputs.base_input import BaseInput
from server.streaming_agent.state import AgentState

log = logging.getLogger(__name__)

POLL_S = 3.0


class DesktopInput(BaseInput):
    def __init__(self, event_queue: Queue, responding_event=None,
                 state: Optional[AgentState] = None):
        super().__init__(event_queue, name="DesktopInput",
                         responding_event=responding_event)
        self.state = state
        self._last_glance = 0.0
        self._last_proactive = time.time()      # no remark right at startup
        self._glanced_title = ""
        self._last_screen_signature: Optional[bytes] = None

    def run(self) -> None:
        log.info("[%s] watching the desktop (glance every >=%ss)",
                 self.name, SCREEN_WATCH_INTERVAL_S)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("[%s] tick failed", self.name)
            self._stop_event.wait(POLL_S)

    def _busy(self) -> bool:
        recording = bool(self.state and self.state.voice_recording)
        return recording or self._is_responding()

    def _tick(self) -> None:
        title = active_window_title()
        CONTEXT.set_active(title, open_window_titles())

        if self._busy() or is_private(title):
            return
        # Never glance in the middle of a conversation: the vision model would
        # compete with the reply she is about to generate.
        if self.state and self.state.time_since_last_voice() < SCREEN_WATCH_QUIET_S:
            return
        now = time.time()
        since = now - self._last_glance
        changed = title != self._glanced_title
        if since < SCREEN_WATCH_INTERVAL_S or not (
                changed or since >= SCREEN_WATCH_MAX_STALE_S):
            return

        path = capture_screen("screen_watch.jpg")
        if not path:
            self._last_glance = now
            return
        try:
            signature = screen_signature(path)
        except Exception as e:
            log.warning("[%s] screen change detection failed: %s", self.name, e)
            self._last_glance = now
            return
        if signature == self._last_screen_signature and not changed:
            self._last_glance = now
            log.debug("[%s] screen unchanged; skipping vision analysis", self.name)
            return
        from server.llm.image_attach import describe_image
        try:
            desc = describe_image(path, kind="screen", max_tokens=120, brief=True)
        except Exception as e:
            log.warning("[%s] screen description failed: %s", self.name, e)
            self._last_glance = time.time()
            return
        self._last_glance = time.time()
        # The window may have changed while the model was thinking.
        if active_window_title() != title:
            return
        self._last_screen_signature = signature
        self._glanced_title = title
        CONTEXT.set_screen(desc)
        log.info("[%s] screen glance stored (%d chars)", self.name, len(desc))
        if changed:
            self._maybe_speak_up(desc)

    def _maybe_speak_up(self, desc: str) -> None:
        if not SCREEN_PROACTIVE or self._busy():
            return
        now = time.time()
        if now - self._last_proactive < SCREEN_PROACTIVE_MIN_GAP_S:
            return
        if self.state and self.state.time_since_last_voice() < SCREEN_PROACTIVE_MIN_QUIET_S:
            return
        if random.random() > SCREEN_PROACTIVE_CHANCE:
            return
        self._last_proactive = now
        self.emit(Event(type="screen", content=desc, importance=0.9))
