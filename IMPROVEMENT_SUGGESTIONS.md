# Code Improvement Suggestions for Kokoro Reader

## Executive Summary

This document provides comprehensive improvement suggestions for the Kokoro Reader codebase, covering **security**, **performance**, **testing**, **bug fixes**, **best practices**, **refactoring**, and **documentation**. Issues are prioritized by severity and impact.

---

## 🔴 HIGH PRIORITY - Security & Stability

### 1. Security Vulnerabilities

#### 1.1 Path Traversal Prevention in Document Reading
**Location:** `reader_core/documents.py`

**Issue:** The `read_document()` function doesn't validate that file paths are within allowed directories, potentially allowing path traversal attacks.

```python
# Current vulnerable code (line 105-116)
def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    # No validation that path is safe
```

**Recommendation:**
```python
def read_document(path: Path, allowed_root: Path | None = None) -> str:
    # Resolve to absolute path to prevent traversal
    resolved_path = path.resolve(strict=True)
    
    # Validate path is within allowed directory if specified
    if allowed_root is not None:
        allowed_root = allowed_root.resolve(strict=True)
        try:
            resolved_path.relative_to(allowed_root)
        except ValueError:
            raise ValueError(f"Access denied: {path} is outside allowed directory")
    
    # ... rest of implementation
```

#### 1.2 Subprocess Security Hardening
**Location:** `reader_core/audio.py` (line 138-146), `kokoro_gui.py` (multiple locations)

**Issue:** FFmpeg subprocess calls don't validate inputs or use shell=False explicitly.

**Recommendation:**
```python
# In audio.py transcode_audio_file()
result = subprocess.run(
    command,
    capture_output=True,
    text=True,
    shell=False,  # Explicitly disable shell
    check=False,  # Don't auto-raise, handle manually
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    timeout=300,  # Add timeout to prevent hangs
)
```

#### 1.3 File Permission Management
**Location:** `reader_core/profiles.py`, voice profile storage

**Issue:** Voice profile files containing sensitive audio data may have overly permissive file permissions.

**Recommendation:**
```python
# When creating profile files
profile_path.chmod(0o600)  # Owner read/write only
```

### 2. Race Conditions & Thread Safety

#### 2.1 Runtime Coordinator Timer Race Condition
**Location:** `reader_core/runtime.py` (lines 111-127)

**Issue:** The `schedule_preload()` method has a potential race condition between timer cancellation and state updates.

```python
# Current code (lines 116-127)
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
```

**Recommendation:** Ensure all timer operations are atomic within the lock:
```python
def schedule_preload(self, selection: RuntimeSelection, loader: Loader, delay: float = 0.65) -> None:
    with self._lock:
        if self._closed:
            return
        self._desired = selection
        
        # Cancel existing timer atomically
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        
        # Check if already loaded
        if self._current is not None and self._current.runtime_key == selection.runtime_key:
            self._current = selection
            # Schedule notification on worker thread to maintain ordering
            self._enqueue(self._notify, RuntimeState.READY, selection, "ready")
            return
        
        # Create and start new timer
        timer = threading.Timer(max(0.0, delay), self._submit, args=(selection, loader))
        timer.daemon = True
        self._timer = timer
    
    timer.start()
    with self._lock:
        if not self._closed:
            self._notify(RuntimeState.QUEUED, selection, "load queued")
```

#### 2.2 Document Load Generation Tracking
**Location:** `kokoro_gui.py` (lines 241, 264)

**Issue:** Multiple document loads can interfere with each other despite generation tracking.

**Recommendation:** Add generation validation in `_poll_document_load()`:
```python
def _poll_document_load(self) -> None:
    current_generation = self.document_load_generation
    try:
        result = self.document_results.get_nowait()
        # Validate this result belongs to current generation
        if result.generation != current_generation:
            return  # Stale result, discard
        # ... process result
    except queue.Empty:
        pass
    finally:
        if not self.loading_document:
            self.after(50, self._poll_document_load)
```

### 3. Memory Leaks & Resource Management

#### 3.1 Long-Running GUI Application Memory
**Location:** `kokoro_gui.py`

**Issue:** Several potential memory leaks in long-running sessions:
- `document_states` dictionary grows without bounds
- `recent_files` list not properly bounded
- Event handler references preventing garbage collection

