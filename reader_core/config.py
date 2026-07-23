from __future__ import annotations

import os
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe


# Portable builds keep models, settings, logs, and temporary playback files beside
# the executable.  Set the Hugging Face paths before importing torch/Kokoro.
PACKAGE_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = PACKAGE_DIR.parent
PORTABLE_MODE = (SCRIPT_DIR / "portable.flag").exists() or os.environ.get("KOKORO_PORTABLE") == "1"
if PORTABLE_MODE:
    os.environ.setdefault("HF_HOME", str(SCRIPT_DIR / "models"))
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(SCRIPT_DIR / "models" / "hub"))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

APP_NAME = "Kokoro Reader"
APP_VERSION = "3.12"
SAMPLE_RATE = 24000
APP_DIR = SCRIPT_DIR / "data" if PORTABLE_MODE else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Kokoro82M"
SETTINGS_PATH = APP_DIR / "settings.json"
PREVIEW_PATH = APP_DIR / "preview.wav"
STREAM_PATH = APP_DIR / "stream_segment.wav"
FFMPEG_EXE = Path(get_ffmpeg_exe())

ENGINE_MAP = {
    "Kokoro-82M (Local)": "kokoro",
    "Edge TTS (Online)": "edge",
    "Supertonic 3 (Local)": "supertonic",
    "Chatterbox-Flash (Local)": "chatterbox_flash",
}
ENGINE_LABEL_BY_KEY = {key: label for label, key in ENGINE_MAP.items()}

