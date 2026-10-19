"""MemorySession — orchestrates the three memory tiers for one session.

Owns the verbatim window (short), the rolling gist (medium), the FAISS
store (long) and the raw SessionLog. This is the glue the live loop needs;
the chat demo (and, later, chat_terminal) drive it through three calls:

    ctx, history = sess.retrieve_context(user_text)   # build prompt injection
    ...                                               # run the LLM yourself
    sess.record(user_text, assistant_text)            # store turn + maybe compact
    sess.extract()                                    # at session end

NON-BLOCKING by design: when the window overflows, the gist is recomputed on
a BACKGROUND thread and the window is trimmed only once that finishes. Until
then the full (untrimmed) window stays in the prompt — so the turn never
waits on the network and there is never a context gap. A single compaction
runs at a time; if another overflow happens first, it's picked up next turn.
"""

from __future__ import annotations

import re
import threading

from server.config import ANTI_REPEAT_LAST_REPLIES, MEMORY_EXTRACT_EVERY_TURNS
from server.memory.store import MemoryStore
from server.memory.session_log import SessionLog, ROLE_LABELS, strip_tags
from server.memory.window import RollingWindow
from server.memory.gist import update_gist
from server.memory.extractor import extract_session
from server.memory.archive import MessageArchive, is_recall_request

# Replies that carry no lasting value (the model failed to see / refused).
# Keeping them poisons the next turns: she copies them back word for word.
_FAILED_REPLY = re.compile(
    r"je\s+ne\s+(?:peux|vois|sais)\s+(?:pas|plus)\s+(?:vraiment\s+|clairement\s+)?"
    r"(?:voir|distinguer)|je\s+ne\s+vois\s+(?:pas|rien)|"
    r"d[ée]sol[ée],?\s+(?:mais\s+)?je\s+ne\s+peux\s+pas",
    re.IGNORECASE,
)


def is_failed_reply(text: str) -> bool:
    """True for a vision/refusal failure that must not be remembered."""
    return bool(_FAILED_REPLY.search(strip_tags(text or "")))


def _fmt(msgs: list[dict]) -> str:
    """Render window messages as plain text for the gist LLM. Handles the two
    Responses-API typed items (function_call / function_call_output) that ride
    in the window alongside role messages — they have no role/content."""
    lines = []
    for m in msgs:
        if "role" in m:
            lines.append(f"{ROLE_LABELS.get(m['role'], m['role'])}: {m['content']}")
        elif m.get("type") == "function_call":
            lines.append(f"tool_call: {m.get('name', '?')}({m.get('arguments', '')})")
        elif m.get("type") == "function_call_output":
            lines.append(f"tool_result: {m.get('output', '')}")
    return "\n".join(lines)