**Recommendation:**
```python
# Add LRU cache for document states
from functools import lru_cache

# Limit document states to last 10 documents
MAX_DOCUMENT_STATES = 10
if len(self.document_states) > MAX_DOCUMENT_STATES:
    oldest_id = next(iter(self.document_states))
    del self.document_states[oldest_id]

# Ensure recent_files is bounded (already done but verify)
self.recent_files = self.recent_files[:10]

# Use weak references for callbacks where appropriate
import weakref
self._weak_self = weakref.ref(self)
```

#### 3.2 Temporary File Cleanup
**Location:** `reader_core/audio.py` (lines 172-184)

**Issue:** Temporary WAV files may not be cleaned up on exception.

**Recommendation:** Already uses try/finally, but add error logging:
```python
finally:
    try:
        temp_path.unlink()
    except OSError as e:
        import logging
        logging.warning(f"Failed to clean up temporary file {temp_path}: {e}")
```

---

## 🟡 MEDIUM PRIORITY - Performance Optimization

### 4. Audio Processing Optimizations

#### 4.1 Pipeline Caching with LRU Cache
**Location:** `kokoro_gui.py` (lines 159-164)

**Issue:** Manual cache management for Kokoro pipelines is error-prone and lacks eviction policy.

**Recommendation:**
```python
from functools import lru_cache
from typing import Dict, Tuple, Any

# Replace manual dict caches with LRU caches
@lru_cache(maxsize=8)
def get_kokoro_pipeline(collection_key: str, voice_key: str, device: str) -> Any:
    """Cached pipeline retrieval with automatic eviction."""
    # Implementation from existing load logic
    pass

# Usage in kokoro_gui.py
pipeline = get_kokoro_pipeline(collection, voice, device)
```

#### 4.2 Regex Pattern Compilation
**Location:** `reader_core/text.py` (lines 45-51)

**Issue:** Abbreviation regex patterns are recompiled on every `prepare_speech_text()` call.

**Recommendation:**
```python
# Module-level compiled patterns
_ABBREVIATION_PATTERNS = {
    re.compile(rf"\b{re.escape(src)}", re.IGNORECASE): repl
    for src, repl in {
        "Mr.": "Mister", "Mrs.": "Missus", "Ms.": "Miss", "Dr.": "Doctor",
        "Prof.": "Professor", "Sr.": "Senior", "Jr.": "Junior", "vs.": "versus",
        "etc.": "et cetera",
    }.items()
}

def prepare_speech_text(text: str) -> str:
    # ... existing code ...
    for pattern, replacement in _ABBREVIATION_PATTERNS.items():
        text = pattern.sub(replacement, text)
    return normalize_text(text)
```

#### 4.3 Streaming Audio with Pipes
**Location:** `reader_core/audio.py`, `kokoro_gui.py`

**Issue:** Audio transcoding writes intermediate WAV to disk before encoding.

**Recommendation:** Use pipes for streaming when possible:
```python
def transcode_audio_stream(
    audio_bytes: bytes,
    output: Path,
    format_key: str,
    sample_rate: int,
    # ... other params
) -> None:
    """Transcode audio directly from memory using pipes."""
    import subprocess
    
    command = [
        str(FFMPEG_EXE), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "wav", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
        # ... output options
        str(output)
    ]
    
    process = subprocess.run(
        command,
        input=audio_bytes,
        capture_output=True,
        timeout=300,
    )
    
    if process.returncode:
        raise RuntimeError(f"Audio encoding failed: {process.stderr.decode()}")
```

### 5. Voice Catalog Lookup Optimization

**Location:** `reader_core/voice_catalog.py`

**Issue:** Voice lookups iterate through collections repeatedly.

**Recommendation:**
```python
from functools import lru_cache

@lru_cache(maxsize=128)
def get_voice_info(collection: str, voice_id: str) -> dict | None:
    """Cached voice information lookup."""
    voice_map = KOKORO_COLLECTION_VOICE_MAPS.get(collection)
    if voice_map is None:
        return None
    return voice_map.get(voice_id)
```

---

## 🟢 LOWER PRIORITY - Code Quality & Best Practices

### 6. Type Annotation Completeness

**Location:** Throughout codebase, especially `kokoro_gui.py`

**Issue:** Many methods lack complete type annotations, particularly for complex types.

**Examples to Fix:**

