"""Streaming LLM backends with a shared text-delta interface."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Iterable, Iterator, List, Mapping

import requests

from server.config import vlog


def accumulate_usage(sink: dict, new: dict) -> None:
    """Sum `new` into `sink` in place. The tool loop makes one API call per
    round, so a turn with a tool call has more than one usage block."""
    for k, v in new.items():
        if v is None:
            continue
        sink[k] = (sink.get(k) or 0) + v


def _without_thinking(chunks: Iterable[str]) -> Iterator[str]:
    """Drop Qwen-style <think> blocks, including markers split across chunks."""
    opening, closing = "<think>", "</think>"
    buffer = ""
    thinking = False

    for chunk in chunks:
        buffer += chunk
        while buffer:
            marker = closing if thinking else opening
            index = buffer.find(marker)
            if index >= 0:
                if not thinking and index:
                    yield buffer[:index]
                buffer = buffer[index + len(marker):]
                thinking = not thinking
                continue

            keep = min(len(marker) - 1, len(buffer))
            while keep and not marker.startswith(buffer[-keep:]):
                keep -= 1
            if thinking:
                buffer = buffer[-keep:] if keep else ""
            else:
                safe_length = len(buffer) - keep
                if safe_length:
                    yield buffer[:safe_length]
                buffer = buffer[safe_length:]
            break

    if not thinking and buffer:
        yield buffer


def _to_ollama_message(message: Mapping) -> dict:
    """Convert Responses-style content arrays to Ollama's text + images."""
    content = message.get("content")
    if not isinstance(content, list):
        return dict(message)
    texts: List[str] = []
    images: List[str] = []
    for part in content:
        kind = part.get("type")
        if kind in ("input_text", "text"):
            texts.append(part.get("text", ""))
        elif kind == "input_image":
            url = part.get("image_url", "")
            images.append(url.split(",", 1)[1] if url.startswith("data:") else url)
    converted = {k: v for k, v in message.items() if k != "content"}
    converted["content"] = "\n".join(t for t in texts if t)
    if images:
        converted["images"] = images
    return converted


class LLMBackend(ABC):
    """Abstract streaming LLM backend.

    `stream(messages)` must yield raw text deltas (str fragments) in order.
    The chunker upstream is responsible for buffering / boundary detection;
    backends do not return punctuation-aligned chunks.
    """

    @abstractmethod
    def stream(self, messages: Iterable[Mapping[str, str]]) -> Iterator[str]:
        raise NotImplementedError


class OllamaBackend(LLMBackend):
    """Ollama's local streaming chat API, with thinking disabled for speech."""

    def __init__(
        self,
        model: str,
        base_url: str,
        temperature: float = 1.0,
        top_p: float = 0.95,
        repeat_penalty: float = 1.15,
        max_output_tokens: int = 512,
        keep_alive: str = "30m",
    ):
        self.endpoint = base_url.rstrip("/") + "/api/chat"
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.repeat_penalty = repeat_penalty
        self.max_output_tokens = max_output_tokens
        self.keep_alive = keep_alive
        self.last_turn_usage: dict = {}
        print(f"[llm_backend] OllamaBackend ready model={self.model} "
              f"url={self.endpoint}")

    def stream(self, messages: Iterable[Mapping[str, str]]) -> Iterator[str]:
        msgs: List[Mapping] = [_to_ollama_message(m) for m in messages]
        self.last_turn_usage = {}
        vlog(f"[llm_backend] Ollama stream start messages={len(msgs)}")

        def response_chunks() -> Iterator[str]:
            with requests.post(
                self.endpoint,
                json={
                    "model": self.model,
                    "messages": msgs,
                    "stream": True,
                    "think": False,
                    "keep_alive": self.keep_alive,
                    "options": {
                        "temperature": self.temperature,
                        "top_p": self.top_p,
                        "repeat_penalty": self.repeat_penalty,
                        "num_predict": self.max_output_tokens,
                    },
                },
                stream=True,
                timeout=(5, 600),
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    event = json.loads(line)
                    content = (event.get("message") or {}).get("content")
                    if content:
                        yield content
                    if event.get("done"):
                        self.last_turn_usage = {
                            "input_tokens": event.get("prompt_eval_count"),
                            "output_tokens": event.get("eval_count"),
                            "cached_tokens": 0,
                            "model_load_ms": _nanoseconds_to_ms(
                                event.get("load_duration")
                            ),
                            "prompt_eval_ms": _nanoseconds_to_ms(
                                event.get("prompt_eval_duration")
                            ),
                            "generation_ms": _nanoseconds_to_ms(
                                event.get("eval_duration")
                            ),
                        }

        yield from _without_thinking(response_chunks())
        vlog("[llm_backend] Ollama stream end")


def _nanoseconds_to_ms(value: int | None) -> float | None:
    """Convert Ollama's nanosecond duration fields to milliseconds."""
    return round(value / 1_000_000, 1) if value is not None else None
