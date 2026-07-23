from __future__ import annotations

import asyncio
import csv
import gc
import json
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
import traceback
from collections import deque
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np
import soundfile as sf
from tkinterdnd2 import DND_FILES, TkinterDnD

from reader_core.audio import (
    apply_chunk_edge_fade,
    apply_pitch,
    ffmpeg_audio_output_args,
    format_key_for_path,
    format_time,
    normalized_path_key,
    write_audio_file,
)
from reader_core.chatterbox import ChatterboxFlashClient
from reader_core.config import (
    APP_DIR,
    APP_NAME,
    APP_VERSION,
    AUDIO_BITRATES,
    AUDIO_FILE_TYPES,
    AUDIO_FORMATS,
    AUDIO_LABEL_TO_KEY,
    CHATTERBOX_FLASH_LANG_MAP,
    CHATTERBOX_FLASH_PREFERRED_ORDER,
    CHATTERBOX_FLASH_PREFERRED_VOICES,
    CHATTERBOX_FLASH_VOICE_MAP,
    EDGE_FALLBACK_USE_CASES,
    EDGE_FALLBACK_VOICES,
    EDGE_PREFERRED_ORDER,
    ENGINE_LABEL_BY_KEY,
    ENGINE_MAP,
    FFMPEG_EXE,
    KOKORO_PREFERRED_ORDER,
    KOKORO_PREFERRED_VOICES,
    OPEN_FILE_TYPES,
    PORTABLE_MODE,
    PREVIEW_PATH,
    SAMPLE_RATE,
    SCRIPT_DIR,
    SETTINGS_PATH,
    STREAM_PATH,
    SUPERTONIC_LANG_MAP,
    SUPERTONIC_PREFERRED_ORDER,
    SUPERTONIC_PREFERRED_VOICES,
    SUPERTONIC_VOICE_MAP,
    SUPPORTED_DOCUMENT_EXTENSIONS,
    VOICE_USE_CASE_KEYS,
    VOICE_USE_CASE_OPTIONS,
    VOICE_VIEW_OPTIONS,
)
from reader_core.documents import read_document
from reader_core.playback import MciPlayer
from reader_core.profiles import (
    ChatterboxProfile,
    create_chatterbox_profile,
    delete_chatterbox_profile,
    extract_chatterbox_excerpt,
)
from reader_core.providers import (
    edge_voice_label,
    load_edge_runtime,
    load_kokoro_runtime,
    load_supertonic_runtime,
    loaded_kokoro_torch,
    resolve_kokoro_voice,
    stream_edge_audio_bytes,
    synthesize_edge_audio,
)
from reader_core.text import (
    next_text_segment,
    prepare_speech_text,
    rate_to_speed,
    safe_float,
    safe_int,
    split_text,
)
from reader_core.runtime import RuntimeCoordinator, RuntimeSelection, RuntimeState
from reader_core.voice_catalog import (
    KOKORO_COLLECTION_LABELS,
    KOKORO_DEFAULT_COLLECTION,
    kokoro_collection_key,
    kokoro_collection_label,
    kokoro_language_code,
    kokoro_language_names,
    kokoro_model_repository,
    kokoro_voice_id,
    kokoro_voice_names,
    normalize_kokoro_collection,
    split_stable_kokoro_voice_id,
    stable_kokoro_voice_id,
)
from reader_ui.batch import BatchWorkspaceMixin
from reader_ui.models import DocumentState