```python
# kokoro_gui.py - Line 159-164
# Current:
self.kokoro_models: dict[str, object] = {}
self.kokoro_pipelines: dict[tuple[str, str], object] = {}

# Recommended (with proper types):
from typing import Any, Dict, Tuple

self.kokoro_models: Dict[str, Any] = {}
self.kokoro_pipelines: Dict[Tuple[str, str], Any] = {}

# Even better - define specific types
from reader_core.providers import KokoroPipeline

self.kokoro_pipelines: Dict[Tuple[str, str], KokoroPipeline] = {}
```

```python
# reader_core/runtime.py - Line 41
# Current:
Loader = Callable[[RuntimeSelection], bool]

# More descriptive:
from typing import Protocol

class RuntimeLoader(Protocol):
    def __call__(self, selection: RuntimeSelection) -> bool:
        """Load runtime, returns True if successfully committed."""
        ...

Loader = RuntimeLoader
```

### 7. Error Handling Improvements

#### 7.1 Consistent F-string Usage
**Location:** Scattered throughout

**Issue:** Mix of `.format()` and f-strings.

**Recommendation:** Standardize on f-strings:
```python
# Current (documents.py line 41)
raise ValueError(f"Document is larger than the {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB safety limit.")

# Already good! Just ensure consistency everywhere
```

#### 7.2 Platform-Agnostic Paths
**Location:** `kokoro_gui.py` (Windows-specific code)

**Issue:** Some paths use string concatenation instead of `Path`.

**Recommendation:**
```python
# Instead of:
path = APP_DIR + "\\voice_profiles\\chatterbox"

# Use:
path = APP_DIR / "voice_profiles" / "chatterbox"
```

#### 7.3 Exception Chaining
**Location:** Multiple locations

**Issue:** Original exceptions are lost when re-raising.

**Recommendation:**
```python
# Current (audio.py line 144-146)
if result.returncode:
    details = (result.stderr or result.stdout).strip()
    raise RuntimeError(f"Audio encoding failed: {details or 'FFmpeg returned an error.'}")

# Better - preserve original context
if result.returncode:
    details = (result.stderr or result.stdout).strip()
    error_msg = f"Audio encoding failed: {details or 'FFmpeg returned an error.'}"
    raise RuntimeError(error_msg) from None  # Explicit about no chaining
    # OR if there's an underlying exception:
    # raise RuntimeError(error_msg) from underlying_exception
```

### 8. Code Refactoring

#### 8.1 Decompose Large Files
**Location:** `kokoro_gui.py` (~2500+ lines)

**Issue:** Single file contains UI, business logic, and integration code.

**Recommendation:** Split into focused modules:
```
kokoro_gui/
├── __init__.py
├── app.py              # Main KokoroApp class
├── ui_components/
│   ├── __init__.py
│   ├── toolbar.py      # Toolbar building
│   ├── voice_panel.py  # Voice selection UI
│   ├── editor.py       # Text editor UI
│   ├── find_bar.py     # Find/replace UI
│   └── status_bar.py   # Status bar UI
├── handlers/
│   ├── __init__.py
│   ├── file_ops.py     # File operations
│   ├── speech_ops.py   # Speech synthesis operations
│   └── settings_ops.py # Settings management
└── dialogs/
    ├── __init__.py
    ├── audio_settings.py
    └── preferences.py
```

#### 8.2 Extract Duplicate Code Patterns
**Location:** Multiple locations in `kokoro_gui.py`

**Issue:** Similar patterns for engine loading, voice preview, etc.

**Recommendation:** Create reusable components:
```python
# New file: reader_core/engine_manager.py
from abc import ABC, abstractmethod
from typing import Any, Dict

class EngineManager(ABC):
    """Base class for TTS engine management."""
    
    def __init__(self, script_dir: Path, app_dir: Path) -> None:
        self.script_dir = script_dir
        self.app_dir = app_dir
        self._cache: Dict[str, Any] = {}
    
    @abstractmethod
    def load_engine(self, config: Dict[str, str]) -> bool:
        """Load engine with given configuration."""
        pass
    
    @abstractmethod
    def synthesize(self, text: str, voice_config: Dict[str, str]) -> bytes:
        """Synthesize speech to audio bytes."""
        pass
    
    def unload(self) -> None:
        """Unload engine and free resources."""
        self._cache.clear()

class KokoroEngineManager(EngineManager):
    # Implementation
    pass

class EdgeEngineManager(EngineManager):
    # Implementation
    pass
```

