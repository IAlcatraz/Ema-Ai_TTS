"""Top-level glue: one turn of the LLM pipeline.

Wires together the LLM stream → tag parser → semantic pickers → TTS →
playback queue. A "turn" is one user message in, one assistant response
fully played out.

Flow:
  1. Build messages from history + user_text (prompt_builder).
  2. Stream the LLM via Ollama, chunk it on punctuation/length
     without splitting `<...>` tags (stream_chunker — shared chunker,
     without splitting `<...>` tags (stream_chunker — sentence enders
     only).
  3. For each chunk, parse out events (tag_parser) and dispatch:
       EXPR  → expression_picker → enqueue PlaybackItem(EXPRESSION)
       EMOTE → emote_picker      → enqueue PlaybackItem(EMOTE)
       TEXT  → submit XTTS synthesis in the background, enqueue a deferred
               PlaybackItem(TEXT) so playback order remains unchanged
  4. Wait for the playback queue to drain (audio fully spoken).
  5. Append the raw assistant text (tags included) to history and return
     the updated history. Tags are preserved in storage so the model's
     own style carries into the next turn.

The Ollama backend, both pickers, and the emote name→path map are loaded
lazily once and cached at module scope so repeated turns reuse the same
in-memory state.

Public surface:
    run_turn(user_text: str,
             history: list[dict],
             playback: OrderedPlaybackWorker,
             injected_context: str = "") -> list[dict]

`injected_context` is a free-form string (e.g. vision caption rendered as
natural language by the streaming_agent's payload_formatter) appended to
the system prompt for THIS turn only via `build_messages`. Never stored
in history.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from threading import Event, Lock
from typing import List, Mapping, Optional, Tuple
from uuid import uuid4

from server import config
from server.config import vlog
from server.llm.llm_backend import LLMBackend, OllamaBackend
from server.llm.pickers import build_emote_picker, build_expression_picker
from server.llm.prompt_builder import build_messages
from server.llm.semantic_picker import SemanticPicker
from server.llm.stream_chunker import stream_text_chunks
from server.llm.tag_parser import parse_chunk
from server.llm.text_clean import clean_for_tts
from server.playback.playback import ItemType, OrderedPlaybackWorker, PlaybackItem
from server.turn_log import log_turn

# `tts_backend` currently supports XTTS-v2 only.
def safe_synthesize(text: str, language: str = "fr") -> Optional[Tuple[str, float]]:
    """Safe wrapper around the XTTS sidecar to avoid crashing the orchestrator."""
    try:
        # The sidecar is an HTTP server; we use a POST request to /tts_to_audio
        # to get a high-quality, non-streamed clip for the playback queue.
        # NOTE: For Fast-Start, we usually use /tts_stream, but this synchronous
        # wrapper is used by the orchestrator's executor to create a Future.
        import requests
        from server.config import XTTS_BASE_URL
        url = f"{XTTS_BASE_URL}/tts_to_audio"
        payload = {"text": text, "language": language, "speaker_wav": "riko"}
        response = requests.post(url, json=payload, timeout=(15, 60))
        response.raise_for_status()

        # Save the bytes to a temporary file so the browser can fetch it
        import uuid
        from pathlib import Path
        from server.config import PROJECT_ROOT
        filename = f"tts_sync_{uuid.uuid4().hex}.wav"
        audio_path = PROJECT_ROOT / "client" / "audio" / filename
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(response.content)

        # Return the BROWSER-RELATIVE URL instead of the absolute filesystem path
        # This prevents "Not allowed to load local resource" errors in the browser.
        browser_url = f"{XTTS_BASE_URL}/tts_stream/{filename}"

        # Calculate duration
        import soundfile as sf
        import io
        info = sf.info(io.BytesIO(response.content))

        return browser_url, info.duration
    except Exception as e:
        print(f"[orch] safe_synthesize failed: {type(e).__name__}: {e} text={text!r}")
        return None

print(f"[orch] TTS backend: {config.TTS_BACKEND}")

# XTTS's model serializes inference internally. A single persistent worker
# lets synthesis overlap Ollama streaming without competing XTTS requests.
_tts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="riko-xtts")

# Lazy-initialized singletons. First call to `run_turn` pays the cost of
# loading MiniLM; subsequent calls reuse.
_components: Optional[Tuple[LLMBackend, SemanticPicker, SemanticPicker, dict]] = None


def _get_components() -> Tuple[LLMBackend, SemanticPicker, SemanticPicker, dict]:
    global _components
    if _components is None:
        vlog("[orch] initializing components (LLM backend + pickers)...")
        if config.ENABLE_FUNCTION_CALLING:
            from server.function_calling.do_task import DO_TASK_TOOL, dispatch
            from server.function_calling.openai_tool_backend import (
                OpenAIToolBackend,
            )
            backend: LLMBackend = OpenAIToolBackend(
                model=config.OPENAI_TOOL_MODEL,
                tools=[DO_TASK_TOOL],
                dispatch=dispatch,
            )
            print("[orch] function calling ENABLED — do_task tool exposed")
        else:
            backend = OllamaBackend(
                model=config.OLLAMA_MODEL,
                base_url=config.OLLAMA_BASE_URL,
            )
        expr_picker = build_expression_picker()
        emote_picker, emote_name_to_rel_url = build_emote_picker()
        _components = (backend, expr_picker, emote_picker, emote_name_to_rel_url)
        vlog("[orch] components ready")
    return _components


def reload_emote_picker() -> int:
    """Re-scan the animation folders and rebuild the emote picker in place.

    Call this after dropping new `.fbx` / `.vrma` files into any of the
    folders in `config.ANIMATION_SOURCES` — the new files become pickable
    without restarting the process. Returns the new pool size.

    Note: the MiniLM model itself stays cached (in `semantic_picker`), so
    only the candidate-name embeddings are recomputed. Typically takes
    well under a second.
    """
    global _components
    if _components is None:
        # No components built yet — initial build will pick up new files anyway.
        _get_components()
        return len(_components[3])  # type: ignore[index]

    backend, expr_picker, _old_emote_picker, _old_n2u = _components
    print("[orch] reloading emote picker (re-scanning animation folders)...")
    new_emote_picker, new_n2u = build_emote_picker()
    _components = (backend, expr_picker, new_emote_picker, new_n2u)
    print(f"[orch] reloaded — pool size now {len(new_n2u)}")
    return len(new_n2u)


def _build_tool_items(tool_calls: List[dict]) -> List[dict]:
    """Turn this turn's recorded tool calls into Responses-API history items.

    Each recorded call becomes a (function_call, function_call_output) PAIR that
    shares a short synthetic `call_id`. We keep `call_id` because the Responses
    API rejects a function_call_output whose call_id has no matching
    function_call in the same input — it is one field that MUST survive. We
    drop the server-assigned `id` (never needed on input). Persisting these
    between the user and assistant messages shows the model its own recent tool
    use, which keeps it from "forgetting" it has tools after several turns.

    Empty in -> empty out, so a turn with no tool call is byte-identical to
    before (nothing is appended to history).
    """
    items: List[dict] = []
    for tc in tool_calls:
        cid = "call_" + uuid4().hex[:20]
        items.append({
            "type": "function_call",
            "call_id": cid,
            "name": tc["name"],
            "arguments": json.dumps(tc.get("arguments", {})),
        })
        items.append({
            "type": "function_call_output",
            "call_id": cid,
            "output": str(tc.get("output", "")),
        })
    return items


def run_turn(
    user_text: str,
    history: List[Mapping[str, str]],
    playback: OrderedPlaybackWorker,
    injected_context: str = "",
    image_path: str = "",
    interrupt_event: Event | None = None,
) -> List[dict]:
    """Drive one full turn end-to-end. Returns the updated history list.

    `history` is not mutated; a new list is returned (caller decides whether
    to persist it). The raw assistant text — TAGS INCLUDED — is appended so
    the model's own emote/expression style carries forward.

    `injected_context`, if non-empty, is appended to the base system prompt
    for this single turn (per-turn modality context like vision). Never
    written to history — see prompt_builder.build_messages for the wiring.

    `image_path`, if non-empty, is described by the local vision model before
    the chat model is called. History stores a plain-text marker; the image
    and generated description are never persisted.
    """
    backend, expr_picker, emote_picker, emote_name_to_rel_url = _get_components()

    vlog(f"\n[orch] === run_turn ===  user_text={user_text!r}"
          f" injected_context={'yes' if injected_context else 'no'}"
          f" image={image_path or 'no'}")

    # Function calling on: prepend the do_task usage hint to the per-turn
    # system context. build_messages folds injected_context into the system
    # prompt; it is never written to history.
    if config.ENABLE_FUNCTION_CALLING:
        from server.function_calling.do_task import DO_TASK_TOOL, dispatch as do_task_dispatch
        from server.function_calling.robot_dispatcher import dispatch_robot_tool

        # Load robot tools if enabled
        tools = [DO_TASK_TOOL]
        if config.ENABLE_ROBOT_TOOLS:
            import json
            from pathlib import Path
            from server.config import PROJECT_ROOT
            robot_tools_path = Path(r"C:\Users\crypt\Downloads\Robot_Patreon\Robot_Data\tool_calls.json")
            if robot_tools_path.exists():
                with open(robot_tools_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    tools.extend(data.get("tools", []))
                    print(f"[orch] loaded {len(data.get('tools', []))} robot tools")

        def master_dispatch(tool_call):
            name = tool_call["name"]
            args = tool_call.get("arguments", {})

            # Route to robot dispatcher if it's a robot tool
            if config.ENABLE_ROBOT_TOOLS:
                # We can identify robot tools by their presence in tool_calls.json
                # or just by routing any non-do_task tool to the robot
                if name != "do_task":
                    return dispatch_robot_tool(name, args)

            return do_task_dispatch(tool_call)

        from server.function_calling.openai_tool_backend import (
            OpenAIToolBackend,
        )
        backend: LLMBackend = OpenAIToolBackend(
            model=config.OPENAI_TOOL_MODEL,
            tools=tools,
            dispatch=master_dispatch,
        )
        print(f"[orch] function calling ENABLED — {len(tools)} tools exposed")
    else:
        backend = OllamaBackend(
            model=config.OLLAMA_MODEL,
            base_url=config.OLLAMA_BASE_URL,
        )

    # Chunks come pre-stripped from the chunker, so a naive concat collapses
    # the sentence boundary spacing ("time?That's..."). Re-insert a single
    # space between chunks for the history record.
    chunk_parts: list[str] = []
    chunk_count = 0
    # Turn timing for logs/turns_<date>.jsonl. ttft is measured to the first
    # SPEAKABLE chunk, not the first token — that is the boundary that actually
    # gates TTS, and it is the number to look at when the stream feels stalled.
    t_start = time.perf_counter()
    t_first_token = 0.0
    t_first_chunk = 0.0
    t_first_audio_ready_ms: Optional[int] = None
    first_audio_lock = Lock()
    tts_jobs: list[Future[Optional[Tuple[str, float]]]] = []

    def mark_first_token(timestamp: float) -> None:
        nonlocal t_first_token
        t_first_token = timestamp

    def mark_audio_ready(
        future: Future[Optional[Tuple[str, float]]],
    ) -> None:
        nonlocal t_first_audio_ready_ms
        try:
            result = future.result()
        except CancelledError:
            return
        except Exception as e:
            print(
                f"[orch] XTTS worker failed: {type(e).__name__}: {e}"
            )
            return
        if result is None:
            return
        ready_ms = round((time.perf_counter() - t_start) * 1000)
        with first_audio_lock:
            if t_first_audio_ready_ms is None:
                t_first_audio_ready_ms = ready_ms
            else:
                return
        print(f"[orch] first_audio_ready={ready_ms}ms")

    for chunk in stream_text_chunks(
        backend, messages, on_first_token=mark_first_token,
    ):
        if interrupt_event is not None and interrupt_event.is_set():
            print("[orch] response interrupted; stopping generation")
            break
        chunk_count += 1
        if chunk_count == 1:
            t_first_chunk = time.perf_counter()
        chunk_parts.append(chunk)
        events = parse_chunk(chunk)

        for kind, value in events:
            if kind == "EXPR":
                name, _ = expr_picker.pick(value)
                playback.enqueue(PlaybackItem(
                    item_type=ItemType.EXPRESSION,
                    name=name,
                ))
            elif kind == "EMOTE":
                name, _ = emote_picker.pick(value)
                # `emote_name_to_rel_url[name]` is already the browser-relative
                # path string (e.g. "animations/vrma/Spin.vrma" or
                # "animations/mixamo/Backflip.fbx") — ready to drop into the
                # /animate payload. See server/vrm/vrm_client.py module
                # docstring for why this is relative not absolute.
                playback.enqueue(PlaybackItem(
                    item_type=ItemType.EMOTE,
                    animate_url=emote_name_to_rel_url[name],
                ))
            elif kind == "TEXT":
                # Single pre-clean step before TTS + subtitle. See
                # server/llm/text_clean.py for the rule list. SoVITS still
                # returns 400 on punctuation-only input, so we also skip
                # anything with no letters/digits AFTER cleaning.
                cleaned = clean_for_tts(value)
                if not any(c.isalnum() for c in cleaned):
                    print(f"[orch] skip TEXT (no speakable content): {value!r}")
                    continue
                audio_future = _tts_executor.submit(safe_synthesize, cleaned)
                tts_jobs.append(audio_future)
                audio_future.add_done_callback(mark_audio_ready)
                playback.enqueue(PlaybackItem(
                    item_type=ItemType.TEXT,
                    audio_future=audio_future,
                    text=cleaned,
                ))

    if interrupt_event is not None and interrupt_event.is_set():
        for job in tts_jobs:
            job.cancel()

    if interrupt_event is None or not interrupt_event.is_set():
        playback.finish_turn()
    t_stream_end = time.perf_counter()
    first_token_ms = round((t_first_token - t_start) * 1000) if t_first_token else None
    ttft_ms  = round((t_first_chunk - t_start) * 1000) if t_first_chunk else None
    total_ms = round((t_stream_end - t_start) * 1000)
    usage = dict(getattr(backend, "last_turn_usage", {}) or {})
    audio_ready_label = (
        f"{t_first_audio_ready_ms}ms"
        if t_first_audio_ready_ms is not None
        else "pending"
    )

    full_assistant_text = " ".join(chunk_parts)
    print(
        f"[orch] stream complete chunks={chunk_count} "
        f"raw_len={len(full_assistant_text)} "
        f"first_token={first_token_ms}ms first_chunk={ttft_ms}ms "
        f"first_audio_ready={audio_ready_label} "
        f"total={total_ms}ms "
        f"cached_tokens={usage.get('cached_tokens', '?')}"
    )

    vlog("[orch] waiting for playback queue to drain...")
    playback.wait_until_finished()
    vlog("[orch] playback finished")

    # Write the full structured turn to logs/turns_<date>.jsonl for later
    # fine-tuning / debugging. This is SEPARATE from chat_history.json (which
    # only stores her spoken words). Wrapped in try so a logging failure
    # never crashes the turn.
    try:
        log_turn({
            "function_calling": config.ENABLE_FUNCTION_CALLING,
            "system": (messages[0]["content"]
                       if messages and messages[0].get("role") == "system"
                       else ""),
            "history_in": [m for m in history if m.get("role") != "system"],
            "user": user_text,
            "image_path": image_path or "",
            "injected_context": injected_context or "",
            "tool_calls": list(getattr(backend, "last_turn_tool_calls", [])),
            "assistant": full_assistant_text,
            # Timings + token counts. `cached_tokens` is the one to read for
            # how much of the prompt was cached; `ttft_ms` / `total_ms`
            # are for diagnosing a stalling stream with data instead of vibes.
            "first_token_ms": first_token_ms,
            "ttft_ms": ttft_ms,
            "first_audio_ready_ms": t_first_audio_ready_ms,
            "total_ms": total_ms,
            "chunk_count": chunk_count,
            **usage,
        })
    except Exception as e:
        print(f"[orch] turn_log failed (non-fatal): {type(e).__name__}: {e}")

    # Append the raw assistant text (tags included) so the next turn sees the
    # model's own style. Defensive copy of history — we never mutate input.
    # Order matches the Responses API: user -> [tool call/output pairs] ->
    # assistant. The tool items are only present when a tool actually ran.
    updated: List[dict] = list(history)
    if image_path:
        # Persist text + marker, never the image itself (token saver).
        from server.llm.image_attach import history_text
        updated.append({"role": "user", "content": history_text(user_text)})
    else:
        updated.append({"role": "user", "content": user_text})
    tool_items = _build_tool_items(getattr(backend, "last_turn_tool_calls", []))
    if tool_items:
        updated.extend(tool_items)
        print(f"[orch] persisted {len(tool_items) // 2} tool call(s) to history")
    updated.append({"role": "assistant", "content": full_assistant_text})
    print(f"[orch] history now has {len(updated)} message(s)")
    return updated


if __name__ == "__main__":
    # Standalone test; the configured TTS service must be running.
    # Run with: python -m server.llm.orchestrator
    user_msg = "Do a backflip and say something cheeky to me."
    print(f"[orch.__main__] hardcoded user message: {user_msg!r}\n")

    worker = OrderedPlaybackWorker()
    worker.start()
    try:
        updated_history = run_turn(user_msg, history=[], playback=worker)
    finally:
        worker.stop()

    print("\n[orch.__main__] === FINAL HISTORY ===")
    for i, m in enumerate(updated_history):
        content = m["content"]
        if isinstance(content, str) and len(content) > 200:
            content = content[:200] + "..."
        print(f"  [{i}] {m['role']:>9}: {content!r}")

    assert updated_history[-1]["role"] == "assistant", "last msg should be assistant"
    assert updated_history[-2]["role"] == "user", "second-to-last should be user"
    print("\n[orch.__main__] OK — turn completed end-to-end")
