"""Voice package — Text-to-Speech and Speech-to-Text integration.

Provides:
- ``text_to_speech(text)`` — convert text to an Ogg Opus voice message
- ``speech_to_text(audio_path)`` — transcribe an audio file to text (STT)
- ``VoiceSettings`` — per-chat voice toggle
"""

from antigona.voice.settings import VoiceSettings, get_voice_settings
from antigona.voice.stt import speech_to_text as speech_to_text
from antigona.voice.tts import text_to_speech as text_to_speech

__all__ = [
    "text_to_speech",
    "speech_to_text",
    "VoiceSettings",
    "get_voice_settings",
]
