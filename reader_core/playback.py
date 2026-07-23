from __future__ import annotations

import ctypes
from pathlib import Path


class MciPlayer:
    def __init__(self) -> None:
        self.alias = "kokoro_reader_audio"
        self.loaded = False
        self.paused_position = 0
        self.winmm = ctypes.windll.winmm

    def _send(self, command: str, result_size: int = 256) -> str:
        buffer = ctypes.create_unicode_buffer(result_size)
        code = self.winmm.mciSendStringW(command, buffer, result_size, None)
        if code:
            error = ctypes.create_unicode_buffer(256)
            self.winmm.mciGetErrorStringW(code, error, 256)
            raise RuntimeError(error.value or f"MCI error {code}")
        return buffer.value

    def open(self, path: Path) -> None:
        self.close()
        safe_path = str(path).replace('"', "")
        self._send(f'open "{safe_path}" type waveaudio alias {self.alias}')
        self._send(f"set {self.alias} time format milliseconds")
        self.loaded = True
        self.paused_position = 0

    def play(self, from_ms: int = 0) -> None:
        if self.loaded:
            self._send(f"play {self.alias} from {max(0, from_ms)}")

    def pause(self) -> None:
        if self.loaded and self.mode() == "playing":
            self.paused_position = self.position()
            self._send(f"pause {self.alias}")

    def resume(self) -> None:
        if self.loaded:
            self.play(self.paused_position)

    def stop(self) -> None:
        if self.loaded:
            try:
                self._send(f"stop {self.alias}")
                self._send(f"seek {self.alias} to start")
            except RuntimeError:
                pass
            self.paused_position = 0

    def close(self) -> None:
        if self.loaded:
            try:
                self._send(f"close {self.alias}")
            except RuntimeError:
                pass
        self.loaded = False
        self.paused_position = 0

    def position(self) -> int:
        if not self.loaded:
            return 0
        try:
            return int(self._send(f"status {self.alias} position") or 0)
        except (RuntimeError, ValueError):
            return 0

    def length(self) -> int:
        if not self.loaded:
            return 0
        try:
            return int(self._send(f"status {self.alias} length") or 0)
        except (RuntimeError, ValueError):
            return 0

    def mode(self) -> str:
        if not self.loaded:
            return "stopped"
        try:
            return self._send(f"status {self.alias} mode").lower()
        except RuntimeError:
            return "stopped"

