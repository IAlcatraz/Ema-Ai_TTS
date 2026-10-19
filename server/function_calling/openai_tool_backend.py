"""openai_tool_backend.py — a tool-calling LLM backend for the v2 pipeline.

`OpenAIToolBackend` implements `LLMBackend`: its `stream(messages)` yields a
flat stream of text deltas while internally running the Responses-API tool
loop. It is used only when optional function calling is enabled; ordinary chat
uses Ollama.

Because the OUTPUT shape is identical (an iterator of text deltas), the stream
chunker, tag parser, and playback worker downstream need no changes. When
`config.ENABLE_FUNCTION_CALLING` is on, the orchestrator picks this backend
instead of the regular Ollama backend.

The backend is tool-agnostic — the tool schemas and the dispatch function are
injected by the caller, so this file never imports `do_task` directly.
"""

from __future__ import annotations

import os
from typing import Iterable, Iterator, List, Mapping

from dotenv import load_dotenv
from openai import OpenAI

from server.function_calling.tool_loop import ToolDispatch, stream_tool_turn
from server.llm.llm_backend import LLMBackend

from server.config import vlog


class OpenAIToolBackend(LLMBackend):
    """OpenAI Responses-API backend used for optional function calling.

    Yields text deltas while running tools transparently in between.
    """

    def __init__(
        self,
        model: str,
        tools: List[dict],
        dispatch: ToolDispatch,
        *,
        max_output_tokens: int = 512,
        max_rounds: int = 5,
    ):
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY not set in environment or .env file."
            )
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.tools = list(tools)
        self.dispatch = dispatch
        self.max_output_tokens = max_output_tokens
        self.max_rounds = max_rounds
        # Tool calls made during the most recent `stream()` — the orchestrator
        # reads this after streaming to write the per-turn log. Reset on each
        # call so old calls never leak into a later turn.
        self.last_turn_tool_calls: List[dict] = []
        tool_names = [t.get("name", "?") for t in self.tools]
        print(f"[tool_backend] OpenAIToolBackend ready model={model} "
              f"tools={tool_names}")

    def stream(self, messages: Iterable[Mapping[str, str]]) -> Iterator[str]:
        msgs: List = list(messages)
        # Reset and record tool calls for this turn via a wrapper around the
        # caller-supplied dispatch — purely additive, the result the model
        # sees is whatever the original dispatch returned.
        self.last_turn_tool_calls = []
        self.last_turn_usage: dict = {}
        original_dispatch = self.dispatch

        def recording_dispatch(name: str, args: dict) -> str:
            result = original_dispatch(name, args)
            self.last_turn_tool_calls.append({
                "name": name,
                "arguments": dict(args),
                "output": str(result),
            })
            return result

        vlog(f"[tool_backend] stream start messages={len(msgs)}")
        yield from stream_tool_turn(
            self.client, self.model, msgs, self.tools, recording_dispatch,
            max_output_tokens=self.max_output_tokens,
            max_rounds=self.max_rounds,
            usage_sink=self.last_turn_usage,
        )
        vlog(f"[tool_backend] stream end "
              f"(recorded {len(self.last_turn_tool_calls)} tool call(s))")
