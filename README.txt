Kokoro Reader Desktop
=====================

Launch Kokoro-82M from the Desktop or Start Menu. Select a speech engine,
language, and voice from the controls above the document editor.

Voice discovery:
- Kokoro's Collection selector switches among Official v1.0, Official
  v1.1-zh, Gushi Labs, and Sethblocks.
- All Voices lists favorites first, followed by provider-recommended voices.
- Preferred Only shows curated voices for long-form, general, or professional use.
- Favorites shows voices saved with the star button or detachable Voice menu.
- Favorites and voice-list filters are restored when the app is restarted.

Batch conversion streams generated audio directly into one encoder per output.
Edge audio bytes go straight to that encoder without per-segment MP3/WAV files.
Supertonic processes up to four text segments together; Chatterbox-Flash uses an
adaptive batch of two on 8 GB GPUs and four when at least 12 GB is free. Failed
local batches automatically split to smaller groups instead of losing the export.

Model selection is immediate. A 650 ms latest-selection-wins preload starts only
after the controls settle, and loading never holds the Tk UI lock. Only one
GPU-backed engine is retained at a time. American and British pipelines from the
same Kokoro repository share the active model.

Speech engines:
- Kokoro-82M: local neural speech at 24 kHz. Official v1.0 includes all 28
  English voices (20 American and 8 British). Official v1.1-zh adds Maple,
  Sol, Vale, and 100 Mandarin voices using its separate model checkpoint.
  Gushi Labs adds Vivien and Tony; Sethblocks adds Mika, Mrs. Claus,
  Heart Young, Andy, and Dylan. The two community collections are compatible
  voice packs for the official v1.0 model and are not official Kokoro releases.
- Edge TTS: Microsoft's online neural speech service. The voice catalog is
  refreshed when selected and falls back to common English voices if the
  catalog cannot be reached. Text being spoken is sent to Microsoft.
- Supertonic 3: local ONNX speech at 44.1 kHz with 31 languages, an automatic
  language mode, and ten included voice styles. Its model downloads on first
  use and is cached in models\supertonic3.
- Chatterbox-Flash: local English zero-shot voice cloning at 24 kHz. Create a
  named voice profile from a clean recording by choosing its start time and a
  3–10 second duration. The app stores a normalized managed excerpt and cached
  conditioning; deleting a profile never deletes its original recording.

The first use of a Kokoro model or community voice, Supertonic, or
Chatterbox-Flash may take longer while its files are downloaded and loaded.
Official v1.0 and v1.1-zh models are cached independently; community Kokoro
voice packs reuse the v1.0 model. Later generations run locally.

Default installation folder:
  %LOCALAPPDATA%\Kokoro82M

Output formats:
  MP3, Opus, AAC/M4A, Ogg Vorbis, FLAC, 16-bit WAV, and 24-bit WAV.
  "Voice native" preserves 24 kHz for Kokoro/Edge/Chatterbox-Flash and 44.1 kHz
  for Supertonic.

Notes:
- An internet connection is required for Edge TTS and first-time model downloads.
- Chatterbox-Flash uses the compatibility pins in chatterbox-overrides.txt. Its
  isolated runtime pins Transformers 5.2.0. Its first model download is
  approximately 3.2 GB.
- NVIDIA GPU acceleration requires a compatible NVIDIA driver.
- Windows SmartScreen may warn because this custom installer is not digitally signed.
