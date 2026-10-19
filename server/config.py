# Configuration for Riko's Brain and Body

"""
Global configuration for the Riko AI project.
"""

# --- LLM Settings ---
OLLAMA_MODEL = "Qwen3.5-9B-Uncensored:latest"
OLLAMA_BASE_URL = "http://127.0.0.1:11434"
OPENAI_TOOL_MODEL = "gpt-4o"  # Fallback or primary for tools
ENABLE_FUNCTION_CALLING = True

# --- Robot Integration ---
# Set this to True to allow Riko to use physical tools from Robot_Patreon
ENABLE_ROBOT_TOOLS = True
ROBOT_API_URL = "http://127.0.0.1:8000"  # Default port from chatterbox-server.py

# --- TTS Settings ---
TTS_BACKEND = "xtts"
XTTS_BASE_URL = "http://127.0.0.1:8020"
XTTS_REFERENCE_WAV = "client/characters/riko/voice/voice.wav"

# --- Playback Settings ---
DEFAULT_EXPRESSION = "relaxed"
PLAYBACK_PAD_S = 0.01
PLAYBACK_FIRST_PAD_S = 0.01

# --- Path Settings ---
PROJECT_ROOT = Path(__file__).parent.parent
MIXAMO_REL_DIR = Path("animations/mixamo")
