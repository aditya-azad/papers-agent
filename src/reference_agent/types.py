import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Annotated, ClassVar, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    HttpUrl,
    StringConstraints,
)


def _ensure_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _fix_spacing(value: str) -> str:
    return " ".join(value.split()).strip()


def _require_non_empty(value: str) -> str:
    if not value:
        raise ValueError("value must not be empty")
    return value


def _validate_url(value: str) -> str:
    return str(HttpUrl(value)).rstrip("/")


ProperWhitespacedStr = Annotated[str, BeforeValidator(_fix_spacing)]
UTCDatetime = Annotated[datetime, BeforeValidator(_ensure_utc)]
NonEmptyStr = Annotated[ProperWhitespacedStr, AfterValidator(_require_non_empty)]
Url = Annotated[NonEmptyStr, AfterValidator(_validate_url)]

PaperSourceName = Literal["arxiv", "pubmed", "biorxiv", "openalex"]

MAX_PAPER_NAME_LENGTH = 255
MAX_PAPER_ABSTRACT_LENGTH = 4096

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u0080-\u009f]")


def _strip_control_characters(value: str) -> str:
    return _CONTROL_CHARACTERS.sub("", value)


def _normalize_doi(value: str) -> str:
    normalized = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized.removeprefix(prefix)
            break
    return normalized


PaperAuthorName = Annotated[
    NonEmptyStr,
    BeforeValidator(_strip_control_characters),
    StringConstraints(max_length=MAX_PAPER_NAME_LENGTH),
]

PaperPublisher = Annotated[
    str,
    BeforeValidator(_strip_control_characters),
    StringConstraints(max_length=MAX_PAPER_NAME_LENGTH),
]

PaperAbstract = Annotated[
    str,
    BeforeValidator(_strip_control_characters),
    StringConstraints(max_length=MAX_PAPER_ABSTRACT_LENGTH),
]


def _dedupe_authors(
    value: list[PaperAuthorName],
) -> list[PaperAuthorName]:
    seen: set[str] = set()
    deduplicated: list[PaperAuthorName] = []
    for author in value:
        if author not in seen:
            seen.add(author)
            deduplicated.append(author)
    return deduplicated


PaperAuthors = Annotated[list[PaperAuthorName], BeforeValidator(_dedupe_authors)]

Doi = Annotated[
    NonEmptyStr,
    BeforeValidator(_normalize_doi),
    Field(pattern=r"^10\.\d{4,9}/\S+$"),
]


class Paper(BaseModel):
    source: PaperSourceName
    name: str
    url: Url
    doi: Doi
    authors: PaperAuthors
    publisher: PaperPublisher
    abstract: PaperAbstract
    published_at: UTCDatetime | None


class PaperSource(ABC):
    source: ClassVar[PaperSourceName]

    @classmethod
    @abstractmethod
    def handles_url(cls, url: Url) -> bool:
        """Whether this source can resolve the given paper url."""

    @classmethod
    @abstractmethod
    def handles_doi(cls, doi: Doi) -> bool:
        """Whether this source can resolve the given doi."""

    @abstractmethod
    async def search(
        self, keyword: NonEmptyStr, limit: int
    ) -> list[Paper]: ...

    @abstractmethod
    async def get_papers_in_date_range(
        self, start: UTCDatetime, end: UTCDatetime, limit: int
    ) -> list[Paper]: ...

    @abstractmethod
    async def get_paper_by_url(self, url: Url) -> Paper: ...

    @abstractmethod
    async def get_paper_by_doi(self, doi: Doi) -> Paper: ...

    async def get_authors(self, doi: Doi) -> PaperAuthors:
        return (await self.get_paper_by_doi(doi)).authors

    async def get_abstract(self, doi: Doi) -> PaperAbstract:
        return (await self.get_paper_by_doi(doi)).abstract