class KokoroApp(BatchWorkspaceMixin, TkinterDnD.Tk):
    def __init__(self) -> None:
        super().__init__()
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.settings = self._load_settings()
        saved_glossary = self.settings.get("glossary", [])
        self.glossary: list[dict[str, str]] = [
            {"source": str(entry.get("source", "")), "replacement": str(entry.get("replacement", ""))}
            for entry in saved_glossary if isinstance(entry, dict)
        ] if isinstance(saved_glossary, list) else []
        saved_recent_files = self.settings.get("recent_files", [])
        self.recent_files: list[str] = [
            str(path) for path in saved_recent_files if isinstance(path, str)
        ][:10] if isinstance(saved_recent_files, list) else []
        self.output_format = str(self.settings.get("output_format", "mp3"))
        if self.output_format not in AUDIO_FORMATS:
            self.output_format = "mp3"
        self.output_bitrate = safe_int(self.settings.get("output_bitrate"), 128)
        if self.output_bitrate not in AUDIO_BITRATES:
            self.output_bitrate = 128
        self.output_bitrate_mode = str(self.settings.get("output_bitrate_mode", "CBR"))
        if self.output_bitrate_mode not in {"CBR", "ABR", "VBR"}:
            self.output_bitrate_mode = "CBR"
        self.output_vbr_quality = max(0, min(9, safe_int(self.settings.get("output_vbr_quality"), 2)))
        self.output_sample_rate = str(self.settings.get("output_sample_rate", "Voice native"))
        if self.output_sample_rate not in {"Voice native", "16000", "22050", "24000", "44100", "48000"}:
            self.output_sample_rate = "Voice native"
        self.output_channels = str(self.settings.get("output_channels", "Mono"))
        if self.output_channels not in {"Mono", "Stereo"}:
            self.output_channels = "Mono"
        self.codec_effort = max(0, min(12, safe_int(self.settings.get("codec_effort"), 8)))
        self.current_path: Path | None = None
        self.dirty = False
        self.document_states: dict[str, DocumentState] = {}
        self.active_document_id: str | None = None
        self.pending_document_loads: deque[tuple[str, Path]] = deque()
        self.document_load_in_progress = False
        self.batch_queue: list[Path] = []
        self.batch_queue_status: dict[str, str] = {}
        self.batch_queue_window: tk.Toplevel | None = None
        self.batch_queue_tree: ttk.Treeview | None = None
        self.tab_drag_index: int | None = None
        self.running = False
        self.cancel_event = threading.Event()
        self.kokoro_models: dict[str, object] = {}
        self.kokoro_pipelines: dict[tuple[str, str], object] = {}
        self.kokoro_pipeline_lock = threading.RLock()
        self.kokoro_preloading: set[tuple[str, str, str]] = set()
        self.kokoro_loaded_voices: set[tuple[str, str, str]] = set()
        self.kokoro_voice_tensors: dict[tuple[str, str, str], object] = {}
        self.pipeline_device: str | None = None
        self.supertonic_tts: object | None = None
        self.supertonic_styles: dict[str, object] = {}
        self.chatterbox_flash_device: str | None = None
        self.chatterbox_flash_preloading = False
        self.chatterbox_reference_audio = str(self.settings.get("chatterbox_reference_audio", ""))
        self.chatterbox_profile_root = APP_DIR / "voice_profiles" / "chatterbox"
        raw_profiles = self.settings.get("chatterbox_profiles", [])
        loaded_profiles = [
            ChatterboxProfile.from_dict(item) for item in raw_profiles if isinstance(item, dict)
        ] if isinstance(raw_profiles, list) else []
        self.chatterbox_profiles: dict[str, ChatterboxProfile] = {
            profile.profile_id: profile for profile in loaded_profiles if profile is not None
        }
        saved_profile_id = str(self.settings.get("chatterbox_profile_id", ""))
        self.chatterbox_profile_id = saved_profile_id if saved_profile_id in self.chatterbox_profiles else ""
        self.chatterbox_profile_display_to_id: dict[str, str] = {}
        self.chatterbox_client = ChatterboxFlashClient(SCRIPT_DIR, APP_DIR)
        self.edge_voices: list[dict[str, object]] = [dict(item) for item in EDGE_FALLBACK_VOICES]
        self.edge_voice_by_label: dict[str, dict[str, object]] = {}
        self._rebuild_edge_voice_indexes()
        self.edge_catalog_loading = False
        self.pending_edge_voice = ""
        selections = self.settings.get("engine_selections", {})
        self.engine_selections: dict[str, dict[str, str]] = (
            {str(key): dict(value) for key, value in selections.items() if isinstance(value, dict)}
            if isinstance(selections, dict) else {}
        )
        source_selections = self.settings.get("kokoro_collection_selections", {})
        self.kokoro_collection_selections: dict[str, dict[str, str]] = (
            {
                normalize_kokoro_collection(str(key)): {
                    str(field): str(value) for field, value in selection.items()
                    if field in {"language", "voice"}
                }
                for key, selection in source_selections.items()
                if isinstance(selection, dict)
            }
            if isinstance(source_selections, dict) else {}
        )
        saved_collection = self.engine_selections.get("kokoro", {}).get(
            "collection", str(self.settings.get("kokoro_collection", KOKORO_DEFAULT_COLLECTION)),
        )
        self._active_kokoro_collection = normalize_kokoro_collection(saved_collection)
        saved_favorites = self.settings.get("voice_favorites", {})
        self.voice_favorites: dict[str, list[str]] = {
            engine: list(dict.fromkeys(
                str(voice_id) for voice_id in (
                    saved_favorites.get(engine, [])
                    if isinstance(saved_favorites.get(engine, []), (list, tuple)) else []
                ) if voice_id
            ))
            for engine in ENGINE_LABEL_BY_KEY
        } if isinstance(saved_favorites, dict) else {engine: [] for engine in ENGINE_LABEL_BY_KEY}
        saved_voice_view = str(self.settings.get("voice_view", VOICE_VIEW_OPTIONS[0]))
        saved_use_case = str(self.settings.get("voice_use_case", VOICE_USE_CASE_OPTIONS[0]))
        self.voice_view_var = tk.StringVar(
            value=saved_voice_view if saved_voice_view in VOICE_VIEW_OPTIONS else VOICE_VIEW_OPTIONS[0]
        )
        self.voice_use_case_var = tk.StringVar(
            value=saved_use_case if saved_use_case in VOICE_USE_CASE_OPTIONS else VOICE_USE_CASE_OPTIONS[0]
        )
        self.visible_voice_names: list[str] = []
        self.voice_display_to_name: dict[str, str] = {}
        self.player = MciPlayer()
        self.reading_start_offset = 0
        self.reading_text = ""
        self.last_player_mode = "stopped"
        self.stream_active = False
        self.stream_next_offset = 0
        self.stream_end_offset = 0
        self.stream_segment_number = 0
        self.stream_generation = 0
        self.stream_voice_settings: dict[str, object] | None = None
        self.find_bar_visible = False
        self.stats_update_after_id: str | None = None
        self.document_load_generation = 0
        self.loading_document = False
        self.document_results: queue.Queue = queue.Queue()
        self.selection_revision = 0
        self.settings_save_after_id: str | None = None
        self.runtime_loading = False
        self.preload_after_speech = False
        self.runtime_coordinator = RuntimeCoordinator(
            self._runtime_status_changed, self._runtime_load_failed,
        )

        self.title(APP_NAME)
        saved_geometry = self.settings.get("geometry", "1180x800")
        self.geometry(saved_geometry if isinstance(saved_geometry, str) else "1180x800")
        self.minsize(960, 650)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.option_add("*tearOff", False)
        self._configure_style()
        self._build_ui()
        self._bind_shortcuts()
        self._insert_initial_text()
        self.after_idle(self._enable_file_drop)
        self.after(1000, self._schedule_runtime_preload)
        self.after(50, self._poll_document_load)
        self.after(250, self._poll_player)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.option_add("*Font", "{Segoe UI} 10")
        self.option_add("*TCombobox*Listbox.font", "{Segoe UI} 10")
        style.configure("TButton", padding=(6, 2))
        style.configure("Toolbar.TButton", padding=(6, 3))
        style.configure("Primary.TButton", padding=(9, 4), font=("Segoe UI Semibold", 10))
        style.configure("Favorite.TButton", padding=(5, 2))
        style.configure("Panel.TLabelframe", padding=(7, 4))
        style.configure("Panel.TLabelframe.Label", font=("Segoe UI Semibold", 10))
        style.configure("Muted.TLabel", foreground="#586274")
        style.configure("Status.TLabel", padding=(4, 1))

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self._build_menu()
        self._build_toolbar()
        self._build_voice_panel()
        self._build_editor()
        self._build_find_bar()
        self._build_status_bar()

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        self.config(menu=menu)

        file_menu = tk.Menu(menu)
        menu.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="New", accelerator="Ctrl+N", command=self.new_document)
        file_menu.add_command(label="Open…", accelerator="Ctrl+O", command=self.open_document)
        self.recent_menu = tk.Menu(file_menu)
        file_menu.add_cascade(label="Recent Files", menu=self.recent_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.save_document)
        file_menu.add_command(label="Save As…", accelerator="Ctrl+Shift+S", command=lambda: self.save_document(True))
        file_menu.add_separator()
        file_menu.add_command(label="Export Audio…", accelerator="Ctrl+E", command=self.export_audio)
        file_menu.add_command(label="Audio Output Settings…", command=self.open_audio_settings)
        file_menu.add_command(label="Batch Convert Files…", command=self.batch_convert)
        file_menu.add_command(label="Reset App Preferences…", command=self.reset_app_preferences)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)

        edit_menu = tk.Menu(menu)
        menu.add_cascade(label="Edit", menu=edit_menu)
        edit_menu.add_command(label="Undo", accelerator="Ctrl+Z", command=lambda: self.text.event_generate("<<Undo>>"))
        edit_menu.add_command(label="Redo", accelerator="Ctrl+Y", command=lambda: self.text.event_generate("<<Redo>>"))
        edit_menu.add_separator()
        edit_menu.add_command(label="Cut", accelerator="Ctrl+X", command=lambda: self.text.event_generate("<<Cut>>"))
        edit_menu.add_command(label="Copy", accelerator="Ctrl+C", command=lambda: self.text.event_generate("<<Copy>>"))
        edit_menu.add_command(label="Paste", accelerator="Ctrl+V", command=lambda: self.text.event_generate("<<Paste>>"))
        edit_menu.add_command(label="Select All", accelerator="Ctrl+A", command=self.select_all)
        edit_menu.add_separator()
        edit_menu.add_command(label="Find / Replace", accelerator="Ctrl+F", command=self.show_find_bar)
        edit_menu.add_command(label="Go to Line…", accelerator="Ctrl+G", command=self.go_to_line)

        speech_menu = tk.Menu(menu)
        menu.add_cascade(label="Speech", menu=speech_menu)
        speech_menu.add_command(label="Read from Cursor", accelerator="F5", command=self.read_all)
        speech_menu.add_command(label="Read Selection", accelerator="F6", command=self.read_selection)
        speech_menu.add_separator()
        speech_menu.add_command(label="Pause", accelerator="F7", command=self.pause_speech)
        speech_menu.add_command(label="Resume", accelerator="F8", command=self.resume_speech)
        speech_menu.add_command(label="Stop", accelerator="F9", command=self.stop_speech)

        voice_menu = tk.Menu(menu, tearoff=True)
        self.voice_menu = voice_menu
        menu.add_cascade(label="Voice", menu=voice_menu)
        voice_menu.add_command(label="Preview Voice", command=self.preview_voice)
        voice_menu.add_command(label="New Chatterbox Voice Profile…", command=self.create_chatterbox_profile)
        voice_menu.add_command(label="Delete Chatterbox Voice Profile…", command=self.delete_chatterbox_profile)
        voice_menu.add_separator()
        self.favorite_voice_menu_index = voice_menu.index("end") + 1
        voice_menu.add_command(label="Add Current Voice to Favorites", command=self.toggle_voice_favorite)

        voice_view_menu = tk.Menu(voice_menu, tearoff=True)
        voice_menu.add_cascade(label="Voice List", menu=voice_view_menu)
        for option in VOICE_VIEW_OPTIONS:
            voice_view_menu.add_radiobutton(
                label=option, value=option, variable=self.voice_view_var, command=self._voice_filter_changed,
            )

        voice_use_case_menu = tk.Menu(voice_menu, tearoff=True)
        voice_menu.add_cascade(label="Use Case", menu=voice_use_case_menu)
        for option in VOICE_USE_CASE_OPTIONS:
            voice_use_case_menu.add_radiobutton(
                label=option, value=option, variable=self.voice_use_case_var, command=self._voice_filter_changed,
            )
        voice_menu.add_command(label="Clear All Favorites\u2026", command=self.clear_voice_favorites)
        voice_menu.add_separator()
        voice_menu.add_command(label="Pronunciation Glossary…", command=self.open_glossary)

        view_menu = tk.Menu(menu)
        menu.add_cascade(label="View", menu=view_menu)
        view_menu.add_command(label="Zoom In", accelerator="Ctrl++", command=lambda: self.change_font_size(1))
        view_menu.add_command(label="Zoom Out", accelerator="Ctrl+-", command=lambda: self.change_font_size(-1))
        view_menu.add_command(label="Reset Zoom", accelerator="Ctrl+0", command=self.reset_font_size)
        self.wrap_var = tk.BooleanVar(value=bool(self.settings.get("word_wrap", True)))
        view_menu.add_checkbutton(label="Word Wrap", variable=self.wrap_var, command=self.toggle_word_wrap)

        help_menu = tk.Menu(menu)
        menu.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="Keyboard Shortcuts", command=self.show_shortcuts)
        help_menu.add_command(label="About", command=self.show_about)
        self._rebuild_recent_menu()

    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self, padding=(6, 3))
        toolbar.grid(row=0, column=0, sticky="ew")
        buttons = [
            ("New", self.new_document),
            ("Open", self.open_document),
            ("Save", self.save_document),
            ("Export Audio", self.export_audio),
            ("Batch", self.batch_convert),
        ]
        for label, command in buttons:
            button = ttk.Button(toolbar, text=label, command=command, style="Toolbar.TButton")
            button.pack(side="left", padx=2)
            if label == "Export Audio":
                self.export_button = button
            elif label == "Batch":
                self.batch_button = button
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=5)
        self.read_button = ttk.Button(toolbar, text="▶ Read", command=self.read_all, style="Primary.TButton")
        self.read_button.pack(side="left", padx=2)
        self.read_selection_button = ttk.Button(
            toolbar, text="Read Selection", command=self.read_selection, style="Toolbar.TButton",
        )
        self.read_selection_button.pack(side="left", padx=2)
        self.pause_button = ttk.Button(toolbar, text="⏸ Pause", command=self.pause_speech, style="Toolbar.TButton")
        self.pause_button.pack(side="left", padx=2)
        self.stop_button = ttk.Button(toolbar, text="■ Stop", command=self.stop_speech, style="Toolbar.TButton")
        self.stop_button.pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=5)
        ttk.Button(toolbar, text="Find", command=self.show_find_bar, style="Toolbar.TButton").pack(side="left", padx=2)
        ttk.Button(toolbar, text="Glossary", command=self.open_glossary, style="Toolbar.TButton").pack(side="left", padx=2)

    def _build_voice_panel(self) -> None:
        panel = ttk.LabelFrame(self, text="Speech Engine", style="Panel.TLabelframe")
        panel.grid(row=1, column=0, sticky="ew", padx=9, pady=(0, 0))
        self.voice_panel = panel
        panel.columnconfigure(0, weight=1)

        saved_engine = str(self.settings.get("engine", "kokoro"))
        if saved_engine not in ENGINE_LABEL_BY_KEY:
            saved_engine = "kokoro"
        self._active_engine_key = saved_engine
        self.engine_var = tk.StringVar(value=ENGINE_LABEL_BY_KEY[saved_engine])
        saved_selection = self.engine_selections.get(saved_engine, {})
        collection = normalize_kokoro_collection(
            saved_selection.get("collection", self._active_kokoro_collection)
        )
        self._active_kokoro_collection = collection
        self.kokoro_collection_var = tk.StringVar(value=kokoro_collection_label(collection))
        collection_selection = self.kokoro_collection_selections.get(collection, {})
        legacy_language = str(self.settings.get("language", "American English")) if saved_engine == "kokoro" else ""
        legacy_voice = str(self.settings.get("voice", "American Female — Heart")) if saved_engine == "kokoro" else ""
        language = (
            collection_selection.get("language", saved_selection.get("language", legacy_language))
            if saved_engine == "kokoro" else saved_selection.get("language", legacy_language)
        ) or self._default_language_for_engine(saved_engine)
        languages = self._provider_language_names(saved_engine)
        if language not in languages:
            language = self._default_language_for_engine(saved_engine)
        self.language_var = tk.StringVar(value=language)
        compatible_voices = self._voice_names_for_language(language, saved_engine)
        voice = (
            collection_selection.get("voice", saved_selection.get("voice", legacy_voice))
            if saved_engine == "kokoro" else saved_selection.get("voice", legacy_voice)
        )
        if saved_engine == "edge" and voice and voice not in compatible_voices:
            self.pending_edge_voice = voice
        self.voice_var = tk.StringVar(value=voice if voice in compatible_voices else compatible_voices[0])
        self.voice_display_var = tk.StringVar(value=self.voice_var.get())

        source_row = ttk.Frame(panel)
        source_row.grid(row=0, column=0, sticky="ew")
        source_row.columnconfigure(1, weight=1)
        source_row.columnconfigure(3, weight=1)
        source_row.columnconfigure(5, weight=1)
        ttk.Label(source_row, text="Engine").grid(row=0, column=0, sticky="w", padx=(0, 7))
        self.engine_combo = ttk.Combobox(
            source_row, textvariable=self.engine_var, values=list(ENGINE_MAP), state="readonly", width=26,
        )
        self.engine_combo.grid(row=0, column=1, sticky="ew", padx=(0, 20))
        ttk.Label(source_row, text="Language").grid(row=0, column=2, sticky="w", padx=(0, 7))
        self.language_combo = ttk.Combobox(
            source_row, textvariable=self.language_var, values=languages, state="readonly", width=25,
        )
        self.language_combo.grid(row=0, column=3, sticky="ew", padx=(0, 20))
        self.kokoro_collection_label = ttk.Label(source_row, text="Collection")
        self.kokoro_collection_label.grid(row=0, column=4, sticky="w", padx=(0, 7))
        self.kokoro_collection_combo = ttk.Combobox(
            source_row,
            textvariable=self.kokoro_collection_var,
            values=list(KOKORO_COLLECTION_LABELS),
            state="readonly",
            width=20,
        )
        self.kokoro_collection_combo.grid(row=0, column=5, sticky="ew")

        voice_row = ttk.Frame(panel)
        voice_row.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        voice_row.columnconfigure(1, weight=1)
        ttk.Label(voice_row, text="Voice").grid(row=0, column=0, sticky="w", padx=(0, 7))
        self.voice_combo = ttk.Combobox(
            voice_row, textvariable=self.voice_display_var, values=compatible_voices, state="readonly", width=50,
        )
        self.voice_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.favorite_voice_button = ttk.Button(
            voice_row, text="☆ Favorite", width=11, command=self.toggle_voice_favorite, style="Favorite.TButton",
        )
        self.favorite_voice_button.grid(row=0, column=2, padx=(0, 7))
        self.preview_voice_button = ttk.Button(voice_row, text="Preview Voice", command=self.preview_voice)
        self.preview_voice_button.grid(row=0, column=3)
        self.chatterbox_profile_label = ttk.Label(voice_row, text="Profile")
        self.chatterbox_profile_label.grid(row=0, column=4, padx=(8, 5))
        self.chatterbox_profile_var = tk.StringVar()
        self.chatterbox_profile_combo = ttk.Combobox(
            voice_row, textvariable=self.chatterbox_profile_var, state="readonly", width=20,
        )
        self.chatterbox_profile_combo.grid(row=0, column=5, padx=(0, 5))
        self.chatterbox_profile_new_button = ttk.Button(
            voice_row, text="New…", command=self.create_chatterbox_profile, width=7,
        )
        self.chatterbox_profile_new_button.grid(row=0, column=6, padx=(0, 5))
        self.chatterbox_profile_delete_button = ttk.Button(
            voice_row, text="Delete", command=self.delete_chatterbox_profile, width=7,
        )
        self.chatterbox_profile_delete_button.grid(row=0, column=7)
        self.engine_combo.bind("<<ComboboxSelected>>", self._engine_changed)
        self.language_combo.bind("<<ComboboxSelected>>", self._language_changed)
        self.kokoro_collection_combo.bind("<<ComboboxSelected>>", self._kokoro_collection_changed)
        self.voice_combo.bind("<<ComboboxSelected>>", self._voice_changed)
        self.chatterbox_profile_combo.bind("<<ComboboxSelected>>", self._chatterbox_profile_changed)

        filters = ttk.Frame(panel)
        filters.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        filters.columnconfigure(1, weight=1)
        filters.columnconfigure(3, weight=1)
        filters.columnconfigure(5, weight=2)
        ttk.Label(filters, text="Show").grid(row=0, column=0, sticky="w", padx=(0, 7))
        self.voice_view_combo = ttk.Combobox(
            filters, textvariable=self.voice_view_var, values=VOICE_VIEW_OPTIONS, state="readonly", width=16,
        )
        self.voice_view_combo.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(filters, text="Best for").grid(row=0, column=2, sticky="w", padx=(0, 7))
        self.voice_use_case_combo = ttk.Combobox(
            filters, textvariable=self.voice_use_case_var, values=VOICE_USE_CASE_OPTIONS, state="readonly", width=22,
        )
        self.voice_use_case_combo.grid(row=0, column=3, sticky="ew", padx=(0, 10))
        self.voice_view_combo.bind("<<ComboboxSelected>>", self._voice_filter_changed)
        self.voice_use_case_combo.bind("<<ComboboxSelected>>", self._voice_filter_changed)

        ttk.Separator(filters, orient="vertical").grid(row=0, column=4, sticky="ns", padx=(0, 10))
        controls = ttk.Frame(filters)
        controls.grid(row=0, column=5, sticky="ew")
        for column in (1, 4):
            controls.columnconfigure(column, weight=1)

        saved_rate_control = self.settings.get("rate_control")
        if saved_rate_control is None:
            if "speaking_rate" in self.settings:
                saved_speed = max(0.5, min(2.0, safe_float(self.settings["speaking_rate"], 1.0)))
                saved_rate_control = 10.0 * math.log2(saved_speed)
            elif "rate" in self.settings:
                saved_rate_control = safe_float(self.settings["rate"], 0.0)
            else:
                saved_rate_control = 0.0
        self.rate_var = tk.DoubleVar(value=max(-10.0, min(10.0, safe_float(saved_rate_control, 0.0))))
        self.pitch_var = tk.DoubleVar(value=max(-12.0, min(12.0, safe_float(self.settings.get("pitch"), -0.5))))
        self._add_scale(controls, 0, "Rate", self.rate_var, -10.0, 10.0)
        self._add_scale(controls, 3, "Pitch (st)", self.pitch_var, -12.0, 12.0)
        self._refresh_voice_choices(preferred_voice=self.voice_var.get())
        self._update_kokoro_collection_controls()
        self._update_engine_tab_label()
        self._update_chatterbox_reference_controls()
        if saved_engine == "edge":
            self.after_idle(self._refresh_edge_catalog)

    def _engine_key(self) -> str:
        return ENGINE_MAP.get(self.engine_var.get(), "kokoro")

    def _kokoro_collection_key(self) -> str:
        if not hasattr(self, "kokoro_collection_var"):
            return self._active_kokoro_collection
        return kokoro_collection_key(self.kokoro_collection_var.get())

    def _update_kokoro_collection_controls(self) -> None:
        if self._engine_key() == "kokoro":
            self.kokoro_collection_label.grid()
            self.kokoro_collection_combo.grid()
        else:
            self.kokoro_collection_label.grid_remove()
            self.kokoro_collection_combo.grid_remove()

    def _provider_language_names(self, engine: str | None = None) -> list[str]:
        engine = engine or self._engine_key()
        if engine == "chatterbox_flash":
            return list(CHATTERBOX_FLASH_LANG_MAP)
        if engine == "supertonic":
            return list(SUPERTONIC_LANG_MAP)
        if engine == "edge":
            locales = sorted({str(voice.get("Locale", "")) for voice in self.edge_voices if voice.get("Locale")})
            return locales or ["en-US"]
        return kokoro_language_names(self._kokoro_collection_key())

    def _default_language_for_engine(self, engine: str) -> str:
        if engine == "kokoro":
            return kokoro_language_names(self._kokoro_collection_key())[0]
        return {
            "edge": "en-US", "supertonic": "English", "chatterbox_flash": "English",
        }.get(engine, "English")

    def _rebuild_edge_voice_indexes(self) -> None:
        self.edge_voice_by_label = {edge_voice_label(voice): voice for voice in self.edge_voices}

    def _edge_voice_for_name(self, voice_name: str) -> dict[str, object] | None:
        return self.edge_voice_by_label.get(voice_name)

    def _voice_use_cases(self, voice_name: str, engine: str | None = None) -> set[str]:
        engine = engine or self._engine_key()
        try:
            voice_id = self._voice_code(voice_name, engine)
        except (KeyError, ValueError):
            return set()
        if engine == "kokoro":
            return {key for key, voice_ids in KOKORO_PREFERRED_VOICES.items() if voice_id in voice_ids}
        if engine == "supertonic":
            return {key for key, voice_ids in SUPERTONIC_PREFERRED_VOICES.items() if voice_id in voice_ids}
        if engine == "chatterbox_flash":
            return {
                key for key, voice_ids in CHATTERBOX_FLASH_PREFERRED_VOICES.items()
                if voice_id in voice_ids
            }

        cases = set(EDGE_FALLBACK_USE_CASES.get(voice_id, ()))
        voice = self._edge_voice_for_name(voice_name)
        if voice is None:
            return cases
        voice_tag = voice.get("VoiceTag", {})
        metadata: list[str] = []
        if isinstance(voice_tag, dict):
            for value in voice_tag.values():
                if isinstance(value, (list, tuple)):
                    metadata.extend(str(item) for item in value)
                elif value:
                    metadata.append(str(value))
        searchable = " ".join(metadata).lower()
        if any(word in searchable for word in ("narrat", "novel", "audiobook", "calm", "gentle", "warm")):
            cases.add("long_form")
        if any(word in searchable for word in ("general", "conversation", "assistant", "friendly", "positive")):
            cases.add("general")
        if any(word in searchable for word in ("news", "business", "professional", "authorit", "serious", "reliable")):
            cases.add("professional")
        return cases

    def _voice_preference_rank(self, voice_name: str, engine: str | None = None) -> int:
        engine = engine or self._engine_key()
        try:
            voice_id = self._voice_code(voice_name, engine)
        except (KeyError, ValueError):
            return 10000
        selected_case = VOICE_USE_CASE_KEYS.get(self.voice_use_case_var.get())
        if engine == "kokoro":
            preferred = KOKORO_PREFERRED_VOICES.get(selected_case, ()) if selected_case else KOKORO_PREFERRED_ORDER
        elif engine == "supertonic":
            preferred = SUPERTONIC_PREFERRED_VOICES.get(selected_case, ()) if selected_case else SUPERTONIC_PREFERRED_ORDER
        elif engine == "chatterbox_flash":
            preferred = (
                CHATTERBOX_FLASH_PREFERRED_VOICES.get(selected_case, ())
                if selected_case else CHATTERBOX_FLASH_PREFERRED_ORDER
            )
        else:
            preferred = EDGE_PREFERRED_ORDER
        try:
            return preferred.index(voice_id)
        except ValueError:
            return len(preferred) + 100

    def _voice_is_favorite(self, voice_name: str, engine: str | None = None) -> bool:
        engine = engine or self._engine_key()
        try:
            return self._voice_code(voice_name, engine) in self.voice_favorites.get(engine, [])
        except (KeyError, ValueError):
            return False

    def _filtered_voice_names(self, language_name: str, engine: str | None = None) -> list[str]:
        engine = engine or self._engine_key()
        voices = self._voice_names_for_language(language_name, engine)
        selected_case = VOICE_USE_CASE_KEYS.get(self.voice_use_case_var.get())
        view = self.voice_view_var.get()
        voice_cases = {voice_name: self._voice_use_cases(voice_name, engine) for voice_name in voices}
        voice_ids = {voice_name: self._voice_code(voice_name, engine) for voice_name in voices}
        favorite_ids = self.voice_favorites.get(engine, [])
        favorite_ranks = {voice_id: index for index, voice_id in enumerate(favorite_ids)}

        def is_visible(voice_name: str) -> bool:
            cases = voice_cases[voice_name]
            if selected_case and selected_case not in cases:
                return False
            if view == "Preferred Only":
                return bool(cases)
            if view == "Favorites":
                return voice_ids[voice_name] in favorite_ranks
            return True

        visible = [voice_name for voice_name in voices if is_visible(voice_name)]

        def sort_key(voice_name: str) -> tuple[int, int, int, str]:
            favorite_rank = favorite_ranks.get(voice_ids[voice_name], 10000)
            preferred = bool(voice_cases[voice_name])
            return (
                0 if favorite_rank < 10000 else 1,
                favorite_rank,
                self._voice_preference_rank(voice_name, engine) if preferred else 10000,
                voice_name.casefold(),
            )

        return sorted(visible, key=sort_key)

    def _decorated_voice_label(self, voice_name: str, engine: str | None = None) -> str:
        engine = engine or self._engine_key()
        markers = []
        if self._voice_is_favorite(voice_name, engine):
            markers.append("★ Favorite")
        if self._voice_use_cases(voice_name, engine):
            markers.append("◆ Preferred")
        return f"{' · '.join(markers)} — {voice_name}" if markers else voice_name

    def _refresh_voice_choices(self, preferred_voice: str | None = None) -> None:
        engine = self._engine_key()
        voices = self._filtered_voice_names(self.language_var.get(), engine)
        self.visible_voice_names = voices
        displays = [self._decorated_voice_label(voice_name, engine) for voice_name in voices]
        self.voice_display_to_name = dict(zip(displays, voices))
        self.voice_combo.configure(values=displays)
        selected = preferred_voice or self.voice_var.get()
        if voices:
            if selected not in voices:
                selected = voices[0]
            self.voice_var.set(selected)
            self.voice_display_var.set(self._decorated_voice_label(selected, engine))
            self.voice_combo.configure(state="readonly")
            self.favorite_voice_button.configure(state="normal")
        else:
            self.voice_display_var.set("No voices match these filters")
            self.voice_combo.configure(state="disabled")
            self.favorite_voice_button.configure(state="disabled")
        self._update_favorite_controls()

    def _update_favorite_controls(self) -> None:
        voice_name = self.voice_var.get()
        available = bool(self.visible_voice_names and voice_name in self.visible_voice_names)
        favorite = available and self._voice_is_favorite(voice_name)
        self.favorite_voice_button.configure(text="★ Favorite" if favorite else "☆ Favorite")
        self.voice_menu.entryconfigure(
            self.favorite_voice_menu_index,
            label="Remove Current Voice from Favorites" if favorite else "Add Current Voice to Favorites",
            state="normal" if available else "disabled",
        )

    def _voice_filter_changed(self, _event=None) -> None:
        previous_voice = self.voice_var.get()
        self._refresh_voice_choices(preferred_voice=self.voice_var.get())
        if self.voice_var.get() != previous_voice:
            self._mark_selection_changed("Voice filter changed the selected voice")
        else:
            self._schedule_settings_save()
        if not self.visible_voice_names:
            self.status_var.set("No voices match the selected voice list and use-case filters")

    def toggle_voice_favorite(self) -> None:
        voice_name = self.voice_var.get()
        if voice_name not in self.visible_voice_names:
            return
        engine = self._engine_key()
        voice_id = self._voice_code(voice_name, engine)
        favorites = self.voice_favorites.setdefault(engine, [])
        if voice_id in favorites:
            favorites.remove(voice_id)
            action = "Removed from"
        else:
            favorites.append(voice_id)
            action = "Added to"
        self._refresh_voice_choices(preferred_voice=voice_name)
        self._schedule_settings_save()
        self.status_var.set(f"{action} favorites: {voice_name}")

    def clear_voice_favorites(self) -> None:
        if not any(self.voice_favorites.values()):
            self.status_var.set("There are no favorite voices to clear")
            return
        if not messagebox.askyesno(
            "Clear All Favorites", "Remove every favorite voice from all providers?", parent=self,
        ):
            return
        for favorites in self.voice_favorites.values():
            favorites.clear()
        self._refresh_voice_choices(preferred_voice=self.voice_var.get())
        self._schedule_settings_save()
        self.status_var.set("Cleared all favorite voices")

    def _voice_names_for_language(self, language_name: str, engine: str | None = None) -> list[str]:
        engine = engine or self._engine_key()
        if engine == "chatterbox_flash":
            return list(CHATTERBOX_FLASH_VOICE_MAP)
        if engine == "supertonic":
            return list(SUPERTONIC_VOICE_MAP)
        if engine == "edge":
            labels = [label for label, voice in self.edge_voice_by_label.items()
                      if str(voice.get("Locale", "")) == language_name]
            return labels or [edge_voice_label(EDGE_FALLBACK_VOICES[0])]
        return kokoro_voice_names(self._kokoro_collection_key(), language_name)

    def _voice_code(self, voice_name: str, engine: str | None = None) -> str:
        engine = engine or self._engine_key()
        if engine == "chatterbox_flash":
            return CHATTERBOX_FLASH_VOICE_MAP[voice_name]
        if engine == "supertonic":
            return SUPERTONIC_VOICE_MAP[voice_name]
        if engine == "edge":
            voice = self.edge_voice_by_label.get(voice_name)
            if voice is not None:
                return str(voice["ShortName"])
            match = re.search(r"\(([^()]+Neural)\)$", voice_name)
            if match:
                return match.group(1)
            raise KeyError(voice_name)
        collection = self._kokoro_collection_key()
        return stable_kokoro_voice_id(collection, kokoro_voice_id(collection, voice_name))

    def _voice_selection_is_valid(self) -> bool:
        language = self.language_var.get()
        if language not in self._provider_language_names():
            return False
        return self.voice_var.get() in self.visible_voice_names

    def _validate_synthesis_settings(self) -> bool:
        if not self._voice_selection_is_valid():
            messagebox.showerror(
                "Voice Settings", "Select a valid language and voice for the active filters.", parent=self,
            )
            return False
        if self._engine_key() == "chatterbox_flash":
            reference_path, _conditioning = self._chatterbox_profile_paths()
            reference = Path(reference_path) if reference_path else None
            managed_outputs = {normalized_path_key(PREVIEW_PATH), normalized_path_key(STREAM_PATH)}
            try:
                reference_ready = (
                    reference is not None
                    and reference.is_file()
                    and reference.stat().st_size > 0
                    and normalized_path_key(reference) not in managed_outputs
                )
            except OSError:
                reference_ready = False
            if not reference_ready:
                messagebox.showerror(
                    "Chatterbox Reference Audio",
                    "Create or select a non-empty Chatterbox voice profile before generating speech.",
                    parent=self,
                )
                return False
        return True

    def _save_active_engine_selection(self, engine: str | None = None) -> None:
        engine = engine or self._active_engine_key
        selection = {
            "language": self.language_var.get(),
            "voice": self.voice_var.get(),
        }
        if engine == "kokoro":
            collection = self._kokoro_collection_key()
            selection["collection"] = collection
            self.kokoro_collection_selections[collection] = {
                "language": self.language_var.get(),
                "voice": self.voice_var.get(),
            }
        self.engine_selections[engine] = selection

    def _selection_snapshot(self) -> RuntimeSelection:
        engine = self._engine_key()
        collection = self._kokoro_collection_key() if engine == "kokoro" else ""
        language_name = self.language_var.get()
        if engine == "kokoro":
            language = kokoro_language_code(collection, language_name)
        elif engine == "supertonic":
            language = SUPERTONIC_LANG_MAP.get(language_name, "na")
        elif engine == "chatterbox_flash":
            language = CHATTERBOX_FLASH_LANG_MAP.get(language_name, "en")
        else:
            language = language_name
        try:
            voice_id = self._voice_code(self.voice_var.get(), engine)
        except (KeyError, ValueError):
            voice_id = ""
        return RuntimeSelection(
            provider=engine,
            collection=collection,
            language=language,
            voice_id=voice_id,
            profile_id=self.chatterbox_profile_id if engine == "chatterbox_flash" else "",
            revision=self.selection_revision,
        )

    def _mark_selection_changed(self, message: str = "Selection updated") -> None:
        self.selection_revision += 1
        self._schedule_settings_save()
        self.status_var.set(f"{message} — model load queued")
        self._schedule_runtime_preload()

    def _schedule_settings_save(self) -> None:
        if self.settings_save_after_id is not None:
            try:
                self.after_cancel(self.settings_save_after_id)
            except tk.TclError:
                pass
        self.settings_save_after_id = self.after(500, self._finish_scheduled_settings_save)

    def _finish_scheduled_settings_save(self) -> None:
        self.settings_save_after_id = None
        self._save_settings()

    def _schedule_runtime_preload(self) -> None:
        if self.running:
            self.preload_after_speech = True
            return
        self.preload_after_speech = False
        selection = self._selection_snapshot()
        if selection.provider == "edge":
            self.runtime_coordinator.activate(selection, self._release_gpu_engines)
            return
        self.runtime_coordinator.schedule_preload(selection, self._load_runtime_snapshot, delay=0.65)

    def _release_gpu_engines(self) -> None:
        self._stop_chatterbox_worker()
        self._release_kokoro_runtime()

    def _runtime_status_changed(
        self,
        state: RuntimeState,
        selection: RuntimeSelection | None,
        message: str,
    ) -> None:
        def apply() -> None:
            if selection is not None and selection.revision != self.selection_revision:
                return
            self.runtime_loading = state in {RuntimeState.QUEUED, RuntimeState.LOADING}
            self._refresh_action_states()
            if selection is None:
                return
            label = ENGINE_LABEL_BY_KEY.get(selection.provider, selection.provider)
            if state == RuntimeState.QUEUED:
                self.status_var.set(f"{label}: model load queued")
            elif state == RuntimeState.LOADING:
                self.status_var.set(f"{label}: loading model in the background…")
            elif state == RuntimeState.READY:
                self.status_var.set(f"{label}: ready")
                self._update_engine_tab_label()
            elif state == RuntimeState.FAILED:
                self.status_var.set(f"{label} load failed: {message}")
        try:
            self.after(0, apply)
        except RuntimeError:
            pass

    def _runtime_load_failed(self, _selection: RuntimeSelection, error: Exception) -> None:
        self._log_error(error)

    def _refresh_action_states(self) -> None:
        disabled = self.running or self.runtime_loading
        state = "disabled" if disabled else "normal"
        for name in (
            "read_button", "read_selection_button", "preview_voice_button",
            "export_button", "batch_button",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(state=state)
        self._update_chatterbox_reference_controls()

    def _load_runtime_snapshot(self, selection: RuntimeSelection) -> bool:
        if not self.runtime_coordinator.is_desired(selection):
            return False
        if selection.provider == "kokoro":
            self._stop_chatterbox_worker()
            repo_id = kokoro_model_repository(selection.collection)
            pipeline = self._ensure_pipeline(selection.language, repo_id, selection)
            if pipeline is None or not self.runtime_coordinator.is_desired(selection):
                return False
            voice_collection, voice_id = split_stable_kokoro_voice_id(selection.voice_id)
            resolved_voice = resolve_kokoro_voice(voice_collection, voice_id)
            voice_was_cached = resolved_voice in pipeline.voices
            voice_tensor = pipeline.load_voice(resolved_voice).to(pipeline.model.device)
            if not self.runtime_coordinator.is_desired(selection):
                if not voice_was_cached:
                    pipeline.voices.pop(resolved_voice, None)
                return False
            with self.kokoro_pipeline_lock:
                for cached_pipeline in self.kokoro_pipelines.values():
                    cached_pipeline.voices.clear()
                pipeline.voices[resolved_voice] = voice_tensor
                self.kokoro_voice_tensors.clear()
                self.kokoro_loaded_voices.clear()
                self.kokoro_voice_tensors[(repo_id, selection.language, selection.voice_id)] = voice_tensor
                self.kokoro_loaded_voices.add((repo_id, selection.language, selection.voice_id))
            return True
        if selection.provider == "supertonic":
            self._stop_chatterbox_worker()
            self._release_kokoro_runtime()
            if not self.runtime_coordinator.is_desired(selection):
                return False
            supertonic = self._ensure_supertonic()
            if selection.voice_id not in self.supertonic_styles:
                self.supertonic_styles[selection.voice_id] = supertonic.get_voice_style(selection.voice_id)
            return self.runtime_coordinator.is_desired(selection)
        if selection.provider == "chatterbox_flash":
            self._release_kokoro_runtime()
            if not selection.profile_id:
                self._ensure_legacy_chatterbox_excerpt()
            if not self.runtime_coordinator.is_desired(selection):
                return False
            self._ensure_chatterbox_flash()
            reference, conditioning = self._chatterbox_profile_paths(selection.profile_id)
            if reference:
                self.chatterbox_client.prepare_profile(
                    selection.profile_id or "legacy", reference, conditioning,
                )
            return self.runtime_coordinator.is_desired(selection)
        if selection.provider == "edge":
            self._stop_chatterbox_worker()
            self._release_kokoro_runtime()
            return self.runtime_coordinator.is_desired(selection)
        return True

    def _engine_changed(self, _event=None) -> None:
        new_engine = self._engine_key()
        if self._active_engine_key != new_engine:
            self._save_active_engine_selection(self._active_engine_key)
        self._active_engine_key = new_engine
        saved = self.engine_selections.get(new_engine, {})
        if new_engine == "kokoro":
            collection = normalize_kokoro_collection(
                saved.get("collection", self._active_kokoro_collection)
            )
            self._active_kokoro_collection = collection
            self.kokoro_collection_var.set(kokoro_collection_label(collection))
            saved = {**saved, **self.kokoro_collection_selections.get(collection, {})}
        languages = self._provider_language_names(new_engine)
        language = saved.get("language", self._default_language_for_engine(new_engine))
        if language not in languages:
            language = self._default_language_for_engine(new_engine)
        self.language_var.set(language)
        self.language_combo.configure(values=languages)
        voice = saved.get("voice", "")
        all_voices = self._voice_names_for_language(language, new_engine)
        if voice not in all_voices:
            voice = all_voices[0]
        self.voice_var.set(voice)
        self._refresh_voice_choices(preferred_voice=voice)
        self._update_kokoro_collection_controls()
        self._update_engine_tab_label()
        self._update_chatterbox_reference_controls()
        self._mark_selection_changed(f"{ENGINE_LABEL_BY_KEY[new_engine]} selected")
        if new_engine == "edge":
            self._refresh_edge_catalog(self.selection_revision)

    def _kokoro_collection_changed(self, _event=None) -> None:
        previous = self._active_kokoro_collection
        self.kokoro_collection_selections[previous] = {
            "language": self.language_var.get(),
            "voice": self.voice_var.get(),
        }
        collection = self._kokoro_collection_key()
        self._active_kokoro_collection = collection
        saved = self.kokoro_collection_selections.get(collection, {})
        languages = kokoro_language_names(collection)
        language = saved.get("language", languages[0])
        if language not in languages:
            language = languages[0]
        self.language_var.set(language)
        self.language_combo.configure(values=languages)
        voices = self._voice_names_for_language(language, "kokoro")
        voice = saved.get("voice", "")
        if voice not in voices:
            voice = voices[0]
        self.voice_var.set(voice)
        self._refresh_voice_choices(preferred_voice=voice)
        self._update_engine_tab_label()
        self._mark_selection_changed(
            f"Kokoro collection selected: {kokoro_collection_label(collection)}"
        )

    def _language_changed(self, _event=None) -> None:
        self._refresh_voice_choices(preferred_voice=self.voice_var.get())
        self._mark_selection_changed("Language selected")

    def choose_chatterbox_reference_audio(self) -> None:
        self.create_chatterbox_profile()

    def create_chatterbox_profile(self) -> None:
        current = Path(self.chatterbox_reference_audio) if self.chatterbox_reference_audio else None
        initial_dir = str(current.parent) if current is not None and current.parent.exists() else None
        selected = filedialog.askopenfilename(
            parent=self,
            title="Choose Source Audio for Chatterbox Profile",
            initialdir=initial_dir,
            filetypes=[
                ("Reference audio", "*.wav *.flac *.mp3 *.m4a *.ogg *.opus"),
                ("WAV audio", "*.wav"),
                ("All files", "*.*"),
            ],
        )
        if not selected:
            return
        reference = Path(selected)
        if not reference.is_file():
            messagebox.showerror("Reference Audio", "The selected audio file is unavailable.", parent=self)
            return
        managed_outputs = {normalized_path_key(PREVIEW_PATH), normalized_path_key(STREAM_PATH)}
        if normalized_path_key(reference) in managed_outputs:
            messagebox.showerror(
                "Reference Audio",
                "Choose a permanent reference clip, not the app's preview or streaming output file.",
                parent=self,
            )
            return
        default_name = reference.stem[:40] or "Voice Profile"
        name = simpledialog.askstring(
            "New Chatterbox Voice Profile", "Profile name:", initialvalue=default_name, parent=self,
        )
        if not name:
            return
        if any(profile.name.casefold() == name.strip().casefold() for profile in self.chatterbox_profiles.values()):
            messagebox.showerror("Voice Profile", "A profile with that name already exists.", parent=self)
            return
        start = simpledialog.askfloat(
            "New Chatterbox Voice Profile", "Start time in seconds:",
            initialvalue=0.0, minvalue=0.0, parent=self,
        )
        if start is None:
            return
        duration = simpledialog.askfloat(
            "New Chatterbox Voice Profile", "Duration in seconds (3–10):",
            initialvalue=10.0, minvalue=3.0, maxvalue=10.0, parent=self,
        )
        if duration is None:
            return
        self.chatterbox_profile_new_button.configure(state="disabled")
        self.status_var.set(f"Extracting managed voice sample from {reference.name}…")

        def worker() -> None:
            try:
                profile = create_chatterbox_profile(
                    self.chatterbox_profile_root, FFMPEG_EXE, name, reference, start, duration,
                )
                self.after(0, lambda: self._finish_chatterbox_profile_create(profile, None))
            except Exception as exc:
                self.after(0, lambda exc=exc: self._finish_chatterbox_profile_create(None, exc))

        threading.Thread(target=worker, daemon=True, name="chatterbox-profile-extract").start()

    def _finish_chatterbox_profile_create(
        self, profile: ChatterboxProfile | None, error: Exception | None,
    ) -> None:
        self.chatterbox_profile_new_button.configure(state="normal")
        if error is not None or profile is None:
            messagebox.showerror("Voice Profile", str(error or "Could not create the profile."), parent=self)
            return
        self.chatterbox_profiles[profile.profile_id] = profile
        self.chatterbox_profile_id = profile.profile_id
        self._update_chatterbox_reference_controls()
        self._mark_selection_changed(f"Chatterbox voice profile created: {profile.name}")

    def delete_chatterbox_profile(self) -> None:
        profile = self.chatterbox_profiles.get(self.chatterbox_profile_id)
        if profile is None:
            messagebox.showinfo("Voice Profile", "Select a managed voice profile to delete.", parent=self)
            return
        if not messagebox.askyesno(
            "Delete Voice Profile",
            f"Delete the managed profile ‘{profile.name}’?\n\nThe original source audio will not be deleted.",
            parent=self,
        ):
            return
        try:
            delete_chatterbox_profile(self.chatterbox_profile_root, profile)
        except Exception as exc:
            messagebox.showerror("Voice Profile", str(exc), parent=self)
            return
        self.chatterbox_profiles.pop(profile.profile_id, None)
        self.chatterbox_profile_id = ""
        self.runtime_coordinator.invalidate("chatterbox_flash")
        threading.Thread(
            target=lambda: self.chatterbox_client.delete_profile(profile.profile_id),
            daemon=True,
            name="chatterbox-profile-forget",
        ).start()
        self._update_chatterbox_reference_controls()
        self._mark_selection_changed(f"Deleted Chatterbox voice profile: {profile.name}")

    def _chatterbox_profile_changed(self, _event=None) -> None:
        self.chatterbox_profile_id = self.chatterbox_profile_display_to_id.get(
            self.chatterbox_profile_var.get(), "",
        )
        self._update_chatterbox_reference_controls()
        self._mark_selection_changed("Chatterbox voice profile selected")

    def _chatterbox_profile_paths(self, profile_id: str | None = None) -> tuple[str, str]:
        lookup_id = self.chatterbox_profile_id if profile_id is None else profile_id
        profile = self.chatterbox_profiles.get(lookup_id)
        if profile is not None and Path(profile.excerpt_path).is_file():
            return profile.excerpt_path, profile.conditioning_path
        legacy = Path(self.chatterbox_reference_audio) if self.chatterbox_reference_audio else None
        if legacy is not None and legacy.is_file():
            excerpt = self.chatterbox_profile_root / "legacy" / "reference.wav"
            conditioning = self.chatterbox_profile_root / "legacy" / "conditionals.pt"
            if excerpt.is_file():
                return str(excerpt), str(conditioning)
            return str(legacy), ""
        return "", ""

    def _ensure_legacy_chatterbox_excerpt(self) -> None:
        source = Path(self.chatterbox_reference_audio) if self.chatterbox_reference_audio else None
        if source is None or not source.is_file():
            return
        legacy_dir = self.chatterbox_profile_root / "legacy"
        excerpt = legacy_dir / "reference.wav"
        metadata_path = legacy_dir / "source.json"
        conditioning_path = legacy_dir / "conditionals.pt"
        try:
            stat = source.stat()
        except OSError as exc:
            raise RuntimeError(f"The legacy reference audio is unavailable: {source}") from exc
        signature = {
            "source_path": str(source.resolve()),
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
        }
        try:
            saved_signature = json.loads(metadata_path.read_text(encoding="utf-8"))
            current = excerpt.is_file() and saved_signature == signature
        except (OSError, ValueError, TypeError):
            current = False
        if not current:
            self._set_status(f"Extracting a safe 10-second legacy voice sample from {source.name}…", 0)
            extract_chatterbox_excerpt(FFMPEG_EXE, source, excerpt, 0.0, 10.0)
            conditioning_path.unlink(missing_ok=True)
            temporary = metadata_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(signature, indent=2), encoding="utf-8")
            os.replace(temporary, metadata_path)

    def _update_chatterbox_reference_controls(self) -> None:
        if not hasattr(self, "chatterbox_profile_combo"):
            return
        profiles = sorted(self.chatterbox_profiles.values(), key=lambda item: item.name.casefold())
        displays = [profile.name for profile in profiles]
        self.chatterbox_profile_display_to_id = {
            profile.name: profile.profile_id for profile in profiles
        }
        legacy = Path(self.chatterbox_reference_audio) if self.chatterbox_reference_audio else None
        if legacy is not None and legacy.is_file():
            legacy_label = f"Legacy: {legacy.name}"
            displays.append(legacy_label)
            self.chatterbox_profile_display_to_id[legacy_label] = ""
        self.chatterbox_profile_combo.configure(values=displays)
        selected = self.chatterbox_profiles.get(self.chatterbox_profile_id)
        if selected is not None:
            self.chatterbox_profile_var.set(selected.name)
        elif legacy is not None and legacy.is_file():
            self.chatterbox_profile_var.set(f"Legacy: {legacy.name}")
        else:
            self.chatterbox_profile_var.set("No voice profile")
        can_edit = self._engine_key() == "chatterbox_flash" and not self.running and not self.runtime_loading
        self.chatterbox_profile_new_button.configure(state="normal" if can_edit else "disabled")
        self.chatterbox_profile_delete_button.configure(
            state="normal" if selected is not None and can_edit else "disabled",
        )
        controls = (
            self.chatterbox_profile_label, self.chatterbox_profile_combo,
            self.chatterbox_profile_new_button, self.chatterbox_profile_delete_button,
        )
        for control in controls:
            if self._engine_key() == "chatterbox_flash":
                control.grid()
            else:
                control.grid_remove()

    def _voice_changed(self, _event=None) -> None:
        selected_display = self.voice_display_var.get()
        voice_name = self.voice_display_to_name.get(selected_display)
        if voice_name is None:
            return
        self.voice_var.set(voice_name)
        self._update_favorite_controls()
        self._mark_selection_changed("Voice selected")

    def _refresh_edge_catalog(self, revision: int | None = None) -> None:
        if self.edge_catalog_loading:
            return
        revision = self.selection_revision if revision is None else revision
        self.edge_catalog_loading = True
        self.status_var.set("Refreshing Microsoft Edge voice catalog…")

        def worker() -> None:
            try:
                voices = asyncio.run(load_edge_runtime().list_voices())
                self.after(0, lambda: self._finish_edge_catalog_refresh(voices, None, revision))
            except Exception as exc:
                self.after(0, lambda exc=exc: self._finish_edge_catalog_refresh([], exc, revision))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_edge_catalog_refresh(
        self, voices: list[dict], error: Exception | None, revision: int | None = None,
    ) -> None:
        self.edge_catalog_loading = False
        if error is not None:
            if self._engine_key() == "edge" and revision == self.selection_revision:
                self.status_var.set(f"Edge catalog unavailable; using cached fallback voices: {error}")
            return
        if voices:
            self.edge_voices = [dict(voice) for voice in voices]
            self._rebuild_edge_voice_indexes()
            if self._engine_key() == "edge" and revision == self.selection_revision:
                current_language = self.language_var.get()
                current_voice = self.pending_edge_voice or self.voice_var.get()
                self.pending_edge_voice = ""
                languages = self._provider_language_names("edge")
                if current_language not in languages:
                    current_language = "en-US" if "en-US" in languages else languages[0]
                self.language_var.set(current_language)
                self.language_combo.configure(values=languages)
                voice_names = self._voice_names_for_language(current_language, "edge")
                current_voice = current_voice if current_voice in voice_names else voice_names[0]
                self.voice_var.set(current_voice)
                self._refresh_voice_choices(preferred_voice=current_voice)
                self._schedule_settings_save()
                self.status_var.set(f"Loaded {len(voices)} Microsoft Edge voices")

    def _update_engine_tab_label(self) -> None:
        engine = self._engine_key()
        if engine == "kokoro":
            runtime = self.pipeline_device.upper() if self.pipeline_device else "background preload"
            collection = kokoro_collection_label(self._kokoro_collection_key())
            label = f"Kokoro-82M — {collection} ({runtime})"
        elif engine == "edge":
            label = "Microsoft Edge Neural Voice (online)"
        elif engine == "supertonic":
            label = "Supertonic 3 ONNX Voice (local; model loads on first speech)"
        else:
            runtime = (self.chatterbox_flash_device or "background preload").upper()
            label = f"Chatterbox-Flash Cloned Voice ({runtime})"
        self.voice_panel.configure(text=label)

    def _add_scale(
        self,
        parent: ttk.Frame,
        column: int,
        label: str,
        variable: tk.Variable,
        low: float,
        high: float,
        row: int = 0,
    ) -> None:
        pady = (8, 0) if row else 0
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=(0, 4), pady=pady)
        scale = ttk.Scale(
            parent, from_=low, to=high, variable=variable,
            command=lambda value: self._snap_scale_value(variable, low, high, value),
        )
        scale.grid(row=row, column=column + 1, sticky="ew", padx=(0, 4), pady=pady)
        entry = ttk.Spinbox(parent, from_=low, to=high, increment=0.5, textvariable=variable, width=6)
        entry.grid(row=row, column=column + 2, sticky="e", padx=(0, 8), pady=pady)
        entry.configure(command=lambda: self._manual_scale_value(variable, low, high))
        entry.bind("<Return>", lambda _event: self._manual_scale_value(variable, low, high))
        entry.bind("<FocusOut>", lambda _event: self._manual_scale_value(variable, low, high))

    def _snap_scale_value(self, variable: tk.Variable, low: float, high: float, value: str) -> None:
        try:
            numeric = max(low, min(high, float(value)))
        except (TypeError, ValueError, tk.TclError):
            return
        variable.set(round(numeric * 2.0) / 2.0)

    def _manual_scale_value(self, variable: tk.Variable, low: float, high: float) -> None:
        try:
            numeric = max(low, min(high, float(variable.get())))
        except (TypeError, ValueError, tk.TclError):
            self.bell()
            return
        # Manual entry intentionally keeps precise values such as 0.85 or 0.30.
        variable.set(round(numeric, 3))

    def _build_editor(self) -> None:
        self.document_tabs = ttk.Notebook(self)
        self.document_tabs.grid(row=3, column=0, sticky="nsew", padx=7, pady=(4, 0))
        self.document_tabs.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.document_tabs.bind("<Button-3>", self._show_tab_menu)
        self.document_tabs.bind("<ButtonPress-1>", self._on_tab_drag_start, add="+")
        self.document_tabs.bind("<B1-Motion>", self._on_tab_drag_motion, add="+")
        self.document_tabs.bind("<ButtonRelease-1>", self._on_tab_drag_end, add="+")
        self.font_size = max(8, min(48, safe_int(self.settings.get("font_size"), 12)))
        self.tab_menu = tk.Menu(self.document_tabs)
        self.tab_menu.add_command(label="Move Tab Left", command=lambda: self._move_active_tab(-1))
        self.tab_menu.add_command(label="Move Tab Right", command=lambda: self._move_active_tab(1))
        self.tab_menu.add_separator()
        self.tab_menu.add_command(label="Close Tab", command=self.close_active_tab)
        self._create_document_tab("Untitled")
        self.context_menu = tk.Menu(self)
        for label, event_name in (("Cut", "<<Cut>>"), ("Copy", "<<Copy>>"), ("Paste", "<<Paste>>")):
            self.context_menu.add_command(label=label, command=lambda event_name=event_name: self.text.event_generate(event_name))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Read Selection", command=self.read_selection)
        self.context_menu.add_command(label="Add Selection to Glossary", command=self.add_selection_to_glossary)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Select All", command=self.select_all)

    def _create_document_tab(self, name: str = "Untitled", path: Path | None = None) -> DocumentState:
        tab = ttk.Frame(self.document_tabs)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        text = tk.Text(
            tab,
            wrap="word" if self.wrap_var.get() else "none",
            undo=True,
            maxundo=-1,
            font=("Segoe UI", self.font_size),
            padx=12,
            pady=8,
            relief="flat",
            borderwidth=0,
        )
        vertical = ttk.Scrollbar(tab, orient="vertical", command=text.yview)
        horizontal = ttk.Scrollbar(tab, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        text.tag_configure("reading", background="#fff2a8", foreground="#1f2328")
        text.tag_configure("find_match", background="#b7dcff")
        state = DocumentState(tab=tab, text=text, path=path)
        key = str(tab)
        self.document_states[key] = state
        text.bind("<<Modified>>", lambda _event, key=key: self._on_document_modified(key))
        text.bind("<KeyRelease>", lambda _event, key=key: self._update_document_stats_for(key))
        text.bind("<ButtonRelease-1>", lambda _event, key=key: self._update_document_stats_for(key))
        text.bind("<Button-3>", self._show_context_menu)
        text.drop_target_register(DND_FILES)
        text.dnd_bind("<<Drop>>", self._on_document_drop)
        self.document_tabs.add(tab, text=name)
        self.document_tabs.select(tab)
        self._activate_document(key)
        return state

    def _active_document(self) -> DocumentState | None:
        return self.document_states.get(self.active_document_id or "")

    def _activate_document(self, key: str) -> None:
        state = self.document_states.get(key)
        if state is None:
            return
        self._cancel_scheduled_stats_update()
        self.active_document_id = key
        self.editor_tab = state.tab
        self.text = state.text
        self.current_path = state.path
        self.dirty = state.dirty
        self._update_title()
        self._update_document_stats()

    def _on_tab_changed(self, _event=None) -> None:
        selected = str(self.document_tabs.select())
        if selected in self.document_states:
            self._activate_document(selected)

    def _on_document_modified(self, key: str) -> None:
        state = self.document_states.get(key)
        if state is None or not state.text.edit_modified():
            return
        state.dirty = True
        state.text.edit_modified(False)
        if key == self.active_document_id:
            self.dirty = True
            self._update_title()
            self._update_document_stats_for(key)

    def _update_document_stats_for(self, key: str) -> None:
        if key != self.active_document_id:
            return
        self._cancel_scheduled_stats_update()
        self.stats_update_after_id = self.after(150, self._finish_scheduled_stats_update)

    def _cancel_scheduled_stats_update(self) -> None:
        if self.stats_update_after_id is None:
            return
        try:
            self.after_cancel(self.stats_update_after_id)
        except tk.TclError:
            pass
        self.stats_update_after_id = None

    def _finish_scheduled_stats_update(self) -> None:
        self.stats_update_after_id = None
        self._update_document_stats()

    def _show_tab_menu(self, event) -> None:
        try:
            index = self.document_tabs.index(f"@{event.x},{event.y}")
            self.document_tabs.select(index)
            self.tab_menu.tk_popup(event.x_root, event.y_root)
        except tk.TclError:
            pass
        finally:
            self.tab_menu.grab_release()

    def _on_tab_drag_start(self, event) -> None:
        try:
            index = self.document_tabs.index(f"@{event.x},{event.y}")
        except tk.TclError:
            self.tab_drag_index = None
            return
        self.tab_drag_index = index
        self.document_tabs.select(index)

    def _on_tab_drag_motion(self, event) -> None:
        if self.tab_drag_index is None:
            return
        try:
            target = self.document_tabs.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return
        if target == self.tab_drag_index:
            return
        tabs = self.document_tabs.tabs()
        if not (0 <= self.tab_drag_index < len(tabs)):
            self.tab_drag_index = None
            return
        tab_id = tabs[self.tab_drag_index]
        self.document_tabs.insert(target, tab_id)
        self.tab_drag_index = target
        self._sync_batch_queue_to_tab_order()

    def _on_tab_drag_end(self, _event=None) -> None:
        self.tab_drag_index = None

    def _move_active_tab(self, direction: int) -> None:
        selected = self.document_tabs.select()
        if not selected:
            return
        current = self.document_tabs.index(selected)
        target = max(0, min(len(self.document_tabs.tabs()) - 1, current + direction))
        if target == current:
            return
        self.document_tabs.insert(target, selected)
        self.document_tabs.select(selected)
        self._sync_batch_queue_to_tab_order()

    def _sync_batch_queue_to_tab_order(self) -> None:
        if len(self.batch_queue) < 2:
            return
        queued = {str(path.resolve()).lower(): path for path in self.batch_queue}
        ordered: list[Path] = []
        used: set[str] = set()
        for tab_id in self.document_tabs.tabs():
            state = self.document_states.get(str(tab_id))
            if state is None or state.path is None:
                continue
            key = str(state.path.resolve()).lower()
            if key in queued and key not in used:
                ordered.append(queued[key])
                used.add(key)
        ordered.extend(path for path in self.batch_queue if str(path.resolve()).lower() not in used)
        if ordered != self.batch_queue:
            self.batch_queue = ordered
            self._refresh_batch_queue_view()

    def _build_find_bar(self) -> None:
        self.find_frame = ttk.Frame(self, padding=(8, 5))
        self.find_frame.columnconfigure(1, weight=1)
        ttk.Label(self.find_frame, text="Find").grid(row=0, column=0, padx=(0, 5))
        self.find_var = tk.StringVar()
        self.find_entry = ttk.Entry(self.find_frame, textvariable=self.find_var)
        self.find_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        ttk.Label(self.find_frame, text="Replace").grid(row=0, column=2, padx=(5, 5))
        self.replace_var = tk.StringVar()
        ttk.Entry(self.find_frame, textvariable=self.replace_var, width=24).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(self.find_frame, text="Previous", command=lambda: self.find_next(backward=True)).grid(row=0, column=4, padx=2)
        ttk.Button(self.find_frame, text="Next", command=self.find_next).grid(row=0, column=5, padx=2)
        ttk.Button(self.find_frame, text="Replace", command=self.replace_one).grid(row=0, column=6, padx=2)
        ttk.Button(self.find_frame, text="Replace All", command=self.replace_all).grid(row=0, column=7, padx=2)
        ttk.Button(self.find_frame, text="Close", command=self.hide_find_bar).grid(row=0, column=8, padx=(8, 0))
        self.find_var.trace_add("write", lambda *_args: self.highlight_find_matches())

    def _build_status_bar(self) -> None:
        status = ttk.Frame(self, padding=(4, 2))
        status.grid(row=5, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Ready")
        self.file_status_var = tk.StringVar(value="Untitled")
        self.cursor_var = tk.StringVar(value="Ln 1, Col 1")
        self.stats_var = tk.StringVar(value="0 words | 0 characters")
        self.playback_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status, maximum=100, length=170)
        self.progress.grid(row=0, column=1, padx=6)
        ttk.Label(status, textvariable=self.playback_var, style="Status.TLabel").grid(row=0, column=2)
        ttk.Separator(status, orient="vertical").grid(row=0, column=3, sticky="ns", padx=5)
        ttk.Label(status, textvariable=self.file_status_var, style="Status.TLabel").grid(row=0, column=4)
        ttk.Separator(status, orient="vertical").grid(row=0, column=5, sticky="ns", padx=5)
        ttk.Label(status, textvariable=self.cursor_var, style="Status.TLabel").grid(row=0, column=6)
        ttk.Separator(status, orient="vertical").grid(row=0, column=7, sticky="ns", padx=5)
        ttk.Label(status, textvariable=self.stats_var, style="Status.TLabel").grid(row=0, column=8)

    def _bind_shortcuts(self) -> None:
        bindings = {
            "<Control-n>": self.new_document,
            "<Control-o>": self.open_document,
            "<Control-s>": self.save_document,
            "<Control-Shift-S>": lambda: self.save_document(True),
            "<Control-e>": self.export_audio,
            "<Control-f>": self.show_find_bar,
            "<Control-g>": self.go_to_line,
            "<Control-plus>": lambda: self.change_font_size(1),
            "<Control-equal>": lambda: self.change_font_size(1),
            "<Control-minus>": lambda: self.change_font_size(-1),
            "<Control-Key-0>": self.reset_font_size,
            "<F5>": self.read_all,
            "<F6>": self.read_selection,
            "<F7>": self.pause_speech,
            "<F8>": self.resume_speech,
            "<F9>": self.stop_speech,
            "<Escape>": self.hide_find_bar,
        }
        for sequence, command in bindings.items():
            self.bind_all(sequence, lambda event, command=command: self._invoke_binding(command))

    def _invoke_binding(self, command) -> str:
        command()
        return "break"

    def _insert_initial_text(self) -> None:
        self.text.insert("1.0", "Welcome to Kokoro Reader.\n\nChoose Kokoro, Microsoft Edge, Supertonic, or Chatterbox-Flash above; then open or drag in a document, paste text, or begin typing. Chatterbox-Flash also needs a reference audio clip. Use Read to listen, Export Audio to create MP3, Opus, AAC, Vorbis, FLAC, or WAV files, and Glossary to control pronunciation.")
        self.text.edit_modified(False)
        self.dirty = False
        self._update_title()
        self._update_document_stats()

    def _load_settings(self) -> dict:
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_settings(self) -> None:
        if self.settings_save_after_id is not None:
            try:
                self.after_cancel(self.settings_save_after_id)
            except tk.TclError:
                pass
            self.settings_save_after_id = None
        self._save_active_engine_selection(self._engine_key())
        data = {
            "geometry": self.geometry(),
            "engine": self._engine_key(),
            "engine_selections": self.engine_selections,
            "kokoro_collection": self._kokoro_collection_key(),
            "kokoro_collection_selections": self.kokoro_collection_selections,
            "voice_favorites": self.voice_favorites,
            "voice_view": self.voice_view_var.get(),
            "voice_use_case": self.voice_use_case_var.get(),
            "language": self.language_var.get(),
            "voice": self.voice_var.get(),
            "chatterbox_reference_audio": self.chatterbox_reference_audio,
            "chatterbox_profile_id": self.chatterbox_profile_id,
            "chatterbox_profiles": [
                profile.to_dict() for profile in sorted(
                    self.chatterbox_profiles.values(), key=lambda item: item.name.casefold(),
                )
            ],
            "rate_control": round(float(self.rate_var.get()), 3),
            "speaking_rate": round(rate_to_speed(float(self.rate_var.get())), 3),
            "pitch": round(float(self.pitch_var.get()), 2),
            "supertonic_steps": max(5, min(12, safe_int(self.settings.get("supertonic_steps"), 8))),
            "font_size": self.font_size,
            "word_wrap": bool(self.wrap_var.get()),
            "output_format": self.output_format,
            "output_bitrate": int(self.output_bitrate),
            "output_bitrate_mode": self.output_bitrate_mode,
            "output_vbr_quality": int(self.output_vbr_quality),
            "output_sample_rate": self.output_sample_rate,
            "output_channels": self.output_channels,
            "codec_effort": int(self.codec_effort),
            "recent_files": self.recent_files[:10],
            "glossary": self.glossary,
        }
        temporary_path = SETTINGS_PATH.with_suffix(SETTINGS_PATH.suffix + ".tmp")
        try:
            temporary_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary_path, SETTINGS_PATH)
            self.settings = data
        except OSError:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _apply_voice_defaults(self) -> None:
        self.rate_var.set(0.0)
        self.pitch_var.set(-0.50)

    def reset_app_preferences(self) -> None:
        if not messagebox.askyesno(
            "Reset App Preferences",
            "Reset all saved preferences, audio settings, the glossary, and recent files?\n\n"
            "Open documents and the current batch queue will be kept.",
            parent=self,
        ):
            return
        self.settings = {}
        self.engine_selections.clear()
        for favorites in self.voice_favorites.values():
            favorites.clear()
        self.voice_view_var.set(VOICE_VIEW_OPTIONS[0])
        self.voice_use_case_var.set(VOICE_USE_CASE_OPTIONS[0])
        self.engine_var.set(ENGINE_LABEL_BY_KEY["kokoro"])
        self._active_engine_key = "kokoro"
        self._active_kokoro_collection = KOKORO_DEFAULT_COLLECTION
        self.kokoro_collection_selections.clear()
        self.kokoro_collection_var.set(kokoro_collection_label(KOKORO_DEFAULT_COLLECTION))
        self.language_var.set("American English")
        self.voice_var.set("American Female — Heart")
        self.chatterbox_reference_audio = ""
        self.chatterbox_profile_id = ""
        self.language_combo.configure(values=self._provider_language_names("kokoro"))
        self._update_kokoro_collection_controls()
        self._refresh_voice_choices(preferred_voice=self.voice_var.get())
        self._apply_voice_defaults()
        self.output_format = "mp3"
        self.output_bitrate = 128
        self.output_bitrate_mode = "CBR"
        self.output_vbr_quality = 2
        self.output_sample_rate = "Voice native"
        self.output_channels = "Mono"
        self.codec_effort = 8
        self.glossary.clear()
        self.recent_files.clear()
        self._rebuild_recent_menu()
        self.font_size = 12
        self.wrap_var.set(True)
        self.toggle_word_wrap()
        for state in self.document_states.values():
            state.text.configure(font=("Segoe UI", self.font_size))
        self.geometry("1180x800")
        self._update_engine_tab_label()
        self._update_chatterbox_reference_controls()
        self._save_settings()
        self._mark_selection_changed("App preferences reset")
        self.status_var.set("App preferences reset; open documents and batch queue kept")

    def _current_voice_profile(self, language_name: str | None = None, voice_name: str | None = None) -> dict[str, object]:
        engine = self._engine_key()
        language_name = language_name or self.language_var.get()
        voice_name = voice_name or self.voice_var.get()
        if engine == "supertonic":
            language = SUPERTONIC_LANG_MAP[language_name]
        elif engine == "edge":
            language = language_name
        elif engine == "chatterbox_flash":
            language = CHATTERBOX_FLASH_LANG_MAP[language_name]
        else:
            language = kokoro_language_code(self._kokoro_collection_key(), language_name)
        base_voice = self._voice_code(voice_name, engine)
        sample_rate = 44100 if engine == "supertonic" else SAMPLE_RATE
        reference_audio, conditioning_path = self._chatterbox_profile_paths()
        selection = self._selection_snapshot()
        return {
            "engine": engine,
            "language": language,
            "voice": base_voice,
            "kokoro_collection": self._kokoro_collection_key() if engine == "kokoro" else "",
            "sample_rate": sample_rate,
            "supertonic_steps": max(5, min(12, safe_int(self.settings.get("supertonic_steps"), 8))),
            "reference_audio": reference_audio if engine == "chatterbox_flash" else "",
            "chatterbox_profile_id": self.chatterbox_profile_id if engine == "chatterbox_flash" else "",
            "conditioning_path": conditioning_path if engine == "chatterbox_flash" else "",
            "runtime_selection": selection,
            "rate": rate_to_speed(float(self.rate_var.get())),
            "pitch": max(-12.0, min(12.0, float(self.pitch_var.get()))),
        }

    def _current_audio_profile(self, format_key: str | None = None) -> dict[str, object]:
        return {
            "format": format_key or self.output_format,
            "bitrate": int(self.output_bitrate),
            "bitrate_mode": self.output_bitrate_mode,
            "vbr_quality": int(self.output_vbr_quality),
            "sample_rate": self.output_sample_rate,
            "channels": self.output_channels,
            "codec_effort": int(self.codec_effort),
        }

    def open_audio_settings(self, title: str = "Audio Output Settings") -> bool:
        window = tk.Toplevel(self)
        window.title(title)
        window.transient(self)
        window.resizable(False, False)
        window.columnconfigure(1, weight=1)
        result = {"accepted": False}

        format_var = tk.StringVar(value=AUDIO_FORMATS[self.output_format]["label"])
        bitrate_var = tk.StringVar(value=str(self.output_bitrate))
        mode_var = tk.StringVar(value=self.output_bitrate_mode)
        vbr_var = tk.IntVar(value=self.output_vbr_quality)
        sample_rate_var = tk.StringVar(value=self.output_sample_rate)
        channels_var = tk.StringVar(value=self.output_channels)
        effort_var = tk.IntVar(value=self.codec_effort)
        note_var = tk.StringVar()

        ttk.Label(window, text="Output format").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 5))
        format_combo = ttk.Combobox(
            window, textvariable=format_var, values=[details["label"] for details in AUDIO_FORMATS.values()],
            state="readonly", width=30,
        )
        format_combo.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=(12, 5))

        ttk.Label(window, text="Compression method").grid(row=1, column=0, sticky="w", padx=12, pady=5)
        mode_combo = ttk.Combobox(window, textvariable=mode_var, values=["CBR", "ABR", "VBR"], state="readonly", width=12)
        mode_combo.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=5)

        ttk.Label(window, text="Bit rate").grid(row=2, column=0, sticky="w", padx=12, pady=5)
        bitrate_combo = ttk.Combobox(
            window, textvariable=bitrate_var, values=[str(value) for value in AUDIO_BITRATES],
            state="readonly", width=12,
        )
        bitrate_combo.grid(row=2, column=1, sticky="ew", padx=(0, 12), pady=5)

        ttk.Label(window, text="VBR quality (0 best, 9 smallest)").grid(row=3, column=0, sticky="w", padx=12, pady=5)
        vbr_spin = ttk.Spinbox(window, from_=0, to=9, textvariable=vbr_var, width=10)
        vbr_spin.grid(row=3, column=1, sticky="ew", padx=(0, 12), pady=5)

        format_row = ttk.Frame(window)
        format_row.grid(row=4, column=0, columnspan=2, sticky="ew", padx=12, pady=5)
        format_row.columnconfigure(0, weight=1)
        format_row.columnconfigure(1, weight=1)
        ttk.Label(format_row, text="Sample rate").grid(row=0, column=0, sticky="w")
        ttk.Label(format_row, text="Channels").grid(row=0, column=1, sticky="w", padx=(12, 0))
        sample_combo = ttk.Combobox(
            format_row, textvariable=sample_rate_var,
            values=["Voice native", "16000", "22050", "24000", "44100", "48000"], state="readonly", width=18,
        )
        sample_combo.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        channels_combo = ttk.Combobox(format_row, textvariable=channels_var, values=["Mono", "Stereo"], state="readonly", width=14)
        channels_combo.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(3, 0))

        ttk.Label(window, text="Codec effort / compression level").grid(row=5, column=0, sticky="w", padx=12, pady=5)
        effort_spin = ttk.Spinbox(window, from_=0, to=12, textvariable=effort_var, width=10)
        effort_spin.grid(row=5, column=1, sticky="ew", padx=(0, 12), pady=5)

        ttk.Separator(window).grid(row=6, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 5))
        ttk.Label(window, textvariable=note_var, style="Muted.TLabel", wraplength=390, justify="left").grid(
            row=7, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 10)
        )

        def update_controls(_event=None) -> None:
            format_key = AUDIO_LABEL_TO_KEY[format_var.get()]
            lossy = not AUDIO_FORMATS[format_key]["lossless"]
            bitrate_combo.configure(state="readonly" if lossy else "disabled")
            mode_combo.configure(state="readonly" if lossy else "disabled")
            vbr_spin.configure(state="normal" if lossy and mode_var.get() == "VBR" else "disabled")
            effort_spin.configure(state="normal" if format_key not in {"wav16", "wav24", "aac"} else "disabled")
            if lossy:
                note_var.set("CBR is predictable, ABR targets an average size, and VBR spends bits where speech needs them.")
            elif format_key == "flac":
                note_var.set("FLAC is lossless. Higher compression levels make smaller files but take longer; audio quality is unchanged.")
            else:
                note_var.set("PCM WAV is uncompressed. Choose 24-bit for more precision or 16-bit for smaller, widely compatible files.")

        def accept() -> None:
            self.output_format = AUDIO_LABEL_TO_KEY[format_var.get()]
            self.output_bitrate = int(bitrate_var.get())
            self.output_bitrate_mode = mode_var.get()
            self.output_vbr_quality = max(0, min(9, int(vbr_var.get())))
            self.output_sample_rate = sample_rate_var.get()
            self.output_channels = channels_var.get()
            self.codec_effort = max(0, min(12, int(effort_var.get())))
            self._save_settings()
            result["accepted"] = True
            window.destroy()

        buttons = ttk.Frame(window)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", padx=12, pady=(0, 12))
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="OK", command=accept).pack(side="right")
        format_combo.bind("<<ComboboxSelected>>", update_controls)
        mode_combo.bind("<<ComboboxSelected>>", update_controls)
        update_controls()
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        window.grab_set()
        format_combo.focus_set()
        self.wait_window(window)
        return bool(result["accepted"])

    def _update_title(self) -> None:
        state = self._active_document()
        if state is not None:
            state.path = self.current_path
            state.dirty = self.dirty
        name = self.current_path.name if self.current_path else "Untitled"
        self.title(f"{'*' if self.dirty else ''}{name} — {APP_NAME}")
        if state is not None and hasattr(self, "document_tabs"):
            self._set_tab_label(state)
        if hasattr(self, "file_status_var"):
            self.file_status_var.set(str(self.current_path) if self.current_path else "Untitled")

    def _update_document_stats(self, _event=None) -> None:
        if not hasattr(self, "cursor_var") or not hasattr(self, "text"):
            return
        try:
            index = self.text.index("insert")
            line, column = index.split(".")
            content = self.text.get("1.0", "end-1c")
            words = len(re.findall(r"\S+", content))
            self.cursor_var.set(f"Ln {line}, Col {int(column) + 1}")
            self.stats_var.set(f"{words:,} words | {len(content):,} characters")
        except tk.TclError:
            pass

    def _show_context_menu(self, event) -> None:
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _enable_file_drop(self) -> None:
        """Register stable TkDND handlers for Explorer document drops."""
        try:
            for widget in (self, self.text):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_document_drop)
        except Exception as exc:
            self._log_error(exc)

    def _on_document_drop(self, event):
        try:
            paths = [Path(item) for item in self.tk.splitlist(event.data)]
            self._handle_dropped_files(paths)
        except Exception as exc:
            self._log_error(exc)
            messagebox.showerror("Document Drop Failed", str(exc))
        return getattr(event, "action", "copy")

    def _handle_dropped_files(self, paths: list[Path]) -> None:
        supported = [
            path for path in paths
            if path.is_file() and path.suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS
        ]
        if not supported:
            messagebox.showwarning(
                "Unsupported Drop",
                "Drop a TXT, Markdown, LOG, CSV, subtitle, RTF, DOCX, or EPUB document.",
            )
            return
        self._open_document_paths(supported, add_to_batch=True)

    def select_all(self) -> None:
        self.text.tag_add("sel", "1.0", "end-1c")
        self.text.mark_set("insert", "1.0")
        self.text.see("insert")

    def _confirm_save_changes(self) -> bool:
        if not self.dirty:
            return True
        result = messagebox.askyesnocancel("Unsaved Changes", "Save changes to this document?")
        if result is None:
            return False
        if result:
            return self.save_document()
        return True

    def _confirm_all_save_changes(self) -> bool:
        original = self.active_document_id
        for key, state in list(self.document_states.items()):
            if not state.dirty:
                continue
            self.document_tabs.select(state.tab)
            self._activate_document(key)
            if not self._confirm_save_changes():
                if original and original in self.document_states:
                    self.document_tabs.select(self.document_states[original].tab)
                    self._activate_document(original)
                return False
        return True

    def new_document(self) -> None:
        if not self._confirm_save_changes():
            return
        self.stop_speech()
        self.text.delete("1.0", "end")
        self.current_path = None
        self.dirty = False
        self.text.edit_modified(False)
        self._update_title()
        self._update_document_stats()

    def close_active_tab(self) -> None:
        state = self._active_document()
        if state is None:
            return
        if not self._confirm_save_changes():
            return
        if len(self.document_states) == 1:
            state.text.delete("1.0", "end")
            state.path = None
            state.dirty = False
            state.error = None
            state.loading = False
            state.text.edit_modified(False)
            self.current_path = None
            self.dirty = False
            self._update_title()
            self._update_document_stats()
            return
        key = self.active_document_id or ""
        self.document_tabs.forget(state.tab)
        state.tab.destroy()
        self.document_states.pop(key, None)
        selected = str(self.document_tabs.select())
        self._activate_document(selected)

    def _open_document_paths(self, paths: list[Path], add_to_batch: bool = False) -> None:
        existing = {
            str(state.path.resolve()).lower()
            for state in self.document_states.values()
            if state.path is not None and state.path.exists()
        }
        accepted: list[Path] = []
        skipped = 0
        for path in paths:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            key = str(resolved).lower()
            if key in existing or not resolved.is_file() or resolved.suffix.lower() not in SUPPORTED_DOCUMENT_EXTENSIONS:
                skipped += 1
                continue
            existing.add(key)
            accepted.append(resolved)
            state = self._create_document_tab(resolved.name, resolved)
            state.loading = True
            self._set_tab_label(state)
            self.pending_document_loads.append((str(state.tab), resolved))
        if add_to_batch:
            self._add_to_batch_queue(accepted)
        if accepted:
            self.status_var.set(f"Queued {len(accepted)} document{'s' if len(accepted) != 1 else ''} for opening")
            self._start_next_document_load()
        if skipped:
            self.status_var.set(f"Opened {len(accepted)} document(s); skipped {skipped} duplicate or unsupported item(s)")

    def _set_tab_label(self, state: DocumentState) -> None:
        name = state.path.name if state.path else "Untitled"
        if state.loading:
            name += " (Loading…)"
        elif state.error:
            name += " (Failed)"
        self.document_tabs.tab(state.tab, text=f"{'*' if state.dirty else ''}{name}")

    def open_document(self) -> None:
        paths = filedialog.askopenfilenames(title="Open Documents", filetypes=OPEN_FILE_TYPES)
        if paths:
            self._open_document_paths([Path(path) for path in paths], add_to_batch=True)

    def _load_path(self, path: Path) -> None:
        self._open_document_paths([path], add_to_batch=False)

    def _start_next_document_load(self) -> None:
        if self.document_load_in_progress or not self.pending_document_loads:
            if not self.document_load_in_progress:
                self.progress.stop()
                self.progress.configure(mode="determinate")
            return
        key, path = self.pending_document_loads.popleft()
        if key not in self.document_states:
            self._start_next_document_load()
            return
        self.document_load_in_progress = True
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        threading.Thread(
            target=self._document_load_worker,
            args=(key, path),
            daemon=True,
        ).start()

    def _document_load_worker(self, key: str, path: Path) -> None:
        try:
            content = read_document(path)
        except Exception as exc:
            self.document_results.put(("error", key, path, exc))
            return
        self.document_results.put(("loaded", key, path, content))

    def _poll_document_load(self) -> None:
        try:
            while True:
                result_type, key, path, payload = self.document_results.get_nowait()
                if result_type == "loaded":
                    self._finish_document_load(key, path, payload)
                else:
                    self._finish_document_error(key, path, payload)
        except queue.Empty:
            pass
        self.after(50, self._poll_document_load)

    def _finish_document_error(self, key: str, path: Path, exception: Exception) -> None:
        self.document_load_in_progress = False
        state = self.document_states.get(key)
        if state is None:
            self._start_next_document_load()
            return
        state.loading = False
        state.error = str(exception)
        state.text.insert("1.0", f"Could not open {path.name}.\n\n{exception}")
        state.text.edit_modified(False)
        self._set_tab_label(state)
        self._log_error(exception)
        self.status_var.set(f"Could not open {path.name}")
        self._start_next_document_load()

    def _finish_document_load(self, key: str, path: Path, content: str) -> None:
        self.document_load_in_progress = False
        state = self.document_states.get(key)
        if state is None:
            self._start_next_document_load()
            return
        state.loading = False
        state.error = None
        state.text.delete("1.0", "end")
        state.text.insert("1.0", content)
        state.path = path
        state.dirty = False
        state.text.edit_modified(False)
        self._set_tab_label(state)
        self._add_recent_file(path)
        if key == self.active_document_id:
            self._activate_document(key)
        self.status_var.set(f"Opened {path.name}")
        self._start_next_document_load()

    def save_document(self, save_as: bool = False) -> bool:
        path = self.current_path
        editable = {".txt", ".md", ".log", ".csv", ".srt", ".vtt"}
        if save_as or path is None or path.suffix.lower() not in editable:
            initial = path.stem + ".txt" if path else "document.txt"
            selected = filedialog.asksaveasfilename(
                defaultextension=".txt",
                initialfile=initial,
                filetypes=[("Text files", "*.txt"), ("Markdown files", "*.md"), ("All files", "*.*")],
            )
            if not selected:
                return False
            path = Path(selected)
        try:
            path.write_text(self.text.get("1.0", "end-1c"), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Save Failed", f"Could not save the document.\n\n{exc}")
            return False
        self.current_path = path
        self.dirty = False
        self.text.edit_modified(False)
        self._add_recent_file(path)
        self._update_title()
        self.status_var.set(f"Saved {path.name}")
        return True

    def _add_recent_file(self, path: Path) -> None:
        value = str(path)
        self.recent_files = [item for item in self.recent_files if item.lower() != value.lower()]
        self.recent_files.insert(0, value)
        self.recent_files = self.recent_files[:10]
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.delete(0, "end")
        existing = [item for item in self.recent_files if Path(item).exists()]
        if not existing:
            self.recent_menu.add_command(label="(No recent files)", state="disabled")
            return
        for item in existing:
            self.recent_menu.add_command(label=item, command=lambda item=item: self._open_recent(item))
        self.recent_menu.add_separator()
        self.recent_menu.add_command(label="Clear Recent Files", command=self._clear_recent_files)

    def _open_recent(self, item: str) -> None:
        if self._confirm_save_changes():
            self._load_path(Path(item))

    def _clear_recent_files(self) -> None:
        self.recent_files.clear()
        self._rebuild_recent_menu()

    def show_find_bar(self) -> None:
        if not self.find_bar_visible:
            self.find_frame.grid(row=4, column=0, sticky="ew")
            self.find_bar_visible = True
        try:
            selected = self.text.get("sel.first", "sel.last")
            if selected and "\n" not in selected and len(selected) < 100:
                self.find_var.set(selected)
        except tk.TclError:
            pass
        self.find_entry.focus_set()
        self.find_entry.select_range(0, "end")

    def hide_find_bar(self) -> None:
        if self.find_bar_visible:
            self.find_frame.grid_remove()
            self.find_bar_visible = False
            self.text.tag_remove("find_match", "1.0", "end")
            self.text.focus_set()

    def highlight_find_matches(self) -> None:
        self.text.tag_remove("find_match", "1.0", "end")
        query = self.find_var.get()
        if not query:
            return
        start = "1.0"
        while True:
            found = self.text.search(query, start, stopindex="end", nocase=True)
            if not found:
                break
            end = f"{found}+{len(query)}c"
            self.text.tag_add("find_match", found, end)
            start = end

    def find_next(self, backward: bool = False) -> None:
        query = self.find_var.get()
        if not query:
            return
        start = self.text.index("sel.first") if backward and self.text.tag_ranges("sel") else self.text.index("insert")
        found = self.text.search(
            query,
            start,
            stopindex="1.0" if backward else "end",
            backwards=backward,
            nocase=True,
        )
        if not found:
            found = self.text.search(query, "end" if backward else "1.0", stopindex="1.0" if backward else "end", backwards=backward, nocase=True)
        if found:
            end = f"{found}+{len(query)}c"
            self.text.tag_remove("sel", "1.0", "end")
            self.text.tag_add("sel", found, end)
            self.text.mark_set("insert", end)
            self.text.see(found)

    def replace_one(self) -> None:
        try:
            selected = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            selected = ""
        if selected.lower() == self.find_var.get().lower():
            self.text.delete("sel.first", "sel.last")
            self.text.insert("insert", self.replace_var.get())
        self.find_next()

    def replace_all(self) -> None:
        query = self.find_var.get()
        if not query:
            return
        content = self.text.get("1.0", "end-1c")
        updated, count = re.subn(re.escape(query), lambda _match: self.replace_var.get(), content, flags=re.IGNORECASE)
        if count:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", updated)
            self.status_var.set(f"Replaced {count} occurrence{'s' if count != 1 else ''}")

    def go_to_line(self) -> None:
        line = simpledialog.askinteger("Go to Line", "Line number:", minvalue=1, parent=self)
        if line:
            index = f"{line}.0"
            self.text.mark_set("insert", index)
            self.text.see(index)
            self.text.focus_set()

    def toggle_word_wrap(self) -> None:
        for state in self.document_states.values():
            state.text.configure(wrap="word" if self.wrap_var.get() else "none")

    def change_font_size(self, delta: int) -> None:
        self.font_size = max(8, min(32, self.font_size + delta))
        for state in self.document_states.values():
            state.text.configure(font=("Segoe UI", self.font_size))
        self.status_var.set(f"Editor zoom: {self.font_size} pt")

    def reset_font_size(self) -> None:
        self.font_size = 12
        for state in self.document_states.values():
            state.text.configure(font=("Segoe UI", self.font_size))

    def _apply_glossary(self, text: str) -> str:
        for entry in sorted(self.glossary, key=lambda item: len(item.get("source", "")), reverse=True):
            source = entry.get("source", "").strip()
            replacement = entry.get("replacement", "").strip()
            if not source:
                continue
            boundary = r"\b" if source[0].isalnum() and source[-1].isalnum() else ""
            text = re.sub(boundary + re.escape(source) + boundary, lambda _match, replacement=replacement: replacement, text, flags=re.IGNORECASE)
        return text

    def _prepare_for_speech(self, text: str) -> str:
        return prepare_speech_text(self._apply_glossary(text))

    def open_glossary(self) -> None:
        window = tk.Toplevel(self)
        window.title("Pronunciation Glossary")
        window.geometry("700x430")
        window.transient(self)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)

        frame = ttk.Frame(window, padding=10)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=("source", "replacement"), show="headings", selectmode="browse")
        tree.heading("source", text="Text")
        tree.heading("replacement", text="Pronounce As")
        tree.column("source", width=260)
        tree.column("replacement", width=340)
        tree.grid(row=0, column=0, columnspan=5, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scrollbar.grid(row=0, column=5, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)

        source_var = tk.StringVar()
        replacement_var = tk.StringVar()
        ttk.Label(frame, text="Text").grid(row=1, column=0, sticky="w", pady=(10, 3))
        ttk.Label(frame, text="Pronounce As").grid(row=1, column=2, sticky="w", pady=(10, 3))
        source_entry = ttk.Entry(frame, textvariable=source_var)
        source_entry.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(0, 8))
        ttk.Entry(frame, textvariable=replacement_var).grid(row=2, column=2, columnspan=2, sticky="ew", padx=(0, 8))

        def refresh() -> None:
            tree.delete(*tree.get_children())
            for index, entry in enumerate(self.glossary):
                tree.insert("", "end", iid=str(index), values=(entry.get("source", ""), entry.get("replacement", "")))

        def selected_index() -> int | None:
            selection = tree.selection()
            return int(selection[0]) if selection else None

        def add_or_update() -> None:
            source = source_var.get().strip()
            replacement = replacement_var.get().strip()
            if not source or not replacement:
                messagebox.showwarning("Glossary", "Enter both the text and its pronunciation.", parent=window)
                return
            index = selected_index()
            value = {"source": source, "replacement": replacement}
            if index is None:
                self.glossary.append(value)
            else:
                self.glossary[index] = value
            source_var.set("")
            replacement_var.set("")
            refresh()

        def remove() -> None:
            index = selected_index()
            if index is not None:
                del self.glossary[index]
                refresh()

        def load_selection(_event=None) -> None:
            index = selected_index()
            if index is not None:
                source_var.set(self.glossary[index].get("source", ""))
                replacement_var.set(self.glossary[index].get("replacement", ""))

        def import_csv() -> None:
            path = filedialog.askopenfilename(parent=window, filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
            if not path:
                return
            with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.reader(handle):
                    if len(row) >= 2 and row[0].strip() and row[1].strip():
                        self.glossary.append({"source": row[0].strip(), "replacement": row[1].strip()})
            refresh()

        def export_csv() -> None:
            path = filedialog.asksaveasfilename(parent=window, defaultextension=".csv", filetypes=[("CSV files", "*.csv")])
            if not path:
                return
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["Text", "Pronounce As"])
                for entry in self.glossary:
                    writer.writerow([entry.get("source", ""), entry.get("replacement", "")])

        tree.bind("<<TreeviewSelect>>", load_selection)
        ttk.Button(frame, text="Add / Update", command=add_or_update).grid(row=2, column=4, padx=2)
        ttk.Button(frame, text="Delete", command=remove).grid(row=2, column=5, padx=2)
        actions = ttk.Frame(frame)
        actions.grid(row=3, column=0, columnspan=6, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="Import CSV…", command=import_csv).pack(side="left")
        ttk.Button(actions, text="Export CSV…", command=export_csv).pack(side="left", padx=6)
        ttk.Label(actions, text="Glossary replacements are applied only during speech generation.", style="Muted.TLabel").pack(side="left", padx=10)
        ttk.Button(actions, text="Close", command=lambda: (self._save_settings(), window.destroy())).pack(side="right")
        refresh()
        source_entry.focus_set()

    def add_selection_to_glossary(self) -> None:
        try:
            selected = self.text.get("sel.first", "sel.last").strip()
        except tk.TclError:
            selected = ""
        if not selected:
            messagebox.showinfo("Glossary", "Select a word or phrase first.")
            return
        replacement = simpledialog.askstring("Pronunciation Glossary", f"Pronounce “{selected}” as:", parent=self)
        if replacement:
            self.glossary.append({"source": selected, "replacement": replacement.strip()})
            self.status_var.set(f"Added “{selected}” to the glossary")

    def _selected_text_and_offsets(self, selection_only: bool) -> tuple[str, int]:
        try:
            start = self.text.index("sel.first")
            end = self.text.index("sel.last")
            selected = self.text.get(start, end).strip()
            if selected:
                offset = len(self.text.get("1.0", start))
                return selected, offset
        except tk.TclError:
            pass
        if selection_only:
            return "", 0
        return self.text.get("1.0", "end-1c").strip(), 0

    def preview_voice(self) -> None:
        engine_name = self.engine_var.get().replace(" (Local)", "").replace(" (Online)", "")
        self._start_synthesis(
            f"This is a preview of the selected {engine_name} voice.",
            PREVIEW_PATH,
            autoplay=True,
            reading_offset=0,
        )

    def read_all(self) -> None:
        content = self.text.get("1.0", "end-1c")
        cursor_index = self.text.index("insert")
        cursor_offset = len(self.text.get("1.0", cursor_index))
        self._start_stream_read(cursor_offset, len(content))

    def read_selection(self) -> None:
        try:
            start_index = self.text.index("sel.first")
            end_index = self.text.index("sel.last")
        except tk.TclError:
            messagebox.showinfo("Read Selection", "Select text in the document first.")
            return
        start_offset = len(self.text.get("1.0", start_index))
        end_offset = len(self.text.get("1.0", end_index))
        self._start_stream_read(start_offset, end_offset)

    def _start_stream_read(self, start_offset: int, end_offset: int) -> None:
        if self.running:
            messagebox.showinfo("Speech Engine Busy", "A speech task is already running.")
            return
        language_name = self.language_var.get()
        voice_name = self.voice_var.get()
        if not self._validate_synthesis_settings():
            return
        content = self.text.get("1.0", "end-1c")
        if next_text_segment(content, start_offset, end_offset) is None:
            messagebox.showinfo("Read from Cursor", "There is no readable text after the cursor.")
            return

        self.stop_speech()
        self.player.close()
        self.cancel_event.clear()
        self.stream_active = True
        self.stream_generation += 1
        self.stream_next_offset = max(0, start_offset)
        self.stream_end_offset = min(len(content), end_offset)
        self.stream_segment_number = 0
        self.stream_voice_settings = self._current_voice_profile(language_name, voice_name)
        self._start_next_stream_segment()

    def _start_next_stream_segment(self) -> None:
        if not self.stream_active or self.cancel_event.is_set() or self.stream_voice_settings is None:
            return
        content = self.text.get("1.0", "end-1c")
        segment_info = next_text_segment(content, self.stream_next_offset, self.stream_end_offset)
        if segment_info is None:
            self.stream_active = False
            self.text.tag_remove("reading", "1.0", "end")
            self.status_var.set("Finished reading from cursor")
            self.progress["value"] = 100
            return

        segment, segment_start, _segment_end, next_offset = segment_info
        self.stream_next_offset = next_offset
        self.stream_segment_number += 1
        self.reading_start_offset = segment_start
        self.reading_text = segment
        self.player.close()
        self.running = True
        self._set_busy(True)
        generation = self.stream_generation
        voice_profile = dict(self.stream_voice_settings)
        spoken_segment = self._prepare_for_speech(segment)
        overall = 100 * max(0, segment_start) / max(1, self.stream_end_offset)
        self.status_var.set(f"Generating reading segment {self.stream_segment_number}…")
        self.progress["value"] = overall
        threading.Thread(
            target=self._stream_synthesis_worker,
            args=(spoken_segment, generation, voice_profile),
            daemon=True,
        ).start()

    def _stream_synthesis_worker(
        self,
        text: str,
        generation: int,
        voice_profile: dict[str, object],
    ) -> None:
        try:
            audio = self._synthesize_audio(
                text, voice_profile,
                status_text=f"Generating reading segment {self.stream_segment_number}…",
            )
            if self.cancel_event.is_set() or generation != self.stream_generation:
                return
            write_audio_file(
                STREAM_PATH,
                audio,
                "wav16",
                source_sample_rate=int(voice_profile.get("sample_rate", SAMPLE_RATE)),
            )
            self.after(0, lambda: self._play_stream_segment(generation))
        except InterruptedError:
            self._set_status("Reading stopped", 0)
        except Exception as exc:
            self._log_error(exc)
            self.stream_active = False
            self._set_status(f"Error: {exc}", 0)
            self.after(0, lambda exc=exc: messagebox.showerror("Speech Generation Failed", str(exc)))
        finally:
            self.running = False
            self.after(0, lambda: self._set_busy(False))

    def _play_stream_segment(self, generation: int) -> None:
        if not self.stream_active or self.cancel_event.is_set() or generation != self.stream_generation:
            return
        try:
            self.player.open(STREAM_PATH)
            self.player.play()
            self.last_player_mode = "playing"
            self.status_var.set(f"Reading segment {self.stream_segment_number}")
        except Exception as exc:
            self.stream_active = False
            self._log_error(exc)
            messagebox.showerror("Playback Failed", str(exc))

    def export_audio(self) -> None:
        text, _offset = self._selected_text_and_offsets(False)
        if not text:
            messagebox.showwarning("No Text", "Enter or open some text first.")
            return
        if not self._validate_synthesis_settings():
            return
        if not self.open_audio_settings("Export Audio Settings"):
            return
        details = AUDIO_FORMATS[self.output_format]
        extension = str(details["extension"])
        stem = self.current_path.stem if self.current_path else "speech_output"
        output = filedialog.asksaveasfilename(
            defaultextension=extension,
            initialfile=f"{stem}{extension}",
            filetypes=[(f"{details['label']} audio", f"*{extension}"), *AUDIO_FILE_TYPES],
        )
        if output:
            output_path = Path(output)
            if not output_path.suffix:
                output_path = output_path.with_suffix(extension)
            chatterbox_reference, _conditioning = self._chatterbox_profile_paths()
            if (
                self._engine_key() == "chatterbox_flash"
                and chatterbox_reference
                and normalized_path_key(output_path) == normalized_path_key(Path(chatterbox_reference))
            ):
                messagebox.showerror(
                    "Export Audio",
                    "The export path cannot overwrite the active Chatterbox reference clip.",
                    parent=self,
                )
                return
            format_key = format_key_for_path(output_path, self.output_format)
            self._start_synthesis(
                text, output_path, autoplay=False, reading_offset=0,
                audio_profile=self._current_audio_profile(format_key),
            )

    def _start_synthesis(
        self,
        text: str,
        output: Path,
        autoplay: bool,
        reading_offset: int,
        audio_profile: dict[str, object] | None = None,
    ) -> None:
        if self.running:
            messagebox.showinfo("Speech Engine Busy", "A speech task is already running.")
            return
        text = self._prepare_for_speech(text)
        if not text.strip():
            messagebox.showwarning("No Text", "Enter or open some text first.")
            return
        language_name = self.language_var.get()
        voice_name = self.voice_var.get()
        if not self._validate_synthesis_settings():
            return
        self.stop_speech()
        # MCI keeps a WAV file locked even after playback reaches the end.
        # Release the previous preview/read file before the worker overwrites it.
        self.player.close()
        self.running = True
        self.cancel_event.clear()
        self.reading_start_offset = reading_offset
        self.reading_text = text
        voice_profile = self._current_voice_profile(language_name, voice_name)
        self._set_busy(True)
        thread = threading.Thread(
            target=self._synthesis_worker,
            args=(
                text, output, autoplay, voice_profile, audio_profile,
            ),
            daemon=True,
        )
        thread.start()

    def _ensure_pipeline(
        self,
        language: str,
        repo_id: str,
        selection: RuntimeSelection | None = None,
    ):
        pipeline_key = (repo_id, language)
        with self.kokoro_pipeline_lock:
            existing = self.kokoro_pipelines.get(pipeline_key)
            if existing is not None:
                return existing
            model = self.kokoro_models.get(repo_id)
            active_repositories = set(self.kokoro_models)
        if active_repositories and repo_id not in active_repositories:
            self._release_kokoro_runtime()
            model = None
        self._set_status(
            "Loading Kokoro model in the background…" if model is None
            else "Preparing Kokoro language pipeline…",
            0,
        )
        _torch, pipeline_class, device = load_kokoro_runtime()
        if model is None:
            self.pipeline_device = str(device)
            pipeline = pipeline_class(
                lang_code=language,
                repo_id=repo_id,
                device=self.pipeline_device,
            )
        else:
            pipeline = pipeline_class(
                lang_code=language,
                repo_id=repo_id,
                model=model,
            )
        if selection is not None and not self.runtime_coordinator.is_desired(selection):
            del pipeline
            gc.collect()
            if _torch.cuda.is_available():
                _torch.cuda.empty_cache()
            return None
        with self.kokoro_pipeline_lock:
            if model is None:
                self.kokoro_models.clear()
                self.kokoro_pipelines.clear()
                self.kokoro_loaded_voices.clear()
                self.kokoro_voice_tensors.clear()
                self.kokoro_models[repo_id] = pipeline.model
            self.kokoro_pipelines[pipeline_key] = pipeline
        try:
            self.after(0, lambda: self._update_runtime_label(self.pipeline_device or "cpu"))
        except RuntimeError:
            pass
        return pipeline

    def _release_kokoro_runtime(self) -> None:
        with self.kokoro_pipeline_lock:
            if not self.kokoro_models and not self.kokoro_pipelines:
                return
            self.kokoro_models.clear()
            self.kokoro_pipelines.clear()
            self.kokoro_loaded_voices.clear()
            self.kokoro_voice_tensors.clear()
            self.kokoro_preloading.clear()
        self.pipeline_device = None
        gc.collect()
        torch_runtime = loaded_kokoro_torch()
        if torch_runtime is not None and torch_runtime.cuda.is_available():
            torch_runtime.cuda.empty_cache()

    def _start_kokoro_preload(self, language: str | None = None, voice: str | None = None) -> None:
        self._schedule_runtime_preload()

    def _ensure_chatterbox_flash(self):
        snapshot_root = (
            APP_DIR / "models" / "chatterbox-flash" / "hub"
            / "models--ResembleAI--chatterbox-flash" / "snapshots"
        )
        cached = snapshot_root.is_dir() and any(snapshot_root.glob("*/t3_flash.safetensors"))
        self._set_status(
            "Loading cached Chatterbox-Flash model…" if cached
            else "Downloading Chatterbox-Flash model files for first use…",
            0,
        )
        process = self.chatterbox_client.ensure()
        self.chatterbox_flash_device = self.chatterbox_client.device
        try:
            self.after(0, self._update_engine_tab_label)
        except RuntimeError:
            pass
        return process

    def _stop_chatterbox_worker(self) -> None:
        self.chatterbox_client.stop()
        self.chatterbox_flash_device = None

    def _prepare_chatterbox_reference(self, reference_audio: str) -> None:
        reference = Path(reference_audio) if reference_audio else None
        name = reference.name if reference is not None else "reference audio"
        self._set_status(f"Preparing Chatterbox voice from {name}…", 0)
        self.chatterbox_client.prepare_reference(reference_audio)

    def _synthesize_chatterbox_audio(
        self, text: str, reference_audio: str, rate: float, pitch: float,
    ) -> np.ndarray:
        return self.chatterbox_client.synthesize(text, reference_audio, rate, pitch)

    def _start_chatterbox_flash_preload(self) -> None:
        self._schedule_runtime_preload()

    def _ensure_supertonic(self):
        if self.supertonic_tts is None:
            model_dir = APP_DIR / "models" / "supertonic3"
            if PORTABLE_MODE and os.environ.get("HF_HUB_OFFLINE") == "1" and not model_dir.exists():
                raise RuntimeError(
                    "Supertonic's model is not installed in this portable build. "
                    "Temporarily disable offline mode and generate speech once to download it."
                )
            self._set_status("Loading Supertonic 3 ONNX model (first use downloads it)…", 0)
            tts_class = load_supertonic_runtime()
            self.supertonic_tts = tts_class(
                model="supertonic-3",
                model_dir=model_dir,
                auto_download=True,
            )
            try:
                self.after(0, self._update_engine_tab_label)
            except RuntimeError:
                pass
        return self.supertonic_tts

    def _update_runtime_label(self, device: str) -> None:
        if self._engine_key() == "kokoro":
            collection = kokoro_collection_label(self._kokoro_collection_key())
            self.voice_panel.configure(text=f"Kokoro-82M — {collection} ({device.upper()})")

    def _supertonic_batch_rows(
        self,
        supertonic,
        texts: list[str],
        style,
        language: str,
        steps: int,
        speed: float,
    ) -> list[np.ndarray]:
        try:
            batch_style = type(style)(
                np.repeat(style.ttl, len(texts), axis=0),
                np.repeat(style.dp, len(texts), axis=0),
            )
            audio, durations = supertonic.model(texts, batch_style, steps, speed, language)
            duration_values = np.asarray(durations).reshape(-1)
            rows: list[np.ndarray] = []
            for index in range(len(texts)):
                row = np.asarray(audio[index], dtype=np.float32).reshape(-1)
                length = min(len(row), max(1, int(round(float(duration_values[index]) * supertonic.sample_rate))))
                rows.append(row[:length])
            return rows
        except Exception:
            if len(texts) == 1:
                raise
            midpoint = max(1, len(texts) // 2)
            return [
                *self._supertonic_batch_rows(
                    supertonic, texts[:midpoint], style, language, steps, speed,
                ),
                *self._supertonic_batch_rows(
                    supertonic, texts[midpoint:], style, language, steps, speed,
                ),
            ]

    def _chatterbox_batch_rows(
        self,
        texts: list[str],
        voice_profile: dict[str, object],
    ) -> list[np.ndarray]:
        try:
            return self.chatterbox_client.synthesize_batch(
                texts,
                str(voice_profile.get("reference_audio", "")),
                float(voice_profile["rate"]),
                float(voice_profile["pitch"]),
                str(voice_profile.get("chatterbox_profile_id", "legacy")) or "legacy",
                str(voice_profile.get("conditioning_path", "")),
            )
        except Exception:
            if len(texts) == 1:
                raise
            midpoint = max(1, len(texts) // 2)
            return [
                *self._chatterbox_batch_rows(texts[:midpoint], voice_profile),
                *self._chatterbox_batch_rows(texts[midpoint:], voice_profile),
            ]

    def _chatterbox_batch_size(self) -> int:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            free_mib = max(int(line.strip()) for line in result.stdout.splitlines() if line.strip())
            return 4 if free_mib >= 12288 else 2
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 2

    def _iter_synthesized_pieces(
        self,
        text: str,
        voice_profile: dict[str, object],
        status_text: str | None = None,
    ):
        language = str(voice_profile["language"])
        voice = str(voice_profile["voice"])
        engine = str(voice_profile.get("engine", "kokoro"))
        chunks = split_text(text, limit=280 if engine == "supertonic" else 800)
        if not chunks:
            raise ValueError("No readable text was found.")
        source_rate = max(8000, int(voice_profile.get("sample_rate", SAMPLE_RATE)))
        rate = float(voice_profile["rate"])
        pitch = float(voice_profile["pitch"])
        selection = voice_profile.get("runtime_selection")
        if isinstance(selection, RuntimeSelection) and engine != "edge":
            self.runtime_coordinator.ensure_current(selection, self._load_runtime_snapshot)
            if engine == "chatterbox_flash":
                reference_audio, conditioning_path = self._chatterbox_profile_paths(selection.profile_id)
                voice_profile = {
                    **voice_profile,
                    "reference_audio": reference_audio,
                    "conditioning_path": conditioning_path,
                }
        kokoro_voice = voice
        pipeline = None
        if engine == "kokoro":
            collection = normalize_kokoro_collection(str(voice_profile.get("kokoro_collection", "")))
            voice_collection, voice_id = split_stable_kokoro_voice_id(voice)
            if voice_collection != collection:
                raise RuntimeError("The selected Kokoro voice does not belong to the active collection.")
            repo_id = kokoro_model_repository(collection)
            kokoro_voice = resolve_kokoro_voice(collection, voice_id)
            with self.kokoro_pipeline_lock:
                pipeline = self.kokoro_pipelines.get((repo_id, language))
            if pipeline is None:
                raise RuntimeError("The selected Kokoro pipeline did not finish loading.")
        supertonic = self.supertonic_tts if engine == "supertonic" else None
        if engine == "supertonic" and supertonic is None:
            raise RuntimeError("The Supertonic runtime did not finish loading.")
        supertonic_style = self.supertonic_styles.get(voice) if supertonic is not None else None
        if supertonic is not None and supertonic_style is None:
            supertonic_style = supertonic.get_voice_style(voice)
            self.supertonic_styles[voice] = supertonic_style
        pitch_factor = 2.0 ** (pitch / 12.0)
        generated_any = False

        if engine == "supertonic":
            assert supertonic is not None and supertonic_style is not None
            synthesis_speed = max(0.7, min(2.0, rate / pitch_factor))
            steps = int(voice_profile.get("supertonic_steps", 8))
            batch_size = 4
            for batch_start in range(0, len(chunks), batch_size):
                if self.cancel_event.is_set():
                    raise InterruptedError("Speech task cancelled")
                batch = chunks[batch_start:batch_start + batch_size]
                self._set_status(
                    status_text or f"Generating segments {batch_start + 1}–{batch_start + len(batch)} of {len(chunks)}…",
                    batch_start * 100 / len(chunks),
                )
                rows = self._supertonic_batch_rows(
                    supertonic, batch, supertonic_style, language, steps, synthesis_speed,
                )
                for row_index, audio in enumerate(rows, start=batch_start):
                    value = apply_pitch(np.asarray(audio, dtype=np.float32).reshape(-1), pitch)
                    value = apply_chunk_edge_fade(value, sample_rate=source_rate)
                    generated_any = True
                    yield value
                    if row_index < len(chunks) - 1:
                        yield np.zeros(int(0.3 * source_rate), dtype=np.float32)
            if not generated_any:
                raise RuntimeError("Supertonic returned no audio.")
            return

        if engine == "chatterbox_flash":
            batch_size = self._chatterbox_batch_size()
            for batch_start in range(0, len(chunks), batch_size):
                if self.cancel_event.is_set():
                    raise InterruptedError("Speech task cancelled")
                batch = chunks[batch_start:batch_start + batch_size]
                self._set_status(
                    status_text or f"Generating segments {batch_start + 1}–{batch_start + len(batch)} of {len(chunks)}…",
                    batch_start * 100 / len(chunks),
                )
                for audio in self._chatterbox_batch_rows(batch, voice_profile):
                    value = apply_chunk_edge_fade(
                        np.asarray(audio, dtype=np.float32).reshape(-1), sample_rate=source_rate,
                    )
                    generated_any = True
                    yield value
            if not generated_any:
                raise RuntimeError("Chatterbox-Flash returned no audio.")
            return

        for index, chunk in enumerate(chunks, start=1):
            if self.cancel_event.is_set():
                raise InterruptedError("Speech task cancelled")
            self._set_status(
                status_text or f"Generating segment {index} of {len(chunks)}…",
                (index - 1) * 100 / len(chunks),
            )
            if engine == "edge":
                synthesis_speed = max(0.5, min(2.0, rate))
                audio, actual_rate = synthesize_edge_audio(
                    chunk, voice, synthesis_speed, pitch, self.cancel_event,
                )
                if actual_rate != source_rate:
                    raise RuntimeError(
                        f"Edge TTS returned an unexpected {actual_rate:,} Hz stream; expected {source_rate:,} Hz."
                    )
                generated = [audio]
            else:
                synthesis_speed = max(0.5, min(2.0, rate / pitch_factor))
                generated = []
                for _graphemes, _phonemes, audio in pipeline(
                    chunk, voice=kokoro_voice, speed=synthesis_speed,
                ):
                    if hasattr(audio, "detach"):
                        audio = audio.detach().cpu().numpy()
                    generated.append(np.asarray(audio, dtype=np.float32).reshape(-1))

            for audio in generated:
                if hasattr(audio, "detach"):
                    audio = audio.detach().cpu().numpy()
                value = np.asarray(audio, dtype=np.float32).reshape(-1)
                if engine != "edge":
                    value = apply_pitch(value, pitch)
                value = apply_chunk_edge_fade(value, sample_rate=source_rate)
                generated_any = True
                yield value
        if not generated_any:
            raise RuntimeError(f"{ENGINE_LABEL_BY_KEY.get(engine, engine)} returned no audio.")

    def _synthesize_audio(
        self,
        text: str,
        voice_profile: dict[str, object],
        status_text: str | None = None,
    ) -> np.ndarray:
        pieces = list(self._iter_synthesized_pieces(text, voice_profile, status_text))
        return np.concatenate(pieces).astype(np.float32, copy=False)

    def _synthesize_edge_to_file(
        self,
        text: str,
        output: Path,
        voice_profile: dict[str, object],
        audio_profile: dict[str, object],
    ) -> None:
        chunks = split_text(text)
        if not chunks:
            raise ValueError("No readable text was found.")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.stem}_", suffix=output.suffix, dir=output.parent, delete=False,
        ) as handle:
            temp_path = Path(handle.name)
        command = [
            str(FFMPEG_EXE), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "mp3", "-i", "pipe:0",
        ]
        command.extend(ffmpeg_audio_output_args(
            str(audio_profile["format"]),
            SAMPLE_RATE,
            int(audio_profile["bitrate"]),
            str(audio_profile["bitrate_mode"]),
            int(audio_profile["vbr_quality"]),
            str(audio_profile["sample_rate"]),
            str(audio_profile["channels"]),
            int(audio_profile["codec_effort"]),
        ))
        command.append(str(temp_path))
        encoder = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            assert encoder.stdin is not None

            def write_audio(data: bytes) -> None:
                if self.cancel_event.is_set():
                    raise InterruptedError("Speech task cancelled")
                encoder.stdin.write(data)

            def update_segment(index: int, total: int) -> None:
                self._set_status(
                    f"Generating Edge segment {index} of {total}…",
                    (index - 1) * 100 / total,
                )

            stream_edge_audio_bytes(
                chunks,
                str(voice_profile["voice"]),
                float(voice_profile["rate"]),
                float(voice_profile["pitch"]),
                self.cancel_event,
                write_audio,
                update_segment,
            )
            encoder.stdin.close()
            encoder.stdin = None
            _stdout, stderr = encoder.communicate()
            if encoder.returncode:
                details = (stderr or b"").decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"Audio encoding failed: {details or 'FFmpeg returned an error.'}")
            if not temp_path.is_file() or temp_path.stat().st_size == 0:
                raise RuntimeError("Edge TTS produced an empty output file.")
            os.replace(temp_path, output)
        except Exception:
            if encoder.poll() is None:
                encoder.terminate()
                try:
                    encoder.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    encoder.kill()
                    encoder.communicate()
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _synthesize_to_file(
        self,
        text: str,
        output: Path,
        voice_profile: dict[str, object],
        audio_profile: dict[str, object],
    ) -> None:
        if str(voice_profile.get("engine", "kokoro")) == "edge":
            self._synthesize_edge_to_file(text, output, voice_profile, audio_profile)
            return
        source_rate = max(8000, int(voice_profile.get("sample_rate", SAMPLE_RATE)))
        format_key = str(audio_profile["format"])
        requested_rate = str(audio_profile["sample_rate"])
        target_rate = source_rate if requested_rate == "Voice native" else safe_int(requested_rate, source_rate)
        direct_pcm = format_key in {"wav16", "wav24"} and target_rate == source_rate \
            and str(audio_profile["channels"]) == "Mono"
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.stem}_", suffix=output.suffix, dir=output.parent, delete=False,
        ) as handle:
            temp_path = Path(handle.name)
        samples_written = 0
        encoder: subprocess.Popen | None = None
        try:
            pieces = self._iter_synthesized_pieces(text, voice_profile)
            if direct_pcm:
                subtype = "PCM_16" if format_key == "wav16" else "PCM_24"
                with sf.SoundFile(temp_path, mode="w", samplerate=source_rate, channels=1, subtype=subtype) as writer:
                    for piece in pieces:
                        if self.cancel_event.is_set():
                            raise InterruptedError("Speech task cancelled")
                        writer.write(piece)
                        samples_written += len(piece)
            else:
                command = [
                    str(FFMPEG_EXE), "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "f32le", "-ar", str(source_rate), "-ac", "1", "-i", "pipe:0",
                ]
                command.extend(ffmpeg_audio_output_args(
                    format_key,
                    source_rate,
                    int(audio_profile["bitrate"]),
                    str(audio_profile["bitrate_mode"]),
                    int(audio_profile["vbr_quality"]),
                    requested_rate,
                    str(audio_profile["channels"]),
                    int(audio_profile["codec_effort"]),
                ))
                command.append(str(temp_path))
                encoder = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if encoder.stdin is None:
                    raise RuntimeError("Could not open the audio encoder input stream.")
                for piece in pieces:
                    if self.cancel_event.is_set():
                        raise InterruptedError("Speech task cancelled")
                    try:
                        encoder.stdin.write(np.asarray(piece, dtype="<f4").reshape(-1).tobytes())
                    except BrokenPipeError as exc:
                        details = encoder.stderr.read().decode("utf-8", errors="replace").strip() \
                            if encoder.stderr is not None else ""
                        encoder.wait()
                        raise RuntimeError(f"Audio encoding failed: {details or 'FFmpeg stopped unexpectedly.'}") from exc
                    samples_written += len(piece)
                encoder.stdin.close()
                encoder.stdin = None
                _stdout, stderr = encoder.communicate()
                if encoder.returncode:
                    details = (stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(f"Audio encoding failed: {details or 'FFmpeg returned an error.'}")
            if samples_written == 0:
                engine = str(voice_profile.get("engine", "kokoro"))
                raise RuntimeError(f"{ENGINE_LABEL_BY_KEY.get(engine, engine)} returned no audio.")
            self._set_status("Finalizing audio…", 99)
            os.replace(temp_path, output)
        except Exception:
            if encoder is not None and encoder.poll() is None:
                encoder.terminate()
                try:
                    encoder.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    encoder.kill()
                    encoder.communicate()
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _synthesis_worker(
        self,
        text: str,
        output: Path,
        autoplay: bool,
        voice_profile: dict[str, object],
        audio_profile: dict[str, object] | None,
    ) -> None:
        try:
            profile = audio_profile or {
                "format": "wav16", "bitrate": 128, "bitrate_mode": "CBR", "vbr_quality": 2,
                "sample_rate": "Voice native", "channels": "Mono", "codec_effort": 8,
            }
            if audio_profile is not None and not autoplay:
                self._synthesize_to_file(text, output, voice_profile, profile)
            else:
                audio = self._synthesize_audio(text, voice_profile)
                write_audio_file(
                    output,
                    audio,
                    str(profile["format"]),
                    int(profile["bitrate"]),
                    str(profile["bitrate_mode"]),
                    int(profile["vbr_quality"]),
                    str(profile["sample_rate"]),
                    str(profile["channels"]),
                    int(profile["codec_effort"]),
                    int(voice_profile.get("sample_rate", SAMPLE_RATE)),
                )
            if autoplay:
                self.after(0, lambda: self._play_generated(output))
            else:
                self._set_status(f"Finished: {output}", 100)
                self.after(0, lambda: messagebox.showinfo("Audio Exported", f"Audio created successfully:\n\n{output}"))
        except InterruptedError:
            self._set_status("Speech task cancelled", 0)
        except Exception as exc:
            self._log_error(exc)
            self._set_status(f"Error: {exc}", 0)
            self.after(0, lambda exc=exc: messagebox.showerror("Speech Generation Failed", str(exc)))
        finally:
            self.running = False
            self.after(0, lambda: self._set_busy(False))

    def _play_generated(self, output: Path) -> None:
        try:
            self.player.open(output)
            self.player.play()
            self.last_player_mode = "playing"
            self.status_var.set("Reading aloud")
        except Exception as exc:
            self._log_error(exc)
            messagebox.showerror("Playback Failed", str(exc))

    def pause_speech(self) -> None:
        try:
            self.player.pause()
            if self.player.loaded:
                self.status_var.set("Paused")
        except RuntimeError as exc:
            messagebox.showerror("Playback", str(exc))

    def resume_speech(self) -> None:
        try:
            self.player.resume()
            if self.player.loaded:
                self.status_var.set("Reading aloud")
        except RuntimeError as exc:
            messagebox.showerror("Playback", str(exc))

    def stop_speech(self) -> None:
        self.stream_active = False
        self.stream_generation += 1
        self.cancel_event.set()
        self.player.stop()
        self.player.close()
        self.last_player_mode = "stopped"
        self.reading_text = ""
        self.text.tag_remove("reading", "1.0", "end")
        self.progress["value"] = 0
        self.playback_var.set("00:00 / 00:00")
        if not self.running:
            self.status_var.set("Stopped")

    def _poll_player(self) -> None:
        try:
            if self.player.loaded:
                mode = self.player.mode()
                position = self.player.position()
                length = self.player.length()
                self.playback_var.set(f"{format_time(position)} / {format_time(length)}")
                self.progress["value"] = (position * 100 / length) if length else 0
                if mode in {"playing", "paused"} and length:
                    self._update_reading_highlight(position / length)
                if self.last_player_mode in {"playing", "paused"} and mode == "stopped" and position >= max(0, length - 250):
                    self.text.tag_remove("reading", "1.0", "end")
                    if self.stream_active and not self.cancel_event.is_set():
                        self.player.close()
                        self.last_player_mode = "stopped"
                        self._start_next_stream_segment()
                    else:
                        self.status_var.set("Finished reading")
                        self.progress["value"] = 100
                        self.last_player_mode = mode
                else:
                    self.last_player_mode = mode
        except Exception:
            pass
        finally:
            self.after(250, self._poll_player)

    def _update_reading_highlight(self, fraction: float) -> None:
        if not self.reading_text:
            return
        relative = min(len(self.reading_text) - 1, max(0, int(len(self.reading_text) * fraction)))
        absolute = self.reading_start_offset + relative
        content = self.text.get("1.0", "end-1c")
        if not content:
            return
        absolute = min(len(content) - 1, absolute)
        start = absolute
        end = absolute
        while start > 0 and not content[start - 1].isspace():
            start -= 1
        while end < len(content) and not content[end].isspace():
            end += 1
        start_index = f"1.0+{start}c"
        end_index = f"1.0+{max(start + 1, end)}c"
        self.text.tag_remove("reading", "1.0", "end")
        self.text.tag_add("reading", start_index, end_index)
        self.text.see(start_index)

    def _set_busy(self, busy: bool) -> None:
        self._refresh_action_states()
        if not busy and not self.running and self.preload_after_speech:
            self._schedule_runtime_preload()
        if not busy and self.player.mode() == "stopped" and self.status_var.get().startswith("Generating"):
            self.status_var.set("Ready")

    def _set_status(self, text: str, progress: float | None = None) -> None:
        def update() -> None:
            self.status_var.set(text)
            if progress is not None:
                self.progress["value"] = progress
        self.after(0, update)

    def _log_error(self, exception: Exception) -> None:
        details = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
        try:
            (APP_DIR / "last_error.txt").write_text(details, encoding="utf-8")
        except OSError:
            pass

    def show_shortcuts(self) -> None:
        messagebox.showinfo(
            "Keyboard Shortcuts",
            "Ctrl+N  New document\nCtrl+O  Open document\nCtrl+S  Save\nCtrl+E  Export Audio\n"
            "Ctrl+F  Find / Replace\nCtrl+G  Go to line\nF5  Read from cursor\nF6  Read selection\n"
            "F7  Pause\nF8  Resume\nF9  Stop\nCtrl++ / Ctrl+-  Zoom editor",
        )

    def show_about(self) -> None:
        torch_runtime = loaded_kokoro_torch()
        if self.pipeline_device == "cuda" and torch_runtime is not None:
            gpu = torch_runtime.cuda.get_device_name(0)
        elif self.pipeline_device:
            gpu = self.pipeline_device.upper()
        else:
            gpu = "Auto-detected on first speech"
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME} {APP_VERSION}\n\nText-to-speech with Kokoro-82M, Microsoft Edge TTS, "
            "Supertonic 3, and Chatterbox-Flash.\n\n"
            f"Kokoro device: {gpu}\nNative sample rates: Kokoro/Edge/Chatterbox-Flash "
            f"{SAMPLE_RATE:,} Hz; Supertonic 44,100 Hz.\n\n"
            "Kokoro, Supertonic, and Chatterbox-Flash run locally. Edge TTS sends the text "
            "being spoken to Microsoft's online speech service.",
        )

    def on_close(self) -> None:
        if self.running and not messagebox.askyesno("Speech Task Running", "Cancel the current speech task and exit?"):
            return
        if not self._confirm_all_save_changes():
            return
        self.document_load_generation += 1
        self.cancel_event.set()
        self.player.close()
        self.runtime_coordinator.close()
        self._stop_chatterbox_worker()
        self._save_settings()
        self.destroy()


if __name__ == "__main__":
    startup_error_path = APP_DIR / "startup_error.txt"
    try:
        app = KokoroApp()
        try:
            startup_error_path.unlink(missing_ok=True)
        except OSError:
            pass
        app.mainloop()
    except Exception:
        # The Windows launcher has no console, so preserve startup failures for
        # troubleshooting instead of exiting without any visible explanation.
        try:
            startup_error_path.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        raise
