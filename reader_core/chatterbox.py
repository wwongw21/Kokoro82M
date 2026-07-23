from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import uuid

import numpy as np

from .config import SAMPLE_RATE


class ChatterboxFlashClient:
    """Own the isolated Chatterbox worker and its request-aware JSON protocol."""

    def __init__(self, script_dir: Path, app_dir: Path) -> None:
        self.script_dir = script_dir
        self.app_dir = app_dir
        self.process: subprocess.Popen[str] | None = None
        self.device: str | None = None
        self._lock = threading.RLock()
        self._worker_errors: deque[str] = deque(maxlen=40)
        self._responses: queue.Queue[dict[str, object]] = queue.Queue()

    @property
    def is_ready(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _start_process(self) -> subprocess.Popen[str]:
        runtime = self.script_dir / "chatterbox-runtime" / "Scripts" / "python.exe"
        worker = self.script_dir / "chatterbox_flash_worker.py"
        if not runtime.is_file():
            raise RuntimeError(
                "The isolated Chatterbox-Flash runtime is missing. Reinstall the Chatterbox component."
            )
        worker_cache = self.app_dir / "models" / "chatterbox-flash"
        worker_cache.mkdir(parents=True, exist_ok=True)
        worker_env = os.environ.copy()
        worker_env["HF_HOME"] = str(worker_cache)
        worker_env["HUGGINGFACE_HUB_CACHE"] = str(worker_cache / "hub")
        self._responses = queue.Queue()
        process = subprocess.Popen(
            [str(runtime), "-u", str(worker)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=worker_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.process = process
        threading.Thread(
            target=self._drain_output,
            args=(process,),
            daemon=True,
            name="chatterbox-flash-responses",
        ).start()
        threading.Thread(
            target=self._drain_errors,
            args=(process,),
            daemon=True,
            name="chatterbox-flash-errors",
        ).start()
        return process

    def ensure(self) -> subprocess.Popen[str]:
        with self._lock:
            if self.is_ready:
                assert self.process is not None
                return self.process
            last_error: Exception | None = None
            for _attempt in range(2):
                self.stop()
                process = self._start_process()
                try:
                    response = self._request({"command": "load"}, timeout=120.0)
                    self.device = str(response.get("device", "cpu"))
                    return process
                except Exception as exc:
                    last_error = exc
                    self.stop()
            raise RuntimeError(f"Chatterbox-Flash could not start after a clean retry: {last_error}")

    def _drain_output(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            try:
                response = json.loads(line)
                if isinstance(response, dict):
                    self._responses.put(response)
                else:
                    self._responses.put({"_protocol_error": "Unexpected response type."})
            except json.JSONDecodeError:
                self._responses.put({"_protocol_error": f"Invalid worker response: {line[:200]}"})
        self._responses.put({"_eof": True, "exit_code": process.poll()})

    def _drain_errors(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            cleaned = line.rstrip()
            if cleaned:
                self._worker_errors.append(cleaned)

    def stop(self) -> None:
        with self._lock:
            process = self.process
            self.process = None
            self.device = None
            if process is None:
                return
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                        process.wait()
                    except OSError:
                        pass
                except OSError:
                    pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def _recent_errors(self, count: int) -> str:
        return "\n".join(list(self._worker_errors)[-count:])

    def _request(self, request: dict[str, object], timeout: float = 120.0) -> dict[str, object]:
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            raise RuntimeError(
                f"Chatterbox-Flash worker stopped unexpectedly. {self._recent_errors(5)}".strip()
            )
        request_id = uuid.uuid4().hex
        payload = {**request, "request_id": request_id}
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError(
                f"Could not send work to Chatterbox-Flash. {self._recent_errors(8)}".strip()
            ) from exc
        deadline = time.monotonic() + max(1.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Chatterbox-Flash timed out after {timeout:.0f} seconds. {self._recent_errors(8)}".strip()
                )
            try:
                response = self._responses.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Chatterbox-Flash exited with code {process.returncode}. {self._recent_errors(8)}".strip()
                    )
                continue
            if response.get("_eof"):
                raise RuntimeError(
                    f"Chatterbox-Flash exited with code {response.get('exit_code')}. "
                    f"{self._recent_errors(8)}".strip()
                )
            if response.get("_protocol_error"):
                raise RuntimeError(str(response["_protocol_error"]))
            if response.get("request_id") != request_id:
                continue
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error", "Chatterbox-Flash synthesis failed.")))
            return response

    def _request_with_retry(self, request: dict[str, object], timeout: float) -> dict[str, object]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self.ensure()
                return self._request(request, timeout=timeout)
            except Exception as exc:
                last_error = exc
                self.stop()
                if attempt:
                    break
        raise RuntimeError(f"Chatterbox-Flash failed after a clean worker retry: {last_error}")

    def prepare_reference(self, reference_audio: str) -> None:
        reference = Path(reference_audio) if reference_audio else None
        if reference is None or not reference.is_file():
            raise RuntimeError(
                "Chatterbox-Flash needs a reference voice clip. Create or select a voice profile first."
            )
        with self._lock:
            self._request_with_retry(
                {"command": "prepare", "reference_audio": str(reference.resolve())}, timeout=90.0,
            )

    def prepare_profile(
        self,
        profile_id: str,
        reference_audio: str,
        conditioning_path: str = "",
    ) -> None:
        reference = Path(reference_audio)
        if not reference.is_file():
            raise RuntimeError(f"Chatterbox voice sample is unavailable: {reference}")
        with self._lock:
            self._request_with_retry({
                "command": "prepare_profile",
                "profile_id": profile_id,
                "reference_audio": str(reference.resolve()),
                "conditioning_path": conditioning_path,
            }, timeout=90.0)

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            if self.is_ready:
                self._request({"command": "delete_profile", "profile_id": profile_id}, timeout=10.0)

    def synthesize(self, text: str, reference_audio: str, rate: float, pitch: float) -> np.ndarray:
        rows = self.synthesize_batch([text], reference_audio, rate, pitch)
        if not rows:
            raise RuntimeError("Chatterbox-Flash returned no audio.")
        return rows[0]

    def synthesize_batch(
        self,
        texts: list[str],
        reference_audio: str,
        rate: float,
        pitch: float,
        profile_id: str = "legacy",
        conditioning_path: str = "",
    ) -> list[np.ndarray]:
        if not texts:
            return []
        self.app_dir.mkdir(parents=True, exist_ok=True)
        output: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="chatterbox_flash_", suffix=".npz", dir=self.app_dir, delete=False,
            ) as handle:
                output = Path(handle.name)
            with self._lock:
                self._request_with_retry({
                    "command": "synthesize_batch",
                    "texts": texts,
                    "reference_audio": reference_audio,
                    "profile_id": profile_id,
                    "conditioning_path": conditioning_path,
                    "rate": rate,
                    "pitch": pitch,
                    "output": str(output),
                }, timeout=max(180.0, sum(map(len, texts)) * 1.5))
            with np.load(output, allow_pickle=False) as result:
                sample_rate = int(np.asarray(result["sample_rate"]).reshape(-1)[0])
                lengths = np.asarray(result["lengths"], dtype=np.int64).reshape(-1)
                combined = np.asarray(result["audio"], dtype=np.float32).reshape(-1)
            if sample_rate != SAMPLE_RATE:
                raise RuntimeError(
                    f"Chatterbox-Flash returned {sample_rate:,} Hz audio; expected {SAMPLE_RATE:,} Hz."
                )
            rows: list[np.ndarray] = []
            offset = 0
            for length in lengths:
                rows.append(combined[offset:offset + int(length)])
                offset += int(length)
            if len(rows) != len(texts) or offset != len(combined):
                raise RuntimeError("Chatterbox-Flash returned an invalid batch archive.")
            return rows
        finally:
            if output is not None:
                try:
                    output.unlink(missing_ok=True)
                except OSError:
                    pass
