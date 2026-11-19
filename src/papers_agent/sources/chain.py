"""A chain of paper sources with all fallback policy in one place."""

from collections.abc import Awaitable, Callable
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


class ChainedSource(PaperSource):
    """Tries its sources in order; the first answer wins.

    Fallback policy lives here: local sources first (cheap, offline),
    live sources to fill gaps, deduplicated by url. A source returning
    None means "cannot answer" and the chain falls through to the next
    one; a source raising PaperSourceError for an unsupported capability
    (e.g. semantic search without an embedding provider) is skipped for
    that capability. Sources are prepared once before the first query.
    """

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
        """Keyword search, local first, live api to fill gaps."""
        self._prepare()
        results: list[Paper] = []
        seen: set[str] = set()
        for source in self._sources:
            papers = await source.search(keyword, limit)
            if papers is None:
                continue
            for paper in papers:
                if paper.url not in seen:
                    seen.add(paper.url)
                    results.append(paper)
            if len(results) >= limit:
                break
        return results[:limit]

    async def semantic_search(self, query: NonEmptyStr, limit: int) -> list[Paper]:
        """Semantic search from the first source providing ids, hydrated
        through the chain."""
        self._prepare()
        ids: list[str] = []
        for source in self._sources:
            try:
                ids = (await source.semantic_search_ids(query))[:limit]
            except PaperSourceError:
                continue
            if ids:
                break
        if not ids:
            return []
        papers = await self.papers_by_ids(ids)
        return [paper for arxiv_id in ids if (paper := papers.get(arxiv_id))]

    async def papers_by_ids(self, arxiv_ids: list[str]) -> dict[str, Paper]:
        """Hydrate ids through the chain, local first, live api for gaps."""
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
        """Date-range query, local first (its cutoff defines coverage),
        live api otherwise."""
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
        """Run a doi-based capability on the first source supporting it."""
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
        """Derive the paper's doi from its arxiv abstract or pdf page url."""
        arxiv_id = arxiv_id_from_url(url)
        if arxiv_id is None:
            raise PaperNotFoundError(f"no arxiv paper for url {url}")
        return f"{ARXIV_DOI_PREFIX}{base_arxiv_id(arxiv_id)}"
