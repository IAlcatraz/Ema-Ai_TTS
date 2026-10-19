"""Browser input routes — the web page's mic button, text box and image upload.

The browser (client/ui.js) and the streaming agent (chat_terminal.py) are
different processes that only share HTTP, so uploads land here on the
server's port and
sit in `_common.py` buffers until the agent's BrowserVoiceInput polls them.

  POST /upload-audio/   — save the mic-button clip to a shared temp path,
                          bump `pending_voice` {version, path, image_path}.
                          Optional `image` part rides along (voice + picture).
  GET  /pending_voice   — polled by BrowserVoiceInput (~0.5s)
  POST /send_message    — buffer text from the UI box, bump `pending_text`
  GET  /pending_text    — polled by BrowserVoiceInput (~0.5s)
  POST /upload-image/   — save to uploads/browser_image.<ext> + set
                          `latest_uploaded_image` (revives /take_picture).
                          Optional `text` field is routed via pending_text
                          so the message still reaches the agent.
  POST /vision/frame    — raw JPEG webcam frame; only the newest is kept.
  GET  /vision/latest   — path + age of that frame for the agent.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile

from server.config import UPLOADS_DIR
from server.routes._common import (
    latest_uploaded_image,
    latest_webcam_frame,
    pending_poke,
    pending_text,
    pending_voice,
)
from server.routes.schemas import ClickInteractionRequest, SendMessageRequest


logger = logging.getLogger(__name__)
router = APIRouter()


async def _save_image(file: UploadFile) -> str:
    """Write an uploaded picture to the one stable path and return it.

    One filename (uploads/browser_image.<ext>) so the base registry's
    analyze_images tool — which scans ALL of uploads/ — never sees a backlog.
    Also fills `latest_uploaded_image`, which un-blocks /take_picture.
    """
    data = await file.read()
    ext = Path(file.filename or "image.jpg").suffix or ".jpg"
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS_DIR / f"browser_image{ext}"
    dest.write_bytes(data)
    latest_uploaded_image["path"] = str(dest)
    logger.info(f"[browser-input] image saved: {file.filename} "
                f"({len(data)} bytes) -> {dest}")
    return str(dest)


@router.post("/upload-audio/")
async def upload_audio(file: UploadFile = File(...),
                       image: Optional[UploadFile] = File(None)) -> dict:
    """Receive a full voice clip from the browser mic button.

    Saved to one stable temp path (extension follows the upload — webm from
    Chrome, mp4 from Safari). The agent process reads this same path, so it
    must be a location both processes can see (/tmp is, on WSL).

    `image` is optional and mirrors the `text` field on /upload-image/: it lets
    a staged picture ride along with a VOICE memo, so "here, look at this" can
    be spoken instead of typed. Sending them in ONE request keeps the pairing
    atomic — no server-side tray that could attach a forgotten image to an
    unrelated turn later.
    """
    data = await file.read()
    ext = Path(file.filename or "recording.webm").suffix or ".webm"
    dest = Path(tempfile.gettempdir()) / f"riko_browser_voice{ext}"
    dest.write_bytes(data)

    image_path = await _save_image(image) if image is not None else None

    pending_voice["version"] += 1
    pending_voice["path"] = str(dest)
    pending_voice["image_path"] = image_path
    logger.info(
        f"[browser-input] audio received: {file.filename} ({len(data)} bytes) "
        f"-> {dest} (voice v{pending_voice['version']}, image={image_path})"
    )
    return {"status": "received", "bytes": len(data),
            "version": pending_voice["version"], "path": str(dest),
            "image_path": image_path}


@router.get("/pending_voice")
async def get_pending_voice() -> dict:
    """Polled by the agent's BrowserVoiceInput. Fires on version change."""
    return dict(pending_voice)


@router.post("/send_message")
async def send_message(req: SendMessageRequest) -> dict:
    """Buffer a typed message from the UI text box for the agent."""
    pending_text["version"] += 1
    pending_text["text"] = req.message_text
    pending_text["image_path"] = None
    logger.info(
        f"[browser-input] text received: {req.message_text!r} "
        f"(text v{pending_text['version']})"
    )
    return {"status": "received", "version": pending_text["version"]}


@router.get("/pending_text")
async def get_pending_text() -> dict:
    """Polled by the agent's BrowserVoiceInput. Fires on version change."""
    return dict(pending_text)


@router.post("/send_click_interaction")
async def send_click_interaction(req: ClickInteractionRequest) -> dict:
    """The user clicked/poked the avatar; the agent reacts to it."""
    pending_poke["version"] += 1
    pending_poke["region"] = req.region
    pending_poke["bone"] = req.bone
    pending_poke["timestamp"] = time.time()
    logger.info(f"[browser-input] poke: {req.region} (v{pending_poke['version']})")
    return {"status": "received", "version": pending_poke["version"]}


@router.get("/pending_poke")
async def get_pending_poke() -> dict:
    """Polled by the agent's BrowserVoiceInput. Fires on version change."""
    return dict(pending_poke)


@router.post("/upload-image/")
async def upload_image(file: UploadFile = File(...),
                       text: Optional[str] = Form(None)) -> dict:
    """Receive an image (+ optional text) from the browser picker.

    ALWAYS bumps pending_text (even with no text) so an image-only upload
    still triggers a turn: BrowserVoiceInput emits the message with
    `image_path` metadata, and run_turn sends the image to the configured
    local vision model for a description before generating the chat reply.

    For image + VOICE, the picture goes to /upload-audio/ instead — one
    request, one turn.
    """
    dest = await _save_image(file)

    pending_text["version"] += 1
    pending_text["text"] = text or ""
    pending_text["image_path"] = dest
    logger.info(
        f"[browser-input] image queued as a turn (text={text!r}, "
        f"text v{pending_text['version']})"
    )
    return {"status": "received", "saved_to": dest,
            "text": text, "version": pending_text["version"]}


@router.post("/vision/frame")
async def vision_frame(request: Request) -> dict:
    """Keep only the newest raw-JPEG webcam frame sent by the video button."""
    data = await request.body()
    if not data:
        return {"status": "empty"}
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS_DIR / "webcam_frame.jpg"
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)  # atomic, so the agent never reads a half-written frame
    latest_webcam_frame["path"] = str(dest)
    latest_webcam_frame["timestamp"] = time.time()
    return {"status": "received", "bytes": len(data)}


@router.get("/vision/latest")
async def vision_latest() -> dict:
    """Polled by the agent: newest webcam frame path and its age in seconds."""
    path = latest_webcam_frame["path"]
    age = time.time() - latest_webcam_frame["timestamp"] if path else None
    return {"path": path, "age_s": age}


if __name__ == "__main__":
    print("[browser-input] this is a route module — mount via server.server.")
    print("  verify with:")
    from server.config import SERVER_PORT
    print(f"    curl -F file=@sample.webm 127.0.0.1:{SERVER_PORT}/upload-audio/")
    print(f"    curl 127.0.0.1:{SERVER_PORT}/pending_voice")
