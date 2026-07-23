from __future__ import annotations

import math
import re
import unicodedata


def safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def safe_float(value: object, default: float) -> float:
    try:
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    except (TypeError, ValueError, OverflowError):
        return default



def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def prepare_speech_text(text: str) -> str:
    """Make ordinary document text easier for the phonemizer to say clearly."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = text.translate(str.maketrans({
        "“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "…": "...",
        "—": ", ", "–": " - ", "−": "-", "×": " by ",
    }))
    text = text.replace("&", " and ").replace("@", " at ")
    text = re.sub(r"(?<!\w)#\s*(\d+)", r"number \1", text)
    text = re.sub(r"\$(\d+(?:[.,]\d+)?)", r"\1 dollars", text)
    text = re.sub(r"(\d+)\s*%", r"\1 percent", text)
    text = re.sub(r"\b(?:e\.\s*g\.)", "for example", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:i\.\s*e\.)", "that is", text, flags=re.IGNORECASE)
    abbreviations = {
        "Mr.": "Mister", "Mrs.": "Missus", "Ms.": "Miss", "Dr.": "Doctor",
        "Prof.": "Professor", "Sr.": "Senior", "Jr.": "Junior", "vs.": "versus",
        "etc.": "et cetera",
    }
    for source, replacement in abbreviations.items():
        text = re.sub(rf"\b{re.escape(source)}", replacement, text, flags=re.IGNORECASE)
    return normalize_text(text)


def _split_speech_sentences(text: str) -> list[str]:
    """Split sentences without treating common abbreviations and initials as ends."""
    marker = "\uFFF0"
    protected = re.sub(
        r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc)\.",
        lambda match: match.group(0).replace(".", marker),
        text,
        flags=re.IGNORECASE,
    )
    protected = re.sub(
        r"\b(?:[A-Za-z]\.\s*){2,}",
        lambda match: match.group(0).replace(".", marker),
        protected,
    )
    protected = re.sub(
        r"\b[A-Z]\.(?=\s+[A-Z][a-z])",
        lambda match: match.group(0).replace(".", marker),
        protected,
    )
    return [value.replace(marker, ".") for value in re.split(r"(?<=[.!?\u3002\uFF01\uFF1F])\s+|\n+", protected)]


def _sentence_end_matches(text: str) -> list[re.Match[str]]:
    """Return real sentence ends, excluding abbreviation and initial periods."""
    matches: list[re.Match[str]] = []
    for match in re.finditer(r"[.!?…][\"'”’)]*\s+", text):
        if text[match.start()] != ".":
            matches.append(match)
            continue
        prefix = text[max(0, match.start() - 48):match.start() + 1]
        suffix = text[match.end():]
        common = re.search(r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc)\.$", prefix, flags=re.IGNORECASE)
        initials = re.search(r"\b(?:[A-Za-z]\.\s*){2,}$", prefix)
        single_initial = re.search(r"\b[A-Z]\.$", prefix) and re.match(r"[A-Z][a-z]", suffix)
        following_initials = re.match(r"[A-Z]\.\s*[A-Z]\.", suffix)
        if not (common or initials or single_initial or following_initials):
            matches.append(match)
    return matches


def split_text(text: str, limit: int = 800) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    sentences = _split_speech_sentences(text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            piece, sentence = sentence[:cut].strip(), sentence[cut:].strip()
            if current:
                chunks.append(current)
                current = ""
            if piece:
                chunks.append(piece)
        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def next_text_segment(
    text: str,
    start_offset: int,
    end_offset: int | None = None,
    limit: int = 700,
) -> tuple[str, int, int, int] | None:
    """Return one paragraph/sentence-sized segment and its source offsets."""
    end_offset = len(text) if end_offset is None else max(0, min(len(text), end_offset))
    cursor = max(0, min(start_offset, end_offset))
    while cursor < end_offset and text[cursor].isspace():
        cursor += 1
    if cursor >= end_offset:
        return None

    remaining = text[cursor:end_offset]
    paragraph_break = re.search(r"\n[ \t]*\n", remaining)
    paragraph_end = cursor + paragraph_break.start() if paragraph_break else end_offset
    next_paragraph = cursor + paragraph_break.end() if paragraph_break else end_offset

    if paragraph_end - cursor <= limit:
        segment_end = paragraph_end
        next_offset = next_paragraph
    else:
        hard_end = min(paragraph_end, cursor + limit)
        sample = text[cursor:hard_end]
        sentence_ends = _sentence_end_matches(sample)
        if sentence_ends:
            segment_end = cursor + sentence_ends[-1].end()
        else:
            whitespace = max(sample.rfind(" "), sample.rfind("\n"), sample.rfind("\t"))
            segment_end = cursor + whitespace if whitespace > max(40, limit // 3) else hard_end
        next_offset = segment_end

    while segment_end > cursor and text[segment_end - 1].isspace():
        segment_end -= 1
    segment = text[cursor:segment_end]
    if not segment:
        return next_text_segment(text, next_offset, end_offset, limit)
    return segment, cursor, segment_end, max(next_offset, segment_end)


def rate_to_speed(rate: float) -> float:
    return 2.0 ** (max(-10, min(10, rate)) / 10.0)

