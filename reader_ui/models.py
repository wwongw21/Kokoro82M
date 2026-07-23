from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import ttk


@dataclass
class DocumentState:
    tab: ttk.Frame
    text: tk.Text
    path: Path | None = None
    dirty: bool = False
    loading: bool = False
    error: str | None = None

