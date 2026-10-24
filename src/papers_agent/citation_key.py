import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from papers_agent.errors import PaperSourceError

_FILLER_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "by",
        "from",
        "as",
        "is",
        "are",
        "be",
        "been",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "de",
        "du",
        "la",
        "le",
        "el",
        "los",
        "las",
    }
)


def _first_author_last_name(authors: list[str]) -> str:
    if not authors:
        return ""
    parts = re.split(r"\s+", authors[0].strip())
    return parts[-1] if parts else ""


def _camel_case_word(word: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", word)
    if not cleaned:
        return ""
    return cleaned[0].upper() + cleaned[1:].lower()


def _short_title(title: str) -> str:
    picked: list[str] = []
    for word in re.split(r"\s+", title.strip()):
        if not word:
            continue
        if word.lower() in _FILLER_WORDS:
            continue
        camel = _camel_case_word(word)
        if camel:
            picked.append(camel)
        if len(picked) == 3:
            break
    return "".join(picked)


def citation_key(name: str, authors: list[str], published_at: datetime | None) -> str:
    """Derive the authorShortTitleYear citation key of a paper from its fields."""
    auth = re.sub(r"[^A-Za-z0-9]", "", _first_author_last_name(authors)).lower()
    if not auth:
        auth = "anon"
    short = _short_title(name)
    if not short:
        short = "Ref"
    yr = str(published_at.year) if published_at else ""
    return f"{auth}{short}{yr}"


def _disambiguation_suffixes() -> Iterator[str]:
    yield ""
    letters = "abcdefghijklmnopqrstuvwxyz"
    yield from letters
    for first in letters:
        for second in letters:
            yield first + second


def unique_pdf_path(directory: Path, base: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    taken = {path.stem for path in directory.glob("*.pdf")}
    for suffix in _disambiguation_suffixes():
        candidate = base + suffix
        if candidate not in taken:
            return directory / f"{candidate}.pdf"
    msg = f"exhausted citation key space for base {base!r}"
    raise PaperSourceError(msg)


def unique_image_base(directory: Path, base: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    for suffix in _disambiguation_suffixes():
        candidate = base + suffix
        if not any(directory.glob(f"{candidate}_image_*")):
            return candidate
    msg = f"exhausted citation key space for base {base!r}"
    raise PaperSourceError(msg)
