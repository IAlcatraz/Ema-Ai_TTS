"""Attach a fresh webcam frame to messages that ask Riko to look.

The browser's video button streams frames to the server (POST /vision/frame).
Inputs call `webcam_frame_for(text)` when they emit a message; only requests
that sound like "what do you see?" get a frame, so ordinary turns never pay
the vision-model cost.
"""
import json
import logging
import re
import urllib.request

from server.config import BROWSER_INPUT_URL

log = logging.getLogger(__name__)

# Frames older than this are ignored (camera stopped or stream stalled).
WEBCAM_MAX_AGE_S = 5.0

_VISION_REQUEST = re.compile(
    r"\b(?:que\s+vois[\s-]*tu|qu['’]?\s*est[\s-]*ce\s+que\s+tu\s+vois|"
    r"tu\s+vois|vois[\s-]*tu|regarde[rz]?|regardes|montre|"
    r"cam[ée]ra|webcam|devant\s+toi|sur\s+(?:cette\s+)?image|"
    r"ce\s+que\s+je\s+tiens|qu['’]?\s*est[\s-]*ce\s+que\s+je\s+tiens|"
    r"tiens[\s-]*je|dans\s+(?:ma|mes|la)\s+(?:main|mains|paume)|"
    r"objet\s+(?:dans|que\s+je\s+tiens)|ai[\s-]*je\s+(?:dans|sur)|"
    r"je\s+(?:porte|montre|te\s+montre|tiens)|d[ée]cris(?:[\s-]*moi)?|"
    r"c['’]?\s*est\s+quoi|qu['’]?\s*est[\s-]*ce\s+que\s+(?:c['’]?\s*est|j['’]?\s*ai)|"
    r"à\s+quoi\s+(?:je\s+ressemble|ça\s+ressemble)|"
    r"comment\s+(?:suis[\s-]*je|je\s+suis)\s+(?:habill|coiff)|"
    r"look\s+at|what\s+do\s+you\s+see|can\s+you\s+see)\b",
    re.IGNORECASE,
)


_SCREEN_REQUEST = re.compile(
    r"(?:\b(?:mon|ton|cet|l['’]?)\s*[ée]cran|\bsur\s+mon\s+(?:pc|ordi(?:nateur)?)|"
    r"\bmes\s+(?:fen[êe]tres|onglets)|\bma\s+fen[êe]tre|"
    r"\bquel(?:le)?\s+(?:application|appli|programme|site|jeu)\b|"
    r"\bmy\s+screen|\bon\s+the\s+screen)",
    re.IGNORECASE,
)


def is_screen_request(text: str) -> bool:
    """True when the message asks Riko to look at the computer screen."""
    return bool(_SCREEN_REQUEST.search(text or ""))


def is_vision_request(text: str) -> bool:
    """True when the message asks Riko to look at the camera view."""
    return bool(_VISION_REQUEST.search(text or ""))


def webcam_frame_for(text: str, base_url: str = BROWSER_INPUT_URL,
                     timeout: float = 2.0) -> str:
    """Path of a fresh image if `text` asks Riko to look, else ''.

    A request about the screen gets a screenshot of the desktop; any other
    "what do you see?" gets the latest webcam frame."""
    if is_screen_request(text):
        from server.streaming_agent.desktop_context import capture_screen
        path = capture_screen("screen_request.jpg")
        if path:
            log.info("attaching desktop screenshot")
            return path
    if not is_vision_request(text):
        return ""
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/vision/latest")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latest = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("webcam lookup failed: %s", e)
        return ""
    path, age = latest.get("path"), latest.get("age_s")
    if not path or age is None or age > WEBCAM_MAX_AGE_S:
        log.info("vision request but no fresh webcam frame "
                 "(is the camera button on?)")
        return ""
    log.info("attaching webcam frame (%.1fs old)", age)
    return path