class MemorySession:
    def __init__(self, store: MemoryStore | None = None,
                 log: SessionLog | None = None,
                 window: RollingWindow | None = None,
                 enable_memory: bool = True,
                 enable_gist: bool = True):
        # enable_memory → long-term retrieval + session-end extraction.
        # enable_gist   → medium-term rolling gist + window compaction.
        # Independent so the two config flags map straight through.
        self.enable_memory = enable_memory
        self.enable_gist = enable_gist
        self.store = store or MemoryStore()
        self.archive = MessageArchive() if enable_memory else None
        self.log = log or SessionLog()
        self.window = window or RollingWindow()
        self.history: list[dict] = []
        self.gist: str = ""
        self._lock = threading.Lock()
        self._compacting = False
        self._thread: threading.Thread | None = None
        # Periodic extraction state (indexes into the raw session log lines).
        self._extracted_upto = 0
        self._turns_since_extract = 0
        self._extracting = False
        self._extract_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    def _anti_repeat_block(self, history: list[dict]) -> str:
        """Show her how her latest replies began so she varies them."""
        openers = []
        starts = []
        for m in reversed(history):
            if m.get("role") != "assistant":
                continue
            words = strip_tags(m["content"]).split()
            if words:
                openers.append("« " + " ".join(words[:8]) + " »")
                starts.append(" ".join(words[:2]).lower())
            if len(openers) == ANTI_REPEAT_LAST_REPLIES:
                break
        # Only intervene when she really repeats herself; otherwise stay silent
        # so she answers spontaneously and does not look back at her history.
        if len(starts) < 2 or len(set(starts)) == len(starts):
            return ""
        return ("Tes dernières réponses commençaient par : "
                + ", ".join(openers)
                + ". Ne recopie pas ces débuts ni leurs formules : varie, et "
                "réponds à ce qui vient d'être dit.")

    def retrieve_context(self, user_text: str) -> tuple[str, list[dict]]:
        """Return (injected_context, history_snapshot) for prompt building.

        injected_context = memory block + gist block, in the §6 format. The
        history snapshot is a copy so the caller can't race the bg trimmer.
        """
        with self._lock:
            gist = self.gist
            history = list(self.history)
        mem_block = self.store.retrieve_formatted(user_text) if self.enable_memory else ""

        parts = []
        if mem_block:
            parts.append(mem_block)
        if self.enable_gist and gist.strip():
            parts.append(f"Earlier this session:\n{gist}")
        if self.archive is not None and is_recall_request(user_text):
            seen = {strip_tags(m["content"]) for m in history if "content" in m}
            archived = self.archive.retrieve_formatted(user_text, exclude_texts=seen)
            if archived:
                parts.append(archived)
        repeat_block = self._anti_repeat_block(history)
        if repeat_block:
            parts.append(repeat_block)
        return "\n\n".join(parts), history

    def record(self, user_text: str, assistant_text: str,
               tool_items: list[dict] | None = None) -> None:
        """Append the turn to window + raw log, then maybe kick off compaction.

        `tool_items`, if given, are the Responses-API function_call /
        function_call_output items from this turn (built by
        orchestrator._build_tool_items). They sit BETWEEN the user and assistant
        messages in the verbatim window so the next turn's prompt shows the model
        its own recent tool use. They are NOT written to the long-term raw log —
        that stays a clean user/assistant transcript for memory extraction.

        A failed reply ("je ne peux pas voir…") is dropped from the window, the
        log and the archive so she never copies it back.
        """
        if is_failed_reply(assistant_text):
            print("[mem] failed/refusal reply not remembered")
            return
        with self._lock:
            self.history.append({"role": "user", "content": user_text})
            if tool_items:
                self.history.extend(tool_items)
            self.history.append({"role": "assistant", "content": assistant_text})
        if self.enable_memory:  # raw log only feeds long-term extraction
            self.log.append("user", user_text)
            self.log.append("assistant", assistant_text)
            self.archive.add("user", user_text)
            self.archive.add("assistant", assistant_text)
            self._turns_since_extract += 1
            self._maybe_extract()
        if self.enable_gist:
            self._maybe_compact()

    def history_snapshot(self) -> list[dict]:
        """Thread-safe copy of the window (for persisting while the bg trimmer runs)."""
        with self._lock:
            return list(self.history)

    # ------------------------------------------------------------------ #
    def _maybe_compact(self) -> None:
        with self._lock:
            if self._compacting or not self.window.needs_compaction(self.history):
                return
            self._compacting = True
            evict, _ = self.window.split(self.history)
        self._thread = threading.Thread(target=self._compact, args=(evict,), daemon=True)
        self._thread.start()

    def _compact(self, evict: list[dict]) -> None:
        try:
            new_gist = update_gist(self.gist, _fmt(evict))
        except Exception as e:  # never lose context on a failed gist call
            print(f"[mem] gist update failed; keeping window intact: {e}")
            with self._lock:
                self._compacting = False
            return
        with self._lock:
            self.gist = new_gist
            del self.history[: len(evict)]  # trim only the pairs we summarized
            self._compacting = False
        print(f"[mem] gist updated in background; window trimmed to {len(self.history)}")

    def wait_idle(self) -> None:
        """Block until any in-flight background compaction finishes."""
        t = self._thread
        if t is not None and t.is_alive():
            print("[mem] waiting for background gist to finish...")
            t.join()

    # ------------------------------------------------------------------ #
    def _maybe_extract(self) -> None:
        """Every N turns, mine the not-yet-processed part of the session for
        important moments in a background thread (one at a time)."""
        if self._extracting or self._turns_since_extract < MEMORY_EXTRACT_EVERY_TURNS:
            return
        self._extracting = True
        self._turns_since_extract = 0
        self._extract_thread = threading.Thread(target=self._extract_new, daemon=True)
        self._extract_thread.start()

    def _extract_new(self) -> dict:
        try:
            total = len(self.log.turns())
            text = self.log.transcript(self._extracted_upto)
            if not text.strip():
                return {"added": 0, "skipped": 0}
            result = extract_session(text, store=self.store)
            self._extracted_upto = total
            print(f"[mem] periodic extraction: {result}")
            return result
        except Exception as e:
            print(f"[mem] periodic extraction failed (will retry later): {e}")
            return {"added": 0, "skipped": 0}
        finally:
            self._extracting = False

    def extract(self) -> dict:
        """Distill the rest of the session (not yet mined) into the long-term store."""
        if not self.enable_memory:
            return {"added": 0, "skipped": 0}
        self.wait_idle()
        t = self._extract_thread
        if t is not None and t.is_alive():
            t.join()
        self._extracting = True
        return self._extract_new()