AUDIO_FORMATS = {
    "wav16": {"label": "WAV — PCM 16-bit", "extension": ".wav", "lossless": True},
    "wav24": {"label": "WAV — PCM 24-bit", "extension": ".wav", "lossless": True},
    "mp3": {"label": "MP3", "extension": ".mp3", "lossless": False},
    "opus": {"label": "Opus", "extension": ".opus", "lossless": False},
    "aac": {"label": "AAC / M4A", "extension": ".m4a", "lossless": False},
    "vorbis": {"label": "Ogg Vorbis", "extension": ".ogg", "lossless": False},
    "flac": {"label": "FLAC", "extension": ".flac", "lossless": True},
}
AUDIO_LABEL_TO_KEY = {details["label"]: key for key, details in AUDIO_FORMATS.items()}
AUDIO_BITRATES = (24, 32, 48, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
AUDIO_FILE_TYPES = [
    ("MP3 audio", "*.mp3"),
    ("Opus audio", "*.opus"),
    ("AAC / M4A audio", "*.m4a"),
    ("Ogg Vorbis audio", "*.ogg"),
    ("FLAC audio", "*.flac"),
    ("WAV audio", "*.wav"),
    ("All files", "*.*"),
]
FORMAT_BY_EXTENSION = {
    ".mp3": "mp3",
    ".opus": "opus",
    ".m4a": "aac",
    ".aac": "aac",
    ".ogg": "vorbis",
    ".flac": "flac",
    ".wav": "wav16",
}

LANG_MAP = {
    "American English": "a",
    "British English": "b",
}

VOICE_MAP = {
    "American Female — Heart": "af_heart",
    "American Female — Bella": "af_bella",
    "American Female — Nicole": "af_nicole",
    "American Female — Aoede": "af_aoede",
    "American Female — Kore": "af_kore",
    "American Female — Sarah": "af_sarah",
    "American Female — Alloy": "af_alloy",
    "American Female — Nova": "af_nova",
    "American Female — Sky": "af_sky",
    "American Female — Jessica": "af_jessica",
    "American Female — River": "af_river",
    "American Male — Adam": "am_adam",
    "American Male — Echo": "am_echo",
    "American Male — Eric": "am_eric",
    "American Male — Fenrir": "am_fenrir",
    "American Male — Liam": "am_liam",
    "American Male — Michael": "am_michael",
    "American Male — Onyx": "am_onyx",
    "American Male — Puck": "am_puck",
    "American Male — Santa": "am_santa",
    "British Female — Alice": "bf_alice",
    "British Female — Emma": "bf_emma",
    "British Female — Isabella": "bf_isabella",
    "British Female — Lily": "bf_lily",
    "British Male — Daniel": "bm_daniel",
    "British Male — Fable": "bm_fable",
    "British Male — George": "bm_george",
    "British Male — Lewis": "bm_lewis",
}

SUPERTONIC_LANG_MAP = {
    "Automatic / Language Agnostic": "na",
    "Arabic": "ar", "Bulgarian": "bg", "Croatian": "hr", "Czech": "cs",
    "Danish": "da", "Dutch": "nl", "English": "en", "Estonian": "et",
    "Finnish": "fi", "French": "fr", "German": "de", "Greek": "el",
    "Hindi": "hi", "Hungarian": "hu", "Indonesian": "id", "Italian": "it",
    "Japanese": "ja", "Korean": "ko", "Latvian": "lv", "Lithuanian": "lt",
    "Polish": "pl", "Portuguese": "pt", "Romanian": "ro", "Russian": "ru",
    "Slovak": "sk", "Slovenian": "sl", "Spanish": "es", "Swedish": "sv",
    "Turkish": "tr", "Ukrainian": "uk", "Vietnamese": "vi",
}
SUPERTONIC_VOICE_MAP = {
    **{f"Female {number}": f"F{number}" for number in range(1, 6)},
    **{f"Male {number}": f"M{number}" for number in range(1, 6)},
}
CHATTERBOX_FLASH_LANG_MAP = {"English": "en"}
CHATTERBOX_FLASH_VOICE_MAP = {"Cloned Reference Voice": "reference_audio"}

VOICE_VIEW_OPTIONS = ("All Voices", "Preferred Only", "Favorites")
VOICE_USE_CASE_OPTIONS = ("All", "Long-form Reading", "General Purpose", "Professional Delivery")
VOICE_USE_CASE_KEYS = {
    "Long-form Reading": "long_form",
    "General Purpose": "general",
    "Professional Delivery": "professional",
}

# Kokoro is the only provider that publishes explicit training/reference quality
# grades. The strongest available voices lead each curated use-case list.
KOKORO_PREFERRED_VOICES = {
    "long_form": ("af_heart", "af_bella", "af_nicole", "bf_emma", "af_aoede", "af_kore",
                  "af_sarah", "am_fenrir", "am_michael", "am_puck", "bf_isabella", "bm_fable"),
    "general": ("af_heart", "af_bella", "af_nicole", "bf_emma", "af_aoede", "af_kore", "af_sarah",
                "am_fenrir", "am_michael", "am_puck", "af_alloy", "af_nova", "bf_isabella",
                "bm_fable", "bm_george"),
    "professional": ("af_heart", "af_bella", "af_nicole", "bf_emma", "af_aoede", "af_kore",
                     "af_sarah", "am_fenrir", "am_michael", "am_puck", "af_alloy", "af_nova",
                     "bf_isabella", "bm_george"),
}
KOKORO_PREFERRED_ORDER = (
    "af_heart", "af_bella", "af_nicole", "bf_emma", "af_aoede", "af_kore", "af_sarah",
    "am_fenrir", "am_michael", "am_puck", "af_alloy", "af_nova", "bf_isabella",
    "bm_fable", "bm_george",
)

# Supertonic publishes use-case descriptions rather than numeric quality grades.
SUPERTONIC_PREFERRED_VOICES = {
    "long_form": ("F5", "M5"),
    "general": ("M1", "F1", "F2", "M4"),
    "professional": ("F3", "M3", "F4", "M2"),
}
SUPERTONIC_PREFERRED_ORDER = ("F5", "M5", "M1", "F1", "F2", "M4", "F3", "M3", "F4", "M2")

CHATTERBOX_FLASH_PREFERRED_VOICES = {
    "long_form": ("reference_audio",),
    "general": ("reference_audio",),
    "professional": ("reference_audio",),
}
CHATTERBOX_FLASH_PREFERRED_ORDER = ("reference_audio",)

EDGE_FALLBACK_USE_CASES = {
    "en-US-AvaNeural": ("long_form", "general"),
    "en-US-AndrewNeural": ("general", "professional"),
    "en-US-EmmaNeural": ("long_form", "general"),
    "en-US-BrianNeural": ("long_form", "professional"),
    "en-GB-SoniaNeural": ("long_form", "professional"),
    "en-GB-RyanNeural": ("general", "professional"),
}
EDGE_PREFERRED_ORDER = tuple(EDGE_FALLBACK_USE_CASES)

# A useful offline catalog is shown immediately.  A live Edge voice refresh
# replaces it when the online engine is selected.
EDGE_FALLBACK_VOICES = [
    {"ShortName": "en-US-AvaNeural", "Locale": "en-US", "Gender": "Female", "FriendlyName": "Ava"},
    {"ShortName": "en-US-AndrewNeural", "Locale": "en-US", "Gender": "Male", "FriendlyName": "Andrew"},
    {"ShortName": "en-US-EmmaNeural", "Locale": "en-US", "Gender": "Female", "FriendlyName": "Emma"},
    {"ShortName": "en-US-BrianNeural", "Locale": "en-US", "Gender": "Male", "FriendlyName": "Brian"},
    {"ShortName": "en-GB-SoniaNeural", "Locale": "en-GB", "Gender": "Female", "FriendlyName": "Sonia"},
    {"ShortName": "en-GB-RyanNeural", "Locale": "en-GB", "Gender": "Male", "FriendlyName": "Ryan"},
]

OPEN_FILE_TYPES = [
    ("Supported documents", "*.txt *.md *.log *.csv *.srt *.vtt *.rtf *.docx *.epub"),
    ("Text files", "*.txt *.md *.log *.csv"),
    ("Subtitle files", "*.srt *.vtt"),
    ("Word documents", "*.docx"),
    ("EPUB books", "*.epub"),
    ("Rich Text Format", "*.rtf"),
    ("All files", "*.*"),
]
SUPPORTED_DOCUMENT_EXTENSIONS = {".txt", ".md", ".log", ".csv", ".srt", ".vtt", ".rtf", ".docx", ".epub"}
MAX_DOCUMENT_BYTES = 128 * 1024 * 1024
