"""Reference MCP server exposing scholarly paper source operations as tools.

Each tool dispatches to a registered paper source (e.g. arXiv). Tool
failures that are anticipated (unknown source, failed upstream request,
missing paper, invalid date range) are surfaced as tool errors so agents
can read the message and recover.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, ValidationError

from reference_agent.errors import PaperSourceError
from reference_agent.source import PaperSource
from reference_agent.sources.arxiv import ArxivSource
from reference_agent.sources.schemas import (
    Doi,
    Paper,
    PaperAuthors,
    PaperDateRangeQuery,
    PaperDescription,
    PaperSearchQuery,
    PaperSourceName,
)
from reference_agent.types import NonEmptyStr, Url, UTCDatetime

Limit = Annotated[int, Field(ge=1, le=100)]

mcp = MCPServer(
    "reference-agent",
    instructions=(
        "Search scholarly paper sources (arXiv) and fetch paper "
        "metadata. Use search_papers for free-text queries, "
        "get_papers_in_date_range for date-bounded listings, "
        "get_paper_by_url/get_paper_by_doi for exact lookups, and "
        "get_paper_authors/get_paper_description for per-field details."
    ),
)

_sources: dict[PaperSourceName, PaperSource] = {"arxiv": ArxivSource()}


def _source(name: PaperSourceName) -> PaperSource:
    try:
        return _sources[name]
    except KeyError:
        raise ToolError(f"paper source '{name}' is not registered") from None


async def _run[T](
    name: PaperSourceName,
    operation: Callable[[PaperSource], Awaitable[T]],
) -> T:
    try:
        return await operation(_source(name))
    except PaperSourceError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def search_papers(
    source: PaperSourceName,
    text: NonEmptyStr,
    limit: Limit,
) -> list[Paper]:
    """Search a paper source with a free-text query."""
    try:
        query = PaperSearchQuery(text=text, limit=limit)
    except ValidationError as exc:
        raise ToolError(str(exc)) from exc
    return await _run(source, lambda s: s.search(query))


@mcp.tool()
async def get_papers_in_date_range(
    source: PaperSourceName,
    start: UTCDatetime,
    end: UTCDatetime,
    limit: Limit,
) -> list[Paper]:
    """List papers from a paper source published within a date range."""
    try:
        query = PaperDateRangeQuery(start=start, end=end, limit=limit)
    except ValidationError as exc:
        raise ToolError(str(exc)) from exc
    return await _run(source, lambda s: s.get_papers_in_date_range(query))


@mcp.tool()
async def get_paper_by_url(
    source: PaperSourceName,
    url: Url,
) -> Paper:
    """Fetch a single paper from a paper source by its url."""
    return await _run(source, lambda s: s.get_paper_by_url(url))


@mcp.tool()
async def get_paper_by_doi(
    source: PaperSourceName,
    doi: Doi,
) -> Paper:
    """Fetch a single paper from a paper source by its doi."""
    return await _run(source, lambda s: s.get_paper_by_doi(doi))


@mcp.tool()
async def get_paper_authors(
    source: PaperSourceName,
    doi: Doi,
) -> PaperAuthors:
    """Fetch the authors of a paper by its doi."""
    return await _run(source, lambda s: s.get_authors(doi))


@mcp.tool()
async def get_paper_description(
    source: PaperSourceName,
    doi: Doi,
) -> PaperDescription:
    """Fetch the description (abstract) of a paper by its doi."""
    return await _run(source, lambda s: s.get_description(doi))


def main() -> None:
    mcp.run()  # stdio by default; matches `pi mcp add ... -- uv run ... reference-agent`


if __name__ == "__main__":
    main()