if __name__ == "__main__":
    import tempfile
    assert is_failed_reply("<expr:uncertain> Je... Je ne peux pas vraiment voir ce que tu tiens")
    assert is_failed_reply("Désolé, je ne peux pas aider")
    assert not is_failed_reply("Je vois un téléphone noir dans ta main.")

    # Offline smoke test: drive fake turns through compaction with a stubbed
    # gist (no network), assert the window trims and never starts with assistant.
    # Rebind the module global so _compact (same module) picks up the stub.
    update_gist = lambda old, lines: ((old + " | " if old else "") + lines.replace("\n", " "))[-400:]
    _tmp = tempfile.TemporaryDirectory()
    _periodic = []
    extract_session = lambda text, store=None: (_periodic.append(text) or {"added": 0, "skipped": 0})

    sess = MemorySession(window=RollingWindow(max_msgs=8, evict_chunk=4))
    sess.archive = MessageArchive(mem_dir=_tmp.name)
    sess.log = type("L", (), {"append": lambda *a: None,
                              "turns": lambda s: [],
                              "transcript": lambda s, start=0: "x"})()
    for i in range(6):
        sess.record(f"user line {i}", f"riko line {i}")
        sess.wait_idle()
    sess.record("question", "Je ne peux pas vraiment voir ce que tu tiens")
    assert not any("Je ne peux pas" in m.get("content", "") for m in sess.history), \
        "failed reply must not be remembered"
    varied = [{"role": "assistant", "content": c} for c in
              ("Je vois un téléphone", "Oh, mignon", "Il fait beau aujourd'hui")]
    assert sess._anti_repeat_block(varied) == "", "anti-repeat must stay silent when varied"
    sess.record("a", "Hum… je vois un truc")
    sess.record("b", "Hum… je vois autre chose")
    ctx, _ = sess.retrieve_context("salut")
    assert "Ne recopie pas" in ctx, "anti-repeat block missing"
    with sess._lock:
        assert sess.history[0]["role"] == "user"
        assert len(sess.history) <= 8
    assert sess.gist, "gist never built"
    print(f"[session.__main__] OK — window={len(sess.history)} gist_len={len(sess.gist)} (no network)")

    # Tool items ride in the window between user and assistant, survive
    # compaction (folded into the gist via _fmt), and never leave the window
    # starting on a non-user message.
    tool_items = [
        {"type": "function_call", "call_id": "c9", "name": "do_task", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c9", "output": "done"},
    ]
    for i in range(6, 10):
        sess.record(f"user line {i}", f"riko line {i}", tool_items=tool_items)
        sess.wait_idle()
    with sess._lock:
        assert sess.history[0]["role"] == "user", "window must start with user"
        assert any(m.get("type") == "function_call" for m in sess.history), \
            "tool items should be present in the recent window"
    print("[session.__main__] OK — tool items recorded, compacted, window valid")
