import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

from papers_agent.citation_key import citation_key as _compute_citation_key
from papers_agent.errors import PaperNotFoundError, PaperSourceError


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


def _expand_path(value: Path | str) -> Path:
    return Path(value).expanduser()


LocalPath = Annotated[Path, BeforeValidator(_expand_path)]


ProperWhitespacedStr = Annotated[str, BeforeValidator(_fix_spacing)]
UTCDatetime = Annotated[datetime, BeforeValidator(_ensure_utc)]
NonEmptyStr = Annotated[ProperWhitespacedStr, AfterValidator(_require_non_empty)]
Url = Annotated[NonEmptyStr, AfterValidator(_validate_url)]

PaperSourceName = Literal["arxiv"]

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
    citation_key: str = Field(
        default="",
        description=(
            "authorShortTitleYear citation key; computed from the paper's own"
            " fields when not explicitly provided"
        ),
    )

    @model_validator(mode="after")
    def _compute_citation_key(self) -> Paper:
        if not self.citation_key:
            self.citation_key = _compute_citation_key(
                self.name, self.authors, self.published_at
            )
        return self


class PaperSource(ABC):
    source: ClassVar[PaperSourceName]

    def prepare(self) -> None:
        pass

    @classmethod
    @abstractmethod
    def handles_url(cls, url: Url) -> bool:
        pass

    @classmethod
    @abstractmethod
    def handles_doi(cls, doi: Doi) -> bool:
        pass

    @classmethod
    @abstractmethod
    def doi_for_url(cls, url: Url) -> Doi:
        pass

    @abstractmethod
    async def search(self, keyword: NonEmptyStr, limit: int) -> list[Paper] | None:
        pass

    async def semantic_search_ids(self, query: NonEmptyStr, limit: int) -> list[str]:
        raise PaperSourceError(
            f"{type(self).__name__} does not support semantic search"
        )

    async def semantic_search(self, query: NonEmptyStr, limit: int) -> list[Paper]:
        ids = (await self.semantic_search_ids(query))[:limit]
        if not ids:
            return []
        papers = await self.papers_by_ids(ids)
        return [paper for arxiv_id in ids if (paper := papers.get(arxiv_id))]

    async def papers_by_ids(self, arxiv_ids: list[str]) -> dict[str, Paper]:
        raise PaperSourceError(
            f"{type(self).__name__} does not support batch hydration"
        )

    @abstractmethod
    async def get_papers_in_date_range(
        self, start: UTCDatetime, end: UTCDatetime, limit: int
    ) -> list[Paper] | None:
        pass

    @abstractmethod
    async def get_paper_by_url(self, url: Url) -> Paper | None:
        pass

    @abstractmethod
    async def get_paper_by_doi(self, doi: Doi) -> Paper | None:
        pass

    async def citation_key(self, doi: Doi) -> str:
        paper = await self.get_paper_by_doi(doi)
        if paper is None:
            raise PaperNotFoundError(f"no paper for doi {doi}")
        return paper.citation_key

    async def citation_key_for_url(self, url: Url) -> str:
        paper = await self.get_paper_by_url(url)
        if paper is None:
            raise PaperNotFoundError(f"no paper for url {url}")
        return paper.citation_key

    async def get_references(self, doi: Doi) -> list[str]:
        raise PaperSourceError(
            f"{type(self).__name__} does not support reference extraction"
        )

    async def get_references_by_url(self, url: Url) -> list[str]:
        return await self.get_references(self.doi_for_url(url))

    @abstractmethod
    async def download_pdf(self, doi: Doi, directory: LocalPath) -> Path:
        pass

    @abstractmethod
    async def get_pdf_text(self, doi: Doi) -> str:
        pass

    async def get_pdf_text_by_url(self, url: Url) -> str:
        return await self.get_pdf_text(self.doi_for_url(url))

    @abstractmethod
    async def get_pdf_images(self, doi: Doi, directory: LocalPath) -> list[Path]:
        pass

    async def get_pdf_images_by_url(self, url: Url, directory: LocalPath) -> list[Path]:
        return await self.get_pdf_images(self.doi_for_url(url), directory)
