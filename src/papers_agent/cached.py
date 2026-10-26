"""Generic caching wrapper around any PaperSource implementation."""

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from papers_agent.cache import SourceCache
from papers_agent.citation_key import unique_image_base, unique_pdf_path
from papers_agent.pdf import TempPdf, extract_pdf_images, extract_pdf_text
from papers_agent.types import (
    Doi,
    LocalPath,
    NonEmptyStr,
    Paper,
    PaperSource,
    PaperSourceName,
    Url,
    UTCDatetime,
)


class CachedSource(PaperSource):
    """Transparent cache in front of another PaperSource.

    Successful results of every public operation are stored in a shared
    sqlite cache keyed by the source name, so repeated queries and
    repeated requests for a specific paper are answered without hitting
    the underlying source again. Failed lookups are never cached. All
    cache access degrades gracefully to the wrapped source.
    """

    def __init__(self, inner: PaperSource, cache: SourceCache | None = None) -> None:
        self._inner = inner
        self._cache = cache if cache is not None else SourceCache()

    @property
    def source(self) -> PaperSourceName:
        return self._inner.source

    @property
    def inner(self) -> PaperSource:
        return self._inner

    def handles_url(self, url: Url) -> bool:
        return self._inner.handles_url(url)

    def handles_doi(self, doi: Doi) -> bool:
        return self._inner.handles_doi(doi)

    async def search(self, keyword: NonEmptyStr, limit: int) -> list[Paper]:
        return await self._cached_query(
            "search", {"keyword": keyword, "limit": limit},
            lambda: self._inner.search(keyword, limit),
        )

    async def semantic_search(self, query: NonEmptyStr, limit: int) -> list[Paper]:
        return await self._cached_query(
            "semantic_search", {"query": query, "limit": limit},
            lambda: self._inner.semantic_search(query, limit),
        )

    async def get_papers_in_date_range(
        self, start: UTCDatetime, end: UTCDatetime, limit: int
    ) -> list[Paper]:
        return await self._cached_query(
            "date_range",
            {"start": start.isoformat(), "end": end.isoformat(), "limit": limit},
            lambda: self._inner.get_papers_in_date_range(start, end, limit),
        )

    async def get_paper_by_url(self, url: Url) -> Paper:
        paper = await self._cache.get_paper_by_url(self.source, url)
        if paper is not None:
            return paper
        paper = await self._inner.get_paper_by_url(url)
        await self._cache.put_paper(self.source, paper)
        return paper

    async def get_paper_by_doi(self, doi: Doi) -> Paper:
        paper = await self._cache.get_paper_by_doi(self.source, doi)
        if paper is not None:
            return paper
        paper = await self._inner.get_paper_by_doi(doi)
        await self._cache.put_paper(self.source, paper)
        return paper

    async def get_references(self, doi: Doi) -> list[str]:
        text = await self._cache.get_text(self.source, "references", doi)
        if text is not None:
            return text.split("\n")
        references = await self._inner.get_references(doi)
        await self._cache.put_text(
            self.source, "references", doi, "\n".join(references)
        )
        return references

    async def download_pdf(self, doi: Doi, directory: LocalPath) -> Path:
        paper = await self.get_paper_by_doi(doi)
        path = unique_pdf_path(Path(directory), paper.citation_key)
        blob = await self._pdf_blob(doi)
        await asyncio.to_thread(path.write_bytes, blob)
        return path

    async def get_pdf_text(self, doi: Doi) -> str:
        text = await self._cache.get_text(self.source, "pdf-text", doi)
        if text is not None:
            return text
        blob = await self._pdf_blob(doi)
        with TempPdf() as tmp_path:
            await asyncio.to_thread(tmp_path.write_bytes, blob)
            text = await asyncio.to_thread(extract_pdf_text, tmp_path)
        await self._cache.put_text(self.source, "pdf-text", doi, text)
        return text

    async def get_pdf_images(self, doi: Doi, directory: LocalPath) -> list[Path]:
        images_dir = Path(directory)
        paper = await self.get_paper_by_doi(doi)
        base = unique_image_base(images_dir, paper.citation_key)
        blob = await self._pdf_blob(doi)
        with TempPdf() as tmp_path:
            await asyncio.to_thread(tmp_path.write_bytes, blob)
            return await asyncio.to_thread(
                extract_pdf_images, tmp_path, images_dir, base
            )

    async def _pdf_blob(self, doi: Doi) -> bytes:
        blob = await self._cache.get_pdf(self.source, doi)
        if blob is not None:
            return blob
        with tempfile.TemporaryDirectory(prefix="papers-agent-") as tmp_dir:
            path = await self._inner.download_pdf(doi, Path(tmp_dir))
            blob = await asyncio.to_thread(path.read_bytes)
        await self._cache.put_pdf(self.source, doi, blob)
        return blob

    async def _cached_query(
        self,
        scope: str,
        params: dict[str, str],
        fetch: Callable[[], Awaitable[list[Paper]]],
    ) -> list[Paper]:
        cache_key = self._cache.query_key(self.source, scope, params)
        paper_urls = await self._cache.get_query_result(cache_key)
        if paper_urls is not None:
            papers = await self._cache.get_papers_by_urls(self.source, paper_urls)
            if len(papers) == len(paper_urls):
                return [papers[url] for url in paper_urls]
        papers = await fetch()
        for paper in papers:
            await self._cache.put_paper(self.source, paper)
        await self._cache.put_query_result(cache_key, [paper.url for paper in papers])
        return papers
