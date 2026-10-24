"""Reference MCP server exposing scholarly paper source operations as tools.

Each tool dispatches to a registered paper source (e.g. arXiv). Lookup
tools that identify a single paper (by url or doi) infer the source from
the identifier itself; query tools take the source name explicitly. Tool
failures that are anticipated (unknown source, unresolvable identifier,
failed upstream request, missing paper, invalid date range) are surfaced
as tool errors so agents can read the message and recover.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from reference_agent.errors import PaperSourceError
from reference_agent.sources.arxiv import ArxivSource
from reference_agent.types import (
    Doi,
    LocalPath,
    NonEmptyStr,
    Paper,
    PaperAbstract,
    PaperAuthors,
    PaperSource,
    PaperSourceName,
    Url,
    UTCDatetime,
)

Limit = Annotated[int, Field(ge=1, le=100)]

mcp = MCPServer(
    "reference-agent",
    instructions=(
        "Search scholarly paper sources (arXiv) and fetch paper "
        "metadata. Use keyword_search for keyword-based queries and "
        "semantic_search for natural-language descriptions of the topic "
        "(arXiv semantic search is powered by alphaXiv embeddings), "
        "get_papers_in_date_range for date-bounded listings, "
        "get_paper_by_url/get_paper_by_doi for exact lookups (the "
        "source is inferred from the url/doi), "
        "get_paper_authors/get_paper_abstract/get_paper_references for "
        "per-field details, "
        "and download_paper_pdf/get_paper_pdf_text/get_paper_pdf_images "
        "for pdf retrieval by doi."
    ),
)

_sources: dict[PaperSourceName, PaperSource] = {"arxiv": ArxivSource()}


def _source(name: PaperSourceName) -> PaperSource:
    try:
        return _sources[name]
    except KeyError:
        raise ToolError(f"paper source '{name}' is not registered") from None


async def _run[T](
    source: PaperSource,
    operation: Callable[[PaperSource], Awaitable[T]],
) -> T:
    try:
        return await operation(source)
    except PaperSourceError as exc:
        raise ToolError(str(exc)) from exc


def _source_for_url(url: Url) -> PaperSource:
    for source in _sources.values():
        if source.handles_url(url):
            return source
    raise ToolError(f"no registered paper source can resolve url '{url}'")


def _source_for_doi(doi: Doi) -> PaperSource:
    for source in _sources.values():
        if source.handles_doi(doi):
            return source
    raise ToolError(f"no registered paper source can resolve doi '{doi}'")


@mcp.tool()
async def keyword_search(
    source: PaperSourceName,
    keyword: NonEmptyStr,
    limit: Limit,
) -> list[Paper]:
    """Search a paper source by keyword."""
    return await _run(_source(source), lambda s: s.search(keyword, limit))


@mcp.tool()
async def semantic_search(
    source: PaperSourceName,
    query: NonEmptyStr,
    limit: Limit,
) -> list[Paper]:
    """Search a paper source by a natural-language semantic description."""
    return await _run(_source(source), lambda s: s.semantic_search(query, limit))


@mcp.tool()
async def get_papers_in_date_range(
    source: PaperSourceName,
    start: UTCDatetime,
    end: UTCDatetime,
    limit: Limit,
) -> list[Paper]:
    """List papers from a paper source published within a date range."""
    if end <= start:
        raise ToolError("end must be after start")
    return await _run(
        _source(source), lambda s: s.get_papers_in_date_range(start, end, limit)
    )


@mcp.tool()
async def get_paper_by_url(
    url: Url,
) -> Paper:
    """Fetch a single paper by its url; the source is inferred from the url."""
    return await _run(_source_for_url(url), lambda s: s.get_paper_by_url(url))


@mcp.tool()
async def get_paper_by_doi(
    doi: Doi,
) -> Paper:
    """Fetch a single paper by its doi; the source is inferred from the doi."""
    return await _run(_source_for_doi(doi), lambda s: s.get_paper_by_doi(doi))


@mcp.tool()
async def get_paper_authors(
    doi: Doi,
) -> PaperAuthors:
    """Fetch the authors of a paper by its doi."""
    return await _run(_source_for_doi(doi), lambda s: s.get_authors(doi))


@mcp.tool()
async def get_paper_abstract(
    doi: Doi,
) -> PaperAbstract:
    """Fetch the abstract of a paper by its doi."""
    return await _run(_source_for_doi(doi), lambda s: s.get_abstract(doi))


@mcp.tool()
async def get_paper_references(
    doi: Doi,
) -> list[str]:
    """Fetch the bibliography of the paper with the given doi as raw reference strings."""
    return await _run(_source_for_doi(doi), lambda s: s.get_references(doi))


@mcp.tool()
async def download_paper_pdf(
    doi: Doi,
    path: LocalPath,
) -> str:
    """Download the pdf of the paper with the given doi to the given file path."""
    return str(await _run(_source_for_doi(doi), lambda s: s.download_pdf(doi, path)))


@mcp.tool()
async def get_paper_pdf_text(
    doi: Doi,
) -> str:
    """Return the full text extracted from the pdf of the paper with the given doi."""
    return await _run(_source_for_doi(doi), lambda s: s.get_pdf_text(doi))


@mcp.tool()
def get_sources() -> list[str]:
    """List the names of all registered paper sources."""
    return list(_sources)


@mcp.tool()
async def get_paper_pdf_images(
    doi: Doi,
    path: LocalPath,
) -> list[str]:
    """Extract the images of the given paper's pdf into the given directory and return their file paths."""
    paths = await _run(_source_for_doi(doi), lambda s: s.get_pdf_images(doi, path))
    return [str(image_path) for image_path in paths]


def main() -> None:
    mcp.run()  # stdio by default; matches `pi mcp add ... -- uv run ... reference-agent`


if __name__ == "__main__":
    main()
