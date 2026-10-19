"""Ordered playback worker.

Single FIFO thread that drains a queue of `PlaybackItem`s and drives the
VRM avatar. Three item types:

  - TEXT       — `vrm_talk(audio_path, current_expression, text, duration)`
                 then sleeps `duration + pad` so two clips never overlap
                 (pad = the browser's fetch cost; see server/config.py).
  - EXPRESSION — updates `self.current_expression`. No sleep, no HTTP — this
                 just changes which expression the NEXT TEXT item plays with.
  - EMOTE      — `vrm_animate(play_once=True)` with a `.fbx` path.

"Stylistic pause" rules (100ms grace) per the architecture doc:
  - if previous item was EMOTE and current is TEXT → sleep 0.1s before the
    text plays (lets the emote land before audio kicks in).
  - if next queued item is also EMOTE after this EMOTE → sleep 0.1s after
    firing the current one (prevents back-to-back triggers from overlapping).

On TURN_END: reset `current_expression` to `DEFAULT_EXPRESSION`, fire the
idle animation, signal `queue_finished_event`. Queue emptiness alone does not
mean a turn has finished: the producer can be generating audio for the next
chunk, so the producer adds an explicit TURN_END item after the full response.

Public surface:
    OrderedPlaybackWorker
        .start()
        .enqueue(item)
        .wait_until_finished(timeout=None) -> bool
        .stop()
        .current_expression: str
    PlaybackItem (dataclass)
    ItemType (enum: TEXT, EXPRESSION, EMOTE, TURN_END)
"""

from __future__ import annotations

import time
from concurrent.futures import CancelledError, Future, TimeoutError
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Optional, Tuple

from server.config import (
    DEFAULT_EXPRESSION,
    MIXAMO_REL_DIR,
    PLAYBACK_FIRST_PAD_S,
    PLAYBACK_PAD_S,
)
from server.vrm.vrm_client import vrm_animate, vrm_stop_talk, vrm_talk


# Grace period (seconds) between EMOTE→TEXT and EMOTE→EMOTE transitions.
EMOTE_GRACE_S = 0.1


class ItemType(Enum):
    TEXT = "text"
    EXPRESSION = "expression"
    EMOTE = "emote"
    TURN_END = "turn_end"


@dataclass
class PlaybackItem:
    item_type: ItemType
    # TEXT fields:
    audio_path: Optional[Path] = None   # browser-relative, e.g. "audio/x.wav"
    text: str = ""                      # spoken text (for subtitles / lipsync)
    duration: float = 0.0               # seconds
    audio_future: Optional[Future[Optional[Tuple[Path, float]]]] = None
    # EXPRESSION fields:
    name: str = ""                      # one of the 6 VRM expressions
    # EMOTE fields:
    animate_url: str = ""               # browser-relative path to the .fbx
                                        # (e.g. "animations/mixamo/Backflip.fbx")
                                        # — NOT an absolute filesystem path,
                                        # else the browser can't fetch it


