"""Backend seam for the memory subsystem.

Two helpers, three swappable backends. This is the ONLY place memory talks
to an LLM, so the rest of the package is backend-agnostic.

  complete_json(messages, schema, backend, model) -> a `schema` instance
  complete_text(messages, backend, model)         -> str

Backends:
  "openai"  — optional OpenAI Responses API backend. Requires OPENAI_API_KEY.
  "local"   — any OpenAI-compatible server you run yourself (LM Studio,
              llama-server, Ollama). The default: private and needs no key;
              set MEMORY_LOCAL_BASE_URL + MEMORY_LOCAL_MODEL in config.

The local backend uses chat-completions because that is what compatible local
servers expose. Character chat uses Ollama's local chat API separately.
"""

from __future__ import annotations

import json
import os
from typing import Type, TypeVar

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

from server.config import (
    MEMORY_LOCAL_BASE_URL,
)

load_dotenv()

T = TypeVar("T", bound=BaseModel)

# One cached client per backend.
_clients: dict[str, OpenAI] = {}


def _client(backend: str) -> OpenAI:
    if backend not in _clients:
        if backend == "openai":
            _clients[backend] = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        elif backend == "local":
            # llama-server ignores the key but the SDK requires a non-empty one.
            _clients[backend] = OpenAI(base_url=MEMORY_LOCAL_BASE_URL, api_key="local")
        else:
            raise ValueError(f"[mem-llm] unknown backend {backend!r}")
    return _clients[backend]


def _ollama_chat(messages: list[dict], model: str, json_schema: dict | None = None) -> str:
    """Native Ollama call with the same keep_alive/think as the chat backend,
    so memory work never evicts or reloads the chat model."""
    import requests

    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "keep_alive": "30m",
    }
    if json_schema is not None:
        body["format"] = json_schema
    url = MEMORY_LOCAL_BASE_URL.rstrip("/").removesuffix("/v1") + "/api/chat"
    r = requests.post(url, json=body, timeout=(5, 600))
    r.raise_for_status()
    return r.json()["message"]["content"]


def complete_json(messages: list[dict], schema: Type[T], backend: str, model: str) -> T:
    """Return a validated `schema` instance from the chosen backend."""
    print(f"[mem-llm] complete_json backend={backend!r} model={model!r} schema={schema.__name__}")
    client = _client(backend)

    if backend == "openai":
        resp = client.responses.parse(model=model, input=messages, text_format=schema)
        return resp.output_parsed

    # local: ask for JSON matching the schema, then validate what comes back.
    raw = _ollama_chat(messages, model, schema.model_json_schema())
    return schema.model_validate(json.loads(raw))


def complete_text(messages: list[dict], backend: str, model: str) -> str:
    """Return a plain text completion from the chosen backend."""
    print(f"[mem-llm] complete_text backend={backend!r} model={model!r}")
    client = _client(backend)

    if backend == "openai":
        resp = client.responses.create(model=model, input=messages)
        return resp.output_text.strip()

    return _ollama_chat(messages, model).strip()


if __name__ == "__main__":
    # Offline structural checks only — no network.
    from server.memory.schema import MemoryList

    assert "memories" in MemoryList.model_json_schema()["properties"]
    try:
        _client("bogus")
        raise SystemExit("[mem-llm.__main__] FAIL — bogus backend should raise")
    except ValueError:
        pass
    print("[mem-llm.__main__] OK — schema serialises, unknown backend rejected (no network)")