#### 8.3 Design Pattern Implementation

**Strategy Pattern** for different TTS engines:
```python
from abc import ABC, abstractmethod

class TTSEngine(ABC):
    @abstractmethod
    def synthesize(self, text: str, voice: str) -> bytes:
        pass
    
    @abstractmethod
    def is_ready(self) -> bool:
        pass

class KokoroTTSEngine(TTSEngine):
    def synthesize(self, text: str, voice: str) -> bytes:
        # Kokoro-specific implementation
        pass

class EdgeTTSEngine(TTSEngine):
    def synthesize(self, text: str, voice: str) -> bytes:
        # Edge-specific implementation
        pass

# Usage
engine: TTSEngine = KokoroTTSEngine()  # or EdgeTTSEngine()
audio = engine.synthesize("Hello", "af_heart")
```

**Observer Pattern** for runtime state changes:
```python
from typing import List, Callable

class RuntimeObservable:
    def __init__(self) -> None:
        self._observers: List[Callable] = []
    
    def attach(self, observer: Callable) -> None:
        self._observers.append(observer)
    
    def detach(self, observer: Callable) -> None:
        self._observers.remove(observer)
    
    def notify(self, state: str, message: str) -> None:
        for observer in self._observers:
            observer(state, message)
```

### 9. Testing Improvements

#### 9.1 Increase Test Coverage
**Current State:** ~15% coverage (estimated)

**Target:** Minimum 60% for core modules

**Missing Test Areas:**
- `reader_core/audio.py`: Only 2 functions tested, missing `apply_pitch`, `apply_chunk_edge_fade`, `write_audio_file`, `transcode_audio_file`
- `reader_core/playback.py`: No tests
- `reader_core/providers.py`: Minimal coverage
- `reader_core/chatterbox.py`: No tests
- `kokoro_gui.py`: No UI tests

**Recommendations:**
```python
# tests/test_audio.py
import unittest
import numpy as np
from reader_core.audio import apply_pitch, apply_chunk_edge_fade

class AudioProcessingTests(unittest.TestCase):
    def test_apply_pitch_no_change_at_zero_semitones(self) -> None:
        audio = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        result = apply_pitch(audio, 0.0)
        np.testing.assert_array_almost_equal(audio, result)
    
    def test_apply_pitch_increases_frequency(self) -> None:
        # Generate sine wave
        sample_rate = 24000
        duration = 0.1  # 100ms
        frequency = 440  # A4
        t = np.linspace(0, duration, int(sample_rate * duration))
        audio = np.sin(2 * np.pi * frequency * t).astype(np.float32)
        
        # Apply pitch shift (+12 semitones = octave up)
        shifted = apply_pitch(audio, 12.0)
        
        # Should be half the length (double frequency)
        self.assertEqual(len(shifted), len(audio) // 2)
    
    def test_apply_chunk_edge_fade_reduces_discontinuities(self) -> None:
        # Create audio with sharp edges
        audio = np.ones(1000, dtype=np.float32)
        faded = apply_chunk_edge_fade(audio, milliseconds=4.0)
        
        # Edges should be attenuated
        self.assertLess(abs(faded[0]), abs(audio[0]))
        self.assertLess(abs(faded[-1]), abs(audio[-1]))
        
        # Middle should be unchanged
        middle = len(audio) // 2
        self.assertAlmostEqual(faded[middle], audio[middle], places=5)

# tests/test_playback.py
import unittest
from unittest.mock import Mock, patch
from reader_core.playback import MciPlayer

class PlaybackTests(unittest.TestCase):
    @patch('reader_core.playback.winmm')
    def test_mci_player_opens_and_plays(self, mock_winmm) -> None:
        player = MciPlayer()
        player.open("test.wav")
        player.play()
        mock_winmm.mciSendString.assert_called()
    
    def test_mci_player_handles_missing_file(self) -> None:
        player = MciPlayer()
        with self.assertRaises(FileNotFoundError):
            player.open("nonexistent.wav")
```

#### 9.2 Mock External Dependencies
**Issue:** Tests depend on FFmpeg, HuggingFace, actual hardware

