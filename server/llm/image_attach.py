"""Describe uploaded images using a separate local Ollama vision model."""

from __future__ import annotations

import base64
from pathlib import Path

import requests

from server.config import OLLAMA_VISION_BASE_URL, OLLAMA_VISION_MODEL

# Spoken/stored stand-in when the user attaches an image with no message.
DEFAULT_IMAGE_TEXT = "[shared an image]"

def describe_image(image_path: str, question: str = "", kind: str = "camera",
                   max_tokens: int = 700, brief: bool = False) -> str:
    """Return a detailed French description of an image using the local
    vision model. `question` (optional) makes it check that point first.
    `kind` is "camera" (webcam frame) or "screen" (desktop screenshot)."""
    path = Path(image_path)
    image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    print(f"[image_attach] analyzing {path.name} with {OLLAMA_VISION_MODEL}")

    # Short prompts avoid spurious refusals from the small vision model.
    if kind == "screen":
        if brief:
            base = (
                "Décris cette capture d'écran d'ordinateur en français, en 2 "
                "phrases courtes maximum, sans liste : l'application ou le "
                "site ouvert et ce que l'utilisateur semble faire."
            )
        else:
            base = (
                "Décris cette capture d'écran d'ordinateur en français : "
                "l'application ou le site ouvert, le contenu principal "
                "lisible (titres, texte important, nom du jeu ou de la "
                "vidéo) et ce que l'utilisateur semble être en train de faire."
            )
        fallback = "Décris cette capture d'écran en détail, en français."
        system = ("Tu es un assistant qui décrit objectivement des captures "
                  "d'écran. Tu décris toujours ce que tu vois, sans refuser.")
    else:
        base = (
            "Décris en détail tout ce qui est visible dans cette image, en "
            "français : les personnes, ce qu'elles portent et tiennent dans "
            "les mains (objet, couleur, forme, texte), puis le décor et "
            "l'arrière-plan."
        )
        fallback = "Décris cette image en détail, en français."
        system = ("Tu es un assistant de vision qui décrit objectivement des "
                  "images de caméra. Tu décris toujours ce que tu vois, sans "
                  "refuser.")
    prompts = [base + (f" Précise notamment : {question}" if question else ""),
               base, fallback]

    def is_refusal(text: str) -> bool:
        low = text.lower().strip()
        return low.startswith(("désolé", "je ne peux pas", "i'm sorry",
                               "i can't", "i cannot", "sorry"))

    description = ""
    for prompt in prompts:
        response = requests.post(
            f"{OLLAMA_VISION_BASE_URL}/api/chat",
            json={
                "model": OLLAMA_VISION_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt,
                     "images": [image_b64]},
                ],
                "stream": False,
                "think": False,
                "keep_alive": 0,
                "options": {"temperature": 0, "num_predict": max_tokens},
            },
            timeout=(5, 180),
        )
        response.raise_for_status()
        description = ((response.json().get("message") or {})
                       .get("content", "").strip())
        if description and not is_refusal(description):
            break
        print("[image_attach] refusal/empty answer, retrying with simpler prompt")
        description = ""
    if not description:
        raise RuntimeError(
            f"Ollama vision model {OLLAMA_VISION_MODEL!r} returned no "
            "image description."
        )
    print(f"[image_attach] image description ready ({len(description)} chars)")
    return description


def history_text(user_text: str) -> str:
    """What gets PERSISTED to history for an image turn (no image tokens)."""
    base = user_text or DEFAULT_IMAGE_TEXT
    return base if "[shared an image]" in base else f"{base} [shared an image]"


if __name__ == "__main__":
    # Self-test with a generated 1x1 PNG — no network, no real image needed.
    import tempfile

    png_1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
        "YGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
    )
    tmp = Path(tempfile.gettempdir()) / "image_attach_test.png"
    tmp.write_bytes(png_1x1)

    assert history_text("look at this") == "look at this [shared an image]"
    assert history_text("") == DEFAULT_IMAGE_TEXT
    print("[image_attach.__main__] history_text OK")
    tmp.unlink()
