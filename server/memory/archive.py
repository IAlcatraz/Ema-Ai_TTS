"""MessageArchive — searchable archive of every past message.

The window only keeps the last few exchanges and the long-term store only keeps
distilled facts. This archive keeps EVERY message (tag-stripped) with an
embedding, so Riko can look up what was actually said earlier ("tu te souviens
de ce que je t'ai dit ?"). It is only queried when the user refers to the past
(see `is_recall_request`), so ordinary turns never pay for it.

Files (next to the long-term store): archive.faiss + archive.json.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import faiss
import numpy as np

from server.config import ARCHIVE_MIN_SIMILARITY, ARCHIVE_TOP_K, MEMORY_DIR
from server.memory.embedder import get_embedder
from server.memory.session_log import strip_tags

# Phrases that mean "look at what we said before".
_RECALL_REQUEST = re.compile(
    r"\b(?:te\s+souviens|souviens[\s-]*toi|tu\s+te\s+rappelles?|rappelle[\s-]*toi|"
    r"je\s+t['’]ai\s+(?:dit|racont|parl|demand|montr)|tu\s+m['’]as\s+(?:dit|racont|parl|demand)|"
    r"tout\s+à\s+l['’]?\s*heure|la\s+dernière\s+(?:chose|fois)|"
    r"(?:on|nous)\s+(?:parlait|avait\s+parl|a\s+parl)|de\s+quoi\s+(?:on|nous)\s+a\s+parl|"
    r"ce\s+que\s+(?:je\s+t['’]ai|tu\s+m['’]as)|hier|"
    r"remember|you\s+told\s+me|i\s+told\s+you)\b",
    re.IGNORECASE,
)


def is_recall_request(text: str) -> bool:
    """True when the message asks Riko to remember something said before."""
    return bool(_RECALL_REQUEST.search(text or ""))


def _ago(timestamp: str) -> str:
    """French relative time for a stored ISO timestamp."""
    try:
        seconds = (datetime.now() - datetime.fromisoformat(timestamp)).total_seconds()
    except (ValueError, TypeError):
        return "à un moment"
    if seconds < 120:
        return "à l'instant"
    if seconds < 3600:
        return f"il y a {int(seconds // 60)} min"
    if seconds < 86400:
        return f"il y a {int(seconds // 3600)} h"
    return f"il y a {int(seconds // 86400)} j"


class MessageArchive:
    def __init__(self, mem_dir: Path | str | None = None):
        self.dir = Path(mem_dir) if mem_dir is not None else MEMORY_DIR
        self.faiss_path = self.dir / "archive.faiss"
        self.meta_path = self.dir / "archive.json"
        self.embedder = get_embedder()
        self.dim = int(self._embed(["dimension probe"]).shape[1])

        if self.faiss_path.exists() and self.meta_path.exists():
            self.index = faiss.read_index(str(self.faiss_path))
            self.metadata: list[dict] = json.loads(
                self.meta_path.read_text(encoding="utf-8"))
        else:
            self.index = faiss.IndexFlatIP(self.dim)
            self.metadata = []
        print(f"[archive] ready: {self.index.ntotal} message(s) at {self.dir}")

    def _embed(self, texts: list[str]) -> np.ndarray:
        vecs = self.embedder.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True)
        return vecs.astype("float32")

    def add(self, role: str, text: str) -> bool:
        """Archive one message. Returns False when it is too short to be useful."""
        clean = strip_tags(text)
        if len(clean) < 4:
            return False
        self.index.add(self._embed([clean]))
        self.metadata.append({
            "role": role,
            "text": clean,
            "ts": datetime.now().isoformat(timespec="seconds"),
        })
        self.dir.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(self.faiss_path))
        self.meta_path.write_text(
            json.dumps(self.metadata, ensure_ascii=False), encoding="utf-8")
        return True

    def search(self, query: str, top_k: int = ARCHIVE_TOP_K,
               exclude_texts: set[str] | None = None) -> list[dict]:
        """Most relevant past messages, best first. `exclude_texts` hides
        messages the model already sees verbatim in its window."""
        if self.index.ntotal == 0 or top_k <= 0:
            return []
        exclude = exclude_texts or set()
        fetch_k = min(self.index.ntotal, top_k * 4 + len(exclude))
        sims, idxs = self.index.search(self._embed([query]), fetch_k)
        hits = []
        for sim, idx in zip(sims[0], idxs[0]):
            if idx == -1 or float(sim) < ARCHIVE_MIN_SIMILARITY:
                continue
            item = self.metadata[idx]
            if item["text"] in exclude:
                continue
            hits.append(item)
            if len(hits) == top_k:
                break
        return hits

    def retrieve_formatted(self, query: str, top_k: int = ARCHIVE_TOP_K,
                           exclude_texts: set[str] | None = None) -> str:
        """Lean block of old messages for the system prompt, or ""."""
        hits = self.search(query, top_k, exclude_texts)
        if not hits:
            print(f"[archive] nothing relevant for query={query!r}")
            return ""
        who = {"user": "L'utilisateur", "assistant": "Toi"}
        lines = [f"- ({_ago(h['ts'])}) {who.get(h['role'], h['role'])} : {h['text']}"
                 for h in hits]
        print(f"[archive] recalled {len(lines)} message(s) for query={query!r}")
        return "Messages passés retrouvés dans ta mémoire de conversation :\n" + "\n".join(lines)


if __name__ == "__main__":
    assert is_recall_request("Tu te souviens de ce que je t'ai dit ?")
    assert is_recall_request("C'est quoi la dernière chose que je t'ai dite")
    assert is_recall_request("On parlait de quoi tout à l'heure ?")
    assert not is_recall_request("Quelle heure est-il ?")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        arch = MessageArchive(mem_dir=tmp)
        assert arch.add("user", "J'ai adopté un chat qui s'appelle Mochi")
        assert arch.add("user", "Mon jeu préféré est Apex Legends")
        assert not arch.add("user", "ok")
        out = arch.retrieve_formatted("mon chat s'appelle comment ?", top_k=1)
        assert "Mochi" in out, out
        assert MessageArchive(mem_dir=tmp).index.ntotal == 2, "did not persist"
    print("[archive.__main__] OK — recall detection / add / persist / search")