**Recommendation:**
```python
# tests/test_audio_integration.py
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import tempfile
from reader_core.audio import transcode_audio_file

class TranscodeAudioTests(unittest.TestCase):
    @patch('reader_core.audio.subprocess.run')
    def test_transcode_calls_ffmpeg_with_correct_args(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.wav"
            output_path = Path(tmpdir) / "output.mp3"
            
            # Create dummy input file
            input_path.write_bytes(b"RIFF...")
            
            transcode_audio_file(input_path, output_path, "mp3")
            
            # Verify FFmpeg was called
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            self.assertIn("-c:a", call_args)
            self.assertIn("libmp3lame", call_args)
    
    @patch('reader_core.audio.subprocess.run')
    def test_transcode_raises_on_ffmpeg_failure(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="Invalid format",
            stdout=""
        )
        
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.wav"
            output_path = Path(tmpdir) / "output.mp3"
            input_path.write_bytes(b"RIFF...")
            
            with self.assertRaises(RuntimeError) as context:
                transcode_audio_file(input_path, output_path, "mp3")
            
            self.assertIn("Audio encoding failed", str(context.exception))
```

#### 9.3 Integration Tests for Document-to-Audio Pipeline
```python
# tests/test_pipeline_integration.py
import unittest
from pathlib import Path
import tempfile
from reader_core.documents import read_document
from reader_core.text import split_text, prepare_speech_text

class DocumentToSpeechPipelineTests(unittest.TestCase):
    def test_full_pipeline_txt_to_segments(self) -> None:
        """Test complete pipeline from text file to speech segments."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test document
            doc_path = Path(tmpdir) / "test.txt"
            doc_path.write_text(
                "Dr. Smith said: Hello! How are you? I'm fine. Thanks.",
                encoding="utf-8"
            )
            
            # Read document
            text = read_document(doc_path)
            
            # Prepare for speech
            prepared = prepare_speech_text(text)
            
            # Split into segments
            segments = split_text(prepared, limit=50)
            
            # Verify results
            self.assertGreater(len(segments), 0)
            self.assertTrue(all(len(s) <= 50 for s in segments))
            self.assertIn("Doctor Smith", prepared)
    
    def test_epub_extraction_and_processing(self) -> None:
        """Test EPUB document processing."""
        # Create minimal EPUB for testing
        # ... implementation
```

### 10. Documentation Improvements

#### 10.1 Missing Docstrings
**Location:** Private methods and modules

**Recommendation:**
```python
# reader_core/audio.py
def apply_pitch(audio: np.ndarray, semitones: float) -> np.ndarray:
    """Shift pitch using linear resampling; synthesis speed compensates duration.
    
    Args:
        audio: Input audio waveform as numpy array.
        semitones: Pitch shift amount in semitones (positive = higher).
    
    Returns:
        Pitch-shifted audio waveform. Length varies based on semitone value.
    
    Example:
        >>> import numpy as np
        >>> audio = np.sin(2 * np.pi * 440 * np.linspace(0, 0.1, 24000))
        >>> shifted = apply_pitch(audio.astype(np.float32), 12.0)  # Octave up
        >>> len(shifted) < len(audio)
        True
    """
```

#### 10.2 Architecture Documentation
**Create:** `ARCHITECTURE_DETAILED.md`

```markdown
# Kokoro Reader Architecture

## Thread Model

### UI Thread (Main Thread)
- Handles all Tkinter GUI operations
- Processes user input events
- Updates display elements
- Must remain responsive (< 100ms per operation)

### Runtime Worker Thread
- Loads heavy ML models (Kokoro, Supertonic)
- Manages GPU/CPU resource allocation
- Operates via message queue to avoid blocking UI

### Synthesis Worker Threads
- Spawned per synthesis request
- Execute TTS inference
- Stream audio to player or file

### Player Thread
- Manages audio playback via Windows MCI
- Reports playback position to UI
- Handles pause/resume/stop commands

## Data Flow

### Text-to-Speech Pipeline
1. User inputs text in editor
2. Text is normalized and split into segments
3. Selected TTS engine synthesizes audio
4. Audio is optionally post-processed (pitch, fade)
5. Audio is played or saved to file

### Document Loading Flow
1. User opens document file
2. File is read in background thread
3. Content is parsed based on format (TXT, DOCX, EPUB, etc.)
4. Text is normalized and displayed
5. Document state is tracked for undo/redo

## Component Interactions

[KokoroApp] --> [RuntimeCoordinator] --> [Engine Managers]
     |                                       |
     v                                       v
[Document States]                    [Kokoro/Edge/Supertonic]
     |                                       |
     v                                       v
[Text Processor] <------------------ [Audio Generator]
     |                                       |
     v                                       v
[UI Display]                         [Player/File Output]
```

