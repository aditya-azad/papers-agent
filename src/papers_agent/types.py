import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

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


_EPOCH = datetime.min.replace(tzinfo=UTC)


class PaperSource:
    source: PaperSourceName
    doi_prefix: str | None = None

    def __init__(
        self, source: PaperSourceName, sources: Sequence[PaperSource] = ()
    ) -> None:
        self.source = source
        self._sources: tuple[PaperSource, ...] = tuple(sources)
        self._prepared = False

    def prepare(self) -> None:
        if self._prepared:
            return
        self._prepared = True
        for source in self._sources:
            source.prepare()

    def handles_url(self, url: Url) -> bool:
        if self._sources:
            return any(source.handles_url(url) for source in self._sources)
        try:
            self.doi_for_url(url)
        except PaperNotFoundError:
            return False
        return True

    def handles_doi(self, doi: Doi) -> bool:
        if self._sources:
            return any(source.handles_doi(doi) for source in self._sources)
        return self.doi_prefix is not None and doi.startswith(self.doi_prefix)

    def doi_for_url(self, url: Url) -> Doi:
        for source in self._sources:
            try:
                return source.doi_for_url(url)
            except PaperNotFoundError:
                continue
        raise PaperNotFoundError(f"no paper for url {url}")

    async def search(self, keyword: NonEmptyStr, limit: int) -> list[Paper] | None:
        results, _ = await self._query_all(lambda source: source.search(keyword, limit))
        papers = [
            paper for batch in results if batch is not None for paper in batch
        ]
        return self._unique_papers(papers, limit)

    async def semantic_search_ids(self, query: NonEmptyStr, limit: int) -> list[str]:
        if not self._sources:
            raise self._unsupported("semantic search")
        results, _ = await self._query_all(
            lambda source: source.semantic_search_ids(query, limit)
        )
        seen: set[str] = set()
        ids: list[str] = []
        for batch in results:
            for arxiv_id in batch:
                if arxiv_id not in seen:
                    seen.add(arxiv_id)
                    ids.append(arxiv_id)
        return ids[:limit]

    async def semantic_search(self, query: NonEmptyStr, limit: int) -> list[Paper]:
        ids = (await self.semantic_search_ids(query, limit))[:limit]
        if not ids:
            return []
        papers = await self.papers_by_ids(ids)
        return [paper for arxiv_id in ids if (paper := papers.get(arxiv_id))]

    async def papers_by_ids(self, arxiv_ids: list[str]) -> dict[str, Paper]:
        if not self._sources:
            raise self._unsupported("batch hydration")
        results, _ = await self._query_all(
            lambda source: source.papers_by_ids(arxiv_ids)
        )
        papers: dict[str, Paper] = {}
        for found in results:
            papers.update(found)
        return papers

    async def get_papers_in_date_range(
        self, start: UTCDatetime, end: UTCDatetime, limit: int
    ) -> list[Paper] | None:
        results, _ = await self._query_all(
            lambda source: source.get_papers_in_date_range(start, end, limit)
        )
        papers = [
            paper for batch in results if batch is not None for paper in batch
        ]
        return self._unique_papers(papers, limit)

    async def get_paper_by_url(self, url: Url) -> Paper | None:
        paper = await self._first_paper(lambda source: source.get_paper_by_url(url))
        if paper is None:
            raise PaperNotFoundError(f"no paper for url {url}")
        return paper

    async def get_paper_by_doi(self, doi: Doi) -> Paper | None:
        paper = await self._first_paper(lambda source: source.get_paper_by_doi(doi))
        if paper is None:
            raise PaperNotFoundError(f"no paper for doi {doi}")
        return paper

    async def _first_paper(
        self, call: Callable[[PaperSource], Awaitable[Paper | None]]
    ) -> Paper | None:
        results, _ = await self._query_all(call)
        return next((paper for paper in results if paper is not None), None)

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
        return await self._first_supported(
            "reference extraction", lambda source: source.get_references(doi)
        )

    async def get_references_by_url(self, url: Url) -> list[str]:
        return await self.get_references(self.doi_for_url(url))

    async def download_pdf(self, doi: Doi, directory: LocalPath) -> Path:
        return await self._first_supported(
            "pdf download",
            lambda source: source.download_pdf(doi, directory),
        )

    async def get_pdf_text(self, doi: Doi) -> str:
        return await self._first_supported(
            "pdf text extraction", lambda source: source.get_pdf_text(doi)
        )

    async def get_pdf_text_by_url(self, url: Url) -> str:
        return await self.get_pdf_text(self.doi_for_url(url))

    async def get_pdf_images(self, doi: Doi, directory: LocalPath) -> list[Path]:
        return await self._first_supported(
            "pdf image extraction",
            lambda source: source.get_pdf_images(doi, directory),
        )

    async def get_pdf_images_by_url(self, url: Url, directory: LocalPath) -> list[Path]:
        return await self.get_pdf_images(self.doi_for_url(url), directory)

    async def _first_supported[S](
        self,
        capability: str,
        call: Callable[[PaperSource], Awaitable[S]],
    ) -> S:
        values, errors = await self._query_all(call)
        if values:
            return values[0]
        if errors:
            raise errors[-1]
        raise self._unsupported(capability)

    async def _query_all[S](
        self, call: Callable[[PaperSource], Awaitable[S]]
    ) -> tuple[list[S], list[BaseException]]:
        self.prepare()
        outcomes = await asyncio.gather(
            *(call(source) for source in self._sources),
            return_exceptions=True,
        )
        values = [value for value in outcomes if not isinstance(value, BaseException)]
        errors = [value for value in outcomes if isinstance(value, BaseException)]
        return values, errors

    def _unique_papers(self, papers: list[Paper], limit: int) -> list[Paper]:
        seen: set[str] = set()
        unique: list[Paper] = []
        for paper in papers:
            if paper.url not in seen:
                seen.add(paper.url)
                unique.append(paper)
        unique.sort(key=lambda paper: paper.published_at or _EPOCH, reverse=True)
        return unique[:limit]

    def _unsupported(self, capability: str) -> PaperSourceError:
        return PaperSourceError(
            f"{type(self).__name__} does not support {capability}"
        )
