"""Arxiv domain helpers: id/url/doi parsing and Paper construction."""

import re
from datetime import datetime

from pydantic import ValidationError

from papers_agent.errors import PaperNotFoundError, PaperSourceError
from papers_agent.types import Doi, Paper, Url

ARXIV_DOI_PREFIX = "10.48550/arxiv."
PUBLISHER = "arxiv"

ARXIV_ID_PATTERN = re.compile(
    r"(?:[a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
_VERSION_SUFFIX_PATTERN = re.compile(r"v\d+$", re.IGNORECASE)
_URL_PATTERN = re.compile(
    rf"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/({ARXIV_ID_PATTERN.pattern})"
    r"(?:\.pdf)?/?(?:\?.*)?$",
    re.IGNORECASE,
)


def arxiv_id_from_url(url: Url) -> str | None:
    """The arxiv id in the given abstract or pdf page url, or None."""
    match = _URL_PATTERN.fullmatch(url)
    return match.group(1).lower() if match else None


def arxiv_id_from_doi(doi: Doi) -> str | None:
    """The arxiv id encoded in the given synthetic arxiv doi, or None."""
    normalized = doi.lower()
    if not normalized.startswith(ARXIV_DOI_PREFIX):
        return None
    arxiv_id = normalized.removeprefix(ARXIV_DOI_PREFIX)
    if ARXIV_ID_PATTERN.fullmatch(arxiv_id) is None:
        return None
    return arxiv_id


def arxiv_id_for_doi(doi: Doi) -> str:
    """The arxiv id for the given synthetic arxiv doi.

    Raises PaperNotFoundError when the doi is not an arxiv doi.
    """
    arxiv_id = arxiv_id_from_doi(doi)
    if arxiv_id is None:
        raise PaperNotFoundError(f"no arxiv paper for doi {doi}")
    return arxiv_id


def base_arxiv_id(arxiv_id: str) -> str:
    """The arxiv id without its version suffix, lowercased."""
    return _VERSION_SUFFIX_PATTERN.sub("", arxiv_id.lower())


def paper_from_arxiv_fields(
    *,
    base_id: str,
    name: str,
    doi: str | None,
    authors: list[str],
    abstract: str,
    published_at: datetime | str | None,
) -> Paper:
    """Build a Paper from raw arxiv metadata, deriving the synthetic doi.

    Raises PaperSourceError if the fields fail validation (e.g. an empty
    title or an over-long abstract).
    """
    try:
        return Paper(
            source="arxiv",
            name=name,
            url=f"https://arxiv.org/abs/{base_id}",
            doi=doi or f"{ARXIV_DOI_PREFIX}{base_id}",
            authors=authors,
            publisher=PUBLISHER,
            abstract=abstract,
            published_at=published_at,
        )
    except ValidationError as exc:
        raise PaperSourceError("invalid arxiv entry data") from exc
