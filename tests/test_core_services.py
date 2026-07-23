from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from reader_core.audio import ffmpeg_audio_output_args, format_key_for_path
from reader_core.chatterbox import ChatterboxFlashClient
from reader_core.config import APP_DIR, FFMPEG_EXE, SCRIPT_DIR, VOICE_MAP
from reader_core.documents import read_document
from reader_core.profiles import (
    ChatterboxProfile,
    create_chatterbox_profile,
    delete_chatterbox_profile,
)
from reader_core.providers import resolve_kokoro_voice
from reader_core.runtime import RuntimeCoordinator, RuntimeSelection, RuntimeState
from reader_core.text import next_text_segment, prepare_speech_text, split_text
from reader_core.voice_catalog import (
    KOKORO_COLLECTION_VOICE_MAPS,
    kokoro_language_names,
    kokoro_model_repository,
    kokoro_voice_names,
    split_stable_kokoro_voice_id,
    stable_kokoro_voice_id,
)
from reader_ui.batch import BatchWorkspaceMixin


class TextServiceTests(unittest.TestCase):
    def test_prepare_and_split_preserve_readable_sentences(self) -> None:
        prepared = prepare_speech_text("Dr. Smith paid $20.  Next sentence.")
        self.assertIn("Doctor Smith paid 20 dollars.", prepared)
        self.assertEqual(split_text(prepared, limit=40), [
            "Doctor Smith paid 20 dollars.",
            "Next sentence.",
        ])

    def test_next_segment_reports_source_offsets(self) -> None:
        source = "First sentence. Second sentence."
        segment = next_text_segment(source, 0, limit=18)
        self.assertIsNotNone(segment)
        text, start, end, next_offset = segment or ("", 0, 0, 0)
        self.assertEqual(text, "First sentence.")
        self.assertEqual(source[start:end], text)
        self.assertGreaterEqual(next_offset, end)


class AudioServiceTests(unittest.TestCase):
    def test_ffmpeg_arguments_are_bounded_and_provider_independent(self) -> None:
        options = ffmpeg_audio_output_args(
            "mp3", 24000, bitrate=999, bitrate_mode="invalid", vbr_quality=99,
        )
        self.assertIn("320k", options)
        self.assertIn("24000", options)
        self.assertEqual(format_key_for_path(Path("speech.m4a")), "aac")

    def test_unknown_format_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ffmpeg_audio_output_args("unknown", 24000)


class DocumentServiceTests(unittest.TestCase):
    def test_plain_text_document_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.txt"
            path.write_text("hello\nworld", encoding="utf-8")
            self.assertEqual(read_document(path), "hello\nworld")


class BoundaryTests(unittest.TestCase):
    def test_application_root_is_parent_of_packages(self) -> None:
        expected = Path(__file__).resolve().parents[1]
        self.assertEqual(SCRIPT_DIR, expected)

    def test_voice_catalog_uses_stable_kokoro_ids(self) -> None:
        self.assertIn("af_heart", VOICE_MAP.values())
        self.assertEqual(len(VOICE_MAP.values()), len(set(VOICE_MAP.values())))

    def test_additional_kokoro_collections_are_complete(self) -> None:
        self.assertEqual(len(KOKORO_COLLECTION_VOICE_MAPS["official_v1_1_zh"]), 103)
        self.assertEqual(
            len(kokoro_voice_names("official_v1_1_zh", "Mandarin Chinese")), 100,
        )
        self.assertEqual(len(KOKORO_COLLECTION_VOICE_MAPS["gushi_labs"]), 2)
        self.assertEqual(len(KOKORO_COLLECTION_VOICE_MAPS["sethblocks"]), 5)
        self.assertEqual(
            kokoro_language_names("official_v1_1_zh"),
            ["American English", "British English", "Mandarin Chinese"],
        )

    def test_collection_voice_ids_round_trip_without_decorations(self) -> None:
        stable_id = stable_kokoro_voice_id("gushi_labs", "af_vivien")
        self.assertEqual(stable_id, "gushi_labs::af_vivien")
        self.assertEqual(
            split_stable_kokoro_voice_id(stable_id), ("gushi_labs", "af_vivien"),
        )
        self.assertEqual(
            kokoro_model_repository("gushi_labs"), "hexgrad/Kokoro-82M",
        )

    @patch("huggingface_hub.hf_hub_download", return_value="cached/af_vivien.pt")
    def test_community_voice_resolution_uses_its_voice_repository(self, download) -> None:
        resolved = resolve_kokoro_voice("gushi_labs", "af_vivien")
        self.assertEqual(resolved, "cached/af_vivien.pt")
        download.assert_called_once_with(
            repo_id="gushilabs/gushilabs-voices-for-kokoro-v1",
            filename="voices/af_vivien.pt",
        )

    def test_chatterbox_client_starts_without_a_process(self) -> None:
        client = ChatterboxFlashClient(Path.cwd(), APP_DIR)
        self.assertFalse(client.is_ready)

    def test_batch_output_planner_avoids_existing_names(self) -> None:
        workspace = object.__new__(BatchWorkspaceMixin)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "chapter.wav").touch()
            planned, conflicts = workspace._plan_batch_outputs(
                [Path("chapter.txt"), Path("chapter.md")], output_dir, ".wav",
            )
            self.assertEqual([path.name for _, path in planned], [
                "chapter (2).wav",
                "chapter (3).wav",
            ])
            self.assertEqual(len(conflicts), 2)


