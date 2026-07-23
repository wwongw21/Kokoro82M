from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from reader_core.audio import normalized_path_key
from reader_core.config import AUDIO_FORMATS, OPEN_FILE_TYPES, SUPPORTED_DOCUMENT_EXTENSIONS
from reader_core.documents import read_document


class BatchWorkspaceMixin:
    """Batch queue UI and conversion workflow shared by the main Tk application."""

    def _add_to_batch_queue(self, paths: list[Path]) -> None:
        existing = {str(path.resolve()).lower() for path in self.batch_queue if path.exists()}
        for path in paths:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            key = str(resolved).lower()
            if key not in existing and resolved.is_file() and resolved.suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS:
                self.batch_queue.append(resolved)
                self.batch_queue_status[key] = "Queued"
                existing.add(key)
        self._refresh_batch_queue_view()

    def _refresh_batch_queue_view(self) -> None:
        tree = self.batch_queue_tree
        if tree is None or not tree.winfo_exists():
            return
        tree.delete(*tree.get_children())
        for path in self.batch_queue:
            key = str(path.resolve()).lower() if path.exists() else str(path).lower()
            tree.insert("", "end", iid=str(path), values=(path.name, str(path), self.batch_queue_status.get(key, "Queued")))

    def batch_convert(self) -> None:
        self.open_batch_queue()

    def open_batch_queue(self) -> None:
        if self.batch_queue_window is not None and self.batch_queue_window.winfo_exists():
            self.batch_queue_window.deiconify()
            self.batch_queue_window.lift()
            return
        window = tk.Toplevel(self)
        window.title("Batch Queue")
        window.transient(self)
        window.geometry("980x410")
        window.minsize(760, 300)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        self.batch_queue_window = window

        columns = ("name", "path", "status")
        tree = ttk.Treeview(window, columns=columns, show="headings", selectmode="extended")
        tree.heading("name", text="File")
        tree.heading("path", text="Source path")
        tree.heading("status", text="Status")
        tree.column("name", width=190, anchor="w")
        tree.column("path", width=480, anchor="w")
        tree.column("status", width=100, anchor="center")
        vertical = ttk.Scrollbar(window, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vertical.set)
        tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(10, 6))
        vertical.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=(10, 6))
        self.batch_queue_tree = tree

        buttons = ttk.Frame(window)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        ttk.Button(buttons, text="Add Files…", command=self._add_batch_files).pack(side="left")
        ttk.Button(buttons, text="Remove Selected", command=self._remove_selected_batch_files).pack(side="left", padx=6)
        ttk.Button(buttons, text="Move Up", command=lambda: self._move_selected_batch_items(-1)).pack(side="left")
        ttk.Button(buttons, text="Move Down", command=lambda: self._move_selected_batch_items(1)).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Clear Queue", command=self._clear_batch_queue).pack(side="left", padx=6)
        ttk.Label(buttons, text="Queue converts the original source files.", style="Muted.TLabel").pack(side="left", padx=14)
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")
        ttk.Button(buttons, text="Start Batch", command=self._start_batch_from_queue, style="Primary.TButton").pack(side="right", padx=(0, 6))
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        self._refresh_batch_queue_view()

    def _add_batch_files(self) -> None:
        files = filedialog.askopenfilenames(parent=self.batch_queue_window, title="Add Documents to Batch Queue", filetypes=OPEN_FILE_TYPES)
        if files:
            self._add_to_batch_queue([Path(item) for item in files])

    def _remove_selected_batch_files(self) -> None:
        tree = self.batch_queue_tree
        if tree is None:
            return
        selected = {item.lower() for item in tree.selection()}
        if not selected:
            return
        self.batch_queue = [path for path in self.batch_queue if str(path).lower() not in selected]
        self._refresh_batch_queue_view()

    def _move_selected_batch_items(self, direction: int) -> None:
        tree = self.batch_queue_tree
        if tree is None:
            return
        selected = {item.lower() for item in tree.selection()}
        if not selected or len(self.batch_queue) < 2:
            return
        if direction < 0:
            indices = range(1, len(self.batch_queue))
            neighbor_offset = -1
        else:
            indices = range(len(self.batch_queue) - 2, -1, -1)
            neighbor_offset = 1
        for index in indices:
            current_key = str(self.batch_queue[index]).lower()
            neighbor_key = str(self.batch_queue[index + neighbor_offset]).lower()
            if current_key in selected and neighbor_key not in selected:
                other = index + neighbor_offset
                self.batch_queue[index], self.batch_queue[other] = self.batch_queue[other], self.batch_queue[index]
        self._refresh_batch_queue_view()
        for path in self.batch_queue:
            if str(path).lower() in selected and tree.exists(str(path)):
                tree.selection_add(str(path))

    def _clear_batch_queue(self) -> None:
        if self.batch_queue and not messagebox.askyesno("Clear Batch Queue", "Remove all queued files?", parent=self.batch_queue_window):
            return
        self.batch_queue.clear()
        self.batch_queue_status.clear()
        self._refresh_batch_queue_view()

    def _plan_batch_outputs(self, files: list[Path], output_dir: Path, extension: str) -> tuple[list[tuple[Path, Path]], list[str]]:
        reserved: set[str] = set()
        planned: list[tuple[Path, Path]] = []
        conflicts: list[str] = []
        for source in files:
            candidate = output_dir / f"{source.stem}{extension}"
            original = candidate
            suffix = 2
            while candidate.exists() or str(candidate).lower() in reserved:
                candidate = output_dir / f"{source.stem} ({suffix}){extension}"
                suffix += 1
            if candidate != original:
                conflicts.append(f"{source.name} → {candidate.name}")
            reserved.add(str(candidate).lower())
            planned.append((source, candidate))
        return planned, conflicts

    def _start_batch_from_queue(self) -> None:
        if self.running:
            messagebox.showinfo("Speech Engine Busy", "A speech task is already running.")
            return
        files = [path for path in self.batch_queue if path.is_file()]
        if not files:
            messagebox.showinfo("Batch Queue", "Add one or more supported documents to the queue first.")
            return
        if not self._validate_synthesis_settings():
            return
        if not self.open_audio_settings("Batch Audio Settings"):
            return
        output_dir_text = filedialog.askdirectory(title="Select Output Folder")
        if not output_dir_text:
            return
        output_dir = Path(output_dir_text)
        format_key = str(self.output_format)
        extension = str(AUDIO_FORMATS[format_key]["extension"])
        items, conflicts = self._plan_batch_outputs(files, output_dir, extension)
        if self._engine_key() == "chatterbox_flash":
            reference_audio, _conditioning = self._chatterbox_profile_paths()
            reference_key = normalized_path_key(Path(reference_audio)) if reference_audio else ""
            if any(normalized_path_key(output) == reference_key for _source, output in items):
                messagebox.showerror(
                    "Batch Output Folder",
                    "A planned batch output would overwrite the active Chatterbox reference clip. "
                    "Choose another output folder.",
                    parent=self.batch_queue_window,
                )
                return
        if conflicts:
            preview = "\n".join(conflicts[:10])
            if len(conflicts) > 10:
                preview += f"\n… and {len(conflicts) - 10} more"
            if not messagebox.askyesno(
                "Output Name Conflicts",
                "Some output names already exist or repeat. The default suffix names will be used:\n\n"
                f"{preview}\n\nContinue?",
                parent=self.batch_queue_window,
                default=messagebox.YES,
            ):
                return
        voice_profile = self._current_voice_profile()
        audio_profile = self._current_audio_profile(format_key)
        self.running = True
        self.cancel_event.clear()
        self._set_busy(True)
        for source, _output in items:
            self.batch_queue_status[str(source).lower()] = "Queued"
        self._refresh_batch_queue_view()
        threading.Thread(
            target=self._batch_worker,
            args=(items, voice_profile, audio_profile),
            daemon=True,
        ).start()

    def _batch_worker(
        self,
        items: list[tuple[Path, Path]],
        voice_profile: dict[str, object],
        audio_profile: dict[str, object],
    ) -> None:
        completed: list[Path] = []
        failures: list[str] = []
        cancelled = False
        try:
            if str(voice_profile.get("engine", "kokoro")) == "edge" and len(items) > 1:
                completed, failures, cancelled = self._run_parallel_edge_batch(
                    items, voice_profile, audio_profile,
                )
            else:
                for index, (path, output) in enumerate(items, start=1):
                    if self.cancel_event.is_set():
                        cancelled = True
                        break
                    self._set_status(f"Batch {index}/{len(items)}: {path.name}", (index - 1) * 100 / len(items))
                    try:
                        self._convert_batch_item(path, output, voice_profile, audio_profile)
                        completed.append(path)
                        self.after(0, lambda path=path: self._set_batch_queue_status(path, "Done"))
                    except InterruptedError:
                        cancelled = True
                        break
                    except Exception as exc:
                        failures.append(f"{path.name}: {exc}")
                        self.after(0, lambda path=path: self._set_batch_queue_status(path, "Failed"))
            self._set_status(f"Batch finished: {len(completed)} of {len(items)} files", 100)
            message = f"Converted {len(completed)} of {len(items)} files."
            if cancelled:
                message += "\n\nBatch was stopped; remaining files stay queued."
            if failures:
                message += "\n\nFailed:\n" + "\n".join(failures[:8])
            self.after(0, lambda: self._finish_batch_queue_run(items, completed, failures, cancelled, message))
        finally:
            self.running = False
            self.after(0, lambda: self._set_busy(False))

    def _convert_batch_item(
        self,
        path: Path,
        output: Path,
        voice_profile: dict[str, object],
        audio_profile: dict[str, object],
    ) -> None:
        if self.cancel_event.is_set():
            raise InterruptedError("Speech task cancelled")
        self.after(0, lambda path=path: self._set_batch_queue_status(path, "Converting…"))
        text = self._prepare_for_speech(read_document(path))
        self._synthesize_to_file(text, output, voice_profile, audio_profile)

    def _run_parallel_edge_batch(
        self,
        items: list[tuple[Path, Path]],
        voice_profile: dict[str, object],
        audio_profile: dict[str, object],
    ) -> tuple[list[Path], list[str], bool]:
        completed: list[Path] = []
        failures: list[str] = []
        cancelled = False
        worker_count = min(2, len(items))
        self._set_status(f"Edge batch: converting {worker_count} files at a time", 0)
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="edge-batch") as executor:
            future_paths = {
                executor.submit(self._convert_batch_item, path, output, voice_profile, audio_profile): path
                for path, output in items
            }
            for finished, future in enumerate(as_completed(future_paths), start=1):
                path = future_paths[future]
                try:
                    future.result()
                    completed.append(path)
                    self.after(0, lambda path=path: self._set_batch_queue_status(path, "Done"))
                except InterruptedError:
                    cancelled = True
                    self.after(0, lambda path=path: self._set_batch_queue_status(path, "Queued"))
                except Exception as exc:
                    failures.append(f"{path.name}: {exc}")
                    self.after(0, lambda path=path: self._set_batch_queue_status(path, "Failed"))
                self._set_status(
                    f"Edge batch: {finished}/{len(items)} files processed",
                    finished * 100 / len(items),
                )
                if self.cancel_event.is_set():
                    cancelled = True
        return completed, failures, cancelled

    def _set_batch_queue_status(self, path: Path, status: str) -> None:
        self.batch_queue_status[str(path).lower()] = status
        self._refresh_batch_queue_view()

    def _finish_batch_queue_run(
        self,
        items: list[tuple[Path, Path]],
        completed: list[Path],
        failures: list[str],
        cancelled: bool,
        message: str,
    ) -> None:
        if len(completed) == len(items) and not failures and not cancelled:
            self.batch_queue.clear()
            self.batch_queue_status.clear()
        else:
            completed_keys = {str(path).lower() for path in completed}
            # Completed files have already been written. Leave only files that
            # still need attention so Stop is a safe, useful retry action.
            self.batch_queue = [
                path for path in self.batch_queue if str(path).lower() not in completed_keys
            ]
            for key in completed_keys:
                self.batch_queue_status.pop(key, None)
            for path, _output in items:
                if str(path).lower() not in completed_keys and self.batch_queue_status.get(str(path).lower()) != "Failed":
                    self.batch_queue_status[str(path).lower()] = "Queued"
        self._refresh_batch_queue_view()
        messagebox.showinfo("Batch Conversion", message)
