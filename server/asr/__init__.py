"""Speech capture + transcription helpers.

`recorder.record_on_speech` blocks until the user speaks then writes a
WAV. `transcribe.transcribe_audio_groq` posts that WAV to Groq's Whisper
endpoint and returns the text. Kept side-effect-free at import time so
`server.streaming_agent.inputs.voice_input` can pull just what it needs.
"""
