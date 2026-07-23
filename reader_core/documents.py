from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
from xml.etree import ElementTree
import zipfile

from .config import MAX_DOCUMENT_BYTES
from .text import normalize_text


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg"}:
            self.hidden_depth += 1
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)

    def text(self) -> str:
        return normalize_text(" ".join(self.parts))


def read_text_file(path: Path) -> str:
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError(f"Document is larger than the {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB safety limit.")
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_DOCUMENT_BYTES:
            raise ValueError("DOCX document content exceeds the safe extraction limit.")
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(namespace + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(namespace + "t"))
        if text.strip():
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def read_epub(path: Path) -> str:
    sections: list[str] = []
    with zipfile.ZipFile(path) as archive:
        entries = sorted(
            (info for info in archive.infolist()
             if info.filename.lower().endswith((".xhtml", ".html", ".htm"))),
            key=lambda info: info.filename,
        )
        if sum(info.file_size for info in entries) > MAX_DOCUMENT_BYTES:
            raise ValueError("EPUB text content exceeds the safe extraction limit.")
        for info in entries:
            parser = TextExtractor()
            parser.feed(archive.read(info).decode("utf-8", errors="replace"))
            text = parser.text()
            if text:
                sections.append(text)
    return "\n\n".join(sections)


def read_rtf(path: Path) -> str:
    text = read_text_file(path)
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    return normalize_text(text)


def read_subtitles(path: Path) -> str:
    lines = []
    for line in read_text_file(path).splitlines():
        value = line.strip()
        if not value or value.isdigit() or "-->" in value or value.upper() == "WEBVTT":
            continue
        value = re.sub(r"<[^>]+>", "", value)
        lines.append(value)
    return normalize_text(" ".join(lines))


def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return read_docx(path)
    if suffix == ".epub":
        return read_epub(path)
    if suffix == ".rtf":
        return read_rtf(path)
    if suffix in {".srt", ".vtt"}:
        return read_subtitles(path)
    return read_text_file(path)

