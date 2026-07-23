from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import queue
import threading
from typing import Callable, TypeVar


class RuntimeState(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"
    STOPPING = "stopping"


@dataclass(frozen=True)
class RuntimeSelection:
    provider: str
    collection: str = ""
    language: str = ""
    voice_id: str = ""
    profile_id: str = ""
    revision: int = 0

    @property
    def runtime_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.provider,
            self.collection,
            self.language,
            self.voice_id,
            self.profile_id,
        )


StatusCallback = Callable[[RuntimeState, RuntimeSelection | None, str], None]
ErrorCallback = Callable[[RuntimeSelection, Exception], None]
Loader = Callable[[RuntimeSelection], bool]
T = TypeVar("T")


class RuntimeCoordinator:
    """Coalesce model loads and keep heavy work off the UI thread.

    A loader returns True only when it committed the requested runtime.  It
    should call :meth:`is_desired` before publishing a costly, newly-created
    object so stale selections cannot replace the current runtime.
    """

    def __init__(
        self,
        status_callback: StatusCallback | None = None,
        error_callback: ErrorCallback | None = None,
    ) -> None:
        self._status_callback = status_callback
        self._error_callback = error_callback
        self._lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._jobs: queue.Queue[tuple[Callable[..., object], tuple[object, ...]] | None] = queue.Queue()
        self._worker = threading.Thread(
            target=self._work_loop, daemon=True, name="tts-runtime",
        )
        self._timer: threading.Timer | None = None
        self._desired: RuntimeSelection | None = None
        self._current: RuntimeSelection | None = None
        self._state = RuntimeState.IDLE
        self._closed = False
        self._worker.start()

    def _work_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            callback, args = job
            try:
                callback(*args)
            except Exception:
                # Load paths report their own failures; cleanup must never kill
                # the serialized runtime worker.
                pass

    def _enqueue(self, callback: Callable[..., object], *args: object) -> None:
        with self._lock:
            if not self._closed:
                self._jobs.put((callback, args))

    @property
    def state(self) -> RuntimeState:
        with self._lock:
            return self._state

    @property
    def current(self) -> RuntimeSelection | None:
        with self._lock:
            return self._current

    def is_desired(self, selection: RuntimeSelection) -> bool:
        with self._lock:
            return not self._closed and self._desired == selection

    def _notify(self, state: RuntimeState, selection: RuntimeSelection | None, message: str) -> None:
        with self._lock:
            self._state = state
        if self._status_callback is not None:
            self._status_callback(state, selection, message)

    def schedule_preload(self, selection: RuntimeSelection, loader: Loader, delay: float = 0.65) -> None:
        with self._lock:
            if self._closed:
                return
            self._desired = selection
            if self._timer is not None:
                self._timer.cancel()
            if self._current is not None and self._current.runtime_key == selection.runtime_key:
                self._current = selection
                self._timer = None
                self._notify(RuntimeState.READY, selection, "ready")
                return
            timer = threading.Timer(max(0.0, delay), self._submit, args=(selection, loader))
            timer.daemon = True
            self._timer = timer
            self._notify(RuntimeState.QUEUED, selection, "load queued")
            timer.start()

    def activate(
        self,
        selection: RuntimeSelection,
        deferred_cleanup: Callable[[], None] | None = None,
    ) -> None:
        """Activate a provider that needs no local model, then clean up in order."""
        with self._lock:
            if self._closed:
                return
            self._desired = selection
            self._current = selection
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._notify(RuntimeState.READY, selection, "ready")
        if deferred_cleanup is not None:
            self._enqueue(self._run_deferred_cleanup, selection, deferred_cleanup)

    def _run_deferred_cleanup(
        self,
        selection: RuntimeSelection,
        cleanup: Callable[[], None],
    ) -> None:
        with self._operation_lock:
            if self.is_desired(selection):
                cleanup()

    def _submit(self, selection: RuntimeSelection, loader: Loader) -> None:
        with self._lock:
            if self._closed or self._desired != selection:
                return
            self._timer = None
        self._enqueue(self._run_load, selection, loader)

    def _run_load(self, selection: RuntimeSelection, loader: Loader) -> bool:
        with self._operation_lock:
            if not self.is_desired(selection):
                return False
            self._notify(RuntimeState.LOADING, selection, "loading")
            try:
                committed = bool(loader(selection))
            except Exception as exc:
                if self._error_callback is not None:
                    self._error_callback(selection, exc)
                if self.is_desired(selection):
                    self._notify(RuntimeState.FAILED, selection, str(exc))
                return False
            if committed and self.is_desired(selection):
                with self._lock:
                    self._current = selection
                self._notify(RuntimeState.READY, selection, "ready")
                return True
            return False

    def ensure_current(self, selection: RuntimeSelection, loader: Loader) -> bool:
        """Synchronously ensure a runtime from a non-UI synthesis worker."""
        with self._lock:
            if self._closed:
                raise RuntimeError("The speech runtime is stopping.")
            self._desired = selection
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._current is not None and self._current.runtime_key == selection.runtime_key:
                self._current = selection
                return True
        if not self._run_load(selection, loader):
            raise RuntimeError("The selected speech runtime was superseded before it became ready.")
        return True

    def run(self, selection: RuntimeSelection, loader: Loader, action: Callable[[], T]) -> T:
        """Ensure and use a runtime without allowing a replacement mid-call."""
        with self._operation_lock:
            self.ensure_current(selection, loader)
            return action()

    def invalidate(self, provider: str | None = None) -> None:
        with self._lock:
            if provider is None or (self._current and self._current.provider == provider):
                self._current = None
                if not self._closed:
                    self._state = RuntimeState.IDLE

    def release(self, releaser: Callable[[], None] | None = None) -> None:
        with self._operation_lock:
            if releaser is not None:
                releaser()
            self.invalidate()

    def close(self, releaser: Callable[[], None] | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._state = RuntimeState.STOPPING
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if releaser is not None:
            with self._operation_lock:
                releaser()
        self._jobs.put(None)
