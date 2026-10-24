import re
import secrets
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import httpx
from defusedxml import ElementTree as DefusedElementTree
from pydantic import ValidationError

from reference_agent.errors import (
    PaperNotFoundError,
    PaperSourceError,
)
from reference_agent.types import (
    Doi,
    Paper,
    PaperDateRangeQuery,
    PaperSearchQuery,
    PaperSource,
    Url,
)

_BASE_URL = "https://export.arxiv.org/api/query"
_OAI_BASE_URL = "https://oaipmh.arxiv.org/oai"
_REQUEST_TIMEOUT_SECONDS = 30.0
_ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
_ARXIV_NAMESPACE = "http://arxiv.org/schemas/atom"
_OAI_PMH_NAMESPACE = "http://www.openarchives.org/OAI/2.0/"
_OAI_ARXIV_NAMESPACE = "http://arxiv.org/OAI/arXiv/"
_OAI_METADATA_PREFIX = "arXiv"
_OAI_IDENTIFIER_PREFIX = "oai:arXiv.org:"
_OAI_NOT_FOUND_ERROR_CODE = "idDoesNotExist"
_ARXIV_DOI_PREFIX = "10.48550/arxiv."
_PUBLISHER = "arXiv"
_USER_AGENT = "papers-agent/0.1.0"
_ARXIV_DATE_FORMAT = "%Y%m%d%H%M"

_ARXIV_ID_PATTERN_TEXT = r"(?:[a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?"
_ARXIV_ID_PATTERN = re.compile(_ARXIV_ID_PATTERN_TEXT, re.IGNORECASE)
_VERSION_SUFFIX_PATTERN = re.compile(r"v\d+$", re.IGNORECASE)
_ABS_URL_PATTERN = re.compile(
    rf"^https?://(?:www\.)?arxiv\.org/abs/({_ARXIV_ID_PATTERN_TEXT})$",
    re.IGNORECASE,
)


def _random_email() -> str:
    return f"{secrets.token_hex(12)}@example.org"