class OrderedPlaybackWorker:
    """Drains a FIFO queue of `PlaybackItem`s on a single background thread."""

    def __init__(self, interrupt_event: Optional[Event] = None) -> None:
        self.queue: "Queue[Optional[PlaybackItem]]" = Queue()
        self.thread = Thread(target=self._run, daemon=True)
        self._running = False
        self.queue_finished_event = Event()
        self.queue_finished_event.set()  # start "finished" (nothing pending)
        self.current_expression: str = DEFAULT_EXPRESSION
        self._previous_type: Optional[ItemType] = None
        # First TEXT of a turn pays the client's cold-start cost — see the pad
        # constants in server/config.py. Reset every time the queue drains.
        self._first_text = True
        self.interrupt_event = interrupt_event or Event()
        self._interrupt_lock = Lock()
        self._interrupted = False

    # ---- producer-side API ----

    def start(self) -> None:
        if not self._running:
            self._running = True
            self.thread.start()
            print(
                f"[playback] worker started "
                f"(default_expression={self.current_expression!r})"
            )

    def enqueue(self, item: PlaybackItem) -> None:
        with self._interrupt_lock:
            if self.interrupt_event.is_set():
                return
            self.queue_finished_event.clear()
            self.queue.put(item)

    def finish_turn(self) -> None:
        """Queue the idle reset after all items produced for this turn."""
        self.enqueue(PlaybackItem(item_type=ItemType.TURN_END))

    def begin_turn(self) -> None:
        """Clear the prior turn's interruption before accepting playback."""
        with self._interrupt_lock:
            self.interrupt_event.clear()
            self._interrupted = False
            self.current_expression = DEFAULT_EXPRESSION
            self._previous_type = None
            self._first_text = True

    def interrupt(self) -> None:
        """Cancel queued speech and stop the browser's current audio clip."""
        with self._interrupt_lock:
            if self._interrupted:
                return
            self._interrupted = True
            self.interrupt_event.set()
            while True:
                try:
                    self.queue.get_nowait()
                except Empty:
                    break
            self.queue_finished_event.set()
        try:
            vrm_stop_talk()
            print("[playback] speech interrupted")
        except Exception as e:
            print(f"[playback] stop_talk failed: {type(e).__name__}: {e}")

    def wait_until_finished(self, timeout: Optional[float] = None) -> bool:
        return self.queue_finished_event.wait(timeout)

    def stop(self) -> None:
        self.queue.put(None)
        self.thread.join()
        print("[playback] worker stopped")

    # ---- consumer-side internals ----

    def _peek_next_type(self) -> Optional[ItemType]:
        """Look at the front of the queue without consuming. Safe enough for
        our single-producer-single-consumer setup; only used for the EMOTE→
        EMOTE grace decision."""
        try:
            return self.queue.queue[0].item_type  # type: ignore[union-attr]
        except (IndexError, AttributeError):
            return None

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                break
            if self.interrupt_event.is_set():
                continue

            if item.item_type == ItemType.TEXT:
                self._handle_text(item)
            elif item.item_type == ItemType.EXPRESSION:
                self._handle_expression(item)
            elif item.item_type == ItemType.EMOTE:
                self._handle_emote(item)
            elif item.item_type == ItemType.TURN_END:
                self._reset_to_idle()
            else:
                print(f"[playback] WARN unknown item type: {item.item_type}")

            self._previous_type = item.item_type

    def _handle_text(self, item: PlaybackItem) -> None:
        if item.audio_future is not None:
            while not self.interrupt_event.is_set():
                try:
                    result = item.audio_future.result(timeout=0.1)
                    break
                except TimeoutError:
                    continue
                except CancelledError:
                    print("[playback] TEXT synthesis was cancelled")
                    return
                except Exception as e:
                    print(
                        f"[playback] TEXT synthesis future failed: "
                        f"{type(e).__name__}: {e}"
                    )
                    return
            else:
                return

            if self.interrupt_event.is_set():
                return
            if result is None:
                print(f"[playback] TEXT synthesis skipped: {item.text!r}")
                return
            item.audio_path, item.duration = result

        # Stylistic pause: if we just played an EMOTE, give it a tick to land
        # before the next line starts speaking.
        if self._previous_type == ItemType.EMOTE:
            print(f"[playback] EMOTE→TEXT grace {EMOTE_GRACE_S}s")
            time.sleep(EMOTE_GRACE_S)

        print(
            f"[playback] TEXT expression={self.current_expression!r} "
            f"duration={item.duration:.2f}s text={item.text!r}"
        )
        try:
            vrm_talk(
                audio_path=str(item.audio_path),
                expression=self.current_expression,
                audio_text=item.text,
                audio_duration=item.duration,
            )
        except Exception as e:
            print(f"[playback] vrm_talk failed: {e}")

        # Hold for the length of the clip so the next line doesn't stomp the
        # audio still playing in the browser. The pad covers the browser's
        # fetch-before-play cost, which the clip length alone does not —
        # without it a short line gets cut off by the next one. The first clip
        # of a turn pays more (cold connection, first <audio> load), so it has
        # its own value instead of taxing every sentence with the worst case.
        pad = PLAYBACK_FIRST_PAD_S if self._first_text else PLAYBACK_PAD_S
        print(f"[playback] hold {item.duration:.2f}s + {pad:.2f}s pad"
              f"{' (first of turn)' if self._first_text else ''}")
        self._first_text = False
        self.interrupt_event.wait(item.duration + pad)

    def _handle_expression(self, item: PlaybackItem) -> None:
        # No HTTP, no sleep — just remember which expression the next TEXT
        # item should play with.
        print(
            f"[playback] EXPRESSION {self.current_expression!r} -> {item.name!r}"
        )
        self.current_expression = item.name

    def _handle_emote(self, item: PlaybackItem) -> None:
        print(f"[playback] EMOTE animate_url={item.animate_url}")
        try:
            # "auto" — server auto-detects start_mixamo vs start_vrma from the
            # file extension. Works for `.fbx` (mixamo) and `.vrma` alike.
            vrm_animate(
                animate_type="auto",
                animation_url=item.animate_url,
                play_once=True,
            )

            # FIX: Add a mandatory safety delay for animations to ensure they finish.
            # Mixamo clips are usually short, but VRMA (like Dogeza) can be longer.
            # We add a small hold to prevent the next item (or the turn end reset)
            # from interrupting the animation too quickly.
            if ".vrma" in item.animate_url:
                # Give VRMA animations more time to land (e.g., 2 seconds)
                time.sleep(2.0)
            else:
                # Standard Mixamo grace period
                time.sleep(0.5)

        except Exception as e:
            print(f"[playback] vrm_animate failed: {e}")

        # Stylistic pause: if the next queued item is also an emote, give the
        # current one a tick to land before triggering the next.
        if self._peek_next_type() == ItemType.EMOTE:
            print(f"[playback] EMOTE→EMOTE grace {EMOTE_GRACE_S}s")
            time.sleep(EMOTE_GRACE_S)

    def _reset_to_idle(self) -> None:
        """Turn is finished — reset expression and fire the idle animation."""
        print(
            f"[playback] turn complete, reset expression -> "
            f"{DEFAULT_EXPRESSION!r}, idle animation"
        )
        self.current_expression = DEFAULT_EXPRESSION
        self._first_text = True

        # FIX: To prevent the Idle animation from cutting off a long VRMA animation
        # (like dogeza) that was just triggered, we add a small mandatory buffer
        # before resetting to idle.
        time.sleep(0.5)

        try:
            vrm_animate(
                animate_type="start_mixamo",
                animation_url=str(MIXAMO_REL_DIR / "Idle.fbx"),
                play_once=False,
            )
        except Exception as e:
            print(f"[playback] idle vrm_animate failed: {e}")
        self.queue_finished_event.set()


