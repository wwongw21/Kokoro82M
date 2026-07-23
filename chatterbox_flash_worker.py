from __future__ import annotations

import contextlib
import json
import sys
import traceback
from pathlib import Path


def reference_cache_key(reference_audio: str) -> tuple[str, int, int]:
    reference = Path(reference_audio)
    if not reference.is_file():
        raise FileNotFoundError(f"Reference audio is unavailable: {reference}")
    resolved = str(reference.resolve())
    stat = reference.stat()
    return resolved, stat.st_mtime_ns, stat.st_size


def main() -> None:
    protocol_output = sys.stdout
    sys.stdout = sys.stderr

    def reply(payload: dict[str, object]) -> None:
        protocol_output.write(json.dumps(payload, ensure_ascii=False) + "\n")
        protocol_output.flush()

    import librosa
    import numpy as np
    import soundfile as sf
    import torch
    from chatterbox_flash import ChatterboxFlashTTS
    from chatterbox_flash.tts import Conditionals

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    model = None
    conditioned_reference: tuple[str, int, int] | None = None
    conditioned_profile_id = ""

    def ensure_model():
        nonlocal model
        if model is None:
            model = ChatterboxFlashTTS.from_pretrained(
                "ResembleAI/chatterbox-flash", device=device, dtype=dtype,
            )
        return model

    def prepare(reference_audio: str) -> None:
        nonlocal conditioned_reference, conditioned_profile_id
        cache_key = reference_cache_key(reference_audio)
        if conditioned_reference != cache_key:
            ensure_model().prepare_conditionals(cache_key[0])
            conditioned_reference = cache_key
            conditioned_profile_id = "legacy"

    def prepare_profile(profile_id: str, reference_audio: str, conditioning_path: str) -> None:
        nonlocal conditioned_reference, conditioned_profile_id
        cache_key = reference_cache_key(reference_audio)
        if conditioned_profile_id == profile_id and conditioned_reference == cache_key:
            return
        loaded = ensure_model()
        cache = Path(conditioning_path) if conditioning_path else None
        if cache is not None and cache.is_file():
            loaded.conds = Conditionals.load(cache, map_location="cpu").to(device)
        else:
            conditionals = loaded.prepare_conditionals(cache_key[0])
            if cache is not None:
                cache.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache.with_suffix(cache.suffix + ".tmp")
                conditionals.save(temporary)
                temporary.replace(cache)
        conditioned_reference = cache_key
        conditioned_profile_id = profile_id

    def apply_audio_controls(audio, rate: float, pitch: float):
        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        value = np.asarray(audio, dtype=np.float32).reshape(-1)
        if abs(rate - 1.0) > 0.001:
            value = librosa.effects.time_stretch(value, rate=rate)
        if abs(pitch) > 0.001:
            value = librosa.effects.pitch_shift(value, sr=int(model.sr), n_steps=pitch)
        return value

    for raw_line in sys.stdin:
        request_id = ""
        try:
            request = json.loads(raw_line)
            request_id = str(request.get("request_id", ""))
            command = str(request.get("command", ""))
            if command == "shutdown":
                reply({"ok": True, "request_id": request_id})
                return
            if command == "load":
                loaded = ensure_model()
                reply({"ok": True, "request_id": request_id, "device": device, "sample_rate": int(loaded.sr)})
                continue
            if command == "prepare":
                prepare(str(request.get("reference_audio", "")))
                reply({"ok": True, "request_id": request_id, "device": device})
                continue
            if command == "prepare_profile":
                prepare_profile(
                    str(request.get("profile_id", "legacy")),
                    str(request.get("reference_audio", "")),
                    str(request.get("conditioning_path", "")),
                )
                reply({"ok": True, "request_id": request_id, "device": device})
                continue
            if command == "delete_profile":
                if conditioned_profile_id == str(request.get("profile_id", "")):
                    ensure_model().conds = None
                    conditioned_reference = None
                    conditioned_profile_id = ""
                reply({"ok": True, "request_id": request_id})
                continue
            if command == "synthesize":
                text = str(request.get("text", "")).strip()
                if not text:
                    raise ValueError("No text was supplied to Chatterbox-Flash.")
                prepare(str(request.get("reference_audio", "")))
                with contextlib.redirect_stdout(sys.stderr):
                    audio = ensure_model().generate(text, backend="torch")
                rate = max(0.5, min(2.0, float(request.get("rate", 1.0))))
                pitch = max(-12.0, min(12.0, float(request.get("pitch", 0.0))))
                output = Path(str(request["output"]))
                output.parent.mkdir(parents=True, exist_ok=True)
                sf.write(output, apply_audio_controls(audio, rate, pitch), int(model.sr), subtype="FLOAT")
                reply({"ok": True, "request_id": request_id, "device": device, "sample_rate": int(model.sr)})
                continue
            if command == "synthesize_batch":
                texts = [str(text).strip() for text in request.get("texts", [])]
                if not texts or any(not text for text in texts):
                    raise ValueError("Chatterbox batch text must not be empty.")
                prepare_profile(
                    str(request.get("profile_id", "legacy")),
                    str(request.get("reference_audio", "")),
                    str(request.get("conditioning_path", "")),
                )
                with contextlib.redirect_stdout(sys.stderr):
                    rows = ensure_model().generate_batch(
                        texts, conds_list=[model.conds] * len(texts), backend="torch",
                    )
                rate = max(0.5, min(2.0, float(request.get("rate", 1.0))))
                pitch = max(-12.0, min(12.0, float(request.get("pitch", 0.0))))
                values = [apply_audio_controls(audio, rate, pitch) for audio in rows]
                output = Path(str(request["output"]))
                output.parent.mkdir(parents=True, exist_ok=True)
                np.savez(
                    output,
                    sample_rate=np.asarray([int(model.sr)], dtype=np.int32),
                    lengths=np.asarray([len(value) for value in values], dtype=np.int64),
                    audio=np.concatenate(values).astype(np.float32, copy=False),
                )
                reply({"ok": True, "request_id": request_id, "device": device, "sample_rate": int(model.sr)})
                continue
            raise ValueError(f"Unknown worker command: {command}")
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            reply({
                "ok": False,
                "request_id": request_id,
                "error": str(exc),
                "type": type(exc).__name__,
            })


if __name__ == "__main__":
    main()
