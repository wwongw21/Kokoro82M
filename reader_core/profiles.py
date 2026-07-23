from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import shutil
import subprocess
import uuid


CHATTERBOX_PROFILE_VERSION = 1


def extract_chatterbox_excerpt(
    ffmpeg: Path,
    source: Path,
    destination: Path,
    start_seconds: float,
    duration_seconds: float,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.stem}.tmp{destination.suffix}")
    command = [
        str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start_seconds:.3f}", "-i", str(source),
        "-t", f"{duration_seconds:.3f}", "-vn", "-ac", "1", "-ar", "24000",
        "-af", "loudnorm=I=-18:TP=-1.5:LRA=11",
        "-c:a", "pcm_s16le", str(temporary),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
            details = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Could not extract the voice sample: {details or 'FFmpeg failed.'}")
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


@dataclass(frozen=True)
class ChatterboxProfile:
    profile_id: str
    name: str
    source_path: str
    start_seconds: float
    duration_seconds: float
    excerpt_path: str
    conditioning_path: str
    cache_version: int = CHATTERBOX_PROFILE_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ChatterboxProfile | None":
        try:
            profile_id = str(data["profile_id"])
            name = str(data["name"]).strip()
            excerpt_path = str(data["excerpt_path"])
            if not profile_id or not name or not excerpt_path:
                return None
            return cls(
                profile_id=profile_id,
                name=name,
                source_path=str(data.get("source_path", "")),
                start_seconds=max(0.0, float(data.get("start_seconds", 0.0))),
                duration_seconds=max(3.0, min(10.0, float(data.get("duration_seconds", 10.0)))),
                excerpt_path=excerpt_path,
                conditioning_path=str(data.get("conditioning_path", "")),
                cache_version=int(data.get("cache_version", CHATTERBOX_PROFILE_VERSION)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def create_chatterbox_profile(
    root: Path,
    ffmpeg: Path,
    name: str,
    source: Path,
    start_seconds: float,
    duration_seconds: float,
) -> ChatterboxProfile:
    if not source.is_file():
        raise FileNotFoundError(f"Reference audio is unavailable: {source}")
    name = name.strip()
    if not name:
        raise ValueError("Enter a name for the voice profile.")
    start_seconds = max(0.0, float(start_seconds))
    duration_seconds = max(3.0, min(10.0, float(duration_seconds)))
    profile_id = uuid.uuid4().hex
    profile_dir = root / profile_id
    profile_dir.mkdir(parents=True, exist_ok=False)
    excerpt = profile_dir / "reference.wav"
    conditioning = profile_dir / "conditionals.pt"
    try:
        extract_chatterbox_excerpt(ffmpeg, source, excerpt, start_seconds, duration_seconds)
        return ChatterboxProfile(
            profile_id=profile_id,
            name=name,
            source_path=str(source.resolve()),
            start_seconds=start_seconds,
            duration_seconds=duration_seconds,
            excerpt_path=str(excerpt.resolve()),
            conditioning_path=str(conditioning.resolve()),
        )
    except Exception:
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise


def delete_chatterbox_profile(root: Path, profile: ChatterboxProfile) -> None:
    """Delete one app-owned profile without following arbitrary source paths."""
    root = root.resolve()
    profile_dir = (root / profile.profile_id).resolve()
    if profile_dir.parent != root:
        raise RuntimeError("Refusing to delete a voice profile outside the managed directory.")
    if profile_dir.is_dir():
        shutil.rmtree(profile_dir)
