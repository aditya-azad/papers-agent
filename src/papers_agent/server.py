import argparse
import os
from collections.abc import Awaitable, Callable
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from papers_agent.cached import CachedSource
from papers_agent.errors import PaperSourceError
from papers_agent.sources.arxiv import ArxivSource
from papers_agent.types import (
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
    "papers",
    instructions=(
        "Search and fetch scholarly paper metadata and pdfs from "
        "registered paper sources (currently arXiv). Prefer "
        "semantic_search for natural-language topic descriptions "
        "(arXiv uses alphaXiv embeddings); use keyword_search for "
        "keyword queries. Tools taking a url or doi infer the source "
        "from the identifier."
    ),
)

_sources: dict[PaperSourceName, PaperSource] = {
    "arxiv": CachedSource(ArxivSource())
}


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
async def get_paper_citation_key(
    url: Url | None = None,
    doi: Doi | None = None,
) -> str:
    """Return the citation key (authorShortTitleYear) of the paper with the given url or doi; provide exactly one."""
    if url is not None and doi is not None:
        raise ToolError("provide either url or doi, not both")
    if url is not None:
        return await _run(_source_for_url(url), lambda s: s.citation_key_for_url(url))
    if doi is not None:
        return await _run(_source_for_doi(doi), lambda s: s.citation_key(doi))
    raise ToolError("provide exactly one of url or doi")


@mcp.tool()
async def download_paper_pdf(
    doi: Doi,
    directory: LocalPath,
) -> str:
    """Download the pdf of the paper with the given doi into the given directory, named after the paper's citation key."""
    return str(
        await _run(_source_for_doi(doi), lambda s: s.download_pdf(doi, directory))
    )


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
    directory: LocalPath,
) -> list[str]:
    """Extract the images of the given paper's pdf into the given directory and return their file paths; each image is named <citation key>_image_<number>."""
    paths = await _run(_source_for_doi(doi), lambda s: s.get_pdf_images(doi, directory))
    return [str(image_path) for image_path in paths]


def main() -> None:
    parser = argparse.ArgumentParser(description="Papers MCP server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve over Streamable HTTP instead of stdio",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("PAPERS_AGENT_HOST", "127.0.0.1"),
        help="HTTP bind host (HTTP mode only; default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PAPERS_AGENT_PORT", "8000")),
        help="HTTP bind port (HTTP mode only; default 8000)",
    )
    args = parser.parse_args()
    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()  # stdio by default; matches `pi mcp add ... -- uv run ... papers`


if __name__ == "__main__":
    main()
