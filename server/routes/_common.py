"""Shared state + helpers for route modules.

This is the single source of truth for:
  - `active_connections`  — main WS clients (`/ws`) receiving VRM commands
  - `status_connections`  — status-page WS clients (`/ws_status`)
  - `latest_uploaded_image` — set by `/upload-image/` (currently unwired —
    a relic of v1, will be revived in the UI-wiring milestone)
  - `_vr_position`        — last-known VR headset pose
  - `notify_clients`      — broadcast a dict to every `/ws` client
  - `broadcast_status`    — broadcast a client-count update to `/ws_status`
  - `send_transcription_result` — push transcription text to `/ws` clients
    (the client at `ui.js:24` listens for this; not yet emitted by any route
    in v2, but kept here so wiring the voice input back up is a one-import job)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket


logger = logging.getLogger(__name__)

# Active WS connections — the VRM client(s) that receive /talk, /animate, etc.
active_connections: Set[WebSocket] = set()
# Status-page WS connections — receive the live connection count.
status_connections: Set[WebSocket] = set()

# Picture trigger workflow shares this slot between routes (set by upload,
# polled by /take_picture). Set by routes/browser_input.py `/upload-image/`.
latest_uploaded_image: Dict[str, Any] = {"path": None}

# Browser-input buffers (routes/browser_input.py). Written by the upload
# endpoints, polled by the streaming agent's BrowserVoiceInput (a different
# process) via GET /pending_voice + /pending_text. `version` is a monotonic
# counter — the poller fires on change, so repeated GETs are idempotent and
# an agent restart doesn't replay old input.
# `image_path` rides along on BOTH buffers when the client staged a picture
# before sending — the agent passes it to the local vision model for a
# description. Same key on both so the consumer reads it the same way whether
# the turn was spoken or typed.
# Latest webcam frame (POST /vision/frame). The agent asks for it only when a
# message looks like a vision request; `timestamp` lets it reject stale frames.
latest_webcam_frame: Dict[str, Any] = {"path": None, "timestamp": 0.0}

pending_voice: Dict[str, Any] = {"version": 0, "path": None, "image_path": None}
pending_text: Dict[str, Any] = {"version": 0, "text": None, "image_path": None}

# Click/poke on the avatar (client/interaction). Same version-counter contract.
pending_poke: Dict[str, Any] = {"version": 0, "region": None, "bone": None, "timestamp": 0.0}

# Last-known VR headset pose (POSTed by client/vr/vrPositionTracker.js).
_vr_position: Dict[str, float] = {
    "x": 0.0, "y": 0.0, "z": 0.0,
    "rx": 0.0, "ry": 0.0, "rz": 0.0,
    "timestamp": 0.0,
}


async def notify_clients(message: dict) -> None:
    """Broadcast `message` as JSON to every active `/ws` client."""
    if not active_connections:
        logger.info("No clients connected; skipping notify.")
        return
    data = json.dumps(message)
    logger.info(f"Broadcasting to {len(active_connections)} client(s): {data}")
    coros = [ws.send_text(data) for ws in list(active_connections)]
    results = await asyncio.gather(*coros, return_exceptions=True)
    for ws, res in zip(list(active_connections), results):
        if isinstance(res, Exception):
            logger.error(f"Failed to send to {ws.client}: {res}")
            active_connections.discard(ws)


async def broadcast_status(count: int) -> None:
    """Tell every `/ws_status` listener the current client count."""
    if not status_connections:
        return
    msg = json.dumps({"type": "count_update", "count": count})
    coros = [ws.send_text(msg) for ws in list(status_connections)]
    await asyncio.gather(*coros, return_exceptions=True)


async def send_transcription_result(text: str) -> None:
    """Push a transcription string to every `/ws` client.

    Client handler: `client/ui.js:24` displays this in the text-input box
    for review/edit before send. Currently not emitted anywhere in v2 —
    revive when wiring voice-input through the orchestrator pipeline.
    """
    await notify_clients({"type": "transcription_result", "text": text})