if __name__ == "__main__":
    # Standalone test; the configured local voice service must be available.
    # Hand-builds the sequence from the plan and pushes it.
    # Run with: python -m server.playback.playback
    from server.tts.xtts import synthesize

    print("[playback.__main__] synthesizing test audio...")
    hi_path, hi_dur = synthesize("Bonjour ! Regardez ceci.")
    bye_path, bye_dur = synthesize("Très bien, à bientôt !")

    worker = OrderedPlaybackWorker()
    worker.start()

    # Match the plan's example sequence:
    # [EXPR(happy), EMOTE(Backflip), TEXT(hi), EMOTE(Waving), TEXT(bye)]
    # animate_url uses MIXAMO_REL_DIR (browser-relative path), NOT MIXAMO_DIR
    # (absolute filesystem path) — see vrm_client.py module docstring.
    seq = [
        PlaybackItem(item_type=ItemType.EXPRESSION, name="happy"),
        PlaybackItem(
            item_type=ItemType.EMOTE,
            animate_url=str(MIXAMO_REL_DIR / "Backflip.fbx"),
        ),
        PlaybackItem(
            item_type=ItemType.TEXT,
            audio_path=hi_path,
            text="Hi! Watch this.",
            duration=hi_dur,
        ),
        PlaybackItem(
            item_type=ItemType.EMOTE,
            animate_url=str(MIXAMO_REL_DIR / "Waving.fbx"),
        ),
        PlaybackItem(
            item_type=ItemType.TEXT,
            audio_path=bye_path,
            text="All right, bye for now!",
            duration=bye_dur,
        ),
    ]
    for item in seq:
        worker.enqueue(item)

    print("[playback.__main__] enqueued sequence, waiting for finish...")
    worker.finish_turn()
    worker.wait_until_finished()

    # After drain, expression must reset to DEFAULT_EXPRESSION.
    assert worker.current_expression == DEFAULT_EXPRESSION, (
        f"expected reset to {DEFAULT_EXPRESSION!r}, got "
        f"{worker.current_expression!r}"
    )
    print(
        f"[playback.__main__] OK — queue drained, current_expression reset "
        f"to {worker.current_expression!r}"
    )
    worker.stop()