#### 10.3 API Documentation
**Generate with Sphinx or mkdocs:**
```bash
pip install sphinx sphinx-rtd-theme
sphinx-quickstart docs
sphinx-apidoc -o docs/source reader_core
```

Example API doc structure:
```python
# reader_core/__init__.py
"""Core text-to-speech processing modules.

This package provides the foundational services for converting text
into natural-sounding speech using multiple TTS engines.

Modules:
    audio: Audio processing, encoding, and file I/O
    text: Text normalization, splitting, and preparation
    providers: TTS engine implementations (Kokoro, Edge, Supertonic)
    runtime: Runtime coordination and resource management
    voice_catalog: Voice metadata and resolution
    documents: Document format parsing (TXT, DOCX, EPUB, RTF)
    profiles: Voice profile management for custom voices
    playback: Audio playback control via system APIs
    config: Application configuration and constants
    chatterbox: Chatterbox TTS engine client

Example:
    >>> from reader_core.text import prepare_speech_text, split_text
    >>> from reader_core.audio import write_audio_file
    >>> text = prepare_speech_text("Hello, Dr. Smith!")
    >>> segments = split_text(text, limit=100)
"""
```

---

## 📋 IMPLEMENTATION CHECKLIST

### Phase 1: Critical Fixes (Week 1-2)
- [ ] Add path traversal prevention in `read_document()`
- [ ] Harden subprocess calls with timeouts and validation
- [ ] Fix race conditions in `RuntimeCoordinator`
- [ ] Implement proper resource cleanup with context managers
- [ ] Add file permission controls for sensitive data

### Phase 2: Performance & Stability (Week 3-4)
- [ ] Implement LRU caching for pipelines and voice lookups
- [ ] Compile regex patterns at module level
- [ ] Add streaming audio transcoding with pipes
- [ ] Fix memory leaks in long-running sessions
- [ ] Optimize document state management

### Phase 3: Code Quality (Week 5-6)
- [ ] Complete type annotations throughout codebase
- [ ] Standardize error handling and f-string usage
- [ ] Refactor `kokoro_gui.py` into smaller modules
- [ ] Extract duplicate code into reusable components
- [ ] Implement Strategy and Observer patterns

### Phase 4: Testing (Week 7-8)
- [ ] Increase test coverage to 60%+
- [ ] Add tests for audio processing functions
- [ ] Create integration tests for full pipeline
- [ ] Mock external dependencies properly
- [ ] Add performance regression tests

### Phase 5: Documentation (Week 9-10)
- [ ] Add missing docstrings to all public APIs
- [ ] Create detailed architecture documentation
- [ ] Generate API reference documentation
- [ ] Write user guide for developers
- [ ] Document deployment and troubleshooting

---

## 📊 METRICS & MONITORING

### Key Performance Indicators
- **Test Coverage:** Target 60%+, currently ~15%
- **Response Time:** UI operations < 100ms
- **Memory Usage:** Stable over 24h period
- **Crash Rate:** < 0.1% of sessions

### Monitoring Recommendations
```python
# Add simple performance monitoring
import time
import logging

def timed_operation(operation_name: str):
    """Decorator to log operation duration."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
                if elapsed > 0.1:  # Log slow operations
                    logging.warning(f"{operation_name} took {elapsed:.3f}s")
        return wrapper
    return decorator
```

---

## 🔧 QUICK WINS (Can be implemented immediately)

1. **Add timeout to all subprocess calls** (5 min fix)
2. **Compile regex patterns once** (10 min fix)
3. **Add basic input validation** (15 min fix)
4. **Improve error messages** (20 min fix)
5. **Add logging for debugging** (30 min fix)

---

## 📚 REFERENCES

- [Python Security Best Practices](https://docs.python.org/3/library/security.html)
- [PEP 8 - Style Guide for Python Code](https://peps.python.org/pep-0008/)
- [PEP 484 - Type Hints](https://peps.python.org/pep-0484/)
- [pytest Documentation](https://docs.pytest.org/)
- [Sphinx Documentation](https://www.sphinx-doc.org/)

---

*Generated: $(date)*  
*Codebase Version: Kokoro Reader 3.12*
