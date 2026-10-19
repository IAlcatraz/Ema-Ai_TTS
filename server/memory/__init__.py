"""Memory package — long-term memory plus the medium-term session summary.

OFF by default. See server/config.py (ENABLE_MEMORY / ENABLE_SESSION_GIST)
and README.md for what each tier does.
"""

from server.memory.embedder import get_embedder
from server.memory.schema import VAD, MemorySchema, MemoryList
from server.memory.store import MemoryStore
from server.memory.session_log import SessionLog, strip_tags
from server.memory.window import RollingWindow
from server.memory.extractor import extract_session
from server.memory.gist import update_gist

__all__ = [
    "get_embedder",
    "VAD",
    "MemorySchema",
    "MemoryList",
    "MemoryStore",
    "SessionLog",
    "strip_tags",
    "RollingWindow",
    "extract_session",
    "update_gist",
]
