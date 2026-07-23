Audio Post Processor
====================

A standalone Windows batch processor for spoken-word audio. It accepts WAV,
MP3, M4A/AAC, MP4/M4B audio, OGG, Opus, WMA, FLAC, and AIFF files.

The application preserves source files, writes collision-safe processed copies,
supports 1-8 concurrent workers, and includes Audiobook, General Listening, and
ACX-style processing presets plus personal presets.

AudioPostProcessor.exe uses the FFmpeg bundled with the local Kokoro-82M
installation. Application preferences are stored in settings.json and personal
processing presets are stored in presets.json after first use.
