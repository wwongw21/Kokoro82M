from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

import numpy as np
import soundfile as sf

from .config import FFMPEG_EXE, FORMAT_BY_EXTENSION, SAMPLE_RATE
from .text import safe_int


def apply_pitch(audio: np.ndarray, semitones: float) -> np.ndarray:
    """Shift pitch using linear resampling; synthesis speed compensates duration."""
    if semitones == 0 or len(audio) < 2:
        return np.asarray(audio, dtype=np.float32)
    factor = 2.0 ** (semitones / 12.0)
    output_length = max(1, int(len(audio) / factor))
    old_positions = np.arange(len(audio), dtype=np.float64)
    new_positions = np.linspace(0, len(audio) - 1, output_length)
    shifted = np.interp(new_positions, old_positions, audio)
    return shifted.astype(np.float32)


def apply_chunk_edge_fade(
    audio: np.ndarray,
    milliseconds: float = 4.0,
    sample_rate: int = SAMPLE_RATE,
) -> np.ndarray:
    """Remove tiny discontinuities at generated chunk edges without changing timing."""
    result = np.asarray(audio, dtype=np.float32).copy()
    length = min(int(sample_rate * milliseconds / 1000.0), len(result) // 2)
    if length > 1:
        ramp = np.linspace(0.0, 1.0, length, dtype=np.float32)
        result[:length] *= ramp
        result[-length:] *= ramp[::-1]
    return result


def format_time(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_key_for_path(path: Path, fallback: str = "wav16") -> str:
    key = FORMAT_BY_EXTENSION.get(path.suffix.lower(), fallback)
    if path.suffix.lower() == ".wav" and fallback == "wav24":
        return "wav24"
    return key


def normalized_path_key(path: Path) -> str:
    """Return a stable, case-insensitive key for Windows path collision checks."""
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()
    return os.path.normcase(str(resolved))


def ffmpeg_audio_output_args(
    format_key: str,
    input_rate: int,
    bitrate: int = 128,
    bitrate_mode: str = "CBR",
    vbr_quality: int = 2,
    sample_rate: str = "Voice native",
    channels: str = "Mono",
    codec_effort: int = 8,
) -> list[str]:
    """Build validated FFmpeg output options shared by file and streaming encoders."""
    target_rate = input_rate if sample_rate == "Voice native" else safe_int(sample_rate, input_rate)
    channel_count = 1 if channels == "Mono" else 2
    bitrate = max(24, min(320, safe_int(bitrate, 128)))
    bitrate_mode = bitrate_mode if bitrate_mode in {"CBR", "ABR", "VBR"} else "CBR"
    vbr_quality = max(0, min(9, safe_int(vbr_quality, 2)))
    codec_effort = max(0, min(12, safe_int(codec_effort, 8)))
    options = ["-map_metadata", "-1", "-vn", "-ar", str(target_rate), "-ac", str(channel_count)]
    if format_key in {"wav16", "wav24"}:
        options.extend(["-c:a", "pcm_s16le" if format_key == "wav16" else "pcm_s24le"])
    elif format_key == "mp3":
        options.extend(["-c:a", "libmp3lame", "-compression_level", str(max(0, 9 - min(9, codec_effort)))])
        if bitrate_mode == "VBR":
            options.extend(["-q:a", str(vbr_quality)])
        else:
            options.extend(["-b:a", f"{bitrate}k", "-abr", "1" if bitrate_mode == "ABR" else "0"])
    elif format_key == "opus":
        options.extend([
            "-c:a", "libopus", "-b:a", f"{bitrate}k",
            "-vbr", {"CBR": "off", "ABR": "constrained", "VBR": "on"}[bitrate_mode],
            "-compression_level", str(min(10, codec_effort)),
        ])
    elif format_key == "aac":
        options.extend(["-c:a", "aac"])
        options.extend(
            ["-q:a", f"{max(0.1, 2.0 - vbr_quality * 0.2):.1f}"]
            if bitrate_mode == "VBR" else ["-b:a", f"{bitrate}k"]
        )
    elif format_key == "vorbis":
        options.extend(["-c:a", "libvorbis"])
        options.extend(
            ["-q:a", str(max(1, 10 - vbr_quality))]
            if bitrate_mode == "VBR" else ["-b:a", f"{bitrate}k"]
        )
        if bitrate_mode == "CBR":
            options.extend(["-minrate", f"{bitrate}k", "-maxrate", f"{bitrate}k"])
    elif format_key == "flac":
        options.extend(["-c:a", "flac", "-compression_level", str(codec_effort)])
    else:
        raise ValueError(f"Unsupported audio format: {format_key}")
    return options


def transcode_audio_file(
    input_wav: Path,
    output: Path,
    format_key: str,
    bitrate: int = 128,
    bitrate_mode: str = "CBR",
    vbr_quality: int = 2,
    sample_rate: str = "Voice native",
    channels: str = "Mono",
    codec_effort: int = 8,
) -> None:
    """Transcode a disk-backed WAV without loading the full waveform into RAM."""
    output.parent.mkdir(parents=True, exist_ok=True)
    input_rate = int(sf.info(input_wav).samplerate)
    command = [
        str(FFMPEG_EXE), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_wav),
    ]
    command.extend(ffmpeg_audio_output_args(
        format_key, input_rate, bitrate, bitrate_mode, vbr_quality, sample_rate, channels, codec_effort,
    ))
    command.append(str(output))
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Audio encoding failed: {details or 'FFmpeg returned an error.'}")


def write_audio_file(
    output: Path,
    audio: np.ndarray,
    format_key: str,
    bitrate: int = 128,
    bitrate_mode: str = "CBR",
    vbr_quality: int = 2,
    sample_rate: str = "Voice native",
    channels: str = "Mono",
    codec_effort: int = 8,
    source_sample_rate: int = SAMPLE_RATE,
) -> None:
    """Write a short in-memory waveform, using disk-backed transcoding when needed."""
    output.parent.mkdir(parents=True, exist_ok=True)
    source_sample_rate = max(8000, int(source_sample_rate))
    target_rate = source_sample_rate if sample_rate == "Voice native" else int(sample_rate)
    direct_pcm = target_rate == source_sample_rate and channels == "Mono"
    if format_key == "wav16" and direct_pcm:
        sf.write(output, audio, source_sample_rate, subtype="PCM_16")
        return
    if format_key == "wav24" and direct_pcm:
        sf.write(output, audio, source_sample_rate, subtype="PCM_24")
        return
    with tempfile.NamedTemporaryFile(prefix="kokoro_encode_", suffix=".wav", delete=False) as temp_handle:
        temp_path = Path(temp_handle.name)
    try:
        sf.write(temp_path, audio, source_sample_rate, subtype="FLOAT")
        transcode_audio_file(
            temp_path, output, format_key, bitrate, bitrate_mode, vbr_quality,
            sample_rate, channels, codec_effort,
        )
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass

