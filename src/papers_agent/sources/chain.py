
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from papers_agent.errors import PaperNotFoundError, PaperSourceError
from papers_agent.sources.arxiv_paper import (
    ARXIV_DOI_PREFIX,
    arxiv_id_from_doi,
    arxiv_id_from_url,
    base_arxiv_id,
)
from papers_agent.types import (
    Doi,
    LocalPath,
    NonEmptyStr,
    Paper,
    PaperSource,
    Url,
    UTCDatetime,
)


_EPOCH = datetime.min.replace(tzinfo=UTC)


class ChainedSource(PaperSource):
    source = "arxiv"

    def __init__(self, sources: list[PaperSource]) -> None:
        self._sources = sources
        self._prepared = False

    def _prepare(self) -> None:
        if self._prepared:
            return
        self._prepared = True
        for source in self._sources:
            source.prepare()

    async def search(self, keyword: NonEmptyStr, limit: int) -> list[Paper]:
        self._prepare()
        candidates: list[Paper] = []
        for source in self._sources:
            papers = await source.search(keyword, limit)
            if papers is None:
                continue
            candidates.extend(papers)
        seen: set[str] = set()
        unique: list[Paper] = []
        for paper in candidates:
            if paper.url not in seen:
                seen.add(paper.url)
                unique.append(paper)
        unique.sort(
            key=lambda paper: paper.published_at or _EPOCH, reverse=True
        )
        return unique[:limit]

    async def semantic_search_ids(self, query: NonEmptyStr, limit: int) -> list[str]:
        self._prepare()
        for source in self._sources:
            try:
                ids = (await source.semantic_search_ids(query, limit))[:limit]
            except PaperSourceError:
                continue
            if ids:
                return ids
        return []

    async def papers_by_ids(self, arxiv_ids: list[str]) -> dict[str, Paper]:
        self._prepare()
        papers: dict[str, Paper] = {}
        missing = list(arxiv_ids)
        for source in self._sources:
            if not missing:
                break
            try:
                found = await source.papers_by_ids(missing)
            except PaperSourceError:
                continue
            papers.update(found)
            missing = [arxiv_id for arxiv_id in missing if arxiv_id not in found]
        return papers

    async def get_papers_in_date_range(
        self, start: UTCDatetime, end: UTCDatetime, limit: int
    ) -> list[Paper]:
        self._prepare()
        for source in self._sources:
            papers = await source.get_papers_in_date_range(start, end, limit)
            if papers is not None:
                return papers
        return []

    async def get_paper_by_url(self, url: Url) -> Paper:
        self._prepare()
        for source in self._sources:
            try:
                paper = await source.get_paper_by_url(url)
            except PaperNotFoundError:
                continue
            if paper is not None:
                return paper
        raise PaperNotFoundError(f"no paper for url {url}")

    async def get_paper_by_doi(self, doi: Doi) -> Paper:
        self._prepare()
        for source in self._sources:
            try:
                paper = await source.get_paper_by_doi(doi)
            except PaperNotFoundError:
                continue
            if paper is not None:
                return paper
        raise PaperNotFoundError(f"no paper for doi {doi}")

    async def get_references(self, doi: Doi) -> list[str]:
        return await self._first_supported(
            "get_references", doi,
            lambda source: source.get_references(doi),
        )

    async def download_pdf(self, doi: Doi, directory: LocalPath) -> Path:
        return await self._first_supported(
            "download_pdf", doi,
            lambda source: source.download_pdf(doi, directory),
        )

    async def get_pdf_text(self, doi: Doi) -> str:
        return await self._first_supported(
            "get_pdf_text", doi,
            lambda source: source.get_pdf_text(doi),
        )

    async def get_pdf_images(
        self, doi: Doi, directory: LocalPath
    ) -> list[Path]:
        return await self._first_supported(
            "get_pdf_images", doi,
            lambda source: source.get_pdf_images(doi, directory),
        )

    async def _first_supported[S](
        self,
        capability: str,
        doi: Doi,
        call: Callable[[PaperSource], Awaitable[S]],
    ) -> S:
        self._prepare()
        last_error: PaperSourceError | None = None
        for source in self._sources:
            try:
                return await call(source)
            except PaperSourceError as exc:
                last_error = exc
        raise last_error or PaperSourceError(
            f"no source in the chain supports {capability}"
        )

    @classmethod
    def handles_url(cls, url: Url) -> bool:
        return arxiv_id_from_url(url) is not None

    @classmethod
    def handles_doi(cls, doi: Doi) -> bool:
        return arxiv_id_from_doi(doi) is not None

    @classmethod
    def doi_for_url(cls, url: Url) -> Doi:
        arxiv_id = arxiv_id_from_url(url)
        if arxiv_id is None:
            raise PaperNotFoundError(f"no arxiv paper for url {url}")
        return f"{ARXIV_DOI_PREFIX}{base_arxiv_id(arxiv_id)}"
