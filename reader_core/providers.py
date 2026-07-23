from __future__ import annotations

import asyncio
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from typing import Callable, Iterable

import numpy as np
import soundfile as sf

from .config import FFMPEG_EXE
from .voice_catalog import (
    KOKORO_COLLECTION_VOICE_MAPS,
    kokoro_voice_filename,
    kokoro_voice_repository,
    normalize_kokoro_collection,
)


_TORCH = None
_KPIPELINE_CLASS = None
_INFERENCE_DEVICE: str | None = None
_EDGE_TTS_MODULE = None
_SUPERTONIC_TTS_CLASS = None


def load_kokoro_runtime():
    """Import the large ML stack only when speech is requested."""
    global _TORCH, _KPIPELINE_CLASS, _INFERENCE_DEVICE
    if _TORCH is None or _KPIPELINE_CLASS is None:
        import torch
        from kokoro import KPipeline

        _TORCH = torch
        _KPIPELINE_CLASS = KPipeline
        _INFERENCE_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    return _TORCH, _KPIPELINE_CLASS, _INFERENCE_DEVICE


def load_edge_runtime():
    """Import edge-tts only when its catalog or synthesis is requested."""
    global _EDGE_TTS_MODULE
    if _EDGE_TTS_MODULE is None:
        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError(
                "Edge TTS is not installed. Run: python\\python.exe -m pip install edge-tts"
            ) from exc
        _EDGE_TTS_MODULE = edge_tts
    return _EDGE_TTS_MODULE


def load_supertonic_runtime():
    """Import the Supertonic SDK only when local ONNX speech is requested."""
    global _SUPERTONIC_TTS_CLASS
    if _SUPERTONIC_TTS_CLASS is None:
        try:
            from supertonic import TTS
        except ImportError as exc:
            raise RuntimeError(
                "Supertonic is not installed. Run: python\\python.exe -m pip install supertonic"
            ) from exc
        _SUPERTONIC_TTS_CLASS = TTS
    return _SUPERTONIC_TTS_CLASS


def edge_voice_label(voice: dict[str, object]) -> str:
    short_name = str(voice.get("ShortName", ""))
    friendly = str(voice.get("FriendlyName", "")).strip()
    match = re.search(r"Microsoft\s+(.+?)\s+Online", friendly, flags=re.IGNORECASE)
    name = match.group(1) if match else friendly
    if not name or name == short_name:
        parts = short_name.split("-")
        name = re.sub(r"Neural$", "", parts[-1]) if parts else short_name
    gender = str(voice.get("Gender", "Voice"))
    return f"{name} — {gender} ({short_name})"


def synthesize_edge_audio(
    text: str,
    voice: str,
    speed: float,
    pitch_semitones: float,
    cancel_event: threading.Event,
) -> tuple[np.ndarray, int]:
    """Stream Edge's MP3 response and decode it to a mono float waveform."""
    edge_tts = load_edge_runtime()
    rate_percent = int(round((max(0.5, min(2.0, speed)) - 1.0) * 100.0))
    # Edge exposes pitch in Hz rather than semitones.  Ten Hz per semitone gives
    # a useful, bounded UI mapping while leaving zero exactly neutral.
    pitch_hz = int(round(max(-12.0, min(12.0, pitch_semitones)) * 10.0))
    with tempfile.NamedTemporaryFile(prefix="edge_tts_", suffix=".mp3", delete=False) as media_handle:
        media_path = Path(media_handle.name)
    with tempfile.NamedTemporaryFile(prefix="edge_tts_decode_", suffix=".wav", delete=False) as wav_handle:
        wav_path = Path(wav_handle.name)

    async def collect_audio() -> None:
        communicator = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=f"{rate_percent:+d}%",
            pitch=f"{pitch_hz:+d}Hz",
        )
        wrote_audio = False
        with media_path.open("wb") as output:
            async for message in communicator.stream():
                if cancel_event.is_set():
                    raise InterruptedError("Speech task cancelled")
                if message.get("type") == "audio":
                    output.write(message["data"])
                    wrote_audio = True
        if not wrote_audio:
            raise RuntimeError("Edge TTS returned no audio.")

    try:
        asyncio.run(collect_audio())
        if cancel_event.is_set():
            raise InterruptedError("Speech task cancelled")
        command = [
            str(FFMPEG_EXE), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(media_path), "-ac", "1", "-c:a", "pcm_f32le", str(wav_path),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            details = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Could not decode Edge TTS audio: {details or 'FFmpeg failed.'}")
        audio, sample_rate = sf.read(wav_path, dtype="float32", always_2d=False)
        return np.asarray(audio, dtype=np.float32).reshape(-1), int(sample_rate)
    finally:
        for path in (media_path, wav_path):
            try:
                path.unlink()
            except OSError:
                pass


def stream_edge_audio_bytes(
    texts: Iterable[str],
    voice: str,
    speed: float,
    pitch_semitones: float,
    cancel_event: threading.Event,
    sink: Callable[[bytes], None],
    segment_callback: Callable[[int, int], None] | None = None,
) -> None:
    """Stream all Edge MP3 frames to one consumer without per-chunk temp files."""
    edge_tts = load_edge_runtime()
    text_list = [text for text in texts if text.strip()]
    if not text_list:
        raise ValueError("No readable text was supplied to Edge TTS.")
    rate_percent = int(round((max(0.5, min(2.0, speed)) - 1.0) * 100.0))
    pitch_hz = int(round(max(-12.0, min(12.0, pitch_semitones)) * 10.0))

    async def stream_all() -> None:
        wrote_audio = False
        for index, text in enumerate(text_list, start=1):
            if segment_callback is not None:
                segment_callback(index, len(text_list))
            communicator = edge_tts.Communicate(
                text=text,
                voice=voice,
                rate=f"{rate_percent:+d}%",
                pitch=f"{pitch_hz:+d}Hz",
            )
            async for message in communicator.stream():
                if cancel_event.is_set():
                    raise InterruptedError("Speech task cancelled")
                if message.get("type") == "audio":
                    sink(message["data"])
                    wrote_audio = True
        if not wrote_audio:
            raise RuntimeError("Edge TTS returned no audio.")

    asyncio.run(stream_all())


def loaded_kokoro_torch():
    """Return the loaded torch module without triggering the costly runtime import."""
    return _TORCH


def resolve_kokoro_voice(collection: str, voice_id: str) -> str:
    """Resolve a stable catalog voice ID to the value accepted by KPipeline."""
    collection = normalize_kokoro_collection(collection)
    if voice_id not in KOKORO_COLLECTION_VOICE_MAPS[collection].values():
        raise ValueError(f"Unknown Kokoro voice for {collection}: {voice_id}")
    if collection in {"official_v1", "official_v1_1_zh"}:
        return voice_id
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise RuntimeError("Hugging Face Hub is required for community Kokoro voice packs.") from exc
    try:
        return hf_hub_download(
            repo_id=kokoro_voice_repository(collection),
            filename=kokoro_voice_filename(collection, voice_id),
        )
    except Exception as exc:
        label = collection.replace("_", " ").title()
        raise RuntimeError(
            f"Could not download the {label} voice pack '{voice_id}'. "
            "Check the connection or disable portable offline mode."
        ) from exc