class RuntimeCoordinatorTests(unittest.TestCase):
    def test_rapid_preloads_coalesce_to_latest_selection(self) -> None:
        completed = threading.Event()
        loaded: list[int] = []
        statuses: list[RuntimeState] = []

        def status(state, _selection, _message) -> None:
            statuses.append(state)
            if state == RuntimeState.READY:
                completed.set()

        coordinator = RuntimeCoordinator(status)
        first = RuntimeSelection("kokoro", "official_v1", "a", "af_heart", revision=1)
        second = RuntimeSelection("kokoro", "gushi_labs", "a", "af_vivien", revision=2)

        def loader(selection: RuntimeSelection) -> bool:
            loaded.append(selection.revision)
            return coordinator.is_desired(selection)

        coordinator.schedule_preload(first, loader, delay=0.05)
        coordinator.schedule_preload(second, loader, delay=0.01)
        self.assertTrue(completed.wait(1.0))
        self.assertEqual(loaded, [2])
        self.assertEqual(coordinator.current, second)
        self.assertIn(RuntimeState.QUEUED, statuses)
        coordinator.close()

    def test_scheduling_does_not_wait_for_running_loader(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        coordinator = RuntimeCoordinator()
        first = RuntimeSelection("kokoro", "official_v1", "a", "af_heart", revision=1)
        second = RuntimeSelection("supertonic", language="en", voice_id="F5", revision=2)

        def slow_loader(selection: RuntimeSelection) -> bool:
            if selection == first:
                entered.set()
                release.wait(1.0)
            return coordinator.is_desired(selection)

        coordinator.schedule_preload(first, slow_loader, delay=0.0)
        self.assertTrue(entered.wait(1.0))
        started = time.perf_counter()
        coordinator.schedule_preload(second, slow_loader, delay=0.0)
        elapsed = time.perf_counter() - started
        release.set()
        self.assertLess(elapsed, 0.05)
        deadline = time.monotonic() + 1.0
        while coordinator.current != second and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(coordinator.current, second)
        coordinator.close()

    def test_online_provider_activates_before_deferred_gpu_cleanup(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        cleaned = threading.Event()
        coordinator = RuntimeCoordinator()
        local = RuntimeSelection("kokoro", "gushi_labs", "a", "af_vivien", revision=1)
        online = RuntimeSelection("edge", language="en-US", voice_id="en-US-AvaNeural", revision=2)

        def slow_loader(selection: RuntimeSelection) -> bool:
            entered.set()
            release.wait(1.0)
            return coordinator.is_desired(selection)

        coordinator.schedule_preload(local, slow_loader, delay=0.0)
        self.assertTrue(entered.wait(1.0))
        coordinator.activate(online, cleaned.set)
        self.assertEqual(coordinator.current, online)
        self.assertFalse(cleaned.is_set())
        release.set()
        self.assertTrue(cleaned.wait(1.0))
        coordinator.close()

    def test_loader_failure_is_reported_without_becoming_current(self) -> None:
        failed = threading.Event()
        errors: list[str] = []
        selection = RuntimeSelection("supertonic", language="en", voice_id="F5", revision=1)

        def on_error(_selection: RuntimeSelection, error: Exception) -> None:
            errors.append(str(error))
            failed.set()

        coordinator = RuntimeCoordinator(error_callback=on_error)
        coordinator.schedule_preload(
            selection,
            lambda _selection: (_ for _ in ()).throw(RuntimeError("load failed")),
            delay=0.0,
        )
        self.assertTrue(failed.wait(1.0))
        self.assertEqual(errors, ["load failed"])
        self.assertIsNone(coordinator.current)
        coordinator.close()


class ChatterboxProfileTests(unittest.TestCase):
    def test_profile_extracts_bounded_managed_excerpt_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            samples = np.zeros(12 * 24000, dtype=np.float32)
            samples[2 * 24000:7 * 24000] = 0.1
            sf.write(source, samples, 24000)
            profile = create_chatterbox_profile(
                root / "profiles", FFMPEG_EXE, "Narrator", source, 2.0, 5.0,
            )
            info = sf.info(profile.excerpt_path)
            self.assertEqual(info.samplerate, 24000)
            self.assertEqual(info.channels, 1)
            self.assertAlmostEqual(info.duration, 5.0, places=1)
            delete_chatterbox_profile(root / "profiles", profile)
            self.assertTrue(source.is_file())
            self.assertFalse(Path(profile.excerpt_path).exists())

    def test_profile_metadata_clamps_duration(self) -> None:
        profile = ChatterboxProfile.from_dict({
            "profile_id": "abc",
            "name": "Test",
            "excerpt_path": "reference.wav",
            "duration_seconds": 99,
        })
        self.assertIsNotNone(profile)
        self.assertEqual(profile.duration_seconds if profile else 0, 10.0)


if __name__ == "__main__":
    unittest.main()