class ArxivSource(PaperSource):
    source = "arxiv"

    async def search(
        self, query: PaperSearchQuery
    ) -> list[Paper]:
        data = await self._request(
            {
                "search_query": f"all:{query.keyword}",
                "max_results": str(query.limit),
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
        )
        return [self._paper_from_entry(entry) for entry in self._entries(data)]

    async def get_papers_in_date_range(
        self, query: PaperDateRangeQuery
    ) -> list[Paper]:
        data = await self._request(
            {
                "search_query": (
                    f"submittedDate:[{query.start.strftime(_ARXIV_DATE_FORMAT)}"
                    f" TO {query.end.strftime(_ARXIV_DATE_FORMAT)}]"
                ),
                "max_results": str(query.limit),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        return [self._paper_from_entry(entry) for entry in self._entries(data)]

    @classmethod
    def handles_url(cls, url: Url) -> bool:
        return _ABS_URL_PATTERN.fullmatch(url) is not None

    @classmethod
    def handles_doi(cls, doi: Doi) -> bool:
        normalized = doi.lower()
        if not normalized.startswith(_ARXIV_DOI_PREFIX):
            return False
        arxiv_id = normalized.removeprefix(_ARXIV_DOI_PREFIX)
        return _ARXIV_ID_PATTERN.fullmatch(arxiv_id) is not None

    async def get_paper_by_url(self, url: Url) -> Paper:
        match = _ABS_URL_PATTERN.fullmatch(url)
        if match is None:
            raise PaperNotFoundError(
                f"no arxiv paper for url {url}"
            )
        return await self._get_by_id(match.group(1).lower())

    async def get_paper_by_doi(self, doi: Doi) -> Paper:
        normalized = doi.lower()
        if not normalized.startswith(_ARXIV_DOI_PREFIX):
            raise PaperNotFoundError(
                f"no arxiv paper for doi {doi}"
            )
        arxiv_id = normalized.removeprefix(_ARXIV_DOI_PREFIX)
        if _ARXIV_ID_PATTERN.fullmatch(arxiv_id) is None:
            raise PaperNotFoundError(
                f"no arxiv paper for doi {doi}"
            )
        return await self._get_by_id(arxiv_id)

    async def _get_by_id(self, arxiv_id: str) -> Paper:
        base_id = _VERSION_SUFFIX_PATTERN.sub("", arxiv_id.lower())
        data = await self._oai_request(
            {
                "verb": "GetRecord",
                "identifier": f"{_OAI_IDENTIFIER_PREFIX}{base_id}",
                "metadataPrefix": _OAI_METADATA_PREFIX,
            }
        )
        metadata = self._oai_metadata(data, base_id)
        return self._paper_from_oai_metadata(metadata, base_id)

    async def _request(self, params: dict[str, str]) -> str:
        return await self._get(_BASE_URL, params)

    async def _oai_request(self, params: dict[str, str]) -> str:
        return await self._get(_OAI_BASE_URL, params)

    async def _get(self, url: str, params: dict[str, str]) -> str:
        headers = {"User-Agent": f"{_USER_AGENT} (mailto:{_random_email()})"}
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            try:
                response = await client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise PaperSourceError("arxiv request failed") from exc
        if response.status_code != 200:
            raise PaperSourceError(f"arxiv returned status {response.status_code}")
        return response.text

    def _oai_metadata(self, data: str, base_id: str) -> ET.Element:
        root = self._parse(data)
        error = root.find(f"{{{_OAI_PMH_NAMESPACE}}}error")
        if error is not None:
            code = error.get("code") or "unknown"
            if code == _OAI_NOT_FOUND_ERROR_CODE:
                raise PaperNotFoundError(
                    f"no arxiv paper for id {base_id}"
                )
            raise PaperSourceError(
                f"arxiv oai error {code}: {self._element_text(error)}"
            )
        path = (
            f"{{{_OAI_PMH_NAMESPACE}}}GetRecord/"
            f"{{{_OAI_PMH_NAMESPACE}}}record/"
            f"{{{_OAI_PMH_NAMESPACE}}}metadata/"
            f"{{{_OAI_ARXIV_NAMESPACE}}}arXiv"
        )
        metadata = root.find(path)
        if metadata is None:
            raise PaperSourceError(f"missing arxiv oai metadata for id {base_id}")
        return metadata

    def _parse(self, data: str) -> ET.Element:
        try:
            return DefusedElementTree.fromstring(data)
        except ET.ParseError as exc:
            raise PaperSourceError("invalid arxiv response") from exc

    def _entries(self, data: str) -> list[ET.Element]:
        return self._parse(data).findall(f"{{{_ATOM_NAMESPACE}}}entry")

    def _paper_from_oai_metadata(
        self, metadata: ET.Element, base_id: str
    ) -> Paper:
        record_id = self._required_text(
            metadata, f"{{{_OAI_ARXIV_NAMESPACE}}}id"
        ).lower()
        if record_id != base_id:
            raise PaperSourceError(
                f"arxiv oai returned id {record_id} for requested {base_id}"
            )
        doi = (
            self._optional_text(metadata, f"{{{_OAI_ARXIV_NAMESPACE}}}doi")
            or f"{_ARXIV_DOI_PREFIX}{base_id}"
        )
        created = self._optional_text(metadata, f"{{{_OAI_ARXIV_NAMESPACE}}}created")
        try:
            return Paper(
                source="arxiv",
                name=self._required_text(metadata, f"{{{_OAI_ARXIV_NAMESPACE}}}title"),
                url=f"https://arxiv.org/abs/{base_id}",
                doi=doi,
                authors=self._oai_authors(metadata),
                publisher=_PUBLISHER,
                abstract=self._required_text(
                    metadata, f"{{{_OAI_ARXIV_NAMESPACE}}}abstract"
                ),
                published_at=self._parse_oai_date(created),
            )
        except ValidationError as exc:
            raise PaperSourceError("invalid arxiv entry data") from exc

    def _oai_authors(self, metadata: ET.Element) -> list[str]:
        authors = []
        for author in metadata.findall(
            f"{{{_OAI_ARXIV_NAMESPACE}}}authors/{{{_OAI_ARXIV_NAMESPACE}}}author"
        ):
            keyname = self._required_text(author, f"{{{_OAI_ARXIV_NAMESPACE}}}keyname")
            forenames = self._optional_text(
                author, f"{{{_OAI_ARXIV_NAMESPACE}}}forenames"
            )
            suffix = self._optional_text(author, f"{{{_OAI_ARXIV_NAMESPACE}}}suffix")
            authors.append(
                " ".join(
                    part for part in (forenames, keyname, suffix) if part is not None
                )
            )
        return authors

    @staticmethod
    def _parse_oai_date(value: str | None) -> datetime | None:
        if value is None:
            return None
        return datetime.fromisoformat(value).replace(tzinfo=UTC)

    def _paper_from_entry(self, entry: ET.Element) -> Paper:
        entry_id = self._required_text(entry, f"{{{_ATOM_NAMESPACE}}}id")
        match = _ABS_URL_PATTERN.fullmatch(entry_id)
        if match is None:
            raise PaperSourceError(f"unexpected arxiv entry id {entry_id}")
        arxiv_id = match.group(1).lower()
        base_id = _VERSION_SUFFIX_PATTERN.sub("", arxiv_id)
        doi = self._optional_text(entry, f"{{{_ARXIV_NAMESPACE}}}doi") or (
            f"{_ARXIV_DOI_PREFIX}{base_id}"
        )
        try:
            return Paper(
                source="arxiv",
                name=self._required_text(entry, f"{{{_ATOM_NAMESPACE}}}title"),
                url=f"https://arxiv.org/abs/{base_id}",
                doi=doi,
                authors=[
                    author_name
                    for author in entry.findall(f"{{{_ATOM_NAMESPACE}}}author")
                    if (
                        author_name := self._optional_text(
                            author, f"{{{_ATOM_NAMESPACE}}}name"
                        )
                    )
                ],
                publisher=_PUBLISHER,
                abstract=self._required_text(entry, f"{{{_ATOM_NAMESPACE}}}summary"),
                published_at=self._required_text(
                    entry, f"{{{_ATOM_NAMESPACE}}}published"
                ),
            )
        except ValidationError as exc:
            raise PaperSourceError("invalid arxiv entry data") from exc

    @staticmethod
    def _element_text(node: ET.Element) -> str:
        return " ".join(node.text.split()) if node.text else ""

    @staticmethod
    def _required_text(node: ET.Element, path: str) -> str:
        child = node.find(path)
        if child is None or child.text is None:
            raise PaperSourceError(f"missing {path} in arxiv entry")
        return " ".join(child.text.split())

    @staticmethod
    def _optional_text(node: ET.Element, path: str) -> str | None:
        child = node.find(path)
        if child is None or child.text is None:
            return None
        text = " ".join(child.text.split())
        return text or None
