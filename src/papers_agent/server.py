import argparse
import os
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server.context import HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from papers_agent.cache import SourceCache
from papers_agent.cached import CachedSource
from papers_agent.errors import PaperSourceError
from papers_agent.sources.arxiv import ArxivSource
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

Limit = Annotated[int, Field(ge=1, le=100)]

_cache = SourceCache()


async def _evict_stale_before_tool_call(
    ctx: ServerRequestContext[Any, Any],
    call_next: Callable[
        [ServerRequestContext[Any, Any]], Awaitable[HandlerResult]
    ],
) -> HandlerResult:
    """Evict cache entries untouched for 3+ days before every tool run."""
    if ctx.method == "tools/call":
        await _cache.evict_stale()
    return await call_next(ctx)


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
    middleware=[_evict_stale_before_tool_call],
)

_sources: dict[PaperSourceName, PaperSource] = {
    "arxiv": CachedSource(ArxivSource(), cache=_cache)
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
async def get_paper_metadata_by_url(
    url: Url,
) -> Paper:
    """Fetch the metadata of a single paper by its url; the source is inferred from the url."""
    return await _run(_source_for_url(url), lambda s: s.get_paper_by_url(url))


@mcp.tool()
async def get_paper_metadata_by_doi(
    doi: Doi,
) -> Paper:
    """Fetch the metadata of a single paper by its doi; the source is inferred from the doi."""
    return await _run(_source_for_doi(doi), lambda s: s.get_paper_by_doi(doi))


@mcp.tool()
async def get_paper_metadata_references_by_doi(
    doi: Doi,
) -> list[str]:
    """Fetch the bibliography of the paper with the given doi as raw reference strings."""
    return await _run(_source_for_doi(doi), lambda s: s.get_references(doi))


@mcp.tool()
async def get_paper_metadata_references_by_url(
    url: Url,
) -> list[str]:
    """Fetch the bibliography of the paper at the given url as raw reference strings; the doi is inferred from the url."""
    return await _run(_source_for_url(url), lambda s: s.get_references_by_url(url))


@mcp.tool()
async def get_paper_metadata_citation_key_by_url(
    url: Url,
) -> str:
    """Return the citation key (authorShortTitleYear) of the paper with the given url."""
    return await _run(_source_for_url(url), lambda s: s.citation_key_for_url(url))


@mcp.tool()
async def get_paper_metadata_citation_key_by_doi(
    doi: Doi,
) -> str:
    """Return the citation key (authorShortTitleYear) of the paper with the given doi."""
    return await _run(_source_for_doi(doi), lambda s: s.citation_key(doi))


@mcp.tool()
async def get_paper_pdf_text_by_url(
    url: Url,
) -> str:
    """Return the full text extracted from the pdf of the paper at the given url (e.g. an arxiv abstract or pdf page); the doi is inferred from the url."""
    return await _run(_source_for_url(url), lambda s: s.get_pdf_text_by_url(url))


@mcp.tool()
async def get_paper_pdf_text_by_doi(
    doi: Doi,
) -> str:
    """Return the full text extracted from the pdf of the paper with the given doi."""
    return await _run(_source_for_doi(doi), lambda s: s.get_pdf_text(doi))


@mcp.tool()
def get_sources() -> list[str]:
    """List the names of all registered paper sources."""
    return list(_sources)


@mcp.tool()
async def get_paper_pdf_images_by_doi(
    doi: Doi,
    directory: LocalPath,
) -> list[str]:
    """Extract the images of the given paper's pdf into the given directory and return their file paths; each image is named <citation key>_image_<number>."""
    paths = await _run(_source_for_doi(doi), lambda s: s.get_pdf_images(doi, directory))
    return [str(image_path) for image_path in paths]


@mcp.tool()
async def get_paper_pdf_images_by_url(
    url: Url,
    directory: LocalPath,
) -> list[str]:
    """Extract the images of the pdf of the paper at the given url into the given directory and return their file paths; each image is named <citation key>_image_<number>; the doi is inferred from the url."""
    paths = await _run(_source_for_url(url), lambda s: s.get_pdf_images_by_url(url, directory))
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
